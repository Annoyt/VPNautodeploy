#!/usr/bin/env python3
"""Create the VLESS + XHTTP + Reality inbound on exit (IMPROVEMENT_PLAN F, E3).

DRY RUN by default: prints the inbound it would create and the haproxy lines
entry needs. ``--apply`` creates it through the exit panel's API and attaches
every client of the main Reality inbound (same uuid, same quota, no flow —
XHTTP carries none). Run inside vpn-bot, the way the other panel scripts run:

    docker exec -w /app vpn-bot python3 scripts/setup_xhr_inbound.py
    docker exec -w /app vpn-bot python3 scripts/setup_xhr_inbound.py --apply

Before --apply (AGENTS.md §23, §47-§51):
  1. XHR_SNI = a dest whose TLS Certificate record is ≤ 8000 bytes, measured
     from exit (scripts/rotate_reality_dest.py --sni <host> checks exactly
     that) — and NOT the main inbound's SNI: entry's haproxy routes by SNI.
  2. The exit's xray must stay below 26.7.11 (mihomo does not keep Reality
     compatible past it) — the stand in tests/integration/test_xhr_stand.py
     pins 26.6.22, the version on exit on 2026-11-10.

After --apply:
  3. entry haproxy: the printed acl / use_backend / backend lines, reload.
  4. entry .env: XHR_SNI, XHR_INBOUND_ID=<printed id> (new keys get attached),
     optionally XHR_PATH / XHR_MAX_CONNECTIONS; recreate vpn-bot.
  5. the mihomo probe sidecar: scripts/gen_probe_config_mihomo.py, then
     ``docker compose --profile xhr up -d probe-proxy-mihomo``.
  6. switch ``xhr`` on in the cascade editor (it is opt-in: listed disabled).

The keys: the new inbound reuses the main inbound's Reality key pair and its
first short id, so REALITY_PUBLIC_KEY stays valid (XHR_PUBLIC_KEY / XHR_SID
override). PROXY protocol: mirrored from the main inbound's sockopt — entry's
haproxy sends it to the main inbound, and the same backend line does here.
"""

import argparse
import asyncio
import copy
import json
import sys

sys.path.insert(0, '/app')


def build_inbound(source: dict, *, sni: str, port: int, path: str,
                  short_id: str = '') -> dict:
    """The new inbound (panel ``inbounds/add`` body) from the main Reality
    inbound ``source`` (``inbounds/get`` shape: settings / streamSettings are
    JSON strings). Pure — the unit tests drive it."""
    stream = json.loads(source.get('streamSettings') or '{}')
    reality = stream.get('realitySettings') or {}
    if not reality.get('privateKey'):
        raise ValueError('the main inbound carries no Reality private key')
    short_ids = [short_id] if short_id else (reality.get('shortIds') or [''])[:1]
    new_stream = {
        'network': 'xhttp',
        'security': 'reality',
        'xhttpSettings': {'path': path, 'mode': 'auto'},
        'realitySettings': {
            'show': False,
            'xver': reality.get('xver', 0),
            'dest': f'{sni}:443',
            'serverNames': [sni],
            'privateKey': reality['privateKey'],
            'shortIds': short_ids,
            'settings': copy.deepcopy(reality.get('settings') or {}),
        },
    }
    sockopt = stream.get('sockopt') or (stream.get('tcpSettings') or {})
    if sockopt.get('acceptProxyProtocol'):
        new_stream['sockopt'] = {'acceptProxyProtocol': True}
    return {
        'up': 0, 'down': 0, 'total': 0, 'remark': 'xhr (VLESS XHTTP Reality)',
        'enable': True, 'expiryTime': 0, 'listen': '', 'port': int(port),
        'protocol': 'vless',
        'settings': json.dumps({'clients': [], 'decryption': 'none'}),
        'streamSettings': json.dumps(new_stream),
        'sniffing': json.dumps({'enabled': True, 'destOverride': ['http', 'tls', 'quic']}),
    }


def clients_for(source: dict) -> list:
    """The main inbound's clients, re-shaped for the new one: everything kept
    but ``flow`` (XHTTP carries no Vision)."""
    settings = json.loads(source.get('settings') or '{}')
    out = []
    for client in settings.get('clients') or []:
        c = {k: v for k, v in client.items() if k != 'flow'}
        c['flow'] = ''
        out.append(c)
    return out


def haproxy_lines(sni: str, exit_ip: str, port: int, proxy_protocol: bool) -> str:
    send = ' send-proxy-v2' if proxy_protocol else ''
    return (
        "# entry haproxy, frontend on :8443 (next to is_reality_sni)\n"
        f"    acl is_xhr_sni req.ssl_sni -i {sni}\n"
        "    use_backend xhr_exit if is_xhr_sni\n"
        "\n"
        "backend xhr_exit\n"
        "    mode tcp\n"
        f"    server exit {exit_ip}:{port}{send}\n"
    )


async def _run(apply: bool) -> int:
    from bot.config import Settings
    from bot.services.xui_service import XUIService
    cfg = Settings()
    sni = (cfg.XHR_SNI or '').strip()
    if not sni:
        print('XHR_SNI is empty: set it in .env first (see the docstring)', file=sys.stderr)
        return 2
    if sni == (cfg.SNI_VALUE or '').strip():
        print('XHR_SNI equals SNI_VALUE: haproxy could not tell the two apart', file=sys.stderr)
        return 2
    xui = XUIService(cfg)
    source = await xui.api.get_inbound(int(cfg.INBOUND_ID or 1))
    if not source:
        print('could not read the main Reality inbound — no decision taken', file=sys.stderr)
        return 2
    inbound = build_inbound(source, sni=sni, port=cfg.XHR_EXIT_PORT, path=cfg.XHR_PATH,
                            short_id=cfg.XHR_SID)
    clients = clients_for(source)
    stream = json.loads(inbound['streamSettings'])
    shown = copy.deepcopy(stream)
    shown['realitySettings']['privateKey'] = '<the main inbound key>'
    print(f"inbound: vless xhttp+reality on exit :{cfg.XHR_EXIT_PORT}, sni {sni}, "
          f"path {cfg.XHR_PATH}, {len(clients)} clients to attach")
    print(json.dumps(shown, indent=1))
    print(haproxy_lines(sni, cfg.EXIT_NODE_IP or '<exit ip>', cfg.XHR_EXIT_PORT,
                        'sockopt' in stream))
    if not apply:
        print('dry run — nothing created; --apply to create it')
        return 0
    resp = await xui.api._authenticated_request(
        'POST', f"{xui.api.base_url}/panel/api/inbounds/add", json=inbound)
    body = await resp.json(content_type=None)
    if not (body or {}).get('success'):
        print(f'panel refused the inbound: {body}', file=sys.stderr)
        return 1
    new_id = int((body.get('obj') or {}).get('id') or 0)
    created = await xui.api.get_inbound(new_id)
    settings = json.loads(created.get('settings') or '{}')
    settings['clients'] = clients
    created['settings'] = json.dumps(settings)
    if not await xui.api.update_inbound(new_id, created):
        print(f'inbound {new_id} created, clients NOT attached — attach them by hand',
              file=sys.stderr)
        return 1
    print(f'created inbound id {new_id} with {len(clients)} clients; '
          f'put XHR_INBOUND_ID={new_id} into entry .env')
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--apply', action='store_true', help='create the inbound (default: dry run)')
    args = ap.parse_args(argv)
    return asyncio.run(_run(args.apply))


if __name__ == '__main__':
    sys.exit(main())
