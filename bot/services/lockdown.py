"""LOCKDOWN — the whitelist / "sovereign internet" profile (IMPROVEMENT_PLAN B1 + B5).

Why this exists
---------------
The normal cascade is tuned for DPI: one protocol gets profiled and
banned, the rest keep working, DPIMonitor sinks the dead one to the
tail. A regional shutdown is a different animal: the network passes
ONLY allowed destinations (RU domains, banks, госуслуги, a few CDNs)
and rejects everything else by default. Under that regime every
transport that connects to OUR entry IP dies at once — Reality :8443,
ShadowTLS :443 (a real-looking SNI does not help when the destination
IP itself is not on the list), both hysteria instances (UDP goes
first) — and the only thing that survives is the traffic whose
destination IS whitelisted: the VMess+httpupgrade path through
Cloudflare (``ws``), whose destination is a CF anycast IP that half of
RU-net also sits behind. So the answer is not "hide the protocol
better" but "put the fronted transport first and stop trusting the
local resolver" — within a probe cycle or two, not when the operator
wakes up.

What lockdown changes
---------------------
  * cascade — ``MyKeyAnswerHandler.get_cascade_order`` projects the
    operator's order onto ``LOCKDOWN_ORDER`` (fronted → TCP-direct →
    UDP; UDP dies first under throttling AND whitelists) after the
    base order, before the DPIMonitor partition and the tier filter.
    Today: paid ('ws','stls','reality','hy2','hy2t'), demo
    ('ws','stls','hy2'). Operator override: app_settings
    ``cascade_lockdown``.
  * subscription DNS (B5) — ``SubscriptionService.build_singbox_config
    (lockdown=True)``: every lookup goes through the tunnel (``final:
    remote``), only Clash "Direct" mode keeps the local resolver. The
    ISP resolver is the first thing poisoned under a whitelist, and a
    poisoned answer for the CF host takes down the one transport that
    still works. Route rules are NOT touched: the RU-direct TCP bypass
    is exactly what keeps the whitelisted sites working, and the
    UDP/calls path is unchanged.
  * NOT changed here (follow-ups): STLS_SNI / Reality dest to a
    whitelisted domain (B2 — shadow-tls, haproxy and the panel must
    move in lockstep), a second CDN front (B3), broadcasting users
    automatically (sing-box clients refresh /sub within ~6 h and
    urltest already avoids dead outbounds; ``/broadcast`` by hand).

Detector (mode 'auto')
----------------------
Runs inside ``DPIMonitor.run_once`` on the probe signals the monitor
already collected — one SQL pass, one 10-min tick. The probes run from
the ENTRY host (a RU VPS) through probe-proxy to exit, so their vantage
is "a RU network reaching a foreign IP" — the very hop a whitelist
kills. Signature::

    >= 2 DIRECT protocols measured, ALL measured DIRECT protocols DARK,
    ws measured and NOT dark

Two consecutive evaluations (~20 min) → active. Recovery: >= 2 DIRECT
protocols alive for 12 consecutive evaluations (~2 h) → inactive, but
ONLY when the detector was the one that switched it on (``by`` starts
with ``auto:``) — a manual ``/lockdown on`` is undone by the operator,
never by a probe. Ambiguity freezes the streaks (no count, no event):
stale probes or a failed probe collector; ws dark (from this vantage an
upstream outage — probe-proxy, the entry→exit link, exit itself — looks
exactly like a shutdown, and ``alert_manager`` already owns that
incident); fewer than two direct protocols measured; a half-dark
picture. ``mode`` 'on' / 'off' pins ``active``: the detector keeps
counting so ``/lockdown`` can show the evidence, but never flips.

Storage (app_settings, JSON strings)
------------------------------------
  lockdown_mode      {"mode": "auto"|"on"|"off", "active": bool,
                      "since": iso|null, "by": "admin:<id>" |
                      "auto:probe_signature" | "system", "reason": str,
                      "streak_on": int, "streak_off": int,
                      "last_change": iso|null}
                     missing / bad JSON → mode 'auto', active false.
  cascade_lockdown   JSON list of protocol names — the operator's own
                     lockdown order; unknown names dropped, empty →
                     LOCKDOWN_ORDER.

Every reader here is tolerant (bad JSON → defaults, never raises):
``is_lockdown_active`` runs inside /sub and the key card, where an
exception takes the user's config down with it — the 2026-09-01 lesson
is that the failure mode to avoid is the silent one, and "not active"
is loud in ``/lockdown``.
"""

