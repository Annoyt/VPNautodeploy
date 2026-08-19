"""Edge case and bug hunt tests for admin handlers."""

import pytest
from unittest.mock import MagicMock, patch, mock_open
import os

from bot.handlers.admin import AdminHandler
from bot.config import Settings


@pytest.fixture
def admin_handler():
    """Create AdminHandler with mocks."""
    bot = MagicMock()
    db = MagicMock()
    config = MagicMock(spec=Settings)
    config.DEMO_TRAFFIC_GB = 5.0
    config.FORUM_ENABLED = False
    config.FORUM_GROUP_ID = None
    
    handler = AdminHandler(bot, db, config)
    # Clean shared class-level state between tests
    AdminHandler._pending_broadcasts.clear()
    return handler


class TestAdminBroadcastEdgeCases:
    """Test broadcast handler edge cases."""
    
    def test_broadcast_preview_empty_args(self, admin_handler):
        """Test broadcast_preview with no text sends error."""
        admin_handler.broadcast_preview('admin_id', [])
        
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Укажите текст' in text
    
    def test_broadcast_preview_none_users(self, admin_handler):
        """Test broadcast_preview handles get_all_users returning None."""
        type(admin_handler.db).get_all_users = MagicMock(return_value=None)
        
        # Should not crash; treats as empty list
        admin_handler.broadcast_preview('admin_id', ['hello'])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Получателей: 0' in text
    
    def test_broadcast_preview_with_users(self, admin_handler):
        """Test broadcast_preview calculates active users."""
        u1 = MagicMock(status='demo')
        u2 = MagicMock(status='banned')
        u3 = MagicMock(status='paid')
        admin_handler.db.get_all_users.return_value = [u1, u2, u3]
        
        admin_handler.broadcast_preview('admin_id', ['hello'])
        
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Получателей: 2' in text
        assert 'hello' in text
    
    def test_broadcast_confirm_no_pending(self, admin_handler):
        """Test broadcast_confirm with no pending message."""
        admin_handler.broadcast_confirm('admin_id', [])
        
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Нет подготовленного сообщения' in text
    
    def test_broadcast_confirm_empty_user_list(self, admin_handler):
        """Test broadcast_confirm when no active users exist."""
        admin_handler._pending_broadcasts['admin_id'] = 'hello'
        admin_handler.db.get_all_users.return_value = []
        
        admin_handler.broadcast_confirm('admin_id', [])
        
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Отправлено: 0' in text
        assert 'admin_id' not in admin_handler._pending_broadcasts
    
    def test_broadcast_confirm_failure_handling(self, admin_handler):
        """Test broadcast_confirm counts failures."""
        admin_handler._pending_broadcasts['admin_id'] = 'hello'
        u1 = MagicMock(chat_id='111', status='demo')
        u2 = MagicMock(chat_id='222', status='paid')
        admin_handler.db.get_all_users.return_value = [u1, u2]
        # side_effect: u1 success, u2 exception, admin final message default mock
        admin_handler.bot.send_message.side_effect = [True, Exception("blocked"), True]
        
        admin_handler.broadcast_confirm('admin_id', [])
        
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Отправлено: 1' in text
        assert 'Ошибок: 1' in text
    
    def test_broadcast_cancel_no_active(self, admin_handler):
        """Test broadcast_cancel when nothing pending."""
        admin_handler.broadcast_cancel('admin_id', [])
        
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Нет активной рассылки' in text
    
    def test_broadcast_cancel_removes_pending(self, admin_handler):
        """Test broadcast_cancel deletes pending broadcast."""
        admin_handler._pending_broadcasts['admin_id'] = 'msg'
        admin_handler.broadcast_cancel('admin_id', [])
        
        assert 'admin_id' not in admin_handler._pending_broadcasts


