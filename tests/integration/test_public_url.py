"""SUB_PUBLIC_URL: what users fetch lives apart from the admins' dashboard.

On 2026-11-18 exit's IPv4 was blocked from Russia. The one address the bot
knew, WEBAPP_URL, pointed at exit's Caddy, so Russian users could no longer
import or refresh a subscription, and new installs could not fetch the
profile's rule sets. SUB_PUBLIC_URL is the address users fetch from
(subscriptions, rule sets, the Clash providers and the app's telemetry,
/open, /app, /apk). The dashboard (Mini App, /admin) stays on WEBAPP_URL.
Empty, SUB_PUBLIC_URL is WEBAPP_URL: the profiles do not change by a byte.

Level 2: the /sub handler and the commands on a REAL sqlite with the real
builders (AGENTS.md §60).
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import hop_tls, neko_client, sos
from bot.services.subscription import SubscriptionService
from bot.utils.public_url import public_base_url

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '5d1c0b9a-8e7f-4a6b-9c5d-4e3f2a1b0c9d'
DASH = 'https://dash.example.com:9443'
PUB = 'https://pub.example.ru:8443'


def _config(**over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL=DASH,
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443, EXIT_NODE_IP='203.0.113.30',
        REALITY_PUBLIC_KEY='reality-pbk', SNI_VALUE='www.bing.com',
        SID_VALUE='0123456789abcdef',
        HY2_HOST='203.0.113.20', HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='443,20000:40000', HY2T_PORT='',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api/v1/forecast',
        WS_SNI='cdn.example.com',
        STLS_HOST='203.0.113.20', STLS_PORT=443, STLS_SNI='www.microsoft.com',
        STLS_VERSION=3, STLS_PASSWORD='stls-pw',
        SS_METHOD='2022-blake3-aes-128-gcm', SS_SERVER_PASSWORD='srv-pw',
        SS_USER_SALT='salt', SUB_MIRROR_URLS='https://mirror.example.net:2096',
        FALLBACK_NODE_HOST='198.51.100.9', FALLBACK_NODE_PBK='de-pbk',
        FALLBACK_NODE_SID='de01', FALLBACK_NODE_SNI='www.google.com',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def _server(tmp_path, config):
    db = Database(str(tmp_path / 'bot.db'))
    db._users.save(User(chat_id='1', username='u1', status='paid', uuid=UUID,
                        email='u1@x', quota_gb=100.0, lang='ru', platform='android'))
    db.set_setting('cascade_protocol_order', json.dumps(['reality', 'hy2', 'ws', 'stls']))
    srv = WebAppServer(config, db, xui_service=Mock())
    srv.xui.get_client_traffic = AsyncMock(return_value={'upload': 1, 'download': 2})
    return srv, db


async def _profile(srv, fmt=None):
    req = Mock()
    req.match_info = {'token': srv.subscription.derive_token(UUID)}
    req.rel_url = SimpleNamespace(query={'format': fmt} if fmt else {})
    req.headers = {'User-Agent': 'FlClash/v0.8.90' if fmt else 'Hiddify/2.5.7',
                   'X-Forwarded-For': '203.0.113.30'}
    req.remote = ''
    resp = await srv.handle_subscription(req)
    assert resp.status == 200
    return resp.text


def _urls(text):
    """Every http(s) string anywhere in a JSON profile."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and node.startswith(('http://', 'https://')):
            found.append(node)
    walk(json.loads(text))
    return found


class TestPublicBaseUrl:

    @pytest.mark.parametrize('public, webapp, expected', [
        (PUB, DASH, PUB),
        (PUB + '/', DASH, PUB),
        (f' {PUB} ', DASH, PUB),
        ('', DASH, DASH),
        ('   ', DASH, DASH),
        ('', DASH + '/', DASH),
        ('', '', ''),
    ])
    def test_the_public_address_else_the_dashboards(self, public, webapp, expected):
        assert public_base_url(SimpleNamespace(SUB_PUBLIC_URL=public, WEBAPP_URL=webapp)) == expected

    def test_a_config_without_the_setting_is_the_dashboards(self):
        assert public_base_url(SimpleNamespace(WEBAPP_URL=DASH)) == DASH
        assert public_base_url(SimpleNamespace()) == ''

    def test_values_that_are_not_strings_count_as_unset(self):
        assert public_base_url(SimpleNamespace(SUB_PUBLIC_URL=Mock(), WEBAPP_URL=DASH)) == DASH
        assert public_base_url(Mock()) == ''


