"""Performance monitoring for Exit Nodes with CPU throttling detection."""

import asyncio
import logging
from typing import Dict, List, Optional, Callable

from bot.models.performance import (
    ExitNodePerformanceProfile,
    ThrottleState,
    ExitNodeStatus,
)
from bot.utils.metrics import read_memory_from_proc, ProcStatReader
from .node_tracker import NodePerformanceTracker

logger = logging.getLogger(__name__)


class ClusterPerformanceMonitor:
    """Monitors performance for all Exit Nodes in the cluster."""
    
    def __init__(self, monitor_interval: float = 5.0):
        self.monitor_interval = monitor_interval
        self._trackers: Dict[str, NodePerformanceTracker] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._on_throttle_change: Optional[Callable[[str, ThrottleState], None]] = None
        self._proc_reader = ProcStatReader()
    
    def register_node(self, profile: ExitNodePerformanceProfile) -> NodePerformanceTracker:
        tracker = NodePerformanceTracker(profile)
        async def on_throttle(state: ThrottleState):
            if self._on_throttle_change:
                await self._on_throttle_change(profile.node_id, state)
        tracker.set_throttle_callback(on_throttle)
        self._trackers[profile.node_id] = tracker
        logger.info(f"Registered node for monitoring: {profile.node_id}")
        return tracker
    
    def unregister_node(self, node_id: str) -> bool:
        if node_id in self._trackers:
            del self._trackers[node_id]
            logger.info(f"Unregistered node: {node_id}")
            return True
        return False
    
    def set_throttle_callback(self, callback: Callable[[str, ThrottleState], None]) -> None:
        self._on_throttle_change = callback
    
    def update_node_metrics(self, node_id: str, cpu_percent: float, 
                           memory_percent: float = 0.0, connections: int = 0) -> None:
        tracker = self._trackers.get(node_id)
        if tracker:
            tracker.update_metrics(cpu_percent, memory_percent, connections)
    
    def get_node_status(self, node_id: str) -> Optional[ExitNodeStatus]:
        tracker = self._trackers.get(node_id)
        return tracker.get_status() if tracker else None
    
    def get_all_statuses(self) -> Dict[str, ExitNodeStatus]:
        return {node_id: tracker.get_status() for node_id, tracker in self._trackers.items()}
    
    def select_best_exit_node(self, exclude_throttled: bool = True, 
                              exclude_unhealthy: bool = True) -> Optional[str]:
        statuses = self.get_all_statuses()
        candidates = list(statuses.values())
        if exclude_unhealthy:
            candidates = [s for s in candidates if s.is_healthy]
        if not candidates:
            return None
        preferred = [s for s in candidates if s.is_preferred]
        if preferred:
            best = max(preferred, key=lambda s: (s.performance_score, -s.cpu_percent))
            return best.node_id
        if candidates and not exclude_throttled:
            best = min(candidates, key=lambda s: s.cpu_percent)
            return best.node_id
        return None
    
    def get_failover_recommendation(self, current_exit: str) -> tuple[Optional[str], str]:
        current_status = self.get_node_status(current_exit)
        if current_status and current_status.is_preferred:
            return None, "current_is_optimal"
        target = self.select_best_exit_node(exclude_throttled=True)
        if target:
            return target, "better_performance_available"
        if current_status and current_status.is_throttled:
            target = self.select_best_exit_node(exclude_throttled=False)
            if target and target != current_exit:
                return target, "current_throttled_switch_to_less_loaded"
        return None, "no_better_alternative"
    
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("Performance monitor started")
    
    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Performance monitor stopped")
    
    async def _monitor_loop(self) -> None:
        while self._running:
            try:
                await self._collect_local_metrics()
                await asyncio.sleep(self.monitor_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in monitor loop: {e}")
                await asyncio.sleep(self.monitor_interval)
    
    async def _collect_local_metrics(self) -> None:
        """Collect metrics for local node only.
        
        For multi-node monitoring, each node should report its own metrics
        via the /metrics endpoint, and this method should fetch them remotely.
        """
        try:
            # For local monitoring (single node on this machine)
            try:
                import psutil
                cpu_percent = psutil.cpu_percent(interval=1)
                memory_percent = psutil.virtual_memory().percent
            except ImportError:
                # Use ProcStatReader for proper delta calculation
                cpu_percent = self._proc_reader.read_cpu_percent()
                memory_percent = read_memory_from_proc()
            
        except Exception as e:
            logger.error("Failed to collect local metrics: %s", e)
