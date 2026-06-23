"""Pydantic schemas for Failover API."""

from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class ExitNodeStatusResponse(BaseModel):
    node_id: str
    is_healthy: bool
    is_throttled: bool
    is_preferred: bool
    is_available: bool
    performance_score: int
    cpu_percent: float
    memory_percent: float
    connections: int
    tier: str
    timestamp: str


class AllExitNodesStatusResponse(BaseModel):
    nodes: Dict[str, ExitNodeStatusResponse]
    timestamp: str


class FailoverEventRequest(BaseModel):
    user_id: str
    chat_id: str
    from_exit: str
    to_exit: str
    reason: str
    is_throttled_target: bool
    timestamp: Optional[str] = None


class FailoverEventResponse(BaseModel):
    accepted: bool
    message: str
    notification_sent: bool
    broadcast_requested: bool


class FailoverDecisionResponse(BaseModel):
    user_id: str
    decision: str
    target_exit: Optional[str]
    reason: str
    delay_seconds: int = Field(ge=0, description="Delay before failover in seconds")


class BroadcastRequest(BaseModel):
    message: str
    user_ids: Optional[List[str]] = None


class BroadcastResponse(BaseModel):
    sent_count: int
    failed_count: int
    message: str
