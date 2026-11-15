"""Extractors for BYOND ckeys and 3D station map coordinates from log text."""

from __future__ import annotations

from typing import List, Optional, Set, Tuple

from core.byond_topic import normalize_ckey
from core.log_parser.models import (
    _CKEY_SLASH_COLON_RE,
    _CKEY_START_RE,
    _CKEY_TOKEN_RE,
    _COORD_BRACKET_RX,
    _COORD_PATTERNS,
    _NON_ALNUM_RE,
    _RGB_PREFIX_RE,
    _WORD_TOKEN_RE,
)


def _is_valid_extracted_ckey(norm: str) -> bool:
    return bool(norm) and any("a" <= ch <= "z" for ch in norm)


def extract_ckeys_from_text(plain_text: str) -> List[str]:
    """Extract ordered unique normalized BYOND ckeys from a plain-text log message."""
    seen: Set[str] = set()
    found: List[str] = []
    search_pos = 0

    m_start = _CKEY_START_RE.match(plain_text)
    if m_start:
        norm = normalize_ckey(m_start.group(1))
        if _is_valid_extracted_ckey(norm) and norm not in seen:
            seen.add(norm)
            found.append(norm)
        search_pos = m_start.end()
    else:
        m_slash = _CKEY_SLASH_COLON_RE.match(plain_text)
        if m_slash and not m_slash.group(1).endswith(" "):
            norm = normalize_ckey(m_slash.group(2))
            if _is_valid_extracted_ckey(norm) and norm not in seen:
                seen.add(norm)
                found.append(norm)
            search_pos = m_slash.end()

    for m in _CKEY_TOKEN_RE.finditer(plain_text, pos=search_pos):
        norm = normalize_ckey(m.group(1))
        if _is_valid_extracted_ckey(norm) and norm not in seen:
            seen.add(norm)
            found.append(norm)

    return found


def _extract_word_ckey_tokens(plain_text: str) -> Set[str]:
    """Extract normalized word-bounded tokens for fallback ckey filtering on arbitrary-length lines."""
    tokens: Set[str] = set()
    for m in _WORD_TOKEN_RE.finditer(plain_text):
        norm = _NON_ALNUM_RE.sub("", m.group(0).lower())
        if norm:
            tokens.add(norm)
    return tokens


def extract_coords_from_text(text: str) -> List[Tuple[int, int, int]]:
    """Extract ordered unique valid BYOND map coordinates (x, y, z) from a log message."""
    if not text:
        return []
    matches: List[Tuple[int, int, Tuple[int, int, int]]] = []
    for rx in _COORD_PATTERNS:
        for m in rx.finditer(text):
            if rx is _COORD_BRACKET_RX:
                pair = text[m.start()] + text[m.end() - 1]
                if pair not in ("()", "[]"):
                    continue
                if _RGB_PREFIX_RE.search(text[: m.start()]):
                    continue
            try:
                x = int(m.group(1))
                y = int(m.group(2))
                z = int(m.group(3))
            except ValueError:
                continue
            # Valid SS13 turf coordinates start at (1,1,1); exclude nullspace (0,0,0) and bogus values
            if 1 <= x <= 255 and 1 <= y <= 255 and 1 <= z <= 99:
                matches.append((m.start(), m.end(), (x, y, z)))
    matches.sort(key=lambda item: item[0])
    seen: Set[Tuple[int, int, int]] = set()
    ordered: List[Tuple[int, int, int]] = []
    for _start, _end, xyz in matches:
        if xyz not in seen:
            seen.add(xyz)
            ordered.append(xyz)
    return ordered


def primary_coord_from_text(text: str) -> Optional[Tuple[int, int, int]]:
    """Return the first valid (x, y, z) coordinate found in text, or None."""
    coords = extract_coords_from_text(text)
    return coords[0] if coords else None
