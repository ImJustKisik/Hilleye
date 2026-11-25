"""
Hilleye // Web Application
FastAPI application, Lifespan manager, middleware, exception handlers, and router mounting.
"""

from collections import OrderedDict
from contextlib import asynccontextmanager
import asyncio
import logging
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.byond_topic import (
    normalize_ckey,
    parse_manifest_response,
    public_topic_ip,
    query_topic,
)
from core.card_renderer import render_player_card
from core import db as core_db
from core import logs_db as core_logs_db
from core import map_renderer as core_maps
from core.hub_scraper import KNOWN_PAGER_ENDPOINTS
from core.tracker import get_tracker
from core.config import get_config
from core.log_jobs import JobRunner
from core.log_operations import finish_thread
from core.logs_db.celadon import migrate_celadon
from core.storage import validate_storage
from core.telemetry import telemetry, monitor_loop
from web.logs_routes import create_logs_router
from web.routes import (
    api_router,
    build_network_summary,
    dashboard_page,
    fetch_server_manifest_htmx,
    get_player_api,
    get_player_card_png,
    get_server_api,
    get_servers,
    health_check,
    health_router,
    liveness,
    pages_router,
    player_page,
    player_sessions_partial,
    search_player,
    server_page,
    suggest_players_api,
)

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
    - On startup: initialize SQLite WAL databases and start the background tracker.
    - On shutdown: stop background tracker, log indexer, and safely close database connections.
    """
    logger.info("Initializing Hilleye SQLite WAL database...")
    config = get_config()
    await finish_thread(validate_storage, config)
    try:
        await core_db.init_db(config.resolved_db_path())
        await core_logs_db.init_logs_db(config.resolved_logs_db_path())
        await migrate_celadon(config)
    except BaseException:
        await core_logs_db.close_logs_db()
        await core_db.close_db()
        raise

    logger.info("Starting Hilleye background telemetry tracker...")
    tracker = get_tracker(config=config)
    jobs = JobRunner(config) if config.resolved_logs_db_path() != ":memory:" else None
    app.state.log_jobs = jobs
    lag_task = None

    try:
        await tracker.start()
        if jobs is not None:
            await jobs.start()
        lag_task = asyncio.create_task(monitor_loop())
        yield
    finally:
        try:
            if jobs is not None:
                await jobs.stop()
            app.state.log_jobs = None
            if lag_task is not None:
                lag_task.cancel()
                await asyncio.gather(lag_task, return_exceptions=True)
            await tracker.stop()
            tasks = list(_manifest_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            _manifest_tasks.clear()
            _manifest_results.clear()
        finally:
            await core_logs_db.close_logs_db()
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


def _format_comma(value: Any) -> str:
    """Formats numeric template values with thousands separators."""
    if value is None:
        return "0"
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{int(num):,}" if num.is_integer() else f"{num:,.1f}"


templates.env.filters["comma"] = _format_comma


_CSP_HEADER = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'; "
    "form-action 'self'"
)


def _prefers_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "").lower()


async def _get_shell_context() -> Dict[str, Any]:
    """Safely fetches header/footer status context for SSR templates, including error pages."""
    servers: List[Dict[str, Any]] = []
    tracker_stats: Dict[str, Any] = {}
    collector_state: Optional[Dict[str, Any]] = None
    try:
        if core_db._connection is not None:
            servers = await core_db.get_active_servers()
            collector_state = await core_db.get_collector_state()
    except Exception:
        logger.debug("Failed to load active servers for shell context", exc_info=True)
    try:
        tracker = get_tracker()
        if tracker is not None:
            tracker_stats = tracker.get_stats()
    except Exception:
        logger.debug("Failed to load tracker stats for shell context", exc_info=True)
    summary = build_network_summary(servers, collector_state, tracker_stats)
    return {"servers": servers, "tracker_stats": tracker_stats, "summary": summary}


@app.middleware("http")
async def response_headers(request: Request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    route = request.scope.get("route")
    telemetry.record(getattr(route, "path", "unmatched"), time.monotonic() - started, response.status_code)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers["Content-Security-Policy"] = _CSP_HEADER
    path = request.url.path
    if path == "/logs" or path.startswith(("/logs/", "/api/v1/logs/", "/auth/", "/api/auth/")):
        response.headers["Cache-Control"] = "private, no-store"
    if request.url.path.startswith("/static/") and response.status_code in (200, 206, 304):
        response.headers["Cache-Control"] = "public, max-age=86400, stale-while-revalidate=3600"
    return response


app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if _prefers_html(request):
        shell = await _get_shell_context()
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                **shell,
                "status_code": 422,
                "heading": "Invalid request",
                "message": "Invalid request parameters. Check the address or form values and try again.",
            },
            status_code=422,
        )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code in (408, 429) and request.headers.get("HX-Request") == "true":
        return templates.TemplateResponse(
            request=request, name="partials/logs_operation_error.html",
            context={"message": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers,
        )
    if _prefers_html(request):
        shell = await _get_shell_context()
        if exc.status_code == 404:
            heading = "Page not found"
            message = "The requested page or resource could not be found."
        else:
            heading = "Request error"
            message = str(exc.detail) if exc.status_code in (408, 429) else "The server could not complete this request."
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                **shell,
                "status_code": exc.status_code,
                "heading": heading,
                "message": message,
            },
            status_code=exc.status_code,
            headers=exc.headers,
        )
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error handling %s %s", request.method, request.url.path)
    if _prefers_html(request):
        shell = await _get_shell_context()
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                **shell,
                "status_code": 500,
                "heading": "Internal server error",
                "message": "An unexpected internal error occurred. Please try again shortly.",
            },
            status_code=500,
        )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


app.include_router(health_router)
app.include_router(api_router)
app.include_router(pages_router)
app.include_router(
    create_logs_router(
        templates=templates,
        get_config_fn=lambda: get_config(),
        get_shell_context_fn=_get_shell_context,
    )
)

__all__ = [
    "app",
    "lifespan",
    "templates",
    "clear_card_cache",
    "health_check",
    "liveness",
    "get_servers",
    "get_server_api",
    "get_player_api",
    "get_player_card_png",
    "suggest_players_api",
    "search_player",
    "dashboard_page",
    "player_page",
    "player_sessions_partial",
    "server_page",
    "fetch_server_manifest_htmx",
]
