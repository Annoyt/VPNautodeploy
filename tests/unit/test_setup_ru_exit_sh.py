"""scripts/setup_ru_exit.sh — the RU-zone server's setup and its 2-minute
``--sync`` (bot/services/ru_exit.py).

The script runs as root on entry against docker and systemd; here it runs
for real (bash, python3, flock, cmp, comm) with ``docker`` / ``systemctl``
/ ``ss`` / ``openssl`` / ``id`` replaced by fakes that keep their state in
files. What is pinned: an unchanged user list touches nothing, a changed
one is checked before the live config moves, a container that does not
come back is rolled back, a list that suddenly reads empty is not applied
by the timer, a bot being redeployed is a skip and not a failure, and
setup / remove write and clear what the bot and systemd read. A second,
docker-backed test feeds the rendered config to the real ``sing-box
check`` of the pinned image.
"""

import fcntl
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'setup_ru_exit.sh'
IMAGE = 'ghcr.io/sagernet/sing-box:v1.11.15'

U1 = {'name': 'c111', 'uuid': '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70', 'flow': 'xtls-rprx-vision'}
U2 = {'name': 'c222', 'uuid': '0f9e8d7c-6b5a-4c3d-8e2f-1a0b9c8d7e6f', 'flow': 'xtls-rprx-vision'}

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
st = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
with open(st / 'calls.log', 'a') as f:
    f.write(json.dumps(['docker'] + args) + '\n')

def flag(name):
    return (st / name).exists()

cmd = args[0]
if cmd == 'inspect':
    name = args[-1]
    key = {'vpn-bot': 'bot', 'ru-exit': 'ru_exit'}.get(name, name)
    if not flag(f'{key}_exists'):
        print(f'Error: No such object: {name}', file=sys.stderr)
        sys.exit(1)
    print('true' if flag(f'{key}_running') else 'false')
elif cmd == 'ps':
    for key, name in (('bot', 'vpn-bot'), ('ru_exit', 'ru-exit')):
        if flag(f'{key}_exists'):
            print(name)
elif cmd == 'exec':
    if 'ru_exit.py' in ' '.join(args):           # the user list
        if flag('exec_fail'):
            print('Traceback: boom', file=sys.stderr)
            sys.exit(1)
        if flag('bot_dies_on_exec'):
            (st / 'bot_running').unlink()
            sys.exit(1)
        sys.stdout.write((st / 'users.json').read_text())
    else:                                         # set_bot_setting
        sys.stdin.read()
        (st / 'setting').write_text(args[-1])
elif cmd == 'run':
    if '--rm' in args and 'generate' in args:
        print('PrivateKey: fake-private-key')
        print('PublicKey: fake-public-key')
    elif '--rm' in args and 'check' in args:
        if flag('check_fail'):
            print('FATAL: bad config', file=sys.stderr)
            sys.exit(1)
        host_dir = args[args.index('-v') + 1].split(':')[0]
        json.load(open(Path(host_dir) / Path(args[-1]).name))
    elif '-d' in args:
        host_dir = args[args.index('-v') + 1].split(':')[0]
        n = len(list(st.glob('started_*.json')))
        (st / f'started_{n}.json').write_text((Path(host_dir) / 'config.json').read_text())
        (st / 'ru_exit_exists').touch()
        fails = st / 'start_fails'
        if fails.exists() and int(fails.read_text()) > 0:
            fails.write_text(str(int(fails.read_text()) - 1))
            (st / 'ru_exit_running').unlink(missing_ok=True)
        else:
            (st / 'ru_exit_running').touch()
        print('0123abcd')
    else:
        sys.exit(f'fake docker: unexpected run {args}')
elif cmd == 'rm':
    (st / 'ru_exit_exists').unlink(missing_ok=True)
    (st / 'ru_exit_running').unlink(missing_ok=True)
elif cmd == 'logs':
    print('fake ru-exit log line')
else:
    sys.exit(f'fake docker: unexpected {args}')
'''

FAKE_SS = r'''#!/bin/sh
echo "ss $*" >> "$FAKE_STATE/calls.log"
if [ -e "$FAKE_STATE/ru_exit_running" ] || [ -e "$FAKE_STATE/port_taken" ]; then
  echo "LISTEN 0 4096 0.0.0.0:8445 0.0.0.0:*"
fi
'''

FAKE_OPENSSL = r'''#!/bin/sh
case "$1" in
  rand) echo 0123456789abcdef ;;
  s_client) cat >/dev/null; echo "<<< TLS 1.3, Handshake [length $(cat "$FAKE_STATE/cert_hex")], Certificate" ;;
