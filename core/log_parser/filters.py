"""Line streaming, ReDoS-safe regex compilation, time filtering, pagination, and export."""

from __future__ import annotations

from datetime import datetime
from html import escape as html_escape
from pathlib import Path
import re
from typing import Any, Dict, Iterator, List, Optional, Pattern, Sequence, Tuple

from core.byond_topic import normalize_ckey
from core.log_parser.extractors import (
    _extract_word_ckey_tokens,
    extract_ckeys_from_text,
    extract_coords_from_text,
)
from core.log_parser.models import (
    LogPageResult,
    ParsedLogLine,
    _CAT_PREFIX_RE,
    _PreparedLogFilter,
    _QUANT_BRACE_RE,
    _RoundMetaAccumulator,
    _TIME_BOUND_RE,
    _TS_LINE_RE,
    category_matches_filters,
)
from core.log_parser.sanitizer import (
    decode_log_bytes,
    mask_sensitive_text,
    sanitize_byond_html,
)


def parse_raw_line(
    line_no: int,
    raw_line: str,
    prev_timestamp: str = "",
    prev_category: str = "SYSTEM",
) -> Tuple[str, str, str, bool]:
    """Parse a single physical line into (timestamp, category, raw_message, is_continuation)."""
    stripped = raw_line.rstrip("\r\n")
    m_ts = _TS_LINE_RE.match(stripped)
    if m_ts:
        ts = m_ts.group(1)
        rest = m_ts.group(2)
        m_cat = _CAT_PREFIX_RE.match(rest)
        if m_cat:
            return ts, m_cat.group(1), m_cat.group(2), False
        return ts, "SYSTEM", rest, False

    return prev_timestamp, prev_category or "SYSTEM", stripped, True


def _decode_log_line(raw_line: bytes) -> str:
    try:
        return raw_line.decode("utf-8")
    except UnicodeDecodeError:
        return decode_log_bytes(raw_line)


def iter_parsed_file_lines(file_path: Path) -> Iterator[Tuple[int, str, str, str, bool]]:
    """Stream physical lines from a log file yielding (line_no, timestamp, category, raw_message, is_continuation)."""
    prev_ts = ""
    prev_cat = "SYSTEM"
    with file_path.open("rb") as fh:
        for line_no, raw_bytes in enumerate(fh, 1):
            raw_line = _decode_log_line(raw_bytes)
            if line_no == 1 and raw_line.startswith("\ufeff"):
                raw_line = raw_line.lstrip("\ufeff")
            ts, cat, msg, is_cont = parse_raw_line(line_no, raw_line, prev_ts, prev_cat)
            if not is_cont:
                prev_ts = ts
                prev_cat = cat
            yield line_no, ts, cat, msg, is_cont


def inspect_game_log_summary(file_path: Path) -> Dict[str, Any]:
    """Scan a game.log file once to compute line counts, category histogram, timestamps, and round_id."""
    acc = _RoundMetaAccumulator()
    for _line_no, ts, cat, msg, is_cont in iter_parsed_file_lines(file_path):
        acc.observe(ts, cat, msg, is_cont)

    return {
        "total_lines": acc.total_lines,
        "gameplay_lines": acc.gameplay_lines,
        "category_counts": acc.category_counts,
        "round_id": acc.round_id,
        "started_at": acc.started_at,
        "ended_at": acc.ended_at,
    }


