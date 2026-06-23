"""User management handlers for admin operations.

Includes: approve, reject, ban, unban, reset, show_user, set_limit, grant_100gb
"""

import logging
from typing import Optional

from bot.config import UserState
from bot.config.constants import BYTES_PER_GB
from bot.models import User
from bot.core.state_machine import StateMachine
from bot.services.notifications import NotificationService
from bot.services.user_lifecycle import revoke_user_key
from bot.services.vpn import VPNService
from .base import AdminHandlerBase

logger = logging.getLogger(__name__)


class AdminUsersMixin(AdminHandlerBase):
    """User management admin handlers."""

    def show_pending(self, chat_id: str, args: list) -> None:
        """Show users awaiting approval."""
        users = self.db.get_pending_users()
        
        if not users:
            self.bot.send_message(chat_id=chat_id, text="📭 Нет ожидающих заявок.")
            return
        
        text = f"📋 <b>Ожидают одобрения ({len(users)}):</b>\n\n"
        for user in users:
            username = f"@{user.username}" if user.username else f"user_{user.chat_id}"
            text += f"• {username} <code>{user.chat_id}</code>\n"
            if user.email:
                text += f"  📧 {user.email}\n"
            text += "\n"
        
        text += "<code>/approve @username</code> или <code>/reject @username</code>"
        self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')

    def approve_user(self, chat_id: str, args: list) -> None:
        """Approve a user."""
        if not args:
            self.bot.send_message(chat_id=chat_id, text="❌ Укажите пользователя: /approve @username")
            return
        
        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return
        
        sm = StateMachine(self.db)
        
        # Handle different initial states
        if target.status == UserState.PENDING_DEMO.value:
            sm.transition(target.chat_id, UserState.PLATFORM_SELECT)
        else:
            sm.transition(target.chat_id, UserState.DEMO)
        
        notifier = NotificationService(self.bot, self.db, self.config)
        lang = target.lang or 'ru'
        notifier.notify_approved(target.chat_id, lang)
        
        username = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(
            chat_id=chat_id, 
            text=f"✅ {username} одобрен и активирован."
        )
        logger.info(f"Admin {chat_id} approved user {target.chat_id}")

    def reject_user(self, chat_id: str, args: list) -> None:
        """Reject a user and revoke any active key."""
        if not args:
            self.bot.send_message(chat_id=chat_id, text="❌ Укажите пользователя: /reject @username")
            return

        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return

        # Revoke active VPN key + clear uuid/email so a rejected user can't
        # /mykey their old key back. See bot/services/user_lifecycle.py.
        revoke_user_key(target, self.bot.services.get('xui'), self.db)

        sm = StateMachine(self.db)
        sm.transition(target.chat_id, UserState.REJECTED)

        notifier = NotificationService(self.bot, self.db, self.config)
        notifier.notify_rejected(target.chat_id, target.lang)

        username = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(
            chat_id=chat_id,
            text=f"❌ {username} отклонён."
        )
        logger.info(f"Admin {chat_id} rejected user {target.chat_id}")

    def show_user(self, chat_id: str, args: list) -> None:
        """Show detailed user information."""
        if not args:
            self.bot.send_message(chat_id=chat_id, text="❌ Укажите пользователя: /user @username")
            return
        
        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return
        
        # Get traffic stats if available
        traffic = None
        if target.email:
            try:
                xui = self.bot.services.get('xui')
                if xui and hasattr(xui, 'db'):
                    traffic = xui.db.get_client_traffic(target.email)
            except Exception as e:
                logger.warning(f"Could not get traffic for {target.email}: {e}")
        
        username = f"@{target.username}" if target.username else "(no username)"
        
        text = (
            f"👤 <b>Пользователь {username}</b>\n"
            f"ID: <code>{target.chat_id}</code>\n"
            f"Статус: <b>{target.status}</b>\n"
            f"Email: {target.email or '(none)'}\n"
            f"Язык: {target.lang}\n"
            f"Платформа: {target.platform or '(unknown)'}\n"
            f"UUID: {target.uuid or '(none)'}\n\n"
        )
        
        if traffic:
            up_gb = traffic.get('upload', 0) / BYTES_PER_GB
            down_gb = traffic.get('download', 0) / BYTES_PER_GB
            text += (
                f"📊 <b>Трафик:</b>\n"
                f"↑ Отдано: {up_gb:.2f} GB\n"
                f"↓ Получено: {down_gb:.2f} GB\n"
            )
        
        self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')

    def ban_user(self, chat_id: str, args: list) -> None:
        """Ban a user and revoke any active key."""
        if not args:
            self.bot.send_message(chat_id=chat_id, text="❌ Укажите пользователя: /ban @username")
            return

        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return

        # Same rationale as /reject: clear the x-ui client and the local
        # uuid/email so the ban is enforceable end-to-end.
        revoke_user_key(target, self.bot.services.get('xui'), self.db)

        sm = StateMachine(self.db)
        sm.transition(target.chat_id, UserState.BANNED)

        username = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(chat_id=chat_id, text=f"🚫 {username} забанен.")
        logger.info(f"Admin {chat_id} banned user {target.chat_id}")

    def unban_user(self, chat_id: str, args: list) -> None:
        """Unban a user. Clears any stale uuid/email left over from
        a pre-revoke-helper ban so the user must go through approval
        again rather than re-using a ghost client config."""
        if not args:
            self.bot.send_message(chat_id=chat_id, text="❌ Укажите пользователя: /unban @username")
            return

        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return

        # Strip leftover uuid/email (legacy bans before user_lifecycle
        # helper may have skipped this), without touching x-ui — the
        # client there was already removed at ban time (or never existed).
        if target.uuid or target.email:
            target.uuid = None
            target.email = None
            self.db.save_user(target)

        sm = StateMachine(self.db)
        sm.transition(target.chat_id, UserState.NEW)

        username = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(chat_id=chat_id, text=f"✅ {username} разбанен.")
        logger.info(f"Admin {chat_id} unbanned user {target.chat_id}")

    def reset_user(self, chat_id: str, args: list) -> None:
        """Reset user to initial state."""
        if not args:
            self.bot.send_message(chat_id=chat_id, text="❌ Укажите пользователя: /reset @username")
            return
        
        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return
        
        # Remove from X-UI
        if target.email:
            try:
                xui = self.bot.services.get('xui')
                if xui:
                    xui.remove_client_sync(target.email)
            except Exception as e:
                logger.warning(f"Failed to remove client from X-UI: {e}")
        
        # Force state to NEW (bypass transition rules for admin reset)
        sm = StateMachine(self.db)
        sm.set_state(target.chat_id, UserState.NEW)
        
        # Clear user data
        self.db.reset_user_data(target.chat_id)
        
        username = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(
            chat_id=chat_id,
            text=f"🔄 {username} сброшен (статус: new)."
        )
        logger.info(f"Admin {chat_id} reset user {target.chat_id}")

    def set_limit(self, chat_id: str, args: list) -> None:
        """Set IP limit for user."""
        if len(args) < 2:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат: /set_limit @username N"
            )
            return
        
        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return
        
        try:
            limit = int(args[1])
        except ValueError:
            self.bot.send_message(chat_id=chat_id, text="❌ N должно быть числом.")
            return
        
        user = self.db.get_user(target.chat_id)
        if user:
            user.limit_ip = limit
            self.db.save_user(user)
            
            # Update in X-UI if possible
            if user.email:
                try:
                    xui = self.bot.services.get('xui')
                    if xui:
                        client = xui.get_client_sync(user.email)
                        if client:
                            client['limitIp'] = limit
                            xui.add_client_sync(client, client.get('inbound_id', 1))
                except Exception as e:
                    logger.warning(f"Could not update limit in X-UI: {e}")
        
        username = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Лимит IP для {username} установлен: {limit}"
        )

    def grant_100gb(self, chat_id: str, args: list) -> None:
        """Grant 100GB traffic to user."""
        if not args:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Укажите пользователя: /grant_100gb @username"
            )
            return
        
        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return
        
        # Update quota
        user = self.db.get_user(target.chat_id)
        if user:
            user.quota_gb += 100
            self.db.save_user(user)
        
        # Update in X-UI
        if target.email:
            try:
                xui = self.bot.services.get('xui')
                if xui:
                    client = xui.get_client_sync(target.email)
                    if client:
                        current_total = client.get('totalGB', 0)
                        client['totalGB'] = current_total + (100 * BYTES_PER_GB)
                        xui.add_client_sync(client, client.get('inbound_id', 1))
            except Exception as e:
                logger.warning(f"Could not update quota in X-UI: {e}")
        
        username = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        new_quota = user.quota_gb if user else target.quota_gb
        self.bot.send_message(
            chat_id=chat_id,
            text=f"✅ {username} получил +100 ГБ. Новая квота: {new_quota} ГБ"
        )

    def approve_payment(self, chat_id: str, args: list) -> None:
        """Approve payment and extend subscription."""
        if not args:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Укажите пользователя: /approve_payment @username"
            )
            return
        
        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return
        
        # Transition to paid
        sm = StateMachine(self.db)
        sm.transition(target.chat_id, UserState.PAID)
        
        # Create/update subscription
        from datetime import datetime, timedelta
        end_date = (datetime.now() + timedelta(days=30)).isoformat()
        self.db.create_subscription(
            target.chat_id,
            plan_type='monthly',
            end_date=end_date
        )
        
        notifier = NotificationService(self.bot, self.db, self.config)
        lang = target.lang or 'ru'
        notifier.notify_payment_approved(target.chat_id, lang)
        
        username = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Оплата {username} подтверждена. Подписка до {end_date[:10]}."
        )

    def show_active_users(self, chat_id: str, args: list) -> None:
        """Show active users."""
        self._show_users_list_internal(chat_id, filter_active=True)

    def show_all_users(self, chat_id: str, args: list) -> None:
        """Show all users."""
        self._show_users_list_internal(chat_id, filter_active=False)

    def _show_users_list_internal(self, chat_id: str, filter_active: bool = True):
        """Internal method to show users list."""
        users = self.db.get_all_users()
        
        if filter_active:
            users = [u for u in users if u.status in ('demo', 'paid')]
            title = f"Активные пользователи ({len(users)})"
        else:
            title = f"Все пользователи ({len(users)})"
        
        if not users:
            self.bot.send_message(chat_id=chat_id, text=f"📭 {title}: нет записей.")
            return
        
        text = f"👥 <b>{title}</b>\n\n"
        
        for user in users[:50]:  # Limit to 50 users
            username = f"@{user.username}" if user.username else f"ID:{user.chat_id[:8]}"
            status_emoji = {
                'demo': '🎁',
                'paid': '💎',
                'pending_demo': '⏳',
                'rejected': '🔄',
                'banned': '🚫',
                'new': '🆕'
            }.get(user.status, '❓')
            
            text += f"{status_emoji} {username} — {user.status}\n"
        
        if len(users) > 50:
            text += f"\n... и ещё {len(users) - 50} пользователей"
        
        self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
