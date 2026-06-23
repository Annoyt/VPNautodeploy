"""Tests for X-UI Service sync wrappers - Phase 2 fix verification.

These tests verify that sync wrapper methods work correctly after
the _run_async() removal (C-03 fix). Sync methods now use asyncio.run()
directly and emit deprecation warnings.
"""

import pytest
import asyncio
import warnings
from unittest.mock import Mock, patch, AsyncMock

from bot.services.xui_service import XUIService


class TestXUIServiceSyncWrappers:
    """Test sync wrapper methods after C-03 fix."""
    
    @pytest.fixture
    def xui_service(self):
        """Create XUIService with mocked dependencies."""
        with patch('bot.services.xui_service.XUIAPIClient'):
            with patch('bot.services.xui_service.XUIDatabase'):
                service = XUIService(db_path='/tmp/test.db', api_config={
                    'base_url': 'http://test:2053',
                    'username': 'admin',
                    'password': 'admin'
                })
                yield service
    
    def test_get_client_traffic_sync_emits_warning(self, xui_service):
        """Test get_client_traffic_sync emits deprecation warning."""
        # Mock the async method
        xui_service.get_client_traffic = AsyncMock(return_value={'upload': 100})
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = xui_service.get_client_traffic_sync('test@example.com')
            
            # Verify deprecation warning
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert 'deprecated' in str(w[0].message).lower()
    
    def test_get_inbound_settings_sync_emits_warning(self, xui_service):
        """Test get_inbound_settings_sync emits deprecation warning."""
        xui_service.get_inbound_settings = AsyncMock(return_value={'clients': []})
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = xui_service.get_inbound_settings_sync()
            
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
    
    def test_get_client_sync_emits_warning(self, xui_service):
        """Test get_client_sync emits deprecation warning."""
        xui_service.get_client = AsyncMock(return_value={'email': 'test@example.com'})
        
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = xui_service.get_client_sync('test@example.com')
            
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)


class TestXUIServiceThreadFallback:
    """Test that sync wrappers use thread fallback in async context."""
    
    def test_run_sync_exists(self):
        """Test that _run_sync method exists for safe async-to-sync bridge."""
        assert hasattr(XUIService, '_run_sync'), \
            "_run_sync should exist to safely bridge async calls"
    
    def test_executor_exists(self):
        """Test that _executor class attribute exists for thread fallback."""
        assert '_executor' in XUIService.__dict__, \
            "_executor should exist for running coroutines in separate threads"
    
    @pytest.mark.asyncio
    async def test_sync_wrapper_works_in_async_context(self):
        """Test that get_client_traffic_sync works from async context."""
        with patch('bot.services.xui_service.XUIAPIClient'):
            with patch('bot.services.xui_service.XUIDatabase'):
                service = XUIService(db_path='/tmp/test.db', api_config={
                    'base_url': 'http://test:2053',
                    'username': 'admin',
                    'password': 'admin'
                })
                service.get_client_traffic = AsyncMock(return_value={'upload': 100})
                
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    result = service.get_client_traffic_sync('test@example.com')
                
                assert result == {'upload': 100}


class TestXUIServiceAsyncMethods:
    """Test that async methods still work correctly."""
    
    @pytest.fixture
    def xui_service(self):
        """Create XUIService with mocked dependencies."""
        with patch('bot.services.xui_service.XUIAPIClient'):
            with patch('bot.services.xui_service.XUIDatabase'):
                service = XUIService(db_path='/tmp/test.db', api_config={
                    'base_url': 'http://test:2053',
                    'username': 'admin',
                    'password': 'admin'
                })
                yield service
    
    @pytest.mark.asyncio
    async def test_get_client_traffic_async_works(self, xui_service):
        """Test that async get_client_traffic still works."""
        # Mock DB (fallback when API returns None)
        mock_db = Mock()
        mock_db.get_client_traffic.return_value = {'upload': 100, 'download': 200, 'total': 300}
        xui_service.db = mock_db
        # API returns None to trigger DB fallback
        xui_service.api = None
        
        result = await xui_service.get_client_traffic('test@example.com')
        
        assert result is not None
        assert result['upload'] == 100
    
    @pytest.mark.asyncio
    async def test_get_inbound_settings_async_works(self, xui_service):
        """Test that async get_inbound_settings still works."""
        mock_db = Mock()
        mock_db.get_inbound_settings.return_value = {'clients': []}
        xui_service.db = mock_db
        
        result = await xui_service.get_inbound_settings()
        
        assert result is not None
        assert 'clients' in result
