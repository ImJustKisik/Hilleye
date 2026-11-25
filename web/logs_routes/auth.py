"""Discord OAuth2 login, callback, and logout route handlers."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from core import discord_auth, logs_db
from web.logs_routes.deps import LogsRouteContext

logger = logging.getLogger(__name__)


def register_auth_routes(router: APIRouter, ctx: LogsRouteContext) -> None:
    @router.get("/auth/discord/login")
    async def discord_login(request: Request, next: str = "/logs"):
        config = ctx.get_config_fn()
        if not config.discord_oauth_configured():
            return await ctx.render_auth_gate(request, status_kind="unconfigured", next_path=next)

        state_token, state_cookie = discord_auth.create_oauth_state_cookie(config, next_path=next)
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
        if error:
            resp = await ctx.render_auth_gate(
                request,
                status_kind="unauthenticated",
                error_message="Авторизация через Discord отменена.",
                http_status=400,
            )
            resp.delete_cookie(
                discord_auth.OAUTH_STATE_COOKIE_NAME,
                path="/",
                secure=cookie_secure,
                httponly=True,
                samesite="lax",
            )
            return resp

        next_path = discord_auth.verify_oauth_state(state, state_cookie, config)
        if not code or not next_path:
            resp = await ctx.render_auth_gate(
                request,
                status_kind="unauthenticated",
                error_message="Некорректный или устаревший OAuth state. Попробуйте войти снова.",
                http_status=400,
            )
            resp.delete_cookie(
                discord_auth.OAUTH_STATE_COOKIE_NAME,
                path="/",
                secure=cookie_secure,
                httponly=True,
                samesite="lax",
            )
            return resp

        try:
            profile = await discord_auth.exchange_discord_code(code, config)
        except Exception as exc:
            logger.warning("Discord OAuth callback error: %s", exc)
            resp = await ctx.render_auth_gate(
                request,
                status_kind="unauthenticated",
                error_message="Не удалось подтвердить профиль в Discord API. Попробуйте снова.",
                http_status=502,
            )
            resp.delete_cookie(
                discord_auth.OAUTH_STATE_COOKIE_NAME,
                path="/",
                secure=cookie_secure,
                httponly=True,
                samesite="lax",
            )
            return resp

        ip = discord_auth.get_client_ip(request)
        if profile["mfa_enabled"] is not True:
            await logs_db.record_audit_event(
                "login_denied",
                discord_id=profile["discord_id"],
                discord_username=profile["username"],
                target="mfa_required",
                ip_address=ip,
            )
            resp = await ctx.render_auth_gate(
                request,
                status_kind="unauthenticated",
                error_message="Для входа включите двухфакторную аутентификацию (2FA) в настройках Discord и войдите снова.",
                next_path=next_path,
                http_status=403,
            )
            resp.delete_cookie(
                discord_auth.OAUTH_STATE_COOKIE_NAME,
                path="/",
                secure=cookie_secure,
                httponly=True,
                samesite="lax",
            )
            return resp

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

        redirect = RedirectResponse(next_path, status_code=302)
        redirect.delete_cookie(
            discord_auth.OAUTH_STATE_COOKIE_NAME,
            path="/",
            secure=cookie_secure,
            httponly=True,
            samesite="lax",
        )
        redirect.set_cookie(
            key=discord_auth.SESSION_COOKIE_NAME,
            value=session_cookie,
            max_age=int(config.discord_session_ttl_seconds),
            httponly=True,
            samesite="lax",
            secure=cookie_secure,
            path="/",
        )
        return redirect

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
