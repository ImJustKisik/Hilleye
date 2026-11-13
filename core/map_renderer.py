"""SierraBay12 DMM/DMI map renderer, GitHub/local git fetcher, and fast area lookup cache."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
import io
import json
import logging
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import httpx
from PIL import Image

from core.config import AppConfig

logger = logging.getLogger(__name__)

GITHUB_ARCHIVE_TIMEOUT_SECONDS: float = 300.0
MAX_GITHUB_ARCHIVE_BYTES: int = 1024 * 1024 * 1024
MAX_EXTRACTED_REPO_BYTES: int = 1024 * 1024 * 1024

_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")

_DEFINE_RE = re.compile(r"^\s*#define\s+([A-Z0-9_]+)\s+([^\r\n/]+)")
_TYPE_HDR_RE = re.compile(r"^(/+(?:turf|obj|area|mob)(?:/[A-Za-z0-9_]+)*)\s*(?:\{|$)")
_ASSIGN_RE = re.compile(
    r"^(?:\t+| {2,})(icon|icon_state|color|dir|layer|name|alpha)\s*=\s*(.+?)\s*(?://.*)?$"
)
_INLINE_RE = re.compile(
    r"^(/+(?:turf|obj|area|mob)(?:/[A-Za-z0-9_]+)*)/(icon|icon_state|color|dir|layer|name|alpha)\s*=\s*(.+?)\s*(?://.*)?$"
)
_DMM_DEF_START_RE = re.compile(r'^"([^"]+)"\s*=\s*\(', re.MULTILINE)
_DMM_BLOCK_RE = re.compile(r'\((\d+),(\d+),(\d+)\)\s*=\s*\{"([^"]*)"\}', re.DOTALL)
_ATTR_PAIR_RE = re.compile(r'([A-Za-z0-9_]+)\s*=\s*("[^"]*"|\'[^\']*\'|[^;\r\n}]+)')
_AREA_PATH_RE = re.compile(r"/area(?:/[A-Za-z0-9_]+)*")

_DIR_ORDER_4: Dict[int, int] = {2: 0, 1: 1, 4: 2, 8: 3}
_DIR_ORDER_8: Dict[int, int] = {2: 0, 1: 1, 4: 2, 8: 3, 6: 4, 10: 5, 5: 6, 9: 7}

# Technical/non-physical helper prefixes excluded from clean station renders
_SKIP_ATOM_PREFIXES: Tuple[str, ...] = (
    "/area",
    "/turf/space",
    "/obj/effect",
    "/obj/landmark",
    "/obj/shuttle_landmark",
    "/obj/abstract",
    "/mob/observer",
)

_ALLOWED_EXTRACT_PREFIXES: Tuple[str, ...] = (
    "code/",
    "maps/sierra/",
    "mods/",
    "packs/",
    "icons/",
    "nano/images/sierra/",
)


def _should_extract_github_path(rel_str: str) -> bool:
    """Only extract map/icon/DM assets needed for Sierra rendering."""
    low = rel_str.lower()
    if low.startswith(("maps/sierra/", "icons/", "nano/images/sierra/")):
        return True
    if low.startswith(("code/", "mods/", "packs/")):
        return low.endswith((".dm", ".dmi", ".png"))
    return False


@dataclass(frozen=True)
class SierraLevelSpec:
    z: int
    dmm_relpath: str
    dmm_z: int
    label: str
    short_label: str
    nano_fallback: Optional[str] = None


SIERRA_Z_LEVELS: Tuple[SierraLevelSpec, ...] = (
    SierraLevelSpec(
        z=1,
        dmm_relpath="maps/sierra/z1-z4_sierra.dmm",
        dmm_z=1,
        label="Палуба 4 — Инженерный / Карго / Гидропоника",
        short_label="Z1 • Палуба 4",
        nano_fallback="nano/images/sierra/sierra-1.png",
    ),
    SierraLevelSpec(
        z=2,
        dmm_relpath="maps/sierra/z1-z4_sierra.dmm",
        dmm_z=2,
        label="Палуба 3 — Технические отсеки / Ангар",
        short_label="Z2 • Палуба 3",
        nano_fallback="nano/images/sierra/sierra-2.png",
    ),
    SierraLevelSpec(
        z=3,
        dmm_relpath="maps/sierra/z1-z4_sierra.dmm",
        dmm_z=3,
        label="Палуба 2 — Медбэй / Наука / Бар",
        short_label="Z3 • Палуба 2",
        nano_fallback="nano/images/sierra/sierra-3.png",
    ),
    SierraLevelSpec(
        z=4,
        dmm_relpath="maps/sierra/z1-z4_sierra.dmm",
        dmm_z=4,
        label="Палуба 1 — Мостик / СБ / Командование",
        short_label="Z4 • Палуба 1",
        nano_fallback="nano/images/sierra/sierra-4.png",
    ),
    SierraLevelSpec(
        z=5,
        dmm_relpath="maps/sierra/z6_admin.dmm",
        dmm_z=1,
        label="ЦентКом / Админ-зона",
        short_label="Z5 • ЦентКом",
    ),
    SierraLevelSpec(
        z=6,
        dmm_relpath="maps/sierra/z7_transit.dmm",
        dmm_z=1,
        label="Транзит",
        short_label="Z6 • Транзит",
    ),
    SierraLevelSpec(
        z=7,
        dmm_relpath="maps/sierra/z8_virtualreality.dmm",
        dmm_z=1,
        label="Виртуальная реальность",
        short_label="Z7 • VR",
    ),
)

_LEVEL_BY_Z: Dict[int, SierraLevelSpec] = {spec.z: spec for spec in SIERRA_Z_LEVELS}

_map_build_lock = asyncio.Lock()
_background_build_task: Optional[asyncio.Task] = None
_github_transport_factory: Optional[Callable[[], httpx.AsyncBaseTransport]] = None
_area_cache_lock = threading.Lock()
_area_cache_key: Optional[Tuple[str, float]] = None
_area_grids: Dict[int, Tuple[int, int, List[str], List[int]]] = {}
_status_lock = threading.Lock()
_build_status: Dict[str, Any] = {
    "state": "idle",
    "phase": "",
    "source": "",
    "downloaded_mb": 0.0,
    "total_mb": 0.0,
    "detail": "",
    "error": "",
    "started_at": "",
    "finished_at": "",
}


def _set_build_status(**updates: Any) -> None:
    with _status_lock:
        _build_status.update(updates)


def get_map_build_status() -> Dict[str, Any]:
    """Return a snapshot of the current/last station map build task."""
    with _status_lock:
        return dict(_build_status)


def set_github_http_transport(factory: Optional[Callable[[], httpx.AsyncBaseTransport]]) -> None:
    """Test hook for mocking GitHub archive downloads."""
    global _github_transport_factory
    _github_transport_factory = factory


def _clean_dm_value(raw: str, defines: Dict[str, str]) -> str:
    val = raw.strip()
    if (val.startswith("'") and val.endswith("'")) or (val.startswith('"') and val.endswith('"')):
        inner = val[1:-1]
        # Strip BYOND improper/prefix macros like \improper
        if inner.startswith("\\improper "):
            inner = inner[10:]
        elif inner.startswith("\\proper "):
            inner = inner[8:]
        return inner
    return defines.get(val, val)


def _load_dm_defines(repo_dir: Path) -> Dict[str, str]:
    defines: Dict[str, str] = {
        "TURF_LAYER": "2.0",
        "PLATING_LAYER": "2.0",
        "LATTICE_LAYER": "2.02",
        "DISPOSAL_PIPE_LAYER": "2.1",
        "WIRE_LAYER": "2.15",
        "PIPES_LAYER": "2.2",
        "CATWALK_LAYER": "2.3",
        "TURF_DETAIL_LAYER": "2.4",
        "OBJ_LAYER": "3.0",
        "OPEN_DOOR_LAYER": "2.7",
        "TABLE_LAYER": "2.8",
        "CLOSED_DOOR_LAYER": "3.02",
        "ABOVE_OBJ_LAYER": "3.1",
        "MOB_LAYER": "4.0",
        "WALL_OBJ_LAYER": "4.2",
        "BASE_AREA_LAYER": "999",
    }
    for rel in ("code/__defines/colors.dm", "code/__defines/__renderer.dm"):
        p = repo_dir / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _DEFINE_RE.match(line)
            if not m:
                continue
            val = m.group(2).strip().strip('"').strip("'")
            defines[m.group(1)] = val

    for k, v in list(defines.items()):
        if v in defines:
            defines[k] = defines[v]
    return defines


def _scan_dm_type_properties(repo_dir: Path, defines: Dict[str, str]) -> Dict[str, Dict[str, str]]:
    """Scan DM source files to build inheritance map for icon, icon_state, dir, color, layer, name."""
    props: Dict[str, Dict[str, str]] = {}
    for sub in ("code", "maps", "mods", "packs"):
        root = repo_dir / sub
        if not root.is_dir():
            continue
        for dm_file in sorted(root.rglob("*.dm")):
            try:
                lines = dm_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            cur_type: Optional[str] = None
            for line in lines:
                if not line or line.startswith("//"):
                    continue
                if not line[0].isspace():
                    m_inline = _INLINE_RE.match(line)
                    if m_inline:
                        tp = "/" + m_inline.group(1).lstrip("/")
                        props.setdefault(tp, {})[m_inline.group(2)] = _clean_dm_value(m_inline.group(3), defines)
                        cur_type = None
                        continue
                    m_hdr = _TYPE_HDR_RE.match(line)
                    if m_hdr:
                        cur_type = "/" + m_hdr.group(1).lstrip("/")
                        props.setdefault(cur_type, {})
                    else:
                        cur_type = None
                elif cur_type is not None:
                    m_assign = _ASSIGN_RE.match(line)
                    if m_assign:
                        props[cur_type][m_assign.group(1)] = _clean_dm_value(m_assign.group(2), defines)
    return props


def _prettify_area_path(area_path: str) -> str:
    clean = area_path.strip()
    if clean == "/area/space":
        return "Открытый космос"
    parts = [p for p in clean.split("/") if p and p not in ("area", "sierra")]
    if not parts:
        return clean or "Неизвестно"
    return " / ".join(p.replace("_", " ").title() for p in parts)


class _DmiSpriteAtlas:
    """Lazy cache for reading BYOND .dmi PNG sprite sheets and extracting tinted frames."""

    def __init__(self, repo_dir: Path, tile_px: int) -> None:
        self.repo_dir = repo_dir
        self.tile_px = tile_px
        self._dmi_cache: Dict[str, Optional[Tuple[Image.Image, int, int, int, Dict[str, Tuple[int, int, int]]]]] = {}
        self._sprite_cache: Dict[Tuple[str, str, int, str], Optional[Image.Image]] = {}

    def _load_dmi(self, rel_icon: str) -> Optional[Tuple[Image.Image, int, int, int, Dict[str, Tuple[int, int, int]]]]:
        norm_rel = rel_icon.replace("\\", "/").lstrip("/")
        if norm_rel in self._dmi_cache:
            return self._dmi_cache[norm_rel]
        full_path = (self.repo_dir / norm_rel).resolve()
        if not full_path.is_relative_to(self.repo_dir.resolve()) or not full_path.is_file():
            self._dmi_cache[norm_rel] = None
            return None
        try:
            im = Image.open(full_path).convert("RGBA")
        except Exception:
            self._dmi_cache[norm_rel] = None
            return None

        desc = str(im.info.get("Description", ""))
        w, h = 32, 32
        mw = re.search(r"width\s*=\s*(\d+)", desc)
        mh = re.search(r"height\s*=\s*(\d+)", desc)
        if mw:
            w = max(1, int(mw.group(1)))
        if mh:
            h = max(1, int(mh.group(1)))
        cols = max(1, im.width // w)

        states: Dict[str, Tuple[int, int, int]] = {}
        cur_state: Optional[str] = None
        cur_dirs = 1
        cur_frames = 1
        idx = 0
        for line in desc.splitlines():
            ls = line.strip()
            if ls.startswith("state = "):
                if cur_state is not None:
                    states[cur_state] = (cur_dirs, cur_frames, idx)
                    idx += cur_dirs * cur_frames
                cur_state = ls[len("state = ") :].strip().strip('"')
                cur_dirs = 1
                cur_frames = 1
            elif ls.startswith("dirs = "):
                try:
                    cur_dirs = max(1, int(ls.split("=", 1)[1].strip()))
                except ValueError:
                    cur_dirs = 1
            elif ls.startswith("frames = "):
                try:
                    cur_frames = max(1, int(ls.split("=", 1)[1].strip()))
                except ValueError:
                    cur_frames = 1
        if cur_state is not None:
            states[cur_state] = (cur_dirs, cur_frames, idx)

        if not states:
            states[""] = (1, 1, 0)

        loaded = (im, w, h, cols, states)
        self._dmi_cache[norm_rel] = loaded
        return loaded

    def get_sprite(self, icon_path: str, state: str, direction: int, color: str) -> Optional[Image.Image]:
        key = (icon_path, state, direction, color)
        if key in self._sprite_cache:
            return self._sprite_cache[key]
        data = self._load_dmi(icon_path)
        if data is None:
            self._sprite_cache[key] = None
            return None
        im, w, h, cols, states = data
        chosen_state = state if state in states else ("" if "" in states else None)
        if chosen_state is None:
            self._sprite_cache[key] = None
            return None
        dirs, frames, base_idx = states[chosen_state]
        dir_off = 0
        if dirs == 4:
            dir_off = _DIR_ORDER_4.get(direction, 0)
        elif dirs == 8:
            dir_off = _DIR_ORDER_8.get(direction, 0)
        tile_idx = base_idx + dir_off * frames
        r = tile_idx // cols
        c = tile_idx % cols
        crop = im.crop((c * w, r * h, (c + 1) * w, (r + 1) * h))

        if color and color.startswith("#") and len(color) in (7, 9):
            try:
                cr = int(color[1:3], 16) / 255.0
                cg = int(color[3:5], 16) / 255.0
                cb = int(color[5:7], 16) / 255.0
                r_ch, g_ch, b_ch, a_ch = crop.split()
                r_ch = r_ch.point(lambda x: int(x * cr))
                g_ch = g_ch.point(lambda x: int(x * cg))
                b_ch = b_ch.point(lambda x: int(x * cb))
                crop = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_ch))
            except ValueError:
                pass

        if crop.size != (self.tile_px, self.tile_px):
            crop = crop.resize((self.tile_px, self.tile_px), Image.Resampling.NEAREST)

        self._sprite_cache[key] = crop
        return crop


def _split_dmm_tuple(body: str) -> List[str]:
    items: List[str] = []
    cur: List[str] = []
    depth = 0
    in_quote: Optional[str] = None
    for ch in body:
        if in_quote is not None:
            cur.append(ch)
            if ch == in_quote:
                in_quote = None
        else:
            if ch in ('"', "'"):
                in_quote = ch
                cur.append(ch)
            elif ch == "{":
                depth += 1
                cur.append(ch)
            elif ch == "}":
                depth = max(0, depth - 1)
                cur.append(ch)
            elif ch == "," and depth == 0:
                piece = "".join(cur).strip()
                if piece:
                    items.append(piece)
                cur = []
            else:
                cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        items.append(tail)
    return items


def _parse_atom_spec(spec: str, defines: Dict[str, str]) -> Tuple[str, Dict[str, str]]:
    if "{" not in spec:
        return spec.strip(), {}
    tp, rest = spec.split("{", 1)
    rest = rest.rsplit("}", 1)[0]
    overrides: Dict[str, str] = {}
    for attr_name, raw_val in _ATTR_PAIR_RE.findall(rest):
        overrides[attr_name] = _clean_dm_value(raw_val, defines)
    return tp.strip(), overrides


def _iter_dmm_defs(text: str):
    """Yield (key, body) for both single-line DMM and multi-line TGM tile definitions."""
    n = len(text)
    for m in _DMM_DEF_START_RE.finditer(text):
        key = m.group(1)
        pos = m.end()
        depth = 1
        in_quote: Optional[str] = None
        escaped = False
        while pos < n and depth > 0:
            ch = text[pos]
            if in_quote is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == in_quote:
                    in_quote = None
            else:
                if ch in ('"', "'"):
                    in_quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        yield key, text[m.end() : pos]
                        break
            pos += 1


def _encode_rle(values: Sequence[int]) -> List[List[int]]:
    """Run-length encode integer area IDs for compact JSON storage."""
    if not values:
        return []
    rle: List[List[int]] = []
    cur_val = values[0]
    run_len = 1
    for v in values[1:]:
        if v == cur_val:
            run_len += 1
        else:
            rle.append([run_len, cur_val])
            cur_val = v
            run_len = 1
    rle.append([run_len, cur_val])
    return rle


def _decode_rle(rle: Sequence[Sequence[int]], expected_len: int) -> List[int]:
    flat: List[int] = []
    for pair in rle:
        if len(pair) != 2:
            continue
        cnt = max(0, int(pair[0]))
        val = int(pair[1])
        flat.extend([val] * cnt)
    if len(flat) < expected_len:
        flat.extend([0] * (expected_len - len(flat)))
    elif len(flat) > expected_len:
        flat = flat[:expected_len]
    return flat


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


def _detect_local_git_commit(repo_dir: Path) -> str:
    if not (repo_dir / ".git").exists():
        return ""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def _extract_tar_subset_sync(archive_path: Path, dest_dir: Path) -> None:
    dest_resolved = dest_dir.resolve()
    total_extracted = 0
    with tarfile.open(archive_path, mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isreg():
                continue
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            rel_parts = parts[1:]
            if any(p in ("..", "") for p in rel_parts):
                continue
            rel_str = "/".join(rel_parts)
            if not _should_extract_github_path(rel_str):
                continue
            total_extracted += member.size
            if total_extracted > MAX_EXTRACTED_REPO_BYTES:
                raise ValueError("Превышен лимит размера распаковки архива GitHub.")
            out_path = (dest_resolved / Path(*rel_parts)).resolve()
            if not out_path.is_relative_to(dest_resolved):
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, out_path.open("wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)


async def _download_github_repo_subset(
    github_repo: str,
    github_branch: str,
    dest_dir: Path,
) -> None:
    """Safely stream and selectively extract map/icon files from GitHub archive tarball."""
    repo_clean = (github_repo or "").strip()
    branch_clean = (github_branch or "").strip()
    if not _GITHUB_REPO_RE.match(repo_clean):
        raise ValueError("Некорректное имя GitHub-репозитория (ожидается owner/repo).")
    if not _GITHUB_BRANCH_RE.match(branch_clean) or ".." in branch_clean:
        raise ValueError("Некорректное имя ветки GitHub.")

    url = f"https://github.com/{repo_clean}/archive/refs/heads/{branch_clean}.tar.gz"
    transport = _github_transport_factory() if _github_transport_factory else None
    client_kwargs: Dict[str, Any] = {
        "timeout": httpx.Timeout(
            connect=15.0,
            read=GITHUB_ARCHIVE_TIMEOUT_SECONDS,
            write=30.0,
            pool=15.0,
        ),
        "follow_redirects": True,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    archive_path = dest_dir / "_github_archive.tar.gz"
    downloaded = 0
    _set_build_status(
        phase="downloading_github",
        downloaded_mb=0.0,
        total_mb=0.0,
        detail=f"Скачивание {repo_clean}@{branch_clean} с GitHub...",
    )
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"GitHub вернул HTTP {resp.status_code} при загрузке {url}")
                content_len = resp.headers.get("content-length")
                total_mb = round(int(content_len) / (1024 * 1024), 1) if content_len and content_len.isdigit() else 0.0
                _set_build_status(total_mb=total_mb)
                with archive_path.open("wb") as fp:
                    async for chunk in resp.aiter_bytes(chunk_size=262144):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > MAX_GITHUB_ARCHIVE_BYTES:
                            raise ValueError("Архив GitHub превышает допустимый размер.")
                        fp.write(chunk)
                        _set_build_status(
                            downloaded_mb=round(downloaded / (1024 * 1024), 1),
                            detail=f"Скачивание с GitHub: {downloaded / (1024 * 1024):.1f} МБ...",
                        )

        _set_build_status(
            phase="extracting",
            detail="Распаковка карт и спрайтов из архива GitHub...",
        )
        await asyncio.to_thread(_extract_tar_subset_sync, archive_path, dest_dir)
    finally:
        archive_path.unlink(missing_ok=True)


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
    global _area_cache_key, _area_grids
    with _area_cache_lock:
        _area_cache_key = None
        _area_grids = {}


def _ensure_area_cache_loaded(cache_dir: Path) -> Dict[int, Tuple[int, int, List[str], List[int]]]:
    global _area_cache_key, _area_grids
    manifest_path = cache_dir / "manifest.json"
    mtime = manifest_path.stat().st_mtime if manifest_path.is_file() else -1.0
    key = (str(cache_dir.resolve()), mtime)
    with _area_cache_lock:
        if _area_cache_key == key and _area_grids:
            return _area_grids
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
        _area_cache_key = key
        _area_grids = loaded
        return _area_grids


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


def deck_short_label(z_level: int) -> str:
    spec = _LEVEL_BY_Z.get(int(z_level))
    if spec:
        return spec.short_label
    return f"Z{int(z_level)}"


async def build_map_cache(
    config: AppConfig,
    *,
    force: bool = False,
    prefer_github: bool = False,
) -> Dict[str, Any]:
    """Build or refresh cached PNGs & area metadata from local git or GitHub."""
    cache_dir = config.resolved_maps_cache_dir()
    async with _map_build_lock:
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
                with tempfile.TemporaryDirectory(prefix="hilleye_maps_") as tmp_dir:
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
    global _background_build_task
    if _background_build_task is not None and not _background_build_task.done():
        return False

    async def _runner() -> None:
        try:
            await build_map_cache(config, force=force, prefer_github=prefer_github)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Background station map rebuild failed", exc_info=True)

    _background_build_task = asyncio.create_task(_runner())
    return True
