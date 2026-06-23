"""Test Suite: Failover API

Purpose:
    Verify API endpoints, request/response handling, and error cases.

Key Scenarios:
    1. Endpoint responses
    2. Error handling
    3. Callback integration

When to Run:
    - After changes to failover_api.py or failover_schemas.py
    - When adding new endpoints

Dependencies:
    - FastAPI test client
"""

import pytest
from fastapi.testclient import TestClient
from datetime import datetime, timezone

from bot.core.cluster.failover_api import create_failover_api
from bot.models.performance import ExitNodeStatus, FailoverEvent, FailoverDecision


class TestFailoverAPI:
    """Tests for Failover API endpoints."""
    
    @pytest.fixture
    def mock_statuses(self):
        """Mock Exit Node statuses."""
        return {
            "exit-1": ExitNodeStatus(
                node_id="exit-1", is_healthy=True, is_throttled=False,
                performance_score=80, cpu_percent=20.0,
                memory_percent=30.0, connections=10, tier="high",
            ),
            "exit-2": ExitNodeStatus(
                node_id="exit-2", is_healthy=True, is_throttled=True,
                performance_score=20, cpu_percent=50.0,
                memory_percent=40.0, connections=5, tier="limited",
            ),
        }
    
    @pytest.fixture
    def client(self, mock_statuses, monkeypatch):
        """Test client with mocked callbacks."""
        monkeypatch.setenv("BOT_API_SECRET", "test-secret")
        
        def get_statuses():
            return mock_statuses
        
        app = create_failover_api(
            get_exit_statuses=get_statuses,
            on_failover_event=lambda e: {"notification_sent": True, "broadcast_requested": False},
            get_failover_decision=lambda uid: FailoverDecision(
                user_id=uid, decision="failover", target_exit="exit-1",
                reason="test", delay_seconds=0
            ),
            on_broadcast_request=lambda msg, uids: {"sent_count": 5, "failed_count": 0, "message": "sent"},
        )
        return TestClient(app)
    
    def test_health_endpoint(self, client):
        """Test health check endpoint."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
    
    def test_get_all_exit_statuses(self, client):
        """Test getting all exit node statuses."""
        response = client.get("/exit/nodes/status")
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert "exit-1" in data["nodes"]
        assert "exit-2" in data["nodes"]
    
    def test_get_single_exit_status(self, client):
        """Test getting single exit node status."""
        response = client.get("/exit/nodes/exit-1/status")
        assert response.status_code == 200
        data = response.json()
        assert data["node_id"] == "exit-1"
        assert data["is_healthy"] is True
    
    def test_get_exit_status_not_found(self, client):
        """Test 404 for non-existent exit node."""
        response = client.get("/exit/nodes/exit-unknown/status")
        assert response.status_code == 404
    
    def test_report_failover_event(self, client):
        """Test reporting failover event."""
        payload = {
            "user_id": "user1",
            "chat_id": "chat1",
            "from_exit": "exit-1",
            "to_exit": "exit-2",
            "reason": "health_check",
            "is_throttled_target": True,
        }
        response = client.post("/failover/event", json=payload, headers={"X-API-Secret": "test-secret"})
        # 200 if callback configured, 501 if not
        assert response.status_code in [200, 501]
        if response.status_code == 200:
            data = response.json()
            assert data["accepted"] is True
    
    def test_get_failover_decision(self, client):
        """Test getting failover decision."""
        response = client.get("/failover/decision/user1", headers={"X-API-Secret": "test-secret"})
        assert response.status_code == 200
        data = response.json()
        assert data["user_id"] == "user1"
        assert data["decision"] == "failover"
    
    def test_admin_broadcast(self, client):
        """Test admin broadcast endpoint."""
        payload = {"message": "Test message", "user_ids": ["user1", "user2"]}
        response = client.post("/admin/broadcast", json=payload, headers={"X-API-Secret": "test-secret"})
        # 200 if callback configured, 501 if not
        assert response.status_code in [200, 501]
        if response.status_code == 200:
            data = response.json()
            assert data["sent_count"] == 5
    
    def test_exit_recommendation(self, client):
        """Test exit recommendation endpoint."""
        response = client.get("/exit/recommendation")
        assert response.status_code == 200
        data = response.json()
        assert data["recommended"]["node_id"] == "exit-1"  # Higher score
        assert data["reason"] == "best_performance"


class TestFailoverAPIErrors:
    """Tests for error handling."""
    
    def test_failover_decision_service_unavailable(self, monkeypatch):
        """Test 501 when decision service not configured."""
        monkeypatch.setenv("BOT_API_SECRET", "test-secret")
        app = create_failover_api(get_exit_statuses=lambda: {})
        client = TestClient(app)
        
        response = client.get("/failover/decision/user1", headers={"X-API-Secret": "test-secret"})
        assert response.status_code == 501
    
    def test_broadcast_service_unavailable(self, monkeypatch):
        """Test 501 when broadcast service not configured."""
        monkeypatch.setenv("BOT_API_SECRET", "test-secret")
        app = create_failover_api(get_exit_statuses=lambda: {})
        client = TestClient(app)
        
        response = client.post("/admin/broadcast", json={"message": "test"}, headers={"X-API-Secret": "test-secret"})
        assert response.status_code == 501


class TestFailoverAPIAuth:
    """Tests for API authentication (CRIT-01)."""
    
    @pytest.fixture
    def client_with_auth(self, monkeypatch):
        """Test client with auth enabled."""
        monkeypatch.setenv("BOT_API_SECRET", "test-secret-123")
        
        app = create_failover_api(get_exit_statuses=lambda: {})
        return TestClient(app)
    
    def test_protected_endpoint_without_auth_fails(self, client_with_auth):
        """Test that protected endpoints reject requests without auth header."""
        payload = {
            "user_id": "user1",
            "chat_id": "chat1",
            "from_exit": "exit-1",
            "to_exit": "exit-2",
            "reason": "health_check",
            "is_throttled_target": True,
        }
        response = client_with_auth.post("/failover/event", json=payload)
        assert response.status_code == 401
    
    def test_protected_endpoint_with_invalid_auth_fails(self, client_with_auth):
        """Test that protected endpoints reject invalid auth."""
        response = client_with_auth.post(
            "/failover/event",
            json={"user_id": "user1"},
            headers={"X-API-Secret": "wrong-secret"}
        )
        assert response.status_code == 401
    
    def test_protected_endpoint_with_valid_auth_succeeds(self, client_with_auth):
        """Test that protected endpoints accept valid auth."""
        payload = {
            "user_id": "user1",
            "chat_id": "chat1",
            "from_exit": "exit-1",
            "to_exit": "exit-2",
            "reason": "health_check",
            "is_throttled_target": True,
        }
        response = client_with_auth.post(
            "/failover/event",
            json=payload,
            headers={"X-API-Secret": "test-secret-123"}
        )
        # Should pass auth (actual processing may fail due to missing callback)
        assert response.status_code in [200, 501]  # 501 = service not configured
    
    def test_health_endpoint_is_public(self, client_with_auth):
        """Test that health endpoint is accessible without auth."""
        response = client_with_auth.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
    
    def test_exit_nodes_status_is_public(self, client_with_auth):
        """Test that exit nodes status is accessible without auth."""
        response = client_with_auth.get("/exit/nodes/status")
        assert response.status_code == 200
