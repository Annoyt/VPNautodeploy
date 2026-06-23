"""Unit tests for ClusterState."""

import pytest
from datetime import datetime, timezone

from bot.models.vpn_node import VPNNode, NodeType
from bot.models.cluster import NodeRole, NodeState, LeaderInfo
from bot.core.cluster.state import ClusterState


class TestClusterStateBasics:
    """Test basic ClusterState operations."""
    
    def test_initial_state(self):
        """Test initial state of cluster."""
        state = ClusterState(node_id="exit-1")
        
        assert state.node_id == "exit-1"
        assert state.current_state == NodeState.FOLLOWER
        assert state.current_term == 0
        assert state.is_leader() is False
        assert state.get_leader() is None
    
    def test_state_transition(self):
        """Test state transitions."""
        state = ClusterState(node_id="exit-1")
        
        state.set_state(NodeState.CANDIDATE)
        assert state.get_state() == NodeState.CANDIDATE
        
        state.set_state(NodeState.LEADER)
        assert state.get_state() == NodeState.LEADER
        assert state.is_leader() is True
    
    def test_term_increment(self):
        """Test term increment."""
        state = ClusterState(node_id="exit-1")
        
        term = state.increment_term()
        assert term == 1
        assert state.get_term() == 1
        
        term = state.increment_term()
        assert term == 2


class TestClusterStateVoting:
    """Test voting operations."""
    
    def test_record_vote(self):
        """Test recording votes."""
        state = ClusterState(node_id="exit-1")
        
        count = state.record_vote("exit-2")
        assert count == 1
        
        count = state.record_vote("exit-3")
        assert count == 2
    
    def test_duplicate_vote(self):
        """Test that duplicate votes are not counted twice."""
        state = ClusterState(node_id="exit-1")
        
        state.record_vote("exit-2")
        state.record_vote("exit-2")  # Duplicate
        
        assert state.get_vote_count() == 1
    
    def test_cast_vote(self):
        """Test casting vote."""
        state = ClusterState(node_id="exit-1")
        
        assert state.has_voted() is False
        
        state.cast_vote("exit-2")
        
        assert state.has_voted() is True
        assert state.voted_for == "exit-2"
    
    def test_reset_votes(self):
        """Test resetting votes."""
        state = ClusterState(node_id="exit-1")
        
        state.record_vote("exit-2")
        state.cast_vote("exit-2")
        
        state.reset_votes()
        
        assert state.get_vote_count() == 0
        assert state.has_voted() is False


class TestClusterStateLeader:
    """Test leader management."""
    
    def test_set_leader(self):
        """Test setting leader."""
        state = ClusterState(node_id="exit-1")
        
        state.set_leader("exit-2", term=5)
        
        leader = state.get_leader()
        assert leader is not None
        assert leader.node_id == "exit-2"
        assert leader.term == 5
    
    def test_clear_leader(self):
        """Test clearing leader."""
        state = ClusterState(node_id="exit-1")
        
        state.set_leader("exit-2", term=5)
        state.clear_leader()
        
        assert state.get_leader() is None
    
    def test_update_leader_heartbeat(self):
        """Test updating leader heartbeat."""
        state = ClusterState(node_id="exit-1")
        
        state.set_leader("exit-2", term=5)
        last_seen_before = state.get_leader_last_seen()
        
        # Update heartbeat
        state.update_leader_heartbeat()
        last_seen_after = state.get_leader_last_seen()
        
        assert last_seen_after != last_seen_before


