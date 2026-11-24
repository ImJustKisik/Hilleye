"""Read pages and context using indexed offsets when the file matches the index."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import closing, contextmanager

from core.log_parser import LogPageResult, category_matches_filters
from core.log_parser.filters import (
    _build_context_entries,
    _build_entry,
    _decode_log_line,
    _iter_parsed_log_stream,
    filter_game_log_file,
    parse_raw_line,
    read_context_lines,
)
from core.log_operations import run_log_operation


@contextmanager
def _index_snapshot(db_path, uid, file_path):
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rnd = conn.execute("SELECT * FROM log_rounds WHERE round_uid=?", (uid,)).fetchone()
        if rnd is None or rnd["index_error"]:
            yield None
            return
        row = conn.execute("SELECT checkpoint_json FROM log_index_checkpoints WHERE round_uid=? AND generation=?", (uid, rnd["active_generation"])).fetchone()
        if row is None:
            yield None
            return
        checkpoint = json.loads(row[0])
        with file_path.open("rb") as stream:
            if not _matches_checkpoint(stream, checkpoint):
                yield None
                return
            yield conn, rnd, stream, checkpoint


def _matches_checkpoint(stream, checkpoint):
    stat = os.fstat(stream.fileno())
    return (
        checkpoint["file_id"] == [stat.st_dev, stat.st_ino]
        and checkpoint["offset"] == stat.st_size
        and checkpoint["mtime_ns"] == stat.st_mtime_ns
    )


def _indexed_page(db_path, uid, file_path, options):
    with _index_snapshot(db_path, uid, file_path) as snapshot:
        if snapshot is None:
            return None
        conn, rnd, stream, checkpoint = snapshot
        counts = json.loads(rnd["category_counts_json"])
        categories = [cat for cat in counts if category_matches_filters(cat, preset=options.get("preset", "gameplay"), selected_categories=options.get("categories"))]
        if "DEBUG" in categories and not rnd["includes_debug"]:
            return None
        page = max(1, int(options.get("page", 1)))
        per_page = max(25, min(500, int(options.get("per_page", 200))))
        params = [uid, rnd["active_generation"], *categories]
        clause = f"round_uid=? AND generation=? AND category IN ({','.join('?' for _ in categories)})" if categories else "round_uid=? AND generation=? AND 0"
        total = sum(counts[category] for category in categories)
        pages = max(1, (total + per_page - 1) // per_page)
        focus = options.get("focus_line")
        if focus and focus <= rnd["line_count"] and conn.execute(f"SELECT 1 FROM log_entries WHERE {clause} AND line_no=?", [*params, focus]).fetchone():
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
        if not _matches_checkpoint(stream, checkpoint):
            return None
        return LogPageResult(entries, total, rnd["line_count"], page, per_page, pages, counts,
                             rnd["round_id"], rnd["started_at"], rnd["ended_at"])


async def read_log_page(request, config, round_uid, file_path, **options):
    from core import logs_db
    if (file_path.name == "game.log" and logs_db._current_logs_db_path != ":memory:"
            and not any(options.get(key) for key in ("q", "ckey", "use_regex", "time_from", "time_to"))):
        result = await asyncio.to_thread(_indexed_page, logs_db._current_logs_db_path, round_uid, file_path, options)
        if result is not None:
            return result
    return await run_log_operation(request, config, filter_game_log_file, file_path, **options)


def _indexed_context(db_path, uid, file_path, lo, hi, options):
    with _index_snapshot(db_path, uid, file_path) as snapshot:
        if snapshot is None:
            return None
        conn, rnd, stream, checkpoint = snapshot
        if lo > rnd["line_count"]:
            return [] if _matches_checkpoint(stream, checkpoint) else None
        anchor = conn.execute(
            """SELECT line_no, byte_offset, timestamp, category FROM log_entries
               WHERE round_uid=? AND generation=? AND line_no<=?
               ORDER BY line_no DESC LIMIT 1""",
            (uid, rnd["active_generation"], lo),
        ).fetchone()
        if anchor is None or anchor["byte_offset"] is None:
            return None
        # Read physical lines from the nearest anchor so unindexed DEBUG and continuations survive.
        stream.seek(anchor["byte_offset"])
        lines = _iter_parsed_log_stream(
            stream, first_line=anchor["line_no"], prev_ts=anchor["timestamp"], prev_cat=anchor["category"],
        )
        entries = _build_context_entries(lines, lo, hi, **options)
        return entries if _matches_checkpoint(stream, checkpoint) else None


async def read_log_context(round_uid, file_path, target_line, *, radius, mask_ips, render_html):
    from core import logs_db

    options = {"mask_ips": mask_ips, "render_html": render_html}
    if file_path.name == "game.log" and logs_db._current_logs_db_path != ":memory:":
        result = await asyncio.to_thread(
            _indexed_context, logs_db._current_logs_db_path, round_uid, file_path,
            max(1, target_line - radius), target_line + radius, options,
        )
        if result is not None:
            return result
    return await asyncio.to_thread(read_context_lines, file_path, target_line, radius=radius, **options)
