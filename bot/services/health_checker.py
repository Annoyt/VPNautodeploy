"""Active health checks for outbound protocols.

Every 15 minutes the scheduler runs ``check_all_outbounds()`` which:
1. Picks a set of representative domains (RU services, global platforms)
2. Makes a lightweight HTTP HEAD request to each
3. Records latency + status in ``outbound_health`` table

Results power the dashboard's health widget and help us distinguish
"VPN down" from "specific service blocked".
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

    # Protocol tags matching the cascade order in MyKeyAnswerHandler.
    PROTOCOL_TAGS = ['reality', 'hy2', 'ws', 'xhttp', 'stls']

    # Request limits per check.
    TIMEOUT_SEC = 10
    MAX_CONCURRENT = 5

    def __init__(self, db, config=None):
        """Initialize health checker.

        Args:
            db: Database instance for writing results.
            config: Settings object (optional, for future proxy routing).
        """
        self.db = db
        self.config = config

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
            for protocol in self.PROTOCOL_TAGS:
                protocol_results = {}
                tasks = [
                    self._check_one(session, domain)
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

    async def _check_one(self, session: aiohttp.ClientSession, domain: str) -> Dict:
        """Probe a single domain. Returns dict with status, latency_ms, error."""
        url = f'https://{domain}'
        start = time.time()
        try:
            # HEAD is lighter than GET; most services support it.
            async with session.head(url, allow_redirects=True) as resp:
                latency_ms = int((time.time() - start) * 1000)
                if resp.status < 400:
                    return {'status': 'ok', 'latency_ms': latency_ms, 'error': None}
                if resp.status == 403:
                    return {'status': 'blocked', 'latency_ms': latency_ms, 'error': f'HTTP {resp.status}'}
                if resp.status >= 500:
                    return {'status': 'error', 'latency_ms': latency_ms, 'error': f'HTTP {resp.status}'}
                return {'status': 'error', 'latency_ms': latency_ms, 'error': f'HTTP {resp.status}'}
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
