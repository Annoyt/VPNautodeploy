#!/usr/bin/env python3
"""Exit-side xray log → bot DPI reporter (runs on the EXIT host).

The entry node is only a DNAT/HAProxy front: its local xray never sees
user traffic, so the bot's original access.log/error.log DPI sources
produced zero rows for their entire lifetime. The real handshakes —
users, RKN active probing, port scanners — all land in the EXIT xray's
logs. This oneshot (systemd timer, every 5 min):

  1. Reads only the *new* bytes of access.log / error.log since the
     previous run (byte offsets kept in a state file; a shrunken file
     means logrotate copytruncate fired, so we restart from 0).
  2. Aggregates accepted connections per inbound tag (with per-IP
     counts) and reject events per kind/reason (REALITY invalid
     connection, shadowsocks invalid request, httpupgrade failures).
  3. POSTs the summary to the bot's /api/dpi/exit_report, which does
     the geoip bucketing and writes dpi_metrics rows.

If the POST fails the offsets are NOT advanced — the same bytes are
re-parsed and re-sent on the next tick, so nothing is lost and nothing
is double-counted.

Environment:
    BOT_URL           Bot web server base (default https://130.49.146.10:8448,
                      the hop's HTTPS listener, AGENTS.md §53). Plain http is
                      refused unless the host is loopback: the token must not
                      cross the internet in clear.
    BOT_CA_FILE       The certificate an https BOT_URL must present (default
                      /etc/nekovpn-hop/entry-hop.pem, the hop's pinned
                      certificate); it is the ONLY trust anchor. Empty = the
                      system's CAs (a BOT_URL with a public certificate).
    DPI_REPORT_TOKEN  Shared secret for the report endpoint (required)
    XRAY_LOG_DIR      Log dir (default: the 3x-ui docker volume)
    STATE_PATH        Offset state file (default /var/lib/exit-dpi/state.json)
    MAX_CHUNK_BYTES   Per-run parse cap; larger backlogs skip to tail
"""

import ipaddress
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

BOT_URL = os.getenv("BOT_URL", "https://130.49.146.10:8448").rstrip("/")
BOT_CA_FILE = os.getenv("BOT_CA_FILE", "/etc/nekovpn-hop/entry-hop.pem").strip()
TOKEN = os.getenv("DPI_REPORT_TOKEN", "")
LOG_DIR = os.getenv(
    "XRAY_LOG_DIR",
    "/var/lib/docker/volumes/vpn-bot_3xui-data/_data",
)
STATE_PATH = os.getenv("STATE_PATH", "/var/lib/exit-dpi/state.json")
MAX_CHUNK_BYTES = int(os.getenv("MAX_CHUNK_BYTES", str(20 * 1024 * 1024)))
TOP_IPS = 20

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("exit-dpi")

# 2026/08/19 21:34:53 from 130.49.146.10:10611 accepted udp:1.2.3.4:443
#   [inbound-2053 >> direct] email: user_..@nekovo.ru
ACCESS_RE = re.compile(
    r" from (\S+?):\d+ accepted \S+ "
    r"\[([^\]\s]+)(?: >> [^\]]+)?\](?: email: (\S+))?"
)
# transport/internet/tcp: REALITY: processed invalid connection
#   from 45.156.128.134:53070: failed to read client hello
REALITY_RE = re.compile(
    r"REALITY: processed invalid connection from (\S+?):\d+: (.+?)\s*$"
)
# app/proxyman/inbound: connection ends > shadowsocks: serve TCP
#   from 118.193.72.187:52756: invalid request
SS_RE = re.compile(
    r"shadowsocks: serve \w+ from (\S+?):\d+: (.+?)\s*$"
)
# transport/internet/httpupgrade: failed to handle request > <reason>
# (only some reasons embed a peer address: "read tcp a->1.2.3.4:56")
WS_RE = re.compile(
    r"transport/internet/httpupgrade: failed to handle request > (.+?)\s*$"
)
WS_PEER_RE = re.compile(r"read tcp \S+->(\S+?):\d+")


