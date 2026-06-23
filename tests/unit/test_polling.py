"""Tests for polling service."""

import pytest
from unittest.mock import Mock, patch, MagicMock

from bot.core.polling import PollingService
from bot.core.telegram_client import TelegramClient


class TestPollingServiceInitialization:
    """Test PollingService initialization."""
    
    def test_polling_service_initialization(self):
        """Test polling service creates with correct state."""
        mock_client = Mock(spec=TelegramClient)
        processor = Mock()
        
        service = PollingService(mock_client, processor)
        
        assert service.client == mock_client
        assert service.processor == processor
        assert service.running is False
        assert service.offset is None


class TestPollingServiceLifecycle:
    """Test polling service start/stop."""
    
    @pytest.fixture
    def service(self):
        """Create polling service with mocks."""
        mock_client = Mock(spec=TelegramClient)
        processor = Mock()
        return PollingService(mock_client, processor)
    
    def test_stop_sets_running_false(self, service):
        """Test stop() sets running to False."""
        service.running = True
        
        service.stop()
        
        assert service.running is False
        
    def test_stop_logs_message(self, service):
        """Test stop() logs message."""
        with patch('bot.core.polling.logger') as mock_logger:
            service.stop()
            mock_logger.info.assert_called_once_with("Stop requested")


class TestPollingServiceLoop:
    """Test polling loop logic."""
    
    def test_polling_loop_processes_updates(self):
        """Test polling loop processes updates correctly."""
        mock_client = Mock(spec=TelegramClient)
        processor = Mock()
        
        # Simulate 2 updates then stop
        updates = [
            {'update_id': 1, 'message': {'text': 'Hello'}},
            {'update_id': 2, 'message': {'text': 'World'}}
        ]
        mock_client.get_updates = Mock(return_value=(updates, 3))
        
        service = PollingService(mock_client, processor)
        
        # Run one iteration then stop
        with patch.object(service, 'running', True):
            def stop_after_iteration(*args, **kwargs):
                service.running = False
                return (updates, 3)
            mock_client.get_updates.side_effect = stop_after_iteration
            
            # Run polling (will process one batch then stop)
            service.start()
        
        # Verify updates were processed
        assert processor.call_count == 2
        processor.assert_any_call({'update_id': 1, 'message': {'text': 'Hello'}})
        processor.assert_any_call({'update_id': 2, 'message': {'text': 'World'}})
        
    def test_polling_loop_updates_offset(self):
        """Test polling loop updates offset."""
        mock_client = Mock(spec=TelegramClient)
        processor = Mock()
        mock_client.get_updates = Mock(return_value=([], 100))
        
        service = PollingService(mock_client, processor)
        
        with patch.object(service, 'running', True):
            def stop_after_iteration(*args, **kwargs):
                service.running = False
                return ([], 100)
            mock_client.get_updates.side_effect = stop_after_iteration
            
            service.start()
        
        assert service.offset == 100
        
    def test_polling_loop_handles_processor_exception(self):
        """Test polling loop handles processor exceptions gracefully."""
        mock_client = Mock(spec=TelegramClient)
        processor = Mock(side_effect=Exception("Processing error"))
        
        updates = [{'update_id': 1}]
        mock_client.get_updates = Mock(return_value=(updates, 2))
        
        service = PollingService(mock_client, processor)
        
        with patch.object(service, 'running', True):
            def stop_after_iteration(*args, **kwargs):
                service.running = False
                return (updates, 2)
            mock_client.get_updates.side_effect = stop_after_iteration
            
            # Should not raise
            service.start()
        
        # Processor was called despite exception
        processor.assert_called_once()
        
    def test_polling_loop_handles_client_exception(self):
        """Test polling loop handles client exceptions gracefully."""
        mock_client = Mock(spec=TelegramClient)
        processor = Mock()
        mock_client.get_updates = Mock(side_effect=Exception("API error"))
        
        service = PollingService(mock_client, processor)
        
        with patch.object(service, 'running', True):
            with patch.object(service._stop_event, 'wait') as mock_wait:
                def stop_after_iteration(*args, **kwargs):
                    service.running = False
                    raise Exception("API error")
                mock_client.get_updates.side_effect = stop_after_iteration
                
                # Should not raise
                service.start()
                
                # Should wait before retry (interruptible sleep)
                mock_wait.assert_called_once_with(5)
        
        # Processor was not called due to client error
        processor.assert_not_called()
        
    def test_polling_loop_uses_correct_parameters(self):
        """Test polling loop uses correct get_updates parameters."""
        mock_client = Mock(spec=TelegramClient)
        processor = Mock()
        mock_client.get_updates = Mock(return_value=([], 1))
        
        service = PollingService(mock_client, processor)
        service.offset = 50
        
        with patch.object(service, 'running', True):
            def stop_after_iteration(*args, **kwargs):
                service.running = False
                return ([], 51)
            mock_client.get_updates.side_effect = stop_after_iteration
            
            service.start()
        
        # Verify get_updates was called with correct parameters
        mock_client.get_updates.assert_called_once_with(
            offset=50,
            limit=100,
            timeout=30,
            allowed_updates=['message', 'callback_query', 'pre_checkout_query', 'edited_message']
        )
        
    def test_polling_loop_processes_all_updates(self):
        """Test polling loop processes all updates including None."""
        mock_client = Mock(spec=TelegramClient)
        processor = Mock()
        
        updates = [None, {'update_id': 1}]  # Include None update
        mock_client.get_updates = Mock(return_value=(updates, 2))
        
        service = PollingService(mock_client, processor)
        
        with patch.object(service, 'running', True):
            def stop_after_iteration(*args, **kwargs):
                service.running = False
                return (updates, 2)
            mock_client.get_updates.side_effect = stop_after_iteration
            
            # Should not raise
            service.start()
        
        # Processor called for all updates (including None)
        assert processor.call_count == 2
        processor.assert_any_call(None)
        processor.assert_any_call({'update_id': 1})
