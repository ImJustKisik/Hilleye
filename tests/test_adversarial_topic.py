"""
Adversarial Stress Test Suite for BYOND Topic Protocol (core/byond_topic.py).
Tests boundary conditions, corrupted headers, truncated payloads, float edge cases,
TCP packet fragmentation, concurrent load, and abrupt socket disconnections.
"""

import asyncio
import math
import struct
import pytest
from core.byond_topic import (
    TOPIC_MAGIC,
    TYPE_FLOAT,
    TYPE_NULL,
    TYPE_STRING,
    make_topic_packet,
    parse_topic_query_string,
    parse_topic_response,
    query_topic,
)


class TestPacketSerializationAdversarial:
    """Stress-tests make_topic_packet boundary conditions."""

    def test_empty_string(self):
        packet = make_topic_packet("")
        assert packet[:2] == TOPIC_MAGIC
        assert packet.endswith(b"?\x00")
        payload_len = struct.unpack(">H", packet[2:4])[0]
        assert payload_len == 7  # len("?") + 6

    def test_single_question_mark(self):
        packet = make_topic_packet("?")
        assert packet[:2] == TOPIC_MAGIC
        assert packet.endswith(b"?\x00")
        payload_len = struct.unpack(">H", packet[2:4])[0]
        assert payload_len == 7

    def test_multiple_question_marks(self):
        packet = make_topic_packet("???status")
        assert packet[9:-1] == b"???status"
        payload_len = struct.unpack(">H", packet[2:4])[0]
        assert payload_len == len(b"???status") + 6

    def test_unicode_and_special_chars(self):
        query = "?status=Привет_мир_🚀"
        q_bytes = query.encode("utf-8")
        packet = make_topic_packet(query)
        payload_len = struct.unpack(">H", packet[2:4])[0]
        assert payload_len == len(q_bytes) + 6
        assert packet[9:-1] == q_bytes

    def test_embedded_null_byte(self):
        query = "status\x00injected"
        packet = make_topic_packet(query)
        assert b"\x00injected" in packet

    def test_maximum_allowed_uint16_payload(self):
        query = "?" + ("A" * 65528)
        assert len(query) == 65529
        packet = make_topic_packet(query)
        payload_len = struct.unpack(">H", packet[2:4])[0]
        assert payload_len == 65535
        assert len(packet) == 4 + 65535

    def test_query_exceeding_uint16_max_raises_or_fails(self):
        huge_query = "A" * 70000
        with pytest.raises(struct.error):
            make_topic_packet(huge_query)


