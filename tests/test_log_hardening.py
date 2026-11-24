"""End-to-end and failure-path regressions for the log hardening rollout."""
import asyncio
import hashlib
import io
import json
import multiprocessing
import os
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
from core.log_parser import filter_game_log_file, read_context_lines
from core.logs_db.indexer import DiscoveryResult
from core.logs_db.reader import _indexed_page, read_log_context
from core.maintenance import create_backup, prune_logs, restore_backup, verify_archive
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


async def test_removed_game_file_clears_index_but_preserves_qdel(hardening_env):
    _, _, game, _ = hardening_env
    await _sync(hardening_env)
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    (game.parent / "qdel.log").write_text("Path: /obj", encoding="utf-8")
    game.unlink()
    await _sync(hardening_env)
    round_record = await logs_db.get_round_by_uid(uid)
    assert not round_record["has_game_log"] and round_record["has_qdel_log"]
    assert round_record["line_count"] == 0
    assert await logs_db.search_logs_fts(query="markerfirst") == ([], 0)


async def test_index_detects_early_edit_in_file_that_also_grew(hardening_env):
    _, _, game, _ = hardening_env
    game.write_text(LINE + ("extra line\n" * 500), encoding="utf-8")
    await _sync(hardening_env)
    with game.open("r+b") as stream:
        stream.seek(LINE.index("markerfirst"))
        stream.write(b"replacement")
        stream.seek(0, 2)
        stream.write(b"appended\n")
    assert not (await _sync(hardening_env))["errors"]
    assert (await logs_db.search_logs_fts(query="markerfirst"))[1] == 0
    assert (await logs_db.search_logs_fts(query="replacement"))[1] == 1
    assert (await logs_db.search_logs_fts(query="appended"))[1] == 1


async def test_settled_disk_line_is_searchable_and_rescanned_if_later_completed(hardening_env):
    _, _, game, _ = hardening_env
    game.write_text(LINE + "[2026-11-16 12:00:01] SAY: unfinished", encoding="utf-8")
    await _sync(hardening_env)
    assert (await logs_db.search_logs_fts(query="unfinished"))[1] == 0
    old = time.time() - 600
    os.utime(game, (old, old))
    await _sync(hardening_env)
    assert (await logs_db.search_logs_fts(query="unfinished"))[1] == 1
    with game.open("a", encoding="utf-8") as stream:
        stream.write("message\n")
    await _sync(hardening_env)
    assert (await logs_db.search_logs_fts(query="unfinished"))[1] == 0
    assert (await logs_db.search_logs_fts(query="unfinishedmessage"))[1] == 1


async def test_upload_completes_while_disk_discovery_is_waiting(hardening_env, monkeypatch):
    from core.logs_db import indexer

    _, _, _, uploads = hardening_env
    entered, release = threading.Event(), threading.Event()
    original = indexer._discover_dirs_with_logs

    def slow_disk(root, kind, **options):
        if kind == "disk":
            entered.set()
            release.wait(5)
        return original(root, kind, **options)

    monkeypatch.setattr(indexer, "_discover_dirs_with_logs", slow_disk)
    scan = asyncio.create_task(_sync(hardening_env))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert await asyncio.wait_for(
            logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads), timeout=3,
        )
        assert not scan.done()
    finally:
        release.set()
        await scan


