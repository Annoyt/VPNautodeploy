"""The healthcheck on a timer, the agent as an adviser — IMPROVEMENT_PLAN F, D.

* D1  ``protocol_healthcheck.py --post`` reports its ``findings`` (Reality
      dest cert, entry DNAT, exit hop rules, hysteria units, shadow-tls, the
      panel's client fields) to ``/api/healthcheck/report``; each becomes an
      alert ``healthcheck:<key>`` before the probes go dark; a stale or
      blind script pages its own warn.
* D2  the agent names one action from a fixed list; the bot shows it as a
      button that runs ordinary code — admins only, stale buttons refused.
* D3  the exit report's presence becomes per-day ``protocol_usage``;
      ``/usage`` prints protocol × operator.
* D4  hy2 sessions paired from the hysteria journal land in
      ``hy2_sessions``; ``/protocols`` shows how long they last.

Level 2: real sqlite, the real handlers and checks; the agent, the panel
and Telegram are stubbed.
"""

import importlib.util
import json
from pathlib import Path
import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.services import agent_actions, cascade_manual
from bot.services.alert_manager import AlertManager, build_default_checks

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

_spec = importlib.util.spec_from_file_location(
    'protocol_healthcheck_d', os.path.join(os.path.dirname(__file__), '..', '..',
                                           'scripts', 'protocol_healthcheck.py'))
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


DNAT_OK = [
    '-A PREROUTING -p udp -m udp --dport 8400 -j DNAT --to-destination 198.51.100.1:8400',
    '-A PREROUTING -p udp -m udp --dport 443 -j DNAT --to-destination 198.51.100.1:8400',
    '-A PREROUTING -p udp -m udp --dport 20000:40000 -j DNAT --to-destination 198.51.100.1:8400',
    '-A PREROUTING -p udp -m udp --dport 8402 -j DNAT --to-destination 198.51.100.1:8402',
    '-A PREROUTING -p udp -m udp --dport 40001:50000 -j DNAT --to-destination 198.51.100.1:8402',
]
HOP_OK = [
    '-A PREROUTING -p udp -m udp --dport 20000:40000 -j REDIRECT --to-ports 8400',
    '-A PREROUTING -p udp -m udp --dport 40001:50000 -j REDIRECT --to-ports 8402',
]


def layers(**over):
    base = {
        'a': {'ok': True, 'audit': {'rc': 0, 'lines': ['OK: 324 client records']}},
        'b': {'ok': True, 'nat_prerouting': list(DNAT_OK),
              'services': {'shadow-tls': 'active'}},
        'c': {'ok': True, 'cert_record_len': 3920, 'cert_sni': 'www.bing.com',
              'services': {'hysteria': 'active', 'hysteria-turbo': 'active'},
              'nat_prerouting': list(HOP_OK)},
    }
    for layer, patch_ in over.items():
        base[layer] = {**base[layer], **patch_}
    return base


# ------------------------------------------------------------------ D1 -------

