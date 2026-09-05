"""Operational admin commands: status, protocols, find, recent, repair, topics, quota, expire, whoami.

These cover the gap between the dashboard (rich UI, mouse-friendly) and
inline ops where you just need a quick text answer in the same forum
topic — e.g. "what's the bot's RAM right now", "find user with uuid
starting with abc", "set @ivan's quota to 50", "which protocol is dead".
"""

import html
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from bot.config.constants import BYTES_PER_GB
from .base import AdminHandlerBase

logger = logging.getLogger(__name__)


def _fmt_bytes(n: float) -> str:
    """Render bytes as GB / MB / KB."""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{int(n)} B"


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


# ----- /protocols helpers -----
# Twins of the closures inside alert_manager.build_default_checks (they
# are not importable from there). Keep the semantics identical: the card
# and the pager must agree on what "alive" and "how long" mean.

def _probe_alive(row) -> bool:
    """LIVENESS RULE. The tunnel carried something if the probe either
    succeeded or failed AFTER a round trip (a latency means bytes came
    back — e.g. an HTTP 418 from a site that blocks our exit IP). Only
    "nothing ever connected" leaves both empty, which is what the
    2026-09-01 outage rows looked like."""
    status, latency = row[0], row[1]
    return latency is not None or status == 'ok'


def _minutes_since(ts_str, now: datetime) -> float:
    """Minutes between an ISO-UTC-naive timestamp string and ``now``;
    ``inf`` when unparsable so callers can treat it as 'never'."""
    try:
        ts = datetime.fromisoformat(str(ts_str).replace('Z', ''))
    except (TypeError, ValueError):
        return float('inf')
    return max(0.0, (now - ts).total_seconds() / 60.0)


def _humanize_minutes(mins: float) -> str:
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


def _format_probe_line(tag: str, info: Optional[dict], now: datetime,
                       *, window_h: int, min_samples: int) -> str:
    """One card line per protocol. States, in order of severity:

    🔴 no sign of life across the whole window (≥ ``min_samples`` rows,
       none alive) — this is what the pager calls protocol_down;
    🟡 the LAST run had no answer at all, or fewer than half its probes
       succeeded — degraded / possibly the first minutes of an outage;
    🟢 otherwise (7/10 is the everyday baseline — RU domestic targets
       fail through the tunnel by design);
    ⚪ no rows in the window at all.
    """
    if not info:
        return f"⚪ <b>{tag}</b> — нет проб за {window_h} ч"
    rate = f"{info['last_ok']}/{info['last_n']} ok"
    if info['alive_window'] == 0 and info['rows'] >= min_samples:
        since = info.get('last_alive_ts')
        how_long = (_humanize_minutes(_minutes_since(since, now))
                    if since else 'всё время наблюдений')
        return (f"🔴 <b>{tag}</b> — {rate}, лежит {how_long} "
                f"(ни одного ответа за {info['rows']} попыток)")
    if info['last_alive'] == 0:
        return f"🟡 <b>{tag}</b> — {rate}, последний прогон без единого ответа"
    if info['last_ok'] * 2 < info['last_n']:
        return f"🟡 <b>{tag}</b> — {rate}, деградация"
    return f"🟢 <b>{tag}</b> — {rate}"


