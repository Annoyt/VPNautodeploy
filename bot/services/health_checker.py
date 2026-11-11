"""Active health checks for outbound protocols — through the REAL tunnels.

Every 15 minutes the scheduler runs ``check_all_outbounds()`` which, per
protocol:
1. Makes a lightweight HTTP HEAD request to each of the ten targets of
   ``probe_rules.PROBE_TARGETS``, all at once — routed through the
   probe-proxy sidecar's per-protocol HTTP proxy (sing-box with the
   actual reality/hy2/ws/stls outbounds, plus hy2t on deployments that
   set HY2T_PORT and the DE reserve where it is configured; see
   scripts/gen_probe_config.py)
2. Records latency + status in ``outbound_health`` table, one row per
   (outbound_tag, target_domain) — the tag IS the protocol short-name
   ('hy2t' for Turbo, 'de' for the reserve), which is what the pager and
   /protocols key on.
3. Downloads ``BULK_BYTES`` through the same proxy and records it in
   ``outbound_bulk`` (IMPROVEMENT_PLAN F, B1): a HEAD cannot show a path
   that freezes after the first kilobytes.

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

from bot.services import probe_rules

logger = logging.getLogger(__name__)


class HealthChecker:
    """Active health checks for VPN outbounds."""

    # The sites each protocol is probed against — owned by probe_rules
    # (IMPROVEMENT_PLAN F, B5), which every reader of the rows also uses:
    # ten targets per run is what the pager's and the monitor's sample
    # counts are built on. Until 2026-11 vk.com and sberbank.ru sat here and
    # never once passed through the foreign exit (418 / an untrusted CA).
    TARGET_DOMAINS = list(probe_rules.PROBE_TARGETS)

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

    # The DE reserve node (IMPROVEMENT_PLAN F, B6): probed wherever paid
    # users are given it (FALLBACK_NODE_HOST + FALLBACK_NODE_PBK, the same
    # gate as FallbackNodeService.is_configured). Until 2026-11 nothing
    # watched it, so it could be dead exactly when it was needed. The tag
    # is probe_rules.RESERVE_TAG; the sidecar inbound comes with a
    # regenerated probe config (gen_probe_config.py) — until then the
    # port answers nothing and _check_protocol writes no rows for it.
    RESERVE_TAG = probe_rules.RESERVE_TAG
    RESERVE_PROBE_PORT = 18086

    # VLESS + XHTTP + Reality (IMPROVEMENT_PLAN F, E3): sing-box has no
    # XHTTP, so this one protocol is probed through a second sidecar on
    # mihomo — the client's own core (scripts/gen_probe_config_mihomo.py,
    # compose service probe-proxy-mihomo). Probed where XHR_SNI is set.
    XHR_TAG = 'xhr'
    XHR_PROBE_PORT = 18087
    # Tags whose sidecar inbound arrives with a regenerated probe config:
    # until it does, every probe is proxy_down and nothing is written.
    OPTIONAL_SIDECAR_TAGS = (RESERVE_TAG, XHR_TAG)

    # Per-request time budget, seconds. Every request of a run starts at
    # once (the pool holds one connection per target), so the budget is
    # the request's own: until 2026-11 five connections served ten requests
    # and the wait for a free one counted inside the 10 s — a path that
    # answers in 6 s read as 2/10, i.e. "degraded" (measured locally).
    TIMEOUT_SEC = 10
    # https in production; a test's local forward proxy speaks plain http.
    TARGET_SCHEME = 'https'

    # Bulk probe (B1): one download per protocol per run, written to
    # outbound_bulk, never to outbound_health (whose ten rows a run the
    # thresholds count). A HEAD answer is a few hundred bytes; the freeze
    # reported on Russian networks since 2025 cuts a TCP connection to
    # foreign hosting / Cloudflare after ~16 KB, which only a body that
    # large can show. Cloudflare's speed-test endpoint serves exactly the
    # bytes asked for; the probe asks for twice the 64 KB the plan sets.
    BULK_URL = 'https://speed.cloudflare.com/__down?bytes={bytes}'
    BULK_BYTES = 128 * 1024
    BULK_TIMEOUT_SEC = 20

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

    @staticmethod
    def reserve_probe_enabled(config) -> bool:
        """The DE reserve is probed where it is handed out: host and public
        key set (FallbackNodeService.is_configured). Strings only — a Mock
        attribute in tests is "not configured"."""
        host = getattr(config, 'FALLBACK_NODE_HOST', '') or ''
        pbk = getattr(config, 'FALLBACK_NODE_PBK', '') or ''
        return isinstance(host, str) and isinstance(pbk, str) and bool(host.strip() and pbk.strip())

    @classmethod
    def probe_ports_for(cls, config) -> Dict[str, int]:
        """{tag: sidecar port} for THIS deployment — the four base
        protocols, plus hy2t when the config enables it, plus the DE
        reserve where it is configured. The single source of truth for
        both the checker and gen_probe_config.py."""
        ports = dict(cls.PROBE_PORTS)
        if cls.hy2t_probe_enabled(config):
            ports[cls.HY2T_TAG] = cls.HY2T_PROBE_PORT
        if cls.reserve_probe_enabled(config):
            ports[cls.RESERVE_TAG] = cls.RESERVE_PROBE_PORT
        if cls.xhr_probe_enabled(config):
            ports[cls.XHR_TAG] = cls.XHR_PROBE_PORT
        return ports

    @staticmethod
    def xhr_probe_enabled(config) -> bool:
        """Same gate as subscription._build_xhr: XHR_SNI set (a string)."""
        sni = getattr(config, 'XHR_SNI', '') or ''
        return isinstance(sni, str) and bool(sni.strip())

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
        if not port:
            return None
        if protocol == self.XHR_TAG:
            host = (getattr(self.config, 'PROBE_PROXY_MIHOMO_HOST', '') or
                    'probe-proxy-mihomo')   # Settings + compose pass it
            host = host.strip() if isinstance(host, str) else 'probe-proxy-mihomo'
            return f'http://{host}:{port}'
        return f'http://{self.proxy_host}:{port}'

    async def check_all_outbounds(self) -> Dict[str, Dict[str, str]]:
        """Run health checks for all configured outbounds against all targets.

        Returns a dict like:
        {
            'reality': {'yandex.ru': {...}, 'google.com': {...}, ...},
            'hy2': {...},
            ...
        }
        """
        results = {}
        # Config-driven, not the class constant: hy2t is probed only where
        # HY2T_PORT enables it, the DE reserve only where it is configured.
        for protocol in self.protocol_tags:
            protocol_results = await self._check_protocol(protocol)
            if protocol_results is not None:
                results[protocol] = protocol_results
        return results

    async def _check_protocol(self, protocol: str) -> Optional[Dict[str, Dict]]:
        """One run for one protocol: the HEAD probes to every target at
        once, then the bulk download. Rows go to the db as they finish.

        A connection pool of one connection per target: no request waits
        for another to finish, so ``TIMEOUT_SEC`` is each request's own
        budget (B1). None (and nothing written) for the DE reserve when the
        sidecar has no inbound for it yet: every probe ``proxy_down`` means
        the probe config predates B6 — rows saying "dark" would page about
        a reserve that may be fine."""
        proxy = self._proxy_url(protocol)
        # A sidecar of its own that does not answer at all (not created yet,
        # stopped, its name unresolvable) is not a dark protocol: aiohttp
        # reports a name it cannot resolve as a plain error, not proxy_down,
        # and the rows would page "xhr dark" and demote it (review of E).
        if (protocol in self.OPTIONAL_SIDECAR_TAGS and proxy
                and not await self._sidecar_answers(proxy)):
            logger.warning(f"health_checker: the probe sidecar for {protocol} "
                           f"({proxy}) does not answer — no rows written")
            return None
        connector = aiohttp.TCPConnector(limit=len(self.TARGET_DOMAINS))
        async with aiohttp.ClientSession(connector=connector) as session:
            statuses = await asyncio.gather(
                *(self._check_one(session, domain, proxy) for domain in self.TARGET_DOMAINS),
                return_exceptions=True,
            )
            protocol_results = {}
            for domain, status_obj in zip(self.TARGET_DOMAINS, statuses):
                if isinstance(status_obj, Exception):
                    status_obj = {
                        'status': 'error',
                        'latency_ms': None,
                        'error': str(status_obj)[:200],
                    }
                protocol_results[domain] = status_obj
            if (protocol in self.OPTIONAL_SIDECAR_TAGS
                    and all(r['status'] == 'proxy_down' for r in protocol_results.values())):
                logger.warning(
                    f"health_checker: no probe sidecar inbound for {protocol} yet — "
                    f"regenerate it (scripts/gen_probe_config.py, "
                    f"gen_probe_config_mihomo.py for xhr); no rows written"
                )
                return None
            for domain, result in protocol_results.items():
                self._write_result(protocol, domain, result)
            bulk = await self._check_bulk(session, proxy)
            self._write_bulk(protocol, bulk)
        return protocol_results

    SIDECAR_CONNECT_S = 3

    async def _sidecar_answers(self, proxy: str) -> bool:
        """Does the sidecar's port take a TCP connection at all?"""
        from urllib.parse import urlsplit
        parts = urlsplit(proxy)
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(parts.hostname, parts.port), self.SIDECAR_CONNECT_S)
        except Exception:
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True

    async def _check_bulk(self, session: aiohttp.ClientSession,
                          proxy: Optional[str]) -> Dict:
        """Download ``BULK_BYTES`` through the protocol and count what came.

        ``ok`` — every byte; ``partial`` — the transfer started and stopped
        short (the freeze signature: the first kilobytes pass, then
        nothing); ``timeout`` / ``error`` / ``proxy_down`` — as for HEAD."""
        url = self.BULK_URL.format(bytes=self.BULK_BYTES)
        start = time.time()
        got = 0
        try:
            async with session.get(
                url, proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=self.BULK_TIMEOUT_SEC),
            ) as resp:
                if resp.status >= 400:
                    return {'status': 'error', 'bytes': 0,
                            'duration_ms': int((time.time() - start) * 1000),
                            'error': f'HTTP {resp.status}'}
                async for chunk in resp.content.iter_chunked(16384):
                    got += len(chunk)
                    if got >= self.BULK_BYTES:
                        break
        except aiohttp.ClientProxyConnectionError as e:
            return {'status': 'proxy_down', 'bytes': got, 'duration_ms': None,
                    'error': str(e)[:100]}
        except asyncio.TimeoutError:
            return {'status': 'partial' if got else 'timeout', 'bytes': got,
                    'duration_ms': int((time.time() - start) * 1000), 'error': 'timeout'}
        except Exception as e:
            return {'status': 'partial' if got else 'error', 'bytes': got,
                    'duration_ms': int((time.time() - start) * 1000),
                    'error': str(e)[:100]}
        duration = int((time.time() - start) * 1000)
        if got >= self.BULK_BYTES:
            return {'status': 'ok', 'bytes': got, 'duration_ms': duration, 'error': None}
        return {'status': 'partial', 'bytes': got, 'duration_ms': duration,
                'error': 'short body'}

    async def _check_one(self, session: aiohttp.ClientSession, domain: str,
                         proxy: Optional[str] = None) -> Dict:
        """Probe a single domain through the protocol's proxy.

        ``proxy_down`` means the SIDECAR was unreachable — a monitoring
        outage, not a verdict about the tunnel or the target.
        """
        url = f'{self.TARGET_SCHEME}://{domain}'
        start = time.time()
        try:
            # HEAD is lighter than GET; most services support it. DNS
            # resolution happens on the far side of the tunnel (CONNECT
            # goes to the proxy), so entry-node DNS can't skew results.
            async with session.head(
                url, allow_redirects=True, proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=self.TIMEOUT_SEC),
            ) as resp:
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

    def _write_bulk(self, protocol: str, result: Dict) -> None:
        """Persist one bulk probe to outbound_bulk. Never raises."""
        try:
            with self.db._connect() as conn:
                conn.execute(
                    "INSERT INTO outbound_bulk "
                    "(ts, outbound_tag, status, bytes, duration_ms, error_msg) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        datetime.utcnow().isoformat(),
                        protocol,
                        result['status'],
                        int(result.get('bytes') or 0),
                        result.get('duration_ms'),
                        result.get('error'),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"health_checker: failed to write the bulk probe for {protocol}: {e}")
