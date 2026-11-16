"""
Hilleye // Web SSR & HTMX Page Routes
"""

import asyncio
import logging
import time
from typing import Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/search")
async def search_player(request: Request, ckey: str = Form(default="")):
    """
    HTMX search endpoint:
    Returns HTML dossier fragment or tactical 'NOT FOUND' partial.
    Supports HX-Push-Url to update browser location bar dynamically.
    """
    import web.app as _app

    norm_ckey = _app.normalize_ckey(ckey.strip()) if ckey else ""
    target_url = f"/player/{norm_ckey}" if norm_ckey else "/"
    if request.headers.get("hx-request", "").lower() != "true":
        return RedirectResponse(target_url, status_code=303)
    dossier = await _app.core_db.get_player_dossier(norm_ckey) if norm_ckey else None

    if dossier is None:
        suggestions = (
            await _app.core_db.search_players(norm_ckey, limit=8, include_fuzzy=True)
            if norm_ckey
            else []
        )
        response = _app.templates.TemplateResponse(
            request=request,
            name="partials/not_found.html",
            context={"ckey": ckey, "raw_query": ckey, "suggestions": suggestions},
            status_code=200,
        )
        response.headers["HX-Push-Url"] = target_url
        return response

    response = _app.templates.TemplateResponse(
        request=request,
        name="partials/player_dossier.html",
        context={"dossier": dossier, "ckey": norm_ckey},
        status_code=200,
    )
    response.headers["HX-Push-Url"] = f"/player/{norm_ckey}"
    return response


@router.get("/")
async def dashboard_page(request: Request):
    """
    Renders main tactical search console and monitored sector overview.
    """
    import web.app as _app

    servers = await _app.core_db.get_active_servers()
    overview = await _app.core_db.get_network_overview()
    tracker = _app.get_tracker()
    tracker_stats = tracker.get_stats() if tracker else {}
    return _app.templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "servers": servers,
            "overview": overview,
            "tracker_stats": tracker_stats,
            "base_url": str(request.base_url).rstrip("/"),
        },
    )


@router.get("/player/{ckey}")
async def player_page(request: Request, ckey: str):
    """
    Renders permalink page for a target ckey.
    """
    import web.app as _app

    norm_ckey = _app.normalize_ckey(ckey.strip()) if ckey else ""
    dossier = await _app.core_db.get_player_dossier(norm_ckey) if norm_ckey else None
    suggestions = (
        await _app.core_db.search_players(norm_ckey, limit=8, include_fuzzy=True)
        if (dossier is None and norm_ckey)
        else []
    )
    servers = await _app.core_db.get_active_servers()
    tracker = _app.get_tracker()
    tracker_stats = tracker.get_stats() if tracker else {}

    return _app.templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "request": request,
            "dossier": dossier,
            "target_ckey": ckey,
            "ckey": ckey,
            "not_found": (dossier is None),
            "suggestions": suggestions,
            "servers": servers,
            "tracker_stats": tracker_stats,
            "base_url": str(request.base_url).rstrip("/"),
        },
        status_code=404 if dossier is None else 200,
    )


@router.get("/player/{ckey}/sessions")
async def player_sessions_partial(
    request: Request,
    ckey: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=10, ge=1, le=50),
):
    """
    HTMX pagination partial for player recent sessions table rows.
    """
    import web.app as _app

    norm_ckey = _app.normalize_ckey(ckey.strip()) if ckey else ""
    dossier = (
        await _app.core_db.get_player_dossier(norm_ckey, sessions_limit=limit, sessions_offset=offset)
        if norm_ckey
        else None
    )
    if dossier is None:
        return HTMLResponse(content="", status_code=404)

    return _app.templates.TemplateResponse(
        request=request,
        name="partials/player_sessions_rows.html",
        context={
            "request": request,
            "dossier": dossier,
            "sessions_offset": offset,
            "sessions_limit": limit,
        },
        status_code=200,
    )


@router.get("/server/{server_id}")
async def server_page(request: Request, server_id: int, time_range: str = Query(default='day')):
    """
    Renders detailed server telemetry page (/server/{server_id}).
    Returns 404 HTML response if server_id does not exist in database.
    """
    import web.app as _app

    server_details = await _app.core_db.get_server_details(server_id, time_range=time_range)
    servers = await _app.core_db.get_active_servers()
    tracker = _app.get_tracker()
    tracker_stats = tracker.get_stats() if tracker else {}

    if server_details is None:
        return _app.templates.TemplateResponse(
            request=request,
            name="server.html",
            context={
                "request": request,
                "server": None,
                "server_id": server_id,
                "time_range": time_range,
                "not_found": True,
                "manifest_supported": False,
                "servers": servers,
                "tracker_stats": tracker_stats,
                "base_url": str(request.base_url).rstrip("/"),
            },
            status_code=404,
        )

    ident = server_details.get("identifier") or ""
    manifest_supported = bool(server_details.get("topic_available") or ident in _app.KNOWN_PAGER_ENDPOINTS)

    return _app.templates.TemplateResponse(
        request=request,
        name="server.html",
        context={
            "request": request,
            "server": server_details,
            "server_id": server_id,
            "time_range": time_range,
            "not_found": False,
            "manifest_supported": manifest_supported,
            "servers": servers,
            "tracker_stats": tracker_stats,
            "base_url": str(request.base_url).rstrip("/"),
        },
        status_code=200,
    )


