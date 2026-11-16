#!/usr/bin/env python3
"""Daily encrypted backup of the whole NekoVPN service, sent by the bot to the
«Бэкапы» topic of the admin group (AGENTS.md §58, docs/runbooks/backup_restore.md).

Runs as root on ENTRY, from the root-owned copy scripts/install_service_backup.sh
puts in /usr/local/lib/nekovpn-backup/ (systemd nekovpn-backup.timer, daily):

    /usr/bin/python3 /usr/local/lib/nekovpn-backup/service_backup.py
        the run: collect, pack, encrypt, send; the last 7 archives stay in
        /var/lib/nekovpn-backup/archives (encrypted)
    ... --dry-run   collect and list what would go in (paths and sizes only);
                    nothing is encrypted, kept or sent, the plaintext is deleted
    ... --no-send   a full encrypted archive, kept locally, not sent

What goes in (README.md inside the archive lists every path with what it is):
  entry/entry.tar  entry's files at their own paths: the bot's volume (bot.db as
                   a consistent copy made INSIDE the vpn-bot container, secrets/,
                   hop-tls/), .env and compose, haproxy, shadow-tls, iptables,
                   the RU-zone, the probe sidecars, our units, Hermes' settings,
                   plus nekovpn-facts/ (versions, live firewall rules, containers)
  exit/exit.tar    the same for exit, made ON exit in one ssh round-trip (this
                   very file on stdin, `python3 -I - --collect exit`): x-ui.db as
                   a consistent copy, compose and .env, hysteria, Caddy, the hop
                   pin, xhr-exit, ssh-exit, tinyproxy, iptables, acme.sh
  README.md, MANIFEST.json (every file with its size and sha256)
tar → zstd → age, to the public key(s) in /etc/nekovpn-backup/recipients.txt. The
private key stays on the owner's machine. Logs, caches, venvs, images and the DE
reserve stay out (the bot re-provisions the reserve).

Why bot.db is copied inside the container: it is in WAL mode and the bot runs as
UID 1000. A root process opening it on the host while the bot holds no connection
creates root-owned -wal/-shm files, and the bot's next write fails ("attempt to
write a readonly database"). Inside the container the copy is made as the bot's
own user through sqlite's backup API, which stays consistent while the bot writes.

Why exit's part is never unpacked on entry: it is a tar another host made. Entry
only lists it and keeps it whole inside the archive, so a compromised exit cannot
write files on entry as root. The same holds for the bytes the container returns:
they become one file, never an extracted tree.

Sending: the bot does it (`docker exec -i vpn-bot python3 /app/scripts/backup_send.py
send`, the archive on stdin): entry cannot reach api.telegram.org, and the bot
uploads through its TG_PROXY_URLS pool. It refuses anything that is not an age
file, records the run in app_settings.backup_state, and AlertManager pages
backup:stale when no complete backup went out for 26 h.

Exit code: 0 = a complete archive was sent (or the dry run / --no-send finished);
1 = sent without a part (exit, the bot's data) or with a problem in one;
2 = nothing was sent.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import fnmatch
import glob
import grp
import hashlib
import io
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

FORMAT = 1
STREAM_MAGIC = b'NEKOVPN-BACKUP-STREAM-1\n'
DB_MAGIC = b'NEKOVPN-BACKUP-DB-1\n'
AGE_HEADER = b'age-encryption.org/v1\n'
MANIFEST_NAME = 'nekovpn-manifest.json'
FACTS_DIR = 'nekovpn-facts'

MB = 1024 * 1024
FILE_CAP = 20 * MB          # a bigger file is listed, not packed (logs, binaries, old copies)
SCRIPT_CAP = 1 * MB         # /usr/local/{bin,sbin}: text files up to this size
FACT_CAP = 4 * MB
PART_CAP = 2048 * MB        # a part bigger than this is refused
MANIFEST_CAP = 32 * MB
MAGIC_WINDOW = 64 * 1024    # an ssh banner may precede the stream, nothing longer

DEFAULT_STATE_DIR = '/var/lib/nekovpn-backup'
DEFAULT_RECIPIENTS = '/etc/nekovpn-backup/recipients.txt'
EXIT_SSH_HOST = 'exit-node'
BOT_CONTAINER = 'vpn-bot'
BOT_DB = '/var/lib/vpn-bot/bot.db'                                 # inside the container
BOT_VOLUME = '/var/lib/docker/volumes/vpn-bot_vpn-bot-data/_data'  # the same, on entry
XUI_VOLUME = '/var/lib/docker/volumes/vpn-bot_3xui-data/_data'
SENDER = '/app/scripts/backup_send.py'
KEEP_LOCAL = 7
ZSTD_LEVEL = 12
SSH_TIMEOUT_S = 300
SSH_RETRY_WAIT_S = 30
DOCKER_TIMEOUT_S = 600
SEND_TIMEOUT_S = 1800
FACT_TIMEOUT_S = 20
AGE_RECIPIENT = re.compile(r'^age1[02-9ac-hj-np-z]{58}$')

# Hermes' skills that are ours (scripts/deploy_hermes_skills.sh, the repo's
# skills/ directory; a test keeps the two lists equal). The stock ones are not.
HERMES_SKILLS = ('billing-ops', 'code-review', 'dpi-analysis', 'incident-response',
                 'server-admin', 'user-ops', 'vpn-ops')

# Never packed, wherever they sit (basename globs).
EXCLUDE = ('__pycache__', '*.pyc', '*.log', '*.log.[0-9]*', '*.log.gz', 'node_modules',
           'venv', '.venv', '.cache')

# Another project's container on entry: not ours, never touched, and kept out of
# the container lists the facts record.
FOREIGN = ('cavitator',)


@dataclass(frozen=True)
class Item:
    path: str                       # absolute; may be a glob
    what: str                       # what it is (README.md)
    required: bool = False          # missing = the part is incomplete
    text_only: bool = False         # binaries are listed, not packed
    max_size: int = FILE_CAP
    exclude: Tuple[str, ...] = ()   # basename globs, on top of EXCLUDE


@dataclass(frozen=True)
class Db:
    path: str
    what: str
    required: bool = False


def _common_items(node: str) -> Tuple[Item, ...]:
    """Host-level things both nodes have: ssh, cron, kernel and docker knobs."""
    return (
        Item('/root/.ssh', f'ssh root на {node}: ключи, config (алиасы), authorized_keys'),
        Item('/etc/ssh/sshd_config', 'sshd'),
        Item('/etc/ssh/sshd_config.d', 'sshd, дополнения'),
        Item('/etc/ssh/ssh_host_*', 'ключи хоста: после переезда тот же отпечаток, '
                                   'known_hosts у клиентов и у entry не ломаются'),
        Item('/var/spool/cron/crontabs', 'crontab (acme.sh продлевает сертификаты отсюда)'),
        Item('/etc/cron.d', 'cron.d'),
        Item('/etc/sysctl.conf', 'sysctl'),
        Item('/etc/sysctl.d', 'sysctl'),
        Item('/etc/gai.conf', 'порядок IPv4/IPv6 для glibc'),
        Item('/etc/docker/daemon.json', 'настройки docker'),
        Item('/etc/logrotate.d', 'logrotate (в т.ч. error.log xray)'),
        Item('/etc/systemd/journald.conf.d', 'лимит журнала (300 МБ с 17.11.2026)'),
        Item('/etc/os-release', 'версия ОС'),
        Item('/etc/hosts', 'hosts'),
        Item('/etc/iptables', 'iptables: rules.v4 / rules.v6 (сохранённые правила)'),
        Item('/etc/ufw', 'ufw'),
        Item('/usr/local/bin', 'root-копии наших скриптов (бинарники — только в списке)',
             text_only=True, max_size=SCRIPT_CAP),
        Item('/usr/local/sbin', 'root-копии наших скриптов (бинарники — только в списке)',
             text_only=True, max_size=SCRIPT_CAP),
    )


ENTRY_ITEMS: Tuple[Item, ...] = (
    Item(BOT_VOLUME, 'том бота vpn-bot_vpn-bot-data (/var/lib/vpn-bot в контейнере): '
                     'secrets/ — ключ /sub-ссылок и токен из дашборда (§54), '
                     'hop-tls/ — сертификат перегона exit → entry (§53); '
                     'bot.db — согласованная копия, снятая в контейнере',
         required=True, exclude=('bot.db', 'bot.db-wal', 'bot.db-shm', 'bot.db-journal')),
    Item('/opt/vpn-bot/.env', '.env бота и compose: BOT_TOKEN, ключи протоколов, токены',
         required=True),
    Item('/opt/vpn-bot/docker-compose.yml', 'compose entry (правлен руками, не равен репо)',
         required=True),
    Item('/opt/vpn-bot/docker-compose.override.yml', 'override compose'),
    Item('/opt/vpn-bot/nodes.json', 'nodes.json (старый формат кластера)'),
    Item('/opt/vpn-bot/probe-proxy/config.json',
         'сайдкар проб sing-box (scripts/gen_probe_config.py)'),
    Item('/opt/vpn-bot/probe-proxy-mihomo/config.yaml',
         'сайдкар проб mihomo (scripts/gen_probe_config_mihomo.py)'),
    Item('/opt/vpn-bot/3x-ui/cert', 'сертификаты локальной панели entry'),
    Item('/etc/haproxy', 'haproxy: SNI-роутинг Reality :8443 и xhr на exit (§23, §51)',
         required=True),
    Item('/etc/shadow-tls*', 'shadow-tls :443 → exit:8444 (если его конфиг вне юнита; '
                             'сам юнит — в /etc/systemd/system и в фактах)'),
    Item('/etc/default/shadow-tls', 'shadow-tls, переменные'),
    Item('/etc/ru-exit', 'RU-зона: ключи Reality (keys.env), server.env, конфиг (§36)'),
    Item('/etc/ssh-relay', 'SSH-ретранслятор на entry (§51, через entry)'),
    Item('/etc/ssl/cdn', 'сертификат CF-origin'),
    Item('/root/.acme.sh', 'acme.sh: аккаунт, токены DNS, сертификаты', exclude=('*.log',)),
    Item('/etc/systemd/system', 'юниты и drop-in\'ы: shadow-tls, hermes-*, protocol-healthcheck, '
                                'ru-exit-sync, nekovpn-backup…'),
    Item('/usr/local/lib/nekovpn-healthcheck', 'root-копия protocol_healthcheck.py (§50)'),
    Item('/usr/local/lib/nekovpn-backup', 'root-копия этого скрипта'),
    Item('/etc/nekovpn-backup', 'открытый ключ age, которым шифруются бэкапы'),
    Item('/root/.hermes/config.yaml', 'Hermes (/ai): модель, фолбэки, aux-модели',
         required=True),
    Item('/root/.hermes/.env', 'Hermes: ключи провайдеров (OpenRouter…)', required=True),
    Item('/root/.hermes/SOUL.md', 'Hermes: личность агента'),
    Item('/root/.hermes/memories', 'Hermes: память агента'),
    *(Item(f'/root/.hermes/skills/{name}', 'наш скилл Hermes (с подставленными значениями)')
      for name in HERMES_SKILLS),
    Item('/root/hermes-work/AGENTS.md', 'рабочий AGENTS.md агента'),
    Item('/opt/site-vpn-kzstomsk', 'SSTP-шлюз объекта kzstomsk (не из репо; README внутри)'),
    *_common_items('entry'),
)

ENTRY_DBS: Tuple[Db, ...] = (
    Db(XUI_VOLUME + '/x-ui.db', 'локальная панель 3x-ui на entry (снимок 19.07, бот ей не пользуется)'),
)

EXIT_ITEMS: Tuple[Item, ...] = (
    Item('/opt/vpn-bot/docker-compose.yml', 'compose exit (разошёлся с репо: sysctls, образ 3x-ui '
                                            'по digest, §23)', required=True),
    Item('/opt/vpn-bot/.env', '.env exit', required=True),
    Item('/opt/vpn-bot/3x-ui/cert', 'сертификаты панели 3x-ui'),
    Item('/etc/hysteria', 'hysteria и hysteria-turbo: конфиги (ACL §52, auth через релей §56), '
                          'сертификаты', required=True),
    Item('/etc/systemd/system', 'юниты: hysteria*, drop-in entry-hop (§53), hy2-auth-relay, '
                                'exit-dpi-reporter, hy2-traffic-collector, xhr/ssh-exit-sync, watchdog'),
    Item('/etc/caddy', 'Caddy: сайты :9443 и :2096, пин перегона (§53), бэкапы Caddyfile',
         required=True),
    Item('/var/lib/caddy/.local/share/caddy', 'Caddy: выпущенные сертификаты и ACME-аккаунт'),
    Item('/etc/nekovpn-hop', 'пин сертификата перегона exit → entry и hosts (§53)'),
    Item('/etc/xhr-exit', 'xhr-exit: ключи Reality, server.env, traffic.pending (§51)'),
    Item('/etc/xhr-exit.env', 'xhr-exit: BOT_URL, токен, XHR_SNI'),
    Item('/etc/ssh-exit', 'ssh-exit: ключ хоста (его пин — SSH_HOST_KEY у бота), users.plan (§51)'),
    Item('/etc/ssh-exit.env', 'ssh-exit: BOT_URL, токен'),
    Item('/etc/exit-dpi.env', 'DPI-репортёр и релей hy2: BOT_URL, BOT_CA_FILE, DPI_REPORT_TOKEN'),
    Item('/etc/hy2-auth-relay.env', 'релей авторизации hy2 (§56)'),
    Item('/usr/local/lib/nekovpn-hy2-relay', 'root-копия релея hy2 (§56)'),
    Item('/etc/tinyproxy', 'tinyproxy :8888 — выход бота в Telegram (BasicAuth, §24)'),
    Item('/root/.acme.sh', 'acme.sh: аккаунт, токен Cloudflare (dns_cf), сертификаты',
         exclude=('*.log',)),
    Item('/etc/ssl/sub', 'сертификат запасного домена подписки (:2096)'),
    Item('/etc/ssl/cdn', 'сертификат CF-origin (ws)'),
    *_common_items('exit'),
)

EXIT_DBS: Tuple[Db, ...] = (
    Db(XUI_VOLUME + '/x-ui.db', 'панель 3x-ui: клиенты (uuid, квоты, сроки), инбаунды, настройки',
       required=True),
)

_COMMON_FACTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ('uname', ('uname', '-a')),
    ('ip-addr', ('ip', '-br', 'addr')),
    ('ip-route', ('ip', 'route')),
    ('iptables-save', ('iptables-save',)),
    ('ip6tables-save', ('ip6tables-save',)),
    ('ufw-status', ('ufw', 'status', 'verbose')),
    ('docker-ps', ('docker', 'ps', '-a', '--format',
                   '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}')),
    ('docker-images', ('docker', 'image', 'ls', '--digests', '--format',
                       '{{.Repository}}:{{.Tag}}\t{{.Digest}}\t{{.ID}}')),
    ('docker-volumes', ('docker', 'volume', 'ls')),
    ('docker-version', ('docker', 'version', '--format', '{{.Server.Version}}')),
    ('systemctl-units', ('systemctl', 'list-unit-files', '--no-pager')),
    ('systemctl-timers', ('systemctl', 'list-timers', '--all', '--no-pager')),
    ('crontab', ('crontab', '-l')),
    ('packages', ('dpkg-query', '-W', '-f', '${Package}\t${Version}\n')),
)

ENTRY_FACTS = _COMMON_FACTS + (
    ('docker-inspect', ('docker', 'inspect', 'vpn-bot', '3x-ui', 'probe-proxy',
                        'probe-proxy-mihomo', 'ru-exit', 'ssh-relay', 'site-vpn-kzstomsk')),
    ('systemctl-cat', ('systemctl', 'cat', '--no-pager', 'haproxy', 'shadow-tls', 'hermes-api')),
    ('haproxy-version', ('haproxy', '-v')),
    ('hermes-pip-freeze', ('/usr/local/lib/hermes-agent/venv/bin/pip', 'freeze')),
)

EXIT_FACTS = _COMMON_FACTS + (
    ('docker-inspect', ('docker', 'inspect', '3x-ui', 'xhr-exit', 'ssh-exit')),
    ('systemctl-cat', ('systemctl', 'cat', '--no-pager', 'hysteria', 'hysteria-turbo', 'caddy',
                       'tinyproxy', 'hy2-auth-relay', 'exit-dpi-reporter',
                       'hy2-traffic-collector')),
    ('hysteria-version', ('hysteria', 'version')),
    ('caddy-version', ('caddy', 'version')),
    ('xray-version', ('docker', 'exec', '3x-ui', 'sh', '-c', '/app/bin/xray-linux-* version')),
    ('acme-list', ('/root/.acme.sh/acme.sh', '--list')),
)

# Facts whose lines name containers: the foreign ones are dropped.
CONTAINER_FACTS = ('docker-ps', 'docker-images')

PROFILES = {
    'entry': (ENTRY_ITEMS, ENTRY_DBS, ENTRY_FACTS),
    'exit': (EXIT_ITEMS, EXIT_DBS, EXIT_FACTS),
}


class BackupFailure(Exception):
    """A run that cannot produce or send an archive: the message is short,
    Russian, and carries no secret (it goes to the bot's state and the alert)."""


def log(msg: str) -> None:
    print(f'[backup] {msg}', file=sys.stderr, flush=True)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t: dt.datetime) -> str:
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def human(n: Optional[int]) -> str:
    if n is None:
        return '—'
    if n < 1024:
        return f'{n} Б'
    if n < MB:
        return f'{n / 1024:.1f} КБ'
    return f'{n / MB:.1f} МБ'


