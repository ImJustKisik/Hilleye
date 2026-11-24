"""Round directory discovery and incremental SQLite FTS5 indexing."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from stat import S_ISREG
import time
from contextlib import nullcontext
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from core.log_parser import (
    extract_ckeys_from_text,
    primary_coord_from_text,
    sanitize_byond_html,
)
from core.log_parser.models import _RoundMetaAccumulator
from core.logs_db.connection import _DATE_PATH_RE, _SAFE_SLUG_RE, run_logs_transaction

logger = logging.getLogger(__name__)
_FILE_FIELDS = (
    "has_game_log", "has_qdel_log", "has_initialize_log",
    "game_log_size", "game_log_mtime", "qdel_log_size", "qdel_log_mtime", "initialize_log_size",
)


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
    round_date = ""
    if m_date:
        try:
            round_date = date(*(int(part) for part in m_date.groups())).isoformat()
        except ValueError:
            pass
    return round_id, round_date


def _discover_dirs_with_logs(base_dir: Optional[Path], source_type: str, *, selected=None) -> List[Dict[str, Any]]:
    if base_dir is None:
        return DiscoveryResult()
    if not base_dir.is_dir():
        return DiscoveryResult(complete=False, errors=["Source directory is unavailable"])
    resolved_base = base_dir.resolve()
    discovered = DiscoveryResult()

    def walk_error(exc):
        discovered.complete = False
        discovered.errors.append(str(exc))

    def scan_selected():
        for directory in selected:
            try:
                yield directory, [], os.listdir(directory)
            except OSError as exc:
                walk_error(exc)

    directories = os.walk(resolved_base, onerror=walk_error) if selected is None else scan_selected()
    for dirpath, dirnames, filenames in directories:
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
        date_from_path = bool(round_date)

        game_path = cur_dir / "game.log"
        qdel_path = cur_dir / "qdel.log"
        init_path = cur_dir / "initialize.log"

        try:
            for file_path in (game_path, qdel_path, init_path):
                if file_path.is_symlink():
                    raise OSError("Log files must not be symbolic links")
            game_stat = game_path.stat() if has_game else None
            qdel_stat = qdel_path.stat() if has_qdel else None
            init_stat = init_path.stat() if has_init else None
            if any(info is not None and not S_ISREG(info.st_mode) for info in (game_stat, qdel_stat, init_stat)):
                raise OSError("Log files must be regular files")
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
                "date_from_path": date_from_path,
                "dir_path": str(cur_dir),
                "has_game_log": int(has_game),
                "has_qdel_log": int(has_qdel),
                "has_initialize_log": int(has_init),
                "game_log_size": int(game_stat.st_size) if game_stat else 0,
                "game_log_mtime": float(game_stat.st_mtime) if game_stat else 0.0,
                "game_log_stat": game_stat,
                "qdel_log_size": int(qdel_stat.st_size) if qdel_stat else 0,
                "qdel_log_mtime": float(qdel_stat.st_mtime) if qdel_stat else 0.0,
                "initialize_log_size": int(init_stat.st_size) if init_stat else 0,
            }
        )
    return discovered


_CHECKPOINT_VERSION = 3
_MAP_REVISION = re.compile(r"[0-9a-fA-F]{7,40}")
_DISK_TAIL_SETTLE_SECONDS = 300


def _hash_prefix(stream, size):
    stream.seek(0)
    digest = hashlib.sha256()
    while size:
        chunk = stream.read(min(size, 1024 * 1024))
        if not chunk:
            raise OSError("Log was truncated while verifying its index checkpoint")
        digest.update(chunk)
        size -= len(chunk)
    return digest


def _checkpoint_matches_file(checkpoint, stat, include_debug):
    if not (
        isinstance(checkpoint, dict)
        and checkpoint.get("version") == _CHECKPOINT_VERSION
        and checkpoint.get("file_id") == [stat.st_dev, stat.st_ino]
        and checkpoint.get("include_debug") == bool(include_debug)
        and isinstance(checkpoint.get("offset"), int)
        and 0 <= checkpoint["offset"] <= stat.st_size
        and isinstance(checkpoint.get("meta"), dict)
        and type(checkpoint.get("line_no")) is int
        and checkpoint["line_no"] >= 0
        and isinstance(checkpoint.get("prev_ts"), str)
        and isinstance(checkpoint.get("prev_cat"), str)
    ):
        return False
    try:
        meta = _RoundMetaAccumulator(**checkpoint["meta"])
        return (
            meta.total_lines == checkpoint["line_no"] and isinstance(meta.category_counts, dict)
            and all(type(value) is int and value >= 0 for value in meta.category_counts.values())
        )
    except TypeError:
        return False


def _checkpoint_is_current(checkpoint, stat, include_debug, allow_partial=False):
    return (
        _checkpoint_matches_file(checkpoint, stat, include_debug)
        and checkpoint.get("observed_size") == stat.st_size
        and checkpoint.get("mtime_ns") == stat.st_mtime_ns
        and (not allow_partial or checkpoint["offset"] == stat.st_size)
        # Windows path stat and descriptor stat can expose different ctime semantics.
        and (os.name == "nt" or checkpoint.get("ctime_ns") == stat.st_ctime_ns)
    )


def _read_checkpoint_digest(stream, checkpoint, stat, include_debug):
    if not _checkpoint_matches_file(checkpoint, stat, include_debug):
        return None
    offset = checkpoint["offset"]
    if 0 < offset < stat.st_size:
        stream.seek(offset - 1)
        if stream.read(1) != b"\n":
            return None
    digest = _hash_prefix(stream, offset)
    return digest if digest.hexdigest() == checkpoint.get("prefix_sha256") else None


def _file_version(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _confirm_file_prefix(path, stream, initial, size, digest, terminated):
    current = os.fstat(stream.fileno())
    if not os.path.samestat(path.stat(), current) or current.st_size < initial.st_size:
        raise OSError("Log was rotated or truncated during indexing")
    if not terminated and current.st_size != initial.st_size:
        raise OSError("The unterminated log line changed during indexing")
    if _file_version(current) != _file_version(initial) and _hash_prefix(stream, size).digest() != digest.digest():
        raise OSError("Log contents changed during indexing")


def _write_checkpoint(conn, uid, generation, cp):
    conn.execute(
        "INSERT INTO log_index_checkpoints VALUES (?,?,?) ON CONFLICT(round_uid,generation) DO UPDATE SET checkpoint_json=excluded.checkpoint_json",
        (uid, generation, json.dumps(cp, ensure_ascii=False)),
    )


def _publish_meta(conn, item, generation, cp):
    meta = cp["meta"]
    date = item["round_date"]
    if not item.get("origin_round_id") and not item.get("date_from_path") and meta["started_at"]:
        date = meta["started_at"][:10]
    conn.execute(
        """UPDATE log_rounds SET active_generation=?,line_count=?,gameplay_line_count=?,
        category_counts_json=?,started_at=?,ended_at=?,round_id=?,round_date=?,
        indexed_mtime=?,indexed_size=?,indexed_at=?,includes_debug=?,map_revision=?,index_error=NULL WHERE round_uid=?""",
        (generation, meta["total_lines"], meta["gameplay_lines"], json.dumps(meta["category_counts"]),
         meta["started_at"], meta["ended_at"], item.get("origin_round_id") or meta["round_id"] or item["round_id"], date,
         cp["mtime_ns"] / 1e9, cp["offset"], time.time(), int(cp["include_debug"]),
         item.get("map_revision") or meta.get("map_revision", ""), item["round_uid"]),
    )


def _record_job_round(conn, job_id, uid, generation, indexed_lines):
    if job_id is None:
        return
    conn.execute(
        """INSERT INTO log_job_rounds VALUES (?,?,?,?) ON CONFLICT(job_id,round_uid)
           DO UPDATE SET generation=excluded.generation,indexed_lines=excluded.indexed_lines""",
        (job_id, uid, generation, indexed_lines),
    )
    conn.execute(
        """UPDATE log_jobs SET progress='Готово раундов: ' ||
           (SELECT COUNT(*) FROM log_job_rounds WHERE job_id=?) WHERE id=? AND state='running'""",
        (job_id, job_id),
    )


def _index_file(conn, item, old, *, include_debug, force_reindex, job_id=None):
    from core.log_parser.filters import _decode_log_line, parse_raw_line
    uid = item["round_uid"]
    path = Path(item["dir_path"]) / "game.log"
    if not item["has_game_log"]:
        stale = old.get("has_game_log") or old.get("active_generation") or old.get("line_count")
        unchanged = (
            old.get("indexed_at") and not stale and not old.get("index_error")
            and old["round_date"] == item["round_date"] and old["round_id"] == item["round_id"]
            and old.get("map_revision") == item.get("map_revision", "")
        )
        if unchanged:
            if force_reindex and job_id is not None:
                with conn:
                    _record_job_round(conn, job_id, uid, 0, 0)
            return 0, True
        with conn:
            conn.execute("DELETE FROM log_entries WHERE round_uid=?", (uid,))
            conn.execute("DELETE FROM log_index_checkpoints WHERE round_uid=?", (uid,))
            conn.execute(
                """UPDATE log_rounds SET active_generation=0,line_count=0,gameplay_line_count=0,
                   category_counts_json='{}',started_at=NULL,ended_at=NULL,round_id=?,round_date=?,
                   indexed_mtime=NULL,indexed_size=NULL,indexed_at=?,includes_debug=0,map_revision=?,index_error=NULL
                   WHERE round_uid=?""",
                (item["round_id"], item["round_date"], time.time(), item.get("map_revision", ""), uid),
            )
            _record_job_round(conn, job_id, uid, 0, 0)
        return 0, False
    active = int(old.get("active_generation", 0))
    checkpoints = {}
    for generation, raw_checkpoint in conn.execute(
        "SELECT generation,checkpoint_json FROM log_index_checkpoints WHERE round_uid=?", (uid,),
    ):
        try:
            checkpoints[generation] = json.loads(raw_checkpoint)
        except ValueError:
            checkpoints[generation] = None
    cp = checkpoints.get(active)
    pending = max((gen for gen in checkpoints if gen > active), default=None)
    discovered_stat = item.get("game_log_stat")
    def accepts_partial(stat):
        return item["source_type"] == "upload" or time.time() - stat.st_mtime >= _DISK_TAIL_SETTLE_SECONDS

    if (pending is None and not force_reindex and discovered_stat is not None
            and _checkpoint_is_current(cp, discovered_stat, include_debug, accepts_partial(discovered_stat))):
        _finish_unchanged_file(conn, old, uid, active, checkpoints)
        return 0, True
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        limit = stat.st_size
        allow_partial = accepts_partial(stat)
        rebuilding = True
        if pending is not None and (digest := _read_checkpoint_digest(stream, checkpoints[pending], stat, include_debug)):
            generation, cp = pending, checkpoints[pending]
        elif not force_reindex and _checkpoint_matches_file(cp, stat, include_debug):
            if _checkpoint_is_current(cp, stat, include_debug, allow_partial):
                _finish_unchanged_file(conn, old, uid, active, checkpoints)
                return 0, True
            digest = _read_checkpoint_digest(stream, cp, stat, include_debug)
            if digest is not None:
                generation = active
                rebuilding = False
        else:
            digest = None
        if digest is None:
            generation = max([active, *checkpoints.keys()]) + 1
            digest = hashlib.sha256()
            cp = {
                "version": _CHECKPOINT_VERSION,
                "file_id": [stat.st_dev, stat.st_ino], "include_debug": bool(include_debug),
                "offset": 0, "line_no": 0, "prev_ts": "", "prev_cat": "SYSTEM",
                "meta": {"total_lines": 0, "gameplay_lines": 0, "category_counts": {},
                         "round_id": None, "started_at": None, "ended_at": None},
            }
        cp["mtime_ns"] = stat.st_mtime_ns
        cp["ctime_ns"] = stat.st_ctime_ns
        cp["observed_size"] = limit
        stream.seek(cp["offset"])
        accumulator = _RoundMetaAccumulator(**cp["meta"])
        rows = []
        lines_in_batch = 0
        added = 0

        def flush():
            nonlocal rows, lines_in_batch
            cp["prefix_sha256"] = digest.hexdigest()
            cp["meta"] = asdict(accumulator)
            with conn if rebuilding else nullcontext():
                if rows:
                    conn.executemany(
                        """INSERT INTO log_entries (round_uid,file_name,line_no,timestamp,category,
                        ckeys_space,message,coord_x,coord_y,coord_z,generation,byte_offset,byte_length,is_continuation)
                        VALUES (?,'game.log',?,?,?,?,?,?,?,?,?,?,?,?)""", rows,
                    )
                _write_checkpoint(conn, uid, generation, cp)
            rows = []
            lines_in_batch = 0

        def index_next_line():
            nonlocal lines_in_batch, added
            offset = stream.tell()
            raw = stream.readline(limit - offset)
            if not raw:
                return False
            terminated = raw.endswith(b"\n")
            if not terminated and not allow_partial:
                return False
            cp["last_line_terminated"] = terminated
            digest.update(raw)
            cp["line_no"] += 1
            decoded = _decode_log_line(raw)
            if cp["line_no"] == 1:
                decoded = decoded.lstrip("\ufeff")
            ts, cat, msg, continuation = parse_raw_line(cp["line_no"], decoded, cp["prev_ts"], cp["prev_cat"])
            if not continuation:
                cp["prev_ts"], cp["prev_cat"] = ts, cat
            accumulator.observe(ts, cat, msg, continuation)
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
            return True

        # Keep unverified append rows invisible. A rebuild can commit batches in its pending generation.
        with nullcontext() if rebuilding else conn:
            while stream.tell() < limit and index_next_line():
                pass
            flush()
            _confirm_file_prefix(path, stream, stat, cp["offset"], digest, cp.get("last_line_terminated", True))
            with conn if rebuilding else nullcontext():
                _publish_meta(conn, item, generation, cp)
                _record_job_round(
                    conn, job_id, uid, generation,
                    accumulator.total_lines - (0 if include_debug else accumulator.category_counts.get("DEBUG", 0)),
                )
        _collect_other_generations(conn, uid, generation)
        return added, False


def _finish_unchanged_file(conn, old, uid, generation, checkpoints):
    if old.get("index_error"):
        with conn:
            conn.execute("UPDATE log_rounds SET index_error=NULL WHERE round_uid=?", (uid,))
    if any(gen != generation for gen in checkpoints):
        _collect_other_generations(conn, uid, generation)


def _collect_other_generations(conn, uid, generation):
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


def _sync_rounds_conn(
    conn, root_dir, uploads_dir, *, include_debug=False, force_reindex=False, upload_dirs=None,
    use_saved_debug=False, persist_debug=False, import_batch=None, job_id=None, source_scope=None,
):
    from core.logs_db.uploads import (
        _collect_abandoned_staging, _recover_upload_batches, reconcile_deleted_uploads, remove_rediscovered_uploads,
    )

    started = time.monotonic()
    now = time.time()
    conn.row_factory = sqlite3.Row
    if persist_debug:
        with conn:
            conn.execute(
                """INSERT INTO log_maintenance_state VALUES ('index_include_debug',?)
                   ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json""",
                (json.dumps(bool(include_debug)),),
            )
    elif use_saved_debug:
        setting = conn.execute(
            "SELECT value_json FROM log_maintenance_state WHERE name='index_include_debug'"
        ).fetchone()
        if setting is not None:
            include_debug = json.loads(setting[0])
    if upload_dirs is None:
        cleanup_errors = []
        sources = {}
        if source_scope != "upload":
            sources["disk"] = _discover_dirs_with_logs(root_dir, "disk")
        if source_scope != "disk":
            cleanup_errors.extend(reconcile_deleted_uploads(conn, uploads_dir))
            cleanup_errors.extend(_collect_abandoned_staging(uploads_dir))
            cleanup_errors.extend(_recover_upload_batches(conn, uploads_dir, include_debug))
            state = _discover_dirs_with_logs(uploads_dir, "upload")
            sources["upload"] = state
            if uploads_dir is not None:
                cleanup_errors.extend(remove_rediscovered_uploads(conn, uploads_dir, state))
            unpublished = {row[0] for row in conn.execute("SELECT id FROM log_upload_batches")}
            state[:] = [item for item in state if Path(item["dir_path"]).parent.name not in unpublished]
        params = () if source_scope is None else (source_scope,)
        source_filter = " AND source_type=?" if params else ""
        existing = {
            row["round_uid"]: dict(row)
            for row in conn.execute(f"SELECT * FROM log_rounds WHERE import_batch IS NULL{source_filter}", params)
        }
    else:
        cleanup_errors = []
        found = _discover_dirs_with_logs(uploads_dir, "upload", selected=upload_dirs)
        sources = {"upload": found}
        existing = {}
        for item in found:
            row = conn.execute("SELECT * FROM log_rounds WHERE round_uid=?", (item["round_uid"],)).fetchone()
            if row:
                existing[row["round_uid"]] = dict(row)
    for source, items in sources.items():
        duplicates = {uid for uid, count in Counter(item["round_uid"] for item in items).items() if count > 1}
        if duplicates:
            sources[source] = DiscoveryResult(
                (item for item in items if item["round_uid"] not in duplicates),
                complete=False,
                errors=[*getattr(items, "errors", []), f"Duplicate round identifiers: {len(duplicates)}"],
            )
    summary = dict(discovered=sum(map(len, sources.values())), indexed_rounds=0, indexed_lines=0,
                   skipped_unchanged=0, removed_stale=0, errors=0)
    completed = dict(conn.execute(
        "SELECT round_uid,generation FROM log_job_rounds WHERE job_id=?", (job_id,),
    )) if job_id else {}
    for source, found in sources.items():
        root = root_dir if source == "disk" else uploads_dir
        complete = getattr(found, "complete", True)
        errors = [*getattr(found, "errors", []), *(cleanup_errors if source == "upload" else [])]
        summary["errors"] += int(not complete) + (len(cleanup_errors) if source == "upload" else 0)
        found_uids = {item["round_uid"] for item in found}
        with conn:
            if upload_dirs is None:
                backlog = sum(
                    max(0, item["game_log_size"] - (existing.get(item["round_uid"], {}).get("indexed_size") or 0))
                    for item in found
                )
                conn.execute(
                    """INSERT INTO log_sources(source,status,last_scan_at,error,pending_bytes) VALUES (?,?,?,?,?)
                       ON CONFLICT(source) DO UPDATE SET status=excluded.status,last_scan_at=excluded.last_scan_at,
                       error=excluded.error,pending_bytes=excluded.pending_bytes""",
                    (source, "disabled" if root is None else ("indexing" if complete else "degraded"), now,
                     "; ".join(errors)[:500] or None, backlog),
                )
            for uid, old in existing.items():
                if old["source_type"] != source or root is None or not complete:
                    continue
                if uid in found_uids and old.get("missing_since") is not None:
                    conn.execute("UPDATE log_rounds SET missing_since=NULL WHERE round_uid=?", (uid,))
                elif uid in found_uids:
                    continue
                elif old.get("missing_since") is not None and now - old["missing_since"] >= 60:
                    conn.execute("DELETE FROM log_rounds WHERE round_uid=?", (uid,))
                    summary["removed_stale"] += 1
                elif old.get("missing_since") is None:
                    conn.execute("UPDATE log_rounds SET missing_since=? WHERE round_uid=?", (now, uid))
        pending_bytes = 0
        source_failed = not complete or bool(errors)
        for item in found:
            uid = item["round_uid"]
            old = existing.get(uid, {})
            label, uploaded_by, uploaded_at = old.get("label", ""), old.get("uploaded_by"), old.get("uploaded_at")
            metadata = Path(item["dir_path"]) / ".hilleye_meta.json"
            if source == "upload" and metadata.is_file():
                try:
                    with metadata.open("rb") as stream:
                        raw = stream.read(16385)
                    if len(raw) > 16384:
                        raise ValueError("Upload metadata is too large")
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("Upload metadata must be a JSON object")
                    if isinstance(data.get("label"), str):
                        label = label or data["label"][:100]
                    if isinstance(data.get("uploaded_by"), str):
                        uploaded_by = uploaded_by or data["uploaded_by"][:24]
                    if type(data.get("uploaded_at")) in (int, float) and 0 <= data["uploaded_at"] <= 253402300799:
                        uploaded_at = uploaded_at or data["uploaded_at"]
                    revision = data.get("map_revision")
                    if isinstance(revision, str) and _MAP_REVISION.fullmatch(revision):
                        item["map_revision"] = revision.lower()
                    origin_date, origin_id = data.get("origin_round_date"), data.get("origin_round_id")
                    if isinstance(origin_date, str) and isinstance(origin_id, str) and len(origin_id) <= 128:
                        if date.fromisoformat(origin_date).isoformat() == origin_date and origin_id:
                            item.update(round_date=origin_date, round_id=origin_id, origin_round_id=origin_id)
                except (ValueError, OSError) as exc:
                    logger.warning("Invalid log metadata for %s: %s", uid, exc)
                    source_failed = True
                    summary["errors"] += 1
            with conn:
                base_fields = dict(dir_path=item["dir_path"], label=label, uploaded_by=uploaded_by,
                                   uploaded_at=uploaded_at, **{key: item[key] for key in _FILE_FIELDS})
                if item.get("map_revision"):
                    base_fields["map_revision"] = item["map_revision"]
                if not old:
                    fields = dict(round_uid=uid, source_type=source, round_id=item["round_id"],
                                  round_date=item["round_date"], import_batch=import_batch, **base_fields)
                    conn.execute(
                        f"INSERT INTO log_rounds({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
                        tuple(fields.values()),
                    )
                else:
                    changed = {key: value for key, value in base_fields.items() if old[key] != value}
                    if changed:
                        conn.execute(
                            f"UPDATE log_rounds SET {','.join(key + '=?' for key in changed)} WHERE round_uid=?",
                            (*changed.values(), uid),
                        )
            try:
                resumed = uid in completed and completed[uid] == old.get("active_generation")
                added, unchanged = _index_file(
                    conn, item, old, include_debug=include_debug,
                    force_reindex=force_reindex and not resumed, job_id=job_id,
                )
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
        if upload_dirs is None:
            with conn:
                conn.execute("""UPDATE log_sources SET pending_bytes=?,status=?,
                             last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END WHERE source=?""",
                             (pending_bytes, "disabled" if root is None else ("degraded" if source_failed else "ok"),
                               int(root is not None and not source_failed), time.time(), source))
    if job_id is not None:
        summary["indexed_rounds"], summary["indexed_lines"] = conn.execute(
            """SELECT COUNT(*),COALESCE(SUM(p.indexed_lines),0)
               FROM log_job_rounds p JOIN log_rounds r ON r.round_uid=p.round_uid
               WHERE p.job_id=? AND r.active_generation=p.generation""", (job_id,),
        ).fetchone()
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return summary


async def sync_and_index_rounds(
    root_dir, uploads_dir, *, include_debug=False, force_reindex=False, upload_dirs=None,
    use_saved_debug=False, persist_debug=False, job_id=None,
):
    import core.logs_db as _ldb
    from core.file_lock import lease, lock_path
    started = time.monotonic()
    path = _ldb._current_logs_db_path
    async with _ldb._sync_lock:
        if upload_dirs is not None:
            async with lease(lock_path(path, "uploads")):
                return await run_logs_transaction(
                    _sync_rounds_conn, root_dir, uploads_dir, include_debug=include_debug,
                    force_reindex=force_reindex, upload_dirs=upload_dirs,
                    use_saved_debug=use_saved_debug, persist_debug=persist_debug,
                    job_id=job_id, source_scope="upload",
                )
        async with lease(lock_path(path, "index-write")):
            disk = await run_logs_transaction(
                _sync_rounds_conn, root_dir, None, include_debug=include_debug, force_reindex=force_reindex,
                use_saved_debug=use_saved_debug, persist_debug=persist_debug, job_id=job_id, source_scope="disk",
            )
        async with lease(lock_path(path, "uploads")):
            uploads = await run_logs_transaction(
                _sync_rounds_conn, None, uploads_dir, include_debug=include_debug, force_reindex=force_reindex,
                use_saved_debug=use_saved_debug or persist_debug, job_id=job_id, source_scope="upload",
            )
    summary = {key: disk[key] + uploads[key] for key in disk if key != "elapsed_seconds"}
    if job_id is not None:
        summary.update(indexed_rounds=uploads["indexed_rounds"], indexed_lines=uploads["indexed_lines"])
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return summary
