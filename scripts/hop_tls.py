#!/usr/bin/env python3
"""TLS on the exit → entry hop (AGENTS.md §53; bot/services/hop_tls.py).

Runs inside the vpn-bot container on entry, as the container's user, so the
key is written into the bot's own volume and never leaves it:

    docker exec -w /app vpn-bot python3 scripts/hop_tls.py init
        make the key and the certificate (once; --force makes new ones,
        and exit must pin the new certificate)
    docker exec -w /app vpn-bot python3 scripts/hop_tls.py show [--pem]
        the certificate exit pins (public), its SHA-256 fingerprint, expiry
    docker exec -w /app vpn-bot python3 scripts/hop_tls.py check
        /health over the HTTPS listener, pinned as on exit (JSON; the
        deploy smoke in scripts/deploy_to_entry.sh)
    docker exec -w /app vpn-bot python3 scripts/hop_tls.py verify
        /health and the probe account's /sub through the listener,
        WEBAPP_URL and every SUB_MIRROR_URLS mirror: same version, same
        bytes, HTTP 200

Never prints the key, the probe account's token or any profile.
"""

import argparse
import json
import os
import sys

# Run as a file, sys.path starts at scripts/; the bot package is one up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.services import hop_tls  # noqa: E402


def _config():
    from bot.config import Settings
    return Settings()


def cmd_init(args, config, out) -> int:
    ip = (args.ip or getattr(config, 'ENTRY_NODE_IP', '') or '').strip()
    if not ip:
        print("no address: pass --ip <entry's public IP> (ENTRY_NODE_IP is empty)", file=out)
        return 2
    _port, directory = hop_tls.settings(config)
    try:
        cert = hop_tls.generate(directory, ip, force=args.force)
    except FileExistsError as e:
        print(str(e), file=out)
        return 1
    except ValueError:
        print(f"not an IP address: {ip!r}", file=out)
        return 2
    print(f"made {cert} (and its key, 0600, next to it)", file=out)
    return _show(cert, out, pem_only=False)


def _show(cert, out, pem_only) -> int:
    info = hop_tls.describe(cert)
    if pem_only:
        out.write(info['pem'])
        return 0
    print(f"names:      {info['names']}", file=out)
    print(f"not after:  {info['not_after']}", file=out)
    print(f"sha256:     {info['sha256']}", file=out)
    print("The certificate to pin on exit (public; compare the fingerprint there with", file=out)
    print("`openssl x509 -noout -fingerprint -sha256 -in /etc/nekovpn-hop/entry-hop.pem`):", file=out)
    out.write(info['pem'])
    return 0


def cmd_show(args, config, out) -> int:
    _port, directory = hop_tls.settings(config)
    cert, _key = hop_tls.files(directory)
    if not os.path.isfile(cert):
        print(f"no certificate in {directory} (init makes one)", file=out)
        return 1
    return _show(cert, out, pem_only=args.pem)


def cmd_check(args, config, out) -> int:
    result = hop_tls.check(config)
    print(json.dumps(result, ensure_ascii=False), file=out)
    return 1 if result['state'] == 'fail' else 0


def cmd_verify(args, config, out) -> int:
    result = hop_tls.verify(config, config.DB_PATH)
    for row in result['paths']:
        cells = [row['path'].ljust(9)]
        for key in ('health', 'sub_clash', 'sub'):
            c = row.get(key, {})
            if 'error' in c:
                cells.append(f"{key}: {c['error']}")
            elif key == 'health':
                cells.append(f"health {c['status']} {c.get('version', '?')}")
            else:
                cells.append(f"{key} {c['status']} {c['size']}B {c['sha256']}")
        print('  '.join(cells), file=out)
    for p in result['problems']:
        print(f"PROBLEM: {p}", file=out)
    print('OK: every path answers the same' if result['ok'] else 'FAILED', file=out)
    return 0 if result['ok'] else 1


def main(argv=None, *, config=None, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_init = sub.add_parser('init', help='make the key and the certificate')
    p_init.add_argument('--ip', help="entry's public address (default ENTRY_NODE_IP)")
    p_init.add_argument('--force', action='store_true',
                        help='replace an existing pair (exit must pin the new certificate)')
    p_show = sub.add_parser('show', help='the certificate exit pins')
    p_show.add_argument('--pem', action='store_true', help='the PEM alone')
    sub.add_parser('check', help='/health over the HTTPS listener, pinned')
    sub.add_parser('verify', help='/health and the probe profile through every path')
    args = parser.parse_args(argv)
    config = config or _config()
    return {'init': cmd_init, 'show': cmd_show, 'check': cmd_check,
            'verify': cmd_verify}[args.cmd](args, config, out)


if __name__ == '__main__':
    sys.exit(main())