def short(text, limit: int = 200) -> str:
    """One line, printable, at most ``limit`` chars: for errors that go to the
    bot's state, the caption and the alert."""
    text = text.decode('utf-8', 'replace') if isinstance(text, bytes) else str(text or '')
    text = ' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


# ---------------------------------------------------------------- sqlite ----

def _sqlite_uri(path: str, flags: str) -> str:
    return f'file:{urllib.parse.quote(path)}?{flags}'


def snapshot_sqlite(src: str, dst: str, timeout: float = 60) -> None:
    """A consistent copy through sqlite's backup API. The source is opened
    read-only: the copy never writes to it."""
    conn = sqlite3.connect(_sqlite_uri(src, 'mode=ro'), uri=True, timeout=timeout)
    try:
        out = sqlite3.connect(dst)
        try:
            conn.backup(out)
        finally:
            out.close()
    finally:
        conn.close()


def check_sqlite(path: str) -> dict:
    """integrity_check and row counts of a copy that nothing else has open."""
    info = {'size': os.path.getsize(path), 'integrity': None, 'tables': {}}
    try:
        with open(path, 'rb') as fh:
            if fh.read(16) != b'SQLite format 3\x00':
                info['integrity'] = 'не файл sqlite'
                return info
        conn = sqlite3.connect(_sqlite_uri(path, 'immutable=1'), uri=True)
        try:
            rows = [r[0] for r in conn.execute('PRAGMA integrity_check')]
            info['integrity'] = 'ok' if rows == ['ok'] else '; '.join(map(str, rows[:5]))
            names = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
            for name in names:
                quoted = '"' + name.replace('"', '""') + '"'
                info['tables'][name] = conn.execute(f'SELECT count(*) FROM {quoted}').fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as e:
        info['integrity'] = f'ошибка sqlite: {short(e, 160)}'
    return info


