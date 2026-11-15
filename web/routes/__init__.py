"""
Hilleye // Web Tracker Routes Package
"""

from web.routes.api import (
    get_player_api,
    get_player_card_png,
    get_server_api,
    get_servers,
    router as api_router,
)
from web.routes.health import (
    health_check,
    liveness,
    router as health_router,
)
from web.routes.pages import (
    dashboard_page,
    fetch_server_manifest_htmx,
    player_page,
    player_sessions_partial,
    router as pages_router,
    search_player,
    server_page,
)

__all__ = [
    "api_router",
    "health_router",
    "pages_router",
    "health_check",
    "liveness",
    "get_servers",
    "get_server_api",
    "get_player_api",
    "get_player_card_png",
    "search_player",
    "dashboard_page",
    "player_page",
    "player_sessions_partial",
    "server_page",
    "fetch_server_manifest_htmx",
]