def _has_dangerous_regex(pattern: str) -> bool:
    """Reject nested/excessive unbounded quantifiers that can cause catastrophic backtracking."""
    n = len(pattern)
    i = 0
    in_char_class = False
    char_class_just_opened = False
    group_stack: List[bool] = []
    unbounded_count = 0

    def _quantifier_at(pos: int) -> Tuple[bool, bool, bool, int]:
        """Return (is_quantifier, repeats_gt1, is_unbounded, next_pos)."""
        if pos >= n:
            return False, False, False, pos
        ch = pattern[pos]
        if ch in ("*", "+"):
            nxt = pos + 1
            if nxt < n and pattern[nxt] in ("?", "+"):
                nxt += 1
            return True, True, True, nxt
        if ch == "?":
            nxt = pos + 1
            if nxt < n and pattern[nxt] in ("?", "+"):
                nxt += 1
            return True, False, False, nxt
        if ch == "{":
            m_q = _QUANT_BRACE_RE.match(pattern[pos:])
            if m_q:
                lo = int(m_q.group(1))
                hi_str = m_q.group(2)
                nxt = pos + m_q.end()
                if nxt < n and pattern[nxt] in ("?", "+"):
                    nxt += 1
                is_unbounded = hi_str == ""
                # Exact {k} (hi_str is None) has no repetition-count backtracking; range {m,n} or {m,} does
                has_variable_repeat = is_unbounded or (hi_str is not None and int(hi_str) > lo and int(hi_str) > 1)
                return True, has_variable_repeat, is_unbounded, nxt
        return False, False, False, pos

    while i < n:
        ch = pattern[i]
        if ch == "\\":
            if i + 1 < n and "1" <= pattern[i + 1] <= "9" and not in_char_class and group_stack:
                group_stack[-1] = True
            i += 2
            char_class_just_opened = False
            continue
        if in_char_class:
            if ch == "^" and char_class_just_opened:
                i += 1
                continue
            if ch == "]" and not char_class_just_opened:
                in_char_class = False
            char_class_just_opened = False
            i += 1
            continue
        if ch == "[":
            in_char_class = True
            char_class_just_opened = True
            i += 1
            continue
        if ch == "(":
            if pattern.startswith("(?#", i):
                end_c = pattern.find(")", i + 3)
                if end_c == -1:
                    return False
                i = end_c + 1
                continue
            group_stack.append(False)
            i += 1
            continue
        if ch == ")":
            inner_has_quant = group_stack.pop() if group_stack else False
            is_q, repeats_gt1, is_unbounded, nxt = _quantifier_at(i + 1)
            if is_q and repeats_gt1 and inner_has_quant:
                return True
            if is_q and repeats_gt1 and group_stack:
                group_stack[-1] = True
            if is_q:
                if is_unbounded:
                    unbounded_count += 1
                    if unbounded_count > 8:
                        return True
                i = nxt
            else:
                i += 1
            continue

        is_q, repeats_gt1, is_unbounded, nxt = _quantifier_at(i)
        if is_q:
            if is_unbounded:
                unbounded_count += 1
                if unbounded_count > 8:
                    return True
            if repeats_gt1 and group_stack:
                group_stack[-1] = True
            i = nxt
            continue

        i += 1

    return False


def _compile_search_pattern(q: str, use_regex: bool) -> Tuple[Optional[Pattern[str]], Optional[str]]:
    needle = (q or "").strip()
    if not needle:
        return None, None
    if not use_regex:
        return None, None
    if len(needle) > 256:
        return None, "Регулярное выражение слишком длинное (максимум 256 символов)."
    if _has_dangerous_regex(needle):
        return None, "Регулярное выражение слишком сложное (вложенные или множественные квантификаторы запрещены)."
    try:
        return re.compile(needle, re.IGNORECASE), None
    except re.error as exc:
        return None, f"Ошибка в регулярном выражении: {exc}"


def _time_hhmm_from_ts(ts: str) -> str:
    if len(ts) >= 16:
        return ts[11:16]
    return ""


def _parse_time_seconds(raw_val: str, *, is_end: bool) -> Tuple[Optional[int], bool]:
    """Parse HH:MM[:SS] into seconds since midnight. Returns (seconds_or_None, is_invalid)."""
    val = (raw_val or "").strip()
    if not val:
        return None, False
    m = _TIME_BOUND_RE.match(val)
    if not m:
        return None, True
    hh = int(m.group(1))
    mm = int(m.group(2))
    ss = int(m.group(3)) if m.group(3) is not None else (59 if is_end else 0)
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None, True
    return hh * 3600 + mm * 60 + ss, False


def _line_time_seconds(ts: str) -> Optional[int]:
    if len(ts) < 19:
        return None
    try:
        hh = int(ts[11:13])
        mm = int(ts[14:16])
        ss = int(ts[17:19])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        return None
    return hh * 3600 + mm * 60 + ss


