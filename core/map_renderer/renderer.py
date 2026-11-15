"""DMM/DMI Z-level rendering pipeline, manifest/tile resolution, and area lookup cache."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image

from core.config import AppConfig
from core.map_renderer.dm_parser import (
    _DmiSpriteAtlas,
    _decode_rle,
    _encode_rle,
    _iter_dmm_defs,
    _load_dm_defines,
    _parse_atom_spec,
    _prettify_area_path,
    _scan_dm_type_properties,
    _split_dmm_tuple,
)
from core.map_renderer.repo_loader import (
    _detect_local_git_commit,
    _download_github_repo_subset,
)
from core.map_renderer.specs import (
    SIERRA_Z_LEVELS,
    SierraLevelSpec,
    _DMM_BLOCK_RE,
    _LEVEL_BY_Z,
    _SKIP_ATOM_PREFIXES,
    _set_build_status,
)

logger = logging.getLogger(__name__)


def _render_nano_fallback(nano_path: Path, width: int, height: int, tile_px: int) -> Optional[Image.Image]:
    if not nano_path.is_file():
        return None
    try:
        with Image.open(nano_path) as im:
            rgba = im.convert("RGBA")
            # NanoMapGen dumps 200x200 tiles at 4px/tile bottom-up onto 1024x1024 -> (0, 1024 - h*4, w*4, 1024)
            crop_w = min(rgba.width, width * 4)
            crop_h = min(rgba.height, height * 4)
            top = max(0, rgba.height - crop_h)
            cropped = rgba.crop((0, top, crop_w, top + crop_h))
            return cropped.resize((width * tile_px, height * tile_px), Image.Resampling.NEAREST)
    except Exception:
        return None


def _render_dmm_file_levels(
    dmm_path: Path,
    specs: Sequence[SierraLevelSpec],
    *,
    repo_dir: Path,
    cache_dir: Path,
    tile_px: int,
    defines: Dict[str, str],
    type_props: Dict[str, Dict[str, str]],
    atlas: _DmiSpriteAtlas,
) -> List[Dict[str, Any]]:
    """Parse one .dmm file once and render all requested Sierra Z-levels contained in it."""
    text = dmm_path.read_text(encoding="utf-8", errors="replace")

    resolved_cache: Dict[str, Dict[str, str]] = {}

    def resolve_type(tp: str) -> Dict[str, str]:
        if tp in resolved_cache:
            return resolved_cache[tp]
        parts = [p for p in tp.split("/") if p]
        merged: Dict[str, str] = {}
        for i in range(1, len(parts) + 1):
            merged.update(type_props.get("/" + "/".join(parts[:i]), {}))
        resolved_cache[tp] = merged
        return merged

    area_palette: List[str] = ["Открытый космос"]
    area_to_idx: Dict[str, int] = {"Открытый космос": 0}

    def get_area_idx(area_path: str, area_ovr: Dict[str, str]) -> int:
        props_area = resolve_type(area_path)
        name = (area_ovr.get("name") or props_area.get("name") or "").strip()
        if not name:
            name = _prettify_area_path(area_path)
        idx = area_to_idx.get(name)
        if idx is None:
            idx = len(area_palette)
            area_to_idx[name] = idx
            area_palette.append(name)
        return idx

    key_len = 1
    key_tile_img: Dict[str, Optional[Image.Image]] = {}
    key_area_idx: Dict[str, int] = {}

    for key, body in _iter_dmm_defs(text):
        key_len = max(key_len, len(key))
        atoms = _split_dmm_tuple(body)

        area_idx = 0
        draw_list: List[Tuple[Tuple[int, float], str, str, int, str]] = []

        for atom_spec in atoms:
            tp, ovr = _parse_atom_spec(atom_spec, defines)
            if tp.startswith("/area"):
                area_idx = get_area_idx(tp, ovr)
                continue
            if any(tp.startswith(prefix) for prefix in _SKIP_ATOM_PREFIXES):
                continue
            merged = dict(resolve_type(tp))
            merged.update(ovr)
            icon = merged.get("icon")
            if not icon:
                continue
            state = merged.get("icon_state", "")
            try:
                direction = int(float(merged.get("dir", 2)))
            except (ValueError, TypeError):
                direction = 2
            color = merged.get("color", "")
            try:
                layer = float(merged.get("layer", 2.0 if tp.startswith("/turf") else 3.0))
            except (ValueError, TypeError):
                layer = 2.0 if tp.startswith("/turf") else 3.0
            prio = (0 if tp.startswith("/turf") else 1, layer)
            draw_list.append((prio, icon, state, direction, color))

        key_area_idx[key] = area_idx
        if not draw_list:
            key_tile_img[key] = None
            continue
        draw_list.sort(key=lambda item: item[0])
        tile_img: Optional[Image.Image] = None
        for _prio, ic, st, dr, col in draw_list:
            sp = atlas.get_sprite(ic, st, dr, col)
            if sp is not None:
                if tile_img is None:
                    tile_img = Image.new("RGBA", (tile_px, tile_px), (0, 0, 0, 0))
                tile_img.alpha_composite(sp)
        key_tile_img[key] = tile_img

    # Group coordinate blocks by dmm_z
    blocks_by_z: Dict[int, List[Tuple[int, int, List[str]]]] = {}
    dims_by_z: Dict[int, Tuple[int, int]] = {}
    for xs, ys, zs, grid in _DMM_BLOCK_RE.findall(text):
        dz = int(zs)
        x0, y0 = int(xs), int(ys)
        lines = [ln for ln in grid.strip("\r\n").splitlines() if ln]
        if not lines:
            continue
        h = len(lines)
        w = max(1, len(lines[0]) // key_len)
        blocks_by_z.setdefault(dz, []).append((x0, y0, lines))
        prev_w, prev_h = dims_by_z.get(dz, (0, 0))
        dims_by_z[dz] = (max(prev_w, x0 + w - 1), max(prev_h, y0 + h - 1))

    rendered_levels: List[Dict[str, Any]] = []
    for spec in specs:
        width, height = dims_by_z.get(spec.dmm_z, (200, 200))
        blocks = blocks_by_z.get(spec.dmm_z, [])
        flat_areas = [0] * (width * height)
        canvas = Image.new("RGBA", (width * tile_px, height * tile_px), (9, 13, 20, 255))
        placed_sprites = 0

        for x0, y0, lines in blocks:
            h = len(lines)
            for r_idx, row in enumerate(lines):
                byond_y = y0 + (h - 1 - r_idx)
                if not (1 <= byond_y <= height):
                    continue
                py_y = (height - byond_y) * tile_px
                row_offset = (byond_y - 1) * width
                for c_idx in range(len(row) // key_len):
                    bx = x0 + c_idx
                    if not (1 <= bx <= width):
                        continue
                    key = row[c_idx * key_len : (c_idx + 1) * key_len]
                    flat_areas[row_offset + (bx - 1)] = key_area_idx.get(key, 0)
                    timg = key_tile_img.get(key)
                    if timg is not None:
                        canvas.alpha_composite(timg, ((bx - 1) * tile_px, py_y))
                        placed_sprites += 1

        render_mode = "dmm_dmi"
        if placed_sprites == 0 and spec.nano_fallback:
            fallback_img = _render_nano_fallback(repo_dir / spec.nano_fallback, width, height, tile_px)
            if fallback_img is not None:
                canvas = fallback_img
                render_mode = "nano_fallback"

        png_name = f"z{spec.z}.png"
        areas_name = f"areas_z{spec.z}.json"
        canvas.save(cache_dir / png_name, optimize=True)

        # Compact per-level area palette (keep only areas referenced on this Z-level)
        used_ids = sorted(set(flat_areas))
        remap = {old_id: new_id for new_id, old_id in enumerate(used_ids)}
        compact_palette = [area_palette[old_id] for old_id in used_ids]
        compact_flat = [remap[v] for v in flat_areas]

        areas_payload = {
            "z": spec.z,
            "width": width,
            "height": height,
            "palette": compact_palette,
            "rle": _encode_rle(compact_flat),
        }
        (cache_dir / areas_name).write_text(
            json.dumps(areas_payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        rendered_levels.append(
            {
                "z": spec.z,
                "label": spec.label,
                "short_label": spec.short_label,
                "dmm": spec.dmm_relpath,
                "dmm_z": spec.dmm_z,
                "width": width,
                "height": height,
                "tile_px": tile_px,
                "image_file": png_name,
                "areas_file": areas_name,
                "area_count": len(compact_palette),
                "render_mode": render_mode,
            }
        )

    return rendered_levels


def _render_maps_from_repo_sync(
    repo_dir: Path,
    cache_dir: Path,
    *,
    tile_px: int,
    source_kind: str,
    commit_sha: str,
    github_repo: str,
    github_branch: str,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    cache_dir.mkdir(parents=True, exist_ok=True)
    defines = _load_dm_defines(repo_dir)
    type_props = _scan_dm_type_properties(repo_dir, defines)
    atlas = _DmiSpriteAtlas(repo_dir, tile_px)

    # Group Z-level specs by .dmm file so multi-Z files like z1-z4_sierra.dmm parse only once
    by_dmm: Dict[str, List[SierraLevelSpec]] = {}
    for spec in SIERRA_Z_LEVELS:
        if (repo_dir / spec.dmm_relpath).is_file():
            by_dmm.setdefault(spec.dmm_relpath, []).append(spec)
        elif spec.nano_fallback and (repo_dir / spec.nano_fallback).is_file():
            # Render nano fallback directly even if .dmm missing
            by_dmm.setdefault(spec.dmm_relpath, []).append(spec)

    if not by_dmm:
        raise FileNotFoundError(f"В источнике {repo_dir} не найдены файлы карт maps/sierra/*.dmm")

    all_levels: List[Dict[str, Any]] = []
    for dmm_rel, specs in by_dmm.items():
        dmm_file = repo_dir / dmm_rel
        if dmm_file.is_file():
            all_levels.extend(
                _render_dmm_file_levels(
                    dmm_file,
                    specs,
                    repo_dir=repo_dir,
                    cache_dir=cache_dir,
                    tile_px=tile_px,
                    defines=defines,
                    type_props=type_props,
                    atlas=atlas,
                )
            )
        else:
            for spec in specs:
                if not spec.nano_fallback:
                    continue
                fb = _render_nano_fallback(repo_dir / spec.nano_fallback, 200, 200, tile_px)
                if fb is None:
                    continue
                png_name = f"z{spec.z}.png"
                areas_name = f"areas_z{spec.z}.json"
                fb.save(cache_dir / png_name, optimize=True)
                (cache_dir / areas_name).write_text(
                    json.dumps(
                        {
                            "z": spec.z,
                            "width": 200,
                            "height": 200,
                            "palette": ["NSV Sierra"],
                            "rle": [[40000, 0]],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                all_levels.append(
                    {
                        "z": spec.z,
                        "label": spec.label,
                        "short_label": spec.short_label,
                        "dmm": spec.dmm_relpath,
                        "dmm_z": spec.dmm_z,
                        "width": 200,
                        "height": 200,
                        "tile_px": tile_px,
                        "image_file": png_name,
                        "areas_file": areas_name,
                        "area_count": 1,
                        "render_mode": "nano_fallback",
                    }
                )

    all_levels.sort(key=lambda item: item["z"])
    elapsed = round(time.perf_counter() - t0, 2)
    manifest = {
        "ready": bool(all_levels),
        "source_kind": source_kind,
        "repo_dir": str(repo_dir) if source_kind == "local_git" else "",
        "github_repo": github_repo,
        "github_branch": github_branch,
        "commit": commit_sha,
        "tile_px": tile_px,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": elapsed,
        "levels": all_levels,
    }
    (cache_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    invalidate_area_cache()
    return manifest


def get_map_manifest(cache_dir: Path) -> Dict[str, Any]:
    """Read cached manifest.json safely, or return unbuilt status skeleton."""
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.is_file():
        return {
            "ready": False,
            "source_kind": "none",
            "commit": "",
            "tile_px": 16,
            "generated_at": None,
            "elapsed_seconds": 0.0,
            "levels": [],
        }
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "ready": False,
        "source_kind": "error",
        "commit": "",
        "tile_px": 16,
        "generated_at": None,
        "elapsed_seconds": 0.0,
        "levels": [],
    }


def get_map_image_path(cache_dir: Path, z_level: int) -> Optional[Path]:
    """Resolve cached PNG path for z_level with strict boundary checks."""
    if z_level not in _LEVEL_BY_Z:
        return None
    root = cache_dir.resolve()
    candidate = (root / f"z{int(z_level)}.png").resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def get_map_areas_path(cache_dir: Path, z_level: int) -> Optional[Path]:
    """Resolve cached areas_zN.json path for z_level with strict boundary checks."""
    if z_level not in _LEVEL_BY_Z:
        return None
    root = cache_dir.resolve()
    candidate = (root / f"areas_z{int(z_level)}.json").resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return None
    return candidate


def invalidate_area_cache() -> None:
    import core.map_renderer as _mr

    with _mr._area_cache_lock:
        _mr._area_cache_key = None
        _mr._area_grids = {}


def _ensure_area_cache_loaded(cache_dir: Path) -> Dict[int, Tuple[int, int, List[str], List[int]]]:
    import core.map_renderer as _mr

    manifest_path = cache_dir / "manifest.json"
    mtime = manifest_path.stat().st_mtime if manifest_path.is_file() else -1.0
    key = (str(cache_dir.resolve()), mtime)
    with _mr._area_cache_lock:
        if _mr._area_cache_key == key and _mr._area_grids:
            return _mr._area_grids
        loaded: Dict[int, Tuple[int, int, List[str], List[int]]] = {}
        for spec in SIERRA_Z_LEVELS:
            p = cache_dir / f"areas_z{spec.z}.json"
            if not p.is_file():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                w = int(data["width"])
                h = int(data["height"])
                palette = [str(x) for x in data.get("palette", [])]
                flat = _decode_rle(data.get("rle", []), w * h)
                loaded[spec.z] = (w, h, palette, flat)
            except Exception:
                continue
        _mr._area_cache_key = key
        _mr._area_grids = loaded
        return _mr._area_grids


def lookup_area_name(cache_dir: Path, z_level: int, x: int, y: int) -> str:
    """Return human-readable Sierra area name at (x, y, z) or empty string if unmapped."""
    grids = _ensure_area_cache_loaded(cache_dir)
    entry = grids.get(int(z_level))
    if entry is None:
        if int(z_level) > 7:
            return f"Сектор / Экспедиция (Z{int(z_level)})"
        return ""
    width, height, palette, flat = entry
    if not (1 <= x <= width and 1 <= y <= height):
        return ""
    idx = flat[(y - 1) * width + (x - 1)]
    if 0 <= idx < len(palette):
        return palette[idx]
    return ""


async def build_map_cache(
    config: AppConfig,
    *,
    force: bool = False,
    prefer_github: bool = False,
) -> Dict[str, Any]:
    """Build or refresh cached PNGs & area metadata from local git or GitHub."""
    import core.map_renderer as _mr

    cache_dir = config.resolved_maps_cache_dir()
    async with _mr._map_build_lock:
        if not force:
            existing = get_map_manifest(cache_dir)
            if existing.get("ready"):
                return existing

        local_repo = config.resolved_maps_repo_dir()
        use_local = (
            not prefer_github
            and local_repo is not None
            and (local_repo / "maps" / "sierra").is_dir()
        )
        _set_build_status(
            state="running",
            phase="rendering" if use_local else "downloading_github",
            source="local_git" if use_local else "github",
            downloaded_mb=0.0,
            total_mb=0.0,
            detail="Рендер палуб Z1–Z7 из локального Git..." if use_local else "Подготовка загрузки с GitHub...",
            error="",
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            finished_at="",
        )
        try:
            if use_local and local_repo is not None:
                commit_sha = await asyncio.to_thread(_detect_local_git_commit, local_repo)
                manifest = await asyncio.to_thread(
                    _render_maps_from_repo_sync,
                    local_repo,
                    cache_dir,
                    tile_px=config.maps_tile_px,
                    source_kind="local_git",
                    commit_sha=commit_sha,
                    github_repo=config.maps_github_repo,
                    github_branch=config.maps_github_branch,
                )
            else:
                cache_dir.mkdir(parents=True, exist_ok=True)
                for stale in cache_dir.glob("_gh_src_*"):
                    if stale.is_dir():
                        shutil.rmtree(stale, ignore_errors=True)
                with tempfile.TemporaryDirectory(prefix="_gh_src_", dir=cache_dir) as tmp_dir:
                    tmp_path = Path(tmp_dir)
                    await _download_github_repo_subset(
                        config.maps_github_repo,
                        config.maps_github_branch,
                        tmp_path,
                    )
                    _set_build_status(
                        phase="rendering",
                        detail="Рендер палуб Z1–Z7 (.dmm + .dmi)...",
                    )
                    manifest = await asyncio.to_thread(
                        _render_maps_from_repo_sync,
                        tmp_path,
                        cache_dir,
                        tile_px=config.maps_tile_px,
                        source_kind="github",
                        commit_sha=config.maps_github_branch,
                        github_repo=config.maps_github_repo,
                        github_branch=config.maps_github_branch,
                    )
        except Exception as exc:
            _set_build_status(
                state="error",
                phase="error",
                detail="",
                error=str(exc),
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            raise

        _set_build_status(
            state="ready",
            phase="done",
            detail=f"Собрано {len(manifest.get('levels', []))} палуб за {manifest.get('elapsed_seconds')}с",
            error="",
            finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        return manifest


def start_map_rebuild_background(
    config: AppConfig,
    *,
    force: bool = True,
    prefer_github: bool = False,
) -> bool:
    """Launch station map build in the background; returns False if already running."""
    import core.map_renderer as _mr

    if _mr._background_build_task is not None and not _mr._background_build_task.done():
        return False

    async def _runner() -> None:
        try:
            await build_map_cache(config, force=force, prefer_github=prefer_github)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Background station map rebuild failed", exc_info=True)

    _mr._background_build_task = asyncio.create_task(_runner())
    return True
