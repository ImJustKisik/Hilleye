"""Read pages and context using indexed offsets when the file matches the index."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import closing, contextmanager
from functools import lru_cache

from core.log_parser import LogPageResult, category_matches_filters, sanitize_byond_html
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
from core.log_parser.models import _RoundMetaAccumulator
from core.logs_db.indexer import _checkpoint_matches_file, _file_version, _hash_prefix

_MAX_TAIL_BYTES = 8 * 1024 * 1024


class _BoundedLines:
    def __init__(self, stream, end):
        self.stream = stream
        self.end = end

    def tell(self):
        return self.stream.tell()

    def __iter__(self):
        while self.tell() < self.end:
            line = self.stream.readline(self.end - self.tell())
            if not line:
                break
            yield line


@contextmanager
def _index_snapshot(db_path, uid, file_path):
    with closing(sqlite3.connect(db_path, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        rnd = conn.execute("SELECT * FROM log_rounds WHERE round_uid=? AND import_batch IS NULL", (uid,)).fetchone()
        if rnd is None or rnd["index_error"]:
            yield None
            return
        row = conn.execute("SELECT checkpoint_json FROM log_index_checkpoints WHERE round_uid=? AND generation=?", (uid, rnd["active_generation"])).fetchone()
        if row is None:
            yield None
            return
        try:
            checkpoint = json.loads(row[0])
            if not isinstance(checkpoint, dict) or type(checkpoint.get("offset")) is not int:
                yield None
                return
        except ValueError:
            yield None
            return
        with file_path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            if (not _checkpoint_matches_file(checkpoint, stat, bool(rnd["includes_debug"]))
                    or not _matches_checkpoint(file_path, stat, checkpoint)):
                yield None
                return
            yield conn, rnd, stream, checkpoint, stat


def _matches_checkpoint(file_path, stat, checkpoint):
    offset = checkpoint["offset"]
    if checkpoint.get("file_id") != [stat.st_dev, stat.st_ino] or not 0 <= stat.st_size - offset <= _MAX_TAIL_BYTES:
        return False
    if (checkpoint.get("observed_size", offset) == stat.st_size and checkpoint.get("mtime_ns") == stat.st_mtime_ns
            and checkpoint.get("ctime_ns", stat.st_ctime_ns) == stat.st_ctime_ns):
        return True
    digest = checkpoint.get("prefix_sha256")
    if digest and stat.st_size > offset:
        try:
            return _prefix_is_current(file_path, _file_version(stat), offset, digest)
        except OSError:
            return False
    return False


@lru_cache(maxsize=128)
def _prefix_is_current(file_path, version, offset, digest):
    with file_path.open("rb") as stream:
        if _file_version(os.fstat(stream.fileno())) != version:
            return False
        if offset:
            stream.seek(offset - 1)
            if stream.read(1) != b"\n":
                return False
        return _hash_prefix(stream, offset).hexdigest() == digest and _file_version(os.fstat(stream.fileno())) == version


def _snapshot_still_current(path, stream, initial):
    try:
        return (
            _file_version(os.fstat(stream.fileno())) == _file_version(initial)
            and _file_version(path.stat())[:4] == _file_version(initial)[:4]
        )
    except OSError:
        return False


def _parse_tail(stream, checkpoint, end, indexed_lines):
    stream.seek(checkpoint["offset"])
    return _iter_parsed_log_stream(
        _BoundedLines(stream, end), first_line=indexed_lines + 1,
        prev_ts=checkpoint.get("prev_ts", ""), prev_cat=checkpoint.get("prev_cat", "SYSTEM"),
    )


def _indexed_entry_matches(file_path, row, checkpoint):
    if row["byte_offset"] is None or not checkpoint:
        return False
    try:
        with file_path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            if checkpoint.get("file_id") != [stat.st_dev, stat.st_ino]:
                return False
            stream.seek(row["byte_offset"])
            raw = stream.read(row["byte_length"])
    except OSError:
        return False
    if len(raw) != row["byte_length"]:
        return False
    raw_line = _decode_log_line(raw)
    if row["line_no"] == 1:
        raw_line = raw_line.lstrip("\ufeff")
    ts, cat, message, continuation = parse_raw_line(row["line_no"], raw_line, row["timestamp"], row["category"])
    return (
        (ts, cat, continuation) == (row["timestamp"], row["category"], bool(row["is_continuation"]))
        and sanitize_byond_html(message)[1] == row["message"]
    )


async def indexed_entry_matches(round_uid, file_path, line_no, entry_id):
    from core import logs_db

    conn = await logs_db.get_logs_connection()
    async with conn.execute(
        """SELECT e.line_no,e.timestamp,e.category,e.message,e.is_continuation,e.byte_offset,
                  e.byte_length,c.checkpoint_json
           FROM log_entries e
           JOIN log_rounds r ON r.round_uid=e.round_uid AND r.active_generation=e.generation
           JOIN log_index_checkpoints c ON c.round_uid=r.round_uid AND c.generation=r.active_generation
           WHERE e.id=? AND e.round_uid=? AND e.line_no=? AND e.file_name='game.log'
                 AND r.has_game_log=1 AND r.import_batch IS NULL""",
        (entry_id, round_uid, line_no),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return False
    try:
        checkpoint = json.loads(row["checkpoint_json"])
    except ValueError:
        return False
    if not isinstance(checkpoint, dict):
        return False
    return await asyncio.to_thread(_indexed_entry_matches, file_path, row, checkpoint)


def _indexed_page(db_path, uid, file_path, options):
    with _index_snapshot(db_path, uid, file_path) as snapshot:
        if snapshot is None:
            return None
        conn, rnd, stream, checkpoint, stat = snapshot
        counts = json.loads(rnd["category_counts_json"])
        def includes(category):
            return category_matches_filters(category, preset=options.get("preset", "gameplay"),
                                            selected_categories=options.get("categories"))

        categories = [cat for cat in counts if includes(cat)]
        if "DEBUG" in categories and not rnd["includes_debug"]:
            return None
        page = max(1, int(options.get("page", 1)))
        per_page = max(25, min(500, int(options.get("per_page", 200))))
        params = [uid, rnd["active_generation"], *categories]
        clause = f"round_uid=? AND generation=? AND category IN ({','.join('?' for _ in categories)})" if categories else "round_uid=? AND generation=? AND 0"
        indexed_matches = sum(counts[category] for category in categories)
        focus = options.get("focus_line")
        total = indexed_matches
        tail_focus = None
        meta = _RoundMetaAccumulator(
            total_lines=rnd["line_count"], category_counts=counts,
            round_id=rnd["round_id"], started_at=rnd["started_at"], ended_at=rnd["ended_at"],
        )
        has_tail = stat.st_size > checkpoint["offset"]
        if has_tail:
            for line_no, ts, cat, msg, continuation in _parse_tail(stream, checkpoint, stat.st_size, rnd["line_count"]):
                meta.observe(ts, cat, msg, continuation)
                if includes(cat):
                    if line_no == focus:
                        tail_focus = total
                    total += 1
        pages = max(1, (total + per_page - 1) // per_page)
        if focus and focus <= rnd["line_count"] and conn.execute(f"SELECT 1 FROM log_entries WHERE {clause} AND line_no=?", [*params, focus]).fetchone():
            before = conn.execute(f"SELECT COUNT(*) FROM log_entries WHERE {clause} AND line_no<?", [*params, focus]).fetchone()[0]
            page = before // per_page + 1
        elif tail_focus is not None:
            page = tail_focus // per_page + 1
        page = min(page, pages)
        offset = (page - 1) * per_page
        rows = conn.execute(f"SELECT * FROM log_entries WHERE {clause} ORDER BY line_no LIMIT ? OFFSET ?",
                            [*params, per_page, offset]).fetchall() if offset < indexed_matches else []
        entries = []
        display = {"mask_ips": options.get("mask_ips", False), "render_html": options.get("render_html", True)}
        for row in rows:
            if row["byte_offset"] is None:
                return None
            stream.seek(row["byte_offset"])
            raw = _decode_log_line(stream.read(row["byte_length"]))
            if row["line_no"] == 1:
                raw = raw.lstrip("\ufeff")
            ts, cat, msg, cont = parse_raw_line(row["line_no"], raw, row["timestamp"], row["category"])
            entries.append(_build_entry(row["line_no"], ts, cat, msg, cont, **display))
        if has_tail and offset + per_page > indexed_matches:
            ordinal = indexed_matches
            for record in _parse_tail(stream, checkpoint, stat.st_size, rnd["line_count"]):
                if not includes(record[2]):
                    continue
                if ordinal >= offset:
                    entries.append(_build_entry(*record, **display))
                ordinal += 1
                if ordinal >= offset + per_page:
                    break
        if not _snapshot_still_current(file_path, stream, stat):
            return None
        return LogPageResult(entries, total, meta.total_lines, page, per_page, pages, meta.category_counts,
                             meta.round_id, meta.started_at, meta.ended_at)


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
        conn, rnd, stream, checkpoint, stat = snapshot
        if lo > rnd["line_count"]:
            lines = _parse_tail(stream, checkpoint, stat.st_size, rnd["line_count"])
            entries = _build_context_entries(lines, lo, hi, **options)
            return entries if _snapshot_still_current(file_path, stream, stat) else None
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
            _BoundedLines(stream, stat.st_size), first_line=anchor["line_no"],
            prev_ts=anchor["timestamp"], prev_cat=anchor["category"],
        )
        entries = _build_context_entries(lines, lo, hi, **options)
        return entries if _snapshot_still_current(file_path, stream, stat) else None


async def read_log_context(round_uid, file_path, target_line, *, radius, mask_ips, render_html,
                           request=None, config=None):
    from core import logs_db

    options = {"mask_ips": mask_ips, "render_html": render_html}
    if file_path.name == "game.log" and logs_db._current_logs_db_path != ":memory:":
        result = await asyncio.to_thread(
            _indexed_context, logs_db._current_logs_db_path, round_uid, file_path,
            max(1, target_line - radius), target_line + radius, options,
        )
        if result is not None:
            return result
    if config is not None:
        return await run_log_operation(request, config, read_context_lines, file_path, target_line,
                                       radius=radius, **options)
    return await asyncio.to_thread(read_context_lines, file_path, target_line, radius=radius, **options)
