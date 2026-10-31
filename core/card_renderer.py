"""
Hilleye // Lightweight Pillow Player Dossier PNG Card Renderer
Generates 720x360 RGBA tactical summary cards in-memory (< 20ms) for Discord clients.
"""

from datetime import datetime, timezone
from functools import lru_cache
import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image, ImageDraw, ImageFont

CARD_WIDTH = 720
CARD_HEIGHT = 360
ERROR_CARD_HEIGHT = 160

# Color Palette (RGBA / Hex)
COLOR_BG = "#0f1117"
COLOR_BORDER = "#1e222d"
COLOR_CKEY = "#f1f5f9"
COLOR_ONLINE = "#22c55e"
COLOR_OFFLINE = "#64748b"
COLOR_CARD_BG = "#161922"
COLOR_CARD_BORDER = "#232733"
COLOR_LABEL = "#94a3b8"
COLOR_VALUE = "#f8fafc"
COLOR_SECTION = "#64748b"
COLOR_BAR_TRACK = "#1e222d"
COLOR_BAR_FILL = "#3b82f6"
COLOR_FOOTER = "#475569"

FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

_REGULAR_FONT_CANDIDATES = (
    "DejaVuSans.ttf",
    "Inter.ttf",
    "RobotoMono-Regular.ttf",
)
_BOLD_FONT_CANDIDATES = (
    "DejaVuSans-Bold.ttf",
    "DejaVuSans.ttf",
    "Inter-Bold.ttf",
    "Inter.ttf",
    "RobotoMono-Bold.ttf",
    "RobotoMono-Regular.ttf",
)


@lru_cache(maxsize=32)
def _load_font(
    size: int,
    bold: bool = False,
    font_dir: Optional[str] = None,
) -> Union[ImageFont.FreeTypeFont, ImageFont.ImageFont]:
    """
    Loads a TrueType font from core/assets/fonts/ with LRU caching.
    Safely falls back to ImageFont.load_default() if no TrueType font is found.
    """
    search_dir = Path(font_dir) if font_dir else FONTS_DIR
    candidates = _BOLD_FONT_CANDIDATES if bold else _REGULAR_FONT_CANDIDATES

    for filename in candidates:
        candidate_path = search_dir / filename
        if candidate_path.is_file():
            try:
                return ImageFont.truetype(str(candidate_path), size=size)
            except Exception:
                continue

    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _truncate_text(text: str, max_chars: int = 26) -> str:
    """Truncates string to max_chars with '...' on overflow."""
    clean = str(text or "").strip()
    if len(clean) <= max_chars:
        return clean
    if max_chars <= 3:
        return "." * max_chars
    return clean[: max_chars - 3] + "..."


def _extract_date_short(ts_val: Any) -> str:
    """Extracts YYYY-MM-DD date string from a timestamp or formatted date string."""
    if ts_val is None or ts_val == "":
        return "N/A"
    if isinstance(ts_val, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts_val), tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return "N/A"
    text = str(ts_val).strip()
    if len(text) >= 10 and text[4:5] == "-" and text[7:8] == "-":
        return text[:10]
    return text[:10] if text else "N/A"


