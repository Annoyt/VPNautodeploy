"""User model."""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class User:
    """User model representing a VPN user."""
    chat_id: str
    username: Optional[str] = None
    previous_state: Optional[str] = None
    reject_count: int = 0
    uuid: Optional[str] = None
    email: Optional[str] = None
    status: str = 'new'
    lang: str = 'ru'
    platform: Optional[str] = None
    support_topic_id: Optional[int] = None
    created_at: str = None
    subscription_expiry: Optional[str] = None
    limit_ip: int = 1
    quota_gb: float = 5.0
    last_traffic_update: Optional[str] = None
    # Tracks which protocol the user gets on the next /mykey call.
    # 0 = Reality (primary), 1 = Hy2, 2 = VMess CDN, 3 = ShadowTLS.
    # Advances on every /mykey so users who report "didn't work" can
    # just send /mykey again and receive the next bypass instead of
    # hunting for tiny cascade buttons.
    next_protocol_idx: int = 0
    # ISO-3166 alpha-2 of the most recent source IP seen for this user
    # (populated on /sub fetch or hy2_auth). Drives the per-region
    # cascade override; None means "use the unrestricted default".
    last_country: Optional[str] = None
    # ASN string of the same source IP ("AS8359"). For Russian users
    # this is the operator slice — MTS/MegaFon/Beeline/Rostelecom/
    # Tattelecom — which is the only sub-country axis we have without
    # a city GeoIP DB. Drives ``cascade_by_asn`` overrides.
    last_asn: Optional[str] = None
    # last_city / last_lat / last_lon — best-guess location from the
    # db-ip city-lite mmdb on the most recent /sub fetch or hy2_auth.
    # Used by the Signals map; missing for users whose IP fell outside
    # the city DB coverage (≈country-centroid only).
    last_city: Optional[str] = None
    last_lat: Optional[float] = None
    last_lon: Optional[float] = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now().isoformat()
    
    @staticmethod
    def _safe_get(row, key: str, default=None):
        """Safely get value from row, handling sqlite3.Row, dict, and tuple."""
        if hasattr(row, 'get'):
            return row.get(key, default)
        try:
            return row[key]
        except (KeyError, IndexError):
            return default
    
    @classmethod
    def from_row(cls, row) -> 'User':
        """Create User from database row.
        
        Args:
            row: SQLite row object
            
        Returns:
            User instance
        """
        return cls(
            chat_id=cls._safe_get(row, 'chat_id'),
            username=cls._safe_get(row, 'username'),
            previous_state=cls._safe_get(row, 'previous_state'),
            reject_count=cls._safe_get(row, 'reject_count') or 0,
            uuid=cls._safe_get(row, 'uuid'),
            email=cls._safe_get(row, 'email'),
            status=cls._safe_get(row, 'status', 'new'),
            lang=cls._safe_get(row, 'lang', 'ru'),
            platform=cls._safe_get(row, 'platform'),
            support_topic_id=cls._safe_get(row, 'support_topic_id'),
            created_at=cls._safe_get(row, 'created_at'),
            subscription_expiry=cls._safe_get(row, 'subscription_expiry'),
            limit_ip=cls._safe_get(row, 'limit_ip') or 1,
            quota_gb=cls._safe_get(row, 'quota_gb') or 5.0,
            last_traffic_update=cls._safe_get(row, 'last_traffic_update'),
            next_protocol_idx=cls._safe_get(row, 'next_protocol_idx') or 0,
            last_country=cls._safe_get(row, 'last_country'),
            last_asn=cls._safe_get(row, 'last_asn'),
            last_city=cls._safe_get(row, 'last_city'),
            last_lat=cls._safe_get(row, 'last_lat'),
            last_lon=cls._safe_get(row, 'last_lon'),
        )
