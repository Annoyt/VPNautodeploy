"""Command handler for bot commands"""

import logging
from typing import Optional

from bot.config import UserState
from bot.handlers.base import BaseHandler
from bot.services.notifications import NotificationService
from bot.utils.helpers import format_bytes

logger = logging.getLogger(__name__)


class CommandHandler(BaseHandler):
    """Handler for bot commands (/start, /help, /stats, /mykey)."""
    
    COMMANDS = {
        '/start': 'handle_start',
        '/help': 'handle_help',
        '/stats': 'handle_stats',
        '/mykey': 'handle_mykey',
        '/sub': 'handle_sub',
        '/raw': 'handle_raw',
        '/admin': 'handle_admin',
        '/setemail': 'handle_setemail',
    }
    
    def can_handle(self, update: dict) -> bool:
        """Check if update contains a command.
        
        Args:
            update: Telegram update object
            
        Returns:
            True if message starts with /
        """
        message = update.get('message')
        if not message or 'text' not in message:
            return False

        text = message.get('text', '')
        if not text.startswith('/'):
            return False

        chat_type = (message.get('chat') or {}).get('type', 'private')
        if chat_type == 'private':
            return True
        # In groups this handler only owns /admin (dashboard opener).
        # The rest belongs to AdminHandler (registered before us) or
        # ForumHandler (/close inside support topics, registered after
        # us) — claiming every slash message here used to swallow
        # /close and bounce "Unknown command" into the General topic.
        command = text.split()[0].split('@')[0].lower()
        return command == '/admin'
    
    def handle(self, update: dict) -> None:
        """Route command to appropriate handler.

        Args:
            update: Telegram update object
        """
        message = update.get('message', {})
        text = message.get('text', '')
        chat_id = self._get_chat_id(update)

        if not chat_id:
            logger.warning("Cannot handle command: no chat_id")
            return

        # Extract command (remove @botname if present)
        command = text.split()[0].split('@')[0].lower()

        handler_name = self.COMMANDS.get(command)
        if handler_name:
            handler = getattr(self, handler_name)
            handler(update, chat_id)
        else:
            # Unknown command
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Unknown command. Use /help for available commands."
            )

    @staticmethod
    def _command_user_id(update: dict) -> str:
        """Get the sender's user_id, falling back to chat_id in 1:1 chats.

        Important in forum-group context: chat_id is the group, the actual
        admin user_id lives in message.from.id. Treating chat_id as user_id
        (the old code path) made `_is_admin(chat_id)` always false in
        groups, so admins lost their /help / /admin in the forum.
        """
        msg = update.get('message') or {}
        from_user = msg.get('from') or {}
        uid = from_user.get('id')
        return str(uid) if uid is not None else ''
    
    def handle_start(self, update: dict, chat_id: str) -> None:
        """Handle /start command.
        
        Args:
            update: Telegram update object
            chat_id: User's chat ID
        """
        user = self._get_or_create_user(update)
        if not user:
            logger.error("Failed to get or create user")
            return
        
        notifier = NotificationService(self.bot, self.db, self.config)
        
        if user.status == UserState.NEW.value:
            notifier.notify_welcome(chat_id, user.lang)
        elif user.status == UserState.PENDING_DEMO.value:
            notifier.notify_pending(chat_id, user.lang)
        elif user.status == UserState.REJECTED.value:
            notifier.notify_rejected_can_retry(chat_id, user)
        elif user.status in (UserState.DEMO.value, UserState.PAID.value, UserState.SUPPORT_TOPIC.value):
            # User already has access, show main menu
            notifier.notify_main_menu(chat_id, user.lang)
        elif user.status == UserState.PLATFORM_SELECT.value:
            # User was approved but hasn't selected a platform yet
            notifier.notify_approved(chat_id, user.lang)
        elif user.status == UserState.BANNED.value:
            notifier.notify_banned(chat_id, user.lang)
        else:
            # For other states, just show welcome
            notifier.notify_welcome(chat_id, user.lang)

        # If the sender is the super admin, also offer the dashboard right
        # after /start so they don't have to remember /admin. PM only —
        # in a forum group /start is too noisy a place to attach this.
        user_id = self._command_user_id(update) or chat_id
        if str(chat_id) == str(user_id) and self.config.is_admin(user_id):
            webapp_url = getattr(self.config, 'WEBAPP_URL', '')
            if webapp_url:
                self.bot.setup_webapp_menu_button(chat_id=chat_id)
                self.bot.send_message(
                    chat_id=chat_id,
                    text="👑 <b>Админ-режим</b>\nДашборд — кнопка ниже или команда /admin.",
                    parse_mode='HTML',
                    reply_markup={'inline_keyboard': [[{
                        'text': '📊 Open Dashboard',
                        'web_app': {'url': webapp_url},
                    }]]},
                )

        logger.info(f"Handled /start for user {chat_id}")
    
    def handle_help(self, update: dict, chat_id: str) -> None:
        """Handle /help command.
        
        Args:
            update: Telegram update object
            chat_id: User's chat ID
        """
        user = self._get_or_create_user(update)
        lang = user.lang if user else 'ru'

        user_id = self._command_user_id(update) or chat_id
        if self._is_admin(user_id, chat_id):
            # Single source of truth for the admin command list — keep
            # in sync via ADMIN_HELP_TEXT in admin/base.py.
            from bot.handlers.admin.base import ADMIN_HELP_TEXT
            help_text = ADMIN_HELP_TEXT
        else:
            if lang == 'ru':
                help_text = (
                    "📖 <b>Доступные команды</b>\n\n"
                    "/start - Запустить бота и запросить доступ\n"
                    "/help - Показать эту справку\n"
                    "/stats - Показать статистику\n"
                    "/mykey - Получить subscription URL (=/sub)\n"
                    "/sub - Subscription URL (один линк = весь каскад)\n"
                    "/buy - Купить подписку (100 ГБ/мес, все протоколы)\n"
                    "/raw - Raw-ключи для legacy-клиентов (CDN-only)\n\n"
                    "Нужна помощь? Используйте кнопку поддержки в меню."
                )
            else:
                help_text = (
                    "📖 <b>Available Commands</b>\n\n"
                    "/start - Start bot and request demo\n"
                    "/help - Show this help\n"
                    "/stats - Show your statistics\n"
                    "/mykey - Get subscription URL (=/sub)\n"
                    "/sub - Subscription URL (one link = full cascade)\n"
                    "/buy - Buy a subscription (100 GB/mo, all protocols)\n"
                    "/raw - Raw keys for legacy clients (CDN-only)\n\n"
                    "Need help? Use the support button in the menu."
                )
        
        self.bot.send_message(
            chat_id=chat_id,
            text=help_text,
            parse_mode='HTML'
        )
        
        logger.info(f"Handled /help for user {chat_id}")
    
    def handle_stats(self, update: dict, chat_id: str) -> None:
        """Handle /stats command.
        
        Args:
            update: Telegram update object
            chat_id: User's chat ID
        """
        user_id = self._command_user_id(update) or chat_id
        if self._is_admin(user_id, chat_id):
            # Admin sees system stats
            stats = self.db.get_stats()
            notifier = NotificationService(self.bot, self.db, self.config)
            notifier.notify_stats(chat_id, stats, is_admin=True)
        else:
            # User sees personal stats
            user = self.db.get_user(chat_id)
            if not user or not user.email:
                text = "❌ Your user profile is incomplete or not found."
                self.bot.send_message(chat_id=chat_id, text=text)
                return

            try:
                # Through XUIService, not a raw XUIDatabase: on the entry
                # node there is no local x-ui.db (API-only mode), and a
                # raw connect used to fabricate an empty stub file there.
                xui = None
                if getattr(self.bot, 'services', None):
                    xui = self.bot.services.get('xui')
                if xui is None:
                    from bot.services.xui_service import XUIService
                    xui = XUIService(self.config)
                traffic = xui.get_client_traffic_sync(user.email)

                if traffic:
                    used_traffic_bytes = (
                        traffic.get('upload', 0) + traffic.get('download', 0)
                    )
                    # Real per-client quota from the panel; only fall
                    # back to the demo default when the panel has none.
                    total_limit_bytes = traffic.get('total') or (
                        self.config.DEMO_TRAFFIC_GB * 1024 * 1024 * 1024
                    )

                    percentage = 0
                    if total_limit_bytes > 0:
                        percentage = (used_traffic_bytes / total_limit_bytes) * 100

                    text = (
                        f"📊 <b>Your Traffic Stats</b>\n\n"
                        f"⬆️ Upload: {format_bytes(traffic.get('upload', 0))}\n"
                        f"⬇️ Download: {format_bytes(traffic.get('download', 0))}\n"
                        f"📈 Total Used: {format_bytes(used_traffic_bytes)} / {format_bytes(total_limit_bytes)}\n"
                        f"({percentage:.2f}%)"
                    )
                else:
                    text = "Could not retrieve traffic statistics for your account."

                self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')

            except Exception as e:
                logger.error(f"Error getting user stats for {chat_id}: {e}")
                text = "An error occurred while fetching your stats."
                self.bot.send_message(chat_id=chat_id, text=text)
        
        logger.info(f"Handled /stats for user {chat_id}")
    
    def handle_admin(self, update: dict, chat_id: str) -> None:
        """Handle /admin command — open admin dashboard.

        Works in BOTH the admin's PM and in the forum group. In a 1:1
        chat `chat_id == user_id`, so the old `_is_admin(chat_id)` check
        worked by coincidence. In the forum group `chat_id` is the group
        and `user_id` lives in message.from.id — the old code rejected
        every admin in the forum.
        """
        user = self._get_or_create_user(update)
        user_id = self._command_user_id(update) or chat_id
        if not user or not self._is_admin(user_id, chat_id):
            self.bot.send_message(
                chat_id=chat_id,
                text="🚫 This command is only available for administrators."
            )
            return

        webapp_url = getattr(self.config, 'WEBAPP_URL', '')
        if not webapp_url:
            self.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Dashboard URL is not configured. Please check WEBAPP_URL in .env"
            )
            return

        # Two Bot-API constraints to work around:
        #   1. The persistent Mini-App menu button (left of the input
        #      field) is 1:1-chat-only — silently skipped in groups.
        #   2. Inline `web_app` buttons are also 1:1-chat-only and
        #      cause "Bad Request: BUTTON_TYPE_INVALID" in a forum
        #      group. In groups we fall back to a regular `url` button,
        #      which opens the dashboard in Telegram's in-app browser
        #      (still our valid Let's Encrypt URL, so it works).
        # Append a signed admin_token so the dashboard authenticates
        # even when opened via the `url` fallback (no Telegram WebApp
        # initData available outside `web_app` buttons).
        from bot.utils.admin_token import make_admin_token
        try:
            token = make_admin_token(self.config.BOT_TOKEN, str(user_id))
            sep = '&' if '?' in webapp_url else '?'
            tokened_url = f"{webapp_url}{sep}admin_token={token}"
        except Exception as e:
            logger.warning(f"admin: failed to mint admin_token: {e}")
            tokened_url = webapp_url

        is_pm = str(chat_id) == str(user_id)
        if is_pm:
            self.bot.setup_webapp_menu_button(chat_id=chat_id)
            button = {'text': '📊 Open Dashboard', 'web_app': {'url': tokened_url}}
        else:
            button = {'text': '📊 Open Dashboard', 'url': tokened_url}

        keyboard = {'inline_keyboard': [[button]]}

        # Reply inside the same forum topic the command was sent from,
        # so the admin sees the dashboard button right where they asked.
        thread_id = (update.get('message') or {}).get('message_thread_id')

        self.bot.send_message(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text="<b>Admin Dashboard</b>\n\nTap the button below to open the dashboard.",
            parse_mode='HTML',
            reply_markup=keyboard
        )

    # Mirrors GetKeyHandler._KEY_ALLOWED_STATUSES — only approved users with
    # an active subscription can ask for their key, even if a stale uuid is
    # still sitting in the DB row (e.g. legacy data before reject cleanup).
    _MYKEY_ALLOWED_STATUSES = frozenset({"demo", "paid", "support_topic"})

    def handle_mykey(self, update: dict, chat_id: str) -> None:
        """Handle /mykey command — alias for /sub since the round-robin
        migration. One subscription URL is *the* key; the client picks
        the live outbound on its own via urltest. No more Y/N prompt
        because there's nothing to rotate from the user's side.
        Legacy clients that can't read sing-box subscriptions still
        have /raw.
        """
        self.handle_sub(update, chat_id)

    def handle_sub(self, update: dict, chat_id: str) -> None:
        """Handle /sub command — DM the user their subscription URL.

        Subscription URL bundles all of the user's cascade outbounds
        as one config that the client refreshes on a schedule. Lets us
        rotate IPs/paths without forcing every user to re-import a
        key. ECH is set on each outbound so the user doesn't have to
        flip the global Hiddify toggle.
        """
        from bot.services.subscription import SubscriptionService

        user = self.db.get_user(chat_id)
        if not user or not user.uuid or user.status not in self._MYKEY_ALLOWED_STATUSES:
            if user and user.lang == 'ru':
                text = "❌ У вас еще нет активного VPN ключа.\nИспользуйте /start для запроса доступа."
            else:
                text = "❌ You don't have an active VPN key yet.\nUse /start to request demo access."
            self.bot.send_message(chat_id=chat_id, text=text)
            return

        sub = SubscriptionService(self.config)
        url = sub.build_subscription_url(user)
        if not url:
            self.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Subscription URL временно недоступен. Используйте /mykey.",
            )
            logger.warning(f"/sub: WEBAPP_URL not set, cannot build URL for {chat_id}")
            return

        # iOS RU App Store lost Hiddify AND Happ; Karing (sing-box)
        # survives and reads the plain sing-box subscription.
        is_ios = (getattr(user, 'platform', None) or '') == 'ios'

        # Platform quick-switch: user may have changed devices since
        # onboarding. One tap re-renders the key in the right format.
        plat_keyboard = {'inline_keyboard': [[
            {'text': '🍎 iOS (Karing)', 'callback_data': 'setplat:ios'},
            {'text': '📱 Android', 'callback_data': 'setplat:android'},
            {'text': '💻 ПК', 'callback_data': 'setplat:windows'},
        ]]}

        if is_ios and user.lang != 'en':
            text = (
                "🔗 <b>Subscription URL для Karing</b>\n\n"
                f"<code>{url}</code>\n\n"
                "В Karing: «+» → «Добавить из ссылки» → вставь URL.\n"
                "Один URL = весь каскад протоколов. Когда мы меняем "
                "сервера, клиент сам подтягивает свежее каждые 6 часов."
            )
        elif is_ios:
            text = (
                "🔗 <b>Subscription URL for Karing</b>\n\n"
                f"<code>{url}</code>\n\n"
                "In Karing: «+» → «Add from URL» → paste it.\n"
                "One URL = the whole cascade. When we rotate servers the "
                "client picks it up automatically every 6 hours."
            )
        elif user.lang == 'ru':
            text = (
                "🔗 <b>Subscription URL</b>\n\n"
                f"<code>{url}</code>\n\n"
                "В Hiddify: «+» → «Добавить из ссылки» → вставь URL.\n"
                "Один URL = весь каскад протоколов. Когда мы меняем "
                "сервера, клиент сам подтягивает свежее каждые 6 часов.\n\n"
                "💡 ECH здесь включён автоматически — глобальный тоггл "
                "в настройках Hiddify трогать не надо."
            )
        else:
            text = (
                "🔗 <b>Subscription URL</b>\n\n"
                f"<code>{url}</code>\n\n"
                "In Hiddify: «+» → «Add from URL» → paste it.\n"
                "One URL = the whole cascade. When we rotate servers the "
                "client picks it up automatically every 6 hours.\n\n"
                "💡 ECH is already on per-outbound — no need to flip the "
                "global toggle in Hiddify settings."
            )
        self.bot.send_message(
            chat_id=chat_id, text=text, parse_mode='HTML',
            reply_markup=plat_keyboard,
        )
        logger.info(f"Sent /sub URL to {chat_id}")

    # CDN/stealth-only protocols safe to expose as raw URLs. Reality
    # and Hy2 are deliberately excluded — their raw URLs embed the
    # entry IP / hostname, so a screenshot or chat-leak hands the
    # infra address to anyone who sees it. They live exclusively
    # inside the encrypted subscription JSON.
    _RAW_PROTOCOLS = ('stls', 'ws')   # xhttp retired 2026-08-19: zero clients ever used it

    def handle_raw(self, update: dict, chat_id: str) -> None:
        """Handle /raw command — opt-in fallback for clients that
        can't read sing-box subscription JSON (some old v2rayN
        variants, manual setups). Emits only the CDN-fronted and
        stealth-fronted protocols whose URLs don't leak our entry
        infrastructure. Reality / Hy2 are subscription-only.
        """
        from bot.services.vpn import VPNService
        from bot.handlers.callbacks.user import MyKeyAnswerHandler

        user = self.db.get_user(chat_id)
        if not user or not user.uuid or user.status not in self._MYKEY_ALLOWED_STATUSES:
            if user and user.lang == 'ru':
                text = "❌ У вас еще нет активного VPN ключа.\nИспользуйте /start для запроса доступа."
            else:
                text = "❌ You don't have an active VPN key yet.\nUse /start to request demo access."
            self.bot.send_message(chat_id=chat_id, text=text)
            return

        vpn = VPNService(self.config)
        method_map = MyKeyAnswerHandler.PROTOCOL_METHOD_MAP
        labels_ru = NotificationService.PROTOCOL_LABELS_RU
        labels_en = NotificationService.PROTOCOL_LABELS_EN
        labels = labels_en if user.lang == 'en' else labels_ru
        lines = []
        for proto in self._RAW_PROTOCOLS:
            method_name = method_map.get(proto)
            if not method_name:
                continue
            link = getattr(vpn, method_name)(user.uuid, user.email)
            if not link:
                continue
            title = (labels.get(proto) or (proto, ''))[0]
            lines.append(f"<b>{title}</b>\n<code>{link}</code>")
        if not lines:
            text = ("⚠️ Сейчас raw-ключи недоступны. Используй /sub."
                    if user.lang == 'ru' else
                    "⚠️ Raw keys unavailable right now. Use /sub.")
            self.bot.send_message(chat_id=chat_id, text=text)
            return

        header_ru = (
            "📦 <b>Raw-ключи для legacy-клиентов</b>\n\n"
            "Только CDN/stealth-протоколы — направление "
            "через нашу инфраструктуру здесь скрыто. Reality и Hysteria2 "
            "выдаются только через /sub (там они зашифрованы внутри JSON и "
            "никому не светятся).\n\n"
            "Рекомендуем всё-таки /sub — там урлтест автоматически "
            "переключается между протоколами когда что-то падает."
        )
        header_en = (
            "📦 <b>Raw keys for legacy clients</b>\n\n"
            "CDN/stealth protocols only — these don't reveal our entry "
            "infra in the URL. Reality and Hysteria2 are subscription-only "
            "(encrypted inside JSON, never exposed in chat).\n\n"
            "We still recommend /sub — urltest there switches between "
            "protocols when one goes down."
        )
        header = header_ru if user.lang == 'ru' else header_en
        text = header + "\n\n" + "\n\n".join(lines)

        # Happ / xray-core clients: the same subscription URL with
        # ?format=links returns a share-links list that renders as
        # separate servers in Happ / v2rayNG / Streisand.
        from bot.services.subscription import SubscriptionService
        sub_url = SubscriptionService(self.config).build_subscription_url(user)
        if sub_url:
            happ_ru = (
                "📱 <b>Для v2rayNG / Streisand:</b> добавь как подписку эту "
                "ссылку (каждый протокол появится отдельным сервером):\n"
                f"<code>{sub_url}?format=links</code>"
            )
            happ_en = (
                "📱 <b>For v2rayNG / Streisand:</b> add this as a subscription "
                "(each protocol shows up as a separate server):\n"
                f"<code>{sub_url}?format=links</code>"
            )
            text += "\n\n" + (happ_ru if user.lang == 'ru' else happ_en)
        # Same failure-report button so legacy users have the same
        # escalation path as subscription users.
        btn_label = ("🆘 Не подключается? Сообщить" if user.lang == 'ru'
                     else "🆘 Not connecting? Report it")
        keyboard = {'inline_keyboard': [[
            {'text': btn_label, 'callback_data': 'report_failure'}
        ]]}
        self.bot.send_message(
            chat_id=chat_id, text=text, parse_mode='HTML',
            reply_markup=keyboard,
        )
        logger.info(f"Sent /raw ({len(lines)} protos) to {chat_id}")

    def handle_setemail(self, update: dict, chat_id: str) -> None:
        """Handle /setemail command - save user's email for fallback contact."""
        message = update.get('message', {})
        text = message.get('text', '').strip()

        # Reply inside the same forum topic the command was sent from —
        # without this, send_message() drops the reply into the group's
        # General topic and the admin (who lives in forum topics, never
        # PM) never sees it, even though the command otherwise succeeds.
        thread_id = message.get('message_thread_id')

        # Extract email from command
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            user = self.db.get_user(chat_id)
            lang = user.lang if user else 'ru'
            if lang == 'en':
                text = "📧 <b>Set your email</b>\n\nUsage: /setemail your@email.com\n\nExample: /setemail john@gmail.com"
            else:
                text = "📧 <b>Укажи свой email</b>\n\nИспользование: /setemail твой@email.com\n\nПример: /setemail ivan@gmail.com"
            self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML', message_thread_id=thread_id)
            return

        email = parts[1].strip()

        # Basic email validation
        if '@' not in email or '.' not in email.split('@')[-1]:
            user = self.db.get_user(chat_id)
            lang = user.lang if user else 'ru'
            if lang == 'en':
                text = "❌ Invalid email format.\n\nUsage: /setemail your@email.com"
            else:
                text = "❌ Неверный формат email.\n\nИспользование: /setemail твой@email.com"
            self.bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id)
            return

        # Save email
        user = self.db.get_user(chat_id)
        if not user:
            user = self._get_or_create_user(update)

        # contact_email, NOT user.email: the latter is the synthetic
        # user_<name>_<id>@nekovo.ru identifier keys live under in X-UI.
        # Writing there either did nothing (or-guard) or corrupted the
        # panel identifier for keyless users.
        user.contact_email = email
        self.db.save_user(user)
        self._push_panel_comment(user)

        lang = user.lang or 'ru'
        if lang == 'en':
            text = f"✅ Email saved: {email}\n\nWe'll use it to send backup keys if your VPN gets blocked."
        else:
            text = f"✅ Email сохранён: {email}\n\nИспользуем его для отправки резервного ключа если VPN заблокируют."

        self.bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id)
        logger.info(f"User {chat_id} set contact email")
