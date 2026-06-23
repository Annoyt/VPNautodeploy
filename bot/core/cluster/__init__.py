"""Cluster synchronization module for multi-node VPN architecture."""

from bot.models.cluster import (
    ClusterNode,
    LeaderInfo,
    NodeState,
    NodeRole,
    SyncUserRequest,
    SyncUserResponse,
    HealthStatus,
    VoteRequest,
    VoteResponse,
    TrafficStats,
    AggregatedTraffic,
    FailoverRequest,
)

from bot.core.cluster.state import ClusterState
from bot.core.cluster.election import LeaderElection
from bot.core.cluster.sync_api import (
    create_sync_api,
    HMACAuth,
    run_sync_api_server,
)
from bot.core.cluster.sync_client import NodeSyncClient
from bot.core.cluster.entry_detector import EntryNodeDetector, RoutingEntry
from bot.core.cluster.performance_monitor import ClusterPerformanceMonitor
from bot.core.cluster.node_tracker import NodePerformanceTracker
from bot.core.cluster.smart_routing import SmartRoutingTable
from bot.core.cluster.failover_api import create_failover_api
from bot.core.cluster.failover_schemas import (
    ExitNodeStatusResponse,
    FailoverEventRequest,
    FailoverEventResponse,
    BroadcastRequest,
    BroadcastResponse,
)

__all__ = [
    'ClusterNode',
    'LeaderInfo',
    'NodeState',
    'NodeRole',
    'SyncUserRequest',
    'SyncUserResponse',
    'HealthStatus',
    'VoteRequest',
    'VoteResponse',
    'TrafficStats',
    'AggregatedTraffic',
    'FailoverRequest',
    'ClusterState',
    'LeaderElection',
    'create_sync_api',
    'HMACAuth',
    'run_sync_api_server',
    'NodeSyncClient',
    'EntryNodeDetector',
    'RoutingEntry',
    'ClusterPerformanceMonitor',
    'NodePerformanceTracker',
    'SmartRoutingTable',
    'create_failover_api',
    'ExitNodeStatusResponse',
    'FailoverEventRequest',
    'FailoverEventResponse',
    'BroadcastRequest',
    'BroadcastResponse',
]
