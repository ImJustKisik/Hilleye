"""Manual log upload and archive ingestion (Zip-Slip & Zip-Bomb protected)."""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import struct
import tarfile
from typing import Optional
import time
import uuid
import zipfile
from core.config import MAX_LOG_UPLOAD_MB
from core.file_lock import FileLock, lease, lock_path
from core.log_operations import finish_thread, staging_directory, sync_directory
from core.log_parser import decode_log_bytes
from core.logs_db.connection import ALLOWED_LOG_FILES, run_logs_transaction
from core.logs_db.indexer import _slug, _sync_rounds_conn

logger = logging.getLogger(__name__)
_DELETED_UPLOADS = ".hilleye-deleted"
_STAGING_MAX_AGE = 86400


class UploadPendingError(RuntimeError):
    """All source files are durable and will be indexed on a later scan."""


class UploadQuotaError(RuntimeError):
    """The upload would exceed the configured storage limit."""


class _ArchiveStream:
    def __init__(self, stream, max_bytes):
        self.stream = stream
        self.remaining = max_bytes

    def read(self, size):
        chunk = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise ValueError("Распакованный TAR превышает допустимый лимит.")
        return chunk


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
    if normalized.startswith("/"):
        raise ValueError("Абсолютные пути в архиве запрещены.")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p or "\x00" in p for p in parts):
        raise ValueError(f"Небезопасный путь в архиве: {member_path}")
    if len(parts) == 1:
        return ""
    return "/".join(parts[:-1])


def _upload_fingerprint(folder, identity):
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8"))
    for name in sorted(ALLOWED_LOG_FILES):
        output = folder / name
        if output.is_file():
            if output.is_symlink():
                raise ValueError("Log files must not be symbolic links")
            with output.open("rb") as stream:
                file_hash = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    file_hash.update(chunk)
            digest.update(name.encode("ascii") + b"\0" + file_hash.digest())
    return digest.hexdigest()


