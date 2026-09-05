"""protocol_down alerts get an automatic agent diagnosis; DPI keeps its
dashboard-only follow-up; an agent failure never touches the pager.

Why
---
The protocol_down pager (tests/integration/test_alert_protocol_down.py)
turns the 2026-09-01 Reality outage into a headline. A headline still
needs someone to go and look, so on suspicion the alert now also kicks
the /ai agent with a prompt pinned to ``scripts/protocol_healthcheck.py``
and posts the reply into the same forum topic. Asked "which protocol is
down" without that pin, the agent spent 105 s on ports/iptables/
containers and answered "everything alive" — those checks cannot see a
dead inbound; only the probe table and the panel field audit can, and
the script reads exactly those.

Also covers the agent-hygiene changes in ``agent_client``: the output
contract in SYSTEM_PREAMBLE and the protocol-state markers that route
"какой протокол не работает" to vpn-ops.

Real sqlite bot.db so the alert_history UPDATE that attaches the
analysis is exercised, not mocked.
"""

import logging
import threading
import time
from unittest.mock import Mock, patch

import pytest

from bot.core.database import Database
from bot.services.agent_client import SYSTEM_PREAMBLE, _detect_skill_domains
from bot.services.alert_manager import (
    Alert,
    AlertManager,
    DPI_AGENT_TIMEOUT_S,
    PROTOCOL_AGENT_TIMEOUT_S,
    PROTOCOL_HEALTHCHECK_CMD,
    _Tracker,
    _protocol_agent_prompt,
)


REPLY = ("ИТОГ: reality лежит 4 д 3 ч; hy2, ws, stls живы; hy2t без проб\n"
         "ПОДОЗРЕВАЕМЫЙ: flow пуст у 80/81 клиентов inbound-443 "
         "(строка: BROKEN: 80 problem(s))\n"
         "СЛЕДУЮЩАЯ КОМАНДА: docker exec 3x-ui sh -c '... inbounduser -tag inbound-443'")


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
    # Explicit strings: a bare Mock attr is truthy and would make
    # get_agent_url() believe an agent is configured.
    cfg.AGENT_BACKEND = 'hermes'
    cfg.HERMES_URL = 'http://hermes:4097'
    return cfg


@pytest.fixture
def agent():
    """Stub the agent factory; yields (client, factory_mock)."""
    client = Mock()
    client.ask.return_value = (REPLY, 4200)
    with patch('bot.services.agent_factory.build_agent_client',
               return_value=client) as factory:
        yield client, factory


def proto_alert(key='protocol_down:reality', severity='critical'):
    return Alert(
        key=key, severity=severity, min_cycles=2,
        title='Протокол reality мёртв (лежит 4 д 3 ч)',
        detail=('За последние 3 прогона (30 попыток) через reality не '
                'вернулся НИ ОДИН ответ — даже ошибочный. Частые причины: '
                'клиенты потеряли per-protocol поле (flow у Reality); '
                'разъехались hop-порты у hy2; xray отверг конфиг.'),
    )


def dpi_alert(severity='warn'):
    return Alert(
        key='dpi_short:RU:AS8402', severity=severity, min_cycles=2,
        title='DPI? RU/AS8402 55% short sessions',
        detail='Rostelecom · 55/100 коротких сессий за час.',
    )


def join_agent(mgr, timeout=10):
    """The protocol follow-up runs on a worker thread; wait for it so
    the assertions below are deterministic."""
    t = mgr._last_agent_thread
    if t is not None:
        t.join(timeout=timeout)
        assert not t.is_alive(), 'agent worker did not finish'


def fire(mgr, alert, *, wait=True):
    """Drive _fire directly — the tracker bookkeeping in _process_alert
    is covered elsewhere; here we care about what firing does."""
    mgr._fire(alert, _Tracker(consecutive_fails=2))
    if wait:
        join_agent(mgr)


def history(db):
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT key, severity, kimi_analysis, kimi_at FROM alert_history "
            "ORDER BY id").fetchall()]


