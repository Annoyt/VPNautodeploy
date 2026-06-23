"""Tests for core Bot class."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from bot.core.bot import Bot
from bot.config.settings import Settings


class TestBotInitialization:
    """Test Bot initialization."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = Mock(spec=Settings)
        config.DB_PATH = ":memory:"
        return config
    
    def test_bot_initialization(self, mock_config):
        """Test bot creates all required components."""
        with patch('bot.core.bot.TelegramClient') as mock_client:
            with patch('bot.core.bot.Database') as mock_db:
                with patch('bot.core.bot.PollingService') as mock_polling:
                    mock_client_instance = Mock()
                    mock_client.return_value = mock_client_instance
                    
                    mock_db_instance = Mock()
                    mock_db.return_value = mock_db_instance
                    
                    mock_polling_instance = Mock()
                    mock_polling.return_value = mock_polling_instance
                    
                    bot = Bot("test_token", mock_config)
                    
                    assert bot.token == "test_token"
                    assert bot.config == mock_config
                    assert bot.client == mock_client_instance
                    assert bot.db == mock_db_instance
                    assert bot.handlers == []
                    mock_client.assert_called_once_with("test_token")
                    mock_db.assert_called_once_with(":memory:")


class TestBotHandlerRegistration:
    """Test handler registration."""
    
    @pytest.fixture
    def bot(self):
        """Create bot with mocked dependencies."""
        with patch('bot.core.bot.TelegramClient'):
            with patch('bot.core.bot.Database'):
                with patch('bot.core.bot.PollingService'):
                    config = Mock(spec=Settings)
                    config.DB_PATH = ":memory:"
                    return Bot("test_token", config)
    
    def test_register_single_handler(self, bot):
        """Test registering a single handler."""
        handler = Mock()
        handler.__class__.__name__ = "TestHandler"
        
        bot.register_handler(handler)
        
        assert len(bot.handlers) == 1
        assert handler in bot.handlers
        assert handler.bot == bot
        assert handler.db == bot.db
        assert handler.config == bot.config
        
    def test_register_multiple_handlers(self, bot):
        """Test registering multiple handlers."""
        handler1 = Mock()
        handler1.__class__.__name__ = "Handler1"
        handler2 = Mock()
        handler2.__class__.__name__ = "Handler2"
        
        bot.register_handler(handler1)
        bot.register_handler(handler2)
        
        assert len(bot.handlers) == 2
        assert handler1 in bot.handlers
        assert handler2 in bot.handlers


class TestBotHandleUpdate:
    """Test update handling."""
    
    @pytest.fixture
    def bot(self):
        """Create bot with mocked dependencies."""
        with patch('bot.core.bot.TelegramClient'):
            with patch('bot.core.bot.Database'):
                with patch('bot.core.bot.PollingService'):
                    config = Mock(spec=Settings)
                    config.DB_PATH = ":memory:"
                    return Bot("test_token", config)
    
    def test_handle_update_matching_handler(self, bot):
        """Test update routed to matching handler."""
        handler = Mock()
        handler.can_handle = Mock(return_value=True)
        handler.handle = Mock()
        handler.__class__.__name__ = "TestHandler"
        
        bot.register_handler(handler)
        
        update = {'message': {'chat': {'id': 123}}}
        bot._handle_update(update)
        
        handler.can_handle.assert_called_once_with(update)
        handler.handle.assert_called_once_with(update)
        
    def test_handle_update_no_matching_handler(self, bot):
        """Test update with no matching handler."""
        handler = Mock()
        handler.can_handle = Mock(return_value=False)
        handler.__class__.__name__ = "TestHandler"
        
        bot.register_handler(handler)
        
        update = {'message': {'chat': {'id': 123}}}
        bot._handle_update(update)
        
        handler.can_handle.assert_called_once_with(update)
        handler.handle.assert_not_called()
        
    def test_handle_update_first_matching_handler_wins(self, bot):
        """Test only first matching handler receives update."""
        handler1 = Mock()
        handler1.can_handle = Mock(return_value=True)
        handler1.handle = Mock()
        handler1.__class__.__name__ = "Handler1"
        
        handler2 = Mock()
        handler2.can_handle = Mock(return_value=True)
        handler2.handle = Mock()
        handler2.__class__.__name__ = "Handler2"
        
        bot.register_handler(handler1)
        bot.register_handler(handler2)
        
        update = {'message': {'chat': {'id': 123}}}
        bot._handle_update(update)
        
        handler1.handle.assert_called_once()
        handler2.handle.assert_not_called()
        
    def test_handle_update_handler_exception(self, bot):
        """Test exception in handler is caught and logged."""
        handler = Mock()
        handler.can_handle = Mock(return_value=True)
        handler.handle = Mock(side_effect=Exception("Handler error"))
        handler.__class__.__name__ = "TestHandler"
        
        bot.register_handler(handler)
        
        update = {'message': {'chat': {'id': 123}}}
        # Should not raise
        bot._handle_update(update)
        
        handler.handle.assert_called_once()


