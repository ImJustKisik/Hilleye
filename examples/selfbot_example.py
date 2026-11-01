"""
Hilleye // Discord Selfbot Client with Persistent Whitelist & Telemetry Commands
(discord.py-self + aiohttp)

Features:
- Persistent JSON whitelist (`examples/whitelist.json`) allowing trusted Discord
  users (in addition to the account owner) to invoke telemetry commands.
- Commands:
  * `,who <ckey>` (aliases: `,w`, `,dossier`, `,player`) — PNG player dossier card.
  * `,servers` (aliases: `,hub`, `,online`) — Active monitored SS13 servers list.
  * `,server <id | name>` (aliases: `,srv`, `,sector`) — Detailed server status & roster.
  * `,status` (aliases: `,health`, `,ping`) — Hilleye tracker health check.
  * `,wl list` — View whitelisted Discord user IDs (file and env).
  * `,wl add <@user | user_id>` — [Owner only] Add user to whitelist.
  * `,wl remove <@user | user_id>` — [Owner only] Remove user from whitelist.
  * `,find <ckey>`, `,seen <ckey>`, `,playtime <ckey>`, `,compare <a> <b>` — Player lookups.
  * `,round`, `,peak`, `,graph`, `,top` — Per-server round info, 24h stats, sparkline, top players.
  * `,watch <ckey>` / `,alert <server> <N>` — Background subscriptions with @mention notifications.
    `,watchlist` lists them, `,unwatch <ckey | #id | all>` removes them.
  * `,uptime`, `,reload` — Service commands.
  * `,commands` (aliases: `,h`) — Show command reference.

Usage:
    pip install discord.py-self aiohttp
    $env:DISCORD_USER_TOKEN = "your_token_here"
    python examples/selfbot_example.py
"""

from collections import OrderedDict
import asyncio
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
import discord
from discord.ext import commands

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("hilleye.selfbot")

# ============================================================================
# Discord Token & Path Resolution
# ============================================================================
_PLACEHOLDER_TOKEN = "YOUR_DISCORD_USER_TOKEN"
DISCORD_TOKEN = os.getenv("DISCORD_USER_TOKEN", _PLACEHOLDER_TOKEN)

_TOKEN_FILE = Path(__file__).resolve().parent / "token.txt"
if DISCORD_TOKEN == _PLACEHOLDER_TOKEN and _TOKEN_FILE.is_file():
    try:
        DISCORD_TOKEN = _TOKEN_FILE.read_text(encoding="utf-8").strip().strip('"').strip("'")
    except Exception as exc:
        logger.warning("Could not read token from %s: %s", _TOKEN_FILE, exc)


def _resolve_default_api_base() -> str:
    """Reads web port from project config.json (default 8080) to build API base URL."""
    env_base = os.getenv("HILLEYE_API_BASE")
    if env_base:
        return env_base.rstrip("/")
    cfg_path = Path(__file__).resolve().parent.parent / "config.json"
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            port = int(cfg.get("web", {}).get("port", 8080))
            return f"http://127.0.0.1:{port}"
        except Exception:
            pass
    return "http://127.0.0.1:8080"


HILLEYE_API_BASE = _resolve_default_api_base()
DEFAULT_WHITELIST_PATH = Path(__file__).resolve().parent / "whitelist.json"
WHITELIST_PATH = Path(os.getenv("HILLEYE_WHITELIST_FILE", str(DEFAULT_WHITELIST_PATH)))

DEFAULT_SUBS_PATH = Path(__file__).resolve().parent / "subscriptions.json"
SUBS_PATH = Path(os.getenv("HILLEYE_SUBSCRIPTIONS_FILE", str(DEFAULT_SUBS_PATH)))

try:
    SUBS_POLL_SECONDS = max(5.0, float(os.getenv("HILLEYE_SUBS_POLL_SECONDS", "30")))
except ValueError:
    SUBS_POLL_SECONDS = 30.0

MAX_SUBS_PER_USER = 10
NOT_FOUND = "404"
DEFAULT_CACHE_TTL = 10.0
MAX_CACHE_ENTRIES = 256
SPARK_CHARS = "▁▂▃▄▅▆▇█"
_started_at = time.time()


# ============================================================================
# Sanitization, Normalization & Mention Defusal
# ============================================================================

def defuse_mentions(value: Any) -> Any:
    """
    Recursively breaks '@' with a zero-width space (U+200B) in strings, lists,
    and dict values to prevent unauthorized @everyone/@here or role mentions.
    """
    if isinstance(value, str):
        return value.replace("@", "@\u200b")
    if isinstance(value, list):
        return [defuse_mentions(v) for v in value]
    if isinstance(value, tuple):
        return tuple(defuse_mentions(v) for v in value)
    if isinstance(value, dict):
        return {k: defuse_mentions(v) for k, v in value.items()}
    return value


# Alias for backward compatibility
_defuse_mentions = defuse_mentions


def escape_code_text(text: Any) -> str:
    """
    Sanitizes arbitrary text for safe inclusion inside Markdown inline code `...`.
    Replaces backticks and controls characters so untrusted text cannot break out.
    """
    raw = str(text if text is not None else "")
    clean = raw.replace("@", "@\u200b").replace("`", "'")
    return clean.strip()


def normalize_ckey(raw: str) -> str:
    """Normalizes a BYOND ckey to lowercase alphanumeric characters."""
    return re.sub(r"[^a-z0-9]", "", (raw or "").lower())


def extract_discord_user_id(raw: str) -> Optional[int]:
    """
    Strictly extracts a numeric Discord snowflake ID from raw input or mention.
    Accepts:
      - '<@123456789012345678>'
      - '<@!123456789012345678>'
      - '123456789012345678'
    Rejects strings with leading/trailing non-whitespace garbage or invalid bounds.
    """
    s = str(raw or "").strip()
    mention_match = re.fullmatch(r"<@!?(\d{15,22})>", s)
    if mention_match:
        return int(mention_match.group(1))

    digits_match = re.fullmatch(r"(\d{15,22})", s)
    if digits_match:
        return int(digits_match.group(1))

    return None


def format_age(seconds: float) -> str:
    """Formats duration in seconds into a human-readable string (days, hours, minutes)."""
    secs = max(0, int(seconds))
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ============================================================================
# Server Search & Multi-Tier Fuzzy Matching
# ============================================================================

