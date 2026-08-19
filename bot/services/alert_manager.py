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
- Always to the super-admin's PM if the alert severity is "critical".

Each message carries an inline keyboard with "✅ ACK" →
``alert_ack:<key>``; ``AlertAckHandler`` records the ack so we
silence that key for 6h.
"""

from __future__ import annotations

import html
import logging
import os
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


class AlertManager:
    """Runs configured checks every cycle, fires Telegram alerts as needed."""

    def __init__(self, bot, config, db=None):
        self.bot = bot
        self.config = config
        self.db = db  # optional — alerts still fire to Telegram if missing
        self._state: Dict[str, _Tracker] = {}
        self._checks: List[Callable[[], Optional[Alert]]] = []

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
            # Forum topic — same place Kimi posts so admin watches one channel.
            topic = getattr(self.config, 'TOPIC_AI', 0) or 0
            group = getattr(self.config, 'FORUM_GROUP_ID', None)
            sent_to_group = False
            if topic and group:
                try:
                    self.bot.send_message(
                        chat_id=group, text=text, parse_mode='HTML',
                        message_thread_id=topic, reply_markup=kb,
                    )
                    sent_to_group = True
                except Exception as e:
                    logger.warning(f"alert: forum send failed: {e}")

            # PM is a FALLBACK for criticals, not a duplicate: while the
            # forum group is configured and reachable the bot must not
            # write to the admin's personal chat (house rule).
            if alert.severity == 'critical' and not sent_to_group:
                admin = getattr(self.config, 'SUPER_ADMIN_ID', None)
                if admin:
                    try:
                        self.bot.send_message(
                            chat_id=admin, text=text, parse_mode='HTML',
                            reply_markup=kb,
                        )
                    except Exception as e:
                        logger.warning(f"alert: PM send failed: {e}")

        logger.info(
            f"ALERT fired: {alert.key} [{alert.severity}] "
            f"{'(dashboard-only) ' if is_dashboard_only else ''}{alert.title}"
        )

        # DPI alerts get a Kimi follow-up — the analysis is stored
        # against the alert row, not posted to chat.
        if alert.key.startswith(('dpi_short:', 'dpi_hsfail:', 'dpi_rst:')):
            self._kick_dpi_agent(alert, alert_db_id)

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

    def _kick_dpi_agent(self, alert: Alert, alert_db_id: Optional[int]) -> None:
        """Ask the OpenCode agent for a DPI-analysis follow-up and attach
        the result to the alert row in alert_history. Result is visible in
        the dashboard's Alerts tab — no chat noise.
        """
        from bot.services.agent_factory import build_agent_client, get_agent_url
        url = get_agent_url(self.config)
        if not url:
            return
        try:
            # 600s (10 min) gives the agent room to finish the multi-step
            # skill: read dpi_metrics rows + grep error.log for the same
            # hour, compare to 7d baseline, render HTML output.
            client = build_agent_client(
                self.config,
                getattr(self.config, 'DB_PATH', '') or '/var/lib/vpn-bot/bot.db',
                default_timeout=600,
            )
            prompt = (
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
            # Fresh session per alert so analyses don't bleed into each
            # other and Kimi starts with a clean slate.
            session_key = f"dpi-alert:{alert.key}:{int(time.time())}"
            reply, _ms = client.ask(
                session_key, prompt, model=None, timeout=600, mode=None,
            )
            if not reply:
                return
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
                    logger.warning(f"dpi-kimi: DB attach failed: {e}")
            logger.info(f"dpi-agent: analysis stored for {alert.key} ({len(reply)} chars)")
        except Exception as e:
            # Quota / rate-limit / time-out are all transient operator
            # conditions, not bugs in our code. Log them at INFO so the
            # noise doesn't drown signal in the dashboard's logs panel.
            msg = str(e).lower()
            if 'rate_limit' in msg or '429' in str(e):
                logger.info(
                    f"dpi-agent: skipping {alert.key} — provider quota exhausted "
                    f"(will retry on next alert tick after refresh)"
                )
            elif '504' in str(e) or 'timed out' in msg:
                logger.info(
                    f"dpi-agent: skipping {alert.key} — agent timed out "
                    f"(consider a larger default_timeout)"
                )
            else:
                logger.warning(f"dpi-agent: agent call failed for {alert.key}: {e}")


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
        check_dpi_short_sessions, check_dpi_handshake_spike, check_dpi_rst_spike,
    ])
    return checks
