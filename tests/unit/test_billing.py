"""grant_paid_access — the single definition of the paid tier.

Stars payments, /approve_payment and the dashboard button all route
through it; these tests pin the invariants each surface used to break
on its own (status not flipped, quota left at the demo default, panel
never synced / synced via the traffic-wiping add path).
"""

from datetime import datetime
from unittest.mock import MagicMock, Mock, patch

from bot.services.billing import grant_paid_access
from bot.config.constants import BYTES_PER_GB

PAID_UNTIL = datetime(2026, 12, 31, 23, 59)


def _user(status='demo', quota=5.0, email='u@x', uuid='uuid-1'):
    u = Mock()
    u.chat_id = '42'
    u.status = status
    u.quota_gb = quota
    u.email = email
    u.uuid = uuid
    u.limit_ip = 1
    u.subscription_expiry = None
    return u


def _config(paid_gb=100):
    cfg = Mock()
    cfg.PAID_TRAFFIC_GB = paid_gb
    cfg.INBOUND_ID = 1
    return cfg


class TestGrantPaidAccess:

    def test_promotes_demo_to_paid_everywhere(self):
        user = _user()
        db = MagicMock()
        db.get_user.return_value = user
        xui = Mock()
        xui.sync_client_settings_sync.return_value = True

        with patch('bot.services.billing.StateMachine') as SM:
            SM.return_value.transition.return_value = True
            result = grant_paid_access(db, _config(), xui, '42', PAID_UNTIL)

        assert result['status_ok'] is True
        assert result['panel_ok'] is True
        assert user.status == 'paid'
        assert user.quota_gb == 100.0
        assert user.subscription_expiry == PAID_UNTIL.isoformat()
        db.save_user.assert_called_once_with(user)
        SM.return_value.transition.assert_called_once()

        _email, updates = xui.sync_client_settings_sync.call_args[0]
        assert updates['enable'] is True
        assert updates['totalGB'] == 100 * BYTES_PER_GB
        assert updates['expiryTime'] == int(PAID_UNTIL.timestamp() * 1000)

    def test_never_lowers_hand_raised_quota(self):
        user = _user(status='paid', quota=500.0)
        db = MagicMock()
        db.get_user.return_value = user
        xui = Mock()
        xui.sync_client_settings_sync.return_value = True

        result = grant_paid_access(db, _config(), xui, '42', PAID_UNTIL)

        assert user.quota_gb == 500.0
        _email, updates = xui.sync_client_settings_sync.call_args[0]
        assert updates['totalGB'] == 500 * BYTES_PER_GB
        assert result['panel_ok'] is True

    def test_already_paid_skips_transition(self):
        user = _user(status='paid')
        db = MagicMock()
        db.get_user.return_value = user
        xui = Mock()
        xui.sync_client_settings_sync.return_value = True

        with patch('bot.services.billing.StateMachine') as SM:
            grant_paid_access(db, _config(), xui, '42', PAID_UNTIL)

        SM.return_value.transition.assert_not_called()

    def test_reprovisions_purged_client_with_same_uuid(self):
        """Panel purged the client after a long lapse: the in-place
        update misses, so the grant re-adds with the SAME uuid — keys
        the user already installed must start working again. The add
        only re-attaches the relational record, so a SECOND in-place
        update must follow to refresh the stale client_traffics row
        that actually gates xray/hy2."""
        user = _user()
        db = MagicMock()
        db.get_user.return_value = user
        xui = Mock()
        xui.sync_client_settings_sync.side_effect = [False, True]
        xui.add_client_sync.return_value = True

        with patch('bot.services.billing.StateMachine') as SM:
            SM.return_value.transition.return_value = True
            result = grant_paid_access(db, _config(), xui, '42', PAID_UNTIL)

        assert result['panel_ok'] is True
        client_cfg, inbound = xui.add_client_sync.call_args[0]
        assert client_cfg['id'] == 'uuid-1'
        assert client_cfg['email'] == 'u@x'
        assert client_cfg['totalGB'] == 100 * BYTES_PER_GB
        assert client_cfg['enable'] is True
        assert xui.sync_client_settings_sync.call_count == 2

    def test_reprovision_with_stale_accounting_reports_failure(self):
        """Re-add succeeded but the follow-up accounting refresh did
        not: the client stays gated (client_traffics.enable=0), so the
        grant must report panel_ok=False, not claim success."""
        user = _user()
        db = MagicMock()
        db.get_user.return_value = user
        xui = Mock()
        xui.sync_client_settings_sync.return_value = False
        xui.add_client_sync.return_value = True

        with patch('bot.services.billing.StateMachine') as SM:
            SM.return_value.transition.return_value = True
            result = grant_paid_access(db, _config(), xui, '42', PAID_UNTIL)

        assert result['panel_ok'] is False

    def test_panel_failure_reported_not_raised(self):
        user = _user(status='paid')
        db = MagicMock()
        db.get_user.return_value = user
        xui = Mock()
        xui.sync_client_settings_sync.side_effect = Exception('panel down')

        result = grant_paid_access(db, _config(), xui, '42', PAID_UNTIL)

        assert result['panel_ok'] is False
        assert result['status_ok'] is True   # bot.db side still applied
        db.save_user.assert_called_once()

    def test_no_key_yet_skips_panel(self):
        user = _user(status='paid', email=None, uuid=None)
        db = MagicMock()
        db.get_user.return_value = user

        result = grant_paid_access(db, _config(), Mock(), '42', PAID_UNTIL)

        assert result['panel_ok'] is None

    def test_missing_user_is_safe(self):
        db = MagicMock()
        db.get_user.return_value = None

        result = grant_paid_access(db, _config(), Mock(), '42', PAID_UNTIL)

        assert result['status_ok'] is False
        assert result['user'] is None
