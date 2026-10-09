"""Fallback reserve node: provisioning, outbound building, revocation."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from bot.services.fallback_node import (
    FALLBACK_ALLOWED_STATUSES,
    FallbackNodeService,
    _ENSURE_CACHE_TTL,
    _PANEL_SKIP_S,
)


UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
EMAIL = 'user_paidguy_123@nekovo.ru'


def make_config(**over):
    base = dict(
        FALLBACK_NODE_HOST='<reserve-node-ip>',
        FALLBACK_NODE_PORT=443,
        FALLBACK_NODE_SNI='www.google.com',
        FALLBACK_NODE_PBK='pbk123',
        FALLBACK_NODE_SID='c7',
        FALLBACK_NODE_XUI_URL='https://<reserve-node-ip>:2026',
        FALLBACK_NODE_XUI_BASE_PATH='/sub',
        FALLBACK_NODE_XUI_USER='admin',
        FALLBACK_NODE_XUI_PASS='pw',
        FALLBACK_NODE_INBOUND_ID=1,
    )
    base.update(over)
    return SimpleNamespace(**base)


def make_user(status='paid', uuid=UUID, email=EMAIL):
    return SimpleNamespace(status=status, uuid=uuid, email=email, chat_id='123')


def resp(payload, status=200):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    return r


def panel_session(emails_to_uuids, add_ok=True):
    s = MagicMock()
    s.post.side_effect = lambda url, **kw: (
        resp({'success': True}) if url.endswith('/login')
        else resp({'success': add_ok, 'msg': '' if add_ok else 'err'})
    )
    s.get.return_value = resp({
        'obj': {'settings': json.dumps({'clients': [
            {'email': e, 'id': u} for e, u in emails_to_uuids.items()
        ]})}
    })
    return s


class TestOutbound:
    def test_builds_vless_reality_outbound(self):
        svc = FallbackNodeService(make_config())
        ob = svc.build_outbound(make_user())
        assert ob['type'] == 'vless'
        assert ob['tag'] == 'user_paidguy_123-de'
        assert ob['server'] == '<reserve-node-ip>'
        assert ob['server_port'] == 443
        assert ob['uuid'] == UUID
        assert 'flow' not in ob
        assert ob['tls']['reality']['public_key'] == 'pbk123'
        assert ob['tls']['reality']['short_id'] == 'c7'
        assert ob['tls']['server_name'] == 'www.google.com'

    def test_disabled_when_unconfigured(self):
        svc = FallbackNodeService(make_config(FALLBACK_NODE_HOST=''))
        assert svc.enabled is False
        assert svc.build_outbound(make_user()) is None

    def test_none_without_uuid(self):
        svc = FallbackNodeService(make_config())
        assert svc.build_outbound(make_user(uuid=None)) is None


class TestEnsureClient:
    def _session(self, emails_to_uuids, add_ok=True):
        return panel_session(emails_to_uuids, add_ok)

    def test_adds_missing_client(self):
        svc = FallbackNodeService(make_config())
        s = self._session({})
        with patch.object(svc, '_new_session', return_value=s):
            assert svc.ensure_client(make_user()) is True
        add_calls = [c for c in s.post.call_args_list if 'addClient' in c.args[0]]
        assert len(add_calls) == 1
        body = add_calls[0].kwargs['json']
        client = json.loads(body['settings'])['clients'][0]
        assert client['id'] == UUID
        assert client['email'] == EMAIL

    def test_skips_when_already_present(self):
        svc = FallbackNodeService(make_config())
        s = self._session({EMAIL: UUID})
        with patch.object(svc, '_new_session', return_value=s):
            assert svc.ensure_client(make_user()) is True
        assert not any('addClient' in c.args[0] for c in s.post.call_args_list)

    def test_uuid_mismatch_left_alone(self):
        svc = FallbackNodeService(make_config())
        s = self._session({EMAIL: 'other-uuid'})
        with patch.object(svc, '_new_session', return_value=s):
            assert svc.ensure_client(make_user()) is False
        assert not any('addClient' in c.args[0] for c in s.post.call_args_list)

    def test_cache_avoids_repeat_panel_calls(self):
        svc = FallbackNodeService(make_config())
        s = self._session({EMAIL: UUID})
        with patch.object(svc, '_new_session', return_value=s) as ns:
            assert svc.ensure_client(make_user()) is True
            assert svc.ensure_client(make_user()) is True
        assert ns.call_count == 1

    def test_panel_failure_returns_false(self):
        svc = FallbackNodeService(make_config())
        s = MagicMock()
        s.post.side_effect = Exception('panel down')
        with patch.object(svc, '_new_session', return_value=s):
            assert svc.ensure_client(make_user()) is False

    def test_noop_when_not_paid_flow(self):
        svc = FallbackNodeService(make_config())
        assert svc.ensure_client(make_user(email=None)) is False


class _Clock:
    """Stands in for the ``time`` module inside fallback_node."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    import bot.services.fallback_node as fallback_node
    c = _Clock()
    monkeypatch.setattr(fallback_node, 'time', c)
    return c


