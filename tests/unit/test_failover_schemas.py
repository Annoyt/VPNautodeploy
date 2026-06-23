"""Tests for failover API schemas (Pydantic models)."""

import pytest
from pydantic import ValidationError

from bot.core.cluster.failover_schemas import (
    ExitNodeStatusResponse,
    AllExitNodesStatusResponse,
    FailoverEventRequest,
    FailoverEventResponse,
    FailoverDecisionResponse,
    BroadcastRequest,
    BroadcastResponse,
)


class TestExitNodeStatusResponse:
    """Test ExitNodeStatusResponse schema."""
    
    def test_valid_status_response(self):
        """Test creating valid ExitNodeStatusResponse."""
        response = ExitNodeStatusResponse(
            node_id="exit-1",
            is_healthy=True,
            is_throttled=False,
            is_preferred=True,
            is_available=True,
            performance_score=95,
            cpu_percent=25.5,
            memory_percent=60.0,
            connections=150,
            tier="HIGH",
            timestamp="2026-04-13T10:00:00Z"
        )
        assert response.node_id == "exit-1"
        assert response.is_healthy is True
        assert response.performance_score == 95
        
    def test_throttled_node_response(self):
        """Test status response for throttled node."""
        response = ExitNodeStatusResponse(
            node_id="exit-2",
            is_healthy=True,
            is_throttled=True,
            is_preferred=False,
            is_available=True,
            performance_score=40,
            cpu_percent=85.0,
            memory_percent=70.0,
            connections=300,
            tier="LIMITED",
            timestamp="2026-04-13T10:00:00Z"
        )
        assert response.is_throttled is True
        assert response.performance_score == 40
        
    def test_invalid_performance_score_type(self):
        """Test validation rejects invalid performance_score type."""
        with pytest.raises(ValidationError):
            ExitNodeStatusResponse(
                node_id="exit-1",
                is_healthy=True,
                is_throttled=False,
                is_preferred=True,
                is_available=True,
                performance_score="high",  # Should be int
                cpu_percent=25.5,
                memory_percent=60.0,
                connections=150,
                tier="HIGH",
                timestamp="2026-04-13T10:00:00Z"
            )


class TestAllExitNodesStatusResponse:
    """Test AllExitNodesStatusResponse schema."""
    
    def test_valid_multiple_nodes(self):
        """Test response with multiple nodes."""
        response = AllExitNodesStatusResponse(
            nodes={
                "exit-1": ExitNodeStatusResponse(
                    node_id="exit-1",
                    is_healthy=True,
                    is_throttled=False,
                    is_preferred=True,
                    is_available=True,
                    performance_score=95,
                    cpu_percent=25.5,
                    memory_percent=60.0,
                    connections=150,
                    tier="HIGH",
                    timestamp="2026-04-13T10:00:00Z"
                ),
                "exit-2": ExitNodeStatusResponse(
                    node_id="exit-2",
                    is_healthy=True,
                    is_throttled=True,
                    is_preferred=False,
                    is_available=True,
                    performance_score=40,
                    cpu_percent=85.0,
                    memory_percent=70.0,
                    connections=300,
                    tier="LIMITED",
                    timestamp="2026-04-13T10:00:00Z"
                )
            },
            timestamp="2026-04-13T10:00:00Z"
        )
        assert len(response.nodes) == 2
        assert "exit-1" in response.nodes
        assert "exit-2" in response.nodes
        
    def test_empty_nodes(self):
        """Test response with no nodes."""
        response = AllExitNodesStatusResponse(
            nodes={},
            timestamp="2026-04-13T10:00:00Z"
        )
        assert response.nodes == {}


class TestFailoverEventRequest:
    """Test FailoverEventRequest schema."""
    
    def test_valid_event_request(self):
        """Test creating valid failover event request."""
        request = FailoverEventRequest(
            user_id="user123",
            chat_id="chat456",
            from_exit="exit-1",
            to_exit="exit-2",
            reason="CPU throttled",
            is_throttled_target=True,
            timestamp="2026-04-13T10:00:00Z"
        )
        assert request.user_id == "user123"
        assert request.from_exit == "exit-1"
        assert request.to_exit == "exit-2"
        assert request.is_throttled_target is True
        
    def test_optional_timestamp_defaults(self):
        """Test that timestamp is optional."""
        request = FailoverEventRequest(
            user_id="user123",
            chat_id="chat456",
            from_exit="exit-1",
            to_exit="exit-2",
            reason="Manual failover",
            is_throttled_target=False
        )
        assert request.timestamp is None
        
    def test_required_fields(self):
        """Test that required fields are enforced."""
        with pytest.raises(ValidationError):
            FailoverEventRequest(
                user_id="user123",
                # Missing required fields
            )


