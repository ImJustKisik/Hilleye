"""
Unit and Integration Tests for Pillow Player Card Renderer (core/card_renderer.py)
and FastAPI PNG Card Endpoint (GET /api/v1/player/{ckey}/card.png).
"""

import io
from unittest.mock import patch

from PIL import Image
import pytest

from core import db as core_db
from core.card_renderer import (
    CARD_HEIGHT,
    CARD_WIDTH,
    _load_font,
    _truncate_text,
    render_not_found_card,
    render_player_card,
)
from web import app as web_app

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def test_render_player_card_online():
    """
    Verifies PNG card generation for an online player with active server session
    and playtime distribution. Checks PNG signature, 720x360 dimensions, and RGBA mode.
    """
    dossier = {
        "ckey": "honkmaster",
        "is_online": True,
        "current_server": "Tau Ceti Classic",
        "current_session_duration": "1h 45m",
        "total_hours": 284.5,
        "total_playtime_seconds": 1024200,
        "sessions_count": 48,
        "servers_count": 3,
        "first_seen": "2026-01-15 12:30:00",
        "last_seen": "2026-10-31 16:00:00",
        "server_breakdown": [
            {
                "server_id": 1,
                "title": "Tau Ceti Classic",
                "seconds": 757800,
                "hours": 210.5,
                "percentage": 74.0,
            },
            {
                "server_id": 2,
                "title": "White Dream 1 - Very Long Extended Server Title Over 26 Chars",
                "seconds": 194400,
                "hours": 54.0,
                "percentage": 19.0,
            },
            {
                "server_id": 3,
                "title": "Paradise Station",
                "seconds": 72000,
                "hours": 20.0,
                "percentage": 7.0,
            },
        ],
        "recent_sessions": [
            {
                "server_id": 1,
                "server_title": "Tau Ceti Classic",
                "started_at": "2026-10-31 14:15:00",
                "ended_at": None,
                "duration_seconds": 6300,
                "duration_str": "1h 45m",
                "is_active": True,
            }
        ],
    }

    png_bytes = render_player_card(dossier)

    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(PNG_SIGNATURE)
    assert len(png_bytes) > 1000

    with Image.open(io.BytesIO(png_bytes)) as img:
        assert img.format == "PNG"
        assert img.size == (CARD_WIDTH, CARD_HEIGHT)
        assert img.mode == "RGBA"


def test_render_player_card_offline():
    """
    Verifies PNG card generation for an offline player with zero playtime
    and empty sessions/servers lists without raising ZeroDivisionError.
    """
    dossier = {
        "ckey": "ghostplayer",
        "is_online": False,
        "current_server": None,
        "current_session_duration": None,
        "total_hours": 0.0,
        "total_playtime_seconds": 0,
        "sessions_count": 0,
        "servers_count": 0,
        "first_seen": "2026-05-01 09:00:00",
        "last_seen": "2026-05-01 09:00:00",
        "server_breakdown": [],
        "recent_sessions": [],
    }

    png_bytes = render_player_card(dossier)

    assert isinstance(png_bytes, bytes)
    assert png_bytes.startswith(PNG_SIGNATURE)

    with Image.open(io.BytesIO(png_bytes)) as img:
        assert img.format == "PNG"
        assert img.size == (CARD_WIDTH, CARD_HEIGHT)
        assert img.mode == "RGBA"

    # Also verify offline player with a zero-second server entry does not trigger ZeroDivisionError
    zero_srv_dossier = {
        **dossier,
        "server_breakdown": [
            {"server_id": 1, "title": "Zero Time Sector", "seconds": 0, "hours": 0.0}
        ],
    }
    png_zero = render_player_card(zero_srv_dossier)
    assert png_zero.startswith(PNG_SIGNATURE)


@pytest.mark.asyncio
async def test_card_endpoint_200(async_client):
    """
    Calls GET /api/v1/player/{ckey}/card.png for a seeded player and verifies
    HTTP 200, Content-Type: image/png, Cache-Control header, and valid PNG payload.
    """
    web_app.clear_card_cache()
    server_id = await core_db.upsert_server(
        identifier="byond://178.62.241.130:1337",
        title="Tau Ceti Classic",
        ip="178.62.241.130",
        port=1337,
    )
    await core_db.update_player_sessions(server_id, {"honkmaster"}, now_ts=1000.0)
    await core_db.update_player_sessions(server_id, {"honkmaster"}, now_ts=4600.0)

    response = await async_client.get("/api/v1/player/Honk_Master/card.png")
    assert response.status_code == 200
    assert response.headers.get("content-type") == "image/png"
    assert "max-age=60" in response.headers.get("cache-control", "")
    assert response.content.startswith(PNG_SIGNATURE)

    with Image.open(io.BytesIO(response.content)) as img:
        assert img.size == (720, 360)
        assert img.mode == "RGBA"


@pytest.mark.asyncio
async def test_card_endpoint_404(async_client):
    """
    Verifies GET /api/v1/player/{ckey}/card.png returns HTTP 404 for unknown
    or invalid ckeys.
    """
    web_app.clear_card_cache()
    response = await async_client.get("/api/v1/player/nonexistent_agent_404/card.png")
    assert response.status_code == 404

    invalid_response = await async_client.get("/api/v1/player/___/card.png")
    assert invalid_response.status_code == 404


@pytest.mark.asyncio
async def test_card_endpoint_in_memory_ttl_cache(async_client):
    """
    Verifies that repeated requests within the 60s TTL window serve the cached PNG
    without re-invoking render_player_card.
    """
    web_app.clear_card_cache()
    server_id = await core_db.upsert_server(
        identifier="byond://127.0.0.1:25000",
        title="White Dream",
        ip="127.0.0.1",
        port=25000,
    )
    await core_db.update_player_sessions(server_id, {"cachetester"}, now_ts=1000.0)
    await core_db.update_player_sessions(server_id, {"cachetester"}, now_ts=2800.0)

    with patch("web.app.render_player_card", wraps=render_player_card) as spy_render:
        resp1 = await async_client.get("/api/v1/player/cachetester/card.png")
        assert resp1.status_code == 200
        assert spy_render.call_count == 1

        resp2 = await async_client.get("/api/v1/player/CACHE_TESTER/card.png")
        assert resp2.status_code == 200
        assert resp2.content == resp1.content
        assert spy_render.call_count == 1


def test_font_fallback_and_helpers(tmp_path):
    """
    Verifies safe fallback to ImageFont.load_default() when TrueType fonts are absent,
    string truncation to 26 characters, and 404 error card rendering.
    """
    _load_font.cache_clear()
    fallback_font = _load_font(14, bold=False, font_dir=str(tmp_path / "missing_fonts"))
    assert fallback_font is not None

    short_name = "Tau Ceti Classic"
    assert _truncate_text(short_name, 26) == short_name

    long_name = "White Dream 1 - High Roleplay Station Alpha"
    truncated = _truncate_text(long_name, 26)
    assert len(truncated) == 26
    assert truncated.endswith("...")

    err_png = render_not_found_card("unknown_ckey")
    assert err_png.startswith(PNG_SIGNATURE)
    with Image.open(io.BytesIO(err_png)) as img:
        assert img.size == (720, 160)
        assert img.mode == "RGBA"
