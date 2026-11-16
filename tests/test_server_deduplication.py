"""Tests for canonical server deduplication across BYOND.world.<id> restarts and startup DB consolidation."""

import json
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from core import db as core_db
from core.hub_scraper import (
    HubResult,
    ServerInfo,
    merge_custom_servers,
    parse_pager_endpoints_with_titles,
)
from core.tracker import Tracker


def test_parse_pager_endpoints_with_titles_excludes_ambiguous_and_placeholder():
    raw = {
        "endpoints": {
            "byond://BYOND.world.101": {
                "ip": "15.235.86.196",
                "port": 26100,
                "title": "Goonstation 1 Classic: Heisenbee",
            },
            "byond://BYOND.world.102": {
                "ip": "15.235.86.196",
                "port": 26100,
                "title": "  goonstation 1 classic: heisenbee ",
            },
            "byond://BYOND.world.201": {
                "ip": "78.29.55.150",
                "port": 1337,
                "title": "Twin Server",
            },
            "byond://BYOND.world.202": {
                "ip": "104.28.228.105",
                "port": 25664,
                "title": "Twin Server",
            },
            "byond://BYOND.world.301": {
                "ip": "45.79.208.108",
                "port": 3000,
                "title": "Unknown Station",
            },
        }
    }
    by_id, by_title = parse_pager_endpoints_with_titles(raw)
    assert len(by_id) == 5
    assert by_title == {"goonstation 1 classic: heisenbee": ("15.235.86.196", 26100)}


def test_merge_custom_servers_uses_unambiguous_title_fallback_and_stays_untrusted():
    hub = [
        ServerInfo(
            identifier="byond://BYOND.world.999999",
            title="Goonstation 1 Classic: Heisenbee",
            ip="BYOND.world.999999",
            port=0,
            player_count=12,
        ),
        ServerInfo(
            identifier="byond://BYOND.world.888881",
            title="Twin Station",
            ip="BYOND.world.888881",
            port=0,
        ),
        ServerInfo(
            identifier="byond://BYOND.world.888882",
            title="Twin Station",
            ip="BYOND.world.888882",
            port=0,
        ),
    ]
    merged = merge_custom_servers(
        hub,
        custom_servers=[],
        extra_pager_endpoints={},
        extra_pager_title_endpoints={
            "goonstation 1 classic: heisenbee": ("15.235.86.196", 26100),
            "twin station": ("1.2.3.4", 1337),
        },
    )
    goon = next(s for s in merged if s.identifier == "byond://BYOND.world.999999")
    assert (goon.ip, goon.port, goon.trusted) == ("15.235.86.196", 26100, False)

    # Ambiguous concurrent titles must never pick a single endpoint.
    twins = [s for s in merged if s.title == "Twin Station"]
    assert len(twins) == 2
    assert all(s.port == 0 for s in twins)