class TestUsersGetThePublicAddress:

    @pytest.mark.asyncio
    async def test_the_clash_profile_points_only_at_the_public_address(self, tmp_path):
        srv, _db = _server(tmp_path, _config(SUB_PUBLIC_URL=PUB))
        text = await _profile(srv, 'clash')
        cfg = json.loads(text)
        ours = [u for u in _urls(text) if 'example.com' in u or 'example.ru' in u]
        assert ours and all(u.startswith(PUB + '/') for u in ours), ours
        assert DASH.split('//')[1] not in text
        assert all(p['url'].startswith(PUB + '/lists/') for p in cfg['rule-providers'].values())
        providers = cfg['proxy-providers']
        assert providers['emergency']['url'].startswith(f'{PUB}/sub/')
        assert providers['emergency']['health-check']['url'].startswith(f'{PUB}/probe/')
        assert any(name.startswith('p-') for name in providers)
        # the mirror stays the mirror
        assert providers['mirror-1']['url'].startswith('https://mirror.example.net:2096/sub/')

    @pytest.mark.asyncio
    async def test_the_sing_box_profile_fetches_its_rule_sets_from_the_public_address(self, tmp_path):
        srv, _db = _server(tmp_path, _config(SUB_PUBLIC_URL=PUB))
        text = await _profile(srv)
        rule_sets = json.loads(text)['route']['rule_set']
        assert rule_sets and all(r['url'].startswith(f'{PUB}/rule-sets/') for r in rule_sets)
        assert DASH.split('//')[1] not in text

    @pytest.mark.asyncio
    async def test_the_testers_big_check_is_on_the_public_address(self, tmp_path):
        """E5: the testers' Cascade / Auto / Calls check /probe-big."""
        srv, db = _server(tmp_path, _config(SUB_PUBLIC_URL=PUB))
        db.set_setting(WebAppServer.PROBE_BIG_SETTING, '1')
        db.set_setting(neko_client.TESTERS_KEY, json.dumps(['1']))
        groups = json.loads(await _profile(srv, 'clash'))['proxy-groups']
        checks = {g['name']: g.get('url') for g in groups if g.get('url')}
        assert checks and all(url == f'{PUB}/probe-big' for url in checks.values()), checks

    def test_links_the_bot_hands_out(self):
        config = _config(SUB_PUBLIC_URL=PUB)
        user = SimpleNamespace(uuid=UUID, email='u1@x', status='paid', lang='ru', chat_id='1')
        token = SubscriptionService(config).derive_token(UUID)
        assert SubscriptionService(config).build_subscription_url(user) == f'{PUB}/sub/{token}'
        assert neko_client.open_page_url(config, user) == f'{PUB}/open/{token}'
        assert neko_client.download_page_url(config) == f'{PUB}/app'
        assert neko_client.download_page_url(config, user) == f'{PUB}/app/{token}'
        assert sos.emergency_urls(config, user) == (
            f'{PUB}/sub/{token}?emergency=1', f'{PUB}/sub/{token}?format=clash&emergency=1')

    @pytest.mark.asyncio
    async def test_the_app_page_links_the_apk_on_the_public_address(self, tmp_path, monkeypatch):
        tag = 'v0.9.2-neko.3'
        release = neko_client.pick_release([{
            'tag_name': tag, 'draft': False, 'prerelease': True,
            'html_url': f'https://github.example/releases/{tag}',
            'assets': [{'name': f'NekoVPN-0.9.2-neko.3-android-{abi}.apk', 'size': 59768832,
                        'browser_download_url': f'https://github.example/{tag}/{abi}.apk'}
                       for abi in neko_client.ABIS]}])
        monkeypatch.setattr(neko_client.ReleaseCache, 'get', classmethod(lambda cls: release))
        srv, _db = _server(tmp_path, _config(SUB_PUBLIC_URL=PUB))
        req = Mock()
        req.match_info = {}
        req.headers = {'User-Agent': 'Mozilla/5.0 (Linux; Android 14)'}
        body = (await srv.handle_app(req)).text
        assert f'{PUB}/apk' in body and DASH not in body

    def test_the_sub_command_shows_the_public_link(self, tmp_path):
        from bot.handlers.commands import CommandHandler
        db = Database(str(tmp_path / 'bot.db'))
        db._users.save(User(chat_id='1', username='u1', status='demo', uuid=UUID,
                            email='u1@x', lang='ru', platform='windows'))
        bot = Mock()
        CommandHandler(bot, db, _config(SUB_PUBLIC_URL=PUB)).handle_sub({}, '1')
        text = bot.send_message.call_args.kwargs['text']
        assert f'{PUB}/sub/' in text and 'dash.example.com' not in text


