"""FastAPI HTTP API for cluster synchronization.

Provides endpoints for:
- User synchronization between nodes
- Health checks
- Traffic statistics aggregation
- Leader election voting
"""

import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bot.models.cluster import (
    SyncUserRequest,
    SyncUserResponse,
    HealthStatus,
    NodeState,
    VoteRequest,
    VoteResponse,
    TrafficStats,
)
from bot.core.cluster.state import ClusterState

logger = logging.getLogger(__name__)


# === Pydantic Models for API ===

class UserSyncPayload(BaseModel):
    """Payload for user synchronization."""
    chat_id: str
    username: Optional[str] = None
    uuid: Optional[str] = None
    email: Optional[str] = None
    status: str = "active"
    lang: str = "ru"
    platform: Optional[str] = None
    subscription_expiry: Optional[str] = None
    limit_ip: int = 1
    quota_gb: float = 5.0


class ClientConfigPayload(BaseModel):
    """Payload for X-UI client configuration."""
    id: str
    flow: str = "xtls-rprx-vision"
    email: str
    limitIp: int = 1
    totalGB: int = 0
    expiryTime: int = 0
    enable: bool = True


class SyncUserRequestPayload(BaseModel):
    """API payload for sync user request."""
    user: UserSyncPayload
    client_config: ClientConfigPayload
    source_node_id: str
    timestamp: str
    signature: str


class SyncUserResponsePayload(BaseModel):
    """API response for sync user request."""
    success: bool
    node_id: str
    message: str
    timestamp: str


class HealthStatusPayload(BaseModel):
    """API payload for health status."""
    node_id: str
    state: str
    term: int
    is_leader: bool
    last_heartbeat: str
    db_status: bool
    xui_status: bool
    uptime_seconds: int
    timestamp: str


class VoteRequestPayload(BaseModel):
    """API payload for vote request."""
    term: int
    candidate_id: str
    last_log_index: int = 0
    last_log_term: int = 0
    timestamp: str


class VoteResponsePayload(BaseModel):
    """API response for vote request."""
    term: int
    vote_granted: bool
    voter_id: str
    timestamp: str


class TrafficStatsPayload(BaseModel):
    """API payload for traffic statistics."""
    email: str
    upload_bytes: int
    download_bytes: int
    node_id: str
    timestamp: str


# === Authentication ===

