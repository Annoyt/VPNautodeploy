"""Integration tests for XUI API client.

Tests cover:
1. API connection failures
2. Inbound operations (get_inbounds, get_inbound, update_inbound)
3. Client operations (add_client, get_client_traffic, get_all_clients_stats)
4. Stats operations (get_online_clients, get_all_clients_stats)
5. Error handling and retry logic (401 re-authentication)
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from typing import Dict, Any

import pytest
import aiohttp

from bot.services.xui_api.client import XUIAPIClient, XUIClientConfig


def _create_async_context_mock(response):
    """Helper to create a proper async context manager mock."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _create_mock_response(status=200, json_data=None, text=""):
    """Helper to create mock response."""
    mock_response = MagicMock()
    mock_response.status = status
    mock_response.json = AsyncMock(return_value=json_data or {})
    mock_response.text = AsyncMock(return_value=text)
    return mock_response


class TestXUIClientConfig:
    """Tests for XUIClientConfig dataclass."""

    def test_config_repr_masks_password(self):
        """Password should be masked in repr output."""
        config = XUIClientConfig(
            base_url="http://localhost:2026",
            username="admin",
            password="secret123"
        )
        repr_str = repr(config)
        assert "secret123" not in repr_str
        assert "password='***'" in repr_str

    def test_config_defaults(self):
        """Test default configuration values."""
        config = XUIClientConfig()
        assert config.base_url == "http://127.0.0.1:2026"
        assert config.username == "admin"
        assert config.password == "admin"


class TestXUIAPIClientConnection:
    """Tests for API connection and authentication."""

    @pytest.fixture
    def client_config(self):
        return XUIClientConfig(
            base_url="http://test.xui.example:2053",
            username="test_admin",
            password="test_pass"
        )

    @pytest.fixture
    def client(self, client_config):
        return XUIAPIClient(client_config)

    @pytest.mark.asyncio
    async def test_login_success(self, client):
        csrf_response = _create_mock_response(
            text='<html><meta name="csrf-token" content="test-csrf-123" /></html>'
        )
        login_response = _create_mock_response(json_data={"success": True})

        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=_create_async_context_mock(csrf_response))
        mock_session.post = MagicMock(return_value=_create_async_context_mock(login_response))

        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.login()

        assert result is True
        assert client._csrf_token == "test-csrf-123"


class TestXUIAPIClientInbounds:
    """Tests for inbound operations."""

    @pytest.fixture
    def client(self):
        config = XUIClientConfig(base_url="http://test:2026")
        return XUIAPIClient(config)

    @pytest.mark.asyncio
    async def test_get_inbounds_success(self, client):
        mock_response = _create_mock_response(json_data={
            "obj": [
                {"id": 1, "port": 443, "protocol": "vmess"},
                {"id": 2, "port": 8443, "protocol": "vless"}
            ]
        })

        with patch.object(client, '_authenticated_request', return_value=mock_response):
            inbounds = await client.get_inbounds()

        assert len(inbounds) == 2
        assert inbounds[0]["port"] == 443

    @pytest.mark.asyncio
    async def test_get_inbound_by_id(self, client):
        mock_response = _create_mock_response(json_data={
            "obj": {"id": 1, "port": 443, "protocol": "vmess"}
        })

        with patch.object(client, '_authenticated_request', return_value=mock_response):
            inbound = await client.get_inbound(1)

        assert inbound["id"] == 1

    @pytest.mark.asyncio
    async def test_update_inbound_success(self, client):
        mock_response = _create_mock_response(json_data={"success": True})

        with patch.object(client, '_authenticated_request', return_value=mock_response):
            result = await client.update_inbound(1, {"port": 444})

        assert result is True


