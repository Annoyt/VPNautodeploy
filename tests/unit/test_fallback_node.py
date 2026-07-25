"""Fallback reserve node: provisioning, outbound building, revocation."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bot.services.fallback_node import (
    FALLBACK_ALLOWED_STATUSES,
    FallbackNodeService,
    _ENSURE_CACHE_TTL,
)


UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
EMAIL = 'user_paidguy_123@nekovo.ru'


def make_config(**over):
    base = dict(
        FALLBACK_NODE_HOST='212.60.153.208',
        FALLBACK_NODE_PORT=443,
        FALLBACK_NODE_SNI='www.google.com',
        FALLBACK_NODE_PBK='pbk123',
        FALLBACK_NODE_SID='c7',
        FALLBACK_NODE_XUI_URL='https://212.60.153.208:2026',
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


class TestOutbound:
    def test_builds_vless_reality_outbound(self):
        svc = FallbackNodeService(make_config())
        ob = svc.build_outbound(make_user())
        assert ob['type'] == 'vless'
        assert ob['tag'] == 'user_paidguy_123-de'
        assert ob['server'] == '212.60.153.208'
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
