"""Tests for Topic Probing, payload normalization, and preferred_topic_query persistence."""

import asyncio
import pytest

from core import db
from core.byond_topic import (
    is_usable_topic_payload,
    normalize_topic_payload,
    probe_server_topic,
)
from core.config import AppConfig, DEFAULT_TOPIC_PROBE_QUERIES


def test_config_topic_probe_queries_normalization():
    cfg = AppConfig(topic_probe_queries=["status=2", " ?who ", "?who", ""])
    assert cfg.topic_probe_queries == ["?status=2", "?who"]

    cfg_default = AppConfig(topic_probe_queries=[])
    assert cfg_default.topic_probe_queries == DEFAULT_TOPIC_PROBE_QUERIES


def test_normalize_topic_payload_variants():
    # Dict with mixed case keys
    assert normalize_topic_payload({"Players": 12, "Map_Name": "Box"}) == {
        "players": 12,
        "map_name": "Box",
    }

    # Raw numeric string (?ping / ?playing)
    assert normalize_topic_payload({"raw": "15"}) == {"players": 15}
    assert normalize_topic_payload({"raw": "hello"}) == {"raw": "hello"}

    # Float (?ping TYPE_FLOAT)
    assert normalize_topic_payload(24.0) == {"players": 24}
    assert normalize_topic_payload(-1.0) is None
    assert normalize_topic_payload(True) is None

    # List (?who)
    who_norm = normalize_topic_payload(["Alice", "Bob"])
    assert who_norm["players"] == 2
    assert list(who_norm["playerlist"]) == ["Alice", "Bob"]

    # Empty list (?who when empty)
    empty_who = normalize_topic_payload([])
    assert empty_who["players"] == 0
    assert list(empty_who["playerlist"]) == []


def test_is_usable_topic_payload():
    assert is_usable_topic_payload({"players": 0}) is True
    assert is_usable_topic_payload({"map_name": "MetaStation"}) is True
    assert is_usable_topic_payload({"round_duration": "01:15:00"}) is True
    assert is_usable_topic_payload({"raw": "ok"}) is False
    assert is_usable_topic_payload({}) is False


@pytest.mark.asyncio
async def test_probe_server_topic_fallback_and_preferred():
    calls = []

    async def fake_query(ip, port, query, timeout=3.0, semaphore=None):
        calls.append(query)
        if query == "?who":
            return {"players": ["alice", "bob"], "playerlist": ["alice", "bob"]}
        return {"raw": "unknown"}

    payload, winner, should_clear = await probe_server_topic(
        ip="127.0.0.1",
        port=1337,
        queries=["?status=2", "?status", "?who", "?ping"],
        preferred_query=None,
        query_fn=fake_query,
    )
    assert calls == ["?status=2", "?status", "?who"]
    assert winner == "?who"
    assert should_clear is False
    assert payload["playerlist"] == ["alice", "bob"]

    # With preferred_query, it tries ?who first in a single call
    calls.clear()
    payload2, winner2, should_clear2 = await probe_server_topic(
        ip="127.0.0.1",
        port=1337,
        queries=["?status=2", "?status", "?who", "?ping"],
        preferred_query="?who",
        query_fn=fake_query,
    )
    assert calls == ["?who"]
    assert winner2 == "?who"
    assert should_clear2 is False


@pytest.mark.asyncio
async def test_probe_server_topic_short_circuits_on_transport_error():
    calls = []

    async def failing_query(ip, port, query, timeout=3.0, semaphore=None):
        calls.append(query)
        raise asyncio.TimeoutError()

    payload, winner, should_clear = await probe_server_topic(
        ip="127.0.0.1",
        port=1337,
        queries=["?status=2", "?status", "?who", "?ping"],
        preferred_query="?status=2",
        query_fn=failing_query,
    )
    assert calls == ["?status=2"]
    assert payload is None
    assert winner is None
    assert should_clear is False


@pytest.mark.asyncio
async def test_probe_server_topic_clears_stale_preferred_when_all_unusable():
    calls = []

    async def unusable_query(ip, port, query, timeout=3.0, semaphore=None):
        calls.append(query)
        return {"raw": "nope"}

    payload, winner, should_clear = await probe_server_topic(
        ip="127.0.0.1",
        port=1337,
        queries=["?status=2", "?status"],
        preferred_query="?who",
        query_fn=unusable_query,
    )
    assert calls == ["?who", "?status=2", "?status"]
    assert payload is None
    assert winner is None
    assert should_clear is True


@pytest.mark.asyncio
async def test_db_preferred_topic_query_persistence(test_db):
    ident = "byond://127.0.0.1:7777"
    await db.upsert_server(ident, "Probe Server", "127.0.0.1", 7777, now_ts=1000.0)
    assert await db.get_preferred_topic_queries() == {}

    await db.set_preferred_topic_query(ident, "?who")
    assert await db.get_preferred_topic_queries() == {ident: "?who"}

    await db.set_preferred_topic_query(ident, None)
    assert await db.get_preferred_topic_queries() == {}