# The consistent copy of bot.db, made INSIDE the vpn-bot container as the bot's
# user (see the module docstring). Prints DB_MAGIC, then the copy's bytes.
# The copy is a temporary file in the container's /tmp (TMPDIR), removed at
# once; one a killed run left behind is removed by the next run.
SNAPSHOT_CODE = r'''
import glob, os, sqlite3, sys, tempfile, time, urllib.parse
tmpdir = tempfile.gettempdir()
for old in glob.glob(os.path.join(tmpdir, 'nekovpn-backup-*.db')):
    try:
        if time.time() - os.path.getmtime(old) > 3600:
            os.unlink(old)
    except OSError:
        pass
fd, tmp = tempfile.mkstemp(prefix='nekovpn-backup-', suffix='.db', dir=tmpdir)
os.close(fd)
try:
    src = sqlite3.connect('file:' + urllib.parse.quote(sys.argv[1]) + '?mode=ro', uri=True,
                          timeout=60)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    out = sys.stdout.buffer
    out.write(%(magic)r)
    with open(tmp, 'rb') as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    out.flush()
finally:
    os.unlink(tmp)
''' % {'magic': DB_MAGIC}


# ------------------------------------------------------------- collector ----

def _owner_names(uid: int, gid: int, cache: dict) -> Tuple[str, str]:
    key = (uid, gid)
    if key not in cache:
        try:
            uname = pwd.getpwuid(uid).pw_name
        except KeyError:
            uname = ''
        try:
            gname = grp.getgrgid(gid).gr_name
        except KeyError:
            gname = ''
        cache[key] = (uname, gname)
    return cache[key]


@dataclass
class Extra:
    """A file made elsewhere (the bot.db copy) that goes in under ``arcname``
    with the owner and mode of the live file it stands for."""
    arcname: str
    src: str
    like: Optional[str]
    what: str
    check: Optional[dict] = None


