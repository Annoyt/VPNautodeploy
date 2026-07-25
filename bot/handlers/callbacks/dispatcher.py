"""Callback dispatcher - routes callbacks to appropriate handlers.

Implements Chain of Responsibility pattern for clean callback routing.
"""

import logging
from typing import List, TYPE_CHECKING

from bot.config import Settings
from bot.handlers.callbacks.base import BaseCallbackHandler

# Import all handlers
from bot.handlers.callbacks.user import (
    DemoRequestHandler,
    PlatformSelectHandler,
    GetKeyHandler,
    MyKeyAnswerHandler,
    TryAltProtocolHandler,
    SupportRequestHandler,
    EmailPromptHandler,
    EmailKeyHandler,
    StatsRequestHandler,
    FullVersionHandler,
    LanguageSetHandler,
)
from bot.handlers.callbacks.admin import (
    ApproveUserHandler,
    RejectUserHandler,
    RevokeUserHandler,
    AdminMessageHandler,
    AdminProfileHandler,
    ResetApprovalHandler,
    CloseTicketHandler,
    BanFromTicketHandler,
    AlertAckHandler,
)
from bot.handlers.callbacks.ai_model import AiModelSelectHandler

if TYPE_CHECKING:
    from bot.core.bot import Bot
    from bot.core.database import Database

logger = logging.getLogger(__name__)


class CallbackDispatcher:
    """Dispatches callbacks to appropriate handlers.
    
    Maintains list of handlers and routes callbacks to first matching handler.
    """
    
    def __init__(self, bot: 'Bot', db: 'Database', config: Settings):
        """Initialize dispatcher with all handlers.
        
        Args:
            bot: Bot instance
            db: Database instance
            config: Settings configuration
        """
        self.bot = bot
        self.db = db
        self.config = config
        self.handlers: List[BaseCallbackHandler] = []
        
        self._register_handlers()
    
    def _register_handlers(self) -> None:
        """Register all callback handlers in priority order."""
        # User handlers (check first for user actions)
        self.handlers.append(DemoRequestHandler(self.bot, self.db, self.config))
        self.handlers.append(GetKeyHandler(self.bot, self.db, self.config))
        self.handlers.append(PlatformSelectHandler(self.bot, self.db, self.config))
        # /mykey Y/N answer (current UX).  The old cascading-fallback
        # button (TryAltProtocolHandler) stays registered for backward
        # compatibility with key messages already sitting in users'
        # chat history — old buttons keep working until those messages
        # roll off-screen.  Both must precede SupportRequestHandler so
        # their prefixes aren't swallowed by the support pattern.
        self.handlers.append(MyKeyAnswerHandler(self.bot, self.db, self.config))
        self.handlers.append(TryAltProtocolHandler(self.bot, self.db, self.config))
        self.handlers.append(SupportRequestHandler(self.bot, self.db, self.config))
        self.handlers.append(EmailPromptHandler(self.bot, self.db, self.config))
        self.handlers.append(EmailKeyHandler(self.bot, self.db, self.config))
        self.handlers.append(StatsRequestHandler(self.bot, self.db, self.config))
        self.handlers.append(FullVersionHandler(self.bot, self.db, self.config))
        self.handlers.append(LanguageSetHandler(self.bot, self.db, self.config))
        
        # Admin handlers (check after user handlers)
        self.handlers.append(ApproveUserHandler(self.bot, self.db, self.config))
        self.handlers.append(RejectUserHandler(self.bot, self.db, self.config))
        self.handlers.append(RevokeUserHandler(self.bot, self.db, self.config))
        self.handlers.append(AdminMessageHandler(self.bot, self.db, self.config))
        self.handlers.append(AdminProfileHandler(self.bot, self.db, self.config))
        self.handlers.append(ResetApprovalHandler(self.bot, self.db, self.config))
        self.handlers.append(CloseTicketHandler(self.bot, self.db, self.config))
        self.handlers.append(BanFromTicketHandler(self.bot, self.db, self.config))
        self.handlers.append(AlertAckHandler(self.bot, self.db, self.config))
        self.handlers.append(AiModelSelectHandler(self.bot, self.db, self.config))

        logger.info(f"Registered {len(self.handlers)} callback handlers")
    
    def dispatch(self, update: dict, chat_id: str, user_id: str, data: str) -> bool:
        """Dispatch callback to appropriate handler.
        
        Args:
            update: Telegram update object
            chat_id: Chat ID
            user_id: User ID
            data: Callback data string
            
        Returns:
            True if handled, False otherwise
        """
        for handler in self.handlers:
            if handler.can_handle(data):
                try:
                    handler.handle(update, chat_id, user_id, data=data)
                    logger.debug(f"Callback '{data}' handled by {handler.__class__.__name__}")
                    return True
                except Exception as e:
                    logger.exception(f"Error in handler {handler.__class__.__name__}: {e}")
                    return False
        
        logger.warning(f"No handler found for callback: {data}")
        return False
    
    def get_handler_count(self) -> int:
        """Get number of registered handlers.
        
        Returns:
            Handler count
        """
        return len(self.handlers)
    
    def get_handler_names(self) -> List[str]:
        """Get list of registered handler names.
        
        Returns:
            List of handler class names
        """
        return [h.__class__.__name__ for h in self.handlers]
