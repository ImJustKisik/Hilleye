"""
Tier 5 / E2E Integration: Full Simulated End-to-End Scenario
Simulates the entire CyberSyn Intel system lifecycle:
1. Hub scraping and server discovery (with custom server merge).
2. Socket Topic querying and player discovery.
3. Database upserts and session state machine (continuous play, transient disconnect, grace period expiry).
4. Web layer verification via REST API (/health, /api/v1/servers, /api/v1/player/{ckey}).
5. HTMX dynamic search fragment rendering and full permalink page rendering.
"""

import asyncio
import pytest
import httpx

# Verify required modules are present
byond_topic = pytest.importorskip("core.byond_topic", reason="core.byond_topic not yet implemented")
hub_scraper = pytest.importorskip("core.hub_scraper", reason="core.hub_scraper not yet implemented")
core_db = pytest.importorskip("core.db", reason="core.db not yet implemented")
web_app = pytest.importorskip("web.app", reason="web.app not yet implemented")


@pytest.mark.asyncio
async def test_full_simulated_e2e_lifecycle(temp_db_path, mock_topic_server):
    """
    Executes a comprehensive end-to-end test verifying all system tiers
    working together in a realistic multi-tick operational scenario.
    """
    # Step 1: Initialize Database in WAL mode
    await core_db.init_db(temp_db_path)

    # Step 2: Configure Mock Topic Server on ephemeral port
    port = mock_topic_server.port
    server_identifier = f"byond://127.0.0.1:{port}"
    server_title = "Syndicate Black Ops Outpost"

    # Step 3: Server Discovery (Hub Scraper + Custom Server)
    custom_servers = [
        {
            "identifier": server_identifier,
            "title": server_title,
            "ip": "127.0.0.1",
            "port": port
        }
    ]

    # Mock Hub HTTP Transport returning 1 public server plus merging custom server
    hub_json = [
        {
            "url": "byond://178.62.241.130:1337",
            "status": "Tau Ceti Classic",
            "players": ["clown", "mime"]
        }
    ]

    def hub_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=hub_json)

    async with httpx.AsyncClient(transport=httpx.MockTransport(hub_handler)) as http_client:
        discovered_servers = await hub_scraper.fetch_hub_servers(http_client, custom_servers=custom_servers)

    assert len(discovered_servers) >= 2
    # Register our test server in the DB
    server_id = await core_db.upsert_server(server_identifier, server_title, "127.0.0.1", port)
    assert server_id > 0

    # Step 4: Tick 1 (T0 = 1000.0) — Player enters server
    # Configure mock Topic socket to report honkmaster and syndicate_agent
    mock_topic_server.set_string_response("status=1&players=2&playerlist=honkmaster,syndicate_agent")

    # Simulate topic query
    topic_res = await byond_topic.query_topic("127.0.0.1", port, "?status")
    assert topic_res is not None

    t0 = 1000.0
    active_players = {"honkmaster", "syndicateagent"}  # ckey-normalized
    await core_db.update_player_sessions(server_id, active_players, now_ts=t0, grace_period_seconds=300)

    # Step 5: Web API & UI Verification at T0
    from web.app import app
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Check /health
        health_resp = await client.get("/health")
        assert health_resp.status_code == 200
        health_data = health_resp.json()
        assert health_data.get("status") == "ok"

        # Check /api/v1/servers
        servers_resp = await client.get("/api/v1/servers")
        assert servers_resp.status_code == 200

        # Check /api/v1/player/honkmaster -> should be ONLINE
        p_resp = await client.get("/api/v1/player/honkmaster")
        assert p_resp.status_code == 200
        p_data = p_resp.json()
        assert p_data["ckey"] == "honkmaster"
        status_info = p_data.get("status", p_data)
        if isinstance(status_info, dict):
            assert status_info.get("online") is True or status_info.get("is_online") is True

        # Check POST /search -> returns HTMX fragment with online status
        search_resp = await client.post("/search", data={"ckey": "honkmaster"})
        assert search_resp.status_code == 200
        assert "honkmaster" in search_resp.text.lower()

    # Step 6: Tick 2 (T1 = 1120.0) — Player continues playing (+120s)
    t1 = 1120.0
    await core_db.update_player_sessions(server_id, active_players, now_ts=t1, grace_period_seconds=300)

    # Step 7: Tick 3 (T2 = 1240.0) — Transient disconnect (< 300s grace)
    t2 = 1240.0
    # honkmaster temporarily missing
    await core_db.update_player_sessions(server_id, {"syndicateagent"}, now_ts=t2, grace_period_seconds=300)

    dossier_grace = await core_db.get_player_dossier("honkmaster")
    assert dossier_grace is not None
    # Session must still be active / open
    assert len(dossier_grace.get("recent_sessions", [])) == 1

    # Step 8: Tick 4 (T3 = 1360.0) — Seamless Reconnection
    t3 = 1360.0
    await core_db.update_player_sessions(server_id, active_players, now_ts=t3, grace_period_seconds=300)

    # Step 9: Tick 5 (T4 = 1700.0) — Final disconnect >= 300s (Delta = 340s)
    t4 = 1700.0
    await core_db.update_player_sessions(server_id, set(), now_ts=t4, grace_period_seconds=300)

    # Step 10: Final Web Verification (Offline Dossier & Not Found Handling)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Target is now offline
        p_resp = await client.get("/api/v1/player/honkmaster")
        assert p_resp.status_code == 200
        p_data = p_resp.json()
        status_info = p_data.get("status", p_data)
        if isinstance(status_info, dict):
            assert status_info.get("online") is False or status_info.get("is_online") is False

        # Playtime duration must NOT count the 340s absence: 1360 - 1000 = 360 seconds
        summary = p_data.get("summary", p_data)
        assert summary.get("total_playtime_seconds") == 360

        # Direct dossier page permalink
        page_resp = await client.get("/player/honkmaster")
        assert page_resp.status_code == 200
        assert "honkmaster" in page_resp.text.lower()

        # Unknown target handling
        unknown_resp = await client.get("/api/v1/player/nonexistent_agent_999")
        assert unknown_resp.status_code == 404

        unknown_search = await client.post("/search", data={"ckey": "nonexistent_agent_999"})
        assert unknown_search.status_code == 200
        assert "NO RECORD FOUND" in unknown_search.text or "NOT DETECTED" in unknown_search.text.upper()

    # Step 11: Clean DB shutdown
    await core_db.close_db()
