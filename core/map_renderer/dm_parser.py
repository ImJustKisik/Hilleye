"""BYOND .dm defines/types parser, .dmi sprite atlas, .dmm definition parser, and RLE codec."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image

from core.map_renderer.specs import (
    _ASSIGN_RE,
    _ATTR_PAIR_RE,
    _DEFINE_RE,
    _DIR_ORDER_4,
    _DIR_ORDER_8,
    _DMM_DEF_START_RE,
    _INLINE_RE,
    _TYPE_HDR_RE,
)


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
