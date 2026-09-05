"""/protocols — the deterministic protocol card (no LLM).

Why
---
/status pings services and said "ok" all through the 2026-09-01 Reality
outage; the probe table knew from minute one. This command is the plain
read of that table (LIVENESS RULE: a latency OR status='ok' proves the
tunnel carried something) + the panel field audit + the recent pager
rows — for when the agent is down or slow, which is exactly when you
need a straight answer in the topic.

Real sqlite bot.db (Database creates outbound_health / alert_history in
its own migrations) so the SQL is exercised. The panel audit is a
subprocess; it is stubbed at ``subprocess.run`` so no test ever talks
to a panel, and one test asserts the exact command shape instead.
"""

import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from bot.core.database import Database
from bot.handlers.admin.base import ADMIN_HELP_TEXT, AdminHandlerBase
from bot.handlers.admin.ops import AdminOpsMixin


PROTOCOLS = ['reality', 'hy2', 'ws', 'stls']
DOMAINS = ['vk.com', 'yandex.ru', 'sberbank.ru', 'rutube.ru', 'youtube.com',
           'google.com', 'facebook.com', 'telegram.org', 'github.com',
           'anthropic.com']


def completed(rc, stdout):
    return subprocess.CompletedProcess(args=['x'], returncode=rc,
                                       stdout=stdout, stderr='')


AUDIT_OK = completed(0, 'OK: 128 client records on 5 inbounds, all required '
                        'per-protocol fields present\n')
AUDIT_BROKEN = completed(1, (
    'BROKEN: 2 problem(s) across 128 client records on 5 inbounds:\n'
    "  inbound-443 (id=1, vless): user_1@nekovo.ru has empty 'flow'\n"
    "  inbound-443 (id=1, vless): user_2@nekovo.ru has empty 'flow'\n"))
AUDIT_CANNOT = completed(2, 'CANNOT CHECK: panel unreachable: timeout\n')


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def handler(db):
    h = AdminOpsMixin.__new__(AdminOpsMixin)
    h.bot = Mock()
    h.bot.send_message = Mock()
    h.bot.services = {}
    h.db = db
    cfg = Mock()
    cfg.FORUM_ENABLED = False
    cfg.FORUM_GROUP_ID = None
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.HY2T_PORT = ''          # no turbo on this deployment unless a test sets it
    h.config = cfg
    h._get_thread_id = Mock(return_value=None)
    return h


def seed(db, protocol, *, runs, ok_per_run, status='timeout',
         minutes_ago_start=15, latency_ms=None):
    """``runs`` probe runs, one row per domain per run — the shape
    HealthChecker._write_result produces (per-row utc-naive ISO ts)."""
    with db._connect() as conn:
        for run in range(runs):
            ts = (datetime.utcnow()
                  - timedelta(minutes=minutes_ago_start + run * 15)).isoformat()
            for i, domain in enumerate(DOMAINS):
                st = 'ok' if i < ok_per_run else status
                conn.execute(
                    "INSERT INTO outbound_health (outbound_tag, target_domain,"
                    " status, latency_ms, error_msg, ts) VALUES (?,?,?,?,?,?)",
                    (protocol, domain, st, 120 if st == 'ok' else latency_ms,
                     None if st == 'ok' else st, ts),
                )
        conn.commit()


def run(handler, audit=AUDIT_OK, **run_kw):
    """Invoke /protocols with subprocess.run stubbed, join the worker,
    return the send_message kwargs."""
    kw = run_kw or {'return_value': audit}
    with patch('bot.handlers.admin.ops.subprocess.run', **kw) as sp:
        t = handler.show_protocols('chat', [])
        t.join(timeout=10)
        assert not t.is_alive(), '/protocols worker did not finish'
        handler.bot.send_message.assert_called_once()
        out = handler.bot.send_message.call_args.kwargs
        out['_subprocess_run'] = sp
        return out


