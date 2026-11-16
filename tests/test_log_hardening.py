"""End-to-end and failure-path regressions for the log hardening rollout."""
import asyncio
import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import sqlite3
import time
import tracemalloc
import zipfile
import threading
from contextlib import closing

import httpx
import pytest

from core import db, logs_db, discord_auth
from core.config import AppConfig
from core.file_lock import FileLock, lock_path
from core.log_jobs import JobRunner, enqueue_job, get_job, logs_status
from core.log_operations import heavy_slot, run_log_operation
from core.log_parser import filter_game_log_file
from core.logs_db.indexer import DiscoveryResult
from core.logs_db.reader import _indexed_page
from core.maintenance import create_backup, restore_backup, verify_archive
import web.app as web

LINE = "[2026-11-16 12:00:00] SAY: alice/(Alice): markerfirst (10,10,1)\n"


@pytest.fixture
async def hardening_env(tmp_path, monkeypatch):
    root = tmp_path / "source"
    folder = root / "2026" / "11" / "16" / "round-123"
    folder.mkdir(parents=True)
    file = folder / "game.log"
    file.write_text(LINE, encoding="utf-8")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    config = AppConfig(db_path=str(tmp_path / "tracker.db"), logs_db_path=str(tmp_path / "logs.db"),
                       logs_root_dir=str(root), logs_uploads_dir=str(uploads),
                       maps_cache_dir=str(tmp_path / "maps"), maps_auto_build_on_startup=False,
                       logs_auto_scan_on_startup=False, logs_periodic_scan=False,
                       discord_client_id="test", discord_client_secret="test", discord_redirect_uri="https://test/auth",
                       discord_session_secret="test-hardening-secret", discord_superadmin_ids=["111111111111111111"],
                       discord_secure_cookies=False, custom_servers=[])
    await db.init_db(config.resolved_db_path())
    await logs_db.init_logs_db(config.resolved_logs_db_path())
    monkeypatch.setattr(web, "get_config", lambda: config)
    monkeypatch.setattr(web.app.state, "log_jobs", None, raising=False)
    yield config, root, file, uploads
    runner = getattr(web.app.state, "log_jobs", None)
    if runner:
        await runner.stop()
    await logs_db.close_logs_db()
    await db.close_db()


def _cookie(config, uid="111111111111111111"):
    return discord_auth.create_session_cookie({"discord_id": uid, "username": "tester", "display_name": "Tester"}, config)


def _lock_probe(path, queue):
    lock = FileLock(Path(path))
    acquired = lock.acquire()
    queue.put(acquired)
    lock.release()


async def _sync(env, **options):
    config, root, _, uploads = env
    return await logs_db.sync_and_index_rounds(root, uploads, **options)


async def test_missing_and_partial_sources_preserve_index(hardening_env, monkeypatch):
    config, root, file, uploads = hardening_env
    await _sync(hardening_env)
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    hidden = root.with_name("offline")
    root.rename(hidden)
    result = await _sync(hardening_env)
    assert result["errors"] == 1 and result["removed_stale"] == 0
    assert await logs_db.get_round_by_uid(uid)
    assert logs_status(config.resolved_logs_db_path())["status"] == "degraded"
    hidden.rename(root)
    monkeypatch.setattr("core.logs_db.indexer._discover_dirs_with_logs", lambda *_: DiscoveryResult(complete=False, errors=["permission denied"]))
    await _sync(hardening_env)
    assert await logs_db.get_round_by_uid(uid)


async def test_removal_needs_two_successful_scans(hardening_env, monkeypatch):
    await _sync(hardening_env)
    hardening_env[2].unlink()
    first = await _sync(hardening_env)
    assert first["removed_stale"] == 0
    now = time.time()
    monkeypatch.setattr("core.logs_db.indexer.time.time", lambda: now + 61)
    assert (await _sync(hardening_env))["removed_stale"] == 1


