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
from web.logs_routes import create_logs_router
from web.routes import (
    api_router,
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
    await core_db.init_db(config.resolved_db_path())
    await core_logs_db.init_logs_db(config.resolved_logs_db_path())

    logger.info("Starting Hilleye background telemetry tracker...")
    tracker = get_tracker(config=config)
    log_sync_task: Optional[asyncio.Task] = None
    map_build_task: Optional[asyncio.Task] = None

    async def _initial_log_sync() -> None:
        try:
            root_dir = config.resolved_logs_root_dir()
            uploads_dir = config.resolved_logs_uploads_dir()
            if (root_dir and root_dir.is_dir()) or uploads_dir.is_dir():
                summary = await core_logs_db.sync_and_index_rounds(
                    root_dir,
                    uploads_dir,
                    include_debug=config.logs_index_include_debug,
                    force_reindex=False,
                )
                logger.info(
                    "Initial log index sync complete: %s rounds discovered, %s indexed (%ss)",
                    summary["discovered"],
                    summary["indexed_rounds"],
                    summary["elapsed_seconds"],
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background log index sync failed")

    async def _initial_map_build() -> None:
        try:
            cache_dir = config.resolved_maps_cache_dir()
            manifest = core_maps.get_map_manifest(cache_dir)
            if not manifest.get("ready"):
                built = await core_maps.build_map_cache(config, force=False)
                logger.info(
                    "Station map cache ready (%s, %s Z-levels in %ss)",
                    built.get("source_kind"),
                    len(built.get("levels", [])),
                    built.get("elapsed_seconds"),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Background station map cache build failed", exc_info=True)

    try:
        await tracker.start()
        if config.logs_auto_scan_on_startup:
            root_dir = config.resolved_logs_root_dir()
            uploads_dir = config.resolved_logs_uploads_dir()
            if (root_dir and root_dir.is_dir()) or uploads_dir.is_dir():
                log_sync_task = asyncio.create_task(_initial_log_sync())
        if config.maps_auto_build_on_startup:
            cache_dir = config.resolved_maps_cache_dir()
            if not core_maps.get_map_manifest(cache_dir).get("ready"):
                repo_dir = config.resolved_maps_repo_dir()
                if (repo_dir and (repo_dir / "maps" / "sierra").is_dir()) or config.maps_github_repo:
                    map_build_task = asyncio.create_task(_initial_map_build())
        yield
    finally:
        try:
            for bg_task in (log_sync_task, map_build_task):
                if bg_task is not None and not bg_task.done():
                    bg_task.cancel()
                    await asyncio.gather(bg_task, return_exceptions=True)
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
    try:
        if core_db._connection is not None:
            servers = await core_db.get_active_servers()
    except Exception:
        logger.debug("Failed to load active servers for shell context", exc_info=True)
    try:
        tracker = get_tracker()
        if tracker is not None:
            tracker_stats = tracker.get_stats()
    except Exception:
        logger.debug("Failed to load tracker stats for shell context", exc_info=True)
    return {"servers": servers, "tracker_stats": tracker_stats}


@app.middleware("http")
async def response_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
        response.headers["Content-Security-Policy"] = _CSP_HEADER
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
    if _prefers_html(request):
        shell = await _get_shell_context()
        if exc.status_code == 404:
            heading = "Page not found"
            message = "The requested page or resource could not be found."
        else:
            heading = "Request error"
            message = "The server could not complete this request."
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