_DOMAINS = ['vk.com', 'yandex.ru', 'sberbank.ru', 'rutube.ru', 'youtube.com',
            'google.com', 'facebook.com', 'telegram.org', 'github.com',
            'anthropic.com']


def _seed_probe_rows(db, protocol, *, ok_per_run, runs=3):
    """Three probe runs in HealthChecker's row shape; failures carry no
    latency (the 2026-09-01 signature)."""
    from datetime import datetime, timedelta
    with db._connect() as conn:
        for run in range(runs):
            ts = (datetime.utcnow() - timedelta(minutes=15 + run * 15)).isoformat()
            for i, domain in enumerate(_DOMAINS):
                ok = i < ok_per_run
                conn.execute(
                    "INSERT INTO outbound_health (outbound_tag, target_domain,"
                    " status, latency_ms, error_msg, ts) VALUES (?,?,?,?,?,?)",
                    (protocol, domain, 'ok' if ok else 'timeout',
                     120 if ok else None, None if ok else 'timeout', ts),
                )
        conn.commit()


def _pager_alerts(config):
    """Alerts the real check_protocol_probe_down produces right now."""
    from bot.services.alert_manager import build_default_checks
    check = next(c for c in build_default_checks(config, Mock())
                 if c.__name__ == 'check_protocol_probe_down')
    _prefix, alerts = check()
    return alerts


