"""Unit tests for NodeSyncClient."""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import aiohttp
import aiohttp.web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from bot.models import User
from bot.models.vpn_node import VPNNode, NodeType
from bot.models.cluster import (
    NodeRole,
    SyncUserRequest,
    HealthStatus,
    VoteRequest,
    TrafficStats,
)
from bot.core.cluster.sync_client import NodeSyncClient


class TestNodeSyncClientBasics:
    """Test basic NodeSyncClient operations."""
    
    @pytest.fixture
    def client(self):
        """Create sync client."""
        return NodeSyncClient(
            node_id="exit-1",
            secret="test-secret",
            timeout=5.0,
        )
    
    @pytest.mark.asyncio
    async def test_start_stop(self, client):
        """Test start and stop lifecycle."""
        await client.start()
        assert client.session is not None
        assert not client.session.closed
        
        await client.stop()
        assert client.session is None  # Session is set to None after close
    
    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager."""
        async with NodeSyncClient("exit-1", "test-secret") as client:
            assert client.session is not None
            assert not client.session.closed
        
        assert client.session is None  # Session is set to None after close
    
    def test_add_peer(self, client):
        """Test adding peer."""
        peer = VPNNode(base_path="", 
            node_id="exit-2",
            node_type=NodeType.EXIT,
            host="10.0.0.2",
            api_port=8081,
        )
        
        client.add_peer(peer)
        
        assert "exit-2" in client.peers
        assert client.get_peer("exit-2") == peer
    
    def test_remove_peer(self, client):
        """Test removing peer."""
        peer = VPNNode(base_path="", 
            node_id="exit-2",
            node_type=NodeType.EXIT,
            host="10.0.0.2",
        )
        client.add_peer(peer)
        
        result = client.remove_peer("exit-2")
        
        assert result is True
        assert "exit-2" not in client.peers
    
    def test_get_all_peers(self, client):
        """Test getting all peers."""
        peer1 = VPNNode(base_path="", node_id="exit-2", node_type=NodeType.EXIT, host="10.0.0.2")
        peer2 = VPNNode(base_path="", node_id="exit-3", node_type=NodeType.EXIT, host="10.0.0.3")
        
        client.add_peer(peer1)
        client.add_peer(peer2)
        
        peers = client.get_all_peers()
        
        assert len(peers) == 2
        assert peer1 in peers
        assert peer2 in peers


class TestNodeSyncClientCircuitBreaker:
    """Test circuit breaker functionality."""
    
    @pytest.fixture
    def client(self):
        """Create sync client with low threshold."""
        client = NodeSyncClient(
            node_id="exit-1",
            secret="test-secret",
        )
        client._circuit_threshold = 2  # Lower for testing
        client._circuit_timeout = 1  # 1 second for testing
        return client
    
    def test_circuit_starts_closed(self, client):
        """Test that circuit starts closed."""
        assert client._is_circuit_open("exit-2") is False
    
    def test_circuit_opens_after_failures(self, client):
        """Test that circuit opens after threshold failures."""
        client._record_failure("exit-2")
        assert client._is_circuit_open("exit-2") is False
        
        client._record_failure("exit-2")
        assert client._is_circuit_open("exit-2") is True
    
    def test_circuit_closes_after_timeout(self, client):
        """Test that circuit closes after timeout."""
        # Open circuit
        client._record_failure("exit-2")
        client._record_failure("exit-2")
        assert client._is_circuit_open("exit-2") is True
        
        # Wait for timeout
        import time
        time.sleep(1.1)
        
        # Circuit should be closed now
        assert client._is_circuit_open("exit-2") is False
    
    def test_success_resets_failure_count(self, client):
        """Test that success resets failure counter."""
        client._record_failure("exit-2")
        client._record_success("exit-2")
        
        # One more failure should not open circuit
        client._record_failure("exit-2")
        assert client._is_circuit_open("exit-2") is False


class TestNodeSyncClientSigning:
    """Test request signing."""
    
    @pytest.fixture
    def client(self):
        """Create sync client."""
        return NodeSyncClient("exit-1", "test-secret")
    
    def test_sign_request(self, client):
        """Test request signing."""
        payload = {"test": "data", "number": 123}
        
        signature = client._sign_request(payload)
        
        assert len(signature) == 64  # SHA256 hex
        assert all(c in '0123456789abcdef' for c in signature)
    
    def test_sign_request_deterministic(self, client):
        """Test that signing is deterministic."""
        payload = {"test": "data"}
        
        sig1 = client._sign_request(payload)
        sig2 = client._sign_request(payload)
        
        assert sig1 == sig2
    
    def test_different_payloads_different_signatures(self, client):
        """Test that different payloads produce different signatures."""
        sig1 = client._sign_request({"test": 1})
        sig2 = client._sign_request({"test": 2})
        
        assert sig1 != sig2


@pytest.mark.asyncio
class TestNodeSyncClientIntegration:
    """Integration tests with mock server."""
    
    async def test_sync_user_success(self):
        """Test successful user sync."""
        from aiohttp import web
        
        async def handler(request):
            return web.json_response({
                "success": True,
                "node_id": "exit-2",
                "message": "User synced",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        
        app = web.Application()
        app.router.add_post("/sync/user", handler)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 18081)
        await site.start()
        
        try:
            client = NodeSyncClient("exit-1", "test-secret")
            client.add_peer(VPNNode(base_path="", 
                node_id="exit-2",
                node_type=NodeType.EXIT,
                host="127.0.0.1",
                api_port=18081,
            ))
            
            await client.start()
            
            user = User(
                chat_id="123456789",
                username="testuser",
                email="test@nekovo.ru",
            )
            request = SyncUserRequest(
                user=user,
                client_config={"id": "test-uuid"},
                source_node_id="exit-1",
                timestamp=datetime.now(timezone.utc).isoformat(),
                signature="test-sig",
            )
            
            response = await client.sync_user("exit-2", request)
            
            assert response is not None
            assert response.success is True
            assert response.node_id == "exit-2"
            
            await client.stop()
        finally:
            await runner.cleanup()
    
    async def test_get_health_success(self):
        """Test successful health check."""
        from aiohttp import web
        
        async def handler(request):
            return web.json_response({
                "node_id": "exit-2",
                "state": "leader",
                "term": 5,
                "is_leader": True,
                "last_heartbeat": "",
                "db_status": True,
                "xui_status": True,
                "uptime_seconds": 3600,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        
        app = web.Application()
        app.router.add_get("/health", handler)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 18082)
        await site.start()
        
        try:
            client = NodeSyncClient("exit-1", "test-secret")
            client.add_peer(VPNNode(base_path="", 
                node_id="exit-2",
                node_type=NodeType.EXIT,
                host="127.0.0.1",
                api_port=18082,
            ))
            
            await client.start()
            
            health = await client.get_health("exit-2")
            
            assert health is not None
            assert health.node_id == "exit-2"
            assert health.is_leader is True
            assert health.term == 5
            
            await client.stop()
        finally:
            await runner.cleanup()
    
    async def test_request_vote_success(self):
        """Test successful vote request."""
        from aiohttp import web
        
        async def handler(request):
            return web.json_response({
                "term": 2,
                "vote_granted": True,
                "voter_id": "exit-2",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        
        app = web.Application()
        app.router.add_post("/vote", handler)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 18083)
        await site.start()
        
        try:
            client = NodeSyncClient("exit-1", "test-secret")
            client.add_peer(VPNNode(base_path="", 
                node_id="exit-2",
                node_type=NodeType.EXIT,
                host="127.0.0.1",
                api_port=18083,
            ))
            
            await client.start()
            
            request = VoteRequest(
                term=2,
                candidate_id="exit-1",
                last_log_index=0,
                last_log_term=0,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            
            response = await client.request_vote("exit-2", request)
            
            assert response is not None
            assert response.vote_granted is True
            assert response.voter_id == "exit-2"
            
            await client.stop()
        finally:
            await runner.cleanup()
    
    async def test_unknown_peer_returns_none(self):
        """Test that unknown peer returns None."""
        client = NodeSyncClient("exit-1", "test-secret")
        await client.start()
        
        user = User(chat_id="123", email="test@nekovo.ru")
        request = SyncUserRequest(
            user=user,
            client_config={},
            source_node_id="exit-1",
            timestamp=datetime.now(timezone.utc).isoformat(),
            signature="",
        )
        
        response = await client.sync_user("unknown-peer", request)
        
        assert response is None
        
        await client.stop()
    
    async def test_server_error_returns_none(self):
        """Test that server error returns None."""
        from aiohttp import web
        
        async def handler(request):
            return web.json_response({"error": "Server error"}, status=500)
        
        app = web.Application()
        app.router.add_post("/sync/user", handler)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 18084)
        await site.start()
        
        try:
            client = NodeSyncClient("exit-1", "test-secret")
            client.add_peer(VPNNode(base_path="", 
                node_id="exit-2",
                node_type=NodeType.EXIT,
                host="127.0.0.1",
                api_port=18084,
            ))
            
            await client.start()
            
            user = User(chat_id="123", email="test@nekovo.ru")
            request = SyncUserRequest(
                user=user,
                client_config={},
                source_node_id="exit-1",
                timestamp=datetime.now(timezone.utc).isoformat(),
                signature="",
            )
            
            response = await client.sync_user("exit-2", request)
            
            # Should return None on error
            assert response is None
            
            await client.stop()
        finally:
            await runner.cleanup()


class TestNodeSyncClientEdgeCases:
    """Test edge cases and potential bugs."""
    
    @pytest.mark.asyncio
    async def test_max_retries_parameter_used(self):
        """max_retries parameter should control retry attempts."""
        client = NodeSyncClient("exit-1", "secret", max_retries=2)
        await client.start()
        
        peer = VPNNode(base_path="", 
            node_id="exit-2",
            node_type=NodeType.EXIT,
            host="10.0.0.2",
            api_port=8081,
        )
        client.add_peer(peer)
        
        # Mock session and response to always fail
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = aiohttp.ClientError("fail")
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_response)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        fake_session = MagicMock()
        fake_session.request = MagicMock(return_value=mock_cm)
        fake_session.closed = False
        fake_session.close = AsyncMock()
        client.session = fake_session
        
        with pytest.raises(aiohttp.ClientError):
            await client._request(peer, "GET", "/health")
        
        # Should retry max_retries times (2 attempts)
        assert fake_session.request.call_count == 2
        
        await client.stop()
    
    @pytest.mark.asyncio
    async def test_sync_user_to_all_no_peers(self):
        """Test sync_user_to_all with no peers returns empty dict."""
        client = NodeSyncClient("exit-1", "test-secret")
        user = User(chat_id="123", email="test@nekovo.ru")
        request = SyncUserRequest(
            user=user,
            client_config={},
            source_node_id="exit-1",
            timestamp=datetime.now(timezone.utc).isoformat(),
            signature="",
        )
        results = await client.sync_user_to_all(request)
        assert results == {}
    
    def test_record_failure_opens_circuit_for_unknown_peer(self):
        """Test circuit breaker tracks failures even for unknown peers."""
        client = NodeSyncClient("exit-1", "test-secret")
        client._circuit_threshold = 2
        client._record_failure("unknown")
        client._record_failure("unknown")
        assert client._is_circuit_open("unknown") is True
    
    def test_remove_peer_clears_circuit_state(self):
        """Test remove_peer clears failure and circuit state."""
        client = NodeSyncClient("exit-1", "test-secret")
        peer = VPNNode(base_path="", node_id="exit-2", node_type=NodeType.EXIT, host="10.0.0.2")
        client.add_peer(peer)
        client._record_failure("exit-2")
        client._record_failure("exit-2")
        client._circuit_open_until["exit-2"] = 9999999999
        
        client.remove_peer("exit-2")
        
        assert "exit-2" not in client.peers
        assert "exit-2" not in client._failed_peers
        assert "exit-2" not in client._circuit_open_until
    
    def test_prepare_headers_signature(self):
        """Test _prepare_headers generates correct signature."""
        client = NodeSyncClient("exit-1", "test-secret")
        body = '{"action":"test"}'
        headers = client._prepare_headers(body)
        
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Node-ID"] == "exit-1"
        assert len(headers["X-Signature"]) == 64
        
        # Signature should be deterministic for same body
        headers2 = client._prepare_headers(body)
        assert headers["X-Signature"] == headers2["X-Signature"]
    
    @pytest.mark.asyncio
    async def test_request_without_session_raises(self):
        """Test _request raises RuntimeError when session not started."""
        client = NodeSyncClient("exit-1", "test-secret")
        peer = VPNNode(base_path="", node_id="exit-2", node_type=NodeType.EXIT, host="127.0.0.1", api_port=8080)
        client.add_peer(peer)
        
        with pytest.raises(RuntimeError, match="HTTP session not initialized"):
            await client._request(peer, "GET", "/health")
