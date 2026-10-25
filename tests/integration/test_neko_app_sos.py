"""The NekoVPN app's SOS button (client plan E16/E23): POST /api/neko/sos/<token>.

The app sends what it measured; the bot reports it like /sos (the same
user_failure_reports row shape, the same topic post, DPIMonitor's R5 counts
it) and answers with what the app shows: the status lines of the SOS letter
and the FlClash emergency profile. Level 2: real sqlite, real profile
builders; Telegram and the panel read are faked.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import bot.services.sos as sos
from bot.core.database import Database
from bot.core.web_server import NEKO_SOS_MAX_BODY, WebAppServer
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
from bot.models.user import User
from bot.services.sos import SosService, app_report_lines
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

CID = '52291265'
UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
GROUP, TOPIC_SUPPORT = -1001234, 17

REPORT = {
    'app': '0.9.2-neko.2', 'platform': 'android', 'network': 'wifi',
    'vpn_running': True, 'profile_age_min': 42, 'settings_version': 1,
    'delays': [{'name': 'u-reality', 'ms': 120}, {'name': 'u-hy2', 'ms': None}],
}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_config(**over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL='https://dash.example.com',
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='4bRlZyNH8VVqnRd3w6M_sYC6a1VKyXUvt5BBvXmnmHE',
        SNI_VALUE='www.bing.com', SID_VALUE='0123456789abcdef',
        HY2_HOST='203.0.113.20', HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='443,20000:40000', HY2T_PORT='',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api/v1/forecast',
        WS_SNI='cdn.example.com',
        STLS_HOST='203.0.113.20', STLS_PORT=443, STLS_SNI='www.microsoft.com',
        STLS_VERSION=3, STLS_PASSWORD='stls-pw',
        SS_METHOD='2022-blake3-aes-128-gcm',
        SS_SERVER_PASSWORD='c3J2LXB3LTE2Ynl0ZXMhIQ==', SS_USER_SALT='salt',
        FORUM_GROUP_ID=GROUP, TOPIC_SUPPORT=TOPIC_SUPPORT, TOPIC_AI=55,
        FORUM_ENABLED=True, SUPER_ADMIN_ID='1',
        AGENT_BACKEND='hermes', HERMES_URL='', DB_PATH='',
    )
    cfg.update(over)
    ns = SimpleNamespace(**cfg)
    ns.is_admin = lambda user_id: str(user_id) == '1'
    return ns


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    MK._panel_skip_until = 0.0
    sos._agent = None
    seen = utcnow() - timedelta(minutes=12)
    ms = int(seen.replace(tzinfo=timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(MK, '_fetch_panel_last_online_ms', lambda self, email: ms)
    yield
    MK._panel_skip_until = 0.0
    sos._agent = None


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def config():
    return make_config()


@pytest.fixture
def bot():
    b = Mock()
    b.send_message.return_value = {'message_id': 1}
    return b


def add_user(db, *, status='paid', uuid=UUID, lang='ru'):
    db._users.save(User(
        chat_id=CID, username='ziriki', status=status, uuid=uuid,
        email=f'user_ziriki_{CID}@nekovo.ru' if uuid else None,
        lang=lang, platform='android', last_asn='AS31133', last_country='RU',
    ))
    return db.get_user(CID)


def report_rows(db):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT chat_id, target_domain FROM user_failure_reports").fetchall()]


def topic_posts(bot):
    return [c.kwargs for c in bot.send_message.call_args_list
            if c.kwargs.get('chat_id') == GROUP
            and c.kwargs.get('message_thread_id') == TOPIC_SUPPORT]


class TestHandleAppSos:

    def test_reports_like_sos_and_answers_the_app(self, db, config, bot):
        user = add_user(db)
        answer = SosService(bot, db, config).handle_app_sos(user, REPORT)
        assert answer['status'] == 'reported' and answer['report_id']
        base = SubscriptionService(config).build_subscription_url(user)
        assert answer['emergency'] == f'{base}?format=clash&emergency=1'
        text = '\n'.join(answer['lines'])
        assert '(шаг 2)' not in text and '<' not in text
        assert report_rows(db) == [(CID, 'sos_app')]
        post = topic_posts(bot)[0]['text']
        assert f"SOS #{answer['report_id']}</b> · приложение NekoVPN" in post
        assert '📱 Приложение: NekoVPN 0.9.2-neko.2, android, сеть wifi' in post
        assert 'Задержки в приложении: u-reality 120 мс, u-hy2 —' in post

    def test_a_repeat_gets_the_status_but_no_second_report(self, db, config, bot):
        user = add_user(db)
        svc = SosService(bot, db, config)
        svc.handle_app_sos(user, REPORT)
        again = svc.handle_app_sos(user, REPORT)
        assert again['status'] == 'repeat' and again['lines'] and again['emergency']
        assert 'report_id' not in again
        assert len(report_rows(db)) == 1 and len(topic_posts(bot)) == 1

    def test_the_window_is_ten_minutes(self, db, config, bot):
        user = add_user(db)
        svc = SosService(bot, db, config)
        svc.handle_app_sos(user, REPORT)
        later = utcnow() + timedelta(seconds=sos.SOS_RATE_LIMIT_S + 5)
        assert svc.handle_app_sos(user, REPORT, now=later)['status'] == 'reported'
        assert len(report_rows(db)) == 2

    def test_no_key_no_report(self, db, config, bot):
        user = add_user(db, uuid=None)
        answer = SosService(bot, db, config).handle_app_sos(user, REPORT)
        assert answer['status'] == 'no_key' and answer['lines']
        assert report_rows(db) == [] and topic_posts(bot) == []

    def test_unknown_network_line_without_the_sos_step(self, db, config, bot):
        user = add_user(db)
        user.last_asn = None
        db._users.save(user)
        lines = SosService(bot, db, config).handle_app_sos(user, {})['lines']
        assert any(line.startswith('📶 Вашу сеть мы пока не знаем — обновите подписку,')
                   for line in lines), lines

    def test_english_lines_for_an_english_user(self, db, config, bot):
        user = add_user(db, lang='en')
        answer = SosService(bot, db, config).handle_app_sos(user, {})
        assert any(line.startswith('Our servers') or 'server' in line
                   for line in answer['lines'])
        assert '(step 2)' not in '\n'.join(answer['lines'])


class TestAppReportLines:

    def test_only_known_fields_escaped_and_cut(self):
        lines = app_report_lines({
            'app': '<b>' + 'x' * 100, 'network': 'cellular', 'vpn_running': False,
            'platform': True,
            'profile_age_min': True, 'unknown': 'ignored',
            'delays': [{'name': '<script>', 'ms': 5}, {'name': 'bool', 'ms': True},
                       {'name': ' ', 'ms': 1}, 'junk', {'ms': 3}],
        })
        assert lines[0].startswith('📱 Приложение: NekoVPN &lt;b&gt;xxx')
        assert len(lines[0]) < 120
        assert 'VPN выключен' in lines[0] and 'сеть cellular' in lines[0]
        assert 'профиль обновлён' not in lines[0] and 'ignored' not in lines[0]
        assert 'True' not in lines[0]
        assert lines[1] == 'Задержки в приложении: &lt;script&gt; 5 мс, bool —'

    def test_at_most_thirty_delays(self):
        delays = [{'name': f'p{i}', 'ms': 10} for i in range(40)]
        line = app_report_lines({'delays': delays})[1]
        assert line.count(' мс') == sos.APP_REPORT_MAX_DELAYS

    def test_junk(self):
        assert app_report_lines('x') == []
        assert app_report_lines({}) == ['📱 Приложение: NekoVPN ?']


class TestEndpoint:

    @pytest.fixture
    def server(self, db, config, bot):
        add_user(db)
        srv = WebAppServer(config, db, xui_service=Mock(), bot_instance=bot)
        srv.xui.get_client_traffic = AsyncMock(return_value={})
        return srv

    def _post(self, server, body, token=None, length=True):
        req = Mock()
        req.match_info = {'token': token or server.subscription.derive_token(UUID)}
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        req.content_length = len(raw) if length else None
        raw = raw[:NEKO_SOS_MAX_BODY + 1]
        req.content = Mock()
        req.content.read = AsyncMock(return_value=raw)
        return asyncio.run(server.handle_neko_sos(req))

    def test_answers_with_the_status(self, server, db):
        resp = self._post(server, REPORT)
        assert resp.status == 200 and resp.headers['Cache-Control'] == 'no-store'
        answer = json.loads(resp.text)
        assert answer['status'] == 'reported' and answer['emergency']
        assert report_rows(db) == [(CID, 'sos_app')]

    def test_gates(self, server, db):
        assert self._post(server, REPORT, token='0' * 32).status == 404
        assert self._post(server, b'{not json').status == 400
        assert self._post(server, b'x' * (NEKO_SOS_MAX_BODY + 1)).status == 413
        big = json.dumps({'pad': 'x' * NEKO_SOS_MAX_BODY}).encode()
        assert self._post(server, big, length=False).status == 413
        user = db._users.get_by_id(CID)
        user.status = 'banned'
        db._users.save(user)
        assert self._post(server, REPORT).status == 410
        assert report_rows(db) == []

    def test_route(self, server):
        routes = {(r.method, r.resource.canonical) for r in server.app.router.routes()}
        assert ('POST', '/api/neko/sos/{token}') in routes


class TestStartSos:
    """``t.me/<bot>?start=sos`` — the app's link when it reached nothing."""

    def _start(self, db, config, bot, text):
        from bot.handlers.commands import CommandHandler
        handler = CommandHandler(bot, db, config)
        update = {'message': {'text': text, 'chat': {'id': int(CID), 'type': 'private'},
                              'from': {'id': int(CID), 'username': 'ziriki'}}}
        handler.handle_start(update, CID)

    def test_start_sos_runs_sos(self, db, config, bot, monkeypatch):
        add_user(db)
        calls = []
        monkeypatch.setattr(SosService, 'handle_sos',
                            lambda self, chat_id, user: calls.append(chat_id))
        self._start(db, config, bot, '/start sos')
        assert calls == [CID]

    def test_plain_start_does_not(self, db, config, bot, monkeypatch):
        add_user(db)
        calls = []
        monkeypatch.setattr(SosService, 'handle_sos',
                            lambda self, chat_id, user: calls.append(chat_id))
        self._start(db, config, bot, '/start')
        self._start(db, config, bot, '/start promo')
        assert calls == []

