"""Protected JSON API, log download endpoints, and station map asset routes."""

from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from core import logs_db, map_renderer
from core.log_operations import run_log_operation, finish_thread
from core.logs_db.reader import read_log_context, read_log_page
from core.log_jobs import configured_logs_status, get_job, map_job_status
from core.log_parser.filters import export_filtered_game_log_to_file, mask_log_to_file
from core.log_parser import (
    LOG_PRESETS,
    collect_round_map_events,
    mask_sensitive_text,
    parse_qdel_log_file,
)
from web.logs_routes.deps import LogsRouteContext, record_log_access, require_indexed_entry, round_alias_location, validate_log_search

_ROUND_FIELDS = (
    "round_uid", "source_type", "round_id", "round_date", "label", "uploaded_at",
    "has_game_log", "has_qdel_log", "has_initialize_log", "game_log_size", "qdel_log_size", "initialize_log_size",
    "line_count", "gameplay_line_count", "category_counts", "started_at", "ended_at", "map_revision",
)


class _LogDownloadResponse(FileResponse):
    def __init__(self, *args, audit, staging=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.audit = audit
        self.staging = staging

    async def __call__(self, scope, receive, send):
        status = self.status_code
        finished = False

        async def tracked_send(message):
            nonlocal status, finished
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.pathsend" or (
                message["type"] == "http.response.body" and not message.get("more_body")
            ):
                finished = True

        try:
            await super().__call__(scope, receive, tracked_send)
        finally:
            if self.staging is not None:
                await finish_thread(self.staging.cleanup)
            await self.audit(f"http_{status}" if finished else "interrupted")


