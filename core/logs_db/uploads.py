"""Manual log upload and archive ingestion (Zip-Slip & Zip-Bomb protected)."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import shutil
import tarfile
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
    import core.logs_db as _ldb

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

    await _ldb.sync_and_index_rounds(
        None,
        resolved_uploads,
        include_debug=include_debug,
        force_reindex=False,
    )
    # Return full DB records for created rounds
    results: List[Dict[str, Any]] = []
    for item in created_rounds:
        rec = await _ldb.get_round_by_uid(item["round_uid"])
        if rec:
            results.append(rec)
    return results


async def delete_uploaded_round(round_uid: str, *, uploads_dir: Path) -> bool:
    """Delete an uploaded round (refuses deleting disk rounds)."""
    import core.logs_db as _ldb

    rec = await _ldb.get_round_by_uid(round_uid)
    if rec is None:
        return False
    if rec.get("source_type") != "upload":
        raise ValueError("Удалять можно только загруженные вручную логи.")
    resolved_uploads = uploads_dir.resolve()
    target_dir = Path(rec["dir_path"]).resolve()
    if not target_dir.is_relative_to(resolved_uploads) or target_dir == resolved_uploads:
        raise ValueError("Небезопасный путь удаления.")

    conn = await _ldb.get_logs_connection()
    await conn.execute("DELETE FROM log_rounds WHERE round_uid = ?", (round_uid,))
    await conn.commit()
    if target_dir.is_dir():
        await asyncio.to_thread(shutil.rmtree, target_dir, True)
    return True
