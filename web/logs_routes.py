"""FastAPI router for the SierraBay12 Log Viewer, Discord OAuth2 auth, whitelist, and audit admin."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from core import discord_auth
from core import logs_db
from core import map_renderer
from core.byond_topic import normalize_ckey
from core.config import AppConfig
from core.log_parser import (
    CATEGORY_META,
    LOG_PRESETS,
    category_info,
    category_matches_filters,
    collect_round_map_events,
    export_filtered_game_log,
    filter_game_log_file,
    mask_sensitive_text,
    parse_qdel_log_file,
    read_context_lines,
    sanitize_byond_html,
    scan_raw_files_for_query,
)

logger = logging.getLogger(__name__)


def _fmt_ts_utc(epoch: Optional[float]) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_bytes(num: int) -> str:
    val = float(num or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if val < 1024.0 or unit == "ГБ":
            return f"{val:.1f} {unit}" if unit != "Б" else f"{int(val)} Б"
        val /= 1024.0
    return f"{int(val)} Б"


_AUDIT_ACTION_LABELS: Dict[str, str] = {
    "login": "Вход через Discord",
    "login_denied": "Отказ входа (нет доступа)",
    "logout": "Выход",
    "view_round": "Просмотр раунда",
    "search_global": "Глобальный поиск",
    "download_log": "Скачивание лога",
    "upload_round": "Загрузка лога",
    "delete_upload": "Удаление загруженного лога",
    "whitelist_add": "Выдача доступа",
    "whitelist_remove": "Отзыв доступа",
    "refresh": "Обновление списка",
    "reindex": "Переиндексация",
    "rebuild_maps": "Пересборка карты",
}


def _audit_action_label(action: str) -> str:
    return _AUDIT_ACTION_LABELS.get(action, action)


def _entry_to_vm(
    entry: Any,
    cache_dir: Path,
    *,
    focus: Optional[int] = None,
    target_line: Optional[int] = None,
    active_ckey_norm: str = "",
) -> Dict[str, Any]:
    meta = category_info(entry.category)
    visible_ckeys = (
        [ck for ck in entry.ckeys if ck != active_ckey_norm]
        if active_ckey_norm
        else entry.ckeys
    )
    return {
        "line_no": entry.line_no,
        "timestamp": entry.timestamp,
        "time_short": entry.timestamp[11:] if len(entry.timestamp) > 11 else entry.timestamp,
        "category": entry.category,
        "cat_css": meta["css"],
        "safe_html": entry.safe_html,
        "ckeys": entry.ckeys,
        "visible_ckeys": visible_ckeys,
        "coords": _coord_vm(entry.coords, cache_dir),
        "is_continuation": getattr(entry, "is_continuation", False),
        "is_focused": focus is not None and entry.line_no == focus,
        "is_target": target_line is not None and entry.line_no == target_line,
    }


def _parse_round_dt(raw: str) -> Optional[datetime]:
    text = (raw or "").strip()
    if len(text) < 19:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _fmt_duration(seconds: int) -> str:
    if seconds < 0:
        return ""
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}ч {minutes:02d}м"
    if minutes > 0:
        return f"{minutes}м"
    return f"{secs}с"


def _round_time_vm(rnd: Dict[str, Any]) -> Dict[str, str]:
    date_label = str(rnd.get("round_date") or "").strip()
    started_raw = str(rnd.get("started_at") or "").strip()
    ended_raw = str(rnd.get("ended_at") or "").strip()

    def _compact(ts: str, base_date: str) -> str:
        if not ts:
            return ""
        head = ts[:19] if len(ts) >= 19 and ts[10:11] == " " and ts[13:14] == ":" else ts
        if base_date and head.startswith(base_date + " "):
            return head[len(base_date) + 1 :]
        return head

    start_short = _compact(started_raw, date_label)
    end_short = _compact(ended_raw, date_label)
    time_range = f"{start_short} – {end_short}" if (start_short and end_short) else (start_short or end_short)

    dt_start = _parse_round_dt(started_raw)
    dt_end = _parse_round_dt(ended_raw)
    duration = _fmt_duration(int((dt_end - dt_start).total_seconds())) if (dt_start and dt_end) else ""

    return {
        "date": date_label,
        "range": time_range,
        "duration": duration,
    }


def _coord_vm(coords: Optional[Tuple[int, int, int]], cache_dir: Path) -> Optional[Dict[str, Any]]:
    if not coords:
        return None
    x, y, z = int(coords[0]), int(coords[1]), int(coords[2])
    if x < 1 or y < 1 or z < 1:
        return None
    return {
        "x": x,
        "y": y,
        "z": z,
        "z_label": map_renderer.deck_short_label(z),
        "area": map_renderer.lookup_area_name(cache_dir, z, x, y),
    }


def _prefers_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "").lower()
    return "application/json" in accept and "text/html" not in accept


def _parse_bool_flag(raw: Optional[str], default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _ordered_categories(
    category_counts: Dict[str, int],
    *,
    preset: str = "all",
    selected_cats: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    priority = list(CATEGORY_META.keys())
    known = [c for c in priority if c in category_counts]
    extra = sorted(c for c in category_counts if c not in CATEGORY_META)
    items = []
    sel_arg = selected_cats or None
    for cat in known + extra:
        meta = category_info(cat)
        items.append(
            {
                "name": cat,
                "count": category_counts[cat],
                "css": meta["css"],
                "desc": meta["desc"],
                "included": category_matches_filters(cat, preset=preset, selected_categories=sel_arg),
            }
        )
    return items


def create_logs_router(
    templates: Jinja2Templates,
    get_config_fn: Callable[[], AppConfig],
    get_shell_context_fn: Callable[[], Awaitable[Dict[str, Any]]],
) -> APIRouter:
    """Build and return the FastAPI APIRouter for /auth/discord/*, /logs/*, and /api/v1/logs/*."""
    router = APIRouter()

    async def _render_auth_gate(
        request: Request,
        *,
        status_kind: str,
        session: Optional[Dict[str, Any]] = None,
        error_message: str = "",
        next_path: str = "/logs",
        http_status: Optional[int] = None,
    ) -> HTMLResponse:
        shell = await get_shell_context_fn()
        config = get_config_fn()
        code = http_status
        if code is None:
            if status_kind == "unconfigured":
                code = 503
            elif status_kind == "forbidden":
                code = 403
            else:
                code = 401
        return templates.TemplateResponse(
            request=request,
            name="logs_auth.html",
            context={
                **shell,
                "status_kind": status_kind,
                "session_user": session,
                "error_message": error_message,
                "next_path": discord_auth.sanitize_next_path(next_path),
                "oauth_configured": config.discord_oauth_configured(),
                "redirect_uri_hint": config.discord_redirect_uri or "https://your-domain.example/auth/discord/callback",
            },
            status_code=code,
        )

    async def _require_html_user(
        request: Request,
        *,
        require_admin: bool = False,
    ) -> Tuple[Optional[discord_auth.AuthUser], Optional[HTMLResponse]]:
        config = get_config_fn()
        status_kind, session, user = await discord_auth.resolve_auth_state(request, config)
        if status_kind != "ok" or user is None:
            resp = await _render_auth_gate(
                request,
                status_kind=status_kind,
                session=session,
                next_path=request.url.path,
            )
            return None, resp
        if require_admin and not user.is_admin:
            resp = await _render_auth_gate(
                request,
                status_kind="admin_required",
                session=session,
                error_message="Для открытия этого раздела нужны права администратора.",
                http_status=403,
            )
            return None, resp
        return user, None

    async def _require_api_user(
        request: Request,
        *,
        require_admin: bool = False,
    ) -> discord_auth.AuthUser:
        config = get_config_fn()
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

    async def _resolve_round_and_file(
        round_uid: str,
        file_name: str,
    ) -> Tuple[Dict[str, Any], Path]:
        config = get_config_fn()
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

    # -----------------------------------------------------------------------
    # Discord OAuth2 Endpoints
    # -----------------------------------------------------------------------

    @router.get("/auth/discord/login")
    async def discord_login(request: Request, next: str = "/logs"):
        config = get_config_fn()
        if not config.discord_oauth_configured():
            return await _render_auth_gate(request, status_kind="unconfigured", next_path=next)

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
        config = get_config_fn()
        if not config.discord_oauth_configured():
            return await _render_auth_gate(request, status_kind="unconfigured")

        state_cookie = request.cookies.get(discord_auth.OAUTH_STATE_COOKIE_NAME)
        cookie_secure = bool(config.discord_secure_cookies)
        if error:
            resp = await _render_auth_gate(
                request,
                status_kind="unauthenticated",
                error_message="Авторизация через Discord отменена.",
                http_status=400,
            )
            resp.delete_cookie(discord_auth.OAUTH_STATE_COOKIE_NAME, path="/", secure=cookie_secure, httponly=True, samesite="lax")
            return resp

        next_path = discord_auth.verify_oauth_state(state, state_cookie, config)
        if not code or not next_path:
            resp = await _render_auth_gate(
                request,
                status_kind="unauthenticated",
                error_message="Некорректный или устаревший OAuth state. Попробуйте войти снова.",
                http_status=400,
            )
            resp.delete_cookie(discord_auth.OAUTH_STATE_COOKIE_NAME, path="/", secure=cookie_secure, httponly=True, samesite="lax")
            return resp

        try:
            profile = await discord_auth.exchange_discord_code(code, config)
        except Exception as exc:
            logger.warning("Discord OAuth callback error: %s", exc)
            resp = await _render_auth_gate(
                request,
                status_kind="unauthenticated",
                error_message="Не удалось подтвердить профиль в Discord API. Попробуйте снова.",
                http_status=502,
            )
            resp.delete_cookie(discord_auth.OAUTH_STATE_COOKIE_NAME, path="/", secure=cookie_secure, httponly=True, samesite="lax")
            return resp

        session_cookie = discord_auth.create_session_cookie(profile, config)
        allowed, role, _ = await logs_db.check_whitelist_access(
            profile["discord_id"], config.discord_superadmin_ids
        )
        ip = discord_auth.get_client_ip(request)
        await logs_db.record_audit_event(
            "login" if allowed else "login_denied",
            discord_id=profile["discord_id"],
            discord_username=profile["username"],
            target=role if allowed else "whitelist_missing",
            ip_address=ip,
            details=f"display_name={profile['display_name']}",
        )

        redirect = RedirectResponse(next_path, status_code=302)
        redirect.delete_cookie(discord_auth.OAUTH_STATE_COOKIE_NAME, path="/", secure=cookie_secure, httponly=True, samesite="lax")
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
        config = get_config_fn()
        status_kind, session, user = await discord_auth.resolve_auth_state(request, config)
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

    # -----------------------------------------------------------------------
    # HTML Views (/logs, /logs/round/..., /logs/search, /logs/admin)
    # -----------------------------------------------------------------------

    @router.get("/logs")
    async def logs_dashboard(
        request: Request,
        date: str = "",
        source: str = "",
        q: str = "",
        page: int = Query(default=1, ge=1),
        notice: str = "",
    ):
        user, err_resp = await _require_html_user(request)
        if err_resp is not None:
            return err_resp

        config = get_config_fn()
        shell = await get_shell_context_fn()
        per_page = 40
        offset = (page - 1) * per_page
        rounds, total_rounds = await logs_db.list_rounds(
            date_filter=date,
            source_filter=source,
            q=q,
            limit=per_page,
            offset=offset,
        )
        for r in rounds:
            r["size_label"] = _fmt_bytes(int(r.get("game_log_size") or 0) + int(r.get("qdel_log_size") or 0))
            r["uploaded_at_str"] = _fmt_ts_utc(r.get("uploaded_at"))

        stats = await logs_db.get_logs_stats()
        stats["total_size_label"] = _fmt_bytes(stats["total_bytes"])
        stats["last_indexed_str"] = _fmt_ts_utc(stats.get("last_indexed_at"))
        root_dir = config.resolved_logs_root_dir()
        root_dir_exists = bool(root_dir and root_dir.is_dir())
        total_pages = max(1, (total_rounds + per_page - 1) // per_page)

        return templates.TemplateResponse(
            request=request,
            name="logs_index.html",
            context={
                **shell,
                "user": user,
                "rounds": rounds,
                "total_rounds": total_rounds,
                "page": page,
                "total_pages": total_pages,
                "stats": stats,
                "date_filter": date,
                "source_filter": source,
                "q_filter": q,
                "notice": notice,
                "logs_root_dir": str(root_dir) if root_dir else "",
                "root_dir_exists": root_dir_exists,
                "max_upload_mb": config.logs_max_upload_mb,
            },
        )

    @router.get("/logs/round/{round_uid}")
    async def logs_round_view(
        request: Request,
        round_uid: str,
        file: str = "",
        preset: str = "gameplay",
        cat: List[str] = Query(default=[]),
        ckey: str = "",
        q: str = "",
        regex: str = "0",
        time_from: str = "",
        time_to: str = "",
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=200, ge=25, le=500),
        focus: Optional[int] = Query(default=None, ge=1),
        mask_ips: str = "0",
        html: str = "1",
        view: str = "stream",
        z: Optional[int] = Query(default=None, ge=1, le=99),
        tx: Optional[int] = Query(default=None, ge=1, le=255),
        ty: Optional[int] = Query(default=None, ge=1, le=255),
        qdel_view: str = "table",
        sort_by: str = "destroy_cost_ms",
        sort_dir: str = "desc",
    ):
        user, err_resp = await _require_html_user(request)
        if err_resp is not None:
            return err_resp

        config = get_config_fn()
        shell = await get_shell_context_fn()
        rnd = await logs_db.get_round_by_uid(round_uid)
        if rnd is None:
            raise HTTPException(status_code=404, detail="Раунд не найден в индексе логов.")

        available_files: List[str] = []
        if rnd.get("has_game_log"):
            available_files.append("game.log")
        if rnd.get("has_qdel_log"):
            available_files.append("qdel.log")
        if rnd.get("has_initialize_log"):
            available_files.append("initialize.log")

        selected_file = file if file in available_files else (available_files[0] if available_files else "game.log")
        file_path = logs_db.resolve_round_file_path(
            rnd,
            selected_file,
            root_dir=config.resolved_logs_root_dir(),
            uploads_dir=config.resolved_logs_uploads_dir(),
        )
        if file_path is None:
            raise HTTPException(status_code=404, detail=f"Файл {selected_file} не найден на диске.")

        if page == 1 and not q and not ckey and focus is None and request.headers.get("HX-Request") != "true":
            await logs_db.record_audit_event(
                "view_round",
                discord_id=user.discord_id,
                discord_username=user.username,
                target=f"{round_uid}:{selected_file}",
                ip_address=discord_auth.get_client_ip(request),
            )

        round_time = _round_time_vm(rnd)

        if selected_file == "qdel.log":
            qdel_report = await asyncio.to_thread(
                parse_qdel_log_file,
                file_path,
                q=q,
                sort_by=sort_by,
                sort_dir=sort_dir,
                include_raw=(qdel_view == "raw"),
            )
            return templates.TemplateResponse(
                request=request,
                name="logs_round.html",
                context={
                    **shell,
                    "user": user,
                    "round": rnd,
                    "round_time": round_time,
                    "available_files": available_files,
                    "selected_file": selected_file,
                    "is_qdel": True,
                    "qdel_report": qdel_report,
                    "qdel_view": qdel_view,
                    "q": q,
                    "sort_by": sort_by,
                    "sort_dir": sort_dir,
                    "presets": LOG_PRESETS,
                },
            )

        use_regex = _parse_bool_flag(regex, default=False)
        mask_flag = _parse_bool_flag(mask_ips, default=False)
        render_html = _parse_bool_flag(html, default=True)
        view_mode = "map" if view == "map" else "stream"
        selected_cats = [c.strip().upper() for c in cat if c and c.strip()]
        active_preset = preset if preset in LOG_PRESETS else "gameplay"
        cache_dir = config.resolved_maps_cache_dir()
        map_manifest = map_renderer.get_map_manifest(cache_dir)

        result = await asyncio.to_thread(
            filter_game_log_file,
            file_path,
            preset=active_preset,
            categories=selected_cats or None,
            ckey=ckey,
            q=q,
            use_regex=use_regex,
            time_from=time_from,
            time_to=time_to,
            page=page,
            per_page=per_page,
            focus_line=focus,
            mask_ips=mask_flag,
            render_html=render_html,
        )

        active_ckey_norm = normalize_ckey(ckey) if ckey else ""
        entries_vm = [
            _entry_to_vm(entry, cache_dir, focus=focus, active_ckey_norm=active_ckey_norm)
            for entry in result.entries
        ]

        categories_vm = _ordered_categories(
            result.category_counts,
            preset=active_preset,
            selected_cats=selected_cats,
        )
        query_pairs: List[Tuple[str, str]] = [
            ("file", selected_file),
            ("preset", active_preset),
            ("per_page", str(per_page)),
            ("html", "1" if render_html else "0"),
            ("mask_ips", "1" if mask_flag else "0"),
        ]
        for c in selected_cats:
            query_pairs.append(("cat", c))
        if ckey:
            query_pairs.append(("ckey", ckey))
        if q:
            query_pairs.append(("q", q))
        if use_regex:
            query_pairs.append(("regex", "1"))
        if time_from:
            query_pairs.append(("time_from", time_from))
        if time_to:
            query_pairs.append(("time_to", time_to))
        base_query_str = urlencode(query_pairs)

        template_name = (
            "partials/logs_round_stream.html"
            if request.headers.get("HX-Request") == "true" and view_mode == "stream"
            else "logs_round.html"
        )

        return templates.TemplateResponse(
            request=request,
            name=template_name,
            context={
                **shell,
                "user": user,
                "round": rnd,
                "round_time": round_time,
                "available_files": available_files,
                "selected_file": selected_file,
                "is_qdel": False,
                "entries": entries_vm,
                "result": result,
                "categories_vm": categories_vm,
                "selected_cats": set(selected_cats),
                "active_preset": active_preset,
                "presets": LOG_PRESETS,
                "ckey": ckey,
                "active_ckey_norm": active_ckey_norm,
                "q": q,
                "use_regex": use_regex,
                "time_from": time_from,
                "time_to": time_to,
                "per_page": per_page,
                "focus_line": focus,
                "mask_ips": mask_flag,
                "render_html": render_html,
                "view_mode": view_mode,
                "target_z": z,
                "target_x": tx,
                "target_y": ty,
                "map_manifest": map_manifest,
                "base_query_str": base_query_str,
            },
        )

    @router.get("/logs/round/{round_uid}/context")
    async def logs_round_context_partial(
        request: Request,
        round_uid: str,
        file: str = "game.log",
        line: int = Query(default=1, ge=1),
        radius: int = Query(default=20, ge=1, le=100),
        mask_ips: str = "0",
        html: str = "1",
        inline: str = "0",
    ):
        user = await _require_api_user(request)
        config = get_config_fn()
        rnd, file_path = await _resolve_round_and_file(round_uid, file)

        mask_flag = _parse_bool_flag(mask_ips, default=False)
        render_html = _parse_bool_flag(html, default=True)
        inline_flag = _parse_bool_flag(inline, default=False)
        context_entries = await asyncio.to_thread(
            read_context_lines,
            file_path,
            line,
            radius=radius,
            mask_ips=mask_flag,
            render_html=render_html,
        )
        cache_dir = config.resolved_maps_cache_dir()
        rows = [_entry_to_vm(entry, cache_dir, target_line=line) for entry in context_entries]
        return templates.TemplateResponse(
            request=request,
            name="partials/logs_context.html",
            context={
                "user": user,
                "round": rnd,
                "selected_file": file,
                "target_line": line,
                "radius": radius,
                "rows": rows,
                "inline": inline_flag,
            },
        )

    @router.get("/logs/search")
    async def logs_global_search(
        request: Request,
        q: str = "",
        ckey: str = "",
        preset: str = "gameplay",
        cat: List[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        round_uid: str = "",
        z_level: str = "",
        only_coords: str = "0",
        scan_debug: str = "0",
        mask_ips: str = "0",
        page: int = Query(default=1, ge=1),
    ):
        user, err_resp = await _require_html_user(request)
        if err_resp is not None:
            return err_resp

        config = get_config_fn()
        shell = await get_shell_context_fn()
        active_preset = preset if preset in LOG_PRESETS else "gameplay"
        selected_cats = [c.strip().upper() for c in cat if c and c.strip()]
        scan_debug_flag = _parse_bool_flag(scan_debug, default=False)
        only_coords_flag = _parse_bool_flag(only_coords, default=False)
        mask_flag = _parse_bool_flag(mask_ips, default=False)
        z_val: Optional[int] = None
        if z_level.strip():
            try:
                parsed_z = int(z_level.strip())
                if 1 <= parsed_z <= 99:
                    z_val = parsed_z
            except ValueError:
                z_val = None
        per_page = 100
        offset = (page - 1) * per_page

        has_query = bool(
            q.strip()
            or ckey.strip()
            or selected_cats
            or round_uid.strip()
            or date_from.strip()
            or date_to.strip()
            or z_val is not None
            or only_coords_flag
        )
        hits: List[Dict[str, Any]] = []
        total_hits = 0

        if has_query:
            await logs_db.record_audit_event(
                "search_global",
                discord_id=user.discord_id,
                discord_username=user.username,
                target=(q or ckey or round_uid)[:160],
                ip_address=discord_auth.get_client_ip(request),
                details=f"preset={active_preset}, scan_debug={scan_debug_flag}, z={z_val or ''}",
            )
            if scan_debug_flag:
                all_rounds, _ = await logs_db.list_rounds(limit=5000, offset=0)
                if round_uid.strip():
                    all_rounds = [r for r in all_rounds if r["round_uid"] == round_uid.strip()]
                raw_hits = await asyncio.to_thread(
                    scan_raw_files_for_query,
                    all_rounds,
                    query=q,
                    ckey=ckey,
                    preset=active_preset,
                    categories=selected_cats or None,
                    date_from=date_from,
                    date_to=date_to,
                    limit=per_page if (z_val is None and not only_coords_flag) else 500,
                )
                if z_val is not None:
                    raw_hits = [h for h in raw_hits if h.get("coord_z") == z_val]
                elif only_coords_flag:
                    raw_hits = [h for h in raw_hits if h.get("coord_z")]
                hits = raw_hits[:per_page]
                total_hits = len(hits)
            else:
                hits, total_hits = await logs_db.search_logs_fts(
                    query=q,
                    ckey=ckey,
                    preset=active_preset,
                    categories=selected_cats or None,
                    date_from=date_from,
                    date_to=date_to,
                    round_uid=round_uid,
                    z_level=z_val,
                    only_coords=only_coords_flag,
                    limit=per_page,
                    offset=offset,
                )

        cache_dir = config.resolved_maps_cache_dir()
        hits_vm = []
        for h in hits:
            msg = mask_sensitive_text(h["message"]) if mask_flag else h["message"]
            safe_html, _ = sanitize_byond_html(msg)
            meta = category_info(h["category"])
            cx, cy, cz = h.get("coord_x"), h.get("coord_y"), h.get("coord_z")
            coord_tuple = (int(cx), int(cy), int(cz)) if (cx and cy and cz) else None
            hits_vm.append(
                {
                    **h,
                    "cat_css": meta["css"],
                    "safe_html": safe_html,
                    "coords": _coord_vm(coord_tuple, cache_dir),
                }
            )

        total_pages = max(1, (total_hits + per_page - 1) // per_page)
        search_pairs: List[Tuple[str, str]] = [("preset", active_preset)]
        if q:
            search_pairs.append(("q", q))
        if ckey:
            search_pairs.append(("ckey", ckey))
        for c in selected_cats:
            search_pairs.append(("cat", c))
        if date_from:
            search_pairs.append(("date_from", date_from))
        if date_to:
            search_pairs.append(("date_to", date_to))
        if round_uid:
            search_pairs.append(("round_uid", round_uid))
        if z_val is not None:
            search_pairs.append(("z_level", str(z_val)))
        if only_coords_flag:
            search_pairs.append(("only_coords", "1"))
        if scan_debug_flag:
            search_pairs.append(("scan_debug", "1"))
        if mask_flag:
            search_pairs.append(("mask_ips", "1"))
        search_query_str = urlencode(search_pairs)

        return templates.TemplateResponse(
            request=request,
            name="logs_search.html",
            context={
                **shell,
                "user": user,
                "hits": hits_vm,
                "total_hits": total_hits,
                "has_query": has_query,
                "q": q,
                "ckey": ckey,
                "active_preset": active_preset,
                "presets": LOG_PRESETS,
                "all_categories": list(CATEGORY_META.keys()),
                "all_categories_vm": _ordered_categories({c: 0 for c in CATEGORY_META.keys()}),
                "selected_cats": set(selected_cats),
                "date_from": date_from,
                "date_to": date_to,
                "round_uid": round_uid,
                "z_level": z_val,
                "z_levels": map_renderer.SIERRA_Z_LEVELS,
                "only_coords": only_coords_flag,
                "scan_debug": scan_debug_flag,
                "mask_ips": mask_flag,
                "page": page,
                "total_pages": total_pages,
                "search_query_str": search_query_str,
            },
        )

    @router.get("/logs/admin")
    async def logs_admin_page(
        request: Request,
        action: str = "",
        notice: str = "",
        error: str = "",
    ):
        user, err_resp = await _require_html_user(request, require_admin=True)
        if err_resp is not None:
            return err_resp

        config = get_config_fn()
        shell = await get_shell_context_fn()
        whitelist = await logs_db.list_whitelist(config.discord_superadmin_ids)
        for item in whitelist:
            item["added_at_str"] = _fmt_ts_utc(item.get("added_at")) if item.get("added_at") else "config.json"

        events, total_events = await logs_db.get_audit_events(limit=100, offset=0, action_filter=action)
        for ev in events:
            ev["created_at_str"] = _fmt_ts_utc(ev.get("created_at"))
            ev["action_label"] = _audit_action_label(ev.get("action") or "")

        uploaded_rounds, _ = await logs_db.list_rounds(source_filter="upload", limit=100, offset=0)
        for ur in uploaded_rounds:
            ur["uploaded_at_str"] = _fmt_ts_utc(ur.get("uploaded_at"))
            ur["size_label"] = _fmt_bytes(int(ur.get("game_log_size") or 0) + int(ur.get("qdel_log_size") or 0))

        stats = await logs_db.get_logs_stats()
        stats["total_size_label"] = _fmt_bytes(stats["total_bytes"])
        stats["last_indexed_str"] = _fmt_ts_utc(stats.get("last_indexed_at"))
        map_manifest = map_renderer.get_map_manifest(config.resolved_maps_cache_dir())
        map_build_status = map_renderer.get_map_build_status()

        return templates.TemplateResponse(
            request=request,
            name="logs_admin.html",
            context={
                **shell,
                "user": user,
                "whitelist": whitelist,
                "audit_events": events,
                "audit_actions": list(_AUDIT_ACTION_LABELS.items()),
                "total_events": total_events,
                "action_filter": action,
                "uploaded_rounds": uploaded_rounds,
                "stats": stats,
                "index_include_debug": config.logs_index_include_debug,
                "map_manifest": map_manifest,
                "map_build_status": map_build_status,
                "maps_repo_dir": str(config.resolved_maps_repo_dir() or ""),
                "maps_github_repo": config.maps_github_repo,
                "maps_github_branch": config.maps_github_branch,
                "notice": notice,
                "error": error,
            },
        )

    # -----------------------------------------------------------------------
    # Mutations (CSRF Protected)
    # -----------------------------------------------------------------------

    @router.post("/logs/refresh")
    async def refresh_logs(
        request: Request,
        csrf_token: str = Form(default=""),
    ):
        user = await _require_api_user(request)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = get_config_fn()
        summary = await logs_db.sync_and_index_rounds(
            config.resolved_logs_root_dir(),
            config.resolved_logs_uploads_dir(),
            include_debug=config.logs_index_include_debug,
            force_reindex=False,
        )
        await logs_db.record_audit_event(
            "refresh",
            discord_id=user.discord_id,
            discord_username=user.username,
            ip_address=discord_auth.get_client_ip(request),
            details=f"discovered={summary['discovered']}, indexed={summary['indexed_rounds']}",
        )
        if _prefers_json(request):
            return {"ok": True, **summary}
        msg = quote(
            f"Список обновлён: {summary['discovered']} раундов, проиндексировано {summary['indexed_rounds']} ({summary['elapsed_seconds']} с)"
        )
        return RedirectResponse(f"/logs?notice={msg}", status_code=303)

    @router.post("/logs/reindex")
    async def reindex_logs(
        request: Request,
        csrf_token: str = Form(default=""),
        include_debug: str = Form(default="0"),
    ):
        user = await _require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = get_config_fn()
        inc_dbg = _parse_bool_flag(include_debug, default=config.logs_index_include_debug)
        summary = await logs_db.sync_and_index_rounds(
            config.resolved_logs_root_dir(),
            config.resolved_logs_uploads_dir(),
            include_debug=inc_dbg,
            force_reindex=True,
        )
        await logs_db.record_audit_event(
            "reindex",
            discord_id=user.discord_id,
            discord_username=user.username,
            ip_address=discord_auth.get_client_ip(request),
            details=f"include_debug={inc_dbg}, indexed_rounds={summary['indexed_rounds']}, lines={summary['indexed_lines']}",
        )
        if _prefers_json(request):
            return {"ok": True, **summary}
        msg = quote(f"Переиндексация завершена: {summary['indexed_rounds']} раундов, {summary['indexed_lines']} строк ({summary['elapsed_seconds']}с)")
        return RedirectResponse(f"/logs/admin?notice={msg}", status_code=303)

    @router.post("/logs/admin/maps/rebuild")
    async def admin_rebuild_maps(
        request: Request,
        source: str = Form(default="auto"),
        background: str = Form(default="0"),
        csrf_token: str = Form(default=""),
    ):
        user = await _require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = get_config_fn()
        prefer_gh = source.strip().lower() == "github"
        run_bg = _parse_bool_flag(background, default=False)
        if run_bg:
            started = map_renderer.start_map_rebuild_background(
                config,
                force=True,
                prefer_github=prefer_gh,
            )
            await logs_db.record_audit_event(
                "rebuild_maps",
                discord_id=user.discord_id,
                discord_username=user.username,
                ip_address=discord_auth.get_client_ip(request),
                details=f"background=1, prefer_github={prefer_gh}, started={started}",
            )
            if _prefers_json(request):
                return {
                    "ok": True,
                    "started": started,
                    "build_status": map_renderer.get_map_build_status(),
                }
            msg = quote(
                "Запущена фоновая сборка карты. Прогресс отображается ниже."
                if started
                else "Сборка карты уже выполняется в фоне."
            )
            return RedirectResponse(f"/logs/admin?notice={msg}", status_code=303)

        try:
            manifest = await map_renderer.build_map_cache(
                config,
                force=True,
                prefer_github=prefer_gh,
            )
        except Exception as exc:
            logger.warning("Map cache rebuild failed: %s", exc)
            if _prefers_json(request):
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            err = quote(f"Ошибка пересборки карты: {exc}")
            return RedirectResponse(f"/logs/admin?error={err}", status_code=303)

        await logs_db.record_audit_event(
            "rebuild_maps",
            discord_id=user.discord_id,
            discord_username=user.username,
            ip_address=discord_auth.get_client_ip(request),
            details=f"source={manifest.get('source_kind')}, levels={len(manifest.get('levels', []))}",
        )
        if _prefers_json(request):
            return {"ok": True, **manifest}
        msg = quote(
            f"Карта пересобрана ({manifest.get('source_kind')}, "
            f"{len(manifest.get('levels', []))} Z-уровней за {manifest.get('elapsed_seconds')}с)"
        )
        return RedirectResponse(f"/logs/admin?notice={msg}", status_code=303)

    @router.post("/logs/upload")
    async def upload_logs(
        request: Request,
        file: UploadFile = File(...),
        label: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        user = await _require_api_user(request)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = get_config_fn()

        max_upload_bytes = int(config.logs_max_upload_mb) * 1024 * 1024
        max_extracted_bytes = int(config.logs_max_extracted_mb) * 1024 * 1024
        chunks: List[bytes] = []
        total_read = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"Файл превышает максимальный размер ({config.logs_max_upload_mb} МБ).",
                )
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)

        try:
            created = await logs_db.ingest_uploaded_log(
                file.filename or "uploaded.log",
                raw_bytes,
                uploads_dir=config.resolved_logs_uploads_dir(),
                label=label,
                uploaded_by=user.discord_id,
                max_upload_bytes=max_upload_bytes,
                max_extracted_bytes=max_extracted_bytes,
                max_archive_files=int(config.logs_max_archive_files),
                include_debug=config.logs_index_include_debug,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        await logs_db.record_audit_event(
            "upload_round",
            discord_id=user.discord_id,
            discord_username=user.username,
            target=",".join(r["round_uid"] for r in created)[:180],
            ip_address=discord_auth.get_client_ip(request),
            details=f"filename={file.filename}, count={len(created)}",
        )
        if _prefers_json(request) or request.headers.get("x-requested-with") == "fetch":
            return {
                "ok": True,
                "rounds": [
                    {
                        "round_uid": r["round_uid"],
                        "round_id": r["round_id"],
                        "round_date": r["round_date"],
                        "url": f"/logs/round/{r['round_uid']}",
                    }
                    for r in created
                ],
            }
        if len(created) == 1:
            return RedirectResponse(f"/logs/round/{created[0]['round_uid']}", status_code=303)
        msg = quote(f"Загружено раундов: {len(created)}")
        return RedirectResponse(f"/logs?notice={msg}", status_code=303)

    @router.post("/logs/rounds/{round_uid}/delete")
    async def delete_upload_route(
        request: Request,
        round_uid: str,
        csrf_token: str = Form(default=""),
    ):
        user = await _require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = get_config_fn()
        try:
            deleted = await logs_db.delete_uploaded_round(
                round_uid, uploads_dir=config.resolved_logs_uploads_dir()
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail="Загруженный раунд не найден.")

        await logs_db.record_audit_event(
            "delete_upload",
            discord_id=user.discord_id,
            discord_username=user.username,
            target=round_uid,
            ip_address=discord_auth.get_client_ip(request),
        )
        if _prefers_json(request):
            return {"ok": True, "round_uid": round_uid}
        return RedirectResponse(f"/logs/admin?notice={quote('Загруженный лог удалён')}", status_code=303)

    @router.post("/logs/admin/whitelist/add")
    async def whitelist_add_route(
        request: Request,
        discord_id: str = Form(default=""),
        display_name: str = Form(default=""),
        role: str = Form(default="viewer"),
        csrf_token: str = Form(default=""),
    ):
        user = await _require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        # Only superadmins can grant admin role
        effective_role = role if (role == "admin" and user.is_superadmin) else "viewer"
        try:
            await logs_db.add_to_whitelist(
                discord_id,
                display_name=display_name,
                role=effective_role,
                added_by=user.discord_id,
            )
        except ValueError as exc:
            if _prefers_json(request):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return RedirectResponse(f"/logs/admin?error={quote(str(exc))}", status_code=303)

        await logs_db.record_audit_event(
            "whitelist_add",
            discord_id=user.discord_id,
            discord_username=user.username,
            target=discord_id.strip(),
            ip_address=discord_auth.get_client_ip(request),
            details=f"role={effective_role}, note={display_name.strip()[:60]}",
        )
        if _prefers_json(request):
            return {"ok": True, "discord_id": discord_id.strip(), "role": effective_role}
        return RedirectResponse(f"/logs/admin?notice={quote('Доступ выдан')}", status_code=303)

    @router.post("/logs/admin/whitelist/remove")
    async def whitelist_remove_route(
        request: Request,
        discord_id: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        user = await _require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = get_config_fn()
        try:
            removed = await logs_db.remove_from_whitelist(discord_id, config.discord_superadmin_ids)
        except ValueError as exc:
            if _prefers_json(request):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return RedirectResponse(f"/logs/admin?error={quote(str(exc))}", status_code=303)

        await logs_db.record_audit_event(
            "whitelist_remove",
            discord_id=user.discord_id,
            discord_username=user.username,
            target=discord_id.strip(),
            ip_address=discord_auth.get_client_ip(request),
            details=f"removed={removed}",
        )
        if _prefers_json(request):
            return {"ok": True, "removed": removed}
        return RedirectResponse(f"/logs/admin?notice={quote('Доступ отозван')}", status_code=303)

    # -----------------------------------------------------------------------
    # Protected JSON & Download API (/api/v1/logs/...)
    # -----------------------------------------------------------------------

    @router.get("/api/v1/logs/rounds")
    async def api_logs_rounds(
        request: Request,
        date: str = "",
        source: str = "",
        q: str = "",
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        await _require_api_user(request)
        rounds, total = await logs_db.list_rounds(
            date_filter=date, source_filter=source, q=q, limit=limit, offset=offset
        )
        return {"total": total, "rounds": rounds}

    @router.get("/api/v1/logs/rounds/{round_uid}/entries")
    async def api_logs_entries(
        request: Request,
        round_uid: str,
        file: str = "game.log",
        preset: str = "gameplay",
        cat: List[str] = Query(default=[]),
        ckey: str = "",
        q: str = "",
        regex: bool = False,
        time_from: str = "",
        time_to: str = "",
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=200, ge=25, le=500),
        focus: Optional[int] = Query(default=None, ge=1),
        mask_ips: bool = False,
    ):
        await _require_api_user(request)
        config = get_config_fn()
        rnd, file_path = await _resolve_round_and_file(round_uid, file)

        res = await asyncio.to_thread(
            filter_game_log_file,
            file_path,
            preset=preset,
            categories=cat or None,
            ckey=ckey,
            q=q,
            use_regex=regex,
            time_from=time_from,
            time_to=time_to,
            page=page,
            per_page=per_page,
            focus_line=focus,
            mask_ips=mask_ips,
            render_html=False,
        )
        return {
            "round_uid": round_uid,
            "file": file,
            "page": res.page,
            "per_page": res.per_page,
            "total_pages": res.total_pages,
            "total_matches": res.total_matches,
            "total_lines": res.total_lines,
            "category_counts": res.category_counts,
            "regex_error": res.regex_error,
            "entries": [
                {
                    "line_no": e.line_no,
                    "timestamp": e.timestamp,
                    "category": e.category,
                    "message": e.plain_message,
                    "ckeys": e.ckeys,
                    "is_continuation": e.is_continuation,
                }
                for e in res.entries
            ],
        }

    @router.get("/api/v1/logs/rounds/{round_uid}/qdel")
    async def api_logs_qdel(
        request: Request,
        round_uid: str,
        q: str = "",
        sort_by: str = "destroy_cost_ms",
        sort_dir: str = "desc",
    ):
        await _require_api_user(request)
        _rnd, file_path = await _resolve_round_and_file(round_uid, "qdel.log")
        report = await asyncio.to_thread(
            parse_qdel_log_file, file_path, q=q, sort_by=sort_by, sort_dir=sort_dir, include_raw=False
        )
        return {
            "round_uid": round_uid,
            "total_paths": report.total_paths,
            "total_qdels": report.total_qdels,
            "total_unnecessary": report.total_unnecessary,
            "total_destroy_ms": report.total_destroy_ms,
            "entries": [
                {
                    "line_no": e.line_no,
                    "timestamp": e.timestamp,
                    "path": e.path,
                    "qdel_count": e.qdel_count,
                    "unnecessary_count": e.unnecessary_count,
                    "destroy_cost_ms": e.destroy_cost_ms,
                }
                for e in report.entries
            ],
        }

    @router.get("/api/v1/logs/rounds/{round_uid}/context")
    async def api_logs_context(
        request: Request,
        round_uid: str,
        file: str = "game.log",
        line: int = Query(default=1, ge=1),
        radius: int = Query(default=20, ge=1, le=100),
        mask_ips: bool = False,
    ):
        await _require_api_user(request)
        config = get_config_fn()
        rnd, file_path = await _resolve_round_and_file(round_uid, file)
        entries = await asyncio.to_thread(
            read_context_lines, file_path, line, radius=radius, mask_ips=mask_ips, render_html=False
        )
        return {
            "round_uid": round_uid,
            "target_line": line,
            "entries": [
                {
                    "line_no": e.line_no,
                    "timestamp": e.timestamp,
                    "category": e.category,
                    "message": e.plain_message,
                    "ckeys": e.ckeys,
                }
                for e in entries
            ],
        }

    @router.get("/api/v1/logs/rounds/{round_uid}/download")
    async def api_logs_download(
        request: Request,
        round_uid: str,
        file: str = "game.log",
        mode: str = "filtered",
        preset: str = "gameplay",
        cat: List[str] = Query(default=[]),
        ckey: str = "",
        q: str = "",
        regex: bool = False,
        time_from: str = "",
        time_to: str = "",
        mask_ips: bool = False,
    ):
        user = await _require_api_user(request)
        rnd, file_path = await _resolve_round_and_file(round_uid, file)

        await logs_db.record_audit_event(
            "download_log",
            discord_id=user.discord_id,
            discord_username=user.username,
            target=f"{round_uid}:{file}",
            ip_address=discord_auth.get_client_ip(request),
            details=f"mode={mode}, preset={preset}, mask_ips={mask_ips}",
        )

        if mode == "raw" or file == "qdel.log":
            content_bytes = file_path.read_bytes()
            if mask_ips:
                from core.log_parser import decode_log_bytes
                content_bytes = mask_sensitive_text(decode_log_bytes(content_bytes)).encode("utf-8")
            dl_name = f"round-{rnd['round_id']}-{file}"
        else:
            try:
                text = await asyncio.to_thread(
                    export_filtered_game_log,
                    file_path,
                    preset=preset,
                    categories=cat or None,
                    ckey=ckey,
                    q=q,
                    use_regex=regex,
                    time_from=time_from,
                    time_to=time_to,
                    mask_ips=mask_ips,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            content_bytes = text.encode("utf-8")
            dl_name = f"round-{rnd['round_id']}-{preset}-{file}"

        safe_dl_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in dl_name)
        return Response(
            content=content_bytes,
            media_type="text/plain; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_dl_name}"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/logs/maps/manifest")
    async def logs_maps_manifest(request: Request):
        await _require_api_user(request)
        config = get_config_fn()
        manifest = map_renderer.get_map_manifest(config.resolved_maps_cache_dir())
        return {**manifest, "build_status": map_renderer.get_map_build_status()}

    @router.get("/logs/maps/z/{z_level}.png")
    async def logs_map_tile_image(request: Request, z_level: int):
        await _require_api_user(request)
        config = get_config_fn()
        img_path = map_renderer.get_map_image_path(config.resolved_maps_cache_dir(), z_level)
        if img_path is None:
            raise HTTPException(status_code=404, detail="Изображение карты для этого Z-уровня не найдено.")
        return FileResponse(
            img_path,
            media_type="image/png",
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/logs/maps/areas/{z_level}.json")
    async def logs_map_areas_json(request: Request, z_level: int):
        await _require_api_user(request)
        config = get_config_fn()
        areas_path = map_renderer.get_map_areas_path(config.resolved_maps_cache_dir(), z_level)
        if areas_path is None:
            raise HTTPException(status_code=404, detail="Данные отсеков для этого Z-уровня не найдены.")
        return FileResponse(
            areas_path,
            media_type="application/json; charset=utf-8",
            headers={
                "Cache-Control": "private, max-age=3600",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.get("/api/v1/logs/rounds/{round_uid}/map-events")
    async def api_logs_round_map_events(
        request: Request,
        round_uid: str,
        file: str = "game.log",
        preset: str = "gameplay",
        cat: List[str] = Query(default=[]),
        ckey: str = "",
        q: str = "",
        regex: bool = False,
        time_from: str = "",
        time_to: str = "",
        mask_ips: bool = False,
        z: Optional[int] = Query(default=None, ge=1, le=99),
        limit: int = Query(default=5000, ge=1, le=10000),
    ):
        await _require_api_user(request)
        config = get_config_fn()
        rnd, file_path = await _resolve_round_and_file(round_uid, file)
        if file == "qdel.log":
            raise HTTPException(status_code=400, detail="Координаты карты поддерживаются только для game.log / initialize.log.")

        selected_cats = [c.strip().upper() for c in cat if c and c.strip()]
        active_preset = preset if preset in LOG_PRESETS else "gameplay"
        payload = await asyncio.to_thread(
            collect_round_map_events,
            file_path,
            preset=active_preset,
            categories=selected_cats or None,
            ckey=ckey,
            q=q,
            use_regex=regex,
            time_from=time_from,
            time_to=time_to,
            mask_ips=mask_ips,
            render_html=True,
            z_filter=z,
            limit=limit,
        )
        if payload.get("regex_error"):
            raise HTTPException(status_code=400, detail=payload["regex_error"])

        cache_dir = config.resolved_maps_cache_dir()
        for ev in payload["events"]:
            ev["area"] = map_renderer.lookup_area_name(cache_dir, ev["z"], ev["x"], ev["y"])
            ev["z_label"] = map_renderer.deck_short_label(ev["z"])

        manifest = map_renderer.get_map_manifest(cache_dir)
        return {
            "round_uid": round_uid,
            "round_id": rnd["round_id"],
            "file": file,
            "manifest": manifest,
            **payload,
        }

    @router.get("/api/v1/logs/search")
    async def api_logs_search(
        request: Request,
        q: str = "",
        ckey: str = "",
        preset: str = "gameplay",
        cat: List[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        round_uid: str = "",
        z_level: Optional[int] = Query(default=None, ge=1, le=99),
        only_coords: bool = False,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ):
        await _require_api_user(request)
        config = get_config_fn()
        cache_dir = config.resolved_maps_cache_dir()
        hits, total = await logs_db.search_logs_fts(
            query=q,
            ckey=ckey,
            preset=preset,
            categories=cat or None,
            date_from=date_from,
            date_to=date_to,
            round_uid=round_uid,
            z_level=z_level,
            only_coords=only_coords,
            limit=limit,
            offset=offset,
        )
        for h in hits:
            cx, cy, cz = h.get("coord_x"), h.get("coord_y"), h.get("coord_z")
            if cx and cy and cz:
                h["area"] = map_renderer.lookup_area_name(cache_dir, int(cz), int(cx), int(cy))
        return {"total": total, "hits": hits}

    return router
