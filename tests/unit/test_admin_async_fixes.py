"""Tests for admin.py async/sync fixes."""

import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from bot.handlers.admin import AdminHandler
from bot.config import Settings


class TestAdminHandlerSyncCalls:
    """Test that admin.py uses correct XUI methods."""
    
    @pytest.fixture
    def admin_handler(self):
        """Create AdminHandler with mocks."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock(spec=Settings)
        config.DEMO_TRAFFIC_GB = 5.0
        config.FORUM_ENABLED = False
        
        handler = AdminHandler(bot, db, config)
        return handler
    
    def test_show_user_uses_db_traffic_method(self, admin_handler):
        """Test show_user uses xui.db.get_client_traffic."""
        # Setup
        user = MagicMock()
        user.email = 'test@example.com'
        user.username = 'testuser'
        user.chat_id = '123456'
        user.status = 'demo'
        user.lang = 'ru'
        user.platform = 'ios'
        user.created_at = '2024-01-01T00:00:00'
        
        admin_handler.db.get_user.return_value = user
        
        mock_xui = MagicMock()
        mock_xui.db.get_client_traffic.return_value = {
            'upload': 1000000,
            'download': 2000000,
            'total': 3000000
        }
        admin_handler.bot.services.get.return_value = mock_xui
        
        # Execute
        admin_handler.show_user('admin_id', ['123456'])
        
        # Verify db method was called
        mock_xui.db.get_client_traffic.assert_called_once_with('test@example.com')
    
    def test_show_overall_stats_uses_db_inbound_method(self, admin_handler):
        """Test show_overall_stats uses xui.db.get_inbound_settings."""
        mock_xui = MagicMock()
        mock_xui.db.get_inbound_settings.return_value = {
            'clients': [{'email': 'test@example.com'}]
        }
        admin_handler.bot.services.get.return_value = mock_xui
        
        admin_handler.db.get_stats.return_value = {
            'total': 0,
            'by_status': {},
            'by_platform': {}
        }
        
        # Execute
        admin_handler.show_overall_stats('admin_id', [])
        
        # Verify db method was called
        mock_xui.db.get_inbound_settings.assert_called_once()


class TestAdminHandlerXUISyncMethods:
    """Test X-UI sync methods in admin.py."""
    
    @pytest.fixture
    def admin_handler(self):
        """Create AdminHandler with mocks."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock(spec=Settings)
        config.DEMO_TRAFFIC_GB = 5.0
        config.FORUM_ENABLED = False
        
        handler = AdminHandler(bot, db, config)
        return handler
    
    def test_set_limit_uses_in_place_update(self, admin_handler):
        """set_limit must update in place — add_client on an existing
        email deletes + re-adds the client, wiping accounted traffic."""
        # Setup
        user = MagicMock()
        user.chat_id = '123456'
        user.email = 'test@example.com'
        user.limit_ip = 1
        
        admin_handler.db.get_user.return_value = user
        
        mock_xui = MagicMock()
        mock_xui.sync_client_settings_sync.return_value = True
        admin_handler.bot.services.get.return_value = mock_xui
        
        # Execute
        admin_handler.set_limit('admin_id', ['123456', '3'])
        
        # Verify correct methods were called
        mock_xui.sync_client_settings_sync.assert_called_once_with(
            'test@example.com', {'limitIp': 3})
        mock_xui.add_client_sync.assert_not_called()
    
    def test_grant_100gb_uses_in_place_update(self, admin_handler):
        """grant_100gb reads the live quota from the accounting row and
        updates in place (add_client would wipe accounted traffic)."""
        # Setup
        user = MagicMock()
        user.chat_id = '123456'
        user.email = 'test@example.com'
        user.quota_gb = 5.0
        
        admin_handler.db.get_user.return_value = user
        
        mock_xui = MagicMock()
        mock_xui.get_client_traffic_sync.return_value = {
            'upload': 0, 'download': 0, 'total': 5 * 1024**3,
        }
        mock_xui.sync_client_settings_sync.return_value = True
        admin_handler.bot.services.get.return_value = mock_xui
        
        # Execute
        admin_handler.grant_100gb('admin_id', ['123456'])
        
        # Verify correct methods were called
        mock_xui.get_client_traffic_sync.assert_called_once_with('test@example.com')
        mock_xui.sync_client_settings_sync.assert_called_once_with(
            'test@example.com', {'totalGB': 105 * 1024**3})
        mock_xui.add_client_sync.assert_not_called()


class TestAdminHandlerErrorHandling:
    """Test error handling in admin.py X-UI operations."""
    
    @pytest.fixture
    def admin_handler(self):
        """Create AdminHandler with mocks."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock(spec=Settings)
        config.DEMO_TRAFFIC_GB = 5.0
        config.FORUM_ENABLED = False
        
        handler = AdminHandler(bot, db, config)
        return handler
    
    def test_show_user_handles_xui_exception(self, admin_handler):
        """Test show_user gracefully handles X-UI exceptions."""
        user = MagicMock()
        user.email = 'test@example.com'
        user.username = 'testuser'
        user.chat_id = '123456'
        user.status = 'demo'
        user.lang = 'ru'
        user.platform = 'ios'
        user.created_at = '2024-01-01T00:00:00'
        
        admin_handler.db.get_user.return_value = user
        
        mock_xui = MagicMock()
        mock_xui.db.get_client_traffic.side_effect = Exception("X-UI error")
        admin_handler.bot.services.get.return_value = mock_xui
        
        # Should not raise
        admin_handler.show_user('admin_id', ['123456'])
        
        # Verify error was handled (message still sent)
        assert admin_handler.bot.send_message.called
