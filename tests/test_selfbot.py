"""
Unit & Integration Tests for Hilleye Selfbot

Covers:
- Mention defusal and Markdown escaping
- Ckey normalization and strict Discord snowflake ID extraction
- Multi-tier server fuzzy matching & tie-breaking
- Atomic JSON storage and corrupted file quarantine (Whitelist & Subscriptions)
- 24-hour anchored sparkline calculation
- Bounded TTL/LRU caching
- HilleyeAPIClient status code handling (HTTP 200, 404, 503 allow/reject)
- Unified resolve_server logic
- Bot configuration, cooldowns, and global allowed_mentions safety
"""

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
discord = pytest.importorskip("discord")
from discord.ext import commands

from examples.selfbot_utils import (
    NOT_FOUND,
    BoundedTTLCache,
    HilleyeAPIClient,
    SubscriptionManager,
    WhitelistManager,
    atomic_write_json,
    build_sparkline,
    defuse_mentions,
    escape_code_text,
    extract_discord_user_id,
    format_age,
    match_servers_by_query,
    normalize_ckey,
    safe_load_json,
)
from examples.selfbot_example import bot, is_owner


# ============================================================================
# Ckey Normalization & Snowflake Validation Tests
# ============================================================================

def test_normalize_ckey() -> None:
    assert normalize_ckey("Honk_Master-99") == "honkmaster99"
    assert normalize_ckey("  Admin_User_1  ") == "adminuser1"
    assert normalize_ckey("CKEY!@#$%^&*()") == "ckey"
    assert normalize_ckey("") == ""
    assert normalize_ckey("   ") == ""
    assert normalize_ckey("ПРИВЕТ") == ""  # Non-ascii stripped


def test_extract_discord_user_id_valid() -> None:
    # 18-digit typical snowflake
    assert extract_discord_user_id("123456789012345678") == 123456789012345678
    assert extract_discord_user_id("  123456789012345678  ") == 123456789012345678
    # User mention
    assert extract_discord_user_id("<@123456789012345678>") == 123456789012345678
    # Nickname mention
    assert extract_discord_user_id("<@!123456789012345678>") == 123456789012345678
    # Boundaries: 15 digits to 22 digits
    assert extract_discord_user_id("123456789012345") == 123456789012345
    assert extract_discord_user_id("1234567890123456789012") == 1234567890123456789012


def test_extract_discord_user_id_invalid() -> None:
    # Too short or too long
    assert extract_discord_user_id("12345678901234") is None
    assert extract_discord_user_id("12345678901234567890123") is None
    # Embedded in other text (strict validation must reject)
    assert extract_discord_user_id("user 123456789012345678") is None
    assert extract_discord_user_id("123456789012345678xyz") is None
    assert extract_discord_user_id("<@123456789012345678> extra") is None
    assert extract_discord_user_id("not-a-number") is None
    assert extract_discord_user_id("") is None
    assert extract_discord_user_id(None) is None


# ============================================================================
# Mention Defusal & Markdown Escaping Tests
# ============================================================================

def test_defuse_mentions() -> None:
    # Direct mention strings
    assert defuse_mentions("@everyone") == "@\u200beveryone"
    assert defuse_mentions("@here") == "@\u200bhere"
    assert defuse_mentions("<@&123456789>") == "<@\u200b&123456789>"

    # Nested data structures
    data = {
        "title": "Welcome @everyone to the server",
        "nested": ["@here", {"tag": "admin@role"}],
        "number": 42,
    }
    sanitized = defuse_mentions(data)
    assert sanitized["title"] == "Welcome @\u200beveryone to the server"
    assert sanitized["nested"][0] == "@\u200bhere"
    assert sanitized["nested"][1]["tag"] == "admin@\u200brole"
    assert sanitized["number"] == 42


def test_escape_code_text() -> None:
    # Backticks replaced, mentions defused
    assert escape_code_text("foo`bar`baz") == "foo'bar'baz"
    assert escape_code_text("`test` @everyone") == "'test' @\u200beveryone"
    assert escape_code_text(123) == "123"
    assert escape_code_text(None) == ""


