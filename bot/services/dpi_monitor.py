"""DPIMonitor — the piece that ACTS on the telemetry (IMPROVEMENT_PLAN A1).

Why this exists
---------------
2026-09-01, 00:01 UTC: a client update blanked ``flow`` on the Reality
inbound. The probe suite saw it within one run (reality: 672/960 ok per
day → 0/960), ``dpi_metrics`` saw it too (MegaFon AS31133: 879 handshake
fails against 0 connections in the outage window) — and every user whose
sing-box ``auto`` selector sat on Reality was down for FOUR DAYS, because
no code path turned those rows into a different cascade. The pager
(``alert_manager.check_protocol_probe_down``) now tells the operator;
this module is the other half: the cascade reacts on its own, the
operator gets one message in the forum topic, and one command
(``/cascade reset``) undoes everything. 2026-09-05 repeated the pattern
in miniature — ws sat at 2/30 ok for an hour behind a slow CF path while
it stayed the second protocol demo users were handed.

What it does (and deliberately does NOT do)
-------------------------------------------
Every ``DPI_MONITOR_INTERVAL_MIN`` (10) minutes the job reads four
signals and, with hysteresis, moves failing protocols to the END of the
user-facing cascade. It never removes or disables a protocol, and it
never edits the operator's own settings — ``cascade_protocol_order``,
``cascade_by_asn`` and ``cascade_by_country`` stay untouched.
``MyKeyAnswerHandler.get_cascade_order`` stable-partitions the
operator's order by the auto-demoted set (``cascade_auto``), so an
explicit ASN override still decides the base order and auto only
reorders within it. When the signal goes quiet the protocol is restored.

Rules (signal → target)
-----------------------
  id                signal (window)                                  demotes
  probe_dark        outbound_health: zero ALIVE rows in the last     <p> globally
                    3 runs (LIMIT 30 within 3 h, ≥15 samples);
                    alive = latency_ms IS NOT NULL OR status='ok'
  probe_degraded    outbound_health: ok/len < 0.25 over ≥25 samples  <p> globally
  reality_asn       dpi_metrics inbound_tag='reality' per ASN, 2 h:  reality @ASN
                    hsfail ≥ 30 AND hsfail ≥ 2 × conn
  udp_storm_asn     hy2_auth_log: ≥30 allows / 2 h for ONE chat_id,  hy2 + hy2t @ASN
                    attributed to users.last_asn (the auth row's own
                    asn is always NULL — src is entry)
  user_reports_asn  user_failure_reports: ≥2 distinct chat_ids from  head of that ASN's
                    one ASN within 6 h                               effective order,
                                                                     ≤2 per ASN

Hysteresis
----------
  demote after 2 consecutive bad evaluations (~20 min at the 10-min
  cadence); restore after 6 consecutive good ones (~1 h); at least
  30 min between two changes of the same target; at most 2 changes per
  run, ranked probe_dark > probe_degraded > reality_asn > udp_storm_asn
  > user_reports_asn with restores after demotes. "good" means the RULE
  that demoted the target is quiet this evaluation (the rule id is kept
  in state), so a probe-demoted protocol is not restored just because
  no user complained.

Guards
------
  * every probed protocol dark at once → upstream outage (probe sidecar,
    entry→exit link, exit itself); alert_manager already pages, nothing
    here moves and no streak advances — reordering would be noise;
  * probe pipeline stale (newest outbound_health row > 45 min old) →
    probe rules frozen (no demote, no restore, pending bad streaks kept
    as they are), per-ASN rules still run;
  * a collector raises (table missing, locked, schema drift) → its rule
    is UNKNOWN for this run: targets it demoted and streaks it was
    building are frozen (no good, no bad), every other rule runs as
    usual, and a WARNING is logged on every run it stays broken. "No
    data" is never read as "signal quiet" — that reading would restore
    a demotion an hour after the collector broke;
  * app_settings unreadable (sqlite error on the state keys) → the run
    is skipped with a WARNING. ``Database.get_setting`` returns the
    default on an error, which the monitor would otherwise take for
    "nothing demoted" and persist — wiping every demotion;
  * ``dpi_monitor_enabled`` = "0" → the job logs and returns; state and
    cascade_auto untouched.

Storage (app_settings, JSON strings)
------------------------------------
  cascade_auto        {"global": {"<proto>": {"since": iso, "reason": rule_id,
                       "evidence": str}}, "asn": {"AS31133": {"<proto>": {...}}}}
                      — EFFECTIVE demotions only; this is what
                      get_cascade_order reads.
  dpi_monitor_state   {"targets": {"global:ws": {"bad": int, "good": int,
                       "last_change": iso|null, "rule": rule_id|null},
                       "asn:AS31133:reality": {...}}, "last_run": iso, "runs": int}
  dpi_monitor_enabled "1" / "0" (missing → Settings.DPI_MONITOR_ENABLED).

Layout: ``collect_signals`` (I/O in) → ``evaluate`` (pure, no I/O) →
``apply_changes`` (I/O out); ``run_once`` strings them together.
"""

