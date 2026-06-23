"""Unit tests for LeaderElection."""

import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from bot.models.vpn_node import VPNNode, NodeType
from bot.models.cluster import (
    NodeRole,
    NodeState,
    VoteRequest,
    VoteResponse,
    HealthStatus,
)
from bot.core.cluster.state import ClusterState
from bot.core.cluster.election import LeaderElection


class TestLeaderElectionBasics:
    """Test basic LeaderElection operations."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state for tests."""
        return ClusterState(node_id="exit-1")
    
    @pytest.fixture
    def election(self, cluster_state):
        """Create election manager for tests."""
        return LeaderElection(
            state=cluster_state,
            min_timeout=0.1,
            max_timeout=0.2,
            heartbeat_interval=0.05,
        )
    
    @pytest.mark.asyncio
    async def test_initial_state(self, election, cluster_state):
        """Test initial election state."""
        assert election.state == cluster_state
        assert cluster_state.current_state == NodeState.FOLLOWER
        assert election._running is False
    
    @pytest.mark.asyncio
    async def test_start_stop(self, election, cluster_state):
        """Test start and stop lifecycle."""
        await election.start()
        assert election._running is True
        assert cluster_state.current_state == NodeState.FOLLOWER
        
        await election.stop()
        assert election._running is False
    
    @pytest.mark.asyncio
    async def test_callback_registration(self, election):
        """Test callback registration."""
        become_leader = AsyncMock()
        step_down = AsyncMock()
        
        election.on_become_leader(become_leader)
        election.on_step_down(step_down)
        
        assert election._on_become_leader == become_leader
        assert election._on_step_down == step_down


