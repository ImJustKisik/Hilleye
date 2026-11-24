"""Comprehensive unit, security, and E2E integration tests for the SierraBay12 Log Viewer."""

from __future__ import annotations

import io
import json
from pathlib import Path
import re
import tarfile
import time
import zipfile

import aiosqlite
from httpx import ASGITransport, AsyncClient
import httpx
from PIL import Image, PngImagePlugin
import pytest

from core import celadon_logs
from core import db as core_db
from core import discord_auth
from core import logs_db
from core import map_renderer
from core.config import AppConfig
from core.log_parser import (
    collect_round_map_events,
    extract_ckeys_from_text,
    extract_coords_from_text,
    filter_game_log_file,
    mask_sensitive_text,
    parse_qdel_log_file,
    primary_coord_from_text,
    read_context_lines,
    sanitize_byond_html,
)
import web.app as web_app


SAMPLE_GAME_LOG = """\
[2026-11-08 14:21:29.828] Starting up. (ID: 0fd7d409)
[2026-11-08 14:21:29.829] ---------------------------
[2026-11-08 14:21:29.847] ADMIN: Loading jobban_rank
[2026-11-08 14:21:31.145] DEBUG: Abstract atom /obj/item/material/knife created!
[2026-11-08 14:22:10.000] ACCESS: Login: AwkwardlyConfusedNeuralNetwork/(AwkwardlyConfusedNeuralNetwork) from 127.0.0.1-4153998451 || BYOND v516
[2026-11-08 14:23:00.100] SAY: AwkwardlyConfusedNeuralNetwork/(<span style="color:#ffa500">Angel Murray</span>) : Привет, станция! <script>alert(1)</script>
[2026-11-08 14:23:05.200] SAY: grey baby slime (15)/ : Blorble...
[2026-11-08 14:23:15.300] EMOTE: Ian/ : <B>Ian</B> dances around.
[2026-11-08 14:24:00.500] ATTACK: AwkwardlyConfusedNeuralNetwork/(Angel Murray) has punched BadGuy/(John Doe) (INTENT: HARM) (<a href='byond://?_src_=holder;adminplayerobservecoodjump=1;X=82;Y=104;Z=3'>LOC</a>) (<a href='byond://?_src_=holder;adminplayerobservefollow=[0x30001b4]'>MOB</a>)
[2026-11-08 14:24:30.000] VOTE: <b>Total Votes: 0/1</b>
 - \t<b>Votes Per Option:</b>
[2026-11-08 14:25:00.000] DEBUG: Verbose tick noise
"""

SAMPLE_QDEL_LOG = """\
[2026-04-30 18:44:49.981] Path: /turf/space
 - \tqdel() Count: 185726
 - \tDestroy() Cost: 19032.3ms
 - Path: /datum/light_source
 - \tqdel() Count: 26434
 - \tunecessary qdel() Count: 508
 - \tDestroy() Cost: 4706.17ms
 - Path: /obj/floor_decal
 - \tqdel() Count: 4534
 - \tDestroy() Cost: 326.708ms
"""


# ---------------------------------------------------------------------------
# Tier 1: Parser, HTML Sanitizer, Ckey Extraction, Masking, qdel
# ---------------------------------------------------------------------------

def test_sanitize_byond_html_strips_xss_and_styles():
    raw = (
        '<B>Ian</B> <span style="color:#ffa500">Antag</span> '
        '<img src=x onerror=alert(1)> <script>alert(1)</script> '
        "<a href='byond://?_src_=holder;X=82;Y=104;Z=3'>LOC</a> "
        "<a href='byond://?_src_=holder;follow=1'>MOB</a> "
        "<a href='javascript:alert(1)'>badlink</a>"
    )
    safe_html, plain = sanitize_byond_html(raw)
    assert "<script" not in safe_html.lower()
    assert "<img" not in safe_html.lower()
    assert "&lt;img src=x onerror=alert(1)&gt;" in safe_html
    assert "style=" not in safe_html.lower()
    assert "javascript:" not in safe_html.lower()
    assert "byond://" not in safe_html.lower()
    assert "<strong>Ian</strong>" in safe_html
    assert '<span class="log-c-orange">Antag</span>' in safe_html
    assert '<span class="log-loc-badge">[82, 104, 3]</span>' in safe_html
    assert '<span class="log-mob-badge">[MOB]</span>' in safe_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in safe_html
    assert "[82, 104, 3]" in plain


def test_ckey_extraction_handles_players_and_skips_simple_mobs():
    assert extract_ckeys_from_text(
        "Login: AwkwardlyConfusedNeuralNetwork/(AwkwardlyConfusedNeuralNetwork) from 127.0.0.1-4153998451"
    ) == ["awkwardlyconfusedneuralnetwork"]
    assert extract_ckeys_from_text(
        "AwkwardlyConfusedNeuralNetwork/(Angel Murray) has punched BadGuy/(John Doe)"
    ) == ["awkwardlyconfusedneuralnetwork", "badguy"]
    assert extract_ckeys_from_text("John Doe (DC)/(Capt) said hello") == ["johndoe"]
    assert extract_ckeys_from_text("Styx/Dikiy_Bonor : эу.") == ["dikiybonor"]
    assert extract_ckeys_from_text("[Common] Styx/Dikiy_Bonor (DC) : hi") == ["dikiybonor"]
    assert extract_ckeys_from_text("grey baby slime (15)/ : Blorble...") == []
    assert extract_ckeys_from_text("Ian/ : <B>Ian</B> dances around.") == []
    assert extract_ckeys_from_text("/obj/item/radio : beep") == []
    assert extract_ckeys_from_text("foo /bar : baz") == []
    assert extract_ckeys_from_text("20/40 : ratio") == []


def test_mask_sensitive_text():
    raw = "Login: Player/(Player) from 192.168.10.22-4153998451 || BYOND v516"
    masked = mask_sensitive_text(raw)
    assert "192.168.10.22" not in masked
    assert "4153998451" not in masked
    assert "[IP-СКРЫТ]-[CID-СКРЫТ]" in masked


def test_filter_game_log_presets_continuation_context_and_cp1251(tmp_path):
    log_path = tmp_path / "game.log"
    log_path.write_bytes(SAMPLE_GAME_LOG.encode("cp1251"))

    # Default preset 'gameplay' hides DEBUG lines
    res_gameplay = filter_game_log_file(log_path, preset="gameplay")
    assert res_gameplay.round_id == "0fd7d409"
    assert res_gameplay.total_lines == 12
    assert res_gameplay.total_matches == 10
    assert all(e.category != "DEBUG" for e in res_gameplay.entries)
    # Continuation line under VOTE inherits VOTE category
    vote_lines = [e for e in res_gameplay.entries if e.category == "VOTE"]
    assert len(vote_lines) == 2
    assert vote_lines[1].is_continuation is True

    # Preset 'admin_combat'
    res_admin = filter_game_log_file(log_path, preset="admin_combat")
    assert {e.category for e in res_admin.entries} == {"ADMIN", "ACCESS", "ATTACK"}

    # Ckey filter
    res_badguy = filter_game_log_file(log_path, preset="all", ckey="BadGuy")
    assert res_badguy.total_matches == 1
    assert res_badguy.entries[0].category == "ATTACK"

    # Bad regex returns clean error instead of crashing
    res_bad_rx = filter_game_log_file(log_path, q="[unclosed(", use_regex=True)
    assert res_bad_rx.regex_error is not None
    assert res_bad_rx.total_matches == 0

    # Context lines include DEBUG around line 5
    ctx = read_context_lines(log_path, target_line=5, radius=2)
    assert [c.line_no for c in ctx] == [3, 4, 5, 6, 7]
    assert any(c.category == "DEBUG" for c in ctx)


