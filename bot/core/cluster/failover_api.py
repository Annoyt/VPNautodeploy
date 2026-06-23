"""Failover API endpoints for Entry Node communication."""

import logging
import os
from typing import Optional, Callable, Dict, List
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Header, Depends

from bot.models.performance import ExitNodeStatus, FailoverEvent, FailoverDecision
from .failover_schemas import (
    ExitNodeStatusResponse,
    AllExitNodesStatusResponse,
    FailoverEventRequest,
    FailoverEventResponse,
    FailoverDecisionResponse,
    BroadcastRequest,
    BroadcastResponse,
)

logger = logging.getLogger(__name__)


def verify_api_secret(x_api_secret: Optional[str] = Header(None, description="API secret for authentication")) -> str:
    """Verify API secret from header.
    
    Raises:
        HTTPException: 401 if secret is invalid
    """
    expected = os.getenv("BOT_API_SECRET", "")
    if not expected:
        logger.error("BOT_API_SECRET not configured")
        raise HTTPException(status_code=500, detail="Server configuration error")
    if not x_api_secret or x_api_secret != expected:
        logger.warning("Invalid or missing API secret attempt")
        raise HTTPException(status_code=401, detail="Unauthorized")
    return x_api_secret


def _status_to_response(status: ExitNodeStatus) -> ExitNodeStatusResponse:
    """Convert ExitNodeStatus to response model."""
    return ExitNodeStatusResponse(
        node_id=status.node_id,
        is_healthy=status.is_healthy,
        is_throttled=status.is_throttled,
        is_preferred=status.is_preferred,
        is_available=status.is_available,
        performance_score=status.performance_score,
        cpu_percent=status.cpu_percent,
        memory_percent=status.memory_percent,
        connections=status.connections,
        tier=status.tier,
        timestamp=status.timestamp,
    )


def create_failover_api(
    get_exit_statuses: Callable[[], Dict[str, ExitNodeStatus]],
    on_failover_event: Optional[Callable[[FailoverEvent], None]] = None,
    get_failover_decision: Optional[Callable[[str], FailoverDecision]] = None,
    on_broadcast_request: Optional[Callable[[str, List[str]], Dict]] = None,
) -> FastAPI:
    """Create FastAPI app with failover endpoints."""
    app = FastAPI(title="Failover API", version="1.0.0")
    app.state.get_exit_statuses = get_exit_statuses
    app.state.on_failover_event = on_failover_event
    app.state.get_failover_decision = get_failover_decision
    app.state.on_broadcast_request = on_broadcast_request
    
    @app.get("/health", response_model=Dict)
    async def health_check():
        return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}
    
    @app.get("/exit/nodes/status", response_model=AllExitNodesStatusResponse)
    async def get_all_exit_statuses():
        statuses = app.state.get_exit_statuses()
        nodes = {node_id: _status_to_response(status) for node_id, status in statuses.items()}
        return AllExitNodesStatusResponse(
            nodes=nodes,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
    
    @app.get("/exit/nodes/{node_id}/status", response_model=ExitNodeStatusResponse)
    async def get_exit_status(node_id: str):
        statuses = app.state.get_exit_statuses()
        if node_id not in statuses:
            raise HTTPException(status_code=404, detail=f"Exit Node {node_id} not found")
        return _status_to_response(statuses[node_id])
    
    @app.post("/failover/event", response_model=FailoverEventResponse, dependencies=[Depends(verify_api_secret)])
    async def report_failover_event(request: FailoverEventRequest):
        event = FailoverEvent(
            user_id=request.user_id,
            chat_id=request.chat_id,
            from_exit=request.from_exit,
            to_exit=request.to_exit,
            reason=request.reason,
            is_throttled_target=request.is_throttled_target,
            timestamp=request.timestamp or datetime.now(timezone.utc).isoformat(),
        )
        notification_sent = False
        broadcast_requested = False
        if app.state.on_failover_event:
            try:
                result = app.state.on_failover_event(event)
                if isinstance(result, dict):
                    notification_sent = result.get("notification_sent", False)
                    broadcast_requested = result.get("broadcast_requested", False)
                else:
                    notification_sent = True
            except Exception as e:
                logger.error(f"Error handling failover event: {e}")
                return FailoverEventResponse(
                    accepted=False,
                    message=f"Error: {e}",
                    notification_sent=False,
                    broadcast_requested=False,
                )
        return FailoverEventResponse(
            accepted=True,
            message="Failover event accepted",
            notification_sent=notification_sent,
            broadcast_requested=broadcast_requested,
        )
    
    @app.get("/failover/decision/{user_id}", response_model=FailoverDecisionResponse, dependencies=[Depends(verify_api_secret)])
    async def get_failover_decision(user_id: str):
        if not app.state.get_failover_decision:
            raise HTTPException(status_code=501, detail="Decision service not available")
        try:
            decision = app.state.get_failover_decision(user_id)
            return FailoverDecisionResponse(
                user_id=decision.user_id,
                decision=decision.decision,
                target_exit=decision.target_exit,
                reason=decision.reason,
                delay_seconds=decision.delay_seconds,
            )
        except Exception as e:
            logger.error(f"Error getting decision: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.post("/admin/broadcast", response_model=BroadcastResponse, dependencies=[Depends(verify_api_secret)])
    async def admin_broadcast(request: BroadcastRequest):
        if not app.state.on_broadcast_request:
            raise HTTPException(status_code=501, detail="Broadcast service not available")
        try:
            result = app.state.on_broadcast_request(request.message, request.user_ids or [])
            return BroadcastResponse(
                sent_count=result.get("sent_count", 0),
                failed_count=result.get("failed_count", 0),
                message=result.get("message", "Broadcast completed"),
            )
        except Exception as e:
            logger.error(f"Error sending broadcast: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.get("/exit/recommendation")
    async def get_exit_recommendation(user_id: Optional[str] = None, exclude_throttled: bool = True):
        statuses = app.state.get_exit_statuses()
        candidates = [s for s in statuses.values() if s.is_healthy]
        if exclude_throttled:
            candidates = [s for s in candidates if not s.is_throttled]
        if not candidates:
            return {"recommended": None, "reason": "no_available_nodes", "alternatives": []}
        candidates.sort(key=lambda s: (-s.performance_score, s.cpu_percent))
        recommended = candidates[0]
        return {
            "recommended": {
                "node_id": recommended.node_id,
                "cpu_percent": recommended.cpu_percent,
                "performance_score": recommended.performance_score,
                "is_throttled": recommended.is_throttled,
            },
            "reason": "best_performance",
            "alternatives": [c.node_id for c in candidates[1:3]],
        }
    
    return app
