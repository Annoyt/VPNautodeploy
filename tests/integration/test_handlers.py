"""Integration tests for handlers"""

import os
import tempfile
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot.config import Settings, UserState
from bot.core.bot import Bot
from bot.core.database import Database, User
from bot.handlers.callbacks import CallbackHandler
from bot.handlers.commands import CommandHandler
from bot.handlers.messages import MessageHandler
from bot.handlers.admin import AdminHandler
from bot.handlers.forum import ForumHandler

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)


class TestHandlerIntegration:
    """Integration tests for handlers with services"""
    
    @pytest.fixture
    def temp_db(self):
        """Create temporary database"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        yield db_path
        os.unlink(db_path)
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config"""
        config = Mock(spec=Settings)
        config.BOT_TOKEN = 'test_token'
        config.DB_PATH = '/tmp/test.db'
        config.XUI_DB_PATH = '/tmp/test_xui.db'
        config.SUPER_ADMIN_ID = '1652899'
        config.FORUM_ENABLED = False
        config.FORUM_GROUP_ID = None
        config.DEMO_TRAFFIC_GB = 5
        config.DEMO_DAYS = 7
        config.ENTRY_NODE_IP = '1.2.3.4'
        config.REALITY_PUBLIC_KEY = 'test_key'
        config.SNI_VALUE = 'test.com'
        config.SID_VALUE = ''
        config.MODE = 'PM'
        config.TOPIC_REQUESTS = 15
        config.TOPIC_SUPPORT = 17
        config.TOPIC_PAYMENTS = 16
        config.TOPIC_STATS = 18
        config.TOPIC_SOLVED = 37
        config.is_admin = Mock(return_value=False)
        return config
    
    @pytest.fixture
    def mock_bot(self, mock_config, temp_db):
        """Create mock bot"""
        mock_config.DB_PATH = temp_db
        
        bot = Mock()
        bot.db = Database(temp_db)
        bot.config = mock_config
        bot.handlers = []
        bot.send_message = Mock(return_value={'message_id': 123})
        bot.send_message_to_topic = Mock(return_value={'message_id': 456})
        bot.answer_callback_query = Mock(return_value=True)
        bot.create_forum_topic = Mock(return_value=789)
        bot.forward_message = Mock(return_value={'message_id': 999})
        
        # Mock services with async methods
        xui_mock = Mock()
        xui_mock.sync_user = AsyncMock(return_value=True)
        xui_mock.remove_client = Mock(return_value=True)
        xui_mock.get_client_traffic = Mock(return_value={'total': 1000})
        
        bot.services = {'xui': xui_mock}
        return bot
    
    # ===== CommandHandler Tests =====
    
    def test_command_handler_start_new_user(self, mock_bot, mock_config):
        """Test /start command creates new user"""
        handler = CommandHandler(mock_bot, mock_bot.db, mock_config)
        
        update = {
            'message': {
                'text': '/start',
                'chat': {'id': 123456},
                'from': {'id': 123456, 'username': 'testuser'}
            }
        }
        
        assert handler.can_handle(update) is True
        handler.handle(update)
        
        user = mock_bot.db.get_user('123456')
        assert user is not None
        assert user.username == 'testuser'
    
    def test_command_handler_help_ru(self, mock_bot, mock_config):
        """Test /help command returns Russian text for ru user"""
        handler = CommandHandler(mock_bot, mock_bot.db, mock_config)
        mock_bot.db.save_user(User(chat_id='123', lang='ru'))
        
        update = {
            'message': {
                'text': '/help',
                'chat': {'id': 123},
                'from': {'id': 123}
            }
        }
        
        handler.handle(update)
        call_args = mock_bot.send_message.call_args
        assert 'Доступные команды' in call_args[1]['text']
    
    def test_command_handler_help_en(self, mock_bot, mock_config):
        """Test /help command returns English text for en user"""
        handler = CommandHandler(mock_bot, mock_bot.db, mock_config)
        mock_bot.db.save_user(User(chat_id='123', lang='en'))
        
        update = {
            'message': {
                'text': '/help',
                'chat': {'id': 123},
                'from': {'id': 123}
            }
        }
        
        handler.handle(update)
        call_args = mock_bot.send_message.call_args
        assert 'Available Commands' in call_args[1]['text']
    
    def test_command_handler_mykey_no_key(self, mock_bot, mock_config):
        """Test /mykey when user has no key shows localized error"""
        handler = CommandHandler(mock_bot, mock_bot.db, mock_config)
        mock_bot.db.save_user(User(chat_id='123', lang='ru'))
        
        update = {
            'message': {
                'text': '/mykey',
                'chat': {'id': 123},
                'from': {'id': 123}
            }
        }
        
        handler.handle(update)
        call_args = mock_bot.send_message.call_args
        assert 'нет активного VPN ключа' in call_args[1]['text']
    
    # ===== CallbackHandler Tests =====
    
    def test_callback_handler_can_handle(self, mock_bot, mock_config):
        """Test callback handler detection"""
        handler = CallbackHandler(mock_bot, mock_bot.db, mock_config)
        
        callback_update = {'callback_query': {'data': 'test'}}
        message_update = {'message': {'text': 'test'}}
        
        assert handler.can_handle(callback_update) is True
        assert handler.can_handle(message_update) is False
    
    def test_callback_handler_request_demo(self, mock_bot, mock_config):
        """Test request demo callback auto-approves realistic accounts."""
        from bot.handlers.callbacks.user import DemoRequestHandler
        DemoRequestHandler._demo_request_times.clear()

        handler = CallbackHandler(mock_bot, mock_bot.db, mock_config)

        user = User(chat_id='123456', username='testuser', status='new')
        mock_bot.db.save_user(user)

        update = {
            'callback_query': {
                'data': 'request_demo',
                'message': {'chat': {'id': 123456}},
                'from': {'id': 123456, 'username': 'testuser'}
            }
        }

        handler.handle(update)

        updated = mock_bot.db.get_user('123456')
        assert updated.status == UserState.PLATFORM_SELECT.value
    
    def test_callback_my_key_routes(self, mock_bot, mock_config):
        """Test 'my_key' callback (without colon) routes correctly"""
        handler = CallbackHandler(mock_bot, mock_bot.db, mock_config)
        mock_bot.db.save_user(User(chat_id='123', uuid='test-uuid', email='t@v.com'))
        
        update = {
            'callback_query': {
                'data': 'my_key',
                'message': {'chat': {'id': 123}},
                'from': {'id': 123}
            }
        }
        
        handler.handle(update)
        assert mock_bot.send_message.called
    
    def test_callback_support_routes(self, mock_bot, mock_config):
        """Test 'support' callback (without colon) routes correctly"""
        handler = CallbackHandler(mock_bot, mock_bot.db, mock_config)
        mock_bot.db.save_user(User(chat_id='123'))
        
        update = {
            'callback_query': {
                'data': 'support',
                'message': {'chat': {'id': 123}},
                'from': {'id': 123}
            }
        }
        
        handler.handle(update)
        assert mock_bot.send_message.called
    
    def test_callback_stats_routes(self, mock_bot, mock_config):
        """Test 'stats' callback (without colon) routes correctly"""
        handler = CallbackHandler(mock_bot, mock_bot.db, mock_config)
        mock_bot.db.save_user(User(chat_id='123'))
        
        update = {
            'callback_query': {
                'data': 'stats',
                'message': {'chat': {'id': 123}},
                'from': {'id': 123}
            }
        }
        
        handler.handle(update)
        assert mock_bot.send_message.called
    
    def test_callback_full_routes(self, mock_bot, mock_config):
        """Test 'full' callback routes correctly"""
        handler = CallbackHandler(mock_bot, mock_bot.db, mock_config)
        mock_bot.db.save_user(User(chat_id='123'))

        update = {
            'callback_query': {
                'data': 'full',
                'message': {'chat': {'id': 123}},
                'from': {'id': 123}
            }
        }

        handler.handle(update)
        call_args = mock_bot.send_message.call_args
        assert '💳' in call_args[1]['text']
    
    def test_callback_set_lang(self, mock_bot, mock_config):
        """Test language switch callback"""
        handler = CallbackHandler(mock_bot, mock_bot.db, mock_config)
        mock_bot.db.save_user(User(chat_id='123', lang='ru'))
        
        update = {
            'callback_query': {
                'data': 'set_lang:en',
                'message': {'chat': {'id': 123}},
                'from': {'id': 123, 'username': 'test'}
            }
        }
        
        handler.handle(update)
        updated = mock_bot.db.get_user('123')
        assert updated.lang == 'en'
    
    # ===== MessageHandler Tests =====
    
    def test_message_handler_can_handle(self, mock_bot, mock_config):
        """Test message handler detection"""
        handler = MessageHandler(mock_bot, mock_bot.db, mock_config)
        
        text_message = {'message': {'text': 'Hello'}}
        command_message = {'message': {'text': '/start'}}
        topic_message = {'message': {'text': 'Hello', 'is_topic_message': True}}
        
        assert handler.can_handle(text_message) is True
        assert handler.can_handle(command_message) is False
        assert handler.can_handle(topic_message) is False
    
    def test_admin_pm_reply_forwarding(self, mock_bot, mock_config):
        """Test admin reply to forwarded support message in PM mode"""
        mock_config.is_admin = Mock(return_value=True)
        handler = MessageHandler(mock_bot, mock_bot.db, mock_config)
        
        # Create message map entry
        mock_bot.db.log_message_map(500, '12345', 100)
        mock_bot.db.save_user(User(chat_id='1652899'))
        
        update = {
            'message': {
                'text': 'Here is your answer',
                'chat': {'id': 1652899},
                'from': {'id': 1652899},
                'reply_to_message': {'message_id': 500}
            }
        }
        
        handler.handle(update)
        
        # Should forward to user 12345
        calls = mock_bot.send_message.call_args_list
        user_reply = [c for c in calls if c[1].get('chat_id') == '12345']
        assert len(user_reply) > 0
        assert 'Here is your answer' in user_reply[0][1]['text']
    
    # ===== AdminHandler Tests =====
    
    def test_admin_handler_can_handle_admin(self, mock_bot, mock_config):
        """Test admin handler with admin user"""
        mock_config.is_admin = Mock(return_value=True)
        handler = AdminHandler(mock_bot, mock_bot.db, mock_config)
        
        update = {
            'message': {
                'text': '/pending',
                'chat': {'id': 1652899},
                'from': {'id': 1652899}
            }
        }
        
        assert handler.can_handle(update) is True
    
    def test_admin_handler_can_handle_non_admin(self, mock_bot, mock_config):
        """Test admin handler with non-admin user"""
        mock_config.is_admin = Mock(return_value=False)
        handler = AdminHandler(mock_bot, mock_bot.db, mock_config)
        
        update = {
            'message': {
                'text': '/pending',
                'chat': {'id': 123456},
                'from': {'id': 123456}
            }
        }
        
        assert handler.can_handle(update) is False
    
    def test_admin_broadcast(self, mock_bot, mock_config):
        """Test /broadcast sends to all users"""
        mock_config.is_admin = Mock(return_value=True)
        handler = AdminHandler(mock_bot, mock_bot.db, mock_config)
        
        mock_bot.db.save_user(User(chat_id='1', status='demo'))
        mock_bot.db.save_user(User(chat_id='2', status='demo'))
        mock_bot.db.save_user(User(chat_id='3', status='paid'))
        
        handler.broadcast_preview('admin', ['Hello', 'everyone!'])
        
        # Preview sends 1 message to admin
        assert mock_bot.send_message.call_count == 1
        
        # Confirm broadcast
        handler.broadcast_confirm('admin', [])
        
        # Should send to 3 users + preview + completion = 5 calls total
        assert mock_bot.send_message.call_count == 5
    
    # ===== ForumHandler Tests =====
    
    def test_forum_handler_disabled_when_forum_off(self, mock_bot, mock_config):
        """Test forum handler is disabled when forum disabled"""
        mock_config.FORUM_ENABLED = False
        handler = ForumHandler(mock_bot, mock_bot.db, mock_config)
        
        assert handler.disabled is True
        
        update = {
            'message': {
                'text': 'Reply',
                'is_topic_message': True,
                'chat': {'id': -1001234567890},
                'from': {'id': 1652899}
            }
        }
        
        assert handler.can_handle(update) is False
    
    # ===== Handler Registration Order =====
    
    def test_handler_registration_order(self, mock_bot, mock_config):
        """Test that handlers can be registered"""
        handlers = [
            CommandHandler(mock_bot, mock_bot.db, mock_config),
            CallbackHandler(mock_bot, mock_bot.db, mock_config),
            AdminHandler(mock_bot, mock_bot.db, mock_config),
            MessageHandler(mock_bot, mock_bot.db, mock_config),
        ]
        
        for handler in handlers:
            mock_bot.handlers.append(handler)
        
        assert len(mock_bot.handlers) == 4
        assert isinstance(mock_bot.handlers[0], CommandHandler)
        assert isinstance(mock_bot.handlers[1], CallbackHandler)
        assert isinstance(mock_bot.handlers[2], AdminHandler)
        assert isinstance(mock_bot.handlers[3], MessageHandler)
