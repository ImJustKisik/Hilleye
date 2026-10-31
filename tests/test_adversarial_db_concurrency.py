"""
Adversarial Stress Test Suite for SQLite WAL Concurrency, Multi-Connection Reader Contention,
Windows Handle Lifecycle, and Dossier Mathematical Correctness.

Tests:
1. Concurrent reads while background writes (update_player_sessions, upsert_server) are running.
2. Multi-connection reader contention across independent aiosqlite connections.
3. Multiple concurrent writers respecting PRAGMA busy_timeout=5000.
4. Windows file handle lifecycle and absence of lock leaks on db close.
5. Windows path handling with spaces and unicode.
6. Dossier mathematical correctness: percentages, zero-division, hour conversions, formatting.
7. Extreme scale formatting (massive hours, unicode titles, missing servers).
8. High-volume dossier calculation performance under 5,000+ session records.
"""

import asyncio
import os
from pathlib import Path
import random
import time
import pytest
import aiosqlite

from core.db import (
    Database,
    close_db,
    format_duration,
    format_timestamp,
    get_active_servers,
    get_connection,
    get_player_dossier,
    init_db,
    normalize_ckey,
    update_player_sessions,
    upsert_server,
)


class TestWindowsConnectionLifecycle:
    """Tests Windows file handle closing and lock leak prevention."""

    @pytest.mark.asyncio
    async def test_close_db_releases_windows_file_handle(self, tmp_path):
        db_file = tmp_path / "lifecycle_test.db"
        await init_db(str(db_file))

        # Perform multiple operations
        s_id = await upsert_server("byond://127.0.0.1:1001", "Sector Alpha", "127.0.0.1", 1001)
        await update_player_sessions(s_id, {"operative1", "operative2"}, now_ts=1000.0)
        dossier = await get_player_dossier("operative1")
        assert dossier is not None

        # Cleanly close
        await close_db()

        # On Windows, if a file handle or lock leaks, os.remove or Path.unlink raises PermissionError
        assert db_file.exists()
        try:
            db_file.unlink()
        except PermissionError as exc:
            pytest.fail(f"Windows file handle was not released by close_db(): {exc}")

        # Check that file is indeed removed
        assert not db_file.exists()

    @pytest.mark.asyncio
    async def test_reinit_db_cycles_cleanly_on_windows(self, tmp_path):
        for i in range(5):
            db_file = tmp_path / f"cycle_{i}.db"
            await init_db(str(db_file))
            s_id = await upsert_server(f"byond://127.0.0.1:{2000+i}", f"Station {i}", "127.0.0.1", 2000 + i)
            assert s_id > 0
            await close_db()
            # Must be immediately deletable
            db_file.unlink()
            assert not db_file.exists()

    @pytest.mark.asyncio
    async def test_windows_paths_with_spaces_and_unicode(self, tmp_path):
        """Tests that Windows paths containing spaces and non-ASCII work seamlessly."""
        custom_dir = tmp_path / "CyberSyn Test Folder 2026" / "Оперативный архив"
        custom_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(custom_dir / "tracker space test.db")

        await init_db(db_path)
        sid = await upsert_server("byond://127.0.0.1:9090", "Unicode Station", "127.0.0.1", 9090)
        assert sid > 0
        await close_db()

        assert Path(db_path).exists()
        Path(db_path).unlink()
        assert not Path(db_path).exists()