import copy
import html
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---- rule ids --------------------------------------------------------------
RULE_PROBE_DARK = 'probe_dark'
RULE_PROBE_DEGRADED = 'probe_degraded'
RULE_REALITY_ASN = 'reality_asn'
RULE_UDP_STORM_ASN = 'udp_storm_asn'
RULE_USER_REPORTS_ASN = 'user_reports_asn'
# Rank for the per-run cap: DARK > DEGRADED > R3 > R4 > R5.
RULES = (
    RULE_PROBE_DARK,
    RULE_PROBE_DEGRADED,
    RULE_REALITY_ASN,
    RULE_UDP_STORM_ASN,
    RULE_USER_REPORTS_ASN,
)
RULE_RANK = {rule: i for i, rule in enumerate(RULES)}
PROBE_RULES = frozenset({RULE_PROBE_DARK, RULE_PROBE_DEGRADED})
# Which rules go UNKNOWN (frozen) when a collector fails — see Guards.
COLLECTOR_RULES = {
    'probe': PROBE_RULES,
    'reality_asn': frozenset({RULE_REALITY_ASN}),
    'udp_storm_asn': frozenset({RULE_UDP_STORM_ASN}),
    'reports_asn': frozenset({RULE_USER_REPORTS_ASN}),
}
# Human labels for /cascade and the dashboard note.
RULE_TITLES_RU = {
    RULE_PROBE_DARK: 'пробы: протокол мёртв (DARK)',
    RULE_PROBE_DEGRADED: 'пробы: протокол деградировал (DEGRADED)',
    RULE_REALITY_ASN: 'Reality: handshake-fail по ASN',
    RULE_UDP_STORM_ASN: 'Hy2: reconnect-шторм по ASN',
    RULE_USER_REPORTS_ASN: 'жалобы «не работает» по ASN',
}

# ---- probe thresholds — MIRROR of alert_manager.build_default_checks -------
# Those constants are locals of the check factory (not importable), so they
# are copied here verbatim. Keep the two blocks in step: the pager and the
# monitor must agree on what "dark" and "degraded" mean, or the operator
# gets paged about a protocol the cascade still hands out (or vice versa).
PROBE_RUNS = 3              # consecutive probe runs that must be all-bad
try:
    from bot.services.health_checker import HealthChecker as _HC
    PROBE_DOMAINS = len(_HC.TARGET_DOMAINS)   # rows per protocol per run
except Exception:            # the monitor must not die with the checker import
    PROBE_DOMAINS = 10
PROBE_MIN_SAMPLES = 15      # ignore thin windows (partial run, new deploy)
PROBE_WINDOW_H = 3          # only look at recent rows
PROBE_STALE_MIN = 45        # 3 missed runs at the 15-min cadence
PROBE_DEGRADED_OK_RATIO = 0.25   # below a quarter of the 7/10 norm…
PROBE_DEGRADED_MIN_SAMPLES = 25  # …sustained over ~3 runs, not one dip

# ---- R3: Reality handshake failures per ASN --------------------------------
# Reality users reach exit with their real IP (PROXY protocol via entry
# haproxy), so a dpi_metrics row with inbound_tag='reality' AND an ASN is a
# real cohort. 2026-09-01 MegaFon: 879 hsfail / 0 conn; healthy ASNs sit at
# a hsfail:conn ratio ≤ 0.1, so "twice the connections" is a wide margin.
REALITY_WINDOW_H = 2
REALITY_MIN_HSFAIL = 30
REALITY_HSFAIL_PER_CONN = 2
REALITY_INBOUND_TAG = 'reality'
# cf-ws / ss2022 collapse into this marker bucket (entry MASQUERADE) and
# their hsfail is mostly background probing — never a per-ASN decision.
# Mirrors web_server.DPI_TUNNEL_BUCKET_CC; any '*…*' marker is skipped.
DPI_MARKER_PREFIX = '*'

# ---- R4: hy2 reconnect storms per user → ASN --------------------------------
# Calibration (prod): normal 4-8 allow/day/user; heavy-but-healthy peaks
# ~44/day; a client on throttled mobile UDP (2026-09-01/02) produced
# 108-128/day in 17-25/h bursts. 30 in 2 h is above any healthy pattern.
UDP_STORM_WINDOW_H = 2
UDP_STORM_MIN_ALLOWS = 30
UDP_STORM_PROTOCOLS = ('hy2', 'hy2t')   # throttled UDP hits both instances

# ---- R5: the user's "не работает" button -----------------------------------
# 2 rows in 30 days on prod — weak, so it needs two DIFFERENT users of one
# ASN, and it may push at most two protocols per ASN before it stops.
REPORTS_WINDOW_H = 6
REPORTS_MIN_USERS = 2
REPORTS_MAX_PER_ASN = 2

# ---- hysteresis -------------------------------------------------------------
DEMOTE_AFTER_BAD = 2        # consecutive bad evaluations (~20 min)
RESTORE_AFTER_GOOD = 6      # consecutive good evaluations (~1 h)
MIN_CHANGE_GAP_MIN = 30     # between two changes of the same target
MAX_CHANGES_PER_RUN = 2