class TestMembershipCache:
    """One cache per process: /sub and /kit build a new service per request,
    so a cache on the instance never hit (every paid /sub fetch logged into
    the reserve panel)."""

    def _sessions(self, monkeypatch, *sessions):
        factory = MagicMock(side_effect=list(sessions))
        monkeypatch.setattr(FallbackNodeService, '_new_session', lambda self: factory())
        return factory

    def test_shared_by_every_instance(self, monkeypatch, clock):
        factory = self._sessions(monkeypatch, panel_session({EMAIL: UUID}))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert factory.call_count == 1

    def test_expires_after_the_ttl(self, monkeypatch, clock):
        factory = self._sessions(monkeypatch, *(panel_session({EMAIL: UUID}) for _ in range(2)))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        clock.now += _ENSURE_CACHE_TTL - 1
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert factory.call_count == 1
        clock.now += 1
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert factory.call_count == 2

    def test_a_failure_is_asked_again(self, monkeypatch, clock):
        dead = MagicMock()
        dead.post.side_effect = Exception('panel down')
        factory = self._sessions(monkeypatch, dead, panel_session({}))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is False
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert factory.call_count == 2

    def test_a_rejected_add_is_asked_again(self, monkeypatch, clock):
        factory = self._sessions(monkeypatch, panel_session({}, add_ok=False), panel_session({}))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is False
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert factory.call_count == 2

    def test_keyed_by_uuid_not_email(self, monkeypatch, clock):
        # A re-keyed user keeps the email; the new uuid must reach the panel.
        new_uuid = '11111111-2222-3333-4444-555555555555'
        session = panel_session({EMAIL: UUID})
        factory = self._sessions(monkeypatch, session, panel_session({EMAIL: UUID}))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert FallbackNodeService(make_config()).ensure_client(make_user(uuid=new_uuid)) is False
        assert factory.call_count == 2

    def test_email_of_another_uuid_is_remembered_as_absent(self, monkeypatch, clock):
        factory = self._sessions(monkeypatch, panel_session({EMAIL: 'other-uuid'}))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is False
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is False
        assert factory.call_count == 1

    def test_remove_client_forgets_the_uuid(self, monkeypatch, clock):
        removal = MagicMock()
        removal.post.return_value = resp({'success': True})
        factory = self._sessions(monkeypatch, panel_session({EMAIL: UUID}), removal,
                                 panel_session({}))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert FallbackNodeService(make_config()).remove_client(UUID) is True
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert factory.call_count == 3


OTHER = make_user(uuid='11111111-2222-3333-4444-555555555555',
                  email='user_other_9@nekovo.ru')


def silent_at(step, exc):
    """A session whose ``step`` (login / inbound / add) raises ``exc``."""
    s = panel_session({})
    post = s.post.side_effect

    def _post(url, **kw):
        if (step == 'login' and url.endswith('/login')) or \
                (step == 'add' and url.endswith('/addClient')):
            raise exc
        return post(url, **kw)

    s.post.side_effect = _post
    if step == 'inbound':
        s.get.side_effect = exc
    return s


