"""Git commit inspection and streaming selective GitHub tarball downloader."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
import queue
import re
import subprocess
import tarfile
from typing import Any, Dict, Optional

import httpx

from core.log_operations import finish_awaitable
from core.map_renderer.specs import (
    GITHUB_ARCHIVE_TIMEOUT_SECONDS,
    MAX_EXTRACTED_REPO_BYTES,
    MAX_GITHUB_ARCHIVE_BYTES,
    _GITHUB_BRANCH_RE,
    _GITHUB_REPO_RE,
    _set_build_status,
)


def _should_extract_github_path(rel_str: str) -> bool:
    """Only extract map/icon/DM assets needed for Sierra rendering."""
    low = rel_str.lower()
    if low.startswith(("maps/sierra/", "icons/", "nano/images/sierra/")):
        return True
    if low.startswith(("code/", "mods/", "packs/")):
        return low.endswith((".dm", ".dmi"))
    return False


def _detect_local_git_commit(repo_dir: Path) -> str:
    if not (repo_dir / ".git").exists():
        return ""
    try:
        status = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            return ""
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


class _ChunkQueueReader(io.RawIOBase):
    """Stream adapter feeding async HTTP chunks into synchronous tarfile('r|gz')."""

    def __init__(self, chunk_queue: "queue.Queue[Optional[bytes]]") -> None:
        self._queue = chunk_queue
        self._buf = bytearray()
        self._eof = False

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        while not self._eof and (size < 0 or len(self._buf) < size):
            item = self._queue.get()
            if item is None:
                self._eof = True
                break
            self._buf.extend(item)
        if size < 0 or size >= len(self._buf):
            out = bytes(self._buf)
            self._buf.clear()
            return out
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out


def _extract_tar_stream_sync(reader: _ChunkQueueReader, dest_dir: Path) -> str:
    dest_resolved = dest_dir.resolve()
    total_extracted = 0
    with tarfile.open(fileobj=reader, mode="r|gz") as tf:
        for member in tf:
            if not member.isreg():
                continue
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            rel_parts = parts[1:]
            if any(p in ("..", "") for p in rel_parts):
                continue
            rel_str = "/".join(rel_parts)
            if not _should_extract_github_path(rel_str):
                continue
            total_extracted += member.size
            if total_extracted > MAX_EXTRACTED_REPO_BYTES:
                raise ValueError("Превышен лимит размера распаковки архива GitHub.")
            out_path = (dest_resolved / Path(*rel_parts)).resolve()
            if not out_path.is_relative_to(dest_resolved):
                continue
            out_path.parent.mkdir(parents=True, exist_ok=True)
            src = tf.extractfile(member)
            if src is None:
                continue
            with src, out_path.open("wb") as dst:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
        comment = tf.pax_headers.get("comment", "").strip().lower()
        return comment if re.fullmatch(r"[0-9a-f]{40}", comment) else ""


async def _download_github_repo_subset(
    github_repo: str,
    github_branch: str,
    dest_dir: Path,
) -> str:
    """Stream and selectively extract map/icon files from GitHub tarball with zero archive disk buffer."""
    import core.map_renderer as _mr

    repo_clean = (github_repo or "").strip()
    branch_clean = (github_branch or "").strip()
    if not _GITHUB_REPO_RE.match(repo_clean):
        raise ValueError("Некорректное имя GitHub-репозитория (ожидается owner/repo).")
    if not _GITHUB_BRANCH_RE.match(branch_clean) or ".." in branch_clean:
        raise ValueError("Некорректное имя ветки GitHub.")

    is_commit = re.fullmatch(r"[0-9a-fA-F]{40}", branch_clean) is not None
    archive_ref = branch_clean if is_commit else f"refs/heads/{branch_clean}"
    url = f"https://github.com/{repo_clean}/archive/{archive_ref}.tar.gz"
    transport = _mr._github_transport_factory() if _mr._github_transport_factory else None
    client_kwargs: Dict[str, Any] = {
        "timeout": httpx.Timeout(
            connect=15.0,
            read=GITHUB_ARCHIVE_TIMEOUT_SECONDS,
            write=30.0,
            pool=15.0,
        ),
        "follow_redirects": True,
    }
    if transport is not None:
        client_kwargs["transport"] = transport

    downloaded = 0
    _set_build_status(
        phase="downloading_github",
        downloaded_mb=0.0,
        total_mb=0.0,
        detail=f"Потоковое скачивание {repo_clean}@{branch_clean} с GitHub...",
    )
    chunk_queue: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=16)
    reader = _ChunkQueueReader(chunk_queue)
    extract_task: Optional[asyncio.Task] = None
    eof_sent = False

    async def put_chunk(chunk):
        while True:
            if extract_task is not None and extract_task.done():
                await extract_task
                return False
            try:
                chunk_queue.put_nowait(chunk)
                return True
            except queue.Full:
                await asyncio.sleep(0.01)

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"GitHub вернул HTTP {resp.status_code} при загрузке {url}")
                content_len = resp.headers.get("content-length")
                total_mb = (
                    round(int(content_len) / (1024 * 1024), 1)
                    if content_len and content_len.isdigit()
                    else 0.0
                )
                _set_build_status(total_mb=total_mb)
                extract_task = asyncio.create_task(
                    asyncio.to_thread(_extract_tar_stream_sync, reader, dest_dir)
                )
                async for chunk in resp.aiter_bytes(chunk_size=131072):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > MAX_GITHUB_ARCHIVE_BYTES:
                        raise ValueError("Архив GitHub превышает допустимый размер.")
                    if extract_task.done():
                        break
                    if not await put_chunk(chunk):
                        break
                    _set_build_status(
                        downloaded_mb=round(downloaded / (1024 * 1024), 1),
                        detail=f"Скачивание и распаковка с GitHub: {downloaded / (1024 * 1024):.1f} МБ...",
                    )
        if extract_task is not None:
            eof_sent = await put_chunk(None)
            recorded = await asyncio.shield(extract_task)
            if is_commit and recorded and recorded != branch_clean.lower():
                raise ValueError("GitHub archive does not match the requested revision")
            return branch_clean.lower() if is_commit else recorded
        return ""
    finally:
        if extract_task is not None:
            if not eof_sent:
                while True:
                    try:
                        chunk_queue.put_nowait(None)
                        break
                    except queue.Full:
                        try:
                            chunk_queue.get_nowait()
                        except queue.Empty:
                            break
            if not extract_task.done():
                try:
                    await finish_awaitable(extract_task)
                except Exception:
                    pass
