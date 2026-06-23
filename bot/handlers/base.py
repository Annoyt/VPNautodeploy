"""Base handler for all update handlers"""

import logging
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

from bot.config import Settings
from bot.core.database import Database, User

if TYPE_CHECKING:
    from bot.core.bot import Bot

logger = logging.getLogger(__name__)


class BaseHandler(ABC):
    """Abstract base class for all update handlers.
    
    All handlers must inherit from this class and implement
    can_handle() and handle() methods.
    """
    
    def __init__(self, bot: 'Bot', db: Database, config: Settings):
        """Initialize handler with dependencies.
        
        Args:
            bot: Bot instance for sending messages
            db: Database instance for data operations
            config: Settings configuration
        """
        self.bot = bot
        self.db = db
        self.config = config
    
    @abstractmethod
    def can_handle(self, update: dict) -> bool:
        """Check if this handler can process the update.
        
        Args:
            update: Telegram update object
            
        Returns:
            True if handler can process this update
        """
        pass
    
    @abstractmethod
    def handle(self, update: dict) -> None:
        """Process the update.
        
        Args:
            update: Telegram update object
        """
        pass
    
    def _get_chat_id(self, update: dict) -> Optional[str]:
        """Extract chat_id from update.
        
        Args:
            update: Telegram update object
            
        Returns:
            Chat ID as string or None
        """
        # Try message first
        message = update.get('message')
        if message and 'chat' in message and 'id' in message['chat']:
            return str(message['chat']['id'])
        
        # Try callback_query
        callback = update.get('callback_query')
        if callback and 'message' in callback and 'chat' in callback['message'] and 'id' in callback['message']['chat']:
            return str(callback['message']['chat']['id'])
        
        return None
    
    def _get_user_id(self, update: dict) -> Optional[str]:
        """Extract user_id from update.
        
        Args:
            update: Telegram update object
            
        Returns:
            User ID as string or None
        """
        # Try message
        message = update.get('message')
        if message and 'from' in message:
            return str(message['from']['id'])
        
        # Try callback_query
        callback = update.get('callback_query')
        if callback and 'from' in callback:
            return str(callback['from']['id'])
        
        return None
    
    def _get_username(self, update: dict) -> Optional[str]:
        """Extract username from update.
        
        Args:
            update: Telegram update object
            
        Returns:
            Username or None
        """
        # Try message
        message = update.get('message')
        if message and 'from' in message:
            return message['from'].get('username')
        
        # Try callback_query
        callback = update.get('callback_query')
        if callback and 'from' in callback:
            return callback['from'].get('username')
        
        return None
    
    def _is_admin(self, user_id: str, chat_id: Optional[str] = None) -> bool:
        """Check if user is admin globally or in a specific chat.
        
        Args:
            user_id: User ID to check
            chat_id: Optional chat ID to check permissions in
            
        Returns:
            True if user is admin
        """
        # 1. Check if SUPER_ADMIN
        if self.config.is_admin(user_id):
            return True
            
        # 2. If in a group/forum and chat_id provided, check if user is chat admin
        if chat_id and chat_id != user_id:
            return self._is_chat_admin(chat_id, user_id)
            
        return False
        
    def _is_chat_admin(self, chat_id: str, user_id: str) -> bool:
        """Check if user is an administrator in a specific chat.
        
        Args:
            chat_id: Chat ID
            user_id: User ID
            
        Returns:
            True if administrator/creator
        """
        try:
            member = self.bot.get_chat_member(chat_id, user_id)
            if member and member.get('status') in ['administrator', 'creator']:
                return True
        except Exception as e:
            logger.error(f"Error checking chat admin status: {e}")
            
        return False
    
    def _get_or_create_user(self, update: dict) -> Optional[User]:
        """Get existing user or create new one.
        
        Args:
            update: Telegram update object
            
        Returns:
            User object or None
        """
        user_id = self._get_user_id(update)
        if not user_id:
            return None
        
        user = self.db.get_user(user_id)
        if not user:
            username = self._get_username(update)
            user = User(chat_id=user_id, username=username)
            self.db.save_user(user)
            logger.info(f"Created new user: {user_id}")
        
        return user
    
    def _log_ticket_message(self, message: dict, topic_id: int, sender_type: str = 'user') -> None:
        """Log message to ticket_messages table.
        
        Args:
            message: Telegram message object
            topic_id: Forum topic ID
            sender_type: 'user' or 'admin'
        """
        from_user = message.get('from', {})
        sender_name = from_user.get('username', from_user.get('first_name', 'Unknown'))
        text = message.get('text', '')
        has_media = bool(message.get('photo') or message.get('document') or message.get('voice'))
        media_file_id = None
        if message.get('photo'):
            media_file_id = message['photo'][-1].get('file_id')
        elif message.get('document'):
            media_file_id = message['document'].get('file_id')
        elif message.get('voice'):
            media_file_id = message['voice'].get('file_id')
        
        self.db.log_ticket_message(
            topic_id, sender_type, sender_name, text,
            has_media, media_file_id, message.get('message_id')
        )
