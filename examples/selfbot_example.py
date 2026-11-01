"""
Hilleye // Discord Selfbot Client with Persistent Whitelist & Telemetry Commands
(discord.py-self + aiohttp)

Features:
- Persistent JSON whitelist (`examples/whitelist.json`) allowing trusted Discord
  users (in addition to the account owner) to invoke telemetry commands.
- Commands:
  * `,who <ckey>` (aliases: `,w`, `,dossier`, `,player`) — PNG player dossier card.
  * `,servers` (aliases: `,hub`, `,online`) — Active monitored SS13 servers list.
  * `,server <id | name>` (aliases: `,srv`, `,sector`) — Detailed server status & roster (by ID or partial name).
  * `,status` (aliases: `,health`, `,ping`) — Hilleye tracker health check.
  * `,wl list` — View whitelisted Discord user IDs.
  * `,wl add <@user | user_id>` — [Owner only] Add user to whitelist.
  * `,wl remove <@user | user_id>` — [Owner only] Remove user from whitelist.
  * `,find <ckey>`, `,seen <ckey>`, `,playtime <ckey>`, `,compare <a> <b>` — Player lookups.
  * `,round`, `,peak`, `,graph`, `,top` — Per-server round info, 24h stats, sparkline, top players.
  * `,watch <ckey>` / `,alert <server> <N>` — Background subscriptions: the bot @mentions the
    subscriber in the channel where they subscribed (stored in `examples/subscriptions.json`).
    `,watchlist` lists them, `,unwatch <ckey | #id | all>` removes them.
  * `,uptime`, `,reload` — Service commands.
  * `,commands` (aliases: `,h`) — Show command reference (auto-generated).

Usage:
    pip uninstall discord.py -y
    pip install discord.py-self aiohttp
    $env:DISCORD_USER_TOKEN = "your_token_here"
    python examples/selfbot_example.py
"""

import asyncio
import io
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import aiohttp
import discord
from discord.ext import commands

# ============================================================================
# НАСТРОЙКА ТОКЕНА DISCORD:
# Впишите ваш пользовательский токен внутри кавычек ниже (или создайте файл
# examples/token.txt и вставьте токен туда):
# ============================================================================
DISCORD_TOKEN = os.getenv("DISCORD_USER_TOKEN", "YOUR_DISCORD_USER_TOKEN")

_TOKEN_FILE = Path(__file__).resolve().parent / "token.txt"
if DISCORD_TOKEN == "YOUR_DISCORD_USER_TOKEN" and _TOKEN_FILE.is_file():
    DISCORD_TOKEN = _TOKEN_FILE.read_text(encoding="utf-8").strip().strip('"').strip("'")


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


class WhitelistManager:
    """
    Manages a persistent set of Discord user IDs allowed to execute bot commands.
    The selfbot account owner is always implicitly authorized.
    """

    def __init__(self, path: Path = WHITELIST_PATH) -> None:
        self.path = Path(path)
        self.allowed_ids: Set[int] = set()
        self.env_ids: Set[int] = set()
        self.load()

        # Also load optional comma-separated IDs from HILLEYE_WHITELIST_IDS env var
        env_ids = os.getenv("HILLEYE_WHITELIST_IDS", "")
        for part in env_ids.split(","):
            uid = extract_discord_user_id(part)
            if uid is not None:
                self.env_ids.add(uid)

    def load(self) -> Set[int]:
        """Loads allowed Discord user IDs from JSON file if present."""
        if not self.path.is_file():
            return self.allowed_ids
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            ids = raw.get("allowed_user_ids", raw) if isinstance(raw, dict) else raw
            if isinstance(ids, list):
                self.allowed_ids = {int(x) for x in ids if str(x).isdigit()}
        except Exception:
            pass
        return self.allowed_ids

    def save(self) -> None:
        """Persists allowed Discord user IDs to JSON file on disk."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"allowed_user_ids": sorted(self.allowed_ids)}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def add(self, user_id: int) -> bool:
        """Adds a Discord user ID to the whitelist and saves. Returns True if newly added."""
        uid = int(user_id)
        is_new = uid not in self.allowed_ids
        self.allowed_ids.add(uid)
        self.save()
        return is_new

    def remove(self, user_id: int) -> bool:
        """Removes a Discord user ID from the whitelist and saves. Returns True if existed."""
        uid = int(user_id)
        if uid in self.allowed_ids:
            self.allowed_ids.remove(uid)
            self.save()
            return True
        return False

    def is_allowed(self, author_id: int, owner_id: Optional[int] = None) -> bool:
        """Returns True if author_id is the account owner or is in the whitelist."""
        if owner_id is not None and int(author_id) == int(owner_id):
            return True
        self.load()
        return int(author_id) in (self.allowed_ids | self.env_ids)


def normalize_ckey(raw: str) -> str:
    """Normalizes a BYOND ckey to lowercase alphanumeric characters."""
    return re.sub(r"[^a-z0-9]", "", (raw or "").lower())


def extract_discord_user_id(raw: str) -> Optional[int]:
    """
    Extracts a numeric Discord snowflake ID from raw input or mention
    (e.g. '<@123456789012345678>', '<@!123456789012345678>', or '123456789012345678').
    """
    match = re.search(r"(\d{15,22})", str(raw or "").strip())
    if not match:
        return None
    return int(match.group(1))


def match_servers_by_query(
    servers: List[Dict[str, Any]],
    query: str,
) -> List[Dict[str, Any]]:
    """
    Matches servers by ID or partial/full name (case-insensitive).
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

    # If there is an exact ID or exact title match, return only exact matches
    best_tier = scored[0][0]
    if best_tier in (0, 1):
        return [item[3] for item in scored if item[0] == best_tier]

    return [item[3] for item in scored]


