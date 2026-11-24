"""Parser and aggregator for SierraBay12 qdel.log destruction reports."""

from __future__ import annotations

from contextlib import closing, nullcontext
from pathlib import Path
import sqlite3

from core.log_parser.filters import _decode_log_line
from core.log_parser.models import (
    QdelEntry,
    QdelReport,
    _QDEL_COST_RE,
    _QDEL_COUNT_RE,
    _QDEL_PATH_RE,
    _QDEL_UNNECESSARY_RE,
)
from core.log_parser.sanitizer import prepare_log_message

_SORT_COLUMNS = {
    "destroy_cost_ms": ("destroy_cost_ms", "qdel_count"),
    "qdel_count": ("qdel_count", "destroy_cost_ms"),
    "unnecessary_count": ("unnecessary_count", "destroy_cost_ms"),
    "path": ("path_key",),
}
_MAX_QDEL_LINE = 65536


def parse_qdel_log_file(
    file_path: Path,
    *,
    q: str = "",
    sort_by: str = "destroy_cost_ms",
    sort_dir: str = "desc",
    include_raw: bool = True,
    page: int | None = None,
    per_page: int = 100,
    mask_ips: bool = False,
) -> QdelReport:
    """Parse SierraBay12 qdel.log into structured sortable entries and summary KPIs."""
    per_page = max(1, min(500, per_page))
    report = QdelReport([], 0, 0, 0, 0.0, "", max(1, page or 1), per_page)
    offset = (report.page - 1) * per_page
    needle = (q or "").strip().lower()
    raw_lines = []
    line_count = 0
    raw_page = include_raw and page is not None

    def capture(line_no, raw, truncated=False):
        nonlocal line_count
        line_count = line_no
        if include_raw and (not raw_page or offset < line_no <= offset + per_page):
            if mask_ips:
                raw, _ = prepare_log_message(raw, mask_ips=True)
            report.raw_truncated |= truncated
            if raw_page and len(raw) > 4096:
                raw = raw[:4096] + "…"
                report.raw_truncated = True
            raw_lines.append(raw)

    scratch = nullcontext(None) if raw_page else closing(sqlite3.connect(""))
    with scratch as conn:
        if conn is not None:
            conn.execute("PRAGMA temp_store=FILE")
            conn.execute(
                """CREATE TABLE entries(line_no INTEGER,timestamp TEXT,path TEXT,qdel_count INTEGER,
                   unnecessary_count INTEGER,destroy_cost_ms REAL,path_key TEXT)"""
            )
        for entry in _iter_qdel_entries(file_path, capture):
            report.total_paths += 1
            report.total_qdels += entry.qdel_count
            report.total_unnecessary += entry.unnecessary_count
            report.total_destroy_ms += entry.destroy_cost_ms
            path = prepare_log_message(entry.path, mask_ips=True)[0] if mask_ips else entry.path
            path_key = path.lower()
            if needle in path_key:
                report.matched_entries += 1
                if conn is not None:
                    conn.execute(
                        "INSERT INTO entries VALUES (?,?,?,?,?,?,?)",
                        (entry.line_no, entry.timestamp, path, entry.qdel_count,
                         entry.unnecessary_count, entry.destroy_cost_ms, path_key),
                    )
        if conn is not None:
            direction = "ASC" if sort_dir.lower() == "asc" else "DESC"
            columns = _SORT_COLUMNS.get(sort_by, _SORT_COLUMNS["destroy_cost_ms"])
            ordering = ", ".join(f"{column} {direction}" for column in columns)
            report.entries = [QdelEntry(*row) for row in conn.execute(
                f"""SELECT line_no,timestamp,path,qdel_count,unnecessary_count,destroy_cost_ms
                    FROM entries ORDER BY {ordering}, line_no LIMIT ? OFFSET ?""",
                (-1 if page is None else per_page, offset),
            )]
    count = line_count if raw_page else report.matched_entries
    report.total_pages = max(1, (count + per_page - 1) // per_page) if page is not None else 1
    report.total_destroy_ms = round(report.total_destroy_ms, 2)
    report.raw_text = "\n".join(raw_lines)
    return report


def _iter_qdel_entries(file_path, capture):
    current = None
    with file_path.open("rb") as stream:
        line_no = 0
        while raw_bytes := stream.readline(_MAX_QDEL_LINE + 1):
            line_no += 1
            if len(raw_bytes) > _MAX_QDEL_LINE:
                while raw_bytes and not raw_bytes.endswith(b"\n"):
                    raw_bytes = stream.readline(_MAX_QDEL_LINE + 1)
                capture(line_no, "[Строка слишком длинная для предпросмотра]", truncated=True)
                continue
            line = _decode_log_line(raw_bytes).rstrip("\r\n")
            if line_no == 1:
                line = line.lstrip("\ufeff")
            capture(line_no, line)
            path = _QDEL_PATH_RE.match(line)
            if path:
                if current is not None:
                    yield current
                current = QdelEntry(line_no, path[1] or "", path[2], 0, 0, 0.0)
            elif current is not None:
                if (match := _QDEL_COUNT_RE.match(line)) and len(match[1]) <= 18:
                    current.qdel_count = int(match[1])
                elif (match := _QDEL_UNNECESSARY_RE.match(line)) and len(match[1]) <= 18:
                    current.unnecessary_count = int(match[1])
                elif match := _QDEL_COST_RE.match(line):
                    cost = float(match[1])
                    if cost <= 1_000_000_000_000:
                        current.destroy_cost_ms = round(cost, 3)
    if current is not None:
        yield current
