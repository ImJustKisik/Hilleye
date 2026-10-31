"""Unit tests for BYOND Topic binary protocol serialization and deserialization."""

import asyncio
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


class TestTopicPacketConstruction:
    def test_make_topic_packet_with_question_mark(self):
        query = "?status"
        packet = make_topic_packet(query)

        # Expected: \x00\x83 + \x00\x0d + \x00*5 + ?status + \x00
        expected = b"\x00\x83\x00\x0d\x00\x00\x00\x00\x00?status\x00"
        assert packet == expected
        assert len(packet) == 17
        assert packet[:2] == TOPIC_MAGIC

    def test_make_topic_packet_prepends_question_mark(self):
        query = "status"
        packet = make_topic_packet(query)
        expected = b"\x00\x83\x00\x0d\x00\x00\x00\x00\x00?status\x00"
        assert packet == expected

    def test_make_topic_packet_length_calculation(self):
        query = "?players"
        q_bytes = query.encode("utf-8")
        packet = make_topic_packet(query)

        # Payload len should be len(q_bytes) + 6
        payload_len = struct.unpack(">H", packet[2:4])[0]
        assert payload_len == len(q_bytes) + 6
        assert packet[4:9] == b"\x00" * 5
        assert packet[9:-1] == q_bytes
        assert packet[-1:] == b"\x00"

    def test_make_topic_packet_empty(self):
        packet = make_topic_packet("")
        # Empty query becomes "?"
        assert packet.endswith(b"?\x00")
        payload_len = struct.unpack(">H", packet[2:4])[0]
        assert payload_len == 1 + 6  # len("?") + 6 = 7


class TestTopicResponseParsing:
    def test_parse_string_response(self):
        raw_text = "version=516&players=42"
        text_bytes = raw_text.encode("utf-8") + b"\x00"
        payload_len = len(text_bytes) + 1  # 1 byte for type
        data = TOPIC_MAGIC + struct.pack(">H", payload_len) + bytes([TYPE_STRING]) + text_bytes

        result = parse_topic_response(data)
        assert result == raw_text

    def test_parse_string_response_with_trailing_nulls(self):
        raw_text = "test_data"
        text_bytes = raw_text.encode("utf-8") + b"\x00\x00\x00"
        payload_len = len(text_bytes) + 1
        data = TOPIC_MAGIC + struct.pack(">H", payload_len) + bytes([TYPE_STRING]) + text_bytes

        result = parse_topic_response(data)
        assert result == raw_text

    def test_parse_float_response(self):
        test_val = 42.5
        float_bytes = struct.pack("<f", test_val)
        data = TOPIC_MAGIC + struct.pack(">H", 5) + bytes([TYPE_FLOAT]) + float_bytes

        result = parse_topic_response(data)
        assert isinstance(result, float)
        assert abs(result - test_val) < 0.0001

    def test_parse_float_response_negative(self):
        test_val = -1337.25
        float_bytes = struct.pack("<f", test_val)
        data = TOPIC_MAGIC + struct.pack(">H", 5) + bytes([TYPE_FLOAT]) + float_bytes

        result = parse_topic_response(data)
        assert isinstance(result, float)
        assert abs(result - test_val) < 0.01

    def test_parse_null_response(self):
        data = TOPIC_MAGIC + struct.pack(">H", 1) + bytes([TYPE_NULL])
        result = parse_topic_response(data)
        assert result is None

    def test_parse_invalid_magic_header(self):
        data = b"\x01\x83\x00\x05\x06test\x00"
        assert parse_topic_response(data) is None

    def test_parse_short_data(self):
        assert parse_topic_response(b"") is None
        assert parse_topic_response(b"\x00\x83") is None
        assert parse_topic_response(b"\x00\x83\x00") is None

    def test_parse_unknown_type_byte(self):
        data = TOPIC_MAGIC + struct.pack(">H", 5) + b"\x99data"
        assert parse_topic_response(data) is None


