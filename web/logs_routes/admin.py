"""Admin page, log refresh/reindex, map rebuild, archive upload/delete, and whitelist management routes."""

from __future__ import annotations

import logging
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse

from core import discord_auth, logs_db, map_renderer
from web.logs_routes.deps import LogsRouteContext
from web.logs_routes.viewmodels import (
    _AUDIT_ACTION_LABELS,
    _audit_action_label,
    _fmt_bytes,
    _fmt_ts_utc,
    _parse_bool_flag,
    _prefers_json,
)

logger = logging.getLogger(__name__)


def register_admin_routes(router: APIRouter, ctx: LogsRouteContext) -> None:
    @router.get("/logs/admin")
    async def logs_admin_page(
        request: Request,
        action: str = "",
        notice: str = "",
        error: str = "",
    ):
        user, err_resp = await ctx.require_html_user(request, require_admin=True)
        if err_resp is not None:
            return err_resp

        config = ctx.get_config_fn()
        shell = await ctx.get_shell_context_fn()
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

        return ctx.templates.TemplateResponse(
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

    @router.post("/logs/refresh")
    async def refresh_logs(
        request: Request,
        csrf_token: str = Form(default=""),
    ):
        user = await ctx.require_api_user(request)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = ctx.get_config_fn()
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
        user = await ctx.require_api_user(request, require_admin=True)
        discord_auth.verify_csrf(user, csrf_token or request.headers.get("x-csrf-token"))
        config = ctx.get_config_fn()
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
