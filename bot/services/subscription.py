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
from bot.services.fallback_node import (
    FALLBACK_ALLOWED_STATUSES,
    FallbackNodeService,
)

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

    # ---------- Share-links subscription (Happ / v2rayNG / Streisand) ----------

    def build_links(
        self,
        user,
        enabled_protocols: Tuple[str, ...],
    ) -> str:
        """v2ray-style share-links subscription: newline-joined share URLs
        (vless://, hysteria2://, vmess://, ss://) as PLAIN TEXT.

        Plain text, not the traditional base64 blob: Happ's docs define a
        plain subscription as "конфигурации серверов в открытом виде" and
        its iOS build (4.11.0) silently imports nothing from a base64
        response (ziriki, 2026-07-25). v2rayNG / Streisand / Karing all
        accept plain per-line links too, so plain text is the universal
        choice.

        This is THE format for client apps that show a per-server list.
        A full xray JSON config (``?format=xray``) imports into Happ as a
        SINGLE profile because the app passes it 1:1 to the core; only
        the links list renders as separate switchable servers.

        Reuses VPNService's own generators so the links are identical to
        what /raw and try_alt emit. The DE fallback link for paid users
        is built here (no generator exists for it in VPNService).
        """
        from bot.services.vpn import VPNService
        vpn = VPNService(self.config)
        generators = {
            'reality': vpn.generate_vless_link,
            'hy2': vpn.generate_hy2_link,
            'hy2t': vpn.generate_hy2t_link,
            'ws': vpn.generate_vless_ws_link,
            'stls': vpn.generate_stls_link,
        }
        links: List[str] = []
        for proto in enabled_protocols:
            gen = generators.get(proto)
            if gen is None:
                continue
            try:
                link = gen(user.uuid, user.email)
            except Exception as e:
                logger.error(f'links: {proto} generation failed: {e}')
                continue
            if link:
                links.append(link)

        if user and getattr(user, 'status', None) in FALLBACK_ALLOWED_STATUSES:
            fb_link = self._fallback_share_link(user)
            if fb_link:
                links.append(fb_link)

        return '\n'.join(links)

    def _fallback_share_link(self, user) -> Optional[str]:
        """vless:// share link for the reserve DE node (paid tier)."""
        fb = FallbackNodeService(self.config)
        if not fb.enabled or not getattr(user, 'uuid', None):
            return None
        from urllib.parse import quote
        email = getattr(user, 'email', '') or 'user'
        name = quote(f"{email.split('@')[0]}-de")
        host = fb._cfg('FALLBACK_NODE_HOST')
        port = fb._cfg('FALLBACK_NODE_PORT', '443') or '443'
        sni = fb._cfg('FALLBACK_NODE_SNI', 'www.google.com')
        pbk = fb._cfg('FALLBACK_NODE_PBK')
        sid = fb._cfg('FALLBACK_NODE_SID')
        params = (
            'encryption=none&security=reality&type=tcp&fp=chrome'
            f'&sni={sni}&pbk={pbk}&sid={sid}'
        )
        return f'vless://{user.uuid}@{host}:{port}?{params}#{name}'

    # ---------- Xray JSON (Happ / v2rayNG legacy clients) ----------

    def build_xray_config(
        self,
        user,
        enabled_protocols: Tuple[str, ...],
    ) -> dict:
        """Xray-core client config for clients that can't read sing-box
        JSON — primarily **Happ**, which passes an imported JSON 1:1 to
        its xray core (see happ.su dev-docs, "Принцип прямой передачи").

        Served from the same token-gated /sub URL as the sing-box config
        (``?format=xray`` or a Happ User-Agent), so it may carry the
        Reality outbound with the entry IP — same exposure policy as the
        sing-box variant.

        Xray-core has no Hysteria2 or ShadowTLS client, so those are
        silently skipped; hy2 remains available to Happ users as a raw
        ``hy2://`` link (the app runs it through a separate core).
        """
        outbounds: List[dict] = []
        builders = {
            'reality': self._xray_reality,
            'ws': self._xray_ws,
        }
        for proto in enabled_protocols:
            builder = builders.get(proto)
            if builder is None:
                continue
            try:
                ob = builder(user)
            except Exception as e:
                logger.error(f'xray_config: failed to build {proto}: {e}')
                continue
            if ob is not None:
                outbounds.append(ob)

        # Reserve fallback node (DE) — same paid-tier gating as sing-box.
        if user and getattr(user, 'status', None) in FALLBACK_ALLOWED_STATUSES:
            try:
                fb = self._xray_fallback(user)
            except Exception as e:
                logger.warning(f'xray_config: fallback skipped: {e}')
                fb = None
            if fb is not None:
                outbounds.append(fb)

        if not outbounds:
            logger.warning(f'xray_config: no usable outbounds for {getattr(user, "chat_id", "?")}')

        outbounds.append({'protocol': 'freedom', 'tag': 'direct', 'settings': {}})
        outbounds.append({'protocol': 'blackhole', 'tag': 'block', 'settings': {}})

        return {
            'log': {'loglevel': 'warning'},
            'dns': {'servers': ['https+local://1.1.1.1/dns-query', 'localhost']},
            'inbounds': [
                {
                    'listen': '127.0.0.1',
                    'port': 10808,
                    'protocol': 'socks',
                    'settings': {'auth': 'noauth', 'udp': True},
                    'sniffing': {'enabled': True, 'destOverride': ['http', 'tls']},
                },
                {
                    'listen': '127.0.0.1',
                    'port': 10809,
                    'protocol': 'http',
                    'settings': {},
                },
            ],
            'outbounds': outbounds,
            'routing': {
                'domainStrategy': 'AsIs',
                'rules': [
                    {'type': 'field', 'ip': ['geoip:private'], 'outboundTag': 'direct'},
                ],
            },
        }

    def _xray_reality(self, user) -> Optional[dict]:
        cfg = self.config
        host = getattr(cfg, 'ENTRY_NODE_IP', '') or ''
        pbk = getattr(cfg, 'REALITY_PUBLIC_KEY', '') or ''
        sni = getattr(cfg, 'SNI_VALUE', '') or ''
        if not (host and pbk and sni and user.uuid):
            return None
        try:
            port = int(getattr(cfg, 'ENTRY_NODE_PORT', 443) or 443)
        except (ValueError, TypeError):
            port = 443
        email = getattr(user, 'email', '') or 'user'
        return {
            'protocol': 'vless',
            'tag': f'{email.split("@")[0]}-reality',
            'settings': {
                'vnext': [{
                    'address': host,
                    'port': port,
                    'users': [{
                        'id': user.uuid,
                        'encryption': 'none',
                        'flow': 'xtls-rprx-vision',
                    }],
                }],
            },
            'streamSettings': {
                'network': 'tcp',
                'security': 'reality',
                'realitySettings': {
                    'serverName': sni,
                    'publicKey': pbk,
                    'shortId': getattr(cfg, 'SID_VALUE', '') or '',
                    'fingerprint': 'chrome',
                },
            },
        }

    def _xray_ws(self, user) -> Optional[dict]:
        cfg = self.config
        host = getattr(cfg, 'WS_HOST', '') or ''
        if not (host and user.uuid):
            return None
        try:
            port = int(getattr(cfg, 'WS_PORT', 2053) or 2053)
        except (ValueError, TypeError):
            port = 2053
        sni = getattr(cfg, 'WS_SNI', '') or host
        path = getattr(cfg, 'WS_PATH', '/') or '/'
        email = getattr(user, 'email', '') or 'user'
        return {
            'protocol': 'vmess',
            'tag': f'{email.split("@")[0]}-cdn-ws',
            'settings': {
                'vnext': [{
                    'address': host,
                    'port': port,
                    'users': [{'id': user.uuid, 'alterId': 0, 'security': 'auto'}],
                }],
            },
            'streamSettings': {
                'network': 'httpupgrade',
                'security': 'tls',
                'httpupgradeSettings': {'path': path, 'host': host},
                'tlsSettings': {'serverName': sni, 'fingerprint': 'chrome'},
            },
        }

    def _xray_xhttp(self, user) -> Optional[dict]:
        cfg = self.config
        host = getattr(cfg, 'WS2_HOST', '') or ''
        if not (host and user.uuid):
            return None
        try:
            port = int(getattr(cfg, 'WS2_PORT', 443) or 443)
        except (ValueError, TypeError):
            port = 443
        sni = getattr(cfg, 'WS2_SNI', '') or host
        path = getattr(cfg, 'WS2_PATH', '/') or '/'
        email = getattr(user, 'email', '') or 'user'
        return {
            'protocol': 'vmess',
            'tag': f'{email.split("@")[0]}-cdn-xhttp',
            'settings': {
                'vnext': [{
                    'address': host,
                    'port': port,
                    'users': [{'id': user.uuid, 'alterId': 0, 'security': 'auto'}],
                }],
            },
            'streamSettings': {
                'network': 'xhttp',
                'security': 'tls',
                'xhttpSettings': {'path': path, 'host': host, 'mode': 'auto'},
                'tlsSettings': {
                    'serverName': sni,
                    'fingerprint': 'chrome',
                    'alpn': ['h2', 'http/1.1'],
                },
            },
        }

    def _xray_fallback(self, user) -> Optional[dict]:
        """Reserve DE node outbound in xray shape (no vision flow —
        the reserve inbound doesn't use it)."""
        if not getattr(user, 'uuid', None):
            return None
        fb = FallbackNodeService(self.config)
        if not fb.enabled:
            return None
        host = fb._cfg('FALLBACK_NODE_HOST')
        email = getattr(user, 'email', '') or 'user'
        return {
            'protocol': 'vless',
            'tag': f'{email.split("@")[0]}-de',
            'settings': {
                'vnext': [{
                    'address': host,
                    'port': int(fb._cfg('FALLBACK_NODE_PORT', '443') or 443),
                    'users': [{'id': user.uuid, 'encryption': 'none'}],
                }],
            },
            'streamSettings': {
                'network': 'tcp',
                'security': 'reality',
                'realitySettings': {
                    'serverName': fb._cfg('FALLBACK_NODE_SNI', 'www.google.com'),
                    'publicKey': fb._cfg('FALLBACK_NODE_PBK'),
                    'shortId': fb._cfg('FALLBACK_NODE_SID'),
                    'fingerprint': 'chrome',
                },
            },
        }

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
        # Reality (xudp) and Hy2 (native UDP) carry Telegram call media
        # well; the CDN transports (ws/xhttp) can't do real UDP. Collect
        # the UDP-native tags for a dedicated 'calls' selector so voice/
        # video isn't stranded on a CDN outbound that the auto-selector
        # happened to pick by its TCP latency probe.
        udp_call_tags: List[str] = []
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
                tag = ob[0]['tag']
            else:
                outbounds.append(ob)
                tag = ob['tag']
            proxy_tags.append(tag)
            if proto in ('reality', 'hy2', 'hy2t'):
                udp_call_tags.append(tag)

        # Reserve fallback node (DE) — paid-tier only. The client was
        # provisioned lazily by the /sub handler before this build; here
        # we just append the outbound so it joins the auto-selector. A
        # broken reserve panel must never kill the whole subscription.
        if user and getattr(user, 'status', None) in FALLBACK_ALLOWED_STATUSES:
            try:
                fb = FallbackNodeService(self.config).build_outbound(user)
            except Exception as e:
                logger.warning(f'subscription: fallback outbound skipped: {e}')
                fb = None
            if fb is not None:
                outbounds.append(fb)
                proxy_tags.append(fb['tag'])
                # Reality carries UDP — usable for calls too.
                udp_call_tags.append(fb['tag'])

        if not proxy_tags:
            # Empty cascade — return a no-op config rather than a
            # broken one. Client will just show no servers.
            proxy_tags = []

        # A bare Reality outbound can pass the 'calls' urltest probe via its
        # TLS masquerade even when the user isn't provisioned as a client on
        # the Reality inbound (false-healthy) — the selector then picks it
        # and silently swallows call media. Hy2 only probes healthy when the
        # tunnel really carries data, so when Hy2 is present make it the sole
        # call transport; only fall back to Reality if there's no Hy2.
        hy2_call_tags = [t for t in udp_call_tags if t.endswith(('-hy2', '-hy2t'))]
        if hy2_call_tags:
            udp_call_tags = hy2_call_tags

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

        # UDP-native selector for Telegram call media (see udp_call_tags).
        # Ranks only Reality/Hy2, so a call never lands on a CDN transport
        # that can't carry UDP. The route rules send Telegram + RU-domestic
        # UDP here; if no UDP-native outbound exists on this deployment the
        # rules fall back to 'proxy'.
        calls_tag: Optional[str] = None
        if udp_call_tags:
            calls_tag = 'calls'
            outbounds.insert(2, {
                'type': 'urltest',
                'tag': 'calls',
                'outbounds': udp_call_tags,
                'url': 'https://www.gstatic.com/generate_204',
                'interval': '3m',
                'tolerance': 50,
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
                'rules': self._build_route_rules(lang, ru_exit_tag, calls_tag),
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

    # Telegram's own IP ranges (signaling + the voice/video "reflector"
    # servers that carry calls), from
    # https://core.telegram.org/resources/cidr.txt. Matched literally as
    # ``ip_cidr`` instead of a downloaded ``geoip-telegram`` rule set so
    # call routing has ZERO dependency on the .srs mirror (and keeps
    # working under a whitelist/lockdown where the mirror may be
    # unreachable). This is what actually forces call media through the
    # tunnel: the media is raw UDP to these IPs with no SNI, so the
    # domain-based ``geosite-telegram`` rule can never match it.
    _TELEGRAM_IP_CIDRS = (
        '91.105.192.0/23',
        '91.108.4.0/22',
        '91.108.8.0/22',
        '91.108.12.0/22',
        '91.108.16.0/22',
        '91.108.20.0/22',
        '91.108.56.0/22',
        '95.161.64.0/20',
        '149.154.160.0/20',
        '185.76.151.0/24',
        '2001:67c:4e8::/48',
        '2001:b28:f23c::/48',
        '2001:b28:f23d::/48',
        '2001:b28:f23f::/48',
        '2a0a:f280::/32',
    )

    # Self-hosted rule sets are served from the dashboard domain so
    # clients inside Russia (where raw.githubusercontent.com is often
    # blocked by РКН) can still download them. Falls back to SagerNet
    # GitHub mirrors if no dashboard URL is configured.
    _GEOSITE_URL = (
        'https://raw.githubusercontent.com/SagerNet/sing-geosite/'
        'rule-set/{tag}.srs'
    )
    _GEOIP_URL = (
        'https://raw.githubusercontent.com/SagerNet/sing-geoip/'
        'rule-set/{tag}.srs'
    )

    def _rule_set_url(self, tag: str) -> str:
        """Return rule-set URL, preferring self-hosted mirror."""
        base = getattr(self.config, 'WEBAPP_URL', '') or ''
        if base:
            base = base.rstrip('/')
            return f"{base}/rule-sets/{tag}.srs"
        if tag.startswith('geoip-'):
            return self._GEOIP_URL.format(tag=tag)
        return self._GEOSITE_URL.format(tag=tag)

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
            rs.append({
                'type': 'remote',
                'tag': tag,
                'format': 'binary',
                'url': self._rule_set_url(tag),
                'download_detour': 'direct',
                'update_interval': '1d',
            })
        return rs

    def _build_route_rules(
        self, lang: str = 'ru', ru_exit_tag: str = None, calls_tag: str = None
    ) -> list:
        """Route table evaluated top-to-bottom; first match wins.

        Order rationale:
        1. DNS protocol packets → dedicated dns outbound (sing-box requirement).
        2. clash_mode user overrides — explicit Direct/Global from the
           Hiddify mode switch wins over the auto rules below.
        3. For lang='en': RU-geo content (VK/Yandex/etc) → ru-exit so foreign
           users can access RU-geo-blocked services from abroad.
        4. Telegram by IP — force calls (UDP media) + signaling through the
           tunnel, ABOVE the RU-direct bypass, so voice/video isn't throttled.
        5. Always-proxy allow-list (YT/Meta/X/GitHub/etc.) — these have
           to tunnel even when their CDN serves from a Russian PoP.
        6. RU geoip + geosite bypass — VK/Yandex/banks/госуслуги direct, but
           TCP only.
        7. Blanket UDP → UDP-native path — carries Telegram voice/video to
           any peer (RU or abroad); TCP falls through to ``final``.
        8. Final ``proxy`` — anything not matched defaults to tunneling.

        Args:
            lang: User's language ('en' or 'ru'). EN users get RU-exit routing.
            ru_exit_tag: Tag of the RU-exit outbound (if lang='en' and it exists).
            calls_tag: Tag of the UDP-native 'calls' selector, or None if this
                deployment has no UDP-native outbound (then UDP falls to proxy).
        """
        # Telegram call media (and all UDP) prefers a UDP-native outbound
        # (Reality/Hy2); fall back to the general proxy selector when the
        # deployment has none.
        udp_out = calls_tag or 'proxy'

        # Where RU-domestic QUIC should land: the home connection for RU
        # users, the RU exit for foreigners — either way the same
        # country as the TCP path for those sites.
        ru_quic_out = ru_exit_tag if (lang == 'en' and ru_exit_tag) else 'direct'

        tg_cidr = list(self._TELEGRAM_IP_CIDRS)
        rules = [
            {'protocol': 'dns', 'outbound': 'dns-out'},
            # Explicit Direct mode wins (user chose "no VPN").
            {'clash_mode': 'Direct', 'outbound': 'direct'},
            # RU-domestic HTTP/3 (QUIC = UDP:443) must NOT ride the
            # blanket UDP rule below: VK sees the session's TCP from a
            # home IP but its QUIC probes from the exit IP and shows
            # the "VPN detected" banner. Port-scoped to 443 so Telegram
            # P2P call media (random high UDP ports, even to RU
            # residential peers) still tunnels past RKN's call
            # throttle.
            {'rule_set': list(self._DIRECT_RULE_SET_TAGS), 'network': 'udp',
             'port': [443], 'outbound': ru_quic_out},
            # ALL other UDP → the UDP-native transport (Hy2/Reality). This
            # sits ABOVE clash_mode Global on purpose: when the user manually
            # picks a server in the client (Hiddify sets Global, routing
            # everything through that one outbound), call media would
            # otherwise go down whatever they picked — and a TCP-only
            # transport (ShadowTLS/CDN) simply can't carry UDP, so the call
            # dies. Confirmed in the field: calls work ONLY when Hy2 is the
            # active protocol; any other selection breaks them. Forcing UDP
            # to `udp_out` here makes voice/video ride Hy2 no matter what
            # the user selected. DNS is already peeled off above.
            {'network': 'udp', 'outbound': udp_out},
            # Telegram signaling (TCP, by IP — MTProto hits DC IPs with no
            # SNI so the domain rule can't catch it) must tunnel too.
            {'ip_cidr': tg_cidr, 'outbound': 'proxy'},
            # Explicit Global mode → remaining (TCP) traffic via selected proxy.
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

        # Always-proxy allow-list (YT/Meta/etc) — TCP; UDP already routed above.
        rules.append({
            'rule_set': list(self._PROXY_RULE_SET_TAGS),
            'outbound': 'proxy',
        })

        # For RU users: domestic TCP traffic direct (skip if EN user already
        # has RU-exit rule). UDP is intentionally NOT here — it was already
        # sent to the UDP transport at the top so P2P call media to a Russian
        # peer tunnels instead of going direct into RKN's call throttle.
        if lang != 'en' or not ru_exit_tag:
            rules.append({
                'rule_set': list(self._DIRECT_RULE_SET_TAGS),
                'network': 'tcp',
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
        # NOTE: 'xhttp' is deliberately absent. The :2054 inbound runs
        # Xray's XHTTP transport, which sing-box does not implement at
        # all — its similarly-named "http" transport is plain HTTP/2 and
        # cannot talk to an XHTTP server. Emitting it here produced an
        # outbound that could never connect, and it also polluted the
        # 'auto' urltest ranking. Confirmed in prod: the :2054 inbound
        # carried zero sessions while ShadowTLS/Reality carried all the
        # traffic. Xray-core clients still get xhttp through
        # build_xray_config and the share-links list.
        builders = {
            'reality': self._build_reality,
            'hy2': self._build_hy2,
            'hy2t': self._build_hy2t,
            'ws': self._build_ws,
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
        port = int(getattr(self.config, 'HY2_PORT', 8400) or 8400)
        hop = getattr(self.config, 'HY2_HOP_PORTS', '') or ''
        return self._hy2_outbound(uuid, name_prefix, '-hy2', port, hop)

    def _build_hy2t(self, uuid: str, name_prefix: str) -> Optional[dict]:
        """Turbo Hy2: second exit-side hysteria instance with Brutal CC
        honoured. Unlike the share-link (where bandwidth params are
        non-standard), the sing-box outbound carries real up_mbps /
        down_mbps hints, so config-format clients get Brutal for real.
        """
        cfg = self.config
        port_raw = (getattr(cfg, 'HY2T_PORT', '') or '').strip()
        if not port_raw:
            return None
        try:
            port = int(port_raw)
        except ValueError:
            return None
        hop = getattr(cfg, 'HY2T_HOP_PORTS', '') or ''
        return self._hy2_outbound(
            uuid, name_prefix, '-hy2t', port, hop,
            up_mbps=int(getattr(cfg, 'HY2T_UP_MBPS', 20) or 20),
            down_mbps=int(getattr(cfg, 'HY2T_DOWN_MBPS', 60) or 60),
        )

    def _hy2_outbound(
        self,
        uuid: str,
        name_prefix: str,
        tag_suffix: str,
        port: int,
        hop: str,
        up_mbps: Optional[int] = None,
        down_mbps: Optional[int] = None,
    ) -> Optional[dict]:
        cfg = self.config
        host = getattr(cfg, 'HY2_HOST', '') or ''
        if not host:
            return None
        sni = getattr(cfg, 'HY2_SNI', host) or host
        ob = {
            'type': 'hysteria2',
            'tag': f'{name_prefix}{tag_suffix}',
            'server': host,
            'server_port': port,
            'password': uuid,
            'tls': {
                'enabled': True,
                'server_name': sni,
                'alpn': ['h3'],
            },
        }
        # Brutal bandwidth hints (turbo only) — the server honours them
        # because the turbo instance runs ignoreClientBandwidth: false.
        if up_mbps:
            ob['up_mbps'] = up_mbps
        if down_mbps:
            ob['down_mbps'] = down_mbps
        # Salamander obfuscation — makes the QUIC packets look like random
        # noise so РКН's UDP/QUIC throttle can't fingerprint them. Without
        # it plain Hy2 handshakes through but the data stream gets choked
        # to a timeout (observed: client connects, tx=0, idle-disconnect),
        # which kills Telegram calls (the only UDP transport). Must match
        # the server's obfs.salamander.password.
        obfs_pw = getattr(cfg, 'HY2_OBFS_PASSWORD', '') or ''
        if obfs_pw:
            ob['obfs'] = {'type': 'salamander', 'password': obfs_pw}
        # Port hopping — the client sprays the QUIC flow across a whole
        # UDP port range instead of a single port. РКН throttles Hy2 by
        # port/flow on the user's last mile (confirmed: handshake passes,
        # sustained stream chokes to a timeout even WITH obfs), so a
        # single fixed port gets rate-limited to death; hopping spreads
        # the flow so no one port trips the throttle. The entry node DNATs
        # each range to the matching exit port (iptables 'hy2-hop' /
        # 'hy2t-hop').
        if hop:
            # The env value follows the hysteria2-URI ``mport`` convention
            # (comma-separated, e.g. "443,20000:40000") because
            # ``generate_hy2_link`` emits it verbatim. sing-box instead
            # wants a LIST of "start:end" ranges and rejects both a
            # comma-joined string and a bare port with "bad port range" —
            # an error that aborts the ENTIRE config, not just this
            # outbound. Split on commas and widen single ports.
            ports = []
            for part in hop.split(','):
                part = part.strip()
                if not part:
                    continue
                ports.append(part if ':' in part else f'{part}:{part}')
            if ports:
                ob['server_ports'] = ports
                ob['hop_interval'] = '30s'
        return ob

    def _build_ws(self, uuid: str, name_prefix: str) -> Optional[dict]:
        """VMess over httpupgrade through Cloudflare.

        ECH is deliberately OFF. Enabling it without an inline config
        makes sing-box fetch the key from a DNS HTTPS (type 65) record
        first; RU/KZ mobile resolvers routinely drop that record type,
        and the handshake then fails outright instead of degrading. The
        SNI here is the CDN hostname anyway, so hiding it buys little.
        """
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