class TestFindings:

    def test_a_healthy_host_has_none(self):
        assert hc.findings_for(layers()) == []

    @pytest.mark.parametrize('size,severity', [(7500, 'warn'), (8100, 'critical'), (6900, None)])
    def test_the_dest_cert_creeps_before_it_breaks(self, size, severity):
        found = {f['key']: f for f in hc.findings_for(layers(c={'cert_record_len': size}))}
        assert (found.get('reality_dest_cert') or {}).get('severity') == severity

    def test_units_dnat_hop_and_audit(self):
        found = {f['key']: f['severity'] for f in hc.findings_for(layers(
            a={'audit': {'rc': 1, 'lines': [
                "BROKEN: 80 problem(s)",
                "  inbound-443 (id=1, vless): user_x@nekovo.ru has empty 'flow'"]}},
            b={'nat_prerouting': DNAT_OK[1:], 'services': {'shadow-tls': 'failed'}},
            c={'services': {'hysteria': 'failed', 'hysteria-turbo': 'inactive'},
               'nat_prerouting': HOP_OK[:1]},
        ))}
        assert found == {'panel_audit': 'critical', 'unit:hysteria': 'critical',
                         'unit:hysteria-turbo': 'warn', 'exit_hop:hy2t': 'warn',
                         'entry_dnat:hy2': 'critical', 'unit:shadow-tls': 'critical'}

    def test_every_finding_names_its_layer(self):
        found = hc.findings_for(layers(
            a={'audit': {'rc': 1, 'lines': ["BROKEN: 1", "  inbound-443 (id=1, vless): x has empty 'flow'"]}},
            b={'nat_prerouting': DNAT_OK[1:], 'services': {'shadow-tls': 'failed'}},
            c={'services': {'hysteria': 'failed'}, 'cert_record_len': 8100}))
        assert {f['key']: f['layer'] for f in found} == {
            'panel_audit': 'a', 'entry_dnat:hy2': 'b', 'unit:shadow-tls': 'b',
            'reality_dest_cert': 'c', 'unit:hysteria': 'c'}

    def test_a_switched_off_turbo_raises_nothing(self):
        order = json.dumps([{'name': n, 'enabled': n != 'hy2t'}
                            for n in ('reality', 'hy2', 'ws', 'stls', 'hy2t')])
        found = {f['key'] for f in hc.findings_for(layers(
            a={'cascade_order': order},
            b={'nat_prerouting': [l for l in DNAT_OK if '8402' not in l]},
            c={'services': {'hysteria': 'active', 'hysteria-turbo': 'inactive'},
               'nat_prerouting': [l for l in HOP_OK if '8402' not in l]}))}
        assert not any('hy2t' in k or 'turbo' in k for k in found)
        found_on = {f['key'] for f in hc.findings_for(layers(
            c={'services': {'hysteria': 'active', 'hysteria-turbo': 'inactive'}}))}
        assert 'unit:hysteria-turbo' in found_on

    def test_a_layer_that_could_not_look_finds_nothing(self):
        assert hc.findings_for(layers(b={'ok': False, 'nat_prerouting': []},
                                      c={'ok': False, 'services': {'hysteria': 'failed'}})) == []

    def test_the_report_carries_them(self):
        report = hc.assess(layers(c={'cert_record_len': 8100}))
        assert [f['key'] for f in report['findings']] == ['reality_dest_cert']

    def test_post_sends_the_report_without_the_raw_layers(self, monkeypatch):
        sent = {}

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            sent.update(url=req.full_url, token=req.get_header('X-dpi-token'),
                        body=json.loads(req.data))
            return Resp()
        monkeypatch.setattr(hc.urllib.request, 'urlopen', fake_urlopen)
        ok = hc.post_report('http://127.0.0.1:8080/api/healthcheck/report',
                            {'verdict': 'x', 'exit_code': 0, 'findings': [], 'layers_raw': {'big': 1}},
                            token='sekret')
        assert ok and sent['token'] == 'sekret' and 'layers_raw' not in sent['body']


def _server(db, token='sekret'):
    cfg = SimpleNamespace(BOT_TOKEN='t', DPI_REPORT_TOKEN=token, ENTRY_NODE_IP='',
                          EXIT_NODE_IP='', WEBAPP_URL='', SUB_MIRROR_URLS='')
    return WebAppServer(cfg, db, xui_service=Mock())


def _post(srv, body, token='sekret', handler='handle_healthcheck_report'):
    req = Mock()
    req.headers = {'X-DPI-Token': token}
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    req.content_length = len(raw)
    req.read = AsyncMock(return_value=raw)
    req.json = AsyncMock(return_value=json.loads(raw) if not isinstance(body, bytes) else None)
    import asyncio
    return asyncio.run(getattr(srv, handler)(req))


