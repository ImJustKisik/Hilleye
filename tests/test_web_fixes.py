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
        self.server_rows = 0
        self.server_links = 0
        self.join_links = 0
        self.saw_table = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if attrs.get("id") == "ckey-input":
            self.search = attrs
        if attrs.get("id") == "servers-table" and tag == "table":
            self.saw_table = True
        if "server-row" in classes:
            assert tag == "tr"
            assert self.stack[-1] == "tbody" and self.stack[-2] == "table"
            self.server_rows += 1
        if "server-name-link" in classes:
            assert tag == "a" and attrs.get("href", "").startswith("/server/")
            self.server_links += 1
        if "join-btn" in classes:
            assert tag == "a" and attrs.get("href", "").startswith("byond://")
            self.join_links += 1
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
    assert parser.saw_table
    assert parser.server_rows == 1
    assert parser.server_links == 1
    assert parser.join_links == 1
    assert "required" in parser.search
    assert "autofocus" not in parser.search
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


async def test_csp_headers_and_external_js(async_client):
    response = await async_client.get("/")
    assert response.status_code == 200
    csp = response.headers.get("Content-Security-Policy", "")
    assert "script-src 'self'" in csp
    assert "style-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert '<script src="/static/js/app.js" defer></script>' in response.text
    assert "onclick=" not in response.text
    assert "oninput=" not in response.text
    assert "<script>" not in response.text

    docs_resp = await async_client.get("/docs")
    assert docs_resp.status_code == 200
    assert "Content-Security-Policy" not in docs_resp.headers


async def test_http_404_content_negotiation_and_header_count(async_client):
    html_resp = await async_client.get("/nonexistent-route-xyz", headers={"Accept": "text/html"})
    assert html_resp.status_code == 404
    assert html_resp.headers["content-type"].startswith("text/html")
    assert "404" in html_resp.text
    assert "Page not found" in html_resp.text
    assert "header-stats" in html_resp.text
    assert '<span class="header-stat-label">servers</span>' in html_resp.text
    assert ">Active<" not in html_resp.text

    json_resp = await async_client.get("/nonexistent-route-xyz", headers={"Accept": "application/json"})
    assert json_resp.status_code == 404
    assert json_resp.json()["detail"] == "Not Found"


async def test_dashboard_overview_and_empty_directory_state(async_client):
    empty_resp = await async_client.get("/")
    assert empty_resp.status_code == 200
    assert 'aria-label="Network summary"' in empty_resp.text
    assert "No active servers recorded yet" in empty_resp.text

    sid = await db.upsert_server("byond://127.0.0.1:4100", "Overview Station", "127.0.0.1", 4100, current_players=4)
    await db.update_player_sessions(sid, {"alice_pilot"}, now_ts=1000.0)
    await db.update_player_sessions(sid, {"alice_pilot"}, now_ts=1200.0)
    await db.update_player_sessions(sid, {"alice_pilot"}, now_ts=1400.0)
    await db.update_player_sessions(sid, {"alice_pilot"}, now_ts=1600.0)

    populated_resp = await async_client.get("/")
    assert populated_resp.status_code == 200
    assert "Recently Active Players" in populated_resp.text
    assert "/player/alicepilot" in populated_resp.text
    assert "Overview Station" in populated_resp.text
    assert "0.2h" in populated_resp.text


