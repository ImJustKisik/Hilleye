"""
Adversarial Stress Test Suite for Hub Scraper and Config.
Modules under test: core/hub_scraper.py, core/config.py.

Empirically verifies resilience, boundary conditions, malformed JSON/text payloads,
HTML error pages, complex entities, line ending variations, missing fields,
and explicitly identifies architectural/logic defects under adversarial inputs.
"""

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, List
import httpx
import pytest

from core.config import AppConfig, get_config, load_config
from core.hub_scraper import (
    DEFAULT_HUB_URL,
    DEFAULT_TEXT_URL,
    USER_AGENT,
    ServerInfo,
    clean_server_title,
    fetch_hub_servers,
    merge_custom_servers,
    parse_hub_json,
    parse_hub_text,
    _parse_url_address,
)


# ============================================================================
# 1. Title Cleaning & HTML Entity Stress Tests
# ============================================================================

class TestAdversarialTitleCleaning:
    """Stress tests for clean_server_title with hostile and edge-case inputs."""

    def test_unicode_and_multilingual_titles(self):
        """Multilingual titles (Cyrillic, CJK, Arabic) and emojis survive intact."""
        raw_ru = "<b>[RU] Тау Кита — Сервер #1</b> - Раунд начался"
        assert clean_server_title(raw_ru) == "[RU] Тау Кита — Сервер #1"

        raw_cjk = "<b>【CN】太空站13号 — 欢迎光临</b>"
        assert clean_server_title(raw_cjk) == "【CN】太空站13号 — 欢迎光临"

        raw_emoji = "<b>🚀 NSS CyberSyn ★ [24/7] ☢</b>"
        assert clean_server_title(raw_emoji) == "🚀 NSS CyberSyn ★ [24/7] ☢"

    def test_complex_and_nested_html_entities(self):
        """Named, decimal, and hex HTML entities unescaping."""
        raw = "<b>Goonstation &amp; &quot;Classic&quot; &#8212; &mdash; &ndash; &copy; &#x27; &#x1F680;</b>"
        cleaned = clean_server_title(raw)
        assert '&amp;' not in cleaned
        assert '&quot;' not in cleaned
        assert '&mdash;' not in cleaned
        assert '—' in cleaned
        assert '🚀' in cleaned
        assert "Goonstation & \"Classic\" — — – © ' 🚀" == cleaned

    def test_malformed_and_incomplete_entities(self):
        """Broken entities like &unknown;, bare ampersand, &#, etc."""
        raw = "<b>Station &unknown; & &# &#x; &amp;amp; Title</b>"
        cleaned = clean_server_title(raw)
        assert "Station &unknown; & &# &#x; &amp; Title" == cleaned

    def test_nested_and_unclosed_html_tags(self):
        """Nested formatting, unclosed tags, and multiple bold sections."""
        # Nested tags
        assert clean_server_title("<b><i><u>Deeply Nested Station</u></i></b>") == "Deeply Nested Station"
        # Unclosed <b> tag
        assert clean_server_title("<b>Unclosed Bold Station Title") == "Unclosed Bold Station Title"
        # Multiple <b> tags: extracts the first bold section
        assert clean_server_title("<b>Primary Title</b> other text <b>Secondary Title</b>") == "Primary Title"

    def test_script_tags_and_html_comments(self):
        """Script tags and HTML comments are completely stripped without leaking markup."""
        raw = "<b>Station<!-- comment --> Name</b><script>alert(1)</script>"
        assert clean_server_title(raw) == "Station Name"

    def test_byond_backslash_escapes(self):
        """BYOND-specific backslash escapes: \\[ \\] \\\"."""
        raw = r"<b>\[EN\] Space Station 13 \"Secret\"</b>"
        assert clean_server_title(raw) == '[EN] Space Station 13 "Secret"'

    def test_whitespace_and_empty_edge_cases(self):
        """Empty, whitespace, and tags-only strings default to 'Unknown Station'."""
        assert clean_server_title("") == "Unknown Station"
        assert clean_server_title("   \t\r\n   ") == "Unknown Station"
        assert clean_server_title("<b>   </b>") == "Unknown Station"
        assert clean_server_title("<b><img src='x'></b>") == "Unknown Station"
        assert clean_server_title("&nbsp;&nbsp;") == "Unknown Station"

    def test_massive_status_string_no_catastrophic_backtracking(self):
        """Verifies regex handles 200KB status strings without ReDoS freeze."""
        huge_raw = "<b>" + "A" * 100_000 + "</b>" + "B" * 100_000
        cleaned = clean_server_title(huge_raw)
        assert len(cleaned) == 100_000
        assert cleaned.startswith("AAAA")

    def test_finding_b_tag_with_attributes_leaks_extra_text(self):
        """
        FINDING: clean_server_title uses re.search(r'<b>(.*?)</b>') which strictly
        requires <b> without attributes. If a server status uses styled bold tags like
        <b style="color:red">Station Name</b> - 50 Players, bold_match is None and the
        regex falls back to stripping tags from the ENTIRE status string, leaking
        round information and player count into the title.
        """
        raw = '<b style="color:red">Styled Station</b> - 50 players'
        cleaned = clean_server_title(raw)
        # Expected: extracts the bold title 'Styled Station'
        # Actual: returns 'Styled Station - 50 players'
        assert cleaned == "Styled Station", f"Expected 'Styled Station' but got '{cleaned}'"


