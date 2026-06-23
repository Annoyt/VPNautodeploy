"""Admin callback handlers."""

import logging
from typing import TYPE_CHECKING

from bot.config import UserState
from bot.core.state_machine import StateMachine
from bot.handlers.callbacks.base import BaseCallbackHandler
from bot.services.admin_notifications import build_rejected_user_keyboard
from bot.services.notifications import NotificationService
from bot.services.user_lifecycle import revoke_user_key
from bot.utils.exceptions import PermissionDeniedError, UserNotFoundError

if TYPE_CHECKING:
    from bot.core.bot import Bot
    from bot.core.database import Database
    from bot.config import Settings

logger = logging.getLogger(__name__)


class ApproveUserHandler(BaseCallbackHandler):
    """Handle user approval callback."""
    
    CALLBACK_PATTERN = 'approve:'
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process user approval."""
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            self._send_permission_denied(user_id)
            return
        
        data = kwargs.get('data', '')
        parts = data.split(':')
        if len(parts) < 2 or not parts[1]:
            self.bot.send_message(chat_id=chat_id, text="❌ Invalid callback data.")
            return
        target_id = parts[1]
        
        callback = update.get('callback_query', {})
        message = callback.get('message', {})
        
        self._approve_user(target_id, user_id, message)
    
    def _send_permission_denied(self, admin_id: str) -> None:
        """Send permission denied message."""
        self.bot.send_message(chat_id=admin_id, text="❌ No permission.")
    
    def _approve_user(self, target_id: str, admin_id: str, message: dict) -> None:
        """Approve user and send notifications."""
        try:
            user = self.validator.validate_user_exists(target_id)
        except UserNotFoundError:
            self._send_user_not_found(admin_id, message, target_id)
            return

        # Idempotency: only act if user is still in the PENDING_DEMO state.
        # Without this gate a stale Approve button (in a DM that wasn't edited
        # back when FORUM_ENABLED=False) lets the admin re-fire the whole flow
        # repeatedly, sending duplicate notifications to the user.
        if user.status != UserState.PENDING_DEMO.value:
            self.forum_service.update_request_message(
                message=message, user=user, admin_id=admin_id,
                action="APPROVED (already)", emoji="✅"
            )
            logger.info(f"Approve ignored: user {target_id} is in state {user.status!r}, not pending_demo")
            return

        # Update state
        sm = StateMachine(self.db)
        sm.transition(target_id, UserState.PLATFORM_SELECT)

        # Notify user
        notifier = NotificationService(self.bot, self.db, self.config)
        notifier.notify_approved(target_id, user.lang)

        # Strip buttons + stamp the request message (works in PM and forum mode)
        self._update_forum(message, user, admin_id)

        # Admin notification
        self._notify_admin(message, target_id, user.username, admin_id)

        logger.info(f"User {target_id} approved by admin {admin_id}")
    
    def _send_user_not_found(self, admin_id: str, message: dict, target_id: str) -> None:
        """Send user not found notification to admin."""
        source_chat_id = str(message.get('chat', {}).get('id', admin_id))
        message_thread_id = message.get('message_thread_id')
        
        self.bot.send_message(
            chat_id=source_chat_id,
            message_thread_id=message_thread_id,
            text=f"❌ User {target_id} not found."
        )
    
    def _update_forum(self, message: dict, user, admin_id: str) -> None:
        """Update forum message if in forum mode."""
        self.forum_service.update_request_message(
            message=message,
            user=user,
            admin_id=admin_id,
            action="APPROVED",
            emoji="✅"
        )
        
        # Migration notification
        if self.config.FORUM_ENABLED:
            notifier = NotificationService(self.bot, self.db, self.config)
            notifier.notify_approved_admin_migration(user, admin_id)
    
    def _notify_admin(self, message: dict, target_id: str, username: str, admin_id: str) -> None:
        """Send approval confirmation to admin."""
        source_chat_id = str(message.get('chat', {}).get('id', admin_id))
        message_thread_id = message.get('message_thread_id')
        
        self.forum_service.send_admin_notification(
            chat_id=source_chat_id,
            message_thread_id=message_thread_id,
            target_thread_id=self.config.TOPIC_USERS,
            text=f"✅ User {target_id} ({username or 'no_username'}) approved for DEMO."
        )


class RejectUserHandler(BaseCallbackHandler):
    """Handle user rejection callback."""
    
    CALLBACK_PATTERN = 'reject:'
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process user rejection."""
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            self._send_permission_denied(user_id)
            return
        
        data = kwargs.get('data', '')
        parts = data.split(':')
        if len(parts) < 2 or not parts[1]:
            self.bot.send_message(chat_id=chat_id, text="❌ Invalid callback data.")
            return
        target_id = parts[1]
        
        callback = update.get('callback_query', {})
        message = callback.get('message', {})
        
        self._reject_user(target_id, user_id, message)
    
    def _send_permission_denied(self, admin_id: str) -> None:
        """Send permission denied message."""
        self.bot.send_message(chat_id=admin_id, text="❌ No permission.")
    
    def _reject_user(self, target_id: str, admin_id: str, message: dict) -> None:
        """Reject user and send notifications."""
        try:
            user = self.validator.validate_user_exists(target_id)
        except UserNotFoundError:
            self._send_user_not_found(admin_id, message, target_id)
            return

        # Idempotency — see ApproveUserHandler._approve_user for rationale.
        if user.status != UserState.PENDING_DEMO.value:
            self.forum_service.update_request_message(
                message=message, user=user, admin_id=admin_id,
                action="REJECTED (already)", emoji="❌"
            )
            logger.info(f"Reject ignored: user {target_id} is in state {user.status!r}, not pending_demo")
            return

        # Revoke active key + clear uuid/email — see services/user_lifecycle.py.
        revoke_user_key(user, self.bot.services.get('xui'), self.db)

        # Update state
        sm = StateMachine(self.db)
        sm.transition(target_id, UserState.REJECTED)

        # Increment reject counter
        user.reject_count = (user.reject_count or 0) + 1
        self.db.save_user(user)
        
        # Default rejection reason based on missing username
        reason = "Отсутствует username в Telegram" if not user.username else "Заявка отклонена"
        
        # Notify user
        notifier = NotificationService(self.bot, self.db, self.config)
        notifier.notify_rejected(target_id, reason, user.lang)
        
        # Forum updates
        self._update_forum(message, user, admin_id)
        
        # Admin notification
        self._notify_admin(message, target_id, user.username, admin_id)
        
        logger.info(f"User {target_id} rejected by admin {admin_id} (count: {user.reject_count})")
    
    def _send_user_not_found(self, admin_id: str, message: dict, target_id: str) -> None:
        """Send user not found notification to admin."""
        source_chat_id = str(message.get('chat', {}).get('id', admin_id))
        message_thread_id = message.get('message_thread_id')
        
        self.bot.send_message(
            chat_id=source_chat_id,
            message_thread_id=message_thread_id,
            text=f"❌ User {target_id} not found."
        )
    
    def _update_forum(self, message: dict, user, admin_id: str) -> None:
        """Update forum message if in forum mode."""
        self.forum_service.update_request_message(
            message=message,
            user=user,
            admin_id=admin_id,
            action="REJECTED",
            emoji="❌"
        )
        
        # Send to rejected topic
        if self.config.FORUM_ENABLED:
            keyboard = build_rejected_user_keyboard(user)
            self.bot.send_message_to_topic(
                chat_id=self.config.FORUM_GROUP_ID,
                message_thread_id=self.config.TOPIC_REJECTED,
                text=f"❌ <b>Request Rejected</b>\n\nUser: {user.chat_id} (@{user.username})\nAdmin: {admin_id}",
                parse_mode='HTML',
                reply_markup=keyboard
            )
    
    def _notify_admin(self, message: dict, target_id: str, username: str, admin_id: str) -> None:
        """Send rejection confirmation to admin."""
        source_chat_id = str(message.get('chat', {}).get('id', admin_id))
        message_thread_id = message.get('message_thread_id')
        
        self.forum_service.send_admin_notification(
            chat_id=source_chat_id,
            message_thread_id=message_thread_id,
            target_thread_id=self.config.TOPIC_REJECTED,
            text=f"❌ User {target_id} ({username or 'no_username'}) rejected."
        )


