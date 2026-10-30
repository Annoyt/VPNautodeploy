"""One-tap import into the NekoVPN app — bot/services/neko_client.py and
``GET /open/<token>``.

Real sqlite: the testers list lives in app_settings.client_testers (the
list scripts/client_release_notify.py writes); the intent link is parsed
back the way Android and the app's LinkManager read it.
"""

import base64
import hashlib
import importlib.util
import json
import os
import re
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import neko_client as nc
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
UUID2 = '0f9e8d7c-6b5a-4c3d-8e2f-1a0b9c8d7e6f'
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


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


def _seed(db, chat_id='111', uuid=UUID, status='paid', platform='android', lang='ru'):
    db._users.save(User(chat_id=chat_id, username=f'u{chat_id}', status=status,
                        uuid=uuid, email=f'{chat_id}@x', platform=platform, lang=lang))
    return db._users.get_by_id(chat_id)


def _testers(db, *ids):
    db.set_setting(nc.TESTERS_KEY, json.dumps(list(ids)))


def _token(uuid=UUID):
    return SubscriptionService(_config()).derive_token(uuid)


PROFILE = f'https://dash.example.com/sub/{_token()}?format=clash'


def _intent_parts(link):
    """Android's reading of an intent:// URI: the part before #Intent is the
    data (scheme from the extras), the extras are ;-separated key=value."""
    assert link.startswith('intent://')
    data, _, extras = link[len('intent://'):].partition('#Intent;')
    fields = dict(x.split('=', 1) for x in extras.split(';') if '=' in x)
    return data, fields


class TestTesters:

    def test_ids_as_strings(self, db):
        db.set_setting(nc.TESTERS_KEY, json.dumps([111, ' 222 ', '']))
        assert nc.load_testers(db) == {'111', '222'}

    @pytest.mark.parametrize('raw', [None, 'not json', '{"a": 1}', '"111"'])
    def test_unusable_is_empty(self, db, raw):
        if raw is not None:
            db.set_setting(nc.TESTERS_KEY, raw)
        assert nc.load_testers(db) == set()

    def test_broken_db_is_empty(self):
        broken = Mock()
        broken.get_setting.side_effect = RuntimeError('boom')
        assert nc.load_testers(broken) == set()

    def test_same_list_as_the_release_notifier(self):
        spec = importlib.util.spec_from_file_location(
            'crn', os.path.join(ROOT, 'scripts', 'client_release_notify.py'))
        crn = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(crn)
        assert nc.TESTERS_KEY == crn.SETTING_KEY


class TestLinks:

    def test_intent_opens_the_app_with_the_profile(self):
        data, fields = _intent_parts(nc.intent_link(PROFILE))
        host, _, query = data.partition('?')
        assert host == 'install-config'
        # LinkManager reads uri.queryParameters['url'].
        assert parse_qs(query) == {'url': [PROFILE]}
        assert fields['scheme'] == 'nekovpn'
        assert fields['package'] == 'com.nekovpn.client'
        # Chrome wants the fallback URL percent-encoded in the extra.
        assert '/' not in fields['S.browser_fallback_url']
        assert unquote(fields['S.browser_fallback_url']) == nc.RELEASES_URL

    def test_the_profile_url_cannot_leak_into_the_intent_syntax(self):
        link = nc.intent_link(PROFILE + '&x=#;end')
        data, fields = _intent_parts(link)
        assert set(fields) == {'scheme', 'package', 'S.browser_fallback_url'}
        assert parse_qs(data.partition('?')[2])['url'] == [PROFILE + '&x=#;end']

    def test_plain_app_link(self):
        parts = urlsplit(nc.install_link(PROFILE))
        assert (parts.scheme, parts.netloc) == ('nekovpn', 'install-config')
        assert parse_qs(parts.query) == {'url': [PROFILE]}


class TestButton:

    def test_tester_with_a_key_on_android(self, db):
        _testers(db, '111')
        row = nc.open_button_row(db, _config(), _seed(db))
        assert row == [{'text': '📲 Открыть в NekoVPN',
                        'url': f'https://dash.example.com/open/{_token()}'}]

    def test_english_label(self, db):
        _testers(db, '111')
        row = nc.open_button_row(db, _config(), _seed(db, lang='en'))
        assert row[0]['text'] == '📲 Open in NekoVPN'

    @pytest.mark.parametrize('seed', [
        {'platform': 'ios'},            # no NekoVPN for iOS
        {'uuid': None},
        {'status': 'banned'},
        {'status': 'new'},
    ])
    def test_not_for(self, db, seed):
        _testers(db, '111')
        assert nc.open_button_row(db, _config(), _seed(db, **seed)) is None

    def test_everyone_on_android(self, db):
        _testers(db, '999')
        row = nc.open_button_row(db, _config(), _seed(db, status='demo'))
        assert row[0]['url'] == f'https://dash.example.com/open/{_token()}'

    @pytest.mark.parametrize('platform', ['windows', 'linux', 'macos', None])
    def test_elsewhere_testers_only(self, db, platform):
        _testers(db, '999')
        assert nc.open_button_row(db, _config(), _seed(db, platform=platform)) is None
        _testers(db, '111')
        assert nc.open_button_row(db, _config(), _seed(db, platform=platform))

    def test_no_public_address_no_button(self, db):
        _testers(db, '111')
        assert nc.open_button_row(db, _config(WEBAPP_URL=''), _seed(db)) is None

    def test_never_raises(self):
        broken = Mock()
        broken.get_setting.side_effect = RuntimeError('boom')
        user = SimpleNamespace(chat_id='1', uuid=UUID, status='paid',
                               platform='windows', lang='ru')
        # The tester list is unreadable: no button off Android...
        assert nc.open_button_row(broken, _config(), user) is None
        # ...and on Android the list is not needed.
        user.platform = 'android'
        assert nc.open_button_row(broken, _config(), user)


