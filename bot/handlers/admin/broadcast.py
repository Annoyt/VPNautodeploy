"""Broadcast handlers for admin operations.

Includes: broadcast_preview, broadcast_confirm, broadcast_cancel
"""

import logging
from typing import Optional

from bot.config import UserState
from bot.models import User
from .base import AdminHandlerBase

logger = logging.getLogger(__name__)


class AdminBroadcastMixin(AdminHandlerBase):
    """Broadcast admin handlers."""

    def broadcast_preview(self, chat_id: str, args: list) -> None:
        """Prepare broadcast message."""
        if not args:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Укажите текст сообщения: /broadcast текст"
            )
            return
        
        message_text = ' '.join(args)
        
        # Store pending broadcast
        self._pending_broadcasts[chat_id] = message_text
        
        # Preview with stats
        all_users = self.db.get_all_users() or []
        active_users = len([
            u for u in all_users
            if u.status in ('demo', 'paid')
        ])
        
        preview = (
            f"📢 <b>Предпросмотр рассылки</b>\n\n"
            f"{message_text}\n\n"
            f"👥 Получателей: {active_users}\n\n"
            f"Отправьте:\n"
            f"• <code>/broadcast_confirm</code> — отправить\n"
            f"• <code>/broadcast_cancel</code> — отменить"
        )
        
        self.bot.send_message(chat_id=chat_id, text=preview, parse_mode='HTML')

    def broadcast_confirm(self, chat_id: str, args: list) -> None:
        """Confirm and send broadcast."""
        message_text = self._pending_broadcasts.get(chat_id)
        
        if not message_text:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Нет подготовленного сообщения. Сначала /broadcast текст"
            )
            return
        
        # Get active users
        users = [
            u for u in self.db.get_all_users()
            if u.status in ('demo', 'paid')
        ]
        
        sent = 0
        failed = 0
        
        for user in users:
            try:
                self.bot.send_message(
                    chat_id=user.chat_id,
                    text=message_text,
                    parse_mode='HTML'
                )
                sent += 1
            except Exception as e:
                logger.warning(f"Failed to send broadcast to {user.chat_id}: {e}")
                failed += 1
        
        # Clear pending
        del self._pending_broadcasts[chat_id]
        
        self.bot.send_message(
            chat_id=chat_id,
            text=f"✅ Рассылка завершена.\n📤 Отправлено: {sent}\n❌ Ошибок: {failed}"
        )
        logger.info(f"Admin {chat_id} sent broadcast to {sent} users")

    def broadcast_cancel(self, chat_id: str, args: list) -> None:
        """Cancel pending broadcast."""
        if chat_id in self._pending_broadcasts:
            del self._pending_broadcasts[chat_id]
            self.bot.send_message(chat_id=chat_id, text="❌ Рассылка отменена.")
        else:
            self.bot.send_message(chat_id=chat_id, text="📭 Нет активной рассылки.")
