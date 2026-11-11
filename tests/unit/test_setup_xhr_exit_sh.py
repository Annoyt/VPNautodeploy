"""scripts/setup_xhr_exit.sh — the xhr server on exit (VLESS + XHTTP +
Reality, an xray of its own) and its 2-minute ``--sync`` against the bot's
``GET /api/xhr/users`` (bot/services/xhr_exit.py).

The script runs as root on exit against docker, systemd and the bot; here
it runs for real (bash, python3, flock, cmp, sort) with ``docker`` /
``curl`` / ``systemctl`` / ``ss`` / ``openssl`` / ``id`` / ``chown`` /
``ip`` replaced by fakes that keep their state in files. What is pinned:
the dest and image checks refuse before anything is written, the keys are
generated once and stay root-only, the config carries the PROXY-protocol
requirement and the egress guards, an unchanged list touches nothing, a
changed one is checked before the live config moves, a container that
does not come back is rolled back, a list that suddenly reads empty is not
applied by the timer, a bot that does not answer leaves the users as they
are, a bot that hands out other keys fails loudly, the token never reaches
argv, and the 3x-ui container is never touched. One test feeds the script
the body the real endpoint answers on a real sqlite.
"""

import asyncio
import fcntl
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'setup_xhr_exit.sh'
IMAGE = ('ghcr.io/xtls/xray-core:26.6.22'
         '@sha256:7ec1ff2ea903a1e0999ada29557eadec1447588a59fd3afae64b46f9ae4b7953')
PRIV = 'fake-private-key-' + 'x' * 26
PUB = 'fake-public-key-' + 'y' * 27
SID = '0123456789abcdef'
TOKEN = 'tok-123'

U1 = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
U2 = '0f9e8d7c-6b5a-4c3d-8e2f-1a0b9c8d7e6f'

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
st = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
with open(st / 'calls.log', 'a') as f:
    f.write(json.dumps(['docker'] + args) + '\n')

def flag(name):
    return (st / name).exists()

def text(name, default=''):
    p = st / name
    return p.read_text() if p.exists() else default

cmd = args[0]
if cmd == 'inspect':
    name = args[-1]
    if name != 'xhr-exit' or not flag('xhr_exists'):
        print(f'Error: No such object: {name}', file=sys.stderr)
        sys.exit(1)
    running = 'true' if flag('xhr_running') else 'false'
    fmt = args[args.index('-f') + 1]
    print(f'{running} {text("restarts", "0")}' if 'RestartCount' in fmt else running)
elif cmd == 'ps':
    print('3x-ui')
    if flag('xhr_exists'):
        print('xhr-exit')
elif cmd == 'run' and '--rm' in args:
    if 'version' in args:
        print(text('version', 'Xray 26.6.22 (Xray, Penetrates Everything.) Custom (go1.26.4 linux/amd64)'))
    elif 'x25519' in args:
        print('PrivateKey: ' + os.environ['FAKE_PRIV'])
        print('Password (PublicKey): ' + os.environ['FAKE_PUB'])
        print('Hash32: ignored')
    elif '-test' in args:
        if flag('test_fail'):
            print('Failed to start: main: failed to load config files', file=sys.stderr)
            sys.exit(23)
        host = args[args.index('-v') + 1].split(':')[0]
        json.load(open(host))
        print('Configuration OK.')
    else:
        sys.exit(f'fake docker: unexpected run {args}')
elif cmd == 'run' and '-d' in args:
    host = args[args.index('-v') + 1].split(':')[0]
    n = len(list(st.glob('started_*.json')))
    (st / f'started_{n}.json').write_text(Path(host).read_text())
    (st / f'started_{n}.args').write_text(json.dumps(args))
    (st / 'xhr_exists').touch()
    (st / 'restarts').write_text('0')
    fails = st / 'start_fails'
    if fails.exists() and int(fails.read_text()) > 0:
        fails.write_text(str(int(fails.read_text()) - 1))
        (st / 'xhr_running').unlink(missing_ok=True)
    else:
        (st / 'xhr_running').touch()
    print('0123abcd')
elif cmd == 'rm':
    (st / 'xhr_exists').unlink(missing_ok=True)
    (st / 'xhr_running').unlink(missing_ok=True)
elif cmd == 'logs':
    print('fake xhr-exit log line')
else:
    sys.exit(f'fake docker: unexpected {args}')
'''

FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
st = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
header = sys.stdin.read() if '@-' in args else ''
with open(st / 'calls.log', 'a') as f:
    f.write(json.dumps(['curl'] + args) + '\n')
(st / 'curl_header').write_text(header)
if (st / 'curl_fail').exists():
    sys.stdout.write('000')
    sys.exit(7)
code = (st / 'http_code').read_text().strip() if (st / 'http_code').exists() else '200'
out = args[args.index('-o') + 1]
Path(out).write_text((st / 'answer').read_text() if (st / 'answer').exists() else '')
sys.stdout.write(code)
'''

FAKE_SS = r'''#!/bin/sh
echo "ss $*" >> "$FAKE_STATE/calls.log"
if [ -e "$FAKE_STATE/xhr_running" ] || [ -e "$FAKE_STATE/port_taken" ]; then
  echo "LISTEN 0 4096 0.0.0.0:8446 0.0.0.0:*"
fi
'''

FAKE_OPENSSL = r'''#!/bin/sh
case "$1" in
  rand) echo 0123456789abcdef ;;
  s_client)
    cat >/dev/null
    [ -e "$FAKE_STATE/no_tls13" ] && exit 1
    echo "<<< TLS 1.3, Handshake [length $(cat "$FAKE_STATE/cert_hex")], Certificate"
    echo "---"
    echo "ALPN protocol: $(cat "$FAKE_STATE/alpn")" ;;
esac
'''

FAKE_SYSTEMCTL = r'''#!/bin/sh
echo "systemctl $*" >> "$FAKE_STATE/calls.log"
'''

FAKE_ID = '#!/bin/sh\necho 0\n'

FAKE_CHOWN = r'''#!/bin/sh
echo "chown $*" >> "$FAKE_STATE/calls.log"
if [ "$1" = root:root ] && [ -e "$FAKE_STATE/chown_root_fail" ]; then exit 1; fi
exit 0
'''

FAKE_IP = r'''#!/bin/sh
echo "1.1.1.1 via 198.51.100.1 dev eth0 src 198.51.100.7 uid 0"
'''

