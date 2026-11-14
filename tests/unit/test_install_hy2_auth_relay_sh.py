"""scripts/install_hy2_auth_relay.sh and systemd/hy2-auth-relay.service — the
hy2 auth relay's installer on exit (IMPROVEMENT_PLAN F, A6; AGENTS.md §56).

The installer runs for real (bash, python3, install, grep) with
``systemctl`` replaced by a fake that keeps its state in files and
``install`` by a wrapper that only drops the owner a test cannot set. What is
pinned: the unit runs a root-owned copy with ``python3 -I``, never the rsync
target; the installer never touches hysteria (it only reports whether
hysteria asks the relay); it fails when the relay does not stay up; --remove
refuses while a hysteria config still points at the relay.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'install_hy2_auth_relay.sh'
UNIT = REPO / 'systemd' / 'hy2-auth-relay.service'

# hysteria's configs on exit, as they are since the §53 cutover (the hop) and
# as they are once they ask the relay
BOT_MAIN = ('listen: :8400\nauth:\n  type: http\n  http:\n'
            '    url: https://entry-hop.invalid:8448/api/hy2/auth\n')
BOT_TURBO = ('listen: :8402\nauth:\n  type: http\n  http:\n'
             '    url: https://entry-hop.invalid:8448/api/hy2t/auth\n')
RELAY_MAIN = BOT_MAIN.replace('https://entry-hop.invalid:8448', 'http://127.0.0.1:9979')
RELAY_TURBO = BOT_TURBO.replace('https://entry-hop.invalid:8448', 'http://127.0.0.1:9979')

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
st = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
with open(st / 'calls.log', 'a') as f:
    f.write(json.dumps(['systemctl'] + args) + '\n')
if args[:1] == ['is-active']:
    print((st / 'active').read_text().strip() if (st / 'active').exists() else 'active')
elif args[:1] == ['show']:
    pids = (st / 'pids').read_text().split() if (st / 'pids').exists() else ['4242']
    if len(pids) > 1:
        (st / 'pids').write_text(' '.join(pids[1:]))
    print(pids[0])
'''

# The real install, minus -o/-g: a test does not run as root everywhere. The
# call is logged with the owner it asked for.
FAKE_INSTALL = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.path.join(os.environ['FAKE_STATE'], 'calls.log'), 'a') as f:
    f.write(json.dumps(['install'] + args) + '\n')
keep, skip = [], False
for a in args:
    if skip:
        skip = False
    elif a in ('-o', '-g'):
        skip = True
    else:
        keep.append(a)
os.execv(os.environ['REAL_INSTALL'], ['install'] + keep)
'''

FAKE_ID = r'''#!/bin/sh
[ "$1" = "-u" ] && { cat "$FAKE_STATE/uid" 2>/dev/null || echo 0; exit 0; }
exec /usr/bin/id "$@"
'''

# Tripwires: nothing the installer runs may reach the machine's power state,
# hysteria, docker, the firewall or the host's users. They come first in PATH,
# log the attempt and fail; the fixture fails the test. Test payloads only
# ever touch files in the test's own directory (AGENTS.md §51: a ";reboot" in
# a test env file rebooted the developer's laptop twice).
TRIPWIRES = ('reboot', 'shutdown', 'poweroff', 'halt', 'init', 'telinit', 'kill', 'pkill',
             'killall', 'hysteria', 'docker', 'iptables', 'nft', 'ufw', 'useradd', 'userdel',
             'adduser', 'deluser', 'chpasswd', 'curl', 'ssh')
FAKE_TRIPWIRE = r'''#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_STATE/tripwire.log"
exit 1
'''


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / 'bin'
        self.state = root / 'state'
        self.lib = root / 'lib' / 'nekovpn-hy2-relay'
        self.units = root / 'units'
        self.hysteria = root / 'hysteria'
        for d in (self.bin, self.state, self.units, self.hysteria):
            d.mkdir(parents=True)
        for name, body in (('systemctl', FAKE_SYSTEMCTL), ('install', FAKE_INSTALL),
                           ('id', FAKE_ID), *((t, FAKE_TRIPWIRE) for t in TRIPWIRES)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.configs(BOT_MAIN, BOT_TURBO)

    def configs(self, main, turbo):
        (self.hysteria / 'config.yaml').write_text(main)
        (self.hysteria / 'config-turbo.yaml').write_text(turbo)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith('RELAY_')}
        env.update(PATH=f'{self.bin}:{env["PATH"]}', FAKE_STATE=str(self.state),
                   REAL_INSTALL=shutil.which('install'), SYSTEMCTL='systemctl',
                   RELAY_LIB_DIR=str(self.lib), RELAY_UNIT_DIR=str(self.units),
                   RELAY_HYSTERIA_DIR=str(self.hysteria), RELAY_WAIT_S='0', **extra)
        return env

    def run(self, *args, script=SCRIPT, **extra):
        return subprocess.run(['bash', str(script), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def calls(self, tool=None):
        p = self.state / 'calls.log'
        calls = [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []
        return [c for c in calls if tool is None or c[0] == tool]

    def set(self, name, text):
        (self.state / name).write_text(text)


@pytest.fixture
def env(tmp_path):
    if shutil.which('install') is None:
        pytest.skip('install (coreutils) is not installed')
    e = Env(tmp_path)
    yield e
    tripped = e.state / 'tripwire.log'
    assert not tripped.exists(), f'the installer ran {tripped.read_text()!r}'
    for call in e.calls('systemctl'):
        assert not set(call[1:]) & {'reboot', 'poweroff', 'halt', 'kexec', 'suspend',
                                     'hibernate'}, call
        assert not any('hysteria' in a for a in call[1:]), call


class TestInstall:

    def test_a_root_owned_copy_is_started_and_hysteria_is_left_alone(self, env):
        r = env.run()
        assert r.returncode == 0, r.stderr
        copy = env.lib / 'hy2_auth_relay.py'
        assert copy.read_bytes() == (REPO / 'scripts' / 'hy2_auth_relay.py').read_bytes()
        assert oct(copy.stat().st_mode & 0o777) == '0o644'
        assert oct(env.lib.stat().st_mode & 0o777) == '0o755'
        assert not (env.lib / 'hy2_auth_relay.py.new').exists()
        assert (env.units / 'hy2-auth-relay.service').read_bytes() == UNIT.read_bytes()
        installs = env.calls('install')
        assert len(installs) == 3
        for call in installs:
            assert call[call.index('-o') + 1] == 'root' and call[call.index('-g') + 1] == 'root'
        assert [c[1:] for c in env.calls('systemctl')] == [
            ['daemon-reload'], ['enable', 'hy2-auth-relay.service'],
            ['restart', 'hy2-auth-relay.service'],
            ['show', '-p', 'MainPID', '--value', 'hy2-auth-relay.service'],
            ['is-active', 'hy2-auth-relay.service'],
            ['show', '-p', 'MainPID', '--value', 'hy2-auth-relay.service']]
        assert 'running' in r.stdout and 'hysteria does not ask the relay yet' in r.stdout
        assert (env.hysteria / 'config.yaml').read_text() == BOT_MAIN
        assert (env.hysteria / 'config-turbo.yaml').read_text() == BOT_TURBO

    def test_a_second_run_updates_the_copy(self, env):
        assert env.run().returncode == 0
        (env.lib / 'hy2_auth_relay.py').write_text('stale')
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert (env.lib / 'hy2_auth_relay.py').read_bytes() == \
            (REPO / 'scripts' / 'hy2_auth_relay.py').read_bytes()

    def test_it_says_when_hysteria_asks_the_relay(self, env):
        env.configs(RELAY_MAIN, RELAY_TURBO)
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert 'hysteria asks the relay' in r.stdout
        assert 'config.yaml' in r.stdout and 'config-turbo.yaml' in r.stdout

    @pytest.mark.parametrize('active, pids', [
        ('activating', '4242 4242'),     # restarting after it refused its config
        ('active', '0 4300'),            # not up at the first look
        ('active', '0 0'),               # never up
        ('active', '4242 4300'),         # died and came back between the looks
    ])
    def test_a_relay_that_does_not_stay_up_fails_the_install(self, env, active, pids):
        env.set('active', active)
        env.set('pids', pids)
        r = env.run()
        assert r.returncode == 1
        assert 'does not stay up' in r.stderr and 'journalctl -u hy2-auth-relay' in r.stderr

    def test_a_copy_that_does_not_compile_is_not_installed(self, env, tmp_path):
        tree = tmp_path / 'checkout'
        (tree / 'scripts').mkdir(parents=True)
        (tree / 'systemd').mkdir()
        shutil.copy(SCRIPT, tree / 'scripts')
        shutil.copy(UNIT, tree / 'systemd')
        (tree / 'scripts' / 'hy2_auth_relay.py').write_text('def broken(:\n')
        r = env.run(script=tree / 'scripts' / 'install_hy2_auth_relay.sh')
        assert r.returncode != 0
        assert not env.lib.exists() and env.calls('systemctl') == []

    def test_a_missing_file_is_refused(self, env, tmp_path):
        tree = tmp_path / 'checkout'
        (tree / 'scripts').mkdir(parents=True)
        shutil.copy(SCRIPT, tree / 'scripts')
        shutil.copy(REPO / 'scripts' / 'hy2_auth_relay.py', tree / 'scripts')
        r = env.run(script=tree / 'scripts' / 'install_hy2_auth_relay.sh')
        assert r.returncode == 1 and 'missing:' in r.stderr
        assert env.calls() == []

    def test_only_root(self, env):
        env.set('uid', '1000')
        r = subprocess.run(['bash', str(SCRIPT)],
                           env={k: v for k, v in env.env().items() if k != 'RELAY_ALLOW_NONROOT'},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 1 and 'run as root' in r.stderr
        assert env.calls() == []

    def test_an_unknown_argument(self, env):
        r = env.run('--force')
        assert r.returncode == 2 and 'usage' in r.stderr
        assert env.calls() == []


class TestRemove:

    def test_it_refuses_while_hysteria_asks_the_relay(self, env):
        assert env.run().returncode == 0
        env.configs(BOT_MAIN, RELAY_TURBO)
        before = len(env.calls())
        r = env.run('--remove')
        assert r.returncode == 1
        assert 'config-turbo.yaml' in r.stderr and 'point it back at the bot first' in r.stderr
        assert (env.lib / 'hy2_auth_relay.py').exists()
        assert (env.units / 'hy2-auth-relay.service').exists()
        assert len(env.calls()) == before

    @pytest.mark.parametrize('line', [
        "    url: 'http://127.0.0.1:9979/api/hy2/auth'",
        '    url: "http://[::1]:9979/api/hy2/auth"',
        '    url: http://localhost:9979/api/hy2t/auth',
        '    url:   http://127.0.0.2/api/hy2/auth',
    ])
    def test_any_spelling_of_a_loopback_relay_counts(self, env, line):
        env.configs(f'auth:\n  type: http\n  http:\n{line}\n', BOT_TURBO)
        assert env.run('--remove').returncode == 1

    def test_a_commented_out_relay_line_does_not_count(self, env):
        assert env.run().returncode == 0
        env.configs(BOT_MAIN + '#   url: http://127.0.0.1:9979/api/hy2/auth\n', BOT_TURBO)
        r = env.run('--remove')
        assert r.returncode == 0, r.stderr

    def test_it_removes_everything_it_installed(self, env):
        assert env.run().returncode == 0
        r = env.run('--remove')
        assert r.returncode == 0 and 'removed' in r.stdout
        assert not env.lib.exists()
        assert not (env.units / 'hy2-auth-relay.service').exists()
        calls = [c[1:] for c in env.calls('systemctl')]
        assert calls[-2:] == [['disable', '--now', 'hy2-auth-relay.service'], ['daemon-reload']]


class TestUnit:

    def unit(self):
        return UNIT.read_text()

    def test_it_runs_the_root_owned_copy_isolated(self):
        text = self.unit()
        execs = re.findall(r'^ExecStart=(.*)$', text, re.M)
        assert execs == ['/usr/bin/python3 -I /usr/local/lib/nekovpn-hy2-relay/hy2_auth_relay.py']
        default = re.search(r'LIB_DIR="\$\{RELAY_LIB_DIR:-([^}]+)\}"', SCRIPT.read_text()).group(1)
        assert execs[0].endswith(f'{default}/hy2_auth_relay.py')
        assert '/opt/vpn-bot' not in text.split('[Service]')[1]
        assert not re.search(r'^Exec(StartPre|StartPost|Reload|Stop)=', text, re.M)

    def test_it_never_stops_restarting_and_needs_no_privilege(self):
        text = self.unit()
        for line in ('StartLimitIntervalSec=0', 'Restart=always', 'DynamicUser=yes',
                     'NoNewPrivileges=yes', 'CapabilityBoundingSet=', 'ProtectSystem=strict',
                     'Before=hysteria.service hysteria-turbo.service'):
            assert re.search(rf'^{re.escape(line)}$', text, re.M), line
        assert text.index('StartLimitIntervalSec=0') < text.index('[Service]')

    def test_its_environment_files_are_optional_data(self):
        assert re.findall(r'^EnvironmentFile=(.*)$', self.unit(), re.M) == [
            '-/etc/exit-dpi.env', '-/etc/hy2-auth-relay.env']

    def test_systemd_accepts_it(self, tmp_path):
        if shutil.which('systemd-analyze') is None:
            pytest.skip('systemd-analyze is not installed')
        unit = tmp_path / 'hy2-auth-relay.service'
        shutil.copy(UNIT, unit)
        r = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                           capture_output=True, text=True, timeout=60)
        if 'Failed to ' in r.stderr and 'bus' in r.stderr.lower():
            pytest.skip(f'systemd-analyze cannot run here: {r.stderr.strip()[:200]}')
        assert r.returncode == 0, r.stderr
        assert 'hy2-auth-relay.service' not in r.stderr, r.stderr
