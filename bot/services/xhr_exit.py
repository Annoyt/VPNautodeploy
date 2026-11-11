"""VLESS + XHTTP + Reality (``xhr``) on an xray of its own on exit
(IMPROVEMENT_PLAN F, E3).

The 3x-ui panel cannot host it. The relational panel keeps ONE flow per
client and gives that Vision flow to every VLESS inbound of the client, and
xray rejects a Vision-flow account whose client sends no flow with TLS
inside ("account is rejected since the client flow is empty"). XHTTP carries
no Vision flow, so a panel inbound would reject every user (AGENTS.md §51;
the CDN inbound is VMess for the same reason).

So the server is the container ``xhr-exit`` on EXIT
(``scripts/setup_xhr_exit.sh``): xray pinned by digest and below 26.7.11
(mihomo does not keep Reality compatible past it), flow-less clients,
Reality keys of its own, reached through entry's haproxy :8443 by an SNI
route ``XHR_SNI`` → exit with PROXY protocol v2. A root timer on exit pulls
``GET /api/xhr/users`` (``users_payload``) every 2 minutes and recreates the
container when the list changed.

* **Who:** the users whose profile carries ``xhr`` (switched on in the
  cascade — it is opt-in, ``OPT_IN_PROTOCOLS`` — and the user's tier gets
  it, ``PROTOCOL_TIER``), narrowed to the rule the RU-zone and SSH exits
  ask too: ``ru_exit.is_eligible`` — paid and not lapsed, or a payer's open
  ticket. The sync on exit then leaves out whom the panel has switched off.
  The profile also carries xhr for a lapsed payer (still status ``paid``)
  and for a demo user with an open ticket (``PAID_USER_STATUSES``); their
  xhr entry fails like a lapsed key does everywhere, and the cascade moves
  on. The other way round never happens: everyone listed has xhr in their
  profile.
* **What:** ``[{"uuid"}]``. VLESS checks the uuid itself, so unlike the SSH
  feed no hash can stand in for it. It travels the exit → entry leg the
  bot's panel reads (``XUI_API_URL``) already use for every uuid.
* **server:** what the profiles carry (sni, Reality public key, short id,
  XHTTP path), read off the builder. The sync on exit compares it with its
  own keys and fails loudly when the bot hands out other ones (an unset
  ``XHR_PUBLIC_KEY`` falls back to the main inbound's key).

A read error raises: the endpoint answers 503, never an empty list (the
sync would drop every user).

Traffic: the sync on exit reads xray's per-user counters and adds them to
the user's ``client_traffics`` row in the panel's x-ui.db, the quota every
panel protocol shares, and keeps out of the config whom the panel has
switched off (AGENTS.md §51). The RU-zone and SSH exits are still
unmetered, which is why they stay paid-only.
"""

import logging
import re
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional

logger = logging.getLogger(__name__)

PROTOCOL = 'xhr'

# xray reads a VLESS id that is not a UUID as the name of a UUIDv5: a
# malformed row would become some other id, not an error. Skip it.
_UUID_RE = re.compile(
    r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
)

# Any well-formed uuid: the builder needs one to say what it would hand out.
_SAMPLE_UUID = '00000000-0000-4000-8000-000000000000'


def profile_server(config) -> Optional[dict]:
    """What a profile's xhr proxy carries about the server — ``sni``, the
    Reality public key ``pbk`` and short id ``sid``, the XHTTP ``path`` —
    or None while the builders hand out no xhr (XHR_SNI, ENTRY_NODE_IP or a
    public key empty). Read off ``_build_xhr`` itself, so the two cannot
    drift apart."""
    from bot.services.subscription import SubscriptionService
    ob = SubscriptionService(config)._build_xhr(_SAMPLE_UUID, 'xhr')
    if ob is None:
        return None
    tls = ob.get('tls') or {}
    reality = tls.get('reality') or {}
    return {
        'sni': tls.get('server_name') or '',
        'pbk': reality.get('public_key') or '',
        'sid': reality.get('short_id') or '',
        'path': (ob.get('transport') or {}).get('path') or '',
    }


class _Settings:
    """``get_setting`` over values read once, strictly: the cascade code
    asks ``db.get_setting``, which takes a failed read for "no row"."""

    def __init__(self, values: dict):
        self._values = values

    def get_setting(self, key, default=None):
        return self._values.get(key, default)


def _read(db):
    """The cascade setting and every keyed user, in one connection. Raises
    on any sqlite error."""
    from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
    conn = db._connect()
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?",
                           (MK.SETTING_KEY,)).fetchone()
        users = conn.execute(
            "SELECT uuid, status, subscription_expiry, previous_state FROM users "
            "WHERE uuid IS NOT NULL AND uuid != ''").fetchall()
    finally:
        conn.close()
    settings = {MK.SETTING_KEY: row[0]} if row else {}
    return _Settings(settings), [tuple(u) for u in users]


def _carried_by(settings: _Settings) -> dict:
    """``{status: profile carries xhr}`` — ``get_cascade_order``'s own answer
    (the cascade's enabled set, then the tier filter). Reordering layers
    (per-ASN / country orders, lockdown, demotions) never drop a protocol,
    so the status alone decides."""
    from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK

    def carries(status):
        order = MK.get_cascade_order(settings, user=SimpleNamespace(status=status),
                                     apply_auto=False, apply_lockdown=False)
        return PROTOCOL in order
    return {s: carries(s) for s in ('demo', 'paid', 'support_topic')}


def users_payload(db, now: Optional[datetime] = None) -> dict:
    """``{"in_cascade", "users": [{"uuid"}]}`` for the exit's sync; raises
    on a read error."""
    from bot.services.ru_exit import is_eligible
    settings, rows = _read(db)
    carried = _carried_by(settings)
    seen, users = set(), []
    for uuid, status, expiry, previous in rows:
        if not carried.get(status):
            continue
        uuid = str(uuid).strip().lower()
        if not _UUID_RE.match(uuid) or uuid in seen:
            continue
        user = SimpleNamespace(uuid=uuid, status=status, subscription_expiry=expiry,
                               previous_state=previous)
        if not is_eligible(user, now):
            continue
        seen.add(uuid)
        users.append({'uuid': uuid})
    users.sort(key=lambda u: u['uuid'])
    return {'in_cascade': any(carried.values()), 'users': users}
