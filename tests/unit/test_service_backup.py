"""scripts/service_backup.py — the daily encrypted service backup on entry
(AGENTS.md §58, docs/runbooks/backup_restore.md).

Two levels:
- in-process: the collector (paths, owners, caps, binaries, links, excludes,
  required items, databases as consistent copies, facts), the framed streams,
  the part check, the recipients file;
- the script as a file, the way the timer runs it, with ``ssh``, ``docker``,
  ``zstd`` and ``age`` replaced by fakes first in PATH: ``ssh`` runs the
  remote command locally against a fake exit tree, ``docker exec`` runs the
  snapshot code locally against a real WAL-mode sqlite file and stores what
  the sender would get. Every other host command the facts call is a fake
  that logs; power, firewall and process commands are tripwires that fail
  the test (AGENTS.md §51: a test payload once rebooted the laptop).

At the end, the restore stand: real age-keygen / age / zstd, and the check
block of docs/runbooks/backup_restore.md run as written (skipped without the
tools, failed with BACKUP_REQUIRE_AGE=1, which CI sets).
"""

import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'service_backup.py'


def _load():
    spec = importlib.util.spec_from_file_location('service_backup', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod          # dataclasses look the module up while it loads
    spec.loader.exec_module(mod)
    return mod


sb = _load()

RECIPIENT = 'age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p'
ENTRY_SECRET = 'ENTRY-TOKEN-must-never-be-printed'
EXIT_SECRET = 'EXIT-TOKEN-must-never-be-printed'
VOLUME = 'var/lib/docker/volumes/vpn-bot_vpn-bot-data/_data'
XUI = 'var/lib/docker/volumes/vpn-bot_3xui-data/_data'
ROOT = os.geteuid() == 0

# ------------------------------------------------------------------ fakes --

TRIPWIRES = ('reboot', 'shutdown', 'poweroff', 'halt', 'init', 'telinit', 'kill', 'pkill',
             'killall', 'iptables', 'ip6tables', 'nft', 'useradd', 'userdel', 'chpasswd',
             'curl', 'wget', 'rm', 'mkfs', 'dd')
FAKE_TRIPWIRE = '#!/bin/sh\necho "$(basename "$0") $*" >> "$FAKE_STATE/tripwire.log"\nexit 1\n'

# Read-only host commands the facts call: they log and print a line.
FACT_COMMANDS = ('uname', 'ip', 'iptables-save', 'ip6tables-save', 'ufw', 'systemctl',
                 'crontab', 'dpkg-query', 'haproxy', 'hysteria', 'caddy')
FAKE_FACT = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.path.join(os.environ['FAKE_STATE'], 'calls.log'), 'a') as f:
    f.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + '\n')
print('fake', os.path.basename(sys.argv[0]), *sys.argv[1:])
'''

FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
st = os.environ['FAKE_STATE']
args = sys.argv[1:]
with open(os.path.join(st, 'calls.log'), 'a') as f:
    f.write(json.dumps(['ssh'] + args) + '\n')
mode_file = os.path.join(st, 'ssh_mode')
mode = open(mode_file).read().strip() if os.path.exists(mode_file) else 'ok'
rest, i = [], 0
while i < len(args):
    if args[i] == '-o':
        i += 2
        continue
    rest.append(args[i])
    i += 1
command = ' '.join(rest[1:])
if mode == 'down':
    print('ssh: connect to host exit-node port 22: Connection timed out', file=sys.stderr)
    sys.exit(255)
if mode == 'slow':
    import time
    with open(os.path.join(st, 'slow.pid'), 'w') as f:
        f.write(str(os.getpid()))
    time.sleep(60)
out = sys.stdout.buffer
if mode == 'banner':
    out.write(b'Welcome to exit! Last login: never\n')
    out.flush()
if mode == 'truncated':
    r = subprocess.run(['sh', '-c', command], stdin=sys.stdin, stdout=subprocess.PIPE)
    out.write(r.stdout[:len(r.stdout) // 2])
    sys.exit(0)
if mode == 'garbage':
    sys.stdin.buffer.read()
    out.write(b'not a stream ' * 10000)
    sys.exit(0)
out.flush()
os.execvp('sh', ['sh', '-c', command])
'''

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
st = os.environ['FAKE_STATE']
args = sys.argv[1:]
with open(os.path.join(st, 'calls.log'), 'a') as f:
    f.write(json.dumps(['docker'] + [a if len(a) < 300 else a[:40] + '…' for a in args]) + '\n')

def mode(name, default='ok'):
    p = os.path.join(st, name)
    return open(p).read().strip() if os.path.exists(p) else default

if args[:1] == ['exec']:
    rest = args[1:]
    while rest and rest[0].startswith('-'):
        rest = rest[1:]
    container, cmd = rest[0], rest[1:]
    if container == '3x-ui':
        print('Xray 26.6.22 (Xray, Penetrates Everything.)')
        sys.exit(0)
    if container != 'vpn-bot':
        print(f'Error: No such container: {container}', file=sys.stderr)
        sys.exit(1)
    bot = mode('bot_mode')
    if bot == 'down':
        print('Error response from daemon: container is not running', file=sys.stderr)
        sys.exit(1)
    if cmd[:3] == ['python3', '-I', '-c']:
        if bot == 'snapshot_broken':
            print('sqlite3.OperationalError: unable to open database file', file=sys.stderr)
            sys.exit(1)
        os.execvp('python3', cmd)
    if cmd[:2] == ['python3', '/app/scripts/backup_send.py']:
        if bot == 'nosender':
            print("python3: can't open file '/app/scripts/backup_send.py': [Errno 2] No such "
                  "file or directory", file=sys.stderr)
            sys.exit(2)
        sub = cmd[2]
        if sub == 'send':
            data = sys.stdin.buffer.read()
            n = len([x for x in os.listdir(st) if x.startswith('sent-')])
            with open(os.path.join(st, f'sent-{n}.bin'), 'wb') as f:
                f.write(data)
            with open(os.path.join(st, f'sent-{n}.json'), 'w') as f:
                json.dump(cmd[3:], f)
            if bot == 'sendfail':
                print(json.dumps({'ok': False, 'error': 'Telegram: Bad Request: chat not found'}))
                sys.exit(1)
            print('INFO some log line')
            print(json.dumps({'ok': True, 'thread_id': 77, 'message_ids': [1001], 'parts': 1}))
            sys.exit(0)
        if sub == 'fail':
            with open(os.path.join(st, 'fail.log'), 'a') as f:
                f.write(json.dumps(cmd[3:]) + '\n')
            print('{"ok": true}')
            sys.exit(0)
    sys.exit(0)
if args[:1] == ['ps']:
    print('vpn-bot\tvpn-bot\tUp 2 days\t0.0.0.0:8080->8080/tcp')
    print('cavitator\tcavitator:latest\tUp 9 days\t0.0.0.0:80->80/tcp')
    sys.exit(0)
if args[:1] == ['image']:
    print('vpn-bot:latest\t<none>\tabc')
    print('cavitator:latest\tsha256:feed\tdef')
    sys.exit(0)
print('fake docker', *args)
'''

FAKE_ZSTD = '#!/bin/sh\nprintf "FAKEZSTD\\n"\ncat\n'

FAKE_AGE = r'''#!/usr/bin/env python3
import os, sys
st = os.environ['FAKE_STATE']
if os.path.exists(os.path.join(st, 'age_mode')):
    print('age: error: failed to parse recipients', file=sys.stderr)
    sys.exit(1)
args = sys.argv[1:]
rcpt = args[args.index('-R') + 1]
out = args[args.index('-o') + 1]
keys = [l for l in open(rcpt).read().split() if l.startswith('age1')]
assert keys, 'no recipient'
data = sys.stdin.buffer.read()
with open(out, 'wb') as f:
    if os.path.exists(os.path.join(st, 'age_plain')):
        f.write(data)
    else:
        f.write(b'age-encryption.org/v1\n' + b'FAKE-AGE ' + keys[0].encode() + b'\n' + data)