def test_parse_qdel_log_file_sorts_and_filters(tmp_path):
    qdel_path = tmp_path / "qdel.log"
    qdel_path.write_text(SAMPLE_QDEL_LOG, encoding="utf-8")

    report = parse_qdel_log_file(qdel_path, sort_by="unnecessary_count", sort_dir="desc")
    assert report.total_paths == 3
    assert report.total_qdels == 185726 + 26434 + 4534
    assert report.total_unnecessary == 508
    assert report.entries[0].path == "/datum/light_source"

    filtered = parse_qdel_log_file(qdel_path, q="floor_decal", include_raw=False)
    assert len(filtered.entries) == 1
    assert filtered.entries[0].destroy_cost_ms == 326.708
    assert filtered.raw_text == ""


def test_log_parser_edge_cases_and_regressions(tmp_path):
    # 1. Self-closing tags, misnested tags, comments, links with inner tags, rgb()/!important colors
    assert sanitize_byond_html("<br/>") == ("&lt;br/&gt;", "<br/>")
    assert sanitize_byond_html("<img src=x/>") == ("&lt;img src=x/&gt;", "<img src=x/>")
    assert sanitize_byond_html("<b>hi</font></b>") == ("<strong>hi</strong>", "hi")
    assert sanitize_byond_html("<font color='red'><b>hi</font></b>") == (
        '<span class="log-c-red"><strong>hi</strong></span>',
        "hi",
    )
    assert sanitize_byond_html("<!-- note -->") == ("&lt;!-- note --&gt;", "<!-- note -->")
    assert sanitize_byond_html("<a href='byond://?X=10;Y=20;Z=1'><b>LOC</b></a>") == (
        '<span class="log-loc-badge">[10, 20, 1]</span>',
        "[10, 20, 1]",
    )
    assert sanitize_byond_html('<span style="color: rgb(255, 0, 0) !important">warn</span>') == (
        '<span class="log-c-red">warn</span>',
        "warn",
    )
    assert sanitize_byond_html('<span color="#808080">muted</span>') == (
        '<span class="log-c-gray">muted</span>',
        "muted",
    )

    # 2. Ckey and coordinate edge cases
    assert extract_ckeys_from_text("10/(20) — a/(Alice) and Bob/(Bob)") == ["a", "bob"]
    assert extract_coords_from_text("rgb (255, 100, 1) date 12-05-24 mismatch (10, 20, 1]") == []
    assert extract_coords_from_text("Ghost/player - 55-66-2 and [12, 34, 1]") == [(55, 66, 2), (12, 34, 1)]

    # 3. Midnight time filter, plain-text search, ckey word boundaries, ReDoS protection, mixed UTF-8/cp1251 & BOM
    from core.log_parser import scan_raw_files_for_query

    log_path = tmp_path / "game.log"
    lines = [
        "\ufeff[2026-11-08 23:45:00.000] Starting up. (ID: mix1)",
        "[2026-11-08 23:50:00.000] SAY: Alice/(Alice) : <font color='red'>Привет</font> I walked home",
        "[2026-11-09 00:10:00.000] ATTACK: " + ("x" * 140) + " Удар BadGuy kicked <a href='byond://?X=82;Y=104;Z=3'>LOC</a>",
        "[2026-11-09 00:25:00.000] SAY: Bob/(Bob) : secret 10.0.0.5-12345678",
    ]
    # Write line 2 in UTF-8 and append a cp1251 line to test per-line decoding resilience
    raw_bytes = ("\n".join(lines[:2]) + "\n").encode("utf-8") + (lines[2] + "\n").encode("cp1251") + (lines[3] + "\n").encode("utf-8")
    log_path.write_bytes(raw_bytes)

    # Midnight wrap-around: 23:46 .. 00:15 matches lines 2 and 3
    res_time = filter_game_log_file(log_path, preset="all", time_from="23:46", time_to="00:15")
    assert res_time.round_id == "mix1"
    assert [e.line_no for e in res_time.entries] == [2, 3]
    assert "Привет" in res_time.entries[0].plain_message
    assert "Удар" in res_time.entries[1].plain_message

    # Search matches rendered plain text (e.g. [82, 104, 3]) and NOT raw HTML tags like "font"
    assert filter_game_log_file(log_path, preset="all", q="font").total_matches == 0
    assert filter_game_log_file(log_path, preset="all", q="[82, 104, 3]").total_matches == 1

    # Ckey filter does not false-positive "al" inside "walked", and works on >128 char lines
    assert filter_game_log_file(log_path, preset="all", ckey="al").total_matches == 0
    assert filter_game_log_file(log_path, preset="all", ckey="badguy").total_matches == 1

    # Masked IP cannot be probed via search query
    assert filter_game_log_file(log_path, preset="all", q="10.0.0.5", mask_ips=True).total_matches == 0

    # ReDoS nested quantifier rejected cleanly while valid IPv4 regex is allowed
    res_redos = filter_game_log_file(log_path, preset="all", q="(a+)+b", use_regex=True)
    assert res_redos.regex_error is not None
    res_ip_rx = filter_game_log_file(log_path, preset="all", q=r"(\d{1,3}\.){3}\d{1,3}", use_regex=True)
    assert res_ip_rx.regex_error is None
    assert res_ip_rx.total_matches == 1

    # scan_raw_files_for_query with limit=0 returns []
    rnd_stub = [{"round_uid": "u1", "round_id": "mix1", "round_date": "2026-11-08", "source_type": "disk", "has_game_log": True, "dir_path": str(tmp_path)}]
    assert scan_raw_files_for_query(rnd_stub, query="Alice", limit=0) == []


# ---------------------------------------------------------------------------
# Tier 2: Logs DB, FTS5 Indexing, & Upload Archive Security
# ---------------------------------------------------------------------------

@pytest.fixture
async def logs_env(tmp_path, monkeypatch):
    tracker_db_path = str(tmp_path / "tracker.db")
    logs_db_path = str(tmp_path / "logs.db")
    logs_root = tmp_path / "sierra_logs"
    uploads_dir = tmp_path / "uploads"

    round1_dir = logs_root / "2026" / "11" / "08" / "round-0fd7d409"
    round1_dir.mkdir(parents=True)
    (round1_dir / "game.log").write_text(SAMPLE_GAME_LOG, encoding="utf-8")
    (round1_dir / "qdel.log").write_text(SAMPLE_QDEL_LOG, encoding="utf-8")

    # Empty folder must be ignored gracefully
    (logs_root / "2026" / "11" / "09" / "round-empty").mkdir(parents=True)

    cfg = AppConfig(
        db_path=tracker_db_path,
        custom_servers=[],
        discord_client_id="client_123",
        discord_client_secret="secret_456",
        discord_redirect_uri="https://logs.example.test/auth/discord/callback",
        discord_session_secret="test_super_secret_hmac_key_123456",
        discord_superadmin_ids=["111111111111111111"],
        discord_secure_cookies=False,  # Allow HTTP test transport cookies
        logs_root_dir=str(logs_root),
        logs_uploads_dir=str(uploads_dir),
        logs_db_path=logs_db_path,
        logs_max_upload_mb=2,
        logs_max_extracted_mb=3,
        logs_max_archive_files=10,
        logs_auto_scan_on_startup=False,
        maps_repo_dir=str(tmp_path / "missing_repo"),
        maps_cache_dir=str(tmp_path / "map_cache"),
        maps_auto_build_on_startup=False,
    )

    await core_db.init_db(tracker_db_path)
    await logs_db.init_logs_db(logs_db_path)
    monkeypatch.setattr(web_app, "get_config", lambda: cfg)

    yield {
        "cfg": cfg,
        "logs_root": logs_root,
        "uploads_dir": uploads_dir,
        "round1_dir": round1_dir,
    }

    discord_auth.set_discord_http_transport(None)
    await logs_db.close_logs_db()
    await core_db.close_db()


