"""Bounded log work; regex runs in a process that can actually be terminated."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import multiprocessing
import os
from pathlib import Path
import pickle
import tempfile
import time

from fastapi import HTTPException

from core.file_lock import FileLock, lock_path

logger = logging.getLogger(__name__)


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


@asynccontextmanager
async def heavy_slot(config):
    slots = [FileLock(lock_path(config.resolved_logs_db_path(), f"heavy-{i}")) for i in range(2)]
    slot = next((item for item in slots if item.acquire()), None)
    if slot is None:
        raise HTTPException(429, "Сервис обрабатывает другие тяжёлые задачи. Повторите позже.", headers={"Retry-After": "2"})
    try:
        yield
    finally:
        slot.release()


async def finish_thread(fn, *args, **kwargs):
    """Do not release a lock or remove staging while a cancelled thread still writes."""
    return await finish_awaitable(asyncio.to_thread(fn, *args, **kwargs))


async def finish_awaitable(operation):
    task = asyncio.ensure_future(operation)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            continue
        if cancelled:
            raise asyncio.CancelledError
        return result


@asynccontextmanager
async def staging_directory(**kwargs):
    temporary = await finish_thread(tempfile.TemporaryDirectory, **kwargs)
    try:
        yield Path(temporary.name)
    finally:
        await finish_thread(temporary.cleanup)


def _process_main(fn, args, kwargs, result_path):
    # The payload and pickle are created locally; neither is accepted from an HTTP client.
    try:
        result = (True, fn(*args, **kwargs))
    except Exception as exc:
        result = (False, str(exc))
    path = Path(result_path)
    pending = path.with_suffix(".pending")
    with pending.open("wb") as stream:
        pickle.dump(result, stream, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(pending, path)


async def _stop_process(process):
    if process.is_alive():
        process.terminate()
    await asyncio.to_thread(process.join, 1)
    if process.is_alive():
        process.kill()
        await asyncio.to_thread(process.join)


async def run_log_operation(request, config, fn, *args, **kwargs):
    started = time.monotonic()
    async with heavy_slot(config):
        try:
            if not kwargs.get("use_regex", False):
                return await finish_thread(fn, *args, **kwargs)
            with tempfile.TemporaryDirectory(prefix="hilleye-regex-") as tmp:
                result_path = Path(tmp) / "result.pickle"
                process = multiprocessing.get_context("spawn").Process(
                    target=_process_main, args=(fn, args, kwargs, str(result_path)), daemon=True,
                )
                process.start()
                deadline = time.monotonic() + config.logs_regex_timeout_seconds
                try:
                    while process.is_alive():
                        if time.monotonic() >= deadline:
                            raise HTTPException(408, "Поиск превысил допустимое время. Уточните выражение или фильтры.")
                        if request is not None and await request.is_disconnected():
                            raise HTTPException(499, "Клиент отключился; поиск остановлен.")
                        await asyncio.sleep(0.025)
                    await asyncio.to_thread(process.join)
                    if process.exitcode != 0 or not result_path.is_file():
                        raise HTTPException(500, "Процесс поиска завершился с ошибкой.")
                    def read_result():
                        with result_path.open("rb") as stream:
                            return pickle.load(stream)
                    ok, result = await asyncio.to_thread(read_result)
                    if not ok:
                        raise ValueError(result)
                    return result
                finally:
                    await asyncio.shield(_stop_process(process))
                    process.close()
        finally:
            logger.info("log_operation name=%s regex=%s elapsed=%.3f", fn.__name__, bool(kwargs.get("use_regex")), time.monotonic() - started)