class TestReportEndpoint:

    def test_stores_the_report(self, db):
        resp = _post(_server(db), {'verdict': 'ИТОГ: всё OK', 'exit_code': 0, 'findings': []})
        assert resp.status == 200
        with db._connect() as conn:
            row = conn.execute("SELECT exit_code, verdict, report_json FROM healthcheck_reports").fetchone()
        assert (row[0], row[1]) == (0, 'ИТОГ: всё OK') and json.loads(row[2])['findings'] == []

    def test_a_big_body_without_a_length_is_refused_after_the_read(self, db):
        import asyncio
        req = Mock()
        req.headers = {'X-DPI-Token': 'sekret'}
        req.content_length = None
        req.read = AsyncMock(return_value=b'{"x": "' + b'a' * (300 * 1024) + b'"}')
        assert asyncio.run(_server(db).handle_healthcheck_report(req)).status == 413

    @pytest.mark.parametrize('token,body,status', [
        ('wrong', {'exit_code': 0}, 403),
        ('sekrét', {'exit_code': 0}, 403),                 # non-ASCII: 403, not a 500
        ('sekret', b'not json', 400),
        ('sekret', b'[1, 2]', 400),
        ('sekret', b'{"x": "' + b'a' * (300 * 1024) + b'"}', 413),
    ])
    def test_gates(self, db, token, body, status):
        assert _post(_server(db), body, token=token).status == status
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM healthcheck_reports").fetchone()[0] == 0


OK3 = {'a': 'ok', 'b': 'ok', 'c': 'ok'}
NO_EXIT = {'a': 'ok', 'b': 'ok', 'c': 'FAILED: ssh: connect to host exit-node: timed out'}


def _report(db, *, minutes_ago=1, exit_code=1, findings=(), layers=OK3):
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).isoformat()
    with db._connect() as conn:
        conn.execute("INSERT INTO healthcheck_reports (ts, exit_code, verdict, report_json) "
                     "VALUES (?, ?, 'v', ?)",
                     (ts, exit_code, json.dumps({'findings': list(findings), 'layers': layers})))
        conn.commit()


def _healthcheck(db):
    cfg = Mock()
    cfg.DB_PATH = db.db_path
    return next(c for c in build_default_checks(cfg, Mock()) if c.__name__ == 'check_healthcheck')


def _check(db):
    prefix, alerts = _healthcheck(db)()[:2]
    assert prefix == 'healthcheck:'
    return {a.key: a for a in alerts}


def _judged(db, key):
    result = _healthcheck(db)()
    judged = result[2] if len(result) == 3 else None
    if judged is None:
        return True
    return judged(key) if callable(judged) else key in judged


def _manager(db):
    cfg = Mock()
    cfg.DB_PATH = db.db_path
    cfg.TOPIC_AI, cfg.FORUM_GROUP_ID, cfg.SUPER_ADMIN_ID = 55, -100, '1'
    cfg.ALERT_TG_MIN_SEVERITY = 'critical'
    cfg.HERMES_URL = ''
    mgr = AlertManager(Mock(), cfg, db=db)
    mgr.register(_healthcheck(db))
    return mgr


def _texts(mgr):
    return [c.kwargs['text'] for c in mgr.bot.send_message.call_args_list]


CERT = {'key': 'reality_dest_cert', 'severity': 'critical', 'title': 'cert 8100', 'next': 'rotate'}


