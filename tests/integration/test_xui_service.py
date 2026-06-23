"""Integration tests for unified XUIService (HTTP API + DB fallback)"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot.services.xui_service import XUIService
from bot.services.xui_db import XUIDatabase


class TestXUIService:
    """Tests for XUIService with API and fallback"""
    
    @pytest.fixture
    def temp_xui_db(self):
        """Create temporary X-UI database"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # inbounds table
        c.execute('''
            CREATE TABLE inbounds (
                id INTEGER PRIMARY KEY,
                protocol TEXT,
                port INTEGER,
                settings TEXT
            )
        ''')
        
        # Insert test VLESS inbound
        settings = json.dumps({
            'clients': [{'id': 'test-uuid', 'email': 'test@example.com'}]
        })
        c.execute(
            "INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)",
            (1, 'vless', 443, settings)
        )
        
        # client_traffics table
        c.execute('''
            CREATE TABLE client_traffics (
                id INTEGER PRIMARY KEY,
                email TEXT,
                up INTEGER DEFAULT 0,
                down INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0
            )
        ''')
        
        # Insert test traffic
        c.execute(
            "INSERT INTO client_traffics (email, up, down, total) VALUES (?, ?, ?, ?)",
            ('test@example.com', 1024, 2048, 3072)
        )
        
        conn.commit()
        conn.close()
        
        yield db_path
        os.unlink(db_path)
    
    @pytest.fixture
    def service_db_only(self, temp_xui_db):
        """Create service without API (DB only)"""
        return XUIService(db_path=temp_xui_db, api_config=None)
    
    @pytest.fixture
    def service_with_api(self, temp_xui_db):
        """Create service with API config"""
        return XUIService(
            db_path=temp_xui_db,
            api_config={
                'base_url': 'http://test.example.com:2053',
                'username': 'admin',
                'password': 'admin'
            }
        )
    
    @pytest.mark.asyncio
    async def test_get_client_traffic_db_fallback(self, service_db_only):
        """Test getting traffic from DB when no API"""
        result = await service_db_only.get_client_traffic('test@example.com')
        
        assert result is not None
        assert result['upload'] == 1024
        assert result['download'] == 2048
        assert result['total'] == 3072
    
    @pytest.mark.asyncio
    async def test_get_client_traffic_api_fallback_to_db(self, service_with_api):
        """Test API failure falls back to DB"""
        # Mock API to fail
        with patch.object(service_with_api.api, 'get_client_traffic', side_effect=Exception("API Error")):
            result = await service_with_api.get_client_traffic('test@example.com')
        
        # Should fallback to DB
        assert result is not None
        assert result['upload'] == 1024
    
    @pytest.mark.asyncio
    async def test_get_client_traffic_api_success(self, service_with_api):
        """Test successful API call returns data without DB fallback"""
        # Mock API to return data (API uses 'up'/'down', service transforms to 'upload'/'download')
        mock_traffic = {'up': 5000, 'down': 6000, 'total': 11000}
        with patch.object(service_with_api.api, 'get_client_traffic', return_value=mock_traffic):
            result = await service_with_api.get_client_traffic('test@example.com')
        
        # Service transforms 'up'->'upload', 'down'->'download' for consistency with DB format
        expected = {'upload': 5000, 'download': 6000, 'total': 11000}
        assert result == expected
    
    @pytest.mark.asyncio
    async def test_get_inbound_settings_db_fallback(self, service_db_only):
        """Test getting inbound settings from DB"""
        result = await service_db_only.get_inbound_settings(1)
        
        assert result is not None
        assert 'clients' in result
        assert result['clients'][0]['email'] == 'test@example.com'
    
    @pytest.mark.asyncio
    async def test_get_online_users_no_api(self, service_db_only):
        """Test online users returns empty when no API"""
        result = await service_db_only.get_online_users()
        
        assert result == []
    
    @pytest.mark.asyncio
    async def test_get_online_users_with_api(self, service_with_api):
        """Test getting online users via API"""
        # Mock login and the API method directly
        with patch.object(service_with_api.api, 'login', return_value=True):
            with patch.object(service_with_api.api, 'get_online_clients',
                       return_value=['user1@test.com', 'user2@test.com']):
                result = await service_with_api.get_online_users()
        
        assert len(result) == 2
        assert 'user1@test.com' in result
    
    @pytest.mark.asyncio
    async def test_get_online_users_login_failure(self, service_with_api):
        """Test online users returns empty on login failure"""
        with patch.object(service_with_api.api, 'login', return_value=False):
            result = await service_with_api.get_online_users()
        
        assert result == []
    
    @pytest.mark.asyncio
    async def test_service_context_manager(self, temp_xui_db):
        """Test async context manager"""
        service = XUIService(
            db_path=temp_xui_db,
            api_config={'base_url': 'http://test.com', 'username': 'admin', 'password': 'admin'}
        )
        
        mock_api = AsyncMock()
        service.api = mock_api
        
        async with service as s:
            assert s is service
        
        mock_api.close.assert_called_once()