# ---- storage / audit --------------------------------------------------------
AUTO_SETTING_KEY = 'cascade_auto'
STATE_SETTING_KEY = 'dpi_monitor_state'
ENABLED_SETTING_KEY = 'dpi_monitor_enabled'
ACTOR = 'dpi_monitor'                   # admin_actions.admin_id
ACTION_DEMOTE = 'cascade_auto_demote'
ACTION_RESTORE = 'cascade_auto_restore'
ACTION_RESET = 'cascade_auto_reset'
UNDO_HINT = '/cascade reset'


class StateUnreadable(RuntimeError):
    """app_settings could not be READ (sqlite error, not a missing row).
    The run must be skipped: evaluating from an empty state and saving
    it back would persist "nothing demoted"."""


@dataclass(frozen=True)
class Change:
    """One applied (or, in dry-run, proposed) cascade move."""
    scope: str                 # 'global' | 'asn'
    target: Optional[str]      # None for global, 'AS31133' for asn
    protocol: str
    action: str                # 'demote' | 'restore'
    reason: str                # rule id (RULE_*)
    evidence: str              # human-readable, Russian

    @property
    def key(self) -> str:
        return target_key(self.scope, self.target, self.protocol)

    @property
    def audit_target(self) -> str:
        """admin_actions.target_id: ``<scope>:<asn|global>:<proto>``."""
        return f"{self.scope}:{self.target or 'global'}:{self.protocol}"

    @property
    def where_ru(self) -> str:
        return 'глобально' if self.scope == 'global' else str(self.target)

    def to_dict(self) -> dict:
        return asdict(self)


# ---- small pure helpers ------------------------------------------------------

def target_key(scope: str, asn: Optional[str], protocol: str) -> str:
    if scope == 'global':
        return f'global:{protocol}'
    return f'asn:{asn}:{protocol}'


def parse_target_key(key: str) -> Tuple[str, Optional[str], str]:
    parts = str(key).split(':', 2)
    if len(parts) == 2 and parts[0] == 'global':
        return 'global', None, parts[1]
    if len(parts) == 3 and parts[0] == 'asn':
        return 'asn', parts[1], parts[2]
    raise ValueError(f'bad target key: {key!r}')


def _norm_asn(asn: Any) -> Optional[str]:
    if not isinstance(asn, str):
        return None
    asn = asn.strip().upper()
    return asn or None


def _minutes_since(ts_str: Any, now: datetime) -> float:
    try:
        ts = datetime.fromisoformat(str(ts_str).replace('Z', ''))
    except (TypeError, ValueError):
        return float('inf')
    return max(0.0, (now - ts).total_seconds() / 60.0)