class AdminOpsMixin(AdminHandlerBase):
    """In-chat ops commands."""

    # ----- /status -----

    def show_status(self, chat_id: str, args: list) -> None:
        """Compact health card: services, host metrics, user counts.

        Per-source indicators:
        ✓ = source available
        ✗ = source failed (error shown inline)
        """
        from bot.services.system_stats import SystemStatsService

        # Track data source status for display
        source_status = {}

        # System stats
        sys_stats = {}
        sys_error = None
        try:
            sys_stats = SystemStatsService.get_stats() or {}
            source_status['sys'] = '✓'
        except Exception as e:
            sys_error = str(e)
            source_status['sys'] = f'✗ ({type(e).__name__})'
            logger.warning(f"/status: system_stats failed: {e}")

        cpu = (sys_stats.get('cpu') or {}).get('percent', '—')
        ram = sys_stats.get('ram') or {}
        ram_pct = ram.get('percent', '—')
        ram_used = ram.get('used', 0)
        ram_total = ram.get('total', 0)
        disk = sys_stats.get('disk') or {}
        disk_pct = disk.get('percent', '—')
        disk_used = disk.get('used', 0)
        disk_total = disk.get('total', 0)
        uptime = _fmt_uptime(sys_stats.get('uptime', 0))

        # Service checks — anything that has a sync ping endpoint
        xui = self.bot.services.get('xui') if hasattr(self.bot, 'services') else None
        # DB mode or API mode both count as "X-UI reachable" — the old
        # db-only check showed "missing" forever on the entry node.
        xui_ok = bool(xui and (getattr(xui, 'db', None)
                               or getattr(xui, 'api', None)))
        source_status['xui_db'] = '✓' if xui_ok else '✗'

        ai_status = '—'
        ai_error = None
        try:
            from bot.services.agent_client import AgentUnavailable
            from bot.services.agent_factory import build_agent_client, get_agent_url
            ocu = get_agent_url(self.config)
            if ocu:
                client = build_agent_client(self.config, self.config.DB_PATH)
                try:
                    h = client.ping()
                    ai_status = h.get('status', '?')
                    source_status['ai'] = '✓'
                except AgentUnavailable as e:
                    ai_status = f'down'
                    ai_error = str(e)
                    source_status['ai'] = f'✗'
            else:
                source_status['ai'] = '—'
        except Exception as e:
            ai_status = f'err'
            ai_error = str(e)
            source_status['ai'] = f'✗'
            logger.warning(f"/status: opencode check failed: {e}")

        # User counts from bot DB
        try:
            counts = {}
            for row in self.db.get_stats().get('by_status', {}).items():
                counts[row[0]] = row[1]
            source_status['bot_db'] = '✓'
        except Exception as e:
            counts = {}
            source_status['bot_db'] = f'✗ ({type(e).__name__})'
            logger.warning(f"/status: bot db stats failed: {e}")

        active = counts.get('demo', 0) + counts.get('paid', 0) + counts.get('support_topic', 0)
        pending = counts.get('pending_demo', 0) + counts.get('platform_select', 0)
        total = sum(counts.values()) if counts else 0

        # Build lines with source indicators
        lines = [
            "🩺 <b>Status</b>",
            f"• Bot: <b>up</b> · Uptime <code>{uptime}</code> {source_status.get('sys', '?')}",
            f"• X-UI: {'<b>ok</b>' if xui_ok else '<b>missing</b>'} {source_status.get('xui_db', '?')}",
            f"• OpenCode: <b>{ai_status}</b> {source_status.get('ai', '?')}",
        ]

        # Add agent error details if available
        if ai_error:
            lines.append(f"  <i>OpenCode error: {ai_error[:60]}</i>")

        # System metrics — hide if completely unavailable
        if sys_error and not sys_stats:
            lines.append(f"💻 System metrics: <i>{sys_error[:60]}</i>")
        else:
            lines.append(
                f"💻 CPU <b>{cpu}%</b> · RAM <b>{ram_pct}%</b> "
                f"({ram_used:.1f}/{ram_total:.1f} GB) · "
                f"Disk <b>{disk_pct}%</b> ({disk_used:.0f}/{disk_total:.0f} GB)"
            )

        lines.append("")  # blank line

        # User counts — show error summary if unavailable
        if '✗' in source_status.get('bot_db', ''):
            lines.append(f"👥 Users: <i>{source_status['bot_db']}</i>")
        else:
            lines.append(
                f"👥 Users: <b>{total}</b> total · <b>{active}</b> active "
                f"(demo {counts.get('demo', 0)} / paid {counts.get('paid', 0)} / "
                f"support {counts.get('support_topic', 0)}) · "
                f"<b>{pending}</b> pending · <b>{counts.get('rejected', 0)}</b> rejected · "
                f"<b>{counts.get('banned', 0)}</b> banned {source_status.get('bot_db', '')}"
            )

        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /protocols -----

    # Window/threshold twins of check_protocol_probe_down in
    # bot/services/alert_manager.py — the card and the pager must agree
    # on what "dark" means, or the admin sees a red line the pager never
    # fired on (or vice versa). Class attrs so tests can shrink them.
    PROTO_RUNS = 3              # probe runs shown per protocol
    PROTO_WINDOW_H = 3          # only rows this recent count
    PROTO_STALE_MIN = 45        # 3 missed runs at the 15-min cadence
    PROTO_MIN_SAMPLES = 15      # fewer rows than this cannot be "DOWN"
    PROTO_ALERT_HOURS = 6       # alert_history lookback
    PROTO_AUDIT_TIMEOUT_S = 40  # panel-audit subprocess budget

    def show_protocols(self, chat_id: str, args: list) -> "threading.Thread":
        """Deterministic per-protocol health card — no LLM involved, so
        it works when the agent is down (which is exactly when you need
        it). Three read-only sources:

        * ``outbound_health`` — the probe rows, judged by the LIVENESS
          RULE (see ``_probe_alive``);
        * the panel field audit (``scripts/verify_panel_client_fields.py``)
          — catches a blanked ``flow``/``password`` on the panel, i.e.
          the 2026-09-01 root cause, independently of the probes;
        * ``alert_history`` — what the pager already said in the last 6 h.

        The audit is a subprocess with a 40-s budget and handlers run on
        the polling thread, so the whole card is built on a worker and
        posted afterwards (same pattern as /addmail). The topic id is
        captured up front: ``_current_update`` may point at a different
        command by the time the worker finishes. The thread is returned
        so tests can join it; the dispatcher ignores the value.
        """
        thread_id = self._get_thread_id(chat_id)

        def _worker() -> None:
            try:
                text = self._build_protocols_report()
            except Exception as e:      # belt and braces — must always answer
                logger.exception("/protocols failed")
                text = f"❌ /protocols: {html.escape(str(e))[:200]}"
            # Same discipline as AIHandler._reply: a Telegram hiccup on a
            # worker thread must be logged, not left as an unhandled
            # thread exception on stderr.
            try:
                self._send(chat_id=chat_id, text=text, parse_mode='HTML',
                           message_thread_id=thread_id)
            except Exception as e:
                logger.warning(f"/protocols: reply send failed: {e}")

        t = threading.Thread(target=_worker, daemon=True,
                             name=f"protocols-{chat_id}")
        t.start()
        return t

    def _build_protocols_report(self) -> str:
        now = datetime.utcnow()
        lines = ["📡 <b>Протоколы</b>"]

        try:
            probes = self._read_probe_state(now)
        except Exception as e:
            logger.warning(f"/protocols: outbound_health read failed: {e}")
            probes = None

        if probes is None:
            lines.append("⚠️ outbound_health недоступна — состояние проб неизвестно")
        else:
            lines.extend(self._probe_header_lines(probes['newest'], now))
            for tag in probes['tags']:
                lines.append(_format_probe_line(
                    tag, probes['per_tag'].get(tag), now,
                    window_h=self.PROTO_WINDOW_H,
                    min_samples=self.PROTO_MIN_SAMPLES,
                ))
        # The probe sidecar has no hy2t inbound (ports 18081-18084 are
        # reality/hy2/ws/stls) — say so instead of showing a blank.
        lines.append("⚪ <b>hy2t</b>: без проб, см. /ai")
        lines.append("")
        lines.append(self._panel_audit_line())
        lines.append("")
        lines.extend(self._recent_alert_lines(now))

        text = "\n".join(lines)
        if len(text) > 3900:  # Telegram cap is 4096
            text = text[:3900] + "\n…(обрезано)"
        return text

    def _read_probe_state(self, now: datetime) -> dict:
        """Newest ``PROTO_RUNS`` runs per protocol from outbound_health.

        A "run" is one row per target domain (HealthChecker writes them
        with per-row timestamps, so runs are recovered by count, exactly
        as the alert check does). Tags come from HealthChecker so a
        protocol with NO rows at all still gets a line.
        """
        try:
            from bot.services.health_checker import HealthChecker as _HC
            expected = list(_HC.PROTOCOL_TAGS)
            per_run = len(_HC.TARGET_DOMAINS)
        except Exception:      # keep the card alive even if that import breaks
            expected, per_run = ['reality', 'hy2', 'ws', 'stls'], 10

        cutoff = (now - timedelta(hours=self.PROTO_WINDOW_H)).isoformat()
        limit = self.PROTO_RUNS * per_run
        per_tag: dict = {}
        with self.db._connect() as conn:
            newest = conn.execute(
                "SELECT MAX(ts) FROM outbound_health").fetchone()[0]
            seen = [r[0] for r in conn.execute(
                "SELECT DISTINCT outbound_tag FROM outbound_health WHERE ts >= ?",
                (cutoff,)).fetchall()]
            tags = expected + sorted(t for t in seen if t not in expected)
            for tag in tags:
                rows = conn.execute(
                    "SELECT status, latency_ms, ts FROM outbound_health "
                    "WHERE outbound_tag = ? AND ts >= ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (tag, cutoff, limit),
                ).fetchall()
                if not rows:
                    continue
                last_run = rows[:per_run]
                info = {
                    'rows': len(rows),
                    'alive_window': sum(1 for r in rows if _probe_alive(r)),
                    'last_n': len(last_run),
                    'last_ok': sum(1 for r in last_run if r[0] == 'ok'),
                    'last_alive': sum(1 for r in last_run if _probe_alive(r)),
                    'last_alive_ts': None,
                }
                if info['alive_window'] == 0:
                    # "How long" is anchored on the last row that showed
                    # life — looked up only for dark tags (rare).
                    info['last_alive_ts'] = conn.execute(
                        "SELECT MAX(ts) FROM outbound_health WHERE "
                        "outbound_tag = ? AND (latency_ms IS NOT NULL "
                        "OR status = 'ok')",
                        (tag,),
                    ).fetchone()[0]
                per_tag[tag] = info
        return {'newest': newest, 'tags': tags, 'per_tag': per_tag}

    def _probe_header_lines(self, newest, now: datetime) -> list:
        """Staleness first: rows that stopped arriving mean the probe
        pipeline died, and every per-protocol line below is then history,
        not status — the first version of the pager was blind to this."""
        if not newest:
            return ["⚠️ outbound_health пуст — пробы ни разу не писались"]
        age = _minutes_since(newest, now)
        if age > self.PROTO_STALE_MIN:
            return [f"⚠️ пробы не пишутся уже {_humanize_minutes(age)} — "
                    f"HealthChecker или probe-proxy встали, строки ниже устарели"]
        return [f"<i>последний прогон {str(newest)[11:16]} UTC "
                f"({int(age)} мин назад) · норма 7/10: vk/yandex/sber "
                f"не ходят через туннель</i>"]

    def _run_panel_audit(self):
        """Run scripts/verify_panel_client_fields.py as a subprocess.

        Returns ``(returncode, first_lines)``; returncode is None when
        the script could not be run at all (missing, timed out, failed
        to spawn). The script is not importable (scripts/ is not a
        package) and needs the panel creds from this process's env, so
        it runs with our environment plus PYTHONPATH=<app root> — that
        is /app in the container and the repo root on a dev box.
        """
        root = Path(__file__).resolve().parents[3]
        script = root / 'scripts' / 'verify_panel_client_fields.py'
        if not script.exists():
            return None, f"скрипт не найден: {script}"
        env = dict(os.environ)
        env['PYTHONPATH'] = (f"{root}{os.pathsep}{env['PYTHONPATH']}"
                             if env.get('PYTHONPATH') else str(root))
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True, text=True,
                timeout=self.PROTO_AUDIT_TIMEOUT_S, env=env,
            )
        except subprocess.TimeoutExpired:
            return None, (f"таймаут {self.PROTO_AUDIT_TIMEOUT_S} с "
                          f"(панель не отвечает?)")
        except Exception as e:
            return None, f"не запустился: {e}"
        out = (proc.stdout or '').strip() or (proc.stderr or '').strip()[-300:]
        return proc.returncode, out

    def _panel_audit_line(self) -> str:
        try:
            rc, out = self._run_panel_audit()
        except Exception as e:      # _run_panel_audit is defensive; this is paranoia
            rc, out = None, f"ошибка: {e}"
        text_lines = [ln.strip() for ln in (out or '').splitlines() if ln.strip()]
        first = text_lines[0] if text_lines else ''
        if rc == 0:
            return f"✅ Аудит панели: {html.escape(first or 'OK')}"
        if rc == 1:
            # Show the headline plus the first few offenders — enough to
            # see WHICH inbound/field, without pasting 80 lines.
            body = html.escape(first or 'BROKEN')
            for p in text_lines[1:4]:
                body += f"\n   <code>{html.escape(p)}</code>"
            return f"🔴 Аудит панели: {body}"
        # rc == 2 (script says it cannot check) or None (we could not run it).
        reason = first or out or '?'
        return f"⚠️ Аудит панели: не удалось проверить — {html.escape(reason)[:200]}"

    def _recent_alert_lines(self, now: datetime) -> list:
        """What the pager already said about protocols/DPI recently, so
        the admin does not have to scroll the topic to correlate."""
        # fired_at defaults to CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS'),
        # so the cutoff must use the same shape for the string compare.
        cutoff = (now - timedelta(hours=self.PROTO_ALERT_HOURS)
                  ).strftime('%Y-%m-%d %H:%M:%S')
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT fired_at, key, title, acked_at, kimi_at "
                    "FROM alert_history "
                    "WHERE (key LIKE 'protocol_down:%' OR key LIKE 'dpi_%') "
                    "AND fired_at >= ? "
                    "ORDER BY fired_at DESC, id DESC LIMIT 8",
                    (cutoff,),
                ).fetchall()
        except Exception as e:
            return [f"⚠️ alert_history недоступна: {html.escape(str(e))[:80]}"]
        if not rows:
            return [f"🔔 Алертов protocol_down / dpi за {self.PROTO_ALERT_HOURS} ч: нет"]
        out = [f"🔔 <b>Алерты за {self.PROTO_ALERT_HOURS} ч</b> "
               f"({len(rows)}; ✅ = ack, 🤖 = есть разбор агента):"]
        for fired_at, key, title, acked_at, kimi_at in rows:
            when = str(fired_at or '')[11:16]
            flags = (' ✅' if acked_at else '') + (' 🤖' if kimi_at else '')
            out.append(f"• <code>{when}</code> {html.escape(str(key))} — "
                       f"{html.escape(str(title))}{flags}")
        return out

    # ----- /whoami -----

    def show_whoami(self, chat_id: str, args: list) -> None:
        """Echo back the caller's id + admin flag — useful when callback
        ids look wrong or you wonder if your forum-group send is admin-
        gated correctly."""
        lines = [
            "🆔 <b>whoami</b>",
            f"• chat_id: <code>{chat_id}</code>",
            f"• super_admin_id: <code>{getattr(self.config, 'SUPER_ADMIN_ID', '?')}</code>",
            f"• forum group id: <code>{getattr(self.config, 'FORUM_GROUP_ID', '?')}</code>",
            f"• you are admin here: <b>yes</b>",
        ]
        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /onlines -----

    def show_onlines(self, chat_id: str, args: list) -> None:
        """Live snapshot of who's connected right now.

        Aggregates three sources: access.log (xray_log.summarize_activity)
        for emails + per-IP counts, 3x-ui's /onlines API for the panel
        signal, and the xray-reload sidecar's /tcp-stats for per-IP
        RTTs from `ss -tin` on the entry node. Output mirrors what the
        dashboard shows so the admin doesn't have to leave the chat to
        check who's live.
        """
        # Track data source status for diagnostics
        source_status = {
            'xray_log': '✗',
            'xui_api': '✗',
            'tcp_stats': '✗',
            'geoip': '✗',
        }
        source_errors = {}

        # Try xray_log (access.log parsing)
        try:
            from bot.services.xray_log import summarize_activity
            from bot.services.xui_reload import get_tcp_stats
            activity = summarize_activity()
            if activity is not None:
                source_status['xray_log'] = '✓'
            else:
                activity = {}
        except PermissionError as e:
            activity = {}
            source_status['xray_log'] = '✗ (perm denied)'
            source_errors['xray_log'] = f"Permission denied: {e}"
            logger.warning(f"/onlines: log permission denied: {e}")
        except FileNotFoundError as e:
            activity = {}
            source_status['xray_log'] = '✗ (not found)'
            source_errors['xray_log'] = f"File not found: {e}"
            logger.warning(f"/onlines: log file not found: {e}")
        except Exception as e:
            activity = {}
            source_status['xray_log'] = f'✗ ({type(e).__name__})'
            source_errors['xray_log'] = str(e)[:100]
            logger.warning(f"/onlines: log parse failed: {e}")

        # Try X-UI API
        try:
            xui = self.bot.services.get('xui') if hasattr(self.bot, 'services') else None
            panel_emails: set = set()
            if xui and hasattr(xui, 'api') and xui.api:
                panel_result = xui.api.get_online_clients_sync()
                panel_emails = set(panel_result)
                if panel_result is not None:
                    source_status['xui_api'] = '✓'
            else:
                source_status['xui_api'] = '— (not configured)'
        except ConnectionRefusedError as e:
            panel_emails = set()
            source_status['xui_api'] = '✗ (conn refused)'
            source_errors['xui_api'] = f"Connection refused: {e}"
            logger.warning(f"/onlines: xui api connection refused: {e}")
        except Exception as e:
            panel_emails = set()
            source_status['xui_api'] = f'✗ ({type(e).__name__})'
            source_errors['xui_api'] = str(e)[:100]
            logger.warning(f"/onlines: xui api failed: {e}")

        # lastOnline from the panel's accounting rows. The fork's
        # /onlines endpoint returns [] — but clientStats.lastOnline is
        # maintained live by the panel stats job (and by the hy2 bridge
        # for hysteria-only users), so it is the authoritative "online"
        # signal in API mode.
        last_online_min: dict = {}
        try:
            if xui and getattr(xui, 'api', None):
                import time as _time
                now_ms = _time.time() * 1000
                for ib in (xui._run_sync(xui.api.get_inbounds()) or []):
                    for cs in (ib.get('clientStats') or []):
                        lo = cs.get('lastOnline') or 0
                        if lo and now_ms - lo < 5 * 60 * 1000:
                            em = cs.get('email')
                            age = int((now_ms - lo) / 60000)
                            if em and (em not in last_online_min
                                       or age < last_online_min[em]):
                                last_online_min[em] = age
                source_status['xui_api'] = '✓'
        except Exception as e:
            logger.warning(f"/onlines: lastOnline fetch failed: {e}")

        # Try TCP stats
        try:
            rtt_by_ip = get_tcp_stats()
            if rtt_by_ip:
                source_status['tcp_stats'] = '✓'
            else:
                rtt_by_ip = {}
                source_status['tcp_stats'] = '— (no data)'
        except ConnectionRefusedError as e:
            rtt_by_ip = {}
            source_status['tcp_stats'] = '✗ (conn refused)'
            source_errors['tcp_stats'] = "Connection refused to xray-reload sidecar"
            logger.warning(f"/onlines: tcp stats connection refused: {e}")
        except Exception as e:
            rtt_by_ip = {}
            source_status['tcp_stats'] = f'✗ ({type(e).__name__})'
            source_errors['tcp_stats'] = str(e)[:100]

        # GeoIP (soft import — works even if maxminddb is missing)
        try:
            from bot.services.geoip import lookup as geo_lookup
            if geo_lookup:
                source_status['geoip'] = '✓'
        except Exception:
            geo_lookup = None

        emails = sorted(
            set(activity.keys()) | panel_emails | set(last_online_min)
        )

        # Check if all primary sources failed
        all_sources_failed = (
            source_status['xray_log'].startswith('✗') and
            source_status['xui_api'].startswith('✗')
        )

        if not emails:
            # No online users - show diagnostic info
            diag_lines = [
                "⚪ <b>Сейчас никто не подключён.</b>\n",
                "📊 <b>Статус источников данных:</b>\n",
                f"• access.log (xray_log): {source_status['xray_log']}",
                f"• X-UI API: {source_status['xui_api']}",
                f"• TCP stats: {source_status['tcp_stats']}",
                f"• GeoIP: {source_status['geoip']}",
            ]

            # Add error details if available
            if source_errors:
                diag_lines.append("\n<b>Ошибки:</b>")
                for src, err in source_errors.items():
                    diag_lines.append(f"• {src}: <code>{err[:80]}</code>")

            # Add fix hints when all sources failed
            if all_sources_failed:
                diag_lines.append("\n💡 <b>Возможные решения:</b>")
                if source_status['xray_log'].startswith('✗'):
                    diag_lines.append("• <b>access.log:</b> sudo chmod +r /var/lib/docker/volumes/vpn-bot_3xui-data/_data/access.log")
                if source_status['xui_api'].startswith('✗'):
                    diag_lines.append("• <b>X-UI:</b> curl http://127.0.0.1:2026/this_is_fine/")

            self.bot.send_message(
                chat_id=chat_id,
                text="\n".join(diag_lines),
                parse_mode='HTML',
                message_thread_id=self._get_thread_id(chat_id),
            )
            return

        # Map email → user row (for username + consumed_gb).
        users_by_email = {}
        try:
            for u in self.db.get_all_users():
                if u.email:
                    users_by_email[u.email] = u
        except Exception as e:
            logger.warning(f"/onlines: users fetch failed: {e}")

        # Per-email traffic from x-ui
        traffic_by_email = {}
        try:
            if xui:
                traffic_by_email = xui.get_all_traffic() or {}
        except Exception as e:
            logger.warning(f"/onlines: traffic fetch failed: {e}")

        lines = [f"🟢 <b>Сейчас онлайн: {len(emails)}</b>"]

        # Add source status indicators when data is degraded
        degraded_sources = [k for k, v in source_status.items() if v.startswith('✗')]
        if degraded_sources:
            src_summary = ", ".join([f"{k}: {source_status[k]}" for k in degraded_sources])
            lines.append(f"<i>⚠️ Данные неполные: {src_summary}</i>\n")
        else:
            lines.append("")
        for email in emails:
            user = users_by_email.get(email)
            uname = (f"@{user.username}" if user and user.username
                     else (f"user_{user.chat_id}" if user else email[:30]))
            act = activity.get(email, {})
            seen = last_online_min.get(email)
            seen_str = (' (только что)' if seen == 0
                        else f' ({seen} мин назад)' if seen is not None
                        else '')
            ips = act.get('ips') or []
            # Build "🇷🇺 91.246… , 🇰🇿 95.55…" with flags when GeoIP
            # is available — useful to spot one-key-two-countries.
            geo_per_ip = []
            cc_set = set()
            for ip in ips:
                if geo_lookup:
                    g = geo_lookup(ip)
                    if g:
                        cc, flag = g
                        cc_set.add(cc)
                        geo_per_ip.append(f"{flag} {ip}")
                        continue
                geo_per_ip.append(ip)
            ips_str = ", ".join(geo_per_ip) if geo_per_ip else "—"
            sharing_marker = " 🚨" if len(cc_set) > 1 else ""
            ip_count = act.get('distinct_ips') or 0
            limit = (user.limit_ip if user else None) or '—'
            conns = act.get('active_connections') or 0
            dests = act.get('distinct_destinations') or 0
            # Avg RTT across user's IPs
            rtts = [rtt_by_ip[ip] for ip in ips if ip in rtt_by_ip]
            rtt_str = f"{round(sum(rtts) / len(rtts), 1)} ms" if rtts else "—"
            # Consumption (bot DB knows quota, x-ui DB knows actual usage)
            t = traffic_by_email.get(email) or {}
            consumed = (t.get('upload', 0) + t.get('download', 0)) / (1024 ** 3)
            quota = user.quota_gb if user else None
            traffic_str = (
                f"{consumed:.2f}/{quota} GB" if quota
                else f"{consumed:.2f} GB"
            )
            lines.append(
                f"• {uname}{sharing_marker}{seen_str} "
                f"· 🔢 {ip_count}/{limit} IP "
                f"· 📶 {rtt_str} "
                f"· 📊 {traffic_str}\n"
                f"   <code>{ips_str}</code> "
                f"· {conns} соед. · {dests} назн."
            )

        text = "\n".join(lines)
        if len(text) > 3900:  # Telegram cap is 4096
            text = text[:3900] + "\n…(обрезано)"
        self.bot.send_message(
            chat_id=chat_id, text=text, parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /find <text> -----

    def find_user(self, chat_id: str, args: list) -> None:
        """Fuzzy search across username / chat_id / email / uuid prefix."""
        if not args:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат: /find <текст>\n"
                     "Ищет по username, chat_id, email и uuid (префикс).",
            )
            return

        query = ' '.join(args).strip().lstrip('@')
        if len(query) < 2:
            self.bot.send_message(chat_id=chat_id, text="❌ Минимум 2 символа.")
            return

        like = f"%{query}%"
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT chat_id, username, status, email, uuid, quota_gb, "
                    "       contact_email "
                    "FROM users "
                    "WHERE chat_id LIKE ? COLLATE NOCASE "
                    "   OR username LIKE ? COLLATE NOCASE "
                    "   OR email LIKE ? COLLATE NOCASE "
                    "   OR uuid LIKE ? COLLATE NOCASE "
                    "   OR contact_email LIKE ? COLLATE NOCASE "
                    "ORDER BY (status='paid') DESC, (status='demo') DESC, chat_id "
                    "LIMIT 20",
                    (like, like, like, like, like),
                ).fetchall()
        except Exception as e:
            logger.exception(f"find_user query failed: {e}")
            self.bot.send_message(chat_id=chat_id, text=f"❌ DB error: {e}")
            return

        if not rows:
            self.bot.send_message(
                chat_id=chat_id,
                text=f"🔍 Ничего не найдено по <code>{query}</code>.",
                parse_mode='HTML',
            )
            return

        lines = [f"🔍 <b>Найдено {len(rows)} (топ 20)</b>"]
        for r in rows:
            cid, uname, status, email, uuid, quota, contact = r
            # ext_* users have no username; the contact address is the
            # only human-recognizable identifier.
            uname_part = (f"@{uname}" if uname
                          else (f"✉️ {contact}" if contact else "—"))
            email_short = email or "—"
            uuid_short = (uuid[:8] + "…") if uuid else "—"
            lines.append(
                f"• <code>{cid}</code> · {uname_part} · {status} · "
                f"{quota or 0}GB · <code>{uuid_short}</code> · {email_short}"
            )
        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /recent [N] -----

    def show_recent_actions(self, chat_id: str, args: list) -> None:
        """Last N admin_actions rows (default 15, max 50)."""
        try:
            n = int(args[0]) if args else 15
        except (ValueError, IndexError):
            n = 15
        n = max(1, min(n, 50))

        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT admin_id, action, target_id, created_at "
                    "FROM admin_actions ORDER BY id DESC LIMIT ?",
                    (n,),
                ).fetchall()
        except Exception as e:
            self.bot.send_message(chat_id=chat_id, text=f"❌ DB error: {e}")
            return

        if not rows:
            self.bot.send_message(chat_id=chat_id, text="📭 Журнал пуст.")
            return

        lines = [f"📜 <b>Последние {len(rows)} действий</b>"]
        for r in rows:
            adm, act, tgt, when = r
            when_short = (when or '')[5:16].replace('T', ' ')  # MM-DD HH:MM
            tgt_short = f" → <code>{tgt}</code>" if tgt else ""
            lines.append(f"• <code>{when_short}</code> {act}{tgt_short} <i>(by {adm})</i>")
        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /repair_stuck -----

    def repair_stuck_support(self, chat_id: str, args: list) -> None:
        """Manually trigger the support_state_repair scheduler job.

        Reverts users whose ``status=support_topic`` AND
        ``support_topic_id IS NULL`` back to their previous_state
        (or DEMO/NEW based on whether a key was issued). Useful when
        you don't want to wait for the next hourly tick.
        """
        notifier = (
            self.bot.services.get('notifications')
            if hasattr(self.bot, 'services') else None
        )
        if not notifier:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ NotificationService недоступен — попробуй /restart_bot через kimi.",
            )
            return
        try:
            before_count = 0
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE status='support_topic' AND support_topic_id IS NULL"
                ).fetchone()
                if row:
                    before_count = row[0]
            notifier._repair_stuck_support_users_sync()
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE status='support_topic' AND support_topic_id IS NULL"
                ).fetchone()
                after_count = row[0] if row else 0
            fixed = before_count - after_count
            self.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🩹 Восстановлено: <b>{fixed}</b> юзеров.\n"
                    f"До: {before_count} застрявших · после: {after_count}"
                ),
                parse_mode='HTML',
                message_thread_id=self._get_thread_id(chat_id),
            )
        except Exception as e:
            logger.exception("repair_stuck_support failed")
            self.bot.send_message(chat_id=chat_id, text=f"❌ {e}")

    # ----- /topics -----

    def show_topics(self, chat_id: str, args: list) -> None:
        """Dump current TOPIC_* env values + the forum group id.

        Handy after forum_bootstrap re-created topics with new ids."""
        cfg = self.config
        forum_id = getattr(cfg, 'FORUM_GROUP_ID', None)
        topic_attrs = [
            'TOPIC_REQUESTS', 'TOPIC_USERS', 'TOPIC_DEMO',
            'TOPIC_REJECTED', 'TOPIC_STATS', 'TOPIC_PAYMENTS',
            'TOPIC_SUPPORT', 'TOPIC_SOLVED', 'TOPIC_AI',
        ]
        lines = [
            "📋 <b>Forum topology</b>",
            f"• Group: <code>{forum_id}</code>",
            f"• Enabled: <b>{getattr(cfg, 'FORUM_ENABLED', False)}</b>",
            "",
        ]
        for name in topic_attrs:
            val = getattr(cfg, name, None)
            mark = '✓' if val else '✗'
            lines.append(f"{mark} <code>{name}</code> = {val}")
        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /quota @user N -----

    def set_quota(self, chat_id: str, args: list) -> None:
        """Set arbitrary quota in GB (replaces, not adds — unlike grant_100gb)."""
        if len(args) < 2:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат: /quota @username N\n(N — лимит в ГБ; ставит, не прибавляет)",
            )
            return

        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return

        try:
            gb = float(args[1])
        except ValueError:
            self.bot.send_message(chat_id=chat_id, text="❌ N должно быть числом.")
            return
        if gb < 0 or gb > 100000:
            self.bot.send_message(chat_id=chat_id, text="❌ N вне диапазона 0–100000.")
            return

        user = self.db.get_user(target.chat_id)
        if not user:
            self.bot.send_message(chat_id=chat_id, text="❌ Юзер исчез между запросом и записью.")
            return
        old = user.quota_gb or 0
        user.quota_gb = gb
        self.db.save_user(user)

        # Propagate to x-ui. In-place update — NOT add_client: re-adding
        # an existing email makes the service delete + re-add the client,
        # which wipes its accounted traffic (and with it the quota state).
        xui_msg = ''
        if user.email:
            xui = self.bot.services.get('xui') if hasattr(self.bot, 'services') else None
            if xui:
                try:
                    ok = xui.sync_client_settings_sync(
                        user.email, {'totalGB': int(gb * BYTES_PER_GB)})
                    xui_msg = ' (x-ui sync OK)' if ok else ' (x-ui sync FAILED)'
                except Exception as e:
                    xui_msg = f' (x-ui error: {e})'

        try:
            admin_id = str(self.config.SUPER_ADMIN_ID)
            self.db.log_admin_action(admin_id, 'cmd_quota', str(target.chat_id), f"{old} → {gb}")
        except Exception:
            pass

        uname = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(
            chat_id=chat_id,
            text=f"⚙️ Квота {uname}: <b>{old}</b> → <b>{gb}</b> ГБ{xui_msg}",
            parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /expire @user YYYY-MM-DD -----

    def set_expire(self, chat_id: str, args: list) -> None:
        """Set ``subscription_expiry`` to an explicit date. Also updates
        the active row in ``subscriptions`` if one exists, so the
        dashboard buckets stay consistent.
        """
        if len(args) < 2:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат: /expire @username YYYY-MM-DD",
            )
            return

        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return

        date_str = args[1].strip()
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат даты: YYYY-MM-DD (например, 2026-12-31).",
            )
            return

        # Store as ISO string at end-of-day for clarity
        new_expiry = (dt + timedelta(hours=23, minutes=59)).isoformat()

        user = self.db.get_user(target.chat_id)
        if not user:
            self.bot.send_message(chat_id=chat_id, text="❌ Юзер исчез между запросом и записью.")
            return
        old = user.subscription_expiry or '—'
        user.subscription_expiry = new_expiry
        self.db.save_user(user)

        # Mirror into subscriptions.expires_at if active row exists.
        subs_msg = ''
        try:
            with self.db._connect() as conn:
                cur = conn.execute(
                    "UPDATE subscriptions SET expires_at = ? "
                    "WHERE chat_id = ? AND is_active = 1",
                    (new_expiry, str(target.chat_id)),
                )
                if cur.rowcount:
                    subs_msg = f' (subscriptions: {cur.rowcount} row)'
        except Exception as e:
            subs_msg = f' (subscriptions sync error: {e})'

        # Mirror into the panel client — otherwise 3x-ui keeps the old
        # expiryTime and disables the key while the bot considers the
        # user paid (bit every paid user when the July grant lapsed).
        xui_msg = ''
        if user.email:
            xui = self.bot.services.get('xui') if hasattr(self.bot, 'services') else None
            if xui:
                try:
                    expiry_ms = int(
                        (dt + timedelta(hours=23, minutes=59)).timestamp() * 1000
                    )
                    ok = xui.sync_client_settings_sync(
                        user.email, {'expiryTime': expiry_ms, 'enable': True})
                    xui_msg = ' (x-ui OK)' if ok else ' (x-ui FAILED)'
                except Exception as e:
                    xui_msg = f' (x-ui error: {e})'

        try:
            admin_id = str(self.config.SUPER_ADMIN_ID)
            self.db.log_admin_action(
                admin_id, 'cmd_expire', str(target.chat_id),
                f"{old} → {date_str}",
            )
        except Exception:
            pass

        uname = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(
            chat_id=chat_id,
            text=f"📅 Подписка {uname} до <b>{date_str}</b>{subs_msg}{xui_msg}",
            parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /addmail email@host [gb] [days] -----

    # Default grant for a manually-added email-only user. Chosen over the
    # 7-day demo because such a user can't easily re-request via Telegram —
    # a demo that silently expires would strand them.
    MAIL_DEFAULT_GB = 100
    MAIL_DEFAULT_DAYS = 30

    def add_mail_user(self, chat_id: str, args: list) -> None:
        """Create an email-only user (no Telegram account), register the
        key in X-UI, and email them the subscription link + setup guide.

        Usage: /addmail user@example.com [gb] [days]
        The address is both the delivery target and the user's stored
        contact_email. Re-running for the same address reuses the same
        user and UUID (installed keys keep working) and just re-sends.
        """
        thread_id = self._get_thread_id(chat_id)

        def reply(text: str) -> None:
            self.bot.send_message(chat_id=chat_id, text=text,
                                  parse_mode='HTML', message_thread_id=thread_id)

        if not args:
            reply("❌ Формат: <code>/addmail user@example.com [ГБ] [дней]</code>\n"
                  f"По умолчанию: {self.MAIL_DEFAULT_GB} ГБ, {self.MAIL_DEFAULT_DAYS} дней.")
            return

        from bot.utils.validators import validate_email
        email = args[0].strip()
        if not validate_email(email):
            reply("❌ Неверный формат email.")
            return

        gb = self.MAIL_DEFAULT_GB
        days = self.MAIL_DEFAULT_DAYS
        if len(args) >= 2:
            try:
                gb = int(args[1])
            except ValueError:
                reply("❌ ГБ должно быть числом.")
                return
        if len(args) >= 3:
            try:
                days = int(args[2])
            except ValueError:
                reply("❌ Дней должно быть числом.")
                return

        mailer = self.bot.services.get('email') if hasattr(self.bot, 'services') else None
        if mailer is None:
            from bot.services.email_service import EmailService
            mailer = EmailService(self.config)
        if not mailer.is_configured():
            reply("❌ Почта не настроена (SMTP_HOST). Заполни SMTP_* в .env и перезапусти бота.")
            return

        try:
            sub_url = self._provision_email_user(email, gb, days)
        except Exception as e:
            logger.exception("add_mail_user provisioning failed")
            reply(f"❌ Не удалось создать ключ: {str(e)[:200]}")
            return
        if not sub_url:
            reply("❌ Ключ создан, но не удалось собрать ссылку (проверь WEBAPP_URL).")
            return

        # SMTP handshake is seconds — send off the polling thread.
        import threading

        def _worker() -> None:
            ok = mailer.send_key(email, sub_url, lang='ru')
            if ok:
                reply(f"✉️ Готово. Пользователь <code>{email}</code> создан "
                      f"({gb} ГБ, {days} дн.), ключ и инструкция отправлены на почту.")
            else:
                reply(f"⚠️ Пользователь <code>{email}</code> создан, но письмо "
                      "не отправилось. Проверь SMTP и попробуй /addmail ещё раз.")

        threading.Thread(target=_worker, daemon=True,
                         name=f"addmail-{email}").start()

        try:
            self.db.log_admin_action(
                str(self.config.SUPER_ADMIN_ID), 'cmd_addmail', email,
                f"{gb}GB/{days}d")
        except Exception:
            pass

    def _provision_email_user(self, email: str, gb: int, days: int,
                              status: str = 'paid') -> Optional[str]:
        """Create-or-update the email-only user + X-UI key. Returns the
        subscription URL. Raises on X-UI failure so the caller reports it.

        ``status``: 'paid' for the manual /addmail grant (default),
        'demo' for self-serve mail-intake approvals — freemium tier,
        renewed monthly by the quota job like every other demo user.
        """
        import asyncio
        import binascii
        from datetime import datetime, timedelta

        from bot.config.constants import UserState, Platform, BYTES_PER_GB
        from bot.models.user import User
        from bot.services.vpn import VPNService
        from bot.services.subscription import SubscriptionService

        # Stable synthetic id from the address: idempotent + can't collide
        # with a real Telegram chat id.
        existing = None
        with self.db._connect() as c:
            row = c.execute(
                "SELECT chat_id FROM users WHERE contact_email = ? LIMIT 1",
                (email,),
            ).fetchone()
            if row:
                existing = row[0]
        crc = binascii.crc32(email.encode()) & 0xFFFFFFFF
        target_chat = existing or f"ext_{crc:08x}"

        user = self.db.get_user(target_chat) or User(chat_id=target_chat, username=None)

        vpn = VPNService(self.config)
        expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)
        if not user.uuid:
            client = vpn.create_client_config(
                chat_id=target_chat, username=None,
                traffic_gb=gb, expiry_days=days,
                comment=email,
            )
            user.uuid = client['id']
            user.email = client['email']
        else:
            # Re-issue: keep the UUID so an already-installed key still works.
            client = {
                "id": user.uuid, "flow": "xtls-rprx-vision", "email": user.email,
                "limitIp": 1, "totalGB": gb * BYTES_PER_GB,
                "expiryTime": expiry_ms, "enable": True,
                "comment": email,
            }

        xui = self.bot.services.get('xui') if hasattr(self.bot, 'services') else None
        if xui is None:
            from bot.services.xui_service import XUIService
            xui = XUIService(self.config)
        if not asyncio.run(xui.sync_user(target_chat, client)):
            raise RuntimeError("X-UI sync failed")

        user.status = (UserState.DEMO.value if status == 'demo'
                       else UserState.PAID.value)
        user.platform = user.platform or Platform.ANDROID.value
        user.quota_gb = float(gb)
        # Demo is freemium: no paid-until date — the monthly job keeps
        # pushing the panel expiry forward like for every demo user.
        user.subscription_expiry = (
            None if status == 'demo'
            else (datetime.now() + timedelta(days=days)).isoformat()
        )
        user.contact_email = email
        self.db.save_user(user)

        return SubscriptionService(self.config).build_subscription_url(user)
