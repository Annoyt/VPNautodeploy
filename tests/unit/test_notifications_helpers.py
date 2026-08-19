"""Tests for notification helper functions (admin + user)."""

import pytest
from unittest.mock import Mock, patch

from bot.services.admin_notifications import (
    build_admin_request_keyboard,
    build_approved_user_keyboard,
    build_rejected_user_keyboard,
    format_new_request_text,
    format_support_ticket_text,
    format_pm_support_text,
    format_payment_issue_text,
    format_admin_stats_text,
    format_user_stats_text,
)
from bot.services.user_notifications import (
    get_message,
    build_welcome_keyboard,
    build_platform_keyboard,
    build_main_menu_keyboard,
)
from bot.models import User


class TestAdminNotificationKeyboards:
    """Test admin notification keyboard builders."""
    
    @pytest.fixture
    def sample_user(self):
        """Create sample user."""
        user = Mock(spec=User)
        user.chat_id = "12345"
        return user
    
    def test_build_admin_request_keyboard(self, sample_user):
        """Test keyboard for new request notification."""
        keyboard = build_admin_request_keyboard(sample_user)
        
        assert 'inline_keyboard' in keyboard
        assert len(keyboard['inline_keyboard']) == 3  # 3 rows
        # First row: Approve, Reject
        assert keyboard['inline_keyboard'][0][0]['callback_data'] == 'approve:12345'
        assert keyboard['inline_keyboard'][0][1]['callback_data'] == 'reject:12345'
        # Second row: Message, Profile
        assert keyboard['inline_keyboard'][1][0]['callback_data'] == 'message:12345'
        assert keyboard['inline_keyboard'][1][1]['callback_data'] == 'profile:12345'
        # Third row: Reset approval
        assert keyboard['inline_keyboard'][2][0]['callback_data'] == 'reset_approval:12345'
        
    def test_build_approved_user_keyboard(self, sample_user):
        """Test keyboard for approved user management."""
        keyboard = build_approved_user_keyboard(sample_user)
        
        assert 'inline_keyboard' in keyboard
        assert len(keyboard['inline_keyboard']) == 1
        assert keyboard['inline_keyboard'][0][0]['callback_data'] == 'revoke:12345'
        assert keyboard['inline_keyboard'][0][1]['callback_data'] == 'reset_approval:12345'
        
    def test_build_rejected_user_keyboard(self, sample_user):
        """Test keyboard for rejected user management."""
        keyboard = build_rejected_user_keyboard(sample_user)
        
        assert 'inline_keyboard' in keyboard
        assert len(keyboard['inline_keyboard']) == 1
        assert keyboard['inline_keyboard'][0][0]['callback_data'] == 'reset_approval:12345'


