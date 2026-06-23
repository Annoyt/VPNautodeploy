"""Node model for multi-node architecture."""

from dataclasses import dataclass
from typing import Optional
from enum import Enum


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


@dataclass
class Node:
    """VPN node configuration.
    
    Represents either an Entry Node (traffic relay) or Exit Node (VPN backend).
    """
    id: int
    name: str
    type: NodeType
    host: str
    api_port: int
    vpn_port: int
    base_path: str
    api_username: str
    api_password: str
    public_key: str
    sni: str
    sid: Optional[str]
    region: str
    city: str
    status: NodeStatus
    is_primary: bool
    weight: int
    max_clients: int
    current_clients: int
    health_check_url: Optional[str] = None
    last_health_check: Optional[str] = None
    created_at: Optional[str] = None
    
    @property
    def api_url(self) -> str:
        """Get API base URL."""
        return f"http://{self.host}:{self.api_port}"
    
    @property
    def vpn_endpoint(self) -> str:
        """Get VPN endpoint (host:port)."""
        return f"{self.host}:{self.vpn_port}"
    
    @property
    def is_available(self) -> bool:
        """Check if node is available for new clients."""
        return (
            self.status == NodeStatus.ACTIVE and
            self.current_clients < self.max_clients
        )
    
    def to_dict(self) -> dict:
        """Convert node to dictionary."""
        return {
            'id': self.id,
            'name': self.name,
            'type': self.type.value,
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
        }
    
    @classmethod
    def from_row(cls, row) -> 'Node':
        """Create Node from database row.
        
        Args:
            row: Database row (sqlite3.Row or tuple)
        """
        # Use column names if available (sqlite3.Row), fallback to positional
        def get(col: str, idx: int):
            return row[col] if hasattr(row, 'keys') else row[idx]
        
        return cls(
            id=get('id', 0),
            name=get('name', 1),
            type=NodeType(get('type', 2)),
            host=get('host', 3),
            api_port=get('api_port', 4),
            vpn_port=get('vpn_port', 5),
            base_path=get('base_path', 6),
            api_username=get('api_username', 7),
            api_password=get('api_password', 8),
            public_key=get('public_key', 9),
            sni=get('sni', 10),
            sid=get('sid', 11),
            region=get('region', 12),
            city=get('city', 13),
            status=NodeStatus(get('status', 14)),
            is_primary=bool(get('is_primary', 15)),
            weight=get('weight', 16),
            max_clients=get('max_clients', 17),
            current_clients=get('current_clients', 18),
            health_check_url=get('health_check_url', 19),
            last_health_check=get('last_health_check', 20),
            created_at=get('created_at', 21)
        )


@dataclass
class UserNodeAssignment:
    """Assignment of user to specific nodes."""
    id: int
    chat_id: str
    exit_node_id: int
    entry_node_id: Optional[int]
    assigned_at: str
    is_active: bool
    
    @classmethod
    def from_row(cls, row: tuple) -> 'UserNodeAssignment':
        """Create from database row."""
        if not hasattr(row, 'keys'):
            row = {
                'id': row[0], 'chat_id': row[1], 'exit_node_id': row[2],
                'entry_node_id': row[3], 'assigned_at': row[4], 'is_active': row[5]
            }
        return cls(
            id=row['id'],
            chat_id=row['chat_id'],
            exit_node_id=row['exit_node_id'],
            entry_node_id=row['entry_node_id'],
            assigned_at=row['assigned_at'],
            is_active=bool(row['is_active'])
        )


@dataclass  
class NodeFailoverLog:
    """Log entry for user failover between nodes."""
    id: int
    chat_id: str
    from_node_id: Optional[int]
    to_node_id: int
    reason: str
    switched_at: str
    
    @classmethod
    def from_row(cls, row: tuple) -> 'NodeFailoverLog':
        """Create from database row."""
        if not hasattr(row, 'keys'):
            row = {
                'id': row[0], 'chat_id': row[1], 'from_node_id': row[2],
                'to_node_id': row[3], 'reason': row[4], 'created_at': row[5]
            }
        return cls(
            id=row['id'],
            chat_id=row['chat_id'],
            from_node_id=row['from_node_id'],
            to_node_id=row['to_node_id'],
            reason=row['reason'],
            switched_at=row['created_at']
        )
