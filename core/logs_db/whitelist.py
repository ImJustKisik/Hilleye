"""Discord OAuth2 whitelist management and security audit event log."""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.telemetry import telemetry

logger = logging.getLogger(__name__)


def is_valid_discord_id(discord_id: str) -> bool:
    val = (discord_id or "").strip()
    return val.isdigit() and 15 <= len(val) <= 22


async def check_whitelist_access(
    discord_id: str,
    superadmin_ids: Sequence[str],
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """Return (allowed, role, db_entry). Superadmins from config always have 'superadmin' access."""
    import core.logs_db as _ldb

    did = (discord_id or "").strip()
    if not did:
        return False, "none", None
    if did in {str(s).strip() for s in superadmin_ids if s}:
        return True, "superadmin", {"discord_id": did, "role": "superadmin", "from_config": True}

    conn = await _ldb.get_logs_connection()
    cursor = await conn.execute(
        "SELECT discord_id, display_name, role, added_by, added_at FROM log_whitelist WHERE discord_id = ?",
        (did,),
    )
    row = await cursor.fetchone()
    if row is None:
        return False, "none", None
    entry = dict(row)
    role = entry.get("role") if entry.get("role") in ("viewer", "admin") else "viewer"
    return True, role, entry


async def list_whitelist(superadmin_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """Return combined view of config super-admins and dynamic SQLite whitelist entries."""
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    cursor = await conn.execute(
        "SELECT discord_id, display_name, role, added_by, added_at FROM log_whitelist ORDER BY added_at DESC"
    )
    rows = [dict(r) for r in await cursor.fetchall()]
    super_set = {str(s).strip() for s in superadmin_ids if s}
    seen = {r["discord_id"] for r in rows}

    for row in rows:
        row["is_config_superadmin"] = row["discord_id"] in super_set

    for sid in sorted(super_set):
        if sid not in seen:
            rows.insert(
                0,
                {
                    "discord_id": sid,
                    "display_name": "Суперадмин (config.json)",
                    "role": "superadmin",
                    "added_by": "config.json",
                    "added_at": None,
                    "is_config_superadmin": True,
                },
            )
    return rows


async def add_to_whitelist(
    discord_id: str,
    *,
    display_name: str = "",
    role: str = "viewer",
    added_by: str = "system",
) -> None:
    import core.logs_db as _ldb

    did = (discord_id or "").strip()
    if not is_valid_discord_id(did):
        raise ValueError("Некорректный Discord ID (ожидается 15–22 цифры).")
    clean_role = "admin" if role == "admin" else "viewer"
    clean_name = (display_name or "").strip()[:80]
    conn = await _ldb.get_logs_connection()
    await conn.execute(
        """
        INSERT INTO log_whitelist (discord_id, display_name, role, added_by, added_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
            display_name = CASE WHEN excluded.display_name != '' THEN excluded.display_name ELSE log_whitelist.display_name END,
            role = excluded.role,
            added_by = excluded.added_by,
            added_at = excluded.added_at
        """,
        (did, clean_name, clean_role, added_by, time.time()),
    )
    await conn.commit()


async def remove_from_whitelist(discord_id: str, superadmin_ids: Sequence[str]) -> bool:
    import core.logs_db as _ldb

    did = (discord_id or "").strip()
    if did in {str(s).strip() for s in superadmin_ids if s}:
        raise ValueError("Нельзя удалить супер-админа из config.json через веб-интерфейс.")
    conn = await _ldb.get_logs_connection()
    cur = await conn.execute("DELETE FROM log_whitelist WHERE discord_id = ?", (did,))
    await conn.commit()
    return cur.rowcount > 0


async def record_audit_event(
    action: str,
    *,
    discord_id: Optional[str] = None,
    discord_username: Optional[str] = None,
    target: Optional[str] = None,
    ip_address: Optional[str] = None,
    details: Optional[str] = None,
) -> None:
    import core.logs_db as _ldb

    if _ldb._connection is None:
        telemetry.log_audit_write_failures += 1
        return
    try:
        await _ldb._connection.execute(
            """
            INSERT INTO log_audit_events (created_at, discord_id, discord_username, action, target, ip_address, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                time.time(),
                discord_id,
                (discord_username or "")[:80] if discord_username else None,
                action[:40],
                (target or "")[:200] if target else None,
                (ip_address or "")[:64] if ip_address else None,
                (details or "")[:500] if details else None,
            ),
        )
        await _ldb._connection.commit()
    except Exception:
        telemetry.log_audit_write_failures += 1
        logger.warning("Failed to write log audit event (action=%s)", action, exc_info=True)


def _escape_like(val: str) -> str:
    return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _parse_utc_date_start(date_str: str) -> Optional[float]:
    raw = (date_str or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


async def get_audit_events(
    *,
    limit: int = 100,
    offset: int = 0,
    action_filter: str = "",
    actions: Optional[Sequence[str]] = None,
    q: str = "",
    user_filter: str = "",
    ip_filter: str = "",
    date_from: str = "",
    date_to: str = "",
) -> Tuple[List[Dict[str, Any]], int]:
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))

    clauses: List[str] = []
    params: List[Any] = []

    action = (action_filter or "").strip()
    if action:
        clauses.append("action = ?")
        params.append(action)
    elif actions:
        clean_actions = [a.strip() for a in actions if a and a.strip()]
        if clean_actions:
            placeholders = ",".join("?" for _ in clean_actions)
            clauses.append(f"action IN ({placeholders})")
            params.extend(clean_actions)

    uf = (user_filter or "").strip()
    if uf:
        clauses.append("(discord_id = ? OR LOWER(COALESCE(discord_username, '')) = LOWER(?))")
        params.extend([uf, uf])

    ipf = (ip_filter or "").strip()
    if ipf:
        clauses.append("ip_address = ?")
        params.append(ipf)

    ts_from = _parse_utc_date_start(date_from)
    if ts_from is not None:
        clauses.append("created_at >= ?")
        params.append(ts_from)

    ts_to_start = _parse_utc_date_start(date_to)
    if ts_to_start is not None:
        clauses.append("created_at < ?")
        params.append(ts_to_start + 86400.0)

    qf = (q or "").strip()
    if qf:
        like_pat = f"%{_escape_like(qf.lower())}%"
        clauses.append(
            "("
            "LOWER(COALESCE(discord_id, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(discord_username, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(action, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(target, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(ip_address, '')) LIKE ? ESCAPE '\\' OR "
            "LOWER(COALESCE(details, '')) LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([like_pat] * 6)

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cnt_cur = await conn.execute(f"SELECT COUNT(*) FROM log_audit_events {where_sql}", params)
    total = int((await cnt_cur.fetchone())[0])
    cur = await conn.execute(
        f"""
        SELECT id, created_at, discord_id, discord_username, action, target, ip_address, details
        FROM log_audit_events
        {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, limit, offset],
    )
    return [dict(r) for r in await cur.fetchall()], total


async def get_audit_summary() -> Dict[str, Any]:
    """Return aggregate audit metrics and per-user last-seen timestamps for admin overview."""
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    cutoff_24h = time.time() - 86400.0
    row = await (
        await conn.execute(
            """
            SELECT
                COUNT(*) AS total_all,
                SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS last_24h,
                SUM(CASE WHEN action = 'login_denied' AND created_at >= ? THEN 1 ELSE 0 END) AS denied_24h,
                SUM(CASE WHEN action = 'login_denied' THEN 1 ELSE 0 END) AS denied_total,
                COUNT(DISTINCT CASE WHEN created_at >= ? AND discord_id IS NOT NULL AND discord_id != '' THEN discord_id END) AS unique_users_24h,
                MAX(created_at) AS latest_event_at
            FROM log_audit_events
            """,
            (cutoff_24h, cutoff_24h, cutoff_24h),
        )
    ).fetchone()

    last_seen_cur = await conn.execute(
        """
        SELECT discord_id, MAX(created_at) AS last_seen_at
        FROM log_audit_events
        WHERE discord_id IS NOT NULL AND discord_id != ''
        GROUP BY discord_id
        """
    )
    last_seen_by_id = {
        str(r["discord_id"]): float(r["last_seen_at"])
        for r in await last_seen_cur.fetchall()
        if r["last_seen_at"] is not None
    }
    return {
        "total_all": int((row["total_all"] if row else 0) or 0),
        "last_24h": int((row["last_24h"] if row else 0) or 0),
        "denied_24h": int((row["denied_24h"] if row else 0) or 0),
        "denied_total": int((row["denied_total"] if row else 0) or 0),
        "unique_users_24h": int((row["unique_users_24h"] if row else 0) or 0),
        "latest_event_at": row["latest_event_at"] if row else None,
        "last_seen_by_id": last_seen_by_id,
    }