def match_servers_by_query(
    servers: List[Dict[str, Any]],
    query: str,
) -> List[Dict[str, Any]]:
    """
    Matches servers by numeric ID or partial/full title/identifier (case-insensitive).
    Ranks matches by relevance tier first, then by current online players descending:
      0 - Exact ID match (`12` or `#12`)
      1 - Exact title match
      2 - Title prefix match
      3 - Word prefix match in title
      4 - Substring match in title
      5 - Multi-token substring match (all words from query present in title)
      6 - Substring match in server identifier or map_name
    """
    raw_q = (query or "").strip()
    if not raw_q:
        return []

    q_cf = raw_q.casefold()
    q_tokens = [tok for tok in q_cf.split() if tok]
    id_match = re.fullmatch(r"#?(\d+)", raw_q)
    target_id: Optional[int] = int(id_match.group(1)) if id_match else None

    scored: List[Tuple[int, int, int, Dict[str, Any]]] = []
    for srv in servers:
        srv_id = srv.get("id")
        title = str(srv.get("title") or "")
        title_cf = title.casefold()
        ident_cf = str(srv.get("identifier") or "").casefold()
        map_cf = str(srv.get("map_name") or "").casefold()
        players = int(srv.get("current_players") or 0)
        numeric_id = int(srv_id) if isinstance(srv_id, int) or str(srv_id).isdigit() else 999999

        tier: Optional[int] = None
        if target_id is not None and numeric_id == target_id:
            tier = 0
        elif title_cf == q_cf:
            tier = 1
        elif title_cf.startswith(q_cf):
            tier = 2
        elif any(w.startswith(q_cf) for w in re.split(r"\W+", title_cf) if w):
            tier = 3
        elif q_cf in title_cf:
            tier = 4
        elif len(q_tokens) > 1 and all(tok in title_cf for tok in q_tokens):
            tier = 5
        elif q_cf in ident_cf or q_cf in map_cf:
            tier = 6

        if tier is not None:
            scored.append((tier, -players, numeric_id, srv))

    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    if not scored:
        return []

    best_tier = scored[0][0]
    if best_tier in (0, 1):
        return [item[3] for item in scored if item[0] == best_tier]

    return [item[3] for item in scored]


# ============================================================================
# Atomic Persistence & Quarantine
# ============================================================================

def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Atomically writes JSON payload using temporary file and atomic rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    payload_str = json.dumps(data, indent=indent, ensure_ascii=False)
    tmp_path.write_text(payload_str, encoding="utf-8")
    os.replace(tmp_path, target)


def safe_load_json(
    path: Path,
    default_factory: Any,
    backup_on_error: bool = True,
) -> Tuple[Any, bool]:
    """Safely loads JSON file from disk, quarantining corrupted files."""
    target = Path(path)
    if not target.is_file():
        return (default_factory() if callable(default_factory) else default_factory), True

    try:
        raw_text = target.read_text(encoding="utf-8")
        parsed = json.loads(raw_text)
        return parsed, True
    except Exception as exc:
        logger.error("Failed to load JSON file %s: %s", target, exc)
        if backup_on_error:
            timestamp = int(time.time())
            backup_path = target.with_name(f"{target.name}.corrupted.{timestamp}.bak")
            try:
                target.rename(backup_path)
                logger.warning("Quarantined corrupted file %s -> %s", target, backup_path)
            except Exception as b_exc:
                logger.error("Failed to rename corrupted file %s: %s", target, b_exc)
        return (default_factory() if callable(default_factory) else default_factory), False


# ============================================================================
# Whitelist Manager
# ============================================================================

class WhitelistManager:
    """
    Manages a persistent set of Discord user IDs allowed to execute bot commands.
    Caches allowed IDs in memory with fast mtime checking.
    The selfbot account owner is always implicitly authorized.
    """

    def __init__(self, path: Path = WHITELIST_PATH) -> None:
        self.path = Path(path)
        self.allowed_ids: Set[int] = set()
        self.env_ids: Set[int] = set()
        self._last_mtime: int = 0
        self.load()

        # Parse HILLEYE_WHITELIST_IDS env var (comma-separated snowflakes)
        env_raw = os.getenv("HILLEYE_WHITELIST_IDS", "")
        for part in env_raw.split(","):
            uid = extract_discord_user_id(part)
            if uid is not None:
                self.env_ids.add(uid)

    def _safe_load_from_disk(self) -> Tuple[Any, bool]:
        """Self-contained safe JSON loader for WhitelistManager."""
        if not self.path.is_file():
            return {"allowed_user_ids": []}, True
        try:
            mtime = self.path.stat().st_mtime_ns
            self._last_mtime = mtime
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data, True
        except Exception:
            try:
                timestamp = int(time.time())
                bak = self.path.with_name(f"{self.path.name}.corrupted.{timestamp}.bak")
                self.path.rename(bak)
            except Exception:
                pass
            return {"allowed_user_ids": []}, False

    def load(self) -> Set[int]:
        """Loads allowed Discord user IDs from disk into memory."""
        data, ok = self._safe_load_from_disk()
        if not ok:
            return self.allowed_ids

        ids = data.get("allowed_user_ids", data) if isinstance(data, dict) else data
        if isinstance(ids, list):
            self.allowed_ids = {int(x) for x in ids if str(x).isdigit()}
        return self.allowed_ids

    def save(self) -> None:
        """Atomically persists allowed Discord user IDs to disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"allowed_user_ids": sorted(self.allowed_ids)}
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
        try:
            self._last_mtime = self.path.stat().st_mtime_ns
        except Exception:
            pass

    def add(self, user_id: int) -> bool:
        """Adds a Discord user ID to whitelist and saves. Returns True if newly added."""
        uid = int(user_id)
        is_new = uid not in self.allowed_ids
        self.allowed_ids.add(uid)
        self.save()
        return is_new

    def remove(self, user_id: int) -> bool:
        """Removes a Discord user ID from whitelist and saves. Returns True if existed."""
        uid = int(user_id)
        if uid in self.allowed_ids:
            self.allowed_ids.remove(uid)
            self.save()
            return True
        return False

    def is_allowed(self, author_id: int, owner_id: Optional[int] = None) -> bool:
        """Checks if author_id is the account owner or in whitelist (checks mtime)."""
        uid = int(author_id)
        if owner_id is not None and uid == int(owner_id):
            return True

        # Check if file changed on disk via fast stat check
        try:
            if self.path.is_file():
                current_mtime = self.path.stat().st_mtime_ns
                if current_mtime != self._last_mtime:
                    self.load()
        except Exception:
            pass

        return uid in (self.allowed_ids | self.env_ids)

    def get_details(self) -> Dict[str, Set[int]]:
        """Returns sets of file-configured and env-configured allowed IDs."""
        return {
            "allowed_ids": set(self.allowed_ids),
            "env_ids": set(self.env_ids),
        }


# ============================================================================
# Subscription Manager
# ============================================================================

class SubscriptionManager:
    """
    Stores watch and alert subscriptions atomically on disk.
    Quarantines corrupted files and prevents accidental overwrite.
    """

    def __init__(self, path: Path = SUBS_PATH) -> None:
        self.path = Path(path)
        self.items: List[Dict[str, Any]] = []
        self.next_id = 1
        self.load()

    def load(self) -> None:
        """Loads subscriptions from disk into memory safely."""
        data, ok = safe_load_json(self.path, default_factory=dict, backup_on_error=True)
        if not ok:
            return

        items = data.get("subscriptions", []) if isinstance(data, dict) else data
        if isinstance(items, list):
            self.items = [x for x in items if isinstance(x, dict)]
            self.next_id = max([int(x.get("id", 0)) for x in self.items] + [0]) + 1

    def save(self) -> None:
        """Atomically persists subscriptions to disk."""
        payload = {"subscriptions": self.items}
        atomic_write_json(self.path, payload)

    def for_user(self, user_id: int) -> List[Dict[str, Any]]:
        return [s for s in self.items if int(s.get("user_id", 0)) == int(user_id)]

    def add(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record["id"] = self.next_id
        self.next_id += 1
        self.items.append(record)
        self.save()
        return record

    def remove_ids(self, user_id: int, ids: Set[int]) -> int:
        before = len(self.items)
        self.items = [
            s for s in self.items
            if not (int(s.get("user_id", 0)) == int(user_id) and int(s.get("id", 0)) in ids)
        ]
        removed = before - len(self.items)
        if removed:
            self.save()
        return removed

    def remove_by_id(self, sub_id: int) -> bool:
        before = len(self.items)
        self.items = [s for s in self.items if int(s.get("id", 0)) != int(sub_id)]
        if len(self.items) != before:
            self.save()
            return True
        return False


# ============================================================================
# Sparkline Generator
# ============================================================================

def build_sparkline(
    snapshots: List[Dict[str, Any]],
    now: Optional[float] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Builds a true 24-hour sparkline across 24 hourly buckets from [now - 86400 .. now].
    Returns (sparkline_chars, stats_dict).
    """
    if now is None:
        now = time.time()

    window_start = now - 86400.0
    buckets: List[Optional[int]] = [None] * 24

    for snap in snapshots:
        ts = float(snap.get("timestamp", 0))
        if ts < window_start or ts > now:
            continue
        idx = min(23, max(0, int((ts - window_start) / 3600.0)))
        cnt = int(snap.get("player_count", 0))
        buckets[idx] = cnt if buckets[idx] is None else max(buckets[idx], cnt)

    present_values = [v for v in buckets if v is not None]
    if not present_values:
        return " " * 24, {"min": 0, "max": 0, "avg": 0, "count": 0}

    lo = min(present_values)
    hi = max(present_values)
    avg_val = round(sum(present_values) / len(present_values), 1)

    chars: List[str] = []
    for v in buckets:
        if v is None:
            chars.append(" ")
        elif hi == lo:
            chars.append(SPARK_CHARS[0])
        else:
            fraction = (v - lo) / (hi - lo)
            char_idx = min(len(SPARK_CHARS) - 1, int(fraction * (len(SPARK_CHARS) - 1)))
            chars.append(SPARK_CHARS[char_idx])

    sparkline_str = "".join(chars)
    stats = {
        "min": lo,
        "max": hi,
        "avg": avg_val,
        "count": len(present_values),
    }
    return sparkline_str, stats