class HilleyeBot(commands.Bot):
    async def get_context(self, message: discord.Message, *, cls=commands.Context) -> commands.Context:
        """
        Custom get_context ensuring that commands sent by whitelisted users
        are properly parsed and valid, even when running in selfbot mode.
        """
        ctx = await super().get_context(message, cls=cls)
        if not ctx.valid and message.content:
            prefixes = (
                self.command_prefix
                if isinstance(self.command_prefix, (tuple, list))
                else (self.command_prefix,)
            )
            matched_prefix = None
            for p in prefixes:
                if message.content.startswith(p):
                    matched_prefix = p
                    break
            if matched_prefix is not None:
                view = commands.view.StringView(message.content)
                view.skip_string(matched_prefix)
                invoker = view.get_word()
                cmd = self.all_commands.get(invoker)
                if cmd is not None:
                    ctx.prefix = matched_prefix
                    ctx.command = cmd
                    ctx.invoker = invoker
                    ctx.view = view
        return ctx


whitelist = WhitelistManager()
bot = HilleyeBot(command_prefix=",", self_bot=False, help_command=None)


def is_owner(ctx: commands.Context) -> bool:
    """Returns True if the command caller is the selfbot account owner."""
    return bool(bot.user and ctx.author.id == bot.user.id)


@bot.event
async def on_ready() -> None:
    if bot.user:
        print(
            f"[Hilleye Selfbot] Logged in as {bot.user} (ID: {bot.user.id}) | "
            f"Whitelisted users: {len(whitelist.allowed_ids)}"
        )
    start_subscription_loop()


@bot.event
async def on_message(message: discord.Message) -> None:
    """
    Custom message router allowing both the account owner and whitelisted
    Discord user IDs to invoke commands.
    """
    if message.author.bot:
        return

    owner_id = bot.user.id if bot.user else None
    if not whitelist.is_allowed(message.author.id, owner_id=owner_id):
        return

    ctx = await bot.get_context(message)
    if ctx.valid:
        print(f"[*] Command '{message.content}' from {message.author} (ID: {message.author.id})")
        await bot.invoke(ctx)


@bot.command(name="who", aliases=["w", "dossier", "player"])
async def who_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """
    Fetches a player's PNG dossier card from Hilleye and uploads it to the channel.
    Syntax: ,who <ckey>
    """
    clean_ckey = normalize_ckey(ckey)
    if not clean_ckey:
        await ctx.send("Usage: `,who <ckey>`")
        return

    card_url = f"{HILLEYE_API_BASE}/api/v1/player/{clean_ckey}/card.png"

    try:
        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(card_url) as resp:
                if resp.status == 404:
                    await ctx.send(f"Target `{clean_ckey}` not found in Hilleye telemetry.")
                    return
                if resp.status != 200:
                    await ctx.send(f"Hilleye API returned HTTP {resp.status}.")
                    return

                png_data = await resp.read()
    except Exception as exc:
        await ctx.send(f"Failed to query Hilleye API: `{exc}`")
        return

    file_attachment = discord.File(io.BytesIO(png_data), filename=f"{clean_ckey}.png")
    await ctx.send(file=file_attachment)


