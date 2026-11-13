"""Discord OAuth2 authentication, signed session cookies, CSRF validation, and whitelist guard."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlencode

from fastapi import HTTPException, Request
import httpx

from core.config import AppConfig
from core import logs_db

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "hilleye_session"
OAUTH_STATE_COOKIE_NAME = "hilleye_oauth_state"

DISCORD_AUTHORIZE_URL = "https://discord.com/api/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/users/@me"

_EPHEMERAL_SECRET: bytes = secrets.token_bytes(32)
_EPHEMERAL_WARNED = False

# Optional hook for unit/integration tests to inject httpx.AsyncBaseTransport
_http_transport_factory: Optional[Callable[[], httpx.AsyncBaseTransport]] = None


@dataclass
class AuthUser:
    discord_id: str
    username: str
    display_name: str
    role: str  # 'viewer' | 'admin' | 'superadmin'
    csrf_token: str

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "superadmin")

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"


def set_discord_http_transport(factory: Optional[Callable[[], httpx.AsyncBaseTransport]]) -> None:
    """Configure an optional custom httpx transport factory (used in automated tests)."""
    global _http_transport_factory
    _http_transport_factory = factory


def _get_signing_key(config: AppConfig) -> bytes:
    global _EPHEMERAL_WARNED
    configured = (config.discord_session_secret or "").strip()
    if configured:
        return hashlib.sha256(configured.encode("utf-8")).digest()
    if not _EPHEMERAL_WARNED:
        _EPHEMERAL_WARNED = True
        logger.warning(
            "discord.session_secret is empty; using an ephemeral per-process key. "
            "Configure discord.session_secret or HILLEYE_SESSION_SECRET to persist sessions across restarts."
        )
    return _EPHEMERAL_SECRET


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(token: str) -> bytes:
    pad = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(token + pad)


def sign_payload(payload: Dict[str, Any], config: AppConfig) -> str:
    body = _b64u_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(_get_signing_key(config), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64u_encode(sig)}"


def verify_payload(token: Optional[str], config: AppConfig) -> Optional[Dict[str, Any]]:
    if not token or "." not in token:
        return None
    try:
        body, sig_b64 = token.split(".", 1)
        expected_sig = hmac.new(_get_signing_key(config), body.encode("ascii"), hashlib.sha256).digest()
        provided_sig = _b64u_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, provided_sig):
            return None
        payload = json.loads(_b64u_decode(body).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        exp = float(payload.get("exp") or 0)
        if exp <= time.time():
            return None
        return payload
    except Exception:
        return None


def sanitize_next_path(next_path: Optional[str]) -> str:
    """Restrict post-login redirects strictly to /logs paths (prevents open redirects)."""
    raw = (next_path or "").strip()
    if not raw or not raw.startswith("/logs") or raw.startswith("//") or "\\" in raw:
        return "/logs"
    return raw


def build_discord_authorize_url(config: AppConfig, state_token: str) -> str:
    params = {
        "client_id": config.discord_client_id.strip(),
        "redirect_uri": config.discord_redirect_uri.strip(),
        "response_type": "code",
        "scope": "identify",
        "state": state_token,
        "prompt": "consent",
    }
    return f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}"


def create_oauth_state_cookie(config: AppConfig, next_path: str = "/logs") -> Tuple[str, str]:
    """Return (raw_state_param, signed_state_cookie_value)."""
    nonce = secrets.token_urlsafe(24)
    now = time.time()
    cookie_val = sign_payload(
        {
            "state": nonce,
            "next": sanitize_next_path(next_path),
            "exp": now + 600,
        },
        config,
    )
    return nonce, cookie_val


def verify_oauth_state(
    state_param: Optional[str],
    state_cookie: Optional[str],
    config: AppConfig,
) -> Optional[str]:
    """Verify OAuth2 state token against signed cookie and return sanitized next_path if valid."""
    if not state_param:
        return None
    payload = verify_payload(state_cookie, config)
    if not payload:
        return None
    expected = str(payload.get("state") or "")
    if not expected or not hmac.compare_digest(expected, state_param):
        return None
    return sanitize_next_path(payload.get("next"))


async def exchange_discord_code(code: str, config: AppConfig) -> Dict[str, Any]:
    """Exchange OAuth2 authorization code for Discord user profile using /users/@me."""
    transport = _http_transport_factory() if _http_transport_factory else None
    client_kwargs: Dict[str, Any] = {"timeout": 10.0}
    if transport is not None:
        client_kwargs["transport"] = transport

    async with httpx.AsyncClient(**client_kwargs) as client:
        token_resp = await client.post(
            DISCORD_TOKEN_URL,
            data={
                "client_id": config.discord_client_id.strip(),
                "client_secret": config.discord_client_secret.strip(),
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.discord_redirect_uri.strip(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            raise ValueError(f"Discord token exchange failed (HTTP {token_resp.status_code})")
        token_data = token_resp.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError("Discord token response missing access_token")

        user_resp = await client.get(
            DISCORD_ME_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_resp.status_code != 200:
            raise ValueError(f"Discord user lookup failed (HTTP {user_resp.status_code})")
        profile = user_resp.json()
        uid = str(profile.get("id") or "").strip()
        if not uid.isdigit():
            raise ValueError("Discord profile missing valid numeric id")

        username = str(profile.get("username") or uid).strip()
        global_name = str(profile.get("global_name") or username).strip()
        return {
            "discord_id": uid,
            "username": username,
            "display_name": global_name,
        }


def create_session_cookie(profile: Dict[str, Any], config: AppConfig) -> str:
    now = time.time()
    payload = {
        "uid": profile["discord_id"],
        "username": profile["username"],
        "display_name": profile["display_name"],
        "csrf": secrets.token_urlsafe(24),
        "iat": now,
        "exp": now + int(config.discord_session_ttl_seconds),
    }
    return sign_payload(payload, config)


def read_session_cookie(request: Request, config: AppConfig) -> Optional[Dict[str, Any]]:
    raw = request.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_payload(raw, config)
    if not payload:
        return None
    uid = str(payload.get("uid") or "").strip()
    if not uid.isdigit():
        return None
    return payload


async def resolve_auth_state(
    request: Request,
    config: AppConfig,
) -> Tuple[str, Optional[Dict[str, Any]], Optional[AuthUser]]:
    """
    Resolve authentication & whitelist status for the current request.
    Returns (status, raw_session_payload, AuthUser_or_None)
    where status is one of: 'unconfigured', 'unauthenticated', 'forbidden', 'ok'.
    """
    session = read_session_cookie(request, config)
    if not config.discord_oauth_configured() and session is None:
        return "unconfigured", None, None
    if session is None:
        return "unauthenticated", None, None

    uid = str(session["uid"])
    allowed, role, db_entry = await logs_db.check_whitelist_access(uid, config.discord_superadmin_ids)
    if not allowed:
        return "forbidden", session, None

    display = str(session.get("display_name") or session.get("username") or uid)
    if db_entry and db_entry.get("display_name") and role != "superadmin":
        display = str(session.get("display_name") or db_entry["display_name"])

    user = AuthUser(
        discord_id=uid,
        username=str(session.get("username") or uid),
        display_name=display,
        role=role,
        csrf_token=str(session.get("csrf") or ""),
    )
    return "ok", session, user


def verify_csrf(user: AuthUser, provided_token: Optional[str]) -> None:
    """Validate CSRF token against session token in constant time."""
    if not user.csrf_token or not provided_token or not hmac.compare_digest(user.csrf_token, provided_token.strip()):
        raise HTTPException(status_code=403, detail="Недействительный CSRF-токен. Обновите страницу.")


def get_client_ip(request: Request) -> str:
    """Extract client IP address from proxy headers or socket peer."""
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",")[0].strip()[:64]
    xri = request.headers.get("x-real-ip", "").strip()
    if xri:
        return xri[:64]
    if request.client and request.client.host:
        return str(request.client.host)[:64]
    return ""
