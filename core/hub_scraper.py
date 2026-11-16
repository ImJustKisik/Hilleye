"""BYOND Space Station 13 Hub Scraper.

Fetches server listings from the BYOND Hub with JSON support and seamless
fallback to text format (Exadv1/SpaceStation13?format=text) on HTML redirects
or JSON errors. Merges and deduplicates user-defined custom servers from configuration.
"""

import html
import asyncio
import json
import logging
from pathlib import Path
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
import httpx
from core.byond_topic import bounded_int, normalize_ckey, parse_round_duration
from core.text_encoding import decode_byond_text

logger = logging.getLogger(__name__)

DEFAULT_HUB_URL = "http://www.byond.com/games/SpaceStation13?format=json"
DEFAULT_TEXT_URL = "http://www.byond.com/games/Exadv1/SpaceStation13?format=text"
USER_AGENT = "Hilleye/1.0 (BYOND)"
MAX_HUB_BYTES = 4 * 1024 * 1024
MAX_HUB_SERVERS = 2000

# Known direct TCP endpoints for servers that publish a BYOND.world.<id> pager URL on the Hub.
# Ensures direct BYOND Topic (?status=2, ?manifest) works out-of-the-box even if config.json
# on a deployed instance has not been manually updated yet.
KNOWN_PAGER_ENDPOINTS: Dict[str, Tuple[str, int]] = {
    "byond://BYOND.world.1659081175": ("45.93.200.145", 1350),  # [RU] Celadon: SierraBay
    "byond://BYOND.world.805690456": ("45.93.200.145", 1337),   # [RU] Celadon Shiptest: Alpha
    "byond://BYOND.world.1821602746": ("45.93.200.145", 7731),  # [RU] [LRP] Celadon Skyrat
}


@dataclass
class ServerInfo:
    """Standardized representation of a tracked Space Station 13 server."""

    identifier: str
    title: str
    ip: str
    port: int
    players: List[str] = field(default_factory=list)
    player_count: int = 0
    roster_known: bool = False
    from_hub: bool = True
    trusted: bool = False
    count_known: bool = True
    roster_complete: bool = True
    map_name: Optional[str] = None
    gamemode: Optional[str] = None
    round_duration: Optional[int] = None


class HubResult(list):
    def __init__(self, servers, healthy=True, errors=0):
        super().__init__(servers)
        self.healthy = healthy
        self.errors = errors


HubServer = ServerInfo


def _extract_balanced_bold(text: str) -> Optional[Tuple[str, str]]:
    """Extracts the first balanced <b>...</b> inner text and the prefix immediately before it."""
    open_match = re.search(r"<b(?:\s+[^>]*)?>", text, re.IGNORECASE)
    if not open_match:
        return None
    prefix = text[: open_match.start()]
    pos = open_match.end()
    depth = 1
    tag_re = re.compile(r"<(/?)b(?:\s+[^>]*)?>", re.IGNORECASE)
    for m in tag_re.finditer(text, pos):
        if m.group(1):
            depth -= 1
            if depth == 0:
                return prefix, text[pos : m.start()]
        else:
            depth += 1
    return prefix, text[pos:]


def _strip_status_markup(fragment: str) -> str:
    unescaped = html.unescape(fragment)
    no_scripts = re.sub(r"<script\b[^>]*>.*?</script>", "", unescaped, flags=re.IGNORECASE | re.DOTALL)
    no_comments = re.sub(r"<!--.*?-->", "", no_scripts, flags=re.DOTALL)
    clean = re.sub(r"<[^>]+>", "", no_comments)
    clean = clean.replace(r"\[", "[").replace(r"\]", "]").replace(r"\"", '"')
    return " ".join(clean.split()).strip()


