"""Unit tests for EntryNodeDetector."""

import pytest
from datetime import datetime, timezone

from bot.models.vpn_node import VPNNode, NodeType
from bot.models.cluster import NodeRole
from bot.core.cluster.entry_detector import EntryNodeDetector, RoutingEntry


class TestEntryNodeDetectorRegistration:
    """Test Entry Node registration."""
    
    @pytest.fixture
    def detector(self):
        """Create detector."""
        return EntryNodeDetector()
    
    def test_register_entry_node(self, detector):
        """Test registering Entry Node."""
        node = VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
            region="ru",
            city="Moscow",
        )
        
        detector.register_entry_node(node)
        
        assert "entry-moscow-1" in detector._entry_nodes
        assert detector.get_entry_node("entry-moscow-1") == node
    
    def test_register_exit_node_fails(self, detector):
        """Test that registering Exit Node fails."""
        node = VPNNode(
            node_id="exit-frankfurt-1",
            node_type=NodeType.EXIT,
            host="203.0.113.30",
        )
        
        with pytest.raises(ValueError):
            detector.register_entry_node(node)
    
    def test_unregister_entry_node(self, detector):
        """Test unregistering Entry Node."""
        node = VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
        )
        detector.register_entry_node(node)
        
        result = detector.unregister_entry_node("entry-moscow-1")
        
        assert result is True
        assert "entry-moscow-1" not in detector._entry_nodes
    
    def test_unregister_unknown_node(self, detector):
        """Test unregistering unknown node."""
        result = detector.unregister_entry_node("unknown")
        
        assert result is False
    
    def test_get_all_entry_nodes(self, detector):
        """Test getting all Entry Nodes."""
        node1 = VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
        )
        node2 = VPNNode(
            node_id="entry-spb-1",
            node_type=NodeType.ENTRY,
            host="185.1.2.3",
        )
        
        detector.register_entry_node(node1)
        detector.register_entry_node(node2)
        
        nodes = detector.get_all_entry_nodes()
        
        assert len(nodes) == 2
        assert node1 in nodes
        assert node2 in nodes


class TestEntryNodeDetectorDetection:
    """Test Entry Node detection."""
    
    @pytest.fixture
    def detector(self):
        """Create detector with registered nodes."""
        d = EntryNodeDetector()
        
        d.register_entry_node(VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
            region="ru",
        ))
        
        d.register_entry_node(VPNNode(
            node_id="entry-frankfurt-1",
            node_type=NodeType.ENTRY,
            host="185.1.2.3",
            region="eu",
        ))
        
        return d
    
    def test_detect_from_header(self, detector):
        """Test detection from explicit header."""
        headers = {
            "X-Entry-Node-ID": "entry-moscow-1",
        }
        
        entry_id = detector.detect_from_headers(headers)
        
        assert entry_id == "entry-moscow-1"
    
    def test_detect_from_ip_header(self, detector):
        """Test detection from Entry IP header."""
        headers = {
            "X-Entry-Node-IP": "203.0.113.20",
        }
        
        entry_id, node = detector.detect_entry_node(headers)
        
        assert entry_id == "entry-moscow-1"
        assert node is not None
        assert node.node_id == "entry-moscow-1"
    
    def test_detect_unknown_entry(self, detector):
        """Test detection of unknown Entry Node."""
        headers = {
            "X-Entry-Node-ID": "unknown-entry",
        }
        
        entry_id = detector.detect_from_headers(headers)
        
        assert entry_id is None
    
    def test_detect_no_headers(self, detector):
        """Test detection with no relevant headers."""
        headers = {
            "User-Agent": "Test",
        }
        
        entry_id = detector.detect_from_headers(headers)
        
        assert entry_id is None