class TestProtocolDownKicksAgent:

    def test_prompt_pins_the_healthcheck_script(self, db, config, agent):
        client, factory = agent
        fire(AlertManager(Mock(), config, db=db), proto_alert())

        client.ask.assert_called_once()
        session_key, prompt = client.ask.call_args.args
        assert session_key.startswith('proto-alert:protocol_down:reality:')
        assert 'protocol_healthcheck.py' in prompt
        assert PROTOCOL_HEALTHCHECK_CMD in prompt
        # The contract the reply must follow.
        for section in ('ИТОГ', 'ПОДОЗРЕВАЕМЫЙ', 'СЛЕДУЮЩАЯ КОМАНДА'):
            assert section in prompt
        assert '900' in prompt
        # Read-only: no restarts / panel writes without the admin's OK.
        assert 'Ничего не меняй' in prompt
        assert 'OK админа' in prompt

    def test_budget_is_capped_at_five_minutes(self, db, config, agent):
        """The call blocks the alert tick; DPI's 10 min is too long for
        a pager that other checks share."""
        client, factory = agent
        fire(AlertManager(Mock(), config, db=db), proto_alert())

        assert PROTOCOL_AGENT_TIMEOUT_S == 300
        assert factory.call_args.kwargs['default_timeout'] == 300
        assert client.ask.call_args.kwargs['timeout'] == 300

    def test_reply_is_posted_to_the_alert_topic(self, db, config, agent):
        bot = Mock()
        fire(AlertManager(bot, config, db=db), proto_alert())

        # 1st = the alert itself, 2nd = the diagnosis, same topic.
        assert bot.send_message.call_count == 2
        alert_kw = bot.send_message.call_args_list[0].kwargs
        follow_kw = bot.send_message.call_args_list[1].kwargs
        assert follow_kw['chat_id'] == alert_kw['chat_id'] == config.FORUM_GROUP_ID
        assert follow_kw['message_thread_id'] == alert_kw['message_thread_id'] == config.TOPIC_AI
        assert follow_kw['parse_mode'] == 'HTML'
        assert follow_kw['text'].startswith('🤖 <b>Диагностика по алерту</b>')
        assert 'protocol_down:reality' in follow_kw['text']
        assert 'ИТОГ: reality лежит 4 д 3 ч' in follow_kw['text']

    def test_reply_is_stored_for_the_dashboard(self, db, config, agent):
        fire(AlertManager(Mock(), config, db=db), proto_alert())

        rows = history(db)
        assert len(rows) == 1
        assert rows[0]['key'] == 'protocol_down:reality'
        assert rows[0]['kimi_analysis'] == REPLY
        assert rows[0]['kimi_at'] is not None

    def test_html_in_reply_is_escaped(self, db, config, agent):
        """A raw '<' in the agent's plain text kills Telegram's HTML
        parse and the whole message is silently dropped."""
        client, _ = agent
        client.ask.return_value = ("ИТОГ: <flow> пуст у клиентов & inbound-443", 10)
        bot = Mock()
        fire(AlertManager(bot, config, db=db), proto_alert())

        text = bot.send_message.call_args_list[1].kwargs['text']
        assert '&lt;flow&gt;' in text
        assert '&amp; inbound-443' in text
        assert '<flow>' not in text

    def test_long_reply_is_cut_without_a_broken_entity(self, db, config, agent):
        client, _ = agent
        client.ask.return_value = ("x" * 3498 + "&" + "y" * 500, 10)
        bot = Mock()
        fire(AlertManager(bot, config, db=db), proto_alert())

        text = bot.send_message.call_args_list[1].kwargs['text']
        assert '… (обрезано)' in text
        assert '&amp' not in text.split('… (обрезано)')[0][-10:]
        assert len(text) < 4096

    def test_follow_up_uses_pm_only_when_forum_is_unavailable(self, db, config, agent):
        """House rule: never PM the admin while the group is alive. With
        no TOPIC_AI configured the critical alert AND its diagnosis both
        fall back to the PM — the same place, not a duplicate."""
        config.TOPIC_AI = 0
        bot = Mock()
        fire(AlertManager(bot, config, db=db), proto_alert())

        assert bot.send_message.call_count == 2
        for call in bot.send_message.call_args_list:
            assert call.kwargs['chat_id'] == config.SUPER_ADMIN_ID
            assert 'message_thread_id' not in call.kwargs

    def test_every_protocol_down_key_kicks_the_agent(self, db, config, agent):
        """probe_pipeline and :all are protocol_down alerts too — the
        healthcheck script reports staleness and sidecar death as well."""
        client, _ = agent
        mgr = AlertManager(Mock(), config, db=db)
        for key in ('protocol_down:probe_pipeline', 'protocol_down:all',
                    'protocol_down:stls'):
            fire(mgr, proto_alert(key=key))
        assert client.ask.call_count == 3

    @pytest.mark.parametrize('scenario, expected_key', [
        ('one_dark', 'protocol_down:reality'),
        ('all_dark', 'protocol_down:all'),
        ('no_rows', 'protocol_down:probe_pipeline'),
    ])
    def test_prompt_routes_to_vpn_ops_not_incident_response(
            self, db, config, scenario, expected_key):
        """Incident-response wins outright when its markers fire, and its
        broad triage regimen is the exact behaviour we are steering away
        from; code-review's blunt " pr" marker must not fire either. The
        alert title/detail ride in the prompt, so the alerts come from
        the REAL pager check — this guards its wording, not a copy."""
        if scenario == 'one_dark':
            _seed_probe_rows(db, 'reality', ok_per_run=0)
            for p in ('hy2', 'ws', 'stls'):
                _seed_probe_rows(db, p, ok_per_run=7)
        elif scenario == 'all_dark':
            for p in ('reality', 'hy2', 'ws', 'stls'):
                _seed_probe_rows(db, p, ok_per_run=0)
        alerts = _pager_alerts(config)
        assert [a.key for a in alerts] == [expected_key]

        domains = _detect_skill_domains(_protocol_agent_prompt(alerts[0]))
        assert 'vpn-ops' in domains, domains
        assert 'incident-response' not in domains, domains
        assert 'code-review' not in domains, domains


