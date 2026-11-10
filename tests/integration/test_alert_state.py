"""AlertManager state, recovery and agent turns — IMPROVEMENT_PLAN F, B2-B4.

* B3  cooldowns, acks and open alerts live in ``alert_state``: a restart
      during an outage neither re-pages the key nor starts another agent
      turn, an ack still holds, and a recovery is still announced.
* B2/B3  an open protocol_down / inbound_drop alert that stays quiet for
      RECOVERY_TICKS ticks is posted as recovered where it went; its repeat
      cooldown is cleared, so a new failure pages at once.
* B4  problem 2: a single check's streak breaks on a healthy tick;
      problem 12: one agent turn per tick for the protocol family, its
      reply stored on every alert row of the tick;
      B3: a line where the alert went when the agent gives no diagnosis.
* B4  problem 10: one-shot agent sessions are pruned after a week.

Level 2: real sqlite; the agent client and Telegram are stubbed.
"""

import time
from unittest.mock import Mock, patch

import pytest

from bot.core.database import Database
from bot.services import alert_manager as am
from bot.services.alert_manager import Alert, AlertManager

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def config(tmp_path):
    cfg = Mock()
    cfg.DB_PATH = str(tmp_path / 'bot.db')
    cfg.TOPIC_AI = 55
    cfg.FORUM_GROUP_ID = -100123
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.ALERT_TG_MIN_SEVERITY = 'critical'
    cfg.AGENT_BACKEND = 'hermes'
    cfg.HERMES_URL = ''            # no agent unless a test turns it on
    return cfg


class Script:
    """A check whose answer the test sets per tick."""

    def __init__(self, *, multi=None):
        self.multi = multi
        self.out = None
        self.__name__ = f'script_{multi or "single"}'

    def __call__(self):
        if self.multi is not None:
            return (self.multi, list(self.out or []))
        return self.out


def mgr_with(db, config, *checks):
    m = AlertManager(Mock(), config, db=db)
    for c in checks:
        m.register(c)
    return m


def texts(mgr):
    return [c.kwargs['text'] for c in mgr.bot.send_message.call_args_list]


def proto(tag='hy2', severity='critical'):
    return Alert(key=f'protocol_down:{tag}', severity=severity, min_cycles=1,
                 title=f'Протокол {tag} мёртв', detail='d')


# ------------------------------------------------------ B4 problem 2 ---------

class TestSingleCheckStreak:

    def test_two_failures_hours_apart_are_not_two_in_a_row(self, db, config):
        check = Script()
        m = mgr_with(db, config, check)
        bad = Alert(key='cpu', severity='critical', min_cycles=2, title='CPU', detail='')
        check.out = bad
        m.run_once()
        for _ in range(3):
            check.out = None
            m.run_once()
        check.out = bad
        m.run_once()
        assert m.bot.send_message.call_count == 0
        m.run_once()                                   # now two in a row
        assert m.bot.send_message.call_count == 1

    def test_a_crashing_check_says_nothing(self, db, config):
        check = Script()
        m = mgr_with(db, config, check)
        check.out = Alert(key='cpu', severity='critical', min_cycles=2, title='CPU', detail='')
        m.run_once()

        def boom():
            raise RuntimeError('read failed')
        boom.__name__ = check.__name__
        m._checks = [boom]
        m.run_once()                                   # no verdict: streak kept
        m._checks = [check]
        m.run_once()
        assert m.bot.send_message.call_count == 1

    def test_a_check_switching_keys_heals_the_old_one(self, db, config):
        check = Script()
        m = mgr_with(db, config, check)
        check.out = Alert(key='tg_down', severity='critical', min_cycles=2, title='a')
        m.run_once()
        check.out = Alert(key='tg_recovered', severity='warn', min_cycles=1, title='b')
        m.run_once()
        assert m._state['tg_down'].consecutive_fails == 0


# ------------------------------------------------------ B3 persisted ---------

class TestStateSurvivesARestart:

    def test_no_second_page_after_a_restart(self, db, config):
        check = Script(multi='protocol_down:')
        check.out = [proto()]
        mgr_with(db, config, check).run_once()
        again = mgr_with(db, config, check)            # the bot restarted
        again.run_once()
        assert again.bot.send_message.call_count == 0
        assert again._state['protocol_down:hy2'].open

    def test_an_ack_survives_a_restart(self, db, config):
        check = Script(multi='protocol_down:')
        check.out = [proto()]
        m = mgr_with(db, config, check)
        m.run_once()
        m.ack('protocol_down:hy2', by='admin')
        stamp = m._state['protocol_down:hy2'].acked_until_ts
        again = mgr_with(db, config, check)
        assert again._state['protocol_down:hy2'].acked_until_ts == pytest.approx(stamp)

    def test_a_recovery_after_a_restart_is_still_posted(self, db, config):
        check = Script(multi='protocol_down:')
        check.out = [proto()]
        mgr_with(db, config, check).run_once()
        again = mgr_with(db, config, check)
        check.out = []
        for _ in range(am.RECOVERY_TICKS):
            again.run_once()
        assert any('Восстановилось' in t and 'hy2' in t for t in texts(again))


