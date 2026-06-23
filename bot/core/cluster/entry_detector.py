"""Entry Node detection and routing for multi-node VPN architecture.

Detects which Entry Node traffic comes from and manages user-to-entry routing.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from ipaddress import ip_address, ip_network

from bot.models.cluster import ClusterNode, NodeRole

logger = logging.getLogger(__name__)


@dataclass
class RoutingEntry:
    """Routing entry for user-Entry Node mapping."""
    chat_id: str
    entry_node_id: str
    exit_node_id: str
    assigned_at: str
    last_seen: str
    total_bytes: int = 0
    connection_count: int = 0


class EntryNodeDetector:
    """Detects Entry Node from incoming traffic and manages routing.
    
    Features:
    - Detect Entry Node from HTTP headers
    - IP-based fallback detection
    - Geographic routing optimization
    - User-Entry Node affinity tracking
    """
    
    # Default headers to check
    DEFAULT_ENTRY_HEADER = "X-Entry-Node-ID"
    DEFAULT_ENTRY_IP_HEADER = "X-Entry-Node-IP"
    DEFAULT_FORWARDED_FOR = "X-Forwarded-For"
    
    def __init__(
        self,
        entry_header: str = None,
        entry_ip_header: str = None,
        forwarded_for_header: str = None,
    ):
        self.entry_header = entry_header or self.DEFAULT_ENTRY_HEADER
        self.entry_ip_header = entry_ip_header or self.DEFAULT_ENTRY_IP_HEADER
        self.forwarded_for_header = forwarded_for_header or self.DEFAULT_FORWARDED_FOR
        
        # Known Entry Nodes
        self._entry_nodes: Dict[str, ClusterNode] = {}
        
        # IP ranges for Entry Nodes (for fallback detection)
        self._entry_ip_ranges: Dict[str, ip_network] = {}
        
        # User routing table (chat_id -> RoutingEntry)
        self._routing_table: Dict[str, RoutingEntry] = {}
        
        # Statistics
        self._stats: Dict[str, Dict] = {}
    
    # === Entry Node Registration ===
    
    def register_entry_node(self, node: ClusterNode) -> None:
        """Register an Entry Node for detection.
        
        Args:
            node: Entry Node configuration
        """
        if node.role != NodeRole.ENTRY:
            raise ValueError(f"Node {node.node_id} is not an Entry Node")
        
        self._entry_nodes[node.node_id] = node
        
        # Try to parse IP range from host
        try:
            # If host is IP, create /32 network
            ip = ip_address(node.host)
            self._entry_ip_ranges[node.node_id] = ip_network(f"{ip}/32")
        except ValueError:
            # Host is probably a hostname, skip IP-based detection
            pass
        
        logger.info(f"Registered Entry Node: {node.node_id} ({node.host})")
    
    def unregister_entry_node(self, node_id: str) -> bool:
        """Unregister an Entry Node.
        
        Args:
            node_id: Entry Node ID
            
        Returns:
            True if node was removed
        """
        if node_id in self._entry_nodes:
            del self._entry_nodes[node_id]
            self._entry_ip_ranges.pop(node_id, None)
            logger.info(f"Unregistered Entry Node: {node_id}")
            return True
        return False
    
    def get_entry_node(self, node_id: str) -> Optional[ClusterNode]:
        """Get Entry Node by ID."""
        return self._entry_nodes.get(node_id)
    
    def get_all_entry_nodes(self) -> List[ClusterNode]:
        """Get all registered Entry Nodes."""
        return list(self._entry_nodes.values())
    
    def get_entry_node_by_ip(self, ip_str: str) -> Optional[ClusterNode]:
        """Find Entry Node by IP address.
        
        Args:
            ip_str: IP address string
            
        Returns:
            Matching Entry Node or None
        """
        try:
            ip = ip_address(ip_str)
            for node_id, network in self._entry_ip_ranges.items():
                if ip in network:
                    return self._entry_nodes.get(node_id)
        except ValueError:
            pass
        return None
    
    # === Detection ===
    
    def detect_from_headers(self, headers: dict) -> Optional[str]:
        """Detect Entry Node from HTTP headers.
        
        Args:
            headers: Dictionary of HTTP headers
            
        Returns:
            Entry Node ID or None
        """
        if not headers:
            return None
        
        # Primary: Check explicit Entry Node ID header
        entry_id = headers.get(self.entry_header)
        if entry_id and entry_id in self._entry_nodes:
            return entry_id
        
        # Secondary: Check Entry Node IP header
        entry_ip = headers.get(self.entry_ip_header)
        if entry_ip:
            node = self.get_entry_node_by_ip(entry_ip)
            if node:
                return node.node_id
        
        # Tertiary: Check X-Forwarded-For for original client IP
        # This is less reliable but can help with debugging
        forwarded_for = headers.get(self.forwarded_for_header)
        if forwarded_for:
            # X-Forwarded-For can contain multiple IPs: client, proxy1, proxy2
            # The first IP is the original client
            original_ip = forwarded_for.split(',')[0].strip()
            logger.debug(f"Original client IP from X-Forwarded-For: {original_ip}")
        
        return None
    
    def detect_entry_node(
        self,
        headers: dict,
        client_ip: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[ClusterNode]]:
        """Detect Entry Node with full information.
        
        Args:
            headers: HTTP headers
            client_ip: Direct client IP (optional)
            
        Returns:
            Tuple of (entry_node_id, entry_node_object)
        """
        # Try headers first
        entry_id = self.detect_from_headers(headers)
        if entry_id:
            return entry_id, self._entry_nodes.get(entry_id)
        
        # Fallback to client IP
        if client_ip:
            node = self.get_entry_node_by_ip(client_ip)
            if node:
                return node.node_id, node
        
        return None, None
    
    # === Routing ===
    
    def assign_user_to_entry(
        self,
        chat_id: str,
        entry_node_id: str,
        exit_node_id: str,
    ) -> RoutingEntry:
        """Assign user to specific Entry Node.
        
        Args:
            chat_id: User Telegram chat ID
            entry_node_id: Entry Node ID
            exit_node_id: Exit Node ID
            
        Returns:
            Routing entry
        """
        entry = RoutingEntry(
            chat_id=chat_id,
            entry_node_id=entry_node_id,
            exit_node_id=exit_node_id,
            assigned_at=datetime.now(timezone.utc).isoformat(),
            last_seen=datetime.now(timezone.utc).isoformat(),
        )
        
        self._routing_table[chat_id] = entry
        
        logger.info(
            f"Assigned user {chat_id} to Entry {entry_node_id}, "
            f"Exit {exit_node_id}"
        )
        
        return entry
    
    def get_user_routing(self, chat_id: str) -> Optional[RoutingEntry]:
        """Get routing information for user."""
        return self._routing_table.get(chat_id)
    
    def update_user_activity(self, chat_id: str, bytes_transferred: int = 0) -> None:
        """Update last seen time and stats for user.
        
        Args:
            chat_id: User chat ID
            bytes_transferred: Bytes transferred in this connection
        """
        entry = self._routing_table.get(chat_id)
        if entry:
            entry.last_seen = datetime.now(timezone.utc).isoformat()
            entry.total_bytes += bytes_transferred
            entry.connection_count += 1
    
    def get_users_on_entry(self, entry_node_id: str) -> List[str]:
        """Get all users assigned to specific Entry Node."""
        return [
            entry.chat_id
            for entry in self._routing_table.values()
            if entry.entry_node_id == entry_node_id
        ]
    
    def get_entry_load(self, entry_node_id: str) -> int:
        """Get number of users on specific Entry Node."""
        return len(self.get_users_on_entry(entry_node_id))
    
    # === Geographic Routing ===
    
    def find_nearest_entry(self, user_region: str) -> Optional[ClusterNode]:
        """Find nearest Entry Node based on user region.
        
        Args:
            user_region: User's region code (e.g., 'eu', 'ru', 'us')
            
        Returns:
            Best Entry Node or None
        """
        candidates = [
            node for node in self._entry_nodes.values()
            if node.region and node.region.lower() == user_region.lower()
        ]
        
        if candidates:
            # Sort by weight and load
            candidates.sort(key=lambda n: (
                -n.weight,
                self.get_entry_load(n.node_id),
            ))
            return candidates[0]
        
        # Fallback: return any available Entry Node
        if self._entry_nodes:
            return min(
                self._entry_nodes.values(),
                key=lambda n: self.get_entry_load(n.node_id),
            )
        
        return None
    
    def suggest_entry_for_user(
        self,
        chat_id: str,
        user_region: str,
        preferred_entry: Optional[str] = None,
    ) -> Optional[ClusterNode]:
        """Suggest best Entry Node for new user.
        
        Args:
            chat_id: User chat ID
            user_region: User's region
            preferred_entry: Preferred Entry Node ID (optional)
            
        Returns:
            Suggested Entry Node
        """
        # Check if user already has routing
        existing = self.get_user_routing(chat_id)
        if existing:
            # Return existing Entry Node
            return self._entry_nodes.get(existing.entry_node_id)
        
        # Check preferred entry if specified
        if preferred_entry and preferred_entry in self._entry_nodes:
            node = self._entry_nodes[preferred_entry]
            if node.is_available:
                return node
        
        # Find nearest by region
        return self.find_nearest_entry(user_region)
    
    # === Statistics ===
    
    def get_entry_stats(self, entry_node_id: str) -> dict:
        """Get statistics for Entry Node."""
        users = self.get_users_on_entry(entry_node_id)
        total_bytes = sum(
            entry.total_bytes
            for entry in self._routing_table.values()
            if entry.entry_node_id == entry_node_id
        )
        
        return {
            "entry_node_id": entry_node_id,
            "user_count": len(users),
            "total_bytes": total_bytes,
            "users": users,
        }
    
    def get_all_stats(self) -> dict:
        """Get statistics for all Entry Nodes."""
        return {
            entry_id: self.get_entry_stats(entry_id)
            for entry_id in self._entry_nodes.keys()
        }
    
    def cleanup_stale_entries(self, max_age_seconds: int = 86400) -> int:
        """Remove stale routing entries (users not seen for a while).
        
        Args:
            max_age_seconds: Maximum age of entry in seconds
            
        Returns:
            Number of removed entries
        """
        from datetime import timedelta
        
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        removed = 0
        
        stale_ids = [
            chat_id
            for chat_id, entry in self._routing_table.items()
            if datetime.fromisoformat(entry.last_seen) < cutoff
        ]
        
        for chat_id in stale_ids:
            del self._routing_table[chat_id]
            removed += 1
        
        if removed > 0:
            logger.info(f"Cleaned up {removed} stale routing entries")
        
        return removed
