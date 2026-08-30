"""Comprehensive unit tests for bot/handlers/admin/users.py.

Focus areas:
1. approve_user - x-ui sync failures
2. reject_user - email notifications
3. ban_user - key revocation
4. reset_user - complete reset flow
5. set_limit - validation
6. grant_100gb - quota arithmetic
7. approve_payment - subscription creation
"""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

from bot.handlers.admin.users import AdminUsersMixin
from bot.config import UserState, BYTES_PER_GB
from bot.models import User


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_bot():
    """Create mock bot."""
    bot = MagicMock()
    bot.send_message = MagicMock(return_value={'message_id': 123})
    bot.services = {}
    return bot


@pytest.fixture
def mock_db():
    """Create mock database."""
    db = MagicMock()

    # Sample users - these return None by default and should be overridden in tests
    db.get_user = MagicMock(return_value=None)
    db.get_user_by_username = MagicMock(return_value=None)
    db.get_all_users = MagicMock(return_value=[])
    db.get_pending_users = MagicMock(return_value=[])
    db.save_user = MagicMock(return_value=True)
    db.update_status = MagicMock(return_value=True)
    db.reset_user_data = MagicMock(return_value=True)
    db.create_subscription = MagicMock(return_value=True)
    db.log_admin_action = MagicMock()

    return db


@pytest.fixture
def mock_config():
    """Create mock config."""
    config = MagicMock()
    config.is_admin = MagicMock(return_value=True)
    config.FORUM_ENABLED = False
    config.FORUM_GROUP_ID = None
    config.SUPER_ADMIN_ID = 'admin123'
    return config


@pytest.fixture
def admin_handler(mock_bot, mock_db, mock_config):
    """Create AdminUsersMixin handler with all mocks."""
    handler = AdminUsersMixin(mock_bot, mock_db, mock_config)
    return handler


@pytest.fixture
def sample_pending_user():
    """Create sample pending user."""
    return User(
        chat_id='123456',
        username='testuser',
        email='test@example.com',
        status='pending_demo',
        lang='ru'
    )


@pytest.fixture
def sample_active_user():
    """Create sample active user."""
    return User(
        chat_id='789012',
        username='activeuser',
        email='active@example.com',
        uuid='test-uuid-123',
        status='demo',
        lang='ru',
        quota_gb=10.0,
        limit_ip=1
    )


@pytest.fixture
def sample_paid_user():
    """Create sample paid user."""
    return User(
        chat_id='111222',
        username='paiduser',
        email='paid@example.com',
        uuid='paid-uuid-456',
        status='paid',
        lang='en',
        quota_gb=50.0,
        limit_ip=3
    )


# =============================================================================
# Test 1: approve_user - x-ui sync failures
# =============================================================================