# ------------------------------------------------------ B2/B3 recovery -------

class TestRecovery:

    def _open(self, db, config, alert):
        check = Script(multi=alert.key.split(':', 1)[0] + ':')
        check.out = [alert]
        m = mgr_with(db, config, check)
        m.run_once()
        check.out = []
        return m

    def test_posted_after_the_quiet_ticks_only(self, db, config):
        m = self._open(db, config, proto())
        for _ in range(am.RECOVERY_TICKS - 1):
            m.run_once()
        assert not any('Восстановилось' in t for t in texts(m))
        m.run_once()
        recovered = [t for t in texts(m) if 'Восстановилось' in t]
        assert len(recovered) == 1 and 'Протокол hy2 мёртв' in recovered[0]
        for _ in range(3):
            m.run_once()
        assert len([t for t in texts(m) if 'Восстановилось' in t]) == 1

    def test_a_blip_restarts_the_quiet_count(self, db, config):
        check = Script(multi='protocol_down:')
        check.out = [proto()]
        m = mgr_with(db, config, check)
        m.run_once()
        check.out = []
        for _ in range(am.RECOVERY_TICKS - 1):
            m.run_once()
        check.out = [proto()]
        m.run_once()                                   # in cooldown: no page
        check.out = []
        for _ in range(am.RECOVERY_TICKS - 1):
            m.run_once()
        assert not any('Восстановилось' in t for t in texts(m))

    def test_a_new_failure_after_recovery_pages_at_once(self, db, config):
        check = Script(multi='protocol_down:')
        check.out = [proto()]
        m = mgr_with(db, config, check)
        m.run_once()
        check.out = []
        for _ in range(am.RECOVERY_TICKS):
            m.run_once()
        check.out = [proto()]
        m.run_once()                                   # well inside 2 h
        assert len([t for t in texts(m) if 'мёртв' in t and 'Восстановилось' not in t]) == 2

    def test_dashboard_only_alerts_recover_silently(self, db, config):
        m = self._open(db, config, proto('ws', severity='warn'))
        for _ in range(am.RECOVERY_TICKS):
            m.run_once()
        assert m.bot.send_message.call_count == 0
        assert not m._state['protocol_down:ws'].open

    def test_other_families_get_no_recovery_post(self, db, config):
        m = self._open(db, config, Alert(key='lockdown:active', severity='critical',
                                         min_cycles=1, title='LOCKDOWN'))
        for _ in range(am.RECOVERY_TICKS):
            m.run_once()
        assert not any('Восстановилось' in t for t in texts(m))

    def test_inbound_drop_recovers_like_protocol_down(self, db, config):
        m = self._open(db, config, Alert(key='inbound_drop:ws', severity='critical',
                                         min_cycles=1, title='Подключения ws упали'))
        for _ in range(am.RECOVERY_TICKS):
            m.run_once()
        assert any('Восстановилось' in t and 'ws упали' in t for t in texts(m))


# ------------------------------------------------------ B4 problem 12 --------

@pytest.fixture
def agent(config):
    config.HERMES_URL = 'http://hermes:4097'
    client = Mock()
    client.ask.return_value = ('ИТОГ: hy2 лежит, stls лежит', 10)
    with patch('bot.services.agent_factory.build_agent_client', return_value=client):
        yield client


def _join(m):
    t = m._last_agent_thread
    if t is not None:
        t.join(timeout=10)


def _history(db):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT key, kimi_analysis FROM alert_history ORDER BY id").fetchall()]