async def test_dashboard_true_headcount_freshness_sparklines_and_english_copy(async_client):
    import time

    now = time.time()
    fresh_sid = await db.upsert_server(
        "byond://127.0.0.1:4501",
        "Fresh Station",
        "127.0.0.1",
        4501,
        current_players=42,
        map_name="BoxStation",
        gamemode="extended",
        round_duration=3720,
        admins_count=2,
        enter_allowed=False,
        now_ts=now,
    )
    stale_sid = await db.upsert_server(
        "byond://127.0.0.1:4502",
        "Stale Station",
        "127.0.0.1",
        4502,
        current_players=18,
        map_name="MetaStation",
        gamemode="secret",
        round_duration=1800,
        now_ts=now,
    )
    # Mark fresh_sid telemetry fresh and stale_sid telemetry >600s old
    conn = await db.get_connection()
    await conn.execute(
        "UPDATE servers SET telemetry_observed_at = ? WHERE id = ?;",
        (now - 30.0, fresh_sid),
    )
    await conn.execute(
        "UPDATE servers SET telemetry_observed_at = ? WHERE id = ?;",
        (now - 900.0, stale_sid),
    )
    await conn.commit()

    await db.record_collector_cycle(now - 25.0, healthy=True)
    await db.record_server_snapshot(fresh_sid, player_count=20, round_duration=1200, map_name="BoxStation", ts=now - 3600)
    await db.record_server_snapshot(fresh_sid, player_count=42, round_duration=3720, map_name="BoxStation", ts=now - 60)

    resp = await async_client.get("/")
    assert resp.status_code == 200
    html = resp.text

    # True sum of current_players (42 + 18 = 60) across 2 servers
    assert '<span class="header-stat-val mono-val">60</span>' in html

    # Real telemetry, sparkline, Closed & Stale cues, zero inline styles
    assert '<svg class="mini-spark"' in html
    assert "<title>24h: 20&ndash;42 players</title>" in html
    assert "srv-pill-closed" in html
    assert "Closed" in html
    assert "srv-pill-stale" in html
    assert "is-stale-cell" in html
    assert "byond://127.0.0.1:4501" in html
    assert "127.0.0.1:4501" in html
    assert "Updated " in html
    assert 'style="' not in html

    # 100% English & zero leftover AI/surveillance jargon
    for banned in ("Логи", "Monitored sectors", "honkmaster", "Live BYOND Telemetry", "Click any key", "Player Dossier Lookup"):
        assert banned not in html


async def test_server_page_fixes_null_duration_pager_admins_and_single_point(async_client):
    # 1. Pager-only server with None round_duration, 0 admins, and 1 chart snapshot
    pager_sid = await db.upsert_server(
        "byond://BYOND.world.999111222",
        "Pager Colony",
        "BYOND.world.999111222",
        0,
        admins_count=0,
    )
    import time
    now = time.time()
    await db.record_server_snapshot(pager_sid, player_count=5, round_duration=0, map_name="Torch", ts=now)

    resp = await async_client.get(f"/server/{pager_sid}")
    assert resp.status_code == 200
    html = resp.text
    assert "Nones elapsed" not in html
    assert "Not reported" in html
    assert ":0<" not in html
    assert "Connect (BYOND Pager)" in html
    assert "Topic unavailable" in html
    assert "No staff online" in html
    assert 'class="sparkline-point"' in html

    # 2. Direct server with >3 admins shows overflow count
    admins = [{"key": f"Admin{i}", "rank": "Moderator" if i % 2 else ""} for i in range(5)]
    direct_sid = await db.upsert_server(
        "byond://127.0.0.1:4200",
        "Admin Colony",
        "127.0.0.1",
        4200,
        admins_count=5,
        admin_list=admins,
        round_duration=120,
    )
    direct_resp = await async_client.get(f"/server/{direct_sid}")
    assert direct_resp.status_code == 200
    assert "(+2 more)" in direct_resp.text
    assert "Admin0 ()" not in direct_resp.text
    assert "Query Live Manifest" in direct_resp.text


