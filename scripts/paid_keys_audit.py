#!/usr/bin/env python3
"""Audit paid keys on the panel; with --apply, set the drifted ones right.

Runs inside the vpn-bot container:

    docker exec -w /app vpn-bot python3 scripts/paid_keys_audit.py                    dry run: the audit
    docker exec -w /app vpn-bot python3 scripts/paid_keys_audit.py --apply            fix active payers
    docker exec -w /app vpn-bot python3 scripts/paid_keys_audit.py --apply --lapsed   also switch lapsed payers' keys off

Before PR #32, «🔑 Мой ключ» re-added the client of every key holder who
pressed it: a delete + re-add with demo values (10 GB, no expiry, one
address). Since then only a payment, /quota and /expire rewrite a paid
key's quota or date; the monthly paid pass only re-enables it and zeroes
the traffic. So a payer's key may still be capped at 10 GB, and a lapsed
payer's key with no expiry never switches off.

For every payer with a key:
- An active payer (ru_exit.is_eligible) should hold what a payment writes
  (billing.tier_client_values): the paid quota, the paid-until date, the
  paid address limit, enabled. --apply saves the bot's quota and limit
  floors first, then updates the key in place, or re-adds it under the
  same uuid when the panel lost it (billing.readd_client).
- A lapsed payer (status 'paid' after the date, or a payer's ticket after
  it) should have a key that is off. --lapsed, with --apply, writes the
  passed paid-until date and enable=false in place. It is a flag of its
  own: fixing an active payer only gives back what was paid for, while
  this takes a working key away. Read the dry run first.

The panel is read through its API, the way /stats and /sub read it: the
traffic row that gates xray and hy2 (quota, expiry, enable, use) and the
client record (address limit). Nothing is written without --apply. Each
write is audited as admin_actions('owner', 'paid_keys_audit', <chat_id>,
<what changed>).
"""

import argparse
import os
import sys
from datetime import datetime, timezone

ACTION = 'paid_keys_audit'
GB = 1024 ** 3


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_panel(xui, email):
    """The key as the panel holds it, or None when no inbound has it.
    Quota, expiry, enable and use come from the traffic row, or from the
    record when the row cannot be read (``used`` is None then)."""
    record = xui.get_client_sync(email)
    if not record:
        return None
    row = xui.get_client_traffic_sync(email)
    if row:
        return {
            'total': _int(row.get('total')),
            'expiry': _int(row.get('expiry_time')),
            'enable': bool(row.get('enable', True)),
            'used': _int(row.get('upload')) + _int(row.get('download')),
            'limit': _int(record.get('limitIp')),
        }
    return {
        'total': _int(record.get('totalGB')),
        'expiry': _int(record.get('expiryTime')),
        'enable': bool(record.get('enable', True)),
        'used': None,
        'limit': _int(record.get('limitIp')),
    }


def works(panel, now_ms: int) -> bool:
    """The panel lets this key in: enabled, not expired, quota left."""
    if not panel or not panel['enable']:
        return False
    if panel['expiry'] and panel['expiry'] <= now_ms:
        return False
    used = panel['used']
    return not panel['total'] or used is None or used < panel['total']


def active_changes(user, config, panel, target) -> list:
    """[(field, now, should be)] for an active payer. ``quota``,
    ``expiry``, ``limit`` and ``enable`` are the panel's; ``bot quota``
    and ``bot limit`` are users.quota_gb and users.limit_ip."""
    from bot.services.billing import paid_limit_ip, paid_quota_gb
    changes = []
    quota, limit = paid_quota_gb(user, config), paid_limit_ip(user, config)
    if float(user.quota_gb or 0) != quota:
        changes.append(('bot quota', user.quota_gb, quota))
    if user.limit_ip != limit:
        changes.append(('bot limit', user.limit_ip, limit))
    if panel is None:
        return changes
    if panel['total'] != target['totalGB']:
        changes.append(('quota', panel['total'], target['totalGB']))
    if panel['expiry'] != target['expiryTime']:
        changes.append(('expiry', panel['expiry'], target['expiryTime']))
    if panel['limit'] != target['limitIp']:
        changes.append(('limit', panel['limit'], target['limitIp']))
    if not panel['enable'] and (panel['used'] is None
                                or panel['used'] < target['totalGB']):
        changes.append(('enable', False, True))
    return changes


def plan(db, config, xui, now: datetime) -> list:
    """One dict per payer with a key: user, kind ('active' | 'lapsed'),
    panel (read_panel), lost (no inbound has the client, by
    client_attached_sync, which says "attached" when it cannot tell: a
    failed read is not a lost key), target (an active payer's panel
    values) and changes (an active payer's drift)."""
    from bot.services.billing import tier_client_values
    from bot.services.ru_exit import is_eligible
    rows = []
    for user in db.get_all_users() or []:
        if not (user.uuid and user.email):
            continue
        if is_eligible(user, now):
            kind, target = 'active', tier_client_values(user, config, now)
        elif tier_client_values(user, config, now) is None:
            kind, target = 'lapsed', None
        else:
            continue                      # a demo key
        panel = read_panel(xui, user.email)
        lost = panel is None and not xui.client_attached_sync(user.email)
        rows.append({
            'user': user, 'kind': kind, 'panel': panel, 'lost': lost,
            'target': target,
            'changes': (active_changes(user, config, panel, target)
                        if kind == 'active' else []),
        })
    return rows


def _gb(value) -> str:
    return 'unlimited' if not value else f'{value / GB:g} GB'