class RevokeUserHandler(BaseCallbackHandler):
    """Handle user access revocation callback."""
    
    CALLBACK_PATTERN = 'revoke:'
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process access revocation."""
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            self._send_permission_denied(user_id)
            return
        
        data = kwargs.get('data', '')
        parts = data.split(':')
        if len(parts) < 2 or not parts[1]:
            self.bot.send_message(chat_id=chat_id, text="❌ Invalid callback data.")
            return
        target_id = parts[1]
        
        callback = update.get('callback_query', {})
        message = callback.get('message', {})
        
        self._revoke_user(target_id, user_id, message)
    
    def _send_permission_denied(self, admin_id: str) -> None:
        """Send permission denied message."""
        self.bot.send_message(chat_id=admin_id, text="❌ No permission.")
    
    def _revoke_user(self, target_id: str, admin_id: str, message: dict) -> None:
        """Revoke user access — removes x-ui client and clears uuid/email."""
        try:
            user = self.validator.validate_user_exists(target_id)
        except UserNotFoundError:
            self._send_user_not_found(admin_id, message, target_id)
            return

        # Remove from x-ui AND clear uuid/email in the bot DB. The old
        # _remove_from_xui only did the x-ui side, which left a ghost row
        # — see services/user_lifecycle.py for the consolidated behavior.
        revoke_user_key(user, self.bot.services.get('xui'), self.db)

        # Update state
        sm = StateMachine(self.db)
        sm.transition(target_id, UserState.BANNED)

        # Notify user
        notifier = NotificationService(self.bot, self.db, self.config)
        notifier.notify_rejected(target_id, "Access revoked by administrator", user.lang)

        # Notify admin
        self._notify_admin(message, target_id, user.username, admin_id)

        logger.info(f"User {target_id} access revoked by admin {admin_id}")
    
    def _send_user_not_found(self, admin_id: str, message: dict, target_id: str) -> None:
        """Send user not found notification to admin."""
        source_chat_id = str(message.get('chat', {}).get('id', admin_id))
        message_thread_id = message.get('message_thread_id')
        
        self.bot.send_message(
            chat_id=source_chat_id,
            message_thread_id=message_thread_id,
            text=f"❌ User {target_id} not found."
        )
    
    def _remove_from_xui(self, user) -> None:
        """Remove user from X-UI."""
        try:
            xui = self.bot.services.get('xui')
            if xui:
                xui.remove_client(user.email)
        except Exception as e:
            logger.warning(f"Failed to delete client from X-UI: {e}")
    
    def _notify_admin(self, message: dict, target_id: str, username: str, admin_id: str) -> None:
        """Send revocation confirmation to admin."""
        source_chat_id = str(message.get('chat', {}).get('id', admin_id))
        message_thread_id = message.get('message_thread_id')
        
        self.bot.send_message(
            chat_id=source_chat_id,
            message_thread_id=message_thread_id,
            text=f"🚫 User {target_id} ({username or 'no_username'}) access revoked."
        )


class AdminMessageHandler(BaseCallbackHandler):
    """Handle admin message button callback."""
    
    CALLBACK_PATTERN = 'message:'
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process admin message request."""
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            return
        
        data = kwargs.get('data', '')
        parts = data.split(':')
        if len(parts) < 2 or not parts[1]:
            self.bot.send_message(chat_id=user_id, text="❌ Invalid callback data.")
            return
        target_id = parts[1]
        
        self.bot.send_message(
            chat_id=user_id,
            text=f"✍️ Пришлите сообщение для отправки пользователю {target_id}."
        )


