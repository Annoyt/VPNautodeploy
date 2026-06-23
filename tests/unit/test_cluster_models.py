"""Unit tests for cluster synchronization models."""

import pytest
from datetime import datetime, timezone

from bot.models import User
from bot.models.vpn_node import VPNNode, NodeType
from bot.models.cluster import (
    LeaderInfo,
    NodeState,
    NodeRole,
    SyncUserRequest,
    SyncUserResponse,
    HealthStatus,
    VoteRequest,
    VoteResponse,
    TrafficStats,
    AggregatedTraffic,
    FailoverRequest,
)


class TestVPNNode:
    """Test VPNNode model."""
    
    def test_create_exit_node(self):
        """Test creating Exit node."""
        node = VPNNode(
            node_id="exit-frankfurt-1",
            node_type=NodeType.EXIT,
            host="203.0.113.30",
            api_port=8081,
            vpn_port=443,
            public_key="test_key",
            sni="www.microsoft.com",
            region="eu",
            city="Frankfurt",
        )
        
        assert node.node_id == "exit-frankfurt-1"
        assert node.role == NodeRole.EXIT
        assert node.api_url == "http://203.0.113.30:8081/this_is_fine"
        assert node.vpn_endpoint == "203.0.113.30:443"
        assert node.is_primary is False
        assert node.weight == 100
    
    def test_create_entry_node(self):
        """Test creating Entry node."""
        node = VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
            api_port=8081,
            region="ru",
            city="Moscow",
        )
        
        assert node.node_id == "entry-moscow-1"
        assert node.role == NodeRole.ENTRY
        assert node.vpn_endpoint is None  # Entry nodes don't have VPN endpoint
    
    def test_to_dict(self):
        """Test serialization to dict."""
        node = VPNNode(
            node_id="exit-test",
            node_type=NodeType.EXIT,
            host="127.0.0.1",
        )
        
        data = node.to_dict()
        
        assert data['node_id'] == "exit-test"
        assert data['node_type'] == "exit"
        assert data['host'] == "127.0.0.1"
        assert 'api_port' in data
    
    def test_from_dict(self):
        """Test deserialization from dict."""
        data = {
            'node_id': 'exit-test',
            'node_type': 'exit',
            'host': '127.0.0.1',
            'api_port': 8081,
            'vpn_port': 443,
            'region': 'eu',
            'is_primary': True,
        }
        
        node = VPNNode.from_dict(data)
        
        assert node.node_id == "exit-test"
        assert node.role == NodeRole.EXIT
        assert node.is_primary is True


class TestSyncUserRequest:
    """Test SyncUserRequest model."""
    
    @pytest.fixture
    def sample_user(self):
        """Create sample user for tests."""
        return User(
            chat_id="123456789",
            username="testuser",
            uuid="test-uuid-123",
            email="test@nekovo.ru",
            status="active",
        )
    
    def test_create_and_sign(self, sample_user):
        """Test creating and signing sync request."""
        secret = "test-secret"
        client_config = {"id": "test-uuid", "flow": "xtls-rprx-vision"}
        
        request = SyncUserRequest.create(
            user=sample_user,
            client_config=client_config,
            source_node_id="exit-1",
            secret=secret,
        )
        
        assert request.user.chat_id == "123456789"
        assert request.source_node_id == "exit-1"
        assert request.signature != ""
        assert len(request.signature) == 64  # SHA256 hex length
    
    def test_verify_valid_signature(self, sample_user):
        """Test verifying valid signature."""
        secret = "test-secret"
        
        request = SyncUserRequest.create(
            user=sample_user,
            client_config={},
            source_node_id="exit-1",
            secret=secret,
        )
        
        assert request.verify(secret) is True
    
    def test_verify_invalid_signature(self, sample_user):
        """Test verifying invalid signature."""
        secret = "test-secret"
        wrong_secret = "wrong-secret"
        
        request = SyncUserRequest.create(
            user=sample_user,
            client_config={},
            source_node_id="exit-1",
            secret=secret,
        )
        
        assert request.verify(wrong_secret) is False
    
    def test_tampered_data_fails_verification(self, sample_user):
        """Test that tampered data fails verification."""
        secret = "test-secret"
        
        request = SyncUserRequest.create(
            user=sample_user,
            client_config={},
            source_node_id="exit-1",
            secret=secret,
        )
        
        # Tamper with the data
        request.user.status = "blocked"
        
        # Verification should fail because signature doesn't match tampered data
        assert request.verify(secret) is False


