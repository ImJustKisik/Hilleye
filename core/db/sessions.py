"""Player sessionization state machine with 5-minute grace period handling."""

from __future__ import annotations

import time
from typing import Optional, Set

from core.db.connection import _write_transaction
from core.db.schema import normalize_ckey


@_write_transaction
async def update_player_sessions(
    server_id: int,
    online_ckeys: Set[str],
    now_ts: Optional[float] = None,
    grace_period_seconds: int = 300,
    complete_roster: bool = True,
) -> None:
    """
    Executes sessionization step for a single server:
    1. Online players:
       - Upsert into players table (first_seen, last_seen).
       - Locate open session (ended_at IS NULL).
       - If found: extend last_seen_at = now, duration_seconds = now - started_at.
       - If not found: insert new open session (started_at=now, last_seen_at=now, ended_at=NULL, duration_seconds=0).
    2. Offline players with open sessions on this server:
       - If now - last_seen_at >= grace_period_seconds:
         close session (ended_at = last_seen_at, duration_seconds = last_seen_at - started_at).
       - If now - last_seen_at < grace_period_seconds:
         retain session in active state (ended_at IS NULL).
    """
    import core.db as _db

    ts = time.time() if now_ts is None else float(now_ts)
    normalized_online: Set[str] = {normalize_ckey(k) for k in online_ckeys if normalize_ckey(k)}

    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        # Step 1: Process Online Players
        for ckey in normalized_online:
            # Upsert into players
            await cur.execute(
                """
                INSERT INTO players (ckey, first_seen, last_seen)
                VALUES (?, ?, ?)
                ON CONFLICT(ckey) DO UPDATE SET last_seen = MAX(players.last_seen, excluded.last_seen);
                """,
                (ckey, ts, ts),
            )

            # Check open session on this server
            await cur.execute(
                """
                SELECT id, started_at, last_seen_at FROM sessions
                WHERE ckey = ? AND server_id = ? AND ended_at IS NULL
                LIMIT 1;
                """,
                (ckey, server_id),
            )
            open_sess = await cur.fetchone()

            if open_sess is not None:
                session_id = open_sess["id"]
                started_at = float(open_sess["started_at"])
                prev_last_seen = float(open_sess["last_seen_at"])
                absence_gap = ts - prev_last_seen

                if absence_gap < 0:
                    continue  # Late or backwards observations cannot rewrite chronology.
                is_expired_gap = absence_gap >= grace_period_seconds

                if is_expired_gap:
                    # Absence exceeded grace period: finalize expired session at previous last_seen
                    expired_duration = max(0, int(round(prev_last_seen - started_at)))
                    await cur.execute(
                        """
                        UPDATE sessions
                        SET ended_at = ?, duration_seconds = ?
                        WHERE id = ?;
                        """,
                        (prev_last_seen, expired_duration, session_id),
                    )
                    # Open brand new session starting now
                    await cur.execute(
                        """
                        INSERT INTO sessions (ckey, server_id, started_at, last_seen_at, ended_at, duration_seconds)
                        VALUES (?, ?, ?, ?, NULL, 0);
                        """,
                        (ckey, server_id, ts, ts),
                    )
                else:
                    # Within grace period: extend open session
                    duration = max(0, int(round(ts - started_at)))
                    await cur.execute(
                        """
                        UPDATE sessions
                        SET last_seen_at = ?, duration_seconds = ?
                        WHERE id = ?;
                        """,
                        (ts, duration, session_id),
                    )
            else:
                # Open brand new session
                await cur.execute(
                    """
                    INSERT INTO sessions (ckey, server_id, started_at, last_seen_at, ended_at, duration_seconds)
                    VALUES (?, ?, ?, ?, NULL, 0);
                    """,
                    (ckey, server_id, ts, ts),
                )

        # Step 2: Process Offline Players with Open Sessions on This Server
        await cur.execute(
            """
            SELECT id, ckey, started_at, last_seen_at FROM sessions
            WHERE server_id = ? AND ended_at IS NULL;
            """,
            (server_id,),
        )
        active_server_sessions = await cur.fetchall()

        for s_row in active_server_sessions:
            ckey = s_row["ckey"]
            if complete_roster and ckey not in normalized_online:
                last_seen_at = float(s_row["last_seen_at"])
                started_at = float(s_row["started_at"])
                delta = ts - last_seen_at

                if delta >= grace_period_seconds:
                    # Finalize session after grace period expiry
                    final_duration = max(0, int(round(last_seen_at - started_at)))
                    await cur.execute(
                        """
                        UPDATE sessions
                        SET ended_at = ?, duration_seconds = ?
                        WHERE id = ?;
                        """,
                        (last_seen_at, final_duration, s_row["id"]),
                    )
                else:
                    # Grace period active: retain open state
                    pass
