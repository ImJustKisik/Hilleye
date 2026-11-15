"""
Hilleye // Web Health & Readiness Routes
"""

import asyncio
import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

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
    return JSONResponse(
        {
            "status": "ok" if ready and collection_healthy else "degraded",
            "ready": ready,
            "collection_status": "ok" if collection_healthy else "degraded",
            "tracker": tracker_stats,
        },
        status_code=200 if ready else 503,
    )


@router.get("/live")
async def liveness():
    return {"status": "ok"}
