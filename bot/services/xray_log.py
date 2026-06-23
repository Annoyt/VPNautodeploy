"""Parse Xray's access log to surface per-client activity in the dashboard.

Topology
--------
Since 2026-06-03 the entry node runs HAProxy with ``send-proxy-v2``
on :443, and the exit-side Xray's VLESS inbound has
``acceptProxyProtocol: true`` on its tcpSettings. That preserves the
real ``src_ip`` end-to-end, so this parser now extracts genuine
per-client source IPs — making both per-IP counts and 3x-ui's own
``limitIp`` enforcement actually useful again.

What we surface
---------------
- ``distinct_ips`` per email — count of unique source IPs in the
  last ``window_seconds`` (default 60). With PROXY-protocol live
  this is the real "how many devices are using this key right now"
  metric the operator wants.
- ``active_connections`` — distinct ``src_ip:src_port`` pairs in the
  window. Proxy for live TCP streams (a single browser tab opens
  many).
- ``distinct_destinations`` — distinct ``dest_host:dest_port`` pairs.
  Proxy for "how active is this user" (one stream per loaded asset).
- ``last_seen`` — most recent log line timestamp for this email.
- ``ips`` — the actual list of IPs seen (top 10), useful for the
  detail modal.

Log format (current Xray release)
---------------------------------
::

    2026/06/03 21:21:27.560889 from 91.246.101.216:38448 accepted \\
        tcp:example.com:443 [inbound-443 >> direct] email: user_X@…

We tail the last ~50 KB by default (configurable), parse what we can,
ignore unparseable lines (rare warning rotations). Read is best-effort:
no log → empty dict → dashboard shows '—'.
"""

import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# Default location — shared mount between 3x-ui (writes) and the bot
# (reads) via the vpn-bot_3xui-data volume.
DEFAULT_LOG_PATH = "/var/lib/docker/volumes/vpn-bot_3xui-data/_data/access.log"
# Read tail size (bytes). The log can grow fast; ~512 KB is enough for
# a 60s window even on a chatty server but still cheap to scan.
DEFAULT_TAIL_BYTES = 512 * 1024


# Group 1: timestamp "YYYY/MM/DD HH:MM:SS.ffffff"
# Group 2: src ip:port
# Group 3: dest host:port (or ip:port)
# Group 4: inbound tag — the chunk before ">>" inside [..], e.g.
#          "inbound-8443" or "vmess-ws-2053". Drives per-protocol
#          aggregation in dpi_metrics so the dashboard heatmap can
#          show ASN×inbound success/fail rates separately.
# Group 5: email after "email: "
_LINE_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"from\s+(\S+)\s+accepted\s+\S+:(\S+)\s+"
    r"\[(\S+)\s*>>\s*[^\]]+\]\s+email:\s+(\S+)"
)


