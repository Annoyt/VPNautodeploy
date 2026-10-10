"""RU-zone egress for paid users — bot/services/ru_exit.py.

Level 2 (real sqlite): one rule (``is_eligible``: paid with a paid-until
date that has not passed, or a payer with an open ticket) decides the
button, ``/sub?mode=abroad`` (403 for anyone else, 404 while the egress
is not set up) and the user list ``setup_ru_exit.sh --sync`` feeds the
server — the CLI is run as the timer runs it, from a bare interpreter.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.config.constants import FLCLASH_DOWNLOAD_URL
from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services.ru_exit import (
    RU_ZONE_STATUSES, SETTING_KEY, eligible_server_users, is_eligible,
    parse_ru_exit, ru_exit_for, server_users,
)
from bot.services.subscription import SubscriptionService

# find_user_by_token still walks the legacy facade.
pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

REPO = Path(__file__).resolve().parents[2]
UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
UUID2 = '0f9e8d7c-6b5a-4c3d-8e2f-1a0b9c8d7e6f'
RU_EXIT = {'port': 8445, 'sni': 'www.google.com',
           'pbk': 'test-pbk', 'sid': 'abcdef0123456789'}
NOW = datetime(2026, 10, 10, 12, 0, 0)
FUTURE = '2099-01-01T00:00:00'
PAST = '2020-01-01T00:00:00'


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def _config(**over):
    cfg = Mock()
    cfg.BOT_TOKEN = 'test_token'
    cfg.WEBAPP_URL = 'https://dash.example.com'
    cfg.ENTRY_NODE_IP = '203.0.113.20'
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def _seed(db, chat_id='111', uuid=UUID, status='paid', expiry=FUTURE,
          previous_state=None, lang='ru'):
    db._users.save(User(chat_id=chat_id, username=f'u{chat_id}', status=status,
                        uuid=uuid, email=f'{chat_id}@x', quota_gb=10.0, lang=lang,
                        subscription_expiry=expiry, previous_state=previous_state))
    return db._users.get_by_id(chat_id)


def _ru(**over):
    return parse_ru_exit(json.dumps({**RU_EXIT, **over}))


def _u(status='paid', uuid=UUID, expiry=FUTURE, previous_state=None):
    return SimpleNamespace(status=status, uuid=uuid, subscription_expiry=expiry,
                           previous_state=previous_state)


class TestParse:

    def test_valid(self):
        assert _ru() == {'host': '', 'port': 8445, 'sni': 'www.google.com',
                         'pbk': 'test-pbk', 'sid': 'abcdef0123456789'}

    def test_allowlist_leftover_is_ignored(self):
        # The test install wrote chat_ids; access no longer reads them.
        assert _ru(chat_ids=['111']) == _ru()

    @pytest.mark.parametrize('raw', [
        None, '', '   ', 'not json', '[]', '"x"',
        json.dumps({**RU_EXIT, 'pbk': ''}),
        json.dumps({**RU_EXIT, 'sni': ''}),
        json.dumps({**RU_EXIT, 'port': 0}),
        json.dumps({**RU_EXIT, 'port': 65536}),
        json.dumps({**RU_EXIT, 'port': 'abc'}),
    ])
    def test_unusable_is_none(self, raw):
        assert parse_ru_exit(raw) is None


class TestEligibility:

    @pytest.mark.parametrize('user, ok', [
        (_u(), True),
        (_u(expiry=None), True),                    # a grant without a date
        (_u(expiry=''), True),
        (_u(expiry='not a date'), True),            # the hy2t gate's reading
        (_u(expiry=PAST), False),                   # lapsed payer: still 'paid'
        (_u(expiry='2026-10-10T11:59:59'), False),  # a second ago
        (_u(expiry='2026-10-10T12:00:00'), True),   # this very second
        (_u(expiry='2026-10-10T12:30:00+03:00'), False),   # 09:30 UTC
        (_u(expiry='2026-10-10T14:30:00+02:00'), True),    # 12:30 UTC
        (_u(uuid=None), False),
        (_u(uuid=''), False),
        (_u(status='support_topic'), True),                         # payer, ticket open
        (_u(status='support_topic', expiry=PAST), False),
        (_u(status='support_topic', expiry=None, previous_state='paid'), True),
        (_u(status='support_topic', expiry=None, previous_state='demo'), False),
        (_u(status='support_topic', expiry=None), False),
        (_u(status='demo', expiry=None), False),
        (_u(status='demo'), False),
        (_u(status='banned'), False),
        (_u(status='pending_demo'), False),
        (None, False),
    ])
    def test_rule(self, user, ok):
        assert is_eligible(user, now=NOW) is ok

    def test_default_now_is_utc(self):
        assert is_eligible(_u(expiry=(datetime.now(timezone.utc) + timedelta(minutes=5))
                              .replace(tzinfo=None).isoformat()))
        assert not is_eligible(_u(expiry=(datetime.now(timezone.utc) - timedelta(minutes=5))
                                  .replace(tzinfo=None).isoformat()))

    def test_statuses_are_the_paid_tier(self):
        # Only statuses that hold the paid tier at all can get the RU-zone.
        from bot.handlers.callbacks.user import MyKeyAnswerHandler
        assert set(RU_ZONE_STATUSES) == set(MyKeyAnswerHandler.PAID_USER_STATUSES)

    def test_not_configured(self, db):
        assert ru_exit_for(db, _seed(db)) is None

    def test_paid_user(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        assert ru_exit_for(db, _seed(db))['port'] == 8445

    @pytest.mark.parametrize('over', [
        {'status': 'demo', 'expiry': None},
        {'expiry': PAST},
        {'uuid': None},
        {'status': 'banned'},
    ])
    def test_others(self, db, over):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        assert ru_exit_for(db, _seed(db, **over)) is None

    def test_broken_db_never_raises(self):
        broken = Mock()
        broken.get_setting.side_effect = RuntimeError('boom')
        assert ru_exit_for(broken, _u()) is None

    def test_no_db_read_for_others(self):
        # Every key card asks; only an eligible user costs a settings read.
        db = Mock()
        assert ru_exit_for(db, _u(status='demo', expiry=None)) is None
        db.get_setting.assert_not_called()


class TestServerUsers:

    @staticmethod
    def _row(chat_id='111', uuid=UUID, status='paid', expiry=FUTURE, prev=None):
        return (chat_id, uuid, status, expiry, prev)

    def test_shape(self):
        assert server_users([self._row()], now=NOW) == [
            {'name': 'c111', 'uuid': UUID, 'flow': 'xtls-rprx-vision'}]

    def test_the_rule_filters(self):
        rows = [
            self._row('1'),
            self._row('2', UUID2, expiry=PAST),
            self._row('3', '1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed', 'support_topic', None, 'paid'),
            self._row('4', '6ec0bd7f-11c0-43da-975e-2a8ad9ebae0b', 'support_topic', None, 'demo'),
        ]
        assert [u['name'] for u in server_users(rows, now=NOW)] == ['c1', 'c3']

    def test_sorted_by_name_whatever_the_row_order(self):
        rows = [self._row('9'), self._row('10', UUID2)]
        assert [u['name'] for u in server_users(rows, now=NOW)] == ['c10', 'c9']
        assert server_users(rows, now=NOW) == server_users(rows[::-1], now=NOW)

    def test_one_entry_per_uuid(self):
        rows = [self._row('2'), self._row('1', UUID.upper())]
        assert server_users(rows, now=NOW) == [
            {'name': 'c1', 'uuid': UUID.upper(), 'flow': 'xtls-rprx-vision'}]

    @pytest.mark.parametrize('row', [
        (None, UUID, 'paid', FUTURE, None),
        ('', UUID, 'paid', FUTURE, None),
        ('5', 'not-a-uuid', 'paid', FUTURE, None),
        ('5', UUID + 'x', 'paid', FUTURE, None),
        ('5', None, 'paid', FUTURE, None),
    ])
    def test_unusable_rows_are_skipped(self, row):
        # A malformed uuid would fail `sing-box check` for everyone.
        assert server_users([row, self._row('7', UUID2)], now=NOW) == [
            {'name': 'c7', 'uuid': UUID2, 'flow': 'xtls-rprx-vision'}]

    def test_real_table(self, db):
        _seed(db, '1')
        _seed(db, '2', UUID2, status='demo', expiry=None)
        _seed(db, '3', '1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed', expiry=PAST)
        _seed(db, '4', '6ec0bd7f-11c0-43da-975e-2a8ad9ebae0b', status='support_topic',
              expiry=None, previous_state='paid')
        _seed(db, '5', None)
        with db._connect() as conn:
            assert [u['name'] for u in eligible_server_users(conn)] == ['c1', 'c4']


class TestCli:
    """``python3 -P bot/services/ru_exit.py users`` — what --sync runs in
    the container. A bare interpreter (no PYTHONPATH, -P): a top-level
    ``bot`` import would fail here, and the timer would pay the
    ``bot.services`` package init on every run."""

    def _run(self, *args, db_path):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['DB_PATH'] = str(db_path)
        return subprocess.run(
            [sys.executable, '-P', 'bot/services/ru_exit.py', *args],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=60,
        )

    def test_users(self, db):
        _seed(db, '1')
        _seed(db, '2', UUID2, status='demo', expiry=None)
        r = self._run('users', db_path=db.db_path)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout) == [
            {'name': 'c1', 'uuid': UUID, 'flow': 'xtls-rprx-vision'}]

    def test_usage(self, db):
        r = self._run(db_path=db.db_path)
        assert r.returncode == 2 and 'usage' in r.stderr and r.stdout == ''

    def test_missing_db_fails_loudly(self, tmp_path):
        # Never an empty list for a DB that is not there.
        r = self._run('users', db_path=tmp_path / 'nope.db')
        assert r.returncode != 0 and r.stdout == ''

    def test_read_only(self, db):
        _seed(db, '1')
        before = Path(db.db_path).read_bytes()
        assert self._run('users', db_path=db.db_path).returncode == 0
        assert Path(db.db_path).read_bytes() == before


class TestAbroadProfile:

    def _build(self, **over):
        user = SimpleNamespace(chat_id="111", uuid=UUID)
        return SubscriptionService(_config()).build_abroad_singbox_config(user, _ru(**over))

    def test_ru_zone_outbound(self):
        ob = next(o for o in self._build()['outbounds'] if o['tag'] == 'ru-zone')
        assert ob['server'] == '203.0.113.20'   # ENTRY_NODE_IP by default
        assert ob['server_port'] == 8445
        assert ob['uuid'] == UUID
        assert ob['flow'] == 'xtls-rprx-vision'
        assert ob['tls']['server_name'] == 'www.google.com'
        assert ob['tls']['reality'] == {
            'enabled': True, 'public_key': 'test-pbk', 'short_id': 'abcdef0123456789',
        }
        assert 'fragment' not in ob['tls']

    def test_host_override(self):
        ob = next(o for o in self._build(host='198.51.100.7')['outbounds']
                  if o['tag'] == 'ru-zone')
        assert ob['server'] == '198.51.100.7'

    def test_reverse_routing(self):
        route = self._build()['route']
        assert route['final'] == 'direct'
        rules = route['rules']

        def idx(pred):
            return next(i for i, r in enumerate(rules) if pred(r))
        ru_suffix = idx(lambda r: r.get('domain_suffix') == ['ru', 'su', 'xn--p1ai'])
        ru_sets = idx(lambda r: r.get('rule_set') == ['geoip-ru', 'geosite-category-ru'])
        carve = idx(lambda r: 'geosite-youtube' in (r.get('rule_set') or []))
        tg = idx(lambda r: 'ip_cidr' in r)
        assert rules[ru_suffix]['outbound'] == rules[ru_sets]['outbound'] == 'proxy'
        # YouTube/Google/Telegram must win over the .ru suffix (google.ru)
        # and stay direct — through a RU IP they are what RKN throttles.
        assert rules[carve]['outbound'] == rules[tg]['outbound'] == 'direct'
        assert max(carve, tg) < min(ru_suffix, ru_sets)

    def test_proxy_selector_is_ru_zone_only(self):
        sel = self._build()['outbounds'][0]
        assert sel == {'type': 'selector', 'tag': 'proxy',
                       'outbounds': ['ru-zone'], 'default': 'ru-zone'}

    def test_dns(self):
        dns = self._build()['dns']
        ru = next(s for s in dns['servers'] if s['tag'] == 'ru')
        assert ru['detour'] == 'ru-zone'
        assert dns['final'] == 'local'
        servers = [r.get('server') for r in dns['rules']]
        carve = next(i for i, r in enumerate(dns['rules'])
                     if 'geosite-youtube' in (r.get('rule_set') or []))
        assert servers[carve] == 'local'
        assert carve < servers.index('ru', 2)   # after the clash_mode pair

    def test_no_uuid_is_none(self):
        svc = SubscriptionService(_config())
        assert svc.build_abroad_singbox_config(
            SimpleNamespace(chat_id='111', uuid=None), _ru()) is None


class TestAbroadClash:

    def _build(self, **over):
        user = SimpleNamespace(chat_id="111", uuid=UUID)
        txt = SubscriptionService(_config()).build_abroad_clash_config(user, _ru(**over))
        return json.loads(txt), txt

    def test_is_yaml_too(self):
        yaml = pytest.importorskip('yaml')
        cfg, txt = self._build()
        assert yaml.safe_load(txt) == cfg

    def test_proxy(self):
        (proxy,) = self._build()[0]['proxies']
        assert proxy['server'] == '203.0.113.20'
        assert (proxy['port'], proxy['uuid']) == (8445, UUID)
        assert proxy['type'] == 'vless' and proxy['flow'] == 'xtls-rprx-vision'
        assert proxy['servername'] == 'www.google.com'
        assert proxy['reality-opts'] == {'public-key': 'test-pbk',
                                         'short-id': 'abcdef0123456789'}

    def test_reverse_rules(self):
        cfg = self._build()[0]
        rules = cfg['rules']
        assert rules[-1] == 'MATCH,DIRECT'
        ru = [i for i, r in enumerate(rules) if r.endswith(',RU')]
        assert [rules[i] for i in ru] == [
            'DOMAIN-SUFFIX,ru,RU', 'DOMAIN-SUFFIX,su,RU', 'DOMAIN-SUFFIX,xn--p1ai,RU',
            'GEOSITE,category-ru,RU', 'GEOIP,RU,RU',
        ]
        direct = [i for i, r in enumerate(rules[:-1]) if ',DIRECT' in r]
        assert 'GEOSITE,google,DIRECT' in rules and 'GEOSITE,youtube,DIRECT' in rules
        assert max(direct) < min(ru)        # exceptions win over .ru
        assert 'IP-CIDR6,2001:67c:4e8::/48,DIRECT,no-resolve' in rules
        assert cfg['proxy-groups'] == [
            {'name': 'RU', 'type': 'select', 'proxies': ['RU-zone', 'DIRECT']}]

    def test_no_uuid_is_none(self):
        svc = SubscriptionService(_config())
        assert svc.build_abroad_clash_config(
            SimpleNamespace(chat_id='111', uuid=None), _ru()) is None


class TestSubHandler:

    @pytest.fixture
    def server(self, db):
        srv = WebAppServer(_config(), db, xui_service=Mock())
        srv.xui.get_client_traffic = AsyncMock(return_value={})
        # The normal builders are covered elsewhere; here only WHICH
        # profile is served matters.
        srv.subscription.build_singbox_config = Mock(return_value={'normal': True})
        srv.subscription.build_clash_config = Mock(return_value='{"clash": true}')
        srv.subscription.build_clash_proxies = Mock(return_value='{"proxies": []}')
        return srv

    async def _resp(self, server, query, uuid=UUID):
        token = server.subscription.derive_token(uuid)
        req = Mock()
        req.match_info = {'token': token}
        req.rel_url = SimpleNamespace(query=query)
        req.headers = {'User-Agent': 'HiddifyNext/2.5'}
        req.remote = ''
        return await server.handle_subscription(req)

    async def _get(self, server, query):
        resp = await self._resp(server, query)
        assert resp.status == 200
        return json.loads(resp.text), resp.headers['profile-title']

    @pytest.mark.asyncio
    async def test_paid_user_gets_clash_profile(self, server, db):
        _seed(db)
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        resp = await self._resp(server, {'mode': 'abroad', 'format': 'clash'})
        assert resp.status == 200
        assert resp.content_type == 'text/plain'
        assert json.loads(resp.text)['rules'][-1] == 'MATCH,DIRECT'
        assert 'NekoVPN-RU-zone.yaml' in resp.headers['content-disposition']
        assert resp.headers['profile-title'] == 'NekoVPN RU-zone'

    @pytest.mark.asyncio
    async def test_paid_user_gets_singbox_abroad_profile(self, server, db):
        _seed(db)
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        body, title = await self._get(server, {'mode': 'abroad'})
        assert body['route']['final'] == 'direct'
        assert any(o['tag'] == 'ru-zone' for o in body['outbounds'])
        assert title == 'NekoVPN RU-zone'

    @pytest.mark.asyncio
    @pytest.mark.parametrize('seed', [
        {'status': 'demo', 'expiry': None},
        {'expiry': PAST},
        {'status': 'support_topic', 'expiry': None, 'previous_state': 'demo'},
    ])
    @pytest.mark.parametrize('fmt', [{}, {'format': 'clash'}])
    async def test_others_are_refused(self, server, db, seed, fmt):
        # Never the home profile under the RU-zone URL: a failed refresh
        # leaves FlClash the profile it has.
        _seed(db, **seed)
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        resp = await self._resp(server, {'mode': 'abroad', **fmt})
        assert resp.status == 403
        server.subscription.build_singbox_config.assert_not_called()
        server.subscription.build_clash_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_refusal_records_nothing(self, server, db):
        _seed(db, status='demo', expiry=None)
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        server._record_sub_fetch = Mock()
        server._schedule_fallback_provisioning = Mock()
        resp = await self._resp(server, {'mode': 'abroad', 'format': 'clash'})
        assert resp.status == 403
        server._record_sub_fetch.assert_not_called()
        server._schedule_fallback_provisioning.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_configured(self, server, db):
        _seed(db)
        resp = await self._resp(server, {'mode': 'abroad', 'format': 'clash'})
        assert resp.status == 404
        server.subscription.build_clash_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_address_is_503_not_the_home_profile(self, db):
        srv = WebAppServer(_config(ENTRY_NODE_IP=''), db, xui_service=Mock())
        srv.xui.get_client_traffic = AsyncMock(return_value={})
        srv.subscription.build_clash_config = Mock(return_value='{"clash": true}')
        _seed(db)
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        resp = await self._resp(srv, {'mode': 'abroad', 'format': 'clash'})
        assert resp.status == 503
        srv.subscription.build_clash_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_provider_refresh_ignores_the_mode(self, server, db):
        _seed(db, status='demo', expiry=None)
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        resp = await self._resp(server, {'mode': 'abroad', 'format': 'clash-proxies'})
        assert resp.status == 200 and resp.text == '{"proxies": []}'

    @pytest.mark.asyncio
    @pytest.mark.parametrize('status', ['demo', 'paid'])
    async def test_without_mode_everyone_gets_the_home_profile(self, server, db, status):
        _seed(db, status=status)
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        body, title = await self._get(server, {})
        assert body == {'normal': True} and title == 'NekoVPN'
        resp = await self._resp(server, {'format': 'clash'})
        assert json.loads(resp.text) == {'clash': True}
        assert 'NekoVPN.yaml' in resp.headers['content-disposition']

    @pytest.mark.asyncio
    async def test_other_mode_values_are_the_home_profile(self, server, db):
        _seed(db, status='demo', expiry=None)
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        body, _ = await self._get(server, {'mode': 'home'})
        assert body == {'normal': True}


# ---------- "🇷🇺 RU-зона" button ----------

from bot.handlers.callbacks.dispatcher import CallbackDispatcher   # noqa: E402
from bot.handlers.callbacks.user import build_key_delivery_message  # noqa: E402
from bot.handlers.commands import CommandHandler                    # noqa: E402
from bot.services.notifications import NotificationService          # noqa: E402
from bot.services.ru_exit import RU_ZONE_CALLBACK, ru_zone_button_row  # noqa: E402

RU_ROW = [{'text': '🇷🇺 RU-зона', 'callback_data': 'ru_zone'}]
DEMO = {'chat_id': '222', 'uuid': UUID2, 'status': 'demo', 'expiry': None}
LAPSED = {'chat_id': '333', 'uuid': '1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed',
          'expiry': PAST}


def _rows(keyboard):
    return keyboard['inline_keyboard']


def _has_ru(keyboard):
    return RU_ROW in _rows(keyboard)


class TestRuZoneButtonRow:

    def test_paid(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        assert ru_zone_button_row(db, _seed(db)) == RU_ROW

    def test_english_label(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        assert ru_zone_button_row(db, _seed(db, lang='en'))[0]['text'] == '🇷🇺 RU zone'

    def test_others_and_unconfigured(self, db):
        assert ru_zone_button_row(db, _seed(db)) is None
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        assert ru_zone_button_row(db, _seed(db, **DEMO)) is None
        assert ru_zone_button_row(db, _seed(db, **LAPSED)) is None

    def test_callback_matches_handler(self):
        from bot.handlers.callbacks.user import RuZoneHandler
        assert RuZoneHandler.CALLBACK_DATA == RU_ZONE_CALLBACK


class TestButtonPlacement:

    def test_key_card(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        _, kb = build_key_delivery_message(_seed(db), _config(), db=db)
        rows = _rows(kb)
        assert _has_ru(kb)
        # above the failure-report row, which stays last
        assert rows.index(RU_ROW) == len(rows) - 2
        assert rows[-1][0]['callback_data'] == 'report_failure'

    def test_key_card_without_db_or_for_others(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        _, kb = build_key_delivery_message(_seed(db), _config())
        assert not _has_ru(kb)
        for other in (DEMO, LAPSED):
            _, kb = build_key_delivery_message(_seed(db, **other), _config(), db=db)
            assert not _has_ru(kb)

    def _sub_keyboard(self, db, chat_id):
        bot = Mock()
        CommandHandler(bot, db, _config()).handle_sub({}, chat_id)
        return bot.send_message.call_args.kwargs['reply_markup']

    def test_sub_command(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        _seed(db)
        _seed(db, **DEMO)
        _seed(db, **LAPSED)
        assert _has_ru(self._sub_keyboard(db, '111'))
        assert not _has_ru(self._sub_keyboard(db, '222'))
        assert not _has_ru(self._sub_keyboard(db, '333'))

    def _menu_keyboard(self, db, user):
        bot = Mock()
        NotificationService(bot, db, _config()).notify_main_menu(
            str(user.chat_id), 'ru', user=user)
        return bot.send_message.call_args.kwargs['reply_markup']

    def test_main_menu(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        kb = self._menu_keyboard(db, _seed(db))
        assert _rows(kb)[1] == RU_ROW         # right under Stats / My key
        assert not _has_ru(self._menu_keyboard(db, _seed(db, **DEMO)))


class TestRuZoneHandler:

    def _press(self, db, chat_id='111', user_id=None):
        bot = Mock()
        handled = CallbackDispatcher(bot, db, _config()).dispatch(
            {}, chat_id, user_id or chat_id, RU_ZONE_CALLBACK)
        assert handled
        return bot

    def _text(self, db, chat_id='111'):
        return self._press(db, chat_id=chat_id).send_message.call_args.kwargs['text']

    def test_paid_user_gets_flclash_link(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        _seed(db)
        bot = self._press(db)
        kw = bot.send_message.call_args.kwargs
        token = SubscriptionService(_config()).derive_token(UUID)
        # HTML parse mode: the & must be escaped inside <code>
        assert f'/sub/{token}?mode=abroad&amp;format=clash</code>' in kw['text']
        assert 'iPhone' in kw['text']
        assert kw['parse_mode'] == 'HTML'
        assert kw['reply_markup'] == {'inline_keyboard': [[
            {'text': '⬇️ Скачать FlClash', 'url': FLCLASH_DOWNLOAD_URL}]]}

    def test_english(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        _seed(db, lang='en')
        kw = self._press(db).send_message.call_args.kwargs
        assert 'RU zone' in kw['text'] and 'iPhone' in kw['text']
        assert kw['reply_markup']['inline_keyboard'][0][0]['text'] == '⬇️ Download FlClash'

    @pytest.mark.parametrize('who', [DEMO, LAPSED])
    def test_key_holder_without_paid_is_pointed_at_buy(self, db, who):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        _seed(db, **who)
        text = self._text(db, chat_id=who['chat_id'])
        assert 'платн' in text and '/buy' in text and '/sub/' not in text

    def test_inactive_user_is_refused(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        _seed(db, status='banned')
        text = self._text(db)
        assert 'недоступно' in text and '/sub/' not in text and '/buy' not in text

    def test_unknown_user_is_refused(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        assert 'недоступно' in self._text(db, chat_id='999')

    def test_paid_user_while_not_configured(self, db):
        _seed(db)
        text = self._text(db)
        assert 'недоступно' in text and '/sub/' not in text

    def test_never_outside_own_private_chat(self, db):
        db.set_setting(SETTING_KEY, json.dumps(RU_EXIT))
        _seed(db)
        bot = self._press(db, chat_id='111', user_id='999')
        bot.send_message.assert_not_called()
