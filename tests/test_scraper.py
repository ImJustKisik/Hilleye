"""Unit tests for BYOND Hub scraper and custom server merging."""

import httpx
import pytest
from core.hub_scraper import (
    USER_AGENT,
    ServerInfo,
    clean_server_title,
    fetch_hub_servers,
    merge_custom_servers,
    parse_hub_json,
    parse_hub_text,
)


class TestTitleCleaning:
    def test_clean_server_title_with_bold_tags(self):
        raw = "<b>[RU] Tau Ceti Classic</b> - Round in progress"
        assert clean_server_title(raw) == "[RU] Tau Ceti Classic"

    def test_clean_server_title_with_html_entities(self):
        raw = "<b>Goonstation 1 &amp; 2 &#8212; Classic</b>"
        assert clean_server_title(raw) == "Goonstation 1 & 2 — Classic"

    def test_clean_server_title_with_byond_escapes(self):
        raw = "<b>\\[EN\\] Space Station 13</b>"
        assert clean_server_title(raw) == "[EN] Space Station 13"

    def test_clean_server_title_empty(self):
        assert clean_server_title("") == "Unknown Station"
        assert clean_server_title("   ") == "Unknown Station"


class TestHubTextParsing:
    SAMPLE_TEXT = """general
\ttype = "game"
\ttitle = "Space Station 13"
world/1
\turl = "byond://178.62.241.130:1337"
\tserver_version = 516
\tstatus = "<b>Tau Ceti Classic</b>"
\tplayers = list("HonkMaster","Clown","Mime")
world/2
\turl = "byond://192.168.1.100:25000"
\tstatus = "<b>[RU] White Dream</b>"
\tplayers = 42
world/3
\turl = "byond://BYOND.world.9999"
\tstatus = "<b>Pager Station</b>"
"""

    def test_parse_hub_text_extracts_all_worlds(self):
        servers = parse_hub_text(self.SAMPLE_TEXT)
        assert len(servers) == 3

        s1 = servers[0]
        assert s1.identifier == "byond://178.62.241.130:1337"
        assert s1.title == "Tau Ceti Classic"
        assert s1.ip == "178.62.241.130"
        assert s1.port == 1337
        assert s1.players == ["HonkMaster", "Clown", "Mime"]
        assert s1.player_count == 3

        s2 = servers[1]
        assert s2.identifier == "byond://192.168.1.100:25000"
        assert s2.title == "[RU] White Dream"
        assert s2.ip == "192.168.1.100"
        assert s2.port == 25000
        assert s2.players == []
        assert s2.player_count == 42

        s3 = servers[2]
        assert s3.identifier == "byond://BYOND.world.9999"
        assert s3.title == "Pager Station"
        assert s3.port == 0

    def test_parse_hub_text_empty(self):
        assert parse_hub_text("") == []


class TestHubJsonParsing:
    def test_parse_hub_json_list(self):
        data = [
            {
                "url": "byond://10.0.0.1:1234",
                "status": "<b>TG Manuel</b>",
                "players": ["Alice", "Bob"],
            },
            {
                "url": "byond://10.0.0.2:5678",
                "status": "<b>TG Terry</b>",
                "players": 18,
            },
        ]
        servers = parse_hub_json(data)
        assert len(servers) == 2
        assert servers[0].identifier == "byond://10.0.0.1:1234"
        assert servers[0].title == "TG Manuel"
        assert servers[0].players == ["Alice", "Bob"]
        assert servers[0].player_count == 2

        assert servers[1].identifier == "byond://10.0.0.2:5678"
        assert servers[1].player_count == 18

    def test_parse_hub_json_dict_format(self):
        data = {
            "world/1": {
                "url": "byond://1.1.1.1:2000",
                "title": "Station Alpha",
                "player_count": 5,
            }
        }
        servers = parse_hub_json(data)
        assert len(servers) == 1
        assert servers[0].identifier == "byond://1.1.1.1:2000"
        assert servers[0].title == "Station Alpha"
        assert servers[0].player_count == 5