def _load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def read_new_lines(path: str, state: dict, key: str):
    """Yield lines added since the stored offset; return the new offset.

    A file smaller than the stored offset means copytruncate rotation —
    start over from 0. A backlog above MAX_CHUNK_BYTES (first run
    against a huge file, long outage) is skipped: we baseline to the
    tail instead of chewing hundreds of MB in one tick.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], state.get(key, 0)
    offset = state.get(key, 0)
    if offset > size:
        offset = 0  # rotated underneath us
    if size - offset > MAX_CHUNK_BYTES:
        logger.warning(
            f"{path}: backlog {size - offset} bytes > cap, skipping to tail"
        )
        return [], size
    if key not in state:
        return [], size  # first run — baseline, report nothing
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(size - offset)
    # A write may be mid-line at the moment we read; keep the partial
    # tail out of this batch by not advancing past the last newline.
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        return [], offset
    lines = chunk[: last_nl + 1].decode("utf-8", "replace").splitlines()
    return lines, offset + last_nl + 1


def parse_access(lines) -> list:
    """Aggregate accepted connections per inbound tag.

    Skips the panel's own stats polling (api tag / localhost) and our
    probe clients — they'd drown the real signal.
    """
    buckets: dict = {}
    for line in lines:
        m = ACCESS_RE.search(line)
        if not m:
            continue
        ip, tag, email = m.group(1), m.group(2), m.group(3) or ""
        if tag == "api" or ip.startswith("127."):
            continue
        if email.startswith("probe"):
            continue
        b = buckets.setdefault(tag, {"conns": 0, "ips": {}, "emails": set()})
        b["conns"] += 1
        b["ips"][ip] = b["ips"].get(ip, 0) + 1
        if email:
            b["emails"].add(email)
    return [
        {
            "tag": tag,
            "conns": b["conns"],
            "uniq_emails": len(b["emails"]),
            "ips": dict(
                sorted(b["ips"].items(), key=lambda kv: -kv[1])[:TOP_IPS]
            ),
        }
        for tag, b in buckets.items()
    ]


def parse_presence(lines) -> list:
    """Per-email: the inbound tag each user was last seen on.

    Feeds /onlines' "which protocol is this user on right now".
    Xray's access.log is the only place that knows it — the panel
    keys client_traffics by email alone (UNIQUE(email)), so both
    lastOnline and up/down are shared across inbounds and can't
    attribute a user to one. Hysteria2 is a separate binary and never
    lands here; the bot fills hy2 in from hy2_auth_log instead.

    Lines arrive in chronological order, so the last tag seen for an
    email wins — that's the transport it's using now. ``conns`` is
    how many accepts it made this window, which reads as activity.
    """
    per_email: dict = {}
    for line in lines:
        m = ACCESS_RE.search(line)
        if not m:
            continue
        ip, tag, email = m.group(1), m.group(2), m.group(3) or ""
        if not email or tag == "api" or ip.startswith("127."):
            continue
        if email.startswith("probe"):
            continue
        rec = per_email.get(email)
        if rec is None or rec["tag"] != tag:
            # New user, or they moved to a different inbound: the
            # later tag is the live one, so restart the count.
            per_email[email] = {"tag": tag, "conns": 1}
        else:
            rec["conns"] += 1
    return [
        {"email": email, "tag": rec["tag"], "conns": rec["conns"]}
        for email, rec in per_email.items()
    ]


def parse_usage(lines) -> list:
    """``[{email, tag, conns}]``: every inbound each user was accepted on
    this window, with the count (IMPROVEMENT_PLAN F, D3). The presence block
    keeps only the LAST tag per user, so a client's url test through
    another protocol could put the user under that one for the window;
    the bot weighs these counts instead."""
    per: dict = {}
    for line in lines:
        m = ACCESS_RE.search(line)
        if not m:
            continue
        ip, tag, email = m.group(1), m.group(2), m.group(3) or ""
        if not email or tag == "api" or ip.startswith("127.") or email.startswith("probe"):
            continue
        per[(email, tag)] = per.get((email, tag), 0) + 1
    return [{"email": e, "tag": t, "conns": n} for (e, t), n in sorted(per.items())]


# ---- hy2 sessions from the hysteria journal (IMPROVEMENT_PLAN F, D4) --------
# "client connected {"addr": "...", "id": "<email>", "tx": 0}" and
# "client disconnected {"addr": "...", "id": "<email>", "error": "..."}" on
# both instances. A session is paired inside one hysteria PROCESS (unit,
# boot id, pid): a restart or a reboot logs no disconnect for the
# connections it killed, and pairing across it shifted every later session
# of that client by one. A disconnect closes the open connect with the same
# addr; the addr changes with port hopping, so otherwise the NEWEST open
# one (a lost disconnect then leaves one stale connect behind, forgotten
# after a day, instead of stretching every later session).
HYSTERIA_UNITS = ("hysteria", "hysteria-turbo")
HY_EVENT_RE = re.compile(r"client (connected|disconnected)\s+(\{.*\})")
HY_OPEN_MAX = 20                 # open connects remembered per client
HY_OPEN_MAX_AGE_S = 24 * 3600    # a connect never closed is forgotten after a day
HY_SESSIONS_MAX = 1000           # per report
# The stored cursor is trusted while the journal was last read less than this
# long ago. Longer (the reporter was down, or the bot refused its reports) =
# start over 10 min back and forget the open connects: the backlog of a long
# outage is not read (it could run past the timeout or the unit's MemoryMax
# and take the whole report with it), and its disconnects are gone. A quiet
# journal is no outage: the cursor's own entry may be hours old while the
# reporter ran every 5 min, and a session open across the quiet stretch keeps
# its connect. Until 2026-11-11 the age was the cursor entry's, and 30 quiet
# minutes forgot every open connect: 8 of the 33 sessions over 30 min in a
# week of exit's journal.
JOURNAL_CURSOR_MAX_AGE_S = 30 * 60
JOURNAL_TIMEOUT_S = 30


def _message_text(raw) -> str:
    """A journal MESSAGE as text. ``journalctl -o json`` gives it as a string,
    or as an array of byte values when the line holds control characters:
    hysteria colours its level (``ESC[34mINFO ESC[0m``), so every line of it
    arrives as bytes (seen on exit, 2026-11-11). The codes stay: they sit
    before the level, and the event pattern is searched for, not anchored."""
    if isinstance(raw, list):
        try:
            raw = bytes(raw).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return str(raw or "")


def _journal_restart(state: dict, why: str) -> None:
    logger.warning(f"hysteria journal: {why} — starting 10 min back, open connects forgotten")
    state.pop("journal_cursor", None)
    state.pop("journal_cursor_ts", None)
    state["hy2_open"] = {}


def read_hysteria_events(state: dict, now: Optional[float] = None) -> list:
    """``[(unit, epoch_s, kind, id, error, process, addr)]`` since the stored
    journal cursor (the first run, and a journal last read over
    JOURNAL_CURSOR_MAX_AGE_S ago, start 10 minutes back); the new cursor, the
    time of its entry and the time of this read go into ``state``.
    journalctl missing, failing or too slow = no events and a fresh start
    next run. Never raises."""
    now = time.time() if now is None else now
    cursor = state.get("journal_cursor")
    cursor_ts = float(state.get("journal_cursor_ts") or 0)
    # A state written before journal_read_ts existed: the cursor's own entry
    # stands in, as it used to.
    read_ts = float(state.get("journal_read_ts") or cursor_ts)
    if cursor and now - read_ts > JOURNAL_CURSOR_MAX_AGE_S:
        _journal_restart(state, f"not read for {int(now - read_ts) // 60} min")
        cursor = None
    cmd = ["journalctl", "-o", "json", "--no-pager"]
    for unit in HYSTERIA_UNITS:
        cmd += ["-u", unit]
    cmd += ["--after-cursor", cursor] if cursor else ["--since", "-10min"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=JOURNAL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        _journal_restart(state, f"journalctl failed: {e}")
        return []
    if proc.returncode != 0:
        _journal_restart(state, f"journalctl rc={proc.returncode}: {(proc.stderr or '')[:200]}")
        return []
    state["journal_read_ts"] = now
    events = []
    for line in proc.stdout.splitlines():
        try:
            entry = json.loads(line)
            epoch = int(entry.get("__REALTIME_TIMESTAMP") or 0) / 1e6
        except (ValueError, TypeError):
            continue
        if entry.get("__CURSOR"):
            state["journal_cursor"] = entry["__CURSOR"]
            state["journal_cursor_ts"] = epoch or now
        # A cursor the journal no longer has makes journalctl start from
        # its head (rc 0): entries older than the cursor's are not news.
        if cursor and epoch and epoch < cursor_ts:
            continue
        m = HY_EVENT_RE.search(_message_text(entry.get("MESSAGE")))
        if not m:
            continue
        try:
            fields = json.loads(m.group(2))
        except ValueError:
            continue
        unit = str(entry.get("_SYSTEMD_UNIT") or "").replace(".service", "")
        process = f"{entry.get('_BOOT_ID') or '-'}:{entry.get('_PID') or '-'}"
        events.append((unit, epoch, m.group(1), str(fields.get("id") or ""),
                       str(fields.get("error") or ""), process, str(fields.get("addr") or "")))
    return events


def pair_sessions(events: list, state: dict, now: float) -> list:
    """Sessions closed by ``events``: ``[{id, unit, duration_s, error,
    ended_at}]``. Open connects carry over in ``state['hy2_open']`` as
    ``{"<unit>|<process>|<id>": [[epoch, addr], ...]}``. A unit's event from a
    new process drops that unit's opens of the old one. The probe client is
    left out, as in the presence block."""
    open_ = state.setdefault("hy2_open", {})
    current: dict = {}
    sessions = []
    for event in events:
        unit, epoch, kind, cid, error, process, addr = (tuple(event) + ("-", ""))[:7]
        if current.get(unit) != process:
            current[unit] = process
            for key in [k for k in open_ if k.startswith(f"{unit}|") and
                        k.split("|")[1] != process]:
                del open_[key]
        if not cid or cid.startswith("probe"):
            continue
        key = f"{unit}|{process}|{cid}"
        starts = open_.setdefault(key, [])
        if kind == "connected":
            starts.append([epoch, addr])
            del starts[:-HY_OPEN_MAX]
            continue
        if not starts:
            continue
        same = [i for i, (_t, a) in enumerate(starts) if addr and a == addr]
        start, _addr = starts.pop(same[0] if same else len(starts) - 1)
        sessions.append({"id": cid, "unit": unit, "duration_s": max(0, int(epoch - start)),
                         "error": error[:160], "ended_at": round(epoch, 3)})
    for key in list(open_):
        open_[key] = [s for s in open_[key]
                      if isinstance(s, list) and len(s) == 2 and now - s[0] < HY_OPEN_MAX_AGE_S]
        if not open_[key]:
            del open_[key]
    return sessions[:HY_SESSIONS_MAX]


def parse_rejects(lines) -> list:
    """Aggregate reject/probe events per (kind, reason) with IP counts."""
    buckets: dict = {}

    def add(kind: str, reason: str, ip: str):
        key = (kind, reason)
        b = buckets.setdefault(key, {"count": 0, "ips": {}})
        b["count"] += 1
        if ip:
            b["ips"][ip] = b["ips"].get(ip, 0) + 1

    for line in lines:
        m = REALITY_RE.search(line)
        if m:
            reason = m.group(2).rstrip(": ")
            add("reality", reason, m.group(1))
            continue
        m = SS_RE.search(line)
        if m:
            add("ss2022", m.group(2), m.group(1))
            continue
        m = WS_RE.search(line)
        if m:
            reason = m.group(1)
            peer = WS_PEER_RE.search(reason)
            # Normalise per-connection noise (addresses, paths) so the
            # reason space stays small enough to bucket on.
            reason = WS_PEER_RE.sub("read tcp <peer>", reason)
            reason = re.sub(r"bad path: \S+", "bad path", reason)
            add("ws-upgrade", reason, peer.group(1) if peer else "")
    return [
        {
            "kind": kind,
            "reason": reason,
            "count": b["count"],
            "ips": dict(
                sorted(b["ips"].items(), key=lambda kv: -kv[1])[:TOP_IPS]
            ),
        }
        for (kind, reason), b in buckets.items()
    ]


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bot_problem(url: str = None, ca_file: str = None) -> Optional[str]:
    """Why no report may go to the bot as configured, or None.

    The token rides in every report and also opens the users' lists the
    exit syncs pull (/api/xhr/users, /api/ssh/users), so it never crosses
    the internet in clear: plain http only to loopback. An https BOT_URL
    with a BOT_CA_FILE trusts that file alone (the hop's pinned
    certificate, AGENTS.md §53), and the file must be there."""
    url = BOT_URL if url is None else url
    ca_file = BOT_CA_FILE if ca_file is None else ca_file
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "http":
        if not _is_loopback(parts.hostname or ""):
            return (f"BOT_URL {url} is plain http to another host: the token would "
                    f"cross the internet in clear. Use the hop's https listener "
                    f"(AGENTS.md §53).")
        return None
    if parts.scheme != "https" or not parts.hostname:
        return f"BOT_URL {url!r} is not an http(s) URL"
    if ca_file and not os.access(ca_file, os.R_OK):
        return f"BOT_CA_FILE {ca_file} is not readable (empty = the system's CAs)"
    return None


def _ssl_context() -> Optional[ssl.SSLContext]:
    if urllib.parse.urlsplit(BOT_URL).scheme != "https":
        return None
    if BOT_CA_FILE:
        # The pinned certificate is the only trust anchor: no system CA
        # can stand in for it, whatever it issued for the bot's address.
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=BOT_CA_FILE)
        return ctx
    return ssl.create_default_context()


def post_report(payload: dict) -> bool:
    problem = bot_problem()
    if problem:
        logger.error(f"report not sent: {problem}")
        return False
    req = urllib.request.Request(
        f"{BOT_URL}/api/dpi/exit_report",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-DPI-Token": TOKEN},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as resp:
            body = json.loads(resp.read().decode() or "{}")
            return bool(body.get("ok"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        logger.warning(f"report POST failed: {e}")
        return False


def run_once() -> int:
    if not TOKEN:
        logger.error("DPI_REPORT_TOKEN is not set")
        return 1
    state = _load_state()
    access_path = os.path.join(LOG_DIR, "access.log")
    error_path = os.path.join(LOG_DIR, "error.log")

    access_lines, access_off = read_new_lines(access_path, state, "access")
    error_lines, error_off = read_new_lines(error_path, state, "error")

    access = parse_access(access_lines)
    presence = parse_presence(access_lines)
    usage = parse_usage(access_lines)
    rejects = parse_rejects(error_lines)
    # The journal cursor and the open connects advance with the offsets,
    # only once the bot took the report. The journal must never cost the
    # report itself.
    pending = json.loads(json.dumps(state))
    try:
        hy2_sessions = pair_sessions(read_hysteria_events(pending), pending, time.time())
    except Exception as e:
        _journal_restart(pending, f"pairing failed: {e}")
        hy2_sessions = []

    # Posted even when there is nothing to say (a quiet window, the
    # first-run baseline): the bot keeps one exit_reports row per report
    # (IMPROVEMENT_PLAN F, B2), and its inbound-drop alert must tell "the
    # exit did not report" from "nobody connected". An empty window used
    # to send nothing, which read as a missing report.
    ok = post_report(
        {"access": access, "rejects": rejects, "presence": presence,
         "usage": usage, "hy2_sessions": hy2_sessions}
    )
    if not ok:
        # Leave offsets untouched: the same window is retried next tick.
        return 1
    state.update({"access": access_off, "error": error_off,
                  "journal_cursor": pending.get("journal_cursor"),
                  "journal_cursor_ts": pending.get("journal_cursor_ts"),
                  "journal_read_ts": pending.get("journal_read_ts"),
                  "hy2_open": pending.get("hy2_open", {})})
    _save_state(state)
    logger.info(
        f"reported {sum(a['conns'] for a in access)} conns / "
        f"{sum(r['count'] for r in rejects)} rejects / "
        f"{len(presence)} present users / {len(hy2_sessions)} hy2 sessions"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_once())
