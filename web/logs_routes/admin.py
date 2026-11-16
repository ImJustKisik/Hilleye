"""Admin page, log refresh/reindex, map rebuild, archive upload/delete, and whitelist management routes."""

from __future__ import annotations

import csv
import io
import logging
import math
import time
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlencode

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import RedirectResponse, Response, JSONResponse

from core import discord_auth, logs_db, map_renderer
from core.log_operations import heavy_slot, finish_thread, staging_directory
from core.log_jobs import map_job_status
import asyncio
from web.logs_routes.deps import LogsRouteContext
from web.logs_routes.viewmodels import (
    _AUDIT_ACTION_GROUPS,
    _AUDIT_ACTION_LABELS,
    _enrich_audit_event,
    _fmt_bytes,
    _fmt_relative_ru,
    _fmt_ts_utc,
    _parse_bool_flag,
    _prefers_json,
)

logger = logging.getLogger(__name__)


async def _index_job(request, config, user, kind, payload, background):
    runner = getattr(request.app.state, "log_jobs", None)
    if runner is not None and runner.path == config.resolved_logs_db_path():
        try:
            job_id = await runner.submit(kind, user.discord_id, payload)
        except OverflowError as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc
        if background:
            await logs_db.record_audit_event(kind, discord_id=user.discord_id, target=job_id, details="background=1")
            return JSONResponse({"ok": True, "job_id": job_id}, status_code=202)
        return await runner.wait(job_id)
    if background:
        raise HTTPException(503, "Фоновый обработчик ещё не готов.")
    return await logs_db.sync_and_index_rounds(config.resolved_logs_root_dir(), config.resolved_logs_uploads_dir(),
                                              include_debug=payload.get("include_debug", config.logs_index_include_debug),
                                              force_reindex=kind == "reindex")


def _csv_safe_cell(val: Any) -> str:
    text = "" if val is None else str(val)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _resolve_audit_group(group: str, action: str) -> Tuple[str, List[str]]:
    if action:
        return "", []
    g = (group or "").strip().lower()
    if g in _AUDIT_ACTION_GROUPS:
        return g, _AUDIT_ACTION_GROUPS[g][1]
    return "", []


async def _build_audit_vm(
    *,
    action: str = "",
    group: str = "",
    audit_q: str = "",
    audit_user: str = "",
    audit_ip: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    per_page: int = 50,
    whitelist_ids: Optional[set] = None,
) -> Dict[str, Any]:
    now_ts = time.time()
    clean_action = (action or "").strip()
    active_group, group_actions = _resolve_audit_group(group, clean_action)
    offset = (max(1, page) - 1) * per_page

    events, total_events = await logs_db.get_audit_events(
        limit=per_page,
        offset=offset,
        action_filter=clean_action,
        actions=group_actions or None,
        q=audit_q,
        user_filter=audit_user,
        ip_filter=audit_ip,
        date_from=date_from,
        date_to=date_to,
    )
    total_pages = max(1, math.ceil(total_events / per_page)) if total_events else 1
    current_page = min(max(1, page), total_pages)
    if current_page != page and total_events > 0:
        offset = (current_page - 1) * per_page
        events, total_events = await logs_db.get_audit_events(
            limit=per_page,
            offset=offset,
            action_filter=clean_action,
            actions=group_actions or None,
            q=audit_q,
            user_filter=audit_user,
            ip_filter=audit_ip,
            date_from=date_from,
            date_to=date_to,
        )

    wl_set = whitelist_ids if whitelist_ids is not None else set()
    for ev in events:
        _enrich_audit_event(ev, whitelist_ids=wl_set, now_ts=now_ts)

    filter_params: Dict[str, Any] = {}
    if clean_action:
        filter_params["action"] = clean_action
    if active_group:
        filter_params["group"] = active_group
    if audit_q.strip():
        filter_params["audit_q"] = audit_q.strip()
    if audit_user.strip():
        filter_params["audit_user"] = audit_user.strip()
    if audit_ip.strip():
        filter_params["audit_ip"] = audit_ip.strip()
    if date_from.strip():
        filter_params["date_from"] = date_from.strip()
    if date_to.strip():
        filter_params["date_to"] = date_to.strip()
    if per_page != 50:
        filter_params["per_page"] = per_page

    base_qs = urlencode(filter_params)
    without_user = {k: v for k, v in filter_params.items() if k != "audit_user"}
    without_ip = {k: v for k, v in filter_params.items() if k != "audit_ip"}
    clear_user_url = "/logs/admin" + (f"?{urlencode(without_user)}" if without_user else "")
    clear_ip_url = "/logs/admin" + (f"?{urlencode(without_ip)}" if without_ip else "")
    has_filters = bool(
        clean_action
        or active_group
        or audit_q.strip()
        or audit_user.strip()
        or audit_ip.strip()
        or date_from.strip()
        or date_to.strip()
    )
    showing_from = offset + 1 if total_events > 0 else 0
    showing_to = min(offset + len(events), total_events)

    return {
        "audit_events": events,
        "audit_actions": list(_AUDIT_ACTION_LABELS.items()),
        "audit_groups": [(k, v[0]) for k, v in _AUDIT_ACTION_GROUPS.items()],
        "total_events": total_events,
        "action_filter": clean_action,
        "group_filter": active_group,
        "audit_q": audit_q.strip(),
        "audit_user": audit_user.strip(),
        "audit_ip": audit_ip.strip(),
        "date_from": date_from.strip(),
        "date_to": date_to.strip(),
        "audit_page": current_page,
        "audit_per_page": per_page,
        "audit_total_pages": total_pages,
        "audit_showing_from": showing_from,
        "audit_showing_to": showing_to,
        "audit_base_qs": base_qs,
        "audit_clear_user_url": clear_user_url,
        "audit_clear_ip_url": clear_ip_url,
        "audit_has_filters": has_filters,
    }