def clean_server_title(raw_status: str) -> str:
    """
    Cleans raw HTML markup from BYOND server status strings.

    Extracts primary title from the first balanced <b>...</b> section on the
    header line if present (restoring an immediately preceding '[' left outside
    the tag), unescapes HTML entities and BYOND slash escapes, and strips
    leftover tags. Falls back to the first non-empty header line if the bold
    section is empty or only on later metadata lines.
    """
    if not raw_status:
        return "Unknown Station"

    normalized = raw_status.replace(r"\r\n", "\n").replace(r"\n", "\n")
    lines = [line for line in re.split(r"<br\s*/?>|\r\n|\r|\n", normalized, flags=re.IGNORECASE) if line.strip()]
    first_line = lines[0] if lines else normalized

    if re.search(r"<b(?:\s+[^>]*)?>", first_line, re.IGNORECASE):
        bold_extracted = _extract_balanced_bold(normalized)
        if bold_extracted is not None:
            prefix, bold_inner = bold_extracted
            clean = _strip_status_markup(bold_inner)
            if clean:
                clean_prefix = _strip_status_markup(prefix)
                # Some BYOND hubs place '[' outside <b> (e.g. "\[<b>RU\] Name</b>").
                if clean_prefix.endswith("[") and "]" in clean and not clean.startswith("["):
                    clean = "[" + clean
                return clean

    for line in lines or [normalized]:
        clean = _strip_status_markup(line)
        if clean:
            return clean
    return "Unknown Station"


