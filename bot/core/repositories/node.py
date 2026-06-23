"""Node repository for multi-node operations."""

import logging
from typing import Optional, List

from bot.models.node import Node, NodeType, NodeStatus
from .base import BaseRepository

logger = logging.getLogger(__name__)


class NodeRepository(BaseRepository):
    """Repository for node operations."""
    
    def create(self, node: Node) -> int:
        """Create new node.
        
        Args:
            node: Node object
            
        Returns:
            Created node ID
        """
        try:
            with self._transaction() as c:
                c.execute('''
                    INSERT INTO nodes 
                    (name, type, host, api_port, vpn_port, base_path, 
                     api_username, api_password, public_key, sni, sid,
                     region, city, status, is_primary, weight, max_clients, current_clients)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    node.name, node.type.value, node.host, node.api_port, node.vpn_port,
                    node.base_path, node.api_username, node.api_password,
                    node.public_key, node.sni, node.sid,
                    node.region, node.city, node.status.value,
                    node.is_primary, node.weight, node.max_clients, node.current_clients
                ))
                return c.lastrowid
        except Exception as e:
            logger.error(f"Failed to create node {node.name}: {e}")
            return -1
    
    def get_by_id(self, node_id: int) -> Optional[Node]:
        """Get node by ID.
        
        Args:
            node_id: Node ID
            
        Returns:
            Node object or None
        """
        row = self._execute('SELECT * FROM nodes WHERE id = ?', (node_id,))
        return self._row_to_node(row) if row else None
    
    def get_by_name(self, name: str) -> Optional[Node]:
        """Get node by name.
        
        Args:
            name: Node name
            
        Returns:
            Node object or None
        """
        row = self._execute('SELECT * FROM nodes WHERE name = ?', (name,))
        return self._row_to_node(row) if row else None
    
    def get_all(self, node_type: Optional[NodeType] = None) -> List[Node]:
        """Get all nodes, optionally filtered by type.
        
        Args:
            node_type: Filter by node type
            
        Returns:
            List of Node objects
        """
        if node_type:
            rows = self._execute_many(
                'SELECT * FROM nodes WHERE type = ? ORDER BY weight DESC',
                (node_type.value,)
            )
        else:
            rows = self._execute_many('SELECT * FROM nodes ORDER BY weight DESC')
        
        return [self._row_to_node(row) for row in rows]
    
    def get_by_status(self, status: NodeStatus) -> List[Node]:
        """Get nodes by status.
        
        Args:
            status: Node status
            
        Returns:
            List of Node objects
        """
        rows = self._execute_many(
            'SELECT * FROM nodes WHERE status = ? ORDER BY weight DESC',
            (status.value,)
        )
        return [self._row_to_node(row) for row in rows]
    
    def update_status(self, node_id: int, status) -> bool:
        """Update node status.
        
        Args:
            node_id: Node ID
            status: New status (NodeStatus enum or string)
            
        Returns:
            True if successful
        """
        status_value = status.value if isinstance(status, NodeStatus) else status
        affected = self._execute_write(
            'UPDATE nodes SET status = ? WHERE id = ?',
            (status_value, node_id)
        )
        return affected > 0
    
    def update_client_count(self, node_id: int, count: int) -> bool:
        """Update current client count.
        
        Args:
            node_id: Node ID
            count: Current number of clients
            
        Returns:
            True if successful
        """
        affected = self._execute_write(
            'UPDATE nodes SET current_clients = ? WHERE id = ?',
            (count, node_id)
        )
        return affected > 0
    
    def delete(self, node_id: int) -> bool:
        """Delete node.
        
        Args:
            node_id: Node ID
            
        Returns:
            True if successful
        """
        affected = self._execute_write('DELETE FROM nodes WHERE id = ?', (node_id,))
        return affected > 0
    
    def _row_to_node(self, row) -> Node:
        """Convert database row to Node object."""
        return Node(
            id=row['id'],
            name=row['name'],
            type=NodeType(row['type']),
            host=row['host'],
            api_port=row['api_port'],
            vpn_port=row['vpn_port'],
            base_path=row['base_path'],
            api_username=row['api_username'],
            api_password=row['api_password'],
            public_key=row['public_key'],
            sni=row['sni'],
            sid=row['sid'],
            region=row['region'],
            city=row['city'],
            status=NodeStatus(row['status']),
            is_primary=bool(row['is_primary']),
            weight=row['weight'],
            max_clients=row['max_clients'],
            current_clients=row['current_clients']
        )
