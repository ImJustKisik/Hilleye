"""Server registry upserts, observation persistence, snapshots, and collector state."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import aiosqlite

from core.db.connection import _read_snapshot, _write_transaction
from core.db.schema import (
    NON_IDENTIFYING_TITLES,
    format_hms,
    has_real_endpoint,
    normalize_server_title,
)


def _min_abs_diff(ts_a: Sequence[float], ts_b: Sequence[float]) -> float:
    """Returns minimum |a - b| between two sorted timestamp sequences in O(|A| + |B|)."""
    if not ts_a or not ts_b:
        return float("inf")
    ia = ib = 0
    best = float("inf")
    len_a, len_b = len(ts_a), len(ts_b)
    while ia < len_a and ib < len_b:
        diff = abs(ts_a[ia] - ts_b[ib])
        if diff < best:
            best = diff
            if best == 0.0:
                return 0.0
        if ts_a[ia] < ts_b[ib]:
            ia += 1
        else:
            ib += 1
    return best


def _merge_sorted_ts(ts_a: Sequence[float], ts_b: Sequence[float]) -> List[float]:
    """Merges two sorted timestamp sequences into a new sorted list."""
    out: List[float] = []
    ia = ib = 0
    len_a, len_b = len(ts_a), len(ts_b)
    while ia < len_a and ib < len_b:
        if ts_a[ia] <= ts_b[ib]:
            out.append(ts_a[ia])
            ia += 1
        else:
            out.append(ts_b[ib])
            ib += 1
    if ia < len_a:
        out.extend(ts_a[ia:])
    if ib < len_b:
        out.extend(ts_b[ib:])
    return out


def _sort_server_candidates(
    rows: Sequence[Any],
    sess_counts: Dict[int, int],
    snap_ts_map: Dict[int, List[float]],
) -> List[Dict[str, Any]]:
    """
    Orders candidate duplicate server rows from strongest canonical winner to weakest.
    Groups consecutive rows within the same active flag and 30s observation window so
    poll-order jitter does not outrank rows with real sessions/endpoints.
    """
    items = [dict(r) for r in rows]
    items.sort(
        key=lambda r: (1 if r["is_active"] else 0, float(r["last_seen"] or 0.0), int(r["id"])),
        reverse=True,
    )
    if len(items) <= 1:
        return items

    tiers: List[List[Dict[str, Any]]] = [[items[0]]]
    for item in items[1:]:
        prev = tiers[-1][-1]
        same_active = bool(item["is_active"]) == bool(prev["is_active"])
        within_poll = abs(float(prev["last_seen"] or 0.0) - float(item["last_seen"] or 0.0)) <= 30.0
        if same_active and within_poll:
            tiers[-1].append(item)
        else:
            tiers.append([item])

    ordered: List[Dict[str, Any]] = []
    for tier in tiers:
        tier.sort(
            key=lambda r: (
                1 if sess_counts.get(int(r["id"]), 0) > 0 else 0,
                1 if has_real_endpoint(r["ip"], r["port"]) else 0,
                len(snap_ts_map.get(int(r["id"]), ())),
                float(r["last_seen"] or 0.0),
                int(r["id"]),
            ),
            reverse=True,
        )
        ordered.extend(tier)
    return ordered


async def _load_obs_metadata(
    conn: aiosqlite.Connection,
    server_ids: Sequence[int],
) -> Tuple[Dict[int, List[float]], Dict[int, int]]:
    if not server_ids:
        return {}, {}
    placeholders = ",".join("?" for _ in server_ids)
    params = tuple(int(sid) for sid in server_ids)

    snap_ts_map: Dict[int, List[float]] = {int(sid): [] for sid in server_ids}
    async with conn.execute(
        f"SELECT server_id, timestamp FROM server_snapshots WHERE server_id IN ({placeholders}) ORDER BY server_id, timestamp ASC;",
        params,
    ) as cur:
        async for row in cur:
            snap_ts_map[int(row["server_id"])].append(float(row["timestamp"]))

    sess_counts: Dict[int, int] = {}
    async with conn.execute(
        f"SELECT server_id, COUNT(*) AS cnt FROM sessions WHERE server_id IN ({placeholders}) GROUP BY server_id;",
        params,
    ) as cur:
        async for row in cur:
            sess_counts[int(row["server_id"])] = int(row["cnt"] or 0)

    return snap_ts_map, sess_counts


async def _merge_server_pair(
    conn: aiosqlite.Connection,
    winner: Dict[str, Any],
    loser: Dict[str, Any],
) -> None:
    """Merges loser server row into winner row, preserving sessions, totals, snapshots, and alias."""
    winner_id = int(winner["id"])
    loser_id = int(loser["id"])
    if winner_id == loser_id:
        return

    new_ip = winner["ip"]
    new_port = int(winner["port"] or 0)
    if not has_real_endpoint(new_ip, new_port) and has_real_endpoint(loser["ip"], loser["port"]):
        new_ip = loser["ip"]
        new_port = int(loser["port"])
        winner["ip"] = new_ip
        winner["port"] = new_port

    new_title = winner["title"]
    if normalize_server_title(new_title) in NON_IDENTIFYING_TITLES and normalize_server_title(loser["title"]) not in NON_IDENTIFYING_TITLES:
        new_title = loser["title"]
        winner["title"] = new_title

    new_pref = winner.get("preferred_topic_query") or loser.get("preferred_topic_query")
    winner["preferred_topic_query"] = new_pref
    new_last_seen = max(float(winner["last_seen"] or 0.0), float(loser["last_seen"] or 0.0))
    winner["last_seen"] = new_last_seen

    await conn.execute(
        """
        UPDATE servers
        SET ip = ?, port = ?, title = ?, preferred_topic_query = ?, last_seen = MAX(last_seen, ?)
        WHERE id = ?;
        """,
        (new_ip, new_port, new_title, new_pref, new_last_seen, winner_id),
    )

    # Ensure moving sessions never violates idx_sessions_one_open (unique active session per server_id, ckey).
    async with conn.execute(
        """
        SELECT
            s_win.id AS win_id, s_win.started_at AS win_start, s_win.last_seen_at AS win_seen,
            s_lose.id AS lose_id, s_lose.started_at AS lose_start, s_lose.last_seen_at AS lose_seen
        FROM sessions s_win
        JOIN sessions s_lose ON s_win.ckey = s_lose.ckey
        WHERE s_win.server_id = ? AND s_win.ended_at IS NULL
          AND s_lose.server_id = ? AND s_lose.ended_at IS NULL;
        """,
        (winner_id, loser_id),
    ) as cur:
        collisions = await cur.fetchall()

    for col in collisions:
        win_seen = float(col["win_seen"])
        lose_seen = float(col["lose_seen"])
        if win_seen >= lose_seen:
            close_id = int(col["lose_id"])
            close_end = lose_seen
            close_dur = max(0, int(round(lose_seen - float(col["lose_start"]))))
        else:
            close_id = int(col["win_id"])
            close_end = win_seen
            close_dur = max(0, int(round(win_seen - float(col["win_start"]))))
        await conn.execute(
            "UPDATE sessions SET ended_at = ?, duration_seconds = ? WHERE id = ?;",
            (close_end, close_dur, close_id),
        )

    await conn.execute("UPDATE sessions SET server_id = ? WHERE server_id = ?;", (winner_id, loser_id))
    await conn.execute("DELETE FROM server_player_totals WHERE server_id = ?;", (loser_id,))
    await conn.execute("UPDATE server_snapshots SET server_id = ? WHERE server_id = ?;", (winner_id, loser_id))
    await conn.execute("UPDATE server_aliases SET canonical_id = ? WHERE canonical_id = ?;", (winner_id, loser_id))
    await conn.execute(
        "INSERT OR REPLACE INTO server_aliases (old_id, canonical_id) VALUES (?, ?);",
        (loser_id, winner_id),
    )
    await conn.execute("DELETE FROM servers WHERE id = ?;", (loser_id,))


async def _consolidate_group(
    conn: aiosqlite.Connection,
    rows: Sequence[Any],
    snap_ts_map: Dict[int, List[float]],
    sess_counts: Dict[int, int],
) -> int:
    ordered = _sort_server_candidates(rows, sess_counts, snap_ts_map)

    def _obs_ts(row: Dict[str, Any]) -> List[float]:
        snaps = snap_ts_map.get(int(row["id"]))
        if snaps:
            return list(snaps)
        return [float(row["last_seen"])] if row["last_seen"] is not None else []

    buckets: List[Dict[str, Any]] = []
    for row in ordered:
        row_ts = _obs_ts(row)
        matched_bucket = None
        for bucket in buckets:
            if _min_abs_diff(row_ts, bucket["obs_ts"]) >= 30.0:
                matched_bucket = bucket
                break
        if matched_bucket is not None:
            matched_bucket["losers"].append(row)
            matched_bucket["obs_ts"] = _merge_sorted_ts(matched_bucket["obs_ts"], row_ts)
        else:
            buckets.append({"winner": row, "obs_ts": row_ts, "losers": []})

    merged = 0
    for bucket in buckets:
        for loser in bucket["losers"]:
            await _merge_server_pair(conn, bucket["winner"], loser)
            merged += 1
    return merged


async def _consolidate_duplicate_servers(conn: aiosqlite.Connection) -> int:
    """
    Merges non-overlapping duplicate server rows (caused by ephemeral BYOND.world.<id>
    changes across restarts) inside an active transaction. Returns count of merged rows.
    """
    total_merged = 0
    for pass_kind in ("endpoint", "title"):
        async with conn.execute(
            """
            SELECT id, identifier, title, ip, port, last_seen, is_active, preferred_topic_query
            FROM servers;
            """
        ) as cur:
            all_rows = await cur.fetchall()
        if len(all_rows) <= 1:
            break

        groups: Dict[Any, List[Any]] = {}
        for r in all_rows:
            if pass_kind == "endpoint":
                if not has_real_endpoint(r["ip"], r["port"]):
                    continue
                key = (str(r["ip"]).strip().lower(), int(r["port"]))
            else:
                key = normalize_server_title(r["title"])
                if key in NON_IDENTIFYING_TITLES:
                    continue
            groups.setdefault(key, []).append(r)

        dup_groups = [grp for grp in groups.values() if len(grp) > 1]
        if not dup_groups:
            continue

        dup_ids = [int(r["id"]) for grp in dup_groups for r in grp]
        snap_ts_map, sess_counts = await _load_obs_metadata(conn, dup_ids)
        for grp in dup_groups:
            total_merged += await _consolidate_group(conn, grp, snap_ts_map, sess_counts)

    return total_merged


@_write_transaction
async def consolidate_duplicate_servers() -> int:
    """Runs duplicate server consolidation inside a write transaction."""
    import core.db as _db

    conn = await _db.get_connection()
    return await _consolidate_duplicate_servers(conn)


async def _find_canonical_server_row(
    conn: aiosqlite.Connection,
    identifier: str,
    title: str,
    ip: str,
    port: int,
    ts: float,
    live_identifiers: Optional[Set[str]] = None,
    exclude_server_ids: Optional[Set[int]] = None,
) -> Optional[Any]:
    """
    Resolves an incoming server observation to its canonical row in `servers`:
    1. Exact `identifier` match.
    2. Real `(ip, port)` match (excluding live/claimed rows).
    3. Normalized non-generic `title` match (excluding live/claimed rows).
    """
    row = await (await conn.execute("SELECT * FROM servers WHERE identifier = ?;", (identifier,))).fetchone()
    if row is not None:
        return row

    excluded = exclude_server_ids or set()
    live_idents = live_identifiers

    if has_real_endpoint(ip, port):
        rows = await (
            await conn.execute(
                """
                SELECT * FROM servers
                WHERE LOWER(ip) = LOWER(?) AND port = ?
                ORDER BY is_active DESC, last_seen DESC, id DESC;
                """,
                (str(ip).strip(), int(port)),
            )
        ).fetchall()
        for candidate in rows:
            if int(candidate["id"]) in excluded:
                continue
            if live_idents is not None and candidate["identifier"] in live_idents:
                continue
            return candidate

    norm_title = normalize_server_title(title)
    if norm_title not in NON_IDENTIFYING_TITLES:
        rows = await (
            await conn.execute("SELECT * FROM servers ORDER BY is_active DESC, last_seen DESC, id DESC;")
        ).fetchall()
        for candidate in rows:
            if int(candidate["id"]) in excluded:
                continue
            if live_idents is not None and candidate["identifier"] in live_idents:
                continue
            if normalize_server_title(candidate["title"]) != norm_title:
                continue
            if (
                live_idents is None
                and has_real_endpoint(ip, port)
                and has_real_endpoint(candidate["ip"], candidate["port"])
                and (str(ip).strip().lower(), int(port)) != (str(candidate["ip"]).strip().lower(), int(candidate["port"]))
                and abs(ts - float(candidate["last_seen"] or 0.0)) < 30.0
            ):
                continue
            return candidate

    return None


@_read_snapshot
async def resolve_servers_for_cycle(discovered_servers: Sequence[Any]) -> Dict[int, Dict[str, Any]]:
    """
    Resolves a poll cycle's discovered servers to existing `servers` rows in three passes
    (exact identifier -> real endpoint -> normalized title) so concurrent same-title
    servers never steal each other's rows and restarted pager servers keep their IDs/endpoints.
    """
    import core.db as _db

    if not discovered_servers:
        return {}

    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT id, identifier, title, ip, port, is_active, last_seen, preferred_topic_query
            FROM servers
            ORDER BY is_active DESC, last_seen DESC, id DESC;
            """
        )
        rows = await cur.fetchall()

    if not rows:
        return {}

    by_identifier: Dict[str, Any] = {}
    by_endpoint: Dict[Tuple[str, int], List[Any]] = {}
    by_title: Dict[str, List[Any]] = {}
    for r in rows:
        by_identifier[str(r["identifier"])] = r
        if has_real_endpoint(r["ip"], r["port"]):
            by_endpoint.setdefault((str(r["ip"]).strip().lower(), int(r["port"])), []).append(r)
        norm_t = normalize_server_title(r["title"])
        if norm_t not in NON_IDENTIFYING_TITLES:
            by_title.setdefault(norm_t, []).append(r)

    live_identifiers = {str(getattr(srv, "identifier", "") or "") for srv in discovered_servers}
    claimed_ids: Set[int] = set()
    assignments: Dict[int, Dict[str, Any]] = {}

    def _assign(idx: int, row: Any) -> None:
        sid = int(row["id"])
        claimed_ids.add(sid)
        assignments[idx] = {
            "id": sid,
            "identifier": str(row["identifier"]),
            "ip": str(row["ip"]),
            "port": int(row["port"] or 0),
            "preferred_topic_query": row["preferred_topic_query"],
        }

    # Pass 1: exact identifier match
    for idx, srv in enumerate(discovered_servers):
        ident = str(getattr(srv, "identifier", "") or "")
        if ident in by_identifier:
            _assign(idx, by_identifier[ident])

    # Pass 2: real (ip, port) match
    for idx, srv in enumerate(discovered_servers):
        if idx in assignments:
            continue
        srv_ip = str(getattr(srv, "ip", "") or "")
        srv_port = int(getattr(srv, "port", 0) or 0)
        if not has_real_endpoint(srv_ip, srv_port):
            continue
        for candidate in by_endpoint.get((srv_ip.strip().lower(), srv_port), ()):
            if int(candidate["id"]) not in claimed_ids and candidate["identifier"] not in live_identifiers:
                _assign(idx, candidate)
                break

    # Pass 3: normalized title match
    for idx, srv in enumerate(discovered_servers):
        if idx in assignments:
            continue
        norm_t = normalize_server_title(getattr(srv, "title", ""))
        if norm_t in NON_IDENTIFYING_TITLES:
            continue
        for candidate in by_title.get(norm_t, ()):
            if int(candidate["id"]) not in claimed_ids and candidate["identifier"] not in live_identifiers:
                _assign(idx, candidate)
                break

    return assignments


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
    *,
    live_identifiers: Optional[Set[str]] = None,
    target_server_id: Optional[int] = None,
    cycle_claimed_ids: Optional[Set[int]] = None,
) -> int:
    """
    Upserts server record and returns its database ID.
    Resolves canonical identity across BYOND.world.<id> restarts, preserving real
    endpoints and round history while keeping concurrent same-title servers distinct.
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
    ins_manifest_obs = ts if manifest is not None else None
    incoming_port = int(port)

    conn = await _db.get_connection()
    target_row = None
    if target_server_id is not None:
        ident_row = await (await conn.execute("SELECT * FROM servers WHERE identifier = ?;", (identifier,))).fetchone()
        if ident_row is not None:
            target_row = ident_row
        else:
            target_row = await (
                await conn.execute("SELECT * FROM servers WHERE id = ?;", (int(target_server_id),))
            ).fetchone()
    if target_row is None:
        target_row = await _find_canonical_server_row(
            conn=conn,
            identifier=identifier,
            title=title,
            ip=ip,
            port=incoming_port,
            ts=ts,
            live_identifiers=live_identifiers,
            exclude_server_ids=cycle_claimed_ids,
        )

    async with conn.cursor() as cur:
        if target_row is not None:
            sid = int(target_row["id"])
            old_norm_title = normalize_server_title(target_row["title"])
            new_norm_title = normalize_server_title(title)
            effective_title = (
                target_row["title"]
                if (new_norm_title in NON_IDENTIFYING_TITLES and old_norm_title not in NON_IDENTIFYING_TITLES)
                else title
            )
            if has_real_endpoint(ip, incoming_port):
                effective_ip = ip
                effective_port = incoming_port
            elif has_real_endpoint(target_row["ip"], target_row["port"]):
                effective_ip = target_row["ip"]
                effective_port = int(target_row["port"])
            else:
                effective_ip = ip
                effective_port = incoming_port

            await cur.execute(
                """
                UPDATE servers SET
                    identifier = ?,
                    title = ?,
                    ip = ?,
                    port = ?,
                    last_seen = MAX(servers.last_seen, ?),
                    is_active = ?,
                    current_players = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.current_players END,
                    map_name = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.map_name END,
                    round_duration = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.round_duration END,
                    gamemode = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.gamemode END,
                    admins_count = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.admins_count END,
                    version = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.version END,
                    active_players = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.active_players END,
                    station_time = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.station_time END,
                    enter_allowed = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.enter_allowed END,
                    respawn_allowed = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.respawn_allowed END,
                    admin_list_json = CASE WHEN ? IS NOT NULL THEN ? ELSE servers.admin_list_json END,
                    manifest_json = CASE
                        WHEN ? IS NOT NULL THEN ?
                        WHEN ? IS NOT NULL AND servers.round_duration IS NOT NULL
                             AND ? + 60 < servers.round_duration THEN NULL
                        ELSE servers.manifest_json
                    END,
                    manifest_observed_at = CASE
                        WHEN ? IS NOT NULL THEN ?
                        WHEN ? IS NOT NULL AND servers.round_duration IS NOT NULL
                             AND ? + 60 < servers.round_duration THEN NULL
                        ELSE servers.manifest_observed_at
                    END
                WHERE id = ?;
                """,
                (
                    identifier,
                    effective_title,
                    effective_ip,
                    effective_port,
                    ts,
                    1 if is_active else 0,
                    ins_players,
                    ins_players,
                    map_name,
                    map_name,
                    ins_round_dur,
                    ins_round_dur,
                    gamemode,
                    gamemode,
                    ins_admins,
                    ins_admins,
                    version,
                    version,
                    ins_active_players,
                    ins_active_players,
                    station_time,
                    station_time,
                    ins_enter,
                    ins_enter,
                    ins_respawn,
                    ins_respawn,
                    ins_admin_json,
                    ins_admin_json,
                    ins_manifest_json,
                    ins_manifest_json,
                    ins_round_dur,
                    ins_round_dur,
                    ins_manifest_obs,
                    ins_manifest_obs,
                    ins_round_dur,
                    ins_round_dur,
                    sid,
                ),
            )

            # If a booting server just revealed its real title, consolidate an unclaimed historical twin.
            if old_norm_title in NON_IDENTIFYING_TITLES and new_norm_title not in NON_IDENTIFYING_TITLES:
                other_rows = await (
                    await conn.execute(
                        "SELECT id, identifier, title, ip, port, last_seen, is_active, preferred_topic_query FROM servers WHERE id != ? ORDER BY is_active DESC, last_seen DESC, id DESC;",
                        (sid,),
                    )
                ).fetchall()
                excluded = cycle_claimed_ids or set()
                live_idents = live_identifiers or set()
                matching = [
                    r
                    for r in other_rows
                    if int(r["id"]) not in excluded
                    and r["identifier"] not in live_idents
                    and normalize_server_title(r["title"]) == new_norm_title
                ]
                if matching:
                    winner_fresh = dict(
                        await (
                            await conn.execute(
                                "SELECT id, identifier, title, ip, port, last_seen, is_active, preferred_topic_query FROM servers WHERE id = ?;",
                                (sid,),
                            )
                        ).fetchone()
                    )
                    snap_ts_map, _ = await _load_obs_metadata(conn, [sid] + [int(r["id"]) for r in matching])
                    winner_obs = snap_ts_map.get(sid) or ([float(winner_fresh["last_seen"])] if winner_fresh["last_seen"] is not None else [])
                    for cand in matching:
                        cand_id = int(cand["id"])
                        cand_obs = snap_ts_map.get(cand_id) or ([float(cand["last_seen"])] if cand["last_seen"] is not None else [])
                        if _min_abs_diff(winner_obs, cand_obs) >= 30.0:
                            await _merge_server_pair(conn, winner_fresh, dict(cand))
                            winner_obs = _merge_sorted_ts(winner_obs, cand_obs)
        else:
            await cur.execute(
                """
                INSERT INTO servers (
                    identifier, title, ip, port, last_seen, is_active,
                    current_players, map_name, round_duration, gamemode, admins_count, version,
                    active_players, station_time, enter_allowed, respawn_allowed, admin_list_json, manifest_json, manifest_observed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    identifier,
                    title,
                    ip,
                    incoming_port,
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
                    ins_manifest_obs,
                ),
            )
            sid = int(cur.lastrowid or 0)
            if sid <= 0:
                await cur.execute("SELECT id FROM servers WHERE identifier = ?;", (identifier,))
                row = await cur.fetchone()
                if row is None:
                    raise RuntimeError(f"Failed to retrieve id for upserted server '{identifier}'")
                sid = int(row["id"])

    if cycle_claimed_ids is not None:
        cycle_claimed_ids.add(sid)
    return sid


