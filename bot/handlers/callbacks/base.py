"""Base callback handler with validation and utilities."""

import logging
from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING

from bot.config import Settings, UserState
from bot.core.state_machine import StateMachine
from bot.handlers.base import BaseHandler
from bot.utils.exceptions import PermissionDeniedError, UserNotFoundError, InvalidStateError

if TYPE_CHECKING:
    from bot.core.bot import Bot
    from bot.core.database import Database
    from bot.models import User

logger = logging.getLogger(__name__)


class ValidationService:
    """Service for validation operations.
    
    Centralizes all validation logic to avoid duplication.
    """
    
    def __init__(self, db: 'Database', config: Settings):
        self.db = db
        self.config = config
    
    def validate_admin(self, user_id: str) -> bool:
        """Validate if user is admin.
        
        Args:
            user_id: User ID to check
            
        Returns:
            True if admin
            
        Raises:
            PermissionDeniedError: If not admin
        """
        if str(user_id) != str(self.config.SUPER_ADMIN_ID):
            raise PermissionDeniedError("Admin access required")
        return True
    
    def validate_user_exists(self, chat_id: str) -> 'User':
        """Validate user exists in database.
        
        Args:
            chat_id: User's chat ID
            
        Returns:
            User object
            
        Raises:
            UserNotFoundError: If user not found
        """
        user = self.db.get_user(chat_id)
        if not user:
            raise UserNotFoundError(chat_id)
        return user
    
    def validate_state_transition(self, chat_id: str, new_state: UserState) -> bool:
        """Validate state transition is allowed.
        
        Args:
            chat_id: User's chat ID
            new_state: Target state
            
        Returns:
            True if transition valid
            
        Raises:
            InvalidStateError: If transition not allowed
        """
        sm = StateMachine(self.db)
        current_state = sm.get_state(chat_id)
        
        if not sm.can_transition(current_state, new_state):
            raise InvalidStateError(
                f"Cannot transition from {current_state} to {new_state}"
            )
        return True


