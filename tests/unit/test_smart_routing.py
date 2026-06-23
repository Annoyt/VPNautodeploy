"""Test Suite: Smart Routing

Purpose:
    Verify routing decisions, failover logic, and user affinity
    work correctly under various conditions.

Key Scenarios:
    1. Failover decision logic
    2. Cooldown enforcement
    3. Max failover limits
    4. Priority selection

When to Run:
    - After changes to smart_routing.py
    - When modifying failover rules
    - Before deploying routing changes

Dependencies:
    - None (pure logic)
"""

import pytest
from datetime import datetime, timezone, timedelta

from bot.core.cluster.smart_routing import SmartRoutingTable, RoutingDecision
from bot.models.performance import (
    ExitNodePerformanceProfile, ExitNodeStatus, ServerTier,
)


class TestSmartRoutingDecisions:
    """Tests for routing decision logic."""
    
    @pytest.fixture
    def routing_table(self):
        return SmartRoutingTable()
    
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
    
    @pytest.fixture
    def healthy_status(self):
        return ExitNodeStatus(
            node_id="exit-high", is_healthy=True, is_throttled=False,
            performance_score=80, cpu_percent=20.0,
            memory_percent=30.0, connections=10, tier="high",
        )
    
    @pytest.fixture
    def throttled_status(self):
        return ExitNodeStatus(
            node_id="exit-limited", is_healthy=True, is_throttled=True,
            performance_score=20, cpu_percent=50.0,
            memory_percent=40.0, connections=5, tier="limited",
        )
    
    def test_stay_when_current_optimal(self, routing_table, high_profile, healthy_status):
        """Test decision to stay when current exit is optimal."""
        routing_table.register_exit_node(high_profile)
        routing_table.update_exit_status(healthy_status)
        routing_table.assign_user("user1", "chat1", "exit-high")
        
        decision = routing_table.should_failover("user1")
        
        assert decision.action == "stay"
        assert decision.reason == "current_is_optimal"
    
    def test_failover_when_better_available(self, routing_table, high_profile, limited_profile,
                                           healthy_status, throttled_status):
        """Test failover when better node is available."""
        routing_table.register_exit_node(high_profile)
        routing_table.register_exit_node(limited_profile)
        routing_table.update_exit_status(throttled_status)  # Current
        healthy_status.node_id = "exit-high"  # Better alternative
        routing_table.update_exit_status(healthy_status)
        routing_table.assign_user("user1", "chat1", "exit-high", current_exit="exit-limited")
        
        decision = routing_table.should_failover("user1")
        
        assert decision.action == "failover"
        assert decision.target_exit == "exit-high"
        assert decision.reason == "better_performance_available"
    
    def test_delay_when_target_throttled(self, routing_table, high_profile, limited_profile,
                                         throttled_status):
        """Test delay decision when target is throttled."""
        routing_table.register_exit_node(high_profile)
        routing_table.register_exit_node(limited_profile)
        routing_table.update_exit_status(throttled_status)
        # Make high unhealthy so throttled is the only option
        high_unhealthy = ExitNodeStatus(
            node_id="exit-high", is_healthy=False, is_throttled=False,
            performance_score=0, cpu_percent=0,
            memory_percent=0, connections=0, tier="high",
        )
        routing_table.update_exit_status(high_unhealthy)
        routing_table.assign_user("user1", "chat1", "exit-high", current_exit="exit-high")
        
        decision = routing_table.should_failover("user1")
        
        assert decision.action == "delay"
        assert decision.reason == "target_is_throttled"
        assert decision.details["delay_seconds"] == 30
    
    def test_cooldown_blocks_failover(self, routing_table, high_profile, limited_profile,
                                      healthy_status, throttled_status):
        """Test that cooldown period blocks immediate failover."""
        routing_table.register_exit_node(high_profile)
        routing_table.register_exit_node(limited_profile)
        routing_table.update_exit_status(throttled_status)
        routing_table.update_exit_status(healthy_status)
        routing_table.assign_user("user1", "chat1", "exit-high", current_exit="exit-limited")
        
        # Execute first failover
        routing_table.execute_failover("user1", "exit-high", "test")
        
        # Immediately try another - should be blocked
        decision = routing_table.should_failover("user1")
        
        assert decision.action == "delay"
        assert decision.reason == "failover_cooldown_active"
    
    def test_max_failover_count_blocks(self, routing_table, high_profile, limited_profile,
                                       healthy_status, throttled_status):
        """Test that max failover count blocks further failovers."""
        routing_table.register_exit_node(high_profile)
        routing_table.register_exit_node(limited_profile)
        routing_table.update_exit_status(throttled_status)
        routing_table.update_exit_status(healthy_status)
        routing_table.assign_user("user1", "chat1", "exit-high", current_exit="exit-limited")
        
        # Execute 3 failovers
        for i in range(3):
            routing_table.execute_failover("user1", "exit-high", f"test{i}")
            # Reset cooldown manually
            routing_table._recent_failovers.clear()
        
        # 4th failover should be blocked
        decision = routing_table.should_failover("user1")
        
        assert decision.action == "stay"
        assert decision.reason == "max_failovers_reached"
    
    def test_no_failover_when_no_alternative(self, routing_table, limited_profile, throttled_status):
        """Test staying when no better alternative exists."""
        routing_table.register_exit_node(limited_profile)
        routing_table.update_exit_status(throttled_status)
        routing_table.assign_user("user1", "chat1", "exit-limited")
        
        decision = routing_table.should_failover("user1")
        
        assert decision.action == "stay"
        # When current node is the only available, it's considered "best available"
        assert decision.reason == "current_is_best_available"
    
    def test_fallback_priority_order(self, routing_table, high_profile, limited_profile):
        """Test that fallback priority respects server tiers."""
        routing_table.register_exit_node(high_profile)
        routing_table.register_exit_node(limited_profile)
        
        entry = routing_table.assign_user("user1", "chat1", "exit-high")
        
        # Fallback should prefer limited over unknown, but high is primary
        assert "exit-limited" in entry.fallback_priority