def test_format_age() -> None:
    assert format_age(45) == "0m"
    assert format_age(120) == "2m"
    assert format_age(3660) == "1h 1m"
    assert format_age(90000) == "1d 1h"
    assert format_age(-10) == "0m"


# ============================================================================
# Server Search & Fuzzy Matching Tests
# ============================================================================

def test_match_servers_by_query() -> None:
    mock_servers = [
        {"id": 1, "title": "Goonstation 1 Classic", "identifier": "goon1", "map_name": "donut3", "current_players": 40},
        {"id": 2, "title": "Goonstation 2 Roleplay", "identifier": "goon2", "map_name": "cogmap", "current_players": 65},
        {"id": 12, "title": "SierraBay 220 Station", "identifier": "sierra", "map_name": "sierra_v2", "current_players": 15},
        {"id": 220, "title": "Paradise Station", "identifier": "paradise", "map_name": "cyberiad", "current_players": 80},
    ]

    # Tier 0: exact numeric ID
    res = match_servers_by_query(mock_servers, "12")
    assert len(res) == 1
    assert res[0]["id"] == 12

    res_hash = match_servers_by_query(mock_servers, "#12")
    assert len(res_hash) == 1
    assert res_hash[0]["id"] == 12

    # Tier 1: exact title match
    res_exact = match_servers_by_query(mock_servers, "Paradise Station")
    assert len(res_exact) == 1
    assert res_exact[0]["id"] == 220

    # Tier 2: title prefix match (higher player count Goon 2 before Goon 1)
    res_prefix = match_servers_by_query(mock_servers, "Goonstation")
    assert len(res_prefix) == 2
    assert res_prefix[0]["id"] == 2  # 65 players
    assert res_prefix[1]["id"] == 1  # 40 players

    # Tier 4/5: substring / multi-token match
    res_tokens = match_servers_by_query(mock_servers, "Sierra 220")
    assert len(res_tokens) == 1
    assert res_tokens[0]["id"] == 12

    # Tier 6: identifier or map_name
    res_map = match_servers_by_query(mock_servers, "donut3")
    assert len(res_map) == 1
    assert res_map[0]["id"] == 1

    # Empty query
    assert match_servers_by_query(mock_servers, "") == []
    assert match_servers_by_query(mock_servers, "   ") == []

    # Non-matching query
    assert match_servers_by_query(mock_servers, "NonExistentServer999") == []


# ============================================================================
# Atomic Persistence & Corrupted File Quarantine Tests
# ============================================================================

def test_atomic_write_and_safe_load(tmp_path: Path) -> None:
    test_file = tmp_path / "sub" / "data.json"
    payload = {"status": "ok", "count": 10}

    atomic_write_json(test_file, payload)
    assert test_file.is_file()

    loaded, ok = safe_load_json(test_file, default_factory=dict)
    assert ok is True
    assert loaded == payload


def test_safe_load_corrupted_quarantine(tmp_path: Path) -> None:
    corrupted_file = tmp_path / "corrupted.json"
    corrupted_file.write_text("{this is not valid json!@@#", encoding="utf-8")

    loaded, ok = safe_load_json(corrupted_file, default_factory=dict, backup_on_error=True)
    assert ok is False
    assert loaded == {}

    # Check that original corrupted file was quarantined
    bak_files = list(tmp_path.glob("corrupted.json.corrupted.*.bak"))
    assert len(bak_files) == 1
    assert "{this is not valid json!" in bak_files[0].read_text(encoding="utf-8")