# ============================================================================
# 2. Hub JSON Parsing Stress Tests
# ============================================================================

class TestAdversarialHubJsonParsing:
    """Stress tests for parse_hub_json under adversarial structures."""

    def test_empty_and_primitive_payloads(self):
        """Empty, None, or primitive JSON payloads must safely return an empty list."""
        assert parse_hub_json(None) == []
        assert parse_hub_json("") == []
        assert parse_hub_json([]) == []
        assert parse_hub_json({}) == []
        assert parse_hub_json("just a string") == []
        assert parse_hub_json(12345) == []
        assert parse_hub_json(True) == []
        assert parse_hub_json(False) == []

    def test_malformed_entries_in_list(self):
        """List containing nulls, integers, strings, empty dicts, or missing fields."""
        data = [
            None,
            123,
            "not a dict",
            [],
            {},
            {"url": ""},  # empty url
            {"url": "byond://1.1.1.1:1111", "status": "<b>Valid</b>", "players": ["clown"]},
        ]
        servers = parse_hub_json(data)
        assert len(servers) == 1
        assert servers[0].identifier == "byond://1.1.1.1:1111"
        assert servers[0].title == "Valid"
        assert servers[0].players == ["clown"]

    def test_dict_of_objects_format(self):
        """Supports BYOND hub returning dictionary of world records."""
        data = {
            "world1": {"url": "byond://1.1.1.1:1111", "status": "<b>Station 1</b>", "players": 10},
            "world2": {"url": "byond://2.2.2.2:2222", "title": "<b>Station 2</b>", "players": ["alice"]},
            "world_bad": "not a dict",
        }
        servers = parse_hub_json(data)
        assert len(servers) == 2
        urls = {s.identifier for s in servers}
        assert "byond://1.1.1.1:1111" in urls
        assert "byond://2.2.2.2:2222" in urls

    def test_players_field_data_types(self):
        """Tests players field as list, scalar integer, float, None, or string."""
        data = [
            # List of strings
            {"url": "byond://1.1.1.1:1111", "status": "S1", "players": ["a", "b", "c"]},
            # Scalar integer
            {"url": "byond://2.2.2.2:2222", "status": "S2", "players": 42},
            # Float count
            {"url": "byond://3.3.3.3:3333", "status": "S3", "players": 15.0},
            # Fallback to player_count field
            {"url": "byond://4.4.4.4:4444", "status": "S4", "player_count": 27},
            # Missing or None players
            {"url": "byond://5.5.5.5:5555", "status": "S5", "players": None},
            # Invalid string players
            {"url": "byond://6.6.6.6:6666", "status": "S6", "players": "invalid_string"},
        ]
        servers = parse_hub_json(data)
        assert len(servers) == 6
        assert servers[0].player_count == 3
        assert servers[0].players == ["a", "b", "c"]
        assert servers[1].player_count == 42
        assert servers[1].players == []
        assert servers[2].player_count == 15
        assert servers[3].player_count == 27
        assert servers[4].player_count == 0
        assert servers[5].player_count == 0

    def test_players_list_with_mixed_elements(self):
        """Tests that dirty player lists (empty strings, whitespace) are filtered."""
        data = [
            {
                "url": "byond://1.1.1.1:1111",
                "status": "S1",
                "players": ["Alice", "", "   ", "Bob"],
            }
        ]
        servers = parse_hub_json(data)
        assert servers[0].players == ["Alice", "Bob"]
        assert servers[0].player_count == 2

    def test_finding_null_url_parses_as_none_server(self):
        """
        FINDING: In parse_hub_json line 161:
            raw_url = str(entry.get("url", "")).strip()
        If entry has {"url": null}, entry.get("url", "") returns None, which str()
        converts to "None". "None".strip() is truthy, causing parse_hub_json to create
        a bogus ServerInfo with identifier='byond://None' and ip='None'.
        """
        data = [{"url": None, "status": "Ghost Station", "players": []}]
        servers = parse_hub_json(data)
        # Expected: Entry with url=None should be skipped (0 servers)
        # Actual: Produces ServerInfo(identifier='byond://None', ip='None', port=0)
        assert len(servers) == 0, f"Expected 0 servers for null url, but got {servers}"


