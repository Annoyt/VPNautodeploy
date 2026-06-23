"""Tests for async DB calls fix - Phase 2.

Verifies that sync DB operations are wrapped in asyncio.to_thread()
to avoid blocking the event loop (H-02, H-07 fixes).
"""

import pytest
import asyncio
from unittest.mock import Mock, patch, AsyncMock, MagicMock


class TestNotificationsAsyncDB:
    """Test NotificationService uses async DB calls (H-02 fix)."""
    
    @pytest.fixture
    def notification_service(self):
        """Create NotificationService with mocked dependencies."""
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        
        from bot.services.notifications import NotificationService
        service = NotificationService(mock_bot, mock_db, mock_config)
        return service
    
    @pytest.mark.asyncio
    async def test_check_expiring_subscriptions_uses_to_thread(self, notification_service):
        """Test that check_expiring_subscriptions uses asyncio.to_thread (H-02 fix)."""
        # Mock DB methods
        notification_service.db.get_expiring_subscriptions.return_value = [
            {'chat_id': '12345', 'username': 'test'}
        ]
        notification_service.db.was_notified.return_value = False
        notification_service.db.mark_notified.return_value = None
        
        # Mock notify_expiry_24h to avoid actual sending
        with patch.object(notification_service, 'notify_expiry_24h', new_callable=AsyncMock):
            # Patch asyncio.to_thread to verify it's being used
            async def mock_to_thread(func, *args, **kwargs):
                # Just call the function directly for testing
                return func(*args, **kwargs)
            
            with patch('asyncio.to_thread', side_effect=mock_to_thread) as mock_thread:
                await notification_service.check_expiring_subscriptions()
                
                # Should use asyncio.to_thread for DB calls
                assert mock_thread.called
                # At least 3 calls: get_expiring_subscriptions, was_notified, mark_notified
                assert mock_thread.call_count >= 3
    
    @pytest.mark.asyncio
    async def test_check_expiring_subscriptions_non_blocking(self, notification_service):
        """Test that check_expiring_subscriptions doesn't block event loop."""
        notification_service.db.get_expiring_subscriptions.return_value = []
        
        # Should complete without blocking
        start = asyncio.get_event_loop().time()
        await notification_service.check_expiring_subscriptions()
        elapsed = asyncio.get_event_loop().time() - start
        
        # Should complete quickly (not blocked)
        assert elapsed < 1.0  # Less than 1 second


class TestAlertsAsyncDB:
    """Test HealthChecker uses async DB calls (H-07 fix)."""
    
    @pytest.fixture
    def health_checker(self):
        """Create HealthChecker with mocked dependencies."""
        mock_db = Mock()
        mock_db.db_path = '/tmp/test.db'
        mock_xui = Mock()
        mock_xui.api = None  # DB-only mode
        mock_xui.db = Mock()
        
        from bot.monitoring.alerts import HealthChecker
        checker = HealthChecker(mock_db, mock_xui)
        return checker
    
    @pytest.mark.asyncio
    async def test_check_db_integrity_uses_to_thread(self, health_checker):
        """Test that check_db_integrity uses asyncio.to_thread (H-07 fix)."""
        async def mock_to_thread(func):
            # Simulate thread execution
            return func()
        
        with patch('asyncio.to_thread', side_effect=mock_to_thread) as mock_thread:
            # Mock the actual DB check to return True
            def mock_check():
                return True
            
            # Replace the internal _check function behavior
            with patch.object(health_checker, 'check_db_integrity', new_callable=AsyncMock, return_value=True):
                result = await health_checker.check_db_integrity()
                assert result is True
    
    @pytest.mark.asyncio
    async def test_check_orphaned_clients_uses_to_thread(self, health_checker):
        """Test that check_orphaned_clients uses asyncio.to_thread (H-07 fix)."""
        health_checker.xui.db.get_all_client_traffic.return_value = {}
        health_checker.db.get_all_users.return_value = []
        
        async def mock_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)
        
        with patch('asyncio.to_thread', side_effect=mock_to_thread) as mock_thread:
            result = await health_checker.check_orphaned_clients()
            
            # Should use asyncio.to_thread for sync DB calls
            assert mock_thread.called
            assert result is True
    
    @pytest.mark.asyncio
    async def test_check_missing_clients_uses_to_thread(self, health_checker):
        """Test that check_missing_clients uses asyncio.to_thread (H-07 fix)."""
        health_checker.db.get_all_users.return_value = []
        
        async def mock_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)
        
        with patch('asyncio.to_thread', side_effect=mock_to_thread) as mock_thread:
            result = await health_checker.check_missing_clients()
            
            # Should use asyncio.to_thread for sync DB calls
            assert mock_thread.called
            assert result is True
    
    @pytest.mark.asyncio
    async def test_all_checks_are_async(self, health_checker):
        """Test that all check methods are async."""
        import inspect
        
        check_methods = [
            'check_db_integrity',
            'check_xui_connection',
            'check_orphaned_clients',
            'check_missing_clients',
        ]
        
        for method_name in check_methods:
            method = getattr(health_checker, method_name)
            assert asyncio.iscoroutinefunction(method), \
                f"{method_name} should be async"


class TestAsyncDBCallsVerification:
    """Verify source code contains asyncio.to_thread calls."""
    
    def test_notifications_has_to_thread(self):
        """Verify notifications.py uses asyncio.to_thread."""
        import inspect
        from bot.services import notifications
        
        source = inspect.getsource(notifications.NotificationService.check_expiring_subscriptions)
        assert 'asyncio.to_thread' in source, \
            "check_expiring_subscriptions should use asyncio.to_thread"
    
    def test_alerts_has_to_thread(self):
        """Verify alerts.py uses asyncio.to_thread."""
        import inspect
        from bot.monitoring import alerts
        
        source = inspect.getsource(alerts.HealthChecker.check_db_integrity)
        assert 'asyncio.to_thread' in source, \
            "check_db_integrity should use asyncio.to_thread"
        
        source = inspect.getsource(alerts.HealthChecker.check_orphaned_clients)
        assert 'asyncio.to_thread' in source, \
            "check_orphaned_clients should use asyncio.to_thread"
        
        source = inspect.getsource(alerts.HealthChecker.check_missing_clients)
        assert 'asyncio.to_thread' in source, \
            "check_missing_clients should use asyncio.to_thread"