async def test_upload_delete_retries_quarantined_files(hardening_env, monkeypatch):
    from core.logs_db import uploads as upload_module

    _, _, _, uploads = hardening_env
    await _sync(hardening_env)
    record, = await logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads)
    uid = record["round_uid"]
    path = Path(record["dir_path"])
    real_remove = upload_module.shutil.rmtree

    def fail_quarantine(path, *args, **kwargs):
        if Path(path).name == uid:
            raise PermissionError("cleanup temporarily unavailable")
        return real_remove(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(upload_module.shutil, "rmtree", fail_quarantine)
        assert await logs_db.delete_uploaded_round(uid, uploads_dir=uploads)
        assert not path.exists()
        assert await logs_db.get_round_by_uid(uid) is None
    await _sync(hardening_env)
    assert await logs_db.get_round_by_uid(uid) is None
    assert not (uploads / upload_module._DELETED_UPLOADS / uid).exists()


async def test_upload_batch_is_hidden_until_recovered_and_duplicate_resolves_to_same_rounds(hardening_env, monkeypatch):
    from core.logs_db import indexer
    from core.logs_db.uploads import UploadPendingError

    config, _, _, uploads = hardening_env
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as writer:
        writer.writestr("first/game.log", LINE)
        writer.writestr("second/game.log", LINE.replace("markerfirst", "markersecond"))
    original = indexer._publish_meta

    def fail_second(conn, item, generation, checkpoint):
        if item["round_uid"].endswith("-second"):
            raise OSError("simulated interruption")
        return original(conn, item, generation, checkpoint)

    with monkeypatch.context() as patch:
        patch.setattr(indexer, "_publish_meta", fail_second)
        with pytest.raises(UploadPendingError):
            await logs_db.ingest_uploaded_log("archive.zip", archive.getvalue(), uploads_dir=uploads)
        assert logs_status(config.resolved_logs_db_path())["pending_imports"] == 1
        assert (await logs_db.list_rounds(source_filter="upload"))[1] == 0
        assert await logs_db.search_logs_fts(query="markerfirst") == ([], 0)
        with pytest.raises(UploadPendingError):
            await logs_db.ingest_uploaded_log("archive.zip", archive.getvalue(), uploads_dir=uploads)
        assert logs_status(config.resolved_logs_db_path())["pending_imports"] == 1
    await _sync(hardening_env)
    assert logs_status(config.resolved_logs_db_path())["pending_imports"] == 0
    first = (await logs_db.list_rounds(source_filter="upload"))[0]
    repeated = await logs_db.ingest_uploaded_log("archive.zip", archive.getvalue(), uploads_dir=uploads)
    assert len(first) == len(repeated) == 2
    assert {record["round_uid"] for record in repeated} == {record["round_uid"] for record in first}


async def test_duplicate_legacy_upload_reuses_its_existing_uid(hardening_env):
    _, _, _, uploads = hardening_env
    folder = uploads / "u-legacy"
    folder.mkdir()
    (folder / "game.log").write_bytes(LINE.encode())
    await _sync(hardening_env)
    record, = await logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads)
    assert record["round_uid"] == "u-legacy"
    assert (await logs_db.list_rounds(source_filter="upload"))[1] == 1


async def test_upload_quota_rejects_new_contents_but_allows_an_exact_retry(hardening_env):
    from core.logs_db.uploads import UploadQuotaError

    _, _, _, uploads = hardening_env
    record, = await logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads)
    repeated, = await logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads, quota_bytes=1)
    assert repeated["round_uid"] == record["round_uid"]
    with pytest.raises(UploadQuotaError):
        await logs_db.ingest_uploaded_log("game.log", b"another log\n", uploads_dir=uploads, quota_bytes=1)
    assert (await logs_db.list_rounds(source_filter="upload"))[1] == 1
    assert not list(uploads.glob(".staging-*"))


async def test_deleted_upload_cannot_return_after_its_directory_reappears(hardening_env, tmp_path):
    _, _, _, uploads = hardening_env
    record, = await logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads)
    path = Path(record["dir_path"])
    offline = tmp_path / "offline"
    path.parent.rename(offline)
    assert await logs_db.delete_uploaded_round(record["round_uid"], uploads_dir=uploads)
    offline.rename(path.parent)
    await _sync(hardening_env)
    assert not path.exists()
    assert await logs_db.get_round_by_uid(record["round_uid"]) is None


async def test_forced_reindex_resume_keeps_previously_published_round_generation(hardening_env, monkeypatch):
    from core.logs_db import indexer

    config, root, game, _ = hardening_env
    another = root / "another"
    another.mkdir()
    (another / "game.log").write_text(LINE, encoding="utf-8")
    await _sync(hardening_env)
    job = enqueue_job(config.resolved_logs_db_path(), "reindex")
    original = indexer._publish_meta

    def fail_one(conn, item, generation, checkpoint):
        if Path(item["dir_path"]) == game.parent:
            raise OSError("interrupted")
        return original(conn, item, generation, checkpoint)

    with monkeypatch.context() as patch:
        patch.setattr(indexer, "_publish_meta", fail_one)
        assert (await _sync(hardening_env, force_reindex=True, job_id=job))["errors"] == 1
    records, _ = await logs_db.list_rounds()
    completed = next(item for item in records if Path(item["dir_path"]) == another)
    result = await _sync(hardening_env, force_reindex=True, job_id=job)
    assert result["errors"] == 0 and result["indexed_rounds"] == 2
    assert (await logs_db.get_round_by_uid(completed["round_uid"]))["active_generation"] == completed["active_generation"]


