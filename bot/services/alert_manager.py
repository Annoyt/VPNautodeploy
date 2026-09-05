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


@dataclass
class Alert:
    key: str               # unique per problem (cpu, container:vpn-bot, …)
    severity: str          # "warn" or "critical"
    title: str             # one-line headline shown in the message
    detail: str = ""       # extra paragraph with diagnostic numbers
    min_cycles: int = DEFAULT_MIN_CYCLES  # consecutive failures to fire


@dataclass
class _Tracker:
    """Per-alert-key state held in memory only."""
    consecutive_fails: int = 0
    last_fired_ts: float = 0.0
    acked_until_ts: float = 0.0
    last_payload: Optional[Alert] = None


# Alert keys with these prefixes are dashboard-only (no Telegram).
# DPI/handshake/probing signals fire constantly — sending each one to
# TOPIC_AI spams the chat. The dashboard "Alerts" tab is the authoritative
# place to review them; daily DPI summary still goes to Telegram.
DASHBOARD_ONLY_PREFIXES = ('dpi_short:', 'dpi_hsfail:', 'dpi_rst:')

# Alert families that get an agent follow-up (see module docstring).
DPI_AGENT_PREFIXES = ('dpi_short:', 'dpi_hsfail:', 'dpi_rst:')
PROTOCOL_AGENT_PREFIX = 'protocol_down:'

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

# The one command the agent must run for a protocol_down alert. It lives
# on the entry HOST (rsync target /opt/vpn-bot), where Hermes runs as
# root — not the bot container path (/app), which the agent cannot see.
PROTOCOL_HEALTHCHECK_CMD = 'python3 /opt/vpn-bot/scripts/protocol_healthcheck.py'

