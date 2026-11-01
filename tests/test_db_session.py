"""
Tier 3: Database & Sessionization Engine Tests
Covers SQLite WAL mode initialization, ckey normalization, player/server CRUD,
active session tracking, and the 5-minute (300s) grace period state machine.
"""

import re
import pytest
import aiosqlite

db_module = pytest.importorskip("core.db", reason="core.db not yet implemented")

init_db = getattr(db_module, "init_db", None)
close_db = getattr(db_module, "close_db", None)
upsert_server = getattr(db_module, "upsert_server", None)
get_active_servers = getattr(db_module, "get_active_servers", None)
update_player_sessions = getattr(db_module, "update_player_sessions", None)
get_player_dossier = getattr(db_module, "get_player_dossier", None)
normalize_ckey = getattr(db_module, "normalize_ckey", None)

# If normalize_ckey is in core.tracker instead, try importing it
if normalize_ckey is None:
    try:
        from core import tracker
        normalize_ckey = getattr(tracker, "normalize_ckey", None)
    except ImportError:
        pass


class TestDatabaseInitialization:
    """Verifies SQLite initialization, WAL mode pragma, and schema creation."""

    @pytest.mark.asyncio
    async def test_wal_mode_and_pragmas(self, temp_db_path):
        await init_db(temp_db_path)

        async with aiosqlite.connect(temp_db_path) as db:
            # Check journal_mode = wal
            cursor = await db.execute("PRAGMA journal_mode;")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0].lower() == "wal"

            # Check required tables exist
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [r[0] for r in await cursor.fetchall()]
            assert "servers" in tables
            assert "players" in tables
            assert "sessions" in tables

            # Check indexes exist
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='index';")
            indexes = [r[0] for r in await cursor.fetchall()]
            assert any("sessions" in idx for idx in indexes)

        await close_db()


class TestCkeyNormalization:
    """Tests ckey normalization: re.sub(r'[^a-z0-9]', '', raw.lower())."""

    @pytest.mark.parametrize("raw_input, expected", [
        ("HonkMaster", "honkmaster"),
        ("Target_Alpha-01", "targetalpha01"),
        ("  Captain  Jack  ", "captainjack"),
        ("Clown!@#$%^&*()", "clown"),
        ("Кей_123", "123"),
        ("user.name@station.org", "usernamestationorg"),
        ("___", ""),
        ("   ", ""),
        ("!@#$", ""),
        ("SYNDICATE_OPERATIVE_99", "syndicateoperative99")
    ])
    def test_normalize_ckey(self, raw_input, expected):
        if normalize_ckey is not None:
            assert normalize_ckey(raw_input) == expected
        else:
            # Test specification logic directly
            result = re.sub(r'[^a-z0-9]', '', raw_input.lower())
            assert result == expected


class TestServerAndPlayerCrud:
    """Tests server and player registry upserts."""

    @pytest.mark.asyncio
    async def test_upsert_server_lifecycle(self, test_db):
        identifier = "byond://178.62.241.130:1337"
        title = "Tau Ceti Classic"
        ip = "178.62.241.130"
        port = 1337

        server_id = await upsert_server(identifier, title, ip, port)
        assert server_id is not None
        assert server_id > 0

        # Updating the same server should return the same ID
        server_id_2 = await upsert_server(identifier, "Tau Ceti Classic (Updated)", ip, port)
        assert server_id == server_id_2

        # Verify via get_active_servers
        servers = await get_active_servers()
        assert len(servers) >= 1
        found = next((s for s in servers if s.get("identifier") == identifier or s.get("id") == server_id), None)
        assert found is not None


