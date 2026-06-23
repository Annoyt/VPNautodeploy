"""Leader election implementation based on RAFT consensus algorithm.

Simplified for multi-node VPN cluster where all nodes can communicate
directly via HTTP API.
"""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional, Set

from bot.models.cluster import (
    NodeState,
    VoteRequest,
    VoteResponse,
    HealthStatus,
)
from bot.core.cluster.state import ClusterState

logger = logging.getLogger(__name__)


class LeaderElection:
    """RAFT-like leader election for multi-node cluster.
    
    Manages state transitions:
    - FOLLOWER: Replicates from leader, waits for heartbeat
    - CANDIDATE: Requests votes when election timeout fires
    - LEADER: Sends heartbeats, coordinates replication
    
    Attributes:
        state: Shared cluster state
        min_timeout: Minimum election timeout (seconds)
        max_timeout: Maximum election timeout (seconds)
        heartbeat_interval: Leader heartbeat interval (seconds)
    """
    
    def __init__(
        self,
        state: ClusterState,
        min_timeout: float = 5.0,
        max_timeout: float = 10.0,
        heartbeat_interval: float = 2.0,
    ):
        self.state = state
        self.min_timeout = min_timeout
        self.max_timeout = max_timeout
        self.heartbeat_interval = heartbeat_interval
        
        # Election timer
        self._election_timer: Optional[asyncio.Task] = None
        self._election_timeout: float = self._randomize_timeout()
        
        # Heartbeat task (leader only)
        self._heartbeat_task: Optional[asyncio.Task] = None
        
        # Callbacks
        self._on_become_leader: Optional[Callable[[], Awaitable[None]]] = None
        self._on_step_down: Optional[Callable[[], Awaitable[None]]] = None
        self._send_vote_request: Optional[Callable[[VoteRequest], Awaitable[VoteResponse]]] = None
        self._send_heartbeat: Optional[Callable[[], Awaitable[bool]]] = None
        
        # Control flags
        self._running = False
        self._shutdown_event = asyncio.Event()
    
    # === Callback Registration ===
    
    def on_become_leader(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback for when node becomes leader."""
        self._on_become_leader = callback
    
    def on_step_down(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback for when node steps down from leadership."""
        self._on_step_down = callback
    
    def set_vote_request_handler(
        self,
        handler: Callable[[VoteRequest], Awaitable[VoteResponse]]
    ) -> None:
        """Set handler for sending vote requests to peers."""
        self._send_vote_request = handler
    
    def set_heartbeat_handler(self, handler: Callable[[], Awaitable[bool]]) -> None:
        """Set handler for sending heartbeats to peers."""
        self._send_heartbeat = handler
    
    # === Lifecycle ===
    
    async def start(self) -> None:
        """Start election manager."""
        if self._running:
            return
        
        self._running = True
        self._shutdown_event.clear()
        
        logger.info(
            f"Starting election manager for {self.state.node_id} "
            f"(timeout: {self.min_timeout}-{self.max_timeout}s, "
            f"heartbeat: {self.heartbeat_interval}s)"
        )
        
        # Start as follower and begin election timer
        self.state.set_state(NodeState.FOLLOWER)
        await self._start_election_timer()
    
    async def stop(self) -> None:
        """Stop election manager."""
        if not self._running:
            return
        
        logger.info("Stopping election manager")
        self._running = False
        self._shutdown_event.set()
        
        # Cancel tasks
        if self._election_timer:
            self._election_timer.cancel()
            try:
                await self._election_timer
            except asyncio.CancelledError:
                pass
        
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        
        # Step down if leader
        if self.state.is_leader():
            await self._step_down()
    
    # === Timer Management ===
    
    def _randomize_timeout(self) -> float:
        """Generate random election timeout."""
        return random.uniform(self.min_timeout, self.max_timeout)
    
    async def _start_election_timer(self) -> None:
        """Start or restart election timer."""
        if self._election_timer:
            self._election_timer.cancel()
        
        self._election_timeout = self._randomize_timeout()
        self._election_timer = asyncio.create_task(
            self._election_timer_loop()
        )
        logger.debug(f"Election timer started: {self._election_timeout:.2f}s")
    
    async def _election_timer_loop(self) -> None:
        """Election timer loop - fires if no heartbeat received."""
        while self._running:
            try:
                # Wait for election timeout
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self._election_timeout
                )
                # Shutdown event received
                break
            except asyncio.TimeoutError:
                # Election timeout fired
                if self.state.current_state == NodeState.FOLLOWER:
                    logger.info("Election timeout fired, starting election")
                    await self._start_election()
    
    async def _reset_election_timer(self) -> None:
        """Reset election timer (called on valid heartbeat)."""
        if self.state.current_state == NodeState.FOLLOWER:
            await self._start_election_timer()
    
    async def _start_heartbeat_loop(self) -> None:
        """Start heartbeat loop (leader only)."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop()
        )
        logger.info("Heartbeat loop started")
    
    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats to all peers."""
        while self._running and self.state.is_leader():
            try:
                if self._send_heartbeat:
                    await self._send_heartbeat()
                
                # Wait for next heartbeat interval
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=self.heartbeat_interval
                )
                break  # Shutdown received
            except asyncio.TimeoutError:
                continue  # Send next heartbeat
            except Exception as e:
                logger.error(f"Error in heartbeat loop: {e}")
                await asyncio.sleep(self.heartbeat_interval)
    
    # === State Transitions ===
    
    async def _start_election(self) -> None:
        """Start leader election process."""
        # Increment term and become candidate
        term = self.state.start_election()
        logger.info(f"Starting election for term {term}")
        
        # Get peer nodes
        peers = self.state.get_exit_peers()
        
        if not peers:
            # No peers - automatically become leader
            logger.info("No peers found, becoming leader")
            await self._become_leader()
            return
        
        if not self._send_vote_request:
            logger.warning("No vote request handler set, cannot start election")
            self.state.end_election()
            return
        
        # Request votes from all peers concurrently
        votes_needed = (len(peers) + 1) // 2 + 1  # Majority including self
        logger.info(f"Requesting votes from {len(peers)} peers, need {votes_needed}")
        
        vote_tasks = []
        for peer in peers:
            request = VoteRequest(
                term=term,
                candidate_id=self.state.node_id,
                last_log_index=0,  # TODO: Implement log replication
                last_log_term=0,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            task = asyncio.create_task(
                self._request_vote_from_peer(peer.node_id, request)
            )
            vote_tasks.append(task)
        
        # Wait for all votes or timeout
        try:
            await asyncio.wait_for(
                self._collect_votes(vote_tasks, votes_needed),
                timeout=self.max_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Election timed out, will retry")
            self.state.end_election()
            await self._start_election_timer()
    
    async def _request_vote_from_peer(
        self,
        peer_id: str,
        request: VoteRequest
    ) -> Optional[VoteResponse]:
        """Request vote from a single peer."""
        try:
            response = await self._send_vote_request(request)
            return response
        except Exception as e:
            logger.warning(f"Failed to get vote from {peer_id}: {e}")
            return None
    
    async def _collect_votes(
        self,
        tasks: list,
        votes_needed: int
    ) -> None:
        """Collect votes and determine if we won."""
        pending = set(tasks)
        
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED
            )
            
            for task in done:
                try:
                    response = task.result()
                    if response and response.vote_granted:
                        self.state.record_vote(response.voter_id)
                        current_votes = self.state.get_vote_count()
                        logger.debug(f"Vote granted, have {current_votes}/{votes_needed}")
                        
                        if current_votes >= votes_needed:
                            await self._become_leader()
                            return
                except Exception as e:
                    logger.warning(f"Error processing vote: {e}")
        
        # All tasks completed but not enough votes
        logger.info(f"Election lost, got {self.state.get_vote_count()} votes")
        self.state.end_election()
        await self._start_election_timer()
    
    async def _become_leader(self) -> None:
        """Transition to leader state."""
        logger.info(f"Becoming leader for term {self.state.get_term()}")
        
        # Cancel election timer
        if self._election_timer:
            self._election_timer.cancel()
            self._election_timer = None
        
        # Update state
        self.state.set_state(NodeState.LEADER)
        self.state.set_leader(
            self.state.node_id,
            self.state.get_term()
        )
        self.state.end_election()
        
        # Start heartbeat loop
        await self._start_heartbeat_loop()
        
        # Notify callback
        if self._on_become_leader:
            try:
                await self._on_become_leader()
            except Exception as e:
                logger.error(f"Error in become leader callback: {e}")
    
    async def _step_down(self) -> None:
        """Step down from leadership to follower."""
        logger.info("Stepping down from leadership")
        
        # Cancel heartbeat
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        
        # Update state
        self.state.set_state(NodeState.FOLLOWER)
        self.state.clear_leader()
        
        # Notify callback
        if self._on_step_down:
            try:
                await self._on_step_down()
            except Exception as e:
                logger.error(f"Error in step down callback: {e}")
        
        # Restart election timer
        await self._start_election_timer()
    
    # === RPC Handlers ===
    
    async def handle_vote_request(self, request: VoteRequest) -> VoteResponse:
        """Handle RequestVote RPC from candidate.
        
        Args:
            request: Vote request from candidate
            
        Returns:
            Vote response (granted or denied)
        """
        current_term = self.state.get_term()
        
        # If request term < current term, reject
        if request.term < current_term:
            logger.debug(f"Rejecting vote: term {request.term} < {current_term}")
            return VoteResponse(
                term=current_term,
                vote_granted=False,
                voter_id=self.state.node_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        
        # If request term > current term, update term and become follower
        if request.term > current_term:
            logger.info(f"Received higher term {request.term}, stepping down")
            self.state.current_term = request.term
            self.state.set_state(NodeState.FOLLOWER)
            self.state.voted_for = None
            self.state.clear_leader()
            await self._start_election_timer()
        
        # Check if we can vote for this candidate
        can_vote = (
            not self.state.has_voted() or
            self.state.voted_for == request.candidate_id
        )
        
        # TODO: Check log consistency (last_log_index, last_log_term)
        log_ok = True
        
        if can_vote and log_ok:
            self.state.cast_vote(request.candidate_id)
            await self._start_election_timer()  # Reset timer
            
            logger.info(f"Voted for {request.candidate_id} in term {request.term}")
            return VoteResponse(
                term=request.term,
                vote_granted=True,
                voter_id=self.state.node_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        
        logger.debug(f"Rejected vote for {request.candidate_id}")
        return VoteResponse(
            term=request.term,
            vote_granted=False,
            voter_id=self.state.node_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    
    async def handle_heartbeat(self, health: HealthStatus) -> bool:
        """Handle heartbeat/AppendEntries from leader.
        
        Args:
            health: Health status from leader
            
        Returns:
            True if heartbeat accepted
        """
        current_term = self.state.get_term()
        
        # If leader term < current term, reject
        if health.term < current_term:
            logger.debug(f"Rejecting heartbeat: term {health.term} < {current_term}")
            return False
        
        # If leader term > current term, update term
        if health.term > current_term:
            logger.info(f"Received higher term {health.term} from leader")
            self.state.current_term = health.term
            self.state.voted_for = None
        
        # If we are candidate or leader, step down
        if self.state.current_state in (NodeState.CANDIDATE, NodeState.LEADER):
            logger.info(f"Stepping down, {health.node_id} is leader for term {health.term}")
            await self._step_down()
        
        # Update leader info
        self.state.set_leader(health.node_id, health.term)
        await self._reset_election_timer()
        
        logger.debug(f"Heartbeat accepted from {health.node_id}")
        return True
