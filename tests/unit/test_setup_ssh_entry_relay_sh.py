"""scripts/setup_ssh_entry_relay.sh — the relay on entry in front of ssh-exit
(AGENTS.md §51, the SSH path through entry): haproxy on the host network,
forwarding entry's SSH_ENTRY_PORT to SSH_EXIT_HOST:SSH_EXIT_PORT.

The script runs as root on entry against docker; here it runs for real
(bash, sed, cmp) with ``docker`` / ``ss`` / ``id`` / ``ufw`` replaced by
fakes that keep their state in files. What is pinned:

* the bot's .env is data: no value in it ever runs, only its four keys are
  taken, and anything that is not a port or a host name is refused before
  it reaches haproxy's config;
* the container: the pinned image, the host network, read-only, no
  capabilities, no new privileges; the config it mounts is what
  --print-config prints;
* a new config passes ``haproxy -c`` before the live one moves; an
  unchanged one restarts nothing; a container that does not come back is
  rolled back to the previous config;
* a port someone else holds is refused; the banner through the relay, the
  ufw hint and the line for exit are printed; --remove takes the container.

The relay itself, in front of the sshd of setup_ssh_exit.sh and under the
real prober, runs on the stand in tests/integration/test_ssh_entry_path.py.
"""

import json
import os
import socket
import subprocess
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'setup_ssh_entry_relay.sh'
IMAGE = ('haproxy:2.8-alpine'
         '@sha256:fce63f5dde742cd1f2b883ab39f7d4f9f88788827dd54fcfa864f63b9eec3bc8')
CFG = '/usr/local/etc/haproxy/haproxy.cfg'
BOT_TOKEN = '123456:never-printed-token'

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
    if args[-1] != 'ssh-relay' or not flag('exists'):
        print(f'Error: No such object: {args[-1]}', file=sys.stderr)
        sys.exit(1)
    print(('true' if flag('running') else 'false') + ' '
          + ('true' if flag('restarting') else 'false'))
elif cmd == 'run' and '--rm' in args:
    # haproxy -c on the mounted file
    mount = args[args.index('-v') + 1].rsplit(':', 2)[0]
    n = len(list(st.glob('checked_*.cfg')))
    (st / f'checked_{n}.cfg').write_text(Path(mount).read_text())
    (st / f'checked_{n}.args').write_text(json.dumps(args))
    sys.exit(1 if flag('check_fails') else 0)
elif cmd == 'run' and '-d' in args:
    n = len(list(st.glob('started_*.args')))
    (st / f'started_{n}.args').write_text(json.dumps(args))
    mount = args[args.index('-v') + 1].rsplit(':', 2)[0]
    (st / f'started_{n}.cfg').write_text(Path(mount).read_text())
    (st / 'exists').touch()
    (st / 'running').touch()
    fails = st / 'fail_starts'
    if fails.exists() and int(fails.read_text()) > 0:
        fails.write_text(str(int(fails.read_text()) - 1))
        (st / 'restarting').touch()          # a crash loop
    else:
        (st / 'restarting').unlink(missing_ok=True)
    print('0123abcd')
elif cmd == 'rm':
    for name in ('exists', 'running', 'restarting'):
        (st / name).unlink(missing_ok=True)
elif cmd == 'logs':
    print('fake haproxy log line')
else:
    sys.exit(f'fake docker: unexpected {args}')
'''

# ss -ltnH "( sport = :PORT )": a line when something listens there — the
# relay (running, not crash-looping) or someone else.
FAKE_SS = r'''#!/usr/bin/env python3
import os, sys
from pathlib import Path
st = Path(os.environ['FAKE_STATE'])
ours = (st / 'running').exists() and not (st / 'restarting').exists()
if ours or (st / 'other_listener').exists():
    print('LISTEN 0 4096 0.0.0.0:22022 0.0.0.0:*')