class Collector:
    """Writes one node's files into a tar stream: each item at its own path
    (relative to /), the databases as consistent copies, the facts, and the
    manifest LAST — a part that ends without it was cut short."""

    def __init__(self, node: str, root: str, out, *, now: dt.datetime):
        self.node, self.root, self.now = node, root, now
        self.tar = tarfile.open(fileobj=out, mode='w|', format=tarfile.PAX_FORMAT)
        self.seen = set()
        self.names: dict = {}
        self.files: List[dict] = []
        self.skipped: List[dict] = []
        self.errors: List[dict] = []
        self.items: List[dict] = []
        self.dbs: List[dict] = []
        self.facts: List[dict] = []
        self.problems: List[str] = []
        self.dirs = 0
        self.links = 0

    # paths: "host" = as on the node (/etc/x), "fs" = where to read it (root/etc/x)
    def fs(self, host: str) -> str:
        return os.path.join(self.root, host.lstrip('/'))

    def host(self, fs: str) -> str:
        rel = os.path.relpath(fs, self.root)
        return '/' if rel == '.' else '/' + rel

    def _info(self, arcname: str, st: os.stat_result) -> tarfile.TarInfo:
        info = tarfile.TarInfo(arcname)
        info.uid, info.gid = st.st_uid, st.st_gid
        info.uname, info.gname = _owner_names(st.st_uid, st.st_gid, self.names)
        info.mode = stat.S_IMODE(st.st_mode)
        info.mtime = int(st.st_mtime)
        return info

    def _excluded(self, name: str, item: Item) -> bool:
        return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE + item.exclude)

    def add_item(self, item: Item) -> None:
        stats = {'path': item.path, 'what': item.what, 'required': item.required,
                 'files': 0, 'bytes': 0, 'status': 'ok'}
        self.items.append(stats)
        pattern = self.fs(item.path)
        if glob.has_magic(item.path):
            matches = sorted(glob.glob(glob.escape(self.root.rstrip('/') + '/')
                                       + item.path.lstrip('/')))
        else:
            matches = [pattern] if os.path.lexists(pattern) else []
        if not matches:
            stats['status'] = 'absent'
            if item.required:
                self.problems.append(f'нет {item.path}')
            return
        for match in matches:
            self._add_tree(match, item, stats)
            if os.path.islink(match):
                # The item names a link: its target's content goes in too, at
                # the target's own path, when the target is on this node.
                target = os.path.realpath(match)
                inside = os.path.commonpath([target, os.path.realpath(self.root)]) == \
                    os.path.realpath(self.root)
                if inside and os.path.exists(target):
                    self._add_tree(target, item, stats)

    def _add_tree(self, fs_path: str, item: Item, stats: dict) -> None:
        self._add_entry(fs_path, item, stats)
        try:
            if not stat.S_ISDIR(os.lstat(fs_path).st_mode):
                return
        except OSError:
            return

        def onerror(e):
            self.errors.append({'path': self.host(getattr(e, 'filename', '') or fs_path),
                                'error': short(e, 160)})
        for dirpath, dirnames, filenames in os.walk(fs_path, onerror=onerror):
            dirnames.sort()
            keep = []
            for d in dirnames:
                if self._excluded(d, item):
                    continue
                full = os.path.join(dirpath, d)
                self._add_entry(full, item, stats)
                if not os.path.islink(full):
                    keep.append(d)
            dirnames[:] = keep
            for f in sorted(filenames):
                if not self._excluded(f, item):
                    self._add_entry(os.path.join(dirpath, f), item, stats)

    def _add_entry(self, fs_path: str, item: Item, stats: dict) -> None:
        host = self.host(fs_path)
        if host in self.seen:
            return
        self.seen.add(host)
        try:
            st = os.lstat(fs_path)
        except OSError as e:
            self.errors.append({'path': host, 'error': short(e, 160)})
            return
        info = self._info(host.lstrip('/'), st)
        if stat.S_ISLNK(st.st_mode):
            try:
                info.linkname = os.readlink(fs_path)
            except OSError as e:
                self.errors.append({'path': host, 'error': short(e, 160)})
                return
            info.type = tarfile.SYMTYPE
            self.tar.addfile(info)
            self.links += 1
            return
        if stat.S_ISDIR(st.st_mode):
            info.type = tarfile.DIRTYPE
            self.tar.addfile(info)
            self.dirs += 1
            return
        if not stat.S_ISREG(st.st_mode):
            self.skipped.append({'path': host, 'size': None, 'why': 'не файл (сокет, fifo, устройство)'})
            return
        if st.st_size > item.max_size:
            self.skipped.append({'path': host, 'size': st.st_size,
                                 'why': f'больше {human(item.max_size)}'})
            return
        try:
            with open(fs_path, 'rb') as fh:
                data = fh.read(item.max_size + 1)
        except OSError as e:
            self.errors.append({'path': host, 'error': short(e, 160)})
            return
        if len(data) > item.max_size:          # it grew after the stat
            self.skipped.append({'path': host, 'size': len(data),
                                 'why': f'больше {human(item.max_size)}'})
            return
        if item.text_only and b'\x00' in data[:8192]:
            self.skipped.append({'path': host, 'size': len(data), 'why': 'бинарный файл'})
            return
        info.size = len(data)
        self.tar.addfile(info, io.BytesIO(data))
        self.files.append({'path': host, 'size': len(data),
                           'sha256': hashlib.sha256(data).hexdigest(),
                           'mode': f'{info.mode:04o}', 'uid': info.uid, 'gid': info.gid})
        stats['files'] += 1
        stats['bytes'] += len(data)

    def _add_file_from(self, arcname: str, src: str, st: os.stat_result) -> Tuple[int, str]:
        digest = hashlib.sha256()
        with open(src, 'rb') as fh:
            for chunk in iter(lambda: fh.read(MB), b''):
                digest.update(chunk)
        size = os.path.getsize(src)
        info = self._info(arcname, st)
        info.size = size
        info.mtime = int(self.now.timestamp())
        with open(src, 'rb') as fh:
            self.tar.addfile(info, fh)
        return size, digest.hexdigest()

    def reserve(self, host: str) -> None:
        """Keep a path out of the tree walks (the live DB a copy stands for)."""
        for suffix in ('', '-wal', '-shm', '-journal'):
            self.seen.add(host + suffix)

    def add_db(self, db: Db, workdir: str) -> None:
        entry = {'path': db.path, 'what': db.what, 'required': db.required}
        self.dbs.append(entry)
        live = self.fs(db.path)
        if not os.path.exists(live):
            entry['status'] = 'absent'
            if db.required:
                self.problems.append(f'нет {db.path}')
            return
        copy = os.path.join(workdir, 'copy.db')
        try:
            snapshot_sqlite(live, copy)
            check = check_sqlite(copy)
            size, sha = self._add_file_from(db.path.lstrip('/'), copy, os.stat(live))
        except (sqlite3.Error, OSError) as e:
            entry.update(status='error', error=short(e, 160))
            self.problems.append(f'{os.path.basename(db.path)}: {short(e, 120)}')
            return
        finally:
            for suffix in ('', '-journal', '-wal', '-shm'):
                try:
                    os.unlink(copy + suffix)
                except OSError:
                    pass
        entry.update(status='ok', size=size, sha256=sha, integrity=check['integrity'],
                     tables=check['tables'])
        if check['integrity'] != 'ok':
            self.problems.append(f'{os.path.basename(db.path)}: integrity_check — {check["integrity"]}')

    def add_extra(self, extra: Extra) -> None:
        host = '/' + extra.arcname
        try:
            st = os.lstat(self.fs(extra.like)) if extra.like else None
        except OSError:
            st = None
        if st is None:
            st = os.stat(extra.src)
        size, sha = self._add_file_from(extra.arcname, extra.src, st)
        entry = {'path': host, 'what': extra.what, 'required': True, 'status': 'ok',
                 'size': size, 'sha256': sha}
        if extra.check:
            entry.update(integrity=extra.check.get('integrity'), tables=extra.check.get('tables'))
        self.dbs.append(entry)

    def add_fact(self, name: str, argv: Sequence[str]) -> None:
        env = dict(os.environ, LANG='C', LC_ALL='C')       # stable, untranslated output
        try:
            r = subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=FACT_TIMEOUT_S, env=env)
            out, rc = r.stdout[:FACT_CAP], r.returncode
        except FileNotFoundError:
            out, rc = b'(no such command)\n', 127
        except subprocess.TimeoutExpired:
            out, rc = b'(timed out)\n', -1
        except OSError as e:
            out, rc = f'({short(e, 120)})\n'.encode(), -2
        if name in CONTAINER_FACTS:
            out = b''.join(line for line in out.splitlines(keepends=True)
                           if not any(f.encode() in line for f in FOREIGN))
        header = f'# {" ".join(shlex.quote(a) for a in argv)}\n# rc={rc}\n'.encode()
        data = header + out
        info = tarfile.TarInfo(f'{FACTS_DIR}/{name}.txt')
        info.size, info.mode, info.mtime = len(data), 0o600, int(self.now.timestamp())
        self.tar.addfile(info, io.BytesIO(data))
        self.facts.append({'name': name, 'rc': rc, 'bytes': len(out)})

    def manifest(self) -> dict:
        return {
            'format': FORMAT, 'node': self.node, 'host': socket.gethostname(),
            'created': iso(self.now), 'python': sys.version.split()[0],
            'items': self.items, 'databases': self.dbs, 'files': self.files,
            'skipped': self.skipped, 'errors': self.errors, 'facts': self.facts,
            'problems': self.problems, 'dirs': self.dirs, 'links': self.links,
            'bytes': sum(f['size'] for f in self.files)
                     + sum(d.get('size') or 0 for d in self.dbs),
        }

    def finish(self) -> dict:
        manifest = self.manifest()
        data = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True).encode()
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size, info.mode, info.mtime = len(data), 0o600, int(self.now.timestamp())
        self.tar.addfile(info, io.BytesIO(data))
        self.tar.close()
        return manifest