# ============================================================================
# 3. Hub Text Parsing Stress Tests
# ============================================================================

class TestAdversarialHubTextParsing:
    """Stress tests for parse_hub_text with unconventional formats and line endings."""

    def test_empty_and_garbage_text(self):
        """Empty, None, or random non-hub text returns empty list."""
        assert parse_hub_text("") == []
        assert parse_hub_text(None) == []
        assert parse_hub_text("random string without world blocks") == []
        assert parse_hub_text("general\n\ttitle = 'Space Station 13'\n") == []

    def test_crlf_line_endings(self):
        """Verifies Windows CRLF (\\r\\n) is parsed without issues."""
        text = (
            "general\r\n"
            "\ttype = \"game\"\r\n"
            "world/1\r\n"
            "\turl = \"byond://1.1.1.1:1111\"\r\n"
            "\tstatus = \"<b>CRLF Station 1</b>\"\r\n"
            "\tplayers = list(\"Honk\", \"Clown\")\r\n"
            "world/2\r\n"
            "\turl = \"byond://2.2.2.2:2222\"\r\n"
            "\tstatus = \"<b>CRLF Station 2</b>\"\r\n"
            "\tplayers = 18\r\n"
        )
        servers = parse_hub_text(text)
        assert len(servers) == 2
        assert servers[0].identifier == "byond://1.1.1.1:1111"
        assert servers[0].title == "CRLF Station 1"
        assert servers[0].players == ["Honk", "Clown"]
        assert servers[0].player_count == 2
        assert servers[1].identifier == "byond://2.2.2.2:2222"
        assert servers[1].player_count == 18

    def test_mixed_line_endings_and_whitespace(self):
        """Verifies mixed LF, CRLF, and spaces between blocks."""
        text = (
            "world/1\n"
            "url = \"byond://1.1.1.1:1111\"\n"
            "status = \"<b>Station 1</b>\"\n"
            "\r\n\r\n"
            "world/2\r\n"
            "url = \"byond://2.2.2.2:2222\"\r\n"
            "status = \"<b>Station 2</b>\"\n"
        )
        servers = parse_hub_text(text)
        assert len(servers) == 2

    def test_missing_fields_in_text_blocks(self):
        """Blocks with missing url, missing status, or missing players."""
        text = (
            # Missing url -> should be skipped
            "world/no_url\n"
            "\tstatus = \"<b>No URL</b>\"\n"
            "\tplayers = 10\n"
            # Missing status -> defaults to 'Unknown Station'
            "world/no_status\n"
            "\turl = \"byond://1.1.1.1:1111\"\n"
            "\tplayers = 5\n"
            # Missing players -> defaults to empty list and 0 count
            "world/no_players\n"
            "\turl = \"byond://2.2.2.2:2222\"\n"
            "\tstatus = \"<b>No Players</b>\"\n"
        )
        servers = parse_hub_text(text)
        assert len(servers) == 2

        assert servers[0].identifier == "byond://1.1.1.1:1111"
        assert servers[0].title == "Unknown Station"
        assert servers[0].player_count == 5

        assert servers[1].identifier == "byond://2.2.2.2:2222"
        assert servers[1].title == "No Players"
        assert servers[1].player_count == 0
        assert servers[1].players == []

    def test_multiline_status_and_escaped_quotes(self):
        """Status strings spanning multiple lines and containing escaped quotes."""
        text = (
            'world/1\n'
            '\turl = "byond://1.1.1.1:1111"\n'
            '\tstatus = "<b>Complex Station</b> - \\"Secret\\" Mode\\nMap: BoxStation\\nAdmin: Yes"\n'
            '\tplayers = list("Player1")\n'
        )
        servers = parse_hub_text(text)
        assert len(servers) == 1
        assert servers[0].title == "Complex Station"

    def test_player_list_variations(self):
        """Tests players = list(), players = 0, players = list(""), multiline players."""
        text = (
            'world/empty_list\n'
            '\turl = "byond://1.1.1.1:1001"\n'
            '\tplayers = list()\n'
            'world/zero_count\n'
            '\turl = "byond://1.1.1.1:1002"\n'
            '\tplayers = 0\n'
            'world/multiline_players\n'
            '\turl = "byond://1.1.1.1:1003"\n'
            '\tplayers = list(\n'
            '\t\t"Alice",\n'
            '\t\t"Bob"\n'
            '\t)\n'
        )
        servers = parse_hub_text(text)
        assert len(servers) == 3
        assert servers[0].players == []
        assert servers[0].player_count == 0
        assert servers[1].players == []
        assert servers[1].player_count == 0
        assert servers[2].players == ["Alice", "Bob"]
        assert servers[2].player_count == 2


