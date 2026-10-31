"""Unit tests for configuration loading and validation."""

import json
from pathlib import Path
import pytest
from core.config import AppConfig, get_config, load_config


class TestConfigLoading:
    def test_load_default_config(self):
        cfg = load_config()
        assert cfg.poll_interval == 120
        assert cfg.poll_interval_seconds == 120
        assert cfg.grace_period_seconds == 300
        assert cfg.socket_timeout == 3.0
        assert cfg.topic_timeout_seconds == 3.0
        assert cfg.topic_concurrency == 30
        assert cfg.topic_semaphore_limit == 30
        assert cfg.db_path == "tracker.db"
        assert "format=json" in cfg.hub_url
        assert "format=text" in cfg.hub_text_url
        assert len(cfg.custom_servers) >= 1

    def test_dict_style_access(self):
        cfg = load_config()
        assert cfg["poll_interval"] == 120
        assert cfg.get("grace_period_seconds") == 300
        assert cfg.get("non_existent_key", "default_val") == "default_val"

    def test_load_non_existent_file_returns_defaults(self, tmp_path: Path):
        fake_path = tmp_path / "does_not_exist.json"
        cfg = load_config(fake_path)
        assert cfg.poll_interval == 120
        assert cfg.grace_period_seconds == 300

    def test_load_corrupted_json_returns_defaults(self, tmp_path: Path):
        bad_file = tmp_path / "corrupted.json"
        bad_file.write_text("{invalid json", encoding="utf-8")
        cfg = load_config(bad_file)
        assert cfg.poll_interval == 120

    def test_alias_synchronization(self):
        cfg1 = AppConfig(poll_interval=60, socket_timeout=5.0)
        assert cfg1.poll_interval_seconds == 60
        assert cfg1.topic_timeout_seconds == 5.0

        cfg2 = AppConfig(poll_interval_seconds=45, topic_timeout_seconds=2.0)
        assert cfg2.poll_interval == 45
        assert cfg2.socket_timeout == 2.0

    def test_custom_servers_normalization(self):
        raw_servers = [
            {"ip": "1.2.3.4", "port": 7777, "title": "My Server"},
            {"identifier": "byond://custom.station:1234", "ip": "custom.station", "port": 1234},
        ]
        cfg = AppConfig(custom_servers=raw_servers)
        assert len(cfg.custom_servers) == 2
        assert cfg.custom_servers[0]["identifier"] == "byond://1.2.3.4:7777"
        assert cfg.custom_servers[1]["title"] == "Server custom.station:1234"