'''


def open_fake_archive(data: bytes) -> tarfile.TarFile:
    """Undo the fakes: age header + FAKE-AGE line, then FAKEZSTD line."""
    assert data.startswith(b'age-encryption.org/v1\nFAKE-AGE ' + RECIPIENT.encode() + b'\n')
    body = data.split(b'\n', 2)[2]
    assert body.startswith(b'FAKEZSTD\n')
    return tarfile.open(fileobj=io.BytesIO(body[len(b'FAKEZSTD\n'):]), mode='r:')


def make_bot_db(path: Path, users=5):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE users (chat_id TEXT PRIMARY KEY, status TEXT)')
    conn.execute('CREATE TABLE app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.executemany('INSERT INTO users VALUES (?, ?)', [(str(i), 'paid') for i in range(users)])
    conn.commit()
    return conn


NETSTAT_MARK = 'NETSTAT-ROW-must-never-be-archived'
NETSTAT_ROWS = 300


def add_netstat_events(conn):
    """The table bot/services/netstat.py writes, with rows a test can find
    in the bytes of a file."""
    conn.execute('CREATE TABLE netstat_events (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, '
                 'area TEXT, app TEXT NOT NULL)')
    conn.executemany('INSERT INTO netstat_events (ts, area, app) VALUES (?, ?, ?)',
                     [(1800000000 + i, 'ucfv0', NETSTAT_MARK) for i in range(NETSTAT_ROWS)])
    conn.commit()


def tables_of(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def make_xui_db(path: Path, clients=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE clients (id INTEGER PRIMARY KEY, email TEXT)')
    conn.executemany('INSERT INTO clients(email) VALUES (?)', [(f'u{i}',) for i in range(clients)])
    conn.commit()
    conn.close()


class Stand:
    """A fake entry tree, a fake exit tree, fakes first in PATH."""

    def __init__(self, base: Path):
        self.hermes_db = None
        self.base = base
        self.bin = base / 'bin'
        self.state = base / 'fakestate'
        self.entry = base / 'entry'
        self.exit = base / 'exit'
        self.backup_state = base / 'backupstate'
        self.tmp = base / 'tmp'
        self.recipients = base / 'recipients.txt'
        for d in (self.bin, self.state, self.entry, self.exit, self.tmp):
            d.mkdir(parents=True)
        fakes = {'ssh': FAKE_SSH, 'docker': FAKE_DOCKER, 'zstd': FAKE_ZSTD, 'age': FAKE_AGE,
                 **{t: FAKE_TRIPWIRE for t in TRIPWIRES}, **{c: FAKE_FACT for c in FACT_COMMANDS}}
        for name, body in fakes.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.recipients.write_text(f'# test\n{RECIPIENT}\n')
        self._entry_tree()
        self._exit_tree()

    def put(self, root: Path, rel: str, text: str = 'x', mode: int = 0o644) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        p.chmod(mode)
        return p

    def _entry_tree(self):
        e = self.entry
        self.put(e, 'opt/vpn-bot/.env', f'BOT_TOKEN={ENTRY_SECRET}\n', 0o600)
        self.put(e, 'opt/vpn-bot/docker-compose.yml', 'name: vpn-bot\n')
        self.put(e, 'opt/vpn-bot/probe-proxy/config.json', '{}')
        self.put(e, 'etc/haproxy/haproxy.cfg', 'global\n')
        self.put(e, 'etc/haproxy/old.log', 'log lines\n')
        self.put(e, 'etc/ru-exit/keys.env', 'RU_PRIVATE=k\n', 0o600)
        self.put(e, 'etc/iptables/rules.v4', '*filter\nCOMMIT\n')
        self.put(e, 'etc/systemd/system/shadow-tls.service', '[Service]\nExecStart=/x\n')
        wants = e / 'etc/systemd/system/multi-user.target.wants'
        wants.mkdir(parents=True)
        (wants / 'shadow-tls.service').symlink_to('../shadow-tls.service')
        self.put(e, 'root/.hermes/config.yaml', 'model: free\n')
        self.put(e, 'root/.hermes/.env', 'OPENROUTER_API_KEY=k\n', 0o600)
        self.put(e, 'root/.hermes/skills/vpn-ops/SKILL.md', '# vpn-ops\n')
        self.put(e, 'root/.hermes/skills/stock-skill/SKILL.md', '# not ours\n')
        self.put(e, 'root/.hermes/sessions/session.json', '{"chat": "not a setting"}')
        self.put(e, 'root/.hermes/auth.json', '{"provider": "t"}', 0o600)
        self.put(e, 'root/.hermes/cron/jobs.json', '[]')
        self.put(e, 'root/.hermes/cron/run.lock', '')
        self.put(e, 'root/.hermes/memories/MEMORY.md', '# memory')
        self.put(e, 'root/.hermes/memories/agent.pid', '42')
        (e / 'root/.hermes/tools/python/bin').mkdir(parents=True)
        (e / 'root/.hermes/tools/python/bin/python3').write_bytes(b'\x7fELF\x00' * 10)
        self.put(e, 'root/.hermes/cache/blob', 'cache')
        self.put(e, 'root/.hermes/logs/gateway.log', 'log')
        self.put(e, 'root/.hermes/sandboxes/s1/file', 'sandbox')
        self.hermes_db = make_bot_db(e / 'root/.hermes/state.db', users=2)
        self.put(e, 'root/hermes-work/AGENTS.md', '# agent\n')
        self.put(e, 'root/.ssh/config', 'Host exit-node\n', 0o600)
        self.put(e, 'usr/local/bin/hermes_model_guard.py', '#!/usr/bin/env python3\n', 0o755)
        (e / 'usr/local/bin/sing-box').write_bytes(b'\x7fELF\x00\x01' * 100)
        self.put(e, 'usr/local/sbin/ru-exit-sync', '#!/bin/sh\n', 0o755)
        self.put(e, f'{VOLUME}/secrets/sub_token_secret.json', '{"k": 1}', 0o600)
        self.put(e, f'{VOLUME}/hop-tls/key.pem', 'KEY', 0o600)
        self.put(e, f'{VOLUME}/hop-tls/__pycache__/x.pyc', 'junk')
        self.bot_db = e / VOLUME / 'bot.db'
        self.bot_conn = make_bot_db(self.bot_db)
        make_xui_db(e / XUI / 'x-ui.db', clients=1)
        if ROOT:
            for p in (e / VOLUME).rglob('*'):
                os.lchown(p, 1000, 1000)
            os.chown(e / VOLUME, 1000, 1000)

    def _exit_tree(self):
        x = self.exit
        self.put(x, 'opt/vpn-bot/docker-compose.yml', 'name: vpn-bot\n')
        self.put(x, 'opt/vpn-bot/.env', f'XUI_PASS={EXIT_SECRET}\n', 0o600)
        self.put(x, 'etc/hysteria/config.yaml', 'listen: :8400\n')
        self.put(x, 'etc/caddy/Caddyfile', 'sub.example:2096 {}\n')
        self.put(x, 'etc/nekovpn-hop/entry-hop.pem', 'PIN')
        self.put(x, 'etc/ssh-exit/ssh_host_ed25519_key', 'HOSTKEY', 0o600)
        self.put(x, 'etc/xhr-exit/keys.env', 'XHR_PRIVATE=k\n', 0o600)
        self.put(x, 'etc/exit-dpi.env', 'DPI_REPORT_TOKEN=t\n', 0o600)
        self.put(x, 'root/.acme.sh/account.conf', "SAVED_CF_Token='t'\n", 0o600)
        self.put(x, 'root/.acme.sh/acme.sh.log', 'renewal log\n')
        self.put(x, f'{XUI}/access.log', 'a' * 1000)
        make_xui_db(x / XUI / 'x-ui.db', clients=7)

    def mode(self, name: str, value: str):
        (self.state / name).write_text(value)

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH',)}
        env.update(PATH=f'{self.bin}:{env["PATH"]}', FAKE_STATE=str(self.state),
                   TMPDIR=str(self.tmp))
        return env

    def argv(self, *extra):
        return [sys.executable, str(SCRIPT), '--root', str(self.entry),
                '--exit-root', str(self.exit), '--state-dir', str(self.backup_state),
                '--recipients', str(self.recipients), '--bot-db', str(self.bot_db),
                '--ssh-retry-wait', '0', *extra]

    def run(self, *extra, timeout=120):
        return subprocess.run(self.argv(*extra), env=self.env(), capture_output=True, text=True,
                              timeout=timeout)

    def calls(self):
        log = self.state / 'calls.log'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def sent(self, n=0):
        return (self.state / f'sent-{n}.bin').read_bytes()

    def sent_args(self, n=0):
        return json.loads((self.state / f'sent-{n}.json').read_text())

    def failures(self):
        log = self.state / 'fail.log'
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def stand(tmp_path):
    s = Stand(tmp_path)
    yield s
    s.bot_conn.close()
    s.hermes_db.close()
    trip = s.state / 'tripwire.log'
    assert not trip.exists(), trip.read_text()
    # facts only ever READ systemd
    for call in s.calls():
        if call[0] == 'systemctl':
            assert call[1] in ('list-unit-files', 'list-timers', 'cat'), call


def members(tf: tarfile.TarFile):
    return {m.name: m for m in tf.getmembers()}


def inner(outer: tarfile.TarFile, name: str) -> tarfile.TarFile:
    data = outer.extractfile(outer.getmember(name)).read()
    return tarfile.open(fileobj=io.BytesIO(data), mode='r:')


def stem_of(outer: tarfile.TarFile) -> str:
    return outer.getnames()[0].split('/')[0]


# ======================================================= the whole run ======

class TestTheRun:

    def test_a_complete_backup(self, stand):
        r = stand.run()
        assert r.returncode == 0, r.stderr
        archives = sorted((stand.backup_state / 'archives').iterdir())
        assert len(archives) == 1 and archives[0].name.endswith('.tar.zst.age')
        assert archives[0].stat().st_mode & 0o777 == 0o600
        data = stand.sent()
        assert data == archives[0].read_bytes()            # the bot got exactly the archive
        args = stand.sent_args()
        assert args[args.index('--name') + 1] == archives[0].name
        import hashlib
        assert args[args.index('--sha256') + 1] == hashlib.sha256(data).hexdigest()
        meta = json.loads(args[args.index('--meta-json') + 1])
        assert meta['complete'] is True
        assert {k: v['status'] for k, v in meta['parts'].items()} == \
            {'entry': 'ok', 'bot': 'ok', 'exit': 'ok'}
        assert meta['parts']['bot']['dbs'][0]['users'] == 5
        assert meta['parts']['exit']['dbs'][0]['clients'] == 7
        assert 'dbs' not in meta['parts']['entry']      # the caption names those two only
        assert not stand.failures()
        # the plaintext work dir is gone, the lock file and archives stay
        assert sorted(p.name for p in stand.backup_state.iterdir()) == ['.lock', 'archives']
        # no secret ever reaches the journal
        for out in (r.stdout, r.stderr):
            assert ENTRY_SECRET not in out and EXIT_SECRET not in out

    def test_what_is_inside(self, stand):
        assert stand.run().returncode == 0
        outer = open_fake_archive(stand.sent())
        stem = stem_of(outer)
        names = set(outer.getnames())
        assert {f'{stem}/README.md', f'{stem}/MANIFEST.json', f'{stem}/entry/entry.tar',
                f'{stem}/exit/exit.tar'} <= names
        entry = members(inner(outer, f'{stem}/entry/entry.tar'))
        for path in ('opt/vpn-bot/.env', 'opt/vpn-bot/docker-compose.yml', 'etc/haproxy/haproxy.cfg',
                     'etc/ru-exit/keys.env', 'etc/iptables/rules.v4', 'root/.hermes/config.yaml',
                     'root/.hermes/.env', 'root/.hermes/skills/vpn-ops/SKILL.md',
                     'root/hermes-work/AGENTS.md', 'root/.ssh/config', 'root/.hermes/auth.json',
                     'root/.hermes/cron/jobs.json', 'root/.hermes/memories/MEMORY.md',
                     'root/.hermes/state.db',
                     'usr/local/bin/hermes_model_guard.py', 'usr/local/sbin/ru-exit-sync',
                     f'{VOLUME}/bot.db', f'{VOLUME}/secrets/sub_token_secret.json',
                     f'{VOLUME}/hop-tls/key.pem', f'{XUI}/x-ui.db',
                     'nekovpn-facts/iptables-save.txt', 'nekovpn-facts/docker-ps.txt'):
            assert path in entry, path
        for path in ('etc/haproxy/old.log', 'root/.hermes/skills/stock-skill/SKILL.md',
                     'root/.hermes/sessions/session.json', 'usr/local/bin/sing-box',
                     'root/.hermes/cron/run.lock', 'root/.hermes/memories/agent.pid',
                     'root/.hermes/tools', 'root/.hermes/tools/python/bin/python3',
                     'root/.hermes/cache/blob', 'root/.hermes/logs/gateway.log',
                     'root/.hermes/sandboxes/s1/file', 'root/.hermes/state.db-wal',
                     f'{VOLUME}/hop-tls/__pycache__/x.pyc', f'{VOLUME}/bot.db-wal',
                     f'{VOLUME}/bot.db-shm'):
            assert path not in entry, path
        link = entry['etc/systemd/system/multi-user.target.wants/shadow-tls.service']
        assert link.issym() and link.linkname == '../shadow-tls.service'
        assert entry['opt/vpn-bot/.env'].mode == 0o600
        if ROOT:
            assert entry[f'{VOLUME}/secrets/sub_token_secret.json'].uid == 1000
            assert entry[f'{VOLUME}/bot.db'].uid == 1000
        assert list(entry)[-1] == 'nekovpn-manifest.json'
        exit_ = members(inner(outer, f'{stem}/exit/exit.tar'))
        for path in ('opt/vpn-bot/.env', 'etc/hysteria/config.yaml', 'etc/caddy/Caddyfile',
                     'etc/nekovpn-hop/entry-hop.pem', 'etc/ssh-exit/ssh_host_ed25519_key',
                     'etc/xhr-exit/keys.env', 'etc/exit-dpi.env', 'root/.acme.sh/account.conf',
                     f'{XUI}/x-ui.db', 'nekovpn-facts/xray-version.txt'):
            assert path in exit_, path
        for path in ('root/.acme.sh/acme.sh.log', f'{XUI}/access.log'):
            assert path not in exit_, path
        # the foreign container is in no container list
        facts = inner(outer, f'{stem}/entry/entry.tar').extractfile('nekovpn-facts/docker-ps.txt')
        text = facts.read().decode()
        assert 'vpn-bot' in text and 'cavitator' not in text

    def test_the_copies_are_whole_databases(self, stand, tmp_path):
        assert stand.run().returncode == 0
        outer = open_fake_archive(stand.sent())
        stem = stem_of(outer)
        out = tmp_path / 'check'
        out.mkdir()
        for part, rel, table, n in (('entry', f'{VOLUME}/bot.db', 'users', 5),
                                    ('exit', f'{XUI}/x-ui.db', 'clients', 7)):
            data = inner(outer, f'{stem}/{part}/{part}.tar').extractfile(rel).read()
            db = out / f'{part}.db'
            db.write_bytes(data)
            conn = sqlite3.connect(db)
            assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == n
            conn.close()

    def test_netstat_events_stay_out_of_the_archive(self, stand, tmp_path):
        # the NekoVPN app's network diagnostics live 14 days in the live
        # bot.db and nowhere else (bot/services/netstat.py, AGENTS.md §67)
        add_netstat_events(stand.bot_conn)
        assert stand.run().returncode == 0
        outer = open_fake_archive(stand.sent())
        stem = stem_of(outer)
        data = inner(outer, f'{stem}/entry/entry.tar').extractfile(f'{VOLUME}/bot.db').read()
        assert NETSTAT_MARK.encode() not in data            # not even in a freed page
        db = tmp_path / 'bot.db'
        db.write_bytes(data)
        conn = sqlite3.connect(db)
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert 'netstat_events' not in tables_of(conn)
        assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == 5
        conn.close()
        manifest = json.loads(outer.extractfile(f'{stem}/MANIFEST.json').read())
        [bot_db] = [d for d in manifest['parts']['entry']['manifest']['databases']
                    if d['path'].endswith('/bot.db')]
        assert 'netstat_events' not in bot_db['tables'] and bot_db['tables']['users'] == 5
        # the live database still has every row
        live = stand.bot_conn.execute('SELECT count(*) FROM netstat_events').fetchone()[0]
        assert live == NETSTAT_ROWS

    def test_readme_and_manifest(self, stand):
        assert stand.run().returncode == 0
        outer = open_fake_archive(stand.sent())
        stem = stem_of(outer)
        readme = outer.extractfile(f'{stem}/README.md').read().decode()
        manifest = json.loads(outer.extractfile(f'{stem}/MANIFEST.json').read())
        assert manifest['complete'] is True and manifest['archive'] == stem
        assert '## Порядок восстановления' in readme
        assert 'age -d -i ~/.config/nekovpn-signing/nekovpn-backup-age.key' in readme
        assert '`/etc/haproxy`' in readme and '`/etc/hysteria`' in readme
        assert 'users: 5' in readme and 'clients: 7' in readme
        files = {f['path']: f for f in manifest['parts']['entry']['manifest']['files']}
        env = files['/opt/vpn-bot/.env']
        import hashlib
        assert env['sha256'] == hashlib.sha256(f'BOT_TOKEN={ENTRY_SECRET}\n'.encode()).hexdigest()
        assert ENTRY_SECRET not in readme and ENTRY_SECRET not in json.dumps(manifest)

    def test_ssh_runs_this_file_on_exit(self, stand):
        assert stand.run().returncode == 0
        (ssh,) = [c for c in stand.calls() if c[0] == 'ssh']
        assert 'BatchMode=yes' in ssh and ssh[-2] == 'exit-node'
        assert ssh[-1].startswith('python3 -I - --collect exit --root ')

    def test_a_banner_before_the_stream_is_dropped(self, stand):
        stand.mode('ssh_mode', 'banner')
        assert stand.run().returncode == 0
        meta = json.loads(stand.sent_args()[stand.sent_args().index('--meta-json') + 1])
        assert meta['parts']['exit']['status'] == 'ok'

    @pytest.mark.parametrize('mode,why', [
        ('down', 'ssh rc=255: ssh: connect to host exit-node port 22: Connection timed out'),
        ('truncated', 'часть'),
        ('garbage', 'нет метки начала потока'),
    ])
    def test_exit_unreachable_still_sends_the_rest(self, stand, mode, why):
        stand.mode('ssh_mode', mode)
        r = stand.run()
        assert r.returncode == 1, r.stderr
        assert len([c for c in stand.calls() if c[0] == 'ssh']) == 2       # one retry
        meta = json.loads(stand.sent_args()[stand.sent_args().index('--meta-json') + 1])
        assert meta['complete'] is False
        assert meta['parts']['exit']['status'] == 'missing'
        assert why in meta['parts']['exit']['problems'][0]
        assert meta['parts']['entry']['status'] == 'ok'
        outer = open_fake_archive(stand.sent())
        stem = stem_of(outer)
        assert f'{stem}/exit/exit.tar' not in outer.getnames()             # never a cut part
        assert f'{stem}/entry/entry.tar' in outer.getnames()

    def test_bot_db_copy_failed(self, stand):
        stand.mode('bot_mode', 'snapshot_broken')
        r = stand.run()
        assert r.returncode == 1
        meta = json.loads(stand.sent_args()[stand.sent_args().index('--meta-json') + 1])
        assert meta['parts']['bot']['status'] == 'missing'
        assert 'unable to open database file' in meta['parts']['bot']['problems'][0]
        entry = members(inner(open_fake_archive(stand.sent()),
                              f'{stem_of(open_fake_archive(stand.sent()))}/entry/entry.tar'))
        assert f'{VOLUME}/bot.db' not in entry                              # never the live file
        assert f'{VOLUME}/secrets/sub_token_secret.json' in entry

    def test_a_required_file_missing_is_not_complete(self, stand):
        (stand.entry / 'root/.hermes/.env').unlink()
        r = stand.run()
        assert r.returncode == 1
        meta = json.loads(stand.sent_args()[stand.sent_args().index('--meta-json') + 1])
        assert meta['parts']['entry']['status'] == 'incomplete'
        assert 'нет /root/.hermes/.env' in meta['parts']['entry']['problems']

    def test_the_bot_down_nothing_sent_failure_kept_local(self, stand):
        stand.mode('bot_mode', 'down')
        r = stand.run()
        assert r.returncode == 2
        assert not list(stand.state.glob('sent-*'))
        archives = list((stand.backup_state / 'archives').iterdir())
        assert len(archives) == 1                     # made, kept encrypted on entry

    def test_sender_refused(self, stand):
        stand.mode('bot_mode', 'sendfail')
        r = stand.run()
        assert r.returncode == 2
        (fail,) = stand.failures()
        reason = fail[fail.index('--reason') + 1]
        assert 'отправка: Telegram: Bad Request: chat not found' in reason
        assert 'архив собран и лежит на entry' in reason

    def test_an_old_bot_image_without_the_sender(self, stand):
        stand.mode('bot_mode', 'nosender')
        r = stand.run()
        assert r.returncode == 2
        assert 'сначала задеплойте бота' in r.stderr

    def test_no_age_on_entry(self, stand, tmp_path):
        """PATH = the fakes + a python3 alone: no real age can stand in."""
        (stand.bin / 'age').unlink()
        pydir = tmp_path / 'py'
        pydir.mkdir()
        (pydir / 'python3').symlink_to(sys.executable)
        env = dict(stand.env(), PATH=f'{stand.bin}:{pydir}')
        r = subprocess.run([sys.executable, str(SCRIPT), '--root', str(stand.entry),
                            '--state-dir', str(stand.backup_state), '--recipients',
                            str(stand.recipients)], env=env, capture_output=True, text=True,
                           timeout=60)
        assert r.returncode == 2, r.stderr
        (fail,) = stand.failures()
        assert 'на entry нет age: apt-get install age' in fail[fail.index('--reason') + 1]
        assert not [c for c in stand.calls() if c[0] == 'ssh']       # nothing collected

    def test_a_secret_key_in_the_recipients_file(self, stand):
        stand.recipients.write_text('AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ\n')
        r = stand.run()
        assert r.returncode == 2
        assert 'СЕКРЕТНЫЙ ключ age' in r.stderr
        assert 'AGE-SECRET-KEY-1QQQ' not in r.stderr + r.stdout
        assert not (stand.backup_state / 'archives').exists()
        assert not [c for c in stand.calls() if c[0] == 'ssh']

    def test_what_age_wrote_is_checked(self, stand):
        stand.mode('age_plain', '1')
        r = stand.run()
        assert r.returncode == 2 and 'на выходе не файл age' in r.stderr
        assert not list(stand.state.glob('sent-*'))
        assert not list((stand.backup_state / 'archives').glob('*.age'))

    def test_age_fails(self, stand):
        stand.mode('age_mode', 'fail')
        r = stand.run()
        assert r.returncode == 2
        assert not list(stand.state.glob('sent-*'))
        assert not list((stand.backup_state / 'archives').glob('*.age'))
        assert 'упаковка не удалась' in stand.failures()[0][1]

    def test_dry_run_lists_and_leaves_nothing(self, stand):
        r = stand.run('--dry-run')
        assert r.returncode == 0, r.stderr
        assert '/etc/haproxy' in r.stdout and '/etc/hysteria' in r.stdout
        assert 'usr/local/bin/sing-box (бинарный файл)' in r.stdout
        assert not list(stand.state.glob('sent-*')) and not stand.failures()
        assert sorted(p.name for p in stand.backup_state.iterdir()) == ['.lock']
        assert ENTRY_SECRET not in r.stdout + r.stderr

    def test_no_send_keeps_the_archive(self, stand):
        r = stand.run('--no-send')
        assert r.returncode == 0
        assert not list(stand.state.glob('sent-*'))
        assert len(list((stand.backup_state / 'archives').glob('*.tar.zst.age'))) == 1

    def test_local_copies_are_pruned(self, stand):
        archives = stand.backup_state / 'archives'
        archives.mkdir(parents=True)
        for day in range(1, 10):
            (archives / f'nekovpn-backup-202001{day:02d}T014000Z.tar.zst.age').write_bytes(b'old')
        (archives / 'nekovpn-backup-20200101T000000Z.tar.zst.age.partial').write_bytes(b'cut')
        assert stand.run('--no-send', '--keep', '3').returncode == 0
        left = sorted(p.name for p in archives.iterdir())
        assert len(left) == 3 and not any(n.endswith('.partial') for n in left)
        assert left[:2] == ['nekovpn-backup-20200108T014000Z.tar.zst.age',
                            'nekovpn-backup-20200109T014000Z.tar.zst.age']

    def test_one_run_at_a_time(self, stand):
        import fcntl
        stand.backup_state.mkdir(mode=0o700)
        with open(stand.backup_state / '.lock', 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            r = stand.run()
        assert r.returncode == 2 and 'другой запуск бэкапа ещё идёт' in r.stderr
        assert not stand.calls()

    def test_leftovers_of_a_killed_run_are_removed(self, stand):
        stand.backup_state.mkdir(mode=0o700)
        old = stand.backup_state / 'work-nekovpn-backup-20261101T014000Z'
        (old / 'x').mkdir(parents=True)
        (old / 'x' / 'plain.env').write_text(ENTRY_SECRET)
        assert stand.run('--no-send').returncode == 0
        assert not old.exists()

    def test_a_stopped_run_leaves_no_plaintext_and_no_children(self, stand):
        """systemd's SIGTERM (the unit's timeout) mid-run: the work dir goes,
        the ssh child is killed, the bot hears why."""
        import signal
        stand.mode('ssh_mode', 'slow')
        p = subprocess.Popen(stand.argv(), env=stand.env(), stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
        pid_file = stand.state / 'slow.pid'
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not pid_file.exists():
            time.sleep(0.05)
        assert pid_file.exists() and list(stand.backup_state.glob('work-*'))
        p.send_signal(signal.SIGTERM)
        out, err = p.communicate(timeout=60)
        assert p.returncode == 2, err
        assert not list(stand.backup_state.glob('work-*'))
        (fail,) = stand.failures()
        assert fail[fail.index('--reason') + 1] == \
            'прерван сигналом SIGTERM: таймаут юнита или остановка'
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail('the ssh child outlived the run')

    def test_the_bot_writing_during_the_copy(self, stand):
        """The snapshot is consistent while the bot writes (WAL, backup API)."""
        stop = threading.Event()
        writer = sqlite3.connect(stand.bot_db, check_same_thread=False)

        def write():
            i = 1000
            while not stop.is_set():
                writer.execute('INSERT INTO users VALUES (?, ?)', (str(i), 'demo'))
                writer.commit()
                i += 1
                time.sleep(0.002)
        t = threading.Thread(target=write)
        t.start()
        try:
            r = stand.run()
        finally:
            stop.set()
            t.join()
        total = writer.execute('SELECT count(*) FROM users').fetchone()[0]
        writer.close()
        assert r.returncode == 0, r.stderr
        meta = json.loads(stand.sent_args()[stand.sent_args().index('--meta-json') + 1])
        users = meta['parts']['bot']['dbs'][0]['users']
        assert 5 <= users <= total
        assert meta['parts']['bot']['dbs'][0]['integrity'] == 'ok'


# ===================================================== the bot.db copy ====

class TestBotDbCopy:
    """SNAPSHOT_CODE itself, the way ``docker exec`` runs it in the container:
    ``python3 -I -c SNAPSHOT_CODE <bot.db>``, the copy on stdout after
    DB_MAGIC, a temporary file in TMPDIR meanwhile."""

    def _copy(self, live: Path, tmp: Path) -> bytes:
        tmp.mkdir(exist_ok=True)
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        r = subprocess.run([sys.executable, '-I', '-c', sb.SNAPSHOT_CODE, str(live)],
                           capture_output=True, env={**env, 'TMPDIR': str(tmp)}, timeout=60)
        assert r.returncode == 0, r.stderr.decode()
        assert r.stdout.startswith(sb.DB_MAGIC)
        assert list(tmp.iterdir()) == []                 # nothing left behind
        return r.stdout[len(sb.DB_MAGIC):]

    def test_netstat_events_are_dropped_from_the_copy_only(self, tmp_path):
        live = tmp_path / 'bot.db'
        conn = make_bot_db(live)
        add_netstat_events(conn)
        data = self._copy(live, tmp_path / 'tmp')
        assert NETSTAT_MARK.encode() not in data
        # the copy is a whole database on its own: no -wal file goes with it
        assert data[18:20] == b'\x01\x01'
        copy = tmp_path / 'copy' / 'bot.db'
        copy.parent.mkdir()
        copy.write_bytes(data)
        out = sqlite3.connect(copy)
        assert out.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert tables_of(out) == {'users', 'app_settings'}
        assert out.execute('SELECT count(*) FROM users').fetchone()[0] == 5
        out.close()
        # the live database: every row, and still in WAL
        assert conn.execute('SELECT count(*) FROM netstat_events').fetchone()[0] == NETSTAT_ROWS
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
        conn.close()

    def test_a_database_without_the_table(self, tmp_path):
        live = tmp_path / 'bot.db'
        make_bot_db(live).close()
        copy = tmp_path / 'copy.db'
        copy.write_bytes(self._copy(live, tmp_path / 'tmp'))
        out = sqlite3.connect(copy)
        assert tables_of(out) == {'users', 'app_settings'}
        out.close()

    def test_old_temporary_copies_are_removed(self, tmp_path):
        live = tmp_path / 'bot.db'
        make_bot_db(live).close()
        tmp = tmp_path / 'tmp'
        tmp.mkdir()
        old = [tmp / 'nekovpn-backup-x.db', tmp / 'nekovpn-backup-x.db-journal']
        for p in old:
            p.write_bytes(b'left by a killed run')
            os.utime(p, (time.time() - 7200, time.time() - 7200))
        self._copy(live, tmp)

    def test_the_tables_left_out_are_the_ones_netstat_creates(self, tmp_path):
        # a new diagnostics table must be left out of backups too, or be
        # meant to go in: this test makes that a decision
        from bot.services import netstat

        class Db:
            def _connect(self):
                return sqlite3.connect(tmp_path / 'netstat.db')

        netstat.ensure_table(Db())
        conn = sqlite3.connect(tmp_path / 'netstat.db')
        assert tables_of(conn) == set(sb.BOT_DB_LEFT_OUT)
        conn.close()


# ======================================================== the collector ====

@pytest.fixture
def profile(monkeypatch, tmp_path):
    """collect('entry', …) over a custom item list."""
    def use(items, dbs=(), facts=()):
        monkeypatch.setitem(sb.PROFILES, 'entry', (tuple(items), tuple(dbs), tuple(facts)))
    return use


def run_collect(root, **kw):
    buf = io.BytesIO()
    manifest = sb.collect('entry', str(root), buf, **kw)
    buf.seek(0)
    return manifest, members(tarfile.open(fileobj=buf, mode='r:')), buf


class TestCollector:

    def test_paths_owners_and_the_manifest_last(self, tmp_path, profile):
        root = tmp_path / 'root'
        (root / 'etc/a').mkdir(parents=True)
        (root / 'etc/a/conf').write_text('c')
        (root / 'etc/a/conf').chmod(0o640)
        profile([sb.Item('/etc/a', 'a')])
        manifest, tar, buf = run_collect(root)
        assert list(tar) == ['etc/a', 'etc/a/conf', 'nekovpn-manifest.json']
        assert tar['etc/a/conf'].mode == 0o640 and tar['etc/a'].isdir()
        assert manifest['files'][0]['path'] == '/etc/a/conf'
        assert manifest['items'][0] == {'path': '/etc/a', 'what': 'a', 'required': False,
                                        'files': 1, 'bytes': 1, 'status': 'ok'}

    def test_caps_binaries_and_excludes(self, tmp_path, profile):
        root = tmp_path / 'root'
        bin_ = root / 'usr/local/bin'
        bin_.mkdir(parents=True)
        (bin_ / 'tool.sh').write_text('#!/bin/sh\n')
        (bin_ / 'elf').write_bytes(b'\x7fELF\x00')
        (bin_ / 'big.sh').write_text('#' * 2000)
        (bin_ / 'edge.sh').write_text('#' * 1000)            # exactly the cap: packed
        (bin_ / 'x.log').write_text('log')
        (bin_ / '__pycache__').mkdir()
        (bin_ / '__pycache__' / 'm.pyc').write_text('c')
        profile([sb.Item('/usr/local/bin', 's', text_only=True, max_size=1000)])
        manifest, tar, _ = run_collect(root)
        assert 'usr/local/bin/tool.sh' in tar and 'usr/local/bin/edge.sh' in tar
        for gone in ('elf', 'big.sh', 'x.log', '__pycache__', '__pycache__/m.pyc'):
            assert f'usr/local/bin/{gone}' not in tar
        whys = {s['path']: s['why'] for s in manifest['skipped']}
        assert whys == {'/usr/local/bin/big.sh': 'больше 1000 Б', '/usr/local/bin/elf': 'бинарный файл'}

    def test_a_file_bigger_than_the_cap_is_listed_not_read(self, tmp_path, profile, monkeypatch):
        root = tmp_path / 'root'
        (root / 'opt').mkdir(parents=True)
        (root / 'opt/huge.db.bak').write_bytes(b'0' * 5000)
        profile([sb.Item('/opt', 'o', max_size=4096)])
        opened = []
        real_open = open

        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)
        monkeypatch.setattr('builtins.open', spy)
        manifest, tar, _ = run_collect(root)
        assert 'opt/huge.db.bak' not in tar
        assert not any(p.endswith('huge.db.bak') for p in opened)
        assert manifest['skipped'][0]['size'] == 5000

    def test_required_missing_and_globs(self, tmp_path, profile):
        root = tmp_path / 'root'
        (root / 'etc').mkdir(parents=True)
        (root / 'etc/shadow-tls.env').write_text('PASSWORD=p')
        profile([sb.Item('/etc/shadow-tls*', 'stls'), sb.Item('/etc/nothing', 'n', required=True),
                 sb.Item('/etc/optional', 'o')])
        manifest, tar, _ = run_collect(root)
        assert 'etc/shadow-tls.env' in tar
        assert manifest['problems'] == ['нет /etc/nothing']
        assert [i['status'] for i in manifest['items']] == ['ok', 'absent', 'absent']

    def test_an_item_that_is_a_link_brings_its_target(self, tmp_path, profile):
        root = tmp_path / 'root'
        (root / 'etc/real').mkdir(parents=True)
        (root / 'etc/real/cert.pem').write_text('CERT')
        (root / 'opt/abs').mkdir(parents=True)
        (root / 'opt/abs/key.pem').write_text('KEY')
        (root / 'etc/ssl').mkdir()
        (root / 'etc/ssl/sub').symlink_to('../real')
        # Absolute targets count from the node's root, as they do on the node:
        # never the test machine's own /opt or /etc, and '..' cannot climb out.
        (root / 'etc/abs').symlink_to('/opt/abs')
        (root / 'etc/up').symlink_to('../../../../../../../../opt/abs')
        profile([sb.Item('/etc/ssl/sub', 's'), sb.Item('/etc/abs', 'a'), sb.Item('/etc/up', 'u')])
        manifest, tar, _ = run_collect(root)
        assert tar['etc/ssl/sub'].issym() and 'etc/real/cert.pem' in tar
        assert tar['etc/abs'].issym() and tar['etc/abs'].linkname == '/opt/abs'
        assert tar['etc/up'].issym() and 'opt/abs/key.pem' in tar
        assert sorted(tar) == ['etc/abs', 'etc/real', 'etc/real/cert.pem', 'etc/ssl/sub', 'etc/up',
                               'nekovpn-manifest.json', 'opt/abs', 'opt/abs/key.pem']

    def test_links_in_a_chain(self, tmp_path, profile):
        """An item that is a link to a link whose target's folder is a link
        again: every link goes in as a link, the files at their real path."""
        root = tmp_path / 'root'
        (root / 'opt/real/inner').mkdir(parents=True)
        (root / 'opt/real/inner/f.conf').write_text('F')
        (root / 'lnk').symlink_to('opt/real')
        (root / 'etc').mkdir()
        (root / 'etc/c1').symlink_to('c2')
        (root / 'etc/c2').symlink_to('/lnk/inner')
        profile([sb.Item('/etc/c1', 'chain')])
        manifest, tar, _ = run_collect(root)
        assert tar['etc/c1'].linkname == 'c2' and tar['etc/c2'].linkname == '/lnk/inner'
        assert tar['lnk'].linkname == 'opt/real'
        assert 'opt/real/inner/f.conf' in tar and 'lnk/inner/f.conf' not in tar
        assert manifest['layout'] == [{'path': '/lnk', 'target': 'opt/real'}]

    def test_a_db_behind_a_link_is_never_packed_raw(self, tmp_path, profile):
        """The DB is named through a link; a walked folder is its real home:
        only the copy goes in, never the live file or its -wal."""
        root = tmp_path / 'root'
        conn = make_bot_db(root / 'data' / 'app.db', users=3)
        conn.execute('INSERT INTO users VALUES (?, ?)', ('w', 'demo'))
        conn.commit()
        (root / 'data' / 'notes.txt').write_text('n')
        (root / 'link').symlink_to('data')
        profile([sb.Item('/data', 'd')], dbs=[sb.Db('/link/app.db', 'db')])
        manifest, tar, buf = run_collect(root)
        conn.close()
        assert 'data/notes.txt' in tar and 'data/app.db' in tar
        assert 'data/app.db-wal' not in tar and 'data/app.db-shm' not in tar
        buf.seek(0)
        data = tarfile.open(fileobj=buf, mode='r:').extractfile('data/app.db').read()
        copy = tmp_path / 'c.db'
        copy.write_bytes(data)
        assert sqlite3.connect(copy).execute('SELECT count(*) FROM users').fetchone()[0] == 4
        assert [d['path'] for d in manifest['databases']] == ['/data/app.db']

    @pytest.mark.parametrize('target', ['/root/.hermes-v023', '.hermes-v023'])
    def test_hermes_behind_a_link(self, tmp_path, profile, target):
        """Hermes v0.23 may move to /root/.hermes-v023 behind a link: the
        settings and state go in under the real directory, the link next to
        them, the runtime never; GNU tar puts the same layout back."""
        root = tmp_path / 'root'
        real = root / 'root' / '.hermes-v023'
        for rel, text in (('config.yaml', 'model: x'), ('.env', 'K=1'), ('auth.json', '{}'),
                          ('SOUL.md', 'soul'), ('cron/jobs.json', '[]'), ('cron/run.lock', ''),
                          ('memories/MEMORY.md', 'm'), ('skills/vpn-ops/SKILL.md', '# ours'),
                          ('skills/stock/SKILL.md', '# stock'), ('cache/blob', 'c'),
                          ('logs/gateway.log', 'l'), ('sandboxes/s/f', 's'), ('gateway.pid', '1')):
            (real / rel).parent.mkdir(parents=True, exist_ok=True)
            (real / rel).write_text(text)
        (real / 'tools/python/bin').mkdir(parents=True)
        (real / 'tools/python/bin/python3').write_bytes(b'\x7fELF\x00')
        conn = make_bot_db(real / 'state.db', users=2)
        make_xui_db(real / 'kanban.db', clients=4)
        (root / 'root' / '.hermes').symlink_to(target)
        profile(sb.ENTRY_ITEMS, sb.ENTRY_DBS, ())
        manifest, tar, buf = run_collect(root)
        conn.close()
        assert tar['root/.hermes'].issym() and tar['root/.hermes'].linkname == target
        for rel in ('config.yaml', '.env', 'auth.json', 'SOUL.md', 'cron/jobs.json',
                    'memories/MEMORY.md', 'skills/vpn-ops/SKILL.md', 'state.db', 'kanban.db'):
            assert f'root/.hermes-v023/{rel}' in tar, rel
        for rel in ('cron/run.lock', 'skills/stock/SKILL.md', 'cache/blob', 'logs/gateway.log',
                    'sandboxes/s/f', 'gateway.pid', 'tools', 'tools/python/bin/python3',
                    'state.db-wal', 'response_store.db'):
            assert f'root/.hermes-v023/{rel}' not in tar, rel
        assert not [n for n in tar if n.startswith('root/.hermes/')]
        assert manifest['layout'] == [{'path': '/root/.hermes', 'target': target}]
        dbs = {d['path']: d for d in manifest['databases']}
        assert dbs['/root/.hermes-v023/state.db']['integrity'] == 'ok'
        assert dbs['/root/.hermes-v023/state.db']['tables']['users'] == 2
        assert dbs['/root/.hermes-v023/kanban.db']['tables']['clients'] == 4
        assert dbs['/root/.hermes-v023/response_store.db']['status'] == 'absent'
        assert manifest['problems'] == [f'нет {sb.BOT_VOLUME}', 'нет /opt/vpn-bot/.env',
                                        'нет /opt/vpn-bot/docker-compose.yml', 'нет /etc/haproxy']
        readme = sb.render_readme({'archive': 'x', 'created': '2026-11-17T01:40:12Z',
                                   'made_on': 'e', 'complete': False,
                                   'parts': {'entry': {'status': 'incomplete', 'manifest': manifest}}})
        assert f'`/root/.hermes` — ссылка на `{target}`' in readme
        part = tmp_path / 'entry.tar'
        part.write_bytes(buf.getvalue())
        out = tmp_path / 'out'
        out.mkdir()
        subprocess.run(['tar', '-xf', str(part), '-C', str(out)], check=True)
        assert os.readlink(out / 'root/.hermes') == target
        assert (out / 'root/.hermes-v023/config.yaml').read_text() == 'model: x'
        if not target.startswith('/'):     # an absolute link resolves once unpacked at /
            assert (out / 'root/.hermes/skills/vpn-ops/SKILL.md').read_text() == '# ours'

    def test_a_db_is_a_consistent_copy_and_its_live_files_stay_out(self, tmp_path, profile):
        root = tmp_path / 'root'
        live = root / 'data' / 'x-ui.db'
        conn = make_bot_db(live, users=3)       # WAL, an open connection: -wal and -shm exist
        conn.execute('INSERT INTO users VALUES (?, ?)', ('in-wal', 'demo'))
        conn.commit()
        assert (root / 'data' / 'x-ui.db-wal').exists()
        profile([sb.Item('/data', 'd')], dbs=[sb.Db('/data/x-ui.db', 'panel', required=True)])
        manifest, tar, buf = run_collect(root)
        conn.close()
        buf.seek(0)
        assert 'data/x-ui.db-wal' not in tar and 'data/x-ui.db-shm' not in tar
        (db,) = manifest['databases']
        assert db['integrity'] == 'ok' and db['tables']['users'] == 4      # the WAL row is in
        data = tarfile.open(fileobj=buf, mode='r:').extractfile('data/x-ui.db').read()
        copy = tmp_path / 'copy.db'
        copy.write_bytes(data)
        assert sqlite3.connect(copy).execute("SELECT count(*) FROM users").fetchone()[0] == 4

    def test_a_missing_required_db(self, tmp_path, profile):
        root = tmp_path / 'root'
        root.mkdir()
        profile([], dbs=[sb.Db('/data/x-ui.db', 'panel', required=True)])
        manifest, _, _ = run_collect(root)
        assert manifest['problems'] == ['нет /data/x-ui.db']

    def test_a_broken_db_is_a_problem(self, tmp_path, profile):
        root = tmp_path / 'root'
        (root / 'data').mkdir(parents=True)
        (root / 'data/x-ui.db').write_bytes(b'not sqlite at all' * 100)
        profile([], dbs=[sb.Db('/data/x-ui.db', 'panel', required=True)])
        manifest, _, _ = run_collect(root)
        assert manifest['problems'] and manifest['problems'][0].startswith('x-ui.db')

    def test_a_copy_that_fails_integrity_check(self, tmp_path, profile):
        root = tmp_path / 'root'
        (root / 'data').mkdir(parents=True)
        live = root / 'data' / 'x-ui.db'
        conn = sqlite3.connect(live)
        conn.execute('PRAGMA page_size=1024')
        conn.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)')
        conn.execute('CREATE INDEX t_v ON t(v)')
        conn.executemany('INSERT INTO t(v) VALUES (?)', [(f'value-{i:06d}',) for i in range(3000)])
        conn.commit()
        pages = conn.execute('PRAGMA page_count').fetchone()[0]
        conn.close()
        raw = bytearray(live.read_bytes())
        # The type byte of a b-tree page in the middle (no overflow or free pages
        # here): the backup API copies pages as they are, the copy fails the check.
        page = pages // 2
        raw[(page - 1) * 1024] = 0x00
        live.write_bytes(bytes(raw))
        profile([], dbs=[sb.Db('/data/x-ui.db', 'panel', required=True)])
        manifest, tar, _ = run_collect(root)
        (db,) = manifest['databases']
        assert db['status'] == 'ok' and db['integrity'] != 'ok'        # packed, and flagged
        assert 'data/x-ui.db' in tar
        assert manifest['problems'] == [f'x-ui.db: integrity_check — {db["integrity"]}']

    def test_facts(self, tmp_path, profile, monkeypatch):
        root = tmp_path / 'root'
        root.mkdir()
        bin_ = tmp_path / 'bin'
        bin_.mkdir()
        (bin_ / 'docker').write_text('#!/bin/sh\necho "vpn-bot ok"\necho "cavitator up"\nexit 3\n')
        (bin_ / 'docker').chmod(0o755)
        monkeypatch.setenv('PATH', f'{bin_}:{os.environ["PATH"]}')
        profile([], facts=[('docker-ps', ('docker', 'ps')), ('none', ('no-such-cmd-xyz',))])
        manifest, tar, buf = run_collect(root)
        buf.seek(0)
        tf = tarfile.open(fileobj=buf, mode='r:')
        ps = tf.extractfile('nekovpn-facts/docker-ps.txt').read().decode()
        assert ps == '# docker ps\n# rc=3\nvpn-bot ok\n'
        assert manifest['facts'] == [{'name': 'docker-ps', 'rc': 3, 'bytes': 11},
                                     {'name': 'none', 'rc': 127, 'bytes': 18}]

    def test_the_hermes_skills_are_the_repo_ones(self):
        ours = sorted(p.name for p in (REPO / 'skills').iterdir() if p.is_dir())
        assert list(sb.HERMES_SKILLS) == ours


# ========================================================== the pieces =====

class TestFramedStreams:

    def py(self, code):
        return [sys.executable, '-c', code]

    def test_banner_dropped_payload_kept(self, tmp_path):
        dest = tmp_path / 'p'
        r = sb.run_framed(self.py("import sys; o=sys.stdout.buffer; o.write(b'MOTD\\nM1\\npayload')"),
                          str(dest), magic=b'M1\n', timeout=30, cap=1000)
        assert r['rc'] == 0 and r['found'] and r['why'] is None
        assert dest.read_bytes() == b'payload'

    def test_stdin_is_fed(self, tmp_path):
        dest = tmp_path / 'p'
        code = "import sys; d=sys.stdin.buffer.read(); sys.stdout.buffer.write(b'M1\\n' + d[::-1])"
        r = sb.run_framed(self.py(code), str(dest), magic=b'M1\n', timeout=30, cap=10 ** 6,
                          stdin_data=b'abc' * 100000)
        assert r['rc'] == 0 and dest.read_bytes() == (b'abc' * 100000)[::-1]

    def test_no_magic(self, tmp_path):
        r = sb.run_framed(self.py("import sys; sys.stdout.buffer.write(b'x' * 200000)"),
                          str(tmp_path / 'p'), magic=b'M1\n', timeout=30, cap=10 ** 6)
        assert not r['found'] and r['why'] == 'нет метки начала потока'

    def test_cap(self, tmp_path):
        r = sb.run_framed(self.py("import sys; sys.stdout.buffer.write(b'M1\\n' + b'x' * 300000)"),
                          str(tmp_path / 'p'), magic=b'M1\n', timeout=30, cap=1000)
        assert r['why'] == 'больше 1000 Б'

    def test_timeout(self, tmp_path):
        started = time.monotonic()
        r = sb.run_framed(self.py('import time; time.sleep(60)'), str(tmp_path / 'p'),
                          magic=b'M1\n', timeout=1, cap=1000)
        assert time.monotonic() - started < 20
        assert r['why'] == 'таймаут 1 с' and r['rc'] != 0

    def test_stderr_kept(self, tmp_path):
        r = sb.run_framed(self.py("import sys; print('boom', file=sys.stderr); sys.exit(5)"),
                          str(tmp_path / 'p'), magic=b'M1\n', timeout=30, cap=1000)
        assert r['rc'] == 5 and 'boom' in r['stderr']


class TestInspectPart:

    def part(self, tmp_path, profile, node='entry'):
        root = tmp_path / 'root'
        (root / 'etc').mkdir(parents=True, exist_ok=True)
        (root / 'etc/f').write_text('f')
        profile([sb.Item('/etc', 'e')])
        _, _, buf = run_collect(root)
        p = tmp_path / 'part.tar'
        p.write_bytes(buf.getvalue())
        return p

    def test_whole(self, tmp_path, profile):
        assert sb.inspect_part(str(self.part(tmp_path, profile)), 'entry')['node'] == 'entry'

    def test_another_node(self, tmp_path, profile):
        with pytest.raises(sb.BackupFailure, match='это не часть exit'):
            sb.inspect_part(str(self.part(tmp_path, profile)), 'exit')

    def test_cut_short(self, tmp_path, profile):
        p = self.part(tmp_path, profile)
        p.write_bytes(p.read_bytes()[:1024])
        with pytest.raises(sb.BackupFailure):
            sb.inspect_part(str(p), 'entry')

    def test_no_manifest_at_the_end(self, tmp_path):
        p = tmp_path / 'p.tar'
        with tarfile.open(p, 'w') as tf:
            info = tarfile.TarInfo('etc/x')
            info.size = 1
            tf.addfile(info, io.BytesIO(b'x'))
        with pytest.raises(sb.BackupFailure, match='нет манифеста в конце'):
            sb.inspect_part(str(p), 'entry')


class TestRecipients:

    def test_ok_with_comments(self, tmp_path):
        p = tmp_path / 'r'
        p.write_text(f'# key\n\n{RECIPIENT}\n')
        assert sb.read_recipients(str(p)) == [RECIPIENT]

    @pytest.mark.parametrize('text,why', [
        ('', 'нет открытого ключа'),
        ('# only a comment\n', 'нет открытого ключа'),
        ('ssh-ed25519 AAAA\n', 'не ключ age'),
        (RECIPIENT[:-1] + '\n', 'не ключ age'),
        (RECIPIENT + 'b\n', 'не ключ age'),
        (f'{RECIPIENT}\nAGE-SECRET-KEY-1ABC\n', 'СЕКРЕТНЫЙ ключ'),
        (f'{RECIPIENT}\nage-secret-key-1abc\n', 'СЕКРЕТНЫЙ ключ'),
    ])
    def test_refused(self, tmp_path, text, why):
        p = tmp_path / 'r'
        p.write_text(text)
        with pytest.raises(sb.BackupFailure, match=why) as err:
            sb.read_recipients(str(p))
        assert 'SECRET-KEY-1ABC' not in str(err.value).upper()

    def test_missing_file(self, tmp_path):
        with pytest.raises(sb.BackupFailure, match='нет файла получателей'):
            sb.read_recipients(str(tmp_path / 'nope'))


def test_short():
    assert sb.short('a\nb\tc\x00d') == 'a b c d'
    assert sb.short('x' * 300, 10) == 'x' * 9 + '…'
    assert sb.short(b'\xff') == '�'


# ============================================ real age and zstd, the runbook ==

RUNBOOK = REPO / 'docs' / 'runbooks' / 'backup_restore.md'


def runbook_blocks():
    import re
    return re.findall(r'```bash\n(.*?)```', RUNBOOK.read_text(), re.S)


def runbook_block(marker):
    (block,) = [b for b in runbook_blocks() if marker in b]
    return block


CHECK_BLOCK = 'RESTORE=${RESTORE:-$(mktemp -d)}'
JOIN_BLOCK = 'cat "$ARCHIVE".[0-9][0-9][0-9] > "$ARCHIVE"'


class TestRunbook:

    def test_every_block_parses(self):
        import ast
        import re
        blocks = runbook_blocks()
        assert len(blocks) >= 12
        for body in blocks:
            r = subprocess.run(['bash', '-n'], input=body, capture_output=True, text=True)
            assert r.returncode == 0, (body[:80], r.stderr)
        (py,) = re.findall(r"<<'PY'\n(.*?)\nPY\n", runbook_block(CHECK_BLOCK), re.S)
        ast.parse(py)

    def test_the_readme_and_the_runbook_name_the_same_key(self):
        assert 'KEY=~/.config/nekovpn-signing/nekovpn-backup-age.key\n' in RUNBOOK.read_text()
        manifest = {'archive': 'nekovpn-backup-x', 'created': '2026-11-17T01:40:12Z',
                    'made_on': 'entry', 'parts': {}, 'complete': True}
        assert 'age -d -i ~/.config/nekovpn-signing/nekovpn-backup-age.key' in \
            sb.render_readme(manifest)


@pytest.fixture
def real_tools():
    import shutil
    if not all(shutil.which(t) for t in ('age', 'age-keygen', 'zstd')):
        if os.environ.get('BACKUP_REQUIRE_AGE') == '1':
            pytest.fail('BACKUP_REQUIRE_AGE=1 but age / age-keygen / zstd are missing')
        pytest.skip('needs age, age-keygen and zstd (apt-get install age zstd)')


@pytest.fixture
def keypair(tmp_path, real_tools):
    key = tmp_path / 'owner' / 'nekovpn-backup-age.key'
    key.parent.mkdir()
    subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
    pub = subprocess.run(['age-keygen', '-y', str(key)], check=True, capture_output=True,
                         text=True).stdout.strip()
    return key, pub


@pytest.fixture
def real_stand(tmp_path, keypair):
    s = Stand(tmp_path / 'stand')
    for fake in ('age', 'zstd'):
        (s.bin / fake).unlink()                 # the real ones from here on
    s.recipients.write_text(f'# test\n{keypair[1]}\n')
    yield s
    s.bot_conn.close()
    s.hermes_db.close()
    trip = s.state / 'tripwire.log'
    assert not trip.exists(), trip.read_text()


def run_check(stand, key, archive, restore):
    """The runbook's check block, as written, with tripwires first in PATH."""
    env = dict(os.environ, KEY=str(key), ARCHIVE=str(archive), RESTORE=str(restore),
               PATH=f'{stand.bin}:{os.environ["PATH"]}', FAKE_STATE=str(stand.state))
    return subprocess.run(['bash', '-euo', 'pipefail', '-c', runbook_block(CHECK_BLOCK)],
                          env=env, capture_output=True, text=True, timeout=180)


class TestRestoreStand:
    """Encrypt for real, decrypt with the owner's key the way the runbook says,
    and get back exactly what the nodes had."""

    def made(self, stand, *args):
        r = stand.run(*args)
        archives = sorted((stand.backup_state / 'archives').glob('*.tar.zst.age'))
        return r, archives[-1] if archives else None

    def test_the_whole_way_back(self, real_stand, keypair, tmp_path):
        key, _ = keypair
        r, archive = self.made(real_stand)
        assert r.returncode == 0, r.stderr
        data = archive.read_bytes()
        assert data.startswith(b'age-encryption.org/v1\n')
        for plain in (ENTRY_SECRET.encode(), EXIT_SECRET.encode(), b'SQLite format 3',
                      b'nekovpn-manifest.json'):
            assert plain not in data
        restore = tmp_path / 'restore'
        restore.mkdir()
        out = run_check(real_stand, key, archive, restore)
        assert out.returncode == 0, out.stdout + out.stderr
        lines = out.stdout.splitlines()
        assert lines[-2] == 'архив цел' and lines[-1] == f'распаковано в {restore}'
        assert any(l.startswith('entry: файлов ') and l.endswith('sha256 сверены') for l in lines)
        assert f'entry: /{VOLUME}/bot.db: integrity_check ok, users: 5' in lines
        assert f'exit: /{XUI}/x-ui.db: integrity_check ok, clients: 7' in lines
        e, x = restore / 'entry', restore / 'exit'
        assert (e / 'opt/vpn-bot/.env').read_text() == f'BOT_TOKEN={ENTRY_SECRET}\n'
        assert (e / 'opt/vpn-bot/.env').stat().st_mode & 0o777 == 0o600
        assert (e / VOLUME / 'secrets/sub_token_secret.json').read_text() == '{"k": 1}'
        assert (x / 'etc/ssh-exit/ssh_host_ed25519_key').read_text() == 'HOSTKEY'
        assert (x / 'root/.acme.sh/account.conf').read_text() == "SAVED_CF_Token='t'\n"
        assert os.readlink(e / 'etc/systemd/system/multi-user.target.wants/shadow-tls.service') \
            == '../shadow-tls.service'
        if ROOT:
            assert os.stat(e / VOLUME / 'secrets/sub_token_secret.json').st_uid == 1000
            assert os.stat(e / VOLUME / 'bot.db').st_uid == 1000
        conn = sqlite3.connect(e / VOLUME / 'bot.db')
        assert conn.execute('SELECT count(*) FROM users').fetchone()[0] == 5
        conn.close()

    def test_parts_join_back(self, real_stand, keypair, tmp_path):
        """The bot's part names and the runbook's join command agree."""
        from bot.services import backups
        key, _ = keypair
        r, archive = self.made(real_stand)
        assert r.returncode == 0, r.stderr
        data = archive.read_bytes()
        got = tmp_path / 'downloads'
        got.mkdir()
        chunks = backups.split_parts(data, limit=len(data) // 12)
        names = backups.part_names(archive.name, len(chunks))
        assert len(chunks) > 10                      # the glob must hold past .009
        for name, chunk in zip(names, chunks):
            (got / name).write_bytes(chunk)
        joined = got / archive.name
        env = dict(os.environ, ARCHIVE=str(joined), PATH=f'{real_stand.bin}:{os.environ["PATH"]}')
        subprocess.run(['bash', '-euo', 'pipefail', '-c', runbook_block(JOIN_BLOCK)], env=env,
                       check=True, timeout=30)
        assert joined.read_bytes() == data
        restore = tmp_path / 'restore'
        restore.mkdir()
        out = run_check(real_stand, key, joined, restore)
        assert out.returncode == 0 and 'архив цел' in out.stdout, out.stdout + out.stderr

    def test_a_damaged_archive_fails_the_check(self, real_stand, keypair, tmp_path):
        key, _ = keypair
        r, archive = self.made(real_stand)
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0x01
        archive.write_bytes(bytes(data))
        restore = tmp_path / 'restore'
        restore.mkdir()
        out = run_check(real_stand, key, archive, restore)
        assert out.returncode != 0 and 'архив цел' not in out.stdout

    def test_another_key_opens_nothing(self, real_stand, tmp_path):
        r, archive = self.made(real_stand)
        other = tmp_path / 'other.key'
        subprocess.run(['age-keygen', '-o', str(other)], check=True, capture_output=True)
        restore = tmp_path / 'restore'
        restore.mkdir()
        out = run_check(real_stand, other, archive, restore)
        assert out.returncode != 0 and not list(restore.glob('nekovpn-backup-*'))

    def test_a_partial_archive_says_what_is_missing(self, real_stand, keypair, tmp_path):
        key, _ = keypair
        real_stand.mode('ssh_mode', 'down')
        r, archive = self.made(real_stand)
        assert r.returncode == 1
        restore = tmp_path / 'restore'
        restore.mkdir()
        out = run_check(real_stand, key, archive, restore)
        assert out.returncode == 1
        assert 'exit: нет в архиве — exit: ssh rc=255' in out.stdout
        assert 'АРХИВ ПОВРЕЖДЁН ИЛИ НЕПОЛОН: проблем 1' in out.stdout
        assert f'entry: /{VOLUME}/bot.db: integrity_check ok, users: 5' in out.stdout
