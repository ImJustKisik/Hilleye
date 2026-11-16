"""Regression tests for server monitoring code review fixes."""

from pathlib import Path
import json
import time
from unittest.mock import AsyncMock

import pytest

from core import db
from core.byond_topic import is_usable_topic_payload, probe_server_topic
from core.collector_lock import CollectorLock
from core.hub_scraper import HubResult, ServerInfo, clean_server_title, merge_custom_servers
from core.tracker import Tracker


def test_clean_server_title_real_hub_edge_cases():
    assert clean_server_title(r"\[<b>RU\] The Fluffy Frontier 18+ MRP</b>") == "[RU] The Fluffy Frontier 18+ MRP"
    assert (
        clean_server_title("<b>[RU] Twilight Axis (18+) - (WL - <b>OPEN</b>)</b> - 80 players")
        == "[RU] Twilight Axis (18+) - (WL - OPEN)"
    )
    assert clean_server_title("<b></b> SerenityStation") == "SerenityStation"
    assert clean_server_title("Plain Server Name\nMode: <b>Adventure Mode</b>") == "Plain Server Name"
    assert clean_server_title("<b>Multi-line\nStation Name</b>\nMode: <b>Extended</b>") == "Multi-line Station Name"


def test_numeric_version_payload_usable_and_null_probes_preserve_preferred():
    assert is_usable_topic_payload({"version": 516}) is True


@pytest.mark.asyncio
async def test_probe_server_topic_preserves_preferred_when_all_responses_none():
    async def null_query(ip, port, query, timeout=3.0, semaphore=None):
        return None

    payload, winner, should_clear = await probe_server_topic(
        ip="127.0.0.1",
        port=1337,
        queries=["?status=2", "?status"],
        preferred_query="?status=2",
        query_fn=null_query,
    )
    assert payload is None
    assert winner is None
    assert should_clear is False


def test_collector_lock_supports_in_memory_db():
    lock = CollectorLock(":memory:")
    assert lock.acquire() is True
    lock.release()


@pytest.mark.asyncio
async def test_double_merge_with_pager_endpoints_does_not_keep_offline_custom_active(test_db, tmp_path, monkeypatch):
    pager_file = tmp_path / "pager_endpoints.json"
    pager_file.write_text(
        json.dumps({"endpoints": {"byond://BYOND.world.555": {"ip": "127.0.0.1", "port": 59991}}}),
        encoding="utf-8",
    )
    custom_cfg = [{"identifier": "byond://127.0.0.1:59992", "title": "Dead Custom", "ip": "127.0.0.1", "port": 59992}]

    async def fake_hub(client, custom_servers=None, **kwargs):
        hub_srv = ServerInfo(
            identifier="byond://BYOND.world.555",
            title="Hub Pager Server",
            ip="BYOND.world.555",
            port=0,
            player_count=5,
            from_hub=True,
        )
        return HubResult(merge_custom_servers([hub_srv], custom_servers), healthy=True)

    monkeypatch.setattr("core.tracker.fetch_hub_servers", fake_hub)
    monkeypatch.setattr("core.tracker.query_topic", AsyncMock(return_value=None))

    tracker = Tracker(
        config={
            "custom_servers": custom_cfg,
            "pager_endpoints_source": str(pager_file),
            "socket_timeout": 0.1,
        }
    )
    await tracker.poll_once(now_ts=10000.0)

    active = await db.get_active_servers()
    idents = {s["identifier"] for s in active}
    assert "byond://BYOND.world.555" in idents
    assert "byond://127.0.0.1:59992" not in idents


@pytest.mark.asyncio
async def test_topic_unreachable_targets_do_not_degrade_collector_health(async_client, monkeypatch):
    hub_ok = ServerInfo("byond://127.0.0.1:4111", "OK Server", "127.0.0.1", 4111, player_count=3, trusted=True)
    hub_dead = ServerInfo("byond://127.0.0.1:4112", "Dead Topic Server", "127.0.0.1", 4112, player_count=1, trusted=True)

    monkeypatch.setattr(
        "core.tracker.fetch_hub_servers",
        AsyncMock(return_value=HubResult([hub_ok, hub_dead], healthy=True)),
    )

    async def topic_reply(ip, port, **kwargs):
        if port == 4111:
            return {"players": 3, "playerlist": ["alice", "bob", "carol"]}
        return None

    monkeypatch.setattr("core.tracker.query_topic", topic_reply)

    tracker = Tracker(config={"poll_interval": 60, "custom_servers": []})
    monkeypatch.setattr("core.tracker._GLOBAL_TRACKER", tracker)
    await tracker.start()
    try:
        stats = await tracker.poll_once(now_ts=time.time())
        assert tracker._cycle_healthy is True
        assert stats["topic_failures"] == 1
        assert stats["topic_successes"] == 1
        assert stats["status"] == "ok"

        resp = await async_client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["collection_status"] == "ok"
    finally:
        await tracker.stop()


