"""Unit tests for XUI API Client - Phase 2 fix verification.

Tests for H-03 fix: Auto-login retry on 401.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
import aiohttp

from bot.services.xui_api.client import XUIAPIClient, XUIClientConfig


class TestXUIAPIClient:
    """Tests for XUIAPIClient."""
    
    @pytest.fixture
    def config(self):
        """Test configuration."""
        return XUIClientConfig(
            base_url="http://test:2026",
            username="admin",
            password="admin"
        )
    
    @pytest.fixture
    def client(self, config):
        """Test client instance."""
        return XUIAPIClient(config)
    
    def _create_mock_response(self, status=200, json_data=None, text="success"):
        """Helper to create mock response."""
        mock_response = MagicMock()
        mock_response.status = status
        mock_response.json = AsyncMock(return_value=json_data or {})
        mock_response.text = AsyncMock(return_value=text)
        return mock_response

    def _create_async_context_mock(self, response):
        """Helper to create an async context manager mock."""
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=response)
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    def _create_mock_session(self, get_response=None, post_response=None):
        """Helper to create a mock aiohttp session."""
        mock_session = MagicMock()
        if get_response is not None:
            mock_session.get = MagicMock(return_value=self._create_async_context_mock(get_response))
        if post_response is not None:
            mock_session.post = MagicMock(return_value=self._create_async_context_mock(post_response))
        return mock_session

    @pytest.mark.asyncio
    async def test_login_success(self, client):
        """Test successful login."""
        csrf_response = self._create_mock_response(
            text='<meta name="csrf-token" content="test-csrf-token">'
        )
        login_response = self._create_mock_response(
            json_data={"success": True}
        )

        mock_session = self._create_mock_session(
            get_response=csrf_response,
            post_response=login_response
        )

        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.login()
            assert result is True

    @pytest.mark.asyncio
    async def test_login_failure(self, client):
        """Test failed login."""
        csrf_response = self._create_mock_response(
            text='<meta name="csrf-token" content="test-csrf-token">'
        )
        login_response = self._create_mock_response(status=401)

        mock_session = self._create_mock_session(
            get_response=csrf_response,
            post_response=login_response
        )

        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.login()
            assert result is False
    
    @pytest.mark.asyncio
    async def test_authenticated_request_exists(self, client):
        """Test that _authenticated_request method exists (H-03 fix)."""
        assert hasattr(client, '_authenticated_request'), \
            "_authenticated_request method should exist"
    
    @pytest.mark.asyncio
    async def test_get_inbounds_success(self, client):
        """Test getting inbounds via _authenticated_request."""
        mock_response = self._create_mock_response(
            json_data={"obj": [{"id": 1, "protocol": "vless"}]}
        )
        
        # Mock _authenticated_request to return the mock response
        with patch.object(client, '_authenticated_request', return_value=mock_response):
            inbounds = await client.get_inbounds()
            assert len(inbounds) == 1
            assert inbounds[0]["id"] == 1
    
    @pytest.mark.asyncio
    async def test_get_inbound_by_id(self, client):
        """Test getting specific inbound."""
        mock_response = self._create_mock_response(
            json_data={"obj": {"id": 1, "protocol": "vless"}}
        )
        
        with patch.object(client, '_authenticated_request', return_value=mock_response):
            inbound = await client.get_inbound(1)
            assert inbound["id"] == 1
    
    @pytest.mark.asyncio
    async def test_get_client_traffic(self, client):
        """Test getting client traffic."""
        mock_response = self._create_mock_response(
            json_data={
                "obj": [{
                    "id": 1,
                    "clientStats": [
                        {"email": "test@example.com", "up": 1000, "down": 2000, "total": 3000}
                    ]
                }]
            }
        )
        
        with patch.object(client, '_authenticated_request', return_value=mock_response):
            traffic = await client.get_client_traffic("test@example.com")
            assert traffic["email"] == "test@example.com"
            assert traffic["up"] == 1000
            assert traffic["down"] == 2000
    
    @pytest.mark.asyncio
    async def test_get_all_clients_stats(self, client):
        """Test getting all clients stats."""
        mock_response = self._create_mock_response(
            json_data={
                "obj": [
                    {
                        "id": 1,
                        "clientStats": [
                            {"email": "user1@example.com", "up": 1000, "down": 2000, "total": 3000},
                            {"email": "user2@example.com", "up": 500, "down": 1000, "total": 1500}
                        ]
                    }
                ]
            }
        )
        
        with patch.object(client, '_authenticated_request', return_value=mock_response):
            stats = await client.get_all_clients_stats()
            assert isinstance(stats, dict)
            assert len(stats) == 2
            assert "user1@example.com" in stats
            assert stats["user1@example.com"]["up"] == 1000
    
    # ===== NEW TESTS FOR UPDATE METHODS =====
    
    @pytest.mark.asyncio
    async def test_update_inbound_success(self, client):
        """Test successful inbound update via API."""
        mock_response = self._create_mock_response(
            status=200,
            json_data={"success": True, "msg": "Inbound updated"}
        )
        
        with patch.object(client, '_authenticated_request', return_value=mock_response):
            result = await client.update_inbound(1, {"protocol": "vless"})
            assert result is True
    
    @pytest.mark.asyncio
    async def test_update_inbound_api_error(self, client):
        """Test inbound update when API returns error."""
        mock_response = self._create_mock_response(
            status=200,
            json_data={"success": False, "msg": "Invalid protocol"}
        )
        
        with patch.object(client, '_authenticated_request', return_value=mock_response):
            result = await client.update_inbound(1, {"protocol": "invalid"})
            assert result is False
    
    @pytest.mark.asyncio
    async def test_update_inbound_http_error(self, client):
        """Test inbound update when HTTP request fails."""
        mock_response = self._create_mock_response(status=500)
        
        with patch.object(client, '_authenticated_request', return_value=mock_response):
            result = await client.update_inbound(1, {"protocol": "vless"})
            assert result is False
    
    @pytest.mark.asyncio
    async def test_update_inbound_exception(self, client):
        """Test inbound update when exception occurs."""
        with patch.object(client, '_authenticated_request', side_effect=Exception("Network error")):
            result = await client.update_inbound(1, {"protocol": "vless"})
            assert result is False


class TestXUIAPIClientReAuth:
    """Test auto-login retry functionality (H-03 fix)."""
    
    @pytest.fixture
    def client(self):
        """Test client instance."""
        config = XUIClientConfig(
            base_url="http://test:2026",
            username="admin",
            password="admin"
        )
        return XUIAPIClient(config)
    
    @pytest.mark.asyncio
    async def test_authenticated_request_retries_on_401(self, client):
        """Test that _authenticated_request retries on 401 (H-03 fix)."""
        # First call returns 401, second call returns 200
        mock_response_401 = MagicMock()
        mock_response_401.status = 401
        
        mock_response_200 = MagicMock()
        mock_response_200.status = 200
        mock_response_200.json = AsyncMock(return_value={"obj": []})
        
        mock_session = MagicMock()
        mock_session.request = AsyncMock(side_effect=[mock_response_401, mock_response_200])
        
        # Mock login to succeed
        with patch.object(client, 'login', return_value=True) as mock_login:
            with patch.object(client, '_get_session', return_value=mock_session):
                response = await client._authenticated_request('GET', 'http://test/url')
                
                # Should have called login after 401
                mock_login.assert_called_once()
                # Should have made 2 requests
                assert mock_session.request.call_count == 2
                # Final response should be 200
                assert response.status == 200
    
    @pytest.mark.asyncio
    async def test_authenticated_request_no_retry_on_success(self, client):
        """Test that _authenticated_request doesn't retry on success."""
        mock_response = MagicMock()
        mock_response.status = 200
        
        mock_session = MagicMock()
        mock_session.request = AsyncMock(return_value=mock_response)
        
        with patch.object(client, 'login') as mock_login:
            with patch.object(client, '_get_session', return_value=mock_session):
                response = await client._authenticated_request('GET', 'http://test/url')
                
                # Should not have called login
                mock_login.assert_not_called()
                # Should have made only 1 request
                assert mock_session.request.call_count == 1
                assert response.status == 200
    
    @pytest.mark.asyncio
    async def test_authenticated_request_fails_after_login_failure(self, client):
        """Test that request fails if re-login fails."""
        mock_response_401 = MagicMock()
        mock_response_401.status = 401
        
        mock_session = MagicMock()
        mock_session.request = AsyncMock(return_value=mock_response_401)
        
        # Mock login to fail
        with patch.object(client, 'login', return_value=False) as mock_login:
            with patch.object(client, '_get_session', return_value=mock_session):
                response = await client._authenticated_request('GET', 'http://test/url')
                
                # Should have tried login
                mock_login.assert_called_once()
                # Response should still be 401
                assert response.status == 401
