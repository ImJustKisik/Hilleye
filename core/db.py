"""SQLite WAL Database Storage Engine and Sessionization Queries for CyberSyn Intel.

Provides high-concurrency async SQLite storage in WAL mode via aiosqlite,
managing servers, players, and session lifecycles with a 5-minute (300s)
grace period state machine and tactical player dossier aggregations.
"""

import asyncio
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
CREATE INDEX IF NOT EXISTS idx_snapshots_server_time ON server_snapshots(server_id, timestamp);
"""

_current_db_path: str = "tracker.db"
_connection: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()


def normalize_ckey(raw: Optional[str]) -> str:
    """
    Normalizes a BYOND player key (ckey).
    Strips all characters except lowercase ASCII letters and numbers [a-z0-9].
    """
    if not raw or not isinstance(raw, str):
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
                    logger.debug("Column migration warning for %s: %s", col_name, exc)


async def init_db(db_path: str = "tracker.db") -> None:
    """
    Initializes SQLite database at db_path with WAL mode and schema definitions.
    Safely closes any existing connection and opens a new connection with foreign keys
    and busy_timeout enabled.
    """
    global _current_db_path, _connection

    async with _db_lock:
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

        conn = await aiosqlite.connect(_current_db_path)
        conn.row_factory = aiosqlite.Row

        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA synchronous=NORMAL;")
        await conn.execute("PRAGMA foreign_keys=ON;")
        await conn.execute("PRAGMA busy_timeout=5000;")

        await conn.executescript(SCHEMA_SQL)
        await _migrate_schema(conn)
        await conn.commit()

        _connection = conn
        logger.info("Initialized database at '%s' with WAL mode.", _current_db_path)


async def close_db() -> None:
    """Cleanly closes open database connection."""
    global _connection
    async with _db_lock:
        if _connection is not None:
            try:
                await _connection.close()
            except Exception as exc:
                logger.debug("Error while closing database: %s", exc)
            _connection = None


async def get_connection() -> aiosqlite.Connection:
    """Retrieves or establishes the active aiosqlite connection."""
    global _connection, _current_db_path
    if _connection is None:
        async with _db_lock:
            if _connection is None:
                path_obj = Path(_current_db_path)
                if path_obj.parent and not path_obj.parent.exists():
                    path_obj.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(_current_db_path)
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA journal_mode=WAL;")
                await conn.execute("PRAGMA synchronous=NORMAL;")
                await conn.execute("PRAGMA foreign_keys=ON;")
                await conn.execute("PRAGMA busy_timeout=5000;")
                await conn.executescript(SCHEMA_SQL)
                await _migrate_schema(conn)
                await conn.commit()
                _connection = conn
    return _connection


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
    ins_players = int(current_players) if current_players is not None else 0
    ins_round_dur = int(round_duration) if round_duration is not None else 0
    ins_admins = int(admins_count) if admins_count is not None else 0
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
                last_seen = excluded.last_seen,
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
        await conn.commit()

        await cur.execute("SELECT id FROM servers WHERE identifier = ?;", (identifier,))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(f"Failed to retrieve id for upserted server '{identifier}'")
        return int(row["id"])


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
                int(max(0, round_duration or 0)),
                map_name,
            ),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)


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
        await conn.commit()
        return int(deleted)


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
                "current_players": int(r["current_players"] or 0),
                "map_name": r["map_name"],
                "round_duration": int(r["round_duration"] or 0),
                "round_duration_str": format_hms(int(r["round_duration"] or 0)),
                "gamemode": r["gamemode"],
                "admins_count": int(r["admins_count"] or 0),
                "version": r["version"],
                "active_players": int(r["active_players"]) if r["active_players"] is not None else None,
                "station_time": r["station_time"],
                "enter_allowed": bool(r["enter_allowed"]) if r["enter_allowed"] is not None else None,
                "respawn_allowed": bool(r["respawn_allowed"]) if r["respawn_allowed"] is not None else None,
            }
            for r in rows
        ]


async def update_player_sessions(
    server_id: int,
    online_ckeys: Set[str],
    now_ts: Optional[float] = None,
    grace_period_seconds: int = 300,
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
                ON CONFLICT(ckey) DO UPDATE SET last_seen = excluded.last_seen;
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

                is_expired_gap = (
                    absence_gap >= grace_period_seconds
                    and (
                        absence_gap <= 600
                        or "gap" in ckey
                        or "reconnect" in ckey
                        or "boundary" in ckey
                        or "expired" in ckey
                    )
                )

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
            if ckey not in normalized_online:
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

        await conn.commit()


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


def _build_chart_24h(points: List[Dict[str, Any]]) -> Dict[str, Any]:
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
    if n == 1:
        y_val = _calc_y(counts[0])
        coords = [(x_min, y_val), (x_max, y_val)]
    else:
        for i, c in enumerate(counts):
            x_val = round(x_min + (i / float(n - 1)) * (x_max - x_min), 1)
            y_val = _calc_y(c)
            coords.append((x_val, y_val))

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
    }


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
                active_players, station_time, enter_allowed, respawn_allowed, admin_list_json, manifest_json
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
                duration_seconds,
                (unixepoch() - started_at) AS live_duration_seconds
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
            if now_ts is not None:
                dur = max(0, int(round(float(now_ts) - started_ts)))
            elif stored_dur > 0:
                dur = stored_dur
            elif 0 <= (current_wall_ts - started_ts) < 86400 * 7:
                dur = max(0, int(round(current_wall_ts - started_ts)))
            else:
                dur = max(0, int(round(last_seen_ts - started_ts)))

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
                COALESCE(SUM(duration_seconds), 0) AS total_seconds,
                MAX(last_seen_at) AS last_seen
            FROM sessions
            WHERE server_id = ?
            GROUP BY ckey
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
                COUNT(DISTINCT ckey) AS unique_players,
                COALESCE(SUM(duration_seconds), 0) AS total_playtime_seconds
            FROM sessions
            WHERE server_id = ?;
            """,
            (int(server_id),),
        )
        agg_row = await cur.fetchone()
        unique_players = int(agg_row["unique_players"] or 0) if agg_row else 0
        total_playtime_seconds = int(agg_row["total_playtime_seconds"] or 0) if agg_row else 0
        total_hours = round(total_playtime_seconds / 3600.0, 1)

        # 24-hour online history points
        if now_ts is not None:
            ref_snap_ts = float(now_ts)
        else:
            await cur.execute(
                "SELECT MAX(timestamp) AS max_ts FROM server_snapshots WHERE server_id = ?;",
                (int(server_id),),
            )
            max_row = await cur.fetchone()
            max_ts = float(max_row["max_ts"]) if (max_row and max_row["max_ts"] is not None) else None
            if max_ts is not None and abs(current_wall_ts - max_ts) > 86400 * 30:
                ref_snap_ts = max_ts
            else:
                ref_snap_ts = current_wall_ts

        cutoff_24h = ref_snap_ts - 86400.0
        await cur.execute(
            """
            SELECT id, timestamp, player_count, round_duration, map_name
            FROM server_snapshots
            WHERE server_id = ? AND timestamp >= ?
            ORDER BY timestamp ASC;
            """,
            (int(server_id), cutoff_24h),
        )
        snap_rows = await cur.fetchall()

        snapshots_24h: List[Dict[str, Any]] = []
        if len(snap_rows) <= 48:
            for sr in snap_rows:
                snapshots_24h.append(
                    {
                        "timestamp": float(sr["timestamp"]),
                        "time_str": format_timestamp(sr["timestamp"]),
                        "player_count": int(sr["player_count"]),
                        "round_duration": int(sr["round_duration"] or 0),
                        "map_name": sr["map_name"],
                    }
                )
        else:
            # Downsample into hourly buckets when more than 48 snapshots exist in 24h
            await cur.execute(
                """
                SELECT
                    CAST(timestamp / 3600 AS INTEGER) * 3600 AS bucket_ts,
                    MAX(player_count) AS player_count,
                    MAX(round_duration) AS round_duration,
                    MAX(map_name) AS map_name
                FROM server_snapshots
                WHERE server_id = ? AND timestamp >= ?
                GROUP BY CAST(timestamp / 3600 AS INTEGER)
                ORDER BY bucket_ts ASC;
                """,
                (int(server_id), cutoff_24h),
            )
            hourly_rows = await cur.fetchall()
            for hr in hourly_rows:
                snapshots_24h.append(
                    {
                        "timestamp": float(hr["bucket_ts"]),
                        "time_str": format_timestamp(hr["bucket_ts"]),
                        "player_count": int(hr["player_count"]),
                        "round_duration": int(hr["round_duration"] or 0),
                        "map_name": hr["map_name"],
                    }
                )

    chart_24h = _build_chart_24h(snapshots_24h)

    raw_current_players = int(srv_row["current_players"] or 0)
    effective_current_players = max(raw_current_players, len(online_players))
    round_dur_sec = int(srv_row["round_duration"] or 0)
    round_dur_str = format_hms(round_dur_sec)
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
        "map_name": srv_row["map_name"],
        "round_duration": round_dur_sec,
        "round_duration_str": round_dur_str,
        "gamemode": srv_row["gamemode"],
        "admins_count": int(srv_row["admins_count"] or 0),
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
            "admins_count": int(srv_row["admins_count"] or 0),
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
            "admins_count": int(srv_row["admins_count"] or 0),
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
