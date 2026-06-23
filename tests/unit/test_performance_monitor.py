"""Test Suite: Performance Monitor

Purpose:
    Verify CPU monitoring, throttle detection, and performance scoring
    work correctly under various conditions.

Key Scenarios:
    1. Throttle detection with rolling window
    2. Performance score calculation with penalties
    3. Health status determination

When to Run:
    - After changes to performance_monitor.py or node_tracker.py
    - Before deploying new Exit Node configuration
    - When modifying CPU thresholds

Dependencies:
    - None (pure logic, no external deps)
"""

import pytest
import time
from datetime import datetime, timezone

from bot.core.cluster.node_tracker import NodePerformanceTracker
from bot.core.cluster.performance_monitor import ClusterPerformanceMonitor
from bot.models.performance import (
    ExitNodePerformanceProfile, ServerTier, ThrottleStatus
)


class TestNodePerformanceTracker:
    """Tests for single node performance tracking."""
    
    @pytest.fixture
    def limited_profile(self):
        """Profile for limited tier server."""
        return ExitNodePerformanceProfile(
            node_id="exit-limited-1",
            tier=ServerTier.LIMITED,
            host="192.168.1.1",
            cpu_threshold=30.0,
            throttle_window_seconds=300,
        )
    
    @pytest.fixture
    def high_profile(self):
        """Profile for high tier server."""
        return ExitNodePerformanceProfile(
            node_id="exit-high-1",
            tier=ServerTier.HIGH,
            host="192.168.1.2",
        )
    
    def test_throttle_detection_triggers(self, limited_profile):
        """Test that throttle triggers when CPU > threshold for window."""
        tracker = NodePerformanceTracker(limited_profile, window_size=10)
        
        # Add measurements above threshold
        for i in range(10):
            tracker.update_metrics(cpu_percent=35.0, memory_percent=50.0)
        
        status = tracker.get_throttle_state()
        assert status.status == ThrottleStatus.THROTTLED
        assert status.is_throttled is True
    
    def test_throttle_not_triggered_on_high_tier(self, high_profile):
        """Test that high tier servers never throttle."""
        tracker = NodePerformanceTracker(high_profile, window_size=10)
        
        # Even with high CPU
        for i in range(10):
            tracker.update_metrics(cpu_percent=90.0, memory_percent=80.0)
        
        status = tracker.get_throttle_state()
        assert status.status == ThrottleStatus.NORMAL
        assert status.is_throttled is False
    
    def test_warning_state(self, limited_profile):
        """Test WARNING state when CPU is at 80% of threshold."""
        tracker = NodePerformanceTracker(limited_profile, window_size=10)
        
        # CPU at 25% (>80% of 30% threshold)
        for i in range(10):
            tracker.update_metrics(cpu_percent=25.0)
        
        status = tracker.get_throttle_state()
        assert status.status == ThrottleStatus.WARNING
    
    def test_throttle_clears_on_low_cpu(self, limited_profile):
        """Test that throttle clears when CPU drops."""
        tracker = NodePerformanceTracker(limited_profile, window_size=10)
        
        # First trigger throttle
        for i in range(10):
            tracker.update_metrics(cpu_percent=35.0)
        assert tracker.get_throttle_state().is_throttled
        
        # Then drop CPU
        for i in range(10):
            tracker.update_metrics(cpu_percent=10.0)
        
        status = tracker.get_throttle_state()
        assert status.is_throttled is False
    
    def test_performance_score_normal(self, limited_profile):
        """Test performance score calculation for normal state."""
        tracker = NodePerformanceTracker(limited_profile)
        tracker.update_metrics(cpu_percent=20.0, memory_percent=30.0)
        
        status = tracker.get_status()
        # NORMAL: (100-20 + 100-30) / 1 = 150 (MAJ-02: NORMAL has no penalty)
        assert status.performance_score == 150
    
    def test_performance_score_throttled_penalty(self, limited_profile):
        """Test that throttled nodes get 75% penalty."""
        tracker = NodePerformanceTracker(limited_profile, window_size=10)
        
        # Trigger throttle
        for i in range(10):
            tracker.update_metrics(cpu_percent=35.0, memory_percent=30.0)
        
        status = tracker.get_status()
        # THROTTLED: (100-35 + 100-30) / 4 = 33 (MAJ-02: 75% penalty)
        assert status.performance_score == 33
    
    def test_health_status_timeout(self, limited_profile):
        """Test that node becomes unhealthy after timeout."""
        tracker = NodePerformanceTracker(limited_profile)
        
        # Initial update
        tracker.update_metrics(cpu_percent=20.0)
        assert tracker.get_status().is_healthy is True
        
        # Simulate time passing (more than 30s)
        tracker._last_update = time.time() - 31
        assert tracker.get_status().is_healthy is False
    
    def test_average_cpu_calculation(self, limited_profile):
        """Test rolling window average calculation."""
        tracker = NodePerformanceTracker(limited_profile, window_size=5)
        
        tracker.update_metrics(cpu_percent=10.0)
        tracker.update_metrics(cpu_percent=20.0)
        tracker.update_metrics(cpu_percent=30.0)
        
        avg = tracker._get_average_cpu()
        assert avg == 20.0  # (10+20+30)/3


