"""Server registry upserts, observation persistence, snapshots, and collector state."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Set

from core.db.connection import _read_snapshot, _write_transaction
from core.db.schema import format_hms


@_write_transaction
async def upsert_server(
    identifier: str,
    title: str,
    ip: str,
    port: int,
    is_active: bool = True,
    now_ts: Optional[float] = None,
    current_players: Optional[int] = None,
    map_name: Optional[str] = None,
    round_duration: Optional[int] = None,
    gamemode: Optional[str] = None,
    admins_count: Optional[int] = None,
    version: Optional[str] = None,
    active_players: Optional[int] = None,
    station_time: Optional[str] = None,
    enter_allowed: Optional[bool] = None,
    respawn_allowed: Optional[bool] = None,
    admin_list: Optional[List[Dict[str, str]]] = None,
    manifest: Optional[Dict[str, List[Dict[str, str]]]] = None,
) -> int:
    """
    Upserts server record and returns its database ID.
    Updates title, ip, port, last_seen, is_active, and round metrics on identifier conflict.
    """
    import core.db as _db

    ts = time.time() if now_ts is None else float(now_ts)
    ins_players = int(current_players) if current_players is not None else None
    ins_round_dur = int(round_duration) if round_duration is not None else None
    ins_admins = int(admins_count) if admins_count is not None else None
    ins_active_players = int(active_players) if active_players is not None else None
    ins_enter = (1 if enter_allowed else 0) if enter_allowed is not None else None
    ins_respawn = (1 if respawn_allowed else 0) if respawn_allowed is not None else None
    ins_admin_json = json.dumps(admin_list, ensure_ascii=False) if admin_list is not None else None
    ins_manifest_json = json.dumps(manifest, ensure_ascii=False) if manifest is not None else None

    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO servers (
                identifier, title, ip, port, last_seen, is_active,
                current_players, map_name, round_duration, gamemode, admins_count, version,
                active_players, station_time, enter_allowed, respawn_allowed, admin_list_json, manifest_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identifier) DO UPDATE SET
                title = excluded.title,
                ip = excluded.ip,
                port = excluded.port,
                last_seen = MAX(servers.last_seen, excluded.last_seen),
                is_active = excluded.is_active,
                current_players = CASE WHEN ? IS NOT NULL THEN excluded.current_players ELSE servers.current_players END,
                map_name = CASE WHEN ? IS NOT NULL THEN excluded.map_name ELSE servers.map_name END,
                round_duration = CASE WHEN ? IS NOT NULL THEN excluded.round_duration ELSE servers.round_duration END,
                gamemode = CASE WHEN ? IS NOT NULL THEN excluded.gamemode ELSE servers.gamemode END,
                admins_count = CASE WHEN ? IS NOT NULL THEN excluded.admins_count ELSE servers.admins_count END,
                version = CASE WHEN ? IS NOT NULL THEN excluded.version ELSE servers.version END,
                active_players = CASE WHEN ? IS NOT NULL THEN excluded.active_players ELSE servers.active_players END,
                station_time = CASE WHEN ? IS NOT NULL THEN excluded.station_time ELSE servers.station_time END,
                enter_allowed = CASE WHEN ? IS NOT NULL THEN excluded.enter_allowed ELSE servers.enter_allowed END,
                respawn_allowed = CASE WHEN ? IS NOT NULL THEN excluded.respawn_allowed ELSE servers.respawn_allowed END,
                admin_list_json = CASE WHEN ? IS NOT NULL THEN excluded.admin_list_json ELSE servers.admin_list_json END,
                manifest_json = CASE WHEN ? IS NOT NULL THEN excluded.manifest_json ELSE servers.manifest_json END;
            """,
            (
                identifier,
                title,
                ip,
                int(port),
                ts,
                1 if is_active else 0,
                ins_players,
                map_name,
                ins_round_dur,
                gamemode,
                ins_admins,
                version,
                ins_active_players,
                station_time,
                ins_enter,
                ins_respawn,
                ins_admin_json,
                ins_manifest_json,
                current_players,
                map_name,
                round_duration,
                gamemode,
                admins_count,
                version,
                ins_active_players,
                station_time,
                ins_enter,
                ins_respawn,
                ins_admin_json,
                ins_manifest_json,
            ),
        )
        await cur.execute("SELECT id FROM servers WHERE identifier = ?;", (identifier,))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(f"Failed to retrieve id for upserted server '{identifier}'")
        if manifest is not None:
            await cur.execute("UPDATE servers SET manifest_observed_at=? WHERE id=?", (ts, row["id"]))
        return int(row["id"])