class TestDpiFollowUpUnchanged:

    def test_dpi_stays_dashboard_only(self, db, config, agent):
        """DPI fires often; its analysis goes to alert_history for the
        dashboard and NOTHING goes to chat — the pre-existing contract."""
        client, factory = agent
        bot = Mock()
        fire(AlertManager(bot, config, db=db), dpi_alert())

        bot.send_message.assert_not_called()
        client.ask.assert_called_once()
        session_key, prompt = client.ask.call_args.args
        assert session_key.startswith('dpi-alert:dpi_short:RU:AS8402:')
        assert 'dpi-analysis skill' in prompt
        assert 'protocol_healthcheck' not in prompt
        assert client.ask.call_args.kwargs['timeout'] == DPI_AGENT_TIMEOUT_S == 600
        assert factory.call_args.kwargs['default_timeout'] == 600
        assert history(db)[0]['kimi_analysis'] == REPLY

    def test_critical_dpi_still_posts_nothing(self, db, config, agent):
        bot = Mock()
        fire(AlertManager(bot, config, db=db), dpi_alert(severity='critical'))
        bot.send_message.assert_not_called()


class TestAgentFailureIsContained:
    """Whatever the agent does, _fire returns and the alert is already
    persisted and delivered."""

    def test_ask_raising_does_not_break_fire(self, db, config, agent):
        client, _ = agent
        client.ask.side_effect = RuntimeError('boom')
        bot = Mock()

        fire(AlertManager(bot, config, db=db), proto_alert())   # must not raise

        rows = history(db)
        assert [(r['key'], r['severity']) for r in rows] == [('protocol_down:reality', 'critical')]
        assert rows[0]['kimi_analysis'] is None
        # The pager message went out; no follow-up, no failure notice.
        bot.send_message.assert_called_once()
        assert 'reality' in bot.send_message.call_args.kwargs['text']

    def test_quota_and_timeout_are_logged_at_info(self, db, config, agent, caplog):
        """Transient provider conditions are not bugs; they must not
        show up as warnings in the dashboard's log panel."""
        client, _ = agent
        mgr = AlertManager(Mock(), config, db=db)
        with caplog.at_level(logging.INFO, logger='bot.services.alert_manager'):
            client.ask.side_effect = RuntimeError('HTTP 429: rate_limit_exceeded')
            fire(mgr, proto_alert())
            client.ask.side_effect = RuntimeError('agent turn timed out after 300s')
            fire(mgr, proto_alert(key='protocol_down:stls'))
        infos = [r for r in caplog.records if r.levelno == logging.INFO
                 and r.name == 'bot.services.alert_manager']
        assert any('quota exhausted' in r.getMessage() for r in infos)
        assert any('timed out' in r.getMessage() for r in infos)
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING
                    and 'proto-alert-agent' in r.getMessage()]

    def test_factory_raising_is_contained(self, db, config):
        bot = Mock()
        with patch('bot.services.agent_factory.build_agent_client',
                   side_effect=ImportError('no hermes client')):
            fire(AlertManager(bot, config, db=db), proto_alert())
        assert len(history(db)) == 1
        bot.send_message.assert_called_once()

    def test_agent_not_configured_is_skipped_quietly(self, db, config, agent):
        client, factory = agent
        config.HERMES_URL = ''
        bot = Mock()
        fire(AlertManager(bot, config, db=db), proto_alert())

        factory.assert_not_called()
        client.ask.assert_not_called()
        bot.send_message.assert_called_once()
        assert len(history(db)) == 1

    def test_empty_reply_stores_and_posts_nothing(self, db, config, agent):
        client, _ = agent
        client.ask.return_value = ('', 10)
        bot = Mock()
        fire(AlertManager(bot, config, db=db), proto_alert())

        bot.send_message.assert_called_once()
        assert history(db)[0]['kimi_analysis'] is None

    def test_no_db_still_posts_the_follow_up(self, config, agent):
        """AlertManager runs without a DB handle in some deployments;
        the chat follow-up must not depend on the dashboard attach."""
        bot = Mock()
        fire(AlertManager(bot, config, db=None), proto_alert())
        assert bot.send_message.call_count == 2
        assert 'ИТОГ' in bot.send_message.call_args_list[1].kwargs['text']

    def test_follow_up_send_failure_is_swallowed(self, db, config, agent):
        bot = Mock()
        bot.send_message.side_effect = [None, RuntimeError('tg 502'), RuntimeError('tg 502')]
        fire(AlertManager(bot, config, db=db), proto_alert())   # must not raise
        assert history(db)[0]['kimi_analysis'] == REPLY


