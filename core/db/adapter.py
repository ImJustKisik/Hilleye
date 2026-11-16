"""Object-oriented Database adapter delegating through the core.db package namespace."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set


class Database:
    """Wrapper class providing an object-oriented interface for the SQLite WAL engine."""

    def __init__(self, db_path: str = "tracker.db"):
        self.db_path = db_path

    async def init_db(self) -> None:
        import core.db as _db

        await _db.init_db(self.db_path)

    async def close_db(self) -> None:
        import core.db as _db

        await _db.close_db()

    async def upsert_server(
        self,
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
        import core.db as _db

        return await _db.upsert_server(
            identifier,
            title,
            ip,
            port,
            is_active=is_active,
            now_ts=now_ts,
            current_players=current_players,
            map_name=map_name,
            round_duration=round_duration,
            gamemode=gamemode,
            admins_count=admins_count,
            version=version,
            active_players=active_players,
            station_time=station_time,
            enter_allowed=enter_allowed,
            respawn_allowed=respawn_allowed,
            admin_list=admin_list,
            manifest=manifest,
            live_identifiers=live_identifiers,
            target_server_id=target_server_id,
            cycle_claimed_ids=cycle_claimed_ids,
        )

    async def consolidate_duplicate_servers(self) -> int:
        import core.db as _db

        return await _db.consolidate_duplicate_servers()

    async def record_server_snapshot(
        self,
        server_id: int,
        player_count: int,
        round_duration: int = 0,
        map_name: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> int:
        import core.db as _db

        return await _db.record_server_snapshot(server_id, player_count, round_duration, map_name, ts)

    async def prune_server_snapshots(
        self, max_age_days: int = 7, now_ts: Optional[float] = None
    ) -> int:
        import core.db as _db

        return await _db.prune_server_snapshots(max_age_days, now_ts)

    async def get_server_details(
        self, server_id: int, now_ts: Optional[float] = None, time_range: str = 'day'
    ) -> Optional[Dict[str, Any]]:
        import core.db as _db

        return await _db.get_server_details(server_id, now_ts=now_ts, time_range=time_range)

    async def get_active_servers(self) -> List[Dict[str, Any]]:
        import core.db as _db

        return await _db.get_active_servers()

    async def update_player_sessions(
        self,
        server_id: int,
        online_ckeys: Set[str],
        now_ts: Optional[float] = None,
        grace_period_seconds: int = 300,
    ) -> None:
        import core.db as _db

        await _db.update_player_sessions(server_id, online_ckeys, now_ts, grace_period_seconds)

    async def get_player_dossier(
        self, ckey: str, sessions_limit: int = 10, sessions_offset: int = 0
    ) -> Optional[Dict[str, Any]]:
        import core.db as _db

        return await _db.get_player_dossier(ckey, sessions_limit=sessions_limit, sessions_offset=sessions_offset)

    async def get_network_overview(self, recent_players_limit: int = 8) -> Dict[str, Any]:
        import core.db as _db

        return await _db.get_network_overview(recent_players_limit=recent_players_limit)

    async def search_players(
        self, query: str, limit: int = 8, include_fuzzy: bool = True
    ) -> List[Dict[str, Any]]:
        import core.db as _db

        return await _db.search_players(query, limit=limit, include_fuzzy=include_fuzzy)
