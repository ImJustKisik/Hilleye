"""Celadon source bindings and publication of downloaded log revisions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
import uuid

from fastapi import HTTPException

from core import celadon_logs
from core.file_lock import lease, lock_path
from core.log_operations import finish_thread, heavy_slot, staging_directory, sync_directory
from core.logs_db.connection import ALLOWED_LOG_FILES, run_logs_transaction
from core.logs_db.indexer import _sync_rounds_conn
from core.logs_db.uploads import (
    UploadPendingError, UploadQuotaError, _checked_upload_path, _is_batch_name, _prepare_upload,
    _stored_bytes, _upload_fingerprint,
)

logger = logging.getLogger(__name__)
_READ_GRACE = 120
_LEGACY_MIGRATION = "celadon_bind_legacy_uploads"
_FILE_FLAGS = {
    "game.log": "has_game_log",
    "qdel.log": "has_qdel_log",
    "initialize.log": "has_initialize_log",
}


@dataclass(frozen=True)
class ImportResult:
    status: str
    rounds: list[dict]


def local_file_name(path):
    name = path.rsplit("/", 1)[-1]
    return name if name in ALLOWED_LOG_FILES else None


def _origin_metadata(directory):
    metadata = directory / ".hilleye_meta.json"
    if metadata.is_symlink():
        return {}
    try:
        with metadata.open("rb") as stream:
            raw = stream.read(16385)
        data = json.loads(raw) if len(raw) <= 16384 else None
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _adopt_legacy(conn, root, path=None):
    statement = """SELECT r.* FROM log_rounds r
                   WHERE source_type='upload' AND import_batch IS NULL AND missing_since IS NULL
                     AND indexed_at IS NOT NULL AND index_error IS NULL
                     AND NOT EXISTS (SELECT 1 FROM log_celadon_rounds WHERE round_uid=r.round_uid)
                     AND NOT EXISTS (SELECT 1 FROM log_celadon_aliases WHERE old_uid=r.round_uid)"""
    args = ()
    if path is not None:
        day, folder = celadon_logs.round_location(path)
        statement += " AND (label=? OR (round_date=? AND round_id=?))"
        args = (path[:100], day, folder[6:])
    statement += " ORDER BY uploaded_at DESC, indexed_at DESC, round_uid DESC"

    with conn:
        for row in map(dict, conn.execute(statement, args).fetchall()):
            uid = row["round_uid"]
            directory = Path(row["dir_path"])
            try:
                if directory.resolve() != directory or not directory.is_relative_to(root):
                    continue
            except (OSError, RuntimeError):
                continue
            metadata = _origin_metadata(directory)
            origin = metadata.get("origin_path")
            if metadata.get("origin") != "celadon" or not isinstance(origin, str) or (path and path != origin):
                continue
            try:
                celadon_logs.validate_file_path(origin)
                if not _available(row, root):
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            group = metadata.get("origin_group")
            if not isinstance(group, str):
                group = "" if origin.lower().endswith((".log", ".txt")) else None
            day, folder = celadon_logs.round_location(origin)
            single = group is None and bool(folder) and metadata.get("origin_round_id") == folder[6:] and (
                metadata.get("origin_round_date") == day
            )
            # A legacy single-group archive has no recorded internal directory.
            canonical = conn.execute(
                """SELECT round_uid FROM log_celadon_rounds WHERE path=? AND
                   ((group_path=? AND present) OR (? AND legacy_single AND present))""",
                (origin, group, single),
            ).fetchone()
            if canonical:
                conn.execute("INSERT INTO log_celadon_aliases VALUES (?,?)", (uid, canonical[0]))
            else:
                conn.execute(
                    "INSERT INTO log_celadon_rounds(round_uid,path,group_path,legacy_single) VALUES (?,?,?,?)",
                    (uid, origin, group, single),
                )


def excluded_celadon_directories(conn, root):
    if root is None:
        return set()
    root = root.resolve()
    rows = conn.execute(
        """SELECT dir_path FROM log_rounds WHERE round_uid IN
           (SELECT old_uid FROM log_celadon_aliases UNION SELECT round_uid FROM log_celadon_rounds WHERE NOT present)"""
    )
    return {row[0] for row in rows} | {
        str(root / path) for (path,) in conn.execute("SELECT relative_path FROM log_celadon_cleanup")
    }


def _cleanup_celadon(conn, root):
    errors = []
    for (relative,) in conn.execute(
        "SELECT relative_path FROM log_celadon_cleanup WHERE remove_after<=?", (time.time(),),
    ).fetchall():
        try:
            target = _checked_upload_path(root, root / relative)
            if target != root / relative:
                raise ValueError("Invalid Celadon cleanup path")
            if conn.execute("SELECT 1 FROM log_rounds WHERE dir_path=?", (str(target),)).fetchone():
                raise ValueError("Celadon cleanup still references a live round")
            if target.is_dir():
                shutil.rmtree(target)
            parent = target.parent
            if parent.parent == root and _is_batch_name(parent.name) and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
            with conn:
                conn.execute("DELETE FROM log_celadon_cleanup WHERE relative_path=?", (relative,))
        except (OSError, ValueError) as exc:
            errors.append(f"Celadon {relative}: {exc}")
    return errors


def recover_celadon(conn, root):
    if root is None or not root.is_dir():
        return []
    root = root.resolve()
    errors = _cleanup_celadon(conn, root)
    if not conn.execute("SELECT 1 FROM log_maintenance_state WHERE name=?", (_LEGACY_MIGRATION,)).fetchone():
        _adopt_legacy(conn, root)
        if not conn.execute("SELECT 1 FROM log_upload_batches LIMIT 1").fetchone():
            with conn:
                conn.execute("INSERT INTO log_maintenance_state VALUES (?,'true')", (_LEGACY_MIGRATION,))
    return errors


async def migrate_celadon(config):
    async with lease(lock_path(config.resolved_logs_db_path(), "uploads")):
        errors = await run_logs_transaction(recover_celadon, config.resolved_logs_uploads_dir())
        for error in errors:
            logger.warning("%s", error)


def _managed_rounds(conn, path):
    return [dict(row) for row in conn.execute(
        """SELECT r.*, c.group_path, c.legacy_single, c.present FROM log_celadon_rounds c
           JOIN log_rounds r USING(round_uid) WHERE c.path=? AND r.import_batch IS NULL
           ORDER BY c.group_path, r.round_uid""", (path,),
    )]


def _disk_rounds(conn, path, root):
    from core.logs_db.queries import _visible_rounds, resolve_round_file_path

    day, folder = celadon_logs.round_location(path)
    filename = local_file_name(path)
    if root is None or not day or not folder or not filename:
        return []
    rows = conn.execute(
        f"""SELECT * FROM log_rounds WHERE source_type='disk' AND round_date=? AND round_id=?
            AND missing_since IS NULL AND indexed_at IS NOT NULL AND index_error IS NULL AND {_visible_rounds()}""",
        (day, folder[6:]),
    )
    return [
        row for row in map(dict, rows)
        if row[_FILE_FLAGS[filename]] and resolve_round_file_path(row, filename, root_dir=root, uploads_dir=None)
    ]


def catalog_file_states(conn, paths, disk_root):
    return {path: _managed_rounds(conn, path) or _disk_rounds(conn, path, disk_root) for path in paths}


def _lookup(conn, uploads_root, disk_root, path):
    for error in _cleanup_celadon(conn, uploads_root):
        logger.warning("%s", error)
    _adopt_legacy(conn, uploads_root, path)
    managed = _managed_rounds(conn, path)
    if not managed:
        with conn:
            conn.execute("DELETE FROM log_celadon_versions WHERE path=?", (path,))
        return managed, _disk_rounds(conn, path, disk_root), None
    version = conn.execute(
        "SELECT etag,modified,size,confirmed FROM log_celadon_versions WHERE path=?", (path,),
    ).fetchone()
    return managed, [], version


def _save_version(conn, path, version, confirmed):
    conn.execute(
        """INSERT INTO log_celadon_versions VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
           etag=excluded.etag, modified=excluded.modified, size=excluded.size, confirmed=excluded.confirmed""",
        (path, version.etag, version.modified, version.size, confirmed),
    )


def _observe_version(conn, path, version):
    with conn:
        if _managed_rounds(conn, path):
            _save_version(conn, path, version, False)


def _available(row, root):
    from core.logs_db.queries import resolve_round_file_path

    names = [name for name, flag in _FILE_FLAGS.items() if row[flag]]
    return bool(row["indexed_at"] and not row["index_error"] and names and all(
        resolve_round_file_path(row, name, root_dir=None, uploads_dir=root) for name in names
    ))


def _cached_version(path, rounds, saved):
    version = celadon_logs.FileVersion(*saved[:3]) if saved else celadon_logs.FileVersion()
    if version.size is not None or not path.lower().endswith((".log", ".txt")) or len(rounds) != 1:
        return version
    row = rounds[0]
    size = sum(row[key] for key in ("game_log_size", "qdel_log_size", "initialize_log_size"))
    return celadon_logs.FileVersion(version.etag, version.modified, size)


def _has_new_version_evidence(observed, saved):
    if saved is None:
        return bool(observed.etag or observed.modified or observed.size is not None)
    if saved[3]:
        return False
    if observed.etag and not saved[0]:
        return True
    return not saved[0] and (
        bool(observed.modified and observed.size is not None and not saved[1])
        or (observed.size is not None and saved[2] is None)
    )


def _publication(conn, root, staging, prepared, path, version, *, include_debug, quota_bytes, expected_uids):
    existing = _managed_rounds(conn, path)
    if {row["round_uid"] for row in existing} != expected_uids:
        raise HTTPException(409, "Состав сохранённых логов изменился. Откройте файл снова.")
    by_group = {row["group_path"]: row for row in existing}
    if None in by_group:
        if len(existing) != 1 or not existing[0]["legacy_single"] or len(prepared) != 1:
            _observe_version(conn, path, version)
            return ImportResult("unverified", [row for row in existing if row["present"] and _available(row, root)])
        by_group = {prepared[0][3][1]: existing[0]}

    batch = root / f"batch-{uuid.uuid4().hex}"
    staged_batch = staging / "batch"
    staged_batch.mkdir()
    changed = []
    active = []
    for new_uid, folder, fingerprint, identity in prepared:
        group = identity[1]
        previous = by_group.get(group)
        uid = previous["round_uid"] if previous else new_uid
        active.append((uid, group))
        if previous:
            old_dir = _checked_upload_path(root, Path(previous["dir_path"]))
            if _available(previous, root) and _upload_fingerprint(old_dir, identity) == fingerprint:
                continue
            metadata = _origin_metadata(folder)
            metadata.update(label=previous["label"], uploaded_by=previous["uploaded_by"], uploaded_at=previous["uploaded_at"])
            revision = _origin_metadata(old_dir).get("map_revision")
            if isinstance(revision, str):
                metadata["map_revision"] = revision
            with (folder / ".hilleye_meta.json").open("w", encoding="utf-8") as stream:
                json.dump(metadata, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            old_dir = None
        folder.rename(staged_batch / uid)
        sync_directory(staged_batch / uid)
        changed.append((uid, fingerprint, old_dir))

    if quota_bytes and changed:
        superseded = {old for _, _, old in changed if old and old.is_dir()}
        superseded.update(
            directory for (relative,) in conn.execute("SELECT relative_path FROM log_celadon_cleanup")
            if (directory := _checked_upload_path(root, root / relative)).is_dir()
        )
        stored = _stored_bytes(root, skip_staging=True) - sum(_stored_bytes(directory) for directory in superseded)
        if stored + _stored_bytes(staged_batch) > quota_bytes:
            raise UploadQuotaError("Хранилище загруженных логов достигло настроенного лимита.")

    paths = [batch / uid for uid, _, _ in changed]
    pending = [target.relative_to(root).as_posix() for target in paths]
    if changed:
        with conn:
            conn.executemany("INSERT INTO log_celadon_cleanup VALUES (?,0)", [(entry,) for entry in pending])
        sync_directory(staged_batch)
        staged_batch.rename(batch)
        sync_directory(root)

    with conn:
        conn.execute("BEGIN IMMEDIATE")
        if changed:
            try:
                summary = _sync_rounds_conn(
                    conn, None, root, include_debug=include_debug, upload_dirs=paths, use_saved_debug=True, atomic=True,
                )
            except Exception as exc:
                logger.exception("Failed to index a Celadon revision for %s", path)
                raise UploadPendingError("Не удалось проиндексировать обновление Celadon.") from exc
            if summary["errors"] or summary["discovered"] != len(changed):
                raise UploadPendingError("Не удалось проиндексировать обновление Celadon.")
            for uid, fingerprint, old in changed:
                conn.execute("DELETE FROM log_upload_receipts WHERE round_uid=?", (uid,))
                conn.execute(
                    """INSERT INTO log_upload_receipts VALUES (?,?)
                       ON CONFLICT(fingerprint) DO UPDATE SET round_uid=excluded.round_uid""",
                    (fingerprint, uid),
                )
                if old is not None:
                    conn.execute(
                        "INSERT INTO log_celadon_cleanup VALUES (?,?)",
                        (old.relative_to(root).as_posix(), time.time() + _READ_GRACE),
                    )
            conn.execute(
                "UPDATE log_rounds SET missing_since=NULL WHERE round_uid IN (" + ",".join("?" for _ in changed) + ")",
                [uid for uid, _, _ in changed],
            )
            conn.executemany("DELETE FROM log_celadon_cleanup WHERE relative_path=?", [(entry,) for entry in pending])
        conn.execute("UPDATE log_celadon_rounds SET present=0 WHERE path=?", (path,))
        conn.executemany(
            """INSERT INTO log_celadon_rounds(round_uid,path,group_path) VALUES (?,?,?)
               ON CONFLICT(round_uid) DO UPDATE SET group_path=excluded.group_path,present=1,legacy_single=0""",
            [(uid, path, group) for uid, group in active],
        )
        _save_version(conn, path, version, True)
    rounds = [row for row in _managed_rounds(conn, path) if row["present"]]
    if not existing:
        status = "imported"
    elif changed or {row["round_uid"] for row in existing if row["present"]} != {uid for uid, _ in active}:
        status = "updated"
    else:
        status = "cached"
    return ImportResult(status, rounds)


async def import_round(path, config, uploaded_by):
    celadon_logs.validate_file_path(path)
    db_path = config.resolved_logs_db_path()
    root = config.resolved_logs_uploads_dir().resolve()
    stripe = hashlib.sha256(path.encode()).hexdigest()[:2]
    async with lease(lock_path(db_path, f"celadon-{stripe}")):
        async with lease(lock_path(db_path, "uploads")):
            existing, disk, saved = await run_logs_transaction(
                _lookup, root, config.resolved_logs_root_dir(), path,
            )
        if disk:
            return ImportResult("disk" if len(disk) == 1 else "ambiguous", disk)
        present = [row for row in existing if row["present"]]
        ready = [row for row in present if _available(row, root)]
        if any(row["group_path"] is None for row in existing) and (
            len(existing) != 1 or not existing[0]["legacy_single"]
        ):
            if ready:
                return ImportResult("unverified", ready)
            raise HTTPException(409, "Не удалось сопоставить раунды из старого архива Celadon.")

        try:
            for attempt in range(2):
                expected = None
                if ready and len(ready) == len(present):
                    expected = await celadon_logs.file_version(path)
                    changed = expected.compare(_cached_version(path, ready, saved))
                    if not changed:
                        if _has_new_version_evidence(expected, saved):
                            async with lease(lock_path(db_path, "uploads")):
                                await run_logs_transaction(_observe_version, path, expected)
                        confirmed = changed is False and saved is not None and saved[3]
                        return ImportResult("cached" if confirmed else "unverified", ready)
                try:
                    async with heavy_slot(config):
                        await finish_thread(root.mkdir, parents=True, exist_ok=True)
                        async with staging_directory(prefix=".staging-celadon-", dir=root) as staging:
                            async with lease(staging / ".lock"):
                                source = staging / "input"
                                filename, version = await celadon_logs.download(
                                    path, source, max_mb=config.logs_max_upload_mb, expected=expected,
                                )
                                prepared = await finish_thread(
                                    _prepare_upload, filename, source, staging, label=path, uploaded_by=uploaded_by,
                                    max_extracted_bytes=config.logs_max_extracted_mb * 1024 * 1024,
                                    max_archive_files=config.logs_max_archive_files, origin_path=path, preserve_groups=True,
                                )
                                async with lease(lock_path(db_path, "uploads")):
                                    return await run_logs_transaction(
                                        _publication, root, staging, prepared, path, version,
                                        include_debug=config.logs_index_include_debug,
                                        quota_bytes=config.logs_upload_quota_mb * 1024 * 1024,
                                        expected_uids={row["round_uid"] for row in existing},
                                    )
                except celadon_logs.VersionChanged:
                    if attempt:
                        raise HTTPException(502, "Файл Celadon изменился во время скачивания.")
        except (HTTPException, UploadPendingError, UploadQuotaError, OSError, ValueError) as exc:
            if isinstance(exc, HTTPException) and exc.status_code == 409:
                raise
            if ready:
                logger.warning("Celadon refresh failed for %s: %s", path, exc)
                return ImportResult("unverified", ready)
            raise