def test_safe_load_missing_file(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing.json"
    loaded, ok = safe_load_json(missing_file, default_factory=lambda: [1, 2, 3])
    assert ok is True
    assert loaded == [1, 2, 3]


# ============================================================================
# Whitelist Manager Tests
# ============================================================================

def test_whitelist_manager_basic(tmp_path: Path) -> None:
    wl_path = tmp_path / "whitelist.json"
    os.environ["HILLEYE_WHITELIST_IDS"] = "999888777666555444"

    wm = WhitelistManager(path=wl_path)
    owner_id = 111222333444555666
    file_user = 222333444555666777
    env_user = 999888777666555444
    stranger = 333444555666777888

    # Owner is always allowed
    assert wm.is_allowed(owner_id, owner_id=owner_id) is True
    # Stranger not allowed
    assert wm.is_allowed(stranger, owner_id=owner_id) is False
    # Env user allowed
    assert wm.is_allowed(env_user, owner_id=owner_id) is True

    # Add file user
    added = wm.add(file_user)
    assert added is True
    assert wm.is_allowed(file_user, owner_id=owner_id) is True

    # Verify atomic disk persistence
    assert wl_path.is_file()
    disk_data = json.loads(wl_path.read_text(encoding="utf-8"))
    assert file_user in disk_data["allowed_user_ids"]

    # Reload from disk into new instance
    wm2 = WhitelistManager(path=wl_path)
    assert wm2.is_allowed(file_user, owner_id=owner_id) is True

    # Remove user
    removed = wm.remove(file_user)
    assert removed is True
    assert wm.is_allowed(file_user, owner_id=owner_id) is False

    details = wm.get_details()
    assert env_user in details["env_ids"]
    assert file_user not in details["allowed_ids"]


def test_whitelist_manager_corrupted_file(tmp_path: Path) -> None:
    wl_path = tmp_path / "broken_wl.json"
    wl_path.write_text("CORRUPTED_JSON", encoding="utf-8")

    wm = WhitelistManager(path=wl_path)
    # Should safely initialize with empty allowed set rather than crashing
    assert len(wm.allowed_ids) == 0
    # Quarantined file should exist
    assert len(list(tmp_path.glob("broken_wl.json.corrupted.*.bak"))) == 1


# ============================================================================
# Subscription Manager Tests
# ============================================================================

def test_subscription_manager_lifecycle(tmp_path: Path) -> None:
    subs_path = tmp_path / "subs.json"
    sm = SubscriptionManager(path=subs_path)

    u1 = 123456789012345678
    u2 = 876543210987654321

    # Add watch
    sub1 = sm.add({"type": "watch", "user_id": u1, "channel_id": 111, "ckey": "testckey"})
    assert sub1["id"] == 1

    # Add alert
    sub2 = sm.add({"type": "alert", "user_id": u1, "channel_id": 111, "server_id": 5, "threshold": 20})
    assert sub2["id"] == 2

    # Add another user sub
    sub3 = sm.add({"type": "watch", "user_id": u2, "channel_id": 222, "ckey": "otherckey"})
    assert sub3["id"] == 3

    assert len(sm.for_user(u1)) == 2
    assert len(sm.for_user(u2)) == 1

    # Remove by ID (used on dead channels)
    removed_channel_sub = sm.remove_by_id(2)
    assert removed_channel_sub is True
    assert len(sm.for_user(u1)) == 1

    # Remove by user IDs set
    removed_count = sm.remove_ids(u1, {1})
    assert removed_count == 1
    assert len(sm.for_user(u1)) == 0
    assert len(sm.items) == 1  # only sub3 left


# ============================================================================
# Sparkline Generator Tests
# ============================================================================

def test_build_sparkline_24h_window() -> None:
    fixed_now = 1700000000.0  # arbitrary reference timestamp
    # Snapshots over 24 hours
    snapshots = [
        {"timestamp": fixed_now - 80000, "player_count": 10},
        {"timestamp": fixed_now - 50000, "player_count": 50},
        {"timestamp": fixed_now - 10000, "player_count": 100},
        {"timestamp": fixed_now - 100000, "player_count": 999},  # Older than 24h, must be ignored
    ]

    spark_str, stats = build_sparkline(snapshots, now=fixed_now)
    assert len(spark_str) == 24
    assert stats["min"] == 10
    assert stats["max"] == 100
    assert stats["count"] == 3
    # Check that older point (>86400s) was ignored
    assert stats["max"] < 999


def test_build_sparkline_empty() -> None:
    spark_str, stats = build_sparkline([], now=1700000000.0)
    assert len(spark_str) == 24
    assert spark_str == " " * 24
    assert stats["count"] == 0


def test_build_sparkline_flat() -> None:
    fixed_now = 1700000000.0
    snapshots = [
        {"timestamp": fixed_now - 3600, "player_count": 25},
        {"timestamp": fixed_now - 7200, "player_count": 25},
    ]
    spark_str, stats = build_sparkline(snapshots, now=fixed_now)
    assert stats["min"] == 25
    assert stats["max"] == 25
    # When min == max, uses lowest spark bar instead of division by zero
    non_spaces = [c for c in spark_str if c != " "]
    assert all(c == "▁" for c in non_spaces)


# ============================================================================
# Bounded TTL Cache Tests
# ============================================================================

def test_bounded_ttl_cache() -> None:
    cache = BoundedTTLCache(max_entries=3, default_ttl=0.2)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)

    assert cache.get("a") == 1
    assert cache.get("b") == 2
    assert cache.get("c") == 3

    # Add 4th entry: LRU should evict oldest (which is 'a' if 'b' and 'c' were accessed)
    cache.get("b")
    cache.get("c")
    cache.set("d", 4)
    assert cache.get("a") is None
    assert cache.get("d") == 4

    # Wait for TTL expiration
    time.sleep(0.25)
    assert cache.get("b") is None
    assert cache.get("c") is None
    assert cache.get("d") is None


