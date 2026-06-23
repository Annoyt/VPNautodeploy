"""Unit tests for custom exceptions.

Tests all custom exception classes and their messages.
"""

import pytest

from bot.utils.exceptions import (
    VPNBotError,
    ConfigurationError,
    XUIError,
    XUIConnectionError,
    XUIAuthError,
    XUISyncError,
    VPNGenerationError,
    DatabaseError,
    UserNotFoundError,
    InvalidStateError,
    TelegramAPIError,
    PermissionDeniedError,
)


class TestVPNBotError:
    """Test base VPNBotError class."""
    
    def test_default_user_message(self):
        """Test default user message."""
        error = VPNBotError("Internal error")
        assert error.message == "Internal error"
        assert error.user_message == "⚠️ Произошла ошибка. Попробуйте позже."
    
    def test_custom_user_message(self):
        """Test custom user message."""
        error = VPNBotError("Internal error", "Custom message for user")
        assert error.user_message == "Custom message for user"
    
    def test_exception_can_be_raised(self):
        """Test that exception can be raised and caught."""
        with pytest.raises(VPNBotError) as exc_info:
            raise VPNBotError("Test error")
        
        assert str(exc_info.value) == "Test error"


class TestConfigurationError:
    """Test ConfigurationError."""
    
    def test_error_message(self):
        """Test error message format."""
        error = ConfigurationError("BOT_TOKEN not set")
        assert error.message == "BOT_TOKEN not set"
        assert "конфигурации" in error.user_message.lower()
    
    def test_is_vpn_bot_error_subclass(self):
        """Test that ConfigurationError is subclass of VPNBotError."""
        error = ConfigurationError("Test")
        assert isinstance(error, VPNBotError)


class TestXUIErrors:
    """Test X-UI related errors."""
    
    def test_xui_error_base(self):
        """Test base XUIError."""
        error = XUIError("Connection failed")
        assert "недоступен" in error.user_message.lower()
    
    def test_xui_connection_error(self):
        """Test XUIConnectionError."""
        error = XUIConnectionError()
        assert error.message == "Failed to connect to X-UI panel"
        assert "подключиться" in error.user_message.lower()
        
        # Test with custom message
        error = XUIConnectionError("Custom connection error")
        assert error.message == "Custom connection error"
    
    def test_xui_auth_error(self):
        """Test XUIAuthError."""
        error = XUIAuthError()
        assert error.message == "X-UI authentication failed"
        assert "авторизации" in error.user_message.lower()
    
    def test_xui_sync_error(self):
        """Test XUISyncError."""
        error = XUISyncError()
        assert error.message == "Failed to sync user to X-UI"
        assert "создать vpn ключ" in error.user_message.lower()
    
    def test_xui_errors_inheritance(self):
        """Test that all XUI errors inherit from XUIError."""
        assert issubclass(XUIConnectionError, XUIError)
        assert issubclass(XUIAuthError, XUIError)
        assert issubclass(XUISyncError, XUIError)


class TestVPNGenerationError:
    """Test VPNGenerationError."""
    
    def test_default_message(self):
        """Test default error message."""
        error = VPNGenerationError()
        assert error.message == "Failed to generate VPN configuration"
        assert "сгенерировать vpn ключ" in error.user_message.lower()
    
    def test_custom_message(self):
        """Test custom error message."""
        error = VPNGenerationError("UUID generation failed")
        assert error.message == "UUID generation failed"


class TestDatabaseError:
    """Test DatabaseError."""
    
    def test_default_message(self):
        """Test default error message."""
        error = DatabaseError()
        assert error.message == "Database operation failed"
        assert "базы данных" in error.user_message.lower()


class TestUserNotFoundError:
    """Test UserNotFoundError."""
    
    def test_with_chat_id(self):
        """Test error with chat_id."""
        error = UserNotFoundError("123456")
        assert error.message == "User not found: 123456"
        assert error.chat_id == "123456"
        assert "не найден" in error.user_message.lower()
        assert "/start" in error.user_message
    
    def test_without_chat_id(self):
        """Test error without chat_id."""
        error = UserNotFoundError()
        assert error.message == "User not found: None"


