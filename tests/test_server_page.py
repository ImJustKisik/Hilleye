"""
Unit and Integration Tests for Server Detail Page (/server/{server_id}),
REST Endpoint (/api/v1/server/{server_id}), Round Metadata Storage,
and Timeseries Online Snapshots.
"""

import time
import aiosqlite
import pytest

from core import db as core_db
from core.tracker import Tracker


@pytest.mark.asyncio
async def test_server_metadata_storage(test_db):
    """
    1. Verifies accurate storage and update of map_name, round_duration,
       current_players, gamemode, admins_count, and version in SQLite.
       Also verifies safe column migration on legacy servers table.
    """
    server_id = await core_db.upsert_server(
        identifier="byond://178.62.241.130:1337",
        title="Tau Ceti Classic",
        ip="178.62.241.130",
        port=1337,
        is_active=True,
        now_ts=1000.0,
        current_players=42,
        map_name="BoxStation",
        round_duration=3725,
        gamemode="extended",
        admins_count=3,
        version="516.1650",
    )
    assert server_id > 0

    async with aiosqlite.connect(test_db) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM servers WHERE id = ?;", (server_id,))
        row = await cur.fetchone()
        assert row is not None
        assert row["current_players"] == 42
        assert row["map_name"] == "BoxStation"
        assert row["round_duration"] == 3725
        assert row["gamemode"] == "extended"
        assert row["admins_count"] == 3
        assert row["version"] == "516.1650"

    # Update round parameters on next poll tick
    await core_db.upsert_server(
        identifier="byond://178.62.241.130:1337",
        title="Tau Ceti Classic",
        ip="178.62.241.130",
        port=1337,
        is_active=True,
        now_ts=1120.0,
        current_players=50,
        map_name="MetaStation",
        round_duration=120,
        gamemode="secret",
        admins_count=5,
        version="516.1651",
    )

    details = await core_db.get_server_details(server_id, now_ts=1120.0)
    assert details is not None
    assert details["current_players"] == 50
    assert details["map_name"] == "MetaStation"
    assert details["round_duration"] == 120
    assert details["round_duration_str"] == "00:02:00"
    assert details["gamemode"] == "secret"
    assert details["admins_count"] == 5
    assert details["version"] == "516.1651"