esac
'''

FAKE_SYSTEMCTL = r'''#!/bin/sh
echo "systemctl $*" >> "$FAKE_STATE/calls.log"
'''

FAKE_ID = '#!/bin/sh\necho 0\n'

FAKE_CHOWN = r'''#!/bin/sh
echo "chown $*" >> "$FAKE_STATE/calls.log"
[ ! -e "$FAKE_STATE/chown_fail" ]
'''


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / 'bin'
        self.state = root / 'state'
        self.dir = root / 'etc-ru-exit'
        self.units = root / 'units'
        self.sbin = root / 'sbin'
        self.copy = self.sbin / 'ru-exit-sync'
        for d in (self.bin, self.state, self.units, self.sbin):
            d.mkdir()
        for name, body in (('docker', FAKE_DOCKER), ('ss', FAKE_SS),
                           ('openssl', FAKE_OPENSSL), ('systemctl', FAKE_SYSTEMCTL),
                           ('id', FAKE_ID), ('chown', FAKE_CHOWN)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        (self.state / 'cert_hex').write_text('09d8')    # 2520 bytes
        self.bot(True)
        self.users([U1])

    def bot(self, running: bool):
        (self.state / 'bot_exists').touch()
        p = self.state / 'bot_running'
        if running:
            p.touch()
        else:
            p.unlink(missing_ok=True)

    def users(self, users):
        (self.state / 'users.json').write_text(json.dumps(users))

    def touch(self, name, text=None):
        p = self.state / name
        p.write_text(text) if text is not None else p.touch()

    def env(self, **extra):
        env = dict(os.environ)
        for k in ('RU_EXIT_PORT', 'RU_EXIT_SNI', 'RU_EXIT_IMAGE'):
            env.pop(k, None)
        env.update(PATH=f'{self.bin}:{env["PATH"]}', FAKE_STATE=str(self.state),
                   RU_EXIT_DIR=str(self.dir), RU_EXIT_UNIT_DIR=str(self.units),
                   RU_EXIT_BIN=str(self.copy), RU_EXIT_SETTLE_S='0', **extra)
        return env

    def run(self, *args, script=SCRIPT, **extra):
        return subprocess.run(['bash', str(script), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def calls(self):
        p = self.state / 'calls.log'
        if not p.exists():
            return []
        out = []
        for line in p.read_text().splitlines():
            out.append(json.loads(line) if line.startswith('[') else line.split())
        return out

    def docker_calls(self, sub):
        return [c for c in self.calls() if c[:2] == ['docker', sub]]

    def reset_calls(self):
        (self.state / 'calls.log').unlink(missing_ok=True)

    def config(self, name='config.json'):
        return json.loads((self.dir / name).read_text())

    def starts(self):
        return sorted(self.state.glob('started_*.json'))


@pytest.fixture
def env(tmp_path):
    if shutil.which('flock') is None:
        pytest.skip('flock (util-linux) is not installed')
    return Env(tmp_path)


def _names(cfg):
    return [u['name'] for u in cfg['inbounds'][0]['users']]


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
        assert 'PRIVATE_KEY=fake-private-key' in keys and 'SHORT_ID=0123456789abcdef' in keys
        assert (env.dir / 'server.env').read_text() == (
            f'PORT=8445\nSNI=www.google.com\nIMAGE={IMAGE}\n')
        cfg = env.config()
        assert cfg['inbounds'][0]['users'] == [U1]
        assert cfg['inbounds'][0]['listen_port'] == 8445
        assert cfg['inbounds'][0]['tls']['reality']['private_key'] == 'fake-private-key'
        assert cfg['inbounds'][0]['tls']['reality']['short_id'] == ['0123456789abcdef']
        assert (env.state / 'ru_exit_running').exists()
        # The bot reads the server parameters; access is no list any more.
        assert json.loads((env.state / 'setting').read_text()) == {
            'port': 8445, 'sni': 'www.google.com', 'pbk': 'fake-public-key',
            'sid': '0123456789abcdef'}
        assert f'ExecStart={env.copy} --sync' in (
            env.units / 'ru-exit-sync.service').read_text()
        timer = (env.units / 'ru-exit-sync.timer').read_text()
        assert 'OnUnitActiveSec=2min' in timer and 'WantedBy=timers.target' in timer
        assert ['systemctl', 'enable', '--now', 'ru-exit-sync.timer'] in env.calls()
        assert '1 user(s) (+1 -0)' in r.stdout

    def test_timer_runs_a_root_owned_copy(self, env):
        # /opt/vpn-bot is an rsync target the deploy account can write: the
        # root timer must never execute the repo file itself.
        assert env.run().returncode == 0
        assert env.copy.read_bytes() == SCRIPT.read_bytes()
        assert env.copy.stat().st_mode & 0o777 == 0o755
        assert ['chown', 'root:root', f'{env.copy}.new'] in env.calls()
        unit = (env.units / 'ru-exit-sync.service').read_text()
        assert str(SCRIPT) not in unit
        assert not (env.sbin / 'ru-exit-sync.new').exists()

    def test_setup_from_the_copy_itself(self, set_up):
        r = set_up.run(script=set_up.copy)
        assert r.returncode == 0, r.stderr
        assert f'ExecStart={set_up.copy} --sync' in (
            set_up.units / 'ru-exit-sync.service').read_text()

    def test_copy_that_cannot_be_owned_by_root(self, env):
        env.touch('chown_fail')
        r = env.run()
        assert r.returncode != 0 and 'could not install' in r.stderr
        assert not env.copy.exists() and not (env.sbin / 'ru-exit-sync.new').exists()
        assert not (env.units / 'ru-exit-sync.service').exists()

    def test_route_guards(self, set_up):
        rules = set_up.config()['route']['rules']
        assert rules[:3] == [{'action': 'sniff'},
                             {'protocol': 'bittorrent', 'action': 'reject'},
                             {'port': 25, 'action': 'reject'}]
        assert rules[-1] == {'ip_is_private': True, 'action': 'reject'}
        assert set_up.config()['route']['final'] == 'direct'

    def test_config_is_private(self, set_up):
        for name in ('config.json', 'keys.env', 'server.env'):
            assert (set_up.dir / name).stat().st_mode & 0o077 == 0, name

    def test_rerun_keeps_the_keys(self, set_up):
        (set_up.dir / 'keys.env').write_text(
            'PRIVATE_KEY=kept\nPUBLIC_KEY=kept-pub\nSHORT_ID=feed\n')
        assert set_up.run().returncode == 0
        assert all('generate' not in c for c in set_up.docker_calls('run'))
        assert set_up.config()['inbounds'][0]['tls']['reality']['private_key'] == 'kept'
        assert json.loads((set_up.state / 'setting').read_text())['pbk'] == 'kept-pub'

    def test_empty_list_is_applied_by_hand(self, set_up):
        set_up.users([])
        r = set_up.run()
        assert r.returncode == 0, r.stderr
        assert _names(set_up.config()) == []

    def test_port_taken_by_someone_else(self, env):
        env.touch('port_taken')
        r = env.run()
        assert r.returncode != 0 and 'already taken' in r.stderr
        assert not (env.dir / 'config.json').exists()

    def test_oversized_dest_certificate(self, env):
        env.touch('cert_hex', '2051')     # 8273 bytes — the 2026-07-20 outage
        r = env.run()
        assert r.returncode != 0 and '8273 bytes' in r.stderr
        assert not (env.dir / 'config.json').exists()

    def test_bot_not_running(self, env):
        env.bot(False)
        r = env.run()
        assert r.returncode != 0 and 'vpn-bot is not running' in r.stderr

    def test_usage(self, env):
        r = env.run('--bogus')
        assert r.returncode != 0 and 'usage' in r.stderr


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
        assert 'check' in runs[0] and '-d' in runs[1]
        assert _names(set_up.config()) == ['c111', 'c222']
        assert _names(set_up.config('config.json.prev')) == ['c111']
        assert _names(json.loads(set_up.starts()[-1].read_text())) == ['c111', 'c222']
        assert '2 user(s) (+1 -0)' in r.stdout

    def test_removal(self, set_up):
        set_up.users([U2])
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert _names(set_up.config()) == ['c222']
        assert '1 user(s) (+1 -1)' in r.stdout

    def test_stopped_container_comes_back(self, set_up):
        (set_up.state / 'ru_exit_running').unlink()
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert (set_up.state / 'ru_exit_running').exists()
        assert len(set_up.starts()) == 2

    def test_empty_list_is_not_applied(self, set_up):
        set_up.users([])
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'reads 0 users' in r.stderr
        assert _names(set_up.config()) == ['c111']
        assert not set_up.docker_calls('run')
        assert not (set_up.dir / 'config.json.new').exists()

    def test_check_failure_leaves_the_live_config(self, set_up):
        set_up.users([U1, U2])
        set_up.touch('check_fail')
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'sing-box check failed' in r.stderr
        assert _names(set_up.config()) == ['c111']
        assert len(set_up.starts()) == 1
        assert not (set_up.dir / 'config.json.new').exists()

    def test_container_that_does_not_come_back_is_rolled_back(self, set_up):
        set_up.users([U1, U2])
        set_up.touch('start_fails', '1')
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'rolled back' in r.stderr
        assert _names(set_up.config()) == ['c111']
        started = [_names(json.loads(p.read_text())) for p in set_up.starts()]
        assert started[-2:] == [['c111', 'c222'], ['c111']]
        assert (set_up.state / 'ru_exit_running').exists()

    def test_bot_redeploy_is_a_skip(self, set_up):
        set_up.bot(False)
        r = set_up.run('--sync')
        assert r.returncode == 0 and 'skipped' in r.stdout
        assert not set_up.docker_calls('exec')

    def test_bot_gone_mid_read_is_a_skip(self, set_up):
        set_up.touch('bot_dies_on_exec')
        r = set_up.run('--sync')
        assert r.returncode == 0 and 'skipped' in r.stdout
        assert len(set_up.starts()) == 1

    def test_failed_read_is_a_failure(self, set_up):
        set_up.touch('exec_fail')
        r = set_up.run('--sync')
        assert r.returncode != 0 and 'could not read the user list' in r.stderr
        assert len(set_up.starts()) == 1

    def test_junk_list_leaves_the_live_config(self, set_up):
        (set_up.state / 'users.json').write_text('{"not": "a list"}')
        r = set_up.run('--sync')
        assert r.returncode != 0
        assert _names(set_up.config()) == ['c111']
        assert len(set_up.starts()) == 1

    def test_not_set_up(self, env):
        r = env.run('--sync')
        assert r.returncode != 0 and 'not set up' in r.stderr
        assert not env.docker_calls('exec')

    def test_sync_keeps_the_setup_parameters(self, env):
        r = env.run(RU_EXIT_PORT='9445', RU_EXIT_SNI='www.bing.com')
        assert r.returncode == 0, r.stderr
        env.users([U1, U2])
        # The timer runs without the knobs: server.env must carry them.
        assert env.run('--sync').returncode == 0
        inbound = env.config()['inbounds'][0]
        assert inbound['listen_port'] == 9445
        assert inbound['tls']['server_name'] == 'www.bing.com'

    def test_a_running_sync_holds_the_lock(self, set_up):
        set_up.users([U1, U2])
        set_up.dir.mkdir(exist_ok=True)
        with open(set_up.dir / '.lock', 'w') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            r = set_up.run('--sync')
        assert r.returncode == 0 and r.stdout == ''
        assert not set_up.docker_calls('exec')
        assert _names(set_up.config()) == ['c111']


class TestRemove:

    def test_remove(self, set_up):
        r = set_up.run('--remove')
        assert r.returncode == 0, r.stderr
        assert (set_up.state / 'setting').read_text() == ''
        assert ['systemctl', 'disable', '--now', 'ru-exit-sync.timer'] in set_up.calls()
        assert not (set_up.units / 'ru-exit-sync.service').exists()
        assert not (set_up.units / 'ru-exit-sync.timer').exists()
        assert not set_up.copy.exists()
        assert not (set_up.state / 'ru_exit_exists').exists()
        assert (set_up.dir / 'keys.env').exists()


@pytest.mark.requires_docker
def test_rendered_config_passes_the_real_sing_box_check(set_up, tmp_path):
    if shutil.which('docker') is None:
        pytest.skip('docker is not installed')
    real = tmp_path / 'real'
    real.mkdir()
    cfg = set_up.config()
    cfg['inbounds'][0]['users'] = [U1, U2]
    # A real key: sing-box parses it.
    kp = subprocess.run(['docker', 'run', '--rm', IMAGE, 'generate', 'reality-keypair'],
                        capture_output=True, text=True, timeout=120)
    assert kp.returncode == 0, kp.stderr
    priv = next(ln.split()[1] for ln in kp.stdout.splitlines() if ln.startswith('PrivateKey'))
    cfg['inbounds'][0]['tls']['reality']['private_key'] = priv
    (real / 'config.json').write_text(json.dumps(cfg))
    r = subprocess.run(['docker', 'run', '--rm', '-v', f'{real}:/etc/sing-box:ro', IMAGE,
                        'check', '-c', '/etc/sing-box/config.json'],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
