"""Statistics and backup handlers for admin operations.

Includes: show_overall_stats, backup_db
"""

import logging
import os
from datetime import datetime
from typing import Optional

from .base import AdminHandlerBase

logger = logging.getLogger(__name__)


class AdminStatsMixin(AdminHandlerBase):
    """Statistics and backup admin handlers."""

    def show_overall_stats(self, chat_id: str, args: list) -> None:
        """Show overall system statistics."""
        stats = self.db.get_stats()
        
        text = "📊 <b>Общая статистика</b>\n\n"
        
        # User stats
        total = stats.get('total', 0)
        text += f"👥 Всего пользователей: <b>{total}</b>\n"
        
        by_status = stats.get('by_status', {})
        for status, count in sorted(by_status.items(), key=lambda x: -x[1]):
            emoji = {
                'demo': '🎁',
                'paid': '💎',
                'pending_demo': '⏳',
                'pending_payment': '💳',
                'banned': '🚫',
                'rejected': '❌',
                'new': '🆕'
            }.get(status, '❓')
            text += f"  {emoji} {status}: {count}\n"
        
        # Platform stats
        by_platform = stats.get('by_platform', {})
        if by_platform:
            text += "\n📱 <b>По платформам:</b>\n"
            for platform, count in sorted(by_platform.items(), key=lambda x: -x[1]):
                text += f"  {platform}: {count}\n"
        
        # X-UI stats if available
        try:
            xui = self.bot.services.get('xui')
            if xui:
                # API-aware: on entry there is no local x-ui.db.
                inbounds = xui.get_inbound_settings_sync()
                if inbounds:
                    clients = inbounds.get('clients', [])
                    text += f"\n🔌 X-UI клиентов: {len(clients)}\n"
        except Exception as e:
            logger.debug(f"Could not get X-UI stats: {e}")
        
        self._send(chat_id=chat_id, text=text, parse_mode='HTML')

    def backup_db(self, chat_id: str, args: list) -> None:
        """Create database backup."""
        import shutil
        
        backup_dir = os.path.join(os.path.dirname(self.db.db_path), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_path = os.path.join(backup_dir, f'bot_backup_{timestamp}.db')
        
        try:
            shutil.copy2(self.db.db_path, backup_path)
            
            # Get file size
            size_mb = os.path.getsize(backup_path) / (1024 * 1024)
            
            self.bot.send_message(
                chat_id=chat_id,
                text=f"✅ <b>Бэкап создан</b>\n\n📁 {backup_path}\n📦 Размер: {size_mb:.2f} MB"
            )
            logger.info(f"Database backup created: {backup_path}")
            
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            self.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Ошибка создания бэкапа: {e}"
            )
