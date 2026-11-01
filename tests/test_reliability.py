"""Regression proofs for BUGS_AND_RELIABILITY.md; no production network or data."""

import asyncio
import json
import time
import subprocess
import sys
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
from pydantic import ValidationError

from core import db
from core.byond_topic import parse_round_duration, parse_topic_query_string, query_topic
from core.config import AppConfig, load_config
from core.hub_scraper import ServerInfo, parse_hub_json
from core.tracker import Tracker
from core.collector_lock import CollectorLock


async def seed(now=1000):
    sid = await db.upsert_server("byond://127.0.0.1:1234", "Test", "127.0.0.1", 1234, now_ts=now)
    await db.update_player_sessions(sid, {"alice"}, now_ts=now)
    return sid


@pytest.mark.parametrize("name", ["alice", "gapalice", "reconnectbob"])
async def test_unobserved_hour_is_not_playtime(test_db, name):
    sid = await seed()
    await db.update_player_sessions(sid, {name}, now_ts=1000)
    await db.update_player_sessions(sid, {name}, now_ts=4600)
    dossier = await db.get_player_dossier(name)
    assert dossier["total_playtime_seconds"] == 0
    assert dossier["sessions_count"] == 2


async def test_concurrent_session_updates_are_unique(test_db):
    sid = await seed()
    await asyncio.gather(*(db.update_player_sessions(sid, {"bob"}, now_ts=1100) for _ in range(8)))
    conn = await db.get_connection()
    rows = await (await conn.execute("SELECT id FROM sessions WHERE ckey='bob' AND ended_at IS NULL")).fetchall()
    assert len(rows) == 1


async def test_failed_write_cannot_be_committed_by_another_operation(test_db):
    sid = await seed()
    conn = await db.get_connection()
    await conn.execute("CREATE TRIGGER reject_bob BEFORE INSERT ON sessions WHEN NEW.ckey='bob' BEGIN SELECT RAISE(ABORT, 'injected'); END")
    await conn.commit()
    with pytest.raises(aiosqlite.IntegrityError):
        await db.update_player_sessions(sid, {"bob"}, now_ts=1100)
    await db.upsert_server("other", "Other", "127.0.0.1", 1235)
    assert await db.get_player_dossier("bob") is None


@pytest.mark.parametrize("response", [None, {"players": "5"}, {"version": "516"}])
async def test_unknown_roster_does_not_confirm_departure(test_db, monkeypatch, response):
    sid = await seed()
    monkeypatch.setattr("core.tracker.fetch_hub_servers", AsyncMock(return_value=[ServerInfo("byond://127.0.0.1:1234", "Test", "127.0.0.1", 1234)]))
    monkeypatch.setattr("core.tracker.query_topic", AsyncMock(return_value=response))
    tracker = Tracker(config={"custom_servers": [{"ip": "127.0.0.1", "port": 1234}]})
    await tracker.poll_once(now_ts=1400)
    conn = await db.get_connection()
    row = await (await conn.execute("SELECT ended_at FROM sessions WHERE server_id=? AND ckey='alice'", (sid,))).fetchone()
    assert row[0] is None
    assert await db.get_player_dossier("5") is None


async def test_fresh_empty_topic_overrides_hub_roster(test_db, monkeypatch):
    await seed()
    srv = ServerInfo("byond://127.0.0.1:1234", "Test", "127.0.0.1", 1234, ["alice"], 20)
    srv.trusted = True
    monkeypatch.setattr("core.tracker.fetch_hub_servers", AsyncMock(return_value=[srv]))
    monkeypatch.setattr("core.tracker.query_topic", AsyncMock(return_value={"players": 0, "playerlist": []}))
    await Tracker(config={"custom_servers": []}).poll_once(now_ts=1400)
    assert not (await db.get_player_dossier("alice"))["is_online"]
    assert (await db.get_active_servers())[0]["current_players"] == 0


async def test_socket_budget_starts_after_semaphore(mock_topic_server):
    mock_topic_server.set_string_response("players=0")
    gate = asyncio.Semaphore(0)
    task = asyncio.create_task(query_topic("127.0.0.1", mock_topic_server.port, timeout=0.05, semaphore=gate))
    await asyncio.sleep(0.1)
    gate.release()
    assert await task == {"players": 0}


