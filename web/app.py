"""
CYBERSYN INTEL // Web Application
FastAPI application, Lifespan manager, REST endpoints, and HTMX SSR views.
"""

from collections import OrderedDict
from contextlib import asynccontextmanager
import asyncio
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.byond_topic import (
    normalize_ckey,
    parse_manifest_response,
    public_topic_ip,
    query_topic,
)
from core.card_renderer import render_player_card
from core import db as core_db
from core.tracker import get_tracker
from core.config import get_config

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"

CARD_CACHE_TTL_SECONDS: float = 60.0
CARD_CACHE_MAX_SIZE: int = 256
_card_cache: "OrderedDict[str, Tuple[float, bytes]]" = OrderedDict()
_card_cache_db_path: Optional[str] = None
_card_slots = asyncio.Semaphore(2)
_manifest_tasks: Dict[Tuple[str, int], asyncio.Task] = {}
_manifest_results: OrderedDict = OrderedDict()


def clear_card_cache() -> None:
    """Clears the in-memory player card PNG cache."""
    _card_cache.clear()


def _get_cached_card(norm_ckey: str) -> Optional[bytes]:
    """Retrieves cached PNG bytes for norm_ckey if within TTL, else None."""
    global _card_cache_db_path
    current_db = getattr(core_db, "_current_db_path", None)
    if current_db != _card_cache_db_path:
        _card_cache.clear()
        _card_cache_db_path = current_db

    entry = _card_cache.get(norm_ckey)
    if entry is None:
        return None

    cached_at, png_bytes = entry
    if (time.monotonic() - cached_at) >= CARD_CACHE_TTL_SECONDS:
        _card_cache.pop(norm_ckey, None)
        return None

    _card_cache.move_to_end(norm_ckey)
    return png_bytes