class TestXUIAPIClientClientOperations:
    """Tests for client operations."""

    @pytest.fixture
    def client(self):
        config = XUIClientConfig(base_url="http://test:2026")
        return XUIAPIClient(config)

    @pytest.mark.asyncio
    async def test_add_client_success(self, client):
        mock_response = _create_mock_response(json_data={"success": True})

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=_create_async_context_mock(mock_response))

        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.add_client(
                inbound_ids=1,
                client_config={"email": "test@example.com", "id": "abc-123"}
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_update_client_posts_flat_body_with_inbound_ids(self, client):
        """The update endpoint reads a FLAT camelCase client body plus
        inboundIds. Nesting it under "client" fails with "client email is
        required", and snake_case total_gb is ignored — which silently
        resets the quota to unlimited. Both verified on the live panel."""
        mock_response = _create_mock_response(json_data={"success": True})
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=_create_async_context_mock(mock_response))

        with patch.object(client, '_get_session', return_value=mock_session), \
                patch.object(client, '_ensure_auth', new=AsyncMock(return_value=True)):
            result = await client.update_client(
                "user_bob_42@nekovo.ru",
                {"email": "user_bob_42@nekovo.ru", "id": "abc-123",
                 "totalGB": 500, "comment": "bob@gmail.com"},
                [1, 4, 5, 6],
            )

        assert result is True
        url = mock_session.post.call_args[0][0]
        body = mock_session.post.call_args[1]["json"]
        assert url.endswith("/panel/api/clients/update/user_bob_42%40nekovo.ru")
        assert "client" not in body
        assert body["totalGB"] == 500
        assert body["comment"] == "bob@gmail.com"
        assert body["inboundIds"] == [1, 4, 5, 6]

    @pytest.mark.asyncio
    async def test_reset_client_traffic_posts_to_fork_route(self, client):
        """Traffic reset goes through the fork's relational client API
        (/panel/api/clients/resetTraffic/{email}), email URL-escaped."""
        mock_response = _create_mock_response(json_data={"success": True})
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=_create_async_context_mock(mock_response))

        with patch.object(client, '_get_session', return_value=mock_session), \
                patch.object(client, '_ensure_auth', new=AsyncMock(return_value=True)):
            result = await client.reset_client_traffic("user_bob_42@nekovo.ru")

        assert result is True
        url = mock_session.post.call_args[0][0]
        assert url.endswith(
            "/panel/api/clients/resetTraffic/user_bob_42%40nekovo.ru")

    @pytest.mark.asyncio
    async def test_reset_client_traffic_reports_panel_failure(self, client):
        mock_response = _create_mock_response(
            json_data={"success": False, "msg": "record not found"})
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=_create_async_context_mock(mock_response))

        with patch.object(client, '_get_session', return_value=mock_session), \
                patch.object(client, '_ensure_auth', new=AsyncMock(return_value=True)):
            assert await client.reset_client_traffic("nope@x") is False

    @pytest.mark.asyncio
    async def test_update_client_reports_panel_failure(self, client):
        mock_response = _create_mock_response(
            json_data={"success": False, "msg": "record not found"})
        mock_session = MagicMock()
        mock_session.post = MagicMock(
            return_value=_create_async_context_mock(mock_response))

        with patch.object(client, '_get_session', return_value=mock_session), \
                patch.object(client, '_ensure_auth', new=AsyncMock(return_value=True)):
            result = await client.update_client("nope@x", {"email": "nope@x"}, 1)

        assert result is False

    @pytest.mark.asyncio
    async def test_get_client_traffic_success(self, client):
        mock_response = _create_mock_response(json_data={
            "obj": [{
                "id": 1,
                "clientStats": [
                    {"email": "test@example.com", "up": 1000, "down": 2000}
                ]
            }]
        })

        with patch.object(client, '_authenticated_request', return_value=mock_response):
            traffic = await client.get_client_traffic("test@example.com")

        assert traffic["email"] == "test@example.com"
        assert traffic["up"] == 1000

    @pytest.mark.asyncio
    async def test_get_all_clients_stats_success(self, client):
        mock_response = _create_mock_response(json_data={
            "obj": [{
                "id": 1,
                "clientStats": [
                    {"email": "user1@example.com", "up": 1000, "down": 2000},
                    {"email": "user2@example.com", "up": 500, "down": 1000}
                ]
            }]
        })

        with patch.object(client, '_authenticated_request', return_value=mock_response):
            stats = await client.get_all_clients_stats()

        assert isinstance(stats, dict)
        assert len(stats) == 2


class TestXUIAPIClientStats:
    """Tests for stats operations."""

    @pytest.fixture
    def client(self):
        config = XUIClientConfig(base_url="http://test:2026")
        return XUIAPIClient(config)

    @pytest.mark.asyncio
    async def test_get_online_clients_success(self, client):
        client._csrf_token = "test-token"
        mock_response = _create_mock_response(json_data={
            "success": True,
            "obj": ["user1@example.com", "user2@example.com"]
        })

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=_create_async_context_mock(mock_response))

        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.get_online_clients()

        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_get_online_clients_lazy_login(self, client):
        client._csrf_token = None
        mock_response = _create_mock_response(json_data={
            "success": True,
            "obj": ["user@example.com"]
        })

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=_create_async_context_mock(mock_response))

        with patch.object(client, 'login', return_value=True):
            with patch.object(client, '_get_session', return_value=mock_session):
                result = await client.get_online_clients()

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_online_clients_401_reauth(self, client):
        client._csrf_token = "expired-token"

        mock_response_401 = _create_mock_response(status=401)
        mock_response_ok = _create_mock_response(json_data={
            "success": True,
            "obj": ["user@example.com"]
        })

        mock_session = MagicMock()
        mock_session.post = MagicMock(side_effect=[
            _create_async_context_mock(mock_response_401),
            _create_async_context_mock(mock_response_ok),
        ])

        with patch.object(client, 'login', return_value=True):
            with patch.object(client, '_get_session', return_value=mock_session):
                result = await client.get_online_clients()

        assert len(result) == 1


class TestXUIAPIClientContextManager:
    """Tests for async context manager."""

    @pytest.fixture
    def client(self):
        config = XUIClientConfig(base_url="http://test:2026")
        return XUIAPIClient(config)

    @pytest.mark.asyncio
    async def test_context_manager_enters_and_exits(self, client):
        async with client as c:
            assert c is client
        # After exit, session should be closed (or None)
        assert client.session is None or client.session.closed


class TestXUIClientSyncWrapper:
    """Tests for sync wrapper methods."""

    @pytest.fixture
    def client(self):
        config = XUIClientConfig(base_url="http://test:2026")
        return XUIAPIClient(config)

    def test_get_online_clients_sync_from_sync_context(self, client):
        with patch('asyncio.run') as mock_run:
            mock_run.return_value = ["user@example.com"]
            result = client.get_online_clients_sync()
            assert result == ["user@example.com"]


class TestXUIAPIClientEdgeCases:
    """Tests for edge cases and error handling."""

    @pytest.fixture
    def client(self):
        config = XUIClientConfig(base_url="http://test:2026")
        return XUIAPIClient(config)

    @pytest.mark.asyncio
    async def test_get_online_clients_filters_non_strings(self, client):
        client._csrf_token = "test-token"
        mock_response = _create_mock_response(json_data={
            "success": True,
            "obj": ["valid@example.com", 123, None, b"bytes@example.com"]
        })

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=_create_async_context_mock(mock_response))

        with patch.object(client, '_get_session', return_value=mock_session):
            result = await client.get_online_clients()

        # Should filter to strings only
        assert "valid@example.com" in result
        assert 123 not in result
