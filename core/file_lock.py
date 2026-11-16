"""Small non-blocking OS locks shared by web workers and maintenance commands."""
from __future__ import annotations

from contextlib import asynccontextmanager
import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import time


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.handle = None

    def acquire(self) -> bool:
        if self.handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None

    def __enter__(self):
        if not self.acquire():
            raise BlockingIOError(f"Operation is already running: {self.path.name}")
        return self

    def __exit__(self, *_):
        self.release()


def lock_path(db_path: str, name: str) -> Path:
    if db_path == ":memory:":
        root = Path(tempfile.gettempdir()) / f"hilleye-{os.getpid()}"
    else:
        db = Path(db_path).resolve()
        root = db.parent / ".hilleye-locks" / hashlib.sha256(os.path.normcase(str(db)).encode()).hexdigest()[:16]
    return root / f"{name}.lock"


@asynccontextmanager
async def lease(path: Path, *, timeout: float = 60):
    lock = FileLock(path)
    deadline = time.monotonic() + timeout
    try:
        while not lock.acquire():
            if time.monotonic() >= deadline:
                raise TimeoutError("Another maintenance operation is still running")
            await asyncio.sleep(0.05)
        yield
    finally:
        lock.release()