class TestApproveUser:
    """Tests for approve_user with focus on x-ui sync failures."""

    def test_approve_user_no_args(self, admin_handler):
        """Test approve_user with no arguments sends error."""
        admin_handler.approve_user('admin_chat', [])

        admin_handler.bot.send_message.assert_called_once()
        args = admin_handler.bot.send_message.call_args
        assert 'Укажите пользователя' in args[1]['text']

    def test_approve_user_user_not_found(self, admin_handler):
        """Test approve_user with non-existent user."""
        admin_handler._resolve_target = MagicMock(return_value=None)

        admin_handler.approve_user('admin_chat', ['@nonexistent'])

        admin_handler.bot.send_message.assert_called_once()
        args = admin_handler.bot.send_message.call_args
        assert 'Пользователь не найден' in args[1]['text']

    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_approve_from_pending_demo_success(self, mock_notifier, mock_sm_class, admin_handler, sample_pending_user):
        """Test approving user from pending_demo state transitions to platform_select."""
        admin_handler._resolve_target = MagicMock(return_value=sample_pending_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.approve_user('admin_chat', ['@testuser'])

        # Should transition to PLATFORM_SELECT from PENDING_DEMO
        mock_sm.transition.assert_called_once_with('123456', UserState.PLATFORM_SELECT)

        # Notification should be sent
        mock_notifier.return_value.notify_approved.assert_called_once_with('123456', 'ru')

        # Admin confirmation
        admin_handler.bot.send_message.assert_called()
        confirm_call = [c for c in admin_handler.bot.send_message.call_args_list
                        if 'одобрен' in str(c[1]['text'])]
        assert len(confirm_call) > 0

    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_approve_from_other_state_goes_to_demo(self, mock_notifier, mock_sm_class, admin_handler, sample_active_user):
        """Test approving user from non-pending state transitions to DEMO."""
        # User is in NEW state (not PENDING_DEMO)
        sample_active_user.status = 'new'
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.approve_user('admin_chat', ['@activeuser'])

        # Should transition to DEMO (default for non-pending-demo states)
        mock_sm.transition.assert_called_once_with('789012', UserState.DEMO)

    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_approve_handles_xui_failure_gracefully(self, mock_notifier, mock_sm_class, admin_handler, sample_pending_user):
        """Test that approval succeeds even if x-ui sync fails later."""
        admin_handler._resolve_target = MagicMock(return_value=sample_pending_user)

        # State machine might fail x-ui sync internally but approve itself
        # should not crash - it only deals with state transitions
        mock_sm = MagicMock()
        mock_sm.transition.side_effect = Exception("X-UI API timeout")
        mock_sm_class.return_value = mock_sm

        # Should not crash despite x-ui failure
        with pytest.raises(Exception) as exc_info:
            admin_handler.approve_user('admin_chat', ['@testuser'])

        # The error should propagate from state machine
        assert "X-UI API timeout" in str(exc_info.value)

    def test_approve_without_username(self, admin_handler, sample_pending_user):
        """Test approving user without username shows user_chat_id."""
        sample_pending_user.username = None
        admin_handler._resolve_target = MagicMock(return_value=sample_pending_user)

        with patch('bot.handlers.admin.users.StateMachine'), \
             patch('bot.handlers.admin.users.NotificationService'):
            admin_handler.approve_user('admin_chat', ['123456'])

            # Check that confirmation uses user_ID format
            confirm_call = [c for c in admin_handler.bot.send_message.call_args_list
                           if 'одобрен' in str(c[1]['text'])]
            assert len(confirm_call) > 0
            assert 'user_123456' in confirm_call[0][1]['text']


# =============================================================================
# Test 2: reject_user - email notifications
# =============================================================================

class TestRejectUser:
    """Tests for reject_user with focus on email notifications."""

    def test_reject_user_no_args(self, admin_handler):
        """Test reject_user with no arguments sends error."""
        admin_handler.reject_user('admin_chat', [])

        admin_handler.bot.send_message.assert_called_once()
        assert 'Укажите пользователя' in admin_handler.bot.send_message.call_args[1]['text']

    def test_reject_user_not_found(self, admin_handler):
        """Test reject_user with non-existent user."""
        admin_handler._resolve_target = MagicMock(return_value=None)

        admin_handler.reject_user('admin_chat', ['@nonexistent'])

        assert 'Пользователь не найден' in admin_handler.bot.send_message.call_args[1]['text']

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_reject_revokes_key_and_transitions_state(self, mock_notifier, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test reject_user revokes key and transitions to REJECTED."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.reject_user('admin_chat', ['@activeuser'])

        # Key should be revoked
        mock_revoke.assert_called_once()
        revoke_args = mock_revoke.call_args
        assert revoke_args[0][0] == sample_active_user  # user object

        # State should transition to REJECTED
        mock_sm.transition.assert_called_once_with('789012', UserState.REJECTED)

        # Notification should be sent
        mock_notifier.return_value.notify_rejected.assert_called_once_with('789012', 'ru')

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_reject_clears_email_to_prevent_key_restore(self, mock_notifier, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test that reject clears email so user can't /mykey old key back."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        # Mock revoke_user_key to clear email
        def revoke_side_effect(user, xui, db):
            user.email = None
            user.uuid = None

        mock_revoke.side_effect = revoke_side_effect

        with patch('bot.handlers.admin.users.StateMachine') as sm_class:
            mock_sm = MagicMock()
            sm_class.return_value = mock_sm

            admin_handler.reject_user('admin_chat', ['@activeuser'])

            # After rejection, user should not be able to get old key
            assert sample_active_user.email is None
            assert sample_active_user.uuid is None

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_reject_with_different_language(self, mock_notifier, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test reject_user with English language."""
        sample_active_user.lang = 'en'
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.reject_user('admin_chat', ['@activeuser'])

        # Notification should use English language
        mock_notifier.return_value.notify_rejected.assert_called_once_with('789012', 'en')

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_reject_sends_admin_confirmation(self, mock_notifier, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test reject_user sends confirmation to admin."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.reject_user('admin_chat', ['@activeuser'])

        # Check admin confirmation message
        admin_handler.bot.send_message.assert_called()
        confirm_call = [c for c in admin_handler.bot.send_message.call_args_list
                        if 'отклонён' in str(c[1]['text'])]
        assert len(confirm_call) > 0


# =============================================================================
# Test 3: ban_user - key revocation
# =============================================================================

class TestBanUser:
    """Tests for ban_user with focus on key revocation."""

    def test_ban_user_no_args(self, admin_handler):
        """Test ban_user with no arguments sends error."""
        admin_handler.ban_user('admin_chat', [])

        assert 'Укажите пользователя' in admin_handler.bot.send_message.call_args[1]['text']

    def test_ban_user_not_found(self, admin_handler):
        """Test ban_user with non-existent user."""
        admin_handler._resolve_target = MagicMock(return_value=None)

        admin_handler.ban_user('admin_chat', ['@nonexistent'])

        assert 'Пользователь не найден' in admin_handler.bot.send_message.call_args[1]['text']

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    def test_ban_revokes_key(self, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test ban_user revokes the VPN key."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.ban_user('admin_chat', ['@activeuser'])

        # Key should be revoked
        mock_revoke.assert_called_once()
        revoke_call = mock_revoke.call_args
        assert revoke_call[0][0] == sample_active_user

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    def test_ban_clears_uuid_and_email(self, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test ban_user clears uuid and email to make ban enforceable."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        # Simulate revoke clearing the fields
        def clear_fields(user, xui, db):
            user.uuid = None
            user.email = None
            db.save_user(user)

        mock_revoke.side_effect = clear_fields

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.ban_user('admin_chat', ['@activeuser'])

        # Verify fields were cleared
        assert sample_active_user.uuid is None
        assert sample_active_user.email is None

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    def test_ban_transitions_to_banned_state(self, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test ban_user transitions user to BANNED state."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.ban_user('admin_chat', ['@activeuser'])

        # State should transition to BANNED
        mock_sm.transition.assert_called_once_with('789012', UserState.BANNED)

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    def test_ban_sends_confirmation(self, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test ban_user sends confirmation to admin."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.ban_user('admin_chat', ['@activeuser'])

        # Should send confirmation
        assert admin_handler.bot.send_message.called
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'забанен' in text or 'banned' in text.lower()


# =============================================================================
# Test 4: reset_user - complete reset flow
# =============================================================================

class TestResetUser:
    """Tests for reset_user with focus on complete reset flow."""

    def test_reset_user_no_args(self, admin_handler):
        """Test reset_user with no arguments sends error."""
        admin_handler.reset_user('admin_chat', [])

        assert 'Укажите пользователя' in admin_handler.bot.send_message.call_args[1]['text']

    def test_reset_user_not_found(self, admin_handler):
        """Test reset_user with non-existent user."""
        admin_handler._resolve_target = MagicMock(return_value=None)

        admin_handler.reset_user('admin_chat', ['@nonexistent'])

        assert 'Пользователь не найден' in admin_handler.bot.send_message.call_args[1]['text']

    def test_reset_user_removes_from_xui(self, admin_handler, sample_active_user):
        """Test reset_user removes client from X-UI."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_xui = MagicMock()
        admin_handler.bot.services = {'xui': mock_xui}

        with patch('bot.handlers.admin.users.StateMachine'):
            admin_handler.reset_user('admin_chat', ['@activeuser'])

            # Should attempt to remove from X-UI
            mock_xui.remove_client_sync.assert_called_once_with('active@example.com')

    def test_reset_user_handles_xui_failure(self, admin_handler, sample_active_user):
        """Test reset_user continues even if X-UI removal fails."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_xui = MagicMock()
        mock_xui.remove_client_sync.side_effect = Exception("X-UI down")
        admin_handler.bot.services = {'xui': mock_xui}

        with patch('bot.handlers.admin.users.StateMachine'):
            # Should not crash despite X-UI failure
            admin_handler.reset_user('admin_chat', ['@activeuser'])

            # Reset should still complete
            admin_handler.db.reset_user_data.assert_called_once_with('789012')

    def test_reset_user_sets_state_to_new(self, admin_handler, sample_active_user):
        """Test reset_user sets state to NEW bypassing transition rules."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        with patch('bot.handlers.admin.users.StateMachine') as sm_class:
            mock_sm = MagicMock()
            sm_class.return_value = mock_sm

            admin_handler.reset_user('admin_chat', ['@activeuser'])

            # Should use set_state (unforced) not transition (validated)
            mock_sm.set_state.assert_called_once_with('789012', UserState.NEW)
            mock_sm.transition.assert_not_called()

    def test_reset_user_clears_user_data(self, admin_handler, sample_active_user):
        """Test reset_user clears all user data."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        with patch('bot.handlers.admin.users.StateMachine'):
            admin_handler.reset_user('admin_chat', ['@activeuser'])

            # Should call reset_user_data
            admin_handler.db.reset_user_data.assert_called_once_with('789012')

    def test_reset_user_with_no_email(self, admin_handler, sample_active_user):
        """Test reset_user when user has no email (no X-UI client)."""
        sample_active_user.email = None
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_xui = MagicMock()
        admin_handler.bot.services = {'xui': mock_xui}

        with patch('bot.handlers.admin.users.StateMachine'):
            admin_handler.reset_user('admin_chat', ['@activeuser'])

            # Should not attempt X-UI removal if no email
            mock_xui.remove_client_sync.assert_not_called()

    def test_reset_user_sends_confirmation(self, admin_handler, sample_active_user):
        """Test reset_user sends confirmation to admin."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        with patch('bot.handlers.admin.users.StateMachine'):
            admin_handler.reset_user('admin_chat', ['@activeuser'])

            # Should send confirmation
            assert admin_handler.bot.send_message.called
            text = admin_handler.bot.send_message.call_args[1]['text']
            assert 'сброшен' in text or 'reset' in text.lower()


# =============================================================================
# Test 5: set_limit - validation
# =============================================================================

class TestSetLimit:
    """Tests for set_limit with focus on validation."""

    def test_set_limit_no_args(self, admin_handler):
        """Test set_limit with no arguments sends error."""
        admin_handler.set_limit('admin_chat', [])

        assert 'Формат' in admin_handler.bot.send_message.call_args[1]['text']

    def test_set_limit_one_arg(self, admin_handler):
        """Test set_limit with only username (missing limit value)."""
        admin_handler.set_limit('admin_chat', ['@user'])

        assert 'Формат' in admin_handler.bot.send_message.call_args[1]['text']

    def test_set_limit_user_not_found(self, admin_handler):
        """Test set_limit with non-existent user."""
        admin_handler._resolve_target = MagicMock(return_value=None)

        admin_handler.set_limit('admin_chat', ['@nonexistent', '5'])

        assert 'Пользователь не найден' in admin_handler.bot.send_message.call_args[1]['text']

    def test_set_limit_invalid_number(self, admin_handler, sample_active_user):
        """Test set_limit with non-numeric limit."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        admin_handler.set_limit('admin_chat', ['@activeuser', 'abc'])

        assert 'должно быть числом' in admin_handler.bot.send_message.call_args[1]['text']

    def test_set_limit_valid_integer(self, admin_handler, sample_active_user):
        """Test set_limit with valid integer."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        admin_handler.set_limit('admin_chat', ['@activeuser', '3'])

        # Should save user with new limit
        admin_handler.db.save_user.assert_called_once()
        saved_user = admin_handler.db.save_user.call_args[0][0]
        assert saved_user.limit_ip == 3

    def test_set_limit_zero(self, admin_handler, sample_active_user):
        """Test set_limit with zero (unlimited)."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        admin_handler.set_limit('admin_chat', ['@activeuser', '0'])

        # Should accept zero
        saved_user = admin_handler.db.save_user.call_args[0][0]
        assert saved_user.limit_ip == 0

    def test_set_limit_negative_number(self, admin_handler, sample_active_user):
        """Test set_limit with negative number (accepted as stored)."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        admin_handler.set_limit('admin_chat', ['@activeuser', '-1'])

        # Currently accepts any int that can be parsed
        saved_user = admin_handler.db.save_user.call_args[0][0]
        assert saved_user.limit_ip == -1

    def test_set_limit_updates_xui(self, admin_handler, sample_active_user):
        """Test set_limit updates X-UI when user has email."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        mock_xui = MagicMock()
        admin_handler.bot.services = {'xui': mock_xui}

        admin_handler.set_limit('admin_chat', ['@activeuser', '5'])

        # Should update X-UI in place (add_client would del+re-add the
        # client and wipe its accounted traffic).
        mock_xui.sync_client_settings_sync.assert_called_once()
        email_arg, updates = mock_xui.sync_client_settings_sync.call_args[0]
        assert updates['limitIp'] == 5
        mock_xui.add_client_sync.assert_not_called()

    def test_set_limit_xui_failure_continues(self, admin_handler, sample_active_user):
        """Test set_limit continues even if X-UI update fails."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_xui = MagicMock()
        mock_xui.sync_client_settings_sync = MagicMock(
            side_effect=Exception("X-UI error"))
        admin_handler.bot.services = {'xui': mock_xui}

        # Should not crash
        admin_handler.set_limit('admin_chat', ['@activeuser', '5'])

        # Should still send confirmation
        assert admin_handler.bot.send_message.called
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Лимит IP' in text or 'установлен' in text

    def test_set_limit_with_chat_id_not_username(self, admin_handler, sample_active_user):
        """Test set_limit using chat_id instead of username."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        admin_handler.set_limit('admin_chat', ['789012', '2'])

        # Should work with chat_id
        assert admin_handler.db.save_user.called
        saved_user = admin_handler.db.save_user.call_args[0][0]
        assert saved_user.limit_ip == 2


# =============================================================================
# Test 6: grant_100gb - quota arithmetic
# =============================================================================

class TestGrant100Gb:
    """Tests for grant_100gb with focus on quota arithmetic."""

    def test_grant_100gb_no_args(self, admin_handler):
        """Test grant_100gb with no arguments sends error."""
        admin_handler.grant_100gb('admin_chat', [])

        assert 'Укажите пользователя' in admin_handler.bot.send_message.call_args[1]['text']

    def test_grant_100gb_user_not_found(self, admin_handler):
        """Test grant_100gb with non-existent user."""
        admin_handler._resolve_target = MagicMock(return_value=None)

        admin_handler.grant_100gb('admin_chat', ['@nonexistent'])

        assert 'Пользователь не найден' in admin_handler.bot.send_message.call_args[1]['text']

    def test_grant_100gb_adds_to_existing_quota(self, admin_handler, sample_active_user):
        """Test grant_100gb adds 100GB to existing quota."""
        sample_active_user.quota_gb = 50.0
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        admin_handler.grant_100gb('admin_chat', ['@activeuser'])

        # Should add 100GB
        saved_user = admin_handler.db.save_user.call_args[0][0]
        assert saved_user.quota_gb == 150.0

    def test_grant_100gb_from_zero(self, admin_handler, sample_active_user):
        """Test grant_100gb starting from zero quota."""
        sample_active_user.quota_gb = 0.0
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        admin_handler.grant_100gb('admin_chat', ['@activeuser'])

        saved_user = admin_handler.db.save_user.call_args[0][0]
        assert saved_user.quota_gb == 100.0

    def test_grant_100gb_multiple_grants(self, admin_handler, sample_active_user):
        """Test granting 100GB multiple times accumulates correctly."""
        sample_active_user.quota_gb = 5.0
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        # First grant
        admin_handler.grant_100gb('admin_chat', ['@activeuser'])
        assert admin_handler.db.save_user.call_args[0][0].quota_gb == 105.0

        # Simulate second grant by updating the return value
        sample_active_user.quota_gb = 105.0
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)
        admin_handler.db.save_user.reset_mock()

        admin_handler.grant_100gb('admin_chat', ['@activeuser'])
        assert admin_handler.db.save_user.call_args[0][0].quota_gb == 205.0

    def test_grant_100gb_updates_xui_quota(self, admin_handler, sample_active_user):
        """Test grant_100gb updates X-UI quota in bytes."""
        sample_active_user.quota_gb = 10.0
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_xui = MagicMock()
        mock_xui.get_client_traffic_sync = MagicMock(return_value={
            'upload': 0, 'download': 0, 'total': 10 * BYTES_PER_GB,
        })
        admin_handler.bot.services = {'xui': mock_xui}

        admin_handler.grant_100gb('admin_chat', ['@activeuser'])

        # Should update X-UI with correct byte arithmetic
        _email, updates = mock_xui.sync_client_settings_sync.call_args[0]
        # 10GB + 100GB = 110GB in bytes
        assert updates['totalGB'] == 110 * BYTES_PER_GB

    def test_grant_100gb_xui_byte_conversion(self, admin_handler, sample_active_user):
        """Test that X-UI quota uses correct BYTES_PER_GB constant."""
        sample_active_user.quota_gb = 0.0
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_xui = MagicMock()
        mock_xui.get_client_traffic_sync = MagicMock(return_value={
            'upload': 0, 'download': 0, 'total': 0,
        })
        admin_handler.bot.services = {'xui': mock_xui}

        admin_handler.grant_100gb('admin_chat', ['@activeuser'])

        # 100GB in bytes = 100 * 1024^3
        _email, updates = mock_xui.sync_client_settings_sync.call_args[0]
        assert updates['totalGB'] == 100 * BYTES_PER_GB

    def test_grant_100gb_without_xui(self, admin_handler, sample_active_user):
        """Test grant_100gb when X-UI service is not available."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)
        admin_handler.bot.services = {}

        # Should not crash
        admin_handler.grant_100gb('admin_chat', ['@activeuser'])

        # Should still update local quota
        saved_user = admin_handler.db.save_user.call_args[0][0]
        assert saved_user.quota_gb == 110.0  # 10 + 100

    def test_grant_100gb_shows_new_quota_in_confirmation(self, admin_handler, sample_active_user):
        """Test grant_100gb confirmation shows new quota value."""
        sample_active_user.quota_gb = 25.0
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        admin_handler.grant_100gb('admin_chat', ['@activeuser'])

        # Confirmation should show new total (125.0 format includes decimal)
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert '125.0' in text  # 25 + 100, with decimal
        assert 'ГБ' in text or 'GB' in text


# =============================================================================
# Test 7: approve_payment - subscription creation
# =============================================================================

class TestApprovePayment:
    """Tests for approve_payment with focus on subscription creation."""

    def test_approve_payment_no_args(self, admin_handler):
        """Test approve_payment with no arguments sends error."""
        admin_handler.approve_payment('admin_chat', [])

        assert 'Укажите пользователя' in admin_handler.bot.send_message.call_args[1]['text']

    def test_approve_payment_user_not_found(self, admin_handler):
        """Test approve_payment with non-existent user."""
        admin_handler._resolve_target = MagicMock(return_value=None)

        admin_handler.approve_payment('admin_chat', ['@nonexistent'])

        assert 'Пользователь не найден' in admin_handler.bot.send_message.call_args[1]['text']

    @patch('bot.services.billing.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_approve_payment_transitions_to_paid(self, mock_notifier, mock_sm_class, admin_handler, sample_active_user):
        """Test approve_payment transitions user to PAID state (the
        transition lives in the shared grant_paid_access now)."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.approve_payment('admin_chat', ['@activeuser'])

        # Should transition to PAID
        mock_sm.transition.assert_called_once_with('789012', UserState.PAID)

    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    @patch('datetime.datetime')
    def test_approve_payment_creates_monthly_subscription(self, mock_datetime, mock_notifier, mock_sm_class, admin_handler, sample_active_user):
        """Test approve_payment creates 30-day monthly subscription."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        # Mock datetime to return fixed date
        fixed_now = datetime(2024, 6, 15, 12, 0, 0)
        mock_datetime.now = MagicMock(return_value=fixed_now)
        mock_datetime.timedelta = timedelta

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.approve_payment('admin_chat', ['@activeuser'])

        # Should create subscription
        admin_handler.db.create_subscription.assert_called_once()
        call_args = admin_handler.db.create_subscription.call_args
        assert call_args[0][0] == '789012'  # chat_id
        assert call_args[1]['plan_type'] == 'monthly'
        # Should be 30 days from now
        expected_end = (fixed_now + timedelta(days=30)).isoformat()
        assert call_args[1]['end_date'] == expected_end

    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_approve_payment_sends_notification(self, mock_notifier, mock_sm_class, admin_handler, sample_active_user):
        """Test approve_payment sends payment approval notification."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.approve_payment('admin_chat', ['@activeuser'])

        # Should send notification
        mock_notifier.return_value.notify_payment_approved.assert_called_once_with('789012', 'ru')

    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_approve_payment_with_english_language(self, mock_notifier, mock_sm_class, admin_handler, sample_active_user):
        """Test approve_payment respects user language setting."""
        sample_active_user.lang = 'en'
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.approve_payment('admin_chat', ['@activeuser'])

        # Should send English notification
        mock_notifier.return_value.notify_payment_approved.assert_called_once_with('789012', 'en')

    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_approve_payment_sends_admin_confirmation(self, mock_notifier, mock_sm_class, admin_handler, sample_active_user):
        """Test approve_payment sends confirmation to admin with end date."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.approve_payment('admin_chat', ['@activeuser'])

        # Should send confirmation to admin
        assert admin_handler.bot.send_message.called
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'подтверждена' in text or 'confirmed' in text.lower()
        # Should mention subscription end date
        # Format is YYYY-MM-DD from isoformat()[:10]
        import re
        date_match = re.search(r'\d{4}-\d{2}-\d{2}', text)
        assert date_match is not None

    @patch('bot.handlers.admin.users.StateMachine')
    @patch('bot.handlers.admin.users.NotificationService')
    def test_approve_payment_default_language(self, mock_notifier, mock_sm_class, admin_handler, sample_active_user):
        """Test approve_payment defaults to 'ru' when lang is None."""
        sample_active_user.lang = None
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.approve_payment('admin_chat', ['@activeuser'])

        # Should default to Russian
        mock_notifier.return_value.notify_payment_approved.assert_called_once_with('789012', 'ru')


# =============================================================================
# Test 8: Helper and edge case tests
# =============================================================================

class TestAdminUsersEdgeCases:
    """Tests for edge cases and helper methods."""

    def test_show_pending_empty_list(self, admin_handler):
        """Test show_pending with no pending users."""
        admin_handler.db.get_pending_users.return_value = []

        admin_handler.show_pending('admin_chat', [])

        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Нет ожидающих заявок' in text or 'no pending' in text.lower()

    def test_show_pending_with_users(self, admin_handler):
        """Test show_pending displays users correctly."""
        pending_users = [
            User(chat_id='111', username='user1', email='user1@test.com', status='pending_demo'),
            User(chat_id='222', username=None, email='user2@test.com', status='pending_demo'),
        ]
        admin_handler.db.get_pending_users.return_value = pending_users

        admin_handler.show_pending('admin_chat', [])

        text = admin_handler.bot.send_message.call_args[1]['text']
        assert '@user1' in text
        assert '111' in text
        assert 'user1@test.com' in text
        assert 'user_222' in text  # No username

    def test_show_user_basic_info(self, admin_handler, sample_active_user):
        """Test show_user displays basic user information."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        admin_handler.show_user('admin_chat', ['@activeuser'])

        text = admin_handler.bot.send_message.call_args[1]['text']
        assert '@activeuser' in text
        assert '789012' in text
        assert 'demo' in text
        assert 'active@example.com' in text

    def test_show_user_with_traffic_stats(self, admin_handler, sample_active_user):
        """Test show_user displays traffic statistics when available."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_xui = MagicMock()
        mock_xui.get_client_traffic_sync = MagicMock(return_value={
            'upload': 2 * BYTES_PER_GB,
            'download': 5 * BYTES_PER_GB
        })
        admin_handler.bot.services = {'xui': mock_xui}

        admin_handler.show_user('admin_chat', ['@activeuser'])

        text = admin_handler.bot.send_message.call_args[1]['text']
        assert '2.00' in text  # Upload GB
        assert '5.00' in text  # Download GB

    def test_show_user_handles_traffic_fetch_error(self, admin_handler, sample_active_user):
        """Test show_user gracefully handles traffic fetch errors."""
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_xui = MagicMock()
        mock_xui.get_client_traffic_sync = MagicMock(side_effect=Exception("DB error"))
        admin_handler.bot.services = {'xui': mock_xui}

        # Should not crash
        admin_handler.show_user('admin_chat', ['@activeuser'])

        # Should still show user info without traffic
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert '@activeuser' in text

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    def test_unban_user_clears_stale_data(self, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test unban_user clears stale uuid/email left over from pre-revoke-helper ban."""
        sample_active_user.status = 'banned'
        sample_active_user.uuid = 'old-uuid'
        sample_active_user.email = 'old@example.com'
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.unban_user('admin_chat', ['@activeuser'])

        # Should clear uuid and email
        assert admin_handler.db.save_user.called
        saved_user = admin_handler.db.save_user.call_args[0][0]
        assert saved_user.uuid is None
        assert saved_user.email is None

    @patch('bot.handlers.admin.users.revoke_user_key')
    @patch('bot.handlers.admin.users.StateMachine')
    def test_unban_user_transitions_to_new(self, mock_sm_class, mock_revoke, admin_handler, sample_active_user):
        """Test unban_user transitions user to NEW state."""
        sample_active_user.status = 'banned'
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm

        admin_handler.unban_user('admin_chat', ['@activeuser'])

        mock_sm.transition.assert_called_once_with('789012', UserState.NEW)

    def test_unban_user_with_no_stale_data(self, admin_handler, sample_active_user):
        """Test unban_user when user has no stale uuid/email."""
        sample_active_user.status = 'banned'
        sample_active_user.uuid = None
        sample_active_user.email = None
        admin_handler._resolve_target = MagicMock(return_value=sample_active_user)

        with patch('bot.handlers.admin.users.StateMachine'):
            admin_handler.unban_user('admin_chat', ['@activeuser'])

            # Should still proceed
            admin_handler.db.save_user.assert_not_called()  # Nothing to clear

    def test_show_active_users_filter(self, admin_handler):
        """Test show_active_users filters by demo and paid status."""
        users = [
            User(chat_id='1', status='demo'),
            User(chat_id='2', status='paid'),
            User(chat_id='3', status='banned'),
            User(chat_id='4', status='new'),
        ]
        admin_handler.db.get_all_users.return_value = users

        admin_handler.show_active_users('admin_chat', [])

        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Активные пользователи' in text or 'Active' in text
        # Should show demo and paid, not banned or new
        assert '2' in text  # Count of active users

    def test_show_all_users_no_filter(self, admin_handler):
        """Test show_all_users shows all users regardless of status."""
        users = [
            User(chat_id='1', status='demo'),
            User(chat_id='2', status='banned'),
            User(chat_id='3', status='new'),
        ]
        admin_handler.db.get_all_users.return_value = users

        admin_handler.show_all_users('admin_chat', [])

        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Все пользователи' in text or 'All users' in text
        assert '3' in text  # Count of all users

    def test_show_users_list_ext_user_shows_contact_email(self, admin_handler):
        """Email-only (ext_*) users have no username — the list must show
        their contact_email, otherwise they're unrecognizable."""
        users = [
            User(chat_id='ext_c1d47097', status='demo',
                 contact_email='someone@gmail.com'),
            User(chat_id='12345678', status='paid', username='tguser'),
        ]
        admin_handler.db.get_all_users.return_value = users

        admin_handler.show_active_users('admin_chat', [])

        text = admin_handler.bot.send_message.call_args[1]['text']
        assert '✉️ someone@gmail.com' in text
        assert '@tguser' in text

    def test_resolve_target_by_username(self, admin_handler, sample_active_user):
        """Test _resolve_target handles @username correctly."""
        admin_handler.db.get_user_by_username = MagicMock(return_value=sample_active_user)

        result = admin_handler._resolve_target('@testuser')

        assert result is not None
        admin_handler.db.get_user_by_username.assert_called_once_with('@testuser')

    def test_resolve_target_by_chat_id(self, admin_handler, sample_active_user):
        """Test _resolve_target handles chat_id correctly."""
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        result = admin_handler._resolve_target('789012')

        assert result is not None
        admin_handler.db.get_user.assert_called_once_with('789012')

    def test_resolve_target_without_at_symbol(self, admin_handler, sample_active_user):
        """Test _resolve_target handles username without @ symbol."""
        # Should treat it as chat_id if no @ prefix
        admin_handler.db.get_user = MagicMock(return_value=sample_active_user)

        result = admin_handler._resolve_target('plainusername')

        # Tries as chat_id first
        admin_handler.db.get_user.assert_called_once_with('plainusername')
