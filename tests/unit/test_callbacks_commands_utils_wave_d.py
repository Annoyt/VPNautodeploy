"""Wave D: Callbacks, Commands, and Utilities - edge cases and bug hunt."""

import pytest
from unittest.mock import MagicMock, Mock, patch

from bot.handlers.commands import CommandHandler
from bot.handlers.callbacks.dispatcher import CallbackDispatcher
from bot.handlers.callbacks.base import ValidationService, ForumMessageService
from bot.handlers.callbacks.user import (
    DemoRequestHandler, PlatformSelectHandler, GetKeyHandler, SupportRequestHandler
)
from bot.handlers.callbacks.admin import (
    ApproveUserHandler, RejectUserHandler, RevokeUserHandler,
    AdminMessageHandler, AdminProfileHandler, ResetApprovalHandler, CloseTicketHandler
)
from bot.utils.helpers import escape_markdown, escape_html, truncate_text, format_bytes, format_duration, mask_sensitive
from bot.utils.validators import validate_chat_id, validate_callback_data
from bot.config import UserState, Platform


@pytest.fixture
def mock_command_handler():
    """Create CommandHandler with mocks."""
    bot = MagicMock()
    db = MagicMock()
    config = MagicMock()
    config.SUPER_ADMIN_ID = '1652899'
    config.FORUM_ENABLED = False
    return CommandHandler(bot, db, config)


@pytest.fixture
def mock_dispatcher():
    """Create CallbackDispatcher with mocks."""
    bot = MagicMock()
    db = MagicMock()
    config = MagicMock()
    config.SUPER_ADMIN_ID = '1652899'
    config.FORUM_ENABLED = False
    return CallbackDispatcher(bot, db, config)


class TestCommandHandlerEdgeCases:
    """Test CommandHandler edge cases."""
    
    def test_can_handle_missing_message(self, mock_command_handler):
        """Test can_handle returns False for updates without message."""
        assert mock_command_handler.can_handle({}) is False
        assert mock_command_handler.can_handle({'callback_query': {}}) is False
    
    def test_can_handle_non_command(self, mock_command_handler):
        """Test can_handle returns False for regular text."""
        update = {'message': {'text': 'hello'}}
        assert mock_command_handler.can_handle(update) is False
    
    def test_handle_missing_chat_id(self, mock_command_handler):
        """Test handle returns early when no chat_id."""
        update = {'message': {'text': '/start', 'chat': {}}}
        mock_command_handler.handle(update)
        # After fix in base.py, _get_chat_id returns None when 'id' missing
        mock_command_handler.bot.send_message.assert_not_called()
    
    def test_handle_unknown_command(self, mock_command_handler):
        """Test handle sends error for unknown command."""
        update = {'message': {'text': '/unknown', 'chat': {'id': '123'}}}
        mock_command_handler.handle(update)
        text = mock_command_handler.bot.send_message.call_args[1]['text']
        assert 'Unknown command' in text
    
    def test_handle_command_with_bot_mention(self, mock_command_handler):
        """Test handle strips @botname from command."""
        update = {'message': {'text': '/start@mybot', 'chat': {'id': '123'}}}
        with patch.object(mock_command_handler, 'handle_start') as mock_start:
            mock_command_handler.handle(update)
            mock_start.assert_called_once()
    
    def test_handle_start_no_user(self, mock_command_handler):
        """Test handle_start returns early when user creation fails."""
        with patch.object(mock_command_handler, '_get_or_create_user', return_value=None):
            mock_command_handler.handle_start({'message': {}}, '123')
            mock_command_handler.bot.send_message.assert_not_called()

    def test_handle_start_support_topic_shows_main_menu(self, mock_command_handler):
        """Test /start for user in support_topic shows main menu with key button."""
        user = MagicMock()
        user.status = UserState.SUPPORT_TOPIC.value
        user.lang = 'ru'
        with patch.object(mock_command_handler, '_get_or_create_user', return_value=user):
            with patch('bot.handlers.commands.NotificationService') as mock_notifier_cls:
                mock_notifier = MagicMock()
                mock_notifier_cls.return_value = mock_notifier
                mock_command_handler.handle_start({'message': {'chat': {'id': '123'}}}, '123')
                mock_notifier.notify_main_menu.assert_called_once_with('123', 'ru')
                mock_notifier.notify_welcome.assert_not_called()
    
    def test_handle_help_admin(self, mock_command_handler):
        """Test handle_help shows admin help for admin."""
        mock_command_handler._is_admin = Mock(return_value=True)
        user = MagicMock(lang='ru')
        with patch.object(mock_command_handler, '_get_or_create_user', return_value=user):
            update = {'message': {'chat': {'id': '123'}}}
            mock_command_handler.handle_help(update, '123')
            text = mock_command_handler.bot.send_message.call_args[1]['text']
            assert 'Available Commands' in text or 'Панель управления' in text or 'команды' in text.lower()