async def test_append_checkpoint_continuation_rotation_and_partial_line(hardening_env):
    config, root, file, uploads = hardening_env
    await _sync(hardening_env)
    conn = await logs_db.get_logs_connection()
    original = await (await conn.execute("SELECT id,generation FROM log_entries")).fetchone()
    with file.open("ab") as stream:
        stream.write(b"continuation-marker\n[2026-11-16 12:01:00] SAY: incomplete")
    assert (await _sync(hardening_env))["indexed_lines"] == 1
    rows = await (await conn.execute("SELECT id,generation,is_continuation,timestamp FROM log_entries ORDER BY line_no")).fetchall()
    assert rows[0]["id"] == original["id"] and rows[0]["generation"] == original["generation"]
    assert rows[1]["is_continuation"] == 1 and rows[1]["timestamp"] == "2026-11-16 12:00:00"
    with file.open("ab") as stream:
        stream.write(b"markerlast\n")
    assert (await _sync(hardening_env))["indexed_lines"] == 1
    assert (await logs_db.search_logs_fts(query="incompletemarkerlast", preset="all"))[1] == 1
    file.rename(file.with_suffix(".old"))
    file.write_text(LINE.replace("markerfirst", "rotatedmarker"), encoding="utf-8")
    await _sync(hardening_env)
    assert (await logs_db.search_logs_fts(query="markerfirst", preset="all"))[1] == 0
    assert (await logs_db.search_logs_fts(query="rotatedmarker", preset="all"))[1] == 1
    file.write_text("[2026-11-16 12:02:00] SAY: short\n", encoding="utf-8")
    await _sync(hardening_env)
    assert (await logs_db.search_logs_fts(query="short", preset="all"))[1] == 1


async def test_failed_rebuild_keeps_old_generation_and_resumes(hardening_env, monkeypatch):
    await _sync(hardening_env)
    from core.logs_db import indexer
    original = indexer._publish_meta
    def fail(*_):
        raise RuntimeError("simulated interruption before publication")
    monkeypatch.setattr(indexer, "_publish_meta", fail)
    result = await _sync(hardening_env, force_reindex=True)
    assert result["errors"] == 1
    assert (await logs_db.search_logs_fts(query="markerfirst", preset="all"))[1] == 1
    monkeypatch.setattr(indexer, "_publish_meta", original)
    await _sync(hardening_env, force_reindex=True)
    assert (await logs_db.search_logs_fts(query="markerfirst", preset="all"))[1] == 1