@bot.command(name="servers", aliases=["hub", "online"])
async def servers_command(ctx: commands.Context) -> None:
    """
    Displays top active monitored SS13 servers sorted by current player count.
    Syntax: ,servers
    """
    url = f"{HILLEYE_API_BASE}/api/v1/servers"
    try:
        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    await ctx.send(f"Hilleye API returned HTTP {resp.status}.")
                    return
                data: Dict[str, Any] = await resp.json()
    except Exception as exc:
        await ctx.send(f"Failed to query Hilleye API: `{exc}`")
        return

    servers = sorted(
        data.get("servers", []),
        key=lambda s: int(s.get("current_players") or 0),
        reverse=True,
    )[:12]

    if not servers:
        await ctx.send("No active monitored servers found.")
        return

    lines = ["```", "ID  | ONLINE | SERVER TITLE                 | MAP", "-" * 58]
    for srv in servers:
        sid = srv.get("id", "?")
        players = int(srv.get("current_players") or 0)
        title = str(srv.get("title") or "Unknown")[:28]
        map_name = str(srv.get("map_name") or "N/A")[:14]
        lines.append(f"#{sid:<2} | {players:>6} | {title:<28} | {map_name}")
    lines.append("```")
    await ctx.send("\n".join(lines))


@bot.command(name="server", aliases=["srv", "sector"])
async def server_command(ctx: commands.Context, *, query: str = "") -> None:
    """
    Displays detailed telemetry for a specific server by ID or full/partial name.
    Syntax: ,server <id | name>
    """
    raw_query = (query or "").strip()
    if not raw_query:
        await ctx.send("Usage: `,server <id | name>` (see list via `,servers`)")
        return

    srv: Optional[Dict[str, Any]] = None
    other_matches: List[Dict[str, Any]] = []
    id_match = re.fullmatch(r"#?(\d+)", raw_query)

    try:
        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # 1. If query looks like a positive numeric ID, try direct lookup first
            if id_match and int(id_match.group(1)) > 0:
                server_id = int(id_match.group(1))
                direct_url = f"{HILLEYE_API_BASE}/api/v1/server/{server_id}"
                async with session.get(direct_url) as resp:
                    if resp.status == 200:
                        srv = await resp.json()
                    elif resp.status != 404:
                        await ctx.send(f"Hilleye API returned HTTP {resp.status}.")
                        return

            # 2. If not resolved by numeric ID (or ID 404'd, e.g. searching '220'), match by name
            if srv is None:
                list_url = f"{HILLEYE_API_BASE}/api/v1/servers"
                async with session.get(list_url) as resp:
                    if resp.status != 200:
                        await ctx.send(f"Hilleye API returned HTTP {resp.status}.")
                        return
                    list_data: Dict[str, Any] = await resp.json()

                matches = match_servers_by_query(list_data.get("servers", []), raw_query)
                if not matches:
                    await ctx.send(f"Server matching `{raw_query}` not found.")
                    return

                best = matches[0]
                other_matches = matches[1:6]
                target_id = best.get("id")
                detail_url = f"{HILLEYE_API_BASE}/api/v1/server/{target_id}"
                async with session.get(detail_url) as resp:
                    if resp.status == 200:
                        srv = await resp.json()
                    elif resp.status == 404:
                        srv = best
                    else:
                        await ctx.send(f"Hilleye API returned HTTP {resp.status}.")
                        return
    except Exception as exc:
        await ctx.send(f"Failed to query Hilleye API: `{exc}`")
        return

    title = srv.get("title", f"Server #{srv.get('id', '?')}")
    players = srv.get("current_players", 0)
    map_name = srv.get("map_name") or "N/A"
    gamemode = srv.get("gamemode") or "N/A"
    round_hms = srv.get("round_duration_str") or srv.get("round_duration_hms") or "00:00:00"
    online_roster = [p.get("ckey", "") for p in srv.get("online_players", [])[:15]]
    roster_str = ", ".join(online_roster) if online_roster else "None recorded"
    if len(srv.get("online_players", [])) > 15:
        roster_str += f" (+{len(srv['online_players']) - 15} more)"

    msg = (
        f"**[{srv.get('id')}] {title}**\n"
        f"> **Online:** `{players}` | **Map:** `{map_name}` | **Mode:** `{gamemode}` | **Round:** `{round_hms}`\n"
        f"> **Active Roster:** `{roster_str}`"
    )
    if other_matches:
        others_str = ", ".join(
            f"`#{m.get('id')}` {m.get('title')} ({int(m.get('current_players') or 0)})"
            for m in other_matches
        )
        msg += f"\n> **Other matches:** {others_str}"
    await ctx.send(msg)