class TestPanelSkipWindow:
    """A panel that did not answer is left alone by every ensure_client for
    _PANEL_SKIP_S: a blackholed panel costs one timeout, not one per user."""

    def _sessions(self, monkeypatch, *sessions):
        factory = MagicMock(side_effect=list(sessions))
        monkeypatch.setattr(FallbackNodeService, '_new_session', lambda self: factory())
        return factory

    @pytest.mark.parametrize('step', ['login', 'inbound', 'add'])
    @pytest.mark.parametrize('exc', [
        requests.exceptions.ConnectTimeout('connect timed out'),
        requests.exceptions.ReadTimeout('read timed out'),
        requests.exceptions.ConnectionError('connection reset'),
    ])
    def test_silence_at_any_step_opens_the_window(self, monkeypatch, clock, step, exc):
        factory = self._sessions(monkeypatch, silent_at(step, exc))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is False
        # another user, a moment later: the panel is not asked
        assert FallbackNodeService(make_config()).ensure_client(OTHER) is False
        assert factory.call_count == 1

    @pytest.mark.parametrize('step', ['login', 'inbound', 'add'])
    def test_an_answer_does_not(self, monkeypatch, clock, step):
        # non-JSON from a live panel (a login page for a dropped session)
        garbled = ValueError('Expecting value: line 1 column 1')
        factory = self._sessions(monkeypatch, silent_at(step, garbled), panel_session({}))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is False
        assert FallbackNodeService(make_config()).ensure_client(OTHER) is True
        assert factory.call_count == 2

    def test_the_window_lasts_a_minute(self, monkeypatch, clock):
        dead = silent_at('login', requests.exceptions.ConnectTimeout('timed out'))
        factory = self._sessions(monkeypatch, dead, panel_session({}))
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is False
        clock.now += _PANEL_SKIP_S - 1
        assert FallbackNodeService(make_config()).ensure_client(OTHER) is False
        assert factory.call_count == 1
        clock.now += 1
        assert FallbackNodeService(make_config()).ensure_client(OTHER) is True
        assert factory.call_count == 2

    def test_a_cached_answer_still_counts_inside_the_window(self, monkeypatch, clock):
        dead = silent_at('login', requests.exceptions.ConnectTimeout('timed out'))
        self._sessions(monkeypatch, panel_session({EMAIL: UUID}), dead)
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True
        assert FallbackNodeService(make_config()).ensure_client(OTHER) is False
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is True

    def test_revocation_asks_inside_the_window(self, monkeypatch, clock):
        dead = silent_at('login', requests.exceptions.ConnectTimeout('timed out'))
        removal = MagicMock()
        removal.post.return_value = resp({'success': True})
        factory = self._sessions(monkeypatch, dead, removal)
        assert FallbackNodeService(make_config()).ensure_client(make_user()) is False
        assert FallbackNodeService(make_config()).remove_client(UUID) is True
        assert factory.call_count == 2
        assert any('delClient' in c.args[0] for c in removal.post.call_args_list)

    def test_a_silent_panel_fails_a_revocation_quietly(self, monkeypatch, clock):
        dead = silent_at('login', requests.exceptions.ConnectTimeout('timed out'))
        self._sessions(monkeypatch, dead)
        assert FallbackNodeService(make_config()).remove_client(UUID) is False


class TestRemoveClient:
    def test_deletes_by_uuid(self):
        svc = FallbackNodeService(make_config())
        s = MagicMock()
        s.post.return_value = resp({'success': True})
        with patch.object(svc, '_new_session', return_value=s):
            assert svc.remove_client(UUID) is True
        del_calls = [c for c in s.post.call_args_list if 'delClient' in c.args[0]]
        assert len(del_calls) == 1
        assert UUID in del_calls[0].args[0]

    def test_noop_when_unconfigured(self):
        svc = FallbackNodeService(make_config(FALLBACK_NODE_XUI_PASS=''))
        assert svc.remove_client(UUID) is False


class TestRevokeIntegration:
    def test_revoke_user_key_removes_fallback_client(self):
        from bot.services.user_lifecycle import revoke_user_key
        user = make_user()
        db = MagicMock()
        with patch('bot.services.fallback_node.FallbackNodeService.remove_client') as rm, \
             patch('bot.services.fallback_node.FallbackNodeService.enabled', new=True), \
             patch('bot.services.fallback_node.FallbackNodeService._api_configured', new=True):
            revoke_user_key(user, None, db)
        rm.assert_called_once_with(UUID)
        assert user.uuid is None
        db.save_user.assert_called_once_with(user)


class TestSubscriptionGating:
    def test_paid_user_gets_fallback_outbound(self):
        from bot.services.subscription import SubscriptionService
        cfg = make_config(
            ENTRY_NODE_IP='', REALITY_PUBLIC_KEY='', SNI_VALUE='',
            HY2_HOST='', WS_HOST='', WS2_HOST='', STLS_HOST='',
        )
        svc = SubscriptionService(cfg)
        out = svc.build_singbox_config(make_user(), ('reality',))
        tags = [o['tag'] for o in out['outbounds']]
        assert 'user_paidguy_123-de' in tags
        assert 'user_paidguy_123-de' in out['outbounds'][1]['outbounds']  # urltest 'auto'

    def test_demo_user_gets_no_fallback(self):
        from bot.services.subscription import SubscriptionService
        cfg = make_config(
            ENTRY_NODE_IP='', REALITY_PUBLIC_KEY='', SNI_VALUE='',
            HY2_HOST='', WS_HOST='', WS2_HOST='', STLS_HOST='',
        )
        svc = SubscriptionService(cfg)
        out = svc.build_singbox_config(make_user(status='demo'), ('reality',))
        tags = [o['tag'] for o in out['outbounds']]
        assert not any(t.endswith('-de') for t in tags)
