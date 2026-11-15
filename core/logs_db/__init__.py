"""SQLite WAL storage, FTS5 global log index, Discord whitelist, audit log, and upload manager."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiosqlite

from core.logs_db.connection import (
    ALLOWED_LOG_FILES,
    LOGS_SCHEMA_SQL,
    _DATE_PATH_RE,
    _SAFE_SLUG_RE,
    close_logs_db,
    get_logs_connection,
    init_logs_db,
)
from core.logs_db.indexer import (
    _build_index_rows_for_game_log,
    _discover_dirs_with_logs,
    _infer_round_meta_from_path,
    _make_disk_round_uid,
    _slug,
    _sync_rounds_sync,
    sync_and_index_rounds,
)
from core.logs_db.queries import (
    _build_safe_fts_query,
    get_logs_stats,
    get_round_by_uid,
    list_rounds,
    resolve_round_file_path,
    search_logs_fts,
)
from core.logs_db.uploads import (
    _classify_uploaded_filename,
    _extract_archive_groups,
    _safe_rel_group,
    delete_uploaded_round,
    ingest_uploaded_log,
)
from core.logs_db.whitelist import (
    add_to_whitelist,
    check_whitelist_access,
    get_audit_events,
    is_valid_discord_id,
    list_whitelist,
    record_audit_event,
    remove_from_whitelist,
)

logger = logging.getLogger(__name__)

_current_logs_db_path: str = "logs.db"
_connection: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()
_sync_lock = asyncio.Lock()

__all__ = [
    "ALLOWED_LOG_FILES",
    "LOGS_SCHEMA_SQL",
    "_SAFE_SLUG_RE",
    "_DATE_PATH_RE",
    "_current_logs_db_path",
    "_connection",
    "_db_lock",
    "_sync_lock",
    "init_logs_db",
    "close_logs_db",
    "get_logs_connection",
    "is_valid_discord_id",
    "check_whitelist_access",
    "list_whitelist",
    "add_to_whitelist",
    "remove_from_whitelist",
    "record_audit_event",
    "get_audit_events",
    "_slug",
    "_make_disk_round_uid",
    "_infer_round_meta_from_path",
    "_discover_dirs_with_logs",
    "_build_index_rows_for_game_log",
    "_sync_rounds_sync",
    "sync_and_index_rounds",
    "list_rounds",
    "get_round_by_uid",
    "get_logs_stats",
    "resolve_round_file_path",
    "_build_safe_fts_query",
    "search_logs_fts",
    "_classify_uploaded_filename",
    "_safe_rel_group",
    "_extract_archive_groups",
    "ingest_uploaded_log",
    "delete_uploaded_round",
]
