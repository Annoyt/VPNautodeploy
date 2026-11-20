"""The dashboard's credentials never reach the access log (2026-11-20).

Until 2026-11-20 the dashboard sent ``admin_token`` and ``initData`` in the
URL of every call, and aiohttp's access log wrote each URL, and the page's
own ``/?admin_token=…`` as the Referer, into ``docker logs vpn-bot`` and
bot.log. Now the page sends them in ``Authorization: NekoAdmin …``; the URL
still opens the dashboard (the WebSocket, a page loaded before the change)
and the access log redacts it there, as it does the users' subscription
tokens in /sub, /open, /app, /probe and the SOS paths.

The real WebAppServer runs on the runner the bot serves it with
(``make_runner``), on 127.0.0.1 and a free port; what the access log wrote
is read once the runner has shut down, so every line is in.
"""

import logging
import socket
import time
from contextlib import asynccontextmanager
from unittest.mock import Mock
from urllib.parse import quote

import aiohttp
import pytest
from aiohttp import web

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.utils.access_log import REDACTED
from bot.utils.admin_token import make_admin_token

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning",
)

BOT_TOKEN = 'access-log-bot-token'
OLD_BOT_TOKEN = 'the-token-before-a-change'
ADMIN = '42'
STRANGER = '7'
SUB_TOKEN = '0123456789abcdef' * 2          # a /sub token's shape, nobody's
ADMIN_API = '/api/admin/client_health'      # 200 for an admin on an empty db


@pytest.fixture(autouse=True)
def fresh_admin_rate_limit(monkeypatch):
    """One admin limiter per process, 60 a minute per address: the suite's
    other requests from 127.0.0.1 must not leave these a 429."""
    from bot.utils import rate_limit
    monkeypatch.setattr(rate_limit, '_admin_rate_limiter', rate_limit.RateLimiter())


@pytest.fixture
def srv(tmp_path):
    config = Mock()
    config.BOT_TOKEN = BOT_TOKEN
    config.is_admin = lambda uid: str(uid) == ADMIN
    return WebAppServer(config, Database(str(tmp_path / 'bot.db')), xui_service=Mock())


@pytest.fixture
def access_log(caplog):
    """The access log's lines, each with every string the record holds."""
    caplog.set_level(logging.INFO, logger='aiohttp.access')

    def lines():
        out = []
        for r in caplog.records:
            if r.name == 'aiohttp.access':
                held = [r.getMessage()] + [str(v) for v in vars(r).values()]
                out.append((r.getMessage(), '\n'.join(held)))
        return out
    return lines


@asynccontextmanager
async def serve(server):
    runner = server.make_runner()
    await runner.setup()
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    site = web.SockSite(runner, sock)
    await site.start()
    try:
        async with aiohttp.ClientSession(f'http://127.0.0.1:{sock.getsockname()[1]}') as s:
            yield s
    finally:
        await runner.cleanup()


def nekoadmin(**creds):
    """The header the dashboard sends (authHeader in app.js)."""
    return {'Authorization': 'NekoAdmin ' + '&'.join(
        f'{k}={quote(v, safe="")}' for k, v in creds.items())}


async def status(session, path, **kw):
    async with session.get(path, **kw) as resp:
        await resp.read()
        return resp.status


def assert_not_logged(lines, *secrets):
    """No line, and nothing else its record holds, has any secret in any
    form a URL or a header gives it."""
    assert lines, 'the access log wrote nothing'
    forms = set()
    for s in secrets:
        forms |= {s, quote(s, safe=''), quote(s, safe='=&')}
    for message, held in lines:
        for form in forms:
            assert form not in held, f'{form[:12]}… reached the access log: {message}'


