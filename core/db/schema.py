"""Database schema definitions, migrations, department labels, and formatting helpers."""

from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Dict, List, Optional, Tuple, Union

import aiosqlite

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


async def _migrate_schema(conn: aiosqlite.Connection) -> None:
    """Safely migrates existing database tables by adding missing columns via PRAGMA table_info."""
    import core.db as _db

    async with conn.cursor() as cur:
        await cur.execute("PRAGMA table_info(servers);")
        rows = await cur.fetchall()
        existing_cols = {r[1] if isinstance(r, tuple) else r["name"] for r in rows}

        for col_name, col_def in _db._SERVER_MIGRATION_COLUMNS:
            if col_name not in existing_cols:
                try:
                    await cur.execute(f"ALTER TABLE servers ADD COLUMN {col_name} {col_def};")
                except Exception as exc:
                    await cur.execute("PRAGMA table_info(servers)")
                    if col_name not in {row[1] for row in await cur.fetchall()}:
                        raise RuntimeError(f"Cannot migrate servers.{col_name}") from exc


async def _initialize_aggregates(conn: aiosqlite.Connection) -> None:
    row = await (await conn.execute("SELECT version FROM schema_migrations WHERE version=1")).fetchone()
    if row is None:
        await conn.execute("DELETE FROM server_player_totals")
        await conn.execute(
            """INSERT INTO server_player_totals
            SELECT server_id,ckey,COALESCE(SUM(duration_seconds),0),COUNT(*),MAX(last_seen_at)
            FROM sessions GROUP BY server_id,ckey"""
        )
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
    for name, event, body in (
        ("insert", "INSERT", insert),
        ("update", "UPDATE", remove + insert),
        ("delete", "DELETE", remove),
    ):
        await conn.execute(f"CREATE TRIGGER IF NOT EXISTS totals_{name} AFTER {event} ON sessions BEGIN {body} END")
