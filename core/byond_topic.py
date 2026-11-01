"""BYOND Topic Binary Protocol Client.

Implements packet formulation and response parsing for DreamDaemon world/Topic()
binary communication (0x00 0x83 framing, 16-bit big-endian payload length,
5 null padding bytes, null-terminated query; 0x06 string and 0x2a float response unpacking).
"""

import asyncio
import json
import logging
import re
import socket
import struct
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

TOPIC_MAGIC = b"\x00\x83"
TYPE_STRING = 0x06
TYPE_FLOAT = 0x2A
TYPE_NULL = 0x00

_LEGACY_PLAYER_KEY_RE = re.compile(r"^player(\d+)$", re.IGNORECASE)
_DURATION_COLON_RE = re.compile(r"^(\d+):(\d{1,2})(?::(\d{1,2}))?$")


def normalize_ckey(raw: Optional[str]) -> str:
    """Normalize a BYOND key/ckey by stripping punctuation and lowercasing."""
    if not raw:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(raw).lower())


def parse_round_duration(raw: Any) -> Optional[int]:
    """
    Parses round duration from various SS13 topic formats into integer seconds:
      - Integer / float seconds (e.g. 3725, "2450", "120.5")
      - Baystation / SierraBay / Polaris roundduration2text() "HH:MM" (e.g. "01:24" -> 5040s)
      - "HH:MM:SS" string (e.g. "01:24:05" -> 5045s)
      - Deciseconds if explicitly tagged or numeric strings
    Returns None if raw cannot be parsed.
    """
    if raw is None or isinstance(raw, (list, dict, bool)):
        return None
    if isinstance(raw, (int, float)):
        return max(0, int(raw))

    s = str(raw).strip()
    if not s:
        return None

    # Check "HH:MM" or "HH:MM:SS" format (Baystation / SierraBay roundduration2text() outputs "hh:mm")
    colon_match = _DURATION_COLON_RE.match(s)
    if colon_match:
        part1 = int(colon_match.group(1))
        part2 = int(colon_match.group(2))
        part3 = colon_match.group(3)
        if part3 is not None:
            return max(0, part1 * 3600 + part2 * 60 + int(part3))
        # Baystation roundduration2text() is HH:MM (hours:minutes)
        return max(0, part1 * 3600 + part2 * 60)

    # Check human-readable "1h 24m" or "45m"
    hm_match = re.match(r"^(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?$", s, re.IGNORECASE)
    if hm_match and any(hm_match.groups()):
        h = int(hm_match.group(1) or 0)
        m = int(hm_match.group(2) or 0)
        sec = int(hm_match.group(3) or 0)
        return max(0, h * 3600 + m * 60 + sec)

    try:
        return max(0, int(float(s)))
    except (ValueError, TypeError):
        return None


def _split_byond_sublist(val: str) -> List[str]:
    """
    Splits a BYOND list value encoded either as comma-separated ('a,b,c'),
    semicolon-separated ('a;b;c'), or nested list2params ('a&b&c' or 'a=1&b=2').
    """
    v = val.strip()
    if not v:
        return []
    # Nested list2params (e.g. status=2 playerlist="Alice&Bob&Charlie" or URL-encoded)
    if "&" in v and "," not in v:
        items: List[str] = []
        for token in v.split("&"):
            token = urllib.parse.unquote_plus(token).strip()
            if not token:
                continue
            if "=" in token:
                k_part, _ = token.split("=", 1)
                if k_part.strip():
                    items.append(k_part.strip())
            else:
                items.append(token)
        return items
    if "," in v:
        return [p.strip() for p in v.split(",") if p.strip()]
    if ";" in v:
        return [p.strip() for p in v.split(";") if p.strip()]
    return [v]