@bot.command(name="status", aliases=["health", "ping"])
async def status_command(ctx: commands.Context) -> None:
    """
    Checks Hilleye API and background tracker operational status.
    Syntax: ,status
    """
    url = f"{HILLEYE_API_BASE}/health"
    try:
        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status not in (200, 503):
                    await ctx.send(f"Hilleye health check HTTP {resp.status}.")
                    return
                data = await resp.json()
    except Exception as exc:
        await ctx.send(f"Hilleye API unreachable: `{exc}`")
        return

    tracker = data.get("tracker", {})
    await ctx.send(
        f"**Hilleye Status:** `{data.get('status', 'unknown').upper()}` | "
        f"**Polls:** `{tracker.get('poll_count', 0)}` | "
        f"**Tracked Servers:** `{tracker.get('servers_tracked', 0)}`"
    )


@bot.command(name="wl", aliases=["whitelist"])
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
        extra_ids = ", ".join(f"`{uid}`" for uid in sorted(whitelist.allowed_ids)) or "None"
        await ctx.send(f"**Whitelist:** {owner_str} | **Allowed IDs:** {extra_ids}")
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
        await ctx.send(f"User `{uid}` {status} whitelist (`{ len(whitelist.allowed_ids) }` total).")
    elif act in ("remove", "rm", "del", "-"):
        removed = whitelist.remove(uid)
        status = "removed from" if removed else "was not in"
        await ctx.send(f"User `{uid}` {status} whitelist (`{ len(whitelist.allowed_ids) }` total).")
    else:
        await ctx.send("Usage: `,wl list` | `,wl add <@user|id>` | `,wl remove <@user|id>`")


# ============================================================================
# Shared HTTP helper (single session + short TTL cache for read-only JSON GETs)
# ============================================================================
NOT_FOUND = "404"
CACHE_TTL_SECONDS = 10.0
_http_session: Optional[aiohttp.ClientSession] = None
_json_cache: Dict[str, Tuple[float, Any]] = {}
_started_at = time.time()


def _session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5.0))
    return _http_session


async def api_json(path: str, *, cache: bool = True) -> Tuple[Optional[Any], Optional[str]]:
    """
    GETs JSON from Hilleye. Returns (data, None) on success, or (None, error)
    where error is NOT_FOUND for HTTP 404 or a user-facing message otherwise.
    """
    now = time.monotonic()
    if cache:
        hit = _json_cache.get(path)
        if hit and now - hit[0] < CACHE_TTL_SECONDS:
            return hit[1], None
    try:
        async with _session().get(f"{HILLEYE_API_BASE}{path}") as resp:
            if resp.status == 404:
                return None, NOT_FOUND
            if resp.status not in (200, 503):
                return None, f"Hilleye API returned HTTP {resp.status}."
            data = await resp.json()
    except Exception as exc:
        return None, f"Failed to query Hilleye API: `{exc}`"
    if cache:
        _json_cache[path] = (now, data)
    return data, None


async def send_long(ctx: commands.Context, text: str) -> None:
    """Sends text, truncating to stay under Discord's 2000 character limit."""
    if len(text) > 1950:
        text = text[:1940] + "\n…(truncated)"
    await ctx.send(text)


async def resolve_server(query: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolves a server by ID or partial name. Returns (server, error_message)."""
    data, err = await api_json("/api/v1/servers")
    if err:
        return None, err
    matches = match_servers_by_query(data.get("servers", []), query)
    if not matches:
        return None, f"Server matching `{query}` not found."
    return matches[0], None


async def fetch_player(ckey: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetches a player dossier. Returns (dossier, error_message)."""
    data, err = await api_json(f"/api/v1/player/{ckey}")
    if err == NOT_FOUND:
        return None, f"Target `{ckey}` not found in Hilleye telemetry."
    return data, err


def format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


# ============================================================================
# Persistent subscriptions (watch / alert) with @mention notifications
# ============================================================================
DEFAULT_SUBS_PATH = Path(__file__).resolve().parent / "subscriptions.json"
SUBS_PATH = Path(os.getenv("HILLEYE_SUBSCRIPTIONS_FILE", str(DEFAULT_SUBS_PATH)))
SUBS_POLL_SECONDS = float(os.getenv("HILLEYE_SUBS_POLL_SECONDS", "30"))
MAX_SUBS_PER_USER = 10


class SubscriptionManager:
    """
    Stores watch/alert subscriptions as JSON. Each record contains the Discord
    user to ping and the channel where the subscription was created.

      watch: {"id", "type": "watch", "user_id", "channel_id", "ckey"}
      alert: {"id", "type": "alert", "user_id", "channel_id", "server_id",
              "server_title", "threshold"}
    """

    def __init__(self, path: Path = SUBS_PATH) -> None:
        self.path = Path(path)
        self.items: List[Dict[str, Any]] = []
        self.next_id = 1
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = [x for x in raw.get("subscriptions", []) if isinstance(x, dict)]
            self.next_id = max([int(x.get("id", 0)) for x in self.items] + [0]) + 1
        except Exception:
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"subscriptions": self.items}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def for_user(self, user_id: int) -> List[Dict[str, Any]]:
        return [s for s in self.items if int(s["user_id"]) == int(user_id)]

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
            if not (int(s["user_id"]) == int(user_id) and int(s["id"]) in ids)
        ]
        removed = before - len(self.items)
        if removed:
            self.save()
        return removed


