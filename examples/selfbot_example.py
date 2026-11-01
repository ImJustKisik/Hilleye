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
  * `,commands` (aliases: `,h`) — Show command reference.

Usage:
    pip uninstall discord.py -y
    pip install discord.py-self aiohttp
    $env:DISCORD_USER_TOKEN = "your_token_here"
    python examples/selfbot_example.py
"""

import io
import json
import os
from pathlib import Path
import re
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


@bot.command(name="commands", aliases=["h", "help_hilleye"])
async def commands_list(ctx: commands.Context) -> None:
    """Displays available selfbot commands."""
    help_text = (
        "**Hilleye Selfbot Commands:**\n"
        "- `,who <ckey>` (`,w`) — PNG-карточка досье игрока\n"
        "- `,servers` (`,hub`) — Список активных серверов SS13\n"
        "- `,server <id | имя>` (`,srv`) — Подробная сводка по серверу (по ID или части названия)\n"
        "- `,status` (`,ping`) — Статус трекера Hilleye\n"
        "- `,wl list` — Список пользователей в вайтлисте\n"
        "- `,wl add <@user|id>` / `,wl remove <@user|id>` — Управление вайтлистом (только владелец)"
    )
    await ctx.send(help_text)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