def collect(node: str, root: str, out, *, extras: Sequence[Extra] = (),
            now: Optional[dt.datetime] = None, workdir: Optional[str] = None) -> dict:
    """One node's part as a tar stream into ``out``; returns its manifest."""
    items, dbs, facts = PROFILES[node]
    col = Collector(node, root, out, now=now or utcnow())
    for db in dbs:
        col.reserve(db.path)
    for extra in extras:
        col.reserve('/' + extra.arcname)
    own_tmp = workdir is None
    if own_tmp:
        workdir = tempfile.mkdtemp(prefix='nekovpn-backup-')
    try:
        for extra in extras:
            col.add_extra(extra)
        for db in dbs:
            col.add_db(db, workdir)
        for item in items:
            col.add_item(item)
        for name, argv in facts:
            col.add_fact(name, argv)
    finally:
        if own_tmp:
            shutil.rmtree(workdir, ignore_errors=True)
    return col.finish()


# ------------------------------------------------- running other commands --

def run_framed(argv: Sequence[str], dest: str, *, magic: bytes, timeout: float, cap: int,
               stdin_data: Optional[bytes] = None) -> dict:
    """Run ``argv``; its stdout is ``magic`` + payload, and the payload goes to
    ``dest``. Text before the magic (an ssh banner) is dropped. Returns
    {rc, stderr, bytes, found, why}: ``why`` names what cut the run short."""
    proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL)
    state = {'why': None}
    err: List[bytes] = []

    def feed():
        try:
            proc.stdin.write(stdin_data)
        except OSError:
            pass
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass

    def drain():
        total = 0
        for chunk in iter(lambda: proc.stderr.read(4096), b''):
            if total < 65536:
                err.append(chunk)
                total += len(chunk)

    def expire():
        state['why'] = state['why'] or f'таймаут {int(timeout)} с'
        proc.kill()

    threads = [threading.Thread(target=drain, daemon=True)]
    if stdin_data is not None:
        threads.append(threading.Thread(target=feed, daemon=True))
    for t in threads:
        t.start()
    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    written, head, found = 0, b'', False
    try:
        with open(dest, 'wb') as out:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                if not found:
                    head += chunk
                    at = head.find(magic)
                    if at < 0:
                        if len(head) > MAGIC_WINDOW:
                            state['why'] = state['why'] or 'нет метки начала потока'
                            proc.kill()
                            break
                        continue
                    found = True
                    chunk, head = head[at + len(magic):], b''
                written += len(chunk)
                if written > cap:
                    state['why'] = state['why'] or f'больше {human(cap)}'
                    proc.kill()
                    break
                out.write(chunk)
        rc = proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:            # an exception (a stop signal) left it running
            proc.kill()
            proc.wait()
        for t in threads:
            t.join(5)
        proc.stdout.close()
        proc.stderr.close()
    return {'rc': rc, 'stderr': b''.join(err).decode('utf-8', 'replace'), 'bytes': written,
            'found': found, 'why': state['why']}


def inspect_part(path: str, node: str) -> dict:
    """List a part's tar (never extract) and read the manifest it must END
    with. Raises BackupFailure when it is not a whole part of ``node``."""
    try:
        with tarfile.open(path, 'r:') as tf:
            members = tf.getmembers()
            if not members or members[-1].name != MANIFEST_NAME:
                raise BackupFailure('часть оборвалась: нет манифеста в конце')
            last = members[-1]
            if not last.isfile() or last.size > MANIFEST_CAP:
                raise BackupFailure('манифест части не похож на манифест')
            manifest = json.loads(tf.extractfile(last).read().decode('utf-8'))
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BackupFailure(f'часть не читается как tar: {short(e, 120)}')
    except (ValueError, UnicodeDecodeError) as e:
        raise BackupFailure(f'манифест части не JSON: {short(e, 120)}')
    if not isinstance(manifest, dict) or manifest.get('node') != node:
        raise BackupFailure(f'это не часть {node}')
    return manifest


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(MB), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _part_status(manifest: dict) -> str:
    return 'ok' if not manifest.get('problems') else 'incomplete'


def fetch_exit(dest: str, args) -> dict:
    """exit's part: this file on stdin to `python3 -I -` over ssh. One retry."""
    with open(os.path.abspath(__file__), 'rb') as fh:
        script = fh.read()
    remote = ['python3', '-I', '-', '--collect', 'exit', '--root', args.exit_root]
    argv = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=4',
            args.exit_host, ' '.join(shlex.quote(a) for a in remote)]
    why = ''
    for attempt in (1, 2):
        r = run_framed(argv, dest, magic=STREAM_MAGIC, timeout=args.ssh_timeout, cap=PART_CAP,
                       stdin_data=script)
        if r['rc'] == 0 and r['found'] and not r['why']:
            try:
                manifest = inspect_part(dest, 'exit')
            except BackupFailure as e:
                why = str(e)
            else:
                return {'status': _part_status(manifest), 'manifest': manifest,
                        'size': os.path.getsize(dest), 'sha256': _sha256_file(dest)}
        else:
            tail = short(r['stderr'].strip().splitlines()[-1] if r['stderr'].strip() else '', 160)
            why = r['why'] or (f'ssh rc={r["rc"]}: {tail}' if tail else f'ssh rc={r["rc"]}')
            if not r['found'] and not r['why'] and r['rc'] == 0:
                why = 'нет метки начала потока'
        log(f'exit: попытка {attempt} не удалась: {why}')
        if attempt == 1 and args.ssh_retry_wait:
            time.sleep(args.ssh_retry_wait)
    try:
        os.unlink(dest)
    except OSError:
        pass
    return {'status': 'missing', 'error': f'exit: {why}'}


def snapshot_bot_db(dest: str, args) -> dict:
    """bot.db's consistent copy from inside the container (module docstring)."""
    argv = ['docker', 'exec', args.bot_container, 'python3', '-I', '-c', SNAPSHOT_CODE, args.bot_db]
    r = run_framed(argv, dest, magic=DB_MAGIC, timeout=DOCKER_TIMEOUT_S, cap=PART_CAP)
    if r['rc'] != 0 or not r['found'] or r['why']:
        tail = r['stderr'].strip().splitlines()[-1] if r['stderr'].strip() else ''
        why = r['why'] or (short(tail, 160) or f'docker exec rc={r["rc"]}')
        try:
            os.unlink(dest)
        except OSError:
            pass
        return {'status': 'missing', 'error': f'bot.db: {why}'}
    check = check_sqlite(dest)
    out = {'status': 'ok', 'path': dest, 'check': check, 'size': check['size']}
    if check['integrity'] != 'ok':
        out.update(status='incomplete', error=f'bot.db: integrity_check — {check["integrity"]}')
    return out


# ----------------------------------------------------- README / MANIFEST ----

