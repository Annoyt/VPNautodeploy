"""Tests for LeaderElection (RAFT consensus)."""

import pytest
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime, timezone

from bot.core.cluster.election import LeaderElection
from bot.core.cluster.state import ClusterState
from bot.models.cluster import NodeState, VoteRequest, VoteResponse, HealthStatus


class TestLeaderElectionInitialization:
    """Test LeaderElection initialization."""
    
    @pytest.fixture
    def mock_state(self):
        """Create mock cluster state."""
        state = Mock(spec=ClusterState)
        state.node_id = 'exit-1'
        return state
    
    def test_initialization(self, mock_state):
        """Test election initializes with correct settings."""
        election = LeaderElection(
            state=mock_state,
            min_timeout=5.0,
            max_timeout=10.0,
            heartbeat_interval=2.0,
        )
        
        assert election.state == mock_state
        assert election.min_timeout == 5.0
        assert election.max_timeout == 10.0
        assert election.heartbeat_interval == 2.0
        assert election._running is False
        assert election._on_become_leader is None
        assert election._on_step_down is None
    
    def test_randomize_timeout(self, mock_state):
        """Test timeout randomization."""
        election = LeaderElection(state=mock_state, min_timeout=5.0, max_timeout=10.0)
        
        timeout = election._randomize_timeout()
        
        assert 5.0 <= timeout <= 10.0
    
    def test_randomize_timeout_different_calls(self, mock_state):
        """Test that multiple calls produce different timeouts."""
        election = LeaderElection(state=mock_state, min_timeout=5.0, max_timeout=10.0)
        
        timeouts = [election._randomize_timeout() for _ in range(10)]
        
        # Check all in range
        assert all(5.0 <= t <= 10.0 for t in timeouts)
        # Check some variation (very unlikely all same)
        assert len(set(timeouts)) > 1


class TestLeaderElectionCallbacks:
    """Test callback registration."""
    
    @pytest.fixture
    def election(self):
        """Create election with mocked state."""
        state = Mock(spec=ClusterState)
        state.node_id = 'exit-1'
        return LeaderElection(state=state)
    
    def test_on_become_leader_callback(self, election):
        """Test registering become leader callback."""
        callback = AsyncMock()
        election.on_become_leader(callback)
        
        assert election._on_become_leader == callback
    
    def test_on_step_down_callback(self, election):
        """Test registering step down callback."""
        callback = AsyncMock()
        election.on_step_down(callback)
        
        assert election._on_step_down == callback
    
    def test_set_vote_request_handler(self, election):
        """Test setting vote request handler."""
        handler = AsyncMock()
        election.set_vote_request_handler(handler)
        
        assert election._send_vote_request == handler
    
    def test_set_heartbeat_handler(self, election):
        """Test setting heartbeat handler."""
        handler = AsyncMock()
        election.set_heartbeat_handler(handler)
        
        assert election._send_heartbeat == handler


