"""Read ordinary pages using indexed offsets; preserve the streaming filter fallback."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import closing

from core.log_parser import LogPageResult, category_matches_filters
from core.log_parser.filters import _build_entry, _decode_log_line, parse_raw_line, filter_game_log_file
from core.log_operations import run_log_operation


def _indexed_page(db_path, uid, file_path, options):
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rnd = conn.execute("SELECT * FROM log_rounds WHERE round_uid=?", (uid,)).fetchone()
        if rnd is None or rnd["index_error"]:
            return None
        row = conn.execute("SELECT checkpoint_json FROM log_index_checkpoints WHERE round_uid=? AND generation=?", (uid, rnd["active_generation"])).fetchone()
        if row is None:
            return None
        checkpoint = json.loads(row[0])
        counts = json.loads(rnd["category_counts_json"])
        categories = [cat for cat in counts if category_matches_filters(cat, preset=options.get("preset", "gameplay"), selected_categories=options.get("categories"))]
        if "DEBUG" in categories and not rnd["includes_debug"]:
            return None
        page = max(1, int(options.get("page", 1)))
        per_page = max(25, min(500, int(options.get("per_page", 200))))
        with file_path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            if checkpoint["file_id"] != [stat.st_dev, stat.st_ino] or checkpoint["offset"] != stat.st_size or checkpoint["mtime_ns"] != stat.st_mtime_ns:
                return None
            params = [uid, rnd["active_generation"], *categories]
            clause = f"round_uid=? AND generation=? AND category IN ({','.join('?' for _ in categories)})" if categories else "round_uid=? AND generation=? AND 0"
            total = sum(counts[category] for category in categories)
            pages = max(1, (total + per_page - 1) // per_page)
            focus = options.get("focus_line")
            if focus and conn.execute(f"SELECT 1 FROM log_entries WHERE {clause} AND line_no=?", [*params, focus]).fetchone():
                before = conn.execute(f"SELECT COUNT(*) FROM log_entries WHERE {clause} AND line_no<?", [*params, focus]).fetchone()[0]
                page = before // per_page + 1
            page = min(page, pages)
            rows = conn.execute(f"SELECT * FROM log_entries WHERE {clause} ORDER BY line_no LIMIT ? OFFSET ?", [*params, per_page, (page - 1) * per_page]).fetchall()
            entries = []
            for row in rows:
                if row["byte_offset"] is None:
                    return None
                stream.seek(row["byte_offset"])
                raw = _decode_log_line(stream.read(row["byte_length"]))
                if row["line_no"] == 1:
                    raw = raw.lstrip("\ufeff")
                ts, cat, msg, cont = parse_raw_line(row["line_no"], raw, row["timestamp"], row["category"])
                entries.append(_build_entry(row["line_no"], ts, cat, msg, cont,
                                            mask_ips=options.get("mask_ips", False), render_html=options.get("render_html", True)))
            after = os.fstat(stream.fileno())
            if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                return None
        return LogPageResult(entries, total, rnd["line_count"], page, per_page, pages, counts,
                             rnd["round_id"], rnd["started_at"], rnd["ended_at"])


async def read_log_page(request, config, round_uid, file_path, **options):
    from core import logs_db
    if (file_path.name == "game.log" and logs_db._current_logs_db_path != ":memory:"
            and not any(options.get(key) for key in ("q", "ckey", "radius", "use_regex", "time_from", "time_to"))):
        result = await asyncio.to_thread(_indexed_page, logs_db._current_logs_db_path, round_uid, file_path, options)
        if result is not None:
            return result
    return await run_log_operation(request, config, filter_game_log_file, file_path, **options)