class TestWiringThroughRunOnce:

    def test_second_cycle_fires_alert_then_diagnosis(self, db, config, agent):
        """End to end through the real tick: multi-bucket check shape,
        min_cycles=2, then alert + follow-up in one _fire."""
        client, _ = agent
        bot = Mock()
        mgr = AlertManager(bot, config, db=db)
        mgr.register(lambda: ("protocol_down:", [proto_alert()]))

        mgr.run_once()
        bot.send_message.assert_not_called()
        client.ask.assert_not_called()

        mgr.run_once()
        join_agent(mgr)
        client.ask.assert_called_once()
        assert bot.send_message.call_count == 2
        assert bot.send_message.call_args_list[1].kwargs['text'].startswith('🤖')


class TestProtocolKickIsOffThread:
    """The alert tick is one APScheduler job (60 s, max_instances=1).
    A synchronous 300-s agent turn would blind CPU/RAM/disk and the
    other protocol checks for five minutes per dark protocol."""

    @pytest.fixture
    def blocking_agent(self, agent):
        client, factory = agent
        gate = threading.Event()

        def _ask(*a, **kw):
            gate.wait(timeout=10)
            return REPLY, 10
        client.ask.side_effect = _ask
        yield client, gate
        gate.set()

    def test_fire_returns_while_the_agent_is_still_thinking(self, db, config, blocking_agent):
        client, gate = blocking_agent
        mgr = AlertManager(Mock(), config, db=db)
        t0 = time.monotonic()
        fire(mgr, proto_alert(), wait=False)
        assert time.monotonic() - t0 < 2.0
        # The alert itself is already out and persisted; the agent is not done.
        assert mgr.bot.send_message.call_count == 1
        assert history(db)[0]['kimi_analysis'] is None
        gate.set()
        join_agent(mgr)
        assert history(db)[0]['kimi_analysis'] == REPLY
        assert mgr.bot.send_message.call_count == 2

    def test_same_key_is_not_kicked_twice_while_in_flight(self, db, config, blocking_agent):
        client, gate = blocking_agent
        mgr = AlertManager(Mock(), config, db=db)
        fire(mgr, proto_alert(), wait=False)
        first = mgr._last_agent_thread
        fire(mgr, proto_alert(), wait=False)          # same key, still in flight
        assert mgr._last_agent_thread is first
        # A different key is a different problem and must not be suppressed.
        fire(mgr, proto_alert(key='protocol_down:stls'), wait=False)
        other = mgr._last_agent_thread
        assert other is not first
        gate.set()
        first.join(timeout=10)
        other.join(timeout=10)
        assert client.ask.call_count == 2
        keys = sorted(c.args[0].split(':', 1)[1].rsplit(':', 1)[0]
                      for c in client.ask.call_args_list)
        assert keys == ['protocol_down:reality', 'protocol_down:stls']

    def test_key_is_released_after_the_turn_even_on_failure(self, db, config, agent):
        client, _ = agent
        client.ask.side_effect = RuntimeError('boom')
        mgr = AlertManager(Mock(), config, db=db)
        fire(mgr, proto_alert())
        assert mgr._agent_inflight == set()
        client.ask.side_effect = None
        fire(mgr, proto_alert())
        assert client.ask.call_count == 2
        assert mgr._agent_inflight == set()

    def test_worker_is_a_daemon_thread(self, db, config, agent):
        mgr = AlertManager(Mock(), config, db=db)
        fire(mgr, proto_alert(), wait=False)
        assert mgr._last_agent_thread.daemon is True
        join_agent(mgr)

    def test_dpi_kick_still_runs_inline(self, db, config, agent):
        """DPI is unchanged: synchronous, so the (many) DPI buckets are
        serialised through the tick instead of flooding the agent."""
        client, _ = agent
        mgr = AlertManager(Mock(), config, db=db)
        fire(mgr, dpi_alert(), wait=False)
        client.ask.assert_called_once()
        assert history(db)[0]['kimi_analysis'] == REPLY
        assert mgr._last_agent_thread is None