@_write_transaction
async def save_observation(
    server: Dict[str, Any],
    roster: Optional[Set[str]],
    grace_period_seconds: int,
    topic_received: bool,
    roster_complete: bool = True,
    *,
    live_identifiers: Optional[Set[str]] = None,
    target_server_id: Optional[int] = None,
    cycle_claimed_ids: Optional[Set[int]] = None,
) -> int:
    """Persist one observation atomically; an unknown roster never signals logout."""
    import core.db as _db

    if live_identifiers is None and target_server_id is None and cycle_claimed_ids is None:
        sid = await _db.upsert_server(**server)
    else:
        sid = await _db.upsert_server(
            **server,
            live_identifiers=live_identifiers,
            target_server_id=target_server_id,
            cycle_claimed_ids=cycle_claimed_ids,
        )
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
    await conn.execute("UPDATE servers SET is_active=0 WHERE is_active=1 AND last_seen < ?", (now_ts - max_age,))
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


@_read_snapshot
async def get_preferred_topic_queries() -> Dict[str, str]:
    """Returns mapping of server identifier -> preferred_topic_query for cached probes."""
    import core.db as _db

    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT identifier, preferred_topic_query FROM servers WHERE preferred_topic_query IS NOT NULL;"
        )
        rows = await cur.fetchall()
        return {str(r["identifier"]): str(r["preferred_topic_query"]) for r in rows if r["preferred_topic_query"]}


@_write_transaction
async def set_preferred_topic_query(identifier: str, query: Optional[str]) -> None:
    """Persists or clears the preferred_topic_query for a server identifier."""
    import core.db as _db

    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE servers SET preferred_topic_query = ? WHERE identifier = ?;",
            (query, identifier),
        )
