"""Aiohttp web server for Telegram Mini App and API"""

import logging
import json
import threading
import asyncio
import hmac
import hashlib
from urllib.parse import parse_qs, unquote
from aiohttp import web
from pathlib import Path
from typing import Optional

from bot.config import Settings
from bot.config.constants import UserState
from bot.core.database import Database
from bot.core.state_machine import StateMachine
from bot.services.user_lifecycle import revoke_user_key
from bot.services.xui_service import XUIService
from bot.services.system_stats import SystemStatsService
from bot.services.subscription import SubscriptionService
from bot.utils.admin_token import verify_admin_token
from bot.utils.rate_limit import check_admin_rate_limit, get_admin_rate_limit_remaining
from bot.utils.prometheus import metrics, set_gauge_users, set_system_gauge

logger = logging.getLogger(__name__)


# Actions that map cleanly to a target UserState via StateMachine.transition().
ACTION_STATE_MAP = {
    'approve': UserState.PLATFORM_SELECT,
    'reject': UserState.REJECTED,
    'ban': UserState.BANNED,
    'unban': UserState.NEW,
    # `revoke` is like `ban` but also implies the user already had a key —
    # the side-effect handler will additionally call revoke_user_key.
    'revoke': UserState.BANNED,
    # Promote demo/support user to paid. Unlocks the full protocol cascade
    # (hy2/reality) and the DE fallback node on the user's next /sub refresh.
    'grant_paid': UserState.PAID,
}

# Actions that don't translate to a simple state transition. They're handled
# in dedicated branches in handle_user_action.
SPECIAL_ACTIONS = {'reset', 'grant_100gb', 'set_limit_ip', 'set_quota', 'set_expire'}

# Bytes-per-GB constant used by /grant_100gb. Keep in sync with bot.config.constants.
BYTES_PER_GB = 1024 ** 3