@pytest.mark.asyncio
async def test_chart_y_ticks_and_weighted_trend_consistency(test_db):
    now_ts = 100_000.0
    sid = await db.upsert_server("byond://127.0.0.1:7100", "Chart Station", "127.0.0.1", 7100, now_ts=now_ts)

    # First half (older): bucket with [0, 30] -> max=30, min=0, avg=15.0
    await db.record_server_snapshot(sid, player_count=0, ts=now_ts - 7200)
    await db.record_server_snapshot(sid, player_count=30, ts=now_ts - 7190)
    # Second half (newer): bucket with [10, 40] -> max=40, min=10, avg=25.0
    await db.record_server_snapshot(sid, player_count=10, ts=now_ts - 600)
    await db.record_server_snapshot(sid, player_count=40, ts=now_ts - 590)

    details = await db.get_server_details(sid, now_ts=now_ts, time_range="day")
    chart = details["chart_24h"]
    assert chart["min_players"] == 0
    assert chart["max_players"] == 40
    assert chart["y_ticks"] == {"top": "40", "mid": "20", "bottom": "0"}
    assert chart["trend"]["first_avg"] == 15.0
    assert chart["trend"]["second_avg"] == 25.0
    assert chart["trend"]["line"]["direction"] == "up"


@pytest.mark.asyncio
async def test_admin_list_cleared_on_zero_and_manifest_cleared_on_round_restart(async_client, monkeypatch):
    sid = await db.upsert_server(
        identifier="byond://127.0.0.1:4555",
        title="Admin & Manifest Station",
        ip="127.0.0.1",
        port=4555,
        now_ts=1000.0,
        round_duration=5000,
        admins_count=1,
        admin_list=[{"ckey": "boss", "key": "Boss", "rank": "Host"}],
        manifest={"command": [{"name": "John Doe", "rank": "Captain"}]},
    )

    # Next poll: new round (round_duration=30 < 5000 - 60) and admins=0 without adminlist
    srv = ServerInfo("byond://127.0.0.1:4555", "Admin & Manifest Station", "127.0.0.1", 4555, trusted=True)
    monkeypatch.setattr("core.tracker.fetch_hub_servers", AsyncMock(return_value=HubResult([srv], healthy=True)))
    monkeypatch.setattr(
        "core.tracker.query_topic",
        AsyncMock(return_value={"players": 0, "playerlist": [], "admins": 0, "roundduration": "00:01"}),
    )

    tracker = Tracker(config={"custom_servers": []})
    await tracker.poll_once(now_ts=1120.0)

    details = await db.get_server_details(sid, now_ts=1120.0)
    assert details["admins_count"] == 0
    assert details["admin_list"] == []
    assert details["manifest"] == {}
    assert details["manifest_observed_at"] is None

    resp = await async_client.get(f"/server/{sid}")
    assert resp.status_code == 200
    assert "No staff online" in resp.text
    assert "Boss (Host)" not in resp.text


@pytest.mark.asyncio
async def test_schema_index_trigger_v2_and_dossier_totals(test_db):
    conn = await db.get_connection()
    indexes = {r[0] for r in await (await conn.execute("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()}
    assert "idx_totals_ckey" in indexes
    versions = {r[0] for r in await (await conn.execute("SELECT version FROM schema_migrations")).fetchall()}
    assert {1, 2}.issubset(versions)

    sid = await db.upsert_server("byond://127.0.0.1:4666", "Totals Station", "127.0.0.1", 4666, now_ts=1000.0)
    await db.update_player_sessions(sid, {"tester"}, now_ts=1000.0)
    await db.update_player_sessions(sid, {"tester"}, now_ts=1600.0, grace_period_seconds=800)
    await db.update_player_sessions(sid, set(), now_ts=2000.0, grace_period_seconds=300)

    dossier = await db.get_player_dossier("tester")
    assert dossier["total_playtime_seconds"] == 600
    assert dossier["sessions_count"] == 1
    assert dossier["servers_count"] == 1
    assert dossier["server_breakdown"][0]["seconds"] == 600


def test_backup_sh_uses_systemd_state_dir():
    content = Path("backup.sh").read_text(encoding="utf-8")
    assert "/var/lib/hilleye/tracker.db" in content
    assert "logs.db" in content
    assert "/opt/Hilleye/tracker.db" not in content