class TestResponseParsingAdversarial:
    """Stress-tests parse_topic_response edge cases and malformed inputs."""

    def test_corrupt_magic_headers(self):
        assert parse_topic_response(b"\x01\x83\x00\x05\x06test") is None
        assert parse_topic_response(b"\x00\x84\x00\x05\x06test") is None
        assert parse_topic_response(b"\x83\x00\x00\x05\x06test") is None
        assert parse_topic_response(b"\xFF\xFF\x00\x05\x06test") is None
        assert parse_topic_response(b"HTTP/1.1 200 OK\r\n\r\n") is None

    def test_short_and_empty_payloads(self):
        assert parse_topic_response(b"") is None
        assert parse_topic_response(b"\x00") is None
        assert parse_topic_response(b"\x00\x83") is None
        assert parse_topic_response(b"\x00\x83\x00") is None
        assert parse_topic_response(b"\x00\x83\x00\x05") is None

    def test_zero_payload_length(self):
        data = TOPIC_MAGIC + struct.pack(">H", 0) + bytes([TYPE_STRING])
        assert parse_topic_response(data) is None

    def test_unsupported_type_bytes(self):
        for bad_type in [0x01, 0x05, 0x07, 0x29, 0x2B, 0x83, 0xFE, 0xFF]:
            data = TOPIC_MAGIC + struct.pack(">H", 5) + bytes([bad_type]) + b"abcd"
            assert parse_topic_response(data) is None

    def test_null_response_type(self):
        data = TOPIC_MAGIC + struct.pack(">H", 1) + bytes([TYPE_NULL])
        assert parse_topic_response(data) is None

    def test_null_response_with_trailing_data(self):
        data = TOPIC_MAGIC + struct.pack(">H", 10) + bytes([TYPE_NULL]) + b"extra_garbage"
        assert parse_topic_response(data) is None

    def test_float_valid_values(self):
        for val in [0.0, -0.0, 1.0, -1.0, 3.14159, 1e6, -1e6, 1e-6]:
            data = TOPIC_MAGIC + struct.pack(">H", 5) + bytes([TYPE_FLOAT]) + struct.pack("<f", val)
            res = parse_topic_response(data)
            assert isinstance(res, float)
            assert math.isclose(res, val, rel_tol=1e-5, abs_tol=1e-5)

    def test_float_special_values_nan_inf(self):
        data_nan = TOPIC_MAGIC + struct.pack(">H", 5) + bytes([TYPE_FLOAT]) + struct.pack("<f", float("nan"))
        res_nan = parse_topic_response(data_nan)
        assert isinstance(res_nan, float)
        assert math.isnan(res_nan)

        data_inf = TOPIC_MAGIC + struct.pack(">H", 5) + bytes([TYPE_FLOAT]) + struct.pack("<f", float("inf"))
        res_inf = parse_topic_response(data_inf)
        assert isinstance(res_inf, float)
        assert math.isinf(res_inf) and res_inf > 0

        data_ninf = TOPIC_MAGIC + struct.pack(">H", 5) + bytes([TYPE_FLOAT]) + struct.pack("<f", float("-inf"))
        res_ninf = parse_topic_response(data_ninf)
        assert isinstance(res_ninf, float)
        assert math.isinf(res_ninf) and res_ninf < 0

    def test_float_corrupt_payload_len_less_than_five(self):
        """
        FINDING: If payload_len < 5 (e.g. payload_len = 1), a 4-byte float cannot exist.
        The parser must reject this packet. However, parse_topic_response currently ignores
        payload_len and returns the float if len(data) >= 9.
        """
        data = TOPIC_MAGIC + struct.pack(">H", 1) + bytes([TYPE_FLOAT]) + struct.pack("<f", 42.5)
        res = parse_topic_response(data)
        assert res is None, f"Expected None for corrupt float payload_len=1, but got {res}"

    def test_float_truncated_buffer(self):
        data = TOPIC_MAGIC + struct.pack(">H", 5) + bytes([TYPE_FLOAT]) + b"\x00\x00\x00"
        assert parse_topic_response(data) is None

    def test_string_truncated_payload(self):
        """
        FINDING: Header declares payload_len = 50, but only 10 bytes arrive.
        The packet is truncated and must be rejected (return None) to prevent
        corrupted data from propagating. Current code returns partial string.
        """
        data = TOPIC_MAGIC + struct.pack(">H", 50) + bytes([TYPE_STRING]) + b"partial"
        res = parse_topic_response(data)
        assert res is None, f"Expected None for truncated string payload, but got {repr(res)}"

    def test_string_with_excess_trailing_bytes(self):
        data = (
            TOPIC_MAGIC
            + struct.pack(">H", 5)
            + bytes([TYPE_STRING])
            + b"ping"
            + b"GARBAGE_TRAILING_BYTES_HERE"
        )
        res = parse_topic_response(data)
        assert res == "ping"

    def test_string_empty_payload(self):
        data = TOPIC_MAGIC + struct.pack(">H", 1) + bytes([TYPE_STRING])
        res = parse_topic_response(data)
        assert res == ""


