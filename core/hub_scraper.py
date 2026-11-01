"""BYOND Space Station 13 Hub Scraper.

Fetches server listings from the BYOND Hub with JSON support and seamless
fallback to text format (Exadv1/SpaceStation13?format=text) on HTML redirects
or JSON errors. Merges and deduplicates user-defined custom servers from configuration.
"""

import html
import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
import httpx
from core.byond_topic import bounded_int, normalize_ckey, parse_round_duration

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


def clean_server_title(raw_status: str) -> str:
    """
    Cleans raw HTML markup from BYOND server status strings.

    Extracts primary title from <b>...</b> tags if present, unescapes
    HTML entities, unescapes BYOND slash escapes, and strips leftover tags.
    Handles <b style="..."> styled tags properly without leaking adjacent text.
    """
    if not raw_status:
        return "Unknown Station"

    # Extract bold section if present as primary station title (supports HTML attributes)
    bold_match = re.search(r"<b(?:\s+[^>]*)?>(.*?)</b>", raw_status, re.IGNORECASE | re.DOTALL)
    candidate = bold_match.group(1) if bold_match else raw_status

    # Unescape HTML entities
    unescaped = html.unescape(candidate)

    # Remove all HTML tags
    clean = re.sub(r"<[^>]+>", "", unescaped)

    # Remove BYOND backslash escapes like \[ and \]
    clean = clean.replace(r"\[", "[").replace(r"\]", "]").replace(r"\"", '"')

    # Normalize whitespace
    clean = " ".join(clean.split()).strip()
    return clean or "Unknown Station"


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

    # Handle bracketed IPv6 addresses
    if addr.startswith("[") and "]" in addr:
        bracket_end = addr.index("]")
        ip = addr[1:bracket_end]
        rest = addr[bracket_end + 1 :]
        port = 0
        if rest.startswith(":"):
            port_s = rest[1:]
            port = int(port_s) if port_s.isdigit() and len(port_s) <= 5 else 0
        if port > 65535:
            raise ValueError("Invalid port")
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
            port = int(port_s) if port_s.isdigit() and len(port_s) <= 5 else 0
            if port > 65535:
                raise ValueError("Invalid port")
            identifier = f"byond://{ip}:{port}" if port > 0 else f"byond://{ip}"
    else:
        ip = addr
        port = 0
        identifier = f"byond://{ip}" if not raw.startswith("byond://") else raw

    return identifier, ip, port


def parse_hub_text(text: str) -> List[ServerInfo]:
    """
    Parses the BYOND Hub text payload into structured ServerInfo records.

    Handles 'world/<id>' blocks, status strings, scalar player counts,
    and 'players = list(...)' player lists.
    """
    servers: List[ServerInfo] = []
    errors = 0
    if not text:
        return servers

    # Split by world/ headers
    blocks = re.split(r"(?:^|\n)(?=world/\w+)", text)

    for block in blocks[:MAX_HUB_SERVERS + 1]:
        if not block.strip().startswith("world/"):
            continue

        url_match = re.search(r'url\s*=\s*"([^"]+)"', block)
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

        list_match = re.search(r"players\s*=\s*list\((.*?)\)", block, re.DOTALL)
        if list_match:
            raw_list = list_match.group(1)
            found = re.findall(r'"([^"]*)"', raw_list)
            players = [p.strip() for p in found if normalize_ckey(p)]
            remaining = re.sub(r'"[^"]*"', "", raw_list)
            list_complete = not remaining.strip(" ,\t\r\n") and all(len(p) <= 128 for p in found)
            if not list_complete:
                errors += 1
            player_count = len(players)
        else:
            count_match = re.search(r"players\s*=\s*(\d+)", block)
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
                roster_known=list_match is not None or count_val == 0,
                count_known=(list_match is not None and list_complete) or count_val is not None,
                roster_complete=list_complete,
                map_name=status_meta.get("map_name"),
                gamemode=status_meta.get("gamemode"),
                round_duration=status_meta.get("round_duration"),
            )
        )

    truncated = sum(block.strip().startswith("world/") for block in blocks) > MAX_HUB_SERVERS
    return HubResult(servers, errors=errors + int(truncated))