# ============================================================================
# 4. URL Parsing & Custom Servers Merging Stress Tests
# ============================================================================

class TestAdversarialCustomServersMerging:
    """Stress tests for _parse_url_address and merge_custom_servers."""

    def test_url_address_edge_cases(self):
        """Tests various formats in _parse_url_address."""
        # Standard with prefix
        ident, ip, port = _parse_url_address("byond://1.2.3.4:1337")
        assert ident == "byond://1.2.3.4:1337"
        assert ip == "1.2.3.4"
        assert port == 1337

        # Without prefix
        ident, ip, port = _parse_url_address("1.2.3.4:1337")
        assert ident == "byond://1.2.3.4:1337"
        assert ip == "1.2.3.4"
        assert port == 1337

        # Pager address
        ident, ip, port = _parse_url_address("byond://BYOND.world.12345")
        assert ident == "byond://BYOND.world.12345"
        assert ip == "BYOND.world.12345"
        assert port == 0

        # Hostname with port
        ident, ip, port = _parse_url_address("byond://game.tgstation13.org:25000")
        assert ident == "byond://game.tgstation13.org:25000"
        assert ip == "game.tgstation13.org"
        assert port == 25000

    def test_merge_custom_servers_empty_or_none(self):
        """Returns hub servers when custom_servers is empty or None."""
        hub = [ServerInfo("byond://1.1.1.1:1111", "Hub S1", "1.1.1.1", 1111)]
        assert len(merge_custom_servers(hub, None)) == 1
        assert len(merge_custom_servers(hub, [])) == 1

    def test_custom_server_overrides_hub_title_preserves_players(self):
        """Custom server overriding title must keep discovered players from hub."""
        hub = [
            ServerInfo(
                identifier="byond://1.1.1.1:1111",
                title="Hub Title",
                ip="1.1.1.1",
                port=1111,
                players=["Alice", "Bob"],
                player_count=2,
            )
        ]
        custom = [
            {
                "identifier": "byond://1.1.1.1:1111",
                "title": "Custom Overridden Title",
                "ip": "1.1.1.1",
                "port": 1111,
            }
        ]
        merged = merge_custom_servers(hub, custom)
        assert len(merged) == 1
        s = merged[0]
        assert s.identifier == "byond://1.1.1.1:1111"
        assert s.title == "Custom Overridden Title"
        assert s.players == ["Alice", "Bob"]
        assert s.player_count == 2

    def test_merge_custom_matches_by_endpoint_when_identifier_differs(self):
        """Custom server with different identifier alias matches hub by (ip, port)."""
        hub = [
            ServerInfo(
                identifier="byond://1.1.1.1:1111",
                title="Hub Title",
                ip="1.1.1.1",
                port=1111,
                players=["Clown"],
                player_count=1,
            )
        ]
        custom = [
            {
                "identifier": "byond://my-custom-alias",
                "title": "Alias Title",
                "ip": "1.1.1.1",
                "port": 1111,
            }
        ]
        merged = merge_custom_servers(hub, custom)
        assert len(merged) == 1
        assert merged[0].identifier == "byond://my-custom-alias"
        assert merged[0].title == "Alias Title"
        assert merged[0].players == ["Clown"]

    def test_finding_merge_skips_deduplication_when_custom_servers_empty(self):
        """
        FINDING: In core/hub_scraper.py line 205-206:
            if not custom_servers:
                return list(hub_servers)
        When custom_servers is empty/None, merge_custom_servers short-circuits and
        returns list(hub_servers) directly, skipping the deduplication logic completely.
        Any duplicate servers present in hub_servers remain duplicated in the returned list.
        """
        hub = [
            ServerInfo("byond://1.1.1.1:1111", "S1", "1.1.1.1", 1111),
            ServerInfo("byond://1.1.1.1:1111", "S1 Duplicate", "1.1.1.1", 1111),
        ]
        merged = merge_custom_servers(hub, [])
        # Expected: Deduplicated to 1 ServerInfo
        # Actual: Returns 2 ServerInfo entries
        assert len(merged) == 1, f"Expected 1 deduplicated server, got {len(merged)}"

    def test_finding_duplicate_custom_servers_not_deduplicated(self):
        """
        FINDING: In merge_custom_servers line 221-250, custom servers are looped over
        and immediately appended to merged WITHOUT checking seen_identifiers or
        seen_endpoints. If a user specifies duplicate custom servers in config.json,
        both are added to the returned list.
        """
        custom = [
            {"ip": "1.2.3.4", "port": 1337, "title": "Primary"},
            {"ip": "1.2.3.4", "port": 1337, "title": "Duplicate Copy"},
        ]
        merged = merge_custom_servers([], custom)
        # Expected: Deduplicated to 1 ServerInfo
        # Actual: Returns 2 ServerInfo entries with identical endpoints
        assert len(merged) == 1, f"Expected 1 deduplicated server, got {len(merged)}"

    def test_finding_merge_custom_servers_crashes_on_non_dict_elements(self):
        """
        FINDING: In merge_custom_servers line 222-223:
            for c in custom_servers:
                ip = str(c.get("ip", "")).strip()
        If custom_servers contains a non-dict element (e.g. [123] or ['bad']),
        c.get(...) raises AttributeError: 'int' object has no attribute 'get'.
        """
        res = merge_custom_servers([], [123, "bad_item"])
        assert res == []

    def test_finding_custom_server_identifier_only_lacks_ip_port(self):
        """
        FINDING: In merge_custom_servers lines 223-230 and AppConfig.validate_custom_servers:
        When a custom server is defined with {"identifier": "byond://1.2.3.4:5678"},
        neither function parses ip and port from identifier. ip remains "" and port remains 0,
        which prevents core/tracker.py from being able to query the server via TCP socket.
        """
        custom = [{"identifier": "byond://1.2.3.4:5678", "title": "Identifier Only"}]
        merged = merge_custom_servers([], custom)
        s = merged[0]
        # Expected: ip='1.2.3.4', port=5678
        # Actual: ip='', port=0
        assert s.ip == "1.2.3.4" and s.port == 5678, (
            f"Expected ip='1.2.3.4', port=5678, but got ip='{s.ip}', port={s.port}"
        )


