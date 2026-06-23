"""Performance monitoring models for Exit Node CPU and resource tracking.

Used by Entry Node to make intelligent failover decisions based on
server performance characteristics and throttling status.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional
from enum import Enum


class ServerTier(Enum):
    """Performance tier for Exit Nodes."""
    HIGH = "high"           # No CPU limits
    STANDARD = "standard"   # Normal limits
    LIMITED = "limited"     # Strict CPU throttling


class ThrottleStatus(Enum):
    """CPU throttling status."""
    NORMAL = "normal"       # Below threshold
    WARNING = "warning"     # Approaching threshold
    THROTTLED = "throttled" # Above threshold for window duration


@dataclass
class CPUMetrics:
    """CPU metrics snapshot."""
    percent: float  # Current CPU usage (0-100)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    @property
    def is_critical(self) -> bool:
        """Check if CPU is in critical range (>80%)."""
        return self.percent > 80.0
    
    @property
    def is_high(self) -> bool:
        """Check if CPU is high (>50%)."""
        return self.percent > 50.0


@dataclass 
class PerformanceSnapshot:
    """Complete performance snapshot for an Exit Node."""
    node_id: str
    cpu: CPUMetrics
    memory_percent: float  # Memory usage (0-100)
    connections: int       # Active connections
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    @property
    def is_healthy(self) -> bool:
        """Check if node is healthy (CPU < 80%, Memory < 90%)."""
        return self.cpu.percent < 80.0 and self.memory_percent < 90.0


@dataclass
class ExitNodePerformanceProfile:
    """Performance profile and configuration for an Exit Node.
    
    This is configured per-node based on its hardware capabilities.
    """
    node_id: str
    tier: ServerTier
    host: str
    api_port: int = 8081
    
    # CPU throttling configuration
    cpu_threshold: float = 30.0  # Throttle if above this %
    throttle_window_seconds: int = 300  # For this duration (5 min)
    
    # Derived properties
    is_limited: bool = field(init=False)
    warning_threshold: float = field(init=False)  # 80% of cpu_threshold
    
    def __post_init__(self):
        self.is_limited = self.tier == ServerTier.LIMITED
        self.warning_threshold = self.cpu_threshold * 0.8
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "node_id": self.node_id,
            "tier": self.tier.value,
            "host": self.host,
            "api_port": self.api_port,
            "cpu_threshold": self.cpu_threshold,
            "warning_threshold": self.warning_threshold,
            "throttle_window_seconds": self.throttle_window_seconds,
            "is_limited": self.is_limited,
        }


@dataclass
class ThrottleState:
    """Current throttling state for an Exit Node."""
    node_id: str
    status: ThrottleStatus
    current_cpu: float
    average_cpu_5min: float
    threshold: float
    is_throttled: bool = field(init=False)
    
    def __post_init__(self):
        self.is_throttled = self.status == ThrottleStatus.THROTTLED
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "node_id": self.node_id,
            "status": self.status.value,
            "current_cpu": round(self.current_cpu, 1),
            "average_cpu_5min": round(self.average_cpu_5min, 1),
            "threshold": self.threshold,
            "is_throttled": self.is_throttled,
        }


@dataclass
class ExitNodeStatus:
    """Complete status for an Exit Node (health + performance)."""
    node_id: str
    is_healthy: bool           # Responding to health checks
    is_throttled: bool         # CPU throttled
    performance_score: int     # 0-100 (higher is better)
    cpu_percent: float
    memory_percent: float
    connections: int
    tier: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    @property
    def is_available(self) -> bool:
        """Node is available for new connections."""
        return self.is_healthy
    
    @property
    def is_preferred(self) -> bool:
        """Node is preferred (healthy and not throttled)."""
        return self.is_healthy and not self.is_throttled
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "node_id": self.node_id,
            "is_healthy": self.is_healthy,
            "is_throttled": self.is_throttled,
            "is_preferred": self.is_preferred,
            "is_available": self.is_available,
            "performance_score": self.performance_score,
            "cpu_percent": round(self.cpu_percent, 1),
            "memory_percent": round(self.memory_percent, 1),
            "connections": self.connections,
            "tier": self.tier,
            "timestamp": self.timestamp,
        }


@dataclass
class FailoverEvent:
    """Event triggered when user is moved between Exit Nodes."""
    user_id: str
    chat_id: str
    from_exit: str
    to_exit: str
    reason: str  # "health_check_failed", "manual", "recovery"
    is_throttled_target: bool  # Target node is throttled
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API calls."""
        return {
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "from_exit": self.from_exit,
            "to_exit": self.to_exit,
            "reason": self.reason,
            "is_throttled_target": self.is_throttled_target,
            "timestamp": self.timestamp,
        }


@dataclass
class FailoverDecision:
    """Decision made by Entry Node for failover."""
    user_id: str
    decision: str  # "failover", "stay", "delay"
    target_exit: Optional[str]
    reason: str
    delay_seconds: int = 0  # If decision is "delay"
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "user_id": self.user_id,
            "decision": self.decision,
            "target_exit": self.target_exit,
            "reason": self.reason,
            "delay_seconds": self.delay_seconds,
        }
