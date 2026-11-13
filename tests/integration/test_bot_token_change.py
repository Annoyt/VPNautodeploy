"""Settings → «Токен бота»: a new bot token from the dashboard (2026-11-11).

Real sqlite, real ``Settings`` built from the environment the way the
container builds it, the real WebAppServer and its routes; only getMe and
the restart are faked. A "restart" is a new ``Process`` on the same data
directory with the same environment (.env does not change on its own).

The regression it is built around: a /sub link was HMAC(BOT_TOKEN, uuid),
so a new token would have changed every user's link at once.
"""

import hashlib
import hmac
import json
import logging
import os
import signal
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.config import Settings
from bot.config import secret_store as ss
from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import bot_token as bt
from bot.utils import token_envelope as te
from bot.utils.admin_token import make_admin_token, verify_admin_token

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

BOT_ID = 7012345678
OLD = f'{BOT_ID}:AAE' + 'o' * 32
NEW = f'{BOT_ID}:AAE' + 'n' * 32
NEWER = f'{BOT_ID}:AAE' + 'w' * 32
OTHER = '7099999999:AAE' + 'x' * 32
ADMIN = '1652899'
UUIDS = ('11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222')


class FakeTelegram:
    """getMe: a well-formed token is a bot named nekotest_bot, unless told."""

    def __init__(self):
        self.answers = {}
        self.calls = []
        self.kwargs = []

    def __call__(self, token, **kw):
        self.calls.append(token)
        self.kwargs.append(kw)
        if token in self.answers:
            return self.answers[token]
        return bt.TokenCheck('ok', {'id': ss.bot_id_of(token), 'username': 'nekotest_bot',
                                    'first_name': 'Neko'})


@pytest.fixture
def telegram():
    return FakeTelegram()


@pytest.fixture(autouse=True)
def fresh_admin_rate_limit(monkeypatch):
    """The admin limiter is one per process, 60 a minute per address, and
    every request here comes from 127.0.0.1: without a fresh one the file's
    own requests add up and a late test gets 429."""
    from bot.utils import rate_limit
    monkeypatch.setattr(rate_limit, '_admin_rate_limiter', rate_limit.RateLimiter())