def _time_matches_range(ts: str, from_sec: Optional[int], to_sec: Optional[int], time_invalid: bool) -> bool:
    if time_invalid:
        return False
    if from_sec is None and to_sec is None:
        return True
    line_sec = _line_time_seconds(ts)
    if line_sec is None:
        return False
    if from_sec is not None and to_sec is not None and from_sec > to_sec:
        # Cross-midnight range, e.g. 23:30 .. 01:15
        return line_sec >= from_sec or line_sec <= to_sec
    if from_sec is not None and line_sec < from_sec:
        return False
    if to_sec is not None and line_sec > to_sec:
        return False
    return True


def _line_epoch_seconds(ts: str) -> Optional[float]:
    if len(ts) < 19:
        return None
    try:
        return datetime.fromisoformat(ts[:19]).timestamp()
    except ValueError:
        return None


def _collect_ckey_coord_anchors(
    file_path: Path,
    ckey_norm: str,
) -> List[Tuple[Optional[float], int, int, int, int]]:
    """Collect chronological (epoch_sec, line_no, x, y, z) anchors for lines mentioning ckey_norm."""
    if not ckey_norm:
        return []
    anchors: List[Tuple[Optional[float], int, int, int, int]] = []
    for line_no, ts, _cat, raw_msg, _is_cont in iter_parsed_file_lines(file_path):
        coords = extract_coords_from_text(raw_msg)
        if not coords:
            continue
        _, plain_msg = sanitize_byond_html(raw_msg)
        if ckey_norm not in extract_ckeys_from_text(plain_msg) and ckey_norm not in _extract_word_ckey_tokens(plain_msg):
            continue
        x, y, z = coords[0]
        anchors.append((_line_epoch_seconds(ts), line_no, x, y, z))
    return anchors


def _nearest_anchor_for_line(
    anchors: Sequence[Tuple[Optional[float], int, int, int, int]],
    line_no: int,
    line_epoch: Optional[float],
    hint_idx: int,
) -> Tuple[Optional[Tuple[int, int, int]], int]:
    """Return ((x, y, z), updated_hint_idx) for the anchor nearest in time (or line_no) to this line."""
    n = len(anchors)
    if n == 0:
        return None, 0
    idx = max(0, min(hint_idx, n - 1))

    def _dist(i: int) -> Tuple[float, int]:
        a_epoch, a_ln, _, _, _ = anchors[i]
        if line_epoch is not None and a_epoch is not None:
            return (abs(a_epoch - line_epoch), abs(a_ln - line_no))
        return (float(abs(a_ln - line_no)), abs(a_ln - line_no))

    while idx + 1 < n and _dist(idx + 1) <= _dist(idx):
        idx += 1
    _, _, ax, ay, az = anchors[idx]
    return (ax, ay, az), idx


def _prepare_log_filter(
    *,
    preset: str,
    categories: Optional[Sequence[str]],
    ckey: str,
    q: str,
    use_regex: bool,
    time_from: str,
    time_to: str,
    radius: int = 0,
) -> _PreparedLogFilter:
    pattern, regex_error = _compile_search_pattern(q, use_regex)
    from_sec, from_inv = _parse_time_seconds(time_from, is_end=False)
    to_sec, to_inv = _parse_time_seconds(time_to, is_end=True)
    clamped_radius = max(0, min(int(radius or 0), 100))
    return _PreparedLogFilter(
        preset=preset,
        categories=categories,
        ckey_norm=normalize_ckey(ckey) if ckey else "",
        q_lower=(q or "").strip().lower(),
        pattern=pattern,
        regex_error=regex_error,
        from_sec=from_sec,
        to_sec=to_sec,
        time_invalid=from_inv or to_inv,
        radius=clamped_radius,
    )


