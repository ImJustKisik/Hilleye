"""HTML and HTMX routes for log dashboard, round viewer, context inspector, and global search."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Query, Request

from core import celadon_logs, discord_auth, logs_db, map_renderer
from core.log_operations import run_log_operation
from core.logs_db.reader import read_log_context, read_log_page
from core.byond_topic import normalize_ckey
from core.log_parser import (
    CATEGORY_META,
    LOG_PRESETS,
    category_info,
    filter_game_log_file,
    mask_sensitive_text,
    parse_qdel_log_file,
    sanitize_byond_html,
    scan_raw_files_for_query,
)
from web.logs_routes.deps import LogsRouteContext
from web.logs_routes.viewmodels import (
    _coord_vm,
    _entry_to_vm,
    _fmt_bytes,
    _fmt_ts_utc,
    _ordered_categories,
    _parse_bool_flag,
    _round_time_vm,
)


def _unfiltered_line_query(file: str, mask_ips: bool, render_html: bool) -> str:
    return urlencode({"file": file, "preset": "all", "mask_ips": int(mask_ips), "html": int(render_html)})


def register_viewer_routes(router: APIRouter, ctx: LogsRouteContext) -> None:
    @router.get("/logs")
    async def logs_dashboard(
        request: Request,
        date: str = "",
        source: str = "",
        q: str = "",
        page: int = Query(default=1, ge=1),
        notice: str = "",
        round_folder: str = Query(default="", max_length=1000),
        import_error: str = Query(default="", max_length=1000),
    ):
        user, err_resp = await ctx.require_html_user(request)
        if err_resp is not None:
            return err_resp

        config = ctx.get_config_fn()
        shell = await ctx.get_shell_context_fn()
        per_page = 40
        offset = (page - 1) * per_page
        celadon_error = ""
        if source == "celadon":
            rounds, total_rounds = [], 0
            try:
                date = date or await celadon_logs.latest_day()
                catalog = await celadon_logs.list_rounds(date) if date else []
                search = q.strip().casefold().removeprefix("#").removeprefix("round-")
                matches = [item for item in catalog if search in item["round_id"].casefold()]
                total_rounds = len(matches)
                rounds = matches[offset:offset + per_page]
                for item in rounds:
                    item["files_url"] = "/logs?" + urlencode(
                        dict(source=source, date=date, q=q, page=page, round_folder=item["folder"])
                    ) + "#celadon-files"
                    if item["folder"] == round_folder:
                        item["files"] = [
                            (name, path) for name, path in await celadon_logs.list_directory(item["path"])
                            if not path.endswith("/")
                        ]
            except ValueError:
                celadon_error = "Укажите корректную дату."
            except HTTPException as exc:
                celadon_error = exc.detail
        else:
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
                r["duration_str"] = _round_time_vm(r).get("duration", "")

        stats = await logs_db.get_logs_stats()
        stats["total_size_label"] = _fmt_bytes(stats["total_bytes"])
        stats["last_indexed_str"] = _fmt_ts_utc(stats.get("last_indexed_at"))
        root_dir = config.resolved_logs_root_dir()
        root_dir_exists = bool(root_dir and root_dir.is_dir())
        total_pages = max(1, (total_rounds + per_page - 1) // per_page)
        now_utc = datetime.now(timezone.utc)
        today_utc = now_utc.strftime("%Y-%m-%d")
        yesterday_utc = (now_utc - timedelta(days=1)).strftime("%Y-%m-%d")

        is_rounds_fragment = request.headers.get("HX-Request") == "true"
        template_name = (
            "partials/logs_rounds_table.html"
            if is_rounds_fragment
            else "logs_index.html"
        )

        return ctx.templates.TemplateResponse(
            request=request,
            name=template_name,
            context={
                **shell,
                "user": user,
                "rounds": rounds,
                "total_rounds": total_rounds,
                "is_rounds_fragment": is_rounds_fragment,
                "page": page,
                "total_pages": total_pages,
                "stats": stats,
                "date_filter": date,
                "source_filter": source,
                "q_filter": q,
                "today_utc": today_utc,
                "yesterday_utc": yesterday_utc,
                "notice": notice,
                "celadon_error": celadon_error,
                "celadon_date_label": ".".join(reversed(date.split("-"))) if source == "celadon" else "",
                "import_error": import_error if source == "celadon" else "",
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
        radius: int = Query(default=0, ge=0, le=100),
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
        user, err_resp = await ctx.require_html_user(request)
        if err_resp is not None:
            return err_resp

        config = ctx.get_config_fn()
        shell = await ctx.get_shell_context_fn()
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
            return ctx.templates.TemplateResponse(
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
        active_ckey_norm = normalize_ckey(ckey)
        if not active_ckey_norm:
            radius = 0
        selected_cats = [c.strip().upper() for c in cat if c and c.strip()]
        active_preset = preset if preset in LOG_PRESETS else "gameplay"
        cache_dir = config.resolved_maps_cache_dir()
        map_manifest = map_renderer.get_map_manifest(cache_dir)

        result = await read_log_page(
            request, config, round_uid, file_path,
            preset=active_preset,
            categories=selected_cats or None,
            ckey=ckey,
            radius=radius,
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
            if radius > 0:
                query_pairs.append(("radius", str(radius)))
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
            if view_mode == "stream" and request.headers.get("HX-Request") == "true"
            and request.headers.get("HX-History-Restore-Request") != "true"
            else "logs_round.html"
        )

        return ctx.templates.TemplateResponse(
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
                "radius": radius,
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
                "unfiltered_line_query": _unfiltered_line_query(selected_file, mask_flag, render_html),
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
        user = await ctx.require_api_user(request)
        config = ctx.get_config_fn()
        rnd, file_path = await ctx.resolve_round_and_file(round_uid, file)

        mask_flag = _parse_bool_flag(mask_ips, default=False)
        render_html = _parse_bool_flag(html, default=True)
        inline_flag = _parse_bool_flag(inline, default=False)
        context_entries = await read_log_context(
            round_uid,
            file_path,
            line,
            radius=radius,
            mask_ips=mask_flag,
            render_html=render_html,
        )
        cache_dir = config.resolved_maps_cache_dir()
        rows = [_entry_to_vm(entry, cache_dir, target_line=line) for entry in context_entries]
        return ctx.templates.TemplateResponse(
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
                "unfiltered_line_query": _unfiltered_line_query(file, mask_flag, render_html),
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
        user, err_resp = await ctx.require_html_user(request)
        if err_resp is not None:
            return err_resp

        config = ctx.get_config_fn()
        shell = await ctx.get_shell_context_fn()
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

        grouped_hits: List[Dict[str, Any]] = []
        groups_by_uid: Dict[str, Dict[str, Any]] = {}
        for item in hits_vm:
            ruid = str(item.get("round_uid") or "")
            grp = groups_by_uid.get(ruid)
            if grp is None:
                grp = {
                    "round_uid": ruid,
                    "round_id": item.get("round_id") or ruid,
                    "round_date": item.get("round_date") or "",
                    "hits": [],
                }
                groups_by_uid[ruid] = grp
                grouped_hits.append(grp)
            grp["hits"].append(item)

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

        return ctx.templates.TemplateResponse(
            request=request,
            name="logs_search.html",
            context={
                **shell,
                "user": user,
                "hits": hits_vm,
                "grouped_hits": grouped_hits,
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
