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
from contextlib import closing
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


class DiscoveryResult(list):
    def __init__(self, items=(), *, complete=True, errors=None):
        super().__init__(items)
        self.complete = complete
        self.errors = errors or []


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
    if base_dir is None:
        return DiscoveryResult()
    if not base_dir.is_dir():
        return DiscoveryResult(complete=False, errors=["Source directory is unavailable"])
    resolved_base = base_dir.resolve()
    discovered = DiscoveryResult()

    def walk_error(exc):
        discovered.complete = False
        discovered.errors.append(str(exc))

    for dirpath, dirnames, filenames in os.walk(resolved_base, onerror=walk_error):
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

        try:
            game_stat = game_path.stat() if has_game else None
            qdel_stat = qdel_path.stat() if has_qdel else None
            init_stat = init_path.stat() if has_init else None
            for file_path in (game_path, qdel_path, init_path):
                if file_path.exists() and not file_path.resolve().is_relative_to(resolved_base):
                    raise OSError("Log symlink escapes its source directory")
        except OSError as exc:
            walk_error(exc)
            continue

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


def _boundary_hash(stream, offset):
    position = stream.tell()
    start = max(0, offset - 4096)
    stream.seek(start)
    value = hashlib.sha256(stream.read(offset - start)).hexdigest()
    stream.seek(position)
    return value


def _valid_checkpoint(stream, checkpoint, stat, include_debug):
    return bool(
        checkpoint
        and checkpoint.get("file_id") == [stat.st_dev, stat.st_ino]
        and checkpoint.get("include_debug") == bool(include_debug)
        and 0 <= checkpoint.get("offset", -1) <= stat.st_size
        and checkpoint.get("boundary") == _boundary_hash(stream, checkpoint["offset"])
    )


def _write_checkpoint(conn, uid, generation, cp):
    conn.execute(
        "INSERT INTO log_index_checkpoints VALUES (?,?,?) ON CONFLICT(round_uid,generation) DO UPDATE SET checkpoint_json=excluded.checkpoint_json",
        (uid, generation, json.dumps(cp, ensure_ascii=False)),
    )


def _publish_meta(conn, item, generation, cp):
    meta = cp["meta"]
    date = item["round_date"]
    if not _DATE_PATH_RE.search(item["dir_path"]) and meta["started_at"]:
        date = meta["started_at"][:10]
    conn.execute(
        """UPDATE log_rounds SET active_generation=?,line_count=?,gameplay_line_count=?,
        category_counts_json=?,started_at=?,ended_at=?,round_id=?,round_date=?,
        indexed_mtime=?,indexed_size=?,indexed_at=?,includes_debug=?,index_error=NULL WHERE round_uid=?""",
        (generation, meta["total_lines"], meta["gameplay_lines"], json.dumps(meta["category_counts"]),
         meta["started_at"], meta["ended_at"], meta["round_id"] or item["round_id"], date,
         cp["mtime_ns"] / 1e9, cp["offset"], time.time(), int(cp["include_debug"]), item["round_uid"]),
    )


