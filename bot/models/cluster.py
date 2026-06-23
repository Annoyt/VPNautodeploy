"""Cluster synchronization models for multi-node architecture.

DEPRECATED: Use bot.models.vpn_node.VPNNode instead.
This module is kept for backward compatibility.
"""

import warnings
import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Any

from bot.models import User
from bot.models.vpn_node import (
    VPNNode, 
    ClusterNode,  # Re-export for backward compatibility
    NodeState as VPNNodeState,
    NodeRole as VPNNodeRole
)

# Re-export for backward compatibility
NodeState = VPNNodeState
NodeRole = VPNNodeRole

# Emit warning when ClusterNode is instantiated
_original_vpnnode_init = VPNNode.__init__

def _cluster_node_init(self, *args, **kwargs):
    """Wrapper that emits deprecation warning."""
    warnings.warn(
        "ClusterNode is deprecated, use VPNNode from bot.models.vpn_node",
        DeprecationWarning,
        stacklevel=2
    )
    return _original_vpnnode_init(self, *args, **kwargs)

# Note: We can't easily override __init__ for the class
# The deprecation warning is handled in VPNNode.__post_init__ when node_id is set


@dataclass
class LeaderInfo:
    """Information about the current cluster leader."""
    node_id: str
    term: int
    elected_at: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'node_id': self.node_id,
            'term': self.term,
            'elected_at': self.elected_at,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LeaderInfo':
        return cls(
            node_id=data['node_id'],
            term=data['term'],
            elected_at=data['elected_at'],
        )


@dataclass
class SyncUserRequest:
    """Request to sync user to peer nodes."""
    user: User
    client_config: Dict[str, Any]
    source_node_id: str
    timestamp: str
    signature: str
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary (excludes signature for signing)."""
        return {
            'user': {
                'chat_id': self.user.chat_id,
                'username': self.user.username,
                'uuid': self.user.uuid,
                'email': self.user.email,
                'status': self.user.status,
                'lang': self.user.lang,
                'platform': self.user.platform,
                'support_topic_id': self.user.support_topic_id,
                'created_at': self.user.created_at,
                'subscription_expiry': self.user.subscription_expiry,
                'limit_ip': self.user.limit_ip,
                'quota_gb': self.user.quota_gb,
                'last_traffic_update': self.user.last_traffic_update,
            },
            'client_config': self.client_config,
            'source_node_id': self.source_node_id,
            'timestamp': self.timestamp,
        }
    
    def sign(self, secret: str) -> str:
        """Generate HMAC signature for this request."""
        import json
        payload = json.dumps(self.to_dict(), sort_keys=True)
        return hmac.HMAC(
            secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
    
    def verify(self, secret: str) -> bool:
        """Verify request signature."""
        expected = self.sign(secret)
        return hmac.compare_digest(self.signature, expected)
    
    @classmethod
    def create(cls, user: User, client_config: Dict[str, Any],
               source_node_id: str, secret: str) -> 'SyncUserRequest':
        """Create and sign a new sync request."""
        timestamp = datetime.now(timezone.utc).isoformat()
        request = cls(
            user=user,
            client_config=client_config,
            source_node_id=source_node_id,
            timestamp=timestamp,
            signature='',  # Will be set after creation
        )
        request.signature = request.sign(secret)
        return request


@dataclass
class SyncUserResponse:
    """Response to sync user request."""
    success: bool
    node_id: str
    message: str
    timestamp: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'success': self.success,
            'node_id': self.node_id,
            'message': self.message,
            'timestamp': self.timestamp,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SyncUserResponse':
        return cls(
            success=data['success'],
            node_id=data['node_id'],
            message=data['message'],
            timestamp=data['timestamp'],
        )


@dataclass
class HealthStatus:
    """Health check response from a node."""
    node_id: str
    state: NodeState
    term: int
    is_leader: bool
    last_heartbeat: str
    db_status: bool
    xui_status: bool
    uptime_seconds: int
    timestamp: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'node_id': self.node_id,
            'state': self.state.value,
            'term': self.term,
            'is_leader': self.is_leader,
            'last_heartbeat': self.last_heartbeat,
            'db_status': self.db_status,
            'xui_status': self.xui_status,
            'uptime_seconds': self.uptime_seconds,
            'timestamp': self.timestamp,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'HealthStatus':
        return cls(
            node_id=data['node_id'],
            state=NodeState(data['state']),
            term=data['term'],
            is_leader=data['is_leader'],
            last_heartbeat=data['last_heartbeat'],
            db_status=data['db_status'],
            xui_status=data['xui_status'],
            uptime_seconds=data['uptime_seconds'],
            timestamp=data['timestamp'],
        )


@dataclass
class VoteRequest:
    """Request for vote during leader election."""
    term: int
    candidate_id: str
    last_log_index: int
    last_log_term: int
    timestamp: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'term': self.term,
            'candidate_id': self.candidate_id,
            'last_log_index': self.last_log_index,
            'last_log_term': self.last_log_term,
            'timestamp': self.timestamp,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VoteRequest':
        return cls(
            term=data['term'],
            candidate_id=data['candidate_id'],
            last_log_index=data['last_log_index'],
            last_log_term=data['last_log_term'],
            timestamp=data['timestamp'],
        )


@dataclass
class VoteResponse:
    """Response to vote request."""
    term: int
    vote_granted: bool
    voter_id: str
    timestamp: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'term': self.term,
            'vote_granted': self.vote_granted,
            'voter_id': self.voter_id,
            'timestamp': self.timestamp,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VoteResponse':
        return cls(
            term=data['term'],
            vote_granted=data['vote_granted'],
            voter_id=data['voter_id'],
            timestamp=data['timestamp'],
        )


@dataclass
class TrafficStats:
    """Traffic statistics for a user."""
    email: str
    upload_bytes: int
    download_bytes: int
    node_id: str
    timestamp: str
    
    @property
    def total_bytes(self) -> int:
        return self.upload_bytes + self.download_bytes
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'email': self.email,
            'upload_bytes': self.upload_bytes,
            'download_bytes': self.download_bytes,
            'node_id': self.node_id,
            'timestamp': self.timestamp,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TrafficStats':
        return cls(
            email=data['email'],
            upload_bytes=data['upload_bytes'],
            download_bytes=data['download_bytes'],
            node_id=data['node_id'],
            timestamp=data['timestamp'],
        )


@dataclass
class AggregatedTraffic:
    """Aggregated traffic from multiple nodes."""
    email: str
    total_upload: int
    total_download: int
    by_node: Dict[str, Dict[str, int]]
    timestamp: str
    
    @property
    def total_bytes(self) -> int:
        return self.total_upload + self.total_download
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'email': self.email,
            'total_upload': self.total_upload,
            'total_download': self.total_download,
            'total_bytes': self.total_bytes,
            'by_node': self.by_node,
            'timestamp': self.timestamp,
        }


@dataclass
class FailoverRequest:
    """Request to failover user to another node."""
    chat_id: str
    from_node_id: str
    to_node_id: str
    reason: str
    timestamp: str
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            'chat_id': self.chat_id,
            'from_node_id': self.from_node_id,
            'to_node_id': self.to_node_id,
            'reason': self.reason,
            'timestamp': self.timestamp,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FailoverRequest':
        return cls(
            chat_id=data['chat_id'],
            from_node_id=data['from_node_id'],
            to_node_id=data['to_node_id'],
            reason=data['reason'],
            timestamp=data['timestamp'],
        )
