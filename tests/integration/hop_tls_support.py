"""Shared by test_hop_tls.py and test_hop_tls_stand.py: the real WebAppServer
on a real sqlite with both listeners (AGENTS.md §53)."""

import asyncio
import os
import socket
import threading
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import web

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import hop_tls

REPO = Path(__file__).resolve().parents[2]
UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
PROBE_UUID = '1f2e3d4c-5b6a-4978-8a6b-5c4d3e2f1a00'
ENTRY_IP = '203.0.113.20'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def make_config(**over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL='',
        ENTRY_NODE_IP=ENTRY_IP, ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='reality-pbk', SNI_VALUE='www.bing.com',
        SID_VALUE='0123456789abcdef',
        HY2_HOST=ENTRY_IP, HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='', HY2T_PORT='',
        WS_HOST='', STLS_HOST='', SUB_MIRROR_URLS='',
        WEB_TLS_PORT=0, WEB_TLS_DIR='',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


class Server:
    """The real WebAppServer on a real sqlite, both listeners on 127.0.0.1."""

    def __init__(self, tmp_path, *, tls_dir=None, **config):
        self.db = Database(str(tmp_path / 'bot.db'))
        self.db._users.save(User(chat_id='42', username='u42', status='paid', uuid=UUID,
                                 email='u42@x', quota_gb=100.0, lang='ru'))
        self.db._users.save(User(chat_id=hop_tls.PROBE_CHAT_ID, username='probe_monitor',
                                 status='paid', uuid=PROBE_UUID, email='probe_monitor@x',
                                 quota_gb=1.0, lang='ru'))
        self.tls_dir = tls_dir or str(tmp_path / 'hop-tls')
        if tls_dir is None:
            hop_tls.generate(self.tls_dir, ENTRY_IP)
        self.cert = os.path.join(self.tls_dir, 'cert.pem')
        self.port, self.tls_port = free_port(), free_port()
        self.config = make_config(WEB_TLS_PORT=self.tls_port, WEB_TLS_DIR=self.tls_dir,
                                  DB_PATH=str(tmp_path / 'bot.db'), **config)
        self.config.is_admin = Mock(return_value=False)
        self.srv = WebAppServer(self.config, self.db, xui_service=Mock())
        self.srv.xui.get_client_traffic = AsyncMock(return_value={'upload': 1, 'download': 2})

        async def peer(request):
            return web.json_response({'remote': request.remote,
                                      'xff': request.headers.get('X-Forwarded-For'),
                                      'secure': request.secure})
        self.srv.app.router.add_get('/_peer', peer)
        self.loop = asyncio.new_event_loop()
        self.runner = web.AppRunner(self.srv.app)
        self.loop.run_until_complete(self.runner.setup())
        self.sites = self.loop.run_until_complete(self.srv.start_sites(
            self.runner, '127.0.0.1', self.port, hop_tls.listener(self.config)))
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def close(self):
        async def drain():
            # the hy2 callback's audit writes run after its answer
            while self.srv._bg_tasks:
                await asyncio.gather(*list(self.srv._bg_tasks), return_exceptions=True)
            await self.runner.cleanup()
        asyncio.run_coroutine_threadsafe(drain(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)

    def hop(self, path, **kw):
        return hop_tls._hop_get('127.0.0.1', self.tls_port, self.cert, path, **kw)

    def plain(self, path):
        with urllib.request.urlopen(f'http://127.0.0.1:{self.port}{path}', timeout=10) as r:
            return r.status, r.read()
