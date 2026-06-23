"""Tests for X-UI API client (async HTTP)"""

import json
import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock

from bot.services.xui_api.client import XUIAPIClient, XUIClientConfig


class AsyncContextManagerMock:
    """Helper class to mock async context managers"""
    def __init__(self, return_value):
        self.return_value = return_value
    
    async def __aenter__(self):
        return self.return_value
    
    async def __aexit__(self, *args):
        pass


class TestXUIAPIClient:
    """Tests for XUIAPIClient"""
    
    @pytest.fixture
    def client(self):
        """Create API client instance"""
        config = XUIClientConfig(
            base_url='http://test.example.com:2053',
            username='test_user',
            password='test_pass'
        )
        return XUIAPIClient(config)
    
    @pytest.mark.asyncio
    async def test_init(self, client):
        """Test client initialization"""
        assert client.config.base_url == 'http://test.example.com:2053'
        assert client.config.username == 'test_user'
        assert client.config.password == 'test_pass'
        assert client.session is None
    
    @pytest.mark.asyncio
    async def test_login_success(self, client):
        """Test successful login"""
        csrf_response = Mock()
        csrf_response.status = 200
        csrf_response.text = AsyncMock(
            return_value='<meta name="csrf-token" content="test-csrf-token">'
        )

        login_response = Mock()
        login_response.status = 200
        login_response.json = AsyncMock(return_value={"success": True})

        mock_session = Mock()
        mock_session.get = Mock(return_value=AsyncContextManagerMock(csrf_response))
        mock_session.post = Mock(return_value=AsyncContextManagerMock(login_response))

        with patch.object(client, '_get_session', new_callable=AsyncMock, return_value=mock_session):
            result = await client.login()

        assert result is True
    
    @pytest.mark.asyncio
    async def test_login_failure(self, client):
        """Test failed login"""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={'success': False, 'msg': 'Invalid credentials'})
        
        mock_post = AsyncContextManagerMock(mock_response)
        mock_session = Mock()
        mock_session.post = Mock(return_value=mock_post)
        
        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.login()
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_login_http_error(self, client):
        """Test login with HTTP error"""
        mock_response = Mock()
        mock_response.status = 500
        
        mock_post = AsyncContextManagerMock(mock_response)
        mock_session = Mock()
        mock_session.post = Mock(return_value=mock_post)
        
        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.login()
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_get_inbound_success(self, client):
        """Test getting inbound successfully"""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            'success': True,
            'obj': {'id': 1, 'protocol': 'vless', 'port': 443}
        })
        
        mock_session = AsyncMock()
        mock_session.request = AsyncMock(return_value=mock_response)
        
        with patch.object(client, '_get_session', return_value=mock_session):
            with patch.object(client, 'login', return_value=True):
                result = await client.get_inbound(1)
        
        assert result is not None
        assert result['protocol'] == 'vless'
    
    @pytest.mark.asyncio
    async def test_get_inbound_not_found(self, client):
        """Test getting non-existent inbound"""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            'success': False,
            'msg': 'Inbound not found'
        })
        
        mock_get = AsyncContextManagerMock(mock_response)
        mock_session = Mock()
        mock_session.get = Mock(return_value=mock_get)
        
        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.get_inbound(999)
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_online_clients_success(self, client):
        """Test getting online clients successfully"""
        csrf_response = Mock()
        csrf_response.status = 200
        csrf_response.text = AsyncMock(
            return_value='<meta name="csrf-token" content="test-csrf-token">'
        )

        login_response = Mock()
        login_response.status = 200
        login_response.json = AsyncMock(return_value={"success": True})

        online_response = Mock()
        online_response.status = 200
        online_response.json = AsyncMock(return_value={
            'success': True,
            'obj': ['client1@test.com', 'client2@test.com']
        })

        mock_session = Mock()
        mock_session.get = Mock(return_value=AsyncContextManagerMock(csrf_response))
        mock_session.post = Mock(side_effect=[
            AsyncContextManagerMock(login_response),
            AsyncContextManagerMock(online_response),
        ])

        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.get_online_clients()

        assert result is not None
        assert len(result) == 2
        assert 'client1@test.com' in result
        assert 'client2@test.com' in result

    @pytest.mark.asyncio
    async def test_get_client_traffic_success(self, client):
        """Test getting client traffic successfully"""
        # Mock get_inbounds to return data with clientStats
        mock_inbounds_response = Mock()
        mock_inbounds_response.status = 200
        mock_inbounds_response.json = AsyncMock(return_value={
            'success': True,
            'obj': [{
                'id': 1,
                'clientStats': [
                    {'email': 'test@test.com', 'up': 1024, 'down': 2048, 'total': 3072}
                ]
            }]
        })
        
        mock_session = AsyncMock()
        mock_session.request = AsyncMock(return_value=mock_inbounds_response)
        
        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.get_client_traffic('test@test.com')
        
        assert result is not None
        assert result['email'] == 'test@test.com'
        assert result['up'] == 1024
        assert result['down'] == 2048
        assert result['total'] == 3072
    
    @pytest.mark.asyncio
    async def test_get_client_traffic_not_found(self, client):
        """Test getting traffic for non-existent client"""
        mock_response = Mock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            'success': True,
            'obj': None
        })
        
        mock_get = AsyncContextManagerMock(mock_response)
        mock_session = Mock()
        mock_session.get = Mock(return_value=mock_get)
        
        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.get_client_traffic('nonexistent@test.com')
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_close_session(self, client):
        """Test closing session"""
        mock_session = AsyncMock()
        mock_session.closed = False
        client.session = mock_session
        
        await client.close()
        
        mock_session.close.assert_called_once()
        assert client.session is None
    
    @pytest.mark.asyncio
    async def test_context_manager(self, client):
        """Test async context manager"""
        mock_session = AsyncMock()
        mock_session.closed = False
        client.session = mock_session
        
        async with client as c:
            assert c is client
        
        mock_session.close.assert_called_once()
