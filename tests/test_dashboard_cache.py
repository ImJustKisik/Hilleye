import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from core import db
from web import dashboard_cache
from web.app import app


@pytest.fixture
async def dashboard(async_client, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(dashboard_cache, "monotonic", lambda: clock[0])
    now = time.time()
    server_id = await db.upsert_server(
        "byond://127.0.0.1:4100", "Cache Station", "127.0.0.1", 4100,
        current_players=2, now_ts=now - 60,
    )
    await db.update_player_sessions(server_id, {"oldpilot"}, now_ts=now - 60)
    await db.record_server_snapshot(server_id, 1, ts=now - 7200)
    await db.record_server_snapshot(server_id, 2, ts=now - 60)
    await db.record_collector_cycle(now - 60, healthy=True)
    try:
        yield async_client, server_id, now, clock
    finally:
        await dashboard_cache.close_dashboard_cache(app.state)


async def test_dashboard_serves_ready_data_during_a_shared_refresh(dashboard, monkeypatch):
    client, server_id, now, _ = dashboard
    started = asyncio.Event()
    finish = asyncio.Event()
    calculate_charts = db.get_servers_sparklines_24h

    async def delayed_charts(server_ids):
        started.set()
        await finish.wait()
        return await calculate_charts(server_ids)

    overview = AsyncMock(wraps=db.get_network_overview)
    charts = AsyncMock(side_effect=delayed_charts)
    monkeypatch.setattr(db, "get_network_overview", overview)
    monkeypatch.setattr(db, "get_servers_sparklines_24h", charts)

    first_request = asyncio.create_task(client.get("/"))
    await asyncio.wait_for(started.wait(), timeout=5)
    first_request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_request
    finish.set()
    responses = await asyncio.gather(*(client.get("/") for _ in range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert all("24h: 1&ndash;2 players" in response.text for response in responses)
    assert overview.await_count == charts.await_count == 1

    started.clear()
    finish.clear()
    await db.upsert_server(
        "byond://127.0.0.1:4100", "Cache Station", "127.0.0.1", 4100,
        current_players=9, now_ts=now,
    )
    await db.record_server_snapshot(server_id, 9, ts=now)
    await db.update_player_sessions(server_id, {"newpilot"}, now_ts=now)
    await db.record_collector_cycle(now, healthy=False)

    responses = await asyncio.wait_for(
        asyncio.gather(*(client.get("/") for _ in range(4))), timeout=5,
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    assert overview.await_count == charts.await_count == 2
    for response in responses:
        assert response.status_code == 200
        assert '<span class="header-stat-val mono-val">9</span>' in response.text
        assert "Degraded &middot; just now" in response.text
        assert "24h: 1&ndash;2 players" in response.text
        assert "/player/oldpilot" in response.text
        assert "/player/newpilot" not in response.text

    finish.set()
    await asyncio.wait_for(app.state.dashboard_cache._task, timeout=5)
    refreshed = await client.get("/")
    assert "24h: 1&ndash;9 players" in refreshed.text
    assert "/player/newpilot" in refreshed.text
    assert overview.await_count == charts.await_count == 2


@pytest.mark.parametrize("change", ["age", "servers"])
async def test_dashboard_refreshes_when_the_entry_ages_or_servers_change(dashboard, monkeypatch, change):
    client, server_id, now, clock = dashboard
    charts = AsyncMock(wraps=db.get_servers_sparklines_24h)
    monkeypatch.setattr(db, "get_servers_sparklines_24h", charts)
    assert (await client.get("/")).status_code == 200

    server_ids = [server_id]
    if change == "age":
        clock[0] += 120
    else:
        new_id = await db.upsert_server(
            "byond://127.0.0.1:4200", "New Station", "127.0.0.1", 4200, now_ts=now,
        )
        server_ids.append(new_id)
    response = await client.get("/")
    assert response.status_code == 200
    if change == "servers":
        assert "New Station" in response.text
    await asyncio.wait_for(app.state.dashboard_cache._task, timeout=5)
    assert charts.await_count == 2
    assert sorted(charts.await_args.args[0]) == server_ids


async def test_dashboard_refresh_retries_without_extending_the_last_good_entry(dashboard, monkeypatch):
    client, server_id, now, clock = dashboard
    assert "24h: 1&ndash;2 players" in (await client.get("/")).text
    await db.record_server_snapshot(server_id, 9, ts=now)
    await db.update_player_sessions(server_id, {"newpilot"}, now_ts=now)
    await db.record_collector_cycle(now, healthy=True)

    calculate_charts = db.get_servers_sparklines_24h
    charts = AsyncMock(side_effect=RuntimeError("chart read failed"))
    monkeypatch.setattr(db, "get_servers_sparklines_24h", charts)

    async def assert_stale_response():
        response = await client.get("/")
        assert response.status_code == 200
        assert "24h: 1&ndash;2 players" in response.text
        assert "/player/newpilot" not in response.text

    await assert_stale_response()
    with pytest.raises(RuntimeError, match="chart read failed"):
        await asyncio.wait_for(app.state.dashboard_cache._task, timeout=5)
    clock[0] += 4
    await assert_stale_response()
    assert charts.await_count == 1
    clock[0] += 1
    await assert_stale_response()
    with pytest.raises(RuntimeError, match="chart read failed"):
        await asyncio.wait_for(app.state.dashboard_cache._task, timeout=5)
    assert charts.await_count == 2

    clock[0] = 1180
    with pytest.raises(RuntimeError, match="chart read failed"):
        await client.get("/")
    assert charts.await_count == 3
    clock[0] += 5
    monkeypatch.setattr(db, "get_servers_sparklines_24h", calculate_charts)
    refreshed = await client.get("/")
    assert "24h: 1&ndash;9 players" in refreshed.text
    assert "/player/newpilot" in refreshed.text


async def test_dashboard_cache_isolated_from_a_reinitialized_database(dashboard, tmp_path):
    client, server_id, now, _ = dashboard
    assert "/player/oldpilot" in (await client.get("/")).text

    await db.init_db(str(tmp_path / "replacement.db"))
    replacement_id = await db.upsert_server(
        "byond://127.0.0.1:4100", "Replacement Station", "127.0.0.1", 4100,
        current_players=7, now_ts=now - 60,
    )
    assert replacement_id == server_id
    await db.update_player_sessions(replacement_id, {"replacementpilot"}, now_ts=now - 60)
    await db.record_collector_cycle(now - 60, healthy=True)
    response = await client.get("/")
    assert response.status_code == 200
    assert "Replacement Station" in response.text
    assert "/player/replacementpilot" in response.text
    assert "/player/oldpilot" not in response.text
