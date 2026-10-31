#!/usr/bin/env python3
"""Bring every key's address limit to its tier: paid PAID_LIMIT_IP (5),
demo DEMO_LIMIT_IP (2).

Runs inside the vpn-bot container:

    docker exec -w /app vpn-bot python3 scripts/limit_ip.py                 dry run
    docker exec -w /app vpn-bot python3 scripts/limit_ip.py --apply         update
    docker exec -w /app vpn-bot python3 scripts/limit_ip.py --tier demo     one tier

Payments, /addmail and new keys set the limit from now on; this is for
keys made before they did. The panel's own value is read (it has drifted
from the bot's: unlimited 0 on keys the panel took in before the camelCase
fix), and both are set to the tier's value (billing.paid_limit_ip /
demo_limit_ip): never below a higher value an admin set, an admin's 0 (no
limit) kept. Each change is audited as
admin_actions('owner', 'limit_ip', <chat_id>, '<tier> old->new').
"""

import argparse
import json
import os
import sys

ACTION = 'limit_ip'
TIERS = ('paid', 'demo')


def tier_of(user):
    """'paid' for a payer (status 'paid', or 'support_topic' with a paid-
    until date or a paid previous state: ru_exit's rule for telling a
    payer's open ticket from a demo user's), 'demo' for a demo key
    (incl. a demo user's open ticket), None for anyone else."""
    status = getattr(user, 'status', '') or ''
    if status == 'paid':
        return 'paid'
    if status == 'support_topic':
        payer = (getattr(user, 'subscription_expiry', None)
                 or getattr(user, 'previous_state', None) == 'paid')
        return 'paid' if payer else 'demo'
    if status == 'demo':
        return 'demo'
    return None


def plan(db, config, panel_limit, tiers=TIERS) -> list:
    """[(user, tier, panel value, target)] for every key holder of
    ``tiers``. ``panel_limit(email)`` is the panel's limitIp, None when
    the panel does not show the key."""
    from bot.services.billing import demo_limit_ip, paid_limit_ip
    target_of = {'paid': paid_limit_ip, 'demo': demo_limit_ip}
    rows = []
    for user in db.get_all_users() or []:
        tier = tier_of(user)
        if user.uuid and user.email and tier in tiers:
            rows.append((user, tier, panel_limit(user.email),
                         target_of[tier](user, config)))
    return rows


def needs_change(user, panel, target) -> bool:
    return user.limit_ip != target or panel != target


def apply(db, xui, rows) -> list:
    """Set every row that differs; [(chat_id, ok)]. The bot's value is
    saved first: a failed panel update leaves the row to the next run."""
    results = []
    for user, tier, panel, target in rows:
        if not needs_change(user, panel, target):
            continue
        user.limit_ip = target
        db.save_user(user)
        ok = bool(xui.sync_client_settings_sync(user.email, {'limitIp': target}))
        if ok:
            db.log_admin_action('owner', ACTION, user.chat_id,
                                f'{tier} {panel}->{target}')
        results.append((user.chat_id, ok))
    return results


def inbound_ids(config) -> list:
    """Every inbound a bot key lives on, Shadowsocks first like the bot's
    own reads."""
    def _id(key, default=0):
        try:
            return int(getattr(config, key, default) or 0)
        except (TypeError, ValueError):
            return 0
    ids = [_id('SS_INBOUND_ID'), _id('INBOUND_ID', 1) or 1,
           _id('WS_INBOUND_ID'), _id('WS2_INBOUND_ID')]
    return [i for n, i in enumerate(ids) if i and i not in ids[:n]]


def panel_reader(xui, config):
    """email -> the panel's limitIp, from one read per inbound (the panel
    writes a client's limitIp to every inbound it is on)."""
    limits = {}
    for iid in inbound_ids(config):
        try:
            inbound = xui.get_inbound_settings_sync(iid) or {}
            raw = inbound.get('settings')
            settings = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception as e:
            print(f'inbound {iid} unreadable: {e}', file=sys.stderr)
            continue
        for client in settings.get('clients') or []:
            email = client.get('email')
            if email and email not in limits:
                try:
                    limits[email] = int(client.get('limitIp') or 0)
                except (TypeError, ValueError):
                    limits[email] = None
    return limits.get


def main(argv=None, *, db=None, xui=None, config=None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true', help='update (default: dry run)')
    ap.add_argument('--tier', choices=TIERS, help='one tier only (default: both)')
    args = ap.parse_args(argv)

    if db is None or xui is None or config is None:
        import warnings
        warnings.simplefilter('ignore')
        from bot.config import Settings
        from bot.core.database import Database
        from bot.services.xui_service import XUIService
        config = config or Settings()
        db = db or Database(config.DB_PATH)
        xui = xui or XUIService(config)

    tiers = (args.tier,) if args.tier else TIERS
    rows = plan(db, config, panel_reader(xui, config), tiers)
    for user, tier, panel, target in rows:
        if needs_change(user, panel, target):
            print(f'{user.chat_id} {tier}: bot={user.limit_ip} panel={panel} -> {target}',
                  file=out)
    counts = {t: sum(1 for r in rows if r[1] == t) for t in tiers}
    pending = sum(1 for user, _t, panel, target in rows
                  if needs_change(user, panel, target))
    summary = ', '.join(f'{t} {n}' for t, n in counts.items())
    if not args.apply:
        print(f'dry run: {summary} keys, {pending} to change; --apply to update',
              file=out)
        return 0
    results = apply(db, xui, rows)
    failed = [chat_id for chat_id, ok in results if not ok]
    print(f'{summary} keys; updated {len(results) - len(failed)}, '
          f'panel update failed: {", ".join(failed) or "none"}', file=out)
    return 1 if failed else 0


if __name__ == '__main__':
    # Run as a file, sys.path starts at scripts/; the bot package is one
    # level up (/app in the container).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