'''

FAKE_ID = r'''#!/bin/sh
cat "$FAKE_STATE/uid" 2>/dev/null || echo 0
'''

FAKE_UFW = r'''#!/bin/sh
echo "ufw $*" >> "$FAKE_STATE/ufw.log"
[ -f "$FAKE_STATE/ufw_status" ] || exit 1
cat "$FAKE_STATE/ufw_status"
'''

# Tripwires: the script never touches the host's power state, firewall,
# services or processes. They come first in PATH, log the attempt and fail;
# the fixture fails the test. Test payloads only ever touch a file in the
# test's own directory (a ";reboot" rebooted the developer's laptop on
# 2026-11-11).
TRIPWIRES = ('reboot', 'shutdown', 'poweroff', 'halt', 'init', 'telinit', 'systemctl',
             'iptables', 'ip6tables', 'nft', 'pkill', 'killall')
FAKE_TRIPWIRE = r'''#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_STATE/tripwire.log"
exit 1
'''


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / 'bin'
        self.state = root / 'state'
        self.dir = root / 'etc-ssh-relay'
        self.env_file = root / 'bot.env'
        for d in (self.bin, self.state):
            d.mkdir()
        for name, body in (('docker', FAKE_DOCKER), ('ss', FAKE_SS), ('id', FAKE_ID),
                           ('ufw', FAKE_UFW), *((t, FAKE_TRIPWIRE) for t in TRIPWIRES)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.write_env()

    def write_env(self, **over):
        values = dict(BOT_TOKEN=BOT_TOKEN, ENTRY_NODE_IP='203.0.113.20',
                      SSH_EXIT_HOST='198.51.100.4', SSH_EXIT_PORT='22022',
                      SSH_ENTRY_PORT='22022', SSH_USER_SECRET='never-printed-secret')
        values.update(over)
        self.env_file.write_text(''.join(f'{k}={v}\n' for k, v in values.items()
                                         if v is not None))

    def touch(self, name, text=None):
        p = self.state / name
        p.write_text(text) if text is not None else p.touch()

    def run(self, *args, **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('SSH_', 'ENTRY_NODE'))}
        env.update(PATH=f'{self.bin}:{env["PATH"]}', FAKE_STATE=str(self.state),
                   SSH_RELAY_DIR=str(self.dir), SSH_RELAY_ENV=str(self.env_file),
                   SSH_RELAY_SETTLE_S='0', **extra)
        return subprocess.run(['bash', str(SCRIPT), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def calls(self, sub=None):
        p = self.state / 'calls.log'
        if not p.exists():
            return []
        out = [json.loads(line) for line in p.read_text().splitlines()]
        return [c for c in out if sub is None or c[:2] == ['docker', sub]]

    def started(self):
        return sorted(self.state.glob('started_*.args'), key=lambda p: int(p.stem.split('_')[1]))

    def start_args(self, n=-1):
        return json.loads(self.started()[n].read_text())

    def checked(self):
        return sorted(self.state.glob('checked_*.cfg'), key=lambda p: int(p.stem.split('_')[1]))

    def live(self):
        return (self.dir / 'haproxy.cfg').read_text()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    tripped = e.state / 'tripwire.log'
    assert not tripped.exists(), f'the script ran {tripped.read_text()!r}'


@pytest.fixture
def set_up(env):
    r = env.run()
    assert r.returncode == 0, r.stderr
    (env.state / 'calls.log').unlink()
    return env


def ok(r):
    assert r.returncode == 0, r.stderr
    return r


# ----------------------------------------------------------------- config ---

class TestConfig:

    def test_the_relay_is_exits_ssh_on_entrys_port(self, env):
        cfg = ok(env.run('--print-config')).stdout
        assert '    bind 0.0.0.0:22022\n' in cfg
        assert '    server exit 198.51.100.4:22022\n' in cfg
        assert '    mode tcp\n' in cfg and '    timeout tunnel 1h\n' in cfg
        # no `log` line anywhere: the relay records no user's address
        assert not [ln for ln in cfg.splitlines() if ln.strip().startswith('log')]
        env.write_env(SSH_ENTRY_PORT='2222', SSH_EXIT_PORT='22', SSH_EXIT_HOST='exit.example.com')
        cfg = ok(env.run('--print-config')).stdout
        assert '    bind 0.0.0.0:2222\n' in cfg and '    server exit exit.example.com:22\n' in cfg

    def test_print_config_needs_no_root_and_starts_nothing(self, env):
        env.touch('uid', '1000\n')
        ok(env.run('--print-config'))
        assert env.calls() == []

    def test_exit_port_defaults_to_ssh_exits(self, env):
        env.write_env(SSH_EXIT_PORT=None)
        assert '    server exit 198.51.100.4:22022\n' in ok(env.run('--print-config')).stdout

    def test_leading_zeros_are_one_number(self, env):
        env.write_env(SSH_ENTRY_PORT='022022', SSH_EXIT_PORT='00022')
        r = env.run('--print-config')
        assert r.returncode != 0           # six digits: not a port
        env.write_env(SSH_ENTRY_PORT='02222', SSH_EXIT_PORT='0022')
        cfg = ok(env.run('--print-config')).stdout
        assert 'bind 0.0.0.0:2222\n' in cfg and 'server exit 198.51.100.4:22\n' in cfg


class TestTheEnvFileIsData:

    def test_no_value_runs(self, env):
        # Sourced, each of these would run as root on entry. The payloads
        # only touch a file in the test's own directory, on purpose: never
        # put a real command into such a test.
        marker = env.state / 'executed'
        for key, value in (('SSH_EXIT_HOST', f'198.51.100.4;touch {marker}'),
                           ('SSH_EXIT_HOST', f'$(touch {marker})'),
                           ('SSH_ENTRY_PORT', f'`touch {marker}`'),
                           ('SSH_EXIT_PORT', f'22022$(touch {marker})'),
                           ('ENTRY_NODE_IP', f'$(touch {marker})')):
            env.write_env(**{key: value})
            r = env.run()
            assert not marker.exists(), (key, value)
            if key != 'ENTRY_NODE_IP':
                assert r.returncode != 0 and 'must be' in r.stderr, (key, r.stderr)
                assert env.calls('run') == []
        env.write_env(ENTRY_NODE_IP=f'$(touch {marker})')
        r = ok(env.run())
        assert "SSH_RELAY_IPS=<entry's address>" in r.stdout

    def test_only_its_keys_and_no_secret_is_printed(self, env):
        elsewhere = env.root / 'elsewhere'
        with open(env.env_file, 'a') as f:
            f.write(f'NAME=not-ssh-relay\nIMAGE=other:latest\nDIR={elsewhere}\n'
                    f'CFG={elsewhere}/cfg\nBOT_ENV={elsewhere}/env\n')
        r = ok(env.run())
        assert not elsewhere.exists()
        args = env.start_args()
        assert args[args.index('--name') + 1] == 'ssh-relay' and args[-1] == IMAGE
        assert args[args.index('-v') + 1] == f'{env.dir}/haproxy.cfg:{CFG}:ro'
        out = r.stdout + r.stderr + (env.state / 'calls.log').read_text()
        assert BOT_TOKEN not in out and 'never-printed-secret' not in out

    def test_quotes_export_crlf_and_the_last_line(self, env):
        env.env_file.write_bytes(
            b'# bot\n'
            b'export SSH_EXIT_HOST="198.51.100.9"\n'
            b"SSH_ENTRY_PORT='2222'\n"
            b'  SSH_ENTRY_PORT=1\n'                 # indented: not a KEY=VALUE line
            b'SSH_EXIT_PORT=22\r')                  # edited on Windows, no final newline
        cfg = ok(env.run('--print-config')).stdout
        assert 'bind 0.0.0.0:2222\n' in cfg and 'server exit 198.51.100.9:22\n' in cfg

    def test_the_environment_is_no_source(self, env):
        env.write_env(SSH_ENTRY_PORT=None)
        r = env.run('--print-config', SSH_ENTRY_PORT='2222')
        assert r.returncode != 0 and 'SSH_ENTRY_PORT is not set' in r.stderr

    @pytest.mark.parametrize('key,value,said', [
        ('SSH_ENTRY_PORT', None, 'SSH_ENTRY_PORT is not set'),
        ('SSH_ENTRY_PORT', '', 'SSH_ENTRY_PORT is not set'),
        ('SSH_ENTRY_PORT', '1023', 'SSH_ENTRY_PORT must be'),
        ('SSH_ENTRY_PORT', '65536', 'SSH_ENTRY_PORT must be'),
        ('SSH_ENTRY_PORT', '22 22', 'SSH_ENTRY_PORT must be'),
        ('SSH_EXIT_PORT', '0', 'SSH_EXIT_PORT must be'),
        ('SSH_EXIT_PORT', '70000', 'SSH_EXIT_PORT must be'),
        ('SSH_EXIT_HOST', None, 'SSH_EXIT_HOST is not set'),
        ('SSH_EXIT_HOST', '2001:db8::1', 'SSH_EXIT_HOST must be'),
        ('SSH_EXIT_HOST', 'exit host', 'SSH_EXIT_HOST must be'),
        ('SSH_EXIT_HOST', '-exit', 'SSH_EXIT_HOST must be'),
        ('SSH_EXIT_HOST', 'exit.example.com.', 'SSH_EXIT_HOST must be'),
    ])
    def test_refused_before_anything_runs(self, env, key, value, said):
        env.write_env(**{key: value})
        r = env.run()
        assert r.returncode != 0 and said in r.stderr, r.stderr
        assert env.calls() == []

    def test_the_bounds_are_ports(self, env):
        env.write_env(SSH_ENTRY_PORT='1024', SSH_EXIT_PORT='1')
        ok(env.run('--print-config'))
        env.write_env(SSH_ENTRY_PORT='65535', SSH_EXIT_PORT='65535')
        ok(env.run('--print-config'))

    def test_a_missing_file(self, env):
        env.env_file.unlink()
        r = env.run()
        assert r.returncode != 0 and 'missing' in r.stderr and env.calls() == []


# -------------------------------------------------------------- container ---

class TestSetup:

    def test_root_only(self, env):
        env.touch('uid', '1000\n')
        r = env.run()
        assert r.returncode != 0 and 'run as root' in r.stderr and env.calls() == []

    def test_the_container_and_its_flags(self, env):
        r = ok(env.run())
        assert 'ssh-relay on :22022 -> 198.51.100.4:22022' in r.stdout
        args = env.start_args()
        assert args[:2] == ['run', '-d']
        for pair in (['--name', 'ssh-relay'], ['--restart', 'unless-stopped'],
                     ['--network', 'host'], ['--cap-drop', 'ALL'],
                     ['--security-opt', 'no-new-privileges'], ['--memory', '128m'],
                     ['--pids-limit', '64']):
            i = args.index(pair[0])
            assert args[i:i + 2] == pair, pair
        assert '--read-only' in args and args[-1] == IMAGE
        assert '-p' not in args and '--privileged' not in args and '--cap-add' not in args
        assert env.live() == ok(env.run('--print-config')).stdout
        assert (env.dir.stat().st_mode & 0o777) == 0o700
        # read by the image's own user, whatever root's umask
        assert ((env.dir / 'haproxy.cfg').stat().st_mode & 0o777) == 0o644
        # the config passed haproxy -c before the container saw it
        calls = env.calls()
        check = next(i for i, c in enumerate(calls) if c[:3] == ['docker', 'run', '--rm'])
        start = next(i for i, c in enumerate(calls) if c[:3] == ['docker', 'run', '-d'])
        assert check < start
        assert calls[check][-6:] == [IMAGE, 'haproxy', '-c', '-q', '-f', CFG]
        assert env.checked()[0].read_text() == env.live()

    def test_an_unchanged_config_restarts_nothing(self, set_up):
        r = ok(set_up.run())
        assert 'ssh-relay unchanged' in r.stdout
        assert set_up.calls('run') == [] and set_up.calls('rm') == []
        assert len(set_up.started()) == 1

    def test_a_stopped_relay_is_started_again(self, set_up):
        (set_up.state / 'running').unlink()
        ok(set_up.run())
        assert len(set_up.started()) == 2

    def test_a_crash_looping_relay_is_started_again(self, set_up):
        # docker says Running for a container in a restart loop; that is no
        # relay, whatever the config: setup tries again
        set_up.touch('restarting')
        r = ok(set_up.run())
        assert 'unchanged' not in r.stdout and len(set_up.started()) == 2
        assert not (set_up.state / 'restarting').exists()

    def test_a_new_port_is_a_new_container_and_the_old_config_is_kept(self, set_up):
        before = set_up.live()
        set_up.write_env(SSH_ENTRY_PORT='2222')
        ok(set_up.run())
        assert len(set_up.started()) == 2
        assert 'bind 0.0.0.0:2222' in set_up.live()
        assert (set_up.dir / 'haproxy.cfg.prev').read_text() == before

    def test_a_config_haproxy_refuses_leaves_the_live_one(self, set_up):
        before = set_up.live()
        set_up.touch('check_fails')
        set_up.write_env(SSH_ENTRY_PORT='2222')
        r = set_up.run()
        assert r.returncode != 0 and 'haproxy -c failed' in r.stderr
        assert set_up.live() == before and not (set_up.dir / 'haproxy.cfg.new').exists()
        assert set_up.calls('run')[-1][:3] == ['docker', 'run', '--rm']
        assert len(set_up.started()) == 1

    def test_a_relay_that_does_not_come_back_is_rolled_back(self, set_up):
        before = set_up.live()
        set_up.touch('fail_starts', '1')
        set_up.write_env(SSH_ENTRY_PORT='2222')
        r = set_up.run()
        assert r.returncode != 0 and 'rolled back to the previous one' in r.stderr
        assert set_up.live() == before
        cfgs = [p.read_text() for p in sorted(set_up.state.glob('started_*.cfg'),
                                             key=lambda p: int(p.stem.split('_')[1]))]
        assert 'bind 0.0.0.0:2222' in cfgs[1] and cfgs[2] == before
        assert 'fake haproxy log line' in r.stderr

    def test_a_first_relay_that_does_not_come_up(self, env):
        env.touch('fail_starts', '5')
        r = env.run()
        assert r.returncode != 0 and 'ssh-relay is not running' in r.stderr
        assert 'rolled back' not in r.stderr and len(env.started()) == 1

    def test_a_port_someone_else_holds(self, env):
        env.touch('other_listener')
        r = env.run()
        assert r.returncode != 0 and 'already taken by something other than ssh-relay' in r.stderr
        assert env.calls('run') == []

    def test_its_own_port_is_no_conflict(self, set_up):
        ok(set_up.run())                      # it listens there itself


class TestWhatItSays:

    def test_the_banner_through_the_relay(self, env):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        if port < 1024:
            pytest.skip('no unprivileged port')

        def serve():
            conn, _ = srv.accept()
            conn.sendall(b'SSH-2.0-OpenSSH_9.7\r\n')
            conn.close()
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        env.write_env(SSH_ENTRY_PORT=str(port))
        r = ok(env.run())
        t.join(5)
        srv.close()
        assert 'through the relay: SSH-2.0-OpenSSH_9.7\n' in r.stdout

    def test_no_banner_is_a_warning_not_a_failure(self, env):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]          # closed again: nothing listens
        env.write_env(SSH_ENTRY_PORT=str(max(port, 1024)))
        r = ok(env.run())
        assert 'WARNING: no SSH banner through the relay' in r.stderr

    def test_the_ufw_hint(self, env):
        env.touch('ufw_status', 'Status: active\n\nTo   Action  From\n22/tcp  ALLOW  Anywhere\n')
        r = ok(env.run())
        assert 'ufw allow 22022/tcp' in r.stdout
        env.touch('ufw_status', 'Status: active\n\n22022/tcp                  ALLOW       Anywhere\n')
        assert 'ufw allow' not in ok(env.run()).stdout
        env.touch('ufw_status', 'Status: inactive\n')
        assert 'ufw allow' not in ok(env.run()).stdout
        # only ever asked, never changed
        assert {line.split()[1] for line in (env.state / 'ufw.log').read_text().splitlines()} \
            == {'status'}

    def test_the_line_for_exit(self, env):
        r = ok(env.run())
        assert 'SSH_RELAY_IPS=203.0.113.20' in r.stdout
        assert 'SSH_ENTRY_PORT=22022' in r.stdout
        env.write_env(ENTRY_NODE_IP=None)
        assert "SSH_RELAY_IPS=<entry's address>" in ok(env.run()).stdout


class TestRemove:

    def test_takes_the_container_and_says_what_is_left(self, set_up):
        r = ok(set_up.run('--remove'))
        assert set_up.calls('rm') == [['docker', 'rm', '-f', 'ssh-relay']]
        assert not (set_up.state / 'exists').exists()
        assert 'SSH_ENTRY_PORT is still set' in r.stdout
        set_up.write_env(SSH_ENTRY_PORT=None)
        assert 'still set' not in ok(set_up.run('--remove')).stdout

    def test_root_only(self, set_up):
        set_up.touch('uid', '1000\n')
        r = set_up.run('--remove')
        assert r.returncode != 0 and set_up.calls('rm') == []

    def test_unknown_flag(self, env):
        r = env.run('--sync')
        assert r.returncode == 2 and 'usage' in r.stderr and env.calls() == []