class TestHealthcheckAlerts:

    def test_no_timer_no_alerts(self, db):
        assert _check(db) == {}

    def test_findings_become_alerts(self, db):
        _report(db, findings=[CERT, {'key': 'exit_hop:hy2t', 'severity': 'warn', 'title': 'hop'}])
        alerts = _check(db)
        assert alerts['healthcheck:reality_dest_cert'].severity == 'critical'
        assert 'rotate' in alerts['healthcheck:reality_dest_cert'].detail
        assert alerts['healthcheck:exit_hop:hy2t'].severity == 'warn'

    def test_a_stale_report_is_not_trusted(self, db):
        _report(db, minutes_ago=60, findings=[CERT])
        alerts = _check(db)
        assert list(alerts) == ['healthcheck:stale']
        assert alerts['healthcheck:stale'].severity == 'critical'      # it pages
        assert not _judged(db, 'healthcheck:reality_dest_cert')

    def test_a_stale_report_is_no_recovery(self, db):
        # the review's case: an open finding, then the timer dies
        mgr = _manager(db)
        _report(db, findings=[CERT])
        mgr.run_once()
        with db._connect() as conn:
            conn.execute("UPDATE healthcheck_reports SET ts = ?",
                         ((datetime.utcnow() - timedelta(minutes=60)).isoformat(),))
            conn.commit()
        from bot.services.alert_manager import RECOVERY_TICKS
        for _ in range(RECOVERY_TICKS + 1):
            mgr.run_once()
        assert not any('Восстановилось' in t for t in _texts(mgr))
        assert mgr._state['healthcheck:reality_dest_cert'].open

    def test_a_layer_that_could_not_look_keeps_its_findings(self, db):
        mgr = _manager(db)
        _report(db, findings=[CERT])                          # layer C: the dest cert
        mgr.run_once()
        _report(db, minutes_ago=0, findings=[], layers=NO_EXIT)
        from bot.services.alert_manager import RECOVERY_TICKS
        for _ in range(RECOVERY_TICKS + 1):
            mgr.run_once()
        assert not any('Восстановилось' in t for t in _texts(mgr))
        assert mgr._state['healthcheck:reality_dest_cert'].open
        assert _judged(db, 'healthcheck:entry_dnat:hy2')           # layer B looked

    def test_one_blind_run_is_not_a_pattern(self, db):
        for m, lay in ((31, OK3), (16, OK3), (1, NO_EXIT)):
            _report(db, minutes_ago=m, exit_code=1, layers=lay)
        assert 'healthcheck:blind' not in _check(db)

    def test_three_blind_runs_page_whatever_the_exit_code(self, db):
        # exit unreachable three times while ws is degraded: codes 1, 1, 1
        for m in (31, 16, 1):
            _report(db, minutes_ago=m, exit_code=1, layers=NO_EXIT)
        alert = _check(db)['healthcheck:blind']
        assert alert.severity == 'critical' and 'C (exit)' in alert.title
        _report(db, minutes_ago=0, exit_code=0)
        assert 'healthcheck:blind' not in _check(db)

    def test_a_failed_read_judges_nothing(self, db, tmp_path):
        cfg = Mock()
        cfg.DB_PATH = str(tmp_path / 'no' / 'bot.db')
        check = next(c for c in build_default_checks(cfg, Mock()) if c.__name__ == 'check_healthcheck')
        prefix, alerts, judged = check()
        assert alerts == [] and not judged

    def test_a_cleared_finding_is_posted_as_recovered(self, db, tmp_path):
        mgr = _manager(db)
        _report(db, findings=[CERT])
        mgr.run_once()
        _report(db, minutes_ago=0, findings=[])
        from bot.services.alert_manager import RECOVERY_TICKS
        for _ in range(RECOVERY_TICKS):
            mgr.run_once()
        texts = _texts(mgr)
        assert any('cert 8100' in t and 'Восстановилось' not in t for t in texts)
        assert any('Восстановилось' in t and 'cert 8100' in t for t in texts)

    def test_retention_keeps_the_newest_report(self, db):
        from bot.services.notifications import NotificationService
        _report(db, minutes_ago=60 * 24 * 9)
        _report(db, minutes_ago=60 * 24 * 8)
        svc = NotificationService.__new__(NotificationService)
        svc.db = db
        svc._cleanup_dpi_metrics_sync()
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM healthcheck_reports").fetchone()[0] == 1
        assert list(_check(db)) == ['healthcheck:stale']      # not "never installed"


# ------------------------------------------------------------------ D2 -------

class TestActionParsing:

    @pytest.mark.parametrize('reply,expected', [
        ('ИТОГ: ...\nДЕЙСТВИЕ: restart_xray', ('restart_xray', None)),
        ('ДЕЙСТВИЕ: «demote hy2»', ('demote', 'hy2')),
        ('действие: demote stls', ('demote', 'stls')),
        ('ДЕЙСТВИЕ: restart_xray.', ('restart_xray', None)),
        ('ДЕЙСТВИЕ: restart_xray не нужен', None),
        ('действие: demote stls — там рассинхрон', None),
        ('ДЕЙСТВИЕ: нет', None),
        ('ДЕЙСТВИЕ: demote everything', None),
        ('ДЕЙСТВИЕ: rm -rf /', None),
        ('СЛЕДУЮЩАЯ КОМАНДА: restart_xray', None),
        ('', None),
    ])
    def test_parse(self, reply, expected):
        assert agent_actions.parse(reply) == expected

    def test_button_roundtrip_and_expiry(self):
        kb = agent_actions.keyboard('demote', 'hy2', now=1000)
        data = kb['inline_keyboard'][0][0]['callback_data']
        assert len(data.encode()) <= 64
        assert agent_actions.parse_callback(data, now=1000 + 60) == ('demote', 'hy2')
        assert 'устарела' in agent_actions.parse_callback(
            data, now=1000 + agent_actions.BUTTON_TTL_S + 1)
        assert agent_actions.parse_callback('agent_act:demote:everything:1000', now=1001) \
            == 'неизвестное действие'
        assert agent_actions.parse_callback('agent_act:restart_xray:-:x', now=1) == 'кнопка повреждена'