class TestTheDashboardStaysOnWebappUrl:

    def test_admin_button(self, tmp_path):
        from bot.handlers.commands import CommandHandler
        db = Database(str(tmp_path / 'bot.db'))
        config = _config(SUB_PUBLIC_URL=PUB, SUPER_ADMIN_ID='1', ADMIN_IDS=['1'])
        config.is_admin = lambda uid: str(uid) == '1'
        bot = Mock()
        update = {'message': {'chat': {'id': 1, 'type': 'private'},
                              'from': {'id': 1, 'username': 'u1'}, 'text': '/admin'}}
        CommandHandler(bot, db, config).handle_admin(update, '1')
        button = bot.send_message.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]
        url = button.get('web_app', {}).get('url') or button.get('url')
        assert url.startswith(DASH) and 'example.ru' not in url


class TestTheListsOverview:

    def test_it_names_the_address_users_fetch_the_lists_from(self, tmp_path):
        from bot.handlers.admin import AdminHandler
        config = _config(SUB_PUBLIC_URL=PUB, SUPER_ADMIN_ID='1', FORUM_ENABLED=True,
                         FORUM_GROUP_ID='-100', TOPIC_SUPPORT=17)
        config.is_admin = lambda uid: str(uid) == '1'
        text = AdminHandler(Mock(), Database(str(tmp_path / 'bot.db')), config)._lists_overview()
        assert f'раздаются: {PUB}/lists/' in text and 'dash.example.com' not in text


class TestUnsetChangesNothing:
    """Without SUB_PUBLIC_URL (absent, empty, or equal to WEBAPP_URL) every
    profile is byte for byte what it was."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize('fmt', [None, 'clash'])
    @pytest.mark.parametrize('over', [{'SUB_PUBLIC_URL': ''}, {'SUB_PUBLIC_URL': '  '},
                                      {'SUB_PUBLIC_URL': DASH}, {'SUB_PUBLIC_URL': DASH + '/'}],
                             ids=['empty', 'blank', 'same', 'same-slash'])
    async def test_profiles_are_byte_identical(self, tmp_path, fmt, over):
        before_srv, _ = _server(tmp_path / 'a', _config())
        after_srv, _ = _server(tmp_path / 'b', _config(**over))
        assert await _profile(after_srv, fmt) == await _profile(before_srv, fmt)


class TestHopVerify:

    def _targets(self, config):
        Database(config.DB_PATH)._users.save(User(
            chat_id=hop_tls.PROBE_CHAT_ID, username='probe', status='paid', uuid=UUID,
            email='probe@x', lang='ru'))
        def fake_get(url, timeout):
            return 200, b'{"version": "x"}'
        result = hop_tls.verify(config, config.DB_PATH, public_get=fake_get)
        return [row['path'] for row in result['paths']]

    def test_the_public_address_is_checked_too(self, tmp_path):
        config = _config(SUB_PUBLIC_URL=PUB, WEB_TLS_PORT=0, DB_PATH=str(tmp_path / 'bot.db'))
        assert self._targets(config) == ['webapp', 'public', 'mirror-1']

    def test_the_same_address_is_checked_once(self, tmp_path):
        config = _config(SUB_PUBLIC_URL=DASH + '/', WEB_TLS_PORT=0, DB_PATH=str(tmp_path / 'bot.db'))
        assert self._targets(config) == ['webapp', 'mirror-1']