class TestClusterStateNodes:
    """Test node management."""
    
    @pytest.fixture
    def sample_exit_node(self):
        """Create sample Exit node."""
        return VPNNode(
            node_id="exit-frankfurt-1",
            node_type=NodeType.EXIT,
            host="203.0.113.30",
        )
    
    @pytest.fixture
    def sample_entry_node(self):
        """Create sample Entry node."""
        return VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
        )
    
    def test_add_exit_node(self, sample_exit_node):
        """Test adding Exit node."""
        state = ClusterState(node_id="exit-1")
        
        state.add_node(sample_exit_node)
        
        assert "exit-frankfurt-1" in state.nodes
        assert "exit-frankfurt-1" in state.exit_nodes
        assert len(state.get_exit_nodes()) == 1
    
    def test_add_entry_node(self, sample_entry_node):
        """Test adding Entry node."""
        state = ClusterState(node_id="exit-1")
        
        state.add_node(sample_entry_node)
        
        assert "entry-moscow-1" in state.nodes
        assert "entry-moscow-1" in state.entry_nodes
        assert len(state.get_entry_nodes()) == 1
    
    def test_get_node(self, sample_exit_node):
        """Test getting node by ID."""
        state = ClusterState(node_id="exit-1")
        state.add_node(sample_exit_node)
        
        node = state.get_node("exit-frankfurt-1")
        
        assert node is not None
        assert node.node_id == "exit-frankfurt-1"
    
    def test_get_nonexistent_node(self):
        """Test getting non-existent node."""
        state = ClusterState(node_id="exit-1")
        
        node = state.get_node("nonexistent")
        
        assert node is None
    
    def test_remove_node(self, sample_exit_node):
        """Test removing node."""
        state = ClusterState(node_id="exit-1")
        state.add_node(sample_exit_node)
        
        result = state.remove_node("exit-frankfurt-1")
        
        assert result is True
        assert "exit-frankfurt-1" not in state.nodes
    
    def test_remove_nonexistent_node(self):
        """Test removing non-existent node."""
        state = ClusterState(node_id="exit-1")
        
        result = state.remove_node("nonexistent")
        
        assert result is False
    
    def test_update_node_status(self, sample_exit_node):
        """Test updating node status."""
        state = ClusterState(node_id="exit-1")
        state.add_node(sample_exit_node)
        
        result = state.update_node_status("exit-frankfurt-1", "degraded")
        
        assert result is True
        assert state.get_node("exit-frankfurt-1").status == "degraded"
    
    def test_get_peer_nodes(self, sample_exit_node, sample_entry_node):
        """Test getting peer nodes (excluding self)."""
        state = ClusterState(node_id="exit-1")
        
        # Add self
        self_node = VPNNode(node_id="exit-1", node_type=NodeType.EXIT, host="127.0.0.1")
        state.add_node(self_node)
        
        # Add peers
        state.add_node(sample_exit_node)
        state.add_node(sample_entry_node)
        
        peers = state.get_peer_nodes()
        
        assert len(peers) == 2
        assert all(p.node_id != "exit-1" for p in peers)
    
    def test_get_exit_peers(self, sample_exit_node, sample_entry_node):
        """Test getting Exit peer nodes."""
        state = ClusterState(node_id="exit-1")
        
        # Add self
        self_node = VPNNode(node_id="exit-1", node_type=NodeType.EXIT, host="127.0.0.1")
        state.add_node(self_node)
        
        # Add Exit peer
        state.add_node(sample_exit_node)
        
        # Add Entry node (should not be in exit_peers)
        state.add_node(sample_entry_node)
        
        exit_peers = state.get_exit_peers()
        
        assert len(exit_peers) == 1
        assert exit_peers[0].role == NodeRole.EXIT


class TestClusterStateElection:
    """Test election operations."""
    
    def test_start_election(self):
        """Test starting election."""
        state = ClusterState(node_id="exit-1")
        
        term = state.start_election()
        
        assert term == 1
        assert state.current_state == NodeState.CANDIDATE
        assert state.is_election_in_progress() is True
        assert state.get_vote_count() == 1  # Self vote
        assert state.voted_for == "exit-1"
    
    def test_end_election(self):
        """Test ending election."""
        state = ClusterState(node_id="exit-1")
        
        state.start_election()
        state.end_election()
        
        assert state.is_election_in_progress() is False
    
    def test_multiple_elections_increment_term(self):
        """Test that multiple elections increment term."""
        state = ClusterState(node_id="exit-1")
        
        term1 = state.start_election()
        state.end_election()
        
        term2 = state.start_election()
        state.end_election()
        
        assert term2 > term1


class TestClusterStateThreadSafety:
    """Test thread safety of ClusterState."""
    
    def test_concurrent_state_access(self):
        """Test that concurrent access doesn't crash."""
        import threading
        import time
        
        state = ClusterState(node_id="exit-1")
        errors = []
        
        def writer():
            try:
                for i in range(100):
                    state.set_state(NodeState.CANDIDATE)
                    state.set_state(NodeState.LEADER)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)
        
        def reader():
            try:
                for i in range(100):
                    _ = state.get_state()
                    _ = state.is_leader()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)
        
        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
        ]
        
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        
        assert len(errors) == 0, f"Thread errors: {errors}"


