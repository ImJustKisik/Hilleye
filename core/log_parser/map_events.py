"""Raw file search fallback and coordinate-bearing round map event collector."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from core.byond_topic import normalize_ckey
from core.log_parser.extractors import (
    _extract_word_ckey_tokens,
    extract_ckeys_from_text,
    extract_coords_from_text,
    primary_coord_from_text,
)
from core.log_parser.filters import (
    _build_entry,
    _evaluate_line_filter,
    _prepare_log_filter,
    _time_hhmm_from_ts,
    iter_parsed_file_lines,
)
from core.log_parser.models import category_info, category_matches_filters
from core.log_parser.sanitizer import sanitize_byond_html


def scan_raw_files_for_query(
    rounds: Sequence[Dict[str, Any]],
    *,
    query: str,
    ckey: str = "",
    preset: str = "all",
    categories: Optional[Sequence[str]] = None,
    date_from: str = "",
    date_to: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Direct file scan fallback for global search (e.g. searching inside DEBUG lines not in FTS)."""
    limit = max(0, int(limit))
    if limit == 0:
        return []
    q_lower = (query or "").strip().lower()
    ckey_norm = normalize_ckey(ckey) if ckey else ""
    hits: List[Dict[str, Any]] = []

    for rnd in rounds:
        if not rnd.get("has_game_log"):
            continue
        rdate = rnd.get("round_date") or ""
        if date_from and rdate and rdate < date_from:
            continue
        if date_to and rdate and rdate > date_to:
            continue
        dir_path = Path(rnd["dir_path"])
        game_log = dir_path / "game.log"
        if not game_log.is_file():
            continue
        for line_no, ts, cat, raw_msg, _is_cont in iter_parsed_file_lines(game_log):
            if not category_matches_filters(cat, preset=preset, selected_categories=categories):
                continue
            _, plain_msg = sanitize_byond_html(raw_msg)
            line_ckeys: Optional[List[str]] = None
            if ckey_norm:
                line_ckeys = extract_ckeys_from_text(plain_msg)
                if ckey_norm not in line_ckeys and ckey_norm not in _extract_word_ckey_tokens(plain_msg):
                    continue
            if q_lower and q_lower not in plain_msg.lower():
                continue
            if line_ckeys is None:
                line_ckeys = extract_ckeys_from_text(plain_msg)
            coords = primary_coord_from_text(raw_msg)
            hits.append(
                {
                    "round_uid": rnd["round_uid"],
                    "round_id": rnd["round_id"],
                    "round_date": rdate,
                    "source_type": rnd["source_type"],
                    "file_name": "game.log",
                    "line_no": line_no,
                    "timestamp": ts,
                    "category": cat,
                    "message": plain_msg,
                    "ckeys": line_ckeys,
                    "coord_x": coords[0] if coords else None,
                    "coord_y": coords[1] if coords else None,
                    "coord_z": coords[2] if coords else None,
                }
            )
            if len(hits) >= limit:
                return hits
    return hits


def collect_round_map_events(
    file_path: Path,
    *,
    preset: str = "gameplay",
    categories: Optional[Sequence[str]] = None,
    ckey: str = "",
    q: str = "",
    use_regex: bool = False,
    time_from: str = "",
    time_to: str = "",
    mask_ips: bool = False,
    render_html: bool = True,
    z_filter: Optional[int] = None,
    limit: int = 5000,
) -> Dict[str, Any]:
    """Collect coordinate-bearing log events from a round file honoring active filters."""
    limit = max(1, min(int(limit), 10000))
    filt = _prepare_log_filter(
        preset=preset,
        categories=categories,
        ckey=ckey,
        q=q,
        use_regex=use_regex,
        time_from=time_from,
        time_to=time_to,
    )
    if filt.regex_error:
        return {
            "events": [],
            "total_with_coords": 0,
            "z_counts": {},
            "truncated": False,
            "regex_error": filt.regex_error,
        }

    events: List[Dict[str, Any]] = []
    z_counts: Dict[int, int] = {}
    total_with_coords = 0
    truncated = False

    for line_no, ts, cat, raw_msg, is_cont in iter_parsed_file_lines(file_path):
        coords_list = extract_coords_from_text(raw_msg)
        if not coords_list:
            continue
        matched, pre_display_raw, pre_sanitized = _evaluate_line_filter(
            ts, cat, raw_msg, filt, mask_ips=mask_ips
        )
        if not matched:
            continue

        primary = coords_list[0]
        primary_z = primary[2]
        z_counts[primary_z] = z_counts.get(primary_z, 0) + 1
        total_with_coords += 1

        if z_filter is not None and primary_z != z_filter:
            continue
        if len(events) >= limit:
            truncated = True
            continue

        entry = _build_entry(
            line_no,
            ts,
            cat,
            raw_msg,
            is_cont,
            mask_ips=mask_ips,
            render_html=render_html,
            pre_display_raw=pre_display_raw,
            pre_sanitized=pre_sanitized,
        )
        meta = category_info(cat)
        events.append(
            {
                "line_no": entry.line_no,
                "timestamp": entry.timestamp,
                "time_short": _time_hhmm_from_ts(entry.timestamp) or entry.timestamp,
                "category": entry.category,
                "cat_css": meta["css"],
                "plain_message": entry.plain_message,
                "safe_html": entry.safe_html,
                "ckeys": entry.ckeys,
                "x": primary[0],
                "y": primary[1],
                "z": primary[2],
                "all_coords": [[cx, cy, cz] for cx, cy, cz in coords_list],
            }
        )

    return {
        "events": events,
        "total_with_coords": total_with_coords,
        "z_counts": {str(z): cnt for z, cnt in sorted(z_counts.items())},
        "truncated": truncated,
        "regex_error": None,
    }
