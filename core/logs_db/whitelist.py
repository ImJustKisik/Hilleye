"""Discord OAuth2 whitelist management and security audit event log."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
        logger.debug("Failed to write log audit event", exc_info=True)


async def get_audit_events(
    *,
    limit: int = 100,
    offset: int = 0,
    action_filter: str = "",
) -> Tuple[List[Dict[str, Any]], int]:
    import core.logs_db as _ldb

    conn = await _ldb.get_logs_connection()
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    action = (action_filter or "").strip()

    if action:
        cnt_cur = await conn.execute("SELECT COUNT(*) FROM log_audit_events WHERE action = ?", (action,))
        total = int((await cnt_cur.fetchone())[0])
        cur = await conn.execute(
            """
            SELECT id, created_at, discord_id, discord_username, action, target, ip_address, details
            FROM log_audit_events
            WHERE action = ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (action, limit, offset),
        )
    else:
        cnt_cur = await conn.execute("SELECT COUNT(*) FROM log_audit_events")
        total = int((await cnt_cur.fetchone())[0])
        cur = await conn.execute(
            """
            SELECT id, created_at, discord_id, discord_username, action, target, ip_address, details
            FROM log_audit_events
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
    return [dict(r) for r in await cur.fetchall()], total
