"""SierraBay12 DMM/DMI map renderer, GitHub/local git fetcher, and fast area lookup cache."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from core.map_renderer.dm_parser import (
    _DmiSpriteAtlas,
    _clean_dm_value,
    _decode_rle,
    _encode_rle,
    _iter_dmm_defs,
    _load_dm_defines,
    _parse_atom_spec,
    _prettify_area_path,
    _scan_dm_type_properties,
    _split_dmm_tuple,
)
from core.map_renderer.renderer import (
    _ensure_area_cache_loaded,
    _render_dmm_file_levels,
    _render_maps_from_repo_sync,
    _render_nano_fallback,
    build_map_cache,
    get_map_areas_path,
    get_map_image_path,
    get_map_manifest,
    invalidate_area_cache,
    lookup_area_name,
    start_map_rebuild_background,
)
from core.map_renderer.repo_loader import (
    _ChunkQueueReader,
    _detect_local_git_commit,
    _download_github_repo_subset,
    _extract_tar_stream_sync,
    _should_extract_github_path,
)
from core.map_renderer.specs import (
    GITHUB_ARCHIVE_TIMEOUT_SECONDS,
    MAX_EXTRACTED_REPO_BYTES,
    MAX_GITHUB_ARCHIVE_BYTES,
    SIERRA_Z_LEVELS,
    SierraLevelSpec,
    _ALLOWED_EXTRACT_PREFIXES,
    _AREA_PATH_RE,
    _ASSIGN_RE,
    _ATTR_PAIR_RE,
    _DEFINE_RE,
    _DIR_ORDER_4,
    _DIR_ORDER_8,
    _DMM_BLOCK_RE,
    _DMM_DEF_START_RE,
    _GITHUB_BRANCH_RE,
    _GITHUB_REPO_RE,
    _INLINE_RE,
    _LEVEL_BY_Z,
    _SKIP_ATOM_PREFIXES,
    _TYPE_HDR_RE,
    _set_build_status,
    deck_short_label,
    get_map_build_status,
    set_github_http_transport,
)

logger = logging.getLogger(__name__)

_map_build_lock = asyncio.Lock()
_background_build_task: Optional[asyncio.Task] = None
_github_transport_factory: Optional[Callable[[], httpx.AsyncBaseTransport]] = None
_area_cache_lock = threading.Lock()
_area_cache_key: Optional[Tuple[str, float]] = None
_area_grids: Dict[int, Tuple[int, int, List[str], List[int]]] = {}
_status_lock = threading.Lock()
_build_status: Dict[str, Any] = {
    "state": "idle",
    "phase": "",
    "source": "",
    "downloaded_mb": 0.0,
    "total_mb": 0.0,
    "detail": "",
    "error": "",
    "started_at": "",
    "finished_at": "",
}

__all__ = [
    "GITHUB_ARCHIVE_TIMEOUT_SECONDS",
    "MAX_GITHUB_ARCHIVE_BYTES",
    "MAX_EXTRACTED_REPO_BYTES",
    "SierraLevelSpec",
    "SIERRA_Z_LEVELS",
    "_LEVEL_BY_Z",
    "_map_build_lock",
    "_background_build_task",
    "_github_transport_factory",
    "_area_cache_lock",
    "_area_cache_key",
    "_area_grids",
    "_status_lock",
    "_build_status",
    "_should_extract_github_path",
    "_set_build_status",
    "get_map_build_status",
    "set_github_http_transport",
    "_clean_dm_value",
    "_load_dm_defines",
    "_scan_dm_type_properties",
    "_prettify_area_path",
    "_DmiSpriteAtlas",
    "_split_dmm_tuple",
    "_parse_atom_spec",
    "_iter_dmm_defs",
    "_encode_rle",
    "_decode_rle",
    "_render_nano_fallback",
    "_render_dmm_file_levels",
    "_detect_local_git_commit",
    "_ChunkQueueReader",
    "_extract_tar_stream_sync",
    "_download_github_repo_subset",
    "_render_maps_from_repo_sync",
    "get_map_manifest",
    "get_map_image_path",
    "get_map_areas_path",
    "invalidate_area_cache",
    "_ensure_area_cache_loaded",
    "lookup_area_name",
    "deck_short_label",
    "build_map_cache",
    "start_map_rebuild_background",
]