class TestActionExecution:

    def test_demote_is_a_hand_demotion(self, db):
        text = agent_actions.execute('demote', 'hy2', db=db, config=Mock(), xui=None, admin_id='1')
        assert text.startswith('✅ hy2 в конце каскада')
        assert cascade_manual.demoted_for(db, None) == {'hy2'}
        with db._connect() as conn:
            assert tuple(conn.execute("SELECT action, target_id FROM admin_actions").fetchone()) \
                == ('agent_action_demote', 'hy2')

    @pytest.mark.parametrize('answer,expect', [(True, '✅ xray на exit перезапущен'),
                                               (False, '❌ панель не подтвердила')])
    def test_restart_xray_through_the_panel(self, db, answer, expect):
        xui = Mock()
        xui.api.restart_xray = AsyncMock(return_value=answer)
        xui._run_sync = lambda coro: __import__('asyncio').run(coro)
        text = agent_actions.execute('restart_xray', None, db=db, config=Mock(), xui=xui,
                                     admin_id='1')
        assert text.startswith(expect)
        xui.api.restart_xray.assert_awaited_once()

    def test_a_failure_is_a_line_not_an_exception(self, db):
        xui = Mock()
        xui._run_sync = Mock(side_effect=RuntimeError('panel down'))
        text = agent_actions.execute('restart_xray', None, db=db, config=Mock(), xui=xui,
                                     admin_id='1')
        assert text.startswith('❌ не вышло') and 'panel down' in text


class TestActionButtonUnderTheDiagnosis:

    def _mgr(self, db, reply):
        cfg = Mock()
        cfg.DB_PATH = db.db_path
        cfg.TOPIC_AI, cfg.FORUM_GROUP_ID, cfg.SUPER_ADMIN_ID = 55, -100, '1'
        cfg.ALERT_TG_MIN_SEVERITY = 'critical'
        cfg.AGENT_BACKEND, cfg.HERMES_URL = 'hermes', 'http://hermes:4097'
        client = Mock()
        client.ask.return_value = (reply, 10)
        return AlertManager(Mock(), cfg, db=db), client

    def _fire(self, mgr, client):
        from bot.services.alert_manager import Alert, _Tracker
        with patch('bot.services.agent_factory.build_agent_client', return_value=client):
            mgr._fire(Alert(key='protocol_down:stls', severity='critical', min_cycles=1,
                            title='stls мёртв', detail='d'), _Tracker())
            mgr._last_agent_thread.join(timeout=10)
        return mgr.bot.send_message.call_args_list[-1].kwargs

    def test_the_proposed_action_is_a_button(self, db):
        mgr, client = self._mgr(db, 'ИТОГ: stls лежит\nДЕЙСТВИЕ: restart_xray')
        kw = self._fire(mgr, client)
        button = kw['reply_markup']['inline_keyboard'][0][0]
        assert button['text'] == '▶️ Перезапустить xray на exit'
        assert button['callback_data'].startswith('agent_act:restart_xray:-:')

    def test_no_action_no_button(self, db):
        mgr, client = self._mgr(db, 'ИТОГ: всё живо\nДЕЙСТВИЕ: нет')
        assert self._fire(mgr, client).get('reply_markup') is None

    def test_the_prompt_asks_for_the_line(self):
        from bot.services.alert_manager import Alert, _protocol_agent_prompt
        prompt = _protocol_agent_prompt(Alert(key='protocol_down:hy2', severity='critical',
                                              title='t'))
        assert 'ДЕЙСТВИЕ:' in prompt and 'restart_xray' in prompt