import html
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---- protocol classes --------------------------------------------------------
# Fronted first, then TCP-direct, UDP last: under throttling and under a
# whitelist UDP is what goes first, and stls (TCP :443 to entry with a
# real-looking SNI) outlives Reality (:8443) on networks that only
# whitelist by port.
LOCKDOWN_ORDER = ('ws', 'stls', 'reality', 'hy2', 'hy2t')
# Direct-to-entry transports: the ones a whitelist kills together.
DIRECT_PROTOCOLS = ('reality', 'hy2', 'hy2t', 'stls')
# CDN-fronted transports: the ones that survive. ws is the only one today
# (VMess+httpupgrade via Cloudflare, see project_phase_h_protocol_lessons).
FRONTED_PROTOCOLS = ('ws',)

# ---- modes ------------------------------------------------------------------
MODE_AUTO = 'auto'
MODE_ON = 'on'
MODE_OFF = 'off'
MODES = (MODE_AUTO, MODE_ON, MODE_OFF)

# ---- hysteresis -------------------------------------------------------------
ON_AFTER = 2            # consecutive signature evaluations (~20 min)
OFF_AFTER = 12          # consecutive healthy evaluations (~2 h)
MIN_DIRECT_MEASURED = 2 # one dark protocol is a ban, not a shutdown
MIN_DIRECT_ALIVE = 2    # "healthy" = at least two direct transports back
EVAL_INTERVAL_MIN = 10  # DPI_MONITOR_INTERVAL_MIN default — human text only

# ---- storage / audit --------------------------------------------------------
SETTING_KEY = 'lockdown_mode'
ORDER_SETTING_KEY = 'cascade_lockdown'
BY_AUTO = 'auto:probe_signature'
BY_SYSTEM = 'system'
AUTO_PREFIX = 'auto:'
ACTOR = 'dpi_monitor'                   # admin_actions.admin_id for auto events
ACTION_AUTO_ON = 'lockdown_auto_on'
ACTION_AUTO_OFF = 'lockdown_auto_off'
ACTION_SET = 'lockdown_set'             # the operator's /lockdown on|off|auto
AUDIT_TARGET = 'lockdown'               # admin_actions.target_id for auto events
EVENT_AUTO_ON = 'auto_on'
EVENT_AUTO_OFF = 'auto_off'


@dataclass(frozen=True)
class Event:
    """One detector decision: ``kind`` is 'auto_on' | 'auto_off',
    ``reason`` a short id, ``evidence`` the human (Russian) sentence
    that goes to admin_actions.details and the topic message."""
    kind: str
    reason: str
    evidence: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---- state ------------------------------------------------------------------

def default_state() -> dict:
    return {
        'mode': MODE_AUTO,
        'active': False,
        'since': None,
        'by': BY_SYSTEM,
        'reason': '',
        'streak_on': 0,
        'streak_off': 0,
        'last_change': None,
    }


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return False


