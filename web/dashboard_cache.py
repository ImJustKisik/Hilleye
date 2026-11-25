"""Keep expensive dashboard calculations ready between collector cycles."""

import asyncio
from dataclasses import dataclass
import logging
from time import monotonic
from typing import Any

from starlette.datastructures import State

from core import db


logger = logging.getLogger(__name__)

_FRESH_SECONDS = 120.0
_MAX_AGE_SECONDS = 180.0
_RETRY_SECONDS = 5.0
_DashboardKey = tuple[int, float, tuple[int, ...]]


@dataclass(frozen=True)
class DashboardInputs:
    servers: list[dict[str, Any]]
    collector_state: dict[str, Any] | None
    key: _DashboardKey | None


@dataclass(frozen=True)
class DashboardData:
    overview: dict[str, Any]
    sparklines: dict[int, dict[str, Any]]
    key: _DashboardKey | None
    loaded_at: float


@db._read_snapshot
async def read_dashboard_inputs() -> DashboardInputs:
    generation = db._read_pool_generation
    collector_state = await db.get_collector_state()
    servers = await db.get_active_servers()
    key = None
    if collector_state is not None:
        server_ids = tuple(sorted(server["id"] for server in servers))
        key = (generation, collector_state["completed_at"], server_ids)
    return DashboardInputs(servers, collector_state, key)


@db._read_snapshot
async def _load_dashboard_data() -> DashboardData:
    loaded_at = monotonic()
    inputs = await read_dashboard_inputs()
    overview = await db.get_network_overview()
    sparklines = await db.get_servers_sparklines_24h([server["id"] for server in inputs.servers])
    return DashboardData(overview, sparklines, inputs.key, loaded_at)


def _retrieve_refresh_exception(task: asyncio.Task[DashboardData]) -> None:
    # A refresh may finish after all of its callers have returned.
    if not task.cancelled():
        task.exception()


class DashboardCache:
    def __init__(self, loop: asyncio.AbstractEventLoop, generation: int):
        self.loop = loop
        self.generation = generation
        self._entry: DashboardData | None = None
        self._task: asyncio.Task[DashboardData] | None = None
        self._retry_at = 0.0

    async def _refresh(self) -> DashboardData:
        try:
            result = await _load_dashboard_data()
            if self.generation != db._read_pool_generation:
                raise RuntimeError("Tracker database changed during dashboard refresh")
        except Exception:
            self._retry_at = monotonic() + _RETRY_SECONDS
            logger.warning("Dashboard data refresh failed", exc_info=True)
            raise
        self._entry = result if result.key is not None else None
        self._retry_at = 0.0
        return result

    async def get(self, key: _DashboardKey | None) -> DashboardData:
        if self.generation != db._read_pool_generation:
            raise RuntimeError("Tracker database changed during dashboard request")
        now = monotonic()
        entry = self._entry if key is not None else None
        if entry is not None:
            age = now - entry.loaded_at
            if age < _FRESH_SECONDS and entry.key == key:
                return entry
            if age >= _MAX_AGE_SECONDS:
                entry = None

        task = self._task
        if task is None or (task.done() and now >= self._retry_at):
            task = self._task = asyncio.create_task(self._refresh())
            task.add_done_callback(_retrieve_refresh_exception)
        if entry is not None:
            return entry
        return await asyncio.shield(task)

    def cancel_refresh(self) -> asyncio.Task[DashboardData] | None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        return self._task


def get_dashboard_cache(state: State) -> DashboardCache:
    loop = asyncio.get_running_loop()
    generation = db._read_pool_generation
    cache = getattr(state, "dashboard_cache", None)
    if cache is None or cache.loop is not loop or cache.generation != generation:
        if cache is not None and cache.loop is loop:
            cache.cancel_refresh()
        cache = state.dashboard_cache = DashboardCache(loop, generation)
    return cache


async def close_dashboard_cache(state: State) -> None:
    cache = getattr(state, "dashboard_cache", None)
    state.dashboard_cache = None
    if cache is not None and cache.loop is asyncio.get_running_loop():
        task = cache.cancel_refresh()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
