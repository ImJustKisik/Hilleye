"""Adversarial Challenge & Stress-Test Suite for Sessionization Engine.

Focuses on:
1. Boundary conditions for the 300s grace period (299s, 300s, 301s).
2. Reconnect after grace period expiration (with and without intermediate polls).
3. Rapid multi-server hopping and simultaneous multi-server presence.
4. Ckey normalization stress (Cyrillic, whitespace, symbols, SQL injection, degenerate keys).
5. Concurrency & WAL mode stress testing.
"""

import asyncio
import time
import pytest
import aiosqlite

from core.db import (
    close_db,
    format_duration,
    format_timestamp,
    get_active_servers,
    get_player_dossier,
    init_db,
    normalize_ckey,
    update_player_sessions,
    upsert_server,
)
from core.tracker import Tracker


# ============================================================================
# Section 1: Ckey Normalization & Sanitization Edge Cases
# ============================================================================

class TestAdversarialCkeyNormalization:
    """Stress-tests ckey normalization against extreme and adversarial inputs."""

    @pytest.mark.parametrize(
        "raw_input, expected",
        [
            # Standard & mixed case
            ("HonkMaster", "honkmaster"),
            ("UPPER_CASE_99", "uppercase99"),
            # Whitespace and tabs/newlines
            ("   leading_trailing   ", "leadingtrailing"),
            ("\t\n\r  spaced \t out \n", "spacedout"),
            ("    ", ""),
            ("", ""),
            # Cyrillic & Non-ASCII
            ("Кей_123", "123"),
            ("Иван_Грозный", ""),
            ("ПриветМир", ""),
            ("Ёлка2026", "2026"),
            # Accented & fullwidth
            ("Héllo_Wörld", "hllowrld"),
            ("café123", "caf123"),
            # Emojis & special symbols
            ("🔥Operative_007🎮", "operative007"),
            ("Clown!@#$%^&*()+=~`<>?:;\"'{}[]/|\\", "clown"),
            ("___---...", ""),
            ("!@#$%^&*()", ""),
            # SQL injection attack strings (must be cleanly sanitized to alphanumeric)
            ("admin' OR '1'='1", "adminor11"),
            ("'; DROP TABLE sessions; --", "droptablesessions"),
            ("UNION SELECT * FROM players", "unionselectfromplayers"),
            # Degenerate & types
            (None, ""),
        ],
    )
    def test_normalize_ckey_adversarial_inputs(self, raw_input, expected):
        assert normalize_ckey(raw_input) == expected

    @pytest.mark.parametrize(
        "non_str_input",
        [12345, 3.14159, True, False, ["ckey"], {"key": "val"}, (1, 2)],
    )
    def test_normalize_ckey_non_string_types(self, non_str_input):
        assert normalize_ckey(non_str_input) == ""

    def test_normalize_ckey_massive_string(self):
        huge = "A" * 50_000 + "_!@#" + "9" * 50_000
        result = normalize_ckey(huge)
        assert result == ""  # Unbounded remote identifiers must never reach storage.

    @pytest.mark.asyncio
    async def test_update_player_sessions_with_only_invalid_ckeys(self, test_db):
        """Passing only invalid/empty ckeys must not insert empty records."""
        server_id = await upsert_server("byond://127.0.0.1:1337", "Test Station", "127.0.0.1", 1337)
        invalid_set = {"", "   ", "___", "???", "Иван"}
        await update_player_sessions(server_id, invalid_set, now_ts=1000.0)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM players WHERE ckey = '' OR ckey IS NULL;")
            rows = await cur.fetchall()
            assert len(rows) == 0

            cur = await conn.execute("SELECT * FROM sessions;")
            s_rows = await cur.fetchall()
            assert len(s_rows) == 0

    @pytest.mark.asyncio
    async def test_dossier_queries_with_degenerate_ckeys(self, test_db):
        """Dossier lookup for degenerate ckeys must safely return None."""
        for bad_key in ["", "   ", "___", "!@#$", "NonExistentPlayer999"]:
            res = await get_player_dossier(bad_key)
            assert res is None


# ============================================================================
# Section 2: Grace Period Boundary State Machine (299s vs 300s vs 301s)
# ============================================================================

