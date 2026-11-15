"""Player dossiers, network overview aggregations, 24h charts, and server detail queries."""

from __future__ import annotations

import difflib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from core.db.connection import _read_snapshot
from core.db.schema import format_duration, format_hms, format_timestamp, normalize_ckey


@_read_snapshot
async def search_players(
    query: str,
    limit: int = 8,
    include_fuzzy: bool = True,
) -> List[Dict[str, Any]]:
    """
    Searches players by prefix, substring, and optional fuzzy (edit-distance) similarity.
    Returns ranked player candidate dictionaries with online status, current server,
    total playtime hours, session count, and last seen timestamp.
    """
    import core.db as _db

    norm_q = normalize_ckey(query)
    if not norm_q:
        return []
    safe_limit = max(1, min(32, int(limit)))

    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        # 1. Exact, prefix, and substring matches in SQL
        await cur.execute(
            """
            SELECT
                p.ckey,
                p.last_seen,
                COALESCE(t.total_seconds, 0) AS total_seconds,
                COALESCE(t.sessions_count, 0) AS sessions_count,
                act.server_id AS current_server_id,
                act.server_title AS current_server,
                CASE
                    WHEN p.ckey = ? THEN 0
                    WHEN p.ckey LIKE ? THEN 1
                    ELSE 2
                END AS match_rank
            FROM players p
            LEFT JOIN (
                SELECT ckey, SUM(total_seconds) AS total_seconds, SUM(sessions_count) AS sessions_count
                FROM server_player_totals
                GROUP BY ckey
            ) t ON t.ckey = p.ckey
            LEFT JOIN (
                SELECT s.ckey, s.server_id, COALESCE(srv.title, 'Sector #' || s.server_id) AS server_title
                FROM sessions s
                LEFT JOIN servers srv ON srv.id = s.server_id
                WHERE s.ended_at IS NULL
                GROUP BY s.ckey
            ) act ON act.ckey = p.ckey
            WHERE p.ckey LIKE ?
            ORDER BY
                match_rank ASC,
                (act.server_id IS NOT NULL) DESC,
                total_seconds DESC,
                p.last_seen DESC,
                LENGTH(p.ckey) ASC,
                p.ckey ASC
            LIMIT ?;
            """,
            (norm_q, f"{norm_q}%", f"%{norm_q}%", safe_limit),
        )
        direct_rows = await cur.fetchall()

        results: List[Dict[str, Any]] = []
        seen_ckeys = set()

        for r in direct_rows:
            ck = r["ckey"]
            seen_ckeys.add(ck)
            tot_sec = int(r["total_seconds"] or 0)
            rank_val = int(r["match_rank"])
            match_kind = "exact" if rank_val == 0 else ("prefix" if rank_val == 1 else "substring")
            results.append(
                {
                    "ckey": ck,
                    "is_online": r["current_server_id"] is not None,
                    "current_server_id": int(r["current_server_id"]) if r["current_server_id"] is not None else None,
                    "current_server": r["current_server"],
                    "total_seconds": tot_sec,
                    "total_hours": round(tot_sec / 3600.0, 1),
                    "sessions_count": int(r["sessions_count"] or 0),
                    "last_seen": format_timestamp(r["last_seen"]),
                    "match_kind": match_kind,
                }
            )

        # 2. Fuzzy fallback when direct matches are fewer than requested limit
        remaining = safe_limit - len(results)
        if include_fuzzy and remaining > 0 and len(norm_q) >= 2:
            q_len = len(norm_q)
            min_len = max(1, q_len - 4)
            max_len = q_len + 6
            await cur.execute(
                """
                SELECT
                    p.ckey,
                    p.last_seen,
                    COALESCE(t.total_seconds, 0) AS total_seconds,
                    COALESCE(t.sessions_count, 0) AS sessions_count,
                    act.server_id AS current_server_id,
                    act.server_title AS current_server
                FROM players p
                LEFT JOIN (
                    SELECT ckey, SUM(total_seconds) AS total_seconds, SUM(sessions_count) AS sessions_count
                    FROM server_player_totals
                    GROUP BY ckey
                ) t ON t.ckey = p.ckey
                LEFT JOIN (
                    SELECT s.ckey, s.server_id, COALESCE(srv.title, 'Sector #' || s.server_id) AS server_title
                    FROM sessions s
                    LEFT JOIN servers srv ON srv.id = s.server_id
                    WHERE s.ended_at IS NULL
                    GROUP BY s.ckey
                ) act ON act.ckey = p.ckey
                WHERE LENGTH(p.ckey) BETWEEN ? AND ?
                ORDER BY (act.server_id IS NOT NULL) DESC, p.last_seen DESC
                LIMIT 1500;
                """,
                (min_len, max_len),
            )
            candidate_rows = await cur.fetchall()
            cutoff = 0.6 if q_len >= 4 else 0.66
            scored: List[Tuple[float, int, int, float, Dict[str, Any]]] = []
            for r in candidate_rows:
                ck = r["ckey"]
                if ck in seen_ckeys:
                    continue
                ratio = difflib.SequenceMatcher(None, norm_q, ck).ratio()
                if ck[0] == norm_q[0]:
                    ratio += 0.04
                if ratio >= cutoff:
                    tot_sec = int(r["total_seconds"] or 0)
                    is_on = 1 if r["current_server_id"] is not None else 0
                    last_ts = float(r["last_seen"] or 0.0)
                    scored.append(
                        (
                            ratio,
                            is_on,
                            tot_sec,
                            last_ts,
                            {
                                "ckey": ck,
                                "is_online": bool(is_on),
                                "current_server_id": int(r["current_server_id"]) if is_on else None,
                                "current_server": r["current_server"],
                                "total_seconds": tot_sec,
                                "total_hours": round(tot_sec / 3600.0, 1),
                                "sessions_count": int(r["sessions_count"] or 0),
                                "last_seen": format_timestamp(r["last_seen"]),
                                "match_kind": "fuzzy",
                            },
                        )
                    )
            scored.sort(key=lambda item: (item[0], item[1], item[2], item[3]), reverse=True)
            for item in scored[:remaining]:
                results.append(item[4])

    return results


