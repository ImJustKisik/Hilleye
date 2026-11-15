"""Round queries, summary statistics, safe file resolution, and FTS5 global search."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.byond_topic import normalize_ckey
from core.logs_db.connection import ALLOWED_LOG_FILES


async def list_rounds(
    *,
    date_filter: str = "",
    source_filter: str = "",
    q: str = "",
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Query indexed round directory with date, source, and text filters."""
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    clauses: List[str] = []
    params: List[Any] = []

    if date_filter.strip():
        clauses.append("round_date = ?")
        params.append(date_filter.strip())
    if source_filter in ("disk", "upload"):
        clauses.append("source_type = ?")
        params.append(source_filter)
    if q.strip():
        like = f"%{q.strip()}%"
        clauses.append("(round_id LIKE ? OR round_date LIKE ? OR label LIKE ? OR round_uid LIKE ?)")
        params.extend([like, like, like, like])

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cnt_cur = await conn.execute(f"SELECT COUNT(*) FROM log_rounds {where_sql}", params)
    total = int((await cnt_cur.fetchone())[0])

    limit = max(1, min(int(limit), 10000))
    offset = max(0, int(offset))
    cur = await conn.execute(
        f"""
        SELECT *
        FROM log_rounds
        {where_sql}
        ORDER BY round_date DESC, COALESCE(started_at, '') DESC, game_log_mtime DESC, round_uid DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    items = []
    for row in await cur.fetchall():
        d = dict(row)
        try:
            d["category_counts"] = json.loads(d.get("category_counts_json") or "{}")
        except Exception:
            d["category_counts"] = {}
        items.append(d)
    return items, total


async def get_round_by_uid(round_uid: str) -> Optional[Dict[str, Any]]:
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    cur = await conn.execute("SELECT * FROM log_rounds WHERE round_uid = ?", (round_uid,))
    row = await cur.fetchone()
    if row is None:
        return None
    d = dict(row)
    try:
        d["category_counts"] = json.loads(d.get("category_counts_json") or "{}")
    except Exception:
        d["category_counts"] = {}
    return d


async def get_logs_stats() -> Dict[str, Any]:
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    row = await (
        await conn.execute(
            """
            SELECT
                COUNT(*) AS total_rounds,
                SUM(CASE WHEN source_type = 'disk' THEN 1 ELSE 0 END) AS disk_rounds,
                SUM(CASE WHEN source_type = 'upload' THEN 1 ELSE 0 END) AS upload_rounds,
                COALESCE(SUM(line_count), 0) AS total_lines,
                COALESCE(SUM(gameplay_line_count), 0) AS gameplay_lines,
                COALESCE(SUM(game_log_size + qdel_log_size), 0) AS total_bytes,
                MAX(indexed_at) AS last_indexed_at
            FROM log_rounds
            """
        )
    ).fetchone()
    fts_cnt = await (await conn.execute("SELECT COUNT(*) FROM log_entries")).fetchone()
    dates_cur = await conn.execute(
        "SELECT round_date, COUNT(*) AS cnt FROM log_rounds GROUP BY round_date ORDER BY round_date DESC LIMIT 60"
    )
    dates = [dict(r) for r in await dates_cur.fetchall()]
    return {
        "total_rounds": int(row["total_rounds"] or 0),
        "disk_rounds": int(row["disk_rounds"] or 0),
        "upload_rounds": int(row["upload_rounds"] or 0),
        "total_lines": int(row["total_lines"] or 0),
        "gameplay_lines": int(row["gameplay_lines"] or 0),
        "total_bytes": int(row["total_bytes"] or 0),
        "indexed_entries": int(fts_cnt[0] or 0),
        "last_indexed_at": row["last_indexed_at"],
        "dates": dates,
    }


async def get_index_error_rounds(limit: int = 50) -> List[Dict[str, Any]]:
    """Return rounds that encountered an indexing error."""
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    cur = await conn.execute(
        """
        SELECT round_uid, round_id, round_date, source_type, index_error
        FROM log_rounds
        WHERE index_error IS NOT NULL AND index_error != ''
        ORDER BY round_date DESC, round_uid DESC
        LIMIT ?
        """,
        (max(1, min(int(limit), 200)),),
    )
    return [dict(r) for r in await cur.fetchall()]


def resolve_round_file_path(
    round_record: Dict[str, Any],
    file_name: str,
    *,
    root_dir: Optional[Path],
    uploads_dir: Optional[Path],
) -> Optional[Path]:
    """Strictly verify round directory containment and file allowlist before disk access."""
    if file_name not in ALLOWED_LOG_FILES:
        return None
    dir_path = Path(round_record["dir_path"]).resolve()
    source_type = round_record.get("source_type")
    if source_type == "disk":
        if root_dir is None or not dir_path.is_relative_to(root_dir.resolve()):
            return None
    elif source_type == "upload":
        if uploads_dir is None or not dir_path.is_relative_to(uploads_dir.resolve()):
            return None
    else:
        return None

    candidate = (dir_path / file_name).resolve()
    if not candidate.is_relative_to(dir_path) or not candidate.is_file():
        return None
    return candidate


def _build_safe_fts_query(raw_query: str) -> str:
    """Convert arbitrary user input into a safe SQLite FTS5 query string."""
    tokens = re.findall(r"[^\s]+", (raw_query or "").strip())
    if not tokens:
        return ""
    quoted = []
    for tok in tokens[:16]:
        safe_tok = tok.replace('"', '""')
        quoted.append(f'"{safe_tok}"')
    return " AND ".join(quoted)


async def search_logs_fts(
    *,
    query: str = "",
    ckey: str = "",
    preset: str = "gameplay",
    categories: Optional[Sequence[str]] = None,
    date_from: str = "",
    date_to: str = "",
    round_uid: str = "",
    z_level: Optional[int] = None,
    only_coords: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Execute global cross-round search via SQLite FTS5."""
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    ckey_norm = normalize_ckey(ckey) if ckey else ""
    fts_parts: List[str] = []
    safe_q = _build_safe_fts_query(query)
    if safe_q:
        fts_parts.append(f"({safe_q})")
    if ckey_norm:
        fts_parts.append(f'ckeys_space : "{ckey_norm}"')

    where_clauses: List[str] = []
    params: List[Any] = []

    use_fts = bool(fts_parts)
    if use_fts:
        where_clauses.append("log_entries_fts MATCH ?")
        params.append(" AND ".join(fts_parts))

    if round_uid.strip():
        where_clauses.append("e.round_uid = ?")
        params.append(round_uid.strip())
    if date_from.strip():
        where_clauses.append("r.round_date >= ?")
        params.append(date_from.strip())
    if date_to.strip():
        where_clauses.append("r.round_date <= ?")
        params.append(date_to.strip())
    if z_level is not None and int(z_level) > 0:
        where_clauses.append("e.coord_z = ?")
        params.append(int(z_level))
    elif only_coords:
        where_clauses.append("e.coord_z IS NOT NULL")

    # Category / preset filter
    if categories:
        clean_cats = sorted({c.strip().upper() for c in categories if c and c.strip()})
        if clean_cats:
            placeholders = ",".join("?" for _ in clean_cats)
            where_clauses.append(f"e.category IN ({placeholders})")
            params.extend(clean_cats)
    else:
        from core.log_parser import LOG_PRESETS

        preset_cfg = LOG_PRESETS.get(preset, LOG_PRESETS["gameplay"])
        if preset_cfg.get("include"):
            inc = sorted(preset_cfg["include"])
            placeholders = ",".join("?" for _ in inc)
            where_clauses.append(f"e.category IN ({placeholders})")
            params.extend(inc)
        elif preset_cfg.get("exclude"):
            exc = sorted(preset_cfg["exclude"])
            placeholders = ",".join("?" for _ in exc)
            where_clauses.append(f"e.category NOT IN ({placeholders})")
            params.extend(exc)

    if not where_clauses:
        return [], 0

    where_sql = " AND ".join(where_clauses)
    if use_fts:
        from_sql = """
            FROM log_entries_fts
            JOIN log_entries e ON e.id = log_entries_fts.rowid
            JOIN log_rounds r ON r.round_uid = e.round_uid
        """
    else:
        from_sql = """
            FROM log_entries e
            JOIN log_rounds r ON r.round_uid = e.round_uid
        """

    cnt_cur = await conn.execute(f"SELECT COUNT(*) {from_sql} WHERE {where_sql}", params)
    total = int((await cnt_cur.fetchone())[0])

    cur = await conn.execute(
        f"""
        SELECT
            e.round_uid, e.file_name, e.line_no, e.timestamp, e.category, e.ckeys_space, e.message,
            e.coord_x, e.coord_y, e.coord_z,
            r.round_id, r.round_date, r.source_type, r.label
        {from_sql}
        WHERE {where_sql}
        ORDER BY r.round_date DESC, e.timestamp DESC, e.line_no ASC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    hits = []
    for row in await cur.fetchall():
        d = dict(row)
        d["ckeys"] = [c for c in (d.get("ckeys_space") or "").split(" ") if c]
        hits.append(d)
    return hits, total
