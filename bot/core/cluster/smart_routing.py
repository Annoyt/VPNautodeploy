"""Smart routing for Entry Node failover with performance awareness."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bot.models.performance import (
    ExitNodeStatus, ExitNodePerformanceProfile, FailoverEvent,
    FailoverDecision, ServerTier,
)

logger = logging.getLogger(__name__)


@dataclass
class UserRoutingEntry:
    user_id: str
    chat_id: str
    primary_exit: str
    current_exit: str
    fallback_priority: List[str] = field(default_factory=list)
    assigned_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_activity: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_failover: Optional[str] = None
    failover_count: int = 0
    
    def record_failover(self, new_exit: str) -> None:
        self.last_failover = datetime.now(timezone.utc).isoformat()
        self.last_activity = datetime.now(timezone.utc).isoformat()
        self.failover_count += 1
        self.current_exit = new_exit
    
    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = datetime.now(timezone.utc).isoformat()


@dataclass
class RoutingDecision:
    action: str  # "stay", "failover", "delay"
    target_exit: Optional[str]
    reason: str
    details: Dict = field(default_factory=dict)
    
    @property
    def should_failover(self) -> bool:
        return self.action == "failover" and self.target_exit is not None


class SmartRoutingTable:
    """Routing table with performance-aware failover logic."""
    
    FAILOVER_COOLDOWN_SECONDS = 300
    MAX_FAILOVER_COUNT = 3
    THROTTLED_FAILOVER_DELAY = 30
    
    def __init__(self):
        self._user_routes: Dict[str, UserRoutingEntry] = {}
        self._exit_statuses: Dict[str, ExitNodeStatus] = {}
        self._exit_profiles: Dict[str, ExitNodePerformanceProfile] = {}
        self._recent_failovers: Dict[str, datetime] = {}
    
    def register_exit_node(self, profile: ExitNodePerformanceProfile) -> None:
        self._exit_profiles[profile.node_id] = profile
        logger.info(f"Registered Exit Node: {profile.node_id}")
    
    def update_exit_status(self, status: ExitNodeStatus) -> None:
        self._exit_statuses[status.node_id] = status
    
    def assign_user(self, user_id: str, chat_id: str, primary_exit: str,
                    current_exit: Optional[str] = None) -> UserRoutingEntry:
        entry = UserRoutingEntry(
            user_id=user_id, chat_id=chat_id, primary_exit=primary_exit,
            current_exit=current_exit or primary_exit,
            fallback_priority=self._build_fallback_priority(primary_exit),
        )
        self._user_routes[user_id] = entry
        logger.info(f"Assigned user {user_id} to {primary_exit}")
        return entry
    
    def _build_fallback_priority(self, primary_exit: str) -> List[str]:
        other_exits = [nid for nid in self._exit_profiles.keys() if nid != primary_exit]
        tier_order = {ServerTier.HIGH: 0, ServerTier.STANDARD: 1, ServerTier.LIMITED: 2}
        def sort_key(nid: str) -> tuple:
            profile = self._exit_profiles.get(nid)
            tier = profile.tier if profile else ServerTier.STANDARD
            return (tier_order.get(tier, 2), nid)
        return sorted(other_exits, key=sort_key)
    
    def get_user_route(self, user_id: str) -> Optional[UserRoutingEntry]:
        entry = self._user_routes.get(user_id)
        if entry:
            entry.touch()
        return entry
    
    def should_failover(self, user_id: str) -> RoutingDecision:
        entry = self._user_routes.get(user_id)
        if not entry:
            return RoutingDecision("stay", None, "user_not_routed")
        # Check hard limits first (before performance checks)
        if entry.failover_count >= self.MAX_FAILOVER_COUNT:
            return RoutingDecision("stay", None, "max_failovers_reached",
                {"failover_count": entry.failover_count})
        if self._is_in_cooldown(user_id):
            return RoutingDecision("delay", None, "failover_cooldown_active",
                {"cooldown_seconds": self.FAILOVER_COOLDOWN_SECONDS})
        current_status = self._exit_statuses.get(entry.current_exit)
        if current_status and current_status.is_preferred:
            return RoutingDecision("stay", None, "current_is_optimal",
                {"current_exit": entry.current_exit, "cpu": current_status.cpu_percent})
        target = self._find_best_target(entry)
        if not target:
            return RoutingDecision("stay", None, "no_better_alternative")
        if target == entry.current_exit:
            return RoutingDecision("stay", None, "current_is_best_available")
        target_status = self._exit_statuses.get(target)
        if target_status and target_status.is_throttled:
            return RoutingDecision("delay", target, "target_is_throttled",
                {"delay_seconds": self.THROTTLED_FAILOVER_DELAY, "target_cpu": target_status.cpu_percent})
        return RoutingDecision("failover", target, "better_performance_available",
            {"from_exit": entry.current_exit, "from_cpu": current_status.cpu_percent if current_status else None,
             "to_exit": target, "to_cpu": target_status.cpu_percent if target_status else None})
    
    def _is_in_cooldown(self, user_id: str) -> bool:
        last = self._recent_failovers.get(user_id)
        if not last:
            return False
        return (datetime.now(timezone.utc) - last).total_seconds() < self.FAILOVER_COOLDOWN_SECONDS
    
    def _find_best_target(self, entry: UserRoutingEntry) -> Optional[str]:
        primary = self._exit_statuses.get(entry.primary_exit)
        if primary and primary.is_preferred:
            return entry.primary_exit
        for node_id in entry.fallback_priority:
            status = self._exit_statuses.get(node_id)
            if status and status.is_preferred:
                return node_id
        healthy = [(nid, s) for nid, s in self._exit_statuses.items() if s.is_healthy]
        if healthy:
            healthy.sort(key=lambda x: x[1].cpu_percent)
            return healthy[0][0]
        return None
    
    def execute_failover(self, user_id: str, target_exit: str, reason: str) -> FailoverEvent:
        entry = self._user_routes.get(user_id)
        if not entry:
            raise ValueError(f"User {user_id} not found")
        from_exit = entry.current_exit
        entry.record_failover(target_exit)
        self._recent_failovers[user_id] = datetime.now(timezone.utc)
        target_status = self._exit_statuses.get(target_exit)
        event = FailoverEvent(
            user_id=user_id, chat_id=entry.chat_id, from_exit=from_exit,
            to_exit=target_exit, reason=reason,
            is_throttled_target=target_status.is_throttled if target_status else False,
        )
        logger.info(f"Failover: {user_id} from {from_exit} to {target_exit}")
        return event
    
    def get_failover_decision(self, user_id: str) -> FailoverDecision:
        decision = self.should_failover(user_id)
        delay = decision.details.get("delay_seconds", self.THROTTLED_FAILOVER_DELAY) if decision.action == "delay" else 0
        return FailoverDecision(user_id, decision.action, decision.target_exit, decision.reason, delay)
    
    def get_all_user_routes(self) -> Dict[str, UserRoutingEntry]:
        return dict(self._user_routes)
    
    def get_users_on_exit(self, exit_id: str) -> List[UserRoutingEntry]:
        return [e for e in self._user_routes.values() if e.current_exit == exit_id]
    
    def get_routing_stats(self) -> Dict:
        return {
            "total_users": len(self._user_routes),
            "by_exit": {exit_id: len([e for e in self._user_routes.values() if e.current_exit == exit_id])
                        for exit_id in set(e.current_exit for e in self._user_routes.values())},
            "total_failovers": sum(e.failover_count for e in self._user_routes.values()),
            "users_in_cooldown": sum(1 for uid in self._user_routes if self._is_in_cooldown(uid)),
        }
    
    def cleanup_stale_routes(self, max_age_hours: int = 24) -> int:
        """Remove routes for users not seen recently.
        
        Returns:
            Number of removed routes
        """
        cutoff = datetime.now(timezone.utc) - __import__('datetime').timedelta(hours=max_age_hours)
        stale_ids = [
            uid for uid, entry in self._user_routes.items()
            if datetime.fromisoformat(entry.last_activity) < cutoff
        ]
        for uid in stale_ids:
            del self._user_routes[uid]
            self._recent_failovers.pop(uid, None)
        if stale_ids:
            logger.info("Cleaned up %d stale routes", len(stale_ids))
        return len(stale_ids)
