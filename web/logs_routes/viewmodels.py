"""View-model formatters and request helpers for the SierraBay12 Log Viewer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlencode

from fastapi import Request

from core import map_renderer
from core.log_parser import (
    CATEGORY_META,
    category_info,
    category_matches_filters,
)

_AUDIT_ACTION_LABELS: Dict[str, str] = {
    "login": "Вход через Discord",
    "login_denied": "Отказ входа (нет доступа)",
    "logout": "Выход",
    "view_round": "Просмотр раунда",
    "search_global": "Глобальный поиск",
    "download_log": "Скачивание лога",
    "upload_round": "Загрузка лога",
    "delete_upload": "Удаление загруженного лога",
    "whitelist_add": "Выдача доступа",
    "whitelist_remove": "Отзыв доступа",
    "refresh": "Обновление списка",
    "reindex": "Переиндексация",
    "rebuild_maps": "Пересборка карты",
}

_AUDIT_ACTION_CSS: Dict[str, str] = {
    "login": "audit-ok",
    "login_denied": "audit-danger",
    "logout": "audit-muted",
    "view_round": "audit-info",
    "search_global": "audit-info",
    "download_log": "audit-warn",
    "upload_round": "audit-ok",
    "delete_upload": "audit-danger",
    "whitelist_add": "audit-warn",
    "whitelist_remove": "audit-danger",
    "refresh": "audit-muted",
    "reindex": "audit-warn",
    "rebuild_maps": "audit-warn",
}

_AUDIT_ACTION_GROUPS: Dict[str, Tuple[str, List[str]]] = {
    "security": ("Безопасность и доступ", ["login_denied", "whitelist_add", "whitelist_remove", "delete_upload"]),
    "auth": ("Входы и сессии", ["login", "login_denied", "logout"]),
    "reads": ("Просмотр и поиск", ["view_round", "search_global", "download_log"]),
    "ops": ("Система и архивы", ["refresh", "reindex", "rebuild_maps", "upload_round"]),
}


def _fmt_ts_utc(epoch: Optional[float]) -> str:
    if not epoch:
        return "—"
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _fmt_ts_iso(epoch: Optional[float]) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fmt_relative_ru(epoch: Optional[float], now: Optional[float] = None) -> str:
    if not epoch:
        return "—"
    ref = float(now if now is not None else time.time())
    diff = max(0, int(ref - float(epoch)))
    if diff < 60:
        return "только что"
    if diff < 3600:
        return f"{diff // 60} мин назад"
    if diff < 86400:
        return f"{diff // 3600} ч назад"
    days = diff // 86400
    if days < 30:
        return f"{days} д назад"
    return f"{days // 30} мес назад"


def _fmt_bytes(num: int) -> str:
    val = float(num or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if val < 1024.0 or unit == "ГБ":
            return f"{val:.1f} {unit}" if unit != "Б" else f"{int(val)} Б"
        val /= 1024.0
    return f"{int(val)} Б"


def _audit_action_label(action: str) -> str:
    return _AUDIT_ACTION_LABELS.get(action, action)


def _audit_action_css(action: str) -> str:
    return _AUDIT_ACTION_CSS.get(action, "audit-muted")


def _parse_audit_details_pairs(details: str) -> List[Dict[str, str]]:
    raw = (details or "").strip()
    if not raw or "=" not in raw:
        return []
    pairs: List[Dict[str, str]] = []
    for part in raw.split(","):
        chunk = part.strip()
        if "=" not in chunk:
            return []
        k, v = chunk.split("=", 1)
        key = k.strip()
        if not key or " " in key:
            return []
        pairs.append({"key": key, "val": v.strip()})
    return pairs


def _build_audit_target_links(action: str, target: str) -> List[Dict[str, str]]:
    t = (target or "").strip()
    if not t:
        return []
    if action in ("view_round", "download_log"):
        if ":" in t:
            ruid, fname = t.split(":", 1)
            return [{"label": t, "href": f"/logs/round/{quote(ruid)}?{urlencode({'file': fname})}"}]
        return [{"label": t, "href": f"/logs/round/{quote(t)}"}]
    if action == "upload_round":
        links = []
        for uid in [u.strip() for u in t.split(",") if u.strip()]:
            links.append({"label": uid, "href": f"/logs/round/{quote(uid)}"})
        return links
    if action == "search_global":
        return [{"label": t, "href": f"/logs/search?{urlencode({'q': t})}"}]
    if action in ("whitelist_add", "whitelist_remove") and t.isdigit():
        return [{"label": t, "href": f"/logs/admin?{urlencode({'audit_user': t})}"}]
    return []


def _enrich_audit_event(
    ev: Dict[str, Any],
    *,
    whitelist_ids: Set[str],
    now_ts: float,
) -> Dict[str, Any]:
    action = str(ev.get("action") or "")
    target = str(ev.get("target") or "")
    details = str(ev.get("details") or "")
    discord_id = str(ev.get("discord_id") or "")
    created_at = ev.get("created_at")

    details_pairs = _parse_audit_details_pairs(details)
    quick_grant_name = str(ev.get("discord_username") or "")
    for p in details_pairs:
        if p["key"] == "display_name" and p["val"]:
            quick_grant_name = p["val"]
            break

    ev["created_at_str"] = _fmt_ts_utc(created_at)
    ev["created_at_iso"] = _fmt_ts_iso(created_at)
    ev["relative_time_str"] = _fmt_relative_ru(created_at, now=now_ts)
    ev["action_label"] = _audit_action_label(action)
    ev["action_css"] = _audit_action_css(action)
    ev["target_links"] = _build_audit_target_links(action, target)
    ev["details_pairs"] = details_pairs
    ev["can_quick_grant"] = (
        action == "login_denied"
        and discord_id.isdigit()
        and 15 <= len(discord_id) <= 22
        and discord_id not in whitelist_ids
    )
    ev["quick_grant_name"] = quick_grant_name[:80]
    return ev


def _coord_vm(coords: Optional[Tuple[int, int, int]], cache_dir: Optional[Path]) -> Optional[Dict[str, Any]]:
    if not coords:
        return None
    x, y, z = int(coords[0]), int(coords[1]), int(coords[2])
    if x < 1 or y < 1 or z < 1:
        return None
    return {
        "x": x,
        "y": y,
        "z": z,
        "z_label": map_renderer.deck_short_label(z),
        "area": map_renderer.lookup_area_name(cache_dir, z, x, y) if cache_dir is not None else "",
    }


def _entry_to_vm(
    entry: Any,
    cache_dir: Optional[Path],
    *,
    focus: Optional[int] = None,
    target_line: Optional[int] = None,
    active_ckey_norm: str = "",
) -> Dict[str, Any]:
    meta = category_info(entry.category)
    visible_ckeys = (
        [ck for ck in entry.ckeys if ck != active_ckey_norm]
        if active_ckey_norm
        else entry.ckeys
    )
    return {
        "line_no": entry.line_no,
        "timestamp": entry.timestamp,
        "time_short": entry.timestamp[11:] if len(entry.timestamp) > 11 else entry.timestamp,
        "category": entry.category,
        "cat_css": meta["css"],
        "safe_html": entry.safe_html,
        "ckeys": entry.ckeys,
        "visible_ckeys": visible_ckeys,
        "coords": _coord_vm(entry.coords, cache_dir),
        "is_continuation": getattr(entry, "is_continuation", False),
        "is_focused": focus is not None and entry.line_no == focus,
        "is_target": target_line is not None and entry.line_no == target_line,
    }


def _parse_round_dt(raw: str) -> Optional[datetime]:
    text = (raw or "").strip()
    if len(text) < 19:
        return None
    try:
        return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _fmt_duration(seconds: int) -> str:
    if seconds < 0:
        return ""
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}ч {minutes:02d}м"
    if minutes > 0:
        return f"{minutes}м"
    return f"{secs}с"


def _round_time_vm(rnd: Dict[str, Any]) -> Dict[str, str]:
    date_label = str(rnd.get("round_date") or "").strip()
    started_raw = str(rnd.get("started_at") or "").strip()
    ended_raw = str(rnd.get("ended_at") or "").strip()

    def _compact(ts: str, base_date: str) -> str:
        if not ts:
            return ""
        head = ts[:19] if len(ts) >= 19 and ts[10:11] == " " and ts[13:14] == ":" else ts
        if base_date and head.startswith(base_date + " "):
            return head[len(base_date) + 1 :]
        return head

    start_short = _compact(started_raw, date_label)
    end_short = _compact(ended_raw, date_label)
    time_range = f"{start_short} – {end_short}" if (start_short and end_short) else (start_short or end_short)

    dt_start = _parse_round_dt(started_raw)
    dt_end = _parse_round_dt(ended_raw)
    duration = _fmt_duration(int((dt_end - dt_start).total_seconds())) if (dt_start and dt_end) else ""

    return {
        "date": date_label,
        "range": time_range,
        "duration": duration,
    }


def _prefers_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "").lower()
    return "application/json" in accept and "text/html" not in accept


def _parse_bool_flag(raw: Optional[str], default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _ordered_categories(
    category_counts: Dict[str, int],
    *,
    preset: str = "all",
    selected_cats: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    priority = list(CATEGORY_META.keys())
    known = [c for c in priority if c in category_counts]
    extra = sorted(c for c in category_counts if c not in CATEGORY_META)
    items = []
    sel_arg = selected_cats or None
    for cat in known + extra:
        meta = category_info(cat)
        items.append(
            {
                "name": cat,
                "count": category_counts[cat],
                "css": meta["css"],
                "desc": meta["desc"],
                "included": category_matches_filters(cat, preset=preset, selected_categories=sel_arg),
            }
        )
    return items
