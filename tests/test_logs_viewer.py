"""Comprehensive unit, security, and E2E integration tests for the SierraBay12 Log Viewer."""

from __future__ import annotations

import io
import json
from pathlib import Path
import re
import tarfile
import time
import zipfile

from httpx import ASGITransport, AsyncClient
import httpx
import pytest

from core import db as core_db
from core import discord_auth
from core import logs_db
from core.config import AppConfig
from core.log_parser import (
    extract_ckeys_from_text,
    filter_game_log_file,
    mask_sensitive_text,
    parse_qdel_log_file,
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
    assert extract_ckeys_from_text("grey baby slime (15)/ : Blorble...") == []


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

    filtered = parse_qdel_log_file(qdel_path, q="floor_decal")
    assert len(filtered.entries) == 1
    assert filtered.entries[0].destroy_cost_ms == 326.708


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
