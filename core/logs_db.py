"""SQLite WAL storage, FTS5 global log index, Discord whitelist, audit log, and upload manager."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tarfile
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import uuid
import zipfile

import aiosqlite

from core.byond_topic import normalize_ckey
from core.log_parser import (
    TECHNICAL_NOISE_CATEGORIES,
    category_matches_filters,
    decode_log_bytes,
    extract_ckeys_from_text,
    inspect_game_log_summary,
    iter_parsed_file_lines,
    primary_coord_from_text,
    sanitize_byond_html,
)

logger = logging.getLogger(__name__)

ALLOWED_LOG_FILES: Set[str] = {"game.log", "qdel.log", "initialize.log"}
_SAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_DATE_PATH_RE = re.compile(r"(20\d{2})[/\\](0[1-9]|1[0-2])[/\\](0[1-9]|[12]\d|3[01])")

LOGS_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

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

_current_logs_db_path: str = "logs.db"
_connection: Optional[aiosqlite.Connection] = None
_db_lock = asyncio.Lock()
_sync_lock = asyncio.Lock()


async def init_logs_db(db_path: str = "logs.db") -> None:
    """Initialize SQLite WAL database for the log viewer, whitelist, audit log, and FTS5 index."""
    global _connection, _current_logs_db_path
    async with _db_lock:
        if _connection is not None:
            await _connection.close()
            _connection = None

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
        await conn.executescript(LOGS_SCHEMA_SQL)
        await conn.commit()
        _connection = conn
        _current_logs_db_path = db_path


async def close_logs_db() -> None:
    """Safely close the async SQLite connection for logs."""
    global _connection
    async with _db_lock:
        if _connection is not None:
            await _connection.close()
            _connection = None


async def get_logs_connection() -> aiosqlite.Connection:
    if _connection is None:
        raise RuntimeError("Logs database is not initialized")
    return _connection


# ---------------------------------------------------------------------------
# Discord Whitelist & Audit Log
# ---------------------------------------------------------------------------

def is_valid_discord_id(discord_id: str) -> bool:
    val = (discord_id or "").strip()
    return val.isdigit() and 15 <= len(val) <= 22


async def check_whitelist_access(
    discord_id: str,
    superadmin_ids: Sequence[str],
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Return (allowed, role, db_entry). Superadmins from config always have 'superadmin' access."""
    did = (discord_id or "").strip()
    if not did:
        return False, "none", None
    if did in {str(s).strip() for s in superadmin_ids if s}:
        return True, "superadmin", {"discord_id": did, "role": "superadmin", "from_config": True}

    conn = await get_logs_connection()
    cursor = await conn.execute(
        "SELECT discord_id, display_name, role, added_by, added_at FROM log_whitelist WHERE discord_id = ?",
        (did,),
    )
    row = await cursor.fetchone()
    if row is None:
        return False, "none", None
    entry = dict(row)
    role = entry.get("role") if entry.get("role") in ("viewer", "admin") else "viewer"
    return True, role, entry