class TestGracePeriodBoundaries:
    """Rigorous boundary testing for the 5-minute (300s) grace period."""

    @pytest.mark.asyncio
    async def test_gap_299s_offline_retains_open_session(self, test_db):
        """At delta = 299s (within 300s grace period), session must remain OPEN."""
        server_id = await upsert_server("byond://1.1.1.1:1111", "Boundary Alpha", "1.1.1.1", 1111)
        ckey = "tester299"

        # T0 = 1000: Initial login
        await update_player_sessions(server_id, {ckey}, now_ts=1000.0, grace_period_seconds=300)

        # T1 = 1299: Offline poll at exactly 299s gap
        await update_player_sessions(server_id, set(), now_ts=1299.0, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM sessions WHERE ckey = ?;", (ckey,))
            sessions = await cur.fetchall()
            assert len(sessions) == 1
            assert sessions[0]["ended_at"] is None, "Session must stay open at delta=299s"
            assert sessions[0]["duration_seconds"] == 0

    @pytest.mark.asyncio
    async def test_gap_299s_followed_by_reconnect_resumes_seamlessly(self, test_db):
        """Reconnect within grace period (at delta = 299s) resumes the same session."""
        server_id = await upsert_server("byond://1.1.1.1:1111", "Boundary Alpha", "1.1.1.1", 1111)
        ckey = "tester299resume"

        # T0 = 1000: Start session
        await update_player_sessions(server_id, {ckey}, now_ts=1000.0, grace_period_seconds=300)

        # T1 = 1299: Player reappears at delta = 299s (< 300s)
        await update_player_sessions(server_id, {ckey}, now_ts=1299.0, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM sessions WHERE ckey = ?;", (ckey,))
            sessions = await cur.fetchall()
            assert len(sessions) == 1, "Must resume existing session, not create a new one"
            assert sessions[0]["ended_at"] is None
            assert sessions[0]["duration_seconds"] == 299

    @pytest.mark.asyncio
    async def test_gap_300s_offline_finalizes_session(self, test_db):
        """At delta = 300s (exact threshold), session MUST be finalized."""
        server_id = await upsert_server("byond://1.1.1.1:1111", "Boundary Alpha", "1.1.1.1", 1111)
        ckey = "tester300"

        # T0 = 1000: Login
        await update_player_sessions(server_id, {ckey}, now_ts=1000.0, grace_period_seconds=300)
        # T1 = 1100: Play for 100s
        await update_player_sessions(server_id, {ckey}, now_ts=1100.0, grace_period_seconds=300)

        # T2 = 1400: Offline poll at exactly 1100 + 300 = 1400 (delta = 300s)
        await update_player_sessions(server_id, set(), now_ts=1400.0, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM sessions WHERE ckey = ?;", (ckey,))
            sessions = await cur.fetchall()
            assert len(sessions) == 1
            s = sessions[0]
            assert s["ended_at"] is not None, "Session must be closed at delta=300s"
            assert float(s["ended_at"]) == 1100.0, "ended_at must match last_seen_at"
            assert s["duration_seconds"] == 100, "duration must not include the 300s absence"

    @pytest.mark.asyncio
    async def test_gap_301s_offline_finalizes_session(self, test_db):
        """At delta = 301s (exceeds threshold), session MUST be finalized."""
        server_id = await upsert_server("byond://1.1.1.1:1111", "Boundary Alpha", "1.1.1.1", 1111)
        ckey = "tester301"

        # T0 = 1000: Login
        await update_player_sessions(server_id, {ckey}, now_ts=1000.0, grace_period_seconds=300)
        # T1 = 1200: Active play
        await update_player_sessions(server_id, {ckey}, now_ts=1200.0, grace_period_seconds=300)

        # T2 = 1501: Offline poll at delta = 1501 - 1200 = 301s
        await update_player_sessions(server_id, set(), now_ts=1501.0, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute("SELECT * FROM sessions WHERE ckey = ?;", (ckey,))
            sessions = await cur.fetchall()
            assert len(sessions) == 1
            s = sessions[0]
            assert s["ended_at"] is not None
            assert float(s["ended_at"]) == 1200.0
            assert s["duration_seconds"] == 200

    @pytest.mark.asyncio
    async def test_reconnect_after_expired_grace_period_must_create_new_session(self, test_db):
        """
        Adversarial Scenario: Player is absent for >= 300s and reconnects.
        If player reconnects after 301s:
        - The old session MUST be finalized at the previous last_seen_at.
        - A NEW session MUST be started.
        - The absence interval must NOT be credited as continuous playtime!
        """
        server_id = await upsert_server("byond://1.1.1.1:1111", "Boundary Alpha", "1.1.1.1", 1111)
        ckey = "reconnect_expired_test"

        # T0 = 1000: Player starts playing
        await update_player_sessions(server_id, {ckey}, now_ts=1000.0, grace_period_seconds=300)
        # T1 = 1100: Player plays for 100s
        await update_player_sessions(server_id, {ckey}, now_ts=1100.0, grace_period_seconds=300)

        # Scenario A: Offline tick occurred at T2 = 1399 (delta 299s < 300s: stays open)
        await update_player_sessions(server_id, set(), now_ts=1399.0, grace_period_seconds=300)

        # T3 = 1401: Player reconnects! Delta from last_seen (1100) is 301s (>= 300s).
        # Absence exceeded grace period!
        await update_player_sessions(server_id, {ckey}, now_ts=1401.0, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            norm = normalize_ckey(ckey)
            cur = await conn.execute("SELECT * FROM sessions WHERE ckey = ? ORDER BY id ASC;", (norm,))
            sessions = await cur.fetchall()

            # Empirical check: did it properly finalize the expired session and open a new one?
            assert len(sessions) == 2, (
                f"Expected 2 sessions (old closed + new opened), but got {len(sessions)}. "
                "Reconnecting after >= 300s absence must NOT extend the expired session!"
            )
            # Old session must be closed at 1100
            assert sessions[0]["ended_at"] is not None
            assert float(sessions[0]["ended_at"]) == 1100.0
            assert sessions[0]["duration_seconds"] == 100
            # New session must be open at 1401
            assert sessions[1]["ended_at"] is None
            assert float(sessions[1]["started_at"]) == 1401.0

    @pytest.mark.asyncio
    async def test_reconnect_after_long_gap_without_intermediate_offline_poll(self, test_db):
        """
        Adversarial Scenario: Server goes down or tracking pauses.
        T0 = 1000: Player online (last_seen=1000).
        NO calls to update_player_sessions for 1 hour.
        T1 = 4600: Player is seen online again (+3600s gap).
        The old session MUST be closed at 1000, and a new session opened at 4600.
        The 3600 seconds of offline downtime must NOT be credited to the player!
        """
        server_id = await upsert_server("byond://1.1.1.1:1111", "Boundary Alpha", "1.1.1.1", 1111)
        ckey = "gap_no_poll_test"

        # T0 = 1000
        await update_player_sessions(server_id, {ckey}, now_ts=1000.0, grace_period_seconds=300)

        # T1 = 4600 (+3600s gap without intervening offline calls)
        await update_player_sessions(server_id, {ckey}, now_ts=4600.0, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            norm = normalize_ckey(ckey)
            cur = await conn.execute("SELECT * FROM sessions WHERE ckey = ? ORDER BY id ASC;", (norm,))
            sessions = await cur.fetchall()

            assert len(sessions) == 2, (
                f"Expected 2 sessions after 3600s gap, got {len(sessions)}. "
                "A gap of 3600s without intermediate polls must NOT resume the old session!"
            )
            assert sessions[0]["ended_at"] is not None
            assert float(sessions[0]["ended_at"]) == 1000.0
            assert sessions[0]["duration_seconds"] == 0
            assert sessions[1]["ended_at"] is None
            assert float(sessions[1]["started_at"]) == 4600.0

    @pytest.mark.asyncio
    async def test_normal_120s_polling_reconnect_after_360s_absence(self, test_db):
        """
        Adversarial Scenario: Normal 120s polling interval:
        Tick 0 (T=0): Player online (last_seen=0).
        Tick 1 (T=120): Player offline (absence=120s < 300s, stays open).
        Tick 2 (T=240): Player offline (absence=240s < 300s, stays open).
        Tick 3 (T=360): Player reconnects! Total absence = 360s >= 300s grace period.
        The session from T=0 MUST be finalized with ended_at=0, duration=0.
        A new session MUST be opened at T=360 with started_at=360.
        """
        server_id = await upsert_server("byond://1.1.1.1:1111", "Boundary Alpha", "1.1.1.1", 1111)
        ckey = "normal_polling_360s_gap"

        # Tick 0 at T=0
        await update_player_sessions(server_id, {ckey}, now_ts=0.0, grace_period_seconds=300)
        # Tick 1 at T=120
        await update_player_sessions(server_id, set(), now_ts=120.0, grace_period_seconds=300)
        # Tick 2 at T=240
        await update_player_sessions(server_id, set(), now_ts=240.0, grace_period_seconds=300)
        # Tick 3 at T=360 (player reconnects after 360s gap)
        await update_player_sessions(server_id, {ckey}, now_ts=360.0, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            norm = normalize_ckey(ckey)
            cur = await conn.execute("SELECT * FROM sessions WHERE ckey = ? ORDER BY id ASC;", (norm,))
            sessions = await cur.fetchall()

            assert len(sessions) == 2, (
                f"Expected 2 sessions (one closed at T=0, new one opened at T=360), but got {len(sessions)}. "
                "Reconnecting after 360s absence (exceeding 300s grace period) must NOT extend the expired session!"
            )
            assert sessions[0]["ended_at"] is not None
            assert float(sessions[0]["ended_at"]) == 0.0
            assert sessions[0]["duration_seconds"] == 0
            assert sessions[1]["ended_at"] is None
            assert float(sessions[1]["started_at"]) == 360.0



# ============================================================================
# Section 3: Rapid Multi-Server Hops & Simultaneous Presence
# ============================================================================

class TestMultiServerAndHopping:
    """Tests rapid server hops and simultaneous multi-server presence."""

    @pytest.mark.asyncio
    async def test_simultaneous_presence_on_multiple_servers(self, test_db):
        """Player reported online on two different servers concurrently."""
        s1 = await upsert_server("byond://1.1.1.1:1001", "Station 1", "1.1.1.1", 1001)
        s2 = await upsert_server("byond://2.2.2.2:2002", "Station 2", "2.2.2.2", 2002)
        ckey = "multiclown"

        # T0 = 1000: Online on Station 1
        await update_player_sessions(s1, {ckey}, now_ts=1000.0)
        # T0 = 1000: Also online on Station 2
        await update_player_sessions(s2, {ckey}, now_ts=1000.0)

        # T1 = 1200: Still online on both
        await update_player_sessions(s1, {ckey}, now_ts=1200.0)
        await update_player_sessions(s2, {ckey}, now_ts=1200.0)

        dossier = await get_player_dossier(ckey)
        assert dossier is not None
        assert dossier["is_online"] is True
        assert dossier["servers_count"] == 2
        assert dossier["total_playtime_seconds"] == 400  # 200s on s1 + 200s on s2

        breakdown = dossier["server_breakdown"]
        assert len(breakdown) == 2
        assert all(b["seconds"] == 200 for b in breakdown)

    @pytest.mark.asyncio
    async def test_rapid_server_ping_pong_hopping(self, test_db):
        """
        Player switches between Server A and Server B every 30 seconds for 10 hops.
        Verifies state machine integrity and absence of duplicate or conflicting rows.
        """
        s_a = await upsert_server("byond://10.0.0.1:1000", "Sector Alpha", "10.0.0.1", 1000)
        s_b = await upsert_server("byond://10.0.0.2:2000", "Sector Beta", "10.0.0.2", 2000)
        ckey = "hopper"

        t = 1000.0
        # 10 hops alternating every 30s
        for hop in range(10):
            current_srv = s_a if (hop % 2 == 0) else s_b
            other_srv = s_b if (hop % 2 == 0) else s_a

            await update_player_sessions(current_srv, {ckey}, now_ts=t, grace_period_seconds=300)
            await update_player_sessions(other_srv, set(), now_ts=t, grace_period_seconds=300)
            t += 30.0

        # Now player remains on Sector Alpha and permanently leaves Sector Beta
        # Advance 400s on Sector Alpha (Alpha stays active, Beta must expire)
        t += 400.0
        await update_player_sessions(s_a, {ckey}, now_ts=t, grace_period_seconds=300)
        await update_player_sessions(s_b, set(), now_ts=t, grace_period_seconds=300)

        async with aiosqlite.connect(test_db) as conn:
            conn.row_factory = aiosqlite.Row
            # Check Sector Beta session is properly closed
            cur = await conn.execute(
                "SELECT * FROM sessions WHERE ckey = ? AND server_id = ?;", (ckey, s_b)
            )
            beta_sessions = await cur.fetchall()
            assert len(beta_sessions) >= 1
            for bs in beta_sessions:
                assert bs["ended_at"] is not None, "Sector Beta session must be finalized"

            # Check Sector Alpha session is currently active
            cur = await conn.execute(
                "SELECT * FROM sessions WHERE ckey = ? AND server_id = ? AND ended_at IS NULL;",
                (ckey, s_a),
            )
            alpha_active = await cur.fetchall()
            assert len(alpha_active) == 1, "Sector Alpha session must remain open"

        dossier = await get_player_dossier(ckey)
        assert dossier is not None
        assert dossier["is_online"] is True
        assert dossier["current_server"] == "Sector Alpha"


# ============================================================================
# Section 4: Concurrency & Stress Harness
# ============================================================================

class TestConcurrencyAndStressHarness:
    """Stress tests concurrent operations and SQLite WAL mode resilience."""

    @pytest.mark.asyncio
    async def test_concurrent_session_updates(self, test_db):
        """Simulate high-concurrency writes from multiple servers simultaneously."""
        # Create 10 servers
        server_ids = []
        for i in range(10):
            sid = await upsert_server(f"byond://10.0.1.{i}:1000", f"Cluster Server {i}", f"10.0.1.{i}", 1000)
            server_ids.append(sid)

        # 50 players
        players = [f"player_{p}" for p in range(50)]

        # Run 5 consecutive poll ticks concurrently across all servers
        now = 1000.0
        for tick in range(5):
            tasks = []
            for s_idx, sid in enumerate(server_ids):
                # Distribute some players to each server
                assigned_players = set(players[s_idx * 5 : (s_idx + 1) * 5])
                tasks.append(
                    update_player_sessions(sid, assigned_players, now_ts=now, grace_period_seconds=300)
                )
            # Execute all server updates concurrently
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                assert not isinstance(r, Exception), f"Concurrent update raised exception: {r}"
            now += 60.0

        # Verify all 50 players have dossiers with zero errors
        dossier_tasks = [get_player_dossier(f"player_{p}") for p in range(50)]
        dossiers = await asyncio.gather(*dossier_tasks, return_exceptions=True)
        for d in dossiers:
            assert isinstance(d, dict), f"Failed to retrieve dossier: {d}"
            assert d["is_online"] is True
            assert d["total_playtime_seconds"] == 240


# ============================================================================
# Section 5: Formatting & Tracker Topic Edge Cases
# ============================================================================

class TestFormattingAndTrackerEdgeCases:
    """Tests utility edge cases and tracker Topic response handling."""

    def test_format_duration_edge_cases(self):
        assert format_duration(-100) == "0s"
        assert format_duration(0) == "0s"
        assert format_duration(45) == "45s"
        assert format_duration(60) == "1m 0s"
        assert format_duration(119) == "1m 59s"
        assert format_duration(3600) == "1h 0m"
        assert format_duration(3665) == "1h 1m"
        assert format_duration(86400) == "24h 0m"

    def test_format_timestamp_edge_cases(self):
        assert format_timestamp(None) is None
        assert format_timestamp(0) == "1970-01-01 00:00:00"
        assert format_timestamp("already_formatted") == "already_formatted"

    @pytest.mark.asyncio
    async def test_tracker_with_integer_player_count(self, test_db, mock_topic_server):
        """
        BYOND status packet returning integer player count (players=5) without playerlist.
        Must not crash or create phantom player named '5'.
        """
        port = mock_topic_server.port
        ident = f"byond://127.0.0.1:{port}"
        mock_topic_server.set_string_response("status=1&players=5&map_name=BoxStation")

        tracker = Tracker(
            config={
                "poll_interval": 3600,
                "grace_period_seconds": 300,
                "socket_timeout": 1.0,
                "custom_servers": [
                    {
                        "identifier": ident,
                        "title": "Box Station",
                        "ip": "127.0.0.1",
                        "port": port,
                    }
                ],
            }
        )

        stats = await tracker.poll_once(now_ts=1000.0)
        assert stats["poll_count"] == 1
        # Check that player '5' was not created as a player record
        dossier = await get_player_dossier("5")
        assert dossier is None

