#!/usr/bin/env python3
"""Deterministic protocol health check — run FIRST when anyone suspects an outage.

WHY THIS EXISTS
---------------
2026-09-01 00:01 UTC: the monthly quota job blanked ``flow`` on the
VLESS-Reality clients. Reality served ZERO connections for FOUR DAYS.
The probe suite saw it immediately (reality went from ~7/10 to 0/10
every 15 minutes), the panel audit would have named the exact field —
and when an operator asked the Hermes agent "какой протокол не
работает?" it spent 105 s poking ports, iptables and container states,
found all of them "alive" and answered that everything was fine. Ports
were open; nobody could complete a handshake. None of the things the
agent improvised can see that class of failure.

This script is the fixed, boring first step that replaces the
improvisation. It reads the THREE signals that actually distinguish
"the tunnel works" from "the port answers":

  A) the bot's own probe rows (``outbound_health``), recent alerts,
     bot /health, and the panel audit (``verify_panel_client_fields.py``);
  B) the entry host: shadow-tls / haproxy / probe-proxy up, the UDP
     DNAT rules that carry Hysteria to exit;
  C) the exit host (ONE ssh round-trip): hysteria daemons, xray RUNTIME
     users per inbound (``xray api inbounduser``) vs config.json, recent
     accepted connections per inbound, hop REDIRECT rules, hysteria
     connections in the last hour, the Reality dest certificate size
     (the 2026-07-20 outage: cert > 8192 bytes kills Reality for all),
     free RAM, and what a tunnel user can reach on exit itself: the
     hysteria ACLs, a listener on :2019 (Caddy's admin), the panel xray's
     routing towards its own API (AGENTS.md §52; ``exit_guard()``).

Each layer is collected in isolation with its own timeout, so a dead
exit link never hides a flow wipe and vice versa. A layer that could
not be collected is reported as FAILED — never as "OK".

The verdict logic lives in ``assess()`` — a pure function over the three
layer dicts — so the rules are unit-tested without any host
(tests/unit/test_protocol_healthcheck.py).

LIVENESS RULE (same as bot/services/alert_manager.py)
-----------------------------------------------------
A probe row proves the tunnel works if ``latency_ms IS NOT NULL`` OR
``status = 'ok'``. An HTTP 418 that came back THROUGH the tunnel has a
latency. The 2026-09-01 rows had latency NULL: nothing ever connected.
Normal is 10/10 ok per protocol per run since 2026-11 (the two targets that
never passed through the exit, vk.com and sberbank.ru, were replaced); the
thresholds are bot/services/probe_rules.py, shared with the pager.

HY2T (Hysteria Turbo) COVERAGE IS DYNAMIC
-----------------------------------------
The sidecar gets a hy2t inbound (:18085) only on deployments with
HY2T_PORT set (HealthChecker.probe_tags_for / gen_probe_config.py). This
script runs on the host without the bot's config, so "is hy2t probed
here?" is read off the rows: layer A lists a tag only when outbound_health
has rows for it in the window. Rows present → hy2t is judged like hy2
(probe verdict first, layer C evidence second, hysteria suspects). No
rows → the pre-2026-09-05 path: layer C (+ entry DNAT) is the only
source and the evidence says "no probe coverage". Either way a definite
layer-C failure (daemon not active, entry DNAT :8402 gone) is DOWN —
probe coverage adds a signal, it never removes one.

PROTOCOLS SWITCHED OFF IN THE CASCADE ARE "OFF"
-----------------------------------------------
``probe_rules.operator_disabled`` — the pager's, the monitor's and
/protocols' rule, i.e. the cascade editor's own answer: hy2t since
2026-09-21, and an opt-in protocol (xhr) until the operator switches it
on (its server lists nobody until then, so its probe is dark by
construction). Such a protocol keeps its facts as evidence, but its state
is OFF: outside the exit code, the ИТОГ line and "every protocol dark".
Answers through it still prove the channel up, as for the pager. Without
layer A the cascade is unknown and nothing is OFF.

HOW TO RUN (as root on the ENTRY host; plain python3, no bot imports)
---------------------------------------------------------------------
    python3 /opt/vpn-bot/scripts/protocol_healthcheck.py          # human report (RU)
    python3 /opt/vpn-bot/scripts/protocol_healthcheck.py --json   # machine-readable

From the dev box:  ssh entry 'python3 /opt/vpn-bot/scripts/protocol_healthcheck.py'

EXIT CODES
----------
    0  every protocol OK (protocols switched off in the cascade — OFF — do
       not count, here or below)
    1  at least one protocol DEGRADED or DOWN (see ПОДОЗРЕВАЕМЫЕ for the
       ranked suspects, each with the concrete next command). DEGRADED
       also covers: the newest probe run fully dark while older runs were
       fine (an outage that started < 45 min ago), and a BROKEN panel
       audit on that protocol's inbound while the probes are green (the
       probe's own client may be the one survivor — 2026-09-01 was 1/81).
    2  could not assess — no probe verdict (layer A failed / probe
       pipeline stale / that tag's rows stale), or a layer whose facts
       are needed for a verdict is missing (exit ssh or entry iptables
       for hy2t) and no other layer found a definite failure.
       A 2 must not be read as a pass.

Total runtime target < 60 s: the three layers run concurrently and every
subprocess has a timeout.
"""

import argparse
import ipaddress
import json
import os
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants (mirror bot/services/alert_manager.py + health_checker.py)
# ---------------------------------------------------------------------------