RESTORE_ORDER = """\
## Порядок восстановления

Подробно, с командами и проверками, — `docs/runbooks/backup_restore.md` в репо. Коротко:

1. **Проверить архив.** `PRAGMA integrity_check` у копий bot.db и x-ui.db (`ok` записан и в
   MANIFEST.json), sha256 каждого файла — там же.
2. **exit** (сервер за рубежом), если пропал он:
   1. ключи хоста `etc/ssh/ssh_host_*` до первого входа — тот же отпечаток;
   2. docker; `opt/vpn-bot/docker-compose.yml` и `.env`; 3x-ui остановлен →
      `x-ui.db` в том `vpn-bot_3xui-data` → `docker compose up -d 3x-ui` (образ — по digest из
      compose; xray не новее 26.7.10, §51);
   3. hysteria: `etc/hysteria/*`, юниты и drop-in `entry-hop`, `etc/nekovpn-hop/*`, релей hy2
      (`install_hy2_auth_relay.sh`, `etc/hy2-auth-relay.env`);
   4. Caddy (`etc/caddy`, `var/lib/caddy/...`), acme.sh (`root/.acme.sh`, `etc/ssl/sub`,
      `etc/ssl/cdn`, crontab), tinyproxy (`etc/tinyproxy`), правила (`etc/iptables`, `etc/ufw`,
      `usr/local/bin/exit_firewall.sh`);
   5. xhr-exit и ssh-exit: `etc/xhr-exit*`, `etc/ssh-exit*`, затем `setup_xhr_exit.sh`,
      `setup_ssh_exit.sh` — ключи берутся из этих каталогов, клиентам ничего менять не надо;
   6. репортёр и таймеры (`etc/exit-dpi.env`, юниты из `etc/systemd/system`).
3. **entry** (сервер в РФ), если пропал он:
   1. ключи хоста, `root/.ssh` (алиас `exit-node` и ключ к exit), docker, haproxy
      (`etc/haproxy`), shadow-tls (юнит и `nekovpn-facts/systemctl-cat.txt`), `etc/iptables`;
   2. `/opt/vpn-bot` = код из репо (`scripts/deploy_to_entry.sh`) + `.env` и
      `docker-compose.yml` из архива;
   3. том `vpn-bot_vpn-bot-data` при остановленном боте: `bot.db`, `secrets/`, `hop-tls/` с
      владельцем 1000:1000 (`tar --numeric-owner`); затем бот (`deploy_entry_bot.sh`,
      всегда `--no-deps`);
   4. RU-зона (`etc/ru-exit` → `setup_ru_exit.sh`), ssh-relay (`etc/ssh-relay` →
      `setup_ssh_entry_relay.sh`), сайдкары проб (`opt/vpn-bot/probe-proxy*`), таймеры
      (`install_protocol_healthcheck.sh`, `install_service_backup.sh`);
   5. Hermes: `root/.hermes/{config.yaml,.env,SOUL.md,memories,skills}`,
      `root/hermes-work/AGENTS.md`; версия — `nekovpn-facts/hermes-pip-freeze.txt`.
4. **Новый адрес** entry или exit: `ENTRY_NODE_IP` / `EXIT_NODE_IP` в .env, DNS доменов, правила
   DOCKER-USER (`entry-fw-*`); при новом адресе entry — новый сертификат перегона
   (`hop_tls.py init --force --ip`) и его пин на exit (§53).
"""


def build_manifest(stem: str, created: dt.datetime, parts: Dict[str, dict], dry_run: bool) -> dict:
    out = {'format': FORMAT, 'archive': stem, 'created': iso(created),
           'made_on': socket.gethostname(), 'dry_run': dry_run, 'parts': {}}
    for name, part in parts.items():
        entry = {k: v for k, v in part.items() if k not in ('path', 'check')}
        out['parts'][name] = entry
    out['complete'] = all(p.get('status') == 'ok' for p in parts.values())
    return out


def _dbs_of(manifest: dict) -> List[dict]:
    dbs = []
    for name in ('entry', 'exit'):
        part = manifest['parts'].get(name) or {}
        for db in (part.get('manifest') or {}).get('databases') or []:
            dbs.append(dict(db, node=name))
    return dbs


def render_readme(manifest: dict) -> str:
    stem = manifest['archive']
    created = manifest['created'].replace('T', ' ').replace('Z', ' UTC')
    mark = {'ok': '✅', 'incomplete': '⚠️', 'missing': '❌'}
    lines = [f'# Бэкап NekoVPN — {created}', '',
             f'Сделан `scripts/service_backup.py` на entry (`{manifest.get("made_on")}`), '
             'зашифрован age открытым ключом владельца. AGENTS.md §58.', '',
             '| Часть | Состояние | Что внутри |', '|---|---|---|']
    descr = {
        'entry': ('`entry/entry.tar`', 'файлы entry по их путям: том бота (bot.db — согласованная '
                  'копия), .env, compose, haproxy, shadow-tls, правила, RU-зона, юниты, Hermes; '
                  '`nekovpn-facts/` — версии, живые правила, контейнеры'),
        'bot': ('bot.db в `entry/entry.tar`', 'копия снята в контейнере vpn-bot через backup API sqlite'),
        'exit': ('`exit/exit.tar`', 'файлы exit по их путям: x-ui.db (копия), compose, .env, '
                 'hysteria, Caddy, пин перегона, xhr-exit, ssh-exit, tinyproxy, acme.sh; '
                 '`nekovpn-facts/`'),
    }
    for name in ('entry', 'bot', 'exit'):
        part = manifest['parts'].get(name)
        if not part:
            continue
        where, what = descr[name]
        state = mark.get(part.get('status'), '?')
        if part.get('error'):
            state += ' ' + part['error']
        problems = (part.get('manifest') or {}).get('problems') or []
        if problems:
            state += ' ' + '; '.join(problems[:5])
        lines.append(f'| {where} | {state} | {what} |')
    lines += ['', '## Распаковать (на машине владельца)', '', '```bash',
              f'age -d -i ~/.config/nekovpn-signing/nekovpn-backup-age.key {stem}.tar.zst.age \\',
              '    | zstd -d | tar -x',
              'mkdir -p restore/entry restore/exit',
              f'tar -xpf {stem}/entry/entry.tar -C restore/entry --numeric-owner',
              f'tar -xpf {stem}/exit/exit.tar -C restore/exit --numeric-owner',
              '```', '',
              'Архив из нескольких частей (`.001`, `.002`…) сначала склеить: '
              f'`cat {stem}.tar.zst.age.[0-9][0-9][0-9] > {stem}.tar.zst.age`. '
              'Внутри части каждый файл лежит по своему пути от корня; '
              f'`{MANIFEST_NAME}` в конце части — её список с sha256.', '']
    dbs = _dbs_of(manifest)
    if dbs:
        lines += ['## Базы', '']
        for db in dbs:
            tables = db.get('tables') or {}
            users = f', users: {tables["users"]}' if 'users' in tables else ''
            clients = f', clients: {tables["clients"]}' if 'clients' in tables else ''
            lines.append(f'- {db["node"]}: `{db["path"]}` — {db.get("status")}, '
                         f'{human(db.get("size"))}, integrity_check: {db.get("integrity")}'
                         f'{users}{clients}. {db.get("what", "")}')
        lines.append('')
    for name in ('entry', 'exit'):
        part = manifest['parts'].get(name) or {}
        pm = part.get('manifest')
        if not pm:
            continue
        lines += [f'## Что где: {name}', '', '| Путь | Что это | Файлов | Есть |', '|---|---|---|---|']
        for item in pm.get('items') or []:
            have = {'ok': '✅', 'absent': '—'}.get(item.get('status'), item.get('status'))
            if item.get('status') == 'absent' and item.get('required'):
                have = '❌ нет'
            lines.append(f'| `{item["path"]}` | {item["what"]} | {item.get("files", 0)} '
                         f'({human(item.get("bytes"))}) | {have} |')
        skipped = pm.get('skipped') or []
        if skipped:
            lines += ['', f'Не вошли ({len(skipped)}):', '']
            lines += [f'- `{s["path"]}` — {s["why"]} ({human(s.get("size"))})' for s in skipped[:60]]
            if len(skipped) > 60:
                lines.append(f'- … и ещё {len(skipped) - 60} (все — в {MANIFEST_NAME})')
        errors = pm.get('errors') or []
        if errors:
            lines += ['', f'Не прочитались ({len(errors)}):', '']
            lines += [f'- `{e["path"]}` — {e["error"]}' for e in errors[:30]]
        lines.append('')
    lines.append(RESTORE_ORDER)
    return '\n'.join(lines)


