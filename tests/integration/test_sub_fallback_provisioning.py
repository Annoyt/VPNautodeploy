"""Paid /sub fetches provision the DE reserve lazily, off the response path.

``handle_subscription`` builds a new ``FallbackNodeService`` for every
request. The membership cache used to live on the instance, so it never hit,
and every paid /sub fetch logged into the reserve panel (login + inbound GET
over HTTPS). The cache now belongs to the class. What stays the same:
provisioning is lazy (a paid /sub fetch adds the client if it is missing),
idempotent (a present client is not added again), a failed panel call is
retried on the next fetch, and a dead reserve panel never breaks /sub
(AGENTS.md §24).

Nor does it slow /sub down. The call is a background task, at most one per
uuid in flight, on a thread of its own, and a panel that does not answer at
all is skipped by every call for ``_PANEL_SKIP_S``. Awaited inline, each
request may take the full 15-s timeout. A blackholed panel used to hold
every paid fetch that missed the cache for that long. A few such calls on the
default pool also stalled the to_thread calls of every other request.

Level 2: the /sub handler on a REAL sqlite with the real builders and the
real FallbackNodeService, which talks HTTP to a fake 2.8.x panel on
127.0.0.1:0, or to a socket that accepts the connection and never answers.
Only the geo lookups (conftest: none) and the main panel's quota read are
stubbed. The background calls are awaited with ``_flush`` before anything is
asserted about the panel, including what it did NOT see.
"""

import asyncio
import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bot.services.fallback_node as fallback_node
from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services.fallback_node import (
    FallbackNodeService, _ENSURE_CACHE_TTL, _PANEL_SKIP_S,
)
from bot.services.sos import SosService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '0f9e8d7c-6b5a-4f3e-8d2c-1b0a9f8e7d6c'
EMAIL = 'user_u1_1@nekovo.ru'
BASE = '/sub'          # the reserve panel's webBasePath
INBOUND = 1
# "Answers at once": far below the panel's 15-s timeout, which an inline
# call would wait out; generous for a slow CI runner.
FAST = 3.0


class FakePanel:
    """The reserve x-ui 2.8.x panel, as much of it as FallbackNodeService uses:
    form login, GET inbound, POST addClient. ``healthy=False`` answers 500;
    ``garbage=True`` answers the inbound read with an HTML page (what x-ui
    serves for a dropped session) — an answer, just not JSON."""

    def __init__(self):
        self.clients = {}      # email -> uuid on the fallback inbound
        self.calls = []        # (method, path) in arrival order
        self.healthy = True
        self.garbage = False
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
                if panel.garbage:
                    body = b'<html><body>login</body></html>'
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
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


class HangingPanel:
    """A reserve panel that accepts the TCP connection and never answers:
    a blackholed upstream behind a live port. ``release()`` hangs up on every
    held connection, so the waiting call fails at once, and from then on it
    hangs up on new ones as they arrive."""

    def __init__(self):
        self.accepted = 0
        self._held = []
        self._hang = True
        self._lock = threading.Lock()
        self._running = True
        self._sock = socket.create_server(('127.0.0.1', 0))
        self._sock.settimeout(0.05)
        self.url = f'http://127.0.0.1:{self._sock.getsockname()[1]}'
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while self._running:
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self.accepted += 1
                if self._hang:
                    self._held.append(conn)
                    continue
            conn.close()

    async def wait_for(self, n):
        """Until ``n`` connections have arrived (a background call did start)."""
        deadline = time.monotonic() + FAST
        while self.accepted < n:
            assert time.monotonic() < deadline, f'{self.accepted} connections, not {n}'
            await asyncio.sleep(0.01)

    def release(self):
        with self._lock:
            self._hang = False
            held, self._held = self._held, []
        for conn in held:
            conn.close()

    def stop(self):
        self.release()
        self._running = False
        self._thread.join(timeout=1)
        self._sock.close()