async def test_qdel_api_paginates_sorted_entries_and_html_paginates_raw(hardening_env):
    config, _, game, _ = hardening_env
    qdel = game.with_name("qdel.log")
    qdel.write_text("".join(f"Path: /obj/{i:03}\n - qdel() Count: {i}\n" for i in range(125)), encoding="utf-8")
    await _sync(hardening_env)
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app), base_url="http://test",
        cookies={discord_auth.SESSION_COOKIE_NAME: _cookie(config)},
    ) as client:
        response = await client.get(f"/api/v1/logs/rounds/{uid}/qdel",
                                    params={"page": 2, "sort_by": "qdel_count", "per_page": 100})
        data = response.json()
        assert response.status_code == 200 and data["total_pages"] == 2 and data["total_paths"] == 125
        assert [entry["qdel_count"] for entry in data["entries"]] == list(range(24, -1, -1))
        html = await client.get(f"/logs/round/{uid}", params={
            "page": 2, "file": "qdel.log", "qdel_view": "raw", "per_page": 100,
        })
        assert html.status_code == 200
        assert "Path: /obj/050" in html.text and "Path: /obj/000" not in html.text
        assert "Страница 2 из 3" in html.text


async def test_log_retention_reports_candidates_before_deleting(hardening_env):
    config, _, _, uploads = hardening_env
    config.logs_upload_retention_days = config.logs_audit_retention_days = config.logs_job_retention_days = 10
    record, = await logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads)
    uid = record["round_uid"]
    job_path = config.resolved_logs_db_path()
    finished_id, queued_id = enqueue_job(job_path, "refresh"), enqueue_job(job_path, "reindex")
    await logs_db.record_audit_event("view_round")
    conn = await logs_db.get_logs_connection()
    old = time.time() - 20 * 86400
    await conn.execute("UPDATE log_rounds SET uploaded_at=? WHERE round_uid=?", (old, uid))
    await conn.execute("UPDATE log_audit_events SET created_at=?", (old,))
    await conn.execute("UPDATE log_jobs SET state='partial',finished_at=? WHERE id=?", (old, finished_id))
    await conn.commit()
    preview = await asyncio.to_thread(prune_logs, config)
    assert preview["candidates"] == {"uploads": 1, "audit_events": 1, "jobs": 1, "map_versions": 0}
    assert not any(preview["removed"].values())
    assert (await logs_db.get_round_by_uid(uid)) is not None
    applied = await asyncio.to_thread(prune_logs, config, apply=True, optimize_index=True)
    assert applied["removed"] == preview["candidates"]
    assert applied["optimized_index"]
    assert await logs_db.get_round_by_uid(uid) is None
    assert not Path(record["dir_path"]).exists()
    assert get_job(job_path, finished_id, admin=True) is None
    assert get_job(job_path, queued_id, admin=True)["state"] == "queued"


async def test_upload_enforces_body_limit_without_content_length(hardening_env):
    config, _, _, _ = hardening_env
    config.logs_max_upload_mb = 1
    received = 0

    async def body():
        nonlocal received
        yield b'--audit-boundary\r\nContent-Disposition: form-data; name="file"; filename="game.log"\r\n\r\n'
        for _ in range(32):
            received += 1
            yield b"A" * (64 * 1024)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app), base_url="http://test",
        cookies={discord_auth.SESSION_COOKIE_NAME: _cookie(config)},
    ) as client:
        response = await client.post("/logs/upload", content=body(),
                                     headers={"Content-Type": "multipart/form-data; boundary=audit-boundary"})
    assert response.status_code == 413
    assert 0 < received < 32
    assert (await logs_db.list_rounds(source_filter="upload"))[1] == 0