# ============================================================================
# Bounded LRU Cache & Hilleye API Client
# ============================================================================

class BoundedTTLCache:
    """Bounded LRU + TTL cache to avoid unbounded memory growth."""

    def __init__(self, max_entries: int = MAX_CACHE_ENTRIES, default_ttl: float = DEFAULT_CACHE_TTL):
        self.max_entries = max_entries
        self.default_ttl = default_ttl
        self._entries: OrderedDict[str, Tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Optional[Any]:
        now = time.monotonic()
        if key not in self._entries:
            return None
        created_at, val = self._entries[key]
        if now - created_at > self.default_ttl:
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return val

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        now = time.monotonic()
        if key in self._entries:
            self._entries.pop(key, None)
        elif len(self._entries) >= self.max_entries:
            self._entries.popitem(last=False)
        self._entries[key] = (now, value)

    def clear(self) -> None:
        self._entries.clear()


class HilleyeAPIClient:
    """
    Unified HTTP client for Hilleye backend.
    - Manages single persistent aiohttp.ClientSession
    - Strict HTTP 503 handling: only allowed for /health
    - Automatic mention defusal on all returned JSON data
    - Server lookup and player card retrieval
    """

    def __init__(self, base_url: str, cache_ttl: float = DEFAULT_CACHE_TTL):
        self.base_url = base_url.rstrip("/")
        self.cache = BoundedTTLCache(max_entries=MAX_CACHE_ENTRIES, default_ttl=cache_ttl)
        self._session: Optional[aiohttp.ClientSession] = None

    def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=5.0)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def api_json(
        self,
        path: str,
        *,
        cache: bool = True,
        allow_503: bool = False,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """
        Queries Hilleye JSON API.
        Returns (data, None) on success.
        Returns (None, NOT_FOUND) on HTTP 404.
        Returns (None, error_str) on other failures.
        """
        if cache:
            cached_val = self.cache.get(path)
            if cached_val is not None:
                return cached_val, None

        url = f"{self.base_url}{path}"
        try:
            session = self.get_session()
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None, NOT_FOUND

                # Only /health (allow_503=True) may accept 503 as a valid response
                if resp.status == 503 and not allow_503:
                    return None, "Hilleye API temporarily unavailable (HTTP 503)."

                if resp.status not in (200, 503) if allow_503 else (resp.status != 200):
                    return None, f"Hilleye API returned HTTP {resp.status}."

                raw_data = await resp.json()
        except Exception as exc:
            logger.warning("API query failed for %s: %s", path, exc)
            return None, "Hilleye API connection failed."

        sanitized_data = defuse_mentions(raw_data)
        if cache:
            self.cache.set(path, sanitized_data)
        return sanitized_data, None

    async def api_card(self, ckey: str) -> Tuple[Optional[bytes], Optional[str]]:
        """Fetches player card PNG bytes."""
        clean = normalize_ckey(ckey)
        if not clean:
            return None, "Invalid or empty ckey."

        url = f"{self.base_url}/api/v1/player/{clean}/card.png"
        try:
            session = self.get_session()
            async with session.get(url) as resp:
                if resp.status == 404:
                    return None, NOT_FOUND
                if resp.status != 200:
                    return None, f"Hilleye API returned HTTP {resp.status}."
                png_bytes = await resp.read()
                return png_bytes, None
        except Exception as exc:
            logger.warning("Player card query failed for %s: %s", clean, exc)
            return None, "Hilleye API connection failed."

    async def resolve_server(self, query: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Unified server resolver. Supports direct numeric ID (e.g. '12' or '#12')
        and multi-tier fuzzy matching across title, identifier, and map_name.
        """
        raw_query = (query or "").strip()
        if not raw_query:
            return None, "No server query provided."

        id_match = re.fullmatch(r"#?(\d+)", raw_query)
        if id_match and int(id_match.group(1)) > 0:
            target_id = int(id_match.group(1))
            srv, err = await self.api_json(f"/api/v1/server/{target_id}")
            if srv is not None:
                return srv, None

        # Fall back to matching among all active servers
        data, err = await self.api_json("/api/v1/servers")
        if err:
            return None, err

        servers = data.get("servers", []) if isinstance(data, dict) else []
        matches = match_servers_by_query(servers, raw_query)
        if not matches:
            return None, f"Server matching `{escape_code_text(raw_query)}` not found."

        best = matches[0]
        # Retrieve full server details
        detail, err_detail = await self.api_json(f"/api/v1/server/{best.get('id')}")
        if detail is not None:
            return detail, None
        return best, None

    async def fetch_player(self, ckey: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """Fetches player dossier by ckey."""
        clean = normalize_ckey(ckey)
        if not clean:
            return None, "Invalid or empty ckey."
        data, err = await self.api_json(f"/api/v1/player/{clean}")
        if err == NOT_FOUND:
            return None, f"Target `{clean}` not found in Hilleye telemetry."
        return data, err


# Singletons
api_client = HilleyeAPIClient(base_url=HILLEYE_API_BASE)
whitelist = WhitelistManager(path=WHITELIST_PATH)
subs = SubscriptionManager(path=SUBS_PATH)

_watch_online: Dict[str, bool] = {}   # ckey -> last known online state
_alert_over: Dict[int, bool] = {}     # subscription id -> last known "above threshold" state
_subs_task: Optional["asyncio.Task[None]"] = None


# Backward-compatible helper functions
async def api_json(path: str, *, cache: bool = True) -> Tuple[Optional[Any], Optional[str]]:
    return await api_client.api_json(path, cache=cache)


async def resolve_server(query: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    return await api_client.resolve_server(query)


async def fetch_player(ckey: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    return await api_client.fetch_player(ckey)


# ============================================================================
# Bot Subclass with Clean Lifecycle & Safe Allowed Mentions
# ============================================================================

class HilleyeBot(commands.Bot):
    """
    Selfbot client configured with ironclad mention protection:
    prevents @everyone and @here mentions from ever firing.
    """

    def __init__(self, **kwargs: Any) -> None:
        # Ironclad mention protection at Discord gateway payload level
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=True,       # Allows explicit user pings for alerts/watches
            replied_user=True,
        )
        kwargs.setdefault("allowed_mentions", allowed_mentions)
        kwargs.setdefault("command_prefix", ",")
        kwargs.setdefault("self_bot", False)
        kwargs.setdefault("help_command", None)
        super().__init__(**kwargs)

    async def close(self) -> None:
        """Gracefully closes HTTP sessions on bot shutdown."""
        logger.info("Shutting down HilleyeBot; closing API client sessions...")
        await api_client.close()
        await super().close()

    async def get_context(self, message: discord.Message, *, cls=commands.Context) -> commands.Context:
        """
        Resolves command context for whitelisted users and owner.
        """
        ctx = await super().get_context(message, cls=cls)
        if not ctx.valid and message.content:
            prefixes = (
                self.command_prefix
                if isinstance(self.command_prefix, (tuple, list))
                else (self.command_prefix,)
            )
            for p in prefixes:
                if message.content.startswith(p):
                    view = commands.view.StringView(message.content)
                    view.skip_string(p)
                    invoker = view.get_word()
                    cmd = self.all_commands.get(invoker)
                    if cmd is not None:
                        ctx.prefix = p
                        ctx.command = cmd
                        ctx.invoker = invoker
                        ctx.view = view
                    break
        return ctx


bot = HilleyeBot()


def is_owner(ctx: commands.Context) -> bool:
    """Returns True if the command caller is the selfbot account owner."""
    return bool(bot.user and ctx.author.id == bot.user.id)


async def send_long(ctx: commands.Context, text: str) -> None:
    """Sends text, truncating to stay under Discord's 2000 character limit."""
    safe_text = defuse_mentions(text)
    if len(safe_text) > 1950:
        safe_text = safe_text[:1940] + "\n…(truncated)"
    await ctx.send(safe_text)


# ============================================================================
# Event Handlers & Message Router
# ============================================================================

@bot.event
async def on_ready() -> None:
    if bot.user:
        logger.info(
            "Logged in as %s (ID: %s) | Whitelisted users: %d | Subscriptions: %d",
            bot.user,
            bot.user.id,
            len(whitelist.allowed_ids),
            len(subs.items),
        )
    start_subscription_loop()


@bot.event
async def on_message(message: discord.Message) -> None:
    """
    High-performance message router:
    - Skips bot messages
    - Checks prefix in memory BEFORE checking whitelist or touching disk
    - Checks whitelist against memory cache (zero disk I/O on normal chat messages)
    """
    if message.author.bot or not message.content:
        return

    # Check prefix FIRST: if not a command, do not touch whitelist or disk
    prefixes = (
        bot.command_prefix
        if isinstance(bot.command_prefix, (tuple, list))
        else (bot.command_prefix,)
    )
    if not any(message.content.startswith(p) for p in prefixes):
        return

    owner_id = bot.user.id if bot.user else None
    if not whitelist.is_allowed(message.author.id, owner_id=owner_id):
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        logger.info("Command '%s' invoked by %s (ID: %s)", message.content, message.author, message.author.id)
        await bot.invoke(ctx)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception) -> None:
    """Unified error handler for bot commands."""
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Cooldown: please wait `{error.retry_after:.1f}s` before reusing this command.")
        return

    if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument, commands.UserInputError)):
        await ctx.send(f"⚠️ Invalid command arguments. Use `,commands` for syntax.")
        return

    # Log full traceback internally, return sanitized message to chat
    logger.exception("Unhandled error in command '%s': %s", ctx.command, error)
    await ctx.send("⚠️ An error occurred while executing the command.")