def line_for(text, tag):
    """The card line for one protocol (bold tag is the anchor)."""
    return next(ln for ln in text.splitlines() if f"<b>{tag}</b>" in ln)


class TestProtocolLines:

    def test_dark_protocol_is_down_and_healthy_ones_are_ok(self, handler, db):
        """The incident, reproduced: reality all-timeout with NULL
        latency, everyone else at the 7/10 baseline."""
        seed(db, 'reality', runs=3, ok_per_run=0)
        for p in ('hy2', 'ws', 'stls'):
            seed(db, p, runs=3, ok_per_run=7)

        text = run(handler)['text']

        assert text.startswith('📡 <b>Протоколы</b>')
        assert line_for(text, 'reality').startswith('🔴')
        assert 'лежит' in line_for(text, 'reality')
        assert '0/10 ok' in line_for(text, 'reality')
        for p in ('hy2', 'ws', 'stls'):
            assert line_for(text, p) == f"🟢 <b>{p}</b> — 7/10 ok"
        # No HY2T_PORT and no hy2t rows: not probed here — say so
        # instead of a blank (see TestHy2tLine for the probed cases).
        assert '<b>hy2t</b>: без проб, см. /ai' in text

    def test_how_long_is_anchored_on_the_last_alive_row(self, handler, db):
        """A run that showed life 6 h ago is outside the window (so it
        cannot save the protocol) but is the right anchor for 'лежит'."""
        seed(db, 'reality', runs=1, ok_per_run=7, minutes_ago_start=360)
        seed(db, 'reality', runs=3, ok_per_run=0)

        assert 'лежит 6 ч' in line_for(run(handler)['text'], 'reality')

    def test_never_alive_says_so(self, handler, db):
        seed(db, 'reality', runs=3, ok_per_run=0)
        assert 'лежит всё время наблюдений' in line_for(run(handler)['text'], 'reality')

    def test_blocked_sites_are_degraded_not_dead(self, handler, db):
        """Errors that came back THROUGH the tunnel carry a latency: the
        tunnel demonstrably works even at 0/10 ok."""
        seed(db, 'reality', runs=3, ok_per_run=0, latency_ms=430)

        ln = line_for(run(handler)['text'], 'reality')
        assert ln.startswith('🟡')
        assert 'деградация' in ln
        assert 'лежит' not in ln

    def test_last_run_dead_after_alive_runs_is_yellow(self, handler, db):
        """First minutes of an outage: the pager needs 3 dark runs, the
        card already flags the newest run as answerless."""
        seed(db, 'reality', runs=2, ok_per_run=7, minutes_ago_start=20)
        seed(db, 'reality', runs=1, ok_per_run=0, minutes_ago_start=5)

        ln = line_for(run(handler)['text'], 'reality')
        assert ln.startswith('🟡')
        assert 'последний прогон без единого ответа' in ln

    def test_one_dark_run_alone_is_not_down(self, handler, db):
        """10 rows are below the 15-sample floor — same rule as the pager."""
        seed(db, 'reality', runs=1, ok_per_run=0)
        assert line_for(run(handler)['text'], 'reality').startswith('🟡')

    def test_protocol_without_rows_gets_a_grey_line(self, handler, db):
        seed(db, 'hy2', runs=3, ok_per_run=7)
        text = run(handler)['text']
        assert line_for(text, 'reality') == '⚪ <b>reality</b> — нет проб за 3 ч'
        assert line_for(text, 'hy2').startswith('🟢')

    def test_empty_table_says_pipeline_never_wrote(self, handler, db):
        text = run(handler)['text']
        assert 'outbound_health пуст' in text
        for p in PROTOCOLS:
            assert line_for(text, p).startswith('⚪')

    def test_stale_rows_flag_a_dead_probe_pipeline(self, handler, db):
        """Rows 2 h old are still inside the 3 h window, so the lines
        render — but the header must say they are history, not status."""
        for p in PROTOCOLS:
            seed(db, p, runs=3, ok_per_run=7, minutes_ago_start=120)

        text = run(handler)['text']
        assert 'пробы не пишутся уже 2 ч' in text
        assert line_for(text, 'reality').startswith('🟢')

    def test_fresh_rows_show_last_run_time(self, handler, db):
        seed(db, 'reality', runs=1, ok_per_run=7, minutes_ago_start=4)
        text = run(handler)['text']
        assert 'последний прогон' in text
        assert 'UTC (4 мин назад)' in text