async def test_memory_database_can_refresh_from_web_without_a_durable_runner(hardening_env):
    config, _, _, _ = hardening_env
    config.logs_db_path = ":memory:"
    await logs_db.init_logs_db(":memory:")
    token = _cookie(config)
    csrf = discord_auth.verify_payload(token, config)["csrf"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app), base_url="http://test",
        cookies={discord_auth.SESSION_COOKIE_NAME: token},
        headers={"Accept": "application/json"},
    ) as client:
        initial = await client.get("/api/v1/logs/status")
        assert initial.json()["status"] == "uninitialized"
        refresh = await client.post("/logs/refresh", data={"background": "1", "csrf_token": csrf})
        assert refresh.status_code == 200 and refresh.json()["ok"]
        assert (await client.get("/api/v1/logs/status")).json()["status"] == "ok"
        assert (await client.get("/api/v1/logs/search", params={"q": "markerfirst"})).json()["total"] == 1


async def test_disabled_disk_source_keeps_its_data_without_serving_it(hardening_env, tmp_path):
    config, root, _, uploads = hardening_env
    await _sync(hardening_env)
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    await logs_db.sync_and_index_rounds(None, uploads)
    assert await logs_db.get_round_by_uid(uid) is None
    assert await logs_db.search_logs_fts(query="markerfirst") == ([], 0)
    config.logs_root_dir = ""
    archive = await asyncio.to_thread(create_backup, config, tmp_path / "backups")
    assert uid not in verify_archive(archive)["round_paths"]
    config.logs_root_dir = str(root)
    await logs_db.sync_and_index_rounds(root, uploads)
    assert (await logs_db.search_logs_fts(query="markerfirst"))[1] == 1


async def test_backup_excludes_round_already_marked_missing(hardening_env, tmp_path):
    config, _, game, _ = hardening_env
    await _sync(hardening_env)
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    game.unlink()
    game.parent.rmdir()
    await _sync(hardening_env)
    assert (await logs_db.get_round_by_uid(uid))["missing_since"] is not None
    archive = await asyncio.to_thread(create_backup, config, tmp_path / "backups")
    assert uid not in verify_archive(archive)["round_paths"]
    restored = tmp_path / "restored"
    await asyncio.to_thread(restore_backup, archive, restored)
    with closing(sqlite3.connect(restored / "logs.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM log_rounds").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM log_entries").fetchone()[0] == 0


async def test_backup_rejects_raw_log_that_no_longer_matches_index(hardening_env, tmp_path):
    config, _, game, _ = hardening_env
    await _sync(hardening_env)
    game.write_text(LINE.replace("markerfirst", "replacement"), encoding="utf-8")
    with pytest.raises(ValueError, match="differs from the active index"):
        await asyncio.to_thread(create_backup, config, tmp_path / "backups")


async def test_celadon_import_uses_remote_identity_and_only_indexes_new_uploads(hardening_env):
    _, _, _, uploads = hardening_env
    other = uploads / "u-other"
    other.mkdir()
    (other / "game.log").write_text(LINE, encoding="utf-8")
    (other / ".hilleye_meta.json").write_text("[]", encoding="utf-8")
    record, = await logs_db.ingest_uploaded_log(
        "qdel.log", b"Path: /obj\n", uploads_dir=uploads,
        origin_path="2026/10/15/round-123/qdel.log",
    )
    assert record["round_id"] == "123" and record["round_date"] == "2026-10-15"
    assert await logs_db.get_round_by_uid("u-other") is None
    assert (await _sync(hardening_env))["errors"] == 1
    assert await logs_db.get_round_by_uid("u-other") is not None


async def test_masked_links_export_and_log_cache_policy(hardening_env):
    config, _, game, _ = hardening_env
    game.write_text(
        LINE + "[2026-11-16 12:00:01] SAY: sensitive 192&#46;<b>0</b>&#46;2.1 2001:db8::1 CID: 123456 (10,10,1)\n",
        encoding="utf-8",
    )
    await _sync(hardening_env)
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app), base_url="http://test",
        cookies={discord_auth.SESSION_COOKIE_NAME: _cookie(config)},
    ) as client:
        page = await client.get("/logs/search", params={"q": "sensitive", "mask_ips": "1"})
        assert page.status_code == 200
        assert f"/logs/round/{uid}?file=game.log&amp;mask_ips=1" in page.text
        assert "192.0.2.1" not in page.text and "2001:db8::1" not in page.text
        assert page.headers["cache-control"] == "private, no-store"
        api = await client.get("/api/v1/logs/rounds")
        assert "dir_path" not in api.json()["rounds"][0]
        assert api.headers["cache-control"] == "private, no-store"
        for mode in ("raw", "filtered"):
            result = await client.get(f"/api/v1/logs/rounds/{uid}/download",
                                      params={"mask_ips": True, "mode": mode})
            assert result.status_code == 200
            assert result.headers["cache-control"] == "private, no-store"
            assert "192.0.2.1" not in result.text and "2001:db8::1" not in result.text
            assert "123456" not in result.text and "sensitive" in result.text