subs = SubscriptionManager()
_watch_online: Dict[str, bool] = {}   # ckey -> last known online state
_alert_over: Dict[int, bool] = {}     # subscription id -> last known "above threshold" state
_subs_task: Optional["asyncio.Task[None]"] = None


async def notify(sub: Dict[str, Any], text: str) -> None:
    """Posts a message in the subscription's channel pinging the subscriber."""
    content = f"<@{int(sub['user_id'])}> {text}"
    try:
        channel = bot.get_channel(int(sub["channel_id"])) or await bot.fetch_channel(int(sub["channel_id"]))
        await channel.send(content)
    except Exception as exc:
        print(f"[!] Failed to notify subscription #{sub.get('id')}: {exc}")


async def check_watches() -> None:
    watches = [s for s in subs.items if s.get("type") == "watch"]
    for ckey in sorted({s["ckey"] for s in watches}):
        data, err = await api_json(f"/api/v1/player/{ckey}", cache=False)
        if err == NOT_FOUND:
            online, server = False, None
        elif err or not isinstance(data, dict):
            continue  # transient API error: keep previous state
        else:
            online = bool(data.get("is_online"))
            server = data.get("current_server")
        prev = _watch_online.get(ckey)
        _watch_online[ckey] = online
        if prev is False and online:
            for s in [w for w in watches if w["ckey"] == ckey]:
                where = f" on **{server}**" if server else ""
                await notify(s, f"🟢 `{ckey}` just came online{where}.")


async def check_alerts() -> None:
    alerts = [s for s in subs.items if s.get("type") == "alert"]
    if not alerts:
        return
    data, err = await api_json("/api/v1/servers", cache=False)
    if err or not isinstance(data, dict):
        return
    by_id = {int(s["id"]): s for s in data.get("servers", []) if str(s.get("id", "")).isdigit()}
    for sub in alerts:
        srv = by_id.get(int(sub["server_id"]))
        if srv is None:
            continue
        players = int(srv.get("current_players") or 0)
        over = players > int(sub["threshold"])
        prev = _alert_over.get(int(sub["id"]))
        _alert_over[int(sub["id"])] = over
        # Edge-triggered: ping on the transition to "above"; re-arms once it drops back.
        if over and prev is False:
            title = srv.get("title") or sub.get("server_title") or f"#{sub['server_id']}"
            await notify(sub, f"🔔 **{title}** online is `{players}` (above `{sub['threshold']}`).")


async def subscription_loop() -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await check_watches()
            await check_alerts()
        except Exception as exc:
            print(f"[!] Subscription poll error: {exc}")
        await asyncio.sleep(SUBS_POLL_SECONDS)


def start_subscription_loop() -> None:
    global _subs_task
    if _subs_task is None or _subs_task.done():
        _subs_task = asyncio.create_task(subscription_loop())


# ============================================================================
# Subscription commands
# ============================================================================
@bot.command(name="watch")
async def watch_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """Pings you when a player comes online. Syntax: ,watch <ckey>"""
    clean = normalize_ckey(ckey)
    if not clean:
        await ctx.send("Usage: `,watch <ckey>`")
        return
    mine = subs.for_user(ctx.author.id)
    if any(s["type"] == "watch" and s["ckey"] == clean for s in mine):
        await ctx.send(f"You are already watching `{clean}`.")
        return
    if len(mine) >= MAX_SUBS_PER_USER:
        await ctx.send(f"Subscription limit reached (`{MAX_SUBS_PER_USER}`). Use `,unwatch`.")
        return

    data, err = await api_json(f"/api/v1/player/{clean}", cache=False)
    if err and err != NOT_FOUND:
        await ctx.send(err)
        return
    online = bool(data.get("is_online")) if isinstance(data, dict) else False
    _watch_online[clean] = online
    rec = subs.add({
        "type": "watch", "user_id": ctx.author.id,
        "channel_id": ctx.channel.id, "ckey": clean,
    })
    note = " (currently **online** — you'll be pinged on next login)" if online else ""
    if err == NOT_FOUND:
        note = " (not seen yet — you'll be pinged when first spotted online)"
    await ctx.send(f"{ctx.author.mention} watching `{clean}` as `#{rec['id']}`{note}.")


