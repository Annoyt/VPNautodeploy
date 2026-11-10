#!/usr/bin/env python3
"""Generate the mihomo config for the second probe sidecar (IMPROVEMENT_PLAN F, E3).

sing-box (the probe-proxy sidecar, scripts/gen_probe_config.py) has no XHTTP,
so VLESS + XHTTP + Reality is probed through mihomo — the core NekoVPN and
FlClash run. One HTTP-proxy listener per protocol, each bound to its proxy
(``listeners[].proxy``), the proxy built by the same SubscriptionService code
as the users' Clash profile, under the dedicated ``probe_internal`` client:

    docker exec vpn-bot python3 scripts/gen_probe_config_mihomo.py \
        > /opt/vpn-bot/probe-proxy-mihomo/config.yaml
    docker compose up -d probe-proxy-mihomo     # first time
    docker compose restart probe-proxy-mihomo  # after a regeneration

Ports come from HealthChecker.probe_ports_for (XHR_TAG → 18087), so the
checker and this file cannot drift apart. Emitted as JSON, a YAML subset
mihomo reads.
"""

import json
import sys

sys.path.insert(0, '/app')

from bot.config import Settings                      # noqa: E402
from bot.core.database import Database               # noqa: E402
from bot.services.health_checker import HealthChecker  # noqa: E402
from bot.services.subscription import SubscriptionService  # noqa: E402

PROBE_CHAT_ID = 'probe_internal'
MIHOMO_TAGS = (HealthChecker.XHR_TAG,)


def build_config(config, user, svc=None, *, log=None) -> dict:
    """The mihomo config dict (unit-tested): listeners only for the tags the
    sing-box sidecar cannot speak."""
    svc = svc or SubscriptionService(config)
    log = log or (lambda msg: None)
    proxies, listeners = [], []
    for proto, port in HealthChecker.probe_ports_for(config).items():
        if proto not in MIHOMO_TAGS:
            continue
        ob = svc._build_outbound(proto, user)
        proxy = svc._to_clash_proxy(ob, {ob['tag']: ob}) if ob else None
        if proxy is None:
            log(f"skip {proto}: not configured")
            continue
        proxies.append(proxy)
        listeners.append({'name': f'probe-in-{proto}', 'type': 'http',
                          'listen': '0.0.0.0', 'port': port, 'proxy': proxy['name']})
    return {
        'mode': 'rule',
        'log-level': 'warning',
        'ipv6': False,
        'proxies': proxies,
        'listeners': listeners,
        'rules': ['MATCH,DIRECT'],
    }


def main() -> int:
    config = Settings()
    db = Database(config.DB_PATH)
    user = db.get_user(PROBE_CHAT_ID)
    if not user or not user.uuid:
        print(f"probe user {PROBE_CHAT_ID} not found in bot.db", file=sys.stderr)
        return 1
    print(json.dumps(build_config(config, user, log=lambda m: print(m, file=sys.stderr)),
                     indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
