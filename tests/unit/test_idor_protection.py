"""Tests for IDOR (Insecure Direct Object Reference) protection.

These tests verify that users cannot access/modify other users' data
by tampering with callback data.
"""

import pytest
from unittest.mock import Mock, patch

from bot.handlers.callbacks.user import (
    GetKeyHandler,
    SupportRequestHandler,
    PlatformSelectHandler,
    StatsRequestHandler,
)


class TestIDORProtection:
    """Test IDOR protection in callback handlers."""

    @pytest.fixture
    def get_key_handler(self):
        """Create GetKeyHandler with mocked dependencies."""
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        return GetKeyHandler(mock_bot, mock_db, mock_config)

    @pytest.fixture
    def support_handler(self):
        """Create SupportRequestHandler with mocked dependencies."""
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        return SupportRequestHandler(mock_bot, mock_db, mock_config)

    @pytest.fixture
    def platform_handler(self):
        """Create PlatformSelectHandler with mocked dependencies."""
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        return PlatformSelectHandler(mock_bot, mock_db, mock_config)

    @pytest.fixture
    def stats_handler(self):
        """Create StatsRequestHandler with mocked dependencies."""
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        return StatsRequestHandler(mock_bot, mock_db, mock_config)

    def test_get_key_blocks_other_user(self, get_key_handler):
        """Test that GetKeyHandler blocks access to other users' keys."""
        get_key_handler._is_admin = Mock(return_value=False)

        get_key_handler.handle(
            update={},
            chat_id='12345',
            user_id='12345',
            data='get_key:99999'
        )

        # Should send access denied message
        get_key_handler.bot.send_message.assert_called_once()
        call_args = get_key_handler.bot.send_message.call_args
        assert '❌' in call_args[1]['text']
        assert 'only retrieve your own' in call_args[1]['text']

    def test_get_key_allows_own_key(self, get_key_handler):
        """Test that GetKeyHandler allows access to own key."""
        get_key_handler._is_admin = Mock(return_value=False)

        # Patch _run_async to avoid actual execution
        with patch.object(get_key_handler, '_run_async'):
            get_key_handler.handle(
                update={},
                chat_id='12345',
                user_id='12345',
                data='get_key:12345'
            )

            # Should NOT send access denied message
            get_key_handler.bot.send_message.assert_not_called()

    def test_get_key_allows_admin(self, get_key_handler):
        """Test that admin can access any user's key."""
        get_key_handler._is_admin = Mock(return_value=True)

        with patch.object(get_key_handler, '_run_async'):
            get_key_handler.handle(
                update={},
                chat_id='12345',
                user_id='12345',
                data='get_key:99999'
            )

            # Should NOT send access denied message (admin is allowed)
            get_key_handler.bot.send_message.assert_not_called()

    def test_support_blocks_other_user(self, support_handler):
        """Test that SupportRequestHandler blocks access to other users' support."""
        support_handler.handle(
            update={},
            chat_id='12345',
            user_id='12345',
            data='support:99999'
        )

        # Should send access denied message
        support_handler.bot.send_message.assert_called_once()
        call_args = support_handler.bot.send_message.call_args
        assert '❌' in call_args[1]['text']
        assert 'own support chat' in call_args[1]['text']

    def test_support_allows_own_support(self, support_handler):
        """Test that SupportRequestHandler allows access to own support."""
        with patch.object(support_handler, '_open_support_ticket'):
            support_handler.handle(
                update={},
                chat_id='12345',
                user_id='12345',
                data='support:12345'
            )

            # Should NOT send access denied message
            support_handler.bot.send_message.assert_not_called()

    def test_platform_allows_own_platform(self, platform_handler):
        """Test that PlatformSelectHandler allows setting own platform."""
        with patch.object(platform_handler, '_process_platform_selection'):
            platform_handler.handle(
                update={},
                chat_id='12345',
                user_id='12345',
                data='platform:ios'
            )

            # Should NOT send access denied message
            platform_handler.bot.send_message.assert_not_called()


class TestIDORLogging:
    """Test that IDOR attempts are properly logged."""

    @pytest.fixture
    def get_key_handler(self):
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        handler._is_admin = Mock(return_value=False)
        return handler

    @patch('bot.handlers.callbacks.user.logger')
    def test_get_key_logs_idor_attempt(self, mock_logger, get_key_handler):
        """Test that IDOR attempt in get_key is logged."""
        with patch.object(get_key_handler, '_run_async'):
            get_key_handler.handle(
                update={},
                chat_id='12345',
                user_id='12345',
                data='get_key:99999'
            )

            # Should log warning
            mock_logger.warning.assert_called_once()
            log_message = mock_logger.warning.call_args[0][0]
            assert 'IDOR attempt' in log_message
            assert 'get_key' in log_message

    @patch('bot.handlers.callbacks.user.logger')
    def test_support_logs_idor_attempt(self, mock_logger):
        """Test that IDOR attempt in support is logged."""
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        handler = SupportRequestHandler(mock_bot, mock_db, mock_config)

        handler.handle(
            update={},
            chat_id='12345',
            user_id='12345',
            data='support:99999'
        )

        mock_logger.warning.assert_called_once()
        log_message = mock_logger.warning.call_args[0][0]
        assert 'IDOR attempt' in log_message
        assert 'support' in log_message

