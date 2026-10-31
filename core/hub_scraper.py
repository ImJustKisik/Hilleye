"""BYOND Space Station 13 Hub Scraper.

Fetches server listings from the BYOND Hub with JSON support and seamless
fallback to text format (Exadv1/SpaceStation13?format=text) on HTML redirects
or JSON errors. Merges and deduplicates user-defined custom servers from configuration.
"""

import html
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
import httpx

logger = logging.getLogger(__name__)

DEFAULT_HUB_URL = "http://www.byond.com/games/SpaceStation13?format=json"
DEFAULT_TEXT_URL = "http://www.byond.com/games/Exadv1/SpaceStation13?format=text"
USER_AGENT = "CyberSynIntel/1.0 (BYOND)"


@dataclass
class ServerInfo:
    """Standardized representation of a tracked Space Station 13 server."""

    identifier: str
    title: str
    ip: str
    port: int
    players: List[str] = field(default_factory=list)
    player_count: int = 0


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
            port = int(port_s) if port_s.isdigit() else 0
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
            port = int(port_s) if port_s.isdigit() else 0
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
    if not text:
        return servers

    # Split by world/ headers
    blocks = re.split(r"(?:^|\n)(?=world/\w+)", text)

    for block in blocks:
        if not block.strip().startswith("world/"):
            continue

        url_match = re.search(r'url\s*=\s*"([^"]+)"', block)
        if not url_match:
            continue

        raw_url = url_match.group(1).strip()
        identifier, ip, port = _parse_url_address(raw_url)

        status_match = re.search(r'status\s*=\s*"((?:[^"\\]|\\.)*)"', block, re.DOTALL)
        raw_status = status_match.group(1) if status_match else ""
        title = clean_server_title(raw_status)

        # Parse player list or scalar player count
        players: List[str] = []
        player_count = 0

        list_match = re.search(r"players\s*=\s*list\((.*?)\)", block, re.DOTALL)
        if list_match:
            raw_list = list_match.group(1)
            found = re.findall(r'"([^"]*)"', raw_list)
            players = [p.strip() for p in found if p.strip()]
            player_count = len(players)
        else:
            count_match = re.search(r"players\s*=\s*(\d+)", block)
            if count_match:
                player_count = int(count_match.group(1))

        servers.append(
            ServerInfo(
                identifier=identifier,
                title=title,
                ip=ip,
                port=port,
                players=players,
                player_count=player_count,
            )
        )

    return servers


def parse_hub_json(data: Any) -> List[ServerInfo]:
    """
    Parses a BYOND Hub JSON structure into structured ServerInfo records.
    Supports list-of-objects and dict-of-objects formats.
    """
    servers: List[ServerInfo] = []
    if not data:
        return servers

    entries: List[Dict[str, Any]] = []
    if isinstance(data, list):
        entries = [e for e in data if isinstance(e, dict)]
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                entries.append(v)

    for entry in entries:
        raw_url_val = entry.get("url")
        if not raw_url_val:
            continue
        raw_url = str(raw_url_val).strip()
        if not raw_url or raw_url.lower() == "none":
            continue

        identifier, ip, port = _parse_url_address(raw_url)

        raw_status = entry.get("status") or entry.get("title") or ""
        title = clean_server_title(str(raw_status))

        players: List[str] = []
        player_count = 0

        raw_players = entry.get("players")
        if isinstance(raw_players, list):
            players = [str(p).strip() for p in raw_players if str(p).strip()]
            player_count = len(players)
        elif isinstance(raw_players, (int, float)):
            player_count = int(raw_players)
        elif "player_count" in entry and isinstance(entry["player_count"], (int, float)):
            player_count = int(entry["player_count"])

        servers.append(
            ServerInfo(
                identifier=identifier,
                title=title,
                ip=ip,
                port=port,
                players=players,
                player_count=player_count,
            )
        )

    return servers


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

    for s in hub_servers:
        hub_map[s.identifier] = s
        if s.port > 0 and s.ip:
            hub_endpoint_map[(s.ip, s.port)] = s

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

            # Check if matched in hub
            existing = hub_map.get(identifier) or (hub_endpoint_map.get((ip, port)) if (port > 0 and ip) else None)
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
    headers = {"User-Agent": USER_AGENT}
    servers: List[ServerInfo] = []

    # 1. Attempt JSON Hub
    try:
        response = await client.get(
            hub_url, headers=headers, follow_redirects=True, timeout=timeout
        )
        body = response.text.strip()
        lower_body = body[:200].lower()

        # Check if the response is actually HTML instead of JSON
        if (
            response.status_code != 200
            or "<html" in lower_body
            or "<!doctype" in lower_body
        ):
            raise ValueError(f"Received HTML or status {response.status_code} from JSON hub")

        parsed_json = response.json()
        servers = parse_hub_json(parsed_json)
        # If JSON returned error dict or empty list, trigger text fallback
        if not servers:
            raise ValueError("No valid servers parsed from JSON hub")
        logger.debug("Successfully scraped %d servers from JSON hub", len(servers))

    except Exception as json_err:
        logger.info(
            "JSON hub fetch failed (%s). Falling back to text hub scraper at %s",
            json_err,
            fallback_url,
        )
        # 2. Fallback to Text Hub
        try:
            text_response = await client.get(
                fallback_url, headers=headers, follow_redirects=True, timeout=timeout
            )
            if text_response.status_code == 200:
                servers = parse_hub_text(text_response.text)
                logger.debug("Successfully scraped %d servers from text hub fallback", len(servers))
            else:
                logger.warning(
                    "Text hub fallback returned HTTP status %s", text_response.status_code
                )
        except Exception as text_err:
            logger.error("Text hub fallback also failed: %s", text_err)

    # 3. Merge custom servers from configuration
    final_servers = merge_custom_servers(servers, custom_servers)
    return final_servers
