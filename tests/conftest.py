"""
CyberSyn Intel // Pytest Configuration and Shared Test Fixtures
Provides reusable fixtures for Tiers 1-4:
- Event loop & async setup
- Temporary SQLite database in WAL mode
- Mock TCP Topic socket server (0x06 string, 0x2a float, timeout, disconnect)
- Mock HTTP transport for BYOND hub scraping (JSON & text fallback)
- httpx.AsyncClient with ASGITransport for FastAPI web & API testing
"""

import asyncio
import json
import struct
from typing import Any, Callable, Dict, List, Optional
import pytest
import httpx


@pytest.fixture(autouse=True)
def offline_tracker_hub(monkeypatch):
    """Tracker tests use local custom sockets; scraper tests provide their own HTTP transport."""
    from core.hub_scraper import HubResult, merge_custom_servers

    async def discover(*, custom_servers=None, **kwargs):
        return HubResult(merge_custom_servers([], custom_servers), healthy=True)

    monkeypatch.setattr("core.tracker.fetch_hub_servers", discover)


# Sample Hub Payloads for Testing
SAMPLE_HUB_JSON = [
    {
        "url": "byond://178.62.241.130:1337",
        "status": "Tau Ceti Classic",
        "players": ["honkmaster", "clown", "mime"]
    },
    {
        "url": "byond://185.100.20.5:25000",
        "status": "White Dream 1",
        "players": ["security_guard", "captain"]
    }
]

SAMPLE_HUB_TEXT = (
    'world/1\n'
    '    status = "Tau Ceti Classic - High RP Station"\n'
    '    url = "byond://178.62.241.130:1337"\n'
    '    players = list("honkmaster", "captain_jack", "security_officer")\n'
    'world/2\n'
    '    status = "White Dream 1"\n'
    '    url = "byond://185.100.20.5:25000"\n'
    '    players = list("player_one", "player_two")\n'
)


class MockTopicServer:
    """
    Simulates a live BYOND server socket responding to binary Topic queries.
    Capable of responding with string (0x06), float (0x2a), custom bytes,
    delaying (for timeout testing), or closing immediately.
    """
    def __init__(self, host: str = "127.0.0.1"):
        self.host = host
        self.port: int = 0
        self.server: Optional[asyncio.Server] = None
        self.query_history: List[bytes] = []
        self._response_bytes: Optional[bytes] = None
        self._delay: float = 0.0
        self._close_immediately: bool = False

    def set_string_response(self, text: str) -> None:
        """Sets response to 0x06 string payload with proper BYOND packet framing."""
        encoded = text.encode("utf-8") + b"\x00"
        payload = b"\x06" + encoded
        self._response_bytes = b"\x00\x83" + struct.pack(">H", len(payload)) + payload

    def set_float_response(self, value: float) -> None:
        """Sets response to 0x2a 32-bit float payload (<f)."""
        payload = b"\x2a" + struct.pack("<f", value)
        self._response_bytes = b"\x00\x83" + struct.pack(">H", len(payload)) + payload

    def set_raw_response(self, data: bytes) -> None:
        """Sets arbitrary byte response for edge cases (corrupt headers, truncated packets)."""
        self._response_bytes = data

    def set_delay(self, seconds: float) -> None:
        """Configures simulated server hang to trigger client timeouts."""
        self._delay = seconds

    def set_close_immediately(self) -> None:
        """Configures server to drop connections immediately without responding."""
        self._close_immediately = True

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self._close_immediately:
                writer.close()
                await writer.wait_closed()
                return

            data = await reader.read(1024)
            self.query_history.append(data)

            if self._delay > 0:
                await asyncio.sleep(self._delay)

            if self._response_bytes is not None:
                writer.write(self._response_bytes)
                await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle_client, self.host, 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()


@pytest.fixture
async def mock_topic_server():
    """Starts a local MockTopicServer on an ephemeral port and stops it on teardown."""
    server = MockTopicServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def mock_hub_transport():
    """
    Factory fixture providing an httpx.MockTransport simulating the BYOND hub API.
    Supports configuring custom JSON or text payloads and HTTP status codes.
    """
    def _create(
        json_data: Any = None,
        text_data: Optional[str] = None,
        json_status: int = 200,
        text_status: int = 200,
    ) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "format=json" in url_str:
                if json_status != 200:
                    return httpx.Response(json_status, text="Internal Server Error")
                payload = json_data if json_data is not None else SAMPLE_HUB_JSON
                return httpx.Response(200, json=payload)
            else:  # format=text or fallback
                if text_status != 200:
                    return httpx.Response(text_status, text="Internal Server Error")
                text_payload = text_data if text_data is not None else SAMPLE_HUB_TEXT
                return httpx.Response(200, text=text_payload)

        return httpx.MockTransport(handler)

    return _create


@pytest.fixture
def temp_db_path(tmp_path):
    """Provides a fresh temporary file path for SQLite test database."""
    return str(tmp_path / "test_tracker.db")


@pytest.fixture
async def test_db(temp_db_path):
    """
    Initializes a clean SQLite database in WAL mode using core.db if implemented.
    Ensures safe teardown and connection closure after test execution.
    """
    try:
        from core import db
        await db.init_db(temp_db_path)
    except (ImportError, AttributeError):
        pass

    yield temp_db_path

    try:
        from core import db
        await db.close_db()
    except (ImportError, AttributeError):
        pass


@pytest.fixture
async def async_client(test_db):
    """
    Provides an httpx.AsyncClient hooked directly to the FastAPI ASGI app.
    Automatically skips if web.app is not yet implemented.
    """
    try:
        from web.app import app
    except ImportError:
        pytest.skip("web.app not yet implemented")

    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
