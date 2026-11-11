"""A protocol the user's own client stopped passing (IMPROVEMENT_PLAN F, C3).

The Clash profile health-checks every protocol it holds through its own
telemetry provider ``p-<proto>`` (E8); ``GET /probe/<token>/p-<proto>``
leaves a ``client_probe`` row only when the check got THROUGH that
protocol. DPIMonitor's R6 needs three such clients in one network before it
acts — on 2026-11-10 two clients sent telemetry at all — so a user whose
network blocks one protocol kept it in Cascade and Auto. Here the user's
own rows decide, for that user's next profile:

* the client checked in through OTHER cascade protocols in at least
  ``PERSONAL_MIN_CHECKS`` distinct 10-minute slots of the last
  ``PERSONAL_WINDOW_H`` hours (about two hours of an open app — its
  providers check every 10 min; silence is never a signal: a closed app or
  a sleeping phone leaves no rows on any protocol). Slots, not rows: a paid
  profile checks three other protocols at once, and twelve rows were then
  forty minutes;
* and not once through this one, which it did pass within the last
  ``PERSONAL_SEEN_DAYS`` days — a protocol the client never passed (new in
  its profile, or never working for it) is not "stopped passing".

The result joins the network's demotions on the /sub path: the protocol
leaves Cascade and Auto (and sinks in the calls group), stays in the manual
pick, and its telemetry provider keeps checking it — the first row through
it again lifts the demotion at the next profile fetch.

Per user, not per device: the rows carry our egress address, not the
device's network, so a phone's blocked protocol sinks on the same user's
laptop too. In NekoVPN / FlClash / Karing that costs a server choice, never
connectivity: the manual pick keeps it, and a demotion of every protocol
excludes nothing (SubscriptionService). Hiddify gets no personal demotions:
there a demotion hides the server, and the telemetry came from another
device (the /sub handler).
"""

import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

PERSONAL_WINDOW_H = 3
PERSONAL_MIN_CHECKS = 12          # distinct 10-min slots ≈ 2 h of an open app
PERSONAL_SLOT_S = 600
PERSONAL_SEEN_DAYS = 7
TELEMETRY_PREFIX = 'p-'           # subscription.CLASH_TELEMETRY_PREFIX
RESERVE = 'de'                    # the reserve is not a cascade protocol


def _space_ts(dt: datetime) -> str:
    # client_probe.ts is sqlite CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS');
    # an isoformat 'T' cutoff would sort every same-day row below it.
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def personal_dark(db, chat_id, cascade: Iterable[str],
                  now: Optional[datetime] = None) -> frozenset:
    """Protocols of ``cascade`` (the user's profile) this user's client
    stopped passing. Never raises: an unreadable table demotes nothing."""
    protocols = [p for p in (cascade or ()) if isinstance(p, str) and p != RESERVE]
    if not chat_id or len(protocols) < 2:
        return frozenset()
    now = now or datetime.utcnow()
    recent = _space_ts(now - timedelta(hours=PERSONAL_WINDOW_H))
    week = _space_ts(now - timedelta(days=PERSONAL_SEEN_DAYS))
    try:
        with db._connect() as conn:
            seen_week = {str(r[0]) for r in conn.execute(
                "SELECT DISTINCT substr(grp, ?) FROM client_probe "
                "WHERE chat_id = ? AND ts >= ? AND substr(grp, 1, ?) = ?",
                (len(TELEMETRY_PREFIX) + 1, str(chat_id), week,
                 len(TELEMETRY_PREFIX), TELEMETRY_PREFIX),
            )}
            slots_rows = conn.execute(
                "SELECT DISTINCT substr(grp, ?), "
                f"CAST(strftime('%s', ts) AS INTEGER) / {PERSONAL_SLOT_S} "
                "FROM client_probe "
                "WHERE chat_id = ? AND ts >= ? AND substr(grp, 1, ?) = ?",
                (len(TELEMETRY_PREFIX) + 1, str(chat_id), recent,
                 len(TELEMETRY_PREFIX), TELEMETRY_PREFIX),
            ).fetchall()
    except Exception as e:
        logger.warning(f"personal demotion read failed for {chat_id}: {e}")
        return frozenset()
    slots: dict = {}
    for proto, slot in slots_rows:
        slots.setdefault(str(proto), set()).add(slot)
    dark = set()
    for proto in protocols:
        if slots.get(proto) or proto not in seen_week:
            continue
        others = set()
        for p, s in slots.items():
            if p != proto and p in protocols:
                others |= s
        if len(others) >= PERSONAL_MIN_CHECKS:
            dark.add(proto)
    return frozenset(dark)
