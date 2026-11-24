"""
Hilleye // Web Health & Readiness Routes
"""

import asyncio
import logging
import shutil
import time
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from core.log_jobs import configured_logs_status
from core.telemetry import telemetry

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/health")
@router.get("/ready")
async def health_check():
    """
    Check DB/worker readiness separately from external collection quality.
    """
    import web.app as _app

    tracker = _app.get_tracker()
    tracker_stats = tracker.get_stats() if tracker else {}
    ready = False
    collection_healthy = False
    try:
        if _app.core_db._connection is not None:
            state = await asyncio.wait_for(_app.core_db.get_collector_state(), timeout=2)
            age = time.time() - state["completed_at"] if state else None
            ready = bool(
                tracker.is_running()
                and state
                and 0 <= age <= tracker.poll_interval + tracker.config.cycle_timeout_seconds
            )
            collection_healthy = bool(state and state["healthy"])
            if tracker.is_leader:
                collection_healthy = collection_healthy and tracker._cycle_healthy
                ready = ready and tracker._clock_trusted
    except Exception:
        logger.exception("Database readiness check failed")
    logs = {"status": "unavailable"}
    if _app.core_logs_db._connection is not None and _app.core_logs_db._current_logs_db_path != ":memory:":
        try:
            cfg = _app.get_config()
            details = await asyncio.wait_for(configured_logs_status(cfg), 2)
            logs = {key: details[key] for key in ("status", "pending_bytes", "pending_imports", "index_errors", "queued", "last_backup_at")}
            sizes = {}
            for label, path in (("tracker", _app.core_db._current_db_path), ("logs", _app.core_logs_db._current_logs_db_path)):
                if path != ":memory:":
                    for suffix in ("", "-wal"):
                        file_path = Path(str(path) + suffix)
                        sizes[label + ("_wal_bytes" if suffix else "_bytes")] = file_path.stat().st_size if file_path.exists() else 0
            logs["storage"] = sizes
            logs["disk"] = await asyncio.to_thread(
                _free_disk_bytes,
                {"index": Path(_app.core_logs_db._current_logs_db_path).parent,
                 "uploads": cfg.resolved_logs_uploads_dir(), "maps": cfg.resolved_maps_cache_dir()},
            )
        except Exception:
            logger.warning("Logs status is unavailable", exc_info=True)
    return JSONResponse(
        {
            "status": "ok" if ready and collection_healthy else "degraded",
            "ready": ready,
            "collection_status": "ok" if collection_healthy else "degraded",
            "tracker": tracker_stats,
            "logs": logs,
            "telemetry": telemetry.snapshot(),
        },
        status_code=200 if ready else 503,
    )


@router.get("/live")
async def liveness():
    return {"status": "ok"}


def _free_disk_bytes(paths):
    result = {}
    for label, path in paths.items():
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        result[label] = {"free_bytes": usage.free, "total_bytes": usage.total}
    return result
