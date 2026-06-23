"""Unit tests for cluster sync API."""

import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from bot.models import User
from bot.models.vpn_node import VPNNode, NodeType
from bot.models.cluster import (
    NodeRole,
    NodeState,
    SyncUserRequest,
    HealthStatus,
    VoteRequest,
    VoteResponse,
    TrafficStats,
)
from bot.core.cluster.state import ClusterState
from bot.core.cluster.sync_api import (
    create_sync_api,
    HMACAuth,
    UserSyncPayload,
    ClientConfigPayload,
    SyncUserRequestPayload,
)


class TestHMACAuth:
    """Test HMAC authentication."""
    
    def test_sign_payload(self):
        """Test signing payload."""
        auth = HMACAuth("test-secret")
        
        signature = auth.sign('{"test": "data"}')
        
        assert len(signature) == 64  # SHA256 hex length
        assert all(c in '0123456789abcdef' for c in signature)
    
    def test_verify_valid_signature(self):
        """Test verifying valid signature."""
        auth = HMACAuth("test-secret")
        payload = '{"test": "data"}'
        
        signature = auth.sign(payload)
        
        assert auth.verify(payload, signature) is True
    
    def test_verify_invalid_signature(self):
        """Test verifying invalid signature."""
        auth = HMACAuth("test-secret")
        
        assert auth.verify('{"test": "data"}', "invalid-signature") is False
    
    def test_different_secrets_produce_different_signatures(self):
        """Test that different secrets produce different signatures."""
        auth1 = HMACAuth("secret-1")
        auth2 = HMACAuth("secret-2")
        payload = '{"test": "data"}'
        
        sig1 = auth1.sign(payload)
        sig2 = auth2.sign(payload)
        
        assert sig1 != sig2


class TestSyncAPIHealth:
    """Test health check endpoint."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state."""
        state = ClusterState(node_id="exit-1")
        state.set_state(NodeState.LEADER)
        state.current_term = 5
        return state
    
    @pytest.fixture
    def auth(self):
        """Create HMAC auth."""
        return HMACAuth("test-secret")
    
    @pytest.fixture
    def app(self, cluster_state, auth):
        """Create FastAPI app."""
        return create_sync_api(cluster_state, auth)
    
    @pytest.fixture
    def client(self, app):
        """Create test client."""
        return TestClient(app)
    
    def test_health_check(self, client):
        """Test health check endpoint."""
        response = client.get("/health", headers={"X-Node-ID": "exit-2"})
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["node_id"] == "exit-1"
        assert data["state"] == "leader"
        assert data["term"] == 5
        assert data["is_leader"] is True
        assert "timestamp" in data
    
    def test_health_check_callback(self, client, cluster_state):
        """Test health check with callback."""
        callback = AsyncMock()
        
        from bot.core.cluster.sync_api import create_sync_api
        auth = HMACAuth("test-secret")
        app = create_sync_api(
            cluster_state,
            auth,
            on_get_health=callback,
        )
        
        test_client = TestClient(app)
        response = test_client.get("/health", headers={"X-Node-ID": "exit-2"})
        
        assert response.status_code == 200


