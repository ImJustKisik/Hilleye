"""Raw file search fallback and coordinate-bearing round map event collector."""

from __future__ import annotations

from contextlib import closing
from itertools import islice
from pathlib import Path
import time
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
    _collect_ckey_coord_anchors,
    _evaluate_line_filter,
    _iter_parsed_log_stream,
    _prepare_log_filter,
    _time_hhmm_from_ts,
    iter_parsed_file_lines,
)
from core.log_parser.models import category_info, category_matches_filters
from core.log_parser.sanitizer import prepare_log_message


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
    """Return up to limit matches for internal callers that only need a prefix."""
    return list(islice(_iter_raw_search_hits(
        rounds, query=query, ckey=ckey, preset=preset, categories=categories,
        date_from=date_from, date_to=date_to,
    ), max(0, int(limit))))


class _RawSearchLimit(Exception):
    pass


def search_raw_log_files(rounds, *, offset, limit, **options):
    """Return a page and count within a bounded, explicitly reported scan window."""
    deadline = time.monotonic() + 8
    remaining = 64 * 1024 * 1024

    def read_lines(stream):
        nonlocal remaining
        while True:
            if time.monotonic() >= deadline:
                raise _RawSearchLimit
            line = stream.readline(min(remaining, 1024 * 1024) + 1)
            if not line:
                return
            if len(line) > min(remaining, 1024 * 1024):
                raise _RawSearchLimit
            remaining -= len(line)
            yield line

    hits = []
    total = 0
    truncated = False
    matches = _iter_raw_search_hits(rounds, read_lines=read_lines, **options)
    with closing(matches):
        while True:
            try:
                hit = next(matches, None)
            except _RawSearchLimit:
                truncated = True
                break
            if hit is None:
                break
            if offset <= total < offset + limit:
                hits.append(hit)
            total += 1
    return hits, total, truncated


def _iter_raw_search_hits(
    rounds, *, query, ckey="", preset="all", categories=None, date_from="", date_to="",
    file_resolver=None, read_lines=iter, z_level=None, only_coords=False, mask_ips=False,
):
    q_lower = (query or "").strip().lower()
    ckey_norm = normalize_ckey(ckey) if ckey else ""

    for rnd in rounds:
        if not rnd.get("has_game_log"):
            continue
        rdate = rnd.get("round_date") or ""
        if date_from and rdate and rdate < date_from:
            continue
        if date_to and rdate and rdate > date_to:
            continue
        game_log = file_resolver(rnd) if file_resolver else Path(rnd["dir_path"]) / "game.log"
        if game_log is None:
            continue
        try:
            stream = game_log.open("rb")
        except FileNotFoundError:
            continue
        with stream:
            for line_no, ts, cat, raw_msg, _ in _iter_parsed_log_stream(read_lines(stream)):
                if not category_matches_filters(cat, preset=preset, selected_categories=categories):
                    continue
                _, (_, plain_msg) = prepare_log_message(raw_msg, mask_ips=mask_ips)
                if q_lower and q_lower not in plain_msg.lower():
                    continue
                line_ckeys = extract_ckeys_from_text(plain_msg)
                if ckey_norm and ckey_norm not in line_ckeys and ckey_norm not in _extract_word_ckey_tokens(plain_msg):
                    continue
                coords = primary_coord_from_text(raw_msg)
                if (z_level is not None and (coords is None or coords[2] != z_level)) or (only_coords and not coords):
                    continue
                yield {
                    "round_uid": rnd["round_uid"],
                    "round_id": rnd["round_id"],
                    "round_date": rdate,
                    "source_type": rnd["source_type"],
                    "map_revision": rnd.get("map_revision", ""),
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


def collect_round_map_events(
    file_path: Path,
    *,
    preset: str = "gameplay",
    categories: Optional[Sequence[str]] = None,
    ckey: str = "",
    radius: int = 0,
    q: str = "",
    use_regex: bool = False,
    time_from: str = "",
    time_to: str = "",
    mask_ips: bool = False,
    render_html: bool = True,
    z_filter: Optional[int] = None,
    limit: int = 5000,
    offset: int = 0,
    focus_line: Optional[int] = None,
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
        radius=radius,
    )
    if filt.regex_error or filt.invalid_time_fields:
        return {
            "events": [],
            "total_with_coords": 0,
            "z_counts": {},
            "truncated": False,
            "regex_error": filt.regex_error,
            "invalid_time_fields": filt.invalid_time_fields,
        }

    anchors = (
        _collect_ckey_coord_anchors(file_path, filt.ckey_norm)
        if (filt.ckey_norm and filt.radius > 0)
        else []
    )
    anchor_hint = 0

    events: List[Dict[str, Any]] = []
    z_counts: Dict[int, int] = {}
    total_with_coords = 0
    selected_count = 0
    pinned_event = None

    for line_no, ts, cat, raw_msg, is_cont in iter_parsed_file_lines(file_path):
        coords_list = extract_coords_from_text(raw_msg)
        if not coords_list:
            continue
        matched, pre_display_raw, pre_sanitized, anchor_hint = _evaluate_line_filter(
            ts,
            cat,
            raw_msg,
            filt,
            mask_ips=mask_ips,
            line_no=line_no,
            anchors=anchors,
            anchor_hint=anchor_hint,
        )
        if not matched:
            continue

        primary = coords_list[0]
        primary_z = primary[2]
        z_counts[primary_z] = z_counts.get(primary_z, 0) + 1
        total_with_coords += 1

        if z_filter is not None and primary_z != z_filter:
            continue
        in_page = offset <= selected_count < offset + limit
        selected_count += 1
        if not in_page and line_no != focus_line:
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
        event = {
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
        if in_page:
            events.append(event)
        else:
            pinned_event = event

    if pinned_event is not None:
        events.append(pinned_event)
        events.sort(key=lambda record: record["line_no"])

    return {
        "events": events,
        "total_with_coords": total_with_coords,
        "z_counts": {str(z): cnt for z, cnt in sorted(z_counts.items())},
        "truncated": selected_count > limit,
        "offset": offset,
        "previous_offset": max(0, offset - limit) if offset else None,
        "next_offset": offset + limit if selected_count > offset + limit else None,
        "selected_count": selected_count,
        "regex_error": None,
    }
