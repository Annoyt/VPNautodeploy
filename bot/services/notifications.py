"""Notification Service - User and admin notifications"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any

try:
    # BackgroundScheduler runs in its own thread and does not require a
    # live asyncio loop — the bot uses synchronous polling on the main
    # thread, so AsyncIOScheduler would fail with "no running event loop"
    # at start_scheduler() time.
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False

from bot.config import Platform, MESSAGES

logger = logging.getLogger(__name__)


class NotificationService:
    """Service for sending notifications to users and admins."""
    
    def __init__(self, bot, db, config):
        """Initialize notification service.
        
        Args:
            bot: Telegram bot instance
            db: Database instance  
            config: Bot configuration
        """
        self.bot = bot
        self.db = db
        self.config = config
        self.scheduler: Optional["BackgroundScheduler"] = None
        # Held by start_scheduler so _alert_tick_sync can use it.
        # Lives on bot.services['alert_manager'] for the callback
        # handler that processes the "✅ ACK" buttons.
        self._alert_manager = None
    
    # ========== User Notifications ==========
    
    def notify_welcome(self, chat_id: str, lang: str = 'ru') -> bool:
        """Send welcome message to new user."""
        text = MESSAGES[lang]['welcome']
        btn_support = '💬 Поддержка' if lang == 'ru' else '💬 Support'
        keyboard = {
            'inline_keyboard': [
                [{
                    'text': '🎁 Запросить демо' if lang == 'ru' else '🎁 Request Demo',
                    'callback_data': 'request_demo',
                }],
                [{'text': btn_support, 'callback_data': 'support'}],
            ]
        }
        
        try:
            self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode='HTML'
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send welcome: {e}")
            return False
    
    def notify_pending(self, chat_id: str, lang: str = 'ru') -> bool:
        """Notify user their request is pending."""
        texts = {
            'ru': '⏳ Заявка отправлена администратору.\nОжидайте подтверждения.',
            'en': '⏳ Request sent to administrator.\nPlease wait for approval.'
        }
        text = texts.get(lang, texts['ru'])
        
        try:
            self.bot.send_message(chat_id=chat_id, text=text)
            return True
        except Exception as e:
            logger.error(f"Failed to send pending: {e}")
            return False
    
    def notify_main_menu(self, chat_id: str, lang: str = 'ru') -> bool:
        """Show main menu to active users."""
        texts = {
            'ru': '👋 Главное меню NekoVPN\n\nВыберите действие:',
            'en': '👋 NekoVPN Main Menu\n\nChoose an action:'
        }
        text = texts.get(lang, texts['ru'])
        btn_stats = {'ru': '📊 Статистика', 'en': '📊 Statistics'}
        btn_my_key = {'ru': '🔑 Мой ключ', 'en': '🔑 My Key'}
        btn_support = {'ru': '💬 Поддержка', 'en': '💬 Support'}
        btn_buy = {'ru': '💳 Купить подписку', 'en': '💳 Buy subscription'}
        keyboard = {
            'inline_keyboard': [
                [{'text': btn_stats.get(lang, btn_stats['ru']), 'callback_data': 'stats'},
                 {'text': btn_my_key.get(lang, btn_my_key['ru']), 'callback_data': 'my_key'}],
                [{'text': btn_buy.get(lang, btn_buy['ru']), 'callback_data': 'buy_menu'}],
                [{'text': btn_support.get(lang, btn_support['ru']), 'callback_data': 'support'}]
            ]
        }
        
        try:
            self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send main menu: {e}")
            return False
    
    def notify_approved_admin_migration(self, user, admin_id: str) -> bool:
        """Notify USERS topic about approved user (admin migration notification)."""
        if not self.config.FORUM_ENABLED:
            return False
            
        username_display = f"@{user.username}" if user.username else "no_username"
        text = (
            f"✅ <b>User Approved</b>\n\n"
            f"👤 User: <code>{user.chat_id}</code> ({username_display})\n"
            f"👨‍💻 Approved by: <code>{admin_id}</code>\n"
            f"📧 Email: <code>{user.email}</code>"
        )
        
        try:
            self.bot.send_message_to_topic(
                chat_id=self.config.FORUM_GROUP_ID,
                message_thread_id=self.config.TOPIC_USERS,
                text=text,
                parse_mode='HTML'
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send admin migration notification: {e}")
            return False
    
    def notify_approved(self, chat_id: str, lang: str = 'ru') -> bool:
        """Notify user their request was approved."""
        texts = {
            'ru': '✅ Ваша заявка одобрена!\n\nВыберите платформу.\nЕсли что-то непонятно — напишите в поддержку.',
            'en': '✅ Your request has been approved!\n\nSelect your platform.\nIf anything is unclear — message support.'
        }
        text = texts.get(lang, texts['ru'])
        btn_support = '💬 Поддержка' if lang == 'ru' else '💬 Support'
        keyboard = {
            'inline_keyboard': [
                [{'text': '📱 Android', 'callback_data': 'platform:android'},
                 {'text': '🍎 iOS', 'callback_data': 'platform:ios'}],
                [{'text': '💻 Windows', 'callback_data': 'platform:windows'},
                 {'text': '🐧 Linux', 'callback_data': 'platform:linux'}],
                [{'text': btn_support, 'callback_data': 'support'}]
            ]
        }
        
        try:
            self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send approved: {e}")
            return False
    
    def notify_platform_selected(self, chat_id: str, platform: Platform, lang: str = 'ru') -> bool:
        """Notify user about platform selection."""
        platform_name = platform.name if hasattr(platform, 'name') else str(platform)
        texts = {
            'ru': f"📱 Платформа: {platform_name}\n\nНажмите кнопку ниже для получения ключа:",
            'en': f"📱 Platform: {platform_name}\n\nTap the button below to get your key:"
        }
        text = texts.get(lang, texts['ru'])
        keyboard = {
            'inline_keyboard': [[{
                'text': '🔑 Получить ключ',
                'callback_data': 'generate_key'
            }]]
        }
        
        try:
            self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send platform selected: {e}")
            return False
    
    # Labels are keyed by the short protocol name (stls/ws/hy2/reality),
    # not by cascade index, because the index meaning changes whenever
    # the operator reorders the cascade in the dashboard. The position
    # within that order (#1 / #2 / #3) is rendered separately from the
    # protocol description so a reorder doesn't desync the wording.
    PROTOCOL_LABELS_RU = {
        'stls':    ("🛡 ShadowTLS",
                    "Маскируется под обычный HTTPS к microsoft.com. Самый DPI-устойчивый. Чуть медленнее остальных."),
        'ws':      ("🌐 VMess через CDN (Cloudflare, WebSocket)",
                    "Трафик идёт через серверы Cloudflare. Хорош когда блочат прямые подключения, но CF могут резать в отдельных сетях."),
        'xhttp':   ("🌐 VMess через CDN (Cloudflare, XHTTP)",
                    "Альтернативный CDN-транспорт с другим DPI-отпечатком — пригодится, если первый CDN-вариант начали фильтровать."),
        'hy2':     ("🛟 Hysteria2 (UDP)",
                    "UDP-протокол. Проходит там, где TCP-VPN режут. Быстрый, но UDP-порты часто фильтруются провайдерами."),
        'reality': ("🔑 Reality (TCP)",
                    "Классический VLESS+Reality. Самый быстрый из всех, но первым выходит из строя если DPI начинает профайлить нашу инфру."),
    }
    PROTOCOL_LABELS_EN = {
        'stls':    ("🛡 ShadowTLS",
                    "Indistinguishable from regular HTTPS to microsoft.com. The most DPI-resistant. Slightly slower than the rest."),
        'ws':      ("🌐 VMess over CDN (Cloudflare, WebSocket)",
                    "Traffic routes through Cloudflare. Useful when direct connections get throttled, but CF itself is filtered in some networks."),
        'xhttp':   ("🌐 VMess over CDN (Cloudflare, XHTTP)",
                    "Second CDN transport with a different DPI fingerprint — useful if the first CDN variant gets filtered."),
        'hy2':     ("🛟 Hysteria2 (UDP)",
                    "UDP protocol. Slips through where TCP VPNs get throttled. Fast, but UDP ports are often filtered."),
        'reality': ("🔑 Reality (TCP)",
                    "Classic VLESS+Reality. Fastest of the four, but the first to die once DPI starts profiling our infra."),
    }

    # Dashboard key-text overrides app_settings key. Kept after the
    # round-robin migration so handle_admin_key_texts_get/set
    # endpoints have a single source of truth even though no live
    # message template reads them any more (the dashboard editor will
    # be retired in a later UI cleanup pass).
    TEXT_OVERRIDES_KEY = 'key_message_texts'

    def notify_key_generated(
        self, chat_id: str, key: str, lang: str = 'ru',
        hy2_link: Optional[str] = None,
        ws_link: Optional[str] = None,
        stls_link: Optional[str] = None,
    ) -> bool:
        """Send primary VLESS+Reality link with a cascading-fallback button.

        Only the primary link goes out here. ``hy2_link`` and ``ws_link``
        are kept as parameters so the caller's API stays the same, but
        they're used only to decide which alt-protocol button to attach.
        TryAltProtocolHandler regenerates the next protocol on demand
        when the user taps the button. Order: Reality → Hy2 → WS → support.
        """
        if lang == 'en':
            text = (
                f"🔑 <b>Your VPN key:</b>\n\n<code>{key}</code>\n\n"
                f"Import into <a href=\"https://hiddify.com/\">Hiddify</a> "
                f"and connect.\n\n"
                f"If it doesn't connect — tap the button below to try a "
                f"different protocol."
            )
            btn_hy2 = "❌ Doesn't connect? Try UDP backup →"
            btn_ws = "❌ Doesn't connect? Try CDN backup →"
            btn_stls = "❌ Doesn't connect? Try ShadowTLS →"
        else:
            text = (
                f"🔑 <b>Ваш VPN ключ:</b>\n\n<code>{key}</code>\n\n"
                f"Импортируйте в <a href=\"https://hiddify.com/\">Hiddify</a> "
                f"и подключитесь.\n\n"
                f"Если не подключается — нажмите кнопку ниже, "
                f"попробуем другой способ обхода."
            )
            btn_hy2 = "❌ Не подключается? Резервный (UDP) →"
            btn_ws = "❌ Не подключается? Резервный (CDN) →"
            btn_stls = "❌ Не подключается? ShadowTLS →"

        keyboard = None
        if hy2_link:
            keyboard = {'inline_keyboard': [[
                {'text': btn_hy2, 'callback_data': 'try_alt:hy2'}
            ]]}
        elif ws_link:
            keyboard = {'inline_keyboard': [[
                {'text': btn_ws, 'callback_data': 'try_alt:ws'}
            ]]}
        elif stls_link:
            keyboard = {'inline_keyboard': [[
                {'text': btn_stls, 'callback_data': 'try_alt:stls'}
            ]]}
        try:
            self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send key: {e}")
            return False

    def notify_alt_protocol(
        self, chat_id: str, link: str, protocol: str,
        next_protocol: Optional[str] = None, lang: str = 'ru',
    ) -> bool:
        """Send an alternative-protocol link with next-step button.

        ``protocol`` is the label of the link being sent ('hy2' or 'ws');
        ``next_protocol``, when set, attaches a "try the next one" button.
        When None, the message ends with a hint to contact support.
        """
        if protocol == 'hy2':
            head_ru = ("🛟 <b>Резервный способ #1 — Hysteria2 (UDP):</b>\n\n"
                       "Этот вариант идёт через UDP — обычно проходит там, "
                       "где TCP-VPN режут.")
            head_en = ("🛟 <b>Backup #1 — Hysteria2 (UDP):</b>\n\n"
                       "This one runs over UDP — usually slips through "
                       "where TCP VPNs get throttled.")
        elif protocol == 'ws':
            head_ru = ("🌐 <b>Резервный способ #2 — через CDN (Cloudflare):</b>\n\n"
                       "Трафик идёт через сервера Cloudflare. Самый медленный, "
                       "но переживает почти любой DPI.")
            head_en = ("🌐 <b>Backup #2 — via CDN (Cloudflare):</b>\n\n"
                       "Traffic routes through Cloudflare. Slowest of the "
                       "three but survives almost any DPI.")
        else:  # 'stls'
            head_ru = ("🛡 <b>Резервный способ #3 — ShadowTLS:</b>\n\n"
                       "Трафик неотличим от обычного HTTPS к microsoft.com. "
                       "Это последний рубеж — если этот не работает, "
                       "блокировка идёт самим Microsoft (вряд ли).")
            head_en = ("🛡 <b>Backup #3 — ShadowTLS:</b>\n\n"
                       "Traffic is indistinguishable from regular HTTPS to "
                       "microsoft.com. Last line of defense — if this fails, "
                       "they'd have to block Microsoft itself.")

        next_button_labels = {
            'ws': ("❌ Still doesn't work? Try CDN →", "❌ Тоже не работает? CDN →"),
            'stls': ("❌ Still doesn't work? Try ShadowTLS →", "❌ Тоже не работает? ShadowTLS →"),
        }

        if lang == 'en':
            text = f"{head_en}\n\n<code>{link}</code>\n\n"
            if next_protocol:
                text += "Import into Hiddify, replace the previous key, and reconnect."
                btn_text = next_button_labels.get(next_protocol, ("❌ Still doesn't work? Next →",))[0]
            else:
                text += (
                    "Import into Hiddify, replace the previous key, and reconnect.\n\n"
                    "If even this one fails — tap below to reach support."
                )
                btn_text = "💬 Contact support"
        else:
            text = f"{head_ru}\n\n<code>{link}</code>\n\n"
            if next_protocol:
                text += "Импортируйте в Hiddify вместо предыдущего и подключитесь."
                btn_text = next_button_labels.get(next_protocol, ("", "❌ Тоже не работает? Следующий →"))[1]
            else:
                text += (
                    "Импортируйте в Hiddify вместо предыдущего и подключитесь.\n\n"
                    "Если и этот не работает — нажмите кнопку, чтобы написать в поддержку."
                )
                btn_text = "💬 Написать в поддержку"

        cb = f"try_alt:{next_protocol}" if next_protocol else 'support'
        keyboard = {'inline_keyboard': [[
            {'text': btn_text, 'callback_data': cb}
        ]]}
        try:
            self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode='HTML',
                reply_markup=keyboard,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send alt-protocol link: {e}")
            return False
    
    def notify_rejected(self, chat_id: str, reason: str = None, lang: str = 'ru') -> bool:
        """Notify user their request was rejected."""
        reason_text = reason if reason else 'Not specified'
        texts = {
            'ru': f"❌ Заявка отклонена.\n\nПричина: {reason_text}",
            'en': f"❌ Request rejected.\n\nReason: {reason_text}"
        }
        text = texts.get(lang, texts['ru'])
        
        try:
            self.bot.send_message(chat_id=chat_id, text=text)
            return True
        except Exception as e:
            logger.error(f"Failed to send rejected: {e}")
            return False
    
    def notify_rejected_can_retry(self, chat_id: str, user) -> bool:
        """Rejected user with retry option + rate-limit info."""
        remaining = max(0, self.config.MAX_REJECT_RETRIES - (user.reject_count or 0))
        lang = user.lang or 'ru'
        texts = {
            'ru': f"❌ Заявка отклонена.\nОсталось попыток: {remaining}",
            'en': f"❌ Request rejected.\nRetries remaining: {remaining}"
        }
        keyboard = None
        if remaining > 0:
            btn_text = {'ru': '🔄 Повторить заявку', 'en': '🔄 Re-apply'}
            keyboard = {'inline_keyboard': [[{
                'text': btn_text.get(lang, btn_text['ru']),
                'callback_data': 'request_demo'
            }]]}
        try:
            self.bot.send_message(
                chat_id=chat_id,
                text=texts.get(lang, texts['ru']),
                reply_markup=keyboard
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send rejected can retry: {e}")
            return False
    
    def notify_banned(self, chat_id: str, lang: str = 'ru') -> bool:
        """Permanent ban notification."""
        texts = {
            'ru': "🚫 Аккаунт заблокирован. Обратитесь к администратору.",
            'en': "🚫 Account banned. Contact administrator."
        }
        try:
            self.bot.send_message(chat_id=chat_id, text=texts.get(lang, texts['ru']))
            return True
        except Exception as e:
            logger.error(f"Failed to send banned: {e}")
            return False
    
    # ========== Admin Notifications ==========
    
    def notify_new_request(self, user) -> int:
        """Notify admin about new demo request.
        
        Returns:
            message_id or topic_id
        """
        username = f"@{user.username}" if user.username else f"user_{user.chat_id}"
        text = f"🆕 <b>New Demo Request</b>\n\nUser: {username}\nID: <code>{user.chat_id}</code>"
        keyboard = {
            'inline_keyboard': [
                [
                    {'text': '✅ Approve', 'callback_data': f'approve:{user.chat_id}'},
                    {'text': '❌ Reject', 'callback_data': f'reject:{user.chat_id}'}
                ]
            ]
        }
        
        try:
            if self.config.FORUM_ENABLED:
                result = self.bot.send_message_to_topic(
                    chat_id=self.config.FORUM_GROUP_ID,
                    text=text,
                    message_thread_id=self.config.TOPIC_REQUESTS,
                    reply_markup=keyboard,
                    parse_mode='HTML'
                )
                return result['message_id']
            else:
                result = self.bot.send_message(
                    chat_id=self.config.SUPER_ADMIN_ID,
                    text=text,
                    reply_markup=keyboard
                )
                return result['message_id']
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")
            return None
    
    def notify_auto_approved(self, user) -> Optional[int]:
        """Lightweight admin notification for an auto-approved user.

        No action buttons — auto-approval has already moved the user
        to PLATFORM_SELECT. This is informational so the admin can
        still see the funnel in TOPIC_REQUESTS / SUPER_ADMIN DM. If
        the user turns out to be abusive, the admin can /ban or /reset
        them after the fact.
        """
        username = f"@{user.username}" if user.username else f"user_{user.chat_id}"
        text = (
            f"⚡ <b>Auto-approved</b>\n\n"
            f"User: {username}\n"
            f"ID: <code>{user.chat_id}</code>\n\n"
            f"<i>Has Telegram @username → fast-tracked to platform select.</i>"
        )
        try:
            if self.config.FORUM_ENABLED:
                result = self.bot.send_message_to_topic(
                    chat_id=self.config.FORUM_GROUP_ID,
                    text=text,
                    message_thread_id=self.config.TOPIC_REQUESTS,
                    parse_mode='HTML',
                )
                return result.get('message_id') if result else None
            result = self.bot.send_message(
                chat_id=self.config.SUPER_ADMIN_ID,
                text=text,
                parse_mode='HTML',
            )
            return result.get('message_id') if result else None
        except Exception as e:
            logger.error(f"notify_auto_approved failed: {e}")
            return None

    def notify_new_support_ticket(self, user, issue_text: str) -> int:
        """Create/notify about new support ticket.

        The initial topic message carries an inline keyboard with a
        "🔒 Закрыть тикет" button (callback ``close_ticket:<topic_id>``)
        so the admin can resolve the ticket without typing ``/close``.
        See ``CloseTicketHandler`` and ``ForumHandler.handle_close_ticket``.

        Returns:
            topic_id or message_id
        """
        username = f"@{user.username}" if user.username else f"user_{user.chat_id}"

        # Build a topic title that is searchable in the topic list: the
        # @handle plus the first line of the issue (≤60 chars). Telegram
        # caps topic names at 128 chars, so we stay well under that.
        snippet = (issue_text or '').strip().splitlines()[0] if (issue_text or '').strip() else ''
        if len(snippet) > 60:
            snippet = snippet[:57] + '…'
        title_parts = [f"🎫 {username}"]
        if snippet:
            title_parts.append(snippet)
        topic_name = ' · '.join(title_parts)[:128]

        # Three-button keyboard so the admin can resolve common
        # outcomes without leaving the topic: close, PM the user,
        # ban the user. The PM button is a deep-link URL (works in
        # both PM and groups since Telegram doesn't enforce
        # BUTTON_TYPE_INVALID for tg:// URLs in inline KBs of bot
        # messages).
        pm_url = (
            f"https://t.me/{user.username}"
            if user.username
            else f"tg://user?id={user.chat_id}"
        )

        try:
            if self.config.FORUM_ENABLED:
                topic_id = self.bot.create_forum_topic(
                    chat_id=self.config.FORUM_GROUP_ID,
                    name=topic_name,
                )

                ticket_kb = {
                    'inline_keyboard': [[
                        {
                            'text': '🔒 Закрыть',
                            'callback_data': f'close_ticket:{topic_id}',
                        },
                        {
                            'text': '📞 PM',
                            'url': pm_url,
                        },
                        {
                            'text': '🚫 Бан',
                            'callback_data': f'ban_from_ticket:{user.chat_id}:{topic_id}',
                        },
                    ]]
                }
                self.bot.send_message_to_topic(
                    chat_id=self.config.FORUM_GROUP_ID,
                    text=f"🆘 <b>Support Request</b>\nUser: {username}\nID: <code>{user.chat_id}</code>\n\n{issue_text}",
                    message_thread_id=topic_id,
                    parse_mode='HTML',
                    reply_markup=ticket_kb,
                )
                return topic_id
            else:
                result = self.bot.send_message(
                    chat_id=self.config.SUPER_ADMIN_ID,
                    text=f"🆘 <b>Support Request</b>\nUser: {username}\nID: <code>{user.chat_id}</code>\n\n{issue_text}",
                    parse_mode='HTML'
                )
                return result['message_id']
        except Exception as e:
            logger.error(f"Failed to create support ticket: {e}")
            return None
    
    def notify_payment_approved(self, chat_id: str, lang: str = 'ru') -> bool:
        """Notify user that payment was approved."""
        messages = {
            'ru': "✅ Оплата подтверждена! Ваша подписка активирована.",
            'en': "✅ Payment confirmed! Your subscription is now active."
        }
        text = messages.get(lang, messages['ru'])
        try:
            self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            return True
        except Exception as e:
            logger.error(f"Failed to notify payment approved: {e}")
            return False
    
    def notify_payment_issue(self, user, issue: str) -> bool:
        """Notify admin about payment issue."""
        username = f"@{user.username}" if user.username else f"user_{user.chat_id}"
        text = f"💰 <b>Payment Issue</b>\nUser: {username}\nID: <code>{user.chat_id}</code>\n\n{issue}"
        
        try:
            if self.config.FORUM_ENABLED:
                self.bot.send_message_to_topic(
                    chat_id=self.config.FORUM_GROUP_ID,
                    text=text,
                    message_thread_id=self.config.TOPIC_PAYMENTS,
                    parse_mode='HTML'
                )
            else:
                self.bot.send_message(
                    chat_id=self.config.SUPER_ADMIN_ID,
                    text=text,
                    parse_mode='HTML'
                )
            return True
        except Exception as e:
            logger.error(f"Failed to notify payment issue: {e}")
            return False
    
    def notify_stats(self, chat_id: str, stats: dict, is_admin: bool = False) -> bool:
        """Send statistics to user or admin."""
        if is_admin:
            text = f"📊 <b>Statistics</b>\n\n{stats}"
        else:
            text = "📊 Your Statistics:\nNo data available" if not stats else f"📊 Stats: {stats}"
        
        try:
            self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
            return True
        except Exception as e:
            logger.error(f"Failed to send stats: {e}")
            return False
    
    # ========== Support Management ==========
    
    def forward_to_support(self, user, message: dict) -> int:
        """Forward user message to support; auto-recreate the topic if it's gone.

        When an admin deletes the forum topic (or a migration loses it),
        the stored ``support_topic_id`` becomes stale. forwardMessage then
        returns ``Bad Request: message thread not found`` and the user's
        ticket silently fails forever. We catch that case here: clear the
        stale id, mint a fresh topic via notify_new_support_ticket, save
        it back on the user, and retry the forward.
        """
        if not getattr(user, 'support_topic_id', None):
            logger.warning(f"User {user.chat_id} has no support topic, cannot forward")
            return None

        target_chat = (
            self.config.FORUM_GROUP_ID
            if self.config.FORUM_ENABLED
            else self.config.SUPER_ADMIN_ID
        )

        def _try_forward(thread_id):
            try:
                result = self.bot.forward_message(
                    chat_id=target_chat,
                    from_chat_id=user.chat_id,
                    message_id=message['message_id'],
                    message_thread_id=thread_id,
                )
                if not result:
                    return None
                return result.get('message_id')
            except Exception as e:
                logger.error(f"Failed to forward to support: {e}")
                return None

        msg_id = _try_forward(user.support_topic_id)
        if msg_id is not None:
            return msg_id

        # Topic is gone (or chat lost permissions). Recreate it.
        logger.warning(
            f"forward_to_support: thread {user.support_topic_id} unusable for "
            f"user {user.chat_id}, recreating topic"
        )
        stale_id = user.support_topic_id
        user.support_topic_id = None
        try:
            self.db.save_user(user)
        except Exception as e:
            logger.error(f"Failed to clear stale support_topic_id={stale_id}: {e}")

        recovery_text = (
            message.get('text')
            or message.get('caption')
            or "[предыдущая тема обращения недоступна, пересоздаём]"
        )
        new_topic = self.notify_new_support_ticket(user, recovery_text)
        if not new_topic:
            return None

        user.support_topic_id = new_topic
        try:
            self.db.save_user(user)
        except Exception as e:
            logger.error(f"Failed to save new support_topic_id={new_topic}: {e}")

        return _try_forward(new_topic)
    
    def reply_to_user(self, user_chat_id: str, text: str, admin_chat_id: str = None) -> bool:
        """Send reply from admin to user."""
        try:
            self.bot.send_message(
                chat_id=user_chat_id,
                text=f"💬 <b>Support Reply:</b>\n\n{text}",
                parse_mode='HTML'
            )
            
            if admin_chat_id:
                self.bot.send_message(
                    chat_id=admin_chat_id,
                    text="✅ Reply sent to user."
                )
            return True
        except Exception as e:
            logger.error(f"Failed to reply to user: {e}")
            return False
    
    # ========== Scheduled Notifications (from XRay-bot) ==========
    
    def _check_expiring_subscriptions_sync(self):
        """Sync wrapper so BackgroundScheduler can invoke the async job.

        BackgroundScheduler runs jobs in its own thread; there is no event
        loop there, so we create one on-demand for this single coroutine.
        """
        import asyncio
        try:
            asyncio.run(self.check_expiring_subscriptions())
        except Exception as e:
            logger.exception(f"check_expiring_subscriptions failed: {e}")

    def _cleanup_old_tickets_sync(self):
        """Daily housekeeping: drop ticket_messages older than 30 days.

        The compiled log lives forever in the Telegram Solved topic;
        the SQLite rows are only useful while a ticket is open or
        being reviewed shortly after close.
        """
        try:
            deleted = self.db.cleanup_old_ticket_messages(days=30)
            if deleted:
                logger.info(f"Ticket cleanup: deleted {deleted} old message rows")
        except Exception as e:
            logger.exception(f"ticket cleanup failed: {e}")

    def _repair_stuck_support_users_sync(self):
        """Hourly: revert users stuck in support_topic with NULL topic_id.

        Pattern: user clicks "Поддержка" → state transitions to
        SUPPORT_TOPIC → user never writes → no topic ever gets created
        → user.support_topic_id stays NULL → dashboard shows them as
        support_topic forever. This sweep flips them back to their
        previous state (or DEMO / NEW based on whether a key was issued).
        """
        try:
            import sqlite3
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT chat_id, previous_state, email FROM users "
                    "WHERE status = 'support_topic' AND support_topic_id IS NULL"
                ).fetchall()
            if not rows:
                return
            from bot.config.constants import UserState
            from bot.core.state_machine import StateMachine
            sm = StateMachine(self.db)
            fixed = 0
            for r in rows:
                chat_id, prev, email = r[0], r[1], r[2]
                if prev in ('demo', 'paid'):
                    target = UserState(prev)
                else:
                    target = UserState.DEMO if email else UserState.NEW
                if sm.set_state(chat_id, target):
                    fixed += 1
            if fixed:
                logger.info(f"Support-state repair: reverted {fixed} stuck users")
        except Exception as e:
            logger.exception(f"support-state repair failed: {e}")

    def _reset_demo_quota_sync(self):
        """Monthly: reset traffic counters for demo AND paid users.

        Freemium model: demo users get DEMO_TRAFFIC_GB every calendar
        month, forever. Per user the panel client is re-enabled, its
        expiry pushed 30 days forward (so 3x-ui does not purge it as
        "expired") and its traffic zeroed.

        Paid users get their counters zeroed too (quota is "N GB per
        month") — but their quota amount and paid-until date stay
        admin-managed; see ``_reset_paid_quota_sync``.

        Primary path is the panel HTTP API (on entry the bot has no
        writable x-ui.db); the old direct-DB writes are kept as a
        fallback for deployments with a local panel database. Users are
        notified only after their refresh actually succeeded.
        """
        import sqlite3
        import time as _time
        from datetime import datetime, timedelta
        from bot.services.xui_service import XUIService

        bot_db_path = getattr(self.config, 'DB_PATH', None)
        if not bot_db_path:
            logger.warning("demo quota reset: DB_PATH not configured")
            return

        xui = XUIService(self.config)

        # Paid pass first — it must run even when there are no demo
        # users (the demo pass below early-returns in that case).
        try:
            self._reset_paid_quota_sync(xui, bot_db_path)
        except Exception as e:
            logger.exception(f"paid quota reset failed: {e}")

        new_expiry_ms = int((datetime.utcnow() + timedelta(days=30)).timestamp() * 1000)
        demo_bytes = int(getattr(self.config, 'DEMO_TRAFFIC_GB', 5) or 5) * 1024 ** 3

        try:
            conn = sqlite3.connect(bot_db_path)
            conn.row_factory = sqlite3.Row
            demo_users = conn.execute(
                "SELECT chat_id, email FROM users WHERE status = 'demo' AND email IS NOT NULL"
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.exception(f"demo quota reset: failed to read demo users: {e}")
            return

        if not demo_users:
            logger.info("demo quota reset: no demo users found")
            return

        renewed = []   # [(chat_id, email)] that actually got refreshed
        if xui.api:
            for u in demo_users:
                if xui.renew_client_sync(u['email'], new_expiry_ms,
                                         total_bytes=demo_bytes):
                    renewed.append((u['chat_id'], u['email']))
                else:
                    logger.warning(
                        f"demo quota reset: API renew failed for {u['email']}"
                    )
            logger.info(
                f"demo quota reset: renewed {len(renewed)}/{len(demo_users)} "
                f"demo clients via panel API"
            )
        else:
            emails = [u['email'] for u in demo_users]
            if not self._reset_demo_quota_db_fallback(emails, new_expiry_ms):
                return
            renewed = [(u['chat_id'], u['email']) for u in demo_users]

        if not renewed:
            return

        reset_emails = [email for _, email in renewed]

        # Zero bot.db counters so dashboard reads fresh numbers.
        try:
            conn = sqlite3.connect(bot_db_path)
            placeholders = ','.join('?' * len(reset_emails))
            conn.execute(
                f"UPDATE users SET traffic_up = 0, traffic_down = 0, "
                f"last_traffic_update = ? WHERE email IN ({placeholders})",
                (datetime.utcnow().isoformat(), *reset_emails),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.exception(f"demo quota reset: failed to update bot db: {e}")

        for chat_id, email in renewed:
            try:
                # Notify user quietly; failures are non-fatal.
                self.bot.send_message(
                    chat_id=chat_id,
                    text="🔄 Ваш тестовый трафик 5 ГБ обновлён на новый месяц.\n"
                         "Your 5 GB demo traffic has been refreshed for the new month.",
                    parse_mode='HTML',
                )
            except Exception as e:
                logger.warning(f"demo quota reset: notify {email} failed: {e}")
            _time.sleep(0.1)   # stay clear of Telegram's send rate limit

    def _reset_paid_quota_sync(self, xui, bot_db_path: str) -> None:
        """Monthly counter reset for paid users.

        Paid quota is "N GB per month": on the 1st the counters go back
        to zero and a depletion-disabled client is re-enabled. The quota
        amount and the paid-until date are deliberately NOT touched —
        those are admin-managed (/quota, /expire). Users whose
        subscription already lapsed are skipped: payment, not the
        calendar, brings them back.
        """
        import sqlite3
        import time as _time
        from datetime import datetime

        try:
            conn = sqlite3.connect(bot_db_path)
            conn.row_factory = sqlite3.Row
            paid_users = conn.execute(
                "SELECT chat_id, email, subscription_expiry FROM users "
                "WHERE status = 'paid' AND email IS NOT NULL"
            ).fetchall()
            conn.close()
        except Exception as e:
            logger.exception(f"paid quota reset: failed to read paid users: {e}")
            return

        if not paid_users:
            return

        now = datetime.utcnow()
        renewed = []
        for u in paid_users:
            expiry = u['subscription_expiry']
            if expiry:
                try:
                    if datetime.fromisoformat(expiry) < now:
                        continue   # lapsed — stays off until re-payment
                except ValueError:
                    pass
            try:
                ok = (
                    xui.sync_client_settings_sync(u['email'], {'enable': True})
                    and xui.reset_client_traffic_sync(u['email'])
                )
            except Exception as e:
                logger.warning(f"paid quota reset: {u['email']}: {e}")
                ok = False
            if ok:
                renewed.append((u['chat_id'], u['email']))
            else:
                logger.warning(f"paid quota reset: renew failed for {u['email']}")

        logger.info(
            f"paid quota reset: {len(renewed)}/{len(paid_users)} paid clients reset"
        )
        if not renewed:
            return

        emails = [email for _, email in renewed]
        try:
            conn = sqlite3.connect(bot_db_path)
            placeholders = ','.join('?' * len(emails))
            conn.execute(
                f"UPDATE users SET traffic_up = 0, traffic_down = 0, "
                f"last_traffic_update = ? WHERE email IN ({placeholders})",
                (now.isoformat(), *emails),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.exception(f"paid quota reset: bot.db update failed: {e}")

        for chat_id, email in renewed:
            try:
                # Notify quietly; email-only users (chat_id "ext_…")
                # simply fail here and that's fine.
                self.bot.send_message(
                    chat_id=chat_id,
                    text="🔄 Ваш месячный трафик обновлён.\n"
                         "Your monthly traffic has been refreshed.",
                    parse_mode='HTML',
                )
            except Exception as e:
                logger.warning(f"paid quota reset: notify {email} failed: {e}")
            _time.sleep(0.1)   # stay clear of Telegram's send rate limit

    def _reset_demo_quota_db_fallback(self, reset_emails, new_expiry_ms) -> bool:
        """Direct x-ui.db writes — only for deployments where the panel
        database is local and writable (the bot on entry is API-only)."""
        import sqlite3

        xui_db_path = getattr(self.config, 'XUI_DB_PATH', None)
        if not xui_db_path:
            logger.warning("demo quota reset: XUI_DB_PATH not configured")
            return False

        try:
            # mode=rw: never create the file — a plain connect on the
            # entry node's sentinel path once fabricated a stub DB that
            # silently re-enabled (broken) DB mode for a month.
            xui_conn = sqlite3.connect(f"file:{xui_db_path}?mode=rw", uri=True)
            xui_conn.row_factory = sqlite3.Row
            c = xui_conn.cursor()

            # 1. Reset x-ui traffic counters and extend expiry for matching emails.
            placeholders = ','.join('?' * len(reset_emails))
            c.execute(
                f"UPDATE client_traffics SET up = 0, down = 0, all_time = 0, "
                f"expiry_time = ?, enable = 1 WHERE email IN ({placeholders})",
                (new_expiry_ms, *reset_emails),
            )
            traffic_updated = c.rowcount

            # 2. Ensure the clients table has enable=1 (this is what xray config gen uses).
            c.execute(
                f"UPDATE clients SET enable = 1, expiry_time = ? "
                f"WHERE email IN ({placeholders})",
                (new_expiry_ms, *reset_emails),
            )
            clients_updated = c.rowcount

            # 3. Update inbounds.settings JSON to enable + future expiry.
            updated_inbounds = 0
            for row in c.execute("SELECT id, settings FROM inbounds").fetchall():
                try:
                    settings = json.loads(row['settings'] or '{}')
                except Exception:
                    continue
                clients = settings.get('clients', [])
                changed = False
                for client in clients:
                    if client.get('email') in reset_emails:
                        client['enable'] = True
                        client['expiryTime'] = new_expiry_ms
                        changed = True
                if changed:
                    c.execute(
                        "UPDATE inbounds SET settings = ? WHERE id = ?",
                        (json.dumps(settings), row['id']),
                    )
                    updated_inbounds += 1

            xui_conn.commit()
            xui_conn.close()

            logger.info(
                f"demo quota reset: {len(reset_emails)} users, "
                f"client_traffics={traffic_updated}, clients={clients_updated}, "
                f"inbounds={updated_inbounds}"
            )
        except Exception as e:
            logger.exception(f"demo quota reset: failed to update x-ui db: {e}")
            return False

        # Reload xray so the enabled/expiry changes take effect.
        try:
            from bot.services.xui_service import XUIService
            xui = XUIService(self.config)
            if xui.reload_xray_sync():
                logger.info("demo quota reset: xray reloaded")
            else:
                logger.warning("demo quota reset: xray reload returned False")
        except Exception as e:
            logger.exception(f"demo quota reset: xray reload failed: {e}")
        return True

    def _keep_xray_log_readable_sync(self):
        """Hourly: chmod 644 on Xray access.log AND error.log so the
        bot (uid 1000) can read them. Xray creates the files with mode
        600 by default, which would lock the bot out across rotations
        and after every SIGUSR1 reload that recreates them. Cheap to
        retry — no-op if the file already has the right mode.
        """
        import os
        base = "/var/lib/docker/volumes/vpn-bot_3xui-data/_data"
        for name in ("access.log", "error.log"):
            log_path = f"{base}/{name}"
            try:
                st = os.stat(log_path)
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(f"xray_log perms: stat {name} failed: {e}")
                continue
            want = 0o644
            have = st.st_mode & 0o777
            if have == want:
                continue
            try:
                os.chmod(log_path, want)
                logger.info(f"xray_log perms: chmod {oct(have)}->{oct(want)} on {name}")
            except Exception as e:
                logger.warning(f"xray_log perms: chmod {name} failed: {e}")

    def _cleanup_tg_media_sync(self):
        """Hourly: drop /tmp/tg_media/* older than 1 hour.

        AIHandler already unlinks each photo in its per-request finally
        block; this is a safety net for the case where the bot crashed
        before reaching the cleanup or where the host accumulated files
        from previous runs / kimi-side experiments.
        """
        import os
        import time as _t
        media_dir = '/tmp/tg_media'
        if not os.path.isdir(media_dir):
            return
        cutoff = _t.time() - 3600
        removed = 0
        for name in os.listdir(media_dir):
            path = os.path.join(media_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.unlink(path)
                    removed += 1
            except OSError as e:
                logger.warning(f"tg_media cleanup: failed on {path}: {e}")
        if removed:
            logger.info(f"tg_media cleanup: removed {removed} stale file(s)")

    def _alert_tick_sync(self):
        """One alert-manager pass. Kept tiny so the scheduler thread
        spends ~no time here; AlertManager.run_once() handles dedupe,
        cooldown, and message delivery.
        """
        if self._alert_manager is None:
            return
        try:
            self._alert_manager.run_once()
        except Exception as e:
            logger.exception(f"alert_tick failed: {e}")

    # ----- Reminder defaults (overridable via app_settings) -----
    # Source of truth for both runtime behaviour and the dashboard
    # editor. Any key listed here can be edited from the dashboard
    # without a code change. Numeric settings are stored as text and
    # cast on read; texts are stored verbatim.
    REMINDER_DEFAULTS = {
        # Numeric (hours / days / count)
        'reminder_min_age_hours':       '24',
        'reminder_repeat_after_hours':  '72',
        'reminder_max_per_user':        '2',
        'reminder_interval_hours':      '6',
        # Auto-reject NEW/PLATFORM_SELECT users abandoned this many
        # days. 0 = disabled. Reversible via /reset.
        'reminder_delete_after_days':   '0',
        # Texts
        'reminder_text_new_ru': (
            "👋 Привет! Кажется, ты не завершил оформление демо-доступа.\n\n"
            "Нажми «🚀 Запросить демо» ниже, чтобы продолжить.\n"
            "Если что-то непонятно — напиши в поддержку, поможем."
        ),
        'reminder_text_new_en': (
            "👋 Hi! Looks like you didn't finish requesting your demo.\n\n"
            "Tap «🚀 Request demo» below to continue.\n"
            "If anything is unclear — message support, we'll help."
        ),
        'reminder_text_platform_ru': (
            "👋 Привет! Ты ещё не выбрал платформу для своего VPN ключа.\n\n"
            "Выбери ниже — ключ создаётся автоматически.\n"
            "Если что-то непонятно — напиши в поддержку."
        ),
        'reminder_text_platform_en': (
            "👋 Hi! You haven't picked a platform for your VPN key yet.\n\n"
            "Choose below — the key is generated automatically.\n"
            "If anything is unclear — message support."
        ),
    }

    NUMERIC_REMINDER_KEYS = {
        'reminder_min_age_hours',
        'reminder_repeat_after_hours',
        'reminder_max_per_user',
        'reminder_interval_hours',
        'reminder_delete_after_days',
    }

    def get_reminder_setting(self, key: str):
        """Read a reminder setting with default fallback.

        Numeric keys are returned as int; text keys as str.
        """
        default = self.REMINDER_DEFAULTS.get(key)
        raw = self.db.get_setting(key, default) if hasattr(self.db, 'get_setting') else default
        if key in self.NUMERIC_REMINDER_KEYS:
            try:
                return int(raw)
            except (TypeError, ValueError):
                try:
                    return int(default)
                except (TypeError, ValueError):
                    return 0
        return raw if raw is not None else default

    def _remind_stuck_sync(self):
        """Nudge users who got stuck mid-funnel.

        Two cohorts:
          • NEW with @username — they opened the bot, saw the
            welcome with the demo button, never tapped.
          • PLATFORM_SELECT with @username — admin approved them
            (or auto-approve fast-tracked them) but they never
            picked a platform.

        Gates (all values now operator-tunable via app_settings):
          • account age > reminder_min_age_hours
          • no reminder of this kind in last reminder_repeat_after_hours
          • total reminders of this kind < reminder_max_per_user
          • has @username (no-username users never get a reminder —
            they're disproportionately throwaways)
        """
        min_age = self.get_reminder_setting('reminder_min_age_hours')
        try:
            import sqlite3
            with self.db._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT chat_id, username, lang, status FROM users "
                    "WHERE status IN ('new','platform_select') "
                    "AND username IS NOT NULL AND username != '' "
                    "AND datetime(created_at) < datetime('now', '-' || ? || ' hours')",
                    (min_age,),
                ).fetchall()
        except Exception as e:
            logger.exception(f"remind_stuck: db read failed: {e}")
            return

        max_per = self.get_reminder_setting('reminder_max_per_user')
        repeat_h = self.get_reminder_setting('reminder_repeat_after_hours')
        sent = 0
        for r in rows:
            if self._send_one_reminder(
                r['chat_id'], r['lang'] or 'ru', r['status'],
                max_per=max_per, repeat_h=repeat_h, force=False,
            ):
                sent += 1
        if sent:
            logger.info(f"remind_stuck: sent {sent} reminders")

    def _send_one_reminder(
        self, chat_id: str, lang: str, status: str,
        *, max_per: int, repeat_h: int, force: bool,
    ) -> bool:
        """Send a single reminder, respecting gates unless force=True.

        ``status`` is the user's status string ('new' or
        'platform_select') and determines which template+keyboard to
        send. ``force=True`` is used for manual-send from the dashboard
        — admin explicitly asked, skip the cooldown but still record
        the send in notification_log.
        """
        kind = 'reminder_new' if status == 'new' else 'reminder_platform_select'
        if not force:
            try:
                with self.db._connect() as conn:
                    row = conn.execute(
                        "SELECT COUNT(*) c, MAX(sent_at) last_at "
                        "FROM notification_log WHERE chat_id=? AND notification_type=?",
                        (chat_id, kind),
                    ).fetchone()
                    count = row[0] if row else 0
                    last_at = row[1] if row else None
            except Exception as e:
                logger.warning(f"remind: log check failed for {chat_id}: {e}")
                return False
            if count >= max_per:
                return False
            if last_at:
                try:
                    with self.db._connect() as conn:
                        fresh = conn.execute(
                            "SELECT 1 FROM notification_log WHERE chat_id=? "
                            "AND notification_type=? AND "
                            "datetime(sent_at) > datetime('now', '-' || ? || ' hours') LIMIT 1",
                            (chat_id, kind, repeat_h),
                        ).fetchone()
                except Exception:
                    fresh = None
                if fresh:
                    return False
        ok = (
            self._send_reminder_new(chat_id, lang)
            if status == 'new'
            else self._send_reminder_platform(chat_id, lang)
        )
        if ok:
            try:
                self.db.mark_notified(chat_id, kind)
            except Exception:
                pass
        return ok

    def _reminder_text(self, key_prefix: str, lang: str) -> str:
        """Read a reminder text from app_settings with default fallback."""
        lang = 'en' if lang == 'en' else 'ru'
        return self.get_reminder_setting(f"{key_prefix}_{lang}")

    def _send_reminder_new(self, chat_id: str, lang: str) -> bool:
        text = self._reminder_text('reminder_text_new', lang)
        btn_demo = {'ru': '🚀 Запросить демо', 'en': '🚀 Request demo'}
        btn_support = {'ru': '💬 Поддержка', 'en': '💬 Support'}
        kb = {'inline_keyboard': [
            [{'text': btn_demo.get(lang, btn_demo['ru']), 'callback_data': 'request_demo'}],
            [{'text': btn_support.get(lang, btn_support['ru']), 'callback_data': 'support'}],
        ]}
        try:
            self.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            return True
        except Exception as e:
            logger.warning(f"reminder_new send failed for {chat_id}: {e}")
            return False

    def _send_reminder_platform(self, chat_id: str, lang: str) -> bool:
        text = self._reminder_text('reminder_text_platform', lang)
        btn_support = {'ru': '💬 Поддержка', 'en': '💬 Support'}
        kb = {'inline_keyboard': [
            [{'text': '📱 Android', 'callback_data': 'platform:android'},
             {'text': '🍎 iOS',     'callback_data': 'platform:ios'}],
            [{'text': '💻 Windows', 'callback_data': 'platform:windows'},
             {'text': '🐧 Linux',   'callback_data': 'platform:linux'}],
            [{'text': btn_support.get(lang, btn_support['ru']), 'callback_data': 'support'}],
        ]}
        try:
            self.bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
            return True
        except Exception as e:
            logger.warning(f"reminder_platform send failed for {chat_id}: {e}")
            return False

    def _cleanup_abandoned_sync(self):
        """Auto-reject users stuck at NEW/PLATFORM_SELECT for too long.

        Gated by ``reminder_delete_after_days`` (0 = disabled). Sets
        status='rejected' and bumps reject_count — reversible by an
        admin via /reset. Audit-logged so we can see who was swept.
        Only touches users with @username — bots/throwaways don't
        need the cycle either way and the audit clutter isn't worth it.
        """
        days = self.get_reminder_setting('reminder_delete_after_days')
        if not days or days <= 0:
            return
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT chat_id, username FROM users "
                    "WHERE status IN ('new','platform_select') "
                    "AND username IS NOT NULL AND username != '' "
                    "AND datetime(created_at) < datetime('now', '-' || ? || ' days')",
                    (days,),
                ).fetchall()
                if not rows:
                    return
                for chat_id, username in rows:
                    conn.execute(
                        "UPDATE users SET previous_state = status, "
                        "status = 'rejected', reject_count = COALESCE(reject_count, 0) + 1 "
                        "WHERE chat_id = ?",
                        (chat_id,),
                    )
                conn.commit()
        except Exception as e:
            logger.exception(f"cleanup_abandoned: failed: {e}")
            return
        for chat_id, username in rows:
            try:
                self.db.log_admin_action(
                    'auto', 'cleanup_abandoned', str(chat_id),
                    f"stuck > {days}d, @{username or '?'}",
                )
            except Exception:
                pass
        logger.info(f"cleanup_abandoned: rejected {len(rows)} abandoned users")

    def send_reminder_to_chat(self, chat_id: str, force: bool = True) -> bool:
        """Public entry-point for manual send from the dashboard.

        Looks up the user, dispatches the right reminder template
        based on their current status. Returns True if a reminder was
        actually sent. ``force=True`` skips the cooldown gates.
        """
        try:
            user = self.db.get_user(chat_id)
        except Exception:
            user = None
        if not user:
            return False
        status = user.status
        if status not in ('new', 'platform_select'):
            return False
        lang = user.lang or 'ru'
        return self._send_one_reminder(
            str(chat_id), lang, status,
            max_per=self.get_reminder_setting('reminder_max_per_user'),
            repeat_h=self.get_reminder_setting('reminder_repeat_after_hours'),
            force=force,
        )

    def send_reminders_to_cohort(self, cohort: str, force: bool = True) -> int:
        """Manual cohort send. ``cohort`` is 'new' or 'platform_select'.

        Returns the count of reminders actually sent.
        """
        if cohort not in ('new', 'platform_select'):
            return 0
        try:
            import sqlite3
            with self.db._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT chat_id, lang FROM users "
                    "WHERE status = ? AND username IS NOT NULL AND username != ''",
                    (cohort,),
                ).fetchall()
        except Exception as e:
            logger.exception(f"send_reminders_to_cohort failed: {e}")
            return 0
        max_per = self.get_reminder_setting('reminder_max_per_user')
        repeat_h = self.get_reminder_setting('reminder_repeat_after_hours')
        sent = 0
        for r in rows:
            if self._send_one_reminder(
                r['chat_id'], r['lang'] or 'ru', cohort,
                max_per=max_per, repeat_h=repeat_h, force=force,
            ):
                sent += 1
        return sent

    def start_scheduler(self):
        """Start background scheduler for periodic checks."""
        if not SCHEDULER_AVAILABLE:
            logger.warning("APScheduler not available")
            return

        # Wire up the alert manager (lazy import to keep the
        # NotificationService module's top free of optional deps).
        try:
            from bot.services.alert_manager import AlertManager, build_default_checks
            mgr = AlertManager(self.bot, self.config, db=self.db)
            for c in build_default_checks(self.config, self.bot):
                mgr.register(c)
            self._alert_manager = mgr
            # Expose so callback handler (AlertAck) can call .ack()
            if hasattr(self.bot, 'services'):
                self.bot.services['alert_manager'] = mgr
            logger.info(f"AlertManager initialised with {len(mgr._checks)} checks")
        except Exception as e:
            logger.warning(f"AlertManager init failed: {e}")
            self._alert_manager = None

        self.scheduler = BackgroundScheduler()
        self.scheduler.add_job(
            self._check_expiring_subscriptions_sync,
            IntervalTrigger(hours=1),
            id='check_expiring',
            replace_existing=True,
        )
        # Daily ticket-messages cleanup at 04:00 server time.
        self.scheduler.add_job(
            self._cleanup_old_tickets_sync,
            IntervalTrigger(hours=24),
            id='ticket_cleanup',
            replace_existing=True,
        )
        # Hourly: revert users stuck in support_topic with NULL topic_id
        # back to their previous (or fallback) state. Pattern shows up
        # when the user taps "Поддержка" but never writes anything.
        self.scheduler.add_job(
            self._repair_stuck_support_users_sync,
            IntervalTrigger(hours=1),
            id='support_state_repair',
            replace_existing=True,
        )
        # Hourly: prune stale /tmp/tg_media photos that escaped the
        # per-request unlink in AIHandler.
        self.scheduler.add_job(
            self._cleanup_tg_media_sync,
            IntervalTrigger(hours=1),
            id='tg_media_cleanup',
            replace_existing=True,
        )
        # Hourly: re-chmod Xray's access.log to 644 so the bot can
        # parse it after Xray reloads recreated it with mode 600.
        self.scheduler.add_job(
            self._keep_xray_log_readable_sync,
            IntervalTrigger(hours=1),
            id='xray_log_perms',
            replace_existing=True,
        )
        # Monthly on the 1st at 00:00 UTC: reset demo traffic counters
        # and extend expiry so demo users keep their 5 GB allowance.
        self.scheduler.add_job(
            self._reset_demo_quota_sync,
            CronTrigger(day=1, hour=0, minute=0),
            id='demo_quota_reset',
            replace_existing=True,
        )
        # Every 60s: health checks → Telegram alerts. AlertManager
        # rate-limits and de-dupes internally so this is cheap.
        if self._alert_manager is not None:
            self.scheduler.add_job(
                self._alert_tick_sync,
                IntervalTrigger(seconds=60),
                id='alert_tick',
                replace_existing=True,
            )
        # Weekly: refresh the GeoIP mmdb.
        self.scheduler.add_job(
            self._refresh_geoip_db_sync,
            IntervalTrigger(days=7),
            id='geoip_refresh',
            replace_existing=True,
        )
        # Every 30 min: snapshot x-ui traffic counters per email into
        # traffic_history. Dashboard derives per-period deltas on read.
        self.scheduler.add_job(
            self._snapshot_traffic_sync,
            IntervalTrigger(minutes=30),
            id='traffic_snapshot',
            replace_existing=True,
        )
        # Daily: prune traffic_history rows older than 90 days.
        self.scheduler.add_job(
            self._cleanup_traffic_history_sync,
            IntervalTrigger(hours=24),
            id='traffic_history_cleanup',
            replace_existing=True,
        )
        # Nudge users stuck at NEW / PLATFORM_SELECT with a @username.
        # Interval is operator-tunable via app_settings (default 6h);
        # change takes effect on next bot restart.
        try:
            interval_h = max(1, int(self.get_reminder_setting('reminder_interval_hours')))
        except Exception:
            interval_h = 6
        self.scheduler.add_job(
            self._remind_stuck_sync,
            IntervalTrigger(hours=interval_h),
            id='remind_stuck',
            replace_existing=True,
        )
        # Daily: auto-reject NEW/PLATFORM_SELECT users abandoned for
        # too long. Disabled by default (reminder_delete_after_days=0).
        self.scheduler.add_job(
            self._cleanup_abandoned_sync,
            IntervalTrigger(hours=24),
            id='cleanup_abandoned',
            replace_existing=True,
        )
        # Every 5 min: roll up access.log into per-(country, asn)
        # dpi_metrics buckets. Phase A — Phase B will add error.log
        # + nstat signals on top.
        self.scheduler.add_job(
            self._dpi_collect_sync,
            IntervalTrigger(minutes=5),
            id='dpi_collect',
            replace_existing=True,
        )
        # Daily: drop dpi_metrics rows older than DPI_METRICS_RETENTION_DAYS.
        self.scheduler.add_job(
            self._cleanup_dpi_metrics_sync,
            IntervalTrigger(hours=24),
            id='dpi_metrics_cleanup',
            replace_existing=True,
        )
        # Daily DPI summary at 09:00 server time (UTC). Posts a numeric
        # summary in TOPIC_AI and, if OPENCODE_URL is set, hands off
        # to the OpenCode agent for the dpi-analysis follow-up.
        self.scheduler.add_job(
            self._dpi_daily_summary_sync,
            CronTrigger(hour=9, minute=0),
            id='dpi_daily_summary',
            replace_existing=True,
        )
        # Daily: drop alert_history rows > 30d and dpi_reports > 365d.
        self.scheduler.add_job(
            self._cleanup_alert_history_sync,
            IntervalTrigger(hours=24),
            id='alert_history_cleanup',
            replace_existing=True,
        )
        self.scheduler.add_job(
            self._cleanup_dpi_reports_sync,
            IntervalTrigger(hours=24),
            id='dpi_reports_cleanup',
            replace_existing=True,
        )
        # Daily: prune hy2_auth_log (7d retention — adoption widget
        # uses dpi_metrics for anything wider).
        self.scheduler.add_job(
            self._cleanup_hy2_auth_log_sync,
            IntervalTrigger(hours=24),
            id='hy2_auth_log_cleanup',
            replace_existing=True,
        )
        # Every 15 minutes: health check all outbounds against popular domains.
        self.scheduler.add_job(
            self._health_check_sync,
            IntervalTrigger(minutes=15),
            id='health_check',
            replace_existing=True,
        )
        self.scheduler.start()

    def _snapshot_traffic_sync(self):
        """Snapshot current x-ui traffic counters into traffic_history.

        Runs every 30 minutes. Pulls the full client_traffics table
        from the x-ui SQLite, writes one row per email into
        traffic_history. The dashboard derives per-period consumption
        on read by diffing successive rows.

        We do not skip "no change" rows — they're cheap and let the
        Chart.js sparkline render zero-traffic gaps explicitly instead
        of interpolating across multi-hour idle windows.
        """
        try:
            from datetime import datetime
            xui = (
                self.bot.services.get('xui')
                if hasattr(self.bot, 'services') else None
            )
            if not xui or not getattr(xui, 'db', None):
                return
            traffic = xui.db.get_all_client_traffic() or {}
            if not traffic:
                return
            ts = datetime.utcnow().isoformat()
            rows = [
                (
                    ts, email,
                    int(t.get('upload', 0)),
                    int(t.get('download', 0)),
                )
                for email, t in traffic.items()
            ]
            with self.db._connect() as conn:
                conn.executemany(
                    'INSERT INTO traffic_history '
                    '(recorded_at, email, upload_bytes, download_bytes) '
                    'VALUES (?, ?, ?, ?)',
                    rows,
                )
            logger.debug(f"traffic snapshot: {len(rows)} rows at {ts}")
        except Exception as e:
            logger.exception(f"traffic snapshot failed: {e}")

    def _cleanup_traffic_history_sync(self):
        """Daily: drop traffic_history rows older than 90 days.

        At 30-min cadence ~4300 rows/email/year accumulates; 90 days =
        ~4300 rows per active user — trivial size, but unbounded growth
        is unbounded growth.
        """
        try:
            from datetime import datetime, timedelta
            cutoff = (datetime.utcnow() - timedelta(days=90)).isoformat()
            with self.db._connect() as conn:
                cur = conn.execute(
                    'DELETE FROM traffic_history WHERE recorded_at < ?',
                    (cutoff,),
                )
                if cur.rowcount:
                    logger.info(
                        f"traffic_history cleanup: dropped {cur.rowcount} rows older than 90d"
                    )
        except Exception as e:
            logger.exception(f"traffic_history cleanup failed: {e}")

    # Window (seconds) of access.log / error.log to roll up per tick.
    # 5 minutes is a good compromise between catching brief DPI spikes
    # and giving each (country, asn) bucket enough samples to be
    # statistically meaningful.
    DPI_WINDOW_SEC = 300
    # Retention for dpi_metrics rows. 30 days is enough to build a
    # baseline and review historical incidents from the dashboard.
    DPI_METRICS_RETENTION_DAYS = 30
    # Reserved country code for the "host-wide" snapshot row that
    # carries kernel-level TCP abort deltas (no per-country attribution
    # possible from /proc/net/netstat alone).
    DPI_GLOBAL_BUCKET_CC = '*GLOBAL*'

    # Inbound tag we attribute REALITY handshake failures to. error.log
    # REALITY rejects don't carry the inbound tag (xray writes the
    # global engine context), so we hand-pin this to the current Reality
    # inbound id from x-ui. Update if the operator moves Reality off
    # 8443. Misalignment with the live inbound only mismatches the
    # heatmap bucket — it doesn't lose data.
    REALITY_FAIL_INBOUND_TAG = 'inbound-8443'
    # Inbound tag for the Hysteria2 sidecar. It's not in Xray's access
    # log (separate binary), so we synthesise an inbound tag and roll
    # up hy2_auth_log into the same dpi_metrics table so the dashboard
    # heatmap has one source of truth.
    HY2_INBOUND_TAG = 'hy2-8400'

    def _dpi_collect_sync(self):
        """Every 5 min: roll up access.log + error.log + hy2_auth_log
        into dpi_metrics.

        Rows written:

        1. **Per-(country, ASN, inbound_tag) rows from access.log** —
           conn_count, short_session_count, avg_session_sec. With
           Phase B the inbound_tag is parsed out of each log line so
           ws/xhttp/ss-2022/Reality each get their own buckets.
        2. **Per-(country, ASN) rows from error.log** — REALITY
           handshake-failure counts. Pinned to REALITY_FAIL_INBOUND_TAG
           because the error log doesn't carry the inbound tag itself.
        3. **Per-(country, ASN) rows from hy2_auth_log** — Hy2 auth
           callbacks since the previous tick, attributed to
           HY2_INBOUND_TAG. Hysteria2 runs as a separate binary so
           it doesn't show up in Xray's access.log; without this
           step every Hy2 user is invisible to the heatmap.
        4. **One global row** (``country=*GLOBAL*``) carrying the
           kernel TCP abort delta — host-wide DPI/health pulse.
        """
        try:
            import json
            from datetime import datetime
            from bot.services.xray_log import (
                summarize_dpi, summarize_handshake_failures,
                read_tcp_abort_counters,
            )
            from bot.services.geoip import lookup as geo_lookup, lookup_asn

            # access.log → per (cc, asn, inbound_tag)
            traffic_buckets = summarize_dpi(
                window_seconds=self.DPI_WINDOW_SEC,
                geoip_lookup=geo_lookup,
                asn_lookup=lookup_asn,
            )
            # error.log → per (cc, asn) Reality handshake failures
            handshake_buckets = summarize_handshake_failures(
                window_seconds=self.DPI_WINDOW_SEC,
                geoip_lookup=geo_lookup,
                asn_lookup=lookup_asn,
            )

            ts = datetime.utcnow().isoformat()
            rows = []

            # 1. Per-inbound access.log rows.
            for key, tb in traffic_buckets.items():
                cc, asn, inbound_tag = key
                avg_conns = tb.get('avg_conns_per_ip') or 0
                avg_session = (
                    self.DPI_WINDOW_SEC / avg_conns if avg_conns > 0 else None
                )
                rows.append((
                    ts, cc, asn, tb.get('as_org') or '',
                    inbound_tag or '',
                    int(tb.get('conn_count') or 0),
                    avg_session,
                    int(tb.get('short_session_count') or 0),
                    0,  # rst_count: needs eBPF; left 0
                    0,  # handshake_fail_count: filled by the next loop
                    None, None,
                ))

            # 2. error.log Reality rejects — own bucket since the log
            # doesn't carry the inbound tag.
            for key, hb in handshake_buckets.items():
                cc, asn = key
                probe_ips = hb.get('top_ips') or []
                reason_buckets = hb.get('reason_buckets') or {}
                rows.append((
                    ts, cc, asn, hb.get('as_org') or '',
                    self.REALITY_FAIL_INBOUND_TAG,
                    0,           # conn_count: failures don't accept
                    None,        # avg_session n/a
                    0,           # short_session_count n/a
                    0,           # rst_count n/a
                    int(hb.get('fail_count') or 0),
                    json.dumps(probe_ips) if probe_ips else None,
                    json.dumps(reason_buckets) if reason_buckets else None,
                ))

            # 3. Hy2 — pull successful auths from hy2_auth_log since
            # the previous snapshot, attribute to HY2_INBOUND_TAG.
            rows.extend(self._dpi_hy2_rows(ts))

            # Host-wide TCP abort delta row. We track the previous
            # absolute counters on self so we can subtract.
            cur_abort = read_tcp_abort_counters()
            if cur_abort:
                prev = getattr(self, '_prev_tcp_abort', None)
                if prev is not None:
                    delta = max(0, sum(
                        cur_abort.get(k, 0) - prev.get(k, 0)
                        for k in cur_abort
                    ))
                else:
                    delta = 0  # first tick — no baseline yet
                self._prev_tcp_abort = cur_abort
                if delta or prev is None:
                    rows.append((
                        ts, self.DPI_GLOBAL_BUCKET_CC, None, '', 'host',
                        0,           # conn_count not applicable
                        None,        # avg_session_sec n/a
                        0,           # short_session_count n/a
                        int(delta),  # rst_count = TCP abort delta this window
                        0,           # handshake_fail_count: per-bucket rows already have it
                        None, json.dumps(cur_abort),
                    ))

            if not rows:
                return
            with self.db._connect() as conn:
                conn.executemany(
                    "INSERT INTO dpi_metrics ("
                    "snapshot_at, country, asn, as_org, inbound_tag, "
                    "conn_count, avg_session_sec, short_session_count, "
                    "rst_count, handshake_fail_count, "
                    "probe_ips_json, reason_buckets_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
            logger.debug(f"dpi_collect: wrote {len(rows)} rows at {ts}")
        except Exception as e:
            logger.exception(f"dpi_collect failed: {e}")

    def _dpi_hy2_rows(self, ts: str) -> list:
        """Synthesise dpi_metrics rows for Hy2 from hy2_auth_log.

        We re-count the last ``DPI_WINDOW_SEC`` seconds of allow/deny
        events per (country, asn) so the same 5-min window the
        access.log buckets cover is represented for Hy2 too. ``conn_count``
        is allow events; deny events flow into ``handshake_fail_count``
        — the closest analogue we have, since a Hy2 auth deny is the
        protocol-level equivalent of a Reality reject.
        """
        import json
        rows: list = []
        try:
            with self.db._connect() as conn:
                cur = conn.execute(
                    "SELECT country, asn, as_org, decision, COUNT(*) "
                    "FROM hy2_auth_log "
                    "WHERE ts > datetime('now', '-' || ? || ' seconds') "
                    "GROUP BY country, asn, as_org, decision",
                    (self.DPI_WINDOW_SEC,),
                )
                # Combine allow/deny per (cc, asn, as_org) so each
                # bucket emits one row carrying both counters.
                buckets: dict = {}
                for cc, asn, as_org, decision, count in cur.fetchall():
                    key = (cc, asn, as_org or '')
                    b = buckets.setdefault(key, {'allow': 0, 'deny': 0})
                    if decision == 'allow':
                        b['allow'] += int(count)
                    else:
                        b['deny'] += int(count)
                for (cc, asn, as_org), b in buckets.items():
                    if not (b['allow'] or b['deny']):
                        continue
                    rows.append((
                        ts, cc, asn, as_org, self.HY2_INBOUND_TAG,
                        b['allow'],   # conn_count
                        None,         # avg_session_sec — not measured for Hy2
                        0,            # short_session_count n/a
                        0,            # rst_count n/a
                        b['deny'],    # deny ≈ handshake-equivalent failure
                        None, None,
                    ))
        except Exception as e:
            logger.warning(f"dpi_collect: hy2 rollup failed: {e}")
        return rows

    def _health_check_sync(self):
        """Every 15 min: probe popular domains through each outbound.

        Writes to outbound_health table. Runs async checks from the
        sync scheduler thread via asyncio.run().
        """
        try:
            from bot.services.health_checker import HealthChecker
            checker = HealthChecker(self.db, self.config)
            asyncio.run(checker.check_all_outbounds())
        except Exception as e:
            logger.warning(f"health_check: failed: {e}")

    def _dpi_daily_summary_sync(self):
        """Daily 09:00: roll up 24h of DPI metrics, persist into the
        long-lived dpi_reports table, post a numeric summary to TOPIC_AI
        and (if kimi-bridge is configured) attach a Kimi analysis.

        Why two tables: dpi_metrics keeps a 30-day rolling raw window
        for the heatmap; dpi_reports keeps one row per day for 365 days
        so the operator can scroll the trend without re-running the
        rollup query every time.
        """
        try:
            from datetime import datetime, timedelta
            cutoff_24 = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            cutoff_48 = (datetime.utcnow() - timedelta(hours=48)).isoformat()
            with self.db._connect() as conn:
                today = conn.execute(
                    "SELECT country, asn, MAX(as_org), "
                    "SUM(conn_count), SUM(short_session_count), "
                    "SUM(handshake_fail_count), SUM(rst_count) "
                    "FROM dpi_metrics WHERE snapshot_at >= ? "
                    "GROUP BY country, asn",
                    (cutoff_24,),
                ).fetchall()
                yest = conn.execute(
                    "SELECT country, asn, "
                    "SUM(conn_count), SUM(short_session_count), "
                    "SUM(handshake_fail_count), SUM(rst_count) "
                    "FROM dpi_metrics WHERE snapshot_at >= ? AND snapshot_at < ? "
                    "GROUP BY country, asn",
                    (cutoff_48, cutoff_24),
                ).fetchall()
        except Exception as e:
            logger.exception(f"dpi_daily_summary: db read failed: {e}")
            return

        # Quick numeric summary regardless of kimi availability.
        total_conn = sum((r[3] or 0) for r in today)
        total_short = sum((r[4] or 0) for r in today)
        total_hsfail = sum((r[5] or 0) for r in today)
        worst = sorted(
            (r for r in today if r[0] != '*GLOBAL*' and (r[3] or 0) >= 50),
            key=lambda r: -((r[4] or 0) / max(r[3] or 1, 1)),
        )[:3]
        worst_str = ", ".join(
            f"{r[0]}/{r[1]}={int(100 * (r[4] or 0) / max(r[3] or 1, 1))}% short"
            for r in worst
        ) or "никаких аномалий"
        summary = (
            f"📊 <b>DPI сводка за 24ч</b>\n\n"
            f"<code>conn: {total_conn} · short: {total_short} · "
            f"hsfail: {total_hsfail}</code>\n\n"
            f"Топ-3 проблемных: {worst_str}"
        )

        # Persist into dpi_reports (1 row per day, retained 365 days).
        import json as _json
        period_end = datetime.utcnow().isoformat()
        period_start = cutoff_24
        snapshot = {
            'today': [
                {
                    'country': r[0], 'asn': r[1], 'as_org': r[2],
                    'conn_count': r[3] or 0, 'short_session_count': r[4] or 0,
                    'handshake_fail_count': r[5] or 0, 'rst_count': r[6] or 0,
                }
                for r in today
            ],
            'yesterday': [
                {
                    'country': r[0], 'asn': r[1],
                    'conn_count': r[2] or 0, 'short_session_count': r[3] or 0,
                    'handshake_fail_count': r[4] or 0, 'rst_count': r[5] or 0,
                }
                for r in yest
            ],
            'totals': {
                'conn': total_conn,
                'short': total_short,
                'hsfail': total_hsfail,
            },
        }
        report_db_id = None
        try:
            with self.db._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO dpi_reports (kind, period_start, period_end, snapshot_json) "
                    "VALUES (?, ?, ?, ?)",
                    ('daily', period_start, period_end, _json.dumps(snapshot)),
                )
                conn.commit()
                report_db_id = cur.lastrowid
        except Exception as e:
            logger.warning(f"dpi_daily_summary: persist report failed: {e}")
        topic = getattr(self.config, 'TOPIC_AI', 0) or 0
        group = getattr(self.config, 'FORUM_GROUP_ID', None)
        if topic and group:
            try:
                self.bot.send_message(
                    chat_id=group, text=summary, parse_mode='HTML',
                    message_thread_id=topic,
                )
            except Exception as e:
                logger.warning(f"dpi_daily_summary: send summary failed: {e}")

        # Hand off to the OpenCode agent for deeper analysis if configured.
        # Daily analysis is stored ONLY in the database (dashboard-viewable).
        # No chat spam — incident alerts already fire real-time via AlertManager.
        from bot.services.agent_factory import build_agent_client, get_agent_url
        url = get_agent_url(self.config)
        if not url:
            return
        try:
            import json, time as _time
            client = build_agent_client(
                self.config,
                getattr(self.config, 'DB_PATH', '') or '/var/lib/vpn-bot/bot.db',
                default_timeout=240,
            )
            # Pack the rollups into compact JSON so the agent has structured
            # data to read without needing to query the DB itself.
            payload_today = [
                {
                    'country': r[0], 'asn': r[1], 'as_org': r[2],
                    'conn': r[3] or 0, 'short': r[4] or 0,
                    'hsfail': r[5] or 0, 'rst': r[6] or 0,
                }
                for r in today
            ]
            payload_yest = [
                {
                    'country': r[0], 'asn': r[1],
                    'conn': r[2] or 0, 'short': r[3] or 0,
                    'hsfail': r[4] or 0, 'rst': r[5] or 0,
                }
                for r in yest
            ]
            prompt = (
                "DAILY DPI SUMMARY — produce the report per dpi-analysis skill's "
                "'When called from the daily summary cron' format. HTML output, "
                "~1500 chars max.\n\n"
                f"TODAY (last 24h): {json.dumps(payload_today, ensure_ascii=False)[:6000]}\n\n"
                f"YESTERDAY (24-48h ago): {json.dumps(payload_yest, ensure_ascii=False)[:6000]}\n"
            )
            session_key = f"dpi-daily:{int(_time.time())}"
            reply, _ms = client.ask(session_key, prompt, timeout=240)
            if reply:
                # Attach to the dpi_reports row we just inserted.
                # Dashboard is the ONLY place for daily analysis — Telegram
                # is reserved for incident alerts.
                if report_db_id is not None:
                    try:
                        with self.db._connect() as conn:
                            conn.execute(
                                "UPDATE dpi_reports SET kimi_analysis = ? WHERE id = ?",
                                (reply[:8000], report_db_id),
                            )
                            conn.commit()
                    except Exception as e:
                        logger.warning(f"dpi_daily_summary: agent DB attach failed: {e}")
                # NOTE: No Telegram post for daily analysis — admin can view
                # it in the dashboard's "DPI Reports" tab. Real-time incident
                # alerts fire via AlertManager (see _kick_dpi_agent).
        except Exception as e:
            logger.warning(f"dpi_daily_summary: agent call failed: {e}")

    ALERT_HISTORY_RETENTION_DAYS = 30
    DPI_REPORTS_RETENTION_DAYS = 365

    HY2_AUTH_LOG_RETENTION_DAYS = 7

    def _cleanup_hy2_auth_log_sync(self):
        """Daily: drop hy2_auth_log rows older than 7 days.

        Each row is small, but every accepted connection makes one —
        the table grows fast on a busy day. The adoption widget only
        looks at the last 30 days max and uses dpi_metrics for any
        wider window, so 7 days is enough.
        """
        try:
            from datetime import datetime, timedelta
            cutoff = (
                datetime.utcnow() - timedelta(days=self.HY2_AUTH_LOG_RETENTION_DAYS)
            ).isoformat()
            with self.db._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM hy2_auth_log WHERE ts < ?",
                    (cutoff,),
                )
                if cur.rowcount:
                    logger.info(
                        f"hy2_auth_log cleanup: dropped {cur.rowcount} rows"
                    )
        except Exception as e:
            logger.exception(f"hy2_auth_log cleanup failed: {e}")

    def _cleanup_alert_history_sync(self):
        """Daily: drop alert_history rows older than 30 days."""
        try:
            from datetime import datetime, timedelta
            cutoff = (
                datetime.utcnow() - timedelta(days=self.ALERT_HISTORY_RETENTION_DAYS)
            ).isoformat()
            with self.db._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM alert_history WHERE fired_at < ?",
                    (cutoff,),
                )
                if cur.rowcount:
                    logger.info(
                        f"alert_history cleanup: dropped {cur.rowcount} "
                        f"rows older than {self.ALERT_HISTORY_RETENTION_DAYS}d"
                    )
        except Exception as e:
            logger.exception(f"alert_history cleanup failed: {e}")

    def _cleanup_dpi_reports_sync(self):
        """Daily: drop dpi_reports rows older than 365 days."""
        try:
            from datetime import datetime, timedelta
            cutoff = (
                datetime.utcnow() - timedelta(days=self.DPI_REPORTS_RETENTION_DAYS)
            ).isoformat()
            with self.db._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM dpi_reports WHERE created_at < ?",
                    (cutoff,),
                )
                if cur.rowcount:
                    logger.info(
                        f"dpi_reports cleanup: dropped {cur.rowcount} "
                        f"rows older than {self.DPI_REPORTS_RETENTION_DAYS}d"
                    )
        except Exception as e:
            logger.exception(f"dpi_reports cleanup failed: {e}")

    def _cleanup_dpi_metrics_sync(self):
        """Daily: prune dpi_metrics rows older than retention."""
        try:
            from datetime import datetime, timedelta
            cutoff = (
                datetime.utcnow() - timedelta(days=self.DPI_METRICS_RETENTION_DAYS)
            ).isoformat()
            with self.db._connect() as conn:
                cur = conn.execute(
                    "DELETE FROM dpi_metrics WHERE snapshot_at < ?",
                    (cutoff,),
                )
                if cur.rowcount:
                    logger.info(
                        f"dpi_metrics cleanup: dropped {cur.rowcount} rows "
                        f"older than {self.DPI_METRICS_RETENTION_DAYS}d"
                    )
        except Exception as e:
            logger.exception(f"dpi_metrics cleanup failed: {e}")

    def _refresh_geoip_db_sync(self):
        """Pull the latest GeoIP mmdb. Soft-fails (logs) on network errors."""
        try:
            from bot.services.geoip import refresh_db_sync
            ok = refresh_db_sync()
            logger.info(f"GeoIP refresh: {'OK' if ok else 'FAILED (kept stale)'}")
        except Exception as e:
            logger.exception(f"GeoIP refresh crashed: {e}")
        logger.info("Notification scheduler started")
    
    def stop_scheduler(self):
        """Stop background scheduler."""
        if self.scheduler:
            self.scheduler.shutdown()
    
    async def check_expiring_subscriptions(self):
        """Check and notify about expiring subscriptions.
        
        Uses asyncio.to_thread() for DB calls to avoid blocking event loop (H-02 fix).
        """
        import asyncio
        logger.info("Checking expiring subscriptions...")
        
        # Run sync DB calls in thread pool to avoid blocking (H-02 fix)
        expiring_24h = await asyncio.to_thread(
            self.db.get_expiring_subscriptions, hours=24
        )
        for sub in expiring_24h:
            was_notified = await asyncio.to_thread(
                self.db.was_notified, sub['chat_id'], 'expiry_24h', hours=24
            )
            if not was_notified:
                await self.notify_expiry_24h(sub['chat_id'])
                await asyncio.to_thread(
                    self.db.mark_notified, sub['chat_id'], 'expiry_24h'
                )
    
    async def notify_expiry_24h(self, chat_id: str):
        """Send 24-hour expiry warning."""
        text = (
            "⏳ <b>Напоминание</b>\n\n"
            "Ваша VPN подписка истекает через 24 часа.\n"
            "Продлите подписку, чтобы избежать отключения."
        )
        try:
            self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Failed to send expiry warning: {e}")