def _index_file(conn, item, old, *, include_debug, force_reindex):
    from core.log_parser.filters import _decode_log_line, parse_raw_line
    uid = item["round_uid"]
    path = Path(item["dir_path"]) / "game.log"
    if not item["has_game_log"]:
        conn.execute("UPDATE log_rounds SET indexed_at=?,index_error=NULL WHERE round_uid=?", (time.time(), uid))
        conn.commit()
        return 0, False
    active = int(old.get("active_generation", 0))
    checkpoints = {
        int(row[0]): json.loads(row[1])
        for row in conn.execute("SELECT generation,checkpoint_json FROM log_index_checkpoints WHERE round_uid=?", (uid,))
    }
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        limit = stat.st_size
        cp = checkpoints.get(active)
        pending = max((g for g in checkpoints if g > active), default=None)
        rebuilding = True
        if pending is not None and _valid_checkpoint(stream, checkpoints[pending], stat, include_debug):
            generation, cp = pending, checkpoints[pending]
        elif not force_reindex and _valid_checkpoint(stream, cp, stat, include_debug) and not (
            cp["offset"] == limit and cp.get("mtime_ns") != stat.st_mtime_ns
        ):
            generation = active
            rebuilding = False
            if cp["offset"] == limit and cp.get("mtime_ns") == stat.st_mtime_ns:
                return 0, True
        else:
            generation = max([active, *checkpoints.keys()]) + 1
            cp = {
                "file_id": [stat.st_dev, stat.st_ino], "include_debug": bool(include_debug),
                "offset": 0, "line_no": 0, "prev_ts": "", "prev_cat": "SYSTEM",
                "boundary": hashlib.sha256(b"").hexdigest(),
                "meta": {"total_lines": 0, "gameplay_lines": 0, "category_counts": {},
                         "round_id": None, "started_at": None, "ended_at": None},
            }
        cp["mtime_ns"] = stat.st_mtime_ns
        stream.seek(cp["offset"])
        rows = []
        lines_in_batch = 0
        added = 0

        def flush():
            nonlocal rows, lines_in_batch
            cp["boundary"] = _boundary_hash(stream, cp["offset"])
            with conn:
                if rows:
                    conn.executemany(
                        """INSERT INTO log_entries (round_uid,file_name,line_no,timestamp,category,
                        ckeys_space,message,coord_x,coord_y,coord_z,generation,byte_offset,byte_length,is_continuation)
                        VALUES (?,'game.log',?,?,?,?,?,?,?,?,?,?,?,?)""", rows,
                    )
                _write_checkpoint(conn, uid, generation, cp)
                if not rebuilding:
                    _publish_meta(conn, item, generation, cp)
            rows = []
            lines_in_batch = 0

        while stream.tell() < limit:
            offset = stream.tell()
            raw = stream.readline(limit - offset)
            if not raw:
                break
            if item["source_type"] == "disk" and not raw.endswith(b"\n"):
                break
            cp["line_no"] += 1
            decoded = _decode_log_line(raw)
            if cp["line_no"] == 1:
                decoded = decoded.lstrip("\ufeff")
            ts, cat, msg, continuation = parse_raw_line(cp["line_no"], decoded, cp["prev_ts"], cp["prev_cat"])
            if not continuation:
                cp["prev_ts"], cp["prev_cat"] = ts, cat
            meta = cp["meta"]
            meta["total_lines"] += 1
            meta["gameplay_lines"] += int(cat not in TECHNICAL_NOISE_CATEGORIES)
            meta["category_counts"][cat] = meta["category_counts"].get(cat, 0) + 1
            if ts:
                meta["started_at"] = meta["started_at"] or ts
                meta["ended_at"] = ts
            if meta["round_id"] is None and not continuation and cat == "SYSTEM":
                match = re.search(r"Starting up.*\(ID:\s*([A-Za-z0-9_-]+)\)", msg)
                if match:
                    meta["round_id"] = match.group(1)
            if include_debug or cat != "DEBUG":
                _, plain = sanitize_byond_html(msg)
                coords = primary_coord_from_text(msg) or (None, None, None)
                rows.append((uid, cp["line_no"], ts, cat, " ".join(extract_ckeys_from_text(plain)), plain,
                             *coords, generation, offset, len(raw), int(continuation)))
                added += 1
            cp["offset"] = stream.tell()
            lines_in_batch += 1
            if lines_in_batch >= 1000:
                flush()
        flush()
        # The previous generation remains searchable until this single commit.
        with conn:
            _publish_meta(conn, item, generation, cp)
        # Collect obsolete rows in bounded transactions, preserving active readers' WAL snapshots.
        while True:
            with conn:
                count = conn.execute(
                    "DELETE FROM log_entries WHERE id IN (SELECT id FROM log_entries WHERE round_uid=? AND generation!=? LIMIT 1000)",
                    (uid, generation),
                ).rowcount
            if not count:
                break
        with conn:
            conn.execute("DELETE FROM log_index_checkpoints WHERE round_uid=? AND generation!=?", (uid, generation))
        return added, False