class TestActionHandler:

    def _handler(self, db):
        from bot.handlers.callbacks.admin import AgentActionHandler
        AgentActionHandler._pressed.clear()          # process memory: per test
        bot = Mock()
        bot.services = {'xui': None}
        return AgentActionHandler(bot, db, SimpleNamespace(SUPER_ADMIN_ID='1652899',
                                                           ADMIN_IDS=[])), bot

    def _update(self, data):
        return {'callback_query': {'data': data, 'message': {
            'message_id': 7, 'chat': {'id': -100}, 'text': '🤖 Диагностика'}}}

    def test_admin_press_runs_and_edits(self, db):
        h, bot = self._handler(db)
        data = agent_actions.keyboard('demote', 'ws')['inline_keyboard'][0][0]['callback_data']
        assert h.can_handle(data)
        h.handle(self._update(data), chat_id='-100', user_id='1652899', data=data)
        assert cascade_manual.demoted_for(db, None) == {'ws'}
        edited = bot.client.edit_message_text.call_args.kwargs['text']
        assert edited.startswith('🤖 Диагностика') and '✅ ws в конце каскада' in edited

    def test_others_are_refused(self, db):
        h, bot = self._handler(db)
        data = agent_actions.keyboard('demote', 'ws')['inline_keyboard'][0][0]['callback_data']
        h.handle(self._update(data), chat_id='-100', user_id='42', data=data)
        assert cascade_manual.demoted_for(db, None) == set()
        bot.client.edit_message_text.assert_not_called()

    def test_a_button_runs_once(self, db):
        h, bot = self._handler(db)
        calls = []
        real = agent_actions.execute

        def spy(*a, **kw):
            calls.append(a)
            return real(*a, **kw)
        import unittest.mock as um
        with um.patch.object(agent_actions, 'execute', spy):
            data = agent_actions.keyboard('demote', 'ws')['inline_keyboard'][0][0]['callback_data']
            for _ in range(2):
                h.handle(self._update(data), chat_id='-100', user_id='1652899', data=data)
            # a new process (restart): the audit row still says it ran
            from bot.handlers.callbacks.admin import AgentActionHandler
            AgentActionHandler._pressed.clear()
            h.handle(self._update(data), chat_id='-100', user_id='1652899', data=data)
        assert len(calls) == 1
        assert 'уже выполнено' in bot.client.edit_message_text.call_args.kwargs['text']

    def test_a_failed_edit_answers_in_the_same_topic_not_the_pm(self, db):
        h, bot = self._handler(db)
        bot.client.edit_message_text.return_value = None        # Telegram refused
        data = agent_actions.keyboard('demote', 'ws')['inline_keyboard'][0][0]['callback_data']
        update = self._update(data)
        update['callback_query']['message']['message_thread_id'] = 55
        h.handle(update, chat_id='-100', user_id='1652899', data=data)
        kw = bot.send_message.call_args.kwargs
        assert kw['chat_id'] == '-100' and kw['message_thread_id'] == 55
        assert '✅ ws в конце каскада' in kw['text']

    def test_a_stale_button_does_nothing(self, db):
        h, bot = self._handler(db)
        data = f'agent_act:demote:ws:{int(time.time()) - agent_actions.BUTTON_TTL_S - 5}'
        h.handle(self._update(data), chat_id='-100', user_id='1652899', data=data)
        assert cascade_manual.demoted_for(db, None) == set()
        assert 'устарела' in bot.client.edit_message_text.call_args.kwargs['text']


# ------------------------------------------------------------------ D3/D4 ----

def _users(db):
    with db._connect() as conn:
        conn.executemany("INSERT INTO users (chat_id, username, email, status, last_asn) "
                         "VALUES (?,?,?,?,?)",
                         [('1', 'u1', 'u1@x', 'paid', 'AS31133'),
                          ('2', 'u2', 'u2@x', 'demo', 'AS56981'),
                          ('3', 'u3', 'u3@x', 'demo', None)])
        conn.commit()


