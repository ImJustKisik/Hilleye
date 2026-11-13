"""Configuration loader and validator for CyberSyn Intel."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, field_validator

from core.hub_scraper import _parse_url_address

logger = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.json"

DEFAULT_CUSTOM_SERVERS: List[Dict[str, Any]] = [
    {
        "identifier": "byond://BYOND.world.1659081175",
        "title": "[RU] Celadon: SierraBay",
        "ip": "45.93.200.145",
        "port": 1350,
    },
    {
        "identifier": "byond://BYOND.world.805690456",
        "title": "[RU] Celadon Shiptest: Alpha",
        "ip": "45.93.200.145",
        "port": 1337,
    },
    {
        "identifier": "byond://BYOND.world.1821602746",
        "title": "[RU] [LRP] Celadon Skyrat",
        "ip": "45.93.200.145",
        "port": 7731,
    },
    {
        "identifier": "byond://178.62.241.130:1337",
        "title": "Tau Ceti Classic",
        "ip": "178.62.241.130",
        "port": 1337,
    },
]


class AppConfig(BaseModel):
    """Application configuration schema with fallback defaults and field synchronization."""

    poll_interval: int = Field(default=120, ge=1, le=86400, description="Polling interval in seconds")
    poll_interval_seconds: int = Field(default=120, ge=1, le=86400, description="Alias for poll_interval")
    grace_period_seconds: int = Field(
        default=300, ge=1, le=86400, description="Grace period before closing disconnected player session"
    )
    socket_timeout: float = Field(default=3.0, gt=0, le=300, allow_inf_nan=False, description="Socket connect/read timeout in seconds")
    topic_timeout_seconds: float = Field(default=3.0, gt=0, le=300, allow_inf_nan=False, description="Alias for socket_timeout")
    topic_concurrency: int = Field(default=30, ge=1, le=256, description="Maximum concurrent topic socket queries")
    topic_semaphore_limit: int = Field(default=30, ge=1, le=256, description="Alias for topic_concurrency")
    cycle_timeout_seconds: float = Field(default=300, gt=0, le=3600, allow_inf_nan=False)
    stale_server_seconds: int = Field(default=900, ge=1)
    min_free_disk_mb: int = Field(default=100, ge=0)
    db_path: str = Field(default="tracker.db", min_length=1, description="SQLite database path")
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
    discord_client_id: str = Field(default="", description="Discord OAuth2 application client ID")
    discord_client_secret: str = Field(default="", description="Discord OAuth2 application client secret")
    discord_redirect_uri: str = Field(default="", description="Explicit HTTPS Discord OAuth2 callback URI")
    discord_session_secret: str = Field(default="", description="HMAC key for signed session cookies")
    discord_superadmin_ids: List[str] = Field(
        default_factory=list,
        description="Base super-admin Discord user IDs with full whitelist control",
    )
    discord_session_ttl_seconds: int = Field(default=604800, ge=60, le=2592000)
    discord_secure_cookies: bool = Field(default=True, description="Require Secure flag on auth cookies")

    logs_root_dir: str = Field(default="", description="Root directory containing SierraBay12 round logs")
    logs_uploads_dir: str = Field(default="uploaded_logs", min_length=1, description="Directory for uploaded logs")
    logs_db_path: str = Field(default="logs.db", min_length=1, description="SQLite database path for log index/auth")
    logs_max_upload_mb: int = Field(default=50, ge=1, le=500)
    logs_max_extracted_mb: int = Field(default=200, ge=1, le=2000)
    logs_max_archive_files: int = Field(default=100, ge=1, le=1000)
    logs_index_include_debug: bool = Field(default=False)
    logs_auto_scan_on_startup: bool = Field(default=True)

    model_config = {"extra": "allow"}

    @field_validator("discord_superadmin_ids", mode="before")
    @classmethod
    def validate_superadmin_ids(cls, v: Any) -> List[str]:
        if isinstance(v, str):
            raw_items = [part.strip() for part in v.split(",")]
        elif isinstance(v, (list, tuple, set)):
            raw_items = [str(part).strip() for part in v]
        else:
            return []
        cleaned: List[str] = []
        seen = set()
        for item in raw_items:
            if item and item.isdigit() and item not in seen:
                seen.add(item)
                cleaned.append(item)
        return cleaned

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
        # Explicit top-level values take precedence over the legacy nested layout.
        nested = data.get("tracker", {})
        if nested is not None and not isinstance(nested, dict):
            raise ValueError("tracker must be an object")
        aliases = {
            "poll_interval_seconds": "poll_interval",
            "socket_timeout_seconds": "socket_timeout",
            "topic_timeout_seconds": "socket_timeout",
            "max_concurrent_sockets": "topic_concurrency",
            "topic_semaphore_limit": "topic_concurrency",
            "byond_hub_url": "hub_url",
        }
        for key, value in (nested or {}).items():
            canonical = aliases.get(key, key)
            if canonical not in data and not any(k in data for k, v in aliases.items() if v == canonical):
                data[canonical] = value
        for key, canonical in aliases.items():
            if key in data:
                data.setdefault(canonical, data[key])
        database = data.get("database", {})
        if not isinstance(database, dict):
            raise ValueError("database must be an object")
        if "path" in database:
            data.setdefault("db_path", database["path"])

        discord_cfg = data.get("discord", {})
        if discord_cfg is not None and not isinstance(discord_cfg, dict):
            raise ValueError("discord must be an object")
        discord_aliases = {
            "client_id": "discord_client_id",
            "client_secret": "discord_client_secret",
            "redirect_uri": "discord_redirect_uri",
            "session_secret": "discord_session_secret",
            "superadmin_ids": "discord_superadmin_ids",
            "session_ttl_seconds": "discord_session_ttl_seconds",
            "secure_cookies": "discord_secure_cookies",
        }
        for key, canonical in discord_aliases.items():
            if key in (discord_cfg or {}) and canonical not in data:
                data[canonical] = discord_cfg[key]

        logs_cfg = data.get("logs", {})
        if logs_cfg is not None and not isinstance(logs_cfg, dict):
            raise ValueError("logs must be an object")
        logs_aliases = {
            "root_dir": "logs_root_dir",
            "uploads_dir": "logs_uploads_dir",
            "db_path": "logs_db_path",
            "max_upload_mb": "logs_max_upload_mb",
            "max_extracted_mb": "logs_max_extracted_mb",
            "max_archive_files": "logs_max_archive_files",
            "index_include_debug": "logs_index_include_debug",
            "auto_scan_on_startup": "logs_auto_scan_on_startup",
        }
        for key, canonical in logs_aliases.items():
            if key in (logs_cfg or {}) and canonical not in data:
                data[canonical] = logs_cfg[key]

        # Isolate sibling logs storage when callers/tests specify an explicit db_path without explicit logs paths.
        explicit_db = data.get("db_path")
        if explicit_db and "logs_db_path" not in data:
            if explicit_db == ":memory:":
                data["logs_db_path"] = ":memory:"
            elif Path(str(explicit_db)).is_absolute():
                parent = Path(str(explicit_db)).parent
                data["logs_db_path"] = str(parent / "logs.db")
                if "logs_uploads_dir" not in data:
                    data["logs_uploads_dir"] = str(parent / "uploaded_logs")

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

    def resolved_db_path(self) -> str:
        if self.db_path == ":memory:":
            return self.db_path
        path = Path(self.db_path).expanduser()
        return str(path.resolve() if path.is_absolute() else (PROJECT_DIR / path).resolve())

    def resolved_logs_db_path(self) -> str:
        if self.logs_db_path == ":memory:":
            return self.logs_db_path
        path = Path(self.logs_db_path).expanduser()
        return str(path.resolve() if path.is_absolute() else (PROJECT_DIR / path).resolve())

    def resolved_logs_uploads_dir(self) -> Path:
        path = Path(self.logs_uploads_dir).expanduser()
        return path.resolve() if path.is_absolute() else (PROJECT_DIR / path).resolve()

    def resolved_logs_root_dir(self) -> Optional[Path]:
        raw = (self.logs_root_dir or "").strip()
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path.resolve() if path.is_absolute() else (PROJECT_DIR / path).resolve()

    def discord_oauth_configured(self) -> bool:
        return bool(
            (self.discord_client_id or "").strip()
            and (self.discord_client_secret or "").strip()
            and (self.discord_redirect_uri or "").strip()
        )

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


def _apply_env_overrides(raw_data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply environment variable overrides without altering explicit config schema."""
    env_string_map = {
        "HILLEYE_DB_PATH": "db_path",
        "HILLEYE_DISCORD_CLIENT_ID": "discord_client_id",
        "HILLEYE_DISCORD_CLIENT_SECRET": "discord_client_secret",
        "HILLEYE_DISCORD_REDIRECT_URI": "discord_redirect_uri",
        "HILLEYE_SESSION_SECRET": "discord_session_secret",
        "HILLEYE_DISCORD_SUPERADMIN_IDS": "discord_superadmin_ids",
        "HILLEYE_LOGS_DIR": "logs_root_dir",
        "HILLEYE_LOGS_UPLOADS_DIR": "logs_uploads_dir",
        "HILLEYE_LOGS_DB_PATH": "logs_db_path",
    }
    for env_name, field_name in env_string_map.items():
        val = os.getenv(env_name)
        if val is not None and val != "":
            raw_data[field_name] = val

    secure_env = os.getenv("HILLEYE_SECURE_COOKIES")
    if secure_env is not None and secure_env != "":
        raw_data["discord_secure_cookies"] = secure_env.strip().lower() not in {"0", "false", "no", "off"}

    max_upload_env = os.getenv("HILLEYE_LOGS_MAX_UPLOAD_MB")
    if max_upload_env is not None and max_upload_env != "":
        raw_data["logs_max_upload_mb"] = int(max_upload_env)
    return raw_data


def load_config(config_path: Union[str, Path] = DEFAULT_CONFIG_PATH) -> AppConfig:
    """
    Loads configuration from a JSON file.
    A missing file uses defaults; an invalid existing configuration stops startup.
    """
    path = Path(config_path)
    if not path.is_file():
        logger.info("Config file '%s' not found. Using default configuration.", path)
        return AppConfig(**_apply_env_overrides({"db_path": "tracker.db"}))

    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise ValueError("configuration is empty")
        raw_data = json.loads(content)
        if not isinstance(raw_data, dict):
            raise ValueError("configuration root must be an object")
        _apply_env_overrides(raw_data)
        return AppConfig(**raw_data)
    except Exception as exc:
        raise ValueError(f"Invalid configuration '{path}': {exc}") from exc


_GLOBAL_CONFIG: Optional[AppConfig] = None


def get_config(reload: bool = False, config_path: Union[str, Path] = DEFAULT_CONFIG_PATH) -> AppConfig:
    """Returns the cached global configuration or loads a new instance."""
    global _GLOBAL_CONFIG
    if _GLOBAL_CONFIG is None or reload:
        _GLOBAL_CONFIG = load_config(config_path)
    return _GLOBAL_CONFIG