@_write_transaction
async def save_observation(
    server: Dict[str, Any],
    roster: Optional[Set[str]],
    grace_period_seconds: int,
    topic_received: bool,
    roster_complete: bool = True,
) -> int:
    """Persist one observation atomically; an unknown roster never signals logout."""
    import core.db as _db

    sid = await _db.upsert_server(**server)
    ts = server["now_ts"]
    if topic_received:
        conn = await _db.get_connection()
        await conn.execute("UPDATE servers SET telemetry_observed_at=? WHERE id=?", (ts, sid))
    if roster is not None:
        await _db.update_player_sessions(sid, roster, ts, grace_period_seconds, complete_roster=roster_complete)
        conn = await _db.get_connection()
        if roster_complete:
            await conn.execute("UPDATE servers SET roster_observed_at=? WHERE id=?", (ts, sid))
    if server.get("current_players") is not None:
        await _db.record_server_snapshot(
            sid, server["current_players"], server.get("round_duration"), server.get("map_name"), ts
        )
    return sid


@_write_transaction
async def expire_stale_servers(now_ts: float, max_age: int) -> None:
    """End observation windows at last confirmation, without crediting the outage."""
    import core.db as _db

    conn = await _db.get_connection()
    await conn.execute("UPDATE servers SET is_active=0 WHERE last_seen < ?", (now_ts - max_age,))
    await conn.execute(
        """UPDATE sessions SET ended_at=last_seen_at
                          WHERE ended_at IS NULL AND (last_seen_at < ? OR server_id IN
                          (SELECT id FROM servers WHERE is_active=0))""",
        (now_ts - max_age,),
    )


@_write_transaction
async def record_collector_cycle(completed_at: float, healthy: bool) -> None:
    import core.db as _db

    conn = await _db.get_connection()
    await conn.execute(
        """INSERT INTO collector_state VALUES (1,?,?) ON CONFLICT(id)
                          DO UPDATE SET completed_at=excluded.completed_at, healthy=excluded.healthy""",
        (completed_at, int(healthy)),
    )


@_write_transaction
async def save_manifest(server_id: int, manifest: Dict[str, Any], ts: float) -> None:
    import core.db as _db

    conn = await _db.get_connection()
    await conn.execute(
        "UPDATE servers SET manifest_json=?,manifest_observed_at=? WHERE id=?",
        (json.dumps(manifest, ensure_ascii=False), ts, server_id),
    )


@_read_snapshot
async def get_collector_state() -> Optional[Dict[str, Any]]:
    import core.db as _db

    conn = await _db.get_connection()
    row = await (await conn.execute("SELECT completed_at,healthy FROM collector_state WHERE id=1")).fetchone()
    return dict(row) if row else None


@_write_transaction
async def record_server_snapshot(
    server_id: int,
    player_count: int,
    round_duration: int = 0,
    map_name: Optional[str] = None,
    ts: Optional[float] = None,
) -> int:
    """Records a point-in-time server online snapshot for timeseries history."""
    import core.db as _db

    snap_ts = time.time() if ts is None else float(ts)
    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO server_snapshots (server_id, timestamp, player_count, round_duration, map_name)
            VALUES (?, ?, ?, ?, ?);
            """,
            (
                int(server_id),
                snap_ts,
                int(max(0, player_count)),
                int(max(0, round_duration)) if round_duration is not None else None,
                map_name,
            ),
        )
        return int(cur.lastrowid or 0)


@_write_transaction
async def prune_server_snapshots(
    max_age_days: int = 7,
    now_ts: Optional[float] = None,
) -> int:
    """Deletes server snapshots older than max_age_days and returns the count of deleted rows."""
    import core.db as _db

    ref_ts = time.time() if now_ts is None else float(now_ts)
    cutoff_ts = ref_ts - (max_age_days * 86400.0)
    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM server_snapshots WHERE timestamp < ?;",
            (cutoff_ts,),
        )
        deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
        return int(deleted)


@_read_snapshot
async def get_active_servers() -> List[Dict[str, Any]]:
    """Returns list of active monitored servers."""
    import core.db as _db

    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                id, identifier, title, ip, port, last_seen, is_active,
                current_players, map_name, round_duration, gamemode, admins_count, version,
                active_players, station_time, enter_allowed, respawn_allowed
            FROM servers
            WHERE is_active = 1
            ORDER BY current_players DESC, title ASC;
            """
        )
        rows = await cur.fetchall()
        return [
            {
                "id": r["id"],
                "identifier": r["identifier"],
                "title": r["title"],
                "ip": r["ip"],
                "port": r["port"],
                "last_seen": r["last_seen"],
                "is_active": bool(r["is_active"]),
                "current_players": r["current_players"],
                "map_name": r["map_name"],
                "round_duration": r["round_duration"],
                "round_duration_str": format_hms(r["round_duration"]) if r["round_duration"] is not None else "Unknown",
                "gamemode": r["gamemode"],
                "admins_count": r["admins_count"],
                "version": r["version"],
                "active_players": int(r["active_players"]) if r["active_players"] is not None else None,
                "station_time": r["station_time"],
                "enter_allowed": bool(r["enter_allowed"]) if r["enter_allowed"] is not None else None,
                "respawn_allowed": bool(r["respawn_allowed"]) if r["respawn_allowed"] is not None else None,
            }
            for r in rows
        ]
