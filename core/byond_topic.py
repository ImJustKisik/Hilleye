"""BYOND Topic Binary Protocol Client.

Implements packet formulation and response parsing for DreamDaemon world/Topic()
binary communication (0x00 0x83 framing, 16-bit big-endian payload length,
5 null padding bytes, null-terminated query; 0x06 string and 0x2a float response unpacking).
"""

import asyncio
import json
import logging
import socket
import struct
import urllib.parse
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

TOPIC_MAGIC = b"\x00\x83"
TYPE_STRING = 0x06
TYPE_FLOAT = 0x2A
TYPE_NULL = 0x00


def normalize_ckey(raw: Optional[str]) -> str:
    """Normalize a BYOND key/ckey by stripping punctuation and lowercasing."""
    if not raw:
        return ""
    import re
    return re.sub(r"[^a-z0-9]", "", str(raw).lower())



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
        pairs = urllib.parse.parse_qsl(clean_str, keep_blank_values=True)
        for key, val in pairs:
            k = key.strip()
            v = val.strip()

            if k.lower() in ("playerlist", "players_list", "player_list", "list"):
                if "," in v:
                    result[k] = [p.strip() for p in v.split(",") if p.strip()]
                elif v:
                    result[k] = [v]
                else:
                    result[k] = []
            elif k.lower() in ("players", "player_count", "version", "round_duration", "admins"):
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