class AdminProfileHandler(BaseCallbackHandler):
    """Handle admin profile button callback."""
    
    CALLBACK_PATTERN = 'profile:'
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Show user profile to admin."""
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            return
        
        data = kwargs.get('data', '')
        parts = data.split(':')
        if len(parts) < 2 or not parts[1]:
            self.bot.send_message(chat_id=user_id, text="❌ Invalid callback data.")
            return
        target_id = parts[1]
        
        self._show_profile(target_id, user_id)
    
    def _show_profile(self, target_id: str, admin_id: str) -> None:
        """Show user profile."""
        try:
            user = self.validator.validate_user_exists(target_id)
        except UserNotFoundError:
            self.bot.send_message(
                chat_id=admin_id,
                text=f"❌ Пользователь {target_id} не найден."
            )
            return
        
        username = f"@{user.username}" if user.username else "Нет username"
        text = (
            f"👤 <b>Профиль пользователя</b>\n\n"
            f"Имя: {username}\n"
            f"ID: <code>{user.chat_id}</code>\n"
            f"Статус: {user.status}\n"
            f"Платформа: {user.platform or 'Не выбрана'}\n"
            f"Язык: {user.lang}\n"
            f"Создан: {user.created_at[:10]}"
        )
        self.bot.send_message(chat_id=admin_id, text=text, parse_mode='HTML')


class ResetApprovalHandler(BaseCallbackHandler):
    """Handle reset approval callback."""
    
    CALLBACK_PATTERN = 'reset_approval:'
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process approval reset."""
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            self._send_permission_denied(user_id)
            return
        
        data = kwargs.get('data', '')
        parts = data.split(':')
        if len(parts) < 2 or not parts[1]:
            self.bot.send_message(chat_id=chat_id, text="❌ Invalid callback data.")
            return
        target_id = parts[1]
        
        callback = update.get('callback_query', {})
        message = callback.get('message', {})
        
        self._reset_approval(target_id, user_id, message)
    
    def _send_permission_denied(self, admin_id: str) -> None:
        """Send permission denied message."""
        self.bot.send_message(chat_id=admin_id, text="❌ No permission.")
    
    def _reset_approval(self, target_id: str, admin_id: str, message: dict) -> None:
        """Reset user approval."""
        try:
            user = self.validator.validate_user_exists(target_id)
        except UserNotFoundError:
            self._send_user_not_found(admin_id, message, target_id)
            return
        
        # Try transition
        sm = StateMachine(self.db)
        success = sm.transition(target_id, UserState.NEW)
        
        if not success:
            self._send_transition_error(admin_id, message, target_id, user.status)
            return
        
        # Remove from X-UI
        self._remove_from_xui(user)
        
        # Clear user data
        self._clear_user_data(user)
        
        # Notify user
        self._notify_user_reset(target_id, user.lang)
        
        # Notify admin
        self._notify_admin(message, target_id, user.username, admin_id)
        
        logger.info(f"Approval reset for user {target_id} by admin {admin_id}")
    
    def _send_user_not_found(self, admin_id: str, message: dict, target_id: str) -> None:
        """Send user not found notification."""
        source_chat_id = str(message.get('chat', {}).get('id', admin_id))
        message_thread_id = message.get('message_thread_id')
        
        self.bot.send_message(
            chat_id=source_chat_id,
            message_thread_id=message_thread_id,
            text=f"❌ User {target_id} not found."
        )
    
    def _send_transition_error(self, admin_id: str, message: dict, target_id: str, status: str) -> None:
        """Send state transition error."""
        source_chat_id = str(message.get('chat', {}).get('id', admin_id))
        message_thread_id = message.get('message_thread_id')
        
        self.bot.send_message(
            chat_id=source_chat_id,
            message_thread_id=message_thread_id,
            text=f"❌ Cannot reset user {target_id}: invalid state transition from {status}"
        )
    
    def _remove_from_xui(self, user) -> None:
        """Remove user from X-UI."""
        if user.email:
            try:
                xui = self.bot.services.get('xui')
                if xui:
                    xui.remove_client_sync(user.email)
            except Exception as e:
                logger.warning(f"Failed to remove client from X-UI: {e}")
    
    def _clear_user_data(self, user) -> None:
        """Clear user VPN data — full reset including the reject quota."""
        user.uuid = None
        user.email = None
        user.platform = None
        # /reset / Reset-approval is meant to be a clean slate; without this
        # the reject_count carries over and the user can hit MAX_REJECT_RETRIES
        # immediately on the very first new request.
        user.reject_count = 0
        self.db.save_user(user)
    
    def _notify_user_reset(self, target_id: str, lang: str) -> None:
        """Notify user about reset."""
        if lang == 'ru':
            text = "⚠️ Ваше одобрение было сброшено. Вы можете подать новую заявку через /start"
        else:
            text = "⚠️ Your approval has been reset. You can submit a new request via /start"
        
        self.bot.send_message(chat_id=target_id, text=text)
    
    def _notify_admin(self, message: dict, target_id: str, username: str, admin_id: str) -> None:
        """Notify admin about reset."""
        source_chat_id = str(message.get('chat', {}).get('id', admin_id))
        message_thread_id = message.get('message_thread_id')
        
        username_display = username or 'no_username'
        self.bot.send_message(
            chat_id=source_chat_id,
            message_thread_id=message_thread_id,
            text=f"🔄 Approval reset for user {target_id} (@{username_display})."
        )


