"""End-to-end integration tests for complete user flows"""

from unittest.mock import Mock, patch

import pytest

from bot.config import UserState
from bot.models import User
from bot.handlers.callbacks import CallbackHandler
from bot.handlers.commands import CommandHandler

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)


class TestNewUserToKeyFlow:
    """Test complete flow: new user → demo request → approval → get key"""
    
    def test_new_user_registration(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test /start creates new user"""
        handler = CommandHandler(mock_telegram_bot, mock_bot_db, mock_config)
        
        update = {
            'message': {
                'text': '/start',
                'chat': {'id': 123456},
                'from': {'id': 123456, 'username': 'newuser'}
            }
        }
        
        handler.handle(update)
        
        user = mock_bot_db.get_user('123456')
        assert user is not None
        assert user.username == 'newuser'
        assert user.status == UserState.NEW.value
    
    def test_request_demo_callback(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test request demo callback auto-approves realistic accounts."""
        # Clear rate limit state to avoid interference from other tests
        from bot.handlers.callbacks.user import DemoRequestHandler
        DemoRequestHandler._demo_request_times.clear()

        # Setup: User exists with username (auto-approved)
        mock_bot_db.save_user(User(chat_id='123456', username='testuser', status='new'))

        handler = CallbackHandler(mock_telegram_bot, mock_bot_db, mock_config)

        update = {
            'callback_query': {
                'data': 'request_demo',
                'message': {'chat': {'id': 123456}},
                'from': {'id': 123456, 'username': 'testuser'}
            }
        }

        handler.handle(update)

        user = mock_bot_db.get_user('123456')
        assert user.status == UserState.PLATFORM_SELECT.value
    
    def test_admin_approve_creates_key(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test admin approval generates VPN key"""
        # Setup: User pending
        mock_bot_db.save_user(User(
            chat_id='123456',
            username='testuser',
            status='pending_demo'
        ))
        
        mock_config.is_admin = Mock(return_value=True)
        
        from bot.handlers.admin import AdminHandler
        handler = AdminHandler(mock_telegram_bot, mock_bot_db, mock_config)
        
        update = {
            'message': {
                'text': '/approve 123456',
                'chat': {'id': 1652899},
                'from': {'id': 1652899}
            }
        }
        
        handler.handle(update)
        
        user = mock_bot_db.get_user('123456')
        # After approval, user should be in PLATFORM_SELECT state
        assert user.status == UserState.PLATFORM_SELECT.value


class TestSupportTicketFlow:
    """Test support ticket flow: user → support → admin reply → user receives"""
    
    def test_user_opens_support_ticket(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test user can open support ticket"""
        mock_bot_db.save_user(User(
            chat_id='123456',
            username='testuser',
            status='demo',
            support_topic_id=None
        ))
        
        handler = CallbackHandler(mock_telegram_bot, mock_bot_db, mock_config)
        
        update = {
            'callback_query': {
                'data': 'support',
                'message': {'chat': {'id': 123456}},
                'from': {'id': 123456}
            }
        }
        
        handler.handle(update)
        
        # Should send message asking for problem description
        assert mock_telegram_bot.send_message.called
    
    def test_admin_reply_forwarded_to_user(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test admin reply is forwarded to user in PM mode"""
        # Setup: Admin user in DB
        mock_bot_db.save_user(User(
            chat_id='1652899',
            username='admin',
            status='demo'
        ))
        
        # Setup: Message map (admin's forwarded message → user's original)
        mock_bot_db.log_message_map(500, '123456', 100)
        
        from bot.handlers.messages import MessageHandler
        handler = MessageHandler(mock_telegram_bot, mock_bot_db, mock_config)
        
        update = {
            'message': {
                'text': 'Here is your solution',
                'chat': {'id': 1652899},
                'from': {'id': 1652899},
                'reply_to_message': {'message_id': 500}
            }
        }
        
        handler.handle(update)
        
        # Should forward reply to user
        calls = mock_telegram_bot.send_message.call_args_list
        user_calls = [c for c in calls if c[1].get('chat_id') == '123456']
        assert len(user_calls) > 0


class TestUpgradeToFullFlow:
    """Test upgrade flow: demo → payment → full version"""
    
    def test_user_requests_full_version(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test user can request full version"""
        mock_bot_db.save_user(User(
            chat_id='123456',
            username='testuser',
            status='demo',
            quota_gb=5.0
        ))
        
        handler = CallbackHandler(mock_telegram_bot, mock_bot_db, mock_config)
        
        update = {
            'callback_query': {
                'data': 'full',
                'message': {'chat': {'id': 123456}},
                'from': {'id': 123456}
            }
        }
        
        handler.handle(update)
        
        # Should show payment options
        assert mock_telegram_bot.send_message.called
    
    def test_admin_approves_payment(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test admin approval extends subscription"""
        mock_bot_db.save_user(User(
            chat_id='123456',
            username='testuser',
            status='demo',
            quota_gb=5.0,
            email='test@nekovo.ru'
        ))
        
        mock_config.is_admin = Mock(return_value=True)
        
        from bot.handlers.admin import AdminHandler
        handler = AdminHandler(mock_telegram_bot, mock_bot_db, mock_config)
        
        update = {
            'message': {
                'text': '/approve_payment 123456 30',
                'chat': {'id': 1652899},
                'from': {'id': 1652899}
            }
        }
        
        handler.handle(update)
        
        user = mock_bot_db.get_user('123456')
        assert user.status == 'paid'


class TestErrorHandlingFlows:
    """Test error handling in various flows"""
    
    def test_duplicate_user_creation(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test creating user with existing chat_id updates instead of error"""
        # First creation
        mock_bot_db.save_user(User(chat_id='123456', username='oldname', status='new'))
        
        # Second creation with same chat_id
        mock_bot_db.save_user(User(chat_id='123456', username='newname', status='demo'))
        
        user = mock_bot_db.get_user('123456')
        assert user.username == 'newname'
        assert user.status == 'demo'
    
    def test_callback_with_invalid_data(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test handling invalid callback data gracefully"""
        handler = CallbackHandler(mock_telegram_bot, mock_bot_db, mock_config)
        
        update = {
            'callback_query': {
                'data': 'invalid_callback_data',
                'message': {'chat': {'id': 123456}},
                'from': {'id': 123456}
            }
        }
        
        # Should not raise exception
        handler.handle(update)
    
    def test_nonexistent_user_lookup(self, mock_bot_db, mock_config, mock_telegram_bot):
        """Test lookup of non-existent user returns None"""
        user = mock_bot_db.get_user('999999999')
        assert user is None