class TestInvalidStateError:
    """Test InvalidStateError."""
    
    def test_default_message(self):
        """Test default error message."""
        error = InvalidStateError()
        assert error.message == "Invalid user state"
        assert "состояние" in error.user_message.lower()
    
    def test_custom_message(self):
        """Test custom error message."""
        error = InvalidStateError("Cannot transition from new to active")
        assert error.message == "Cannot transition from new to active"


class TestTelegramAPIError:
    """Test TelegramAPIError."""
    
    def test_default_message(self):
        """Test default error message."""
        error = TelegramAPIError()
        assert error.message == "Telegram API request failed"
        assert "Telegram" in error.user_message


class TestPermissionDeniedError:
    """Test PermissionDeniedError."""
    
    def test_default_message(self):
        """Test default error message."""
        error = PermissionDeniedError()
        assert error.message == "Permission denied"
        assert "нет прав" in error.user_message.lower()
    
    def test_custom_message(self):
        """Test custom error message."""
        error = PermissionDeniedError("Admin access required")
        assert error.message == "Admin access required"


class TestExceptionHierarchy:
    """Test exception class hierarchy."""
    
    def test_all_inherit_from_vpn_bot_error(self):
        """Test that all custom exceptions inherit from VPNBotError."""
        exceptions = [
            ConfigurationError,
            XUIError,
            VPNGenerationError,
            DatabaseError,
            UserNotFoundError,
            InvalidStateError,
            TelegramAPIError,
            PermissionDeniedError,
        ]
        
        for exc_class in exceptions:
            error = exc_class("Test message")
            assert isinstance(error, VPNBotError), f"{exc_class} should inherit from VPNBotError"
    
    def test_xui_errors_inherit_from_xui_error(self):
        """Test X-UI errors inheritance chain."""
        xui_errors = [XUIConnectionError, XUIAuthError, XUISyncError]
        
        for exc_class in xui_errors:
            error = exc_class()
            assert isinstance(error, XUIError)
            assert isinstance(error, VPNBotError)


class TestExceptionUsage:
    """Test exception usage patterns."""
    
    def test_catching_base_exception(self):
        """Test catching base VPNBotError."""
        errors = [
            ConfigurationError("Config error"),
            UserNotFoundError("123"),
            VPNGenerationError(),
        ]
        
        for error in errors:
            with pytest.raises(VPNBotError):
                raise error
    
    def test_exception_with_context(self):
        """Test using exceptions in context."""
        try:
            raise UserNotFoundError("999")
        except VPNBotError as e:
            # Should be able to access user_message
            assert e.user_message is not None
            assert isinstance(e.user_message, str)
    
    def test_all_exceptions_have_user_message(self):
        """Test that all exceptions have user_message attribute."""
        exceptions = [
            ConfigurationError("test"),
            XUIError("test"),
            XUIConnectionError(),
            XUIAuthError(),
            XUISyncError(),
            VPNGenerationError(),
            DatabaseError(),
            UserNotFoundError(),
            InvalidStateError(),
            TelegramAPIError(),
            PermissionDeniedError(),
        ]
        
        for exc in exceptions:
            assert hasattr(exc, 'user_message')
            assert exc.user_message is not None
            assert len(exc.user_message) > 0


class TestRussianUserMessages:
    """Test that user messages are in Russian."""
    
    def test_user_friendly_messages(self):
        """Test that all messages are user-friendly."""
        error = VPNGenerationError()
        assert "⚠️" in error.user_message  # Has warning emoji
        
        error = UserNotFoundError("123")
        assert "⚠️" in error.user_message
        
        error = PermissionDeniedError()
        assert "❌" in error.user_message  # Has cross emoji


class TestExceptionStringRepresentation:
    """Test string representation of exceptions."""
    
    def test_str_contains_message(self):
        """Test that str(exception) contains the message."""
        error = ConfigurationError("Missing BOT_TOKEN")
        assert "Missing BOT_TOKEN" in str(error)
    
    def test_repr_format(self):
        """Test repr format."""
        error = UserNotFoundError("123")
        repr_str = repr(error)
        assert "UserNotFoundError" in repr_str or "VPNBotError" in repr_str