class TestAdminNotificationTextFormatters:
    """Test admin notification text formatters."""
    
    @pytest.fixture
    def sample_user(self):
        """Create sample user."""
        user = Mock(spec=User)
        user.chat_id = "12345"
        user.username = "testuser"
        user.lang = "ru"
        user.created_at = "2026-04-13T10:00:00"
        return user
    
    def test_format_new_request_text_with_username(self, sample_user):
        """Test new request text with username."""
        text = format_new_request_text(sample_user)
        
        assert "New Demo Request" in text
        assert "@testuser" in text
        assert "12345" in text
        assert "ru" in text
        assert "2026-04-13" in text
        
    def test_format_new_request_text_without_username(self, sample_user):
        """Test new request text without username."""
        sample_user.username = None
        text = format_new_request_text(sample_user)
        
        assert "No username" in text
        
    def test_format_support_ticket_text(self, sample_user):
        """Test support ticket text formatting."""
        text = format_support_ticket_text(sample_user, "Help me please!")
        
        assert "Support Ticket" in text
        assert "@testuser" in text
        assert "Help me please!" in text
        
    def test_format_support_ticket_text_truncated(self, sample_user):
        """Test long message is truncated."""
        long_message = "A" * 1000
        text = format_support_ticket_text(sample_user, long_message)
        
        assert len(text) < len(long_message) + 100  # Should be truncated
        
    def test_format_pm_support_text(self, sample_user):
        """Test PM support text formatting."""
        text = format_pm_support_text(sample_user, "PM request")
        
        assert "Support Request" in text
        assert "Reply to this message" in text
        
    def test_format_payment_issue_text(self, sample_user):
        """Test payment issue text formatting."""
        text = format_payment_issue_text(sample_user, "Payment failed")
        
        assert "Payment Issue" in text
        assert "Payment failed" in text
        
    def test_format_admin_stats_text(self):
        """Test admin stats text formatting."""
        stats = {
            'total': 100,
            'by_status': {
                'pending_demo': 5,
                'demo': 20,
                'demo': 70,
                'banned': 5
            },
            'by_platform': {
                'ios': 40,
                'android': 35,
                'windows': 25
            }
        }
        text = format_admin_stats_text(stats)
        
        assert "System Statistics" in text
        assert "Total users: 100" in text
        assert "Pending: 5" in text
        assert "ios" in text
        assert "android" in text
        
    def test_format_user_stats_text_with_traffic_ru(self, sample_user):
        """Test user stats text in Russian with traffic."""
        traffic = {'total': 5 * 1024**3}  # 5 GB
        text = format_user_stats_text(sample_user, traffic, demo_traffic_gb=10)
        
        assert "Ваша статистика" in text
        assert "5.00 GB" in text  # Used
        assert "5.00 GB" in text  # Remaining
        
    def test_format_user_stats_text_no_traffic_en(self, sample_user):
        """Test user stats text in English without traffic."""
        sample_user.lang = "en"
        text = format_user_stats_text(sample_user, {}, demo_traffic_gb=10)
        
        assert "Your Statistics" in text
        assert "Used: 0.00 GB" in text
        assert "Remaining: 10.00 GB" in text


class TestUserNotificationHelpers:
    """Test user notification helpers."""
    
    def test_get_message_existing_key(self):
        """Test getting existing message."""
        with patch('bot.services.user_notifications.MESSAGES', {
            'ru': {'welcome': 'Добро пожаловать, {name}!'},
            'en': {'welcome': 'Welcome, {name}!'}
        }):
            text = get_message('welcome', 'ru', name='Иван')
            assert text == 'Добро пожаловать, Иван!'
            
    def test_get_message_fallback_lang(self):
        """Test fallback to Russian if language not found."""
        with patch('bot.services.user_notifications.MESSAGES', {
            'ru': {'welcome': 'Добро пожаловать!'},
            'en': {'welcome': 'Welcome!'}
        }):
            text = get_message('welcome', 'de')  # German not available
            assert text == 'Добро пожаловать!'
            
    def test_get_message_missing_key(self):
        """Test placeholder for missing key."""
        with patch('bot.services.user_notifications.MESSAGES', {
            'ru': {},
            'en': {}
        }):
            text = get_message('nonexistent', 'ru')
            assert text == '[nonexistent]'
            
    def test_get_message_format_error(self):
        """Test handling format error gracefully."""
        with patch('bot.services.user_notifications.MESSAGES', {
            'ru': {'test': 'Hello {missing_key}'}
        }):
            # Should not raise, returns template as-is
            text = get_message('test', 'ru', wrong_param='value')
            assert 'Hello' in text
            
    def test_build_welcome_keyboard(self):
        """Test welcome keyboard structure."""
        keyboard = build_welcome_keyboard('ru')
        
        assert 'inline_keyboard' in keyboard
        # Should have request demo and language buttons
        assert len(keyboard['inline_keyboard']) >= 1
        assert keyboard['inline_keyboard'][0][0]['callback_data'] == 'request_demo'
        
    def test_build_platform_keyboard(self):
        """Test platform selection keyboard."""
        keyboard = build_platform_keyboard("12345")
        
        assert 'inline_keyboard' in keyboard
        # Check all platforms are present
        callbacks = []
        for row in keyboard['inline_keyboard']:
            for btn in row:
                callbacks.append(btn['callback_data'])
                
        assert 'platform:android:12345' in callbacks
        assert 'platform:ios:12345' in callbacks
        assert 'platform:windows:12345' in callbacks
        assert 'platform:macos:12345' in callbacks
        assert 'platform:other:12345' in callbacks
        
    def test_build_main_menu_keyboard(self):
        """Test main menu keyboard structure."""
        keyboard = build_main_menu_keyboard('ru')
        
        assert 'inline_keyboard' in keyboard
        callbacks = []
        for row in keyboard['inline_keyboard']:
            for btn in row:
                callbacks.append(btn['callback_data'])
                
        assert 'stats' in callbacks
        assert 'my_key' in callbacks
        assert 'support' in callbacks
        assert 'full' in callbacks


