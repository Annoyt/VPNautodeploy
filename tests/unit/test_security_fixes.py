"""Tests for security fixes from security_code_review.md."""

import pytest
from unittest.mock import MagicMock, patch
import time


class TestCommandsAdminCheck:
    """Test MED-01: Fix _is_admin duplicate args in commands.py."""
    
    def test_handle_help_uses_message_chat_id(self):
        """Test that handle_help uses message chat_id for admin check."""
        from bot.handlers.commands import CommandHandler
        
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.SUPER_ADMIN_ID = '1652899'
        
        handler = CommandHandler(bot, db, config)
        
        # Create update where user_id != message_chat_id (e.g., in a group)
        update = {
            'message': {
                'message_id': 1,
                'chat': {'id': -1001234567890, 'type': 'supergroup'},  # Group chat
                'from': {'id': 1652899, 'username': 'admin'},  # Admin user
                'text': '/help'
            }
        }
        
        # Mock user
        mock_user = MagicMock()
        mock_user.lang = 'en'
        db.get_user.return_value = mock_user
        
        # Call handle_help
        handler.handle_help(update, '1652899')
        
        # Verify bot.send_message was called (admin sees admin help)
        calls = bot.send_message.call_args_list
        assert len(calls) > 0
        # Admin should see admin-only commands listed (e.g. /approve, /ban).
        # The heading text was localized to Russian in the dashboard refresh
        # so don't pin on the literal "Admin Commands" string.
        text = calls[0][1].get('text', '')
        assert '/approve' in text and '/ban' in text


class TestDemoRateLimiting:
    """Test MED-02: Rate limiting on demo requests."""
    
    def test_demo_request_rate_limiting(self):
        """Test that demo requests are rate limited."""
        from bot.handlers.callbacks.user import DemoRequestHandler
        
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        
        handler = DemoRequestHandler(bot, db, config)
        
        # Clear any existing rate limit data
        DemoRequestHandler._demo_request_times.clear()
        
        # First request should work
        update = {'callback_query': {'data': 'request_demo'}}
        mock_user = MagicMock()
        mock_user.status = 'new'
        db.get_user.return_value = mock_user
        
        with patch('bot.handlers.callbacks.user.StateMachine'):
            with patch('bot.handlers.callbacks.user.NotificationService'):
                handler.handle(update, '123456', '123456')
        
        # Second request immediately should be rate limited
        bot.reset_mock()
        handler.handle(update, '123456', '123456')
        
        # Should get rate limit message
        calls = bot.send_message.call_args_list
        assert len(calls) > 0
        assert 'wait' in calls[0][1].get('text', '').lower()
    
    def test_demo_request_after_timeout(self):
        """Test that demo request works after rate limit timeout."""
        from bot.handlers.callbacks.user import DemoRequestHandler
        
        # Set last request to past
        DemoRequestHandler._demo_request_times['999999'] = time.time() - 120  # 2 minutes ago
        
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        
        handler = DemoRequestHandler(bot, db, config)
        
        update = {'callback_query': {'data': 'request_demo'}}
        mock_user = MagicMock()
        mock_user.status = 'new'
        db.get_user.return_value = mock_user
        
        # Should work after timeout
        with patch('bot.handlers.callbacks.user.StateMachine'):
            with patch('bot.handlers.callbacks.user.NotificationService'):
                handler.handle(update, '999999', '999999')
        
        # Should not get rate limit message
        calls = bot.send_message.call_args_list
        for call in calls:
            assert 'wait' not in call[1].get('text', '').lower()


class TestIDORProtection:
    """Test MED-03: IDOR protection in stats callback."""
    
    def test_user_cannot_view_other_stats(self):
        """Test that user cannot view another user's stats via IDOR."""
        from bot.handlers.callbacks.user import StatsRequestHandler
        
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.SUPER_ADMIN_ID = '1652899'
        
        handler = StatsRequestHandler(bot, db, config)
        handler._is_admin = MagicMock(return_value=False)
        
        # User 123456 tries to view stats of user 999999
        update = {'callback_query': {'data': 'stats:999999'}}
        
        # Mock _send_stats to verify it's NOT called for unauthorized access
        with patch.object(handler, '_send_stats') as mock_handle:
            handler.handle(update, '123456', '123456', data='stats:999999')
            
            # _send_stats should NOT be called
            mock_handle.assert_not_called()
        
        # Should get error message
        calls = bot.send_message.call_args_list
        assert len(calls) > 0
        assert 'own statistics' in calls[0][1].get('text', '')
    
    def test_admin_can_view_any_stats(self):
        """Test that admin can view any user's stats."""
        from bot.handlers.callbacks.user import StatsRequestHandler
        
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.SUPER_ADMIN_ID = '1652899'
        
        handler = StatsRequestHandler(bot, db, config)
        
        # Admin tries to view stats of user 999999
        update = {'callback_query': {'data': 'stats:999999'}}
        
        # Mock _send_stats to avoid full execution
        with patch.object(handler, '_send_stats') as mock_handle:
            handler.handle(update, '1652899', '1652899', data='stats:999999')
            
            # Should be allowed
            mock_handle.assert_called_once_with('999999')


class TestXUIDbDuplicateMethod:
    """Test MED-04: Duplicate method removed from xui_db.py."""
    
    def test_no_duplicate_get_inbound_settings(self):
        """Test that get_inbound_settings is defined only once."""
        from bot.services.xui_db import XUIDatabase
        import inspect
        
        # Get all methods of the class
        methods = [name for name, _ in inspect.getmembers(XUIDatabase, predicate=inspect.isfunction)]
        
        # Count occurrences of get_inbound_settings
        count = methods.count('get_inbound_settings')
        assert count == 1, f"get_inbound_settings should be defined once, found {count} times"


class TestTokenSanitization:
    """Test MED-06: Bot token sanitization in logs."""
    
    def test_sanitize_url_masks_token(self):
        """Test that _sanitize_url masks bot token."""
        from bot.core.telegram_client import TelegramClient
        
        client = TelegramClient("123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
        
        url = "https://api.telegram.org/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11/getUpdates"
        sanitized = client._sanitize_url(url)
        
        assert "***TOKEN***" in sanitized
        assert "123456:ABC-DEF" not in sanitized
    
    def test_sanitize_url_handles_empty(self):
        """Test that _sanitize_url handles empty/None input."""
        from bot.core.telegram_client import TelegramClient
        
        client = TelegramClient("test_token")
        
        assert client._sanitize_url("") == ""
        assert client._sanitize_url(None) is None