@bot.command(name="alert")
async def alert_command(ctx: commands.Context, *args: str) -> None:
    """Pings you when server online exceeds N. Syntax: ,alert <id | name> <N>"""
    if len(args) < 2 or not args[-1].isdigit():
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

    srv, err = await resolve_server(" ".join(args[:-1]))
    if err:
        await ctx.send(err)
        return
    sid = int(srv["id"])
    if any(s["type"] == "alert" and int(s["server_id"]) == sid and int(s["threshold"]) == threshold for s in mine):
        await ctx.send("You already have this alert.")
        return

    players = int(srv.get("current_players") or 0)
    rec = subs.add({
        "type": "alert", "user_id": ctx.author.id, "channel_id": ctx.channel.id,
        "server_id": sid, "server_title": srv.get("title"), "threshold": threshold,
    })
    _alert_over[int(rec["id"])] = players > threshold
    state = "currently above — re-arms when it drops" if players > threshold else "armed"
    await ctx.send(
        f"{ctx.author.mention} alert `#{rec['id']}`: **{srv.get('title')}** "
        f"online > `{threshold}` (now `{players}`, {state})."
    )


@bot.command(name="watchlist", aliases=["subs"])
async def watchlist_command(ctx: commands.Context) -> None:
    """Shows your active subscriptions. Syntax: ,watchlist"""
    mine = subs.for_user(ctx.author.id)
    if not mine:
        await ctx.send("You have no subscriptions. Use `,watch <ckey>` or `,alert <server> <N>`.")
        return
    lines = [f"**Subscriptions for {ctx.author.mention}:**"]
    for s in mine:
        if s["type"] == "watch":
            lines.append(f"- `#{s['id']}` watch `{s['ckey']}`")
        else:
            lines.append(f"- `#{s['id']}` alert **{s.get('server_title')}** > `{s['threshold']}`")
    await send_long(ctx, "\n".join(lines))


@bot.command(name="unwatch", aliases=["unalert"])
async def unwatch_command(ctx: commands.Context, *, target: str = "") -> None:
    """Removes subscriptions. Syntax: ,unwatch <ckey | #id | all>"""
    t = target.strip()
    if not t:
        await ctx.send("Usage: `,unwatch <ckey | #id | all>` (see `,watchlist`)")
        return
    mine = subs.for_user(ctx.author.id)
    if t.lower() == "all":
        ids = {int(s["id"]) for s in mine}
    elif re.fullmatch(r"#\d+", t):
        ids = {int(t[1:])}
    else:
        clean = normalize_ckey(t)
        ids = {int(s["id"]) for s in mine if s["type"] == "watch" and s["ckey"] == clean}
    removed = subs.remove_ids(ctx.author.id, ids)
    await ctx.send(f"Removed `{removed}` subscription(s)." if removed else "No matching subscription found.")


# ============================================================================
# Player commands
# ============================================================================
@bot.command(name="find", aliases=["where"])
async def find_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """Shows which server a player is on right now. Syntax: ,find <ckey>"""
    clean = normalize_ckey(ckey)
    if not clean:
        await ctx.send("Usage: `,find <ckey>`")
        return
    d, err = await fetch_player(clean)
    if err:
        await ctx.send(err)
        return
    if d.get("is_online"):
        await ctx.send(
            f"🟢 `{clean}` is online on **{d.get('current_server')}** "
            f"(session `{d.get('current_session_duration') or '?'}`)."
        )
    else:
        await ctx.send(f"⚫ `{clean}` is offline. Last seen: `{d.get('last_seen') or 'unknown'}`.")


@bot.command(name="seen")
async def seen_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """Shows when a player was last seen. Syntax: ,seen <ckey>"""
    clean = normalize_ckey(ckey)
    if not clean:
        await ctx.send("Usage: `,seen <ckey>`")
        return
    d, err = await fetch_player(clean)
    if err:
        await ctx.send(err)
        return
    recent = d.get("recent_sessions") or []
    last_srv = recent[0].get("server_title") if recent else None
    state = f"online on **{d.get('current_server')}**" if d.get("is_online") else "offline"
    msg = (
        f"**{clean}** — {state}\n"
        f"> **Last seen:** `{d.get('last_seen') or 'unknown'}`"
        + (f" on **{last_srv}**" if last_srv else "")
        + f"\n> **First seen:** `{d.get('first_seen') or 'unknown'}`"
    )
    await ctx.send(msg)