# ============================================================================
# Hilleye API Client Status Code Tests
# ============================================================================

@pytest.mark.asyncio
async def test_api_client_status_codes() -> None:
    client = HilleyeAPIClient(base_url="http://mock-hilleye.local", cache_ttl=5.0)

    # 1. Successful 200 with mention defusal
    mock_resp_200 = MagicMock()
    mock_resp_200.status = 200
    mock_resp_200.json = AsyncMock(return_value={"server": "SS13 @everyone", "players": 15})

    with patch.object(client, "get_session") as mock_get_sess:
        mock_sess = MagicMock()
        mock_sess.get.return_value.__aenter__.return_value = mock_resp_200
        mock_get_sess.return_value = mock_sess

        data, err = await client.api_json("/api/v1/servers", cache=False)
        assert err is None
        assert data["server"] == "SS13 @\u200beveryone"

    # 2. 404 Not Found
    mock_resp_404 = MagicMock()
    mock_resp_404.status = 404
    with patch.object(client, "get_session") as mock_get_sess:
        mock_sess = MagicMock()
        mock_sess.get.return_value.__aenter__.return_value = mock_resp_404
        mock_get_sess.return_value = mock_sess

        data, err = await client.api_json("/api/v1/player/unknown", cache=False)
        assert data is None
        assert err == NOT_FOUND

    # 3. 503 Rejected on general endpoints
    mock_resp_503 = MagicMock()
    mock_resp_503.status = 503
    with patch.object(client, "get_session") as mock_get_sess:
        mock_sess = MagicMock()
        mock_sess.get.return_value.__aenter__.return_value = mock_resp_503
        mock_get_sess.return_value = mock_sess

        data, err = await client.api_json("/api/v1/servers", cache=False, allow_503=False)
        assert data is None
        assert "503" in (err or "")

    # 4. 503 Allowed on /health endpoint
    mock_resp_503_health = MagicMock()
    mock_resp_503_health.status = 503
    mock_resp_503_health.json = AsyncMock(return_value={"status": "degraded"})
    with patch.object(client, "get_session") as mock_get_sess:
        mock_sess = MagicMock()
        mock_sess.get.return_value.__aenter__.return_value = mock_resp_503_health
        mock_get_sess.return_value = mock_sess

        data, err = await client.api_json("/health", cache=False, allow_503=True)
        assert err is None
        assert data["status"] == "degraded"

    await client.close()