def register_admin_routes(router: APIRouter, ctx: LogsRouteContext) -> None:
    @router.get("/logs/admin")
    async def logs_admin_page(
        request: Request,
        action: str = "",
        group: str = "",
        audit_q: str = "",
        audit_user: str = "",
        audit_ip: str = "",
        date_from: str = "",
        date_to: str = "",
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=25, ge=10, le=200),
        partial: str = "",
        notice: str = "",
        error: str = "",
    ):
        user, err_resp = await ctx.require_html_user(request, require_admin=True)
        if err_resp is not None:
            return err_resp

        config = ctx.get_config_fn()
        whitelist = await logs_db.list_whitelist(config.discord_superadmin_ids)
        whitelist_ids: Set[str] = {str(item.get("discord_id") or "") for item in whitelist if item.get("discord_id")}
        audit_summary = await logs_db.get_audit_summary()
        last_seen_map: Dict[str, float] = audit_summary.get("last_seen_by_id") or {}

        audit_vm = await _build_audit_vm(
            action=action,
            group=group,
            audit_q=audit_q,
            audit_user=audit_user,
            audit_ip=audit_ip,
            date_from=date_from,
            date_to=date_to,
            page=page,
            per_page=per_page,
            whitelist_ids=whitelist_ids,
        )

        is_audit_partial = (
            partial == "audit"
            or (
                request.headers.get("hx-request", "").lower() == "true"
                and request.headers.get("hx-target", "") == "admin-audit-container"
            )
        )
        if is_audit_partial:
            return ctx.templates.TemplateResponse(
                request=request,
                name="partials/logs_admin_audit.html",
                context={
                    "user": user,
                    "audit_summary": audit_summary,
                    **audit_vm,
                },
            )

        shell = await ctx.get_shell_context_fn()
        now_ts = time.time()
        superadmin_count = 0
        admin_count = 0
        viewer_count = 0
        for item in whitelist:
            did = str(item.get("discord_id") or "")
            if item.get("is_config_superadmin") or item.get("role") == "superadmin":
                item["effective_role"] = "superadmin"
                superadmin_count += 1
            elif item.get("role") == "admin":
                item["effective_role"] = "admin"
                admin_count += 1
            else:
                item["effective_role"] = "viewer"
                viewer_count += 1
            item["added_at_str"] = _fmt_ts_utc(item.get("added_at")) if item.get("added_at") else "config.json"
            item["added_at_rel"] = _fmt_relative_ru(item.get("added_at"), now=now_ts) if item.get("added_at") else "конфиг"
            last_seen = last_seen_map.get(did)
            item["last_seen_str"] = _fmt_ts_utc(last_seen) if last_seen else "—"
            item["last_seen_rel"] = _fmt_relative_ru(last_seen, now=now_ts) if last_seen else "не заходил"

        wl_name_by_id = {
            str(item.get("discord_id") or ""): (item.get("display_name") or item.get("discord_id") or "")
            for item in whitelist
        }

        uploaded_rounds, _ = await logs_db.list_rounds(source_filter="upload", limit=100, offset=0)
        for ur in uploaded_rounds:
            ur["uploaded_at_str"] = _fmt_ts_utc(ur.get("uploaded_at"))
            ur["uploaded_at_rel"] = _fmt_relative_ru(ur.get("uploaded_at"), now=now_ts)
            ur["size_label"] = _fmt_bytes(int(ur.get("game_log_size") or 0) + int(ur.get("qdel_log_size") or 0))
            ub_id = str(ur.get("uploaded_by") or "")
            ur["uploaded_by_name"] = wl_name_by_id.get(ub_id, ub_id or "—")

        stats = await logs_db.get_logs_stats()
        stats["total_size_label"] = _fmt_bytes(stats["total_bytes"])
        stats["last_indexed_str"] = _fmt_ts_utc(stats.get("last_indexed_at"))
        stats["last_indexed_rel"] = _fmt_relative_ru(stats.get("last_indexed_at"), now=now_ts)
        index_errors = await logs_db.get_index_error_rounds(limit=50)
        map_manifest = map_renderer.get_map_manifest(config.resolved_maps_cache_dir())
        map_build_status = await asyncio.to_thread(map_job_status, config.resolved_logs_db_path()) or map_renderer.get_map_build_status()

        return ctx.templates.TemplateResponse(
            request=request,
            name="logs_admin.html",
            context={
                **shell,
                "user": user,
                "whitelist": whitelist,
                "whitelist_counts": {
                    "total": len(whitelist),
                    "superadmin": superadmin_count,
                    "admin": admin_count,
                    "viewer": viewer_count,
                },
                "audit_summary": audit_summary,
                **audit_vm,
                "uploaded_rounds": uploaded_rounds,
                "stats": stats,
                "index_errors": index_errors,
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

    @router.get("/logs/admin/audit/export")
    async def export_audit_csv(
        request: Request,
        action: str = "",
        group: str = "",
        audit_q: str = "",
        audit_user: str = "",
        audit_ip: str = "",
        date_from: str = "",
        date_to: str = "",
    ):
        await ctx.require_api_user(request, require_admin=True)
        clean_action = (action or "").strip()
        _active_group, group_actions = _resolve_audit_group(group, clean_action)
        events, _ = await logs_db.get_audit_events(
            limit=500,
            offset=0,
            action_filter=clean_action,
            actions=group_actions or None,
            q=audit_q,
            user_filter=audit_user,
            ip_filter=audit_ip,
            date_from=date_from,
            date_to=date_to,
        )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "timestamp_utc", "discord_id", "discord_username", "action", "target", "ip_address", "details"])
        for ev in events:
            writer.writerow(
                [
                    ev.get("id"),
                    _fmt_ts_utc(ev.get("created_at")),
                    _csv_safe_cell(ev.get("discord_id") or ""),
                    _csv_safe_cell(ev.get("discord_username") or ""),
                    _csv_safe_cell(ev.get("action") or ""),
                    _csv_safe_cell(ev.get("target") or ""),
                    _csv_safe_cell(ev.get("ip_address") or ""),
                    _csv_safe_cell(ev.get("details") or ""),
                ]
            )
        csv_bytes = ("\ufeff" + buf.getvalue()).encode("utf-8")
        return Response(
            content=csv_bytes,
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": 'attachment; filename="hilleye-audit.csv"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/logs/refresh")
    async def refresh_logs(
        request: Request,
        csrf_token: str = Form(default=""),
        next_url: str = Form(default="/logs"),
        background: str = Form(default="0"),
    ):
        user = await ctx.require_api_user(request)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = ctx.get_config_fn()
        summary = await _index_job(request, config, user, "refresh", {}, _parse_bool_flag(background))
        if isinstance(summary, Response):
            return summary
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
        target_path = "/logs/admin" if (next_url or "").strip() == "/logs/admin" and user.is_admin else "/logs"
        return RedirectResponse(f"{target_path}?notice={msg}", status_code=303)

    @router.post("/logs/reindex")
    async def reindex_logs(
        request: Request,
        csrf_token: str = Form(default=""),
        include_debug: str = Form(default="0"),
        background: str = Form(default="0"),
    ):
        user = await ctx.require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = ctx.get_config_fn()
        inc_dbg = _parse_bool_flag(include_debug, default=config.logs_index_include_debug)
        summary = await _index_job(request, config, user, "reindex", {"include_debug": inc_dbg}, _parse_bool_flag(background))
        if isinstance(summary, Response):
            return summary
        await logs_db.record_audit_event(
            "reindex",
            discord_id=user.discord_id,
            discord_username=user.username,
            ip_address=discord_auth.get_client_ip(request),
            details=f"include_debug={inc_dbg}, indexed_rounds={summary['indexed_rounds']}, lines={summary['indexed_lines']}",
        )
        if _prefers_json(request):
            return {"ok": True, **summary}
        msg = quote(
            f"Переиндексация завершена: {summary['indexed_rounds']} раундов, {summary['indexed_lines']} строк ({summary['elapsed_seconds']}с)"
        )
        return RedirectResponse(f"/logs/admin?notice={msg}", status_code=303)

    @router.post("/logs/admin/maps/rebuild")
    async def admin_rebuild_maps(
        request: Request,
        source: str = Form(default="auto"),
        background: str = Form(default="0"),
        csrf_token: str = Form(default=""),
    ):
        user = await ctx.require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = ctx.get_config_fn()
        prefer_gh = source.strip().lower() == "github"
        run_bg = _parse_bool_flag(background, default=False)
        runner = getattr(request.app.state, "log_jobs", None)
        if runner is not None and runner.path == config.resolved_logs_db_path():
            try:
                job_id = await runner.submit("map_rebuild", user.discord_id, {"force": True, "prefer_github": prefer_gh})
            except OverflowError as exc:
                raise HTTPException(429, str(exc), headers={"Retry-After": "2"}) from exc
            await logs_db.record_audit_event("rebuild_maps", discord_id=user.discord_id, target=job_id)
            if run_bg:
                if _prefers_json(request):
                    return {"ok": True, "started": True, "job_id": job_id}
                return RedirectResponse(f"/logs/admin?notice={quote('Сборка карты запущена')}", status_code=303)
            manifest = await runner.wait(job_id)
            if _prefers_json(request):
                return {"ok": True, **manifest}
            return RedirectResponse(f"/logs/admin?notice={quote('Карта пересобрана')}", status_code=303)
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
        user = await ctx.require_api_user(request)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = ctx.get_config_fn()

        max_upload_bytes = int(config.logs_max_upload_mb) * 1024 * 1024
        max_extracted_bytes = int(config.logs_max_extracted_mb) * 1024 * 1024
        try:
            async with heavy_slot(config):
                async with staging_directory(prefix="hilleye-upload-") as staging:
                    source = staging / "input"
                    total_read = 0
                    with source.open("wb") as stream:
                        while chunk := await file.read(1024 * 1024):
                            total_read += len(chunk)
                            if total_read > max_upload_bytes:
                                raise HTTPException(413, f"Файл превышает максимальный размер ({config.logs_max_upload_mb} МБ).")
                            await finish_thread(stream.write, chunk)
                    created = await logs_db.ingest_uploaded_log(
                        file.filename or "uploaded.log", source,
                        uploads_dir=config.resolved_logs_uploads_dir(), label=label,
                        uploaded_by=user.discord_id, max_upload_bytes=max_upload_bytes,
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
        user = await ctx.require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = ctx.get_config_fn()
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
        user = await ctx.require_api_user(request, require_admin=True)
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
        user = await ctx.require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = ctx.get_config_fn()
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