class TestFailoverEventResponse:
    """Test FailoverEventResponse schema."""
    
    def test_successful_response(self):
        """Test successful failover event response."""
        response = FailoverEventResponse(
            accepted=True,
            message="Failover accepted",
            notification_sent=True,
            broadcast_requested=False
        )
        assert response.accepted is True
        assert response.notification_sent is True
        
    def test_rejected_response(self):
        """Test rejected failover event response."""
        response = FailoverEventResponse(
            accepted=False,
            message="Failover rejected: cooldown active",
            notification_sent=False,
            broadcast_requested=False
        )
        assert response.accepted is False
        assert response.notification_sent is False


class TestFailoverDecisionResponse:
    """Test FailoverDecisionResponse schema."""
    
    def test_stay_decision(self):
        """Test decision to stay on current node."""
        response = FailoverDecisionResponse(
            user_id="user123",
            decision="STAY",
            target_exit=None,
            reason="Current node is optimal",
            delay_seconds=0
        )
        assert response.decision == "STAY"
        assert response.target_exit is None
        assert response.delay_seconds == 0
        
    def test_failover_decision(self):
        """Test decision to failover."""
        response = FailoverDecisionResponse(
            user_id="user123",
            decision="FAILOVER",
            target_exit="exit-2",
            reason="Better node available",
            delay_seconds=5
        )
        assert response.decision == "FAILOVER"
        assert response.target_exit == "exit-2"
        assert response.delay_seconds == 5
        
    def test_delay_decision(self):
        """Test decision to delay failover."""
        response = FailoverDecisionResponse(
            user_id="user123",
            decision="DELAY",
            target_exit="exit-2",
            reason="Target is throttled, waiting",
            delay_seconds=60
        )
        assert response.decision == "DELAY"
        assert response.delay_seconds == 60
        
    def test_negative_delay_rejected(self):
        """Test that negative delay_seconds is rejected."""
        with pytest.raises(ValidationError):
            FailoverDecisionResponse(
                user_id="user123",
                decision="FAILOVER",
                target_exit="exit-2",
                reason="Test",
                delay_seconds=-1  # Should be >= 0
            )


class TestBroadcastRequest:
    """Test BroadcastRequest schema."""
    
    def test_broadcast_to_all(self):
        """Test broadcast to all users."""
        request = BroadcastRequest(
            message="System maintenance scheduled",
            user_ids=None  # All users
        )
        assert request.message == "System maintenance scheduled"
        assert request.user_ids is None
        
    def test_broadcast_to_specific_users(self):
        """Test broadcast to specific users."""
        request = BroadcastRequest(
            message="Your failover completed",
            user_ids=["user1", "user2", "user3"]
        )
        assert request.user_ids == ["user1", "user2", "user3"]
        
    def test_empty_message_allowed(self):
        """Test that empty message is allowed (validated elsewhere)."""
        request = BroadcastRequest(
            message="",
            user_ids=None
        )
        assert request.message == ""


class TestBroadcastResponse:
    """Test BroadcastResponse schema."""
    
    def test_successful_broadcast(self):
        """Test successful broadcast response."""
        response = BroadcastResponse(
            sent_count=150,
            failed_count=0,
            message="Broadcast sent successfully"
        )
        assert response.sent_count == 150
        assert response.failed_count == 0
        
    def test_partial_failure(self):
        """Test broadcast with partial failures."""
        response = BroadcastResponse(
            sent_count=145,
            failed_count=5,
            message="Broadcast completed with some failures"
        )
        assert response.sent_count == 145
        assert response.failed_count == 5
        
    def test_complete_failure(self):
        """Test complete broadcast failure."""
        response = BroadcastResponse(
            sent_count=0,
            failed_count=150,
            message="All broadcasts failed"
        )
        assert response.sent_count == 0
        assert response.failed_count == 150