@pytest.mark.asyncio
async def test_api_client_resolve_server() -> None:
    client = HilleyeAPIClient(base_url="http://mock-hilleye.local")

    # 1. Direct numeric ID lookup hit
    with patch.object(client, "api_json") as mock_api:
        mock_api.side_effect = [
            ({"id": 12, "title": "SierraBay"}, None),
        ]
        srv, err = await client.resolve_server("12")
        assert err is None
        assert srv["id"] == 12

    # 2. Numeric ID misses, falls back to name matching
    with patch.object(client, "api_json") as mock_api:
        mock_api.side_effect = [
            (None, NOT_FOUND),  # direct ID /server/999 -> 404
            ({"servers": [{"id": 999, "title": "SierraBay 999", "current_players": 5}]}, None),  # list
            ({"id": 999, "title": "SierraBay 999", "current_players": 5, "map_name": "space"}, None),  # detail
        ]
        srv, err = await client.resolve_server("999")
        assert err is None
        assert srv["id"] == 999
        assert srv["map_name"] == "space"

    # 3. Non-numeric query goes straight to name matching
    with patch.object(client, "api_json") as mock_api:
        mock_api.side_effect = [
            ({"servers": [{"id": 2, "title": "Goonstation Roleplay", "current_players": 50}]}, None),  # list
            ({"id": 2, "title": "Goonstation Roleplay", "current_players": 50, "map_name": "cogmap"}, None),  # detail
        ]
        srv, err = await client.resolve_server("Goonstation")
        assert err is None
        assert srv["id"] == 2
        assert srv["map_name"] == "cogmap"

    await client.close()


# ============================================================================
# Bot Configuration & Allowed Mentions Safety Tests
# ============================================================================

def test_bot_allowed_mentions_security() -> None:
    # Verify global allowed_mentions on the bot instance
    assert bot.allowed_mentions is not None
    assert bot.allowed_mentions.everyone is False
    assert bot.allowed_mentions.roles is False
    assert bot.allowed_mentions.users is True


def test_bot_command_cooldowns_present() -> None:
    # Check that high-load commands enforce cooldowns
    cooldown_cmds = ["who", "compare", "servers", "graph", "top"]
    for cmd_name in cooldown_cmds:
        cmd = bot.get_command(cmd_name)
        assert cmd is not None, f"Command {cmd_name} not found"
        assert cmd._buckets._cooldown is not None, f"Command {cmd_name} missing cooldown"


# ============================================================================
# Additional Comprehensive Selfbot Hardening Tests
# ============================================================================

@pytest.mark.asyncio
async def test_notify_removes_dead_channel(tmp_path: Path) -> None:
    from examples.selfbot_example import notify, subs
    subs.path = tmp_path / "subs_test.json"
    subs.items = []
    sub = subs.add({"type": "watch", "user_id": 12345, "channel_id": 99999, "ckey": "test"})
    sub_id = sub["id"]

    # Mock bot.get_channel raising discord.NotFound
    with patch.object(bot, "get_channel", return_value=None):
        with patch.object(bot, "fetch_channel", side_effect=discord.NotFound(MagicMock(), "Channel deleted")):
            await notify(sub, "Hello")

    # Subscription should be automatically deactivated / removed
    assert not any(s["id"] == sub_id for s in subs.items)


@pytest.mark.asyncio
async def test_notify_removes_forbidden_channel(tmp_path: Path) -> None:
    from examples.selfbot_example import notify, subs
    subs.path = tmp_path / "subs_test.json"
    subs.items = []
    sub = subs.add({"type": "watch", "user_id": 12345, "channel_id": 88888, "ckey": "test"})
    sub_id = sub["id"]

    # Mock bot.get_channel returning a channel where send raises discord.Forbidden
    mock_channel = MagicMock(spec=discord.abc.Messageable)
    mock_channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "Missing permissions"))

    with patch.object(bot, "get_channel", return_value=mock_channel):
        await notify(sub, "Hello")

    # Subscription should be automatically removed
    assert not any(s["id"] == sub_id for s in subs.items)