# ============================================================================
# Core Telemetry Commands
# ============================================================================

@bot.command(name="who", aliases=["w", "dossier", "player"])
@commands.cooldown(1, 3.0, commands.BucketType.user)
async def who_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """
    Fetches a player's PNG dossier card from Hilleye and uploads it to the channel.
    Syntax: ,who <ckey>
    """
    clean_ckey = normalize_ckey(ckey)
    if not clean_ckey:
        await ctx.send("Usage: `,who <ckey>`")
        return

    png_bytes, err = await api_client.api_card(clean_ckey)
    if err == NOT_FOUND:
        await ctx.send(f"Target `{escape_code_text(clean_ckey)}` not found in Hilleye telemetry.")
        return
    if err or not png_bytes:
        await ctx.send(err or "Failed to retrieve player card.")
        return

    file_attachment = discord.File(io.BytesIO(png_bytes), filename=f"{clean_ckey}.png")
    await ctx.send(file=file_attachment)


@bot.command(name="servers", aliases=["hub", "online"])
@commands.cooldown(1, 3.0, commands.BucketType.user)
async def servers_command(ctx: commands.Context) -> None:
    """
    Displays top active monitored SS13 servers sorted by current player count.
    Syntax: ,servers
    """
    data, err = await api_client.api_json("/api/v1/servers")
    if err:
        await ctx.send(err)
        return

    servers = sorted(
        (data or {}).get("servers", []),
        key=lambda s: int(s.get("current_players") or 0),
        reverse=True,
    )[:12]

    if not servers:
        await ctx.send("No active monitored servers found.")
        return

    lines = ["```", "ID  | ONLINE | SERVER TITLE                 | MAP", "-" * 58]
    for srv in servers:
        sid = escape_code_text(srv.get("id", "?"))
        players = int(srv.get("current_players") or 0)
        title = escape_code_text(srv.get("title") or "Unknown")[:28]
        map_name = escape_code_text(srv.get("map_name") or "N/A")[:14]
        lines.append(f"#{sid:<2} | {players:>6} | {title:<28} | {map_name}")
    lines.append("```")
    await send_long(ctx, "\n".join(lines))


