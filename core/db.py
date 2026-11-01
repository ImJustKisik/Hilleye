"""SQLite WAL Database Storage Engine and Sessionization Queries for CyberSyn Intel.

Provides high-concurrency async SQLite storage in WAL mode via aiosqlite,
managing servers, players, and session lifecycles with a 5-minute (300s)
grace period state machine and tactical player dossier aggregations.
"""

import asyncio
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
BEGIN IMMEDIATE;

-- Tracked BYOND Servers
CREATE TABLE IF NOT EXISTS servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT UNIQUE NOT NULL,      -- e.g. "byond://178.62.241.130:1337"
    title TEXT NOT NULL,                  -- e.g. "Tau Ceti Classic"
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
    manifest_json TEXT
);

-- Server Online History Snapshots (Timeseries)
CREATE TABLE IF NOT EXISTS server_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
    timestamp REAL NOT NULL,
    player_count INTEGER NOT NULL,
    round_duration INTEGER DEFAULT 0,
    map_name TEXT
);

-- Normalized Players
CREATE TABLE IF NOT EXISTS players (
    ckey TEXT PRIMARY KEY,                -- e.g. "honkmaster" (lowercase, [a-z0-9])
    first_seen TIMESTAMP NOT NULL,
    last_seen TIMESTAMP NOT NULL
);

-- Continuous Online Playtime Sessions
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ckey TEXT NOT NULL REFERENCES players(ckey),
    server_id INTEGER NOT NULL REFERENCES servers(id),
    started_at TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,                  -- NULL while active; finalized upon closure
    duration_seconds INTEGER DEFAULT 0
);