# Telegram hard cap is 4096; leave headroom for the prefix + HTML.
_FOLLOWUP_BODY_LIMIT = 3500


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
        t.last_payload = alert
        if t.consecutive_fails < alert.min_cycles:
            return
        if t.acked_until_ts > now:
            return
        if now - t.last_fired_ts < REPEAT_COOLDOWN_S:
            return
        self._fire(alert, t)
        t.last_fired_ts = now

    def run_once(self) -> None:
        """Run every registered check once. Called by the scheduler.

        A check may return:
          - ``None`` — single check, healthy (single alert key, the one
            the check tracks implicitly).
          - ``Alert`` — single check, problem detected.
          - ``(prefix, [Alert, ...])`` — multi-bucket check; ``prefix``
            is the shared key prefix (e.g. ``"dpi_short:"``). The
            manager will reset trackers whose key starts with that
            prefix but does NOT appear in the returned list — they're
            considered healed and shouldn't carry over consecutive_fails.
        """
        now = time.time()
        for check in self._checks:
            try:
                result = check()
            except Exception as e:
                logger.exception(f"alert check {check.__name__} crashed: {e}")
                continue
            if result is None:
                continue
            # Multi-bucket shape: (prefix, [Alert, ...])
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str):
                prefix, alerts = result
                live_keys = {a.key for a in alerts}
                # Reset trackers under this prefix that weren't reported
                # this cycle. They've healed (or the bucket disappeared).
                for k, tr in list(self._state.items()):
                    if k.startswith(prefix) and k not in live_keys:
                        tr.consecutive_fails = 0
                for alert in alerts:
                    self._process_alert(alert, now)
                continue
            # Single-Alert shape (legacy)
            if isinstance(result, Alert):
                self._process_alert(result, now)

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

        # Agent follow-ups. Both wrappers hand the turn to a worker thread
        # and return at once, and both swallow every failure — an agent
        # outage must never take the pager down with it, and the alert
        # row above is already persisted whatever happens here.
        if alert.key.startswith(DPI_AGENT_PREFIXES):
            self._kick_dpi_agent(alert, alert_db_id)
        elif alert.key.startswith(PROTOCOL_AGENT_PREFIX):
            # The diagnosis goes where the alert went: if the alert was
            # dashboard-only (below ALERT_TG_MIN_SEVERITY), the chat must
            # not receive a follow-up to a headline it never saw.
            self._kick_protocol_agent(
                alert, alert_db_id, post_to_topic=not is_dashboard_only,
            )

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
        (agent not configured / key in flight / no slot). Never raises.
        """
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
        """protocol_down follow-up: stored AND (``post_to_topic``) posted
        to the alert's topic. The prompt pins the agent to
        ``protocol_healthcheck.py`` — the only reader of the two signals
        (probe rows, panel field audit) that actually saw the 2026-09-01
        outage.

        Runs on a daemon thread. The alert tick is a single APScheduler
        job (60 s, max_instances=1): a synchronous 300-s agent turn would
        hold CPU/RAM/disk AND the other protocol_down checks hostage for
        five minutes per dark protocol (two protocols dark = 10 min of
        blind pager) — and a hung Hermes/proxy would do that on every
        fire. The turn is bounded per key: while one is in flight for
        ``alert.key`` a second fire of the same key is skipped (log-only)
        rather than queued. No global slot pool on purpose: there are a
        handful of protocol keys, each fire is a real pager event, and a
        DPI storm holding every slot must not postpone the diagnosis of
        a dead protocol. Returns the started thread, or None when
        nothing was started (agent not configured / already in flight).
        Never raises.
        """
        return self._spawn_agent_worker(
            alert, alert_db_id,
            prompt=_protocol_agent_prompt(alert),
            session_prefix='proto-alert',
            post_to_topic=post_to_topic,
            timeout=PROTOCOL_AGENT_TIMEOUT_S,
            slots=None,
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
        alert itself went to. Failures (agent down, quota, timeout,
        empty reply) are log-only — no chat notice, same as DPI: the
        pager already fired, and ``/protocols`` is the deterministic
        fallback the admin has regardless of the agent.

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
            elif '504' in str(e) or 'timed out' in msg:
                logger.info(
                    f"{tag}: skipping {alert.key} — agent timed out "
                    f"(consider a larger default_timeout)"
                )
            else:
                logger.warning(f"{tag}: agent call failed for {alert.key}: {e}")
            return None

        if not reply:
            logger.info(f"{tag}: empty reply for {alert.key}")
            return None

        # Persist against the alert row — dashboard reads from here.
        if self.db is not None and alert_db_id is not None:
            try:
                with self.db._connect() as conn:
                    conn.execute(
                        "UPDATE alert_history "
                        "SET kimi_analysis = ?, kimi_at = CURRENT_TIMESTAMP "
                        "WHERE id = ?",
                        (reply[:8000], alert_db_id),
                    )
                    conn.commit()
            except Exception as e:
                logger.warning(f"{tag}: DB attach failed: {e}")
        logger.info(f"{tag}: analysis stored for {alert.key} ({len(reply)} chars)")

        if post_to_topic:
            self._post_agent_followup(alert, reply)
        return reply

    def _post_agent_followup(self, alert: Alert, reply: str) -> None:
        """Post the agent's diagnosis to the same place the alert went.
        Never raises — a Telegram hiccup here must not be mistaken for
        an agent failure by the caller."""
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
        f"СЛЕДУЮЩАЯ КОМАНДА: одна точная команда для подтверждения или починки.\n\n"
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
    # read them. This check closes that loop.
    PROBE_RUNS = 3              # consecutive probe runs that must be all-bad
    try:
        from bot.services.health_checker import HealthChecker as _HC
        PROBE_DOMAINS = len(_HC.TARGET_DOMAINS)   # rows per protocol per run
    except Exception:            # keep the alert alive even if that import breaks
        PROBE_DOMAINS = 10
    PROBE_MIN_SAMPLES = 15      # ignore thin windows (partial run, new deploy)
    PROBE_WINDOW_H = 3          # only look at recent rows
    PROBE_STALE_MIN = 45        # 3 missed runs at the 15-min cadence
    PROBE_DEGRADED_OK_RATIO = 0.25   # below a quarter of the 7/10 norm…
    PROBE_DEGRADED_MIN_SAMPLES = 25  # …sustained over ~3 runs, not one dip

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
        proves the tunnel works (5306 of 19535 recent error rows are
        like that — sites that block our exit IP); the 2026-09-01 rows
        had none, because nothing ever connected.

        Two sibling failure modes are deliberately NOT reported as
        per-protocol outages:
          * every protocol dark at once — that is the probe sidecar or
            the exit link, ONE incident, not four;
          * no fresh rows at all — the probe job itself died, which the
            first version of this check could not see (too few samples
            => stay quiet), leaving it blind exactly when blindness was
            total.
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
        except Exception as e:
            # A monitoring read failure is itself worth a line in the
            # log at a level someone will see — this check exists
            # because a silent gap cost four days.
            logger.warning(f"protocol-probe alert read failed: {e}")
            return ("protocol_down:", [])

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
            )])

        def _alive(row):
            """The tunnel carried something: either it succeeded, or it
            failed AFTER a round trip (a latency means bytes came back).
            Only "nothing ever connected" leaves both empty."""
            status, latency = row
            return latency is not None or status == 'ok'

        dark = {
            tag: rows for tag, rows in samples.items()
            if len(rows) >= PROBE_MIN_SAMPLES
            and not any(_alive(r) for r in rows)
        }
        measured = [t for t, rows in samples.items()
                    if len(rows) >= PROBE_MIN_SAMPLES]

        # Everything dark at once = one upstream incident.
        if measured and len(dark) == len(measured) and len(measured) >= 2:
            return ("protocol_down:", [Alert(
                key='protocol_down:all',
                severity='critical',
                min_cycles=2,
                title=f'Все протоколы молчат ({len(measured)} шт.)',
                detail=(
                    "Ни один протокол не отвечает — это не отдельный "
                    "inbound, а общий канал: сайдкар «probe-proxy», линк "
                    "entry→exit или сам exit. Проверь контейнер "
                    "«probe-proxy» и доступность exit-узла."
                ),
            )])

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
        for tag, rows in dark.items():
            since = _last_alive(tag)
            how_long = (
                _humanize_minutes(_minutes_since(since, now)) if since else '—'
            )
            alerts.append(Alert(
                key=f'protocol_down:{tag}',
                severity='critical',
                min_cycles=2,
                title=f'Протокол {tag} мёртв (лежит {how_long})',
                detail=(
                    f"За последние {PROBE_RUNS} прогона ({len(rows)} попыток) "
                    f"через {tag} не вернулся НИ ОДИН ответ — даже ошибочный, "
                    f"то есть туннель не устанавливается вовсе. Остальные "
                    f"протоколы при этом живы, значит дело в самом inbound'е. "
                    f"Частые причины: клиенты потеряли per-protocol поле "
                    f"(flow у Reality, password у Shadowsocks) после правки "
                    f"клиента или месячной джобы; сменился/протух SNI-dest; "
                    f"разъехались hop-порты у hy2; xray отверг конфиг."
                ),
            ))

        # Degraded: the tunnel comes up (rows carry a latency) but almost
        # nothing gets through. 2026-09-05: ws sat at 2/30 ok for an hour
        # — a CF-path slowdown — and nothing paged, because "dark" needs
        # zero alive rows. Baseline is ~7/10; a quarter of that over
        # three runs is not a blip. warn-level (dashboard by default),
        # same key family so the agent hook and the reset logic apply.
        for tag, rows in samples.items():
            if tag in dark or len(rows) < PROBE_DEGRADED_MIN_SAMPLES:
                continue
            ok = sum(1 for s, _l in rows if s == 'ok')
            if ok / len(rows) >= PROBE_DEGRADED_OK_RATIO:
                continue
            alerts.append(Alert(
                key=f'protocol_down:{tag}:degraded',
                severity='warn',
                min_cycles=3,
                title=f'Протокол {tag} деградировал: ok {ok}/{len(rows)}',
                detail=(
                    f"Туннель через {tag} устанавливается, но за последние "
                    f"{PROBE_RUNS} прогона прошло лишь {ok} из {len(rows)} проб "
                    f"при норме ~7/10. Обычно это медленный путь (CF-PoP, "
                    f"перегруженный exit, троттлинг UDP), а не мёртвый "
                    f"inbound. Сравни с остальными протоколами и запусти "
                    f"protocol_healthcheck.py."
                ),
            ))
        return ("protocol_down:", alerts)

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

    checks.extend([
        check_cpu, check_ram, check_disk,
        check_opencode, check_xray_reload_sidecar, check_xui_inbounds,
        check_protocol_probe_down,
        check_dpi_short_sessions, check_dpi_handshake_spike, check_dpi_rst_spike,
    ])
    return checks
