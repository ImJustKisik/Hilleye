"""Parser and aggregator for SierraBay12 qdel.log destruction reports."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from core.log_parser.filters import _decode_log_line
from core.log_parser.models import (
    QdelEntry,
    QdelReport,
    _QDEL_COST_RE,
    _QDEL_COUNT_RE,
    _QDEL_PATH_RE,
    _QDEL_UNNECESSARY_RE,
)


def parse_qdel_log_file(
    file_path: Path,
    *,
    q: str = "",
    sort_by: str = "destroy_cost_ms",
    sort_dir: str = "desc",
    include_raw: bool = True,
) -> QdelReport:
    """Parse SierraBay12 qdel.log into structured sortable entries and summary KPIs."""
    raw_lines: List[str] = []
    entries: List[QdelEntry] = []
    current: Optional[Dict[str, Any]] = None

    def flush_current() -> None:
        if current is None:
            return
        entries.append(
            QdelEntry(
                line_no=current["line_no"],
                timestamp=current["timestamp"],
                path=current["path"],
                qdel_count=current["qdel_count"],
                unnecessary_count=current["unnecessary_count"],
                destroy_cost_ms=round(current["destroy_cost_ms"], 3),
            )
        )

    with file_path.open("rb") as fh:
        for line_no, raw_bytes in enumerate(fh, 1):
            raw_line = _decode_log_line(raw_bytes).rstrip("\r\n")
            if line_no == 1 and raw_line.startswith("\ufeff"):
                raw_line = raw_line.lstrip("\ufeff")
            if include_raw:
                raw_lines.append(raw_line)
            m_path = _QDEL_PATH_RE.match(raw_line)
            if m_path:
                flush_current()
                current = {
                    "line_no": line_no,
                    "timestamp": m_path.group(1) or "",
                    "path": m_path.group(2),
                    "qdel_count": 0,
                    "unnecessary_count": 0,
                    "destroy_cost_ms": 0.0,
                }
                continue
            if current is None:
                continue
            m_cnt = _QDEL_COUNT_RE.match(raw_line)
            if m_cnt:
                current["qdel_count"] = int(m_cnt.group(1))
                continue
            m_unn = _QDEL_UNNECESSARY_RE.match(raw_line)
            if m_unn:
                current["unnecessary_count"] = int(m_unn.group(1))
                continue
            m_cost = _QDEL_COST_RE.match(raw_line)
            if m_cost:
                current["destroy_cost_ms"] = float(m_cost.group(1))
                continue

    flush_current()

    total_paths = len(entries)
    total_qdels = sum(e.qdel_count for e in entries)
    total_unnecessary = sum(e.unnecessary_count for e in entries)
    total_destroy_ms = round(sum(e.destroy_cost_ms for e in entries), 2)

    needle = (q or "").strip().lower()
    if needle:
        entries = [e for e in entries if needle in e.path.lower()]

    reverse = sort_dir.lower() != "asc"
    sort_key_map = {
        "destroy_cost_ms": lambda e: (e.destroy_cost_ms, e.qdel_count),
        "qdel_count": lambda e: (e.qdel_count, e.destroy_cost_ms),
        "unnecessary_count": lambda e: (e.unnecessary_count, e.destroy_cost_ms),
        "path": lambda e: e.path.lower(),
    }
    key_fn = sort_key_map.get(sort_by, sort_key_map["destroy_cost_ms"])
    entries.sort(key=key_fn, reverse=reverse)

    return QdelReport(
        entries=entries,
        total_paths=total_paths,
        total_qdels=total_qdels,
        total_unnecessary=total_unnecessary,
        total_destroy_ms=total_destroy_ms,
        raw_text="\n".join(raw_lines) if include_raw else "",
    )
