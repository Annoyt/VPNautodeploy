"""The demo allowance by client app: 10 GB a month with the NekoVPN app,
5 GB with any other client (bot/services/demo_tiers.py).

Real sqlite (users, client_apps); the panel is faked. Covers the whole
path: the app's profile fetch is recorded and raises a demo key at once,
the monthly reset gives each key its allowance, new keys start on the
smaller one, and every text that shows a quota says that only traffic
through the VPN counts.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot.config.constants import BYTES_PER_GB
from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import demo_tiers
from bot.services.demo_tiers import (
    app_users, demo_traffic_gb, grant_app_allowance, mark_app_seen,
    neko_app_version,
)
# The download page on the bot's own domain (config's WEBAPP_URL).
DOWNLOAD = 'https://dash.example.com/app'

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

GB = BYTES_PER_GB
NEKO_UA = 'NekoVPN/v0.9.2-neko.3 clash-verge Platform/android'
NOTE_RU = 'учитывается ТОЛЬКО трафик вне РФ'
IOS_NOTE_RU = 'Учитывается только трафик через VPN.'
FLCLASH_RU = 'В NekoVPN и FlClash учитывается ТОЛЬКО трафик вне РФ'


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def config(**over):
    cfg = dict(
        DEMO_TRAFFIC_GB=10, DEMO_OTHER_TRAFFIC_GB=5, DEMO_DAYS=30,
        BOT_TOKEN='test_token', WEBAPP_URL='https://dash.example.com',
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='reality-pbk', SNI_VALUE='www.bing.com',
        SID_VALUE='0123456789abcdef', SUB_MIRROR_URLS='',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def seed(db, chat_id, status='demo', quota=5.0, platform='android', lang='ru'):
    db._users.save(User(
        chat_id=str(chat_id), username=f'u{chat_id}', status=status,
        uuid=f'0f9e8d7c-6b5a-4f3e-8d2c-{int(chat_id):012d}',
        email=f'user_{chat_id}@x', quota_gb=quota, platform=platform, lang=lang))
    return db.get_user(str(chat_id))


class FakePanel:
    def __init__(self, ok=True):
        self.ok = ok
        self.updates = []

    def sync_client_settings_sync(self, email, updates):
        self.updates.append((email, dict(updates)))
        return self.ok


class TestRecognisingTheApp:

    @pytest.mark.parametrize('ua, version', [
        (NEKO_UA, '0.9.2-neko.3'),
        ('NekoVPN/v0.8.99-neko.2 clash-verge Platform/android', '0.8.99-neko.2'),
        ('NekoVPN/1.0.0', '1.0.0'),
    ])
    def test_the_app(self, ua, version):
        assert neko_app_version(ua) == version

    @pytest.mark.parametrize('ua', [
        None, '', 'NekoVPN-selftest', 'NekoVPN-1.0.0', 'NekoVPN 0.9.2',
        'FlClash/v0.8.80 clash-verge Platform/android',
        'HiddifyNext/2.5.7 (android) like ClashMeta v2ray sing-box',
        'Mozilla/5.0 NekoVPN/v0.9.2-neko.3',
    ])
    def test_anything_else(self, ua):
        assert neko_app_version(ua) is None


class TestAllowance:

    def test_the_two_allowances(self):
        assert demo_traffic_gb(config(), in_app=True) == 10
        assert demo_traffic_gb(config(), in_app=False) == 5

    def test_unset_or_broken_values_fall_back(self):
        assert demo_traffic_gb(SimpleNamespace(), in_app=True) == 10
        assert demo_traffic_gb(SimpleNamespace(), in_app=False) == 5
        assert demo_traffic_gb(Mock(), in_app=False) == 5
        assert demo_traffic_gb(config(DEMO_OTHER_TRAFFIC_GB='x'), in_app=False) == 5
        assert demo_traffic_gb(config(DEMO_OTHER_TRAFFIC_GB='7'), in_app=False) == 7


class TestAppRecord:

    def test_seen_within_the_week(self, db):
        now = _now()
        mark_app_seen(db, '1', '0.9.2-neko.3', now=now - timedelta(days=6, hours=23))
        mark_app_seen(db, '2', '0.9.2-neko.3', now=now - timedelta(days=7, hours=1))
        with db._connect() as conn:
            assert app_users(conn, now=now) == {'1'}

    def test_a_new_fetch_refreshes_the_row(self, db):
        now = _now()
        mark_app_seen(db, '1', '0.9.2-neko.1', now=now - timedelta(days=30))
        mark_app_seen(db, '1', '0.9.2-neko.3', now=now)
        with db._connect() as conn:
            assert app_users(conn, now=now) == {'1'}
            rows = conn.execute("SELECT version FROM client_apps").fetchall()
        assert [r[0] for r in rows] == ['0.9.2-neko.3']


class TestRaise:

    def test_a_demo_key_gets_the_app_allowance(self, db):
        seed(db, 1, quota=5.0)
        panel = FakePanel()
        assert grant_app_allowance(db, config(), panel, '1') is True
        assert panel.updates == [('user_1@x', {'totalGB': 10 * GB, 'enable': True})]
        assert db.get_user('1').quota_gb == 10.0

    @pytest.mark.parametrize('status, quota', [
        ('demo', 10.0),    # already there
        ('demo', 105.0),   # an admin's gift stays
        ('paid', 100.0),
        ('support_topic', 5.0),
    ])
    def test_nothing_to_do(self, db, status, quota):
        seed(db, 1, status=status, quota=quota)
        panel = FakePanel()
        assert grant_app_allowance(db, config(), panel, '1') is False
        assert panel.updates == []
        assert db.get_user('1').quota_gb == quota

    def test_a_refused_update_keeps_the_quota_for_the_next_try(self, db):
        seed(db, 1, quota=5.0)
        assert grant_app_allowance(db, config(), FakePanel(ok=False), '1') is False
        assert db.get_user('1').quota_gb == 5.0

    def test_reads_the_user_fresh(self, db):
        # The /sub request saw a demo user; they paid before the task ran.
        seed(db, 1, quota=5.0)
        user = db.get_user('1')
        user.status, user.quota_gb = 'paid', 100.0
        db.save_user(user)
        panel = FakePanel()
        assert grant_app_allowance(db, config(), panel, '1') is False
        assert panel.updates == []


def _server(db, panel):
    srv = WebAppServer(config(), db, xui_service=panel)
    panel.get_client_traffic = AsyncMock(return_value={'upload': 1, 'download': 2})
    return srv


async def _sub(srv, uuid, ua, query=None):
    req = Mock()
    req.match_info = {'token': srv.subscription.derive_token(uuid)}
    req.rel_url = SimpleNamespace(query=query or {})
    req.headers = {'User-Agent': ua, 'X-Forwarded-For': '198.51.100.77'}
    req.remote = ''
    return await srv.handle_subscription(req)


async def _flush(srv):
    while True:
        pending = [t for t in srv._bg_tasks if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending)


def _seen(db):
    with db._connect() as conn:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT chat_id, version FROM client_apps")}


class TestProfileFetch:

    @pytest.mark.asyncio
    async def test_the_apps_fetch_raises_a_demo_key(self, db):
        user = seed(db, 1, quota=5.0)
        panel = FakePanel()
        srv = _server(db, panel)
        resp = await _sub(srv, user.uuid, NEKO_UA, {'format': 'clash'})
        assert resp.status == 200
        await _flush(srv)
        assert _seen(db) == {'1': '0.9.2-neko.3'}
        assert panel.updates == [('user_1@x', {'totalGB': 10 * GB, 'enable': True})]
        assert db.get_user('1').quota_gb == 10.0

    @pytest.mark.asyncio
    async def test_another_client_changes_nothing(self, db):
        user = seed(db, 1, quota=5.0)
        panel = FakePanel()
        srv = _server(db, panel)
        await _sub(srv, user.uuid, 'HiddifyNext/2.5.7 (android)')
        await _flush(srv)
        assert _seen(db) == {} and panel.updates == []

    @pytest.mark.asyncio
    async def test_a_provider_refresh_is_not_a_profile_fetch(self, db):
        user = seed(db, 1, quota=5.0)
        panel = FakePanel()
        srv = _server(db, panel)
        await _sub(srv, user.uuid, NEKO_UA, {'format': 'clash-proxies'})
        await _flush(srv)
        assert _seen(db) == {} and panel.updates == []

    @pytest.mark.asyncio
    async def test_a_paid_key_is_recorded_but_untouched(self, db):
        user = seed(db, 1, status='paid', quota=100.0)
        panel = FakePanel()
        srv = _server(db, panel)
        await _sub(srv, user.uuid, NEKO_UA, {'format': 'clash'})
        await _flush(srv)
        assert _seen(db) == {'1': '0.9.2-neko.3'}
        assert [u for u in panel.updates if 'totalGB' in u[1]] == []

    @pytest.mark.asyncio
    async def test_one_call_per_key_in_flight(self, db):
        user = seed(db, 1, quota=5.0)
        srv = _server(db, FakePanel())
        srv._neko_inflight.add(user.uuid)
        await _sub(srv, user.uuid, NEKO_UA, {'format': 'clash'})
        await _flush(srv)
        assert _seen(db) == {}


class TestMonthlyReset:

    def run_reset(self, db, panel_renew):
        from bot.services.notifications import NotificationService
        cfg = config(DB_PATH=db.db_path)
        svc = NotificationService(Mock(), db, cfg)
        xui = Mock()
        xui.api = Mock()
        xui.renew_client_sync.side_effect = panel_renew
        with patch('bot.services.xui_service.XUIService', return_value=xui), \
                patch('time.sleep'):
            svc._reset_demo_quota_sync()
        return svc

    def test_each_key_gets_its_allowance(self, db):
        seed(db, 1, quota=10.0)      # the app this week
        seed(db, 2, quota=10.0)      # the app, but 8 days ago
        seed(db, 3, quota=10.0)      # never the app
        mark_app_seen(db, '1', '0.9.2-neko.3')
        mark_app_seen(db, '2', '0.9.2-neko.2', now=_now() - timedelta(days=8))
        renewed = {}

        def renew(email, expiry_ms, total_bytes=None):
            renewed[email] = total_bytes
            return True

        svc = self.run_reset(db, renew)
        assert renewed == {'user_1@x': 10 * GB, 'user_2@x': 5 * GB, 'user_3@x': 5 * GB}
        assert [db.get_user(c).quota_gb for c in '123'] == [10.0, 5.0, 5.0]
        texts = {c.kwargs['chat_id']: c.kwargs['text']
                 for c in svc.bot.send_message.call_args_list}
        assert 'трафик 10 ГБ' in texts['1'] and DOWNLOAD not in texts['1']
        assert 'трафик 5 ГБ' in texts['3'] and DOWNLOAD in texts['3']
        assert all(NOTE_RU in t for t in texts.values())

    def test_a_failed_renew_keeps_the_quota(self, db):
        seed(db, 1, quota=10.0)
        svc = self.run_reset(db, lambda *a, **k: False)
        assert db.get_user('1').quota_gb == 10.0
        svc.bot.send_message.assert_not_called()


class TestQuotaWarnings:

    def warn(self, total_gb, used_gb, status):
        from bot.services.notifications import NotificationService
        svc = NotificationService(Mock(), Mock(), config())
        svc.db.was_notified = Mock(return_value=False)
        svc.db.mark_notified = Mock()
        svc._send_quota_threshold_alerts(
            {'u@x': {'upload': 0, 'download': used_gb * GB, 'total': total_gb * GB}},
            {'u@x': ('42', status)})
        return svc.bot.send_message.call_args.kwargs['text']

    def test_a_demo_key_under_the_app_allowance_hears_of_the_app(self):
        for used in (4, 5):   # 80 % and 100 %
            text = self.warn(5, used, 'demo')
            assert DOWNLOAD in text and NOTE_RU in text

    def test_the_rule_alone_for_the_rest(self):
        for total, used, status in ((10, 9, 'demo'), (100, 100, 'paid')):
            text = self.warn(total, used, status)
            assert DOWNLOAD not in text and NOTE_RU in text


class TestTexts:

    def test_the_demo_key_card(self, db):
        from bot.handlers.callbacks.user import build_key_delivery_message
        text, _kb = build_key_delivery_message(seed(db, 1), config())
        assert 'Демо: 5 ГБ в месяц, с приложением NekoVPN — 10 ГБ' in text
        assert DOWNLOAD in text and FLCLASH_RU in text
        assert 'подписка в нём та же, что для FlClash' in text

    def test_the_iphone_card_names_no_flclash(self, db):
        from bot.handlers.callbacks.user import build_key_delivery_message
        text, _kb = build_key_delivery_message(seed(db, 1, platform='ios'), config())
        assert IOS_NOTE_RU in text and 'FlClash' not in text

    def test_a_paid_card_has_no_demo_terms(self, db):
        from bot.handlers.callbacks.user import build_key_delivery_message
        text, _kb = build_key_delivery_message(seed(db, 1, status='paid'), config())
        assert 'Демо' not in text

    def test_the_sub_command(self, db):
        from bot.handlers.commands import CommandHandler
        seed(db, 1)
        bot = Mock()
        CommandHandler(bot, db, config()).handle_mykey({}, '1')
        text = bot.send_message.call_args.kwargs['text']
        assert 'Демо: 5 ГБ' in text and NOTE_RU in text

    def test_stats_footer(self, db):
        demo = seed(db, 1)
        assert demo_tiers.stats_footer(demo, config(), 5 * GB) == (
            f"{demo_tiers.app_hint(config(), 'ru')}\n{demo_tiers.vpn_only_note('ru')}")
        paid = seed(db, 2, status='paid', lang='en', platform='ios')
        for total in (100 * GB, 5 * GB):   # a small paid quota is no demo
            assert demo_tiers.stats_footer(paid, config(), total) == (
                'Only traffic through the VPN counts.')


class TestNewKeys:

    def test_a_new_key_starts_on_the_smaller_allowance(self, db):
        from bot.handlers.callbacks.user import GetKeyHandler
        GetKeyHandler._inflight_chat_ids.clear()
        db._users.save(User(chat_id='9', username='u9', status='demo'))
        xui = Mock()
        xui.sync_user = AsyncMock(return_value=True)
        bot = Mock()
        bot.services = {'xui': xui}
        handler = GetKeyHandler(bot, db, config(DEMO_OTHER_TRAFFIC_GB=3))
        with patch.object(handler, '_send_key_to_user', new_callable=AsyncMock):
            asyncio.run(handler._process_key_request('9'))
        client = xui.sync_user.call_args.args[1]
        assert client['totalGB'] == 3 * GB
        assert db.get_user('9').quota_gb == 3.0
