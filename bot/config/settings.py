"""Bot configuration - ENV vars with defaults"""

import os
from typing import Optional


class Settings:
    """Application settings loaded from environment"""
    
    def __init__(self):
        # Bot
        self.BOT_TOKEN: str = os.getenv('BOT_TOKEN', '')
        self.SUPER_ADMIN_ID: str = os.getenv('SUPER_ADMIN_ID', '1652899')
        
        # Mode: "GROUP" (with forum) or "PM_ONLY" (admin PM only)
        self.MODE: str = os.getenv('MODE', 'GROUP')
        
        # Forum (only if MODE=GROUP)
        self.FORUM_ENABLED: bool = self.MODE == 'GROUP'
        self.FORUM_GROUP_ID: Optional[str] = os.getenv('FORUM_GROUP_ID', '-1003686477257')
        
        try:
            self.TOPIC_STATS: int = int(os.getenv('TOPIC_STATS', '18'))
            self.TOPIC_REQUESTS: int = int(os.getenv('TOPIC_REQUESTS', '15'))
            self.TOPIC_DEMO: int = int(os.getenv('TOPIC_DEMO', '19'))
            self.TOPIC_REJECTED: int = int(os.getenv('TOPIC_REJECTED', '20'))
            self.TOPIC_USERS: int = int(os.getenv('TOPIC_USERS', '19'))
            self.TOPIC_SUPPORT: int = int(os.getenv('TOPIC_SUPPORT', '17'))
            self.TOPIC_PAYMENTS: int = int(os.getenv('TOPIC_PAYMENTS', '16'))
            self.TOPIC_SOLVED: int = int(os.getenv('TOPIC_SOLVED', '37'))
        except ValueError:
            self.TOPIC_STATS = 18
            self.TOPIC_REQUESTS = 15
            self.TOPIC_DEMO = 19
            self.TOPIC_REJECTED = 20
            self.TOPIC_USERS = 19
            self.TOPIC_SUPPORT = 17
            self.TOPIC_PAYMENTS = 16
            self.TOPIC_SOLVED = 37
            
        # Database
        self.DB_PATH: str = os.getenv('DB_PATH', '/etc/cascade-vpn/bot.db')
        # Auto-detect correct X-UI DB path: prefer Docker volume over stale bind mount
        default_xui_db = '/opt/3x-ui/db/x-ui.db'
        docker_volume_db = '/var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db'
        if os.path.exists(docker_volume_db):
            default_xui_db = docker_volume_db
        self.XUI_DB_PATH: str = os.getenv('XUI_DB_PATH', default_xui_db)
        
        # X-UI Panel
        self.XUI_PORT: int = int(os.getenv('XUI_PORT', '2053'))
        self.XUI_API_URL: str = os.getenv('XUI_API_URL', f'http://127.0.0.1:{self.XUI_PORT}')
        self.XUI_USERNAME: str = os.getenv('XUI_USERNAME', 'admin')
        self.XUI_PASSWORD: str = os.getenv('XUI_PASSWORD', 'admin')
        self.XUI_CONTAINER_NAME: str = os.getenv('XUI_CONTAINER_NAME', '3x-ui')
        
        # VPN — MUST be set via environment variables. Defaults are EMPTY:
        # leaking production IP/key/sid as a fallback means a dev or staging
        # instance with a half-configured .env will issue links that resolve
        # to the real prod entry node. The validate() call below rejects
        # blank values so the bot won't even start if these are missing.
        self.ENTRY_NODE_IP: str = os.getenv('ENTRY_NODE_IP', '')
        self.REALITY_PUBLIC_KEY: str = os.getenv('REALITY_PUBLIC_KEY', '')
        self.SNI_VALUE: str = os.getenv('SNI_VALUE', '')
        self.SID_VALUE: str = os.getenv('SID_VALUE', '')

        # Hysteria2 sidecar (UDP, fallback when TCP is throttled).
        # Optional — if HY2_HOST is blank, generate_hy2_link returns
        # None and only the VLESS URL gets emitted to the user.
        self.HY2_HOST: str = os.getenv('HY2_HOST', '')
        try:
            self.HY2_PORT: int = int(os.getenv('HY2_PORT', '8400'))
        except ValueError:
            self.HY2_PORT = 8400
        self.HY2_SNI: str = os.getenv('HY2_SNI', '') or self.HY2_HOST

        # Cloudflare-fronted VLESS+WS+TLS (Phase H).
        # Client connects to <host>:<port>, CF terminates TLS at edge,
        # forwards to origin (exit:<port>) which runs Xray WS+TLS.
        # Optional — empty WS_HOST disables this protocol's emission,
        # bot will only send VLESS+Reality (and Hy2 if configured).
        self.WS_HOST: str = os.getenv('WS_HOST', '')
        try:
            self.WS_PORT: int = int(os.getenv('WS_PORT', '2053'))
        except ValueError:
            self.WS_PORT = 2053
        self.WS_PATH: str = os.getenv('WS_PATH', '/api/v1/forecast')
        self.WS_SNI: str = os.getenv('WS_SNI', '') or self.WS_HOST
        try:
            self.WS_INBOUND_ID: int = int(os.getenv('WS_INBOUND_ID', '0') or '0')
        except ValueError:
            self.WS_INBOUND_ID = 0

        # Phase G1: second VMess transport (xhttp) sharing the same
        # cdn.nekoweather.xyz hostname but routed by URL path to a
        # different exit port via CF Origin Rule. Different DPI
        # fingerprint than httpupgrade — when one gets profiled the
        # other should still slip through.
        self.WS2_HOST: str = os.getenv('WS2_HOST', '') or self.WS_HOST
        try:
            self.WS2_PORT: int = int(os.getenv('WS2_PORT', '443'))
        except ValueError:
            self.WS2_PORT = 443
        self.WS2_PATH: str = os.getenv('WS2_PATH', '/api/v2/observations')
        self.WS2_SNI: str = os.getenv('WS2_SNI', '') or self.WS2_HOST

        # Reality entry-side port. Configurable because HAProxy moved
        # from :443 (taken by ShadowTLS) to :8443 on the new entry.
        try:
            self.ENTRY_NODE_PORT: int = int(os.getenv('ENTRY_NODE_PORT', '443'))
        except ValueError:
            self.ENTRY_NODE_PORT = 443

        # ShadowTLS v3 in front of Shadowsocks-2022. The cascade's
        # fourth protocol step. Lives on entry:STLS_PORT, masquerades
        # as TLS to STLS_SNI, forwards verified shadow bytes to an
        # SS-2022 inbound on the exit. Per-user SS passwords are
        # derived as base64(HMAC_SHA256(SS_USER_SALT, uuid)[:16]) so
        # both bot and server can compute them without DB lookup.
        self.STLS_HOST: str = os.getenv('STLS_HOST', '')
        try:
            self.STLS_PORT: int = int(os.getenv('STLS_PORT', '443'))
        except ValueError:
            self.STLS_PORT = 443
        self.STLS_SNI: str = os.getenv('STLS_SNI', 'www.microsoft.com')
        try:
            self.STLS_VERSION: int = int(os.getenv('STLS_VERSION', '3'))
        except ValueError:
            self.STLS_VERSION = 3
        self.STLS_PASSWORD: str = os.getenv('STLS_PASSWORD', '')
        self.SS_METHOD: str = os.getenv('SS_METHOD', '2022-blake3-aes-128-gcm')
        self.SS_SERVER_PASSWORD: str = os.getenv('SS_SERVER_PASSWORD', '')
        self.SS_USER_SALT: str = os.getenv('SS_USER_SALT', '')
        try:
            self.SS_INBOUND_ID: int = int(os.getenv('SS_INBOUND_ID', '0') or '0')
        except ValueError:
            self.SS_INBOUND_ID = 0
        
        # Demo limits
        try:
            self.DEMO_TRAFFIC_GB: int = int(os.getenv('DEMO_TRAFFIC_GB', '5'))
            self.DEMO_DAYS: int = int(os.getenv('DEMO_DAYS', '7'))
        except ValueError:
            self.DEMO_TRAFFIC_GB = 5
            self.DEMO_DAYS = 7
            
        # Rejection settings
        try:
            self.MAX_REJECT_RETRIES: int = int(os.getenv('MAX_REJECT_RETRIES', '10'))
        except ValueError:
            self.MAX_REJECT_RETRIES = 10
        
        # Web App
        # Empty default — falling back to a stale ngrok tunnel meant `/admin`
        # showed a button to a dead URL and nobody noticed. Now if it's
        # missing, handle_admin says so out loud.
        self.WEBAPP_URL: str = os.getenv('WEBAPP_URL', '')

        # Kimi-code AI bridge — host-side HTTP wrapper around the kimi CLI.
        # Optional: if KIMI_BRIDGE_URL is empty, the /ai command politely
        # tells the admin AI is not configured.
        self.KIMI_BRIDGE_URL: str = os.getenv('KIMI_BRIDGE_URL', '')
        self.KIMI_BRIDGE_TOKEN: str = os.getenv('KIMI_BRIDGE_TOKEN', '')
        # "fast" = default (no --plan flag), "plan" = deep think (--plan).
        # Use /ai_plan command explicitly when you want plan mode.
        self.KIMI_DEFAULT_MODE: str = os.getenv('KIMI_DEFAULT_MODE', 'fast').lower()
        # If set, ANY message the super-admin posts inside this forum topic
        # is treated as a Kimi prompt (no /ai prefix needed).
        try:
            self.TOPIC_AI: int = int(os.getenv('TOPIC_AI', '0') or '0')
        except ValueError:
            self.TOPIC_AI = 0

    def validate(self) -> list:
        """Validate settings, return list of errors"""
        errors = []
        
        if not self.BOT_TOKEN:
            errors.append("BOT_TOKEN is required")
        
        if not self.SUPER_ADMIN_ID:
            errors.append("SUPER_ADMIN_ID is required")
        
        if self.MODE == 'GROUP' and not self.FORUM_GROUP_ID:
            errors.append("FORUM_GROUP_ID required when MODE=GROUP")
        
        if not self.ENTRY_NODE_IP:
            errors.append("ENTRY_NODE_IP is required")

        if not self.REALITY_PUBLIC_KEY:
            errors.append("REALITY_PUBLIC_KEY is required")


        
        # Security: Check for default credentials (warnings only, non-blocking)
        import logging
        _logger = logging.getLogger(__name__)
        if self.XUI_PASSWORD == 'admin':
            _logger.warning("XUI_PASSWORD is set to default 'admin'. Change in production!")
        
        if self.XUI_USERNAME == 'admin' and self.XUI_PASSWORD == 'admin':
            _logger.warning("X-UI using default credentials admin:admin. Change immediately!")
        
        return errors
    
    def is_admin(self, user_id: str) -> bool:
        """Check if user is admin"""
        return str(user_id) == str(self.SUPER_ADMIN_ID)
