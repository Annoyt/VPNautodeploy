#!/usr/bin/env python3
"""Generate the sing-box config for the probe-proxy sidecar.

The sidecar is what makes HealthChecker honest: one HTTP-proxy inbound
per VPN protocol, each routed through the REAL outbound built by the
same SubscriptionService code that builds user subscriptions, using the
dedicated ``probe_internal`` panel client. HealthChecker then requests
its target domains through http://probe-proxy:<port> and the result
reflects the actual tunnel, not the entry node's own connectivity
(the old checker probed directly and wrote one identical result under
five protocol labels — vk/yandex/youtube were "down" since June 21
because of entry-side DNS, while the tunnels were fine).

Run inside the vpn-bot container, redirect to the mounted config:

    docker exec vpn-bot python3 scripts/gen_probe_config.py \
        > /opt/vpn-bot/probe-proxy/config.json
    docker compose up -d probe-proxy   # или restart, если уже запущен

Regenerate whenever protocol env (SNI/ports/keys) changes.

xhttp is absent by design: sing-box does not implement Xray's XHTTP
transport (see subscription.py) — that path can only be probed by an
xray-core client.
"""

import json
import sys

sys.path.insert(0, '/app')

from bot.config import Settings                      # noqa: E402
from bot.core.database import Database               # noqa: E402
from bot.services.subscription import SubscriptionService  # noqa: E402

PROBE_PORTS = {
    'reality': 18081,
    'hy2': 18082,
    'ws': 18083,
    'stls': 18084,
}

PROBE_CHAT_ID = 'probe_internal'


def main() -> int:
    config = Settings()
    db = Database(config.DB_PATH)
    user = db.get_user(PROBE_CHAT_ID)
    if not user or not user.uuid:
        print(f"probe user {PROBE_CHAT_ID} not found in bot.db",
              file=sys.stderr)
        return 1

    svc = SubscriptionService(config)
    inbounds, outbounds, rules = [], [], []
    for proto, port in PROBE_PORTS.items():
        ob = svc._build_outbound(proto, user)
        if ob is None:
            print(f"skip {proto}: not configured", file=sys.stderr)
            continue
        obs = ob if isinstance(ob, list) else [ob]
        # tls.fragment is a client-app anti-DPI knob some sing-box
        # builds (incl. the pinned sidecar) reject as an unknown field;
        # probes don't need it.
        for o in obs:
            tls = o.get('tls')
            if isinstance(tls, dict):
                tls.pop('fragment', None)
        outbounds.extend(obs)
        tag = obs[0]['tag']
        inbounds.append({
            'type': 'http',
            'tag': f'probe-in-{proto}',
            'listen': '0.0.0.0',   # container-network only, no host ports
            'listen_port': port,
        })
        rules.append({'inbound': [f'probe-in-{proto}'], 'outbound': tag})

    cfg = {
        'log': {'level': 'warn'},
        'inbounds': inbounds,
        'outbounds': outbounds + [{'type': 'direct', 'tag': 'direct'}],
        'route': {'rules': rules, 'final': 'direct'},
    }
    print(json.dumps(cfg, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