class TestFollowUpGoesWhereTheAlertWent:

    def test_dashboard_only_protocol_alert_gets_no_chat_follow_up(self, db, config, agent):
        """A warn below ALERT_TG_MIN_SEVERITY never reaches the chat; the
        diagnosis is still stored for the dashboard, but posting it to
        the topic would be a follow-up to a headline nobody saw."""
        client, _ = agent
        bot = Mock()
        fire(AlertManager(bot, config, db=db), proto_alert(severity='warn'))
        client.ask.assert_called_once()
        bot.send_message.assert_not_called()
        assert history(db)[0]['kimi_analysis'] == REPLY

    def test_forum_send_returning_none_falls_back_to_pm_for_criticals(self, db, config, agent):
        """TelegramClient.send_message swallows RequestException / ok:false
        and returns None — the alert and its diagnosis must not vanish."""
        bot = Mock()
        bot.send_message.side_effect = (
            lambda **kw: None if kw.get('chat_id') == config.FORUM_GROUP_ID
            else {'message_id': 1})
        fire(AlertManager(bot, config, db=db), proto_alert())
        chats = [c.kwargs['chat_id'] for c in bot.send_message.call_args_list]
        assert chats == [config.FORUM_GROUP_ID, config.SUPER_ADMIN_ID,
                         config.FORUM_GROUP_ID, config.SUPER_ADMIN_ID]
        assert bot.send_message.call_args_list[3].kwargs['text'].startswith('🤖')


class TestAgentHygiene:
    """agent_client: markers + the output contract in the preamble."""

    @pytest.mark.parametrize('question', [
        'какой протокол не работает',
        'какой протокол не работает?',
        'hy2 отвалился?',
        'stls лежит?',
        'что с пробами',
        'health протоколов',
    ])
    def test_protocol_state_questions_route_to_vpn_ops(self, question):
        assert 'vpn-ops' in _detect_skill_domains(question)

    def test_plain_protocol_question_is_not_generic(self):
        """Before the markers this fell through to __generic__ and the
        agent improvised a host walk instead of opening vpn-ops."""
        assert _detect_skill_domains('какой протокол не работает') == ['vpn-ops']

    def test_incident_phrases_still_win(self):
        assert _detect_skill_domains('лежит всё, протокол reality') == ['incident-response']
        assert _detect_skill_domains('упало всё, hy2 тоже') == ['incident-response']

    def test_existing_routing_unchanged(self):
        assert _detect_skill_domains('проверь это') == ['__generic__']
        assert _detect_skill_domains('привет, как дела') == []

    def test_preamble_carries_the_output_contract(self):
        assert 'ФОРМАТ ОТВЕТА' in SYSTEM_PREAMBLE
        for banned in ('«проверю»', '«у меня есть всё»', '«достаточно»', '«готово»'):
            assert banned in SYSTEM_PREAMBLE
        assert 'русский' in SYSTEM_PREAMBLE
        assert 'переносами строк' in SYSTEM_PREAMBLE

    def test_preamble_names_the_healthcheck_as_first_action(self):
        assert 'python3 /opt/vpn-bot/scripts/protocol_healthcheck.py' in SYSTEM_PREAMBLE
        assert 'ИТОГ' in SYSTEM_PREAMBLE

    def test_preamble_still_ends_with_the_handover_marker(self):
        """Both clients do SYSTEM_PREAMBLE + prompt; the admin's message
        must still start on its own line after the handover phrase."""
        assert SYSTEM_PREAMBLE.endswith('Дальше идёт сообщение от админа:\n\n')
        # And the earlier safety rules survived the edit.
        assert '/opt/vpn-bot/.env' in SYSTEM_PREAMBLE
        assert '[[SEND_FILE:' in SYSTEM_PREAMBLE
