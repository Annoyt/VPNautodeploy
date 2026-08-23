"""Forum handler for topic messages"""

import logging
import re
from typing import Optional

from bot.handlers.base import BaseHandler

logger = logging.getLogger(__name__)

# Directed reply in a shared topic: "@<chat_id или username> текст".
# Usernames are 5-32 [A-Za-z0-9_] per Telegram; numeric = chat_id.
DIRECTED_RE = re.compile(r'^@([A-Za-z0-9_]{3,32})(?:[\s,:]+(.*))?$', re.DOTALL)

# chat_id inside the bot's own posts (failure reports, ticket logs):
# "User: @name (8111788347)" — the parenthesised id is authoritative.
REPLY_TO_ID_RE = re.compile(r'\((\d{5,})\)')


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
        chat_id = str((message.get('chat') or {}).get('id'))
        if chat_id != self.config.FORUM_GROUP_ID:
            return False
            
        return True
    
    def handle(self, update: dict) -> None:
        """Handle forum message (reply to user or /close command).
        
        Args:
            update: Telegram update object
        """
        message = update.get('message', {})
        text = message.get('text') or ''
        topic_id = message.get('message_thread_id')

        if not topic_id:
            return
            
        # 1. Handle /close command
        if text.strip():
            parts = text.strip().split()
            if parts and parts[0].lower() == '/close':
                self.handle_close_ticket(update, topic_id)
                return

        # 2. Log message to ticket history
        self._log_ticket_message(message, topic_id, sender_type='admin')

        # 3. If from admin, forward to user
        if not self._is_forum_admin(message):
            return

        user = self._get_user_by_topic(topic_id)
        if user:
            delivered = self._deliver_reply(user, message, text)
            if delivered is False:
                # The old code dropped failures silently — that's how
                # lost replies went unnoticed for weeks. Surface it.
                self._notify_topic(
                    topic_id,
                    f"⚠️ Не доставлено пользователю <code>{user.chat_id}</code> — "
                    f"возможно, бот заблокирован.",
                )
            return

        # Topic without a bound user (общая «Поддержка», куда бот постит
        # failure-репорты). Admins answer here as "@<chat_id> текст" or
        # by swipe-replying to the bot's report — relay through the bot,
        # since the client is not in the group and a raw @mention of a
        # numeric id delivers nothing.
        target, body, label = self._resolve_directed_reply(message, text)
        if label is None:
            return  # ordinary chatter, not addressed to anyone
        if target is None:
            self._notify_topic(
                topic_id,
                f"⚠️ Не нашёл пользователя @{label} в базе бота — ответ не доставлен.",
            )
            return
        delivered = self._deliver_reply(target, message, body, caption_override=body)
        if delivered:
            self._notify_topic(
                topic_id,
                f"✅ Доставлено пользователю <code>{target.chat_id}</code> через бота.",
            )
        else:
            self._notify_topic(
                topic_id,
                f"⚠️ Не доставлено <code>{target.chat_id}</code> — пустое сообщение, "
                f"неподдерживаемый тип вложения или бот заблокирован.",
            )

    def _deliver_reply(self, user, message: dict, text: str,
                       caption_override: Optional[str] = None) -> Optional[bool]:
        """Ship an admin reply (text or media) to the user's PM.

        Returns:
            True if delivered, False if sending failed, None when the
            message carries nothing forwardable (sticker, poll, …).
        """
        has_media = bool(
            message.get('photo') or message.get('document')
            or message.get('voice') or message.get('video')
        )
        if has_media:
            # copyMessage replays the attachment (with its caption)
            # into the user's PM — plain sendMessage would drop the
            # screenshot the admin just pasted.
            return self._copy_media_to_user(user, message, caption=caption_override)
        if text:
            return self._forward_reply_to_user(user, text)
        return None

    def _resolve_directed_reply(self, message: dict, text: str):
        """Work out which user a shared-topic admin message addresses.

        Returns:
            (user, body, label) — ``label is None`` means the message is
            not addressed to anyone; ``user is None`` with a label means
            the address didn't resolve to a known bot user.
        """
        source = text or message.get('caption') or ''
        m = DIRECTED_RE.match(source.strip())
        if m:
            handle = m.group(1)
            body = (m.group(2) or '').strip()
            if handle.isdigit():
                user = self.db.get_user(handle)
            else:
                user = self.db.get_user_by_username(handle)
            return user, body, handle

        reply_to = message.get('reply_to_message') or {}
        rt_text = reply_to.get('text') or reply_to.get('caption') or ''
        ids = REPLY_TO_ID_RE.findall(rt_text) or re.findall(r'@(\d{5,})', rt_text)
        if ids:
            return self.db.get_user(ids[0]), source.strip(), ids[0]

        return None, '', None

    def _notify_topic(self, topic_id: int, text: str) -> None:
        """Best-effort operator feedback into the topic itself."""
        try:
            self.bot.send_message(
                chat_id=self.config.FORUM_GROUP_ID,
                text=text,
                message_thread_id=topic_id,
                parse_mode='HTML',
            )
        except Exception as e:
            logger.warning(f"topic ack failed for {topic_id}: {e}")

    # Telegram's service account that fronts "Remain Anonymous" admins.
    GROUP_ANONYMOUS_BOT_ID = '1087968824'

    def _is_forum_admin(self, message: dict) -> bool:
        """True when the message author counts as support staff.

        An admin reply arrives in one of three shapes:
        - normal: ``from.id`` is the admin's own account;
        - anonymous ("Remain Anonymous" enabled): ``from`` is
          GroupAnonymousBot and ``sender_chat`` is the forum group
          itself — only chat admins can post that way, so it's proof
          of adminship by construction;
        - another group admin (not SUPER_ADMIN): resolved via
          getChatMember, which BaseHandler._is_admin only does when
          given the chat_id.
        """
        chat_id = str((message.get('chat') or {}).get('id') or '')
        sender_chat_id = str((message.get('sender_chat') or {}).get('id') or '')
        if sender_chat_id and sender_chat_id == chat_id:
            return True
        user_id = str((message.get('from') or {}).get('id') or '')
        if not user_id:
            return False
        return self._is_admin(user_id, chat_id)

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

    def _copy_media_to_user(self, user, message: dict,
                            caption: Optional[str] = None) -> bool:
        """Replay an admin's media reply into the user's PM.

        Sends the standard "Support Reply" header first, then copies the
        original message (attachment + caption) via copyMessage.

        Args:
            user: User object
            message: Telegram message object from the topic
            caption: replaces the original caption when not None (used
                to strip the "@<chat_id>" addressing prefix)

        Returns:
            True if the copy succeeded
        """
        extra = {}
        if caption is not None:
            extra['caption'] = caption
        try:
            self.bot.send_message(
                chat_id=user.chat_id,
                text="💬 <b>Support Reply:</b>",
                parse_mode='HTML',
            )
            result = self.bot.copy_message(
                chat_id=user.chat_id,
                from_chat_id=self.config.FORUM_GROUP_ID,
                message_id=message['message_id'],
                **extra,
            )
            return result is not None
        except Exception as e:
            logger.error(f"Failed to copy media reply to user {user.chat_id}: {e}")
            return False
