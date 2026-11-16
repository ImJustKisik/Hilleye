"""SQLite WAL connection lifecycle, transaction decorators, and snapshot isolation."""

from __future__ import annotations

import asyncio
from functools import wraps
import logging
from pathlib import Path
from typing import Tuple

import aiosqlite

logger = logging.getLogger(__name__)

READ_POOL_MAX_SIZE = 8


def _recreate_loop_primitives(_db) -> None:
    """Recreates loop-bound synchronization primitives when called from a new event loop."""
    loop = asyncio.get_running_loop()
    for attr, factory in (
        ("_operation_lock", asyncio.Lock),
        ("_db_lock", asyncio.Lock),
        ("_read_pool_lock", asyncio.Lock),
        ("_read_slots", lambda: asyncio.Semaphore(READ_POOL_MAX_SIZE)),
    ):
        obj = getattr(_db, attr, None)
        old_loop = getattr(obj, "_loop", None)
        if old_loop is not None and old_loop is not loop and old_loop.is_closed():
            setattr(_db, attr, factory())


async def _open_read_connection(path: str) -> aiosqlite.Connection:
    """Opens a tuned read-only SQLite connection for the reader pool."""
    conn = aiosqlite.connect(path)
    try:
        await conn
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA query_only=ON")
        await conn.execute("PRAGMA cache_size=-16384")
        await conn.execute("PRAGMA temp_store=MEMORY")
        await conn.execute("PRAGMA mmap_size=268435456")
        return conn
    except BaseException:
        await asyncio.shield(conn.close())
        raise


async def _acquire_read_connection(_db) -> Tuple[aiosqlite.Connection, int, str]:
    """Checks out an idle read connection or opens a new one for the current DB generation."""
    for _ in range(4):
        async with _db._read_pool_lock:
            gen = _db._read_pool_generation
            db_path = _db._current_db_path
            if _db._read_pool:
                conn = _db._read_pool.pop()
                _db._active_read_conns.add(conn)
                return conn, gen, db_path

        conn = await _open_read_connection(db_path)
        async with _db._read_pool_lock:
            if gen == _db._read_pool_generation and db_path == _db._current_db_path:
                _db._active_read_conns.add(conn)
                return conn, gen, db_path

        await asyncio.shield(conn.close())

    # Fallback if init_db/close_db kept racing across retries.
    async with _db._read_pool_lock:
        gen = _db._read_pool_generation
        db_path = _db._current_db_path
    conn = await _open_read_connection(db_path)
    async with _db._read_pool_lock:
        _db._active_read_conns.add(conn)
    return conn, gen, db_path


async def _release_read_connection(
    _db,
    conn: aiosqlite.Connection,
    gen: int,
    db_path: str,
    *,
    clean: bool,
) -> None:
    """Ends the WAL read snapshot and returns clean connections to the pool, closing stale ones."""
    if clean:
        try:
            await conn.rollback()
        except BaseException:
            clean = False

    keep = False
    async with _db._read_pool_lock:
        _db._active_read_conns.discard(conn)
        if (
            clean
            and gen == _db._read_pool_generation
            and db_path == _db._current_db_path
            and len(_db._read_pool) < READ_POOL_MAX_SIZE
        ):
            _db._read_pool.append(conn)
            keep = True

    if not keep:
        try:
            await conn.close()
        except Exception as exc:
            logger.debug("Error while closing pooled read connection: %s", exc)


async def _close_read_pool(_db) -> None:
    """Invalidates the reader pool generation and closes all pooled and active read handles."""
    async with _db._read_pool_lock:
        _db._read_pool_generation += 1
        idle = list(_db._read_pool)
        _db._read_pool.clear()
        active = list(_db._active_read_conns)
        _db._active_read_conns.clear()

    for conn in idle:
        try:
            await conn.close()
        except Exception as exc:
            logger.debug("Error while closing pooled read connection: %s", exc)

    for conn in active:
        try:
            await conn.close()
        except Exception as exc:
            logger.debug("Error while closing active read connection: %s", exc)


def _write_transaction(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        import core.db as _db

        if _db._operation_connection.get() is not None:
            return await func(*args, **kwargs)
        _recreate_loop_primitives(_db)
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
        _recreate_loop_primitives(_db)
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
            conn, gen, db_path = await _acquire_read_connection(_db)
            clean = False
            token = None
            try:
                await conn.execute("BEGIN")
                token = _db._operation_connection.set(conn)
                result = await func(*args, **kwargs)
                clean = True
                return result
            finally:
                if token is not None:
                    _db._operation_connection.reset(token)
                await asyncio.shield(_release_read_connection(_db, conn, gen, db_path, clean=clean))

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
        await _db._consolidate_duplicate_servers(conn)
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
    _recreate_loop_primitives(_db)

    async with _db._operation_lock, _db._db_lock:
        await _close_read_pool(_db)
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
    """Cleanly closes open database connection and any pooled read handles."""
    import core.db as _db

    _recreate_loop_primitives(_db)
    async with _db._operation_lock, _db._db_lock:
        await _close_read_pool(_db)
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
    _recreate_loop_primitives(_db)
    if _db._connection is None:
        async with _db._db_lock:
            if _db._connection is None:
                path_obj = Path(_db._current_db_path)
                if path_obj.parent and not path_obj.parent.exists():
                    path_obj.parent.mkdir(parents=True, exist_ok=True)
                _db._connection = await _db._open_database(_db._current_db_path)
    return _db._connection
