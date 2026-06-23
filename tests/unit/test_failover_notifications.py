"""Test Suite: Failover Notifications

Purpose:
    Verify silent mode operation and admin notification flow.

Key Scenarios:
    1. Silent mode (no user notifications)
    2. Admin notification with buttons
    3. Broadcast functionality

When to Run:
    - After changes to failover_notifications.py
    - When modifying notification logic

Dependencies:
    - Mock telegram client
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from bot.services.failover_notifications import FailoverNotificationService, FailoverBatch
from bot.models.performance import FailoverEvent, ExitNodeStatus


class MockTelegramClient:
    """Mock Telegram client for testing."""
    
    def __init__(self):
        self.send_message = AsyncMock()


class TestSilentMode:
    """Tests for silent operation mode."""
    
    @pytest.fixture
    def service(self):
        telegram = MockTelegramClient()
        return FailoverNotificationService(telegram_client=telegram, admin_chat_id="admin123")
    
    @pytest.fixture
    def sample_event(self):
        return FailoverEvent(
            user_id="user1",
            chat_id="chat1",
            from_exit="exit-1",
            to_exit="exit-2",
            reason="health_check",
            is_throttled_target=True,
        )
    
    @pytest.mark.asyncio
    async def test_handle_failover_event_returns_silent(self, service, sample_event):
        """Test that events are handled silently."""
        result = await service.handle_failover_event(sample_event)
        
        assert result["notification_sent"] is False
        assert result["batched"] is True
    
    @pytest.mark.asyncio
    async def test_no_user_notification_sent(self, service, sample_event):
        """Test that users receive no automatic notifications."""
        await service.handle_failover_event(sample_event)
        
        # Check that telegram.send_message was NOT called for user
        for call in service.telegram.send_message.call_args_list:
            assert call.kwargs.get("chat_id") != "chat1"


class TestAdminNotifications:
    """Tests for admin notification flow."""
    
    @pytest.fixture
    def service(self):
        telegram = MockTelegramClient()
        return FailoverNotificationService(telegram_client=telegram, admin_chat_id="admin123")
    
    @pytest.fixture
    def sample_batch(self):
        return FailoverBatch(
            batch_id="batch-1",
            affected_users=[
                FailoverEvent("user1", "chat1", "exit-1", "exit-2", "test", True),
                FailoverEvent("user2", "chat2", "exit-1", "exit-2", "test", True),
            ],
            from_exit="exit-1",
            to_exit="exit-2",
            to_exit_status=ExitNodeStatus(
                node_id="exit-2", is_healthy=True, is_throttled=True,
                performance_score=20, cpu_percent=50.0,
                memory_percent=40.0, connections=5, tier="limited",
            ),
        )
    
    @pytest.mark.asyncio
    async def test_admin_notified_about_failover(self, service, sample_batch):
        """Test that admin receives notification."""
        await service._notify_admin(sample_batch)
        
        service.telegram.send_message.assert_called_once()
        call = service.telegram.send_message.call_args
        assert call.kwargs["chat_id"] == "admin123"
        assert "Событие Failover" in call.kwargs["text"]
    
    @pytest.mark.asyncio
    async def test_admin_notification_includes_buttons(self, service, sample_batch):
        """Test that admin notification includes action buttons."""
        await service._notify_admin(sample_batch)
        
        call = service.telegram.send_message.call_args
        keyboard = call.kwargs["reply_markup"]
        assert "inline_keyboard" in keyboard
        # Should have broadcast, stats, ignore buttons
        assert len(keyboard["inline_keyboard"]) >= 2
    
    @pytest.mark.asyncio
    async def test_admin_notification_shows_throttled(self, service, sample_batch):
        """Test that throttled status is shown to admin."""
        await service._notify_admin(sample_batch)
        
        call = service.telegram.send_message.call_args
        assert "THROTTLED" in call.kwargs["text"]
        assert "50.0%" in call.kwargs["text"]


class TestAdminCallbacks:
    """Tests for admin callback handling."""
    
    @pytest.fixture
    def service(self):
        telegram = MockTelegramClient()
        svc = FailoverNotificationService(telegram_client=telegram, admin_chat_id="admin123")
        # Add a pending batch
        svc._pending_batches["batch-1"] = FailoverBatch(
            batch_id="batch-1",
            affected_users=[FailoverEvent("user1", "chat1", "exit-1", "exit-2", "test", True)],
            from_exit="exit-1",
            to_exit="exit-2",
            to_exit_status=None,
        )
        return svc
    
    @pytest.mark.asyncio
    async def test_broadcast_callback_shows_dialog(self, service):
        """Test that broadcast callback shows message selection dialog."""
        result = await service.handle_admin_callback("failover:broadcast:batch-1", "admin123")
        
        assert result == "dialog_shown"
        # Should send dialog message
        assert service.telegram.send_message.call_count >= 1
    
    @pytest.mark.asyncio
    async def test_ignore_callback_removes_batch(self, service):
        """Test that ignore callback removes the batch."""
        result = await service.handle_admin_callback("failover:ignore:batch-1", "admin123")
        
        assert result == "ignored"
        assert "batch-1" not in service._pending_batches
    
    @pytest.mark.asyncio
    async def test_stats_callback_shows_stats(self, service):
        """Test that stats callback shows statistics."""
        result = await service.handle_admin_callback("failover:stats:batch-1", "admin123")
        
        assert result == "stats_sent"
        service.telegram.send_message.assert_called()


class TestBroadcastFunctionality:
    """Tests for broadcast to users."""
    
    @pytest.fixture
    def service(self):
        telegram = MockTelegramClient()
        return FailoverNotificationService(telegram_client=telegram, admin_chat_id="admin123")
    
    @pytest.fixture
    def sample_batch(self):
        return FailoverBatch(
            batch_id="batch-1",
            affected_users=[
                FailoverEvent("user1", "chat1", "exit-1", "exit-2", "test", True),
                FailoverEvent("user2", "chat2", "exit-1", "exit-2", "test", True),
            ],
            from_exit="exit-1",
            to_exit="exit-2",
            to_exit_status=None,
        )
    
    @pytest.mark.asyncio
    async def test_broadcast_sends_to_all_users(self, service, sample_batch):
        """Test that broadcast sends to all affected users."""
        await service._send_broadcast(sample_batch, "Test message")
        
        # Should send to both users
        assert service.telegram.send_message.call_count >= 2
    
    @pytest.mark.asyncio
    async def test_broadcast_reports_results(self, service, sample_batch):
        """Test that broadcast reports success/failure counts."""
        result = await service._send_broadcast(sample_batch, "Test message")
        
        assert "sent" in result
        assert "failed" in result
    
    @pytest.mark.asyncio
    async def test_batch_stats_generation(self, service, sample_batch):
        """Test batch statistics generation."""
        stats = service._get_batch_stats(sample_batch)
        
        assert "Статистика Failover" in stats
        assert "user1" in stats
        assert "user2" in stats