def _prepare_upload(filename, source, staging, *, label, uploaded_by, max_extracted_bytes, max_archive_files,
                    origin_path=None, preserve_groups=False):
    """Extract into private staging directories using bounded chunks, never archive bytes."""
    groups = {}
    total = count = 0

    def copy_member(name, stream, size):
        nonlocal total, count
        group = _safe_rel_group(name)
        count += 1
        if count > max_archive_files or size > max_extracted_bytes:
            raise ValueError("Превышен лимит числа файлов или распакованного размера.")
        head = stream.read(4096)
        target = _classify_uploaded_filename(name, head)
        total += len(head)
        output = None
        try:
            if target:
                if group not in groups:
                    folder = staging / f"round-{len(groups)}"
                    folder.mkdir()
                    groups[group] = folder
                output_path = groups[group] / target
                if output_path.exists():
                    raise ValueError("Несколько файлов архива соответствуют одному логу раунда.")
                output = output_path.open("wb")
                output.write(head)
            while True:
                if total > max_extracted_bytes:
                    raise ValueError("Распакованный объём архива превышает допустимый лимит.")
                chunk = stream.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if output:
                    output.write(chunk)
            if output:
                output.flush()
                os.fsync(output.fileno())
        finally:
            if output:
                output.close()

    lower = filename.lower()
    max_members = max_archive_files * 2 + 100
    if lower.endswith(".zip"):
        # Reject huge central directories before ZipFile allocates an object per entry.
        with source.open("rb") as stream:
            stream.seek(max(0, source.stat().st_size - 65557))
            tail = stream.read(65557)
        marker = tail.rfind(b"PK\x05\x06")
        if marker >= 0 and len(tail) >= marker + 22:
            entries = struct.unpack_from("<H", tail, marker + 10)[0]
            directory_size = struct.unpack_from("<I", tail, marker + 12)[0]
            if entries > max_members or directory_size > max(max_members * 2048, 128 * 1024):
                raise ValueError("Слишком много записей в ZIP-архиве.")
        try:
            with zipfile.ZipFile(source) as archive:
                if len(archive.infolist()) > max_members:
                    raise ValueError("Слишком много записей в ZIP-архиве.")
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    if (member.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Символические ссылки в архивах запрещены.")
                    with archive.open(member) as stream:
                        copy_member(member.filename, stream, member.file_size)
        except (zipfile.BadZipFile, RuntimeError) as exc:
            raise ValueError("Повреждённый или зашифрованный ZIP-архив.") from exc
    elif lower.endswith((".tar.gz", ".tgz", ".tar")):
        opener = gzip.open if lower.endswith((".tar.gz", ".tgz")) else Path.open
        try:
            with opener(source, "rb") as compressed:
                bounded = _ArchiveStream(compressed, max_extracted_bytes + max_members * 4096 + 10240)
                with tarfile.open(fileobj=bounded, mode="r|") as archive:
                    for entry_number, member in enumerate(archive, 1):
                        if entry_number > max_members:
                            raise ValueError("Слишком много записей в TAR-архиве.")
                        if member.isdir():
                            continue
                        if not member.isfile():
                            raise ValueError("В архиве допускаются только обычные файлы.")
                        stream = archive.extractfile(member)
                        if stream:
                            with stream:
                                copy_member(member.name, stream, member.size)
        except (tarfile.TarError, gzip.BadGzipFile, EOFError) as exc:
            raise ValueError("Повреждённый TAR/TGZ архив.") from exc
    elif lower.endswith((".log", ".txt")):
        with source.open("rb") as stream:
            copy_member(Path(filename).name, stream, source.stat().st_size)
    else:
        raise ValueError("Допустимы только файлы .log, .txt, .zip, .tar.gz, .tgz.")
    if not groups:
        raise ValueError("Не найдено ни одного подходящего файла лога.")
    origin_meta = {}
    if origin_path:
        from core.celadon_logs import round_location

        day, round_folder = round_location(origin_path)
        origin_meta = {"origin": "celadon", "origin_path": origin_path}
        if len(groups) == 1 and day and 6 < len(round_folder) <= 134:
            origin_meta.update(origin_round_date=day, origin_round_id=round_folder[6:])
    prepared = []
    for group, folder in groups.items():
        hint = Path(group).name if group else Path(filename).stem
        uid = f"u-{uuid.uuid4().hex[:10]}-{_slug(hint.removeprefix('round-'))[:16]}"
        meta = dict(label=(label.strip() or Path(filename).name)[:100], uploaded_by=uploaded_by,
                    uploaded_at=time.time(), original_filename=Path(filename).name, **origin_meta)
        if preserve_groups:
            meta["origin_group"] = group
        with (folder / ".hilleye_meta.json").open("w", encoding="utf-8") as metadata:
            json.dump(meta, metadata, ensure_ascii=False)
            metadata.flush()
            os.fsync(metadata.fileno())
        sync_directory(folder)
        identity = [origin_path or "", group if preserve_groups or len(groups) > 1 else ""]
        prepared.append((uid, folder, _upload_fingerprint(folder, identity), identity))
    return prepared


async def ingest_uploaded_log(filename, raw_bytes, *, uploads_dir, label="", uploaded_by="",
                              max_upload_bytes=MAX_LOG_UPLOAD_MB*1024*1024, max_extracted_bytes=200*1024*1024,
                              max_archive_files=100, include_debug=False, origin_path=None, quota_bytes=0):
    from core import logs_db as _ldb
    resolved = uploads_dir.resolve()
    await finish_thread(resolved.mkdir, parents=True, exist_ok=True)
    async with staging_directory(prefix=".staging-", dir=resolved) as staging:
        async with lease(staging / ".lock"):
            if isinstance(raw_bytes, (str, Path)):
                source = Path(raw_bytes)
                size = source.stat().st_size
            else:
                size = len(raw_bytes)
                if size and size <= max_upload_bytes:
                    source = staging / "input"
                    await finish_thread(source.write_bytes, raw_bytes)
            if not size or size > max_upload_bytes:
                raise ValueError("Загруженный файл пуст или превышает максимальный размер.")
            prepared = await finish_thread(
                _prepare_upload, filename, source, staging, label=label, uploaded_by=uploaded_by,
                max_extracted_bytes=max_extracted_bytes, max_archive_files=max_archive_files, origin_path=origin_path,
            )
            async with lease(lock_path(_ldb._current_logs_db_path, "uploads")):
                uids = await run_logs_transaction(_publish_upload, resolved, staging, prepared, include_debug, quota_bytes)
                return [record for uid in uids if (record := await _ldb.get_round_by_uid(uid)) is not None]


def _stored_bytes(folder, *, skip_staging=False):
    def fail_scan(exc):
        raise exc

    total = 0
    for current, dirs, files in os.walk(folder, onerror=fail_scan):
        parent = Path(current)
        dirs[:] = [
            name for name in dirs if (parent / name).resolve() == parent / name
            and not (skip_staging and parent == folder and name.startswith(".staging-"))
        ]
        total += sum((parent / name).stat().st_size for name in files if not (parent / name).is_symlink())
    return total


def _publish_upload(conn, root, staging, prepared, include_debug, quota_bytes):
    _recover_upload_batches(conn, root, include_debug)
    pending = {
        fingerprint
        for (entries_json,) in conn.execute("SELECT entries_json FROM log_upload_batches")
        for _, fingerprint in json.loads(entries_json)
    }
    if any(fingerprint in pending for _, _, fingerprint, _ in prepared):
        raise UploadPendingError("Эти логи уже сохранены. Индексация ещё ожидает повторного запуска.")
    result = []
    entries = []
    batch = staging / "batch"
    batch.mkdir()
    for uid, folder, fingerprint, identity in prepared:
        previous = conn.execute(
            """SELECT r.round_uid,r.dir_path FROM log_upload_receipts p
               JOIN log_rounds r ON r.round_uid=p.round_uid
               WHERE p.fingerprint=? AND r.import_batch IS NULL AND r.missing_since IS NULL
               AND NOT EXISTS(SELECT 1 FROM log_celadon_aliases a WHERE a.old_uid=r.round_uid)
               AND NOT EXISTS(SELECT 1 FROM log_celadon_rounds c WHERE c.round_uid=r.round_uid AND NOT c.present)""",
            (fingerprint,),
        ).fetchone()
        if previous:
            path = Path(previous["dir_path"])
            if path.is_dir() and path.resolve().is_relative_to(root):
                result.append(previous["round_uid"])
                continue
        legacy_uid = _find_legacy_duplicate(conn, root, folder, fingerprint, identity)
        if legacy_uid and legacy_uid not in result:
            with conn:
                conn.execute(
                    """INSERT INTO log_upload_receipts VALUES (?,?)
                       ON CONFLICT(fingerprint) DO UPDATE SET round_uid=excluded.round_uid""",
                    (fingerprint, legacy_uid),
                )
            result.append(legacy_uid)
            continue
        folder.rename(batch / uid)
        entries.append((uid, fingerprint))
        result.append(uid)
    if not entries:
        return result
    if quota_bytes and _stored_bytes(root, skip_staging=True) + _stored_bytes(batch) > quota_bytes:
        raise UploadQuotaError("Хранилище загруженных логов достигло настроенного лимита.")
    sync_directory(batch)
    batch_id = f"batch-{uuid.uuid4().hex}"
    with conn:
        conn.execute("INSERT INTO log_upload_batches VALUES (?,?,?)", (batch_id, time.time(), json.dumps(entries)))
    batch.rename(root / batch_id)
    sync_directory(root)
    _index_upload_batch(conn, root, batch_id, entries, include_debug)
    return result


def _find_legacy_duplicate(conn, root, incoming, fingerprint, identity):
    flags = [(incoming / name).is_file() for name in ("game.log", "qdel.log", "initialize.log")]
    sizes = {
        name: (incoming / name).stat().st_size
        for name in ALLOWED_LOG_FILES if (incoming / name).is_file()
    }
    candidates = conn.execute(
        """SELECT r.round_uid,r.dir_path FROM log_rounds r
           WHERE source_type='upload' AND import_batch IS NULL AND missing_since IS NULL
             AND has_game_log=? AND has_qdel_log=? AND has_initialize_log=?
             AND game_log_size=? AND qdel_log_size=?
             AND NOT EXISTS(SELECT 1 FROM log_upload_receipts p WHERE p.round_uid=r.round_uid)
             AND NOT EXISTS(SELECT 1 FROM log_celadon_aliases a WHERE a.old_uid=r.round_uid)
             AND NOT EXISTS(SELECT 1 FROM log_celadon_rounds c WHERE c.round_uid=r.round_uid AND NOT c.present)
           ORDER BY uploaded_at, r.round_uid""",
        (*flags, sizes.get("game.log", 0), sizes.get("qdel.log", 0)),
    )
    for uid, stored in candidates:
        folder = Path(stored)
        try:
            if not folder.is_dir() or not folder.resolve().is_relative_to(root):
                continue
            if {name: (folder / name).stat().st_size for name in ALLOWED_LOG_FILES if (folder / name).is_file()} != sizes:
                continue
            metadata = folder / ".hilleye_meta.json"
            origin = ""
            if metadata.is_file() and not metadata.is_symlink():
                with metadata.open("rb") as stream:
                    info = json.loads(stream.read(16385))
                if isinstance(info, dict):
                    origin = info.get("origin_path", "")
            if origin == identity[0] and _upload_fingerprint(folder, identity) == fingerprint:
                return uid
        except (OSError, ValueError):
            continue
    return None


def _index_upload_batch(conn, root, batch_id, entries, include_debug):
    folder = root / batch_id
    summary = _sync_rounds_conn(
        conn, None, root, include_debug=include_debug, use_saved_debug=True,
        upload_dirs=[folder / uid for uid, _ in entries], import_batch=batch_id,
    )
    if summary["errors"] or summary["discovered"] != len(entries):
        raise UploadPendingError("Логи сохранены. Индексация будет повторена при следующем сканировании.")
    with conn:
        conn.executemany(
            """INSERT INTO log_upload_receipts VALUES (?,?)
               ON CONFLICT(fingerprint) DO UPDATE SET round_uid=excluded.round_uid""",
            [(fingerprint, uid) for uid, fingerprint in entries],
        )
        conn.execute("UPDATE log_rounds SET import_batch=NULL WHERE import_batch=?", (batch_id,))
        conn.execute("DELETE FROM log_upload_batches WHERE id=?", (batch_id,))


def _recover_upload_batches(conn, root, include_debug):
    if root is None or not root.is_dir():
        return []
    errors = []
    batches = conn.execute("SELECT id,entries_json FROM log_upload_batches ORDER BY created_at").fetchall()
    for batch_id, entries_json in batches:
        try:
            if not _is_batch_name(batch_id):
                raise ValueError("Invalid upload batch identifier")
            folder = root / batch_id
            if folder.is_symlink():
                raise ValueError("Upload batch directory must not be a symbolic link")
            if not folder.is_dir():
                # The writer had not completed the directory rename.
                with conn:
                    conn.execute("DELETE FROM log_rounds WHERE import_batch=?", (batch_id,))
                    conn.execute("DELETE FROM log_upload_batches WHERE id=?", (batch_id,))
                continue
            _index_upload_batch(conn, root, batch_id, json.loads(entries_json), include_debug)
        except (UploadPendingError, OSError, ValueError) as exc:
            errors.append(f"{batch_id}: {exc}")
    return errors


def _is_batch_name(name):
    return name.startswith("batch-") and len(name) == 38 and all(char in "0123456789abcdef" for char in name[6:])


def _collect_abandoned_staging(root):
    if root is None or not root.is_dir():
        return []
    root = root.resolve()
    errors = []
    for folder in root.glob(".staging-*"):
        try:
            if (folder.resolve().parent != root or folder.is_symlink() or not folder.is_dir()
                    or time.time() - folder.stat().st_mtime < _STAGING_MAX_AGE):
                continue
            lock = FileLock(folder / ".lock")
            if lock.acquire():
                lock.release()
                shutil.rmtree(folder)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(str(exc))
    return errors


def reconcile_deleted_uploads(conn, root):
    """Complete tombstoned deletions under the uploads lock."""
    if root is None or not root.is_dir():
        return []
    root = root.resolve()
    deleted = root / _DELETED_UPLOADS
    if deleted.resolve() != deleted or deleted.is_symlink():
        return ["Invalid upload deletion directory"]
    try:
        entries = list(deleted.iterdir()) if deleted.is_dir() else []
    except OSError as exc:
        return [str(exc)]
    errors = []
    for entry in entries:
        if entry.resolve() != entry or entry.is_symlink() or not entry.name.startswith("u-") or not entry.is_dir():
            continue
        try:
            # Compatibility with quarantines created by the previous deletion flow.
            with conn:
                conn.execute("DELETE FROM log_rounds WHERE round_uid=? AND source_type='upload'", (entry.name,))
            shutil.rmtree(entry)
        except OSError as exc:
            errors.append(str(exc))
    return errors


def remove_rediscovered_uploads(conn, root, discovered):
    tombstones = dict(conn.execute("SELECT round_uid,relative_path FROM log_upload_deletions"))
    errors = []
    for item in discovered:
        uid = item["round_uid"]
        if uid in tombstones:
            try:
                _clean_deleted_round(root.resolve(), uid, tombstones[uid])
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
    discovered[:] = [item for item in discovered if item["round_uid"] not in tombstones]
    return errors


def _checked_upload_path(root, path):
    target = path.resolve()
    if path.is_symlink() or target == root or not target.is_relative_to(root):
        raise ValueError("Небезопасный путь удаления.")
    if target.exists() and not target.is_dir():
        raise ValueError("Путь раунда не является каталогом.")
    return target


def _clean_deleted_round(root, uid, relative_path):
    if not uid.startswith("u-") or Path(uid).name != uid:
        raise ValueError("Недопустимый идентификатор загруженного раунда.")
    target = _checked_upload_path(root, root / relative_path)
    deleted = root / _DELETED_UPLOADS
    quarantine = deleted / uid
    if deleted.resolve() != deleted or quarantine.resolve() != quarantine:
        raise ValueError("Небезопасный каталог для удаления.")
    if quarantine.is_dir():
        shutil.rmtree(quarantine)
    if target.is_dir():
        deleted.mkdir(exist_ok=True)
        target.rename(quarantine)
        shutil.rmtree(quarantine)
    parent = target.parent
    if parent.parent == root and _is_batch_name(parent.name) and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


def _delete_upload(conn, round_uid, root):
    if conn.execute("SELECT 1 FROM log_celadon_aliases WHERE old_uid=?", (round_uid,)).fetchone():
        return False
    row = conn.execute("SELECT source_type,dir_path FROM log_rounds WHERE round_uid=?", (round_uid,)).fetchone()
    if row is None:
        return False
    if row[0] != "upload":
        raise ValueError("Удалять можно только загруженные вручную логи.")
    target = _checked_upload_path(root, Path(row[1]))
    relative_path = target.relative_to(root).as_posix()
    with conn:
        conn.execute("INSERT OR IGNORE INTO log_upload_deletions VALUES (?,?,?)",
                     (round_uid, relative_path, time.time()))
        conn.execute("DELETE FROM log_rounds WHERE round_uid=? AND source_type='upload'", (round_uid,))
    try:
        _clean_deleted_round(root, round_uid, relative_path)
    except (OSError, ValueError):
        logger.warning("Upload %s removed from the catalog; disk cleanup will be retried", round_uid, exc_info=True)
    return True


async def delete_uploaded_round(round_uid: str, *, uploads_dir: Path) -> bool:
    from core import logs_db as _ldb

    async with lease(lock_path(_ldb._current_logs_db_path, "uploads")):
        return await run_logs_transaction(_delete_upload, round_uid, uploads_dir.resolve())
