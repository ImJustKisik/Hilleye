"""Manual log upload and archive ingestion (Zip-Slip & Zip-Bomb protected)."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import shutil
import struct
import tarfile
import tempfile
import time
from typing import Any, Dict, List, Optional
import uuid
import zipfile

from core.log_parser import decode_log_bytes
from core.logs_db.connection import ALLOWED_LOG_FILES
from core.logs_db.indexer import _slug


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


def _prepare_upload(filename, source, staging, *, label, uploaded_by, max_extracted_bytes, max_archive_files):
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
        finally:
            if output:
                output.close()

    lower = filename.lower()
    if lower.endswith(".zip"):
        # Reject huge central directories before ZipFile allocates an object per entry.
        with source.open("rb") as stream:
            stream.seek(max(0, source.stat().st_size - 65557))
            tail = stream.read(65557)
        marker = tail.rfind(b"PK\x05\x06")
        if marker >= 0 and len(tail) >= marker + 22:
            entries = struct.unpack_from("<H", tail, marker + 10)[0]
            if entries > max_archive_files * 2 + 100:
                raise ValueError("Слишком много записей в ZIP-архиве.")
        try:
            with zipfile.ZipFile(source) as archive:
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
        try:
            with tarfile.open(source, mode="r|*") as archive:
                for member in archive:
                    if member.isdir():
                        continue
                    if not member.isfile():
                        raise ValueError("В архиве допускаются только обычные файлы.")
                    stream = archive.extractfile(member)
                    if stream:
                        with stream:
                            copy_member(member.name, stream, member.size)
        except tarfile.TarError as exc:
            raise ValueError("Повреждённый TAR/TGZ архив.") from exc
    elif lower.endswith((".log", ".txt")):
        with source.open("rb") as stream:
            copy_member(Path(filename).name, stream, source.stat().st_size)
    else:
        raise ValueError("Допустимы только файлы .log, .txt, .zip, .tar.gz, .tgz.")
    if not groups:
        raise ValueError("Не найдено ни одного подходящего файла лога.")
    prepared = []
    for group, folder in groups.items():
        hint = Path(group).name if group else Path(filename).stem
        uid = f"u-{uuid.uuid4().hex[:10]}-{_slug(hint.removeprefix('round-'))[:16]}"
        meta = dict(label=(label.strip() or Path(filename).name)[:100], uploaded_by=uploaded_by,
                    uploaded_at=time.time(), original_filename=Path(filename).name)
        (folder / ".hilleye_meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        prepared.append((uid, folder))
    return prepared


async def ingest_uploaded_log(filename, raw_bytes, *, uploads_dir, label="", uploaded_by="",
                              max_upload_bytes=50*1024*1024, max_extracted_bytes=200*1024*1024,
                              max_archive_files=100, include_debug=False):
    from core import logs_db as _ldb
    from core.file_lock import lease, lock_path
    from core.log_operations import finish_thread, staging_directory
    resolved = uploads_dir.resolve()
    await finish_thread(resolved.mkdir, parents=True, exist_ok=True)
    created = []
    async with staging_directory(prefix=".staging-", dir=resolved) as staging:
        if isinstance(raw_bytes, (str, Path)):
            source = Path(raw_bytes)
            size = source.stat().st_size
        else:
            size = len(raw_bytes)
            source = staging / "input"
            await finish_thread(source.write_bytes, raw_bytes)
        if not size or size > max_upload_bytes:
            raise ValueError("Загруженный файл пуст или превышает максимальный размер.")
        prepared = await finish_thread(
            _prepare_upload, filename, source, staging, label=label, uploaded_by=uploaded_by,
            max_extracted_bytes=max_extracted_bytes, max_archive_files=max_archive_files,
        )
        try:
            async with lease(lock_path(_ldb._current_logs_db_path, "uploads")):
                def publish():
                    for uid, folder in prepared:
                        destination = resolved / uid
                        folder.rename(destination)
                        created.append((uid, destination))
                await finish_thread(publish)
            await _ldb.sync_and_index_rounds(None, resolved, include_debug=include_debug)
            return [rec for uid, _ in created if (rec := await _ldb.get_round_by_uid(uid))]
        except BaseException:
            async with lease(lock_path(_ldb._current_logs_db_path, "uploads")):
                conn = await _ldb.get_logs_connection()
                for uid, folder in created:
                    await conn.execute("DELETE FROM log_rounds WHERE round_uid=?", (uid,))
                    await finish_thread(shutil.rmtree, folder, True)
                await conn.commit()
            raise


async def delete_uploaded_round(round_uid: str, *, uploads_dir: Path) -> bool:
    from core import logs_db as _ldb
    from core.file_lock import lease, lock_path
    from core.log_operations import finish_thread
    async with lease(lock_path(_ldb._current_logs_db_path, "uploads")):
        rec = await _ldb.get_round_by_uid(round_uid)
        if rec is None:
            return False
        if rec["source_type"] != "upload":
            raise ValueError("Удалять можно только загруженные вручную логи.")
        root = uploads_dir.resolve()
        target = Path(rec["dir_path"]).resolve()
        if target == root or not target.is_relative_to(root):
            raise ValueError("Небезопасный путь удаления.")
        conn = await _ldb.get_logs_connection()
        await conn.execute("DELETE FROM log_rounds WHERE round_uid=?", (round_uid,))
        await conn.commit()
        if target.is_dir():
            await finish_thread(shutil.rmtree, target, True)
        return True