async def test_player_dossier_og_links_and_sessions_pagination(async_client):
    sid = await db.upsert_server("byond://127.0.0.1:4300", "Dossier Bay", "127.0.0.1", 4300)
    # Seed 12 closed sessions + 1 active session = 13 sessions total
    base_ts = 10000.0
    for i in range(12):
        start = base_ts + i * 1000.0
        await db.update_player_sessions(sid, {"paginated_user"}, now_ts=start, grace_period_seconds=300)
        await db.update_player_sessions(sid, {"paginated_user"}, now_ts=start + 120.0, grace_period_seconds=300)
        await db.update_player_sessions(sid, set(), now_ts=start + 500.0, grace_period_seconds=300)
    await db.update_player_sessions(sid, {"paginated_user"}, now_ts=base_ts + 15000.0, grace_period_seconds=300)

    page_resp = await async_client.get("/player/paginateduser")
    assert page_resp.status_code == 200
    html = page_resp.text
    assert 'property="og:image"' in html
    assert "/api/v1/player/paginateduser/card.png" in html
    assert f'href="/server/{sid}"' in html
    assert "13 sessions" in html
    assert 'class="meter-progress"' in html
    assert 'style="' not in html
    assert "Load more sessions (3 remaining)" in html

    more_resp = await async_client.get("/player/paginateduser/sessions?offset=10&limit=10")
    assert more_resp.status_code == 200
    assert "load-more-sessions-row" not in more_resp.text
    assert ">13</td>" in more_resp.text


async def test_player_typeahead_suggest_and_fuzzy_not_found(async_client):
    sid = await db.upsert_server("byond://127.0.0.1:4400", "Sierra Bay", "127.0.0.1", 4400)
    await db.update_player_sessions(sid, {"honkmaster", "honkjunior", "captain_kirk"}, now_ts=1000.0)
    await db.update_player_sessions(sid, {"honkmaster"}, now_ts=4600.0)

    # 1. Prefix suggestion API
    prefix_resp = await async_client.get("/api/v1/players/suggest?q=honk")
    assert prefix_resp.status_code == 200
    payload = prefix_resp.json()
    assert payload["normalized"] == "honk"
    ckeys = [item["ckey"] for item in payload["suggestions"]]
    assert "honkmaster" in ckeys
    assert "honkjunior" in ckeys
    # honkmaster has more playtime (3600s vs 0s), should rank first among prefix matches
    assert ckeys.index("honkmaster") < ckeys.index("honkjunior")

    # 2. Fuzzy typo suggestion API (e.g. "hnkmaster" -> "honkmaster")
    fuzzy_resp = await async_client.get("/api/v1/players/suggest?q=hnkmaster")
    assert fuzzy_resp.status_code == 200
    fuzzy_items = fuzzy_resp.json()["suggestions"]
    assert any(item["ckey"] == "honkmaster" and item["match_kind"] == "fuzzy" for item in fuzzy_items)

    # 3. HTMX /search with a typo renders "Did you mean" fuzzy suggestions in not_found partial
    htmx_resp = await async_client.post(
        "/search",
        headers={"HX-Request": "true"},
        data={"ckey": "honkmastre"},
    )
    assert htmx_resp.status_code == 200
    assert "No record found for" in htmx_resp.text
    assert "Did you mean one of these players?" in htmx_resp.text
    assert "/player/honkmaster" in htmx_resp.text

    # 4. Direct /player/{typo} 404 page also includes fuzzy suggestions
    page_404 = await async_client.get("/player/captainkrik")
    assert page_404.status_code == 404
    assert "Did you mean one of these players?" in page_404.text
    assert "/player/captainkirk" in page_404.text


async def test_gzip_compression_for_html_and_static_assets(async_client):
    html_resp = await async_client.get("/", headers={"Accept-Encoding": "gzip"})
    assert html_resp.status_code == 200
    assert html_resp.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in html_resp.headers.get("vary", "").lower()
    assert "Hilleye" in html_resp.text

    css_resp = await async_client.get("/static/css/style.css", headers={"Accept-Encoding": "gzip"})
    assert css_resp.status_code == 200
    assert css_resp.headers.get("content-encoding") == "gzip"
    assert css_resp.headers["Cache-Control"] == "public, max-age=86400, stale-while-revalidate=3600"
    assert len(css_resp.text) > 500