class WebAppServer:
    """Lightweight web server for Mini App."""
    
    def __init__(
        self,
        config: Settings,
        db: Database,
        xui_service=None,
        cluster_manager=None,
        health_checker=None,
        notification_service=None,
        bot_instance=None,
    ):
        self.config = config
        self.db = db
        self.app = web.Application()
        self.xui = xui_service or XUIService(config)
        self.cluster_manager = cluster_manager
        self.health_checker = health_checker
        self.notification_service = notification_service
        self.bot = bot_instance
        self.state_machine = StateMachine(db)
        self.subscription = SubscriptionService(config)
        # Connected admin WebSocket clients. handle_user_action /
        # broadcast / other writes publish a dict here so every
        # client repaints instantly instead of waiting for the next
        # 5s poll.
        self._ws_clients: set = set()
        self._setup_routes()
    
    def _setup_routes(self):
        """Register API and static routes."""
        # Public
        self.app.router.add_get('/health', self.handle_health)
        self.app.router.add_get('/metrics', self.handle_metrics)
        self.app.router.add_get('/api/me', self.handle_me)
        # Hysteria2 sidecar auth callback. NOT admin-gated — the
        # hysteria daemon calls this on every new connection. The
        # password (= user UUID) is the auth secret.
        self.app.router.add_post('/api/hy2/auth', self.handle_hy2_auth)
        # Sing-box subscription endpoint. Public — the path token is
        # the auth (HMAC of the user's UUID with BOT_TOKEN as key).
        # Returns the user's full cascade as one config the client
        # refreshes on a schedule; rotates IPs/paths without user
        # action.
        self.app.router.add_get('/sub/{token}', self.handle_subscription)
        
        # Admin — read
        self.app.router.add_get('/api/admin/users', self.handle_admin_users)
        self.app.router.add_get('/api/admin/users/{chat_id}/detail', self.handle_admin_user_detail)
        self.app.router.add_get('/api/admin/health', self.handle_admin_health)
        self.app.router.add_get('/api/admin/stats', self.handle_admin_stats)
        self.app.router.add_get('/api/admin/audit', self.handle_admin_audit)
        self.app.router.add_get('/api/admin/system_info', self.handle_admin_system_info)
        self.app.router.add_get('/api/admin/logs', self.handle_admin_logs)
        self.app.router.add_get('/api/admin/nodes', self.handle_admin_nodes)
        self.app.router.add_get('/api/admin/xui_clients', self.handle_admin_xui_clients)
        self.app.router.add_get('/api/admin/subscriptions', self.handle_admin_subscriptions)
        self.app.router.add_get('/api/admin/online_clients', self.handle_admin_online_clients)
        self.app.router.add_get('/api/admin/traffic_history', self.handle_admin_traffic_history)
        self.app.router.add_get('/api/admin/ws', self.handle_admin_ws)
        self.app.router.add_get('/api/admin/plans', self.handle_admin_plans_get)
        self.app.router.add_get('/api/admin/reminders', self.handle_admin_reminders_get)
        self.app.router.add_get('/api/admin/dpi_metrics', self.handle_admin_dpi_metrics)
        self.app.router.add_get('/api/admin/alerts', self.handle_admin_alerts_get)
        self.app.router.add_get('/api/admin/protocol_stats', self.handle_admin_protocol_stats)
        self.app.router.add_get('/api/admin/dpi_reports', self.handle_admin_dpi_reports_get)
        self.app.router.add_post('/api/admin/alerts/{alert_id}/ack', self.handle_admin_alerts_ack)

        # Admin — write
        self.app.router.add_post(
            '/api/admin/users/{chat_id}/action',
            self.handle_user_action
        )
        self.app.router.add_post(
            '/api/admin/broadcast',
            self.handle_broadcast
        )
        self.app.router.add_post('/api/admin/plans', self.handle_admin_plans_set)
        self.app.router.add_post('/api/admin/reminders', self.handle_admin_reminders_set)
        self.app.router.add_get(
            '/api/admin/cascade_order', self.handle_admin_cascade_order_get,
        )
        self.app.router.add_post(
            '/api/admin/cascade_order', self.handle_admin_cascade_order_set,
        )
        self.app.router.add_get(
            '/api/admin/key_texts', self.handle_admin_key_texts_get,
        )
        self.app.router.add_post(
            '/api/admin/key_texts', self.handle_admin_key_texts_set,
        )
        self.app.router.add_post(
            '/api/admin/reminders/send', self.handle_admin_reminders_send,
        )
        # Failure-report / ASN-heatmap endpoints — back the new Signals
        # dashboard tab where the operator triages user complaints and
        # spots per-ASN blocking patterns.
        self.app.router.add_get(
            '/api/admin/asn_heatmap', self.handle_admin_asn_heatmap,
        )
        self.app.router.add_get(
            '/api/admin/failure_reports', self.handle_admin_failure_reports,
        )
        self.app.router.add_post(
            '/api/admin/failure_reports/{report_id}/ack',
            self.handle_admin_failure_report_ack,
        )
        self.app.router.add_get(
            '/api/admin/geo_points', self.handle_admin_geo_points,
        )
        
        # Static files — explicit routes to avoid conflicts with API
        project_root = Path(__file__).parent.parent.parent
        static_dir = project_root / 'bot' / 'webapp'
        if static_dir.exists():
            self.app.router.add_get('/', self.handle_index)
            self.app.router.add_get('/style.css', self.handle_style)
            self.app.router.add_get('/app.js', self.handle_app_js)
            logger.info(f"Serving static files from {static_dir}")
        else:
            logger.warning(
                f"Static directory {static_dir} not found. "
                "Web UI will be unavailable."
            )

    # ==================== Auth ====================

    def _validate_init_data(self, init_data: str) -> Optional[dict]:
        """Validate Telegram Mini App initData."""
        if not init_data:
            return None
            
        try:
            parsed = {k: v[0] for k, v in parse_qs(init_data).items()}
            hash_str = parsed.pop('hash', None)
            if not hash_str:
                return None
                
            data_check_string = "\n".join(
                [f"{k}={v}" for k, v in sorted(parsed.items())]
            )
            secret_key = hmac.HMAC(
                b"WebAppData", 
                self.config.BOT_TOKEN.encode(), 
                hashlib.sha256
            ).digest()
            calc_hash = hmac.HMAC(
                secret_key, 
                data_check_string.encode(), 
                hashlib.sha256
            ).hexdigest()
            
            if calc_hash == hash_str:
                return json.loads(parsed.get('user', '{}'))
            return None
        except Exception as e:
            logger.error(f"initData validation error: {e}")
            return None

    def _validate_admin(self, request: web.Request) -> Optional[dict]:
        """Validate request is from an authenticated admin.

        Two paths:
          1. Telegram WebApp initData — works only when the dashboard
             was opened via a `web_app` inline button (PM with bot).
          2. Signed `admin_token` query param — works in any browser,
             generated by `/admin` for the URL fallback used in groups.

        Returns:
            Telegram user dict if valid admin, None otherwise.
        """
        init_data = request.query.get('initData', '')
        if init_data:
            tg_user = self._validate_init_data(init_data)
            if tg_user and self.config.is_admin(str(tg_user.get('id'))):
                return tg_user

        admin_token = request.query.get('admin_token', '')
        if admin_token:
            admin_id = verify_admin_token(self.config.BOT_TOKEN, admin_token)
            if admin_id and self.config.is_admin(admin_id):
                return {'id': admin_id, 'username': f'admin_{admin_id}'}

        return None

    def _check_admin_rate_limit(self, request: web.Request) -> bool:
        """Check if admin request is within rate limit.

        Uses IP address as rate limit key.
        60 requests per minute per IP.

        Args:
            request: Incoming request

        Returns:
            True if within rate limit, False if exceeded
        """
        # Use remote IP as rate limit key
        ip = request.remote or 'unknown'
        return check_admin_rate_limit(ip)

    def _validate_admin_with_rate_limit(
        self,
        request: web.Request
    ) -> tuple[Optional[dict], Optional[web.Response]]:
        """Validate admin auth AND rate limit in one call.

        Args:
            request: Incoming request

        Returns:
            (tg_user_dict, error_response) - one will be None
            - If auth OK and rate limit OK: (user, None)
            - If auth failed: (None, 401 response)
            - If rate limited: (None, 429 response)
        """
        # Check rate limit first (cheaper)
        if not self._check_admin_rate_limit(request):
            ip = request.remote or 'unknown'
            remaining = get_admin_rate_limit_remaining(ip)
            logger.warning(f"Admin rate limit exceeded for {ip}")
            return None, web.json_response(
                {
                    'error': 'Rate limit exceeded',
                    'remaining': remaining,
                    'retry_after': 60
                },
                status=429
            )

        # Check auth
        tg_user = self._validate_admin(request)
        if not tg_user:
            return None, web.json_response({'error': 'Unauthorized'}, status=401)

        return tg_user, None

    # ==================== Public Endpoints ====================

    async def handle_index(self, request: web.Request) -> web.Response:
        """Serve index.html for Telegram Mini App."""
        project_root = Path(__file__).parent.parent.parent
        static_dir = project_root / 'bot' / 'webapp'
        index_path = static_dir / 'index.html'
        try:
            content = await asyncio.to_thread(index_path.read_text, encoding='utf-8')
            return web.Response(text=content, content_type='text/html')
        except Exception as e:
            logger.error(f"Failed to serve index.html: {e}")
            return web.Response(status=404)

    async def handle_style(self, request: web.Request) -> web.Response:
        """Serve style.css."""
        project_root = Path(__file__).parent.parent.parent
        static_dir = project_root / 'bot' / 'webapp'
        css_path = static_dir / 'style.css'
        try:
            content = await asyncio.to_thread(css_path.read_text, encoding='utf-8')
            return web.Response(text=content, content_type='text/css')
        except Exception as e:
            logger.error(f"Failed to serve style.css: {e}")
            return web.Response(status=404)

    async def handle_app_js(self, request: web.Request) -> web.Response:
        """Serve app.js."""
        project_root = Path(__file__).parent.parent.parent
        static_dir = project_root / 'bot' / 'webapp'
        js_path = static_dir / 'app.js'
        try:
            content = await asyncio.to_thread(js_path.read_text, encoding='utf-8')
            return web.Response(text=content, content_type='application/javascript')
        except Exception as e:
            logger.error(f"Failed to serve app.js: {e}")
            return web.Response(status=404)

    async def handle_health(self, request: web.Request) -> web.Response:
        """Health check endpoint for Docker."""
        try:
            await asyncio.to_thread(self.db.get_stats)
            
            return web.json_response({
                'status': 'healthy',
                'service': 'vpn-bot',
                'database': 'connected'
            }, status=200)
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return web.json_response({
                'status': 'unhealthy',
                'error': 'Internal server error'
            }, status=503)

    async def handle_metrics(self, request: web.Request) -> web.Response:
        """Prometheus metrics endpoint.

        Returns metrics in Prometheus exposition format.
        Can be scraped by Prometheus, Grafana, etc.
        """
        try:
            # Update live gauges before export
            stats = await asyncio.to_thread(self.db.get_stats)

            # User counts by status
            by_status = stats.get('by_status', {})
            set_gauge_users(
                by_status.get('total', 0) + by_status.get('new', 0) +
                by_status.get('pending_demo', 0) + by_status.get('demo', 0) +
                by_status.get('paid', 0),
                'total'
            )
            set_gauge_users(by_status.get('new', 0), 'new')
            set_gauge_users(by_status.get('demo', 0), 'demo')
            set_gauge_users(by_status.get('paid', 0), 'paid')
            set_gauge_users(by_status.get('pending_demo', 0), 'pending')

            # System stats
            try:
                system_stats = await asyncio.to_thread(SystemStatsService.get_stats)
                set_system_gauge('disk_usage_percent', system_stats.get('disk_usage', 0))
                set_system_gauge('memory_usage_percent', system_stats.get('memory_usage', 0))
                set_system_gauge('cpu_usage_percent', system_stats.get('cpu_usage', 0))
            except Exception:
                pass  # Non-fatal

            # Export metrics
            metrics_text = metrics.export()
            return web.Response(
                text=metrics_text,
                content_type='text/plain; version=0.0.4; charset=utf-8'
            )
        except Exception as e:
            logger.error(f"Metrics export failed: {e}")
            return web.Response(status=500)

    async def handle_me(self, request: web.Request) -> web.Response:
        """Fetch current user statistics."""
        init_data = request.query.get('initData')
        tg_user = self._validate_init_data(init_data)
        
        if not tg_user:
            return web.json_response({'error': 'Unauthorized'}, status=401)
        
        chat_id = str(tg_user.get('id'))
        is_admin = self.config.is_admin(chat_id)
        
        if is_admin:
            return web.json_response({
                'is_admin': True,
                'chat_id': chat_id,
                'username': tg_user.get('username') or tg_user.get('first_name')
            })
        
        user = await asyncio.to_thread(self.db.get_user, chat_id)
        if not user or not user.email:
            return web.json_response(
                {'error': 'User not found or no VPN access'}, status=404
            )
            
        traffic = await self.xui.get_client_traffic(user.email) or {}
        
        return web.json_response({
            'is_admin': False,
            'status': user.status,
            'quota_gb': user.quota_gb,
            'consumed_bytes': (
                traffic.get('upload', 0) + traffic.get('download', 0)
            ),
            'expiry': user.subscription_expiry,
            'platform': user.platform
        })

    async def handle_hy2_auth(self, request: web.Request) -> web.Response:
        """Hysteria2 sidecar auth callback.

        Called by the hysteria daemon on every new connection. The
        client sends ``auth`` = the user's UUID (which we also use as
        the Reality client id). We look it up in users.uuid and, if
        the user is active (demo/paid status, not banned, not expired),
        return ``{"ok": true, "id": "<email>"}``. Anything else fails
        closed.

        Why uuid-as-password: avoids a second secret to sync between
        bot and hysteria. Same secret protects the VLESS side already.

        Tracked in audit_log so we have a record of first-connect
        per user per day — useful for the dashboard's Hy2 adoption
        widget once it ships.
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({'ok': False}, status=400)
        password = (payload or {}).get('auth') or ''
        addr = (payload or {}).get('addr') or ''
        # Strip port if present: "1.2.3.4:1234" → "1.2.3.4". IPv6 has
        # colons too — but the hysteria daemon wraps v6 in [] so
        # rsplit(":", 1)[0] strips the port unambiguously.
        addr_ip = addr.rsplit(":", 1)[0] if addr else ''
        if addr_ip.startswith('[') and addr_ip.endswith(']'):
            addr_ip = addr_ip[1:-1]

        # Resolve country / ASN / city once — used by the audit row,
        # the dashboard adoption widget, the per-region cascade tuning,
        # and the new Signals map markers.
        #
        # Entry-node DNAT+MASQUERADE caveat: the hysteria daemon sits on
        # the exit host and all hy2 traffic arrives via the entry node's
        # UDP DNAT with SNAT, so ``addr_ip`` is ALWAYS the entry node's
        # own IP, never the real client IP. Geo-resolving it would pin
        # the user to the entry node's city (Moscow) and clobber the
        # accurate geo captured by /sub fetches. Detect our own node IPs
        # and skip both the geo lookup and the users.last_geo write.
        node_ips = {
            (getattr(self.config, 'ENTRY_NODE_IP', '') or '').strip(),
            (getattr(self.config, 'EXIT_NODE_IP', '') or '').strip(),
        }
        is_node_ip = addr_ip in {ip for ip in node_ips if ip}

        cc = asn = org = None
        city = lat = lon = None
        if not is_node_ip:
            try:
                from bot.services.geoip import (
                    lookup as _geo_lookup, lookup_asn, lookup_city,
                )
                if addr_ip:
                    g = _geo_lookup(addr_ip)
                    if g:
                        cc = g[0]
                    a = lookup_asn(addr_ip)
                    if a:
                        asn = a[0]
                        org = a[1] if len(a) > 1 else None
                    c = lookup_city(addr_ip)
                    if c:
                        city, _region, lat, lon = c
            except Exception:
                pass

        decision = 'deny'
        chat_id_str = None
        email_id = None

        if password:
            try:
                with self.db._connect() as conn:
                    row = conn.execute(
                        "SELECT chat_id, email, status, subscription_expiry "
                        "FROM users WHERE uuid = ? LIMIT 1",
                        (password,),
                    ).fetchone()
            except Exception as e:
                logger.warning(f"hy2_auth: db lookup failed: {e}")
                row = None
            if row:
                chat_id_str, email_id, status, expiry = row[0], row[1], row[2], row[3]
                expired = False
                if expiry:
                    try:
                        from datetime import datetime as _dt
                        exp = _dt.fromisoformat(expiry)
                        if exp < _dt.utcnow():
                            expired = True
                    except Exception:
                        pass
                # Hy2 is a paid-tier protocol (PROTOCOL_TIER): demo
                # subscriptions never contain hy2 links, so the auth
                # callback must not accept demo UUIDs either — otherwise
                # anyone who extracts their UUID from a free key gets a
                # direct-to-entry transport the tier gate was supposed
                # to withhold.
                from bot.handlers.callbacks.user import MyKeyAnswerHandler
                if (not expired
                        and status in MyKeyAnswerHandler.PAID_USER_STATUSES):
                    decision = 'allow'

        # Panel-side quota gate. Hy2 bytes are bridged into the panel's
        # client_traffics on the exit host (hy2-traffic-collector), so
        # the xray+hy2 quota is one shared counter there. Deny when the
        # panel has disabled the client or the quota is spent — without
        # this, an over-quota user kicked out of xray could still ride
        # hy2 forever via reconnects. Panel unreachable → keep 'allow'
        # (availability over enforcement; the exit-side kick loop still
        # covers connected sessions).
        if decision == 'allow' and email_id and self.xui:
            try:
                t = await self.xui.get_client_traffic(email_id)
            except Exception as e:
                logger.warning(f"hy2_auth: panel quota lookup failed: {e}")
                t = None
            if t:
                # XUIService's API path speaks upload/download, its DB
                # fallback up/down — accept both.
                total = t.get('total') or 0
                used = (
                    (t.get('upload') or t.get('up') or 0)
                    + (t.get('download') or t.get('down') or 0)
                )
                if t.get('enable') is False or (total > 0 and used >= total):
                    decision = 'deny'
                    logger.info(
                        f"hy2_auth: quota gate deny {chat_id_str} "
                        f"(used {used} of {total}, enable={t.get('enable')})"
                    )

        # Pin the user's last geo (country / ASN / city / lat / lon)
        # whenever we know them — drives per-region cascade tuning
        # when the same user hits /mykey later via TG DM and supplies
        # marker positions for the Signals map.
        if decision == 'allow' and chat_id_str and (cc or asn or city):
            try:
                with self.db._connect() as conn:
                    conn.execute(
                        "UPDATE users SET "
                        " last_country = COALESCE(?, last_country), "
                        " last_asn     = COALESCE(?, last_asn), "
                        " last_city    = COALESCE(?, last_city), "
                        " last_lat     = COALESCE(?, last_lat), "
                        " last_lon     = COALESCE(?, last_lon) "
                        "WHERE chat_id = ?",
                        (cc, asn, city, lat, lon, chat_id_str),
                    )
                    conn.commit()
            except Exception as e:
                logger.warning(f"hy2_auth: last_geo update failed: {e}")

        # Audit every callback for the adoption widget. Best-effort —
        # failure here can't block the auth response.
        try:
            with self.db._connect() as conn:
                conn.execute(
                    "INSERT INTO hy2_auth_log "
                    "(chat_id, decision, addr_ip, country, asn, as_org) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id_str, decision, addr_ip, cc, asn, org),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"hy2_auth: audit insert failed: {e}")

        if decision == 'allow':
            logger.info(f"hy2_auth: allow {chat_id_str} ({email_id}) from {addr}")
            return web.json_response(
                {'ok': True, 'id': email_id or str(chat_id_str)}
            )
        logger.info(f"hy2_auth: deny from {addr}")
        return web.json_response({'ok': False})

    def _record_sub_fetch(
        self, chat_id: str, country, asn,
        city=None, lat=None, lon=None,
    ) -> None:
        """Insert one row into sub_fetches with the full geo snapshot.
        Runs in a thread because the rest of /sub already uses
        asyncio.to_thread for DB I/O.
        """
        try:
            with self.db._connect() as conn:
                conn.execute(
                    "INSERT INTO sub_fetches "
                    "(chat_id, country, asn, city, lat, lon) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, country, asn, city, lat, lon),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"sub_fetches insert failed: {e}")

    async def handle_subscription(self, request: web.Request) -> web.Response:
        """Serve a sing-box JSON subscription for a single user.

        The path token is HMAC(BOT_TOKEN, user.uuid) — looking it up is
        a linear scan over users WHERE uuid IS NOT NULL. Cache later if
        request rate makes it visible in profiles; at our scale it's
        well under 50ms even with thousands of rows.

        The response includes ``subscription-userinfo`` per the V2Ray
        subscription convention so Hiddify can show quota/expiry in its
        profile panel. 404 with a generic body on miss — we don't tell
        the world which tokens exist.
        """
        token = request.match_info.get('token', '')
        user = await asyncio.to_thread(
            self.subscription.find_user_by_token, self.db, token,
        )
        if not user:
            return web.Response(status=404, text='Not found')
        # Inactive user → 410 so the client stops refreshing. Same as
        # /api/me handling: only demo / paid / support_topic see keys.
        if user.status not in ('demo', 'paid', 'support_topic'):
            return web.Response(status=410, text='Subscription inactive')

        # Geolocate the requesting client. Caddy reverse-proxies us, so
        # the real IP lives in X-Forwarded-For; fall back to peer addr.
        # Used both for the per-region cascade ordering and to update
        # user.last_country so subsequent /mykey (which has no HTTP
        # request) also benefits from the regional tuning.
        client_ip = (
            (request.headers.get('X-Forwarded-For', '') or '')
            .split(',')[0].strip()
            or (request.headers.get('X-Real-IP', '') or '').strip()
            or (request.remote or '')
        )
        country = None
        asn = None
        city = region = None
        lat = lon = None
        try:
            from bot.services.geoip import (
                lookup as _geo, lookup_asn as _asn,
                lookup_city as _city,
            )
            if client_ip:
                g = await asyncio.to_thread(_geo, client_ip)
                if g:
                    country = g[0]
                a = await asyncio.to_thread(_asn, client_ip)
                if a:
                    asn = a[0]
                c = await asyncio.to_thread(_city, client_ip)
                if c:
                    city, region, lat, lon = c
        except Exception:
            pass
        # Best-effort write-through so /mykey resolves the same region
        # the next time the same user hits the cascade flow over TG DM.
        cur_country = user.last_country or ''
        cur_asn = user.last_asn or ''
        cur_city = user.last_city or ''
        if (
            (country and country != cur_country)
            or (asn and asn != cur_asn)
            or (city and city != cur_city)
        ):
            try:
                if country:
                    user.last_country = country
                if asn:
                    user.last_asn = asn
                if city:
                    user.last_city = city
                if lat is not None:
                    user.last_lat = lat
                if lon is not None:
                    user.last_lon = lon
                await asyncio.to_thread(self.db._users.save, user)
            except Exception as e:
                logger.warning(f"sub: last_geo save failed for {user.chat_id}: {e}")

        # Heartbeat row for the per-(country, ASN) heatmap AND the map.
        # Hy2 / Reality users show up in hy2_auth_log / dpi_metrics
        # respectively; sub-only users (CDN via urltest, no direct hit
        # on our infra) need this row or they're invisible.
        if country or asn or city:
            try:
                await asyncio.to_thread(
                    self._record_sub_fetch,
                    user.chat_id, country, asn, city, lat, lon,
                )
            except Exception as e:
                logger.warning(f"sub: fetch log insert failed: {e}")

        # Build the cascade exactly like /mykey does, tier-filtered
        # and — when we know it — ASN/country tuned. ASN wins; country
        # is the fallback.
        from bot.handlers.callbacks.user import MyKeyAnswerHandler
        from bot.services.fallback_node import (
            FALLBACK_ALLOWED_STATUSES,
            FallbackNodeService,
        )
        cascade = MyKeyAnswerHandler.get_cascade_order(
            self.db, user=user, country=country, asn=asn,
        )

        # Paid-tier users get the reserve fallback node appended by the
        # builder below. Provision them there lazily (idempotent, cached)
        # so the outbound actually authenticates when they switch to it.
        # Sync HTTP to the reserve panel — must not block the loop.
        if user.status in FALLBACK_ALLOWED_STATUSES:
            try:
                await asyncio.to_thread(
                    FallbackNodeService(self.config).ensure_client, user,
                )
            except Exception as e:
                logger.warning(f'sub: fallback provisioning failed for {user.chat_id}: {e}')

        # Format selection: default is the sing-box config (Hiddify).
        # ``?format=xray`` gets the Xray-core JSON (imports into Happ as
        # ONE profile — the app passes it 1:1 to the core).
        # ``?format=links`` (or a Happ User-Agent) gets the v2ray-style
        # share-links list — the format that renders as separate servers
        # in Happ / v2rayNG / Streisand.
        fmt = (request.rel_url.query.get('format') or '').lower()
        ua = (request.headers.get('User-Agent', '') or '').lower()
        links_body = None
        if fmt == 'links' or (not fmt and 'happ' in ua):
            links_body = self.subscription.build_links(user, cascade)
        elif fmt == 'xray':
            config_obj = self.subscription.build_xray_config(user, cascade)
        else:
            config_obj = self.subscription.build_singbox_config(user, cascade)

        # Surface quota/expiry to Hiddify's profile panel via the
        # subscription-userinfo header. Bytes-per-GB matches the units
        # the bot logs internally so Hiddify shows the same numbers as
        # the dashboard.
        headers = {
            'profile-update-interval': '6',  # refresh every 6 hours
            'profile-title': 'NekoVPN',
        }
        try:
            quota_bytes = int((user.quota_gb or 0) * BYTES_PER_GB)
            traffic = await self.xui.get_client_traffic(user.email) or {}
            used = int(
                (traffic.get('upload', 0) or 0)
                + (traffic.get('download', 0) or 0)
            )
            parts = [f'upload=0', f'download={used}', f'total={quota_bytes}']
            if user.subscription_expiry:
                from datetime import datetime as _dt
                try:
                    exp = _dt.fromisoformat(user.subscription_expiry)
                    parts.append(f'expire={int(exp.timestamp())}')
                except Exception:
                    pass
            headers['subscription-userinfo'] = '; '.join(parts)
        except Exception as e:
            logger.warning(f"subscription: userinfo header skipped: {e}")

        if links_body is not None:
            return web.Response(
                text=links_body, content_type='text/plain', headers=headers,
            )
        return web.json_response(config_obj, headers=headers)

    # ==================== Admin — Read ====================

    async def handle_admin_users(self, request: web.Request) -> web.Response:
        """Fetch users for admin dashboard with optional filtering."""
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp
        
        # Parse filters
        status_filter = request.query.get('status', '')
        search_query = request.query.get('search', '')
        special_filter = request.query.get('filter', '')
        
        # Fetch users based on filters
        if special_filter == 'tickets':
            users = await asyncio.to_thread(
                self.db._users.get_with_open_tickets
            )
        elif search_query:
            users = await asyncio.to_thread(
                self.db._users.search, search_query
            )
        elif status_filter:
            users = await asyncio.to_thread(
                self.db.get_users_by_status, status_filter
            )
        else:
            users = await asyncio.to_thread(self.db.get_all_users)
        
        # Get traffic data
        try:
            all_traffic = await asyncio.to_thread(self.xui.get_all_traffic)
        except Exception:
            all_traffic = {}
        
        data = []
        for u in users:
            traffic = all_traffic.get(u.email, {}) if u.email else {}
            consumed = traffic.get('upload', 0) + traffic.get('download', 0)
            data.append({
                'chat_id': u.chat_id,
                'username': u.username,
                'status': u.status,
                'quota_gb': u.quota_gb,
                'consumed_gb': round(consumed / (1024**3), 2),
                'expiry': u.subscription_expiry,
                'platform': u.platform,
                'created_at': u.created_at,
                'reject_count': u.reject_count,
                'support_topic_id': u.support_topic_id,
                # email + limit_ip are required by the dashboard for
                # the per-row 🟢 online badge and 🔢 limit badge.
                # Without them the frontend has no way to look up by_email
                # or render the limit count.
                'email': u.email,
                'limit_ip': u.limit_ip,
            })
            
        return web.json_response({'users': data})

    async def handle_admin_health(self, request: web.Request) -> web.Response:
        """Fetch server health status for admin dashboard."""
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp
        
        result = {
            'cluster': None,
            'system': {},
            'services': {},
            'nodes_overview': [],
        }
        
        # System stats (current node only)
        try:
            result['system'] = await asyncio.to_thread(
                SystemStatsService.get_stats
            )
        except Exception as e:
            logger.error(f"Failed to get system stats: {e}")
            result['system'] = {'error': str(e)}
        
        # Cluster status
        if self.cluster_manager:
            try:
                result['cluster'] = self.cluster_manager.get_cluster_status()
                
                # Build nodes overview from cluster peers
                cluster_info = result['cluster']
                nodes = []
                # Current node
                nodes.append({
                    'node_id': cluster_info.get('node_id', 'unknown'),
                    'role': 'exit',
                    'status': 'active',
                    'is_leader': cluster_info.get('is_leader', False),
                })
                # Remote peers
                for nid, info in cluster_info.get('peers', {}).items():
                    nodes.append({
                        'node_id': nid,
                        'role': 'exit' if 'exit' in nid else 'entry',
                        'status': info.get('status', 'unknown'),
                        'is_leader': (
                            cluster_info.get('leader_id') == nid
                        ),
                    })
                result['nodes_overview'] = nodes
            except Exception as e:
                logger.error(f"Failed to get cluster status: {e}")
        
        # Health checks
        if self.health_checker:
            try:
                checks = await self.health_checker.run_all_checks()
                result['services'] = checks
            except Exception as e:
                logger.error(f"Health checks failed: {e}")
                result['services'] = {'error': str(e)}
        
        return web.json_response(result)

    async def handle_admin_stats(self, request: web.Request) -> web.Response:
        """Fetch aggregated statistics for admin dashboard."""
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp
        
        # User stats (by_status, by_platform)
        user_stats = await asyncio.to_thread(self.db.get_stats)
        
        # Registration timeline
        reg_stats = await asyncio.to_thread(
            self.db._users.get_registration_stats
        )
        
        # Traffic summary
        traffic_summary = {'total_consumed_bytes': 0, 'users_above_90_percent': 0}
        try:
            all_traffic = await asyncio.to_thread(self.xui.get_all_traffic)
            all_users = await asyncio.to_thread(self.db.get_all_users)
            
            total_bytes = 0
            above_90 = 0
            active_count = 0
            
            for u in all_users:
                if u.email and u.status in ('demo', 'paid'):
                    t = all_traffic.get(u.email, {})
                    consumed = t.get('upload', 0) + t.get('download', 0)
                    total_bytes += consumed
                    active_count += 1
                    quota_bytes = (u.quota_gb or 5.0) * (1024**3)
                    if quota_bytes > 0 and consumed / quota_bytes > 0.9:
                        above_90 += 1
            
            traffic_summary = {
                'total_consumed_bytes': total_bytes,
                'avg_per_user_bytes': (
                    total_bytes // active_count if active_count else 0
                ),
                'users_above_90_percent': above_90,
            }
        except Exception as e:
            logger.error(f"Failed to compute traffic stats: {e}")
        
        return web.json_response({
            'users': user_stats,
            'traffic': traffic_summary,
            'registrations': reg_stats,
        })

    # ==================== Admin — Detail / Audit / System ====================

    async def handle_admin_user_detail(self, request: web.Request) -> web.Response:
        """Full detail card for a single user.

        Combines the User row, live traffic from x-ui, computed
        subscription status, and the last few admin actions involving
        this user — everything the dashboard needs to render the
        "detail modal" without making 4 separate requests.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        chat_id = request.match_info['chat_id']

        user = await asyncio.to_thread(self.db.get_user, chat_id)
        if not user:
            return web.json_response({'error': f'User {chat_id} not found'}, status=404)

        # Live traffic from x-ui, may be stale if the inbound was reloaded
        traffic_up = traffic_down = 0
        if user.email and self.xui and getattr(self.xui, 'db', None):
            try:
                t = await asyncio.to_thread(self.xui.db.get_client_traffic, user.email)
                if t:
                    traffic_up = int(t.get('upload') or 0)
                    traffic_down = int(t.get('download') or 0)
            except Exception as e:
                logger.warning(f"user_detail: traffic fetch failed for {user.email}: {e}")

        # Last 20 admin actions where this user was the target
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT admin_id, action, details, created_at "
                    "FROM admin_actions WHERE target_id = ? "
                    "ORDER BY id DESC LIMIT 20",
                    (str(chat_id),),
                ).fetchall()
            history = [
                {
                    'admin_id': r[0],
                    'action': r[1],
                    'details': r[2],
                    'at': r[3],
                }
                for r in rows
            ]
        except Exception as e:
            logger.warning(f"user_detail: admin_actions fetch failed: {e}")
            history = []

        quota_bytes = (user.quota_gb or 0) * (1024 ** 3)
        used_bytes = traffic_up + traffic_down
        usage_ratio = (used_bytes / quota_bytes) if quota_bytes > 0 else 0.0

        return web.json_response({
            'chat_id': user.chat_id,
            'username': user.username,
            'status': user.status,
            'previous_state': getattr(user, 'previous_state', None),
            'lang': user.lang,
            'platform': user.platform,
            'uuid': user.uuid,
            'email': user.email,
            'limit_ip': user.limit_ip,
            'quota_gb': user.quota_gb,
            'reject_count': getattr(user, 'reject_count', 0) or 0,
            'created_at': user.created_at,
            'subscription_expiry': user.subscription_expiry,
            'last_traffic_update': getattr(user, 'last_traffic_update', None),
            'traffic': {
                'up': traffic_up,
                'down': traffic_down,
                'total': used_bytes,
                'quota_bytes': int(quota_bytes),
                'usage_ratio': round(usage_ratio, 4),
            },
            'recent_admin_actions': history,
        })

    async def handle_admin_audit(self, request: web.Request) -> web.Response:
        """Most-recent admin actions (any target). Used by the Audit section."""
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)

        try:
            limit = int(request.query.get('limit', '100'))
        except ValueError:
            limit = 100
        limit = max(1, min(limit, 500))

        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT id, admin_id, action, target_id, details, created_at "
                    "FROM admin_actions ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            entries = [
                {
                    'id': r[0],
                    'admin_id': r[1],
                    'action': r[2],
                    'target_id': r[3],
                    'details': r[4],
                    'at': r[5],
                }
                for r in rows
            ]
        except Exception as e:
            logger.exception(f"audit fetch failed: {e}")
            entries = []

        return web.json_response({'count': len(entries), 'entries': entries})

    async def handle_admin_system_info(self, request: web.Request) -> web.Response:
        """Operational summary: git sha, uptime, env (redacted), mode."""
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)

        import os
        import subprocess

        # Git sha — the bot's own /app is COPYed at image build, not a
        # working git tree; we therefore look for /opt/vpn-bot/.git via
        # a host-mounted path if available, otherwise return "n/a".
        git_sha = 'n/a'
        for git_dir in ('/app/.git', '/opt/vpn-bot/.git'):
            if os.path.isdir(git_dir):
                try:
                    git_sha = subprocess.check_output(
                        ['git', '--git-dir', git_dir, 'rev-parse', '--short', 'HEAD'],
                        text=True, timeout=3,
                    ).strip()
                    break
                except Exception:
                    pass

        # Process uptime
        try:
            with open('/proc/self/stat') as f:
                fields = f.read().split()
            starttime_jiffies = int(fields[21])
            with open('/proc/uptime') as f:
                host_up = float(f.read().split()[0])
            hz = os.sysconf('SC_CLK_TCK')
            uptime_sec = int(host_up - starttime_jiffies / hz)
        except Exception:
            uptime_sec = -1

        # Public config snapshot. Whitelist to avoid printing tokens.
        cfg = self.config
        public_keys = [
            'MODE', 'FORUM_ENABLED', 'FORUM_GROUP_ID',
            'ENTRY_NODE_IP', 'SNI_VALUE', 'SID_VALUE',
            'DEMO_TRAFFIC_GB', 'DEMO_DAYS',
            'TOPIC_REQUESTS', 'TOPIC_USERS', 'TOPIC_DEMO',
            'TOPIC_REJECTED', 'TOPIC_STATS', 'TOPIC_PAYMENTS',
            'TOPIC_SUPPORT', 'TOPIC_SOLVED', 'TOPIC_AI',
            'WEBAPP_URL', 'XUI_API_URL', 'XUI_CONTAINER_NAME',
            'OPENCODE_URL', 'AI_DEFAULT_MODE', 'OPENCODE_DEFAULT_MODEL',
        ]
        # SECURITY: never widen this list to BOT_TOKEN, XUI_PASSWORD,
        # REALITY_PUBLIC_KEY, or OPENCODE_SERVER_PASSWORD. The dashboard is
        # admin-only, but Mini Apps cache HTML in clients we don't control.
        settings_dict = {k: getattr(cfg, k, None) for k in public_keys}

        # Indicate (without exposing) whether secrets are set.
        secret_status = {
            'BOT_TOKEN': bool(getattr(cfg, 'BOT_TOKEN', '')),
            'XUI_PASSWORD_default_admin': getattr(cfg, 'XUI_PASSWORD', '') == 'admin',
            'REALITY_PUBLIC_KEY': bool(getattr(cfg, 'REALITY_PUBLIC_KEY', '')),
            'OPENCODE_SERVER_PASSWORD': bool(getattr(cfg, 'OPENCODE_SERVER_PASSWORD', '')),
        }

        return web.json_response({
            'git_sha': git_sha,
            'bot_uptime_sec': uptime_sec,
            'settings': settings_dict,
            'secret_status': secret_status,
        })

    async def handle_admin_logs(self, request: web.Request) -> web.Response:
        """Tail of the bot's log file with optional level filter.

        Query params:
          limit  — max number of lines (1–2000, default 200)
          level  — comma-separated levels to KEEP, e.g. "WARNING,ERROR"
                   (case-insensitive). Empty = all.

        Reads /var/log/vpn-bot/bot.log written by setup_logging() in
        main.py — the same docker volume the container has writable.
        Falls back to bot.log.1 (the rotated previous slice) if the
        current file has fewer lines than asked for.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)

        import os
        try:
            limit = int(request.query.get('limit', '200'))
        except ValueError:
            limit = 200
        limit = max(1, min(limit, 2000))

        level_filter = {
            x.strip().upper()
            for x in (request.query.get('level') or '').split(',')
            if x.strip()
        }

        log_dir = os.environ.get("VPN_BOT_LOG_DIR", "/var/log/vpn-bot")
        paths = [
            os.path.join(log_dir, 'bot.log.1'),  # older first
            os.path.join(log_dir, 'bot.log'),    # current, scanned last so newest stays
        ]

        def _read_tail() -> list[str]:
            lines: list[str] = []
            for p in paths:
                if not os.path.isfile(p):
                    continue
                try:
                    with open(p, 'r', encoding='utf-8', errors='replace') as f:
                        lines.extend(f.readlines())
                except Exception as e:
                    logger.warning(f"logs: failed to read {p}: {e}")
            if level_filter:
                lines = [
                    ln for ln in lines
                    if any(f' - {lvl} - ' in ln for lvl in level_filter)
                ]
            return lines[-limit:]

        try:
            tail = await asyncio.to_thread(_read_tail)
        except Exception as e:
            logger.exception("logs read failed")
            return web.json_response({'error': str(e)}, status=500)

        return web.json_response({
            'count': len(tail),
            'limit': limit,
            'level_filter': sorted(level_filter),
            'log_dir': log_dir,
            'lines': [ln.rstrip('\n') for ln in tail],
        })

    async def handle_admin_nodes(self, request: web.Request) -> web.Response:
        """Read nodes.json + TCP-reachability check per node + (best-effort)
        aggregate traffic from x-ui for exit nodes.

        nodes.json lives next to docker-compose.yml. Both entry and exit
        nodes are reported; reachability is a 2-second TCP connect to
        vpn_port (or 443) — quick failure mode if the entry's DNAT or
        the exit's Xray gets killed.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)

        import json
        import os
        import socket
        import time as time_mod

        nodes_path = os.environ.get(
            "NODES_FILE",
            "/opt/vpn-bot/nodes.json",  # host path mounted into the container
        )
        # Try a couple of likely paths because the container's filesystem
        # may have nodes.json at /app/nodes.json instead.
        candidates = [
            nodes_path,
            '/app/nodes.json',
            os.path.join(os.path.dirname(__file__), '..', '..', 'nodes.json'),
        ]
        nodes_raw = None
        for p in candidates:
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    nodes_raw = json.load(f)
                    break
            except FileNotFoundError:
                continue
            except Exception as e:
                logger.warning(f"nodes: failed to parse {p}: {e}")
                continue

        if nodes_raw is None:
            return web.json_response({'nodes': [], 'error': 'nodes.json not found'})

        nodes = nodes_raw.get('nodes', []) if isinstance(nodes_raw, dict) else nodes_raw

        # Best-effort: aggregate up+down across every x-ui client for
        # "exit" nodes. We only have the one default x-ui service, so
        # all exit nodes get the same total for now; once multi-node
        # ships (phases 2–5 of the failover plan) this becomes per-node.
        exit_total_bytes = 0
        try:
            if self.xui:
                all_traffic = await asyncio.to_thread(self.xui.get_all_traffic)
                for v in (all_traffic or {}).values():
                    if isinstance(v, dict):
                        exit_total_bytes += int(v.get('upload') or 0) + int(v.get('download') or 0)
        except Exception as e:
            logger.warning(f"nodes: traffic aggregate failed: {e}")

        def _probe(host: str, port: int) -> tuple[bool, int]:
            """Return (reachable, latency_ms)."""
            start = time_mod.time()
            try:
                with socket.create_connection((host, port), timeout=2.0):
                    return True, int((time_mod.time() - start) * 1000)
            except Exception:
                return False, -1

        out_nodes = []
        for n in nodes:
            host = n.get('public_ip') or n.get('host') or ''
            role = (n.get('role') or '').lower()
            port = int(n.get('vpn_port') or (443 if role == 'exit' else 22))
            reachable, latency = (False, -1)
            if host:
                reachable, latency = await asyncio.to_thread(_probe, host, port)
            out_nodes.append({
                'id': n.get('id'),
                'name': n.get('name'),
                'role': role,
                'host': host,
                'region': n.get('region'),
                'enabled': n.get('enabled', True),
                'is_primary': n.get('is_primary', False),
                'probe': {
                    'port': port,
                    'reachable': reachable,
                    'latency_ms': latency,
                },
                # Only exits get a (best-effort) total; entries route
                # through DNAT so their counter sits on iptables, not here.
                'traffic_total_bytes': exit_total_bytes if role == 'exit' else None,
            })

        return web.json_response({'nodes': out_nodes})

    async def handle_admin_xui_clients(self, request: web.Request) -> web.Response:
        """List clients straight from the x-ui inbound + cross-ref with bot DB.

        Returns three buckets so the dashboard can render mismatches:
          - synced:        client exists in both x-ui and bot.users
          - orphan_in_xui: x-ui has the client, bot.users doesn't recognise it
          - orphan_in_bot: bot.users has a uuid/email, x-ui inbound doesn't

        Each row gets x-ui-side flow/enable/limitIp/totalGB/expiryTime and
        live up+down traffic; orphan_in_bot rows obviously lack the x-ui
        side. This is the diagnostic page for "why isn't this user's key
        working" — if the same email shows up only on the bot side, the
        startup sync silently failed.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)

        # API-first: on entry there is no local x-ui.db, but the panel
        # API serves the same inbound settings — this page was dead
        # there behind a db-only gate.
        if not self.xui:
            return web.json_response({'error': 'x-ui not available'}, status=503)

        try:
            settings = await self.xui.get_inbound_settings()
        except Exception as e:
            logger.exception("xui_clients: get_inbound_settings failed")
            return web.json_response({'error': str(e)}, status=500)

        xui_clients_raw = []
        if isinstance(settings, dict):
            xui_clients_raw = settings.get('clients') or []

        try:
            all_traffic = await asyncio.to_thread(self.xui.get_all_traffic)
        except Exception as e:
            logger.warning(f"xui_clients: traffic fetch failed: {e}")
            all_traffic = {}

        # Pull bot.users in one shot — quick local table, no concern about
        # paging at our scale (~80 rows).
        try:
            bot_users = await asyncio.to_thread(self.db.get_all_users) or []
        except Exception as e:
            logger.exception("xui_clients: bot users fetch failed")
            return web.json_response({'error': str(e)}, status=500)
        bot_by_email = {u.email: u for u in bot_users if getattr(u, 'email', None)}

        def _traffic_for(email: str) -> dict:
            t = (all_traffic or {}).get(email) or {}
            up = int(t.get('upload') or t.get('up') or 0)
            down = int(t.get('download') or t.get('down') or 0)
            return {'up': up, 'down': down, 'total': up + down}

        synced = []
        orphan_in_xui = []
        seen_emails: set = set()

        for c in xui_clients_raw:
            if not isinstance(c, dict):
                continue
            email = c.get('email') or ''
            seen_emails.add(email)
            entry = {
                'uuid': c.get('id'),
                'email': email,
                'flow': c.get('flow'),
                'enable': bool(c.get('enable', True)),
                'limit_ip': c.get('limitIp'),
                'total_gb_bytes': int(c.get('totalGB') or 0),
                'expiry_time_ms': int(c.get('expiryTime') or 0),
                'traffic': _traffic_for(email),
            }
            bu = bot_by_email.get(email)
            if bu:
                entry['bot_user'] = {
                    'chat_id': bu.chat_id,
                    'username': bu.username,
                    'status': bu.status,
                    'uuid_match': (bu.uuid == c.get('id')),
                }
                synced.append(entry)
            else:
                orphan_in_xui.append(entry)

        orphan_in_bot = [
            {
                'chat_id': u.chat_id,
                'username': u.username,
                'status': u.status,
                'uuid': u.uuid,
                'email': u.email,
            }
            for u in bot_users
            if getattr(u, 'email', None) and u.email not in seen_emails
        ]

        return web.json_response({
            'inbound_total': len(xui_clients_raw),
            'synced_count': len(synced),
            'orphan_in_xui_count': len(orphan_in_xui),
            'orphan_in_bot_count': len(orphan_in_bot),
            'synced': synced,
            'orphan_in_xui': orphan_in_xui,
            'orphan_in_bot': orphan_in_bot,
        })

    async def handle_admin_ws(self, request: web.Request) -> web.WebSocketResponse:
        """Live push channel for the dashboard.

        Wire format (server → client):
          {"type": "online", "data": <same shape as /online_clients>}
          {"type": "stats",  "data": <same shape as /stats>}
          {"type": "user_update", "data": {chat_id, status, …}}
          {"type": "alert", "data": {title, severity, …}}

        The server pushes ``online`` and ``stats`` every 5s and also
        synchronously after every successful POST through
        /api/admin/users/{id}/action or /broadcast, so the dashboard
        sees changes within ~one render frame.

        Auth: same admin_token / initData as the REST endpoints. We
        validate before performing the WS upgrade so an unauth'd
        client never gets a socket.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)

        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        self._ws_clients.add(ws)
        logger.info(f"WS: client connected, total={len(self._ws_clients)}")

        async def _push_periodic():
            """Background task per connection: every 5s push a fresh
            online + stats snapshot. Stops on ws close."""
            while not ws.closed:
                try:
                    await asyncio.sleep(5)
                    if ws.closed:
                        break
                    payload = await self._build_periodic_push()
                    for ev_type, ev_data in payload.items():
                        try:
                            await ws.send_json({'type': ev_type, 'data': ev_data})
                        except Exception:
                            return
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.debug(f"WS periodic push: {e}")

        push_task = asyncio.create_task(_push_periodic())
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    # Clients can ping or request a specific refresh;
                    # we just echo for now so the connection stays
                    # active when the client wants to test.
                    if msg.data == 'ping':
                        await ws.send_str('pong')
                elif msg.type == web.WSMsgType.ERROR:
                    logger.debug(f"WS error: {ws.exception()}")
                    break
        finally:
            push_task.cancel()
            self._ws_clients.discard(ws)
            logger.info(f"WS: client disconnected, total={len(self._ws_clients)}")
        return ws

    async def _build_periodic_push(self) -> dict:
        """Compose the data blocks pushed every 5s. Mirrors the GET
        endpoints so the client renders identically.
        """
        # online_clients block (cheap: log parse + cached tcp-stats)
        from bot.services.xray_log import summarize_activity
        from bot.services.xui_reload import get_tcp_stats
        try:
            geo_lookup = (await asyncio.to_thread(
                lambda: __import__('bot.services.geoip', fromlist=['lookup']).lookup
            ))
        except Exception:
            geo_lookup = None
        try:
            activity = await asyncio.to_thread(summarize_activity)
        except Exception:
            activity = {}
        try:
            panel_emails = set(await self.xui.get_online_clients()) if self.xui else set()
        except Exception:
            panel_emails = set()
        try:
            rtt_by_ip = await asyncio.to_thread(get_tcp_stats)
        except Exception:
            rtt_by_ip = {}
        log_emails = set(activity.keys())
        online_emails = sorted(log_emails | panel_emails)
        by_email = {}
        for email in online_emails:
            act = activity.get(email, {})
            ips = act.get('ips') or []
            rtts = [rtt_by_ip[ip] for ip in ips if ip in rtt_by_ip]
            avg_rtt = round(sum(rtts) / len(rtts), 1) if rtts else None
            ip_geo = {}
            countries = set()
            if geo_lookup:
                for ip in ips:
                    g = geo_lookup(ip)
                    if g:
                        cc, flag = g
                        ip_geo[ip] = {'cc': cc, 'flag': flag}
                        countries.add(cc)
            by_email[email] = {
                'online': True,
                'distinct_ips': act.get('distinct_ips', 0),
                'ips': ips,
                'ip_geo': ip_geo,
                'countries': sorted(countries),
                'sharing_flag': len(countries) > 1,
                'active_connections': act.get('active_connections', 0),
                'distinct_destinations': act.get('distinct_destinations', 0),
                'last_seen': act.get('last_seen'),
                'avg_rtt_ms': avg_rtt,
            }
        online_block = {
            'count': len(online_emails),
            'emails': online_emails,
            'by_email': by_email,
        }
        return {'online': online_block}

    async def _broadcast_ws(self, event_type: str, data: dict) -> None:
        """Push an event to every connected admin WS client.

        Drops a slow / dead client silently — the per-connection loop
        in handle_admin_ws will clean it up via the same
        finally-block path when the next periodic push fails.
        """
        if not self._ws_clients:
            return
        msg = {'type': event_type, 'data': data}
        for ws in list(self._ws_clients):
            try:
                await ws.send_json(msg)
            except Exception:
                pass

    async def handle_admin_traffic_history(self, request: web.Request) -> web.Response:
        """Per-client traffic history series for the dashboard sparkline.

        Query params:
          email       — required, the x-ui client email
          days        — optional (default 14), how far back to fetch
          chat_id     — alternative to email; resolve email from bot.users

        Returns:
          {
            email, days, points: [{ts, total_bytes, delta_bytes}],
            total_window_bytes, total_window_gb
          }

        ``delta_bytes`` is the per-period consumption derived on read
        from the absolute counters. If the counter goes backwards
        (client traffic reset) we report delta=0 for that step instead
        of going negative.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)

        email = request.query.get('email') or ''
        try:
            days = int(request.query.get('days') or '14')
        except ValueError:
            days = 14
        days = max(1, min(days, 90))

        # Allow lookup by chat_id as a convenience for the dashboard
        # detail modal — it has the user object handy, not the email.
        if not email:
            chat_id = request.query.get('chat_id')
            if chat_id:
                user = await asyncio.to_thread(self.db.get_user, chat_id)
                if user and user.email:
                    email = user.email
        if not email:
            return web.json_response(
                {'error': 'email or chat_id required'}, status=400
            )

        from datetime import datetime, timedelta
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()

        def _fetch():
            with self.db._connect() as conn:
                return conn.execute(
                    'SELECT recorded_at, upload_bytes, download_bytes '
                    'FROM traffic_history '
                    'WHERE email = ? AND recorded_at >= ? '
                    'ORDER BY recorded_at ASC',
                    (email, cutoff),
                ).fetchall()
        try:
            rows = await asyncio.to_thread(_fetch)
        except Exception as e:
            logger.exception(f"traffic_history fetch failed: {e}")
            return web.json_response({'error': str(e)}, status=500)

        points = []
        prev = None
        window_delta = 0
        for ts, up, down in rows:
            total = int(up or 0) + int(down or 0)
            if prev is None:
                delta = 0
            else:
                delta = total - prev
                if delta < 0:
                    delta = 0  # counter reset
            window_delta += delta
            points.append({
                'ts': ts,
                'total_bytes': total,
                'delta_bytes': delta,
            })
            prev = total

        return web.json_response({
            'email': email,
            'days': days,
            'points': points,
            'total_window_bytes': window_delta,
            'total_window_gb': round(window_delta / (1024 ** 3), 3),
        })

    async def handle_admin_online_clients(self, request: web.Request) -> web.Response:
        """Live activity per client — online status + recent-traffic counts.

        Combines 3x-ui's ``/onlines`` (binary: who's sending traffic
        right now) with a parse of Xray's access.log over the last
        minute. The log gives ``active_connections`` (distinct
        src_ip:src_port pairs) and ``distinct_destinations``, which on
        our entry→exit DNAT topology are the best proxy for a
        "how many sessions" metric — the literal source-IP count is
        always 1 because all traffic comes from the entry node.

        Returns ``{count, emails, by_email: {email: {online, active_connections,
        distinct_destinations, last_seen}}}`` so the dashboard can render
        every row in O(1).
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        if not self.xui:
            return web.json_response(
                {'count': 0, 'emails': [], 'by_email': {},
                 'error': 'xui not configured'},
            )
        # Best-effort log-side activity (primary), /onlines secondary,
        # and per-IP RTT from the sidecar's /tcp-stats (live `ss -tin`
        # on the entry node).
        from bot.services.xray_log import summarize_activity
        from bot.services.xui_reload import get_tcp_stats
        try:
            activity = await asyncio.to_thread(summarize_activity)
        except Exception as e:
            logger.warning(f"online_clients endpoint: log parse error {e}")
            activity = {}

        try:
            panel_emails = set(await self.xui.get_online_clients())
        except Exception as e:
            logger.warning(f"online_clients endpoint: api error {e}")
            panel_emails = set()

        try:
            rtt_by_ip = await asyncio.to_thread(get_tcp_stats)
        except Exception as e:
            logger.warning(f"online_clients endpoint: tcp_stats error {e}")
            rtt_by_ip = {}

        # Optional GeoIP — soft import so the endpoint still works
        # if maxminddb isn't installed yet (e.g. during the first
        # rebuild after pulling).
        try:
            from bot.services.geoip import lookup as geo_lookup
        except Exception:
            geo_lookup = None

        log_emails = set(activity.keys())
        online_emails = sorted(log_emails | panel_emails)
        by_email = {}
        for email in online_emails:
            act = activity.get(email, {})
            ips = act.get('ips') or []
            rtts = [rtt_by_ip[ip] for ip in ips if ip in rtt_by_ip]
            avg_rtt = round(sum(rtts) / len(rtts), 1) if rtts else None

            # geo: per-IP and aggregate. We surface both so the
            # dashboard can choose: per-row flag = first IP's flag,
            # detail modal = full per-IP geo with sharing detection.
            ip_geo = {}
            countries = set()
            if geo_lookup:
                for ip in ips:
                    g = geo_lookup(ip)
                    if g:
                        cc, flag = g
                        ip_geo[ip] = {'cc': cc, 'flag': flag}
                        countries.add(cc)
            shared = len(countries) > 1  # multi-country = likely shared key

            by_email[email] = {
                'online': True,
                'distinct_ips': act.get('distinct_ips', 0),
                'ips': ips,
                'ip_geo': ip_geo,
                'countries': sorted(countries),
                'sharing_flag': shared,
                'active_connections': act.get('active_connections', 0),
                'distinct_destinations': act.get('distinct_destinations', 0),
                'last_seen': act.get('last_seen'),
                'avg_rtt_ms': avg_rtt,
            }
        return web.json_response({
            'count': len(online_emails),
            'emails': online_emails,
            'by_email': by_email,
        })

    async def handle_admin_subscriptions(self, request: web.Request) -> web.Response:
        """Subscriptions / payments summary.

        Buckets:
          active            — is_active=1, end_date in the future
          expiring_in_7d    — same, end_date within next 7 days
          expired           — is_active=0 OR end_date in the past
          no_subscription   — users with an active key but no subscription row

        Each bucket is a small list of dicts so the dashboard can show
        a card-per-user view without a follow-up fetch. Total counts come
        first for an aggregate row.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)

        from datetime import datetime, timedelta

        def _parse_dt(s):
            if not s:
                return None
            for fmt in (
                '%Y-%m-%d %H:%M:%S',
                '%Y-%m-%dT%H:%M:%S',
                '%Y-%m-%d',
            ):
                try:
                    return datetime.strptime(s, fmt)
                except (ValueError, TypeError):
                    continue
            return None

        now = datetime.utcnow()
        soon = now + timedelta(days=7)

        try:
            with self.db._connect() as conn:
                # Prod schema: started_at / expires_at (not start_date / end_date).
                # The 2nd started_at fills the `created_at` slot used downstream;
                # subscriptions has no real created_at column on prod.
                rows = conn.execute(
                    "SELECT s.id, s.chat_id, s.plan_type, s.started_at, s.expires_at, "
                    "s.is_active, s.started_at, u.username, u.status "
                    "FROM subscriptions s LEFT JOIN users u ON u.chat_id = s.chat_id "
                    "ORDER BY s.id DESC LIMIT 1000"
                ).fetchall()
        except Exception as e:
            logger.exception("subscriptions: db read failed")
            return web.json_response({'error': str(e)}, status=500)

        active = []
        expiring_in_7d = []
        expired = []
        seen_chat_ids: set = set()

        for r in rows:
            sid, chat_id, plan, start, end, is_active, created_at, uname, ustatus = r
            seen_chat_ids.add(str(chat_id))
            end_dt = _parse_dt(end)
            entry = {
                'sub_id': sid,
                'chat_id': chat_id,
                'username': uname,
                'user_status': ustatus,
                'plan': plan or 'demo',
                'start': start,
                'end': end,
                'is_active': bool(is_active),
                'created_at': created_at,
            }
            if not is_active or (end_dt and end_dt <= now):
                expired.append(entry)
            elif end_dt and end_dt <= soon:
                expiring_in_7d.append(entry)
            else:
                active.append(entry)

        # Users with an active key but no row in subscriptions yet
        # (e.g. demo handed out before the subscriptions table got
        # populated, or a migration left someone behind).
        try:
            users = await asyncio.to_thread(self.db.get_all_users) or []
        except Exception:
            users = []
        no_subscription = [
            {
                'chat_id': u.chat_id,
                'username': u.username,
                'status': u.status,
                'subscription_expiry': getattr(u, 'subscription_expiry', None),
            }
            for u in users
            if getattr(u, 'uuid', None)
            and u.status in ('demo', 'paid', 'support_topic')
            and str(u.chat_id) not in seen_chat_ids
        ]

        return web.json_response({
            'totals': {
                'active': len(active),
                'expiring_in_7d': len(expiring_in_7d),
                'expired': len(expired),
                'no_subscription': len(no_subscription),
            },
            'active': active[:200],
            'expiring_in_7d': expiring_in_7d[:200],
            'expired': expired[:200],
            'no_subscription': no_subscription[:200],
        })

    async def handle_admin_cascade_order_get(self, request: web.Request) -> web.Response:
        """Return the cascade as a list of {name, enabled} pairs plus
        the catalog of known protocols so the dashboard can render
        labels and checkboxes without hard-coding them. ``order`` is
        the operator's persisted view (kept for backward compat as a
        bare list of enabled names); ``config`` is the new
        per-protocol on/off list the editor talks to."""
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        from bot.handlers.callbacks.user import MyKeyAnswerHandler
        from bot.services.notifications import NotificationService
        cfg = MyKeyAnswerHandler.get_cascade_config(self.db)
        order = [c['name'] for c in cfg if c.get('enabled')]
        labels = NotificationService.PROTOCOL_LABELS_RU
        catalog = []
        for name in MyKeyAnswerHandler.PROTOCOL_METHOD_MAP:
            title, desc = labels.get(name, (name, ''))
            catalog.append({
                'name': name,
                'title': title,
                'desc': desc,
                'tier': MyKeyAnswerHandler.PROTOCOL_TIER.get(name, 'paid'),
            })
        return web.json_response({
            'config': cfg,
            'order': order,
            'catalog': catalog,
            'default': list(MyKeyAnswerHandler.DEFAULT_CASCADE_ORDER),
        })

    async def handle_admin_cascade_order_set(self, request: web.Request) -> web.Response:
        """Save a new cascade. Accepts either:

        * legacy bare-string list: ``{"order": ["stls", "ws", ...]}``
          → every entry stored as enabled
        * config form: ``{"config": [{"name": "stls", "enabled": true},
          ...]}`` → each protocol's on/off persisted

        Unknown protocols are silently dropped (defense against stale
        UI state after a code update); duplicates are deduplicated;
        empty payload resets to default. Existing users' rotation
        pointers are NOT renumbered — when a protocol is disabled they
        just skip past it on the next /mykey tap.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid json'}, status=400)
        from bot.handlers.callbacks.user import MyKeyAnswerHandler
        import json as _json
        known = set(MyKeyAnswerHandler.PROTOCOL_METHOD_MAP)
        seen = set()
        cleaned = []
        # New shape first; legacy ``order`` is a degenerate case of
        # config where every entry is enabled.
        cfg_in = (payload or {}).get('config')
        if cfg_in is None:
            order_in = (payload or {}).get('order')
            if isinstance(order_in, list):
                cfg_in = [{'name': n, 'enabled': True} for n in order_in if isinstance(n, str)]
        if not isinstance(cfg_in, list):
            return web.json_response({'error': 'expected config or order list'}, status=400)
        for item in cfg_in:
            if isinstance(item, str):
                name, enabled = item, True
            elif isinstance(item, dict) and isinstance(item.get('name'), str):
                name = item['name']
                enabled = bool(item.get('enabled', True))
            else:
                continue
            if name not in known or name in seen:
                continue
            seen.add(name)
            cleaned.append({'name': name, 'enabled': enabled})
        if not cleaned:
            self.db.set_setting(MyKeyAnswerHandler.SETTING_KEY, '')
            saved_cfg = [{'name': n, 'enabled': True}
                         for n in MyKeyAnswerHandler.DEFAULT_CASCADE_ORDER]
        else:
            self.db.set_setting(
                MyKeyAnswerHandler.SETTING_KEY,
                _json.dumps(cleaned, ensure_ascii=False),
            )
            saved_cfg = cleaned
        saved_order = [c['name'] for c in saved_cfg if c.get('enabled')]
        return web.json_response({
            'ok': True,
            'config': saved_cfg,
            'order': saved_order,
        })

    async def handle_admin_key_texts_get(self, request: web.Request) -> web.Response:
        """Return the operator overrides for key-message text fields,
        plus the hard-coded defaults so the dashboard can render
        placeholders showing what the user would see if a field is
        left blank."""
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        from bot.services.notifications import NotificationService
        from bot.handlers.callbacks.user import MyKeyAnswerHandler
        import json as _json
        raw = self.db.get_setting(NotificationService.TEXT_OVERRIDES_KEY)
        overrides = {}
        if raw:
            try:
                parsed = _json.loads(raw)
                if isinstance(parsed, dict):
                    overrides = parsed
            except (ValueError, TypeError):
                pass
        # Hard-coded defaults that the bot will fall back to.
        def _defaults(lang_dict, button, footer):
            return {
                'button': button,
                'footer': footer,
                'protocols': {
                    name: {'title': title, 'desc': desc}
                    for name, (title, desc) in lang_dict.items()
                },
            }
        defaults = {
            'ru': _defaults(
                NotificationService.PROTOCOL_LABELS_RU,
                '🚨 НЕ РАБОТАЕТ? Запросить другой ключ 🚨',
                '👆 Если этот ключ не работает — жми кнопку ВЫШЕ.',
            ),
            'en': _defaults(
                NotificationService.PROTOCOL_LABELS_EN,
                '🚨 NOT WORKING? Tap to get another key 🚨',
                '👆 Tap the button ABOVE if this one doesn\'t work.',
            ),
        }
        return web.json_response({
            'overrides': overrides,
            'defaults': defaults,
            'protocols': list(MyKeyAnswerHandler.PROTOCOL_METHOD_MAP),
        })

    async def handle_admin_key_texts_set(self, request: web.Request) -> web.Response:
        """Save key-message text overrides. Body shape mirrors what
        GET returns under ``overrides``. Empty values are dropped so
        the bot falls back to defaults — that's how the operator
        resets a single field without clearing the whole blob."""
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid json'}, status=400)
        from bot.services.notifications import NotificationService
        from bot.handlers.callbacks.user import MyKeyAnswerHandler
        import json as _json
        incoming = (payload or {}).get('overrides') or {}
        if not isinstance(incoming, dict):
            return web.json_response({'error': 'overrides must be object'}, status=400)
        known_protos = set(MyKeyAnswerHandler.PROTOCOL_METHOD_MAP)
        cleaned = {}
        for lang in ('ru', 'en'):
            lang_block = incoming.get(lang)
            if not isinstance(lang_block, dict):
                continue
            out_lang = {}
            for key in ('button', 'footer'):
                val = lang_block.get(key)
                if isinstance(val, str) and val.strip():
                    out_lang[key] = val.strip()
            protos_in = lang_block.get('protocols') or {}
            if isinstance(protos_in, dict):
                out_protos = {}
                for proto_name, fields in protos_in.items():
                    if proto_name not in known_protos or not isinstance(fields, dict):
                        continue
                    out_proto = {}
                    for key in ('title', 'desc'):
                        val = fields.get(key)
                        if isinstance(val, str) and val.strip():
                            out_proto[key] = val.strip()
                    if out_proto:
                        out_protos[proto_name] = out_proto
                if out_protos:
                    out_lang['protocols'] = out_protos
            if out_lang:
                cleaned[lang] = out_lang
        if cleaned:
            self.db.set_setting(
                NotificationService.TEXT_OVERRIDES_KEY,
                _json.dumps(cleaned, ensure_ascii=False),
            )
        else:
            # Empty overrides — clear the setting entirely so defaults
            # kick in cleanly.
            self.db.set_setting(NotificationService.TEXT_OVERRIDES_KEY, '')
        return web.json_response({'ok': True, 'overrides': cleaned})

    async def handle_admin_plans_get(self, request: web.Request) -> web.Response:
        """Return the active Stars pricing ladder for the buy menu.

        Resolution order (DB → env → factory) is fully inside
        ``get_active_plans``; the dashboard just renders what comes
        back. Effective price per month is precomputed so the UI
        doesn't have to do the math.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        from bot.handlers.payments import get_active_plans, PLAN_DEFINITIONS
        active = get_active_plans(self.db)
        plans = []
        for (months, label, factory), (_, _, stars) in zip(PLAN_DEFINITIONS, active):
            plans.append({
                'months': months,
                'label': label,
                'stars': stars,
                'factory_stars': factory,
                'per_month': round(stars / months, 1),
            })
        return web.json_response({'plans': plans})

    # ==================== Admin — Write ====================

    async def handle_admin_plans_set(self, request: web.Request) -> web.Response:
        """Upsert plan prices. Body: ``{"prices": {"1": 100, "3": 270, ...}}``.

        Only months from PLAN_DEFINITIONS are accepted — sending
        ``{"99": 1}`` is a no-op for that key. Prices must be positive
        integers (Stars are whole units). To reset a plan to env/factory
        defaults, send ``null`` for that month.
        """
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid json'}, status=400)
        prices = (payload or {}).get('prices') or {}
        if not isinstance(prices, dict):
            return web.json_response({'error': 'prices must be object'}, status=400)

        from bot.handlers.payments import PLAN_DEFINITIONS, _setting_key
        valid_months = {m for m, _, _ in PLAN_DEFINITIONS}
        changed = []
        for k, v in prices.items():
            try:
                months = int(k)
            except (TypeError, ValueError):
                continue
            if months not in valid_months:
                continue
            key = _setting_key(months)
            if v is None:
                try:
                    with self.db._connect() as conn:
                        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
                        conn.commit()
                    changed.append({'months': months, 'reset': True})
                except Exception as e:
                    logger.warning(f"plans_set: reset {key} failed: {e}")
                continue
            try:
                stars = int(v)
                if stars <= 0:
                    return web.json_response(
                        {'error': f'price for {months}m must be a positive integer'},
                        status=400,
                    )
            except (TypeError, ValueError):
                return web.json_response(
                    {'error': f'price for {months}m is not a number'},
                    status=400,
                )
            if self.db.set_setting(key, str(stars)):
                changed.append({'months': months, 'stars': stars})

        admin_id = str(tg_user.get('id'))
        try:
            self.db.log_admin_action(
                admin_id, 'plans_set', None,
                ', '.join(
                    f"{c['months']}m=reset" if c.get('reset')
                    else f"{c['months']}m={c['stars']}⭐"
                    for c in changed
                ) or 'no-op',
            )
        except Exception:
            pass
        return web.json_response({'ok': True, 'changed': changed})

    # ---- Reminders settings ----

    def _get_notif_service(self):
        """NotificationService is the source of truth for defaults + senders."""
        if self.notification_service is not None:
            return self.notification_service
        try:
            return self.bot.services.get('notifications')  # type: ignore[attr-defined]
        except Exception:
            return None

    async def handle_admin_reminders_get(self, request: web.Request) -> web.Response:
        """Return current reminder settings + eligible-cohort counts."""
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        notif = self._get_notif_service()
        if notif is None:
            return web.json_response({'error': 'notification service unavailable'}, status=503)
        from bot.services.notifications import NotificationService
        defaults = NotificationService.REMINDER_DEFAULTS
        numeric_keys = NotificationService.NUMERIC_REMINDER_KEYS
        settings = {}
        for key, factory in defaults.items():
            settings[key] = {
                'value': notif.get_reminder_setting(key),
                'factory': int(factory) if key in numeric_keys else factory,
                'numeric': key in numeric_keys,
            }
        # Live cohort counts so the operator sees who would be affected.
        cohorts = {'new': 0, 'platform_select': 0}
        min_age = notif.get_reminder_setting('reminder_min_age_hours')
        try:
            with self.db._connect() as conn:
                for status in cohorts:
                    row = conn.execute(
                        "SELECT COUNT(*) FROM users WHERE status = ? "
                        "AND username IS NOT NULL AND username != '' "
                        "AND datetime(created_at) < datetime('now', '-' || ? || ' hours')",
                        (status, min_age),
                    ).fetchone()
                    cohorts[status] = row[0] if row else 0
        except Exception:
            pass
        return web.json_response({'settings': settings, 'cohorts_eligible': cohorts})

    async def handle_admin_reminders_set(self, request: web.Request) -> web.Response:
        """Upsert reminder settings. Body: ``{"<key>": <value>, ...}``.

        Unknown keys are ignored. ``null`` value resets that key to the
        factory default (deletes the row from app_settings). Numeric
        keys must be non-negative integers; strings are stored verbatim.
        """
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid json'}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({'error': 'payload must be object'}, status=400)
        from bot.services.notifications import NotificationService
        valid_keys = set(NotificationService.REMINDER_DEFAULTS.keys())
        numeric_keys = NotificationService.NUMERIC_REMINDER_KEYS
        changed = []
        for key, value in payload.items():
            if key not in valid_keys:
                continue
            if value is None:
                try:
                    with self.db._connect() as conn:
                        conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
                        conn.commit()
                    changed.append({'key': key, 'reset': True})
                except Exception as e:
                    logger.warning(f"reminders_set: reset {key} failed: {e}")
                continue
            if key in numeric_keys:
                try:
                    iv = int(value)
                    if iv < 0:
                        return web.json_response(
                            {'error': f'{key} must be >= 0'}, status=400,
                        )
                except (TypeError, ValueError):
                    return web.json_response(
                        {'error': f'{key} is not a number'}, status=400,
                    )
                if self.db.set_setting(key, str(iv)):
                    changed.append({'key': key, 'value': iv})
            else:
                # Text — store as-is, no length cap (operator's problem
                # if they paste 10kB).
                if self.db.set_setting(key, str(value)):
                    changed.append({'key': key, 'value_len': len(str(value))})
        try:
            self.db.log_admin_action(
                str(tg_user.get('id')), 'reminders_set', None,
                ', '.join(c['key'] for c in changed) or 'no-op',
            )
        except Exception:
            pass
        return web.json_response({'ok': True, 'changed': changed})

    async def handle_admin_dpi_metrics(self, request: web.Request) -> web.Response:
        """Return DPI-quality buckets grouped by (country, ASN) for a time window.

        Query params:
          - ``hours``    — lookback window (default 24, max 720 = 30d).
          - ``country``  — optional filter to one ISO code.
          - ``asn``      — optional filter to one ASN.

        Response shape:
            {
              "window_hours": 24,
              "countries": [
                {
                  "country": "RU",
                  "conn_count": 4231,
                  "short_session_count": 142,
                  "short_ratio": 0.034,
                  "operators": [
                    {
                      "asn": "AS8402", "as_org": "Corbina",
                      "conn_count": 2104, "short_session_count": 120,
                      "short_ratio": 0.057, "avg_session_sec": 18.4,
                      "last_seen": "2026-06-04T18:32:00",
                    },
                    ...
                  ]
                },
                ...
              ],
              "timeseries": [
                {"snapshot_at": "...", "country": "RU", "asn": "AS8402",
                 "conn_count": ..., "short_session_count": ...},
                ...
              ]
            }

        The timeseries array is the raw rows (capped at 5000) — the
        dashboard reuses it for spark-lines in the operator drill-down.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        try:
            hours = max(1, min(720, int(request.query.get('hours', '24'))))
        except (TypeError, ValueError):
            hours = 24
        country = (request.query.get('country') or '').strip().upper() or None
        asn = (request.query.get('asn') or '').strip() or None

        try:
            from datetime import datetime, timedelta
            cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
            params = [cutoff]
            sql_where = "snapshot_at >= ?"
            if country:
                sql_where += " AND country = ?"
                params.append(country)
            if asn:
                sql_where += " AND asn = ?"
                params.append(asn)

            with self.db._connect() as conn:
                # Aggregate per (country, asn) over the window
                rows = conn.execute(
                    f"SELECT country, asn, MAX(as_org) as_org, "
                    f"SUM(conn_count) conn_count, "
                    f"SUM(short_session_count) short_session_count, "
                    f"SUM(handshake_fail_count) hs_fail_count, "
                    f"SUM(rst_count) rst_count, "
                    f"AVG(avg_session_sec) avg_session_sec, "
                    f"MAX(snapshot_at) last_seen "
                    f"FROM dpi_metrics WHERE {sql_where} "
                    f"GROUP BY country, asn",
                    params,
                ).fetchall()
                # Raw time series for spark-lines
                ts_rows = conn.execute(
                    f"SELECT snapshot_at, country, asn, conn_count, "
                    f"short_session_count, avg_session_sec, "
                    f"handshake_fail_count, rst_count "
                    f"FROM dpi_metrics WHERE {sql_where} "
                    f"ORDER BY snapshot_at LIMIT 5000",
                    params,
                ).fetchall()
                # Latest probe IPs + reason buckets per operator (over
                # the same window). One row per (country, asn) — the
                # newest snapshot's JSON blobs.
                detail_rows = conn.execute(
                    f"SELECT country, asn, probe_ips_json, reason_buckets_json "
                    f"FROM dpi_metrics WHERE {sql_where} "
                    f"AND (probe_ips_json IS NOT NULL OR reason_buckets_json IS NOT NULL) "
                    f"AND snapshot_at = ("
                    f"  SELECT MAX(snapshot_at) FROM dpi_metrics d2 "
                    f"  WHERE d2.country = dpi_metrics.country AND d2.asn = dpi_metrics.asn"
                    f")",
                    params,
                ).fetchall()
                # 7-day baseline per (country, asn) for anomaly detection.
                # We aggregate by full days because traffic patterns are
                # diurnal; comparing today to today-1w / today-2w / etc.
                # would be more accurate but adds query cost. Simple sum
                # over 7 days works well enough as a "normal level" anchor.
                from datetime import datetime, timedelta as _td
                bl_cutoff = (datetime.utcnow() - _td(days=7)).isoformat()
                bl_rows = conn.execute(
                    "SELECT country, asn, "
                    "SUM(conn_count), SUM(short_session_count), "
                    "SUM(handshake_fail_count) "
                    "FROM dpi_metrics WHERE snapshot_at >= ? "
                    "GROUP BY country, asn",
                    (bl_cutoff,),
                ).fetchall()
        except Exception as e:
            logger.exception("dpi_metrics: db read failed")
            return web.json_response({'error': str(e)}, status=500)

        import json as _json
        details = {}
        for r in detail_rows:
            key = (r[0], r[1])
            try:
                probe = _json.loads(r[2]) if r[2] else []
            except Exception:
                probe = []
            try:
                reasons = _json.loads(r[3]) if r[3] else {}
            except Exception:
                reasons = {}
            details[key] = {'probe_ips': probe, 'reason_buckets': reasons}

        # Baseline lookup: window-rate per hour, so we can compare to
        # current window-rate per hour without unit mismatches.
        baseline = {}
        bl_hours = max(7 * 24, 1)
        for r in bl_rows:
            key = (r[0], r[1])
            cnt = int(r[2] or 0)
            short = int(r[3] or 0)
            fails = int(r[4] or 0)
            baseline[key] = {
                'conn_rate_h':       cnt   / bl_hours,
                'short_rate_h':      short / bl_hours,
                'hs_fail_rate_h':    fails / bl_hours,
            }

        cur_hours = max(hours, 1)
        # Group operators under their country
        by_country: dict = {}
        for r in rows:
            cc, asn_, org, cnt, short, hs_fail, rst, avg, last = (
                r[0], r[1], r[2], r[3] or 0, r[4] or 0,
                r[5] or 0, r[6] or 0, r[7], r[8],
            )
            cc_key = cc or '??'
            entry = by_country.setdefault(cc_key, {
                'country': cc_key,
                'conn_count': 0,
                'short_session_count': 0,
                'handshake_fail_count': 0,
                'operators': [],
            })
            entry['conn_count'] += int(cnt)
            entry['short_session_count'] += int(short)
            entry['handshake_fail_count'] += int(hs_fail)

            bl = baseline.get((cc, asn_)) or {}
            cur_hs_rate = (hs_fail or 0) / cur_hours
            bl_hs_rate = bl.get('hs_fail_rate_h') or 0
            hs_ratio = (cur_hs_rate / bl_hs_rate) if bl_hs_rate > 0 else None

            det = details.get((cc, asn_)) or {}
            entry['operators'].append({
                'asn': asn_ or '??',
                'as_org': org or '',
                'conn_count': int(cnt),
                'short_session_count': int(short),
                'handshake_fail_count': int(hs_fail),
                'rst_count': int(rst),
                'short_ratio': (int(short) / int(cnt)) if cnt else 0.0,
                'avg_session_sec': round(avg, 1) if avg is not None else None,
                'last_seen': last,
                'hs_baseline_ratio': round(hs_ratio, 2) if hs_ratio is not None else None,
                'probe_ips': det.get('probe_ips') or [],
                'reason_buckets': det.get('reason_buckets') or {},
            })

        countries = []
        for entry in by_country.values():
            entry['short_ratio'] = (
                entry['short_session_count'] / entry['conn_count']
                if entry['conn_count'] else 0.0
            )
            # Operators inside each country sorted worst-first by
            # short_ratio, then by handshake fail count.
            entry['operators'].sort(
                key=lambda x: (-x['short_ratio'], -x['handshake_fail_count'])
            )
            countries.append(entry)
        # Skip the synthetic *GLOBAL* row from the country list — it's
        # surfaced separately for host-wide signals.
        global_row = next(
            (c for c in countries if c['country'] == '*GLOBAL*'), None,
        )
        countries = [c for c in countries if c['country'] != '*GLOBAL*']
        countries.sort(key=lambda x: -x['short_ratio'])

        timeseries = [
            {
                'snapshot_at': r[0], 'country': r[1], 'asn': r[2],
                'conn_count': r[3], 'short_session_count': r[4],
                'avg_session_sec': r[5],
                'handshake_fail_count': r[6], 'rst_count': r[7],
            }
            for r in ts_rows
        ]

        return web.json_response({
            'window_hours': hours,
            'countries': countries,
            'global': global_row,
            'timeseries': timeseries,
        })

    async def handle_admin_protocol_stats(self, request: web.Request) -> web.Response:
        """VLESS vs Hy2 adoption stats for the dashboard widget.

        Pulls two parallel signals:
          - **VLESS**: rolled-up Xray access.log via dpi_metrics
            (per-(country, ASN) ``conn_count`` and unique IPs).
          - **Hy2**:   hy2_auth_log rows where decision='allow'.

        Returns headline cards (unique users + total conns for each
        protocol over the window) and a per-day trend the dashboard
        can render as a tiny bar chart.

        Query params:
          - ``hours``  — lookback (default 24, max 720 = 30d)
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        try:
            hours = max(1, min(720, int(request.query.get('hours', '24'))))
        except (TypeError, ValueError):
            hours = 24
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.utcnow() - _td(hours=hours)).isoformat()

        try:
            with self.db._connect() as conn:
                # VLESS rollup — sum across all per-(country, asn) rows
                # excluding the synthetic *GLOBAL* host-wide row.
                vless_row = conn.execute(
                    "SELECT SUM(conn_count), COUNT(DISTINCT country || '/' || asn) "
                    "FROM dpi_metrics "
                    "WHERE snapshot_at >= ? AND country != '*GLOBAL*'",
                    (cutoff,),
                ).fetchone()
                vless_conns = int(vless_row[0] or 0)
                vless_buckets = int(vless_row[1] or 0)
                vless_country_rows = conn.execute(
                    "SELECT country, SUM(conn_count) c "
                    "FROM dpi_metrics "
                    "WHERE snapshot_at >= ? AND country != '*GLOBAL*' "
                    "GROUP BY country ORDER BY c DESC LIMIT 10",
                    (cutoff,),
                ).fetchall()

                # Hy2 — only count the 'allow' rows for real adoption,
                # but expose deny separately so the operator can see
                # probing spikes vs legitimate use.
                hy2_row = conn.execute(
                    "SELECT "
                    "SUM(CASE WHEN decision='allow' THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN decision='deny' THEN 1 ELSE 0 END), "
                    "COUNT(DISTINCT chat_id) "
                    "FROM hy2_auth_log WHERE ts >= ?",
                    (cutoff,),
                ).fetchone()
                hy2_allow = int(hy2_row[0] or 0)
                hy2_deny = int(hy2_row[1] or 0)
                hy2_unique_users = int(hy2_row[2] or 0)
                hy2_country_rows = conn.execute(
                    "SELECT country, COUNT(*) c FROM hy2_auth_log "
                    "WHERE ts >= ? AND decision='allow' "
                    "GROUP BY country ORDER BY c DESC LIMIT 10",
                    (cutoff,),
                ).fetchall()

                # Per-day trend over the window (max 30 buckets so
                # the chart stays readable on mobile).
                trend_rows_vless = conn.execute(
                    "SELECT substr(snapshot_at, 1, 10) day, SUM(conn_count) c "
                    "FROM dpi_metrics WHERE snapshot_at >= ? "
                    "AND country != '*GLOBAL*' "
                    "GROUP BY day ORDER BY day",
                    (cutoff,),
                ).fetchall()
                trend_rows_hy2 = conn.execute(
                    "SELECT substr(ts, 1, 10) day, COUNT(*) c "
                    "FROM hy2_auth_log WHERE ts >= ? AND decision='allow' "
                    "GROUP BY day ORDER BY day",
                    (cutoff,),
                ).fetchall()
        except Exception as e:
            logger.exception("protocol_stats: db read failed")
            return web.json_response({'error': str(e)}, status=500)

        # Merge trend rows by day so the dashboard can render side-by-side bars.
        days_map = {}
        for day, c in trend_rows_vless:
            days_map.setdefault(day, {'day': day, 'vless': 0, 'hy2': 0})['vless'] = int(c or 0)
        for day, c in trend_rows_hy2:
            days_map.setdefault(day, {'day': day, 'vless': 0, 'hy2': 0})['hy2'] = int(c or 0)
        trend = sorted(days_map.values(), key=lambda x: x['day'])

        return web.json_response({
            'window_hours': hours,
            'vless': {
                'total_conns': vless_conns,
                'distinct_buckets': vless_buckets,
                'top_countries': [
                    {'country': r[0] or '??', 'conns': int(r[1] or 0)}
                    for r in vless_country_rows
                ],
            },
            'hy2': {
                'allow_conns': hy2_allow,
                'deny_conns': hy2_deny,
                'unique_users': hy2_unique_users,
                'top_countries': [
                    {'country': r[0] or '??', 'conns': int(r[1] or 0)}
                    for r in hy2_country_rows
                ],
            },
            'trend': trend,
        })

    async def handle_admin_alerts_get(self, request: web.Request) -> web.Response:
        """Return recent alert_history with optional filter.

        Query params:
          - ``hours``   — lookback (default 168 = 7d, max 720 = 30d)
          - ``state``   — 'all' (default) | 'active' (acked_at IS NULL)
                          | 'acked'
          - ``key``     — substring match on alert key
          - ``limit``   — max rows (default 200, max 1000)
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        try:
            hours = max(1, min(720, int(request.query.get('hours', '168'))))
        except (TypeError, ValueError):
            hours = 168
        state = (request.query.get('state') or 'all').lower()
        key_filter = (request.query.get('key') or '').strip()
        try:
            limit = max(1, min(1000, int(request.query.get('limit', '200'))))
        except (TypeError, ValueError):
            limit = 200

        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.utcnow() - _td(hours=hours)).isoformat()
        sql_where = "fired_at >= ?"
        params = [cutoff]
        if state == 'active':
            sql_where += " AND acked_at IS NULL"
        elif state == 'acked':
            sql_where += " AND acked_at IS NOT NULL"
        if key_filter:
            sql_where += " AND key LIKE ?"
            params.append(f"%{key_filter}%")
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    f"SELECT id, key, severity, title, detail, fired_at, "
                    f"kimi_analysis, kimi_at, acked_at, acked_by "
                    f"FROM alert_history WHERE {sql_where} "
                    f"ORDER BY id DESC LIMIT ?",
                    params + [limit],
                ).fetchall()
                stats = conn.execute(
                    f"SELECT COUNT(*) total, "
                    f"SUM(CASE WHEN acked_at IS NULL THEN 1 ELSE 0 END) active, "
                    f"SUM(CASE WHEN severity='critical' THEN 1 ELSE 0 END) critical "
                    f"FROM alert_history WHERE fired_at >= ?",
                    (cutoff,),
                ).fetchone()
        except Exception as e:
            logger.exception("alerts read failed")
            return web.json_response({'error': str(e)}, status=500)
        return web.json_response({
            'window_hours': hours,
            'totals': {
                'total': int(stats[0] or 0),
                'active': int(stats[1] or 0),
                'critical': int(stats[2] or 0),
            },
            'alerts': [
                {
                    'id': r[0], 'key': r[1], 'severity': r[2],
                    'title': r[3], 'detail': r[4],
                    'fired_at': r[5],
                    'kimi_analysis': r[6], 'kimi_at': r[7],
                    'acked_at': r[8], 'acked_by': r[9],
                }
                for r in rows
            ],
        })

    async def handle_admin_alerts_ack(self, request: web.Request) -> web.Response:
        """Ack a single alert from the dashboard."""
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp
        try:
            alert_id = int(request.match_info['alert_id'])
        except (TypeError, ValueError):
            return web.json_response({'error': 'bad alert_id'}, status=400)
        admin_id = str(tg_user.get('id'))
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT key FROM alert_history WHERE id = ?",
                    (alert_id,),
                ).fetchone()
                if not row:
                    return web.json_response({'error': 'not found'}, status=404)
                key = row[0]
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
        # Use AlertManager.ack so the in-memory tracker is reset too.
        mgr = None
        try:
            if self.bot_instance and getattr(self.bot_instance, 'services', None):
                mgr = self.bot_instance.services.get('alert_manager')
        except Exception:
            mgr = None
        if mgr is not None:
            try:
                mgr.ack(key, by=admin_id)
            except Exception as e:
                logger.warning(f"alerts_ack: mgr.ack failed: {e}")
        else:
            # Fallback: update DB only
            try:
                with self.db._connect() as conn:
                    conn.execute(
                        "UPDATE alert_history SET acked_at = CURRENT_TIMESTAMP, "
                        "acked_by = ? WHERE id = ?",
                        (admin_id, alert_id),
                    )
                    conn.commit()
            except Exception as e:
                return web.json_response({'error': str(e)}, status=500)
        return web.json_response({'ok': True})

    async def handle_admin_dpi_reports_get(self, request: web.Request) -> web.Response:
        """Return persisted daily/weekly/monthly DPI summaries.

        Query params:
          - ``kind``  — 'daily' (default) / 'weekly' / 'monthly'
          - ``days``  — lookback (default 90, max 365)
          - ``limit`` — max rows (default 90, max 365)
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        kind = (request.query.get('kind') or 'daily').lower()
        if kind not in ('daily', 'weekly', 'monthly'):
            return web.json_response({'error': 'bad kind'}, status=400)
        try:
            days = max(1, min(365, int(request.query.get('days', '90'))))
        except (TypeError, ValueError):
            days = 90
        try:
            limit = max(1, min(365, int(request.query.get('limit', '90'))))
        except (TypeError, ValueError):
            limit = 90
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.utcnow() - _td(days=days)).isoformat()
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT id, kind, period_start, period_end, "
                    "snapshot_json, kimi_analysis, created_at "
                    "FROM dpi_reports WHERE kind = ? AND created_at >= ? "
                    "ORDER BY id DESC LIMIT ?",
                    (kind, cutoff, limit),
                ).fetchall()
        except Exception as e:
            logger.exception("dpi_reports read failed")
            return web.json_response({'error': str(e)}, status=500)
        import json as _json
        out = []
        for r in rows:
            try:
                snapshot = _json.loads(r[4]) if r[4] else {}
            except Exception:
                snapshot = {}
            out.append({
                'id': r[0], 'kind': r[1],
                'period_start': r[2], 'period_end': r[3],
                'totals': snapshot.get('totals') or {},
                'snapshot': snapshot,
                'kimi_analysis': r[5],
                'created_at': r[6],
            })
        return web.json_response({'reports': out})

    async def handle_admin_reminders_send(self, request: web.Request) -> web.Response:
        """Manual reminder dispatch.

        Body shapes:
          ``{"scope": "cohort", "cohort": "new"|"platform_select", "force": true}``
          ``{"scope": "user",   "chat_id": "12345",                "force": true}``

        ``force`` (default true for manual sends) skips the cooldown
        gates so the admin can re-ping even if the system already
        sent a reminder this week. The send is still logged in
        notification_log for the audit trail.
        """
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp
        if not tg_user:
            return web.json_response({'error': 'Unauthorized'}, status=401)
        notif = self._get_notif_service()
        if notif is None:
            return web.json_response({'error': 'notification service unavailable'}, status=503)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid json'}, status=400)
        scope = (payload or {}).get('scope')
        force = bool((payload or {}).get('force', True))
        admin_id = str(tg_user.get('id'))
        if scope == 'cohort':
            cohort = (payload or {}).get('cohort')
            if cohort not in ('new', 'platform_select'):
                return web.json_response({'error': 'invalid cohort'}, status=400)
            sent = notif.send_reminders_to_cohort(cohort, force=force)
            try:
                self.db.log_admin_action(
                    admin_id, 'reminders_manual_cohort', None,
                    f"cohort={cohort}, force={force}, sent={sent}",
                )
            except Exception:
                pass
            return web.json_response({'ok': True, 'sent': sent})
        if scope == 'user':
            target = (payload or {}).get('chat_id')
            if not target:
                return web.json_response({'error': 'chat_id required'}, status=400)
            ok = notif.send_reminder_to_chat(str(target), force=force)
            try:
                self.db.log_admin_action(
                    admin_id, 'reminders_manual_user', str(target),
                    f"force={force}, ok={ok}",
                )
            except Exception:
                pass
            if not ok:
                return web.json_response(
                    {'ok': False, 'error': 'user not stuck in NEW/PLATFORM_SELECT or send failed'},
                    status=400,
                )
            return web.json_response({'ok': True})
        return web.json_response({'error': 'invalid scope'}, status=400)

    async def handle_admin_asn_heatmap(self, request: web.Request) -> web.Response:
        """Per-(country, ASN) protocol heatmap for the Signals tab.

        Joins three sources by (country, asn) over the last N hours:
        - ``dpi_metrics`` for conn/fail per inbound_tag
        - ``sub_fetches`` for subscription refresh activity
        - ``user_failure_reports`` for explicit complaint count
        - ``users`` for active subscriber count

        Returns one row per (country, asn). Inbound tags become columns
        on the client so the operator can pivot at will. Active
        subscribers tells "how many users live in this slice", reports
        and refreshes tell "how loud are they", conn/fail tell "how well
        is each protocol doing for them".
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        try:
            hours = max(1, min(720, int(request.query.get('hours', '24'))))
        except (TypeError, ValueError):
            hours = 24
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.utcnow() - _td(hours=hours)).isoformat()

        def _run():
            with self.db._connect() as conn:
                dpi = conn.execute(
                    "SELECT country, asn, MAX(as_org) AS as_org, "
                    "       inbound_tag, "
                    "       SUM(conn_count) AS conns, "
                    "       SUM(handshake_fail_count) AS fails, "
                    "       SUM(short_session_count) AS shorts "
                    "FROM dpi_metrics "
                    "WHERE snapshot_at >= ? AND country != '*GLOBAL*' "
                    "GROUP BY country, asn, inbound_tag",
                    (cutoff,),
                ).fetchall()
                fetches = conn.execute(
                    "SELECT country, asn, COUNT(*) "
                    "FROM sub_fetches WHERE ts >= ? "
                    "GROUP BY country, asn",
                    (cutoff,),
                ).fetchall()
                reports = conn.execute(
                    "SELECT country, asn, COUNT(*) "
                    "FROM user_failure_reports WHERE ts >= ? "
                    "GROUP BY country, asn",
                    (cutoff,),
                ).fetchall()
                # active users per (country, asn) — picks up users we
                # know but who haven't fetched / failed in the window.
                actives = conn.execute(
                    "SELECT last_country, last_asn, COUNT(*) "
                    "FROM users "
                    "WHERE status IN ('demo', 'paid', 'support_topic') "
                    "  AND last_country IS NOT NULL "
                    "GROUP BY last_country, last_asn",
                ).fetchall()
            return dpi, fetches, reports, actives

        dpi, fetches, reports, actives = await asyncio.to_thread(_run)
        buckets: dict = {}
        for cc, asn, as_org, inbound, conns, fails, shorts in dpi:
            key = (cc or '', asn or '')
            b = buckets.setdefault(key, {
                'country': cc, 'asn': asn, 'as_org': as_org or '',
                'protocols': {}, 'sub_fetches': 0,
                'failure_reports': 0, 'active_users': 0,
            })
            if as_org and not b['as_org']:
                b['as_org'] = as_org
            b['protocols'][inbound or ''] = {
                'conn': int(conns or 0),
                'fail': int(fails or 0),
                'short': int(shorts or 0),
            }
        for cc, asn, n in fetches:
            key = (cc or '', asn or '')
            b = buckets.setdefault(key, {
                'country': cc, 'asn': asn, 'as_org': '',
                'protocols': {}, 'sub_fetches': 0,
                'failure_reports': 0, 'active_users': 0,
            })
            b['sub_fetches'] = int(n)
        for cc, asn, n in reports:
            key = (cc or '', asn or '')
            b = buckets.setdefault(key, {
                'country': cc, 'asn': asn, 'as_org': '',
                'protocols': {}, 'sub_fetches': 0,
                'failure_reports': 0, 'active_users': 0,
            })
            b['failure_reports'] = int(n)
        for cc, asn, n in actives:
            key = (cc or '', asn or '')
            b = buckets.setdefault(key, {
                'country': cc, 'asn': asn, 'as_org': '',
                'protocols': {}, 'sub_fetches': 0,
                'failure_reports': 0, 'active_users': 0,
            })
            b['active_users'] = int(n)

        rows = sorted(
            buckets.values(),
            key=lambda r: (-(r['failure_reports'] * 100
                             + r['active_users']
                             + r['sub_fetches']),
                           r['country'] or '', r['asn'] or ''),
        )
        return web.json_response({'rows': rows, 'hours': hours})

    async def handle_admin_failure_reports(self, request: web.Request) -> web.Response:
        """List user_failure_reports for the Signals tab triage list."""
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        try:
            hours = max(1, min(720, int(request.query.get('hours', '168'))))
        except (TypeError, ValueError):
            hours = 168
        state = (request.query.get('state') or 'open').lower()
        try:
            limit = max(1, min(500, int(request.query.get('limit', '100'))))
        except (TypeError, ValueError):
            limit = 100

        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.utcnow() - _td(hours=hours)).isoformat(sep=' ')
        sql_where = "ufr.ts >= ?"
        params = [cutoff]
        if state == 'open':
            sql_where += " AND ufr.acked_at IS NULL"
        elif state == 'acked':
            sql_where += " AND ufr.acked_at IS NOT NULL"

        def _run():
            with self.db._connect() as conn:
                return conn.execute(
                    f"SELECT ufr.id, ufr.ts, ufr.chat_id, ufr.country, "
                    f"       ufr.asn, ufr.last_sub_fetch_ts, "
                    f"       ufr.last_traffic_ts, ufr.acked_at, ufr.ack_note, "
                    f"       u.username, u.status "
                    f"FROM user_failure_reports ufr "
                    f"LEFT JOIN users u ON u.chat_id = ufr.chat_id "
                    f"WHERE {sql_where} "
                    f"ORDER BY ufr.id DESC LIMIT ?",
                    params + [limit],
                ).fetchall()

        rows_raw = await asyncio.to_thread(_run)
        rows = [
            {
                'id': r[0], 'ts': r[1], 'chat_id': r[2],
                'country': r[3], 'asn': r[4],
                'last_sub_fetch_ts': r[5], 'last_traffic_ts': r[6],
                'acked_at': r[7], 'ack_note': r[8],
                'username': r[9], 'status': r[10],
            }
            for r in rows_raw
        ]
        return web.json_response({'rows': rows, 'hours': hours, 'state': state})

    async def handle_admin_failure_report_ack(self, request: web.Request) -> web.Response:
        """Mark a failure report as acknowledged. Optional note in body."""
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp
        try:
            report_id = int(request.match_info.get('report_id', ''))
        except (TypeError, ValueError):
            return web.json_response({'error': 'invalid report_id'}, status=400)
        try:
            body = await request.json()
        except Exception:
            body = {}
        note = (body or {}).get('note') or None

        def _run():
            with self.db._connect() as conn:
                conn.execute(
                    "UPDATE user_failure_reports "
                    "SET acked_at = CURRENT_TIMESTAMP, ack_note = ? "
                    "WHERE id = ?",
                    (note, report_id),
                )
                conn.commit()
        await asyncio.to_thread(_run)
        try:
            self.db.log_admin_action(
                str(tg_user.get('id')), 'failure_report_ack',
                str(report_id), f"note={note or ''}",
            )
        except Exception:
            pass
        return web.json_response({'ok': True})

    async def handle_admin_geo_points(self, request: web.Request) -> web.Response:
        """Return point sources for the Signals map.

        Three layers, all clamped to ``hours`` lookback:

        * ``reports`` — open user_failure_reports with lat/lon. These
          are the red pins the operator triages.
        * ``fetches`` — recent sub_fetches rows, grouped by approx
          (lat, lon) so a hot city collapses to one bubble whose size
          encodes the request count.
        * ``users`` — active subscriber positions (users.last_lat/lon)
          ungrouped, so the operator can see who lives where regardless
          of whether they've refreshed lately.

        Rows without lat/lon are dropped silently — the map has no use
        for them. The dashboard heatmap (no map needed) shows them.
        """
        if not self._validate_admin(request):
            return web.json_response({'error': 'Unauthorized'}, status=401)
        try:
            hours = max(1, min(720, int(request.query.get('hours', '168'))))
        except (TypeError, ValueError):
            hours = 168
        from datetime import datetime as _dt, timedelta as _td
        cutoff = (_dt.utcnow() - _td(hours=hours)).isoformat(sep=' ')

        def _run():
            with self.db._connect() as conn:
                reports = conn.execute(
                    "SELECT ufr.id, ufr.ts, ufr.country, ufr.asn, ufr.city, "
                    "       ufr.lat, ufr.lon, ufr.acked_at, "
                    "       u.username, u.chat_id "
                    "FROM user_failure_reports ufr "
                    "LEFT JOIN users u ON u.chat_id = ufr.chat_id "
                    "WHERE ufr.ts >= ? "
                    "  AND ufr.lat IS NOT NULL AND ufr.lon IS NOT NULL "
                    "ORDER BY ufr.id DESC LIMIT 500",
                    (cutoff,),
                ).fetchall()
                # Coarse grid (~0.5° ≈ 50km) to merge many fetches from
                # one city into a single bubble. Keeps the map readable
                # when one operator has hundreds of refreshes.
                fetches = conn.execute(
                    "SELECT country, asn, city, "
                    "       ROUND(lat * 2) / 2.0 AS glat, "
                    "       ROUND(lon * 2) / 2.0 AS glon, "
                    "       COUNT(*) AS n "
                    "FROM sub_fetches "
                    "WHERE ts >= ? "
                    "  AND lat IS NOT NULL AND lon IS NOT NULL "
                    "GROUP BY country, asn, city, glat, glon "
                    "ORDER BY n DESC LIMIT 500",
                    (cutoff,),
                ).fetchall()
                users = conn.execute(
                    "SELECT chat_id, username, last_country, last_asn, "
                    "       last_city, last_lat, last_lon, status "
                    "FROM users "
                    "WHERE status IN ('demo', 'paid', 'support_topic') "
                    "  AND last_lat IS NOT NULL AND last_lon IS NOT NULL",
                ).fetchall()
            return reports, fetches, users

        reports, fetches, users = await asyncio.to_thread(_run)
        return web.json_response({
            'hours': hours,
            'reports': [
                {
                    'id': r[0], 'ts': r[1],
                    'country': r[2], 'asn': r[3], 'city': r[4],
                    'lat': r[5], 'lon': r[6],
                    'acked': r[7] is not None,
                    'username': r[8], 'chat_id': r[9],
                } for r in reports
            ],
            'fetches': [
                {
                    'country': r[0], 'asn': r[1], 'city': r[2],
                    'lat': r[3], 'lon': r[4], 'count': r[5],
                } for r in fetches
            ],
            'users': [
                {
                    'chat_id': r[0], 'username': r[1],
                    'country': r[2], 'asn': r[3], 'city': r[4],
                    'lat': r[5], 'lon': r[6], 'status': r[7],
                } for r in users
            ],
        })

    async def handle_user_action(self, request: web.Request) -> web.Response:
        """Handle admin action on a user (approve/reject/ban/unban)."""
        # Auth check — initData in query string for POST too
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp
        
        target_chat_id = request.match_info['chat_id']
        admin_id = str(tg_user.get('id'))
        
        # Parse action from body
        try:
            body = await request.json()
            action = body.get('action', '')
        except Exception:
            return web.json_response(
                {'error': 'Invalid JSON body'}, status=400
            )
        
        valid_actions = set(ACTION_STATE_MAP) | SPECIAL_ACTIONS
        if action not in valid_actions:
            return web.json_response(
                {'error': f'Unknown action: {action}. Valid: {sorted(valid_actions)}'},
                status=400
            )

        # Get target user
        user = await asyncio.to_thread(self.db.get_user, target_chat_id)
        if not user:
            return web.json_response(
                {'error': f'User {target_chat_id} not found'}, status=404
            )

        if action in ACTION_STATE_MAP:
            target_state = ACTION_STATE_MAP[action]
            success = await asyncio.to_thread(
                self.state_machine.transition, target_chat_id, target_state
            )
            if not success:
                return web.json_response({
                    'error': f'Invalid transition: {user.status} → {target_state.value}'
                }, status=400)
        elif action == 'reset':
            # `reset` bypasses transition rules (set_state) and clears uuid/email/
            # platform/reject_count. Mirrors the /reset admin command.
            await asyncio.to_thread(
                self.state_machine.set_state, target_chat_id, UserState.NEW
            )
            # Revoke any active VPN key BEFORE clearing the row (helper needs
            # the email to talk to x-ui).
            await asyncio.to_thread(
                revoke_user_key, user,
                self.bot.services.get('xui') if self.bot else None,
                self.db,
            )
            # Full reset (also clears platform + reject_count)
            await asyncio.to_thread(self.db.reset_user_data, target_chat_id)
        elif action == 'grant_100gb':
            # No state transition — just bump quota and propagate to x-ui.
            current = (user.quota_gb or 5.0)
            user.quota_gb = current + 100.0
            await asyncio.to_thread(self.db.save_user, user)
            xui = self.bot.services.get('xui') if self.bot else None
            if xui and user.email:
                try:
                    # In-place update — add_client on an existing email
                    # deletes + re-adds the client, wiping its traffic.
                    await asyncio.to_thread(
                        xui.sync_client_settings_sync,
                        user.email,
                        {'totalGB': int(user.quota_gb * BYTES_PER_GB)},
                    )
                except Exception as e:
                    logger.warning(f"grant_100gb: x-ui update failed for {target_chat_id}: {e}")
        elif action == 'set_limit_ip':
            # Parse "value" from the request body — set limit_ip on the
            # bot user AND push the new limitIp into the x-ui client so
            # Xray actually enforces it on the next reload. The reload
            # itself happens inside sync_client_settings_sync via the
            # xray-reload sidecar.
            try:
                limit = int(body.get('value'))
            except (TypeError, ValueError):
                return web.json_response(
                    {'error': 'set_limit_ip: integer "value" required'}, status=400
                )
            if limit < 0 or limit > 100:
                return web.json_response(
                    {'error': 'set_limit_ip: value out of range 0–100'}, status=400
                )
            user.limit_ip = limit
            await asyncio.to_thread(self.db.save_user, user)
            xui = self.bot.services.get('xui') if self.bot else None
            if xui and user.email:
                try:
                    await asyncio.to_thread(
                        xui.sync_client_settings_sync,
                        user.email, {'limitIp': limit},
                    )
                except Exception as e:
                    logger.warning(f"set_limit_ip: x-ui update failed for {target_chat_id}: {e}")
        elif action == 'set_quota':
            # Replace (not add) quota_gb. Mirrors /quota admin command.
            try:
                gb = float(body.get('value'))
            except (TypeError, ValueError):
                return web.json_response(
                    {'error': 'set_quota: numeric "value" (GB) required'}, status=400
                )
            if gb < 0 or gb > 100000:
                return web.json_response(
                    {'error': 'set_quota: value out of range 0–100000'}, status=400
                )
            user.quota_gb = gb
            await asyncio.to_thread(self.db.save_user, user)
            xui = self.bot.services.get('xui') if self.bot else None
            if xui and user.email:
                try:
                    await asyncio.to_thread(
                        xui.sync_client_settings_sync,
                        user.email, {'totalGB': int(gb * BYTES_PER_GB)},
                    )
                except Exception as e:
                    logger.warning(f"set_quota: x-ui update failed for {target_chat_id}: {e}")
        elif action == 'set_expire':
            # value = YYYY-MM-DD string. Stores end-of-day ISO in both
            # users.subscription_expiry and any active subscriptions row.
            from datetime import datetime, timedelta
            raw = (body.get('value') or '').strip()
            try:
                dt = datetime.strptime(raw, '%Y-%m-%d')
            except ValueError:
                return web.json_response(
                    {'error': 'set_expire: "value" must be YYYY-MM-DD'}, status=400
                )
            end_of_day = dt + timedelta(hours=23, minutes=59)
            new_expiry = end_of_day.isoformat()
            user.subscription_expiry = new_expiry
            await asyncio.to_thread(self.db.save_user, user)
            try:
                with self.db._connect() as conn:
                    conn.execute(
                        "UPDATE subscriptions SET expires_at = ? "
                        "WHERE chat_id = ? AND is_active = 1",
                        (new_expiry, str(target_chat_id)),
                    )
            except Exception as e:
                logger.warning(f"set_expire: subscriptions update failed: {e}")
            # Mirror the date into the panel client — otherwise 3x-ui
            # keeps the old expiryTime and disables the key while the
            # bot still considers the user paid (bit every paid user
            # when their July grant lapsed).
            xui = self.bot.services.get('xui') if self.bot else None
            if xui and user.email:
                try:
                    ok = await asyncio.to_thread(
                        xui.sync_client_settings_sync,
                        user.email,
                        {'expiryTime': int(end_of_day.timestamp() * 1000),
                         'enable': True},
                    )
                    if not ok:
                        logger.warning(
                            f"set_expire: x-ui update returned False "
                            f"for {target_chat_id}"
                        )
                except Exception as e:
                    logger.warning(
                        f"set_expire: x-ui update failed for {target_chat_id}: {e}"
                    )

        # Post-action side effects (notifications, revoke side-effect for ACTION_STATE_MAP entries)
        await self._execute_action_side_effects(
            action, target_chat_id, user, admin_id
        )
        
        # Log admin action
        try:
            await asyncio.to_thread(
                self.db.log_admin_action,
                admin_id, f'webapp_{action}', target_chat_id
            )
        except Exception as e:
            logger.warning(f"Failed to log admin action: {e}")
        
        # Return updated user
        updated_user = await asyncio.to_thread(
            self.db.get_user, target_chat_id
        )

        # Push the change to every connected WS client so the
        # dashboard re-renders the affected row without waiting for
        # the next 5s poll.
        await self._broadcast_ws('user_update', {
            'chat_id': updated_user.chat_id,
            'username': updated_user.username,
            'status': updated_user.status,
            'action': action,
        })

        return web.json_response({
            'success': True,
            'action': action,
            'user': {
                'chat_id': updated_user.chat_id,
                'username': updated_user.username,
                'status': updated_user.status,
            }
        })

    async def _execute_action_side_effects(
        self, action: str, chat_id: str, user, admin_id: str
    ) -> None:
        """Execute side effects after action (notifications, etc.)."""
        if not self.notification_service:
            logger.warning(
                "NotificationService not available, skipping notifications"
            )
            return
        
        try:
            if action == 'approve':
                await asyncio.to_thread(
                    self.notification_service.notify_approved,
                    chat_id, user.lang
                )
            elif action == 'reject':
                # Increment reject counter
                user.reject_count = (user.reject_count or 0) + 1
                await asyncio.to_thread(self.db.save_user, user)

                # Revoke active key + clear uuid/email so the user can't
                # /mykey their old key back. Same fix applied to /reject
                # command and Reject callback — see services/user_lifecycle.py.
                await asyncio.to_thread(
                    revoke_user_key, user,
                    self.bot.services.get('xui') if self.bot else None,
                    self.db,
                )

                reason = (
                    "Отсутствует username" if not user.username
                    else "Заявка отклонена"
                )
                await asyncio.to_thread(
                    self.notification_service.notify_rejected,
                    chat_id, reason, user.lang
                )
            elif action == 'ban':
                # Same as /ban command — kill the x-ui client so the ban
                # is enforceable end-to-end.
                await asyncio.to_thread(
                    revoke_user_key, user,
                    self.bot.services.get('xui') if self.bot else None,
                    self.db,
                )
                await asyncio.to_thread(
                    self.notification_service.notify_banned,
                    chat_id, user.lang
                )
            elif action == 'unban':
                # Unban → NEW: strip any leftover uuid/email so the user
                # must go through approval again instead of re-using a
                # stale config (mirrors the /unban command fix).
                if user.uuid or user.email:
                    user.uuid = None
                    user.email = None
                    await asyncio.to_thread(self.db.save_user, user)
                await asyncio.to_thread(
                    self.notification_service.notify_welcome,
                    chat_id, user.lang
                )
            elif action == 'revoke':
                # Revoke from dashboard — same as the Telegram Revoke callback:
                # kill the x-ui client AND notify the user.
                await asyncio.to_thread(
                    revoke_user_key, user,
                    self.bot.services.get('xui') if self.bot else None,
                    self.db,
                )
                await asyncio.to_thread(
                    self.notification_service.notify_rejected,
                    chat_id, "Access revoked by administrator", user.lang,
                )
            elif action == 'reset':
                # State and DB cleanup were already done in handle_user_action.
                # Here we just let the user know.
                if user.lang == 'ru':
                    msg = "⚠️ Ваше одобрение сброшено. Нажмите /start чтобы подать новую заявку."
                else:
                    msg = "⚠️ Your approval was reset. Press /start to submit a new request."
                self.bot.send_message(chat_id=chat_id, text=msg)
            elif action == 'grant_100gb':
                # No state change, just notify the user about the new quota.
                if user.lang == 'ru':
                    msg = f"🎁 Администратор увеличил ваш лимит до {user.quota_gb:.0f} ГБ."
                else:
                    msg = f"🎁 Admin raised your quota to {user.quota_gb:.0f} GB."
                self.bot.send_message(chat_id=chat_id, text=msg)
            elif action == 'grant_paid':
                # The generic state map already flipped status to paid;
                # the shared grant adds what the button alone used to
                # skip — quota floor, subscription_expiry (+30d or the
                # remaining term, whichever is later) and the panel
                # expiry/enable/quota sync.
                from datetime import datetime as _dt, timedelta as _td
                from bot.services.billing import grant_paid_access
                try:
                    cur = (
                        _dt.fromisoformat(user.subscription_expiry)
                        if user.subscription_expiry else _dt.utcnow()
                    )
                except ValueError:
                    cur = _dt.utcnow()
                paid_until = max(cur, _dt.utcnow()) + _td(days=30)
                xui = self.bot.services.get('xui') if self.bot else None
                grant = await asyncio.to_thread(
                    grant_paid_access,
                    self.db, self.config, xui, chat_id, paid_until,
                )
                if grant.get('panel_ok') is False:
                    logger.warning(
                        f"grant_paid: panel sync failed for {chat_id}"
                    )
                if user.lang == 'ru':
                    msg = (
                        "⭐ Вам выдан полный доступ!\n\n"
                        "Теперь доступны все протоколы (включая Hysteria2) и "
                        "резервный сервер в Германии. Обновите подписку в "
                        "приложении, чтобы изменения подтянулись."
                    )
                else:
                    msg = (
                        "⭐ You've been upgraded to full access!\n\n"
                        "All protocols (including Hysteria2) and the DE "
                        "backup server are now available. Refresh your "
                        "subscription in the app to pick up the changes."
                    )
                self.bot.send_message(chat_id=chat_id, text=msg)
        except Exception as e:
            logger.error(
                f"Failed to send notification for {action} "
                f"on user {chat_id}: {e}"
            )

    # ==================== Admin — Broadcast ====================

    # Statuses that can receive a broadcast message. New/pending/rejected/banned
    # users are excluded — they don't have an active relationship with the bot.
    _BROADCAST_AUDIENCES = {
        'active': ('demo', 'paid', 'support_topic'),
        'demo': ('demo',),
        'all_known': ('demo', 'paid', 'support_topic', 'platform_select', 'pending_demo'),
    }

    async def handle_broadcast(self, request: web.Request) -> web.Response:
        """Preview or send a broadcast message.

        Body: {"text": "...", "confirm": bool, "audience": "active"|"demo"|"all_known"}
        - confirm=false → returns a recipient count and a sample of usernames
        - confirm=true  → sends the message to the audience, returns sent/failed counts
        """
        tg_user, error_resp = self._validate_admin_with_rate_limit(request)
        if error_resp is not None:
            return error_resp

        try:
            body = await request.json()
        except Exception:
            return web.json_response({'error': 'Invalid JSON body'}, status=400)

        text = (body.get('text') or '').strip()
        confirm = bool(body.get('confirm', False))
        audience = body.get('audience', 'active')

        if not text:
            return web.json_response({'error': 'text is required'}, status=400)
        if audience not in self._BROADCAST_AUDIENCES:
            return web.json_response(
                {'error': f'unknown audience: {audience}. '
                          f'Valid: {sorted(self._BROADCAST_AUDIENCES)}'},
                status=400,
            )

        allowed_statuses = self._BROADCAST_AUDIENCES[audience]
        all_users = await asyncio.to_thread(self.db.get_all_users) or []
        recipients = [u for u in all_users if u.status in allowed_statuses]

        if not confirm:
            sample = [
                (u.username or f"user_{u.chat_id}") for u in recipients[:10]
            ]
            return web.json_response({
                'preview': True,
                'audience': audience,
                'recipients_count': len(recipients),
                'sample': sample,
                'text': text,
            })

        # Actually send. Do it in a worker thread because send_message is sync
        # and we don't want to block the aiohttp event loop for N×latency seconds.
        admin_id = str(tg_user.get('id'))
        result = await asyncio.to_thread(self._broadcast_send, recipients, text)

        try:
            await asyncio.to_thread(
                self.db.log_admin_action,
                admin_id, f'webapp_broadcast_{audience}', f"sent={result['sent']}"
            )
        except Exception as e:
            logger.warning(f"Failed to log broadcast: {e}")

        return web.json_response({
            'preview': False,
            'audience': audience,
            **result,
        })

    def _broadcast_send(self, recipients, text: str) -> dict:
        """Synchronous core of broadcast send. Called via asyncio.to_thread."""
        import time
        sent = 0
        failed = 0
        failed_chat_ids = []
        for u in recipients:
            try:
                ok = self.bot.send_message(
                    chat_id=str(u.chat_id),
                    text=text,
                    parse_mode='HTML',
                )
                if ok:
                    sent += 1
                else:
                    failed += 1
                    failed_chat_ids.append(u.chat_id)
            except Exception as e:
                failed += 1
                failed_chat_ids.append(u.chat_id)
                logger.warning(f"Broadcast: failed to send to {u.chat_id}: {e}")
            time.sleep(0.05)  # ~20 msg/s — well under Telegram's 30 msg/s cap
        return {'sent': sent, 'failed': failed, 'failed_chat_ids': failed_chat_ids[:20]}

    # ==================== Server Lifecycle ====================

    def run_in_thread(self, port: int = 8080):
        """Run the web server in a separate thread."""
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            runner = web.AppRunner(self.app)
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, '0.0.0.0', port)
            loop.run_until_complete(site.start())
            
            logger.info(f"Web server started on http://0.0.0.0:{port}")
            loop.run_forever()
            
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return thread
