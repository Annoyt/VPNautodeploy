#!/usr/bin/env python3
"""Raise paid keys to the paid address limit (PAID_LIMIT_IP, 5 by default).

Runs inside the vpn-bot container:

    docker exec -w /app vpn-bot python3 scripts/paid_limit_ip.py           dry run
    docker exec -w /app vpn-bot python3 scripts/paid_limit_ip.py --apply   update

grant_paid_access sets the limit on every payment from now on; this is for
keys paid before it did. The panel's own value is read per key (it has
drifted from the bot's: unlimited 0 on keys the panel took in before the
camelCase fix), and both are set to billing.paid_limit_ip(): never below a
higher value an admin set, an admin's 0 (no limit) kept. Each change is
audited as admin_actions('owner', 'paid_limit_ip', <chat_id>, 'old->new').
"""

import argparse
import os
import sys

ACTION = 'paid_limit_ip'


def is_payer(user) -> bool:
    """Status 'paid', or 'support_topic' for a payer with a ticket open:
    told apart from a demo user with one by the paid-until date (demo
    users never have one) or by previous_state (ru_exit uses the same
    rule)."""
    status = getattr(user, 'status', '') or ''
    if status == 'paid':
        return True
    return status == 'support_topic' and bool(
        getattr(user, 'subscription_expiry', None)
        or getattr(user, 'previous_state', None) == 'paid')


def plan(db, config, panel_limit) -> list:
    """[(user, panel value, target)] for every payer with a key.
    ``panel_limit(email)`` is the panel's limitIp, None when unreadable."""
    from bot.services.billing import paid_limit_ip
    rows = []
    for user in db.get_all_users() or []:
        if user.uuid and user.email and is_payer(user):
            rows.append((user, panel_limit(user.email),
                         paid_limit_ip(user, config)))
    return rows


def needs_change(user, panel, target) -> bool:
    return user.limit_ip != target or panel != target


def apply(db, xui, rows) -> list:
    """Set every row that differs; [(chat_id, ok)]. The bot's value is
    saved first: a failed panel update leaves the row to the next run."""
    results = []
    for user, panel, target in rows:
        if not needs_change(user, panel, target):
            continue
        user.limit_ip = target
        db.save_user(user)
        ok = bool(xui.sync_client_settings_sync(user.email, {'limitIp': target}))
        if ok:
            db.log_admin_action('owner', ACTION, user.chat_id, f'{panel}->{target}')
        results.append((user.chat_id, ok))
    return results


def panel_reader(xui):
    def read(email):
        client = xui.get_client_sync(email)
        if not client:
            return None
        try:
            return int(client.get('limitIp') or 0)
        except (TypeError, ValueError):
            return None
    return read


def main(argv=None, *, db=None, xui=None, config=None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--apply', action='store_true', help='update (default: dry run)')
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

    rows = plan(db, config, panel_reader(xui))
    for user, panel, target in rows:
        mark = '  <- change' if needs_change(user, panel, target) else ''
        print(f'{user.chat_id}: bot={user.limit_ip} panel={panel} -> {target}{mark}',
              file=out)
    if not args.apply:
        print(f'dry run: {len(rows)} payers; --apply to update', file=out)
        return 0
    results = apply(db, xui, rows)
    failed = [chat_id for chat_id, ok in results if not ok]
    print(f'updated {len(results) - len(failed)}, panel update failed: '
          f'{", ".join(failed) or "none"}', file=out)
    return 1 if failed else 0


if __name__ == '__main__':
    # Run as a file, sys.path starts at scripts/; the bot package is one
    # level up (/app in the container).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