def sender_meta(manifest: dict, size: int, sha256: str) -> dict:
    """What the bot's caption and state need, nothing else: statuses, short
    errors and counts — no paths of secrets, no contents."""
    parts = {}
    for name, part in manifest['parts'].items():
        pm = part.get('manifest') or {}
        problems = [short(p, 120) for p in (part.get('error'),) if p]
        problems += [short(p, 120) for p in (pm.get('problems') or [])[:3]]
        entry = {'status': part.get('status'), 'problems': problems}
        if pm:
            entry.update(files=len(pm.get('files') or []), bytes=pm.get('bytes'))
        parts[name] = entry
    for db in _dbs_of(manifest):
        target = 'bot' if db['path'].endswith('/bot.db') else db['node']
        tables = db.get('tables') or {}
        if target in parts:
            parts[target].setdefault('dbs', []).append({
                'name': os.path.basename(db['path']), 'integrity': db.get('integrity'),
                'size': db.get('size'), 'users': tables.get('users'),
                'clients': tables.get('clients')})
    return {'created': manifest['created'], 'complete': manifest['complete'], 'parts': parts,
            'size': size, 'sha256': sha256}


# ------------------------------------------------------------- the run ----

def read_recipients(path: str) -> List[str]:
    try:
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
    except OSError as e:
        raise BackupFailure(f'нет файла получателей {path}: {short(e, 80)}')
    if 'AGE-SECRET-KEY-' in text.upper():
        raise BackupFailure(f'в {path} лежит СЕКРЕТНЫЙ ключ age: ему не место на сервере — '
                            'удалите файл, положите только открытый ключ (age1…)')
    recipients = [line.strip() for line in text.splitlines()
                  if line.strip() and not line.strip().startswith('#')]
    if not recipients:
        raise BackupFailure(f'в {path} нет открытого ключа age')
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if line and not line.startswith('#') and not AGE_RECIPIENT.match(line):
            # The line itself is not echoed: it may be half of a pasted secret.
            raise BackupFailure(f'в {path} строка {n} — не ключ age (нужен открытый, age1…)')
    return recipients


