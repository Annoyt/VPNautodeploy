"""VPN service for key generation and VLESS links - Multi-node support."""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from bot.config import Settings, Platform, PLATFORM_INSTRUCTIONS
from bot.config.constants import BYTES_PER_GB
from bot.utils.exceptions import (
    ConfigurationError,
    VPNGenerationError,
    UserNotFoundError,
    InvalidStateError
)

logger = logging.getLogger(__name__)


class VPNService:
    """Service for VPN client configuration and key generation.
    
    Supports both single-node (legacy) and multi-node configurations.
    """
    
    def __init__(self, config: Settings, node_manager=None):
        """Initialize VPN service.
        
        Args:
            config: Application settings
            node_manager: Optional NodeManager for multi-node support
            
        Raises:
            ConfigurationError: If required configuration is missing
        """
        self.config = config
        self.node_manager = node_manager
        
        # Validate required configuration
        self._validate_config()
    
    def _validate_config(self):
        """Validate required configuration parameters.
        
        Raises:
            ConfigurationError: If required config is missing
        """
        if not self.config.ENTRY_NODE_IP:
            raise ConfigurationError("ENTRY_NODE_IP is not configured")
        
        if not self.config.REALITY_PUBLIC_KEY:
            raise ConfigurationError("REALITY_PUBLIC_KEY is not configured")
        
        if not self.config.SNI_VALUE:
            raise ConfigurationError("SNI_VALUE is not configured")
    
    def generate_uuid(self) -> str:
        """Generate UUID v4 for client.
        
        Returns:
            UUID string in standard format
        """
        return str(uuid.uuid4())
    
    def generate_email(self, chat_id: str, username: Optional[str] = None) -> str:
        """Generate unique email for X-UI client.
        
        Format: user_{chat_id}@nekovo.ru
        
        Args:
            chat_id: User's Telegram chat ID
            username: Optional username for readability
            
        Returns:
            Email string for X-UI client identification
        """
        # Clean chat_id (remove negative sign for groups if present)
        clean_id = str(chat_id).lstrip('-')
        
        if username:
            # Sanitize username (remove special chars)
            safe_username = ''.join(c for c in username if c.isalnum() or c == '_')
            return f"user_{safe_username}_{clean_id}@nekovo.ru"
        
        return f"user_{clean_id}@nekovo.ru"
    
    # ===== Legacy Single-Node Methods =====
    
    def generate_vless_link(self, client_uuid: str, email: str) -> str:
        """Generate VLESS connection link (legacy single-node mode).
        
        Args:
            client_uuid: Client UUID
            email: Client email (used as connection name)
            
        Returns:
            VLESS URL string
            
        Raises:
            VPNGenerationError: If required parameters are missing
        """
        if not client_uuid:
            raise VPNGenerationError("Client UUID is required")
        
        if not email:
            raise VPNGenerationError("Client email is required")
        
        entry_ip = self.config.ENTRY_NODE_IP
        public_key = self.config.REALITY_PUBLIC_KEY
        sni = self.config.SNI_VALUE
        sid = self.config.SID_VALUE
        
        # Reality port is configurable: we moved HAProxy from :443 to
        # :8443 on the entry to free :443 for the ShadowTLS frontend.
        # Defaults to 443 so older single-stack deployments keep working.
        port = int(getattr(self.config, 'ENTRY_NODE_PORT', 443) or 443)
        try:
            return self._build_vless_link(
                client_uuid=client_uuid,
                email=email,
                entry_ip=entry_ip,
                public_key=public_key,
                sni=sni,
                sid=sid,
                port=port,
            )
        except Exception as e:
            logger.error(f"Failed to generate VLESS link: {e}")
            raise VPNGenerationError(f"Failed to build VLESS link: {e}")
    
    # ===== Multi-Node Methods =====
    
    async def generate_vless_link_for_user(
        self,
        chat_id: str,
        user=None
    ) -> str:
        """Generate VLESS link based on user's assigned nodes.
        
        If user has assigned nodes — uses them.
        If not — auto-selects best available nodes and assigns.
        
        Args:
            chat_id: User's Telegram chat ID
            user: User object (required - caller must fetch from DB)
            
        Returns:
            VLESS URL string
            
        Raises:
            UserNotFoundError: If user is None
            InvalidStateError: If user has no UUID
            VPNGenerationError: If failed to generate link
        """
        if user is None:
            raise UserNotFoundError(chat_id)
        
        if not user.uuid:
            raise InvalidStateError(f"User {chat_id} has no UUID assigned")
        
        # Multi-node mode
        if self.node_manager:
            return await self._generate_multi_node_link(chat_id, user)
        
        # Legacy mode
        return self.generate_vless_link(user.uuid, user.email)
    
    async def _generate_multi_node_link(self, chat_id: str, user) -> str:
        """Generate VLESS link using multi-node architecture.
        
        Args:
            chat_id: User's chat ID
            user: User object
            
        Returns:
            VLESS URL string
            
        Raises:
            VPNGenerationError: If no nodes available or generation fails
        """
        # Get user's current node assignment
        user_nodes = await self.node_manager.get_user_nodes(chat_id)
        
        exit_node_id = None
        entry_node_id = None
        
        if user_nodes:
            exit_node_id = user_nodes.get('exit_node_id')
            entry_node_id = user_nodes.get('entry_node_id')
        
        # Auto-assign if not set
        if not exit_node_id:
            exit_node = await self.node_manager.find_best_exit_for_user(chat_id)
            if exit_node:
                exit_node_id = exit_node.id
                # Assign user to nodes
                await self.node_manager.assign_user_to_node(
                    chat_id, exit_node_id, entry_node_id
                )
        
        if not exit_node_id:
            raise VPNGenerationError(f"No exit node available for user {chat_id}")
        
        # Get exit node details
        exit_node = await self.node_manager.get_node(exit_node_id)
        if not exit_node:
            raise VPNGenerationError(f"Exit node {exit_node_id} not found")
        
        # Get entry node (assigned or auto-selected)
        if entry_node_id:
            entry_node = await self.node_manager.get_node(entry_node_id)
        else:
            entry_node = await self.node_manager.get_available_entry()
        
        # Use entry node IP or fallback to config
        if entry_node:
            entry_ip = entry_node.host
        else:
            entry_ip = self.config.ENTRY_NODE_IP
        
        # Generate link with node-specific parameters
        try:
            return self._build_vless_link(
                client_uuid=user.uuid,
                email=user.email,
                entry_ip=entry_ip,
                public_key=exit_node.public_key,
                sni=exit_node.sni,
                sid=exit_node.sid,
                port=exit_node.vpn_port
            )
        except Exception as e:
            logger.error(f"Failed to build multi-node VLESS link: {e}")
            raise VPNGenerationError(f"Failed to build VLESS link: {e}")
    
    def _build_vless_link(
        self,
        client_uuid: str,
        email: str,
        entry_ip: str,
        public_key: str,
        sni: str,
        sid: Optional[str],
        port: int = 443
    ) -> str:
        """Build VLESS URL with given parameters.
        
        Args:
            client_uuid: Client UUID
            email: Client email
            entry_ip: Entry node IP
            public_key: Reality public key
            sni: SNI domain
            sid: Short ID (optional)
            port: Port number
            
        Returns:
            VLESS URL string
        """
        # Build VLESS URL
        # Format: vless://uuid@host:port?params#name
        params = [
            "type=tcp",
            "security=reality",
            f"pbk={public_key}",
            "fp=chrome",
            f"sni={sni}",
        ]
        
        if sid is not None:
            params.append(f"sid={sid}")

        params.append("flow=xtls-rprx-vision")
        params.append("spx=/")

        param_str = "&".join(params)
        
        # Use email prefix as connection name
        connection_name = quote(email.split('@')[0])
        
        return f"vless://{client_uuid}@{entry_ip}:{port}?{param_str}#{connection_name}"

    def generate_hy2_link(
        self,
        client_uuid: str,
        email: str,
    ) -> Optional[str]:
        """Build the Hysteria2 connection URL for this user.

        Returns ``None`` if Hy2 isn't configured (HY2_HOST env empty),
        so callers can ignore Hy2 and emit only VLESS.

        Format (Hiddify consumes this; spec is the upstream hy2 URL):
            hysteria2://<password>@<host>:<port>?sni=<sni>&insecure=0#<name>

        Password = the user's UUID — same secret as VLESS, validated
        by the bot's /api/hy2/auth callback. No separate password DB.
        """
        host = getattr(self.config, 'HY2_HOST', '') or ''
        if not host:
            return None
        port = int(getattr(self.config, 'HY2_PORT', 8400) or 8400)
        sni = getattr(self.config, 'HY2_SNI', host) or host
        connection_name = quote(f"{email.split('@')[0]}-hy2")
        params = [f"sni={sni}", "insecure=0"]
        # Salamander obfs + port hopping — must mirror the hysteria
        # server config or the daemon silently drops every packet
        # (this is exactly what killed raw hy2 links for users whose
        # subscription was fresh but who used try_alt:hy2).
        obfs = getattr(self.config, 'HY2_OBFS_PASSWORD', '') or ''
        if obfs:
            params.append('obfs=salamander')
            params.append(f'obfs-password={obfs}')
        hop = (getattr(self.config, 'HY2_HOP_PORTS', '') or '').strip()
        if hop:
            # "20000:40000" → hy2 URL multi-port form "20000-40000"
            params.append(f'mport={hop.replace(":", "-")}')
            params.append('mportHopInt=30')
        return (
            f"hysteria2://{client_uuid}@{host}:{port}"
            f"?{'&'.join(params)}#{connection_name}"
        )

    def generate_stls_link(
        self,
        client_uuid: str,
        email: str,
    ) -> Optional[str]:
        """Build the ShadowTLS-v3 + Shadowsocks-2022 URL for this user.

        Returns ``None`` if ShadowTLS isn't configured. ShadowTLS hides
        the connection as TLS to ``STLS_SNI`` (default www.microsoft.com)
        from any DPI on the wire — the only way to tell our traffic
        from real Microsoft.com is to know ``STLS_PASSWORD``.

        Per-user SS-2022 password is derived deterministically from
        the user's UUID and SS_USER_SALT so we never store it; the
        x-ui inbound regen script computes the same value.
        """
        import base64
        import hmac
        import hashlib
        host = getattr(self.config, 'STLS_HOST', '') or ''
        stls_password = getattr(self.config, 'STLS_PASSWORD', '') or ''
        salt = getattr(self.config, 'SS_USER_SALT', '') or ''
        server_pw = getattr(self.config, 'SS_SERVER_PASSWORD', '') or ''
        if not (host and stls_password and salt and server_pw):
            return None
        port = int(getattr(self.config, 'STLS_PORT', 443) or 443)
        sni = getattr(self.config, 'STLS_SNI', 'www.microsoft.com') or 'www.microsoft.com'
        version = int(getattr(self.config, 'STLS_VERSION', 3) or 3)
        method = getattr(self.config, 'SS_METHOD', '2022-blake3-aes-128-gcm')
        # Derived SS-2022 user password: keyed-HMAC truncated to 16
        # bytes (the AES-128 key length). Same computation runs on the
        # server when seeding inbound 5 client passwords.
        user_pw_bytes = hmac.new(
            salt.encode(), client_uuid.encode(), hashlib.sha256
        ).digest()[:16]
        user_pw = base64.b64encode(user_pw_bytes).decode()
        # SS-2022 SIP008 share-link form: credentials = method:server_pw:user_pw
        # (both already base64), the whole thing base64-encoded for the
        # URL userinfo portion. Hiddify / sing-box both accept this.
        userinfo = base64.urlsafe_b64encode(
            f"{method}:{server_pw}:{user_pw}".encode()
        ).decode().rstrip('=')
        # shadow-tls plugin params live inside the ?plugin= query param,
        # semicolon-separated and percent-encoded (RFC 3986).
        from urllib.parse import quote as _quote
        plugin_args = ';'.join([
            'shadow-tls',
            f'version={version}',
            f'password={stls_password}',
            f'host={sni}',
        ])
        name = quote(f"{email.split('@')[0]}-stls")
        return (
            f"ss://{userinfo}@{host}:{port}/"
            f"?plugin={_quote(plugin_args, safe='')}#{name}"
        )

    def generate_vmess_xhttp_link(
        self,
        client_uuid: str,
        email: str,
    ) -> Optional[str]:
        """Build the Cloudflare-fronted VMess+xhttp URL for this user.

        Phase G1: parallel transport variant alongside httpupgrade. Both
        share the cdn.nekoweather.xyz hostname; a CF Origin Rule routes
        the /api/v2/observations path to exit:2054 (xhttp) and keeps
        /api/v1/forecast on exit:2053 (httpupgrade). Different transport
        fingerprints — when RKN profiles one, the other should still pass.
        """
        import base64
        import json as _json
        host = getattr(self.config, 'WS2_HOST', '') or ''
        if not host:
            return None
        port = int(getattr(self.config, 'WS2_PORT', 443) or 443)
        sni = getattr(self.config, 'WS2_SNI', host) or host
        path = getattr(self.config, 'WS2_PATH', '/api/v2/observations') or '/'
        vmess_config = {
            "v": "2",
            "ps": f"{email.split('@')[0]}-xhttp",
            "add": host,
            "port": str(port),
            "id": client_uuid,
            "aid": "0",
            "scy": "auto",
            "net": "xhttp",
            "type": "none",
            "host": host,
            "path": path,
            "tls": "tls",
            "sni": sni,
            "alpn": "h2,http/1.1",
            "fp": "chrome",
            "mode": "auto",
        }
        encoded = base64.b64encode(
            _json.dumps(vmess_config, separators=(',', ':')).encode()
        ).decode()
        return f"vmess://{encoded}"

    def generate_vless_ws_link(
        self,
        client_uuid: str,
        email: str,
    ) -> Optional[str]:
        """Build the Cloudflare-fronted VMess+httpupgrade URL for this user.

        Returns ``None`` if WS isn't configured (WS_HOST env empty).

        Despite the legacy "vless_ws" name (kept for caller compatibility),
        this now emits a VMess URL because Xray's VLESS-without-flow
        anti-TLS-in-TLS guard rejected the user's HTTPS traffic on every
        VLESS+WS / VLESS+httpupgrade combo we tried. VMess has no flow
        concept and no such check, so the same UUID flows cleanly.

        URL shape is the canonical base64-JSON VMess link consumed by
        Hiddify / v2rayN / sing-box-based clients.
        """
        import base64
        import json as _json
        host = getattr(self.config, 'WS_HOST', '') or ''
        if not host:
            return None
        port = int(getattr(self.config, 'WS_PORT', 2053) or 2053)
        sni = getattr(self.config, 'WS_SNI', host) or host
        path = getattr(self.config, 'WS_PATH', '/api/v1/forecast') or '/'
        vmess_config = {
            "v": "2",
            "ps": f"{email.split('@')[0]}-cdn",
            "add": host,
            "port": str(port),
            "id": client_uuid,
            "aid": "0",
            "scy": "auto",
            "net": "httpupgrade",
            "type": "none",
            "host": host,
            "path": path,
            "tls": "tls",
            "sni": sni,
            "alpn": "http/1.1",
            "fp": "chrome",
        }
        encoded = base64.b64encode(
            _json.dumps(vmess_config, separators=(',', ':')).encode()
        ).decode()
        return f"vmess://{encoded}"

    async def get_connection_info(self, chat_id: str) -> Optional[dict]:
        """Get connection information for user.
        
        Args:
            chat_id: User's Telegram chat ID
            
        Returns:
            Dict with connection details or None
        """
        if not self.node_manager:
            # Legacy mode
            return {
                'entry_ip': self.config.ENTRY_NODE_IP,
                'exit_ip': self.config.ENTRY_NODE_IP,  # Same in legacy
                'port': 443,
                'type': 'legacy'
            }
        
        # Multi-node mode
        user_nodes = await self.node_manager.get_user_nodes(chat_id)
        
        if not user_nodes:
            return None
        
        exit_node = await self.node_manager.get_node(user_nodes['exit_node_id'])
        entry_node = None
        if user_nodes.get('entry_node_id'):
            entry_node = await self.node_manager.get_node(user_nodes['entry_node_id'])
        
        return {
            'entry_ip': entry_node.host if entry_node else self.config.ENTRY_NODE_IP,
            'exit_ip': exit_node.host if exit_node else 'unknown',
            'port': exit_node.vpn_port if exit_node else 443,
            'exit_node_name': exit_node.name if exit_node else 'unknown',
            'region': exit_node.region if exit_node else 'unknown',
            'type': 'multi-node'
        }
    
    # ===== Common Methods =====
    
    def get_instructions(self, platform: Platform, lang: str = 'ru') -> str:
        """Get platform-specific setup instructions.
        
        Args:
            platform: Target platform enum
            lang: Language code ('ru' or 'en')
            
        Returns:
            Instructions text
        """
        if lang not in PLATFORM_INSTRUCTIONS:
            lang = 'ru'  # Fallback to Russian
        
        instructions = PLATFORM_INSTRUCTIONS[lang].get(platform)
        
        if not instructions:
            # Fallback to generic instructions
            instructions = PLATFORM_INSTRUCTIONS[lang][Platform.OTHER]
        
        return instructions
    
    def create_client_config(
        self,
        chat_id: str,
        username: Optional[str] = None,
        traffic_gb: int = None,
        expiry_days: int = None,
        comment: Optional[str] = None
    ) -> dict:
        """Create complete X-UI client configuration.

        Args:
            chat_id: User's Telegram chat ID
            username: Optional Telegram username
            traffic_gb: Traffic limit in GB (default from config)
            expiry_days: Expiry in days (0 = no expiry)
            comment: Free-text panel note — we pass the user's real
                contact email so an operator can map the synthetic
                identifier back to a person. Never used as the client
                key: panel emails are globally unique, so a
                user-supplied address would let two users collide.

        Returns:
            X-UI client configuration dict
        """
        client_uuid = self.generate_uuid()
        email = self.generate_email(chat_id, username)
        
        # Use defaults from config if not specified
        if traffic_gb is None:
            traffic_gb = self.config.DEMO_TRAFFIC_GB
        else:
            traffic_gb = max(0, traffic_gb)

        # Convert GB to bytes
        total_bytes = traffic_gb * BYTES_PER_GB
        
        # Calculate expiry timestamp (0 = no expiry)
        expiry_timestamp = 0
        if expiry_days and expiry_days > 0:
            expiry_date = datetime.now() + timedelta(days=expiry_days)
            expiry_timestamp = int(expiry_date.timestamp() * 1000)
        
        return {
            "id": client_uuid,
            "flow": "xtls-rprx-vision",
            "email": email,
            "limitIp": 1,
            "totalGB": total_bytes,
            "expiryTime": expiry_timestamp,
            "enable": True,
            "comment": comment or ""
        }
    
    def get_client_info(self, client_config: dict) -> dict:
        """Extract readable info from client config.
        
        Args:
            client_config: X-UI client configuration dict
            
        Returns:
            Human-readable client info
        """
        total_gb = client_config.get('totalGB', 0) / BYTES_PER_GB
        
        expiry = "No expiry"
        expiry_ts = client_config.get('expiryTime', 0)
        if expiry_ts > 0:
            expiry = datetime.fromtimestamp(expiry_ts / 1000).strftime('%Y-%m-%d')
        
        return {
            'uuid': client_config.get('id'),
            'email': client_config.get('email'),
            'traffic_gb': round(total_gb, 2),
            'expiry': expiry,
            'enabled': client_config.get('enable', False)
        }
    
    def get_connection_preview(self, vless_link: str) -> dict:
        """Parse VLESS link and return preview info.
        
        Args:
            vless_link: VLESS URL
            
        Returns:
            Parsed connection details
        """
        from urllib.parse import urlparse, parse_qs
        
        try:
            parsed = urlparse(vless_link)
            
            if parsed.scheme != 'vless':
                raise ValueError("Not a VLESS link")
            
            uuid = parsed.username
            host = parsed.hostname
            port = parsed.port or 443
            name = parsed.fragment or 'unnamed'
            
            params = parse_qs(parsed.query)
            
            return {
                'uuid': uuid,
                'host': host,
                'port': port,
                'name': name,
                'security': params.get('security', [''])[0],
                'sni': params.get('sni', [''])[0],
                'flow': params.get('flow', [''])[0],
                'valid': True
            }
        except (ValueError, AttributeError) as e:
            return {
                'valid': False,
                'error': str(e)
            }