def test_nested_config_and_explicit_precedence():
    cfg = AppConfig(tracker={"poll_interval_seconds": 42, "socket_timeout_seconds": 7, "max_concurrent_sockets": 2, "byond_hub_url": "https://example.com"}, database={"path": "other.db"})
    assert (cfg.poll_interval, cfg.socket_timeout, cfg.topic_concurrency, cfg.db_path) == (42, 7, 2, "other.db")
    assert cfg.hub_url == "https://example.com"
    assert AppConfig(poll_interval=50, tracker={"poll_interval_seconds": 42}).poll_interval == 50


@pytest.mark.parametrize("field,value", [("topic_concurrency", 0), ("topic_concurrency", -1), ("poll_interval", 0), ("socket_timeout", float("inf"))])
def test_invalid_config_fails(field, value):
    with pytest.raises(ValidationError):
        AppConfig(**{field: value})


def test_invalid_config_does_not_silently_discard_custom_servers(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"topic_concurrency": "bad", "custom_servers": [{"ip": "127.0.0.1", "port": 1234}]}))
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("value", [float("inf"), float("nan"), "1e999", "inf", "9" * 5000])
def test_invalid_duration_is_isolated(value):
    assert parse_round_duration(value) is None


def test_extreme_index_and_associative_single_player():
    parsed = parse_topic_query_string("player" + "9" * 5000 + "=Bad&player0=Alice")
    assert parsed["playerlist"] == ["Alice"]
    assert parse_topic_query_string("playerlist=Alice%3D1")["playerlist"] == ["Alice"]


def test_one_bad_hub_record_preserves_good_records():
    servers = parse_hub_json([{"url": "byond://8.8.8.8:1234", "players": ["Alice", None, True, {"name": "Bob"}]}, {"url": "byond://1.1.1.1:1234", "players": float("inf")}])
    assert servers[0].players == ["Alice"]
    assert len(servers) >= 1


async def test_backwards_observation_cannot_rewind_session(test_db):
    sid = await seed()
    await db.update_player_sessions(sid, {"alice"}, now_ts=1100)
    await db.update_player_sessions(sid, {"alice"}, now_ts=900)
    conn = await db.get_connection()
    row = await (await conn.execute("SELECT started_at,last_seen_at,duration_seconds FROM sessions WHERE ckey='alice'")).fetchone()
    assert tuple(row) == (1000, 1100, 100)


async def test_history_window_and_raw_statistics(test_db):
    now = time.time()
    sid = await seed(now)
    await db.record_server_snapshot(sid, 90, ts=now - 31 * 86400)
    await db.record_server_snapshot(sid, 99, ts=now + 100)
    assert (await db.get_server_details(sid))["snapshots_24h"] == []
    for i in range(49):
        await db.record_server_snapshot(sid, 50 if i == 48 else 0, ts=now - 100 + i)
    chart = (await db.get_server_details(sid, now_ts=now))["chart_24h"]
    assert chart["min_players"] == 0
    assert chart["max_players"] == 50
    assert chart["avg_players"] == 1.0


async def test_health_reports_stopped_tracker(async_client):
    response = await async_client.get("/health")
    assert response.status_code == 503
    assert response.json()["status"] != "ok"
    assert (await async_client.get("/live")).status_code == 200


async def test_search_navigation_and_missing_permalink(async_client):
    response = await async_client.post("/search", data={"ckey": "ghost"})
    assert response.status_code == 303
    assert response.headers["location"] == "/player/ghost"
    response = await async_client.post("/search", data={"ckey": "ghost"}, headers={"HX-Request": "true"})
    assert response.headers["hx-push-url"] == "/player/ghost"
    response = await async_client.get("/player/ghost")
    assert response.status_code == 404
    assert "ghost" in response.text


