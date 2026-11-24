"""SQLite WAL connection lifecycle and FTS5 schema for the SS13 Log Viewer."""

from __future__ import annotations

import asyncio
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from typing import Set

import aiosqlite

ALLOWED_LOG_FILES: Set[str] = {"game.log", "qdel.log", "initialize.log"}
_SAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_DATE_PATH_RE = re.compile(r"(20\d{2})[/\\](0[1-9]|1[0-2])[/\\](0[1-9]|[12]\d|3[01])")
_SCHEMA_VERSION = 2

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

CREATE TABLE IF NOT EXISTS log_index_checkpoints (
    round_uid TEXT NOT NULL REFERENCES log_rounds(round_uid) ON DELETE CASCADE,
    generation INTEGER NOT NULL,
    checkpoint_json TEXT NOT NULL,
    PRIMARY KEY(round_uid, generation)
);
CREATE TABLE IF NOT EXISTS log_sources (
    source TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    last_scan_at REAL,
    last_success_at REAL,
    error TEXT,
    pending_bytes INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS log_jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    created_by TEXT,
    state TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    heartbeat REAL,
    progress TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_log_jobs_queue ON log_jobs(kind,state,created_at);
CREATE TABLE IF NOT EXISTS log_job_users (
    job_id TEXT NOT NULL REFERENCES log_jobs(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    PRIMARY KEY(job_id,user_id)
);
CREATE TABLE IF NOT EXISTS log_maintenance_state (
    name TEXT PRIMARY KEY,
    value_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_upload_batches (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    entries_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_upload_receipts (
    fingerprint TEXT PRIMARY KEY,
    round_uid TEXT NOT NULL REFERENCES log_rounds(round_uid) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_log_upload_receipts_round ON log_upload_receipts(round_uid);
CREATE TABLE IF NOT EXISTS log_upload_deletions (
    round_uid TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL,
    deleted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS log_job_rounds (
    job_id TEXT NOT NULL REFERENCES log_jobs(id) ON DELETE CASCADE,
    round_uid TEXT NOT NULL,
    generation INTEGER NOT NULL,
    indexed_lines INTEGER NOT NULL,
    PRIMARY KEY(job_id,round_uid)
);
"""


def _configure_connection(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA cache_size=-65536")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")


def _initialize_schema(conn, schema):
    _configure_connection(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute("PRAGMA user_version").fetchone()[0] > _SCHEMA_VERSION:
            raise RuntimeError("Logs database schema was created by a newer Hilleye version")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        migrated_coords = False
        if "log_entries" in tables:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(log_entries)")}
            for name in ("coord_x", "coord_y", "coord_z"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE log_entries ADD COLUMN {name} INTEGER")
                    migrated_coords = True
        if migrated_coords and "log_rounds" in tables:
            conn.execute("UPDATE log_rounds SET indexed_at=NULL")
        statement = ""
        for line in schema.splitlines():
            if line.lstrip().startswith("PRAGMA "):
                continue
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ""
        for table, additions in (
            ("log_rounds", {"active_generation": "INTEGER NOT NULL DEFAULT 0", "missing_since": "REAL",
                            "import_batch": "TEXT", "initialize_log_size": "INTEGER NOT NULL DEFAULT 0",
                            "map_revision": "TEXT NOT NULL DEFAULT ''"}),
            ("log_entries", {"generation": "INTEGER NOT NULL DEFAULT 0", "byte_offset": "INTEGER", "byte_length": "INTEGER", "is_continuation": "INTEGER NOT NULL DEFAULT 0"}),
        ):
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, declaration in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_log_generation_line ON log_entries(round_uid,generation,line_no)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_log_rounds_import_batch ON log_rounds(import_batch) WHERE import_batch IS NOT NULL")
        conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


async def init_logs_db(db_path: str = "logs.db") -> None:
    """Initialize or additively migrate the log schema in one transaction."""
    import core.logs_db as _ldb
    loop = asyncio.get_running_loop()
    for name in ("_db_lock", "_sync_lock"):
        old_loop = getattr(getattr(_ldb, name), "_loop", None)
        if old_loop is not None and old_loop is not loop and old_loop.is_closed():
            setattr(_ldb, name, asyncio.Lock())
    async with _ldb._db_lock:
        if _ldb._connection is not None:
            await _ldb._connection.close()
            _ldb._connection = None
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(db_path, timeout=10)
        conn.row_factory = aiosqlite.Row
        try:
            await conn._execute(_initialize_schema, conn._conn, _ldb.LOGS_SCHEMA_SQL)
        except BaseException:
            await asyncio.shield(conn.close())
            raise
        _ldb._connection = conn
        _ldb._current_logs_db_path = str(db_path)


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


async def run_logs_transaction(operation, *args, **kwargs):
    """Run a synchronous storage operation without releasing its caller's locks on cancellation."""
    import core.logs_db as _ldb
    from core.log_operations import finish_awaitable, finish_thread

    path = _ldb._current_logs_db_path
    if path == ":memory:":
        conn = await get_logs_connection()
        return await finish_awaitable(conn._execute(operation, conn._conn, *args, **kwargs))

    def invoke():
        with closing(sqlite3.connect(path, timeout=15)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            return operation(conn, *args, **kwargs)

    return await finish_thread(invoke)