def _tail_bytes(path: str, n: int) -> str:
    """Read the last ``n`` bytes of a file. Returns '' on error."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > n:
                f.seek(-n, os.SEEK_END)
            data = f.read()
    except OSError as e:
        logger.warning(f"xray_log: read failed {path}: {e}")
        return ""
    try:
        return data.decode('utf-8', errors='replace')
    except Exception:
        return ""


def _parse_ts(s: str) -> Optional[datetime]:
    try:
        return datetime.strptime(s, "%Y/%m/%d %H:%M:%S.%f")
    except ValueError:
        return None


def summarize_activity(
    path: str = DEFAULT_LOG_PATH,
    window_seconds: int = 60,
    tail_bytes: int = DEFAULT_TAIL_BYTES,
) -> Dict[str, dict]:
    """Return per-email activity summary for the last ``window_seconds``.

    Output shape:
        {
          "user_X@example": {
              "active_connections": 7,    # distinct src_ip:src_port pairs
              "distinct_destinations": 5, # distinct dest_host:dest_port
              "last_seen": "2026-06-03 21:21:43",  # ISO, no millis
          },
          ...
        }

    Empty dict if the log isn't present or unreadable — callers should
    treat that as "data unavailable", not as "no activity".
    """
    text = _tail_bytes(path, tail_bytes)
    if not text:
        return {}

    # First pass: find the latest timestamp in the file. We use that as
    # the window's right edge (instead of wall-clock now()) because the
    # bot's container clock can drift from the 3x-ui container clock,
    # and we want to count "the most recent <window>s of activity",
    # not "the last <window>s of wall time".
    latest: Optional[datetime] = None
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            ts = _parse_ts(m.group(1))
            if ts and (latest is None or ts > latest):
                latest = ts
    if latest is None:
        return {}
    cutoff = latest - timedelta(seconds=window_seconds)

    per_email: Dict[str, dict] = defaultdict(
        lambda: {
            "ips": set(),
            "active_connections": set(),
            "distinct_destinations": set(),
            "last_seen": None,
        }
    )
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        if not ts or ts < cutoff:
            continue
        src = m.group(2)
        # src is "ip:port"; the IP is the part before the LAST colon
        # (IPv6 has colons, IPv4 does not — rsplit covers both).
        src_ip = src.rsplit(":", 1)[0]
        dest = m.group(3)
        # group(4) is the inbound tag — irrelevant for per-email rollup
        email = m.group(5)
        rec = per_email[email]
        rec["ips"].add(src_ip)
        rec["active_connections"].add(src)
        rec["distinct_destinations"].add(dest)
        if rec["last_seen"] is None or ts > rec["last_seen"]:
            rec["last_seen"] = ts

    out: Dict[str, dict] = {}
    for email, rec in per_email.items():
        out[email] = {
            "distinct_ips": len(rec["ips"]),
            "ips": sorted(rec["ips"])[:10],
            "active_connections": len(rec["active_connections"]),
            "distinct_destinations": len(rec["distinct_destinations"]),
            "last_seen": rec["last_seen"].strftime("%Y-%m-%d %H:%M:%S")
                if rec["last_seen"] else None,
        }
    return out


# ---- DPI metrics rollup ----

# Reconnect threshold: if a single src_ip connects more than this
# many times within the window, we treat each such IP as showing a
# "short session" symptom (DPI cutting and the client retrying).
DPI_RECONNECT_THRESHOLD = 3


def summarize_dpi(
    path: str = DEFAULT_LOG_PATH,
    window_seconds: int = 300,
    tail_bytes: int = 2 * 1024 * 1024,
    geoip_lookup=None,
    asn_lookup=None,
) -> Dict[tuple, dict]:
    """Roll up the last ``window_seconds`` of access.log by
    ``(country, ASN, inbound_tag)``.

    The inbound_tag axis was added with Phase B of the dpi-metrics
    rollout — before, every row was attributed to the Reality inbound.
    Now ws/xhttp/ss-2022 inbounds get their own buckets so the
    dashboard heatmap can show per-protocol success/fail per ASN
    instead of one aggregate.

    ``geoip_lookup`` and ``asn_lookup`` are callables ``ip -> (cc, ...)``
    and ``ip -> (asn, org)``; injected by the caller so tests can stub
    them out and so this module stays import-cycle-free with geoip.py.

    Returns a dict keyed by ``(country, asn, inbound_tag)`` (any of
    them may be ``None`` for unmapped IPs / pre-tag log lines) with:

        {
          ("RU", "AS8402", "inbound-8443"): {
              "as_org": "Corbina",
              "conn_count": 142,
              "unique_ips": 31,
              "short_session_count": 4,
              "avg_conns_per_ip": 4.58,
          },
          ...
        }
    """
    text = _tail_bytes(path, tail_bytes)
    if not text:
        return {}

    latest: Optional[datetime] = None
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            ts = _parse_ts(m.group(1))
            if ts and (latest is None or ts > latest):
                latest = ts
    if latest is None:
        return {}
    cutoff = latest - timedelta(seconds=window_seconds)

    # Per (country, asn, inbound_tag): collect src_ip → connect_count
    per_bucket: Dict[tuple, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    per_bucket_org: Dict[tuple, str] = {}

    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        if not ts or ts < cutoff:
            continue
        src_ip = m.group(2).rsplit(":", 1)[0]
        inbound_tag = m.group(4) or None
        cc = None
        asn = None
        org = ""
        if geoip_lookup is not None:
            try:
                geo = geoip_lookup(src_ip)
                if geo:
                    cc = geo[0]
            except Exception:
                pass
        if asn_lookup is not None:
            try:
                a = asn_lookup(src_ip)
                if a:
                    asn = a[0]
                    org = a[1] if len(a) > 1 else ""
            except Exception:
                pass
        bucket = (cc, asn, inbound_tag)
        per_bucket[bucket][src_ip] += 1
        if org and bucket not in per_bucket_org:
            per_bucket_org[bucket] = org

    out: Dict[tuple, dict] = {}
    for bucket, ip_counts in per_bucket.items():
        conn_count = sum(ip_counts.values())
        unique_ips = len(ip_counts)
        short_sessions = sum(
            1 for c in ip_counts.values() if c > DPI_RECONNECT_THRESHOLD
        )
        out[bucket] = {
            "as_org": per_bucket_org.get(bucket, ""),
            "conn_count": conn_count,
            "unique_ips": unique_ips,
            "short_session_count": short_sessions,
            "avg_conns_per_ip": round(conn_count / unique_ips, 2) if unique_ips else 0.0,
        }
    return out


# ---- error.log → handshake failure parsing ----

DEFAULT_ERROR_LOG_PATH = "/var/lib/docker/volumes/vpn-bot_3xui-data/_data/error.log"

# Format observed on Xray 26.x:
#   2026/06/06 21:02:18.191717 [Info] transport/internet/tcp:
#       REALITY: processed invalid connection from <IP>:<PORT>: <REASON>
# We capture (timestamp, ip, reason) — port discarded as it's ephemeral.
_REALITY_FAIL_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"\[(?:Info|Warning)\]\s+transport/internet/tcp:\s+"
    r"REALITY:\s+processed\s+invalid\s+connection\s+from\s+"
    r"([0-9a-fA-F:.]+):\d+:\s+(.+?)\s*$"
)


# Canonical buckets for the noisy free-form reason text. Lets us
# distinguish "actual probing" from "just a buggy old client".
def _classify_reason(reason: str) -> str:
    r = reason.lower()
    if "unsupported tls" in r or "tls version" in r:
        return "tls_version"          # ancient client OR DPI emulating
    if "failed to read client hello" in r or "client hello" in r:
        return "no_hello"             # bare TCP probe, no TLS — strong DPI signal
    if "server name mismatch" in r or "sni" in r:
        return "sni_mismatch"         # wrong SNI — probe trying wrong domain
    if "auth" in r or "key" in r or "short id" in r:
        return "auth_fail"            # wrong Reality keys — client mis-config
    return "other"


def summarize_handshake_failures(
    path: str = DEFAULT_ERROR_LOG_PATH,
    window_seconds: int = 300,
    tail_bytes: int = 2 * 1024 * 1024,
    geoip_lookup=None,
    asn_lookup=None,
) -> Dict[tuple, dict]:
    """Parse the last ``window_seconds`` of error.log for REALITY
    handshake failures, group by (country, ASN).

    Returns per-bucket:

        {
          ("AU", "AS16509"): {
              "as_org": "Amazon.com Inc.",
              "fail_count": 8,
              "unique_ips": 1,
              "reason_buckets": {"no_hello": 5, "tls_version": 2, "sni_mismatch": 1},
              "top_ips": [("16.176.125.156", 8)],  # sorted desc
          },
          ...
        }

    Active probing shows up as a small number of source IPs producing
    a lot of fails in a short window, mostly clustered in
    ``no_hello`` / ``sni_mismatch`` reason buckets. Regular client
    mis-config is the opposite shape: lots of distinct source IPs,
    each with 1-2 fails, mostly ``auth_fail`` or ``tls_version``.
    """
    text = _tail_bytes(path, tail_bytes)
    if not text:
        return {}

    latest: Optional[datetime] = None
    for line in text.splitlines():
        m = _REALITY_FAIL_RE.match(line)
        if m:
            ts = _parse_ts(m.group(1))
            if ts and (latest is None or ts > latest):
                latest = ts
    if latest is None:
        return {}
    cutoff = latest - timedelta(seconds=window_seconds)

    per_bucket: Dict[tuple, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    per_bucket_org: Dict[tuple, str] = {}
    per_bucket_reasons: Dict[tuple, Dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    for line in text.splitlines():
        m = _REALITY_FAIL_RE.match(line)
        if not m:
            continue
        ts = _parse_ts(m.group(1))
        if not ts or ts < cutoff:
            continue
        ip = m.group(2)
        reason = m.group(3)
        cc = None
        asn = None
        org = ""
        if geoip_lookup is not None:
            try:
                g = geoip_lookup(ip)
                if g:
                    cc = g[0]
            except Exception:
                pass
        if asn_lookup is not None:
            try:
                a = asn_lookup(ip)
                if a:
                    asn = a[0]
                    org = a[1] if len(a) > 1 else ""
            except Exception:
                pass
        bucket = (cc, asn)
        per_bucket[bucket][ip] += 1
        per_bucket_reasons[bucket][_classify_reason(reason)] += 1
        if org and bucket not in per_bucket_org:
            per_bucket_org[bucket] = org

    out: Dict[tuple, dict] = {}
    for bucket, ip_counts in per_bucket.items():
        fail_count = sum(ip_counts.values())
        top_ips = sorted(ip_counts.items(), key=lambda kv: -kv[1])[:5]
        out[bucket] = {
            "as_org": per_bucket_org.get(bucket, ""),
            "fail_count": fail_count,
            "unique_ips": len(ip_counts),
            "reason_buckets": dict(per_bucket_reasons[bucket]),
            "top_ips": top_ips,
        }
    return out


# ---- /proc TCP abort counters ----

# Counter keys we care about from /proc/net/netstat (TcpExt section).
# These accumulate since boot; the collector takes deltas vs the
# previous snapshot to get a per-window rate.
TCP_ABORT_KEYS = (
    "TCPAbortOnData",      # Close from local app while data still in flight
    "TCPAbortOnClose",     # Close from local app, sent RST
    "TCPAbortOnMemory",    # Out-of-memory aborts (rare)
    "TCPAbortOnTimeout",   # No keepalive response in time
    "TCPAbortOnLinger",    # SO_LINGER timeout
    "TCPAbortFailed",      # The kernel tried to abort but couldn't
)


def read_tcp_abort_counters(path: str = "/proc/net/netstat") -> Dict[str, int]:
    """Read /proc/net/netstat and extract TcpExt abort counters.

    Returns a dict like ``{"TCPAbortOnData": 12345, ...}``.
    Returns empty dict if /proc isn't readable (containers without
    --network host) — caller should treat that as "data unavailable".
    """
    try:
        with open(path, 'r') as f:
            lines = f.readlines()
    except OSError:
        return {}
    # The file has paired header/values lines per subsystem.
    # We want lines starting with "TcpExt:".
    headers = None
    values = None
    for line in lines:
        if not line.startswith("TcpExt:"):
            continue
        parts = line[len("TcpExt:"):].strip().split()
        # First time we see "TcpExt:" it's the header (alpha tokens),
        # second time it's the values (numeric tokens).
        if headers is None:
            headers = parts
        else:
            values = parts
            break
    if not headers or not values or len(headers) != len(values):
        return {}
    by_name = dict(zip(headers, values))
    out: Dict[str, int] = {}
    for key in TCP_ABORT_KEYS:
        v = by_name.get(key)
        if v is None:
            continue
        try:
            out[key] = int(v)
        except ValueError:
            pass
    return out