@pytest.mark.asyncio
async def test_tracker_keeps_canonical_server_across_byond_world_restarts(
    test_db, tmp_path, mock_topic_server, monkeypatch
):
    pager_file = tmp_path / "pager_endpoints.json"
    pager_file.write_text(
        json.dumps(
            {
                "endpoints": {
                    "byond://BYOND.world.100001": {
                        "ip": "127.0.0.1",
                        "port": mock_topic_server.port,
                        "title": "[SS220][RU] Primera WL",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    mock_topic_server.set_string_response(
        "version=516&mode=extended&players=1&playerlist=truecomrade&map_name=MetaStation&round_duration=600"
    )
    monkeypatch.setattr("core.tracker.public_topic_ip", AsyncMock(return_value="127.0.0.1"))

    current_hub = [
        ServerInfo(
            identifier="byond://BYOND.world.100001",
            title="[SS220][RU] Primera WL",
            ip="BYOND.world.100001",
            port=0,
            players=["truecomrade"],
            player_count=1,
            roster_known=True,
        )
    ]

    async def fake_hub(client, custom_servers=None, **kwargs):
        return HubResult(merge_custom_servers(current_hub, custom_servers), healthy=True)

    monkeypatch.setattr("core.tracker.fetch_hub_servers", fake_hub)

    tracker = Tracker(
        config={
            "custom_servers": [],
            "pager_endpoints_source": str(pager_file),
            "socket_timeout": 1.0,
        }
    )

    # Tick 1: initial BYOND.world.100001
    await tracker.poll_once(now_ts=1000.0)
    servers_1 = await core_db.get_active_servers()
    assert len(servers_1) == 1
    sid_1 = servers_1[0]["id"]
    assert servers_1[0]["ip"] == "127.0.0.1"
    assert servers_1[0]["port"] == mock_topic_server.port

    # Tick 2: server restarts with brand-new BYOND.world.200002 (NOT in pager_endpoints.json by ID)
    current_hub[:] = [
        ServerInfo(
            identifier="byond://BYOND.world.200002",
            title="[SS220][RU] Primera WL",
            ip="BYOND.world.200002",
            port=0,
            players=["truecomrade"],
            player_count=1,
            roster_known=True,
        )
    ]
    await tracker.poll_once(now_ts=1120.0)

    # Tick 3: another restart without touching pager_endpoints.json -> inherits DB ip/port & preferred query
    pager_file.write_text(json.dumps({"endpoints": {}}), encoding="utf-8")
    tracker._cached_pager_endpoints = {}
    tracker._cached_pager_title_endpoints = {}
    current_hub[:] = [
        ServerInfo(
            identifier="byond://BYOND.world.300003",
            title="[SS220][RU] Primera WL",
            ip="BYOND.world.300003",
            port=0,
            players=["truecomrade"],
            player_count=1,
            roster_known=True,
        )
    ]
    await tracker.poll_once(now_ts=1240.0)

    async with aiosqlite.connect(test_db) as conn:
        all_rows = await (await conn.execute("SELECT id, identifier, ip, port, preferred_topic_query FROM servers")).fetchall()
    assert len(all_rows) == 1
    assert all_rows[0][0] == sid_1
    assert all_rows[0][1] == "byond://BYOND.world.300003"
    assert all_rows[0][2] == "127.0.0.1"
    assert all_rows[0][3] == mock_topic_server.port
    assert all_rows[0][4] == "?status=2"

    dossier = await core_db.get_player_dossier("truecomrade")
    assert dossier is not None
    assert dossier["servers_count"] == 1
    assert dossier["sessions_count"] == 1
    assert dossier["total_playtime_seconds"] == 240
    assert len(dossier["server_breakdown"]) == 1
    assert dossier["server_breakdown"][0]["percentage"] == 100.0


@pytest.mark.asyncio
async def test_concurrent_same_title_servers_stay_separate_and_survive_single_restart(test_db, monkeypatch):
    current_hub = [
        ServerInfo("byond://BYOND.world.111", "Twin Station", "BYOND.world.111", 0, ["alice"], 1, roster_known=True),
        ServerInfo("byond://BYOND.world.222", "Twin Station", "BYOND.world.222", 0, ["bob"], 1, roster_known=True),
    ]

    async def fake_hub(client, custom_servers=None, **kwargs):
        return HubResult(list(current_hub), healthy=True)

    monkeypatch.setattr("core.tracker.fetch_hub_servers", fake_hub)
    tracker = Tracker(config={"custom_servers": []})
    await tracker.poll_once(now_ts=1000.0)

    active_1 = await core_db.get_active_servers()
    assert len(active_1) == 2
    by_ident_1 = {s["identifier"]: s["id"] for s in active_1}
    id_111 = by_ident_1["byond://BYOND.world.111"]
    id_222 = by_ident_1["byond://BYOND.world.222"]
    assert id_111 != id_222

    # 222 restarts as 333 AND appears BEFORE 111 in hub payload; 333 must take over 222, never steal 111!
    current_hub[:] = [
        ServerInfo("byond://BYOND.world.333", "Twin Station", "BYOND.world.333", 0, ["bob"], 1, roster_known=True),
        ServerInfo("byond://BYOND.world.111", "Twin Station", "BYOND.world.111", 0, ["alice"], 1, roster_known=True),
    ]
    await tracker.poll_once(now_ts=1120.0)

    active_2 = await core_db.get_active_servers()
    assert len(active_2) == 2
    by_ident_2 = {s["identifier"]: s["id"] for s in active_2}
    assert by_ident_2["byond://BYOND.world.111"] == id_111
    assert by_ident_2["byond://BYOND.world.333"] == id_222

    assert (await core_db.get_player_dossier("alice"))["servers_count"] == 1
    assert (await core_db.get_player_dossier("bob"))["servers_count"] == 1


@pytest.mark.asyncio
async def test_unknown_station_placeholders_never_merge_until_real_title_known(test_db):
    id_old = await core_db.upsert_server("byond://BYOND.world.10", "Blastwave Station", "5.78.184.148", 1338, now_ts=1000.0)
    id_u1 = await core_db.upsert_server("byond://BYOND.world.11", "Unknown Station", "BYOND.world.11", 0, now_ts=1120.0)
    id_u2 = await core_db.upsert_server("byond://BYOND.world.12", "Unknown Station", "BYOND.world.12", 0, now_ts=1240.0)
    assert len({id_old, id_u1, id_u2}) == 3

    # Once BYOND.world.12 finishes booting and reveals its real title, it absorbs the historical Blastwave row.
    id_revealed = await core_db.upsert_server(
        "byond://BYOND.world.12",
        "Blastwave Station",
        "BYOND.world.12",
        0,
        now_ts=1360.0,
        live_identifiers={"byond://BYOND.world.12"},
    )
    assert id_revealed == id_u2
    details = await core_db.get_server_details(id_old)
    assert details is not None
    assert details["id"] == id_u2
    assert details["ip"] == "5.78.184.148"
    assert details["port"] == 1338


@pytest.mark.asyncio
async def test_endpoint_match_unifies_title_change_and_direct_to_pager(test_db):
    sid_1 = await core_db.upsert_server("byond://1.2.3.4:1337", "Station Old Name", "1.2.3.4", 1337, now_ts=1000.0)
    sid_2 = await core_db.upsert_server("byond://BYOND.world.777", "Station New Name", "1.2.3.4", 1337, now_ts=1120.0)
    assert sid_1 == sid_2
    details = await core_db.get_server_details(sid_1)
    assert details["title"] == "Station New Name"
    assert details["identifier"] == "byond://BYOND.world.777"


@pytest.mark.asyncio
async def test_startup_consolidation_merges_historical_duplicates_and_preserves_totals_and_redirects(
    tmp_path, async_client
):
    db_file = str(tmp_path / "historical_dups.db")
    async with aiosqlite.connect(db_file) as conn:
        await conn.executescript(
            """
            CREATE TABLE servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier TEXT UNIQUE NOT NULL,
                title TEXT NOT NULL,
                ip TEXT NOT NULL,
                port INTEGER NOT NULL,
                last_seen TIMESTAMP NOT NULL,
                is_active BOOLEAN DEFAULT 1,
                current_players INTEGER DEFAULT 0,
                map_name TEXT,
                round_duration INTEGER DEFAULT 0,
                gamemode TEXT,
                admins_count INTEGER DEFAULT 0,
                version TEXT,
                active_players INTEGER,
                station_time TEXT,
                enter_allowed BOOLEAN,
                respawn_allowed BOOLEAN,
                admin_list_json TEXT,
                manifest_json TEXT,
                preferred_topic_query TEXT
            );
            CREATE TABLE server_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                player_count INTEGER NOT NULL,
                round_duration INTEGER DEFAULT 0,
                map_name TEXT
            );
            CREATE TABLE players (
                ckey TEXT PRIMARY KEY,
                first_seen TIMESTAMP NOT NULL,
                last_seen TIMESTAMP NOT NULL
            );
            CREATE TABLE sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ckey TEXT NOT NULL,
                server_id INTEGER NOT NULL,
                started_at TIMESTAMP NOT NULL,
                last_seen_at TIMESTAMP NOT NULL,
                ended_at TIMESTAMP,
                duration_seconds INTEGER DEFAULT 0
            );
            """
        )
        # 5 sequential restarts of [SS220][RU] Primera WL, including an active open-session collision!
        await conn.execute(
            "INSERT INTO servers (id, identifier, title, ip, port, last_seen, is_active, preferred_topic_query) "
            "VALUES (10, 'byond://BYOND.world.10', '[SS220][RU] Primera WL', '116.202.156.155', 3254, 10000, 0, '?status=2')"
        )
        await conn.execute(
            "INSERT INTO servers (id, identifier, title, ip, port, last_seen, is_active) "
            "VALUES (11, 'byond://BYOND.world.11', '[SS220][RU] Primera WL', 'BYOND.world.11', 0, 20000, 0)"
        )
        await conn.execute(
            "INSERT INTO servers (id, identifier, title, ip, port, last_seen, is_active) "
            "VALUES (12, 'byond://BYOND.world.12', '[SS220][RU] Primera WL', 'BYOND.world.12', 0, 30000, 1)"
        )
        # 2 genuinely concurrent servers sharing a title (snapshots 0.5s apart) -> must NOT merge!
        await conn.execute(
            "INSERT INTO servers (id, identifier, title, ip, port, last_seen, is_active) "
            "VALUES (20, 'byond://BYOND.world.20', 'Concurrent Twin', '1.1.1.1', 1337, 30000, 1)"
        )
        await conn.execute(
            "INSERT INTO servers (id, identifier, title, ip, port, last_seen, is_active) "
            "VALUES (21, 'byond://BYOND.world.21', 'Concurrent Twin', '2.2.2.2', 1337, 30000.5, 1)"
        )
        await conn.execute("INSERT INTO players VALUES ('truecomrade', 9000, 30000)")
        await conn.execute("INSERT INTO sessions VALUES (1, 'truecomrade', 10, 9000, 10000, 10000, 1000)")
        # Colliding open sessions across 11 and 12 (e.g. crash before expire_stale_servers closed 11)
        await conn.execute("INSERT INTO sessions VALUES (2, 'truecomrade', 11, 19000, 20000, NULL, 1000)")
        await conn.execute("INSERT INTO sessions VALUES (3, 'truecomrade', 12, 29000, 30000, NULL, 1000)")

        await conn.execute("INSERT INTO server_snapshots (server_id, timestamp, player_count) VALUES (10, 10000, 15)")
        await conn.execute("INSERT INTO server_snapshots (server_id, timestamp, player_count) VALUES (11, 20000, 20)")
        await conn.execute("INSERT INTO server_snapshots (server_id, timestamp, player_count) VALUES (12, 30000, 25)")
        await conn.execute("INSERT INTO server_snapshots (server_id, timestamp, player_count) VALUES (20, 30000, 5)")
        await conn.execute("INSERT INTO server_snapshots (server_id, timestamp, player_count) VALUES (21, 30000.5, 6)")
        await conn.commit()

    await core_db.init_db(db_file)
    try:
        dossier = await core_db.get_player_dossier("truecomrade")
        assert dossier is not None
        assert dossier["servers_count"] == 1
        assert dossier["sessions_count"] == 3
        assert dossier["total_playtime_seconds"] == 3000
        assert len(dossier["server_breakdown"]) == 1
        assert dossier["server_breakdown"][0]["server_id"] == 12
        assert dossier["server_breakdown"][0]["title"] == "[SS220][RU] Primera WL"
        assert dossier["server_breakdown"][0]["percentage"] == 100.0

        details = await core_db.get_server_details(12, now_ts=30000.0, time_range="all")
        assert details is not None
        assert details["ip"] == "116.202.156.155"
        assert details["port"] == 3254
        assert len(details["snapshots_24h"]) == 3

        # Concurrent twins stayed separate (total servers = 1 + 2 = 3).
        active = await core_db.get_active_servers()
        assert len(active) == 3

        # Verify old server IDs redirect cleanly on HTML page and resolve on JSON API.
        html_redir = await async_client.get("/server/10?time_range=all", follow_redirects=False)
        assert html_redir.status_code == 302
        assert html_redir.headers["location"] == "/server/12?time_range=all"

        api_alias = await async_client.get("/api/v1/server/10")
        assert api_alias.status_code == 200
        assert api_alias.json()["id"] == 12

        # Truly nonexistent server still returns 404.
        assert (await async_client.get("/server/999999")).status_code == 404

        # Verify aggregate table matches sessions with zero drift.
        conn = await core_db.get_connection()
        drift = await (
            await conn.execute(
                """
                SELECT COUNT(*) FROM (
                    SELECT server_id, ckey, total_seconds, sessions_count FROM server_player_totals
                    EXCEPT
                    SELECT server_id, ckey, COALESCE(SUM(duration_seconds), 0), COUNT(*) FROM sessions GROUP BY server_id, ckey
                )
                """
            )
        ).fetchone()
        assert drift[0] == 0
    finally:
        await core_db.close_db()