class TestLeaderElectionVoteHandling:
    """Test vote request handling."""
    
    @pytest.fixture
    def election(self):
        """Create election with mocked state."""
        state = Mock(spec=ClusterState)
        state.node_id = 'exit-1'
        state.get_term = Mock(return_value=5)
        state.has_voted = Mock(return_value=False)
        state.set_state = Mock()
        state.cast_vote = Mock()
        return LeaderElection(state=state)
    
    @pytest.mark.asyncio
    async def test_handle_vote_request_lower_term(self, election):
        """Test rejecting vote request with lower term."""
        request = VoteRequest(
            term=3,  # Lower than current (5)
            candidate_id='exit-2',
            last_log_index=0,
            last_log_term=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        response = await election.handle_vote_request(request)
        
        assert response.vote_granted is False
        assert response.term == 5  # Current term
    
    @pytest.mark.asyncio
    async def test_handle_vote_request_higher_term(self, election):
        """Test accepting vote request with higher term."""
        request = VoteRequest(
            term=10,  # Higher than current (5)
            candidate_id='exit-2',
            last_log_index=0,
            last_log_term=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        response = await election.handle_vote_request(request)
        
        # Should update term and become follower
        assert election.state.current_term == 10
        election.state.set_state.assert_called_with(NodeState.FOLLOWER)
    
    @pytest.mark.asyncio
    async def test_handle_vote_request_grant_vote(self, election):
        """Test granting vote to valid candidate."""
        request = VoteRequest(
            term=5,
            candidate_id='exit-2',
            last_log_index=0,
            last_log_term=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        response = await election.handle_vote_request(request)
        
        assert response.vote_granted is True
        assert response.voter_id == 'exit-1'
        election.state.cast_vote.assert_called_with('exit-2')
    
    @pytest.mark.asyncio
    async def test_handle_vote_request_already_voted(self, election):
        """Test rejecting vote if already voted for different candidate."""
        election.state.has_voted = Mock(return_value=True)
        election.state.voted_for = 'exit-3'  # Voted for someone else
        
        request = VoteRequest(
            term=5,
            candidate_id='exit-2',
            last_log_index=0,
            last_log_term=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        response = await election.handle_vote_request(request)
        
        assert response.vote_granted is False


class TestLeaderElectionHeartbeatHandling:
    """Test heartbeat handling."""
    
    @pytest.fixture
    def election(self):
        """Create election with mocked state."""
        state = Mock(spec=ClusterState)
        state.node_id = 'exit-1'
        state.get_term = Mock(return_value=5)
        state.set_leader = Mock()
        state.clear_leader = Mock()
        state.set_state = Mock()
        state.current_state = NodeState.FOLLOWER
        return LeaderElection(state=state)
    
    @pytest.mark.asyncio
    async def test_handle_heartbeat_lower_term(self, election):
        """Test rejecting heartbeat with lower term."""
        health = HealthStatus(
            node_id='exit-2',
            state=NodeState.LEADER,
            term=3,  # Lower than current (5)
            is_leader=True,
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
            db_status=True,
            xui_status=True,
            uptime_seconds=100,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        result = await election.handle_heartbeat(health)
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_handle_heartbeat_higher_term(self, election):
        """Test accepting heartbeat with higher term."""
        health = HealthStatus(
            node_id='exit-2',
            state=NodeState.LEADER,
            term=10,  # Higher than current (5)
            is_leader=True,
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
            db_status=True,
            xui_status=True,
            uptime_seconds=100,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        result = await election.handle_heartbeat(health)
        
        assert result is True
        assert election.state.current_term == 10
        election.state.set_leader.assert_called_with('exit-2', 10)
    
    @pytest.mark.asyncio
    async def test_handle_heartbeat_step_down(self, election):
        """Test stepping down when receive heartbeat from valid leader."""
        election.state.current_state = NodeState.CANDIDATE
        election._step_down = AsyncMock()
        
        health = HealthStatus(
            node_id='exit-2',
            state=NodeState.LEADER,
            term=6,  # Higher term
            is_leader=True,
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
            db_status=True,
            xui_status=True,
            uptime_seconds=100,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        result = await election.handle_heartbeat(health)
        
        assert result is True


class TestLeaderElectionStateTransitions:
    """Test leader state transitions."""
    
    @pytest.fixture
    def election(self):
        """Create election with mocked state."""
        state = Mock(spec=ClusterState)
        state.node_id = 'exit-1'
        state.get_term = Mock(return_value=5)
        state.set_state = Mock()
        state.set_leader = Mock()
        state.clear_leader = Mock()
        state.end_election = Mock()
        state.is_leader = Mock(return_value=True)
        return LeaderElection(state=state)
    
    @pytest.mark.asyncio
    async def test_become_leader(self, election):
        """Test transitioning to leader."""
        callback = AsyncMock()
        election.on_become_leader(callback)
        
        with patch.object(election, '_start_heartbeat_loop', new_callable=AsyncMock):
            await election._become_leader()
        
        election.state.set_state.assert_called_with(NodeState.LEADER)
        election.state.set_leader.assert_called_with('exit-1', 5)
        callback.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_step_down(self, election):
        """Test stepping down from leader."""
        callback = AsyncMock()
        election.on_step_down(callback)
        
        with patch.object(election, '_start_election_timer', new_callable=AsyncMock):
            await election._step_down()
        
        election.state.set_state.assert_called_with(NodeState.FOLLOWER)
        election.state.clear_leader.assert_called_once()
        callback.assert_called_once()


class TestLeaderElectionNoPeers:
    """Test election with no peers."""
    
    @pytest.fixture
    def election(self):
        """Create election with mocked state."""
        state = Mock(spec=ClusterState)
        state.node_id = 'exit-1'
        state.get_term = Mock(return_value=5)
        state.get_exit_peers = Mock(return_value=[])  # No peers
        state.set_state = Mock()
        state.set_leader = Mock()
        state.end_election = Mock()
        return LeaderElection(state=state)
    
    @pytest.mark.asyncio
    async def test_start_election_no_peers_becomes_leader(self, election):
        """Test becoming leader automatically when no peers."""
        with patch.object(election, '_become_leader', new_callable=AsyncMock) as mock_become:
            await election._start_election()
        
        mock_become.assert_called_once()