def parse_hub_status_metadata(raw_status: str) -> Dict[str, Any]:
    """
    Extracts fallback round metadata (map_name, gamemode, round_duration) from a BYOND Hub
    status HTML string when direct Topic telemetry is unavailable (e.g., pager-only servers).

    Supported Hub status formats:
      - Baystation / SierraBay: `playing <b>secret</b> on <b>NSV Sierra</b>`
      - TG / Monkestation / Para / CM: `Map: <b>MetaStation</b>`, `Mode: <b>Extended</b>`,
        `Round Time: <b>01:24</b>` or `Time: <b>01:24:05</b>`
    """
    if not raw_status or not isinstance(raw_status, str):
        return {}

    text = html.unescape(raw_status)
    text = text.replace(r"\[", "[").replace(r"\]", "]").replace(r"\"", '"')
    meta: Dict[str, Any] = {}

    def _clean_fragment(val: str) -> str:
        cleaned = re.sub(r"<[^>]+>", "", val)
        return " ".join(cleaned.split()).strip()[:128]

    # 1. Baystation / SierraBay / Polaris pattern: "playing <b>mode</b> on <b>map</b>"
    bay_match = re.search(
        r"playing\s+<b(?:\s+[^>]*)?>(.*?)</b>\s+on\s+<b(?:\s+[^>]*)?>(.*?)</b>",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if bay_match:
        mode_val = _clean_fragment(bay_match.group(1))
        map_val = _clean_fragment(bay_match.group(2))
        if mode_val:
            meta["gamemode"] = mode_val
        if map_val:
            meta["map_name"] = map_val

    # 2. Explicit "Map: <b>...</b>" or "Current Map: ..."
    if "map_name" not in meta:
        map_match = re.search(
            r"(?:current\s+)?map\s*:\s*(?:<b(?:\s+[^>]*)?>(.*?)</b>|([^<\r\n|;\]]+))",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if map_match:
            raw_map = map_match.group(1) if map_match.group(1) is not None else map_match.group(2)
            map_val = _clean_fragment(raw_map or "")
            if map_val and map_val.lower() not in ("unknown", "n/a", "none"):
                meta["map_name"] = map_val

    # 3. Explicit "Mode: <b>...</b>" or "Game Mode: ..."
    if "gamemode" not in meta:
        mode_match = re.search(
            r"(?:game\s+)?mode\s*:\s*(?:<b(?:\s+[^>]*)?>(.*?)</b>|([^<\r\n|;\]]+))",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if mode_match:
            raw_mode = mode_match.group(1) if mode_match.group(1) is not None else mode_match.group(2)
            mode_val = _clean_fragment(raw_mode or "")
            if mode_val and mode_val.lower() not in ("unknown", "n/a", "none"):
                meta["gamemode"] = mode_val

    # 4. Explicit "Round Time: <b>01:24</b>" / "Time: 01:24:05" / "Duration: 01:24"
    dur_match = re.search(
        r"(?:round\s+time|round\s+duration|duration|time)\s*:\s*(?:<b(?:\s+[^>]*)?>)?\s*([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)",
        text,
        re.IGNORECASE,
    )
    if dur_match:
        parsed_dur = parse_round_duration(dur_match.group(1))
        if parsed_dur is not None:
            meta["round_duration"] = parsed_dur

    return meta


def _parse_port_str(port_s: str) -> int:
    if not port_s or not port_s.isdigit() or len(port_s) > 5:
        raise ValueError("Invalid port")
    port = int(port_s)
    if port > 65535:
        raise ValueError("Invalid port")
    return port


def _parse_url_address(url_str: str) -> Tuple[str, str, int]:
    """
    Parses a BYOND url into (identifier, ip, port).
    Handles:
      - 'byond://ip:port' and 'ip:port'
      - IPv6 addresses: 'byond://[2001:db8::1]:1234', '[2001:db8::1]:1234', 'byond://[2001:db8::1]'
      - Pager addresses: 'byond://BYOND.world.X'
      - Hostnames: 'byond://game.tgstation13.org:25000'
      - Trailing slashes: 'byond://1.2.3.4:1234/'
    """
    raw = url_str.strip()
    if len(raw) > 512 or ("://" in raw and not raw.startswith("byond://")):
        raise ValueError("Invalid BYOND URL")
    addr = raw[8:] if raw.startswith("byond://") else raw
    addr = addr.rstrip("/")
    if not addr:
        raise ValueError("Invalid BYOND URL")

    # Handle bracketed IPv6 addresses
    if addr.startswith("[") and "]" in addr:
        bracket_end = addr.index("]")
        ip = addr[1:bracket_end]
        rest = addr[bracket_end + 1 :]
        if not ip:
            raise ValueError("Invalid BYOND URL")
        port = 0
        if rest:
            if not rest.startswith(":"):
                raise ValueError("Invalid BYOND URL")
            port = _parse_port_str(rest[1:])
        identifier = f"byond://[{ip}]:{port}" if port > 0 else f"byond://[{ip}]"
        return identifier, ip, port

    if ":" in addr:
        # Check if bare unbracketed IPv6 (contains multiple colons)
        if addr.count(":") > 1:
            ip = addr
            port = 0
            identifier = f"byond://[{ip}]"
        else:
            ip, _, port_s = addr.partition(":")
            if not ip:
                raise ValueError("Invalid BYOND URL")
            port = _parse_port_str(port_s)
            identifier = f"byond://{ip}:{port}" if port > 0 else f"byond://{ip}"
    else:
        ip = addr
        port = 0
        identifier = f"byond://{ip}"

    return identifier, ip, port


def _extract_byond_player_list(block: str) -> Optional[Tuple[List[str], bool]]:
    """Parse `players = list("a", "b", ...)` handling escaped quotes and ')' inside player names."""
    m = re.search(r"^\s*players\s*=\s*list\(", block, re.MULTILINE)
    if not m:
        return None
    i = m.end()
    n = len(block)
    items: List[str] = []
    complete = True
    expect_comma_or_end = False

    while i < n:
        ch = block[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == ")":
            return items, complete and all(len(p) <= 128 for p in items)
        if ch == ",":
            expect_comma_or_end = False
            i += 1
            continue
        if ch == '"' and not expect_comma_or_end:
            i += 1
            buf: List[str] = []
            closed = False
            while i < n:
                c = block[i]
                if c == "\\" and i + 1 < n:
                    buf.append(block[i + 1])
                    i += 2
                    continue
                if c == '"':
                    closed = True
                    i += 1
                    break
                buf.append(c)
                i += 1
            if not closed:
                complete = False
                break
            items.append("".join(buf))
            expect_comma_or_end = True
            continue
        complete = False
        i += 1

    return items, False


def parse_hub_text(text: str) -> HubResult:
    """
    Parses the BYOND Hub text payload into structured ServerInfo records.

    Handles 'world/<id>' blocks, status strings, scalar player counts,
    and 'players = list(...)' player lists.
    """
    servers: List[ServerInfo] = []
    errors = 0
    if not text:
        return HubResult(servers)

    # Split by world/ headers
    blocks = re.split(r"(?:^|\n)(?=world/\w+)", text)

    for block in blocks[:MAX_HUB_SERVERS + 1]:
        if not block.strip().startswith("world/"):
            continue

        url_match = re.search(r'^\s*url\s*=\s*"([^"]+)"', block, re.MULTILINE)
        if not url_match:
            continue

        raw_url = url_match.group(1).strip()
        try:
            identifier, ip, port = _parse_url_address(raw_url)
        except ValueError:
            logger.warning("Skipping invalid Hub endpoint")
            errors += 1
            continue

        status_match = re.search(r'status\s*=\s*"((?:[^"\\]|\\.)*)"', block, re.DOTALL)
        raw_status = status_match.group(1) if status_match else ""
        title = clean_server_title(raw_status)
        status_meta = parse_hub_status_metadata(raw_status)

        # Parse player list or scalar player count
        players: List[str] = []
        player_count = 0
        count_val = None
        list_complete = True

        parsed_list = _extract_byond_player_list(block)
        if parsed_list is not None:
            found, list_complete = parsed_list
            players = [p.strip() for p in found if normalize_ckey(p)]
            if not list_complete:
                errors += 1
            player_count = len(players)
        else:
            count_match = re.search(r"^\s*players\s*=\s*(\d+)", block, re.MULTILINE)
            if count_match:
                count_val = bounded_int(count_match.group(1))
                player_count = count_val or 0
                if count_val is None:
                    errors += 1

        servers.append(
            ServerInfo(
                identifier=identifier,
                title=title,
                ip=ip,
                port=port,
                players=players,
                player_count=player_count,
                roster_known=parsed_list is not None or count_val == 0,
                count_known=(parsed_list is not None and list_complete) or count_val is not None,
                roster_complete=list_complete,
                map_name=status_meta.get("map_name"),
                gamemode=status_meta.get("gamemode"),
                round_duration=status_meta.get("round_duration"),
            )
        )

    truncated = sum(block.strip().startswith("world/") for block in blocks) > MAX_HUB_SERVERS
    return HubResult(servers, errors=errors + int(truncated))


def parse_hub_json(data: Any) -> HubResult:
    """
    Parses a BYOND Hub JSON structure into structured ServerInfo records.
    Supports list-of-objects and dict-of-objects formats.
    """
    servers: List[ServerInfo] = []
    errors = 0
    if not data:
        return HubResult(servers)

    entries: List[Dict[str, Any]] = []
    if isinstance(data, list):
        entries = [e for e in data if isinstance(e, dict)]
    elif isinstance(data, dict):
        for v in data.values():
            if isinstance(v, dict):
                entries.append(v)

    for entry in entries[:MAX_HUB_SERVERS]:
        raw_url_val = entry.get("url")
        if not raw_url_val or not isinstance(raw_url_val, str):
            continue
        raw_url = raw_url_val.strip()
        if not raw_url or raw_url.lower() == "none":
            continue

        try:
            identifier, ip, port = _parse_url_address(raw_url)
        except ValueError:
            logger.warning("Skipping invalid Hub endpoint")
            errors += 1
            continue

        raw_status = entry.get("status") or entry.get("title") or ""
        title = clean_server_title(str(raw_status))
        status_meta = parse_hub_status_metadata(str(raw_status))

        players: List[str] = []
        player_count = 0
        roster_known = False
        count_known = False
        roster_complete = True

        raw_players = entry.get("players")
        if isinstance(raw_players, list):
            valid_list = all(isinstance(p, str) and len(p) <= 128 for p in raw_players)
            players = [p.strip() for p in raw_players if normalize_ckey(p)]
            player_count = len(players)
            roster_known = True
            count_known = valid_list
            roster_complete = valid_list
            if not valid_list:
                logger.warning("Invalid Hub player list elements ignored")
                errors += 1
        elif isinstance(raw_players, (int, float)):
            count = bounded_int(raw_players)
            if count is None:
                logger.warning("Skipping invalid Hub player count")
                errors += 1
                continue
            player_count = count
            roster_known = count == 0
            count_known = True
        elif "player_count" in entry and isinstance(entry["player_count"], (int, float)):
            count = bounded_int(entry["player_count"])
            if count is None:
                errors += 1
                continue
            player_count = count
            count_known = True

        servers.append(
            ServerInfo(
                identifier=identifier,
                title=title,
                ip=ip,
                port=port,
                players=players,
                player_count=player_count,
                roster_known=roster_known,
                count_known=count_known,
                roster_complete=roster_complete,
                map_name=status_meta.get("map_name"),
                gamemode=status_meta.get("gamemode"),
                round_duration=status_meta.get("round_duration"),
            )
        )

    return HubResult(servers, errors=errors + int(len(entries) > MAX_HUB_SERVERS))


def parse_pager_endpoints_payload(raw: Any) -> Dict[str, Tuple[str, int]]:
    """Parses a pager_endpoints.json payload into {identifier: (ip, port)}."""
    if isinstance(raw, dict) and isinstance(raw.get("endpoints"), dict):
        source_map = raw["endpoints"]
    elif isinstance(raw, dict):
        source_map = raw
    else:
        return {}

    parsed: Dict[str, Tuple[str, int]] = {}
    for raw_ident, target in source_map.items():
        if not isinstance(raw_ident, str) or not raw_ident.strip():
            continue
        ident = raw_ident.strip()
        if not ident.lower().startswith("byond://"):
            ident = f"byond://{ident}"

        ip = ""
        port = 0
        if isinstance(target, dict):
            ip = str(target.get("ip") or "").strip()
            port = bounded_int(target.get("port"), maximum=65535) or 0
        elif isinstance(target, (list, tuple)) and len(target) >= 2:
            ip = str(target[0] or "").strip()
            port = bounded_int(target[1], maximum=65535) or 0
        elif isinstance(target, str) and ":" in target:
            host_part, _, port_part = target.rpartition(":")
            ip = host_part.strip()
            port = bounded_int(port_part.strip(), maximum=65535) or 0

        if ip and 1 <= port <= 65535:
            parsed[ident] = (ip, port)
    return parsed


async def load_pager_endpoints(source: str, timeout: float = 5.0) -> Dict[str, Tuple[str, int]]:
    """Loads resolved BYOND.world.<id> -> (ip, port) mappings from a local file or HTTP(S) URL."""
    src = (source or "").strip()
    if not src:
        return {}
    try:
        if src.lower().startswith(("http://", "https://")):
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                resp = await client.get(src)
                resp.raise_for_status()
                if len(resp.content) > MAX_HUB_BYTES:
                    logger.warning("pager_endpoints_source response too large (%d bytes)", len(resp.content))
                    return {}
                data = resp.json()
        else:
            path = Path(src)
            if not path.is_file() and not path.is_absolute():
                project_candidate = Path(__file__).resolve().parent.parent / path
                if project_candidate.is_file():
                    path = project_candidate
            if not path.is_file():
                logger.debug("pager_endpoints_source file not found: %s", src)
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
        return parse_pager_endpoints_payload(data)
    except Exception as exc:
        logger.warning("Failed to load pager_endpoints_source (%s): %s", src, exc)
        return {}


def merge_custom_servers(
    hub_servers: List[ServerInfo],
    custom_servers: Optional[List[Dict[str, Any]]] = None,
    extra_pager_endpoints: Optional[Dict[str, Tuple[str, int]]] = None,
) -> List[ServerInfo]:
    """
    Merges custom user-defined servers with hub-discovered servers.

    Deduplicates by identifier and (ip, port). Custom servers override
    server title if specified while preserving live players discovered from the hub.
    Always deduplicates even when custom_servers is empty or None.
    Deduplicates custom servers among themselves.
    Filters non-dict items safely.
    Populates ip and port from identifier when only identifier is given.
    """
    merged: List[ServerInfo] = []
    seen_identifiers: Set[str] = set()
    seen_endpoints: Set[Tuple[str, int]] = set()

    # Index hub servers
    hub_map: Dict[str, ServerInfo] = {}
    hub_endpoint_map: Dict[Tuple[str, int], ServerInfo] = {}
    hub_pager_title_map: Dict[str, ServerInfo] = {}

    for s in hub_servers:
        if not getattr(s, "from_hub", True):
            continue
        hub_map[s.identifier] = s
        if s.port > 0 and s.ip:
            hub_endpoint_map[(s.ip, s.port)] = s
        elif s.title and s.title != "Unknown Station":
            hub_pager_title_map.setdefault(s.title.strip().lower(), s)

    # Merge custom servers first (custom servers have priority)
    if custom_servers and isinstance(custom_servers, list):
        for c in custom_servers:
            # Filter non-dict elements safely
            if not isinstance(c, dict):
                continue

            raw_ip = c.get("ip")
            ip = str(raw_ip).strip() if raw_ip is not None else ""
            if ip.lower() == "none":
                ip = ""

            raw_port = c.get("port")
            port = 0
            if raw_port is not None:
                port_str = str(raw_port).strip()
                if port_str.isdigit():
                    port = int(port_str)

            raw_ident = c.get("identifier")
            identifier = str(raw_ident).strip() if raw_ident is not None else ""
            if identifier.lower() == "none":
                identifier = ""

            # Extract ip and port from identifier if missing or port is 0
            if identifier and (not ip or port == 0):
                try:
                    _parsed_ident, parsed_ip, parsed_port = _parse_url_address(identifier)
                except ValueError:
                    logger.warning("Skipping custom server with invalid identifier")
                    continue
                if not ip and parsed_ip:
                    ip = parsed_ip
                if port == 0 and parsed_port > 0:
                    port = parsed_port

            if not identifier:
                if ip and port > 0:
                    identifier = f"byond://[{ip}]:{port}" if ":" in ip else f"byond://{ip}:{port}"
                elif ip:
                    identifier = f"byond://[{ip}]" if ":" in ip else f"byond://{ip}"
                else:
                    continue

            # Deduplicate custom servers among themselves
            if identifier in seen_identifiers:
                continue
            if port > 0 and ip and (ip, port) in seen_endpoints:
                continue

            raw_title = c.get("title")
            title = str(raw_title).strip() if raw_title is not None else ""
            if title.lower() == "none":
                title = ""

            # Check if matched in hub (by identifier, endpoint, or pager title when custom has real ip:port)
            existing = hub_map.get(identifier) or (hub_endpoint_map.get((ip, port)) if (port > 0 and ip) else None)
            if existing is None and title and port > 0 and ip:
                pager_match = hub_pager_title_map.get(title.lower())
                if pager_match is not None and pager_match.identifier not in seen_identifiers:
                    existing = pager_match
                    seen_identifiers.add(pager_match.identifier)

            players = existing.players if existing else []
            player_count = existing.player_count if existing else 0

            fallback_title = existing.title if existing else (f"Server {ip}:{port}" if port > 0 else (f"Server {ip}" if ip else identifier))
            srv = ServerInfo(
                identifier=identifier,
                title=title or fallback_title,
                ip=ip,
                port=port,
                players=players,
                player_count=player_count,
                roster_known=existing.roster_known if existing else False,
                from_hub=existing is not None,
                trusted=True,
                count_known=existing.count_known if existing else False,
                roster_complete=existing.roster_complete if existing else True,
                map_name=existing.map_name if existing else None,
                gamemode=existing.gamemode if existing else None,
                round_duration=existing.round_duration if existing else None,
            )
            merged.append(srv)
            seen_identifiers.add(identifier)
            if port > 0 and ip:
                seen_endpoints.add((ip, port))

    pager_map: Dict[str, Tuple[str, int]] = dict(KNOWN_PAGER_ENDPOINTS)
    if extra_pager_endpoints:
        pager_map.update(extra_pager_endpoints)

    # Add remaining hub servers that weren't matched by custom servers
    # Deduplicates hub servers even when custom_servers is empty or omitted
    for s in hub_servers:
        if s.identifier in seen_identifiers:
            continue
        if s.port > 0 and s.ip and (s.ip, s.port) in seen_endpoints:
            continue

        # Upgrade known pager addresses to their real TCP endpoints if not already overridden
        if s.port == 0 and s.identifier in pager_map:
            known_ip, known_port = pager_map[s.identifier]
            if (known_ip, known_port) not in seen_endpoints:
                s = ServerInfo(
                    identifier=s.identifier,
                    title=s.title,
                    ip=known_ip,
                    port=known_port,
                    players=s.players,
                    player_count=s.player_count,
                    roster_known=s.roster_known,
                    from_hub=s.from_hub,
                    trusted=(s.identifier in KNOWN_PAGER_ENDPOINTS),
                    count_known=s.count_known,
                    roster_complete=s.roster_complete,
                    map_name=s.map_name,
                    gamemode=s.gamemode,
                    round_duration=s.round_duration,
                )

        merged.append(s)
        seen_identifiers.add(s.identifier)
        if s.port > 0 and s.ip:
            seen_endpoints.add((s.ip, s.port))

    return merged


async def fetch_hub_servers(
    client: httpx.AsyncClient,
    custom_servers: Optional[List[Dict[str, Any]]] = None,
    hub_url: str = DEFAULT_HUB_URL,
    fallback_url: str = DEFAULT_TEXT_URL,
    timeout: float = 10.0,
) -> HubResult:
    """
    Fetches Space Station 13 server listings from the BYOND Hub.

    Queries the JSON endpoint with custom User-Agent 'Hilleye/1.0 (BYOND)'.
    If JSONDecodeError, HTTP status error, HTML redirect, or empty/error JSON occurs,
    automatically falls back to the text format scraper.
    Merges custom servers from configuration, deduplicating records.

    Args:
        client: httpx.AsyncClient session.
        custom_servers: Optional list of custom server dictionaries.
        hub_url: Primary hub endpoint URL (default: JSON).
        fallback_url: Fallback hub endpoint URL (default: Text).
        timeout: HTTP request timeout in seconds.

    Returns:
        Deduplicated list of ServerInfo objects.
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    servers: List[ServerInfo] = []
    healthy = False
    errors = 0

    async def read_body(url):
        async def receive():
            async with client.stream("GET", url, headers=headers, follow_redirects=True, timeout=timeout) as response:
                response.raise_for_status()
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise ValueError("Compressed Hub responses are not accepted")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_HUB_BYTES:
                        raise ValueError("Hub response exceeds size limit")
                return decode_byond_text(bytes(body), response.charset_encoding or "cp1251")
        return await asyncio.wait_for(receive(), timeout=timeout)

    try:
        body = await read_body(hub_url)
        if "<html" in body[:200].lower() or "<!doctype" in body[:200].lower():
            raise ValueError("Hub returned HTML")
        # The configured primary endpoint may itself use the BYOND text format.
        if body.lstrip().startswith("world/"):
            servers = parse_hub_text(body)
        else:
            parsed = json.loads(body)
            servers = parse_hub_json(parsed)
        if not servers:
            raise ValueError("No valid servers parsed from primary Hub")
        healthy = True
        errors += getattr(servers, "errors", 0)
    except Exception as exc:
        errors += 1
        logger.info("Primary Hub failed (%s); trying text fallback", exc)
        try:
            body = await read_body(fallback_url)
            servers = parse_hub_text(body)
            healthy = bool(servers)
            errors += getattr(servers, "errors", 0)
            if not healthy:
                errors += 1
                logger.warning("Text Hub returned no usable records")
        except Exception as exc:
            errors += 1
            logger.warning("Text Hub failed: %s", exc)

    if len(servers) > MAX_HUB_SERVERS:
        errors += 1
        healthy = False
        servers = servers[:MAX_HUB_SERVERS]
    return HubResult(merge_custom_servers(servers, custom_servers), healthy=healthy, errors=errors)