class TestAdminStatsEdgeCases:
    """Test stats handler edge cases."""
    
    def test_show_overall_stats_empty(self, admin_handler):
        """Test show_overall_stats with empty stats."""
        admin_handler.db.get_stats.return_value = {
            'total': 0,
            'by_status': {},
            'by_platform': {}
        }
        
        admin_handler.show_overall_stats('admin_id', [])
        
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Всего пользователей: <b>0</b>' in text
    
    def test_show_overall_stats_with_none_platform(self, admin_handler):
        """Test show_overall_stats handles None platform counts."""
        admin_handler.db.get_stats.return_value = {
            'total': 1,
            'by_status': {'demo': 1},
            'by_platform': {'ios': 1, None: 1}
        }
        
        # Sorting on None key may crash in Python < 3.10? Actually dict with None key sorts fine.
        admin_handler.show_overall_stats('admin_id', [])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'ios' in text
    
    def test_backup_db_success(self, admin_handler):
        """Test backup_db creates backup and reports size."""
        admin_handler.db.db_path = '/tmp/test.db'
        
        with patch('os.makedirs') as mock_makedirs, \
             patch('shutil.copy2') as mock_copy, \
             patch('os.path.getsize', return_value=2 * 1024 * 1024):
            
            admin_handler.backup_db('admin_id', [])
            
            mock_makedirs.assert_called_once()
            mock_copy.assert_called_once()
            text = admin_handler.bot.send_message.call_args[1]['text']
            assert 'Бэкап создан' in text
            assert '2.00 MB' in text
    
    def test_backup_db_failure(self, admin_handler):
        """Test backup_db handles copy failure."""
        admin_handler.db.db_path = '/tmp/test.db'
        
        with patch('shutil.copy2', side_effect=IOError("disk full")):
            admin_handler.backup_db('admin_id', [])
            
            text = admin_handler.bot.send_message.call_args[1]['text']
            assert 'Ошибка создания бэкапа' in text


class TestAdminUsersEdgeCases:
    """Test user management handler edge cases."""
    
    def test_show_pending_none_users(self, admin_handler):
        """Test show_pending handles None gracefully."""
        admin_handler.db.get_pending_users.return_value = None
        
        admin_handler.show_pending('admin_id', [])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Нет ожидающих заявок' in text
    
    def test_approve_user_no_args(self, admin_handler):
        """Test approve_user with no args sends error."""
        admin_handler.approve_user('admin_id', [])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Укажите пользователя' in text
    
    def test_reject_user_not_found(self, admin_handler):
        """Test reject_user when target not found."""
        admin_handler.db.get_user.return_value = None
        admin_handler.db.get_user_by_username.return_value = None
        
        admin_handler.reject_user('admin_id', ['@unknown'])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Пользователь не найден' in text
    
    def test_ban_user_resolve_by_username(self, admin_handler):
        """Test ban_user resolves @username correctly."""
        from bot.config import UserState
        user = MagicMock()
        user.chat_id = '123'
        user.username = 'testuser'
        user.email = None
        user.uuid = None
        admin_handler.db.get_user_by_username.return_value = user

        with patch('bot.handlers.admin.users.StateMachine') as MockSM:
            admin_handler.ban_user('admin_id', ['@testuser'])
            MockSM.return_value.transition.assert_called_once_with('123', UserState.BANNED)
    
    def test_show_user_no_traffic(self, admin_handler):
        """Test show_user when X-UI traffic is unavailable."""
        user = MagicMock()
        user.email = 'test@example.com'
        user.username = 'testuser'
        user.chat_id = '123456'
        user.status = 'demo'
        user.lang = 'ru'
        user.platform = 'ios'
        user.uuid = 'uuid-1'
        
        admin_handler.db.get_user.return_value = user
        admin_handler.bot.services.get.return_value = None
        
        admin_handler.show_user('admin_id', ['123456'])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert '@testuser' in text
    
    def test_set_limit_invalid_number(self, admin_handler):
        """Test set_limit with non-numeric limit."""
        user = MagicMock()
        user.chat_id = '123'
        admin_handler.db.get_user.return_value = user
        
        admin_handler.set_limit('admin_id', ['123', 'abc'])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'должно быть числом' in text
    
    def test_set_limit_user_not_found(self, admin_handler):
        """Test set_limit when target not found."""
        admin_handler.db.get_user_by_username.return_value = None
        admin_handler.db.get_user.return_value = None
        
        admin_handler.set_limit('admin_id', ['@unknown', '3'])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Пользователь не найден' in text
    
    def test_grant_100gb_get_user_returns_none(self, admin_handler):
        """Test grant_100gb handles db.get_user returning None after target resolved."""
        target = MagicMock()
        target.chat_id = '123'
        target.username = 'testuser'
        target.email = 'test@example.com'
        target.quota_gb = 5.0
        
        admin_handler.db.get_user_by_username.return_value = target
        # get_user is called only inside grant_100gb when resolved by username
        admin_handler.db.get_user.return_value = None
        
        # Should not crash; falls back to target.quota_gb
        admin_handler.grant_100gb('admin_id', ['@testuser'])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert '5.0' in text
    
    def test_grant_100gb_success(self, admin_handler):
        """Test successful grant_100gb."""
        target = MagicMock()
        target.chat_id = '123'
        target.username = 'testuser'
        target.email = 'test@example.com'
        target.quota_gb = 5.0
        
        admin_handler.db.get_user.return_value = target
        
        mock_xui = MagicMock()
        mock_xui.get_client_sync.return_value = {
            'email': 'test@example.com',
            'totalGB': 5 * 1024**3,
            'id': 'uuid',
            'inbound_id': 1
        }
        admin_handler.bot.services.get.return_value = mock_xui
        
        admin_handler.grant_100gb('admin_id', ['@testuser'])
        
        assert target.quota_gb == 105.0
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert '105' in text
    
    def test_approve_payment_creates_subscription(self, admin_handler):
        """Test approve_payment creates subscription."""
        target = MagicMock()
        target.chat_id = '123'
        target.username = 'testuser'
        target.lang = 'ru'
        
        admin_handler.db.get_user_by_username.return_value = target
        admin_handler.db.create_subscription = MagicMock()
        
        with patch('bot.handlers.admin.users.NotificationService') as MockNotifier, \
             patch('bot.services.billing.StateMachine') as MockSM:
            
            admin_handler.approve_payment('admin_id', ['@testuser'])
            
            MockSM.return_value.transition.assert_called_once()
            admin_handler.db.create_subscription.assert_called_once()
            MockNotifier.return_value.notify_payment_approved.assert_called_once()
    
    def test_show_active_users_empty(self, admin_handler):
        """Test show_active_users when no active users."""
        admin_handler.db.get_all_users.return_value = []
        
        admin_handler.show_active_users('admin_id', [])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'нет записей' in text
    
    def test_show_all_users_limit_50(self, admin_handler):
        """Test show_all_users truncates at 50 users."""
        users = []
        for i in range(60):
            u = MagicMock()
            u.chat_id = str(i)
            u.username = f'user{i}'
            u.status = 'demo'
            users.append(u)
        
        admin_handler.db.get_all_users.return_value = users
        
        admin_handler.show_all_users('admin_id', [])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'ещё 10 пользователей' in text
    
    def test_show_user_with_non_string_chat_id(self, admin_handler):
        """Test show_user handles integer chat_id safely."""
        user = MagicMock()
        user.email = None
        user.username = 'testuser'
        user.chat_id = 123456  # integer
        user.status = 'demo'
        user.lang = 'ru'
        user.platform = 'ios'
        user.uuid = 'uuid'
        
        admin_handler.db.get_user.return_value = user
        
        admin_handler.show_user('admin_id', ['123456'])
        text = admin_handler.bot.send_message.call_args[1]['text']
        assert 'testuser' in text or '123456' in text