class TestFailoverExecution:
    """Tests for failover execution."""
    
    def test_execute_failover_updates_route(self):
        """Test that execute_failover updates user route."""
        table = SmartRoutingTable()
        profile = ExitNodePerformanceProfile(
            node_id="exit-1", tier=ServerTier.HIGH, host="1.1.1.1"
        )
        table.register_exit_node(profile)
        table.assign_user("user1", "chat1", "exit-1")
        
        event = table.execute_failover("user1", "exit-1", "test")
        
        assert event.user_id == "user1"
        assert event.to_exit == "exit-1"
        assert table.get_user_route("user1").failover_count == 1
    
    def test_execute_failover_tracks_recent(self):
        """Test that failover is tracked for cooldown."""
        table = SmartRoutingTable()
        profile = ExitNodePerformanceProfile(
            node_id="exit-1", tier=ServerTier.HIGH, host="1.1.1.1"
        )
        table.register_exit_node(profile)
        table.assign_user("user1", "chat1", "exit-1")
        
        table.execute_failover("user1", "exit-1", "test")
        
        assert "user1" in table._recent_failovers
    
    def test_routing_stats_calculation(self):
        """Test routing statistics calculation."""
        table = SmartRoutingTable()
        profile = ExitNodePerformanceProfile(
            node_id="exit-1", tier=ServerTier.HIGH, host="1.1.1.1"
        )
        table.register_exit_node(profile)
        table.assign_user("user1", "chat1", "exit-1")
        table.assign_user("user2", "chat2", "exit-1")
        table.execute_failover("user1", "exit-1", "test")
        
        stats = table.get_routing_stats()
        
        assert stats["total_users"] == 2
        assert stats["total_failovers"] == 1
        assert stats["by_exit"]["exit-1"] == 2