async def test_sync_fts5_search_and_archive_security(logs_env):
    cfg = logs_env["cfg"]
    summary = await logs_db.sync_and_index_rounds(
        cfg.resolved_logs_root_dir(),
        cfg.resolved_logs_uploads_dir(),
        include_debug=False,
    )
    assert summary["discovered"] == 1
    assert summary["indexed_rounds"] == 1

    # Second run skips unchanged round
    summary2 = await logs_db.sync_and_index_rounds(
        cfg.resolved_logs_root_dir(),
        cfg.resolved_logs_uploads_dir(),
        include_debug=False,
    )
    assert summary2["skipped_unchanged"] == 1
    assert summary2["indexed_rounds"] == 0

    # FTS5 search handles punctuation & finds non-DEBUG entries
    hits, total = await logs_db.search_logs_fts(query="punched", ckey="badguy", preset="gameplay")
    assert total == 1
    assert hits[0]["category"] == "ATTACK"
    assert hits[0]["round_id"] == "0fd7d409"

    # FTS5 syntax chars like ':' or '-' don't crash
    hits_punct, _ = await logs_db.search_logs_fts(query="127.0.0.1-4153998451 OR (test):", preset="all")
    assert isinstance(hits_punct, list)

    # Zip-slip archive must be rejected
    evil_zip = io.BytesIO()
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../../outside/game.log", SAMPLE_GAME_LOG)
    with pytest.raises(ValueError, match="Небезопасный путь"):
        await logs_db.ingest_uploaded_log(
            "evil.zip",
            evil_zip.getvalue(),
            uploads_dir=cfg.resolved_logs_uploads_dir(),
        )

    # Tarball symlink must be rejected
    evil_tar = io.BytesIO()
    with tarfile.open(fileobj=evil_tar, mode="w:gz") as tf:
        ti = tarfile.TarInfo("round-sym/game.log")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tf.addfile(ti)
    with pytest.raises(ValueError, match="обычные файлы"):
        await logs_db.ingest_uploaded_log(
            "sym.tar.gz",
            evil_tar.getvalue(),
            uploads_dir=cfg.resolved_logs_uploads_dir(),
        )

    # Zip-bomb exceeding max_extracted_bytes must be rejected
    bomb_zip = io.BytesIO()
    with zipfile.ZipFile(bomb_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("round-bomb/game.log", b"A" * (4 * 1024 * 1024))
    with pytest.raises(ValueError, match="лимит"):
        await logs_db.ingest_uploaded_log(
            "bomb.zip",
            bomb_zip.getvalue(),
            uploads_dir=cfg.resolved_logs_uploads_dir(),
            max_extracted_bytes=1 * 1024 * 1024,
        )

    # Valid zip upload creates and indexes uploaded round, and disk rounds cannot be deleted
    good_zip = io.BytesIO()
    with zipfile.ZipFile(good_zip, "w") as zf:
        zf.writestr("round-abcd1234/game.log", SAMPLE_GAME_LOG.replace("0fd7d409", "abcd1234"))
    created = await logs_db.ingest_uploaded_log(
        "batch.zip",
        good_zip.getvalue(),
        uploads_dir=cfg.resolved_logs_uploads_dir(),
        label="Uploaded Shift",
        uploaded_by="111111111111111111",
    )
    assert len(created) == 1
    assert created[0]["source_type"] == "upload"
    assert created[0]["round_id"] == "abcd1234"

    rounds, _ = await logs_db.list_rounds()
    disk_round = next(r for r in rounds if r["source_type"] == "disk")
    with pytest.raises(ValueError, match="только загруженные"):
        await logs_db.delete_uploaded_round(disk_round["round_uid"], uploads_dir=cfg.resolved_logs_uploads_dir())

    assert await logs_db.delete_uploaded_round(created[0]["round_uid"], uploads_dir=cfg.resolved_logs_uploads_dir()) is True


# ---------------------------------------------------------------------------
# Tier 3: Discord OAuth2, Whitelist, CSRF, & Full Web E2E
# ---------------------------------------------------------------------------

def _mock_discord_transport(discord_id: str, username: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == discord_auth.DISCORD_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "tok_xyz", "token_type": "Bearer"})
        if str(request.url) == discord_auth.DISCORD_ME_URL:
            return httpx.Response(
                200,
                json={"id": discord_id, "username": username, "global_name": username},
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


async def _login_via_discord(client: AsyncClient, discord_id: str, username: str) -> None:
    discord_auth.set_discord_http_transport(lambda: _mock_discord_transport(discord_id, username))
    login_resp = await client.get("/auth/discord/login?next=/logs", follow_redirects=False)
    assert login_resp.status_code == 302
    loc = login_resp.headers["location"]
    assert loc.startswith(discord_auth.DISCORD_AUTHORIZE_URL)
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(loc).query)["state"][0]
    cb_resp = await client.get(f"/auth/discord/callback?code=fake_code&state={state}", follow_redirects=False)
    assert cb_resp.status_code == 302
    assert cb_resp.headers["location"] == "/logs"


async def test_celadon_catalog_import_and_file_cap(logs_env, monkeypatch):
    cfg = logs_env["cfg"]
    assert AppConfig(logs_max_upload_mb=50).logs_max_upload_mb == 20
    cfg.logs_max_upload_mb = 20
    round_path = "2026/11/08/round-0fd7d409/"
    log_path = round_path + "game.log"
    upstream_requests = []

    class LogStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(20):
                yield b"x" * (1024 * 1024)
            yield b"x"

    def upstream(request):
        path = str(request.url).removeprefix(celadon_logs.ROOT_URL)
        upstream_requests.append(path)
        directories = {
            "": '<a href="2025/">2025</a><a href="2026/">2026</a>',
            "2026/": '<a href="10/">10</a><a href="11/">11</a>',
            "2026/11/": '<a href="01/">01</a><a href="08/">08</a>',
            "2026/11/08/": (
                '<a href="round-0fd7d409/">Первый</a><a href="round-abcd1234/">Второй</a>'
                '<a href="misc/">Разное</a><a href="https://example.org/round-external/">External</a>'
                '<a href="%2e%2e/">Parent</a>'
            ),
            round_path: '<a href="game.log">game.log</a><a href="notes.html">notes.html</a>',
        }
        if path in directories:
            return httpx.Response(200, headers={"Content-Type": "text/html"}, text=directories[path])
        if path == log_path:
            return httpx.Response(200, content=SAMPLE_GAME_LOG.encode("utf-8"))
        if path == "large.log":
            return httpx.Response(200, stream=LogStream())
        if path == "redirect.log":
            return httpx.Response(302, headers={"Location": "https://example.org/redirected.log"})
        if path == "2026/11/09/":
            return httpx.Response(403)
        return httpx.Response(404)

    monkeypatch.setattr(celadon_logs.httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(upstream))
    async with AsyncClient(transport=ASGITransport(app=web_app.app), base_url="http://test") as client:
        assert (await client.get("/logs/celadon")).status_code == 401
        assert not upstream_requests
        await _login_via_discord(client, "111111111111111111", "admin")
        dashboard = await client.get("/logs")
        assert "Показать Celadon" in dashboard.text
        assert 'href="/logs/celadon"' not in dashboard.text
        assert not upstream_requests
        root = await client.get("/logs/celadon", follow_redirects=True)
        assert root.status_code == 200
        assert root.url.path == "/logs"
        assert 'type="date"' in root.text and 'value="2026-11-08"' in root.text
        assert "08.11.2026" in root.text and "#0fd7d409" in root.text and "#abcd1234" in root.text
        assert "example.org" not in root.text
        assert "Разное" not in root.text
        assert upstream_requests == ["", "2026/", "2026/11/", "2026/11/08/"]

        matches = await client.get("/logs", params={"source": "celadon", "date": "2026-11-08", "q": "#0FD7"})
        assert "#0fd7d409" in matches.text and "#abcd1234" not in matches.text
        missing = await client.get("/logs", params={"source": "celadon", "date": "2026-11-10"})
        assert "Раунды по заданным фильтрам не найдены" in missing.text

        listing = await client.get("/logs/celadon", params={"path": round_path}, follow_redirects=True)
        assert listing.status_code == 200
        assert listing.url.path == "/logs"
        assert log_path in listing.text
        assert "notes.html" not in listing.text
        requests_before_import = upstream_requests.copy()
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', listing.text).group(1)
        assert (await client.post("/logs/celadon", data={"path": log_path})).status_code == 403
        assert upstream_requests == requests_before_import

        headers = {"Accept": "application/json"}
        for path in ("../game.log", "%2e%2e/game.log", "https://example.org/evil.log"):
            response = await client.post("/logs/celadon", data={"path": path, "csrf_token": csrf}, headers=headers)
            assert response.status_code == 400
        assert upstream_requests == requests_before_import

        for path, status in (("redirect.log", 502), ("large.log", 413)):
            response = await client.post("/logs/celadon", data={"path": path, "csrf_token": csrf}, headers=headers)
            assert response.status_code == status
        blocked = await client.get("/logs", params={"source": "celadon", "date": "2026-11-09"})
        assert "белом списке" in blocked.text

        manual = await client.post(
            "/logs/upload", data={"csrf_token": csrf},
            files={"file": ("large.log", b"x" * (20 * 1024 * 1024 + 1))}, headers=headers,
        )
        assert manual.status_code == 413
        assert (await logs_db.list_rounds(source_filter="upload"))[1] == 0

        imported = await client.post("/logs/celadon", data={"path": log_path, "csrf_token": csrf})
        assert imported.status_code == 303
        assert imported.headers["location"].startswith("/logs/round/")
        round_page = await client.get(imported.headers["location"])
        assert round_page.status_code == 200
        hits, total = await logs_db.search_logs_fts(query="punched", preset="gameplay")
        assert total == 1 and hits[0]["round_id"] == "0fd7d409"
        events, _ = await logs_db.get_audit_events(action_filter="upload_round")
        assert log_path in events[0]["details"]