class CloseTicketHandler(BaseCallbackHandler):
    """Handle close ticket callback."""
    
    CALLBACK_PATTERN = 'close_ticket:'
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process ticket closure."""
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            self.bot.send_message(chat_id=user_id, text="❌ No permission.")
            return
        
        data = kwargs.get('data', '')
        parts = data.split(':')
        if len(parts) < 2 or not parts[1]:
            self.bot.send_message(chat_id=user_id, text="❌ Invalid callback data.")
            return
        try:
            topic_id = int(parts[1])
        except ValueError:
            self.bot.send_message(chat_id=user_id, text="❌ Invalid ticket ID.")
            return
        
        self._close_ticket(update, topic_id)
    
    def _close_ticket(self, update: dict, topic_id: int) -> None:
        """Close support ticket."""
        from bot.handlers.forum import ForumHandler
        forum = ForumHandler(self.bot, self.db, self.config)
        forum.handle_close_ticket(update, topic_id)


class AlertAckHandler(BaseCallbackHandler):
    """Inline "✅ Понятно" button on system alert messages.

    Tells the AlertManager to silence the given alert key for 6 hours
    so admin isn't pinged every 30 min while they're already working
    on the issue.
    """

    CALLBACK_PATTERN = 'alert_ack:'

    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            self.bot.send_message(chat_id=user_id, text="❌ No permission.")
            return

        data = kwargs.get('data', '')
        key = data[len(self.CALLBACK_PATTERN):].strip()
        if not key:
            return

        mgr = (
            self.bot.services.get('alert_manager')
            if hasattr(self.bot, 'services') else None
        )
        if mgr and mgr.ack(key):
            # Edit the original message to mark it acked instead of
            # leaving the button live (a second press is a no-op but
            # confusing).
            cb_msg = update.get('callback_query', {}).get('message', {})
            msg_id = cb_msg.get('message_id')
            msg_chat = (cb_msg.get('chat') or {}).get('id')
            if msg_id and msg_chat is not None:
                try:
                    old = cb_msg.get('text', '')
                    self.bot.client.edit_message_text(
                        chat_id=str(msg_chat), message_id=msg_id,
                        text=f"{old}\n\n✅ <i>Подтверждено · тише на 6 ч.</i>",
                        parse_mode='HTML',
                    )
                except Exception as e:
                    logger.warning(f"alert ack: edit failed: {e}")


class BanFromTicketHandler(BaseCallbackHandler):
    """Inline "🚫 Бан" button on the Support Request post.

    Bans the user (state → BANNED), revokes the x-ui client, notifies
    them, and closes the support topic in one click. The callback_data
    carries both ``chat_id`` and ``topic_id`` so we don't need an extra
    DB lookup to know which topic to close.
    """

    CALLBACK_PATTERN = 'ban_from_ticket:'

    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            self.bot.send_message(chat_id=user_id, text="❌ No permission.")
            return

        data = kwargs.get('data', '')
        parts = data.split(':')
        if len(parts) < 3 or not parts[1] or not parts[2]:
            self.bot.send_message(chat_id=user_id, text="❌ Invalid callback data.")
            return

        target_chat_id = parts[1]
        try:
            topic_id = int(parts[2])
        except ValueError:
            self.bot.send_message(chat_id=user_id, text="❌ Invalid topic id.")
            return

        forum_group = self.config.FORUM_GROUP_ID

        try:
            user = self.validator.validate_user_exists(target_chat_id)
        except UserNotFoundError:
            self.bot.send_message(
                chat_id=forum_group,
                message_thread_id=topic_id,
                text=f"❌ User {target_chat_id} not found.",
            )
            return

        # Revoke key + transition to BANNED.
        try:
            revoke_user_key(user, self.bot.services.get('xui') if hasattr(self.bot, 'services') else None, self.db)
            StateMachine(self.db).transition(target_chat_id, UserState.BANNED)
        except Exception as e:
            logger.exception(f"ban_from_ticket: revoke/transition failed: {e}")
            self.bot.send_message(
                chat_id=forum_group,
                message_thread_id=topic_id,
                text=f"❌ Не удалось забанить: <code>{e}</code>",
                parse_mode='HTML',
            )
            return

        # Notify the user.
        try:
            NotificationService(self.bot, self.db, self.config).notify_banned(
                target_chat_id, user.lang or 'ru',
            )
        except Exception as e:
            logger.warning(f"ban_from_ticket: notify_banned failed: {e}")

        # Audit.
        try:
            self.db.log_admin_action(str(user_id), 'webapp_ban_from_ticket', target_chat_id)
        except Exception as e:
            logger.warning(f"ban_from_ticket: audit log failed: {e}")

        uname = f"@{user.username}" if user.username else f"user_{user.chat_id}"
        self.bot.send_message(
            chat_id=forum_group,
            message_thread_id=topic_id,
            text=f"🚫 {uname} забанен. Закрываю тикет.",
            parse_mode='HTML',
        )

        # Close the ticket (reuses the full close flow: compile log →
        # SOLVED archive, notify user, rename topic, close).
        from bot.handlers.forum import ForumHandler
        ForumHandler(self.bot, self.db, self.config).handle_close_ticket(update, topic_id)