class TestBotLifecycle:
    """Test bot start/stop lifecycle."""
    
    @pytest.fixture
    def bot(self):
        """Create bot with mocked dependencies."""
        with patch('bot.core.bot.TelegramClient'):
            with patch('bot.core.bot.Database'):
                with patch('bot.core.bot.PollingService') as mock_polling:
                    mock_polling_instance = Mock()
                    mock_polling.return_value = mock_polling_instance
                    
                    config = Mock(spec=Settings)
                    config.DB_PATH = ":memory:"
                    bot = Bot("test_token", config)
                    bot._polling_service = mock_polling_instance
                    return bot
    
    def test_start_bot(self, bot):
        """Test starting bot starts polling."""
        bot.start()
        bot._polling_service.start.assert_called_once()
        
    def test_stop_bot(self, bot):
        """Test stopping bot stops polling."""
        bot.stop()
        bot._polling_service.stop.assert_called_once()


class TestBotSendMethods:
    """Test bot send methods delegate to client."""
    
    @pytest.fixture
    def bot(self):
        """Create bot with mocked client."""
        with patch('bot.core.bot.TelegramClient') as mock_client:
            with patch('bot.core.bot.Database'):
                with patch('bot.core.bot.PollingService'):
                    mock_client_instance = Mock()
                    mock_client.return_value = mock_client_instance
                    
                    config = Mock(spec=Settings)
                    config.DB_PATH = ":memory:"
                    bot = Bot("test_token", config)
                    return bot
    
    def test_send_message(self, bot):
        """Test send_message delegates to client."""
        bot.client.send_message = Mock(return_value={'message_id': 123})
        
        result = bot.send_message(
            chat_id="123456",
            text="Hello",
            parse_mode="HTML"
        )
        
        bot.client.send_message.assert_called_once_with(
            chat_id="123456",
            text="Hello",
            parse_mode="HTML",
            reply_markup=None
        )
        assert result == {'message_id': 123}
        
    def test_send_message_to_topic(self, bot):
        """Test send_message_to_topic delegates to client."""
        bot.client.send_message = Mock(return_value={'message_id': 456})
        
        result = bot.send_message_to_topic(
            chat_id="123456",
            message_thread_id=99,
            text="Topic message"
        )
        
        bot.client.send_message.assert_called_once_with(
            chat_id="123456",
            text="Topic message",
            message_thread_id=99
        )
        
    def test_forward_message(self, bot):
        """Test forward_message delegates to client."""
        bot.client.forward_message = Mock(return_value={'message_id': 789})
        
        result = bot.forward_message(
            chat_id="123456",
            from_chat_id="654321",
            message_id=111
        )
        
        bot.client.forward_message.assert_called_once_with(
            chat_id="123456",
            from_chat_id="654321",
            message_id=111
        )
        assert result == {'message_id': 789}
        
    def test_answer_callback_query(self, bot):
        """Test answer_callback_query delegates to client."""
        bot.client.answer_callback_query = Mock(return_value=True)
        
        result = bot.answer_callback_query(
            callback_query_id="query_123",
            text="Answer",
            show_alert=True
        )
        
        bot.client.answer_callback_query.assert_called_once_with(
            "query_123", "Answer", True
        )
        assert result is True
        
    def test_delete_message(self, bot):
        """Test delete_message delegates to client."""
        bot.client.delete_message = Mock(return_value=True)
        
        result = bot.delete_message("123456", 111)
        
        bot.client.delete_message.assert_called_once_with("123456", 111)
        assert result is True
        
    def test_edit_message_text(self, bot):
        """Test edit_message_text delegates to client."""
        bot.client.edit_message_text = Mock(return_value={'message_id': 111})
        
        result = bot.edit_message_text(
            chat_id="123456",
            message_id=111,
            text="Edited",
            parse_mode="HTML"
        )
        
        bot.client.edit_message_text.assert_called_once_with(
            chat_id="123456",
            message_id=111,
            text="Edited",
            parse_mode="HTML",
            reply_markup=None
        )
        
    def test_create_forum_topic(self, bot):
        """Test create_forum_topic delegates to client."""
        bot.client.create_forum_topic = Mock(return_value=123)
        
        result = bot.create_forum_topic("123456", "New Topic")
        
        bot.client.create_forum_topic.assert_called_once_with("123456", "New Topic")
        assert result == 123
        
    def test_close_forum_topic(self, bot):
        """Test close_forum_topic delegates to client."""
        bot.client.close_forum_topic = Mock(return_value=True)
        
        result = bot.close_forum_topic("123456", 123)
        
        bot.client.close_forum_topic.assert_called_once_with(chat_id="123456", message_thread_id=123)
        assert result is True
        
    def test_get_chat_member(self, bot):
        """Test get_chat_member delegates to client."""
        bot.client.get_chat_member = Mock(return_value={'user': {'id': 123}})
        
        result = bot.get_chat_member("123456", "789")
        
        bot.client.get_chat_member.assert_called_once_with("123456", "789")
        assert result == {'user': {'id': 123}}