class TestClusterStateSerialization:
    """Test serialization."""
    
    def test_to_dict(self):
        """Test serialization to dict."""
        state = ClusterState(node_id="exit-1")
        state.set_state(NodeState.LEADER)
        state.increment_term()
        state.set_leader("exit-1", term=1)
        
        data = state.to_dict()
        
        assert data['node_id'] == "exit-1"
        assert data['current_state'] == "leader"
        assert data['current_term'] == 1
        assert data['current_leader']['node_id'] == "exit-1"
    
    def test_repr(self):
        """Test string representation."""
        state = ClusterState(node_id="exit-1")
        state.set_state(NodeState.LEADER)
        state.increment_term()
        
        repr_str = repr(state)
        
        assert "exit-1" in repr_str
        assert "leader" in repr_str


class TestClusterStateEdgeCases:
    """Test edge cases and potential bugs."""
    
    def test_update_node_status_missing_node(self):
        """Test update_node_status returns False for missing node."""
        state = ClusterState(node_id="exit-1")
        result = state.update_node_status("nonexistent", "down")
        assert result is False
    
    def test_add_node_overwrites_existing(self):
        """Test adding node with same ID overwrites previous."""
        state = ClusterState(node_id="exit-1")
        node1 = VPNNode(node_id="peer-1", node_type=NodeType.EXIT, host="10.0.0.1")
        node2 = VPNNode(node_id="peer-1", node_type=NodeType.ENTRY, host="10.0.0.2")
        
        state.add_node(node1)
        state.add_node(node2)
        
        assert state.get_node("peer-1").role == NodeRole.ENTRY
        assert "peer-1" not in state.exit_nodes
        assert "peer-1" in state.entry_nodes
    
    def test_remove_node_cleans_both_exit_and_entry(self):
        """Test remove_node cleans node from both role-specific dicts."""
        state = ClusterState(node_id="exit-1")
        node = VPNNode(node_id="peer-1", node_type=NodeType.EXIT, host="10.0.0.1")
        state.add_node(node)
        
        result = state.remove_node("peer-1")
        assert result is True
        assert "peer-1" not in state.nodes
        assert "peer-1" not in state.exit_nodes
        assert "peer-1" not in state.entry_nodes
    
    def test_to_dict_with_nodes(self):
        """Test to_dict serializes nodes correctly."""
        state = ClusterState(node_id="exit-1")
        node = VPNNode(node_id="peer-1", node_type=NodeType.EXIT, host="10.0.0.1")
        state.add_node(node)
        
        data = state.to_dict()
        assert "peer-1" in data["nodes"]
        assert data["nodes"]["peer-1"]["node_id"] == "peer-1"
    
    def test_increment_term_resets_votes(self):
        """Test increment_term clears voted_for and votes_received."""
        state = ClusterState(node_id="exit-1")
        state.record_vote("peer-1")
        state.cast_vote("peer-2")
        
        state.increment_term()
        
        assert state.has_voted() is False
        assert state.get_vote_count() == 0
    
    def test_start_election_increments_term_and_self_votes(self):
        """Test start_election increments term and adds self-vote."""
        state = ClusterState(node_id="exit-1")
        term1 = state.get_term()
        term2 = state.start_election()
        
        assert term2 == term1 + 1
        assert state.voted_for == "exit-1"
        assert state.get_vote_count() == 1
    
    def test_get_exit_peers_excludes_self(self):
        """Test get_exit_peers excludes self even if in exit_nodes."""
        state = ClusterState(node_id="exit-1")
        self_node = VPNNode(node_id="exit-1", node_type=NodeType.EXIT, host="127.0.0.1")
        state.add_node(self_node)
        
        peers = state.get_exit_peers()
        assert peers == []
    
    def test_clear_leader_when_already_none(self):
        """Test clear_leader when leader is already None."""
        state = ClusterState(node_id="exit-1")
        state.clear_leader()
        assert state.get_leader() is None
        assert state.get_leader_last_seen() is None
