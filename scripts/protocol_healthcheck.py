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
     free RAM.

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
latency; vk/yandex/sberbank legitimately fail through the exit and still
carry one. The 2026-09-01 rows had latency NULL: nothing ever connected.
Normal is 7/10 ok per protocol per run.

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

HOW TO RUN (as root on the ENTRY host; plain python3, no bot imports)
---------------------------------------------------------------------
    python3 /opt/vpn-bot/scripts/protocol_healthcheck.py          # human report (RU)
    python3 /opt/vpn-bot/scripts/protocol_healthcheck.py --json   # machine-readable

From the dev box:  ssh entry 'python3 /opt/vpn-bot/scripts/protocol_healthcheck.py'

EXIT CODES
----------
    0  every protocol OK
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

PROBED = ['reality', 'hy2', 'ws', 'stls']      # what the probe sidecar ALWAYS speaks
PROBED_OPTIONAL = ['hy2t']   # probed only where HY2T_PORT is set — decided per run by probed_tags()
PROTOCOLS = ['reality', 'hy2', 'hy2t', 'ws', 'stls']   # what we report on

PROBE_DOMAINS = 10           # rows per protocol per run (HealthChecker.TARGET_DOMAINS)
PROBE_RUNS = 3               # "last 3 runs"
PROBE_LIMIT = PROBE_RUNS * PROBE_DOMAINS
PROBE_WINDOW_H = 3
PROBE_STALE_MIN = 45         # 3 missed runs at the 15-min cadence
RUN_SPAN_MIN = 5             # rows within 5 min of the newest row = one run (a run writes 10 rows in ms)
BASELINE_OK = 7 / PROBE_DOMAINS          # 7/10 is normal
DEGRADED_BELOW = BASELINE_OK / 2         # < half of normal = DEGRADED
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

STATE_ORDER = {'DOWN': 3, 'DEGRADED': 2, 'UNKNOWN': 1, 'OK': 0}


# ---------------------------------------------------------------------------
# Layer A — runs INSIDE the bot container (python3 -c). Stdlib only; it must
# work on the prod image's python 3.11. Prints one JSON blob.
# ---------------------------------------------------------------------------

INLINE_A = r'''
import json, sqlite3, subprocess, sys
from datetime import datetime, timedelta
DB = %(db)r
out = {"probes": {}, "newest_ts": None, "alerts": [], "audit": {}}
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
rc, o, e = sh("docker exec 3x-ui cat /app/bin/config.json", 15)
try:
    for ib in json.loads(o).get("inbounds") or []:
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
print(json.dumps(out))
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
            'free_ram_mb': None, 'errors': {}}
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
           'alerts': [], 'audit': {}, 'health': {}}
    rc, so, se = _run(['docker', 'exec', '-e', 'PYTHONPATH=/app', BOT_CONTAINER,
                       'python3', '-c', INLINE_A], TIMEOUT_A)
    if rc != 0 and not so.strip():
        out['error'] = (se.strip() or f'docker exec rc={rc}')[:300]
    else:
        try:
            data = json.loads(so.strip().splitlines()[-1])
            out.update({k: data.get(k, out[k]) for k in ('probes', 'newest_ts', 'alerts', 'audit')})
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
    script = REMOTE_C % {'sni': sni}
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
    if rate < DEGRADED_BELOW:
        return {'state': 'DEGRADED', 'degraded_reason': 'probe_rate',
                'evidence': [f'пробы: ok {ok_rate} ({rate:.0%}) при норме ~70% — '
                             f'туннель жив ({len(alive)} с задержкой), но сайты не проходят; {run_line}; {sample}'],
                'down_for': None, 'ok_rate': ok_rate}
    if len(latest) >= PROBE_DOMAINS and l_ok == 0:
        # The 3-run average still looks fine, but the newest full run has
        # not a single ok (normal is 7/10): an outage that started within
        # the last 45 min looks exactly like this. Never "OK".
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

    # --- suspects ---------------------------------------------------------------
    states = {t: protocols[t]['state'] for t in PROTOCOLS if t in protocols}
    probed_down = [t for t in probed if states.get(t) == 'DOWN']
    all_dark = len(probed_down) == len(probed)

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
    read as a pass — that is precisely how four days went by."""
    vals = [states.get(t) for t in PROTOCOLS if t in states]
    if any(v in ('DOWN', 'DEGRADED') for v in vals):
        return 1
    if vals and all(v == 'OK' for v in vals):
        return 0
    return 2


def _verdict_line(protocols, states, all_dark, pipeline_stale, a_ok, stale_min) -> str:
    if all_dark:
        return 'ИТОГ: ВСЕ протоколы DOWN — общий канал (probe-proxy / entry→exit / exit), не отдельный inbound'
    if pipeline_stale:
        return f'ИТОГ: пробы не пишутся {_humanize(stale_min)} — состояние протоколов НЕИЗВЕСТНО'
    if not a_ok:
        bad = [f'{t} {states[t]}' for t in PROTOCOLS if states.get(t) in ('DOWN', 'DEGRADED')]
        return 'ИТОГ: слой A недоступен, вердикт по пробам невозможен' + (
            f'; по exit: {", ".join(bad)}' if bad else '')
    bad = []
    for t in PROTOCOLS:
        st = states.get(t)
        if st in ('DOWN', 'DEGRADED'):
            df = protocols[t].get('down_for')
            bad.append(f'{t} {st}' + (f' {df}' if df and st == 'DOWN' else ''))
        elif st == 'UNKNOWN':
            bad.append(f'{t} UNKNOWN')
    if not bad:
        # Rates for whatever was actually probed this run; anything judged
        # from layer C alone (hy2t without rows) is named as such.
        rates = ', '.join(f'{t} {protocols[t].get("ok_rate")}' for t in PROTOCOLS
                          if t in protocols and protocols[t].get('probe_coverage'))
        unprobed = [t for t in PROTOCOLS if t in protocols and not protocols[t].get('probe_coverage')]
        tail = f'; {", ".join(unprobed)} active, без проб' if unprobed else ''
        return f'ИТОГ: все протоколы OK ({rates}{tail})'
    rest_ok = [t for t in PROTOCOLS if states.get(t) == 'OK']
    tail = 'остальные OK' if len(rest_ok) == len(PROTOCOLS) - len(bad) and rest_ok else 'остальных OK нет'
    return f'ИТОГ: {", ".join(bad)}, {tail}'


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
    for tag in PROTOCOLS:
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

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='Deterministic protocol health check (run as root on entry).')
    ap.add_argument('--json', action='store_true', help='machine-readable report')
    ap.add_argument('--sni', default=None,
                    help=f'Reality dest to measure (default: SNI_VALUE from {BOT_ENV}, else {DEFAULT_SNI})')
    args = ap.parse_args(argv)

    sni = args.sni or _read_env_value(BOT_ENV, 'SNI_VALUE') or DEFAULT_SNI
    if not re.fullmatch(r'[A-Za-z0-9.-]+', sni):     # it is interpolated into a remote shell line
        print(f'bad SNI value: {sni!r}', file=sys.stderr)
        return 2
    layers = collect_all(sni)
    report = assess(layers)
    if args.json:
        report['layers_raw'] = layers
        print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    else:
        print(render_human(report))
    return report['exit_code']


if __name__ == '__main__':
    sys.exit(main())
