"""Client-side success per protocol × operator — IMPROVEMENT_PLAN C2 (rest).

The entry-host probes (``outbound_health``, AGENTS.md §28) look from one
network, the entry VPS's. What works for people on MegaFon or MTS is
visible only from their own clients: the FlClash profile's per-protocol
health checks (E8) leave ``client_probe`` rows (§32) —
``(chat_id, grp, ts, src_ip)`` = this client passed a health check through
a tunnel at ``ts``.

``grp = 'p-<proto>'`` is one protocol (reality, hy2, hy2t, ws, stls, de).
Any other group (``emergency``, ``mirror-<n>``) is a CHANNEL: proof that the
client is alive, never a protocol column and never evidence that a
protocol failed. A client's operator is ``users.last_asn`` (blank, NULL or
no users row → the unknown group, ``asn: None``), its country
``users.last_country``.

Per ASN, over the window:

* ``clients`` — distinct clients with ANY row (channels too);
* ``probed``  — of them, the ones with at least one protocol row;
* per protocol P:

  * ``alive`` — clients with a ``p-P`` row;
  * ``dead``  — clients P is OFFERED to that have rows on other protocols
    but none on P: their client is up and checking, P does not answer;
  * ``rate``  — ``alive / (alive + dead)``; ``None`` when both are 0.

"Offered" is the tier gate the profile is built with: ``PROTOCOL_TIER`` /
``PAID_USER_STATUSES`` of ``MyKeyAnswerHandler`` and the DE reserve's
``FALLBACK_ALLOWED_STATUSES``. A demo profile carries no Reality, no Turbo
Hy2 and no reserve, so a demo client without those rows is not a failure;
without the gate every paid-only protocol would read as the paid share of
the base (most of it is demo). Free-tier protocols, and any protocol this
code does not know, are offered to everyone. ``users.status`` is the
CURRENT status: a client upgraded inside the window starts probing its new
protocols only after its next profile refresh.

The matrix comes from ONE query — a row per (client, group) in the window,
joined to users — never a query per operator or per protocol.
"""

import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from bot.services.fallback_node import FALLBACK_ALLOWED_STATUSES

# The windows the dashboard offers, in hours. Anything else is refused:
# the query scans the window, so an arbitrary window is an arbitrary scan.
WINDOWS_H = (1, 6, 24, 168)
DEFAULT_WINDOW_H = 24
_WINDOW_ARGS = {str(h): h for h in WINDOWS_H}

PROTOCOL_PREFIX = 'p-'
_PROTOCOL_RE = re.compile(r'[a-z0-9][a-z0-9_-]{0,31}')
# Column order for the protocols the E8 contract names; anything else that
# shows up in the data follows, alphabetically.
PROTOCOL_ORDER = ('reality', 'hy2', 'hy2t', 'ws', 'stls', 'de')

# Caps, like the other admin reads: (client, group) pairs read (~85
# clients × ~8 groups today) and operator rows returned. The total line
# is computed over every pair read, not only over the rows returned.
MAX_PAIRS = 20000
MAX_ROWS = 200

# client_probe.ts is sqlite's CURRENT_TIMESTAMP: UTC with a SPACE.
_TS_FORMAT = '%Y-%m-%d %H:%M:%S'

PAIRS_SQL = (
    "SELECT p.chat_id, p.grp, p.last_ts, u.last_asn, u.last_country, u.status "
    "FROM (SELECT chat_id, grp, MAX(ts) AS last_ts FROM client_probe "
    "WHERE ts >= ? GROUP BY chat_id, grp LIMIT ?) AS p "
    "LEFT JOIN users AS u ON u.chat_id = p.chat_id"
)


def parse_window(raw: Optional[str]) -> Optional[int]:
    """The ``hours`` query argument: absent → DEFAULT_WINDOW_H; exactly one
    of WINDOWS_H written as a plain integer → it; anything else → None
    (the endpoint answers 400 rather than guess a window)."""
    if raw is None:
        return DEFAULT_WINDOW_H
    return _WINDOW_ARGS.get(raw)