@bot.command(name="server", aliases=["srv", "sector"])
@commands.cooldown(1, 2.0, commands.BucketType.user)
async def server_command(ctx: commands.Context, *, query: str = "") -> None:
    """
    Displays detailed telemetry for a specific server by ID or full/partial name.
    Syntax: ,server <id | name>
    """
    raw_query = (query or "").strip()
    if not raw_query:
        await ctx.send("Usage: `,server <id | name>` (see list via `,servers`)")
        return

    srv, err = await api_client.resolve_server(raw_query)
    if err:
        await ctx.send(err)
        return

    title = escape_code_text(srv.get("title", f"Server #{srv.get('id', '?')}"))
    players = int(srv.get("current_players") or 0)
    map_name = escape_code_text(srv.get("map_name") or "N/A")
    gamemode = escape_code_text(srv.get("gamemode") or "N/A")
    round_hms = escape_code_text(srv.get("round_duration_str") or srv.get("round_duration_hms") or "00:00:00")
    
    online_roster = [escape_code_text(p.get("ckey", "")) for p in srv.get("online_players", [])[:15]]
    roster_str = ", ".join(online_roster) if online_roster else "None recorded"
    if len(srv.get("online_players", [])) > 15:
        roster_str += f" (+{len(srv['online_players']) - 15} more)"

    msg = (
        f"**[{srv.get('id')}] {title}**\n"
        f"> **Online:** `{players}` | **Map:** `{map_name}` | **Mode:** `{gamemode}` | **Round:** `{round_hms}`\n"
        f"> **Active Roster:** `{roster_str}`"
    )
    await send_long(ctx, msg)


@bot.command(name="status", aliases=["health", "ping"])
@commands.cooldown(1, 1.0, commands.BucketType.user)
async def status_command(ctx: commands.Context) -> None:
    """
    Checks Hilleye API and background tracker operational status.
    Syntax: ,status
    """
    data, err = await api_client.api_json("/health", allow_503=True)
    if err and not data:
        await ctx.send(f"Hilleye API unreachable: `{escape_code_text(err)}`")
        return

    tracker = (data or {}).get("tracker", {})
    status_str = escape_code_text((data or {}).get("status", "unknown").upper())
    await ctx.send(
        f"**Hilleye Status:** `{status_str}` | "
        f"**Polls:** `{tracker.get('poll_count', 0)}` | "
        f"**Tracked Servers:** `{tracker.get('servers_tracked', 0)}`"
    )


# ============================================================================
# Whitelist Management Commands
# ============================================================================

@bot.command(name="wl", aliases=["whitelist"])
@commands.cooldown(1, 1.0, commands.BucketType.user)
async def whitelist_command(
    ctx: commands.Context,
    action: str = "list",
    target: str = "",
) -> None:
    """
    Manages the user whitelist.
    Syntax:
      ,wl list
      ,wl add <@user | user_id>     (Owner only)
      ,wl remove <@user | user_id>  (Owner only)
    """
    act = action.lower().strip()

    if act in ("list", "ls", "show"):
        owner_str = f"`{bot.user.id}` (Owner)" if bot.user else "Owner"
        details = whitelist.get_details()
        file_ids_str = ", ".join(f"`{uid}`" for uid in sorted(details["allowed_ids"])) or "None"
        env_ids_str = ", ".join(f"`{uid}` (env)" for uid in sorted(details["env_ids"]))
        combined = f"Allowed: {file_ids_str}"
        if env_ids_str:
            combined += f" | {env_ids_str}"
        await ctx.send(f"**Whitelist:** {owner_str} | {combined}")
        return

    if not is_owner(ctx):
        await ctx.send("Only the selfbot owner can modify the whitelist.")
        return

    uid = extract_discord_user_id(target)
    if uid is None:
        await ctx.send("Specify a valid Discord User ID or `@mention`. Example: `,wl add 123456789012345678`")
        return

    if act in ("add", "+", "allow"):
        added = whitelist.add(uid)
        status = "added to" if added else "already in"
        await ctx.send(f"User `{uid}` {status} whitelist (`{len(whitelist.allowed_ids)}` file entries).")
    elif act in ("remove", "rm", "del", "-"):
        if uid in whitelist.env_ids:
            await ctx.send(f"User `{uid}` is configured via environment variable `HILLEYE_WHITELIST_IDS` and cannot be removed via command.")
            return
        removed = whitelist.remove(uid)
        status = "removed from" if removed else "was not in"
        await ctx.send(f"User `{uid}` {status} whitelist (`{len(whitelist.allowed_ids)}` file entries).")
    else:
        await ctx.send("Usage: `,wl list` | `,wl add <@user|id>` | `,wl remove <@user|id>`")


# ============================================================================
# Subscription Loop & Background Notifications
# ============================================================================

async def notify(sub: Dict[str, Any], text: str) -> None:
    """
    Posts a message in the subscription's channel pinging the subscriber.
    Auto-removes subscriptions if the channel was deleted or forbidden.
    Throttles message dispatch to protect against Discord rate limits.
    """
    user_id = int(sub.get("user_id", 0))
    sub_id = int(sub.get("id", 0))
    channel_id = int(sub.get("channel_id", 0))
    content = f"<@{user_id}> {text}"

    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if isinstance(channel, discord.abc.Messageable):
            await channel.send(content)
            await asyncio.sleep(0.5)  # Throttling between successive notifications
    except (discord.NotFound, discord.Forbidden) as exc:
        logger.warning(
            "Channel %s is inaccessible (%s). Removing subscription #%s for user %s",
            channel_id,
            exc.__class__.__name__,
            sub_id,
            user_id,
        )
        subs.remove_by_id(sub_id)
    except Exception as exc:
        logger.warning("Failed to notify subscription #%s: %s", sub_id, exc)


async def check_watches() -> None:
    """Checks online status of watched ckeys and alerts subscribers."""
    watches = [s for s in subs.items if s.get("type") == "watch"]
    if not watches:
        return

    owner_id = bot.user.id if bot.user else None
    ckeys = sorted({s["ckey"] for s in watches})

    # Fetch player statuses concurrently
    results = await asyncio.gather(*(api_client.fetch_player(c) for c in ckeys))

    for ckey, (data, err) in zip(ckeys, results):
        if err == NOT_FOUND:
            online, server = False, None
        elif err or not isinstance(data, dict):
            continue  # transient API error: keep previous state
        else:
            online = bool(data.get("is_online"))
            server = data.get("current_server")

        prev = _watch_online.get(ckey)
        _watch_online[ckey] = online

        # Edge-triggered: notify on transition from offline (or unknown) to online
        if prev is False and online:
            for s in [w for w in watches if w.get("ckey") == ckey]:
                # Verify that the subscriber is STILL in the whitelist before sending notification
                sub_user_id = int(s.get("user_id", 0))
                if not whitelist.is_allowed(sub_user_id, owner_id=owner_id):
                    continue

                server_title = escape_code_text(server) if server else ""
                where = f" on **{server_title}**" if server_title else ""
                await notify(s, f"🟢 `{escape_code_text(ckey)}` just came online{where}.")