@bot.command(name="playtime", aliases=["pt"])
async def playtime_command(ctx: commands.Context, *, ckey: str = "") -> None:
    """Shows a player's playtime per server. Syntax: ,playtime <ckey>"""
    clean = normalize_ckey(ckey)
    if not clean:
        await ctx.send("Usage: `,playtime <ckey>`")
        return
    d, err = await fetch_player(clean)
    if err:
        await ctx.send(err)
        return
    lines = [
        f"**{clean}** — total `{d.get('total_hours', 0)}h` over `{d.get('sessions_count', 0)}` sessions",
    ]
    for b in (d.get("server_breakdown") or [])[:8]:
        lines.append(f"> `{b.get('hours', 0):>7}h` ({b.get('percentage', 0)}%) — {b.get('title')}")
    await send_long(ctx, "\n".join(lines))


@bot.command(name="compare", aliases=["cmp"])
async def compare_command(ctx: commands.Context, first: str = "", second: str = "") -> None:
    """Compares two players' playtime and shared servers. Syntax: ,compare <ckey1> <ckey2>"""
    a, b = normalize_ckey(first), normalize_ckey(second)
    if not a or not b:
        await ctx.send("Usage: `,compare <ckey1> <ckey2>`")
        return
    da, err = await fetch_player(a)
    if err:
        await ctx.send(err)
        return
    db_, err = await fetch_player(b)
    if err:
        await ctx.send(err)
        return
    sa = {x["server_id"]: x for x in da.get("server_breakdown") or []}
    sb = {x["server_id"]: x for x in db_.get("server_breakdown") or []}
    common = sorted(set(sa) & set(sb), key=lambda k: -(sa[k]["seconds"] + sb[k]["seconds"]))[:6]
    lines = [
        f"**{a}** `{da.get('total_hours', 0)}h` vs **{b}** `{db_.get('total_hours', 0)}h`",
        f"> **Shared servers:** `{len(set(sa) & set(sb))}`",
    ]
    for k in common:
        lines.append(f"> {sa[k]['title']}: `{sa[k]['hours']}h` / `{sb[k]['hours']}h`")
    await send_long(ctx, "\n".join(lines))


# ============================================================================
# Server commands
# ============================================================================
SPARK = "▁▂▃▄▅▆▇█"


@bot.command(name="top")
async def top_command(ctx: commands.Context, *, query: str = "") -> None:
    """Top players by playtime on a server. Syntax: ,top [id | name]"""
    if query.strip():
        srv, err = await resolve_server(query)
    else:
        data, err = await api_json("/api/v1/servers")
        servers = sorted((data or {}).get("servers", []), key=lambda s: -int(s.get("current_players") or 0))
        srv = servers[0] if servers else None
        err = err or (None if srv else "No active monitored servers found.")
    if err:
        await ctx.send(err)
        return
    d, err = await api_json(f"/api/v1/server/{srv['id']}")
    if err:
        await ctx.send("Server not found." if err == NOT_FOUND else err)
        return
    lines = [f"**Top players — [{d.get('id')}] {d.get('title')}**"]
    for p in (d.get("top_players") or [])[:10]:
        lines.append(f"> `{p.get('rank'):>2}.` {p.get('ckey')} — `{p.get('total_hours')}h`")
    if len(lines) == 1:
        lines.append("> No data yet.")
    await send_long(ctx, "\n".join(lines))


