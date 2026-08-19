#!/usr/bin/env python3
"""Hysteria2 → 3x-ui traffic bridge (runs on the EXIT host).

Hysteria2 runs beside Xray and its traffic never crosses the panel's
stats pipeline, so hy2 bytes were invisible to per-client quotas. This
daemon closes that gap:

  1. Polls hysteria's Traffic Stats API (``/traffic?clear=1``) — the
     ids are the same synthetic emails the bot's /api/hy2/auth returns,
     which are also the client_traffics keys in x-ui.db.
  2. Adds the deltas straight into ``client_traffics.up/down`` with an
     atomic ``up = up + ?`` update, so the panel's own depletion job
     enforces the shared quota across xray *and* hy2.
  3. Kicks live hy2 connections whose client is disabled, expired or
     over quota (``POST /kick``). The bot's auth callback is the gate
     for *new* connections; this covers the ones already connected.

Deltas that fail to persist (panel holding a write lock, etc.) are kept
in memory and merged into the next round — ``clear=1`` means hysteria
forgets them the moment we fetch.

Environment:
    HY2_STATS_URL     Traffic Stats API base (default http://127.0.0.1:9977)
    HY2_STATS_SECRET  Secret from hysteria's trafficStats.secret (required)
    XUI_DB_PATH       x-ui.db path (default: the 3x-ui docker volume)
    INTERVAL_SEC      Poll interval, seconds (default 60)
"""

import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

STATS_URL = os.getenv("HY2_STATS_URL", "http://127.0.0.1:9977").rstrip("/")
SECRET = os.getenv("HY2_STATS_SECRET", "")
XUI_DB_PATH = os.getenv(
    "XUI_DB_PATH",
    "/var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db",
)
INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "60"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("hy2-collector")


def _api(path: str, payload=None):
    """Call the hysteria Traffic Stats API. Returns parsed JSON or None."""
    req = urllib.request.Request(
        f"{STATS_URL}{path}",
        headers={"Authorization": SECRET, "Content-Type": "application/json"},
        data=json.dumps(payload).encode() if payload is not None else None,
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            return json.loads(body) if body.strip() else {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning(f"hysteria API {path} failed: {e}")
        return None


def fetch_deltas() -> dict:
    """Fetch and clear per-user counters: {email: {'tx': n, 'rx': n}}."""
    return _api("/traffic?clear=1")


def fetch_online() -> dict:
    """Currently connected ids: {email: connection_count}."""
    return _api("/online") or {}


def apply_deltas(pending: dict) -> dict:
    """Add deltas into client_traffics. Returns what could NOT be applied.

    Panel semantics: ``down`` is what the user downloaded (hysteria tx,
    server→client), ``up`` is what the user sent (hysteria rx).
    """
    if not pending:
        return {}
    try:
        conn = sqlite3.connect(XUI_DB_PATH, timeout=15)
        conn.execute("PRAGMA busy_timeout = 10000")
    except sqlite3.Error as e:
        logger.warning(f"cannot open x-ui.db: {e}")
        return pending

    leftover = {}
    try:
        with conn:
            for email, t in pending.items():
                tx, rx = int(t.get("tx", 0)), int(t.get("rx", 0))
                if tx == 0 and rx == 0:
                    continue
                cur = conn.execute(
                    "UPDATE client_traffics SET up = up + ?, down = down + ? "
                    "WHERE email = ?",
                    (rx, tx, email),
                )
                if cur.rowcount == 0:
                    # No accounting row → cannot meter this id at all.
                    logger.warning(f"unknown hy2 id (no client_traffics row): {email}")
    except sqlite3.Error as e:
        logger.warning(f"x-ui.db write failed, keeping deltas for next round: {e}")
        leftover = pending
    finally:
        conn.close()
    return leftover


def find_kickable(online_ids: list) -> list:
    """Of the online ids, return those that must be disconnected."""
    if not online_ids:
        return []
    now_ms = int(time.time() * 1000)
    try:
        conn = sqlite3.connect(f"file:{XUI_DB_PATH}?mode=ro", uri=True, timeout=15)
        conn.execute("PRAGMA busy_timeout = 10000")
    except sqlite3.Error as e:
        logger.warning(f"cannot open x-ui.db read-only: {e}")
        return []
    try:
        placeholders = ",".join("?" * len(online_ids))
        rows = conn.execute(
            f"SELECT email, enable, up, down, total, expiry_time "
            f"FROM client_traffics WHERE email IN ({placeholders})",
            online_ids,
        ).fetchall()
    except sqlite3.Error as e:
        logger.warning(f"x-ui.db read failed: {e}")
        return []
    finally:
        conn.close()

    known = {r[0] for r in rows}
    # Ids without an accounting row can't be metered — kick them too.
    kick = [i for i in online_ids if i not in known]
    for email, enable, up, down, total, expiry in rows:
        over_quota = total and total > 0 and (up + down) >= total
        expired = expiry and expiry > 0 and expiry < now_ms
        if not enable or over_quota or expired:
            kick.append(email)
    return kick


def main():
    if not SECRET:
        logger.error("HY2_STATS_SECRET is not set")
        sys.exit(1)

    pending: dict = {}
    logger.info(
        f"starting: stats={STATS_URL} db={XUI_DB_PATH} interval={INTERVAL_SEC}s"
    )
    while True:
        fresh = fetch_deltas()
        if fresh:
            for email, t in fresh.items():
                acc = pending.setdefault(email, {"tx": 0, "rx": 0})
                acc["tx"] += int(t.get("tx", 0))
                acc["rx"] += int(t.get("rx", 0))
        if pending:
            applied = len(pending)
            pending = apply_deltas(pending)
            if not pending:
                logger.info(f"applied hy2 deltas for {applied} user(s)")

        online = fetch_online()
        kick = find_kickable(list(online.keys()))
        if kick:
            if _api("/kick", kick) is not None:
                logger.info(f"kicked {len(kick)} over-quota/disabled hy2 user(s): {kick}")

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
