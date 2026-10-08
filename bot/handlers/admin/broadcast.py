"""Broadcast handlers for admin operations.

Includes: broadcast_preview, broadcast_confirm, broadcast_cancel, nudge_sub
"""

import html
import logging
import threading
import time
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
        
        self._send(chat_id=chat_id, text=preview, parse_mode='HTML')

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
            self._send(chat_id=chat_id, text="❌ Рассылка отменена.")
        else:
            self._send(chat_id=chat_id, text="📭 Нет активной рассылки.")

    # ----- /nudge_sub (IMPROVEMENT_PLAN A1.2) -----

    NUDGE_STATUSES = ('demo', 'paid', 'support_topic')
    NUDGE_SEND_DELAY_S = 0.05   # same cadence as the dashboard broadcast
    NUDGE_SAMPLE = 10

    def _nudge_audience(self) -> list:
        """Active key holders whose ``last_asn`` is empty: they have not
        fetched /sub since geo landed, so per-ASN cascade tuning (and the
        "your network" line of /sos) cannot reach them. A refresh from
        their app fills it in."""
        return [
            u for u in (self.db.get_all_users() or [])
            if u.status in self.NUDGE_STATUSES and u.uuid
            and not (u.last_asn or '').strip()
        ]

    @staticmethod
    def _telegram_reachable(user) -> bool:
        # ext_* users are email-only: there is no chat to send to.
        return str(user.chat_id or '').lstrip('-').isdigit()

    def _nudge_admin_id(self) -> str:
        upd = getattr(self, '_current_update', None) or {}
        try:
            uid = self._get_user_id(upd)
        except Exception:
            uid = None
        return str(uid or getattr(self.config, 'SUPER_ADMIN_ID', '') or 'admin')

    def _last_nudge(self) -> Optional[tuple]:
        try:
            with self.db._connect() as conn:
                return conn.execute(
                    "SELECT created_at, target_id FROM admin_actions "
                    "WHERE action = 'nudge_sub' ORDER BY id DESC LIMIT 1"
                ).fetchone()
        except Exception as e:
            logger.warning(f"/nudge_sub: last-run read failed: {e}")
            return None

    def nudge_sub(self, chat_id: str, args: list) -> Optional[threading.Thread]:
        """``/nudge_sub`` — how many active users have no ``last_asn`` and
        a sample of them; ``/nudge_sub go`` — send them "refresh your
        subscription" (ru/en) on a worker, 50 ms apart, then report back
        here and log ``admin_actions('nudge_sub')``. Returns the worker
        so tests can join it."""
        from bot.services.sos import nudge_text, user_lang

        audience = self._nudge_audience()
        reachable = [u for u in audience if self._telegram_reachable(u)]
        go = bool(args) and str(args[0]).lower() == 'go'
        if not go:
            sample = ', '.join(
                html.escape(f"@{u.username}" if u.username else str(u.chat_id))
                for u in reachable[:self.NUDGE_SAMPLE]) or '—'
            last = self._last_nudge()
            lines = [
                "🔄 <b>/nudge_sub</b> — подсказка «обнови подписку» тем, у кого "
                "пуст last_asn",
                f"Активных без оператора: <b>{len(audience)}</b> "
                f"(Telegram: {len(reachable)}, только почта: "
                f"{len(audience) - len(reachable)} — им не шлём)",
                f"Пример: {sample}",
            ]
            if last:
                lines.append(f"Последняя рассылка: {html.escape(str(last[0]))} UTC, "
                             f"доставлено {html.escape(str(last[1]))}")
            lines.append("Отправить: <code>/nudge_sub go</code>")
            self._send(chat_id=chat_id, text='\n'.join(lines), parse_mode='HTML')
            return None

        if not reachable:
            self._send(chat_id=chat_id, text="📭 /nudge_sub: некому слать — у всех "
                                             "активных last_asn уже заполнен.")
            return None
        thread_id = self._get_thread_id(chat_id)
        admin_id = self._nudge_admin_id()
        self._send(chat_id=chat_id, message_thread_id=thread_id,
                   text=f"🔄 /nudge_sub: отправляю {len(reachable)} юзерам…")

        def _worker() -> None:
            sent = failed = 0
            for u in reachable:
                try:
                    ok = self.bot.send_message(chat_id=str(u.chat_id),
                                               text=nudge_text(user_lang(u)),
                                               parse_mode='HTML')
                    if ok:
                        sent += 1
                    else:
                        failed += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"/nudge_sub: send to {u.chat_id} failed: {e}")
                time.sleep(self.NUDGE_SEND_DELAY_S)
            try:
                self.db.log_admin_action(admin_id, 'nudge_sub',
                                         f"{sent}/{len(reachable)}",
                                         f"sent={sent} failed={failed}")
            except Exception as e:
                logger.warning(f"/nudge_sub: audit log failed: {e}")
            try:
                self._send(chat_id=chat_id, message_thread_id=thread_id,
                           text=f"✅ /nudge_sub: доставлено {sent}, ошибок {failed}.")
            except Exception as e:
                logger.warning(f"/nudge_sub: report send failed: {e}")
            logger.info(f"/nudge_sub by {admin_id}: sent={sent} failed={failed}")

        t = threading.Thread(target=_worker, daemon=True, name="nudge-sub")
        t.start()
        return t
