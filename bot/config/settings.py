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
        # Exit node public IP — optional; used by hy2_auth to recognise
        # our own node traffic (geo lookup must not pin users to it).
        self.EXIT_NODE_IP: str = os.getenv('EXIT_NODE_IP', '')
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
        # Salamander obfs shared secret — must match the hysteria server's
        # obfs.salamander.password. Empty → plain Hy2 (throttled by РКН's
        # QUIC filter). See subscription._build_hy2.
        self.HY2_OBFS_PASSWORD: str = os.getenv('HY2_OBFS_PASSWORD', '')
        # Port-hopping range "start:end" (e.g. "20000:40000"). Empty →
        # single fixed HY2_PORT. Entry must DNAT the range to exit:HY2_PORT.
        self.HY2_HOP_PORTS: str = os.getenv('HY2_HOP_PORTS', '').strip()

        # Reserve fallback node (DE) for paid users — a second,
        # provider-independent VLESS+Reality server appended to the
        # subscription for FALLBACK_ALLOWED_STATUSES. Users are
        # provisioned there lazily on /sub fetch via the 2.8.x panel API.
        # Empty FALLBACK_NODE_HOST disables the whole feature.
        self.FALLBACK_NODE_HOST: str = os.getenv('FALLBACK_NODE_HOST', '')
        try:
            self.FALLBACK_NODE_PORT: int = int(os.getenv('FALLBACK_NODE_PORT', '443'))
        except ValueError:
            self.FALLBACK_NODE_PORT = 443
        self.FALLBACK_NODE_SNI: str = os.getenv('FALLBACK_NODE_SNI', 'www.google.com')
        self.FALLBACK_NODE_PBK: str = os.getenv('FALLBACK_NODE_PBK', '')
        self.FALLBACK_NODE_SID: str = os.getenv('FALLBACK_NODE_SID', '')
        self.FALLBACK_NODE_XUI_URL: str = os.getenv('FALLBACK_NODE_XUI_URL', '')
        self.FALLBACK_NODE_XUI_BASE_PATH: str = os.getenv('FALLBACK_NODE_XUI_BASE_PATH', '/sub')
        self.FALLBACK_NODE_XUI_USER: str = os.getenv('FALLBACK_NODE_XUI_USER', 'admin')
        self.FALLBACK_NODE_XUI_PASS: str = os.getenv('FALLBACK_NODE_XUI_PASS', '')
        try:
            self.FALLBACK_NODE_INBOUND_ID: int = int(os.getenv('FALLBACK_NODE_INBOUND_ID', '1'))
        except ValueError:
            self.FALLBACK_NODE_INBOUND_ID = 1

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
        # Primary inbound the client's UUID lives on (VLESS-Reality). Used
        # by the API-mode client add to know which inbound is canonical
        # when a caller doesn't pass one.
        try:
            self.INBOUND_ID: int = int(os.getenv('INBOUND_ID', '1') or '1')
        except ValueError:
            self.INBOUND_ID = 1
        # Second VMess transport inbound (xhttp / WS2). Attached alongside
        # the primary in API mode so new clients get the xhttp fallback too.
        try:
            self.WS2_INBOUND_ID: int = int(os.getenv('WS2_INBOUND_ID', '0') or '0')
        except ValueError:
            self.WS2_INBOUND_ID = 0

        # Outbound mail (backup key delivery) via an external SMTP relay.
        # Empty SMTP_HOST = feature off. See services/email_service.py for
        # why we relay instead of running an MTA on the node.
        self.SMTP_HOST: str = os.getenv('SMTP_HOST', '').strip()
        try:
            self.SMTP_PORT: int = int(os.getenv('SMTP_PORT', '587'))
        except ValueError:
            self.SMTP_PORT = 587
        self.SMTP_USER: str = os.getenv('SMTP_USER', '').strip()
        self.SMTP_PASSWORD: str = os.getenv('SMTP_PASSWORD', '')
        self.SMTP_FROM: str = os.getenv('SMTP_FROM', '').strip()
        self.SMTP_FROM_NAME: str = os.getenv('SMTP_FROM_NAME', 'NekoVPN').strip()

        # Ceiling for one /ai agent turn, seconds. The call runs on a
        # worker thread (doesn't block the bot), so a generous ceiling
        # is safe — plan-mode multi-tool turns routinely take minutes.
        try:
            self.OPENCODE_TIMEOUT: int = int(os.getenv('OPENCODE_TIMEOUT', '600'))
        except ValueError:
            self.OPENCODE_TIMEOUT = 600

        # Minimum severity pushed to Telegram by AlertManager: 'critical'
        # (default) keeps the chat quiet — warns stay dashboard-only;
        # 'warn' restores the old push-everything behavior.
        self.ALERT_TG_MIN_SEVERITY: str = (
            os.getenv('ALERT_TG_MIN_SEVERITY', 'critical') or 'critical'
        ).strip().lower()

        # Demo limits. Freemium: the demo allowance renews monthly (the
        # demo_quota_reset job pushes expiry +30d on the 1st), so a fresh
        # key must live at least until the next run — 7 days would strand
        # mid-month signups until the next reset.
        try:
            self.DEMO_TRAFFIC_GB: int = int(os.getenv('DEMO_TRAFFIC_GB', '5'))
            self.DEMO_DAYS: int = int(os.getenv('DEMO_DAYS', '30'))
            # Monthly allowance a paid grant floors the quota to
            # (grant_paid_access never lowers a higher hand-set quota).
            self.PAID_TRAFFIC_GB: int = int(os.getenv('PAID_TRAFFIC_GB', '100'))
        except ValueError:
            self.DEMO_TRAFFIC_GB = 5
            self.DEMO_DAYS = 30
            self.PAID_TRAFFIC_GB = 100
            
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

        # OpenCode AI agent — the bot talks to a local `opencode serve`
        # HTTP server (headless). Optional: if OPENCODE_URL is empty, the
        # /ai command politely tells the admin AI is not configured.
        self.OPENCODE_URL: str = os.getenv('OPENCODE_URL', '')
        # HTTP basic auth for the OpenCode server. Username defaults to
        # "opencode" (OpenCode's own default); password from env.
        self.OPENCODE_USERNAME: str = os.getenv('OPENCODE_USERNAME', 'opencode')
        self.OPENCODE_SERVER_PASSWORD: str = os.getenv('OPENCODE_SERVER_PASSWORD', '')
        # Default provider/model in OpenCode "provider/model" form (e.g.
        # "moonshotai/kimi-k2" or "anthropic/claude-..."). Empty → let the
        # server / selected agent pick its configured default.
        self.OPENCODE_DEFAULT_MODEL: str = os.getenv('OPENCODE_DEFAULT_MODEL', '')
        # Optional named OpenCode agents (defined in opencode.json) mapped
        # to our /ai switches. Empty → server default agent.
        self.OPENCODE_AGENT_DEFAULT: str = os.getenv('OPENCODE_AGENT_DEFAULT', '')
        self.OPENCODE_AGENT_PLAN: str = os.getenv('OPENCODE_AGENT_PLAN', '')
        self.OPENCODE_AGENT_YOLO: str = os.getenv('OPENCODE_AGENT_YOLO', '')
        # "fast" = default agent, "plan" = deep-think agent. /ai_plan forces plan.
        self.AI_DEFAULT_MODE: str = os.getenv('AI_DEFAULT_MODE', 'fast').lower()
        # Which role runs `opencode serve`: "control" (default — has DB/repo
        # access) or "entry" (direct access to Xray logs from inside RU).
        self.AGENT_NODE_TYPE: str = os.getenv('AGENT_NODE_TYPE', 'control').lower()
        # SSHFS config for when the agent and the data it needs live on
        # different nodes (e.g. agent on control, Xray logs on entry).
        self.ENTRY_NODE_SSH_HOST: str = os.getenv('ENTRY_NODE_SSH_HOST', '')  # e.g., "root@entry.local"
        self.ENTRY_NODE_SSH_KEY: str = os.getenv('ENTRY_NODE_SSH_KEY', '')  # Path to SSH private key
        self.ENTRY_NODE_SSHFS_MOUNT: str = os.getenv('ENTRY_NODE_SSHFS_MOUNT', '/mnt/entry_node')  # Mount point
        # Which agent runner backs /ai + alert analysis: "hermes" (Hermes
        # Agent API server, OpenAI-compatible) or "opencode" (legacy). The
        # factory in bot/services/agent_factory.py picks the client from this.
        self.AGENT_BACKEND: str = os.getenv('AGENT_BACKEND', 'opencode').strip().lower()
        # Hermes Agent API server — used when AGENT_BACKEND=hermes. URL empty
        # disables /ai just like an empty OPENCODE_URL does. HERMES_MODEL is
        # the API alias ("hermes-agent"); the real model lives in Hermes'
        # own config.yaml (model.default).
        self.HERMES_URL: str = os.getenv('HERMES_URL', '')
        self.HERMES_API_KEY: str = os.getenv('HERMES_API_KEY', '')
        self.HERMES_MODEL: str = os.getenv('HERMES_MODEL', 'hermes-agent')
        # If set, ANY message the super-admin posts inside this forum topic
        # is treated as an AI prompt (no /ai prefix needed).
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