@pytest.mark.asyncio
async def test_legacy_schema_migration(tmp_path):
    """Verifies that an older database without round columns is safely migrated via PRAGMA table_info."""
    legacy_db_path = str(tmp_path / "legacy_tracker.db")
    async with aiosqlite.connect(legacy_db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                ip TEXT NOT NULL,
                port INTEGER NOT NULL,
                last_seen TIMESTAMP NOT NULL,
                is_active BOOLEAN DEFAULT 1
            );
            """
        )
        await conn.commit()

    await core_db.init_db(legacy_db_path)
    try:
        sid = await core_db.upsert_server(
            identifier="byond://10.0.0.1:1337",
            title="Legacy Migrated Station",
            ip="10.0.0.1",
            port=1337,
            current_players=12,
            map_name="DeltaStation",
            round_duration=600,
        )
        details = await core_db.get_server_details(sid)
        assert details is not None
        assert details["map_name"] == "DeltaStation"
        assert details["current_players"] == 12
        assert details["round_duration"] == 600
    finally:
        await core_db.close_db()


@pytest.mark.asyncio
async def test_server_snapshots_and_pruning(test_db):
    """
    2. Verifies recording online history snapshots and pruning snapshots older than N days.
    """
    server_id = await core_db.upsert_server(
        identifier="byond://127.0.0.1:7777",
        title="Snapshot Station",
        ip="127.0.0.1",
        port=7777,
        now_ts=1000000.0,
    )

    now_ts = 1000000.0
    # Snapshot 10 days ago (should be pruned when max_age_days=7)
    old_ts_1 = now_ts - (10 * 86400)
    # Snapshot 8 days ago (should be pruned when max_age_days=7)
    old_ts_2 = now_ts - (8 * 86400)
    # Snapshot 2 hours ago (within 24h and 7d)
    recent_ts_1 = now_ts - 7200
    # Snapshot now (within 24h and 7d)
    recent_ts_2 = now_ts

    await core_db.record_server_snapshot(server_id, player_count=10, round_duration=300, map_name="BoxStation", ts=old_ts_1)
    await core_db.record_server_snapshot(server_id, player_count=15, round_duration=600, map_name="BoxStation", ts=old_ts_2)
    await core_db.record_server_snapshot(server_id, player_count=25, round_duration=1200, map_name="MetaStation", ts=recent_ts_1)
    await core_db.record_server_snapshot(server_id, player_count=32, round_duration=8400, map_name="MetaStation", ts=recent_ts_2)

    deleted_count = await core_db.prune_server_snapshots(max_age_days=7, now_ts=now_ts)
    assert deleted_count == 2

    async with aiosqlite.connect(test_db) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM server_snapshots WHERE server_id = ?;", (server_id,))
        remaining = (await cur.fetchone())[0]
        assert remaining == 2

    details = await core_db.get_server_details(server_id, now_ts=now_ts)
    assert details is not None
    assert len(details["snapshots_24h"]) == 2
    assert details["chart_24h"]["min_players"] == 25
    assert details["chart_24h"]["max_players"] == 32
    assert details["chart_24h"]["points_str"] != ""


@pytest.mark.asyncio
async def test_get_server_details_query(test_db):
    """
    3. Verifies the structure and accuracy of get_server_details:
       - Server metadata and round parameters
       - Active online players (ended_at IS NULL)
       - Top-20 players ordered by total playtime descending
       - Aggregate server hours and unique player count
    """
    server_id = await core_db.upsert_server(
        identifier="byond://192.168.1.50:25000",
        title="SierraBay Frontier",
        ip="192.168.1.50",
        port=25000,
        now_ts=10000.0,
        current_players=2,
        map_name="Torch",
        round_duration=5432,
        gamemode="exploration",
        admins_count=2,
        version="516.1650",
    )

    # Player 1: veteranskipper plays 7200s (2.0h) then logs off
    await core_db.update_player_sessions(server_id, {"veteranskipper"}, now_ts=1000.0)
    await core_db.update_player_sessions(server_id, {"veteranskipper"}, now_ts=8200.0, grace_period_seconds=86400)
    await core_db.update_player_sessions(server_id, set(), now_ts=9000.0, grace_period_seconds=300)

    # Player 2 & 3: currently online at T=10000.0 (polled within 300s grace window)
    await core_db.update_player_sessions(server_id, {"chiefengineer", "cargotech"}, now_ts=9400.0)
    await core_db.update_player_sessions(server_id, {"chiefengineer", "cargotech"}, now_ts=9600.0)
    await core_db.update_player_sessions(server_id, {"chiefengineer", "cargotech"}, now_ts=9800.0)
    await core_db.update_player_sessions(server_id, {"chiefengineer", "cargotech"}, now_ts=10000.0)

    await core_db.record_server_snapshot(server_id, player_count=2, round_duration=5432, map_name="Torch", ts=10000.0)

    details = await core_db.get_server_details(server_id, now_ts=10000.0)
    assert details is not None
    assert details["title"] == "SierraBay Frontier"
    assert details["map_name"] == "Torch"
    assert details["round_duration"] == 5432
    assert details["round_duration_str"] == "01:30:32"
    assert details["gamemode"] == "exploration"
    assert details["admins_count"] == 2
    assert details["unique_players"] == 3
    assert details["total_playtime_seconds"] == 7200 + 600 + 600
    assert details["total_hours"] == round(8400 / 3600.0, 1)

    # Online roster should contain only chiefengineer and cargotech (not veteranskipper)
    online_ckeys = [p["ckey"] for p in details["online_players"]]
    assert set(online_ckeys) == {"chiefengineer", "cargotech"}
    assert "veteranskipper" not in online_ckeys
    for p in details["online_players"]:
        assert p["duration_seconds"] == 600
        assert p["duration_str"] == "10m 0s"

    # Top players should have veteranskipper at rank #1 with 2.0h
    top = details["top_players"]
    assert len(top) == 3
    assert top[0]["rank"] == 1
    assert top[0]["ckey"] == "veteranskipper"
    assert top[0]["total_seconds"] == 7200
    assert top[0]["total_hours"] == 2.0

    # Non-existent server returns None
    assert await core_db.get_server_details(999999) is None


@pytest.mark.asyncio
async def test_api_get_server_200_and_404(async_client):
    """
    4. Verifies REST endpoint GET /api/v1/server/{server_id} returns 200 with full JSON
       payload for existing server and 404 for unknown server_id.
    """
    server_id = await core_db.upsert_server(
        identifier="byond://178.62.241.130:1337",
        title="Tau Ceti Classic",
        ip="178.62.241.130",
        port=1337,
        now_ts=5000.0,
        current_players=18,
        map_name="BoxStation",
        round_duration=1800,
        gamemode="nuclear",
        admins_count=4,
        version="516",
    )
    await core_db.update_player_sessions(server_id, {"honkmaster"}, now_ts=4400.0)
    await core_db.update_player_sessions(server_id, {"honkmaster"}, now_ts=5000.0)
    await core_db.record_server_snapshot(server_id, player_count=18, round_duration=1800, map_name="BoxStation", ts=time.time())

    resp_200 = await async_client.get(f"/api/v1/server/{server_id}")
    assert resp_200.status_code == 200
    data = resp_200.json()
    assert data["id"] == server_id
    assert data["title"] == "Tau Ceti Classic"
    assert data["map_name"] == "BoxStation"
    assert data["round_duration"] == 1800
    assert data["gamemode"] == "nuclear"
    assert data["admins_count"] == 4
    assert len(data["online_players"]) == 1
    assert data["online_players"][0]["ckey"] == "honkmaster"
    assert len(data["top_players"]) == 1
    assert len(data["snapshots_24h"]) == 1

    resp_404 = await async_client.get("/api/v1/server/99999")
    assert resp_404.status_code == 404
    err_data = resp_404.json()
    assert "error" in err_data or "detail" in err_data


@pytest.mark.asyncio
async def test_web_server_page_html(async_client):
    """
    5. Verifies HTML rendering of GET /server/{server_id} (status 200 and 404),
       presence of round parameters, SVG sparkline, online players links, top-20 table,
       and clickable links from / and /player/{ckey}.
    """
    server_id = await core_db.upsert_server(
        identifier="byond://178.62.241.130:1337",
        title="Paradise Station",
        ip="178.62.241.130",
        port=1337,
        now_ts=2000.0,
        current_players=35,
        map_name="Cyberiad",
        round_duration=3661,
        gamemode="traitor",
        admins_count=6,
        version="516.1650",
    )
    await core_db.update_player_sessions(server_id, {"captain_fox"}, now_ts=1400.0)
    await core_db.update_player_sessions(server_id, {"captain_fox"}, now_ts=2000.0)
    await core_db.record_server_snapshot(server_id, player_count=20, round_duration=3000, map_name="Cyberiad", ts=time.time()-600)
    await core_db.record_server_snapshot(server_id, player_count=35, round_duration=3661, map_name="Cyberiad", ts=time.time())

    resp = await async_client.get(f"/server/{server_id}")
    assert resp.status_code == 200
    html = resp.text

    assert "Paradise Station" in html
    assert "178.62.241.130:1337" in html
    assert "byond://178.62.241.130:1337" in html
    assert "Cyberiad" in html
    assert "01:01:01" in html
    assert "traitor" in html
    assert "<svg" in html
    assert "<polyline" in html
    assert "/player/captainfox" in html

    # Verify links on main page (/) and player dossier (/player/captainfox)
    index_resp = await async_client.get("/")
    assert index_resp.status_code == 200
    assert f'/server/{server_id}' in index_resp.text

    player_resp = await async_client.get("/player/captainfox")
    assert player_resp.status_code == 200
    assert f'/server/{server_id}' in player_resp.text

    # Verify 404 HTML response for non-existent server
    resp_404 = await async_client.get("/server/88888")
    assert resp_404.status_code == 404
    assert "88888" in resp_404.text


@pytest.mark.asyncio
async def test_tracker_collects_round_metrics_and_snapshots(test_db, mock_topic_server):
    """
    Verifies that Tracker.poll_once parses map_name, round_duration, players,
    gamemode, admins, and version from Topic ?status and stores both server
    metadata and a server_snapshot entry.
    """
    port = mock_topic_server.port
    ident = f"byond://127.0.0.1:{port}"
    mock_topic_server.set_string_response(
        "version=516.1650&mode=cult&players=24&playerlist=alice,bob&map_name=MetaStation&round_duration=2450&admins=3"
    )

    tracker = Tracker(
        config={
            "poll_interval": 120,
            "grace_period_seconds": 300,
            "socket_timeout": 1.0,
            "custom_servers": [
                {
                    "identifier": ident,
                    "title": "Cult Outpost",
                    "ip": "127.0.0.1",
                    "port": port,
                }
            ],
        }
    )

    await tracker.poll_once(now_ts=50000.0)

    servers = await core_db.get_active_servers()
    target = next((s for s in servers if s["identifier"] == ident), None)
    assert target is not None

    details = await core_db.get_server_details(target["id"], now_ts=50000.0)
    assert details is not None
    assert details["current_players"] == 24
    assert details["map_name"] == "MetaStation"
    assert details["round_duration"] == 2450
    assert details["gamemode"] == "cult"
    assert details["admins_count"] == 3
    assert details["version"] == "516.1650"
    assert len(details["snapshots_24h"]) == 1
    assert details["snapshots_24h"][0]["player_count"] == 24


@pytest.mark.asyncio
async def test_sierrabay_world_topic_status2_and_legacy_parsing(test_db, mock_topic_server):
    """
    Verifies full compatibility with Baystation12 / SierraBay / Polaris / Aurora
    world/Topic() formats:
      - ?status=2 response with nested list2params playerlist (Alice&Bob),
        adminlist (AdminOne=Game+Master&ModTwo=Moderator), roundduration (HH:MM),
        stationtime (13:45), active_players, enter, respawn, and map.
      - Legacy ?status response with indexed player0..playerN keys.
    """
    port = mock_topic_server.port
    ident = f"byond://127.0.0.1:{port}"
    # Exact format returned by SierraBay /world/Topic("status=2") via list2params(s)
    mock_topic_server.set_string_response(
        "version=SierraBay-2.0&mode=Extended&respawn=1&enter=1&vote=1&ai=1&host=SierraHost"
        "&players=3&stationtime=13%3A45&roundduration=01%3A24&map=SEV+Torch"
        "&admins=2&playerlist=Captain_Fox%26Chief_Wolf%26Doc_Bear"
        "&adminlist=Captain_Fox%3DGame%2BMaster%26Mod_Hawk%3DModerator&active_players=2"
    )

    tracker = Tracker(
        config={
            "poll_interval": 120,
            "grace_period_seconds": 300,
            "socket_timeout": 1.0,
            "custom_servers": [
                {
                    "identifier": ident,
                    "title": "SEV Torch — SierraBay",
                    "ip": "127.0.0.1",
                    "port": port,
                }
            ],
        }
    )

    await tracker.poll_once(now_ts=60000.0)

    servers = await core_db.get_active_servers()
    target = next((s for s in servers if s["identifier"] == ident), None)
    assert target is not None
    assert target["active_players"] == 2
    assert target["station_time"] == "13:45"
    assert target["round_duration"] == 5040  # 01:24 -> 1h 24m = 5040s

    details = await core_db.get_server_details(target["id"], now_ts=60000.0)
    assert details is not None
    assert details["map_name"] == "SEV Torch"
    assert details["round_duration"] == 5040
    assert details["round_duration_str"] == "01:24:00"
    assert details["active_players"] == 2
    assert details["station_time"] == "13:45"
    assert details["enter_allowed"] is True
    assert details["respawn_allowed"] is True
    assert len(details["admin_list"]) == 2
    assert details["admin_list"][0]["key"] == "Captain_Fox"
    assert details["admin_list"][0]["rank"] == "Game Master"

    online_ckeys = {p["ckey"] for p in details["online_players"]}
    assert online_ckeys == {"captainfox", "chiefwolf", "docbear"}

    # Now test Legacy Baystation response with player0, player1, player2
    mock_topic_server.set_string_response(
        "version=Bay12&mode=Secret&players=2&roundduration=02:15&map=Exodus&player0=Legacy_One&player1=Legacy_Two"
    )
    await tracker.poll_once(now_ts=60120.0)
    details_legacy = await core_db.get_server_details(target["id"], now_ts=60120.0)
    assert details_legacy is not None
    assert details_legacy["round_duration"] == (2 * 3600 + 15 * 60)
    legacy_ckeys = {p["ckey"] for p in details_legacy["online_players"]}
    assert "legacyone" in legacy_ckeys
    assert "legacytwo" in legacy_ckeys


@pytest.mark.asyncio
async def test_sierrabay_manifest_endpoint(async_client, mock_topic_server, monkeypatch):
    """
    Verifies POST /server/{server_id}/manifest queries ?manifest from a SierraBay server,
    parses nested list2params departments & crew ranks, stores them in SQLite, and renders HTML.
    """
    port = mock_topic_server.port
    monkeypatch.setattr("core.tracker._GLOBAL_TRACKER", Tracker(config={"custom_servers": [{"ip": "127.0.0.1", "port": port}]}))
    server_id = await core_db.upsert_server(
        identifier=f"byond://127.0.0.1:{port}",
        title="SierraBay SEV Torch",
        ip="127.0.0.1",
        port=port,
        now_ts=70000.0,
    )

    # SierraBay ?manifest response format: positions[dept] = list2params(list("Name"="Rank"))
    mock_topic_server.set_string_response(
        "command=Ivan+Petrov%3DCaptain%26 Elena+Vance%3DHead+of+Personnel"
        "&sec=Marcus+Reed%3DHead+of+Security"
        "&eng=Isaac+Clarke%3DChief+Engineer"
    )

    resp = await async_client.post(f"/server/{server_id}/manifest")
    assert resp.status_code == 200
    html = resp.text
    assert "Command" in html
    assert "Ivan Petrov" in html
    assert "Captain" in html
    assert "Security" in html
    assert "Marcus Reed" in html
    assert "Isaac Clarke" in html

    # Verify manifest is also persisted in get_server_details
    details = await core_db.get_server_details(server_id, now_ts=70000.0)
    assert details is not None
    assert details["manifest_total_crew"] == 4
    assert "command" in details["manifest"]