@_read_snapshot
async def get_player_dossier(
    ckey: str,
    sessions_limit: int = 10,
    sessions_offset: int = 0,
) -> Optional[Dict[str, Any]]:
    """
    Returns tactical player dossier dictionary or None if player does not exist.

    Aggregates:
    - Target status (online/offline, current sector, current duration)
    - Summary metrics (total playtime seconds, total hours, count of distinct servers visited, first and last seen)
    - Server breakdown with percentage and last visit timestamps
    - Recent sessions log (paginated via sessions_limit and sessions_offset)
    """
    import core.db as _db

    norm_ckey = normalize_ckey(ckey)
    if not norm_ckey:
        return None
    limit = max(1, min(100, int(sessions_limit)))
    offset = max(0, int(sessions_offset))

    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        # Check player existence
        await cur.execute(
            "SELECT ckey, first_seen, last_seen FROM players WHERE ckey = ?;",
            (norm_ckey,),
        )
        player_row = await cur.fetchone()
        if player_row is None:
            return None

        # Check active session
        await cur.execute(
            """
            SELECT s.id, s.server_id, s.started_at, s.last_seen_at, s.duration_seconds, srv.title as server_title
            FROM sessions s
            LEFT JOIN servers srv ON s.server_id = srv.id
            WHERE s.ckey = ? AND s.ended_at IS NULL
            ORDER BY s.started_at DESC
            LIMIT 1;
            """,
            (norm_ckey,),
        )
        active_sess = await cur.fetchone()

        if active_sess is not None:
            is_online = True
            current_server_id: Optional[int] = int(active_sess["server_id"])
            current_server = active_sess["server_title"] or f"Sector #{active_sess['server_id']}"
            current_duration_str = format_duration(active_sess["duration_seconds"])
        else:
            is_online = False
            current_server_id = None
            current_server = None
            current_duration_str = None

        # Playtime summary
        await cur.execute(
            """
            SELECT 
                COALESCE(SUM(duration_seconds), 0) as total_playtime_seconds,
                COUNT(DISTINCT server_id) as servers_count,
                COUNT(id) as sessions_count
            FROM sessions
            WHERE ckey = ?;
            """,
            (norm_ckey,),
        )
        summary_row = await cur.fetchone()
        total_playtime_seconds = int(summary_row["total_playtime_seconds"]) if summary_row else 0
        total_hours = round(total_playtime_seconds / 3600.0, 1)
        servers_count = int(summary_row["servers_count"]) if summary_row else 0
        sessions_count = int(summary_row["sessions_count"]) if summary_row else 0

        first_seen_str = format_timestamp(player_row["first_seen"])
        last_seen_str = format_timestamp(player_row["last_seen"])

        # Server breakdown
        await cur.execute(
            """
            SELECT 
                s.server_id,
                COALESCE(srv.title, 'Unknown Sector ' || s.server_id) as title,
                COALESCE(SUM(s.duration_seconds), 0) as total_seconds,
                MAX(s.last_seen_at) as last_visit
            FROM sessions s
            LEFT JOIN servers srv ON s.server_id = srv.id
            WHERE s.ckey = ?
            GROUP BY s.server_id
            ORDER BY total_seconds DESC;
            """,
            (norm_ckey,),
        )
        breakdown_rows = await cur.fetchall()
        server_breakdown: List[Dict[str, Any]] = []
        for b in breakdown_rows:
            sec = int(b["total_seconds"])
            pct = round((sec / total_playtime_seconds * 100.0), 1) if total_playtime_seconds > 0 else 0.0
            server_breakdown.append(
                {
                    "server_id": b["server_id"],
                    "title": b["title"],
                    "seconds": sec,
                    "hours": round(sec / 3600.0, 1),
                    "percentage": pct,
                    "last_visit": format_timestamp(b["last_visit"]),
                }
            )

        # Recent sessions (paginated)
        await cur.execute(
            """
            SELECT 
                s.id,
                s.server_id,
                COALESCE(srv.title, 'Unknown Sector ' || s.server_id) as server_title,
                s.started_at,
                s.last_seen_at,
                s.ended_at,
                s.duration_seconds
            FROM sessions s
            LEFT JOIN servers srv ON s.server_id = srv.id
            WHERE s.ckey = ?
            ORDER BY s.started_at DESC, s.id DESC
            LIMIT ? OFFSET ?;
            """,
            (norm_ckey, limit, offset),
        )
        sess_rows = await cur.fetchall()
        recent_sessions: List[Dict[str, Any]] = []
        for s in sess_rows:
            dur = int(s["duration_seconds"])
            recent_sessions.append(
                {
                    "server_id": s["server_id"],
                    "server_title": s["server_title"],
                    "started_at": format_timestamp(s["started_at"]),
                    "ended_at": format_timestamp(s["ended_at"]) if s["ended_at"] is not None else None,
                    "duration_seconds": dur,
                    "duration_str": format_duration(dur),
                    "is_active": s["ended_at"] is None,
                }
            )

    dossier: Dict[str, Any] = {
        "ckey": norm_ckey,
        "is_online": is_online,
        "current_server": current_server,
        "current_server_id": current_server_id,
        "current_session_duration": current_duration_str,
        "first_seen": first_seen_str,
        "last_seen": last_seen_str,
        "total_playtime_seconds": total_playtime_seconds,
        "total_hours": total_hours,
        "servers_count": servers_count,
        "sessions_count": sessions_count,
        "sessions_limit": limit,
        "sessions_offset": offset,
        "has_more_sessions": (offset + len(recent_sessions)) < sessions_count,
        "status": {
            "online": is_online,
            "is_online": is_online,
            "current_server": current_server,
            "current_server_id": current_server_id,
            "current_session_duration": current_duration_str,
            "last_seen": last_seen_str,
        },
        "summary": {
            "total_playtime_seconds": total_playtime_seconds,
            "total_hours": total_hours,
            "servers_count": servers_count,
            "sessions_count": sessions_count,
            "first_seen": first_seen_str,
            "last_seen": last_seen_str,
        },
        "server_breakdown": server_breakdown,
        "servers": server_breakdown,
        "recent_sessions": recent_sessions,
    }
    return dossier


