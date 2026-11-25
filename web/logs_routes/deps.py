"""Shared route context, Discord OAuth2 guards, and round file resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from core import discord_auth, logs_db
from core.byond_topic import normalize_ckey
from core.config import AppConfig
from core.log_parser import mask_sensitive_text
from core.logs_db.reader import indexed_entry_matches


def validate_log_search(ckey: str, date_from: str, date_to: str) -> tuple[str, str, str]:
    normalized_ckey = normalize_ckey(ckey.strip())
    if ckey.strip() and not normalized_ckey:
        raise HTTPException(400, "Укажите корректный ckey.")

    def parse_day(value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        try:
            day = date.fromisoformat(value).isoformat()
        except ValueError:
            raise HTTPException(400, "Укажите дату в формате ГГГГ-ММ-ДД.") from None
        if day != value:
            raise HTTPException(400, "Укажите дату в формате ГГГГ-ММ-ДД.")
        return day

    start, end = parse_day(date_from), parse_day(date_to)
    if start and end and start > end:
        raise HTTPException(400, "Дата начала должна быть не позже даты окончания.")
    return normalized_ckey, start, end


async def require_indexed_entry(round_uid, file_name, file_path, line_no, entry_id):
    if entry_id is not None and (
        file_name != "game.log" or line_no is None
        or not await indexed_entry_matches(round_uid, file_path, line_no, entry_id)
    ):
        raise HTTPException(409, "Исходный лог изменился после поиска. Повторите поиск и откройте актуальную строку.")


async def record_log_access(request, user, action, target, *, details):
    await logs_db.record_audit_event(
        action, discord_id=user.discord_id, discord_username=user.username,
        target=mask_sensitive_text(target), ip_address=discord_auth.get_client_ip(request),
        details=details,
    )


@dataclass
class LogsRouteContext:
    templates: Jinja2Templates
    get_config_fn: Callable[[], AppConfig]
    get_shell_context_fn: Callable[[], Awaitable[Dict[str, Any]]]

    async def render_auth_gate(
        self,
        request: Request,
        *,
        status_kind: str,
        session: Optional[Dict[str, Any]] = None,
        error_message: str = "",
        next_path: str = "/logs",
        http_status: Optional[int] = None,
        popup_attempt: str = "",
    ) -> HTMLResponse:
        shell = await self.get_shell_context_fn()
        config = self.get_config_fn()
        code = http_status
        if code is None:
            if status_kind == "unconfigured":
                code = 503
            elif status_kind == "forbidden":
                code = 403
            else:
                code = 401
        return self.templates.TemplateResponse(
            request=request,
            name="logs_auth.html",
            context={
                **shell,
                "status_kind": status_kind,
                "session_user": session,
                "error_message": error_message,
                "popup_attempt": popup_attempt,
                "next_path": discord_auth.sanitize_next_path(next_path),
                "oauth_configured": config.discord_oauth_configured(),
                "redirect_uri_hint": config.discord_redirect_uri or "https://your-domain.example/auth/discord/callback",
            },
            status_code=code,
        )

    async def require_html_user(
        self,
        request: Request,
        *,
        require_admin: bool = False,
    ) -> Tuple[Optional[discord_auth.AuthUser], Optional[HTMLResponse]]:
        config = self.get_config_fn()
        status_kind, session, user = await discord_auth.resolve_auth_state(request, config)
        if status_kind != "ok" or user is None:
            resp = await self.render_auth_gate(
                request,
                status_kind=status_kind,
                session=session,
                next_path=request.url.path,
            )
            return None, resp
        if require_admin and not user.is_admin:
            resp = await self.render_auth_gate(
                request,
                status_kind="admin_required",
                session=session,
                error_message="Для открытия этого раздела нужны права администратора.",
                http_status=403,
            )
            return None, resp
        return user, None

    async def require_api_user(
        self,
        request: Request,
        *,
        require_admin: bool = False,
    ) -> discord_auth.AuthUser:
        config = self.get_config_fn()
        status_kind, _session, user = await discord_auth.resolve_auth_state(request, config)
        if status_kind == "unconfigured":
            raise HTTPException(status_code=503, detail="Discord OAuth2 не настроен на сервере.")
        if status_kind == "unauthenticated" or (user is None and status_kind != "forbidden"):
            raise HTTPException(status_code=401, detail="Требуется вход через Discord.")
        if status_kind == "forbidden" or user is None:
            raise HTTPException(status_code=403, detail="Ваш Discord ID не добавлен в список доступа.")
        if require_admin and not user.is_admin:
            raise HTTPException(status_code=403, detail="Требуются права администратора.")
        return user

    async def resolve_round_and_file(
        self,
        round_uid: str,
        file_name: str,
    ) -> Tuple[Dict[str, Any], Path]:
        config = self.get_config_fn()
        rnd = await logs_db.get_round_by_uid(round_uid)
        if rnd is None:
            raise HTTPException(status_code=404, detail="Раунд не найден.")
        file_path = logs_db.resolve_round_file_path(
            rnd,
            file_name,
            root_dir=config.resolved_logs_root_dir(),
            uploads_dir=config.resolved_logs_uploads_dir(),
        )
        if file_path is None:
            raise HTTPException(status_code=404, detail=f"Файл {file_name} не найден.")
        return rnd, file_path
