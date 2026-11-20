"""The NekoVPN app's network diagnostics: ``POST /api/neko/netstat``
(docs/NEKO6_PLAN.md, contract D2).

The app reports, from where it is, whether our channel works: ``drop`` when
it does not, ``ok`` once an hour while the VPN is on. Stage 1 (every app
whose signed settings do not say ``flags.netstat: false``) sends the
network, the operator and the signal; stage 2 (the user's own switch,
«Помогать искать блокировки») adds the serving cell and a 5-character
geohash, about 5 km.

Nothing kept names the user. ``WebAppServer.handle_neko_netstat``:

- checks the subscription token from ``X-Neko-Token`` against the users
  table (an active key) and drops it; the rate limiter holds a keyed hash
  of it, in memory only;
- never reads the caller's address or a forwarding header;
- keeps the request out of aiohttp's logs (bot/utils/unlogged.py): the
  access line carries the address.

``netstat_events`` has no token, chat_id or address column. Its ids are
random, so the rows of one request are not a run of ids that would tie one
device's events together. ``ts`` is floored to 10 minutes here as on the
phone. Rows older than 14 days go daily (``NotificationService``, job
``netstat_cleanup``); an event already that old, or more than an hour
ahead of this clock, is valid but not stored.

The table is created here on first use, as mail_intake creates
``email_requests``; ``init_db()`` does not know it.
"""

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from collections import deque
from typing import Optional

from bot.services.neko_client import KEY_STATUSES

logger = logging.getLogger(__name__)

TOKEN_HEADER = 'X-Neko-Token'
# A subscription is active for the statuses that hold a key: the gate of
# /sub, /open and the app's SOS. A token is only ever derived from a uuid.
ACTIVE_STATUSES = KEY_STATUSES

MAX_BODY = 64 * 1024
MAX_EVENTS = 100
BUCKET_S = 600                        # ts is floored to 10 minutes
RETENTION_DAYS = 14
RETENTION_S = RETENTION_DAYS * 86400
FUTURE_SKEW_S = 3600                  # a phone clock up to an hour ahead
TS_LIMIT = 1 << 32                    # unix seconds; a millisecond stamp is over it
RATE_LIMIT = 30                       # requests per token...
RATE_WINDOW_S = 3600                  # ...an hour

KINDS = frozenset({'drop', 'ok'})
NETS = frozenset({'wifi', 'cell', 'other'})
RATS = frozenset({'gsm', 'umts', 'lte', 'nr', 'unknown'})
TOP_FIELDS = frozenset({'v', 'app', 'events'})
EVENT_FIELDS = frozenset({'ts', 'kind', 'net', 'mcc', 'mnc', 'rat', 'level',
                          'ru', 'ours', 'proto', 'cell', 'area'})
REQUIRED_FIELDS = ('ts', 'kind', 'net', 'ours')
# The serving cell (stage 2), one set of names for every radio: tac = TAC
# or LAC, cid = the cell identity (NR's NCI has 36 bits), pci = PCI, PSC or
# BSIC, earfcn = the channel (ARFCN, UARFCN, EARFCN or NR-ARFCN). Android's
# "unavailable" (Integer.MAX_VALUE) is out of range: the app sends null.
CELL_RANGES = {
    'tac': (0, (1 << 24) - 1),
    'cid': (0, (1 << 36) - 1),
    'pci': (0, 1007),
    'earfcn': (0, 3279165),
}
MAX_PROTOCOLS = 16

_APP = re.compile(r'[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}'
                  r'(?:-[0-9A-Za-z.]{1,24})?(?:\+[0-9A-Za-z.]{1,24})?')
_MCC = re.compile(r'[0-9]{3}')
_MNC = re.compile(r'[0-9]{2,3}')
_GEOHASH5 = re.compile(r'[0-9b-hjkmnp-z]{5}')    # base32 without a, i, l, o
_PROTOCOL = re.compile(r'[a-z][a-z0-9_-]{0,15}')

COLUMNS = ('id', 'ts', 'kind', 'net', 'mcc', 'mnc', 'rat', 'level', 'ru',
           'ours', 'proto', 'cell_tac', 'cell_cid', 'cell_pci', 'cell_earfcn',
           'area', 'app')