def pack_and_encrypt(tree: str, out_path: str, recipients: str, level: int) -> None:
    """tar (in-process, our own staging files) → zstd → age → out_path."""
    zstd = subprocess.Popen(['zstd', '-q', f'-{level}', '-T1', '-c'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    age = subprocess.Popen(['age', '-R', recipients, '-o', out_path], stdin=zstd.stdout)
    zstd.stdout.close()
    broke = None
    try:
        try:
            with tarfile.open(fileobj=zstd.stdin, mode='w|', format=tarfile.PAX_FORMAT) as tar:
                tar.add(tree, arcname=os.path.basename(tree))
        except (BrokenPipeError, OSError) as e:
            broke = e
        finally:
            try:
                zstd.stdin.close()
            except OSError:
                pass
        try:
            zrc, arc = zstd.wait(timeout=1800), age.wait(timeout=1800)
        except subprocess.TimeoutExpired:
            raise BackupFailure('zstd/age не закончили за 30 мин')
    except BaseException:
        for proc in (zstd, age):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        raise
    if zrc or arc or broke:
        raise BackupFailure(f'упаковка не удалась: zstd rc={zrc}, age rc={arc}'
                            + (f', {short(broke, 80)}' if broke else ''))
    with open(out_path, 'rb') as fh:
        if fh.read(len(AGE_HEADER)) != AGE_HEADER:
            raise BackupFailure('на выходе не файл age')
    os.chmod(out_path, 0o600)


def prune(archives: str, keep: int) -> List[str]:
    removed = []
    for stale in glob.glob(os.path.join(archives, '*.partial')):
        os.unlink(stale)
        removed.append(stale)
    done = sorted(glob.glob(os.path.join(archives, 'nekovpn-backup-*.tar.zst.age')))
    for old in done[:max(0, len(done) - keep)]:
        os.unlink(old)
        removed.append(old)
    return removed


def send_archive(path: str, name: str, sha256: str, meta: dict, args) -> dict:
    argv = ['docker', 'exec', '-i', args.bot_container, 'python3', SENDER, 'send',
            '--name', name, '--sha256', sha256,
            '--meta-json', json.dumps(meta, ensure_ascii=False, separators=(',', ':'))]
    try:
        with open(path, 'rb') as fh:
            r = subprocess.run(argv, stdin=fh, capture_output=True, timeout=SEND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise BackupFailure(f'бот не отправил архив за {SEND_TIMEOUT_S // 60} мин')
    result = None
    for line in reversed(r.stdout.decode('utf-8', 'replace').splitlines()):
        if line.startswith('{'):
            try:
                result = json.loads(line)
            except ValueError:
                pass
            break
    if r.returncode == 0 and isinstance(result, dict) and result.get('ok'):
        return result
    why = (result or {}).get('error') if isinstance(result, dict) else None
    if not why:
        err = r.stderr.decode('utf-8', 'replace').strip().splitlines()
        why = err[-1] if err else f'rc={r.returncode}'
        if "can't open file" in why or 'No such file' in why:
            why = f'в образе бота нет {SENDER}: сначала задеплойте бота'
    raise BackupFailure(f'отправка: {short(why, 200)}')


def report_failure(reason: str, args) -> None:
    """Best effort: the bot records the failed run (the alert shows it)."""
    if args.dry_run or args.no_send:
        return
    argv = ['docker', 'exec', args.bot_container, 'python3', SENDER, 'fail', '--reason', short(reason, 300)]
    try:
        r = subprocess.run(argv, capture_output=True, timeout=120)
        if r.returncode != 0:
            log(f'бот не записал сбой (rc={r.returncode})')
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f'бот не записал сбой: {short(e, 120)}')


def summary_lines(manifest: dict) -> List[str]:
    out = []
    for name in ('exit', 'bot', 'entry'):
        part = manifest['parts'].get(name)
        if not part:
            continue
        pm = part.get('manifest') or {}
        text = f'{name}: {part.get("status")}'
        if pm:
            text += f', файлов {len(pm.get("files") or [])}, {human(pm.get("bytes"))}'
            text += (f', нет на хосте {sum(1 for i in pm.get("items") or [] if i.get("status") == "absent")}'
                     f', пропущено {len(pm.get("skipped") or [])}, ошибок {len(pm.get("errors") or [])}')
        if name == 'bot' and part.get('size'):
            text += f', {human(part.get("size"))}'
        if part.get('error'):
            text += f' — {part["error"]}'
        for p in pm.get('problems') or []:
            text += f' — {p}'
        out.append(text)
    for db in _dbs_of(manifest):
        users = (db.get('tables') or {}).get('users')
        out.append(f'{db["node"]}: {db["path"]}: {db.get("status")}, {human(db.get("size"))}, '
                   f'integrity {db.get("integrity")}' + (f', users {users}' if users is not None else ''))
    return out


def dry_run_listing(manifest: dict) -> List[str]:
    out = []
    for name in ('entry', 'exit'):
        pm = (manifest['parts'].get(name) or {}).get('manifest') or {}
        for item in pm.get('items') or []:
            out.append(f'  {name} {item["status"]:<7} {item.get("files", 0):>5} '
                       f'{human(item.get("bytes")):>9}  {item["path"]}')
        for s in pm.get('skipped') or []:
            out.append(f'  {name} skipped           {human(s.get("size")):>9}  {s["path"]} ({s["why"]})')
        for e in pm.get('errors') or []:
            out.append(f'  {name} error                       {e["path"]} ({e["error"]})')
    return out


def _stopped(signum, frame):
    """systemd's stop (the unit's timeout, a shutdown) sends SIGTERM, whose
    default kills Python without running a single `finally`: the plaintext
    work directory (exit's secrets too) would stay on entry until the next
    run. As an exception it unwinds: children killed, work dir removed, the
    bot told. A second signal during the cleanup is ignored."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    raise BackupFailure(f'прерван сигналом {signal.Signals(signum).name}: '
                        'таймаут юнита или остановка')


def acquire_lock(state_dir: str):
    fh = open(os.path.join(state_dir, '.lock'), 'w')
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise BackupFailure('другой запуск бэкапа ещё идёт')
    return fh


def run(args) -> int:
    created = utcnow()
    stem = 'nekovpn-backup-' + created.strftime('%Y%m%dT%H%M%SZ')
    signal.signal(signal.SIGTERM, _stopped)
    os.makedirs(args.state_dir, mode=0o700, exist_ok=True)
    os.chmod(args.state_dir, 0o700)
    try:
        lock = acquire_lock(args.state_dir)
    except BackupFailure as e:
        log(str(e))
        return 2
    try:
        return _run_locked(args, created, stem)
    except BackupFailure as e:
        log(f'не удалось: {e}')
        report_failure(str(e), args)
        return 2
    except Exception as e:          # noqa: BLE001 - the bot must hear of any failure
        traceback.print_exc()
        report_failure(f'сбой скрипта: {short(repr(e), 200)}', args)
        return 2
    finally:
        lock.close()


def _run_locked(args, created: dt.datetime, stem: str) -> int:
    if not args.dry_run:
        read_recipients(args.recipients)
        for tool in ('zstd', 'age'):
            if shutil.which(tool) is None:
                raise BackupFailure(f'на entry нет {tool}: apt-get install {tool}')
    for old in glob.glob(os.path.join(args.state_dir, 'work-*')):
        shutil.rmtree(old, ignore_errors=True)
    work = os.path.join(args.state_dir, f'work-{stem}')
    os.makedirs(work, mode=0o700)
    try:
        return _build(args, created, stem, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _build(args, created: dt.datetime, stem: str, work: str) -> int:
    tree = os.path.join(work, stem)
    for sub in ('entry', 'exit'):
        os.makedirs(os.path.join(tree, sub))
    parts: Dict[str, dict] = {}

    parts['exit'] = fetch_exit(os.path.join(tree, 'exit', 'exit.tar'), args)
    log(f'exit: {parts["exit"]["status"]}' + (f' — {parts["exit"].get("error")}'
                                                if parts['exit'].get('error') else ''))

    bot = snapshot_bot_db(os.path.join(work, 'bot.db'), args)
    parts['bot'] = {k: v for k, v in bot.items() if k not in ('path', 'check')}
    if bot.get('check'):
        parts['bot']['integrity'] = bot['check'].get('integrity')
    log(f'bot.db: {bot["status"]}' + (f' — {bot.get("error")}' if bot.get('error') else
                                       f', {human(bot.get("size"))}'))

    extras = []
    if bot.get('path'):
        extras.append(Extra(arcname=(BOT_VOLUME + '/bot.db').lstrip('/'), src=bot['path'],
                            like=BOT_VOLUME + '/bot.db', what='bot.db: согласованная копия '
                            '(backup API sqlite в контейнере vpn-bot)', check=bot.get('check')))
    entry_tar = os.path.join(tree, 'entry', 'entry.tar')
    dbwork = os.path.join(work, 'db')
    os.makedirs(dbwork)
    with open(entry_tar, 'wb') as out:
        manifest = collect('entry', args.root, out, extras=extras, now=created, workdir=dbwork)
    if bot.get('path'):
        os.unlink(bot['path'])
    parts['entry'] = {'status': _part_status(manifest), 'manifest': manifest,
                      'size': os.path.getsize(entry_tar), 'sha256': _sha256_file(entry_tar)}

    full = build_manifest(stem, created, {'entry': parts['entry'], 'bot': parts['bot'],
                                          'exit': parts['exit']}, args.dry_run)
    with open(os.path.join(tree, 'MANIFEST.json'), 'w', encoding='utf-8') as fh:
        json.dump(full, fh, ensure_ascii=False, indent=1, sort_keys=True)
    with open(os.path.join(tree, 'README.md'), 'w', encoding='utf-8') as fh:
        fh.write(render_readme(full))
    for line in summary_lines(full):
        log(line)

    if args.dry_run:
        print('\n'.join(dry_run_listing(full)))
        log('dry run: ничего не зашифровано, не сохранено и не отправлено')
        return 0

    archives = os.path.join(args.state_dir, 'archives')
    os.makedirs(archives, mode=0o700, exist_ok=True)
    name = f'{stem}.tar.zst.age'
    partial = os.path.join(archives, name + '.partial')
    final = os.path.join(archives, name)
    pack_and_encrypt(tree, partial, args.recipients, args.zstd_level)
    os.replace(partial, final)
    size, sha = os.path.getsize(final), _sha256_file(final)
    log(f'архив: {name}, {human(size)}, sha256 {sha}')
    for gone in prune(archives, args.keep):
        log(f'удалён старый: {os.path.basename(gone)}')
    if args.no_send:
        log(f'--no-send: архив лежит в {final}')
        return 0
    try:
        result = send_archive(final, name, sha, sender_meta(full, size, sha), args)
    except BackupFailure as e:
        raise BackupFailure(f'{e}; архив собран и лежит на entry: {final}')
    log(f'отправлен: частей {result.get("parts")}, сообщения {result.get("message_ids")}, '
        f'топик {result.get("thread_id")}')
    return 0 if full['complete'] else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--dry-run', action='store_true',
                    help='collect and list; encrypt, keep and send nothing')
    ap.add_argument('--no-send', action='store_true', help='keep the archive locally, do not send')
    ap.add_argument('--collect', choices=sorted(PROFILES),
                    help='(internal) write this node\'s part to stdout')
    ap.add_argument('--root', default='/', help='(tests) read the node\'s files under this root')
    ap.add_argument('--exit-root', default='/', help='(tests) --root for the exit collector')
    ap.add_argument('--state-dir', default=DEFAULT_STATE_DIR)
    ap.add_argument('--recipients', default=DEFAULT_RECIPIENTS)
    ap.add_argument('--exit-host', default=EXIT_SSH_HOST)
    ap.add_argument('--bot-container', default=BOT_CONTAINER)
    ap.add_argument('--bot-db', default=BOT_DB, help='bot.db inside the container')
    ap.add_argument('--zstd-level', type=int, default=ZSTD_LEVEL)
    ap.add_argument('--keep', type=int, default=KEEP_LOCAL)
    ap.add_argument('--ssh-timeout', type=float, default=SSH_TIMEOUT_S)
    ap.add_argument('--ssh-retry-wait', type=float, default=SSH_RETRY_WAIT_S)
    args = ap.parse_args(argv)
    os.umask(0o077)
    if args.collect:
        out = sys.stdout.buffer
        out.write(STREAM_MAGIC)
        collect(args.collect, args.root, out)
        out.flush()
        return 0
    return run(args)


if __name__ == '__main__':
    sys.exit(main())