@pytest.fixture
def panel():
    p = FakePanel()
    yield p
    p.stop()


@pytest.fixture
def hanging():
    p = HangingPanel()
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


@pytest.fixture(autouse=True)
def _no_kit_rate_limit():
    SosService._kit_sent_at.clear()
    yield
    SosService._kit_sent_at.clear()


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


def _add_user(db, n, status='paid'):
    """Another keyed user; returns the uuid."""
    uuid = f'0f9e8d7c-6b5a-4f3e-8d2c-{n:012d}'
    db._users.save(User(chat_id=str(n), username=f'u{n}', status=status, uuid=uuid,
                        email=f'user_u{n}_{n}@nekovo.ru', quota_gb=100.0, lang='ru'))
    return uuid


async def _sub(srv, query=None, uuid=UUID):
    req = Mock()
    req.match_info = {'token': srv.subscription.derive_token(uuid)}
    req.rel_url = SimpleNamespace(query=query or {})
    req.headers = {'User-Agent': 'Hiddify/2.5.7', 'X-Forwarded-For': '198.51.100.77'}
    req.remote = ''
    return await srv.handle_subscription(req)


async def _timed_sub(srv, **kw):
    """/sub, asserted to answer at once."""
    started = time.perf_counter()
    resp = await _sub(srv, **kw)
    assert time.perf_counter() - started < FAST
    return resp


async def _flush(srv):
    """Wait for the background calls /sub started. Only on PENDING tasks:
    since 3.12 gather() over finished tasks completes without yielding, so
    looping on the set until their discard callbacks run would spin."""
    while True:
        pending = [t for t in srv._bg_tasks if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending)


def _de_served(resp):
    """200, and the profile still carries the DE outbound."""
    assert resp.status == 200
    tags = [o.get('tag') for o in json.loads(resp.text)['outbounds']]
    return 'user_u1_1-de' in tags


def _spy_ensure_client(monkeypatch):
    """The uuid of every ensure_client call, in the order they ran."""
    seen = []
    real = FallbackNodeService.ensure_client

    def spy(self, user):
        seen.append(user.uuid)
        return real(self, user)

    monkeypatch.setattr(FallbackNodeService, 'ensure_client', spy)
    return seen


