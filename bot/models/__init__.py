"""Data models for the bot."""

from .user import User
from .node import Node, NodeType, NodeStatus  # deprecated, use vpn_node
from .vpn_node import (
    VPNNode,
    NodeType,
    NodeStatus,
    NodeRole,
    NodeState
)
from .performance import (
    CPUMetrics,
    PerformanceSnapshot,
    ExitNodePerformanceProfile,
    ThrottleState,
    ThrottleStatus,
    ExitNodeStatus,
    ServerTier,
    FailoverEvent,
    FailoverDecision,
)

__all__ = [
    'User',
    'VPNNode',  # New unified model
    'Node', 'NodeType', 'NodeStatus',  # deprecated
    'NodeRole', 'NodeState',
    'CPUMetrics', 'PerformanceSnapshot', 'ExitNodePerformanceProfile',
    'ThrottleState', 'ThrottleStatus', 'ExitNodeStatus', 'ServerTier',
    'FailoverEvent', 'FailoverDecision',
]