# ============================================================================
# 5. Network Hub Scraper & Fallback Stress Tests
# ============================================================================

class TestAdversarialFetchHubServers:
    """Stress tests for fetch_hub_servers under network anomalies and mock servers."""

    @pytest.mark.asyncio
    async def test_json_hub_html_error_page_fallback(self):
        """When JSON hub returns HTML 502/500, falls back to text hub seamlessly."""
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "format=json" in url_str:
                return httpx.Response(
                    502,
                    text="<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head><body>Error</body></html>",
                )
            # Text fallback
            return httpx.Response(
                200,
                text='world/1\nurl = "byond://1.2.3.4:1234"\nstatus = "<b>Fallback Station</b>"\nplayers = 12\n',
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client)
            assert len(servers) == 1
            assert servers[0].identifier == "byond://1.2.3.4:1234"
            assert servers[0].title == "Fallback Station"
            assert servers[0].player_count == 12

    @pytest.mark.asyncio
    async def test_json_hub_html_with_leading_comment_fallback(self):
        """When JSON hub returns HTML preceded by a 300-char comment, fallback activates."""
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "format=json" in url_str:
                return httpx.Response(
                    200,
                    text="<!-- " + "x" * 300 + " -->\n<html><body>Maintenance</body></html>",
                )
            return httpx.Response(
                200,
                text='world/1\nurl = "byond://1.2.3.4:1234"\nstatus = "<b>Text Fallback</b>"\n',
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client)
            assert len(servers) == 1
            assert servers[0].title == "Text Fallback"

    @pytest.mark.asyncio
    async def test_json_hub_malformed_json_fallback(self):
        """Truncated or malformed JSON triggers text fallback."""
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "format=json" in url_str:
                return httpx.Response(200, text='[{"url": "byond://1.1.1.1:1111", "status": "Incom')
            return httpx.Response(
                200,
                text='world/1\nurl = "byond://1.2.3.4:1234"\nstatus = "<b>Text Fallback</b>"\n',
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client)
            assert len(servers) == 1
            assert servers[0].title == "Text Fallback"

    @pytest.mark.asyncio
    async def test_json_hub_timeout_fallback(self):
        """Timeout on JSON endpoint triggers text fallback."""
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "format=json" in url_str:
                raise httpx.ReadTimeout("JSON endpoint timed out")
            return httpx.Response(
                200,
                text='world/1\nurl = "byond://1.2.3.4:1234"\nstatus = "<b>Recovered From Timeout</b>"\n',
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client)
            assert len(servers) == 1
            assert servers[0].title == "Recovered From Timeout"

    @pytest.mark.asyncio
    async def test_both_endpoints_fail_graceful_custom_servers_return(self):
        """When both JSON and Text hubs fail completely, returns custom servers without crashing."""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("Network unreachable")

        custom = [{"ip": "10.0.0.1", "port": 1337, "title": "Emergency Backup"}]
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client, custom_servers=custom)
            assert len(servers) == 1
            assert servers[0].identifier == "byond://10.0.0.1:1337"
            assert servers[0].title == "Emergency Backup"

    @pytest.mark.asyncio
    async def test_custom_user_agent_header_sent(self):
        """Verifies that the required User-Agent 'CyberSynIntel/1.0 (BYOND)' is sent."""
        captured_headers = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers.update(request.headers)
            return httpx.Response(200, json=[])

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            await fetch_hub_servers(client)

        assert captured_headers.get("user-agent") == USER_AGENT

    @pytest.mark.asyncio
    async def test_finding_json_error_dict_fails_to_fallback_to_text(self):
        """
        FINDING: In fetch_hub_servers line 309-311:
            parsed_json = response.json()
            servers = parse_hub_json(parsed_json)
        If the JSON hub returns HTTP 200 with an error object (e.g. {"error": "Hub offline"}),
        response.json() succeeds without raising an exception, parse_hub_json returns [],
        and the function returns 0 servers without ever falling back to the text hub.
        """
        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            if "format=json" in url_str:
                return httpx.Response(200, json={"error": "Hub Maintenance in Progress"})
            # Fallback text hub has live servers
            return httpx.Response(
                200,
                text='world/1\nurl = "byond://1.2.3.4:1234"\nstatus = "<b>Fallback Live Station</b>"\n',
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            servers = await fetch_hub_servers(client)
            # Expected: Falls back to text hub and finds 1 server
            # Actual: Returns 0 servers because no exception was raised in try block
            assert len(servers) == 1, (
                f"Expected fallback to text hub (1 server) when JSON returns error dict, got {len(servers)}"
            )


# ============================================================================
# 6. Config Loading & Schema Validation Stress Tests
# ============================================================================

class TestAdversarialConfig:
    """Stress tests for load_config and AppConfig with corrupt, missing, or invalid files."""

    def test_missing_config_file_returns_defaults(self):
        """Non-existent config file returns valid AppConfig with default values."""
        cfg = load_config(Path("non_existent_config_file_12345.json"))
        assert cfg.poll_interval == 120
        assert cfg.poll_interval_seconds == 120
        assert cfg.grace_period_seconds == 300
        assert cfg.socket_timeout == 3.0
        assert cfg.topic_concurrency == 30
        assert len(cfg.custom_servers) > 0

    def test_empty_config_file_reports_error(self):
        """Empty existing config is rejected."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            f.write("")
            path = Path(f.name)
        try:
            with pytest.raises(ValueError, match="Invalid configuration"):
                load_config(path)
        finally:
            path.unlink()

    def test_malformed_json_config_file_reports_error(self):
        """Corrupted JSON file produces an actionable configuration error."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            f.write("{invalid: json, broken: [}")
            path = Path(f.name)
        try:
            with pytest.raises(ValueError, match="Invalid configuration"):
                load_config(path)
        finally:
            path.unlink()

    def test_root_is_not_dict_reports_error(self):
        """Config file root must be an object."""
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            f.write('["item1", "item2"]')
            path = Path(f.name)
        try:
            with pytest.raises(ValueError, match="Invalid configuration"):
                load_config(path)
        finally:
            path.unlink()

    def test_invalid_field_types_reports_error(self):
        """Invalid field types stop startup instead of discarding valid settings."""
        data = {
            "poll_interval": "not_an_int",
            "socket_timeout": "fast",
            "topic_concurrency": {"invalid": "dict"},
        }
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            with pytest.raises(ValueError, match="Invalid configuration"):
                load_config(path)
        finally:
            path.unlink()

    def test_alias_synchronization(self):
        """Tests that aliases synchronize bidirectionally."""
        # poll_interval -> poll_interval_seconds
        c1 = AppConfig(poll_interval=60)
        assert c1.poll_interval == 60
        assert c1.poll_interval_seconds == 60

        # poll_interval_seconds -> poll_interval
        c2 = AppConfig(poll_interval_seconds=45)
        assert c2.poll_interval == 45
        assert c2.poll_interval_seconds == 45

        # socket_timeout -> topic_timeout_seconds
        c3 = AppConfig(socket_timeout=5.5)
        assert c3.socket_timeout == 5.5
        assert c3.topic_timeout_seconds == 5.5

        # topic_semaphore_limit -> topic_concurrency
        c4 = AppConfig(topic_semaphore_limit=50)
        assert c4.topic_concurrency == 50
        assert c4.topic_semaphore_limit == 50

        # hub_text_url -> hub_fallback_url
        c5 = AppConfig(hub_text_url="http://custom.hub/text")
        assert c5.hub_text_url == "http://custom.hub/text"
        assert c5.hub_fallback_url == "http://custom.hub/text"

    def test_dictionary_style_access(self):
        """AppConfig supports both dict index access and .get()."""
        cfg = AppConfig(poll_interval=90)
        assert cfg["poll_interval"] == 90
        assert cfg.get("poll_interval") == 90
        assert cfg.get("non_existent_key", "default_val") == "default_val"
        with pytest.raises(KeyError):
            _ = cfg["non_existent_key"]

    def test_custom_servers_sanitization_in_config(self):
        """Sanitizes dirty custom_servers inputs in AppConfig."""
        # custom_servers not a list -> defaults
        c_bad_type = AppConfig(custom_servers="not_a_list")
        assert len(c_bad_type.custom_servers) > 0

        # list with non-dict elements filtered out
        c_dirty_list = AppConfig(custom_servers=[123, "bad", None, {"ip": "1.2.3.4", "port": 5000}])
        assert len(c_dirty_list.custom_servers) == 1
        assert c_dirty_list.custom_servers[0]["ip"] == "1.2.3.4"
        assert c_dirty_list.custom_servers[0]["port"] == 5000
        assert c_dirty_list.custom_servers[0]["identifier"] == "byond://1.2.3.4:5000"

    def test_utf8_config_file_loading(self):
        """Validates that UTF-8 Cyrillic and emojis in config.json load correctly."""
        data = {
            "custom_servers": [
                {
                    "ip": "127.0.0.1",
                    "port": 1337,
                    "title": "Тау Кита [Classic] 🚀",
                }
            ]
        }
        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            path = Path(f.name)
        try:
            cfg = load_config(path)
            assert cfg.custom_servers[0]["title"] == "Тау Кита [Classic] 🚀"
        finally:
            path.unlink()

    def test_finding_custom_server_null_keys_become_string_none(self):
        """
        FINDING: In core/config.py lines 65-73:
            title = str(s.get("title", f"Server {ip}:{port}")).strip()
            identifier = str(s.get("identifier", "")).strip()
        When a custom server dictionary has {"identifier": null, "title": null},
        s.get(...) returns None, which str() turns into "None".
        Because "None" is truthy, the default expression (f"Server {ip}:{port}")
        is bypassed, resulting in title="None" and identifier="None".
        """
        cfg = AppConfig(custom_servers=[{"ip": "1.2.3.4", "port": 1337, "identifier": None, "title": None}])
        s = cfg.custom_servers[0]
        # Expected: identifier='byond://1.2.3.4:1337', title='Server 1.2.3.4:1337'
        # Actual: identifier='None', title='None'
        assert s["identifier"] != "None", f"Expected normalized identifier, but got string 'None'"
        assert s["title"] != "None", f"Expected default server title, but got string 'None'"


# ============================================================================
# 7. URL Address Parsing Edge Cases
# ============================================================================

class TestAdversarialUrlParsing:
    """Stress tests for _parse_url_address."""

    def test_finding_ipv6_split_at_first_colon(self):
        """
        FINDING: In _parse_url_address line 74-76:
            ip, _, port_s = addr.partition(":")
        addr.partition(":") splits at the FIRST colon. For IPv6 addresses like
        byond://[2001:db8::1]:1234, this sets ip="[2001", port=0, and identifier="byond://[2001".
        """
        ident, ip, port = _parse_url_address("byond://[2001:db8::1]:1234")
        # Expected: ip='2001:db8::1' or '[2001:db8::1]', port=1234
        # Actual: ip='[2001', port=0
        assert port == 1234, f"Expected port 1234 on IPv6 address, but got {port} (ip='{ip}')"
