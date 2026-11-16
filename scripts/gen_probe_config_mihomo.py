#!/usr/bin/env python3
"""Generate the mihomo config for the second probe sidecar (IMPROVEMENT_PLAN F, E3).

sing-box (the probe-proxy sidecar, scripts/gen_probe_config.py) has no
XHTTP, so VLESS + XHTTP + Reality is probed through mihomo — the core
NekoVPN and FlClash run. The SSH emergency path (E4, ssh-exit on exit) is
probed here too (since 2026-11-15): users reach it from their Clash
profile, i.e. through mihomo. So is the same server through entry's relay
(since 2026-11-16), from this sidecar to entry's public address. One
HTTP-proxy listener per protocol, each bound to its proxy
(``listeners[].proxy``), the proxy built by the same SubscriptionService
code as the users' Clash profile, under the dedicated ``probe_internal``
client:

    docker exec vpn-bot python3 scripts/gen_probe_config_mihomo.py \
        > /opt/vpn-bot/probe-proxy-mihomo/config.yaml
    docker compose up -d probe-proxy-mihomo     # first time
    docker exec probe-proxy-mihomo /mihomo -t -d /root/.config/mihomo
    docker compose restart probe-proxy-mihomo  # after a regeneration

Ports come from HealthChecker.probe_ports_for (xhr → 18087, ssh → 18088,
ssh-entry → 18089), so the checker and this file cannot drift apart. Emitted as JSON, a YAML
subset mihomo reads. A tag the deployment has no server for is skipped,
with a line on stderr: the checker then finds no listener and writes no
rows for it, so a sidecar without the listener pages nothing.
"""

import json
import sys

sys.path.insert(0, '/app')

from bot.config import Settings                      # noqa: E402
from bot.core.database import Database               # noqa: E402
from bot.services import ssh_exit                    # noqa: E402
from bot.services.health_checker import HealthChecker  # noqa: E402
from bot.services.subscription import SubscriptionService  # noqa: E402

PROBE_CHAT_ID = 'probe_internal'
MIHOMO_TAGS = HealthChecker.MIHOMO_TAGS
# The SSH servers of the emergency profile, by probe tag: which builder
# names the one each listener is bound to.
SSH_BUILDERS = {HealthChecker.SSH_TAG: ssh_exit.outbound,
                HealthChecker.SSH_ENTRY_TAG: ssh_exit.entry_outbound}


def _proxy_for(proto: str, user, svc, config):
    """The mihomo proxy users get for ``proto``, or None."""
    if proto in SSH_BUILDERS:
        # Exactly what a payer's emergency profile carries: the server list
        # build_clash_config makes under lockdown, its ssh entry of that
        # name (on exit, or through entry's relay). None when the user is
        # not an eligible payer (ssh_exit.outbound).
        want = SSH_BUILDERS[proto](config, user)
        if want is None:
            return None
        proxies, _calls, _protocols = svc._clash_proxies(user, (), emergency=True)
        return next((p for p in proxies
                     if p.get('type') == 'ssh' and p.get('name') == want['tag']), None)
    ob = svc._build_outbound(proto, user)
    return svc._to_clash_proxy(ob, {ob['tag']: ob}) if ob else None


def build_config(config, user, svc=None, *, log=None) -> dict:
    """The mihomo config dict (unit-tested): listeners only for the tags the
    sing-box sidecar does not probe."""
    svc = svc or SubscriptionService(config)
    log = log or (lambda msg: None)
    proxies, listeners = [], []
    for proto, port in HealthChecker.probe_ports_for(config).items():
        if proto not in MIHOMO_TAGS:
            continue
        proxy = _proxy_for(proto, user, svc, config)
        if proxy is None:
            if proto in SSH_BUILDERS:
                log(f"skip {proto}: the probe user is not an eligible payer "
                    f"(status paid, paid-until not passed) — the emergency "
                    f"profile carries no ssh server for it")
            else:
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
