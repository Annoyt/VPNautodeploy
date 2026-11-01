"""The NekoVPN download page: ``GET /app``, ``GET /app/<token>`` and the
stable APK link ``GET /apk`` (bot/services/neko_client.py, plan E35).

Real sqlite for the users; GitHub's release list is faked. The page gives
Android the newest APK and, with the /sub token, the button that adds the
subscription after the install; iPhones and computers get Karing and
FlClash instead.
"""

import re
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import demo_tiers
from bot.services import neko_client as nc
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
BASE = 'https://dash.example.com'
ANDROID = 'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/130 Mobile'
IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15'
WINDOWS = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130'


def _asset(tag, abi, size=59768832):
    version = tag.lstrip('v')
    return {'name': f'NekoVPN-{version}-android-{abi}.apk', 'size': size,
            'browser_download_url': f'https://github.example/{tag}/{abi}.apk'}


def _release(tag, *, draft=False, abis=nc.ABIS):
    return {'tag_name': tag, 'draft': draft, 'prerelease': True,
            'html_url': f'https://github.example/releases/{tag}',
            'assets': [_asset(tag, abi) for abi in abis]
            + [{'name': 'SHA256SUMS', 'browser_download_url': 'x'}]}


RELEASES = [
    _release('v0.9.2-neko.4', draft=True),
    _release('v1.0.0'),                                  # not ours
    _release('v0.9.2-neko.3'),
    _release('v0.9.2-neko.2'),
]


@pytest.fixture(autouse=True)
def _fresh_cache():
    nc.ReleaseCache.clear()
    yield
    nc.ReleaseCache.clear()


def _config(**over):
    cfg = SimpleNamespace(BOT_TOKEN='test_token', WEBAPP_URL=BASE,
                          ENTRY_NODE_IP='203.0.113.20', DEMO_TRAFFIC_GB=10,
                          DEMO_OTHER_TRAFFIC_GB=5)
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def _seed(db, status='demo', lang='ru'):
    db._users.save(User(chat_id='111', username='u111', status=status, uuid=UUID,
                        email='111@x', platform='android', lang=lang))


def _token():
    return SubscriptionService(_config()).derive_token(UUID)


class TestPickRelease:

    def test_the_newest_published_neko_release(self):
        release = nc.pick_release(RELEASES)
        assert release['tag'] == 'v0.9.2-neko.3'
        assert release['page'] == 'https://github.example/releases/v0.9.2-neko.3'
        assert release['apks']['arm64-v8a'] == (
            'https://github.example/v0.9.2-neko.3/arm64-v8a.apk', 59768832)
        assert set(release['apks']) == set(nc.ABIS)

    def test_a_release_without_the_common_apk_is_skipped(self):
        releases = [_release('v0.9.2-neko.5', abis=('x86_64',)), _release('v0.9.2-neko.3')]
        assert nc.pick_release(releases)['tag'] == 'v0.9.2-neko.3'

    @pytest.mark.parametrize('releases', [None, [], [_release('v0.9.2-neko.4', draft=True)],
                                          ['junk', {'tag_name': None}]])
    def test_nothing_to_offer(self, releases):
        assert nc.pick_release(releases) is None


class TestReleaseCache:

    def test_asks_github_once_per_ttl(self):
        calls = []

        def fetch():
            calls.append(1)
            return RELEASES

        assert nc.ReleaseCache.get(fetch, now=1000.0)['tag'] == 'v0.9.2-neko.3'
        assert nc.ReleaseCache.get(fetch, now=1000.0 + nc.ReleaseCache.TTL - 1)
        assert len(calls) == 1
        nc.ReleaseCache.get(fetch, now=1000.0 + nc.ReleaseCache.TTL)
        assert len(calls) == 2

    def test_a_failure_is_remembered_briefly(self):
        calls = []

        def broken():
            calls.append(1)
            raise ConnectionError('github down')

        assert nc.ReleaseCache.get(broken, now=1000.0) is None
        assert nc.ReleaseCache.get(broken, now=1000.0 + nc.ReleaseCache.FAILED_TTL - 1) is None
        assert len(calls) == 1
        assert nc.ReleaseCache.get(lambda: RELEASES,
                                   now=1000.0 + nc.ReleaseCache.FAILED_TTL)['tag'] == 'v0.9.2-neko.3'


class TestLinks:

    def test_apk_redirect(self):
        release = nc.pick_release(RELEASES)
        assert nc.apk_redirect(release, None).endswith('/arm64-v8a.apk')
        assert nc.apk_redirect(release, 'x86_64').endswith('/x86_64.apk')
        assert nc.apk_redirect(release, 'mips').endswith('/arm64-v8a.apk')
        assert nc.apk_redirect(None, None) == nc.RELEASES_URL

    @pytest.mark.parametrize('ua, platform', [
        (ANDROID, 'android'), (IPHONE, 'ios'), ('Mozilla/5.0 (iPad; CPU OS 18)', 'ios'),
        (WINDOWS, 'desktop'), (None, 'desktop'),
    ])
    def test_platform(self, ua, platform):
        assert nc.platform_of(ua) == platform

    def test_download_page_url(self):
        user = SimpleNamespace(uuid=UUID)
        assert nc.download_page_url(_config()) == f'{BASE}/app'
        assert nc.download_page_url(_config(), user) == f'{BASE}/app/{_token()}'
        assert nc.download_page_url(_config(WEBAPP_URL='')) == nc.RELEASES_URL

    def test_the_demo_texts_name_the_page(self):
        assert f'{BASE}/app' in demo_tiers.app_hint(_config(), 'ru')
        assert f'{BASE}/app' in demo_tiers.demo_terms(_config(), 'ru')


