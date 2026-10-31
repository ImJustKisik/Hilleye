"""
Tier 4: FastAPI Application & REST API / HTMX Integration Tests
Covers health check, REST endpoints (/api/v1/servers, /api/v1/player/{ckey}),
HTMX search fragment rendering (/search), and direct player dossier page (/player/{ckey}).
"""

import pytest
import httpx

web_app = pytest.importorskip("web.app", reason="web.app not yet implemented")
core_db = pytest.importorskip("core.db", reason="core.db not yet implemented")


async def seed_player_data(ckey: str = "honkmaster", server_title: str = "Tau Ceti Classic") -> int:
    """Helper to seed a test server and active session into SQLite."""
    server_id = await core_db.upsert_server(
        identifier="byond://178.62.241.130:1337",
        title=server_title,
        ip="178.62.241.130",
        port=1337
    )
    await core_db.update_player_sessions(server_id, {ckey}, now_ts=1000.0)
    await core_db.update_player_sessions(server_id, {ckey}, now_ts=4600.0)
    return server_id


class TestHealthEndpoint:
    """Tests GET /health operational status."""

    @pytest.mark.asyncio
    async def test_health_check_returns_ok_and_tracker_status(self, async_client):
        response = await async_client.get("/health")
        assert response.status_code == 200

        data = response.json()
        assert data.get("status") == "ok"
        assert "tracker" in data
        assert isinstance(data["tracker"], dict)


class TestServersApi:
    """Tests GET /api/v1/servers listing active monitored servers."""

    @pytest.mark.asyncio
    async def test_get_servers_empty_or_populated(self, async_client):
        response = await async_client.get("/api/v1/servers")
        assert response.status_code == 200

        data = response.json()
        # Accept either a direct list or {"servers": [...]}
        servers = data if isinstance(data, list) else data.get("servers", [])
        assert isinstance(servers, list)

    @pytest.mark.asyncio
    async def test_get_servers_with_registered_server(self, async_client):
        await core_db.upsert_server("byond://127.0.0.1:2000", "Station Test", "127.0.0.1", 2000)

        response = await async_client.get("/api/v1/servers")
        assert response.status_code == 200

        data = response.json()
        servers = data if isinstance(data, list) else data.get("servers", [])
        assert any(s.get("identifier") == "byond://127.0.0.1:2000" or s.get("port") == 2000 for s in servers)


class TestPlayerJsonApi:
    """Tests GET /api/v1/player/{ckey} machine-readable metrics."""

    @pytest.mark.asyncio
    async def test_get_player_existing_returns_200_and_metrics(self, async_client):
        ckey = "honkmaster"
        await seed_player_data(ckey=ckey)

        response = await async_client.get(f"/api/v1/player/{ckey}")
        assert response.status_code == 200

        data = response.json()
        assert data.get("ckey") == ckey
        # Check summary metrics
        summary = data.get("summary", data)
        assert "total_playtime_seconds" in summary or "total_hours" in summary
        # Check servers list
        assert "servers" in data or "server_breakdown" in data
        # Check recent sessions
        assert "recent_sessions" in data

    @pytest.mark.asyncio
    async def test_get_player_unknown_returns_404(self, async_client):
        response = await async_client.get("/api/v1/player/unknown_player_xyz_404")
        assert response.status_code == 404
        data = response.json()
        assert "detail" in data or "error" in data


class TestHtmxSearchEndpoint:
    """Tests POST /search HTMX partial rendering."""

    @pytest.mark.asyncio
    async def test_search_existing_player_renders_dossier_fragment(self, async_client):
        ckey = "honkmaster"
        await seed_player_data(ckey=ckey, server_title="Syndicate Outpost")

        response = await async_client.post("/search", data={"ckey": ckey})
        assert response.status_code == 200
        html = response.text

        # Should render dossier partial with player name and stats
        assert ckey in html.lower()
        # Should not return a 500 error or full HTML document if HTMX fragment
        assert "Syndicate" in html or "dossier" in html.lower() or "target" in html.lower()

    @pytest.mark.asyncio
    async def test_search_unknown_player_renders_not_found_partial(self, async_client):
        response = await async_client.post("/search", data={"ckey": "ghost_target_999"})
        assert response.status_code == 200  # HTMX partial renders 200, never 500
        html = response.text

        # Must display tactical not-found message
        assert "NO RECORD FOUND" in html or "NOT DETECTED" in html.upper()

    @pytest.mark.asyncio
    async def test_search_empty_or_special_chars_does_not_crash(self, async_client):
        for bad_input in ["", "   ", "___", "!@#$"]:
            response = await async_client.post("/search", data={"ckey": bad_input})
            # Must return 200 (not-found fragment) or 400, never 500
            assert response.status_code in (200, 400)
            if response.status_code == 200:
                assert "NO RECORD FOUND" in response.text or "NOT DETECTED" in response.text.upper()


class TestPlayerPageAndDashboardRoutes:
    """Tests GET / (dashboard) and GET /player/{ckey} (permalink page)."""

    @pytest.mark.asyncio
    async def test_root_dashboard_renders_search_terminal(self, async_client):
        response = await async_client.get("/")
        assert response.status_code == 200
        html = response.text

        # Must include tactical title and search bar
        assert "CYBERSYN" in html.upper() or "INTEL" in html.upper() or "DOSSIER" in html.upper()
        assert "<form" in html or "hx-post" in html or "search" in html.lower()

    @pytest.mark.asyncio
    async def test_player_page_existing_target(self, async_client):
        ckey = "agent_smith"
        await seed_player_data(ckey=ckey)

        response = await async_client.get(f"/player/{ckey}")
        assert response.status_code == 200
        html = response.text

        assert "agent_smith" in html.lower() or "agentsmith" in html.lower()

    @pytest.mark.asyncio
    async def test_player_page_unknown_target_renders_not_found(self, async_client):
        response = await async_client.get("/player/unknown_infiltrator_007")
        assert response.status_code == 200
        html = response.text

        assert "NO RECORD FOUND" in html or "NOT DETECTED" in html.upper()
