"""
Hilleye // Web REST API v1 Routes
"""

import asyncio
from typing import Any, Dict

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response

router = APIRouter()


@router.get("/api/v1/players/suggest")
async def suggest_players_api(
    q: str = Query(default=""),
    limit: int = Query(default=8, ge=1, le=20),
) -> Dict[str, Any]:
    """
    Returns prefix, substring, and fuzzy player ckey suggestions for typeahead search.
    """
    import web.app as _app

    norm_q = _app.normalize_ckey(q)
    if not norm_q:
        return {"query": q, "normalized": "", "suggestions": []}

    suggestions = await _app.core_db.search_players(norm_q, limit=limit, include_fuzzy=True)
    return {
        "query": q,
        "normalized": norm_q,
        "suggestions": suggestions,
    }


@router.get("/api/v1/servers")
async def get_servers() -> Dict[str, Any]:
    """
    Returns list of active monitored SS13 servers.
    """
    import web.app as _app

    servers = await _app.core_db.get_active_servers()
    return {"servers": servers}


@router.get("/api/v1/server/{server_id}")
async def get_server_api(server_id: int) -> Any:
    """
    Returns detailed JSON status, current round parameters, active roster,
    top-20 players by playtime, and 24-hour online history for the specified server.
    Returns 404 if server_id does not exist.
    """
    import web.app as _app

    details = await _app.core_db.get_server_details(server_id)
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


@router.get("/api/v1/player/{ckey}")
async def get_player_api(ckey: str) -> Dict[str, Any]:
    """
    Returns tactical JSON dossier for the specified ckey.
    Returns 404 if the player has not been recorded on any monitored sector.
    """
    import web.app as _app

    norm_ckey = _app.normalize_ckey(ckey)
    if not norm_ckey:
        return JSONResponse(
            status_code=404,
            content={"error": "Player not found", "ckey": ckey, "detail": "Invalid or empty ckey"},
        )

    dossier = await _app.core_db.get_player_dossier(norm_ckey)
    if dossier is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Player not found", "ckey": ckey, "detail": "Target not found on monitored frequencies"},
        )

    return dossier


@router.get("/api/v1/player/{ckey}/card.png")
async def get_player_card_png(ckey: str) -> Response:
    """
    Returns a 720x360 PNG player dossier card for Discord clients.
    Caches rendered PNGs in memory with a 60-second TTL and sets Cache-Control header.
    Returns 404 if the player is not found.
    """
    import web.app as _app

    norm_ckey = _app.normalize_ckey(ckey)
    if not norm_ckey:
        return JSONResponse(
            status_code=404,
            content={"error": "Player not found", "ckey": ckey, "detail": "Invalid or empty ckey"},
        )

    cached_png = _app._get_cached_card(norm_ckey)
    if cached_png is not None:
        return Response(
            content=cached_png,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=60"},
        )

    dossier = await _app.core_db.get_player_dossier(norm_ckey)
    if dossier is None:
        return JSONResponse(
            status_code=404,
            content={"error": "Player not found", "ckey": ckey, "detail": "Target not found on monitored frequencies"},
        )

    async with _app._card_slots:
        # Hold the slot until the thread finishes, even if the HTTP client leaves.
        task = asyncio.create_task(asyncio.to_thread(_app.render_player_card, dossier))
        try:
            png_bytes = await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
    _app._set_cached_card(norm_ckey, png_bytes)

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=60"},
    )