class TestEntryNodeDetectorRouting:
    """Test user routing."""
    
    @pytest.fixture
    def detector(self):
        """Create detector with nodes."""
        d = EntryNodeDetector()
        d.register_entry_node(VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
            region="ru",
        ))
        return d
    
    def test_assign_user_to_entry(self, detector):
        """Test assigning user to Entry Node."""
        entry = detector.assign_user_to_entry(
            chat_id="123456789",
            entry_node_id="entry-moscow-1",
            exit_node_id="exit-frankfurt-1",
        )
        
        assert entry.chat_id == "123456789"
        assert entry.entry_node_id == "entry-moscow-1"
        assert entry.exit_node_id == "exit-frankfurt-1"
    
    def test_get_user_routing(self, detector):
        """Test getting user routing."""
        detector.assign_user_to_entry(
            chat_id="123456789",
            entry_node_id="entry-moscow-1",
            exit_node_id="exit-frankfurt-1",
        )
        
        entry = detector.get_user_routing("123456789")
        
        assert entry is not None
        assert entry.entry_node_id == "entry-moscow-1"
    
    def test_update_user_activity(self, detector):
        """Test updating user activity."""
        detector.assign_user_to_entry(
            chat_id="123456789",
            entry_node_id="entry-moscow-1",
            exit_node_id="exit-frankfurt-1",
        )
        
        detector.update_user_activity("123456789", bytes_transferred=1024)
        
        entry = detector.get_user_routing("123456789")
        assert entry.total_bytes == 1024
        assert entry.connection_count == 1
    
    def test_get_users_on_entry(self, detector):
        """Test getting users on specific Entry Node."""
        detector.assign_user_to_entry("111", "entry-moscow-1", "exit-1")
        detector.assign_user_to_entry("222", "entry-moscow-1", "exit-1")
        detector.assign_user_to_entry("333", "entry-frankfurt-1", "exit-2")
        
        users = detector.get_users_on_entry("entry-moscow-1")
        
        assert "111" in users
        assert "222" in users
        assert "333" not in users
    
    def test_get_entry_load(self, detector):
        """Test getting Entry Node load."""
        detector.assign_user_to_entry("111", "entry-moscow-1", "exit-1")
        detector.assign_user_to_entry("222", "entry-moscow-1", "exit-1")
        
        load = detector.get_entry_load("entry-moscow-1")
        
        assert load == 2


class TestEntryNodeDetectorGeographic:
    """Test geographic routing."""
    
    @pytest.fixture
    def detector(self):
        """Create detector with multiple regional nodes."""
        d = EntryNodeDetector()
        
        d.register_entry_node(VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
            region="ru",
            weight=100,
        ))
        
        d.register_entry_node(VPNNode(
            node_id="entry-spb-1",
            node_type=NodeType.ENTRY,
            host="185.1.2.3",
            region="ru",
            weight=50,
        ))
        
        d.register_entry_node(VPNNode(
            node_id="entry-frankfurt-1",
            node_type=NodeType.ENTRY,
            host="212.60.1.1",
            region="eu",
            weight=100,
        ))
        
        return d
    
    def test_find_nearest_entry_same_region(self, detector):
        """Test finding nearest entry in same region."""
        node = detector.find_nearest_entry("ru")
        
        assert node is not None
        assert node.region == "ru"
    
    def test_find_nearest_entry_fallback(self, detector):
        """Test fallback when no matching region."""
        node = detector.find_nearest_entry("us")  # No US nodes
        
        # Should return any available node
        assert node is not None
    
    def test_suggest_entry_for_new_user(self, detector):
        """Test suggesting entry for new user."""
        node = detector.suggest_entry_for_user(
            chat_id="123",
            user_region="ru",
        )
        
        assert node is not None
        assert node.region == "ru"
    
    def test_suggest_entry_respects_preferred(self, detector):
        """Test that preferred entry is respected."""
        node = detector.suggest_entry_for_user(
            chat_id="123",
            user_region="ru",
            preferred_entry="entry-spb-1",
        )
        
        assert node is not None
        # Fallback should return one of the available nodes
        assert node.node_id in {"entry-moscow-1", "entry-spb-1"}
    
    def test_suggest_entry_uses_existing(self, detector):
        """Test that existing assignment is used."""
        detector.assign_user_to_entry("123", "entry-moscow-1", "exit-1")
        
        node = detector.suggest_entry_for_user(
            chat_id="123",
            user_region="eu",  # Different region, but should use existing
        )
        
        assert node is not None
        assert node.node_id == "entry-moscow-1"


class TestEntryNodeDetectorStats:
    """Test statistics."""
    
    @pytest.fixture
    def detector(self):
        """Create detector with users."""
        d = EntryNodeDetector()
        d.register_entry_node(VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
        ))
        
        d.assign_user_to_entry("111", "entry-moscow-1", "exit-1")
        d.assign_user_to_entry("222", "entry-moscow-1", "exit-1")
        d.update_user_activity("111", 1024)
        d.update_user_activity("222", 2048)
        
        return d
    
    def test_get_entry_stats(self, detector):
        """Test getting entry statistics."""
        stats = detector.get_entry_stats("entry-moscow-1")
        
        assert stats["entry_node_id"] == "entry-moscow-1"
        assert stats["user_count"] == 2
        assert stats["total_bytes"] == 3072
        assert "111" in stats["users"]
        assert "222" in stats["users"]
    
    def test_get_all_stats(self, detector):
        """Test getting all statistics."""
        stats = detector.get_all_stats()
        
        assert "entry-moscow-1" in stats
        assert stats["entry-moscow-1"]["user_count"] == 2