def _coerce_flag(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ('1', 'true', 'yes', 'on'):
            return True
        if v in ('0', 'false', 'no', 'off', ''):
            return False
    return default


def empty_signals() -> dict:
    return {
        'probe': {
            'stale': True,
            'newest_age_min': None,
            'dark': {},
            'degraded': {},
            'measured': [],
            'all_dark': False,
        },
        'reality_asn': {},
        'udp_storm_asn': {},
        'reports_asn': {},
        'asn_head': {},
        # Rule ids whose collector failed this run (frozen in evaluate).
        'unknown': [],
    }


def empty_state() -> dict:
    return {
        'targets': {},
        'auto': {'global': {}, 'asn': {}},
        'last_run': None,
        'runs': 0,
    }


def normalize_auto(raw: Any) -> dict:
    """Project whatever sits in ``cascade_auto`` onto the documented shape.
    Tolerant on purpose: bad JSON or a hand-edited blob must degrade to
    "nothing demoted", never to an exception in /sub."""
    out = {'global': {}, 'asn': {}}
    if not isinstance(raw, dict):
        return out
    g = raw.get('global')
    if isinstance(g, dict):
        for proto, entry in g.items():
            if isinstance(proto, str):
                out['global'][proto] = dict(entry) if isinstance(entry, dict) else {}
    a = raw.get('asn')
    if isinstance(a, dict):
        for asn, protos in a.items():
            asn_n = _norm_asn(asn)
            if not asn_n or not isinstance(protos, dict):
                continue
            bucket = {}
            for proto, entry in protos.items():
                if isinstance(proto, str):
                    bucket[proto] = dict(entry) if isinstance(entry, dict) else {}
            if bucket:
                out['asn'][asn_n] = bucket
    return out


def normalize_state(raw: Any) -> dict:
    """Merged in-memory state: ``dpi_monitor_state`` + ``cascade_auto``."""
    st = empty_state()
    if isinstance(raw, dict):
        targets = raw.get('targets')
        if isinstance(targets, dict):
            for key, entry in targets.items():
                if not isinstance(key, str) or not isinstance(entry, dict):
                    continue
                try:
                    parse_target_key(key)
                except ValueError:
                    continue
                st['targets'][key] = {
                    'bad': _int_or_zero(entry.get('bad')),
                    'good': _int_or_zero(entry.get('good')),
                    'last_change': entry.get('last_change') or None,
                    'rule': entry.get('rule') or None,
                }
        st['auto'] = normalize_auto(raw.get('auto'))
        st['last_run'] = raw.get('last_run') or None
        st['runs'] = _int_or_zero(raw.get('runs'))
    return st


def _int_or_zero(value: Any) -> int:
    """A hand-edited ``"bad": "x"`` must not make every run raise until
    someone finds the typo — the monitor would be dead exactly as
    silently as the gap it exists to close."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _auto_keys(auto: dict) -> set:
    keys = set()
    for proto in auto.get('global', {}):
        keys.add(target_key('global', None, proto))
    for asn, protos in auto.get('asn', {}).items():
        for proto in protos:
            keys.add(target_key('asn', asn, proto))
    return keys


def _auto_get(auto: dict, key: str) -> dict:
    scope, asn, proto = parse_target_key(key)
    if scope == 'global':
        return auto.get('global', {}).get(proto) or {}
    return auto.get('asn', {}).get(asn, {}).get(proto) or {}


def _auto_set(auto: dict, key: str, entry: dict) -> None:
    scope, asn, proto = parse_target_key(key)
    if scope == 'global':
        auto.setdefault('global', {})[proto] = entry
    else:
        auto.setdefault('asn', {}).setdefault(asn, {})[proto] = entry


def _auto_del(auto: dict, key: str) -> None:
    scope, asn, proto = parse_target_key(key)
    if scope == 'global':
        auto.get('global', {}).pop(proto, None)
    else:
        bucket = auto.get('asn', {}).get(asn)
        if bucket is not None:
            bucket.pop(proto, None)
            if not bucket:
                auto['asn'].pop(asn, None)


def format_changes_html(changes: List[Change]) -> str:
    """The forum-topic message: one per run, all changes batched.

    Single change reads as a headline; several as a list. Always ends
    with the undo hint — an auto action the operator cannot reverse in
    one command is worse than no auto action.
    """
    def _verb(ch: Change) -> str:
        return 'в конец' if ch.action == 'demote' else 'на место'

    if len(changes) == 1:
        ch = changes[0]
        return (
            f"🔁 Каскад: <b>{html.escape(ch.protocol)}</b> → {_verb(ch)} "
            f"({html.escape(ch.where_ru)})\n"
            f"Причина: {html.escape(ch.evidence)}\n"
            f"Отменить: {UNDO_HINT}"
        )
    lines = ["🔁 <b>Каскад: авто-изменения</b>"]
    for ch in changes:
        lines.append(
            f"• <b>{html.escape(ch.protocol)}</b> → {_verb(ch)} "
            f"({html.escape(ch.where_ru)})\n"
            f"  Причина: {html.escape(ch.evidence)}"
        )
    lines.append(f"Отменить: {UNDO_HINT}")
    return '\n'.join(lines)


class DPIMonitor:
    """Scheduled auto-demotion/restoration of cascade protocols.

    ``bot`` may be None (tests, manual runs): the topic message is then
    skipped, everything else still happens.
    """

    def __init__(self, db, config, bot=None):
        self.db = db
        self.config = config
        self.bot = bot

    # ---- enable flag -------------------------------------------------------

    def is_enabled(self) -> bool:
        """``dpi_monitor_enabled`` in app_settings wins (that is what
        ``/cascade on|off`` flips at runtime); when it is unset the env
        default ``Settings.DPI_MONITOR_ENABLED`` decides."""
        raw = self.db.get_setting(ENABLED_SETTING_KEY) if self.db else None
        if isinstance(raw, str) and raw.strip():
            return raw.strip() == '1'
        return _coerce_flag(getattr(self.config, 'DPI_MONITOR_ENABLED', True), True)

    def set_enabled(self, flag: bool) -> bool:
        return bool(self.db.set_setting(ENABLED_SETTING_KEY, '1' if flag else '0'))

    # ---- storage -----------------------------------------------------------

    def _read_setting(self, key: str) -> Any:
        """Read one app_settings row, telling MISSING (None) apart from
        UNREADABLE (raises ``StateUnreadable``).

        ``Database.get_setting`` folds both into its default — fine for
        a tunable, fatal here: a locked / broken read taken for "no row"
        makes the run evaluate from an empty state and then persist it,
        i.e. silently restore every demotion. So the state keys are
        read on a raw connection and a sqlite error stops the run.
        Non-sqlite dbs (fakes, mocks) fall back to ``get_setting``.
        """
        if self.db is None:
            return None
        connect = getattr(self.db, '_connect', None)
        if connect is None:
            return self.db.get_setting(key)
        try:
            conn = connect()
        except sqlite3.Error as e:
            raise StateUnreadable(f"connect: {e}") from e
        try:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
        except sqlite3.Error as e:
            raise StateUnreadable(f"{key}: {e}") from e
        except Exception:
            # Not a sqlite connection (a Mock, a dict-backed fake):
            # take whatever get_setting says, as before.
            return self.db.get_setting(key)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _load_json(self, key: str) -> Any:
        raw = self._read_setting(key)
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as e:
            logger.warning(f"dpi_monitor: bad JSON in app_settings[{key}]: {e}")
            return None

    def load_auto(self) -> dict:
        return normalize_auto(self._load_json(AUTO_SETTING_KEY))

    def load_state(self) -> dict:
        """Merged state for ``evaluate``: streaks from dpi_monitor_state
        plus the effective demotions from cascade_auto (kept as the
        source of truth for "what is demoted now", so a cleared
        cascade_auto means "nothing demoted" even if streaks linger)."""
        st = normalize_state(self._load_json(STATE_SETTING_KEY))
        st['auto'] = self.load_auto()
        return st

    def save_state(self, state: dict) -> None:
        state = normalize_state(state)
        auto = state['auto']
        monitor = {
            'targets': state['targets'],
            'last_run': state['last_run'],
            'runs': state['runs'],
        }
        # ``set_setting`` swallows sqlite errors into ``False``. A change
        # that was audited and announced but never landed in
        # cascade_auto is a lie to the operator — cascade_auto goes
        # first, and a failed write stops the run before audit/notify.
        ok = self.db.set_setting(
            AUTO_SETTING_KEY, json.dumps(auto, ensure_ascii=False, sort_keys=True))
        if ok is False:
            raise RuntimeError(f"dpi_monitor: write of app_settings[{AUTO_SETTING_KEY}] "
                               f"failed — run aborted, nothing applied")
        ok = self.db.set_setting(
            STATE_SETTING_KEY, json.dumps(monitor, ensure_ascii=False, sort_keys=True))
        if ok is False:
            # The opposite case: cascade_auto IS written, so the change is
            # live for users — it must still be audited and announced.
            # Only the streaks are lost (the next run starts them over).
            logger.error(f"dpi_monitor: write of app_settings[{STATE_SETTING_KEY}] failed "
                         f"— cascade_auto saved (changes are live), streaks lost this run")

    def reset(self, actor: str = ACTOR) -> dict:
        """``/cascade reset``: drop every auto demotion and every streak.
        Returns the cleared ``cascade_auto`` so the caller can say what
        went back to normal. Logged under ``actor`` (the admin id when
        invoked from the command)."""
        before = self.load_auto()
        self.db.set_setting(AUTO_SETTING_KEY, '{}')
        self.db.set_setting(STATE_SETTING_KEY, '{}')
        cleared = sorted(_auto_keys(before))
        self.db.log_admin_action(
            str(actor), ACTION_RESET, target_id='cascade_auto',
            details='cleared: ' + (', '.join(cleared) if cleared else 'nothing'),
        )
        logger.info(f"dpi_monitor: reset by {actor}, cleared {cleared}")
        return before

    def status(self) -> dict:
        """One dict for /cascade and GET /api/admin/cascade_order."""
        st = self.load_state()
        return {
            'enabled': self.is_enabled(),
            'global': st['auto']['global'],
            'asn': st['auto']['asn'],
            'last_run': st['last_run'],
            'runs': st['runs'],
            'targets': st['targets'],
        }

    # ---- collectors (I/O in) ------------------------------------------------

    def collect_signals(self, now: Optional[datetime] = None) -> dict:
        """Read the four sources with the documented windows. Each
        collector is isolated: one failing table must not blind the
        others (the whole point is to stop being blind)."""
        now = now or datetime.utcnow()
        signals = empty_signals()
        unknown: set = set()
        try:
            conn = self.db._connect()
        except Exception as e:
            # Every rule is blind; the WARNING repeats each run on purpose.
            logger.warning(f"dpi_monitor: db connect failed: {e} — all rules "
                           f"UNKNOWN this run, existing demotions frozen")
            signals['unknown'] = sorted(RULES)
            return signals
        try:
            for name, fn in (
                ('probe', self._collect_probe),
                ('reality_asn', self._collect_reality_asn),
                ('udp_storm_asn', self._collect_udp_storm),
                ('reports_asn', self._collect_reports),
            ):
                try:
                    signals[name] = fn(conn, now)
                except Exception as e:
                    rules = COLLECTOR_RULES.get(name, frozenset())
                    unknown |= set(rules)
                    # An empty result is NOT "the rule is quiet": mark it
                    # UNKNOWN so evaluate freezes what this rule holds
                    # instead of counting a good streak toward a restore.
                    logger.warning(
                        f"dpi_monitor: collector {name} failed: {e} — rule(s) "
                        f"{', '.join(sorted(rules))} UNKNOWN this run, their "
                        f"demotions/streaks frozen until the collector works again")
        finally:
            try:
                conn.close()
            except Exception:
                pass
        # R5 needs the head of each firing ASN's EFFECTIVE order (after the
        # demotions already in place) — that is a cascade read, not SQL.
        if signals['reports_asn']:
            signals['asn_head'] = self._collect_asn_heads(signals['reports_asn'])
        signals['unknown'] = sorted(unknown)
        return signals

    @staticmethod
    def _known_protocols() -> set:
        try:
            from bot.handlers.callbacks.user import MyKeyAnswerHandler
            return set(MyKeyAnswerHandler.PROTOCOL_METHOD_MAP)
        except Exception:
            return {'stls', 'ws', 'hy2', 'hy2t', 'reality'}

    def _collect_probe(self, conn, now: datetime) -> dict:
        out = empty_signals()['probe']
        newest = conn.execute("SELECT MAX(ts) FROM outbound_health").fetchone()[0]
        age = _minutes_since(newest, now) if newest else None
        out['newest_age_min'] = age
        out['stale'] = newest is None or age > PROBE_STALE_MIN
        if out['stale']:
            return out
        cutoff = (now - timedelta(hours=PROBE_WINDOW_H)).isoformat()
        limit = PROBE_RUNS * PROBE_DOMAINS
        known = self._known_protocols()
        tags = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT outbound_tag FROM outbound_health WHERE ts >= ?",
                (cutoff,),
            ).fetchall()
            if r[0] in known
        ]
        for tag in sorted(tags):
            rows = conn.execute(
                "SELECT status, latency_ms FROM outbound_health "
                "WHERE outbound_tag = ? AND ts >= ? ORDER BY ts DESC LIMIT ?",
                (tag, cutoff, limit),
            ).fetchall()
            n = len(rows)
            if n < PROBE_MIN_SAMPLES:
                continue
            out['measured'].append(tag)
            # Liveness = a round trip happened (same rule as the pager).
            alive = sum(1 for st, lat in rows if lat is not None or st == 'ok')
            ok = sum(1 for st, _lat in rows if st == 'ok')
            if alive == 0:
                out['dark'][tag] = (
                    f"пробы {ok}/{n} за {PROBE_RUNS} прогона, ни одного живого "
                    f"ответа (DARK)"
                )
            elif n >= PROBE_DEGRADED_MIN_SAMPLES and ok / n < PROBE_DEGRADED_OK_RATIO:
                out['degraded'][tag] = (
                    f"пробы ok {ok}/{n} за {PROBE_RUNS} прогона при норме ~7/10 "
                    f"(DEGRADED)"
                )
        measured = out['measured']
        out['all_dark'] = (
            bool(measured) and len(out['dark']) == len(measured) and len(measured) >= 2
        )
        return out

    def _collect_reality_asn(self, conn, now: datetime) -> dict:
        cutoff = (now - timedelta(hours=REALITY_WINDOW_H)).isoformat()
        rows = conn.execute(
            "SELECT UPPER(TRIM(asn)) AS asn, "
            "SUM(handshake_fail_count) AS hs, SUM(conn_count) AS conn "
            "FROM dpi_metrics "
            "WHERE snapshot_at >= ? AND inbound_tag = ? "
            "AND asn IS NOT NULL AND TRIM(asn) != '' "
            "AND (country IS NULL OR substr(country, 1, 1) != ?) "
            "GROUP BY UPPER(TRIM(asn))",
            (cutoff, REALITY_INBOUND_TAG, DPI_MARKER_PREFIX),
        ).fetchall()
        out = {}
        for asn, hs, conn_count in rows:
            hs = int(hs or 0)
            conn_count = int(conn_count or 0)
            if hs >= REALITY_MIN_HSFAIL and hs >= REALITY_HSFAIL_PER_CONN * conn_count:
                out[asn] = (
                    f"{asn}: Reality handshake-fail {hs} / conn {conn_count} "
                    f"за {REALITY_WINDOW_H} ч"
                )
        return out

    def _collect_udp_storm(self, conn, now: datetime) -> dict:
        # hy2_auth_log.ts is sqlite CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS'),
        # not the 'T' isoformat the probe/dpi tables use — a 'T' cutoff
        # would sort every same-day row BELOW it.
        cutoff = (now - timedelta(hours=UDP_STORM_WINDOW_H)).strftime('%Y-%m-%d %H:%M:%S')
        rows = conn.execute(
            "SELECT h.chat_id, COUNT(*) AS n, UPPER(TRIM(u.last_asn)) AS asn "
            "FROM hy2_auth_log h JOIN users u ON u.chat_id = h.chat_id "
            "WHERE h.ts >= ? AND h.decision = 'allow' "
            "AND u.last_asn IS NOT NULL AND TRIM(u.last_asn) != '' "
            "GROUP BY h.chat_id HAVING COUNT(*) >= ?",
            (cutoff, UDP_STORM_MIN_ALLOWS),
        ).fetchall()
        per_asn: Dict[str, List[int]] = {}
        for _chat_id, n, asn in rows:
            per_asn.setdefault(asn, []).append(int(n))
        return {
            asn: (
                f"{asn}: hy2 reconnect-шторм у {len(counts)} юз. "
                f"(≥{UDP_STORM_MIN_ALLOWS} allow за {UDP_STORM_WINDOW_H} ч, "
                f"макс {max(counts)})"
            )
            for asn, counts in per_asn.items()
        }

    def _collect_reports(self, conn, now: datetime) -> dict:
        cutoff = (now - timedelta(hours=REPORTS_WINDOW_H)).strftime('%Y-%m-%d %H:%M:%S')
        rows = conn.execute(
            "SELECT UPPER(TRIM(asn)) AS asn, COUNT(DISTINCT chat_id) AS users "
            "FROM user_failure_reports "
            "WHERE ts >= ? AND asn IS NOT NULL AND TRIM(asn) != '' "
            "AND chat_id IS NOT NULL "
            "GROUP BY UPPER(TRIM(asn)) HAVING COUNT(DISTINCT chat_id) >= ?",
            (cutoff, REPORTS_MIN_USERS),
        ).fetchall()
        return {
            asn: (
                f"{asn}: {int(users)} жалобы «не работает» от разных юзеров "
                f"за {REPORTS_WINDOW_H} ч"
            )
            for asn, users in rows
        }

    def _collect_asn_heads(self, asns) -> dict:
        try:
            from bot.handlers.callbacks.user import MyKeyAnswerHandler
        except Exception as e:
            logger.warning(f"dpi_monitor: cascade import failed: {e}")
            return {}
        heads = {}
        for asn in asns:
            try:
                order = MyKeyAnswerHandler.get_cascade_order(self.db, asn=asn)
            except Exception as e:
                logger.warning(f"dpi_monitor: cascade order for {asn} failed: {e}")
                continue
            if order:
                heads[asn] = order[0]
        return heads

    # ---- core (pure) -------------------------------------------------------

    @staticmethod
    def evaluate(signals: dict, state: dict, now: datetime) -> Tuple[dict, List[Change]]:
        """Streaks in, decisions out. No I/O; ``state`` is not mutated."""
        state = normalize_state(copy.deepcopy(state))
        targets: Dict[str, dict] = state['targets']
        auto: dict = state['auto']
        now_iso = now.isoformat()
        state['last_run'] = now_iso
        state['runs'] = int(state.get('runs') or 0) + 1

        probe = (signals or {}).get('probe') or {}
        probe_usable = not probe.get('stale', True)
        dark = probe.get('dark') or {}
        degraded = probe.get('degraded') or {}
        reality = (signals or {}).get('reality_asn') or {}
        storms = (signals or {}).get('udp_storm_asn') or {}
        reports = (signals or {}).get('reports_asn') or {}
        heads = (signals or {}).get('asn_head') or {}
        # Rules with no verdict this run: their collector failed, or (for
        # the probe pair) the probe pipeline is stale. Whatever such a
        # rule holds — a demotion or a half-built bad streak — is left
        # exactly as it is: no good, no bad, no change.
        frozen_rules = {r for r in ((signals or {}).get('unknown') or ()) if r in RULE_RANK}
        if not probe_usable:
            frozen_rules |= PROBE_RULES

        # Upstream outage: everything measured is dark at once. The pager
        # owns that incident; here nothing moves and no streak advances.
        if probe_usable and probe.get('all_dark'):
            return state, []

        # What is firing this evaluation: target key → (rule, evidence).
        firing: Dict[str, Tuple[str, str]] = {}
        if probe_usable:
            for proto, ev in dark.items():
                firing[target_key('global', None, proto)] = (RULE_PROBE_DARK, ev)
            for proto, ev in degraded.items():
                firing.setdefault(target_key('global', None, proto), (RULE_PROBE_DEGRADED, ev))
        for asn, ev in reality.items():
            firing[target_key('asn', asn, REALITY_INBOUND_TAG)] = (RULE_REALITY_ASN, ev)
        for asn, ev in storms.items():
            for proto in UDP_STORM_PROTOCOLS:
                firing.setdefault(target_key('asn', asn, proto), (RULE_UDP_STORM_ASN, ev))
        for asn, ev in reports.items():
            head = heads.get(asn)
            if not head:
                continue
            already = sum(
                1 for entry in auto.get('asn', {}).get(asn, {}).values()
                if entry.get('reason') == RULE_USER_REPORTS_ASN
            )
            if already >= REPORTS_MAX_PER_ASN:
                continue
            firing.setdefault(target_key('asn', asn, head), (RULE_USER_REPORTS_ASN, ev))

        def rule_active(rule: Optional[str], key: str) -> bool:
            """Is the rule that demoted ``key`` still firing? Per-ASN rules
            are judged per ASN (R5 moves the head, so the demoted protocol
            itself is no longer the head the rule points at)."""
            _scope, asn, proto = parse_target_key(key)
            if rule in PROBE_RULES:
                return proto in dark or proto in degraded
            if rule == RULE_REALITY_ASN:
                return asn in reality
            if rule == RULE_UDP_STORM_ASN:
                return asn in storms
            if rule == RULE_USER_REPORTS_ASN:
                return asn in reports
            return False

        def gap_ok(entry: dict) -> bool:
            last = entry.get('last_change')
            return not last or _minutes_since(last, now) >= MIN_CHANGE_GAP_MIN

        demoted_keys = _auto_keys(auto)
        candidates: List[Tuple[str, str, str, str]] = []   # action, rule, key, evidence
        for key in sorted(set(firing) | demoted_keys | set(targets)):
            entry = targets.get(key) or {
                'bad': 0, 'good': 0, 'last_change': None, 'rule': None}
            if key in demoted_keys:
                auto_entry = _auto_get(auto, key)
                rule = entry.get('rule') or auto_entry.get('reason')
                if rule in frozen_rules:
                    targets[key] = entry            # frozen: no data, no verdict
                    continue
                entry['bad'] = 0
                entry['rule'] = rule
                entry['good'] = 0 if rule_active(rule, key) else int(entry.get('good') or 0) + 1
                targets[key] = entry
                if entry['good'] >= RESTORE_AFTER_GOOD and gap_ok(entry):
                    candidates.append((
                        'restore', rule or '', key,
                        f"{RULE_TITLES_RU.get(rule, rule or '?')}: сигнал молчит "
                        f"{RESTORE_AFTER_GOOD} оценок подряд",
                    ))
            elif key in firing:
                rule, ev = firing[key]
                entry['bad'] = int(entry.get('bad') or 0) + 1
                entry['good'] = 0
                entry['rule'] = rule
                targets[key] = entry
                if entry['bad'] >= DEMOTE_AFTER_BAD and gap_ok(entry):
                    candidates.append(('demote', rule, key, ev))
            else:
                if entry.get('rule') in frozen_rules:
                    targets[key] = entry            # frozen: the bad streak waits
                    continue
                # Quiet and not demoted: the streak is broken. Keep the row
                # only while last_change still matters for the gap rule.
                if entry.get('last_change'):
                    targets[key] = {'bad': 0, 'good': 0,
                                    'last_change': entry['last_change'], 'rule': None}
                else:
                    targets.pop(key, None)

        candidates.sort(key=lambda c: (
            0 if c[0] == 'demote' else 1, RULE_RANK.get(c[1], 99), c[2]))
        changes: List[Change] = []
        for action, rule, key, ev in candidates[:MAX_CHANGES_PER_RUN]:
            scope, asn, proto = parse_target_key(key)
            entry = targets[key]
            if action == 'demote':
                _auto_set(auto, key, {'since': now_iso, 'reason': rule, 'evidence': ev})
                entry.update(bad=0, good=0, last_change=now_iso, rule=rule)
            else:
                _auto_del(auto, key)
                entry.update(bad=0, good=0, last_change=now_iso, rule=None)
            changes.append(Change(scope, asn, proto, action, rule, ev))
        return state, changes

    # ---- applier (I/O out) --------------------------------------------------

    def apply_changes(self, changes: List[Change], state: dict,
                      now: Optional[datetime] = None) -> None:
        """Persist ``state`` (streaks AND cascade_auto — streaks must
        survive runs with no change, or hysteresis never accumulates),
        audit each change, and post one topic message per run."""
        self.save_state(state)
        for ch in changes:
            try:
                self.db.log_admin_action(
                    ACTOR,
                    ACTION_DEMOTE if ch.action == 'demote' else ACTION_RESTORE,
                    target_id=ch.audit_target,
                    details=f"{ch.reason}: {ch.evidence}",
                )
            except Exception as e:
                logger.warning(f"dpi_monitor: audit write failed for {ch.key}: {e}")
        if changes:
            self._notify(changes)

    def _notify(self, changes: List[Change]) -> None:
        """Forum topic only (TOPIC_AI in FORUM_GROUP_ID) — never the
        admin's PM (feedback_no_admin_pm_when_group). Silently skipped
        without a bot (tests, manual runs) or without a group."""
        if self.bot is None:
            return
        group = getattr(self.config, 'FORUM_GROUP_ID', None)
        if not group:
            logger.info("dpi_monitor: no FORUM_GROUP_ID — change not announced")
            return
        kwargs = {
            'chat_id': group,
            'text': format_changes_html(changes),
            'parse_mode': 'HTML',
        }
        topic = getattr(self.config, 'TOPIC_AI', 0) or 0
        if isinstance(topic, int) and topic:
            kwargs['message_thread_id'] = topic
        try:
            self.bot.send_message(**kwargs)
        except Exception as e:
            logger.warning(f"dpi_monitor: topic send failed: {e}")

    # ---- entry point ---------------------------------------------------------

    def run_once(self, dry_run: bool = False,
                 now: Optional[datetime] = None) -> List[Change]:
        """One evaluation. ``dry_run`` evaluates and returns the changes
        WITHOUT writing state, auditing or messaging. ``now`` is a test
        seam; the scheduler passes nothing."""
        if not self.is_enabled():
            logger.info("dpi_monitor: disabled (dpi_monitor_enabled=0) — evaluation skipped")
            return []
        now = now or datetime.utcnow()
        signals = self.collect_signals(now)
        try:
            state = self.load_state()
        except StateUnreadable as e:
            # Not "nothing demoted" — unknown. Skip; the next tick retries.
            logger.warning(f"dpi_monitor: app_settings unreadable ({e}) — run skipped, "
                           f"state and cascade_auto left untouched")
            return []
        new_state, changes = self.evaluate(signals, state, now)
        if dry_run:
            return changes
        self.apply_changes(changes, new_state, now)
        if changes:
            logger.info("dpi_monitor: applied " + '; '.join(
                f"{c.action} {c.key} ({c.reason})" for c in changes))
        else:
            logger.debug(f"dpi_monitor: run {new_state['runs']}, no changes")
        return changes