class TestTheHeader:

    @pytest.mark.asyncio
    async def test_the_admin_token_opens_the_dashboard(self, srv, access_log):
        token = make_admin_token(BOT_TOKEN, ADMIN)
        async with serve(srv) as s:
            assert await status(s, ADMIN_API, headers=nekoadmin(admin_token=token)) == 200
            # the scheme's name as any case; anything around it stripped
            assert await status(s, ADMIN_API, headers={
                'Authorization': f'  nekoADMIN   admin_token={token} '}) == 200
        assert_not_logged(access_log(), token)

    @pytest.mark.asyncio
    async def test_initdata_opens_it_for_an_admin_only(self, srv, access_log, signed_init_data):
        admin, stranger = signed_init_data(BOT_TOKEN, ADMIN), signed_init_data(BOT_TOKEN, STRANGER)
        async with serve(srv) as s:
            assert await status(s, ADMIN_API, headers=nekoadmin(initData=admin)) == 200
            assert await status(s, ADMIN_API, headers=nekoadmin(initData=stranger)) == 401
        assert_not_logged(access_log(), admin, stranger)

    @pytest.mark.asyncio
    async def test_initdata_opens_it_for_a_day(self, srv, signed_init_data):
        # a logged initData used to open the dashboard until the bot token
        # changed; since 2026-11-20 it is dead a day after Telegram signed it
        now = int(time.time())
        async with serve(srv) as s:
            for age_s, expected in ((3600, 200), (25 * 3600, 401)):
                init = signed_init_data(BOT_TOKEN, ADMIN, auth_date=now - age_s)
                assert await status(s, ADMIN_API, headers=nekoadmin(initData=init)) == expected
                assert await status(s, ADMIN_API, params={'initData': init}) == expected

    @pytest.mark.asyncio
    async def test_after_a_bot_token_change_the_admin_token_still_opens_it(
            self, srv, access_log, signed_init_data):
        # the Mini App's initData was signed with the old bot token; the page
        # holds the admin token the change handed out and sends both
        stale = signed_init_data(OLD_BOT_TOKEN, ADMIN)
        token = make_admin_token(BOT_TOKEN, ADMIN)
        async with serve(srv) as s:
            assert await status(s, ADMIN_API, headers=nekoadmin(initData=stale)) == 401
            assert await status(s, ADMIN_API,
                                headers=nekoadmin(initData=stale, admin_token=token)) == 200
        assert_not_logged(access_log(), stale, token)

    @pytest.mark.asyncio
    async def test_anything_else_is_refused(self, srv, access_log):
        token = make_admin_token(BOT_TOKEN, ADMIN)
        async with serve(srv) as s:
            assert await status(s, ADMIN_API) == 401
            assert await status(s, ADMIN_API, headers=nekoadmin(admin_token='garbage')) == 401
            assert await status(s, ADMIN_API, headers=nekoadmin(
                admin_token=make_admin_token(OLD_BOT_TOKEN, ADMIN))) == 401
            assert await status(s, ADMIN_API, headers=nekoadmin(
                admin_token=make_admin_token(BOT_TOKEN, STRANGER))) == 401
            # our credentials under another scheme are not ours
            assert await status(s, ADMIN_API, headers={
                'Authorization': f'Bearer admin_token={token}'}) == 401
            assert await status(s, ADMIN_API, headers={'Authorization': 'NekoAdmin'}) == 401

    @pytest.mark.asyncio
    async def test_the_header_wins_over_the_url(self, srv):
        token = make_admin_token(BOT_TOKEN, ADMIN)
        async with serve(srv) as s:
            assert await status(s, f'{ADMIN_API}?admin_token=garbage',
                                headers=nekoadmin(admin_token=token)) == 200
            assert await status(s, f'{ADMIN_API}?admin_token={token}',
                                headers=nekoadmin(admin_token='garbage')) == 401

    @pytest.mark.asyncio
    async def test_a_post_reads_it_too(self, srv, access_log):
        token = make_admin_token(BOT_TOKEN, ADMIN)
        async with serve(srv) as s:
            async with s.post('/api/admin/broadcast', headers=nekoadmin(admin_token=token),
                              json={'text': 'hi', 'audience': 'active', 'confirm': False}) as r:
                assert r.status == 200 and (await r.json())['preview'] is True
            async with s.post('/api/admin/broadcast', json={'text': 'hi'}) as r:
                assert r.status == 401
        assert_not_logged(access_log(), token)