async def check_alerts() -> None:
    """Checks server player count thresholds and notifies subscribers."""
    alerts = [s for s in subs.items if s.get("type") == "alert"]
    if not alerts:
        return

    owner_id = bot.user.id if bot.user else None
    data, err = await api_client.api_json("/api/v1/servers", cache=False)
    if err or not isinstance(data, dict):
        return

    by_id = {int(s["id"]): s for s in data.get("servers", []) if str(s.get("id", "")).isdigit()}
    for sub in alerts:
        sub_user_id = int(sub.get("user_id", 0))
        if not whitelist.is_allowed(sub_user_id, owner_id=owner_id):
            continue

        srv = by_id.get(int(sub["server_id"]))
        if srv is None:
            continue

        players = int(srv.get("current_players") or 0)
        threshold = int(sub.get("threshold", 0))
        over = players > threshold
        sub_id = int(sub.get("id", 0))
        prev = _alert_over.get(sub_id)
        _alert_over[sub_id] = over

        # Edge-triggered: ping on transition to "above threshold"
        if over and prev is False:
            title = escape_code_text(srv.get("title") or sub.get("server_title") or f"#{sub['server_id']}")
            await notify(sub, f"🔔 **{title}** online is `{players}` (above `{threshold}`).")


async def subscription_loop() -> None:
    """Background polling loop for watch and alert subscriptions."""
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await check_watches()
            await check_alerts()
        except Exception as exc:
            logger.error("Subscription poll error: %s", exc)
        await asyncio.sleep(SUBS_POLL_SECONDS)


def start_subscription_loop() -> None:
    """Starts subscription polling task if not already running."""
    global _subs_task
    if _subs_task is None or _subs_task.done():
        _subs_task = asyncio.create_task(subscription_loop())


# ============================================================================
# Subscription Commands
# ============================================================================

@bot.command(name="watch")
@commands.cooldown(1, 1.0, commands.BucketType.user)
async def watch_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """Pings you when a player comes online. Syntax: ,watch <ckey>"""
    clean = normalize_ckey(ckey)
    if not clean:
        await ctx.send("Usage: `,watch <ckey>`")
        return

    mine = subs.for_user(ctx.author.id)
    if any(s.get("type") == "watch" and s.get("ckey") == clean for s in mine):
        await ctx.send(f"You are already watching `{escape_code_text(clean)}`.")
        return
    if len(mine) >= MAX_SUBS_PER_USER:
        await ctx.send(f"Subscription limit reached (`{MAX_SUBS_PER_USER}`). Use `,unwatch`.")
        return

    data, err = await api_client.api_json(f"/api/v1/player/{clean}", cache=False)
    if err and err != NOT_FOUND:
        await ctx.send(err)
        return

    online = bool(data.get("is_online")) if isinstance(data, dict) else False
    _watch_online[clean] = online
    rec = subs.add({
        "type": "watch",
        "user_id": ctx.author.id,
        "channel_id": ctx.channel.id,
        "ckey": clean,
    })
    note = " (currently **online** — you'll be pinged on next login)" if online else ""
    if err == NOT_FOUND:
        note = " (not seen yet — you'll be pinged when first spotted online)"
    await ctx.send(f"{ctx.author.mention} watching `{escape_code_text(clean)}` as `#{rec['id']}`{note}.")


@bot.command(name="alert")
@commands.cooldown(1, 1.0, commands.BucketType.user)
async def alert_command(ctx: commands.Context, *args: str) -> None:
    """Pings you when server online exceeds N. Syntax: ,alert <id | name> <N>"""
    if len(args) < 2 or not re.fullmatch(r"[0-9]+", args[-1]):
        await ctx.send("Usage: `,alert <id | name> <N>` — pings you when online rises above N.")
        return

    threshold = int(args[-1])
    if not 0 <= threshold <= 1000:
        await ctx.send("Threshold must be between 0 and 1000.")
        return

    mine = subs.for_user(ctx.author.id)
    if len(mine) >= MAX_SUBS_PER_USER:
        await ctx.send(f"Subscription limit reached (`{MAX_SUBS_PER_USER}`). Use `,unwatch`.")
        return

    srv_query = " ".join(args[:-1])
    srv, err = await api_client.resolve_server(srv_query)
    if err:
        await ctx.send(err)
        return

    sid = int(srv["id"])
    if any(s.get("type") == "alert" and int(s.get("server_id", 0)) == sid and int(s.get("threshold", 0)) == threshold for s in mine):
        await ctx.send("You already have this alert.")
        return

    players = int(srv.get("current_players") or 0)
    title = escape_code_text(srv.get("title", f"#{sid}"))
    rec = subs.add({
        "type": "alert",
        "user_id": ctx.author.id,
        "channel_id": ctx.channel.id,
        "server_id": sid,
        "server_title": srv.get("title"),
        "threshold": threshold,
    })
    _alert_over[int(rec["id"])] = players > threshold
    state = "currently above — re-arms when it drops" if players > threshold else "armed"
    await ctx.send(
        f"{ctx.author.mention} alert `#{rec['id']}`: **{title}** "
        f"online > `{threshold}` (now `{players}`, {state})."
    )


@bot.command(name="watchlist", aliases=["subs"])
@commands.cooldown(1, 1.0, commands.BucketType.user)
async def watchlist_command(ctx: commands.Context) -> None:
    """Shows your active subscriptions. Syntax: ,watchlist"""
    mine = subs.for_user(ctx.author.id)
    if not mine:
        await ctx.send("You have no subscriptions. Use `,watch <ckey>` or `,alert <server> <N>`.")
        return

    lines = [f"**Subscriptions for {ctx.author.mention}:**"]
    for s in mine:
        sub_id = s.get("id")
        if s.get("type") == "watch":
            lines.append(f"- `#{sub_id}` watch `{escape_code_text(s.get('ckey'))}`")
        else:
            title = escape_code_text(s.get("server_title", f"#{s.get('server_id')}"))
            lines.append(f"- `#{sub_id}` alert **{title}** > `{s.get('threshold')}`")
    await send_long(ctx, "\n".join(lines))


@bot.command(name="unwatch", aliases=["unalert"])
@commands.cooldown(1, 1.0, commands.BucketType.user)
async def unwatch_command(ctx: commands.Context, *, target: str = "") -> None:
    """Removes subscriptions. Syntax: ,unwatch <ckey | #id | all>"""
    t = target.strip()
    if not t:
        await ctx.send("Usage: `,unwatch <ckey | #id | all>` (see `,watchlist`)")
        return

    mine = subs.for_user(ctx.author.id)
    if t.lower() == "all":
        ids = {int(s["id"]) for s in mine if "id" in s}
    elif re.fullmatch(r"#\d+", t):
        ids = {int(t[1:])}
    else:
        clean = normalize_ckey(t)
        ids = {int(s["id"]) for s in mine if s.get("type") == "watch" and s.get("ckey") == clean and "id" in s}

    removed = subs.remove_ids(ctx.author.id, ids)
    await ctx.send(f"Removed `{removed}` subscription(s)." if removed else "No matching subscription found.")


