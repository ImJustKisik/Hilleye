"""Round queries, summary statistics, safe file resolution, and FTS5 global search."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.byond_topic import normalize_ckey
from core.logs_db.connection import ALLOWED_LOG_FILES


def _visible_rounds(table="log_rounds"):
    return (
        f"{table}.import_batch IS NULL AND NOT EXISTS "
        f"(SELECT 1 FROM log_sources s WHERE s.source={table}.source_type AND s.source='disk' AND s.status='disabled')"
    )


async def list_rounds(
    *,
    date_filter: str = "",
    source_filter: str = "",
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    round_uid: str = "",
    require_game_log: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> Tuple[List[Dict[str, Any]], int]:
    """Query indexed round directory with date, source, and text filters."""
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    clauses: List[str] = [_visible_rounds()]
    params: List[Any] = []

    if date_filter.strip():
        clauses.append("round_date = ?")
        params.append(date_filter.strip())
    if source_filter in ("disk", "upload"):
        clauses.append("source_type = ?")
        params.append(source_filter)
    for column, operator, value in (
        ("round_date", ">=", date_from), ("round_date", "<=", date_to), ("round_uid", "=", round_uid),
    ):
        if value:
            clauses.append(f"{column} {operator} ?")
            params.append(value)
    if require_game_log:
        clauses.append("has_game_log=1 AND missing_since IS NULL")
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
    cur = await conn.execute(f"SELECT * FROM log_rounds WHERE round_uid = ? AND {_visible_rounds()}", (round_uid,))
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
            f"""
            SELECT
                COUNT(*) AS total_rounds,
                SUM(CASE WHEN source_type = 'disk' THEN 1 ELSE 0 END) AS disk_rounds,
                SUM(CASE WHEN source_type = 'upload' THEN 1 ELSE 0 END) AS upload_rounds,
                COALESCE(SUM(line_count), 0) AS total_lines,
                COALESCE(SUM(gameplay_line_count), 0) AS gameplay_lines,
                COALESCE(SUM(game_log_size + qdel_log_size + initialize_log_size), 0) AS total_bytes,
                COALESCE(SUM(CASE WHEN has_game_log AND missing_since IS NULL AND indexed_at IS NOT NULL
                    THEN line_count - CASE WHEN NOT includes_debug AND json_valid(category_counts_json)
                                           THEN COALESCE(json_extract(category_counts_json, '$.DEBUG'), 0)
                                           ELSE 0 END ELSE 0 END), 0) AS indexed_entries,
                MAX(indexed_at) AS last_indexed_at
            FROM log_rounds
            WHERE {_visible_rounds()}
            """
        )
    ).fetchone()
    dates_cur = await conn.execute(
        f"""SELECT round_date, COUNT(*) AS cnt FROM log_rounds WHERE {_visible_rounds()}
           GROUP BY round_date ORDER BY round_date DESC LIMIT 60"""
    )
    dates = [dict(r) for r in await dates_cur.fetchall()]
    return {
        "total_rounds": int(row["total_rounds"] or 0),
        "disk_rounds": int(row["disk_rounds"] or 0),
        "upload_rounds": int(row["upload_rounds"] or 0),
        "total_lines": int(row["total_lines"] or 0),
        "gameplay_lines": int(row["gameplay_lines"] or 0),
        "total_bytes": int(row["total_bytes"] or 0),
        "indexed_entries": int(row["indexed_entries"] or 0),
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


async def get_index_debug_policy(default: bool) -> bool:
    from core import logs_db

    conn = await logs_db.get_logs_connection()
    async with conn.execute(
        "SELECT value_json FROM log_maintenance_state WHERE name='index_include_debug'"
    ) as cursor:
        row = await cursor.fetchone()
    return json.loads(row[0]) if row is not None else default


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

    source = dir_path / file_name
    if source.is_symlink():
        return None
    candidate = source.resolve()
    if not candidate.is_relative_to(dir_path) or not candidate.is_file():
        return None
    return candidate


def _build_safe_fts_query(raw_query: str) -> str:
    """Convert arbitrary user input into a safe SQLite FTS5 query string."""
    tokens = re.findall(r"[^\s]+", (raw_query or "").strip())
    if not tokens:
        return ""
    quoted = []
    for tok in tokens:
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
    if ckey.strip() and not ckey_norm:
        return [], 0
    fts_parts: List[str] = []
    safe_q = _build_safe_fts_query(query)
    if safe_q:
        fts_parts.append(f"message : ({safe_q})")
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

    clean_cats = sorted({c.strip().upper() for c in categories or () if c and c.strip()})
    if clean_cats:
        placeholders = ",".join("?" for _ in clean_cats)
        where_clauses.append(f"e.category IN ({placeholders})")
        params.extend(clean_cats)
    else:
        if not where_clauses:
            return [], 0
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

    where_clauses.extend((
        "e.generation = r.active_generation", "r.has_game_log=1",
        "r.missing_since IS NULL", "r.indexed_at IS NOT NULL", _visible_rounds("r"),
    ))
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

    async with conn.execute(
        f"""
        WITH page AS (
            SELECT e.id AS entry_id, e.round_uid, e.file_name, e.line_no, e.timestamp,
                   e.category, e.ckeys_space, e.message, e.coord_x, e.coord_y, e.coord_z,
                   r.round_id, r.round_date, r.source_type, r.label, r.map_revision
            {from_sql}
            WHERE {where_sql}
            ORDER BY r.round_date DESC, e.timestamp DESC, e.line_no, e.id
            LIMIT ? OFFSET ?
        ), totals AS (SELECT COUNT(*) AS total {from_sql} WHERE {where_sql})
        SELECT page.*, totals.total FROM totals LEFT JOIN page ON 1=1
        ORDER BY page.round_date DESC, page.timestamp DESC, page.line_no, page.entry_id
        """,
        [*params, limit, offset, *params],
    ) as cursor:
        rows = await cursor.fetchall()
    total = int(rows[0]["total"])
    hits = []
    for row in rows:
        if row["entry_id"] is None:
            continue
        d = dict(row)
        del d["total"]
        d["ckeys"] = [c for c in (d.get("ckeys_space") or "").split(" ") if c]
        hits.append(d)
    return hits, total
