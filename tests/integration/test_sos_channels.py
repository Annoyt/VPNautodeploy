"""SOS channels (IMPROVEMENT_PLAN E19 /sos, E27 /kit, E29 /share, A1.2
/nudge_sub) and the emergency profile behind them (``?emergency=1``).

Why
---
Whatever a user needs during an outage has to reach them BEFORE it: the
emergency profile (the lockdown profile — ws first, DNS through the
tunnel — forced for one user), the "what works right now" summary, the
offline kit. On the operator side a /sos is a failure report with an SOS
mark, a row DPIMonitor's R5 counts, and an agent diagnosis.

Level 2: real sqlite (Database creates every table the flows read and
write: outbound_health, user_presence, hy2_auth_log, dpi_metrics,
user_failure_reports, app_settings, admin_actions). Only Telegram, the
panel read and the agent are faked. The profile builders are REAL —
what the emergency link serves is what the tests parse.
"""

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

import bot.services.sos as sos
from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
from bot.models.user import User
from bot.services.agent_client import _detect_skill_domains
from bot.services.sos import SosService
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

CID = '52291265'
UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
GROUP, TOPIC_SUPPORT, TOPIC_AI = -1001234, 17, 55
ADMIN = '1652899'


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_config(**over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL='https://dash.example.com',
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='4bRlZyNH8VVqnRd3w6M_sYC6a1VKyXUvt5BBvXmnmHE',
        SNI_VALUE='www.bing.com', SID_VALUE='0123456789abcdef',
        HY2_HOST='203.0.113.20', HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='443,20000:40000', HY2T_PORT='',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api/v1/forecast',
        WS_SNI='cdn.example.com',
        STLS_HOST='203.0.113.20', STLS_PORT=443, STLS_SNI='www.microsoft.com',
        STLS_VERSION=3, STLS_PASSWORD='stls-pw',
        SS_METHOD='2022-blake3-aes-128-gcm',
        SS_SERVER_PASSWORD='c3J2LXB3LTE2Ynl0ZXMhIQ==', SS_USER_SALT='salt',
        FORUM_GROUP_ID=GROUP, TOPIC_SUPPORT=TOPIC_SUPPORT, TOPIC_AI=TOPIC_AI,
        FORUM_ENABLED=True, SUPER_ADMIN_ID=ADMIN,
        AGENT_BACKEND='hermes', HERMES_URL='', DB_PATH='',
    )
    cfg.update(over)
    ns = SimpleNamespace(**cfg)
    ns.is_admin = lambda user_id: str(user_id) == ADMIN
    return ns


@pytest.fixture(autouse=True)
def _clean_state():
    SosService._kit_sent_at.clear()
    MK._panel_skip_until = 0.0
    sos._agent = None
    yield
    SosService._kit_sent_at.clear()
    MK._panel_skip_until = 0.0
    sos._agent = None