class TestSyncAPIUserSync:
    """Test user sync endpoint."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state."""
        return ClusterState(node_id="exit-1")
    
    @pytest.fixture
    def auth(self):
        """Create HMAC auth."""
        return HMACAuth("test-secret")
    
    def test_sync_user_success(self, cluster_state, auth):
        """Test successful user sync."""
        callback = AsyncMock(return_value=True)
        
        from bot.core.cluster.sync_api import create_sync_api
        app = create_sync_api(
            cluster_state,
            auth,
            on_sync_user=callback,
        )
        
        client = TestClient(app)
        
        # Create payload
        payload = {
            "user": {
                "chat_id": "123456789",
                "username": "testuser",
                "uuid": "test-uuid",
                "email": "test@nekovo.ru",
                "status": "active",
                "lang": "ru",
                "platform": "ios",
                "limit_ip": 1,
                "quota_gb": 5.0,
            },
            "client_config": {
                "id": "test-uuid",
                "flow": "xtls-rprx-vision",
                "email": "test@nekovo.ru",
                "limitIp": 1,
                "totalGB": 0,
                "expiryTime": 0,
                "enable": True,
            },
            "source_node_id": "exit-2",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signature": "test-signature",
        }
        
        # Calculate body exactly as FastAPI will receive it
        body = json.dumps(payload, separators=(',', ':'))
        signature = auth.sign(body)
        
        response = client.post(
            "/sync/user",
            content=body.encode(),
            headers={
                "X-Node-ID": "exit-2",
                "X-Signature": signature,
                "Content-Type": "application/json",
            },
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["success"] is True
        assert data["node_id"] == "exit-1"
    
    def test_sync_user_invalid_signature(self, cluster_state, auth):
        """Test user sync with invalid signature."""
        from bot.core.cluster.sync_api import create_sync_api
        app = create_sync_api(cluster_state, auth)
        client = TestClient(app)
        
        response = client.post(
            "/sync/user",
            json={"test": "data"},
            headers={
                "X-Node-ID": "exit-2",
                "X-Signature": "invalid-signature",
            },
        )
        
        assert response.status_code == 401


class TestSyncAPIVote:
    """Test vote endpoint."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state."""
        state = ClusterState(node_id="exit-1")
        state.current_term = 1
        return state
    
    @pytest.fixture
    def auth(self):
        """Create HMAC auth."""
        return HMACAuth("test-secret")
    
    def test_request_vote_granted(self, cluster_state, auth):
        """Test vote request that grants vote."""
        callback = AsyncMock(return_value=VoteResponse(
            term=2,
            vote_granted=True,
            voter_id="exit-1",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        
        from bot.core.cluster.sync_api import create_sync_api
        app = create_sync_api(
            cluster_state,
            auth,
            on_vote_request=callback,
        )
        
        client = TestClient(app)
        
        response = client.post(
            "/vote",
            json={
                "term": 2,
                "candidate_id": "exit-2",
                "last_log_index": 0,
                "last_log_term": 0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            headers={"X-Node-ID": "exit-2"},
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["vote_granted"] is True
        assert data["voter_id"] == "exit-1"
    
    def test_request_vote_denied(self, cluster_state, auth):
        """Test vote request that denies vote."""
        callback = AsyncMock(return_value=VoteResponse(
            term=1,
            vote_granted=False,
            voter_id="exit-1",
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        
        from bot.core.cluster.sync_api import create_sync_api
        app = create_sync_api(
            cluster_state,
            auth,
            on_vote_request=callback,
        )
        
        client = TestClient(app)
        
        response = client.post(
            "/vote",
            json={
                "term": 1,
                "candidate_id": "exit-2",
                "last_log_index": 0,
                "last_log_term": 0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            headers={"X-Node-ID": "exit-2"},
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["vote_granted"] is False


class TestSyncAPITraffic:
    """Test traffic sync endpoint."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state."""
        return ClusterState(node_id="exit-1")
    
    @pytest.fixture
    def auth(self):
        """Create HMAC auth."""
        return HMACAuth("test-secret")
    
    def test_sync_traffic(self, cluster_state, auth):
        """Test traffic statistics sync."""
        callback = AsyncMock()
        
        from bot.core.cluster.sync_api import create_sync_api
        app = create_sync_api(
            cluster_state,
            auth,
            on_traffic_stats=callback,
        )
        
        client = TestClient(app)
        
        payload = {
            "email": "user@nekovo.ru",
            "upload_bytes": 1024,
            "download_bytes": 2048,
            "node_id": "exit-2",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        # Calculate body exactly as FastAPI will receive it
        body = json.dumps(payload, separators=(',', ':'))
        signature = auth.sign(body)
        
        response = client.post(
            "/sync/traffic",
            content=body.encode(),
            headers={
                "X-Node-ID": "exit-2",
                "X-Signature": signature,
                "Content-Type": "application/json",
            },
        )
        
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


class TestSyncAPIClusterStatus:
    """Test cluster status endpoint."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state with nodes."""
        state = ClusterState(node_id="exit-1")
        state.set_state(NodeState.LEADER)
        state.set_leader("exit-1", term=1)
        
        # Add peer node
        state.add_node(VPNNode(
            node_id="exit-2",
            node_type=NodeType.EXIT,
            host="10.0.0.2",
        ))
        
        return state
    
    @pytest.fixture
    def auth(self):
        """Create HMAC auth."""
        return HMACAuth("test-secret")
    
    def test_cluster_status(self, cluster_state, auth):
        """Test getting cluster status."""
        from bot.core.cluster.sync_api import create_sync_api
        app = create_sync_api(cluster_state, auth)
        client = TestClient(app)
        
        response = client.get(
            "/cluster/status",
            headers={"X-Node-ID": "exit-2"},
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["node_id"] == "exit-1"
        assert data["is_leader"] is True
        assert data["leader"]["node_id"] == "exit-1"
        assert "exit-2" in data["nodes"]
        assert "exit-2" in data["exit_nodes"]


class TestSyncAPIErrorHandling:
    """Test error handling."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state."""
        return ClusterState(node_id="exit-1")
    
    @pytest.fixture
    def auth(self):
        """Create HMAC auth."""
        return HMACAuth("test-secret")
    
    def test_missing_headers(self, cluster_state, auth):
        """Test request without required headers."""
        from bot.core.cluster.sync_api import create_sync_api
        app = create_sync_api(cluster_state, auth)
        client = TestClient(app)
        
        # Try health check without X-Node-ID
        response = client.get("/health")
        
        assert response.status_code == 422  # Validation error