class TestOneAgentTurnPerTick:

    def test_three_dark_protocols_one_turn(self, db, config, agent):
        check = Script(multi='protocol_down:')
        check.out = [proto('hy2'), proto('stls'), proto('reality')]
        m = mgr_with(db, config, check)
        m.run_once()
        _join(m)
        assert agent.ask.call_count == 1
        session_key, prompt = agent.ask.call_args.args[:2]
        assert session_key.startswith('proto-alert:protocol_down:hy2+reality+stls:')
        assert 'Протокол hy2 мёртв' in prompt and 'Протокол stls мёртв' in prompt
        assert [r[1] for r in _history(db)] == ['ИТОГ: hy2 лежит, stls лежит'] * 3
        follow = [t for t in texts(m) if 'Диагностика' in t]
        assert len(follow) == 1

    def test_the_inbound_drop_alert_joins_the_same_turn(self, db, config, agent):
        probes = Script(multi='protocol_down:')
        probes.out = [proto('ws')]
        drops = Script(multi='inbound_drop:')
        drops.out = [Alert(key='inbound_drop:ws', severity='critical', min_cycles=1,
                           title='Подключения ws упали')]
        m = mgr_with(db, config, probes, drops)
        m.run_once()
        _join(m)
        assert agent.ask.call_count == 1

    def test_a_later_tick_gets_its_own_turn(self, db, config, agent):
        check = Script(multi='protocol_down:')
        check.out = [proto('hy2')]
        m = mgr_with(db, config, check)
        m.run_once()
        _join(m)
        check.out = [proto('hy2'), proto('stls')]       # hy2 in cooldown
        m.run_once()
        _join(m)
        assert agent.ask.call_count == 2
        assert agent.ask.call_args.args[0].startswith('proto-alert:protocol_down:stls:')

    def test_silence_is_posted_where_the_alert_went(self, db, config, agent):
        agent.ask.side_effect = RuntimeError('agent turn timed out after 300s')
        check = Script(multi='protocol_down:')
        check.out = [proto('hy2')]
        m = mgr_with(db, config, check)
        m.run_once()
        _join(m)
        note = [t for t in texts(m) if 'Агент не ответил' in t]
        assert len(note) == 1 and 'не уложился в 300 с' in note[0] and '/protocols' in note[0]

    def test_no_silence_note_for_a_dashboard_only_alert(self, db, config, agent):
        agent.ask.return_value = ('', 1)
        check = Script(multi='protocol_down:')
        check.out = [proto('ws', severity='warn')]
        m = mgr_with(db, config, check)
        m.run_once()
        _join(m)
        assert m.bot.send_message.call_count == 0


# ------------------------------------------------------ B4 problem 10 --------

class TestAiSessionsCleanup:

    def _seed(self, db):
        now = int(time.time())
        old = now - 8 * 86400
        with db._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS ai_sessions (session_key TEXT PRIMARY KEY, "
                         "kimi_session TEXT NOT NULL, created_at INTEGER NOT NULL, "
                         "updated_at INTEGER NOT NULL, message_count INTEGER NOT NULL DEFAULT 0)")
            for key, ts in (('proto-alert:protocol_down:hy2:1', old),
                            ('dpi-alert:dpi_hsfail:RU:AS1:2', old),
                            ('sos:sos:42:3', old), ('dpi-daily:4', old),
                            ('dpi-alert:dpi_hsfail:RU:AS1:5', now),
                            ('pm:6186958828', old), ('topic:-100:55', old)):
                conn.execute("INSERT INTO ai_sessions VALUES (?, 's', ?, ?, 1)", (key, ts, ts))
            conn.commit()
        return now

    def test_one_shot_sessions_go_after_a_week(self, db, config):
        from bot.services.notifications import NotificationService
        now = self._seed(db)
        svc = NotificationService.__new__(NotificationService)
        svc.db = db
        assert svc._cleanup_ai_sessions_sync(now=now) == 4
        with db._connect() as conn:
            left = sorted(r[0] for r in conn.execute("SELECT session_key FROM ai_sessions"))
        assert left == ['dpi-alert:dpi_hsfail:RU:AS1:5', 'pm:6186958828', 'topic:-100:55']

    def test_no_table_is_nothing_to_do(self, db, config):
        from bot.services.notifications import NotificationService
        svc = NotificationService.__new__(NotificationService)
        svc.db = db
        assert svc._cleanup_ai_sessions_sync() == 0


class TestAlertStateRetention:

    def test_closed_keys_go_open_ones_stay(self, db, config):
        from bot.services.notifications import NotificationService
        with db._connect() as conn:
            conn.executemany(
                "INSERT INTO alert_state (key, open, updated_at) VALUES (?, ?, ?)",
                [('dpi_hsfail:RU:AS1', 0, '2026-01-01 00:00:00'),
                 ('protocol_down:hy2', 1, '2026-01-01 00:00:00'),
                 ('dpi_hsfail:RU:AS2', 0, '2999-01-01 00:00:00')])
            conn.commit()
        svc = NotificationService.__new__(NotificationService)
        svc.db = db
        svc._cleanup_alert_history_sync()
        with db._connect() as conn:
            left = sorted(r[0] for r in conn.execute("SELECT key FROM alert_state"))
        assert left == ['dpi_hsfail:RU:AS2', 'protocol_down:hy2']
