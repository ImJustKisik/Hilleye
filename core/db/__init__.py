"""SQLite WAL Database Storage Engine and Sessionization Queries for CyberSyn Intel.

Provides high-concurrency async SQLite storage in WAL mode via aiosqlite,
managing servers, players, and session lifecycles with a 5-minute (300s)
grace period state machine and tactical player dossier aggregations.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
import logging
from typing import List, Optional, Set

import aiosqlite

from core.db.adapter import Database
from core.db.connection import (
    _open_database,
    _read_snapshot,
    _write_transaction,
    close_db,
    get_connection,
    init_db,
)
from core.db.dossiers import (
    _build_chart_24h,
    get_network_overview,
    get_player_dossier,
    get_server_details,
    search_players,
)
from core.db.schema import (
    SCHEMA_SQL,
    _DEPT_LABELS,
    _SERVER_MIGRATION_COLUMNS,
    _initialize_aggregates,
    _migrate_schema,
    format_duration,
    format_hms,
    format_timestamp,
    normalize_ckey,
)
from core.db.servers import (
    expire_stale_servers,
    get_active_servers,
    get_collector_state,
    get_preferred_topic_queries,
    prune_server_snapshots,
    record_collector_cycle,
    record_server_snapshot,
    save_manifest,
    save_observation,
    set_preferred_topic_query,
    upsert_server,
)
from core.db.sessions import update_player_sessions

logger = logging.getLogger(__name__)

_current_db_path: str = "tracker.db"
_connection: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()
_operation_lock = asyncio.Lock()
_read_slots = asyncio.Semaphore(8)
_read_pool: List[aiosqlite.Connection] = []
_read_pool_lock = asyncio.Lock()
_read_pool_generation: int = 0
_active_read_conns: Set[aiosqlite.Connection] = set()
_operation_connection: ContextVar[Optional[aiosqlite.Connection]] = ContextVar("db_operation", default=None)

__all__ = [
    "SCHEMA_SQL",
    "_DEPT_LABELS",
    "_SERVER_MIGRATION_COLUMNS",
    "_current_db_path",
    "_connection",
    "_db_lock",
    "_operation_lock",
    "_read_slots",
    "_operation_connection",
    "_write_transaction",
    "_read_snapshot",
    "normalize_ckey",
    "format_timestamp",
    "format_duration",
    "format_hms",
    "_migrate_schema",
    "_open_database",
    "_initialize_aggregates",
    "init_db",
    "close_db",
    "get_connection",
    "upsert_server",
    "save_observation",
    "expire_stale_servers",
    "record_collector_cycle",
    "save_manifest",
    "get_collector_state",
    "record_server_snapshot",
    "prune_server_snapshots",
    "get_active_servers",
    "get_preferred_topic_queries",
    "set_preferred_topic_query",
    "update_player_sessions",
    "get_player_dossier",
    "search_players",
    "_build_chart_24h",
    "get_network_overview",
    "get_server_details",
    "Database",
]
