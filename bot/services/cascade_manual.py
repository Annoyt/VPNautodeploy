"""The operator's own demotions, with an expiry (IMPROVEMENT_PLAN F, C6).

``/cascade demote <proto> [ASN] [ttl]`` moves a protocol to the end of the
cascade exactly as a DPIMonitor demotion does — for everyone, or for one
network — and ``/cascade restore <proto> [ASN]`` takes it back. They live in
``app_settings.cascade_manual``, apart from the monitor's ``cascade_auto``:
written into the monitor's store, a hand demotion would be "restored" by the
monitor within the hour, because none of its rules fired for it.

    {"global": {"hy2": {"since": iso, "until": iso, "by": "admin:<id>"}},
     "asn": {"AS31133": {"reality": {...}}}}

Every entry carries ``until``; an expired one is ignored on read and dropped
on the next write. ``MyKeyAnswerHandler.get_auto_demotions`` unions these
with the monitor's, so /sub, the key card, the SOS kit and the cascade order
all honour them. Readers never raise (they run inside /sub). Writers read
strictly: a locked read taken for "nothing stored" would overwrite every
other live entry (the rule_lists hazard, AGENTS.md §31), and a write that
did not land raises instead of being reported as done.
"""

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

SETTING_KEY = 'cascade_manual'
DEFAULT_TTL = timedelta(hours=6)
MAX_TTL = timedelta(days=7)
_TTL_RE = re.compile(r'^(\d{1,4})\s*([mhd])$')


def parse_ttl(raw: Optional[str]) -> Optional[timedelta]:
    """``30m`` / ``6h`` / ``2d`` → timedelta (capped at ``MAX_TTL``); None
    for anything else."""
    m = _TTL_RE.match((raw or '').strip().lower())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        return None
    ttl = {'m': timedelta(minutes=n), 'h': timedelta(hours=n), 'd': timedelta(days=n)}[unit]
    return min(ttl, MAX_TTL)


def normalize_asn(raw) -> Optional[str]:
    """``AS31133`` / ``31133`` / ``as31133`` → ``AS31133``; None otherwise."""
    if not isinstance(raw, str):
        return None
    m = re.fullmatch(r'(?:AS)?(\d{1,10})', raw.strip().upper())
    return f'AS{m.group(1)}' if m else None


class ManualStoreError(RuntimeError):
    """cascade_manual could not be read before a write, or the write did
    not land."""


def _parse_ts(value) -> Optional[datetime]:
    """Naive UTC; an offset (a hand-edited ``…+00:00``) is converted, not
    compared against naive times (that raised and dropped every entry)."""
    try:
        ts = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is not None:
        ts = (ts - ts.utcoffset()).replace(tzinfo=None)
    return ts


def _read_strict(db) -> Optional[str]:
    """The stored value, None when there is none; raises ManualStoreError
    on a sqlite error (``get_setting`` folds that into "nothing stored")."""
    connect = getattr(db, '_connect', None)
    if connect is None:
        return db.get_setting(SETTING_KEY)
    try:
        with connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key = ?",
                               (SETTING_KEY,)).fetchone()
    except sqlite3.Error as e:
        raise ManualStoreError(f'{SETTING_KEY} не читается: {e}') from e
    return row[0] if row else None


def load(db, now: Optional[datetime] = None, *, strict: bool = False) -> dict:
    """The live entries: ``{'global': {proto: entry}, 'asn': {ASN: {proto:
    entry}}}``, expired and malformed ones left out. Never raises, unless
    ``strict`` (the writers): then an unreadable store raises
    ManualStoreError."""
    now = now or datetime.utcnow()
    out = {'global': {}, 'asn': {}}
    if strict:
        raw = _read_strict(db)
    else:
        try:
            raw = db.get_setting(SETTING_KEY) if db else None
        except Exception as e:
            logger.warning(f"cascade_manual read failed: {e}")
            return out
    if not isinstance(raw, str) or not raw.strip():
        return out
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return out
    if not isinstance(data, dict):
        return out

    def live(entries) -> dict:
        kept = {}
        if not isinstance(entries, dict):
            return kept
        for proto, entry in entries.items():
            if not isinstance(proto, str) or not isinstance(entry, dict):
                continue
            until = _parse_ts(entry.get('until'))
            if until is None or until <= now:
                continue
            kept[proto] = entry
        return kept

    out['global'] = live(data.get('global'))
    asn_part = data.get('asn')
    if isinstance(asn_part, dict):
        for asn, entries in asn_part.items():
            norm = normalize_asn(asn)
            kept = live(entries)
            if norm and kept:
                out['asn'][norm] = kept
    return out


def demoted_for(db, asn: Optional[str], now: Optional[datetime] = None) -> set:
    """The protocols demoted by hand for ``asn`` (global ∪ that ASN's)."""
    manual = load(db, now)
    out = set(manual['global'])
    norm = normalize_asn(asn) if asn else None
    if norm:
        out |= set(manual['asn'].get(norm, {}))
    return out


def _save(db, manual: dict) -> None:
    ok = db.set_setting(SETTING_KEY, json.dumps(manual, ensure_ascii=False, sort_keys=True))
    if ok is False:
        raise ManualStoreError(f'{SETTING_KEY} не записался')


def demote(db, proto: str, asn: Optional[str], ttl: timedelta, by: str,
           now: Optional[datetime] = None) -> dict:
    """Add (or extend) one entry; returns it. Expired entries go on the way.
    Raises ManualStoreError when the store cannot be read or written."""
    now = now or datetime.utcnow()
    manual = load(db, now, strict=True)
    entry = {'since': now.isoformat(timespec='seconds'),
             'until': (now + ttl).isoformat(timespec='seconds'), 'by': by}
    if asn:
        manual['asn'].setdefault(asn, {})[proto] = entry
    else:
        manual['global'][proto] = entry
    _save(db, manual)
    return entry


def restore(db, proto: str, asn: Optional[str],
            now: Optional[datetime] = None) -> Tuple[bool, dict]:
    """Drop one entry; ``(was_there, manual_after)``. Raises
    ManualStoreError when the store cannot be read or written."""
    manual = load(db, now, strict=True)
    scope = manual['asn'].get(asn, {}) if asn else manual['global']
    was = scope.pop(proto, None) is not None
    if asn and asn in manual['asn'] and not manual['asn'][asn]:
        manual['asn'].pop(asn)
    if was:
        _save(db, manual)
    return was, manual


def clear(db) -> dict:
    """Drop every hand demotion (``/cascade reset``); returns what was live."""
    before = load(db)
    if db.set_setting(SETTING_KEY, '{}') is False:
        raise ManualStoreError(f'{SETTING_KEY} не записался')
    return before