class TestEntryNodeDetectorCleanup:
    """Test cleanup of stale entries."""
    
    def test_cleanup_stale_entries(self):
        """Test cleaning up stale entries."""
        detector = EntryNodeDetector()
        detector.register_entry_node(VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
        ))
        
        # Add entry with old timestamp
        from datetime import timedelta
        old_time = datetime.now(timezone.utc) - timedelta(days=2)
        
        entry = RoutingEntry(
            chat_id="123",
            entry_node_id="entry-moscow-1",
            exit_node_id="exit-1",
            assigned_at=old_time.isoformat(),
            last_seen=old_time.isoformat(),
        )
        detector._routing_table["123"] = entry
        
        # Add recent entry
        detector.assign_user_to_entry("456", "entry-moscow-1", "exit-1")
        
        # Cleanup entries older than 1 day
        removed = detector.cleanup_stale_entries(max_age_seconds=86400)
        
        assert removed == 1
        assert "123" not in detector._routing_table
        assert "456" in detector._routing_table


class TestEntryNodeDetectorEdgeCases:
    """Test edge cases and potential bugs."""
    
    def test_detect_from_headers_with_none(self):
        """Test detect_from_headers handles None gracefully."""
        detector = EntryNodeDetector()
        detector.register_entry_node(VPNNode(
            node_id="entry-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
        ))
        
        # This should not crash
        result = detector.detect_from_headers(None)
        assert result is None
    
    def test_find_nearest_entry_with_none_region(self):
        """Test find_nearest_entry handles nodes with None region."""
        detector = EntryNodeDetector()
        detector.register_entry_node(VPNNode(
            node_id="entry-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
            region=None,
            weight=100,
        ))
        
        # Should not crash; should fallback to any node
        node = detector.find_nearest_entry("ru")
        assert node is not None
        assert node.node_id == "entry-1"
    
    def test_update_user_activity_unknown_user(self):
        """Test update_user_activity for unknown chat_id doesn't crash."""
        detector = EntryNodeDetector()
        detector.update_user_activity("unknown", 1024)
        # No assertion needed — just shouldn't raise
    
    def test_get_entry_stats_unknown_node(self):
        """Test get_entry_stats for nonexistent node returns empty stats."""
        detector = EntryNodeDetector()
        stats = detector.get_entry_stats("nonexistent")
        assert stats["user_count"] == 0
        assert stats["total_bytes"] == 0
        assert stats["users"] == []
    
    def test_suggest_entry_preferred_unavailable_fallback(self):
        """Test suggest_entry_for_user falls back when preferred is unavailable."""
        detector = EntryNodeDetector()
        detector.register_entry_node(VPNNode(
            node_id="entry-moscow-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
            region="ru",
            weight=100,
        ))
        detector.register_entry_node(VPNNode(
            node_id="entry-spb-1",
            node_type=NodeType.ENTRY,
            host="185.1.2.3",
            region="ru",
            weight=50,
        ))
        
        # Mark moscow as unavailable by filling its capacity
        detector._entry_nodes["entry-moscow-1"].current_clients = 100
        detector._entry_nodes["entry-moscow-1"].max_clients = 100
        
        node = detector.suggest_entry_for_user(
            chat_id="123",
            user_region="ru",
            preferred_entry="entry-moscow-1",
        )
        
        assert node is not None
        assert node.node_id in {"entry-moscow-1", "entry-spb-1"}
    
    def test_get_entry_node_by_ip_invalid(self):
        """Test get_entry_node_by_ip with invalid IP string."""
        detector = EntryNodeDetector()
        detector.register_entry_node(VPNNode(
            node_id="entry-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
        ))
        
        result = detector.get_entry_node_by_ip("not-an-ip")
        assert result is None
    
    def test_detect_entry_node_with_client_ip_fallback(self):
        """Test detect_entry_node falls back to client_ip detection."""
        detector = EntryNodeDetector()
        detector.register_entry_node(VPNNode(
            node_id="entry-1",
            node_type=NodeType.ENTRY,
            host="203.0.113.20",
        ))
        
        entry_id, node = detector.detect_entry_node({}, client_ip="203.0.113.20")
        assert entry_id == "entry-1"
        assert node is not None