# Tripwires: nothing the script runs may reach the machine's power state.
# They come first in PATH, log the attempt and fail; the fixture fails the
# test (an earlier version of a test here sourced ";reboot" and rebooted the
# developer's laptop: a desktop session may reboot without sudo).
TRIPWIRES = ('reboot', 'shutdown', 'poweroff', 'halt', 'init', 'telinit')
FAKE_TRIPWIRE = r'''#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_STATE/tripwire.log"
exit 1
'''


def answer(users, *, pbk=PUB, sid=SID, sni='dl.google.com', path='/xhr', in_cascade=True):
    return json.dumps({'ok': True, 'in_cascade': in_cascade,
                       'server': {'sni': sni, 'pbk': pbk, 'sid': sid, 'path': path},
                       'users': [{'uuid': u} for u in users]})


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / 'bin'
        self.state = root / 'state'
        self.dir = root / 'etc-xhr-exit'
        self.env_file = root / 'xhr-exit.env'
        self.units = root / 'units'
        self.sbin = root / 'sbin'
        self.copy = self.sbin / 'xhr-exit-sync'
        for d in (self.bin, self.state, self.units, self.sbin):
            d.mkdir()
        for name, body in (('docker', FAKE_DOCKER), ('curl', FAKE_CURL), ('ss', FAKE_SS),
                           ('openssl', FAKE_OPENSSL), ('systemctl', FAKE_SYSTEMCTL),
                           ('id', FAKE_ID), ('chown', FAKE_CHOWN), ('ip', FAKE_IP),
                           *((t, FAKE_TRIPWIRE) for t in TRIPWIRES)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.touch('cert_hex', '12f2')    # 4850 bytes
        self.touch('alpn', 'h2')
        self.write_env()
        self.users([U1])

    def write_env(self, **over):
        values = dict(BOT_URL='https://dash.example.com:9443', DPI_REPORT_TOKEN=TOKEN,
                      XHR_SNI='dl.google.com', XHR_EXIT_PUBLIC_IP='198.51.100.4')
        values.update(over)
        self.env_file.write_text(''.join(f'{k}={v}\n' for k, v in values.items() if v is not None))

    def users(self, users, **kw):
        self.touch('answer', answer(users, **kw))

    def touch(self, name, text=None):
        p = self.state / name
        p.write_text(text) if text is not None else p.touch()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith('XHR_') or k in ('BOT_URL', 'DPI_REPORT_TOKEN'))}
        env.update(PATH=f'{self.bin}:{env["PATH"]}', FAKE_STATE=str(self.state),
                   FAKE_PRIV=PRIV, FAKE_PUB=PUB,
                   XHR_EXIT_DIR=str(self.dir), XHR_EXIT_ENV=str(self.env_file),
                   XHR_EXIT_UNIT_DIR=str(self.units), XHR_EXIT_BIN=str(self.copy),
                   XHR_EXIT_SETTLE_S='0', **extra)
        return env

    def run(self, *args, script=SCRIPT, **extra):
        return subprocess.run(['bash', str(script), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        p = self.state / 'calls.log'
        if not p.exists():
            return []
        return [json.loads(line) if line.startswith('[') else line.split()
                for line in p.read_text().splitlines()]

    def docker_calls(self, sub):
        return [c for c in self.calls() if c[:2] == ['docker', sub]]

    def reset_calls(self):
        (self.state / 'calls.log').unlink(missing_ok=True)

    def config(self, name='config.json'):
        return json.loads((self.dir / name).read_text())

    def starts(self):
        return sorted(self.state.glob('started_*.json'),
                      key=lambda p: int(p.stem.split('_')[1]))

    def start_args(self, n=-1):
        args = sorted(self.state.glob('started_*.args'), key=lambda p: int(p.stem.split('_')[1]))
        return json.loads(args[n].read_text())


@pytest.fixture
def env(tmp_path):
    for tool in ('flock', 'sort', 'cmp'):
        if shutil.which(tool) is None:
            pytest.skip(f'{tool} is not installed')
    e = Env(tmp_path)
    yield e
    tripped = e.state / 'tripwire.log'
    assert not tripped.exists(), f'the script ran {tripped.read_text()!r}'
    power = [c for c in e.calls() if c[:1] == ['systemctl']
             and set(c[1:]) & {'reboot', 'poweroff', 'halt', 'kexec', 'suspend', 'hibernate'}]
    assert not power, power


def _ids(cfg):
    return [c['id'] for c in cfg['inbounds'][0]['settings']['clients']]


@pytest.fixture
def set_up(env):
    r = env.run()
    assert r.returncode == 0, r.stderr
    env.reset_calls()
    return env


class TestSetup:

    def test_fresh(self, env):
        r = env.run()
        assert r.returncode == 0, r.stderr
        keys = (env.dir / 'keys.env').read_text()
        assert keys == f'PRIVATE_KEY={PRIV}\nPUBLIC_KEY={PUB}\nSHORT_ID={SID}\n'
        assert (env.dir / 'server.env').read_text() == (
            f'PORT=8446\nSNI=dl.google.com\nXPATH=/xhr\nIMAGE={IMAGE}\nMEMORY=128m\n')
        inbound = env.config()['inbounds'][0]
        assert (inbound['protocol'], inbound['port'], inbound['listen']) == ('vless', 8446, '0.0.0.0')
        assert inbound['settings'] == {'clients': [{'id': U1, 'email': _label(U1), 'flow': ''}],
                                       'decryption': 'none'}
        stream = inbound['streamSettings']
        assert (stream['network'], stream['security']) == ('xhttp', 'reality')
        assert stream['xhttpSettings'] == {'path': '/xhr', 'mode': 'auto'}
        assert stream['realitySettings'] == {
            'show': False, 'dest': 'dl.google.com:443', 'serverNames': ['dl.google.com'],
            'privateKey': PRIV, 'shortIds': [SID]}
        assert stream['sockopt'] == {'acceptProxyProtocol': True}
        assert (env.state / 'xhr_running').exists()
        assert f'ExecStart={env.copy} --sync' in (env.units / 'xhr-exit-sync.service').read_text()
        timer = (env.units / 'xhr-exit-sync.timer').read_text()
        assert 'OnUnitActiveSec=2min' in timer and 'WantedBy=timers.target' in timer
        assert ['systemctl', 'enable', '--now', 'xhr-exit-sync.timer'] in env.calls()
        assert '1 user(s) (+1 -0)' in r.stdout

    def test_prints_what_entry_needs(self, env):
        r = env.run()
        assert r.returncode == 0, r.stderr
        lines = [ln.strip() for ln in r.stdout.splitlines()]
        for line in ('acl is_xhr_sni req_ssl_sni -i dl.google.com',
                     'use_backend xhr_exit if is_xhr_sni',
                     'backend xhr_exit', 'mode tcp',
                     'server xhr_exit 198.51.100.4:8446 send-proxy-v2',
                     'XHR_SNI=dl.google.com', 'XHR_PATH=/xhr',
                     f'XHR_PUBLIC_KEY={PUB}', f'XHR_SID={SID}'):
            assert line in lines, line
        assert not any(ln.startswith('XHR_PORT=') for ln in lines)
        assert PRIV not in r.stdout + r.stderr

    def test_the_exit_address_from_the_route_when_unset(self, env):
        env.write_env(XHR_EXIT_PUBLIC_IP=None)
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert 'server xhr_exit 198.51.100.7:8446 send-proxy-v2' in r.stdout

    def test_container_runs_unprivileged_on_its_own_network(self, env):
        assert env.run().returncode == 0
        args = env.start_args()
        joined = ' '.join(args)
        for part in ('--user 65532:65532', '--read-only', '--cap-drop ALL',
                     '--security-opt no-new-privileges', '--memory 128m',
                     '--restart unless-stopped', '-p 8446:8446/tcp',
                     '--sysctl net.ipv6.conf.all.disable_ipv6=1',
                     '--log-driver json-file', '--log-opt max-size=10m',
                     f'-v {env.dir}/config.json:/etc/xray/config.json:ro'):
            assert part in joined, part
        assert '--network' not in args and '--privileged' not in args
        assert args[-4:] == [IMAGE, 'run', '-c', '/etc/xray/config.json']

    def test_never_touches_the_panel(self, env):
        assert env.run().returncode == 0
        assert env.run('--sync').returncode == 0
        assert env.run('--remove').returncode == 0
        for call in env.calls():
            assert not any('3x-ui' in str(a) or 'compose' in str(a) for a in call), call
        assert {c[2] for c in env.docker_calls('rm')} == {'-f'}
        assert {c[-1] for c in env.docker_calls('rm')} == {'xhr-exit'}

    def test_config_is_readable_by_the_image_user_only(self, set_up):
        assert set_up.dir.stat().st_mode & 0o777 == 0o700
        for name in ('keys.env', 'server.env'):
            assert (set_up.dir / name).stat().st_mode & 0o077 == 0, name
        assert (set_up.dir / 'config.json').stat().st_mode & 0o777 == 0o400
        set_up.users([U1, U2])
        assert set_up.run('--sync').returncode == 0
        assert ['chown', '65532:65532', f'{set_up.dir}/config.json.new'] in set_up.calls()
        assert not list(set_up.dir.glob('.answer.*'))

    def test_timer_runs_a_root_owned_copy(self, env):
        assert env.run().returncode == 0
        assert env.copy.read_bytes() == SCRIPT.read_bytes()
        assert env.copy.stat().st_mode & 0o777 == 0o755
        assert ['chown', 'root:root', f'{env.copy}.new'] in env.calls()
        assert str(SCRIPT) not in (env.units / 'xhr-exit-sync.service').read_text()
        assert not (env.sbin / 'xhr-exit-sync.new').exists()

    def test_setup_recreates_even_when_nothing_changed(self, set_up):
        # a re-run is how a new image or new flags reach the container
        assert set_up.run().returncode == 0
        assert len(set_up.starts()) == 2

    def test_setup_from_the_copy_itself(self, set_up):
        r = set_up.run(script=set_up.copy)
        assert r.returncode == 0, r.stderr
        assert f'ExecStart={set_up.copy} --sync' in (
            set_up.units / 'xhr-exit-sync.service').read_text()

    def test_copy_that_cannot_be_owned_by_root(self, env):
        env.touch('chown_root_fail')
        r = env.run()
        assert r.returncode != 0 and 'could not install' in r.stderr
        assert not env.copy.exists() and not (env.sbin / 'xhr-exit-sync.new').exists()
        assert not (env.units / 'xhr-exit-sync.service').exists()

    def test_rerun_keeps_the_keys_and_recreates(self, set_up):
        (set_up.dir / 'keys.env').write_text(
            f'PRIVATE_KEY=kept{"k" * 39}\nPUBLIC_KEY={PUB}\nSHORT_ID=feed\n')
        set_up.users([U1], sid='feed')
        assert set_up.run().returncode == 0
        assert not [c for c in set_up.docker_calls('run') if 'x25519' in c]
        reality = set_up.config()['inbounds'][0]['streamSettings']['realitySettings']
        assert (reality['privateKey'], reality['shortIds']) == ('kept' + 'k' * 39, ['feed'])
        assert len(set_up.starts()) == 2   # setup always recreates

    @pytest.mark.parametrize('version,ok', [
        ('Xray 26.6.22 (Xray, Penetrates Everything.)', True),
        ('Xray 25.12.8 (Xray, Penetrates Everything.)', True),
        ('Xray 26.7.10 (Xray, Penetrates Everything.)', True),
        ('Xray 26.7.11 (Xray, Penetrates Everything.)', False),
        ('Xray 26.10.1 (Xray, Penetrates Everything.)', False),
        ('Xray 27.1.0 (Xray, Penetrates Everything.)', False),
        ('something else', False),
    ])
    def test_xray_below_26_7_11(self, env, version, ok):
        env.touch('version', version)
        r = env.run()
        assert (r.returncode == 0) is ok, r.stderr
        if not ok:
            assert not (env.dir / 'keys.env').exists()
            assert not (env.dir / 'config.json').exists()

    @pytest.mark.parametrize('image', [
        'ghcr.io/xtls/xray-core:26.6.22',
        'ghcr.io/xtls/xray-core:latest',
        'ghcr.io/xtls/xray-core@sha256:7ec1ff2e',
    ])
    def test_image_pinned_by_digest(self, env, image):
        env.write_env(XHR_EXIT_IMAGE=image)
        r = env.run()
        assert r.returncode != 0 and 'pinned by digest' in r.stderr
        assert not env.docker_calls('run')

    def test_oversized_dest_certificate(self, env):
        env.touch('cert_hex', '2051')     # 8273 bytes — the 2026-07-20 outage
        r = env.run()
        assert r.returncode != 0 and '8273 bytes' in r.stderr
        assert not (env.dir / 'keys.env').exists() and not (env.dir / 'config.json').exists()

    def test_dest_at_the_limit(self, env):
        env.touch('cert_hex', '1f40')     # 8000 bytes
        assert env.run().returncode == 0

    def test_dest_without_h2(self, env):
        env.touch('alpn', 'http/1.1')
        r = env.run()
        assert r.returncode != 0 and 'h2' in r.stderr
        assert not (env.dir / 'config.json').exists()

    def test_dest_without_tls13(self, env):
        env.touch('no_tls13')
        r = env.run()
        assert r.returncode != 0 and 'no TLS 1.3' in r.stderr

    def test_port_taken_by_someone_else(self, env):
        env.touch('port_taken')
        r = env.run()
        assert r.returncode != 0 and 'already taken' in r.stderr
        assert not (env.dir / 'config.json').exists()

    @pytest.mark.parametrize('over,message', [
        (dict(XHR_SNI=None), 'XHR_SNI not set'),
        (dict(BOT_URL=None), 'BOT_URL not set'),
        (dict(DPI_REPORT_TOKEN=None), 'DPI_REPORT_TOKEN not set'),
        (dict(XHR_SNI='dl.google.com/x'), 'odd characters in XHR_SNI'),
        (dict(XHR_SNI='localhost'), 'not a host name'),
        (dict(XHR_EXIT_PORT='84x'), 'must be a number'),
        (dict(XHR_EXIT_PORT='70000'), 'out of range'),
        (dict(XHR_EXIT_PORT='0'), 'out of range'),
        (dict(XHR_PATH='xhr'), 'must start with /'),
        (dict(XHR_PATH='/xhr?a=1'), 'odd characters in XHR_PATH'),
        (dict(XHR_EXIT_MEMORY='lots'), 'XHR_EXIT_MEMORY like 128m'),
        (dict(DPI_REPORT_TOKEN='tok"en'), 'odd characters in DPI_REPORT_TOKEN'),
    ])
    def test_bad_settings(self, env, over, message):
        env.write_env(**over)
        r = env.run()
        assert r.returncode != 0 and message in r.stderr
        assert not (env.dir / 'config.json').exists()

    def test_missing_env_file(self, env):
        env.env_file.unlink()
        r = env.run()
        assert r.returncode != 0 and 'missing' in r.stderr

    def test_the_env_file_is_data_not_code(self, env):
        # Sourced, a value runs as a command (as root, on exit). The payload
        # here only touches a file in the test's own directory, on purpose:
        # never put a real command into such a test.
        marker = env.state / 'executed'
        env.write_env(XHR_SNI=f'dl.google.com;touch {marker}',
                      XHR_PATH=f'/xhr$(touch {marker})',
                      XHR_EXIT_MEMORY=f'128m`touch {marker}`')
        r = env.run()
        assert r.returncode != 0 and 'odd characters in XHR_SNI' in r.stderr
        assert not marker.exists()

    def test_quotes_and_export_in_the_env_file(self, env):
        env.env_file.write_text('# xhr-exit\nexport BOT_URL="https://dash.example.com:9443"\n'
                                f"DPI_REPORT_TOKEN='{TOKEN}'\n  XHR_SNI=ignored.example.com\n"
                                'XHR_SNI=dl.google.com\nXHR_EXIT_PUBLIC_IP=198.51.100.4')
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert 'server xhr_exit 198.51.100.4:8446 send-proxy-v2' in r.stdout
        assert (env.state / 'curl_header').read_text() == f'X-DPI-Token: {TOKEN}\n'
        curl = [c for c in env.calls() if c[0] == 'curl']
        assert curl[-1][-1] == 'https://dash.example.com:9443/api/xhr/users'
        assert env.config()['inbounds'][0]['streamSettings']['realitySettings'][
            'serverNames'] == ['dl.google.com']

    def test_first_setup_before_the_bot_knows_this_server(self, env):
        env.touch('http_code', '404')
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert _ids(env.config()) == [] and (env.state / 'xhr_running').exists()
        assert 'users unchanged until it does' in r.stdout
        assert f'XHR_PUBLIC_KEY={PUB}' in r.stdout

    def test_a_refused_token_still_sets_up_but_fails(self, env):
        env.touch('http_code', '403')
        r = env.run()
        assert r.returncode != 0 and 'DPI_REPORT_TOKEN' in r.stderr
        assert (env.state / 'xhr_running').exists()
        assert (env.units / 'xhr-exit-sync.timer').exists()

    def test_empty_list_is_applied_by_hand(self, set_up):
        set_up.users([], in_cascade=False)
        r = set_up.run()
        assert r.returncode == 0, r.stderr
        assert _ids(set_up.config()) == []
        assert 'switched off in its cascade' in r.stdout

    def test_usage(self, env):
        r = env.run('--bogus')
        assert r.returncode != 0 and 'usage' in r.stderr


class TestRender:

    def test_egress_guards(self, set_up):
        cfg = set_up.config()
        rules = cfg['routing']['rules']
        assert cfg['routing']['domainStrategy'] == 'IPIfNonMatch'
        private = next(r for r in rules if 'ip' in r)
        assert private['outboundTag'] == 'block'
        # the SSH exit's list, and loopback/private v6
        for net in ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
                    '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16', '224.0.0.0/4',
                    '240.0.0.0/4', '::1/128', 'fc00::/7', 'fe80::/10'):
            assert net in private['ip'], net
        assert {'type': 'field', 'outboundTag': 'block', 'protocol': ['bittorrent']} in rules
        assert {'type': 'field', 'outboundTag': 'block', 'port': '25'} in rules
        assert cfg['outbounds'] == [
            {'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
            {'tag': 'block', 'protocol': 'blackhole'}]
        sniff = cfg['inbounds'][0]['sniffing']
        assert sniff['enabled'] is True and sniff['routeOnly'] is True
        assert cfg['log'] == {'loglevel': 'warning', 'access': 'none'}

    def test_the_label_is_the_ssh_exits_name(self, set_up):
        from bot.services.ssh_exit import username_for
        clients = set_up.config()['inbounds'][0]['settings']['clients']
        assert clients == [{'id': U1, 'email': username_for(U1), 'flow': ''}]

    def test_deterministic_and_deduplicated(self, set_up):
        set_up.users([U2, U1, U2.upper()])
        assert set_up.run('--sync').returncode == 0
        first = (set_up.dir / 'config.json').read_bytes()
        assert _ids(set_up.config()) == sorted([U1, U2])
        set_up.users([U1, U2])
        set_up.reset_calls()
        r = set_up.run('--sync')
        assert r.returncode == 0 and r.stdout == ''
        assert (set_up.dir / 'config.json').read_bytes() == first
        assert not set_up.docker_calls('run')

    def test_malformed_ids_are_skipped(self, set_up):
        body = json.loads(answer([]))
        body['users'] = [{'uuid': U1}, {'uuid': 'not-a-uuid'}, {'uuid': U2 + 'x'}, {'id': U1},
                         'junk', {'uuid': 42}, {'uuid': U2}]
        (set_up.state / 'answer').write_text(json.dumps(body))
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert _ids(set_up.config()) == sorted([U1, U2])

    def test_ids_are_lower_cased(self, set_up):
        set_up.users([U1, U2.upper()])
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert _ids(set_up.config()) == sorted([U1, U2])


class TestSync:

    def test_unchanged_touches_nothing(self, set_up):
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert r.stdout == '' and r.stderr == ''
        assert not set_up.docker_calls('run') and not set_up.docker_calls('rm')
        assert len(set_up.starts()) == 1

    def test_changed_list_is_checked_then_applied(self, set_up):
        set_up.users([U1, U2])
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        runs = set_up.docker_calls('run')
        assert '-test' in runs[0] and '-d' in runs[1]
        assert runs[0][runs[0].index('-v') + 1] == (
            f'{set_up.dir}/config.json.new:/etc/xray/config.json:ro')
        assert _ids(set_up.config()) == sorted([U1, U2])
        assert _ids(set_up.config('config.json.prev')) == [U1]
        assert _ids(json.loads(set_up.starts()[-1].read_text())) == sorted([U1, U2])
        assert '2 user(s) (+1 -0)' in r.stdout

    def test_changes_in_a_row(self, set_up):
        # config.json and config.json.prev are read-only (0400) once written
        set_up.users([U1, U2])
        assert set_up.run('--sync').returncode == 0
        set_up.users([U2])
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert _ids(set_up.config()) == [U2]
        assert _ids(set_up.config('config.json.prev')) == sorted([U1, U2])
        assert not list(set_up.dir.glob('*.tmp'))

    def test_a_run_killed_midway_leaves_nothing_in_the_way(self, set_up):
        new = set_up.dir / 'config.json.new'
        new.write_text('{"half": ')
        new.chmod(0o400)
        set_up.users([U1, U2])
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert _ids(set_up.config()) == sorted([U1, U2])
        assert not new.exists()

    def test_removal(self, set_up):
        set_up.users([U2])
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert _ids(set_up.config()) == [U2]
        assert '1 user(s) (+1 -1)' in r.stdout

    def test_stopped_container_comes_back(self, set_up):
        (set_up.state / 'xhr_running').unlink()
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert (set_up.state / 'xhr_running').exists()
        assert len(set_up.starts()) == 2

    def test_empty_list_is_not_applied(self, set_up):
        set_up.users([])
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'lists 0 users' in r.stderr
        assert _ids(set_up.config()) == [U1]
        assert not set_up.docker_calls('run')
        assert not (set_up.dir / 'config.json.new').exists()

    def test_empty_on_empty_is_quiet(self, env):
        env.touch('http_code', '404')
        assert env.run().returncode == 0
        env.touch('http_code', '200')
        env.users([], in_cascade=False)
        env.reset_calls()
        r = env.run('--sync')
        assert r.returncode == 0 and r.stdout == ''
        assert not env.docker_calls('run')

    def test_check_failure_leaves_the_live_config(self, set_up):
        set_up.users([U1, U2])
        set_up.touch('test_fail')
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'xray -test failed' in r.stderr
        assert 'failed to load config' in r.stderr
        assert _ids(set_up.config()) == [U1]
        assert len(set_up.starts()) == 1
        assert not (set_up.dir / 'config.json.new').exists()

    def test_container_that_does_not_come_back_is_rolled_back(self, set_up):
        set_up.users([U1, U2])
        set_up.touch('start_fails', '1')
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'rolled back' in r.stderr
        assert _ids(set_up.config()) == [U1]
        started = [_ids(json.loads(p.read_text())) for p in set_up.starts()]
        assert started[-2:] == [sorted([U1, U2]), [U1]]
        assert (set_up.state / 'xhr_running').exists()

    def test_a_crash_loop_is_not_up(self, set_up):
        # running, but xray died and docker brought it back: not up
        set_up.users([U1, U2])
        real = set_up.bin / 'docker'
        body = real.read_text().replace(
            "(st / 'restarts').write_text('0')",
            "(st / 'restarts').write_text('0' if (st / 'restarts_seen').exists() else '1');"
            " (st / 'restarts_seen').touch()")
        real.write_text(body)
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'rolled back' in r.stderr
        assert _ids(set_up.config()) == [U1]

    @pytest.mark.parametrize('trouble', ['curl_fail', '503', '502'])
    def test_a_bot_that_does_not_answer_keeps_the_users(self, set_up, trouble):
        set_up.users([U2])
        if trouble == 'curl_fail':
            set_up.touch('curl_fail')
        else:
            set_up.touch('http_code', trouble)
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert 'users unchanged' in r.stdout
        assert _ids(set_up.config()) == [U1]
        assert not set_up.docker_calls('run')

    def test_a_hand_edited_live_config_is_not_rendered_blindly(self, set_up):
        live = set_up.dir / 'config.json'
        cfg = json.loads(live.read_text())
        cfg['inbounds'][0]['settings']['clients'].append({'id': 'not-a-uuid'})
        live.chmod(0o600)
        live.write_text(json.dumps(cfg))
        set_up.touch('curl_fail')
        (set_up.state / 'xhr_running').unlink()
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'could not render' in r.stderr
        assert not set_up.docker_calls('run')

    def test_a_silent_bot_still_brings_the_container_back(self, set_up):
        set_up.touch('curl_fail')
        (set_up.state / 'xhr_running').unlink()
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert (set_up.state / 'xhr_running').exists()
        assert _ids(json.loads(set_up.starts()[-1].read_text())) == [U1]

    @pytest.mark.parametrize('code,message', [
        ('403', 'refused DPI_REPORT_TOKEN'),
        ('404', 'hands out no xhr'),
        ('401', 'HTTP 401'),
    ])
    def test_a_bot_that_refuses_fails_loudly(self, set_up, code, message):
        set_up.users([U2])
        set_up.touch('http_code', code)
        r = set_up.run('--sync')
        assert r.returncode != 0 and message in r.stderr
        assert _ids(set_up.config()) == [U1]
        assert (set_up.state / 'xhr_running').exists()

    def test_junk_answer_leaves_the_live_config(self, set_up):
        for junk in ('{"not": "a list"}', 'not json', '{"ok": false, "users": []}',
                     '{"ok": true, "users": {}}'):
            set_up.touch('answer', junk)
            r = set_up.run('--sync')
            assert r.returncode != 0 and 'not a user list' in r.stderr, junk
            assert _ids(set_up.config()) == [U1]
        assert len(set_up.starts()) == 1

    @pytest.mark.parametrize('field,value,env_name', [
        ('pbk', 'main-inbound-pbk', 'XHR_PUBLIC_KEY'),
        ('sid', 'aaaa', 'XHR_SID'),
        ('sni', 'www.bing.com', 'XHR_SNI'),
        ('path', '/other', 'XHR_PATH'),
    ])
    def test_other_keys_in_the_profiles_fail_loudly(self, set_up, field, value, env_name):
        own = {'pbk': PUB, 'sid': SID, 'sni': 'dl.google.com', 'path': '/xhr'}
        set_up.users([U1, U2], **{field: value})
        r = set_up.run('--sync')
        assert r.returncode != 0, r.stderr
        assert f'set {env_name}={own[field]} in entry' in r.stderr
        assert f'"{value}"' in r.stderr
        # the users are applied all the same: fixing entry's .env is enough
        assert _ids(set_up.config()) == sorted([U1, U2])

    def test_an_answer_without_the_server_keys(self, set_up):
        body = json.loads(answer([U1, U2]))
        del body['server']
        (set_up.state / 'answer').write_text(json.dumps(body))
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'carries no server keys' in r.stderr
        assert r.stderr.count('set XHR_') == 0
        assert _ids(set_up.config()) == sorted([U1, U2])

    def test_the_token_goes_through_stdin(self, set_up):
        assert set_up.run('--sync').returncode == 0
        curl = [c for c in set_up.calls() if c[0] == 'curl']
        assert curl and not any(TOKEN in a for c in curl for a in c)
        assert curl[-1][-1] == 'https://dash.example.com:9443/api/xhr/users'
        assert (set_up.state / 'curl_header').read_text() == f'X-DPI-Token: {TOKEN}\n'

    def test_not_set_up(self, env):
        r = env.run('--sync')
        assert r.returncode != 0 and 'not set up' in r.stderr
        assert not [c for c in env.calls() if c[0] == 'curl']

    def test_the_bot_url_is_data_too(self, set_up):
        marker = set_up.state / 'executed'
        set_up.write_env(BOT_URL=f'https://dash.example.com:9443/$(touch {marker})')
        set_up.run('--sync')
        assert not marker.exists()
        curl = [c for c in set_up.calls() if c[0] == 'curl']
        assert curl[-1][-1] == f'https://dash.example.com:9443/$(touch {marker})/api/xhr/users'

    @pytest.mark.parametrize('name,text,message', [
        ('server.env', f'PORT=8446\nSNI=dl.google.com\nXPATH=/xhr\n'
                       'IMAGE=ghcr.io/xtls/xray-core:latest\nMEMORY=128m\n', 'pinned by digest'),
        ('server.env', f'PORT=84x\nSNI=dl.google.com\nXPATH=/xhr\nIMAGE={IMAGE}\nMEMORY=128m\n',
         'must be a number'),
        ('keys.env', f'PRIVATE_KEY=short\nPUBLIC_KEY={PUB}\nSHORT_ID={SID}\n', 'damaged'),
        ('keys.env', f'PRIVATE_KEY={PRIV}\nPUBLIC_KEY={PUB}\nSHORT_ID=not-hex\n', 'damaged'),
    ])
    def test_a_damaged_state_file_is_refused(self, set_up, name, text, message):
        (set_up.dir / name).write_text(text)
        set_up.users([U1, U2])
        r = set_up.run('--sync')
        assert r.returncode != 0 and message in r.stderr
        assert _ids(set_up.config()) == [U1]
        assert not set_up.docker_calls('run')

    def test_sync_keeps_the_setup_parameters(self, set_up):
        # the timer renders what setup checked, not a later edit
        set_up.write_env(XHR_SNI='www.microsoft.com', XHR_EXIT_PORT='9999', XHR_PATH='/new')
        set_up.users([U1, U2])
        assert set_up.run('--sync').returncode == 0
        inbound = set_up.config()['inbounds'][0]
        assert inbound['port'] == 8446
        assert inbound['streamSettings']['realitySettings']['serverNames'] == ['dl.google.com']
        assert inbound['streamSettings']['xhttpSettings']['path'] == '/xhr'

    def test_a_running_sync_holds_the_lock(self, set_up):
        set_up.users([U1, U2])
        with open(set_up.dir / '.lock', 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            r = set_up.run('--sync')
        assert r.returncode == 0 and r.stdout == ''
        assert not [c for c in set_up.calls() if c[0] == 'curl']
        assert _ids(set_up.config()) == [U1]


class TestPrintAndRemove:

    def test_print_repeats_the_lines(self, set_up):
        r = set_up.run('--print')
        assert r.returncode == 0, r.stderr
        assert 'server xhr_exit 198.51.100.4:8446 send-proxy-v2' in r.stdout
        assert f'XHR_PUBLIC_KEY={PUB}' in r.stdout
        assert not set_up.docker_calls('run')

    def test_remove(self, set_up):
        r = set_up.run('--remove')
        assert r.returncode == 0, r.stderr
        assert ['systemctl', 'disable', '--now', 'xhr-exit-sync.timer'] in set_up.calls()
        assert not (set_up.units / 'xhr-exit-sync.service').exists()
        assert not (set_up.units / 'xhr-exit-sync.timer').exists()
        assert not set_up.copy.exists()
        assert not (set_up.state / 'xhr_exists').exists()
        assert (set_up.dir / 'keys.env').exists()
        assert 'switch xhr off in the cascade' in r.stdout


def _label(uuid):
    import hashlib
    return 'n' + hashlib.sha256(uuid.encode()).hexdigest()[:12]


# ------------------------------------------------- the bot's own answer ---

def _bot_config(**over):
    cfg = dict(
        BOT_TOKEN='t', WEBAPP_URL='https://dash.example.com', DPI_REPORT_TOKEN=TOKEN,
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='main-inbound-pbk', SNI_VALUE='www.bing.com', SID_VALUE='aaaa',
        HY2_HOST='', HY2_PORT=8400, HY2_SNI='', HY2_OBFS_PASSWORD='', HY2_HOP_PORTS='',
        HY2T_PORT='', WS_HOST='', WS_PORT=2053, WS_PATH='/api', WS_SNI='', WS2_HOST='',
        STLS_HOST='', STLS_PORT=443, STLS_SNI='', STLS_VERSION=3, STLS_PASSWORD='',
        SS_METHOD='2022-blake3-aes-128-gcm', SS_SERVER_PASSWORD='', SS_USER_SALT='salt',
        SUB_MIRROR_URLS='', FALLBACK_NODE_HOST='', TLS_FINGERPRINTS='',
        XHR_SNI='dl.google.com', XHR_PATH='/xhr', XHR_PUBLIC_KEY=PUB, XHR_SID=SID,
        XHR_PORT=0, XHR_MAX_CONNECTIONS=3,
        SSH_EXIT_HOST='', SSH_EXIT_PORT='22022', SSH_HOST_KEY='', SSH_USER_SECRET='',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def _bot_db(path, users):
    """A real bot.db: (chat_id, status, uuid, subscription_expiry) rows, xhr on."""
    from bot.core.database import Database
    from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
    db = Database(str(path))
    with db._connect() as conn:
        conn.executemany("INSERT INTO users (chat_id, username, status, uuid, "
                         "subscription_expiry) VALUES (?, ?, ?, ?, ?)",
                         [(cid, f'u{cid}', st, uid, exp) for cid, st, uid, exp in users])
        conn.commit()
    db.set_setting(MK.SETTING_KEY, json.dumps([{'name': 'reality', 'enabled': True},
                                               {'name': 'xhr', 'enabled': True}]))
    return db


@pytest.mark.filterwarnings("ignore:Database\\..*is deprecated:DeprecationWarning")
class TestTheBotsOwnAnswer:
    """The body the real endpoint answers on a real sqlite, fed to the sync."""

    def _answer(self, tmp_path, **over):
        from bot.core.web_server import WebAppServer
        db = _bot_db(tmp_path / 'bot.db', [('1', 'paid', U1, None), ('2', 'demo', U2, None)])
        srv = WebAppServer(_bot_config(**over), db, xui_service=Mock())
        req = Mock()
        req.headers = {'X-DPI-Token': TOKEN}
        resp = asyncio.run(srv.handle_xhr_users(req))
        assert resp.status == 200
        return resp.text

    def test_the_real_answer_syncs(self, set_up, tmp_path):
        set_up.users([U2])
        assert set_up.run('--sync').returncode == 0          # live: U2
        set_up.touch('answer', self._answer(tmp_path))       # the bot lists the payer only
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr                   # and this server's keys
        assert _ids(set_up.config()) == [U1]
        assert '1 user(s) (+1 -1)' in r.stdout

    def test_a_bot_without_this_servers_key_is_caught(self, set_up, tmp_path):
        set_up.touch('answer', self._answer(tmp_path, XHR_PUBLIC_KEY=''))
        r = set_up.run('--sync')
        assert r.returncode != 0
        assert f'set XHR_PUBLIC_KEY={PUB} in entry' in r.stderr
        assert '"main-inbound-pbk"' in r.stderr


# --------------------------------------------------------------- stand ---

MIHOMO = 'metacubex/mihomo:v1.19.33'
HAPROXY = ('haproxy:2.8-alpine'
           '@sha256:fce63f5dde742cd1f2b883ab39f7d4f9f88788827dd54fcfa864f63b9eec3bc8')
WEB_BASE = 'alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc'


def _have(image):
    return subprocess.run(['docker', 'image', 'inspect', image],
                          capture_output=True).returncode == 0


def _docker(*args, check=True):
    r = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=300)
    if check and r.returncode:
        raise AssertionError(f'docker {args[:3]}: {r.stderr[-500:]}')
    return r.stdout.strip()


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _delay(port, proxy, url, timeout_ms=10000):
    q = urllib.request.quote(url, safe='')
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/proxies/{proxy}/delay'
                                    f'?url={q}&timeout={timeout_ms}', timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None


def _printed_block(stdout, title):
    lines, inside = [], False
    for line in stdout.splitlines():
        if line.startswith('=== '):
            inside = title in line
            continue
        if inside and line.strip() and not line.lstrip().startswith('#'):
            lines.append(line)
    return lines


@pytest.mark.requires_docker
@pytest.mark.requires_network
@pytest.mark.filterwarnings("ignore:Database\\..*is deprecated:DeprecationWarning")
def test_stand_the_scripts_server_behind_haproxy_in_mihomo(env, tmp_path):
    """The rollout, end to end. Setup runs while the bot does not know this
    server yet; the bot then takes the .env lines setup printed and the sync
    pulls the users from the real endpoint (real curl) with no key mismatch.
    The container the script would start (its own flags: read-only, no
    capabilities, the image's user) serves the rendered config in xray
    26.6.22, behind haproxy 2.8 configured from the lines the script printed,
    and the bot's own Clash proxy in mihomo 1.19.33 gets through. A lapsed
    payer's uuid, a connection without haproxy's PROXY header, and a private
    address through the tunnel do not."""
    if shutil.which('curl') is None:
        pytest.skip('curl is not installed')
    if not all(_have(i) for i in (IMAGE, MIHOMO, HAPROXY)):
        pytest.skip('stand images not pulled')
    from aiohttp import web

    from bot.core.web_server import WebAppServer
    from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
    from bot.services.subscription import SubscriptionService

    # Real keys; the script keeps a keys.env it finds.
    kp = dict(ln.split(':', 1) for ln in _docker('run', '--rm', IMAGE, 'x25519').splitlines()
              if ':' in ln)
    priv = kp['PrivateKey'].strip()
    pub = next(v for k, v in kp.items() if k.startswith('Password')).strip()
    env.dir.mkdir(mode=0o700)
    (env.dir / 'keys.env').write_text(f'PRIVATE_KEY={priv}\nPUBLIC_KEY={pub}\nSHORT_ID={SID}\n')
    (env.dir / 'keys.env').chmod(0o600)

    # The bot on 127.0.0.1, before entry's .env knows this server.
    lapsed = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db = _bot_db(tmp_path / 'bot.db', [('1', 'paid', U1, None), ('2', 'paid', U2, lapsed)])
    db.set_setting(MK.SETTING_KEY, '')
    cfg = _bot_config(XHR_SNI='', XHR_PUBLIC_KEY='', XHR_SID='', XHR_PATH='/xhr',
                      ENTRY_NODE_IP='e-xhr-haproxy', ENTRY_NODE_PORT=8443)
    bot_port = _free_port()
    loop = asyncio.new_event_loop()
    runner = web.AppRunner(WebAppServer(cfg, db, xui_service=Mock()).app)
    loop.run_until_complete(runner.setup())
    loop.run_until_complete(web.TCPSite(runner, '127.0.0.1', bot_port).start())
    threading.Thread(target=loop.run_forever, daemon=True).start()

    net = f'nekovpn-xhr-exit-stand-{bot_port}'
    names = ('e-xhr-exit', 'e-xhr-haproxy', 'e-xhr-mihomo', 'e-xhr-web')
    try:
        (env.bin / 'curl').unlink()     # the real curl talks to the real bot
        env.write_env(BOT_URL=f'http://127.0.0.1:{bot_port}', XHR_SNI='www.google.com',
                      XHR_EXIT_PUBLIC_IP='e-xhr-exit')
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert 'HTTP 404' in r.stdout and _ids(env.config()) == []
        setup_out = r.stdout

        # entry's .env from the printed lines; xhr switched on in the cascade
        dotenv = dict(ln.split('=', 1) for ln in _printed_block(setup_out, '.env'))
        assert dotenv == {'XHR_SNI': 'www.google.com', 'XHR_PATH': '/xhr',
                          'XHR_PUBLIC_KEY': pub, 'XHR_SID': SID}
        for key, value in dotenv.items():
            setattr(cfg, key, value)
        db.set_setting(MK.SETTING_KEY, json.dumps([{'name': 'reality', 'enabled': True},
                                                   {'name': 'xhr', 'enabled': True}]))
        r = env.run('--sync')
        assert r.returncode == 0, r.stderr     # the bot hands out this server's keys
        assert _ids(env.config()) == [U1]      # the lapsed payer is not listed

        # the container exactly as the script starts it, on the stand's network
        args = env.start_args()
        stand_cfg = tmp_path / 'stand-config.json'
        stand_cfg.write_text((env.dir / 'config.json').read_text())
        stand_cfg.chmod(0o644)   # the script chowns it to 65532, a test cannot
        run, skip = [], 0
        for i, a in enumerate(args):
            if skip:
                skip -= 1
                continue
            if a in ('-p', '--restart'):
                skip = 1
                continue
            if a == '--name':
                run += ['--name', 'e-xhr-exit', '--network', net]
                skip = 1
                continue
            if a == '-v':
                run += ['-v', f'{stand_cfg}:/etc/xray/config.json:ro']
                skip = 1
                continue
            run.append(a)
        assert run[:2] == ['run', '-d'] and IMAGE in run

        # entry's haproxy: a frontend like entry's, plus exactly the printed lines
        printed = _printed_block(setup_out, 'haproxy')
        backend_at = next(i for i, ln in enumerate(printed) if ln.startswith('backend '))
        assert printed[:backend_at] == ['    acl is_xhr_sni req_ssl_sni -i www.google.com',
                                        '    use_backend xhr_exit if is_xhr_sni']
        (tmp_path / 'h').mkdir()
        (tmp_path / 'h' / 'haproxy.cfg').write_text('\n'.join([
            'global', '    log stdout format raw local0',
            'defaults', '    log global', '    mode tcp', '    timeout connect 5s',
            '    timeout client 1h', '    timeout server 1h',
            'frontend fe_entry', '    bind :8443', '    tcp-request inspect-delay 5s',
            '    tcp-request content accept if { req_ssl_hello_type 1 }',
            *printed, '']))

        web_image = 'nekovpn-xhr-stand-web:local'
        built = subprocess.run(['docker', 'build', '-q', '-t', web_image, '-'], input=(
            f'FROM {WEB_BASE}\nRUN apk add --no-cache busybox-extras\n'
            'CMD ["httpd", "-f", "-p", "80", "-h", "/etc"]\n'), capture_output=True, text=True)
        if built.returncode:
            pytest.skip(f'could not build the web image: {built.stderr[-200:]}')

        user = SimpleNamespace(uuid=U1, email='u1@x', status='paid', lang='ru', chat_id='1',
                               subscription_expiry=None, previous_state=None)
        clash = json.loads(SubscriptionService(cfg).build_clash_config(user, ('xhr',)))
        good = next(p for p in clash['proxies'] if p['name'] == 'u1-xhr')
        assert (good['server'], good['port']) == ('e-xhr-haproxy', 8443)
        proxies = [good, dict(good, name='u2-lapsed', uuid=U2),
                   dict(good, name='u1-no-proxy-header', server='e-xhr-exit', port=8446)]
        (tmp_path / 'm').mkdir()
        (tmp_path / 'm' / 'config.yaml').write_text(json.dumps({
            'mode': 'rule', 'external-controller': '0.0.0.0:9090', 'proxies': proxies,
            'rules': ['MATCH,DIRECT']}))
        ctl = _free_port()

        _docker('network', 'create', net)
        _docker('run', '-d', '--name', 'e-xhr-web', '--network', net, web_image)
        _docker(*run)
        _docker('run', '-d', '--name', 'e-xhr-haproxy', '--network', net,
                '-v', f'{tmp_path}/h:/usr/local/etc/haproxy:ro', HAPROXY)
        _docker('run', '-d', '--name', 'e-xhr-mihomo', '--network', net,
                '-p', f'127.0.0.1:{ctl}:9090',
                '-v', f'{tmp_path}/m:/root/.config/mihomo', MIHOMO, '-d', '/root/.config/mihomo')
        deadline = time.monotonic() + 30
        while _delay(ctl, 'DIRECT', 'http://e-xhr-web/hostname', 2000) is None:
            assert time.monotonic() < deadline, 'mihomo did not come up'
            time.sleep(0.5)

        gstatic = 'https://www.gstatic.com/generate_204'
        assert _delay(ctl, 'u1-xhr', gstatic) == 200, _docker('logs', 'e-xhr-exit', check=False)
        assert _delay(ctl, 'u2-lapsed', gstatic) != 200
        assert _delay(ctl, 'u1-no-proxy-header', gstatic) != 200
        web_ip = _docker('inspect', '-f',
                         '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', 'e-xhr-web')
        private = f'http://{web_ip}/hostname'
        assert _delay(ctl, 'DIRECT', private, 5000) == 200      # it does answer...
        assert _delay(ctl, 'u1-xhr', private, 5000) != 200      # ...but not through xhr
        assert _docker('inspect', '-f', '{{.State.Running}} {{.RestartCount}}',
                       'e-xhr-exit') == 'true 0'
    finally:
        for name in names:
            _docker('rm', '-f', name, check=False)
        _docker('network', 'rm', net, check=False)
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
