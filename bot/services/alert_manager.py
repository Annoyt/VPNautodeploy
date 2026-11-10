"""Periodic health checks → Telegram alerts.

Why
---
We learned about every prod incident only when a user complained.
Disk filling up, container restart-looping, sidecar dead — all
invisible until someone pinged. This service watches the host and
the dependent services and posts a single alert per problem to the
admin (and lets them ack it with an inline button).

Design
------
``AlertManager.run_once()`` is a sync method called by
``NotificationService``'s APScheduler every 60s. Each check returns
either ``None`` (healthy) or an ``Alert`` dict. The manager dedupes
by alert key (``cpu``, ``container:vpn-bot`` etc), tracks how many
consecutive cycles a check has been failing, and only fires once
``min_cycles`` is reached. Cooldown after firing: 30 min for an
unacknowledged repeat, 6 hours after admin acks.

Where alerts go
---------------
- TOPIC_AI in the forum group if ``TOPIC_AI`` is set (this is the
  same topic Kimi posts into — admin's already watching it).
- The super-admin's PM only as a FALLBACK for criticals when the forum
  send failed or no forum is configured (house rule: never PM while the
  group is alive).

Each message carries an inline keyboard with "✅ ACK" →
``alert_ack:<key>``; ``AlertAckHandler`` records the ack so we
silence that key for 6h.

Agent follow-ups
----------------
Two alert families get an automatic AI diagnosis via ``_kick_agent``.
Neither runs on the alert tick: ``_spawn_agent_worker`` hands the turn
to a daemon thread and ``_fire`` returns at once, so the 60-s tick is
never held by Hermes (a hung agent used to blind CPU/RAM/disk and the
protocol pager for up to 10 min per DPI bucket — see the budget block).

- ``dpi_*`` — analysis stored in ``alert_history.kimi_analysis`` for
  the dashboard only (these fire often; chat would drown). Bounded to
  ``DPI_AGENT_MAX_CONCURRENT`` turns at once: a burst of buckets beyond
  that is SKIPPED (log-only), not queued — a dashboard annotation is not
  worth a backlog of 10-min turns, and the key's ``REPEAT_COOLDOWN_S``
  fire will try again if the condition persists.
- ``protocol_down:*`` — critical and rare; the analysis is stored AND
  posted to the same topic as the alert, because the operator asked for
  auto-diagnosis on suspicion and the 2026-09-01 Reality outage showed
  the per-protocol signal needs a human-readable follow-up right where
  the pager fired. The agent is told to run
  ``scripts/protocol_healthcheck.py`` first and reason only from it —
  host-level checks (ports, iptables, containers) all said "alive"
  during that outage. Bounded per key only (there are a handful of
  keys), never by the DPI slot pool — a DPI storm must not delay the
  diagnosis of a dead protocol.

Both families dedupe per alert key: while a turn for ``key`` is in
flight a second fire of the same key is skipped, not stacked.
"""

from __future__ import annotations

import html
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


REPEAT_COOLDOWN_S = 2 * 60 * 60   # 2 h before re-alerting the same unacked key
ACK_COOLDOWN_S = 6 * 60 * 60      # 6 h silence after admin acks
DEFAULT_MIN_CYCLES = 3            # how many consecutive failures before we alert
# An open alert counts as recovered after this many healthy ticks in a row
# (IMPROVEMENT_PLAN F, B3). The probe and connection checks only change
# every 15 min / every report, so five minutes of quiet is a real change.
RECOVERY_TICKS = 5


@dataclass
class Alert:
    key: str               # unique per problem (cpu, container:vpn-bot, …)
    severity: str          # "warn" or "critical"
    title: str             # one-line headline shown in the message
    detail: str = ""       # extra paragraph with diagnostic numbers
    min_cycles: int = DEFAULT_MIN_CYCLES  # consecutive failures to fire


@dataclass
class _Tracker:
    """Per-alert-key state. ``last_fired_ts``, ``acked_until_ts`` and the
    open/notify pair outlive a restart (``alert_state``, B3); the streak
    counters do not — after a restart a check simply counts again."""
    consecutive_fails: int = 0
    last_fired_ts: float = 0.0
    acked_until_ts: float = 0.0
    last_payload: Optional[Alert] = None
    # Fired and not yet recovered; ``notify`` — it went to the chat, so
    # its recovery goes there too. ``healthy_ticks`` counts the quiet run.
    open: bool = False
    notify: bool = False
    open_title: str = ''
    healthy_ticks: int = 0
    # The current (or last) incident: when the key opened and closed, unix
    # seconds. check_inbound_drop keeps those blocks out of its baseline.
    opened_ts: float = 0.0
    closed_ts: float = 0.0


# Alert keys with these prefixes are dashboard-only (no Telegram).
# DPI/handshake/probing signals fire constantly — sending each one to
# TOPIC_AI spams the chat. The dashboard "Alerts" tab is the authoritative
# place to review them; daily DPI summary still goes to Telegram.
DASHBOARD_ONLY_PREFIXES = ('dpi_short:', 'dpi_hsfail:', 'dpi_rst:')

# Alert families that get an agent follow-up (see module docstring).
DPI_AGENT_PREFIXES = ('dpi_short:', 'dpi_hsfail:', 'dpi_rst:')
PROTOCOL_AGENT_PREFIX = 'protocol_down:'
# Users' connections on an exit inbound fell away (B2): the same diagnosis
# turn as protocol_down, the same family for the one-turn-per-tick rule.
INBOUND_DROP_PREFIX = 'inbound_drop:'
PROTOCOL_FAMILY_PREFIXES = (PROTOCOL_AGENT_PREFIX, INBOUND_DROP_PREFIX)
# The healthcheck script's findings, posted by its timer (D1).
HEALTHCHECK_PREFIX = 'healthcheck:'
# Families whose recovery is announced where the alert went (B2/B3).
RECOVERY_PREFIXES = PROTOCOL_FAMILY_PREFIXES + (HEALTHCHECK_PREFIX,)
# Keys of that family that are about our own reporting, not a protocol: no
# agent turn (a 300-s Hermes turn every 2 h while a reporter is down).
AGENT_SKIP_KEYS = ('inbound_drop:no_reports', 'inbound_drop:empty_reports')

# Agent budgets. Both families run on worker threads (see
# _spawn_agent_worker), so a budget is "how long one Hermes turn may
# hold a thread + a session", NOT "how long the alert tick is blind".
# Until 2026-09-05 the DPI call ran synchronously inside the tick —
# APScheduler's default max_instances=1 skips the next 60-s trigger
# while one is running — so every DPI bucket could hold CPU/RAM/disk
# AND the protocol_down pager hostage for the full 10 min, and several
# buckets fire per hour.
#
# DPI keeps the historical 10 min: its skill is multi-step (dpi_metrics
# rows + error.log slice + 7d baseline + HTML render). What replaces the
# tick as its throttle is DPI_AGENT_MAX_CONCURRENT: at most that many
# DPI turns at once against the single Hermes on entry; the rest of a
# burst is skipped (INFO log), not queued — DPI analysis is dashboard
# decoration, and a queue of 10-min turns would still be draining when
# the next hour's buckets arrive. Two = one in-progress + one more for
# a second country/ASN, on a small VPS that also runs the bot; with a
# stuck Hermes that is at most two parked threads, not a pile-up.
#
# protocol_down is a single scripted read, so it gets a tighter budget,
# and is bounded per key only (see module docstring).
DPI_AGENT_TIMEOUT_S = 600
DPI_AGENT_MAX_CONCURRENT = 2
PROTOCOL_AGENT_TIMEOUT_S = 300
# protocol_down / inbound_drop: one turn per tick for everything the tick
# fired, and at most two at once across ticks (a turn from an earlier tick
# may still be running when a second protocol goes dark).
PROTOCOL_AGENT_MAX_CONCURRENT = 2

# The one command the agent must run for a protocol_down alert. It lives
# on the entry HOST (rsync target /opt/vpn-bot), where Hermes runs as
# root — not the bot container path (/app), which the agent cannot see.
PROTOCOL_HEALTHCHECK_CMD = 'python3 /opt/vpn-bot/scripts/protocol_healthcheck.py'

# Telegram hard cap is 4096; leave headroom for the prefix + HTML.
_FOLLOWUP_BODY_LIMIT = 3500

# D2: the agent names one of a fixed list of actions; the bot offers it as
# a button running ordinary code (bot/services/agent_actions.py).
try:
    from bot.services.agent_actions import PROMPT_LINE as AGENT_ACTION_LINE
except Exception:            # the pager must not die with that import
    AGENT_ACTION_LINE = ''