class TestTheUrl:
    """A page loaded before 2026-11-20 and the WebSocket still send the URL."""

    @pytest.mark.asyncio
    async def test_it_still_opens_the_dashboard_and_is_redacted(
            self, srv, access_log, signed_init_data):
        token = make_admin_token(BOT_TOKEN, ADMIN)
        init = signed_init_data(BOT_TOKEN, ADMIN)
        async with serve(srv) as s:
            assert await status(s, ADMIN_API, params={'admin_token': token}) == 200
            assert await status(s, ADMIN_API, params={'initData': init, 'hours': '6'}) == 200
        lines = access_log()
        assert_not_logged(lines, token, init)
        messages = [m for m, _ in lines]
        assert any(f'"GET {ADMIN_API}?admin_token={REDACTED} HTTP/1.1" 200' in m for m in messages)
        assert any(f'"GET {ADMIN_API}?initData={REDACTED}&hours=6 HTTP/1.1" 200' in m
                   for m in messages)

    @pytest.mark.asyncio
    async def test_the_websocket_connects_and_is_redacted(self, srv, access_log, signed_init_data):
        token = make_admin_token(BOT_TOKEN, ADMIN)
        init = signed_init_data(BOT_TOKEN, ADMIN)
        async with serve(srv) as s:
            # what connectWebSocket in app.js opens
            ws = await s.ws_connect('/api/admin/ws', params={'initData': init, 'admin_token': token})
            await ws.close()
            with pytest.raises(aiohttp.WSServerHandshakeError) as refused:
                await s.ws_connect('/api/admin/ws', params={'admin_token': 'garbage'})
            assert refused.value.status == 401
        lines = access_log()
        assert_not_logged(lines, token, init)
        assert any(f'/api/admin/ws?initData={REDACTED}&admin_token={REDACTED} HTTP/1.1" 101' in m
                   for m, _ in lines)


class TestOtherSecrets:

    @pytest.mark.asyncio
    async def test_the_referer_is_redacted(self, srv, access_log):
        token = make_admin_token(BOT_TOKEN, ADMIN)
        async with serve(srv) as s:
            # what a page opened at /?admin_token=… sent before strict-origin
            assert await status(s, ADMIN_API, headers={
                **nekoadmin(admin_token=token),
                'Referer': f'https://dash.example:9443/?admin_token={token}'}) == 200
        lines = access_log()
        assert_not_logged(lines, token)
        assert any(f'"https://dash.example:9443/?admin_token={REDACTED}"' in m for m, _ in lines)

    @pytest.mark.asyncio
    @pytest.mark.parametrize('method,path,logged', [
        ('GET', f'/sub/{SUB_TOKEN}?format=clash', f'/sub/{REDACTED}?format=clash'),
        ('GET', f'/open/{SUB_TOKEN}', f'/open/{REDACTED}'),
        ('GET', f'/app/{SUB_TOKEN}', f'/app/{REDACTED}'),
        ('GET', f'/probe/{SUB_TOKEN}/vpn', f'/probe/{REDACTED}/vpn'),
        ('POST', f'/api/neko/sos/{SUB_TOKEN}', f'/api/neko/sos/{REDACTED}'),
    ])
    async def test_a_subscription_token_in_the_path_is_redacted(
            self, srv, access_log, method, path, logged):
        async with serve(srv) as s:
            async with s.request(method, path) as resp:
                await resp.read()
        lines = access_log()
        assert_not_logged(lines, SUB_TOKEN)
        assert any(f'"{method} {logged} HTTP/1.1"' in m for m, _ in lines)

    @pytest.mark.asyncio
    async def test_the_dashboard_page_gives_out_its_origin_alone(self, srv):
        async with serve(srv) as s:
            async with s.get('/', params={'admin_token': 'x'}) as resp:
                assert resp.status == 200
                assert resp.headers['Referrer-Policy'] == 'strict-origin'
