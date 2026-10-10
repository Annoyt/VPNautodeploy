"""RU-zone egress — Russian sites from abroad, for paid users.

Every regular protocol on entry is transit to exit: haproxy :8443
forwards Reality to exit, shadow-tls :443 forwards to exit:8444, hy2 and
the CDN ports are DNAT'ed to exit. So none of them gives a Russian IP —
a user abroad sees RU sites from exit's foreign address. This egress
terminates ON entry instead: a standalone sing-box container ``ru-exit``
(``scripts/setup_ru_exit.sh``) with a VLESS-Reality inbound on its own
port, leaving straight from entry's RU address. haproxy and 3x-ui are
not involved.

The client side is a separate profile on the same subscription token,
``/sub/<token>?mode=abroad`` (``SubscriptionService.
build_abroad_singbox_config``): RU domains/IPs → ru-exit, everything else
direct — the reverse of the home profile. In practice the client is
FlClash with ``&format=clash`` (Hiddify drops a profile's routing rules
and would send everything through RU); the bot hands that link out via
the "🇷🇺 RU-зона" button (``RuZoneHandler``) on the main menu, the key
card and /sub.

Who gets it — paid users only (``is_eligible``): status 'paid' with a
paid-until date that has not passed (a lapsed payer KEEPS status 'paid':
the panel's expiryTime is what switches the main key off, and this
egress never asks the panel), or 'support_topic' for a payer with an
open ticket — told apart from a demo user with one by the paid-until
date (demo users never have one; ``billing.grant_paid_access`` writes
it) or, for a grant without a date, by ``previous_state``. That is
stricter than ``MyKeyAnswerHandler.PAID_USER_STATUSES``, which lets a
demo user keep the paid protocols while a ticket is open. The button,
``/sub?mode=abroad`` (403 for anyone else) and the server's user list
all ask this one rule.

The server's user list follows the bot DB: ``setup_ru_exit.sh`` installs
a systemd timer on entry that runs ``setup_ru_exit.sh --sync`` every 2
minutes. ``--sync`` reads the list from this module's CLI —
``python3 -P bot/services/ru_exit.py users`` inside the vpn-bot
container, JSON on stdout — and recreates the container only when the
list changed. A new payer gets in, a lapsed / revoked / banned one drops
out, within minutes. The CLI is why nothing from ``bot`` is imported at
the top of this module: run as a file it skips the ``bot.services``
package init, which costs ~1.2 s of the entry's single core per run.

Config lives in ``app_settings.ru_exit`` (JSON), written by the setup
script:

    {"port": 8445, "sni": "www.google.com",
     "pbk": "<reality public key>", "sid": "<short id>"}

``host`` is optional and defaults to ENTRY_NODE_IP. A ``chat_ids`` key
left over from the allowlist test is ignored.

Not done: the traffic never touches the x-ui panel, so it is NOT counted
against the quota (sing-box keeps per-user counters only with the v2ray
API, which the release image is built without).
"""

import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

SETTING_KEY = 'ru_exit'

# "🇷🇺 RU-зона" button (main menu, key card, /sub) → RuZoneHandler.
RU_ZONE_CALLBACK = 'ru_zone'

# The statuses that can hold the paid tier at all; ``is_eligible``
# narrows 'support_topic' down to payers.
RU_ZONE_STATUSES = ('paid', 'support_topic')

# sing-box parses a VLESS user id as a UUID: one malformed row would fail
# `sing-box check` and freeze the list for everyone.
_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)

# Settings.DB_PATH reads the same variable with the same default.
_DEFAULT_DB_PATH = '/etc/cascade-vpn/bot.db'


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _date_passed(expiry, now: datetime) -> bool:
    """``subscription_expiry`` (naive UTC ISO, as grant_paid_access writes
    it) lies in the past. Empty or unparseable = no limit, the reading of
    the paid-only hy2t auth gate in web_server."""
    if not expiry:
        return False
    try:
        exp = datetime.fromisoformat(str(expiry).strip())
    except (TypeError, ValueError):
        return False
    if exp.tzinfo is not None:
        exp = exp.astimezone(timezone.utc).replace(tzinfo=None)
    return exp < now


def _eligible(status, uuid, expiry, previous_state, now: datetime) -> bool:
    if not uuid:
        return False
    if status == 'paid':
        return not _date_passed(expiry, now)
    if status == 'support_topic':
        if expiry:
            return not _date_passed(expiry, now)
        return previous_state == 'paid'
    return False


def is_eligible(user, now: Optional[datetime] = None) -> bool:
    """May this user have the RU-zone? Never raises."""
    try:
        if user is None:
            return False
        return _eligible(
            getattr(user, 'status', None),
            getattr(user, 'uuid', None),
            getattr(user, 'subscription_expiry', None),
            getattr(user, 'previous_state', None),
            now or _utcnow(),
        )
    except Exception as e:
        logger.warning(f"ru_exit: eligibility check failed: {e}")
        return False


