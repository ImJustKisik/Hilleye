"""Durable log/map jobs with independent OS-elected owners and shared status."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager

from core import logs_db, map_renderer
from core.file_lock import FileLock, lock_path
from core.log_operations import finish_thread
from core.logs_db.connection import run_logs_transaction

logger = logging.getLogger(__name__)


@contextmanager
def _connect(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def enqueue_job(path, kind, created_by=None, payload=None):
    payload = payload or {}
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id FROM log_jobs WHERE kind=? AND payload_json=? AND state IN ('queued','running') ORDER BY created_at LIMIT 1",
                           (kind, json.dumps(payload, sort_keys=True))).fetchone()
        if row:
            job_id = row[0]
        else:
            if conn.execute("SELECT COUNT(*) FROM log_jobs WHERE state='queued'").fetchone()[0] >= 32:
                raise OverflowError("Очередь фоновых задач заполнена")
            job_id = uuid.uuid4().hex
            conn.execute("INSERT INTO log_jobs(id,kind,created_by,created_at,payload_json) VALUES (?,?,?,?,?)",
                         (job_id, kind, created_by, time.time(), json.dumps(payload, sort_keys=True)))
        if created_by:
            conn.execute("INSERT OR IGNORE INTO log_job_users VALUES (?,?)", (job_id, created_by))
        return job_id


def get_job(path, job_id, *, user_id=None, admin=False):
    with _connect(path) as conn:
        row = conn.execute("SELECT * FROM log_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        if not admin and not conn.execute("SELECT 1 FROM log_job_users WHERE job_id=? AND user_id=?", (job_id, user_id)).fetchone():
            return None
        result = dict(row)
        result.pop("payload_json")
        result["result"] = json.loads(result.pop("result_json") or "null")
        if result["error"]:
            result["error"] = "Задача завершилась с ошибкой; подробности записаны в журнал сервиса."
        return result


def _logs_status(conn, max_age):
    sources = [dict(row) for row in conn.execute("SELECT source,status,last_scan_at,last_success_at,pending_bytes FROM log_sources")]
    errors = conn.execute("SELECT COUNT(*) FROM log_rounds WHERE index_error IS NOT NULL").fetchone()[0]
    running = [dict(row) for row in conn.execute("SELECT id,kind,heartbeat,progress FROM log_jobs WHERE state='running'")]
    pending = conn.execute("SELECT COUNT(*) FROM log_jobs WHERE state='queued'").fetchone()[0]
    pending_imports = conn.execute("SELECT COUNT(*) FROM log_upload_batches").fetchone()[0]
    backup = conn.execute("SELECT value_json FROM log_maintenance_state WHERE name='last_backup'").fetchone()
    now = time.time()
    for source in sources:
        if (source["status"] == "ok" and max_age is not None and source["last_success_at"] is not None
                and now - source["last_success_at"] > max_age):
            source["status"] = "stale"
    if errors or any(s["status"] in ("degraded", "stale") for s in sources) or any(
        now - (job["heartbeat"] or 0) > 20 for job in running
    ):
        status = "degraded"
    elif any(s["status"] == "indexing" for s in sources):
        status = "indexing"
    elif not sources or any(s["status"] == "uninitialized" for s in sources):
        status = "uninitialized"
    else:
        status = "ok"
    return {"status": status, "sources": sources, "index_errors": errors, "running": running,
            "queued": pending, "pending_imports": pending_imports,
            "busy": bool(pending or running or status == "indexing"),
            "pending_bytes": sum(s["pending_bytes"] for s in sources),
            "last_backup_at": json.loads(backup[0])["completed_at"] if backup else None}


def logs_status(path, max_age=None):
    with _connect(path) as conn:
        return _logs_status(conn, max_age)


async def configured_logs_status(config):
    path = config.resolved_logs_db_path()
    if path == ":memory:":
        return await run_logs_transaction(_logs_status, None)
    max_age = max(60, 3 * config.logs_scan_interval_seconds) if config.logs_periodic_scan else None
    return await asyncio.to_thread(logs_status, path, max_age)


def map_job_status(path):
    if path == ":memory:":
        return None
    with _connect(path) as conn:
        row = conn.execute("SELECT state,progress,started_at,finished_at FROM log_jobs WHERE kind='map_rebuild' ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {"state": {"queued": "running", "running": "running", "succeeded": "ready", "failed": "error"}[row["state"]],
            "phase": row["state"], "detail": row["progress"],
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(row["started_at"])) if row["started_at"] else "",
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(row["finished_at"])) if row["finished_at"] else "",
            "error": "Сборка завершилась с ошибкой" if row["state"] == "failed" else ""}


class JobRunner:
    def __init__(self, config):
        self.config = config
        self.path = config.resolved_logs_db_path()
        self.tasks = []

    async def start(self):
        if self.path == ":memory:":
            return
        self.tasks = [asyncio.create_task(self._lane("index", ("refresh", "reindex"))),
                      asyncio.create_task(self._lane("maps", ("map_rebuild",)))]

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def submit(self, kind, created_by=None, payload=None):
        return await asyncio.to_thread(enqueue_job, self.path, kind, created_by, payload)

    async def wait(self, job_id):
        while True:
            job = await asyncio.to_thread(get_job, self.path, job_id, admin=True)
            if job is None:
                raise RuntimeError("Background job was not found")
            if job["state"] in ("succeeded", "partial"):
                return job["result"]
            if job["state"] == "failed":
                raise RuntimeError(job["error"])
            await asyncio.sleep(0.1)

    def _recover(self, kinds):
        with _connect(self.path) as conn:
            marks = ",".join("?" for _ in kinds)
            conn.execute(f"UPDATE log_jobs SET state='queued',heartbeat=NULL,progress='Возобновление после перезапуска' WHERE kind IN ({marks}) AND state='running'", kinds)

    def _claim(self, kinds):
        with _connect(self.path) as conn:
            marks = ",".join("?" for _ in kinds)
            row = conn.execute(f"SELECT * FROM log_jobs WHERE kind IN ({marks}) AND state='queued' ORDER BY created_at LIMIT 1", kinds).fetchone()
            if row:
                claimed = conn.execute(
                    "UPDATE log_jobs SET state='running',started_at=?,heartbeat=?,progress='Запуск' WHERE id=? AND state='queued'",
                    (time.time(), time.time(), row["id"]),
                )
                if claimed.rowcount:
                    return dict(row)
        return None

    def _update(self, job_id, *, state=None, progress=None, result=None, error=None):
        with _connect(self.path) as conn:
            conn.execute("""UPDATE log_jobs SET state=COALESCE(?,state),heartbeat=?,
                         progress=COALESCE(?,progress),result_json=COALESCE(?,result_json),error=?,
                         finished_at=CASE WHEN ? IS NOT NULL THEN ? ELSE finished_at END WHERE id=? AND state='running'""",
                         (state, time.time(), progress, json.dumps(result) if result is not None else None,
                          error, state, time.time(), job_id))

    async def _heartbeat(self, job):
        while True:
            if job["kind"] == "map_rebuild":
                progress = map_renderer.get_map_build_status().get("detail") or "Сборка карты"
            else:
                progress = None
            try:
                await finish_thread(self._update, job["id"], progress=progress)
            except sqlite3.OperationalError:
                logger.warning("Could not write background job heartbeat", exc_info=True)
            await asyncio.sleep(1)

    async def _execute(self, job):
        started = time.monotonic()
        heartbeat = asyncio.create_task(self._heartbeat(job))
        try:
            payload = json.loads(job["payload_json"])
            if job["kind"] == "map_rebuild":
                result = await map_renderer.build_map_cache(self.config, force=payload.get("force", True), prefer_github=payload.get("prefer_github", False))
            else:
                result = await logs_db.sync_and_index_rounds(
                    self.config.resolved_logs_root_dir(), self.config.resolved_logs_uploads_dir(),
                    include_debug=payload.get("include_debug", self.config.logs_index_include_debug),
                    force_reindex=job["kind"] == "reindex",
                    use_saved_debug=True, persist_debug=job["kind"] == "reindex" and "include_debug" in payload,
                    job_id=job["id"] if job["kind"] == "reindex" else None,
                )
            partial = job["kind"] != "map_rebuild" and bool(result["errors"])
            terminal = dict(state="partial" if partial else "succeeded",
                            progress="Завершено с ошибками" if partial else "Завершено", result=result)
        except asyncio.CancelledError:
            # The next OS-elected owner requeues this job and resumes its committed checkpoint.
            raise
        except Exception as exc:
            logger.exception("Background job failed: %s", job["id"])
            terminal = dict(state="failed", progress="Ошибка", error=str(exc)[:500])
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            logger.info("background_job kind=%s elapsed=%.3f", job["kind"], time.monotonic() - started)
        while True:
            try:
                await finish_thread(self._update, job["id"], **terminal)
                return
            except sqlite3.OperationalError:
                logger.exception("Could not finalize background job %s; retrying", job["id"])
                await asyncio.sleep(1)

    async def _lane(self, name, kinds):
        owner = FileLock(lock_path(self.path, f"{name}-owner"))
        acquired = False
        recovered = False
        due = time.monotonic() + (0 if self.config.logs_auto_scan_on_startup else self.config.logs_scan_interval_seconds)
        map_startup = self.config.maps_auto_build_on_startup
        try:
            while True:
                try:
                    if not acquired:
                        acquired = owner.acquire()
                        if not acquired:
                            await asyncio.sleep(1)
                            continue
                    if not recovered:
                        await finish_thread(self._recover, kinds)
                        recovered = True
                    if name == "index" and self.config.logs_periodic_scan and time.monotonic() >= due:
                        await self.submit("refresh")
                        due = time.monotonic() + self.config.logs_scan_interval_seconds
                    elif name == "index" and self.config.logs_auto_scan_on_startup and due <= time.monotonic():
                        await self.submit("refresh")
                        due = float("inf")
                    if name == "maps" and map_startup:
                        if not map_renderer.get_map_manifest(self.config.resolved_maps_cache_dir()).get("ready"):
                            await self.submit("map_rebuild", payload={"force": False})
                        map_startup = False
                    job = await finish_thread(self._claim, kinds)
                    if job:
                        await self._execute(job)
                    else:
                        await asyncio.sleep(0.2)
                except Exception:
                    logger.exception("Background lane failed; retrying: %s", name)
                    await asyncio.sleep(1)
        finally:
            owner.release()
