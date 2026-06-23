#!/usr/bin/env python3
"""Host-side sidecar that ships a graceful Xray reload signal into the
3x-ui container.

Why a sidecar
-------------
The bot runs inside Docker as uid 1000, with no access to the host's
``docker.sock``. When the bot writes a new client into x-ui's SQLite
the Xray process still has the old config in memory — until something
restarts it. The cheap, zero-downtime restart is ``kill -USR1 1``
inside the 3x-ui container (PID 1 = /app/x-ui, which respawns its
xray child with the fresh SQLite-backed config); that needs docker
access the bot doesn't have.

This sidecar runs on the host as root (or any uid in the docker
group), listens on 127.0.0.1:7079, and forwards POST /reload-xray
into ``docker kill -s USR1 <container>``. Same pattern as
kimi-bridge. Auth via a static shared token (``XRAY_RELOAD_TOKEN``).

Endpoints
---------
- ``POST /reload-xray``          → send SIGUSR1 to the container.
- ``GET  /health``               → liveness + last reload timestamp.
"""

import json
import logging
import os
import re
import subprocess
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = logging.getLogger("xray-reload")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)

CONTAINER = os.environ.get("XRAY_CONTAINER", "3x-ui")
TOKEN = os.environ.get("XRAY_RELOAD_TOKEN", "")
PORT = int(os.environ.get("XRAY_RELOAD_PORT", "7079"))
# Entry node — sidecar SSHes there for `ss -tin` to extract per-IP
# round-trip times from live :443 sockets. Key must already be
# installed (`/root/.ssh/entry_node_kimi`, alias `entry-node` in
# /root/.ssh/config — both done during the kimi setup).
ENTRY_HOST = os.environ.get("ENTRY_NODE_SSH_HOST", "entry-node")
ENTRY_SSH_KEY = os.environ.get("ENTRY_NODE_SSH_KEY", "/root/.ssh/entry_node_kimi")
TCP_STATS_TIMEOUT = float(os.environ.get("XRAY_RELOAD_TCP_STATS_TIMEOUT", "8"))
# Cache the parsed RTT-per-IP map for this many seconds — the bot
# polls /tcp-stats once per dashboard load, but a refreshing user
# can hit it back-to-back. SSH adds ~150ms latency we don't want
# to pay every time.
TCP_STATS_CACHE_S = float(os.environ.get("XRAY_RELOAD_TCP_STATS_CACHE", "10"))
# Default bind target is the docker0 bridge IP — only containers on
# the same host (and the host itself) can reach us. NOT the public
# interface. Override with XRAY_RELOAD_BIND if needed (e.g. "0.0.0.0"
# in dev / tests).
BIND = os.environ.get("XRAY_RELOAD_BIND", "172.17.0.1")
# Hard cap so a malfunctioning bot can't reload-storm the panel and
# disrupt the user-facing Xray. The token-bucket is one reload per
# COOLDOWN_S seconds; bursts queue up to BURST_SIZE.
COOLDOWN_S = float(os.environ.get("XRAY_RELOAD_COOLDOWN", "2.0"))

_last_reload_ts = 0.0
_last_reload_ok = None  # tri-state: None never, True ok, False failed

# /tcp-stats cache
_tcp_stats_cache: dict = {}     # {ip: avg_rtt_ms}
_tcp_stats_cache_ts: float = 0.0
_tcp_stats_cache_err: str = ""


# `ss -tin state established` strips the State column entirely, so
# socket rows start with the Recv-Q/Send-Q integers. Detail rows
# start with whitespace (a tab). Sample:
#   0      0      <entry-ip>:443       <client-ip>:52754
#   <tab>  cubic ... rtt:88.665/18.733 ato:40 mss:1348 ...
# Captures: local_ip, local_port, peer_ip, peer_port. Only rows
# where local_port == "443" are user-facing (HAProxy listens there);
# rows with peer_port == "443" are the haproxy → exit backend side
# and we skip those.
_ESTAB_RE = re.compile(
    r"^\d+\s+\d+\s+"
    r"([0-9a-fA-F:.]+):(\d+)\s+"
    r"([0-9a-fA-F:.]+):(\d+)"
)
_RTT_RE = re.compile(r"\brtt:([0-9.]+)/")


