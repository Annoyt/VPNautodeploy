"""Base admin handler with common functionality."""

import logging
from typing import Optional

from bot.config import UserState
from bot.models import User
from bot.handlers.base import BaseHandler

logger = logging.getLogger(__name__)


# Shared between admin/base.py (group path) and commands.py (PM path)
# so both surfaces show the same list. When adding a new command in
# ADMIN_COMMANDS, add a bullet here in the right group.
ADMIN_HELP_TEXT = (
    "🚀 <b>Панель управления NekoVPN</b>\n\n"

    "📊 <b>Быстрая инфо</b>\n"
    "• <code>/status</code> — health всех сервисов + CPU/RAM/Disk\n"
    "• <code>/stats</code> — статистика по юзерам / трафику\n"
    "• <code>/onlines</code> — кто онлайн (lastOnline панели, hy2 тоже) + трафик\n"
    "• <code>/whoami</code> — твой id + проверка прав\n"
    "• <code>/topics</code> — текущие forum topic IDs\n\n"

    "👥 <b>Пользователи</b>\n"
    "• <code>/users</code> — активные (demo/paid)\n"
    "• <code>/users_all</code> — все в базе\n"
    "• <code>/pending</code> — ожидают одобрения\n"
    "• <code>/user @x</code> — карточка юзера\n"
    "• <code>/find &lt;текст&gt;</code> — поиск по username / id / email / uuid\n\n"

    "⚡ <b>Модерация</b>\n"
    "• демо: авто-аппрув по сигналам (username/фото/bio/premium);\n"
    "  зависшие заявки — ежечасный дайджест с кнопками\n"
    "• заявки с почты: карточка с кнопками Выдать (демо письмом)/Отклонить\n"
    "• <code>/approve @x</code> — одобрить\n"
    "• <code>/reject @x</code> — отклонить\n"
    "• <code>/ban @x</code> — забанить + отозвать ключ\n"
    "• <code>/unban @x</code> — разбанить\n"
    "• <code>/reset @x</code> — полный сброс state + key\n\n"

    "🎫 <b>Поддержка</b>\n"
    "• <code>/close</code> — (внутри support-топика) закрыть тикет\n"
    "  эквивалент кнопки 🔒\n"
    "• <code>/repair_stuck</code> — починить юзеров, застрявших в support_topic\n\n"

    "💰 <b>Подписки / квота</b>\n"
    "• <code>/quota @x N</code> — установить квоту в N ГБ (replace)\n"
    "• <code>/grant_100gb @x</code> — +100 ГБ к текущей квоте (add)\n"
    "• <code>/set_limit @x N</code> — лимит IP-устройств\n"
    "• <code>/expire @x YYYY-MM-DD</code> — установить дату истечения\n"
    "• <code>/approve_payment @x</code> — подтвердить оплату\n"
    "• <code>/addmail email [ГБ] [дней]</code> — создать юзера по email "
    "и выслать ключ + инструкцию письмом (по умолч. 100 ГБ / 30 дней)\n\n"

    "💳 <b>Оплата (для юзеров — /buy в PM)</b>\n"
    "• юзер вводит <code>/buy</code> или жмёт «💳 Купить подписку» в меню\n"
    "• планы: 1 мес / 3 мес / 6 мес / 12 мес (от 100 ⭐)\n"
    "• цены в env: <code>PLAN_1M_STARS</code>, <code>PLAN_3M_STARS</code>, <code>PLAN_6M_STARS</code>, <code>PLAN_12M_STARS</code>\n\n"

    "📢 <b>Рассылка</b>\n"
    "• <code>/broadcast &lt;текст&gt;</code> — превью\n"
    "• <code>/broadcast_confirm</code> — отправить\n"
    "• <code>/broadcast_cancel</code> — отменить\n\n"

    "🤖 <b>AI / Hermes</b> (в любом топике или PM, admin-only)\n"
    "• <code>/ai &lt;запрос&gt;</code> — спросить агента (default mode)\n"
    "• <code>/ai_plan &lt;запрос&gt;</code> — глубже думает (--plan)\n"
    "• <code>/ai_fast &lt;запрос&gt;</code> — быстрее, без plan\n"
    "• <code>/ai_yolo &lt;запрос&gt;</code> — авто-подтверждение shell\n"
    "• <code>/ai_skill &lt;name&gt; &lt;запрос&gt;</code> — форсить скил\n"
    "  (vpn-ops | server-admin | code-review | incident-response | billing-ops | dpi-analysis)\n"
    "• <code>/ai_model [номер|id]</code> — выбрать бесплатную модель (кнопки)\n"
    "• <code>/ai_status</code> — статус агента + модель + сессия\n"
    "• <code>/ai_reset</code> — сбросить сессию\n"
    "• 📷 фото в топик AI — агент прочитает картинку\n\n"

    "🛠 <b>Обслуживание</b>\n"
    "• <code>/backup</code> — снепшот БД в /opt/backups\n"
    "• <code>/recent [N]</code> — последние N действий из аудита\n"
    "• <code>/admin</code> — открыть веб-дашборд\n\n"

    "ℹ️ Кнопки 🔒 / 📞 / 🚫 в support-топиках работают так же, как <code>/close</code> и <code>/ban</code>."
)