@bot.command(name="graph")
async def graph_command(ctx: commands.Context, *, query: str = "") -> None:
    """24h online sparkline for a server. Syntax: ,graph <id | name>"""
    if not query.strip():
        await ctx.send("Usage: `,graph <id | name>`")
        return
    srv, err = await resolve_server(query)
    if err:
        await ctx.send(err)
        return
    d, err = await api_json(f"/api/v1/server/{srv['id']}")
    if err:
        await ctx.send("Server not found." if err == NOT_FOUND else err)
        return
    pts = d.get("snapshots_24h") or []
    if not pts:
        await ctx.send("No 24h history recorded yet.")
        return
    t0, t1 = pts[0]["timestamp"], pts[-1]["timestamp"]
    span = max(1.0, float(t1 - t0))
    buckets: List[Optional[int]] = [None] * 24
    for p in pts:
        idx = min(23, int((p["timestamp"] - t0) / span * 24))
        c = int(p["player_count"])
        buckets[idx] = c if buckets[idx] is None else max(buckets[idx], c)
    vals = [v for v in buckets if v is not None]
    lo, hi = min(vals), max(vals)
    chars = "".join(
        " " if v is None else SPARK[0 if hi == lo else int((v - lo) / (hi - lo) * (len(SPARK) - 1))]
        for v in buckets
    )
    c24 = d.get("chart_24h") or {}
    await ctx.send(
        f"**[{d.get('id')}] {d.get('title')}** — last 24h (oldest → newest)\n"
        f"```\n{chars}\n```"
        f"> **Min:** `{c24.get('min_players', lo)}` | **Max:** `{c24.get('max_players', hi)}` | "
        f"**Avg:** `{c24.get('avg_players', '?')}` | **Now:** `{d.get('current_players')}`"
    )


@bot.command(name="round")
async def round_command(ctx: commands.Context, *, query: str = "") -> None:
    """Current round info for a server. Syntax: ,round <id | name>"""
    if not query.strip():
        await ctx.send("Usage: `,round <id | name>`")
        return
    srv, err = await resolve_server(query)
    if err:
        await ctx.send(err)
        return
    d, err = await api_json(f"/api/v1/server/{srv['id']}")
    if err:
        await ctx.send("Server not found." if err == NOT_FOUND else err)
        return
    await ctx.send(
        f"**[{d.get('id')}] {d.get('title')}**\n"
        f"> **Map:** `{d.get('map_name') or 'N/A'}` | **Mode:** `{d.get('gamemode') or 'N/A'}` | "
        f"**Round:** `{d.get('round_duration_str') or 'N/A'}` | **Station time:** `{d.get('station_time') or 'N/A'}`\n"
        f"> **Online:** `{d.get('current_players')}` | **Admins:** `{d.get('admins_count', 'N/A')}`"
    )


@bot.command(name="peak")
async def peak_command(ctx: commands.Context, *, query: str = "") -> None:
    """24h peak/min/avg online for a server. Syntax: ,peak <id | name>"""
    if not query.strip():
        await ctx.send("Usage: `,peak <id | name>`")
        return
    srv, err = await resolve_server(query)
    if err:
        await ctx.send(err)
        return
    d, err = await api_json(f"/api/v1/server/{srv['id']}")
    if err:
        await ctx.send("Server not found." if err == NOT_FOUND else err)
        return
    c = d.get("chart_24h") or {}
    if not c.get("count"):
        await ctx.send("No 24h history recorded yet.")
        return
    await ctx.send(
        f"**[{d.get('id')}] {d.get('title')}** — 24h: "
        f"**Peak** `{c.get('max_players')}` | **Min** `{c.get('min_players')}` | "
        f"**Avg** `{c.get('avg_players')}` | **Now** `{d.get('current_players')}`"
    )


# ============================================================================
# Service commands
# ============================================================================
@bot.command(name="uptime")
async def uptime_command(ctx: commands.Context) -> None:
    """Shows how long the selfbot has been running. Syntax: ,uptime"""
    await ctx.send(
        f"**Selfbot uptime:** `{format_age(time.time() - _started_at)}` | "
        f"**Active subscriptions:** `{len(subs.items)}`"
    )


@bot.command(name="reload")
async def reload_command(ctx: commands.Context) -> None:
    """Reloads whitelist and subscriptions from disk (owner only). Syntax: ,reload"""
    if not is_owner(ctx):
        await ctx.send("Only the selfbot owner can reload.")
        return
    whitelist.load()
    subs.load()
    _json_cache.clear()
    await ctx.send(
        f"Reloaded: whitelist `{len(whitelist.allowed_ids)}` ids, subscriptions `{len(subs.items)}`."
    )


# Short descriptions for the auto-generated command list
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
async def commands_list(ctx: commands.Context) -> None:
    """Displays available selfbot commands (generated from the registered commands)."""
    lines = ["**Hilleye Selfbot Commands:**"]
    registered = {c.name: c for c in bot.commands if c.name != "commands"}
    ordered = [n for n in COMMAND_HELP if n in registered] + sorted(n for n in registered if n not in COMMAND_HELP)
    for name in ordered:
        cmd = registered[name]
        desc = COMMAND_HELP.get(name) or f"`,{name}` — {(cmd.help or '').splitlines()[0] if cmd.help else ''}"
        lines.append(f"- {desc}")
    await send_long(ctx, "\n".join(lines))


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
