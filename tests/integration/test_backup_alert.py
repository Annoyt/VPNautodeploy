"""backup:stale — the AlertManager check of the daily service backup
(AGENTS.md §58; bot/services/backups.py ``alert_verdict``).

Pinned: no state = not installed, silence; a complete backup younger than
26 h = silence; older, or a failed / partial run before the first complete
one, or an installed timer that never ran = critical, with the last run's
reason in the detail; a failed read judges nothing; the alert reaches the
forum topic through a real AlertManager tick and goes quiet once a complete
backup is recorded.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from bot.core.database import Database
from bot.services import backups
from bot.services.alert_manager import AlertManager, build_default_checks

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def ago(**kw):
    return backups.to_iso(NOW - timedelta(**kw))


def ok_entry(**kw):
    return {'ts': ago(**kw), 'name': 'nekovpn-backup-x.tar.zst.age', 'size': 1, 'sha256': 'a',
            'message_ids': [1], 'chat_id': '-1', 'thread_id': 5, 'missing': {}}


class TestVerdict:

    @pytest.mark.parametrize('state', [None, {}, 'junk', [1]])
    def test_nothing_installed(self, state):
        assert backups.alert_verdict(state, NOW) is None

    def test_fresh(self):
        state = {'last_ok': ok_entry(hours=1), 'last_run': {'ts': ago(hours=1), 'status': 'ok'}}
        assert backups.alert_verdict(state, NOW) is None

    def test_the_edge(self):
        assert backups.alert_verdict({'last_ok': ok_entry(hours=25, minutes=59)}, NOW) is None
        v = backups.alert_verdict({'last_ok': ok_entry(hours=26)}, NOW)
        assert v['key'] == 'backup:stale' and v['severity'] == 'critical'
        assert v['title'] == 'Полный бэкап сервиса не уходил 26 ч'

    def test_days(self):
        v = backups.alert_verdict({'last_ok': ok_entry(days=3, hours=5)}, NOW)
        assert v['title'] == 'Полный бэкап сервиса не уходил 3 сут 5 ч'
        assert v['detail'].startswith('Последний полный: 14.11.2026 07:00 UTC.')
        assert v['detail'].endswith(backups.CHECK_HINT)

    def test_failed_before_any_complete(self):
        state = {'installed': ago(minutes=10),
                 'last_run': {'ts': ago(minutes=5), 'status': 'failed',
                              'error': 'на entry нет age: apt-get install age'}}
        v = backups.alert_verdict(state, NOW)
        assert v['title'] == 'Полного бэкапа сервиса ещё не было'
        assert ('Последний запуск 17.11.2026 11:55 UTC: на entry нет age: apt-get install age.'
                in v['detail'])

    def test_partial_before_any_complete(self):
        sent = dict(ok_entry(minutes=5), missing={'exit': 'exit: ssh rc=255: timed out'})
        state = {'last_sent': sent, 'last_run': {'ts': ago(minutes=5), 'status': 'partial'}}
        v = backups.alert_verdict(state, NOW)
        assert v['title'] == 'Полного бэкапа сервиса ещё не было'
        assert ('Последний архив 17.11.2026 11:55 UTC ушёл не целым: exit: exit: ssh rc=255: '
                'timed out.') in v['detail']

    def test_partial_runs_after_the_last_complete(self):
        sent = dict(ok_entry(hours=2), missing={'exit': 'x', 'bot': 'y'})
        state = {'last_ok': ok_entry(days=2), 'last_sent': sent,
                 'last_run': {'ts': ago(hours=1), 'status': 'failed', 'error': 'отправка: z'}}
        v = backups.alert_verdict(state, NOW)
        assert v['title'] == 'Полный бэкап сервиса не уходил 2 сут'
        assert 'ушёл не целым: exit: x; bot: y.' in v['detail']
        assert 'Последний запуск 17.11.2026 11:00 UTC: отправка: z.' in v['detail']

    def test_an_older_partial_is_not_mentioned(self):
        state = {'last_ok': ok_entry(days=2),
                 'last_sent': dict(ok_entry(days=3), missing={'exit': 'old'})}
        assert 'не целым' not in backups.alert_verdict(state, NOW)['detail']

    def test_installed_but_never_ran(self):
        assert backups.alert_verdict({'installed': ago(hours=25)}, NOW) is None
        assert backups.alert_verdict({'installed': ago(hours=26)}, NOW) is not None
        v = backups.alert_verdict({'installed': ago(hours=27)}, NOW)
        assert v['title'] == 'Бэкап сервиса ни разу не запускался (таймер стоит с 16.11.2026 09:00 UTC)'

    def test_junk_fields(self):
        v = backups.alert_verdict({'last_ok': 'yesterday', 'last_run': 'x', 'installed': 3,
                                   'last_sent': []}, NOW)
        assert v is None


# ======================================================== through the tick ==

@pytest.fixture
def config(tmp_path):
    Database(str(tmp_path / 'bot.db'))
    cfg = Mock()
    cfg.DB_PATH = str(tmp_path / 'bot.db')
    cfg.TOPIC_AI = 55
    cfg.FORUM_GROUP_ID = -100123
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.ALERT_TG_MIN_SEVERITY = 'critical'
    cfg.AGENT_BACKEND = 'hermes'
    cfg.HERMES_URL = ''
    return cfg


def check(config):
    return next(c for c in build_default_checks(config, Mock()) if c.__name__ == 'check_backup')


def put_state(config, state):
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute('INSERT INTO app_settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE '
                 'SET value = excluded.value', (backups.STATE_KEY, json.dumps(state)))
    conn.commit()
    conn.close()


def real_now(**kw):
    return backups.to_iso(datetime.now(timezone.utc) - timedelta(**kw))


class TestCheck:

    def test_no_row(self, config):
        assert check(config)() == ('backup:', [])

    def test_fresh(self, config):
        put_state(config, {'last_ok': {'ts': real_now(hours=2)}})
        assert check(config)() == ('backup:', [])

    def test_stale(self, config):
        put_state(config, {'last_ok': {'ts': real_now(hours=30)}})
        prefix, (alert,) = check(config)()
        assert prefix == 'backup:' and alert.key == 'backup:stale'
        assert alert.severity == 'critical' and alert.min_cycles == 2
        assert alert.title == 'Полный бэкап сервиса не уходил 30 ч'

    def test_a_damaged_row_is_no_state(self, config):
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("INSERT INTO app_settings(key, value) VALUES ('backup_state', '{oops')")
        conn.commit()
        conn.close()
        assert check(config)() == ('backup:', [])

    def test_a_failed_read_judges_nothing(self, config, tmp_path):
        config.DB_PATH = str(tmp_path / 'not-a-db')
        (tmp_path / 'not-a-db').write_bytes(b'garbage' * 100)
        assert check(config)() == ('backup:', [], frozenset())

    def test_pages_the_topic_then_goes_quiet(self, config):
        put_state(config, {'last_ok': {'ts': real_now(hours=30)},
                           'last_run': {'ts': real_now(hours=1), 'status': 'failed',
                                        'error': 'exit: ssh rc=255'}})
        db = Database(config.DB_PATH)
        m = AlertManager(Mock(), config, db=db)
        m.register(check(config))
        m.run_once()
        assert not m.bot.send_message.called                    # min_cycles 2
        m.run_once()
        (call,) = m.bot.send_message.call_args_list
        assert call.kwargs['message_thread_id'] == 55
        assert 'Полный бэкап сервиса не уходил 30 ч' in call.kwargs['text']
        assert 'exit: ssh rc=255' in call.kwargs['text']
        put_state(config, {'last_ok': {'ts': real_now(minutes=1)}})
        for _ in range(3):
            m.run_once()
        assert m.bot.send_message.call_count == 1               # no repeat once it is fine