class TestOpenPage:

    @pytest.fixture
    def server(self, db):
        return WebAppServer(_config(), db, xui_service=Mock())

    async def _get(self, server, token):
        req = Mock()
        req.match_info = {'token': token}
        return await server.handle_open(req)

    @pytest.mark.asyncio
    async def test_unknown_token(self, server, db):
        _seed(db)
        assert (await self._get(server, 'deadbeef')).status == 404

    @pytest.mark.asyncio
    async def test_inactive_user(self, server, db):
        _seed(db, status='banned')
        assert (await self._get(server, _token())).status == 410

    @pytest.mark.asyncio
    async def test_page(self, server, db):
        _seed(db)
        resp = await self._get(server, _token())
        assert resp.status == 200 and resp.content_type == 'text/html'
        body = resp.text
        # The button is the intent link, HTML-escaped (& -> &amp;).
        href = re.search(r'class="btn" href="([^"]+)"', body).group(1)
        assert href.replace('&amp;', '&') == nc.intent_link(PROFILE)
        assert f'value="{PROFILE}"' in body       # the copy fallback
        assert 'Открыть в NekoVPN' in body and nc.RELEASES_URL in body
        assert resp.headers['Cache-Control'] == 'no-store'
        assert resp.headers['Referrer-Policy'] == 'no-referrer'
        assert resp.headers['X-Robots-Tag'] == 'noindex'

    @pytest.mark.asyncio
    async def test_csp_allows_exactly_the_page_script(self, server, db):
        _seed(db)
        resp = await self._get(server, _token())
        script = re.search(r'<script>(.*?)</script>', resp.text, re.S).group(1)
        digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        csp = resp.headers['Content-Security-Policy']
        assert f"script-src 'sha256-{digest}'" in csp
        assert "default-src 'none'" in csp and "'unsafe-inline'" not in csp.split('script-src')[1]

    @pytest.mark.asyncio
    async def test_english_page(self, server, db):
        _seed(db, lang='en')
        resp = await self._get(server, _token())
        assert 'Open in NekoVPN' in resp.text and '<html lang="en">' in resp.text


# ---------- where the button shows ----------

from bot.handlers.callbacks.user import build_key_delivery_message  # noqa: E402
from bot.handlers.commands import CommandHandler                    # noqa: E402


def _rows(kb):
    return kb['inline_keyboard']


class TestPlacement:

    def test_key_card_first_row_on_android(self, db):
        _, kb = build_key_delivery_message(_seed(db), _config(), db=db)
        assert _rows(kb)[0][0]['text'] == '📲 Открыть в NekoVPN'

    def test_key_card_first_row_for_pc_testers_only(self, db):
        user = _seed(db, platform='windows')
        _, before = build_key_delivery_message(user, _config(), db=db)
        assert all('open/' not in json.dumps(r) for r in _rows(before))
        _testers(db, '111')
        _, after = build_key_delivery_message(user, _config(), db=db)
        assert _rows(after)[0][0]['text'] == '📲 Открыть в NekoVPN'
        # Everything else stays as it was.
        assert _rows(after)[1:] == _rows(before)

    def _sub_keyboard(self, db, chat_id='111'):
        bot = Mock()
        CommandHandler(bot, db, _config()).handle_sub({}, chat_id)
        return bot.send_message.call_args.kwargs['reply_markup']

    def test_sub_command_first_row_on_android(self, db):
        _seed(db)
        assert _rows(self._sub_keyboard(db))[0][0]['url'].endswith(f'/open/{_token()}')

    def test_sub_command_first_row_for_pc_testers_only(self, db):
        _seed(db, platform='windows')
        before = self._sub_keyboard(db)
        _testers(db, '111')
        after = self._sub_keyboard(db)
        assert _rows(after)[0][0]['url'].endswith(f'/open/{_token()}')
        assert _rows(after)[1:] == _rows(before)

    def test_ios_tester_gets_no_button(self, db):
        _seed(db, platform='ios')
        _testers(db, '111')
        assert all('open/' not in json.dumps(r) for r in _rows(self._sub_keyboard(db)))