def parse_ru_exit(raw) -> Optional[dict]:
    """Normalised server config or None when unusable. Never raises."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning(f"ru_exit: bad JSON in app_settings[{SETTING_KEY}]: {e}")
        return None
    if not isinstance(data, dict):
        return None
    try:
        port = int(data.get('port') or 0)
    except (TypeError, ValueError):
        return None
    sni = str(data.get('sni') or '').strip()
    pbk = str(data.get('pbk') or '').strip()
    if not (0 < port < 65536 and sni and pbk):
        return None
    return {
        'host': str(data.get('host') or '').strip(),
        'port': port,
        'sni': sni,
        'pbk': pbk,
        'sid': str(data.get('sid') or '').strip(),
    }


def load_ru_exit(db) -> Optional[dict]:
    """The server config, None while the egress is not set up. Never raises."""
    try:
        raw = db.get_setting(SETTING_KEY) if db is not None else None
    except Exception as e:
        logger.warning(f"ru_exit: read of app_settings[{SETTING_KEY}] failed: {e}")
        return None
    return parse_ru_exit(raw)


def ru_exit_for(db, user) -> Optional[dict]:
    """The server config if this user may get the abroad profile, else
    None (not eligible, or the egress is not set up). Never raises."""
    if not is_eligible(user):
        return None
    return load_ru_exit(db)


def ru_zone_button_row(db, user) -> Optional[list]:
    """The keyboard row with the RU-zone button — only for an eligible
    user while the egress is set up. Never raises."""
    if not ru_exit_for(db, user):
        return None
    label = '🇷🇺 RU zone' if getattr(user, 'lang', None) == 'en' else '🇷🇺 RU-зона'
    return [{'text': label, 'callback_data': RU_ZONE_CALLBACK}]


def abroad_profile_url(config, user) -> Optional[str]:
    """The user's subscription URL for FlClash: the Clash variant of the
    abroad profile. Hiddify drops a profile's routing rules (it would
    send everything through RU), so the button hands out only this one."""
    from bot.services.subscription import SubscriptionService
    url = SubscriptionService(config).build_subscription_url(user)
    return f'{url}?mode=abroad&format=clash' if url else None


# ---------- the server's user list (setup_ru_exit.sh --sync) ----------

_SERVER_SQL = (
    "SELECT chat_id, uuid, status, subscription_expiry, previous_state "
    "FROM users WHERE uuid IS NOT NULL AND status IN ("
    + ', '.join('?' * len(RU_ZONE_STATUSES)) + ")"
)


def server_users(rows: Iterable, now: Optional[datetime] = None) -> list:
    """sing-box VLESS users for the eligible rows of
    ``(chat_id, uuid, status, subscription_expiry, previous_state)``.

    Sorted by name, one entry per uuid: an unchanged set renders
    byte-identically, so ``--sync`` recreates the container only on a
    real change. Rows without a chat_id or with a malformed uuid are
    skipped."""
    now = now or _utcnow()
    by_name = {}
    for chat_id, uuid, status, expiry, previous_state in rows:
        cid = str(chat_id or '').strip()
        uid = str(uuid or '').strip()
        if not cid or not _UUID_RE.match(uid):
            continue
        if _eligible(status, uid, expiry, previous_state, now):
            by_name[f'c{cid}'] = uid
    out, seen = [], set()
    for name in sorted(by_name):
        uid = by_name[name]
        if uid.lower() in seen:
            continue
        seen.add(uid.lower())
        out.append({'name': name, 'uuid': uid, 'flow': 'xtls-rprx-vision'})
    return out


def eligible_server_users(conn, now: Optional[datetime] = None) -> list:
    """``server_users`` over the users table of an open sqlite connection."""
    return server_users(conn.execute(_SERVER_SQL, RU_ZONE_STATUSES).fetchall(), now)


def _cli(argv) -> int:
    """``python3 -P bot/services/ru_exit.py users`` — the server's user
    list as JSON on stdout, read-only from ``$DB_PATH``."""
    if list(argv) != ['users']:
        print('usage: ru_exit.py users   (the ru-exit user list as JSON)',
              file=sys.stderr)
        return 2
    path = os.getenv('DB_PATH') or _DEFAULT_DB_PATH
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro',
                           uri=True, timeout=10)
    try:
        users = eligible_server_users(conn)
    finally:
        conn.close()
    print(json.dumps(users))
    return 0


if __name__ == '__main__':
    sys.exit(_cli(sys.argv[1:]))
