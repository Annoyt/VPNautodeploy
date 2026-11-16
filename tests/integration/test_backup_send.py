"""The bot's side of the daily service backup (AGENTS.md §58):
bot/services/backups.py and scripts/backup_send.py, on a real sqlite (the
bot's own schema), with a fake Telegram client.

Pinned: only an age file is sent (a plaintext archive never reaches the
group); the «Бэкапы» topic is made once, stored, and made again only when
Telegram says it is gone or the group changed; an archive over the Bot API
limit goes in numbered parts; what the AlertManager check reads
(backup_state) says complete / partial / failed; a row that cannot be read
is never written over; nothing prints the archive.
"""

import hashlib
import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from bot.core.database import Database
from bot.services import backups

REPO = Path(__file__).resolve().parents[2]
CHAT = '-1003686477257'
NAME = 'nekovpn-backup-20261117T014012Z.tar.zst.age'
NOW = datetime(2026, 11, 17, 1, 41, tzinfo=timezone.utc)


def archive(size=3000):
    body = backups.AGE_HEADER + b'-> X25519 stanza\n' + bytes(range(256)) * (size // 256 + 1)
    return body[:size]


def meta(complete=True, **parts):
    base = {'entry': {'status': 'ok', 'files': 412, 'problems': []},
            'bot': {'status': 'ok', 'problems': [],
                    'dbs': [{'name': 'bot.db', 'integrity': 'ok', 'size': 70 << 20,
                             'users': 160, 'clients': None}]},
            'exit': {'status': 'ok', 'files': 233, 'problems': [],
                     'dbs': [{'name': 'x-ui.db', 'integrity': 'ok', 'size': 512 << 10,
                              'users': None, 'clients': 88}]}}
    base.update(parts)
    return {'created': '2026-11-17T01:40:12Z', 'complete': complete, 'parts': base}


class FakeTg:
    def __init__(self):
        self.topics, self.messages, self.docs = [], [], []
        self.next_thread = 500
        self.fail = None          # (thread_id, filename) -> exception or None
        self.create_ok = True

    def create_forum_topic(self, chat_id, name, **kw):
        if not self.create_ok:
            return None
        self.next_thread += 1
        self.topics.append((chat_id, name, self.next_thread))
        return self.next_thread

    def send_message(self, chat_id, text, message_thread_id=None, **kw):
        self.messages.append((chat_id, message_thread_id, text))
        return {'message_id': 1}

    def send_document_bytes(self, chat_id, filename, content, caption=None, parse_mode=None,
                            raise_errors=False, **kwargs):
        assert raise_errors is True and parse_mode is None
        thread = kwargs['message_thread_id']
        if self.fail:
            err = self.fail(thread, filename)
            if err:
                raise err
        self.docs.append({'chat_id': chat_id, 'filename': filename, 'content': content,
                          'caption': caption, 'thread': thread})
        return {'message_id': 1000 + len(self.docs)}


def tg_error(description):
    e = requests.RequestException('400 Client Error: Bad Request for url: https://api…')
    e.tg_description = description
    return e


@pytest.fixture
def db_path(tmp_path):
    db = Database(str(tmp_path / 'bot.db'))
    return db.db_path


@pytest.fixture
def tg():
    return FakeTg()


def send(tg, db_path, data=None, name=NAME, m=None, sha=None, chat=CHAT, now=NOW):
    data = archive() if data is None else data
    return backups.send_backup(tg, db_path, chat, data, name,
                               sha or hashlib.sha256(data).hexdigest(),
                               meta() if m is None else m, now=now)


def state(db_path):
    return backups.load_state(db_path)


# ================================================================ sending ==

class TestSend:

    def test_first_send_makes_the_topic_once(self, tg, db_path):
        r = send(tg, db_path)
        assert r == {'ok': True, 'thread_id': 501, 'message_ids': [1001], 'parts': 1,
                     'complete': True}
        assert tg.topics == [(CHAT, 'Бэкапы', 501)]
        (intro,) = tg.messages
        assert intro[1] == 501 and 'зашифрованный бэкап' in intro[2]
        (doc,) = tg.docs
        assert doc['filename'] == NAME and doc['thread'] == 501 and doc['chat_id'] == CHAT
        assert doc['content'] == archive()
        assert backups.load(db_path, backups.TOPIC_KEY) == {
            'chat_id': CHAT, 'thread_id': 501, 'created': '2026-11-17T01:41:00Z'}
        send(tg, db_path, now=NOW + timedelta(days=1))
        assert len(tg.topics) == 1 and [d['thread'] for d in tg.docs] == [501, 501]

    def test_the_caption(self, tg, db_path):
        data = archive()
        send(tg, db_path, data=data)
        cap = tg.docs[0]['caption']
        assert cap.splitlines() == [
            '🗄 Бэкап NekoVPN · 17.11.2026 01:40 UTC',
            '✅ entry: файлов 412',
            '✅ bot.db: bot.db 70.0 МБ, integrity ok, юзеров 160',
            '✅ exit: x-ui.db 512.0 КБ, integrity ok, клиентов 88; файлов 233',
            f'{NAME} · 2.9 КБ',
            f'sha256 {hashlib.sha256(data).hexdigest()}',
            'Расшифровка: docs/runbooks/backup_restore.md',
        ]

    def test_a_partial_archive_says_so(self, tg, db_path):
        m = meta(complete=False, exit={'status': 'missing',
                                       'problems': ['exit: ssh rc=255: Connection timed out']})
        r = send(tg, db_path, m=m)
        assert r['complete'] is False
        assert '❌ exit: exit: ssh rc=255: Connection timed out' in tg.docs[0]['caption']
        s = state(db_path)
        assert 'last_ok' not in s
        assert s['last_sent']['missing'] == {'exit': 'exit: ssh rc=255: Connection timed out'}
        assert s['last_run'] == {'ts': '2026-11-17T01:41:00Z', 'status': 'partial',
                                 'error': 'exit: exit: ssh rc=255: Connection timed out'}

    def test_complete_flag_alone_is_not_enough(self, tg, db_path):
        """A meta claiming complete with a part not ok is partial."""
        send(tg, db_path, m=meta(complete=True, bot={'status': 'incomplete', 'problems': ['x']}))
        assert 'last_ok' not in state(db_path)
        send(tg, db_path, m={'created': 'x', 'complete': True, 'parts': {}})
        assert state(db_path)['last_sent']['missing'] == {
            'entry': 'нет в архиве', 'bot': 'нет в архиве', 'exit': 'нет в архиве'}

    def test_a_complete_send_is_recorded(self, tg, db_path):
        data = archive()
        send(tg, db_path, data=data)
        s = state(db_path)
        assert s['last_ok'] == s['last_sent'] == {
            'ts': '2026-11-17T01:41:00Z', 'name': NAME, 'size': len(data),
            'sha256': hashlib.sha256(data).hexdigest(), 'message_ids': [1001],
            'chat_id': CHAT, 'thread_id': 501, 'missing': {}}
        assert s['last_run'] == {'ts': '2026-11-17T01:41:00Z', 'status': 'ok', 'error': None}

    @pytest.mark.parametrize('data,why', [
        (b'PK\x03\x04 a zip, not age', 'не файл age'),
        (b'', 'не файл age'),
        (b'NEKOVPN-BACKUP-STREAM-1\nplain tar', 'не файл age'),
    ])
    def test_never_anything_but_age(self, tg, db_path, data, why):
        with pytest.raises(backups.BackupError, match=why):
            send(tg, db_path, data=data)
        assert not tg.docs and not tg.topics and state(db_path) is None

    def test_a_cut_archive(self, tg, db_path):
        data = archive()
        with pytest.raises(backups.BackupError, match='sha256 не совпал'):
            send(tg, db_path, data=data[:-1], sha=hashlib.sha256(data).hexdigest())
        assert not tg.docs and state(db_path) is None

    @pytest.mark.parametrize('name', ['bot.db', 'nekovpn-backup-x.tar.zst.age',
                                      '../nekovpn-backup-20261117T014012Z.tar.zst.age', ''])
    def test_a_strange_name(self, tg, db_path, name):
        with pytest.raises(backups.BackupError, match='имя архива'):
            send(tg, db_path, name=name)
        assert not tg.docs

    def test_no_group(self, tg, db_path):
        with pytest.raises(backups.BackupError, match='FORUM_GROUP_ID'):
            send(tg, db_path, chat='')

    def test_no_right_to_make_topics(self, tg, db_path):
        tg.create_ok = False
        with pytest.raises(backups.BackupError,
                           match='нет связи с Telegram или у бота нет права Manage Topics'):
            send(tg, db_path)
        assert backups.load(db_path, backups.TOPIC_KEY) is None and state(db_path) is None

    def test_a_deleted_topic_is_made_again(self, tg, db_path):
        send(tg, db_path)
        tg.fail = lambda thread, _: (tg_error('Bad Request: message thread not found')
                                     if thread == 501 else None)
        r = send(tg, db_path, now=NOW + timedelta(days=1))
        assert r['thread_id'] == 502 and [t[2] for t in tg.topics] == [501, 502]
        assert tg.docs[-1]['thread'] == 502
        assert backups.stored_topic(db_path, CHAT) == 502
        assert state(db_path)['last_ok']['thread_id'] == 502

    def test_a_dead_proxy_never_makes_a_topic(self, tg, db_path):
        send(tg, db_path)
        tg.fail = lambda *_: tg_error('')
        with pytest.raises(backups.BackupError, match='^Telegram: '):
            send(tg, db_path, now=NOW + timedelta(days=1))
        assert len(tg.topics) == 1
        assert state(db_path)['last_ok']['ts'] == '2026-11-17T01:41:00Z'    # untouched

    def test_a_closed_topic(self, tg, db_path):
        tg.fail = lambda *_: tg_error('Bad Request: TOPIC_CLOSED')
        with pytest.raises(backups.BackupError, match='закрыт'):
            send(tg, db_path)
        assert len(tg.topics) == 1

    def test_another_group_gets_its_own_topic(self, tg, db_path):
        send(tg, db_path)
        send(tg, db_path, chat='-1009999999999')
        assert [(t[0], t[2]) for t in tg.topics] == [(CHAT, 501), ('-1009999999999', 502)]

    def test_over_the_limit_goes_in_numbered_parts(self, tg, db_path, monkeypatch):
        monkeypatch.setattr(backups, 'PART_BYTES', 1000)
        data = archive(2500)
        r = send(tg, db_path, data=data)
        assert r['parts'] == 3 and r['message_ids'] == [1001, 1002, 1003]
        assert [d['filename'] for d in tg.docs] == [f'{NAME}.001', f'{NAME}.002', f'{NAME}.003']
        assert b''.join(d['content'] for d in tg.docs) == data
        assert all(len(d['content']) <= 1000 for d in tg.docs)
        assert f'Часть 2 из 3: собрать — cat {NAME}.[0-9][0-9][0-9] > {NAME}' in \
            tg.docs[1]['caption']
        assert state(db_path)['last_ok']['message_ids'] == [1001, 1002, 1003]

    def test_exactly_one_part(self, tg, db_path, monkeypatch):
        monkeypatch.setattr(backups, 'PART_BYTES', 2000)
        r = send(tg, db_path, data=archive(2000))
        assert r['parts'] == 1 and tg.docs[0]['filename'] == NAME
        assert 'Часть' not in tg.docs[0]['caption']

    def test_the_caption_fits_whatever_the_problems(self, tg, db_path):
        long = 'x' * 500
        m = meta(complete=False, **{p: {'status': 'missing', 'problems': [long] * 3}
                                    for p in ('entry', 'bot', 'exit')})
        send(tg, db_path, m=m)
        cap = tg.docs[0]['caption']
        assert len(cap) <= 1024
        assert cap.splitlines()[-1] == 'Расшифровка: docs/runbooks/backup_restore.md'


class TestState:

    def test_a_failure_keeps_the_last_good(self, tg, db_path):
        send(tg, db_path)
        backups.record_failure(db_path, 'отправка: Telegram: chat not found',
                               now=NOW + timedelta(days=1))
        s = state(db_path)
        assert s['last_ok']['ts'] == '2026-11-17T01:41:00Z'
        assert s['last_run'] == {'ts': '2026-11-18T01:41:00Z', 'status': 'failed',
                                 'error': 'отправка: Telegram: chat not found'}

    def test_installed_and_forget(self, db_path):
        backups.mark_installed(db_path, now=NOW)
        backups.mark_installed(db_path, now=NOW + timedelta(days=3))     # keeps the first
        assert state(db_path) == {'installed': '2026-11-17T01:41:00Z'}
        backups.forget(db_path)
        assert state(db_path) is None

    def test_forget_keeps_the_topic(self, tg, db_path):
        send(tg, db_path)
        backups.forget(db_path)
        assert backups.stored_topic(db_path, CHAT) == 501

    def test_a_damaged_row_starts_over(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('backup_state', '{oops')")
        conn.commit()
        conn.close()
        backups.record_failure(db_path, 'x', now=NOW)
        assert state(db_path) == {'last_run': {'ts': '2026-11-17T01:41:00Z',
                                               'status': 'failed', 'error': 'x'}}

    def test_a_locked_db_is_never_written_over(self, tg, db_path, monkeypatch):
        send(tg, db_path)
        before = backups.load(db_path, backups.STATE_KEY)
        real = backups._connect
        monkeypatch.setattr(backups, '_connect',
                            lambda p: sqlite3.connect(p, timeout=0.2, isolation_level=None))
        holder = real(db_path)
        holder.execute('BEGIN EXCLUSIVE')
        try:
            with pytest.raises(sqlite3.OperationalError):
                backups.record_failure(db_path, 'x', now=NOW + timedelta(days=1))
        finally:
            holder.execute('ROLLBACK')
            holder.close()
        assert backups.load(db_path, backups.STATE_KEY) == before


# ===================================================================== CLI ==

def cli(argv, db_path, tg=None, data=b'', chat=CHAT, now=NOW):
    import importlib.util
    spec = importlib.util.spec_from_file_location('backup_send', REPO / 'scripts' / 'backup_send.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = io.StringIO()
    rc = module.main(argv, db_path=db_path, client=tg, chat_id=chat, stdin=io.BytesIO(data),
                     out=out, now=now)
    return rc, out.getvalue()


class TestCli:

    def test_send(self, tg, db_path):
        data = archive()
        rc, out = cli(['send', '--name', NAME, '--sha256', hashlib.sha256(data).hexdigest(),
                       '--meta-json', json.dumps(meta())], db_path, tg, data)
        assert rc == 0
        assert json.loads(out) == {'ok': True, 'thread_id': 501, 'message_ids': [1001],
                                   'parts': 1, 'complete': True}
        assert tg.docs[0]['content'] == data

    def test_send_refused_prints_why_not_the_data(self, tg, db_path):
        data = b'SECRET plaintext tar ' * 50
        rc, out = cli(['send', '--name', NAME, '--sha256', hashlib.sha256(data).hexdigest()],
                      db_path, tg, data)
        assert rc == 1
        assert json.loads(out) == {'ok': False,
                                   'error': 'на вход пришёл не файл age: нешифрованное не отправляю'}
        assert 'SECRET' not in out and not tg.docs

    def test_bad_meta_is_no_meta(self, tg, db_path):
        data = archive()
        rc, out = cli(['send', '--name', NAME, '--sha256', hashlib.sha256(data).hexdigest(),
                       '--meta-json', '{not json'], db_path, tg, data)
        assert rc == 0 and json.loads(out)['complete'] is False

    def test_fail_installed_forget_status(self, tg, db_path):
        assert cli(['installed'], db_path)[0] == 0
        rc, out = cli(['fail', '--reason', 'на entry нет age: apt-get install age'], db_path)
        assert rc == 0 and json.loads(out) == {'ok': True}
        rc, out = cli(['status'], db_path, now=NOW + timedelta(minutes=5))
        assert rc == 0
        assert 'last_run: 17.11.2026 01:41 UTC failed — на entry нет age: apt-get install age' in out
        assert 'alert: Полного бэкапа сервиса ещё не было' in out
        assert cli(['forget'], db_path)[0] == 0
        rc, out = cli(['status'], db_path)
        assert 'state: none' in out

    def test_topic_create_refused(self, tg, db_path):
        tg.create_ok = False
        rc, out = cli(['topic', '--create'], db_path, tg)
        assert rc == 1 and out.startswith('ERROR: не смог создать топик «Бэкапы»')

    def test_topic(self, tg, db_path):
        rc, out = cli(['topic'], db_path, tg)
        assert 'not created yet' in out and not tg.topics
        rc, out = cli(['topic', '--create'], db_path, tg)
        assert 'Бэкапы' in out and ': 501' in out
        rc, out = cli(['topic', '--set', '77'], db_path, tg)
        assert ': 77' in out and backups.stored_topic(db_path, CHAT) == 77
