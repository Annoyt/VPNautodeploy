"""Custom exceptions for VPN bot.

These exceptions provide clear error messages for users and handlers.
"""


class VPNBotError(Exception):
    """Base exception for VPN bot."""
    
    def __init__(self, message: str, user_message: str = None):
        super().__init__(message)
        self.message = message
        self.user_message = user_message or "⚠️ Произошла ошибка. Попробуйте позже."


class ConfigurationError(VPNBotError):
    """Raised when configuration is invalid or missing."""
    
    def __init__(self, message: str):
        super().__init__(
            message=message,
            user_message="⚠️ Ошибка конфигурации сервера. Обратитесь к администратору."
        )


class XUIError(VPNBotError):
    """Raised when X-UI API operation fails."""
    
    def __init__(self, message: str, user_message: str = None):
        super().__init__(
            message=message,
            user_message=user_message or "⚠️ Сервис VPN временно недоступен. Попробуйте позже."
        )


class XUIConnectionError(XUIError):
    """Raised when cannot connect to X-UI panel."""
    
    def __init__(self, message: str = None):
        super().__init__(
            message=message or "Failed to connect to X-UI panel",
            user_message="⚠️ Не удалось подключиться к VPN серверу. Попробуйте позже."
        )


class XUIAuthError(XUIError):
    """Raised when X-UI authentication fails."""
    
    def __init__(self, message: str = None):
        super().__init__(
            message=message or "X-UI authentication failed",
            user_message="⚠️ Ошибка авторизации на VPN сервере. Обратитесь к администратору."
        )


class XUISyncError(XUIError):
    """Raised when user sync to X-UI fails."""
    
    def __init__(self, message: str = None):
        super().__init__(
            message=message or "Failed to sync user to X-UI",
            user_message="⚠️ Не удалось создать VPN ключ. Попробуйте позже."
        )


class VPNGenerationError(VPNBotError):
    """Raised when VPN key generation fails."""
    
    def __init__(self, message: str = None):
        super().__init__(
            message=message or "Failed to generate VPN configuration",
            user_message="⚠️ Не удалось сгенерировать VPN ключ. Попробуйте позже."
        )


class DatabaseError(VPNBotError):
    """Raised when database operation fails."""
    
    def __init__(self, message: str = None):
        super().__init__(
            message=message or "Database operation failed",
            user_message="⚠️ Ошибка базы данных. Попробуйте позже."
        )


class UserNotFoundError(VPNBotError):
    """Raised when user is not found in database."""
    
    def __init__(self, chat_id: str = None):
        super().__init__(
            message=f"User not found: {chat_id}",
            user_message="⚠️ Пользователь не найден. Начните с /start"
        )
        self.chat_id = chat_id


class PermissionDeniedError(VPNBotError):
    """Raised when user doesn't have permission for action."""
    
    def __init__(self, message: str = None):
        super().__init__(
            message=message or "Permission denied",
            user_message="❌ У вас нет прав для этого действия."
        )


class InvalidStateError(VPNBotError):
    """Raised when user is in invalid state for operation."""
    
    def __init__(self, message: str = None):
        super().__init__(
            message=message or "Invalid user state",
            user_message="⚠️ Неверное состояние. Начните с /start"
        )


class TelegramAPIError(VPNBotError):
    """Raised when Telegram API request fails."""
    
    def __init__(self, message: str = None):
        super().__init__(
            message=message or "Telegram API request failed",
            user_message="⚠️ Ошибка связи с Telegram. Попробуйте позже."
        )