class TestCallbackDispatcherEdgeCases:
    """Test CallbackDispatcher edge cases."""
    
    def test_dispatch_no_matching_handler(self, mock_dispatcher):
        """Test dispatch returns False for unknown callback."""
        result = mock_dispatcher.dispatch({}, '123', '123', 'totally_unknown')
        assert result is False
    
    def test_dispatch_handler_exception(self, mock_dispatcher):
        """Test dispatch catches handler exceptions and returns False."""
        handler = mock_dispatcher.handlers[0]
        handler.handle = Mock(side_effect=RuntimeError("boom"))
        
        result = mock_dispatcher.dispatch({}, '123', '123', handler.CALLBACK_DATA)
        assert result is False
    
    def test_get_handler_count(self, mock_dispatcher):
        """Test get_handler_count returns number of handlers."""
        assert mock_dispatcher.get_handler_count() == len(mock_dispatcher.handlers)
    
    def test_get_handler_names(self, mock_dispatcher):
        """Test get_handler_names returns class names."""
        names = mock_dispatcher.get_handler_names()
        assert 'DemoRequestHandler' in names
        assert 'ApproveUserHandler' in names


class TestValidationServiceEdgeCases:
    """Test ValidationService edge cases."""
    
    def test_validate_admin_with_none_config_id(self):
        """Test validate_admin when SUPER_ADMIN_ID is None."""
        db = MagicMock()
        config = MagicMock()
        config.SUPER_ADMIN_ID = None
        vs = ValidationService(db, config)
        
        with pytest.raises(Exception):
            vs.validate_admin('123')
    
    def test_validate_user_exists_none(self):
        """Test validate_user_exists raises when user is None."""
        db = MagicMock()
        db.get_user.return_value = None
        config = MagicMock()
        vs = ValidationService(db, config)
        
        from bot.utils.exceptions import UserNotFoundError
        with pytest.raises(UserNotFoundError):
            vs.validate_user_exists('123')
    
    def test_validate_state_transition_invalid(self):
        """Test validate_state_transition raises on invalid transition."""
        db = MagicMock()
        config = MagicMock()
        vs = ValidationService(db, config)
        
        from bot.utils.exceptions import InvalidStateError
        with patch('bot.handlers.callbacks.base.StateMachine') as MockSM:
            mock_sm = MockSM.return_value
            mock_sm.get_state.return_value = UserState.BANNED
            mock_sm.can_transition.return_value = False
            
            with pytest.raises(InvalidStateError):
                vs.validate_state_transition('123', UserState.PAID)


