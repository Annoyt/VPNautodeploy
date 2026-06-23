"""Single node performance tracking."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from bot.models.performance import (
    CPUMetrics,
    PerformanceSnapshot,
    ExitNodePerformanceProfile,
    ThrottleState,
    ThrottleStatus,
    ExitNodeStatus,
)

logger = logging.getLogger(__name__)


@dataclass
class CPUMeasurement:
    """Single CPU measurement with timestamp."""
    percent: float
    timestamp: float


class NodePerformanceTracker:
    """Tracks performance metrics for a single Exit Node."""
    
    def __init__(
        self,
        profile: ExitNodePerformanceProfile,
        window_size: int = 60,
    ):
        self.profile = profile
        self.node_id = profile.node_id
        self.window_size = window_size
        self._cpu_history: deque = deque(maxlen=window_size)
        self._current_cpu: float = 0.0
        self._current_memory: float = 0.0
        self._current_connections: int = 0
        self._last_update: float = 0.0
        self._throttle_status = ThrottleStatus.NORMAL
        self._on_throttle_change: Optional[Callable[[ThrottleState], None]] = None
        self._pending_tasks: set = set()
    
    def set_throttle_callback(self, callback: Callable[[ThrottleState], None]) -> None:
        self._on_throttle_change = callback
    
    def _handle_task_error(self, task) -> None:
        """Handle errors from async tasks."""
        try:
            task.result()
        except Exception as e:
            logger.error("Error in throttle callback: %s", e)
    
    def update_metrics(self, cpu_percent: float, memory_percent: float = 0.0, connections: int = 0) -> None:
        now = time.time()
        self._current_cpu = cpu_percent
        self._current_memory = memory_percent
        self._current_connections = connections
        self._last_update = now
        self._cpu_history.append(CPUMeasurement(cpu_percent, now))
        self._check_throttle_status()
    
    def _check_throttle_status(self) -> None:
        if not self.profile.is_limited:
            new_status = ThrottleStatus.NORMAL
        else:
            new_status = self._calculate_throttle_status()
        
        if new_status != self._throttle_status:
            old_status = self._throttle_status
            self._throttle_status = new_status
            logger.warning(
                "Node %s throttle: %s -> %s (CPU: %.1f%%, Avg5m: %.1f%%)",
                self.node_id, old_status.value, new_status.value,
                self._current_cpu, self._get_average_cpu()
            )
            if self._on_throttle_change:
                # Check if event loop is running (may not be in tests)
                try:
                    loop = asyncio.get_running_loop()
                    task = loop.create_task(self._on_throttle_change(self.get_throttle_state()))
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
                    task.add_done_callback(self._handle_task_error)
                except RuntimeError:
                    # No event loop running (test environment), skip async callback
                    pass
    
    def _calculate_throttle_status(self) -> ThrottleStatus:
        if len(self._cpu_history) < 10:
            return ThrottleStatus.NORMAL
        avg_cpu = self._get_average_cpu()
        # Pre-computed thresholds from profile (MIN-04)
        if avg_cpu > self.profile.cpu_threshold:
            return ThrottleStatus.THROTTLED
        elif avg_cpu > self.profile.warning_threshold:
            return ThrottleStatus.WARNING
        return ThrottleStatus.NORMAL
    
    def _get_average_cpu(self) -> float:
        if not self._cpu_history:
            return 0.0
        cutoff = time.time() - self.profile.throttle_window_seconds
        recent = [m for m in self._cpu_history if m.timestamp >= cutoff]
        return sum(m.percent for m in recent) / len(recent) if recent else 0.0
    
    def get_throttle_state(self) -> ThrottleState:
        return ThrottleState(
            node_id=self.node_id,
            status=self._throttle_status,
            current_cpu=self._current_cpu,
            average_cpu_5min=self._get_average_cpu(),
            threshold=self.profile.cpu_threshold,
        )
    
    def get_status(self) -> ExitNodeStatus:
        throttle_state = self.get_throttle_state()
        cpu_score = max(0, 100 - int(self._current_cpu))
        memory_score = max(0, 100 - int(self._current_memory))
        if throttle_state.is_throttled:
            performance_score = int((cpu_score + memory_score) / 4)  # 75% penalty
        elif throttle_state.status == ThrottleStatus.WARNING:
            performance_score = int((cpu_score + memory_score) / 2)  # 50% penalty
        else:
            performance_score = int((cpu_score + memory_score) / 1)  # NORMAL: no penalty
        is_healthy = (time.time() - self._last_update) < 30.0
        return ExitNodeStatus(
            node_id=self.node_id,
            is_healthy=is_healthy,
            is_throttled=throttle_state.is_throttled,
            performance_score=performance_score,
            cpu_percent=self._current_cpu,
            memory_percent=self._current_memory,
            connections=self._current_connections,
            tier=self.profile.tier.value,
        )
    
    def get_snapshot(self) -> PerformanceSnapshot:
        return PerformanceSnapshot(
            node_id=self.node_id,
            cpu=CPUMetrics(percent=self._current_cpu),
            memory_percent=self._current_memory,
            connections=self._current_connections,
        )