def _build_chart_24h(points: List[Dict[str, Any]], window_start=None, window_end=None) -> Dict[str, Any]:
    """Builds lightweight SVG polyline coordinates (viewBox 0 0 600 120) and summary metrics."""
    if not points:
        return {
            "points_str": "",
            "area_points_str": "",
            "min_players": 0,
            "max_players": 0,
            "avg_players": 0.0,
            "count": 0,
            "start_label": None,
            "end_label": None,
            "window_start_label": format_timestamp(window_start) if window_start is not None else None,
            "window_end_label": format_timestamp(window_end) if window_end is not None else None,
            "segments": [],
        }

    counts = [int(p["player_count"]) for p in points]
    min_p = min(counts)
    max_p = max(counts)
    avg_p = round(sum(counts) / len(counts), 1)

    x_min, x_max = 10.0, 590.0
    y_top, y_bottom = 16.0, 104.0

    def _calc_y(val: int) -> float:
        if max_p <= 0:
            return y_bottom
        if max_p == min_p:
            return (y_top + y_bottom) / 2.0
        ratio = (val - min_p) / float(max_p - min_p)
        return round(y_bottom - ratio * (y_bottom - y_top), 1)

    coords: List[Tuple[float, float]] = []
    n = len(points)
    start = window_start if window_start is not None else points[0]["timestamp"]
    end = window_end if window_end is not None else points[-1]["timestamp"]
    segments = []
    segment = []
    for i, c in enumerate(counts):
        x_val = round(x_min + ((points[i]["timestamp"] - start) / max(1, end - start)) * (x_max - x_min), 1)
        coord = (x_val, _calc_y(c))
        coords.append(coord)
        if i and points[i].get("gap_before"):
            segments.append(segment)
            segment = []
        segment.append(coord)
    segments.append(segment)

    def segment_data(segment):
        line = " ".join(f"{x},{y}" for x, y in segment)
        return {
            "points_str": line,
            "area_points_str": f"{segment[0][0]},{y_bottom} {line} {segment[-1][0]},{y_bottom}",
            "x": segment[0][0],
            "y": segment[0][1],
            "single": len(segment) == 1,
        }

    points_str = " ".join(f"{x},{y}" for x, y in coords)
    area_points_str = f"{coords[0][0]},{y_bottom} {points_str} {coords[-1][0]},{y_bottom}"

    return {
        "points_str": points_str,
        "area_points_str": area_points_str,
        "min_players": min_p,
        "max_players": max_p,
        "avg_players": avg_p,
        "count": n,
        "start_label": points[0].get("time_str"),
        "end_label": points[-1].get("time_str"),
        "window_start_label": format_timestamp(window_start) if window_start is not None else points[0].get("time_str"),
        "window_end_label": format_timestamp(window_end) if window_end is not None else points[-1].get("time_str"),
        "segments": [segment_data(s) for s in segments],
    }