class AlertManager:
    """Runs configured checks every cycle, fires Telegram alerts as needed."""

    def __init__(self, bot, config, db=None):
        self.bot = bot
        self.config = config
        self.db = db  # optional — alerts still fire to Telegram if missing
        self._state: Dict[str, _Tracker] = {}
        self._checks: List[Callable[[], Optional[Alert]]] = []
        # Agent follow-ups run on worker threads (see _spawn_agent_worker).
        # One in-flight agent turn per alert key; the last spawned thread
        # is kept so tests can join it. _agent_lock guards the key set AND
        # the non-blocking slot grab, so "key free + slot free → take
        # both" is atomic and two ticks' worth of fires cannot interleave
        # into a leaked slot.
        self._agent_inflight: set = set()
        self._agent_lock = threading.Lock()
        self._last_agent_thread: Optional[threading.Thread] = None
        # Global bound on concurrent DPI turns (module docstring / budget
        # block). Protocol turns deliberately do NOT draw from this pool.
        self._dpi_agent_slots = threading.Semaphore(DPI_AGENT_MAX_CONCURRENT)
        # The protocol family (protocol_down / inbound_drop) gets one turn
        # per tick (B4, problem 12; _kick_protocol_batch) and at most
        # PROTOCOL_AGENT_MAX_CONCURRENT turns at once across ticks.
        self._protocol_agent_slots = threading.Semaphore(PROTOCOL_AGENT_MAX_CONCURRENT)
        # Alerts of that family fired during the current run_once; kicked
        # as one turn at the end of the tick. None outside run_once.
        self._tick_agent_batch: Optional[list] = None
        # Keys each single-Alert check has reported, so a check that comes
        # back healthy (returns None) resets them (B4, problem 2).
        self._check_keys: Dict[Callable, set] = {}
        self._load_state()

    # ----- persisted state (alert_state, B3) -----

    def _load_state(self) -> None:
        """Trackers from ``alert_state``: cooldowns, acks and open alerts
        survive a restart, so a deploy during an outage neither re-pages
        the same key nor starts another agent turn, and an ack still holds.
        Never raises — no table (an old db), no state."""
        if self.db is None:
            return
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT key, last_fired_ts, acked_until_ts, open, notify, title, "
                    "opened_ts, closed_ts FROM alert_state"
                ).fetchall()
        except Exception as e:
            logger.warning(f"alert state load failed: {e}")
            return
        for key, fired, acked, is_open, notify, title, opened, closed in rows:
            self._state[key] = _Tracker(
                last_fired_ts=float(fired or 0), acked_until_ts=float(acked or 0),
                open=bool(is_open), notify=bool(notify), open_title=title or '',
                opened_ts=float(opened or 0), closed_ts=float(closed or 0),
            )

    def _save_state(self, key: str, t: _Tracker) -> None:
        """Upsert one key's lasting state. Never raises."""
        if self.db is None:
            return
        try:
            with self.db._connect() as conn:
                conn.execute(
                    "INSERT INTO alert_state "
                    "(key, last_fired_ts, acked_until_ts, open, notify, title, "
                    "opened_ts, closed_ts, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "last_fired_ts=excluded.last_fired_ts, "
                    "acked_until_ts=excluded.acked_until_ts, open=excluded.open, "
                    "notify=excluded.notify, title=excluded.title, "
                    "opened_ts=excluded.opened_ts, closed_ts=excluded.closed_ts, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (key, t.last_fired_ts, t.acked_until_ts, int(t.open),
                     int(t.notify), t.open_title, t.opened_ts, t.closed_ts),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"alert state save failed for {key}: {e}")

    # ----- registration -----

    def register(self, check: Callable[[], Optional[Alert]]) -> None:
        self._checks.append(check)

    # ----- ack handling -----

    def ack(self, key: str, *, by: str = '') -> bool:
        """Called by the callback handler when admin presses ✅.

        Also marks all un-acked alert_history rows for this key as
        acknowledged, so the dashboard's Alerts tab reflects the same
        state.
        """
        t = self._state.get(key)
        if t:
            t.acked_until_ts = time.time() + ACK_COOLDOWN_S
            t.consecutive_fails = 0
            self._save_state(key, t)
        if self.db is not None:
            try:
                with self.db._connect() as conn:
                    conn.execute(
                        "UPDATE alert_history "
                        "SET acked_at = CURRENT_TIMESTAMP, acked_by = ? "
                        "WHERE key = ? AND acked_at IS NULL",
                        (by or 'system', key),
                    )
                    conn.commit()
            except Exception as e:
                logger.warning(f"ack DB update failed for {key}: {e}")
        return t is not None

    # ----- main tick -----

    def _process_alert(self, alert: 'Alert', now: float) -> None:
        """Apply tracker bookkeeping for a single alert and fire if needed."""
        t = self._state.setdefault(alert.key, _Tracker())
        t.consecutive_fails += 1
        t.healthy_ticks = 0
        t.last_payload = alert
        if t.consecutive_fails < alert.min_cycles:
            return
        if t.acked_until_ts > now:
            return
        if now - t.last_fired_ts < REPEAT_COOLDOWN_S:
            return
        self._fire(alert, t)
        t.last_fired_ts = now
        self._save_state(alert.key, t)

    def _mark_healthy(self, key: str, t: _Tracker) -> None:
        """A tick on which a check judged ``key`` and did not report it.
        The streak breaks at once; an OPEN alert closes after
        ``RECOVERY_TICKS`` quiet ticks in a row. For the families whose
        recovery is posted (``RECOVERY_PREFIXES``) the close also clears the
        repeat cooldown and the ack: the admin has just been told it is
        over, so a new failure is a new incident and pages at once. Other
        families keep both — a DPI bucket or the disk hovering at its
        threshold must not page and start an agent turn on every flap."""
        t.consecutive_fails = 0
        if not t.open:
            return
        t.healthy_ticks += 1
        if t.healthy_ticks < RECOVERY_TICKS:
            return
        t.open = False
        t.healthy_ticks = 0
        t.closed_ts = time.time()
        announced = key.startswith(RECOVERY_PREFIXES)
        if announced:
            t.last_fired_ts = 0.0
            t.acked_until_ts = 0.0
        notify, title = t.notify, t.open_title
        t.notify = False
        self._save_state(key, t)
        if notify and announced:
            self._deliver_to_admin(
                f"✅ <b>Восстановилось:</b> {html.escape(title or key)}\n"
                f"<code>{html.escape(key)}</code>",
            )
        logger.info(f"ALERT recovered: {key}")

    @staticmethod
    def _judged(judged, key: str) -> bool:
        """Did a multi-bucket check have a verdict for ``key`` this tick?
        ``judged`` None = for every key under its prefix."""
        if judged is None:
            return True
        if callable(judged):
            try:
                return bool(judged(key))
            except Exception:
                return False
        return key in judged

    def run_once(self) -> None:
        """Run every registered check once. Called by the scheduler.

        A check may return:
          - ``None`` — single check, healthy: every key it reported
            before has its streak broken (B4, problem 2 — the streak of a
            single check used to survive healthy ticks, so two failures
            hours apart made "two in a row").
          - ``Alert`` — single check, problem detected; any OTHER key the
            check reported before counts as healthy this tick.
          - ``(prefix, [Alert, ...])`` — multi-bucket check; ``prefix``
            is the shared key prefix (e.g. ``"dpi_short:"``). Trackers
            whose key starts with that prefix but does NOT appear in the
            returned list count as healthy this tick.
          - ``(prefix, [Alert, ...], judged)`` — the same, but only the
            keys in ``judged`` (a set, or a predicate) count as healthy
            when absent; every other key under ``prefix`` had no verdict
            this tick and keeps its state: no healthy tick, no recovery,
            no fire. "Could not look" (a failed read, stale probes, every
            protocol dark at once, a thin window, missing exit reports) is
            never "recovered".

        A check that RAISES says nothing: no key of it changes. An open key
        loaded from ``alert_state`` that no check of this process has spoken
        for heals like any other (``_heal_orphans``). Agent turns of the
        protocol family fired during the tick go out as ONE turn at its end
        (``_kick_protocol_batch``).
        """
        now = time.time()
        self._tick_agent_batch = []
        covered: list = []        # prefixes multi-bucket checks spoke for
        reported: set = set()     # keys single checks reported this tick
        raised = False
        try:
            for check in self._checks:
                try:
                    result = check()
                except Exception as e:
                    raised = True
                    logger.exception(f"alert check {check.__name__} crashed: {e}")
                    continue
                # Multi-bucket shape: (prefix, [Alert, ...][, judged])
                if (isinstance(result, tuple) and len(result) in (2, 3)
                        and isinstance(result[0], str)):
                    prefix, alerts = result[0], result[1]
                    judged = result[2] if len(result) == 3 else None
                    covered.append(prefix)
                    live_keys = {a.key for a in alerts}
                    for k, tr in list(self._state.items()):
                        if (k.startswith(prefix) and k not in live_keys
                                and self._judged(judged, k)):
                            self._mark_healthy(k, tr)
                    for alert in alerts:
                        self._process_alert(alert, now)
                    continue
                # Single shape: an Alert or None.
                single = {result.key} if isinstance(result, Alert) else set()
                reported |= single
                known = self._check_keys.setdefault(check, set())
                for k in known - single:
                    tr = self._state.get(k)
                    if tr is not None:
                        self._mark_healthy(k, tr)
                known |= single
                if isinstance(result, Alert):
                    self._process_alert(result, now)
            if not raised:
                self._heal_orphans(covered, reported)
        finally:
            batch, self._tick_agent_batch = self._tick_agent_batch, None
        if batch:
            self._kick_protocol_batch(batch)

    def _heal_orphans(self, covered: list, reported: set) -> None:
        """An open key loaded from ``alert_state`` that no multi-bucket
        prefix covers and no single check reports: a single check (disk,
        opencode, tg_api…) whose problem healed while the bot was down. Its
        check returns None and knows nothing of the key (``_check_keys`` is
        in memory), so the key stayed open forever. Not called on a tick
        where a check raised — then the silence proves nothing."""
        known = set().union(*self._check_keys.values()) if self._check_keys else set()
        for k, tr in list(self._state.items()):
            if not tr.open or k in reported or k in known:
                continue
            if any(k.startswith(p) for p in covered):
                continue
            self._mark_healthy(k, tr)

    # ----- delivery -----

    def _fire(self, alert: Alert, tracker: _Tracker) -> None:
        # Always persist — dashboard is the source of truth, Telegram
        # just gets the headline for the alerts that need eyeballs now.
        alert_db_id = self._persist_alert(alert)

        # Telegram is for alerts that need eyeballs NOW. By default only
        # criticals are pushed; warns land in the dashboard's Alerts tab
        # (set ALERT_TG_MIN_SEVERITY=warn to push those too).
        min_sev = (getattr(self.config, 'ALERT_TG_MIN_SEVERITY', 'critical') or 'critical').lower()
        is_dashboard_only = (
            alert.key.startswith(DASHBOARD_ONLY_PREFIXES)
            or (min_sev != 'warn' and alert.severity != 'critical')
        )

        prefix = '🔥' if alert.severity == 'critical' else '⚠️'
        # detail is usually a raw exception repr — unescaped '<' breaks
        # Telegram's HTML parser and the alert is never delivered.
        text = f"{prefix} <b>{html.escape(alert.title)}</b>"
        if alert.detail:
            text += f"\n\n<code>{html.escape(alert.detail)}</code>"
        text += (
            f"\n\n<i>Цикл {tracker.consecutive_fails}; "
            f"ack заглушит на 6 ч.</i>"
        )

        if not is_dashboard_only:
            kb = {
                'inline_keyboard': [[
                    {'text': '✅ Понятно', 'callback_data': f'alert_ack:{alert.key}'},
                ]]
            }
            # PM is a FALLBACK for criticals, not a duplicate: while the
            # forum group is configured and reachable the bot must not
            # write to the admin's personal chat (house rule).
            self._deliver_to_admin(
                text, reply_markup=kb,
                pm_fallback=(alert.severity == 'critical'),
            )

        logger.info(
            f"ALERT fired: {alert.key} [{alert.severity}] "
            f"{'(dashboard-only) ' if is_dashboard_only else ''}{alert.title}"
        )

        if not tracker.open:
            tracker.opened_ts = time.time()
        tracker.open = True
        tracker.notify = tracker.notify or not is_dashboard_only
        tracker.open_title = alert.title
        tracker.healthy_ticks = 0

        # Agent follow-ups. Both wrappers hand the turn to a worker thread
        # and return at once, and both swallow every failure — an agent
        # outage must never take the pager down with it, and the alert
        # row above is already persisted whatever happens here.
        if alert.key.startswith(DPI_AGENT_PREFIXES):
            self._kick_dpi_agent(alert, alert_db_id)
        elif (alert.key.startswith(PROTOCOL_FAMILY_PREFIXES)
              and alert.key not in AGENT_SKIP_KEYS):
            # The diagnosis goes where the alert went: if the alert was
            # dashboard-only (below ALERT_TG_MIN_SEVERITY), the chat must
            # not receive a follow-up to a headline it never saw.
            item = (alert, alert_db_id, not is_dashboard_only)
            if self._tick_agent_batch is not None:
                self._tick_agent_batch.append(item)
            else:
                self._kick_protocol_batch([item])

    def _deliver_to_admin(self, text: str, *, reply_markup=None,
                          pm_fallback: bool = False) -> bool:
        """Post ``text`` where the admin watches: TOPIC_AI in the forum
        group. The super-admin's PM is used ONLY when ``pm_fallback`` is
        set and the forum path is unavailable (not configured, or the
        send failed) — never as a duplicate. Returns True if anything
        was delivered. Never raises.

        ``TelegramClient.send_message`` does not raise on a Telegram-side
        failure (HTTP error, ``ok: false`` such as a bad HTML entity or
        the bot kicked from the group) — it logs and returns ``None``.
        A ``None`` is therefore treated as "not delivered" too, so the
        critical fallback still fires in exactly the cases it exists for.
        """
        # Forum topic — same place Kimi posts so admin watches one channel.
        topic = getattr(self.config, 'TOPIC_AI', 0) or 0
        group = getattr(self.config, 'FORUM_GROUP_ID', None)
        if topic and group:
            try:
                res = self.bot.send_message(
                    chat_id=group, text=text, parse_mode='HTML',
                    message_thread_id=topic, reply_markup=reply_markup,
                )
                if res is not None:
                    return True
                logger.warning("alert: forum send returned no message (see client log)")
            except Exception as e:
                logger.warning(f"alert: forum send failed: {e}")
        if not pm_fallback:
            return False
        admin = getattr(self.config, 'SUPER_ADMIN_ID', None)
        if not admin:
            return False
        try:
            res = self.bot.send_message(
                chat_id=admin, text=text, parse_mode='HTML',
                reply_markup=reply_markup,
            )
            if res is not None:
                return True
            logger.warning("alert: PM send returned no message (see client log)")
            return False
        except Exception as e:
            logger.warning(f"alert: PM send failed: {e}")
            return False

    def _persist_alert(self, alert: Alert) -> Optional[int]:
        """Append an alert row to alert_history. Returns the row id or None."""
        if self.db is None:
            return None
        try:
            with self.db._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO alert_history (key, severity, title, detail) "
                    "VALUES (?, ?, ?, ?)",
                    (alert.key, alert.severity, alert.title, alert.detail or ''),
                )
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logger.warning(f"persist_alert failed for {alert.key}: {e}")
            return None

    # ----- agent follow-ups -----

    # How far back a user's footprint still counts as "we have people
    # here". Mobile carriers move a subscriber between ASNs, so
    # users.last_asn alone is a snapshot that can be hours stale —
    # sub_fetches and hy2_auth_log carry the request-time ASN and catch
    # the case where someone is on that carrier right now under a
    # different AS number than the one stored on their row.
    ASN_USER_LOOKBACK_DAYS = 7

    def _asn_has_our_users(self, alert_key: str) -> bool:
        """Does a DPI alert key point at an ASN any of our users sit on?

        A handshake-fail storm on an ASN where we have subscribers means
        "our people are being hit". The same storm on an ASN with none of
        them means "someone is probing us" — a real signal, but a
        standing condition rather than an event, and one no amount of
        agent analysis changes. AS31205 (MegaFon) produced 1073 failures
        and zero successful connections in a day while we had no users on
        it at all; each fire spent a 10-minute Hermes turn to emit ~100
        characters, against a two-slot pool on a VPS with ~190 MB free.

        Fails OPEN: an unparseable key or a DB error returns True, so a
        bug here can only cost us an agent turn, never hide a storm that
        is actually hitting subscribers.
        """
        # dpi_<kind>:<country>:<asn>, e.g. "dpi_hsfail:RU:AS31205".
        parts = (alert_key or '').split(':')
        if len(parts) < 3:
            return True
        asn = parts[-1].strip()
        if not asn or asn == '-':
            return True
        if self.db is None:
            return True
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT 1 FROM users WHERE last_asn = ? LIMIT 1", (asn,)
                ).fetchone()
                if row:
                    return True
                window = f'-{self.ASN_USER_LOOKBACK_DAYS} days'
                for table in ('sub_fetches', 'hy2_auth_log'):
                    row = conn.execute(
                        f"SELECT 1 FROM {table} WHERE asn = ? "
                        f"  AND ts >= datetime('now', ?) LIMIT 1",
                        (asn, window),
                    ).fetchone()
                    if row:
                        return True
        except Exception as e:
            logger.warning(f"asn user lookup failed for {alert_key}: {e}")
            return True
        return False

    def _kick_dpi_agent(self, alert: Alert, alert_db_id: Optional[int]
                        ) -> Optional[threading.Thread]:
        """DPI-analysis follow-up: dashboard-only (stored against the
        alert row, never posted to chat — these fire often).

        Off the tick since 2026-09-05. It used to run synchronously and,
        at 600 s per turn with several buckets an hour, it was the single
        biggest reason the 60-s alert tick went blind for long stretches
        — including for the protocol_down pager that was added precisely
        because nobody noticed a four-day outage. Now a worker thread,
        bounded two ways: one turn per key, and ``DPI_AGENT_MAX_CONCURRENT``
        turns in total (``_dpi_agent_slots``). With no slot free the fire
        is skipped, not queued — the tracker still stamps
        ``last_fired_ts``, so the same key retries on its next fire after
        ``REPEAT_COOLDOWN_S`` if the condition persists, which for a
        dashboard-only annotation is the right amount of trying.
        Returns the started thread, or None when nothing was started
        (agent not configured / no users on the ASN / key in flight /
        no slot). Never raises.
        """
        # Nobody of ours on that ASN => nothing for the agent to diagnose.
        # The alert row is already persisted and still shows in the
        # dashboard; we only decline to spend a 10-min Hermes turn on it.
        if not self._asn_has_our_users(alert.key):
            logger.info(
                f"dpi agent skipped for {alert.key}: no users on that ASN"
            )
            return None
        # 600s (10 min) gives the agent room to finish the multi-step
        # skill: read dpi_metrics rows + grep error.log for the same
        # hour, compare to 7d baseline, render HTML output.
        return self._spawn_agent_worker(
            alert, alert_db_id,
            prompt=_dpi_agent_prompt(alert),
            session_prefix='dpi-alert',
            post_to_topic=False,
            timeout=DPI_AGENT_TIMEOUT_S,
            slots=self._dpi_agent_slots,
        )

    def _kick_protocol_agent(self, alert: Alert, alert_db_id: Optional[int],
                             *, post_to_topic: bool = True
                             ) -> Optional[threading.Thread]:
        """One protocol-family alert's follow-up — a batch of one
        (``_kick_protocol_batch``)."""
        return self._kick_protocol_batch([(alert, alert_db_id, post_to_topic)])

    def _kick_protocol_batch(self, batch: list) -> Optional[threading.Thread]:
        """The diagnosis turn for the protocol-family alerts (protocol_down,
        inbound_drop) one tick fired: ``batch`` = ``[(alert, alert_db_id,
        post_to_topic)]``. ONE turn for all of them (B4, problem 12): the
        prompt pins the agent to ``protocol_healthcheck.py``, which reports
        on every protocol at once — three dark protocols used to start three
        parallel 300-s turns, each with its own ssh to exit, for the same
        answer. The reply is stored against every alert row of the batch and
        posted once, where the alerts went (``post_to_topic`` of any).

        Runs on a daemon thread; ``_protocol_agent_slots`` lets at most two
        such turns run at once (a turn from an earlier tick may still be
        thinking when a protocol goes dark next). The alert tick is a single
        APScheduler job (60 s, max_instances=1): a synchronous turn would
        hold the other checks hostage for minutes. Returns the started
        thread, or None. Never raises."""
        items = [(a, i, p) for a, i, p in (batch or ()) if a is not None]
        if not items:
            return None
        alerts = [a for a, _i, _p in items]
        if len(alerts) == 1:
            lead = alerts[0]
        else:
            # "protocol_down:hy2+stls" — what the prompt and the session
            # key show; the alert rows keep their own keys. A batch of two
            # families names each key in full ("inbound_drop:ws+protocol_down:hy2").
            families = sorted({a.key.split(':', 1)[0] for a in alerts})
            if len(families) == 1:
                tails = sorted({a.key.split(':', 1)[1] for a in alerts})
                lead_key = f"{families[0]}:{'+'.join(tails)}"
            else:
                lead_key = '+'.join(sorted({a.key for a in alerts}))
            lead = Alert(
                key=lead_key,
                severity=('critical' if any(a.severity == 'critical' for a in alerts)
                          else 'warn'),
                title='; '.join(a.title for a in alerts),
                detail='\n'.join(f"[{a.key}] {a.detail}" for a in alerts if a.detail),
            )
        ids = [i for _a, i, _p in items if i is not None]
        return self._spawn_agent_worker(
            lead, ids or None,
            prompt=_protocol_agent_prompt(lead),
            session_prefix='proto-alert',
            post_to_topic=any(p for _a, _i, p in items),
            timeout=PROTOCOL_AGENT_TIMEOUT_S,
            slots=self._protocol_agent_slots,
        )

    def _spawn_agent_worker(self, alert: Alert, alert_db_id: Optional[int], *,
                            prompt: str, session_prefix: str,
                            post_to_topic: bool, timeout: int,
                            slots: Optional[threading.Semaphore]
                            ) -> Optional[threading.Thread]:
        """Run one ``_kick_agent`` turn for ``alert`` on a daemon thread,
        bounded per key and (optionally) by a shared slot pool.

        The shared half of both follow-ups, so the two wrappers cannot
        drift apart on the parts that keep the pager safe:

        * never blocks the caller — the alert tick returns in
          milliseconds whatever Hermes is doing;
        * one turn per ``alert.key`` at a time — a second fire of the
          same key while one is in flight is skipped (INFO), never
          stacked, so a stuck agent parks one thread per key, not one
          per tick;
        * ``slots`` (a ``threading.Semaphore``, or None for "per key
          only") caps the family's concurrency; with no slot free the
          fire is skipped (INFO) rather than queued — the caller's
          tracker stamps ``last_fired_ts`` regardless, so the key retries
          on its next fire after the cooldown;
        * the key and the slot are released in the worker's ``finally``,
          so a raising ``_kick_agent`` (it never should — paranoia) or a
          failed ``Thread.start`` cannot leak them and silence the key
          forever.

        Daemon thread: a parked agent turn must never keep the process
        from exiting on SIGTERM (docker stop has a 10-s grace).

        Returns the started thread, or None when nothing was started
        (agent not configured / key in flight / no slot / could not
        start). Never raises.
        """
        tag = f"{session_prefix}-agent"
        try:
            from bot.services.agent_factory import get_agent_url
            if not get_agent_url(self.config):
                return None
        except Exception as e:
            logger.warning(f"{tag}: agent factory unavailable: {e}")
            return None

        key = alert.key
        # Key check and slot grab under ONE lock so "key free, slot free,
        # take both" is atomic — otherwise two fires racing here could
        # each pass the key check and one of them would leak a slot.
        with self._agent_lock:
            if key in self._agent_inflight:
                logger.info(f"{tag}: {key} already in flight — not re-kicking")
                return None
            if slots is not None and not slots.acquire(blocking=False):
                # INFO, not WARNING: this is the designed throttle doing
                # its job, not a fault — the dashboard log panel must not
                # fill with it during a DPI storm.
                logger.info(
                    f"{tag}: {key} — no free agent slot (family concurrency "
                    f"cap reached); skipped, not queued — the key retries on "
                    f"its next fire after cooldown"
                )
                return None
            self._agent_inflight.add(key)

        def _release() -> None:
            # Idempotent by construction: called exactly once, either from
            # the worker's finally or from the start-failure path below.
            with self._agent_lock:
                self._agent_inflight.discard(key)
            if slots is not None:
                slots.release()

        def _worker() -> None:
            try:
                self._kick_agent(
                    alert, alert_db_id,
                    prompt=prompt,
                    session_prefix=session_prefix,
                    post_to_topic=post_to_topic,
                    timeout=timeout,
                )
            except Exception as e:      # _kick_agent never raises; paranoia
                logger.warning(f"{tag}: worker failed for {key}: {e}")
            finally:
                _release()

        t = threading.Thread(target=_worker, daemon=True,
                             name=f"{session_prefix}-{key}")
        try:
            t.start()
        except Exception as e:
            # RuntimeError("can't start new thread") under memory/thread
            # pressure on the small entry VPS. The worker's finally never
            # ran, so release here — or this key would be "in flight"
            # until the next deploy and its slot gone for good.
            logger.warning(f"{tag}: could not start worker for {key}: {e}")
            _release()
            return None
        self._last_agent_thread = t
        return t

    def _kick_agent(self, alert: Alert, alert_db_id: Optional[int], *,
                    prompt: str, session_prefix: str,
                    post_to_topic: bool, timeout: int) -> Optional[str]:
        """Ask the /ai agent for a follow-up on ``alert`` and attach the
        reply to the alert row (``alert_history.kimi_analysis``), which
        is what the dashboard's Alerts tab renders.

        ``post_to_topic`` additionally posts the reply to the topic the
        alert itself went to. A failure (quota, timeout, error, empty
        reply) then posts one line there instead (B3,
        ``_post_agent_silence``) pointing at ``/protocols``, the
        deterministic view; without ``post_to_topic`` (DPI) it stays
        log-only. ``alert_db_id`` may be a list (a batch).

        Blocking: runs on whatever thread calls it — in production that
        is always a ``_spawn_agent_worker`` daemon thread (both the DPI
        and the protocol hook go through it since 2026-09-05); nothing
        may call this from the alert tick or the polling thread.
        Returns the reply text, or None. Never raises.
        """
        tag = f"{session_prefix}-agent"
        try:
            from bot.services.agent_factory import build_agent_client, get_agent_url
            if not get_agent_url(self.config):
                return None
            client = build_agent_client(
                self.config,
                getattr(self.config, 'DB_PATH', '') or '/var/lib/vpn-bot/bot.db',
                default_timeout=timeout,
            )
            # Fresh session per alert so analyses don't bleed into each
            # other and the agent starts with a clean slate.
            session_key = f"{session_prefix}:{alert.key}:{int(time.time())}"
            reply, _ms = client.ask(
                session_key, prompt, model=None, timeout=timeout, mode=None,
            )
        except Exception as e:
            # Quota / rate-limit / time-out are all transient operator
            # conditions, not bugs in our code. Log them at INFO so the
            # noise doesn't drown signal in the dashboard's logs panel.
            msg = str(e).lower()
            if 'rate_limit' in msg or '429' in str(e):
                logger.info(
                    f"{tag}: skipping {alert.key} — provider quota exhausted "
                    f"(will retry on next alert tick after refresh)"
                )
                why = 'кончилась квота модели'
            elif '504' in str(e) or 'timed out' in msg:
                logger.info(
                    f"{tag}: skipping {alert.key} — agent timed out "
                    f"(consider a larger default_timeout)"
                )
                why = f'не уложился в {timeout} с'
            else:
                logger.warning(f"{tag}: agent call failed for {alert.key}: {e}")
                why = 'ошибка вызова'
            if post_to_topic:
                self._post_agent_silence(alert, why)
            return None

        if not reply:
            logger.info(f"{tag}: empty reply for {alert.key}")
            if post_to_topic:
                self._post_agent_silence(alert, 'пустой ответ')
            return None

        # Persist against the alert row(s) — dashboard reads from here. A
        # batch (_kick_protocol_batch) carries several ids.
        ids = (list(alert_db_id) if isinstance(alert_db_id, (list, tuple))
               else [alert_db_id] if alert_db_id is not None else [])
        if self.db is not None and ids:
            try:
                with self.db._connect() as conn:
                    conn.executemany(
                        "UPDATE alert_history "
                        "SET kimi_analysis = ?, kimi_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        [(reply[:8000], i) for i in ids],
                    )
                    conn.commit()
            except Exception as e:
                logger.warning(f"{tag}: DB attach failed: {e}")
        logger.info(f"{tag}: analysis stored for {alert.key} ({len(reply)} chars)")

        if post_to_topic:
            markup = None
            if session_prefix == 'proto-alert':
                try:
                    from bot.services import agent_actions
                    proposed = agent_actions.parse(reply)
                    if proposed:
                        markup = agent_actions.keyboard(*proposed)
                except Exception as e:
                    logger.warning(f"{tag}: action button skipped: {e}")
            self._post_agent_followup(alert, reply, reply_markup=markup)
        return reply

    def _post_agent_silence(self, alert: Alert, why: str) -> None:
        """One line where the alert went when the agent gave no diagnosis
        (B3): until 2026-11 a failed turn was log-only, and the topic could
        not tell "the agent is still thinking" from "it never will". Points
        at /protocols, the same view without the model. Never raises."""
        try:
            self._deliver_to_admin(
                f"🤖 Агент не ответил по <code>{html.escape(alert.key)}</code>: "
                f"{html.escape(why)}. Сводка без агента — /protocols.",
                pm_fallback=False,
            )
        except Exception as e:
            logger.warning(f"agent silence note failed for {alert.key}: {e}")

    def _post_agent_followup(self, alert: Alert, reply: str, *,
                             reply_markup: Optional[dict] = None) -> None:
        """Post the agent's diagnosis to the same place the alert went,
        with the proposed action's button when there is one (D2). Never
        raises — a Telegram hiccup here must not be mistaken for an agent
        failure by the caller."""
        try:
            head = (f"🤖 <b>Диагностика по алерту</b> "
                    f"<code>{html.escape(alert.key)}</code>:")
            # The agent answers in plain text — a raw '<' kills the HTML
            # parse and the message is silently lost. Escape first, then
            # cut, then drop a half-sliced entity at the cut point.
            body = html.escape(reply.strip())
            if len(body) > _FOLLOWUP_BODY_LIMIT:
                body = body[:_FOLLOWUP_BODY_LIMIT]
                amp = body.rfind('&')
                if amp != -1 and ';' not in body[amp:]:
                    body = body[:amp]
                body += "\n… (обрезано)"
            self._deliver_to_admin(
                f"{head}\n\n{body}",
                reply_markup=reply_markup,
                pm_fallback=(alert.severity == 'critical'),
            )
        except Exception as e:
            logger.warning(f"agent follow-up post failed for {alert.key}: {e}")


