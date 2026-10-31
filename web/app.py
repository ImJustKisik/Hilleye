"""
CYBERSYN INTEL // Web Application
FastAPI application, Lifespan manager, REST endpoints, and HTMX SSR views.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from core.byond_topic import normalize_ckey
from core import db as core_db
from core.tracker import get_tracker

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR = BASE_DIR / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager:
    - On startup: initialize SQLite WAL database and start the background tracker.
    - On shutdown: stop background tracker and safely close database connections.
    """
    logger.info("Initializing Hilleye SQLite WAL database...")
    await core_db.init_db()

    logger.info("Starting Hilleye background telemetry tracker...")
    tracker = get_tracker()
    try:
        await tracker.start()
    except Exception as e:
        logger.error(f"Failed to start tracker: {e}")

    yield

    logger.info("Shutting down Hilleye background telemetry tracker...")
    try:
        await tracker.stop()
    except Exception as e:
        logger.error(f"Error stopping tracker: {e}")

    logger.info("Closing SQLite database connection pool...")
    await core_db.close_db()
    logger.info("Hilleye shutdown complete.")


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
async def health_check() -> Dict[str, Any]:
    """
    Operational health check returning system and tracker status.
    """
    tracker = get_tracker()
    tracker_stats = tracker.get_stats() if tracker else {}
    return {
        "status": "ok",
        "tracker": tracker_stats,
    }


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


@app.post("/search")
async def search_player(request: Request, ckey: str = Form(default="")):
    """
    HTMX search endpoint:
    Returns HTML dossier fragment or tactical 'NOT FOUND' partial.
    Supports HX-Push-Url to update browser location bar dynamically.
    """
    norm_ckey = normalize_ckey(ckey.strip()) if ckey else ""
    dossier = await core_db.get_player_dossier(norm_ckey) if norm_ckey else None

    if dossier is None:
        return templates.TemplateResponse(
            request=request,
            name="partials/not_found.html",
            context={"ckey": ckey, "raw_query": ckey},
            status_code=200,
        )

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
            "not_found": (dossier is None),
            "servers": servers,
            "tracker_stats": tracker_stats,
        },
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