class TestHy2tLine:
    """hy2t is probed only where HY2T_PORT is set (sidecar :18085). With
    rows it gets the same line as any protocol; the static 'без проб'
    note appears only when it is neither expected by config nor present
    in the table — never both, never a blank."""

    def test_rows_present_render_a_real_line_and_no_static_note(self, handler, db):
        for p in PROTOCOLS + ['hy2t']:
            seed(db, p, runs=3, ok_per_run=7)
        text = run(handler)['text']
        assert line_for(text, 'hy2t') == '🟢 <b>hy2t</b> — 7/10 ok'
        assert 'без проб' not in text
        assert text.count('<b>hy2t</b>') == 1

    def test_dark_hy2t_rows_are_red(self, handler, db):
        for p in PROTOCOLS:
            seed(db, p, runs=3, ok_per_run=7)
        seed(db, 'hy2t', runs=3, ok_per_run=0)
        ln = line_for(run(handler)['text'], 'hy2t')
        assert ln.startswith('🔴')
        assert '0/10 ok' in ln and 'лежит' in ln

    def test_expected_by_config_without_rows_is_grey_not_the_static_note(self, handler, db):
        """HY2T_PORT set but the checker has not written yet (first run
        after the deploy, or the container still runs the old code): the
        grey 'нет проб' line is the honest state — and it is ONE line."""
        handler.config.HY2T_PORT = '8402'
        for p in PROTOCOLS:
            seed(db, p, runs=3, ok_per_run=7)
        text = run(handler)['text']
        assert line_for(text, 'hy2t') == '⚪ <b>hy2t</b> — нет проб за 3 ч'
        assert 'без проб, см. /ai' not in text
        assert text.count('<b>hy2t</b>') == 1

    def test_disabled_without_rows_keeps_the_static_note_once(self, handler, db):
        for p in PROTOCOLS:
            seed(db, p, runs=3, ok_per_run=7)
        text = run(handler)['text']
        assert '⚪ <b>hy2t</b>: без проб, см. /ai' in text
        assert text.count('<b>hy2t</b>') == 1

    def test_rows_win_over_a_disabled_config(self, handler, db):
        """Rows in the window are a fact (e.g. HY2T_PORT was just emptied):
        show them rather than claim there are no probes."""
        handler.config.HY2T_PORT = ''
        seed(db, 'hy2t', runs=3, ok_per_run=7)
        text = run(handler)['text']
        assert line_for(text, 'hy2t').startswith('🟢')
        assert 'без проб' not in text

    def test_tag_order_follows_health_checker_then_extras(self, handler, db):
        """Config tags first in HealthChecker order (hy2t last), then any
        other tag that has rows — a new tag needs no code change here."""
        handler.config.HY2T_PORT = '8402'
        for p in PROTOCOLS + ['hy2t', 'xhttp']:
            seed(db, p, runs=3, ok_per_run=7)
        text = run(handler)['text']
        order = [ln.split('<b>')[1].split('</b>')[0]
                 for ln in text.splitlines() if ln.startswith(('🟢', '🟡', '🔴', '⚪'))]
        assert order == ['reality', 'hy2', 'ws', 'stls', 'hy2t', 'xhttp']

    def test_db_failure_makes_no_hy2t_claim(self, handler):
        handler.db = Mock()
        handler.db._connect = Mock(side_effect=sqlite3.OperationalError('database is locked'))
        text = run(handler)['text']
        assert 'outbound_health недоступна' in text
        assert '<b>hy2t</b>' not in text