class TestHealthStatus:
    """Test HealthStatus model."""
    
    def test_create_health_status(self):
        """Test creating health status."""
        status = HealthStatus(
            node_id="exit-1",
            state=NodeState.LEADER,
            term=5,
            is_leader=True,
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
            db_status=True,
            xui_status=True,
            uptime_seconds=3600,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        assert status.node_id == "exit-1"
        assert status.state == NodeState.LEADER
        assert status.is_leader is True
        assert status.db_status is True
    
    def test_serialization(self):
        """Test serialization/deserialization."""
        status = HealthStatus(
            node_id="exit-1",
            state=NodeState.FOLLOWER,
            term=3,
            is_leader=False,
            last_heartbeat="2024-01-01T00:00:00",
            db_status=True,
            xui_status=False,
            uptime_seconds=100,
            timestamp="2024-01-01T00:00:00",
        )
        
        data = status.to_dict()
        restored = HealthStatus.from_dict(data)
        
        assert restored.node_id == status.node_id
        assert restored.state == status.state
        assert restored.xui_status is False


class TestVoteRequest:
    """Test VoteRequest model."""
    
    def test_vote_request(self):
        """Test vote request creation."""
        request = VoteRequest(
            term=5,
            candidate_id="exit-1",
            last_log_index=100,
            last_log_term=4,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        assert request.term == 5
        assert request.candidate_id == "exit-1"
        assert request.last_log_index == 100


class TestVoteResponse:
    """Test VoteResponse model."""
    
    def test_vote_granted(self):
        """Test vote granted response."""
        response = VoteResponse(
            term=5,
            vote_granted=True,
            voter_id="exit-2",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        assert response.vote_granted is True
        assert response.voter_id == "exit-2"
    
    def test_vote_denied(self):
        """Test vote denied response."""
        response = VoteResponse(
            term=5,
            vote_granted=False,
            voter_id="exit-2",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        assert response.vote_granted is False


class TestTrafficStats:
    """Test TrafficStats model."""
    
    def test_traffic_calculation(self):
        """Test total traffic calculation."""
        stats = TrafficStats(
            email="user@nekovo.ru",
            upload_bytes=1024,
            download_bytes=2048,
            node_id="exit-1",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        assert stats.total_bytes == 3072
    
    def test_serialization(self):
        """Test traffic stats serialization."""
        stats = TrafficStats(
            email="user@nekovo.ru",
            upload_bytes=1000,
            download_bytes=2000,
            node_id="exit-1",
            timestamp="2024-01-01T00:00:00",
        )
        
        data = stats.to_dict()
        
        assert data['email'] == "user@nekovo.ru"
        assert data['upload_bytes'] == 1000
        assert data['download_bytes'] == 2000


class TestAggregatedTraffic:
    """Test AggregatedTraffic model."""
    
    def test_aggregation(self):
        """Test traffic aggregation."""
        aggregated = AggregatedTraffic(
            email="user@nekovo.ru",
            total_upload=2048,
            total_download=4096,
            by_node={
                'exit-1': {'upload': 1024, 'download': 2048},
                'exit-2': {'upload': 1024, 'download': 2048},
            },
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        assert aggregated.total_bytes == 6144
        assert len(aggregated.by_node) == 2
        assert aggregated.by_node['exit-1']['upload'] == 1024


class TestLeaderInfo:
    """Test LeaderInfo model."""
    
    def test_leader_info(self):
        """Test leader info creation."""
        info = LeaderInfo(
            node_id="exit-1",
            term=5,
            elected_at="2024-01-01T00:00:00",
        )
        
        assert info.node_id == "exit-1"
        assert info.term == 5
    
    def test_serialization(self):
        """Test serialization."""
        info = LeaderInfo(
            node_id="exit-1",
            term=5,
            elected_at="2024-01-01T00:00:00",
        )
        
        data = info.to_dict()
        restored = LeaderInfo.from_dict(data)
        
        assert restored.node_id == info.node_id
        assert restored.term == info.term


class TestFailoverRequest:
    """Test FailoverRequest model."""
    
    def test_failover_request(self):
        """Test failover request creation."""
        request = FailoverRequest(
            chat_id="123456789",
            from_node_id="exit-1",
            to_node_id="exit-2",
            reason="node_down",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        assert request.chat_id == "123456789"
        assert request.from_node_id == "exit-1"
        assert request.to_node_id == "exit-2"
        assert request.reason == "node_down"