class TestBotWebappMenuButton:
    """Test Dashboard menu button visibility."""

    @pytest.fixture
    def bot(self):
        """Create bot with mocked client."""
        with patch('bot.core.bot.TelegramClient') as mock_client:
            with patch('bot.core.bot.Database'):
                with patch('bot.core.bot.PollingService'):
                    mock_client_instance = Mock()
                    mock_client.return_value = mock_client_instance

                    config = Mock(spec=Settings)
                    config.DB_PATH = ":memory:"
                    config.WEBAPP_URL = "https://example.com/dashboard"
                    bot = Bot("test_token", config)
                    return bot

    def test_start_does_not_set_global_menu_button(self, bot):
        """Test that start() no longer sets a global menu button."""
        bot.client.set_chat_menu_button = Mock(return_value=True)
        bot.start()
        bot.client.set_chat_menu_button.assert_not_called()

    def test_setup_webapp_menu_button_without_chat_id(self, bot):
        """Test setting default menu button without chat_id."""
        bot.client.set_chat_menu_button = Mock(return_value=True)
        bot.setup_webapp_menu_button()

        bot.client.set_chat_menu_button.assert_called_once()
        call_kwargs = bot.client.set_chat_menu_button.call_args[1]
        assert call_kwargs.get('chat_id') is None
        assert call_kwargs.get('menu_button', {}).get('type') == 'web_app'

    def test_setup_webapp_menu_button_with_chat_id(self, bot):
        """Test setting menu button for a specific chat."""
        bot.client.set_chat_menu_button = Mock(return_value=True)
        bot.setup_webapp_menu_button(chat_id='1652899')

        bot.client.set_chat_menu_button.assert_called_once()
        call_kwargs = bot.client.set_chat_menu_button.call_args[1]
        assert call_kwargs.get('chat_id') == '1652899'
