"""Bot core with handler registry"""

import logging
from typing import List, Optional, TYPE_CHECKING

from bot.config.settings import Settings
from bot.core.database import Database
from bot.core.telegram_client import TelegramClient
from bot.core.polling import PollingService

if TYPE_CHECKING:
    from bot.handlers.base import BaseHandler

logger = logging.getLogger(__name__)


class Bot:
    """Bot coordinator with handler registry."""
    
    def __init__(self, token: str, config: Settings):
        """Initialize bot.
        
        Args:
            token: Bot token
            config: Bot configuration
        """
        self.token = token
        self.config = config
        self.client = TelegramClient(token)
        self.db = Database(config.DB_PATH)
        self.handlers: List['BaseHandler'] = []
        self._polling_service = PollingService(self.client, self._handle_update)
        
        logger.info("Bot instance created")
    
    def register_handler(self, handler: 'BaseHandler') -> None:
        """Register an update handler.
        
        Args:
            handler: Handler instance
        """
        handler.bot = self
        handler.db = self.db
        handler.config = self.config
        self.handlers.append(handler)
        logger.debug(f"Registered handler: {handler.__class__.__name__}")
    
    def _handle_update(self, update: dict) -> None:
        """Route update to appropriate handler.
        
        Args:
            update: Telegram update object
        """
        for handler in self.handlers:
            if handler.can_handle(update):
                try:
                    handler.handle(update)
                except Exception as e:
                    logger.exception(f"Handler {handler.__class__.__name__} failed: {e}")
                return
        
        # No handler found - log for debugging
        if 'message' in update:
            msg = update['message']
            chat_id = msg.get('chat', {}).get('id', 'unknown')
            logger.debug(f"No handler for message in chat {chat_id}")
    
    def setup_webapp_menu_button(self, chat_id: Optional[str] = None) -> None:
        """Set Telegram Mini App menu button for a specific chat or default.

        Args:
            chat_id: Target chat ID (None for default button)
        """
        webapp_url = getattr(self.config, 'WEBAPP_URL', '')
        if not webapp_url:
            logger.warning("WEBAPP_URL not configured, skipping Mini App menu button")
            return

        menu_button = {
            "type": "web_app",
            "text": "📊 Dashboard",
            "web_app": {"url": webapp_url}
        }

        result = self.client.set_chat_menu_button(chat_id=chat_id, menu_button=menu_button)
        if result:
            logger.info(f"Mini App menu button set for chat {chat_id}: {webapp_url}")
        else:
            logger.warning(f"Failed to set Mini App menu button for chat {chat_id}")

    def start(self) -> None:
        """Start the bot."""
        logger.info("Starting bot...")
        self._polling_service.start()
    
    def stop(self) -> None:
        """Stop the bot."""
        logger.info("Stopping bot...")
        self._polling_service.stop()
    
    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[dict] = None,
        **kwargs
    ) -> Optional[dict]:
        """Send a text message."""
        return self.client.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            **kwargs
        )
    
    def send_message_to_topic(
        self,
        chat_id: str,
        message_thread_id: int,
        text: str,
        **kwargs
    ) -> Optional[dict]:
        """Send a message to a forum topic."""
        return self.client.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=message_thread_id,
            **kwargs
        )
    
    def forward_message(
        self,
        chat_id: str,
        from_chat_id: str,
        message_id: int,
        **kwargs
    ) -> Optional[dict]:
        """Forward a message."""
        return self.client.forward_message(
            chat_id=chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
            **kwargs
        )
    
    def copy_message(
        self,
        chat_id: str,
        from_chat_id: str,
        message_id: int,
        **kwargs
    ) -> Optional[dict]:
        """Copy a message (used to ship media from closed support topics
        into the Solved archive). See ``TelegramClient.copy_message``.
        """
        return self.client.copy_message(
            chat_id=chat_id,
            from_chat_id=from_chat_id,
            message_id=message_id,
            **kwargs,
        )

    def edit_forum_topic(
        self,
        chat_id: str,
        message_thread_id: int,
        name: Optional[str] = None,
    ) -> bool:
        """Rename a forum topic (used to mark closed tickets)."""
        return self.client.edit_forum_topic(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            name=name,
        )

    def close_forum_topic(
        self,
        chat_id: str,
        message_thread_id: int,
        **kwargs
    ) -> Optional[dict]:
        """Close a forum topic.
        
        Args:
            chat_id: Unique identifier for the target chat or username
            message_thread_id: Unique identifier for the target message thread
            **kwargs: Additional parameters
            
        Returns:
            True on success, None on failure
        """
        return self.client.close_forum_topic(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            **kwargs
        )
    
    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
        **kwargs
    ) -> bool:
        """Answer a callback query."""
        return self.client.answer_callback_query(
            callback_query_id, text, show_alert, **kwargs
        )
    
    def create_forum_topic(
        self,
        chat_id: str,
        name: str,
        **kwargs
    ) -> Optional[int]:
        """Create a forum topic."""
        return self.client.create_forum_topic(chat_id, name, **kwargs)

    def get_chat_member(self, chat_id: str, user_id: str) -> Optional[dict]:
        """Get information about a member of a chat."""
        return self.client.get_chat_member(chat_id, user_id)

    def get_chat(self, chat_id: str) -> Optional[dict]:
        """Get information about a chat/user (username, bio, etc.)."""
        return self.client.get_chat(chat_id)

    def get_user_profile_photos(self, user_id: str, offset: int = 0, limit: int = 1) -> Optional[dict]:
        """Get user's profile photos (returns total_count and photos array)."""
        return self.client.get_user_profile_photos(user_id, offset, limit)

    def delete_message(
        self,
        chat_id: str,
        message_id: int,
        **kwargs
    ) -> bool:
        """Delete a message."""
        return self.client.delete_message(
            chat_id, message_id, **kwargs
        )

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[dict] = None
    ) -> Optional[dict]:
        """Edit message text."""
        return self.client.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup
        )
