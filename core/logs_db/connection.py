"""SQLite WAL connection lifecycle and FTS5 schema for the SS13 Log Viewer."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Set

import aiosqlite

ALLOWED_LOG_FILES: Set[str] = {"game.log", "qdel.log", "initialize.log"}
_SAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_DATE_PATH_RE = re.compile(r"(20\d{2})[/\\](0[1-9]|1[0-2])[/\\](0[1-9]|[12]\d|3[01])")

LOGS_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA cache_size = -65536;
PRAGMA temp_store = MEMORY;
PRAGMA mmap_size = 268435456;

CREATE TABLE IF NOT EXISTS log_whitelist (
    discord_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'viewer',
    added_by TEXT NOT NULL DEFAULT 'system',
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS log_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    discord_id TEXT,
    discord_username TEXT,
    action TEXT NOT NULL,
    target TEXT,
    ip_address TEXT,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON log_audit_events(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON log_audit_events(action, created_at DESC);

CREATE TABLE IF NOT EXISTS log_rounds (
    round_uid TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,            -- 'disk' | 'upload'
    round_id TEXT NOT NULL,
    round_date TEXT NOT NULL,
    dir_path TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    uploaded_by TEXT,
    uploaded_at REAL,
    has_game_log INTEGER NOT NULL DEFAULT 0,
    has_qdel_log INTEGER NOT NULL DEFAULT 0,
    has_initialize_log INTEGER NOT NULL DEFAULT 0,
    game_log_size INTEGER NOT NULL DEFAULT 0,
    game_log_mtime REAL NOT NULL DEFAULT 0,
    qdel_log_size INTEGER NOT NULL DEFAULT 0,
    qdel_log_mtime REAL NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0,
    gameplay_line_count INTEGER NOT NULL DEFAULT 0,
    category_counts_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT,
    ended_at TEXT,
    indexed_mtime REAL,
    indexed_size INTEGER,
    indexed_at REAL,
    includes_debug INTEGER NOT NULL DEFAULT 0,
    index_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_rounds_date ON log_rounds(round_date DESC, started_at DESC, round_uid);
CREATE INDEX IF NOT EXISTS idx_rounds_source ON log_rounds(source_type, round_date DESC);

CREATE TABLE IF NOT EXISTS log_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_uid TEXT NOT NULL REFERENCES log_rounds(round_uid) ON DELETE CASCADE,
    file_name TEXT NOT NULL DEFAULT 'game.log',
    line_no INTEGER NOT NULL,
    timestamp TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'SYSTEM',
    ckeys_space TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL,
    coord_x INTEGER,
    coord_y INTEGER,
    coord_z INTEGER
);
CREATE INDEX IF NOT EXISTS idx_log_entries_round_line ON log_entries(round_uid, file_name, line_no);
CREATE INDEX IF NOT EXISTS idx_log_entries_round_cat ON log_entries(round_uid, category);
CREATE INDEX IF NOT EXISTS idx_log_entries_coords ON log_entries(round_uid, coord_z, coord_x, coord_y);

CREATE VIRTUAL TABLE IF NOT EXISTS log_entries_fts USING fts5(
    message,
    category,
    ckeys_space,
    content='log_entries',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS log_entries_ai AFTER INSERT ON log_entries BEGIN
    INSERT INTO log_entries_fts(rowid, message, category, ckeys_space)
    VALUES (new.id, new.message, new.category, new.ckeys_space);
END;

CREATE TRIGGER IF NOT EXISTS log_entries_ad AFTER DELETE ON log_entries BEGIN
    INSERT INTO log_entries_fts(log_entries_fts, rowid, message, category, ckeys_space)
    VALUES ('delete', old.id, old.message, old.category, old.ckeys_space);
END;

CREATE TRIGGER IF NOT EXISTS log_entries_au AFTER UPDATE ON log_entries BEGIN
    INSERT INTO log_entries_fts(log_entries_fts, rowid, message, category, ckeys_space)
    VALUES ('delete', old.id, old.message, old.category, old.ckeys_space);
    INSERT INTO log_entries_fts(rowid, message, category, ckeys_space)
    VALUES (new.id, new.message, new.category, new.ckeys_space);
END;
"""


async def init_logs_db(db_path: str = "logs.db") -> None:
    """Initialize SQLite WAL database for the log viewer, whitelist, audit log, and FTS5 index."""
    import core.logs_db as _ldb

    async with _ldb._db_lock:
        if _ldb._connection is not None:
            await _ldb._connection.close()
            _ldb._connection = None

        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        conn = await aiosqlite.connect(db_path, timeout=10.0)
        conn.row_factory = aiosqlite.Row
        # Safely migrate pre-coordinate log_entries schemas if upgrading an existing logs.db
        existing_tables = {
            r[0]
            for r in await (
                await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ).fetchall()
        }
        if "log_entries" in existing_tables:
            cols = {
                r[1]
                for r in await (await conn.execute("PRAGMA table_info(log_entries)")).fetchall()
            }
            migrated_coords = False
            for col_name in ("coord_x", "coord_y", "coord_z"):
                if col_name not in cols:
                    await conn.execute(f"ALTER TABLE log_entries ADD COLUMN {col_name} INTEGER")
                    migrated_coords = True
            if migrated_coords and "log_rounds" in existing_tables:
                await conn.execute("UPDATE log_rounds SET indexed_at = NULL")
        await conn.executescript(_ldb.LOGS_SCHEMA_SQL)
        await conn.commit()
        _ldb._connection = conn
        _ldb._current_logs_db_path = db_path


async def close_logs_db() -> None:
    """Safely close the async SQLite connection for logs."""
    import core.logs_db as _ldb

    async with _ldb._db_lock:
        if _ldb._connection is not None:
            await _ldb._connection.close()
            _ldb._connection = None


async def get_logs_connection() -> aiosqlite.Connection:
    import core.logs_db as _ldb

    if _ldb._connection is None:
        raise RuntimeError("Logs database is not initialized")
    return _ldb._connection
