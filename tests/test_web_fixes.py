"""Regression coverage for web availability, HTML errors and legacy BYOND text."""

from html.parser import HTMLParser
import json
import struct
from urllib.parse import quote
from unittest.mock import AsyncMock

import httpx
import pytest

from core import db
from core.byond_topic import parse_manifest_response, parse_topic_query_string, parse_topic_response
from core.hub_scraper import fetch_hub_servers
from core.text_encoding import decode_byond_text


@pytest.mark.parametrize("path", ["/server/abc", "/api/v1/server/abc"])
async def test_validation_content_negotiation(async_client, path):
    html = await async_client.get(path, headers={"Accept": "text/html"})
    assert html.status_code == 422
    assert html.headers["content-type"].startswith("text/html")
    assert "Hilleye" in html.text and 'href="/"' in html.text
    assert "Invalid request" in html.text
    for accept in ("application/json", "*/*"):
        response = await async_client.get(path, headers={"Accept": accept})
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["path", "server_id"]


@pytest.mark.parametrize("path", ["/", "/server/abc", "/api/v1/player/unknown", "/static/css/style.css", "/static/js/htmx.min.js"])
async def test_response_security_and_static_cache_headers(async_client, path):
    response = await async_client.get(path)
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    if path.startswith("/static/"):
        assert response.status_code == 200
        expected = "public, max-age=86400, stale-while-revalidate=3600"
        assert response.headers["Cache-Control"] == expected
        cached = await async_client.get(path, headers={"If-None-Match": response.headers["etag"]})
        assert cached.status_code == 304
        assert cached.headers["Cache-Control"] == expected


async def test_missing_static_files_are_not_cached(async_client):
    response = await async_client.get("/static/missing.css")
    assert response.status_code == 404
    assert "cache-control" not in response.headers


@pytest.mark.parametrize("failure", [RuntimeError("database unavailable"), TimeoutError()])
async def test_database_failure_keeps_readiness_unavailable(async_client, monkeypatch, failure):
    monkeypatch.setattr(db, "get_collector_state", AsyncMock(side_effect=failure))
    for endpoint in ("/health", "/ready"):
        response = await async_client.get(endpoint)
        assert response.status_code == 503
        assert response.json()["ready"] is False
    assert (await async_client.get("/live")).status_code == 200


class DashboardParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.search = None
        self.server_links = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get("id") == "ckey-input":
            self.search = attrs
        if "sector-row" in attrs.get("class", "").split():
            assert tag == "a" and "role" not in attrs
            assert self.stack[-1] == "li" and self.stack[-2] == "ul"
            self.server_links += 1
        if tag not in {"input", "img", "link", "meta", "br", "hr"}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()


async def test_search_constraints_and_semantic_server_list(async_client):
    await db.upsert_server("byond://8.8.8.8:1234", "Test station", "8.8.8.8", 1234)
    response = await async_client.get("/")
    parser = DashboardParser()
    parser.feed(response.text)
    assert parser.server_links == 1
    assert "required" in parser.search
    assert parser.search["pattern"] == r".*\S.*"


@pytest.mark.parametrize("title,encoding", [("Колония Кофе", "cp1251"), ("Sojourn – colony", "cp1252"), ("Русский UTF-8 ☕", "utf-8")])
def test_topic_legacy_bytes_and_percent_encoding(title, encoding):
    raw = ("name=" + title).encode(encoding) + b"\0"
    payload = b"\x06" + raw
    packet = b"\x00\x83" + struct.pack(">H", len(payload)) + payload
    assert parse_topic_query_string(parse_topic_response(packet))["name"] == title
    encoded = "".join(f"%{byte:02X}" for byte in title.encode(encoding))
    assert parse_topic_query_string("name=" + encoded)["name"] == title


def test_declared_western_charset_and_undefined_cp1251_byte():
    assert decode_byond_text("Café colony".encode("cp1252"), "cp1252") == "Café colony"
    assert decode_byond_text(b"Colony \x98") == "Colony ˜"


def test_nested_percent_encoded_legacy_manifest():
    name = quote("Иван Петров".encode("cp1251"))
    rank = quote("Капитан".encode("cp1251"))
    response = parse_topic_query_string("command=" + quote(f"{name}={rank}"))
    assert parse_manifest_response(response) == {
        "command": [{"name": "Иван Петров", "rank": "Капитан"}]
    }


def test_manifest_preserves_commas_in_names_and_ranks():
    response = parse_topic_query_string(
        "command=" + quote("Smith, John=Captain&Dr. Bob, MD=Head of Personnel")
    )
    assert parse_manifest_response(response) == {
        "command": [
            {"name": "Smith, John", "rank": "Captain"},
            {"name": "Dr. Bob, MD", "rank": "Head of Personnel"},
        ]
    }


@pytest.mark.parametrize("encoding,title", [("cp1251", "Колония Кофе"), ("cp1252", "Café colony")])
@pytest.mark.parametrize("text_fallback", [False, True])
async def test_hub_legacy_encoding(encoding, title, text_fallback):
    def respond(request):
        if "format=json" in str(request.url):
            if text_fallback:
                return httpx.Response(500)
            body = json.dumps([{"url": "byond://8.8.8.8:1234", "status": title}], ensure_ascii=False)
        else:
            body = f'world/1\nurl="byond://8.8.8.8:1234"\nstatus="{title}"'
        content_type = "text/plain" if encoding == "cp1251" else f"text/plain; charset={encoding}"
        return httpx.Response(200, content=body.encode(encoding), headers={"Content-Type": content_type})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        servers = await fetch_hub_servers(client)
    assert servers.healthy
    assert servers[0].title == title