async def test_background_debug_policy_persists_and_reports_scan_errors(hardening_env):
    config, root, game, uploads = hardening_env
    with game.open("a", encoding="utf-8") as stream:
        stream.write("[2026-11-16 12:00:01] DEBUG: debugmarker\n")
    runner = JobRunner(config)
    await runner.start()
    try:
        job_id = await runner.submit("reindex", payload={"include_debug": True})
        assert not (await asyncio.wait_for(runner.wait(job_id), 5))["errors"]
        await logs_db.ingest_uploaded_log("game.log", LINE.encode(), uploads_dir=uploads)
        assert {s["source"]: s["status"] for s in logs_status(runner.path)["sources"]}["disk"] == "ok"
        job_id = await runner.submit("refresh")
        assert not (await asyncio.wait_for(runner.wait(job_id), 5))["errors"]
        assert (await logs_db.search_logs_fts(query="debugmarker", preset="all"))[1] == 1
        root.rename(root.with_name("offline"))
        job_id = await runner.submit("refresh")
        assert (await asyncio.wait_for(runner.wait(job_id), 5))["errors"]
        assert get_job(runner.path, job_id, admin=True)["state"] == "partial"
    finally:
        await runner.stop()


async def test_global_search_matches_message_and_paginates_coordinate_raw_scan(hardening_env):
    config, _, game, _ = hardening_env
    game.write_text(
        "[2026-11-16 12:00:00] SAY: scanmarker wrongdeck (10,10,1)\n" * 520
        + "".join(f"[2026-11-16 12:00:00] SAY: scanmarker row-{i:04} (10,10,2)\n" for i in range(110)),
        encoding="utf-8",
    )
    await _sync(hardening_env)
    assert await logs_db.search_logs_fts(query="SAY", preset="all") == ([], 0)
    assert (await logs_db.search_logs_fts(query="scanmarker", offset=1000)) == ([], 630)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app), base_url="http://test",
        cookies={discord_auth.SESSION_COOKIE_NAME: _cookie(config)},
    ) as client:
        params = {"q": "scanmarker", "scan_debug": "1", "z_level": "2"}
        first = await client.get("/logs/search", params={**params, "page": 1})
        second = await client.get("/logs/search", params={**params, "page": 2})
        assert first.status_code == second.status_code == 200
        assert "Найдено совпадений: 110" in first.text and "Найдено совпадений: 110" in second.text
        assert "row-0000 " in first.text and "row-0100 " not in first.text
        assert "row-0000 " not in second.text and "row-0100 " in second.text
        assert "wrongdeck" not in first.text and "wrongdeck" not in second.text
        assert (await client.get("/logs/search", params={"date_from": "2026-02-30"})).status_code == 400
        assert (await client.get("/api/v1/logs/search", params={"ckey": "!"})).status_code == 400