async def test_fast_page_reads_only_selected_offsets(hardening_env, monkeypatch):
    config, _, file, _ = hardening_env
    file.write_text(LINE * 1000, encoding="utf-8")
    await _sync(hardening_env)
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    original_open = Path.open
    reads = []
    class Reader:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *_): self.stream.close()
        def __getattr__(self, name): return getattr(self.stream, name)
        def read(self, size=-1):
            reads.append(size)
            return self.stream.read(size)
    def opened(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return Reader(stream) if path == file else stream
    monkeypatch.setattr(Path, "open", opened)
    page = await asyncio.to_thread(_indexed_page, config.resolved_logs_db_path(), uid, file, {"preset": "all", "per_page": 25, "page": 4})
    assert page.total_matches == 1000 and page.entries[0].line_no == 76
    assert len(reads) == 25 and sum(reads) == 25 * (file.stat().st_size // 1000)


def test_streaming_pagination_memory_and_focus(tmp_path):
    file = tmp_path / "large.log"
    file.write_text(LINE * 100000, encoding="utf-8")
    tracemalloc.start()
    page = filter_game_log_file(file, preset="all", per_page=25)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(page.entries) == 25 and page.total_matches == 100000
    assert peak < 3 * 1024 * 1024
    assert filter_game_log_file(file, preset="all", per_page=25, focus_line=80).entries[0].line_no == 76
    assert filter_game_log_file(file, preset="all", per_page=25, page=999999).page == 4000


async def test_regex_deadline_cancellation_slots_and_live(hardening_env):
    config, _, file, _ = hardening_env
    config.logs_regex_timeout_seconds = .5
    file.write_text("[2026-11-16 12:00:00] SAY: " + "a" * 100 + "!\n", encoding="utf-8")
    from fastapi import HTTPException
    task = asyncio.create_task(run_log_operation(None, config, filter_game_log_file, file, q="(a|aa)+$", use_regex=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test") as client:
        assert (await client.get("/live")).status_code == 200
    with pytest.raises(HTTPException) as exc:
        await task
    assert exc.value.status_code == 408
    async with heavy_slot(config), heavy_slot(config):
        with pytest.raises(HTTPException) as full:
            async with heavy_slot(config): pass
        assert full.value.status_code == 429
    config.logs_regex_timeout_seconds = 5
    task = asyncio.create_task(run_log_operation(None, config, filter_game_log_file, file, q="(a|aa)+$", use_regex=True))
    await asyncio.sleep(.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert not multiprocessing.active_children()
    async with heavy_slot(config), heavy_slot(config): pass


async def test_regex_http_error_fragment(hardening_env):
    config, _, file, _ = hardening_env
    file.write_text("[2026-11-16 12:00:00] SAY: " + "a" * 100 + "!\n", encoding="utf-8")
    await _sync(hardening_env)
    config.logs_regex_timeout_seconds = .5
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test", cookies={discord_auth.SESSION_COOKIE_NAME: _cookie(config)}) as client:
        response = await client.get(f"/logs/round/{uid}", params={"q": "(a|aa)+$", "regex": "1"}, headers={"HX-Request": "true"})
        assert response.status_code == 408 and 'role="alert"' in response.text
        assert "<!DOCTYPE" not in response.text


async def test_backup_restore_portable_and_rejects_corruption(hardening_env, tmp_path):
    config, _, _, uploads = hardening_env
    created = await logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads)
    await _sync(hardening_env)
    archive = await asyncio.to_thread(create_backup, config, tmp_path / "backups")
    manifest = verify_archive(archive)
    assert any(name.endswith(".hilleye_meta.json") for name in manifest["files"])
    assert not any("config.json" in name for name in manifest["files"])
    overrides = await asyncio.to_thread(restore_backup, archive, tmp_path / "restored")
    with closing(sqlite3.connect(overrides["HILLEYE_LOGS_DB_PATH"])) as conn:
        path = conn.execute("SELECT dir_path FROM log_rounds WHERE round_uid=?", (created[0]["round_uid"],)).fetchone()[0]
        assert (Path(path) / "game.log").read_text(encoding="utf-8") == LINE
        assert conn.execute("SELECT COUNT(*) FROM log_entries_fts WHERE log_entries_fts MATCH 'markerfirst'").fetchone()[0] == 2
    await logs_db.init_logs_db(overrides["HILLEYE_LOGS_DB_PATH"])
    config.logs_db_path = overrides["HILLEYE_LOGS_DB_PATH"]
    config.logs_uploads_dir = overrides["HILLEYE_LOGS_UPLOADS_DIR"]
    config.logs_root_dir = overrides["HILLEYE_LOGS_DIR"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test", cookies={discord_auth.SESSION_COOKIE_NAME: _cookie(config)}) as client:
        uid = created[0]["round_uid"]
        assert (await client.get(f"/logs/round/{uid}")).status_code == 200
        assert (await client.get(f"/api/v1/logs/rounds/{uid}/download?mode=raw")).content == LINE.encode()
    with pytest.raises(ValueError, match="empty"):
        restore_backup(archive, tmp_path / "restored")
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(archive.read_bytes()[:100])
    with pytest.raises(Exception): verify_archive(bad)


async def test_backup_failure_keeps_prior_archives(hardening_env, tmp_path):
    config, root, _, _ = hardening_env
    await _sync(hardening_env)
    archive = await asyncio.to_thread(create_backup, config, tmp_path / "backups")
    root.rename(root.with_name("offline"))
    with pytest.raises(ValueError):
        await asyncio.to_thread(create_backup, config, tmp_path / "backups")
    assert archive.is_file()
    assert not list(archive.parent.glob(".backup-*"))


def test_backup_and_restore_preupgrade_schema(tmp_path):
    uploads = tmp_path / "uploads" / "u-old"
    uploads.mkdir(parents=True)
    (uploads / "game.log").write_bytes(LINE.encode())
    tracker, logs = tmp_path / "old-tracker.db", tmp_path / "old-logs.db"
    with closing(sqlite3.connect(tracker)) as conn, conn:
        conn.execute("CREATE TABLE preserved(value TEXT)")
        conn.execute("INSERT INTO preserved VALUES ('before-upgrade')")
    with closing(sqlite3.connect(logs)) as conn, conn:
        conn.execute("CREATE TABLE log_rounds(round_uid TEXT PRIMARY KEY,source_type TEXT,dir_path TEXT,indexed_at REAL)")
        conn.execute("INSERT INTO log_rounds VALUES ('u-old','upload',?,1)", (str(uploads),))
    config = AppConfig(db_path=str(tracker), logs_db_path=str(logs), logs_uploads_dir=str(uploads.parent))
    archive = create_backup(config, tmp_path / "backups")
    restored = restore_backup(archive, tmp_path / "restored-old")
    with closing(sqlite3.connect(restored["HILLEYE_DB_PATH"])) as conn:
        assert conn.execute("SELECT value FROM preserved").fetchone()[0] == "before-upgrade"
    assert (tmp_path / "restored-old" / "uploaded_logs" / "u-old" / "game.log").read_bytes() == LINE.encode()


async def test_map_failed_publication_preserves_old_version(hardening_env, monkeypatch):
    from core import map_renderer
    from core.map_renderer import renderer
    config = hardening_env[0]
    repo = Path(config.maps_cache_dir).parent / "repo"
    folder = repo / "maps" / "sierra"
    folder.mkdir(parents=True)
    (folder / "z1-z4_sierra.dmm").write_text('"a" = (/turf/space,/area/space)\n(1,1,1) = {"\naa\naa\n"}\n', encoding="utf-8")
    config.maps_repo_dir = str(repo)
    first = await map_renderer.build_map_cache(config, force=True)
    old_image = map_renderer.get_map_image_path(config.resolved_maps_cache_dir(), 1)
    original = renderer._render_maps_from_repo_sync
    entered, release = threading.Event(), threading.Event()
    def fail(*args, **kwargs):
        entered.set()
        release.wait(5)
        raise RuntimeError("simulated rendering failure")
    monkeypatch.setattr(renderer, "_render_maps_from_repo_sync", fail)
    task = asyncio.create_task(map_renderer.build_map_cache(config, force=True))
    assert await asyncio.to_thread(entered.wait, 5)
    assert old_image.is_file() and map_renderer.get_map_manifest(config.resolved_maps_cache_dir()) == first
    release.set()
    with pytest.raises(RuntimeError): await task
    assert map_renderer.get_map_manifest(config.resolved_maps_cache_dir()) == first
    monkeypatch.setattr(renderer, "_render_maps_from_repo_sync", original)
    second = await map_renderer.build_map_cache(config, force=True)
    assert second["levels"][0]["image_file"] != first["levels"][0]["image_file"]
    assert old_image.is_file()


async def test_failed_logs_migration_is_atomic(hardening_env, tmp_path, monkeypatch):
    from core.logs_db import connection
    await logs_db.close_logs_db()
    path = tmp_path / "migration.db"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE marker(value TEXT)")
        conn.execute("INSERT INTO marker VALUES ('preserve')")
    monkeypatch.setattr(logs_db, "LOGS_SCHEMA_SQL", logs_db.LOGS_SCHEMA_SQL + "\nTHIS IS INVALID SQL;\n")
    with pytest.raises(sqlite3.OperationalError): await logs_db.init_logs_db(str(path))
    assert logs_db._connection is None
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "preserve"
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='log_rounds'").fetchone() is None


async def test_cancelled_upload_removes_only_its_staging(hardening_env, monkeypatch):
    from core.logs_db import uploads as module
    config, _, _, uploads = hardening_env
    keep = uploads / "keep.txt"
    keep.write_text("preserve", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()
    original = module._prepare_upload
    def slow(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, "_prepare_upload", slow)
    task = asyncio.create_task(logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert keep.read_text() == "preserve"
    assert sorted(p.name for p in uploads.iterdir()) == ["keep.txt"]


async def test_cross_process_lock_is_released(hardening_env):
    config = hardening_env[0]
    path = lock_path(config.resolved_logs_db_path(), "heavy-0")
    lock = FileLock(path)
    assert lock.acquire()
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    child = context.Process(target=_lock_probe, args=(str(path), queue))
    child.start()
    assert await asyncio.to_thread(queue.get, True, 10) is False
    await asyncio.to_thread(child.join, 10)
    lock.release()
    child.close()
    queue.close()
    with FileLock(path): pass


async def test_jobs_coalesce_authorize_and_periodically_index(hardening_env):
    config, _, file, _ = hardening_env
    config.logs_periodic_scan = True
    config.logs_scan_interval_seconds = 1
    runner = JobRunner(config)
    other = JobRunner(config)
    web.app.state.log_jobs = runner
    job = await runner.submit("refresh", "111111111111111111")
    assert await other.submit("refresh", "222222222222222222") == job
    assert get_job(runner.path, job, user_id="222222222222222222")
    assert get_job(runner.path, job, user_id="333333333333333333") is None
    await runner.start()
    await other.start()
    try:
        await asyncio.wait_for(runner.wait(job), 10)
        with file.open("a", encoding="utf-8") as stream: stream.write(LINE.replace("markerfirst", "automaticmarker"))
        async def wait_search():
            while (await logs_db.search_logs_fts(query="automaticmarker", preset="all"))[1] != 1:
                await asyncio.sleep(.1)
        await asyncio.wait_for(wait_search(), 10)
        token = _cookie(config)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test", cookies={discord_auth.SESSION_COOKIE_NAME: token}) as client:
            assert (await client.get(f"/api/v1/logs/jobs/{job}")).json()["state"] == "succeeded"
            assert (await client.get("/api/v1/logs/status")).status_code == 200
            csrf = discord_auth.verify_payload(token, config)["csrf"]
            submitted = await client.post("/logs/refresh", data={"background": "1", "csrf_token": csrf}, headers={"Accept": "application/json"})
            assert submitted.status_code == 202
            await asyncio.wait_for(runner.wait(submitted.json()["job_id"]), 10)
            rejected = await client.post("/logs/reindex", data={"background": "1", "csrf_token": "wrong"})
            assert rejected.status_code == 403
    finally:
        await runner.stop()
        await other.stop()
