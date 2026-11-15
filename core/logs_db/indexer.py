"""Round directory discovery and incremental SQLite FTS5 indexing."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

from core.log_parser import (
    TECHNICAL_NOISE_CATEGORIES,
    extract_ckeys_from_text,
    iter_parsed_file_lines,
    primary_coord_from_text,
    sanitize_byond_html,
)
from core.logs_db.connection import _DATE_PATH_RE, _SAFE_SLUG_RE

logger = logging.getLogger(__name__)


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
            round_uid = (
                cur_dir.name
                if cur_dir.name.startswith("u-")
                else f"u-{hashlib.sha1(rel_posix.encode('utf-8')).hexdigest()[:12]}-{_slug(round_id)[:18]}"
            )

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
) -> Tuple[Dict[str, Any], List[Tuple[str, str, int, str, str, str, str, Optional[int], Optional[int], Optional[int]]]]:
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
    import core.logs_db as _ldb

    async with _ldb._sync_lock:
        if _ldb._current_logs_db_path == ":memory:":
            # For :memory: test DBs, operate directly on the open async connection
            conn = await _ldb.get_logs_connection()
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
                    meta = {
                        "total_lines": 0,
                        "gameplay_lines": 0,
                        "category_counts": {},
                        "round_id": None,
                        "started_at": None,
                        "ended_at": None,
                    }
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
                        uid,
                        item["source_type"],
                        meta["round_id"] or item["round_id"],
                        item["round_date"],
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
                        json.dumps(meta["category_counts"]),
                        meta["started_at"],
                        meta["ended_at"],
                        item["game_log_mtime"],
                        item["game_log_size"],
                        time.time(),
                        int(bool(include_debug)),
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
            return {
                "discovered": len(disk_rounds) + len(upload_rounds),
                "indexed_rounds": indexed_rounds,
                "indexed_lines": indexed_lines,
                "skipped_unchanged": 0,
                "removed_stale": 0,
                "errors": 0,
                "elapsed_seconds": 0.0,
            }

        return await asyncio.to_thread(
            _sync_rounds_sync,
            _ldb._current_logs_db_path,
            root_dir,
            uploads_dir,
            include_debug=include_debug,
            force_reindex=force_reindex,
        )
