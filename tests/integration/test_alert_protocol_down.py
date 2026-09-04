"""protocol_down alert: a protocol whose probe success rate collapsed.

The 2026-09-01 outage was fully observable from minute one. The probe
suite wrote it to ``outbound_health`` on schedule — reality went from
672/960 ok per day to 0/960 at 00:01 UTC while hy2/ws/stls stayed
normal — and nobody found out for four days, because AlertManager only
watched DPI counters, Telegram egress and host resources. Nothing read
the probe table.

Runs against a REAL sqlite bot.db (``Database`` creates outbound_health
in its own migrations), so the check's SQL is exercised rather than
mocked away.
"""

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from bot.core.database import Database
from bot.services.alert_manager import AlertManager, build_default_checks


PROTOCOLS = ['reality', 'hy2', 'ws', 'stls']
DOMAINS = ['vk.com', 'yandex.ru', 'sberbank.ru', 'rutube.ru', 'youtube.com',
           'google.com', 'facebook.com', 'telegram.org', 'github.com',
           'anthropic.com']


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def config(db, tmp_path):
    cfg = Mock()
    cfg.DB_PATH = str(tmp_path / 'bot.db')
    cfg.XUI_DB_PATH = ''
    cfg.TOPIC_AI = 55
    cfg.FORUM_GROUP_ID = -100123
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.ALERT_TG_MIN_SEVERITY = 'critical'
    return cfg


def seed(db, protocol, *, runs, ok_per_run, status='timeout',
         minutes_ago_start=15, domains=DOMAINS):
    """Write ``runs`` probe runs, most recent first.

    One row per (protocol, domain) per run — the exact shape
    HealthChecker._write_result produces.
    """
    with db._connect() as conn:
        for run in range(runs):
            ts = (datetime.utcnow()
                  - timedelta(minutes=minutes_ago_start + run * 15)).isoformat()
            for i, domain in enumerate(domains):
                st = 'ok' if i < ok_per_run else status
                conn.execute(
                    "INSERT INTO outbound_health (outbound_tag, target_domain,"
                    " status, latency_ms, error_msg, ts) VALUES (?,?,?,?,?,?)",
                    (protocol, domain, st, 120 if st == 'ok' else None,
                     None if st == 'ok' else st, ts),
                )
        conn.commit()


def probe_check(config):
    checks = build_default_checks(config, Mock())
    for c in checks:
        if c.__name__ == 'check_protocol_probe_down':
            return c
    raise AssertionError(
        'check_protocol_probe_down is not registered in build_default_checks'
    )


def keys(result):
    _prefix, alerts = result
    return sorted(a.key for a in alerts)


class TestProtocolDownCheck:

    def test_fires_on_three_consecutive_zero_ok_runs(self, db, config):
        """The incident, reproduced: reality all-timeout, everyone else
        at the normal baseline."""
        seed(db, 'reality', runs=3, ok_per_run=0)
        for p in ('hy2', 'ws', 'stls'):
            seed(db, p, runs=3, ok_per_run=7)

        prefix, alerts = probe_check(config)()

        assert prefix == 'protocol_down:'
        assert [a.key for a in alerts] == ['protocol_down:reality']
        assert alerts[0].severity == 'critical'
        assert 'reality' in alerts[0].title

    def test_does_not_fire_on_the_normal_seven_of_ten_baseline(self, db, config):
        """7/10 ok is what a healthy protocol looks like here — RU
        domestic targets fail through the tunnel by design. Alerting on
        that would train everyone to ignore the channel."""
        for p in PROTOCOLS:
            seed(db, p, runs=4, ok_per_run=7)

        assert keys(probe_check(config)()) == []

    def test_does_not_fire_on_a_single_bad_run(self, db, config):
        """One flaky run (deploy, panel restart, transient upstream) is
        not an outage — the check needs consecutive evidence."""
        seed(db, 'reality', runs=1, ok_per_run=0)

        assert keys(probe_check(config)()) == []

    def test_one_surviving_probe_is_enough_to_stay_quiet(self, db, config):
        """Degraded is not dead. Only a total collapse pages."""
        seed(db, 'reality', runs=3, ok_per_run=0)
        seed(db, 'reality', runs=1, ok_per_run=1, minutes_ago_start=5)

        assert keys(probe_check(config)()) == []

    def test_proxy_down_rows_do_not_page_four_fake_outages(self, db, config):
        """``proxy_down`` means the probe sidecar was unreachable — a
        monitoring outage. Counting it would turn one dead sidecar into
        four critical protocol alerts and bury the real signal."""
        for p in PROTOCOLS:
            seed(db, p, runs=4, ok_per_run=0, status='proxy_down')

        assert keys(probe_check(config)()) == []

    def test_stale_rows_outside_the_window_do_not_fire(self, db, config):
        """A protocol that was dead yesterday but has no fresh probes
        (checker stopped, protocol retired) must not page forever."""
        seed(db, 'reality', runs=3, ok_per_run=0,
             minutes_ago_start=60 * 24)

        assert keys(probe_check(config)()) == []

    def test_empty_table_is_silent(self, db, config):
        assert keys(probe_check(config)()) == []

    def test_multiple_dead_protocols_each_get_a_key(self, db, config):
        seed(db, 'reality', runs=3, ok_per_run=0)
        seed(db, 'stls', runs=3, ok_per_run=0)
        seed(db, 'hy2', runs=3, ok_per_run=7)

        assert keys(probe_check(config)()) == [
            'protocol_down:reality', 'protocol_down:stls']

    def test_unreadable_db_is_silent_not_crashing(self, config):
        """Monitoring failures must never take down the alert tick."""
        config.DB_PATH = '/nonexistent/dir/bot.db'
        assert keys(probe_check(config)()) == []


class TestProtocolDownReachesTelegram:
    """Wiring: the check must actually page through AlertManager."""

    def test_fires_to_the_forum_topic_after_min_cycles(self, db, config):
        seed(db, 'reality', runs=3, ok_per_run=0)
        bot = Mock()
        mgr = AlertManager(bot, config, db=db)
        mgr.register(probe_check(config))

        mgr.run_once()
        bot.send_message.assert_not_called()   # min_cycles=2, first cycle
        mgr.run_once()

        bot.send_message.assert_called_once()
        kw = bot.send_message.call_args.kwargs
        assert kw['chat_id'] == config.FORUM_GROUP_ID
        assert kw['message_thread_id'] == config.TOPIC_AI
        assert 'reality' in kw['text']
        # Persisted for the dashboard Alerts tab too.
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT key, severity FROM alert_history").fetchall()
        assert [tuple(r) for r in rows] == [('protocol_down:reality', 'critical')]

    def test_recovery_resets_the_tracker(self, db, config):
        """Once probes come back the key must stop counting, so a later
        blip starts from zero instead of paging instantly."""
        seed(db, 'reality', runs=3, ok_per_run=0)
        bot = Mock()
        mgr = AlertManager(bot, config, db=db)
        mgr.register(probe_check(config))

        mgr.run_once()
        assert mgr._state['protocol_down:reality'].consecutive_fails == 1

        with sqlite3.connect(config.DB_PATH) as conn:
            conn.execute(
                "UPDATE outbound_health SET status = 'ok' "
                "WHERE outbound_tag = 'reality'")
            conn.commit()

        mgr.run_once()
        assert mgr._state['protocol_down:reality'].consecutive_fails == 0
        bot.send_message.assert_not_called()