class TestPanelAudit:

    def test_ok_verdict(self, handler, db):
        assert '✅ Аудит панели: OK: 128 client records' in run(handler)['text']

    def test_broken_verdict_shows_offenders(self, handler, db):
        text = run(handler, AUDIT_BROKEN)['text']
        assert '🔴 Аудит панели: BROKEN: 2 problem(s)' in text
        # Offenders are shown (HTML-escaped: the quote becomes &#x27;).
        assert 'user_1@nekovo.ru has empty &#x27;flow&#x27;' in text

    def test_cannot_check_is_not_a_pass(self, handler, db):
        text = run(handler, AUDIT_CANNOT)['text']
        assert '⚠️ Аудит панели: не удалось проверить — CANNOT CHECK: panel unreachable' in text
        assert '✅ Аудит' not in text

    def test_timeout_does_not_crash_the_card(self, handler, db):
        seed(db, 'reality', runs=3, ok_per_run=7)
        text = run(handler, side_effect=subprocess.TimeoutExpired(cmd='x', timeout=40))['text']
        assert '⚠️ Аудит панели: не удалось проверить — таймаут 40 с' in text
        assert line_for(text, 'reality').startswith('🟢')   # rest of the card intact

    def test_spawn_failure_does_not_crash_the_card(self, handler, db):
        text = run(handler, side_effect=OSError('no such interpreter'))['text']
        assert 'не удалось проверить — не запустился: no such interpreter' in text

    def test_missing_script_is_reported(self, handler, db):
        with patch('bot.handlers.admin.ops.Path.exists', return_value=False):
            text = run(handler)['text']
        assert 'скрипт не найден' in text
        run_mock = handler.bot.send_message  # sanity: still exactly one reply
        assert run_mock.call_count == 1

    def test_runs_the_repo_script_with_app_root_on_pythonpath(self, handler, db):
        """The script is not importable (scripts/ is not a package) and
        needs `bot` on sys.path — PYTHONPATH must point at the app root
        (/app in the container, the repo root here)."""
        sp = run(handler)['_subprocess_run']
        argv = sp.call_args.args[0]
        kw = sp.call_args.kwargs
        assert argv[1].endswith('scripts/verify_panel_client_fields.py')
        assert Path(argv[1]).exists()
        assert kw['timeout'] == 40
        assert kw['capture_output'] is True
        # PYTHONPATH's first entry is the app root — the directory that
        # holds both scripts/ and bot/ (i.e. /app inside the container).
        app_root = Path(kw['env']['PYTHONPATH'].split(':')[0])
        assert app_root == Path(argv[1]).parents[1]
        assert (app_root / 'bot' / 'handlers' / 'admin' / 'ops.py').exists()


class TestRecentAlerts:

    def _insert(self, db, key, title, *, hours_ago=0.0, acked=False, kimi=False):
        fired = (datetime.utcnow() - timedelta(hours=hours_ago)
                 ).strftime('%Y-%m-%d %H:%M:%S')
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO alert_history (key, severity, title, detail, fired_at, "
                "acked_at, kimi_at) VALUES (?, 'critical', ?, '', ?, ?, ?)",
                (key, title, fired,
                 fired if acked else None, fired if kimi else None),
            )
            conn.commit()

    def test_only_protocol_and_dpi_keys_within_six_hours(self, handler, db):
        self._insert(db, 'protocol_down:reality', 'Протокол reality мёртв', acked=True)
        self._insert(db, 'dpi_short:RU:AS8402', 'DPI? RU', hours_ago=2, kimi=True)
        self._insert(db, 'protocol_down:stls', 'Протокол stls мёртв', hours_ago=10)
        self._insert(db, 'disk', 'DISK 96%')

        text = run(handler)['text']
        bullets = [ln for ln in text.splitlines() if ln.startswith('• <code>')]
        assert len(bullets) == 2
        assert 'protocol_down:reality — Протокол reality мёртв ✅' in bullets[0]
        assert 'dpi_short:RU:AS8402 — DPI? RU 🤖' in bullets[1]
        assert 'protocol_down:stls' not in text
        assert 'DISK' not in text

    def test_no_recent_alerts(self, handler, db):
        assert 'Алертов protocol_down / dpi за 6 ч: нет' in run(handler)['text']

    def test_title_html_is_escaped(self, handler, db):
        self._insert(db, 'protocol_down:reality', 'flow <пуст>')
        assert 'flow &lt;пуст&gt;' in run(handler)['text']