class TestQueryStringParsingAdversarial:
    """Stress-tests parse_topic_query_string with messy, corrupted, or edge inputs."""

    def test_empty_and_whitespace(self):
        assert parse_topic_query_string("") == {}
        assert parse_topic_query_string("   ") == {}
        assert parse_topic_query_string("\n\t  \r\n") == {}

    def test_malformed_url_pairs(self):
        res = parse_topic_query_string("&&&=&=&==&key=value&&")
        assert isinstance(res, dict)
        assert res.get("key") == "value"

    def test_duplicate_keys(self):
        res = parse_topic_query_string("players=5&players=10")
        assert isinstance(res, dict)
        assert res["players"] == 10

    def test_invalid_json_fallback(self):
        res = parse_topic_query_string("{not: valid, json")
        assert res is not None

    def test_valid_json_payload(self):
        res = parse_topic_query_string('{"version": 516, "players": 42, "admins": 2}')
        assert isinstance(res, dict)
        assert res["version"] == 516
        assert res["players"] == 42
        assert res["admins"] == 2

    def test_valid_json_array(self):
        res = parse_topic_query_string('["Alice", "Bob", "Charlie"]')
        assert isinstance(res, list)
        assert res == ["Alice", "Bob", "Charlie"]

    def test_comma_separated_playerlist(self):
        res = parse_topic_query_string("Alice, Bob, Charlie, Dave")
        assert isinstance(res, list)
        assert res == ["Alice", "Bob", "Charlie", "Dave"]

    def test_comma_only_string(self):
        res = parse_topic_query_string(",,,,,")
        assert res == []

    def test_numeric_vs_non_numeric_fields(self):
        res = parse_topic_query_string("players=unknown&version=five-sixteen")
        assert isinstance(res, dict)
        assert res["players"] == "unknown"
        assert res["version"] == "five-sixteen"

    def test_playerlist_variations(self):
        res1 = parse_topic_query_string("players=1&playerlist=SoloHero")
        assert res1["playerlist"] == ["SoloHero"]

        res2 = parse_topic_query_string("players=0&playerlist=")
        assert res2["playerlist"] == []

        res3 = parse_topic_query_string("players=2&list=UserA,UserB")
        assert res3["list"] == ["UserA", "UserB"]