def register_api_routes(router: APIRouter, ctx: LogsRouteContext) -> None:
    def map_asset_dir(cache_dir, version):
        if version is None:
            return cache_dir
        directory = map_renderer.get_map_version_dir(cache_dir, version)
        if directory is None:
            raise HTTPException(404, "Эта версия карты больше недоступна.")
        return directory

    @router.get("/api/v1/logs/jobs/{job_id}")
    async def api_log_job(request: Request, job_id: str):
        user = await ctx.require_api_user(request)
        path = ctx.get_config_fn().resolved_logs_db_path()
        job = None if path == ":memory:" else await asyncio.to_thread(
            get_job, path, job_id, user_id=user.discord_id, admin=user.is_admin,
        )
        if job is None:
            raise HTTPException(404, "Задача не найдена.")
        return job

    @router.get("/api/v1/logs/status")
    async def api_log_status(request: Request):
        user = await ctx.require_api_user(request)
        result = await configured_logs_status(ctx.get_config_fn())
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
        return {"total": total, "rounds": [{key: rnd[key] for key in _ROUND_FIELDS} for rnd in rounds]}

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
        user = await ctx.require_api_user(request)
        _rnd, file_path = await ctx.resolve_round_and_file(request, round_uid, file)

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
        await record_log_access(request, user, "view_round", f"{round_uid}:{file}",
                                details=f"surface=api_entries, page={res.page}")
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
        page: int = Query(default=1, ge=1, le=1_000_000),
        per_page: int = Query(default=100, ge=1, le=500),
        mask_ips: bool = False,
    ):
        user = await ctx.require_api_user(request)
        _rnd, file_path = await ctx.resolve_round_and_file(request, round_uid, "qdel.log")
        report = await run_log_operation(
            request, ctx.get_config_fn(), parse_qdel_log_file, file_path,
            q=q, sort_by=sort_by, sort_dir=sort_dir, include_raw=False,
            page=page, per_page=per_page, mask_ips=mask_ips,
        )
        await record_log_access(request, user, "view_round", f"{round_uid}:qdel.log", details="surface=api_qdel")
        return {
            "round_uid": round_uid,
            "total_paths": report.total_paths,
            "total_qdels": report.total_qdels,
            "total_unnecessary": report.total_unnecessary,
            "total_destroy_ms": report.total_destroy_ms,
            "matched_entries": report.matched_entries,
            "page": report.page,
            "per_page": report.per_page,
            "total_pages": report.total_pages,
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
        user = await ctx.require_api_user(request)
        _rnd, file_path = await ctx.resolve_round_and_file(request, round_uid, file)
        entries = await read_log_context(
            round_uid, file_path, line, radius=radius, mask_ips=mask_ips, render_html=False,
            request=request, config=ctx.get_config_fn(),
        )
        await record_log_access(request, user, "view_round", f"{round_uid}:{file}",
                                details=f"surface=api_context, line={line}, radius={radius}")
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
        raw = mode == "raw" or file == "qdel.log"

        async def audit(outcome):
            await record_log_access(
                request, user, "download_log", f"{round_uid}:{file}",
                details=f"mode={'raw' if raw else 'filtered'}, preset={preset}, mask_ips={mask_ips}, outcome={outcome}",
            )

        await audit("requested")
        try:
            rnd, file_path = await ctx.resolve_round_and_file(request, round_uid, file)
        except HTTPException as exc:
            await audit(f"http_{exc.status_code}")
            raise
        dl_name = f"round-{rnd['round_id']}-{file}" if raw else f"round-{rnd['round_id']}-{preset}-{file}"
        safe_dl_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in dl_name)
        if raw and not mask_ips:
            return _LogDownloadResponse(file_path, media_type="text/plain; charset=utf-8",
                                        filename=safe_dl_name, audit=audit)
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
            return _LogDownloadResponse(
                output_path, media_type="text/plain; charset=utf-8", filename=safe_dl_name,
                staging=staging, audit=audit,
            )
        except BaseException as exc:
            await finish_thread(staging.cleanup)
            if isinstance(exc, ValueError):
                await audit("http_400")
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            await audit(f"http_{exc.status_code}" if isinstance(exc, HTTPException) else "interrupted")
            raise

    @router.get("/logs/maps/manifest")
    async def logs_maps_manifest(request: Request):
        await ctx.require_api_user(request)
        config = ctx.get_config_fn()
        _, manifest = map_renderer.select_round_map(config.resolved_maps_cache_dir())
        shared = await asyncio.to_thread(map_job_status, config.resolved_logs_db_path())
        return {**map_renderer.public_map_manifest(manifest), "build_status": shared or map_renderer.get_map_build_status()}

    @router.get("/logs/maps/z/{z_level}.png")
    async def logs_map_tile_image(request: Request, z_level: int, version: Optional[str] = Query(default=None, max_length=32)):
        await ctx.require_api_user(request)
        config = ctx.get_config_fn()
        img_path = map_renderer.get_map_image_path(map_asset_dir(config.resolved_maps_cache_dir(), version), z_level)
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
    async def logs_map_areas_json(request: Request, z_level: int, version: Optional[str] = Query(default=None, max_length=32)):
        await ctx.require_api_user(request)
        config = ctx.get_config_fn()
        areas_path = map_renderer.get_map_areas_path(map_asset_dir(config.resolved_maps_cache_dir(), version), z_level)
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
        html: bool = True,
        entry_id: Optional[int] = Query(default=None, ge=1, le=2**63 - 1),
        line: Optional[int] = Query(default=None, ge=1),
        z: Optional[int] = Query(default=None, ge=1, le=99),
        limit: int = Query(default=5000, ge=1, le=10000),
        offset: int = Query(default=0, ge=0, le=10_000_000),
    ):
        user = await ctx.require_api_user(request)
        config = ctx.get_config_fn()
        rnd, file_path = await ctx.resolve_round_and_file(request, round_uid, file)
        await require_indexed_entry(round_uid, file, file_path, line, entry_id)
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
            render_html=html,
            z_filter=z,
            limit=limit,
            offset=offset,
            focus_line=line,
        )
        await require_indexed_entry(round_uid, file, file_path, line, entry_id)
        if payload.get("regex_error"):
            raise HTTPException(status_code=400, detail=payload["regex_error"])
        if payload.get("invalid_time_fields"):
            raise HTTPException(status_code=400, detail="Укажите время в формате ЧЧ:ММ или ЧЧ:ММ:СС.")

        cache_dir, manifest = map_renderer.select_round_map(config.resolved_maps_cache_dir(), rnd["map_revision"])
        area_dir = cache_dir if manifest["binding"] == "revision" else None
        for ev in payload["events"]:
            ev["area"] = map_renderer.lookup_area_name(area_dir, ev["z"], ev["x"], ev["y"]) if area_dir is not None else ""
            ev["z_label"] = map_renderer.deck_short_label(ev["z"])

        await record_log_access(request, user, "view_round", f"{round_uid}:{file}", details="surface=api_map")
        return {
            "round_uid": round_uid,
            "round_id": rnd["round_id"],
            "file": file,
            "manifest": map_renderer.public_map_manifest(manifest),
            **payload,
        }

    @router.get("/api/v1/logs/search")
    async def api_logs_search(
        request: Request,
        q: str = Query(default="", max_length=512),
        ckey: str = Query(default="", max_length=128),
        preset: str = "gameplay",
        cat: List[str] = Query(default=[]),
        date_from: str = "",
        date_to: str = "",
        round_uid: str = "",
        z_level: Optional[int] = Query(default=None, ge=1, le=99),
        only_coords: bool = False,
        mask_ips: bool = False,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ):
        user = await ctx.require_api_user(request)
        if round_uid and (location := await round_alias_location(request, round_uid.strip(), in_query=True)):
            return RedirectResponse(location, status_code=307)
        ckey, date_from, date_to = validate_log_search(ckey, date_from, date_to)
        config = ctx.get_config_fn()
        cache_dir = config.resolved_maps_cache_dir()
        map_dirs = {}
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
            if mask_ips:
                h["message"] = mask_sensitive_text(h["message"])
            cx, cy, cz = h.get("coord_x"), h.get("coord_y"), h.get("coord_z")
            revision = h.get("map_revision")
            if cx and cy and cz and revision:
                if revision not in map_dirs:
                    map_dirs[revision] = map_renderer.select_round_map(cache_dir, revision)[0]
                if map_dirs[revision] is not None:
                    h["area"] = map_renderer.lookup_area_name(map_dirs[revision], int(cz), int(cx), int(cy))
        await record_log_access(request, user, "search_global", (q or ckey or round_uid)[:160],
                                details=f"surface=api, total={total}")
        return {"total": total, "hits": hits}
