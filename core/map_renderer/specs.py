"""Z-level specifications, regexes, limits, and build status state helpers for the map renderer."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Dict, Optional, Tuple

import httpx

GITHUB_ARCHIVE_TIMEOUT_SECONDS: float = 300.0
MAX_GITHUB_ARCHIVE_BYTES: int = 1024 * 1024 * 1024
MAX_EXTRACTED_REPO_BYTES: int = 1024 * 1024 * 1024

_GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GITHUB_BRANCH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")

_DEFINE_RE = re.compile(r"^\s*#define\s+([A-Z0-9_]+)\s+([^\r\n/]+)")
_TYPE_HDR_RE = re.compile(r"^(/+(?:turf|obj|area|mob)(?:/[A-Za-z0-9_]+)*)\s*(?:\{|$)")
_ASSIGN_RE = re.compile(
    r"^(?:\t+| {2,})(icon|icon_state|color|dir|layer|name|alpha)\s*=\s*(.+?)\s*(?://.*)?$"
)
_INLINE_RE = re.compile(
    r"^(/+(?:turf|obj|area|mob)(?:/[A-Za-z0-9_]+)*)/(icon|icon_state|color|dir|layer|name|alpha)\s*=\s*(.+?)\s*(?://.*)?$"
)
_DMM_DEF_START_RE = re.compile(r'^"([^"]+)"\s*=\s*\(', re.MULTILINE)
_DMM_BLOCK_RE = re.compile(r'\((\d+),(\d+),(\d+)\)\s*=\s*\{"([^"]*)"\}', re.DOTALL)
_ATTR_PAIR_RE = re.compile(r'([A-Za-z0-9_]+)\s*=\s*("[^"]*"|\'[^\']*\'|[^;\r\n}]+)')
_AREA_PATH_RE = re.compile(r"/area(?:/[A-Za-z0-9_]+)*")

_DIR_ORDER_4: Dict[int, int] = {2: 0, 1: 1, 4: 2, 8: 3}
_DIR_ORDER_8: Dict[int, int] = {2: 0, 1: 1, 4: 2, 8: 3, 6: 4, 10: 5, 5: 6, 9: 7}

# Technical/non-physical helper prefixes excluded from clean station renders
_SKIP_ATOM_PREFIXES: Tuple[str, ...] = (
    "/area",
    "/turf/space",
    "/obj/effect",
    "/obj/landmark",
    "/obj/shuttle_landmark",
    "/obj/abstract",
    "/mob/observer",
)

_ALLOWED_EXTRACT_PREFIXES: Tuple[str, ...] = (
    "code/",
    "maps/sierra/",
    "mods/",
    "packs/",
    "icons/",
    "nano/images/sierra/",
)


@dataclass(frozen=True)
class SierraLevelSpec:
    z: int
    dmm_relpath: str
    dmm_z: int
    label: str
    short_label: str
    nano_fallback: Optional[str] = None


SIERRA_Z_LEVELS: Tuple[SierraLevelSpec, ...] = (
    SierraLevelSpec(
        z=1,
        dmm_relpath="maps/sierra/z1-z4_sierra.dmm",
        dmm_z=1,
        label="Палуба 4 — Инженерный / Карго / Гидропоника",
        short_label="Z1 • Палуба 4",
        nano_fallback="nano/images/sierra/sierra-1.png",
    ),
    SierraLevelSpec(
        z=2,
        dmm_relpath="maps/sierra/z1-z4_sierra.dmm",
        dmm_z=2,
        label="Палуба 3 — Технические отсеки / Ангар",
        short_label="Z2 • Палуба 3",
        nano_fallback="nano/images/sierra/sierra-2.png",
    ),
    SierraLevelSpec(
        z=3,
        dmm_relpath="maps/sierra/z1-z4_sierra.dmm",
        dmm_z=3,
        label="Палуба 2 — Медбэй / Наука / Бар",
        short_label="Z3 • Палуба 2",
        nano_fallback="nano/images/sierra/sierra-3.png",
    ),
    SierraLevelSpec(
        z=4,
        dmm_relpath="maps/sierra/z1-z4_sierra.dmm",
        dmm_z=4,
        label="Палуба 1 — Мостик / СБ / Командование",
        short_label="Z4 • Палуба 1",
        nano_fallback="nano/images/sierra/sierra-4.png",
    ),
    SierraLevelSpec(
        z=5,
        dmm_relpath="maps/sierra/z6_admin.dmm",
        dmm_z=1,
        label="ЦентКом / Админ-зона",
        short_label="Z5 • ЦентКом",
    ),
    SierraLevelSpec(
        z=6,
        dmm_relpath="maps/sierra/z7_transit.dmm",
        dmm_z=1,
        label="Транзит",
        short_label="Z6 • Транзит",
    ),
    SierraLevelSpec(
        z=7,
        dmm_relpath="maps/sierra/z8_virtualreality.dmm",
        dmm_z=1,
        label="Виртуальная реальность",
        short_label="Z7 • VR",
    ),
)

_LEVEL_BY_Z: Dict[int, SierraLevelSpec] = {spec.z: spec for spec in SIERRA_Z_LEVELS}


def _set_build_status(**updates: Any) -> None:
    import core.map_renderer as _mr

    with _mr._status_lock:
        _mr._build_status.update(updates)


def get_map_build_status() -> Dict[str, Any]:
    """Return a snapshot of the current/last station map build task."""
    import core.map_renderer as _mr

    with _mr._status_lock:
        return dict(_mr._build_status)


def set_github_http_transport(factory: Optional[Callable[[], httpx.AsyncBaseTransport]]) -> None:
    """Test hook for mocking GitHub archive downloads."""
    import core.map_renderer as _mr

    _mr._github_transport_factory = factory


def deck_short_label(z_level: int) -> str:
    spec = _LEVEL_BY_Z.get(int(z_level))
    if spec:
        return spec.short_label
    return f"Z{int(z_level)}"