def _build_entry(
    line_no: int,
    ts: str,
    cat: str,
    raw_msg: str,
    is_cont: bool,
    *,
    mask_ips: bool,
    render_html: bool,
    pre_display_raw: Optional[str] = None,
    pre_sanitized: Optional[Tuple[str, str]] = None,
) -> ParsedLogLine:
    display_raw = (
        pre_display_raw
        if pre_display_raw is not None
        else (mask_sensitive_text(raw_msg) if mask_ips else raw_msg)
    )
    if pre_sanitized is not None:
        safe_html, plain_text = pre_sanitized
    else:
        safe_html, plain_text = sanitize_byond_html(display_raw)
    if not render_html:
        safe_html = html_escape(display_raw)
    ckeys = extract_ckeys_from_text(plain_text)
    all_coords = extract_coords_from_text(raw_msg)
    return ParsedLogLine(
        line_no=line_no,
        timestamp=ts,
        category=cat,
        raw_message=display_raw,
        plain_message=plain_text,
        safe_html=safe_html,
        ckeys=ckeys,
        is_continuation=is_cont,
        coords=all_coords[0] if all_coords else None,
        all_coords=all_coords,
    )


def _evaluate_line_filter(
    ts: str,
    cat: str,
    raw_msg: str,
    filt: _PreparedLogFilter,
    *,
    mask_ips: bool = False,
    line_no: int = 0,
    anchors: Sequence[Tuple[Optional[float], int, int, int, int]] = (),
    anchor_hint: int = 0,
) -> Tuple[bool, Optional[str], Optional[Tuple[str, str]], int]:
    if not category_matches_filters(cat, preset=filt.preset, selected_categories=filt.categories):
        return False, None, None, anchor_hint
    if not _time_matches_range(ts, filt.from_sec, filt.to_sec, filt.time_invalid):
        return False, None, None, anchor_hint

    if not filt.ckey_norm and not filt.q_lower and filt.pattern is None:
        return True, None, None, anchor_hint

    display_raw = mask_sensitive_text(raw_msg) if mask_ips else raw_msg
    sanitized = sanitize_byond_html(display_raw)
    plain_text = sanitized[1]

    if filt.ckey_norm:
        line_ckeys = extract_ckeys_from_text(plain_text)
        is_own_ckey = (
            filt.ckey_norm in line_ckeys
            or filt.ckey_norm in _extract_word_ckey_tokens(plain_text)
        )
        if not is_own_ckey:
            if filt.radius <= 0 or not anchors:
                return False, None, None, anchor_hint
            line_coords = extract_coords_from_text(raw_msg)
            if not line_coords:
                return False, None, None, anchor_hint
            anchor_xyz, anchor_hint = _nearest_anchor_for_line(
                anchors, line_no, _line_epoch_seconds(ts), anchor_hint
            )
            if anchor_xyz is None:
                return False, None, None, anchor_hint
            ax, ay, az = anchor_xyz
            if not any(
                lz == az and max(abs(lx - ax), abs(ly - ay)) <= filt.radius
                for lx, ly, lz in line_coords
            ):
                return False, None, None, anchor_hint

    if filt.q_lower:
        if filt.pattern is not None:
            if not filt.pattern.search(plain_text):
                return False, None, None, anchor_hint
        elif filt.q_lower not in plain_text.lower():
            return False, None, None, anchor_hint

    return True, display_raw, sanitized, anchor_hint


def _line_matches_filters(
    ts: str,
    cat: str,
    raw_msg: str,
    *,
    preset: str,
    categories: Optional[Sequence[str]],
    ckey_norm: str,
    q_lower: str,
    pattern: Optional[Pattern[str]],
    t_from: str,
    t_to: str,
) -> bool:
    from_sec, from_inv = _parse_time_seconds(t_from, is_end=False)
    to_sec, to_inv = _parse_time_seconds(t_to, is_end=True)
    filt = _PreparedLogFilter(
        preset=preset,
        categories=categories,
        ckey_norm=ckey_norm,
        q_lower=q_lower,
        pattern=pattern,
        regex_error=None,
        from_sec=from_sec,
        to_sec=to_sec,
        time_invalid=from_inv or to_inv,
    )
    matched, _, _, _ = _evaluate_line_filter(ts, cat, raw_msg, filt)
    return matched


