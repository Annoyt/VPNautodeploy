"""Forum handler for topic messages"""

import logging
from typing import Optional

from bot.handlers.base import BaseHandler

logger = logging.getLogger(__name__)


class ForumHandler(BaseHandler):
    """Handler for forum topic messages (admin replies).
    
    Only active when FORUM_ENABLED=True.
    Forwards admin replies from support topics to users.
    """
    
    def __init__(self, bot, db, config):
        """Initialize handler.
        
        Args:
            bot: Bot instance
            db: Database instance
            config: Settings config
        """
        super().__init__(bot, db, config)
        # Mark as disabled if forum mode is off
        self.disabled = not config.FORUM_ENABLED
    
    def can_handle(self, update: dict) -> bool:
        """Check if update is a forum topic message.
        
        Args:
            update: Telegram update object
            
        Returns:
            True if forum message
        """
        if self.disabled:
            return False
        
        message = update.get('message')
        if not message:
            return False
        
        # Must be a topic message
        if not message.get('is_topic_message'):
            return False
        
        # Must be in the configured forum group
        chat_id = str(message.get('chat', {}).get('id', ''))
        if chat_id != self.config.FORUM_GROUP_ID:
            return False
            
        return True
    
    def handle(self, update: dict) -> None:
        """Handle forum message (reply to user or /close command).
        
        Args:
            update: Telegram update object
        """
        message = update.get('message', {})
        text = message.get('text', '')
        topic_id = message.get('message_thread_id')
        user_id = str(message.get('from', {}).get('id', ''))
        
        if not topic_id:
            return
            
        # 1. Handle /close command
        if text and text.strip():
            parts = text.strip().split()
            if parts and parts[0].lower() == '/close':
                self.handle_close_ticket(update, topic_id)
                return

        # 2. Log message to ticket history
        self._log_ticket_message(message, topic_id, sender_type='admin')

        # 3. If from admin, forward to user
        if self._is_admin(user_id):
            user = self._get_user_by_topic(topic_id)
            if not user:
                return
            
            # Forward text if present
            if text:
                self._forward_reply_to_user(user, text)
            
            # Note: Media forwarding could be added here if needed, 
            # but usually admin replies with text only or uses manual forward.

    def handle_close_ticket(self, update: dict, topic_id: int) -> None:
        """Compile ticket log, close topic, notify user, reset state.

        Wrapped in a single try/except so any exception surfaces in the
        topic itself instead of being silently swallowed by the callback
        dispatcher (the symptom that masked the original UserState
        ImportError for so long).
        """
        # UserState lives in bot.config.constants and is re-exported via
        # bot.config — NOT bot.models. The original `from bot.models`
        # import raised every time, killing the close callback silently.
        from bot.config import UserState as Status
        from bot.core.state_machine import StateMachine

        def _say(text: str) -> None:
            try:
                self.bot.send_message(
                    chat_id=self.config.FORUM_GROUP_ID,
                    text=text,
                    message_thread_id=topic_id,
                    parse_mode='HTML',
                )
            except Exception as inner:
                logger.warning(f"close_ticket: feedback send failed: {inner}")

        try:
            user = self._get_user_by_topic(topic_id)
            if not user:
                _say("❌ Не нашёл пользователя для этого топика — возможно, тикет уже закрыт. Закрываю топик.")
                try:
                    self.bot.close_forum_topic(self.config.FORUM_GROUP_ID, topic_id)
                except Exception as e:
                    logger.warning(f"close_ticket: close_forum_topic failed for {topic_id}: {e}")
                return

            # Idempotency: if the user's stored topic differs (e.g. a
            # stale-topic recovery ran in between), bail out so we don't
            # duplicate the SOLVED log + user notification on a repeat
            # click.
            if user.support_topic_id and user.support_topic_id != topic_id:
                _say(
                    f"⚠️ У <code>{user.chat_id}</code> сейчас активный тикет в "
                    f"топике {user.support_topic_id}. Закрываю только этот."
                )
                try:
                    self.bot.close_forum_topic(self.config.FORUM_GROUP_ID, topic_id)
                except Exception:
                    pass
                return

            # Immediate ack so the admin sees the click registered — the
            # forwardMessage / sendMessage chain below can take a second.
            _say("🔒 Закрываю тикет…")

            history = self.db.get_ticket_messages(topic_id) or []

            ticket_id = f"T{topic_id}"
            uname = f"@{user.username}" if user.username else f"user_{user.chat_id}"
            log_lines = [
                f"📄 <b>Ticket Log: #{ticket_id}</b>",
                f"👤 User: <code>{user.chat_id}</code> ({uname})\n",
            ]
            media_count = 0
            for msg in history:
                time_str = (msg.get('timestamp') or '')[11:16]
                sender = "👨‍💻 Admin" if msg.get('sender_type') == 'admin' else "👤 User"
                body = msg.get('message_text') or ''
                line = f"[{time_str}] {sender}: {body}"
                if msg.get('has_media'):
                    line += " 📎"
                    media_count += 1
                log_lines.append(line)
            if media_count:
                log_lines.append(f"\n<i>Вложений: {media_count}</i>")
            full_log = "\n".join(log_lines)

            solved_topic = getattr(self.config, 'TOPIC_SOLVED', None) or self.config.TOPIC_SUPPORT
            self.bot.send_message(
                chat_id=self.config.FORUM_GROUP_ID,
                text=full_log,
                message_thread_id=solved_topic,
                parse_mode='HTML',
            )

            # Copy each media message into the Solved archive so the
            # attachments survive the topic closure (copyMessage strips
            # the "Forwarded from" header and replays as a fresh
            # message; we can't forward across topics in a closed
            # thread anyway).
            for m in history:
                if not m.get('has_media'):
                    continue
                src_msg_id = m.get('message_id')
                if not src_msg_id:
                    continue
                try:
                    self.bot.copy_message(
                        chat_id=self.config.FORUM_GROUP_ID,
                        from_chat_id=self.config.FORUM_GROUP_ID,
                        message_id=int(src_msg_id),
                        message_thread_id=solved_topic,
                        caption=f"#ticket_{topic_id}",
                    )
                except Exception as e:
                    logger.warning(f"close_ticket: copy media {src_msg_id} failed: {e}")

            # Notify the user (default to RU if lang is None).
            texts = {
                'ru': "✅ Ваш тикет закрыт. Если появятся новые вопросы — создайте новый тикет.",
                'en': "✅ Your ticket is closed. Open a new one if you have further questions.",
            }
            lang = (user.lang or 'ru')
            self.bot.send_message(
                chat_id=user.chat_id,
                text=texts.get(lang, texts['ru']),
            )

            if user.status == Status.SUPPORT_TOPIC.value:
                StateMachine(self.db).return_from_support(user.chat_id)

            # Rename topic to "✅ ..." so the closed ones are visually
            # distinct in the topic list (cheap UX win — closed forum
            # topics on mobile look the same as open ones otherwise).
            try:
                uname = f"@{user.username}" if user.username else f"user_{user.chat_id}"
                self.bot.edit_forum_topic(
                    chat_id=self.config.FORUM_GROUP_ID,
                    message_thread_id=topic_id,
                    name=f"✅ {uname}"[:128],
                )
            except Exception as e:
                logger.warning(f"close_ticket: edit_forum_topic rename failed for {topic_id}: {e}")

            # Close topic in Telegram first, THEN clear the id — that
            # way a failure in close_forum_topic doesn't orphan the user.
            try:
                self.bot.close_forum_topic(self.config.FORUM_GROUP_ID, topic_id)
            except Exception as e:
                logger.warning(f"close_ticket: close_forum_topic failed for {topic_id}: {e}")

            user.support_topic_id = None
            self.db.save_user(user)

            logger.info(f"Ticket closed for user {user.chat_id} in topic {topic_id}")

        except Exception as e:
            logger.exception(f"handle_close_ticket failed for topic {topic_id}: {e}")
            _say(
                f"❌ Не удалось закрыть тикет: "
                f"<code>{type(e).__name__}: {str(e)[:200]}</code>"
            )
    
    def _get_user_by_topic(self, topic_id: int) -> Optional:
        """Find user by support topic ID.
        
        Args:
            topic_id: Forum topic ID
            
        Returns:
            User object or None
        """
        return self.db.get_user_by_topic_id(topic_id)
    
    def _forward_reply_to_user(self, user, text: str) -> bool:
        """Send reply to user's private chat.
        
        Args:
            user: User object
            text: Reply text
            
        Returns:
            True if sent successfully
        """
        try:
            # Add admin reply prefix
            reply_text = f"💬 <b>Support Reply:</b>\n\n{text}"
            
            result = self.bot.send_message(
                chat_id=user.chat_id,
                text=reply_text,
                parse_mode='HTML'
            )
            
            return result is not None
            
        except Exception as e:
            logger.error(f"Failed to send reply to user: {e}")
            return False
