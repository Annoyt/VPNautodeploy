"""Xray reload — talks to the host-side ``xray-reload`` sidecar.

Why the sidecar
---------------
The bot runs inside Docker as uid 1000 with no access to the host's
docker socket. When ``add_client_sync`` / ``remove_client_sync`` /
``sync_client_settings_sync`` write the new client list straight into
``x-ui.db``, Xray still has the OLD config loaded in memory — until
*something* nudges it. The cheap, zero-downtime nudge is
``kill -USR1 1`` inside the 3x-ui container: PID 1 is /app/x-ui, which
respawns its xray child with the fresh SQLite-backed config and keeps
:443 reachable throughout.

We can't do that from the bot container, so a tiny HTTP service runs
on the host (see ``scripts/xray_reload.py`` + ``systemd/xray-reload.service``)
and the bot POSTs to it. Token-authenticated, rate-limited.

Configuration
-------------
``XRAY_RELOAD_URL``  — full URL, usually ``http://host.docker.internal:7079``
                       (the gateway address is wired in
                       ``docker-compose.yml`` via ``extra_hosts``).
                       Empty → reload becomes a no-op (debug-only mode).
``XRAY_RELOAD_TOKEN`` — shared secret, matches what the sidecar reads
                       from ``/etc/xray-reload.env``.

Returns
-------
``True`` if the sidecar reports success (HTTP 200, ``ok:true``).
``False`` for any other case — bot logs a warning and continues; the
SQLite write is already done so the next reload (manual, or the next
auto-reload after another write) will pick it up.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT_S = 12  # sidecar's docker kill itself caps at 10s


def _config() -> tuple[str, str]:
    """Read URL + token from env. Cheap to call per request."""
    url = (os.environ.get("XRAY_RELOAD_URL") or "").strip()
    token = (os.environ.get("XRAY_RELOAD_TOKEN") or "").strip()
    return url, token


def reload_xray() -> bool:
    """Trigger a soft Xray reload via the host-side sidecar.

    Returns:
        True on confirmed success; False on any failure (logged).
    """
    url, token = _config()
    if not url:
        logger.debug("reload_xray: XRAY_RELOAD_URL not set, skipping")
        return False

    headers = {"X-Token": token} if token else {}
    try:
        r = requests.post(
            f"{url.rstrip('/')}/reload-xray",
            headers=headers,
            timeout=_DEFAULT_TIMEOUT_S,
        )
    except requests.RequestException as e:
        logger.warning(f"reload_xray: HTTP call failed: {e}")
        return False

    if r.status_code == 200:
        logger.info("reload_xray: sidecar reports SIGUSR1 delivered to 3x-ui")
        return True
    if r.status_code == 429:
        # Sidecar's cooldown — last reload happened <COOLDOWN_S ago.
        # Treat as success: the previous nudge already covered our write
        # because the client write happens BEFORE we call here.
        try:
            wait = (r.json() or {}).get("retry_after_sec")
        except Exception:
            wait = "?"
        logger.info(f"reload_xray: sidecar cooling down ({wait}s), prior reload covers this write")
        return True

    body = ""
    try:
        body = (r.json() or {}).get("error", "")
    except Exception:
        body = r.text[:120]
    logger.warning(f"reload_xray: sidecar returned {r.status_code}: {body}")
    return False


def reload_xray_health() -> Optional[dict]:
    """Best-effort GET /health on the sidecar. Used by /status command."""
    url, token = _config()
    if not url:
        return None
    try:
        r = requests.get(
            f"{url.rstrip('/')}/health",
            headers={"X-Token": token} if token else {},
            timeout=4,
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_tcp_stats() -> dict:
    """Best-effort GET /tcp-stats on the sidecar.

    Returns ``{ip: avg_rtt_ms}``. Empty dict on any failure — caller
    treats that as "RTT unavailable" and renders '—' for the column.
    """
    url, token = _config()
    if not url:
        return {}
    try:
        r = requests.get(
            f"{url.rstrip('/')}/tcp-stats",
            headers={"X-Token": token} if token else {},
            timeout=10,
        )
        if r.status_code == 200:
            body = r.json() or {}
            return body.get("stats") or {}
    except Exception as e:
        logger.debug(f"get_tcp_stats failed: {e}")
    return {}