async def test_search_result_links_reject_rotated_log_and_audit_api_reads(hardening_env):
    config, _, game, _ = hardening_env
    await _sync(hardening_env)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app, client=("192.0.2.10", 1234)),
        base_url="http://test", cookies={discord_auth.SESSION_COOKIE_NAME: _cookie(config)},
    ) as client:
        response = await client.get("/api/v1/logs/search", params={"q": "markerfirst"},
                                    headers={"X-Forwarded-For": "192.0.2.11"})
        hit = response.json()["hits"][0]
        uid = hit["round_uid"]
        link = {"focus": hit["line_no"], "entry_id": hit["entry_id"]}
        assert (await client.get(f"/logs/round/{uid}", params=link)).status_code == 200
        with game.open("a", encoding="utf-8") as stream:
            stream.write("appended continuation\n")
        assert (await client.get(f"/logs/round/{uid}", params=link)).status_code == 200
        download = await client.get(f"/api/v1/logs/rounds/{uid}/download?mode=raw")
        assert download.status_code == 200
        game.rename(game.with_suffix(".old"))
        game.write_text(LINE.replace("markerfirst", "replacement"), encoding="utf-8")
        assert (await client.get(f"/logs/round/{uid}", params=link)).status_code == 409
        assert (await client.get(f"/logs/round/{uid}/context", params={
            "line": hit["line_no"], "entry_id": hit["entry_id"],
        })).status_code == 409
    searches, _ = await logs_db.get_audit_events(action_filter="search_global")
    assert searches[0]["ip_address"] == "192.0.2.10"
    downloads, _ = await logs_db.get_audit_events(action_filter="download_log")
    assert {event["details"].rsplit("=", 1)[1] for event in downloads} == {"requested", "http_200"}

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


async def test_indexed_context_seeks_and_preserves_unindexed_lines(hardening_env, monkeypatch):
    from core.logs_db import reader

    _, _, file, _ = hardening_env
    file.write_text(
        LINE * 1000 + "SAY continuation\n"
        "[2026-11-16 12:00:01] DEBUG: 192.0.2.10 <b>debug</b>\n"
        "DEBUG continuation\n" + LINE,
        encoding="utf-8",
    )
    await _sync(hardening_env)
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    line_bytes = len(file.read_bytes().splitlines(keepends=True)[0])
    options = {"radius": 1, "mask_ips": True, "render_html": False}
    expected = read_context_lines(file, 1002, **options)
    original_stream = reader._iter_parsed_log_stream
    starts = []

    def tracked_stream(stream, **kwargs):
        starts.append((stream.tell(), kwargs["first_line"]))
        return original_stream(stream, **kwargs)

    monkeypatch.setattr(reader, "_iter_parsed_log_stream", tracked_stream)
    entries = await read_log_context(uid, file, 1002, **options)
    assert starts == [(line_bytes * 1000, 1001)]
    assert entries == expected
    assert [entry.category for entry in entries] == ["SAY", "DEBUG", "DEBUG"]

    with file.open("a", encoding="utf-8") as stream:
        stream.write("unindexed tail\n")
    assert await read_log_context(uid, file, 1005, **options) == read_context_lines(file, 1005, **options)
    assert starts[-1][1] == 1004