async def test_logs_viewer_auth_whitelist_csrf_and_e2e(logs_env):
    cfg = logs_env["cfg"]
    await logs_db.sync_and_index_rounds(cfg.resolved_logs_root_dir(), cfg.resolved_logs_uploads_dir())

    async with AsyncClient(transport=ASGITransport(app=web_app.app), base_url="http://test") as client:
        # 1. Public Hilleye still works and links to /logs
        home = await client.get("/")
        assert home.status_code == 200
        assert 'href="/logs"' in home.text

        # 2. Unauthenticated access blocked
        assert (await client.get("/logs")).status_code == 401
        assert (await client.get("/api/v1/logs/rounds")).status_code == 401

        # 3. Non-whitelisted Discord user gets 403 + sees their ID
        await _login_via_discord(client, "999999999999999999", "stranger")
        denied = await client.get("/logs")
        assert denied.status_code == 403
        assert "999999999999999999" in denied.text
        assert (await client.get("/api/v1/logs/rounds")).status_code == 403

        # 4. Open redirect attempt in OAuth state falls back to /logs
        discord_auth.set_discord_http_transport(lambda: _mock_discord_transport("111111111111111111", "headadmin"))
        r_redir = await client.get("/auth/discord/login?next=//evil.example/phish", follow_redirects=False)
        from urllib.parse import parse_qs, urlparse
        st = parse_qs(urlparse(r_redir.headers["location"]).query)["state"][0]
        r_cb = await client.get(f"/auth/discord/callback?code=c&state={st}", follow_redirects=False)
        assert r_cb.headers["location"] == "/logs"

        # 5. Superadmin views dashboard, round game.log, context, qdel.log, download, search
        dash = await client.get("/logs")
        assert dash.status_code == 200
        assert "0fd7d409" in dash.text

        rounds_api = (await client.get("/api/v1/logs/rounds")).json()
        assert rounds_api["total"] == 1
        uid = rounds_api["rounds"][0]["round_uid"]

        round_page = await client.get(f"/logs/round/{uid}")
        assert round_page.status_code == 200
        assert "AwkwardlyConfusedNeuralNetwork" in round_page.text
        assert "<script>alert(1)</script>" not in round_page.text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in round_page.text
        assert "[82, 104, 3]" in round_page.text

        # Masked screenshot mode hides IP & CID
        masked_page = await client.get(f"/logs/round/{uid}?mask_ips=1")
        assert masked_page.status_code == 200
        assert "127.0.0.1" not in masked_page.text
        assert "4153998451" not in masked_page.text
        assert "[IP-СКРЫТ]-[CID-СКРЫТ]" in masked_page.text

        # Context ±20 endpoint includes DEBUG lines
        ctx_html = await client.get(f"/logs/round/{uid}/context?file=game.log&line=5&radius=2")
        assert ctx_html.status_code == 200
        assert "DEBUG" in ctx_html.text

        # qdel.log view, empty filter state, & JSON API
        qdel_page = await client.get(f"/logs/round/{uid}?file=qdel.log")
        assert qdel_page.status_code == 200
        assert "/turf/space" in qdel_page.text
        qdel_empty = await client.get(f"/logs/round/{uid}?file=qdel.log&q=__no_such_datum__")
        assert qdel_empty.status_code == 200
        assert "Записи qdel() по заданному фильтру не найдены." in qdel_empty.text
        qdel_api = (await client.get(f"/api/v1/logs/rounds/{uid}/qdel")).json()
        assert qdel_api["total_unnecessary"] == 508

        # Focused DEBUG line hidden in gameplay preset shows notice with preset=all link
        focus_hidden = await client.get(f"/logs/round/{uid}?focus=4")
        assert focus_hidden.status_code == 200
        assert "скрыта активным фильтром" in focus_hidden.text
        assert "preset=all&amp;focus=4#L4" in focus_hidden.text or "preset=all&focus=4#L4" in focus_hidden.text

        # Download filtered log
        dl = await client.get(f"/api/v1/logs/rounds/{uid}/download?mode=filtered&preset=admin_combat&mask_ips=1")
        assert dl.status_code == 200
        assert "ATTACK:" in dl.text
        assert "DEBUG:" not in dl.text
        assert "127.0.0.1" not in dl.text

        # Global search page (FTS5 + scan_debug disk fallback for DEBUG lines)
        gsearch = await client.get("/logs/search?q=punched&ckey=badguy")
        assert gsearch.status_code == 200
        assert "0fd7d409" in gsearch.text

        dsearch_fts = await client.get("/logs/search?q=Verbose+tick+noise&preset=all")
        assert dsearch_fts.status_code == 200
        assert "Найдено совпадений: 0" in dsearch_fts.text

        dsearch_disk = await client.get("/logs/search?q=Verbose+tick+noise&preset=all&scan_debug=1")
        assert dsearch_disk.status_code == 200
        assert "Найдено совпадений: 1" in dsearch_disk.text
        assert "#L12" in dsearch_disk.text

        # 6. Extract CSRF token from /logs/admin and test CSRF + whitelist management
        admin_page = await client.get("/logs/admin")
        assert admin_page.status_code == 200
        csrf_match = re.search(r'name="csrf_token" value="([^"]+)"', admin_page.text)
        assert csrf_match is not None
        csrf = csrf_match.group(1)

        # POST without CSRF must fail 403
        bad_add = await client.post(
            "/logs/admin/whitelist/add",
            data={"discord_id": "222222222222222222", "display_name": "Mod"},
        )
        assert bad_add.status_code == 403

        # Add viewer to whitelist
        ok_add = await client.post(
            "/logs/admin/whitelist/add",
            data={"discord_id": "222222222222222222", "display_name": "Mod", "role": "viewer", "csrf_token": csrf},
            follow_redirects=False,
        )
        assert ok_add.status_code == 303

        # Upload a manual log via POST /logs/upload
        upload_resp = await client.post(
            "/logs/upload",
            data={"label": "Manual Incident", "csrf_token": csrf},
            files={"file": ("manual.log", SAMPLE_GAME_LOG.encode("utf-8"), "text/plain")},
            headers={"X-Requested-With": "fetch"},
        )
        assert upload_resp.status_code == 200
        up_json = upload_resp.json()
        assert up_json["ok"] is True
        up_uid = up_json["rounds"][0]["round_uid"]

        # Audit log recorded login, login_denied, whitelist_add, upload_round, download_log
        audit_after = await client.get("/logs/admin")
        assert "whitelist_add" in audit_after.text
        assert "upload_round" in audit_after.text

    # 7. Newly whitelisted viewer logs in: can read logs, CANNOT open /logs/admin or delete uploads
    async with AsyncClient(transport=ASGITransport(app=web_app.app), base_url="http://test") as viewer_client:
        await _login_via_discord(viewer_client, "222222222222222222", "mod_viewer")
        assert (await viewer_client.get("/logs")).status_code == 200
        assert (await viewer_client.get("/logs/admin")).status_code == 403
        assert (await viewer_client.get(f"/logs/round/{up_uid}")).status_code == 200

        # Extract viewer CSRF and verify viewer cannot delete uploaded rounds
        v_dash = await viewer_client.get("/logs")
        v_csrf = re.search(r'name="csrf_token" value="([^"]+)"', v_dash.text).group(1)
        del_try = await viewer_client.post(f"/logs/rounds/{up_uid}/delete", data={"csrf_token": v_csrf})
        assert del_try.status_code == 403

        # Logout requires CSRF and clears session cookie
        bad_logout = await viewer_client.post("/auth/logout", data={"csrf_token": "wrong"})
        assert bad_logout.status_code == 403
        ok_logout = await viewer_client.post("/auth/logout", data={"csrf_token": v_csrf}, follow_redirects=False)
        assert ok_logout.status_code == 303
        assert (await viewer_client.get("/logs")).status_code == 401