@router.post("/server/{server_id}/manifest")
async def fetch_server_manifest_htmx(request: Request, server_id: int):
    """
    Queries live BYOND World Topic `?manifest` for a Baystation / SierraBay compatible server,
    caches the structured crew manifest in SQLite, and renders the HTML partial.
    """
    import web.app as _app

    server_details = await _app.core_db.get_server_details(server_id)
    if server_details is None:
        return HTMLResponse(
            content='<div class="block-empty">Server not found.</div>',
            status_code=404,
        )

    ip = server_details.get("ip")
    port = int(server_details.get("port") or 0)
    ident = server_details.get("identifier") or ""
    if port == 0 and ident in _app.KNOWN_PAGER_ENDPOINTS:
        ip, port = _app.KNOWN_PAGER_ENDPOINTS[ident]
    query_error: Optional[str] = None

    cache_key = (_app.core_db._current_db_path, server_id)

    async def refresh():
        error = None
        try:
            if not ip or not 0 < port <= 65535:
                raise ValueError("This server has a pager address; a direct Topic endpoint is unavailable.")
            tracker = _app.get_tracker()
            trusted = (
                ident in _app.KNOWN_PAGER_ENDPOINTS
                or any(
                    c.get("identifier") == server_details["identifier"]
                    or (c.get("ip") == ip and c.get("port") == port)
                    for c in tracker.custom_servers
                )
            )
            # Share the collector's connection budget, including concurrent HTTP requests.
            async with tracker.semaphore:
                target = (
                    ip
                    if trusted
                    else await asyncio.wait_for(_app.public_topic_ip(ip, port), tracker.socket_timeout)
                )
                raw = await _app.query_topic(ip=target, port=port, query="?manifest", timeout=tracker.socket_timeout)
            departments = (
                {str(k).lower(): v for k, v in raw.items() if str(k).lower() in _app.core_db._DEPT_LABELS}
                if isinstance(raw, dict)
                else {}
            )

            def valid_members(value):
                if isinstance(value, str):
                    return not value.strip() or "=" in value
                if isinstance(value, dict):
                    return all(isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in value.items())
                if isinstance(value, list):
                    return all(
                        (isinstance(v, dict) and isinstance(v.get("name"), str) and v["name"].strip())
                        or (isinstance(v, str) and "=" in v)
                        for v in value
                    )
                return False

            recognized = (
                (isinstance(raw, dict) and len(raw) == 0)
                or (bool(departments) and all(valid_members(v) for v in departments.values()))
            )
            if recognized:
                manifest = _app.parse_manifest_response(raw)
                await _app.core_db.save_manifest(server_id, manifest, time.time())
            else:
                error = "Manifest unavailable or unrecognized; displaying previously received data."
        except Exception as exc:
            logger.warning("Manifest query failed for server %s: %s", server_id, exc)
            error = "Manifest query failed; displaying previously received data."
        _app._manifest_results[cache_key] = (time.monotonic(), error)
        _app._manifest_results.move_to_end(cache_key)
        while len(_app._manifest_results) > 256:
            _app._manifest_results.popitem(last=False)
        return error

    cached = _app._manifest_results.get(cache_key)
    if cached is not None and time.monotonic() - cached[0] < 3:
        query_error = cached[1]
    else:
        task = _app._manifest_tasks.get(cache_key)
        if task is None:
            if len(_app._manifest_tasks) >= 64:
                return HTMLResponse("Manifest query capacity reached; retry shortly.", status_code=503)
            task = asyncio.create_task(asyncio.wait_for(refresh(), 30))
            _app._manifest_tasks[cache_key] = task

            def on_done(done):
                _app._manifest_tasks.pop(cache_key, None)
                if not done.cancelled():
                    done.exception()  # Observe failures even if every HTTP client disconnects.

            task.add_done_callback(on_done)
        try:
            query_error = await asyncio.shield(task)
        except asyncio.TimeoutError:
            query_error = "Manifest query timed out; displaying previously received data."
    server_details = await _app.core_db.get_server_details(server_id) or server_details

    return _app.templates.TemplateResponse(
        request=request,
        name="partials/server_manifest.html",
        context={
            "request": request,
            "server": server_details,
            "query_error": query_error,
        },
        status_code=200,
    )
