"""Static Profile model - Shared VPN keys from XRay-bot"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class StaticProfile:
    """Static VPN profile (shared key for multiple users).
    
    From XRay-bot: allows creating shared VPN keys that multiple
    users can use without creating individual clients in X-UI.
    """
    
    id: Optional[int] = None
    name: str = ""  # e.g., "Premium-EU-01"
    vless_url: str = ""  # VLESS connection URL
    email: str = ""  # X-UI client email
    max_users: int = 10  # Maximum concurrent users
    current_users: int = 0  # Currently assigned users
    created_at: Optional[str] = None
    enabled: bool = True
    
    @property
    def is_full(self) -> bool:
        """Check if profile has reached max capacity."""
        return self.current_users >= self.max_users
    
    @property
    def available_slots(self) -> int:
        """Get number of available slots."""
        return max(0, self.max_users - self.current_users)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            'id': self.id,
            'name': self.name,
            'vless_url': self.vless_url,
            'email': self.email,
            'max_users': self.max_users,
            'current_users': self.current_users,
            'created_at': self.created_at,
            'enabled': self.enabled,
            'is_full': self.is_full,
            'available_slots': self.available_slots
        }
