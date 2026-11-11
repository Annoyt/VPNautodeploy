"""xhr's own server on exit, the bot's side: ``bot/services/xhr_exit.py`` and
``GET /api/xhr/users`` (IMPROVEMENT_PLAN F, E3; scripts/setup_xhr_exit.sh).

Level 2: a real sqlite. Pinned: who the list carries (xhr switched on in
the cascade, paid and not lapsed — the rule every unmetered path asks),
that everyone listed has xhr in their own profile, that the server block
is what the profiles carry, and that a read error answers 503, never an
empty list (the exit's sync would drop every user). The script's side and
the docker stand are in tests/unit/test_setup_xhr_exit_sh.py.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
from bot.services import xhr_exit
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning",
)

TOKEN = 'tok-123'
NOW = datetime(2026, 11, 11, 12, 0, 0)
ON = json.dumps([{'name': 'reality', 'enabled': True}, {'name': 'xhr', 'enabled': True},
                 {'name': 'ws', 'enabled': True}])

PAID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
PAID_DATED = '0f9e8d7c-6b5a-4c3d-8e2f-1a0b9c8d7e6f'
LAPSED = '11111111-2222-4333-8444-555555555555'
TICKET_PAYER = '22222222-2222-4333-8444-555555555555'
TICKET_PAYER_UNDATED = '33333333-2222-4333-8444-555555555555'
TICKET_DEMO = '44444444-2222-4333-8444-555555555555'
DEMO = '55555555-2222-4333-8444-555555555555'
BANNED = '66666666-2222-4333-8444-555555555555'
UPPER = 'AAAAAAAA-2222-4333-8444-555555555555'

ROWS = [
    # chat_id, status, uuid, subscription_expiry, previous_state
    ('1', 'paid', PAID, None, None),
    ('2', 'paid', PAID_DATED, (NOW + timedelta(days=20)).isoformat(), None),
    ('3', 'paid', LAPSED, (NOW - timedelta(days=1)).isoformat(), None),
    ('4', 'support_topic', TICKET_PAYER, (NOW + timedelta(days=5)).isoformat(), 'paid'),
    ('5', 'support_topic', TICKET_PAYER_UNDATED, None, 'paid'),
    ('6', 'support_topic', TICKET_DEMO, None, 'demo'),
    ('7', 'demo', DEMO, None, None),
    ('8', 'banned', BANNED, None, 'paid'),
    ('9', 'paid', None, None, None),
    ('10', 'paid', 'not-a-uuid', None, None),
    ('11', 'paid', UPPER, None, None),
    ('12', 'paid', PAID.upper(), None, None),  # the same id to xray (UNIQUE is per text)
]
LISTED = sorted([PAID, PAID_DATED, TICKET_PAYER, TICKET_PAYER_UNDATED, UPPER.lower()])


def _config(**over):
    cfg = dict(
        BOT_TOKEN='t', WEBAPP_URL='https://dash.example.com', DPI_REPORT_TOKEN=TOKEN,
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='main-pbk', SNI_VALUE='www.bing.com', SID_VALUE='0123456789abcdef',
        HY2_HOST='', HY2_PORT=8400, HY2_SNI='', HY2_OBFS_PASSWORD='', HY2_HOP_PORTS='',
        HY2T_PORT='', WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api',
        WS_SNI='cdn.example.com', WS2_HOST='', STLS_HOST='', STLS_PORT=443, STLS_SNI='',
        STLS_VERSION=3, STLS_PASSWORD='', SS_METHOD='2022-blake3-aes-128-gcm',
        SS_SERVER_PASSWORD='', SS_USER_SALT='salt', SUB_MIRROR_URLS='',
        FALLBACK_NODE_HOST='', TLS_FINGERPRINTS='',
        XHR_SNI='dl.google.com', XHR_PATH='/xhr', XHR_PUBLIC_KEY='xhr-pbk', XHR_SID='abcd',
        XHR_PORT=0, XHR_MAX_CONNECTIONS=3,
        SSH_EXIT_HOST='', SSH_EXIT_PORT='22022', SSH_HOST_KEY='', SSH_USER_SECRET='',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / 'bot.db'))
    with d._connect() as conn:
        conn.executemany(
            "INSERT INTO users (chat_id, username, email, status, uuid, subscription_expiry, "
            "previous_state) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(cid, f'u{cid}', f'user_u{cid}_{cid}@nekovo.ru', status, uuid, expiry, prev)
             for cid, status, uuid, expiry, prev in ROWS])
        conn.commit()
    return d


def _uuids(payload):
    return [u['uuid'] for u in payload['users']]


class TestWhoIsListed:

    def test_nobody_while_xhr_is_off_in_the_cascade(self, db):
        assert xhr_exit.users_payload(db, NOW) == {'in_cascade': False, 'users': []}
        for stored in (json.dumps(['reality', 'hy2']),        # the legacy list format
                       json.dumps([{'name': 'xhr', 'enabled': False}]),
                       '{not json'):
            db.set_setting(MK.SETTING_KEY, stored)
            assert xhr_exit.users_payload(db, NOW) == {'in_cascade': False, 'users': []}, stored

    def test_payers_who_have_not_lapsed(self, db):
        db.set_setting(MK.SETTING_KEY, ON)
        payload = xhr_exit.users_payload(db, NOW)
        assert payload['in_cascade'] is True
        assert _uuids(payload) == LISTED
        assert payload['users'] == [{'uuid': u} for u in LISTED]

    def test_the_second_of_expiry(self, db):
        db.set_setting(MK.SETTING_KEY, ON)
        with db._connect() as conn:
            conn.execute("UPDATE users SET subscription_expiry = ? WHERE uuid = ?",
                         ((NOW + timedelta(seconds=1)).isoformat(), LAPSED))
            conn.commit()
        assert LAPSED in _uuids(xhr_exit.users_payload(db, NOW))
        assert LAPSED not in _uuids(xhr_exit.users_payload(db, NOW + timedelta(seconds=2)))

    def test_an_offset_in_the_date_is_read(self, db):
        db.set_setting(MK.SETTING_KEY, ON)
        with db._connect() as conn:
            conn.execute("UPDATE users SET subscription_expiry = ? WHERE uuid = ?",
                         ('2026-11-11T14:30:00+03:00', LAPSED))   # 11:30 UTC
            conn.commit()
        assert LAPSED not in _uuids(xhr_exit.users_payload(db, NOW))
        assert LAPSED in _uuids(xhr_exit.users_payload(db, NOW - timedelta(minutes=31)))

    def test_everyone_listed_has_xhr_in_their_own_profile(self, db):
        db.set_setting(MK.SETTING_KEY, ON)
        cfg = _config()
        svc = SubscriptionService(cfg)
        carriers = set()
        for cid, status, uuid, expiry, prev in ROWS:
            if not uuid or uuid == 'not-a-uuid':
                continue
            user = SimpleNamespace(chat_id=cid, uuid=uuid, email=f'u{cid}@x', status=status,
                                   lang='ru', subscription_expiry=expiry, previous_state=prev,
                                   last_asn=None, last_country=None)
            protocols = MK.get_cascade_order(db, user=user)
            if 'type=xhttp' in svc.build_links(user, protocols):
                carriers.add(uuid.lower())
        listed = set(_uuids(xhr_exit.users_payload(db, NOW)))
        assert listed <= carriers
        # the profile also hands xhr out where this server says no: a lapsed
        # payer (status paid) and a demo user's open ticket; their entry fails
        assert carriers - listed == {LAPSED, TICKET_DEMO}

    def test_the_tier_is_paid(self):
        # The server admits payers only, because nothing meters its traffic.
        # A free tier needs xhr traffic in the panel's counters first.
        assert MK.PROTOCOL_TIER['xhr'] == 'paid'
        assert 'xhr' in MK.OPT_IN_PROTOCOLS


class TestReadErrors:

    def test_a_broken_settings_table_raises(self, db):
        db.set_setting(MK.SETTING_KEY, ON)
        with db._connect() as conn:
            conn.execute("DROP TABLE app_settings")
            conn.commit()
        # the tolerant reader takes the failed read for "no setting"...
        assert db.get_setting(MK.SETTING_KEY) is None
        # ...which would list nobody: this one raises instead
        with pytest.raises(sqlite3.Error):
            xhr_exit.users_payload(db, NOW)

    def test_a_broken_users_table_raises(self, db):
        db.set_setting(MK.SETTING_KEY, ON)
        with db._connect() as conn:
            conn.execute("ALTER TABLE users RENAME TO users_gone")
            conn.commit()
        with pytest.raises(sqlite3.Error):
            xhr_exit.users_payload(db, NOW)


class TestProfileServer:

    def test_what_the_profiles_carry(self):
        cfg = _config()
        server = xhr_exit.profile_server(cfg)
        assert server == {'sni': 'dl.google.com', 'pbk': 'xhr-pbk', 'sid': 'abcd', 'path': '/xhr'}
        user = SimpleNamespace(uuid=PAID, email='u1@x', status='paid', lang='ru', chat_id='1',
                               subscription_expiry=None, previous_state=None)
        clash = json.loads(SubscriptionService(cfg).build_clash_config(user, ('xhr',)))
        proxy = next(p for p in clash['proxies'] if p['name'] == 'u1-xhr')
        assert (proxy['servername'], proxy['reality-opts'], proxy['xhttp-opts']['path']) == (
            server['sni'], {'public-key': server['pbk'], 'short-id': server['sid']}, server['path'])

    def test_unset_keys_fall_back_to_the_main_inbound(self):
        # what the profiles then carry, and what the exit's sync flags
        server = xhr_exit.profile_server(_config(XHR_PUBLIC_KEY='', XHR_SID=''))
        assert (server['pbk'], server['sid']) == ('main-pbk', '0123456789abcdef')

    @pytest.mark.parametrize('over', [dict(XHR_SNI=''), dict(ENTRY_NODE_IP=''),
                                      dict(XHR_PUBLIC_KEY='', REALITY_PUBLIC_KEY='')])
    def test_off(self, over):
        assert xhr_exit.profile_server(_config(**over)) is None


class TestEndpoint:

    def _server(self, db, **over):
        return WebAppServer(_config(**over), db, xui_service=Mock())

    def test_the_route(self, db):
        routes = {(r.method, r.resource.canonical) for r in self._server(db).app.router.routes()}
        assert ('GET', '/api/xhr/users') in routes

    @pytest.mark.asyncio
    async def test_over_http(self, db):
        db.set_setting(MK.SETTING_KEY, ON)
        async with TestClient(TestServer(self._server(db).app)) as client:
            resp = await client.get('/api/xhr/users', headers={'X-DPI-Token': TOKEN})
            assert resp.status == 200
            assert resp.headers['Cache-Control'] == 'no-store'
            body = await resp.json()
        assert body['ok'] is True and body['in_cascade'] is True
        assert _uuids(body) == [u for u in LISTED]
        assert body['server'] == {'sni': 'dl.google.com', 'pbk': 'xhr-pbk', 'sid': 'abcd',
                                  'path': '/xhr'}

    @pytest.mark.asyncio
    @pytest.mark.parametrize('headers', [{}, {'X-DPI-Token': 'nope'},
                                         {'X-DPI-Token': 'тук'}])
    async def test_the_token(self, db, headers):
        db.set_setting(MK.SETTING_KEY, ON)
        req = Mock()
        req.headers = headers
        resp = await self._server(db).handle_xhr_users(req)
        assert resp.status == 403
        assert 'uuid' not in resp.text

    @pytest.mark.asyncio
    async def test_no_token_configured(self, db):
        req = Mock()
        req.headers = {'X-DPI-Token': ''}
        assert (await self._server(db, DPI_REPORT_TOKEN='').handle_xhr_users(req)).status == 403

    @pytest.mark.asyncio
    async def test_not_configured(self, db):
        req = Mock()
        req.headers = {'X-DPI-Token': TOKEN}
        resp = await self._server(db, XHR_SNI='').handle_xhr_users(req)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_off_in_the_cascade_is_an_empty_list(self, db):
        req = Mock()
        req.headers = {'X-DPI-Token': TOKEN}
        resp = await self._server(db).handle_xhr_users(req)
        assert resp.status == 200
        assert json.loads(resp.text)['users'] == []
        assert json.loads(resp.text)['in_cascade'] is False

    @pytest.mark.asyncio
    async def test_a_read_error_is_503_not_an_empty_list(self, db):
        db.set_setting(MK.SETTING_KEY, ON)
        with db._connect() as conn:
            conn.execute("DROP TABLE app_settings")
            conn.commit()
        req = Mock()
        req.headers = {'X-DPI-Token': TOKEN}
        resp = await self._server(db).handle_xhr_users(req)
        assert resp.status == 503
        assert 'users' not in json.loads(resp.text)

    @pytest.mark.asyncio
    async def test_a_broken_profile_check_is_503(self, db, monkeypatch):
        def boom(config):
            raise RuntimeError('boom')
        monkeypatch.setattr(xhr_exit, 'profile_server', boom)
        req = Mock()
        req.headers = {'X-DPI-Token': TOKEN}
        assert (await self._server(db).handle_xhr_users(req)).status == 503
