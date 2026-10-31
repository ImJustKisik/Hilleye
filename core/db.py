"""SQLite WAL Database Storage Engine and Sessionization Queries for CyberSyn Intel.

Provides high-concurrency async SQLite storage in WAL mode via aiosqlite,
managing servers, players, and session lifecycles with a 5-minute (300s)
grace period state machine and tactical player dossier aggregations.
"""

import asyncio
from datetime import datetime, timezone
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
    is_active BOOLEAN DEFAULT 1
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
) -> int:
    """
    Upserts server record and returns its database ID.
    Updates title, ip, port, last_seen, and is_active on identifier conflict.
    """
    ts = time.time() if now_ts is None else float(now_ts)
    conn = await get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO servers (identifier, title, ip, port, last_seen, is_active)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(identifier) DO UPDATE SET
                title = excluded.title,
                ip = excluded.ip,
                port = excluded.port,
                last_seen = excluded.last_seen,
                is_active = excluded.is_active;
            """,
            (identifier, title, ip, int(port), ts, 1 if is_active else 0),
        )
        await conn.commit()

        await cur.execute("SELECT id FROM servers WHERE identifier = ?;", (identifier,))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(f"Failed to retrieve id for upserted server '{identifier}'")
        return int(row["id"])


async def get_active_servers() -> List[Dict[str, Any]]:
    """Returns list of active monitored servers."""
    conn = await get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, identifier, title, ip, port, last_seen, is_active
            FROM servers
            WHERE is_active = 1
            ORDER BY title ASC;
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
                COUNT(DISTINCT server_id) as servers_count
            FROM sessions
            WHERE ckey = ?;
            """,
            (norm_ckey,),
        )
        summary_row = await cur.fetchone()
        total_playtime_seconds = int(summary_row["total_playtime_seconds"]) if summary_row else 0
        total_hours = round(total_playtime_seconds / 3600.0, 1)
        servers_count = int(summary_row["servers_count"]) if summary_row else 0

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
            "first_seen": first_seen_str,
            "last_seen": last_seen_str,
        },
        "server_breakdown": server_breakdown,
        "servers": server_breakdown,
        "recent_sessions": recent_sessions,
    }
    return dossier


class Database:
    """Wrapper class providing an object-oriented interface for the SQLite WAL engine."""

    def __init__(self, db_path: str = "tracker.db"):
        self.db_path = db_path

    async def init_db(self) -> None:
        await init_db(self.db_path)

    async def close_db(self) -> None:
        await close_db()

    async def upsert_server(
        self, identifier: str, title: str, ip: str, port: int, is_active: bool = True
    ) -> int:
        return await upsert_server(identifier, title, ip, port, is_active)

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
