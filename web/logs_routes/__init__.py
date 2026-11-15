"""FastAPI router for the SierraBay12 Log Viewer, Discord OAuth2 auth, whitelist, and audit admin."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict

from fastapi import APIRouter
from fastapi.templating import Jinja2Templates

from core.config import AppConfig
from web.logs_routes.admin import register_admin_routes
from web.logs_routes.api import register_api_routes
from web.logs_routes.auth import register_auth_routes
from web.logs_routes.deps import LogsRouteContext
from web.logs_routes.viewer import register_viewer_routes
from web.logs_routes.viewmodels import (
    _AUDIT_ACTION_LABELS,
    _audit_action_label,
    _coord_vm,
    _entry_to_vm,
    _fmt_bytes,
    _fmt_duration,
    _fmt_ts_utc,
    _ordered_categories,
    _parse_bool_flag,
    _parse_round_dt,
    _prefers_json,
    _round_time_vm,
)


def create_logs_router(
    templates: Jinja2Templates,
    get_config_fn: Callable[[], AppConfig],
    get_shell_context_fn: Callable[[], Awaitable[Dict[str, Any]]],
) -> APIRouter:
    """Build and return the FastAPI APIRouter for /auth/discord/*, /logs/*, and /api/v1/logs/*."""
    router = APIRouter()
    ctx = LogsRouteContext(
        templates=templates,
        get_config_fn=get_config_fn,
        get_shell_context_fn=get_shell_context_fn,
    )
    register_auth_routes(router, ctx)
    register_viewer_routes(router, ctx)
    register_admin_routes(router, ctx)
    register_api_routes(router, ctx)
    return router


__all__ = [
    "create_logs_router",
    "LogsRouteContext",
    "_AUDIT_ACTION_LABELS",
    "_fmt_ts_utc",
    "_fmt_bytes",
    "_audit_action_label",
    "_entry_to_vm",
    "_parse_round_dt",
    "_fmt_duration",
    "_round_time_vm",
    "_coord_vm",
    "_prefers_json",
    "_parse_bool_flag",
    "_ordered_categories",
]
