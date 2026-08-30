"""Dashboard user-action endpoint against a REAL SQLite database.

The 2026-08-30 stale-snapshot bug (reject silently rolled back to
pending_demo, ban/unban reverted for keyed users) was invisible to the
mock-based unit tests: a mock returns the same object everywhere, so
"saving a stale row" cannot happen by construction. These tests run
handle_user_action with a real Database + real StateMachine on a temp
sqlite file, and assert the FINAL persisted row — the same thing an
admin sees after pressing the button.

Panel (x-ui) stays mocked out: bot_instance is None, so every handler
branch takes the xui=None path (panel sync skipped) — this suite pins
DB semantics, not panel semantics.
"""

import json
from unittest.mock import Mock, patch

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def server(db):
    config = Mock()
    config.BOT_TOKEN = 'test_token'
    config.PAID_TRAFFIC_GB = 100
    srv = WebAppServer(config, db, xui_service=Mock())
    # Side effects require a notification service; its sends are noise here.
    srv.notification_service = Mock()
    return srv


def _request(chat_id, action, value=None):
    body = {'action': action}
    if value is not None:
        body['value'] = value

    async def _json():
        return body
    request = Mock()
    request.match_info = {'chat_id': chat_id}
    request.json = _json
    return request


async def _act(server, chat_id, action, value=None):
    with patch.object(
        server, '_validate_admin_with_rate_limit',
        return_value=({'id': 1652899}, None),
    ):
        return await server.handle_user_action(
            _request(chat_id, action, value)
        )


def _seed(db, chat_id, status, **kw):
    db.save_user(User(chat_id=chat_id, username=chat_id, status=status,
                      quota_gb=kw.pop('quota_gb', 10.0), **kw))


class TestUserActionsRealDb:

    @pytest.mark.asyncio
    async def test_approve_pending_demo(self, server, db):
        _seed(db, 'u1', 'pending_demo')
        resp = await _act(server, 'u1', 'approve')
        assert resp.status == 200
        assert db.get_user('u1').status == 'platform_select'

    @pytest.mark.asyncio
    async def test_reject_persists_status_and_count(self, server, db):
        """Regression: the reject side effect must not roll the status
        back to pending_demo when it saves reject_count."""
        _seed(db, 'u1', 'pending_demo')
        resp = await _act(server, 'u1', 'reject')
        assert resp.status == 200
        u = db.get_user('u1')
        assert u.status == 'rejected'
        assert u.reject_count == 1

    @pytest.mark.asyncio
    async def test_ban_keyed_user_persists_and_clears_key(self, server, db):
        """Regression: ban of a user WITH a key must survive
        revoke_user_key's full-row save."""
        _seed(db, 'u1', 'demo', uuid='fake-uuid', email='u1@x')
        resp = await _act(server, 'u1', 'ban')
        assert resp.status == 200
        u = db.get_user('u1')
        assert u.status == 'banned'
        assert u.uuid is None and u.email is None

    @pytest.mark.asyncio
    async def test_unban_keyed_user(self, server, db):
        _seed(db, 'u1', 'banned', uuid='fake-uuid', email='u1@x')
        resp = await _act(server, 'u1', 'unban')
        assert resp.status == 200
        u = db.get_user('u1')
        assert u.status == 'new'
        assert u.uuid is None and u.email is None

    @pytest.mark.asyncio
    async def test_revoke_demo_user(self, server, db):
        _seed(db, 'u1', 'demo', uuid='fake-uuid', email='u1@x')
        resp = await _act(server, 'u1', 'revoke')
        assert resp.status == 200
        u = db.get_user('u1')
        assert u.status == 'banned'
        assert u.uuid is None and u.email is None

    @pytest.mark.asyncio
    async def test_reset_keyed_user_full_clean_slate(self, server, db):
        _seed(db, 'u1', 'demo', uuid='fake-uuid', email='u1@x',
              reject_count=2)
        resp = await _act(server, 'u1', 'reset')
        assert resp.status == 200
        u = db.get_user('u1')
        assert u.status == 'new'
        assert u.uuid is None and u.email is None
        assert u.reject_count == 0

    @pytest.mark.asyncio
    async def test_grant_paid_full_grant(self, server, db):
        _seed(db, 'u1', 'demo')
        resp = await _act(server, 'u1', 'grant_paid')
        assert resp.status == 200
        u = db.get_user('u1')
        assert u.status == 'paid'
        assert u.quota_gb == 100.0
        assert u.subscription_expiry is not None
        with db._connect() as c:
            row = c.execute(
                "SELECT plan_type, is_active FROM subscriptions "
                "WHERE chat_id = 'u1'"
            ).fetchone()
        assert row is not None and row[0] == 'monthly' and row[1] == 1

    @pytest.mark.asyncio
    async def test_grant_paid_never_lowers_hand_raised_quota(self, server, db):
        _seed(db, 'u1', 'demo', quota_gb=500.0)
        await _act(server, 'u1', 'grant_paid')
        assert db.get_user('u1').quota_gb == 500.0

    @pytest.mark.asyncio
    async def test_grant_paid_invalid_from_banned(self, server, db):
        _seed(db, 'u1', 'banned')
        resp = await _act(server, 'u1', 'grant_paid')
        assert resp.status == 400
        assert db.get_user('u1').status == 'banned'

    @pytest.mark.asyncio
    async def test_grant_100gb_adds_to_quota(self, server, db):
        _seed(db, 'u1', 'demo', quota_gb=10.0)
        resp = await _act(server, 'u1', 'grant_100gb')
        assert resp.status == 200
        assert db.get_user('u1').quota_gb == 110.0

    @pytest.mark.asyncio
    async def test_set_quota_replaces(self, server, db):
        _seed(db, 'u1', 'demo', quota_gb=10.0)
        resp = await _act(server, 'u1', 'set_quota', value='25')
        assert resp.status == 200
        assert db.get_user('u1').quota_gb == 25.0

    @pytest.mark.asyncio
    async def test_set_expire_sets_end_of_day(self, server, db):
        _seed(db, 'u1', 'paid')
        resp = await _act(server, 'u1', 'set_expire', value='2027-01-15')
        assert resp.status == 200
        assert db.get_user('u1').subscription_expiry.startswith('2027-01-15T23:59')

    @pytest.mark.asyncio
    async def test_unknown_action_400(self, server, db):
        _seed(db, 'u1', 'demo')
        resp = await _act(server, 'u1', 'nuke_from_orbit')
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_missing_user_404(self, server, db):
        resp = await _act(server, 'ghost', 'ban')
        assert resp.status == 404
