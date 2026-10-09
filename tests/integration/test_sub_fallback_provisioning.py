"""Paid /sub fetches provision the DE reserve lazily, at most once per 10 min.

``handle_subscription`` builds a new ``FallbackNodeService`` for every
request. The membership cache used to live on the instance, so it never hit,
and every paid /sub fetch logged into the reserve panel (login + inbound GET
over HTTPS). The cache now belongs to the class. What stays the same:
provisioning is lazy (a paid /sub fetch adds the client if it is missing),
idempotent (a present client is not added again), a failed panel call is
retried on the next fetch, and a dead reserve panel never breaks /sub
(AGENTS.md §24).

Level 2: the /sub handler on a REAL sqlite with the real builders and the
real FallbackNodeService, which talks HTTP to a fake 2.8.x panel on
127.0.0.1:0. Only the geo lookups (conftest: none) and the main panel's quota
read are stubbed.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bot.services.fallback_node as fallback_node
from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services.fallback_node import FallbackNodeService, _ENSURE_CACHE_TTL

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '0f9e8d7c-6b5a-4f3e-8d2c-1b0a9f8e7d6c'
EMAIL = 'user_u1_1@nekovo.ru'
BASE = '/sub'          # the reserve panel's webBasePath
INBOUND = 1


class FakePanel:
    """The reserve x-ui 2.8.x panel, as much of it as FallbackNodeService uses:
    form login, GET inbound, POST addClient. ``healthy=False`` answers 500."""

    def __init__(self):
        self.clients = {}      # email -> uuid on the fallback inbound
        self.calls = []        # (method, path) in arrival order
        self.healthy = True
        panel = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, payload, status=200):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                panel.calls.append(('GET', self.path))
                if not panel.healthy:
                    return self._reply({'success': False}, 500)
                if self.path == f'{BASE}/panel/api/inbounds/get/{INBOUND}':
                    clients = [{'email': e, 'id': u} for e, u in panel.clients.items()]
                    return self._reply({'success': True, 'obj': {
                        'settings': json.dumps({'clients': clients})}})
                self._reply({'success': False}, 404)

            def do_POST(self):
                length = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(length) if length else b''
                panel.calls.append(('POST', self.path))
                if not panel.healthy:
                    return self._reply({'success': False}, 500)
                if self.path == f'{BASE}/login':
                    return self._reply({'success': True})
                if self.path == f'{BASE}/panel/api/inbounds/addClient':
                    for client in json.loads(json.loads(raw)['settings'])['clients']:
                        panel.clients[client['email']] = client['id']
                    return self._reply({'success': True})
                self._reply({'success': False}, 404)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()

    def logins(self):
        return sum(1 for _method, path in self.calls if path == f'{BASE}/login')

    def adds(self):
        return sum(1 for _method, path in self.calls if path.endswith('/addClient'))

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


@pytest.fixture
def panel():
    p = FakePanel()
    yield p
    p.stop()


class _Clock:
    """Stands in for the ``time`` module inside fallback_node."""

    def __init__(self):
        self.now = 5000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(fallback_node, 'time', c)
    return c


def _config(panel_url, **over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL='https://dash.example.com',
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
        SS_USER_SALT='salt', SUB_MIRROR_URLS='',
        FALLBACK_NODE_HOST='198.51.100.9', FALLBACK_NODE_PORT=443,
        FALLBACK_NODE_PBK='de-pbk', FALLBACK_NODE_SID='de01',
        FALLBACK_NODE_SNI='www.google.com',
        FALLBACK_NODE_XUI_URL=panel_url, FALLBACK_NODE_XUI_BASE_PATH=BASE,
        FALLBACK_NODE_XUI_USER='admin', FALLBACK_NODE_XUI_PASS='pw',
        FALLBACK_NODE_INBOUND_ID=INBOUND,
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def _server(tmp_path, panel_url, status='paid'):
    db = Database(str(tmp_path / 'bot.db'))
    db._users.save(User(chat_id='1', username='u1', status=status, uuid=UUID,
                        email=EMAIL, quota_gb=100.0, lang='ru'))
    db.set_setting('cascade_protocol_order', json.dumps(['reality', 'hy2', 'ws', 'stls']))
    srv = WebAppServer(_config(panel_url), db, xui_service=Mock())
    srv.xui.get_client_traffic = AsyncMock(return_value={'upload': 1, 'download': 2})
    return srv, db


async def _sub(srv, query=None):
    req = Mock()
    req.match_info = {'token': srv.subscription.derive_token(UUID)}
    req.rel_url = SimpleNamespace(query=query or {})
    req.headers = {'User-Agent': 'Hiddify/2.5.7', 'X-Forwarded-For': '198.51.100.77'}
    req.remote = ''
    return await srv.handle_subscription(req)


def _de_served(resp):
    """200, and the profile still carries the DE outbound."""
    assert resp.status == 200
    tags = [o.get('tag') for o in json.loads(resp.text)['outbounds']]
    return 'user_u1_1-de' in tags


class TestProvisioningCache:

    @pytest.mark.asyncio
    async def test_first_fetch_provisions_the_missing_client(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
        assert panel.clients == {EMAIL: UUID}
        assert (panel.logins(), panel.adds()) == (1, 1)

    @pytest.mark.asyncio
    async def test_a_second_fetch_within_ten_minutes_skips_the_panel(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
        calls = len(panel.calls)
        clock.now += _ENSURE_CACHE_TTL - 1
        for query in ({}, {'format': 'clash'}, {'format': 'links'}):
            assert (await _sub(srv, query)).status == 200
        assert len(panel.calls) == calls

    @pytest.mark.asyncio
    async def test_after_ten_minutes_the_panel_is_asked_again(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
        clock.now += _ENSURE_CACHE_TTL
        assert _de_served(await _sub(srv))
        # asked again, found present: idempotent, no second add
        assert (panel.logins(), panel.adds()) == (2, 1)

    @pytest.mark.asyncio
    async def test_a_present_client_is_not_added_again(self, tmp_path, panel, clock):
        panel.clients[EMAIL] = UUID
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
        assert (panel.logins(), panel.adds()) == (1, 0)

    @pytest.mark.asyncio
    async def test_provider_refreshes_and_demo_users_never_touch_the_panel(
            self, tmp_path, panel, clock):
        srv, db = _server(tmp_path, panel.url)
        assert (await _sub(srv, {'format': 'clash-proxies'})).status == 200
        user = db._users.get_by_id('1')
        user.status = 'demo'
        db._users.save(user)
        assert not _de_served(await _sub(srv))
        assert panel.calls == []


class TestDeadPanelNeverBreaksSub:

    @pytest.mark.asyncio
    async def test_a_failing_panel_is_retried_on_the_next_fetch(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        panel.healthy = False
        assert _de_served(await _sub(srv))
        assert panel.clients == {}
        panel.healthy = True
        assert _de_served(await _sub(srv))
        assert panel.clients == {EMAIL: UUID}

    @pytest.mark.asyncio
    async def test_an_unreachable_panel(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        panel.stop()
        assert _de_served(await _sub(srv))
        assert _de_served(await _sub(srv))

    @pytest.mark.asyncio
    async def test_an_exception_out_of_the_service(self, tmp_path, panel, clock, monkeypatch):
        def boom(self, user):
            raise RuntimeError('reserve exploded')

        monkeypatch.setattr(FallbackNodeService, 'ensure_client', boom)
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