def _sync_rounds_conn(conn, root_dir, uploads_dir, *, include_debug=False, force_reindex=False):
    started = time.monotonic()
    now = time.time()
    conn.row_factory = sqlite3.Row
    sources = {"disk": _discover_dirs_with_logs(root_dir, "disk"),
               "upload": _discover_dirs_with_logs(uploads_dir, "upload")}
    existing = {row["round_uid"]: dict(row) for row in conn.execute("SELECT * FROM log_rounds")}
    summary = dict(discovered=sum(map(len, sources.values())), indexed_rounds=0, indexed_lines=0,
                   skipped_unchanged=0, removed_stale=0, errors=0)
    for source, found in sources.items():
        root = root_dir if source == "disk" else uploads_dir
        complete = getattr(found, "complete", True)
        errors = getattr(found, "errors", [])
        summary["errors"] += int(not complete)
        found_uids = {item["round_uid"] for item in found}
        with conn:
            conn.execute(
                """INSERT INTO log_sources(source,status,last_scan_at,last_success_at,error) VALUES (?,?,?,?,?)
                ON CONFLICT(source) DO UPDATE SET status=excluded.status,last_scan_at=excluded.last_scan_at,
                last_success_at=COALESCE(excluded.last_success_at,log_sources.last_success_at),error=excluded.error""",
                (source, "disabled" if root is None else ("indexing" if complete else "degraded"), now,
                 None, "; ".join(errors)[:500] or None),
            )
            for uid, old in existing.items():
                if old["source_type"] != source or root is None or not complete:
                    continue
                if uid in found_uids:
                    conn.execute("UPDATE log_rounds SET missing_since=NULL WHERE round_uid=?", (uid,))
                elif old.get("missing_since") is not None and now - old["missing_since"] >= 60:
                    conn.execute("DELETE FROM log_rounds WHERE round_uid=?", (uid,))
                    summary["removed_stale"] += 1
                elif old.get("missing_since") is None:
                    conn.execute("UPDATE log_rounds SET missing_since=? WHERE round_uid=?", (now, uid))
        pending_bytes = 0
        source_failed = not complete
        for item in found:
            uid = item["round_uid"]
            old = existing.get(uid, {})
            label, uploaded_by, uploaded_at = old.get("label", ""), old.get("uploaded_by"), old.get("uploaded_at")
            metadata = Path(item["dir_path"]) / ".hilleye_meta.json"
            if metadata.is_file():
                try:
                    data = json.loads(metadata.read_text(encoding="utf-8"))
                    label = label or str(data.get("label") or "")
                    uploaded_by = uploaded_by or data.get("uploaded_by")
                    uploaded_at = uploaded_at or data.get("uploaded_at")
                except (ValueError, OSError):
                    pass
            with conn:
                conn.execute(
                    """INSERT INTO log_rounds(round_uid,source_type,round_id,round_date,dir_path,label,uploaded_by,uploaded_at)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(round_uid) DO UPDATE SET dir_path=excluded.dir_path""",
                    (uid, source, item["round_id"], item["round_date"], item["dir_path"], label, uploaded_by, uploaded_at),
                )
                keys = ("has_game_log", "has_qdel_log", "has_initialize_log", "game_log_size", "game_log_mtime", "qdel_log_size", "qdel_log_mtime")
                conn.execute(f"UPDATE log_rounds SET {','.join(k+'=?' for k in keys)} WHERE round_uid=?", [*(item[k] for k in keys), uid])
            try:
                added, unchanged = _index_file(conn, item, old, include_debug=include_debug, force_reindex=force_reindex)
                summary["skipped_unchanged"] += int(unchanged)
                summary["indexed_rounds"] += int(not unchanged)
                summary["indexed_lines"] += added
            except Exception as exc:
                logger.exception("Failed indexing round %s", uid)
                summary["errors"] += 1
                source_failed = True
                with conn:
                    conn.execute("UPDATE log_rounds SET index_error=? WHERE round_uid=?", (str(exc)[:300], uid))
            indexed = conn.execute("SELECT indexed_size FROM log_rounds WHERE round_uid=?", (uid,)).fetchone()[0] or 0
            pending_bytes += max(0, item["game_log_size"] - indexed)
        with conn:
            conn.execute("""UPDATE log_sources SET pending_bytes=?,status=?,
                         last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END WHERE source=?""",
                         (pending_bytes, "disabled" if root is None else ("degraded" if source_failed else "ok"),
                          int(root is not None and not source_failed), time.time(), source))
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return summary


def _sync_rounds_sync(db_path, root_dir, uploads_dir, *, include_debug=False, force_reindex=False):
    from core.file_lock import FileLock, lock_path
    with FileLock(lock_path(db_path, "index-write")):
        with closing(sqlite3.connect(db_path, timeout=15)) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            return _sync_rounds_conn(conn, root_dir, uploads_dir, include_debug=include_debug, force_reindex=force_reindex)


async def sync_and_index_rounds(root_dir, uploads_dir, *, include_debug=False, force_reindex=False):
    import core.logs_db as _ldb
    from core.file_lock import lease, lock_path
    from core.log_operations import finish_thread
    async with _ldb._sync_lock:
        if _ldb._current_logs_db_path == ":memory:":
            conn = await _ldb.get_logs_connection()
            return await conn._execute(_sync_rounds_conn, conn._conn, root_dir, uploads_dir,
                                       include_debug=include_debug, force_reindex=force_reindex)
        # Publication/deletion and backup share this lock so a scan sees complete uploads.
        async with lease(lock_path(_ldb._current_logs_db_path, "uploads")):
            async with lease(lock_path(_ldb._current_logs_db_path, "index-write")):
                def run():
                    with closing(sqlite3.connect(_ldb._current_logs_db_path, timeout=15)) as conn:
                        conn.execute("PRAGMA foreign_keys=ON")
                        conn.execute("PRAGMA busy_timeout=5000")
                        return _sync_rounds_conn(conn, root_dir, uploads_dir, include_debug=include_debug, force_reindex=force_reindex)
                return await finish_thread(run)
