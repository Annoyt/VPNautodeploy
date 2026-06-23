"""Message handler for text messages"""

import logging

from bot.config import UserState
from bot.handlers.base import BaseHandler
from bot.services.notifications import NotificationService

logger = logging.getLogger(__name__)


class MessageHandler(BaseHandler):
    """Handler for text messages from users."""
    
    def can_handle(self, update: dict) -> bool:
        """Check if update contains a message from user.
        
        Args:
            update: Telegram update object
            
        Returns:
            True if user message (not a command, not from forum)
        """
        message = update.get('message')
        if not message:
            return False
            
        # Skip forum messages (handled by ForumHandler)
        if message.get('is_topic_message'):
            return False
            
        text = message.get('text', '')
        # Skip commands
        if text.startswith('/'):
            return False
            
        return True
    
    def handle(self, update: dict) -> None:
        """Route message based on user state.
        
        Args:
            update: Telegram update object
        """
        message = update.get('message', {})
        text = message.get('text', '')
        chat_id = self._get_chat_id(update)
        
        if not chat_id:
            logger.warning("Cannot handle message: no chat_id")
            return
        
        user = self.db.get_user(chat_id)
        if not user:
            # New user - treat as regular message
            self.handle_regular_message(update, chat_id, text)
            return
        
        # Check if this is an admin PM reply
        if self._is_admin(chat_id) and 'reply_to_message' in message:
            self.handle_admin_reply(update)
            return
            
        # Route based on state
        if user.status == UserState.SUPPORT_TOPIC.value:
            self.handle_support_message(update, user)
        else:
            self.handle_regular_message(update, chat_id, text)
            
    def handle_admin_reply(self, update: dict) -> None:
        """Handle admin replying to a user's forwarded message in PM mode."""
        message = update.get('message', {})
        reply_to = message.get('reply_to_message', {})
        text = message.get('text', '')
        
        mapped = self.db.get_mapped_user_message(reply_to.get('message_id'))
        if mapped:
            user_chat_id = mapped['chat_id']
            # Send message to user
            self.bot.send_message(
                chat_id=user_chat_id,
                text=f"💬 <b>Support Reply:</b>\n\n{text}",
                parse_mode='HTML'
            )
            # Send confirmation to admin
            self.bot.send_message(
                chat_id=self._get_chat_id(update),
                text=f"✅ Reply sent to user."
            )
            logger.info(f"Forwarded PM reply to user {user_chat_id}")
        else:
            self.bot.send_message(
                chat_id=self._get_chat_id(update),
                text="❌ Could not find the original user for this message. Reply to a forwarded user message."
            )
    
    def handle_support_message(
        self, update: dict, user
    ) -> None:
        """Forward support message to admins and log it.
        
        Args:
            update: Telegram update object
            user: User object
        """
        message = update.get('message', {})
        text = message.get('text') or message.get('caption') or ""
        notifier = NotificationService(self.bot, self.db, self.config)
        user_msg_id = message.get('message_id')

        # Remember whether we have to mint a topic for this message.
        # notify_new_support_ticket already posts the issue text in the
        # initial "🆘 Support Request" message; forwarding the same
        # user message again right after would duplicate it in the
        # topic. Skip the forward in that case (subsequent messages
        # still go through forward_to_support as usual).
        was_new_ticket = not user.support_topic_id

        # 1. New Ticket Logic
        if not user.support_topic_id:
            if self.config.FORUM_ENABLED:
                topic_id = notifier.notify_new_support_ticket(user, text)
                if topic_id:
                    # Persist topic_id on the user — otherwise every
                    # subsequent message would spawn a fresh topic
                    # because user.support_topic_id stayed NULL.
                    user.support_topic_id = topic_id
                    self.db.save_user(user)
                    self.bot.send_message(chat_id=user.chat_id, text="🎫 Support ticket created! We'll reply soon.")
            else:
                user.support_topic_id = -1
                self.db.save_user(user)
                admin_msg_id = notifier.notify_new_support_ticket(user, text)
                if admin_msg_id:
                    self.db.log_message_map(admin_msg_id, user.chat_id, user_msg_id)
                    self.bot.send_message(chat_id=user.chat_id, text="🎫 Support ticket created! We'll reply soon.")

            # Refresh user with new topic_id
            user = self.db.get_user(user.chat_id)

        # 2. Log & Forward
        if user.support_topic_id:
            # Log to DB for compilation
            self._log_ticket_message(message, user.support_topic_id, sender_type='user')

            # Forward only for follow-up messages — the initial post
            # already showed the user's text inline.
            if not was_new_ticket:
                if self.config.FORUM_ENABLED:
                    notifier.forward_to_support(user, message)
                else:
                    admin_msg_id = notifier.forward_to_support(user, message)
                    if admin_msg_id:
                        self.db.log_message_map(admin_msg_id, user.chat_id, user_msg_id)

    def handle_regular_message(
        self, update: dict, chat_id: str, text: str
    ) -> None:
        """Handle regular message with default response.
        
        Args:
            update: Telegram update object
            chat_id: User's chat ID
            text: Message text
        """
        user = self.db.get_user(chat_id)
        lang = user.lang if user else 'ru'
        
        # Default response
        if lang == 'ru':
            response = (
                "🤖 Я не понимаю это сообщение.\n\n"
                "Используйте /help чтобы увидеть доступные команды "
                "или нажмите кнопку поддержки для связи с администратором."
            )
        else:
            response = (
                "🤖 I don't understand this message.\n\n"
                "Use /help to see available commands "
                "or press the support button to contact an admin."
            )
        
        self.bot.send_message(chat_id=chat_id, text=response)