def _when(ms) -> str:
    if not ms:
        return 'never'
    day = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return day.strftime('%Y-%m-%d %H:%M')


def _show(field, value) -> str:
    if field == 'quota':
        return _gb(value)
    if field == 'expiry':
        return _when(value)
    if field == 'bot quota':
        return f'{value:g} GB'
    return str(value)


def describe(row) -> str:
    """The row's line after the chat_id."""
    user, panel = row['user'], row['panel']
    head = f"{user.status} {row['kind']}"
    if row['kind'] == 'lapsed':
        head += f" since {(user.subscription_expiry or '')[:10]}"
    parts = [f'{field} {_show(field, now)} -> {_show(field, want)}'
             for field, now, want in row['changes']]
    if panel is None:
        parts.insert(0, 'not on the panel' if row['lost']
                     else 'panel unreadable')
    elif row['kind'] == 'lapsed':
        parts.append(f"quota {_gb(panel['total'])}, expiry "
                     f"{_when(panel['expiry'])}, "
                     f"{'enabled' if panel['enable'] else 'disabled'}")
    return f"{head}: {', '.join(parts) or 'ok'}"


def apply(db, xui, config, rows, now_ms: int, lapsed: bool) -> list:
    """Write every row that needs it; [(chat_id, ok)]. The bot's floors
    are saved before the panel: a failed panel write leaves the row to
    the next run."""
    from bot.services.billing import (
        paid_limit_ip, paid_quota_gb, paid_until_ms, readd_client,
    )
    results = []
    for row in rows:
        user = row['user']
        if row['panel'] is None and not row['lost']:
            continue                      # unreadable: never act on it
        if row['kind'] == 'active' and (row['changes'] or row['lost']):
            quota, limit = paid_quota_gb(user, config), paid_limit_ip(user, config)
            if float(user.quota_gb or 0) != quota or user.limit_ip != limit:
                user.quota_gb, user.limit_ip = quota, limit
                db.save_user(user)
            if row['lost']:
                # Asked again: an add of a client the panel has is delete
                # + re-add, which zeroes the key's traffic.
                ok = (not xui.client_attached_sync(user.email)
                      and readd_client(xui, config, user, row['target']))
            elif row['panel'] is not None and any(
                    not field.startswith('bot ') for field, _, _ in row['changes']):
                ok = bool(xui.sync_client_settings_sync(user.email, row['target']))
            else:
                ok = True                 # the bot's copy was all
            detail = describe(row).split(': ', 1)[1]
        elif row['kind'] == 'lapsed' and lapsed and works(row['panel'], now_ms):
            off = {'expiryTime': paid_until_ms(user.subscription_expiry),
                   'enable': False}
            ok = bool(xui.sync_client_settings_sync(user.email, off))
            detail = f"lapsed: off, expiry {_when(off['expiryTime'])}"
        else:
            continue
        if ok:
            db.log_admin_action('owner', ACTION, user.chat_id, detail)
        results.append((user.chat_id, bool(ok)))
    return results


def main(argv=None, *, db=None, xui=None, config=None, out=sys.stdout,
         now=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true',
                    help="fix active payers' keys (default: dry run)")
    ap.add_argument('--lapsed', action='store_true',
                    help="with --apply: also switch lapsed payers' working keys off")
    args = ap.parse_args(argv)
    if args.lapsed and not args.apply:
        ap.error('--lapsed goes with --apply')

    if db is None or xui is None or config is None:
        import warnings
        warnings.simplefilter('ignore')
        from bot.config import Settings
        from bot.core.database import Database
        from bot.services.xui_service import XUIService
        config = config or Settings()
        db = db or Database(config.DB_PATH)
        xui = xui or XUIService(config)

    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    now_ms = int(now.replace(tzinfo=timezone.utc).timestamp() * 1000)
    rows = plan(db, config, xui, now)
    count = {'ok': 0, 'fix': 0, 'missing': 0, 'unreadable': 0,
             'working': 0, 'off': 0}
    for row in rows:
        mark = ''
        if row['panel'] is None and not row['lost']:
            count['unreadable'] += 1
            mark = '  <- not touched'
        elif row['kind'] == 'active':
            if row['lost']:
                count['missing'] += 1
                mark = '  <- re-add'
            elif row['changes']:
                count['fix'] += 1
                mark = '  <- fix'
            else:
                count['ok'] += 1
        elif works(row['panel'], now_ms):
            count['working'] += 1
            mark = '  <- still works; --lapsed switches it off'
        else:
            count['off'] += 1
        print(f"{row['user'].chat_id}: {describe(row)}{mark}", file=out)
    print(f"{len(rows)} payers. Active: {count['ok']} ok, {count['fix']} "
          f"to fix, {count['missing']} not on the panel. Lapsed: "
          f"{count['working']} still working, {count['off']} off. Panel "
          f"unreadable: {count['unreadable']}.", file=out)
    if not args.apply:
        print("dry run; --apply fixes active payers, --apply --lapsed also "
              "switches lapsed payers' keys off", file=out)
        return 0
    results = apply(db, xui, config, rows, now_ms, args.lapsed)
    failed = [chat_id for chat_id, ok in results if not ok]
    print(f'written {len(results) - len(failed)}, failed: '
          f'{", ".join(failed) or "none"}', file=out)
    return 1 if failed else 0


if __name__ == '__main__':
    # Run as a file, sys.path starts at scripts/; the bot package is one
    # level up (/app in the container).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
