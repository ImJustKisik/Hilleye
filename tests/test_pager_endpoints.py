"""Tests for external pager_endpoints.json loading, merging, and Windows resolver helpers."""

import json
import pytest

from core.config import AppConfig
from core.hub_scraper import (
    ServerInfo,
    load_pager_endpoints,
    merge_custom_servers,
    parse_pager_endpoints_payload,
)
from tools.resolve_pager import is_valid_game_endpoint


def test_parse_pager_endpoints_payload_formats():
    raw = {
        "updated_at": 1700000000,
        "endpoints": {
            "byond://BYOND.world.111": {"ip": "45.93.200.145", "port": 1350},
            "BYOND.world.222": ["178.62.241.130", 1337],
            "byond://BYOND.world.333": "198.51.100.5:7731",
            "byond://BYOND.world.bad1": {"ip": "", "port": 1350},
            "byond://BYOND.world.bad2": {"ip": "1.2.3.4", "port": 99999},
        },
    }
    parsed = parse_pager_endpoints_payload(raw)
    assert parsed == {
        "byond://BYOND.world.111": ("45.93.200.145", 1350),
        "byond://BYOND.world.222": ("178.62.241.130", 1337),
        "byond://BYOND.world.333": ("198.51.100.5", 7731),
    }


@pytest.mark.asyncio
async def test_load_pager_endpoints_from_file(tmp_path):
    p = tmp_path / "pager_endpoints.json"
    p.write_text(
        json.dumps({"endpoints": {"byond://BYOND.world.999": {"ip": "203.0.113.10", "port": 4000}}}),
        encoding="utf-8",
    )
    loaded = await load_pager_endpoints(str(p))
    assert loaded == {"byond://BYOND.world.999": ("203.0.113.10", 4000)}

    missing = await load_pager_endpoints(str(tmp_path / "missing.json"))
    assert missing == {}


def test_merge_custom_servers_uses_extra_pager_endpoints():
    hub = [
        ServerInfo(identifier="byond://BYOND.world.555", title="External Pager", ip="BYOND.world.555", port=0),
    ]
    merged = merge_custom_servers(
        hub,
        custom_servers=[],
        extra_pager_endpoints={"byond://BYOND.world.555": ("203.0.113.55", 2500)},
    )
    assert len(merged) == 1
    assert merged[0].ip == "203.0.113.55"
    assert merged[0].port == 2500
    # External endpoints remain untrusted so public_topic_ip SSRF checks still apply
    assert merged[0].trusted is False


def test_config_pager_endpoints_fields():
    cfg = AppConfig(
        pager_endpoints_source="https://raw.githubusercontent.com/user/repo/main/pager_endpoints.json",
        pager_endpoints_refresh_seconds=300,
    )
    assert cfg.pager_endpoints_source.endswith("pager_endpoints.json")
    assert cfg.pager_endpoints_refresh_seconds == 300


def test_is_valid_game_endpoint():
    assert is_valid_game_endpoint("45.93.200.145", 1350) is True
    assert is_valid_game_endpoint("45.93.200.145", 6001) is False
    assert is_valid_game_endpoint("45.93.200.145", 20002) is False
    assert is_valid_game_endpoint("45.93.200.145", 443) is False
    assert is_valid_game_endpoint("127.0.0.1", 1350) is False
    assert is_valid_game_endpoint("192.168.1.10", 1350) is False
    assert is_valid_game_endpoint("192.168.1.10", 1350, allow_private=True) is True