class TestQueryStringParsing:
    def test_parse_url_query_parameters(self):
        raw = "version=516&mode=secret&players=15&map_name=MetaStation"
        parsed = parse_topic_query_string(raw)
        assert isinstance(parsed, dict)
        assert parsed["version"] == 516
        assert parsed["mode"] == "secret"
        assert parsed["players"] == 15
        assert parsed["map_name"] == "MetaStation"

    def test_parse_playerlist_field(self):
        raw = "players=3&playerlist=Alice,Bob,Charlie"
        parsed = parse_topic_query_string(raw)
        assert isinstance(parsed, dict)
        assert parsed["players"] == 3
        assert parsed["playerlist"] == ["Alice", "Bob", "Charlie"]

    def test_parse_comma_separated_player_list(self):
        raw = "UserOne, UserTwo, UserThree"
        parsed = parse_topic_query_string(raw)
        assert isinstance(parsed, list)
        assert parsed == ["UserOne", "UserTwo", "UserThree"]

    def test_parse_json_string(self):
        raw = '{"players": 20, "round_duration": 4800, "status": "active"}'
        parsed = parse_topic_query_string(raw)
        assert isinstance(parsed, dict)
        assert parsed["players"] == 20
        assert parsed["round_duration"] == 4800

    def test_parse_plain_string(self):
        raw = "Server Rebooting"
        parsed = parse_topic_query_string(raw)
        assert parsed == "Server Rebooting"

    def test_parse_empty_string(self):
        assert parse_topic_query_string("") == {}
        assert parse_topic_query_string("   ") == {}


@pytest.mark.asyncio
class TestAsyncTopicClient:
    async def test_query_topic_string_response(self):
        # Spawn a mock BYOND topic server
        async def handle_client(reader, writer):
            req = await reader.read(1024)
            assert req.startswith(TOPIC_MAGIC)
            resp_body = "version=516&players=12&playerlist=Fox,Wolf\x00".encode("utf-8")
            resp_pkt = (
                TOPIC_MAGIC
                + struct.pack(">H", len(resp_body) + 1)
                + bytes([TYPE_STRING])
                + resp_body
            )
            writer.write(resp_pkt)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            res = await query_topic("127.0.0.1", port, query="?status", timeout=2.0)
            assert isinstance(res, dict)
            assert res["players"] == 12
            assert res["version"] == 516
            assert res["playerlist"] == ["Fox", "Wolf"]
        finally:
            server.close()
            await server.wait_closed()

    async def test_query_topic_float_response(self):
        async def handle_client(reader, writer):
            await reader.read(1024)
            float_bytes = struct.pack("<f", 25.5)
            resp_pkt = (
                TOPIC_MAGIC
                + struct.pack(">H", 5)
                + bytes([TYPE_FLOAT])
                + float_bytes
            )
            writer.write(resp_pkt)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            res = await query_topic("127.0.0.1", port, query="?ping", timeout=2.0)
            assert isinstance(res, float)
            assert abs(res - 25.5) < 0.001
        finally:
            server.close()
            await server.wait_closed()

    async def test_query_topic_timeout(self):
        async def handle_slow_client(reader, writer):
            await reader.read(1024)
            await asyncio.sleep(1.0)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_slow_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            # Set short timeout of 0.1s
            res = await query_topic("127.0.0.1", port, query="?status", timeout=0.1)
            assert res is None
        finally:
            server.close()
            await server.wait_closed()

    async def test_query_topic_connection_refused(self):
        # Pick an unused high port
        res = await query_topic("127.0.0.1", 59999, query="?status", timeout=0.5)
        assert res is None

    async def test_query_topic_with_semaphore(self):
        semaphore = asyncio.Semaphore(2)

        async def handle_client(reader, writer):
            await reader.read(1024)
            resp_body = "players=1\x00".encode("utf-8")
            resp_pkt = (
                TOPIC_MAGIC
                + struct.pack(">H", len(resp_body) + 1)
                + bytes([TYPE_STRING])
                + resp_body
            )
            writer.write(resp_pkt)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handle_client, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        try:
            res = await query_topic(
                "127.0.0.1", port, query="?status", timeout=2.0, semaphore=semaphore
            )
            assert isinstance(res, dict)
            assert res["players"] == 1
        finally:
            server.close()
            await server.wait_closed()

    async def test_query_topic_with_mock_topic_server_fixture(self, mock_topic_server):
        mock_topic_server.set_string_response("players=7&map_name=BoxStation&playerlist=Clown,Mime")
        res = await query_topic("127.0.0.1", mock_topic_server.port, query="?status")
        assert isinstance(res, dict)
        assert res["players"] == 7
        assert res["map_name"] == "BoxStation"
        assert res["playerlist"] == ["Clown", "Mime"]

    async def test_query_topic_float_with_mock_topic_server_fixture(self, mock_topic_server):
        mock_topic_server.set_float_response(99.5)
        res = await query_topic("127.0.0.1", mock_topic_server.port, query="?ping")
        assert isinstance(res, float)
        assert abs(res - 99.5) < 0.001

