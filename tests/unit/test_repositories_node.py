"""Tests for NodeRepository."""

import pytest
import sqlite3
import tempfile
import os

from bot.core.repositories.node import NodeRepository
from bot.models.node import Node, NodeType, NodeStatus


class TestNodeRepository:
    """Test NodeRepository functionality."""
    
    @pytest.fixture
    def temp_db(self):
        """Create temporary database with nodes table."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                host TEXT NOT NULL,
                api_port INTEGER DEFAULT 8080,
                vpn_port INTEGER DEFAULT 443,
                base_path TEXT,
                api_username TEXT,
                api_password TEXT,
                public_key TEXT,
                sni TEXT,
                sid TEXT,
                region TEXT,
                city TEXT,
                status TEXT DEFAULT 'active',
                is_primary INTEGER DEFAULT 0,
                weight INTEGER DEFAULT 100,
                max_clients INTEGER DEFAULT 100,
                current_clients INTEGER DEFAULT 0
            )
        ''')
        
        # Insert test nodes
        c.execute('''
            INSERT INTO nodes (name, type, host, status, weight, region)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('exit-1', 'exit', '10.0.0.1', 'active', 100, 'EU'))
        
        c.execute('''
            INSERT INTO nodes (name, type, host, status, weight, region)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('entry-1', 'entry', '10.0.0.2', 'active', 50, 'EU'))
        
        c.execute('''
            INSERT INTO nodes (name, type, host, status, weight, region)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('exit-2', 'exit', '10.0.0.3', 'maintenance', 75, 'US'))
        
        conn.commit()
        conn.close()
        
        yield db_path
        
        os.unlink(db_path)
    
    @pytest.fixture
    def repository(self, temp_db):
        """Create NodeRepository instance."""
        return NodeRepository(temp_db)
    
    def test_create_node(self, repository):
        """Test creating new node."""
        new_node = Node(
            id=None,
            name='exit-3',
            type=NodeType.EXIT,
            host='10.0.0.4',
            api_port=8080,
            vpn_port=443,
            base_path='/api',
            api_username='admin',
            api_password='pass',
            public_key='key123',
            sni='sni.example.com',
            sid='sid123',
            region='Asia',
            city='Tokyo',
            status=NodeStatus.ACTIVE,
            is_primary=False,
            weight=100,
            max_clients=100,
            current_clients=0
        )
        
        node_id = repository.create(new_node)
        
        assert node_id > 0
        
        # Verify created
        created = repository.get_by_id(node_id)
        assert created is not None
        assert created.name == 'exit-3'
        assert created.host == '10.0.0.4'
    
    def test_get_by_id_existing(self, repository):
        """Test get existing node by ID."""
        node = repository.get_by_id(1)
        
        assert node is not None
        assert node.name == 'exit-1'
        assert node.type == NodeType.EXIT
    
    def test_get_by_id_nonexistent(self, repository):
        """Test get non-existent node returns None."""
        node = repository.get_by_id(999)
        
        assert node is None
    
    def test_get_by_name(self, repository):
        """Test get node by name."""
        node = repository.get_by_name('entry-1')
        
        assert node is not None
        assert node.id == 2
        assert node.type == NodeType.ENTRY
    
    def test_get_all(self, repository):
        """Test get all nodes."""
        nodes = repository.get_all()
        
        assert len(nodes) == 3
        # Should be ordered by weight DESC
        assert nodes[0].name == 'exit-1'  # weight 100
    
    def test_get_all_by_type(self, repository):
        """Test get nodes filtered by type."""
        exit_nodes = repository.get_all(node_type=NodeType.EXIT)
        
        assert len(exit_nodes) == 2
        assert all(n.type == NodeType.EXIT for n in exit_nodes)
    
    def test_get_by_status(self, repository):
        """Test get nodes by status."""
        active_nodes = repository.get_by_status(NodeStatus.ACTIVE)
        
        assert len(active_nodes) == 2
        assert all(n.status == NodeStatus.ACTIVE for n in active_nodes)
    
    def test_update_status(self, repository):
        """Test updating node status."""
        result = repository.update_status(1, NodeStatus.MAINTENANCE)
        
        assert result is True
        
        # Verify updated
        node = repository.get_by_id(1)
        assert node.status == NodeStatus.MAINTENANCE
    
    def test_update_client_count(self, repository):
        """Test updating client count."""
        result = repository.update_client_count(1, 50)
        
        assert result is True
        
        # Verify updated
        node = repository.get_by_id(1)
        assert node.current_clients == 50
    
    def test_delete_node(self, repository):
        """Test deleting node."""
        result = repository.delete(2)
        
        assert result is True
        
        # Verify deleted
        node = repository.get_by_id(2)
        assert node is None
        
        # Verify others still exist
        assert repository.get_by_id(1) is not None
