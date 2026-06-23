"""Failover Notification Service with Admin Broadcast Controls."""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable

from bot.models.performance import FailoverEvent, ExitNodeStatus

logger = logging.getLogger(__name__)


@dataclass
class FailoverBatch:
    batch_id: str
    affected_users: List[FailoverEvent]
    from_exit: str
    to_exit: str
    to_exit_status: Optional[ExitNodeStatus]
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    @property
    def user_count(self) -> int:
        return len(self.affected_users)
    
    @property
    def is_throttled(self) -> bool:
        return self.to_exit_status.is_throttled if self.to_exit_status else False


class FailoverNotificationService:
    """Silent mode with admin broadcast control."""
    
    def __init__(self, telegram_client=None, admin_chat_id: Optional[str] = None):
        self.telegram = telegram_client
        self.admin_chat_id = admin_chat_id
        self._pending_batches: Dict[str, FailoverBatch] = {}
        self._recent_failovers: List[FailoverEvent] = []
        self._on_broadcast: Optional[Callable[[str, List[str]], None]] = None
        self._running = False
        self._batch_task: Optional[asyncio.Task] = None
    
    def set_broadcast_callback(self, callback: Callable[[str, List[str]], None]) -> None:
        self._on_broadcast = callback
    
    async def handle_failover_event(self, event: FailoverEvent) -> dict:
        self._recent_failovers.append(event)
        logger.info(f"Failover: {event.user_id} from {event.from_exit} to {event.to_exit}")
        return {"notification_sent": False, "broadcast_requested": False, "batched": True}
    
    async def process_failover_batch(self, events: List[FailoverEvent],
                                     exit_statuses: Dict[str, ExitNodeStatus]) -> None:
        if not events:
            return
        by_target: Dict[str, List[FailoverEvent]] = {}
        for event in events:
            by_target.setdefault(event.to_exit, []).append(event)
        for target_exit, target_events in by_target.items():
            batch = FailoverBatch(
                batch_id=f"batch-{target_exit}-{datetime.now(timezone.utc).strftime('%H%M%S')}",
                affected_users=target_events, from_exit=target_events[0].from_exit,
                to_exit=target_exit, to_exit_status=exit_statuses.get(target_exit),
            )
            self._pending_batches[batch.batch_id] = batch
            await self._notify_admin(batch)
        self._recent_failovers = []
    
    async def _notify_admin(self, batch: FailoverBatch) -> None:
        if not self.admin_chat_id or not self.telegram:
            logger.warning("Cannot notify admin: no chat_id or telegram client")
            return
        status = batch.to_exit_status
        if status is None:
            emoji, status_text = ("❓", "Unknown status")
        elif batch.is_throttled:
            emoji, status_text = ("⚠️", f"THROTTLED (CPU: {status.cpu_percent:.1f}%)")
        else:
            emoji, status_text = ("ℹ️", f"Normal (CPU: {status.cpu_percent:.1f}%)")
        message = f"""{emoji} <b>Событие Failover</b>

<b>Переключено:</b> {batch.user_count} пользователей
<b>С:</b> {batch.from_exit}
<b>На:</b> {batch.to_exit}
<b>Статус:</b> {status_text}

<i>Пользователи не уведомлены (тихий режим).</i>"""
        keyboard = {"inline_keyboard": [
            [{"text": "📢 Разослать", "callback_data": f"failover:broadcast:{batch.batch_id}"}],
            [{"text": "📊 Статистика", "callback_data": f"failover:stats:{batch.batch_id}"},
             {"text": "✅ Игнорировать", "callback_data": f"failover:ignore:{batch.batch_id}"}],
        ]}
        try:
            await self.telegram.send_message(chat_id=self.admin_chat_id, text=message,
                                             parse_mode="HTML", reply_markup=keyboard)
            logger.info(f"Admin notified: {batch.batch_id}")
        except Exception as e:
            logger.error(f"Failed to notify admin: {e}")
    
    async def handle_admin_callback(self, callback_data: str, admin_chat_id: str) -> str:
        parts = callback_data.split(":")
        # Allow 3 or 4 parts (send_broadcast has 4)
        if len(parts) < 3 or parts[0] != "failover":
            return "Invalid callback"
        action, batch_id = parts[1], parts[2]
        batch = self._pending_batches.get(batch_id)
        if not batch:
            return "Batch expired"
        if action == "broadcast":
            await self._show_broadcast_dialog(batch, admin_chat_id)
            return "dialog_shown"
        elif action == "stats":
            await self.telegram.send_message(chat_id=admin_chat_id,
                text=self._get_batch_stats(batch), parse_mode="HTML")
            return "stats_sent"
        elif action == "ignore":
            del self._pending_batches[batch_id]
            return "ignored"
        elif action == "send_broadcast":
            # Note: message text is stored in batch, not in callback_data (to avoid injection and 64 byte limit)
            message_text = batch._pending_message if hasattr(batch, '_pending_message') else "Техническое обслуживание"
            await self._send_broadcast(batch, message_text)
            del self._pending_batches[batch_id]
            return "sent"
        return "Unknown action"
    
    async def _show_broadcast_dialog(self, batch: FailoverBatch, admin_chat_id: str) -> None:
        default = "⚠️ Ведутся работы. Возможны замедления." if batch.is_throttled else "✅ Тех. обслуживание завершено."
        # Store message in batch to avoid callback_data injection and 64 byte limit
        batch._pending_message = default
        keyboard = {"inline_keyboard": [
            [{"text": default[:30] + "...", "callback_data": f"failover:send_broadcast:{batch.batch_id}"}],
            [{"text": "❌ Отмена", "callback_data": f"failover:ignore:{batch.batch_id}"}],
        ]}
        await self.telegram.send_message(chat_id=admin_chat_id,
            text=f"📢 Рассылка ({batch.user_count} пользователей)\nВыберите сообщение:",
            parse_mode="HTML", reply_markup=keyboard)
    
    async def _send_broadcast(self, batch: FailoverBatch, message_text: str) -> dict:
        sent = failed = 0
        for chat_id in [e.chat_id for e in batch.affected_users]:
            try:
                await self.telegram.send_message(chat_id=chat_id, text=message_text)
                sent += 1
            except Exception as e:
                logger.error(f"Failed to send to {chat_id}: {e}")
                failed += 1
        await self.telegram.send_message(chat_id=self.admin_chat_id,
            text=f"📢 Рассылка завершена:\n✅ {sent}\n❌ {failed}")
        return {"sent": sent, "failed": failed}
    
    def _get_batch_stats(self, batch: FailoverBatch) -> str:
        status = batch.to_exit_status
        lines = ["📊 <b>Статистика Failover</b>", "",
                 f"<b>Пользователей:</b> {batch.user_count}",
                 f"<b>С:</b> {batch.from_exit} → <b>На:</b> {batch.to_exit}", ""]
        if status:
            lines.extend([f"CPU: {status.cpu_percent:.1f}%", f"Память: {status.memory_percent:.1f}%",
                         f"Подключения: {status.connections}", f"Оценка: {status.performance_score}",
                         f"Throttled: {'Да' if status.is_throttled else 'Нет'}"])
        lines.extend(["", "<b>Пользователи:</b>"] + [f"  - {e.user_id}" for e in batch.affected_users[:5]])
        if len(batch.affected_users) > 5:
            lines.append(f"  ... и ещё {len(batch.affected_users) - 5}")
        return "\n".join(lines)
    
    async def start_batch_processor(self, interval_seconds: int = 60, get_exit_statuses: Optional[Callable[[], Dict[str, ExitNodeStatus]]] = None) -> None:
        """Start background batch processor.
        
        Args:
            interval_seconds: Check interval
            get_exit_statuses: Optional callback to get current exit node statuses
        """
        self._running = True
        while self._running:
            await asyncio.sleep(interval_seconds)
            if self._recent_failovers and self._running:
                # Get actual statuses if callback provided, else empty dict
                statuses = get_exit_statuses() if get_exit_statuses else {}
                await self.process_failover_batch(list(self._recent_failovers), statuses)
    
    async def stop_batch_processor(self) -> None:
        """Stop background batch processor."""
        self._running = False
        logger.info("Batch processor stopped")