class AdminHandlerBase(BaseHandler):
    """Base class for admin handlers with common functionality."""
    
    # Admin command routing. Methods live across the mixins
    # (users.py, broadcast.py, stats.py, ops.py).
    ADMIN_COMMANDS = {
        # Users (mixin: users.py)
        '/pending': 'show_pending',
        '/approve': 'approve_user',
        '/reject': 'reject_user',
        '/user': 'show_user',
        '/ban': 'ban_user',
        '/unban': 'unban_user',
        '/reset': 'reset_user',
        '/users': 'show_active_users',
        '/users_active': 'show_active_users',
        '/users_all': 'show_all_users',
        '/set_limit': 'set_limit',
        '/approve_payment': 'approve_payment',
        '/grant_100gb': 'grant_100gb',
        # Broadcast (mixin: broadcast.py)
        '/broadcast': 'broadcast_preview',
        '/broadcast_confirm': 'broadcast_confirm',
        '/broadcast_cancel': 'broadcast_cancel',
        # Stats (mixin: stats.py)
        '/stats': 'show_overall_stats',
        '/backup': 'backup_db',
        # Ops — added 2026-06 (mixin: ops.py)
        '/status': 'show_status',
        '/whoami': 'show_whoami',
        '/onlines': 'show_onlines',
        '/find': 'find_user',
        '/recent': 'show_recent_actions',
        '/repair_stuck': 'repair_stuck_support',
        '/topics': 'show_topics',
        '/quota': 'set_quota',
        '/expire': 'set_expire',
        # Create an email-only user and mail them the key
        '/addmail': 'add_mail_user',
        '/mailkey': 'add_mail_user',
        # Help
        '/help': 'show_admin_help',
        '/admin_help': 'show_admin_help',
    }
    
    # Class-level default for backward compatibility (tests, etc.)
    _pending_broadcasts: dict = {}
    
    def __init__(self, bot, db, config):
        super().__init__(bot, db, config)
        # Instance-level storage for pending broadcasts (admin_id -> message_text)
        self._pending_broadcasts: dict = {}

    def _resolve_target(self, arg: str) -> Optional[User]:
        """Resolve chat_id or @username to User object."""
        if arg.startswith('@'):
            return self.db.get_user_by_username(arg)
        return self.db.get_user(arg)

    def can_handle(self, update: dict) -> bool:
        """Check if message is an admin command from admin user."""
        message = update.get('message')
        if not message or 'text' not in message:
            return False
        
        text = message.get('text') or ''
        if not text.startswith('/'):
            return False
        
        user_id = self._get_user_id(update)
        chat_id = self._get_chat_id(update)
        
        if not user_id or not self._is_admin(user_id, chat_id):
            return False
            
        # Isolate admin commands: If forum is enabled, they must be used in the forum group
        if self.config.FORUM_ENABLED and str(chat_id) != str(self.config.FORUM_GROUP_ID):
            return False
        
        command = text.split()[0].split('@')[0].lower()
        return command in self.ADMIN_COMMANDS

    def handle(self, update: dict) -> None:
        """Route admin command to handler."""
        message = update.get('message', {})
        text = message.get('text', '')
        chat_id = self._get_chat_id(update)
        
        if not chat_id:
            return
        
        parts = text.split()
        command = parts[0].split('@')[0].lower()
        args = parts[1:]
        
        handler_name = self.ADMIN_COMMANDS.get(command)
        if handler_name:
            self._current_update = update
            getattr(self, handler_name)(chat_id, args)

    def show_admin_help(self, chat_id: str, args: list) -> None:
        """Display admin help menu — single source of truth for the
        admin command list. Kept in sync with ADMIN_COMMANDS above.
        """
        help_text = ADMIN_HELP_TEXT
        self.bot.send_message(
            chat_id=chat_id, text=help_text, parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    def _send(self, chat_id: str, **kwargs) -> None:
        """send_message with forum-topic routing baked in.

        Admin replies must land in the topic the command came from —
        27 call sites used to reply without message_thread_id and every
        one of them dumped its answer into the forum's General topic.
        """
        kwargs.setdefault('message_thread_id', self._get_thread_id(chat_id))
        self.bot.send_message(chat_id=chat_id, **kwargs)

    def _get_thread_id(self, chat_id: str) -> Optional[int]:
        """Get forum thread ID for admin notifications.
        
        Replies in the same topic the command was sent from;
        falls back to TOPIC_REQUESTS only when the original topic
        cannot be determined (e.g. callback or legacy path).
        """
        if not self.config.FORUM_ENABLED:
            return None
        
        if str(chat_id) != str(self.config.FORUM_GROUP_ID):
            return None
        
        # If we have the original update, reply in the same topic.
        update = getattr(self, '_current_update', None)
        if update:
            thread_id = update.get('message', {}).get('message_thread_id')
            if thread_id is not None:
                return thread_id
            # message_thread_id is absent/missing -> General topic
            return None
        
        # Fallback for callbacks or other paths without a stored update.
        return self.config.TOPIC_REQUESTS
