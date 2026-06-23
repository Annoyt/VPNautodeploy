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
