"""Async HTTP client for cluster synchronization.

Provides client for communicating with peer nodes via HTTP API.
Features:
- Connection pooling via aiohttp
- Automatic retry with exponential backoff
- HMAC request signing
- Circuit breaker pattern
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable

import aiohttp
from tenacity import (
    AsyncRetrying,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from bot.models.cluster import (
    ClusterNode,
    SyncUserRequest,
    SyncUserResponse,
    HealthStatus,
    VoteRequest,
    VoteResponse,
    TrafficStats,
)

logger = logging.getLogger(__name__)


class NodeSyncClient:
    """HTTP client for syncing with peer nodes.
    
    Manages connections to all peer nodes and provides methods for:
    - User synchronization
    - Health checks
    - Leader election voting
    - Traffic statistics aggregation
    
    Attributes:
        node_id: ID of this node
        secret: Shared secret for HMAC signing
        session: aiohttp ClientSession
        peers: Dictionary of peer nodes
        timeout: Request timeout in seconds
    """
    
    def __init__(
        self,
        node_id: str,
        secret: str,
        timeout: float = 10.0,
        max_retries: int = 5,
    ):
        self.node_id = node_id
        self.secret = secret
        self.timeout = timeout
        self.max_retries = max_retries
        
        self.session: Optional[aiohttp.ClientSession] = None
        self.peers: Dict[str, ClusterNode] = {}
        
        # Circuit breaker state
        self._failed_peers: Dict[str, int] = {}
        self._circuit_threshold = 3
        self._circuit_timeout = 60  # seconds
        self._circuit_open_until: Dict[str, float] = {}
    
    async def start(self) -> None:
        """Initialize HTTP session."""
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self.session = aiohttp.ClientSession(timeout=timeout)
            logger.debug("HTTP session initialized")
    
    async def stop(self) -> None:
        """Close HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
            logger.debug("HTTP session closed")
    
    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.stop()
    
    # === Peer Management ===
    
    def add_peer(self, node: ClusterNode) -> None:
        """Add a peer node."""
        self.peers[node.node_id] = node
        logger.debug(f"Added peer: {node.node_id} at {node.api_url}")
    
    def remove_peer(self, node_id: str) -> bool:
        """Remove a peer node."""
        if node_id in self.peers:
            del self.peers[node_id]
            self._failed_peers.pop(node_id, None)
            self._circuit_open_until.pop(node_id, None)
            logger.debug(f"Removed peer: {node_id}")
            return True
        return False
    
    def get_peer(self, node_id: str) -> Optional[ClusterNode]:
        """Get peer node by ID."""
        return self.peers.get(node_id)
    
    def get_all_peers(self) -> List[ClusterNode]:
        """Get all peer nodes."""
        return list(self.peers.values())
    
    # === Circuit Breaker ===
    
    def _is_circuit_open(self, node_id: str) -> bool:
        """Check if circuit breaker is open for a peer."""
        if node_id not in self._circuit_open_until:
            return False
        
        if datetime.now(timezone.utc).timestamp() < self._circuit_open_until[node_id]:
            return True
        
        # Circuit timeout expired, close it
        del self._circuit_open_until[node_id]
        self._failed_peers[node_id] = 0
        return False
    
    def _record_success(self, node_id: str) -> None:
        """Record successful request to peer."""
        if node_id in self._failed_peers:
            del self._failed_peers[node_id]
    
    def _record_failure(self, node_id: str) -> None:
        """Record failed request to peer."""
        self._failed_peers[node_id] = self._failed_peers.get(node_id, 0) + 1
        
        if self._failed_peers[node_id] >= self._circuit_threshold:
            # Open circuit
            self._circuit_open_until[node_id] = (
                datetime.now(timezone.utc).timestamp() + self._circuit_timeout
            )
            logger.warning(
                f"Circuit breaker opened for {node_id} "
                f"(until {self._circuit_timeout}s)"
            )
    
    # === Request Signing ===
    
    def _sign_request(self, payload: dict) -> str:
        """Sign request payload with HMAC-SHA256."""
        import hashlib
        import hmac
        
        body = json.dumps(payload, sort_keys=True, separators=(',', ':'))
        signature = hmac.new(
            self.secret.encode(),
            body.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    
    def _prepare_headers(self, body: str) -> dict:
        """Prepare request headers with HMAC signature."""
        return {
            "Content-Type": "application/json",
            "X-Node-ID": self.node_id,
            "X-Signature": self._sign_request(json.loads(body)),
        }
    
    # === Core Request Method ===
    
    async def _request(
        self,
        node: ClusterNode,
        method: str,
        path: str,
        payload: Optional[dict] = None,
    ) -> dict:
        """Make HTTP request to peer node with retry.
        
        Args:
            node: Target peer node
            method: HTTP method (GET, POST, etc.)
            path: API path
            payload: Request body (optional)
            
        Returns:
            Response JSON as dictionary
            
        Raises:
            aiohttp.ClientError: On connection error
            asyncio.TimeoutError: On timeout
        """
        if self.session is None or self.session.closed:
            raise RuntimeError("HTTP session not initialized")
        
        # Check circuit breaker
        if self._is_circuit_open(node.node_id):
            raise aiohttp.ClientError(
                f"Circuit breaker open for {node.node_id}"
            )
        
        url = f"{node.api_url}{path}"
        body = json.dumps(payload) if payload else None
        
        headers = {"X-Node-ID": self.node_id}
        if body:
            headers["Content-Type"] = "application/json"
            headers["X-Signature"] = self._sign_request(json.loads(body))
        
        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((
                aiohttp.ClientError,
                asyncio.TimeoutError,
            )),
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            before_sleep=before_sleep_log(logger, logging.WARNING),
            reraise=True,
        ):
            with attempt:
                try:
                    async with self.session.request(
                        method=method,
                        url=url,
                        headers=headers,
                        data=body,
                    ) as response:
                        response.raise_for_status()
                        
                        if response.status == 204:
                            return {}
                        
                        result = await response.json()
                        self._record_success(node.node_id)
                        return result
                        
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    self._record_failure(node.node_id)
                    raise
    
    # === User Synchronization ===
    
    async def sync_user(
        self,
        node_id: str,
        request: SyncUserRequest,
    ) -> Optional[SyncUserResponse]:
        """Sync user to a peer node.
        
        Args:
            node_id: Target peer node ID
            request: Sync request with user data
            
        Returns:
            Sync response or None on failure
        """
        node = self.get_peer(node_id)
        if not node:
            logger.error(f"Unknown peer: {node_id}")
            return None
        
        try:
            payload = {
                "user": {
                    "chat_id": request.user.chat_id,
                    "username": request.user.username,
                    "uuid": request.user.uuid,
                    "email": request.user.email,
                    "status": request.user.status,
                    "lang": request.user.lang,
                    "platform": request.user.platform,
                    "subscription_expiry": request.user.subscription_expiry,
                    "limit_ip": request.user.limit_ip,
                    "quota_gb": request.user.quota_gb,
                },
                "client_config": request.client_config,
                "source_node_id": request.source_node_id,
                "timestamp": request.timestamp,
                "signature": request.signature,
            }
            
            result = await self._request(node, "POST", "/sync/user", payload)
            
            return SyncUserResponse(
                success=result.get("success", False),
                node_id=result.get("node_id", ""),
                message=result.get("message", ""),
                timestamp=result.get("timestamp", ""),
            )
            
        except Exception as e:
            logger.error(f"Failed to sync user to {node_id}: {e}")
            return None
    
    async def sync_user_to_all(self, request: SyncUserRequest) -> Dict[str, bool]:
        """Sync user to all peer nodes.
        
        Args:
            request: Sync request
            
        Returns:
            Dictionary mapping node_id to success status
        """
        tasks = []
        for node in self.get_all_peers():
            task = asyncio.create_task(
                self.sync_user(node.node_id, request),
                name=node.node_id,
            )
            tasks.append(task)
        
        results = {}
        if tasks:
            done, _ = await asyncio.wait(tasks)
            for task in done:
                node_id = task.get_name()
                try:
                    response = task.result()
                    results[node_id] = response.success if response else False
                except Exception as e:
                    logger.error(f"Error syncing to {node_id}: {e}")
                    results[node_id] = False
        
        return results
    
    # === Health Checks ===
    
    async def get_health(self, node_id: str) -> Optional[HealthStatus]:
        """Get health status from peer node.
        
        Args:
            node_id: Target peer node ID
            
        Returns:
            Health status or None on failure
        """
        node = self.get_peer(node_id)
        if not node:
            logger.error(f"Unknown peer: {node_id}")
            return None
        
        try:
            result = await self._request(node, "GET", "/health")
            
            from bot.models.cluster import NodeState
            return HealthStatus(
                node_id=result.get("node_id", ""),
                state=NodeState(result.get("state", "follower")),
                term=result.get("term", 0),
                is_leader=result.get("is_leader", False),
                last_heartbeat=result.get("last_heartbeat", ""),
                db_status=result.get("db_status", False),
                xui_status=result.get("xui_status", False),
                uptime_seconds=result.get("uptime_seconds", 0),
                timestamp=result.get("timestamp", ""),
            )
            
        except Exception as e:
            logger.debug(f"Failed to get health from {node_id}: {e}")
            return None
    
    async def check_all_peers(self) -> Dict[str, Optional[HealthStatus]]:
        """Check health of all peer nodes.
        
        Returns:
            Dictionary mapping node_id to health status (or None)
        """
        tasks = [
            asyncio.create_task(self.get_health(node.node_id), name=node.node_id)
            for node in self.get_all_peers()
        ]
        
        results = {}
        if tasks:
            done, _ = await asyncio.wait(tasks)
            for task in done:
                node_id = task.get_name()
                try:
                    results[node_id] = task.result()
                except Exception as e:
                    logger.debug(f"Error checking {node_id}: {e}")
                    results[node_id] = None
        
        return results
    
    # === Leader Election ===
    
    async def request_vote(
        self,
        node_id: str,
        request: VoteRequest,
    ) -> Optional[VoteResponse]:
        """Request vote from peer node.
        
        Args:
            node_id: Target peer node ID
            request: Vote request
            
        Returns:
            Vote response or None on failure
        """
        node = self.get_peer(node_id)
        if not node:
            logger.error(f"Unknown peer: {node_id}")
            return None
        
        try:
            payload = {
                "term": request.term,
                "candidate_id": request.candidate_id,
                "last_log_index": request.last_log_index,
                "last_log_term": request.last_log_term,
                "timestamp": request.timestamp,
            }
            
            result = await self._request(node, "POST", "/vote", payload)
            
            return VoteResponse(
                term=result.get("term", 0),
                vote_granted=result.get("vote_granted", False),
                voter_id=result.get("voter_id", ""),
                timestamp=result.get("timestamp", ""),
            )
            
        except Exception as e:
            logger.warning(f"Failed to request vote from {node_id}: {e}")
            return None
    
    async def request_votes_from_all(self, request: VoteRequest) -> List[VoteResponse]:
        """Request votes from all peer nodes.
        
        Args:
            request: Vote request
            
        Returns:
            List of vote responses
        """
        tasks = [
            asyncio.create_task(
                self.request_vote(node.node_id, request),
                name=node.node_id,
            )
            for node in self.get_all_peers()
        ]
        
        responses = []
        if tasks:
            done, _ = await asyncio.wait(tasks)
            for task in done:
                try:
                    response = task.result()
                    if response:
                        responses.append(response)
                except Exception as e:
                    logger.debug(f"Error requesting vote: {e}")
        
        return responses
    
    # === Traffic Statistics ===
    
    async def send_traffic_stats(
        self,
        node_id: str,
        stats: TrafficStats,
    ) -> bool:
        """Send traffic statistics to peer node.
        
        Args:
            node_id: Target peer node ID
            stats: Traffic statistics
            
        Returns:
            True on success
        """
        node = self.get_peer(node_id)
        if not node:
            logger.error(f"Unknown peer: {node_id}")
            return False
        
        try:
            payload = {
                "email": stats.email,
                "upload_bytes": stats.upload_bytes,
                "download_bytes": stats.download_bytes,
                "node_id": stats.node_id,
                "timestamp": stats.timestamp,
            }
            
            await self._request(node, "POST", "/sync/traffic", payload)
            return True
            
        except Exception as e:
            logger.error(f"Failed to send traffic stats to {node_id}: {e}")
            return False
    
    async def get_traffic_from_peer(
        self,
        node_id: str,
        email: str,
    ) -> Optional[TrafficStats]:
        """Get traffic statistics for user from peer node.
        
        Args:
            node_id: Target peer node ID
            email: User email
            
        Returns:
            Traffic stats or None
        """
        # This would require a new endpoint on peers
        # For now, return None
        logger.debug(f"Getting traffic from {node_id} for {email} not implemented")
        return None