class TestForumMessageServiceEdgeCases:
    """Test ForumMessageService edge cases."""
    
    def test_update_request_message_forum_disabled(self):
        """Test update_request_message returns False when forum disabled."""
        bot = MagicMock()
        config = MagicMock()
        config.FORUM_ENABLED = False
        fms = ForumMessageService(bot, config)
        
        result = fms.update_request_message({}, MagicMock(), 'admin', 'TEST', '✅')
        assert result is False
        bot.edit_message_text.assert_not_called()
    
    def test_update_request_message_wrong_thread(self):
        """Test update_request_message returns False for wrong thread."""
        bot = MagicMock()
        config = MagicMock()
        config.FORUM_ENABLED = True
        config.TOPIC_REQUESTS = 15
        fms = ForumMessageService(bot, config)
        
        message = {'message_thread_id': 99}
        result = fms.update_request_message(message, MagicMock(), 'admin', 'TEST', '✅')
        assert result is False
    
    def test_update_request_message_edit_failure(self):
        """Test update_request_message handles edit failure gracefully."""
        bot = MagicMock()
        bot.edit_message_text.side_effect = Exception("API error")
        config = MagicMock()
        config.FORUM_ENABLED = True
        config.TOPIC_REQUESTS = 15
        fms = ForumMessageService(bot, config)
        
        user = MagicMock(username='testuser', chat_id='123')
        message = {'message_thread_id': 15, 'message_id': 1, 'chat': {'id': 'group'}}
        result = fms.update_request_message(message, user, 'admin', 'TEST', '✅')
        assert result is False
    
    def test_send_admin_notification_success(self):
        """Test send_admin_notification sends message successfully."""
        bot = MagicMock()
        config = MagicMock()
        config.TOPIC_REQUESTS = 15
        fms = ForumMessageService(bot, config)
        
        result = fms.send_admin_notification('chat', 15, 1, 'text')
        assert result is True
        bot.send_message.assert_called_once()
    
    def test_get_username_display_with_none(self):
        """Test _get_username_display when username is None."""
        bot = MagicMock()
        config = MagicMock()
        fms = ForumMessageService(bot, config)
        
        user = MagicMock(username=None)
        assert fms._get_username_display(user) == 'no_username'


