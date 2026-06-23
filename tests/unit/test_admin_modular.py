"""Tests for modular admin handlers - Phase 3 refactoring.

Verifies that admin handlers are properly split into mixins.
"""

import pytest
from unittest.mock import Mock, patch

from bot.handlers.admin import (
    AdminHandler,
    AdminHandlerBase,
    AdminUsersMixin,
    AdminBroadcastMixin,
    AdminStatsMixin
)


class TestAdminModularStructure:
    """Test that admin handlers are properly modularized."""
    
    def test_admin_handler_combines_mixins(self):
        """Test that AdminHandler inherits from all mixins."""
        bases = AdminHandler.__bases__
        
        assert AdminUsersMixin in bases
        assert AdminBroadcastMixin in bases
        assert AdminStatsMixin in bases
    
    def test_base_has_core_functionality(self):
        """Test that AdminHandlerBase has core functionality."""
        assert hasattr(AdminHandlerBase, 'can_handle')
        assert hasattr(AdminHandlerBase, 'handle')
        assert hasattr(AdminHandlerBase, '_resolve_target')
        assert hasattr(AdminHandlerBase, 'show_admin_help')
        assert hasattr(AdminHandlerBase, 'ADMIN_COMMANDS')
    
    def test_users_mixin_has_user_methods(self):
        """Test that AdminUsersMixin has user management methods."""
        methods = [
            'show_pending',
            'approve_user',
            'reject_user',
            'show_user',
            'ban_user',
            'unban_user',
            'reset_user',
            'set_limit',
            'grant_100gb',
            'approve_payment',
            'show_active_users',
            'show_all_users',
        ]
        
        for method in methods:
            assert hasattr(AdminUsersMixin, method), f"Missing {method}"
    
    def test_broadcast_mixin_has_broadcast_methods(self):
        """Test that AdminBroadcastMixin has broadcast methods."""
        methods = [
            'broadcast_preview',
            'broadcast_confirm',
            'broadcast_cancel',
        ]
        
        for method in methods:
            assert hasattr(AdminBroadcastMixin, method), f"Missing {method}"
        
        # Check pending broadcasts storage
        assert hasattr(AdminBroadcastMixin, '_pending_broadcasts')
    
    def test_stats_mixin_has_stats_methods(self):
        """Test that AdminStatsMixin has stats methods."""
        methods = [
            'show_overall_stats',
            'backup_db',
        ]
        
        for method in methods:
            assert hasattr(AdminStatsMixin, method), f"Missing {method}"


class TestAdminBackwardCompatibility:
    """Test backward compatibility with old admin.py imports."""
    
    def test_old_import_path_still_works(self):
        """Test that old import path still works."""
        # Note: DeprecationWarning is only shown on first import
        # We just verify the import works and has expected attributes
        import importlib
        import sys
        
        # Remove cached module to force re-import
        if 'bot.handlers.admin' in sys.modules:
            del sys.modules['bot.handlers.admin']
        if 'bot.handlers.admin' in sys.modules:
            del sys.modules['bot.handlers.admin']
            
        from bot.handlers import admin as old_admin
        
        assert hasattr(old_admin, 'AdminHandler')
        assert hasattr(old_admin, 'AdminHandlerBase')
    
    def test_admin_commands_unchanged(self):
        """Test that ADMIN_COMMANDS dict is preserved."""
        expected_commands = [
            '/pending',
            '/approve',
            '/reject',
            '/user',
            '/ban',
            '/unban',
            '/reset',
            '/broadcast',
            '/broadcast_confirm',
            '/broadcast_cancel',
            '/users',
            '/users_all',
            '/backup',
            '/stats',
            '/grant_100gb',
            '/help',
        ]
        
        for cmd in expected_commands:
            assert cmd in AdminHandlerBase.ADMIN_COMMANDS, f"Missing command {cmd}"


class TestAdminHandlerInstance:
    """Test AdminHandler instance functionality."""
    
    @pytest.fixture
    def admin_handler(self):
        """Create AdminHandler with mocked dependencies."""
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        mock_config.FORUM_ENABLED = False
        
        handler = AdminHandler(mock_bot, mock_db, mock_config)
        handler._is_admin = Mock(return_value=True)
        return handler
    
    def test_can_handle_admin_command(self, admin_handler):
        """Test that handler recognizes admin commands."""
        update = {
            'message': {
                'text': '/pending',
                'chat': {'id': '12345'},
                'from': {'id': '12345'}
            }
        }
        
        assert admin_handler.can_handle(update) is True
    
    def test_can_handle_rejects_non_admin(self, admin_handler):
        """Test that handler rejects non-admin users."""
        admin_handler._is_admin = Mock(return_value=False)
        
        update = {
            'message': {
                'text': '/pending',
                'chat': {'id': '12345'},
                'from': {'id': '12345'}
            }
        }
        
        assert admin_handler.can_handle(update) is False
    
    def test_resolve_target_by_username(self, admin_handler):
        """Test resolving user by username."""
        expected_user = Mock()
        admin_handler.db.get_user_by_username = Mock(return_value=expected_user)
        
        result = admin_handler._resolve_target('@testuser')
        
        assert result == expected_user
        admin_handler.db.get_user_by_username.assert_called_once_with('@testuser')
    
    def test_resolve_target_by_chat_id(self, admin_handler):
        """Test resolving user by chat_id."""
        expected_user = Mock()
        admin_handler.db.get_user = Mock(return_value=expected_user)
        
        result = admin_handler._resolve_target('12345')
        
        assert result == expected_user
        admin_handler.db.get_user.assert_called_once_with('12345')