def _dpi_agent_prompt(alert: Alert) -> str:
    """Prompt for the DPI follow-up — unchanged wording from the original
    ``_kick_dpi_agent`` so the dpi-analysis skill keeps its contract."""
    return (
        f"DPI ALERT fired: {alert.title}\n\n"
        f"Severity: {alert.severity}\n"
        f"Key: {alert.key}\n\n"
        f"Detail: {alert.detail}\n\n"
        f"Investigate using the dpi-analysis skill. Read the "
        f"relevant dpi_metrics rows for the (country, ASN) "
        f"called out above for the last 1h vs the 7d baseline, "
        f"plus the matching error.log / access.log slices. "
        f"Respond in HTML (no markdown fences), ~1200 chars max, "
        f"following the skill's 'When called from an alert' format."
    )


def _protocol_agent_prompt(alert: Alert) -> str:
    """Prompt for the protocol_down follow-up.

    Wording is deliberate on two counts:
    * it names ONE first action — the healthcheck script — and forbids
      substituting a host walk for it. On 2026-09-04 the agent, asked
      "which protocol is down", spent 105 s on ports/iptables/containers
      and answered "everything alive" while Reality had been dead for
      four days; the probe table and the panel audit are the only
      signals that saw it, and the script reads exactly those.
    * it avoids the incident-response marker words ("инцидент",
      "авария", "outage", "срочно"…) — ``_detect_skill_domains`` gives
      that skill absolute priority, and its broad triage regimen is the
      very behaviour this prompt is steering away from. Likewise
      "сверка" rather than "аудит" ("ауди" is a code-review marker).
      Keep it routed to vpn-ops (see the routing test).
    """
    # The key sits in guillemets: a bare " protocol_down…" trips the
    # code-review " pr" marker (as does " probe-proxy" in the details).
    return (
        f"АЛЕРТ ПО ПРОТОКОЛУ: {alert.title}\n"
        f"Severity: {alert.severity}\n"
        f"Key: «{alert.key}»\n"
        f"Детали: {alert.detail}\n\n"
        f"Ты на entry-хосте под root. ПЕРВЫМ действием выполни ровно эту команду:\n"
        f"  {PROTOCOL_HEALTHCHECK_CMD}\n"
        f"Рассуждай ТОЛЬКО по её выводу. Не подменяй его самостоятельным "
        f"обходом хоста (сокеты, файрвол, список запущенного): 2026-09-01 всё "
        f"это показывало «живо», пока Reality четыре дня не принимал ни одного "
        f"клиента. Падение протокола видят только пробы outbound_health и "
        f"сверка полей клиентов панели — именно их читает скрипт.\n\n"
        f"Формат ответа — plain text, без markdown-заборов и заголовков, "
        f"не длиннее 900 символов, без описания процесса (никаких «проверю», "
        f"«готово», «у меня есть всё»):\n"
        f"ИТОГ: одна строка на протокол (reality, hy2, ws, stls, hy2t) — "
        f"жив / лежит <сколько> / нет данных.\n"
        f"ПОДОЗРЕВАЕМЫЙ: главная причина + улика (процитируй строку вывода скрипта).\n"
        f"СЛЕДУЮЩАЯ КОМАНДА: одна точная команда для подтверждения или починки.\n"
        f"{AGENT_ACTION_LINE}\n\n"
        f"Ничего не меняй сам: никаких рестартов, правок панели, iptables и "
        f"записи в БД без явного OK админа. Если скрипт отсутствует или упал — "
        f"напиши это одной строкой и остановись."
    )


