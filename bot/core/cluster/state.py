"""Cluster state management for multi-node architecture."""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from bot.models.cluster import ClusterNode, LeaderInfo, NodeState, NodeRole

logger = logging.getLogger(__name__)


@dataclass
class ClusterState:
    """Shared state for cluster management.
    
    Thread-safe storage for:
    - Current node state (follower/candidate/leader)
    - Known cluster nodes
    - Current leader info
    - Vote tracking during elections
    """
    
    # Node identification
    node_id: str
    node_role: NodeRole = NodeRole.EXIT
    
    # RAFT state
    current_state: NodeState = NodeState.FOLLOWER
    current_term: int = 0
    voted_for: Optional[str] = None
    
    # Leader tracking
    current_leader: Optional[LeaderInfo] = None
    leader_last_seen: Optional[str] = None
    
    # Cluster topology
    nodes: Dict[str, ClusterNode] = field(default_factory=dict)
    exit_nodes: Dict[str, ClusterNode] = field(default_factory=dict)
    entry_nodes: Dict[str, ClusterNode] = field(default_factory=dict)
    
    # Election tracking
    votes_received: Set[str] = field(default_factory=set)
    election_in_progress: bool = False
    
    # Thread safety
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    
    def __post_init__(self):
        """Initialize after dataclass creation."""
        if not self._lock:
            self._lock = threading.RLock()
    
    # === State Management ===
    
    def get_state(self) -> NodeState:
        """Get current node state (thread-safe)."""
        with self._lock:
            return self.current_state
    
    def set_state(self, state: NodeState) -> None:
        """Set current node state (thread-safe)."""
        with self._lock:
            old_state = self.current_state
            self.current_state = state
            logger.info(f"Node state changed: {old_state.value} -> {state.value}")
    
    def increment_term(self) -> int:
        """Increment current term and return new value."""
        with self._lock:
            self.current_term += 1
            self.voted_for = None
            self.votes_received.clear()
            logger.info(f"Term incremented to {self.current_term}")
            return self.current_term
    
    def get_term(self) -> int:
        """Get current term."""
        with self._lock:
            return self.current_term
    
    # === Voting ===
    
    def record_vote(self, voter_id: str) -> int:
        """Record a vote from a node. Returns total votes."""
        with self._lock:
            self.votes_received.add(voter_id)
            return len(self.votes_received)
    
    def get_vote_count(self) -> int:
        """Get number of votes received."""
        with self._lock:
            return len(self.votes_received)
    
    def has_voted(self) -> bool:
        """Check if node has already voted in current term."""
        with self._lock:
            return self.voted_for is not None
    
    def cast_vote(self, candidate_id: str) -> None:
        """Cast vote for a candidate."""
        with self._lock:
            self.voted_for = candidate_id
            logger.info(f"Voted for {candidate_id} in term {self.current_term}")
    
    def reset_votes(self) -> None:
        """Reset voting state for new election."""
        with self._lock:
            self.votes_received.clear()
            self.voted_for = None
    
    # === Leader Management ===
    
    def set_leader(self, leader_id: str, term: int) -> None:
        """Set current leader."""
        with self._lock:
            self.current_leader = LeaderInfo(
                node_id=leader_id,
                term=term,
                elected_at=datetime.now(timezone.utc).isoformat()
            )
            self.leader_last_seen = datetime.now(timezone.utc).isoformat()
            logger.info(f"Leader set to {leader_id} (term {term})")
    
    def get_leader(self) -> Optional[LeaderInfo]:
        """Get current leader info."""
        with self._lock:
            return self.current_leader
    
    def is_leader(self) -> bool:
        """Check if this node is the leader."""
        with self._lock:
            return self.current_state == NodeState.LEADER
    
    def update_leader_heartbeat(self) -> None:
        """Update last seen timestamp for leader."""
        with self._lock:
            self.leader_last_seen = datetime.now(timezone.utc).isoformat()
    
    def get_leader_last_seen(self) -> Optional[str]:
        """Get timestamp of last leader heartbeat."""
        with self._lock:
            return self.leader_last_seen
    
    def clear_leader(self) -> None:
        """Clear leader info (e.g., when leader is suspected down)."""
        with self._lock:
            self.current_leader = None
            self.leader_last_seen = None
            logger.info("Leader cleared")
    
    # === Node Management ===
    
    def add_node(self, node: ClusterNode) -> None:
        """Add a node to cluster topology."""
        with self._lock:
            # If node already exists with a different role, clean up old role index
            if node.node_id in self.nodes:
                old_node = self.nodes[node.node_id]
                if old_node.role == NodeRole.EXIT and node.node_id in self.exit_nodes:
                    del self.exit_nodes[node.node_id]
                elif old_node.role == NodeRole.ENTRY and node.node_id in self.entry_nodes:
                    del self.entry_nodes[node.node_id]
            
            self.nodes[node.node_id] = node
            
            if node.role == NodeRole.EXIT:
                self.exit_nodes[node.node_id] = node
            elif node.role == NodeRole.ENTRY:
                self.entry_nodes[node.node_id] = node
            
            logger.debug(f"Added {node.role.value} node: {node.node_id}")
    
    def remove_node(self, node_id: str) -> bool:
        """Remove a node from cluster."""
        with self._lock:
            if node_id not in self.nodes:
                return False
            
            node = self.nodes.pop(node_id)
            
            if node.role == NodeRole.EXIT and node_id in self.exit_nodes:
                del self.exit_nodes[node_id]
            elif node.role == NodeRole.ENTRY and node_id in self.entry_nodes:
                del self.entry_nodes[node_id]
            
            logger.info(f"Removed node: {node_id}")
            return True
    
    def get_node(self, node_id: str) -> Optional[ClusterNode]:
        """Get node by ID."""
        with self._lock:
            return self.nodes.get(node_id)
    
    def get_all_nodes(self) -> List[ClusterNode]:
        """Get all known nodes."""
        with self._lock:
            return list(self.nodes.values())
    
    def get_exit_nodes(self) -> List[ClusterNode]:
        """Get all Exit nodes."""
        with self._lock:
            return list(self.exit_nodes.values())
    
    def get_entry_nodes(self) -> List[ClusterNode]:
        """Get all Entry nodes."""
        with self._lock:
            return list(self.entry_nodes.values())
    
    def update_node_status(self, node_id: str, status: str) -> bool:
        """Update status of a node."""
        with self._lock:
            if node_id not in self.nodes:
                return False
            
            self.nodes[node_id].status = status
            self.nodes[node_id].last_seen = datetime.now(timezone.utc).isoformat()
            return True
    
    def get_peer_nodes(self) -> List[ClusterNode]:
        """Get all peer nodes (excluding self)."""
        with self._lock:
            return [
                node for node_id, node in self.nodes.items()
                if node_id != self.node_id
            ]
    
    def get_exit_peers(self) -> List[ClusterNode]:
        """Get Exit peer nodes (excluding self)."""
        with self._lock:
            return [
                node for node_id, node in self.exit_nodes.items()
                if node_id != self.node_id
            ]
    
    # === Election Helpers ===
    
    def start_election(self) -> int:
        """Start new election. Returns new term."""
        with self._lock:
            self.election_in_progress = True
            self.current_state = NodeState.CANDIDATE
            self.votes_received.clear()
            self.voted_for = self.node_id  # Vote for self
            self.votes_received.add(self.node_id)
            self.current_term += 1
            
            logger.info(f"Starting election for term {self.current_term}")
            return self.current_term
    
    def end_election(self) -> None:
        """End election process."""
        with self._lock:
            self.election_in_progress = False
    
    def is_election_in_progress(self) -> bool:
        """Check if election is in progress."""
        with self._lock:
            return self.election_in_progress
    
    # === Serialization ===
    
    def to_dict(self) -> Dict:
        """Convert state to dictionary."""
        with self._lock:
            return {
                'node_id': self.node_id,
                'node_role': self.node_role.value,
                'current_state': self.current_state.value,
                'current_term': self.current_term,
                'voted_for': self.voted_for,
                'current_leader': self.current_leader.to_dict() if self.current_leader else None,
                'leader_last_seen': self.leader_last_seen,
                'nodes': {k: v.to_dict() for k, v in self.nodes.items()},
                'votes_received': list(self.votes_received),
                'election_in_progress': self.election_in_progress,
            }
    
    def __repr__(self) -> str:
        """String representation."""
        with self._lock:
            return (
                f"ClusterState(node={self.node_id}, "
                f"state={self.current_state.value}, "
                f"term={self.current_term}, "
                f"leader={self.current_leader.node_id if self.current_leader else 'None'}, "
                f"nodes={len(self.nodes)})"
            )