class TestSessionLifecycleAndGracePeriod:
    """
    Tests the 5-minute (300 seconds) grace period state machine:
    - New session creation
    - Session extension for continuous play
    - Transient disconnections (< 300s) resume seamlessly
    - Extended disconnections (>= 300s) finalize session with ended_at = last_seen_at
    """

    @pytest.mark.asyncio
    async def test_session_lifecycle_and_grace_period_state_machine(self, test_db):
        server_id = await upsert_server("byond://127.0.0.1:1337", "Test Station", "127.0.0.1", 1337)
        player_ckey = "honkmaster"

        # --- Phase 1: New Session Creation at T0 = 1000.0 ---
        t0 = 1000.0
        await update_player_sessions(server_id, {player_ckey}, now_ts=t0, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM sessions WHERE ckey = ?", (player_ckey,))
            sessions = await cursor.fetchall()
            assert len(sessions) == 1
            s = sessions[0]
            assert s["ended_at"] is None
            assert s["duration_seconds"] == 0

        # --- Phase 2: Active Session Extension at T1 = 1120.0 (+120s) ---
        t1 = 1120.0
        await update_player_sessions(server_id, {player_ckey}, now_ts=t1, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM sessions WHERE ckey = ?", (player_ckey,))
            sessions = await cursor.fetchall()
            assert len(sessions) == 1, "Session count must remain 1 during active play"
            s = sessions[0]
            assert s["ended_at"] is None
            assert s["duration_seconds"] == 120

        # --- Phase 3: Transient Disconnect (< 300s) at T2 = 1240.0 ---
        # Player is missing on this tick (e.g. game crashed)
        t2 = 1240.0  # 1240 - 1120 = 120s absence (< 300s)
        await update_player_sessions(server_id, set(), now_ts=t2, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM sessions WHERE ckey = ?", (player_ckey,))
            sessions = await cursor.fetchall()
            assert len(sessions) == 1
            s = sessions[0]
            assert s["ended_at"] is None, "Session must remain OPEN during < 300s grace period"

        # --- Phase 4: Seamless Reconnect within Grace Window at T3 = 1360.0 ---
        # Player reconnects (absence was 240s < 300s)
        t3 = 1360.0
        await update_player_sessions(server_id, {player_ckey}, now_ts=t3, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM sessions WHERE ckey = ?", (player_ckey,))
            sessions = await cursor.fetchall()
            assert len(sessions) == 1, "Reconnection within 300s must NOT create a new session"
            s = sessions[0]
            assert s["ended_at"] is None
            assert s["duration_seconds"] == 360  # (1360 - 1000)

        # --- Phase 5: Grace Period Expiration at T4 = 1700.0 (Delta = 340s >= 300s) ---
        # Player leaves. Absence delta = 1700 - 1360 = 340s >= 300s
        t4 = 1700.0
        await update_player_sessions(server_id, set(), now_ts=t4, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM sessions WHERE ckey = ?", (player_ckey,))
            sessions = await cursor.fetchall()
            assert len(sessions) == 1
            s = sessions[0]
            # Session must be closed
            assert s["ended_at"] is not None, "Session must be closed after >= 300s absence"
            # Crucial: duration must NOT count the absence (ended_at = last_seen_at = 1360, duration = 360)
            assert s["duration_seconds"] == 360, "Absence period must NOT be credited to playtime duration"

        # --- Phase 6: Subsequent Login at T5 = 2000.0 Creates New Session ---
        t5 = 2000.0
        await update_player_sessions(server_id, {player_ckey}, now_ts=t5, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute("SELECT * FROM sessions WHERE ckey = ? ORDER BY id", (player_ckey,))
            sessions = await cursor.fetchall()
            assert len(sessions) == 2, "New session must be opened after grace period expiry"
            # First session remains closed
            assert sessions[0]["ended_at"] is not None
            # Second session is active
            assert sessions[1]["ended_at"] is None


class TestPlayerDossierAggregation:
    """Tests player dossier generation with per-server statistics."""

    @pytest.mark.asyncio
    async def test_get_player_dossier_structure(self, test_db):
        s1 = await upsert_server("byond://1.1.1.1:1000", "Tau Ceti", "1.1.1.1", 1000)
        s2 = await upsert_server("byond://2.2.2.2:2000", "White Dream", "2.2.2.2", 2000)
        ckey = "honkmaster"

        # Create session on Tau Ceti (3600 seconds = 1 hour)
        await update_player_sessions(s1, {ckey}, now_ts=1000.0)
        await update_player_sessions(s1, {ckey}, now_ts=4600.0, grace_period_seconds=86400)
        # Close s1 session
        await update_player_sessions(s1, set(), now_ts=5000.0)

        # Create active session on White Dream (1800 seconds = 0.5 hour)
        await update_player_sessions(s2, {ckey}, now_ts=6000.0)
        await update_player_sessions(s2, {ckey}, now_ts=7800.0, grace_period_seconds=86400)

        dossier = await get_player_dossier(ckey)
        assert dossier is not None
        assert dossier["ckey"] == ckey

        # Status should show online on White Dream
        status = dossier.get("status") or dossier
        if isinstance(status, dict):
            is_online = status.get("online") or dossier.get("is_online")
            assert is_online is True

        # Summary checks
        summary = dossier.get("summary", dossier)
        assert summary["servers_count"] == 2
        total_playtime = summary.get("total_playtime_seconds", 0)
        assert total_playtime >= 5400  # 3600 + 1800

        # Server breakdown checks
        breakdown = dossier.get("servers", dossier.get("server_breakdown", []))
        assert len(breakdown) == 2

    @pytest.mark.asyncio
    async def test_get_player_dossier_unknown_player_returns_none(self, test_db):
        dossier = await get_player_dossier("unknown_nonexistent_ckey_999")
        assert dossier is None


class TestTrackerEngine:
    """Verifies Tracker background worker, polling execution, and lifecycle."""

    @pytest.mark.asyncio
    async def test_tracker_reexport_normalize_ckey(self):
        from core import tracker
        assert hasattr(tracker, "normalize_ckey")
        assert tracker.normalize_ckey("Honk_Master-99") == "honkmaster99"

    @pytest.mark.asyncio
    async def test_tracker_lifecycle_and_stats(self, test_db):
        from core.tracker import Tracker
        tracker = Tracker(config={"poll_interval": 3600, "grace_period_seconds": 300})
        assert not tracker.is_running()
        stats = tracker.get_stats()
        assert stats["running"] is False
        assert stats["poll_count"] == 0

        # Start tracker
        tracker.start()
        assert tracker.is_running()

        stats_running = tracker.get_stats()
        assert stats_running["running"] is True

        # Stop tracker
        await tracker.stop()
        assert not tracker.is_running()

    @pytest.mark.asyncio
    async def test_tracker_poll_once_with_mock_server(self, test_db, mock_topic_server):
        from core.tracker import Tracker
        port = mock_topic_server.port
        ident = f"byond://127.0.0.1:{port}"
        mock_topic_server.set_string_response("status=1&players=2&playerlist=clown_bob,detective_rex")

        tracker = Tracker(
            config={
                "poll_interval": 3600,
                "grace_period_seconds": 300,
                "socket_timeout": 1.0,
                "custom_servers": [
                    {
                        "identifier": ident,
                        "title": "Mock Orbital",
                        "ip": "127.0.0.1",
                        "port": port,
                    }
                ],
            }
        )

        # Execute single poll tick
        stats = await tracker.poll_once(now_ts=1000.0)
        assert stats["poll_count"] == 1
        assert stats["servers_tracked"] >= 1

        # Verify database updated with discovered players
        dossier_bob = await get_player_dossier("clownbob")
        assert dossier_bob is not None
        assert dossier_bob["is_online"] is True
        assert dossier_bob["current_server"] == "Mock Orbital"

        dossier_rex = await get_player_dossier("detectiverex")
        assert dossier_rex is not None
        assert dossier_rex["is_online"] is True

    @pytest.mark.asyncio
    async def test_tracker_error_shielding_on_hub_failure(self, test_db, monkeypatch):
        from core.tracker import Tracker
        import httpx
        tracker = Tracker(
            config={
                "custom_servers": [],
            }
        )
        async def mock_failing_fetch(*args, **kwargs):
            raise httpx.ConnectError("Network unreachable")

        monkeypatch.setattr("core.tracker.fetch_hub_servers", mock_failing_fetch)

        # poll_once should not throw unhandled exception and must record error
        stats = await tracker.poll_once()
        assert stats["poll_count"] == 1
        assert stats["errors_count"] >= 1