class TestUsage:

    def test_two_windows_of_presence_sum_per_day(self, db):
        _users(db)
        srv = _server(db)
        report = {'presence': [{'email': 'u1@x', 'tag': 'inbound-443', 'conns': 3},
                               {'email': 'u2@x', 'tag': 'inbound-2053', 'conns': 2},
                               {'email': 'ghost@x', 'tag': 'inbound-443', 'conns': 9}]}
        for _ in range(2):
            _post(srv, report, handler='handle_dpi_exit_report')
        with db._connect() as conn:
            rows = sorted(tuple(r)[1:] for r in conn.execute("SELECT * FROM protocol_usage"))
        assert rows == [('1', 'reality', 6), ('2', 'cf-ws', 4)]

    def test_the_usage_block_counts_every_inbound(self, db):
        _users(db)
        srv = _server(db)
        _post(srv, {'presence': [{'email': 'u1@x', 'tag': 'inbound-2053', 'conns': 1}],
                    'usage': [{'email': 'u1@x', 'tag': 'inbound-443', 'conns': 40},
                              {'email': 'u1@x', 'tag': 'inbound-2053', 'conns': 1},
                              {'email': 'u1@x', 'tag': 'inbound-x', 'conns': 'junk'}]},
              handler='handle_dpi_exit_report')
        with db._connect() as conn:
            rows = sorted(tuple(r)[1:] for r in conn.execute("SELECT * FROM protocol_usage"))
        assert rows == [('1', 'cf-ws', 1), ('1', 'reality', 40)]

    def test_a_failed_dpi_insert_adds_no_usage(self, db):
        # the reporter re-sends the window after the 500: usage must not double
        _users(db)
        srv = _server(db)
        srv._dpi_exit_rows = lambda payload: [('bad',)]      # an insert that fails
        resp = _post(srv, {'usage': [{'email': 'u1@x', 'tag': 'inbound-443', 'conns': 4}]},
                     handler='handle_dpi_exit_report')
        assert resp.status == 500
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM protocol_usage").fetchone()[0] == 0

    def test_url_tests_do_not_make_a_user(self, db):
        _users(db)
        today = datetime.utcnow().strftime('%Y-%m-%d')
        with db._connect() as conn:
            conn.executemany("INSERT INTO protocol_usage VALUES (?,?,?,?)",
                             [(today, '1', 'reality', 500), (today, '1', 'cf-ws', 20),
                              (today, '1', 'ss2022', 20)])
            conn.commit()
        from bot.handlers.admin.ops import AdminOpsMixin
        h = AdminOpsMixin.__new__(AdminOpsMixin)
        h.db = db
        text = h._usage_report(1)
        head = next(l for l in text.splitlines() if 'сеть' in l and 'всего' in l)
        assert 'real' in head and 'ws' not in head.split() and 'stls' not in head

    def test_usage_table(self, db):
        _users(db)
        today = datetime.utcnow().strftime('%Y-%m-%d')
        with db._connect() as conn:
            conn.executemany("INSERT INTO protocol_usage VALUES (?,?,?,?)",
                             [(today, '1', 'reality', 5), (today, '2', 'cf-ws', 1),
                              (today, '3', 'ss2022', 2)])
            conn.execute("INSERT INTO hy2_auth_log (chat_id, decision) VALUES ('1', 'allow')")
            conn.commit()
        from bot.handlers.admin.ops import AdminOpsMixin
        h = AdminOpsMixin.__new__(AdminOpsMixin)
        h.db = db
        text = h._usage_report(7)
        assert 'AS31133' in text and 'AS56981' in text and '?' in text
        line = next(l for l in text.splitlines() if l.startswith('AS31133'))
        assert line.split()[-1] == '1'                     # one user, two protocols