class TestResetPaidQuota:
    """Monthly paid pass: counters reset, quota/expiry untouched,
    lapsed subscriptions skipped."""

    def _make_service(self, tmp_path, rows):
        import sqlite3
        from unittest.mock import Mock
        from bot.services.notifications import NotificationService

        db_path = str(tmp_path / "bot.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE users (chat_id TEXT, email TEXT, status TEXT, "
            "subscription_expiry TEXT, traffic_up REAL, traffic_down REAL, "
            "last_traffic_update TEXT)"
        )
        conn.executemany(
            "INSERT INTO users (chat_id, email, status, subscription_expiry, "
            "traffic_up, traffic_down) VALUES (?, ?, ?, ?, 1.5, 2.5)",
            rows,
        )
        conn.commit()
        conn.close()

        svc = NotificationService(Mock(), Mock(), Mock())
        return svc, db_path

    def test_resets_active_paid_and_skips_lapsed(self, tmp_path):
        import sqlite3
        from unittest.mock import Mock

        svc, db_path = self._make_service(tmp_path, [
            ("1", "a@x", "paid", "2099-01-01T00:00:00"),   # active
            ("2", "b@x", "paid", "2020-01-01T00:00:00"),   # lapsed — skip
            ("3", "c@x", "paid", None),                    # no expiry — active
            ("4", "d@x", "demo", None),                    # not paid — ignored
        ])
        xui = Mock()
        xui.sync_client_settings_sync.return_value = True
        xui.reset_client_traffic_sync.return_value = True

        svc._reset_paid_quota_sync(xui, db_path)

        reset_emails = [
            c.args[0] for c in xui.reset_client_traffic_sync.call_args_list
        ]
        assert reset_emails == ["a@x", "c@x"]
        # Re-enable only — quota amount and expiry stay admin-managed.
        for c in xui.sync_client_settings_sync.call_args_list:
            assert c.args[1] == {"enable": True}

        conn = sqlite3.connect(db_path)
        rows = dict(conn.execute(
            "SELECT email, traffic_up + traffic_down FROM users").fetchall())
        assert rows["a@x"] == 0 and rows["c@x"] == 0
        assert rows["b@x"] == 4.0   # lapsed user untouched
        assert svc.bot.send_message.call_count == 2

    def test_panel_failure_keeps_counters(self, tmp_path):
        import sqlite3
        from unittest.mock import Mock

        svc, db_path = self._make_service(tmp_path, [
            ("1", "a@x", "paid", None),
        ])
        xui = Mock()
        xui.sync_client_settings_sync.return_value = False   # panel said no

        svc._reset_paid_quota_sync(xui, db_path)

        xui.reset_client_traffic_sync.assert_not_called()
        conn = sqlite3.connect(db_path)
        up_down = conn.execute(
            "SELECT traffic_up + traffic_down FROM users").fetchone()[0]
        assert up_down == 4.0   # bot.db untouched on failure
        svc.bot.send_message.assert_not_called()