class TestConcurrentReaderAndWriterContention:
    """Tests WAL mode under concurrent reading and writing."""

    @pytest.mark.asyncio
    async def test_concurrent_reads_during_continuous_writes(self, temp_db_path):
        """Simulates 30 concurrent web readers querying dossiers while background worker writes."""
        await init_db(temp_db_path)

        # Prepopulate with 10 servers and 50 players
        server_ids = []
        for i in range(10):
            sid = await upsert_server(
                f"byond://10.0.0.{i}:5000", f"Monitored Outpost {i}", f"10.0.0.{i}", 5000
            )
            server_ids.append(sid)

        all_players = [f"agent{n:02d}" for n in range(50)]
        for sid in server_ids:
            await update_player_sessions(sid, set(all_players[:25]), now_ts=1000.0)

        stop_writer = asyncio.Event()
        write_errors = []
        read_errors = []
        write_count = 0
        read_count = 0

        # Background writer task: simulating tracker polling ticks
        async def writer_loop():
            nonlocal write_count
            tick = 1000.0
            try:
                while not stop_writer.is_set():
                    tick += 120.0
                    for sid in server_ids:
                        active = set(all_players[sid : sid + 20])
                        await update_player_sessions(sid, active, now_ts=tick, grace_period_seconds=300)
                        await upsert_server(
                            f"byond://10.0.0.{sid-1}:5000",
                            f"Monitored Outpost {sid-1} (tick {int(tick)})",
                            f"10.0.0.{sid-1}",
                            5000,
                            now_ts=tick,
                        )
                        write_count += 1
                        await asyncio.sleep(0.005)
            except Exception as e:
                write_errors.append(e)

        # Web reader task: simulating web requests querying dossiers
        async def reader_task(player_ckey: str):
            nonlocal read_count
            for _ in range(15):
                try:
                    dossier = await get_player_dossier(player_ckey)
                    assert dossier is None or isinstance(dossier, dict)
                    read_count += 1
                    await asyncio.sleep(0.002)
                except Exception as e:
                    read_errors.append(e)

        writer_coro = asyncio.create_task(writer_loop())

        # Launch 30 concurrent readers
        readers = [asyncio.create_task(reader_task(f"agent{n:02d}")) for n in range(30)]
        await asyncio.gather(*readers)

        stop_writer.set()
        await writer_coro

        await close_db()

        assert not write_errors, f"Writer encountered database errors: {write_errors}"
        assert not read_errors, f"Reader encountered database locking errors: {read_errors}"
        assert write_count > 0, "Writer did not execute any writes"
        assert read_count == 30 * 15, f"Expected {30*15} reads, got {read_count}"

    @pytest.mark.asyncio
    async def test_multi_connection_readers_during_external_writes(self, temp_db_path):
        """
        Tests true multi-connection concurrency:
        Separate aiosqlite connections reading while a writer connection updates the database.
        """
        await init_db(temp_db_path)
        s_id = await upsert_server("byond://1.1.1.1:7777", "High Traffic Node", "1.1.1.1", 7777)
        for i in range(20):
            await update_player_sessions(s_id, {f"target{i}"}, now_ts=1000.0)

        stop_event = asyncio.Event()
        external_read_errors = []
        external_reads_done = 0

        # Writer on the main engine connection
        async def continuous_writer():
            t = 1000.0
            while not stop_event.is_set():
                t += 50.0
                await update_player_sessions(s_id, {"target1", "target2", "target3"}, now_ts=t)
                await asyncio.sleep(0.01)

        # 10 separate connections reading concurrently
        async def external_reader_worker():
            nonlocal external_reads_done
            async with aiosqlite.connect(temp_db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA busy_timeout = 5000;")
                while not stop_event.is_set():
                    try:
                        cursor = await conn.execute(
                            "SELECT COUNT(*) as c FROM sessions WHERE server_id = ?", (s_id,)
                        )
                        row = await cursor.fetchone()
                        assert row["c"] >= 1
                        external_reads_done += 1
                        await asyncio.sleep(0.005)
                    except Exception as exc:
                        external_read_errors.append(exc)
                        break

        writer_task = asyncio.create_task(continuous_writer())
        reader_tasks = [asyncio.create_task(external_reader_worker()) for _ in range(10)]

        await asyncio.sleep(0.5)
        stop_event.set()

        await writer_task
        await asyncio.gather(*reader_tasks)
        await close_db()

        assert not external_read_errors, f"External multi-connection reader failed: {external_read_errors}"
        assert external_reads_done > 50, f"Expected >50 external reads, got {external_reads_done}"

    @pytest.mark.asyncio
    async def test_concurrent_writers_busy_timeout_resolution(self, temp_db_path):
        """
        Verifies that multiple independent connections attempting writes
        resolve cleanly under PRAGMA busy_timeout=5000 rather than crashing.
        """
        await init_db(temp_db_path)
        await close_db()  # Release global connection to test multi-writer connections

        async def worker_write(worker_id: int):
            async with aiosqlite.connect(temp_db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA journal_mode = WAL;")
                await conn.execute("PRAGMA busy_timeout = 5000;")
                for i in range(10):
                    await conn.execute(
                        "INSERT INTO servers (identifier, title, ip, port, last_seen, is_active) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (f"byond://worker_{worker_id}_{i}", f"Server {worker_id}-{i}", "127.0.0.1", 1000 + i, time.time(), 1)
                    )
                    await conn.commit()
                    await asyncio.sleep(0.002)

        tasks = [asyncio.create_task(worker_write(w)) for w in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                pytest.fail(f"Concurrent writer failed with: {res}")

        async with aiosqlite.connect(temp_db_path) as conn:
            cursor = await conn.execute("SELECT COUNT(*) FROM servers")
            count = (await cursor.fetchone())[0]
            assert count == 50

    @pytest.mark.asyncio
    async def test_randomized_fuzzing_readers_writers_contention(self, temp_db_path):
        """Fuzzing stress-test: randomly interleaved reads, writes, and non-existent lookups."""
        await init_db(temp_db_path)
        sids = [await upsert_server(f"byond://10.1.1.{i}:1000", f"Fuzz Node {i}", f"10.1.1.{i}", 1000) for i in range(5)]
        players = [f"fuzzplayer_{p}" for p in range(30)]

        async def random_op():
            op = random.randint(0, 3)
            if op == 0:
                # Upsert server
                sid = random.choice(sids)
                await upsert_server(f"byond://10.1.1.{sid}:1000", f"Fuzz Node {sid} Renamed", f"10.1.1.{sid}", 1000)
            elif op == 1:
                # Update sessions
                sid = random.choice(sids)
                active = set(random.sample(players, k=random.randint(1, 10)))
                await update_player_sessions(sid, active, now_ts=time.time())
            elif op == 2:
                # Read valid dossier
                target = random.choice(players)
                dossier = await get_player_dossier(target)
                assert dossier is None or isinstance(dossier, dict)
            else:
                # Read invalid/non-existent dossier
                bogus = f"bogus_{random.randint(9999, 999999)}"
                dossier = await get_player_dossier(bogus)
                assert dossier is None

        tasks = [asyncio.create_task(random_op()) for _ in range(80)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = [r for r in results if isinstance(r, Exception)]
        await close_db()
        assert not errors, f"Fuzzing operations encountered errors: {errors}"


class TestDossierMathematicalCorrectness:
    """Verifies precision, zero division guards, percentage calculations, and hour conversions."""

    @pytest.mark.asyncio
    async def test_zero_playtime_player_zero_division_guard(self, test_db):
        """A player whose session duration is 0 seconds should not trigger ZeroDivisionError."""
        s_id = await upsert_server("byond://127.0.0.1:3001", "Station Zero", "127.0.0.1", 3001)
        ckey = "zerodurationoperative"

        # Session starts now, duration is 0
        await update_player_sessions(s_id, {ckey}, now_ts=1000.0)

        dossier = await get_player_dossier(ckey)
        assert dossier is not None
        assert dossier["total_playtime_seconds"] == 0
        assert dossier["total_hours"] == 0.0
        assert dossier["servers_count"] == 1

        breakdown = dossier["server_breakdown"]
        assert len(breakdown) == 1
        entry = breakdown[0]
        assert entry["seconds"] == 0
        assert entry["hours"] == 0.0
        assert entry["percentage"] == 0.0

    @pytest.mark.asyncio
    async def test_percentages_sum_to_100_percent(self, test_db):
        """Playtime distribution percentages should sum to 100% within rounding tolerance."""
        s1 = await upsert_server("byond://1.1.1.1:1001", "Outpost Alpha", "1.1.1.1", 1001)
        s2 = await upsert_server("byond://1.1.1.1:1002", "Outpost Beta", "1.1.1.1", 1002)
        s3 = await upsert_server("byond://1.1.1.1:1003", "Outpost Gamma", "1.1.1.1", 1003)

        ckey = "multisectoroperative"

        # S1: 1800s (30m)
        await update_player_sessions(s1, {ckey}, now_ts=1000.0)
        await update_player_sessions(s1, {ckey}, now_ts=2800.0)
        await update_player_sessions(s1, set(), now_ts=3200.0)

        # S2: 3600s (60m)
        await update_player_sessions(s2, {ckey}, now_ts=4000.0)
        await update_player_sessions(s2, {ckey}, now_ts=7600.0)
        await update_player_sessions(s2, set(), now_ts=8000.0)

        # S3: 5400s (90m)
        await update_player_sessions(s3, {ckey}, now_ts=9000.0)
        await update_player_sessions(s3, {ckey}, now_ts=14400.0)
        await update_player_sessions(s3, set(), now_ts=15000.0)

        dossier = await get_player_dossier(ckey)
        assert dossier is not None
        total_sec = dossier["total_playtime_seconds"]
        assert total_sec == 1800 + 3600 + 5400  # 10800s = 3.0h

        assert dossier["total_hours"] == 3.0
        assert dossier["servers_count"] == 3

        breakdown = dossier["server_breakdown"]
        assert len(breakdown) == 3

        pct_sum = sum(b["percentage"] for b in breakdown)
        assert abs(pct_sum - 100.0) <= 0.2, f"Percentages sum {pct_sum} deviates from 100.0%"

    @pytest.mark.asyncio
    async def test_hour_conversions_and_rounding(self, test_db):
        """Tests that hours = round(seconds / 3600.0, 1) across different ranges."""
        s1 = await upsert_server("byond://1.1.1.1:1001", "Station Prime", "1.1.1.1", 1001)
        ckey = "hourtestoperative"

        # 4500 seconds = 1.25 hours -> round(1.25, 1) = 1.2 or 1.3
        await update_player_sessions(s1, {ckey}, now_ts=1000.0)
        await update_player_sessions(s1, {ckey}, now_ts=5500.0)

        dossier = await get_player_dossier(ckey)
        assert dossier is not None
        assert dossier["total_playtime_seconds"] == 4500
        assert dossier["total_hours"] == round(4500 / 3600.0, 1)

    @pytest.mark.asyncio
    async def test_dossier_with_unicode_server_titles_and_special_chars(self, test_db):
        """Verifies dossiers render properly when server titles contain Cyrillic, symbols, quotes."""
        title = "Тау Кита [Roleplay] - «Центральный Комплекс» 🚀 <tag>"
        sid = await upsert_server("byond://9.9.9.9:1234", title, "9.9.9.9", 1234)
        ckey = "specialtitletest"

        await update_player_sessions(sid, {ckey}, now_ts=1000.0)
        await update_player_sessions(sid, {ckey}, now_ts=4600.0)

        dossier = await get_player_dossier(ckey)
        assert dossier is not None
        assert dossier["current_server"] == title
        assert dossier["server_breakdown"][0]["title"] == title
        assert dossier["recent_sessions"][0]["server_title"] == title

    @pytest.mark.asyncio
    async def test_dossier_extreme_hours_formatting(self, test_db):
        """Tests handling of veterans with thousands of hours."""
        sid = await upsert_server("byond://1.2.3.4:5555", "Veteran Server", "1.2.3.4", 5555)
        ckey = "ancientveteran"

        # 36,000,000 seconds = 10,000 hours
        await update_player_sessions(sid, {ckey}, now_ts=1000.0)
        await update_player_sessions(sid, {ckey}, now_ts=36001000.0)
        await update_player_sessions(sid, set(), now_ts=36001500.0)

        dossier = await get_player_dossier(ckey)
        assert dossier is not None
        assert dossier["total_hours"] == 10000.0
        assert "10000h" in format_duration(36000000)

    def test_format_duration_tactical_strings(self):
        """Verifies tactical format_duration formatting output."""
        assert format_duration(0) == "0s"
        assert format_duration(-100) == "0s"
        assert format_duration(45) == "45s"
        assert format_duration(59) == "59s"
        assert format_duration(60) == "1m 0s"
        assert format_duration(65) == "1m 5s"
        assert format_duration(3599) == "59m 59s"
        assert format_duration(3600) == "1h 0m"
        assert format_duration(3665) == "1h 1m"
        assert format_duration(7200) == "2h 0m"
        assert format_duration(7290) == "2h 1m"
        assert format_duration(86400) == "24h 0m"

    def test_format_timestamp_edge_cases(self):
        """Verifies format_timestamp handles ints, floats, None, strings, and datetimes."""
        assert format_timestamp(None) is None
        assert format_timestamp(1000000000) == "2001-09-09 01:46:40"
        assert format_timestamp(1000000000.0) == "2001-09-09 01:46:40"
        assert format_timestamp("already_formatted") == "already_formatted"


class TestHighVolumeDossierPerformance:
    """Stress tests dossier query performance with 1,000 players and 5,000 sessions."""

    @pytest.mark.asyncio
    async def test_dossier_query_latency_under_load(self, temp_db_path):
        await init_db(temp_db_path)

        async with aiosqlite.connect(temp_db_path) as conn:
            for s in range(10):
                await conn.execute(
                    "INSERT INTO servers (id, identifier, title, ip, port, last_seen, is_active) "
                    "VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (s + 1, f"byond://10.0.0.{s}:1000", f"Cluster Node {s}", f"10.0.0.{s}", 1000, 1000000.0)
                )

            players_data = [(f"player{p:04d}", 1000000.0, 1050000.0) for p in range(500)]
            await conn.executemany(
                "INSERT INTO players (ckey, first_seen, last_seen) VALUES (?, ?, ?)",
                players_data
            )

            sessions_data = []
            for sess_id in range(5000):
                p_idx = sess_id % 500
                s_idx = (sess_id % 10) + 1
                ckey = f"player{p_idx:04d}"
                start_ts = 1000000.0 + (sess_id * 10)
                dur = 300 + (sess_id % 3600)
                end_ts = start_ts + dur
                sessions_data.append((ckey, s_idx, start_ts, end_ts, end_ts, dur))

            await conn.executemany(
                "INSERT INTO sessions (ckey, server_id, started_at, last_seen_at, ended_at, duration_seconds) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                sessions_data
            )
            await conn.commit()

        start_time = time.perf_counter()
        for i in range(100):
            target_ckey = f"player{(i * 5) % 500:04d}"
            dossier = await get_player_dossier(target_ckey)
            assert dossier is not None
            assert dossier["total_playtime_seconds"] > 0
            assert len(dossier["server_breakdown"]) > 0

        duration = time.perf_counter() - start_time
        avg_latency_ms = (duration / 100.0) * 1000.0

        await close_db()
        assert avg_latency_ms < 20.0, f"Average dossier query took {avg_latency_ms:.2f}ms (threshold 20ms)"


class TestDatabaseOOPWrapper:
    """Verifies that the Database class wrapper correctly delegates all operations."""

    @pytest.mark.asyncio
    async def test_database_class_wrapper(self, tmp_path):
        db_file = tmp_path / "wrapper_test.db"
        db = Database(str(db_file))
        await db.init_db()

        sid = await db.upsert_server("byond://5.5.5.5:8000", "Wrapper Station", "5.5.5.5", 8000)
        assert sid > 0

        servers = await db.get_active_servers()
        assert len(servers) == 1
        assert servers[0]["title"] == "Wrapper Station"

        await db.update_player_sessions(sid, {"agent_x"}, now_ts=1000.0)
        dossier = await db.get_player_dossier("agent_x")
        assert dossier is not None
        assert dossier["is_online"] is True

        await db.close_db()
        db_file.unlink()
        assert not db_file.exists()