def _load_probe_rules():
    """bot/services/probe_rules.py, loaded by its path: this script runs on
    the entry HOST, outside the container and without the bot's packages,
    and that module is stdlib-only for exactly this reader. The timer runs
    a root-owned copy with the module next to it
    (scripts/install_protocol_healthcheck.sh); a run from the repo (the
    agent's STEP 0) finds it in bot/services, rsynced with the rest of bot/."""
    import importlib.util
    from pathlib import Path
    here = Path(__file__).resolve().parent
    path = here / 'probe_rules.py'
    if not path.exists():
        path = here.parent / 'bot' / 'services' / 'probe_rules.py'
    spec = importlib.util.spec_from_file_location('nekovpn_probe_rules', str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe_rules = _load_probe_rules()

PROBED = ['reality', 'hy2', 'ws', 'stls']      # what the probe sidecar ALWAYS speaks
# Probed only where the deployment has them — decided per run by
# probed_tags(): hy2t with HY2T_PORT, the DE reserve with FALLBACK_NODE_*.
PROBED_OPTIONAL = ['hy2t', probe_rules.RESERVE_TAG, 'xhr']
PROTOCOLS = ['reality', 'hy2', 'hy2t', 'ws', 'stls']   # what we always report on
# ...plus the DE reserve (B6) where it is probed: it joins the report only
# with probe rows of its own, judged from them alone (no layer B/C facts).
REPORT_ORDER = PROTOCOLS + [probe_rules.RESERVE_TAG, 'xhr']

# One definition with the pager, the monitor and /protocols (probe_rules,
# IMPROVEMENT_PLAN F, B5). Until 2026-11 this script called a window
# DEGRADED below 35 % (half of a 7/10 norm) while the pager said 25 %.
PROBE_DOMAINS = probe_rules.PROBE_DOMAINS
PROBE_RUNS = probe_rules.PROBE_RUNS
PROBE_LIMIT = PROBE_RUNS * PROBE_DOMAINS
PROBE_WINDOW_H = probe_rules.PROBE_WINDOW_H
PROBE_STALE_MIN = probe_rules.PROBE_STALE_MIN
RUN_SPAN_MIN = 5             # rows within 5 min of the newest row = one run (a run writes 10 rows in ms)
DEGRADED_BELOW = probe_rules.PROBE_DEGRADED_OK_RATIO
DEGRADED_MIN_SAMPLES = probe_rules.PROBE_DEGRADED_MIN_SAMPLES
ALERT_WINDOW_H = 6
AUDIT_LINES_MAX = 45         # BROKEN summary + up to 40 problem lines + "… and N more"

# The Reality dest TLS Certificate record must fit xtls/reality's
# hardcoded 8192-byte buffer; microsoft's grew to 8273 on 2026-07-20 and
# killed Reality for everyone. Flag well before the edge.
CERT_LIMIT_BYTES = 8000

# Which exit inbound serves which probed protocol (xhttp inbound-2054
# is not probed — sing-box has no XHTTP transport).
INBOUND_FOR = {'reality': 'inbound-443', 'ws': 'inbound-2053', 'stls': 'inbound-8444'}
PROTOCOL_OF_INBOUND = {v: k for k, v in INBOUND_FOR.items()}

# Entry-side UDP DNAT expectations: (dport, exit port). All must exist.
ENTRY_DNAT_FOR = {
    'hy2':  [('443', '8400'), ('8400', '8400'), ('20000:40000', '8400')],
    'hy2t': [('8402', '8402'), ('40001:50000', '8402')],
}
# Exit-side hop REDIRECT expectations: (dport range, local port).
EXIT_HOP_FOR = {
    'hy2':  [('20000:40000', '8400')],
    'hy2t': [('40001:50000', '8402')],
}
HYSTERIA_UNIT = {'hy2': 'hysteria', 'hy2t': 'hysteria-turbo'}

BOT_CONTAINER = 'vpn-bot'
BOT_DB = '/var/lib/vpn-bot/bot.db'
BOT_ENV = '/opt/vpn-bot/.env'
HEALTH_URL = 'http://127.0.0.1:8080/health'
EXIT_SSH_HOST = 'exit-node'
DEFAULT_SNI = 'www.bing.com'

# Per-layer wall-clock budgets (seconds). Layers run concurrently, so the
# total is ~max(), not the sum.
TIMEOUT_A = 50
TIMEOUT_B = 20
TIMEOUT_C = 45

# OFF = switched off in the cascade: outside every verdict (see the docstring).
STATE_ORDER = {'DOWN': 3, 'DEGRADED': 2, 'UNKNOWN': 1, 'OK': 0, 'OFF': -1}


# ---------------------------------------------------------------------------
# Layer A — runs INSIDE the bot container (python3 -c). Stdlib only; it must
# work on the prod image's python 3.11. Prints one JSON blob.
# ---------------------------------------------------------------------------

INLINE_A = r'''
import json, sqlite3, subprocess, sys
from datetime import datetime, timedelta
DB = %(db)r
out = {"probes": {}, "newest_ts": None, "alerts": [], "audit": {}, "cascade_order": None}
now = datetime.utcnow()
cutoff = (now - timedelta(hours=%(window_h)d)).isoformat()
try:
    conn = sqlite3.connect(DB, timeout=5)
    out["newest_ts"] = conn.execute(
        "SELECT MAX(ts) FROM outbound_health").fetchone()[0]
    tags = [r[0] for r in conn.execute(
        "SELECT DISTINCT outbound_tag FROM outbound_health WHERE ts >= ?",
        (cutoff,))]
    for tag in tags:
        rows = conn.execute(
            "SELECT ts, status, latency_ms FROM outbound_health "
            "WHERE outbound_tag = ? AND ts >= ? ORDER BY ts DESC LIMIT %(limit)d",
            (tag, cutoff)).fetchall()
        last_alive = conn.execute(
            "SELECT MAX(ts) FROM outbound_health WHERE outbound_tag = ? "
            "AND (latency_ms IS NOT NULL OR status = 'ok')", (tag,)).fetchone()[0]
        out["probes"][tag] = {"rows": [list(r) for r in rows],
                              "last_alive": last_alive}
    # fired_at is CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS"); normalise a
    # possible ISO 'T' so the string comparison cannot silently drop rows.
    acut = (now - timedelta(hours=%(alert_h)d)).strftime("%%Y-%%m-%%d %%H:%%M:%%S")
    out["alerts"] = [list(r) for r in conn.execute(
        "SELECT key, severity, title, fired_at, acked_at FROM alert_history "
        "WHERE replace(fired_at, 'T', ' ') >= ? "
        "AND (key LIKE 'protocol_down:%%' OR key LIKE 'dpi_%%') "
        "ORDER BY fired_at DESC LIMIT 20", (acut,))]
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'cascade_protocol_order'").fetchone()
    out["cascade_order"] = row[0] if row else None
except Exception as e:
    out["db_error"] = str(e)[:300]
try:
    p = subprocess.run([sys.executable, "/app/scripts/verify_panel_client_fields.py"],
                       capture_output=True, text=True, timeout=35)
    out["audit"] = {"rc": p.returncode,
                    "lines": (p.stdout + p.stderr).splitlines()[:%(audit_lines)d]}
except Exception as e:
    out["audit"] = {"rc": None, "error": str(e)[:300]}
print(json.dumps(out))
''' % {'db': BOT_DB, 'window_h': PROBE_WINDOW_H, 'limit': PROBE_LIMIT,
       'alert_h': ALERT_WINDOW_H, 'audit_lines': AUDIT_LINES_MAX}


# ---------------------------------------------------------------------------
# Layer C — runs on the EXIT host via `ssh exit-node python3 -` (script on
# stdin, ONE round-trip). Stdlib only. Prints one JSON blob. %(sni)s is
# substituted before sending.
# ---------------------------------------------------------------------------

REMOTE_C = r'''
import json, re, subprocess
def sh(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, "", str(e)
out = {"errors": {}}
svc = {}
for u in ("hysteria", "hysteria-turbo", "hy2-traffic-collector", "hy2-traffic-collector-turbo"):
    rc, o, e = sh("systemctl is-active " + u, 10)
    svc[u] = (o.strip().splitlines() or [e.strip() or "unknown"])[0]
out["services"] = svc
# RUNTIME users = what xray actually authenticates right now. The panel
# hot-applies client edits as RemoveUser+AddUser and the AddUser can fail
# silently — runtime < config.json is the signature of that drift.
rt = {}
for tag in ("inbound-443", "inbound-2053", "inbound-8444", "inbound-2054"):
    rc, o, e = sh("docker exec 3x-ui sh -c '/app/bin/xray-linux-amd64 api inbounduser "
                  "-s 127.0.0.1:62789 -tag %%s'" %% tag, 15)
    try:
        users = json.loads(o).get("users") or []
        rt[tag] = {"count": len(users),
                   "with_flow": sum(1 for u in users if (u.get("account") or {}).get("flow"))}
    except Exception as ex:
        rt[tag] = {"error": (e.strip() or str(ex))[:200]}
out["runtime"] = rt
cfg = {}
xcfg = None
rc, o, e = sh("docker exec 3x-ui cat /app/bin/config.json", 15)
try:
    xcfg = json.loads(o)
    for ib in xcfg.get("inbounds") or []:
        clients = (ib.get("settings") or {}).get("clients") or []
        cfg[ib.get("tag")] = {"count": len(clients), "protocol": ib.get("protocol"),
                              "with_flow": sum(1 for c in clients if c.get("flow"))}
except Exception as ex:
    out["errors"]["config"] = (e.strip() or str(ex))[:200]
out["config"] = cfg
acc, last = {}, {}
rc, o, e = sh("docker exec 3x-ui sh -c 'grep -h accepted /etc/x-ui/access.log | tail -5000'", 25)
for line in o.splitlines():
    m = re.search(r"\[(inbound-\d+)", line)
    if not m:
        continue
    acc[m.group(1)] = acc.get(m.group(1), 0) + 1
    last[m.group(1)] = line[:19]
out["accepted"] = acc
out["last_accepted"] = last
rc, o, e = sh("iptables -t nat -S PREROUTING", 10)
out["nat_prerouting"] = o.splitlines()
if rc != 0 or not o.strip():
    # An empty list must read as "iptables did not answer", never as "no rules".
    out["errors"]["nat"] = (e.strip() or "rc=%%s" %% rc)[:200]
conns = {}
for u in ("hysteria", "hysteria-turbo"):
    rc, o, e = sh("journalctl -u %%s --since -1h --no-pager | grep -c 'client connected'" %% u, 15)
    try:
        conns[u] = int(o.strip() or 0)
    except ValueError:
        conns[u] = None
out["hy2_connections_1h"] = conns
rc, o, e = sh("timeout 20 openssl s_client -connect %(sni)s:443 -servername %(sni)s "
              "-tls1_3 -msg </dev/null 2>/dev/null | grep -A1 'Certificate$'", 25)
out["cert_sni"] = %(sni)r
out["cert_lines"] = o.splitlines()[:4]
rc, o, e = sh("free -m | awk '/Mem:/{print $7}'", 5)
try:
    out["free_ram_mb"] = int(o.strip())
except ValueError:
    out["free_ram_mb"] = None
# What a tunnel user can reach on exit itself (AGENTS.md 52). Only the facts
# the verdict needs leave the host: the hysteria configs also hold the auth
# URL, the obfs password and the stats secret; config.json holds every key.
guard = {"hysteria": {}}
try:
    for u in ("hysteria", "hysteria-turbo"):
        rc, o, e = sh("systemctl cat " + u, 10)
        if rc != 0:
            guard["hysteria"][u] = {"missing": (e.strip() or "rc=" + str(rc))[:160]}
            continue
        guard["hysteria"][u] = guard_hysteria(o, guard_read)
    rc, o, e = sh("ss -ltnH", 10)
    guard["listen_2019"] = guard_listen(o, 2019) if rc == 0 else None
    rc, o, e = sh("docker exec 3x-ui /app/bin/xray-linux-amd64 version", 15)
    guard["xray_version"] = (o.strip().splitlines() or [""])[0][:160]
    guard["xray"] = guard_xray_view(xcfg) if isinstance(xcfg, dict) else None
except Exception as ex:
    guard["error"] = str(ex)[:200]
out["guard"] = guard
# The servers setup_ssh_exit.sh / setup_xhr_exit.sh run (AGENTS.md 51): raw
# answers only, judged on entry (exit_servers()).
srv = {}
rc, o, e = sh("cat /proc/uptime", 5)
srv["uptime"] = o.strip()[:60] if rc == 0 else None
for name in ("ssh-exit", "xhr-exit"):
    rc, o, e = sh("docker inspect -f '{{.State.Status}} {{.State.Restarting}}' " + name, 10)
    ins = {"rc": rc, "out": o.strip()[:120], "err": e.strip()[:160]}
    rc, o, e = sh("systemctl show " + name + "-sync.service " + name + "-sync.timer -p Id "
                  "-p LoadState -p ActiveState -p Result -p ExecMainExitTimestampMonotonic", 10)
    srv[name] = {"inspect": ins, "show_rc": rc, "show": o[:1500], "show_err": e.strip()[:160]}
out["exit_servers"] = srv
print(json.dumps(out))
'''

# Helpers of the layer-C script, sent ahead of it (not %-formatted). They are
# plain functions so the tests can run them on sample text.
GUARD_HELPERS = r'''
import json as _gjson
import re as _gre


def guard_read(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read(262144)


def guard_acl_block(text):
    """The top-level `acl` block of a hysteria YAML config, as lines."""
    out, inside = [], False
    for line in text.splitlines():
        if not inside:
            if _gre.match(r"acl\s*:", line):
                inside = True
                out.append(line)
            continue
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and line[:1] not in (" ", "\t"):
            break
        out.append(line)
    return out


def guard_hysteria(unit_text, read):
    """The ACL facts of one hysteria unit: its config path and the `acl`
    block (or, for a JSON config, the `acl` object), plus the rules file the
    block names. ``read(path)`` returns a file's text or raises."""
    path = None
    for line in unit_text.splitlines():
        if line.startswith("ExecStart="):
            m = _gre.search(r"\s(?:-c|--config)(?:\s+|=)(\S+)", line)
            if m:
                path = m.group(1).strip("\"'")
    if not path:
        return {"error": "no -c/--config in ExecStart"}
    try:
        text = read(path)
    except Exception as e:
        return {"config": path, "error": str(e)[:200]}
    fpath = None
    if path.endswith(".json"):
        try:
            acl = _gjson.loads(text).get("acl")
        except Exception as e:
            return {"config": path, "error": "config json: " + str(e)[:160]}
        info = {"config": path, "acl_json": acl if isinstance(acl, dict) or acl is None else str(acl)[:200]}
        if isinstance(acl, dict) and acl.get("file"):
            fpath = str(acl.get("file"))
    else:
        block = guard_acl_block(text)
        info = {"config": path, "acl_lines": block[:400]}
        for line in block:
            m = _gre.match(r"\s+file\s*:\s*(\S+)", line)
            if m:
                fpath = m.group(1).strip("\"'")
    if fpath:
        info["acl_file"] = fpath
        try:
            info["acl_file_lines"] = read(fpath).splitlines()[:2000]
        except Exception as e:
            info["acl_file_error"] = str(e)[:200]
    return info


def guard_listen(ss_text, port):
    """Local addresses of the TCP listeners on ``port`` in ``ss -ltnH``."""
    found = []
    for line in ss_text.splitlines():
        f = line.split()
        if len(f) >= 4 and f[3].rsplit(":", 1)[-1] == str(port):
            found.append(f[3])
    return found


def guard_xray_view(cfg):
    """What decides whether a tunnel user reaches the panel xray's API: the
    routing, the outbounds' kinds and final rules, the inbounds' kinds. No
    client, key or password leaves the host; a rule's other conditions are
    kept by name only."""
    rt = cfg.get("routing") or {}
    kept = ("inboundTag", "outboundTag", "balancerTag", "port", "network", "ip")
    rules = []
    for r in rt.get("rules") or []:
        if not isinstance(r, dict):
            continue
        v = {k: r[k] for k in kept if k in r}
        if isinstance(v.get("ip"), list):
            v["ip"] = [str(x) for x in v["ip"]][:64]
        v["other"] = sorted(k for k in r if k not in kept + ("type", "ruleTag"))
        rules.append(v)
    outs = []
    for ob in cfg.get("outbounds") or []:
        if not isinstance(ob, dict):
            continue
        st = ob.get("settings") or {}
        o = {"tag": ob.get("tag"), "protocol": ob.get("protocol")}
        if ob.get("protocol") in ("freedom", "direct"):
            fr = st.get("finalRules")
            if isinstance(fr, list):
                o["finalRules"] = [{k: x.get(k) for k in ("action", "network", "port", "ip")}
                                   for x in fr if isinstance(x, dict)]
            elif fr is not None:
                o["finalRules"] = str(fr)[:200]
        outs.append(o)
    api = cfg.get("api") or {}
    met = cfg.get("metrics") or {}
    return {
        "domainStrategy": rt.get("domainStrategy"),
        "rules": rules,
        "balancers": [{"tag": b.get("tag"), "selector": b.get("selector")}
                      for b in rt.get("balancers") or [] if isinstance(b, dict)],
        "outbounds": outs,
        "inbounds": [{"tag": ib.get("tag"), "protocol": ib.get("protocol"),
                      "listen": ib.get("listen"), "port": ib.get("port")}
                     for ib in cfg.get("inbounds") or [] if isinstance(ib, dict)],
        "api": {"tag": api.get("tag"), "listen": api.get("listen")},
        "metrics": {"tag": met.get("tag"), "listen": met.get("listen")},
    }
'''


# ---------------------------------------------------------------------------
# Small pure helpers (unit-tested)
# ---------------------------------------------------------------------------

def _run(cmd: List[str], timeout: int, stdin: Optional[str] = None) -> Tuple[int, str, str]:
    """subprocess wrapper that never raises: (rc, stdout, stderr).
    rc -1 = timeout / not found, so a callers' "failed" branch is uniform."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           input=stdin)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, '', f'timeout after {timeout}s: {" ".join(cmd[:3])}'
    except (OSError, ValueError) as e:
        return -1, '', str(e)


def _minutes_since(ts_str, now: datetime) -> float:
    """Minutes between an ISO/CURRENT_TIMESTAMP string and ``now``; inf if
    unparsable (so an unparsable "newest row" reads as stale, not fresh)."""
    try:
        ts = datetime.fromisoformat(str(ts_str).replace('Z', '').replace(' ', 'T'))
    except (TypeError, ValueError):
        return float('inf')
    return max(0.0, (now - ts).total_seconds() / 60.0)


def _humanize(mins: float) -> str:
    if mins == float('inf'):
        return '—'
    mins = int(mins)
    if mins < 60:
        return f'{mins} мин'
    hours, m = divmod(mins, 60)
    if hours < 24:
        return f'{hours}ч {m:02d}мин'
    days, hours = divmod(hours, 24)
    return f'{days}д {hours}ч'


def parse_cert_record_len(lines) -> Optional[int]:
    """Bytes of the TLS Certificate handshake record from ``openssl -msg``.

    The line looks like ``<<< TLS 1.3, Handshake [length 0f47], Certificate``
    — the hex after ``length`` is the record size. None if absent (dest
    unreachable / TLS 1.2 only / grep found nothing)."""
    for line in lines or []:
        m = re.search(r'Handshake \[length ([0-9a-fA-F]+)\],\s*Certificate\b', str(line))
        if m:
            return int(m.group(1), 16)
    return None


def parse_dnat_rules(lines) -> set:
    """{(dport, target_port)} from ``iptables -t nat -S PREROUTING`` UDP DNAT lines."""
    found = set()
    for line in lines or []:
        m = re.search(r'-p udp .*--dport (\S+) .*-j DNAT --to-destination \S+?:(\d+)', str(line))
        if m:
            found.add((m.group(1), m.group(2)))
    return found


def parse_redirect_rules(lines) -> set:
    """{(dport, to_port)} from UDP REDIRECT lines (exit-side hop rules)."""
    found = set()
    for line in lines or []:
        m = re.search(r'-p udp .*--dport (\S+) .*-j REDIRECT --to-ports (\d+)', str(line))
        if m:
            found.add((m.group(1), m.group(2)))
    return found


def parse_exit_json(text: str) -> dict:
    """Tolerant parser for the layer-C blob: always returns a dict with every
    key present (empty defaults), ``ok`` False + ``error`` when the text is
    not the JSON we expect. A half-broken exit must still yield the keys
    that DID come back."""
    base = {'ok': False, 'error': None, 'services': {}, 'runtime': {}, 'config': {},
            'accepted': {}, 'last_accepted': {}, 'nat_prerouting': [],
            'hy2_connections_1h': {}, 'cert_lines': [], 'cert_sni': None,
            'free_ram_mb': None, 'errors': {}, 'guard': {}, 'exit_servers': {}}
    text = (text or '').strip()
    # ssh banners / MOTD may precede the blob: take the last line that parses.
    candidate = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith('{'):
            candidate = line
            break
    if candidate is None:
        base['error'] = 'no JSON in exit output' if text else 'empty exit output'
        return base
    try:
        data = json.loads(candidate)
    except ValueError as e:
        base['error'] = f'exit JSON unparsable: {e}'
        return base
    if not isinstance(data, dict):
        base['error'] = 'exit JSON is not an object'
        return base
    for k, default in base.items():
        if k not in data:
            continue
        # Typed defaults ({} / []) guard the readers below from a stray
        # string; a None default means "scalar, take whatever came".
        if default is None or isinstance(data[k], type(default)):
            base[k] = data[k]
    try:                                   # the one numeric scalar readers format
        base['free_ram_mb'] = int(base['free_ram_mb'])
    except (TypeError, ValueError):
        base['free_ram_mb'] = None
    base['ok'] = True
    base['error'] = None
    return base


def _read_env_value(path: str, key: str) -> Optional[str]:
    """One value from a KEY=VALUE .env file. Reads only the requested key;
    the file also holds the bot token, which must never be echoed."""
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(key + '='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'") or None
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Collectors (host side effects; not unit-tested beyond the parsers)
# ---------------------------------------------------------------------------

def collect_layer_a() -> dict:
    """Probe rows + alerts + panel audit from inside the bot container, and
    the bot's /health from the host. The /health fetch is separate so a
    dead container still yields a clear 'bot down' instead of a stack."""
    out = {'ok': False, 'error': None, 'probes': {}, 'newest_ts': None,
           'alerts': [], 'audit': {}, 'health': {}, 'cascade_order': None}
    rc, so, se = _run(['docker', 'exec', '-e', 'PYTHONPATH=/app', BOT_CONTAINER,
                       'python3', '-c', INLINE_A], TIMEOUT_A)
    if rc != 0 and not so.strip():
        out['error'] = (se.strip() or f'docker exec rc={rc}')[:300]
    else:
        try:
            data = json.loads(so.strip().splitlines()[-1])
            out.update({k: data.get(k, out[k])
                        for k in ('probes', 'newest_ts', 'alerts', 'audit', 'cascade_order')})
            if data.get('db_error'):
                out['error'] = f"bot.db: {data['db_error']}"
            else:
                out['ok'] = True
        except (ValueError, IndexError) as e:
            out['error'] = f'layer A JSON unparsable: {e}'
    # /health — bypass any proxy env (api.telegram proxy must not eat 127.0.0.1)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(HEALTH_URL, timeout=5) as resp:
            h = json.loads(resp.read().decode('utf-8', 'replace'))
            out['health'] = {'status': h.get('status'), 'version': h.get('version'),
                             'database': h.get('database')}
    except Exception as e:                        # noqa: BLE001 - reported, not raised
        out['health'] = {'error': str(e)[:200]}
    return out


def collect_layer_b() -> dict:
    """Entry host: front daemons, sidecar container, UDP DNAT rules."""
    out = {'ok': True, 'error': None, 'services': {}, 'containers': {},
           'nat_prerouting': []}
    errors = []
    for unit in ('shadow-tls', 'haproxy'):
        rc, so, se = _run(['systemctl', 'is-active', unit], 10)
        out['services'][unit] = (so.strip().splitlines() or [se.strip() or 'unknown'])[0]
    for name in ('probe-proxy', BOT_CONTAINER):
        rc, so, se = _run(['docker', 'inspect', '-f', '{{.State.Status}}', name], 10)
        out['containers'][name] = so.strip() if rc == 0 else f'missing ({se.strip()[:80]})'
    rc, so, se = _run(['iptables', '-t', 'nat', '-S', 'PREROUTING'], 10)
    if rc == 0:
        out['nat_prerouting'] = so.splitlines()
    else:
        errors.append(f'iptables: {se.strip()[:120] or rc}')
    if errors:
        out['ok'] = False
        out['error'] = '; '.join(errors)
    return out


def collect_layer_c(sni: str) -> dict:
    """Exit host in ONE ssh round-trip: the remote python script prints JSON."""
    script = GUARD_HELPERS + REMOTE_C % {'sni': sni}
    rc, so, se = _run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                       EXIT_SSH_HOST, 'python3', '-'], TIMEOUT_C, stdin=script)
    data = parse_exit_json(so)
    if not data['ok']:
        data['error'] = f"{data['error']} (ssh rc={rc}: {se.strip()[:160]})"
    data['cert_record_len'] = parse_cert_record_len(data.get('cert_lines'))
    return data


def collect_all(sni: str) -> dict:
    with ThreadPoolExecutor(max_workers=3) as pool:
        fa = pool.submit(collect_layer_a)
        fb = pool.submit(collect_layer_b)
        fc = pool.submit(collect_layer_c, sni)
        layers = {}
        for name, fut in (('a', fa), ('b', fb), ('c', fc)):
            try:
                layers[name] = fut.result()
            except Exception as e:                # noqa: BLE001 - a layer must never take the run down
                layers[name] = {'ok': False, 'error': f'collector crashed: {e!r}'}
    return layers


# ---------------------------------------------------------------------------
# Assessment (pure)
# ---------------------------------------------------------------------------

def probed_tags(probes) -> List[str]:
    """PROBED + the optional tags that actually have rows in the window.

    hy2t is probed only on deployments with HY2T_PORT set, and this
    script has no access to the bot's config — so the rows decide: layer
    A emits a tag only if outbound_health has rows for it in the last
    PROBE_WINDOW_H hours. No rows → hy2t is judged from layer C alone,
    exactly as before the :18085 inbound existed. Never hardcoded, so a
    deployment without turbo does not get a phantom UNKNOWN probe."""
    out = list(PROBED)
    for tag in PROBED_OPTIONAL:
        if ((probes or {}).get(tag) or {}).get('rows'):
            out.append(tag)
    return out


def _is_alive(row) -> bool:
    """LIVENESS RULE on one [ts, status, latency_ms] row."""
    return (len(row) > 2 and row[2] is not None) or (len(row) > 1 and row[1] == 'ok')


def _latest_run(rows, now: datetime) -> list:
    """The newest run's rows: newest-first rows within RUN_SPAN_MIN of the
    first one (HealthChecker writes a tag's 10 rows within milliseconds;
    runs are 15 min apart). Empty if the newest ts is unparsable."""
    if not rows or not rows[0]:
        return []
    head = _minutes_since(rows[0][0], now)
    if head == float('inf'):
        return []
    latest = []
    for r in rows:
        if not r or _minutes_since(r[0], now) - head > RUN_SPAN_MIN:
            break
        latest.append(r)
    return latest


def _probe_verdict(tag: str, probes: dict, now: datetime) -> dict:
    """State + evidence for one probed protocol from its recent rows.

    ``degraded_reason`` ∈ {probe_rate, dark_run} tells the suspect builder
    WHY (the rate over 3 runs is below half of normal / the newest full
    run had not a single ok)."""
    entry = (probes or {}).get(tag) or {}
    rows = entry.get('rows') or []
    last_alive = entry.get('last_alive')
    if not rows:
        return {'state': 'UNKNOWN', 'evidence': [f'пробы: нет строк за {PROBE_WINDOW_H}ч'],
                'down_for': None, 'ok_rate': None}
    newest_min = _minutes_since(rows[0][0], now) if rows[0] else float('inf')
    if newest_min > PROBE_STALE_MIN:
        # Other tags may still be written (the global pipeline check passed);
        # THIS tag's rows are too old to say anything about now.
        return {'state': 'UNKNOWN',
                'evidence': [f'пробы по {tag} не пишутся: последняя строка {_humanize(newest_min)} назад '
                             f'(порог {PROBE_STALE_MIN} мин) — вердикт по старым строкам не строится'],
                'down_for': None, 'ok_rate': None}
    alive = [r for r in rows if _is_alive(r)]
    ok = [r for r in rows if len(r) > 1 and r[1] == 'ok']
    sample = f'{len(rows)} строк за {PROBE_WINDOW_H}ч, последняя {_humanize(newest_min)} назад'
    if not alive:
        down_for = _humanize(_minutes_since(last_alive, now)) if last_alive else '—'
        ev = [f'пробы: 0/{len(rows)} живых ({sample}) — туннель не устанавливается',
              f'последний живой ответ: {last_alive or "нет в истории"}']
        if len(rows) < PROBE_DOMAINS:
            ev.append('тонкая выборка (< 1 полного прогона) — перепроверь через 15 мин')
        return {'state': 'DOWN', 'evidence': ev, 'down_for': down_for, 'ok_rate': '0/%d' % len(rows)}
    latest = _latest_run(rows, now)
    l_alive = sum(1 for r in latest if _is_alive(r))
    l_ok = sum(1 for r in latest if len(r) > 1 and r[1] == 'ok')
    run_line = f'последний прогон: ok {l_ok}/{len(latest)}, живых {l_alive}/{len(latest)}'
    rate = len(ok) / len(rows)
    ok_rate = f'{len(ok)}/{len(rows)}'
    if len(rows) >= DEGRADED_MIN_SAMPLES and rate < DEGRADED_BELOW:
        return {'state': 'DEGRADED', 'degraded_reason': 'probe_rate',
                'evidence': [f'пробы: ok {ok_rate} ({rate:.0%}; {probe_rules.degraded_threshold_text()}) — '
                             f'туннель жив ({len(alive)} с задержкой), но сайты не проходят; {run_line}; {sample}'],
                'down_for': None, 'ok_rate': ok_rate}
    if len(latest) >= PROBE_DOMAINS and l_ok == 0:
        # The 3-run window still looks fine, but the newest full run has
        # not a single ok (probe_rules.last_run_dark, the early sign
        # /protocols shows as "последний прогон без единого ответа"): an
        # outage that started within the last 45 min looks exactly like
        # this. Never "OK".
        how = ('туннель не установился ни разу' if l_alive == 0
               else f'туннель жив ({l_alive} с задержкой), но ни один сайт не прошёл')
        return {'state': 'DEGRADED', 'degraded_reason': 'dark_run',
                'evidence': [f'пробы: ok {ok_rate} за {PROBE_RUNS} прогона, но {run_line} — {how}; '
                             f'DOWN объявляется после {PROBE_RUNS} тёмных прогонов подряд, перепроверь через 15 мин; {sample}'],
                'down_for': None, 'ok_rate': ok_rate}
    return {'state': 'OK',
            'evidence': [f'пробы: ok {ok_rate}, живых {len(alive)}/{len(rows)}; {run_line}; {sample}'],
            'down_for': None, 'ok_rate': ok_rate}


def _audit_flags(audit: dict) -> dict:
    """Flags from verify_panel_client_fields.py output (rc 0/1/2, lines).

    ``broken_inbounds`` = inbound tags named on problem lines ("<tag> (id=N,
    proto): email has empty 'flow'" / "... must NOT carry 'flow'"), so a
    BROKEN audit can be pinned to the protocol it actually breaks."""
    audit = audit or {}
    lines = [str(x) for x in audit.get('lines') or []]
    text = '\n'.join(lines)
    rc = audit.get('rc')
    broken = set()
    if rc == 1:
        for ln in lines:
            if 'has empty' in ln or 'must NOT carry' in ln:
                m = re.search(r'\b(inbound-\d+)\b', ln)
                if m:
                    broken.add(m.group(1))
    if rc is None:
        fallback = 'аудит не запускался' + (f' ({audit["error"][:120]})' if audit.get('error') else '')
    else:
        fallback = f'rc={rc}'
    return {
        'rc': rc,
        'flow_missing': rc == 1 and "empty 'flow'" in text,
        'password_missing': rc == 1 and "empty 'password'" in text,
        'broken_inbounds': broken,
        'summary': next((ln for ln in lines if ln.startswith(('BROKEN', 'OK', 'CANNOT'))),
                        lines[0] if lines else fallback),
    }


def _hy2_suspects(tag: str, b: dict, c: dict, entry_dnat: set, exit_hop: set) -> List[dict]:
    """Shared Hysteria diagnosis for hy2 and hy2t (both probe-driven when
    rows exist; hy2t falls back to layer C only when it has none)."""
    out = []
    unit = HYSTERIA_UNIT[tag]
    svc = (c.get('services') or {}).get(unit)
    if b.get('ok'):          # only judge NAT when iptables actually answered
        missing = [f'{d}→:{p}' for d, p in ENTRY_DNAT_FOR[tag] if (d, p) not in entry_dnat]
        if missing:
            out.append({'rank': 10, 'protocol': tag,
                        'title': f'entry NAT rule gone — нет UDP DNAT {", ".join(missing)} на entry',
                        'next': "iptables -t nat -S PREROUTING | grep -E '8400|8402|dport 443'  "
                                "# восстановить по docs/PROJECT.md (hy2 DNAT), затем повторить чек"})
    if c.get('ok') and svc and svc != 'active':
        out.append({'rank': 5, 'protocol': tag,
                    'title': f'daemon down — {unit} на exit: {svc}',
                    'next': f"ssh exit-node 'systemctl status {unit} --no-pager; "
                            f"journalctl -u {unit} -n 40 --no-pager'"})
    if c.get('ok') and c.get('nat_prerouting'):    # [] = iptables did not answer, not "no rules"
        missing = [f'{d}→:{p}' for d, p in EXIT_HOP_FOR[tag] if (d, p) not in exit_hop]
        if missing:
            out.append({'rank': 30, 'protocol': tag,
                        'title': f'hop rules gone — нет REDIRECT {", ".join(missing)} на exit '
                                 '(основной порт работает, порт-хоп нет)',
                        'next': "ssh exit-node \"iptables -t nat -S PREROUTING | grep -E '8400|8402'\""})
    return out


# ---------------------------------------------------------------------------
# Exit guard (AGENTS.md §52): what a tunnel user can reach on exit itself.
# Pure functions over layer C's ``guard`` facts (GUARD_HELPERS collects them).
# ---------------------------------------------------------------------------

# xray's freedom checks the address it actually dials against a built-in
# default rule (Xray-core PR #5947, first released in v26.4.15) — for traffic
# from these inbound protocols only; configured finalRules come first.
XRAY_DEFAULT_RULE_SINCE = (26, 4, 15)
XRAY_DEFAULT_RULE_INBOUNDS = ('vless', 'vmess', 'trojan', 'hysteria', 'wireguard')
FREEDOM_PROTOCOLS = ('freedom', 'direct')
HY2_UNITS = ('hysteria', 'hysteria-turbo')
# hysteria runs on the HOST (root, no default block for private addresses):
# its ACL is all that keeps a hy2 user off the host's loopback (Caddy admin
# :2019, hysteria's stats APIs) and the docker bridge (the panel container).
HY2_LOOPBACK_TARGETS = ('127.0.0.1', '::1')
HY2_PRIVATE_TARGETS = ('10.0.0.1', '100.64.0.1', '169.254.169.254', '172.16.0.1',
                       '172.20.0.2', '192.168.0.1', 'fc00::1', 'fe80::1')
GUARD_KEYS_XRAY = ('xray_api_exposed', 'xray_api_polling')
HY2_ACL_NEXT = ('AGENTS.md §52: в {config} блок acl: inline: с reject(127.0.0.0/8), reject(::1/128) '
                'и частными сетями, затем systemctl restart {unit}')


def parse_xray_version(text) -> Optional[Tuple[int, int, int]]:
    m = re.search(r'Xray (\d+)\.(\d+)\.(\d+)', str(text or ''))
    return tuple(int(x) for x in m.groups()) if m else None


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return [x.strip() for x in str(value).split(',') if x.strip()]


def port_spec_has(spec, port: int) -> bool:
    """xray's port syntax: 53 / "53" / "1000-2000" / "53,443,1000-2000"."""
    for part in _as_list(spec):
        lo, sep, hi = part.partition('-')
        try:
            if sep:
                if int(lo) <= port <= int(hi):
                    return True
            elif int(part) == port:
                return True
        except ValueError:
            continue
    return False


def _network_has_tcp(spec) -> bool:
    return spec is None or 'tcp' in [x.lower() for x in _as_list(spec)]


def _ip_entry_covers(entry: str, ip: str, unknown: bool) -> bool:
    """Does one routing / final-rule ``ip`` entry cover ``ip``? ``unknown`` is
    the answer for entries this script cannot evaluate (ext:…, geoip:xx): the
    caller picks the side that cannot hide an exposure."""
    entry = str(entry).strip().lower()
    if entry == 'geoip:private':
        return True
    if entry.startswith(('geoip:', 'ext:', 'ext-ip:')):
        return unknown
    try:
        net = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return unknown
    target = ipaddress.ip_address(ip)
    return target.version == net.version and target in net


def _host_port(listen) -> Tuple[Optional[str], Optional[int]]:
    """'127.0.0.1:11111' / '[::1]:62789' → (host, port)."""
    host, _, port = str(listen or '').rpartition(':')
    try:
        return host.strip('[]') or None, int(port)
    except ValueError:
        return None, None


def _is_loopback_listen(listen) -> bool:
    host = str(listen or '').strip('[]').lower()
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _freedom_guarded(ob: dict, inbound_proto: str, port: int, version) -> Tuple[bool, str]:
    """Does this freedom outbound refuse a dial to 127.0.0.1:``port`` for
    traffic from an ``inbound_proto`` inbound? Configured finalRules first,
    then xray's default rule."""
    rules = ob.get('finalRules')
    if isinstance(rules, str):
        return False, f'finalRules не читаются ({rules[:60]})'
    for fr in rules or []:
        if not isinstance(fr, dict):
            continue
        if not _network_has_tcp(fr.get('network')):
            continue
        if fr.get('port') not in (None, '') and not port_spec_has(fr['port'], port):
            continue
        action = str(fr.get('action') or '').lower()
        ips = _as_list(fr.get('ip'))
        # an allow we cannot read may cover loopback; a block we cannot read
        # is not credited
        if ips and not any(_ip_entry_covers(x, '127.0.0.1', unknown=(action == 'allow'))
                           for x in ips):
            continue
        if action == 'allow':
            return False, 'finalRules: allow перед встроенным правилом'
        if action == 'block':
            return True, 'finalRules: block'
    if version < XRAY_DEFAULT_RULE_SINCE:
        return False, 'xray {} старше 26.4.15 (нет встроенного правила)'.format(
            '.'.join(str(x) for x in version))
    proto = str(inbound_proto or '').lower()
    if proto not in XRAY_DEFAULT_RULE_INBOUNDS and not proto.startswith('shadowsocks'):
        return False, f'у {proto or "?"}-инбаунда нет встроенного правила'
    return True, 'встроенное правило'


def _reachable_outbounds(view: dict, inbound_tag: str, port: int) -> List[str]:
    """Outbound tags where a user's TCP request (from ``inbound_tag``, to a
    name of the user's choice, at ``port``) can end. A rule with conditions
    beyond inbound/port/network may or may not match — the user picks — so
    its outbound is reachable and the walk goes on; the first rule without
    them decides; nothing decisive → the first outbound (xray's default)."""
    outbounds = view.get('outbounds') or []
    balancers = {b.get('tag'): _as_list(b.get('selector')) for b in view.get('balancers') or []}
    reachable: List[str] = []

    def targets(rule):
        if rule.get('balancerTag'):
            prefixes = balancers.get(rule['balancerTag'])
            if prefixes is None:
                return [o.get('tag') for o in outbounds]
            return [o.get('tag') for o in outbounds
                    if any(str(o.get('tag') or '').startswith(p) for p in prefixes)]
        return [rule.get('outboundTag')]

    for rule in view.get('rules') or []:
        if 'inboundTag' in rule and inbound_tag not in _as_list(rule['inboundTag']):
            continue
        if 'port' in rule and not port_spec_has(rule['port'], port):
            continue
        if 'network' in rule and not _network_has_tcp(rule['network']):
            continue
        reachable.extend(targets(rule))
        if not rule.get('other') and 'ip' not in rule:
            return reachable
    if outbounds:
        reachable.append(outbounds[0].get('tag'))
    return reachable


def _inbound_port(ib: dict) -> Optional[int]:
    port = ib.get('port')
    if isinstance(port, int):
        return port
    return int(port) if isinstance(port, str) and port.isdigit() else None


def _local_service_ports(view: dict) -> Dict[int, str]:
    """{port: what} for the xray services on the container's loopback."""
    ports: Dict[int, str] = {}
    api = view.get('api') or {}
    if api.get('listen'):
        host, port = _host_port(api['listen'])
        if port:
            ports[port] = 'API'
    for tag in _api_inbound_tags(view):
        for ib in view.get('inbounds') or []:
            if ib.get('tag') == tag and _inbound_port(ib):
                ports[_inbound_port(ib)] = 'API'
    met = view.get('metrics') or {}
    if met.get('listen'):
        host, port = _host_port(met['listen'])
        if port and _is_loopback_listen(host):
            ports.setdefault(port, 'metrics')
    return ports


def _api_inbound_tags(view: dict) -> List[str]:
    """The inbounds the panel polls the API through: those a rule sends to
    the api outbound, and one tagged like it (3x-ui's ``api``) even when no
    rule does — then nothing serves its polling."""
    api_tag = (view.get('api') or {}).get('tag')
    tags: List[str] = []
    for rule in view.get('rules') or []:
        if api_tag and rule.get('outboundTag') == api_tag:
            tags.extend(_as_list(rule.get('inboundTag')))
    if api_tag and api_tag not in tags and any(
            ib.get('tag') == api_tag for ib in view.get('inbounds') or []):
        tags.append(api_tag)
    return tags


def xray_guard_verdict(view: dict, version) -> dict:
    """``exposed``: [(inbound, protocol, port, service, outbound, why)] —
    user inbounds whose traffic can dial a local xray service through a
    freedom that does not refuse loopback. ``polling``: the rule that takes
    the panel's own API traffic away from the api outbound (None = fine)."""
    out = {'exposed': [], 'polling': None, 'ports': _local_service_ports(view)}
    by_tag = {o.get('tag'): o for o in view.get('outbounds') or []}
    api_inbounds = set(_api_inbound_tags(view))
    for ib in view.get('inbounds') or []:
        tag = ib.get('tag')
        if not tag or tag in api_inbounds or _is_loopback_listen(ib.get('listen')):
            continue
        if str(ib.get('protocol') or '').lower() in ('dokodemo-door', 'tunnel'):
            continue                           # its client cannot pick the destination
        for port, what in sorted(out['ports'].items()):
            for ob_tag in _reachable_outbounds(view, tag, port):
                ob = by_tag.get(ob_tag) or {}
                proto = str(ob.get('protocol') or '').lower()
                if proto == 'loopback':
                    why = 'loopback-аутбаунд (трафик заходит в роутинг заново)'
                elif proto in FREEDOM_PROTOCOLS:
                    guarded, why = _freedom_guarded(ob, ib.get('protocol'), port, version)
                    if guarded:
                        continue
                else:
                    continue
                out['exposed'].append((tag, ib.get('protocol'), port, what, ob_tag, why))
                break
    api_tag = (view.get('api') or {}).get('tag')
    if api_inbounds and not (view.get('api') or {}).get('listen'):
        for tag in sorted(api_inbounds):
            port = next((_inbound_port(ib) for ib in view.get('inbounds') or []
                         if ib.get('tag') == tag), None)
            if not port:
                continue
            decided = None
            for i, rule in enumerate(view.get('rules') or []):
                if 'inboundTag' in rule and tag not in _as_list(rule['inboundTag']):
                    continue
                if 'port' in rule and not port_spec_has(rule['port'], port):
                    continue
                if 'network' in rule and not _network_has_tcp(rule['network']):
                    continue
                if rule.get('other'):
                    continue            # domain/user/source…: not this request, or not knowable
                if 'ip' in rule and not any(_ip_entry_covers(x, '127.0.0.1', unknown=False)
                                            for x in _as_list(rule['ip'])):
                    continue
                decided = (i, rule.get('outboundTag') or rule.get('balancerTag'))
                break
            if decided is None:
                outs = view.get('outbounds') or []
                decided = (None, outs[0].get('tag') if outs else None)
            if decided[1] != api_tag:
                out['polling'] = decided
                break
    return out


_HY_RULE = re.compile(r'([A-Za-z_][\w-]*)\s*\(([^()]*)\)')


def parse_hysteria_acl(info) -> Tuple[Optional[List[Tuple[str, str, str]]], Optional[str]]:
    """The ACL rules [(outbound, address, proto/port)] in order, or (None,
    why) when they could not be read. No acl → []: everything goes out."""
    if not isinstance(info, dict) or info.get('error') or info.get('missing'):
        info = info if isinstance(info, dict) else {}
        return None, str(info.get('error') or info.get('missing') or 'нет данных')[:160]
    texts: List[str] = []
    if 'acl_json' in info:
        acl = info['acl_json']
        if isinstance(acl, dict):
            inline = acl.get('inline') or []
            texts.extend(str(x) for x in (inline if isinstance(inline, list) else [inline]))
        elif acl is not None:
            return None, f'acl не объект: {str(acl)[:80]}'
    elif 'acl_lines' in info:
        texts.extend(str(line).split('#', 1)[0] for line in info.get('acl_lines') or [])
    else:
        return None, 'нет данных об acl'
    if info.get('acl_file'):
        if info.get('acl_file_error') or not isinstance(info.get('acl_file_lines'), list):
            return None, f"файл ACL {info['acl_file']}: {info.get('acl_file_error') or 'не прочитан'}"
        texts.extend(str(line).split('#', 1)[0] for line in info['acl_file_lines'])
    rules = []
    for text in texts:
        for m in _HY_RULE.finditer(text):
            args = [a.strip().strip('"\'').lower() for a in m.group(2).split(',')]
            rules.append((m.group(1).lower(), args[0] if args else '',
                          args[1] if len(args) > 1 else ''))
    return rules, None


def _hy_addr_matches(addr: str, ip: str) -> bool:
    if addr in ('all', '*'):
        return True
    try:
        net = ipaddress.ip_network(addr, strict=False)
    except ValueError:
        return False                       # names, wildcards, suffix:, geoip:, geosite:
    target = ipaddress.ip_address(ip)
    return target.version == net.version and target in net


def _hy_tcp_scope(protoport: str) -> str:
    """How much of TCP a rule's protocol/port part covers: all, some, none."""
    if protoport in ('', '*', '*/*', 'tcp', 'tcp/*'):
        return 'all'
    proto = protoport.partition('/')[0]
    return 'none' if proto == 'udp' else 'some'


def hysteria_uncovered(rules, targets) -> List[str]:
    """The ``targets`` a hy2 user still reaches over TCP: the first rule that
    covers all of TCP decides; an earlier non-reject for some ports leaves
    those ports open; nothing decisive → the default outbound (direct)."""
    out = []
    for ip in targets:
        decided = None
        for outbound, addr, protoport in rules:
            if not _hy_addr_matches(addr, ip):
                continue
            scope = _hy_tcp_scope(protoport)
            if scope == 'none':
                continue
            if scope == 'all':
                decided = outbound == 'reject'
                break
            if outbound != 'reject':
                decided = False
                break
        if decided is not True:
            out.append(ip)
    return out


def exit_guard(c: dict) -> Tuple[List[dict], List[str]]:
    """(findings, unjudged keys) from layer C's ``guard`` facts. A key this
    run could not look at is ``unjudged``: the bot keeps its alert as it was
    instead of reading the missing finding as "fixed"."""
    guard = c.get('guard')
    all_keys = [f'hy2_acl:{u}' for u in HY2_UNITS] + list(GUARD_KEYS_XRAY)
    if not isinstance(guard, dict) or not guard:
        return [], all_keys                    # a collector without the guard
    if guard.get('error'):
        return ([{'key': 'exit_guard_unreadable', 'severity': 'warn', 'layer': 'c',
                  'title': f"проверка доступа из туннеля к exit упала: {str(guard['error'])[:160]}",
                  'next': "ssh exit-node 'systemctl cat hysteria; ss -ltnH'"}], all_keys)
    findings: List[dict] = []
    unjudged: List[str] = []
    blind: List[str] = []
    listen = guard.get('listen_2019')
    caddy = ''
    if isinstance(listen, list) and listen:
        caddy = f"; Caddy admin слушает {', '.join(listen[:3])}"
    services = c.get('services') or {}
    for unit in HY2_UNITS:
        state = services.get(unit)
        if state in ('inactive', 'failed'):
            continue                           # a stopped hysteria serves nobody
        if state not in ('active', 'activating', 'reloading', 'deactivating'):
            unjudged.append(f'hy2_acl:{unit}')
            blind.append(f'{unit}: состояние юнита {str(state)[:60]!r}')
            continue
        info = (guard.get('hysteria') or {}).get(unit)
        rules, err = parse_hysteria_acl(info)
        if rules is None:
            unjudged.append(f'hy2_acl:{unit}')
            blind.append(f'{unit}: {err}')
            continue
        config = (info or {}).get('config') or '/etc/hysteria/config.yaml'
        nxt = HY2_ACL_NEXT.format(config=config, unit=unit)
        lo = hysteria_uncovered(rules, HY2_LOOPBACK_TARGETS)
        if lo:
            what = 'нет ACL' if not rules else f"ACL не закрывает {', '.join(lo)}"
            findings.append({'key': f'hy2_acl:{unit}', 'severity': 'critical', 'layer': 'c',
                             'title': (f'{unit}: {what} — hy2-юзеры достают loopback хоста exit'
                                       f'{caddy}'),
                             'next': nxt})
            continue
        priv = hysteria_uncovered(rules, HY2_PRIVATE_TARGETS)
        if priv:
            findings.append({'key': f'hy2_acl:{unit}', 'severity': 'warn', 'layer': 'c',
                             'title': f"{unit}: ACL не закрывает частные сети ({', '.join(priv)})",
                             'next': nxt})
    view = guard.get('xray')
    version = parse_xray_version(guard.get('xray_version'))
    if not isinstance(view, dict) or version is None:
        unjudged.extend(GUARD_KEYS_XRAY)
        blind.append('xray: ' + ('нет config.json' if not isinstance(view, dict)
                                 else f"версия не читается ({str(guard.get('xray_version'))[:60]})"))
    else:
        verdict = xray_guard_verdict(view, version)
        block_tag = next((o.get('tag') for o in view.get('outbounds') or []
                          if o.get('protocol') == 'blackhole'), '<blackhole>')
        ports = ','.join(str(p) for p in sorted(verdict['ports'])) or '62789'
        rule = f'{{"type": "field", "port": "{ports}", "outboundTag": "{block_tag}"}}'
        if verdict['exposed']:
            shown = '; '.join(f'{ib} ({proto}) → {ob} → :{port} {what}: {why}'
                              for ib, proto, port, what, ob, why in verdict['exposed'][:3])
            findings.append({'key': 'xray_api_exposed', 'severity': 'critical', 'layer': 'c',
                             'title': f'API xray панели достаётся из туннеля: {shown}',
                             'next': f'в шаблон xray панели сразу ПОСЛЕ api-правила: {rule} '
                                     '(AGENTS.md §52), затем перезапуск xray'})
        if verdict['polling']:
            index, outbound = verdict['polling']
            where = f'правило №{index + 1}' if index is not None else 'нет api-правила, маршрут по умолчанию'
            findings.append({'key': 'xray_api_polling', 'severity': 'critical', 'layer': 'c',
                             'title': (f'опрос API панелью уходит в {outbound} ({where}): '
                                       'учёт трафика стоит'),
                             'next': 'в шаблоне xray панели api-правило (inboundTag api → api) '
                                     'должно быть первым (AGENTS.md §52, §26)'})
    if blind:
        findings.append({'key': 'exit_guard_unreadable', 'severity': 'warn', 'layer': 'c',
                         'title': 'проверка доступа из туннеля к exit не прочитала: ' + '; '.join(blind),
                         'next': "ssh exit-node 'systemctl cat hysteria hysteria-turbo; "
                                 "docker exec 3x-ui /app/bin/xray-linux-amd64 version'"})
    return findings, unjudged


# The servers scripts/setup_ssh_exit.sh and setup_xhr_exit.sh run on exit
# (AGENTS.md §51): container name → the cascade protocol it serves (None =
# not a cascade protocol: SSH is only in the emergency profiles). Each has a
# root timer that syncs its users from the bot every 2 min (OnUnitActiveSec).
EXIT_SERVERS = {'ssh-exit': None, 'xhr-exit': 'xhr'}
EXIT_SYNC_STALE_S = 10 * 60          # five missed syncs


def parse_systemctl_show(text) -> Dict[str, Dict[str, str]]:
    """``{Id: {property: value}}`` from ``systemctl show`` of several units
    (one blank-line separated block per unit)."""
    units: Dict[str, Dict[str, str]] = {}
    cur: Dict[str, str] = {}
    for line in str(text or '').splitlines() + ['']:
        line = line.strip()
        if not line:
            if cur.get('Id'):
                units[cur['Id']] = cur
            cur = {}
            continue
        key, sep, value = line.partition('=')
        if sep:
            cur[key] = value
    return units


def _exit_server_state(name: str, info: dict, uptime_s: Optional[float]):
    """``(container, sync)`` for one server: each a finding title (str), ''
    for "fine", or None for "could not look". Pure."""
    ins = info.get('inspect') if isinstance(info.get('inspect'), dict) else {}
    units = parse_systemctl_show(info.get('show')) if info.get('show_rc') == 0 else {}
    svc, timer = units.get(f'{name}-sync.service'), units.get(f'{name}-sync.timer')
    sync_installed = None
    if svc and timer:
        sync_installed = not (svc.get('LoadState') == 'not-found'
                              and timer.get('LoadState') == 'not-found')
    # --- the container
    if ins.get('rc') == 0:
        status, _, restarting = str(ins.get('out') or '').partition(' ')
        if status == 'running':
            container = ''
        elif status:
            container = (f'контейнер {name} на exit: {status}'
                         + (' (перезапускается по кругу)' if restarting.strip() == 'true' else ''))
        else:
            container = None
    elif 'No such object' in str(ins.get('err') or ''):
        if sync_installed is False:
            return '', ''                      # never installed on this exit
        container = (f'контейнера {name} на exit нет, а его синхронизация стоит'
                     if sync_installed else None)
    else:
        container = None
    # --- its user sync
    if sync_installed is None:
        sync = None
    elif not sync_installed:
        sync = f'у {name} на exit нет таймера синхронизации юзеров'
    elif timer.get('ActiveState') != 'active':
        sync = f'таймер {name}-sync на exit: {timer.get("ActiveState") or "?"}'
    elif svc.get('ActiveState') in ('activating', 'deactivating', 'reloading'):
        sync = None                            # mid-run: no finished result to read
    elif svc.get('ActiveState') == 'failed' or svc.get('Result') != 'success':
        sync = f'синхронизация юзеров {name} падает ({svc.get("Result") or "?"})'
    elif uptime_s is None:
        sync = None
    else:
        try:
            exited = int(svc.get('ExecMainExitTimestampMonotonic') or 0) / 1e6
        except ValueError:
            return container, None
        age = uptime_s - exited               # exited 0 = not once since boot
        if age > EXIT_SYNC_STALE_S:
            sync = (f'синхронизация юзеров {name} не отрабатывала {_humanize(age / 60)}'
                    if exited else f'синхронизация юзеров {name} не запускалась с загрузки exit')
        else:
            sync = ''
    return container, sync


def exit_servers(c: dict, off) -> Tuple[List[dict], List[str]]:
    """(findings, unjudged keys) for the servers in ``EXIT_SERVERS``: the
    container (``exit_server:<name>``) and its user sync (``exit_sync:<name>``).
    ``off`` = the protocols switched off in the cascade, or None when layer A
    could not read it. A server whose protocol is switched off serves nobody
    on purpose: nothing to say. One that was never installed (no container,
    no sync unit) neither. xhr-exit down while xhr is on is critical: every
    xhr user is cut off; the rest is warn (SSH is only the emergency path, a
    stuck sync only freezes the user list)."""
    keys = [k for name in EXIT_SERVERS for k in (f'exit_server:{name}', f'exit_sync:{name}')]
    srv = c.get('exit_servers')
    if not isinstance(srv, dict):
        return [], keys                        # a collector without these facts
    try:
        uptime_s: Optional[float] = float(str(srv.get('uptime') or '').split()[0])
    except (ValueError, IndexError):
        uptime_s = None
    findings: List[dict] = []
    unjudged: List[str] = []
    for name, proto in EXIT_SERVERS.items():
        server_key, sync_key = f'exit_server:{name}', f'exit_sync:{name}'
        if proto is not None:
            if off is None:
                unjudged += [server_key, sync_key]   # whether it matters is unknown
                continue
            if proto in off:
                continue
        info = srv.get(name)
        if not isinstance(info, dict):
            unjudged += [server_key, sync_key]
            continue
        container, sync = _exit_server_state(name, info, uptime_s)
        if container is None:
            unjudged.append(server_key)
        elif container:
            findings.append({'key': server_key, 'layer': 'c',
                             'severity': 'critical' if proto else 'warn',
                             'title': container,
                             'next': f"ssh exit-node 'docker ps -a --filter name={name}; "
                                     f"docker logs --tail 40 {name}'"})
        if sync is None:
            unjudged.append(sync_key)
        elif sync:
            findings.append({'key': sync_key, 'layer': 'c', 'severity': 'warn',
                             'title': sync,
                             'next': f"ssh exit-node 'systemctl status {name}-sync.timer "
                                     f"{name}-sync.service --no-pager; "
                                     f"journalctl -u {name}-sync -n 40 --no-pager'"})
    return findings, unjudged


def _cascade_off(layers: dict):
    """The protocols switched off in the cascade, or None without layer A."""
    a = layers.get('a') or {}
    return probe_rules.operator_disabled(a.get('cascade_order')) if a.get('ok') else None


def unjudged_for(layers: dict) -> List[str]:
    """Finding keys this run could not look at, while their layer did look."""
    c = layers.get('c') or {}
    if not c.get('ok'):
        return []
    return exit_guard(c)[1] + exit_servers(c, _cascade_off(layers))[1]


# Findings (IMPROVEMENT_PLAN F, D1): facts that break users BEFORE the probes
# go dark, judged on their own, whatever the probe verdict says. The bot turns
# each into an alert ``healthcheck:<key>`` when this script runs on its timer
# (systemd/protocol-healthcheck.timer, --post). Severity: what is already
# broken for users is critical, what is about to be is warn.
CERT_WARN_BYTES = 7000       # the dest cert is creeping towards the 8192 buffer


def findings_for(layers: dict) -> List[dict]:
    """``[{key, severity, title, next, layer}]`` from the raw layers. Pure.
    ``layer`` is the one that looked ('a' bot db, 'b' entry, 'c' exit): the
    bot keeps a finding's alert as it is while its layer could not look."""
    a = layers.get('a') or {}
    b = layers.get('b') or {}
    c = layers.get('c') or {}
    out: List[dict] = []
    # Protocols the operator switched off (hy2t since 2026-09-21): their
    # units and rules may be gone on purpose, nothing to page about.
    off = probe_rules.operator_disabled(a.get('cascade_order')) if a.get('ok') else set()
    audit = _audit_flags(a.get('audit') or {}) if a.get('ok') else None
    if audit and audit['rc'] == 1:
        where = ', '.join(sorted(audit['broken_inbounds'])) or 'см. строки аудита'
        out.append({'key': 'panel_audit', 'severity': 'critical', 'layer': 'a',
                    'title': f'аудит панели: поля клиентов сломаны ({where})',
                    'next': 'docker exec -e PYTHONPATH=/app vpn-bot python3 '
                            '/app/scripts/verify_panel_client_fields.py'})
    if c.get('ok'):
        cert_len = c.get('cert_record_len')
        if cert_len is None:
            cert_len = parse_cert_record_len(c.get('cert_lines'))
        sni = c.get('cert_sni') or DEFAULT_SNI
        if cert_len is not None and cert_len > CERT_WARN_BYTES:
            over = cert_len > CERT_LIMIT_BYTES
            out.append({'key': 'reality_dest_cert', 'severity': 'critical' if over else 'warn',
                        'layer': 'c',
                        'title': (f'сертификат Reality-dest {sni}: {cert_len} байт '
                                  f'(предел xray 8192, порог {CERT_LIMIT_BYTES})'),
                        'next': 'scripts/rotate_reality_dest.py --sni <host>  # см. AGENTS.md §23'})
        for tag, unit in HYSTERIA_UNIT.items():
            svc = (c.get('services') or {}).get(unit)
            if svc and svc != 'active' and tag not in off:
                out.append({'key': f'unit:{unit}', 'layer': 'c',
                            'severity': 'critical' if tag == 'hy2' else 'warn',
                            'title': f'{unit} на exit: {svc}',
                            'next': f"ssh exit-node 'systemctl status {unit} --no-pager'"})
        if c.get('nat_prerouting'):
            hop = parse_redirect_rules(c.get('nat_prerouting'))
            for tag, rules in EXIT_HOP_FOR.items():
                missing = [f'{d}→:{p}' for d, p in rules if (d, p) not in hop]
                if missing and tag not in off:
                    out.append({'key': f'exit_hop:{tag}', 'severity': 'warn', 'layer': 'c',
                                'title': f'{tag}: нет REDIRECT {", ".join(missing)} на exit (порт-хоп)',
                                'next': "ssh exit-node \"iptables -t nat -S PREROUTING | grep -E '8400|8402'\""})
        out.extend(exit_guard(c)[0])
        out.extend(exit_servers(c, _cascade_off(layers))[0])
    if b.get('ok'):
        dnat = parse_dnat_rules(b.get('nat_prerouting'))
        for tag, rules in ENTRY_DNAT_FOR.items():
            missing = [f'{d}→:{p}' for d, p in rules if (d, p) not in dnat]
            if missing and tag not in off:
                out.append({'key': f'entry_dnat:{tag}', 'layer': 'b',
                            'severity': 'critical' if tag == 'hy2' else 'warn',
                            'title': f'{tag}: нет UDP DNAT {", ".join(missing)} на entry',
                            'next': "iptables -t nat -S PREROUTING | grep -E '8400|8402|dport 443'"})
        stls = (b.get('services') or {}).get('shadow-tls')
        if stls and stls != 'active':
            out.append({'key': 'unit:shadow-tls', 'severity': 'critical', 'layer': 'b',
                        'title': f'shadow-tls на entry: {stls}',
                        'next': 'systemctl status shadow-tls --no-pager'})
    return out


def assess(layers: dict, now: Optional[datetime] = None) -> dict:
    """Pure: three layer dicts → report with per-protocol state, evidence,
    ranked suspects, verdict line and exit code. Never raises on missing
    keys — a half-collected layer must still produce a report."""
    # Naive UTC, like the rows HealthChecker writes (datetime.utcnow().isoformat()).
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    a = layers.get('a') or {}
    b = layers.get('b') or {}
    c = layers.get('c') or {}
    a_ok = bool(a.get('ok'))
    b_ok = bool(b.get('ok'))
    c_ok = bool(c.get('ok'))

    probes = a.get('probes') or {}
    probed = probed_tags(probes)          # PROBED (+ hy2t when it has rows)
    hy2t_probed = 'hy2t' in probed
    audit = _audit_flags(a.get('audit') or {})
    entry_dnat = parse_dnat_rules(b.get('nat_prerouting'))
    exit_hop = parse_redirect_rules(c.get('nat_prerouting'))
    exit_nat_known = c_ok and bool(c.get('nat_prerouting'))   # a real dump always has '-P PREROUTING …'
    runtime = c.get('runtime') or {}
    config = c.get('config') or {}
    accepted = c.get('accepted') or {}
    last_acc = c.get('last_accepted') or {}
    cert_len = c.get('cert_record_len')
    if cert_len is None:
        cert_len = parse_cert_record_len(c.get('cert_lines'))
    cert_sni = c.get('cert_sni') or DEFAULT_SNI

    protocols: Dict[str, dict] = {}
    suspects: List[dict] = []

    # --- probe pipeline staleness (layer A) ---------------------------------
    newest = a.get('newest_ts')
    stale_min = _minutes_since(newest, now) if newest else float('inf')
    pipeline_stale = a_ok and (not newest or stale_min > PROBE_STALE_MIN)

    probe_state: Dict[str, str] = {}      # the probe verdict alone, before layer C / audit
    for tag in probed:
        if not a_ok:
            protocols[tag] = {'state': 'UNKNOWN', 'down_for': None, 'ok_rate': None,
                              'evidence': [f'слой A недоступен: {a.get("error") or "нет данных"} — '
                                           'вердикт по пробам невозможен']}
        elif pipeline_stale:
            age = _humanize(stale_min) if newest else 'никогда'
            protocols[tag] = {'state': 'UNKNOWN', 'down_for': None, 'ok_rate': None,
                              'evidence': [f'пробы не пишутся: последняя строка outbound_health — {age}']}
        else:
            protocols[tag] = _probe_verdict(tag, probes, now)
        protocols[tag]['probe_coverage'] = True
        probe_state[tag] = protocols[tag]['state']

    if pipeline_stale:
        suspects.append({'rank': 1, 'protocol': '*',
                         'title': 'probe pipeline dead — HealthChecker не пишет в outbound_health, '
                                  'падение любого протокола сейчас невидимо',
                         'next': "docker logs --since 2h vpn-bot 2>&1 | grep -iE 'health|probe|outbound' | tail -30; "
                                 "docker inspect -f '{{.State.Status}}' probe-proxy"})

    # --- layer C evidence per inbound (works even when A failed) ------------
    def _exit_evidence(tag: str) -> List[str]:
        ib = INBOUND_FOR.get(tag)
        if not ib or not c_ok:
            return []
        rt = runtime.get(ib) or {}
        cf = config.get(ib) or {}
        ev = []
        if 'error' in rt:
            ev.append(f'exit {ib}: xray api не ответил ({rt["error"][:80]})')
        else:
            line = f'exit {ib}: runtime {rt.get("count", "?")} юзеров'
            if tag == 'reality':
                line += f' / {rt.get("with_flow", "?")} с flow'
            line += f'; config.json {cf.get("count", "?")}'
            if tag == 'reality':
                line += f' / {cf.get("with_flow", "?")} с flow'
            ev.append(line)
        n = accepted.get(ib, 0)
        ev.append(f'accepted среди последних 5000 accepted-строк access.log: {n}'
                  + (f' (последний {last_acc[ib]} по часам exit)' if last_acc.get(ib) else ''))
        return ev

    for tag in ('reality', 'ws', 'stls'):
        protocols[tag]['evidence'].extend(_exit_evidence(tag))
    if 'reality' in protocols and c_ok:
        if cert_len is not None:
            protocols['reality']['evidence'].append(
                f'cert {cert_sni}: Certificate record {cert_len} байт (лимит {CERT_LIMIT_BYTES})')
        else:
            protocols['reality']['evidence'].append(f'cert {cert_sni}: размер не измерен (openssl не ответил)')
    if a_ok:
        protocols['reality']['evidence'].append(f'аудит панели: {audit["summary"]}')
        protocols['stls']['evidence'].append(f'аудит панели: {audit["summary"]}')
        # A BROKEN audit means real clients cannot use that inbound even
        # when the probe (one client of 81) is fine: 2026-09-01 left exactly
        # one Reality client with flow. Green probes must not outvote it.
        for ib in sorted(audit['broken_inbounds']):
            tag = PROTOCOL_OF_INBOUND.get(ib)
            if tag and tag in protocols:
                protocols[tag]['evidence'].append(
                    f'аудит панели BROKEN на {ib}: у части клиентов поле не годится для протокола — '
                    'они не подключатся, даже если зонд жив')
                if protocols[tag]['state'] == 'OK':
                    protocols[tag]['state'] = 'DEGRADED'
                    protocols[tag]['degraded_reason'] = 'audit'
    for tag in ('hy2', 'hy2t'):
        if c_ok:
            unit = HYSTERIA_UNIT[tag]
            protocols.setdefault(tag, {'state': 'UNKNOWN', 'evidence': [], 'down_for': None, 'ok_rate': None})
            conns = (c.get('hy2_connections_1h') or {}).get(unit)
            protocols[tag]['evidence'].append(
                f'exit: {unit} {(c.get("services") or {}).get(unit, "?")}, '
                f'client connected за 1ч: {"?" if conns is None else conns} '
                '(долгие QUIC-сессии → 0 не значит падение)')

    # --- hy2t: layer C (+ entry DNAT) --------------------------------------------
    # Without probe rows it is the ONLY source (pre-18085 behaviour). With
    # rows the probe verdict (set in the loop above) leads and layer C is a
    # FLOOR: a dead hysteria-turbo / missing DNAT :8402 was DOWN before the
    # probe existed and stays DOWN — a live probe row proves the tunnel the
    # probe took, not that the daemon users are pointed at is up. Layer C
    # can only worsen the probe verdict, never lift it: UNKNOWN probes plus
    # a fine exit stay UNKNOWN (never a pass by omission).
    hy2t = protocols.setdefault('hy2t', {'state': 'UNKNOWN', 'evidence': [], 'down_for': None, 'ok_rate': None})
    hy2t['probe_coverage'] = hy2t_probed
    if not hy2t_probed:
        hy2t['evidence'].insert(0, f'no probe coverage — строк hy2t в outbound_health за {PROBE_WINDOW_H}ч нет '
                                   '(HY2T_PORT пуст или probe-proxy без inbound :18085 — '
                                   'см. scripts/gen_probe_config.py)')
    if c_ok:
        svc = (c.get('services') or {}).get('hysteria-turbo')
        # Without layer B the entry DNAT is unverified; without the exit NAT
        # dump the hop rules are. Definite failures still win, but "OK"
        # needs every fact on the table — an unverified path is UNKNOWN.
        main_dnat_missing = b_ok and ('8402', '8402') not in entry_dnat
        hop_missing = (exit_nat_known and not all(x in exit_hop for x in EXIT_HOP_FOR['hy2t'])) or (
            b_ok and ('40001:50000', '8402') not in entry_dnat)
        c_note = None
        if svc != 'active' or main_dnat_missing:
            c_state = 'DOWN'
        elif hop_missing:
            c_state = 'DEGRADED'
        elif not b_ok:
            c_state = 'UNKNOWN'
            c_note = (f'entry DNAT не проверен (слой B: {b.get("error") or "нет данных"}) — '
                      'демон активен, но путь с entry не подтверждён')
        elif not exit_nat_known:
            c_state = 'UNKNOWN'
            c_note = ('hop-правила на exit не проверены (iptables на exit не ответил: '
                      f'{(c.get("errors") or {}).get("nat") or "пустой вывод"})')
        else:
            c_state = 'OK'
        if not hy2t_probed:
            hy2t['state'] = c_state
            if c_note:
                hy2t['evidence'].append(c_note)
        elif c_state in ('DOWN', 'DEGRADED') and STATE_ORDER[c_state] > STATE_ORDER[hy2t['state']]:
            hy2t['state'] = c_state
            if c_state == 'DEGRADED':
                # Not a probe-rate problem: the hop suspect below names it,
                # the generic "ok ниже половины нормы" line must not.
                hy2t['degraded_reason'] = 'layer_c'
    else:
        hy2t['evidence'].append(f'слой C недоступен: {c.get("error") or "нет данных"}')

    # --- entry-side evidence ---------------------------------------------------
    if not b_ok:
        for tag in ('hy2', 'hy2t'):
            protocols[tag]['evidence'].append(f'entry DNAT не проверен: слой B недоступен ({b.get("error") or "нет данных"})')
    if b.get('ok'):
        svc_b = b.get('services') or {}
        cont = b.get('containers') or {}
        protocols['stls']['evidence'].append(f'entry: shadow-tls {svc_b.get("shadow-tls", "?")}')
        protocols['reality']['evidence'].append(f'entry: haproxy {svc_b.get("haproxy", "?")}')
        for tag in ('hy2', 'hy2t'):
            missing = [f'{d}→:{p}' for d, p in ENTRY_DNAT_FOR[tag] if (d, p) not in entry_dnat]
            protocols[tag]['evidence'].append(
                'entry DNAT: ' + ('все правила на месте' if not missing else 'НЕТ ' + ', '.join(missing)))
        if cont.get('probe-proxy') != 'running':
            for tag in probed:
                protocols[tag]['evidence'].append(f'entry: контейнер probe-proxy {cont.get("probe-proxy")} — '
                                                  'пробы слепы независимо от протокола')

    # --- protocols switched off in the cascade (see the docstring) ----------
    off = probe_rules.operator_disabled(a.get('cascade_order')) if a_ok else frozenset()
    for tag in sorted(off):
        p = protocols.get(tag)
        if not p:
            continue
        p['evidence'].insert(0, f'выключен оператором в каскаде — вне вердикта '
                                f'(сам по себе был бы {p["state"]})')
        p['state'] = 'OFF'
        p['down_for'] = None
        p.pop('degraded_reason', None)
    # Answers through a switched-off protocol still prove the channel up (the
    # pager agrees); the DE reserve does not: it is meant to outlive exit.
    alive_switched_off = any(probe_state.get(t) in ('OK', 'DEGRADED')
                             for t in off if t != probe_rules.RESERVE_TAG)

    # --- suspects ---------------------------------------------------------------
    states = {t: protocols[t]['state'] for t in REPORT_ORDER if t in protocols}
    # The DE reserve is meant to outlive exit: alive while every cascade
    # protocol is dark it is that same upstream incident (the pager agrees).
    cascade_probed = [t for t in probed if t != probe_rules.RESERVE_TAG and t not in off]
    probed_down = [t for t in cascade_probed if states.get(t) == 'DOWN']
    all_dark = (len(cascade_probed) >= 2 and len(probed_down) == len(cascade_probed)
                and not alive_switched_off)

    if all_dark:
        pp = (b.get('containers') or {}).get('probe-proxy')
        hint = f' — контейнер probe-proxy: {pp}' if b.get('ok') and pp != 'running' else ''
        suspects.append({'rank': 1, 'protocol': '*',
                         'title': 'upstream: probe-proxy / entry→exit link / exit host — все протоколы '
                                  'молчат разом, это ОДИН инцидент, а не четыре inbound\'а' + hint,
                         'next': "docker inspect -f '{{.State.Status}}' probe-proxy; docker logs --tail 40 probe-proxy; "
                                 "ssh -o ConnectTimeout=8 exit-node 'uptime; docker ps --format \"{{.Names}} {{.Status}}\"'"})
    else:
        # Audit-driven suspects do not wait for the probes to go dark: the
        # probe is one client; the audit speaks for all of them.
        if audit['flow_missing']:
            suspects.append({'rank': 1, 'protocol': 'reality',
                             'title': 'flow wipe (месячная джоба / правка клиента) — у клиентов Reality пустой flow, '
                                      'без xtls-rprx-vision handshake невозможен',
                             'next': 'docker exec -e PYTHONPATH=/app vpn-bot python3 /app/scripts/restore_reality_flow.py --apply'
                                     '  # включает панельный restart_xray(); сначала бэкап: '
                                     "ssh exit-node 'docker cp 3x-ui:/etc/x-ui/x-ui.db /opt/backups/x-ui.db.$(date +%F-%H%M%S)'"})
        if audit['password_missing']:
            suspects.append({'rank': 8, 'protocol': 'stls',
                             'title': 'password wipe — у SS-2022 клиентов пустой password (аудит панели)',
                             'next': 'docker exec -e PYTHONPATH=/app vpn-bot python3 /app/scripts/verify_panel_client_fields.py'})
        if audit['rc'] == 1 and not audit['flow_missing'] and not audit['password_missing']:
            where = ', '.join(sorted(audit['broken_inbounds'])) or 'см. строки аудита'
            suspects.append({'rank': 12, 'protocol': '*',
                             'title': f'аудит панели BROKEN ({where}) — поле клиента не годится для протокола inbound\'а',
                             'next': 'docker exec -e PYTHONPATH=/app vpn-bot python3 /app/scripts/verify_panel_client_fields.py'})

        if states.get('reality') == 'DOWN':
            rt = runtime.get('inbound-443') or {}
            cf = config.get('inbound-443') or {}
            if audit['flow_missing']:
                pass                                    # already suspect #1 above
            elif audit['rc'] == 2 or audit['rc'] is None:
                suspects.append({'rank': 12, 'protocol': 'reality',
                                 'title': 'аудит панели не отработал — проверь flow вручную',
                                 'next': 'docker exec -e PYTHONPATH=/app vpn-bot python3 /app/scripts/verify_panel_client_fields.py'})
            if cert_len is not None and cert_len > CERT_LIMIT_BYTES:
                suspects.append({'rank': 15, 'protocol': 'reality',
                                 'title': f'Reality dest cert outgrew 8192 buffer — {cert_sni} отдаёт Certificate '
                                          f'{cert_len} байт; сменить dest/SNI в 3 местах (панель inbound-443 dest/serverNames, '
                                          'HAProxy ACL is_reality_sni на entry, SNI_VALUE в /opt/vpn-bot/.env + рестарт бота)',
                                 'next': "for h in www.bing.com dl.google.com www.cloudflare.com; do printf '%s ' $h; "
                                         "timeout 20 openssl s_client -connect $h:443 -servername $h -tls1_3 -msg </dev/null 2>/dev/null "
                                         "| grep -oE 'Handshake \\[length [0-9a-f]+\\], Certificate'; done  # взять тот, что <= 0x1f40"})
            if 'error' not in rt and cf and (
                    rt.get('with_flow', 0) < cf.get('with_flow', 0) or rt.get('count', 0) < cf.get('count', 0)):
                suspects.append({'rank': 20, 'protocol': 'reality',
                                 'title': f'panel hot-apply drift — в рантайме xray {rt.get("count")} юзеров / '
                                          f'{rt.get("with_flow")} с flow против {cf.get("count")} / {cf.get("with_flow")} в config.json; '
                                          'restart xray via panel',
                                 'next': 'docker exec -e PYTHONPATH=/app vpn-bot python3 -c "import asyncio; from bot.config import Settings; '
                                         'from bot.services.xui_service import XUIService; x=XUIService(Settings()); '
                                         'print(asyncio.run(x.api.restart_xray()))"'})
            if not any(s['protocol'] == 'reality' for s in suspects):
                suspects.append({'rank': 40, 'protocol': 'reality',
                                 'title': 'inbound-443 сам по себе (flow на месте, cert в норме, рантайм совпадает) — смотри error.log',
                                 'next': "ssh exit-node \"docker exec 3x-ui sh -c 'tail -300 /etc/x-ui/error.log | grep -i reality | tail -20'\""})

        if states.get('stls') == 'DOWN':
            if b.get('ok') and (b.get('services') or {}).get('shadow-tls') != 'active':
                suspects.append({'rank': 5, 'protocol': 'stls',
                                 'title': f'shadow-tls на entry: {(b.get("services") or {}).get("shadow-tls")} — фронт :443 не принимает',
                                 'next': 'systemctl status shadow-tls --no-pager; journalctl -u shadow-tls -n 40 --no-pager'})
            rt = runtime.get('inbound-8444') or {}
            cf = config.get('inbound-8444') or {}
            if 'error' not in rt and cf and rt.get('count', 0) < cf.get('count', 0):
                suspects.append({'rank': 20, 'protocol': 'stls',
                                 'title': f'shadowsocks hot-apply drift (Unknown account type: ...ServerConfig) — рантайм '
                                          f'{rt.get("count")} против {cf.get("count")} в config.json; restart xray',
                                 'next': 'docker exec -e PYTHONPATH=/app vpn-bot python3 -c "import asyncio; from bot.config import Settings; '
                                         'from bot.services.xui_service import XUIService; x=XUIService(Settings()); '
                                         'print(asyncio.run(x.api.restart_xray()))"'})
            if not any(s['protocol'] == 'stls' for s in suspects):
                suspects.append({'rank': 40, 'protocol': 'stls',
                                 'title': 'inbound-8444 / shadow-tls мост — смотри error.log на exit',
                                 'next': "ssh exit-node \"docker exec 3x-ui sh -c 'tail -300 /etc/x-ui/error.log | grep -i shadowsocks | tail -20'\""})

        if states.get('ws') == 'DOWN':
            rt = runtime.get('inbound-2053') or {}
            cf = config.get('inbound-2053') or {}
            if 'error' not in rt and cf and rt.get('count', 0) < cf.get('count', 0):
                suspects.append({'rank': 20, 'protocol': 'ws',
                                 'title': f'panel hot-apply drift — inbound-2053 рантайм {rt.get("count")} против '
                                          f'{cf.get("count")} в config.json; restart xray',
                                 'next': 'docker exec -e PYTHONPATH=/app vpn-bot python3 -c "import asyncio; from bot.config import Settings; '
                                         'from bot.services.xui_service import XUIService; x=XUIService(Settings()); '
                                         'print(asyncio.run(x.api.restart_xray()))"'})
            else:
                suspects.append({'rank': 40, 'protocol': 'ws',
                                 'title': 'ws-путь (CF/entry 3x-ui → exit inbound-2053) — смотри error.log',
                                 'next': "ssh exit-node \"docker exec 3x-ui sh -c 'tail -300 /etc/x-ui/error.log | grep -iE \\\"httpupgrade|websocket|2053\\\" | tail -20'\""})

        for tag in ('hy2', 'hy2t'):
            if states.get(tag) in ('DOWN', 'DEGRADED'):
                found = _hy2_suspects(tag, b, c, entry_dnat, exit_hop)
                if not found and states.get(tag) == 'DOWN':
                    unit = HYSTERIA_UNIT[tag]
                    found = [{'rank': 40, 'protocol': tag,
                              'title': f'{unit} активен, NAT и hop на месте — смотри auth ↔ bot /api/{ "hy2" if tag == "hy2" else "hy2t"}/auth',
                              'next': f"ssh exit-node \"journalctl -u {unit} -n 60 --no-pager | grep -iE 'auth|error|fail' | tail -20\""}]
                suspects.extend(found)

        rows_cmd = ('docker exec vpn-bot python3 -c "import sqlite3; c=sqlite3.connect(\'/var/lib/vpn-bot/bot.db\'); '
                    '[print(r) for r in c.execute(\\"SELECT ts,target_domain,status,latency_ms,error_msg FROM outbound_health '
                    'WHERE outbound_tag=\'%s\' ORDER BY ts DESC LIMIT 20\\")]"')
        for tag in probed:
            if states.get(tag) != 'DEGRADED':
                continue
            reason = protocols[tag].get('degraded_reason')
            if reason in ('audit', 'layer_c'):
                continue                # the audit / hop-rule suspect above names it
            if reason == 'dark_run':
                suspects.append({'rank': 35, 'protocol': tag,
                                 'title': f'{tag}: последний прогон проб без единого ok при живых прошлых — '
                                          'возможное НАЧАЛО падения (< 45 мин); смотри status/error_msg свежих строк',
                                 'next': rows_cmd % tag})
            else:
                suspects.append({'rank': 60, 'protocol': tag,
                                 'title': f'{tag} деградирован (туннель жив, ok ниже половины нормы) — смотри какие домены падают',
                                 'next': rows_cmd % tag})

    if not a_ok and not pipeline_stale:
        suspects.append({'rank': 2, 'protocol': '*',
                         'title': f'слой A (бот/пробы/аудит) недоступен: {a.get("error") or "?"} — '
                                  'без него вердикт по протоколам не строится',
                         'next': "docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' vpn-bot; docker logs --tail 40 vpn-bot"})
    if not b_ok:
        suspects.append({'rank': 50, 'protocol': '*',
                         'title': f'слой B (entry: systemd/docker/iptables) недоступен: {b.get("error") or "?"} — '
                                  'entry DNAT для hy2/hy2t не проверен',
                         'next': 'iptables -t nat -S PREROUTING | head; systemctl is-active shadow-tls haproxy'})
    if not c_ok:
        suspects.append({'rank': 50, 'protocol': '*',
                         'title': f'слой C (exit по ssh) недоступен: {c.get("error") or "?"} — '
                                  'рантайм xray/hysteria/cert не проверены',
                         'next': 'ssh -o BatchMode=yes -o ConnectTimeout=8 exit-node uptime'})

    # Several rules can name the same (protocol, cause); the operator
    # must see each once. Stable sort keeps the first (highest-ranked)
    # occurrence, then renumber so the list reads 1..N.
    suspects.sort(key=lambda s: s['rank'])
    seen, unique = set(), []
    for s in suspects:
        ident = (s['protocol'], s['title'])
        if ident in seen:
            continue
        seen.add(ident)
        unique.append(s)
    suspects[:] = unique
    for i, s in enumerate(suspects, 1):
        s['rank'] = i

    # --- verdict + exit code ---------------------------------------------------
    exit_code = exit_code_for(states)
    verdict = _verdict_line(protocols, states, all_dark, pipeline_stale, a_ok, stale_min)

    health = a.get('health') or {}
    return {
        'verdict': verdict,
        'exit_code': exit_code,
        'protocols': protocols,
        'suspects': suspects,
        'findings': findings_for(layers),
        'unjudged': unjudged_for(layers),
        'layers': {
            'a': 'ok' if a_ok else f'FAILED: {a.get("error") or "нет данных"}',
            'b': 'ok' if b.get('ok') else f'FAILED: {b.get("error") or "нет данных"}',
            'c': 'ok' if c_ok else f'FAILED: {c.get("error") or "нет данных"}',
        },
        'context': {
            'bot_version': health.get('version'),
            'bot_health': health.get('status') or health.get('error'),
            'recent_alerts': a.get('alerts') or [],
            'audit_rc': audit['rc'],
            'audit_lines': ((a.get('audit') or {}).get('lines') or [])[:6],
            'free_ram_mb': c.get('free_ram_mb'),
            'probe_newest_ts': newest,
            'generated_at': now.isoformat(timespec='seconds'),
        },
    }


def exit_code_for(states: Dict[str, str]) -> int:
    """0 all OK / 1 any DEGRADED or DOWN / 2 cannot assess.

    Precedence: a definite failure anywhere wins (1) — even when probes
    are blind, a dead hysteria-turbo is still a fact. 0 requires EVERY
    protocol to be OK; any UNKNOWN (layer A failed, stale pipeline, exit
    unreachable for hy2t) is 2, because "we could not look" must never
    read as a pass — that is precisely how four days went by. OFF
    (switched off in the cascade) is left out."""
    vals = [states.get(t) for t in REPORT_ORDER if t in states and states.get(t) != 'OFF']
    if any(v in ('DOWN', 'DEGRADED') for v in vals):
        return 1
    if vals and all(v == 'OK' for v in vals):
        return 0
    return 2


def _verdict_line(protocols, states, all_dark, pipeline_stale, a_ok, stale_min) -> str:
    switched_off = [t for t in REPORT_ORDER if states.get(t) == 'OFF']
    off_tail = f'; выключены в каскаде: {", ".join(switched_off)}' if switched_off else ''
    if all_dark:
        return ('ИТОГ: ВСЕ протоколы DOWN — общий канал (probe-proxy / entry→exit / exit), '
                'не отдельный inbound' + off_tail)
    if pipeline_stale:
        return f'ИТОГ: пробы не пишутся {_humanize(stale_min)} — состояние протоколов НЕИЗВЕСТНО'
    if not a_ok:
        bad = [f'{t} {states[t]}' for t in REPORT_ORDER if states.get(t) in ('DOWN', 'DEGRADED')]
        return 'ИТОГ: слой A недоступен, вердикт по пробам невозможен' + (
            f'; по exit: {", ".join(bad)}' if bad else '')
    present = [t for t in REPORT_ORDER if t in states and states[t] != 'OFF']
    if not present:
        return f'ИТОГ: вердикта нет — все протоколы выключены в каскаде ({", ".join(switched_off)})'
    bad = []
    for t in present:
        st = states.get(t)
        if st in ('DOWN', 'DEGRADED'):
            df = protocols[t].get('down_for')
            bad.append(f'{t} {st}' + (f' {df}' if df and st == 'DOWN' else ''))
        elif st == 'UNKNOWN':
            bad.append(f'{t} UNKNOWN')
    if not bad:
        # Rates for whatever was actually probed this run; anything judged
        # from layer C alone (hy2t without rows) is named as such.
        rates = ', '.join(f'{t} {protocols[t].get("ok_rate")}' for t in present
                          if protocols[t].get('probe_coverage'))
        unprobed = [t for t in present if not protocols[t].get('probe_coverage')]
        tail = f'; {", ".join(unprobed)} active, без проб' if unprobed else ''
        return f'ИТОГ: все протоколы OK ({rates}{tail}){off_tail}'
    rest_ok = [t for t in present if states.get(t) == 'OK']
    tail = 'остальные OK' if len(rest_ok) == len(present) - len(bad) and rest_ok else 'остальных OK нет'
    return f'ИТОГ: {", ".join(bad)}, {tail}{off_tail}'


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_human(report: dict) -> str:
    out = [report['verdict']]
    ctx = report.get('context') or {}
    lay = report.get('layers') or {}
    out.append(f"Слои: A(бот/пробы/аудит) {lay.get('a')} · B(entry) {lay.get('b')} · C(exit) {lay.get('c')}"
               f" · бот {ctx.get('bot_version') or '?'}"
               + (f" · exit RAM {ctx['free_ram_mb']} MB свободно" if ctx.get('free_ram_mb') is not None else ''))
    out.append('')
    for tag in REPORT_ORDER:
        p = (report.get('protocols') or {}).get(tag)
        if not p:
            continue
        head = f"[{tag}] {p['state']}"
        if p.get('down_for'):
            head += f" — лежит {p['down_for']}"
        out.append(head)
        for ev in p.get('evidence') or []:
            out.append(f'  {ev}')
    sus = report.get('suspects') or []
    out.append('')
    if sus:
        out.append('ПОДОЗРЕВАЕМЫЕ (по убыванию):')
        for s in sus:
            out.append(f" {s['rank']}. [{s['protocol']}] {s['title']}")
            out.append(f"    → {s['next']}")
    else:
        out.append('ПОДОЗРЕВАЕМЫЕ: нет — ничего чинить не надо')
    found = report.get('findings') or []
    if found:
        out.append('')
        out.append('НАХОДКИ (вне вердикта по протоколам):')
        for f in found:
            out.append(f" [{f.get('severity')}] {f.get('key')}: {f.get('title')}")
            out.append(f"    → {f.get('next')}")
    alerts = ctx.get('recent_alerts') or []
    if alerts:
        out.append('')
        out.append(f'Алерты за {ALERT_WINDOW_H}ч (protocol_down/dpi):')
        for row in alerts[:8]:
            row = list(row) + [None] * 5
            acked = 'квитирован' if row[4] else 'НЕ квитирован'
            out.append(f'  {row[3]} {row[1]} {row[0]} — {row[2]} ({acked})')
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def post_report(url: str, report: dict, token: Optional[str] = None) -> bool:
    """POST the report (without the raw layers) to the bot's
    /api/healthcheck/report. True on a 2xx. Never raises: the report still
    goes to stdout and the exit code still tells what was found."""
    token = token or _read_env_value(BOT_ENV, 'DPI_REPORT_TOKEN') or ''
    if not token:
        print(f'healthcheck: DPI_REPORT_TOKEN not found in {BOT_ENV}', file=sys.stderr)
        return False
    body = json.dumps({k: v for k, v in report.items() if k != 'layers_raw'},
                      ensure_ascii=False, default=str).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'X-DPI-Token': token})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f'healthcheck: POST {url} failed: {e}', file=sys.stderr)
        return False


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='Deterministic protocol health check (run as root on entry).')
    ap.add_argument('--json', action='store_true', help='machine-readable report')
    ap.add_argument('--sni', default=None,
                    help=f'Reality dest to measure (default: SNI_VALUE from {BOT_ENV}, else {DEFAULT_SNI})')
    ap.add_argument('--post', default=None, metavar='URL',
                    help='POST the report to the bot (the timer: '
                         'http://127.0.0.1:8080/api/healthcheck/report); the token is '
                         f'DPI_REPORT_TOKEN from {BOT_ENV}')
    args = ap.parse_args(argv)

    sni = args.sni or _read_env_value(BOT_ENV, 'SNI_VALUE') or DEFAULT_SNI
    if not re.fullmatch(r'[A-Za-z0-9.-]+', sni):     # it is interpolated into a remote shell line
        print(f'bad SNI value: {sni!r}', file=sys.stderr)
        return 2
    layers = collect_all(sni)
    report = assess(layers)
    if args.post:
        if not post_report(args.post, report):
            print('healthcheck: report POST failed', file=sys.stderr)
    if args.json:
        report['layers_raw'] = layers
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    else:
        print(render_human(report))
    return report['exit_code']


if __name__ == '__main__':
    sys.exit(main())