class TestAdminHandlerResolveTarget:
    """Test _resolve_target edge cases."""
    
    def test_resolve_target_empty_string(self, admin_handler):
        """Test _resolve_target with empty string falls back to get_user."""
        admin_handler.db.get_user.return_value = None
        result = admin_handler._resolve_target('')
        assert result is None
        admin_handler.db.get_user.assert_called_once_with('')
    
    def test_resolve_target_username_with_at(self, admin_handler):
        """Test _resolve_target strips or keeps @ for username lookup."""
        user = MagicMock()
        admin_handler.db.get_user_by_username.return_value = user
        result = admin_handler._resolve_target('@testuser')
        assert result == user
        admin_handler.db.get_user_by_username.assert_called_once_with('@testuser')


class TestAdminHandlerCanHandle:
    """Test can_handle edge cases."""
    
    def test_can_handle_missing_text(self, admin_handler):
        """Test can_handle returns False for updates without text."""
        update = {'message': {'from': {'id': '123'}, 'chat': {'id': '123'}}}
        assert admin_handler.can_handle(update) is False
    
    def test_can_handle_non_admin(self, admin_handler):
        """Test can_handle returns False for non-admin."""
        admin_handler._is_admin = MagicMock(return_value=False)
        update = {'message': {'text': '/stats', 'from': {'id': '999'}, 'chat': {'id': '999'}}}
        assert admin_handler.can_handle(update) is False