class TestExistingStatsProtection:
    """Test that existing IDOR protection in StatsRequestHandler still works."""

    @pytest.fixture
    def stats_handler(self):
        """Create StatsRequestHandler with mocked dependencies."""
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        return StatsRequestHandler(mock_bot, mock_db, mock_config)

    def test_stats_user_blocks_other_user(self, stats_handler):
        """Test that StatsRequestHandler blocks access to other users' stats."""
        stats_handler._is_admin = Mock(return_value=False)

        stats_handler.handle(
            update={},
            chat_id='12345',
            user_id='12345',
            data='stats:99999'
        )

        stats_handler.bot.send_message.assert_called_once()
        call_args = stats_handler.bot.send_message.call_args
        assert '❌' in call_args[1]['text']
        assert 'own statistics' in call_args[1]['text']

    def test_stats_user_allows_own_stats(self, stats_handler):
        """Test that StatsRequestHandler allows access to own stats."""
        with patch.object(stats_handler, '_send_stats'):
            stats_handler.handle(
                update={},
                chat_id='12345',
                user_id='12345',
                data='stats:12345'
            )

            stats_handler.bot.send_message.assert_not_called()

    def test_stats_user_allows_admin(self, stats_handler):
        """Test that admin can view any user's stats."""
        stats_handler._is_admin = Mock(return_value=True)

        with patch.object(stats_handler, '_send_stats'):
            stats_handler.handle(
                update={},
                chat_id='12345',
                user_id='12345',
                data='stats:99999'
            )

            stats_handler.bot.send_message.assert_not_called()


class TestStatsCallbackRealTraffic:
    """Test that stats callback fetches real traffic for regular users."""

    @pytest.fixture
    def stats_handler(self):
        """Create StatsRequestHandler with mocked dependencies."""
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        mock_config.SUPER_ADMIN_ID = '1652899'
        mock_config.DEMO_TRAFFIC_GB = 5
        mock_config.XUI_DB_PATH = '/tmp/test_xui.db'
        return StatsRequestHandler(mock_bot, mock_db, mock_config)

    def test_send_stats_fetches_real_traffic(self, stats_handler):
        """Test that non-admin stats callback fetches traffic from XUI DB."""
        stats_handler._is_admin = Mock(return_value=False)

        mock_user = Mock()
        mock_user.email = 'user_test@nekovo.ru'
        stats_handler.db.get_user.return_value = mock_user

        mock_traffic = {'upload': 1073741824, 'download': 536870912, 'total': 1610612736}
        mock_xui = Mock()
        mock_xui.get_client_traffic_sync.return_value = mock_traffic
        stats_handler.bot.services = {'xui': mock_xui}

        stats_handler._send_stats('12345')

        mock_xui.get_client_traffic_sync.assert_called_once_with('user_test@nekovo.ru')

        # Verify the bot sent a message with actual traffic data
        stats_handler.bot.send_message.assert_called_once()
        call_args = stats_handler.bot.send_message.call_args[1]
        assert 'Upload' in call_args['text']
        assert call_args.get('parse_mode') == 'HTML'

    def test_send_stats_no_data_available(self, stats_handler):
        """Test message when traffic data is missing."""
        stats_handler._is_admin = Mock(return_value=False)

        mock_user = Mock()
        mock_user.email = 'user_test@nekovo.ru'
        stats_handler.db.get_user.return_value = mock_user

        mock_xui = Mock()
        mock_xui.get_client_traffic_sync.return_value = None
        stats_handler.bot.services = {'xui': mock_xui}

        stats_handler._send_stats('12345')

        call_args = stats_handler.bot.send_message.call_args[1]
        assert 'Could not retrieve traffic' in call_args['text']

    def test_send_stats_user_without_email(self, stats_handler):
        """Test message when user has no email."""
        stats_handler._is_admin = Mock(return_value=False)

        mock_user = Mock()
        mock_user.email = None
        stats_handler.db.get_user.return_value = mock_user

        stats_handler._send_stats('12345')

        call_args = stats_handler.bot.send_message.call_args[1]
        assert 'incomplete' in call_args['text']
