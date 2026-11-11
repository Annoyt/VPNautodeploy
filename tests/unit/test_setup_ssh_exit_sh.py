"""scripts/setup_ssh_exit.sh — the SSH emergency exit on exit (IMPROVEMENT_PLAN
F, E4; bot/services/ssh_exit.py): an alpine sshd that only forwards, its users
from the bot's ``GET /api/ssh/users`` every 2 minutes (``--sync``).

The script runs as root on exit against docker, systemd and the bot; here it
runs for real (bash, python3, sed, install) with ``docker`` / ``curl`` /
``systemctl`` replaced by fakes that keep their state in files, and
``install`` by a wrapper that only drops the owner a test cannot set. What is
pinned: /etc/ssh-exit.env is data — no value in it ever runs, only its three
keys are taken, quotes / ``export`` / CRLF / a missing final newline are fine,
and the caller's environment is no source for them — and the flows around it
are as before: setup writes what --print-config / --print-entrypoint print,
the timer runs a root-owned copy, --sync applies the bot's list, leaves the
users alone when the bot does not answer, and does not apply a list that
suddenly reads empty unless forced from the command line. One test feeds the
sync the body the real endpoint answers on a real sqlite.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'setup_ssh_exit.sh'
TOKEN = 'tok-123'
N1 = 'n0123456789ab'
N2 = 'nfedcba987654'
HASH = '$6$saltsalt$' + 'A' * 86     # the shape the sync accepts

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
    if args[-1] != 'ssh-exit' or not flag('exists'):
        print(f'Error: No such object: {args[-1]}', file=sys.stderr)
        sys.exit(1)
    print('true' if flag('running') else 'false')
elif cmd == 'build':
    (st / 'dockerfile').write_text(sys.stdin.read())
elif cmd == 'run' and '--rm' in args:
    if 'ssh-keygen' not in args:
        sys.exit(f'fake docker: unexpected run {args}')
    keys = Path(args[args.index('-v') + 1].split(':')[0])
    (keys / 'ssh_host_ed25519_key').write_text('fake private host key\n')
    (keys / 'ssh_host_ed25519_key.pub').write_text('ssh-ed25519 AAAAC3NzaFAKE root@fake\n')
elif cmd == 'run' and '-d' in args:
    n = len(list(st.glob('started_*.args')))
    (st / f'started_{n}.args').write_text(json.dumps(args))
    (st / 'exists').touch()
    (st / 'running').touch()
    print('0123abcd')
elif cmd == 'rm':
    (st / 'exists').unlink(missing_ok=True)
    (st / 'running').unlink(missing_ok=True)
elif cmd == 'exec' and '-i' in args:
    # the user update inside the container: the plan it was given
    plan = sys.stdin.read()
    n = len(list(st.glob('applied_*.plan')))
    (st / f'applied_{n}.plan').write_text(plan)
    names = [line.split('\t')[0] for line in plan.splitlines() if line.strip()]
    (st / 'container_users').write_text(''.join(name + '\n' for name in names))
elif cmd == 'exec':
    # grep -c of the n<12 hex> accounts in the container's /etc/passwd
    p = st / 'container_users'
    print(len(p.read_text().split()) if p.exists() else 0)
else:
    sys.exit(f'fake docker: unexpected {args}')
'''

FAKE_CURL = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
st = Path(os.environ['FAKE_STATE'])
args = sys.argv[1:]
with open(st / 'calls.log', 'a') as f:
    f.write(json.dumps(['curl'] + args) + '\n')
if (st / 'curl_fail').exists():
    print('curl: (7) Failed to connect', file=sys.stderr)
    sys.exit(7)
sys.stdout.write((st / 'answer').read_text())
'''

FAKE_SYSTEMCTL = r'''#!/bin/sh
echo "systemctl $*" >> "$FAKE_STATE/calls.log"
'''

# The real install, minus -o/-g: a test does not run as root. The call is
# logged with the owner it asked for.
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

# Tripwires: nothing the script runs may reach the machine's power state, or
# manage users and processes on the host (that happens inside the container,
# through `docker exec`). They come first in PATH, log the attempt and fail;
# the fixture fails the test. A ";reboot" in a test env file rebooted the
# developer's laptop on 2026-11-11 (a desktop session may reboot without
# sudo): test payloads only ever touch a file in the test's own directory.
TRIPWIRES = ('reboot', 'shutdown', 'poweroff', 'halt', 'init', 'telinit',
             'pkill', 'killall', 'adduser', 'deluser', 'useradd', 'userdel',
             'chpasswd', 'ufw', 'iptables')
FAKE_TRIPWIRE = r'''#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_STATE/tripwire.log"
exit 1
'''


def answer(users, *, ok=True):
    return json.dumps({'ok': ok, 'users': [{'user': u, 'hash': HASH} for u in users]})


def plan(*users):
    return ''.join(f'{u}\t{HASH}\n' for u in users)


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / 'bin'
        self.state = root / 'state'
        self.dir = root / 'etc-ssh-exit'
        self.env_file = root / 'ssh-exit.env'
        self.units = root / 'units'
        self.sbin = root / 'sbin'
        self.copy = self.sbin / 'ssh-exit-sync'
        for d in (self.bin, self.state, self.units, self.sbin):
            d.mkdir()
        for name, body in (('docker', FAKE_DOCKER), ('curl', FAKE_CURL),
                           ('systemctl', FAKE_SYSTEMCTL), ('install', FAKE_INSTALL),
                           *((t, FAKE_TRIPWIRE) for t in TRIPWIRES)):
            p = self.bin / name
            p.write_text(body)
            p.chmod(0o755)
        self.write_env()
        self.users([N1])

    def write_env(self, **over):
        values = dict(BOT_URL='https://dash.example.com:9443', DPI_REPORT_TOKEN=TOKEN)
        values.update(over)
        self.env_file.write_text(''.join(f'{k}={v}\n' for k, v in values.items() if v is not None))

    def users(self, users, **kw):
        self.touch('answer', answer(users, **kw))

    def touch(self, name, text=None):
        p = self.state / name
        p.write_text(text) if text is not None else p.touch()

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith('SSH_EXIT_') or k in ('BOT_URL', 'DPI_REPORT_TOKEN', 'FORCE'))}
        env.update(PATH=f'{self.bin}:{env["PATH"]}', FAKE_STATE=str(self.state),
                   REAL_INSTALL=shutil.which('install'),
                   SSH_EXIT_DIR=str(self.dir), SSH_EXIT_ENV=str(self.env_file),
                   SSH_EXIT_UNIT_DIR=str(self.units), SSH_EXIT_BIN=str(self.copy), **extra)
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

    def curl_calls(self):
        return [c for c in self.calls() if c[0] == 'curl']

    def reset_calls(self):
        (self.state / 'calls.log').unlink(missing_ok=True)

    def start_args(self, n=-1):
        args = sorted(self.state.glob('started_*.args'), key=lambda p: int(p.stem.split('_')[1]))
        return json.loads(args[n].read_text())

    def applied(self):
        plans = sorted(self.state.glob('applied_*.plan'), key=lambda p: int(p.stem.split('_')[1]))
        return [p.read_text() for p in plans]


@pytest.fixture
def env(tmp_path):
    if shutil.which('install') is None:
        pytest.skip('install (coreutils) is not installed')
    e = Env(tmp_path)
    yield e
    tripped = e.state / 'tripwire.log'
    assert not tripped.exists(), f'the script ran {tripped.read_text()!r}'
    power = [c for c in e.calls() if c[:1] == ['systemctl']
             and set(c[1:]) & {'reboot', 'poweroff', 'halt', 'kexec', 'suspend', 'hibernate'}]
    assert not power, power


@pytest.fixture
def set_up(env):
    r = env.run()
    assert r.returncode == 0, r.stderr
    env.reset_calls()
    return env


class TestTheEnvFileIsData:

    def test_no_value_runs(self, env):
        # Sourced (as before), each of these ran as root on exit. The payloads
        # only touch a file in the test's own directory, on purpose: never put
        # a real command into such a test.
        marker = env.state / 'executed'
        url = f'https://dash.example.com:9443/a\\b$(touch {marker})'
        token = f'tok;touch {marker}'
        port = f'22022`touch {marker}`'
        env.write_env(BOT_URL=url, DPI_REPORT_TOKEN=token, SSH_EXIT_PORT=port)
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert not marker.exists()
        # the values reach the tools as text, byte for byte
        curl = env.curl_calls()
        assert len(curl) == 1                       # setup's sync, through the copy
        assert curl[0][-1] == url + '/api/ssh/users'
        assert f'X-DPI-Token: {token}' in curl[0]
        assert f'{port}:22' in env.start_args()
        assert f'ufw allow {port}/tcp' in r.stdout
        r = env.run('--sync')
        assert r.returncode == 0, r.stderr
        assert not marker.exists()
        assert env.curl_calls()[-1][-1] == url + '/api/ssh/users'

    def test_only_its_three_keys(self, env):
        # Sourced, the file could rename the container, open SMTP through the
        # forwarding or move the config and the copy the timer runs.
        elsewhere = env.root / 'elsewhere'
        with open(env.env_file, 'a') as f:
            f.write(f'NAME=not-ssh-exit\nIMAGE=other:latest\nPERMIT_OPEN=*:25\n'
                    f'CONF_DIR={elsewhere}\nSYNC_BIN={elsewhere}-bin\n')
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert not elsewhere.exists() and not Path(f'{elsewhere}-bin').exists()
        sshd_config = (env.dir / 'sshd_config').read_text()
        assert sshd_config == env.run('--print-config').stdout
        permit = next(ln for ln in sshd_config.splitlines() if ln.startswith('PermitOpen '))
        assert '*:25' not in permit.split()
        args = env.start_args()
        assert args[args.index('--name') + 1] == 'ssh-exit' and args[-1] == 'ssh-exit:local'
        assert env.copy.exists() and env.applied() == [plan(N1)]

    def test_force_is_no_key_of_the_file(self, set_up):
        # FORCE=1 on the command line applies a list that suddenly reads
        # empty; written into the env file it does nothing
        set_up.users([])
        with open(set_up.env_file, 'a') as f:
            f.write('FORCE=1\n')
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert 'not applied' in r.stdout
        assert set_up.applied() == [plan(N1)]       # setup's, nothing since

    def test_quotes_export_crlf_and_the_last_line(self, env):
        env.env_file.write_bytes(
            b'# ssh-exit\n'
            b'export BOT_URL="https://dash.example.com:9443/"\n'
            b'SSH_EXIT_PORT=23000\n'
            b'  SSH_EXIT_PORT=1\n'                  # indented: not a KEY=VALUE line
            b"DPI_REPORT_TOKEN='tok-123'\r")        # edited on Windows, no final newline
        r = env.run()
        assert r.returncode == 0, r.stderr
        curl = env.curl_calls()[-1]
        assert curl[-1] == 'https://dash.example.com:9443/api/ssh/users'
        assert f'X-DPI-Token: {TOKEN}' in curl
        assert '23000:22' in env.start_args()
        assert 'ufw allow 23000/tcp' in r.stdout

    def test_the_environment_is_no_source(self, env):
        # the file is the only source of its keys: one it leaves out is not
        # filled in from the caller's environment
        env.write_env(SSH_EXIT_PORT=None)
        r = env.run(SSH_EXIT_PORT='23000', BOT_URL='https://elsewhere.example.com')
        assert r.returncode == 0, r.stderr
        assert '22022:22' in env.start_args()
        assert env.curl_calls()[-1][-1] == 'https://dash.example.com:9443/api/ssh/users'
        env.write_env(DPI_REPORT_TOKEN=None)
        r = env.run(DPI_REPORT_TOKEN='from-the-environment')
        assert r.returncode != 0 and 'DPI_REPORT_TOKEN not set' in r.stderr

    @pytest.mark.parametrize('args', [(), ('--sync',)])
    def test_missing_env_file(self, env, args):
        env.env_file.unlink()
        r = env.run(*args)
        assert r.returncode == 2 and 'missing' in r.stderr
        assert not env.calls()

    @pytest.mark.parametrize('over,message', [
        (dict(BOT_URL=None), 'BOT_URL not set'),
        (dict(BOT_URL=''), 'BOT_URL not set'),
        (dict(DPI_REPORT_TOKEN=None), 'DPI_REPORT_TOKEN not set'),
    ])
    def test_missing_keys(self, env, over, message):
        env.write_env(**over)
        r = env.run()
        assert r.returncode != 0 and message in r.stderr
        assert not env.calls()


class TestSetup:

    def test_fresh(self, env):
        r = env.run()
        assert r.returncode == 0, r.stderr
        assert (env.dir / 'sshd_config').read_text() == env.run('--print-config').stdout
        entrypoint = env.dir / 'entrypoint.sh'
        assert entrypoint.read_text() == env.run('--print-entrypoint').stdout
        assert entrypoint.stat().st_mode & 0o777 == 0o755
        assert env.dir.stat().st_mode & 0o777 == 0o700
        assert (env.dir / 'keys').stat().st_mode & 0o777 == 0o700
        assert 'FROM alpine:3.20@sha256:' in (env.state / 'dockerfile').read_text()
        args = env.start_args()
        joined = ' '.join(args)
        for part in ('--name ssh-exit', '--restart unless-stopped', '-p 22022:22',
                     '--cap-add NET_ADMIN', '--memory 128m', '--pids-limit 256',
                     f'-v {env.dir}/sshd_config:/etc/ssh/sshd_config:ro',
                     f'-v {env.dir}/keys:/etc/ssh/keys:ro',
                     f'-v {env.dir}/entrypoint.sh:/entrypoint.sh:ro',
                     f'-v {env.dir}/users.plan:/etc/ssh/users.plan:ro'):
            assert part in joined, part
        assert args[-1] == 'ssh-exit:local'
        # the timer runs a root-owned copy; setup ran it once
        assert env.copy.read_bytes() == SCRIPT.read_bytes()
        assert ['install', '-m', '755', '-o', 'root', '-g', 'root',
                str(SCRIPT), str(env.copy)] in env.calls()
        assert f'ExecStart={env.copy} --sync' in (env.units / 'ssh-exit-sync.service').read_text()
        assert 'OnUnitActiveSec=2min' in (env.units / 'ssh-exit-sync.timer').read_text()
        assert ['systemctl', 'enable', '--now', 'ssh-exit-sync.timer'] in env.calls()
        assert env.applied() == [plan(N1)]
        users_plan = env.dir / 'users.plan'
        assert users_plan.read_text() == plan(N1)
        assert users_plan.stat().st_mode & 0o777 == 0o600
        assert 'SSH_HOST_KEY=ssh-ed25519 AAAAC3NzaFAKE' in r.stdout
        assert 'ufw allow 22022/tcp' in r.stdout

    def test_rerun_keeps_the_host_key(self, set_up):
        (set_up.dir / 'keys' / 'ssh_host_ed25519_key.pub').write_text('ssh-ed25519 KEPT x\n')
        r = set_up.run()
        assert r.returncode == 0, r.stderr
        assert not [c for c in set_up.docker_calls('run') if 'ssh-keygen' in c]
        assert 'SSH_HOST_KEY=ssh-ed25519 KEPT' in r.stdout

    def test_setup_from_the_copy_itself(self, set_up):
        r = set_up.run(script=set_up.copy)
        assert r.returncode == 0, r.stderr
        assert not [c for c in set_up.calls() if c[0] == 'install' and str(set_up.copy) in c]

    def test_remove(self, set_up):
        r = set_up.run('--remove')
        assert r.returncode == 0, r.stderr
        assert ['systemctl', 'disable', '--now', 'ssh-exit-sync.timer'] in set_up.calls()
        assert not (set_up.state / 'exists').exists()
        assert (set_up.dir / 'keys' / 'ssh_host_ed25519_key').exists()


class TestSync:

    def test_applies_the_bots_list(self, set_up):
        set_up.users([N1, N2])
        r = set_up.run('--sync')
        assert r.returncode == 0, r.stderr
        assert set_up.applied()[-1] == plan(N1, N2)
        assert (set_up.dir / 'users.plan').read_text() == plan(N1, N2)
        assert 'users synced (2)' in r.stdout

    def test_malformed_entries_are_skipped(self, set_up):
        set_up.touch('answer', json.dumps({'ok': True, 'users': [
            {'user': N2, 'hash': HASH}, {'user': 'root', 'hash': HASH},
            {'user': 'n0123', 'hash': HASH}, {'user': N1, 'hash': 'plain-password'}]}))
        assert set_up.run('--sync').returncode == 0
        assert set_up.applied()[-1] == plan(N2)

    def test_not_running_is_skipped(self, set_up):
        (set_up.state / 'running').unlink()
        r = set_up.run('--sync')
        assert r.returncode == 0 and 'not running' in r.stdout
        assert not set_up.curl_calls()

    def test_an_unreachable_bot_leaves_the_users(self, set_up):
        set_up.touch('curl_fail')
        r = set_up.run('--sync')
        assert r.returncode == 0 and 'users unchanged' in r.stdout
        assert set_up.applied() == [plan(N1)]
        assert (set_up.dir / 'users.plan').read_text() == plan(N1)

    def test_a_refusal_leaves_the_users(self, set_up):
        set_up.users([N1, N2], ok=False)
        r = set_up.run('--sync')
        assert r.returncode == 0 and 'no list' in r.stdout
        assert set_up.applied() == [plan(N1)]

    def test_an_empty_list_waits_for_force(self, set_up):
        set_up.users([])
        r = set_up.run('--sync')
        assert r.returncode == 0 and 'not applied' in r.stdout
        assert set_up.applied() == [plan(N1)]
        r = set_up.run('--sync', FORCE='1')
        assert r.returncode == 0, r.stderr
        assert set_up.applied()[-1] == '\n'
        assert (set_up.dir / 'users.plan').read_text() == '\n'


def test_usage(env):
    r = env.run('--bogus')
    assert r.returncode == 2 and 'usage' in r.stderr


def test_prod_runs_the_paths_it_always_had():
    # the SSH_EXIT_* overrides exist for this file only; unset, the script
    # uses the paths it had as constants (the timer's environment is empty)
    lines = SCRIPT.read_text().splitlines()
    for line in ('CONF_DIR="${SSH_EXIT_DIR:-/etc/ssh-exit}"',
                 'ENV_FILE="${SSH_EXIT_ENV:-/etc/ssh-exit.env}"',
                 'UNIT_DIR="${SSH_EXIT_UNIT_DIR:-/etc/systemd/system}"',
                 'SYNC_BIN="${SSH_EXIT_BIN:-/usr/local/sbin/ssh-exit-sync}"'):
        assert line in lines, line


# ------------------------------------------------- the bot's own answer ---

@pytest.mark.filterwarnings("ignore:Database\\..*is deprecated:DeprecationWarning")
@pytest.mark.asyncio
async def test_the_bots_own_answer_syncs(set_up, tmp_path):
    """The body GET /api/ssh/users answers on a real sqlite passes the sync."""
    from bot.core.database import Database
    from bot.core.web_server import WebAppServer
    from bot.services import ssh_exit
    paid = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
    db = Database(str(tmp_path / 'bot.db'))
    with db._connect() as conn:
        conn.executemany("INSERT INTO users (chat_id, username, status, uuid) VALUES (?,?,?,?)",
                         [('1', 'a', 'paid', paid),
                          ('2', 'b', 'demo', '0f9e8d7c-6b5a-4c3d-8e2f-1a0b9c8d7e6f')])
        conn.commit()
    config = SimpleNamespace(DPI_REPORT_TOKEN=TOKEN, SSH_EXIT_HOST='198.51.100.4',
                             SSH_EXIT_PORT='22022', SSH_HOST_KEY='ssh-ed25519 AAAAC3Nza',
                             SSH_USER_SECRET='s3cret', WEBAPP_URL='https://dash.example.com',
                             BOT_TOKEN='t')
    request = Mock()
    request.headers = {'X-DPI-Token': TOKEN}
    resp = await WebAppServer(config, db, xui_service=Mock()).handle_ssh_users(request)
    assert resp.status == 200
    set_up.touch('answer', resp.text)
    r = set_up.run('--sync')
    assert r.returncode == 0, r.stderr
    name = ssh_exit.username_for(paid)
    [line] = set_up.applied()[-1].splitlines()
    got_name, got_hash = line.split('\t')
    assert got_name == name and got_hash.startswith('$6$')
    assert ssh_exit.sha512_crypt(ssh_exit.password_for(config, paid),
                                 got_hash.split('$')[2]) == got_hash