# ============================================================================
# Player Dossier Commands
# ============================================================================

@bot.command(name="find", aliases=["where"])
@commands.cooldown(1, 2.0, commands.BucketType.user)
async def find_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """Shows which server a player is on right now. Syntax: ,find <ckey>"""
    clean = normalize_ckey(ckey)
    if not clean:
        await ctx.send("Usage: `,find <ckey>`")
        return

    d, err = await api_client.fetch_player(clean)
    if err:
        await ctx.send(err)
        return

    if d.get("is_online"):
        server_str = escape_code_text(d.get("current_server"))
        session_dur = escape_code_text(d.get("current_session_duration") or "?")
        await ctx.send(
            f"🟢 `{clean}` is online on **{server_str}** "
            f"(session `{session_dur}`)."
        )
    else:
        last_seen = escape_code_text(d.get("last_seen") or "unknown")
        await ctx.send(f"⚫ `{clean}` is offline. Last seen: `{last_seen}`.")


@bot.command(name="seen")
@commands.cooldown(1, 2.0, commands.BucketType.user)
async def seen_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """Shows when a player was last seen. Syntax: ,seen <ckey>"""
    clean = normalize_ckey(ckey)
    if not clean:
        await ctx.send("Usage: `,seen <ckey>`")
        return

    d, err = await api_client.fetch_player(clean)
    if err:
        await ctx.send(err)
        return

    recent = d.get("recent_sessions") or []
    last_srv = escape_code_text(recent[0].get("server_title")) if recent else None
    state = f"online on **{escape_code_text(d.get('current_server'))}**" if d.get("is_online") else "offline"
    msg = (
        f"**{clean}** — {state}\n"
        f"> **Last seen:** `{escape_code_text(d.get('last_seen') or 'unknown')}`"
        + (f" on **{last_srv}**" if last_srv else "")
        + f"\n> **First seen:** `{escape_code_text(d.get('first_seen') or 'unknown')}`"
    )
    await ctx.send(msg)


@bot.command(name="playtime", aliases=["pt"])
@commands.cooldown(1, 2.0, commands.BucketType.user)
async def playtime_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """Shows a player's playtime per server. Syntax: ,playtime <ckey>"""
    clean = normalize_ckey(ckey)
    if not clean:
        await ctx.send("Usage: `,playtime <ckey>`")
        return

    d, err = await api_client.fetch_player(clean)
    if err:
        await ctx.send(err)
        return

    lines = [
        f"**{clean}** — total `{d.get('total_hours', 0)}h` over `{d.get('sessions_count', 0)}` sessions",
    ]
    for b in (d.get("server_breakdown") or [])[:8]:
        title = escape_code_text(b.get("title"))
        lines.append(f"> `{b.get('hours', 0):>7}h` ({b.get('percentage', 0)}%) — {title}")
    await send_long(ctx, "\n".join(lines))


@bot.command(name="compare", aliases=["cmp"])
@commands.cooldown(1, 3.0, commands.BucketType.user)
async def compare_command(ctx: commands.Context, first: str = "", second: str = "") -> None:
    """Compares two players' playtime and shared servers concurrently. Syntax: ,compare <ckey1> <ckey2>"""
    a, b = normalize_ckey(first), normalize_ckey(second)
    if not a or not b:
        await ctx.send("Usage: `,compare <ckey1> <ckey2>`")
        return

    # Fetch both player dossiers concurrently via asyncio.gather
    (da, err_a), (db_, err_b) = await asyncio.gather(
        api_client.fetch_player(a),
        api_client.fetch_player(b),
    )

    if err_a:
        await ctx.send(err_a)
        return
    if err_b:
        await ctx.send(err_b)
        return

    sa = {x["server_id"]: x for x in (da or {}).get("server_breakdown") or [] if "server_id" in x}
    sb = {x["server_id"]: x for x in (db_ or {}).get("server_breakdown") or [] if "server_id" in x}
    common = sorted(set(sa) & set(sb), key=lambda k: -(sa[k].get("seconds", 0) + sb[k].get("seconds", 0)))[:6]

    lines = [
        f"**{a}** `{(da or {}).get('total_hours', 0)}h` vs **{b}** `{(db_ or {}).get('total_hours', 0)}h`",
        f"> **Shared servers:** `{len(set(sa) & set(sb))}`",
    ]
    for k in common:
        title = escape_code_text(sa[k].get("title"))
        lines.append(f"> {title}: `{sa[k].get('hours', 0)}h` / `{sb[k].get('hours', 0)}h`")
    await send_long(ctx, "\n".join(lines))


# ============================================================================
# Server Analytics & Graph Commands
# ============================================================================

@bot.command(name="top")
@commands.cooldown(1, 2.0, commands.BucketType.user)
async def top_command(ctx: commands.Context, *, query: str = "") -> None:
    """Top players by playtime on a server. Syntax: ,top [id | name]"""
    if query.strip():
        srv, err = await api_client.resolve_server(query)
    else:
        data, err = await api_client.api_json("/api/v1/servers")
        servers = sorted((data or {}).get("servers", []), key=lambda s: -int(s.get("current_players") or 0))
        srv = servers[0] if servers else None
        err = err or (None if srv else "No active monitored servers found.")

    if err:
        await ctx.send(err)
        return

    d, err = await api_client.api_json(f"/api/v1/server/{srv['id']}")
    if err:
        await ctx.send("Server not found." if err == NOT_FOUND else err)
        return

    title = escape_code_text(d.get("title", f"#{d.get('id')}"))
    lines = [f"**Top players — [{d.get('id')}] {title}**"]
    for p in (d.get("top_players") or [])[:10]:
        ckey_str = escape_code_text(p.get("ckey"))
        lines.append(f"> `{p.get('rank'):>2}.` {ckey_str} — `{p.get('total_hours')}h`")
    if len(lines) == 1:
        lines.append("> No data yet.")
    await send_long(ctx, "\n".join(lines))


@bot.command(name="graph")
@commands.cooldown(1, 3.0, commands.BucketType.user)
async def graph_command(ctx: commands.Context, *, query: str = "") -> None:
    """True 24h online sparkline for a server. Syntax: ,graph <id | name>"""
    if not query.strip():
        await ctx.send("Usage: `,graph <id | name>`")
        return

    srv, err = await api_client.resolve_server(query)
    if err:
        await ctx.send(err)
        return

    d, err = await api_client.api_json(f"/api/v1/server/{srv['id']}")
    if err:
        await ctx.send("Server not found." if err == NOT_FOUND else err)
        return

    pts = d.get("snapshots_24h") or []
    if not pts:
        await ctx.send("No 24h history recorded yet.")
        return

    chars, stats = build_sparkline(pts, now=time.time())
    c24 = d.get("chart_24h") or {}
    title = escape_code_text(d.get("title", f"#{d.get('id')}"))
    
    await ctx.send(
        f"**[{d.get('id')}] {title}** — last 24h (24 hourly buckets, oldest → newest)\n"
        f"```\n{chars}\n```"
        f"> **Min:** `{c24.get('min_players', stats['min'])}` | **Max:** `{c24.get('max_players', stats['max'])}` | "
        f"**Avg:** `{c24.get('avg_players', stats['avg'])}` | **Now:** `{d.get('current_players', 0)}`"
    )


