"""Node Cluster Manager - High-level service for multi-node coordination.
from bot.utils.log_redaction import redact_email

Integrates all cluster components:
- ClusterState for shared state
- LeaderElection for leader management
- NodeSyncClient for peer communication
- Sync API for receiving requests
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Callable

from bot.config import Settings
from bot.models import User
from bot.models.cluster import (
    ClusterNode,
    NodeRole,
    NodeState,
    SyncUserRequest,
    SyncUserResponse,
    HealthStatus,
    VoteRequest,
    VoteResponse,
    TrafficStats,
    AggregatedTraffic,
)
from bot.core.cluster import (
    ClusterState,
    LeaderElection,
    NodeSyncClient,
    create_sync_api,
    HMACAuth,
    run_sync_api_server,
)
from bot.core.database import Database
from bot.services.xui_service import XUIService

logger = logging.getLogger(__name__)


class NodeClusterManager:
    """Main service for multi-node cluster coordination.
    
    Manages:
    - Leader election and failover
    - User synchronization across nodes
    - Health monitoring
    - Traffic aggregation
    
    Usage:
        config = Settings()
        manager = NodeClusterManager(config)
        await manager.start()
        
        # Sync user to cluster
        await manager.sync_user(user, client_config)
        
        # Get aggregated traffic
        traffic = await manager.get_aggregated_traffic(email)
    """
    
    def __init__(
        self,
        config: Settings,
        db: Optional[Database] = None,
        xui_service: Optional[XUIService] = None,
    ):
        """Initialize cluster manager.
        
        Args:
            config: Application settings
            db: Database instance (optional)
            xui_service: X-UI service for adding users (optional)
        """
        self.config = config
        self.db = db
        self.xui_service = xui_service
        
        # Uptime tracking
        self._start_time = datetime.now(timezone.utc)
        
        # Node identification
        self.node_id = getattr(config, 'NODE_ID', 'exit-1')
        self.cluster_secret = getattr(config, 'CLUSTER_SECRET', 'default-secret')
        
        # API settings
        self.api_port = getattr(config, 'SYNC_API_PORT', 8081)
        self.api_host = getattr(config, 'SYNC_API_HOST', '0.0.0.0')
        self.sync_timeout = getattr(config, 'SYNC_TIMEOUT_SECONDS', 10.0)
        
        # Election settings
        self.election_timeout_min = getattr(config, 'ELECTION_TIMEOUT_MIN', 5.0)
        self.election_timeout_max = getattr(config, 'ELECTION_TIMEOUT_MAX', 10.0)
        self.heartbeat_interval = getattr(config, 'HEARTBEAT_INTERVAL', 2.0)
        
        # Initialize components
        self.state = ClusterState(node_id=self.node_id)
        
        self.election = LeaderElection(
            state=self.state,
            min_timeout=self.election_timeout_min,
            max_timeout=self.election_timeout_max,
            heartbeat_interval=self.heartbeat_interval,
        )
        
        self.client = NodeSyncClient(
            node_id=self.node_id,
            secret=self.cluster_secret,
            timeout=self.sync_timeout,
        )
        
        self.auth = HMACAuth(self.cluster_secret)
        
        # API server
        self.api = None
        self._api_task: Optional[asyncio.Task] = None
        
        # Background tasks
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._health_check_task: Optional[asyncio.Task] = None
        
        # Callbacks
        self._on_user_sync: Optional[Callable[[User, dict], None]] = None
    
    def set_user_sync_callback(self, callback: Callable[[User, dict], None]) -> None:
        """Set callback for user sync events."""
        self._on_user_sync = callback
    
    # === Lifecycle ===
    
    async def start(self) -> None:
        """Start cluster manager and all components."""
        if self._running:
            return
        
        logger.info(f"Starting NodeClusterManager for {self.node_id}")
        self._running = True
        self._shutdown_event.clear()
        
        # Load peer nodes from config
        await self._load_peers()
        
        # Start sync client
        await self.client.start()
        
        # Setup election callbacks
        self.election.on_become_leader(self._on_become_leader)
        self.election.on_step_down(self._on_step_down)
        self.election.set_vote_request_handler(self._handle_vote_request)
        self.election.set_heartbeat_handler(self._send_heartbeats)
        
        # Start election manager
        await self.election.start()
        
        # Start API server
        await self._start_api_server()
        
        # Start health check loop
        self._health_check_task = asyncio.create_task(
            self._health_check_loop()
        )
        
        logger.info("NodeClusterManager started successfully")
    
    async def stop(self) -> None:
        """Stop cluster manager and all components."""
        if not self._running:
            return
        
        logger.info("Stopping NodeClusterManager")
        self._running = False
        self._shutdown_event.set()
        
        # Cancel background tasks
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        
        # Stop election
        await self.election.stop()
        
        # Stop client
        await self.client.stop()
        
        # Stop API server
        if self._api_task:
            self._api_task.cancel()
            try:
                await self._api_task
            except asyncio.CancelledError:
                pass
        
        logger.info("NodeClusterManager stopped")
    
    async def _load_peers(self) -> None:
        """Load peer nodes from configuration."""
        peers_str = getattr(self.config, 'EXIT_NODE_PEERS', '')
        if not peers_str:
            logger.warning("No peer nodes configured")
            return
        
        # Format: node_id:http://host:port,node_id:http://host:port
        for peer_str in peers_str.split(','):
            peer_str = peer_str.strip()
            if not peer_str:
                continue
            
            try:
                node_id, url = peer_str.split(':', 1)
                # Reconstruct URL (split removed part of it)
                url = peer_str[len(node_id)+1:]
                
                # Parse URL
                from urllib.parse import urlparse
                parsed = urlparse(url)
                host = parsed.hostname
                port = parsed.port or 8081
                
                peer = ClusterNode(
                    node_id=node_id,
                    role=NodeRole.EXIT,
                    host=host,
                    api_port=port,
                )
                
                self.client.add_peer(peer)
                self.state.add_node(peer)
                
                logger.info(f"Loaded peer: {node_id} at {host}:{port}")
                
            except Exception as e:
                logger.error(f"Failed to parse peer config '{peer_str}': {e}")
    
    async def _start_api_server(self) -> None:
        """Start sync API server."""
        self.api = create_sync_api(
            cluster_state=self.state,
            auth=self.auth,
            on_sync_user=self._handle_user_sync_request,
            on_get_health=self._handle_health_request,
            on_vote_request=self._handle_vote_request_api,
            on_traffic_stats=self._handle_traffic_stats,
        )
        
        # Start server in background
        self._api_task = asyncio.create_task(
            run_sync_api_server(
                self.api,
                host=self.api_host,
                port=self.api_port,
            )
        )
        
        logger.info(f"Sync API server started on {self.api_host}:{self.api_port}")
    
    # === Callback Handlers ===
    
    async def _on_become_leader(self) -> None:
        """Called when this node becomes leader."""
        logger.info(f"🎉 {self.node_id} became leader for term {self.state.get_term()}")
        
        # Trigger full sync if needed
        if self.db:
            await self._sync_all_users_to_peers()
    
    async def _on_step_down(self) -> None:
        """Called when this node steps down from leadership."""
        logger.info(f"👋 {self.node_id} stepped down from leadership")
    
    async def _handle_vote_request(self, request: VoteRequest) -> VoteResponse:
        """Handle vote request (delegates to election)."""
        return await self.election.handle_vote_request(request)
    
    async def _handle_vote_request_api(self, request: VoteRequest) -> VoteResponse:
        """Handle vote request from API."""
        return await self.election.handle_vote_request(request)
    
    async def _handle_user_sync_request(
        self,
        request: SyncUserRequest,
    ) -> bool:
        """Handle user sync request from peer.
        
        Creates or updates user in local database.
        """
        logger.info(
            f"Received user sync from {request.source_node_id}: "
            f"{request.user.chat_id}"
        )
        
        try:
            if self.db:
                # Save user to database
                self.db.save_user(request.user)
                
                # Add to X-UI if configured
                if self.xui_service and request.client_config:
                    try:
                        await self.xui_service.add_client(request.client_config)
                        logger.debug(f"Added user {request.user.chat_id} to X-UI")
                    except Exception as e:
                        logger.warning(f"Failed to add user to X-UI: {e}")
            
            # Notify callback if set
            if self._on_user_sync:
                self._on_user_sync(request.user, request.client_config)
            
            return True
            
        except Exception as e:
            logger.exception(f"Failed to handle user sync: {e}")
            return False
    
    async def _handle_health_request(
        self,
        health: HealthStatus,
        from_node_id: str,
    ) -> None:
        """Handle health check from peer."""
        logger.debug(f"Received health check from {from_node_id}")
        
        # If this is from leader, handle as heartbeat
        if health.is_leader:
            await self.election.handle_heartbeat(health)
    
    async def _handle_traffic_stats(self, stats: TrafficStats) -> None:
        """Handle traffic stats from peer."""
        logger.debug(f"Received traffic stats from {stats.node_id} for {redact_email(stats.email)}")
        
        # Store in database for aggregation
        if self.db:
            try:
                await asyncio.to_thread(
                    self.db.record_traffic,
                    stats.email,
                    stats.upload_bytes,
                    stats.download_bytes
                )
            except Exception as e:
                logger.warning(f"Failed to record traffic stats: {e}")
    
    async def _check_db_health(self) -> bool:
        """Check database connectivity."""
        if not self.db:
            return False
        try:
            # Simple health check query
            await asyncio.to_thread(self.db.get_stats)
            return True
        except Exception:
            return False
    
    async def _check_xui_health(self) -> bool:
        """Check X-UI service connectivity."""
        if not self.xui_service:
            return False
        try:
            # Try to get inbounds as health check
            await self.xui_service.get_inbounds()
            return True
        except Exception:
            return False
    
    def _get_uptime_seconds(self) -> int:
        """Get node uptime in seconds."""
        return int((datetime.now(timezone.utc) - self._start_time).total_seconds())
    
    async def _send_heartbeats(self) -> bool:
        """Send heartbeats to all peers (leader only)."""
        # Check actual service health
        db_status = await self._check_db_health()
        xui_status = await self._check_xui_health()
        uptime_seconds = self._get_uptime_seconds()
        
        health = HealthStatus(
            node_id=self.node_id,
            state=self.state.current_state,
            term=self.state.get_term(),
            is_leader=self.state.is_leader(),
            last_heartbeat=datetime.now(timezone.utc).isoformat(),
            db_status=db_status,
            xui_status=xui_status,
            uptime_seconds=uptime_seconds,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        # Send to all peers concurrently
        tasks = [
            asyncio.create_task(
                self._send_heartbeat_to_peer(node.node_id, health),
                name=node.node_id,
            )
            for node in self.client.get_all_peers()
        ]
        
        if tasks:
            done, _ = await asyncio.wait(tasks, timeout=5.0)
            success_count = sum(
                1 for task in done
                if not task.exception() and task.result()
            )
            logger.debug(f"Sent heartbeats to {success_count}/{len(tasks)} peers")
        
        return True
    
    async def _send_heartbeat_to_peer(
        self,
        node_id: str,
        health: HealthStatus,
    ) -> bool:
        """Send heartbeat to a single peer.
        
        NOTE: Currently uses health check as proxy for heartbeat.
        A proper implementation would POST health data to a /heartbeat endpoint.
        For now, we check peer health which serves as implicit heartbeat acknowledgment.
        """
        try:
            # TODO: Implement proper POST /heartbeat endpoint on peers
            # For now, health check serves as implicit heartbeat
            peer_health = await self.client.get_health(node_id)
            if peer_health:
                # Update peer status based on their health response
                self.state.update_node_status(node_id, "active")
            return peer_health is not None
        except Exception as e:
            logger.debug(f"Failed to send heartbeat to {node_id}: {e}")
            self.state.update_node_status(node_id, "unreachable")
            return False
    
    async def _health_check_loop(self) -> None:
        """Background loop for health checks and peer discovery."""
        while self._running:
            try:
                # Check all peers
                health_results = await self.client.check_all_peers()
                
                for node_id, health in health_results.items():
                    if health:
                        self.state.update_node_status(node_id, "active")
                    else:
                        self.state.update_node_status(node_id, "unreachable")
                
                # Wait for next check
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=30.0,  # Check every 30 seconds
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in health check loop: {e}")
                await asyncio.sleep(5.0)
    
    # === Public API ===
    
    async def sync_user(
        self,
        user: User,
        client_config: dict,
    ) -> Dict[str, bool]:
        """Sync user to all peer nodes.
        
        Args:
            user: User to sync
            client_config: X-UI client configuration
            
        Returns:
            Dictionary mapping node_id to success status
        """
        # If not leader, forward to leader
        if not self.state.is_leader():
            leader_id = self.get_leader_id()
            if leader_id:
                logger.info(f"Forwarding user sync to leader {leader_id}")
                # Create sync request
                request = SyncUserRequest.create(
                    user=user,
                    client_config=client_config,
                    source_node_id=self.node_id,
                    secret=self.cluster_secret,
                )
                # Send to leader only
                result = await self.client.sync_user(leader_id, request)
                return {leader_id: result} if result is not None else {}
            else:
                logger.warning("No leader available for user sync")
                return {}
        
        # Create sync request
        request = SyncUserRequest.create(
            user=user,
            client_config=client_config,
            source_node_id=self.node_id,
            secret=self.cluster_secret,
        )
        
        # Sync to all peers
        results = await self.client.sync_user_to_all(request)
        
        success_count = sum(1 for success in results.values() if success)
        logger.info(
            f"User sync completed: {success_count}/{len(results)} peers succeeded"
        )
        
        return results
    
    async def _get_local_traffic(self, email: str) -> tuple:
        """Get local traffic stats for user from database.
        
        Returns:
            Tuple of (upload_bytes, download_bytes)
        """
        if not self.db:
            return 0, 0
        
        try:
            traffic = await asyncio.to_thread(self.db.get_traffic, email)
            if traffic:
                return traffic.get('upload', 0), traffic.get('download', 0)
        except Exception as e:
            logger.debug(f"Failed to get local traffic for {redact_email(email)}: {e}")
        
        return 0, 0
    
    async def get_aggregated_traffic(self, email: str) -> AggregatedTraffic:
        """Get aggregated traffic statistics for user from all nodes.
        
        Args:
            email: User email
            
        Returns:
            Aggregated traffic statistics
        """
        # Get local traffic from database
        local_upload, local_download = await self._get_local_traffic(email)
        
        # Get traffic from peers
        by_node = {
            self.node_id: {
                "upload": local_upload,
                "download": local_download,
            }
        }
        
        for node in self.client.get_all_peers():
            stats = await self.client.get_traffic_from_peer(node.node_id, email)
            if stats:
                by_node[node.node_id] = {
                    "upload": stats.upload_bytes,
                    "download": stats.download_bytes,
                }
        
        total_upload = sum(n["upload"] for n in by_node.values())
        total_download = sum(n["download"] for n in by_node.values())
        
        return AggregatedTraffic(
            email=email,
            total_upload=total_upload,
            total_download=total_download,
            by_node=by_node,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    
    def is_leader(self) -> bool:
        """Check if this node is the leader."""
        return self.state.is_leader()
    
    def get_leader_id(self) -> Optional[str]:
        """Get current leader node ID."""
        leader = self.state.get_leader()
        return leader.node_id if leader else None
    
    def get_cluster_status(self) -> dict:
        """Get full cluster status."""
        leader = self.state.get_leader()
        
        return {
            "node_id": self.node_id,
            "is_leader": self.state.is_leader(),
            "current_term": self.state.get_term(),
            "current_state": self.state.current_state.value,
            "leader_id": leader.node_id if leader else None,
            "peers": {
                node_id: {
                    "host": node.host,
                    "status": node.status,
                }
                for node_id, node in self.state.nodes.items()
            },
            "api_port": self.api_port,
        }
    
    async def _sync_all_users_to_peers(self) -> None:
        """Sync all users to all peers (called on becoming leader)."""
        if not self.db:
            return
        
        logger.info("Starting full user sync to all peers")
        
        try:
            # Get all active users from database
            users = await asyncio.to_thread(
                self.db.get_users_by_status, 'active'
            )
            
            if not users:
                logger.info("No active users to sync")
                return
            
            logger.info(f"Syncing {len(users)} users to all peers")
            
            # Sync each user to all peers
            sync_count = 0
            for user in users:
                try:
                    # Create minimal client config (UUID only for now)
                    client_config = {
                        'id': user.uuid,
                        'email': user.email,
                    }
                    
                    request = SyncUserRequest.create(
                        user=user,
                        client_config=client_config,
                        source_node_id=self.node_id,
                        secret=self.cluster_secret,
                    )
                    
                    results = await self.client.sync_user_to_all(request)
                    if any(results.values()):
                        sync_count += 1
                    
                    # Small delay to avoid overwhelming peers
                    await asyncio.sleep(0.1)
                    
                except Exception as e:
                    logger.warning(f"Failed to sync user {user.chat_id}: {e}")
            
            logger.info(f"Full user sync completed: {sync_count}/{len(users)} users synced")
            
        except Exception as e:
            logger.error(f"Failed to sync all users: {e}")