async def test_unconfigured_oauth_fails_closed(tmp_path, monkeypatch):
    cfg = AppConfig(
        db_path=str(tmp_path / "t.db"),
        custom_servers=[],
        logs_db_path=str(tmp_path / "l.db"),
        logs_uploads_dir=str(tmp_path / "up"),
        logs_auto_scan_on_startup=False,
        maps_repo_dir=str(tmp_path / "missing_repo"),
        maps_cache_dir=str(tmp_path / "map_cache"),
        maps_auto_build_on_startup=False,
    )
    await core_db.init_db(cfg.resolved_db_path())
    await logs_db.init_logs_db(cfg.resolved_logs_db_path())
    monkeypatch.setattr(web_app, "get_config", lambda: cfg)

    try:
        async with AsyncClient(transport=ASGITransport(app=web_app.app), base_url="http://test") as client:
            resp = await client.get("/logs")
            assert resp.status_code == 503
            assert "Discord OAuth2 не настроен" in resp.text
            assert (await client.get("/api/v1/logs/rounds")).status_code == 503
    finally:
        await logs_db.close_logs_db()
        await core_db.close_db()


# ---------------------------------------------------------------------------
# Tier 3: Coordinate Extraction, Map Rendering (DMM/TGM/GitHub), & Map UI
# ---------------------------------------------------------------------------

def test_extract_coords_all_formats_and_filters():
    assert extract_coords_from_text(
        "(<a href='byond://?_src_=holder;adminplayerobservecoodjump=1;X=82;Y=104;Z=3'>LOC</a>)"
    ) == [(82, 104, 3)]
    assert extract_coords_from_text("Explosion at 91, 130, 1 caused by bomb") == [(91, 130, 1)]
    assert extract_coords_from_text("Shot fired from (45, 78, 2) toward [120, 44, 4]") == [
        (45, 78, 2),
        (120, 44, 4),
    ]
    assert extract_coords_from_text("Ghost/player - 55-66-2") == [(55, 66, 2)]

    # Reject nullspace (0, 0, 0) and CSS rgb/rgba color tuples
    assert extract_coords_from_text("Spawned in nullspace at (0, 0, 0)") == []
    assert extract_coords_from_text("<span style='color: rgb(10, 20, 3)'>hi</span> rgba(12, 34, 5, 1)") == []
    assert primary_coord_from_text("Text (12, 34, 2) and X=55;Y=66;Z=3") == (12, 34, 2)


