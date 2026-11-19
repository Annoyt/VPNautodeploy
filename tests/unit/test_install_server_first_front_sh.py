"""scripts/install_server_first_front.sh and systemd/server-first-front.service —
the server-first front's installer on entry (IMPROVEMENT_PLAN G4,
AGENTS.md §62).

The installer runs for real (bash, python3, install, awk) with
``systemctl`` and ``ss`` replaced by fakes that keep their state in files and
``install`` by a wrapper that only drops the owner a test cannot set. What is
pinned: the unit runs a root-owned copy with ``python3 -I`` and a config it
checks first; the config is written from the example only when there is
none; a config that does not parse installs nothing; the installer fails
when the front does not stay up; it never touches haproxy or shadow-tls and
only says what is missing; --remove keeps the config.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'install_server_first_front.sh'
UNIT = REPO / 'systemd' / 'server-first-front.service'
EXAMPLE = REPO / 'systemd' / 'nekovpn-sf-front.conf.example'
FRONT = REPO / 'scripts' / 'server_first_front.py'

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

# `ss -Hltn "( sport = :PORT )"`: the listening sockets in state/listen
# (one "ADDR:PORT" per line) on that port, in ss's column layout.
FAKE_SS = r'''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
st = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
with open(st / 'calls.log', 'a') as f:
    f.write(json.dumps(['ss'] + args) + '\n')
port = re.search(r'sport = :(\d+)', ' '.join(args)).group(1)
lines = (st / 'listen').read_text().split() if (st / 'listen').exists() else []
for addr in lines:
    if addr.rsplit(':', 1)[1] == port:
        print(f'LISTEN 0      4096      {addr}      0.0.0.0:*')
'''

FAKE_UFW = r'''#!/bin/sh
echo "ufw $*" >> "$FAKE_STATE/ufw.log"
[ "$1" = "status" ] || { echo "ufw $*" >> "$FAKE_STATE/tripwire.log"; exit 1; }
cat "$FAKE_STATE/ufw" 2>/dev/null || echo "Status: inactive"
'''

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
# haproxy, shadow-tls, docker, the firewall or the host's users. Test payloads
# only ever touch files in the test's own directory (AGENTS.md §51).
TRIPWIRES = ('reboot', 'shutdown', 'poweroff', 'halt', 'init', 'telinit', 'kill', 'pkill',
             'killall', 'haproxy', 'shadow-tls', 'docker', 'iptables', 'nft', 'useradd',
             'userdel', 'adduser', 'deluser', 'chpasswd', 'curl', 'ssh')
FAKE_TRIPWIRE = r'''#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_STATE/tripwire.log"
exit 1
'''


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / 'bin'
        self.state = root / 'state'
        self.lib = root / 'lib' / 'nekovpn-sf-front'
        self.units = root / 'units'
        self.conf = root / 'etc' / 'nekovpn-sf-front.conf'
        self.haproxy_cfg = root / 'etc' / 'haproxy.cfg'
        for d in (self.bin, self.state, self.units, self.conf.parent):
            d.mkdir(parents=True)
        for name, body in (('systemctl', FAKE_SYSTEMCTL), ('ss', FAKE_SS), ('ufw', FAKE_UFW),
                           ('install', FAKE_INSTALL), ('id', FAKE_ID),
                           *((t, FAKE_TRIPWIRE) for t in TRIPWIRES)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith('SFF_')}
        env.update(PATH=f'{self.bin}:{env["PATH"]}', FAKE_STATE=str(self.state),
                   REAL_INSTALL=shutil.which('install'), SYSTEMCTL='systemctl', SS='ss',
                   SFF_LIB_DIR=str(self.lib), SFF_UNIT_DIR=str(self.units),
                   SFF_CONF=str(self.conf), SFF_HAPROXY_CFG=str(self.haproxy_cfg),
                   SFF_WAIT_S='0', **extra)
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
        assert not any('haproxy' in a or 'shadow-tls' in a for a in call[1:]), call


class TestInstall:

    def test_a_root_owned_copy_is_started_with_the_example_config(self, env):
        r = env.run()
        assert r.returncode == 0, r.stderr
        copy = env.lib / 'server_first_front.py'
        assert copy.read_bytes() == FRONT.read_bytes()
        assert oct(copy.stat().st_mode & 0o777) == '0o644'
        assert not (env.lib / 'server_first_front.py.new').exists()
        assert (env.units / 'server-first-front.service').read_bytes() == UNIT.read_bytes()
        assert env.conf.read_bytes() == EXAMPLE.read_bytes()
        assert oct(env.conf.stat().st_mode & 0o777) == '0o644'
        for call in env.calls('install'):
            assert call[call.index('-o') + 1] == 'root' and call[call.index('-g') + 1] == 'root'
        assert [c[1:] for c in env.calls('systemctl')] == [
            ['daemon-reload'], ['enable', 'server-first-front.service'],
            ['restart', 'server-first-front.service'],
            ['show', '-p', 'MainPID', '--value', 'server-first-front.service'],
            ['is-active', 'server-first-front.service'],
            ['show', '-p', 'MainPID', '--value', 'server-first-front.service']]
        assert 'wrote' in r.stdout and 'running' in r.stdout

    def test_an_existing_config_is_never_overwritten(self, env):
        env.conf.write_text('route = 0.0.0.0:3443 127.0.0.1:18443 proxy-v2\n')
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert env.conf.read_text() == 'route = 0.0.0.0:3443 127.0.0.1:18443 proxy-v2\n'
        assert 'wrote' not in r.stdout
        assert 'route 0.0.0.0:3443->127.0.0.1:18443 proxy-v2' in r.stdout

    def test_a_config_that_does_not_parse_installs_nothing(self, env):
        env.conf.write_text('route = localhost:2443 127.0.0.1:18443\n')
        r = env.run()
        assert r.returncode == 1
        assert 'does not parse' in r.stderr and 'IP address' in r.stderr
        assert not env.lib.exists() and env.calls('systemctl') == []

    HAPROXY = ('frontend xray_in\n    bind *:8443\n    bind 127.0.0.1:12443 accept-proxy\n'
               '    mode tcp\n    default_backend nginx_decoy\n')

    def test_it_gives_the_haproxy_line_and_what_nothing_listens_on(self, env):
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert "add 'bind 127.0.0.1:12443 accept-proxy' to frontend xray_in" in r.stdout
        assert '  nothing listens on 127.0.0.1:443 yet' in r.stdout
        # shadow-tls's route is no proxy-v2 one: no haproxy line for it
        assert r.stdout.count('accept-proxy') == 1

    def test_a_bound_and_listening_target_is_not_reported(self, env):
        env.haproxy_cfg.write_text(self.HAPROXY)
        env.set('listen', '127.0.0.1:12443\n0.0.0.0:443\n')
        r = env.run()
        assert r.returncode == 0, r.stderr
        for word in ('nothing listens', 'accept-proxy', 'something else', 'reload'):
            assert word not in r.stdout, word

    def test_a_port_something_else_holds_is_reported(self, env):
        # 18443 on entry: the decoy nginx listens there, haproxy binds nothing
        env.conf.write_text('route = 0.0.0.0:2443 127.0.0.1:18443 proxy-v2\n')
        env.haproxy_cfg.write_text(self.HAPROXY)
        env.set('listen', '127.0.0.1:18443\n')
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert 'something else listens on 127.0.0.1:18443' in r.stdout
        assert "add 'bind 127.0.0.1:18443 accept-proxy'" in r.stdout

    def test_a_bind_line_that_nothing_listens_on_asks_for_a_reload(self, env):
        env.haproxy_cfg.write_text(self.HAPROXY)
        env.set('listen', '0.0.0.0:443\n')
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert 'binds 127.0.0.1:12443, but nothing listens there yet: reload haproxy' in r.stdout
        assert "add 'bind" not in r.stdout

    @pytest.mark.parametrize('line', [
        '    bind 127.0.0.1:12443',                     # no accept-proxy
        '    bind 127.0.0.1:12443 tfo maxconn 100',     # other options, no accept-proxy
        '#   bind 127.0.0.1:12443 accept-proxy',        # commented out
        '    bind 127.0.0.1:124430 accept-proxy',       # another port
    ])
    def test_a_bind_that_is_not_the_accept_proxy_one_does_not_count(self, env, line):
        env.haproxy_cfg.write_text(f'frontend xray_in\n{line}\n')
        env.set('listen', '127.0.0.1:12443\n0.0.0.0:443\n')
        r = env.run()
        assert "add 'bind 127.0.0.1:12443 accept-proxy'" in r.stdout

    def test_a_listener_on_another_address_does_not_count(self, env):
        env.set('listen', '10.0.0.5:443\n')
        r = env.run()
        assert 'nothing listens on 127.0.0.1:443 yet' in r.stdout

    def test_an_active_ufw_is_mentioned(self, env):
        env.set('ufw', 'Status: active\n')
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert 'ufw is active' in r.stdout

    @pytest.mark.parametrize('active, pids', [
        ('activating', '4242 4242'),     # restarting after it could not listen
        ('active', '0 4300'),            # not up at the first look
        ('active', '0 0'),               # never up
        ('active', '4242 4300'),         # died and came back between the looks
    ])
    def test_a_front_that_does_not_stay_up_fails_the_install(self, env, active, pids):
        env.set('active', active)
        env.set('pids', pids)
        r = env.run()
        assert r.returncode == 1
        assert 'does not stay up' in r.stderr and 'journalctl -u server-first-front' in r.stderr

    def test_a_copy_that_does_not_compile_is_not_installed(self, env, tmp_path):
        tree = tmp_path / 'checkout'
        (tree / 'scripts').mkdir(parents=True)
        (tree / 'systemd').mkdir()
        shutil.copy(SCRIPT, tree / 'scripts')
        shutil.copy(UNIT, tree / 'systemd')
        shutil.copy(EXAMPLE, tree / 'systemd')
        (tree / 'scripts' / 'server_first_front.py').write_text('def broken(:\n')
        r = env.run(script=tree / 'scripts' / 'install_server_first_front.sh')
        assert r.returncode != 0
        assert not env.lib.exists() and not env.conf.exists() and env.calls('systemctl') == []

    def test_a_missing_file_is_refused(self, env, tmp_path):
        tree = tmp_path / 'checkout'
        (tree / 'scripts').mkdir(parents=True)
        shutil.copy(SCRIPT, tree / 'scripts')
        shutil.copy(FRONT, tree / 'scripts')
        r = env.run(script=tree / 'scripts' / 'install_server_first_front.sh')
        assert r.returncode == 1 and 'missing:' in r.stderr
        assert env.calls() == []

    def test_only_root(self, env):
        env.set('uid', '1000')
        r = subprocess.run(['bash', str(SCRIPT)],
                           env={k: v for k, v in env.env().items() if k != 'SFF_ALLOW_NONROOT'},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 1 and 'run as root' in r.stderr
        assert env.calls() == []

    def test_an_unknown_argument(self, env):
        r = env.run('--force')
        assert r.returncode == 2 and 'usage' in r.stderr
        assert env.calls() == []


class TestRemove:

    def test_it_removes_what_it_installed_and_keeps_the_config(self, env):
        assert env.run().returncode == 0
        r = env.run('--remove')
        assert r.returncode == 0 and 'removed' in r.stdout
        assert not env.lib.exists()
        assert not (env.units / 'server-first-front.service').exists()
        assert env.conf.exists()
        calls = [c[1:] for c in env.calls('systemctl')]
        assert calls[-2:] == [['disable', '--now', 'server-first-front.service'], ['daemon-reload']]


class TestUnit:

    def unit(self):
        return UNIT.read_text()

    def test_it_runs_the_root_owned_copy_isolated_with_a_checked_config(self):
        text = self.unit()
        copy = '/usr/bin/python3 -I /usr/local/lib/nekovpn-sf-front/server_first_front.py'
        assert re.findall(r'^ExecStart=(.*)$', text, re.M) == [
            f'{copy} --config /etc/nekovpn-sf-front.conf']
        assert re.findall(r'^ExecStartPre=(.*)$', text, re.M) == [
            f'{copy} --config /etc/nekovpn-sf-front.conf --check']
        script = SCRIPT.read_text()
        lib = re.search(r'LIB_DIR="\$\{SFF_LIB_DIR:-([^}]+)\}"', script).group(1)
        conf = re.search(r'CONF="\$\{SFF_CONF:-([^}]+)\}"', script).group(1)
        assert copy.endswith(f'{lib}/server_first_front.py')
        assert conf == '/etc/nekovpn-sf-front.conf'
        assert '/opt/vpn-bot' not in text.split('[Service]')[1]

    def test_it_never_stops_restarting_and_needs_no_privilege(self):
        text = self.unit()
        for line in ('StartLimitIntervalSec=0', 'Restart=always', 'DynamicUser=yes',
                     'NoNewPrivileges=yes', 'CapabilityBoundingSet=', 'AmbientCapabilities=',
                     'ProtectSystem=strict', 'LimitNOFILE=65536'):
            assert re.search(rf'^{re.escape(line)}$', text, re.M), line
        assert text.index('StartLimitIntervalSec=0') < text.index('[Service]')
        assert not re.search(r'^EnvironmentFile=', text, re.M)

    def test_the_example_config_parses(self, tmp_path):
        r = subprocess.run(['python3', '-I', str(FRONT), '--config', str(EXAMPLE), '--check'],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert r.stdout.splitlines() == ['0.0.0.0:2443->127.0.0.1:12443 proxy-v2',
                                         '0.0.0.0:2444->127.0.0.1:443']

    def test_systemd_accepts_it(self, tmp_path):
        if shutil.which('systemd-analyze') is None:
            pytest.skip('systemd-analyze is not installed')
        unit = tmp_path / 'server-first-front.service'
        shutil.copy(UNIT, unit)
        r = subprocess.run(['systemd-analyze', 'verify', '--man=no', str(unit)],
                           capture_output=True, text=True, timeout=60)
        if 'Failed to ' in r.stderr and 'bus' in r.stderr.lower():
            pytest.skip(f'systemd-analyze cannot run here: {r.stderr.strip()[:200]}')
        # The copy does not exist on this machine: that is the only complaint allowed.
        complaints = [line for line in r.stderr.splitlines()
                      if 'server-first-front.service' in line
                      and 'nekovpn-sf-front/server_first_front.py' not in line
                      and 'is not executable' not in line]
        assert not complaints, r.stderr
