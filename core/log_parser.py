"""SS13 SierraBay12 log parser, safe BYOND HTML sanitizer, ckey extractor, and qdel analyzer."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape as html_escape
from html.parser import HTMLParser
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Iterator, List, Optional, Pattern, Sequence, Set, Tuple

from core.byond_topic import normalize_ckey
from core.text_encoding import decode_byond_text

# Timestamp + optional category prefix in SierraBay12 game.log
_TS_LINE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\s*(.*)$")
_CAT_PREFIX_RE = re.compile(r"^([A-Z][A-Z0-9_]{1,24}):\s*(.*)$")
_ROUND_START_ID_RE = re.compile(r"Starting up\.\s*\(ID:\s*([A-Za-z0-9_-]+)\)")

# Ckey extraction patterns:
# 1) Start-of-message speaker/subject (allows spaces in legacy BYOND keys): "John Doe/(Name)" or "Login: John Doe/(Name)"
_CKEY_START_RE = re.compile(
    r"^(?:Login:\s*|Logout:\s*)?([A-Za-z0-9][A-Za-z0-9 _.-]{0,30}?)(?:\s*\(DC\))?/\(([^)\r\n]+)\)"
)
# 2) Any inline BYOND key/(Name) token: "AwkwardlyConfusedNeuralNetwork/(Angel Murray)"
_CKEY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9][A-Za-z0-9_.-]{1,31})(?:\s*\(DC\))?/\(([^)\r\n]+)\)"
)

# IP / BYOND CID masking patterns for screenshot privacy mode
_IP_CID_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})-(\d{6,14})\b")
_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")

# qdel.log patterns (first Path has timestamp; subsequent Path lines start with " - Path:")
_QDEL_PATH_RE = re.compile(
    r"^(?:\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]\s*|\s*-\s+)?Path:\s*(\S+)\s*$"
)
_QDEL_COUNT_RE = re.compile(r"^\s*-\s*qdel\(\)\s*Count:\s*(\d+)\s*$", re.IGNORECASE)
_QDEL_UNNECESSARY_RE = re.compile(r"^\s*-\s*un+ecessary\s+qdel\(\)\s*Count:\s*(\d+)\s*$", re.IGNORECASE)
_QDEL_COST_RE = re.compile(r"^\s*-\s*Destroy\(\)\s*Cost:\s*([0-9]+(?:\.[0-9]+)?)\s*ms\s*$", re.IGNORECASE)

# Category display metadata & priority order
CATEGORY_META: Dict[str, Dict[str, str]] = {
    "ATTACK": {"label": "ATTACK", "css": "cat-attack", "desc": "Бой и урон"},
    "ADMIN": {"label": "ADMIN", "css": "cat-admin", "desc": "Действия админов"},
    "ADMINSAY": {"label": "ADMINSAY", "css": "cat-adminsay", "desc": "Чат админов"},
    "ADMINWARN": {"label": "ADMINWARN", "css": "cat-adminwarn", "desc": "Предупреждения"},
    "ACCESS": {"label": "ACCESS", "css": "cat-access", "desc": "Входы / выходы"},
    "SAY": {"label": "SAY", "css": "cat-say", "desc": "IC речь"},
    "WHISPER": {"label": "WHISPER", "css": "cat-whisper", "desc": "Шёпот"},
    "OOC": {"label": "OOC", "css": "cat-ooc", "desc": "OOC чат"},
    "EMOTE": {"label": "EMOTE", "css": "cat-emote", "desc": "Эмоции"},
    "GAME": {"label": "GAME", "css": "cat-game", "desc": "Игровые события"},
    "VOTE": {"label": "VOTE", "css": "cat-vote", "desc": "Голосования"},
    "COMPUTER_COMMAND": {"label": "COMPUTER_COMMAND", "css": "cat-computer", "desc": "Компьютеры"},
    "SYSTEM": {"label": "SYSTEM", "css": "cat-system", "desc": "Запуск / система"},
    "MISC": {"label": "MISC", "css": "cat-misc", "desc": "Прочее"},
    "SS": {"label": "SS", "css": "cat-tech", "desc": "Подсистемы"},
    "ASGEN": {"label": "ASGEN", "css": "cat-tech", "desc": "Генерация астероидов"},
    "SIGNALS": {"label": "SIGNALS", "css": "cat-tech", "desc": "Сигналы"},
    "DD_OUTPUT": {"label": "DD_OUTPUT", "css": "cat-tech", "desc": "Вывод DreamDaemon"},
    "DEBUG": {"label": "DEBUG", "css": "cat-debug", "desc": "Отладка"},
}

TECHNICAL_NOISE_CATEGORIES: Set[str] = {"DEBUG", "SS", "ASGEN", "SIGNALS", "DD_OUTPUT"}

LOG_PRESETS: Dict[str, Dict[str, Any]] = {
    "gameplay": {
        "id": "gameplay",
        "label": "Игровые события",
        "description": "Без DEBUG / SS / ASGEN / SIGNALS",
        "exclude": set(TECHNICAL_NOISE_CATEGORIES),
        "include": None,
    },
    "admin_combat": {
        "id": "admin_combat",
        "label": "Админ / Атаки",
        "description": "ADMIN, ADMINSAY, ADMINWARN, ATTACK, ACCESS",
        "include": {"ADMIN", "ADMINSAY", "ADMINWARN", "ATTACK", "ACCESS"},
        "exclude": None,
    },
    "chat": {
        "id": "chat",
        "label": "Чат",
        "description": "SAY, WHISPER, OOC, EMOTE, ADMINSAY",
        "include": {"SAY", "WHISPER", "OOC", "EMOTE", "ADMINSAY"},
        "exclude": None,
    },
    "all": {
        "id": "all",
        "label": "Все категории",
        "description": "Включая DEBUG и подсистемы",
        "include": None,
        "exclude": None,
    },
}

_COLOR_CLASS_MAP: Dict[str, str] = {
    "orange": "log-c-orange",
    "#ffa500": "log-c-orange",
    "red": "log-c-red",
    "#ff0000": "log-c-red",
    "#bb0202": "log-c-red",
    "#e53935": "log-c-red",
    "crimson": "log-c-red",
    "green": "log-c-green",
    "#00ff00": "log-c-green",
    "#008000": "log-c-green",
    "#00b747": "log-c-green",
    "lime": "log-c-green",
    "blue": "log-c-blue",
    "#0000ff": "log-c-blue",
    "#2a63f4": "log-c-blue",
    "cyan": "log-c-cyan",
    "#00ffff": "log-c-cyan",
    "#00b5b5": "log-c-cyan",
    "teal": "log-c-cyan",
    "yellow": "log-c-amber",
    "gold": "log-c-amber",
    "#ffff00": "log-c-amber",
    "#deb700": "log-c-amber",
    "purple": "log-c-purple",
    "violet": "log-c-purple",
    "magenta": "log-c-purple",
    "gray": "log-c-gray",
    "grey": "log-c-gray",
}

_HEX_COLOR_RE = re.compile(r"^#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
_STYLE_COLOR_RE = re.compile(r"(?:^|;)\s*color\s*:\s*([^;]+)", re.IGNORECASE)
_LOC_COORDS_RE = re.compile(r"(?:^|[;?&\s])X=(-?\d+)\s*[;&]\s*Y=(-?\d+)\s*[;&]\s*Z=(-?\d+)", re.IGNORECASE)


def _bucket_color_class(raw_color: str) -> Optional[str]:
    color = (raw_color or "").strip().lower()
    if not color:
        return None
    if color in _COLOR_CLASS_MAP:
        return _COLOR_CLASS_MAP[color]
    m = _HEX_COLOR_RE.match(color)
    if not m:
        return None
    hex_val = m.group(1)
    if len(hex_val) == 3:
        hex_val = "".join(ch * 2 for ch in hex_val)
    r, g, b = int(hex_val[0:2], 16), int(hex_val[2:4], 16), int(hex_val[4:6], 16)
    if r > 200 and 100 <= g <= 190 and b < 80:
        return "log-c-orange"
    if r > 170 and g < 90 and b < 90:
        return "log-c-red"
    if g > 150 and r < 110 and b < 110:
        return "log-c-green"
    if r > 180 and g > 160 and b < 100:
        return "log-c-amber"
    if g > 140 and b > 140 and r < 100:
        return "log-c-cyan"
    if b > 160 and r < 110:
        return "log-c-blue"
    if r > 130 and b > 130 and g < 120:
        return "log-c-purple"
    return None


class _ByondSanitizer(HTMLParser):
    """CSP-safe sanitizer for inline BYOND HTML in SS13 logs (zero inline styles, zero script/hrefs)."""

    _SIMPLE_ALLOWED = {"b": "strong", "strong": "strong", "i": "em", "em": "em", "u": "u"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_parts: List[str] = []
        self.plain_parts: List[str] = []
        self._tag_stack: List[str] = []
        self._in_byond_link: Optional[str] = None
        self._link_href: str = ""
        self._link_text: List[str] = []

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if t == "a":
            href = attr_map.get("href", "").strip()
            if href.lower().startswith("byond://"):
                self._in_byond_link = "byond"
                self._link_href = href
                self._link_text = []
            else:
                self._in_byond_link = "other"
                self._link_href = ""
                self._link_text = []
            return

        if t in self._SIMPLE_ALLOWED:
            mapped = self._SIMPLE_ALLOWED[t]
            self.html_parts.append(f"<{mapped}>")
            self._tag_stack.append(mapped)
            return

        if t in ("span", "font"):
            raw_color = attr_map.get("color", "")
            if not raw_color and "style" in attr_map:
                m = _STYLE_COLOR_RE.search(attr_map["style"])
                if m:
                    raw_color = m.group(1)
            cls = _bucket_color_class(raw_color)
            if cls:
                self.html_parts.append(f'<span class="{cls}">')
                self._tag_stack.append("span")
            else:
                self._tag_stack.append("")
            return

        # Unknown / dangerous tag -> render safely escaped as literal text so admins see raw chat payloads
        raw = self.get_starttag_text() or f"<{tag}>"
        escaped = html_escape(raw)
        self.html_parts.append(escaped)
        self.plain_parts.append(raw)

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "a" and self._in_byond_link is not None:
            link_label = "".join(self._link_text).strip()
            href = self._link_href
            mode = self._in_byond_link
            self._in_byond_link = None
            self._link_href = ""
            self._link_text = []

            if mode == "byond":
                coords = _LOC_COORDS_RE.search(href)
                if coords and link_label.upper() == "LOC":
                    badge = f"[{coords.group(1)}, {coords.group(2)}, {coords.group(3)}]"
                    self.html_parts.append(f'<span class="log-loc-badge">{html_escape(badge)}</span>')
                    self.plain_parts.append(badge)
                    return
                if link_label.upper() == "MOB":
                    self.html_parts.append('<span class="log-mob-badge">[MOB]</span>')
                    self.plain_parts.append("[MOB]")
                    return
            escaped_label = html_escape(link_label)
            self.html_parts.append(escaped_label)
            self.plain_parts.append(link_label)
            return

        if t in self._SIMPLE_ALLOWED:
            mapped = self._SIMPLE_ALLOWED[t]
            if mapped in self._tag_stack:
                while self._tag_stack:
                    top = self._tag_stack.pop()
                    if top:
                        self.html_parts.append(f"</{top}>")
                    if top == mapped:
                        break
            return

        if t in ("span", "font"):
            if self._tag_stack:
                top = self._tag_stack.pop()
                if top:
                    self.html_parts.append(f"</{top}>")
            return

        raw_end = f"</{tag}>"
        self.html_parts.append(html_escape(raw_end))
        self.plain_parts.append(raw_end)

    def handle_data(self, data: str) -> None:
        if self._in_byond_link is not None:
            self._link_text.append(data)
            return
        self.html_parts.append(html_escape(data))
        self.plain_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._in_byond_link is not None:
            leftover = "".join(self._link_text)
            self.html_parts.append(html_escape(leftover))
            self.plain_parts.append(leftover)
            self._in_byond_link = None
        while self._tag_stack:
            top = self._tag_stack.pop()
            if top:
                self.html_parts.append(f"</{top}>")


def sanitize_byond_html(raw_text: str) -> Tuple[str, str]:
    """Sanitize BYOND log text into (safe_html, plain_text) with zero XSS and zero inline styles."""
    if "<" not in raw_text and "&" not in raw_text:
        escaped = html_escape(raw_text)
        return escaped, raw_text
    parser = _ByondSanitizer()
    parser.feed(raw_text)
    parser.close()
    return "".join(parser.html_parts), "".join(parser.plain_parts)


def mask_sensitive_text(text: str) -> str:
    """Mask IPv4 addresses and BYOND computer IDs (CIDs) for safe screenshot sharing."""
    masked = _IP_CID_RE.sub("[IP-СКРЫТ]-[CID-СКРЫТ]", text)
    masked = _IPV4_RE.sub("[IP-СКРЫТ]", masked)
    return masked


def extract_ckeys_from_text(plain_text: str) -> List[str]:
    """Extract ordered unique normalized BYOND ckeys from a plain-text log message."""
    seen: Set[str] = set()
    found: List[str] = []
    search_pos = 0

    m_start = _CKEY_START_RE.match(plain_text)
    if m_start:
        norm = normalize_ckey(m_start.group(1))
        if norm and norm not in seen:
            seen.add(norm)
            found.append(norm)
        search_pos = m_start.end()

    for m in _CKEY_TOKEN_RE.finditer(plain_text, pos=search_pos):
        norm = normalize_ckey(m.group(1))
        if norm and norm not in seen:
            seen.add(norm)
            found.append(norm)

    return found


def decode_log_bytes(data: bytes) -> str:
    """Decode raw log bytes preferring UTF-8 with CP1251/CP1252/latin-1 fallback."""
    return decode_byond_text(data)


def category_info(category: str) -> Dict[str, str]:
    meta = CATEGORY_META.get(category)
    if meta:
        return meta
    return {"label": category, "css": "cat-misc", "desc": category}


def category_matches_filters(
    category: str,
    preset: str = "gameplay",
    selected_categories: Optional[Sequence[str]] = None,
) -> bool:
    if selected_categories:
        allowed = {c.strip().upper() for c in selected_categories if c and c.strip()}
        if allowed:
            return category in allowed

    preset_cfg = LOG_PRESETS.get(preset, LOG_PRESETS["gameplay"])
    include = preset_cfg.get("include")
    exclude = preset_cfg.get("exclude")
    if include is not None:
        return category in include
    if exclude is not None:
        return category not in exclude
    return True


@dataclass
class ParsedLogLine:
    line_no: int
    timestamp: str
    category: str
    raw_message: str
    plain_message: str
    safe_html: str
    ckeys: List[str] = field(default_factory=list)
    is_continuation: bool = False


@dataclass
class LogPageResult:
    entries: List[ParsedLogLine]
    total_matches: int
    total_lines: int
    page: int
    per_page: int
    total_pages: int
    category_counts: Dict[str, int]
    round_id: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    regex_error: Optional[str] = None


@dataclass
class QdelEntry:
    line_no: int
    timestamp: str
    path: str
    qdel_count: int
    unnecessary_count: int
    destroy_cost_ms: float


@dataclass
class QdelReport:
    entries: List[QdelEntry]
    total_paths: int
    total_qdels: int
    total_unnecessary: int
    total_destroy_ms: float
    raw_text: str


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


def iter_parsed_file_lines(file_path: Path) -> Iterator[Tuple[int, str, str, str, bool]]:
    """Stream physical lines from a log file yielding (line_no, timestamp, category, raw_message, is_continuation)."""
    raw_bytes = file_path.read_bytes()
    text = decode_log_bytes(raw_bytes)
    prev_ts = ""
    prev_cat = "SYSTEM"
    for line_no, raw_line in enumerate(text.splitlines(), 1):
        ts, cat, msg, is_cont = parse_raw_line(line_no, raw_line, prev_ts, prev_cat)
        if not is_cont:
            prev_ts = ts
            prev_cat = cat
        yield line_no, ts, cat, msg, is_cont


def inspect_game_log_summary(file_path: Path) -> Dict[str, Any]:
    """Scan a game.log file once to compute line counts, category histogram, timestamps, and round_id."""
    category_counts: Dict[str, int] = {}
    total_lines = 0
    gameplay_lines = 0
    round_id: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None

    for _line_no, ts, cat, msg, is_cont in iter_parsed_file_lines(file_path):
        total_lines += 1
        category_counts[cat] = category_counts.get(cat, 0) + 1
        if cat not in TECHNICAL_NOISE_CATEGORIES:
            gameplay_lines += 1
        if ts:
            if started_at is None:
                started_at = ts
            ended_at = ts
        if round_id is None and not is_cont and cat == "SYSTEM":
            m_rid = _ROUND_START_ID_RE.search(msg)
            if m_rid:
                round_id = m_rid.group(1)

    return {
        "total_lines": total_lines,
        "gameplay_lines": gameplay_lines,
        "category_counts": category_counts,
        "round_id": round_id,
        "started_at": started_at,
        "ended_at": ended_at,
    }


def _compile_search_pattern(q: str, use_regex: bool) -> Tuple[Optional[Pattern[str]], Optional[str]]:
    needle = (q or "").strip()
    if not needle:
        return None, None
    if not use_regex:
        return None, None
    if len(needle) > 256:
        return None, "Регулярное выражение слишком длинное (максимум 256 символов)."
    try:
        return re.compile(needle, re.IGNORECASE), None
    except re.error as exc:
        return None, f"Ошибка в регулярном выражении: {exc}"


def _time_hhmm_from_ts(ts: str) -> str:
    if len(ts) >= 16:
        return ts[11:16]
    return ""


def _build_entry(
    line_no: int,
    ts: str,
    cat: str,
    raw_msg: str,
    is_cont: bool,
    *,
    mask_ips: bool,
    render_html: bool,
) -> ParsedLogLine:
    display_raw = mask_sensitive_text(raw_msg) if mask_ips else raw_msg
    safe_html, plain_text = sanitize_byond_html(display_raw)
    if not render_html:
        safe_html = html_escape(display_raw)
    ckeys = extract_ckeys_from_text(plain_text)
    return ParsedLogLine(
        line_no=line_no,
        timestamp=ts,
        category=cat,
        raw_message=display_raw,
        plain_message=plain_text,
        safe_html=safe_html,
        ckeys=ckeys,
        is_continuation=is_cont,
    )


def filter_game_log_file(
    file_path: Path,
    *,
    preset: str = "gameplay",
    categories: Optional[Sequence[str]] = None,
    ckey: str = "",
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

    pattern, regex_error = _compile_search_pattern(q, use_regex)
    q_lower = (q or "").strip().lower()
    ckey_norm = normalize_ckey(ckey) if ckey else ""
    t_from = (time_from or "").strip()
    t_to = (time_to or "").strip()

    category_counts: Dict[str, int] = {}
    matched_raw: List[Tuple[int, str, str, str, bool]] = []
    total_lines = 0
    round_id: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None

    for line_no, ts, cat, raw_msg, is_cont in iter_parsed_file_lines(file_path):
        total_lines += 1
        category_counts[cat] = category_counts.get(cat, 0) + 1
        if ts:
            if started_at is None:
                started_at = ts
            ended_at = ts
        if round_id is None and not is_cont and cat == "SYSTEM":
            m_rid = _ROUND_START_ID_RE.search(raw_msg)
            if m_rid:
                round_id = m_rid.group(1)

        if regex_error:
            continue
        if not category_matches_filters(cat, preset=preset, selected_categories=categories):
            continue
        if t_from or t_to:
            hhmm = _time_hhmm_from_ts(ts)
            if not hhmm:
                continue
            if t_from and hhmm < t_from:
                continue
            if t_to and hhmm > t_to:
                continue

        if ckey_norm:
            _, plain_for_ckey = sanitize_byond_html(raw_msg)
            line_ckeys = extract_ckeys_from_text(plain_for_ckey)
            if ckey_norm not in line_ckeys and ckey_norm not in normalize_ckey(plain_for_ckey):
                continue

        if q_lower:
            if pattern is not None:
                if not pattern.search(raw_msg):
                    continue
            else:
                if q_lower not in raw_msg.lower():
                    continue

        matched_raw.append((line_no, ts, cat, raw_msg, is_cont))

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
        _build_entry(ln, ts, cat, msg, is_cont, mask_ips=mask_ips, render_html=render_html)
        for ln, ts, cat, msg, is_cont in matched_raw[start_idx:end_idx]
    ]

    return LogPageResult(
        entries=page_entries,
        total_matches=total_matches,
        total_lines=total_lines,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        category_counts=category_counts,
        round_id=round_id,
        started_at=started_at,
        ended_at=ended_at,
        regex_error=regex_error,
    )


def export_filtered_game_log(
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
) -> str:
    """Render filtered game.log lines back into plain text for download."""
    pattern, regex_error = _compile_search_pattern(q, use_regex)
    if regex_error:
        raise ValueError(regex_error)

    q_lower = (q or "").strip().lower()
    ckey_norm = normalize_ckey(ckey) if ckey else ""
    t_from = (time_from or "").strip()
    t_to = (time_to or "").strip()

    out_lines: List[str] = []
    for _line_no, ts, cat, raw_msg, is_cont in iter_parsed_file_lines(file_path):
        if not category_matches_filters(cat, preset=preset, selected_categories=categories):
            continue
        if t_from or t_to:
            hhmm = _time_hhmm_from_ts(ts)
            if not hhmm:
                continue
            if t_from and hhmm < t_from:
                continue
            if t_to and hhmm > t_to:
                continue
        if ckey_norm:
            _, plain_for_ckey = sanitize_byond_html(raw_msg)
            line_ckeys = extract_ckeys_from_text(plain_for_ckey)
            if ckey_norm not in line_ckeys and ckey_norm not in normalize_ckey(plain_for_ckey):
                continue
        if q_lower:
            if pattern is not None:
                if not pattern.search(raw_msg):
                    continue
            elif q_lower not in raw_msg.lower():
                continue

        display_msg = mask_sensitive_text(raw_msg) if mask_ips else raw_msg
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


def parse_qdel_log_file(
    file_path: Path,
    *,
    q: str = "",
    sort_by: str = "destroy_cost_ms",
    sort_dir: str = "desc",
) -> QdelReport:
    """Parse SierraBay12 qdel.log into structured sortable entries and summary KPIs."""
    raw_text = decode_log_bytes(file_path.read_bytes())
    entries: List[QdelEntry] = []
    current: Optional[Dict[str, Any]] = None

    def flush_current() -> None:
        if current is None:
            return
        entries.append(
            QdelEntry(
                line_no=current["line_no"],
                timestamp=current["timestamp"],
                path=current["path"],
                qdel_count=current["qdel_count"],
                unnecessary_count=current["unnecessary_count"],
                destroy_cost_ms=round(current["destroy_cost_ms"], 3),
            )
        )

    for line_no, raw_line in enumerate(raw_text.splitlines(), 1):
        m_path = _QDEL_PATH_RE.match(raw_line)
        if m_path:
            flush_current()
            current = {
                "line_no": line_no,
                "timestamp": m_path.group(1) or "",
                "path": m_path.group(2),
                "qdel_count": 0,
                "unnecessary_count": 0,
                "destroy_cost_ms": 0.0,
            }
            continue
        if current is None:
            continue
        m_cnt = _QDEL_COUNT_RE.match(raw_line)
        if m_cnt:
            current["qdel_count"] = int(m_cnt.group(1))
            continue
        m_unn = _QDEL_UNNECESSARY_RE.match(raw_line)
        if m_unn:
            current["unnecessary_count"] = int(m_unn.group(1))
            continue
        m_cost = _QDEL_COST_RE.match(raw_line)
        if m_cost:
            current["destroy_cost_ms"] = float(m_cost.group(1))
            continue

    flush_current()

    total_paths = len(entries)
    total_qdels = sum(e.qdel_count for e in entries)
    total_unnecessary = sum(e.unnecessary_count for e in entries)
    total_destroy_ms = round(sum(e.destroy_cost_ms for e in entries), 2)

    needle = (q or "").strip().lower()
    if needle:
        entries = [e for e in entries if needle in e.path.lower()]

    reverse = sort_dir.lower() != "asc"
    sort_key_map = {
        "destroy_cost_ms": lambda e: (e.destroy_cost_ms, e.qdel_count),
        "qdel_count": lambda e: (e.qdel_count, e.destroy_cost_ms),
        "unnecessary_count": lambda e: (e.unnecessary_count, e.destroy_cost_ms),
        "path": lambda e: e.path.lower(),
    }
    key_fn = sort_key_map.get(sort_by, sort_key_map["destroy_cost_ms"])
    entries.sort(key=key_fn, reverse=reverse)

    return QdelReport(
        entries=entries,
        total_paths=total_paths,
        total_qdels=total_qdels,
        total_unnecessary=total_unnecessary,
        total_destroy_ms=total_destroy_ms,
        raw_text=raw_text,
    )


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
            if ckey_norm:
                line_ckeys = extract_ckeys_from_text(plain_msg)
                if ckey_norm not in line_ckeys and ckey_norm not in normalize_ckey(plain_msg):
                    continue
            if q_lower and q_lower not in plain_msg.lower():
                continue
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
                    "ckeys": extract_ckeys_from_text(plain_msg),
                }
            )
            if len(hits) >= limit:
                return hits
    return hits