class TestPages:

    @pytest.fixture
    def server(self, db, monkeypatch):
        monkeypatch.setattr(nc.ReleaseCache, 'get', classmethod(lambda cls: nc.pick_release(RELEASES)))
        return WebAppServer(_config(), db, xui_service=Mock())

    async def _get(self, server, token=None, ua=ANDROID, lang=None):
        req = Mock()
        req.match_info = {'token': token} if token is not None else {}
        req.headers = {'User-Agent': ua, **({'Accept-Language': lang} if lang else {})}
        return await server.handle_app(req)

    @pytest.mark.asyncio
    async def test_android_opens_its_section_with_three_apps(self, server):
        resp = await self._get(server)
        body = resp.text
        assert resp.status == 200 and resp.content_type == 'text/html'
        assert '<details id="android" open>' in body
        assert '<details id="ios">' in body and '<details id="desktop">' in body
        assert f'href="{BASE}/apk"' in body and 'Скачать NekoVPN' in body
        assert 'Версия 0.9.2-neko.3 · 57 МБ' in body
        assert f'href="{BASE}/apk?abi=armeabi-v7a"' in body
        assert nc.HIDDIFY_URL in body and nc.FLCLASH_URL in body and nc.KARING_URL in body
        for scheme in ('intent://', 'hiddify://', 'clash://', 'karing://'):
            assert scheme not in body      # no token: nothing to add
        assert '@neko_vpnbot' in body
        assert 'Демо в NekoVPN — 10 ГБ в месяц' in body
        assert resp.headers['Cache-Control'] == 'no-store'
        assert resp.headers['Referrer-Policy'] == 'no-referrer'
        assert "script-src" not in resp.headers['Content-Security-Policy']

    @pytest.mark.asyncio
    async def test_with_the_token_every_app_adds_the_subscription(self, server, db):
        _seed(db)
        body = (await self._get(server, _token())).text.replace('&amp;', '&')
        plain = f'{BASE}/sub/{_token()}'
        clash = f'{plain}?format=clash'
        assert nc.intent_link(clash, f'{BASE}/app/{_token()}') in body
        # Hiddify and FlClash sit under Android and under the computer.
        assert body.count(f'hiddify://import/{plain}#NekoVPN') == 2
        assert body.count(nc.flclash_link(clash)) == 2
        assert f'hiddify://import/{clash}' not in body
        assert f'href="{nc.flclash_link(plain)}"' not in body
        assert nc.karing_link(plain) in body
        assert f'value="{plain}"' in body and f'value="{clash}"' in body
        assert '@neko_vpnbot: /sub' not in body

    @pytest.mark.asyncio
    async def test_a_payer_sees_no_demo_line(self, server, db):
        _seed(db, status='paid', lang='en')
        body = (await self._get(server, _token())).text
        assert 'Download NekoVPN' in body and 'Demo in NekoVPN' not in body

    @pytest.mark.asyncio
    async def test_iphone_and_computer_open_their_sections(self, server):
        iphone = (await self._get(server, ua=IPHONE)).text
        assert '<details id="ios" open>' in iphone and nc.KARING_URL in iphone
        assert '<details id="android">' in iphone
        desktop = (await self._get(server, ua=WINDOWS, lang='en-US,en')).text
        assert '<details id="desktop" open>' in desktop and 'Pick an app' in desktop

    @pytest.mark.asyncio
    async def test_unknown_and_inactive_tokens(self, server, db):
        assert (await self._get(server, 'deadbeef')).status == 404
        _seed(db, status='banned')
        assert (await self._get(server, _token())).status == 410

    @pytest.mark.asyncio
    async def test_no_release_points_at_github(self, db, monkeypatch):
        monkeypatch.setattr(nc.ReleaseCache, 'get', classmethod(lambda cls: None))
        server = WebAppServer(_config(), db, xui_service=Mock())
        body = (await self._get(server)).text
        assert nc.RELEASES_URL in body and f'href="{BASE}/apk"' not in body

    @pytest.mark.asyncio
    async def test_apk_redirects_to_the_build(self, server):
        req = Mock()
        req.rel_url = SimpleNamespace(query={'abi': 'x86_64'})
        resp = await server.handle_apk(req)
        assert resp.status == 302
        assert resp.headers['Location'] == 'https://github.example/v0.9.2-neko.3/x86_64.apk'

    def test_routes(self, server):
        paths = {r.resource.canonical for r in server.app.router.routes()}
        assert {'/app', '/app/{token}', '/apk'} <= paths

    def test_import_links(self):
        url = 'https://x/sub/t'
        assert nc.hiddify_link(url) == 'hiddify://import/https://x/sub/t#NekoVPN'
        assert nc.flclash_link(url + '?format=clash') == (
            'clash://install-config?url=https%3A%2F%2Fx%2Fsub%2Ft%3Fformat%3Dclash')
        assert nc.karing_link(url) == (
            'karing://install-config?url=https%3A%2F%2Fx%2Fsub%2Ft&name=NekoVPN')

    def test_open_page_falls_back_to_the_download_page(self):
        body = nc.render_open_page('https://x/sub/t?format=clash', 'ru',
                                   download=f'{BASE}/app/t')
        intent = re.search(r'class="btn" href="([^"]+)"', body).group(1)
        assert unquote(intent.split('S.browser_fallback_url=')[1].split(';')[0]) == f'{BASE}/app/t'