class TestLeaderElectionVoting:
    """Test voting logic."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state with peers."""
        state = ClusterState(node_id="exit-1")
        
        # Add peer nodes
        state.add_node(VPNNode(
            node_id="exit-2",
            node_type=NodeType.EXIT,
            host="10.0.0.2",
        ))
        state.add_node(VPNNode(
            node_id="exit-3",
            node_type=NodeType.EXIT,
            host="10.0.0.3",
        ))
        
        return state
    
    @pytest.fixture
    def election(self, cluster_state):
        """Create election manager."""
        return LeaderElection(
            state=cluster_state,
            min_timeout=1.0,
            max_timeout=2.0,
        )
    
    @pytest.mark.asyncio
    async def test_handle_vote_request_higher_term(self, election, cluster_state):
        """Test handling vote request with higher term."""
        cluster_state.current_term = 1
        
        request = VoteRequest(
            term=2,
            candidate_id="exit-2",
            last_log_index=0,
            last_log_term=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        response = await election.handle_vote_request(request)
        
        assert response.vote_granted is True
        assert response.term == 2
        assert cluster_state.current_term == 2
        assert cluster_state.voted_for == "exit-2"
    
    @pytest.mark.asyncio
    async def test_handle_vote_request_lower_term(self, election, cluster_state):
        """Test handling vote request with lower term."""
        cluster_state.current_term = 5
        
        request = VoteRequest(
            term=3,
            candidate_id="exit-2",
            last_log_index=0,
            last_log_term=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        response = await election.handle_vote_request(request)
        
        assert response.vote_granted is False
        assert response.term == 5  # Current term
    
    @pytest.mark.asyncio
    async def test_handle_vote_request_already_voted(self, election, cluster_state):
        """Test handling vote request when already voted."""
        cluster_state.current_term = 1
        cluster_state.cast_vote("exit-3")  # Already voted for exit-3
        
        request = VoteRequest(
            term=1,
            candidate_id="exit-2",
            last_log_index=0,
            last_log_term=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        response = await election.handle_vote_request(request)
        
        assert response.vote_granted is False
    
    @pytest.mark.asyncio
    async def test_handle_vote_request_same_candidate(self, election, cluster_state):
        """Test handling vote request from same candidate (re-vote)."""
        cluster_state.current_term = 1
        cluster_state.cast_vote("exit-2")
        
        request = VoteRequest(
            term=1,
            candidate_id="exit-2",  # Same candidate
            last_log_index=0,
            last_log_term=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        response = await election.handle_vote_request(request)
        
        assert response.vote_granted is True  # Can vote again for same candidate


class TestLeaderElectionHeartbeat:
    """Test heartbeat handling."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state."""
        return ClusterState(node_id="exit-1")
    
    @pytest.fixture
    def election(self, cluster_state):
        """Create election manager."""
        return LeaderElection(
            state=cluster_state,
            min_timeout=1.0,
            max_timeout=2.0,
        )
    
    @pytest.mark.asyncio
    async def test_handle_heartbeat_from_leader(self, election, cluster_state):
        """Test handling valid heartbeat from leader."""
        cluster_state.set_state(NodeState.FOLLOWER)
        cluster_state.current_term = 1
        
        health = HealthStatus(
            node_id="exit-2",
            state=NodeState.LEADER,
            term=1,
            is_leader=True,
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
            db_status=True,
            xui_status=True,
            uptime_seconds=100,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        accepted = await election.handle_heartbeat(health)
        
        assert accepted is True
        assert cluster_state.get_leader().node_id == "exit-2"
    
    @pytest.mark.asyncio
    async def test_handle_heartbeat_higher_term(self, election, cluster_state):
        """Test handling heartbeat with higher term."""
        cluster_state.set_state(NodeState.LEADER)  # Currently leader
        cluster_state.current_term = 1
        
        health = HealthStatus(
            node_id="exit-2",
            state=NodeState.LEADER,
            term=2,  # Higher term
            is_leader=True,
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
            db_status=True,
            xui_status=True,
            uptime_seconds=100,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        accepted = await election.handle_heartbeat(health)
        
        assert accepted is True
        assert cluster_state.current_term == 2
        assert cluster_state.current_state == NodeState.FOLLOWER  # Stepped down
    
    @pytest.mark.asyncio
    async def test_handle_heartbeat_lower_term(self, election, cluster_state):
        """Test handling heartbeat with lower term."""
        cluster_state.current_term = 5
        
        health = HealthStatus(
            node_id="exit-2",
            state=NodeState.LEADER,
            term=3,  # Lower term
            is_leader=True,
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
            db_status=True,
            xui_status=True,
            uptime_seconds=100,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        accepted = await election.handle_heartbeat(health)
        
        assert accepted is False  # Reject stale leader


class TestLeaderElectionStateTransitions:
    """Test state transitions."""
    
    @pytest.fixture
    def cluster_state(self):
        """Create cluster state."""
        state = ClusterState(node_id="exit-1")
        state.add_node(VPNNode(
            node_id="exit-2",
            node_type=NodeType.EXIT,
            host="10.0.0.2",
        ))
        return state
    
    @pytest.fixture
    def election(self, cluster_state):
        """Create election manager."""
        return LeaderElection(
            state=cluster_state,
            min_timeout=1.0,
            max_timeout=2.0,
            heartbeat_interval=0.1,
        )
    
    @pytest.mark.asyncio
    async def test_become_leader(self, election, cluster_state):
        """Test becoming leader."""
        become_leader_cb = AsyncMock()
        election.on_become_leader(become_leader_cb)
        
        cluster_state.set_state(NodeState.CANDIDATE)
        cluster_state.current_term = 1
        cluster_state.record_vote("exit-1")  # Self vote
        
        await election._become_leader()
        
        assert cluster_state.is_leader() is True
        assert cluster_state.get_leader().node_id == "exit-1"
        become_leader_cb.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_step_down(self, election, cluster_state):
        """Test stepping down from leadership."""
        step_down_cb = AsyncMock()
        election.on_step_down(step_down_cb)
        
        # Become leader first
        cluster_state.set_state(NodeState.LEADER)
        cluster_state.set_leader("exit-1", term=1)
        
        await election._step_down()
        
        assert cluster_state.is_leader() is False
        assert cluster_state.current_state == NodeState.FOLLOWER
        step_down_cb.assert_called_once()


class TestLeaderElectionIntegration:
    """Integration tests for leader election."""
    
    @pytest.mark.asyncio
    async def test_single_node_becomes_leader(self):
        """Test single node automatically becomes leader."""
        state = ClusterState(node_id="exit-1")
        election = LeaderElection(
            state=state,
            min_timeout=0.1,
            max_timeout=0.2,
        )
        
        become_leader = AsyncMock()
        election.on_become_leader(become_leader)
        
        # No peers - should become leader immediately
        await election._start_election()
        
        assert state.is_leader() is True
        become_leader.assert_called_once()
        
        await election.stop()
    
    @pytest.mark.asyncio
    async def test_election_with_majority_votes(self):
        """Test winning election with majority votes."""
        state = ClusterState(node_id="exit-1")
        
        # Add 2 peers (total 3 nodes, need 2 votes for majority)
        state.add_node(VPNNode(node_id="exit-2", node_type=NodeType.EXIT, host="10.0.0.2"))
        state.add_node(VPNNode(node_id="exit-3", node_type=NodeType.EXIT, host="10.0.0.3"))
        
        election = LeaderElection(
            state=state,
            min_timeout=0.1,
            max_timeout=0.2,
        )
        
        become_leader = AsyncMock()
        election.on_become_leader(become_leader)
        
        # Mock vote request handler that returns granted votes
        vote_count = [0]
        async def mock_vote_request(request):
            vote_count[0] += 1
            return VoteResponse(
                term=request.term,
                vote_granted=True,
                voter_id=f"exit-{vote_count[0] + 1}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        
        election.set_vote_request_handler(mock_vote_request)
        
        await election._start_election()
        await asyncio.sleep(0.1)  # Wait for votes
        
        # Should have become leader
        assert state.is_leader() is True
        
        await election.stop()
    
    @pytest.mark.asyncio
    async def test_split_vote_retry(self):
        """Test election retry on split vote."""
        state = ClusterState(node_id="exit-1")
        state.add_node(VPNNode(node_id="exit-2", node_type=NodeType.EXIT, host="10.0.0.2"))
        
        election = LeaderElection(
            state=state,
            min_timeout=0.1,
            max_timeout=0.15,
        )
        
        # Mock that denies all votes
        async def mock_vote_request(request):
            return VoteResponse(
                term=request.term,
                vote_granted=False,
                voter_id="exit-2",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        
        election.set_vote_request_handler(mock_vote_request)
        
        await election._start_election()
        await asyncio.sleep(0.2)  # Wait for timeout
        
        # Should still be follower, election ended
        assert state.is_election_in_progress() is False
        
        await election.stop()


class TestLeaderElectionConcurrency:
    """Test concurrent operations."""
    
    @pytest.mark.asyncio
    async def test_concurrent_heartbeats(self):
        """Test handling concurrent heartbeats."""
        state = ClusterState(node_id="exit-1")
        election = LeaderElection(
            state=state,
            min_timeout=1.0,
            max_timeout=2.0,
        )
        
        await election.start()
        
        # Send multiple heartbeats concurrently
        heartbeats = []
        for i in range(5):
            health = HealthStatus(
                node_id=f"exit-{i+2}",
                state=NodeState.LEADER,
                term=1,
                is_leader=True,
                last_heartbeat=datetime.now(timezone.utc).isoformat(),
                db_status=True,
                xui_status=True,
                uptime_seconds=100,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            heartbeats.append(election.handle_heartbeat(health))
        
        results = await asyncio.gather(*heartbeats)
        
        # All should be accepted
        assert all(results)
        
        await election.stop()
