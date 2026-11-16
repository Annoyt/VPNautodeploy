"""scripts/install_service_backup.sh and systemd/nekovpn-backup.{service,timer}:
the daily service backup's installer on entry (AGENTS.md §58).

The installer runs for real (bash, grep, mktemp, install) with ``systemctl``,
``docker`` and ``id`` replaced by fakes that log, ``install`` by a wrapper that
only drops the owner a test cannot set, and power / firewall / process
commands as tripwires first in PATH. Pinned: only an age PUBLIC key is ever
written (a secret key is refused and never echoed), an installed key is
replaced only with --force, the timer runs a root-owned copy, the bot is told
the timer is installed / removed, and nothing is installed when a check fails.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'install_service_backup.sh'
SERVICE = REPO / 'systemd' / 'nekovpn-backup.service'
TIMER = REPO / 'systemd' / 'nekovpn-backup.timer'
KEY = 'age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p'
KEY2 = 'age18kpa37v26zpe3t0hzaznfwur8g9e95hcl6awzta9yue7ard3e5cqjtk48j'
SECRET = 'AGE-SECRET-KEY-1GFPYYSJZGFPYYSJZGFPYYSJZGFPYYSJZGFPYYSJZGFPYYSJZGFPQ4EGAEX'

FAKE_LOG = r'''#!/usr/bin/env python3
import json, os, sys
st = os.environ['FAKE_STATE']
name = os.path.basename(sys.argv[0])
with open(os.path.join(st, 'calls.log'), 'a') as f:
    f.write(json.dumps([name] + sys.argv[1:]) + '\n')
if name == 'docker':
    args = sys.argv[1:]
    if os.path.exists(os.path.join(st, 'docker_down')):
        sys.exit(1)
    if args[:4] == ['exec', 'vpn-bot', 'test', '-f']:
        sys.exit(1 if os.path.exists(os.path.join(st, 'no_sender')) else 0)
    if args[:3] == ['exec', 'vpn-bot', 'python3'] and os.path.exists(os.path.join(st, 'python_fails')):
        sys.exit(1)
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

FAKE_ID = '#!/bin/sh\n[ "$1" = "-u" ] && { cat "$FAKE_STATE/uid" 2>/dev/null || echo 0; exit 0; }\nexec /usr/bin/id "$@"\n'

TRIPWIRES = ('reboot', 'shutdown', 'poweroff', 'halt', 'init', 'telinit', 'kill', 'pkill',
             'killall', 'iptables', 'nft', 'ufw', 'useradd', 'userdel', 'curl', 'ssh', 'apt-get',
             'apt')
FAKE_TRIPWIRE = '#!/bin/sh\necho "$(basename "$0") $*" >> "$FAKE_STATE/tripwire.log"\nexit 1\n'


class Env:
    def __init__(self, root: Path):
        self.root = root
        self.bin = root / 'bin'
        self.state = root / 'state'
        self.lib = root / 'lib' / 'nekovpn-backup'
        self.units = root / 'units'
        self.conf = root / 'etc' / 'nekovpn-backup'
        for d in (self.bin, self.state, self.units):
            d.mkdir(parents=True)
        for name, body in (('systemctl', FAKE_LOG), ('docker', FAKE_LOG), ('age', FAKE_LOG),
                           ('zstd', FAKE_LOG), ('install', FAKE_INSTALL), ('id', FAKE_ID),
                           *((t, FAKE_TRIPWIRE) for t in TRIPWIRES)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    @property
    def rcpt(self):
        return self.conf / 'recipients.txt'

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith('BACKUP_')}
        env.update(PATH=f'{self.bin}:{env["PATH"]}', FAKE_STATE=str(self.state),
                   REAL_INSTALL=shutil.which('install'), SYSTEMCTL='systemctl',
                   BACKUP_LIB_DIR=str(self.lib), BACKUP_UNIT_DIR=str(self.units),
                   BACKUP_CONF_DIR=str(self.conf))
        env.update(extra)
        return env

    def run(self, *args, **extra):
        return subprocess.run(['bash', str(SCRIPT), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=60)

    def calls(self, name=None):
        log = self.state / 'calls.log'
        out = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [c for c in out if name is None or c[0] == name]

    def installed(self):
        return (self.lib / 'service_backup.py').exists() or \
            (self.units / 'nekovpn-backup.timer').exists()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    trip = e.state / 'tripwire.log'
    assert not trip.exists(), trip.read_text()


class TestInstall:

    def test_first_install(self, env):
        r = env.run('--recipient', KEY)
        assert r.returncode == 0, r.stderr
        assert env.rcpt.read_text().splitlines()[-1] == KEY
        assert env.rcpt.stat().st_mode & 0o777 == 0o644
        copy = env.lib / 'service_backup.py'
        assert copy.read_bytes() == (REPO / 'scripts' / 'service_backup.py').read_bytes()
        assert copy.stat().st_mode & 0o777 == 0o755
        assert (env.units / 'nekovpn-backup.service').read_bytes() == SERVICE.read_bytes()
        assert (env.units / 'nekovpn-backup.timer').read_bytes() == TIMER.read_bytes()
        assert env.calls('systemctl') == [['systemctl', 'daemon-reload'],
                                          ['systemctl', 'enable', '--now', 'nekovpn-backup.timer']]
        assert env.calls('docker') == [
            ['docker', 'exec', 'vpn-bot', 'test', '-f', '/app/scripts/backup_send.py'],
            ['docker', 'exec', 'vpn-bot', 'python3', '/app/scripts/backup_send.py', 'installed']]
        owners = [c for c in env.calls('install') if '-o' in c]
        assert owners and all(c[c.index('-o') + 1] == 'root' for c in owners)
        assert '--dry-run' in r.stdout

    def test_a_rerun_keeps_the_key(self, env):
        assert env.run('--recipient', KEY).returncode == 0
        before = env.rcpt.read_text()
        assert env.run().returncode == 0
        assert env.run('--recipient', KEY).returncode == 0          # the same key: no --force
        assert env.rcpt.read_text() == before

    def test_another_key_needs_force(self, env):
        assert env.run('--recipient', KEY).returncode == 0
        r = env.run('--recipient', KEY2)
        assert r.returncode == 1 and 'needs --force' in r.stderr
        assert env.rcpt.read_text().splitlines()[-1] == KEY
        r = env.run('--recipient', KEY2, '--force')
        assert r.returncode == 0
        assert env.rcpt.read_text().splitlines()[-1] == KEY2

    @pytest.mark.parametrize('secret', [SECRET, SECRET.lower(), f'  {SECRET}'])
    def test_a_secret_key_is_refused_and_never_echoed(self, env, secret):
        r = env.run('--recipient', secret)
        assert r.returncode == 1
        assert 'SECRET age key' in r.stderr and 'new key pair' in r.stderr
        assert 'GFPYYSJZ' not in (r.stdout + r.stderr).upper()
        assert not env.rcpt.exists() and not env.installed() and not env.calls('systemctl')

    def test_a_secret_key_as_a_stray_argument_is_not_echoed(self, env):
        r = env.run(SECRET)
        assert r.returncode == 1 and 'unknown argument' in r.stderr
        assert 'GFPYYSJZ' not in (r.stdout + r.stderr).upper()

    @pytest.mark.parametrize('bad', [KEY[:-1], KEY + 'q', 'ssh-ed25519 AAAAC3Nza', KEY.upper(),
                                     KEY.replace('q', 'b', 1), ''])
    def test_not_a_public_key(self, env, bad):
        r = env.run('--recipient', bad)
        assert r.returncode == 1
        assert 'not an age public key' in r.stderr or 'needs the public key' in r.stderr \
            or 'no ' in r.stderr
        assert not env.installed()

    def test_no_key_at_all(self, env):
        r = env.run()
        assert r.returncode == 1 and '--recipient age1' in r.stderr
        assert 'nekovpn-backup-age.pub' in r.stderr and not env.installed()

    @pytest.mark.parametrize('content,why', [
        (f'{KEY}\n{SECRET}\n', 'SECRET age key'),
        ('# only\n', 'no age public key'),
        (f'{KEY}\nnot a key\n', 'not an age public key'),
    ])
    def test_a_bad_recipients_file(self, env, content, why):
        env.conf.mkdir(parents=True)
        env.rcpt.write_text(content)
        r = env.run()
        assert r.returncode == 1 and why in r.stderr
        assert 'GFPYYSJZ' not in r.stdout + r.stderr
        assert not env.installed()

    @pytest.mark.parametrize('tool', ['age', 'zstd'])
    def test_tools_missing(self, env, tool):
        """PATH = the fakes + only the tools the installer uses: no real age
        or zstd can stand in."""
        (env.bin / tool).unlink()
        tools = env.root / 'tools'
        tools.mkdir()
        for name in ('bash', 'grep', 'mktemp', 'chmod', 'mv', 'rm', 'dirname', 'python3'):
            (tools / name).symlink_to(shutil.which(name))
        r = env.run('--recipient', KEY, PATH=f'{env.bin}:{tools}')
        assert r.returncode == 1 and 'apt-get install age zstd' in r.stderr
        assert not env.installed() and not env.rcpt.exists()

    def test_the_bot_cannot_send_yet(self, env):
        (env.state / 'no_sender').touch()
        r = env.run('--recipient', KEY)
        assert r.returncode == 1 and 'deploy the bot first' in r.stderr
        assert not env.installed() and not env.calls('systemctl')

    def test_docker_down_installs_nothing(self, env):
        (env.state / 'docker_down').touch()
        r = env.run('--recipient', KEY)
        assert r.returncode == 1 and 'deploy the bot first' in r.stderr
        assert not env.installed()

    def test_the_bot_not_told_is_only_a_warning(self, env):
        (env.state / 'python_fails').touch()
        r = env.run('--recipient', KEY)
        assert r.returncode == 0 and env.installed()
        assert 'WARN: could not tell the bot' in r.stderr

    def test_not_root(self, env):
        (env.state / 'uid').write_text('1000')
        r = env.run('--recipient', KEY)
        assert r.returncode == 1 and 'run as root' in r.stderr
        assert not env.installed()

    def test_remove(self, env):
        assert env.run('--recipient', KEY).returncode == 0
        r = env.run('--remove')
        assert r.returncode == 0
        assert not env.installed() and not env.lib.exists()
        assert env.rcpt.exists()                                  # the public key stays
        assert ['systemctl', 'disable', '--now', 'nekovpn-backup.timer'] in env.calls('systemctl')
        assert env.calls('docker')[-1] == ['docker', 'exec', 'vpn-bot', 'python3',
                                           '/app/scripts/backup_send.py', 'forget']
        assert 'alert backup:stale is off' in r.stdout

    def test_remove_with_the_bot_down(self, env):
        assert env.run('--recipient', KEY).returncode == 0
        (env.state / 'docker_down').touch()
        r = env.run('--remove')
        assert r.returncode == 0 and not env.installed()
        assert 'backup_send.py forget' in r.stderr


class TestUnits:

    def test_the_service_runs_the_root_owned_copy(self):
        text = SERVICE.read_text()
        assert 'ExecStart=/usr/bin/python3 /usr/local/lib/nekovpn-backup/service_backup.py\n' in text
        assert '/opt/vpn-bot' not in [l for l in text.splitlines() if l.startswith('ExecStart')][0]
        for line in ('Type=oneshot', 'UMask=0077', 'StateDirectory=nekovpn-backup',
                     'StateDirectoryMode=0700', 'NoNewPrivileges=yes', 'PrivateTmp=yes',
                     'ProtectSystem=full', 'MemoryMax=512M'):
            assert f'{line}\n' in text, line

    def test_the_timer(self):
        text = TIMER.read_text()
        assert 'OnCalendar=*-*-* 01:40:00 UTC\n' in text and 'Persistent=true\n' in text
        assert 'WantedBy=timers.target' in text

    def test_the_installer_names_what_the_script_reads(self):
        """The paths the installer writes are the script's defaults."""
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location('sb', REPO / 'scripts' / 'service_backup.py')
        mod = importlib.util.module_from_spec(spec)
        sys.modules['sb'] = mod
        spec.loader.exec_module(mod)
        text = SCRIPT.read_text()
        assert mod.DEFAULT_RECIPIENTS == '/etc/nekovpn-backup/recipients.txt'
        assert 'CONF_DIR="${BACKUP_CONF_DIR:-/etc/nekovpn-backup}"' in text
        assert 'RCPT_FILE="$CONF_DIR/recipients.txt"' in text
        assert f'SENDER={mod.SENDER}\n' in text
        assert mod.DEFAULT_STATE_DIR == '/var/lib/' + 'nekovpn-backup'
