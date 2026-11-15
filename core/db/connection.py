"""SQLite WAL connection lifecycle, transaction decorators, and snapshot isolation."""

from __future__ import annotations

import asyncio
from functools import wraps
import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)


def _write_transaction(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        import core.db as _db

        if _db._operation_connection.get() is not None:
            return await func(*args, **kwargs)
        async with _db._operation_lock:
            conn = await _db.get_connection()
            token = _db._operation_connection.set(conn)
            try:
                await conn.execute("BEGIN IMMEDIATE")
                result = await func(*args, **kwargs)
                await conn.commit()
                return result
            except BaseException:
                await asyncio.shield(conn.rollback())
                raise
            finally:
                _db._operation_connection.reset(token)

    return wrapped


def _read_snapshot(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        import core.db as _db

        if _db._operation_connection.get() is not None:
            return await func(*args, **kwargs)
        if _db._current_db_path == ":memory:":
            async with _db._operation_lock:
                conn = await _db.get_connection()
                token = _db._operation_connection.set(conn)
                try:
                    return await func(*args, **kwargs)
                finally:
                    _db._operation_connection.reset(token)
        async with _db._read_slots:
            # Reads have their own WAL snapshot and never observe uncommitted writes.
            await _db.get_connection()
            async with aiosqlite.connect(_db._current_db_path) as conn:
                conn.row_factory = aiosqlite.Row
                await conn.execute("PRAGMA query_only=ON")
                await conn.execute("BEGIN")
                token = _db._operation_connection.set(conn)
                try:
                    return await func(*args, **kwargs)
                finally:
                    _db._operation_connection.reset(token)

    return wrapped


async def _open_database(path: str) -> aiosqlite.Connection:
    import core.db as _db

    conn = aiosqlite.connect(path)
    try:
        await conn
        conn.row_factory = aiosqlite.Row
        await conn.executescript(_db.SCHEMA_SQL)
        await _db._migrate_schema(conn)
        # Repair duplicate open records without removing any historical row.
        await conn.execute(
            """
            UPDATE sessions SET ended_at=last_seen_at
            WHERE ended_at IS NULL AND id NOT IN (
                SELECT MAX(id) FROM sessions WHERE ended_at IS NULL GROUP BY server_id,ckey
            )
            """
        )
        await conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_one_open
                            ON sessions(server_id,ckey) WHERE ended_at IS NULL"""
        )
        await _db._initialize_aggregates(conn)
        await conn.commit()
        return conn
    except BaseException:
        await asyncio.shield(conn.close())
        raise


async def init_db(db_path: str = "tracker.db") -> None:
    """
    Initializes SQLite database at db_path with WAL mode and schema definitions.
    Safely closes any existing connection and opens a new connection with foreign keys
    and busy_timeout enabled.
    """
    import core.db as _db

    # Reinitialization after an event loop has shut down must recreate its gates.
    loop = asyncio.get_running_loop()
    old_loop = getattr(_db._operation_lock, "_loop", None)
    if old_loop is not None and old_loop is not loop and old_loop.is_closed():
        _db._operation_lock = asyncio.Lock()
    old_loop = getattr(_db._read_slots, "_loop", None)
    if old_loop is not None and old_loop is not loop and old_loop.is_closed():
        _db._read_slots = asyncio.Semaphore(8)

    async with _db._operation_lock, _db._db_lock:
        if _db._connection is not None:
            try:
                await _db._connection.close()
            except Exception:
                pass
            _db._connection = None

        _db._current_db_path = str(db_path)
        path_obj = Path(_db._current_db_path)
        if path_obj.parent and not path_obj.parent.exists():
            path_obj.parent.mkdir(parents=True, exist_ok=True)

        _db._connection = await _db._open_database(_db._current_db_path)
        logger.info("Initialized database at '%s' with WAL mode.", _db._current_db_path)


async def close_db() -> None:
    """Cleanly closes open database connection."""
    import core.db as _db

    async with _db._operation_lock, _db._db_lock:
        if _db._connection is not None:
            try:
                await _db._connection.close()
            except Exception as exc:
                logger.debug("Error while closing database: %s", exc)
            _db._connection = None


async def get_connection() -> aiosqlite.Connection:
    """Retrieves or establishes the active aiosqlite connection."""
    import core.db as _db

    if _db._operation_connection.get() is not None:
        return _db._operation_connection.get()
    if _db._connection is None:
        async with _db._db_lock:
            if _db._connection is None:
                path_obj = Path(_db._current_db_path)
                if path_obj.parent and not path_obj.parent.exists():
                    path_obj.parent.mkdir(parents=True, exist_ok=True)
                _db._connection = await _db._open_database(_db._current_db_path)
    return _db._connection
