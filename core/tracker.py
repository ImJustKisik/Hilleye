"""Background Polling Worker and Session Tracking Engine for CyberSyn Intel.

Continuously discovers servers from the BYOND Hub and custom server registry,
queries each server concurrently over the BYOND Topic binary protocol (\x00\x83),
normalizes player ckeys, and drives the 5-minute (300s) grace period session state machine.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Set, Union

import httpx

from core.byond_topic import (
    parse_adminlist_value,
    parse_manifest_response,
    parse_round_duration,
    query_topic,
)
from core.config import AppConfig, get_config
from core.db import (
    get_active_servers,
    normalize_ckey,
    prune_server_snapshots,
    record_server_snapshot,
    update_player_sessions,
    upsert_server,
)
from core.hub_scraper import ServerInfo, fetch_hub_servers

logger = logging.getLogger(__name__)

# Re-export normalize_ckey as per dispatch specification
__all__ = ["Tracker", "get_tracker", "normalize_ckey"]


class _StartAwaitable:
    """Helper wrapper to allow Tracker.start() to be used with or without await."""

    def __init__(self, task: Optional[asyncio.Task]):
        self.task = task

    def __await__(self):
        async def _dummy():
            return self.task

        return _dummy().__await__()


class _StopAwaitable:
    """Helper wrapper to allow Tracker.stop() to be used with or without await."""

    def __init__(self, coro):
        self._coro = coro

    def __await__(self):
        return self._coro.__await__()


class Tracker:
    """
    Asynchronous background tracker managing polling ticks, socket concurrency,
    and database session updates.
    """

    def __init__(
        self,
        config: Optional[Union[AppConfig, Dict[str, Any]]] = None,
        db_path: Optional[str] = None,
        **kwargs: Any,
    ):
        # Allow passing config or loading global config
        if config is None:
            resolved_config = get_config()
        elif isinstance(config, AppConfig):
            resolved_config = config
        elif isinstance(config, dict):
            resolved_config = AppConfig(**config)
        else:
            resolved_config = get_config()

        self.config = resolved_config
        self.db_path = db_path or getattr(self.config, "db_path", "tracker.db")

        # Polling parameters
        self.poll_interval: int = int(getattr(self.config, "poll_interval", 120))
        self.grace_period_seconds: int = int(
            getattr(self.config, "grace_period_seconds", 300)
        )
        self.socket_timeout: float = float(
            getattr(self.config, "socket_timeout", 3.0)
        )
        self.topic_concurrency: int = int(
            getattr(self.config, "topic_concurrency", 30)
        )
        self.custom_servers: List[Dict[str, Any]] = list(
            getattr(self.config, "custom_servers", [])
        )
        self.hub_url: str = str(
            getattr(
                self.config,
                "hub_url",
                "http://www.byond.com/games/SpaceStation13?format=json",
            )
        )
        self.hub_text_url: str = str(
            getattr(
                self.config,
                "hub_text_url",
                "http://www.byond.com/games/Exadv1/SpaceStation13?format=text",
            )
        )

        self.semaphore = asyncio.Semaphore(self.topic_concurrency)

        # Operational metrics and lifecycle state
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._start_time: Optional[float] = None
        self._poll_count: int = 0
        self._errors_count: int = 0
        self._last_poll_time: Optional[float] = None
        self._last_poll_duration: float = 0.0
        self._servers_tracked: int = 0
        self._tracked_players: int = 0
        self._last_prune_time: Optional[float] = None

    def is_running(self) -> bool:
        """Checks if the background polling loop is actively running."""
        return self._running and self._task is not None and not self._task.done()

    def get_stats(self) -> Dict[str, Any]:
        """Returns diagnostic metrics and operational health of the tracker."""
        now = time.time()
        uptime = (
            round(now - self._start_time, 1)
            if (self._start_time and self._running)
            else 0.0
        )
        return {
            "status": "ok",
            "running": self.is_running(),
            "is_running": self.is_running(),
            "uptime_seconds": uptime,
            "poll_count": self._poll_count,
            "servers_tracked": self._servers_tracked,
            "tracked_servers": self._servers_tracked,
            "tracked_players": self._tracked_players,
            "last_poll_time": self._last_poll_time,
            "last_poll_duration": round(self._last_poll_duration, 3),
            "errors": self._errors_count,
            "errors_count": self._errors_count,
            "poll_interval": self.poll_interval,
            "grace_period_seconds": self.grace_period_seconds,
        }

    async def poll_once(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        """
        Executes a single polling tick:
        1. Scrapes active servers from BYOND Hub and merges custom servers.
        2. Upserts discovered servers into SQLite.
        3. Concurrently queries each server's BYOND Topic (?status) with semaphore throttling.
        4. Extracts and normalizes online ckeys.
        5. Updates player session lifecycles applying the 5-minute grace period state machine.
        """
        ts = time.time() if now_ts is None else float(now_ts)
        self._last_poll_time = ts
        start_time = time.perf_counter()

        logger.debug("Executing Tracker poll tick at ts=%s", ts)

        # Step 1: Discover servers
        discovered_servers: List[ServerInfo] = []
        try:
            async with httpx.AsyncClient(timeout=10.0) as http_client:
                discovered_servers = await fetch_hub_servers(
                    client=http_client,
                    custom_servers=self.custom_servers,
                    hub_url=self.hub_url,
                    fallback_url=self.hub_text_url,
                )
        except Exception as hub_err:
            self._errors_count += 1
            logger.error("Failed to discover servers from hub: %s", hub_err)

        self._servers_tracked = len(discovered_servers)
        total_online_seen: Set[str] = set()

        # Step 2: Query individual servers concurrently
        async def _process_server(srv: ServerInfo) -> int:
            nonlocal total_online_seen
            server_ckeys: Set[str] = set()
            topic_players_count: Optional[int] = None
            map_name: Optional[str] = None
            round_duration: Optional[int] = None
            gamemode: Optional[str] = None
            admins_count: Optional[int] = None
            version: Optional[str] = None
            active_players: Optional[int] = None
            station_time: Optional[str] = None
            enter_allowed: Optional[bool] = None
            respawn_allowed: Optional[bool] = None
            admin_list: Optional[List[Dict[str, str]]] = None

            # Pre-populate with ckeys parsed directly from hub (if present)
            if srv.players:
                for p in srv.players:
                    c = normalize_ckey(str(p))
                    if c:
                        server_ckeys.add(c)

            # Query Topic binary protocol if IP and port are valid
            if srv.ip and srv.port > 0:
                try:
                    topic_data = await query_topic(
                        ip=srv.ip,
                        port=srv.port,
                        query="?status=2",
                        timeout=self.socket_timeout,
                        semaphore=self.semaphore,
                    )
                    if isinstance(topic_data, dict):
                        plist = (
                            topic_data.get("playerlist")
                            or topic_data.get("players")
                            or topic_data.get("list")
                        )
                        if isinstance(plist, list):
                            for p in plist:
                                c = normalize_ckey(str(p))
                                if c:
                                    server_ckeys.add(c)
                        elif isinstance(plist, str):
                            for p in plist.replace("&", ",").split(","):
                                c = normalize_ckey(p)
                                if c:
                                    server_ckeys.add(c)

                        # Also scan any player0..playerN keys if present directly in dict
                        for k, v in topic_data.items():
                            k_str = str(k).lower()
                            if k_str.startswith("player") and k_str[6:].isdigit() and v:
                                c = normalize_ckey(str(v))
                                if c:
                                    server_ckeys.add(c)

                        # Parse numeric player count if provided
                        raw_pcount = topic_data.get("players")
                        if raw_pcount is None or isinstance(raw_pcount, list):
                            raw_pcount = topic_data.get("player_count")
                        if isinstance(raw_pcount, (int, float)):
                            topic_players_count = max(0, int(raw_pcount))
                        elif isinstance(raw_pcount, str) and raw_pcount.strip().isdigit():
                            topic_players_count = int(raw_pcount.strip())

                        # Parse active_players (living players in round on Baystation / SierraBay)
                        raw_active = topic_data.get("active_players")
                        if raw_active is not None:
                            try:
                                active_players = max(0, int(float(str(raw_active).strip())))
                            except (ValueError, TypeError):
                                pass

                        # Parse stationtime / station_time
                        raw_stime = (
                            topic_data.get("stationtime")
                            or topic_data.get("station_time")
                        )
                        if raw_stime is not None:
                            stime_str = str(raw_stime).strip()
                            if stime_str:
                                station_time = stime_str

                        # Parse enter / respawn flags
                        raw_enter = topic_data.get("enter")
                        if raw_enter is None:
                            raw_enter = topic_data.get("enter_allowed")
                        if raw_enter is not None:
                            enter_allowed = str(raw_enter).strip().lower() not in ("0", "false", "no", "")

                        raw_respawn = topic_data.get("respawn")
                        if raw_respawn is None:
                            raw_respawn = topic_data.get("respawn_allowed")
                        if raw_respawn is not None:
                            respawn_allowed = str(raw_respawn).strip().lower() not in ("0", "false", "no", "")

                        # Parse map_name
                        raw_map = topic_data.get("map_name") or topic_data.get("map")
                        if raw_map is not None:
                            map_str = str(raw_map).strip()
                            if map_str:
                                map_name = map_str

                        # Parse round_duration / roundduration / round_time (supports seconds or "HH:MM" / "HH:MM:SS")
                        raw_dur = None
                        for dur_key in ("round_duration", "roundduration", "round_time", "roundtime"):
                            if topic_data.get(dur_key) is not None:
                                raw_dur = topic_data.get(dur_key)
                                break
                        if raw_dur is not None:
                            parsed_dur = parse_round_duration(raw_dur)
                            round_duration = parsed_dur if parsed_dur is not None else 0

                        # Parse gamemode / mode
                        raw_mode = (
                            topic_data.get("gamemode")
                            or topic_data.get("mode")
                            or topic_data.get("game_mode")
                        )
                        if raw_mode is not None:
                            mode_str = str(raw_mode).strip()
                            if mode_str:
                                gamemode = mode_str

                        # Parse adminlist (Baystation / SierraBay ?status=2)
                        raw_adminlist = topic_data.get("adminlist")
                        if raw_adminlist is not None:
                            parsed_admins = parse_adminlist_value(raw_adminlist)
                            admin_list = parsed_admins

                        # Parse admins count
                        raw_admins = (
                            topic_data.get("admins")
                            if topic_data.get("admins") is not None
                            else topic_data.get("admins_count")
                        )
                        if raw_admins is not None:
                            if isinstance(raw_admins, list):
                                admins_count = len(raw_admins)
                            else:
                                try:
                                    admins_count = max(0, int(float(str(raw_admins).strip())))
                                except (ValueError, TypeError):
                                    admins_count = len(admin_list) if admin_list else 0
                        elif admin_list is not None:
                            admins_count = len(admin_list)

                        # Parse version
                        raw_ver = topic_data.get("version")
                        if raw_ver is not None:
                            ver_str = str(raw_ver).strip()
                            if ver_str:
                                version = ver_str
                except Exception as top_err:
                    logger.debug(
                        "Topic query error for server %s: %s",
                        srv.identifier,
                        top_err,
                    )

            effective_players = max(
                len(server_ckeys),
                topic_players_count if topic_players_count is not None else 0,
                int(srv.player_count or 0),
            )

            try:
                # Upsert server record with round metadata
                server_id = await upsert_server(
                    identifier=srv.identifier,
                    title=srv.title,
                    ip=srv.ip,
                    port=srv.port,
                    is_active=True,
                    now_ts=ts,
                    current_players=effective_players,
                    map_name=map_name,
                    round_duration=round_duration if round_duration is not None else 0,
                    gamemode=gamemode,
                    admins_count=admins_count if admins_count is not None else 0,
                    version=version,
                    active_players=active_players,
                    station_time=station_time,
                    enter_allowed=enter_allowed,
                    respawn_allowed=respawn_allowed,
                    admin_list=admin_list,
                )

                # Record online history snapshot
                await record_server_snapshot(
                    server_id=server_id,
                    player_count=effective_players,
                    round_duration=round_duration if round_duration is not None else 0,
                    map_name=map_name,
                    ts=ts,
                )

                # Update session states
                await update_player_sessions(
                    server_id=server_id,
                    online_ckeys=server_ckeys,
                    now_ts=ts,
                    grace_period_seconds=self.grace_period_seconds,
                )
            except Exception as db_err:
                logger.error(
                    "Database error updating server %s: %s",
                    srv.identifier,
                    db_err,
                )
                return 0

            total_online_seen.update(server_ckeys)
            return len(server_ckeys)

        if discovered_servers:
            tasks = [_process_server(s) for s in discovered_servers]
            await asyncio.gather(*tasks, return_exceptions=True)

        # Prune snapshots older than 7 days once per 24h
        if self._last_prune_time is None or (ts - self._last_prune_time) >= 86400.0:
            try:
                await prune_server_snapshots(max_age_days=7, now_ts=ts)
                self._last_prune_time = ts
            except Exception as prune_err:
                logger.debug("Error pruning old server snapshots: %s", prune_err)

        self._tracked_players = len(total_online_seen)
        self._poll_count += 1
        self._last_poll_duration = time.perf_counter() - start_time

        logger.debug(
            "Poll tick completed in %.3fs: %d servers, %d players online",
            self._last_poll_duration,
            self._servers_tracked,
            self._tracked_players,
        )

        return self.get_stats()

    async def run(self) -> None:
        """Continuous execution loop running until stopped or cancelled."""
        logger.info(
            "Tracker background loop started. Polling every %ds (grace=%ds).",
            self.poll_interval,
            self.grace_period_seconds,
        )
        while self._running and not self._stop_event.is_set():
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._errors_count += 1
                logger.error("Error during tracker polling tick: %s", exc, exc_info=True)

            try:
                # Sleep for poll_interval or wake immediately on stop_event
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=max(1, self.poll_interval)
                )
                break
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

        logger.info("Tracker background loop terminated.")

    def start(self) -> Any:
        """
        Starts the background worker. Can be called synchronously or awaited.
        """
        if self._running and self._task is not None and not self._task.done():
            return _StartAwaitable(self._task)

        self._running = True
        self._stop_event.clear()
        self._start_time = time.time()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            self._task = loop.create_task(self.run(), name="tracker_polling_loop")
        else:
            self._task = None

        return _StartAwaitable(self._task)

    def stop(self) -> Any:
        """
        Stops the background worker. Can be called synchronously or awaited.
        """
        self._running = False
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()

        async def _async_stop():
            if self._task is not None:
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
                self._task = None

        return _StopAwaitable(_async_stop())


_GLOBAL_TRACKER: Optional[Tracker] = None


def get_tracker(config: Optional[Any] = None) -> Tracker:
    """Returns or creates the global Tracker singleton."""
    global _GLOBAL_TRACKER
    if _GLOBAL_TRACKER is None:
        _GLOBAL_TRACKER = Tracker(config=config)
    return _GLOBAL_TRACKER
