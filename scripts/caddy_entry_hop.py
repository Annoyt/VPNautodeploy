#!/usr/bin/env python3
"""Move exit's Caddy onto the hop's HTTPS listener (AGENTS.md §53,
docs/runbooks/entry_hop_tls.md). Run as root on EXIT, stdlib only.

    python3 caddy_entry_hop.py              what would change (a diff), nothing written
    python3 caddy_entry_hop.py --apply      back up, write, `caddy validate`, reload

Every site that proxies to the bot in clear, a line that is exactly
``reverse_proxy <entry>:8080``, becomes ``import entry_hop``, and the
snippet below is defined once at the top (after the global options block,
which must stay first). The snippet dials entry's address on the hop port,
checks the name ``entry-hop.invalid``, and trusts the pinned certificate
alone: its trust pool replaces the system's CAs for this upstream.

Refuses, writing nothing, when the file is not what it expects: the number
of such lines differs from ``--expect`` (2: the dashboard and the
Cloudflare mirror), one of them carries a block of its own, the snippet is
already there, or the pin is missing. A config ``caddy validate`` rejects
is put back from the backup before anything is reloaded.
"""

import argparse
import datetime
import difflib
import os
import re
import shutil
import subprocess
import sys

SNIPPET_NAME = 'entry_hop'
HOP_NAME = 'entry-hop.invalid'


def snippet(entry: str, port: int, pin: str) -> str:
    """The Caddyfile snippet, tab-indented like Caddy's own formatter."""
    return (
        f"({SNIPPET_NAME}) {{\n"
        f"\treverse_proxy https://{entry}:{port} {{\n"
        f"\t\ttransport http {{\n"
        f"\t\t\ttls_server_name {HOP_NAME}\n"
        f"\t\t\ttls_trust_pool file {pin}\n"
        f"\t\t}}\n"
        f"\t}}\n"
        f"}}\n"
    )


def _global_block_end(text: str) -> int:
    """Offset just past the global options block, 0 when there is none."""
    i = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            i += len(line)
            continue
        if stripped != '{':
            return 0
        break
    else:
        return 0
    depth = 0
    pos = i
    for line in text[i:].splitlines(keepends=True):
        code = line.split('#', 1)[0]
        depth += code.count('{') - code.count('}')
        pos += len(line)
        if depth == 0:
            return pos
    raise ValueError('the global options block never closes')


def transform(text: str, *, entry: str, old_port: int, port: int, pin: str,
              expect: int) -> str:
    """The new Caddyfile, or ValueError saying why it is left alone."""
    if re.search(rf'^\s*\({re.escape(SNIPPET_NAME)}\)\s*\{{', text, re.M):
        raise ValueError(f'({SNIPPET_NAME}) is already defined: applied before?')
    target = rf'(?:http://)?{re.escape(entry)}:{old_port}'
    plain = re.compile(rf'^([ \t]*)reverse_proxy[ \t]+{target}[ \t]*$', re.M)
    with_block = re.compile(rf'^[ \t]*reverse_proxy[ \t]+{target}\b[^\n]*\{{', re.M)
    if with_block.search(text):
        raise ValueError(f'a `reverse_proxy {entry}:{old_port}` carries a block of its '
                         f'own: edit it by hand (docs/runbooks/entry_hop_tls.md)')
    hits = plain.findall(text)
    if len(hits) != expect:
        raise ValueError(f'expected {expect} `reverse_proxy {entry}:{old_port}` lines, '
                         f'found {len(hits)}: look at the file (--expect N if that is right)')
    text = plain.sub(lambda m: f'{m.group(1)}import {SNIPPET_NAME}', text)
    at = _global_block_end(text)
    block = snippet(entry, port, pin)
    if at:
        return text[:at] + '\n' + block + text[at:]
    return block + '\n' + text


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--caddyfile', default='/etc/caddy/Caddyfile')
    p.add_argument('--entry', default='130.49.146.10', help="entry's address")
    p.add_argument('--old-port', type=int, default=8080)
    p.add_argument('--port', type=int, default=8448, help="the hop's HTTPS port")
    p.add_argument('--pin', default='/etc/nekovpn-hop/entry-hop.pem')
    p.add_argument('--expect', type=int, default=2, help='lines to move (default 2)')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--caddy', default='caddy', help='the caddy binary (validate)')
    p.add_argument('--reload', default='systemctl reload caddy',
                   help='the reload command after a valid write ("" = none)')
    args = p.parse_args(argv)

    if not os.access(args.pin, os.R_OK):
        print(f'the pin {args.pin} is not readable: install it first (runbook step 2)')
        return 1
    with open(args.caddyfile) as f:
        old = f.read()
    try:
        new = transform(old, entry=args.entry, old_port=args.old_port, port=args.port,
                        pin=args.pin, expect=args.expect)
    except ValueError as e:
        print(f'not changed: {e}')
        return 1
    sys.stdout.writelines(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        args.caddyfile, args.caddyfile + ' (new)'))
    if not args.apply:
        print('\ndry run: nothing written (--apply writes, validates and reloads)')
        return 0

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
    backup = f'{args.caddyfile}.bak-{stamp}'
    shutil.copy2(args.caddyfile, backup)
    st = os.stat(args.caddyfile)
    tmp = args.caddyfile + '.new'
    with open(tmp, 'w') as f:
        f.write(new)
    # the same mode and owner: Caddy reloads as its own user and must read it
    os.chmod(tmp, st.st_mode & 0o7777)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.replace(tmp, args.caddyfile)
    r = subprocess.run([args.caddy, 'validate', '--config', args.caddyfile,
                        '--adapter', 'caddyfile'], capture_output=True, text=True)
    if r.returncode != 0:
        shutil.copy2(backup, args.caddyfile)
        print(f'caddy validate rejected it; {args.caddyfile} put back from {backup}:\n'
              f'{(r.stderr or r.stdout).strip()[-2000:]}')
        return 1
    print(f'written and valid; backup {backup}')
    if args.reload:
        r = subprocess.run(args.reload, shell=True, capture_output=True, text=True)
        if r.returncode != 0:
            print(f'reload failed (rc {r.returncode}): {(r.stderr or r.stdout).strip()[-1000:]}\n'
                  f'Caddy keeps the config it runs. To go back: cp -a {backup} '
                  f'{args.caddyfile}')
            return 1
        print('reloaded')
    return 0


if __name__ == '__main__':
    sys.exit(main())