class TestCustomServersMerging:
    def test_merge_custom_servers_appends_new(self):
        hub_servers = [
            ServerInfo(
                identifier="byond://1.1.1.1:1000",
                title="Hub Server 1",
                ip="1.1.1.1",
                port=1000,
            )
        ]
        custom = [
            {
                "identifier": "byond://2.2.2.2:2000",
                "title": "Secret Ops Base",
                "ip": "2.2.2.2",
                "port": 2000,
            }
        ]
        merged = merge_custom_servers(hub_servers, custom)
        assert len(merged) == 2
        custom_srv = next(s for s in merged if s.identifier == "byond://2.2.2.2:2000")
        assert custom_srv.title == "Secret Ops Base"

    def test_merge_custom_servers_deduplicates_and_overrides(self):
        hub_servers = [
            ServerInfo(
                identifier="byond://1.1.1.1:1000",
                title="Original Hub Title",
                ip="1.1.1.1",
                port=1000,
                players=["Player1"],
                player_count=1,
            )
        ]
        custom = [
            {
                "identifier": "byond://1.1.1.1:1000",
                "title": "Custom Overridden Title",
                "ip": "1.1.1.1",
                "port": 1000,
            }
        ]
        merged = merge_custom_servers(hub_servers, custom)
        assert len(merged) == 1
        assert merged[0].title == "Custom Overridden Title"
        # Preserves live players discovered from hub
        assert merged[0].players == ["Player1"]
        assert merged[0].player_count == 1


@pytest.mark.asyncio
class TestFetchHubServers:
    async def test_fetch_hub_servers_json_success(self):
        json_data = [
            {
                "url": "byond://178.62.241.130:1337",
                "status": "<b>Tau Ceti</b>",
                "players": ["Fox"],
            }
        ]

        def handler(request: httpx.Request):
            assert request.headers["user-agent"] == USER_AGENT
            return httpx.Response(200, json=json_data)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client)
            assert len(servers) == 1
            assert servers[0].identifier == "byond://178.62.241.130:1337"
            assert servers[0].title == "Tau Ceti"
            assert servers[0].players == ["Fox"]

    async def test_fetch_hub_servers_fallback_to_text_on_html(self):
        def handler(request: httpx.Request):
            assert request.headers["user-agent"] == USER_AGENT
            if "format=json" in str(request.url):
                # Returns HTML redirect / forum page
                return httpx.Response(
                    200,
                    text="<!DOCTYPE html><html><body>Redirect to BYOND forums</body></html>",
                )
            elif "format=text" in str(request.url):
                text_payload = 'world/1\n\turl = "byond://1.2.3.4:5678"\n\tstatus = "<b>Fallback Station</b>"\n\tplayers = 10\n'
                return httpx.Response(200, text=text_payload)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client)
            assert len(servers) == 1
            assert servers[0].title == "Fallback Station"
            assert servers[0].player_count == 10

    async def test_fetch_hub_servers_fallback_to_text_on_json_error(self):
        def handler(request: httpx.Request):
            if "format=json" in str(request.url):
                return httpx.Response(500, text="Internal Server Error")
            elif "format=text" in str(request.url):
                text_payload = 'world/1\n\turl = "byond://5.5.5.5:5000"\n\tstatus = "<b>Rescue Station</b>"\n\tplayers = 2\n'
                return httpx.Response(200, text=text_payload)
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client)
            assert len(servers) == 1
            assert servers[0].title == "Rescue Station"

    async def test_fetch_hub_servers_with_custom_servers_on_total_network_failure(self):
        def handler(request: httpx.Request):
            raise httpx.ConnectError("Network is down")

        custom = [
            {
                "identifier": "byond://9.9.9.9:9999",
                "title": "Offline Survival Station",
                "ip": "9.9.9.9",
                "port": 9999,
            }
        ]

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client, custom_servers=custom)
            assert len(servers) == 1
            assert servers[0].identifier == "byond://9.9.9.9:9999"
            assert servers[0].title == "Offline Survival Station"

    async def test_fetch_hub_servers_with_conftest_fixture(self, mock_hub_transport):
        transport = mock_hub_transport()
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client)
            assert len(servers) == 2
            assert servers[0].title == "Tau Ceti Classic"
            assert "honkmaster" in servers[0].players
            assert servers[1].title == "White Dream 1"

