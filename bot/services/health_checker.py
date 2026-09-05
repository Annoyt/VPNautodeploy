"""Active health checks for outbound protocols — through the REAL tunnels.

Every 15 minutes the scheduler runs ``check_all_outbounds()`` which:
1. Picks a set of representative domains (RU services, global platforms)
2. Makes a lightweight HTTP HEAD request to each — routed through the
   probe-proxy sidecar's per-protocol HTTP proxy (sing-box with the
   actual reality/hy2/ws/stls outbounds, plus hy2t on deployments that
   set HY2T_PORT; see scripts/gen_probe_config.py)
3. Records latency + status in ``outbound_health`` table, one row per
   (outbound_tag, target_domain) — the tag IS the protocol short-name
   ('hy2t' for Turbo), which is what the pager and /protocols key on.

History note: until 2026-08-19 this probed DIRECTLY from the entry
container and wrote one identical result under five protocol labels —
vk/yandex/youtube showed "down" since June 21 purely because of
entry-side DNS/RKN, while the tunnels were fine. If the sidecar is
down, results are recorded as ``proxy_down`` — never as fake protocol
data.
"""

import asyncio
import logging
import time
import aiohttp
from typing import List, Dict, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


class HealthChecker:
    """Active health checks for VPN outbounds."""

    # Domains we probe daily. Chosen to represent categories users
    # report issues with: RU domestic, global platforms, messaging.
    TARGET_DOMAINS = [
        # RU domestic (should work even when VPN is down, but we
        # check from exit to detect RKN blocks at the border).
        'vk.com',
        'yandex.ru',
        'sberbank.ru',
        'rutube.ru',
        # Global (confirm VPN itself is routing).
        'youtube.com',
        'google.com',
        'facebook.com',
        'telegram.org',
        'github.com',
        'anthropic.com',
    ]

    # Protocols the probe sidecar ALWAYS speaks. xhttp is deliberately
    # absent — sing-box has no XHTTP transport (see subscription.py),
    # so that path can only be exercised by an xray-core client.
    PROTOCOL_TAGS = ['reality', 'hy2', 'ws', 'stls']

    # HTTP-proxy inbound ports of the probe sidecar, one per protocol.
    # scripts/gen_probe_config.py reads THIS table (via probe_ports_for)
    # so the generator and the checker cannot drift apart.
    PROBE_PORTS = {
        'reality': 18081,
        'hy2': 18082,
        'ws': 18083,
        'stls': 18084,
    }

    # Hysteria "Turbo" (second exit-side instance, paid-only) is optional
    # per deployment: an empty HY2T_PORT keeps it out of subscriptions
    # (subscription._build_hy2t returns None), and it must keep it out of
    # the probe list too. A checker that hit :18085 on a sidecar without
    # that inbound would write proxy_down rows (latency NULL) every run —
    # and check_protocol_probe_down would page protocol_down:hy2t forever
    # for a protocol that does not exist. Hence the tag joins the list
    # ONLY through probe_tags_for(config), never the class constant.
    HY2T_TAG = 'hy2t'
    HY2T_PROBE_PORT = 18085

    # Request limits per check.
    TIMEOUT_SEC = 10
    MAX_CONCURRENT = 5

    def __init__(self, db, config=None):
        """Initialize health checker.

        Args:
            db: Database instance for writing results.
            config: Settings object; PROBE_PROXY_HOST overrides the
                sidecar hostname (default: the compose service name);
                HY2T_PORT (non-empty, numeric) enables the hy2t probe.
        """
        self.db = db
        self.config = config
        self.proxy_host = (
            getattr(config, 'PROBE_PROXY_HOST', '') or 'probe-proxy'
        ).strip()

    # ----- config-driven probe set -----

    @staticmethod
    def hy2t_probe_enabled(config) -> bool:
        """Same gate as subscription._build_hy2t: HY2T_PORT must be a
        non-empty integer. Anything else (unset, '', 'oops', a Mock in
        tests) means "no turbo here" — never a probe against a port
        nobody listens on."""
        raw = getattr(config, 'HY2T_PORT', '') or ''
        try:
            return int(str(raw).strip()) > 0
        except (TypeError, ValueError):
            return False

    @classmethod
    def probe_ports_for(cls, config) -> Dict[str, int]:
        """{tag: sidecar port} for THIS deployment — the four base
        protocols plus hy2t when the config enables it. The single
        source of truth for both the checker and gen_probe_config.py."""
        ports = dict(cls.PROBE_PORTS)
        if cls.hy2t_probe_enabled(config):
            ports[cls.HY2T_TAG] = cls.HY2T_PROBE_PORT
        return ports

    @classmethod
    def probe_tags_for(cls, config) -> List[str]:
        """Ordered outbound_tag list the checker will write for ``config``."""
        return list(cls.probe_ports_for(config))

    @property
    def probe_ports(self) -> Dict[str, int]:
        return self.probe_ports_for(self.config)

    @property
    def protocol_tags(self) -> List[str]:
        """Tags probed by THIS instance (computed from config on every
        access — cheap, and a test that swaps ``config`` sees the change)."""
        return self.probe_tags_for(self.config)

    def _proxy_url(self, protocol: str) -> Optional[str]:
        port = self.probe_ports.get(protocol)
        return f'http://{self.proxy_host}:{port}' if port else None

    async def check_all_outbounds(self) -> Dict[str, Dict[str, str]]:
        """Run health checks for all configured outbounds against all targets.

        Returns a dict like:
        {
            'reality': {'vk.com': 'ok', 'yandex.ru': 'timeout', ...},
            'hy2': {...},
            ...
        }
        """
        results = {}

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.TIMEOUT_SEC),
            connector=aiohttp.TCPConnector(limit=self.MAX_CONCURRENT),
        ) as session:
            # Config-driven, not the class constant: hy2t is probed only
            # where HY2T_PORT enables it (see probe_tags_for).
            for protocol in self.protocol_tags:
                proxy = self._proxy_url(protocol)
                protocol_results = {}
                tasks = [
                    self._check_one(session, domain, proxy)
                    for domain in self.TARGET_DOMAINS
                ]
                domain_statuses = await asyncio.gather(*tasks, return_exceptions=True)
                for domain, status_obj in zip(self.TARGET_DOMAINS, domain_statuses):
                    if isinstance(status_obj, Exception):
                        protocol_results[domain] = {
                            'status': 'error',
                            'latency_ms': None,
                            'error': str(status_obj)[:200],
                        }
                    else:
                        protocol_results[domain] = status_obj
                    # Write to DB immediately so partial failures are visible.
                    self._write_result(protocol, domain, protocol_results[domain])
                results[protocol] = protocol_results

        return results

    async def _check_one(self, session: aiohttp.ClientSession, domain: str,
                         proxy: Optional[str] = None) -> Dict:
        """Probe a single domain through the protocol's proxy.

        ``proxy_down`` means the SIDECAR was unreachable — a monitoring
        outage, not a verdict about the tunnel or the target.
        """
        url = f'https://{domain}'
        start = time.time()
        try:
            # HEAD is lighter than GET; most services support it. DNS
            # resolution happens on the far side of the tunnel (CONNECT
            # goes to the proxy), so entry-node DNS can't skew results.
            async with session.head(url, allow_redirects=True,
                                    proxy=proxy) as resp:
                latency_ms = int((time.time() - start) * 1000)
                if resp.status < 400:
                    return {'status': 'ok', 'latency_ms': latency_ms, 'error': None}
                if resp.status == 403:
                    return {'status': 'blocked', 'latency_ms': latency_ms, 'error': f'HTTP {resp.status}'}
                if resp.status >= 500:
                    return {'status': 'error', 'latency_ms': latency_ms, 'error': f'HTTP {resp.status}'}
                return {'status': 'error', 'latency_ms': latency_ms, 'error': f'HTTP {resp.status}'}
        except aiohttp.ClientProxyConnectionError as e:
            return {'status': 'proxy_down', 'latency_ms': None,
                    'error': str(e)[:100]}
        except asyncio.TimeoutError:
            return {'status': 'timeout', 'latency_ms': None, 'error': 'timeout'}
        except aiohttp.ClientError as e:
            return {'status': 'error', 'latency_ms': None, 'error': str(e)[:100]}
        except Exception as e:
            return {'status': 'error', 'latency_ms': None, 'error': str(e)[:100]}

    def _write_result(self, protocol: str, domain: str, result: Dict) -> None:
        """Persist a single check result to outbound_health."""
        try:
            with self.db._connect() as conn:
                conn.execute(
                    "INSERT INTO outbound_health "
                    "(outbound_tag, target_domain, status, latency_ms, error_msg, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        protocol,
                        domain,
                        result['status'],
                        result.get('latency_ms'),
                        result.get('error'),
                        datetime.utcnow().isoformat(),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"health_checker: failed to write result for {protocol}/{domain}: {e}")

    def get_recent_health(self, hours: int = 24) -> List[Dict]:
        """Read recent health checks for the dashboard."""
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM outbound_health
                       WHERE ts > datetime('now', '-' || ? || ' hours')
                       ORDER BY ts DESC""",
                    (hours,),
                )
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"health_checker: failed to read recent health: {e}")
            return []

    def get_health_summary(self, hours: int = 24) -> Dict:
        """Aggregate health by (outbound_tag, status) for the dashboard widget."""
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    """SELECT outbound_tag, status, COUNT(*) as cnt
                       FROM outbound_health
                       WHERE ts > datetime('now', '-' || ? || ' hours')
                       GROUP BY outbound_tag, status""",
                    (hours,),
                )
                summary = {}
                for row in rows:
                    tag = row['outbound_tag']
                    if tag not in summary:
                        summary[tag] = {}
                    summary[tag][row['status']] = row['cnt']
                return summary
        except Exception as e:
            logger.error(f"health_checker: failed to get summary: {e}")
            return {}