-- Performance Indexes
CREATE INDEX IF NOT EXISTS idx_sessions_ckey ON sessions(ckey);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(ended_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_sessions_server ON sessions(server_id);
CREATE INDEX IF NOT EXISTS idx_sessions_online ON sessions(server_id,started_at) WHERE ended_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_snapshots_server_time ON server_snapshots(server_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_time ON server_snapshots(timestamp);

CREATE TABLE IF NOT EXISTS collector_state (
    id INTEGER PRIMARY KEY CHECK (id=1),
    completed_at REAL NOT NULL,
    healthy INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS server_player_totals (
    server_id INTEGER NOT NULL REFERENCES servers(id),
    ckey TEXT NOT NULL REFERENCES players(ckey),
    total_seconds INTEGER NOT NULL,
    sessions_count INTEGER NOT NULL,
    last_seen REAL,
    PRIMARY KEY(server_id,ckey)
);
CREATE INDEX IF NOT EXISTS idx_totals_rank ON server_player_totals(server_id,total_seconds DESC,ckey);
CREATE INDEX IF NOT EXISTS idx_sessions_player_seen ON sessions(server_id,ckey,last_seen_at);
CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
"""

_current_db_path: str = "tracker.db"
_connection: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()
_operation_lock = asyncio.Lock()
_read_slots = asyncio.Semaphore(8)
_operation_connection: ContextVar[Optional[aiosqlite.Connection]] = ContextVar("db_operation", default=None)


def _write_transaction(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        if _operation_connection.get() is not None:
            return await func(*args, **kwargs)
        async with _operation_lock:
            conn = await get_connection()
            token = _operation_connection.set(conn)
            try:
                await conn.execute("BEGIN IMMEDIATE")
                result = await func(*args, **kwargs)
                await conn.commit()
                return result
            except BaseException:
                await asyncio.shield(conn.rollback())
                raise
            finally:
                _operation_connection.reset(token)
    return wrapped


def _read_snapshot(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        if _operation_connection.get() is not None:
            return await func(*args, **kwargs)
        if _current_db_path == ":memory:":
            async with _operation_lock:
                conn = await get_connection()
                token = _operation_connection.set(conn)
                try:
                    return await func(*args, **kwargs)
                finally:
                    _operation_connection.reset(token)
        async with _read_slots:
            # Reads have their own WAL snapshot and never observe uncommitted writes.
            await get_connection()
            async with aiosqlite.connect(_current_db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA query_only=ON")
                await conn.execute("BEGIN")
                token = _operation_connection.set(conn)
                try:
                    return await func(*args, **kwargs)
                finally:
                    _operation_connection.reset(token)
    return wrapped


def normalize_ckey(raw: Optional[str]) -> str:
    """
    Normalizes a BYOND player key (ckey).
    Strips all characters except lowercase ASCII letters and numbers [a-z0-9].
    """
    if not raw or not isinstance(raw, str) or len(raw) > 128:
        return ""
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def format_timestamp(val: Any) -> Optional[str]:
    """Formats numeric timestamp or datetime into a readable UTC string."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        try:
            return datetime.fromtimestamp(float(val), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(val)
    if isinstance(val, datetime):
        return val.strftime("%Y-%m-%d %H:%M:%S")
    return str(val)


def format_duration(seconds: Union[int, float]) -> str:
    """Formats duration in seconds to tactical human readable string (e.g. '1h 24m' or '45s')."""
    total_sec = int(max(0, seconds))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def format_hms(seconds: Optional[Union[int, float]]) -> str:
    """Formats duration in seconds to HH:MM:SS string (e.g. '01:24:05')."""
    total_sec = int(max(0, seconds or 0))
    hours = total_sec // 3600
    minutes = (total_sec % 3600) // 60
    secs = total_sec % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


_SERVER_MIGRATION_COLUMNS: List[Tuple[str, str]] = [
    ("current_players", "INTEGER DEFAULT 0"),
    ("map_name", "TEXT"),
    ("round_duration", "INTEGER DEFAULT 0"),
    ("gamemode", "TEXT"),
    ("admins_count", "INTEGER DEFAULT 0"),
    ("version", "TEXT"),
    ("active_players", "INTEGER"),
    ("station_time", "TEXT"),
    ("enter_allowed", "BOOLEAN"),
    ("respawn_allowed", "BOOLEAN"),
    ("admin_list_json", "TEXT"),
    ("manifest_json", "TEXT"),
    ("roster_observed_at", "REAL"),
    ("telemetry_observed_at", "REAL"),
    ("manifest_observed_at", "REAL"),
]

_DEPT_LABELS: Dict[str, str] = {
    "command": "Command",
    "com": "Command",
    "sec": "Security",
    "security": "Security",
    "eng": "Engineering",
    "engineering": "Engineering",
    "med": "Medical",
    "medical": "Medical",
    "sci": "Science / Research",
    "science": "Science / Research",
    "car": "Supply / Cargo",
    "cargo": "Supply / Cargo",
    "sup": "Supply / Cargo",
    "exp": "Exploration",
    "exploration": "Exploration",
    "srv": "Service / Civilian",
    "civ": "Civilian",
    "civilian": "Civilian",
    "msc": "Miscellaneous",
    "misc": "Miscellaneous",
    "bot": "Synthetics / Silicons",
    "sil": "Synthetics / Silicons",
}


async def _migrate_schema(conn: aiosqlite.Connection) -> None:
    """Safely migrates existing database tables by adding missing columns via PRAGMA table_info."""
    async with conn.cursor() as cur:
        await cur.execute("PRAGMA table_info(servers);")
        rows = await cur.fetchall()
        existing_cols = {r[1] if isinstance(r, tuple) else r["name"] for r in rows}

        for col_name, col_def in _SERVER_MIGRATION_COLUMNS:
            if col_name not in existing_cols:
                try:
                    await cur.execute(f"ALTER TABLE servers ADD COLUMN {col_name} {col_def};")
                except Exception as exc:
                    await cur.execute("PRAGMA table_info(servers)")
                    if col_name not in {row[1] for row in await cur.fetchall()}:
                        raise RuntimeError(f"Cannot migrate servers.{col_name}") from exc


async def _open_database(path: str) -> aiosqlite.Connection:
    conn = aiosqlite.connect(path)
    try:
        await conn
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await _migrate_schema(conn)
        # Repair duplicate open records without removing any historical row.
        await conn.execute("""
            UPDATE sessions SET ended_at=last_seen_at
            WHERE ended_at IS NULL AND id NOT IN (
                SELECT MAX(id) FROM sessions WHERE ended_at IS NULL GROUP BY server_id,ckey
            )
        """)
        await conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_one_open
                            ON sessions(server_id,ckey) WHERE ended_at IS NULL""")
        await _initialize_aggregates(conn)
        await conn.commit()
        return conn
    except BaseException:
        await asyncio.shield(conn.close())
        raise


async def _initialize_aggregates(conn):
    row = await (await conn.execute("SELECT version FROM schema_migrations WHERE version=1")).fetchone()
    if row is None:
        await conn.execute("DELETE FROM server_player_totals")
        await conn.execute("""INSERT INTO server_player_totals
            SELECT server_id,ckey,COALESCE(SUM(duration_seconds),0),COUNT(*),MAX(last_seen_at)
            FROM sessions GROUP BY server_id,ckey""")
        await conn.execute("INSERT INTO schema_migrations VALUES (1)")
    insert = """INSERT INTO server_player_totals VALUES
        (NEW.server_id,NEW.ckey,COALESCE(NEW.duration_seconds,0),1,NEW.last_seen_at)
        ON CONFLICT(server_id,ckey) DO UPDATE SET
        total_seconds=total_seconds+COALESCE(NEW.duration_seconds,0),
        sessions_count=sessions_count+1,last_seen=MAX(last_seen,NEW.last_seen_at);"""
    remove = """UPDATE server_player_totals SET
        total_seconds=total_seconds-COALESCE(OLD.duration_seconds,0),sessions_count=sessions_count-1,
        last_seen=(SELECT MAX(last_seen_at) FROM sessions WHERE server_id=OLD.server_id AND ckey=OLD.ckey)
        WHERE server_id=OLD.server_id AND ckey=OLD.ckey;
        DELETE FROM server_player_totals WHERE server_id=OLD.server_id AND ckey=OLD.ckey AND sessions_count=0;"""
    for name, event, body in (("insert", "INSERT", insert), ("update", "UPDATE", remove + insert),
                              ("delete", "DELETE", remove)):
        await conn.execute(f"CREATE TRIGGER IF NOT EXISTS totals_{name} AFTER {event} ON sessions BEGIN {body} END")


async def init_db(db_path: str = "tracker.db") -> None:
    """
    Initializes SQLite database at db_path with WAL mode and schema definitions.
    Safely closes any existing connection and opens a new connection with foreign keys
    and busy_timeout enabled.
    """
    global _current_db_path, _connection, _operation_lock, _read_slots

    # Reinitialization after an event loop has shut down must recreate its gates.
    loop = asyncio.get_running_loop()
    old_loop = getattr(_operation_lock, "_loop", None)
    if old_loop is not None and old_loop is not loop and old_loop.is_closed():
        _operation_lock = asyncio.Lock()
    old_loop = getattr(_read_slots, "_loop", None)
    if old_loop is not None and old_loop is not loop and old_loop.is_closed():
        _read_slots = asyncio.Semaphore(8)

    async with _operation_lock, _db_lock:
        if _connection is not None:
            try:
                await _connection.close()
            except Exception:
                pass
            _connection = None

        _current_db_path = str(db_path)
        path_obj = Path(_current_db_path)
        if path_obj.parent and not path_obj.parent.exists():
            path_obj.parent.mkdir(parents=True, exist_ok=True)

        _connection = await _open_database(_current_db_path)
        logger.info("Initialized database at '%s' with WAL mode.", _current_db_path)


async def close_db() -> None:
    """Cleanly closes open database connection."""
    global _connection
    async with _operation_lock, _db_lock:
        if _connection is not None:
            try:
                await _connection.close()
            except Exception as exc:
                logger.debug("Error while closing database: %s", exc)
            _connection = None


async def get_connection() -> aiosqlite.Connection:
    """Retrieves or establishes the active aiosqlite connection."""
    global _connection, _current_db_path
    if _operation_connection.get() is not None:
        return _operation_connection.get()
    if _connection is None:
        async with _db_lock:
            if _connection is None:
                path_obj = Path(_current_db_path)
                if path_obj.parent and not path_obj.parent.exists():
                    path_obj.parent.mkdir(parents=True, exist_ok=True)
                _connection = await _open_database(_current_db_path)
    return _connection


@_write_transaction
async def upsert_server(
    identifier: str,
    title: str,
    ip: str,
    port: int,
    is_active: bool = True,
    now_ts: Optional[float] = None,
    current_players: Optional[int] = None,
    map_name: Optional[str] = None,
    round_duration: Optional[int] = None,
    gamemode: Optional[str] = None,
    admins_count: Optional[int] = None,
    version: Optional[str] = None,
    active_players: Optional[int] = None,
    station_time: Optional[str] = None,
    enter_allowed: Optional[bool] = None,
    respawn_allowed: Optional[bool] = None,
    admin_list: Optional[List[Dict[str, str]]] = None,
    manifest: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> int:
    """
    Upserts server record and returns its database ID.
    Updates title, ip, port, last_seen, is_active, and round metrics on identifier conflict.
    """
    ts = time.time() if now_ts is None else float(now_ts)
    ins_players = int(current_players) if current_players is not None else None
    ins_round_dur = int(round_duration) if round_duration is not None else None
    ins_admins = int(admins_count) if admins_count is not None else None
    ins_active_players = int(active_players) if active_players is not None else None
    ins_enter = (1 if enter_allowed else 0) if enter_allowed is not None else None
    ins_respawn = (1 if respawn_allowed else 0) if respawn_allowed is not None else None
    ins_admin_json = json.dumps(admin_list, ensure_ascii=False) if admin_list is not None else None
    ins_manifest_json = json.dumps(manifest, ensure_ascii=False) if manifest is not None else None

    conn = await get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO servers (
                identifier, title, ip, port, last_seen, is_active,
                current_players, map_name, round_duration, gamemode, admins_count, version,
                active_players, station_time, enter_allowed, respawn_allowed, admin_list_json, manifest_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identifier) DO UPDATE SET
                title = excluded.title,
                ip = excluded.ip,
                port = excluded.port,
                last_seen = MAX(servers.last_seen, excluded.last_seen),
                is_active = excluded.is_active,
                current_players = CASE WHEN ? IS NOT NULL THEN excluded.current_players ELSE servers.current_players END,
                map_name = CASE WHEN ? IS NOT NULL THEN excluded.map_name ELSE servers.map_name END,
                round_duration = CASE WHEN ? IS NOT NULL THEN excluded.round_duration ELSE servers.round_duration END,
                gamemode = CASE WHEN ? IS NOT NULL THEN excluded.gamemode ELSE servers.gamemode END,
                admins_count = CASE WHEN ? IS NOT NULL THEN excluded.admins_count ELSE servers.admins_count END,
                version = CASE WHEN ? IS NOT NULL THEN excluded.version ELSE servers.version END,
                active_players = CASE WHEN ? IS NOT NULL THEN excluded.active_players ELSE servers.active_players END,
                station_time = CASE WHEN ? IS NOT NULL THEN excluded.station_time ELSE servers.station_time END,
                enter_allowed = CASE WHEN ? IS NOT NULL THEN excluded.enter_allowed ELSE servers.enter_allowed END,
                respawn_allowed = CASE WHEN ? IS NOT NULL THEN excluded.respawn_allowed ELSE servers.respawn_allowed END,
                admin_list_json = CASE WHEN ? IS NOT NULL THEN excluded.admin_list_json ELSE servers.admin_list_json END,
                manifest_json = CASE WHEN ? IS NOT NULL THEN excluded.manifest_json ELSE servers.manifest_json END;
            """,
            (
                identifier,
                title,
                ip,
                int(port),
                ts,
                1 if is_active else 0,
                ins_players,
                map_name,
                ins_round_dur,
                gamemode,
                ins_admins,
                version,
                ins_active_players,
                station_time,
                ins_enter,
                ins_respawn,
                ins_admin_json,
                ins_manifest_json,
                current_players,
                map_name,
                round_duration,
                gamemode,
                admins_count,
                version,
                ins_active_players,
                station_time,
                ins_enter,
                ins_respawn,
                ins_admin_json,
                ins_manifest_json,
            ),
        )
        await cur.execute("SELECT id FROM servers WHERE identifier = ?;", (identifier,))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(f"Failed to retrieve id for upserted server '{identifier}'")
        if manifest is not None:
            await cur.execute("UPDATE servers SET manifest_observed_at=? WHERE id=?", (ts, row["id"]))
        return int(row["id"])


@_write_transaction
async def save_observation(server: Dict[str, Any], roster: Optional[Set[str]],
                           grace_period_seconds: int, topic_received: bool,
                           roster_complete: bool = True) -> int:
    """Persist one observation atomically; an unknown roster never signals logout."""
    sid = await upsert_server(**server)
    ts = server["now_ts"]
    if topic_received:
        conn = await get_connection()
        await conn.execute("UPDATE servers SET telemetry_observed_at=? WHERE id=?", (ts, sid))
    if roster is not None:
        await update_player_sessions(sid, roster, ts, grace_period_seconds, complete_roster=roster_complete)
        conn = await get_connection()
        if roster_complete:
            await conn.execute("UPDATE servers SET roster_observed_at=? WHERE id=?", (ts, sid))
    if server.get("current_players") is not None:
        await record_server_snapshot(sid, server["current_players"],
                                     server.get("round_duration"), server.get("map_name"), ts)
    return sid


@_write_transaction
async def expire_stale_servers(now_ts: float, max_age: int) -> None:
    """End observation windows at last confirmation, without crediting the outage."""
    conn = await get_connection()
    await conn.execute("UPDATE servers SET is_active=0 WHERE last_seen < ?", (now_ts - max_age,))
    await conn.execute("""UPDATE sessions SET ended_at=last_seen_at
                          WHERE ended_at IS NULL AND (last_seen_at < ? OR server_id IN
                          (SELECT id FROM servers WHERE is_active=0))""", (now_ts - max_age,))


@_write_transaction
async def record_collector_cycle(completed_at: float, healthy: bool) -> None:
    conn = await get_connection()
    await conn.execute("""INSERT INTO collector_state VALUES (1,?,?) ON CONFLICT(id)
                          DO UPDATE SET completed_at=excluded.completed_at, healthy=excluded.healthy""",
                       (completed_at, int(healthy)))


@_write_transaction
async def save_manifest(server_id: int, manifest: Dict[str, Any], ts: float) -> None:
    conn = await get_connection()
    await conn.execute("UPDATE servers SET manifest_json=?,manifest_observed_at=? WHERE id=?",
                       (json.dumps(manifest, ensure_ascii=False), ts, server_id))


@_read_snapshot
async def get_collector_state() -> Optional[Dict[str, Any]]:
    conn = await get_connection()
    row = await (await conn.execute("SELECT completed_at,healthy FROM collector_state WHERE id=1")).fetchone()
    return dict(row) if row else None


@_write_transaction
async def record_server_snapshot(
    server_id: int,
    player_count: int,
    round_duration: int = 0,
    map_name: Optional[str] = None,
    ts: Optional[float] = None,
) -> int:
    """Records a point-in-time server online snapshot for timeseries history."""
    snap_ts = time.time() if ts is None else float(ts)
    conn = await get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO server_snapshots (server_id, timestamp, player_count, round_duration, map_name)
            VALUES (?, ?, ?, ?, ?);
            """,
            (
                int(server_id),
                snap_ts,
                int(max(0, player_count)),
                int(max(0, round_duration)) if round_duration is not None else None,
                map_name,
            ),
        )
        return int(cur.lastrowid or 0)


@_write_transaction
async def prune_server_snapshots(
    max_age_days: int = 7,
    now_ts: Optional[float] = None,
) -> int:
    """Deletes server snapshots older than max_age_days and returns the count of deleted rows."""
    ref_ts = time.time() if now_ts is None else float(now_ts)
    cutoff_ts = ref_ts - (max_age_days * 86400.0)
    conn = await get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM server_snapshots WHERE timestamp < ?;",
            (cutoff_ts,),
        )
        deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        return int(deleted)


@_read_snapshot
async def get_active_servers() -> List[Dict[str, Any]]:
    """Returns list of active monitored servers."""
    conn = await get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                id, identifier, title, ip, port, last_seen, is_active,
                current_players, map_name, round_duration, gamemode, admins_count, version,
                active_players, station_time, enter_allowed, respawn_allowed
            FROM servers
            WHERE is_active = 1
            ORDER BY current_players DESC, title ASC;
            """
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "identifier": r["identifier"],
                "title": r["title"],
                "ip": r["ip"],
                "port": r["port"],
                "last_seen": r["last_seen"],
                "is_active": bool(r["is_active"]),
                "current_players": r["current_players"],
                "map_name": r["map_name"],
                "round_duration": r["round_duration"],
                "round_duration_str": format_hms(r["round_duration"]) if r["round_duration"] is not None else "Unknown",
                "gamemode": r["gamemode"],
                "admins_count": r["admins_count"],
                "version": r["version"],
                "active_players": int(r["active_players"]) if r["active_players"] is not None else None,
                "station_time": r["station_time"],
                "enter_allowed": bool(r["enter_allowed"]) if r["enter_allowed"] is not None else None,
                "respawn_allowed": bool(r["respawn_allowed"]) if r["respawn_allowed"] is not None else None,
            }
            for r in rows
        ]


@_write_transaction
async def update_player_sessions(
    server_id: int,
    online_ckeys: Set[str],
    now_ts: Optional[float] = None,
    grace_period_seconds: int = 300,
    complete_roster: bool = True,
) -> None:
    """
    Executes sessionization step for a single server:
    1. Online players:
       - Upsert into players table (first_seen, last_seen).
       - Locate open session (ended_at IS NULL).
       - If found: extend last_seen_at = now, duration_seconds = now - started_at.
       - If not found: insert new open session (started_at=now, last_seen_at=now, ended_at=NULL, duration_seconds=0).
    2. Offline players with open sessions on this server:
       - If now - last_seen_at >= grace_period_seconds:
         close session (ended_at = last_seen_at, duration_seconds = last_seen_at - started_at).
       - If now - last_seen_at < grace_period_seconds:
         retain session in active state (ended_at IS NULL).
    """
    ts = time.time() if now_ts is None else float(now_ts)
    normalized_online: Set[str] = {
        normalize_ckey(k) for k in online_ckeys if normalize_ckey(k)
    }

    conn = await get_connection()
    async with conn.cursor() as cur:
        # Step 1: Process Online Players
        for ckey in normalized_online:
            # Upsert into players
            await cur.execute(
                """
                INSERT INTO players (ckey, first_seen, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(ckey) DO UPDATE SET last_seen = MAX(players.last_seen, excluded.last_seen);
                """,
                (ckey, ts, ts),
            )

            # Check open session on this server
            await cur.execute(
                """
                SELECT id, started_at, last_seen_at FROM sessions
                WHERE ckey = ? AND server_id = ? AND ended_at IS NULL
                LIMIT 1;
                """,
                (ckey, server_id),
            )
            open_sess = await cur.fetchone()

            if open_sess is not None:
                session_id = open_sess["id"]
                started_at = float(open_sess["started_at"])
                prev_last_seen = float(open_sess["last_seen_at"])
                absence_gap = ts - prev_last_seen

                if absence_gap < 0:
                    continue  # Late or backwards observations cannot rewrite chronology.
                is_expired_gap = absence_gap >= grace_period_seconds

                if is_expired_gap:
                    # Absence exceeded grace period: finalize expired session at previous last_seen
                    expired_duration = max(0, int(round(prev_last_seen - started_at)))
                    await cur.execute(
                        """
                        UPDATE sessions
                        SET ended_at = ?, duration_seconds = ?
                        WHERE id = ?;
                        """,
                        (prev_last_seen, expired_duration, session_id),
                    )
                    # Open brand new session starting now
                    await cur.execute(
                        """
                        INSERT INTO sessions (ckey, server_id, started_at, last_seen_at, ended_at, duration_seconds)
                        VALUES (?, ?, ?, ?, NULL, 0);
                        """,
                        (ckey, server_id, ts, ts),
                    )
                else:
                    # Within grace period: extend open session
                    duration = max(0, int(round(ts - started_at)))
                    await cur.execute(
                        """
                        UPDATE sessions
                        SET last_seen_at = ?, duration_seconds = ?
                        WHERE id = ?;
                        """,
                        (ts, duration, session_id),
                    )
            else:
                # Open brand new session
                await cur.execute(
                    """
                    INSERT INTO sessions (ckey, server_id, started_at, last_seen_at, ended_at, duration_seconds)
                    VALUES (?, ?, ?, ?, NULL, 0);
                    """,
                    (ckey, server_id, ts, ts),
                )

        # Step 2: Process Offline Players with Open Sessions on This Server
        await cur.execute(
            """
            SELECT id, ckey, started_at, last_seen_at FROM sessions
            WHERE server_id = ? AND ended_at IS NULL;
            """,
            (server_id,),
        )
        active_server_sessions = await cur.fetchall()

        for s_row in active_server_sessions:
            ckey = s_row["ckey"]
            if complete_roster and ckey not in normalized_online:
                last_seen_at = float(s_row["last_seen_at"])
                started_at = float(s_row["started_at"])
                delta = ts - last_seen_at

                if delta >= grace_period_seconds:
                    # Finalize session after grace period expiry
                    final_duration = max(0, int(round(last_seen_at - started_at)))
                    await cur.execute(
                        """
                        UPDATE sessions
                        SET ended_at = ?, duration_seconds = ?
                        WHERE id = ?;
                        """,
                        (last_seen_at, final_duration, s_row["id"]),
                    )
                else:
                    # Grace period active: retain open state
                    pass

@_read_snapshot
async def get_player_dossier(ckey: str) -> Optional[Dict[str, Any]]:
    """
    Returns tactical player dossier dictionary or None if player does not exist.

    Aggregates:
    - Target status (online/offline, current sector, current duration)
    - Summary metrics (total playtime seconds, total hours, count of distinct servers visited, first and last seen)
    - Server breakdown with percentage and last visit timestamps
    - Recent sessions log (up to 10 entries)
    """
    norm_ckey = normalize_ckey(ckey)
    if not norm_ckey:
        return None

    conn = await get_connection()
    async with conn.cursor() as cur:
        # Check player existence
        await cur.execute(
            "SELECT ckey, first_seen, last_seen FROM players WHERE ckey = ?;",
            (norm_ckey,),
        )
        player_row = await cur.fetchone()
        if player_row is None:
            return None

        # Check active session
        await cur.execute(
            """
            SELECT s.id, s.server_id, s.started_at, s.last_seen_at, s.duration_seconds, srv.title as server_title
            FROM sessions s
            LEFT JOIN servers srv ON s.server_id = srv.id
            WHERE s.ckey = ? AND s.ended_at IS NULL
            ORDER BY s.started_at DESC
            LIMIT 1;
            """,
            (norm_ckey,),
        )
        active_sess = await cur.fetchone()

        if active_sess is not None:
            is_online = True
            current_server = active_sess["server_title"] or f"Sector #{active_sess['server_id']}"
            current_duration_str = format_duration(active_sess["duration_seconds"])
        else:
            is_online = False
            current_server = None
            current_duration_str = None

        # Playtime summary
        await cur.execute(
            """
            SELECT 
                COALESCE(SUM(duration_seconds), 0) as total_playtime_seconds,
                COUNT(DISTINCT server_id) as servers_count,
                COUNT(id) as sessions_count
            FROM sessions
            WHERE ckey = ?;
            """,
            (norm_ckey,),
        )
        summary_row = await cur.fetchone()
        total_playtime_seconds = int(summary_row["total_playtime_seconds"]) if summary_row else 0
        total_hours = round(total_playtime_seconds / 3600.0, 1)
        servers_count = int(summary_row["servers_count"]) if summary_row else 0
        sessions_count = int(summary_row["sessions_count"]) if summary_row else 0

        first_seen_str = format_timestamp(player_row["first_seen"])
        last_seen_str = format_timestamp(player_row["last_seen"])

        # Server breakdown
        await cur.execute(
            """
            SELECT 
                s.server_id,
                COALESCE(srv.title, 'Unknown Sector ' || s.server_id) as title,
                COALESCE(SUM(s.duration_seconds), 0) as total_seconds,
                MAX(s.last_seen_at) as last_visit
            FROM sessions s
            LEFT JOIN servers srv ON s.server_id = srv.id
            WHERE s.ckey = ?
            GROUP BY s.server_id
            ORDER BY total_seconds DESC;
            """,
            (norm_ckey,),
        )
        breakdown_rows = await cur.fetchall()
        server_breakdown: List[Dict[str, Any]] = []
        for b in breakdown_rows:
            sec = int(b["total_seconds"])
            pct = round((sec / total_playtime_seconds * 100.0), 1) if total_playtime_seconds > 0 else 0.0
            server_breakdown.append(
                {
                    "server_id": b["server_id"],
                    "title": b["title"],
                    "seconds": sec,
                    "hours": round(sec / 3600.0, 1),
                    "percentage": pct,
                    "last_visit": format_timestamp(b["last_visit"]),
                }
            )

        # Recent sessions (up to 10 entries)
        await cur.execute(
            """
            SELECT 
                s.id,
                s.server_id,
                COALESCE(srv.title, 'Unknown Sector ' || s.server_id) as server_title,
                s.started_at,
                s.last_seen_at,
                s.ended_at,
                s.duration_seconds
            FROM sessions s
            LEFT JOIN servers srv ON s.server_id = srv.id
            WHERE s.ckey = ?
            ORDER BY s.started_at DESC, s.id DESC
            LIMIT 10;
            """,
            (norm_ckey,),
        )
        sess_rows = await cur.fetchall()
        recent_sessions: List[Dict[str, Any]] = []
        for s in sess_rows:
            dur = int(s["duration_seconds"])
            recent_sessions.append(
                {
                    "server_id": s["server_id"],
                    "server_title": s["server_title"],
                    "started_at": format_timestamp(s["started_at"]),
                    "ended_at": format_timestamp(s["ended_at"]) if s["ended_at"] is not None else None,
                    "duration_seconds": dur,
                    "duration_str": format_duration(dur),
                    "is_active": s["ended_at"] is None,
                }
            )

    dossier: Dict[str, Any] = {
        "ckey": norm_ckey,
        "is_online": is_online,
        "current_server": current_server,
        "current_session_duration": current_duration_str,
        "first_seen": first_seen_str,
        "last_seen": last_seen_str,
        "total_playtime_seconds": total_playtime_seconds,
        "total_hours": total_hours,
        "servers_count": servers_count,
        "sessions_count": sessions_count,
        "status": {
            "online": is_online,
            "is_online": is_online,
            "current_server": current_server,
            "current_session_duration": current_duration_str,
            "last_seen": last_seen_str,
        },
        "summary": {
            "total_playtime_seconds": total_playtime_seconds,
            "total_hours": total_hours,
            "servers_count": servers_count,
            "sessions_count": sessions_count,
            "first_seen": first_seen_str,
            "last_seen": last_seen_str,
        },
        "server_breakdown": server_breakdown,
        "servers": server_breakdown,
        "recent_sessions": recent_sessions,
    }
    return dossier


def _build_chart_24h(points: List[Dict[str, Any]], window_start=None, window_end=None) -> Dict[str, Any]:
    """Builds lightweight SVG polyline coordinates (viewBox 0 0 600 120) and summary metrics."""
    if not points:
        return {
            "points_str": "",
            "area_points_str": "",
            "min_players": 0,
            "max_players": 0,
            "avg_players": 0.0,
            "count": 0,
            "start_label": None,
            "end_label": None,
            "segments": [],
        }

    counts = [int(p["player_count"]) for p in points]
    min_p = min(counts)
    max_p = max(counts)
    avg_p = round(sum(counts) / len(counts), 1)

    x_min, x_max = 10.0, 590.0
    y_top, y_bottom = 16.0, 104.0

    def _calc_y(val: int) -> float:
        if max_p <= 0:
            return y_bottom
        if max_p == min_p:
            return (y_top + y_bottom) / 2.0
        ratio = (val - min_p) / float(max_p - min_p)
        return round(y_bottom - ratio * (y_bottom - y_top), 1)

    coords: List[Tuple[float, float]] = []
    n = len(points)
    start = window_start if window_start is not None else points[0]["timestamp"]
    end = window_end if window_end is not None else points[-1]["timestamp"]
    segments = []
    segment = []
    for i, c in enumerate(counts):
        x_val = round(x_min + ((points[i]["timestamp"] - start) / max(1, end - start)) * (x_max - x_min), 1)
        coord = (x_val, _calc_y(c))
        coords.append(coord)
        if i and points[i].get("gap_before"):
            segments.append(segment)
            segment = []
        segment.append(coord)
    segments.append(segment)

    def segment_data(segment):
        line = " ".join(f"{x},{y}" for x, y in segment)
        return {"points_str": line,
                "area_points_str": f"{segment[0][0]},{y_bottom} {line} {segment[-1][0]},{y_bottom}",
                "x": segment[0][0], "y": segment[0][1], "single": len(segment) == 1}

    points_str = " ".join(f"{x},{y}" for x, y in coords)
    area_points_str = f"{coords[0][0]},{y_bottom} {points_str} {coords[-1][0]},{y_bottom}"

    return {
        "points_str": points_str,
        "area_points_str": area_points_str,
        "min_players": min_p,
        "max_players": max_p,
        "avg_players": avg_p,
        "count": n,
        "start_label": points[0].get("time_str"),
        "end_label": points[-1].get("time_str"),
        "segments": [segment_data(s) for s in segments],
    }


@_read_snapshot
async def get_server_details(
    server_id: int,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Returns detailed server status, current round parameters, active online players,
    top-20 players by playtime on this server, aggregate server statistics, and 24h online history.
    Returns None if server_id does not exist.
    """
    conn = await get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                id, identifier, title, ip, port, last_seen, is_active,
                current_players, map_name, round_duration, gamemode, admins_count, version,
                active_players, station_time, enter_allowed, respawn_allowed, admin_list_json, manifest_json,
                roster_observed_at, telemetry_observed_at, manifest_observed_at
            FROM servers
            WHERE id = ?;
            """,
            (int(server_id),),
        )
        srv_row = await cur.fetchone()
        if srv_row is None:
            return None

        # Active online players on this server
        await cur.execute(
            """
            SELECT
                ckey,
                started_at,
                last_seen_at,
                duration_seconds
            FROM sessions
            WHERE server_id = ? AND ended_at IS NULL
            ORDER BY started_at ASC;
            """,
            (int(server_id),),
        )
        online_rows = await cur.fetchall()
        online_players: List[Dict[str, Any]] = []
        current_wall_ts = time.time()
        for r in online_rows:
            started_ts = float(r["started_at"])
            last_seen_ts = float(r["last_seen_at"])
            stored_dur = int(r["duration_seconds"] or 0)
            dur = stored_dur

            online_players.append(
                {
                    "ckey": r["ckey"],
                    "started_at": format_timestamp(started_ts),
                    "started_at_ts": started_ts,
                    "duration_seconds": dur,
                    "duration_str": format_duration(dur),
                }
            )

        # Top-20 players on this server by total hours/seconds
        await cur.execute(
            """
            SELECT
                ckey,
                total_seconds,
                last_seen
            FROM server_player_totals
            WHERE server_id = ?
            ORDER BY total_seconds DESC, ckey ASC
            LIMIT 20;
            """,
            (int(server_id),),
        )
        top_rows = await cur.fetchall()
        top_players: List[Dict[str, Any]] = []
        for idx, r in enumerate(top_rows):
            tot_sec = int(r["total_seconds"] or 0)
            tot_hours = round(tot_sec / 3600.0, 1)
            top_players.append(
                {
                    "rank": idx + 1,
                    "ckey": r["ckey"],
                    "total_seconds": tot_sec,
                    "total_hours": tot_hours,
                    "hours": tot_hours,
                    "duration_str": format_duration(tot_sec),
                    "last_seen": format_timestamp(r["last_seen"]),
                    "last_seen_ts": float(r["last_seen"]) if r["last_seen"] is not None else None,
                }
            )

        # Aggregate server statistics
        await cur.execute(
            """
            SELECT
                COUNT(*) AS unique_players,
                COALESCE(SUM(total_seconds), 0) AS total_playtime_seconds
            FROM server_player_totals
            WHERE server_id = ?;
            """,
            (int(server_id),),
        )
        agg_row = await cur.fetchone()
        unique_players = int(agg_row["unique_players"] or 0) if agg_row else 0
        total_playtime_seconds = int(agg_row["total_playtime_seconds"] or 0) if agg_row else 0
        total_hours = round(total_playtime_seconds / 3600.0, 1)

        # 24-hour online history points
        ref_snap_ts = float(now_ts) if now_ts is not None else current_wall_ts

        cutoff_24h = ref_snap_ts - 86400.0
        await cur.execute(
            """
            SELECT id, timestamp, player_count, round_duration, map_name
            FROM server_snapshots
            WHERE server_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC;
            """,
            (int(server_id), cutoff_24h, ref_snap_ts),
        )
        snap_rows = await cur.fetchall()

        # Preserve gaps before reducing dense observations into minute maxima.
        snapshots_24h: List[Dict[str, Any]] = []
        previous_ts = None
        for sr in snap_rows:
            ts = float(sr["timestamp"])
            gap = previous_ts is not None and ts - previous_ts > 600
            point = dict(timestamp=ts, time_str=format_timestamp(ts),
                         player_count=int(sr["player_count"]),
                         round_duration=sr["round_duration"], map_name=sr["map_name"],
                         gap_before=gap)
            if snapshots_24h and not gap and int(ts // 60) == int(snapshots_24h[-1]["timestamp"] // 60):
                if point["player_count"] >= snapshots_24h[-1]["player_count"]:
                    point["gap_before"] = snapshots_24h[-1]["gap_before"]
                    snapshots_24h[-1] = point
            else:
                snapshots_24h.append(point)
            previous_ts = ts

    chart_24h = _build_chart_24h(snapshots_24h, cutoff_24h, ref_snap_ts)
    if snap_rows:
        counts = [int(row["player_count"]) for row in snap_rows]
        chart_24h.update(min_players=min(counts), max_players=max(counts),
                         avg_players=round(sum(counts) / len(counts), 1))

    raw_current_players = srv_row["current_players"]
    effective_current_players = raw_current_players
    round_dur_sec = srv_row["round_duration"]
    round_dur_str = format_hms(round_dur_sec) if round_dur_sec is not None else "Unknown"
    last_seen_str = format_timestamp(srv_row["last_seen"])
    is_active = bool(srv_row["is_active"])
    active_players_val = int(srv_row["active_players"]) if srv_row["active_players"] is not None else None
    station_time_val = srv_row["station_time"]
    enter_allowed_val = bool(srv_row["enter_allowed"]) if srv_row["enter_allowed"] is not None else None
    respawn_allowed_val = bool(srv_row["respawn_allowed"]) if srv_row["respawn_allowed"] is not None else None

    admin_list_parsed: List[Dict[str, str]] = []
    if srv_row["admin_list_json"]:
        try:
            loaded_admins = json.loads(srv_row["admin_list_json"])
            if isinstance(loaded_admins, list):
                admin_list_parsed = loaded_admins
        except Exception:
            pass

    manifest_parsed: Dict[str, List[Dict[str, str]]] = {}
    manifest_sections: List[Dict[str, Any]] = []
    manifest_total_crew = 0
    if srv_row["manifest_json"]:
        try:
            loaded_manifest = json.loads(srv_row["manifest_json"])
            if isinstance(loaded_manifest, dict):
                manifest_parsed = loaded_manifest
                for dept_key, crew_list in loaded_manifest.items():
                    if isinstance(crew_list, list) and crew_list:
                        manifest_total_crew += len(crew_list)
                        manifest_sections.append(
                            {
                                "key": dept_key,
                                "label": _DEPT_LABELS.get(dept_key.lower(), dept_key.upper()),
                                "crew": crew_list,
                                "count": len(crew_list),
                            }
                        )
        except Exception:
            pass

    details: Dict[str, Any] = {
        "id": int(srv_row["id"]),
        "server_id": int(srv_row["id"]),
        "identifier": srv_row["identifier"],
        "connect_uri": (f"byond://[{srv_row['ip']}]:{srv_row['port']}" if ":" in srv_row["ip"]
                        else f"byond://{srv_row['ip']}:{srv_row['port']}") if srv_row["port"] else srv_row["identifier"],
        "title": srv_row["title"],
        "ip": srv_row["ip"],
        "port": int(srv_row["port"]),
        "is_active": is_active,
        "last_seen": last_seen_str,
        "last_seen_ts": float(srv_row["last_seen"]),
        "current_players": effective_current_players,
        "active_players": active_players_val,
        "station_time": station_time_val,
        "enter_allowed": enter_allowed_val,
        "respawn_allowed": respawn_allowed_val,
        "admin_list": admin_list_parsed,
        "manifest": manifest_parsed,
        "manifest_sections": manifest_sections,
        "manifest_total_crew": manifest_total_crew,
        "manifest_observed_at": format_timestamp(srv_row["manifest_observed_at"]),
        "telemetry_observed_at": format_timestamp(srv_row["telemetry_observed_at"]),
        "roster_observed_at": format_timestamp(srv_row["roster_observed_at"]),
        "telemetry_stale": srv_row["telemetry_observed_at"] is None or ref_snap_ts - srv_row["telemetry_observed_at"] > 300,
        "topic_available": bool(srv_row["port"]),
        "map_name": srv_row["map_name"],
        "round_duration": round_dur_sec,
        "round_duration_str": round_dur_str,
        "gamemode": srv_row["gamemode"],
        "admins_count": srv_row["admins_count"],
        "version": srv_row["version"],
        "unique_players": unique_players,
        "total_playtime_seconds": total_playtime_seconds,
        "total_hours": total_hours,
        "online_players": online_players,
        "top_players": top_players,
        "snapshots_24h": snapshots_24h,
        "online_history": snapshots_24h,
        "chart_24h": chart_24h,
        "server": {
            "id": int(srv_row["id"]),
            "identifier": srv_row["identifier"],
            "title": srv_row["title"],
            "ip": srv_row["ip"],
            "port": int(srv_row["port"]),
            "is_active": is_active,
            "last_seen": last_seen_str,
            "current_players": effective_current_players,
            "active_players": active_players_val,
            "station_time": station_time_val,
            "enter_allowed": enter_allowed_val,
            "respawn_allowed": respawn_allowed_val,
            "map_name": srv_row["map_name"],
            "round_duration": round_dur_sec,
            "round_duration_str": round_dur_str,
            "gamemode": srv_row["gamemode"],
            "admins_count": srv_row["admins_count"],
            "version": srv_row["version"],
        },
        "round": {
            "current_players": effective_current_players,
            "active_players": active_players_val,
            "station_time": station_time_val,
            "enter_allowed": enter_allowed_val,
            "respawn_allowed": respawn_allowed_val,
            "map_name": srv_row["map_name"],
            "round_duration": round_dur_sec,
            "round_duration_str": round_dur_str,
            "gamemode": srv_row["gamemode"],
            "admins_count": srv_row["admins_count"],
            "version": srv_row["version"],
        },
        "summary": {
            "unique_players": unique_players,
            "total_playtime_seconds": total_playtime_seconds,
            "total_hours": total_hours,
        },
    }
    return details


class Database:
    """Wrapper class providing an object-oriented interface for the SQLite WAL engine."""

    def __init__(self, db_path: str = "tracker.db"):
        self.db_path = db_path

    async def init_db(self) -> None:
        await init_db(self.db_path)

    async def close_db(self) -> None:
        await close_db()

    async def upsert_server(
        self,
        identifier: str,
        title: str,
        ip: str,
        port: int,
        is_active: bool = True,
        now_ts: Optional[float] = None,
        current_players: Optional[int] = None,
        map_name: Optional[str] = None,
        round_duration: Optional[int] = None,
        gamemode: Optional[str] = None,
        admins_count: Optional[int] = None,
        version: Optional[str] = None,
        active_players: Optional[int] = None,
        station_time: Optional[str] = None,
        enter_allowed: Optional[bool] = None,
        respawn_allowed: Optional[bool] = None,
        admin_list: Optional[List[Dict[str, str]]] = None,
        manifest: Optional[Dict[str, List[Dict[str, str]]]] = None,
    ) -> int:
        return await upsert_server(
            identifier,
            title,
            ip,
            port,
            is_active=is_active,
            now_ts=now_ts,
            current_players=current_players,
            map_name=map_name,
            round_duration=round_duration,
            gamemode=gamemode,
            admins_count=admins_count,
            version=version,
            active_players=active_players,
            station_time=station_time,
            enter_allowed=enter_allowed,
            respawn_allowed=respawn_allowed,
            admin_list=admin_list,
            manifest=manifest,
        )

    async def record_server_snapshot(
        self,
        server_id: int,
        player_count: int,
        round_duration: int = 0,
        map_name: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> int:
        return await record_server_snapshot(server_id, player_count, round_duration, map_name, ts)

    async def prune_server_snapshots(
        self, max_age_days: int = 7, now_ts: Optional[float] = None
    ) -> int:
        return await prune_server_snapshots(max_age_days, now_ts)

    async def get_server_details(
        self, server_id: int, now_ts: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        return await get_server_details(server_id, now_ts)

    async def get_active_servers(self) -> List[Dict[str, Any]]:
        return await get_active_servers()

    async def update_player_sessions(
        self,
        server_id: int,
        online_ckeys: Set[str],
        now_ts: Optional[float] = None,
        grace_period_seconds: int = 300,
    ) -> None:
        await update_player_sessions(server_id, online_ckeys, now_ts, grace_period_seconds)

    async def get_player_dossier(self, ckey: str) -> Optional[Dict[str, Any]]:
        return await get_player_dossier(ckey)
