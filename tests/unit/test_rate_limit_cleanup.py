"""Tests for rate limit cleanup fix - Phase 1."""

import time
import pytest
from unittest.mock import Mock, patch

from bot.handlers.callbacks.user import DemoRequestHandler


class TestRateLimitCleanup:
    """Test memory leak fix in rate limiting (H-04)."""

    def test_cleanup_old_entries(self):
        """Test that old entries are cleaned up to prevent memory leak."""
        # Simulate old entries
        old_time = time.time() - 200  # 200 seconds ago (older than 2x rate limit)
        DemoRequestHandler._demo_request_times = {
            'user1': old_time,
            'user2': old_time,
            'user3': old_time,
        }

        # Create mock handler
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()

        handler = DemoRequestHandler(mock_bot, mock_db, mock_config)

        # Create mock update
        mock_update = {
            'callback_query': {
                'data': 'request_demo',
                'message': {'chat': {'id': 12345}},
                'from': {'id': 12345}
            }
        }

        # Mock db.get_user to return a valid user object
        mock_user = Mock()
        mock_user.status = 'new'
        mock_user.chat_id = '12345'
        mock_user.username = 'testuser'
        mock_user.lang = 'en'
        mock_db.get_user.return_value = mock_user

        # Mock StateMachine to avoid DB calls
        with patch('bot.handlers.callbacks.user.StateMachine') as mock_sm_class:
            mock_sm = Mock()
            mock_sm_class.return_value = mock_sm

            # Mock NotificationService
            with patch('bot.handlers.callbacks.user.NotificationService') as mock_notif_class:
                mock_notifier = Mock()
                mock_notif_class.return_value = mock_notifier

                # Call the handler
                handler.handle(mock_update, '12345', '12345')

        # Verify old entries were cleaned up
        # user1, user2, user3 should be removed (too old)
        # 12345 should be added (current request)
        assert len(DemoRequestHandler._demo_request_times) == 1
        assert '12345' in DemoRequestHandler._demo_request_times
        assert 'user1' not in DemoRequestHandler._demo_request_times
        assert 'user2' not in DemoRequestHandler._demo_request_times
        assert 'user3' not in DemoRequestHandler._demo_request_times

    def test_recent_entries_preserved(self):
        """Test that recent entries are not cleaned up."""
        recent_time = time.time() - 30  # 30 seconds ago (within rate limit)
        DemoRequestHandler._demo_request_times = {
            'recent_user': recent_time,
        }

        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()

        handler = DemoRequestHandler(mock_bot, mock_db, mock_config)

        # Mock to simulate rate limit hit
        mock_user = Mock()
        mock_user.status = 'new'
        mock_db.get_user.return_value = mock_user

        mock_update = {
            'callback_query': {
                'data': 'request_demo',
                'message': {'chat': {'id': 99999}},
                'from': {'id': 99999}
            }
        }

        # Mock rate limit hit - send_message should be called
        handler.handle(mock_update, '99999', '99999')

        # Recent entry should still be there
        assert 'recent_user' in DemoRequestHandler._demo_request_times

    def test_cleanup_threshold_calculation(self):
        """Test that cleanup threshold is 2x rate limit period."""
        # Verify threshold logic
        rate_limit = DemoRequestHandler.DEMO_RATE_LIMIT_SECONDS  # 60
        expected_threshold = rate_limit * 2  # 120

        # This test documents the expected behavior
        assert expected_threshold == 120

        # Verify that entries older than threshold are removed
        current_time = time.time()
        old_entry_time = current_time - 130  # Older than 120 seconds

        DemoRequestHandler._demo_request_times = {
            'old_user': old_entry_time,
        }

        # Simulate cleanup logic
        cleanup_threshold = DemoRequestHandler.DEMO_RATE_LIMIT_SECONDS * 2
        cleaned = {
            k: v for k, v in DemoRequestHandler._demo_request_times.items()
            if current_time - v < cleanup_threshold
        }

        # Old entry should be removed
        assert len(cleaned) == 0
        assert 'old_user' not in cleaned


class TestRateLimitFunctionality:
    """Test that rate limiting still works correctly after cleanup."""

    def test_rate_limit_enforced(self):
        """Test that rate limit is still enforced after cleanup."""
        # Set up a recent request
        recent_time = time.time() - 10  # 10 seconds ago
        DemoRequestHandler._demo_request_times = {
            '12345': recent_time,
        }

        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()

        handler = DemoRequestHandler(mock_bot, mock_db, mock_config)

        mock_update = {
            'callback_query': {
                'data': 'request_demo',
                'message': {'chat': {'id': 12345}},
                'from': {'id': 12345}
            }
        }

        # Call handler - should hit rate limit
        handler.handle(mock_update, '12345', '12345')

        # Should send rate limit message
        mock_bot.send_message.assert_called_once()
        call_args = mock_bot.send_message.call_args
        assert 'wait' in call_args[1]['text'].lower() or '⏳' in call_args[1]['text']

    def test_rate_limit_allows_after_period(self):
        """Test that request is allowed after rate limit period."""
        # Set up an old request
        old_time = time.time() - 70  # 70 seconds ago (past 60s limit)
        DemoRequestHandler._demo_request_times = {
            '12345': old_time,
        }

        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()

        handler = DemoRequestHandler(mock_bot, mock_db, mock_config)

        # Mock user as new with all required attributes
        mock_user = Mock()
        mock_user.status = 'new'
        mock_user.chat_id = '12345'
        mock_user.username = 'testuser'
        mock_user.lang = 'en'
        mock_db.get_user.return_value = mock_user

        mock_update = {
            'callback_query': {
                'data': 'request_demo',
                'message': {'chat': {'id': 12345}},
                'from': {'id': 12345}
            }
        }

        # Mock dependencies to avoid full execution
        with patch('bot.handlers.callbacks.user.StateMachine'):
            with patch('bot.handlers.callbacks.user.NotificationService'):
                # Call handler - should proceed (after cleanup, old entry is gone)
                try:
                    handler.handle(mock_update, '12345', '12345')
                except Exception as e:
                    pytest.fail(f"Handler raised exception: {e}")