_SCHEMA = '''
    CREATE TABLE IF NOT EXISTS netstat_events (
        id INTEGER PRIMARY KEY,      -- random, never a sequence
        ts INTEGER NOT NULL,         -- unix seconds, floored to 10 minutes
        kind TEXT NOT NULL,          -- drop | ok
        net TEXT NOT NULL,           -- wifi | cell | other
        mcc TEXT,
        mnc TEXT,
        rat TEXT,                    -- gsm | umts | lte | nr | unknown
        level INTEGER,               -- signal, 0-4
        ru INTEGER,                  -- a Russian site opened outside the tunnel: 1 / 0
        ours INTEGER NOT NULL,       -- our channel works: 1 / 0
        proto TEXT,                  -- {"hy2": false, ...}, the protocol check
        cell_tac INTEGER,
        cell_cid INTEGER,
        cell_pci INTEGER,
        cell_earfcn INTEGER,
        area TEXT,                   -- geohash, 5 characters
        app TEXT NOT NULL            -- the app's version
    )
'''
_INDEX = 'CREATE INDEX IF NOT EXISTS idx_netstat_events_ts ON netstat_events(ts)'


class BadBody(ValueError):
    """The body is not what D2 describes; the message names the field."""


# ---------- the body ----------

async def read_body(request, limit: int = MAX_BODY) -> Optional[bytes]:
    """The whole body, or None when it is over ``limit`` (by its
    Content-Length or by what arrives). Reads to the end: one read()
    returns only what has arrived so far."""
    if (request.content_length or 0) > limit:
        return None
    chunks, size = [], 0
    while True:
        chunk = await request.content.read(limit + 1 - size)
        if not chunk:
            return b''.join(chunks)
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise BadBody(f'duplicate field {key[:40]!r}')
        out[key] = value
    return out


def _no_constant(name):
    raise BadBody(f'{name} is not JSON')


def parse(raw: bytes):
    """JSON, strictly: UTF-8, no NaN or Infinity, no field twice."""
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise BadBody('not UTF-8') from None
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates,
                          parse_constant=_no_constant)
    except BadBody:
        raise
    except (ValueError, RecursionError):
        raise BadBody('not JSON') from None


def _only(obj: dict, fields, where: str) -> None:
    for key in obj:
        if key not in fields:
            raise BadBody(f'{where}: unknown field {key[:40]!r}')


def _one_of(value, allowed, where: str) -> str:
    if type(value) is not str or value not in allowed:
        raise BadBody(f'{where}: one of {", ".join(sorted(allowed))}')
    return value


def _digits(value, pattern, where: str, what: str) -> Optional[str]:
    if value is None:
        return None
    if type(value) is not str or not pattern.fullmatch(value):
        raise BadBody(f'{where}: {what} as a string, or null')
    return value


def _proto(value, where: str) -> Optional[str]:
    if value is None:
        return None
    if type(value) is not dict or len(value) > MAX_PROTOCOLS:
        raise BadBody(f'{where}: up to {MAX_PROTOCOLS} protocols, or null')
    for name, ok in value.items():
        if not _PROTOCOL.fullmatch(name) or type(ok) is not bool:
            raise BadBody(f'{where}: protocol names to true / false')
    return json.dumps(value, sort_keys=True, separators=(',', ':')) if value else None


def _cell(value, where: str) -> tuple:
    if value is None:
        return (None,) * len(CELL_RANGES)
    if type(value) is not dict:
        raise BadBody(f'{where}: an object, or null')
    _only(value, CELL_RANGES, where)
    out = []
    for key, (low, high) in CELL_RANGES.items():
        number = value.get(key)
        if number is not None and (type(number) is not int
                                   or not low <= number <= high):
            raise BadBody(f'{where}.{key}: {low}-{high}, or null')
        out.append(number)
    return tuple(out)


def _event(event, where: str) -> tuple:
    """One event as a row without the id and the app: (ts, kind, …, area)."""
    if type(event) is not dict:
        raise BadBody(f'{where}: an object')
    _only(event, EVENT_FIELDS, where)
    for key in REQUIRED_FIELDS:
        if key not in event:
            raise BadBody(f'{where}.{key}: required')
    ts = event['ts']
    if type(ts) is not int or not 0 <= ts < TS_LIMIT:
        raise BadBody(f'{where}.ts: unix time in seconds')
    kind = _one_of(event['kind'], KINDS, f'{where}.kind')
    net = _one_of(event['net'], NETS, f'{where}.net')
    mcc = _digits(event.get('mcc'), _MCC, f'{where}.mcc', '3 digits')
    mnc = _digits(event.get('mnc'), _MNC, f'{where}.mnc', '2-3 digits')
    rat = event.get('rat')
    if rat is not None:
        rat = _one_of(rat, RATS, f'{where}.rat')
    level = event.get('level')
    if level is not None and (type(level) is not int or not 0 <= level <= 4):
        raise BadBody(f'{where}.level: 0-4, or null')
    ru = event.get('ru')
    if ru is not None and type(ru) is not bool:
        raise BadBody(f'{where}.ru: true, false or null')
    ours = event['ours']
    if type(ours) is not bool:
        raise BadBody(f'{where}.ours: true or false')
    proto = _proto(event.get('proto'), f'{where}.proto')
    cell = _cell(event.get('cell'), f'{where}.cell')
    area = event.get('area')
    if area is not None and (type(area) is not str or not _GEOHASH5.fullmatch(area)):
        raise BadBody(f'{where}.area: a 5-character geohash, or null')
    return (ts - ts % BUCKET_S, kind, net, mcc, mnc, rat, level,
            None if ru is None else int(ru), int(ours), proto, *cell, area)