def filter_game_log_file(
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
    page: int = 1,
    per_page: int = 200,
    focus_line: Optional[int] = None,
    mask_ips: bool = False,
    render_html: bool = True,
) -> LogPageResult:
    """Filter and paginate a game.log file directly from disk."""
    per_page = max(25, min(int(per_page), 500))
    page = max(1, int(page))

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
    anchors = (
        _collect_ckey_coord_anchors(file_path, filt.ckey_norm)
        if (filt.ckey_norm and filt.radius > 0 and not filt.regex_error)
        else []
    )
    anchor_hint = 0

    acc = _RoundMetaAccumulator()
    matched_raw: List[Tuple[int, str, str, str, bool, Optional[str], Optional[Tuple[str, str]]]] = []

    for line_no, ts, cat, raw_msg, is_cont in iter_parsed_file_lines(file_path):
        acc.observe(ts, cat, raw_msg, is_cont)

        if filt.regex_error:
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

        matched_raw.append((line_no, ts, cat, raw_msg, is_cont, pre_display_raw, pre_sanitized))

    total_matches = len(matched_raw)
    total_pages = max(1, (total_matches + per_page - 1) // per_page)

    if focus_line is not None and focus_line > 0 and matched_raw:
        for idx, item in enumerate(matched_raw):
            if item[0] == focus_line:
                page = (idx // per_page) + 1
                break

    page = min(page, total_pages)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    page_entries = [
        _build_entry(
            ln,
            ts,
            cat,
            msg,
            is_cont,
            mask_ips=mask_ips,
            render_html=render_html,
            pre_display_raw=pre_disp,
            pre_sanitized=pre_san,
        )
        for ln, ts, cat, msg, is_cont, pre_disp, pre_san in matched_raw[start_idx:end_idx]
    ]

    return LogPageResult(
        entries=page_entries,
        total_matches=total_matches,
        total_lines=acc.total_lines,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        category_counts=acc.category_counts,
        round_id=acc.round_id,
        started_at=acc.started_at,
        ended_at=acc.ended_at,
        regex_error=filt.regex_error,
    )


def export_filtered_game_log(
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
) -> str:
    """Render filtered game.log lines back into plain text for download."""
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
    if filt.regex_error:
        raise ValueError(filt.regex_error)

    anchors = (
        _collect_ckey_coord_anchors(file_path, filt.ckey_norm)
        if (filt.ckey_norm and filt.radius > 0)
        else []
    )
    anchor_hint = 0

    out_lines: List[str] = []
    for line_no, ts, cat, raw_msg, is_cont in iter_parsed_file_lines(file_path):
        matched, pre_display_raw, _, anchor_hint = _evaluate_line_filter(
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

        display_msg = (
            pre_display_raw
            if pre_display_raw is not None
            else (mask_sensitive_text(raw_msg) if mask_ips else raw_msg)
        )
        if is_cont:
            out_lines.append(display_msg)
        elif cat == "SYSTEM":
            out_lines.append(f"[{ts}] {display_msg}" if ts else display_msg)
        else:
            out_lines.append(f"[{ts}] {cat}: {display_msg}" if ts else f"{cat}: {display_msg}")

    return "\n".join(out_lines) + ("\n" if out_lines else "")


def read_context_lines(
    file_path: Path,
    target_line: int,
    *,
    radius: int = 20,
    mask_ips: bool = False,
    render_html: bool = True,
) -> List[ParsedLogLine]:
    """Read [target_line - radius .. target_line + radius] lines regardless of category filter."""
    target_line = max(1, int(target_line))
    radius = max(1, min(int(radius), 100))
    lo = max(1, target_line - radius)
    hi = target_line + radius
    entries: List[ParsedLogLine] = []
    for line_no, ts, cat, msg, is_cont in iter_parsed_file_lines(file_path):
        if line_no < lo:
            continue
        if line_no > hi:
            break
        entries.append(
            _build_entry(line_no, ts, cat, msg, is_cont, mask_ips=mask_ips, render_html=render_html)
        )
    return entries