def window_start(hours: int, now: Optional[datetime] = None) -> str:
    """The oldest ``ts`` inside the window, written the way client_probe
    writes it. The strings are compared, and an isoformat cutoff ('…T…')
    sorts after every row of its own day (§32). A naive ``now`` is UTC."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc)
    return (now - timedelta(hours=hours)).strftime(_TS_FORMAT)


def protocol_of(grp) -> Optional[str]:
    """``'p-reality'`` → ``'reality'``; a channel (``emergency``,
    ``mirror-2``) or anything malformed → None."""
    if not isinstance(grp, str) or not grp.startswith(PROTOCOL_PREFIX):
        return None
    name = grp[len(PROTOCOL_PREFIX):]
    return name if _PROTOCOL_RE.fullmatch(name) else None


def offer_rules() -> dict:
    """``{protocol: statuses it is offered to}``. A protocol missing from
    the map (free tier, or one this code does not know) is offered to
    every client."""
    from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
    paid = frozenset(MK.PAID_USER_STATUSES)
    rules = {p: paid for p, tier in MK.PROTOCOL_TIER.items() if tier != 'free'}
    rules['de'] = frozenset(FALLBACK_ALLOWED_STATUSES)
    return rules


def _norm_asn(asn) -> Optional[str]:
    """``' as31133 '`` → ``'AS31133'``; blank / NULL → None."""
    if asn is None:
        return None
    return str(asn).strip().upper() or None


def _later(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _new_group(asn, protocols) -> dict:
    return {
        'asn': asn,
        'country': None,
        'clients': 0,
        'probed': 0,
        'last_ts': None,
        'protocols': {
            p: {'alive': 0, 'dead': 0, 'rate': None, 'last_ts': None}
            for p in protocols
        },
        '_countries': Counter(),
    }


def _add(group: dict, client: dict, protocols: list, rules: dict) -> None:
    group['clients'] += 1
    group['last_ts'] = _later(group['last_ts'], client['last_ts'])
    if client['country']:
        group['_countries'][client['country']] += 1
    seen = client['protos']
    if not seen:
        return  # channels only: alive, but no word on any protocol
    group['probed'] += 1
    for p in protocols:
        cell = group['protocols'][p]
        if p in seen:
            cell['alive'] += 1
            cell['last_ts'] = _later(cell['last_ts'], seen[p])
        elif p not in rules or client['status'] in rules[p]:
            cell['dead'] += 1


def _finish(group: dict) -> dict:
    countries = group.pop('_countries')
    if countries:
        group['country'] = min(countries.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    for cell in group['protocols'].values():
        answered = cell['alive'] + cell['dead']
        cell['rate'] = cell['alive'] / answered if answered else None
    return group


def summarize(pairs: Iterable, *, rules: Optional[dict] = None,
              max_rows: Optional[int] = None) -> dict:
    """The matrix from ``(chat_id, grp, last_ts, last_asn, last_country,
    status)`` rows, one per (client, group) in the window. Pure.

    Returns ``{protocols, rows, rows_total, total}``: ``protocols`` — the
    protocols that occurred, in column order; ``rows`` — one per ASN, the
    busiest first and the unknown group last, at most ``max_rows`` of
    ``rows_total``; ``total`` — the same counts over every client (no
    ``asn`` / ``country``).
    """
    if rules is None:
        rules = offer_rules()
    if max_rows is None:
        max_rows = MAX_ROWS
    clients = {}
    for chat_id, grp, last_ts, asn, country, status in pairs:
        c = clients.get(chat_id)
        if c is None:
            c = clients[chat_id] = {
                'asn': _norm_asn(asn),
                'country': (str(country).strip().upper() or None) if country else None,
                'status': status or '',
                'last_ts': None,
                'protos': {},
            }
        c['last_ts'] = _later(c['last_ts'], last_ts)
        proto = protocol_of(grp)
        if proto is not None:
            c['protos'][proto] = _later(c['protos'].get(proto), last_ts)

    occurred = {p for c in clients.values() for p in c['protos']}
    protocols = ([p for p in PROTOCOL_ORDER if p in occurred]
                 + sorted(p for p in occurred if p not in PROTOCOL_ORDER))
    groups = {}
    total = _new_group(None, protocols)
    for c in clients.values():
        group = groups.get(c['asn'])
        if group is None:
            group = groups[c['asn']] = _new_group(c['asn'], protocols)
        _add(group, c, protocols, rules)
        _add(total, c, protocols, rules)

    ordered = sorted(groups.values(),
                     key=lambda g: (g['asn'] is None, -g['clients'], g['asn'] or ''))
    total = _finish(total)
    del total['asn'], total['country']
    return {
        'protocols': protocols,
        'rows': [_finish(g) for g in ordered[:max_rows]],
        'rows_total': len(ordered),
        'total': total,
    }


def collect(db, hours: int, *, now: Optional[datetime] = None,
            max_pairs: Optional[int] = None,
            max_rows: Optional[int] = None) -> dict:
    """Read the window (one query) and build the matrix. ``truncated`` —
    more than ``max_pairs`` (client, group) pairs: the counts are partial.
    Blocking: the endpoint runs it on a worker thread."""
    if max_pairs is None:
        max_pairs = MAX_PAIRS
    since = window_start(hours, now)
    conn = db._connect()
    try:
        pairs = [tuple(r) for r in
                 conn.execute(PAIRS_SQL, (since, max_pairs + 1)).fetchall()]
    finally:
        conn.close()
    out = summarize(pairs[:max_pairs], max_rows=max_rows)
    out.update(hours=hours, since=since, truncated=len(pairs) > max_pairs)
    return out