@pytest.mark.asyncio
class TestSocketConcurrencyAndDisconnections:
    """Stress-tests query_topic against adverse network behavior and concurrency."""

    async def test_tcp_fragmented_header(self):
        """
        FINDING: Server delivers 4-byte header in two separate TCP chunks.
        Chunk 1: 2 bytes (\\x00\\x83)
        Chunk 2: rest of the packet (length, type, payload)
        query_topic currently fails because it assumes reader.read(65536) returns >= 4 bytes.
        """
        async def handle_fragmented_client(reader, writer):
            await reader.read(1024)
            resp_body = "players=5\x00".encode("utf-8")
            full_pkt = (
                TOPIC_MAGIC
                + struct.pack(">H", len(resp_body) + 1)
                + bytes([TYPE_STRING])
                + resp_body
            )
            # Send first 2 bytes
            writer.write(full_pkt[:2])
            await writer.drain()
            # Brief delay before sending the remaining bytes
            await asyncio.sleep(0.05)
            writer.write(full_pkt[2:])
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_fragmented_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            res = await query_topic("127.0.0.1", port, query="?status", timeout=2.0)
            assert res == {"players": 5}, f"Expected {{'players': 5}} on fragmented TCP header, got {res}"
        finally:
            server.close()
            await server.wait_closed()

    async def test_abrupt_disconnect_on_connect(self):
        async def handle_client(reader, writer):
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            res = await query_topic("127.0.0.1", port, query="?status", timeout=1.0)
            assert res is None
        finally:
            server.close()
            await server.wait_closed()

    async def test_abrupt_disconnect_after_reading_query(self):
        async def handle_client(reader, writer):
            await reader.read(1024)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            res = await query_topic("127.0.0.1", port, query="?status", timeout=1.0)
            assert res is None
        finally:
            server.close()
            await server.wait_closed()

    async def test_abrupt_disconnect_mid_payload(self):
        """
        FINDING: Server sends header claiming 500 bytes, sends only 30 bytes, then disconnects.
        query_topic should recognize premature EOF and return None, rather than returning
        a truncated partial dict.
        """
        async def handle_client(reader, writer):
            await reader.read(1024)
            header = TOPIC_MAGIC + struct.pack(">H", 500) + bytes([TYPE_STRING])
            partial_payload = b"players=99&map_name=Truncated"
            writer.write(header + partial_payload)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            res = await query_topic("127.0.0.1", port, query="?status", timeout=1.0)
            assert res is None, f"Expected None on abruptly disconnected mid-payload, got {res}"
        finally:
            server.close()
            await server.wait_closed()

    async def test_server_hang_respects_timeout(self):
        async def handle_client(reader, writer):
            await asyncio.sleep(10.0)

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            res = await query_topic("127.0.0.1", port, query="?status", timeout=0.2)
            assert res is None
        finally:
            server.close()
            await server.wait_closed()

    async def test_heavy_concurrent_queries_under_semaphore(self):
        counter = 0

        async def handle_client(reader, writer):
            nonlocal counter
            counter += 1
            await reader.read(1024)
            resp_body = f"players={counter}&map=Station\x00".encode("utf-8")
            pkt = (
                TOPIC_MAGIC
                + struct.pack(">H", len(resp_body) + 1)
                + bytes([TYPE_STRING])
                + resp_body
            )
            writer.write(pkt)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        sem = asyncio.Semaphore(15)

        try:
            tasks = [
                query_topic("127.0.0.1", port, query="?status", timeout=5.0, semaphore=sem)
                for _ in range(60)
            ]
            results = await asyncio.gather(*tasks)
            assert len(results) == 60
            assert all(isinstance(r, dict) for r in results)
            assert all("players" in r for r in results)
        finally:
            server.close()
            await server.wait_closed()

    async def test_concurrent_mixed_chaos_queries(self):
        async def h_normal(r, w):
            await r.read(1024)
            resp = b"\x00\x83\x00\x0c\x06players=42\x00"
            w.write(resp)
            await w.drain()
            w.close()
            await w.wait_closed()

        async def h_hang(r, w):
            await asyncio.sleep(5.0)

        async def h_drop(r, w):
            w.close()
            await w.wait_closed()

        async def h_corrupt(r, w):
            await r.read(1024)
            w.write(b"\xDE\xAD\xBE\xEF\x00\x00")
            await w.drain()
            w.close()
            await w.wait_closed()

        s_normal = await asyncio.start_server(h_normal, "127.0.0.1", 0)
        s_hang = await asyncio.start_server(h_hang, "127.0.0.1", 0)
        s_drop = await asyncio.start_server(h_drop, "127.0.0.1", 0)
        s_corrupt = await asyncio.start_server(h_corrupt, "127.0.0.1", 0)

        p_normal = s_normal.sockets[0].getsockname()[1]
        p_hang = s_hang.sockets[0].getsockname()[1]
        p_drop = s_drop.sockets[0].getsockname()[1]
        p_corrupt = s_corrupt.sockets[0].getsockname()[1]

        sem = asyncio.Semaphore(20)

        try:
            tasks = []
            for _ in range(10):
                tasks.append(query_topic("127.0.0.1", p_normal, timeout=1.0, semaphore=sem))
                tasks.append(query_topic("127.0.0.1", p_hang, timeout=0.2, semaphore=sem))
                tasks.append(query_topic("127.0.0.1", p_drop, timeout=1.0, semaphore=sem))
                tasks.append(query_topic("127.0.0.1", p_corrupt, timeout=1.0, semaphore=sem))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert len(results) == 40
            for idx, res in enumerate(results):
                assert not isinstance(res, Exception), f"Query {idx} raised an exception: {res}"

            normal_results = [results[i] for i in range(0, 40, 4)]
            assert all(isinstance(r, dict) and r["players"] == 42 for r in normal_results)

            hang_results = [results[i] for i in range(1, 40, 4)]
            assert all(r is None for r in hang_results)

            drop_results = [results[i] for i in range(2, 40, 4)]
            assert all(r is None for r in drop_results)

            corrupt_results = [results[i] for i in range(3, 40, 4)]
            assert all(r is None for r in corrupt_results)
        finally:
            s_normal.close()
            s_hang.close()
            s_drop.close()
            s_corrupt.close()
            await asyncio.gather(
                s_normal.wait_closed(),
                s_hang.wait_closed(),
                s_drop.wait_closed(),
                s_corrupt.wait_closed(),
            )
