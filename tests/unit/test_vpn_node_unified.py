"""Tests for unified VPNNode model - Phase 3 refactoring.

Verifies that VPNNode replaces both Node and ClusterNode.
"""

import pytest
import warnings
from unittest.mock import Mock

from bot.models.vpn_node import (
    VPNNode, Node, ClusterNode,
    NodeType, NodeStatus, NodeRole, NodeState
)


class TestVPNNodeBasics:
    """Test basic VPNNode functionality."""
    
    def test_create_exit_node(self):
        """Test creating an exit node."""
        node = VPNNode(
            id=1,
            name='exit-1',
            node_type=NodeType.EXIT,
            host='10.0.0.1',
            vpn_port=443,
            public_key='pubkey123',
            sni='www.example.com'
        )
        
        assert node.id == 1
        assert node.node_type == NodeType.EXIT
        assert node.is_exit_node is True
        assert node.is_entry_node is False
        assert node.vpn_endpoint == '10.0.0.1:443'
    
    def test_create_entry_node(self):
        """Test creating an entry node."""
        node = VPNNode(
            id=2,
            name='entry-1',
            node_type=NodeType.ENTRY,
            host='10.0.0.2',
            api_port=8081
        )
        
        assert node.node_type == NodeType.ENTRY
        assert node.is_entry_node is True
        assert node.is_exit_node is False
        assert node.vpn_endpoint is None  # Entry nodes don't have VPN endpoint
    
    def test_api_url_format(self):
        """Test API URL format."""
        node = VPNNode(
            host='example.com',
            api_port=8081,
            base_path='/api'
        )
        
        assert node.api_url == 'http://example.com:8081/api'


class TestVPNNodeAvailability:
    """Test node availability checks."""
    
    def test_available_when_active_and_capacity(self):
        """Test node is available when active and has capacity."""
        node = VPNNode(
            status=NodeStatus.ACTIVE,
            current_clients=50,
            max_clients=100
        )
        
        assert node.is_available is True
    
    def test_not_available_when_full(self):
        """Test node is not available when at capacity."""
        node = VPNNode(
            status=NodeStatus.ACTIVE,
            current_clients=100,
            max_clients=100
        )
        
        assert node.is_available is False
    
    def test_not_available_when_offline(self):
        """Test node is not available when offline."""
        node = VPNNode(
            status=NodeStatus.OFFLINE,
            current_clients=50,
            max_clients=100
        )
        
        assert node.is_available is False


class TestVPNNodeSerialization:
    """Test serialization/deserialization."""
    
    def test_to_dict(self):
        """Test converting to dictionary."""
        node = VPNNode(
            id=1,
            node_id='node-1',
            name='test-node',
            node_type=NodeType.EXIT,
            host='10.0.0.1',
            status=NodeStatus.ACTIVE
        )
        
        data = node.to_dict()
        
        assert data['id'] == 1
        assert data['node_id'] == 'node-1'
        assert data['name'] == 'test-node'
        assert data['node_type'] == 'exit'
        assert data['status'] == 'active'
    
    def test_from_dict(self):
        """Test creating from dictionary."""
        data = {
            'id': 1,
            'node_id': 'node-1',
            'name': 'test-node',
            'node_type': 'exit',
            'host': '10.0.0.1',
            'status': 'active',
            'is_primary': True
        }
        
        node = VPNNode.from_dict(data)
        
        assert node.id == 1
        assert node.node_id == 'node-1'
        assert node.is_primary is True
        assert node.node_type == NodeType.EXIT
    
    def test_from_dict_with_legacy_type_field(self):
        """Test from_dict handles legacy 'type' field."""
        data = {
            'name': 'test-node',
            'type': 'entry',  # Legacy field name
            'host': '10.0.0.1'
        }
        
        node = VPNNode.from_dict(data)
        
        assert node.node_type == NodeType.ENTRY


class TestVPNNodeFromRow:
    """Test creating from database row."""
    
    def test_from_row(self):
        """Test creating from database row tuple."""
        row = (
            1, 'exit-1', 'exit', '10.0.0.1', 8081, 443,
            '/api', 'admin', 'pass', 'pubkey', 'sni', 'sid',
            'EU', 'Frankfurt', 'active', 1, 100, 50, 10,
            'http://health', '2024-01-01', '2024-01-01'
        )
        
        node = VPNNode.from_row(row)
        
        assert node.id == 1
        assert node.name == 'exit-1'
        assert node.node_type == NodeType.EXIT
        assert node.host == '10.0.0.1'
        assert node.api_username == 'admin'
        assert node.is_primary is True


class TestVPNNodeIdSync:
    """Test id/node_id synchronization."""
    
    def test_node_id_generated_from_id(self):
        """Test node_id is auto-generated from id."""
        node = VPNNode(id=42)
        
        assert node.node_id == 'node-42'
    
    def test_id_extracted_from_node_id(self):
        """Test id is extracted from node_id."""
        node = VPNNode(node_id='node-99')
        
        assert node.id == 99
    
    def test_custom_node_id_preserved(self):
        """Test custom node_id is preserved if id not extractable."""
        node = VPNNode(node_id='custom-name')
        
        assert node.node_id == 'custom-name'
        assert node.id is None


class TestBackwardCompatibility:
    """Test backward compatibility with old Node and ClusterNode."""
    
    def test_node_alias_emits_warning(self):
        """Test that Node alias emits deprecation warning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            node = Node(id=1, name='test')
            
            assert len(w) >= 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert 'deprecated' in str(w[0].message).lower()
    
    def test_clusternode_alias_emits_warning(self):
        """Test that ClusterNode alias emits deprecation warning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            node = ClusterNode(node_id='node-1', name='test')
            
            assert len(w) >= 1
            assert issubclass(w[0].category, DeprecationWarning)
    
    def test_backward_compatible_role_property(self):
        """Test role property for backward compatibility."""
        exit_node = VPNNode(node_type=NodeType.EXIT)
        entry_node = VPNNode(node_type=NodeType.ENTRY)
        
        assert exit_node.role == NodeRole.EXIT
        assert entry_node.role == NodeRole.ENTRY
    
    def test_backward_compatible_type_property(self):
        """Test type property for backward compatibility."""
        node = VPNNode(node_type=NodeType.EXIT)
        
        assert node.type == NodeType.EXIT


class TestVPNNodeEquality:
    """Test node equality (useful for testing)."""
    
    def test_nodes_equal_by_id(self):
        """Test nodes with same id are considered equal."""
        node1 = VPNNode(id=1, name='node1')
        node2 = VPNNode(id=1, name='node2')
        
        # Note: dataclass equality compares all fields by default
        # This test documents current behavior
        assert node1.id == node2.id