class TestProvisioningCache:

    @pytest.mark.asyncio
    async def test_first_fetch_provisions_the_missing_client(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
        await _flush(srv)
        assert panel.clients == {EMAIL: UUID}
        assert (panel.logins(), panel.adds()) == (1, 1)

    @pytest.mark.asyncio
    async def test_a_second_fetch_within_ten_minutes_skips_the_panel(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
        await _flush(srv)
        calls = len(panel.calls)
        clock.now += _ENSURE_CACHE_TTL - 1
        for query in ({}, {'format': 'clash'}, {'format': 'links'}):
            assert (await _sub(srv, query)).status == 200
            await _flush(srv)
        assert len(panel.calls) == calls

    @pytest.mark.asyncio
    async def test_after_ten_minutes_the_panel_is_asked_again(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
        await _flush(srv)
        clock.now += _ENSURE_CACHE_TTL
        assert _de_served(await _sub(srv))
        await _flush(srv)
        # asked again, found present: idempotent, no second add
        assert (panel.logins(), panel.adds()) == (2, 1)

    @pytest.mark.asyncio
    async def test_a_present_client_is_not_added_again(self, tmp_path, panel, clock):
        panel.clients[EMAIL] = UUID
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
        await _flush(srv)
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
        await _flush(srv)
        assert panel.calls == []


class TestDeadPanelNeverBreaksSub:

    @pytest.mark.asyncio
    async def test_a_failing_panel_is_retried_on_the_next_fetch(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        panel.healthy = False
        assert _de_served(await _sub(srv))
        await _flush(srv)
        assert panel.clients == {}
        panel.healthy = True
        assert _de_served(await _sub(srv))
        await _flush(srv)
        assert panel.clients == {EMAIL: UUID}

    @pytest.mark.asyncio
    async def test_an_unreachable_panel(self, tmp_path, panel, clock):
        srv, _db = _server(tmp_path, panel.url)
        panel.stop()
        assert _de_served(await _sub(srv))
        await _flush(srv)
        assert _de_served(await _sub(srv))
        await _flush(srv)

    @pytest.mark.asyncio
    async def test_an_exception_out_of_the_service(self, tmp_path, panel, clock, monkeypatch):
        calls = []

        def boom(self, user):
            calls.append(user.uuid)
            raise RuntimeError('reserve exploded')

        monkeypatch.setattr(FallbackNodeService, 'ensure_client', boom)
        srv, _db = _server(tmp_path, panel.url)
        assert _de_served(await _sub(srv))
        tasks = list(srv._bg_tasks)
        await _flush(srv)
        # handled inside the task: nothing left for "exception never retrieved"
        assert tasks and all(t.exception() is None for t in tasks)
        # the failed call released its uuid: the next fetch tries again
        assert _de_served(await _sub(srv))
        await _flush(srv)
        assert calls == [UUID, UUID]


class TestSubNeverWaitsForThePanel:
    """The panel accepts the TCP connection and never answers."""

    @pytest.mark.asyncio
    async def test_sub_answers_while_the_call_hangs(self, tmp_path, hanging, clock):
        # an inline call would wait out the timeout, far longer than FAST
        assert fallback_node._PANEL_TIMEOUT_S > 2 * FAST
        srv, _db = _server(tmp_path, hanging.url)
        assert _de_served(await _timed_sub(srv))
        await hanging.wait_for(1)        # provisioning did start, and hangs
        # while it hangs, the next fetches answer at once as well
        assert (await _timed_sub(srv, query={'format': 'clash'})).status == 200
        assert _de_served(await _timed_sub(srv))
        hanging.release()
        await _flush(srv)

    @pytest.mark.asyncio
    async def test_a_burst_from_one_user_starts_one_call(self, tmp_path, hanging, clock,
                                                         monkeypatch):
        calls = _spy_ensure_client(monkeypatch)
        srv, _db = _server(tmp_path, hanging.url)
        for _ in range(5):
            assert _de_served(await _timed_sub(srv))
        await hanging.wait_for(1)
        hanging.release()
        await _flush(srv)
        assert calls == [UUID]
        assert hanging.accepted == 1
        # the uuid is released with its call: the next fetch starts another
        # (inside the skip window, it does not reach the panel)
        assert _de_served(await _timed_sub(srv))
        await _flush(srv)
        assert calls == [UUID, UUID]
        assert hanging.accepted == 1

    @pytest.mark.asyncio
    async def test_the_default_pool_stays_free(self, tmp_path, hanging, clock):
        """A small host's default pool has a handful of threads, and every
        to_thread of this server shares them. Here it has ONE: while a paid
        user's reserve call hangs, a demo user's /sub, which never involves
        the panel, still answers at once."""
        pool = ThreadPoolExecutor(max_workers=1)
        asyncio.get_running_loop().set_default_executor(pool)
        srv, db = _server(tmp_path, hanging.url)
        demo = _add_user(db, 2, status='demo')
        assert _de_served(await _timed_sub(srv))
        await hanging.wait_for(1)
        resp = await asyncio.wait_for(_sub(srv, uuid=demo), timeout=FAST)
        assert resp.status == 200
        hanging.release()
        await _flush(srv)
        pool.shutdown(wait=False)


class TestDeadPanelCostsOneTimeout:

    @pytest.fixture
    def short_timeout(self, monkeypatch):
        monkeypatch.setattr(fallback_node, '_PANEL_TIMEOUT_S', 0.5)

    @pytest.mark.asyncio
    async def test_one_timeout_not_one_per_user(self, tmp_path, hanging, clock, short_timeout,
                                                monkeypatch):
        calls = _spy_ensure_client(monkeypatch)
        srv, db = _server(tmp_path, hanging.url)
        uuids = [UUID] + [_add_user(db, n) for n in range(2, 6)]
        for uuid in uuids:
            assert (await _timed_sub(srv, uuid=uuid)).status == 200
        await _flush(srv)
        # one call per user, and one of them waited out the read timeout;
        # the rest found the panel in its skip window
        assert sorted(calls) == sorted(uuids)
        assert hanging.accepted == 1

    @pytest.mark.asyncio
    async def test_the_window_lasts_a_minute(self, tmp_path, hanging, clock, short_timeout):
        srv, db = _server(tmp_path, hanging.url)
        other, third = _add_user(db, 2), _add_user(db, 3)
        await _sub(srv)
        await _flush(srv)
        assert hanging.accepted == 1
        clock.now += _PANEL_SKIP_S - 1
        await _sub(srv, uuid=other)
        await _flush(srv)
        assert hanging.accepted == 1
        clock.now += 1
        await _sub(srv, uuid=third)
        await _flush(srv)
        assert hanging.accepted == 2

    @pytest.mark.asyncio
    async def test_a_refused_connection_opens_the_window_too(self, tmp_path, panel, clock):
        srv, db = _server(tmp_path, panel.url)
        panel.stop()                      # nothing listens there now
        await _sub(srv)
        await _flush(srv)
        revived = FakePanel()             # the panel is back, on another port
        try:
            srv.config.FALLBACK_NODE_XUI_URL = revived.url
            await _sub(srv, uuid=_add_user(db, 2))
            await _flush(srv)
            assert revived.calls == []    # inside the window
            clock.now += _PANEL_SKIP_S
            await _sub(srv, uuid=_add_user(db, 3))
            await _flush(srv)
            assert revived.logins() == 1
        finally:
            revived.stop()

    @pytest.mark.asyncio
    async def test_an_answer_is_no_reason_to_skip(self, tmp_path, panel, clock):
        """Garbage from a live panel fails the call, and the next fetch asks
        again at once: only silence opens the window."""
        srv, _db = _server(tmp_path, panel.url)
        panel.garbage = True
        assert _de_served(await _sub(srv))
        await _flush(srv)
        assert panel.clients == {}
        panel.garbage = False
        assert _de_served(await _sub(srv))
        await _flush(srv)
        assert panel.clients == {EMAIL: UUID}


class TestKit:
    """/kit provisions in its own worker thread, synchronously, as before."""

    def _kit(self, db, config):
        bot = Mock()
        bot.send_message.return_value = {'message_id': 1}
        bot.send_document.return_value = {'message_id': 2}
        worker = SosService(bot, db, config).send_kit('1', db.get_user('1'))
        worker.join(timeout=2 * FAST)
        assert not worker.is_alive()
        assert [c.kwargs['filename'] for c in bot.send_document.call_args_list] == [
            'NekoVPN-emergency.yaml', 'NekoVPN-emergency.json']
        return bot

    def test_the_kit_provisions_a_paid_user(self, tmp_path, panel, clock):
        _srv, db = _server(tmp_path, panel.url)
        self._kit(db, _config(panel.url))
        assert panel.clients == {EMAIL: UUID}

    @pytest.mark.asyncio
    async def test_inside_the_window_the_kit_does_not_wait(self, tmp_path, hanging, clock,
                                                          monkeypatch):
        monkeypatch.setattr(fallback_node, '_PANEL_TIMEOUT_S', 0.5)
        srv, db = _server(tmp_path, hanging.url)
        await _sub(srv)
        await _flush(srv)                 # /sub found the panel silent
        assert hanging.accepted == 1
        monkeypatch.setattr(fallback_node, '_PANEL_TIMEOUT_S', 15)
        started = time.perf_counter()
        self._kit(db, _config(hanging.url))
        assert time.perf_counter() - started < FAST
        assert hanging.accepted == 1