@pytest.fixture
def panel(monkeypatch):
    """The panel's lastOnline for any client: 12 minutes ago."""
    seen = utcnow() - timedelta(minutes=12)
    ms = int(seen.replace(tzinfo=timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(MK, '_fetch_panel_last_online_ms', lambda self, email: ms)
    return seen


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def config():
    return make_config()


@pytest.fixture
def bot():
    b = Mock()
    b.send_message.return_value = {'message_id': 1}
    b.send_document.return_value = {'message_id': 2}
    return b


def add_user(db, chat_id=CID, *, status='paid', uuid=UUID, lang='ru',
             platform='android', asn='AS31133', country='RU', username='ziriki',
             email=None, contact_email=None):
    db._users.save(User(
        chat_id=chat_id, username=username, status=status, uuid=uuid,
        email=email or (f'user_{username}_{chat_id}@nekovo.ru' if uuid else None),
        lang=lang, platform=platform, last_asn=asn, last_country=country,
        contact_email=contact_email,
    ))
    return db.get_user(chat_id)


def seed(db, sql, *args):
    with db._connect() as conn:
        conn.execute(sql, args)
        conn.commit()


def seed_probes(db, tag, ok_per_run, *, runs=3, minutes_ago=5):
    """``runs`` probe runs of 10 rows (the HealthChecker shape); a failed
    probe has no latency (nothing came back)."""
    with db._connect() as conn:
        for run in range(runs):
            ts = (utcnow() - timedelta(minutes=minutes_ago + run * 15)).isoformat()
            for i in range(10):
                st = 'ok' if i < ok_per_run else 'timeout'
                conn.execute(
                    "INSERT INTO outbound_health (outbound_tag, target_domain, "
                    "status, latency_ms, error_msg, ts) VALUES (?,?,?,?,?,?)",
                    (tag, f'd{i}.example', st, 120 if st == 'ok' else None,
                     None if st == 'ok' else st, ts))
        conn.commit()


def healthy_probes(db):
    for tag, ok in (('ws', 7), ('stls', 7), ('reality', 7), ('hy2', 7)):
        seed_probes(db, tag, ok)


def sent(bot, chat_id):
    return [c.kwargs for c in bot.send_message.call_args_list
            if str(c.kwargs.get('chat_id')) == str(chat_id)]


def topic_posts(bot):
    return [kw for kw in sent(bot, GROUP)
            if kw.get('message_thread_id') == TOPIC_SUPPORT]


def report_rows(db, target=None):
    with db._connect() as conn:
        sql = ("SELECT chat_id, asn, last_traffic_ts, target_domain "
               "FROM user_failure_reports")
        if target:
            rows = conn.execute(sql + " WHERE target_domain = ?", (target,)).fetchall()
        else:
            rows = conn.execute(sql).fetchall()
        return [tuple(r) for r in rows]


def sub_url(config, user):
    return SubscriptionService(config).build_subscription_url(user)


# ====================================================================
# The emergency profile: get_cascade_order(force_lockdown=) and
# /sub?emergency=1
# ====================================================================

class TestForceLockdown:

    def test_projects_ws_first_while_lockdown_is_off(self, db):
        paid = add_user(db, status='paid')
        demo = add_user(db, '2', status='demo', uuid='22222222-1d4b-4c5e-9f7a-2b3c4d5e6f70')
        # RU users: the resilience ladder, hy2t first for paid
        assert MK.get_cascade_order(db, user=paid) == ('hy2t', 'stls', 'ws', 'hy2', 'reality')
        assert MK.get_cascade_order(db, user=paid, force_lockdown=True) == \
            ('ws', 'stls', 'reality', 'hy2', 'hy2t')
        assert MK.get_cascade_order(db, user=demo) == ('stls', 'ws', 'hy2')
        assert MK.get_cascade_order(db, user=demo, force_lockdown=True) == \
            ('ws', 'stls', 'hy2')

    def test_wins_over_apply_lockdown_false(self, db):
        u = add_user(db)
        assert MK.get_cascade_order(db, user=u, apply_lockdown=False,
                                    force_lockdown=True)[0] == 'ws'

    def test_operator_lockdown_order_is_used(self, db):
        u = add_user(db)
        db.set_setting('cascade_lockdown', json.dumps(['stls', 'ws']))
        assert MK.get_cascade_order(db, user=u, force_lockdown=True)[:2] == ('stls', 'ws')

    def test_monitor_demotions_still_sink_in_the_emergency_order(self, db):
        u = add_user(db)
        db.set_setting('cascade_auto', json.dumps({'global': {'ws': {'reason': 'probe_dark'}}}))
        order = MK.get_cascade_order(db, user=u, force_lockdown=True)
        assert order[0] == 'stls' and order[-1] == 'ws'

    def test_without_the_kwarg_nothing_changes(self, db):
        u = add_user(db)
        assert MK.get_cascade_order(db, user=u) == \
            MK.get_cascade_order(db, user=u, force_lockdown=False)


class TestEmergencySub:
    """The handler with the REAL builders."""

    @pytest.fixture
    def server(self, db, config):
        add_user(db)
        srv = WebAppServer(config, db, xui_service=Mock())
        srv.xui.get_client_traffic = AsyncMock(return_value={})
        return srv

    def _get(self, server, query):
        req = Mock()
        req.match_info = {'token': server.subscription.derive_token(UUID)}
        req.rel_url = SimpleNamespace(query=query)
        req.headers = {'User-Agent': 'HiddifyNext/4.1.2 (android)'}
        req.remote = ''
        resp = asyncio.run(server.handle_subscription(req))
        assert resp.status == 200
        return resp

    def _reference(self, db, config, *, lockdown=False, fmt=''):
        """What /sub served before this change: the operator order (mode
        inactive) and the normal DNS."""
        user = db.get_user(CID)
        cascade = MK.get_cascade_order(db, user=user, country=None, asn=None)
        svc = SubscriptionService(config)
        if fmt == 'clash':
            return svc.build_clash_config(user, cascade, lockdown=lockdown)
        return json.dumps(svc.build_singbox_config(user, cascade, lockdown=lockdown))

    def test_singbox_emergency_is_ws_first_with_dns_through_the_tunnel(self, server):
        cfg = json.loads(self._get(server, {'emergency': '1'}).text)
        assert cfg['dns']['rules'] == [{'clash_mode': 'Direct', 'server': 'local'}]
        assert cfg['dns']['final'] == 'remote'
        selector = next(o for o in cfg['outbounds'] if o['tag'] == 'proxy')
        assert selector['outbounds'][1].endswith('-cdn-ws')

    def test_clash_emergency_is_ws_first_with_dns_through_the_tunnel(self, server):
        resp = self._get(server, {'format': 'clash', 'emergency': '1'})
        cfg = json.loads(resp.text)
        assert cfg['dns']['nameserver'] == ['https://1.1.1.1/dns-query#VPN']
        assert cfg['proxies'][0]['name'].endswith('-cdn-ws')
        assert 'NekoVPN-SOS.yaml' in resp.headers['content-disposition']
        assert resp.headers['profile-title'] == 'NekoVPN SOS'

    def test_normal_profile_is_byte_identical(self, server, db, config):
        for query in ({}, {'emergency': '0'}, {'emergency': 'true'}, {'emergency': ''}):
            resp = self._get(server, query)
            assert resp.text == self._reference(db, config), query
            assert resp.headers['profile-title'] == 'NekoVPN'
        resp = self._get(server, {'format': 'clash'})
        assert resp.text == self._reference(db, config, fmt='clash')
        assert "filename*=UTF-8''NekoVPN.yaml" in resp.headers['content-disposition']

    def test_emergency_equals_the_active_lockdown_profile(self, server, db, config):
        emergency = self._get(server, {'emergency': '1'}).text
        db.set_setting('lockdown_mode', json.dumps(
            {'mode': 'on', 'active': True, 'by': 'admin:1'}))
        assert self._get(server, {}).text == emergency
        assert self._get(server, {'emergency': '1'}).text == emergency

    def test_provider_refresh_ignores_emergency(self, server):
        """``clash-proxies`` is a bare server list for the profile that
        holds it (§32) — the cascade order there is the normal one."""
        plain = self._get(server, {'format': 'clash-proxies'})
        flagged = self._get(server, {'format': 'clash-proxies', 'emergency': '1'})
        assert flagged.text == plain.text
        # the normal RU order (no hy2t on this deployment), not ws-first
        assert json.loads(plain.text)['proxies'][0]['name'].endswith('-stls')

    def test_kit_yaml_is_what_the_emergency_link_serves(self, server, db, config, bot):
        """Demotion-aware groups and providers included: the file a user
        saved is the profile the /sos link would load right now."""
        db.set_setting('cascade_auto', json.dumps(
            {'asn': {'AS31133': {'reality': {'reason': 'reality_asn'}}}}))
        served = self._get(server, {'format': 'clash', 'emergency': '1'}).text
        groups = {g['name']: g for g in json.loads(served)['proxy-groups']}
        assert not any(n.endswith('-reality') for n in groups['Cascade']['proxies'])
        run_kit(bot, db, config)
        assert kit_files(bot)['NekoVPN-emergency.yaml'].decode() == served


# ====================================================================
# /sos (E19)
# ====================================================================

class TestSos:

    def _sos(self, bot, db, config, user=None, **kw):
        return SosService(bot, db, config).handle_sos(CID, user or db.get_user(CID), **kw)

    def test_answer_report_row_and_topic_post(self, bot, db, config, panel):
        user = add_user(db)
        healthy_probes(db)
        rid = self._sos(bot, db, config)

        to_user = sent(bot, CID)
        assert len(to_user) == 1
        text, kb = to_user[0]['text'], to_user[0]['reply_markup']
        base = sub_url(config, user)
        assert f'<code>{base}?emergency=1</code>' in text
        assert f'<code>{base}?format=clash&amp;emergency=1</code>' in text
        assert 'Обнови основную подписку' in text
        assert '✅ Cloudflare (WS) — работает' in text
        assert text.index('Cloudflare (WS)') < text.index('ShadowTLS') < text.index('Reality')
        callbacks = [b.get('callback_data') for row in kb['inline_keyboard'] for b in row]
        assert 'sos:kit' in callbacks and 'sos:share' in callbacks

        assert rid is not None
        assert report_rows(db) == [(CID, 'AS31133',
                                    panel.strftime('%Y-%m-%d %H:%M:%S'), 'sos')]
        posts = topic_posts(bot)
        assert len(posts) == 1
        report = posts[0]['text']
        assert report.startswith(f'🆘 <b>SOS #{rid}</b> · /sos')
        assert '<code>@ziriki</code>' in report
        assert 'Last traffic: 12 мин назад' in report
        assert 'Network: RU / AS31133' in report
        assert 'Аварийный каскад: ws, stls, reality, hy2' in report
        assert 'Агент не настроен' in report
        assert not sent(bot, ADMIN)                      # never a PM

    def test_second_sos_within_10_min_is_short_and_silent(self, bot, db, config, panel):
        add_user(db)
        assert self._sos(bot, db, config) is not None
        bot.send_message.reset_mock()
        assert self._sos(bot, db, config) is None
        (only,) = sent(bot, CID)
        assert only['text'].startswith('⏳ SOS уже получен только что')
        assert not topic_posts(bot)
        assert len(report_rows(db)) == 1

    def test_limit_is_ten_minutes(self, bot, db, config, panel):
        add_user(db)
        old = (utcnow() - timedelta(minutes=11)).strftime('%Y-%m-%d %H:%M:%S')
        recent = (utcnow() - timedelta(minutes=9)).strftime('%Y-%m-%d %H:%M:%S')
        seed(db, "INSERT INTO user_failure_reports (ts, chat_id, target_domain) "
                 "VALUES (?, ?, 'sos')", old, CID)
        assert self._sos(bot, db, config) is not None          # 11 min: allowed
        seed(db, "DELETE FROM user_failure_reports")
        seed(db, "INSERT INTO user_failure_reports (ts, chat_id, target_domain) "
                 "VALUES (?, ?, 'sos')", recent, CID)
        bot.send_message.reset_mock()
        assert self._sos(bot, db, config) is None              # 9 min: limited
        assert '9 мин назад' in sent(bot, CID)[0]['text']

    def test_a_failure_report_does_not_count_against_sos(self, bot, db, config, panel):
        add_user(db)
        seed(db, "INSERT INTO user_failure_reports (chat_id, target_domain) "
                 "VALUES (?, 'nothing_loads')", CID)
        assert self._sos(bot, db, config) is not None

    @pytest.mark.parametrize('status,uuid', [('new', None), ('pending_demo', None),
                                             ('banned', UUID), ('demo', None)])
    def test_without_an_active_key_it_refuses(self, bot, db, config, status, uuid):
        add_user(db, status=status, uuid=uuid)
        assert self._sos(bot, db, config) is None
        (only,) = sent(bot, CID)
        assert only['text'] == '⚠️ Сначала получите ключ — /start'
        assert not topic_posts(bot) and not report_rows(db)

    def test_unknown_user_refuses_in_russian(self, bot, db, config):
        assert SosService(bot, db, config).handle_sos(CID, None) is None
        assert sent(bot, CID)[0]['text'].startswith('⚠️ Сначала получите ключ')

    def test_english(self, bot, db, config, panel):
        add_user(db, lang='en')
        self._sos(bot, db, config)
        text = sent(bot, CID)[0]['text']
        assert text.startswith('🆘 <b>Emergency mode</b>')
        assert 'Refresh your main subscription' in text
        assert 'Profiles → + → URL' in text

    def test_iphone_gets_karing_and_no_flclash(self, bot, db, config, panel):
        user = add_user(db, platform='ios')
        self._sos(bot, db, config)
        text = sent(bot, CID)[0]['text']
        assert 'Karing' in text and 'FlClash' not in text and 'format=clash' not in text
        assert f'<code>{sub_url(config, user)}?emergency=1</code>' in text

    def test_no_public_url_still_answers_and_reports(self, bot, db, panel):
        add_user(db)
        rid = self._sos(bot, db, make_config(WEBAPP_URL=''))
        assert 'сейчас недоступна' in sent(bot, CID)[0]['text']
        assert rid is not None and topic_posts(bot)

    def test_demo_user_sees_only_their_tier(self, bot, db, config, panel):
        add_user(db, status='demo')
        healthy_probes(db)
        self._sos(bot, db, config)
        text = sent(bot, CID)[0]['text']
        assert 'Cloudflare (WS)' in text and 'Hysteria2' in text
        assert 'Reality' not in text


class TestWhatWorks:

    def _text(self, bot, db, config):
        SosService(bot, db, config).handle_sos(CID, db.get_user(CID))
        return sent(bot, CID)[0]['text']

    def test_probe_verdicts_match_protocols_card(self, bot, db, config, panel):
        add_user(db)
        seed_probes(db, 'ws', 7)
        seed_probes(db, 'stls', 4)          # last run 4/10 → degraded
        seed_probes(db, 'reality', 0)       # 30 rows, none alive → down
        text = self._text(bot, db, config)
        assert '✅ Cloudflare (WS) — работает' in text
        assert '⚠️ ShadowTLS — с перебоями' in text
        assert '❌ Reality — не отвечает' in text
        assert 'Hysteria2 —' not in text    # no rows → not claimed either way

    def test_stale_probes_are_not_presented_as_status(self, bot, db, config, panel):
        add_user(db)
        seed_probes(db, 'ws', 7, minutes_ago=120)
        text = self._text(bot, db, config)
        assert 'Свежих проверок серверов сейчас нет' in text
        assert 'Cloudflare (WS) — работает' not in text

    def test_other_users_of_the_same_network(self, bot, db, config, panel):
        add_user(db)
        add_user(db, '901', username='a', uuid='aaaaaaaa-1d4b-4c5e-9f7a-2b3c4d5e6f70')
        add_user(db, '902', username='b', uuid='bbbbbbbb-1d4b-4c5e-9f7a-2b3c4d5e6f70')
        add_user(db, '903', username='c', uuid='cccccccc-1d4b-4c5e-9f7a-2b3c4d5e6f70',
                 asn='AS8359')                                   # another network
        fresh = (utcnow() - timedelta(minutes=10)).isoformat()
        for email in ('user_a_901@nekovo.ru', 'user_b_902@nekovo.ru', 'user_c_903@nekovo.ru'):
            seed(db, "INSERT INTO user_presence (email, inbound_tag, proto, conns, seen_at) "
                     "VALUES (?, 'inbound-443', 'reality', 2, ?)", email, fresh)
        seed(db, "INSERT INTO user_presence (email, inbound_tag, proto, conns, seen_at) "
                 "VALUES (?, 'inbound-443', 'reality', 2, ?)",
             'user_ziriki_52291265@nekovo.ru', fresh)            # the user themself
        seed(db, "INSERT INTO hy2_auth_log (ts, chat_id, decision) VALUES (?, '901', 'allow')",
             (utcnow() - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S'))
        seed(db, "INSERT INTO dpi_metrics (snapshot_at, country, asn, as_org, inbound_tag, "
                 "conn_count) VALUES (?, 'RU', 'AS31133', 'PJSC MegaFon', 'reality', 5)",
             utcnow().isoformat())
        seed(db, "INSERT INTO user_failure_reports (chat_id, asn, target_domain) "
                 "VALUES ('902', 'AS31133', 'nothing_loads')")
        db.set_setting('cascade_auto', json.dumps(
            {'asn': {'AS31133': {'reality': {'reason': 'reality_asn'}}}}))

        text = self._text(bot, db, config)
        assert ('📶 Твоя сеть (PJSC MegaFon, AS31133): за последний час у других '
                'работали: Reality — 2 чел., Hysteria2 — 1 чел.') in text
        assert 'Там сейчас хуже проходит Reality' in text
        assert 'Ещё 1 чел. из твоей сети сообщили о проблемах' in text

    def test_stale_presence_is_not_counted(self, bot, db, config, panel):
        add_user(db)
        add_user(db, '901', username='a', uuid='aaaaaaaa-1d4b-4c5e-9f7a-2b3c4d5e6f70')
        seed(db, "INSERT INTO user_presence (email, inbound_tag, proto, conns, seen_at) "
                 "VALUES ('user_a_901@nekovo.ru', 'inbound-2053', 'cf-ws', 2, ?)",
             (utcnow() - timedelta(minutes=90)).isoformat())
        assert 'данных от других нет' in self._text(bot, db, config)

    def test_unknown_network_asks_for_a_refresh(self, bot, db, config, panel):
        add_user(db, asn=None)
        assert 'Твою сеть мы пока не знаем — обнови подписку' in self._text(bot, db, config)


class TestSosAgent:
    """The protocol_down plumbing, re-used: a worker thread, one turn per
    user, a slot cap — the reply lands in the SUPPORT topic, never a PM."""

    REPLY = "ИТОГ: работает только ws.\nПОДОЗРЕВАЕМЫЙ: Reality режется у AS31133"

    @pytest.fixture
    def agent_config(self):
        return make_config(HERMES_URL='http://hermes:4097')

    @pytest.fixture
    def agent(self):
        client = Mock()
        client.ask.return_value = (self.REPLY, 1200)
        with patch('bot.services.agent_factory.build_agent_client',
                   return_value=client):
            yield client

    def _join(self):
        t = sos._agent._last_agent_thread
        t.join(timeout=10)
        assert not t.is_alive()

    def test_diagnosis_is_kicked_and_posted_to_the_support_topic(
            self, bot, db, agent_config, agent, panel):
        add_user(db)
        rid = SosService(bot, db, agent_config).handle_sos(CID, db.get_user(CID))
        self._join()
        prompt = agent.ask.call_args.args[1]
        assert 'python3 /opt/vpn-bot/scripts/protocol_healthcheck.py' in prompt
        assert f'chat_id {CID}' in prompt and f'№{rid}' in prompt
        assert 'Last traffic: 12 мин назад' in prompt
        posts = topic_posts(bot)
        assert 'Диагностика агентом запущена' in posts[0]['text']
        assert 'Диагностика по алерту' in posts[1]['text']
        assert 'ИТОГ: работает только ws.' in posts[1]['text']
        assert not sent(bot, ADMIN)
        assert not [kw for kw in sent(bot, GROUP)
                    if kw.get('message_thread_id') == TOPIC_AI]

    def test_busy_agent_is_skipped_not_queued(self, bot, db, agent_config, agent, panel):
        add_user(db)
        held = sos._agent_for(bot, agent_config, db)
        assert held.slots.acquire(blocking=False)
        try:
            SosService(bot, db, agent_config).handle_sos(CID, db.get_user(CID))
        finally:
            held.slots.release()
        assert 'Агент занят' in topic_posts(bot)[0]['text']
        agent.ask.assert_not_called()

    def test_prompt_routes_to_vpn_ops(self, db):
        user = add_user(db)
        prompt = sos.agent_prompt(
            user, 12, '/sos',
            ['Network: RU / AS31133 (по /sub 3 ч назад)',
             'Last traffic: 12 мин назад, 15:27 UTC',
             'Protocols: Reality 20 мин назад (соединений за 5 мин: 3) · Hy2 1 ч назад'],
            ('ws', 'stls', 'reality', 'hy2'))
        domains = _detect_skill_domains(prompt)
        assert 'vpn-ops' in domains
        assert 'incident-response' not in domains and 'code-review' not in domains
        assert 'billing-ops' not in domains


# ====================================================================
# /kit (E27)
# ====================================================================

def run_kit(bot, db, config, user=None):
    t = SosService(bot, db, config).send_kit(CID, user or db.get_user(CID))
    if t is not None:
        t.join(timeout=10)
        assert not t.is_alive()
    return t


def kit_files(bot):
    return {c.kwargs['filename']: c.kwargs['content']
            for c in bot.send_document.call_args_list}


class TestKit:

    def test_two_documents_with_the_emergency_profile(self, bot, db, config):
        user = add_user(db)
        run_kit(bot, db, config)
        files = kit_files(bot)
        assert set(files) == {'NekoVPN-emergency.yaml', 'NekoVPN-emergency.json'}
        for call in bot.send_document.call_args_list:
            assert call.kwargs['chat_id'] == CID
        clash = json.loads(files['NekoVPN-emergency.yaml'])
        singbox = json.loads(files['NekoVPN-emergency.json'])
        # the yaml IS what /sub?format=clash&emergency=1 serves
        cascade = MK.get_cascade_order(db, user=user, force_lockdown=True)
        assert files['NekoVPN-emergency.yaml'].decode() == \
            SubscriptionService(config).build_clash_config(user, cascade, lockdown=True)
        assert clash['dns']['nameserver'] == ['https://1.1.1.1/dns-query#VPN']
        assert clash['proxies'][0]['name'].endswith('-cdn-ws')
        assert singbox['dns']['final'] == 'remote'
        selector = next(o for o in singbox['outbounds'] if o['tag'] == 'proxy')
        assert selector['outbounds'][1].endswith('-cdn-ws')

    def test_instruction_first_then_files(self, bot, db, config):
        add_user(db)
        run_kit(bot, db, config)
        names = [c[0] for c in bot.mock_calls if c[0] in ('send_message', 'send_document')]
        assert names == ['send_message', 'send_document', 'send_document']
        intro = sent(bot, CID)[0]['text']
        assert 'Профили → + → Файл' in intro
        assert 'Hiddify' in intro and 'Буфер обмена' in intro
        assert 'сохрани их сейчас' in intro and 'не пересылай' in intro
        captions = {c.kwargs['filename']: c.kwargs['caption']
                    for c in bot.send_document.call_args_list}
        assert captions['NekoVPN-emergency.yaml'].startswith('FlClash')

    def test_once_per_ten_minutes(self, bot, db, config):
        add_user(db)
        run_kit(bot, db, config)
        assert run_kit(bot, db, config) is None
        assert bot.send_document.call_count == 2
        assert sent(bot, CID)[-1]['text'].startswith('⏳ Комплект уже отправлен только что')
        SosService._kit_sent_at[CID] = time.time() - 590   # inside 10 min
        assert run_kit(bot, db, config) is None
        SosService._kit_sent_at[CID] = time.time() - 610   # past it
        run_kit(bot, db, config)
        assert bot.send_document.call_count == 4

    def test_failed_upload_does_not_block_a_retry(self, bot, db, config):
        add_user(db)
        bot.send_document.return_value = None
        run_kit(bot, db, config)
        assert sent(bot, CID)[-1]['text'].startswith('⚠️ Не получилось отправить комплект')
        assert CID not in SosService._kit_sent_at
        bot.send_document.return_value = {'message_id': 3}
        assert run_kit(bot, db, config) is not None

    def test_without_a_key_it_refuses(self, bot, db, config):
        add_user(db, status='new', uuid=None)
        assert run_kit(bot, db, config) is None
        bot.send_document.assert_not_called()
        assert sent(bot, CID)[0]['text'].startswith('⚠️ Сначала получите ключ')

    def test_english(self, bot, db, config):
        add_user(db, lang='en')
        run_kit(bot, db, config)
        assert 'Profiles → + → File' in sent(bot, CID)[0]['text']


# Real validators, when the images are on this machine (never pulled here).
# CI deselects them by marker (-m "not requires_docker and not
# requires_network"); elsewhere they skip themselves without the image.
MIHOMO_IMAGE = 'metacubex/mihomo:latest'
SINGBOX_IMAGE = 'ghcr.io/sagernet/sing-box:v1.11.15'
GEO_CACHE = Path(tempfile.gettempdir()) / 'nekovpn-test-cache' / 'mihomo'


def _have_image(image):
    if not shutil.which('docker'):
        return False
    try:
        return subprocess.run(['docker', 'image', 'inspect', image],
                              capture_output=True, timeout=20).returncode == 0
    except Exception:
        return False


class TestKitValidators:

    @pytest.fixture
    def kit(self, bot, db, config):
        add_user(db, status='paid', lang='en')     # every protocol + RU exit
        run_kit(bot, db, config)
        return kit_files(bot)

    @pytest.mark.requires_docker
    @pytest.mark.requires_network       # geodata download on the first run
    def test_yaml_passes_mihomo_t(self, kit, tmp_path):
        if not _have_image(MIHOMO_IMAGE):
            pytest.skip(f'{MIHOMO_IMAGE} not available')
        home = tmp_path / 'mihomo'
        home.mkdir()
        for geo in ('GeoSite.dat', 'geoip.metadb'):
            if (GEO_CACHE / geo).exists():
                shutil.copy(GEO_CACHE / geo, home / geo)
        (home / 'NekoVPN-emergency.yaml').write_bytes(kit['NekoVPN-emergency.yaml'])
        proc = subprocess.run(
            ['docker', 'run', '--rm', '-v', f'{home}:/root/.config/mihomo', MIHOMO_IMAGE,
             '-t', '-d', '/root/.config/mihomo', '-f',
             '/root/.config/mihomo/NekoVPN-emergency.yaml'],
            capture_output=True, text=True, timeout=300)
        out = proc.stdout + proc.stderr
        if "can't download" in out:
            pytest.skip('mihomo geodata unavailable (offline)')
        assert 'test is successful' in out, out[-2000:]
        GEO_CACHE.mkdir(parents=True, exist_ok=True)
        for geo in ('GeoSite.dat', 'geoip.metadb'):
            if (home / geo).exists() and not (GEO_CACHE / geo).exists():
                part = GEO_CACHE / f'{geo}.{os.getpid()}.part'
                shutil.copy(home / geo, part)
                os.replace(part, GEO_CACHE / geo)     # readers never see half

    @pytest.mark.requires_docker
    def test_json_passes_sing_box_check(self, kit, tmp_path):
        """sing-box 1.11 predates ``tls.fragment`` (the clients' newer cores
        take it), so the check runs on the profile minus that one field."""
        if not _have_image(SINGBOX_IMAGE):
            pytest.skip(f'{SINGBOX_IMAGE} not available')
        cfg = json.loads(kit['NekoVPN-emergency.json'])

        def strip(o):
            if isinstance(o, dict):
                o.pop('fragment', None)
                for v in o.values():
                    strip(v)
            elif isinstance(o, list):
                for v in o:
                    strip(v)
        strip(cfg)
        (tmp_path / 'c.json').write_text(json.dumps(cfg))
        proc = subprocess.run(
            ['docker', 'run', '--rm', '--network', 'none', '-v', f'{tmp_path}:/c:ro',
             SINGBOX_IMAGE, 'check', '-c', '/c/c.json'],
            capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]

    @pytest.mark.requires_docker
    def test_kit_providers_load_in_mihomo(self, bot, db, tmp_path):
        """The kit's proxy-providers (§32: emergency on the main domain,
        a mirror) served by the bot's own endpoints on 127.0.0.1 — mihomo
        accepts the file, fetches both DIRECT, and Cascade drops the
        provider copies of what DPIMonitor demotes for the user's ASN."""
        if not (_have_image(MIHOMO_IMAGE) and (GEO_CACHE / 'GeoSite.dat').exists()):
            pytest.skip('mihomo image or cached geodata not available')
        import socket
        import threading
        import urllib.request
        from aiohttp import web

        def free_port():
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                return s.getsockname()[1]

        main, mirror, ctl = free_port(), free_port(), free_port()
        config = make_config(WEBAPP_URL=f'http://127.0.0.1:{main}',
                             SUB_MIRROR_URLS=f'http://127.0.0.1:{mirror}')
        user = add_user(db)
        db.set_setting('cascade_auto', json.dumps(
            {'asn': {'AS31133': {'reality': {'reason': 'reality_asn'}}}}))
        loops = []
        for port in (main, mirror):
            srv = WebAppServer(config, db, xui_service=Mock())
            srv.xui.get_client_traffic = AsyncMock(return_value={})
            loop = asyncio.new_event_loop()
            runner = web.AppRunner(srv.app)
            loop.run_until_complete(runner.setup())
            loop.run_until_complete(web.TCPSite(runner, '127.0.0.1', port).start())
            threading.Thread(target=loop.run_forever, daemon=True).start()
            loops.append((loop, runner))
        home = tmp_path / 'mihomo'
        home.mkdir()
        for geo in ('GeoSite.dat', 'geoip.metadb'):
            if (GEO_CACHE / geo).exists():
                shutil.copy(GEO_CACHE / geo, home / geo)
        (home / 'kit.yaml').write_bytes(dict(sos.build_kit(db, config, user))[
            'NekoVPN-emergency.yaml'])
        name = f'sos-kit-test-{ctl}'
        proc = subprocess.Popen(
            ['docker', 'run', '--rm', '--name', name, '--network', 'host',
             '-v', f'{home}:/root/.config/mihomo', MIHOMO_IMAGE,
             '-d', '/root/.config/mihomo', '-f', '/root/.config/mihomo/kit.yaml',
             '-ext-ctl', f'127.0.0.1:{ctl}'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            providers = {}
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                time.sleep(0.5)
                try:
                    providers = json.loads(urllib.request.urlopen(
                        f'http://127.0.0.1:{ctl}/providers/proxies', timeout=2).read()
                    )['providers']
                except Exception:
                    continue
                if all((providers.get(p) or {}).get('proxies')
                       for p in ('emergency', 'mirror-1')):
                    break
            own = [p['name'] for p in json.loads(dict(sos.build_kit(db, config, user))[
                'NekoVPN-emergency.yaml'])['proxies']]
            for prov, label in (('emergency', '[E] '), ('mirror-1', '[M1] ')):
                got = [p['name'] for p in providers[prov]['proxies']]
                assert sorted(got) == sorted(label + n for n in own), prov
            groups = json.loads(urllib.request.urlopen(
                f'http://127.0.0.1:{ctl}/proxies', timeout=2).read())['proxies']
            cascade = groups['Cascade']['all']
            assert cascade and not [n for n in cascade if n.endswith('-reality')]
            assert any(n.startswith('[E] ') for n in cascade)
        finally:
            subprocess.run(['docker', 'stop', '-t', '1', name], capture_output=True)
            proc.wait(timeout=30)
            for loop, runner in loops:
                asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
                loop.call_soon_threadsafe(loop.stop)
            # mihomo wrote its caches as root: hand them back to the cleanup
            subprocess.run(['docker', 'run', '--rm', '--entrypoint', 'sh', '-v',
                            f'{home}:/w', MIHOMO_IMAGE, '-c', 'rm -rf /w/*'],
                           capture_output=True, timeout=60)


# ====================================================================
# /share (E29), buttons, routing
# ====================================================================

class TestShareAndRouting:

    def test_share_text_ru(self, bot, db, config):
        add_user(db)
        from bot.handlers.commands import CommandHandler
        CommandHandler(bot, db, config).handle_share({}, CID)
        text = sent(bot, CID)[0]['text']
        for must in ('Раздать VPN соседям', 'Инструменты → Общие', '«Входящие»',
                     '«Разрешить LAN»', '<code>7890</code>', 'IP-адрес',
                     'Прокси: Вручную', 'Использовать прокси-сервер',
                     'iPhone (только как клиент)', 'Настройка прокси → Вручную'):
            assert must in text, must

    def test_share_text_en_and_for_strangers(self, bot, db, config):
        add_user(db, lang='en')
        from bot.handlers.commands import CommandHandler
        CommandHandler(bot, db, config).handle_share({}, CID)
        assert 'Allow LAN' in sent(bot, CID)[0]['text']
        bot.send_message.reset_mock()
        CommandHandler(bot, db, config).handle_share({}, '424242')   # no user row
        assert 'Разрешить LAN' in sent(bot, '424242')[0]['text']

    def test_commands_are_routed(self, bot, db, config, panel):
        from bot.handlers.commands import CommandHandler
        add_user(db)
        h = CommandHandler(bot, db, config)
        assert h.COMMANDS['/sos'] == 'handle_sos'
        assert h.COMMANDS['/kit'] == 'handle_kit'
        assert h.COMMANDS['/share'] == 'handle_share'
        h.handle({'message': {'text': '/sos', 'chat': {'id': int(CID), 'type': 'private'},
                              'from': {'id': int(CID)}}})
        assert report_rows(db, 'sos')

    def test_help_and_menu_list_them(self, bot, db, config):
        from bot.core.bot import Bot
        from bot.handlers.commands import CommandHandler
        for lang in ('ru', 'en'):
            db._users.save(User(chat_id='77', username='h', status='demo', lang=lang))
            bot.send_message.reset_mock()
            CommandHandler(bot, db, config).handle_help(
                {'message': {'chat': {'id': 77}, 'from': {'id': 77}}}, '77')
            text = sent(bot, '77')[0]['text']
            assert '/sos' in text and '/kit' in text and '/share' in text
        menu = {c['command'] for c in Bot.USER_COMMANDS}
        assert {'sos', 'kit', 'share'} <= menu

    def test_buttons_under_the_sos_answer(self, bot, db, config):
        from bot.handlers.callbacks.dispatcher import CallbackDispatcher
        add_user(db)
        d = CallbackDispatcher(bot, db, config)
        assert d.dispatch({}, CID, CID, 'sos:share')
        assert 'Разрешить LAN' in sent(bot, CID)[0]['text']
        assert d.dispatch({}, CID, CID, 'sos:kit')
        sos_thread = [t for t in __import__('threading').enumerate()
                      if t.name == f'sos-kit-{CID}']
        for t in sos_thread:
            t.join(timeout=10)
        assert set(kit_files(bot)) == {'NekoVPN-emergency.yaml', 'NekoVPN-emergency.json'}

    def test_kit_button_pressed_in_a_group_goes_to_the_presser(self, bot, db, config):
        from bot.handlers.callbacks.sos import SosCallbackHandler
        add_user(db)
        SosCallbackHandler(bot, db, config).handle({}, str(GROUP), CID, data='sos:kit')
        for t in __import__('threading').enumerate():
            if t.name == f'sos-kit-{CID}':
                t.join(timeout=10)
        assert {c.kwargs['chat_id'] for c in bot.send_document.call_args_list} == {CID}


# ====================================================================
# /nudge_sub (A1.2)
# ====================================================================

class TestNudgeSub:

    @pytest.fixture
    def admin(self, bot, db, config):
        from bot.handlers.admin import AdminHandler
        h = AdminHandler(bot, db, config)
        h._current_update = {'message': {'message_thread_id': 99,
                                         'from': {'id': int(ADMIN)},
                                         'chat': {'id': GROUP}}}
        return h

    def _seed_users(self, db):
        add_user(db, '1001', username='n1', uuid='10000000-1d4b-4c5e-9f7a-2b3c4d5e6f70',
                 asn=None, status='demo')
        add_user(db, '1002', username='n2', uuid='20000000-1d4b-4c5e-9f7a-2b3c4d5e6f70',
                 asn='', status='paid', lang='en')
        add_user(db, '1003', username='n3', uuid='30000000-1d4b-4c5e-9f7a-2b3c4d5e6f70',
                 asn='  ', status='support_topic')
        add_user(db, 'ext_abc', username='mailonly', uuid='40000000-1d4b-4c5e-9f7a-2b3c4d5e6f70',
                 asn=None, status='paid')
        # not in the audience:
        add_user(db, '1005', username='has_asn', uuid='50000000-1d4b-4c5e-9f7a-2b3c4d5e6f70')
        add_user(db, '1006', username='banned', uuid='60000000-1d4b-4c5e-9f7a-2b3c4d5e6f70',
                 asn=None, status='banned')
        add_user(db, '1007', username='nokey', uuid=None, asn=None, status='demo')
        add_user(db, '1008', username='new', uuid=None, asn=None, status='new')

    def test_preview_counts_and_sends_nothing(self, admin, bot, db):
        self._seed_users(db)
        assert admin.nudge_sub(str(GROUP), []) is None
        (card,) = sent(bot, GROUP)
        text = card['text']
        assert 'Активных без оператора: <b>4</b>' in text
        assert 'Telegram: 3, только почта: 1' in text
        for name in ('@n1', '@n2', '@n3'):
            assert name in text
        for name in ('has_asn', 'banned', 'nokey', 'mailonly'):
            assert name not in text
        assert '<code>/nudge_sub go</code>' in text
        assert card['message_thread_id'] == 99
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM admin_actions").fetchone()[0] == 0

    def test_sample_is_capped_at_ten(self, admin, bot, db):
        for i in range(14):
            add_user(db, str(2000 + i), username=f'u{i:02d}',
                     uuid=f'{i:08d}-aaaa-4c5e-9f7a-2b3c4d5e6f70', asn=None, status='demo')
        admin.nudge_sub(str(GROUP), [])
        text = sent(bot, GROUP)[0]['text']
        assert text.count('@u') == 10

    def test_go_sends_logs_and_reports(self, admin, bot, db, monkeypatch):
        self._seed_users(db)
        sleeps = []
        monkeypatch.setattr('bot.handlers.admin.broadcast.time.sleep', sleeps.append)
        t = admin.nudge_sub(str(GROUP), ['go'])
        t.join(timeout=10)
        assert not t.is_alive()
        got = {kw['chat_id']: kw['text'] for kw in
               (c.kwargs for c in bot.send_message.call_args_list)
               if kw['chat_id'] in ('1001', '1002', '1003', 'ext_abc')}
        assert set(got) == {'1001', '1002', '1003'}
        assert got['1001'].startswith('🔄 <b>Обнови, пожалуйста, подписку</b>')
        assert got['1002'].startswith('🔄 <b>Please refresh your subscription</b>')
        assert sleeps == [0.05] * 3
        with db._connect() as conn:
            row = tuple(conn.execute("SELECT admin_id, action, target_id, details "
                                     "FROM admin_actions").fetchone())
        assert row == (ADMIN, 'nudge_sub', '3/3', 'sent=3 failed=0')
        assert sent(bot, GROUP)[-1]['text'] == '✅ /nudge_sub: доставлено 3, ошибок 0.'
        # the preview now shows the last run
        bot.send_message.reset_mock()
        admin.nudge_sub(str(GROUP), [])
        assert 'Последняя рассылка:' in sent(bot, GROUP)[0]['text']

    def test_go_counts_failures(self, admin, bot, db, monkeypatch):
        self._seed_users(db)
        monkeypatch.setattr('bot.handlers.admin.broadcast.time.sleep', lambda s: None)
        bot.send_message.side_effect = lambda **kw: (
            None if kw.get('chat_id') == '1002' else {'message_id': 1})
        t = admin.nudge_sub(str(GROUP), ['go'])
        t.join(timeout=10)
        with db._connect() as conn:
            row = tuple(conn.execute("SELECT target_id, details FROM admin_actions").fetchone())
        assert row == ('2/3', 'sent=2 failed=1')

    def test_go_with_nobody_to_nudge(self, admin, bot, db):
        add_user(db)                                # has last_asn
        assert admin.nudge_sub(str(GROUP), ['go']) is None
        assert 'некому слать' in sent(bot, GROUP)[0]['text']

    def test_routed_and_in_help(self):
        import re
        from bot.handlers.admin.base import ADMIN_HELP_TEXT, AdminHandlerBase
        assert AdminHandlerBase.ADMIN_COMMANDS['/nudge_sub'] == 'nudge_sub'
        assert '/nudge_sub' in set(re.findall(r'<code>(/[a-z_0-9]+)', ADMIN_HELP_TEXT))
