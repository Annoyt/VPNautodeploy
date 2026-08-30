"""In-process dashboard server for browser E2E tests.

Runs the REAL WebAppServer (real Database on a temp sqlite, real
StateMachine, real admin_token auth) on a random localhost port in a
daemon thread. Only the outer world is mocked: no Telegram bot, no
x-ui panel, no notification sends.
"""

import asyncio
import socket
import threading
import time
from unittest.mock import Mock

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.utils.admin_token import make_admin_token

E2E_BOT_TOKEN = 'e2e_test_bot_token'
E2E_ADMIN_ID = '1652899'


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class E2EStack:
    def __init__(self, db, base_url, token):
        self.db = db
        self.base_url = base_url
        self.token = token
        self._seq = 0

    def dashboard_url(self) -> str:
        return f"{self.base_url}/?admin_token={self.token}"

    def seed_user(self, status: str, **kw) -> str:
        """Create a uniquely-named user; returns its chat_id."""
        self._seq += 1
        chat_id = f"e2e_{status}_{self._seq}"
        self.db.save_user(User(
            chat_id=chat_id, username=chat_id, status=status,
            quota_gb=kw.pop('quota_gb', 10.0), **kw,
        ))
        return chat_id

    def wait_status(self, chat_id: str, expected: str, timeout: float = 6.0):
        """Poll the DB until the user reaches the expected status."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            u = self.db.get_user(chat_id)
            last = u.status if u else None
            if last == expected:
                return u
            time.sleep(0.2)
        raise AssertionError(
            f"{chat_id}: expected status {expected!r}, still {last!r} "
            f"after {timeout}s"
        )


@pytest.fixture(scope='session')
def e2e_stack(tmp_path_factory):
    db = Database(str(tmp_path_factory.mktemp('e2e') / 'bot.db'))

    config = Mock()
    config.BOT_TOKEN = E2E_BOT_TOKEN
    config.PAID_TRAFFIC_GB = 100
    config.is_admin = lambda uid: str(uid) == E2E_ADMIN_ID

    server = WebAppServer(config, db, xui_service=Mock())
    server.notification_service = Mock()

    port = _free_port()

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        from aiohttp import web
        runner = web.AppRunner(server.app)
        loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, '127.0.0.1', port)
        loop.run_until_complete(site.start())
        loop.run_forever()

    threading.Thread(target=_run, daemon=True, name='e2e-webapp').start()

    # Wait for the server to accept connections.
    import urllib.request
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{base}/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise RuntimeError('e2e web server did not start')

    token = make_admin_token(E2E_BOT_TOKEN, E2E_ADMIN_ID)
    return E2EStack(db, base, token)