class ForumMessageService:
    """Service for forum message operations.
    
    Handles all forum-related message editing and notifications.
    """
    
    def __init__(self, bot: 'Bot', config: Settings):
        self.bot = bot
        self.config = config
    
    def _get_username_display(self, user: 'User') -> str:
        """Get formatted username for display."""
        return f"@{user.username}" if user.username else "no_username"
    
    def update_request_message(
        self,
        message: dict,
        user: 'User',
        admin_id: str,
        action: str,
        emoji: str
    ) -> bool:
        """Strip Approve/Reject buttons and stamp the request message.

        Runs in BOTH forum mode and PM mode — in PM mode the request
        notification lands as a bot DM to the admin; without this edit
        the admin can keep clicking Approve, each click re-runs the
        full flow and a duplicate "approved" notification gets sent.

        Args:
            message: Original Telegram message dict (from callback_query.message)
            user: User object
            admin_id: Admin chat ID
            action: Action text (APPROVED/REJECTED)
            emoji: Emoji for action (✅/❌)

        Returns:
            True if the message was edited.
        """
        message_id = message.get('message_id')
        if not message_id:
            return False

        # In forum mode, only edit if the message is in the Requests topic.
        # In PM mode (FORUM_ENABLED=False) we always edit the bot's DM.
        if self.config.FORUM_ENABLED:
            message_thread_id = message.get('message_thread_id')
            if message_thread_id != self.config.TOPIC_REQUESTS:
                return False

        try:
            username_display = self._get_username_display(user)
            source_chat_id = str(message.get('chat', {}).get('id', admin_id))

            self.bot.edit_message_text(
                chat_id=source_chat_id,
                message_id=message_id,
                text=(
                    f"{emoji} <b>{action}</b>\n\n"
                    f"🆕 Demo Request\n"
                    f"👤 User: {user.chat_id} ({username_display})\n"
                    f"👨‍💻 By: {admin_id}"
                ),
                parse_mode='HTML',
                reply_markup={'inline_keyboard': []}
            )
            return True
        except Exception as e:
            logger.warning(f"Failed to edit request message: {e}")
            return False
    
    def send_admin_notification(
        self,
        chat_id: str,
        message_thread_id: Optional[int],
        target_thread_id: int,
        text: str
    ) -> bool:
        """Send notification to appropriate topic.
        
        Args:
            chat_id: Chat ID
            message_thread_id: Current thread ID
            target_thread_id: Target thread ID
            text: Message text
            
        Returns:
            True if successful
        """
        try:
            reply_thread_id = message_thread_id if message_thread_id != self.config.TOPIC_REQUESTS else target_thread_id
            self.bot.send_message(
                chat_id=chat_id,
                message_thread_id=reply_thread_id,
                text=text
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")
            return False


class BaseCallbackHandler(BaseHandler, ABC):
    """Abstract base for all callback handlers.
    
    Provides common validation and utility services.
    """
    
    def __init__(self, bot: 'Bot', db: 'Database', config: Settings):
        super().__init__(bot, db, config)
        self.validator = ValidationService(db, config)
        self.forum_service = ForumMessageService(bot, config)
    
    def _run_async(self, coro_or_factory, *args) -> None:
        """Run async coroutine or factory safely in sync context.

        Avoids creating nested event loops. If called from async context,
        schedules the coroutine as a task with error handling.

        Args:
            coro_or_factory: Async coroutine to run, or callable returning one
            *args: Arguments to pass if coro_or_factory is callable
        """
        import asyncio
        import inspect

        if inspect.iscoroutine(coro_or_factory):
            coro = coro_or_factory
        elif callable(coro_or_factory):
            coro = coro_or_factory(*args)
        else:
            raise TypeError("Expected coroutine or callable")

        async def _wrapped_coro():
            """Wrapper that catches and logs exceptions."""
            try:
                await coro
            except Exception as e:
                logger.exception(f"Error in async callback handler: {e}")

        def _on_task_done(task):
            """Log task completion status."""
            if task.cancelled():
                logger.warning("Async callback handler task was cancelled")
            elif task.exception():
                logger.exception(f"Async callback handler task failed: {task.exception()}")

        try:
            loop = asyncio.get_running_loop()
            # Already in async context - schedule as task
            logger.debug("Scheduling async handler in existing event loop")
            task = loop.create_task(_wrapped_coro())
            task.add_done_callback(_on_task_done)
        except RuntimeError:
            # No event loop running - safe to use asyncio.run()
            try:
                asyncio.run(_wrapped_coro())
            except Exception as e:
                logger.exception(f"Error running async callback handler: {e}")
    
    @abstractmethod
    def can_handle(self, callback_data: str) -> bool:
        """Check if this handler can process the callback.
        
        Args:
            callback_data: Callback data string
            
        Returns:
            True if can handle
        """
        pass
    
    @abstractmethod
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process the callback.
        
        Args:
            update: Telegram update object
            chat_id: Chat ID
            user_id: User ID
            **kwargs: Additional arguments
        """
        pass
    
    def _get_callback_info(self, update: dict) -> tuple[Optional[str], Optional[dict]]:
        """Extract callback info from update.
        
        Args:
            update: Telegram update object
            
        Returns:
            Tuple of (callback_data, callback_object)
        """
        callback = update.get('callback_query', {})
        data = callback.get('data', '')
        return data, callback
    
    def _get_message_info(self, callback: dict) -> tuple[str, Optional[int]]:
        """Extract message info from callback.
        
        Args:
            callback: Callback query object
            
        Returns:
            Tuple of (chat_id, message_thread_id)
        """
        message = callback.get('message', {})
        chat_id = str(message.get('chat', {}).get('id', ''))
        message_thread_id = message.get('message_thread_id')
        return chat_id, message_thread_id