@pytest.fixture
def data(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    data.mkdir()
    for key, value in {
        'BOT_TOKEN': OLD, 'DB_PATH': str(data / 'bot.db'), 'SUPER_ADMIN_ID': ADMIN,
        'WEBAPP_URL': 'https://dash.example:9443/', 'FORUM_GROUP_ID': '-100777',
        'TOPIC_AI': '55',
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('TG_PROXY_URLS', raising=False)
    return data


def secrets_file(data, name):
    return data / 'secrets' / name


class Process:
    """One start of the bot: Settings, on_startup, the web server."""

    def __init__(self, telegram):
        self.config = Settings()
        bt.on_startup(self.config)
        self.db = Database(self.config.DB_PATH)
        self.server = WebAppServer(self.config, self.db, xui_service=Mock())
        self.restarts = []
        self.server.bot_token = bt.BotTokenChanger(
            self.config, self.db, check=telegram, restart=self.restarts.append)

    def link(self, admin=ADMIN):
        return make_admin_token(self.config.BOT_TOKEN, admin)

    async def request(self, method, path, *, auth=None, json_body=None, data=None, params=None):
        params = dict(params or {})
        if auth:
            params['admin_token'] = auth
        async with TestClient(TestServer(self.server.app)) as client:
            resp = await client.request(method, path, params=params, json=json_body, data=data)
            text = await resp.text()
        try:
            body = json.loads(text)
        except ValueError:
            body = None
        return resp.status, body, text

    async def challenge(self, auth=None):
        status, ch, _ = await self.request('POST', '/api/admin/bot_token/challenge',
                                           auth=auth or self.link())
        assert status == 200, ch
        return ch

    async def change(self, new_token, auth=None):
        auth = auth or self.link()
        env = te.seal(await self.challenge(auth), new_token.encode())
        return await self.request('POST', '/api/admin/bot_token/apply', auth=auth, json_body=env)


def seed_users(db):
    for i, uuid in enumerate(UUIDS):
        db.save_user(User(chat_id=str(100 + i), username=f'u{i}', uuid=uuid,
                          email=f'user_u{i}@nekovo.ru', status='paid'))


def old_style_link(uuid):
    return hmac.new(OLD.encode(), uuid.encode(), hashlib.sha256).hexdigest()[:32]


# ---------------------------------------------------------------- the point

class TestLinksSurvive:

    @pytest.mark.asyncio
    async def test_a_new_token_changes_no_subscription_link(self, data, telegram):
        first = Process(telegram)
        seed_users(first.db)
        links = {u: first.server.subscription.derive_token(u) for u in UUIDS}
        # pinned at the first start: exactly the links users already hold
        assert links == {u: old_style_link(u) for u in UUIDS}

        status, body, _ = await first.change(NEW)
        assert status == 200, body

        second = Process(telegram)  # the restart; .env still holds OLD
        assert (second.config.BOT_TOKEN, second.config.BOT_TOKEN_SOURCE) == (NEW, 'dashboard')
        assert {u: second.server.subscription.derive_token(u) for u in UUIDS} == links
        found = second.server.subscription.find_user_by_token(second.db, links[UUIDS[1]])
        assert found is not None and found.uuid == UUIDS[1]

    @pytest.mark.asyncio
    async def test_the_dashboard_keeps_working_across_the_restart(self, data, telegram):
        first = Process(telegram)
        status, body, _ = await first.change(NEW)
        fresh = body['admin_token']
        assert verify_admin_token(NEW, fresh) == ADMIN
        assert verify_admin_token(OLD, fresh) is None
        # the old process does not take the new link yet ...
        assert (await first.request('GET', '/api/admin/bot_token', auth=fresh))[0] == 401
        second = Process(telegram)
        # ... the new one does, and no longer takes the old link
        status, view, _ = await second.request('GET', '/api/admin/bot_token', auth=fresh)
        assert status == 200 and view['source'] == 'dashboard' and not view['restarting']
        assert view['changed_by'] == ADMIN and view['changed_at']
        old_link = make_admin_token(OLD, ADMIN)
        assert (await second.request('GET', '/api/admin/bot_token', auth=old_link))[0] == 401

    def test_rotating_dot_env_by_hand_keeps_the_links_too(self, data, telegram, monkeypatch):
        """The same bear trap the ssh way: edit BOT_TOKEN in .env, recreate."""
        first = Process(telegram)
        links = {u: first.server.subscription.derive_token(u) for u in UUIDS}
        monkeypatch.setenv('BOT_TOKEN', NEW)
        second = Process(telegram)
        assert (second.config.BOT_TOKEN, second.config.BOT_TOKEN_SOURCE) == (NEW, 'env')
        assert {u: second.server.subscription.derive_token(u) for u in UUIDS} == links


# ------------------------------------------------------------ the change

class TestChange:

    @pytest.mark.asyncio
    async def test_saves_audits_and_restarts_once(self, data, telegram, caplog):
        caplog.set_level(logging.DEBUG)
        p = Process(telegram)
        status, body, text = await p.change(NEW)
        assert status == 200
        assert body['ok'] and body['bot'] == {'id': BOT_ID, 'username': 'nekotest_bot'}
        assert NEW not in text and OLD not in text

        path = secrets_file(data, 'bot_token.json')
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(data / 'secrets').st_mode) == 0o700
        rec = json.loads(path.read_text())
        assert rec['token'] == NEW and rec['bot_id'] == BOT_ID and rec['set_by'] == ADMIN
        assert rec['env_sha256'] == ss.token_sha256(OLD) and rec['announce'] is True
        assert p.restarts == [bt.RESTART_DELAY_S] and bt.RESTART_DELAY_S >= 1

        with p.db._connect() as conn:
            rows = conn.execute("SELECT admin_id, action, target_id, details FROM admin_actions "
                                "WHERE action = 'bot_token_set'").fetchall()
        assert [tuple(r[:3]) for r in rows] == [(ADMIN, 'bot_token_set', f'bot:{BOT_ID}')]
        assert NEW not in rows[0][3] and OLD not in rows[0][3]

        # one change per process: the next waits for the restart
        status, body, _ = await p.change(NEWER)
        assert status == 409 and body['error'] == 'restarting'
        assert p.restarts == [bt.RESTART_DELAY_S] and telegram.calls == [NEW]
        assert json.loads(path.read_text())['token'] == NEW

        leaked = [r.getMessage() for r in caplog.records
                  if NEW in r.getMessage() or OLD in r.getMessage()]
        assert leaked == []

    @pytest.mark.parametrize('token, answer, code, error, asked', [
        (OTHER, None, 409, 'other_bot', False),
        ('not-a-token', None, 400, 'format', False),
        (f'{BOT_ID}:short', None, 400, 'format', False),
        (OLD, None, 400, 'same', False),
        (NEW, bt.TokenCheck('rejected', detail='Unauthorized'), 400, 'rejected', True),
        (NEW, bt.TokenCheck('unreachable', detail='ConnectTimeout через exit:8888'),
         503, 'unreachable', True),
        (NEW, bt.TokenCheck('ok', {'id': 7099999999, 'username': 'x'}), 409, 'other_bot', True),
    ])
    @pytest.mark.asyncio
    async def test_refused_changes_change_nothing(self, data, telegram, token, answer,
                                                  code, error, asked):
        p = Process(telegram)
        if answer is not None:
            telegram.answers[token] = answer
        status, body, text = await p.change(token)
        assert (status, body['error']) == (code, error) and body['message']
        assert token not in text
        assert not secrets_file(data, 'bot_token.json').exists() and p.restarts == []
        assert telegram.calls == ([token] if asked else [])
        assert Process(telegram).config.BOT_TOKEN == OLD

    @pytest.mark.asyncio
    async def test_a_pasted_token_with_spaces_around_it(self, data, telegram):
        p = Process(telegram)
        status, body, _ = await p.change(f'  {NEW}\n')
        assert status == 200, body
        assert json.loads(secrets_file(data, 'bot_token.json').read_text())['token'] == NEW

    @pytest.mark.asyncio
    async def test_a_tampered_or_replayed_envelope(self, data, telegram):
        p = Process(telegram)
        auth = p.link()
        env = te.seal(await p.challenge(auth), NEW.encode())
        bad = dict(env, tag=te.b64url(b'\x01' * 32))
        status, body, _ = await p.request('POST', '/api/admin/bot_token/apply', auth=auth, json_body=bad)
        assert (status, body['error']) == (400, 'envelope')
        status, body, _ = await p.request('POST', '/api/admin/bot_token/apply', auth=auth, json_body=env)
        assert (status, body['error']) == (400, 'envelope')   # its challenge is spent
        assert telegram.calls == [] and not secrets_file(data, 'bot_token.json').exists()

    @pytest.mark.asyncio
    async def test_admins_only(self, data, telegram):
        p = Process(telegram)
        env = te.seal(await p.challenge(), NEW.encode())
        for auth in (None, 'garbage', make_admin_token(OLD, '42'), make_admin_token(NEW, ADMIN)):
            assert (await p.request('GET', '/api/admin/bot_token', auth=auth))[0] == 401
            assert (await p.request('POST', '/api/admin/bot_token/challenge', auth=auth))[0] == 401
            status, _, _ = await p.request('POST', '/api/admin/bot_token/apply', auth=auth,
                                           json_body=env)
            assert status == 401
        assert telegram.calls == [] and not secrets_file(data, 'bot_token.json').exists()

    @pytest.mark.asyncio
    async def test_body_limits(self, data, telegram):
        p = Process(telegram)
        auth = p.link()
        status, body, _ = await p.request('POST', '/api/admin/bot_token/apply', auth=auth,
                                          data='x' * 5000)
        assert status == 413
        status, body, _ = await p.request('POST', '/api/admin/bot_token/apply', auth=auth,
                                          data='{not json')
        assert (status, body['error']) == (400, 'invalid json')
        status, body, _ = await p.request('POST', '/api/admin/bot_token/apply', auth=auth,
                                          json_body=['a list'])
        assert (status, body['error']) == (400, 'envelope')


class TestSubscriptionSecretGuard:

    @pytest.mark.asyncio
    async def test_unreadable_secret_blocks_the_change(self, data, telegram, caplog):
        (data / 'secrets').mkdir(mode=0o700)
        secrets_file(data, 'sub_token_secret.json').write_text('{broken')
        p = Process(telegram)
        status, body, _ = await p.change(NEW)
        assert (status, body['error']) == (500, 'pin')
        assert secrets_file(data, 'sub_token_secret.json').read_text() == '{broken'
        assert not secrets_file(data, 'bot_token.json').exists() and p.restarts == []

    @pytest.mark.asyncio
    async def test_missing_secret_after_an_earlier_change_blocks_it(self, data, telegram):
        (data / 'secrets').mkdir(mode=0o700)
        secrets_file(data, 'bot_token.superseded-20261101-000000.json').write_text('{}')
        p = Process(telegram)
        assert not secrets_file(data, 'sub_token_secret.json').exists()
        status, body, _ = await p.change(NEW)
        assert (status, body['error']) == (500, 'pin')
        assert not secrets_file(data, 'bot_token.json').exists()

    @pytest.mark.asyncio
    async def test_a_secret_lost_while_running_on_dot_env_is_pinned_at_the_change(self, data, telegram):
        p = Process(telegram)
        links = {u: p.server.subscription.derive_token(u) for u in UUIDS}
        secrets_file(data, 'sub_token_secret.json').unlink()
        p.config.SUB_TOKEN_SECRET = ''
        status, body, _ = await p.change(NEW)
        assert status == 200, body
        second = Process(telegram)
        assert {u: second.server.subscription.derive_token(u) for u in UUIDS} == links


# ------------------------------------------------------------ the status

class TestStatus:

    @pytest.mark.asyncio
    async def test_says_which_bot_and_never_the_token(self, data, telegram):
        p = Process(telegram)
        status, view, text = await p.request('GET', '/api/admin/bot_token', auth=p.link())
        assert status == 200 and OLD not in text
        assert view == {
            'bot_id': BOT_ID, 'username': 'nekotest_bot', 'token_status': 'ok',
            'token_detail': '', 'source': 'env', 'changed_at': None, 'changed_by': None,
            'sub_secret_pinned': True, 'plain_hop': False, 'restarting': False,
        }

    @pytest.mark.asyncio
    async def test_a_revoked_token_shows(self, data, telegram):
        p = Process(telegram)
        telegram.answers[OLD] = bt.TokenCheck('rejected', detail='Unauthorized')
        _, view, _ = await p.request('GET', '/api/admin/bot_token', auth=p.link())
        assert (view['token_status'], view['token_detail'], view['username']) == (
            'rejected', 'Unauthorized', None)

    @pytest.mark.asyncio
    async def test_getme_is_cached(self, data, telegram):
        p = Process(telegram)
        for _ in range(3):
            await p.request('GET', '/api/admin/bot_token', auth=p.link())
        assert telegram.calls == [OLD]

    @pytest.mark.asyncio
    async def test_the_status_asks_once_and_the_change_retries(self, data, telegram):
        """The status shares asyncio's default pool with /sub: a dead
        Telegram must not hold a thread for every retry. The change keeps
        check_token's retries."""
        p = Process(telegram)
        await p.request('GET', '/api/admin/bot_token', auth=p.link())
        status, _, _ = await p.change(NEW)
        assert status == 200
        assert telegram.calls == [OLD, NEW]
        assert telegram.kwargs == [{'attempts': 1}, {}]

    @pytest.mark.asyncio
    async def test_link_for_a_mini_app_survives_the_revoke(self, data, telegram):
        p = Process(telegram)
        _, view, _ = await p.request('GET', '/api/admin/bot_token', auth=p.link(),
                                     params={'link': '1'})
        prefix = 'https://dash.example:9443/?admin_token='
        assert view['link'].startswith(prefix)
        assert verify_admin_token(OLD, view['link'][len(prefix):]) == ADMIN

    @pytest.mark.parametrize('remote, secure, plain', [
        ('84.75.76.109', False, True), ('127.0.0.1', False, False), ('::1', False, False),
        ('10.0.0.5', False, True), ('172.20.0.1', False, True),   # only loopback is "here"
        ('84.75.76.109', True, False), (None, False, True),
    ])
    def test_plain_hop(self, remote, secure, plain):
        req = SimpleNamespace(remote=remote, secure=secure)
        assert WebAppServer._plain_hop(req) is plain


# ------------------------------------------------------------ startup

class TestStartup:

    def test_first_start_pins_the_key_from_dot_env(self, data, telegram):
        p = Process(telegram)
        path = secrets_file(data, 'sub_token_secret.json')
        assert json.loads(path.read_text())['secret'] == OLD
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert p.config.SUB_TOKEN_SECRET == OLD

    def test_pinned_once_never_rewritten(self, data, telegram, monkeypatch):
        Process(telegram)
        path = secrets_file(data, 'sub_token_secret.json')
        before = path.read_bytes()
        monkeypatch.setenv('BOT_TOKEN', NEW)
        Process(telegram)
        assert path.read_bytes() == before

    @pytest.mark.asyncio
    async def test_dot_env_edited_after_the_dashboard_wins(self, data, telegram, monkeypatch):
        first = Process(telegram)
        await first.change(NEW)
        monkeypatch.setenv('BOT_TOKEN', NEWER)       # someone edited .env since
        second = Process(telegram)
        assert (second.config.BOT_TOKEN, second.config.BOT_TOKEN_SOURCE) == (NEWER, 'env')
        assert not secrets_file(data, 'bot_token.json').exists()
        moved = list((data / 'secrets').glob('bot_token.superseded-*.json'))
        assert len(moved) == 1 and json.loads(moved[0].read_text())['token'] == NEW
        assert json.loads(secrets_file(data, 'sub_token_secret.json').read_text())['secret'] == OLD
        bot = Mock()
        assert bt.announce_if_pending(bot, second.config) is False
        bot.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dot_env_synced_to_the_new_token(self, data, telegram, monkeypatch):
        first = Process(telegram)
        await first.change(NEW)
        monkeypatch.setenv('BOT_TOKEN', NEW)         # the advice: put it in .env too
        second = Process(telegram)
        assert (second.config.BOT_TOKEN, second.config.BOT_TOKEN_SOURCE) == (NEW, 'env')

    @pytest.mark.asyncio
    async def test_a_second_change_before_dot_env_is_synced(self, data, telegram):
        first = Process(telegram)
        await first.change(NEW)
        second = Process(telegram)
        status, body, _ = await second.change(NEWER)
        assert status == 200, body
        third = Process(telegram)
        assert (third.config.BOT_TOKEN, third.config.BOT_TOKEN_SOURCE) == (NEWER, 'dashboard')

    @pytest.mark.parametrize('content', [
        '{broken', '[]', json.dumps({'token': 'nope', 'env_sha256': ss.token_sha256(OLD)}),
        json.dumps({'token': NEW, 'env_sha256': 'x'}),
    ])
    def test_a_malformed_record_means_dot_env(self, data, telegram, content):
        (data / 'secrets').mkdir(mode=0o700)
        secrets_file(data, 'bot_token.json').write_text(content)
        p = Process(telegram)
        assert (p.config.BOT_TOKEN, p.config.BOT_TOKEN_SOURCE) == (OLD, 'env')

    def test_broken_key_is_never_overwritten(self, data, telegram):
        (data / 'secrets').mkdir(mode=0o700)
        secrets_file(data, 'sub_token_secret.json').write_text('{"secret": ""}')
        p = Process(telegram)
        assert secrets_file(data, 'sub_token_secret.json').read_text() == '{"secret": ""}'
        assert p.config.SUB_TOKEN_SECRET == ''

    def test_derive_token_on_a_mock_config_is_the_old_formula(self):
        """Tests and callers that build a Mock config keep their links."""
        from bot.services.subscription import SubscriptionService
        config = Mock()
        config.BOT_TOKEN = OLD
        assert SubscriptionService(config).derive_token(UUIDS[0]) == old_style_link(UUIDS[0])
        config.SUB_TOKEN_SECRET = 'pinned-key'
        assert SubscriptionService(config).derive_token(UUIDS[0]) == hmac.new(
            b'pinned-key', UUIDS[0].encode(), hashlib.sha256).hexdigest()[:32]


# ------------------------------------------------------------ after the restart

class TestAnnouncement:

    @pytest.mark.asyncio
    async def test_once_in_the_admin_topic(self, data, telegram):
        first = Process(telegram)
        await first.change(NEW)
        second = Process(telegram)
        bot = Mock()
        bot.send_message.return_value = {'message_id': 1}
        assert bt.announce_if_pending(bot, second.config) is True
        kwargs = bot.send_message.call_args.kwargs
        assert (kwargs['chat_id'], kwargs['message_thread_id'], kwargs['parse_mode']) == (
            '-100777', 55, 'HTML')
        assert NEW not in kwargs['text'] and OLD not in kwargs['text']
        assert '@nekotest_bot' in kwargs['text'] and 'exit' in kwargs['text']
        rec = json.loads(secrets_file(data, 'bot_token.json').read_text())
        assert rec['announce'] is False and rec['token'] == NEW
        assert bt.announce_if_pending(bot, second.config) is False
        assert bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_pm_only_when_the_topic_fails(self, data, telegram):
        first = Process(telegram)
        await first.change(NEW)
        second = Process(telegram)
        bot = Mock()
        bot.send_message.side_effect = [None, {'message_id': 2}]
        assert bt.announce_if_pending(bot, second.config) is True
        assert bot.send_message.call_args.kwargs['chat_id'] == ADMIN
        assert 'message_thread_id' not in bot.send_message.call_args.kwargs

    @pytest.mark.asyncio
    async def test_undelivered_stays_pending(self, data, telegram):
        first = Process(telegram)
        await first.change(NEW)
        second = Process(telegram)
        bot = Mock()
        bot.send_message.return_value = None
        assert bt.announce_if_pending(bot, second.config) is False
        assert json.loads(secrets_file(data, 'bot_token.json').read_text())['announce'] is True


# ------------------------------------------------------------ restart in place

class TestRestart:

    def test_reexec_runs_the_same_command(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bt.os, 'execv', lambda path, argv: calls.append((path, argv)))
        monkeypatch.setattr(bt.sys, 'orig_argv', ['python', '-m', 'bot.main'], raising=False)
        bt.cancel_restart()
        bt.reexec_if_requested()
        assert calls == []
        bt._RESTART['requested'] = True
        try:
            bt.reexec_if_requested()
        finally:
            bt.cancel_restart()
        assert calls == [(sys.executable, [sys.executable, '-m', 'bot.main'])]

    def test_sigterm_to_itself_after_the_delay(self, monkeypatch):
        kills = []
        monkeypatch.setattr(bt.os, 'kill', lambda pid, sig: kills.append((pid, sig)))
        monkeypatch.setattr(bt.os, 'execv', lambda *a: kills.append('exec'))
        monkeypatch.setattr(bt, 'RESTART_GRACE_S', 3600)
        try:
            bt.request_restart(0.5)
            time.sleep(0.15)
            assert kills == []                   # the answer gets out first
            deadline = time.monotonic() + 5
            while not kills and time.monotonic() < deadline:
                time.sleep(0.01)
            assert kills == [(os.getpid(), signal.SIGTERM)] and bt.restart_requested()
        finally:
            bt.cancel_restart()
        assert not bt.restart_requested()

    def test_a_cancelled_restart_never_execs(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bt.os, 'execv', lambda *a: calls.append(a))
        bt.cancel_restart()
        bt._force_exec()
        assert calls == []


# ------------------------------------------------------------ getMe

class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    script = []
    made = []

    def __init__(self):
        self.trust_env = True
        self.calls = []
        FakeSession.made.append(self)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = FakeSession.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        pass


@pytest.fixture
def session(monkeypatch):
    FakeSession.script = []
    FakeSession.made = []
    monkeypatch.setattr(bt.requests, 'Session', FakeSession)
    return FakeSession


class TestCheckToken:

    OK = {'ok': True, 'result': {'id': BOT_ID, 'is_bot': True, 'username': 'nekotest_bot',
                                 'first_name': 'Neko'}}

    def labels(self, session):
        return [(kw.get('proxies') or {}).get('https', 'env').split('@')[-1]
                for _, kw in session.made[0].calls]

    def test_a_slow_first_try_is_retried_on_the_same_proxy(self, session, monkeypatch):
        """2026-11-11: exit's tinyproxy hung once on IPv6, the next try went
        through. One try per proxy called the token unreachable."""
        import requests
        monkeypatch.setenv('TG_PROXY_URLS', 'http://u:p@exit:8888,http://u:p@de:8888')
        session.script = [requests.ReadTimeout('slow'), FakeResponse(200, self.OK)]
        assert bt.check_token(NEW).status == 'ok'
        assert self.labels(session) == ['exit:8888', 'exit:8888']

    def test_tries_the_pool_in_order(self, session, monkeypatch):
        import requests
        monkeypatch.setenv('TG_PROXY_URLS', 'http://u:p@exit:8888,http://u:p@de:8888')
        session.script = [requests.ConnectionError(f'https://api.telegram.org/bot{NEW}/getMe'),
                          requests.ConnectionError('again'),
                          FakeResponse(200, self.OK)]
        check = bt.check_token(NEW)
        assert check.status == 'ok' and check.bot['username'] == 'nekotest_bot'
        s = session.made[0]
        assert s.trust_env is False
        assert [kw['proxies'] for _, kw in s.calls] == [
            {'http': 'http://u:p@exit:8888', 'https': 'http://u:p@exit:8888'}] * 2 + [
            {'http': 'http://u:p@de:8888', 'https': 'http://u:p@de:8888'}]
        assert all(kw['timeout'] == bt.TOKEN_CHECK_TIMEOUT_S == 10 for _, kw in s.calls)

    @pytest.mark.parametrize('status', [401, 404])
    def test_telegram_says_no(self, session, monkeypatch, status):
        monkeypatch.setenv('TG_PROXY_URLS', 'http://exit:8888,http://de:8888')
        session.script = [FakeResponse(status, {'ok': False, 'description': 'Unauthorized'})]
        check = bt.check_token(NEW)
        assert (check.status, check.detail) == ('rejected', 'Unauthorized')
        assert len(session.made[0].calls) == 1    # an answer, not a proxy problem

    def test_unreachable_names_every_proxy_and_never_the_token(self, session, monkeypatch):
        import requests
        monkeypatch.setenv('TG_PROXY_URLS', 'http://u:secretpw@exit:8888,http://u:secretpw@de:8888')
        session.script = [requests.ReadTimeout(f'https://api.telegram.org/bot{NEW}/getMe'),
                          FakeResponse(429, {'ok': False, 'description': 'Too Many Requests'}),
                          requests.exceptions.ProxyError(f'https://api.telegram.org/bot{NEW}/getMe'),
                          requests.exceptions.ProxyError('again')]
        check = bt.check_token(NEW)
        assert check.status == 'unreachable'
        assert check.detail == 'Too Many Requests через exit:8888; ProxyError через de:8888'
        assert NEW not in check.detail and 'secretpw' not in check.detail
        assert self.labels(session) == ['exit:8888', 'exit:8888', 'de:8888', 'de:8888']

    def test_no_pool_means_the_environment_proxy(self, session, monkeypatch):
        monkeypatch.delenv('TG_PROXY_URLS', raising=False)
        session.script = [FakeResponse(200, self.OK)]
        assert bt.check_token(NEW).status == 'ok'
        s = session.made[0]
        assert s.trust_env is True and 'proxies' not in s.calls[0][1]

    def test_direct_entry(self, session, monkeypatch):
        monkeypatch.setenv('TG_PROXY_URLS', 'direct')
        session.script = [FakeResponse(502, ValueError('html')), FakeResponse(502, ValueError('html'))]
        check = bt.check_token(NEW)
        assert (check.status, check.detail) == ('unreachable', 'HTTP 502 через direct')
        assert [kw['proxies'] for _, kw in session.made[0].calls] == [{}, {}]