def _fetch_tcp_stats() -> tuple[dict, str]:
    """Run `ss -tin` on the entry over SSH and parse per-IP avg RTT.

    Returns ``({ip: round(avg_rtt_ms, 1)}, '')`` on success, or
    ``({}, error_message)`` on failure. Output of ``ss -tin '( sport = :443 )'
    state established`` looks like::

        State  Recv-Q Send-Q  Local Address:Port  Peer Address:Port  Process
        ESTAB  0      0       <entry>:443          <user>:<port>      …
                 cubic wscale:7,7 rto:212 rtt:10.456/3.234 ato:40 mss:1460 …
        ESTAB  0      0       …

    Each ESTAB row is followed by an indented detail row with ``rtt:X.X/Y.Y``.
    We pair them up and average all RTTs per peer IP. Multiple TCP streams
    per user are common (browser tabs); averaging gives a stable per-client
    RTT.
    """
    # We pass the simplest possible remote command (no shell-quoting
    # hell) — `ss -tin state established` — and filter for sport=:443
    # ourselves below from the ESTAB lines. That sidesteps the
    # ambiguous parens-quoting that bit us across the ssh layer.
    cmd = [
        "ssh",
        "-i", ENTRY_SSH_KEY,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        ENTRY_HOST,
        "ss -tin state established",
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TCP_STATS_TIMEOUT,
        )
    except FileNotFoundError:
        return {}, "ssh binary missing"
    except subprocess.TimeoutExpired:
        return {}, "ssh timed out"
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"

    if r.returncode != 0:
        return {}, (r.stderr.strip() or f"ssh exit {r.returncode}")[:200]

    rtts: dict = defaultdict(list)
    current_ip = None
    for line in r.stdout.splitlines():
        # Detail rows are indented (tab/space); socket rows are not.
        if line and line[0].isspace():
            if current_ip:
                mr = _RTT_RE.search(line)
                if mr:
                    try:
                        rtts[current_ip].append(float(mr.group(1)))
                    except ValueError:
                        pass
                current_ip = None
            continue
        # Non-indented row: socket row, OR the header. _ESTAB_RE only
        # matches "<int> <int> <ip:port> <ip:port>" so the header
        # (which starts "Recv-Q") naturally falls through.
        m = _ESTAB_RE.match(line)
        current_ip = None
        if m:
            local_port = m.group(2)
            peer_ip = m.group(3)
            if local_port == "443":
                current_ip = peer_ip

    out: dict = {}
    for ip, samples in rtts.items():
        if samples:
            out[ip] = round(sum(samples) / len(samples), 1)
    return out, ""


def _reload_xray() -> tuple[bool, str]:
    """Send SIGUSR1 to the container's PID 1. Returns (ok, stderr)."""
    try:
        result = subprocess.run(
            ["docker", "kill", "-s", "USR1", CONTAINER],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return False, "docker binary missing"
    except subprocess.TimeoutExpired:
        return False, "docker kill timed out"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return result.returncode == 0, result.stderr.strip()[:200]


class Handler(BaseHTTPRequestHandler):
    server_version = "xray-reload/0.1"

    def _reply(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _check_auth(self) -> bool:
        if not TOKEN:
            return True
        return self.headers.get("X-Token") == TOKEN

    def log_message(self, fmt: str, *args) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        global _tcp_stats_cache, _tcp_stats_cache_ts, _tcp_stats_cache_err
        if self.path == "/health":
            self._reply(200, {
                "status": "ok",
                "container": CONTAINER,
                "last_reload_ts": _last_reload_ts,
                "last_reload_ok": _last_reload_ok,
                "tcp_stats_cache_age_s": round(
                    time.time() - _tcp_stats_cache_ts, 2) if _tcp_stats_cache_ts else None,
            })
            return
        if self.path == "/tcp-stats":
            if not self._check_auth():
                self._reply(401, {"error": "bad token"})
                return
            now = time.time()
            if now - _tcp_stats_cache_ts < TCP_STATS_CACHE_S and _tcp_stats_cache:
                self._reply(200, {
                    "stats": _tcp_stats_cache,
                    "cached": True,
                    "age_s": round(now - _tcp_stats_cache_ts, 2),
                    "error": _tcp_stats_cache_err or None,
                })
                return
            stats, err = _fetch_tcp_stats()
            if stats or not err:
                _tcp_stats_cache = stats
                _tcp_stats_cache_ts = now
                _tcp_stats_cache_err = ""
                self._reply(200, {"stats": stats, "cached": False, "age_s": 0})
            else:
                # Cache the failure too so we don't hammer SSH on
                # repeated dashboard polls if the entry is down.
                _tcp_stats_cache_ts = now
                _tcp_stats_cache_err = err
                LOG.warning("tcp_stats failed: %s", err)
                self._reply(200, {"stats": {}, "cached": False, "error": err})
            return
        self._reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        global _last_reload_ts, _last_reload_ok
        if self.path != "/reload-xray":
            self._reply(404, {"error": "not found"})
            return
        if not self._check_auth():
            self._reply(401, {"error": "bad token"})
            return

        now = time.time()
        wait = (_last_reload_ts + COOLDOWN_S) - now
        if wait > 0:
            self._reply(429, {
                "error": "cooling down",
                "retry_after_sec": round(wait, 2),
            })
            return

        ok, stderr = _reload_xray()
        _last_reload_ts = now
        _last_reload_ok = ok
        if ok:
            LOG.info("reload OK (container=%s)", CONTAINER)
            self._reply(200, {"ok": True})
        else:
            LOG.warning("reload FAILED: %s", stderr)
            self._reply(502, {"ok": False, "error": stderr})


def main() -> None:
    if not TOKEN:
        LOG.warning(
            "XRAY_RELOAD_TOKEN not set — anyone reaching :%d can reload Xray. "
            "Set it via systemd EnvironmentFile.",
            PORT,
        )
    LOG.info("xray-reload starting on %s:%d, container=%s", BIND, PORT, CONTAINER)
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("interrupted")


if __name__ == "__main__":
    main()