@_read_snapshot
async def get_network_overview(recent_players_limit: int = 8) -> Dict[str, Any]:
    """Returns network-wide player/session totals and recently seen players for the dashboard."""
    import core.db as _db

    limit = max(1, min(32, int(recent_players_limit)))
    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) AS total_players FROM players;")
        total_players = int((await cur.fetchone())["total_players"] or 0)

        await cur.execute(
            """
            SELECT
                COUNT(*) AS total_sessions,
                COALESCE(SUM(CASE WHEN ended_at IS NULL THEN 1 ELSE 0 END), 0) AS active_sessions
            FROM sessions;
            """
        )
        sess_row = await cur.fetchone()
        total_sessions = int(sess_row["total_sessions"] or 0) if sess_row else 0
        active_sessions = int(sess_row["active_sessions"] or 0) if sess_row else 0

        await cur.execute("SELECT COALESCE(SUM(total_seconds), 0) AS total_seconds FROM server_player_totals;")
        total_seconds = int((await cur.fetchone())["total_seconds"] or 0)

        await cur.execute(
            """
            SELECT
                p.ckey,
                p.last_seen,
                EXISTS(
                    SELECT 1 FROM sessions s
                    WHERE s.ckey = p.ckey AND s.ended_at IS NULL
                ) AS is_online
            FROM players p
            ORDER BY is_online DESC, p.last_seen DESC, p.ckey ASC
            LIMIT ?;
            """,
            (limit,),
        )
        recent_rows = await cur.fetchall()

    return {
        "total_players": total_players,
        "total_sessions": total_sessions,
        "active_sessions": active_sessions,
        "total_hours": round(total_seconds / 3600.0, 1),
        "recent_players": [
            {
                "ckey": r["ckey"],
                "last_seen": format_timestamp(r["last_seen"]),
                "is_online": bool(r["is_online"]),
            }
            for r in recent_rows
        ],
    }