def validate(doc, now: float) -> list:
    """The rows to store, in COLUMNS order without the id. Raises BadBody
    when anything in the body is off. An event older than the retention
    or more than FUTURE_SKEW_S ahead of ``now`` is valid but not kept."""
    if type(doc) is not dict:
        raise BadBody('the body: an object')
    _only(doc, TOP_FIELDS, 'the body')
    if type(doc.get('v')) is not int or doc['v'] != 1:
        raise BadBody('v: 1')
    app = doc.get('app')
    if type(app) is not str or not _APP.fullmatch(app):
        raise BadBody('app: the app version, x.y.z-neko.n')
    events = doc.get('events')
    if type(events) is not list or not 1 <= len(events) <= MAX_EVENTS:
        raise BadBody(f'events: a list of 1-{MAX_EVENTS} events')
    oldest = int(now) - RETENTION_S
    newest = int(now) + FUTURE_SKEW_S
    rows = []
    for i, event in enumerate(events):
        row = _event(event, f'events[{i}]')
        if oldest <= row[0] <= newest:
            rows.append(row + (app,))
    return rows


# ---------- storage ----------

def _create(conn) -> None:
    conn.execute(_SCHEMA)
    conn.execute(_INDEX)


def ensure_table(db) -> None:
    conn = db._connect()
    try:
        _create(conn)
        conn.commit()
    finally:
        conn.close()


def store(db, rows: list) -> int:
    """Insert the rows of one request in one transaction, each under a
    random id. The number stored."""
    if not rows:
        return 0
    marks = ', '.join('?' * len(COLUMNS))
    conn = db._connect()
    try:
        _create(conn)
        conn.executemany(
            f'INSERT INTO netstat_events ({", ".join(COLUMNS)}) VALUES ({marks})',
            [(secrets.randbits(63),) + tuple(row) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()
    return len(rows)


def cleanup(db, now: Optional[float] = None) -> int:
    """Drop the rows older than RETENTION_DAYS; the number dropped."""
    cutoff = int(time.time() if now is None else now) - RETENTION_S
    conn = db._connect()
    try:
        _create(conn)
        dropped = conn.execute(
            'DELETE FROM netstat_events WHERE ts < ?', (cutoff,)).rowcount
        conn.commit()
        kept = conn.execute('SELECT COUNT(*) FROM netstat_events').fetchone()[0]
    finally:
        conn.close()
    logger.info(f'netstat cleanup: dropped {dropped} rows older than '
                f'{RETENTION_DAYS}d, {kept} kept')
    return dropped


# ---------- the rate limit ----------

# The limiter's keys are HMACs of the token under a key that lives in this
# process only: the limiter holds nothing a token or a user can be read
# back from, and a restart forgets it all.
_KEY_SECRET = secrets.token_bytes(32)


def key_of(token: str) -> str:
    return hmac.new(_KEY_SECRET, token.encode(), hashlib.sha256).hexdigest()


class RateLimiter:
    """At most ``limit`` requests a ``window`` (seconds) per key, in memory.
    A refused request is not counted. Keys idle for a whole window are
    dropped, so memory follows the subscriptions in use."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW_S,
                 clock=time.monotonic):
        self.limit = limit
        self.window = window
        self.clock = clock
        self._hits: dict = {}
        self._lock = threading.Lock()
        self._swept = clock()

    def check(self, key: str) -> float:
        """0 when the request may go ahead (it is counted then), else the
        seconds until it may."""
        now = self.clock()
        expired = now - self.window
        with self._lock:
            if now - self._swept >= self.window:
                self._hits = {k: h for k, h in self._hits.items()
                              if h and h[-1] > expired}
                self._swept = now
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= expired:
                hits.popleft()
            if len(hits) >= self.limit:
                return hits[0] - expired
            hits.append(now)
            return 0.0