async def test_live_index_page_includes_tail_and_uses_original_offsets(hardening_env):
    config, _, file, _ = hardening_env
    file.write_text(LINE * 51, encoding="utf-8")
    await _sync(hardening_env)
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    with file.open("a", encoding="utf-8") as stream:
        stream.write("unindexed continuation\npartial live tail")
    indexed = await asyncio.to_thread(
        _indexed_page, config.resolved_logs_db_path(), uid, file, {"preset": "all", "per_page": 25, "focus_line": 53},
    )
    assert indexed is not None and indexed.total_matches == 53 and indexed.page == 3
    assert [entry.line_no for entry in indexed.entries] == [51, 52, 53]
    assert indexed.entries[-1].plain_message == "partial live tail"


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
    assert await logs_db.search_logs_fts(query="markerfirst") == ([], 0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test", cookies={discord_auth.SESSION_COOKIE_NAME: _cookie(config)}) as client:
        uid = created[0]["round_uid"]
        assert (await client.get(f"/logs/round/{uid}")).status_code == 200
        assert (await client.get(f"/api/v1/logs/rounds/{uid}/download?mode=raw")).content == LINE.encode()
    await logs_db.sync_and_index_rounds(config.resolved_logs_root_dir(), config.resolved_logs_uploads_dir())
    assert (await logs_db.search_logs_fts(query="markerfirst"))[1] == 2
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


async def test_round_map_pins_assets_and_area_labels_to_recorded_revision(hardening_env):
    from PIL import Image

    config, _, game, _ = hardening_env
    cache = config.resolved_maps_cache_dir()
    old_version, current_version = "1" * 32, "2" * 32
    old_revision, current_revision = "a" * 40, "b" * 40
    for version, revision, area, color in (
        (old_version, old_revision, "Old bridge", "red"),
        (current_version, current_revision, "Current bridge", "blue"),
    ):
        folder = cache / "versions" / version
        folder.mkdir(parents=True)
        manifest = {"ready": True, "commit": revision, "tile_px": 1, "repo_dir": "internal-location",
                    "levels": [{"z": 1, "image_file": "z1.png", "areas_file": "areas_z1.json"}]}
        (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (folder / "areas_z1.json").write_text(
            json.dumps({"width": 16, "height": 16, "palette": [area], "rle": [[256, 0]]}), encoding="utf-8",
        )
        Image.new("RGB", (16, 16), color).save(folder / "z1.png")
    current = {**manifest, "version": current_version, "levels": [
        {"z": 1, "image_file": f"versions/{current_version}/z1.png",
         "areas_file": f"versions/{current_version}/areas_z1.json"},
    ]}
    (cache / "manifest.json").write_text(json.dumps(current), encoding="utf-8")
    extra = cache / "versions" / ("3" * 32)
    extra.mkdir()
    (extra / "manifest.json").write_text(json.dumps({**manifest, "commit": "d" * 40}), encoding="utf-8")
    config.maps_retention_days = 30
    expired = time.time() - 60 * 86400
    for folder in (extra, cache / "versions" / old_version, cache / "versions" / current_version):
        os.utime(folder, (expired, expired))
    game.write_text(f"[2026-11-16 11:59:00] SYSTEM: Git revision: {old_revision}\n" + LINE, encoding="utf-8")
    await _sync(hardening_env)
    assert (await asyncio.to_thread(prune_logs, config))["candidates"]["map_versions"] == 1
    assert (await asyncio.to_thread(prune_logs, config, apply=True))["removed"]["map_versions"] == 1
    assert not extra.exists()
    uid = (await logs_db.list_rounds())[0][0]["round_uid"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=web.app), base_url="http://test",
        cookies={discord_auth.SESSION_COOKIE_NAME: _cookie(config)},
    ) as client:
        response = await client.get(f"/api/v1/logs/rounds/{uid}/map-events")
        data = response.json()
        assert response.status_code == 200 and data["manifest"]["version"] == old_version
        assert data["manifest"]["binding"] == "revision" and data["events"][0]["area"] == "Old bridge"
        assert "repo_dir" not in data["manifest"]
        old_tile = await client.get("/logs/maps/z/1.png", params={"version": old_version})
        current_tile = await client.get("/logs/maps/z/1.png")
        assert old_tile.status_code == current_tile.status_code == 200 and old_tile.content != current_tile.content
        old_areas = await client.get("/logs/maps/areas/1.json", params={"version": old_version})
        assert old_areas.json()["palette"] == ["Old bridge"]
        searches = await client.get("/api/v1/logs/search", params={"q": "markerfirst"})
        assert searches.json()["hits"][0]["area"] == "Old bridge"
        game.write_text(f"[2026-11-16 11:59:00] SYSTEM: Git revision: {'c' * 40}\n" + LINE, encoding="utf-8")
        await _sync(hardening_env)
        data = (await client.get(f"/api/v1/logs/rounds/{uid}/map-events")).json()
        assert data["manifest"]["binding"] == "unavailable" and not data["manifest"]["ready"]
        assert data["events"][0]["area"] == ""


def test_map_pages_filter_deck_and_keep_a_focused_event(tmp_path):
    from core.log_parser import collect_round_map_events

    file = tmp_path / "game.log"
    file.write_text("".join(
        f"[2026-11-16 12:00:00] SAY: event {index} (10,10,{index % 2 + 1})\n"
        for index in range(17)
    ), encoding="utf-8")
    first = collect_round_map_events(file, z_filter=2, limit=3, focus_line=16)
    assert first["total_with_coords"] == 17 and first["selected_count"] == 8
    assert first["next_offset"] == 3 and first["previous_offset"] is None
    assert [event["line_no"] for event in first["events"]] == [2, 4, 6, 16]
    second = collect_round_map_events(file, z_filter=2, limit=3, offset=first["next_offset"])
    assert second["next_offset"] == 6 and second["previous_offset"] == 0
    assert [event["line_no"] for event in second["events"]] == [8, 10, 12]


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