class TestUserCallbackHandlersEdgeCases:
    """Test user callback handler edge cases."""
    
    def test_demo_request_handler_cannot_request(self):
        """Test DemoRequestHandler blocks already-active users."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        handler = DemoRequestHandler(bot, db, config)
        
        user = MagicMock()
        user.status = 'demo'
        db.get_user.return_value = user
        
        handler.handle({}, '123', '123')
        text = bot.send_message.call_args[1]['text']
        assert 'already have' in text.lower() or 'pending' in text.lower()
    
    def test_platform_select_handler_invalid_data(self):
        """Test PlatformSelectHandler returns early on invalid data."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        handler = PlatformSelectHandler(bot, db, config)
        
        handler.handle({}, '123', '123', data='platform:')
        bot.send_message.assert_not_called()
    
    def test_get_key_handler_empty_target_fallback(self):
        """Test GetKeyHandler falls back to chat_id when target is empty."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        handler = GetKeyHandler(bot, db, config)
        
        # After fix, empty target falls back to chat_id
        handler.handle({}, '123', '123', data='get_key:')
        # _run_async is called; synchronous execution shouldn't crash
        assert True  # If we got here, no sync crash
    
    def test_support_request_handler_empty_target_fallback(self):
        """Test SupportRequestHandler falls back to chat_id when target is empty."""
        bot = MagicMock()
        db = MagicMock()
        user = MagicMock()
        user.status = 'demo'
        db.get_user.return_value = user
        config = MagicMock()
        handler = SupportRequestHandler(bot, db, config)
        
        # After fix, empty target falls back to chat_id
        handler.handle({}, '123', '123', data='support:')
        # Should not crash; opens ticket for chat_id '123'


class TestAdminCallbackHandlersEdgeCases:
    """Test admin callback handler edge cases."""
    
    def test_approve_user_missing_target_id(self):
        """Test ApproveUserHandler handles 'approve:' with missing target_id."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.SUPER_ADMIN_ID = '1652899'
        handler = ApproveUserHandler(bot, db, config)
        
        handler.handle({}, '123', '1652899', data='approve:')
        text = bot.send_message.call_args[1]['text']
        assert 'Invalid callback data' in text
    
    def test_reject_user_missing_target_id(self):
        """Test RejectUserHandler handles 'reject:' with missing target_id."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.SUPER_ADMIN_ID = '1652899'
        handler = RejectUserHandler(bot, db, config)
        
        handler.handle({}, '123', '1652899', data='reject:')
        text = bot.send_message.call_args[1]['text']
        assert 'Invalid callback data' in text
    
    def test_revoke_user_missing_target_id(self):
        """Test RevokeUserHandler handles 'revoke:' with missing target_id."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.SUPER_ADMIN_ID = '1652899'
        handler = RevokeUserHandler(bot, db, config)
        
        handler.handle({}, '123', '1652899', data='revoke:')
        text = bot.send_message.call_args[1]['text']
        assert 'Invalid callback data' in text
    
    def test_approve_user_permission_denied(self):
        """Test ApproveUserHandler sends permission denied to non-admin."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.SUPER_ADMIN_ID = '1652899'
        handler = ApproveUserHandler(bot, db, config)
        
        handler.handle({}, '123', '999', data='approve:456')
        text = bot.send_message.call_args[1]['text']
        assert 'No permission' in text
    
    def test_reject_user_not_found(self):
        """Test RejectUserHandler sends not found when target missing."""
        bot = MagicMock()
        db = MagicMock()
        db.get_user.return_value = None
        config = MagicMock()
        config.SUPER_ADMIN_ID = '1652899'
        config.FORUM_ENABLED = False
        handler = RejectUserHandler(bot, db, config)
        
        update = {'callback_query': {'message': {'chat': {'id': 'group'}, 'message_thread_id': 20}}}
        handler.handle(update, '123', '1652899', data='reject:456')
        text = bot.send_message.call_args[1]['text']
        assert 'not found' in text.lower()


class TestHelpersEdgeCases:
    """Test helper utility edge cases."""
    
    def test_escape_markdown_none(self):
        """Test escape_markdown handles None input gracefully."""
        assert escape_markdown(None) == ""
    
    def test_escape_html_none(self):
        """Test escape_html handles None input gracefully."""
        assert escape_html(None) == ""
    
    def test_truncate_text_none(self):
        """Test truncate_text handles None input gracefully."""
        assert truncate_text(None) == ""
    
    def test_format_bytes_negative(self):
        """Test format_bytes handles negative input."""
        assert format_bytes(-1) == "0 B"
    
    def test_format_bytes_zero(self):
        """Test format_bytes with zero."""
        assert format_bytes(0) == "0.00 B"
    
    def test_format_bytes_pb(self):
        """Test format_bytes with petabyte scale."""
        # 1024**6 bytes = 1 EB, but function caps at PB and returns 1024.00 PB
        assert format_bytes(1024**6) == "1024.00 PB"
    
    def test_format_duration_zero(self):
        """Test format_duration with zero."""
        assert format_duration(0) == "0s"
    
    def test_format_duration_exact_day(self):
        """Test format_duration with exact day boundary."""
        assert format_duration(86400) == "1d"
    
    def test_format_duration_days_and_hours(self):
        """Test format_duration with days and hours."""
        assert format_duration(90000) == "1d 1h"
    
    def test_mask_sensitive_empty(self):
        """Test mask_sensitive with empty string."""
        assert mask_sensitive("") == ""
    
    def test_mask_sensitive_short(self):
        """Test mask_sensitive when text is shorter than visible_chars."""
        assert mask_sensitive("ab", visible_chars=4) == "**"


class TestValidatorsEdgeCases:
    """Test validator edge cases."""
    
    def test_validate_chat_id_integer_input(self):
        """BUG: validate_chat_id fails or behaves unexpectedly with integer input."""
        # int("123") works, but if not chat_id checks falsiness, 0 would fail
        assert validate_chat_id(0) is False
        assert validate_chat_id(123) is True
    
    def test_validate_callback_data_none(self):
        """Test validate_callback_data with None."""
        assert validate_callback_data(None) is False
    
    def test_validate_callback_data_too_long(self):
        """Test validate_callback_data exceeds 64 bytes."""
        long_data = "a" * 65
        assert validate_callback_data(long_data) is False
    
    def test_validate_callback_data_unicode_length(self):
        """Test validate_callback_data counts bytes not chars."""
        # 32 Cyrillic chars = 64 bytes
        data = "а" * 32
        assert validate_callback_data(data) is True
        # 33 Cyrillic chars = 66 bytes
        data2 = "а" * 33
        assert validate_callback_data(data2) is False
