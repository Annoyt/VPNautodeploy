"""A protocol the user's own client stopped passing (IMPROVEMENT_PLAN F, C3).

The Clash profile health-checks every protocol it holds through its own
telemetry provider ``p-<proto>`` (E8); ``GET /probe/<token>/p-<proto>``
leaves a ``client_probe`` row only when the check got THROUGH that
protocol. DPIMonitor's R6 needs three such clients in one network before it
acts — on 2026-11-10 two clients sent telemetry at all — so a user whose
network blocks one protocol kept it in Cascade and Auto. Here the user's
own rows decide, for that user's next profile:

* the client checked in through OTHER cascade protocols at least
  ``PERSONAL_MIN_OTHER_ROWS`` times in the last ``PERSONAL_WINDOW_H`` hours
  (about two hours of an open app — silence is never a signal: a closed app
  or a sleeping phone leaves no rows on any protocol);
* and not once through this one, which it did pass within the last
  ``PERSONAL_SEEN_DAYS`` days — a protocol the client never passed (new in
  its profile, or never working for it) is not "stopped passing".

The result joins the network's demotions on the /sub path: the protocol
leaves Cascade and Auto (and sinks in the calls group), stays in the manual
pick, and its telemetry provider keeps checking it — the first row through
it again lifts the demotion at the next profile fetch.

Per user, not per device: the rows carry our egress address, not the
device's network, so a phone's blocked protocol is hidden on the same
user's laptop too. That costs a server choice, never connectivity: a
demotion of every protocol excludes nothing (SubscriptionService).
"""

import logging
from datetime import datetime, timedelta
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

PERSONAL_WINDOW_H = 3
PERSONAL_MIN_OTHER_ROWS = 12      # ≈ 2 h of other protocols' 10-min checks
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
            rows = conn.execute(
                "SELECT substr(grp, ?), SUM(ts >= ?), COUNT(*) FROM client_probe "
                "WHERE chat_id = ? AND ts >= ? AND substr(grp, 1, ?) = ? "
                "GROUP BY grp",
                (len(TELEMETRY_PREFIX) + 1, recent, str(chat_id), week,
                 len(TELEMETRY_PREFIX), TELEMETRY_PREFIX),
            ).fetchall()
    except Exception as e:
        logger.warning(f"personal demotion read failed for {chat_id}: {e}")
        return frozenset()
    recent_rows = {str(p): int(r or 0) for p, r, _w in rows}
    seen_week = {str(p) for p, _r, w in rows if w}
    dark = set()
    for proto in protocols:
        if recent_rows.get(proto, 0) or proto not in seen_week:
            continue
        others = sum(n for p, n in recent_rows.items() if p != proto and p in protocols)
        if others >= PERSONAL_MIN_OTHER_ROWS:
            dark.add(proto)
    return frozenset(dark)
