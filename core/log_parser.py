"""SS13 SierraBay12 log parser, safe BYOND HTML sanitizer, ckey extractor, and qdel analyzer."""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape as html_escape
from html.parser import HTMLParser
from pathlib import Path
import re
from typing import Any, Dict, Iterator, List, Optional, Pattern, Sequence, Set, Tuple

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
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9][A-Za-z0-9_.-]{0,31})(?:\s*\(DC\))?/\(([^)\r\n]+)\)"
)
# 3) SierraBay speech/log prefix "MobName/Ckey : message" (no parentheses around Name)
_CKEY_SLASH_COLON_RE = re.compile(
    r"^(?:\[[^\]\r\n]{1,40}\]\s*)?"
    r"([A-Za-z0-9\u0400-\u04FF][^/\r\n:]{0,60}?)"
    r"/([A-Za-z0-9][A-Za-z0-9 _.-]{0,30}?)"
    r"(?:\s*\(DC\))?\s+:"
)
_WORD_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")

# IP / BYOND CID masking patterns for screenshot privacy mode
_IP_CID_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})-(\d{6,14})\b")
_IPV4_RE = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")

# Coordinate extraction patterns across existing SierraBay12 logs:
# 1) BYOND jump links: X=74;Y=99;Z=1
# 2) Tuple/bracket coords: (142,104,3), (134, 115, 19), [82, 104, 3] (ignore rgb(...) / rgba(...))
# 3) Plain 'at X, Y, Z': at 22, 102, 3 / loaded at 1,1,17
# 4) Ghost hyphen format: - 120-88-2
_COORD_BRACKET_RX = re.compile(r"[\(\[]\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,2})\s*[\)\]]")
_RGB_PREFIX_RE = re.compile(r"\brgba?\s*$", re.IGNORECASE)
_COORD_PATTERNS: Tuple[Pattern[str], ...] = (
    re.compile(r"(?:^|[;?&\s])X=(\d{1,3})\s*[;&]\s*Y=(\d{1,3})\s*[;&]\s*Z=(\d{1,2})\b", re.IGNORECASE),
    _COORD_BRACKET_RX,
    re.compile(r"\bat\s+(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,2})(?!\s*,)\b", re.IGNORECASE),
    re.compile(r"(?:^-\s+|\s-\s)(\d{1,3})-(\d{1,3})-(\d{1,2})(?=\s|$)"),
)

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
        "description": "Без DEBUG / SS / ASGEN / SIGNALS / DD_OUTPUT",
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
_RGB_COLOR_RE = re.compile(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*[0-9.]+\s*)?\)$")
_STYLE_COLOR_RE = re.compile(r"(?:^|;)\s*color\s*:\s*([^;]+)", re.IGNORECASE)
_IMPORTANT_SUFFIX_RE = re.compile(r"\s*!important\b", re.IGNORECASE)
_LOC_COORDS_RE = re.compile(r"(?:^|[;?&\s])X=(-?\d+)\s*[;&]\s*Y=(-?\d+)\s*[;&]\s*Z=(-?\d+)", re.IGNORECASE)


def _classify_rgb_bucket(r: int, g: int, b: int) -> Optional[str]:
    if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
        return None
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
    if max(r, g, b) - min(r, g, b) <= 18 and 64 <= r <= 192:
        return "log-c-gray"
    return None


def _bucket_color_class(raw_color: str) -> Optional[str]:
    color = _IMPORTANT_SUFFIX_RE.sub("", (raw_color or "")).strip().lower()
    if not color:
        return None
    if color in _COLOR_CLASS_MAP:
        return _COLOR_CLASS_MAP[color]
    m_hex = _HEX_COLOR_RE.match(color)
    if m_hex:
        hex_val = m_hex.group(1)
        if len(hex_val) == 3:
            hex_val = "".join(ch * 2 for ch in hex_val)
        return _classify_rgb_bucket(int(hex_val[0:2], 16), int(hex_val[2:4], 16), int(hex_val[4:6], 16))
    m_rgb = _RGB_COLOR_RE.match(color)
    if m_rgb:
        return _classify_rgb_bucket(int(m_rgb.group(1)), int(m_rgb.group(2)), int(m_rgb.group(3)))
    return None


