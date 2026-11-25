"""Discord OAuth2 login, callback, and logout route handlers."""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from core import discord_auth, logs_db
from web.logs_routes.deps import LogsRouteContext

logger = logging.getLogger(__name__)


def _clear_oauth_state(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        discord_auth.OAUTH_STATE_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def register_auth_routes(router: APIRouter, ctx: LogsRouteContext) -> None:
    @router.get("/auth/discord/login")
    async def discord_login(request: Request, next: str = "/logs", popup_attempt: UUID | None = None):
        config = ctx.get_config_fn()
        if not config.discord_oauth_configured():
            return await ctx.render_auth_gate(request, status_kind="unconfigured", next_path=next)

        state_token, state_cookie = discord_auth.create_oauth_state_cookie(
            config, next_path=next, popup_attempt=str(popup_attempt) if popup_attempt else ""
        )
        authorize_url = discord_auth.build_discord_authorize_url(config, state_token)
        response = RedirectResponse(authorize_url, status_code=302)
        response.set_cookie(
            key=discord_auth.OAUTH_STATE_COOKIE_NAME,
            value=state_cookie,
            max_age=600,
            httponly=True,
            samesite="lax",
            secure=bool(config.discord_secure_cookies),
            path="/",
        )
        return response

    @router.get("/api/auth/callback")
    @router.get("/auth/discord/callback")
    async def discord_callback(
        request: Request,
        code: Optional[str] = None,
        state: Optional[str] = None,
        error: Optional[str] = None,
    ):
        config = ctx.get_config_fn()
        if not config.discord_oauth_configured():
            return await ctx.render_auth_gate(request, status_kind="unconfigured")

        state_cookie = request.cookies.get(discord_auth.OAUTH_STATE_COOKIE_NAME)
        cookie_secure = bool(config.discord_secure_cookies)
        oauth_state = discord_auth.verify_oauth_state(state, state_cookie, config)

        async def render_failure(message: str, http_status: int, status_kind: str = "unauthenticated"):
            resp = await ctx.render_auth_gate(
                request,
                status_kind=status_kind,
                error_message=message,
                next_path=oauth_state.next_path if oauth_state else "/logs",
                popup_attempt=oauth_state.popup_attempt if oauth_state else "",
                http_status=http_status,
            )
            if oauth_state:
                _clear_oauth_state(resp, secure=cookie_secure)
            return resp

        if error:
            return await render_failure("Авторизация через Discord отменена.", 400)
        if not code or not oauth_state:
            return await render_failure("Некорректный или устаревший OAuth state. Попробуйте войти снова.", 400)

        try:
            profile = await discord_auth.exchange_discord_code(code, config)
        except Exception as exc:
            logger.warning("Discord OAuth callback error: %s", exc)
            return await render_failure(
                "Не удалось подтвердить профиль в Discord API. Попробуйте снова.", 502
            )

        ip = discord_auth.get_client_ip(request)
        if not discord_auth.meets_mfa_requirement(profile["discord_id"], profile["mfa_enabled"], config):
            await logs_db.record_audit_event(
                "login_denied",
                discord_id=profile["discord_id"],
                discord_username=profile["username"],
                target="mfa_required",
                ip_address=ip,
            )
            return await render_failure(
                "Для входа включите двухфакторную аутентификацию (2FA) в настройках Discord и войдите снова.",
                403,
                status_kind="mfa_required",
            )

        session_cookie = discord_auth.create_session_cookie(profile, config)
        allowed, role, _ = await logs_db.check_whitelist_access(
            profile["discord_id"], config.discord_superadmin_ids
        )
        await logs_db.record_audit_event(
            "login" if allowed else "login_denied",
            discord_id=profile["discord_id"],
            discord_username=profile["username"],
            target=role if allowed else "whitelist_missing",
            ip_address=ip,
            details=f"display_name={profile['display_name']}",
        )

        if oauth_state.popup_attempt:
            response = await ctx.render_auth_gate(
                request,
                status_kind="signed_in",
                next_path=oauth_state.next_path,
                popup_attempt=oauth_state.popup_attempt,
                http_status=200,
            )
        else:
            response = RedirectResponse(oauth_state.next_path, status_code=302)
        _clear_oauth_state(response, secure=cookie_secure)
        response.set_cookie(
            key=discord_auth.SESSION_COOKIE_NAME,
            value=session_cookie,
            max_age=int(config.discord_session_ttl_seconds),
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            path="/",
        )
        return response

    @router.post("/auth/logout")
    async def discord_logout(request: Request, csrf_token: str = Form(default="")):
        config = ctx.get_config_fn()
        _status_kind, session, user = await discord_auth.resolve_auth_state(request, config)
        if session is not None:
            expected_csrf = str(session.get("csrf") or "")
            header_csrf = request.headers.get("x-csrf-token", "")
            provided = csrf_token or header_csrf
            if user is not None:
                discord_auth.verify_csrf(user, provided)
            elif expected_csrf and provided != expected_csrf:
                raise HTTPException(status_code=403, detail="Недействительный CSRF-токен.")
            await logs_db.record_audit_event(
                "logout",
                discord_id=str(session.get("uid") or ""),
                discord_username=str(session.get("username") or ""),
                ip_address=discord_auth.get_client_ip(request),
            )
        response = RedirectResponse("/logs", status_code=303)
        response.delete_cookie(
            discord_auth.SESSION_COOKIE_NAME,
            path="/",
            secure=bool(config.discord_secure_cookies),
            httponly=True,
            samesite="lax",
        )
        return response