def _int_or_zero(value: Any) -> int:
    """A hand-edited ``"streak_on": "x"`` must not make every tick raise
    until someone finds the typo."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _str_or_none(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.strip():
        return value
    return None


def parse_lockdown(raw: Any) -> dict:
    """Project whatever sits in ``lockdown_mode`` (a JSON string, an
    already-parsed dict, None, a Mock) onto the documented shape.
    Tolerant on purpose: never raises, unknown values fall back."""
    st = default_state()
    data = raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode('utf-8', 'replace')
    if isinstance(raw, str):
        if not raw.strip():
            return st
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as e:
            logger.warning(f"lockdown: bad JSON in app_settings[{SETTING_KEY}]: {e}")
            return st
    if not isinstance(data, dict):
        return st
    mode = data.get('mode')
    if isinstance(mode, str) and mode.strip().lower() in MODES:
        st['mode'] = mode.strip().lower()
    st['active'] = _coerce_bool(data.get('active'))
    st['since'] = _str_or_none(data.get('since'))
    st['by'] = _str_or_none(data.get('by')) or BY_SYSTEM
    reason = data.get('reason')
    st['reason'] = reason if isinstance(reason, str) else ''
    st['streak_on'] = _int_or_zero(data.get('streak_on'))
    st['streak_off'] = _int_or_zero(data.get('streak_off'))
    st['last_change'] = _str_or_none(data.get('last_change'))
    return st


def load_lockdown(db) -> dict:
    """The persisted state, normalised. Never raises: a missing row,
    bad JSON, a sqlite error (``Database.get_setting`` folds it into
    None) or a Mock db all read as "mode auto, not active"."""
    try:
        raw = db.get_setting(SETTING_KEY) if db is not None else None
    except Exception as e:          # a db double whose get_setting raises
        logger.warning(f"lockdown: read of app_settings[{SETTING_KEY}] failed: {e}")
        return default_state()
    return parse_lockdown(raw)


def is_lockdown_active(db) -> bool:
    """The one question /sub and the key card ask. Never raises."""
    try:
        return bool(load_lockdown(db).get('active'))
    except Exception as e:          # belt and braces — /sub depends on it
        logger.warning(f"lockdown: is_lockdown_active failed: {e}")
        return False


def dump_lockdown(state: dict) -> str:
    return json.dumps(parse_lockdown(state), ensure_ascii=False, sort_keys=True)


def save_lockdown(db, state: dict) -> bool:
    """Persist the normalised state. ``False`` on a failed write
    (``Database.set_setting`` swallows sqlite errors into False)."""
    try:
        return bool(db.set_setting(SETTING_KEY, dump_lockdown(state)))
    except Exception as e:
        logger.error(f"lockdown: write of app_settings[{SETTING_KEY}] failed: {e}")
        return False


def set_mode(db, mode: str, *, by: str, reason: str,
             now: Optional[datetime] = None) -> dict:
    """The operator's switch (``/lockdown on|off|auto``).

    'on'   → active immediately, ``by``/``reason`` recorded, ``since``
             kept if it was already active (the outage did not start
             when the admin pinned it);
    'off'  → inactive immediately;
    'auto' → ``active`` is left as it is, control goes back to the
             detector. ``by``/``since``/``reason`` still describe who
             produced the current ``active`` value, so they are not
             overwritten here. NOTE: an 'on' pinned by an admin and
             then handed to 'auto' stays active until ``/lockdown off``
             — the detector only undoes what the detector did.

    Raises ``ValueError`` on an unknown mode and ``RuntimeError`` when
    the write failed: a mode change the command answered "done" to but
    that never landed is a lie to the operator.
    """
    mode = str(mode or '').strip().lower()
    if mode not in MODES:
        raise ValueError(f"lockdown: unknown mode {mode!r} (expected one of {MODES})")
    now = now or datetime.utcnow()
    now_iso = now.isoformat()
    st = load_lockdown(db)
    st['mode'] = mode
    if mode == MODE_ON:
        if not st['active']:
            st['since'] = now_iso
        st['active'] = True
        st['by'] = str(by)
        st['reason'] = str(reason or '')
    elif mode == MODE_OFF:
        st['active'] = False
        st['since'] = None
        st['by'] = str(by)
        st['reason'] = str(reason or '')
    st['last_change'] = now_iso
    if not save_lockdown(db, st):
        raise RuntimeError(f"lockdown: write of app_settings[{SETTING_KEY}] failed "
                           f"— mode {mode!r} NOT applied")
    logger.info(f"lockdown: mode={mode} active={st['active']} by={st['by']} ({st['reason']})")
    return st


# ---- order ------------------------------------------------------------------

def _known_protocols() -> set:
    try:
        from bot.handlers.callbacks.user import MyKeyAnswerHandler
        return set(MyKeyAnswerHandler.PROTOCOL_METHOD_MAP)
    except Exception:
        return set(LOCKDOWN_ORDER)


def load_lockdown_order(db) -> tuple:
    """The operator's ``cascade_lockdown`` (JSON list), unknown names
    dropped, duplicates collapsed; empty / missing / bad → the
    built-in ``LOCKDOWN_ORDER``. Never raises."""
    try:
        raw = db.get_setting(ORDER_SETTING_KEY) if db is not None else None
    except Exception as e:
        logger.warning(f"lockdown: read of app_settings[{ORDER_SETTING_KEY}] failed: {e}")
        return LOCKDOWN_ORDER
    if not isinstance(raw, str) or not raw.strip():
        return LOCKDOWN_ORDER
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning(f"lockdown: bad JSON in app_settings[{ORDER_SETTING_KEY}]: {e}")
        return LOCKDOWN_ORDER
    if not isinstance(data, list):
        return LOCKDOWN_ORDER
    known = _known_protocols()
    out: List[str] = []
    for name in data:
        if not isinstance(name, str):
            continue
        name = name.strip().lower()
        if name in known and name not in out:
            out.append(name)
    return tuple(out) if out else LOCKDOWN_ORDER


def apply_lockdown_order(ordered: Iterable[str],
                         lockdown_order: Optional[Sequence[str]] = None) -> list:
    """Stable projection: the protocols named in ``lockdown_order``
    (default ``LOCKDOWN_ORDER``) first, in that order, then whatever
    else was in ``ordered`` in its existing order. Nothing is added
    (a protocol the operator disabled stays disabled) and nothing is
    dropped."""
    ordered = list(ordered)
    order = tuple(lockdown_order) if lockdown_order else LOCKDOWN_ORDER
    present = set(ordered)
    head = [p for p in order if p in present]
    head_set = set(head)
    return head + [p for p in ordered if p not in head_set]


# ---- detector (pure) --------------------------------------------------------

def _minutes_text(evaluations: int) -> str:
    mins = evaluations * EVAL_INTERVAL_MIN
    if mins >= 60 and mins % 60 == 0:
        return f"{mins // 60} ч"
    if mins >= 60:
        return f"{mins / 60:.1f} ч"
    return f"{mins} мин"


def _evidence_on(direct_dark: List[str], fronted_alive: List[str], streak: int) -> str:
    return (
        f"прямые протоколы ({', '.join(direct_dark)}) не отвечают "
        f"{_minutes_text(streak)}+ ({streak} оценки подряд), "
        f"CF-фронт ({', '.join(fronted_alive)}) жив"
    )


def _evidence_off(direct_alive: List[str], streak: int) -> str:
    return (
        f"прямые протоколы ({', '.join(direct_alive)}) снова отвечают "
        f"{_minutes_text(streak)}+ ({streak} оценок подряд)"
    )


def evaluate_lockdown(probe_signals: Optional[dict], lstate: Any,
                      now: datetime) -> Tuple[dict, Optional[Event]]:
    """Streaks in, decision out. Pure: no I/O, inputs not mutated.

    ``probe_signals`` is ``DPIMonitor.collect_signals()['probe']``:
    ``{'stale': bool, 'measured': [tag], 'dark': {tag: evidence}, ...}``.
    A failed probe collector must be passed as ``stale=True`` (the
    monitor does that) — "no data" is never "healthy".
    """
    st = parse_lockdown(dict(lstate) if isinstance(lstate, dict) else None)
    probe = probe_signals or {}
    if probe.get('stale', True):
        return st, None                      # no verdict: streaks frozen

    measured = [t for t in (probe.get('measured') or []) if isinstance(t, str)]
    dark = probe.get('dark') or {}
    direct_measured = [p for p in DIRECT_PROTOCOLS if p in measured]
    direct_dark = [p for p in direct_measured if p in dark]
    direct_alive = [p for p in direct_measured if p not in dark]
    fronted_measured = [p for p in FRONTED_PROTOCOLS if p in measured]
    fronted_alive = [p for p in fronted_measured if p not in dark]

    # ws dark: from the entry vantage an upstream outage (probe-proxy,
    # the entry→exit link, exit) and a shutdown are indistinguishable.
    # The pager owns that incident; the streaks wait.
    if fronted_measured and not fronted_alive:
        return st, None

    signature = (
        len(direct_measured) >= MIN_DIRECT_MEASURED
        and len(direct_dark) == len(direct_measured)
        and bool(fronted_alive)
    )
    healthy = len(direct_alive) >= MIN_DIRECT_ALIVE
    if signature:
        st['streak_on'] += 1
        st['streak_off'] = 0
    elif healthy:
        st['streak_off'] += 1
        st['streak_on'] = 0
    else:
        return st, None                      # ambiguous: frozen

    # 'on' / 'off' pin ``active``; the streaks above are still kept so
    # /lockdown can show what the detector would do.
    if st['mode'] != MODE_AUTO:
        return st, None
    now_iso = now.isoformat()
    if signature and not st['active'] and st['streak_on'] >= ON_AFTER:
        evidence = _evidence_on(direct_dark, fronted_alive, st['streak_on'])
        st.update(active=True, since=now_iso, by=BY_AUTO, reason=evidence,
                  last_change=now_iso)
        return st, Event(EVENT_AUTO_ON, 'probe_signature', evidence)
    if (healthy and st['active'] and str(st['by']).startswith(AUTO_PREFIX)
            and st['streak_off'] >= OFF_AFTER):
        evidence = _evidence_off(direct_alive, st['streak_off'])
        st.update(active=False, since=None, by=BY_AUTO, reason=evidence,
                  last_change=now_iso)
        return st, Event(EVENT_AUTO_OFF, 'probe_recovered', evidence)
    return st, None


# ---- messages ---------------------------------------------------------------

def _order_text(order: Optional[Sequence[str]]) -> str:
    return ', '.join(order) if order else ', '.join(LOCKDOWN_ORDER)


def _since_text(since: Optional[str]) -> str:
    if not since:
        return '—'
    s = str(since)
    return f"{s[:10]} {s[11:16]} UTC" if len(s) >= 16 else s


def format_lockdown_html(event_or_state: Any, *, order: Optional[Sequence[str]] = None) -> str:
    """The forum-topic message (auto events) / the status card body
    (a state dict). Always ends with the commands that undo or pin
    it — an automatic actor the operator cannot reverse in one
    command is worse than no automatic actor."""
    order_s = html.escape(_order_text(order))
    if isinstance(event_or_state, Event):
        ev = event_or_state
        if ev.kind == EVENT_AUTO_ON:
            return (
                f"🔒 <b>LOCKDOWN включён автоматически</b>: {html.escape(ev.evidence)}.\n"
                f"Каскад: {order_s}; DNS через туннель.\n"
                f"Снять: /lockdown off · Зафиксировать: /lockdown on · "
                f"Оповестить юзеров: /broadcast"
            )
        return (
            f"🔓 <b>LOCKDOWN снят автоматически</b>: {html.escape(ev.evidence)}.\n"
            f"Каскад и DNS вернулись к обычному профилю.\n"
            f"Статус: /lockdown · Включить вручную: /lockdown on"
        )
    st = parse_lockdown(event_or_state)
    by = html.escape(str(st['by']))
    reason = html.escape(st['reason'] or '—')
    if st['active']:
        return (
            f"🔒 <b>LOCKDOWN активен</b> ({by}, с {html.escape(_since_text(st['since']))}): "
            f"{reason}.\n"
            f"Режим: {st['mode']} · Каскад: {order_s}; DNS через туннель.\n"
            f"Снять: /lockdown off · Оповестить юзеров: /broadcast"
        )
    watching = ' (детектор следит)' if st['mode'] == MODE_AUTO else ''
    return (
        f"🔓 <b>LOCKDOWN не активен</b> · режим: {st['mode']}{watching}.\n"
        f"Включить вручную: /lockdown on · Автодетект: /lockdown auto"
    )
