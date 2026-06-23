"""HMAC-signed admin token for dashboard URLs outside Telegram WebApp.

The dashboard's primary auth is Telegram Mini App initData, which only
works when opened via a `web_app` inline button — and those are 1:1-chat
only. In a forum group, `/admin` falls back to a regular `url` button
that opens the page in the user's external browser without any
initData, so all admin endpoints would return 401.

A short-lived signed token in the URL lets us keep the dashboard
admin-only without dragging in a session cookie / login page. Lifetime
1h: re-run `/admin` to get a fresh one.

Token format (base64url-encoded for URL safety):
    admin_id:expiry:hmac
where hmac = HMAC-SHA256(BOT_TOKEN, f"{admin_id}:{expiry}").hexdigest()
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional


DEFAULT_TTL_SECONDS = 3600  # 1 hour (reduced from 24h for security)


def make_admin_token(
    bot_token: str,
    admin_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    expiry = int(time.time()) + ttl_seconds
    payload = f"{admin_id}:{expiry}"
    mac = hmac.new(
        bot_token.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    raw = f"{payload}:{mac}".encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def verify_admin_token(bot_token: str, token: str) -> Optional[str]:
    """Return the admin_id encoded in the token if valid, else None.

    Validates: base64 decodes, format admin_id:expiry:mac, expiry not
    in the past, and HMAC matches under BOT_TOKEN.
    """
    if not token or not bot_token:
        return None
    try:
        pad = "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode((token + pad).encode()).decode()
        admin_id, expiry_str, signed_mac = raw.rsplit(":", 2)
    except Exception:
        return None

    try:
        expiry = int(expiry_str)
    except ValueError:
        return None
    if expiry < int(time.time()):
        return None

    payload = f"{admin_id}:{expiry_str}"
    expected_mac = hmac.new(
        bot_token.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_mac, signed_mac):
        return None

    return admin_id
