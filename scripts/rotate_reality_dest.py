#!/usr/bin/env python3
"""Rotate the Reality dest/SNI across every layer in ONE checked, reversible run.

WHY THIS EXISTS
---------------
2026-07-20 (AGENTS.md §23): Microsoft's chain grew to an 8273-byte TLS
Certificate record — over the 8192-byte buffer hardcoded in xtls/reality —
and VLESS-Reality died for every user. The cure is a new dest/SNI, and it
only works when ALL of these move together:

  1. exit panel    inbound INBOUND_ID: realitySettings.dest (or .target)
                   + serverNames                       (HTTP API, §23 cheatsheet)
  2. entry HAProxy ``acl is_reality_sni req_ssl_sni -i <names>`` — routes
                   that SNI from entry :8443 to the exit Reality inbound
  3. entry bot     SNI_VALUE in /opt/vpn-bot/.env + a vpn-bot RECREATE
                   (``restart`` keeps the old env) — /sub, the key card and
                   every link hand this name to clients
  4. probe-proxy   its sing-box config is GENERATED from SNI_VALUE
                   (scripts/gen_probe_config.py). Left on the old name, the
                   reality probe goes dark as soon as the panel drops it:
                   protocol_down:reality pages and DPIMonitor demotes
                   Reality for everyone ~20 min later. Nobody listed this
                   layer in July.

By hand, mid-outage, that is four ssh sessions, a JSON edit in the panel
and a compose recreate — each a chance to move two layers of four.

ONE RUN, FOUR MODES
-------------------
check     (default, READ-ONLY — safe at any time)
          Measures the candidate FROM EXIT (that is who dials the dest),
          --cert-samples times: TLS 1.3, ALPN h2, certificate valid for the
          SNI, TLS Certificate record <= CERT_RECORD_LIMIT bytes (the MAX
          over the samples — CDN edges serve different chains, §23). Reads
          the current value of every layer and prints the plan as a diff.
--apply   check must pass -> plan -> type ``yes`` -> the snapshot is written
          BEFORE anything changes -> panel (+ panel-side xray restart) ->
          HAProxy (``haproxy -c`` on a copy, reload; the file is put back if
          the reload fails) -> .env + vpn-bot recreate (--no-deps, waits for
          /health) -> probe-proxy (regenerate, ``sing-box check``, restart).
          A failed panel/HAProxy/.env/bot step reverts whatever was already
          changed, in rollback order (--no-auto-revert to keep it); a failed
          probe-proxy step keeps the rotation (users are fine), exits 1 and
          a re-run of the same --apply finishes it from the snapshot.
          Ends with verify. Every write re-checks that its layer still holds
          what this run's check read — a plan never lands on a stale world.
--verify  Panel returns the planned dest/serverNames, exit's running
          config.json agrees (after the restart), HAProxy is active with the
          planned ACL, bot /health is healthy and the RUNNING container has
          the new SNI_VALUE, probe-proxy is on it, and a real TLS 1.3
          handshake to entry with the new SNI completes with a certificate
          valid for that name (HAProxy -> exit -> Reality hands a
          non-Reality client to dest). Reality AUTHENTICATION is not
          tested — that takes a client key; probe-proxy does it within its
          15-min cycle. Target: --sni, else the snapshot.
--rollback  Puts back the snapshot's values in reverse layer order
          (.env+bot+probe -> HAProxy -> panel). Refuses — before touching
          anything — when a layer holds neither the old nor the new value.
          Also asks for ``yes``. Ends with verify against the old values.

The snapshot (default scripts/.rotation_snapshot.json, gitignored) holds
VALUES only — dest, names, SNI, client counts, backup paths — never a key,
a client id or a file body. Files are backed up next to themselves ON the
hosts (*.rotate-bak-<UTC stamp>); .env backups are mode 0600.

HOW TO RUN — from the operator's machine (repo checkout), ssh aliases
``entry`` / ``vpn-exit``, root on both (haproxy.cfg, systemctl, docker)
----------------------------------------------------------------------
    python3 scripts/rotate_reality_dest.py --sni www.google.com          # plan, changes nothing
    python3 scripts/rotate_reality_dest.py --sni www.google.com --apply  # asks for 'yes'
    python3 scripts/rotate_reality_dest.py --verify                      # against the snapshot
    python3 scripts/rotate_reality_dest.py --rollback                    # asks for 'yes'

DRY RUN WITHOUT PROD — ROTATE_FAKE=1 swaps the ssh layer for a fixture
world (the prod shape of 2026-10: bing dest, ACL "www.bing.com
www.google.com", §23 cert sizes). No ssh, no network.
    ROTATE_FAKE=1 python3 scripts/rotate_reality_dest.py --sni www.google.com
    ROTATE_FAKE=1 python3 scripts/rotate_reality_dest.py --sni www.microsoft.com   # 8273 B -> exit 1
    export ROTATE_FAKE=1 ROTATE_FAKE_STATE=/tmp/rotate_fake_world.json  # world survives between runs
    python3 scripts/rotate_reality_dest.py --sni www.google.com --apply
    python3 scripts/rotate_reality_dest.py --rollback
    ROTATE_FAKE_FAIL=haproxy_set python3 scripts/rotate_reality_dest.py --sni www.google.com --apply
        # injected failure (panel_set, haproxy_set, env_set, bot_recreate,
        # probe_regen, read_panel, read_entry, probe_exit, tls_entry): watch the auto-revert
In fake mode the snapshot defaults to $TMPDIR/rotate_reality_dest.fake_snapshot.json,
so a rehearsal can never block a real rotation.

EXIT CODES
----------
    0  ok (check: candidate fine and a plan was built — possibly "nothing
       to change"; apply/rollback: done and verified; verify: all layers OK)
    1  a check did not pass: bad candidate, a layer in a shape this
       playbook does not edit (no ACL line, SNI_VALUE twice, ...), a failed
       step, a failed verification, or the operator did not type ``yes``
    2  could not look: ssh/docker/panel unreachable, snapshot missing or
       unreadable. A 2 is never a pass.

The verdict and plan logic is pure (build_plan / steps_needed / verify
target compare) and unit-tested on fixtures; the host-side scripts embed the
same parsers verbatim (inspect.getsource), so the code that rewrites
haproxy.cfg and .env on prod is exactly the code the tests exercise
(tests/unit/test_rotate_reality_dest.py).
"""

import argparse
import copy
import importlib.util
import inspect
import json
import os
import posixpath
import re
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple


def _load_sibling(name: str):
    """scripts/ is not a package: load a sibling script by path (no sys.path
    side effects for whoever imports this module)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + '.py')
    spec = importlib.util.spec_from_file_location('_rotate_dep_' + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ONE parser for the ``Handshake [length XXXX], Certificate`` line — the same
# one the deterministic health check uses to flag an oversized dest.
parse_cert_record_len = _load_sibling('protocol_healthcheck').parse_cert_record_len

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# xtls/reality buffers the dest's Certificate record in 8192 bytes;
# microsoft's 8273 killed Reality on 2026-07-20. Same margin as
# protocol_healthcheck.CERT_LIMIT_BYTES (a test pins the two together).
CERT_RECORD_LIMIT = 8000

DEFAULT_ENTRY = 'entry'          # operator's ssh alias for the entry host
DEFAULT_EXIT = 'vpn-exit'        # operator's ssh alias for the exit host
BOT_DIR = '/opt/vpn-bot'         # compose project dir on entry (rsync target)
HAPROXY_CFG = '/etc/haproxy/haproxy.cfg'
ACL_NAME = 'is_reality_sni'
BOT_CONTAINER = 'vpn-bot'        # compose service name AND container_name
PROBE_CONTAINER = 'probe-proxy'
PROBE_MOUNT = '/etc/sing-box'    # <bot-dir>/probe-proxy is mounted here (:ro)
XUI_CONTAINER = '3x-ui'          # on exit
XRAY_CONFIG = '/app/bin/config.json'   # regenerated from the panel DB on xray restart
HEALTH_URL = 'http://127.0.0.1:8080/health'
SNAPSHOT_VERSION = 1
SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.rotation_snapshot.json')
FAKE_SNAPSHOT_PATH = os.path.join(tempfile.gettempdir(), 'rotate_reality_dest.fake_snapshot.json')
CERT_SAMPLES = 3
TLS_TIMEOUT = 12                 # seconds per openssl handshake
BOT_WAIT_S = 120                 # how long the recreated bot gets to report healthy

EXIT_OK, EXIT_FAIL, EXIT_BLIND = 0, 1, 2

# Apply moves the groups left to right; rollback/auto-revert right to left.
# env -> bot -> probe stay in that order both ways: the bot recreate reads
# the new .env, and probe-proxy's config is generated INSIDE that container.
LAYER_GROUPS = (('panel',), ('haproxy',), ('env', 'bot', 'probe'))
APPLY_ORDER = tuple(s for g in LAYER_GROUPS for s in g)
ROLLBACK_ORDER = tuple(s for g in reversed(LAYER_GROUPS) for s in g)
# A failed probe-proxy step never reverts the rotation: users are already
# served correctly; only monitoring lags (and a re-run fixes it).
SOFT_STEPS = ('probe',)
# Snapshot statuses after which the world may be MIXED: apply resumes the
# same rotation from the snapshot, or refuses a different one.
UNFINISHED = ('pending', 'failed', 'partial', 'rolling_back')

STEP_TITLES = {
    'panel': 'панель exit',
    'haproxy': 'haproxy entry',
    'env': '.env бота',
    'bot': 'пересоздание vpn-bot',
    'probe': 'probe-proxy',
}

# ---------------------------------------------------------------------------
# Shared pure helpers — ALSO executed on the hosts. They are shipped
# verbatim (inspect.getsource) inside the remote scripts, so the code that
# rewrites prod files is exactly the code the unit tests exercise. Rules for
# this block: stdlib only, imports inside the function, no annotations, no
# module globals, Python >= 3.8 (whatever the host runs).
# ---------------------------------------------------------------------------


def parse_acl_lines(text, acl):
    """Active ``acl <acl> req_ssl_sni ...`` lines of a HAProxy config.

    One dict per line, in file order: ``lineno`` (index in
    ``text.split('\\n')``), ``names`` (the SNI patterns), ``start``/``end``
    (offsets of the names inside that line — a rewrite replaces exactly
    that span, so indentation, flags and a trailing comment survive) and
    ``error`` when the line cannot be rotated as text (names read from a
    file, or a match method other than exact string). Commented-out lines
    are not ACLs and are skipped.
    """
    import re
    head = re.compile(r'^(\s*acl\s+' + re.escape(acl) + r'\s+(?:req_ssl_sni|req\.ssl_sni)(?=\s|$))')
    out = []
    for lineno, raw in enumerate(text.split('\n')):
        m = head.match(raw)
        if not m:
            continue
        base = m.end(1)
        body = raw[base:]
        hash_at = body.find('#')
        code = body if hash_at < 0 else body[:hash_at]
        toks = [(t.group(0), t.start(), t.end()) for t in re.finditer(r'\S+', code)]
        error = None
        first = None
        i = 0
        while i < len(toks):
            tok = toks[i][0]
            if tok == '--':
                first = i + 1 if i + 1 < len(toks) else None
                break
            if tok in ('-f', '-m', '-u'):
                arg = toks[i + 1][0] if i + 1 < len(toks) else ''
                if tok == '-f':
                    error = 'имена берутся из файла (-f %s) — правь его руками' % arg
                elif tok == '-m' and arg != 'str':
                    error = 'метод сравнения -m %s — это шаблоны, а не имена; правь руками' % arg
                i += 2
                continue
            if tok.startswith('-'):
                i += 1
                continue
            first = i
            break
        names = [t[0] for t in toks[first:]] if first is not None else []
        if names:
            start, end = base + toks[first][1], base + toks[-1][2]
        else:
            start = end = base + len(code.rstrip())
        out.append({'lineno': lineno, 'names': names, 'start': start, 'end': end, 'error': error})
    return out


def rewrite_acl_text(text, acl, names_per_line):
    """``text`` with the names of every ``acl <acl>`` line replaced by the
    matching list of ``names_per_line`` (same count and order as
    parse_acl_lines). Every other byte is kept. ValueError on a count
    mismatch, an empty list, or a line parse_acl_lines flagged."""
    entries = parse_acl_lines(text, acl)
    if len(entries) != len(names_per_line):
        raise ValueError('acl %s: строк %d, а списков имён %d' % (acl, len(entries), len(names_per_line)))
    lines = text.split('\n')
    for entry, names in zip(entries, names_per_line):
        if entry['error']:
            raise ValueError(entry['error'])
        if not names:
            raise ValueError('acl %s: пустой список имён — такой ACL не совпадёт ни с чем' % acl)
        raw = lines[entry['lineno']]
        joined = ' '.join(names)
        if entry['start'] == entry['end']:
            joined = ' ' + joined
        lines[entry['lineno']] = raw[:entry['start']] + joined + raw[entry['end']:]
    return '\n'.join(lines)


def _env_assignment(raw, key):
    """None, or ``(lead, value, quote, tail)`` when ``raw`` is an active
    ``KEY=...`` line of a docker-compose .env. ``lead`` is everything before
    the value (indent, 'export ', 'KEY=', spaces); ``tail`` everything after
    it (closing quote excluded; spacing, a comment and a CR kept)."""
    import re
    m = re.match(r'^(\s*(?:export\s+)?' + re.escape(key) + r'\s*=\s*)(.*?)(\r?)$', raw)
    if not m:
        return None
    lead, v, cr = m.group(1), m.group(2), m.group(3)
    if v[:1] in ('"', "'") and v.find(v[0], 1) > 0:
        q = v[0]
        end = v.find(q, 1)
        return lead, v[1:end], q, v[end + 1:] + cr
    cut = len(v)
    for sep in (' #', '\t#'):
        at = v.find(sep)
        if 0 <= at < cut:
            cut = at
    value = v[:cut].rstrip()
    return lead, value, '', v[len(value):] + cr


def parse_env_value(text, key):
    """``(value, count)``: the value of the LAST active ``key=`` line of a
    compose .env (None when absent) and how many active lines define it."""
    value, count = None, 0
    for raw in text.split('\n'):
        hit = _env_assignment(raw, key)
        if hit is not None:
            value, count = hit[1], count + 1
    return value, count


def rewrite_env_text(text, key, value):
    """``text`` with ``key`` set to ``value``: the one active definition is
    rewritten in place (indent, 'export ', quote style and a trailing
    comment kept); absent -> appended; ``value`` None -> the definition is
    removed (rollback to "was not set"). Several active definitions ->
    ValueError: which one compose uses is not something to guess on prod."""
    lines = text.split('\n')
    hits = [i for i, raw in enumerate(lines) if _env_assignment(raw, key) is not None]
    if len(hits) > 1:
        raise ValueError('%s задан в .env %d раз — сведи к одному руками' % (key, len(hits)))
    if not hits:
        if value is None:
            return text
        sep = '' if (not text or text.endswith('\n')) else '\n'
        return text + sep + '%s=%s\n' % (key, value)
    i = hits[0]
    if value is None:
        del lines[i]
        return '\n'.join(lines)
    lead, _old, q, tail = _env_assignment(lines[i], key)
    lines[i] = lead + q + value + q + tail
    return '\n'.join(lines)


def parse_blob(blob):
    """Panel JSON columns arrive as strings on some routes and dicts on
    others (verify_panel_client_fields._parse) — cope with both."""
    import json
    if isinstance(blob, str):
        try:
            data = json.loads(blob or '{}')
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return blob if isinstance(blob, dict) else {}


def read_reality(inbound):
    """The rotation-relevant facts of one panel inbound — and nothing else:
    no private key, no client ids (this dict is printed and snapshotted)."""
    stream = parse_blob(inbound.get('streamSettings'))
    settings = parse_blob(inbound.get('settings'))
    rs = stream.get('realitySettings')
    rs = rs if isinstance(rs, dict) else {}
    inner = rs.get('settings') if isinstance(rs.get('settings'), dict) else {}
    clients = [c for c in (settings.get('clients') or []) if isinstance(c, dict)]
    names = rs.get('serverNames') or []
    if not isinstance(names, list):
        names = [names]
    return {
        'id': inbound.get('id'),
        'tag': inbound.get('tag'),
        'protocol': inbound.get('protocol'),
        'network': stream.get('network'),
        'security': stream.get('security'),
        'dest_fields': dict((k, rs[k]) for k in ('dest', 'target') if k in rs),
        'server_names': [str(n) for n in names],
        'settings_server_name': inner.get('serverName'),
        'clients': len(clients),
        'with_flow': sum(1 for c in clients if c.get('flow')),
    }


def apply_reality_changes(stream, target):
    """Write ``target``'s dest_fields, serverNames and (when it carries one)
    realitySettings.settings.serverName into a PARSED streamSettings dict,
    in place. The planner only ever names dest keys that already exist."""
    rs = stream.get('realitySettings')
    if not isinstance(rs, dict):
        raise ValueError('в streamSettings нет realitySettings')
    for k, v in (target.get('dest_fields') or {}).items():
        rs[k] = v
    rs['serverNames'] = list(target.get('server_names') or [])
    ssn = target.get('settings_server_name')
    if ssn is not None and isinstance(rs.get('settings'), dict):
        rs['settings']['serverName'] = ssn
    return stream


def same_panel(cur, exp):
    """Do two panel value dicts agree on everything a rotation writes?
    Names compare case-insensitively (an SNI is a DNS name)."""
    def low(xs):
        return [str(x).lower() for x in (xs or [])]
    return (dict(cur.get('dest_fields') or {}) == dict(exp.get('dest_fields') or {})
            and low(cur.get('server_names')) == low(exp.get('server_names'))
            and cur.get('settings_server_name') == exp.get('settings_server_name'))


def probe_reality_server_name(config):
    """server_name of the Reality outbound in a probe-proxy sing-box config
    (gen_probe_config.py output); None when there is none."""
    for ob in (config or {}).get('outbounds') or []:
        if not isinstance(ob, dict):
            continue
        tls = ob.get('tls')
        if isinstance(tls, dict) and isinstance(tls.get('reality'), dict) and tls['reality'].get('enabled'):
            return tls.get('server_name')
    return None


def xray_reality_for_tag(config, tag):
    """dest_fields/serverNames of inbound ``tag`` in xray's running
    config.json; None when the tag is absent."""
    for ib in (config or {}).get('inbounds') or []:
        if isinstance(ib, dict) and ib.get('tag') == tag:
            rs = (ib.get('streamSettings') or {}).get('realitySettings') or {}
            return {'dest_fields': dict((k, rs[k]) for k in ('dest', 'target') if k in rs),
                    'server_names': [str(n) for n in (rs.get('serverNames') or [])]}
    return None


# ---------------------------------------------------------------------------
# Host-side drivers. Assembled by remote_script(): a header (imports +
# PARAMS), the shared helpers above, REMOTE_COMMON, then the driver. Each
# prints ONE JSON line; nothing secret is ever printed (no file bodies, no
# keys) — the dev box only ever sees names, values and verdicts.
# ---------------------------------------------------------------------------

REMOTE_COMMON = r'''
def sh(argv, timeout=30, cwd=None, stdin=None):
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, '', 'timeout after %ss: %s' % (timeout, ' '.join(argv[:3]))
    except OSError as e:
        return -1, '', str(e)


def tail(text, n=400):
    return (text or '').strip()[-n:]


TLS_KEEP = re.compile(r'Handshake \[length [0-9a-fA-F]+\], Certificate|^New, |ALPN protocol|No ALPN'
                      r'|Verify return code|errno|error|refused|timed out|timeout|connect:|getaddrinfo'
                      r'|Name or service|alert', re.I)


def tls_sample(host, port, sni, timeout):
    """One openssl handshake: -4 because the 3x-ui container runs with IPv6
    disabled (exit's v6 egress is flaky, §23) — measure the path xray uses."""
    argv = ['openssl', 's_client', '-4', '-connect', '%s:%s' % (host, port), '-servername', sni,
            '-tls1_3', '-alpn', 'h2', '-msg', '-verify_hostname', sni]
    rc, o, e = sh(argv, timeout, stdin='')
    lines = [l.strip()[:200] for l in (o + '\n' + e).splitlines() if TLS_KEEP.search(l)]
    return {'rc': rc, 'lines': lines[:30]}


def backup_path(path, stamp):
    """``<path>.rotate-bak-<stamp>``, never an existing file: an apply and
    its auto-revert share one stamp, and the second backup must not
    overwrite the first (that one holds the ORIGINAL)."""
    base = '%s.rotate-bak-%s' % (path, stamp)
    candidate, n = base, 1
    while os.path.exists(candidate):
        candidate = '%s.%d' % (base, n)
        n += 1
    return candidate


def write_like(path, text, like, mode=None):
    """Write ``text`` to ``path`` (a temp file next to the target) with the
    mode/owner of ``like`` when it exists."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(text)
    if os.path.exists(like):
        st = os.stat(like)
        os.chmod(path, mode if mode is not None else (st.st_mode & 0o7777))
        try:
            os.chown(path, st.st_uid, st.st_gid)
        except OSError:
            pass
    elif mode is not None:
        os.chmod(path, mode)
'''

ENTRY_READ = r'''
import urllib.request
P = PARAMS
out = {'ok': True, 'euid': os.geteuid()}

h = {'ok': False}
try:
    real = os.path.realpath(P['haproxy_cfg'])
    with open(real, encoding='utf-8') as fh:
        text = fh.read()
    entries = parse_acl_lines(text, P['acl'])
    lines = text.split('\n')
    h.update(ok=True, acl_lines=[e['names'] for e in entries],
             raw=[lines[e['lineno']].strip()[:300] for e in entries],
             errors=[e['error'] for e in entries if e['error']],
             writable=os.access(real, os.W_OK) and os.access(os.path.dirname(real), os.W_OK),
             haproxy_bin=bool(shutil.which('haproxy')))
except Exception as e:
    h['error'] = '%s: %s' % (P['haproxy_cfg'], e)
rc, o, e = sh(['systemctl', 'is-active', 'haproxy'], 10)
h['active'] = (o.strip().splitlines() or [e.strip() or 'unknown'])[0]
out['haproxy'] = h

v = {'ok': False}
try:
    real = os.path.realpath(P['env_file'])
    with open(real, encoding='utf-8') as fh:
        text = fh.read()
    sni, count = parse_env_value(text, 'SNI_VALUE')
    v.update(ok=True, sni=sni, count=count,
             entry_ip=parse_env_value(text, 'ENTRY_NODE_IP')[0],
             entry_port=parse_env_value(text, 'ENTRY_NODE_PORT')[0],
             writable=os.access(real, os.W_OK) and os.access(os.path.dirname(real), os.W_OK))
except Exception as e:
    v['error'] = '%s: %s' % (P['env_file'], e)
out['env'] = v

b = {'ok': bool(shutil.which('docker')), 'container_sni': None}
if not b['ok']:
    b['error'] = 'docker не найден на entry'
rc, o, e = sh(['docker', 'inspect', '-f', '{{.State.Status}}', P['bot_container']], 15)
b['state'] = o.strip() if rc == 0 else 'missing'
if b['state'] == 'running':
    rc, o, e = sh(['docker', 'exec', P['bot_container'], 'printenv', 'SNI_VALUE'], 15)
    b['container_sni'] = o.strip() if rc == 0 else None
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(P['health_url'], timeout=5) as r:
        hj = json.loads(r.read().decode('utf-8', 'replace'))
    b['health'] = hj.get('status')
    b['version'] = hj.get('version')
except Exception as e:
    b['health'] = 'down'
    b['health_error'] = str(e)[:200]
out['bot'] = b

p = {'ok': False}
try:
    real = os.path.realpath(P['probe_cfg'])
    if not os.path.exists(real):
        p.update(ok=True, present=False, server_name=None)
    else:
        with open(real, encoding='utf-8') as fh:
            cfg = json.load(fh)
        p.update(ok=True, present=True, server_name=probe_reality_server_name(cfg))
except Exception as e:
    p['error'] = '%s: %s' % (P['probe_cfg'], e)
rc, o, e = sh(['docker', 'inspect', '-f', '{{.State.Status}}', P['probe_container']], 15)
p['container'] = o.strip() if rc == 0 else 'missing'
out['probe'] = p
print(json.dumps(out))
'''

# Runs INSIDE the vpn-bot container: the bot's own Settings (creds + URL of
# the exit panel) and its own XUIAPIClient (CSRF login, §23) — the same
# path restore_reality_flow.py and verify_panel_client_fields.py use.
PANEL = r'''
import asyncio
import logging

class _Cap(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self, logging.WARNING)
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage()[:300])

CAP = _Cap()
logging.getLogger().addHandler(CAP)


async def main():
    out = {'ok': False}
    try:
        from bot.config import Settings
        from bot.services.xui_service import XUIService
        cfg = Settings()
        iid = int(PARAMS.get('inbound_id') or getattr(cfg, 'INBOUND_ID', 0) or 1)
        out['inbound_id'] = iid
        api = XUIService(cfg).api
        if not api:
            out['error'] = 'API панели не настроен (XUI_API_URL)'
            return out
        try:
            ib = await api.get_inbound(iid)
            if not ib:
                out['error'] = 'inbound %s не читается через API панели' % iid
                return out
            cur = read_reality(ib)
            out['inbound'] = cur
            if PARAMS['op'] == 'read':
                out['ok'] = True
                return out
            if not same_panel(cur, PARAMS['expect']):
                out['conflict'] = True
                out['error'] = 'панель изменилась после проверки: dest %s, serverNames %s' % (
                    cur['dest_fields'], cur['server_names'])
                return out
            raw = ib.get('streamSettings')
            stream = apply_reality_changes(parse_blob(raw), PARAMS['target'])
            # Same shape back as it came: a string column stays a string.
            ib['streamSettings'] = json.dumps(stream, indent=2) if isinstance(raw, str) else stream
            out['updated'] = bool(await api.update_inbound(iid, ib))
            after = await api.get_inbound(iid)
            out['after'] = read_reality(after) if after else None
            if out['updated'] and PARAMS.get('restart'):
                out['restarted'] = bool(await api.restart_xray())
            out['ok'] = bool(out['updated'] and out['after'])
            if not out['ok']:
                out['error'] = 'update_inbound не подтвердился'
        finally:
            await api.close()
    except Exception as e:
        out['error'] = '%s: %s' % (type(e).__name__, str(e)[:300])
    finally:
        out['log'] = CAP.msgs[-5:]
    return out

print(json.dumps(asyncio.run(main())))
'''

EXIT_PROBE = r'''
P = PARAMS
out = {'ok': True, 'tls': {}, 'xray': None, 'openssl': bool(shutil.which('openssl'))}
for job in P.get('jobs') or []:
    out['tls'][job['tag']] = [tls_sample(job['host'], job['port'], job['sni'], P['timeout'])
                              for _ in range(max(1, int(job.get('samples') or 1)))]
if P.get('xray_tag'):
    rc, o, e = sh(['docker', 'exec', P['xui_container'], 'cat', P['xray_config']], 20)
    if rc != 0:
        out['xray_error'] = tail(e or o, 200) or 'rc=%s' % rc
    else:
        try:
            out['xray'] = xray_reality_for_tag(json.loads(o), P['xray_tag'])
            if out['xray'] is None:
                out['xray_error'] = 'в config.json нет inbound с тегом %s' % P['xray_tag']
        except ValueError as ex:
            out['xray_error'] = 'config.json не JSON: %s' % ex
print(json.dumps(out))
'''

# Where clients dial Reality: what the RUNNING bot hands out (entry keeps a
# hand-tuned compose that may set the port outside .env), else .env.
ENTRY_TLS = r'''
P = PARAMS
out = {'ok': False}
host, port, source = P.get('addr'), P.get('port'), '--tls-probe-addr'
if not host:
    rc1, o1, e1 = sh(['docker', 'exec', P['bot_container'], 'printenv', 'ENTRY_NODE_IP'], 15)
    rc2, o2, e2 = sh(['docker', 'exec', P['bot_container'], 'printenv', 'ENTRY_NODE_PORT'], 15)
    host = o1.strip() if rc1 == 0 else None
    port = o2.strip() if rc2 == 0 else None
    source = 'env контейнера %s' % P['bot_container']
    if not host:
        source = P['env_file']
        try:
            with open(os.path.realpath(P['env_file']), encoding='utf-8') as fh:
                text = fh.read()
            host = parse_env_value(text, 'ENTRY_NODE_IP')[0]
            port = port or parse_env_value(text, 'ENTRY_NODE_PORT')[0]
        except Exception as e:
            out['error'] = '%s: %s' % (P['env_file'], e)
port = str(port or 443)
out['source'] = source
if host and not shutil.which('openssl'):
    out['error'] = 'openssl не найден на entry'
elif host:
    # The panel-side xray restart may still be in flight: up to N tries.
    tries = max(1, int(P.get('attempts') or 1))
    for attempt in range(tries):
        s = tls_sample(host, port, P['sni'], P['timeout'])
        if any(l.startswith('New, TLSv1.3') for l in s['lines']) or attempt + 1 == tries:
            break
        time.sleep(P.get('retry_pause', 5))
    out.update(ok=True, addr=host, port=port, sample=s)
elif not out.get('error'):
    out['error'] = 'ENTRY_NODE_IP не задан ни в контейнере бота, ни в .env — укажи --tls-probe-addr'
print(json.dumps(out))
'''

HAPROXY_WRITE = r'''
P = PARAMS
out = {'ok': False, 'changed': False}
try:
    cfg = os.path.realpath(P['haproxy_cfg'])
    with open(cfg, encoding='utf-8') as fh:
        text = fh.read()
    cur = [[n.lower() for n in e['names']] for e in parse_acl_lines(text, P['acl'])]
    exp = [[n.lower() for n in names] for names in P['expect']]
    if cur != exp:
        out.update(conflict=True, error='acl изменился после проверки: %s' % cur)
    else:
        new_text = rewrite_acl_text(text, P['acl'], P['target'])
        if new_text == text:
            out.update(ok=True, acl_lines=P['target'])
        else:
            tmp = cfg + '.rotate-new'
            write_like(tmp, new_text, cfg)
            rc, o, e = sh(['haproxy', '-c', '-f', tmp], 30)
            if rc != 0:
                os.unlink(tmp)
                out.update(stage='check', error='haproxy -c: ' + tail(o + e, 300))
            else:
                bak = backup_path(cfg, P['stamp'])
                shutil.copy2(cfg, bak)
                os.replace(tmp, cfg)
                rc, o, e = sh(['systemctl', 'reload', 'haproxy'], 60)
                rc2, o2, e2 = sh(['systemctl', 'is-active', 'haproxy'], 10)
                active = o2.strip()
                if rc != 0 or active != 'active':
                    shutil.copy2(bak, cfg)
                    sh(['systemctl', 'reload', 'haproxy'], 60)
                    rc3, o3, e3 = sh(['systemctl', 'is-active', 'haproxy'], 10)
                    out.update(stage='reload', restored=True, active_after_restore=o3.strip(), backup=bak,
                               error='reload rc=%s, is-active=%s: %s' % (rc, active, tail(e or o, 200)))
                else:
                    with open(cfg, encoding='utf-8') as fh:
                        after = parse_acl_lines(fh.read(), P['acl'])
                    out.update(ok=True, changed=True, backup=bak, active=active,
                               acl_lines=[x['names'] for x in after])
except Exception as e:
    out['error'] = '%s: %s' % (type(e).__name__, e)
print(json.dumps(out))
'''

ENV_WRITE = r'''
P = PARAMS
out = {'ok': False, 'changed': False}
try:
    path = os.path.realpath(P['env_file'])
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    cur, count = parse_env_value(text, P['key'])
    if count > 1:
        out['error'] = '%s задан в .env %d раз' % (P['key'], count)
    elif cur != P['expect']:
        out.update(conflict=True, error='%s изменился после проверки: %r' % (P['key'], cur))
    else:
        new_text = rewrite_env_text(text, P['key'], P['value'])
        if new_text == text:
            out.update(ok=True, value=cur)
        else:
            bak = backup_path(path, P['stamp'])
            shutil.copy2(path, bak)
            os.chmod(bak, 0o600)            # the .env holds every secret of the bot
            tmp = path + '.rotate-new'
            write_like(tmp, new_text, path)
            os.replace(tmp, path)
            with open(path, encoding='utf-8') as fh:
                check = parse_env_value(fh.read(), P['key'])[0]
            out.update(ok=(check == P['value']), changed=True, backup=bak, value=check)
            if not out['ok']:
                out['error'] = 'после записи %s=%r' % (P['key'], check)
except Exception as e:
    out['error'] = '%s: %s' % (type(e).__name__, e)
print(json.dumps(out))
'''

# --force-recreate: a plain restart keeps the container's OLD environment,
# and compose decides "unchanged" on its own hash. --no-deps: a vpn-bot
# recreate must never drag 3x-ui along (the 2026-07-19 panel wipe).
BOT_RECREATE = r'''
import urllib.request
P = PARAMS
out = {'ok': False}


def health():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(P['health_url'], timeout=5) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        return {'status': 'down', 'error': str(e)[:200]}


rc, o, e = sh(['docker', 'compose', 'up', '-d', '--no-deps', '--force-recreate', '--no-build', P['bot_container']],
              300, cwd=P['bot_dir'])
out['compose_rc'] = rc
if rc != 0:
    out['error'] = 'docker compose up: ' + tail(o + e, 300)
else:
    deadline = time.time() + P['wait']
    hj = health()
    while hj.get('status') != 'healthy' and time.time() < deadline:
        time.sleep(P.get('poll', 5))
        hj = health()
    out['health'] = hj.get('status')
    out['version'] = hj.get('version')
    rc, o, e = sh(['docker', 'exec', P['bot_container'], 'printenv', 'SNI_VALUE'], 15)
    out['container_sni'] = o.strip() if rc == 0 else None
    # expect None = rollback to "SNI_VALUE not in .env": compose substitutes its own default
    out['ok'] = out['health'] == 'healthy' and (P['expect_sni'] is None or out['container_sni'] == P['expect_sni'])
    if not out['ok']:
        out['error'] = 'после пересоздания: health=%s, SNI_VALUE в контейнере=%r (ждали %r)' % (
            out['health'], out['container_sni'], P['expect_sni'])
print(json.dumps(out))
'''

PROBE_REGEN = r'''
P = PARAMS
out = {'ok': False}
try:
    cfgp = os.path.realpath(P['probe_cfg'])
    rc, o, e = sh(['docker', 'exec', '-e', 'PYTHONPATH=/app', P['bot_container'],
                   'python3', '/app/scripts/gen_probe_config.py'], 90)
    if rc != 0:
        out['error'] = 'gen_probe_config.py rc=%s: %s' % (rc, tail(e, 300))
    else:
        cfg = json.loads(o)           # stays on entry: it carries the probe client's credentials
        sn = probe_reality_server_name(cfg)
        if sn != P['expect_sni']:
            out['error'] = 'сгенерированный конфиг со SNI %r, ждали %r' % (sn, P['expect_sni'])
        else:
            tmp = cfgp + '.rotate-new'
            write_like(tmp, json.dumps(cfg, indent=2) + '\n', cfgp, None if os.path.exists(cfgp) else 0o644)
            inner = P['probe_mount'].rstrip('/') + '/' + os.path.basename(tmp)
            rc, o2, e2 = sh(['docker', 'exec', P['probe_container'], 'sing-box', 'check', '-c', inner], 30)
            if rc != 0:
                os.unlink(tmp)
                out['error'] = 'sing-box check: ' + tail(o2 + e2, 300)
            else:
                bak = None
                if os.path.exists(cfgp):
                    bak = backup_path(cfgp, P['stamp'])
                    shutil.copy2(cfgp, bak)
                os.replace(tmp, cfgp)
                rc, o3, e3 = sh(['docker', 'restart', P['probe_container']], 90)
                out.update(ok=(rc == 0), backup=bak, server_name=sn)
                if rc != 0:
                    out['error'] = 'docker restart %s: %s' % (P['probe_container'], tail(e3, 200))
except Exception as e:
    out['error'] = '%s: %s' % (type(e).__name__, e)
print(json.dumps(out))
'''

ACL_FUNCS = (parse_acl_lines, rewrite_acl_text)
ENV_FUNCS = (_env_assignment, parse_env_value, rewrite_env_text)
PANEL_FUNCS = (parse_blob, read_reality, apply_reality_changes, same_panel)


def remote_script(driver: str, params: dict, funcs=()) -> str:
    """Self-contained python for ``python3 -`` on a host: imports, PARAMS
    (repr of a JSON string — a valid literal whatever it holds), the shared
    helpers verbatim, the common block, the driver."""
    parts = ['import json, os, re, shutil, subprocess, sys, time',
             'PARAMS = json.loads(%r)' % json.dumps(params)]
    parts += [textwrap.dedent(inspect.getsource(f)) for f in funcs]
    parts += [REMOTE_COMMON, driver]
    return '\n\n'.join(parts) + '\n'


# ---------------------------------------------------------------------------
# Local pure helpers (planning, verdicts, rendering)
# ---------------------------------------------------------------------------

_LABEL = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
HOST_RE = re.compile(r'^(?=.{4,253}$)' + _LABEL + r'(?:\.' + _LABEL + r')+$')
IPV4_RE = re.compile(r'^(?:\d{1,3}\.){3}\d{1,3}$')
PATH_RE = re.compile(r'^/[A-Za-z0-9._/-]+$')
ACL_RE = re.compile(r'^[A-Za-z0-9_.-]+$')


def parse_sni(value: str) -> str:
    """A DNS name, lowercased (an IP is not an SNI)."""
    v = (value or '').strip().lower().rstrip('.')
    if not HOST_RE.match(v) or IPV4_RE.match(v):
        raise ValueError(f'не похоже на DNS-имя: {value!r}')
    return v


def parse_dest(value: Optional[str], sni: Optional[str]) -> str:
    """``host:port`` for realitySettings.dest; default ``<sni>:443``."""
    if not value:
        return f'{sni}:443'
    v = value.strip().lower()
    host, sep, port = v.rpartition(':')
    if not sep:
        host, port = v, '443'
    if not (HOST_RE.match(host) or IPV4_RE.match(host)) or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f'нужно host:port, получено {value!r}')
    return f'{host}:{int(port)}'


def split_dest(dest: str) -> Tuple[str, int]:
    host, _, port = dest.rpartition(':')
    return host, int(port)


def lower_names(xs) -> List[str]:
    return [str(x).lower() for x in (xs or [])]


def _names(xs) -> str:
    return ' '.join(str(x) for x in xs) if xs else '—'


def transform_names(names: List[str], removed: List[str], added: List[str]) -> List[str]:
    """New name list for one ACL line: each removed name is replaced IN
    PLACE by the next added name not already there, leftovers are appended,
    unrelated names stay put. Case-insensitive, order-preserving, no
    duplicates — so the diff an operator reads is "bing -> google", not a
    reshuffled line."""
    rm = {n.lower() for n in removed}
    have = {n.lower() for n in names}
    pending = [a for a in added if a.lower() not in have]
    out = []
    for n in names:
        if n.lower() in rm:
            if pending:
                out.append(pending.pop(0))
            continue
        out.append(n)
    out.extend(pending)
    seen, uniq = set(), []
    for n in out:
        if n.lower() not in seen:
            seen.add(n.lower())
            uniq.append(n)
    return uniq


def parse_tls_sample(sample: Optional[dict]) -> dict:
    """Facts of one ``openssl s_client -tls1_3 -alpn h2 -msg -verify_hostname`` run."""
    lines = (sample or {}).get('lines') or []
    text = '\n'.join(lines)
    alpn = re.search(r'ALPN protocol:\s*(\S+)', text)
    ver = re.search(r'Verify return code:\s*(\d+)\s*\(([^)]*)\)', text)
    tls13 = bool(re.search(r'^New, TLSv1\.3\b', text, re.M))
    error = None
    if not tls13:
        errs = [x for x in lines if re.search(r'error|errno|refused|timed out|timeout|getaddrinfo|alert', x, re.I)]
        error = (errs[0] if errs else f'рукопожатие не завершилось (rc={(sample or {}).get("rc")})')[:160]
    return {
        'record_len': parse_cert_record_len(lines),
        'tls13': tls13,
        'alpn': alpn.group(1) if alpn else None,
        'verify': int(ver.group(1)) if ver else None,
        'verify_text': ver.group(2) if ver else None,
        'error': error,
    }


def cert_problems(samples: List[dict], limit: int = CERT_RECORD_LIMIT) -> List[str]:
    """Why the candidate is NOT a safe Reality dest (empty list = safe).
    Every sample must pass; the record size is judged on the MAX across
    samples because CDN edges serve different chains (§23: one user got
    through once on an edge that still had the small cert)."""
    if not samples:
        return ['ни одного замера']
    n = len(samples)
    problems = []
    failed = [s for s in samples if not s['tls13']]
    if failed:
        problems.append(f'TLS 1.3 не установился в {len(failed)}/{n} замерах ({failed[0]["error"]})')
    ok = [s for s in samples if s['tls13']]
    sizes = [s['record_len'] for s in ok if s['record_len'] is not None]
    if len(sizes) < len(ok):
        problems.append(f'Certificate-запись не видна в {len(ok) - len(sizes)}/{n} замерах (openssl -msg)')
    if sizes and max(sizes) > limit:
        problems.append(f'Certificate-запись {max(sizes)} Б > {limit} (буфер xtls/reality — 8192, §23)')
    no_h2 = [s for s in ok if s['alpn'] != 'h2']
    if no_h2:
        problems.append(f'ALPN h2 не согласован ({no_h2[0]["alpn"] or "No ALPN"})')
    bad = [s for s in ok if s['verify'] != 0]
    if bad:
        problems.append('сертификат не валиден для SNI ('
                        + (bad[0]['verify_text'] or f'verify code {bad[0]["verify"]}') + ')')
    return problems


def cert_summary(samples: List[dict]) -> str:
    ok = [s for s in samples if s['tls13']]
    sizes = [s['record_len'] for s in ok if s['record_len'] is not None]
    if not ok:
        return samples[0]['error'] if samples else 'нет замера'
    parts = [f'Certificate {max(sizes)} Б' if sizes else 'Certificate —', 'TLS 1.3',
             f'ALPN {ok[0]["alpn"] or "нет"}',
             'сертификат валиден' if all(s['verify'] == 0 for s in ok) else 'сертификат НЕ валиден']
    if len(samples) > 1:
        parts.append(f'{len(ok)}/{len(samples)} замеров')
    return ', '.join(parts)


def values_from_state(state: dict) -> dict:
    """The per-layer values a rotation reads/writes; None for a layer that
    could not be read. This is also the snapshot's ``old``/``new`` shape."""
    def ok(layer):
        return bool((state.get(layer) or {}).get('ok'))
    v = {'panel': None, 'haproxy': None, 'env': None, 'bot': None, 'probe': None}
    if ok('panel'):
        ib = state['panel']['inbound']
        v['panel'] = {k: copy.deepcopy(ib.get(k)) for k in (
            'id', 'tag', 'protocol', 'network', 'security', 'dest_fields', 'server_names',
            'settings_server_name', 'clients', 'with_flow')}
    if ok('haproxy'):
        v['haproxy'] = {'acl_lines': [list(x) for x in state['haproxy'].get('acl_lines') or []]}
    if ok('env'):
        v['env'] = {'sni': state['env'].get('sni')}
    if ok('bot'):
        v['bot'] = {'container_sni': state['bot'].get('container_sni')}
    if ok('probe'):
        v['probe'] = {'present': bool(state['probe'].get('present')),
                      'server_name': state['probe'].get('server_name')}
    return v


def plan_target(cur: dict, sni: str, dest: str, keep_old: bool) -> dict:
    """Target values for a rotation to ``sni``/``dest`` from current values.

    serverNames = [sni] (+ the current names with --keep-old-sni). Every
    ACL line drops what serverNames drops and gains what it gains, in place
    — so each line stays a superset of serverNames, and names in the ACL
    that the panel never had (e.g. a second name kept there on purpose) are
    left alone. settings.serverName (the panel's own share-link hint) moves
    only when it pointed at a name being removed."""
    panel = cur['panel']
    cur_names = lower_names(panel['server_names'])
    new_names = [sni] + ([n for n in cur_names if n != sni] if keep_old else [])
    removed = [n for n in cur_names if n not in new_names]
    ssn = panel.get('settings_server_name')
    new_ssn = sni if (ssn and ssn.lower() in removed) else ssn
    return {
        'panel': {
            'id': panel.get('id'), 'tag': panel.get('tag'),
            'protocol': panel.get('protocol'), 'network': panel.get('network'),
            'security': panel.get('security'),
            'dest_fields': {k: dest for k in (panel.get('dest_fields') or {'dest': None})},
            'server_names': new_names,
            'settings_server_name': new_ssn,
            'clients': panel.get('clients'), 'with_flow': panel.get('with_flow'),
        },
        'haproxy': {'acl_lines': [transform_names(line, removed, new_names)
                                  for line in cur['haproxy']['acl_lines']]},
        'env': {'sni': sni},
        'bot': {'container_sni': sni},
        'probe': {'present': cur['probe']['present'],
                  'server_name': sni if cur['probe']['present'] else None},
    }


def acl_equal(a, b) -> bool:
    return [lower_names(x) for x in (a or [])] == [lower_names(x) for x in (b or [])]


def sni_matches(actual: Optional[str], expected: Optional[str]) -> bool:
    """A container/probe value against the target SNI_VALUE. Expected None =
    the target is "not set in .env": compose then substitutes its own
    default, so any value is what the stack legitimately runs with."""
    return expected is None or actual == expected


def steps_needed(cur: dict, target: dict, order) -> List[str]:
    """Which steps move ``cur`` to ``target``, in ``order``. A layer whose
    current value is unknown (None) is assumed to need its step — the step
    itself re-reads (panel) or refuses."""
    need = set()
    if cur.get('panel') is None or not same_panel(cur['panel'], target['panel']):
        need.add('panel')
    if cur.get('haproxy') is None or not acl_equal(cur['haproxy']['acl_lines'], target['haproxy']['acl_lines']):
        need.add('haproxy')
    env_change = cur.get('env') is None or cur['env']['sni'] != target['env']['sni']
    if env_change:
        need.add('env')
    if env_change or cur.get('bot') is None or not sni_matches(cur['bot']['container_sni'], target['env']['sni']):
        need.add('bot')
    if (target.get('probe') or {}).get('present') and (
            'bot' in need or cur.get('probe') is None
            or not sni_matches(cur['probe']['server_name'], target['env']['sni'])):
        need.add('probe')
    return [s for s in order if s in need]


def diff_values(cur: dict, new: dict, acl: str) -> List[Tuple[str, str, str, str]]:
    """(layer, field, old, new) rows for every value that changes."""
    rows = []
    cp, np_ = cur['panel'], new['panel']
    for k in sorted(set(cp['dest_fields']) | set(np_['dest_fields'])):
        a, b = cp['dest_fields'].get(k), np_['dest_fields'].get(k)
        if a != b:
            rows.append(('panel', k, str(a or '—'), str(b or '—')))
    if lower_names(cp['server_names']) != lower_names(np_['server_names']):
        rows.append(('panel', 'serverNames', _names(cp['server_names']), _names(np_['server_names'])))
    if cp.get('settings_server_name') != np_.get('settings_server_name'):
        rows.append(('panel', 'settings.serverName', str(cp.get('settings_server_name') or '—'),
                     str(np_.get('settings_server_name') or '—')))
    for i, (a, b) in enumerate(zip(cur['haproxy']['acl_lines'], new['haproxy']['acl_lines'])):
        if lower_names(a) != lower_names(b):
            rows.append(('haproxy', f'acl {acl} (строка {i + 1})', _names(a), _names(b)))
    if cur['env']['sni'] != new['env']['sni']:
        rows.append(('env', 'SNI_VALUE', cur['env']['sni'] or '(не задан)', new['env']['sni'] or '(не задан)'))
    return rows


@dataclass
class Plan:
    sni: str
    dest: str
    keep_old: bool
    old: Optional[dict] = None
    new: Optional[dict] = None
    steps: List[str] = field(default_factory=list)
    diff: List[Tuple[str, str, str, str]] = field(default_factory=list)
    candidate: List[dict] = field(default_factory=list)
    current_cert: Optional[List[dict]] = None
    compat: Optional[List[dict]] = None
    errors: List[str] = field(default_factory=list)
    blind: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.blind:
            return EXIT_BLIND
        return EXIT_FAIL if self.errors else EXIT_OK


LAYER_LABELS = (('panel', 'панель exit'), ('haproxy', 'haproxy entry'), ('env', '.env бота'),
                ('bot', 'контейнер vpn-bot'), ('probe', 'probe-proxy'))


def blind_layers(state: dict) -> List[str]:
    """One line per layer that could not be read. Panel calls go THROUGH
    the vpn-bot container, so a dead bot is named as the reason."""
    out = []
    bot_state = (state.get('bot') or {}).get('state')
    for layer, label in LAYER_LABELS:
        part = state.get(layer) or {}
        if part.get('ok'):
            continue
        line = f'{label}: {part.get("error") or "нет данных"}'
        if layer == 'panel' and bot_state not in (None, 'running'):
            line += (f' — панель читается через контейнер {BOT_CONTAINER}, а он {bot_state}: подними его '
                     f'(cd {BOT_DIR} && docker compose up -d --no-deps {BOT_CONTAINER})')
        out.append(line)
    return out


def build_plan(state: dict, tls: dict, opts, target_override: Optional[dict] = None) -> Plan:
    """Pure: verdict + target + diff from what the collectors returned."""
    plan = Plan(sni=opts.sni, dest=opts.dest_resolved, keep_old=opts.keep_old_sni)
    plan.blind = blind_layers(state)
    if not tls.get('ok'):
        plan.blind.append(f'exit (замер кандидата): {tls.get("error") or "нет данных"}')
    probes = tls.get('tls') or {}
    plan.candidate = [parse_tls_sample(s) for s in probes.get('candidate') or []]
    if probes.get('current'):
        plan.current_cert = [parse_tls_sample(s) for s in probes['current']]
    if probes.get('compat'):
        plan.compat = [parse_tls_sample(s) for s in probes['compat']]
    if plan.blind:
        return plan

    cur = values_from_state(state)
    plan.old = cur
    ib = cur['panel']
    if (ib.get('protocol') or '').lower() != 'vless' or (ib.get('security') or '').lower() != 'reality':
        plan.errors.append(f'inbound {ib.get("id")} — не VLESS-Reality ({ib.get("protocol")}/{ib.get("security")}): '
                           'не тот --inbound-id?')
    if not ib.get('dest_fields'):
        plan.errors.append('в realitySettings нет ни dest, ни target — другая версия панели? правь руками')
    h = state['haproxy']
    if not cur['haproxy']['acl_lines']:
        plan.errors.append(f'в {opts.haproxy_cfg} нет активной строки «acl {opts.acl} req_ssl_sni …» — правь руками')
    for err in h.get('errors') or []:
        plan.errors.append(f'haproxy: {err}')
    if h.get('active') != 'active':
        plan.errors.append(f'haproxy на entry не active ({h.get("active")}) — сначала подними его')
    if not h.get('writable'):
        plan.errors.append(f'{opts.haproxy_cfg} не доступен на запись через ssh {opts.entry} (нужен root)')
    if not h.get('haproxy_bin'):
        plan.errors.append('на entry нет бинаря haproxy — нечем проверить конфиг (haproxy -c)')
    env = state['env']
    if (env.get('count') or 0) > 1:
        plan.errors.append(f'SNI_VALUE задан в {opts.env_file} {env["count"]} раз — сведи к одному руками')
    if not env.get('writable'):
        plan.errors.append(f'{opts.env_file} не доступен на запись через ssh {opts.entry} (нужен root)')
    bot = state['bot']
    if bot.get('state') != 'running':
        plan.errors.append(f'контейнер {BOT_CONTAINER} не running ({bot.get("state")})')
    elif bot.get('health') != 'healthy':
        plan.errors.append(f'бот не healthy ещё до ротации ({bot.get("health")}) — сначала разберись: '
                           f'docker logs {BOT_CONTAINER} --tail 50')
    for p in cert_problems(plan.candidate):
        plan.errors.append(f'кандидат {plan.sni}: {p}')

    if target_override is not None:
        plan.new = copy.deepcopy(target_override)
    else:
        plan.new = plan_target(cur, plan.sni, plan.dest, plan.keep_old)
    if any(not line for line in plan.new['haproxy']['acl_lines']):
        plan.errors.append('после замены строка acl осталась бы пустой — правь руками')
    plan.steps = steps_needed(cur, plan.new, APPLY_ORDER)
    plan.diff = diff_values(cur, plan.new, opts.acl)
    _plan_warnings(plan, cur, opts, state)
    return plan


def _plan_warnings(plan: Plan, cur: dict, opts, state: dict) -> None:
    names_now = lower_names(cur['panel']['server_names'])
    env_now = cur['env']['sni']
    w = plan.warnings
    if not env_now:
        w.append(f'SNI_VALUE не задан в {opts.env_file} — compose подставляет свой дефолт '
                 '(в docker-compose.yml это www.microsoft.com, тот самый 8273 Б); шаг 3 его допишет')
    elif env_now.lower() not in names_now:
        w.append(f'слои УЖЕ рассинхронизированы: SNI_VALUE={env_now}, а serverNames панели [{_names(names_now)}] '
                 '— у обновивших подписку Reality не работает уже сейчас')
    for i, line in enumerate(cur['haproxy']['acl_lines']):
        missing = [n for n in names_now if n not in lower_names(line)]
        if missing:
            w.append(f'acl {opts.acl} (строка {i + 1}) не содержит {", ".join(missing)} из serverNames — '
                     'такие клиенты сейчас не доходят до exit')
    if cur['bot']['container_sni'] != env_now:
        w.append(f'контейнер бота работает с SNI_VALUE={cur["bot"]["container_sni"]!r}, а в .env {env_now!r} — '
                 '.env правили без пересоздания контейнера')
    probe = (state.get('probe') or {}).get('container')
    if not cur['probe']['present']:
        w.append(f'конфиг probe-proxy ({opts.probe_cfg}) не найден — шаг 4 пропускается, пробы ротацию не увидят')
    else:
        if cur['probe']['server_name'] != env_now:
            w.append(f'probe-proxy уже сейчас на SNI {cur["probe"]["server_name"]!r}, а SNI_VALUE {env_now!r}')
        if probe not in (None, 'running'):
            w.append(f'контейнер {PROBE_CONTAINER} не запущен ({probe}) — шаг 4 упадёт на sing-box check '
                     '(мягко: ротация останется, пробы — нет)')
    if (cur['panel'].get('with_flow') or 0) < (cur['panel'].get('clients') or 0):
        w.append(f'{cur["panel"]["clients"] - cur["panel"]["with_flow"]} клиентов Reality без flow — '
                 'отдельная беда (§28): verify_panel_client_fields.py / restore_reality_flow.py')
    removed = [n for n in names_now if n not in lower_names(plan.new['panel']['server_names'])]
    if removed and 'panel' in plan.steps:
        w.append(f'клиенты со старым SNI ({", ".join(removed)}) перестанут проходить Reality сразу после шага 1 '
                 'и вернутся, только обновив подписку (sing-box/Hiddify/FlClash — сами по интервалу обновления, '
                 'vless://-ссылки — вручную). --keep-old-sni держит старое имя на переходный период, '
                 'если новый dest его принимает')
    if plan.keep_old and plan.compat is not None:
        problems = cert_problems(plan.compat)
        if problems:
            w.append('--keep-old-sni: новый dest НЕ принимает старый SNI (' + problems[0]
                     + ') — старых клиентов это не спасёт')


def plan_verdict(plan: Plan, hint: bool = True, head: str = 'ИТОГ') -> str:
    """First line of a plan. ``head`` is 'ПЛАН' when an apply is about to ask
    for confirmation — the run's only ИТОГ line is then its outcome."""
    if plan.blind:
        more = f' (+ ещё {len(plan.blind) - 1})' if len(plan.blind) > 1 else ''
        return f'ИТОГ: НЕ СМОГ ПОСМОТРЕТЬ — {plan.blind[0]}{more}; план не строится'
    if plan.errors:
        more = f' (+ ещё {len(plan.errors) - 1})' if len(plan.errors) > 1 else ''
        return f'ИТОГ: ПРОВЕРКА НЕ ПРОШЛА — {plan.errors[0]}{more}'
    cs = cert_summary(plan.candidate)
    if not plan.steps:
        return f'ИТОГ: все слои уже на {plan.sni} — менять нечего (кандидат: {cs})'
    titles = {'panel': 'панель', 'haproxy': 'haproxy', 'env': '.env', 'bot': 'бот', 'probe': 'probe-proxy'}
    layers = [titles[s] for s in plan.steps]
    tail = ' — применить: --apply' if hint else ''
    return f'{head}: кандидат {plan.sni} годен ({cs}); меняются: {", ".join(layers)}{tail}'


def render_plan(plan: Plan, opts, note: Optional[str] = None, hint: bool = True, head: str = 'ИТОГ') -> str:
    out = [plan_verdict(plan, hint, head)]
    if plan.candidate:
        out.append(f'Кандидат {plan.sni} (dest {plan.dest}), замер с exit ×{len(plan.candidate)}: '
                   f'{cert_summary(plan.candidate)}')
    if plan.current_cert and plan.old and plan.old.get('panel'):
        dests = ', '.join(sorted(set(map(str, plan.old['panel']['dest_fields'].values())))) or '—'
        out.append(f'Текущий dest {dests}: {cert_summary(plan.current_cert)}')
    if plan.compat:
        out.append(f'Старый SNI через новый dest (--keep-old-sni): {cert_summary(plan.compat)}')
    if note:
        out.append(note)
    if plan.old and plan.new:
        o, n = plan.old, plan.new
        if plan.steps:
            out.append('План (НЕ будет применён, пока есть ошибки):' if plan.errors else 'План (в этом порядке):')
            by_layer: Dict[str, List[str]] = {}
            for layer, fld, a, b in plan.diff:
                by_layer.setdefault(layer, []).append(f'{fld}: {a} → {b}')
            if 'panel' in plan.steps:
                out.append(f'  1. панель exit · inbound {o["panel"]["id"]} ({o["panel"]["tag"]})')
                out += [f'       {x}' for x in by_layer.get('panel', [])]
                out.append('       + рестарт xray через панель (сессии Reality/WS/SS переподключатся)'
                           if opts.xray_restart else '       рестарт xray: НЕТ (--no-xray-restart)')
            if 'haproxy' in plan.steps:
                out.append(f'  2. haproxy entry · {opts.haproxy_cfg}')
                out += [f'       {x}' for x in by_layer.get('haproxy', [])]
                out.append('       haproxy -c на копии → reload; если reload упал — файл возвращается')
            if 'env' in plan.steps or 'bot' in plan.steps:
                out.append(f'  3. бот entry · {opts.env_file}')
                out += [f'       {x}' for x in by_layer.get('env', [])]
                if 'bot' in plan.steps:
                    out.append(f'       + пересоздание {BOT_CONTAINER} (docker compose up -d --no-deps '
                               '--force-recreate), ждём /health healthy')
            if 'probe' in plan.steps:
                out.append(f'  4. probe-proxy · {opts.probe_cfg}: reality server_name '
                           f'{(o["probe"] or {}).get("server_name") or "—"} → {n["probe"]["server_name"]} '
                           '(gen_probe_config.py → sing-box check → restart)')
            same = [label for key, label in (('panel', 'панель'), ('haproxy', 'haproxy'), ('env', '.env'),
                                             ('bot', 'контейнер бота'), ('probe', 'probe-proxy'))
                    if key not in plan.steps and not (key == 'probe' and not o['probe']['present'])]
            if same:
                out.append('Без изменений: ' + ', '.join(same))
    if plan.warnings:
        out.append('Предупреждения:')
        out += [f'  ! {x}' for x in plan.warnings]
    if plan.errors:
        out.append('Ошибки:')
        out += [f'  ✗ {x}' for x in plan.errors]
    if plan.blind:
        out.append('Не смог посмотреть:')
        out += [f'  ? {x}' for x in plan.blind]
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# Verification (pure core: verify_checks)
# ---------------------------------------------------------------------------

def target_from_values(values: dict) -> dict:
    """Exact verify target from a snapshot's old/new values."""
    panel = values['panel']
    return {
        'sni': values['env']['sni'],
        'dest_fields': dict(panel['dest_fields']),
        'server_names': list(panel['server_names']),
        'acl_lines': [list(x) for x in values['haproxy']['acl_lines']],
        'probe_present': bool((values.get('probe') or {}).get('present')),
        'clients_before': panel.get('clients'),
    }


def target_from_args(opts) -> dict:
    """Loose verify target from --sni/--dest: every layer must carry the
    name; exact lists are unknown without a snapshot."""
    return {'sni': opts.sni, 'dest': opts.dest_resolved, 'server_names': None, 'acl_lines': None,
            'probe_present': None, 'clients_before': None}


def verify_checks(state: dict, xray: dict, tls: dict, target: dict,
                  strict_xray: bool) -> List[Tuple[str, str, str]]:
    """Pure: (status, label, detail) rows; status OK / FAIL / WARN / BLIND."""
    checks = []
    sni = target['sni']
    panel = state.get('panel') or {}
    ib = None
    if not panel.get('ok'):
        checks.append(('BLIND', 'панель exit', str(panel.get('error') or 'нет данных')))
    else:
        ib = panel['inbound']
        dests = set(map(str, (ib.get('dest_fields') or {}).values()))
        if 'dest_fields' in target:
            ok_dest = dict(ib.get('dest_fields') or {}) == target['dest_fields']
        else:
            ok_dest = bool(dests) and dests == {target['dest']}
        names = lower_names(ib.get('server_names'))
        ok_names = (sorted(names) == sorted(lower_names(target['server_names']))
                    if target.get('server_names') is not None else sni in names)
        checks.append(('OK' if ok_dest and ok_names else 'FAIL', f'панель exit · inbound {ib.get("id")}',
                       f'dest {", ".join(sorted(dests)) or "—"}, serverNames [{_names(ib.get("server_names"))}], '
                       f'клиентов {ib.get("clients")} (с flow {ib.get("with_flow")})'))
        if (ib.get('with_flow') or 0) < (ib.get('clients') or 0):
            checks.append(('WARN', 'панель: flow', f'{ib["clients"] - ib["with_flow"]} клиентов без flow — '
                           'verify_panel_client_fields.py / restore_reality_flow.py (§28)'))
        before = target.get('clients_before')
        if before is not None and (ib.get('clients') or 0) < before:
            checks.append(('WARN', 'панель: клиенты', f'было {before}, стало {ib.get("clients")} — ключ выдали '
                           'во время update inbound? сверь с ботом'))
    if ib is not None:
        if not xray.get('ok'):
            checks.append(('BLIND', 'exit config.json', str(xray.get('error') or 'нет данных')))
        elif xray.get('xray') is None:
            checks.append(('WARN', 'exit config.json', str(xray.get('xray_error') or 'не прочитан')))
        else:
            x = xray['xray']
            same = (set(map(str, x['dest_fields'].values())) == set(map(str, (ib.get('dest_fields') or {}).values()))
                    and sorted(lower_names(x['server_names'])) == sorted(lower_names(ib.get('server_names'))))
            if same:
                checks.append(('OK', 'exit config.json', 'dest/serverNames рантайма совпадают с панелью'))
            else:
                detail = (f'рантайм: dest {", ".join(map(str, x["dest_fields"].values())) or "—"}, '
                          f'serverNames [{_names(x["server_names"])}] — ')
                detail += ('xray не подхватил панель: рестарт xray через панель'
                           if strict_xray else 'файл обновляется при рестарте xray; без рестарта это ожидаемо')
                checks.append(('FAIL' if strict_xray else 'WARN', 'exit config.json', detail))
    h = state.get('haproxy') or {}
    if not h.get('ok'):
        checks.append(('BLIND', 'haproxy entry', str(h.get('error') or 'нет данных')))
    else:
        lines = h.get('acl_lines') or []
        if target.get('acl_lines') is not None:
            ok_acl = ([sorted(lower_names(x)) for x in lines]
                      == [sorted(lower_names(x)) for x in target['acl_lines']])
        else:
            ok_acl = bool(lines) and all(sni in lower_names(x) for x in lines)
        active = h.get('active') == 'active'
        checks.append(('OK' if ok_acl and active else 'FAIL', 'haproxy entry',
                       f'{h.get("active")}, acl: ' + ' | '.join(_names(x) for x in lines)))
    e = state.get('env') or {}
    if not e.get('ok'):
        checks.append(('BLIND', '.env бота', str(e.get('error') or 'нет данных')))
    else:
        checks.append(('OK' if e.get('sni') == sni else 'FAIL', '.env бота', f'SNI_VALUE={e.get("sni")}'))
    b = state.get('bot') or {}
    if not b.get('ok'):
        checks.append(('BLIND', 'бот', str(b.get('error') or 'нет данных')))
    else:
        good = b.get('health') == 'healthy' and sni_matches(b.get('container_sni'), sni)
        checks.append(('OK' if good else 'FAIL', 'бот (контейнер)',
                       f'{b.get("state")}, /health {b.get("health")}, SNI_VALUE в контейнере {b.get("container_sni")}'))
    p = state.get('probe') or {}
    if not p.get('ok'):
        checks.append(('BLIND', 'probe-proxy', str(p.get('error') or 'нет данных')))
    elif not p.get('present'):
        checks.append(('FAIL' if target.get('probe_present') else 'WARN', 'probe-proxy',
                       'конфиг не найден — пробы reality ротацию не видят'))
    else:
        good = sni_matches(p.get('server_name'), sni) and p.get('container') == 'running'
        checks.append(('OK' if good else 'FAIL', 'probe-proxy',
                       f'reality server_name {p.get("server_name")}, контейнер {p.get("container")}'
                       + ('' if good else ' — повтори --apply (продолжит по снимку)')))
    if not sni:
        checks.append(('WARN', 'TLS через entry', 'SNI_VALUE не задан — проверять нечего'))
    elif not tls.get('ok'):
        checks.append(('BLIND', 'TLS через entry', str(tls.get('error') or 'нет данных')))
    else:
        s = parse_tls_sample(tls.get('sample'))
        where = f'{tls.get("addr")}:{tls.get("port")}, SNI {sni}'
        if s['tls13'] and s['verify'] == 0:
            size = f', Certificate {s["record_len"]} Б' if s['record_len'] else ''
            checks.append(('OK', 'TLS через entry',
                           f'{where}: рукопожатие TLS 1.3 завершилось{size}, сертификат валиден'))
        elif s['tls13']:
            checks.append(('FAIL', 'TLS через entry', f'{where}: рукопожатие есть, но сертификат не для SNI '
                           f'({s["verify_text"] or s["verify"]}) — haproxy увёл SNI не на exit?'))
        else:
            checks.append(('FAIL', 'TLS через entry', f'{where}: рукопожатие не завершилось — {s["error"]}'))
    return checks


def verify_code(checks) -> int:
    """A definite failure wins (1); otherwise "could not look" is 2, never 0."""
    if any(c[0] == 'FAIL' for c in checks):
        return EXIT_FAIL
    if any(c[0] == 'BLIND' for c in checks):
        return EXIT_BLIND
    return EXIT_OK


def render_verify(code: int, checks, target: dict, note: Optional[str] = None) -> str:
    bad = [c[1] for c in checks if c[0] == 'FAIL']
    blind = [c[1] for c in checks if c[0] == 'BLIND']
    if code == EXIT_FAIL:
        head = 'ИТОГ: verify НЕ ПРОШЁЛ — ' + ', '.join(bad)
    elif code == EXIT_BLIND:
        head = 'ИТОГ: verify НЕ СМОГ ПОСМОТРЕТЬ — ' + ', '.join(blind)
    else:
        n = len([c for c in checks if c[0] == 'OK'])
        head = f'ИТОГ: verify OK — все слои на {target["sni"]} ({n} проверок)'
    out = [head]
    if note:
        out.append(note)
    out += [f'  [{st}] {label}: {detail}' for st, label, detail in checks]
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

class SnapshotError(Exception):
    """The snapshot exists but is not one of ours (exit 2)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class SnapshotStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> Optional[dict]:
        try:
            with open(self.path, encoding='utf-8') as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            raise SnapshotError(f'{self.path}: {e}')
        if (not isinstance(data, dict) or data.get('version') != SNAPSHOT_VERSION
                or not isinstance(data.get('old'), dict) or not isinstance(data.get('new'), dict)):
            raise SnapshotError(f'{self.path}: не снимок rotate_reality_dest (version/old/new)')
        return data

    def save(self, data: dict) -> None:
        data['updated_at'] = now_iso()
        d = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.rotation_snapshot.', suffix='.tmp', dir=d)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write('\n')
        os.replace(tmp, self.path)

    def archive(self) -> Optional[str]:
        """Move the current snapshot aside (kept, never deleted)."""
        if not os.path.exists(self.path):
            return None
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        root, ext = os.path.splitext(self.path)
        dst = f'{root}.{stamp}{ext or ".json"}'
        os.replace(self.path, dst)
        return dst


def new_snapshot(plan: Plan, opts) -> dict:
    return {
        'version': SNAPSHOT_VERSION,
        'tool': 'scripts/rotate_reality_dest.py',
        'status': 'pending',
        'created_at': now_iso(),
        'updated_at': None,
        'hosts': {'entry': opts.entry, 'exit': opts.exit_host},
        'options': {'sni': plan.sni, 'dest': plan.dest, 'keep_old_sni': plan.keep_old,
                    'inbound_id': plan.old['panel']['id'], 'haproxy_cfg': opts.haproxy_cfg,
                    'acl': opts.acl, 'env_file': opts.env_file, 'probe_cfg': opts.probe_cfg},
        'old': copy.deepcopy(plan.old),
        'new': copy.deepcopy(plan.new),
        'steps': [],
        'backups': {},
        'runs': [],
    }


def same_options(snap: dict, opts) -> bool:
    o = snap.get('options') or {}
    return (o.get('sni') == opts.sni and o.get('dest') == opts.dest_resolved
            and bool(o.get('keep_old_sni')) == bool(opts.keep_old_sni))


def rollback_conflicts(cur: dict, old: dict, new: dict) -> List[str]:
    """Layers holding neither the snapshot's old nor its new value: somebody
    changed them after the rotation. Rolling those back would clobber that."""
    out = []
    if cur['panel'] is not None and not (same_panel(cur['panel'], old['panel'])
                                         or same_panel(cur['panel'], new['panel'])):
        out.append(f'панель: сейчас dest {cur["panel"]["dest_fields"]}, '
                   f'serverNames [{_names(cur["panel"]["server_names"])}]')
    if cur['haproxy'] is not None and not (acl_equal(cur['haproxy']['acl_lines'], old['haproxy']['acl_lines'])
                                           or acl_equal(cur['haproxy']['acl_lines'], new['haproxy']['acl_lines'])):
        out.append('haproxy: сейчас acl ' + ' | '.join(_names(x) for x in cur['haproxy']['acl_lines']))
    if cur['env'] is not None and cur['env']['sni'] not in (old['env']['sni'], new['env']['sni']):
        out.append(f'.env: сейчас SNI_VALUE={cur["env"]["sni"]}')
    return out


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def do_step(io, step: str, cur: dict, target: dict, opts) -> dict:
    """Run one step; the host side re-checks that the layer still holds
    ``cur`` before writing (a stale plan conflicts instead of landing)."""
    tsni = target['env']['sni']
    if step == 'panel':
        expect = cur.get('panel')
        if expect is None:            # revert path: read right before acting
            r = io.read_panel()
            if not r.get('ok'):
                return {'ok': False, 'error': f'панель не читается: {r.get("error")}'}
            expect = r['inbound']
            if same_panel(expect, target['panel']):
                return {'ok': True, 'noop': True, 'after': expect}
            guard = (cur.get('_guard') or {}).get('panel')
            if guard is not None and not same_panel(expect, guard):
                return {'ok': False, 'conflict': True,
                        'error': f'панель ни на старых, ни на новых значениях (dest {expect.get("dest_fields")}, '
                                 f'serverNames {expect.get("server_names")}) — её правили не мы, не трогаю'}
        res = io.panel_set(expect, target['panel'], bool(opts.xray_restart))
        if res.get('ok') and not same_panel(res.get('after') or {}, target['panel']):
            after = res.get('after') or {}
            res = dict(res, ok=False, error='после update панель отдаёт другое: '
                       f'{after.get("dest_fields")} / {after.get("server_names")}')
        return res
    if step == 'haproxy':
        if cur.get('haproxy') is None:
            return {'ok': False, 'error': 'текущий acl не прочитан'}
        return io.haproxy_set(cur['haproxy']['acl_lines'], target['haproxy']['acl_lines'])
    if step == 'env':
        if cur.get('env') is None:
            return {'ok': False, 'error': 'текущий .env не прочитан'}
        return io.env_set(cur['env']['sni'], tsni)
    if step == 'bot':
        return io.bot_recreate(tsni)
    if step == 'probe':
        return io.probe_regen(tsni)
    raise ValueError(step)


def _advance(cur: dict, step: str, target: dict, res: dict) -> None:
    if step == 'panel':
        cur['panel'] = copy.deepcopy(res.get('after') or target['panel'])
    elif step == 'haproxy':
        cur['haproxy'] = {'acl_lines': [list(x) for x in target['haproxy']['acl_lines']]}
    elif step == 'env':
        cur['env'] = {'sni': target['env']['sni']}
    elif step == 'bot':
        cur['bot'] = {'container_sni': target['env']['sni']}
    elif step == 'probe':
        cur['probe'] = {'present': True, 'server_name': target['env']['sni']}


def step_detail(step: str, res: dict) -> str:
    if not res.get('ok'):
        msg = str(res.get('error') or 'ошибка без описания')
        if res.get('conflict'):
            msg += ' (ничего не записано)'
        if res.get('restored'):
            msg += f'; файл возвращён из бэкапа, haproxy {res.get("active_after_restore")}'
        if res.get('log'):
            msg += f' [лог панели: {"; ".join(res["log"][-2:])}]'
        return msg
    if res.get('noop'):
        return 'уже на месте'
    if step == 'panel':
        a = res.get('after') or {}
        restarted = res.get('restarted')
        xr = '' if restarted is None else (', xray перезапущен' if restarted else ', рестарт xray НЕ подтвердился')
        return (f'dest {", ".join(map(str, (a.get("dest_fields") or {}).values()))}, '
                f'serverNames [{_names(a.get("server_names"))}]{xr}')
    if step == 'haproxy':
        return ('acl: ' + ' | '.join(_names(x) for x in res.get('acl_lines') or [])
                + (f'; бэкап {res["backup"]}' if res.get('backup') else ''))
    if step == 'env':
        return f'SNI_VALUE={res.get("value")}' + (f'; бэкап {res["backup"]}' if res.get('backup') else '')
    if step == 'bot':
        return f'/health {res.get("health")}, SNI_VALUE в контейнере {res.get("container_sni")}'
    if step == 'probe':
        return (f'reality server_name {res.get("server_name")}'
                + (f'; бэкап {res["backup"]}' if res.get('backup') else ''))
    return 'ok'


def converge(io, cur: dict, target: dict, order, opts, progress: Callable[[str], None], only=None):
    """Move every layer from ``cur`` to ``target`` in ``order`` (restricted
    to the steps in ``only`` when given). Stops at the first failed HARD
    step; a failed SOFT step is logged and the run goes on (in rollback
    order probe-proxy sits mid-way, and HAProxy/panel must still move).
    Returns (log, failed_hard_step, its_result, soft_failures)."""
    cur = copy.deepcopy(cur)
    log, soft = [], []
    for step in steps_needed(cur, target, order):
        if only is not None and step not in only:
            continue
        progress(step)
        res = do_step(io, step, cur, target, opts)
        log.append({'step': step, 'ok': bool(res.get('ok')), 'detail': step_detail(step, res),
                    'backup': res.get('backup'), 'restarted': res.get('restarted'),
                    'conflict': bool(res.get('conflict')), 'at': now_iso()})
        if not res.get('ok'):
            if step in SOFT_STEPS:
                soft.append(step)
                continue
            return log, step, res, soft
        _advance(cur, step, target, res)
    return log, None, None, soft


def collect_state(io) -> dict:
    entry = io.read_entry()
    panel = io.read_panel()
    state = {'panel': panel if panel.get('ok') else {'ok': False, 'error': panel.get('error')}}
    for k in ('haproxy', 'env', 'bot', 'probe'):
        part = entry.get(k) if entry.get('ok') else None
        state[k] = part if isinstance(part, dict) else {'ok': False, 'error': entry.get('error') or 'нет данных'}
    return state


def run_check(io, opts, target_override: Optional[dict] = None) -> Plan:
    state = collect_state(io)
    host, port = split_dest(opts.dest_resolved)
    jobs = [{'tag': 'candidate', 'host': host, 'port': port, 'sni': opts.sni, 'samples': opts.cert_samples}]
    panel = (state['panel'].get('inbound') or {}) if state['panel'].get('ok') else {}
    cur_dests = sorted(set(map(str, (panel.get('dest_fields') or {}).values())))
    if cur_dests and cur_dests[0] != opts.dest_resolved:
        try:
            ch, cp = split_dest(cur_dests[0])
            names = panel.get('server_names') or [ch]
            jobs.append({'tag': 'current', 'host': ch, 'port': cp, 'sni': str(names[0]).lower(), 'samples': 1})
        except ValueError:
            pass
    old_names = [n for n in lower_names(panel.get('server_names')) if n != opts.sni]
    if opts.keep_old_sni and old_names:
        jobs.append({'tag': 'compat', 'host': host, 'port': port, 'sni': old_names[0], 'samples': 1})
    tls = io.probe_exit(jobs, None)
    return build_plan(state, tls, opts, target_override)


def run_verify(io, target: dict, opts, strict_xray: bool = False) -> Tuple[int, list]:
    state = collect_state(io)
    tag = (state['panel'].get('inbound') or {}).get('tag') if state['panel'].get('ok') else None
    xray = io.probe_exit([], tag) if tag else {'ok': False, 'error': 'тег inbound неизвестен (панель не прочитана)'}
    tls = io.tls_entry(target['sni']) if target.get('sni') else {'ok': False}
    checks = verify_checks(state, xray, tls, target, strict_xray)
    return verify_code(checks), checks


def revert_to(io, snap: dict, attempted, opts, progress):
    """Auto-revert after a failed apply step — only the layers this run may
    have written (``attempted``: steps that ran and did not stop at the
    conflict guard, which refuses BEFORE writing), and never a layer that
    now holds neither the old nor the new value (someone else changed it).
    Fresh read of entry (the bot may be down now); the panel is read lazily
    right before its step — after the bot was recreated, since panel calls
    go through it."""
    only = set(attempted)
    if only & {'env', 'bot', 'probe'}:
        only |= {'env', 'bot', 'probe'}
    if not only:
        return [], None, None, []
    entry = io.read_entry()
    if not entry.get('ok'):
        return [], 'read', {'ok': False, 'error': f'entry не читается: {entry.get("error")}'}, []
    state = {'panel': {'ok': False}}
    for k in ('haproxy', 'env', 'bot', 'probe'):
        state[k] = entry.get(k) or {'ok': False}
    cur = values_from_state(state)
    cur['_guard'] = {'panel': snap['new']['panel']}
    old, new = snap['old'], snap['new']
    if 'haproxy' in only and cur['haproxy'] is not None and not (
            acl_equal(cur['haproxy']['acl_lines'], old['haproxy']['acl_lines'])
            or acl_equal(cur['haproxy']['acl_lines'], new['haproxy']['acl_lines'])):
        return [], 'haproxy', {'ok': False, 'conflict': True,
                               'error': 'acl ни на старых, ни на новых значениях — его правили не мы, не трогаю'}, []
    if 'env' in only and cur['env'] is not None and cur['env']['sni'] not in (old['env']['sni'], new['env']['sni']):
        return [], 'env', {'ok': False, 'conflict': True,
                           'error': f'SNI_VALUE={cur["env"]["sni"]!r} — ни старое, ни новое значение, не трогаю'}, []
    return converge(io, cur, old, ROLLBACK_ORDER, opts, progress, only=only)


def _render_steps(title: str, log: List[dict]) -> List[str]:
    if not log:
        return []
    out = [title]
    for e in log:
        out.append(f'  {"✓" if e["ok"] else "✗"} {STEP_TITLES[e["step"]]} — {e["detail"]}')
    return out


def _progress(err):
    def say(step):
        print(f'→ {STEP_TITLES.get(step, step)}…', file=err, flush=True)
    return say


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ''


def cmd_check(io, store, opts, out) -> int:
    """Read-only. Says exactly what --apply with the same flags would do —
    including resuming an unfinished rotation or refusing a different one."""
    override, note, blocker = None, None, None
    try:
        snap = store.load()
    except SnapshotError as e:
        snap, note = None, f'Снимок не читается ({e}) — --apply потребует --force-snapshot'
    if snap and snap.get('status') in UNFINISHED and not opts.force_snapshot:
        if same_options(snap, opts):
            override = snap['new']
            note = (f'Незавершённая ротация на {snap["options"]["sni"]} (статус {snap["status"]}, '
                    f'{snap["created_at"]}): --apply продолжит её по снимку, --rollback откатит')
        else:
            blocker = (f'незавершённая ротация на {snap["options"]["sni"]} (статус {snap["status"]}) — '
                       '--apply на другой SNI откажется: сначала --rollback (или --force-snapshot, '
                       'если состояние проверено руками)')
    plan = run_check(io, opts, override)
    if blocker and not plan.blind:
        plan.errors.insert(0, blocker)
    print(render_plan(plan, opts, note=note, hint=True), file=out)
    return plan.exit_code


def cmd_apply(io, store, opts, confirm, out, err) -> int:
    try:
        existing = store.load()
    except SnapshotError as e:
        if not opts.force_snapshot:
            print(f'ИТОГ: НЕ СМОГ ПОСМОТРЕТЬ — снимок повреждён: {e}; --force-snapshot отложит его в сторону',
                  file=out)
            return EXIT_BLIND
        store.archive()
        existing = None
    override, resume = None, False
    if existing and existing.get('status') in UNFINISHED and not opts.force_snapshot:
        if not same_options(existing, opts):
            print(f'ИТОГ: ПРОВЕРКА НЕ ПРОШЛА — незавершённая ротация на {existing["options"]["sni"]} '
                  f'(статус {existing["status"]}) в {store.path}: сначала --rollback '
                  '(или --force-snapshot, если состояние проверено руками)', file=out)
            return EXIT_FAIL
        override, resume = existing['new'], True
    plan = run_check(io, opts, override)
    note = (f'Продолжаю незавершённую ротацию от {existing["created_at"]} (статус {existing["status"]}): '
            'цель и значения для отката — из снимка') if resume else None
    print(render_plan(plan, opts, note=note, hint=False, head='ПЛАН'), file=out)
    if plan.exit_code != EXIT_OK:
        print('Не применяю: проверка не прошла.', file=out)
        return plan.exit_code
    if not plan.steps:
        if resume:
            existing['status'] = 'applied'
            store.save(existing)
        print('Применять нечего.', file=out)
        return EXIT_OK
    if (confirm('Применить план? Введите yes: ') or '').strip() != 'yes':
        print('ИТОГ: отменено оператором — ничего не изменено', file=out)
        return EXIT_FAIL

    if resume:
        snap = existing
    else:
        if existing:
            store.archive()
        snap = new_snapshot(plan, opts)
    snap['status'] = 'pending'
    snap.setdefault('runs', []).append({'at': now_iso(), 'mode': 'apply', 'steps': list(plan.steps)})
    store.save(snap)                      # BEFORE the first change — rollback depends on it

    progress = _progress(err)
    try:
        log, failed, fres, soft = converge(io, plan.old, plan.new, APPLY_ORDER, opts, progress)
    except KeyboardInterrupt:
        store.save(snap)
        print(f'ИТОГ: ПРЕРВАНО — слои могут быть рассинхронизированы; смотри --verify --sni {opts.sni}, '
              f'откат: --rollback (снимок {store.path})', file=out)
        return EXIT_FAIL
    except Exception as e:                # noqa: BLE001 - a bug must not leave a silent "pending"
        snap['status'] = 'failed'
        snap['error'] = f'{type(e).__name__}: {e}'[:300]
        store.save(snap)
        print(f'ИТОГ: СБОЙ СКРИПТА — {type(e).__name__}: {e}; слои могут быть рассинхронизированы: '
              '--verify, затем --rollback', file=out)
        return EXIT_FAIL
    snap['steps'].extend(log)
    for e in log:
        if e.get('backup'):
            snap['backups'][e['step']] = e['backup']
    revert_log, rfailed, rres = [], None, None
    if failed is None:
        status = 'partial' if soft else 'applied'
    elif opts.auto_revert:
        print('→ откатываю уже сделанное…', file=err, flush=True)
        attempted = [e['step'] for e in log if e['ok'] or not e.get('conflict')]
        try:
            revert_log, rfailed, rres, _soft = revert_to(io, snap, attempted, opts, progress)
        except Exception as e:            # noqa: BLE001
            rfailed, rres = 'revert', {'ok': False, 'error': f'{type(e).__name__}: {e}'}
        status = 'reverted' if rfailed is None else 'failed'
        snap['revert'] = revert_log
    else:
        status = 'failed'
    snap['status'] = status
    store.save(snap)

    checks, vcode = [], None
    if status in ('applied', 'partial'):
        restarted = any(e.get('restarted') for e in log if e['step'] == 'panel')
        vtarget = target_from_values(snap['new'])
        vtarget['clients_before'] = (plan.old.get('panel') or {}).get('clients')
        vcode, checks = run_verify(io, vtarget, opts, strict_xray=restarted)
    elif status == 'reverted':
        vcode, checks = run_verify(io, target_from_values(snap['old']), opts)

    sni = snap['options']['sni']
    if status == 'applied' and vcode == EXIT_OK:
        head = f'ИТОГ: ротация на {sni} применена и проверена — verify OK'
    elif status == 'applied':
        word = 'НЕ ПРОШЁЛ' if vcode == EXIT_FAIL else 'НЕ СМОГ ПОСМОТРЕТЬ'
        head = (f'ИТОГ: ротация на {sni} применена, но verify {word}: '
                + ', '.join(c[1] for c in checks if c[0] in ('FAIL', 'BLIND')))
    elif status == 'partial':
        head = (f'ИТОГ: ротация на {sni} применена, но probe-proxy не обновлён — повтори ту же команду с --apply '
                '(продолжит по снимку); иначе пробы reality погаснут и DPIMonitor понизит Reality')
    elif status == 'reverted':
        head = (f'ИТОГ: ротация НЕ применена — шаг «{STEP_TITLES[failed]}» упал: {step_detail(failed, fres)}; '
                f'уже сделанное откачено (verify старого состояния: {"OK" if vcode == EXIT_OK else "смотри ниже"})')
    elif opts.auto_revert:
        head = (f'ИТОГ: ротация НЕ применена — шаг «{STEP_TITLES[failed]}» упал: {step_detail(failed, fres)}; '
                f'авто-откат ТОЖЕ упал на «{STEP_TITLES.get(rfailed, rfailed)}»: {(rres or {}).get("error")} — '
                'слои рассинхронизированы: исправь причину и --rollback')
    else:
        head = (f'ИТОГ: ротация НЕ применена — шаг «{STEP_TITLES[failed]}» упал: {step_detail(failed, fres)}; '
                'авто-откат выключен — слои рассинхронизированы: --rollback')
    lines = [head]
    lines += _render_steps('Шаги:', log)
    lines += _render_steps('Откат:', revert_log)
    if checks:
        lines.append('Проверка:')
        lines += [f'  [{st}] {label}: {detail}' for st, label, detail in checks]
    lines.append(f'Снимок: {store.path} (статус {status}) · откат: --rollback')
    if status in ('applied', 'partial'):
        baks = ', '.join(f'{k}: {v}' for k, v in sorted(snap['backups'].items()))
        lines.append('Дальше: клиенты получат новый SNI при обновлении подписки (vless://-ссылки — только '
                     'переизданием); пробы reality — живые в /protocols в течение 15 мин'
                     + (f'; бэкапы на entry: {baks}' if baks else ''))
    print('\n'.join(lines), file=out)
    if status == 'applied':
        return vcode
    return EXIT_FAIL


def cmd_rollback(io, store, opts, confirm, out, err) -> int:
    try:
        snap = store.load()
    except SnapshotError as e:
        print(f'ИТОГ: НЕ СМОГ ПОСМОТРЕТЬ — {e}', file=out)
        return EXIT_BLIND
    if not snap:
        print(f'ИТОГ: НЕ СМОГ ПОСМОТРЕТЬ — снимка нет ({store.path}): откатывать не к чему; '
              'вернуть прежний SNI можно обычной ротацией: --sni <старый> --apply', file=out)
        return EXIT_BLIND
    state = collect_state(io)
    blind = blind_layers(state)
    if blind:
        print(f'ИТОГ: НЕ СМОГ ПОСМОТРЕТЬ — {blind[0]}; откат не начат', file=out)
        print('\n'.join(f'  ? {b}' for b in blind), file=out)
        return EXIT_BLIND
    cur = values_from_state(state)
    old, new = snap['old'], snap['new']
    conflicts = rollback_conflicts(cur, old, new)
    steps = steps_needed(cur, old, ROLLBACK_ORDER)
    o = snap['options']
    head_note = f'Снимок {snap["created_at"]} (статус {snap["status"]}): ротация {old["env"]["sni"]} → {o["sni"]}'
    if conflicts:
        print('ИТОГ: ПРОВЕРКА НЕ ПРОШЛА — слой изменён после ротации, откат его затрёт: ' + conflicts[0], file=out)
        print(head_note, file=out)
        print('\n'.join(f'  ✗ {c}' for c in conflicts), file=out)
        print('Ничего не изменено. Вернуть прежний SNI можно обычной ротацией: '
              f'--sni {old["env"]["sni"]} --apply --force-snapshot', file=out)
        return EXIT_FAIL
    if not steps:
        if snap['status'] not in ('rolled_back', 'reverted'):
            snap['status'] = 'rolled_back'
            store.save(snap)
        vtarget = target_from_values(old)
        vtarget['clients_before'] = None
        vcode, checks = run_verify(io, vtarget, opts)
        print(render_verify(vcode, checks, vtarget,
                            note=f'{head_note} — всё уже на значениях до ротации, откатывать нечего'), file=out)
        return vcode
    diff = diff_values(cur, old, opts.acl)
    print(f'ПЛАН ОТКАТА: вернуть {old["env"]["sni"]} (шагов: {len(steps)})', file=out)
    print(head_note, file=out)
    print('План отката (в этом порядке): ' + ' → '.join(STEP_TITLES[s] for s in steps), file=out)
    print('\n'.join(f'  {layer}: {fld}: {a} → {b}' for layer, fld, a, b in diff), file=out)
    if (confirm('Откатить? Введите yes: ') or '').strip() != 'yes':
        print('ИТОГ: отменено оператором — ничего не изменено', file=out)
        return EXIT_FAIL
    snap['status'] = 'rolling_back'
    snap.setdefault('runs', []).append({'at': now_iso(), 'mode': 'rollback', 'steps': list(steps)})
    store.save(snap)
    try:
        log, failed, fres, soft = converge(io, cur, old, ROLLBACK_ORDER, opts, _progress(err))
    except KeyboardInterrupt:
        store.save(snap)
        print('ИТОГ: ПРЕРВАНО — слои могут быть рассинхронизированы; повтори --rollback', file=out)
        return EXIT_FAIL
    snap['rollback'] = log
    snap['status'] = 'rolled_back' if failed is None else 'failed'
    store.save(snap)
    restarted = any(e.get('restarted') for e in log if e['step'] == 'panel')
    vtarget = target_from_values(old)
    vtarget['clients_before'] = None
    vcode, checks = run_verify(io, vtarget, opts, strict_xray=restarted)
    if failed is not None:
        head = (f'ИТОГ: откат НЕ завершён — шаг «{STEP_TITLES[failed]}» упал: {step_detail(failed, fres)}; '
                'исправь причину и повтори --rollback')
    elif soft:
        head = 'ИТОГ: откат выполнен, но probe-proxy не перегенерирован — повтори --rollback'
    elif vcode == EXIT_OK:
        head = f'ИТОГ: откат на {old["env"]["sni"]} выполнен и проверен — verify OK'
    else:
        head = (f'ИТОГ: откат на {old["env"]["sni"]} выполнен, но verify: '
                + ', '.join(c[1] for c in checks if c[0] in ('FAIL', 'BLIND')))
    lines = [head] + _render_steps('Шаги:', log)
    lines.append('Проверка:')
    lines += [f'  [{st}] {label}: {detail}' for st, label, detail in checks]
    lines.append(f'Снимок: {store.path} (статус {snap["status"]})')
    if failed is None:
        lines.append(f'Дальше: клиенты, успевшие получить {o["sni"]}, вернутся на {old["env"]["sni"]} '
                     'при следующем обновлении подписки')
    print('\n'.join(lines), file=out)
    if failed is not None or soft:
        return EXIT_FAIL
    return vcode


def cmd_verify(io, store, opts, out) -> int:
    note = None
    if opts.sni:
        target = target_from_args(opts)
    else:
        try:
            snap = store.load()
        except SnapshotError as e:
            print(f'ИТОГ: НЕ СМОГ ПОСМОТРЕТЬ — {e}', file=out)
            return EXIT_BLIND
        if not snap:
            print(f'ИТОГ: НЕ СМОГ ПОСМОТРЕТЬ — нечего проверять: нет --sni и нет снимка ({store.path})', file=out)
            return EXIT_BLIND
        values = snap['old'] if snap.get('status') in ('reverted', 'rolled_back') else snap['new']
        target = target_from_values(values)
        target['clients_before'] = None
        note = f'Цель из снимка (статус {snap.get("status")}): SNI {target["sni"]}'
    code, checks = run_verify(io, target, opts)
    print(render_verify(code, checks, target, note), file=out)
    return code


# ---------------------------------------------------------------------------
# I/O — real (ssh) and fake (fixture world)
# ---------------------------------------------------------------------------

HOST_PY = ['python3', '-']


def container_py(container: str) -> List[str]:
    return ['docker', 'exec', '-i', '-w', '/app', '-e', 'PYTHONPATH=/app', container, 'python3', '-']


def run_cmd(argv: List[str], stdin: Optional[str], timeout: int) -> Tuple[int, str, str]:
    """subprocess wrapper that never raises: (rc, stdout, stderr); rc -1 =
    timeout / binary missing."""
    try:
        p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return -1, '', f'timeout after {timeout}s: {" ".join(argv[:6])}'
    except (OSError, ValueError) as e:
        return -1, '', str(e)


def last_json(text: str) -> Optional[dict]:
    """The last stdout line that parses as a JSON object (MOTD/log noise
    before it is ignored)."""
    for line in reversed((text or '').strip().splitlines()):
        line = line.strip()
        if line.startswith('{'):
            try:
                data = json.loads(line)
            except ValueError:
                return None
            return data if isinstance(data, dict) else None
    return None


class RealIO:
    """Every host interaction: ``ssh <host> <cmd>`` with a self-contained
    python script on stdin. ``runner`` is injectable (tests run the very
    same scripts locally against temp files)."""

    def __init__(self, opts, runner: Optional[Callable] = None):
        self.o = opts
        self.run = runner or run_cmd
        self.stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')

    def _ssh(self, host: str, remote: List[str], script: str, timeout: int) -> dict:
        argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host] + list(remote)
        rc, so, se = self.run(argv, script, timeout)
        data = last_json(so)
        if data is None:
            why = (se or so or '').strip()[-300:]
            return {'ok': False, 'error': f'нет ответа от {host} (rc={rc}): {why or "пусто"}'}
        return data

    # --- reads -----------------------------------------------------------
    def read_entry(self) -> dict:
        params = {'haproxy_cfg': self.o.haproxy_cfg, 'acl': self.o.acl, 'env_file': self.o.env_file,
                  'bot_container': BOT_CONTAINER, 'probe_container': PROBE_CONTAINER,
                  'probe_cfg': self.o.probe_cfg, 'health_url': HEALTH_URL}
        script = remote_script(ENTRY_READ, params, (parse_acl_lines, _env_assignment, parse_env_value,
                                                    probe_reality_server_name))
        return self._ssh(self.o.entry, HOST_PY, script, 90)

    def read_panel(self) -> dict:
        script = remote_script(PANEL, {'op': 'read', 'inbound_id': self.o.inbound_id}, PANEL_FUNCS)
        return self._ssh(self.o.entry, container_py(BOT_CONTAINER), script, 90)

    def probe_exit(self, jobs: List[dict], xray_tag: Optional[str]) -> dict:
        params = {'jobs': jobs, 'timeout': TLS_TIMEOUT, 'xray_tag': xray_tag,
                  'xui_container': XUI_CONTAINER, 'xray_config': XRAY_CONFIG}
        n = sum(max(1, int(j.get('samples') or 1)) for j in jobs)
        res = self._ssh(self.o.exit_host, HOST_PY, remote_script(EXIT_PROBE, params, (xray_reality_for_tag,)),
                        40 + n * (TLS_TIMEOUT + 3))
        if res.get('ok') and jobs and not res.get('openssl'):
            return {'ok': False, 'error': 'openssl не найден на exit'}
        if res.get('ok') and xray_tag and res.get('xray') is None and res.get('xray_error'):
            res['error'] = res['xray_error']
        return res

    def tls_entry(self, sni: str) -> dict:
        addr, port = split_dest(self.o.tls_probe_addr) if self.o.tls_probe_addr else (None, None)
        params = {'env_file': self.o.env_file, 'sni': sni, 'addr': addr, 'port': port,
                  'bot_container': BOT_CONTAINER, 'timeout': TLS_TIMEOUT, 'attempts': 3}
        script = remote_script(ENTRY_TLS, params, (_env_assignment, parse_env_value))
        return self._ssh(self.o.entry, HOST_PY, script, 40 + 3 * (TLS_TIMEOUT + 8))

    # --- writes ----------------------------------------------------------
    def panel_set(self, expect: dict, target: dict, restart: bool) -> dict:
        params = {'op': 'update', 'inbound_id': expect.get('id') or self.o.inbound_id,
                  'expect': expect, 'target': target, 'restart': bool(restart)}
        return self._ssh(self.o.entry, container_py(BOT_CONTAINER), remote_script(PANEL, params, PANEL_FUNCS), 150)

    def haproxy_set(self, expect_lines: list, target_lines: list) -> dict:
        params = {'haproxy_cfg': self.o.haproxy_cfg, 'acl': self.o.acl, 'expect': expect_lines,
                  'target': target_lines, 'stamp': self.stamp}
        return self._ssh(self.o.entry, HOST_PY, remote_script(HAPROXY_WRITE, params, ACL_FUNCS), 180)

    def env_set(self, expect: Optional[str], value: Optional[str]) -> dict:
        params = {'env_file': self.o.env_file, 'key': 'SNI_VALUE', 'expect': expect, 'value': value,
                  'stamp': self.stamp}
        return self._ssh(self.o.entry, HOST_PY, remote_script(ENV_WRITE, params, ENV_FUNCS), 60)

    def bot_recreate(self, expect_sni: Optional[str]) -> dict:
        params = {'bot_dir': self.o.bot_dir, 'bot_container': BOT_CONTAINER, 'expect_sni': expect_sni,
                  'health_url': HEALTH_URL, 'wait': BOT_WAIT_S}
        return self._ssh(self.o.entry, HOST_PY, remote_script(BOT_RECREATE, params), BOT_WAIT_S + 360)

    def probe_regen(self, expect_sni: Optional[str]) -> dict:
        params = {'probe_cfg': self.o.probe_cfg, 'probe_mount': PROBE_MOUNT, 'probe_container': PROBE_CONTAINER,
                  'bot_container': BOT_CONTAINER, 'expect_sni': expect_sni, 'stamp': self.stamp}
        script = remote_script(PROBE_REGEN, params, (probe_reality_server_name,))
        return self._ssh(self.o.entry, HOST_PY, script, 240)


# The prod shape as of 2026-10 (task E10 / AGENTS.md §23): bing dest, the
# ACL line carrying "www.bing.com www.google.com", 81 Reality clients, bot
# on :8443 behind HAProxy. Cert sizes are the §23 measurements of 2026-07-20.
FAKE_WORLD = {
    'panel': {'id': 1, 'tag': 'inbound-443', 'protocol': 'vless', 'network': 'tcp', 'security': 'reality',
              'dest_fields': {'dest': 'www.bing.com:443'}, 'server_names': ['www.bing.com'],
              'settings_server_name': '', 'clients': 81, 'with_flow': 81},
    'xray': {'dest_fields': {'dest': 'www.bing.com:443'}, 'server_names': ['www.bing.com']},
    'haproxy': {'acl_lines': [['www.bing.com', 'www.google.com']], 'active': 'active'},
    'env': {'sni': 'www.bing.com', 'count': 1, 'entry_ip': '203.0.113.10', 'entry_port': '8443'},
    'bot': {'state': 'running', 'container_sni': 'www.bing.com', 'health': 'healthy', 'version': 'fake'},
    'probe': {'present': True, 'server_name': 'www.bing.com', 'container': 'running'},
    'certs': {'www.bing.com': 3920, 'www.google.com': 2520, 'www.cloudflare.com': 2521,
              'dl.google.com': 4874, 'www.microsoft.com': 8273},
}

FAKE_OPS = ('read_entry', 'read_panel', 'probe_exit', 'tls_entry',
            'panel_set', 'haproxy_set', 'env_set', 'bot_recreate', 'probe_regen')
WRITE_OPS = ('panel_set', 'haproxy_set', 'env_set', 'bot_recreate', 'probe_regen')


def fake_tls_lines(certs: Dict[str, int], host: str, sni: str) -> dict:
    """openssl-shaped lines for a fixture handshake (parse_tls_sample reads
    them exactly like the real ones)."""
    if host not in certs:
        return {'rc': 1, 'lines': ['40C7E0F1:error:10080002:BIO routines:BIO_lookup_ex:system lib: '
                                   f'getaddrinfo {host}: Name or service not known', 'connect:errno=0']}
    verify = '0 (ok)' if sni == host else '62 (hostname mismatch)'
    return {'rc': 0, 'lines': [f'<<< TLS 1.3, Handshake [length {certs[host]:04x}], Certificate',
                               'New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384',
                               'ALPN protocol: h2', f'Verify return code: {verify}']}


class FakeIO:
    """Fixture world with the RealIO interface. ``calls`` records every
    operation in order; ``fail`` makes the named operations fail the way
    the real ones do. ``state_path`` persists the world between runs
    (ROTATE_FAKE_STATE), so apply and rollback can be rehearsed apart."""

    def __init__(self, world: Optional[dict] = None, fail=(), state_path: Optional[str] = None):
        self.state_path = state_path
        if world is None and state_path and os.path.exists(state_path):
            with open(state_path, encoding='utf-8') as fh:
                world = json.load(fh)
        self.w = copy.deepcopy(world if world is not None else FAKE_WORLD)
        # {op: n} fails the first n calls of op; a plain collection fails every call.
        self.fail = dict(fail) if isinstance(fail, dict) else {op: -1 for op in fail}
        self.calls: List[Tuple[str, dict]] = []
        self.on_write: Optional[Callable[[str], None]] = None

    def _save(self):
        if self.state_path:
            with open(self.state_path, 'w', encoding='utf-8') as fh:
                json.dump(self.w, fh, indent=1)

    def _call(self, op: str, **kw) -> bool:
        """Record the call; True when the operation must fail."""
        self.calls.append((op, kw))
        if self.on_write and op in WRITE_OPS:
            self.on_write(op)
        left = self.fail.get(op, 0)
        if left > 0:
            self.fail[op] = left - 1
        return left != 0

    # --- reads -----------------------------------------------------------
    def read_entry(self) -> dict:
        if self._call('read_entry'):
            return {'ok': False, 'error': 'fake: ssh entry: Connection timed out'}
        w = self.w
        b = w['bot']
        running = b['state'] == 'running'
        return {
            'ok': True, 'euid': 0,
            'haproxy': {'ok': True, 'acl_lines': copy.deepcopy(w['haproxy']['acl_lines']),
                        'raw': ['acl is_reality_sni req_ssl_sni -i ' + ' '.join(x) for x in w['haproxy']['acl_lines']],
                        'errors': [], 'active': w['haproxy']['active'], 'writable': True, 'haproxy_bin': True},
            'env': {'ok': True, 'sni': w['env']['sni'], 'count': w['env']['count'],
                    'entry_ip': w['env']['entry_ip'], 'entry_port': w['env']['entry_port'], 'writable': True},
            'bot': {'ok': True, 'state': b['state'], 'container_sni': b['container_sni'] if running else None,
                    'health': b['health'] if running else 'down', 'version': b['version']},
            'probe': {'ok': True, 'present': w['probe']['present'],
                      'server_name': w['probe']['server_name'] if w['probe']['present'] else None,
                      'container': w['probe']['container']},
        }

    def read_panel(self) -> dict:
        if self._call('read_panel'):
            return {'ok': False, 'error': 'fake: XUI API login failed: HTTP 502'}
        if self.w['bot']['state'] != 'running':
            return {'ok': False, 'error': f'fake: Error response from daemon: container {BOT_CONTAINER} is not running'}
        return {'ok': True, 'inbound_id': self.w['panel']['id'], 'inbound': copy.deepcopy(self.w['panel'])}

    def probe_exit(self, jobs: List[dict], xray_tag: Optional[str]) -> dict:
        if self._call('probe_exit', jobs=copy.deepcopy(jobs), xray_tag=xray_tag):
            return {'ok': False, 'error': 'fake: ssh vpn-exit: Connection timed out'}
        out = {'ok': True, 'openssl': True, 'tls': {}, 'xray': None}
        for j in jobs:
            out['tls'][j['tag']] = [fake_tls_lines(self.w['certs'], j['host'], j['sni'])
                                    for _ in range(max(1, int(j.get('samples') or 1)))]
        if xray_tag:
            out['xray'] = copy.deepcopy(self.w['xray']) if xray_tag == self.w['panel']['tag'] else None
        return out

    def tls_entry(self, sni: str) -> dict:
        if self._call('tls_entry', sni=sni):
            return {'ok': False, 'error': 'fake: ssh entry: Connection timed out'}
        w = self.w
        routed = any(sni in lower_names(x) for x in w['haproxy']['acl_lines'])
        if routed:     # HAProxy -> exit -> Reality hands a non-Reality client to dest
            dest = str(next(iter(w['panel']['dest_fields'].values()), ''))
            sample = fake_tls_lines(w['certs'], dest.rpartition(':')[0], sni)
        else:          # HAProxy default backend: some other certificate
            sample = {'rc': 0, 'lines': ['<<< TLS 1.3, Handshake [length 0400], Certificate',
                                         'New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384',
                                         'No ALPN negotiated', 'Verify return code: 62 (hostname mismatch)']}
        return {'ok': True, 'addr': w['env']['entry_ip'], 'port': w['env']['entry_port'], 'sample': sample}

    # --- writes ----------------------------------------------------------
    def panel_set(self, expect: dict, target: dict, restart: bool) -> dict:
        if self._call('panel_set', expect=copy.deepcopy(expect), target=copy.deepcopy(target), restart=restart):
            return {'ok': False, 'updated': False, 'error': 'fake: update_inbound не подтвердился',
                    'log': ['API returned error: fake failure']}
        if self.w['bot']['state'] != 'running':
            return {'ok': False, 'error': f'fake: container {BOT_CONTAINER} is not running'}
        p = self.w['panel']
        if not same_panel(p, expect):
            return {'ok': False, 'conflict': True, 'error': 'панель изменилась после проверки'}
        p['dest_fields'] = dict(target['dest_fields'])
        p['server_names'] = list(target['server_names'])
        if target.get('settings_server_name') is not None:
            p['settings_server_name'] = target['settings_server_name']
        if restart:
            self.w['xray'] = {'dest_fields': dict(p['dest_fields']), 'server_names': list(p['server_names'])}
        self._save()
        return {'ok': True, 'updated': True, 'after': copy.deepcopy(p), 'restarted': bool(restart)}

    def haproxy_set(self, expect_lines: list, target_lines: list) -> dict:
        if self._call('haproxy_set', expect=copy.deepcopy(expect_lines), target=copy.deepcopy(target_lines)):
            return {'ok': False, 'stage': 'check',
                    'error': 'fake: haproxy -c: [ALERT] config : parsing [haproxy.cfg.rotate-new:41] : unknown keyword'}
        if not acl_equal(self.w['haproxy']['acl_lines'], expect_lines):
            return {'ok': False, 'conflict': True, 'error': 'acl изменился после проверки'}
        self.w['haproxy']['acl_lines'] = [list(x) for x in target_lines]
        self._save()
        return {'ok': True, 'changed': True, 'backup': '/etc/haproxy/haproxy.cfg.rotate-bak-fake',
                'acl_lines': [list(x) for x in target_lines], 'active': 'active'}

    def env_set(self, expect: Optional[str], value: Optional[str]) -> dict:
        if self._call('env_set', expect=expect, value=value):
            return {'ok': False, 'error': "fake: PermissionError: [Errno 13] Permission denied: '/opt/vpn-bot/.env'"}
        if self.w['env']['sni'] != expect:
            return {'ok': False, 'conflict': True, 'error': 'SNI_VALUE изменился после проверки'}
        self.w['env']['sni'] = value
        self._save()
        return {'ok': True, 'changed': True, 'backup': '/opt/vpn-bot/.env.rotate-bak-fake', 'value': value}

    def bot_recreate(self, expect_sni: Optional[str]) -> dict:
        b = self.w['bot']
        if self._call('bot_recreate', expect_sni=expect_sni):
            b.update(state='exited', container_sni=None, health='down')
            self._save()
            return {'ok': False, 'health': 'down', 'container_sni': None,
                    'error': f'fake: после пересоздания: health=down, SNI_VALUE в контейнере=None (ждали {expect_sni!r})'}
        b.update(state='running', container_sni=self.w['env']['sni'], health='healthy')
        self._save()
        ok = b['container_sni'] == expect_sni
        return {'ok': ok, 'health': 'healthy', 'container_sni': b['container_sni'], 'version': b['version'],
                'error': None if ok else f'SNI_VALUE в контейнере {b["container_sni"]!r}, ждали {expect_sni!r}'}

    def probe_regen(self, expect_sni: Optional[str]) -> dict:
        if self._call('probe_regen', expect_sni=expect_sni):
            return {'ok': False, 'error': 'fake: sing-box check: decode config: outbounds[0].tls: unknown field'}
        if self.w['bot']['state'] != 'running':
            return {'ok': False, 'error': f'fake: container {BOT_CONTAINER} is not running'}
        self.w['probe']['server_name'] = self.w['bot']['container_sni']
        self._save()
        ok = self.w['probe']['server_name'] == expect_sni
        return {'ok': ok, 'server_name': self.w['probe']['server_name'],
                'backup': '/opt/vpn-bot/probe-proxy/config.json.rotate-bak-fake',
                'error': None if ok else 'сгенерированный конфиг не на том SNI'}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

EPILOG = """\
режимы:
  (без флагов)  замер кандидата с exit + план с диффом; НИЧЕГО не меняет
  --apply       применить (после проверки и ввода yes); снимок пишется ДО изменений
  --verify      проверить слои (цель: --sni, иначе из снимка)
  --rollback    вернуть значения из снимка (после ввода yes)

коды выхода: 0 ок · 1 проверка не прошла · 2 не смог посмотреть

сухой прогон без прода (фикстуры, без ssh):
  ROTATE_FAKE=1 %(prog)s --sni www.google.com
  ROTATE_FAKE=1 ROTATE_FAKE_STATE=/tmp/rotate_fake_world.json %(prog)s --sni www.google.com --apply
  ROTATE_FAKE=1 ROTATE_FAKE_STATE=/tmp/rotate_fake_world.json %(prog)s --rollback
  ROTATE_FAKE_FAIL=<op>[,<op>] — сломать операцию: """ + ', '.join(FAKE_OPS) + """

runbook: docs/runbooks/rotate_reality_dest.md · AGENTS.md §23
"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog='rotate_reality_dest.py',
        description='Ротация Reality dest/SNI одним прогоном: панель exit → HAProxy entry → '
                    '.env бота (+пересоздание) → probe-proxy. Без флагов — только проверка и план.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=EPILOG)
    ap.add_argument('--sni', help='новый SNI / serverName, например www.google.com')
    ap.add_argument('--dest', help='realitySettings.dest как host:port (по умолчанию <sni>:443)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='применить план (спросит yes)')
    mode.add_argument('--verify', action='store_true', help='только проверить слои')
    mode.add_argument('--rollback', action='store_true', help='откатить по снимку (спросит yes)')
    ap.add_argument('--keep-old-sni', action='store_true',
                    help='оставить текущие имена в serverNames и acl на переходный период')
    ap.add_argument('--entry', default=DEFAULT_ENTRY, help='ssh-хост entry (%(default)s)')
    ap.add_argument('--exit', dest='exit_host', default=DEFAULT_EXIT, help='ssh-хост exit (%(default)s)')
    ap.add_argument('--inbound-id', type=int, default=None,
                    help='id Reality-inbound в панели (по умолчанию INBOUND_ID бота, обычно 1)')
    ap.add_argument('--haproxy-cfg', default=HAPROXY_CFG, help='%(default)s')
    ap.add_argument('--acl', default=ACL_NAME, help='имя acl в haproxy (%(default)s)')
    ap.add_argument('--bot-dir', default=BOT_DIR, help='compose-каталог бота на entry (%(default)s)')
    ap.add_argument('--env-file', default=None, help='.env бота (по умолчанию <bot-dir>/.env)')
    ap.add_argument('--probe-cfg', default=None,
                    help='конфиг probe-proxy (по умолчанию <bot-dir>/probe-proxy/config.json)')
    ap.add_argument('--snapshot', default=None, help='файл снимка (по умолчанию scripts/.rotation_snapshot.json)')
    ap.add_argument('--cert-samples', type=int, default=CERT_SAMPLES, help='замеров кандидата (%(default)s)')
    ap.add_argument('--tls-probe-addr', default=None,
                    help='host:port для TLS-проверки через entry (по умолчанию ENTRY_NODE_IP:ENTRY_NODE_PORT из .env)')
    ap.add_argument('--no-xray-restart', dest='xray_restart', action='store_false',
                    help='не перезапускать xray через панель после update inbound')
    ap.add_argument('--no-auto-revert', dest='auto_revert', action='store_false',
                    help='при сбое шага НЕ откатывать уже сделанное')
    ap.add_argument('--force-snapshot', action='store_true',
                    help='отложить незавершённый/повреждённый снимок и начать новую ротацию')
    return ap


def parse_opts(ap: argparse.ArgumentParser, argv) -> argparse.Namespace:
    o = ap.parse_args(argv)
    try:
        if o.sni:
            o.sni = parse_sni(o.sni)
        o.dest_resolved = parse_dest(o.dest, o.sni) if o.sni else None
        if o.tls_probe_addr:
            o.tls_probe_addr = parse_dest(o.tls_probe_addr, None)
    except ValueError as e:
        ap.error(str(e))
    if o.rollback and (o.sni or o.dest or o.keep_old_sni):
        ap.error('--rollback берёт значения из снимка: --sni/--dest/--keep-old-sni не нужны')
    if not o.sni and not (o.verify or o.rollback):
        ap.error('нужен --sni (без него — только --verify по снимку или --rollback)')
    if o.dest and not o.sni:
        ap.error('--dest без --sni')
    if not 1 <= o.cert_samples <= 10:
        ap.error('--cert-samples: от 1 до 10')
    if o.inbound_id is not None and o.inbound_id < 1:
        ap.error('--inbound-id: положительное число')
    o.env_file = o.env_file or posixpath.join(o.bot_dir, '.env')
    o.probe_cfg = o.probe_cfg or posixpath.join(o.bot_dir, 'probe-proxy', 'config.json')
    for name in ('haproxy_cfg', 'bot_dir', 'env_file', 'probe_cfg'):
        if not PATH_RE.match(getattr(o, name)):
            ap.error(f'--{name.replace("_", "-")}: нужен абсолютный путь без пробелов и спецсимволов')
    if not ACL_RE.match(o.acl):
        ap.error('--acl: только буквы, цифры, _ . -')
    return o


def fake_enabled(env) -> bool:
    return env.get('ROTATE_FAKE', '') not in ('', '0')


def make_io(opts, env=None):
    env = os.environ if env is None else env
    if fake_enabled(env):
        fail = [x.strip() for x in (env.get('ROTATE_FAKE_FAIL') or '').split(',') if x.strip()]
        unknown = [x for x in fail if x not in FAKE_OPS]
        if unknown:
            raise SystemExit(f'ROTATE_FAKE_FAIL: неизвестные операции {unknown}; есть: {", ".join(FAKE_OPS)}')
        return FakeIO(fail=fail, state_path=env.get('ROTATE_FAKE_STATE') or None)
    return RealIO(opts)


def main(argv=None, *, io=None, confirm: Optional[Callable[[str], str]] = None,
         out=None, err=None, env=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    env = os.environ if env is None else env
    opts = parse_opts(build_parser(), argv)
    fake = fake_enabled(env)
    if io is None:
        io = make_io(opts, env)
        if fake:
            print('[ROTATE_FAKE] фикстурный мир — ssh не используется'
                  + (f', состояние: {io.state_path}' if io.state_path else ''), file=err)
    store = SnapshotStore(opts.snapshot or (FAKE_SNAPSHOT_PATH if fake else SNAPSHOT_PATH))
    confirm = confirm or _ask
    if opts.rollback:
        return cmd_rollback(io, store, opts, confirm, out, err)
    if opts.verify:
        return cmd_verify(io, store, opts, out)
    if opts.apply:
        return cmd_apply(io, store, opts, confirm, out, err)
    return cmd_check(io, store, opts, out)


if __name__ == '__main__':
    sys.exit(main())