class TestHy2Sessions:

    def test_report_sessions_are_stored_with_reasons(self, db):
        _users(db)
        _post(_server(db), {'hy2_sessions': [
            {'id': 'u1@x', 'unit': 'hysteria', 'duration_s': 2400,
             'error': 'timeout: no recent network activity'},
            {'id': 'u2@x', 'unit': 'hysteria', 'duration_s': 60,
             'error': 'accepting stream failed: Application error 0x107 (local)'},
            {'id': 'ghost@x', 'unit': 'hysteria-turbo', 'duration_s': 5, 'error': 'boom'},
            {'id': 'u1@x', 'duration_s': 'x'},
        ]}, handler='handle_dpi_exit_report')
        with db._connect() as conn:
            rows = sorted((tuple(r)[2:] for r in conn.execute("SELECT * FROM hy2_sessions")),
                          key=lambda r: (r[0] or '', r[1]))
        assert rows == [(None, 'hysteria-turbo', 5, 'other'),
                        ('1', 'hysteria', 2400, 'idle-timeout'),
                        ('2', 'hysteria', 60, 'closed-by-server')]

    def test_the_journals_end_time_is_kept_and_a_resent_window_adds_nothing(self, db):
        _users(db)
        srv = _server(db)
        ended = time.time() - 3600
        body = {'hy2_sessions': [{'id': 'u1@x', 'unit': 'hysteria', 'duration_s': 90,
                                  'error': '', 'ended_at': ended}]}
        for _ in range(2):
            _post(srv, body, handler='handle_dpi_exit_report')
        with db._connect() as conn:
            rows = conn.execute("SELECT ended_at FROM hy2_sessions").fetchall()
        assert len(rows) == 1
        assert rows[0][0].startswith(datetime.utcfromtimestamp(ended).strftime('%Y-%m-%dT%H:%M'))

    def test_protocols_card_line(self, db):
        now = datetime.utcnow()
        with db._connect() as conn:
            conn.executemany("INSERT INTO hy2_sessions (ended_at, chat_id, instance, duration_s, reason) "
                             "VALUES (?,?,?,?,?)",
                             [(now.isoformat(), '1', 'hysteria', d, r) for d, r in
                              ((60, 'idle-timeout'), (400, 'idle-timeout'), (2400, 'closed-by-server'))])
            conn.commit()
        from bot.handlers.admin.ops import AdminOpsMixin
        h = AdminOpsMixin.__new__(AdminOpsMixin)
        h.db = db
        line = h._hy2_sessions_line(now)
        assert line.startswith('⏱ hy2 сессии за 24 ч: 3, медиана 6 мин')
        assert '≥ 30 мин — 33%' in line and 'idle-timeout (66%)' in line


class TestInstaller:
    """scripts/install_protocol_healthcheck.sh: root-owned copies of the
    script and probe_rules.py, the units pointing at them."""

    def test_installs_copies_and_enables_the_timer(self, tmp_path):
        import os
        import subprocess
        repo = Path(__file__).resolve().parents[2]
        lib, units, log = tmp_path / 'lib', tmp_path / 'units', tmp_path / 'systemctl.log'
        fake = tmp_path / 'systemctl'
        fake.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
        fake.chmod(0o755)
        env = dict(os.environ, HC_LIB_DIR=str(lib), HC_UNIT_DIR=str(units),
                   SYSTEMCTL=str(fake), HC_ALLOW_NONROOT='1')
        units.mkdir()
        r = subprocess.run(['bash', str(repo / 'scripts' / 'install_protocol_healthcheck.sh')],
                           env=env, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert (lib / 'protocol_healthcheck.py').read_bytes() == \
            (repo / 'scripts' / 'protocol_healthcheck.py').read_bytes()
        assert (lib / 'probe_rules.py').read_bytes() == \
            (repo / 'bot' / 'services' / 'probe_rules.py').read_bytes()
        assert oct((lib / 'protocol_healthcheck.py').stat().st_mode & 0o777) == '0o755'
        unit = (units / 'protocol-healthcheck.service').read_text()
        assert f'{"/usr/local/lib/nekovpn-healthcheck"}/protocol_healthcheck.py' in unit
        assert '/opt/vpn-bot/scripts' not in unit.split('ExecStart=')[1].split('\n')[0]
        assert 'NoNewPrivileges=yes' in unit
        assert 'enable --now protocol-healthcheck.timer' in log.read_text()
        # the installed copy finds its module next to it
        r = subprocess.run(['python3', '-c', 'import runpy, sys; sys.argv=["x"]; '
                            f'm = runpy.run_path("{lib}/protocol_healthcheck.py", run_name="t"); '
                            'print(m["probe_rules"].RESERVE_TAG)'],
                           capture_output=True, text=True, timeout=30)
        assert r.stdout.strip() == 'de', r.stderr