class HMACAuth:
    """HMAC authentication for cluster API."""
    
    def __init__(self, secret: str):
        self.secret = secret
    
    def sign(self, payload: str) -> str:
        """Sign payload with HMAC-SHA256."""
        return hmac.new(
            self.secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
    
    def verify(self, payload: str, signature: str) -> bool:
        """Verify payload signature."""
        expected = self.sign(payload)
        return hmac.compare_digest(expected, signature)
    
    async def __call__(
        self,
        request: Request,
        x_signature: str = Header(..., alias="X-Signature"),
        x_node_id: str = Header(..., alias="X-Node-ID"),
    ) -> str:
        """FastAPI dependency for HMAC authentication.
        
        Returns:
            node_id if authentication successful
            
        Raises:
            HTTPException: if authentication fails
        """
        # Read request body
        body = await request.body()
        
        # Verify signature
        if not self.verify(body.decode(), x_signature):
            logger.warning(f"Invalid HMAC signature from {x_node_id}")
            raise HTTPException(status_code=401, detail="Invalid signature")
        
        return x_node_id


# === API Factory ===

def create_sync_api(
    cluster_state: ClusterState,
    auth: HMACAuth,
    on_sync_user: Optional[callable] = None,
    on_get_health: Optional[callable] = None,
    on_vote_request: Optional[callable] = None,
    on_traffic_stats: Optional[callable] = None,
) -> FastAPI:
    """Create FastAPI application for cluster synchronization.
    
    Args:
        cluster_state: Shared cluster state
        auth: HMAC authentication instance
        on_sync_user: Callback for user sync
        on_get_health: Callback for health check
        on_vote_request: Callback for vote request
        on_traffic_stats: Callback for traffic stats
        
    Returns:
        FastAPI application instance
    """
    app = FastAPI(
        title="VPN Cluster Sync API",
        description="API for synchronizing VPN nodes in a cluster",
        version="1.0.0",
    )
    
    # === Health Check ===
    
    @app.get("/health", response_model=HealthStatusPayload)
    async def health_check(
        x_node_id: str = Header(..., alias="X-Node-ID"),
    ) -> HealthStatusPayload:
        """Get health status of this node.
        
        Used by peers to check node health and for leader heartbeats.
        """
        health = HealthStatus(
            node_id=cluster_state.node_id,
            state=cluster_state.current_state,
            term=cluster_state.get_term(),
            is_leader=cluster_state.is_leader(),
            last_heartbeat=cluster_state.get_leader_last_seen() or "",
            db_status=True,  # TODO: Check actual DB status
            xui_status=True,  # TODO: Check actual X-UI status
            uptime_seconds=0,  # TODO: Track uptime
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        
        # Notify callback if set
        if on_get_health:
            try:
                await on_get_health(health, x_node_id)
            except Exception as e:
                logger.error(f"Error in health callback: {e}")
        
        return HealthStatusPayload(
            node_id=health.node_id,
            state=health.state.value,
            term=health.term,
            is_leader=health.is_leader,
            last_heartbeat=health.last_heartbeat,
            db_status=health.db_status,
            xui_status=health.xui_status,
            uptime_seconds=health.uptime_seconds,
            timestamp=health.timestamp,
        )
    
    # === User Synchronization ===
    
    @app.post("/sync/user", response_model=SyncUserResponsePayload)
    async def sync_user(
        payload: SyncUserRequestPayload,
        authenticated_node_id: str = Depends(auth),
    ) -> SyncUserResponsePayload:
        """Receive user synchronization from peer node.
        
        Creates or updates user on this node based on data from peer.
        Requires HMAC authentication.
        """
        logger.info(f"Received user sync from {authenticated_node_id}")
        
        try:
            # Convert payload to internal model
            from bot.models import User
            
            user = User(
                chat_id=payload.user.chat_id,
                username=payload.user.username,
                uuid=payload.user.uuid,
                email=payload.user.email,
                status=payload.user.status,
                lang=payload.user.lang,
                platform=payload.user.platform,
                subscription_expiry=payload.user.subscription_expiry,
                limit_ip=payload.user.limit_ip,
                quota_gb=payload.user.quota_gb,
            )
            
            client_config = payload.client_config.model_dump()
            
            # Create sync request
            sync_request = SyncUserRequest(
                user=user,
                client_config=client_config,
                source_node_id=payload.source_node_id,
                timestamp=payload.timestamp,
                signature=payload.signature,
            )
            
            # Call handler if set
            if on_sync_user:
                success = await on_sync_user(sync_request)
            else:
                success = True
            
            return SyncUserResponsePayload(
                success=success,
                node_id=cluster_state.node_id,
                message="User synced successfully" if success else "Sync failed",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            
        except Exception as e:
            logger.exception(f"Error syncing user: {e}")
            return SyncUserResponsePayload(
                success=False,
                node_id=cluster_state.node_id,
                message=f"Error: {str(e)}",
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
    
    # === Leader Election Voting ===
    
    @app.post("/vote", response_model=VoteResponsePayload)
    async def request_vote(
        payload: VoteRequestPayload,
        x_node_id: str = Header(..., alias="X-Node-ID"),
    ) -> VoteResponsePayload:
        """Handle vote request from candidate node.
        
        Part of RAFT leader election algorithm.
        """
        logger.debug(f"Received vote request from {payload.candidate_id}")
        
        try:
            # Convert payload to internal model
            vote_request = VoteRequest(
                term=payload.term,
                candidate_id=payload.candidate_id,
                last_log_index=payload.last_log_index,
                last_log_term=payload.last_log_term,
                timestamp=payload.timestamp,
            )
            
            # Call handler if set
            if on_vote_request:
                vote_response = await on_vote_request(vote_request)
            else:
                # Default: deny vote
                vote_response = VoteResponse(
                    term=cluster_state.get_term(),
                    vote_granted=False,
                    voter_id=cluster_state.node_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            
            return VoteResponsePayload(
                term=vote_response.term,
                vote_granted=vote_response.vote_granted,
                voter_id=vote_response.voter_id,
                timestamp=vote_response.timestamp,
            )
            
        except Exception as e:
            logger.exception(f"Error handling vote request: {e}")
            return VoteResponsePayload(
                term=cluster_state.get_term(),
                vote_granted=False,
                voter_id=cluster_state.node_id,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
    
    # === Traffic Statistics ===
    
    @app.post("/sync/traffic")
    async def sync_traffic(
        payload: TrafficStatsPayload,
        authenticated_node_id: str = Depends(auth),
    ) -> dict:
        """Receive traffic statistics from peer node.
        
        Used for aggregating traffic across all nodes.
        Requires HMAC authentication.
        """
        logger.debug(f"Received traffic stats from {authenticated_node_id}")
        
        try:
            # Convert payload to internal model
            stats = TrafficStats(
                email=payload.email,
                upload_bytes=payload.upload_bytes,
                download_bytes=payload.download_bytes,
                node_id=payload.node_id,
                timestamp=payload.timestamp,
            )
            
            # Call handler if set
            if on_traffic_stats:
                await on_traffic_stats(stats)
            
            return {"status": "ok"}
            
        except Exception as e:
            logger.exception(f"Error handling traffic stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # === Cluster Status ===
    
    @app.get("/cluster/status")
    async def cluster_status(
        x_node_id: str = Header(..., alias="X-Node-ID"),
    ) -> dict:
        """Get full cluster status.
        
        Returns information about all known nodes and current leader.
        """
        leader = cluster_state.get_leader()
        
        return {
            "node_id": cluster_state.node_id,
            "current_state": cluster_state.current_state.value,
            "current_term": cluster_state.get_term(),
            "is_leader": cluster_state.is_leader(),
            "leader": leader.to_dict() if leader else None,
            "nodes": {
                node_id: node.to_dict()
                for node_id, node in cluster_state.nodes.items()
            },
            "exit_nodes": list(cluster_state.exit_nodes.keys()),
            "entry_nodes": list(cluster_state.entry_nodes.keys()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    
    # === Error Handlers ===
    
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        """Global exception handler."""
        logger.exception(f"Unhandled exception in API: {exc}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Internal server error",
                "detail": str(exc) if str(exc) else "Unknown error",
            },
        )
    
    return app


# === Standalone Server ===

async def run_sync_api_server(
    app: FastAPI,
    host: str = "0.0.0.0",
    port: int = 8081,
) -> None:
    """Run the sync API server using uvicorn.
    
    Args:
        app: FastAPI application
        host: Host to bind to
        port: Port to bind to
    """
    import uvicorn
    
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    
    logger.info(f"Starting sync API server on {host}:{port}")
    await server.serve()