def _measure_text(draw: ImageDraw.ImageDraw, text: str, font: Any) -> Tuple[int, int]:
    """Returns (width, height) of rendered text using textbbox."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return int(bbox[2] - bbox[0]), int(bbox[3] - bbox[1])
    except Exception:
        return len(text) * 8, 14


@lru_cache(maxsize=1)
def _get_base_card_template() -> Image.Image:
    """
    Pre-renders and caches the static 720x360 RGBA card background, border,
    4 metric containers with static labels, and section header.
    """
    base = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(base)

    # Background (#0f1117) with 1px rounded border (#1e222d, radius=8)
    draw.rounded_rectangle(
        [0, 0, CARD_WIDTH - 1, CARD_HEIGHT - 1],
        radius=8,
        fill=COLOR_BG,
        outline=COLOR_BORDER,
        width=1,
    )

    font_metric_label = _load_font(11, bold=False)
    font_section = _load_font(11, bold=True)

    metric_labels = ("Total Hours", "Sessions", "Servers", "First Seen")
    grid_x_start = 24
    grid_y1 = 80
    grid_y2 = 140
    card_gap = 12
    card_w = (CARD_WIDTH - 48 - card_gap * 3) // 4  # 159 px

    for idx, label in enumerate(metric_labels):
        cx1 = grid_x_start + idx * (card_w + card_gap)
        cx2 = cx1 + card_w
        draw.rounded_rectangle(
            [cx1, grid_y1, cx2, grid_y2],
            radius=6,
            fill=COLOR_CARD_BG,
            outline=COLOR_CARD_BORDER,
            width=1,
        )
        draw.text((cx1 + 12, grid_y1 + 9), label, fill=COLOR_LABEL, font=font_metric_label)

    draw.text(
        (24, 160),
        "SERVER PLAYTIME DISTRIBUTION",
        fill=COLOR_SECTION,
        font=font_section,
    )
    return base


def render_player_card(dossier: Dict[str, Any]) -> bytes:
    """
    Renders a 720x360 RGBA PNG player dossier card in memory using Pillow.

    Args:
        dossier: Player dossier dictionary from core.db.get_player_dossier().

    Returns:
        Optimized PNG image bytes.
    """
    if not isinstance(dossier, dict):
        dossier = {}

    image = _get_base_card_template().copy()
    draw = ImageDraw.Draw(image)

    # Load cached fonts
    font_ckey = _load_font(23, bold=True)
    font_pill = _load_font(12, bold=False)
    font_metric_val = _load_font(19, bold=True)
    font_srv_name = _load_font(13, bold=False)
    font_srv_stat = _load_font(12, bold=True)
    font_footer = _load_font(11, bold=False)

    # Extract normalized fields with fallbacks for nested or flat dossier structures
    status_block = dossier.get("status") if isinstance(dossier.get("status"), dict) else {}
    summary_block = dossier.get("summary") if isinstance(dossier.get("summary"), dict) else {}

    ckey = str(dossier.get("ckey") or "unknown")
    is_online = bool(
        dossier.get("is_online")
        if "is_online" in dossier
        else status_block.get("is_online", status_block.get("online", False))
    )
    current_server = (
        dossier.get("current_server")
        or status_block.get("current_server")
        or "Unknown Server"
    )
    current_duration = (
        dossier.get("current_session_duration")
        or status_block.get("current_session_duration")
        or "0m"
    )
    last_seen_raw = (
        dossier.get("last_seen")
        or status_block.get("last_seen")
        or summary_block.get("last_seen")
    )
    first_seen_raw = (
        dossier.get("first_seen")
        or summary_block.get("first_seen")
    )

    # -------------------------------------------------------------------------
    # 1. Header (y = 20..60)
    # -------------------------------------------------------------------------
    draw.text((24, 24), ckey, fill=COLOR_CKEY, font=font_ckey)

    if is_online:
        srv_short = _truncate_text(str(current_server), 24)
        status_text = f"Online: {srv_short} ({current_duration})"
        dot_color = COLOR_ONLINE
    else:
        last_seen_date = _extract_date_short(last_seen_raw)
        if last_seen_date != "N/A":
            status_text = f"Offline • Last seen: {last_seen_date}"
        else:
            status_text = "Offline"
        dot_color = COLOR_OFFLINE

    text_w, text_h = _measure_text(draw, status_text, font_pill)
    pill_pad_x = 12
    dot_size = 8
    dot_gap = 8
    pill_w = pill_pad_x * 2 + dot_size + dot_gap + text_w
    pill_h = 30
    pill_x2 = CARD_WIDTH - 24
    pill_x1 = max(240, pill_x2 - pill_w)
    pill_y1 = 23
    pill_y2 = pill_y1 + pill_h

    draw.rounded_rectangle(
        [pill_x1, pill_y1, pill_x2, pill_y2],
        radius=15,
        fill=COLOR_CARD_BG,
        outline=COLOR_CARD_BORDER,
        width=1,
    )

    dot_x1 = pill_x1 + pill_pad_x
    dot_y1 = pill_y1 + (pill_h - dot_size) // 2
    draw.ellipse(
        [dot_x1, dot_y1, dot_x1 + dot_size, dot_y1 + dot_size],
        fill=dot_color,
    )

    text_x = dot_x1 + dot_size + dot_gap
    try:
        bbox = draw.textbbox((0, 0), status_text, font=font_pill)
        text_y = pill_y1 + (pill_h - (bbox[3] - bbox[1])) // 2 - bbox[1]
    except Exception:
        text_y = pill_y1 + (pill_h - text_h) // 2
    draw.text((text_x, text_y), status_text, fill=COLOR_CKEY, font=font_pill)

    # -------------------------------------------------------------------------
    # 2. Key Metrics Grid (y = 80..140)
    # -------------------------------------------------------------------------
    total_seconds = float(
        dossier.get(
            "total_playtime_seconds",
            summary_block.get("total_playtime_seconds", 0),
        )
        or 0.0
    )
    if "total_hours" in dossier and dossier["total_hours"] is not None:
        total_hours = float(dossier["total_hours"])
    elif "total_hours" in summary_block and summary_block["total_hours"] is not None:
        total_hours = float(summary_block["total_hours"])
    else:
        total_hours = round(total_seconds / 3600.0, 1)

    recent_sessions = dossier.get("recent_sessions") or dossier.get("sessions") or []
    if "sessions_count" in dossier and dossier["sessions_count"] is not None:
        sessions_count = int(dossier["sessions_count"])
    elif "sessions_count" in summary_block and summary_block["sessions_count"] is not None:
        sessions_count = int(summary_block["sessions_count"])
    elif "total_sessions" in dossier and dossier["total_sessions"] is not None:
        sessions_count = int(dossier["total_sessions"])
    else:
        sessions_count = len(recent_sessions) if isinstance(recent_sessions, list) else 0

    raw_breakdown = dossier.get("server_breakdown")
    if raw_breakdown is None:
        raw_breakdown = dossier.get("servers") or []

    if "servers_count" in dossier and dossier["servers_count"] is not None:
        servers_count = int(dossier["servers_count"])
    elif "servers_count" in summary_block and summary_block["servers_count"] is not None:
        servers_count = int(summary_block["servers_count"])
    else:
        servers_count = len(raw_breakdown) if isinstance(raw_breakdown, list) else 0

    first_seen_str = _extract_date_short(first_seen_raw)

    metric_values = (
        f"{total_hours:.1f}h",
        str(sessions_count),
        str(servers_count),
        first_seen_str,
    )

    grid_x_start = 24
    grid_y1 = 80
    card_gap = 12
    card_w = (CARD_WIDTH - 48 - card_gap * 3) // 4  # 159 px

    for idx, val_str in enumerate(metric_values):
        cx1 = grid_x_start + idx * (card_w + card_gap)
        draw.text((cx1 + 12, grid_y1 + 28), val_str, fill=COLOR_VALUE, font=font_metric_val)

    # -------------------------------------------------------------------------
    # 3. Server Playtime Distribution (y = 160..300)
    # -------------------------------------------------------------------------

    breakdown_list: List[Dict[str, Any]] = (
        list(raw_breakdown) if isinstance(raw_breakdown, list) else []
    )
    # Sort descending by seconds/hours if not already sorted
    breakdown_list.sort(
        key=lambda item: float(item.get("seconds", item.get("hours", 0.0)) or 0.0),
        reverse=True,
    )
    top_servers = breakdown_list[:3]

    if not top_servers:
        draw.text(
            (24, 215),
            "No server playtime recorded yet.",
            fill=COLOR_OFFLINE,
            font=font_srv_name,
        )
    else:
        row_start_y = 186
        row_step_y = 38
        name_x = 24
        bar_x1 = 240
        bar_x2 = 576
        bar_w = bar_x2 - bar_x1
        bar_h = 12

        for idx, srv in enumerate(top_servers):
            ry = row_start_y + idx * row_step_y
            raw_title = str(srv.get("title") or srv.get("server_title") or f"Server #{idx + 1}")
            srv_title = _truncate_text(raw_title, max_chars=26)

            srv_seconds = float(srv.get("seconds") or 0.0)
            if "hours" in srv and srv["hours"] is not None:
                srv_hours = float(srv["hours"])
            else:
                srv_hours = round(srv_seconds / 3600.0, 1)

            # Safe percentage calculation avoiding ZeroDivisionError
            if "percentage" in srv and srv["percentage"] is not None:
                pct = float(srv["percentage"])
            elif total_seconds > 0:
                pct = (srv_seconds / total_seconds) * 100.0
            elif total_hours > 0:
                pct = (srv_hours / total_hours) * 100.0
            else:
                pct = 0.0

            pct = max(0.0, min(100.0, pct))

            # Server title on the left
            draw.text((name_x, ry), srv_title, fill=COLOR_CKEY, font=font_srv_name)

            # Progress bar track
            by1 = ry + 3
            by2 = by1 + bar_h
            draw.rounded_rectangle(
                [bar_x1, by1, bar_x2, by2],
                radius=6,
                fill=COLOR_BAR_TRACK,
            )

            # Progress bar fill
            fill_w = int(round((pct / 100.0) * bar_w))
            if fill_w > 0:
                fill_w = max(6, min(bar_w, fill_w))
                draw.rounded_rectangle(
                    [bar_x1, by1, bar_x1 + fill_w, by2],
                    radius=6,
                    fill=COLOR_BAR_FILL,
                )

            # Right numeric stats: percentage and hours
            stat_text = f"{pct:.0f}%  {srv_hours:.1f}h"
            stat_w, _ = _measure_text(draw, stat_text, font_srv_stat)
            draw.text(
                (CARD_WIDTH - 24 - stat_w, ry + 1),
                stat_text,
                fill=COLOR_VALUE,
                font=font_srv_stat,
            )

    # -------------------------------------------------------------------------
    # 4. Footer (y = 325..345)
    # -------------------------------------------------------------------------
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    footer_text = f"Hilleye Telemetry • Generated UTC ({now_utc})"
    draw.text((24, 328), footer_text, fill=COLOR_FOOTER, font=font_footer)

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def render_not_found_card(ckey: str) -> bytes:
    """
    Renders a 720x160 RGBA PNG 404 error card for unknown players.
    """
    image = Image.new("RGBA", (CARD_WIDTH, ERROR_CARD_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle(
        [0, 0, CARD_WIDTH - 1, ERROR_CARD_HEIGHT - 1],
        radius=8,
        fill=COLOR_BG,
        outline=COLOR_BORDER,
        width=1,
    )

    font_title = _load_font(20, bold=True)
    font_sub = _load_font(13, bold=False)
    font_footer = _load_font(11, bold=False)

    clean_ckey = _truncate_text(str(ckey or "unknown"), 32)
    draw.text((24, 32), f"404 NOT FOUND // {clean_ckey}", fill="#e11d48", font=font_title)
    draw.text(
        (24, 70),
        "Target ckey has not been observed on any monitored SS13 sector.",
        fill=COLOR_LABEL,
        font=font_sub,
    )
    draw.text(
        (24, 124),
        "Hilleye Telemetry • Generated UTC",
        fill=COLOR_FOOTER,
        font=font_footer,
    )

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
