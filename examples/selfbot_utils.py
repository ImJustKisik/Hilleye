"""
Hilleye // Selfbot Shared Utilities & Re-exports

Modular entry point for selfbot utility functions, data managers, and API client.
"""

from examples.selfbot_example import (
    DEFAULT_CACHE_TTL,
    MAX_CACHE_ENTRIES,
    NOT_FOUND,
    SPARK_CHARS,
    BoundedTTLCache,
    HilleyeAPIClient,
    SubscriptionManager,
    WhitelistManager,
    _defuse_mentions,
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

__all__ = [
    "DEFAULT_CACHE_TTL",
    "MAX_CACHE_ENTRIES",
    "NOT_FOUND",
    "SPARK_CHARS",
    "BoundedTTLCache",
    "HilleyeAPIClient",
    "SubscriptionManager",
    "WhitelistManager",
    "_defuse_mentions",
    "atomic_write_json",
    "build_sparkline",
    "defuse_mentions",
    "escape_code_text",
    "extract_discord_user_id",
    "format_age",
    "match_servers_by_query",
    "normalize_ckey",
    "safe_load_json",
]