def _set_cached_card(norm_ckey: str, png_bytes: bytes) -> None:
    """Stores rendered PNG bytes in the bounded TTL LRU cache."""
    global _card_cache_db_path
    _card_cache_db_path = getattr(core_db, "_current_db_path", None)
    _card_cache[norm_ckey] = (time.monotonic(), png_bytes)
    _card_cache.move_to_end(norm_ckey)
    while len(_card_cache) > CARD_CACHE_MAX_SIZE:
        _card_cache.popitem(last=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager:
    - On startup: initialize SQLite WAL database and start the background tracker.
    - On shutdown: stop background tracker and safely close database connections.
    """
    logger.info("Initializing Hilleye SQLite WAL database...")
    config = get_config()
    await core_db.init_db(config.resolved_db_path())

    logger.info("Starting Hilleye background telemetry tracker...")
    tracker = get_tracker(config=config)
    try:
        await tracker.start()
        yield
    finally:
        try:
            await tracker.stop()
            tasks = list(_manifest_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            _manifest_tasks.clear()
            _manifest_results.clear()
        finally:
            await core_db.close_db()


app = FastAPI(
    title="Hilleye",
    description="Space Station 13 Player & Server Playtime Intelligence Network",
    version="1.0.0",
    lifespan=lifespan,
)

# Static files and Jinja2 templates
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/health")
@app.get("/ready")
async def health_check():
    """
    Operational health check returning system and tracker status.
    """
    tracker = get_tracker()
    tracker_stats = tracker.get_stats() if tracker else {}
    ready = False
    try:
        if core_db._connection is not None:
            state = await asyncio.wait_for(core_db.get_collector_state(), timeout=2)
            age = time.time() - state["completed_at"] if state else None
            ready = bool(tracker.is_running() and state and state["healthy"] and
                         0 <= age <= tracker.poll_interval + tracker.config.cycle_timeout_seconds)
            if tracker.is_leader:
                ready = ready and tracker._cycle_healthy and tracker._clock_trusted
    except Exception:
        logger.exception("Database readiness check failed")
    return JSONResponse({"status": "ok" if ready else "degraded", "tracker": tracker_stats},
                        status_code=200 if ready else 503)


@app.get("/live")
async def liveness():
    return {"status": "ok"}


@app.get("/api/v1/servers")
async def get_servers() -> Dict[str, Any]:
    """
    Returns list of active monitored SS13 servers.
    """
    servers = await core_db.get_active_servers()
    return {"servers": servers}


@app.get("/api/v1/server/{server_id}")
async def get_server_api(server_id: int) -> Any:
    """
    Returns detailed JSON status, current round parameters, active roster,
    top-20 players by playtime, and 24-hour online history for the specified server.
    Returns 404 if server_id does not exist.
    """
    details = await core_db.get_server_details(server_id)
    if details is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "Server not found",
                "server_id": server_id,
                "detail": f"Server #{server_id} not found in registry",
            },
        )
    return details


@app.get("/api/v1/player/{ckey}")
async def get_player_api(ckey: str) -> Dict[str, Any]:
    """
    Returns tactical JSON dossier for the specified ckey.
    Returns 404 if the player has not been recorded on any monitored sector.
    """
    norm_ckey = normalize_ckey(ckey)
    if not norm_ckey:
        return JSONResponse(
            status_code=404,
            content={"error": "Player not found", "ckey": ckey, "detail": "Invalid or empty ckey"},
        )

    dossier = await core_db.get_player_dossier(norm_ckey)
    if dossier is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Player not found", "ckey": ckey, "detail": "Target not found on monitored frequencies"},
        )

    return dossier


@app.get("/api/v1/player/{ckey}/card.png")
async def get_player_card_png(ckey: str) -> Response:
    """
    Returns a 720x360 PNG player dossier card for Discord clients.
    Caches rendered PNGs in memory with a 60-second TTL and sets Cache-Control header.
    Returns 404 if the player is not found.
    """
    norm_ckey = normalize_ckey(ckey)
    if not norm_ckey:
        return JSONResponse(
            status_code=404,
            content={"error": "Player not found", "ckey": ckey, "detail": "Invalid or empty ckey"},
        )

    cached_png = _get_cached_card(norm_ckey)
    if cached_png is not None:
        return Response(
            content=cached_png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=60"},
        )

    dossier = await core_db.get_player_dossier(norm_ckey)
    if dossier is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Player not found", "ckey": ckey, "detail": "Target not found on monitored frequencies"},
        )

    async with _card_slots:
        # Hold the slot until the thread finishes, even if the HTTP client leaves.
        task = asyncio.create_task(asyncio.to_thread(render_player_card, dossier))
        try:
            png_bytes = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    _set_cached_card(norm_ckey, png_bytes)

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=60"},
    )


@app.post("/search")
async def search_player(request: Request, ckey: str = Form(default="")):
    """
    HTMX search endpoint:
    Returns HTML dossier fragment or tactical 'NOT FOUND' partial.
    Supports HX-Push-Url to update browser location bar dynamically.
    """
    norm_ckey = normalize_ckey(ckey.strip()) if ckey else ""
    target_url = f"/player/{norm_ckey}" if norm_ckey else "/"
    if request.headers.get("hx-request", "").lower() != "true":
        return RedirectResponse(target_url, status_code=303)
    dossier = await core_db.get_player_dossier(norm_ckey) if norm_ckey else None

    if dossier is None:
        response = templates.TemplateResponse(
            request=request,
            name="partials/not_found.html",
            context={"ckey": ckey, "raw_query": ckey},
            status_code=200,
        )
        response.headers["HX-Push-Url"] = target_url
        return response

    response = templates.TemplateResponse(
        request=request,
        name="partials/player_dossier.html",
        context={"dossier": dossier, "ckey": norm_ckey},
        status_code=200,
    )
    if "hx-request" in request.headers:
        response.headers["HX-Push-Url"] = f"/player/{norm_ckey}"
    return response


@app.get("/")
async def dashboard_page(request: Request):
    """
    Renders main tactical search console and monitored sector overview.
    """
    servers = await core_db.get_active_servers()
    tracker = get_tracker()
    tracker_stats = tracker.get_stats() if tracker else {}
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request, "servers": servers, "tracker_stats": tracker_stats},
    )


@app.get("/player/{ckey}")
async def player_page(request: Request, ckey: str):
    """
    Renders permalink page for a target ckey.
    """
    norm_ckey = normalize_ckey(ckey.strip()) if ckey else ""
    dossier = await core_db.get_player_dossier(norm_ckey) if norm_ckey else None
    servers = await core_db.get_active_servers()
    tracker = get_tracker()
    tracker_stats = tracker.get_stats() if tracker else {}

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "dossier": dossier,
            "target_ckey": ckey,
            "ckey": ckey,
            "not_found": (dossier is None),
            "servers": servers,
            "tracker_stats": tracker_stats,
        },
        status_code=404 if dossier is None else 200,
    )


@app.get("/server/{server_id}")
async def server_page(request: Request, server_id: int):
    """
    Renders detailed server telemetry page (/server/{server_id}).
    Returns 404 HTML response if server_id does not exist in database.
    """
    server_details = await core_db.get_server_details(server_id)
    servers = await core_db.get_active_servers()
    tracker = get_tracker()
    tracker_stats = tracker.get_stats() if tracker else {}

    if server_details is None:
        return templates.TemplateResponse(
            request=request,
            name="server.html",
            context={
                "request": request,
                "server": None,
                "server_id": server_id,
                "not_found": True,
                "servers": servers,
                "tracker_stats": tracker_stats,
            },
            status_code=404,
        )

    return templates.TemplateResponse(
        request=request,
        name="server.html",
        context={
            "request": request,
            "server": server_details,
            "server_id": server_id,
            "not_found": False,
            "servers": servers,
            "tracker_stats": tracker_stats,
        },
        status_code=200,
    )


@app.post("/server/{server_id}/manifest")
async def fetch_server_manifest_htmx(request: Request, server_id: int):
    """
    Queries live BYOND World Topic `?manifest` for a Baystation / SierraBay compatible server,
    caches the structured crew manifest in SQLite, and renders the HTML partial.
    """
    server_details = await core_db.get_server_details(server_id)
    if server_details is None:
        return HTMLResponse(
            content='<div class="block-empty">Server not found.</div>',
            status_code=404,
        )

    ip = server_details.get("ip")
    port = int(server_details.get("port") or 0)
    query_error: Optional[str] = None

    cache_key = (core_db._current_db_path, server_id)

    async def refresh():
        error = None
        try:
            if not ip or not 0 < port <= 65535:
                raise ValueError("This server has a pager address; a direct Topic endpoint is unavailable.")
            tracker = get_tracker()
            trusted = any(c.get("identifier") == server_details["identifier"] or
                          (c.get("ip") == ip and c.get("port") == port)
                          for c in tracker.custom_servers)
            # Share the collector's connection budget, including concurrent HTTP requests.
            async with tracker.semaphore:
                target = ip if trusted else await asyncio.wait_for(public_topic_ip(ip, port), tracker.socket_timeout)
                raw = await query_topic(ip=target, port=port, query="?manifest", timeout=tracker.socket_timeout)
            departments = {str(k).lower(): v for k, v in raw.items()
                           if str(k).lower() in core_db._DEPT_LABELS} if isinstance(raw, dict) else {}
            def valid_members(value):
                if isinstance(value, str):
                    return not value.strip() or "=" in value
                if isinstance(value, dict):
                    return all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
                if isinstance(value, list):
                    return all((isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip())
                               or (isinstance(v, str) and "=" in v) for v in value)
                return False
            recognized = bool(departments) and all(valid_members(v) for v in departments.values())
            if recognized:
                manifest = parse_manifest_response(raw)
                await core_db.save_manifest(server_id, manifest, time.time())
            else:
                error = "Manifest unavailable or unrecognized; displaying previously received data."
        except Exception as exc:
            logger.warning("Manifest query failed for server %s: %s", server_id, exc)
            error = "Manifest query failed; displaying previously received data."
        _manifest_results[cache_key] = (time.monotonic(), error)
        _manifest_results.move_to_end(cache_key)
        while len(_manifest_results) > 256:
            _manifest_results.popitem(last=False)
        return error

    cached = _manifest_results.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < 3:
        query_error = cached[1]
    else:
        task = _manifest_tasks.get(cache_key)
        if task is None:
            if len(_manifest_tasks) >= 64:
                return HTMLResponse("Manifest query capacity reached; retry shortly.", status_code=503)
            task = asyncio.create_task(asyncio.wait_for(refresh(), 30))
            _manifest_tasks[cache_key] = task
            def on_done(done):
                _manifest_tasks.pop(cache_key, None)
                if not done.cancelled():
                    done.exception()  # Observe failures even if every HTTP client disconnects.
            task.add_done_callback(on_done)
        try:
            query_error = await asyncio.shield(task)
        except asyncio.TimeoutError:
            query_error = "Manifest query timed out; displaying previously received data."
    server_details = await core_db.get_server_details(server_id) or server_details

    return templates.TemplateResponse(
        request=request,
        name="partials/server_manifest.html",
        context={
            "request": request,
            "server": server_details,
            "query_error": query_error,
        },
        status_code=200,
    )