async def test_initialization_failure_closes_local_connection(tmp_path, monkeypatch):
    await db.close_db()
    opened = []
    original = aiosqlite.connect

    def capture(*args, **kwargs):
        conn = original(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(aiosqlite, "connect", capture)
    monkeypatch.setattr(db, "SCHEMA_SQL", "INVALID SQL;")
    try:
        with pytest.raises(aiosqlite.OperationalError):
            await db.init_db(str(tmp_path / "broken.db"))
        assert db._connection is None
        with pytest.raises(ValueError):
            await opened[0].execute("SELECT 1")
    finally:
        for conn in opened:
            await conn.close()


async def test_cancelled_transaction_rolls_back_and_readers_see_committed_data(test_db, monkeypatch):
    await seed()
    inserted = asyncio.Event()
    original = db.upsert_server

    async def pause_after_write(**kwargs):
        sid = await original(**kwargs)
        inserted.set()
        await asyncio.Event().wait()
        return sid

    monkeypatch.setattr(db, "upsert_server", pause_after_write)
    task = asyncio.create_task(db.save_observation(
        dict(identifier="cancelled", title="Hidden", ip="127.0.0.1", port=1235, now_ts=1100),
        {"bob"}, 300, False,
    ))
    await inserted.wait()
    assert all(s["identifier"] != "cancelled" for s in await db.get_active_servers())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await original(identifier="unrelated", title="Other", ip="127.0.0.1", port=1236)
    assert all(s["identifier"] != "cancelled" for s in await db.get_active_servers())
    assert await db.get_player_dossier("bob") is None


async def test_migration_backfill_and_aggregate_updates_survive_restore(tmp_path):
    import sqlite3
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE servers (id INTEGER PRIMARY KEY,identifier TEXT UNIQUE,title TEXT,ip TEXT,port INTEGER,last_seen REAL,is_active INTEGER);
        CREATE TABLE players (ckey TEXT PRIMARY KEY,first_seen REAL,last_seen REAL);
        CREATE TABLE sessions (id INTEGER PRIMARY KEY,ckey TEXT,server_id INTEGER,started_at REAL,last_seen_at REAL,ended_at REAL,duration_seconds INTEGER);
        INSERT INTO servers VALUES(7,'legacy','Legacy','127.0.0.1',1234,1100,1);
        INSERT INTO players VALUES('alice',1000,1100);
        INSERT INTO sessions VALUES(1,'alice',7,1000,1100,NULL,100);
        INSERT INTO sessions VALUES(2,'alice',7,1000,1100,NULL,100);
        """)
    await db.init_db(path)
    try:
        conn = await db.get_connection()
        rows = await (await conn.execute("SELECT ended_at FROM sessions ORDER BY id")).fetchall()
        assert [r[0] for r in rows] == [1100, None]
        details = await db.get_server_details(7)
        assert details["total_playtime_seconds"] == 200
        await db.update_player_sessions(7, {"alice"}, now_ts=1200)
        assert (await db.get_server_details(7))["total_playtime_seconds"] == 300
        # External SQL writers must maintain the same aggregates.
        async with aiosqlite.connect(path) as external:
            await external.execute("DELETE FROM sessions WHERE id=1")
            await external.commit()
        assert (await db.get_server_details(7))["total_playtime_seconds"] == 200
        backup_path = str(tmp_path / "backup.db")
        async with aiosqlite.connect(backup_path) as backup:
            await conn.backup(backup)
        await db.init_db(backup_path)
        restored = await db.get_server_details(7)
        assert restored["total_playtime_seconds"] == 200
        assert restored["top_players"][0]["ckey"] == "alice"
    finally:
        await db.close_db()


async def test_migration_failure_is_not_suppressed(tmp_path):
    import sqlite3
    path = tmp_path / "readonly.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE servers(id INTEGER PRIMARY KEY)")
    async with aiosqlite.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as conn:
        with pytest.raises(RuntimeError, match="Cannot migrate"):
            await db._migrate_schema(conn)


def test_os_lock_survives_contention_and_is_released_after_process_death(tmp_path):
    import os
    import signal
    path = str(tmp_path / "shared.db")
    script = "from core.collector_lock import CollectorLock; import time,sys,os; lock=CollectorLock(sys.argv[1]); print(lock.acquire(),os.getpid(),flush=True); time.sleep(30)"
    process = subprocess.Popen([sys.executable, "-c", script, path], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    lock = CollectorLock(path)
    try:
        acquired, owner_pid = process.stdout.readline().strip().split()
        assert acquired == "True"
        assert not lock.acquire()
        # Windows venv python.exe may be a launcher: kill the actual lock owner.
        os.kill(int(owner_pid), signal.SIGTERM)
        process.wait(timeout=5)
        assert lock.acquire()
    finally:
        lock.release()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


async def test_independent_process_writers_cannot_duplicate_sessions(test_db):
    sid = await seed()
    script = """
import asyncio,sys
from core import db
async def main():
    await db.init_db(sys.argv[1])
    try:
        await db.update_player_sessions(int(sys.argv[2]), {'bob'}, now_ts=1100)
    finally:
        await db.close_db()
asyncio.run(main())
"""
    processes = [await asyncio.create_subprocess_exec(sys.executable, "-c", script, test_db, str(sid)) for _ in range(3)]
    assert await asyncio.gather(*(p.wait() for p in processes)) == [0, 0, 0]
    assert (await db.get_player_dossier("bob"))["sessions_count"] == 1


@pytest.mark.parametrize("unsupported", [None, {}, {"players": "unsupported"}, {"playerlist": {}}])
async def test_topic_status_fallback_and_bad_duration_do_not_drop_fields(test_db, mock_topic_server, monkeypatch, unsupported):
    sid = await seed()
    queries = []

    async def respond(**kwargs):
        queries.append(kwargs["query"])
        if kwargs["query"] == "?status=2":
            return unsupported
        return {"Players_List": ["Alice"], "round_duration": "1e999", "mode": "Extended", "admins": "3"}

    monkeypatch.setattr("core.tracker.query_topic", respond)
    await Tracker(config={"custom_servers": [{"ip": "127.0.0.1", "port": 1234}]}).poll_once(now_ts=1120)
    assert queries == ["?status=2", "?status"]
    details = await db.get_server_details(sid, now_ts=1120)
    assert details["gamemode"] == "Extended"
    assert details["admins_count"] == 3
    assert details["round_duration"] is None
    assert details["online_players"][0]["ckey"] == "alice"


async def test_server_failure_and_disappearance_age_out_without_crediting_gap(test_db, monkeypatch):
    await seed()
    monkeypatch.setattr("core.tracker.query_topic", AsyncMock(return_value=None))
    tracker = Tracker(config={"custom_servers": [{"ip": "127.0.0.1", "port": 1234}]})
    await tracker.poll_once(now_ts=1500)
    assert len(await db.get_active_servers()) == 1
    await tracker.poll_once(now_ts=2000)
    assert await db.get_active_servers() == []
    dossier = await db.get_player_dossier("alice")
    assert not dossier["is_online"]
    assert dossier["total_playtime_seconds"] == 0


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
async def test_untrusted_dns_addresses_are_rejected(monkeypatch, ip):
    import socket
    from core.byond_topic import public_topic_ip
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 1234))]))
    with pytest.raises(ValueError, match="private or special"):
        await public_topic_ip("malicious.example", 1234)


async def test_untrusted_dns_is_pinned_to_checked_address(monkeypatch):
    import socket
    from core.byond_topic import public_topic_ip
    resolver = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 1234))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    assert await public_topic_ip("public.example", 1234) == "8.8.8.8"
    assert resolver.await_count == 1


async def test_manifest_coalesces_and_distinguishes_empty_from_unknown(async_client, monkeypatch):
    import web.app as web
    await seed(time.time())
    sid = (await db.get_active_servers())[0]["id"]
    tracker = Tracker(config={"topic_concurrency": 2, "custom_servers": [{"ip": "127.0.0.1", "port": 1234}]})
    monkeypatch.setattr("core.tracker._GLOBAL_TRACKER", tracker)
    web._manifest_results.clear()
    response_data = {"command": "Alice=Captain"}
    count = 0

    async def respond(**kwargs):
        nonlocal count
        count += 1
        await asyncio.sleep(0.05)
        return response_data

    monkeypatch.setattr(web, "query_topic", respond)
    responses = await asyncio.gather(*(async_client.post(f"/server/{sid}/manifest") for _ in range(12)))
    assert count == 1
    assert all(r.status_code == 200 and "Alice" in r.text for r in responses)
    web._manifest_results.clear()
    response_data = {}
    response = await async_client.post(f"/server/{sid}/manifest")
    assert "Alice" in response.text and "unrecognized" in response.text and "stale" in response.text
    web._manifest_results.clear()
    response_data = {"command": ""}
    response = await async_client.post(f"/server/{sid}/manifest")
    assert "Alice" not in response.text
    assert (await db.get_server_details(sid))["manifest"] == {}


async def test_production_lifespan_uses_configured_database_and_fails_on_start_error(tmp_path, monkeypatch):
    import web.app as web
    path = str(tmp_path / "configured.db")
    cfg = AppConfig(db_path=path, custom_servers=[])
    tracker = Tracker(config=cfg)
    monkeypatch.setattr(web, "get_config", lambda: cfg)
    monkeypatch.setattr(web, "get_tracker", lambda **kwargs: tracker)
    monkeypatch.setattr(tracker, "start", AsyncMock())
    async with web.lifespan(web.app):
        assert db._current_db_path == path
        assert db._connection is not None
    assert db._connection is None
    monkeypatch.setattr(tracker, "start", AsyncMock(side_effect=RuntimeError("start failed")))
    with pytest.raises(RuntimeError, match="start failed"):
        async with web.lifespan(web.app):
            pytest.fail("startup must fail")
    assert db._connection is None


async def test_clock_jump_pauses_retention_and_preserves_monotonic_observation(test_db, monkeypatch):
    import core.tracker as module
    tracker = Tracker(config={"custom_servers": []})
    tracker._clock_wall = 1000
    tracker._clock_mono = 500
    monkeypatch.setattr(module.time, "monotonic", lambda: 600)
    monkeypatch.setattr(module.time, "time", lambda: 1000000)
    assert tracker._observation_time() == 1100
    assert tracker._clock_trusted is False


def test_chart_uses_time_and_leaves_gaps():
    chart = db._build_chart_24h([
        {"timestamp": 0, "player_count": 1},
        {"timestamp": 10, "player_count": 2},
        {"timestamp": 100, "player_count": 3, "gap_before": True},
    ])
    coords = chart["points_str"].split()
    assert coords[1].startswith("68.0,")
    assert len(chart["segments"]) == 2


async def test_hub_size_and_record_limits_are_observable(monkeypatch):
    import core.hub_scraper as scraper
    monkeypatch.setattr(scraper, "MAX_HUB_BYTES", 128)
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 129))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await scraper.fetch_hub_servers(client, custom_servers=[])
    assert result == [] and not result.healthy and result.errors == 2
    monkeypatch.setattr(scraper, "MAX_HUB_SERVERS", 1)
    result = scraper.parse_hub_json([{"url": "byond://8.8.8.8:1234"}, {"url": "byond://1.1.1.1:1234"}])
    assert len(result) == 1 and result.errors == 1


async def test_running_collectors_transfer_ownership(test_db):
    first = Tracker(config={"poll_interval": 1, "custom_servers": []})
    second = Tracker(config={"poll_interval": 1, "custom_servers": []})
    await first.start()
    await second.start()

    async def wait_for(predicate):
        async def check():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(check(), 3)

    try:
        await wait_for(lambda: first._poll_count + second._poll_count > 0)
        assert first.is_leader != second.is_leader
        leader, follower = (first, second) if first.is_leader else (second, first)
        assert follower._poll_count == 0
        await leader.stop()
        await wait_for(lambda: follower.is_leader and follower._poll_count > 0)
    finally:
        await first.stop()
        await second.stop()


async def test_schema_changes_rollback_on_migration_failure(tmp_path, monkeypatch):
    import sqlite3
    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE servers (id INTEGER PRIMARY KEY,identifier TEXT UNIQUE,title TEXT,ip TEXT,port INTEGER,last_seen REAL,is_active INTEGER)")
        conn.execute("INSERT INTO servers VALUES(7,'legacy','Legacy','127.0.0.1',1234,1000,1)")
    monkeypatch.setattr(db, "_initialize_aggregates", AsyncMock(side_effect=RuntimeError("migration failed")))
    with pytest.raises(RuntimeError, match="migration failed"):
        await db.init_db(path)
    with sqlite3.connect(path) as conn:
        assert "current_players" not in {r[1] for r in conn.execute("PRAGMA table_info(servers)")}
        assert conn.execute("SELECT id FROM servers").fetchone()[0] == 7


async def test_cancelled_initialization_closes_connection_and_allows_retry(tmp_path, monkeypatch):
    entered = asyncio.Event()
    connections = []

    async def paused(conn):
        connections.append(conn)
        entered.set()
        await asyncio.Event().wait()

    original = db._migrate_schema
    monkeypatch.setattr(db, "_migrate_schema", paused)
    path = str(tmp_path / "cancelled.db")
    task = asyncio.create_task(db.init_db(path))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert db._connection is None
    with pytest.raises(ValueError):
        await connections[0].execute("SELECT 1")
    monkeypatch.setattr(db, "_migrate_schema", original)
    await db.init_db(path)
    await db.close_db()


async def test_readiness_detects_good_stale_and_failed_collection(async_client, monkeypatch):
    tracker = Tracker(config={"poll_interval": 1, "custom_servers": []})
    monkeypatch.setattr("core.tracker._GLOBAL_TRACKER", tracker)
    await tracker.start()
    try:
        async def collected():
            while tracker._poll_count == 0:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(collected(), 2)
        assert (await async_client.get("/ready")).status_code == 200
        tracker._cycle_healthy = False
        assert (await async_client.get("/ready")).status_code == 503
        tracker._cycle_healthy = True
        await db.record_collector_cycle(time.time() - 10000, True)
        assert (await async_client.get("/ready")).status_code == 503
        assert (await async_client.get("/live")).status_code == 200
    finally:
        await tracker.stop()


async def test_png_rendering_is_bounded_and_keeps_loop_responsive(async_client, monkeypatch):
    import threading
    import web.app as web
    active = maximum = ticks = 0
    lock = threading.Lock()

    def render(dossier):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.04)
        with lock:
            active -= 1
        return b"PNG-test"

    async def probe():
        nonlocal ticks
        for _ in range(10):
            await asyncio.sleep(0.01)
            ticks += 1

    web.clear_card_cache()
    monkeypatch.setattr(web, "_card_slots", asyncio.Semaphore(2))
    monkeypatch.setattr(web, "render_player_card", render)
    monkeypatch.setattr(db, "get_player_dossier", AsyncMock(return_value={"ckey": "alice"}))
    results = await asyncio.gather(probe(), *(async_client.get(f"/api/v1/player/render{i}/card.png") for i in range(10)))
    assert all(r.status_code == 200 for r in results[1:])
    assert maximum == 2 and ticks == 10


def test_whitelist_environment_survives_file_reload(tmp_path, monkeypatch):
    import ast
    import os
    from pathlib import Path
    from typing import Optional, Set
    source = ast.parse(Path("examples/selfbot_example.py").read_text(encoding="utf-8"))
    nodes = [n for n in source.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and
             n.name in ("WhitelistManager", "extract_discord_user_id")]
    namespace = dict(Path=Path, Set=Set, Optional=Optional, os=os, json=json,
                     re=__import__("re"), WHITELIST_PATH=tmp_path / "whitelist.json")
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "whitelist", "exec"), namespace)
    monkeypatch.setenv("HILLEYE_WHITELIST_IDS", "123456789012345")
    path = tmp_path / "whitelist.json"
    path.write_text(json.dumps({"allowed_user_ids": [234567890123456]}))
    manager = namespace["WhitelistManager"](path)
    assert manager.is_allowed(123456789012345)
    path.write_text(json.dumps({"allowed_user_ids": []}))
    assert manager.is_allowed(123456789012345)
    assert not manager.is_allowed(234567890123456)


async def test_partial_malformed_roster_never_confirms_absence(test_db, monkeypatch):
    sid = await seed()
    monkeypatch.setattr("core.tracker.query_topic", AsyncMock(return_value={"playerlist": ["Bob", None, True, {"name": "Eve"}]}))
    tracker = Tracker(config={"custom_servers": [{"ip": "127.0.0.1", "port": 1234}]})
    await tracker.poll_once(now_ts=1400)
    assert (await db.get_player_dossier("alice"))["is_online"]
    assert (await db.get_player_dossier("bob"))["is_online"]
    assert await db.get_player_dossier("none") is None
    assert await db.get_player_dossier("true") is None
    assert await db.get_player_dossier("nameeve") is None
    assert tracker._errors_count > 0


def test_config_and_database_location_do_not_depend_on_cwd(tmp_path, monkeypatch):
    from core.config import PROJECT_DIR
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    assert cfg.custom_servers
    assert cfg.resolved_db_path() == str(PROJECT_DIR / "tracker.db")
    monkeypatch.setenv("HILLEYE_DB_PATH", str(tmp_path / "state.db"))
    assert load_config().resolved_db_path() == str(tmp_path / "state.db")


def test_explicit_pager_mapping_is_unambiguous():
    from core.hub_scraper import merge_custom_servers
    pager = ServerInfo("byond://BYOND.world.12345", "Hub title", "BYOND.world.12345", 0)
    merged = merge_custom_servers([pager], [{"identifier": pager.identifier, "ip": "game.example", "port": 1234}])
    assert len(merged) == 1
    assert (merged[0].ip, merged[0].port, merged[0].trusted) == ("game.example", 1234, True)


async def test_snapshot_retention_uses_timestamp_index(test_db):
    conn = await db.get_connection()
    plan = await (await conn.execute("EXPLAIN QUERY PLAN DELETE FROM server_snapshots WHERE timestamp < ?", (1000,))).fetchall()
    assert any("idx_snapshots_time" in str(tuple(row)) for row in plan)
    plan = await (await conn.execute("EXPLAIN QUERY PLAN SELECT ckey,started_at,last_seen_at,duration_seconds FROM sessions WHERE server_id=? AND ended_at IS NULL ORDER BY started_at", (1,))).fetchall()
    assert any("idx_sessions_online" in str(tuple(row)) for row in plan)


async def test_ipv6_connect_uri_and_unknown_metrics(async_client):
    sid = await db.upsert_server("byond://[2001:db8::1]:1234", "IPv6", "2001:db8::1", 1234)
    details = await db.get_server_details(sid)
    assert details["connect_uri"] == "byond://[2001:db8::1]:1234"
    assert details["round_duration"] is None and details["admins_count"] is None
    assert details["current_players"] is None
    response = await async_client.get(f"/server/{sid}")
    assert response.status_code == 200
    assert 'href="byond://[2001:db8::1]:1234"' in response.text
    assert "Unknown" in response.text


def test_invalid_text_count_is_unknown_and_truncation_is_observable(monkeypatch):
    import core.hub_scraper as scraper
    text = 'world/1\nurl="byond://8.8.8.8:1234"\nplayers=' + '9' * 5000
    result = scraper.parse_hub_text(text)
    assert not result[0].roster_known and not result[0].count_known
    assert result.errors == 1
    monkeypatch.setattr(scraper, "MAX_HUB_SERVERS", 1)
    result = scraper.parse_hub_text('world/1\nurl="byond://8.8.8.8:1234"\nworld/2\nurl="byond://1.1.1.1:1234"')
    assert len(result) == 1 and result.errors == 1


def test_selfbot_server_matching_by_id_and_partial_name():
    import ast
    from pathlib import Path
    from typing import Any, Dict, List, Optional, Tuple
    source = ast.parse(Path("examples/selfbot_example.py").read_text(encoding="utf-8"))
    nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "match_servers_by_query"]
    namespace = dict(Any=Any, Dict=Dict, List=List, Optional=Optional, Tuple=Tuple, re=__import__("re"))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "selfbot_match", "exec"), namespace)
    match_servers_by_query = namespace["match_servers_by_query"]

    servers = [
        {"id": 1, "title": "Tau Ceti Classic I", "current_players": 42, "map_name": "BoxStation"},
        {"id": 2, "title": "Tau Ceti Classic II", "current_players": 65, "map_name": "MetaStation"},
        {"id": 3, "title": "Sierra Bay [RU]", "current_players": 28, "map_name": "Torch"},
        {"id": 4, "title": "SS220 Paradise", "current_players": 110, "map_name": "Cyberiad"},
    ]

    # Exact ID match (with or without '#')
    assert [s["id"] for s in match_servers_by_query(servers, "3")] == [3]
    assert [s["id"] for s in match_servers_by_query(servers, "#2")] == [2]

    # Exact title match (case-insensitive)
    assert [s["id"] for s in match_servers_by_query(servers, "sierra bay [ru]")] == [3]

    # Partial name match (sorted by relevance then online players)
    assert [s["id"] for s in match_servers_by_query(servers, "sierra")] == [3]
    assert [s["id"] for s in match_servers_by_query(servers, "tau ceti")] == [2, 1]
    assert [s["id"] for s in match_servers_by_query(servers, "paradise")] == [4]
    assert [s["id"] for s in match_servers_by_query(servers, "220")] == [4]
    assert match_servers_by_query(servers, "nonexistent") == []

