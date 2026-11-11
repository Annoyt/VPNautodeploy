"""A token the bot never minted is a refusal, never a 500.

``hmac.compare_digest`` raises TypeError on a str with a non-ASCII
character, and three token checks compared str: the exit report's token
(``/api/ssh/users``, tested in test_protocols_fingerprints.py), the /sub
token (``/sub``, ``/open``, ``/app`` and the app's SOS) and the dashboard's
``admin_token``. A percent-encoded non-ASCII token reaches the handlers
(raw non-ASCII bytes do not: aiohttp's parser answers 400 first), so the
requests here are raw bytes through aiohttp's own parser and router.
"""

import asyncio
import base64
import time
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestServer

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.utils.admin_token import make_admin_token

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning",
)

BOT_TOKEN = 'bot-token'
ADMIN = '42'
CYRILLIC = b'%D1%82' * 32               # 'т' * 32 once decoded: 32 characters
UNKNOWN = b'0123456789abcdef' * 2       # a token's shape, nobody's token


@pytest.fixture
def srv(tmp_path):
    db = Database(str(tmp_path / 'bot.db'))
    with db._connect() as conn:
        # compare_digest only ran (and raised) with someone to compare to
        conn.execute("INSERT INTO users (chat_id, username, status, uuid) "
                     "VALUES ('1', 'a', 'paid', '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70')")
        conn.commit()
    config = Mock()
    config.BOT_TOKEN = BOT_TOKEN
    config.is_admin = lambda uid: str(uid) == ADMIN
    return WebAppServer(config, db, xui_service=Mock())


async def _send(srv, method: bytes, target: bytes):
    async with TestServer(srv.app) as server:
        reader, writer = await asyncio.open_connection(server.host, server.port)
        body = b'{}' if method == b'POST' else b''
        writer.write(method + b' ' + target + b' HTTP/1.1\r\nHost: test\r\n'
                     b'Content-Type: application/json\r\n'
                     b'Content-Length: ' + str(len(body)).encode() + b'\r\n'
                     b'Connection: close\r\n\r\n' + body)
        await writer.drain()
        answer = await reader.read()
        writer.close()
    head, _, rest = answer.partition(b'\r\n\r\n')
    return int(head.split()[1]), rest


@pytest.mark.asyncio
@pytest.mark.parametrize('method,prefix', [
    (b'GET', b'/sub/'), (b'GET', b'/open/'), (b'GET', b'/app/'), (b'POST', b'/api/neko/sos/'),
])
@pytest.mark.parametrize('token', [CYRILLIC, UNKNOWN], ids=['non-ascii', 'unknown-hex'])
async def test_a_token_page_answers_404(srv, method, prefix, token):
    status, body = await _send(srv, method, prefix + token)
    # the handler's own 404 (the router's own reads "404: Not Found")
    assert (status, body) == (404, b'Not found')


def _forge(text: str) -> bytes:
    """A token the way make_admin_token encodes one, around any text."""
    return base64.urlsafe_b64encode(text.encode()).rstrip(b'=')


@pytest.mark.asyncio
@pytest.mark.parametrize('token,status', [
    (lambda: make_admin_token(BOT_TOKEN, ADMIN).encode(), 200),   # the request itself is fine
    (lambda: _forge(f'{ADMIN}:{int(time.time()) + 600}:тук'), 401),
    (lambda: _forge(f'{ADMIN}:{int(time.time()) + 600}:' + 'é' * 64), 401),
], ids=['valid', 'non-ascii-mac', 'non-ascii-mac-of-hex-length'])
async def test_a_dashboard_token_answers_401(srv, token, status):
    got, _ = await _send(srv, b'GET', b'/api/admin/client_health?admin_token=' + token())
    assert got == status