# ====== Concrete checks ======

def build_default_checks(config, bot) -> List[Callable[[], Optional[Alert]]]:
    """Construct the suite of checks for this deployment.

    Each check is a callable with no args (closure over bot/config)
    that returns an Alert or None. Kept separate from AlertManager so
    the tests can plug in fakes.
    """
    checks: List[Callable[[], Optional[Alert]]] = []

    def check_telegram_api() -> Optional[Alert]:
        """Telegram Bot API reachability (via the proxy pool).

        TelegramClient tracks consecutive connection failures into
        TG_API_OUTAGE. Two alerts: ongoing outage (critical) and a
        one-shot recovery notice. The recovery alert rides the normal
        Telegram delivery — it can only be sent once the API is back,
        which is exactly when we want it.
        """
        from bot.core.telegram_client import TG_API_OUTAGE
        now = time.time()
        since = TG_API_OUTAGE.get('since')
        if since and now - since > 180:
            return Alert(
                key='tg_api',
                severity='critical',
                title='Telegram API недоступен',
                detail=(
                    f'Бот не может достучаться до api.telegram.org уже '
                    f'{int(now - since)}с — ни через один прокси. Бот не '
                    f'отвечает пользователям. Проверь tinyproxy на exit и '
                    f'reserve-ноде (systemctl status tinyproxy).'
                ),
                min_cycles=1,
            )
        if TG_API_OUTAGE.get('recovery_pending'):
            TG_API_OUTAGE['recovery_pending'] = False
            dur = int(TG_API_OUTAGE.get('last_duration', 0))
            if dur >= 60:
                return Alert(
                    key='tg_api_recovered',
                    severity='warn',
                    title='Telegram API восстановлен',
                    detail=f'Связь с api.telegram.org вернулась. Простой составил {dur}с.',
                    min_cycles=1,
                )
        return None

    checks.append(check_telegram_api)

    def check_cpu() -> Optional[Alert]:
        try:
            from bot.services.system_stats import SystemStatsService
            stats = SystemStatsService.get_stats() or {}
            pct = (stats.get('cpu') or {}).get('percent', 0)
        except Exception as e:
            return None  # don't alert on monitoring failure
        if pct > 90:
            return Alert(
                key='cpu',
                severity='warn',
                title=f'CPU {pct:.0f}%',
                detail=f'Текущая загрузка: {pct:.1f}%',
            )
        return None

    def check_ram() -> Optional[Alert]:
        try:
            from bot.services.system_stats import SystemStatsService
            stats = SystemStatsService.get_stats() or {}
            r = stats.get('ram') or {}
            pct = r.get('percent', 0)
        except Exception:
            return None
        if pct > 90:
            return Alert(
                key='ram',
                severity='warn',
                title=f'RAM {pct:.0f}%',
                detail=(
                    f"used {r.get('used', 0):.1f} GB / "
                    f"{r.get('total', 0):.1f} GB"
                ),
            )
        return None

    def check_disk() -> Optional[Alert]:
        try:
            from bot.services.system_stats import SystemStatsService
            stats = SystemStatsService.get_stats() or {}
            d = stats.get('disk') or {}
            pct = d.get('percent', 0)
        except Exception:
            return None
        # Disk is special — fire on 1 cycle since it's slow-moving.
        if pct > 95:
            return Alert(
                key='disk',
                severity='critical',
                title=f'DISK {pct:.0f}% — место кончается',
                detail=(
                    f"used {d.get('used', 0):.0f} GB / "
                    f"{d.get('total', 0):.0f} GB · "
                    f"free {d.get('free', 0):.0f} GB"
                ),
                min_cycles=1,
            )
        if pct > 85:
            return Alert(
                key='disk',
                severity='warn',
                title=f'Disk {pct:.0f}%',
                detail=(
                    f"used {d.get('used', 0):.0f} GB / "
                    f"{d.get('total', 0):.0f} GB"
                ),
                min_cycles=1,
            )
        return None

    def check_opencode() -> Optional[Alert]:
        try:
            from bot.services.agent_factory import build_agent_client, get_agent_url
            url = get_agent_url(config)
            if not url:
                return None
            client = build_agent_client(config, config.DB_PATH)
            health = client.ping()
            if health.get('status') == 'ok':
                return None
        except Exception as e:
            return Alert(
                key='opencode',
                severity='warn',
                title='AI-агент не отвечает',
                detail=str(e)[:200],
                min_cycles=2,
            )
        return None

    def check_xray_reload_sidecar() -> Optional[Alert]:
        # "Not configured" is not "offline": the entry node deliberately
        # sets XRAY_RELOAD_URL empty (the 3x-ui API reloads xray itself),
        # and alerting on it fired a bogus critical every cycle.
        if not (os.environ.get('XRAY_RELOAD_URL') or '').strip():
            return None
        try:
            from bot.services.xui_reload import reload_xray_health
            h = reload_xray_health()
            if h is None:
                return Alert(
                    key='xray-reload',
                    severity='critical',
                    title='xray-reload sidecar offline',
                    detail='новые ключи не будут активироваться',
                    min_cycles=2,
                )
            return None
        except Exception:
            return None

    def check_xui_inbounds() -> Optional[Alert]:
        """Catch a wiped/reset 3x-ui panel fast.

        2026-07-19: an unpinned :latest image + a dependency-triggered
        container recreate silently upgraded 3x-ui and reset its DB to
        factory defaults, wiping every inbound. The bot's own startup
        sync already detects this (`XUIService.validate_db_path_sync`)
        but only logs it once at boot — nobody watches raw logs, so it
        went unnoticed. This makes the same condition a recurring,
        paged alert instead.
        """
        db_path = (getattr(config, 'XUI_DB_PATH', '') or '').strip()
        if not db_path or not os.path.exists(db_path):
            return None  # API-only deployments have no local DB to check
        try:
            import sqlite3
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            try:
                row = conn.execute("SELECT COUNT(*) FROM inbounds").fetchone()
            finally:
                conn.close()
            count = row[0] if row else 0
            if count == 0:
                return Alert(
                    key='xui-inbounds',
                    severity='critical',
                    title='X-UI панель без inbound\'ов',
                    detail=(
                        'inbounds пуст — похоже панель сброшена (обновление '
                        'образа / ручной reset). Новые ключи не выдать, '
                        'существующие клиенты могут не работать.'
                    ),
                    min_cycles=2,
                )
            return None
        except Exception:
            return None

    # ---- outbound probe collapse (a protocol went dark) ----
    # 2026-09-01: VLESS-Reality died at 00:01 UTC when a client update
    # blanked `flow` on inbound 1. The probe suite recorded it
    # immediately — reality went from 672/960 ok per day to 0/960 —
    # and the rows sat in outbound_health for FOUR DAYS because nothing
    # read them. This check closes that loop. The numbers and the verdict
    # are bot/services/probe_rules.py (B5): the monitor, /protocols and
    # the agent's script read the same ones.
    from bot.services import probe_rules as _pr
    PROBE_RUNS = _pr.PROBE_RUNS
    PROBE_DOMAINS = _pr.PROBE_DOMAINS
    PROBE_WINDOW_H = _pr.PROBE_WINDOW_H
    PROBE_STALE_MIN = _pr.PROBE_STALE_MIN

    def _minutes_since(ts_str, now):
        """Minutes between an ISO timestamp string and ``now``."""
        from datetime import datetime
        try:
            ts = datetime.fromisoformat(str(ts_str).replace('Z', ''))
        except (TypeError, ValueError):
            return float('inf')
        return max(0.0, (now - ts).total_seconds() / 60.0)

    def _humanize_minutes(mins):
        if mins == float('inf'):
            return '—'
        mins = int(mins)
        if mins < 60:
            return f"{mins} мин"
        hours, mins = divmod(mins, 60)
        if hours < 24:
            return f"{hours} ч {mins:02d} мин"
        days, hours = divmod(hours, 24)
        return f"{days} д {hours} ч"

    def check_protocol_probe_down():
        """Per protocol: no sign of life across the last N probe runs.

        Liveness is "a latency came back", not ``status == 'ok'``. An
        error that travelled THROUGH the tunnel carries a latency and
        proves the tunnel works; the 2026-09-01 rows had none, because
        nothing ever connected.

        Two sibling failure modes are deliberately NOT reported as
        per-protocol outages:
          * every cascade protocol dark at once — that is the probe
            sidecar or the exit link, ONE incident, not four (the DE
            reserve does not count: it is meant to outlive exit);
          * no fresh rows at all — the probe job itself died, which the
            first version of this check could not see (too few samples
            => stay quiet), leaving it blind exactly when blindness was
            total.

        Protocols the operator switched off in the cascade are left alone
        (B4, problem 7): Turbo is off while its instance and probe stay
        up, and its dark probe would page critical every two hours. A
        switched-off protocol that still answers does prove the channel
        works, so it rules "all dark" out. The DE reserve, when dark, pages
        as the reserve (B6). Every alert carries the line with all
        protocols' numbers (B3).

        Returns the judged set too: only keys this run had a verdict for can
        heal. A failed read, stale probes, every protocol dark at once and a
        thin window are "could not tell" — an open per-protocol alert keeps
        its state through them instead of posting «Восстановилось».
        """
        try:
            db_path = getattr(config, 'DB_PATH', None) or '/var/lib/vpn-bot/bot.db'
            import sqlite3
            from datetime import datetime, timedelta
            now = datetime.utcnow()
            cutoff = (now - timedelta(hours=PROBE_WINDOW_H)).isoformat()
            limit = PROBE_RUNS * PROBE_DOMAINS
            with sqlite3.connect(db_path) as conn:
                newest = conn.execute(
                    "SELECT MAX(ts) FROM outbound_health"
                ).fetchone()[0]
                tags = [
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT outbound_tag FROM outbound_health "
                        "WHERE ts >= ?", (cutoff,),
                    ).fetchall()
                ]
                samples = {}
                for tag in tags:
                    samples[tag] = conn.execute(
                        "SELECT status, latency_ms FROM outbound_health "
                        "WHERE outbound_tag = ? AND ts >= ? "
                        "ORDER BY ts DESC LIMIT ?",
                        (tag, cutoff, limit),
                    ).fetchall()
                order = conn.execute(
                    "SELECT value FROM app_settings WHERE key = 'cascade_protocol_order'"
                ).fetchone()
        except Exception as e:
            # A monitoring read failure is itself worth a line in the
            # log at a level someone will see — this check exists
            # because a silent gap cost four days.
            logger.warning(f"protocol-probe alert read failed: {e}")
            return ("protocol_down:", [], frozenset())

        # The probe pipeline itself stopped producing rows.
        if not newest or _minutes_since(newest, now) > PROBE_STALE_MIN:
            age = '—' if not newest else f"{int(_minutes_since(newest, now))} мин"
            return ("protocol_down:", [Alert(
                key='protocol_down:probe_pipeline',
                severity='critical',
                min_cycles=2,
                title=f'Пробы не пишутся {age}',
                # «probe-proxy» in guillemets on purpose: this text rides
                # in the agent prompt, and a bare " probe" trips the
                # code-review " pr" marker (see _protocol_agent_prompt).
                detail=(
                    "HealthChecker перестал складывать результаты в "
                    "outbound_health. Пока это так, падение любого "
                    "протокола НЕ будет замечено. Смотри джобу проб в "
                    "логах бота и контейнер «probe-proxy»."
                ),
            )], frozenset())

        disabled = _pr.operator_disabled(order[0] if order else None)
        windows = {tag: _pr.judge_window(rows) for tag, rows in samples.items()}
        live = {t: w for t, w in windows.items() if t not in disabled}
        summary = _pr.summary_line(sorted(windows.items()), disabled)
        cascade = {t: w for t, w in live.items() if t != _pr.RESERVE_TAG}
        dark = {t for t, w in live.items() if w['verdict'] == _pr.VERDICT_DARK}
        measured = [t for t, w in cascade.items() if w['verdict'] != _pr.VERDICT_THIN]
        # Answers through a protocol the operator switched off still prove
        # the channel up (the reserve does not: it is meant to outlive exit).
        alive_switched_off = any(
            w['verdict'] not in (_pr.VERDICT_DARK, _pr.VERDICT_THIN)
            for t, w in windows.items() if t in disabled and t != _pr.RESERVE_TAG)

        def _keys(tag):
            return {f'protocol_down:{tag}', f'protocol_down:{tag}:degraded'}

        judged = {'protocol_down:probe_pipeline'}
        for tag in disabled:
            judged |= _keys(tag)               # switched off: nothing to page
        for tag, w in live.items():
            if w['verdict'] != _pr.VERDICT_THIN:
                judged |= _keys(tag)
        if len(measured) >= 2 or alive_switched_off:
            judged.add('protocol_down:all')

        # Every cascade protocol dark at once = one upstream incident; the
        # per-protocol keys have no verdict meanwhile.
        if (measured and len(measured) >= 2 and all(t in dark for t in measured)
                and not alive_switched_off):
            return ("protocol_down:", [Alert(
                key='protocol_down:all',
                severity='critical',
                min_cycles=2,
                title=f'Все протоколы молчат ({len(measured)} шт.)',
                detail=(
                    "Ни один протокол не отвечает — это не отдельный "
                    "inbound, а общий канал: сайдкар «probe-proxy», линк "
                    "entry→exit или сам exit. Проверь контейнер "
                    "«probe-proxy» и доступность exit-узла.\n"
                    f"Сейчас по пробам: {summary}."
                ),
            )], frozenset({'protocol_down:probe_pipeline'}))

        # "How long" — anchored on the last row that showed life, since
        # in some failure modes rows stop being written at all. Looked
        # up only for the (rare) dark tags: the healthy path is one
        # query per tick, not one per protocol.
        def _last_alive(tag):
            try:
                with sqlite3.connect(db_path) as conn:
                    return conn.execute(
                        "SELECT MAX(ts) FROM outbound_health WHERE "
                        "outbound_tag = ? AND (latency_ms IS NOT NULL "
                        "OR status = 'ok')",
                        (tag,),
                    ).fetchone()[0]
            except Exception:
                return None

        alerts = []
        for tag in sorted(dark):
            w = windows[tag]
            since = _last_alive(tag)
            how_long = (
                _humanize_minutes(_minutes_since(since, now)) if since else '—'
            )
            if tag == _pr.RESERVE_TAG:
                alerts.append(Alert(
                    key=f'protocol_down:{tag}',
                    severity='critical',
                    min_cycles=2,
                    title=f'Резерв DE не отвечает (лежит {how_long})',
                    detail=(
                        f"За последние {PROBE_RUNS} прогона ({w['n']} попыток) "
                        f"через резервный узел DE не вернулся ни один ответ. "
                        f"Платным он нужен, когда лежит основной путь; "
                        f"проверь x-ui и Reality на резервном узле.\n"
                        f"Сейчас по пробам: {summary}."
                    ),
                ))
                continue
            alerts.append(Alert(
                key=f'protocol_down:{tag}',
                severity='critical',
                min_cycles=2,
                title=f'Протокол {tag} мёртв (лежит {how_long})',
                detail=(
                    f"За последние {PROBE_RUNS} прогона ({w['n']} попыток) "
                    f"через {tag} не вернулся НИ ОДИН ответ — даже ошибочный, "
                    f"то есть туннель не устанавливается вовсе. Остальные "
                    f"протоколы при этом живы, значит дело в самом inbound'е. "
                    f"Частые причины: клиенты потеряли per-protocol поле "
                    f"(flow у Reality, password у Shadowsocks) после правки "
                    f"клиента или месячной джобы; сменился/протух SNI-dest; "
                    f"разъехались hop-порты у hy2; xray отверг конфиг.\n"
                    f"Сейчас по пробам: {summary}."
                ),
            ))

        # Degraded: the tunnel comes up (rows carry a latency) but almost
        # nothing gets through. 2026-09-05: ws sat at 2/30 ok for an hour
        # — a CF-path slowdown — and nothing paged, because "dark" needs
        # zero alive rows. warn-level (dashboard by default), same key
        # family so the agent hook and the reset logic apply.
        for tag, w in sorted(live.items()):
            if w['verdict'] != _pr.VERDICT_DEGRADED:
                continue
            alerts.append(Alert(
                key=f'protocol_down:{tag}:degraded',
                severity='warn',
                min_cycles=3,
                title=f"Протокол {tag} деградировал: ok {w['ok']}/{w['n']}",
                detail=(
                    f"Туннель через {tag} устанавливается, но за последние "
                    f"{PROBE_RUNS} прогона прошло лишь {w['ok']} из {w['n']} "
                    f"проб ({_pr.degraded_threshold_text()}). Обычно это "
                    f"медленный путь (CF-PoP, перегруженный exit, троттлинг "
                    f"UDP), а не мёртвый inbound. Сравни с остальными "
                    f"протоколами и запусти protocol_healthcheck.py.\n"
                    f"Сейчас по пробам: {summary}."
                ),
            ))
        return ("protocol_down:", alerts, frozenset(judged))

    # ---- DPI / fingerprint checks (Phase C) ----
    # All three checks share the same DB read pattern: roll up the last
    # hour of dpi_metrics by (country, ASN) and look for anomalies vs
    # the 7-day baseline. Cheap (<50ms on a 30-day window of 5-min
    # snapshots) so we just re-do it each tick.
    DPI_WINDOW_H = 1
    DPI_BASELINE_DAYS = 7
    DPI_MIN_CONNS = 50          # avoid alerting on tiny samples
    DPI_SHORT_RATIO_WARN = 0.40 # 40% short_sessions in window
    DPI_HSFAIL_BASELINE_X = 5   # 5× baseline = warn
    DPI_RST_SPIKE_PCT = 200     # +200% above baseline = warn

    def _read_dpi_buckets():
        """Return list of dicts for last DPI_WINDOW_H, plus baseline lookup."""
        try:
            db_path = getattr(config, 'DB_PATH', None) or '/var/lib/vpn-bot/bot.db'
            import sqlite3
            from datetime import datetime, timedelta
            cutoff = (datetime.utcnow() - timedelta(hours=DPI_WINDOW_H)).isoformat()
            bl_cutoff = (datetime.utcnow() - timedelta(days=DPI_BASELINE_DAYS)).isoformat()
            with sqlite3.connect(db_path) as conn:
                cur_rows = conn.execute(
                    "SELECT country, asn, MAX(as_org), "
                    "SUM(conn_count), SUM(short_session_count), "
                    "SUM(handshake_fail_count), SUM(rst_count) "
                    "FROM dpi_metrics WHERE snapshot_at >= ? "
                    "GROUP BY country, asn", (cutoff,),
                ).fetchall()
                bl_rows = conn.execute(
                    "SELECT country, asn, "
                    "SUM(conn_count), SUM(short_session_count), "
                    "SUM(handshake_fail_count), SUM(rst_count) "
                    "FROM dpi_metrics WHERE snapshot_at >= ? "
                    "GROUP BY country, asn", (bl_cutoff,),
                ).fetchall()
        except Exception as e:
            logger.debug(f"dpi-alert read failed: {e}")
            return [], {}
        bl_hours = DPI_BASELINE_DAYS * 24
        baseline = {
            (r[0], r[1]): {
                'conn_h':   (r[2] or 0) / bl_hours,
                'short_h':  (r[3] or 0) / bl_hours,
                'hsfail_h': (r[4] or 0) / bl_hours,
                'rst_h':    (r[5] or 0) / bl_hours,
            }
            for r in bl_rows
        }
        cur = [
            {
                'country': r[0], 'asn': r[1], 'as_org': r[2],
                'conn_count': r[3] or 0,
                'short_session_count': r[4] or 0,
                'hs_fail_count': r[5] or 0,
                'rst_count': r[6] or 0,
            }
            for r in cur_rows
        ]
        return cur, baseline

    def check_dpi_short_sessions():
        """Per-(country, ASN): too many short sessions in last hour."""
        cur, _bl = _read_dpi_buckets()
        alerts = []
        for b in cur:
            if b['country'] == '*GLOBAL*':
                continue
            if not (b['country'] or '').strip():
                # Unattributable sources (geoip miss) are internet
                # scanners, not a cohort of real users — alerting on
                # them produced daily noise and a paid Kimi run each
                # time. Real DPI shows up under a real country/ASN.
                continue
            if b['conn_count'] < DPI_MIN_CONNS:
                continue
            ratio = b['short_session_count'] / b['conn_count']
            if ratio < DPI_SHORT_RATIO_WARN:
                continue
            key = f"dpi_short:{b['country']}:{b['asn'] or '-'}"
            severity = 'critical' if ratio > 0.7 else 'warn'
            alerts.append(Alert(
                key=key, severity=severity, min_cycles=2,
                title=f"DPI? {b['country']}/{b['asn']} {int(ratio * 100)}% short sessions",
                detail=(
                    f"{b['as_org']} · {b['short_session_count']}/{b['conn_count']} "
                    f"коротких сессий за час. Юзеров режут — рассмотри переключение "
                    f"когорты на резервный inbound или ротацию entry-IP."
                ),
            ))
        return ("dpi_short:", alerts)

    def check_dpi_handshake_spike():
        """Per-(country, ASN): handshake fails > Nx baseline."""
        cur, bl = _read_dpi_buckets()
        alerts = []
        for b in cur:
            if b['country'] == '*GLOBAL*':
                continue
            if not (b['country'] or '').strip():
                # Geoip-less bucket = scanners; see check_dpi_short_sessions.
                continue
            fails_h = b['hs_fail_count'] / DPI_WINDOW_H
            base = (bl.get((b['country'], b['asn'])) or {}).get('hsfail_h', 0)
            if fails_h < 5:
                continue  # absolute floor — ignore tiny absolute counts
            if b['conn_count'] < 1 and fails_h < 30:
                # Handshake fails with ZERO real connections in the
                # bucket is a scanner poking the port, not DPI squeezing
                # users. Only a massive burst is still worth a look.
                continue
            if base <= 0:
                ratio = float('inf') if fails_h > 20 else 0
            else:
                ratio = fails_h / base
            if ratio < DPI_HSFAIL_BASELINE_X:
                continue
            key = f"dpi_hsfail:{b['country']}:{b['asn'] or '-'}"
            severity = 'critical' if ratio > 20 else 'warn'
            r_disp = '∞' if ratio == float('inf') else f"{ratio:.1f}"
            alerts.append(Alert(
                key=key, severity=severity, min_cycles=2,
                title=f"Probing? {b['country']}/{b['asn']} hsfail {int(fails_h)}/h ({r_disp}× baseline)",
                detail=(
                    f"{b['as_org']} · {b['hs_fail_count']} REALITY handshake "
                    f"failures за последний час. Active probing или сканер. "
                    f"Загляни в /api/admin/dpi_metrics для IP."
                ),
            ))
        return ("dpi_hsfail:", alerts)

    def check_dpi_rst_spike():
        """Host-wide TCP abort delta significantly above baseline."""
        cur, bl = _read_dpi_buckets()
        global_row = next(
            (b for b in cur if b['country'] == '*GLOBAL*'), None,
        )
        if not global_row:
            return ("dpi_rst:", [])
        rst_h = global_row['rst_count'] / DPI_WINDOW_H
        base = (bl.get(('*GLOBAL*', None)) or {}).get('rst_h', 0)
        # Absolute floor — random RSTs from misbehaving NAT etc happen.
        if rst_h < 200:
            return ("dpi_rst:", [])
        if base > 0 and (rst_h / base) < (1 + DPI_RST_SPIKE_PCT / 100):
            return ("dpi_rst:", [])
        ratio = rst_h / base if base > 0 else float('inf')
        r_disp = '∞' if ratio == float('inf') else f"{ratio:.1f}"
        return ("dpi_rst:", [Alert(
            key='dpi_rst:global', severity='warn', min_cycles=2,
            title=f"TCP RST spike: {int(rst_h)}/h ({r_disp}× baseline)",
            detail=(
                f"Host-wide TCP aborts резко выше нормы. "
                f"Может быть DPI режет коннекты, NAT flapping, или "
                f"проблема внутри Xray."
            ),
        )])

    # ---- users' connections on an exit inbound fell away (B2) ----
    # 2026-10-29 .. 11-02: ws connections on exit went from 250-300 an hour
    # to zero for 3.5 days while the probes, which leave from the entry
    # datacenter, saw ws at ~89 % — users' networks are invisible to them.
    # The exit's own count of accepted connections is not. Backtested on
    # 30 days of prod dpi_metrics (2026-11-07): exactly one hit, that ws
    # outage, no false ones on reality / ss2022 / hy2. Re-run on this very
    # code (2026-11-10, 23 days past the 7-day warm-up): with three blocks
    # (6 h) it also fired for one hour each on reality 29.10, ss2022 08.11
    # and cf-ws 09.11; with four blocks (8 h) only the ws outage, caught at
    # 22:00 on 29.10. Only an alert, never a DPIMonitor rule: a demotion
    # moves users off the protocol, its connections fall, and the rule
    # would hold itself.
    INBOUND_DROP_BLOCK_H = 2         # one block = two hours
    INBOUND_DROP_BLOCKS = 4          # four low blocks in a row = 8 h
    INBOUND_DROP_BASELINE_DAYS = 7   # the same clock block on each of 7 days
    # The baseline is the 7 most recent USABLE days of the same clock block,
    # looked for up to 21 days back (dpi_metrics keeps 30); fewer than 4 =
    # no verdict. Not usable: a block the exit did not report (it is not
    # zero connections), and a block of the key's current or last incident
    # (alert_state opened_ts .. closed_ts, plus the 8 h that opened it) —
    # the outage's own zeros dragged the median to 0, and an 84-h ws outage
    # would have "recovered" on day four.
    INBOUND_DROP_BASELINE_MAX_DAYS = 21
    INBOUND_DROP_BASELINE_MIN_DAYS = 4
    INBOUND_DROP_MIN_MEDIAN = 50     # too quiet an inbound proves nothing
    INBOUND_DROP_RATIO = 0.2         # below a fifth of its usual count
    # A block counts only if the exit reported through most of it: the
    # reporter posts every 5 min, 24 times a block. Fewer = "no report",
    # never "no connections".
    INBOUND_DROP_MIN_REPORTS = 12
    INBOUND_DROP_REPORT_STALE_MIN = 30
    INBOUND_DROP_RECOMPUTE_S = 600   # the data changes every 5 min
    # dpi_metrics tag → protocol name. hy2-8400 is the bot's own rollup of
    # hy2 auth allows (no exit report behind it): its blocks count only
    # while the bot itself was running.
    INBOUND_DROP_TAGS = {'reality': 'reality', 'cf-ws': 'ws', 'ss2022': 'stls',
                         'xhttp': 'xhttp', 'hy2-8400': 'hy2', 'xhr': 'xhr'}
    _BOT_TAGS = ('hy2-8400',)
    # 'started': when this process began (the hy2 rollup has no heartbeat
    # but the bot itself); a test seam like the rest of the cache.
    _drop_cache = {'at': 0.0, 'result': None, 'started': time.time()}

    def _median(values):
        vals = sorted(values)
        if not vals:
            return 0.0
        mid = len(vals) // 2
        return float(vals[mid]) if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2.0

    def check_inbound_drop():
        """Per exit inbound: users' accepted connections over the last
        8 h, in four 2-h blocks, each below a fifth of the median of the
        same block on the previous 7 days (median ≥ 50). Critical, one
        cycle (the 8 h are the hysteresis), kicks the protocol agent, and
        its recovery is posted (AlertManager, RECOVERY_PREFIXES).

        "The exit did not report" is told from "nobody connected": a block
        with fewer than INBOUND_DROP_MIN_REPORTS reports (the exit_reports
        heartbeat, or exit-tagged dpi_metrics snapshots before it existed)
        gives no verdict, and the key is left out of the judged set, so an
        alert already open stays as it was — across a restart too (hy2's
        blocks have no verdict for 8 h after the bot starts). Baseline
        blocks without reports, and those of the key's own incident, are
        skipped. A reporter silent for INBOUND_DROP_REPORT_STALE_MIN pages
        its own warn, ``inbound_drop:no_reports`` (only once a report has
        ever come); eight hours of reports that carry no rows at all page
        ``inbound_drop:empty_reports`` instead of every inbound at once.
        Recomputed every 10 min."""
        now_ts = time.time()
        if (_drop_cache['result'] is not None
                and now_ts - _drop_cache['at'] < INBOUND_DROP_RECOMPUTE_S):
            return _drop_cache['result']
        try:
            db_path = getattr(config, 'DB_PATH', None) or '/var/lib/vpn-bot/bot.db'
            import sqlite3
            from datetime import datetime, timedelta
            now = datetime.utcnow()
            now_iso = now.isoformat()
            per_day = 24 // INBOUND_DROP_BLOCK_H
            window_s = INBOUND_DROP_BLOCK_H * INBOUND_DROP_BLOCKS * 3600
            horizon = now_ts - (INBOUND_DROP_BASELINE_MAX_DAYS + 1) * 86400
            slot = (f"CAST((julianday(?) - julianday({{col}})) * {per_day} AS INTEGER)")
            tags = tuple(INBOUND_DROP_TAGS)
            marks = ','.join('?' * len(tags))
            with sqlite3.connect(db_path) as conn:
                incidents = {}
                try:
                    for key, is_open, opened, closed in conn.execute(
                            "SELECT key, open, opened_ts, closed_ts FROM alert_state "
                            "WHERE key LIKE 'inbound_drop:%'"):
                        opened = float(opened or 0)
                        end = now_ts if is_open else float(closed or 0)
                        if opened and end > opened and end > horizon:
                            incidents[key] = (opened - window_s, end)
                except sqlite3.Error:
                    incidents = {}
                back_days = INBOUND_DROP_BASELINE_MAX_DAYS
                span = INBOUND_DROP_BLOCKS + per_day * back_days
                since = (now - timedelta(hours=INBOUND_DROP_BLOCK_H * span)).isoformat()
                counts = conn.execute(
                    f"SELECT inbound_tag, {slot.format(col='snapshot_at')} AS s, "
                    f"SUM(conn_count) FROM dpi_metrics "
                    f"WHERE snapshot_at >= ? AND inbound_tag IN ({marks}) "
                    f"GROUP BY inbound_tag, s",
                    (now_iso, since, *tags),
                ).fetchall()
                beats = dict(conn.execute(
                    f"SELECT {slot.format(col='ts')} AS s, COUNT(*) FROM exit_reports "
                    f"WHERE ts >= ? GROUP BY s",
                    (now_iso, since),
                ).fetchall())
                snaps = dict(conn.execute(
                    f"SELECT {slot.format(col='snapshot_at')} AS s, "
                    f"COUNT(DISTINCT snapshot_at) FROM dpi_metrics "
                    f"WHERE snapshot_at >= ? AND inbound_tag IN ({marks}) "
                    f"AND inbound_tag NOT IN ({','.join('?' * len(_BOT_TAGS))}) "
                    f"GROUP BY s",
                    (now_iso, since, *tags, *_BOT_TAGS),
                ).fetchall())
                newest_beat = conn.execute(
                    "SELECT MAX(ts) FROM exit_reports").fetchone()[0]
                newest_snap = conn.execute(
                    f"SELECT MAX(snapshot_at) FROM dpi_metrics WHERE inbound_tag IN ({marks}) "
                    f"AND inbound_tag NOT IN ({','.join('?' * len(_BOT_TAGS))})",
                    (*tags, *_BOT_TAGS),
                ).fetchone()[0]
                window_cut = (now - timedelta(seconds=window_s)).isoformat()
                n_window, rows_window = conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(rows), 0) FROM exit_reports WHERE ts >= ?",
                    (window_cut,),
                ).fetchone()
        except Exception as e:
            logger.warning(f"inbound-drop alert read failed: {e}")
            return ("inbound_drop:", [], frozenset())   # no verdict on anything

        conns: dict = {}
        for tag, s_idx, total in counts:
            conns.setdefault(tag, {})[int(s_idx)] = int(total or 0)
        block_s = INBOUND_DROP_BLOCK_H * 3600

        def exit_reported(j) -> bool:
            return max(beats.get(j, 0), snaps.get(j, 0)) >= INBOUND_DROP_MIN_REPORTS

        def reported(tag, i) -> bool:
            if tag in _BOT_TAGS:
                block_start = now_ts - (i + 1) * block_s
                return block_start >= _drop_cache['started']
            return exit_reported(i)

        def in_incident(key, j) -> bool:
            inc = incidents.get(key)
            if not inc:
                return False
            start, end = now_ts - (j + 1) * block_s, now_ts - j * block_s
            return start < inc[1] and end > inc[0]

        def usual_for(tag, key, series, i):
            """Median of the same clock block on the 7 most recent usable
            days, or None (too little history: no verdict)."""
            vals = []
            for d in range(1, back_days + 1):
                j = i + per_day * d
                if in_incident(key, j):
                    continue
                if tag not in _BOT_TAGS and not exit_reported(j):
                    continue
                vals.append(series.get(j, 0))
                if len(vals) == INBOUND_DROP_BASELINE_DAYS:
                    break
            if len(vals) < INBOUND_DROP_BASELINE_MIN_DAYS:
                return None
            return _median(vals)

        # Eight hours of reports, none of them with a single row: the
        # reporter reads nothing (log path moved, access log off), it is
        # not every user of every inbound leaving at once.
        empty = (int(n_window or 0) >= INBOUND_DROP_MIN_REPORTS * INBOUND_DROP_BLOCKS
                 and int(rows_window or 0) == 0)

        alerts = []
        judged = {'inbound_drop:no_reports', 'inbound_drop:empty_reports'}
        for tag, proto in INBOUND_DROP_TAGS.items():
            key = f'inbound_drop:{proto}'
            bot_tag = tag in _BOT_TAGS
            if empty and not bot_tag:
                continue
            if not all(reported(tag, i) for i in range(INBOUND_DROP_BLOCKS)):
                continue                       # no verdict: the key keeps its state
            series = conns.get(tag, {})
            blocks = []
            for i in range(INBOUND_DROP_BLOCKS):
                usual = usual_for(tag, key, series, i)
                if usual is None:
                    break
                blocks.append((series.get(i, 0), usual))
            if len(blocks) < INBOUND_DROP_BLOCKS:
                continue
            judged.add(key)
            if not all(usual >= INBOUND_DROP_MIN_MEDIAN and got < INBOUND_DROP_RATIO * usual
                       for got, usual in blocks):
                continue
            got_total = sum(g for g, _u in blocks)
            usual_total = int(sum(u for _g, u in blocks))
            per_block = ' / '.join(f"{g} из ~{int(u)}" for g, u in reversed(blocks))
            hours = INBOUND_DROP_BLOCK_H * INBOUND_DROP_BLOCKS
            if bot_tag:
                title = (f"Авторизации {proto} упали: {got_total} за {hours} ч "
                         f"при обычных ~{usual_total}")
                detail = (
                    f"{proto} по журналу авторизаций бота (hy2_auth_log): по 2 ч, от старых "
                    f"к новым — {per_block} (обычное = медиана того же часа за "
                    f"{INBOUND_DROP_BASELINE_DAYS} дней). Бот работал все эти часы, то есть "
                    f"это не пробел в журнале, а пользователи перестали доходить до {proto}. "
                    f"Пробы из датацентра такое не видят: блокировка у операторов, на участке "
                    f"«пользователь → entry». Сравни с /protocols и с подключениями инбаундов."
                )
            else:
                title = (f"Подключения {proto} на exit упали: {got_total} за {hours} ч "
                         f"при обычных ~{usual_total}")
                detail = (
                    f"Инбаунд {tag}: по 2 ч, от старых к новым — {per_block} "
                    f"(обычное = медиана того же часа за {INBOUND_DROP_BASELINE_DAYS} дней). "
                    f"Отчёты exit приходят, то есть это не обрыв отчёта, а пользователи "
                    f"перестали доходить. Пробы из датацентра такое не видят: блокировка "
                    f"у операторов, на участке «пользователь → entry/Cloudflare». "
                    f"Сравни с /protocols и с подключениями других инбаундов."
                )
            alerts.append(Alert(key=key, severity='critical', min_cycles=1,
                                title=title, detail=detail))

        newest = max((t for t in (newest_beat, newest_snap) if t), default=None)
        # No report ever = the reporter is not installed: nothing to say.
        if newest and _minutes_since(newest, now) > INBOUND_DROP_REPORT_STALE_MIN:
            age = _humanize_minutes(_minutes_since(newest, now))
            alerts.append(Alert(
                key='inbound_drop:no_reports',
                severity='warn',
                min_cycles=2,
                title=f'Отчёты exit не приходят ({age})',
                detail=(
                    "exit_dpi_reporter.py на exit не присылал отчёт дольше "
                    f"{INBOUND_DROP_REPORT_STALE_MIN} мин. Пока так, алерт по "
                    "подключениям на инбаундах слеп. Таймер exit-dpi-reporter "
                    "на exit, токен DPI_REPORT_TOKEN, доступ exit → entry :8080."
                ),
            ))
        if empty:
            alerts.append(Alert(
                key='inbound_drop:empty_reports',
                severity='warn',
                min_cycles=2,
                title=f'Отчёты exit пустые {INBOUND_DROP_BLOCK_H * INBOUND_DROP_BLOCKS} ч',
                detail=(
                    f"За {INBOUND_DROP_BLOCK_H * INBOUND_DROP_BLOCKS} ч пришло {int(n_window)} "
                    "отчётов exit, и ни в одном нет ни строки подключений. Скорее всего "
                    "exit_dpi_reporter.py ничего не читает (сменился путь access-лога, "
                    "лог выключен), а не все пользователи ушли разом: алерт по "
                    "подключениям на инбаундах exit пока молчит. Проверь access-лог xray "
                    "на exit и /protocols."
                ),
            ))
        result = ("inbound_drop:", alerts, frozenset(judged))
        _drop_cache['at'] = now_ts
        _drop_cache['result'] = result
        return result

    check_inbound_drop.cache = _drop_cache       # test seam

    # ---- the healthcheck script on its timer (D1) ----
    HEALTHCHECK_STALE_MIN = 45     # the timer runs every 15 min
    HEALTHCHECK_BLIND_RUNS = 3     # a layer that could not look three runs in a row
    HEALTHCHECK_FIXED = ('healthcheck:stale', 'healthcheck:blind')

    def _finding_layer(f: dict) -> str:
        """Which layer a finding came from: the script says so; an older copy
        of it that does not is read by the key."""
        layer = f.get('layer') if isinstance(f, dict) else None
        if layer in ('a', 'b', 'c'):
            return layer
        key = str((f or {}).get('key') or '')
        if key == 'panel_audit':
            return 'a'
        if key.startswith('entry_dnat:') or key == 'unit:shadow-tls':
            return 'b'
        return 'c'

    def _key_layer(key: str) -> str:
        return _finding_layer({'key': key[len(HEALTHCHECK_PREFIX):]})

    def check_healthcheck():
        """What ``scripts/protocol_healthcheck.py --post`` found on its last
        run on entry (IMPROVEMENT_PLAN F, D1): its ``findings`` — the Reality
        dest certificate, entry DNAT, exit hop rules, the hysteria units,
        shadow-tls, the panel's client fields — each an alert
        ``healthcheck:<key>``, before the probes go dark.

        "Could not look" is never "fixed" (AlertManager's judged set): a
        report older than HEALTHCHECK_STALE_MIN pages ``healthcheck:stale``
        and judges no finding, and a finding of a layer that could not look
        this run (ssh to exit failed…) keeps its alert as it is. A layer that
        could not look HEALTHCHECK_BLIND_RUNS runs in a row pages
        ``healthcheck:blind``. Both are critical: a dead adviser is the one
        thing nobody would otherwise notice. No report ever = the timer is
        not installed: nothing to say. A failed read judges nothing. No
        agent turn: the findings are the script's own."""
        try:
            db_path = getattr(config, 'DB_PATH', None) or '/var/lib/vpn-bot/bot.db'
            import json as _json
            import sqlite3
            from datetime import datetime
            now = datetime.utcnow()
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT ts, exit_code, report_json FROM healthcheck_reports "
                    "ORDER BY id DESC LIMIT ?", (HEALTHCHECK_BLIND_RUNS,)).fetchall()
        except Exception as e:
            logger.warning(f"healthcheck alert read failed: {e}")
            return (HEALTHCHECK_PREFIX, [], frozenset())
        if not rows:
            return (HEALTHCHECK_PREFIX, [])

        def parsed(raw):
            try:
                data = _json.loads(raw or '{}')
            except (TypeError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

        reports = [parsed(raw) for _ts, _code, raw in rows]
        newest_ts = rows[0][0]
        age = _minutes_since(newest_ts, now)
        if age > HEALTHCHECK_STALE_MIN:
            return (HEALTHCHECK_PREFIX, [Alert(
                key='healthcheck:stale', severity='critical', min_cycles=2,
                title=f'Скрипт проверки не присылал отчёт {_humanize_minutes(age)}',
                detail=('Таймер protocol-healthcheck на entry не отработал. Пока так, '
                        'сертификат Reality-dest, DNAT и юниты hysteria не проверяются, '
                        'а его прежние находки остаются как были. '
                        'systemctl status protocol-healthcheck.timer'),
            )], frozenset({'healthcheck:blind'}))

        def looked(report) -> set:
            layers = report.get('layers') or {}
            return {name for name in ('a', 'b', 'c') if layers.get(name) == 'ok'}

        alerts = []
        newest = reports[0]
        ok_now = looked(newest)
        if len(reports) >= HEALTHCHECK_BLIND_RUNS:
            blind = [name for name in ('a', 'b', 'c')
                     if all(name not in looked(r) for r in reports)]
            if blind:
                names = {'a': 'A (bot.db)', 'b': 'B (entry)', 'c': 'C (exit)'}
                why = '; '.join(f"{names[n]}: {str((newest.get('layers') or {}).get(n) or '—')[:120]}"
                                for n in blind)
                alerts.append(Alert(
                    key='healthcheck:blind', severity='critical', min_cycles=1,
                    title=(f'Скрипт проверки {HEALTHCHECK_BLIND_RUNS} раза подряд не смог '
                           f'посмотреть слой {", ".join(names[n] for n in blind)}'),
                    detail=(f'{why}. Находки этого слоя пока не проверяются и остаются как '
                            f'были. journalctl -u protocol-healthcheck -n 40'),
                ))
        for f in (newest.get('findings') or []):
            if not isinstance(f, dict) or not f.get('key'):
                continue
            severity = 'critical' if f.get('severity') == 'critical' else 'warn'
            alerts.append(Alert(
                key=f"healthcheck:{str(f['key'])[:60]}", severity=severity, min_cycles=1,
                title=str(f.get('title') or f['key'])[:200],
                detail=f"Следующая команда: {str(f.get('next') or '—')[:400]}",
            ))

        def judged(key: str) -> bool:
            if key in HEALTHCHECK_FIXED:
                return True
            return _key_layer(key) in ok_now

        return (HEALTHCHECK_PREFIX, alerts, judged)

    def check_lockdown_active():
        """LOCKDOWN (IMPROVEMENT_PLAN B1, bot/services/lockdown.py) is
        a standing change to what every user receives — fronted-first
        cascade, all DNS through the tunnel — and it can be switched on
        by a probe pattern at 3 a.m. So while it is on, the operator
        is reminded: critical (forum topic + ACK button) the first
        cycle, then once per REPEAT_COOLDOWN_S (2 h) until it is off.

        Multi-bucket shape on purpose: ``("lockdown:", [])`` while
        inactive resets the tracker (the single-Alert shape's ``None``
        does not), so an off→on later starts from zero again.
        """
        try:
            from bot.services.lockdown import SETTING_KEY, parse_lockdown
            db_path = getattr(config, 'DB_PATH', None) or '/var/lib/vpn-bot/bot.db'
            import sqlite3
            with sqlite3.connect(db_path) as conn:
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?", (SETTING_KEY,),
                ).fetchone()
            state = parse_lockdown(row[0] if row else None)
        except Exception as e:
            # Same stance as the probe check: a monitoring read failure
            # is logged where someone will see it, never paged.
            logger.warning(f"lockdown alert read failed: {e}")
            return ("lockdown:", [])
        if not state.get('active'):
            return ("lockdown:", [])
        since = str(state.get('since') or '')
        since_s = f"{since[:10]} {since[11:16]} UTC" if len(since) >= 16 else (since or '—')
        reason = state.get('reason') or '—'
        return ("lockdown:", [Alert(
            key='lockdown:active',
            severity='critical',
            min_cycles=1,
            title=f"LOCKDOWN активен ({state.get('by')}, с {since_s})",
            detail=(
                f"Причина: {reason}. Режим: {state.get('mode')}. Юзерам "
                f"выдаётся каскад CF-фронт → TCP → UDP и весь DNS через "
                f"туннель. Снять: /lockdown off, статус: /lockdown."
            ),
        )])

    checks.extend([
        check_cpu, check_ram, check_disk,
        check_opencode, check_xray_reload_sidecar, check_xui_inbounds,
        check_protocol_probe_down, check_inbound_drop, check_healthcheck,
        check_dpi_short_sessions, check_dpi_handshake_spike, check_dpi_rst_spike,
        check_lockdown_active,
    ])
    return checks
