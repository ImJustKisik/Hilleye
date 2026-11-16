"""Protected JSON API, log download endpoints, and station map asset routes."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response

from core import discord_auth, logs_db, map_renderer
from core.log_operations import run_log_operation, finish_thread
from core.logs_db.reader import read_log_page
from core.log_jobs import get_job, logs_status, map_job_status
from core.log_parser.filters import export_filtered_game_log_to_file, mask_log_to_file
from core.log_parser import (
    LOG_PRESETS,
    collect_round_map_events,
    decode_log_bytes,
    export_filtered_game_log,
    filter_game_log_file,
    mask_sensitive_text,
    parse_qdel_log_file,
    read_context_lines,
)
from web.logs_routes.deps import LogsRouteContext


class _ExportResponse(FileResponse):
    def __init__(self, *args, staging, **kwargs):
        super().__init__(*args, **kwargs)
        self.staging = staging

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await finish_thread(self.staging.cleanup)


def register_api_routes(router: APIRouter, ctx: LogsRouteContext) -> None:
    @router.get("/api/v1/logs/jobs/{job_id}")
    async def api_log_job(request: Request, job_id: str):
        user = await ctx.require_api_user(request)
        job = await asyncio.to_thread(get_job, ctx.get_config_fn().resolved_logs_db_path(), job_id,
                                      user_id=user.discord_id, admin=user.is_admin)
        if job is None:
            raise HTTPException(404, "Задача не найдена.")
        return job

    @router.get("/api/v1/logs/status")
    async def api_log_status(request: Request):
        user = await ctx.require_api_user(request)
        result = await asyncio.to_thread(logs_status, ctx.get_config_fn().resolved_logs_db_path())
        if not user.is_admin:
            result.pop("running", None)
        return result

    @router.get("/api/v1/logs/rounds")
    async def api_logs_rounds(
        request: Request,
        date: str = "",
        source: str = "",
        q: str = "",
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        await ctx.require_api_user(request)
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
        radius: int = Query(default=0, ge=0, le=100),
        q: str = "",
        regex: bool = False,
        time_from: str = "",
        time_to: str = "",
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=200, ge=25, le=500),
        focus: Optional[int] = Query(default=None, ge=1),
        mask_ips: bool = False,
    ):
        await ctx.require_api_user(request)
        _rnd, file_path = await ctx.resolve_round_and_file(round_uid, file)

        res = await read_log_page(
            request, ctx.get_config_fn(), round_uid, file_path,
            preset=preset,
            categories=cat or None,
            ckey=ckey,
            radius=radius,
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
        await ctx.require_api_user(request)
        _rnd, file_path = await ctx.resolve_round_and_file(round_uid, "qdel.log")
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
        await ctx.require_api_user(request)
        _rnd, file_path = await ctx.resolve_round_and_file(round_uid, file)
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
        radius: int = Query(default=0, ge=0, le=100),
        q: str = "",
        regex: bool = False,
        time_from: str = "",
        time_to: str = "",
        mask_ips: bool = False,
    ):
        user = await ctx.require_api_user(request)
        rnd, file_path = await ctx.resolve_round_and_file(round_uid, file)

        await logs_db.record_audit_event(
            "download_log",
            discord_id=user.discord_id,
            discord_username=user.username,
            target=f"{round_uid}:{file}",
            ip_address=discord_auth.get_client_ip(request),
            details=f"mode={mode}, preset={preset}, mask_ips={mask_ips}",
        )

        raw = mode == "raw" or file == "qdel.log"
        dl_name = f"round-{rnd['round_id']}-{file}" if raw else f"round-{rnd['round_id']}-{preset}-{file}"
        safe_dl_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in dl_name)
        if raw and not mask_ips:
            return FileResponse(file_path, media_type="text/plain; charset=utf-8", filename=safe_dl_name)
        staging = tempfile.TemporaryDirectory(prefix="hilleye-export-")
        output_path = Path(staging.name) / "export.log"
        try:
            if raw:
                await run_log_operation(request, ctx.get_config_fn(), mask_log_to_file, file_path, output_path)
            else:
                await run_log_operation(
                    request, ctx.get_config_fn(), export_filtered_game_log_to_file,
                    file_path, output_path, preset=preset, categories=cat or None,
                    ckey=ckey, radius=radius, q=q, use_regex=regex,
                    time_from=time_from, time_to=time_to, mask_ips=mask_ips,
                )
            return _ExportResponse(
                output_path, media_type="text/plain; charset=utf-8", filename=safe_dl_name,
                staging=staging,
            )
        except BaseException as exc:
            await finish_thread(staging.cleanup)
            if isinstance(exc, ValueError):
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise

    @router.get("/logs/maps/manifest")
    async def logs_maps_manifest(request: Request):
        await ctx.require_api_user(request)
        config = ctx.get_config_fn()
        manifest = map_renderer.get_map_manifest(config.resolved_maps_cache_dir())
        shared = await asyncio.to_thread(map_job_status, config.resolved_logs_db_path())
        return {**manifest, "build_status": shared or map_renderer.get_map_build_status()}

    @router.get("/logs/maps/z/{z_level}.png")
    async def logs_map_tile_image(request: Request, z_level: int):
        await ctx.require_api_user(request)
        config = ctx.get_config_fn()
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
        await ctx.require_api_user(request)
        config = ctx.get_config_fn()
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
        radius: int = Query(default=0, ge=0, le=100),
        q: str = "",
        regex: bool = False,
        time_from: str = "",
        time_to: str = "",
        mask_ips: bool = False,
        z: Optional[int] = Query(default=None, ge=1, le=99),
        limit: int = Query(default=5000, ge=1, le=10000),
    ):
        await ctx.require_api_user(request)
        config = ctx.get_config_fn()
        rnd, file_path = await ctx.resolve_round_and_file(round_uid, file)
        if file == "qdel.log":
            raise HTTPException(
                status_code=400, detail="Координаты карты поддерживаются только для game.log / initialize.log."
            )

        selected_cats = [c.strip().upper() for c in cat if c and c.strip()]
        active_preset = preset if preset in LOG_PRESETS else "gameplay"
        payload = await run_log_operation(
            request, ctx.get_config_fn(), collect_round_map_events,
            file_path,
            preset=active_preset,
            categories=selected_cats or None,
            ckey=ckey,
            radius=radius,
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
        await ctx.require_api_user(request)
        config = ctx.get_config_fn()
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
