#!/usr/bin/env python3
"""Assert every panel client carries the fields its inbound's protocol needs.

WHY THIS EXISTS
---------------
2026-09-01: the monthly quota job blanked ``flow`` on 80 of 81 clients of
the VLESS-Reality inbound. Without xtls-rprx-vision on the server side no
client could complete the Reality handshake, inbound-443 served ZERO
connections, and it took four days for anyone to notice. Unit tests could
not see it (they mock the panel), and the probe rows that DID see it were
not alerted on.

This is the cheap, dumb, end-to-end backstop: ask the live panel what it
actually holds and fail loudly if any client is unusable. It does not
know about the bot's code paths, so it also catches damage done by hand
in the panel UI, by a fork upgrade, or by a future caller nobody thought
of.

THE RULES (derived from each inbound's own protocol + stream settings)
----------------------------------------------------------------------
* VLESS over raw TCP with REALITY → every client needs a non-empty
  ``flow`` (we use xtls-rprx-vision). Empty flow = the 2026-09-01 outage.
  Plain VLESS+TLS is deliberately NOT held to this: vision is optional
  there, and a false alarm on a future inbound would teach operators to
  ignore the audit.
* VLESS/VMess over ws / httpupgrade / xhttp → ``flow`` must be EMPTY.
  xtls-rprx-vision is only valid over raw TCP; a stray flow on the CDN
  mirrors breaks those links — the exact mistake a naive "just always
  set flow" fix would introduce.
* Shadowsocks → every client needs a non-empty ``password`` (the
  per-user SS-2022 key the subscription embeds).
* Any protocol → every client needs a non-empty ``email`` and ``id``.

USAGE
-----
Runs on entry, inside the bot container (it needs the panel creds from
the bot's env; ``bot`` is importable only from the app root)::

    docker exec -e PYTHONPATH=/app vpn-bot \
        python3 /app/scripts/verify_panel_client_fields.py

Exit codes: 0 = clean, 1 = at least one client is broken, 2 = could not
check (panel unreachable / not configured) — a 2 must not be read as a
pass.
"""

import asyncio
import json
import sys
from typing import Dict, List, Tuple


# network values that carry the VLESS stream inside another protocol —
# xtls-rprx-vision cannot be used on any of them.
NON_RAW_NETWORKS = {'ws', 'httpupgrade', 'xhttp', 'grpc', 'h2', 'splithttp'}


def _parse(blob):
    """Panel JSON columns arrive as strings on some routes, dicts on
    others. Every reader in this codebase has to cope with both."""
    if isinstance(blob, str):
        try:
            return json.loads(blob or '{}')
        except ValueError:
            return {}
    return blob or {}


def required_fields(inbound: dict) -> Tuple[List[str], List[str]]:
    """(must be non-empty, must be empty) for clients of this inbound."""
    protocol = (inbound.get('protocol') or '').lower()
    stream = _parse(inbound.get('streamSettings'))
    network = (stream.get('network') or 'tcp').lower()
    security = (stream.get('security') or 'none').lower()

    must_have = ['email', 'id']
    must_be_empty = []

    if protocol == 'vless':
        if network not in NON_RAW_NETWORKS and security == 'reality':
            must_have.append('flow')
        elif network in NON_RAW_NETWORKS:
            must_be_empty.append('flow')
        # raw VLESS+TLS: vision is optional — neither required nor banned
    elif protocol == 'vmess':
        must_be_empty.append('flow')
    elif protocol == 'shadowsocks':
        must_have.append('password')
        must_be_empty.append('flow')

    return must_have, must_be_empty


def audit_inbound(inbound: dict) -> List[str]:
    """Return a list of human-readable problems for one inbound."""
    if not inbound.get('enable', True):
        return []
    must_have, must_be_empty = required_fields(inbound)
    clients = _parse(inbound.get('settings')).get('clients') or []
    iid = inbound.get('id')
    tag = inbound.get('tag') or f'inbound-{iid}'
    problems = []
    for c in clients:
        email = c.get('email') or '<no-email>'
        for field in must_have:
            if not (c.get(field) or ''):
                problems.append(
                    f"{tag} (id={iid}, {inbound.get('protocol')}): "
                    f"{email} has empty '{field}'"
                )
        for field in must_be_empty:
            if c.get(field):
                problems.append(
                    f"{tag} (id={iid}, {inbound.get('protocol')}): "
                    f"{email} must NOT carry '{field}' "
                    f"(got {c.get(field)!r})"
                )
    return problems


def audit_all(inbounds: List[dict]) -> List[str]:
    problems = []
    for ib in inbounds:
        problems.extend(audit_inbound(ib))
    return problems


async def _fetch_inbounds(api, listed: List[dict]) -> List[dict]:
    """``inbounds/list`` omits clients on this fork (and hides id 1
    entirely), so re-read each one by id. Ids come from the list plus
    the ones the bot is configured to use, so a hidden inbound is still
    audited."""
    ids, out = [], []
    for ib in listed:
        if ib.get('id') is not None:
            ids.append(int(ib['id']))
    for extra in _configured_ids():
        if extra and extra not in ids:
            ids.append(extra)
    for iid in sorted(ids):
        full = await api.get_inbound(iid)
        if full:
            out.append(full)
    return out


def _configured_ids() -> List[int]:
    from bot.config import Settings
    cfg = Settings()
    return [
        int(getattr(cfg, name, 0) or 0)
        for name in ('INBOUND_ID', 'WS_INBOUND_ID', 'SS_INBOUND_ID',
                     'WS2_INBOUND_ID')
    ]


async def _main() -> int:
    from bot.config import Settings
    from bot.services.xui_service import XUIService

    xui = XUIService(Settings())
    if not xui.api:
        print('CANNOT CHECK: panel API not configured (XUI_API_URL missing)')
        return 2
    try:
        listed = await xui.api.get_inbounds()
        inbounds = await _fetch_inbounds(xui.api, listed)
    except Exception as e:                       # noqa: BLE001
        print(f'CANNOT CHECK: panel unreachable: {e}')
        return 2
    finally:
        await xui.api.close()   # else aiohttp complains on interpreter exit
    if not inbounds:
        print('CANNOT CHECK: panel returned no inbounds')
        return 2

    problems = audit_all(inbounds)
    total = sum(len(_parse(ib.get('settings')).get('clients') or [])
                for ib in inbounds)
    if problems:
        print(f'BROKEN: {len(problems)} problem(s) across {total} client '
              f'records on {len(inbounds)} inbounds:')
        for p in problems[:40]:
            print('  ' + p)
        if len(problems) > 40:
            print(f'  … and {len(problems) - 40} more')
        return 1
    print(f'OK: {total} client records on {len(inbounds)} inbounds, '
          f'all required per-protocol fields present')
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(_main()))
