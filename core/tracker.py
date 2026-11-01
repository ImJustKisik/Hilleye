"""Background Polling Worker and Session Tracking Engine for CyberSyn Intel.

Continuously discovers servers from the BYOND Hub and custom server registry,
queries each server concurrently over the BYOND Topic binary protocol (\x00\x83),
normalizes player ckeys, and drives the 5-minute (300s) grace period session state machine.
"""

import asyncio
import logging
from pathlib import Path
import shutil
import time
from typing import Any, Dict, List, Optional, Set, Union

import httpx

from core.byond_topic import (
    bounded_int,
    extract_player_list,
    public_topic_ip,
    parse_adminlist_value,
    parse_manifest_response,
    parse_round_duration,
    query_topic,
)
from core import db as core_db
from core.collector_lock import CollectorLock
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
        self._last_success_monotonic: Optional[float] = None
        self._cycle_healthy = False
        self._poll_lock = asyncio.Lock()
        self._clock_wall = time.time()
        self._clock_mono = time.monotonic()
        self._clock_trusted = True
        self._leader_lock: Optional[CollectorLock] = None
        self.is_leader = False
        self._degraded_reasons: List[str] = []

    def is_running(self) -> bool:
        """Checks if the background polling loop is actively running."""
        return self._running and self._task is not None and not self._task.done()

    def get_stats(self) -> Dict[str, Any]:
        """Returns diagnostic metrics and operational health of the tracker."""
        now = time.monotonic()
        uptime = (
            round(now - self._start_time, 1)
            if (self._start_time and self._running)
            else 0.0
        )
        return {
            "status": "ok" if self._cycle_healthy and self.is_running() else "degraded",
            "leader": self.is_leader,
            "degraded_reasons": self._degraded_reasons,
            "clock_trusted": self._clock_trusted,
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

    def _observation_time(self) -> float:
        mono = time.monotonic()
        logical = self._clock_wall + mono - self._clock_mono
        if abs(time.time() - logical) > 5:
            self._clock_trusted = False
        return logical

    async def poll_once(self, now_ts: Optional[float] = None) -> Dict[str, Any]:
        async with self._poll_lock:
            try:
                return await asyncio.wait_for(self._poll_once(now_ts), self.config.cycle_timeout_seconds)
            except BaseException:
                self._cycle_healthy = False
                raise

    async def _poll_once(self, now_ts: Optional[float]) -> Dict[str, Any]:
        started = time.monotonic()
        errors_before = self._errors_count
        self._degraded_reasons = []
        discovered_servers = []
        hub_healthy = False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                discovered_servers = await fetch_hub_servers(
                    client=client, custom_servers=self.custom_servers,
                    hub_url=self.hub_url, fallback_url=self.hub_text_url,
                )
            hub_healthy = getattr(discovered_servers, "healthy", True)
            self._errors_count += getattr(discovered_servers, "errors", 0)
            if not hub_healthy:
                self._degraded_reasons.append("Hub discovery unavailable or partial")
        except Exception:
            self._errors_count += 1
            self._degraded_reasons.append("Hub discovery failed")
            logger.exception("Hub discovery failed")
            # Trusted custom endpoints still get polled during a Hub outage.
            from core.hub_scraper import merge_custom_servers
            discovered_servers = merge_custom_servers([], self.custom_servers)

        self._servers_tracked = len(discovered_servers)
        total_online_seen: Set[str] = set()

        def trusted(srv):
            return srv.trusted or any(
                c.get("identifier") == srv.identifier or
                (c.get("ip") == srv.ip and c.get("port") == srv.port)
                for c in self.custom_servers
            )

        async def process_server(srv):
            roster = {normalize_ckey(p) for p in srv.players if normalize_ckey(p)}
            roster_known = srv.roster_known or bool(srv.players)
            roster_complete = srv.roster_complete
            data = None
            if srv.ip and 0 < srv.port <= 65535:
                try:
                    ip = srv.ip if trusted(srv) else await asyncio.wait_for(public_topic_ip(srv.ip, srv.port), self.socket_timeout)
                    for query in ("?status=2", "?status"):
                        candidate = await query_topic(ip=ip, port=srv.port, query=query,
                                                      timeout=self.socket_timeout, semaphore=self.semaphore)
                        if isinstance(candidate, dict):
                            candidate = {str(k).lower(): v for k, v in candidate.items()}
                            roster_response = extract_player_list(candidate)
                            usable = roster_response is not None or any(
                                bounded_int(candidate.get(k)) is not None
                                for k in ("players", "player_count", "admins", "active_players")
                            ) or any(isinstance(candidate.get(k), str) and candidate[k].strip()
                                     for k in ("version", "map", "map_name", "mode", "gamemode")) or any(
                                parse_round_duration(candidate.get(k)) is not None
                                for k in ("round_duration", "roundduration")
                            )
                            if usable:
                                data = candidate
                                break
                    if data is None:
                        self._errors_count += 1
                except Exception:
                    self._errors_count += 1
                    logger.exception("Topic error for %s", srv.identifier)

            topic_received = data is not None
            # Failed custom probes do not update last_seen or fabricate snapshots.
            if not topic_received and not srv.from_hub:
                return
            ts = float(now_ts) if now_ts is not None else self._observation_time()
            metrics: Dict[str, Any] = {}
            if getattr(srv, "map_name", None):
                metrics["map_name"] = str(srv.map_name).strip()[:512]
            if getattr(srv, "gamemode", None):
                metrics["gamemode"] = str(srv.gamemode).strip()[:512]
            if getattr(srv, "round_duration", None) is not None:
                metrics["round_duration"] = bounded_int(srv.round_duration)
            player_count = bounded_int(srv.player_count) if srv.from_hub and srv.count_known else None
            if topic_received:
                topic_roster = extract_player_list(data)
                if topic_roster is not None:
                    roster = {normalize_ckey(p) for p in topic_roster if normalize_ckey(p)}
                    roster_known = True
                    roster_complete = getattr(topic_roster, "complete", True)
                    if not roster_complete:
                        self._errors_count += 1
                        logger.warning("Incomplete Topic roster for %s; invalid players ignored", srv.identifier)
                count = bounded_int(data.get("players"))
                if count is None:
                    count = bounded_int(data.get("player_count"))
                if count is not None:
                    player_count = count
                elif topic_roster is not None and roster_complete:
                    player_count = len(roster)
                for dest, keys in {
                    "map_name": ("map_name", "map"), "gamemode": ("gamemode", "mode", "game_mode"),
                    "station_time": ("stationtime", "station_time"), "version": ("version",),
                }.items():
                    value = next((data[k] for k in keys if isinstance(data.get(k), (str, int, float))), None)
                    if value is not None:
                        metrics[dest] = str(value).strip()[:512]
                for key in ("round_duration", "roundduration", "round_time", "roundtime"):
                    if key in data:
                        metrics["round_duration"] = parse_round_duration(data[key])
                        break
                metrics["active_players"] = bounded_int(data.get("active_players"))
                for dest, keys in {"enter_allowed": ("enter", "enter_allowed"),
                                   "respawn_allowed": ("respawn", "respawn_allowed")}.items():
                    value = next((data[k] for k in keys if k in data), None)
                    if str(value).strip().lower() in ("0", "1", "true", "false", "yes", "no"):
                        metrics[dest] = str(value).strip().lower() in ("1", "true", "yes")
                if "adminlist" in data:
                    metrics["admin_list"] = parse_adminlist_value(data["adminlist"])
                raw_admins = data.get("admins", data.get("admins_count"))
                admins = len(raw_admins) if isinstance(raw_admins, list) else bounded_int(raw_admins)
                if admins is None and "admin_list" in metrics:
                    admins = len(metrics["admin_list"])
                metrics["admins_count"] = admins

            if roster_known and player_count is None:
                player_count = len(roster)
            await core_db.save_observation(
                dict(identifier=srv.identifier, title=srv.title, ip=srv.ip, port=srv.port,
                     is_active=True, now_ts=ts, current_players=player_count, **metrics),
                roster if roster_known else None, self.grace_period_seconds, topic_received, roster_complete,
            )
            total_online_seen.update(roster)

        # A fixed worker set bounds task creation as well as socket concurrency.
        pending = iter(discovered_servers)
        async def worker():
            for srv in pending:
                try:
                    await process_server(srv)
                except Exception:
                    self._errors_count += 1
                    logger.exception("Failed processing server %s", srv.identifier)
        await asyncio.gather(*(worker() for _ in range(min(self.topic_concurrency, len(discovered_servers)))))

        ts = float(now_ts) if now_ts is not None else self._observation_time()
        await core_db.expire_stale_servers(ts, max(self.config.stale_server_seconds, self.grace_period_seconds))
        # Destructive retention only after a full day of stable process time.
        # Startup and clock changes cannot suddenly prune a historical database.
        mono = time.monotonic()
        if self._clock_trusted and mono - self._clock_mono >= 86400 and (
            self._last_prune_time is None or mono - self._last_prune_time >= 86400
        ):
            await prune_server_snapshots(max_age_days=7, now_ts=ts)
            self._last_prune_time = mono
        if not self._clock_trusted:
            self._degraded_reasons.append("System clock changed; retention paused")
        path = Path(core_db._current_db_path).resolve().parent
        if core_db._current_db_path != ":memory:" and shutil.disk_usage(path).free < self.config.min_free_disk_mb * 1024**2:
            self._degraded_reasons.append("Low free disk space")
        self._tracked_players = len(total_online_seen)
        self._poll_count += 1
        self._last_poll_time = ts
        self._last_poll_duration = mono - started
        self._cycle_healthy = hub_healthy and self._errors_count == errors_before and not self._degraded_reasons
        if self._errors_count > errors_before:
            self._degraded_reasons.append("One or more collection operations failed")
        if self._cycle_healthy:
            self._last_success_monotonic = mono
        await core_db.record_collector_cycle(ts, self._cycle_healthy)
        return self.get_stats()

    async def run(self) -> None:
        self._leader_lock = CollectorLock(core_db._current_db_path)
        try:
            while self._running and not self._stop_event.is_set():
                tick_started = time.monotonic()
                self.is_leader = self._leader_lock.acquire()
                if self.is_leader:
                    try:
                        await self.poll_once()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._cycle_healthy = False
                        self._errors_count += 1
                        logger.exception("Tracker cycle failed")
                delay = max(0.05, self.poll_interval - (time.monotonic() - tick_started)) if self.is_leader else 1.0
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:
            self._errors_count += 1
            logger.exception("Collector stopped unexpectedly")
            raise
        finally:
            self.is_leader = False
            self._leader_lock.release()
            self._running = False

    def start(self) -> Any:
        """
        Starts the background worker. Can be called synchronously or awaited.
        """
        if self._running and self._task is not None and not self._task.done():
            return _StartAwaitable(self._task)

        self._running = True
        self._stop_event.clear()
        self._start_time = time.monotonic()

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