class TestDelivery:

    def test_reply_lands_in_the_calling_topic(self, handler, db):
        """Thread id is captured BEFORE the worker starts: by the time the
        audit returns, _current_update may point at another command."""
        # First call (on the polling thread) sees the command's topic;
        # anything the worker asks later sees a different one.
        handler._get_thread_id = Mock(side_effect=[77] + [99] * 5)
        kw = run(handler)
        assert kw['chat_id'] == 'chat'
        assert kw['message_thread_id'] == 77
        assert kw['parse_mode'] == 'HTML'

    def test_fits_telegram(self, handler, db):
        for p in PROTOCOLS:
            seed(db, p, runs=3, ok_per_run=7)
        assert len(run(handler, AUDIT_BROKEN)['text']) < 4096

    def test_db_failure_still_answers(self, handler):
        handler.db = Mock()
        handler.db._connect = Mock(side_effect=sqlite3.OperationalError('database is locked'))
        text = run(handler)['text']
        assert 'outbound_health недоступна' in text
        assert 'alert_history недоступна: database is locked' in text
        assert 'Аудит панели' in text

    def test_builder_crash_is_reported_not_silent(self, handler, db):
        handler._build_protocols_report = Mock(side_effect=RuntimeError('kaboom'))
        assert run(handler)['text'] == '❌ /protocols: kaboom'

    def test_html_error_text_is_escaped(self, handler, db):
        handler._build_protocols_report = Mock(side_effect=RuntimeError('<oops>'))
        assert run(handler)['text'] == '❌ /protocols: &lt;oops&gt;'

    def test_returns_before_the_audit_finishes(self, handler, db):
        """Handlers run on the polling thread: a 40-s panel audit must
        not stall every other update."""
        import threading
        import time
        gate = threading.Event()

        def _slow_run(*a, **kw):
            gate.wait(timeout=10)
            return AUDIT_OK
        with patch('bot.handlers.admin.ops.subprocess.run', side_effect=_slow_run):
            t0 = time.monotonic()
            t = handler.show_protocols('chat', [])
            assert time.monotonic() - t0 < 2.0
            handler.bot.send_message.assert_not_called()
            gate.set()
            t.join(timeout=10)
        handler.bot.send_message.assert_called_once()

    def test_send_failure_on_the_worker_is_logged_not_raised(self, handler, db, caplog):
        handler.bot.send_message.side_effect = RuntimeError('tg 502')
        hooked = []
        orig = threading.excepthook
        threading.excepthook = lambda args: hooked.append(args)
        try:
            with patch('bot.handlers.admin.ops.subprocess.run', return_value=AUDIT_OK):
                t = handler.show_protocols('chat', [])
                t.join(timeout=10)
        finally:
            threading.excepthook = orig
        assert hooked == []
        assert any('reply send failed' in r.getMessage() for r in caplog.records)


class TestRegistration:

    def test_routed_and_advertised(self):
        assert AdminHandlerBase.ADMIN_COMMANDS['/protocols'] == 'show_protocols'
        assert '<code>/protocols</code>' in ADMIN_HELP_TEXT

    def test_assembled_handler_has_the_method(self):
        from bot.handlers.admin import AdminHandler
        assert callable(getattr(AdminHandler, 'show_protocols', None))