def parse_hub_json(data: Any) -> List[ServerInfo]:
    """
    Parses a BYOND Hub JSON structure into structured ServerInfo records.
    Supports list-of-objects and dict-of-objects formats.
    """
    servers: List[ServerInfo] = []
    errors = 0
    if not data:
        return servers

    entries: List[Dict[str, Any]] = []
    if isinstance(data, list):
        entries = [e for e in data if isinstance(e, dict)]
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                entries.append(v)

    for entry in entries[:MAX_HUB_SERVERS]:
        raw_url_val = entry.get("url")
        if not raw_url_val:
            continue
        if not isinstance(raw_url_val, str):
            continue
        raw_url = raw_url_val.strip()
        if not raw_url or raw_url.lower() == "none":
            continue

        try:
            identifier, ip, port = _parse_url_address(raw_url)
        except ValueError:
            logger.warning("Skipping invalid Hub endpoint")
            continue

        raw_status = entry.get("status") or entry.get("title") or ""
        title = clean_server_title(str(raw_status))
        status_meta = parse_hub_status_metadata(str(raw_status))

        players: List[str] = []
        player_count = 0

        raw_players = entry.get("players")
        if isinstance(raw_players, list):
            players = [p.strip() for p in raw_players if normalize_ckey(p)]
            player_count = len(players)
            if any(not isinstance(p, str) or len(p) > 128 for p in raw_players):
                logger.warning("Invalid Hub player list elements ignored")
                errors += 1
        elif isinstance(raw_players, (int, float)):
            count = bounded_int(raw_players)
            if count is None:
                logger.warning("Skipping invalid Hub player count")
                errors += 1
                continue
            player_count = count
        elif "player_count" in entry and isinstance(entry["player_count"], (int, float)):
            count = bounded_int(entry["player_count"])
            if count is None:
                errors += 1
                continue
            player_count = count

        servers.append(
            ServerInfo(
                identifier=identifier,
                title=title,
                ip=ip,
                port=port,
                players=players,
                player_count=player_count,
                roster_known=isinstance(raw_players, list) or bounded_int(raw_players) == 0,
                count_known=(isinstance(raw_players, list) and all(isinstance(p, str) and len(p) <= 128 for p in raw_players)) or bounded_int(raw_players) is not None or bounded_int(entry.get("player_count")) is not None,
                roster_complete=not isinstance(raw_players, list) or all(isinstance(p, str) and len(p) <= 128 for p in raw_players),
                map_name=status_meta.get("map_name"),
                gamemode=status_meta.get("gamemode"),
                round_duration=status_meta.get("round_duration"),
            )
        )

    return HubResult(servers, errors=errors + int(len(entries) > MAX_HUB_SERVERS))


def merge_custom_servers(
    hub_servers: List[ServerInfo], custom_servers: Optional[List[Dict[str, Any]]] = None
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
                parsed_ident, parsed_ip, parsed_port = _parse_url_address(identifier)
                if not ip and parsed_ip:
                    ip = parsed_ip
                if port == 0 and parsed_port > 0:
                    port = parsed_port
                if not identifier:
                    identifier = parsed_ident

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

    # Add remaining hub servers that weren't matched by custom servers
    # Deduplicates hub servers even when custom_servers is empty or omitted
    for s in hub_servers:
        if s.identifier in seen_identifiers:
            continue
        if s.port > 0 and s.ip and (s.ip, s.port) in seen_endpoints:
            continue

        # Upgrade known pager addresses to their real TCP endpoints if not already overridden
        if s.port == 0 and s.identifier in KNOWN_PAGER_ENDPOINTS:
            known_ip, known_port = KNOWN_PAGER_ENDPOINTS[s.identifier]
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
                    trusted=True,
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
) -> List[ServerInfo]:
    """
    Fetches Space Station 13 server listings from the BYOND Hub.

    Queries the JSON endpoint with custom User-Agent 'CyberSynIntel/1.0 (BYOND)'.
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
                return body.decode("utf-8", errors="replace")
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
