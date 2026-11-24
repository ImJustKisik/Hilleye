"""Regular expressions, category metadata, presets, and data structures for SS13 log parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Dict, List, Optional, Pattern, Sequence, Set, Tuple

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
    "EMOTE": {"label": "EMOTE", "css": "cat-emote", "desc": "Эмоции (ручные)"},
    "EMOTE_MECH": {"label": "EMOTE_MECH", "css": "cat-emote-mech", "desc": "Эмоции (механические)"},
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
        "description": "SAY, WHISPER, OOC, EMOTE, EMOTE_MECH, ADMINSAY",
        "include": {"SAY", "WHISPER", "OOC", "EMOTE", "EMOTE_MECH", "ADMINSAY"},
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
_TIME_BOUND_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_QUANT_BRACE_RE = re.compile(r"^\{(\d+)(?:,(\d*))?\}")


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
    invalid_time_fields: Tuple[str, ...] = ()
    matching_category_counts: Optional[Dict[str, int]] = None


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
    invalid_time_fields: Tuple[str, ...]
    radius: int = 0