class TestClusterPerformanceMonitor:
    """Tests for cluster-wide performance monitoring."""
    
    @pytest.fixture
    def monitor(self):
        return ClusterPerformanceMonitor(monitor_interval=1.0)
    
    @pytest.fixture
    def high_profile(self):
        return ExitNodePerformanceProfile(
            node_id="exit-high", tier=ServerTier.HIGH, host="1.1.1.1"
        )
    
    @pytest.fixture
    def limited_profile(self):
        return ExitNodePerformanceProfile(
            node_id="exit-limited", tier=ServerTier.LIMITED, host="2.2.2.2",
            cpu_threshold=30.0, throttle_window_seconds=300,
        )
    
    def test_register_node(self, monitor, high_profile):
        """Test node registration."""
        tracker = monitor.register_node(high_profile)
        assert monitor.get_node_status("exit-high") is not None
        assert tracker.node_id == "exit-high"
    
    def test_select_best_exit_prefers_healthy(self, monitor, high_profile, limited_profile):
        """Test that best selection prefers healthy nodes."""
        monitor.register_node(high_profile)
        monitor.register_node(limited_profile)
        
        # High is healthy, limited is throttled
        monitor.update_node_metrics("exit-high", cpu_percent=20.0)
        monitor.update_node_metrics("exit-limited", cpu_percent=50.0)
        # Trigger throttle on limited
        for _ in range(10):
            monitor.update_node_metrics("exit-limited", cpu_percent=50.0)
        
        best = monitor.select_best_exit_node()
        assert best == "exit-high"
    
    def test_select_best_avoids_throttled(self, monitor, high_profile, limited_profile):
        """Test that throttled nodes are avoided when alternatives exist."""
        monitor.register_node(high_profile)
        monitor.register_node(limited_profile)
        
        # Make limited throttled
        for _ in range(10):
            monitor.update_node_metrics("exit-limited", cpu_percent=50.0)
        
        # High is normal
        monitor.update_node_metrics("exit-high", cpu_percent=30.0)
        
        best = monitor.select_best_exit_node(exclude_throttled=True)
        assert best == "exit-high"
    
    def test_failover_recommendation_current_optimal(self, monitor, high_profile):
        """Test recommendation when current is optimal."""
        monitor.register_node(high_profile)
        monitor.update_node_metrics("exit-high", cpu_percent=20.0)
        
        target, reason = monitor.get_failover_recommendation("exit-high")
        assert target is None
        assert reason == "current_is_optimal"
    
    def test_failover_recommendation_better_available(self, monitor, high_profile, limited_profile):
        """Test recommendation when better node available."""
        monitor.register_node(high_profile)
        monitor.register_node(limited_profile)
        
        # Current (limited) is throttled
        for _ in range(10):
            monitor.update_node_metrics("exit-limited", cpu_percent=50.0)
        # Alternative is healthy
        monitor.update_node_metrics("exit-high", cpu_percent=20.0)
        
        target, reason = monitor.get_failover_recommendation("exit-limited")
        assert target == "exit-high"
        assert reason == "better_performance_available"
    
    def test_unregister_node(self, monitor, high_profile):
        """Test node unregistration."""
        monitor.register_node(high_profile)
        assert monitor.unregister_node("exit-high") is True
        assert monitor.get_node_status("exit-high") is None