@bot.command(name="round")
@commands.cooldown(1, 2.0, commands.BucketType.user)
async def round_command(ctx: commands.Context, *, query: str = "") -> None:
    """Current round info for a server. Syntax: ,round <id | name>"""
    if not query.strip():
        await ctx.send("Usage: `,round <id | name>`")
        return

    srv, err = await api_client.resolve_server(query)
    if err:
        await ctx.send(err)
        return

    d, err = await api_client.api_json(f"/api/v1/server/{srv['id']}")
    if err:
        await ctx.send("Server not found." if err == NOT_FOUND else err)
        return

    title = escape_code_text(d.get("title", f"#{d.get('id')}"))
    map_name = escape_code_text(d.get("map_name") or "N/A")
    gamemode = escape_code_text(d.get("gamemode") or "N/A")
    round_dur = escape_code_text(d.get("round_duration_str") or "N/A")
    station_time = escape_code_text(d.get("station_time") or "N/A")
    admins_cnt = escape_code_text(d.get("admins_count", "N/A"))

    await ctx.send(
        f"**[{d.get('id')}] {title}**\n"
        f"> **Map:** `{map_name}` | **Mode:** `{gamemode}` | "
        f"**Round:** `{round_dur}` | **Station time:** `{station_time}`\n"
        f"> **Online:** `{d.get('current_players', 0)}` | **Admins:** `{admins_cnt}`"
    )


@bot.command(name="peak")
@commands.cooldown(1, 2.0, commands.BucketType.user)
async def peak_command(ctx: commands.Context, *, query: str = "") -> None:
    """24h peak/min/avg online for a server. Syntax: ,peak <id | name>"""
    if not query.strip():
        await ctx.send("Usage: `,peak <id | name>`")
        return

    srv, err = await api_client.resolve_server(query)
    if err:
        await ctx.send(err)
        return

    d, err = await api_client.api_json(f"/api/v1/server/{srv['id']}")
    if err:
        await ctx.send("Server not found." if err == NOT_FOUND else err)
        return

    c = d.get("chart_24h") or {}
    if not c.get("count"):
        await ctx.send("No 24h history recorded yet.")
        return

    title = escape_code_text(d.get("title", f"#{d.get('id')}"))
    await ctx.send(
        f"**[{d.get('id')}] {title}** — 24h: "
        f"**Peak** `{c.get('max_players', '?')}` | **Min** `{c.get('min_players', '?')}` | "
        f"**Avg** `{c.get('avg_players', '?')}` | **Now** `{d.get('current_players', 0)}`"
    )


# ============================================================================
# Service & Maintenance Commands
# ============================================================================

@bot.command(name="uptime")
@commands.cooldown(1, 1.0, commands.BucketType.user)
async def uptime_command(ctx: commands.Context) -> None:
    """Shows how long the selfbot has been running. Syntax: ,uptime"""
    await ctx.send(
        f"**Selfbot uptime:** `{format_age(time.time() - _started_at)}` | "
        f"**Active subscriptions:** `{len(subs.items)}`"
    )


@bot.command(name="reload")
@commands.cooldown(1, 2.0, commands.BucketType.user)
async def reload_command(ctx: commands.Context) -> None:
    """Reloads whitelist and subscriptions from disk (owner only). Syntax: ,reload"""
    if not is_owner(ctx):
        await ctx.send("Only the selfbot owner can reload.")
        return

    whitelist.load()
    subs.load()
    api_client.cache.clear()
    logger.info("Configuration reloaded by owner: %d whitelisted, %d subscriptions", len(whitelist.allowed_ids), len(subs.items))
    await ctx.send(
        f"Reloaded: whitelist `{len(whitelist.allowed_ids)}` ids, subscriptions `{len(subs.items)}`."
    )


COMMAND_HELP: Dict[str, str] = {
    "who": "`,who <ckey>` (`,w`) — PNG-карточка досье игрока",
    "find": "`,find <ckey>` (`,where`) — на каком сервере игрок сейчас",
    "seen": "`,seen <ckey>` — когда игрока видели последний раз",
    "playtime": "`,playtime <ckey>` (`,pt`) — время игры по серверам",
    "compare": "`,compare <ckey1> <ckey2>` (`,cmp`) — сравнение двух игроков",
    "servers": "`,servers` (`,hub`) — список активных серверов",
    "server": "`,server <id|имя>` (`,srv`) — сводка по серверу",
    "round": "`,round <id|имя>` — карта, режим, длительность раунда",
    "peak": "`,peak <id|имя>` — пик/мин/среднее онлайна за 24ч",
    "graph": "`,graph <id|имя>` — график онлайна за 24ч",
    "top": "`,top [id|имя]` — топ игроков сервера по времени",
    "watch": "`,watch <ckey>` — пинг, когда игрок зайдёт на сервер",
    "alert": "`,alert <id|имя> <N>` — пинг, когда онлайн сервера превысит N",
    "watchlist": "`,watchlist` (`,subs`) — ваши подписки",
    "unwatch": "`,unwatch <ckey|#id|all>` — удалить подписки",
    "status": "`,status` (`,ping`) — статус трекера Hilleye",
    "uptime": "`,uptime` — время работы бота",
    "wl": "`,wl list|add|remove` — вайтлист (изменение — только владелец)",
    "reload": "`,reload` — перечитать вайтлист и подписки (только владелец)",
}


@bot.command(name="commands", aliases=["h", "help_hilleye"])
@commands.cooldown(1, 1.0, commands.BucketType.user)
async def commands_list(ctx: commands.Context) -> None:
    """Displays available selfbot commands."""
    lines = ["**Hilleye Selfbot Commands:**"]
    registered = {c.name: c for c in bot.commands if c.name != "commands"}
    ordered = [n for n in COMMAND_HELP if n in registered] + sorted(n for n in registered if n not in COMMAND_HELP)
    for name in ordered:
        cmd = registered[name]
        desc = COMMAND_HELP.get(name) or f"`,{name}` — {(cmd.help or '').splitlines()[0] if cmd.help else ''}"
        lines.append(f"- {desc}")
    await send_long(ctx, "\n".join(lines))


# ============================================================================
# Main Entry Point
# ============================================================================

def main() -> None:
    """Validates configuration and launches selfbot."""
    if not DISCORD_TOKEN or DISCORD_TOKEN == _PLACEHOLDER_TOKEN:
        logger.critical(
            "Discord token is not configured! Set DISCORD_USER_TOKEN environment variable "
            "or create examples/token.txt containing your user token."
        )
        sys.exit(1)

    logger.info("Starting Hilleye Selfbot...")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