def parse_adminlist_value(raw: Any) -> List[Dict[str, str]]:
    """
    Parses Baystation / SierraBay `adminlist` parameter from `?status=2`.
    In SierraBay: `admins[C.key] = C.holder.rank; s["adminlist"] = list2params(admins)`
    which decodes to `"AdminOne=Game+Master&AdminTwo=Moderator"` or a list/dict.
    Returns list of `{"ckey": ..., "key": ..., "rank": ...}` dicts.
    """
    if not raw:
        return []
    result: List[Dict[str, str]] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            ck = normalize_ckey(str(k))
            if ck:
                result.append({"ckey": ck, "key": str(k).strip(), "rank": str(v or "Admin").strip()})
        return result
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                k = item.get("key") or item.get("ckey") or ""
                ck = normalize_ckey(str(k))
                if ck:
                    result.append({"ckey": ck, "key": str(k).strip(), "rank": str(item.get("rank") or "Admin").strip()})
            elif isinstance(item, str):
                if "=" in item:
                    k, r = item.split("=", 1)
                    ck = normalize_ckey(k)
                    if ck:
                        result.append({"ckey": ck, "key": k.strip(), "rank": urllib.parse.unquote_plus(r).strip() or "Admin"})
                else:
                    ck = normalize_ckey(item)
                    if ck:
                        result.append({"ckey": ck, "key": item.strip(), "rank": "Admin"})
        return result
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if "&" in s or "=" in s:
            for k, v in urllib.parse.parse_qsl(s, keep_blank_values=True):
                ck = normalize_ckey(k)
                if ck:
                    result.append({"ckey": ck, "key": k.strip(), "rank": v.strip() or "Admin"})
        else:
            for item in _split_byond_sublist(s):
                ck = normalize_ckey(item)
                if ck:
                    result.append({"ckey": ck, "key": item.strip(), "rank": "Admin"})
    return result


def parse_manifest_response(raw: Union[str, Dict[str, Any], None]) -> Dict[str, List[Dict[str, str]]]:
    """
    Parses a Baystation / SierraBay `?manifest` World Topic response.
    In SierraBay (`world.dm`):
      `positions[dept][person["name"]] = person["rank"]`
      `for(var/k in positions) positions[k] = list2params(positions[k])`
      `return list2params(positions)`
    Example payload:
      `command=Ivan+Petrov=Captain&sec=John+Doe=Security+Officer&eng=Alice=Chief+Engineer`
    Returns a dict mapping department key (e.g. 'command', 'sec', 'eng', 'med', 'sci', 'car', 'civ', 'bot')
    to a list of `{"name": ..., "rank": ...}` dictionaries.
    """
    if not raw:
        return {}

    dept_dict: Dict[str, Any] = {}
    if isinstance(raw, str):
        s = raw.strip()
        if s.startswith("?"):
            s = s[1:]
        if not s or s.lower().startswith("throttled") or "=" not in s:
            return {}
        for k, v in urllib.parse.parse_qsl(s, keep_blank_values=True):
            dept_key = k.strip().lower()
            if dept_key and v:
                dept_dict[dept_key] = v
    elif isinstance(raw, dict):
        dept_dict = raw
    else:
        return {}

    manifest: Dict[str, List[Dict[str, str]]] = {}
    for dept, encoded_members in dept_dict.items():
        dept_name = str(dept).strip().lower()
        if not dept_name or dept_name in ("raw", "status", "throttled"):
            continue
        entries: List[Dict[str, str]] = []
        if isinstance(encoded_members, str):
            for char_name, job_rank in urllib.parse.parse_qsl(encoded_members, keep_blank_values=True):
                c_name = char_name.strip()
                j_rank = job_rank.strip() or "Crew"
                if c_name:
                    entries.append({"name": c_name, "rank": j_rank})
        elif isinstance(encoded_members, dict):
            for char_name, job_rank in encoded_members.items():
                c_name = str(char_name).strip()
                j_rank = str(job_rank or "Crew").strip()
                if c_name:
                    entries.append({"name": c_name, "rank": j_rank})
        elif isinstance(encoded_members, list):
            for item in encoded_members:
                if isinstance(item, dict) and item.get("name"):
                    entries.append(
                        {
                            "name": str(item["name"]).strip(),
                            "rank": str(item.get("rank") or "Crew").strip(),
                        }
                    )
                elif isinstance(item, str) and "=" in item:
                    c_name, j_rank = item.split("=", 1)
                    if c_name.strip():
                        entries.append(
                            {
                                "name": urllib.parse.unquote_plus(c_name).strip(),
                                "rank": urllib.parse.unquote_plus(j_rank).strip() or "Crew",
                            }
                        )
        if entries:
            manifest[dept_name] = entries

    return manifest



