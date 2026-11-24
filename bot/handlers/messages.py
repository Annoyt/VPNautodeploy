"""Message handler for text messages"""

import logging
import re
import time

from bot.config import UserState
from bot.handlers.base import BaseHandler
from bot.services.notifications import NotificationService
from bot.utils.validators import validate_email

logger = logging.getLogger(__name__)

# chat_id → ts of the "📧 Add email" button tap. While a fresh entry
# exists, the user's next plain-text message is treated as their email
# address. The old flow demanded a /setemail command — users just sent
# the address as plain text, which fell into "I don't understand this
# message" and was silently lost (ziriki, 2026-07-25).
PENDING_EMAIL: dict = {}
PENDING_EMAIL_TTL = 600  # seconds
# chat_id → ts of the code letter: the next message is the six-digit code
# (bot/services/email_link.py). EMAIL_PURPOSE: 'login' for «📧 Я
# регистрировался через почту», 'add' (the default) for «📧 Добавить почту».
PENDING_EMAIL_CODE: dict = {}
PENDING_CODE_TTL = 16 * 60  # seconds: the code's 15 minutes and a margin
EMAIL_PURPOSE: dict = {}

# chat_id → ts of the "📝 Другой сайт" tap in the 🆘 picker: the next
# plain-text message names the site that does not open (IMPROVEMENT_PLAN
# E2, bot/handlers/callbacks/rule_lists.py). Arming one prompt disarms
# the other — the next message answers the latest tap.
PENDING_SITE: dict = {}
PENDING_SITE_TTL = 600  # seconds


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

        # Pending "add email" prompt → this message IS the email address.
        if self._maybe_handle_pending_email(chat_id, text, user):
            return

        # A code was mailed → this message is the code.
        if self._maybe_handle_pending_code(chat_id, text, user):
            return

        # Pending "📝 другой сайт" prompt → this message names the site.
        if self._maybe_handle_pending_site(chat_id, text, user):
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

    def _maybe_handle_pending_email(self, chat_id: str, text: str, user) -> bool:
        """Consume a pending email prompt. Returns True if the message
        was handled (a code mailed to it, or rejected as malformed)."""
        ts = PENDING_EMAIL.get(chat_id)
        if ts is None:
            return False
        if time.time() - ts > PENDING_EMAIL_TTL:
            PENDING_EMAIL.pop(chat_id, None)
            return False

        email = (text or '').strip()
        lang = getattr(user, 'lang', None) or 'ru'
        if not validate_email(email):
            # Keep the pending flag so they can retry within the TTL.
            msg = ("❌ Это не похоже на email. Попробуй ещё раз "
                   "(например ivan@gmail.com) или нажми «Отмена»."
                   if lang == 'ru' else
                   "❌ That doesn't look like an email. Try again "
                   "(e.g. john@gmail.com) or press «Cancel».")
            cancel = {'inline_keyboard': [[{
                'text': '✖️ Отмена' if lang == 'ru' else '✖️ Cancel',
                'callback_data': 'email_cancel'}]]}
            self.bot.send_message(chat_id=chat_id, text=msg, reply_markup=cancel)
            return True

        PENDING_EMAIL.pop(chat_id, None)
        self.send_email_code(chat_id, email, lang)
        return True

    def _mailer(self):
        services = getattr(self.bot, 'services', None)
        mailer = services.get('email') if isinstance(services, dict) else None
        if mailer is None:
            from bot.services.email_service import EmailService
            mailer = EmailService(self.config)
        return mailer

    def send_email_code(self, chat_id: str, email: str, lang: str) -> None:
        """Mail a code to ``email`` and arm the code prompt."""
        from bot.services import email_link
        result = email_link.send_code(self.db, self.config, self._mailer(),
                                      chat_id, email, lang=lang)
        en = lang == 'en'
        if result == 'sent':
            PENDING_EMAIL_CODE[chat_id] = time.time()
            text = (f"📨 Sent a code to {email_link.masked(email)}. Send the six "
                    "digits here: the code works for 15 minutes."
                    if en else
                    f"📨 Отправили код на {email_link.masked(email)}. Пришлите сюда "
                    "шесть цифр из письма: код действует 15 минут.")
            keyboard = {'inline_keyboard': [[
                {'text': '🔁 Send again' if en else '🔁 Отправить ещё раз',
                 'callback_data': 'email_resend'},
                {'text': '✖️ Cancel' if en else '✖️ Отмена',
                 'callback_data': 'email_cancel'},
            ]]}
            self.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
            return
        if result == 'rate_limited':
            EMAIL_PURPOSE.pop(chat_id, None)
            text = ("⏳ Too many letters this hour. Try again later."
                    if en else
                    "⏳ За час отправили уже три письма. Попробуйте позже.")
        else:
            PENDING_EMAIL[chat_id] = time.time()
            text = ("❌ Could not send a letter to that address. Check it and "
                    "send it again." if en else
                    "❌ Не получилось отправить письмо на этот адрес. Проверьте "
                    "его и пришлите ещё раз.")
        self.bot.send_message(chat_id=chat_id, text=text)

    def _maybe_handle_pending_code(self, chat_id: str, text: str, user) -> bool:
        """Consume a pending code. True when the message was the answer."""
        ts = PENDING_EMAIL_CODE.get(chat_id)
        if ts is None:
            return False
        if time.time() - ts > PENDING_CODE_TTL:
            PENDING_EMAIL_CODE.pop(chat_id, None)
            EMAIL_PURPOSE.pop(chat_id, None)
            return False
        from bot.services import email_link
        lang = getattr(user, 'lang', None) or 'ru'
        en = lang == 'en'
        code = re.sub(r'\s+', '', text or '')
        if not re.fullmatch(r'\d{6}', code):
            self.bot.send_message(chat_id=chat_id, text=(
                "Send the six digits from the letter, or press «Cancel»."
                if en else "Пришлите шесть цифр из письма или нажмите «Отмена»."))
            return True
        outcome, email = email_link.check_code(self.db, self.config, chat_id, code)
        if outcome == 'wrong':
            left = email_link.tries_left(self.db, chat_id)
            self.bot.send_message(chat_id=chat_id, text=(
                f"❌ Wrong code. Tries left: {left}." if en
                else f"❌ Неверный код. Осталось попыток: {left}."))
            return True
        if outcome != 'ok':
            if outcome == 'too_many':
                PENDING_EMAIL_CODE.pop(chat_id, None)
                EMAIL_PURPOSE.pop(chat_id, None)
            self.bot.send_message(chat_id=chat_id, text=(
                "⌛ The code no longer works. Press «Send again» for a new one."
                if en else
                "⌛ Этот код больше не действует. Нажмите «Отправить ещё раз» "
                "за новым."))
            return True
        PENDING_EMAIL_CODE.pop(chat_id, None)
        purpose = EMAIL_PURPOSE.pop(chat_id, 'add')
        self._finish_email_link(chat_id, email, purpose, lang)
        return True

    def _finish_email_link(self, chat_id: str, email: str, purpose: str,
                           lang: str) -> None:
        """A verified address: link it (bot/services/email_link.py) and
        tell the user and, for a merge or a conflict, the admin."""
        from bot.services import email_link
        result = email_link.link(self.db, chat_id, email)
        en = lang == 'en'
        user = self.db.get_user(chat_id)
        keyboard = None
        if result.outcome == 'merged':
            self._drop_replaced_client(result.dropped_client)
            self._push_panel_comment(user)
            until = _day(result.paid_until)
            text = ((f"✅ Email confirmed: your subscription{' (paid until ' + until + ')' if until else ''} "
                     "now lives in this Telegram. Press «🔑 My key»: it is the same "
                     "subscription, so your apps keep working.")
                    if en else
                    (f"✅ Почта подтверждена: ваша подписка{' (оплачена до ' + until + ')' if until else ''} "
                     "теперь в этом Telegram. Нажмите «🔑 Мой ключ»: подписка та же, "
                     "приложения работают как раньше."))
            keyboard = {'inline_keyboard': [[{
                'text': '🔑 My key' if en else '🔑 Мой ключ', 'callback_data': 'my_key'}]]}
        elif result.outcome == 'admin':
            text = ("✅ Email confirmed. Another subscription is on it: we passed "
                    "it to the admin, who will join them." if en else
                    "✅ Почта подтверждена. На неё оформлена ещё одна подписка — "
                    "передали администратору, он свяжет их вручную.")
        else:
            self._push_panel_comment(user)
            if purpose == 'login' and not getattr(user, 'uuid', None):
                text = ("✅ Email confirmed, but no subscription is on it. You can "
                        "ask for a demo." if en else
                        "✅ Почта подтверждена, но подписки на неё у нас нет. Можно "
                        "запросить демо.")
                if getattr(user, 'status', None) == UserState.NEW.value:
                    keyboard = {'inline_keyboard': [[{
                        'text': '🎁 Request Demo' if en else '🎁 Запросить демо',
                        'callback_data': 'request_demo'}]]}
            else:
                text = (f"✅ Email confirmed and saved: {email_link.masked(email)}."
                        if en else
                        f"✅ Почта подтверждена и сохранена: {email_link.masked(email)}.")
        self.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
        if result.outcome in ('merged', 'admin'):
            NotificationService(self.bot, self.db, self.config).notify_email_link(
                user, email, result)
        logger.info(f"User {chat_id} confirmed an email: {result.outcome}")

    def _drop_replaced_client(self, panel_email) -> None:
        """Take the demo client a merge replaced off the panel."""
        if not panel_email:
            return
        try:
            xui = self.bot.services.get('xui') if isinstance(
                getattr(self.bot, 'services', None), dict) else None
            if xui is None:
                from bot.services.xui_service import XUIService
                xui = XUIService(self.config)
            xui.remove_client_sync(panel_email)
        except Exception as e:
            logger.warning(f"email link: replaced client not removed: {e}")

    def _maybe_handle_pending_site(self, chat_id: str, text: str, user) -> bool:
        """Consume a pending "📝 другой сайт" prompt (see PENDING_SITE)."""
        if chat_id not in PENDING_SITE:
            return False
        from bot.handlers.callbacks.rule_lists import SiteReportFlow
        return SiteReportFlow(self.bot, self.db, self.config).consume_pending(
            chat_id, text, user)

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


def _day(value) -> str:
    """'2028-03-30T23:59:00' → '30.03.2028'; '' when there is none."""
    text = str(value or '')
    return f'{text[8:10]}.{text[5:7]}.{text[0:4]}' if len(text) >= 10 else ''