@_read_snapshot
async def get_server_details(
    server_id: int,
    now_ts: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """
    Returns detailed server status, current round parameters, active online players,
    top-20 players by playtime on this server, aggregate server statistics, and 24h online history.
    Returns None if server_id does not exist.
    """
    import core.db as _db

    conn = await _db.get_connection()
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT
                id, identifier, title, ip, port, last_seen, is_active,
                current_players, map_name, round_duration, gamemode, admins_count, version,
                active_players, station_time, enter_allowed, respawn_allowed, admin_list_json, manifest_json,
                roster_observed_at, telemetry_observed_at, manifest_observed_at
            FROM servers
            WHERE id = ?;
            """,
            (int(server_id),),
        )
        srv_row = await cur.fetchone()
        if srv_row is None:
            return None

        # Active online players on this server
        await cur.execute(
            """
            SELECT
                ckey,
                started_at,
                last_seen_at,
                duration_seconds
            FROM sessions
            WHERE server_id = ? AND ended_at IS NULL
            ORDER BY started_at ASC;
            """,
            (int(server_id),),
        )
        online_rows = await cur.fetchall()
        online_players: List[Dict[str, Any]] = []
        current_wall_ts = time.time()
        for r in online_rows:
            started_ts = float(r["started_at"])
            stored_dur = int(r["duration_seconds"] or 0)
            dur = stored_dur

            online_players.append(
                {
                    "ckey": r["ckey"],
                    "started_at": format_timestamp(started_ts),
                    "started_at_ts": started_ts,
                    "duration_seconds": dur,
                    "duration_str": format_duration(dur),
                }
            )

        # Top-20 players on this server by total hours/seconds
        await cur.execute(
            """
            SELECT
                ckey,
                total_seconds,
                last_seen
            FROM server_player_totals
            WHERE server_id = ?
            ORDER BY total_seconds DESC, ckey ASC
            LIMIT 20;
            """,
            (int(server_id),),
        )
        top_rows = await cur.fetchall()
        top_players: List[Dict[str, Any]] = []
        for idx, r in enumerate(top_rows):
            tot_sec = int(r["total_seconds"] or 0)
            tot_hours = round(tot_sec / 3600.0, 1)
            top_players.append(
                {
                    "rank": idx + 1,
                    "ckey": r["ckey"],
                    "total_seconds": tot_sec,
                    "total_hours": tot_hours,
                    "hours": tot_hours,
                    "duration_str": format_duration(tot_sec),
                    "last_seen": format_timestamp(r["last_seen"]),
                    "last_seen_ts": float(r["last_seen"]) if r["last_seen"] is not None else None,
                }
            )

        # Aggregate server statistics
        await cur.execute(
            """
            SELECT
                COUNT(*) AS unique_players,
                COALESCE(SUM(total_seconds), 0) AS total_playtime_seconds
            FROM server_player_totals
            WHERE server_id = ?;
            """,
            (int(server_id),),
        )
        agg_row = await cur.fetchone()
        unique_players = int(agg_row["unique_players"] or 0) if agg_row else 0
        total_playtime_seconds = int(agg_row["total_playtime_seconds"] or 0) if agg_row else 0
        total_hours = round(total_playtime_seconds / 3600.0, 1)

        # 24-hour online history points
        ref_snap_ts = float(now_ts) if now_ts is not None else current_wall_ts

        cutoff_24h = ref_snap_ts - 86400.0
        await cur.execute(
            """
            SELECT id, timestamp, player_count, round_duration, map_name
            FROM server_snapshots
            WHERE server_id = ? AND timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC;
            """,
            (int(server_id), cutoff_24h, ref_snap_ts),
        )
        snap_rows = await cur.fetchall()

        # Preserve gaps before reducing dense observations into minute maxima.
        snapshots_24h: List[Dict[str, Any]] = []
        previous_ts = None
        for sr in snap_rows:
            ts = float(sr["timestamp"])
            gap = previous_ts is not None and ts - previous_ts > 600
            point = dict(
                timestamp=ts,
                time_str=format_timestamp(ts),
                player_count=int(sr["player_count"]),
                round_duration=sr["round_duration"],
                map_name=sr["map_name"],
                gap_before=gap,
            )
            if snapshots_24h and not gap and int(ts // 60) == int(snapshots_24h[-1]["timestamp"] // 60):
                if point["player_count"] >= snapshots_24h[-1]["player_count"]:
                    point["gap_before"] = snapshots_24h[-1]["gap_before"]
                    snapshots_24h[-1] = point
            else:
                snapshots_24h.append(point)
            previous_ts = ts

    chart_24h = _db._build_chart_24h(snapshots_24h, cutoff_24h, ref_snap_ts)
    if snap_rows:
        counts = [int(row["player_count"]) for row in snap_rows]
        chart_24h.update(min_players=min(counts), max_players=max(counts), avg_players=round(sum(counts) / len(counts), 1))

    raw_current_players = srv_row["current_players"]
    effective_current_players = raw_current_players
    round_dur_sec = srv_row["round_duration"]
    round_dur_str = format_hms(round_dur_sec) if round_dur_sec is not None else "Unknown"
    last_seen_str = format_timestamp(srv_row["last_seen"])
    is_active = bool(srv_row["is_active"])
    active_players_val = int(srv_row["active_players"]) if srv_row["active_players"] is not None else None
    station_time_val = srv_row["station_time"]
    enter_allowed_val = bool(srv_row["enter_allowed"]) if srv_row["enter_allowed"] is not None else None
    respawn_allowed_val = bool(srv_row["respawn_allowed"]) if srv_row["respawn_allowed"] is not None else None

    admin_list_parsed: List[Dict[str, str]] = []
    if srv_row["admin_list_json"]:
        try:
            loaded_admins = json.loads(srv_row["admin_list_json"])
            if isinstance(loaded_admins, list):
                admin_list_parsed = loaded_admins
        except Exception:
            pass

    manifest_parsed: Dict[str, List[Dict[str, str]]] = {}
    manifest_sections: List[Dict[str, Any]] = []
    manifest_total_crew = 0
    if srv_row["manifest_json"]:
        try:
            loaded_manifest = json.loads(srv_row["manifest_json"])
            if isinstance(loaded_manifest, dict):
                manifest_parsed = loaded_manifest
                for dept_key, crew_list in loaded_manifest.items():
                    if isinstance(crew_list, list) and crew_list:
                        manifest_total_crew += len(crew_list)
                        manifest_sections.append(
                            {
                                "key": dept_key,
                                "label": _db._DEPT_LABELS.get(dept_key.lower(), dept_key.upper()),
                                "crew": crew_list,
                                "count": len(crew_list),
                            }
                        )
        except Exception:
            pass

    details: Dict[str, Any] = {
        "id": int(srv_row["id"]),
        "server_id": int(srv_row["id"]),
        "identifier": srv_row["identifier"],
        "connect_uri": (
            f"byond://[{srv_row['ip']}]:{srv_row['port']}"
            if ":" in srv_row["ip"]
            else f"byond://{srv_row['ip']}:{srv_row['port']}"
        )
        if srv_row["port"]
        else srv_row["identifier"],
        "title": srv_row["title"],
        "ip": srv_row["ip"],
        "port": int(srv_row["port"]),
        "is_active": is_active,
        "last_seen": last_seen_str,
        "last_seen_ts": float(srv_row["last_seen"]),
        "current_players": effective_current_players,
        "active_players": active_players_val,
        "station_time": station_time_val,
        "enter_allowed": enter_allowed_val,
        "respawn_allowed": respawn_allowed_val,
        "admin_list": admin_list_parsed,
        "manifest": manifest_parsed,
        "manifest_sections": manifest_sections,
        "manifest_total_crew": manifest_total_crew,
        "manifest_observed_at": format_timestamp(srv_row["manifest_observed_at"]),
        "telemetry_observed_at": format_timestamp(srv_row["telemetry_observed_at"]),
        "roster_observed_at": format_timestamp(srv_row["roster_observed_at"]),
        "telemetry_stale": srv_row["telemetry_observed_at"] is None
        or ref_snap_ts - srv_row["telemetry_observed_at"] > 300,
        "topic_available": bool(srv_row["port"]),
        "map_name": srv_row["map_name"],
        "round_duration": round_dur_sec,
        "round_duration_str": round_dur_str,
        "gamemode": srv_row["gamemode"],
        "admins_count": srv_row["admins_count"],
        "version": srv_row["version"],
        "unique_players": unique_players,
        "total_playtime_seconds": total_playtime_seconds,
        "total_hours": total_hours,
        "online_players": online_players,
        "top_players": top_players,
        "snapshots_24h": snapshots_24h,
        "online_history": snapshots_24h,
        "chart_24h": chart_24h,
        "server": {
            "id": int(srv_row["id"]),
            "identifier": srv_row["identifier"],
            "title": srv_row["title"],
            "ip": srv_row["ip"],
            "port": int(srv_row["port"]),
            "is_active": is_active,
            "last_seen": last_seen_str,
            "current_players": effective_current_players,
            "active_players": active_players_val,
            "station_time": station_time_val,
            "enter_allowed": enter_allowed_val,
            "respawn_allowed": respawn_allowed_val,
            "map_name": srv_row["map_name"],
            "round_duration": round_dur_sec,
            "round_duration_str": round_dur_str,
            "gamemode": srv_row["gamemode"],
            "admins_count": srv_row["admins_count"],
            "version": srv_row["version"],
        },
        "round": {
            "current_players": effective_current_players,
            "active_players": active_players_val,
            "station_time": station_time_val,
            "enter_allowed": enter_allowed_val,
            "respawn_allowed": respawn_allowed_val,
            "map_name": srv_row["map_name"],
            "round_duration": round_dur_sec,
            "round_duration_str": round_dur_str,
            "gamemode": srv_row["gamemode"],
            "admins_count": srv_row["admins_count"],
            "version": srv_row["version"],
        },
        "summary": {
            "unique_players": unique_players,
            "total_playtime_seconds": total_playtime_seconds,
            "total_hours": total_hours,
        },
    }
    return details