async def list_whitelist(superadmin_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """Return combined view of config super-admins and dynamic SQLite whitelist entries."""
    conn = await get_logs_connection()
    cursor = await conn.execute(
        "SELECT discord_id, display_name, role, added_by, added_at FROM log_whitelist ORDER BY added_at DESC"
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    super_set = {str(s).strip() for s in superadmin_ids if s}
    seen = {r["discord_id"] for r in rows}

    for row in rows:
        row["is_config_superadmin"] = row["discord_id"] in super_set

    for sid in sorted(super_set):
        if sid not in seen:
            rows.insert(
                0,
                {
                    "discord_id": sid,
                    "display_name": "Суперадмин (config.json)",
                    "role": "superadmin",
                    "added_by": "config.json",
                    "added_at": None,
                    "is_config_superadmin": True,
                },
            )
    return rows


async def add_to_whitelist(
    discord_id: str,
    *,
    display_name: str = "",
    role: str = "viewer",
    added_by: str = "system",
) -> None:
    did = (discord_id or "").strip()
    if not is_valid_discord_id(did):
        raise ValueError("Некорректный Discord ID (ожидается 15–22 цифры).")
    clean_role = "admin" if role == "admin" else "viewer"
    clean_name = (display_name or "").strip()[:80]
    conn = await get_logs_connection()
    await conn.execute(
        """
        INSERT INTO log_whitelist (discord_id, display_name, role, added_by, added_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
            display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE log_whitelist.display_name END,
            role = excluded.role,
            added_by = excluded.added_by,
            added_at = excluded.added_at
        """,
        (did, clean_name, clean_role, added_by, time.time()),
    )
    await conn.commit()


async def remove_from_whitelist(discord_id: str, superadmin_ids: Sequence[str]) -> bool:
    did = (discord_id or "").strip()
    if did in {str(s).strip() for s in superadmin_ids if s}:
        raise ValueError("Нельзя удалить супер-админа из config.json через веб-интерфейс.")
    conn = await get_logs_connection()
    cur = await conn.execute("DELETE FROM log_whitelist WHERE discord_id = ?", (did,))
    await conn.commit()
    return cur.rowcount > 0


async def record_audit_event(
    action: str,
    *,
    discord_id: Optional[str] = None,
    discord_username: Optional[str] = None,
    target: Optional[str] = None,
    ip_address: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    if _connection is None:
        return
    try:
        await _connection.execute(
            """
            INSERT INTO log_audit_events (created_at, discord_id, discord_username, action, target, ip_address, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                discord_id,
                (discord_username or "")[:80] if discord_username else None,
                action[:40],
                (target or "")[:200] if target else None,
                (ip_address or "")[:64] if ip_address else None,
                (details or "")[:500] if details else None,
            ),
        )
        await _connection.commit()
    except Exception:
        logger.debug("Failed to write log audit event", exc_info=True)


async def get_audit_events(
    *,
    limit: int = 100,
    offset: int = 0,
    action_filter: str = "",
) -> Tuple[List[Dict[str, Any]], int]:
    conn = await get_logs_connection()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    action = (action_filter or "").strip()

    if action:
        cnt_cur = await conn.execute("SELECT COUNT(*) FROM log_audit_events WHERE action = ?", (action,))
        total = int((await cnt_cur.fetchone())[0])
        cur = await conn.execute(
            """
            SELECT id, created_at, discord_id, discord_username, action, target, ip_address, details
            FROM log_audit_events
            WHERE action = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (action, limit, offset),
        )
    else:
        cnt_cur = await conn.execute("SELECT COUNT(*) FROM log_audit_events")
        total = int((await cnt_cur.fetchone())[0])
        cur = await conn.execute(
            """
            SELECT id, created_at, discord_id, discord_username, action, target, ip_address, details
            FROM log_audit_events
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
    return [dict(r) for r in await cur.fetchall()], total


# ---------------------------------------------------------------------------
# Round Discovery, Indexing, and FTS5 Search
# ---------------------------------------------------------------------------

def _slug(val: str) -> str:
    cleaned = _SAFE_SLUG_RE.sub("-", val).strip("-")
    return cleaned or "round"


def _make_disk_round_uid(rel_posix: str, round_id: str) -> str:
    digest = hashlib.sha1(rel_posix.encode("utf-8")).hexdigest()[:12]
    return f"d-{digest}-{_slug(round_id)[:18]}"


def _infer_round_meta_from_path(round_dir: Path, base_dir: Path) -> Tuple[str, str]:
    """Infer (round_id, round_date) from directory structure."""
    rel = round_dir.relative_to(base_dir).as_posix()
    name = round_dir.name
    round_id = name[6:] if name.startswith("round-") and len(name) > 6 else name

    m_date = _DATE_PATH_RE.search(rel)
    if m_date:
        round_date = f"{m_date.group(1)}-{m_date.group(2)}-{m_date.group(3)}"
    else:
        round_date = ""
    return round_id, round_date


def _discover_dirs_with_logs(base_dir: Optional[Path], source_type: str) -> List[Dict[str, Any]]:
    if base_dir is None or not base_dir.is_dir():
        return []
    resolved_base = base_dir.resolve()
    discovered: List[Dict[str, Any]] = []

    for dirpath, dirnames, filenames in os.walk(resolved_base):
        # Skip runtime and hidden directories
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d.lower() != "runtime"
        )
        fn_set = set(filenames)
        has_game = "game.log" in fn_set
        has_qdel = "qdel.log" in fn_set
        has_init = "initialize.log" in fn_set
        if not (has_game or has_qdel or has_init):
            continue

        cur_dir = Path(dirpath).resolve()
        if not cur_dir.is_relative_to(resolved_base):
            continue

        rel_posix = cur_dir.relative_to(resolved_base).as_posix()
        round_id, round_date = _infer_round_meta_from_path(cur_dir, resolved_base)

        game_path = cur_dir / "game.log"
        qdel_path = cur_dir / "qdel.log"
        init_path = cur_dir / "initialize.log"

        game_stat = game_path.stat() if has_game else None
        qdel_stat = qdel_path.stat() if has_qdel else None
        init_stat = init_path.stat() if has_init else None

        if not round_date:
            ts_fallback = (
                (game_stat.st_mtime if game_stat else None)
                or (qdel_stat.st_mtime if qdel_stat else None)
                or (init_stat.st_mtime if init_stat else time.time())
            )
            round_date = time.strftime("%Y-%m-%d", time.gmtime(ts_fallback))

        if source_type == "disk":
            round_uid = _make_disk_round_uid(rel_posix, round_id)
        else:
            # Uploaded rounds use their folder name as stable uid if formatted u-..., else deterministic
            round_uid = cur_dir.name if cur_dir.name.startswith("u-") else f"u-{hashlib.sha1(rel_posix.encode('utf-8')).hexdigest()[:12]}-{_slug(round_id)[:18]}"

        discovered.append(
            {
                "round_uid": round_uid,
                "source_type": source_type,
                "round_id": round_id,
                "round_date": round_date,
                "dir_path": str(cur_dir),
                "has_game_log": int(has_game),
                "has_qdel_log": int(has_qdel),
                "has_initialize_log": int(has_init),
                "game_log_size": int(game_stat.st_size) if game_stat else 0,
                "game_log_mtime": float(game_stat.st_mtime) if game_stat else 0.0,
                "qdel_log_size": int(qdel_stat.st_size) if qdel_stat else 0,
                "qdel_log_mtime": float(qdel_stat.st_mtime) if qdel_stat else 0.0,
            }
        )
    return discovered


def _build_index_rows_for_game_log(
    round_uid: str,
    game_log_path: Path,
    *,
    include_debug: bool,
) -> Tuple[Dict[str, Any], List[Tuple[str, str, int, str, str, str, str]]]:
    """Parse game.log once to produce metadata summary and FTS index rows."""
    category_counts: Dict[str, int] = {}
    total_lines = 0
    gameplay_lines = 0
    round_id: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    rows: List[Tuple[str, str, int, str, str, str, str, Optional[int], Optional[int], Optional[int]]] = []

    for line_no, ts, cat, raw_msg, is_cont in iter_parsed_file_lines(game_log_path):
        total_lines += 1
        category_counts[cat] = category_counts.get(cat, 0) + 1
        if cat not in TECHNICAL_NOISE_CATEGORIES:
            gameplay_lines += 1
        if ts:
            if started_at is None:
                started_at = ts
            ended_at = ts
        if round_id is None and not is_cont and cat == "SYSTEM" and "Starting up." in raw_msg:
            summary_match = re.search(r"\(ID:\s*([A-Za-z0-9_-]+)\)", raw_msg)
            if summary_match:
                round_id = summary_match.group(1)

        if not include_debug and cat == "DEBUG":
            continue

        _, plain_msg = sanitize_byond_html(raw_msg)
        ckeys = extract_ckeys_from_text(plain_msg)
        ckeys_space = " ".join(ckeys)
        coords = primary_coord_from_text(raw_msg)
        cx, cy, cz = coords if coords else (None, None, None)
        rows.append((round_uid, "game.log", line_no, ts, cat, ckeys_space, plain_msg, cx, cy, cz))

    meta = {
        "total_lines": total_lines,
        "gameplay_lines": gameplay_lines,
        "category_counts": category_counts,
        "round_id": round_id,
        "started_at": started_at,
        "ended_at": ended_at,
    }
    return meta, rows


def _sync_rounds_sync(
    db_path: str,
    root_dir: Optional[Path],
    uploads_dir: Optional[Path],
    *,
    include_debug: bool = False,
    force_reindex: bool = False,
) -> Dict[str, Any]:
    """Synchronous worker executed in thread to discover rounds and update SQLite FTS5 index."""
    t0 = time.monotonic()
    disk_rounds = _discover_dirs_with_logs(root_dir, "disk")
    upload_rounds = _discover_dirs_with_logs(uploads_dir, "upload")
    all_found = disk_rounds + upload_rounds

    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    try:
        existing_rows = {
            r["round_uid"]: dict(r)
            for r in conn.execute("SELECT * FROM log_rounds").fetchall()
        }
        found_uids = {item["round_uid"] for item in all_found}

        # Remove stale rounds whose source root was scanned and the round no longer exists
        removed_count = 0
        for uid, old in existing_rows.items():
            src = old.get("source_type")
            if src == "disk" and root_dir is not None and uid not in found_uids:
                conn.execute("DELETE FROM log_rounds WHERE round_uid = ?", (uid,))
                removed_count += 1
            elif src == "upload" and uploads_dir is not None and uid not in found_uids:
                conn.execute("DELETE FROM log_rounds WHERE round_uid = ?", (uid,))
                removed_count += 1

        indexed_rounds = 0
        indexed_lines = 0
        skipped_unchanged = 0
        errors = 0

        for item in all_found:
            uid = item["round_uid"]
            old = existing_rows.get(uid)
            game_path = Path(item["dir_path"]) / "game.log"

            needs_reindex = (
                force_reindex
                or old is None
                or old.get("indexed_at") is None
                or int(old.get("includes_debug") or 0) != int(bool(include_debug))
                or float(old.get("indexed_mtime") or -1.0) != item["game_log_mtime"]
                or int(old.get("indexed_size") or -1) != item["game_log_size"]
            )

            # Preserve custom label/upload metadata if previously stored
            label = old["label"] if old else ""
            uploaded_by = old["uploaded_by"] if old else None
            uploaded_at = old["uploaded_at"] if old else None
            meta_file = Path(item["dir_path"]) / ".hilleye_meta.json"
            if meta_file.is_file() and (not label or not uploaded_by):
                try:
                    mdata = json.loads(meta_file.read_text(encoding="utf-8"))
                    label = label or str(mdata.get("label") or "")
                    uploaded_by = uploaded_by or mdata.get("uploaded_by")
                    uploaded_at = uploaded_at or mdata.get("uploaded_at")
                except Exception:
                    pass

            if not needs_reindex:
                # Update non-index file flags if qdel/init changed
                if (
                    old["has_qdel_log"] != item["has_qdel_log"]
                    or old["qdel_log_size"] != item["qdel_log_size"]
                    or old["has_initialize_log"] != item["has_initialize_log"]
                ):
                    conn.execute(
                        """
                        UPDATE log_rounds
                        SET has_qdel_log = ?, has_initialize_log = ?, qdel_log_size = ?, qdel_log_mtime = ?
                        WHERE round_uid = ?
                        """,
                        (
                            item["has_qdel_log"],
                            item["has_initialize_log"],
                            item["qdel_log_size"],
                            item["qdel_log_mtime"],
                            uid,
                        ),
                    )
                skipped_unchanged += 1
                continue

            try:
                if item["has_game_log"] and game_path.is_file():
                    meta, fts_rows = _build_index_rows_for_game_log(
                        uid, game_path, include_debug=include_debug
                    )
                else:
                    meta = {
                        "total_lines": 0,
                        "gameplay_lines": 0,
                        "category_counts": {},
                        "round_id": None,
                        "started_at": None,
                        "ended_at": None,
                    }
                    fts_rows = []

                final_round_id = meta["round_id"] or item["round_id"]
                final_round_date = (
                    meta["started_at"][:10]
                    if (not _DATE_PATH_RE.search(item["dir_path"]) and meta["started_at"])
                    else item["round_date"]
                )

                with conn:
                    conn.execute(
                        """
                        INSERT INTO log_rounds (
                            round_uid, source_type, round_id, round_date, dir_path, label,
                            uploaded_by, uploaded_at, has_game_log, has_qdel_log, has_initialize_log,
                            game_log_size, game_log_mtime, qdel_log_size, qdel_log_mtime,
                            line_count, gameplay_line_count, category_counts_json,
                            started_at, ended_at, indexed_mtime, indexed_size, indexed_at,
                            includes_debug, index_error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                        ON CONFLICT(round_uid) DO UPDATE SET
                            round_id = excluded.round_id,
                            round_date = excluded.round_date,
                            dir_path = excluded.dir_path,
                            has_game_log = excluded.has_game_log,
                            has_qdel_log = excluded.has_qdel_log,
                            has_initialize_log = excluded.has_initialize_log,
                            game_log_size = excluded.game_log_size,
                            game_log_mtime = excluded.game_log_mtime,
                            qdel_log_size = excluded.qdel_log_size,
                            qdel_log_mtime = excluded.qdel_log_mtime,
                            line_count = excluded.line_count,
                            gameplay_line_count = excluded.gameplay_line_count,
                            category_counts_json = excluded.category_counts_json,
                            started_at = excluded.started_at,
                            ended_at = excluded.ended_at,
                            indexed_mtime = excluded.indexed_mtime,
                            indexed_size = excluded.indexed_size,
                            indexed_at = excluded.indexed_at,
                            includes_debug = excluded.includes_debug,
                            index_error = NULL
                        """,
                        (
                            uid,
                            item["source_type"],
                            final_round_id,
                            final_round_date,
                            item["dir_path"],
                            label,
                            uploaded_by,
                            uploaded_at,
                            item["has_game_log"],
                            item["has_qdel_log"],
                            item["has_initialize_log"],
                            item["game_log_size"],
                            item["game_log_mtime"],
                            item["qdel_log_size"],
                            item["qdel_log_mtime"],
                            meta["total_lines"],
                            meta["gameplay_lines"],
                            json.dumps(meta["category_counts"], ensure_ascii=False),
                            meta["started_at"],
                            meta["ended_at"],
                            item["game_log_mtime"],
                            item["game_log_size"],
                            time.time(),
                            int(bool(include_debug)),
                        ),
                    )
                    conn.execute("DELETE FROM log_entries WHERE round_uid = ?", (uid,))
                    if fts_rows:
                        conn.executemany(
                            """
                            INSERT INTO log_entries (
                                round_uid, file_name, line_no, timestamp, category,
                                ckeys_space, message, coord_x, coord_y, coord_z
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            fts_rows,
                        )
                indexed_rounds += 1
                indexed_lines += len(fts_rows)
            except Exception as exc:
                errors += 1
                logger.warning("Failed indexing round %s: %s", uid, exc)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO log_rounds (round_uid, source_type, round_id, round_date, dir_path, index_error)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(round_uid) DO UPDATE SET index_error = excluded.index_error
                        """,
                        (uid, item["source_type"], item["round_id"], item["round_date"], item["dir_path"], str(exc)[:300]),
                    )

        conn.commit()
    finally:
        conn.close()

    return {
        "discovered": len(all_found),
        "indexed_rounds": indexed_rounds,
        "indexed_lines": indexed_lines,
        "skipped_unchanged": skipped_unchanged,
        "removed_stale": removed_count,
        "errors": errors,
        "elapsed_seconds": round(time.monotonic() - t0, 3),
    }


async def sync_and_index_rounds(
    root_dir: Optional[Path],
    uploads_dir: Optional[Path],
    *,
    include_debug: bool = False,
    force_reindex: bool = False,
) -> Dict[str, Any]:
    """Thread-safe incremental discovery + SQLite FTS5 indexing for disk & uploaded rounds."""
    async with _sync_lock:
        if _current_logs_db_path == ":memory:":
            # For :memory: test DBs, operate directly on the open async connection
            conn = await get_logs_connection()
            disk_rounds = _discover_dirs_with_logs(root_dir, "disk")
            upload_rounds = _discover_dirs_with_logs(uploads_dir, "upload")
            indexed_rounds = 0
            indexed_lines = 0
            for item in disk_rounds + upload_rounds:
                uid = item["round_uid"]
                game_path = Path(item["dir_path"]) / "game.log"
                label = ""
                uploaded_by = None
                uploaded_at = None
                meta_file = Path(item["dir_path"]) / ".hilleye_meta.json"
                if meta_file.is_file():
                    try:
                        mdata = json.loads(meta_file.read_text(encoding="utf-8"))
                        label = str(mdata.get("label") or "")
                        uploaded_by = mdata.get("uploaded_by")
                        uploaded_at = mdata.get("uploaded_at")
                    except Exception:
                        pass
                if item["has_game_log"] and game_path.is_file():
                    meta, fts_rows = _build_index_rows_for_game_log(uid, game_path, include_debug=include_debug)
                else:
                    meta = {"total_lines": 0, "gameplay_lines": 0, "category_counts": {}, "round_id": None, "started_at": None, "ended_at": None}
                    fts_rows = []
                await conn.execute(
                    """
                    INSERT OR REPLACE INTO log_rounds (
                        round_uid, source_type, round_id, round_date, dir_path, label,
                        uploaded_by, uploaded_at,
                        has_game_log, has_qdel_log, has_initialize_log,
                        game_log_size, game_log_mtime, qdel_log_size, qdel_log_mtime,
                        line_count, gameplay_line_count, category_counts_json,
                        started_at, ended_at, indexed_mtime, indexed_size, indexed_at, includes_debug
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid, item["source_type"], meta["round_id"] or item["round_id"], item["round_date"],
                        item["dir_path"], label, uploaded_by, uploaded_at,
                        item["has_game_log"], item["has_qdel_log"], item["has_initialize_log"],
                        item["game_log_size"], item["game_log_mtime"], item["qdel_log_size"], item["qdel_log_mtime"],
                        meta["total_lines"], meta["gameplay_lines"], json.dumps(meta["category_counts"]),
                        meta["started_at"], meta["ended_at"], item["game_log_mtime"], item["game_log_size"],
                        time.time(), int(bool(include_debug)),
                    ),
                )
                await conn.execute("DELETE FROM log_entries WHERE round_uid = ?", (uid,))
                if fts_rows:
                    await conn.executemany(
                        "INSERT INTO log_entries (round_uid, file_name, line_no, timestamp, category, ckeys_space, message, coord_x, coord_y, coord_z) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        fts_rows,
                    )
                indexed_rounds += 1
                indexed_lines += len(fts_rows)
            await conn.commit()
            return {"discovered": len(disk_rounds) + len(upload_rounds), "indexed_rounds": indexed_rounds, "indexed_lines": indexed_lines, "skipped_unchanged": 0, "removed_stale": 0, "errors": 0, "elapsed_seconds": 0.0}

        return await asyncio.to_thread(
            _sync_rounds_sync,
            _current_logs_db_path,
            root_dir,
            uploads_dir,
            include_debug=include_debug,
            force_reindex=force_reindex,
        )


async def list_rounds(
    *,
    date_filter: str = "",
    source_filter: str = "",
    q: str = "",
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Query indexed round directory with date, source, and text filters."""
    conn = await get_logs_connection()
    clauses: List[str] = []
    params: List[Any] = []

    if date_filter.strip():
        clauses.append("round_date = ?")
        params.append(date_filter.strip())
    if source_filter in ("disk", "upload"):
        clauses.append("source_type = ?")
        params.append(source_filter)
    if q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(round_id LIKE ? OR round_date LIKE ? OR label LIKE ? OR round_uid LIKE ?)")
        params.extend([like, like, like, like])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cnt_cur = await conn.execute(f"SELECT COUNT(*) FROM log_rounds {where_sql}", params)
    total = int((await cnt_cur.fetchone())[0])

    limit = max(1, min(int(limit), 10000))
    offset = max(0, int(offset))
    cur = await conn.execute(
        f"""
        SELECT *
        FROM log_rounds
        {where_sql}
        ORDER BY round_date DESC, COALESCE(started_at, '') DESC, game_log_mtime DESC, round_uid DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    items = []
    for row in await cur.fetchall():
        d = dict(row)
        try:
            d["category_counts"] = json.loads(d.get("category_counts_json") or "{}")
        except Exception:
            d["category_counts"] = {}
        items.append(d)
    return items, total


async def get_round_by_uid(round_uid: str) -> Optional[Dict[str, Any]]:
    conn = await get_logs_connection()
    cur = await conn.execute("SELECT * FROM log_rounds WHERE round_uid = ?", (round_uid,))
    row = await cur.fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["category_counts"] = json.loads(d.get("category_counts_json") or "{}")
    except Exception:
        d["category_counts"] = {}
    return d


async def get_logs_stats() -> Dict[str, Any]:
    conn = await get_logs_connection()
    row = await (
        await conn.execute(
            """
            SELECT
                COUNT(*) AS total_rounds,
                SUM(CASE WHEN source_type = 'disk' THEN 1 ELSE 0 END) AS disk_rounds,
                SUM(CASE WHEN source_type = 'upload' THEN 1 ELSE 0 END) AS upload_rounds,
                COALESCE(SUM(line_count), 0) AS total_lines,
                COALESCE(SUM(gameplay_line_count), 0) AS gameplay_lines,
                COALESCE(SUM(game_log_size + qdel_log_size), 0) AS total_bytes,
                MAX(indexed_at) AS last_indexed_at
            FROM log_rounds
            """
        )
    ).fetchone()
    fts_cnt = await (await conn.execute("SELECT COUNT(*) FROM log_entries")).fetchone()
    dates_cur = await conn.execute(
        "SELECT round_date, COUNT(*) AS cnt FROM log_rounds GROUP BY round_date ORDER BY round_date DESC LIMIT 60"
    )
    dates = [dict(r) for r in await dates_cur.fetchall()]
    return {
        "total_rounds": int(row["total_rounds"] or 0),
        "disk_rounds": int(row["disk_rounds"] or 0),
        "upload_rounds": int(row["upload_rounds"] or 0),
        "total_lines": int(row["total_lines"] or 0),
        "gameplay_lines": int(row["gameplay_lines"] or 0),
        "total_bytes": int(row["total_bytes"] or 0),
        "indexed_entries": int(fts_cnt[0] or 0),
        "last_indexed_at": row["last_indexed_at"],
        "dates": dates,
    }


def resolve_round_file_path(
    round_record: Dict[str, Any],
    file_name: str,
    *,
    root_dir: Optional[Path],
    uploads_dir: Optional[Path],
) -> Optional[Path]:
    """Strictly verify round directory containment and file allowlist before disk access."""
    if file_name not in ALLOWED_LOG_FILES:
        return None
    dir_path = Path(round_record["dir_path"]).resolve()
    source_type = round_record.get("source_type")
    if source_type == "disk":
        if root_dir is None or not dir_path.is_relative_to(root_dir.resolve()):
            return None
    elif source_type == "upload":
        if uploads_dir is None or not dir_path.is_relative_to(uploads_dir.resolve()):
            return None
    else:
        return None

    candidate = (dir_path / file_name).resolve()
    if not candidate.is_relative_to(dir_path) or not candidate.is_file():
        return None
    return candidate


def _build_safe_fts_query(raw_query: str) -> str:
    """Convert arbitrary user input into a safe SQLite FTS5 query string."""
    tokens = re.findall(r"[^\s]+", (raw_query or "").strip())
    if not tokens:
        return ""
    quoted = []
    for tok in tokens[:16]:
        safe_tok = tok.replace('"', '""')
        quoted.append(f'"{safe_tok}"')
    return " AND ".join(quoted)


async def search_logs_fts(
    *,
    query: str = "",
    ckey: str = "",
    preset: str = "gameplay",
    categories: Optional[Sequence[str]] = None,
    date_from: str = "",
    date_to: str = "",
    round_uid: str = "",
    z_level: Optional[int] = None,
    only_coords: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Execute global cross-round search via SQLite FTS5."""
    conn = await get_logs_connection()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    ckey_norm = normalize_ckey(ckey) if ckey else ""
    fts_parts: List[str] = []
    safe_q = _build_safe_fts_query(query)
    if safe_q:
        fts_parts.append(f"({safe_q})")
    if ckey_norm:
        fts_parts.append(f'ckeys_space : "{ckey_norm}"')

    where_clauses: List[str] = []
    params: List[Any] = []

    use_fts = bool(fts_parts)
    if use_fts:
        where_clauses.append("log_entries_fts MATCH ?")
        params.append(" AND ".join(fts_parts))

    if round_uid.strip():
        where_clauses.append("e.round_uid = ?")
        params.append(round_uid.strip())
    if date_from.strip():
        where_clauses.append("r.round_date >= ?")
        params.append(date_from.strip())
    if date_to.strip():
        where_clauses.append("r.round_date <= ?")
        params.append(date_to.strip())
    if z_level is not None and int(z_level) > 0:
        where_clauses.append("e.coord_z = ?")
        params.append(int(z_level))
    elif only_coords:
        where_clauses.append("e.coord_z IS NOT NULL")

    # Category / preset filter
    if categories:
        clean_cats = sorted({c.strip().upper() for c in categories if c and c.strip()})
        if clean_cats:
            placeholders = ",".join("?" for _ in clean_cats)
            where_clauses.append(f"e.category IN ({placeholders})")
            params.extend(clean_cats)
    else:
        from core.log_parser import LOG_PRESETS
        preset_cfg = LOG_PRESETS.get(preset, LOG_PRESETS["gameplay"])
        if preset_cfg.get("include"):
            inc = sorted(preset_cfg["include"])
            placeholders = ",".join("?" for _ in inc)
            where_clauses.append(f"e.category IN ({placeholders})")
            params.extend(inc)
        elif preset_cfg.get("exclude"):
            exc = sorted(preset_cfg["exclude"])
            placeholders = ",".join("?" for _ in exc)
            where_clauses.append(f"e.category NOT IN ({placeholders})")
            params.extend(exc)

    if not where_clauses:
        return [], 0

    where_sql = " AND ".join(where_clauses)
    if use_fts:
        from_sql = """
            FROM log_entries_fts
            JOIN log_entries e ON e.id = log_entries_fts.rowid
            JOIN log_rounds r ON r.round_uid = e.round_uid
        """
    else:
        from_sql = """
            FROM log_entries e
            JOIN log_rounds r ON r.round_uid = e.round_uid
        """

    cnt_cur = await conn.execute(f"SELECT COUNT(*) {from_sql} WHERE {where_sql}", params)
    total = int((await cnt_cur.fetchone())[0])

    cur = await conn.execute(
        f"""
        SELECT
            e.round_uid, e.file_name, e.line_no, e.timestamp, e.category, e.ckeys_space, e.message,
            e.coord_x, e.coord_y, e.coord_z,
            r.round_id, r.round_date, r.source_type, r.label
        {from_sql}
        WHERE {where_sql}
        ORDER BY r.round_date DESC, e.timestamp DESC, e.line_no ASC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    hits = []
    for row in await cur.fetchall():
        d = dict(row)
        d["ckeys"] = [c for c in (d.get("ckeys_space") or "").split(" ") if c]
        hits.append(d)
    return hits, total


# ---------------------------------------------------------------------------
# Manual Log Upload / Archive Ingestion (Zip-Slip & Zip-Bomb Protected)
# ---------------------------------------------------------------------------

def _classify_uploaded_filename(name: str, data: bytes) -> Optional[str]:
    base = Path(name).name.lower()
    if base in ALLOWED_LOG_FILES:
        return base
    if not (base.endswith(".log") or base.endswith(".txt")):
        return None
    if "qdel" in base:
        return "qdel.log"
    if "init" in base:
        return "initialize.log"
    head = decode_log_bytes(data[:4096])
    if "qdel() Count:" in head or "Destroy() Cost:" in head:
        return "qdel.log"
    return "game.log"


def _safe_rel_group(member_path: str) -> str:
    """Extract clean parent directory key inside an archive while rejecting traversal."""
    normalized = member_path.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p or "\x00" in p for p in parts):
        raise ValueError(f"Небезопасный путь в архиве: {member_path}")
    if len(parts) == 1:
        return ""
    return "/".join(parts[:-1])


def _extract_archive_groups(
    filename: str,
    raw_bytes: bytes,
    *,
    max_extracted_bytes: int,
    max_archive_files: int,
) -> Dict[str, Dict[str, bytes]]:
    """Safely extract log files grouped by directory inside a .zip / .tar.gz / .log upload."""
    lower = filename.lower()
    groups: Dict[str, Dict[str, bytes]] = {}
    total_bytes = 0
    file_count = 0

    if lower.endswith(".zip"):
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw_bytes))
        except zipfile.BadZipFile as exc:
            raise ValueError("Повреждённый ZIP-архив.") from exc
        with zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                # Reject symlinks in Unix zip attributes
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Символические ссылки в архивах запрещены.")
                group_key = _safe_rel_group(info.filename)
                file_count += 1
                if file_count > max_archive_files:
                    raise ValueError(f"Слишком много файлов в архиве (лимит {max_archive_files}).")
                if info.file_size > max_extracted_bytes:
                    raise ValueError("Файл внутри архива превышает лимит распакованного размера.")
                with zf.open(info, "r") as member_fp:
                    chunks = []
                    member_read = 0
                    while True:
                        chunk = member_fp.read(65536)
                        if not chunk:
                            break
                        member_read += len(chunk)
                        total_bytes += len(chunk)
                        if total_bytes > max_extracted_bytes:
                            raise ValueError("Распакованный объём архива превышает допустимый лимит.")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                target_name = _classify_uploaded_filename(info.filename, content)
                if target_name:
                    groups.setdefault(group_key, {})[target_name] = content

    elif lower.endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:gz" if lower.endswith((".tar.gz", ".tgz")) else "r:"
        try:
            tf = tarfile.open(fileobj=io.BytesIO(raw_bytes), mode=mode)
        except tarfile.TarError as exc:
            raise ValueError("Повреждённый TAR/TGZ архив.") from exc
        with tf:
            for member in tf.getmembers():
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ValueError("В архиве допускаются только обычные файлы (без симлинков).")
                group_key = _safe_rel_group(member.name)
                file_count += 1
                if file_count > max_archive_files:
                    raise ValueError(f"Слишком много файлов в архиве (лимит {max_archive_files}).")
                if member.size > max_extracted_bytes:
                    raise ValueError("Файл внутри архива превышает лимит распакованного размера.")
                extracted = tf.extractfile(member)
                if extracted is None:
                    continue
                chunks = []
                while True:
                    chunk = extracted.read(65536)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > max_extracted_bytes:
                        raise ValueError("Распакованный объём архива превышает допустимый лимит.")
                    chunks.append(chunk)
                content = b"".join(chunks)
                target_name = _classify_uploaded_filename(member.name, content)
                if target_name:
                    groups.setdefault(group_key, {})[target_name] = content

    elif lower.endswith((".log", ".txt")):
        if len(raw_bytes) > max_extracted_bytes:
            raise ValueError("Размер файла превышает допустимый лимит.")
        target_name = _classify_uploaded_filename(filename, raw_bytes)
        if not target_name:
            raise ValueError("Неподдерживаемый тип файла.")
        groups[""] = {target_name: raw_bytes}
    else:
        raise ValueError("Допустимы только файлы .log, .txt, .zip, .tar.gz, .tgz.")

    if not groups:
        raise ValueError("Не найдено ни одного подходящего файла лога (game.log / qdel.log / .log).")
    return groups


async def ingest_uploaded_log(
    filename: str,
    raw_bytes: bytes,
    *,
    uploads_dir: Path,
    label: str = "",
    uploaded_by: str = "",
    max_upload_bytes: int = 50 * 1024 * 1024,
    max_extracted_bytes: int = 200 * 1024 * 1024,
    max_archive_files: int = 100,
    include_debug: bool = False,
) -> List[Dict[str, Any]]:
    """Validate, extract, save, and index one or more uploaded round logs."""
    if not raw_bytes:
        raise ValueError("Загруженный файл пуст.")
    if len(raw_bytes) > max_upload_bytes:
        raise ValueError(f"Файл превышает максимальный размер {max_upload_bytes // (1024 * 1024)} МБ.")

    groups = await asyncio.to_thread(
        _extract_archive_groups,
        filename,
        raw_bytes,
        max_extracted_bytes=max_extracted_bytes,
        max_archive_files=max_archive_files,
    )

    resolved_uploads = uploads_dir.resolve()
    resolved_uploads.mkdir(parents=True, exist_ok=True)
    created_dirs: List[Path] = []
    created_rounds: List[Dict[str, Any]] = []
    clean_label = (label or "").strip()[:100]

    try:
        for group_key, files_map in groups.items():
            short_uid = uuid.uuid4().hex[:10]
            group_tail = Path(group_key).name if group_key else Path(filename).stem
            round_hint = group_tail[6:] if group_tail.startswith("round-") else group_tail
            uid = f"u-{short_uid}-{_slug(round_hint)[:16]}"
            target_dir = (resolved_uploads / uid).resolve()
            if not target_dir.is_relative_to(resolved_uploads):
                raise ValueError("Некорректный целевой путь загрузки.")
            target_dir.mkdir(parents=True, exist_ok=False)
            created_dirs.append(target_dir)

            for fname, fbytes in files_map.items():
                if fname not in ALLOWED_LOG_FILES:
                    continue
                (target_dir / fname).write_bytes(fbytes)

            meta_label = clean_label or (f"{Path(filename).name} ({group_key})" if group_key else Path(filename).name)
            meta_payload = {
                "label": meta_label[:100],
                "uploaded_by": uploaded_by,
                "uploaded_at": time.time(),
                "original_filename": Path(filename).name,
            }
            (target_dir / ".hilleye_meta.json").write_text(
                json.dumps(meta_payload, ensure_ascii=False), encoding="utf-8"
            )
            created_rounds.append({"round_uid": uid, "label": meta_label})
    except Exception:
        for d in created_dirs:
            shutil.rmtree(d, ignore_errors=True)
        raise

    await sync_and_index_rounds(
        None,
        resolved_uploads,
        include_debug=include_debug,
        force_reindex=False,
    )
    # Return full DB records for created rounds
    results: List[Dict[str, Any]] = []
    for item in created_rounds:
        rec = await get_round_by_uid(item["round_uid"])
        if rec:
            results.append(rec)
    return results


async def delete_uploaded_round(round_uid: str, *, uploads_dir: Path) -> bool:
    """Delete an uploaded round (refuses deleting disk rounds)."""
    rec = await get_round_by_uid(round_uid)
    if rec is None:
        return False
    if rec.get("source_type") != "upload":
        raise ValueError("Удалять можно только загруженные вручную логи.")
    resolved_uploads = uploads_dir.resolve()
    target_dir = Path(rec["dir_path"]).resolve()
    if not target_dir.is_relative_to(resolved_uploads) or target_dir == resolved_uploads:
        raise ValueError("Небезопасный путь удаления.")

    conn = await get_logs_connection()
    await conn.execute("DELETE FROM log_rounds WHERE round_uid = ?", (round_uid,))
    await conn.commit()
    if target_dir.is_dir():
        await asyncio.to_thread(shutil.rmtree, target_dir, True)
    return True
