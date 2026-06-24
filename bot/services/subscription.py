"""Sing-box subscription URL service.

Generates a single URL per user that returns a fresh sing-box JSON
config bundling every enabled cascade protocol as a separate outbound.
The client (Hiddify / sing-box) hits ``/sub/<token>`` on every refresh
and gets the current set — no per-protocol URLs to copy, and if we
rotate an entry IP or a path the user picks it up automatically on the
next refresh without action.

ECH is enabled per-outbound so the client doesn't need the global
ECH toggle in Hiddify Config Options — the subscription closes that
gap automatically for users who pasted the subscription URL but
never touched the toggle.

The token is a deterministic HMAC of the user's UUID with BOT_TOKEN
as the key. Stable, unguessable without BOT_TOKEN, doesn't require a
DB column. Lookup is a linear scan over users which is fine at our
scale; a token-cache can be added later if traffic warrants.
"""

import base64
import hashlib
import hmac
import logging
from typing import Optional, Tuple, List, Any

from bot.config import Settings

logger = logging.getLogger(__name__)


class SubscriptionService:
    """Builds sing-box JSON config and resolves tokens to users."""

    TOKEN_LEN = 32

    def __init__(self, config: Settings):
        self.config = config

    # ---------- Token derivation / lookup ----------

    def derive_token(self, uuid: str) -> str:
        """``HMAC-SHA256(BOT_TOKEN, uuid).hex()[:32]``.

        Deterministic so a user can be DM'd the same URL after a bot
        restart without re-saving anything. The BOT_TOKEN secret is
        already on the host, no extra config plumbing.
        """
        secret = (self.config.BOT_TOKEN or '').encode()
        return hmac.new(
            secret, uuid.encode(), hashlib.sha256,
        ).hexdigest()[: self.TOKEN_LEN]

    def find_user_by_token(self, db, token: str):
        """Linear scan ``users`` to find the row whose UUID hashes to
        ``token``. Returns the ``User`` or ``None``. Constant-time
        comparison so we don't leak the right token via timing.
        """
        if not token or len(token) != self.TOKEN_LEN:
            return None
        try:
            users = db.get_all_users()
        except Exception as e:
            logger.error(f"subscription lookup: db.get_all_users failed: {e}")
            return None
        for u in users:
            if not getattr(u, 'uuid', None):
                continue
            if hmac.compare_digest(self.derive_token(u.uuid), token):
                return u
        return None

    def build_subscription_url(self, user) -> Optional[str]:
        """Public URL the user pastes into Hiddify. ``None`` if the
        bot doesn't know its own public address (WEBAPP_URL empty).
        """
        if not getattr(user, 'uuid', None):
            return None
        base = (self.config.WEBAPP_URL or '').rstrip('/')
        if not base:
            return None
        return f"{base}/sub/{self.derive_token(user.uuid)}"

    # ---------- Sing-box JSON ----------

    def build_singbox_config(
        self,
        user,
        enabled_protocols: Tuple[str, ...],
    ) -> dict:
        """Build a complete sing-box JSON config.

        ``enabled_protocols`` is the tier-filtered cascade order from
        ``MyKeyAnswerHandler.get_cascade_order(db, user)`` — same set
        the user gets via /mykey rotation, but all bundled at once.

        For ``lang='en'`` users, adds a RU-exit outbound so they can
        access RU-geo-blocked content (VK/Yandex/etc) from abroad.
        """
        outbounds: List[dict] = []
        proxy_tags: List[str] = []
        lang = (getattr(user, 'lang', None) or 'ru') if user else 'ru'

        for proto in enabled_protocols:
            ob = self._build_outbound(proto, user)
            if ob is None:
                continue
            if isinstance(ob, list):
                # Chained outbounds (e.g. ShadowSocks → ShadowTLS) need
                # both registered. The first item is the one the
                # selector targets.
                outbounds.extend(ob)
                proxy_tags.append(ob[0]['tag'])
            else:
                outbounds.append(ob)
                proxy_tags.append(ob['tag'])

        if not proxy_tags:
            # Empty cascade — return a no-op config rather than a
            # broken one. Client will just show no servers.
            proxy_tags = []

        # urltest auto-selector at the top so Hiddify's default mode
        # picks the fastest live outbound, falling back along the
        # cascade order on probe failure.
        selector_tags = ['auto'] + proxy_tags if proxy_tags else []
        outbounds.insert(0, {
            'type': 'urltest',
            'tag': 'auto',
            'outbounds': proxy_tags or ['direct'],
            'url': 'https://www.gstatic.com/generate_204',
            'interval': '3m',
            'tolerance': 50,
        })
        outbounds.insert(0, {
            'type': 'selector',
            'tag': 'proxy',
            'outbounds': selector_tags or ['direct'],
            'default': 'auto' if proxy_tags else 'direct',
        })

        # Required system outbounds.
        outbounds.append({'type': 'direct', 'tag': 'direct'})
        outbounds.append({'type': 'block', 'tag': 'block'})
        outbounds.append({'type': 'dns', 'tag': 'dns-out'})

        # RU-exit outbound for lang='en' users — allows accessing RU-geo-blocked
        # content (VK/Yandex/etc) from abroad. Uses the same Reality entry as
        # the primary cascade, but as an exit instead of transit.
        ru_exit_tag = None
        if lang == 'en':
            ru_exit = self._build_ru_exit(user)
            if ru_exit:
                outbounds.append(ru_exit)
                ru_exit_tag = ru_exit['tag']

        return {
            'log': {'level': 'warn'},
            'dns': {
                'servers': [
                    {
                        'tag': 'remote',
                        'address': 'https://1.1.1.1/dns-query',
                        'detour': 'proxy',
                    },
                    {'tag': 'local', 'address': 'local', 'detour': 'direct'},
                ],
                'rules': [
                    {'clash_mode': 'Direct', 'server': 'local'},
                    {'clash_mode': 'Global', 'server': 'remote'},
                    # Domains explicitly routed to proxy below need
                    # remote-resolved DNS too — otherwise the resolver
                    # would leak the lookup to the local ISP before the
                    # rule kicks in.
                    {'rule_set': self._PROXY_RULE_SET_TAGS, 'server': 'remote'},
                    # Russian domestic — resolve locally for speed and
                    # so user's ISP DNS treats them as native traffic.
                    {'rule_set': self._DIRECT_RULE_SET_TAGS, 'server': 'local'},
                ],
                # Default to local DNS to avoid foreign-DNS detection
                # by services like Yandex that treat non-RU DNS as VPN.
                'final': 'local',
            },
            'outbounds': outbounds,
            'route': {
                'rule_set': self._build_rule_sets(),
                'rules': self._build_route_rules(lang, ru_exit_tag),
                'final': 'proxy',
                'auto_detect_interface': True,
            },
        }

    # ---------- Routing rule sets (RU bypass / proxy allow-list) ----------

    # Domains/IPs that must go through the proxy even if they're
    # geo-resolved to a Russian PoP (Google's RU edge, etc.). Order
    # matters — these are evaluated before the geoip-ru bypass.
    _PROXY_RULE_SET_TAGS = (
        'geosite-youtube',
        'geosite-meta',     # Facebook, Instagram, Threads, WhatsApp
        'geosite-twitter',  # X / formerly Twitter
        'geosite-github',
        'geosite-discord',
        'geosite-telegram',
        'geosite-openai',
        'geosite-anthropic',
        'geosite-google',   # Google-wide (Search, Drive, Docs, etc.)
    )
    # Domains/IPs that get the direct route — the user's traffic
    # never leaves the country, so VK / Yandex / banks / госуслуги
    # are fast and look like normal local traffic to the ISP.
    _DIRECT_RULE_SET_TAGS = (
        'geoip-ru',
        'geosite-category-ru',
    )

    _GEOSITE_URL = (
        'https://raw.githubusercontent.com/SagerNet/sing-geosite/'
        'rule-set/{tag}.srs'
    )
    _GEOIP_URL = (
        'https://raw.githubusercontent.com/SagerNet/sing-geoip/'
        'rule-set/{tag}.srs'
    )

    # Anti-DPI: TLS handshake fragmentation.
    # Breaks ClientHello into small chunks so ТСПУ cannot reassemble
    # the SNI field.  Effective against ~90% of Russian DPI boxes.
    #
    # sing-box < 1.12.0 expects ``tls.fragment`` as a boolean.
    # sing-box >= 1.12.0 also accepts a boolean, so this is the safest
    # default for the broadest client compatibility.
    _TLS_FRAGMENT = {
        'fragment': True,
    }

    def _build_rule_sets(self) -> list:
        """Remote rule-set descriptors. Client downloads them on first
        connect and caches; updates daily. ``download_detour=direct``
        so the first fetch doesn't loop through the proxy that's not
        up yet.
        """
        rs = []
        for tag in self._PROXY_RULE_SET_TAGS + self._DIRECT_RULE_SET_TAGS:
            url = (self._GEOIP_URL if tag.startswith('geoip-')
                   else self._GEOSITE_URL).format(tag=tag)
            rs.append({
                'type': 'remote',
                'tag': tag,
                'format': 'binary',
                'url': url,
                'download_detour': 'direct',
                'update_interval': '1d',
            })
        return rs

    def _build_route_rules(self, lang: str = 'ru', ru_exit_tag: str = None) -> list:
        """Route table evaluated top-to-bottom; first match wins.

        Order rationale:
        1. DNS protocol packets → dedicated dns outbound (sing-box requirement).
        2. clash_mode user overrides — explicit Direct/Global from the
           Hiddify mode switch wins over the auto rules below.
        3. For lang='en': RU-geo content (VK/Yandex/etc) → ru-exit so foreign
           users can access RU-geo-blocked services from abroad.
        4. Always-proxy allow-list (YT/Meta/X/GitHub/etc.) — these have
           to tunnel even when their CDN serves from a Russian PoP.
        5. RU geoip + geosite bypass — VK/Yandex/banks/госуслуги direct (for RU users).
        6. Final ``proxy`` — anything not matched defaults to tunneling.

        Args:
            lang: User's language ('en' or 'ru'). EN users get RU-exit routing.
            ru_exit_tag: Tag of the RU-exit outbound (if lang='en' and it exists).
        """
        rules = [
            {'protocol': 'dns', 'outbound': 'dns-out'},
            {'clash_mode': 'Direct', 'outbound': 'direct'},
            {'clash_mode': 'Global', 'outbound': 'proxy'},
            # max.ru — direct (no VPN, no blocking).
            {'domain_suffix': ['max.ru'], 'outbound': 'direct'},
        ]

        # For EN users: RU-geo content → RU-exit (so they can access VK/Yandex from abroad)
        if lang == 'en' and ru_exit_tag:
            rules.append({
                'rule_set': ['geosite-category-ru', 'geoip-ru'],
                'outbound': ru_exit_tag,
            })

        # Always-proxy allow-list (YT/Meta/etc)
        rules.append({
            'rule_set': list(self._PROXY_RULE_SET_TAGS),
            'outbound': 'proxy',
        })

        # For RU users: domestic traffic direct (skip if EN user already has RU-exit rule)
        if lang != 'en' or not ru_exit_tag:
            rules.append({
                'rule_set': list(self._DIRECT_RULE_SET_TAGS),
                'outbound': 'direct',
            })

        return rules

    # ---------- Per-protocol outbound builders ----------

    def _build_outbound(self, proto: str, user) -> Any:
        """Dispatch by protocol short-name; returns a dict or a list
        of dicts (for chained outbounds). ``None`` if the protocol
        isn't fully configured on this deployment."""
        uuid = user.uuid
        email_prefix = (user.email or 'user').split('@')[0]
        builders = {
            'reality': self._build_reality,
            'hy2': self._build_hy2,
            'ws': self._build_ws,
            'xhttp': self._build_xhttp,
            'stls': self._build_stls,
        }
        builder = builders.get(proto)
        if builder is None:
            return None
        try:
            return builder(uuid, email_prefix)
        except Exception as e:
            logger.error(f"subscription: failed to build {proto} outbound: {e}")
            return None

    def _build_reality(self, uuid: str, name_prefix: str) -> Optional[dict]:
        cfg = self.config
        host = cfg.ENTRY_NODE_IP or ''
        pbk = cfg.REALITY_PUBLIC_KEY or ''
        sni = cfg.SNI_VALUE or 'www.microsoft.com'
        sid = getattr(cfg, 'SID_VALUE', None)
        port = int(getattr(cfg, 'ENTRY_NODE_PORT', 443) or 443)
        if not (host and pbk and sni):
            return None
        reality_cfg = {'enabled': True, 'public_key': pbk}
        if sid is not None:
            reality_cfg['short_id'] = sid
        return {
            'type': 'vless',
            'tag': f'{name_prefix}-reality',
            'server': host,
            'server_port': port,
            'uuid': uuid,
            'flow': 'xtls-rprx-vision',
            'packet_encoding': 'xudp',
            'tls': {
                'enabled': True,
                'server_name': sni,
                'utls': {'enabled': True, 'fingerprint': 'chrome'},
                'reality': reality_cfg,
                **self._TLS_FRAGMENT,
            },
        }

    def _build_hy2(self, uuid: str, name_prefix: str) -> Optional[dict]:
        cfg = self.config
        host = getattr(cfg, 'HY2_HOST', '') or ''
        if not host:
            return None
        port = int(getattr(cfg, 'HY2_PORT', 8400) or 8400)
        sni = getattr(cfg, 'HY2_SNI', host) or host
        return {
            'type': 'hysteria2',
            'tag': f'{name_prefix}-hy2',
            'server': host,
            'server_port': port,
            'password': uuid,
            'tls': {
                'enabled': True,
                'server_name': sni,
                'alpn': ['h3'],
            },
        }

    def _build_ws(self, uuid: str, name_prefix: str) -> Optional[dict]:
        """VMess over httpupgrade through Cloudflare. ECH enabled."""
        cfg = self.config
        host = getattr(cfg, 'WS_HOST', '') or ''
        if not host:
            return None
        port = int(getattr(cfg, 'WS_PORT', 2053) or 2053)
        sni = getattr(cfg, 'WS_SNI', host) or host
        path = getattr(cfg, 'WS_PATH', '/api/v1/forecast') or '/'
        return {
            'type': 'vmess',
            'tag': f'{name_prefix}-cdn-ws',
            'server': host,
            'server_port': port,
            'uuid': uuid,
            'security': 'auto',
            'alter_id': 0,
            'transport': {
                'type': 'httpupgrade',
                'host': host,
                'path': path,
            },
            'tls': {
                'enabled': True,
                'server_name': sni,
                'utls': {'enabled': True, 'fingerprint': 'chrome'},
                'ech': {'enabled': True, 'pq_signature_schemes_enabled': False},
                **self._TLS_FRAGMENT,
            },
        }

    def _build_xhttp(self, uuid: str, name_prefix: str) -> Optional[dict]:
        """VMess over xhttp through Cloudflare. ECH enabled."""
        cfg = self.config
        host = getattr(cfg, 'WS2_HOST', '') or ''
        if not host:
            return None
        port = int(getattr(cfg, 'WS2_PORT', 443) or 443)
        sni = getattr(cfg, 'WS2_SNI', host) or host
        path = getattr(cfg, 'WS2_PATH', '/api/v2/observations') or '/'
        return {
            'type': 'vmess',
            'tag': f'{name_prefix}-cdn-xhttp',
            'server': host,
            'server_port': port,
            'uuid': uuid,
            'security': 'auto',
            'alter_id': 0,
            'transport': {
                'type': 'http',
                'host': [host],
                'path': path,
                'method': 'GET',
            },
            'tls': {
                'enabled': True,
                'server_name': sni,
                'utls': {'enabled': True, 'fingerprint': 'chrome'},
                'ech': {'enabled': True, 'pq_signature_schemes_enabled': False},
                'alpn': ['h2', 'http/1.1'],
                **self._TLS_FRAGMENT,
            },
        }

    def _build_stls(self, uuid: str, name_prefix: str) -> Optional[list]:
        """ShadowTLS-v3 fronting Shadowsocks-2022.

        Sing-box models this as two outbounds: a Shadowsocks outbound
        that detours through a ShadowTLS outbound. Returns both as a
        list; the first item is the one the selector tags.
        """
        cfg = self.config
        host = getattr(cfg, 'STLS_HOST', '') or ''
        stls_pw = getattr(cfg, 'STLS_PASSWORD', '') or ''
        salt = getattr(cfg, 'SS_USER_SALT', '') or ''
        server_pw = getattr(cfg, 'SS_SERVER_PASSWORD', '') or ''
        if not (host and stls_pw and salt and server_pw):
            return None
        port = int(getattr(cfg, 'STLS_PORT', 443) or 443)
        sni = getattr(cfg, 'STLS_SNI', 'www.microsoft.com') or 'www.microsoft.com'
        version = int(getattr(cfg, 'STLS_VERSION', 3) or 3)
        method = getattr(cfg, 'SS_METHOD', '2022-blake3-aes-128-gcm')
        user_pw = base64.b64encode(
            hmac.new(salt.encode(), uuid.encode(), hashlib.sha256).digest()[:16]
        ).decode()
        ss_tag = f'{name_prefix}-stls'
        stls_tag = f'{name_prefix}-stls-frontend'
        return [
            {
                'type': 'shadowsocks',
                'tag': ss_tag,
                'method': method,
                'password': f'{server_pw}:{user_pw}',
                'detour': stls_tag,
                'server': host,
                'server_port': port,
            },
            {
                'type': 'shadowtls',
                'tag': stls_tag,
                'server': host,
                'server_port': port,
                'version': version,
                'password': stls_pw,
                'tls': {
                    'enabled': True,
                    'server_name': sni,
                    'utls': {'enabled': True, 'fingerprint': 'chrome'},
                    **self._TLS_FRAGMENT,
                },
            },
        ]

    def _build_ru_exit(self, user) -> Optional[dict]:
        """RU-exit Reality outbound for EN users accessing RU-geo content.

        Uses the same entry as the primary cascade (ENTRY_NODE_IP) but
        as an exit — traffic from foreign users exits to RU sites with
        a Russian IP, bypassing geo-blocks on VK/Yandex/etc.
        """
        cfg = self.config
        host = cfg.ENTRY_NODE_IP or ''
        pbk = cfg.REALITY_PUBLIC_KEY or ''
        sni = cfg.SNI_VALUE or 'www.microsoft.com'
        sid = getattr(cfg, 'SID_VALUE', None)
        port = int(getattr(cfg, 'ENTRY_NODE_PORT', 443) or 443)
        if not (host and pbk and sni):
            return None
        uuid = getattr(user, 'uuid', None)
        if not uuid:
            return None
        reality_cfg = {'enabled': True, 'public_key': pbk}
        if sid is not None:
            reality_cfg['short_id'] = sid
        return {
            'type': 'vless',
            'tag': 'ru-exit',
            'server': host,
            'server_port': port,
            'uuid': uuid,
            'flow': 'xtls-rprx-vision',
            'packet_encoding': 'xudp',
            'tls': {
                'enabled': True,
                'server_name': sni,
                'utls': {'enabled': True, 'fingerprint': 'chrome'},
                'reality': reality_cfg,
                **self._TLS_FRAGMENT,
            },
        }
