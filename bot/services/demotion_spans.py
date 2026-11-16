"""When each protocol was demoted for everyone, kept for the inbound-drop
alert (AGENTS.md §48).

A demotion moves users off a protocol on purpose, so its exit inbound goes
quiet: the inbound-drop alert must not page for it, and those quiet blocks
must not drag the protocol's baseline down once it is back. The cascade's
stores hold only the demotions in force: ``cascade_manual`` (the operator's,
``since`` + ``until``) and ``cascade_auto`` (DPIMonitor's, ``since``). An
ended one leaves nothing usable behind: a hand demotion's TTL runs out
without an audit row, and the agent's ▶️ demote is audited under its own
action. So the alert samples both stores each time it recomputes (10 min)
and keeps what it saw in ``app_settings.cascade_demotion_spans``:

    {"ws": [["2026-11-13T14:10:00", null]],
     "reality": [["2026-11-02T09:00:00", "2026-11-02T15:00:00"]]}

Times are naive UTC, like the stores'; ``null`` = still demoted. Only
demotions for everyone count: an inbound counts every network's users, and
one network moving off a protocol does not take its count below a fifth.
Readers never raise: a hand-edited journal must not take the alert down.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from bot.services import cascade_manual

logger = logging.getLogger(__name__)

SETTING_KEY = 'cascade_demotion_spans'
# The alert's baseline looks up to 21 days (and its own 8-h window) back;
# an older span can no longer touch a block it reads.
KEEP_DAYS = 23

Spans = Dict[str, List[list]]


def _parse_ts(value) -> Optional[datetime]:
    return cascade_manual._parse_ts(value)


def _epoch(ts: datetime) -> float:
    return ts.replace(tzinfo=timezone.utc).timestamp()


def global_demotions(auto_raw, manual_raw, now: datetime) -> Dict[str, datetime]:
    """The protocols demoted for everyone right now (the monitor's and the
    operator's live entries), each with the earliest ``since`` the stores
    give: ``now`` when none parses or it lies ahead. Never raises."""
    from bot.services.dpi_monitor import normalize_auto
    found: Dict[str, datetime] = {}

    def note(proto, since) -> None:
        ts = _parse_ts(since) if since is not None else None
        if ts is None or ts > now:
            ts = now
        found[proto] = min(found.get(proto, ts), ts)

    try:
        parsed = json.loads(auto_raw) if isinstance(auto_raw, str) and auto_raw.strip() else {}
    except (TypeError, ValueError):
        parsed = {}
    for proto, entry in normalize_auto(parsed)['global'].items():
        note(proto, entry.get('since'))
    for proto, entry in cascade_manual.parse_live(manual_raw, now)['global'].items():
        note(proto, entry.get('since'))
    return found


def load_spans(raw) -> Spans:
    """The stored journal as ``{proto: [[start, end | None], ...]}`` in
    datetimes, oldest first; malformed spans are left out, and bad JSON
    reads as an empty journal (the next sample starts it over)."""
    out: Spans = {}
    if not isinstance(raw, str) or not raw.strip():
        return out
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(f"{SETTING_KEY}: bad JSON, the journal starts over")
        return out
    if not isinstance(data, dict):
        return out
    for proto, spans in data.items():
        if not isinstance(proto, str) or not isinstance(spans, list):
            continue
        kept = []
        for span in spans:
            if not isinstance(span, list) or len(span) != 2:
                continue
            start = _parse_ts(span[0])
            end = None if span[1] is None else _parse_ts(span[1])
            if start is None or (span[1] is not None and (end is None or end < start)):
                continue
            kept.append([start, end])
        if kept:
            out[proto] = sorted(kept, key=lambda s: s[0])
    return out


def update(spans: Spans, demoted: Dict[str, datetime], now: datetime,
           keep_days: int = KEEP_DAYS) -> Tuple[Spans, bool]:
    """One sample. A protocol demoted now and without an open span gets one
    from its ``since`` (or its last span reopens, when that ``since`` falls
    before the span ended: the bot was down, or the demotion was extended);
    an open span of a protocol no longer demoted ends now; spans that ended
    more than ``keep_days`` ago go. Returns ``(spans, changed)``."""
    out: Spans = {p: [list(s) for s in ss] for p, ss in spans.items()}
    changed = False
    for proto, since in demoted.items():
        ss = out.setdefault(proto, [])
        if ss and ss[-1][1] is None:
            continue
        since = min(since, now)
        if ss and since <= ss[-1][1]:
            ss[-1][0] = min(ss[-1][0], since)
            ss[-1][1] = None
        else:
            ss.append([since, None])
        changed = True
    for proto, ss in out.items():
        if proto not in demoted and ss and ss[-1][1] is None:
            ss[-1][1] = now
            changed = True
    cut = now - timedelta(days=keep_days)
    for proto in list(out):
        kept = [s for s in out[proto] if s[1] is None or s[1] >= cut]
        if len(kept) != len(out[proto]):
            changed = True
        if kept:
            out[proto] = kept
        else:
            del out[proto]
    return out, changed


def dump(spans: Spans) -> str:
    def iso(ts: Optional[datetime]):
        return None if ts is None else ts.isoformat(timespec='seconds')
    return json.dumps({p: [[iso(s), iso(e)] for s, e in ss]
                       for p, ss in sorted(spans.items())}, sort_keys=True)


def save(conn, spans: Spans) -> None:
    """Write the journal on the caller's sqlite connection (the alert's own)."""
    conn.execute(
        "INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = CURRENT_TIMESTAMP",
        (SETTING_KEY, dump(spans)),
    )
    conn.commit()


def covers(spans: Spans, proto: str, a_ts: float, b_ts: float, now_ts: float,
           tail_max_s: float) -> bool:
    """Does a span of ``proto``, or the tail after it, touch the interval
    ``[a_ts, b_ts)`` (epoch seconds)? The tail lasts as long as the demotion
    did, at most ``tail_max_s``: users come back to the protocol at their
    next profile fetch, and those who did not fetch during a short demotion
    never left."""
    for start, end in spans.get(proto, ()):
        s = _epoch(start)
        e = now_ts if end is None else _epoch(end)
        tail = min(tail_max_s, max(0.0, e - s))
        if s < b_ts and e + tail > a_ts:
            return True
    return False