def make_topic_packet(query: str) -> bytes:
    """
    Constructs a binary BYOND Topic request packet.

    Framing format:
    [0x00][0x83][2-byte BE Payload Len][0x00 * 5][Query String][0x00]

    Payload length = len(query_bytes) + 6
      (5 null padding bytes + query bytes + 1 null terminator)

    Args:
        query: Query string (e.g. "?status" or "status"). If query does not
               start with '?', it is automatically prepended.

    Returns:
        Full binary packet ready to transmit over TCP socket.
    """
    if not query.startswith("?"):
        query = f"?{query}"
    q_bytes = query.encode("utf-8")
    payload_len = len(q_bytes) + 6
    return (
        TOPIC_MAGIC
        + struct.pack(">H", payload_len)
        + b"\x00" * 5
        + q_bytes
        + b"\x00"
    )


def parse_topic_response(data: bytes) -> Union[str, float, None]:
    """
    Parses a raw binary response packet from a BYOND server.

    Header format:
    [0x00][0x83][2-byte BE Len][Type Byte][Payload...]

    Supported Type Bytes:
      - 0x06: ASCII / UTF-8 string (trailing null bytes stripped)
      - 0x2A: IEEE-754 32-bit single precision little-endian float (<f)
      - 0x00: Null / None response

    Args:
        data: Raw bytes received from the TCP connection.

    Returns:
        Unpacked string, float, or None if invalid/null.
    """
    if not data or len(data) < 5:
        return None

    if data[:2] != TOPIC_MAGIC:
        return None

    try:
        payload_len = struct.unpack(">H", data[2:4])[0]
        if payload_len < 1:
            return None

        type_byte = data[4]
        if type_byte == TYPE_STRING:
            if len(data) < 4 + payload_len:
                return None
            raw_payload = data[5 : 4 + payload_len]
            clean_bytes = raw_payload.rstrip(b"\x00")
            return clean_bytes.decode("utf-8", errors="replace")

        elif type_byte == TYPE_FLOAT:
            if payload_len < 5 or len(data) < 4 + payload_len:
                return None
            return struct.unpack("<f", data[5:9])[0]

        elif type_byte == TYPE_NULL:
            return None

        return None
    except Exception as exc:
        logger.debug("Failed to unpack topic response packet: %s", exc)
        return None


def parse_topic_query_string(raw: str) -> Union[Dict[str, Any], List[str], str]:
    """
    Parses a decoded string response from a BYOND server.

    Handles:
      - URL-encoded key-value parameter strings (e.g. 'version=516&players=42&playerlist=a,b')
      - Raw JSON objects or arrays
      - Comma-separated player lists (e.g. 'user1, user2, user3')
      - Plain text messages

    Args:
        raw: String extracted from 0x06 payload.

    Returns:
        Parsed dict, list of strings, or raw string.
    """
    trimmed = raw.strip()
    if not trimmed:
        return {}

    # Check for JSON payloads
    if (trimmed.startswith("{") and trimmed.endswith("}")) or (
        trimmed.startswith("[") and trimmed.endswith("]")
    ):
        try:
            return json.loads(trimmed)
        except Exception:
            pass

    # Strip optional leading '?'
    clean_str = trimmed[1:] if trimmed.startswith("?") else trimmed

    # If key-value pairs are present
    if "=" in clean_str:
        result: Dict[str, Any] = {}
        legacy_players: List[Tuple[int, str]] = []
        pairs = urllib.parse.parse_qsl(clean_str, keep_blank_values=True)
        for key, val in pairs:
            k = key.strip()
            v = val.strip()
            k_low = k.lower()

            legacy_match = _LEGACY_PLAYER_KEY_RE.match(k)
            if legacy_match and v:
                legacy_players.append((int(legacy_match.group(1)), v))

            if k_low in ("playerlist", "players_list", "player_list", "list"):
                result[k] = _split_byond_sublist(v) if v else []
            elif k_low == "adminlist":
                result[k] = parse_adminlist_value(v)
            elif k_low in ("players", "player_count", "version", "round_duration", "admins", "active_players"):
                if v.isdigit():
                    result[k] = int(v)
                elif "," in v:
                    result[k] = [p.strip() for p in v.split(",") if p.strip()]
                else:
                    result[k] = v
            else:
                if v.isdigit():
                    result[k] = int(v)
                elif "," in v and not v.startswith("{"):
                    result[k] = [p.strip() for p in v.split(",") if p.strip()]
                else:
                    result[k] = v

        # Synthesize playerlist from Baystation / SierraBay / TG legacy player0..playerN keys
        if legacy_players and not result.get("playerlist"):
            legacy_players.sort(key=lambda item: item[0])
            result["playerlist"] = [p_name for _, p_name in legacy_players if p_name]

        return result

    # If comma-separated list of items (e.g. who / player list response)
    if "," in clean_str:
        items = [item.strip() for item in clean_str.split(",") if item.strip()]
        return items

    return trimmed


