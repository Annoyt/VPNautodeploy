"""Unified VPN Node model - replaces Node and ClusterNode.

This module provides a single Node model that can be used for:
- Database operations (replaces models.node.Node)
- Cluster synchronization (replaces models.cluster.ClusterNode)

Migration:
    Old: from bot.models.node import Node
    New: from bot.models.vpn_node import VPNNode as Node
    
    Old: from bot.models.cluster import ClusterNode
    New: from bot.models.vpn_node import VPNNode as ClusterNode
"""

import warnings
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any


class NodeType(Enum):
    """Type of VPN node."""
    ENTRY = "entry"
    EXIT = "exit"


class NodeStatus(Enum):
    """Status of a node."""
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    OFFLINE = "offline"
    DEGRADED = "degraded"


class NodeRole(Enum):
    """Role of a node in the VPN infrastructure.
    
    DEPRECATED: Use NodeType instead.
    """
    EXIT = "exit"
    ENTRY = "entry"


class NodeState(Enum):
    """State of a node in the cluster."""
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"


@dataclass
class VPNNode:
    """Unified VPN node model.
    
    Combines functionality from:
    - models.node.Node (DB/VPN operations)
    - models.cluster.ClusterNode (cluster sync)
    
    Attributes:
        id: Database ID (int) OR node_id for cluster (str)
        name: Node name
        node_type: Type (entry/exit) - replaces 'type' and 'role'
        host: Hostname or IP
        api_port: API port (default: 8081)
        vpn_port: VPN port (optional for entry nodes)
        api_username: API username (optional for cluster sync)
        api_password: API password (optional for cluster sync)
        public_key: XRay public key
        sni: SNI value
        sid: Short ID (optional)
        region: Geographic region
        city: City name
        status: Node status (active/maintenance/offline)
        is_primary: Whether this is the primary node
        weight: Load balancing weight
        max_clients: Maximum client capacity
        current_clients: Current client count
        health_check_url: Health check endpoint
        last_health_check: Last health check timestamp
        created_at: Creation timestamp
        last_seen: Last seen timestamp (for cluster sync)
        cluster_state: Cluster state (follower/candidate/leader)
    """
    
    # Identification
    id: Optional[int] = None
    node_id: Optional[str] = None
    name: str = ""
    
    # Type and role (unified)
    node_type: NodeType = NodeType.EXIT
    
    # Network
    host: str = ""
    api_port: int = 8081
    vpn_port: Optional[int] = None
    base_path: str = "/this_is_fine"
    
    # Credentials (for DB operations)
    api_username: str = "admin"
    api_password: str = "admin"
    
    # XRay/REALITY settings
    public_key: str = ""
    sni: str = ""
    sid: Optional[str] = None
    
    # Location
    region: str = ""
    city: str = ""
    
    # Status and capacity
    status: NodeStatus = NodeStatus.ACTIVE
    is_primary: bool = False
    weight: int = 100
    max_clients: int = 100
    current_clients: int = 0
    
    # Health tracking
    health_check_url: Optional[str] = None
    last_health_check: Optional[str] = None
    created_at: Optional[str] = None
    last_seen: Optional[str] = None
    
    # Cluster state
    cluster_state: NodeState = NodeState.FOLLOWER
    
    def __post_init__(self):
        """Ensure id/node_id consistency."""
        # If id is set but node_id is not, generate node_id from id
        if self.id is not None and self.node_id is None:
            self.node_id = f"node-{self.id}"
        # If node_id is set but id is not, try to extract id
        elif self.node_id is not None and self.id is None:
            try:
                if self.node_id.startswith('node-'):
                    self.id = int(self.node_id.split('-')[1])
            except (ValueError, IndexError):
                pass
    
    @property
    def api_url(self) -> str:
        """Get API base URL."""
        return f"http://{self.host}:{self.api_port}{self.base_path}"
    
    @property
    def vpn_endpoint(self) -> Optional[str]:
        """Get VPN endpoint (host:port) if vpn_port is set."""
        if self.vpn_port:
            return f"{self.host}:{self.vpn_port}"
        return None
    
    @property
    def is_available(self) -> bool:
        """Check if node is available for new clients."""
        return (
            self.status == NodeStatus.ACTIVE and
            self.current_clients < self.max_clients
        )
    
    @property
    def is_exit_node(self) -> bool:
        """Check if this is an exit node."""
        return self.node_type == NodeType.EXIT
    
    @property
    def is_entry_node(self) -> bool:
        """Check if this is an entry node."""
        return self.node_type == NodeType.ENTRY
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert node to dictionary (for cluster sync/API)."""
        return {
            'id': self.id,
            'node_id': self.node_id,
            'name': self.name,
            'node_type': self.node_type.value,
            'host': self.host,
            'api_port': self.api_port,
            'vpn_port': self.vpn_port,
            'base_path': self.base_path,
            'public_key': self.public_key,
            'sni': self.sni,
            'sid': self.sid,
            'region': self.region,
            'city': self.city,
            'status': self.status.value,
            'is_primary': self.is_primary,
            'weight': self.weight,
            'max_clients': self.max_clients,
            'current_clients': self.current_clients,
            'last_seen': self.last_seen,
            'cluster_state': self.cluster_state.value,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'VPNNode':
        """Create node from dictionary."""
        # Handle both 'type' and 'node_type' keys
        node_type_str = data.get('node_type') or data.get('type', 'exit')
        
        # Handle both 'status' as string and NodeStatus
        status_str = data.get('status', 'active')
        if isinstance(status_str, NodeStatus):
            status = status_str
        else:
            status = NodeStatus(status_str)
        
        return cls(
            id=data.get('id'),
            node_id=data.get('node_id'),
            name=data.get('name', ''),
            node_type=NodeType(node_type_str),
            host=data.get('host', ''),
            api_port=data.get('api_port', 8081),
            vpn_port=data.get('vpn_port'),
            base_path=data.get('base_path', '/this_is_fine'),
            api_username=data.get('api_username', 'admin'),
            api_password=data.get('api_password', 'admin'),
            public_key=data.get('public_key', ''),
            sni=data.get('sni', ''),
            sid=data.get('sid'),
            region=data.get('region', ''),
            city=data.get('city', ''),
            status=status,
            is_primary=data.get('is_primary', False),
            weight=data.get('weight', 100),
            max_clients=data.get('max_clients', 100),
            current_clients=data.get('current_clients', 0),
            health_check_url=data.get('health_check_url'),
            last_health_check=data.get('last_health_check'),
            created_at=data.get('created_at'),
            last_seen=data.get('last_seen'),
            cluster_state=NodeState(data.get('cluster_state', 'follower')),
        )
    
    @classmethod
    def from_row(cls, row: tuple) -> 'VPNNode':
        """Create node from database row.
        
        Args:
            row: Database row tuple or sqlite3.Row with columns in table order.
        """
        # Support both sqlite3.Row (named access) and plain tuple (positional)
        if not hasattr(row, 'keys'):
            row = {
                'id': row[0], 'name': row[1], 'type': row[2], 'host': row[3],
                'api_port': row[4], 'vpn_port': row[5], 'base_path': row[6],
                'api_username': row[7], 'api_password': row[8], 'public_key': row[9],
                'sni': row[10], 'sid': row[11], 'region': row[12], 'city': row[13],
                'status': row[14], 'is_primary': row[15], 'weight': row[16],
                'max_clients': row[17], 'current_clients': row[18],
                'health_check_url': row[19], 'last_health_check': row[20],
                'created_at': row[21]
            }
        return cls(
            id=row['id'],
            name=row['name'],
            node_type=NodeType(row['type']),
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
            current_clients=row['current_clients'],
            health_check_url=row['health_check_url'],
            last_health_check=row['last_health_check'],
            created_at=row['created_at']
        )
    
    # Backward compatibility methods
    @property
    def type(self) -> NodeType:
        """Backward compatibility: returns node_type."""
        return self.node_type
    
    @property
    def role(self) -> NodeRole:
        """Backward compatibility: returns role based on node_type."""
        if self.node_type == NodeType.EXIT:
            return NodeRole.EXIT
        return NodeRole.ENTRY


# Backward compatibility aliases
@dataclass
class Node(VPNNode):
    """Backward compatibility alias for VPNNode.
    
    DEPRECATED: Use VPNNode directly.
    """
    
    def __post_init__(self):
        super().__post_init__()
        warnings.warn(
            "Node is deprecated, use VPNNode instead",
            DeprecationWarning,
            stacklevel=2
        )


class ClusterNode(VPNNode):
    """Backward compatibility alias for VPNNode.
    
    DEPRECATED: Use VPNNode directly.
    
    This class accepts 'role' parameter for backward compatibility
    and converts it to 'node_type'.
    """
    
    def __init__(
        self,
        node_id: Optional[str] = None,
        role: Optional[NodeRole] = None,
        node_type: Optional[NodeType] = None,
        host: str = "",
        api_port: int = 8081,
        vpn_port: Optional[int] = None,
        public_key: str = "",
        sni: str = "",
        region: str = "",
        city: str = "",
        is_primary: bool = False,
        weight: int = 100,
        max_clients: int = 100,
        current_clients: int = 0,
        status: NodeStatus = NodeStatus.ACTIVE,
        **kwargs
    ):
        """Initialize with backward compatibility for 'role' parameter."""
        import warnings
        warnings.warn(
            "ClusterNode is deprecated, use VPNNode instead",
            DeprecationWarning,
            stacklevel=2
        )
        
        # Convert role to node_type if provided
        if role is not None and node_type is None:
            if role == NodeRole.EXIT:
                node_type = NodeType.EXIT
            else:
                node_type = NodeType.ENTRY
        elif node_type is None:
            node_type = NodeType.EXIT
        
        # Initialize VPNNode fields directly
        self.id = None
        self.node_id = node_id
        self.name = kwargs.get('name', '')
        self.node_type = node_type
        self.host = host
        self.api_port = api_port
        self.vpn_port = vpn_port
        self.base_path = kwargs.get('base_path', '/this_is_fine')
        self.api_username = kwargs.get('api_username', 'admin')
        self.api_password = kwargs.get('api_password', 'admin')
        self.public_key = public_key
        self.sni = sni
        self.sid = kwargs.get('sid')
        self.region = region
        self.city = city
        self.status = status
        self.is_primary = is_primary
        self.weight = weight
        self.max_clients = max_clients
        self.current_clients = current_clients
        self.health_check_url = kwargs.get('health_check_url')
        self.last_health_check = kwargs.get('last_health_check')
        self.created_at = kwargs.get('created_at')
        self.last_seen = kwargs.get('last_seen')
        self.cluster_state = kwargs.get('cluster_state', NodeState.FOLLOWER)
    
    @property
    def api_url(self) -> str:
        """Get API base URL (without base_path for backward compatibility)."""
        return f"http://{self.host}:{self.api_port}"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert node to dictionary with 'role' for backward compatibility."""
        return {
            'id': self.id,
            'node_id': self.node_id,
            'name': self.name,
            'role': self.role.value,  # Use 'role' instead of 'node_type'
            'host': self.host,
            'api_port': self.api_port,
            'vpn_port': self.vpn_port,
            'public_key': self.public_key,
            'sni': self.sni,
            'sid': self.sid,
            'region': self.region,
            'city': self.city,
            'status': self.status.value,
            'is_primary': self.is_primary,
            'weight': self.weight,
            'max_clients': self.max_clients,
            'current_clients': self.current_clients,
            'last_seen': self.last_seen,
            'cluster_state': self.cluster_state.value,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ClusterNode':
        """Create node from dictionary with backward compatibility."""
        # Handle 'role' key for backward compatibility
        if 'role' in data and 'node_type' not in data:
            role_str = data['role']
            if isinstance(role_str, str):
                data = {**data, 'node_type': role_str}
        
        # Use parent from_dict but return ClusterNode instance
        node = VPNNode.from_dict(data)
        
        # Convert VPNNode to ClusterNode
        return cls(
            node_id=node.node_id,
            node_type=node.node_type,
            host=node.host,
            api_port=node.api_port,
            vpn_port=node.vpn_port,
            public_key=node.public_key,
            sni=node.sni,
            region=node.region,
            city=node.city,
            is_primary=node.is_primary,
            weight=node.weight,
            max_clients=node.max_clients,
            current_clients=node.current_clients,
            status=node.status,
        )


# Export all
__all__ = [
    'VPNNode',
    'Node',  # deprecated
    'ClusterNode',  # deprecated
    'NodeType',
    'NodeStatus',
    'NodeRole',  # deprecated
    'NodeState',
]
