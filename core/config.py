"""Configuration loader and validator for CyberSyn Intel."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, field_validator

from core.hub_scraper import _parse_url_address

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config.json")

DEFAULT_CUSTOM_SERVERS: List[Dict[str, Any]] = [
    {
        "identifier": "byond://178.62.241.130:1337",
        "title": "Tau Ceti Classic",
        "ip": "178.62.241.130",
        "port": 1337,
    }
]


class AppConfig(BaseModel):
    """Application configuration schema with fallback defaults and field synchronization."""

    poll_interval: int = Field(default=120, description="Polling interval in seconds")
    poll_interval_seconds: int = Field(default=120, description="Alias for poll_interval")
    grace_period_seconds: int = Field(
        default=300, description="Grace period before closing disconnected player session"
    )
    socket_timeout: float = Field(default=3.0, description="Socket connect/read timeout in seconds")
    topic_timeout_seconds: float = Field(default=3.0, description="Alias for socket_timeout")
    topic_concurrency: int = Field(default=30, description="Maximum concurrent topic socket queries")
    topic_semaphore_limit: int = Field(default=30, description="Alias for topic_concurrency")
    db_path: str = Field(default="tracker.db", description="SQLite database path")
    hub_url: str = Field(
        default="http://www.byond.com/games/SpaceStation13?format=json",
        description="BYOND SS13 Hub JSON URL",
    )
    hub_text_url: str = Field(
        default="http://www.byond.com/games/Exadv1/SpaceStation13?format=text",
        description="BYOND SS13 Hub Text fallback URL",
    )
    hub_fallback_url: str = Field(
        default="http://www.byond.com/games/Exadv1/SpaceStation13?format=text",
        description="Alias for hub_text_url",
    )
    custom_servers: List[Dict[str, Any]] = Field(
        default_factory=lambda: list(DEFAULT_CUSTOM_SERVERS),
        description="User-defined unannounced servers to poll",
    )

    model_config = {"extra": "allow"}

    @field_validator("custom_servers", mode="before")
    @classmethod
    def validate_custom_servers(cls, v: Any) -> List[Dict[str, Any]]:
        """Ensure custom servers have normalized identifiers, ips, and ports."""
        if not isinstance(v, list):
            return list(DEFAULT_CUSTOM_SERVERS)
        sanitized = []
        for s in v:
            if not isinstance(s, dict):
                continue

            raw_ip = s.get("ip")
            ip = str(raw_ip).strip() if raw_ip is not None else ""
            if ip.lower() == "none":
                ip = ""

            raw_port = s.get("port")
            port = 0
            if raw_port is not None:
                port_str = str(raw_port).strip()
                if port_str.isdigit():
                    port = int(port_str)

            raw_ident = s.get("identifier")
            identifier = str(raw_ident).strip() if raw_ident is not None else ""
            if identifier.lower() == "none":
                identifier = ""

            # Extract ip and port from identifier if missing or port is 0
            if identifier and (not ip or port == 0):
                parsed_ident, parsed_ip, parsed_port = _parse_url_address(identifier)
                if not ip and parsed_ip:
                    ip = parsed_ip
                if port == 0 and parsed_port > 0:
                    port = parsed_port
                if not identifier:
                    identifier = parsed_ident

            if not identifier:
                if ip and port > 0:
                    identifier = f"byond://[{ip}]:{port}" if ":" in ip else f"byond://{ip}:{port}"
                elif ip:
                    identifier = f"byond://[{ip}]" if ":" in ip else f"byond://{ip}"

            raw_title = s.get("title")
            title = str(raw_title).strip() if raw_title is not None else ""
            if not title or title.lower() == "none":
                title = f"Server {ip}:{port}" if (ip and port > 0) else (f"Server {ip}" if ip else (identifier or "Custom Server"))

            sanitized.append(
                {
                    "identifier": identifier,
                    "title": title,
                    "ip": ip,
                    "port": port,
                }
            )
        return sanitized

    def __init__(self, **data: Any):
        # Synchronize alias pairs if one is provided
        if "poll_interval" in data and "poll_interval_seconds" not in data:
            data["poll_interval_seconds"] = data["poll_interval"]
        elif "poll_interval_seconds" in data and "poll_interval" not in data:
            data["poll_interval"] = data["poll_interval_seconds"]

        if "socket_timeout" in data and "topic_timeout_seconds" not in data:
            data["topic_timeout_seconds"] = data["socket_timeout"]
        elif "topic_timeout_seconds" in data and "socket_timeout" not in data:
            data["socket_timeout"] = data["topic_timeout_seconds"]

        if "topic_concurrency" in data and "topic_semaphore_limit" not in data:
            data["topic_semaphore_limit"] = data["topic_concurrency"]
        elif "topic_semaphore_limit" in data and "topic_concurrency" not in data:
            data["topic_concurrency"] = data["topic_semaphore_limit"]

        if "hub_text_url" in data and "hub_fallback_url" not in data:
            data["hub_fallback_url"] = data["hub_text_url"]
        elif "hub_fallback_url" in data and "hub_text_url" not in data:
            data["hub_text_url"] = data["hub_fallback_url"]

        super().__init__(**data)

    def __getitem__(self, item: str) -> Any:
        """Allow dictionary-style access for backwards compatibility."""
        if hasattr(self, item):
            return getattr(self, item)
        if item in self.__dict__:
            return self.__dict__[item]
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        """Allow dict-style .get() access."""
        try:
            return self[item]
        except KeyError:
            return default


Config = AppConfig


def load_config(config_path: Union[str, Path] = DEFAULT_CONFIG_PATH) -> AppConfig:
    """
    Loads configuration from a JSON file.
    Falls back gracefully to defaults if the file does not exist, is empty,
    or contains invalid JSON.
    """
    path = Path(config_path)
    if not path.is_file():
        logger.info("Config file '%s' not found. Using default configuration.", path)
        return AppConfig()

    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            logger.warning("Config file '%s' is empty. Using defaults.", path)
            return AppConfig()
        raw_data = json.loads(content)
        if not isinstance(raw_data, dict):
            logger.warning("Config file '%s' root is not a JSON object. Using defaults.", path)
            return AppConfig()
        return AppConfig(**raw_data)
    except Exception as exc:
        logger.warning(
            "Failed to load or parse config from '%s': %s. Using default configuration.",
            path,
            exc,
        )
        return AppConfig()


_GLOBAL_CONFIG: Optional[AppConfig] = None


def get_config(reload: bool = False, config_path: Union[str, Path] = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Returns the cached global configuration or loads a new instance."""
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None or reload:
        _GLOBAL_CONFIG = load_config(config_path)
    return _GLOBAL_CONFIG