class _ByondSanitizer(HTMLParser):
    """CSP-safe sanitizer for inline BYOND HTML in SS13 logs (zero inline styles, zero script/hrefs)."""

    _SIMPLE_ALLOWED = {"b": "strong", "strong": "strong", "i": "em", "em": "em", "u": "u"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.html_parts: List[str] = []
        self.plain_parts: List[str] = []
        self._tag_stack: List[Tuple[str, str]] = []
        self._in_byond_link: Optional[str] = None
        self._link_href: str = ""
        self._link_text: List[str] = []

    def _emit_literal(self, raw: str) -> None:
        if self._in_byond_link is not None:
            self._link_text.append(raw)
            return
        self.html_parts.append(html_escape(raw))
        self.plain_parts.append(raw)

    def _flush_link_as_text(self) -> None:
        leftover = "".join(self._link_text)
        self.html_parts.append(html_escape(leftover))
        self.plain_parts.append(leftover)
        self._in_byond_link = None
        self._link_href = ""
        self._link_text = []

    def _close_tag_kind(self, target_kind: str) -> None:
        if not any(kind == target_kind for kind, _ in self._tag_stack):
            return
        while self._tag_stack:
            kind, emitted = self._tag_stack.pop()
            if emitted:
                self.html_parts.append(f"</{emitted}>")
            if kind == target_kind:
                break

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if t == "a":
            if self._in_byond_link is not None:
                self._flush_link_as_text()
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

        if self._in_byond_link is not None:
            if t in self._SIMPLE_ALLOWED or t in ("span", "font"):
                return
            raw = self.get_starttag_text() or f"<{tag}>"
            self._link_text.append(raw)
            return

        if t in self._SIMPLE_ALLOWED:
            mapped = self._SIMPLE_ALLOWED[t]
            self.html_parts.append(f"<{mapped}>")
            self._tag_stack.append((mapped, mapped))
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
                self._tag_stack.append(("span", "span"))
            else:
                self._tag_stack.append(("span", ""))
            return

        # Unknown / dangerous tag -> render safely escaped as literal text so admins see raw chat payloads
        raw = self.get_starttag_text() or f"<{tag}>"
        self._emit_literal(raw)

    def handle_startendtag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        t = tag.lower()
        if t in self._SIMPLE_ALLOWED or t in ("span", "font", "a"):
            return
        raw = self.get_starttag_text() or f"<{tag}/>"
        self._emit_literal(raw)

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

        if self._in_byond_link is not None:
            if t in self._SIMPLE_ALLOWED or t in ("span", "font"):
                return
            self._link_text.append(f"</{tag}>")
            return

        if t in self._SIMPLE_ALLOWED:
            self._close_tag_kind(self._SIMPLE_ALLOWED[t])
            return

        if t in ("span", "font"):
            self._close_tag_kind("span")
            return

        self._emit_literal(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self._emit_literal(data)

    def handle_comment(self, data: str) -> None:
        self._emit_literal(f"<!--{data}-->")

    def close(self) -> None:
        super().close()
        if self._in_byond_link is not None:
            self._flush_link_as_text()
        while self._tag_stack:
            _kind, emitted = self._tag_stack.pop()
            if emitted:
                self.html_parts.append(f"</{emitted}>")


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
    coords: Optional[Tuple[int, int, int]] = None
    all_coords: List[Tuple[int, int, int]] = field(default_factory=list)


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


_TIME_BOUND_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_QUANT_BRACE_RE = re.compile(r"^\{(\d+)(?:,(\d*))?\}")


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


@dataclass
class _RoundMetaAccumulator:
    total_lines: int = 0
    gameplay_lines: int = 0
    category_counts: Dict[str, int] = field(default_factory=dict)
    round_id: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None

    def observe(self, ts: str, cat: str, msg: str, is_cont: bool) -> None:
        self.total_lines += 1
        self.category_counts[cat] = self.category_counts.get(cat, 0) + 1
        if cat not in TECHNICAL_NOISE_CATEGORIES:
            self.gameplay_lines += 1
        if ts:
            if self.started_at is None:
                self.started_at = ts
            self.ended_at = ts
        if self.round_id is None and not is_cont and cat == "SYSTEM":
            m_rid = _ROUND_START_ID_RE.search(msg)
            if m_rid:
                self.round_id = m_rid.group(1)


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


@dataclass
class _PreparedLogFilter:
    preset: str
    categories: Optional[Sequence[str]]
    ckey_norm: str
    q_lower: str
    pattern: Optional[Pattern[str]]
    regex_error: Optional[str]
    from_sec: Optional[int]
    to_sec: Optional[int]
    time_invalid: bool


def _prepare_log_filter(
    *,
    preset: str,
    categories: Optional[Sequence[str]],
    ckey: str,
    q: str,
    use_regex: bool,
    time_from: str,
    time_to: str,
) -> _PreparedLogFilter:
    pattern, regex_error = _compile_search_pattern(q, use_regex)
    from_sec, from_inv = _parse_time_seconds(time_from, is_end=False)
    to_sec, to_inv = _parse_time_seconds(time_to, is_end=True)
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
    display_raw = pre_display_raw if pre_display_raw is not None else (mask_sensitive_text(raw_msg) if mask_ips else raw_msg)
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
) -> Tuple[bool, Optional[str], Optional[Tuple[str, str]]]:
    if not category_matches_filters(cat, preset=filt.preset, selected_categories=filt.categories):
        return False, None, None
    if not _time_matches_range(ts, filt.from_sec, filt.to_sec, filt.time_invalid):
        return False, None, None

    if not filt.ckey_norm and not filt.q_lower and filt.pattern is None:
        return True, None, None

    display_raw = mask_sensitive_text(raw_msg) if mask_ips else raw_msg
    sanitized = sanitize_byond_html(display_raw)
    plain_text = sanitized[1]

    if filt.ckey_norm:
        line_ckeys = extract_ckeys_from_text(plain_text)
        if filt.ckey_norm not in line_ckeys and filt.ckey_norm not in _extract_word_ckey_tokens(plain_text):
            return False, None, None

    if filt.q_lower:
        if filt.pattern is not None:
            if not filt.pattern.search(plain_text):
                return False, None, None
        elif filt.q_lower not in plain_text.lower():
            return False, None, None

    return True, display_raw, sanitized


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
    matched, _, _ = _evaluate_line_filter(ts, cat, raw_msg, filt)
    return matched


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

    filt = _prepare_log_filter(
        preset=preset,
        categories=categories,
        ckey=ckey,
        q=q,
        use_regex=use_regex,
        time_from=time_from,
        time_to=time_to,
    )

    acc = _RoundMetaAccumulator()
    matched_raw: List[Tuple[int, str, str, str, bool, Optional[str], Optional[Tuple[str, str]]]] = []

    for line_no, ts, cat, raw_msg, is_cont in iter_parsed_file_lines(file_path):
        acc.observe(ts, cat, raw_msg, is_cont)

        if filt.regex_error:
            continue
        matched, pre_display_raw, pre_sanitized = _evaluate_line_filter(
            ts, cat, raw_msg, filt, mask_ips=mask_ips
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
    )
    if filt.regex_error:
        raise ValueError(filt.regex_error)

    out_lines: List[str] = []
    for _line_no, ts, cat, raw_msg, is_cont in iter_parsed_file_lines(file_path):
        matched, pre_display_raw, _ = _evaluate_line_filter(ts, cat, raw_msg, filt, mask_ips=mask_ips)
        if not matched:
            continue

        display_msg = pre_display_raw if pre_display_raw is not None else (mask_sensitive_text(raw_msg) if mask_ips else raw_msg)
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
    include_raw: bool = True,
) -> QdelReport:
    """Parse SierraBay12 qdel.log into structured sortable entries and summary KPIs."""
    raw_lines: List[str] = []
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

    with file_path.open("rb") as fh:
        for line_no, raw_bytes in enumerate(fh, 1):
            raw_line = _decode_log_line(raw_bytes).rstrip("\r\n")
            if line_no == 1 and raw_line.startswith("\ufeff"):
                raw_line = raw_line.lstrip("\ufeff")
            if include_raw:
                raw_lines.append(raw_line)
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
        raw_text="\n".join(raw_lines) if include_raw else "",
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