def _create_synthetic_dmi(path: Path, state_name: str, color: tuple[int, int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGBA", (32, 32), color)
    meta = PngImagePlugin.PngInfo()
    meta.add_text(
        "Description",
        f"# BEGIN DMI\nversion = 4.0\n\twidth = 32\n\theight = 32\nstate = \"{state_name}\"\n\tdirs = 1\n\tframes = 1\n# END DMI\n",
        zip=True,
    )
    img.save(path, format="PNG", pnginfo=meta)


def _create_synthetic_sierra_repo(repo_dir: Path) -> None:
    _create_synthetic_dmi(repo_dir / "icons" / "turf" / "floors.dmi", "plating", (90, 100, 120, 255))
    _create_synthetic_dmi(repo_dir / "icons" / "turf" / "walls.dmi", "wall", (160, 170, 185, 255))

    dm_code = repo_dir / "code" / "game" / "objects.dm"
    dm_code.parent.mkdir(parents=True, exist_ok=True)
    dm_code.write_text(
        """
/area/bridge
\tname = "Мостик (Bridge)"

/area/hallway
\tname = "Коридор"

/turf/simulated/floor/plating
\ticon = 'icons/turf/floors.dmi'
\ticon_state = "plating"

/turf/simulated/wall
\ticon = 'icons/turf/walls.dmi'
\ticon_state = "wall"
\tlayer = 3
""",
        encoding="utf-8",
    )

    # Compact single-line DMM (4x4, Z1..Z4)
    maps_dir = repo_dir / "maps" / "sierra"
    maps_dir.mkdir(parents=True, exist_ok=True)
    (maps_dir / "z1-z4_sierra.dmm").write_text(
        '"a" = (/turf/simulated/floor/plating,/area/bridge)\n'
        '"b" = (/turf/simulated/wall,/area/hallway)\n\n'
        '(1,1,1) = {"\n'
        'aaaa\n'
        'abba\n'
        'abba\n'
        'aaaa\n'
        '"}\n'
        '(1,1,2) = {"\n'
        'bbbb\n'
        'baab\n'
        'baab\n'
        'bbbb\n'
        '"}\n',
        encoding="utf-8",
    )

    # Multi-line TGM format (z6_admin -> Z5)
    (maps_dir / "z6_admin.dmm").write_text(
        "//MAP CONVERTED BY dmm2tgm.py THIS HEADER COMMENT PREVENTS RECONVERSION, DO NOT REMOVE\n"
        '"aa" = (\n'
        '/turf/simulated/floor/plating,\n'
        '/area/bridge)\n'
        '"bb" = (\n'
        '/turf/simulated/wall,\n'
        '/area/hallway)\n\n'
        '(1,1,1) = {"\n'
        'aa\n'
        'aa\n'
        'bb\n'
        'bb\n'
        '"}\n'
        '(3,1,1) = {"\n'
        'aa\n'
        'bb\n'
        'aa\n'
        'bb\n'
        '"}\n',
        encoding="utf-8",
    )


async def test_map_renderer_dmm_tgm_and_github_fallback(tmp_path):
    repo_dir = tmp_path / "SierraBay12"
    cache_dir = tmp_path / "map_cache"
    _create_synthetic_sierra_repo(repo_dir)

    cfg = AppConfig(
        maps_repo_dir=str(repo_dir),
        maps_cache_dir=str(cache_dir),
        maps_tile_px=16,
        maps_github_repo="ImJustKisik/SierraBay12",
        maps_github_branch="dev-sierra",
        maps_auto_build_on_startup=False,
    )
    manifest = await map_renderer.build_map_cache(cfg, force=True, prefer_github=False)
    assert manifest["ready"] is True
    assert manifest["source_kind"] == "local_git"
    assert any(lv["z"] == 1 for lv in manifest["levels"])
    assert any(lv["z"] == 5 for lv in manifest["levels"])

    z1_png = map_renderer.get_map_image_path(cache_dir, 1)
    assert z1_png is not None and z1_png.is_file()
    with Image.open(z1_png) as im:
        assert im.size == (4 * 16, 4 * 16)

    assert map_renderer.lookup_area_name(cache_dir, 1, 2, 2) == "Коридор"
    assert map_renderer.lookup_area_name(cache_dir, 1, 1, 1) == "Мостик (Bridge)"
    assert map_renderer.lookup_area_name(cache_dir, 5, 1, 1) == "Коридор"

    # Build .tar.gz archive in memory and verify GitHub download fallback
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
        for file_path in repo_dir.rglob("*"):
            if file_path.is_file():
                rel = file_path.relative_to(repo_dir).as_posix()
                tf.add(file_path, arcname=f"SierraBay12-dev-sierra/{rel}")
    tar_bytes = tar_buf.getvalue()

    def _fake_transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=tar_bytes)

    map_renderer.set_github_http_transport(lambda: httpx.MockTransport(_fake_transport))
    try:
        gh_cache = tmp_path / "gh_cache"
        gh_cfg = AppConfig(
            maps_repo_dir=str(tmp_path / "does_not_exist"),
            maps_cache_dir=str(gh_cache),
            maps_tile_px=16,
            maps_github_repo="ImJustKisik/SierraBay12",
            maps_github_branch="dev-sierra",
            maps_auto_build_on_startup=False,
        )
        gh_manifest = await map_renderer.build_map_cache(gh_cfg, force=True, prefer_github=True)
        assert gh_manifest["ready"] is True
        assert gh_manifest["source_kind"] == "github"
        assert map_renderer.lookup_area_name(gh_cache, 1, 1, 1) == "Мостик (Bridge)"
    finally:
        map_renderer.set_github_http_transport(None)


async def test_logs_db_coords_and_migration(tmp_path):
    legacy_db = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(legacy_db) as db:
        await db.executescript(
            """
            CREATE TABLE log_rounds (
                round_uid TEXT PRIMARY KEY,
                source_type TEXT NOT NULL,
                round_id TEXT NOT NULL,
                round_date TEXT NOT NULL,
                dir_path TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                has_game_log INTEGER NOT NULL DEFAULT 0,
                indexed_at REAL
            );
            CREATE TABLE log_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_uid TEXT NOT NULL,
                file_name TEXT NOT NULL DEFAULT 'game.log',
                line_no INTEGER NOT NULL,
                timestamp TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'SYSTEM',
                ckeys_space TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL
            );
            INSERT INTO log_rounds (round_uid, source_type, round_id, round_date, dir_path, has_game_log, indexed_at)
            VALUES ('d-test1', 'disk', 'test1', '2026-11-08', 'x', 1, 123456);
            """
        )
        await db.commit()

    await logs_db.init_logs_db(legacy_db)
    try:
        # Migration adds coord_x/y/z and resets indexed_at so rounds re-index coordinates
        async with aiosqlite.connect(legacy_db) as db:
            cur = await db.execute("PRAGMA table_info(log_entries)")
            cols = {row[1] for row in await cur.fetchall()}
            assert {"coord_x", "coord_y", "coord_z"}.issubset(cols)
            cur = await db.execute("SELECT indexed_at FROM log_rounds WHERE round_uid = 'd-test1'")
            row = await cur.fetchone()
            assert row[0] is None
    finally:
        await logs_db.close_logs_db()

    # Now index real round and filter by z_level / only_coords
    db_path = str(tmp_path / "fresh.db")
    logs_root = tmp_path / "roots"
    r_dir = logs_root / "2026" / "11" / "08" / "round-0fd7d409"
    r_dir.mkdir(parents=True)
    (r_dir / "game.log").write_text(SAMPLE_GAME_LOG, encoding="utf-8")
    await logs_db.init_logs_db(db_path)
    try:
        await logs_db.sync_and_index_rounds(logs_root, tmp_path / "up")
        hits, total = await logs_db.search_logs_fts(z_level=3, only_coords=True)
        assert total == 1
        assert hits[0]["coord_x"] == 82
        assert hits[0]["coord_y"] == 104
        assert hits[0]["coord_z"] == 3
    finally:
        await logs_db.close_logs_db()


async def test_web_map_viewer_and_admin_rebuild(tmp_path, logs_env, monkeypatch):
    cfg: AppConfig = logs_env["cfg"]
    repo_dir = tmp_path / "repo_sierra"
    _create_synthetic_sierra_repo(repo_dir)
    monkeypatch.setattr(cfg, "maps_repo_dir", str(repo_dir))

    await logs_db.sync_and_index_rounds(cfg.resolved_logs_root_dir(), cfg.resolved_logs_uploads_dir())

    try:
        async with AsyncClient(transport=ASGITransport(app=web_app.app), base_url="http://test") as client:
            await _login_via_discord(client, "111111111111111111", "admin_user")

            rounds, _ = await logs_db.list_rounds()
            uid = rounds[0]["round_uid"]

            # Stream view contains clickable coordinate badge & sticky/htmx containers
            round_resp = await client.get(f"/logs/round/{uid}")
            assert round_resp.status_code == 200
            assert 'id="logs-stream-container"' in round_resp.text
            assert "logs-sticky-toolbar" in round_resp.text
            assert "log-coord-badge" in round_resp.text
            assert "view=map&z=3&tx=82&ty=104" in round_resp.text or "view=map&amp;z=3&amp;tx=82&amp;ty=104" in round_resp.text
            assert "is-excluded" in round_resp.text
            meta_match = re.search(r'<div class="logs-round-meta[^"]*"[^>]*>(.*?)</div>', round_resp.text, re.DOTALL)
            assert meta_match is not None
            meta_body = meta_match.group(1)
            assert meta_body.count("2026-11-08") == 1
            assert "14:21:29" in meta_body
            assert "3м" in meta_body
            assert "стр. 1 / 1" not in round_resp.text
            assert ">диск<" not in round_resp.text
            assert 'id="logs-raw-html-cb"' in round_resp.text
            assert re.search(r'id="logs-filter-reset"[^>]*\bhidden\b', round_resp.text) is not None

            # Active ckey filter renders summary pill, shows reset button, and hides redundant self-badge
            ck_resp = await client.get(f"/logs/round/{uid}?ckey=AwkwardlyConfusedNeuralNetwork")
            assert ck_resp.status_code == 200
            assert "logs-active-ckey-pill" in ck_resp.text
            assert "data-clear-ckey" in ck_resp.text
            assert re.search(r'id="logs-filter-reset"[^>]*\bhidden\b', ck_resp.text) is None
            assert 'data-ckey="awkwardlyconfusedneuralnetwork"' not in ck_resp.text
            assert 'data-ckey="badguy"' in ck_resp.text

            # HTMX partial request returns only the stream partial without full page shell
            htmx_resp = await client.get(f"/logs/round/{uid}?preset=all", headers={"HX-Request": "true"})
            assert htmx_resp.status_code == 200
            assert "log-stream" in htmx_resp.text
            assert "<!DOCTYPE html>" not in htmx_resp.text

            # Map view & map-events JSON API
            map_page = await client.get(f"/logs/round/{uid}?view=map&z=3&tx=82&ty=104&focus=9")
            assert map_page.status_code == 200
            assert 'id="logs-map-root"' in map_page.text
            assert 'id="logs-map-dim-btn"' in map_page.text
            assert 'id="logs-map-sector-select"' in map_page.text
            assert 'id="logs-map-show-all-deck"' in map_page.text

            ev_resp = await client.get(f"/api/v1/logs/rounds/{uid}/map-events")
            assert ev_resp.status_code == 200
            ev_json = ev_resp.json()
            assert ev_json["total_with_coords"] == 1
            assert ev_json["z_counts"]["3"] == 1
            assert ev_json["events"][0]["x"] == 82
            assert ev_json["events"][0]["y"] == 104
            assert ev_json["events"][0]["z"] == 3

            # Global search filtered by z_level and only_coords
            search_resp = await client.get("/logs/search?only_coords=1&z_level=3")
            assert search_resp.status_code == 200
            assert "Найдено совпадений: 1" in search_resp.text
            assert "На карте" in search_resp.text

            # Admin map rebuild (CSRF checked) + tile/area endpoints
            admin_page = await client.get("/logs/admin")
            csrf = re.search(r'name="csrf_token" value="([^"]+)"', admin_page.text).group(1)

            bad_rebuild = await client.post("/logs/admin/maps/rebuild", data={"source": "auto"})
            assert bad_rebuild.status_code == 403

            ok_rebuild = await client.post(
                "/logs/admin/maps/rebuild",
                data={"source": "auto", "csrf_token": csrf},
                follow_redirects=False,
            )
            assert ok_rebuild.status_code == 303

            man_resp = await client.get("/logs/maps/manifest")
            assert man_resp.status_code == 200
            assert man_resp.json()["ready"] is True

            png_resp = await client.get("/logs/maps/z/1.png")
            assert png_resp.status_code == 200
            assert png_resp.headers["content-type"].startswith("image/png")

            areas_resp = await client.get("/logs/maps/areas/1.json")
            assert areas_resp.status_code == 200
            assert "palette" in areas_resp.json()
    finally:
        discord_auth.set_discord_http_transport(None)


def test_logs_css_scopes_category_colors_to_tags():
    css_path = Path(__file__).resolve().parents[1] / "web" / "static" / "css" / "logs.css"
    css_text = css_path.read_text(encoding="utf-8")
    # Category color fills belong exclusively on .log-cat-tag pills, never bare .cat-* selectors
    for cat_cls in ("cat-say", "cat-access", "cat-ooc", "cat-attack", "cat-admin", "cat-emote", "cat-emote-mech"):
        assert f".log-cat-tag.{cat_cls}" in css_text
        assert re.search(rf"(?m)^\.{cat_cls}\b", css_text) is None


def test_emote_and_emote_mech_separation(tmp_path):
    from core.log_parser import CATEGORY_META, LOG_PRESETS, category_matches_filters

    assert "EMOTE" in CATEGORY_META
    assert "EMOTE_MECH" in CATEGORY_META
    assert CATEGORY_META["EMOTE_MECH"]["css"] == "cat-emote-mech"
    assert "EMOTE" in LOG_PRESETS["chat"]["include"]
    assert "EMOTE_MECH" in LOG_PRESETS["chat"]["include"]
    assert category_matches_filters("EMOTE_MECH", preset="chat")
    assert not category_matches_filters("EMOTE_MECH", selected_categories=["EMOTE"])
    assert category_matches_filters("EMOTE_MECH", selected_categories=["EMOTE_MECH"])

    log_path = tmp_path / "game.log"
    log_path.write_text(
        "[2026-11-08 14:23:10.000] EMOTE: Angel Murray/awkwardly : <B>Angel Murray</B> waves. (80, 100, 3)\n"
        "[2026-11-08 14:23:15.000] EMOTE_MECH: Angel Murray/awkwardly : <B>Angel Murray</B> coughs! (80, 100, 3)\n",
        encoding="utf-8",
    )

    manual_only = filter_game_log_file(log_path, categories=["EMOTE"])
    assert [e.category for e in manual_only.entries] == ["EMOTE"]

    mech_only = filter_game_log_file(log_path, categories=["EMOTE_MECH"])
    assert [e.category for e in mech_only.entries] == ["EMOTE_MECH"]


def test_ckey_spatial_radius_filtering(tmp_path):
    from core.log_parser import export_filtered_game_log

    log_path = tmp_path / "game.log"
    log_path.write_text(
        "[2026-11-08 14:20:00.000] ACCESS: Login: TargetPlayer/(TargetPlayer) from 127.0.0.1-123456\n"
        "[2026-11-08 14:20:05.000] SAY: Target Name/TargetPlayer : Hello here (50, 50, 1)\n"
        "[2026-11-08 14:20:07.000] SAY: Nearby Bob/NearBob : Hi back! (54, 48, 1)\n"
        "[2026-11-08 14:20:08.000] EMOTE_MECH: Nearby Bob/NearBob : <b>Nearby Bob</b> nods. (55, 55, 1)\n"
        "[2026-11-08 14:20:09.000] SAY: Far Alice/FarAlice : Too far (70, 50, 1)\n"
        "[2026-11-08 14:20:10.000] SAY: Other Deck/DeckGuy : Same xy diff z (50, 50, 2)\n"
        "[2026-11-08 14:20:11.000] SAY: NoCoords/NoCoordGuy : No location info\n"
        "[2026-11-08 14:25:00.000] SAY: Target Name/TargetPlayer : Moved away (120, 120, 2)\n"
        "[2026-11-08 14:25:05.000] SAY: Deck2 Friend/DeckFriend : Right next to you now (122, 118, 2)\n"
        "[2026-11-08 14:25:06.000] SAY: Old Spot/OldSpot : At old spot later (50, 50, 1)\n",
        encoding="utf-8",
    )

    # Radius 0: strictly targetplayer's own lines
    strict = filter_game_log_file(log_path, ckey="targetplayer", radius=0)
    assert [e.line_no for e in strict.entries] == [1, 2, 8]

    # Radius 5: targetplayer's own lines + nearby lines within Chebyshev distance 5 on same Z of nearest-in-time anchor
    nearby = filter_game_log_file(log_path, ckey="targetplayer", radius=5)
    assert [e.line_no for e in nearby.entries] == [1, 2, 3, 4, 8, 9]

    # Export also honors radius
    exported = export_filtered_game_log(log_path, ckey="targetplayer", radius=5)
    assert "Hi back!" in exported
    assert "Right next to you now" in exported
    assert "Too far" not in exported
    assert "At old spot later" not in exported

    # Map events also honor radius
    map_res = collect_round_map_events(log_path, ckey="targetplayer", radius=5)
    assert [ev["line_no"] for ev in map_res["events"]] == [2, 3, 4, 8, 9]


async def test_admin_dashboard_audit_filtering_export_and_htmx(logs_env):
    cfg = logs_env["cfg"]
    await logs_db.sync_and_index_rounds(cfg.resolved_logs_root_dir(), cfg.resolved_logs_uploads_dir())
    rounds, _ = await logs_db.list_rounds()
    round_uid = rounds[0]["round_uid"]

    # Seed diverse audit events including a denied login, a round view, and a CSV formula-like target
    await logs_db.record_audit_event(
        "login_denied",
        discord_id="333333333333333333",
        discord_username="intruder_user",
        target="whitelist_missing",
        ip_address="203.0.113.42",
        details="display_name=Intruder Nick",
    )
    await logs_db.record_audit_event(
        "view_round",
        discord_id="111111111111111111",
        discord_username="admin_user",
        target=f"{round_uid}:game.log",
        ip_address="198.51.100.7",
    )
    await logs_db.record_audit_event(
        "search_global",
        discord_id="111111111111111111",
        discord_username="admin_user",
        target="=CMD()_100%",
        ip_address="198.51.100.7",
        details="preset=gameplay, scan_debug=False, z=",
    )

    # Mark a round with an index_error to verify warning banner rendering
    conn = await logs_db.get_logs_connection()
    await conn.execute(
        "UPDATE log_rounds SET index_error = ? WHERE round_uid = ?",
        ("PermissionError: test read failure", round_uid),
    )
    await conn.commit()

    async with AsyncClient(transport=ASGITransport(app=web_app.app), base_url="http://test") as client:
        await _login_via_discord(client, "111111111111111111", "admin_user")

        admin_resp = await client.get("/logs/admin")
        assert admin_resp.status_code == 200
        # KPI strip, index error banner, quick-grant button for login_denied, and smart target link
        assert "Отказов входа: 1" in admin_resp.text
        assert "PermissionError: test read failure" in admin_resp.text
        assert "+ Выдать доступ" in admin_resp.text
        assert f'/logs/round/{round_uid}?file=game.log' in admin_resp.text

        # HTMX partial swap returns only the audit fragment (no full HTML shell)
        htmx_audit = await client.get(
            "/logs/admin?group=security",
            headers={"HX-Request": "true", "HX-Target": "admin-audit-container"},
        )
        assert htmx_audit.status_code == 200
        assert "<!DOCTYPE html>" not in htmx_audit.text
        assert "intruder_user" in htmx_audit.text
        assert f"{round_uid}:game.log" not in htmx_audit.text

        # Search filter with SQLite LIKE wildcard characters ('%' / '_') matches literal target
        q_resp = await client.get(
            "/logs/admin?audit_q=%3DCMD()_100%25",
            headers={"HX-Request": "true", "HX-Target": "admin-audit-container"},
        )
        assert q_resp.status_code == 200
        assert "=CMD()_100%" in q_resp.text
        assert "intruder_user" not in q_resp.text

        # Filter by IP and user
        ip_resp = await client.get(
            "/logs/admin?audit_ip=203.0.113.42",
            headers={"HX-Request": "true", "HX-Target": "admin-audit-container"},
        )
        assert ip_resp.status_code == 200
        assert "intruder_user" in ip_resp.text
        assert "198.51.100.7" not in ip_resp.text

        # CSV export neutralizes spreadsheet formula prefixes
        csv_resp = await client.get("/logs/admin/audit/export")
        assert csv_resp.status_code == 200
        assert csv_resp.headers["content-type"].startswith("text/csv")
        assert "'=CMD()_100%" in csv_resp.text

        # Refresh with next_url=/logs/admin redirects back to /logs/admin; external next_url is ignored
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', admin_resp.text).group(1)
        ref_admin = await client.post(
            "/logs/refresh",
            data={"csrf_token": csrf, "next_url": "/logs/admin"},
            follow_redirects=False,
        )
        assert ref_admin.status_code == 303
        assert ref_admin.headers["location"].startswith("/logs/admin?notice=")

        ref_evil = await client.post(
            "/logs/refresh",
            data={"csrf_token": csrf, "next_url": "https://evil.example/phish"},
            follow_redirects=False,
        )
        assert ref_evil.status_code == 303
        assert ref_evil.headers["location"].startswith("/logs?notice=")





async def test_logs_ux_overhaul_partials_and_csp(logs_env):
    cfg = logs_env["cfg"]
    await logs_db.sync_and_index_rounds(cfg.resolved_logs_root_dir(), cfg.resolved_logs_uploads_dir())
    rounds, _ = await logs_db.list_rounds()
    assert rounds
    round_uid = rounds[0]["round_uid"]

    async with AsyncClient(transport=ASGITransport(app=web_app.app), base_url="http://test") as client:
        await _login_via_discord(client, "111111111111111111", "admin_user")

        # 1. Dashboard renders quick date chips and HTMX returns partial table without full HTML shell
        dash = await client.get("/logs")
        assert dash.status_code == 200
        assert "data-quick-date" in dash.text
        assert 'id="logs-rounds-container"' in dash.text
        assert dash.text.count('id="logs-rounds-header-count"') == 1

        dash_htmx = await client.get("/logs?source=disk", headers={"HX-Request": "true"})
        assert dash_htmx.status_code == 200
        assert "<!DOCTYPE html>" not in dash_htmx.text
        assert "logs-rounds-table" in dash_htmx.text
        assert 'id="logs-rounds-header-count"' in dash_htmx.text
        assert 'hx-swap-oob="true"' in dash_htmx.text

        # 2. Round viewer renders collapsible filter drawer & inline context URLs
        rnd_page = await client.get(f"/logs/round/{round_uid}")
        assert rnd_page.status_code == 200
        assert 'id="logs-filter-drawer"' in rnd_page.text
        assert 'id="logs-filter-drawer-toggle"' in rnd_page.text
        assert "&inline=1" in rnd_page.text

        ctx_inline = await client.get(f"/logs/round/{round_uid}/context?file=game.log&line=5&inline=1")
        assert ctx_inline.status_code == 200
        assert "logs-context-wrap" in ctx_inline.text
        assert "js-close-inline-context" in ctx_inline.text

        # 3. qdel.log view renders visual share bar
        qdel_page = await client.get(f"/logs/round/{round_uid}?file=qdel.log")
        assert qdel_page.status_code == 200
        assert "qdel-share-bar" in qdel_page.text

        # 4. Map view renders labeled timeline scrubber
        map_page = await client.get(f"/logs/round/{round_uid}?view=map")
        assert map_page.status_code == 200
        assert 'id="logs-map-timeline"' in map_page.text
        assert 'id="logs-map-time-start"' in map_page.text
        assert 'id="logs-map-time-end"' in map_page.text
        assert 'id="logs-map-time-from-lbl"' in map_page.text
        assert 'id="logs-map-time-to-lbl"' in map_page.text

        # 5. Global search groups results by round and provides inline ±20 context trigger
        search_page = await client.get("/logs/search?q=punched&preset=all")
        assert search_page.status_code == 200
        assert "logs-search-group" in search_page.text
        assert "&inline=1" in search_page.text

        # 6. Admin page renders unified icons & section tabs
        admin_page = await client.get("/logs/admin")
        assert admin_page.status_code == 200
        assert "admin-section-tabs" in admin_page.text
        assert "logs-ico" in admin_page.text

        # 7. Unauthenticated auth gate renders access badge
        async with AsyncClient(transport=ASGITransport(app=web_app.app), base_url="http://test") as anon:
            auth_page = await anon.get("/logs")
            assert auth_page.status_code == 401
            assert "logs-auth-badge" in auth_page.text

        # 8. CSP check: no inline <style> or <script> blocks or inline style="" in rendered /logs pages
        for html_text in (dash.text, rnd_page.text, qdel_page.text, map_page.text, search_page.text, admin_page.text, auth_page.text):
            assert "<style" not in html_text.lower()
            assert "<script>" not in html_text.lower()
            assert ' style="' not in html_text.lower()