@pytest.mark.asyncio
async def test_check_watches_skips_non_whitelisted_user(tmp_path: Path) -> None:
    from examples.selfbot_example import check_watches, subs, whitelist, _watch_online, api_client
    subs.path = tmp_path / "subs_test.json"
    subs.items = []
    whitelist.path = tmp_path / "wl_test.json"
    whitelist.allowed_ids = set()
    whitelist.env_ids = set()

    unauthorized_user = 555555555555555555
    subs.add({"type": "watch", "user_id": unauthorized_user, "channel_id": 777, "ckey": "sneaky"})
    _watch_online["sneaky"] = False  # previous state offline

    # Mock player online
    with patch.object(api_client, "fetch_player", return_value=({"is_online": True, "current_server": "Test Station"}, None)):
        with patch("examples.selfbot_example.notify", new_callable=AsyncMock) as mock_notify:
            await check_watches()
            # Since user is NOT whitelisted, notify should NOT be called
            mock_notify.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_skips_non_prefix_messages() -> None:
    from examples.selfbot_example import on_message, whitelist
    mock_msg = MagicMock(spec=discord.Message)
    mock_msg.author.bot = False
    mock_msg.content = "hello station how is the round going"
    mock_msg.author.id = 12345

    with patch.object(whitelist, "is_allowed") as mock_allowed:
        with patch.object(bot, "get_context") as mock_ctx:
            await on_message(mock_msg)
            # Prefix check short-circuits: is_allowed and get_context should never be called
            mock_allowed.assert_not_called()
            mock_ctx.assert_not_called()


@pytest.mark.asyncio
async def test_on_command_error_handling() -> None:
    from examples.selfbot_example import on_command_error
    mock_ctx = MagicMock()
    mock_ctx.send = AsyncMock()
    mock_ctx.command = "who"

    # Cooldown error
    cooldown_obj = commands.Cooldown(1, 3.0)
    cooldown_err = commands.CommandOnCooldown(cooldown_obj, retry_after=2.5, type=commands.BucketType.user)
    await on_command_error(mock_ctx, cooldown_err)
    mock_ctx.send.assert_called_with("⏳ Cooldown: please wait `2.5s` before reusing this command.")

    # Syntax error
    arg_err = commands.MissingRequiredArgument(MagicMock(name="ckey"))
    await on_command_error(mock_ctx, arg_err)
    mock_ctx.send.assert_called_with("⚠️ Invalid command arguments. Use `,commands` for syntax.")

    # Unexpected internal exception (must not leak internal stack traces)
    mock_ctx.send.reset_mock()
    internal_err = RuntimeError("Secret database path: C:/private/key.pem")
    await on_command_error(mock_ctx, internal_err)
    mock_ctx.send.assert_called_with("⚠️ An error occurred while executing the command.")


@pytest.mark.asyncio
async def test_send_long_truncation() -> None:
    from examples.selfbot_example import send_long
    mock_ctx = MagicMock()
    mock_ctx.send = AsyncMock()

    huge_text = "A" * 3000
    await send_long(mock_ctx, huge_text)
    assert mock_ctx.send.called
    sent_text = mock_ctx.send.call_args[0][0]
    assert len(sent_text) < 2000
    assert "…(truncated)" in sent_text


@pytest.mark.asyncio
async def test_api_client_card_retrieval() -> None:
    client = HilleyeAPIClient(base_url="http://mock-hilleye.local")

    # Success 200
    mock_resp_200 = MagicMock()
    mock_resp_200.status = 200
    mock_resp_200.read = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n...")

    with patch.object(client, "get_session") as mock_get_sess:
        mock_sess = MagicMock()
        mock_sess.get.return_value.__aenter__.return_value = mock_resp_200
        mock_get_sess.return_value = mock_sess

        png_bytes, err = await client.api_card("alice")
        assert err is None
        assert png_bytes.startswith(b"\x89PNG")

    # 404 Not Found
    mock_resp_404 = MagicMock()
    mock_resp_404.status = 404
    with patch.object(client, "get_session") as mock_get_sess:
        mock_sess = MagicMock()
        mock_sess.get.return_value.__aenter__.return_value = mock_resp_404
        mock_get_sess.return_value = mock_sess

        png_bytes, err = await client.api_card("unknown_player")
        assert png_bytes is None
        assert err == NOT_FOUND

    await client.close()