async def query_topic(
    ip: str,
    port: int,
    query: str = "?status",
    timeout: float = 3.0,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> Union[Dict[str, Any], float, None]:
    """
    Connects asynchronously to a BYOND server, sends a Topic query packet,
    and returns the unpacked response.

    Features:
      - Strict socket timeout handling
      - Concurrency control via asyncio.Semaphore
      - Guaranteed writer.close() / wait_closed() cleanup
      - Complete error shielding (never raises unhandled network exceptions)

    Args:
        ip: Target server IP or hostname.
        port: Target server port.
        query: Query string to send (default: "?status").
        timeout: Socket operation timeout in seconds (default: 3.0).
        semaphore: Optional semaphore to throttle concurrent connections.

    Returns:
        Parsed dict (for string responses), float (for float responses), or None on error/timeout.
    """
    async def _execute_query() -> Optional[bytes]:
        reader, writer = await asyncio.open_connection(ip, port)
        try:
            packet = make_topic_packet(query)
            writer.write(packet)
            await writer.drain()

            data = b""
            # Accumulate at least 4 bytes for header: TOPIC_MAGIC (2) + payload_len (2)
            while len(data) < 4:
                chunk = await reader.read(65536)
                if not chunk:
                    return None
                data += chunk
                # Quick-abort if magic bytes do not match
                if len(data) >= 2 and data[:2] != TOPIC_MAGIC:
                    return None

            if data[:2] != TOPIC_MAGIC:
                return None

            payload_len = struct.unpack(">H", data[2:4])[0]
            if payload_len < 1:
                return None
            expected_total = 4 + payload_len

            # Accumulate remaining payload bytes until expected_total
            while len(data) < expected_total:
                chunk = await reader.read(expected_total - len(data))
                if not chunk:
                    return None
                data += chunk

            return data
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _gated_query() -> Optional[bytes]:
        if semaphore is not None:
            async with semaphore:
                return await _execute_query()
        return await _execute_query()

    try:
        raw_bytes = await asyncio.wait_for(_gated_query(), timeout=timeout)
    except (
        asyncio.TimeoutError,
        ConnectionRefusedError,
        ConnectionResetError,
        OSError,
        socket.gaierror,
    ):
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("Unexpected error during query_topic to %s:%s: %s", ip, port, exc)
        return None

    if not raw_bytes:
        return None

    unpacked = parse_topic_response(raw_bytes)
    if unpacked is None:
        return None

    if isinstance(unpacked, float):
        return unpacked

    if isinstance(unpacked, str):
        parsed_str = parse_topic_query_string(unpacked)
        if isinstance(parsed_str, dict):
            return parsed_str
        elif isinstance(parsed_str, list):
            return {"players": parsed_str, "playerlist": parsed_str}
        else:
            return {"raw": parsed_str}

    return None
