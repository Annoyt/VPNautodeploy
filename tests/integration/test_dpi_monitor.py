"""DPIMonitor on a REAL sqlite bot.db: every rule, the hysteresis, the guards.

Why
---
2026-09-01: Reality died at 00:01 UTC (a client update blanked ``flow``).
outbound_health recorded 0/960 ok for four days, dpi_metrics recorded
879 handshake fails / 0 connections for MegaFon — and the cascade kept
handing Reality out, because nothing read either table with the power
to act. The monitor is that reader. These tests seed the same tables
the production collectors write (outbound_health, dpi_metrics,
hy2_auth_log, user_failure_reports, users) and check that the cascade
JSON, the audit log and the topic message come out as designed — and,
just as important, that nothing moves below the thresholds, during an
upstream outage, with stale probes, or when the operator switched the
monitor off.

Level-2 (real sqlite) so the collectors' SQL — the timestamp formats in
particular: 'T'-isoformat in probe/dpi tables vs CURRENT_TIMESTAMP in
hy2_auth_log / user_failure_reports — is exercised, not mocked away.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from bot.core.database import Database
from bot.handlers.callbacks.user import MyKeyAnswerHandler
from bot.services import dpi_monitor as dm
from bot.services.dpi_monitor import Change, DPIMonitor


PROTOCOLS = ('reality', 'hy2', 'hy2t', 'ws', 'stls')
DOMAINS = ['vk.com', 'yandex.ru', 'sberbank.ru', 'rutube.ru', 'youtube.com',
           'google.com', 'facebook.com', 'telegram.org', 'github.com',
           'anthropic.com']
T0 = datetime(2026, 9, 5, 14, 0, 0)
STEP = timedelta(minutes=10)
MEGAFON = 'AS31133'


# ---- fixtures ---------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def config():
    cfg = Mock()
    cfg.FORUM_GROUP_ID = -100123
    cfg.TOPIC_AI = 55
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.DPI_MONITOR_ENABLED = True
    cfg.DPI_MONITOR_INTERVAL_MIN = 10
    return cfg


@pytest.fixture
def bot():
    return Mock()


@pytest.fixture
def monitor(db, config, bot):
    return DPIMonitor(db, config, bot)


# ---- seed helpers (the exact row shapes production writes) ------------------

def seed_probe(db, protocol, *, now, runs=3, ok_per_run=7, status='timeout',
               latency_ms=None, minutes_ago_start=5, domains=DOMAINS):
    """``runs`` probe runs, newest first, one row per (protocol, domain)
    — HealthChecker._write_result's shape, 'T'-isoformat ts."""
    with db._connect() as conn:
        for run in range(runs):
            ts = (now - timedelta(minutes=minutes_ago_start + run * 15)).isoformat()
            for i, domain in enumerate(domains):
                st = 'ok' if i < ok_per_run else status
                conn.execute(
                    "INSERT INTO outbound_health (outbound_tag, target_domain,"
                    " status, latency_ms, error_msg, ts) VALUES (?,?,?,?,?,?)",
                    (protocol, domain, st,
                     120 if st == 'ok' else latency_ms,
                     None if st == 'ok' else st, ts),
                )
        conn.commit()


def probe_world(db, now, *, dark=(), degraded=(), absent=(), protocols=PROTOCOLS,
                healthy_ok=7):
    """Replace the probe table with a fresh 3-run window relative to ``now``.
    dark = no alive row at all; degraded = alive (latency) but 1/10 ok."""
    with db._connect() as conn:
        conn.execute("DELETE FROM outbound_health")
        conn.commit()
    for p in protocols:
        if p in absent:
            continue
        if p in dark:
            seed_probe(db, p, now=now, ok_per_run=0, latency_ms=None)
        elif p in degraded:
            seed_probe(db, p, now=now, ok_per_run=1, status='error', latency_ms=800)
        else:
            seed_probe(db, p, now=now, ok_per_run=healthy_ok)


def seed_dpi(db, *, now, asn, hsfail, conn=0, minutes_ago=10, country='RU',
             tag='reality'):
    with db._connect() as c:
        c.execute(
            "INSERT INTO dpi_metrics (snapshot_at, country, asn, as_org, inbound_tag,"
            " conn_count, handshake_fail_count) VALUES (?,?,?,?,?,?,?)",
            ((now - timedelta(minutes=minutes_ago)).isoformat(), country, asn,
             'MegaFon', tag, conn, hsfail),
        )
        c.commit()


def clear(db, table):
    with db._connect() as c:
        c.execute(f"DELETE FROM {table}")
        c.commit()


def seed_user(db, chat_id, *, asn=None, status='demo'):
    with db._connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO users (chat_id, username, status, last_asn)"
            " VALUES (?,?,?,?)",
            (str(chat_id), f'u{chat_id}', status, asn),
        )
        c.commit()


def seed_allows(db, chat_id, n, *, now, minutes_ago=30, decision='allow'):
    ts = (now - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%d %H:%M:%S')
    with db._connect() as c:
        c.executemany(
            "INSERT INTO hy2_auth_log (ts, chat_id, decision, addr_ip)"
            " VALUES (?,?,?,?)",
            [(ts, str(chat_id), decision, '10.0.0.1')] * n,
        )
        c.commit()


def seed_report(db, chat_id, asn, *, now, minutes_ago=30):
    ts = (now - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%d %H:%M:%S')
    with db._connect() as c:
        c.execute(
            "INSERT INTO user_failure_reports (ts, chat_id, country, asn)"
            " VALUES (?,?,?,?)",
            (ts, str(chat_id), 'RU', asn),
        )
        c.commit()


def auto(db):
    return json.loads(db.get_setting('cascade_auto') or '{}')


def mon_state(db):
    return json.loads(db.get_setting('dpi_monitor_state') or '{}')


def admin_rows(db):
    with db._connect() as c:
        return [tuple(r) for r in c.execute(
            "SELECT admin_id, action, target_id, details FROM admin_actions"
            " ORDER BY id").fetchall()]


def ticks(monitor, n, *, start=T0, before=None):
    """Run ``n`` evaluations STEP apart. ``before(now)`` reseeds per tick."""
    out = []
    now = start
    for _ in range(n):
        if before:
            before(now)
        out.append(monitor.run_once(now=now))
        now += STEP
    return out


def paid():
    return SimpleNamespace(status='paid', last_asn=None, last_country=None)


# ---- R1: probe DARK ---------------------------------------------------------

class TestProbeDark:

    def test_dark_protocol_is_demoted_globally_on_the_second_evaluation(self, monitor, db):
        """The incident, replayed: reality all-timeout, the others at
        the 7/10 norm. Two consecutive evaluations, then the move."""
        probe_world(db, T0, dark=('reality',))

        first = monitor.run_once(now=T0)
        assert first == []
        assert auto(db) == {'asn': {}, 'global': {}}
        assert mon_state(db)['targets']['global:reality']['bad'] == 1

        second = monitor.run_once(now=T0 + STEP)
        assert [c.to_dict() for c in second] == [{
            'scope': 'global', 'target': None, 'protocol': 'reality',
            'action': 'demote', 'reason': 'probe_dark',
            'evidence': second[0].evidence,
        }]
        assert 'DARK' in second[0].evidence and '0/30' in second[0].evidence
        entry = auto(db)['global']['reality']
        assert entry['reason'] == 'probe_dark'
        assert entry['since'] == (T0 + STEP).isoformat()
        # What the user gets: same set, reality at the tail.
        order = MyKeyAnswerHandler.get_cascade_order(db, user=paid())
        assert order == ('hy2t', 'stls', 'ws', 'hy2', 'reality')
        order_demo = MyKeyAnswerHandler.get_cascade_order(
            db, user=SimpleNamespace(status='demo', last_asn=None, last_country=None))
        assert order_demo == ('stls', 'ws', 'hy2')

    def test_dark_ws_moves_behind_everything_the_operator_ordered(self, monitor, db):
        probe_world(db, T0, dark=('ws',))
        ticks(monitor, 2)
        assert MyKeyAnswerHandler.get_cascade_order(db, user=paid()) == (
            'hy2t', 'stls', 'hy2', 'reality', 'ws')
        # Nothing removed, nothing disabled: the operator's config is untouched.
        assert db.get_setting('cascade_protocol_order') is None
        assert set(MyKeyAnswerHandler.get_cascade_order(db, user=paid())) == set(PROTOCOLS)

    def test_healthy_baseline_never_fires(self, monitor, db):
        changes = ticks(monitor, 4, before=lambda now: probe_world(db, now))
        assert changes == [[], [], [], []]
        assert auto(db) == {'asn': {}, 'global': {}}
        assert mon_state(db)['targets'] == {}
        assert mon_state(db)['runs'] == 4

    def test_dark_needs_min_samples(self, monitor, db):
        """One run (10 rows) of a dark protocol is a deploy blip, not
        evidence — below PROBE_MIN_SAMPLES it is not even measured."""
        def world(now):
            with db._connect() as c:
                c.execute("DELETE FROM outbound_health"); c.commit()
            seed_probe(db, 'reality', now=now, runs=1, ok_per_run=0)
            for p in ('hy2', 'ws', 'stls', 'hy2t'):
                seed_probe(db, p, now=now)
        assert ticks(monitor, 2, before=world) == [[], []]
        assert 'global:reality' not in mon_state(db)['targets']

    def test_all_dark_is_an_upstream_outage_nothing_moves(self, monitor, db):
        """Every protocol dark at once = exit link / probe sidecar down.
        The pager owns that; reordering five dead protocols is noise and
        would leave five demotions to unwind afterwards."""
        changes = ticks(monitor, 3, before=lambda now: probe_world(db, now, dark=PROTOCOLS))
        assert changes == [[], [], []]
        assert auto(db) == {'asn': {}, 'global': {}}
        # Streaks are frozen, not accumulated: no target rows at all.
        assert mon_state(db)['targets'] == {}

    def test_all_dark_with_a_single_measured_protocol_is_a_real_outage(self, monitor, db):
        """Mirror of the pager: 'all dark' needs ≥2 measured protocols —
        one dark protocol alone IS a per-protocol incident."""
        probe_world(db, T0, dark=('reality',), absent=('hy2', 'hy2t', 'ws', 'stls'))
        changes = ticks(monitor, 2)
        assert [c.protocol for c in changes[1]] == ['reality']

    def test_stale_probes_skip_probe_rules_but_reality_asn_still_works(self, monitor, db):
        """Probe pipeline dead for an hour: R1/R2 have nothing to say
        (they do not fire AND they do not restore), the dpi_metrics
        signal is independent and keeps working."""
        probe_world(db, T0 - timedelta(minutes=60), dark=('reality',))
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=879, conn=0)

        changes = ticks(monitor, 2)

        assert changes[0] == []
        assert [c.to_dict() for c in changes[1]] == [{
            'scope': 'asn', 'target': MEGAFON, 'protocol': 'reality',
            'action': 'demote', 'reason': 'reality_asn',
            'evidence': changes[1][0].evidence,
        }]
        assert 'global:reality' not in mon_state(db)['targets']
        assert auto(db)['global'] == {}
        assert list(auto(db)['asn'][MEGAFON]) == ['reality']

    def test_stale_probes_freeze_an_existing_probe_demotion(self, monitor, db):
        """Demoted for DARK, then the probe job dies: no data ≠ good
        news. The demotion stays until probes come back and say so."""
        ticks(monitor, 2, before=lambda now: probe_world(db, now, dark=('ws',)))
        assert 'ws' in auto(db)['global']
        stale = T0 + 2 * STEP
        # Rows stop being written at ``stale``; six more evaluations pass.
        probe_world(db, stale, dark=('ws',))
        changes = ticks(monitor, 6, start=stale + timedelta(minutes=50))
        assert changes == [[]] * 6
        assert 'ws' in auto(db)['global']
        assert mon_state(db)['targets']['global:ws']['good'] == 0

    def test_two_dark_protocols_are_demoted_in_one_run_third_waits(self, monitor, db):
        """MAX_CHANGES_PER_RUN = 2: hy2 + reality move now (key order
        within equal rank), stls on the next run — the streak survives
        the cap."""
        changes = ticks(monitor, 3, before=lambda now: probe_world(
            db, now, dark=('reality', 'hy2', 'stls')))
        assert changes[0] == []
        assert [(c.protocol, c.action) for c in changes[1]] == [
            ('hy2', 'demote'), ('reality', 'demote')]
        assert [(c.protocol, c.action) for c in changes[2]] == [('stls', 'demote')]
        # Stable partition: the demoted keep THEIR original relative order
        # (stls, hy2, reality), not the order they were demoted in.
        assert MyKeyAnswerHandler.get_cascade_order(db, user=paid()) == (
            'hy2t', 'ws', 'stls', 'hy2', 'reality')


# ---- R2: probe DEGRADED -----------------------------------------------------

class TestProbeDegraded:

    def test_degraded_protocol_is_demoted_with_its_own_reason(self, monitor, db):
        """2026-09-05: ws at 2/30 ok for an hour, tunnel up (latencies
        present) but almost nothing through — not dark, still not
        something to hand a demo user as protocol #2."""
        changes = ticks(monitor, 2, before=lambda now: probe_world(db, now, degraded=('ws',)))
        assert [(c.protocol, c.action, c.reason) for c in changes[1]] == [
            ('ws', 'demote', 'probe_degraded')]
        assert 'DEGRADED' in changes[1][0].evidence and '3/30' in changes[1][0].evidence
        assert auto(db)['global']['ws']['reason'] == 'probe_degraded'

    def test_errors_that_carried_a_latency_are_alive_not_dark(self, monitor, db):
        """Liveness is 'a round trip happened', same as the pager: 0/30
        ok but every row has a latency (sites blocking the exit IP fail
        THROUGH the tunnel — 5306 of 19535 recent error rows look like
        that) is DEGRADED, not DARK. Same demotion, different reason,
        and the difference matters for what the agent goes to look at."""
        def world(now):
            with db._connect() as c:
                c.execute("DELETE FROM outbound_health"); c.commit()
            seed_probe(db, 'ws', now=now, ok_per_run=0, status='error', latency_ms=650)
            for p in ('hy2', 'reality', 'stls', 'hy2t'):
                seed_probe(db, p, now=now)
        changes = ticks(monitor, 2, before=world)
        assert [(c.protocol, c.reason) for c in changes[1]] == [('ws', 'probe_degraded')]
        assert 'ok 0/30' in changes[1][0].evidence

    def test_degraded_needs_25_samples(self, monitor, db):
        """Two runs (20 rows) with 1/10 ok: alive (so not dark), but a
        20-row window is one dip, not a trend."""
        def world(now):
            with db._connect() as c:
                c.execute("DELETE FROM outbound_health"); c.commit()
            seed_probe(db, 'ws', now=now, runs=2, ok_per_run=1, status='error', latency_ms=800)
            for p in ('hy2', 'reality', 'stls', 'hy2t'):
                seed_probe(db, p, now=now)
        assert ticks(monitor, 2, before=world) == [[], []]
        assert mon_state(db)['targets'] == {}

    def test_ok_ratio_above_a_quarter_is_not_degraded(self, monitor, db):
        """3/10 ok = 0.30 ≥ 0.25: worse than the norm, not a demotion."""
        def world(now):
            with db._connect() as c:
                c.execute("DELETE FROM outbound_health"); c.commit()
            seed_probe(db, 'ws', now=now, ok_per_run=3, status='error', latency_ms=800)
            for p in ('hy2', 'reality', 'stls', 'hy2t'):
                seed_probe(db, p, now=now)
        assert ticks(monitor, 2, before=world) == [[], []]

    def test_dark_outranks_degraded_outranks_reality_asn_under_the_cap(self, monitor, db):
        """Three candidates ripen together: the run takes DARK and
        DEGRADED, the per-ASN Reality move waits one tick."""
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=100, conn=0)
        changes = ticks(monitor, 3, before=lambda now: probe_world(
            db, now, dark=('stls',), degraded=('ws',)))
        assert [(c.key, c.reason) for c in changes[1]] == [
            ('global:stls', 'probe_dark'), ('global:ws', 'probe_degraded')]
        assert [(c.key, c.reason) for c in changes[2]] == [
            (f'asn:{MEGAFON}:reality', 'reality_asn')]


# ---- R3: Reality handshake failures per ASN ---------------------------------

class TestRealityAsn:

    def test_fires_at_30_hsfail_and_zero_conn_for_that_asn_only(self, monitor, db):
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=30, conn=0)
        seed_dpi(db, now=T0, asn='AS8359', hsfail=2, conn=40)   # MTS, healthy
        changes = ticks(monitor, 2, before=lambda now: probe_world(db, now))

        assert [(c.scope, c.target, c.protocol, c.reason) for c in changes[1]] == [
            ('asn', MEGAFON, 'reality', 'reality_asn')]
        assert '30 / conn 0' in changes[1][0].evidence
        assert MyKeyAnswerHandler.get_cascade_order(db, asn=MEGAFON, user=paid()) == (
            'hy2t', 'stls', 'ws', 'hy2', 'reality')   # reality already last by default…
        # …so pin it with a direct-first override where reality leads:
        db.set_setting('cascade_by_asn', json.dumps({MEGAFON: ['reality', 'hy2t', 'hy2'],
                                                     'AS8359': ['reality', 'hy2t']}))
        assert MyKeyAnswerHandler.get_cascade_order(db, asn=MEGAFON, user=paid()) == (
            'hy2t', 'hy2', 'stls', 'ws', 'reality')
        assert MyKeyAnswerHandler.get_cascade_order(db, asn='AS8359', user=paid())[0] == 'reality'
        assert MyKeyAnswerHandler.get_cascade_order(db, user=paid())[0] == 'hy2t'

    def test_29_hsfail_is_below_threshold(self, monitor, db):
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=29, conn=0)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]

    def test_needs_twice_the_connections(self, monitor, db):
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=40, conn=21)    # 40 < 2×21
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]
        clear(db, 'dpi_metrics')
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=40, conn=20)    # 40 ≥ 2×20
        changes = ticks(monitor, 2, before=lambda now: probe_world(db, now))
        assert [c.key for c in changes[1]] == [f'asn:{MEGAFON}:reality']

    def test_snapshots_add_up_across_the_window(self, monitor, db):
        for m in (10, 40, 70, 100):
            seed_dpi(db, now=T0, asn=MEGAFON, hsfail=8, conn=0, minutes_ago=m)
        changes = ticks(monitor, 2, before=lambda now: probe_world(db, now))
        assert [c.key for c in changes[1]] == [f'asn:{MEGAFON}:reality']

    def test_tunnel_bucket_and_other_inbounds_are_ignored(self, monitor, db):
        """cf-ws / ss2022 land in '*TUNNEL*' (entry MASQUERADE) and their
        hsfail is background probing; and a non-reality tag says nothing
        about Reality."""
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=500, conn=0, country='*TUNNEL*')
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=500, conn=0, tag='cf-ws')
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=500, conn=0, tag='ss2022')
        seed_dpi(db, now=T0, asn=None, hsfail=500, conn=0)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]

    def test_rows_older_than_two_hours_are_ignored(self, monitor, db):
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=879, conn=0, minutes_ago=130)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]

    def test_asn_is_case_normalized(self, monitor, db):
        seed_dpi(db, now=T0, asn='as31133 ', hsfail=50, conn=0)
        changes = ticks(monitor, 2, before=lambda now: probe_world(db, now))
        assert changes[1][0].target == MEGAFON
        assert MEGAFON in auto(db)['asn']


# ---- R4: hy2 reconnect storm per user → ASN ---------------------------------

class TestUdpStorm:

    def test_storm_demotes_both_hy2_variants_for_the_users_asn(self, monitor, db):
        """ziriki, 2026-09-01/02: throttled mobile UDP → 17-25 auths/h.
        The auth row carries no ASN (src is entry), users.last_asn does."""
        seed_user(db, 1001, asn=MEGAFON, status='paid')
        seed_allows(db, 1001, 30, now=T0)
        changes = ticks(monitor, 2, before=lambda now: probe_world(db, now))

        assert sorted((c.scope, c.target, c.protocol, c.action, c.reason)
                      for c in changes[1]) == [
            ('asn', MEGAFON, 'hy2', 'demote', 'udp_storm_asn'),
            ('asn', MEGAFON, 'hy2t', 'demote', 'udp_storm_asn'),
        ]
        assert 'макс 30' in changes[1][0].evidence
        assert sorted(auto(db)['asn'][MEGAFON]) == ['hy2', 'hy2t']
        assert MyKeyAnswerHandler.get_cascade_order(db, asn=MEGAFON, user=paid()) == (
            'stls', 'ws', 'reality', 'hy2t', 'hy2')
        # Only that ASN: a user elsewhere keeps the paid default.
        assert MyKeyAnswerHandler.get_cascade_order(db, asn='AS8359', user=paid())[0] == 'hy2t'

    def test_29_allows_is_not_a_storm(self, monitor, db):
        seed_user(db, 1001, asn=MEGAFON)
        seed_allows(db, 1001, 29, now=T0)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]

    def test_denies_do_not_count(self, monitor, db):
        seed_user(db, 1001, asn=MEGAFON)
        seed_allows(db, 1001, 60, now=T0, decision='deny')
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]

    def test_storm_from_a_user_without_known_asn_is_not_attributed(self, monitor, db):
        """53 of 85 active users have last_asn NULL — a storm there is
        real but has no ASN to act on; a global move on one user's
        radio would be wrong."""
        seed_user(db, 1001, asn=None)
        seed_allows(db, 1001, 80, now=T0)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]
        assert auto(db) == {'asn': {}, 'global': {}}

    def test_allows_older_than_two_hours_do_not_count(self, monitor, db):
        seed_user(db, 1001, asn=MEGAFON)
        seed_allows(db, 1001, 80, now=T0, minutes_ago=125)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]

    def test_two_healthy_users_do_not_add_up_to_a_storm(self, monitor, db):
        """The threshold is per chat_id, not per ASN: 2×20 is two heavy
        users, not one throttled radio."""
        seed_user(db, 1001, asn=MEGAFON)
        seed_user(db, 1002, asn=MEGAFON)
        seed_allows(db, 1001, 20, now=T0)
        seed_allows(db, 1002, 20, now=T0)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]


# ---- R5: the user's "не работает" button -----------------------------------

class TestUserReports:

    def test_two_users_of_one_asn_demote_the_head_of_that_asn(self, monitor, db):
        seed_user(db, 2001, asn=MEGAFON)
        seed_user(db, 2002, asn=MEGAFON)
        seed_report(db, 2001, MEGAFON, now=T0, minutes_ago=200)
        seed_report(db, 2002, MEGAFON, now=T0, minutes_ago=20)
        changes = ticks(monitor, 2, before=lambda now: probe_world(db, now))

        assert [(c.scope, c.target, c.protocol, c.reason) for c in changes[1]] == [
            ('asn', MEGAFON, 'hy2t', 'user_reports_asn')]   # head of the default
        assert '2 жалобы' in changes[1][0].evidence
        assert MyKeyAnswerHandler.get_cascade_order(db, asn=MEGAFON, user=paid()) == (
            'stls', 'ws', 'hy2', 'reality', 'hy2t')

    def test_one_user_reporting_twice_is_not_enough(self, monitor, db):
        seed_user(db, 2001, asn=MEGAFON)
        seed_report(db, 2001, MEGAFON, now=T0, minutes_ago=90)
        seed_report(db, 2001, MEGAFON, now=T0, minutes_ago=20)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]

    def test_two_users_of_different_asns_do_nothing(self, monitor, db):
        seed_report(db, 2001, MEGAFON, now=T0)
        seed_report(db, 2002, 'AS8359', now=T0)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]

    def test_reports_older_than_six_hours_do_not_count(self, monitor, db):
        seed_report(db, 2001, MEGAFON, now=T0, minutes_ago=370)
        seed_report(db, 2002, MEGAFON, now=T0, minutes_ago=20)
        assert ticks(monitor, 2, before=lambda now: probe_world(db, now)) == [[], []]

    def test_r5_pushes_at_most_two_protocols_per_asn(self, monitor, db):
        """The reports stay in the 6-h window for the whole run: head
        hy2t goes at tick 2, the new head stls at tick 4, and then the
        rule stops — it may not reshuffle a whole ASN on two taps."""
        seed_report(db, 2001, MEGAFON, now=T0)
        seed_report(db, 2002, MEGAFON, now=T0)
        changes = ticks(monitor, 9, before=lambda now: probe_world(db, now))

        applied = [(i, c.protocol) for i, run in enumerate(changes) for c in run]
        assert applied == [(1, 'hy2t'), (3, 'stls')]
        assert sorted(auto(db)['asn'][MEGAFON]) == ['hy2t', 'stls']
        assert MyKeyAnswerHandler.get_cascade_order(db, asn=MEGAFON, user=paid()) == (
            'ws', 'hy2', 'reality', 'hy2t', 'stls')

    def test_head_follows_the_operators_asn_override(self, monitor, db):
        db.set_setting('cascade_by_asn', json.dumps({MEGAFON: ['reality', 'hy2', 'stls']}))
        seed_report(db, 2001, MEGAFON, now=T0)
        seed_report(db, 2002, MEGAFON, now=T0)
        changes = ticks(monitor, 2, before=lambda now: probe_world(db, now))
        assert [c.protocol for c in changes[1]] == ['reality']
        # The override itself is untouched; auto only reorders within it.
        assert json.loads(db.get_setting('cascade_by_asn')) == {MEGAFON: ['reality', 'hy2', 'stls']}
        assert MyKeyAnswerHandler.get_cascade_order(db, asn=MEGAFON, user=paid()) == (
            'hy2', 'stls', 'hy2t', 'ws', 'reality')


# ---- hysteresis -------------------------------------------------------------

class TestHysteresis:

    def test_two_bad_then_six_good_end_to_end(self, monitor, db, bot):
        """1 bad → nothing; 2 → demote; 5 good → still demoted; 6 → restore."""
        world = {'dark': ('reality',)}

        def before(now):
            probe_world(db, now, dark=world['dark'], healthy_ok=10)

        bad = ticks(monitor, 2, before=before)
        assert bad[0] == [] and [c.action for c in bad[1]] == ['demote']

        world['dark'] = ()
        good = ticks(monitor, 6, start=T0 + 2 * STEP, before=before)
        assert good[:5] == [[]] * 5, "five good evaluations must not restore yet"
        assert 'reality' in auto(db)['global'] or good[5]   # still demoted before the 6th
        assert [(c.protocol, c.action, c.reason) for c in good[5]] == [
            ('reality', 'restore', 'probe_dark')]
        assert 'молчит 6 оценок' in good[5][0].evidence

        assert auto(db) == {'asn': {}, 'global': {}}
        assert MyKeyAnswerHandler.get_cascade_order(db, user=paid()) == (
            'hy2t', 'stls', 'ws', 'hy2', 'reality')
        assert [(a, t) for _id, a, t, _d in admin_rows(db)] == [
            ('cascade_auto_demote', 'global:global:reality'),
            ('cascade_auto_restore', 'global:global:reality'),
        ]
        assert bot.send_message.call_count == 2
        assert 'на место' in bot.send_message.call_args_list[1].kwargs['text']
        entry = mon_state(db)['targets']['global:reality']
        assert (entry['bad'], entry['good'], entry['rule']) == (0, 0, None)
        assert entry['last_change'] == (T0 + 7 * STEP).isoformat()

    def test_good_streak_counts_before_the_sixth_are_visible_in_state(self, monitor, db):
        world = {'dark': ('ws',)}
        ticks(monitor, 2, before=lambda now: probe_world(db, now, dark=world['dark'], healthy_ok=10))
        world['dark'] = ()
        ticks(monitor, 3, start=T0 + 2 * STEP,
              before=lambda now: probe_world(db, now, healthy_ok=10))
        assert mon_state(db)['targets']['global:ws']['good'] == 3
        assert 'ws' in auto(db)['global']

    def test_one_quiet_evaluation_breaks_the_bad_streak(self, monitor, db):
        world = {'dark': ('ws',)}
        before = lambda now: probe_world(db, now, dark=world['dark'])   # noqa: E731
        ticks(monitor, 1, before=before)
        world['dark'] = ()
        ticks(monitor, 1, start=T0 + STEP, before=before)
        world['dark'] = ('ws',)
        changes = ticks(monitor, 1, start=T0 + 2 * STEP, before=before)
        assert changes == [[]]
        assert mon_state(db)['targets']['global:ws']['bad'] == 1
        assert auto(db)['global'] == {}

    # -- pure-core cases: evaluate() with synthetic signals --------------------

    @staticmethod
    def sig(*, dark=(), degraded=(), stale=False, all_dark=False, reality=(),
            storms=(), reports=(), heads=None):
        s = dm.empty_signals()
        s['probe'].update({
            'stale': stale, 'newest_age_min': 5.0,
            'dark': {p: f'{p} dark' for p in dark},
            'degraded': {p: f'{p} degraded' for p in degraded},
            'measured': list(PROTOCOLS), 'all_dark': all_dark,
        })
        s['reality_asn'] = {a: f'{a} reality' for a in reality}
        s['udp_storm_asn'] = {a: f'{a} storm' for a in storms}
        s['reports_asn'] = {a: f'{a} reports' for a in reports}
        s['asn_head'] = dict(heads or {})
        return s

    def test_min_gap_between_changes_of_one_target(self):
        """Six good evaluations one minute apart reach the good streak
        but not the 30-min gap after the demotion; the restore waits."""
        state = dm.empty_state()
        now = T0
        for _ in range(2):
            state, changes = DPIMonitor.evaluate(self.sig(dark=('ws',)), state, now)
            now += timedelta(minutes=1)
        assert [c.action for c in changes] == ['demote']
        demoted_at = now - timedelta(minutes=1)

        for _ in range(8):
            state, changes = DPIMonitor.evaluate(self.sig(), state, now)
            assert changes == [], f"restored only {now - demoted_at} after the demotion"
            now += timedelta(minutes=1)
        assert state['targets']['global:ws']['good'] >= 6

        state, changes = DPIMonitor.evaluate(
            self.sig(), state, demoted_at + timedelta(minutes=30))
        assert [c.action for c in changes] == ['restore']

    def test_restores_are_ranked_after_demotes_under_the_cap(self):
        state = dm.empty_state()
        # A target long demoted and ready to be restored…
        state['auto']['global']['stls'] = {'since': (T0 - timedelta(hours=2)).isoformat(),
                                           'reason': 'probe_dark', 'evidence': 'x'}
        state['targets']['global:stls'] = {'bad': 0, 'good': 5, 'rule': 'probe_dark',
                                           'last_change': (T0 - timedelta(hours=2)).isoformat()}
        # …and two demotions ripening in the same run.
        for key in ('global:ws', 'global:hy2'):
            state['targets'][key] = {'bad': 1, 'good': 0, 'rule': 'probe_dark',
                                     'last_change': None}
        state, changes = DPIMonitor.evaluate(self.sig(dark=('ws', 'hy2')), state, T0)
        assert [(c.action, c.protocol) for c in changes] == [
            ('demote', 'hy2'), ('demote', 'ws')]
        assert 'stls' in state['auto']['global']
        state, changes = DPIMonitor.evaluate(self.sig(dark=('ws', 'hy2')), state, T0 + STEP)
        assert [(c.action, c.protocol) for c in changes] == [('restore', 'stls')]

    def test_worsening_from_degraded_to_dark_keeps_it_demoted(self):
        state = dm.empty_state()
        state['auto']['global']['ws'] = {'since': T0.isoformat(), 'reason': 'probe_degraded',
                                         'evidence': 'x'}
        state['targets']['global:ws'] = {'bad': 0, 'good': 4, 'rule': 'probe_degraded',
                                         'last_change': (T0 - timedelta(hours=1)).isoformat()}
        for i in range(8):
            state, changes = DPIMonitor.evaluate(
                self.sig(dark=('ws',)), state, T0 + i * STEP)
            assert changes == []
        assert state['targets']['global:ws']['good'] == 0
        assert 'ws' in state['auto']['global']

    def test_still_degraded_keeps_a_degraded_demotion(self):
        """A DEGRADED demotion is judged against the whole probe family:
        as long as ws is still degraded (not just still dark) there is
        no 'good' evaluation to count."""
        state = dm.empty_state()
        state['auto']['global']['ws'] = {'since': T0.isoformat(), 'reason': 'probe_degraded',
                                         'evidence': 'x'}
        for i in range(1, 9):
            state, changes = DPIMonitor.evaluate(
                self.sig(degraded=('ws',)), state, T0 + i * STEP)
            assert changes == []
        assert state['targets']['global:ws']['good'] == 0
        assert 'ws' in state['auto']['global']

    def test_a_probe_demotion_is_not_restored_by_quiet_asn_rules(self):
        """'good' is judged by the rule that demoted the target. Reality
        dark on the probes stays demoted no matter how quiet R3 is."""
        state = dm.empty_state()
        state['auto']['global']['reality'] = {'since': T0.isoformat(), 'reason': 'probe_dark',
                                              'evidence': 'x'}
        for i in range(10):
            state, changes = DPIMonitor.evaluate(
                self.sig(dark=('reality',)), state, T0 + i * STEP)
            assert changes == []
        assert state['targets']['global:reality']['good'] == 0

    def test_an_asn_demotion_restores_when_its_own_rule_is_quiet(self):
        """R3 demoted reality for MegaFon; the probes never saw anything
        (the probe sidecar is not on MegaFon) and R3 goes quiet → restore
        after six evaluations, unaffected by any global probe state."""
        state = dm.empty_state()
        state['auto']['asn'][MEGAFON] = {'reality': {'since': T0.isoformat(),
                                                     'reason': 'reality_asn', 'evidence': 'x'}}
        seen = []
        for i in range(1, 8):
            state, changes = DPIMonitor.evaluate(self.sig(), state, T0 + i * STEP)
            seen.append([(c.action, c.scope, c.target, c.protocol) for c in changes])
        assert seen == [[]] * 5 + [[('restore', 'asn', MEGAFON, 'reality')]] + [[]]
        assert state['auto'] == {'asn': {}, 'global': {}}

    def test_an_asn_rule_that_keeps_firing_keeps_its_demotion(self):
        """R3 and R4 still firing for MegaFon: no 'good' evaluation is
        ever counted, however long it goes on — the restore needs the
        RULE quiet, not just time."""
        state = dm.empty_state()
        state['auto']['asn'][MEGAFON] = {
            'reality': {'since': T0.isoformat(), 'reason': 'reality_asn', 'evidence': 'x'},
            'hy2': {'since': T0.isoformat(), 'reason': 'udp_storm_asn', 'evidence': 'x'},
            'hy2t': {'since': T0.isoformat(), 'reason': 'udp_storm_asn', 'evidence': 'x'},
        }
        for i in range(1, 13):
            state, changes = DPIMonitor.evaluate(
                self.sig(reality=(MEGAFON,), storms=(MEGAFON,)), state, T0 + i * STEP)
            assert changes == [], i
        for proto in ('reality', 'hy2', 'hy2t'):
            assert proto in state['auto']['asn'][MEGAFON]
            assert state['targets'][f'asn:{MEGAFON}:{proto}']['good'] == 0

    def test_min_gap_also_holds_between_a_restore_and_the_next_demote(self):
        """Restored at T, dark again at T+1: the bad streak ripens at
        T+2 min but the target may not move again before T+30."""
        state = dm.empty_state()
        state['auto']['global']['ws'] = {'since': (T0 - timedelta(hours=1)).isoformat(),
                                         'reason': 'probe_dark', 'evidence': 'x'}
        state['targets']['global:ws'] = {'bad': 0, 'good': 5, 'rule': 'probe_dark',
                                         'last_change': (T0 - timedelta(hours=1)).isoformat()}
        state, changes = DPIMonitor.evaluate(self.sig(), state, T0)
        assert [c.action for c in changes] == ['restore']
        for m in (1, 2, 3, 10, 29):
            state, changes = DPIMonitor.evaluate(
                self.sig(dark=('ws',)), state, T0 + timedelta(minutes=m))
            assert changes == [], f"moved again only {m} min after the restore"
        assert state['targets']['global:ws']['bad'] >= 2
        state, changes = DPIMonitor.evaluate(
            self.sig(dark=('ws',)), state, T0 + timedelta(minutes=30))
        assert [c.action for c in changes] == ['demote']

    def test_r5_restore_is_judged_per_asn_not_per_head(self):
        """After R5 moved hy2t to the tail the head is stls; the reports
        are still there → hy2t is NOT 'good' just because the rule now
        points at another protocol."""
        state = dm.empty_state()
        state['auto']['asn'][MEGAFON] = {'hy2t': {'since': T0.isoformat(),
                                                  'reason': 'user_reports_asn', 'evidence': 'x'}}
        for i in range(1, 10):
            state, _ = DPIMonitor.evaluate(
                self.sig(reports=(MEGAFON,), heads={MEGAFON: 'stls'}), state, T0 + i * STEP)
        assert 'hy2t' in state['auto']['asn'][MEGAFON]
        assert state['targets'][f'asn:{MEGAFON}:hy2t']['good'] == 0

    def test_evaluate_does_not_mutate_its_inputs(self):
        state = dm.empty_state()
        state['targets']['global:ws'] = {'bad': 1, 'good': 0, 'rule': 'probe_dark',
                                         'last_change': None}
        snapshot = json.dumps(state, sort_keys=True)
        signals = self.sig(dark=('ws',))
        sig_snapshot = json.dumps(signals, sort_keys=True)
        new_state, changes = DPIMonitor.evaluate(signals, state, T0)
        assert [c.action for c in changes] == ['demote']
        assert json.dumps(state, sort_keys=True) == snapshot
        assert json.dumps(signals, sort_keys=True) == sig_snapshot
        assert new_state is not state

    def test_all_dark_freezes_streaks_instead_of_resetting_them(self):
        state = dm.empty_state()
        state, _ = DPIMonitor.evaluate(self.sig(dark=('ws',)), state, T0)
        state, changes = DPIMonitor.evaluate(
            self.sig(dark=PROTOCOLS, all_dark=True), state, T0 + STEP)
        assert changes == []
        assert state['targets']['global:ws']['bad'] == 1
        state, changes = DPIMonitor.evaluate(self.sig(dark=('ws',)), state, T0 + 2 * STEP)
        assert [c.action for c in changes] == ['demote']


# ---- guards, persistence, visibility ---------------------------------------

class TestGuards:

    def test_disabled_flag_skips_evaluation_and_leaves_state_untouched(self, monitor, db, bot, caplog):
        db.set_setting('dpi_monitor_enabled', '0')
        with caplog.at_level(logging.INFO, logger='bot.services.dpi_monitor'):
            changes = ticks(monitor, 3, before=lambda now: probe_world(db, now, dark=('reality',)))
        assert changes == [[], [], []]
        assert db.get_setting('cascade_auto') is None
        assert db.get_setting('dpi_monitor_state') is None
        assert admin_rows(db) == []
        bot.send_message.assert_not_called()
        assert 'disabled' in caplog.text

    def test_env_default_applies_only_when_the_setting_is_unset(self, db, bot):
        cfg = Mock()
        cfg.FORUM_GROUP_ID = -1
        cfg.TOPIC_AI = 1
        cfg.DPI_MONITOR_ENABLED = False
        m = DPIMonitor(db, cfg, bot)
        assert m.is_enabled() is False
        db.set_setting('dpi_monitor_enabled', '1')          # /cascade on
        assert m.is_enabled() is True
        m.set_enabled(False)                                 # /cascade off
        assert db.get_setting('dpi_monitor_enabled') == '0'
        assert m.is_enabled() is False
        cfg.DPI_MONITOR_ENABLED = True
        db.set_setting('dpi_monitor_enabled', '')
        assert m.is_enabled() is True

    def test_dry_run_returns_the_changes_and_writes_nothing(self, monitor, db, bot):
        probe_world(db, T0, dark=('reality',))
        monitor.run_once(now=T0)
        state_before = db.get_setting('dpi_monitor_state')

        changes = monitor.run_once(dry_run=True, now=T0 + STEP)

        assert [(c.protocol, c.action) for c in changes] == [('reality', 'demote')]
        assert db.get_setting('dpi_monitor_state') == state_before
        assert auto(db) == {'asn': {}, 'global': {}}
        assert admin_rows(db) == []
        bot.send_message.assert_not_called()
        # The real run right after still applies it (streak was not consumed).
        assert [c.action for c in monitor.run_once(now=T0 + STEP)] == ['demote']

    def test_apply_writes_audit_rows_and_one_batched_topic_message(self, monitor, db, bot, config):
        seed_user(db, 1001, asn=MEGAFON)
        seed_allows(db, 1001, 41, now=T0)
        ticks(monitor, 2, before=lambda now: probe_world(db, now))

        rows = admin_rows(db)
        assert [(a, act, t) for a, act, t, _d in rows] == [
            ('dpi_monitor', 'cascade_auto_demote', f'asn:{MEGAFON}:hy2'),
            ('dpi_monitor', 'cascade_auto_demote', f'asn:{MEGAFON}:hy2t'),
        ]
        assert rows[0][3].startswith('udp_storm_asn: ') and 'макс 41' in rows[0][3]

        assert bot.send_message.call_count == 1
        kw = bot.send_message.call_args.kwargs
        assert kw['chat_id'] == config.FORUM_GROUP_ID
        assert kw['message_thread_id'] == config.TOPIC_AI
        assert kw['parse_mode'] == 'HTML'
        text = kw['text']
        assert text.startswith('🔁 <b>Каскад: авто-изменения</b>')
        assert '<b>hy2</b> → в конец (AS31133)' in text
        assert '<b>hy2t</b> → в конец (AS31133)' in text
        assert text.count('Причина:') == 2
        assert text.rstrip().endswith('Отменить: /cascade reset')

    def test_single_change_message_shape(self, monitor, db, bot):
        ticks(monitor, 2, before=lambda now: probe_world(db, now, dark=('ws',)))
        text = bot.send_message.call_args.kwargs['text']
        assert text.split('\n')[0] == '🔁 Каскад: <b>ws</b> → в конец (глобально)'
        assert text.split('\n')[1].startswith('Причина: пробы 0/30')
        assert text.split('\n')[2] == 'Отменить: /cascade reset'

    def test_without_a_bot_everything_but_the_message_happens(self, db, config):
        m = DPIMonitor(db, config, bot=None)
        probe_world(db, T0, dark=('ws',))
        changes = ticks(m, 2)
        assert [c.action for c in changes[1]] == ['demote']
        assert 'ws' in auto(db)['global']
        assert len(admin_rows(db)) == 1

    def test_without_a_forum_group_the_admin_is_never_pmd(self, db, bot):
        cfg = Mock()
        cfg.FORUM_GROUP_ID = None
        cfg.TOPIC_AI = 55
        cfg.SUPER_ADMIN_ID = '1652899'
        cfg.DPI_MONITOR_ENABLED = True
        m = DPIMonitor(db, cfg, bot)
        probe_world(db, T0, dark=('ws',))
        ticks(m, 2)
        assert 'ws' in auto(db)['global']
        bot.send_message.assert_not_called()

    def test_a_failing_send_does_not_lose_the_change(self, monitor, db, bot):
        bot.send_message.side_effect = RuntimeError('tg down')
        probe_world(db, T0, dark=('ws',))
        changes = ticks(monitor, 2)
        assert [c.action for c in changes[1]] == ['demote']
        assert 'ws' in auto(db)['global']

    def test_reset_clears_both_keys_logs_and_restores_the_operator_order(self, monitor, db):
        seed_user(db, 1001, asn=MEGAFON)
        seed_allows(db, 1001, 40, now=T0)
        # Three candidates ripen at tick 2, the cap lets two through, the
        # third lands at tick 3 — hence three ticks to have all of them.
        ticks(monitor, 3, before=lambda now: probe_world(db, now, dark=('ws',)))
        assert 'ws' in auto(db)['global'] and sorted(auto(db)['asn'][MEGAFON]) == ['hy2', 'hy2t']

        before = monitor.reset(actor='1652899')

        assert sorted(dm._auto_keys(before)) == [
            f'asn:{MEGAFON}:hy2', f'asn:{MEGAFON}:hy2t', 'global:ws']
        assert db.get_setting('cascade_auto') == '{}'
        assert db.get_setting('dpi_monitor_state') == '{}'
        assert MyKeyAnswerHandler.get_cascade_order(db, asn=MEGAFON, user=paid()) == (
            'hy2t', 'stls', 'ws', 'hy2', 'reality')
        last = admin_rows(db)[-1]
        assert last[:3] == ('1652899', 'cascade_auto_reset', 'cascade_auto')
        assert 'global:ws' in last[3]
        # And the monitor starts from zero: one bad evaluation is not enough again.
        assert monitor.run_once(now=T0 + 3 * STEP) == []
        assert auto(db) == {'asn': {}, 'global': {}}

    def test_persisted_json_shapes_are_pinned(self, monitor, db):
        """The contract the admin surface and get_cascade_order read."""
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=50, conn=0)
        ticks(monitor, 2, before=lambda now: probe_world(db, now, dark=('ws',)))

        a = auto(db)
        assert set(a) == {'global', 'asn'}
        assert set(a['global']['ws']) == {'since', 'reason', 'evidence'}
        assert a['global']['ws']['since'] == (T0 + STEP).isoformat()
        assert a['global']['ws']['reason'] == 'probe_dark'
        assert set(a['asn'][MEGAFON]['reality']) == {'since', 'reason', 'evidence'}

        s = mon_state(db)
        assert set(s) == {'targets', 'last_run', 'runs'}
        assert s['runs'] == 2 and s['last_run'] == (T0 + STEP).isoformat()
        assert set(s['targets']['global:ws']) == {'bad', 'good', 'last_change', 'rule'}
        assert set(s['targets']) == {'global:ws', f'asn:{MEGAFON}:reality'}

        st = monitor.status()
        assert st['enabled'] is True
        assert st['global'] == a['global'] and st['asn'] == a['asn']
        assert st['last_run'] == s['last_run'] and st['runs'] == 2

    def test_garbage_in_the_settings_is_tolerated(self, monitor, db):
        db.set_setting('cascade_auto', '{not json')
        db.set_setting('dpi_monitor_state', '[1,2,3]')
        probe_world(db, T0, dark=('ws',))
        changes = ticks(monitor, 2)
        assert [c.action for c in changes[1]] == ['demote']
        assert 'ws' in auto(db)['global']
        assert MyKeyAnswerHandler.get_cascade_order(db, user=paid())[-1] == 'ws'

    def test_a_cleared_cascade_auto_means_nothing_is_demoted(self, monitor, db):
        """cascade_auto is the source of truth for 'demoted now': if it is
        emptied by hand, lingering streaks do not resurrect a demotion —
        the rule has to earn it again over two evaluations."""
        ticks(monitor, 2, before=lambda now: probe_world(db, now, dark=('ws',)))
        db.set_setting('cascade_auto', '{}')
        changes = ticks(monitor, 1, start=T0 + 2 * STEP,
                        before=lambda now: probe_world(db, now, dark=('ws',)))
        assert changes == [[]]
        assert auto(db)['global'] == {}

    def test_a_collector_failure_blinds_only_its_own_rule(self, monitor, db, caplog):
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=50, conn=0)
        with db._connect() as c:
            c.execute("DROP TABLE hy2_auth_log"); c.commit()
        with caplog.at_level(logging.WARNING, logger='bot.services.dpi_monitor'):
            changes = ticks(monitor, 2, before=lambda now: probe_world(db, now, dark=('ws',)))
        assert sorted(c.key for c in changes[1]) == [f'asn:{MEGAFON}:reality', 'global:ws']
        assert 'collector udp_storm_asn failed' in caplog.text


# ---- collector failure = UNKNOWN, never "quiet" -----------------------------

class _ReadBrokenConn:
    """A sqlite connection whose reads of app_settings fail while every
    other statement (including the writes) still works — the shape of a
    transient lock that ``Database.get_setting`` swallows into None."""

    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, *args):
        if 'FROM app_settings' in sql:
            raise sqlite3.OperationalError('database is locked')
        return self._c.execute(sql, *args)

    def __enter__(self):
        self._c.__enter__()
        return self

    def __exit__(self, *exc):
        return self._c.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._c, name)


class TestCollectorFailure:
    """A collector that raises must mark its rule UNKNOWN: what the rule
    holds is frozen, nothing else is touched, and the log says so on
    every run. The alternative — an empty result read as "quiet" —
    restores the demotion an hour after the table broke, silently."""

    def test_a_broken_collector_freezes_the_demotions_of_its_own_rule(self, monitor, db, caplog):
        seed_user(db, 1001, asn=MEGAFON)
        seed_allows(db, 1001, 40, now=T0)
        ticks(monitor, 2, before=lambda now: probe_world(db, now))
        assert sorted(auto(db)['asn'][MEGAFON]) == ['hy2', 'hy2t']
        with db._connect() as c:
            c.execute("DROP TABLE hy2_auth_log"); c.commit()

        with caplog.at_level(logging.WARNING, logger='bot.services.dpi_monitor'):
            changes = ticks(monitor, 8, start=T0 + 2 * STEP,
                            before=lambda now: probe_world(db, now))

        assert changes == [[]] * 8, "a broken collector must never restore"
        assert sorted(auto(db)['asn'][MEGAFON]) == ['hy2', 'hy2t']
        for proto in ('hy2', 'hy2t'):
            entry = mon_state(db)['targets'][f'asn:{MEGAFON}:{proto}']
            assert (entry['bad'], entry['good'], entry['rule']) == (0, 0, 'udp_storm_asn')
        assert len(admin_rows(db)) == 2                       # the two demotes only
        warnings = [r for r in caplog.records
                    if r.levelno == logging.WARNING and 'collector udp_storm_asn failed' in r.getMessage()]
        assert len(warnings) == 8, "one WARNING per run it stays broken"
        assert 'UNKNOWN' in warnings[0].getMessage() and 'udp_storm_asn' in warnings[0].getMessage()

    def test_a_broken_collector_leaves_the_other_rules_alone(self, monitor, db):
        """R4 collector broken: an R3 demotion still restores when R3
        goes quiet, and a probe DARK still demotes."""
        seed_dpi(db, now=T0, asn=MEGAFON, hsfail=60, conn=0)
        ticks(monitor, 2, before=lambda now: probe_world(db, now))
        assert list(auto(db)['asn'][MEGAFON]) == ['reality']
        with db._connect() as c:
            c.execute("DROP TABLE hy2_auth_log"); c.commit()
        clear(db, 'dpi_metrics')                              # R3 quiet from now on

        changes = ticks(monitor, 6, start=T0 + 2 * STEP,
                        before=lambda now: probe_world(db, now, dark=('ws',)))

        flat = [(i, c.action, c.key) for i, run in enumerate(changes) for c in run]
        assert flat == [(1, 'demote', 'global:ws'),
                        (5, 'restore', f'asn:{MEGAFON}:reality')]
        assert auto(db) == {'asn': {}, 'global': {'ws': auto(db)['global']['ws']}}

    def test_a_broken_collector_keeps_a_pending_bad_streak(self, monitor, db):
        """bad=1 for R4, then three blind runs, then the table is back:
        the streak neither grew nor reset, so the next bad run demotes."""
        seed_user(db, 1001, asn=MEGAFON)
        seed_allows(db, 1001, 40, now=T0)
        ticks(monitor, 1, before=lambda now: probe_world(db, now))
        assert mon_state(db)['targets'][f'asn:{MEGAFON}:hy2']['bad'] == 1
        with db._connect() as c:
            c.execute("DROP TABLE hy2_auth_log"); c.commit()
        assert ticks(monitor, 3, start=T0 + STEP,
                     before=lambda now: probe_world(db, now)) == [[], [], []]
        assert mon_state(db)['targets'][f'asn:{MEGAFON}:hy2']['bad'] == 1

        db._init_hy2_auth_log()
        seed_allows(db, 1001, 40, now=T0 + 4 * STEP)
        changes = ticks(monitor, 1, start=T0 + 4 * STEP,
                        before=lambda now: probe_world(db, now))
        assert sorted(c.protocol for c in changes[0]) == ['hy2', 'hy2t']

    def test_stale_probes_keep_a_pending_bad_streak(self, monitor, db):
        """Same freeze for the probe pair when the pipeline goes stale
        mid-streak: 'frozen both ways' includes the bad side."""
        probe_world(db, T0, dark=('ws',))
        monitor.run_once(now=T0)
        assert mon_state(db)['targets']['global:ws']['bad'] == 1
        assert monitor.run_once(now=T0 + timedelta(minutes=60)) == []     # stale
        assert mon_state(db)['targets']['global:ws']['bad'] == 1
        probe_world(db, T0 + timedelta(minutes=70), dark=('ws',))
        changes = monitor.run_once(now=T0 + timedelta(minutes=70))
        assert [(c.action, c.key) for c in changes] == [('demote', 'global:ws')]

    def test_collect_signals_reports_the_unknown_rules(self, monitor, db):
        probe_world(db, T0)
        assert monitor.collect_signals(T0)['unknown'] == []
        with db._connect() as c:
            c.execute("DROP TABLE user_failure_reports"); c.commit()
        assert monitor.collect_signals(T0)['unknown'] == ['user_reports_asn']
        with db._connect() as c:
            c.execute("DROP TABLE dpi_metrics"); c.commit()
        assert monitor.collect_signals(T0)['unknown'] == ['reality_asn', 'user_reports_asn']

    def test_db_connect_failure_marks_every_rule_unknown(self, monitor, db, caplog):
        with patch.object(db, '_connect', side_effect=sqlite3.OperationalError('locked')), \
             caplog.at_level(logging.WARNING, logger='bot.services.dpi_monitor'):
            sig = monitor.collect_signals(T0)
        assert sig['unknown'] == sorted(dm.RULES)
        assert sig['probe']['stale'] is True
        assert 'all rules UNKNOWN' in caplog.text

    # -- pure core --------------------------------------------------------------

    def test_evaluate_freezes_unknown_rules_both_ways(self):
        state = dm.empty_state()
        state['auto']['asn'][MEGAFON] = {
            'hy2': {'since': T0.isoformat(), 'reason': 'udp_storm_asn', 'evidence': 'x'},
            'reality': {'since': T0.isoformat(), 'reason': 'reality_asn', 'evidence': 'x'},
        }
        state['targets'][f'asn:{MEGAFON}:hy2'] = {
            'bad': 0, 'good': 4, 'rule': 'udp_storm_asn',
            'last_change': (T0 - timedelta(hours=1)).isoformat()}
        state['targets']['asn:AS8359:hy2t'] = {'bad': 1, 'good': 0, 'rule': 'udp_storm_asn',
                                               'last_change': None}
        sig = TestHysteresis.sig()
        sig['unknown'] = ['udp_storm_asn']
        for i in range(1, 9):
            state, changes = DPIMonitor.evaluate(sig, state, T0 + i * STEP)
            assert all(c.reason != 'udp_storm_asn' for c in changes)
        # R4 targets untouched in every field…
        assert state['targets'][f'asn:{MEGAFON}:hy2']['good'] == 4
        assert state['targets']['asn:AS8359:hy2t']['bad'] == 1
        assert 'hy2' in state['auto']['asn'][MEGAFON]
        # …while the R3 demotion next to them restored on schedule.
        assert 'reality' not in state['auto']['asn'].get(MEGAFON, {})

    def test_evaluate_ignores_unknown_ids_it_does_not_know(self):
        state = dm.empty_state()
        sig = TestHysteresis.sig(dark=('ws',))
        sig['unknown'] = ['not_a_rule', 42, None]
        state, _ = DPIMonitor.evaluate(sig, state, T0)
        state, changes = DPIMonitor.evaluate(sig, state, T0 + STEP)
        assert [c.action for c in changes] == ['demote']


class TestPersistenceSafety:

    def test_unreadable_settings_skip_the_run_and_keep_every_demotion(self, monitor, db, bot, caplog):
        """A read that fails while writes still work is the wipe
        scenario: Database.get_setting returns None on a sqlite error,
        the monitor would take that for 'nothing demoted' and persist
        it. The run must be skipped instead."""
        ticks(monitor, 2, before=lambda now: probe_world(db, now, dark=('ws',)))
        assert 'ws' in auto(db)['global']
        auto_before, state_before = (db.get_setting('cascade_auto'),
                                     db.get_setting('dpi_monitor_state'))
        real_connect = db._connect
        probe_world(db, T0 + 2 * STEP, dark=('ws',))
        with patch.object(db, '_connect', side_effect=lambda: _ReadBrokenConn(real_connect())), \
             caplog.at_level(logging.WARNING, logger='bot.services.dpi_monitor'):
            assert db.get_setting('cascade_auto') is None        # the swallowed failure
            assert db.set_setting('probe_write', '1') is True    # writes still work
            changes = monitor.run_once(now=T0 + 2 * STEP)

        assert changes == []
        assert db.get_setting('cascade_auto') == auto_before
        assert db.get_setting('dpi_monitor_state') == state_before
        assert 'unreadable' in caplog.text and 'run skipped' in caplog.text
        assert len(admin_rows(db)) == 1 and bot.send_message.call_count == 1
        # Back to normal, the streak continues where it was.
        changes = ticks(monitor, 6, start=T0 + 3 * STEP,
                        before=lambda now: probe_world(db, now, healthy_ok=10))
        assert [c.action for run in changes for c in run] == ['restore']

    def test_a_failed_cascade_auto_write_aborts_before_audit_and_message(self, monitor, db, bot, caplog):
        probe_world(db, T0, dark=('ws',))
        monitor.run_once(now=T0)
        probe_world(db, T0 + STEP, dark=('ws',))
        with patch.object(db, 'set_setting', return_value=False):
            with pytest.raises(RuntimeError, match=r'cascade_auto\] failed — run aborted, nothing applied'):
                monitor.run_once(now=T0 + STEP)
        assert auto(db) == {'asn': {}, 'global': {}}
        assert admin_rows(db) == []
        bot.send_message.assert_not_called()
        # And the scheduler wrapper turns that into a log line, not a dead job.
        from bot.services.notifications import NotificationService
        svc = NotificationService(bot, db, monitor.config)
        with patch.object(db, 'set_setting', return_value=False), \
             caplog.at_level(logging.ERROR, logger='bot.services.notifications'):
            svc._dpi_monitor_sync()
        assert 'nothing applied' in caplog.text

    def test_a_failed_streak_write_after_a_saved_cascade_auto_still_announces(self, monitor, db, bot, caplog):
        """cascade_auto landed → users already get the new order; the
        change must be audited and posted even though the streak write
        failed. Loud (ERROR), not fatal."""
        probe_world(db, T0, dark=('ws',))
        monitor.run_once(now=T0)
        probe_world(db, T0 + STEP, dark=('ws',))
        real = db.set_setting

        def flaky(key, value):
            return False if key == 'dpi_monitor_state' else real(key, value)

        with patch.object(db, 'set_setting', side_effect=flaky), \
             caplog.at_level(logging.ERROR, logger='bot.services.dpi_monitor'):
            changes = monitor.run_once(now=T0 + STEP)
        assert [c.action for c in changes] == ['demote']
        assert 'ws' in auto(db)['global']
        assert [(a, t) for _i, a, t, _d in admin_rows(db)] == [('cascade_auto_demote', 'global:global:ws')]
        assert bot.send_message.call_count == 1
        assert 'dpi_monitor_state' in caplog.text and 'changes are live' in caplog.text

    def test_garbage_streak_values_do_not_kill_the_monitor(self, monitor, db):
        db.set_setting('dpi_monitor_state', json.dumps({
            'targets': {'global:ws': {'bad': 'x', 'good': None, 'rule': 'probe_dark'},
                        'nonsense': {'bad': 1}},
            'runs': 'many'}))
        changes = ticks(monitor, 2, before=lambda now: probe_world(db, now, dark=('ws',)))
        assert [c.action for c in changes[1]] == ['demote']
        assert mon_state(db)['runs'] == 2


# ---- settings + scheduler wiring -------------------------------------------

class _FakeScheduler:
    def __init__(self, *args, **kwargs):
        self.calls = []
        self.started = False

    def add_job(self, func, trigger=None, **kwargs):
        self.calls.append((func, trigger, kwargs))

    def start(self):
        self.started = True


class _FakeTrigger:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


def _start(svc):
    from bot.services import notifications as n
    with patch.object(n, 'SCHEDULER_AVAILABLE', True), \
         patch.object(n, 'BackgroundScheduler', _FakeScheduler, create=True), \
         patch.object(n, 'IntervalTrigger', _FakeTrigger, create=True), \
         patch.object(n, 'CronTrigger', _FakeTrigger, create=True), \
         patch('bot.services.alert_manager.AlertManager',
               side_effect=RuntimeError('not under test')):
        svc.start_scheduler()
    return svc.scheduler


class TestSettingsAndScheduling:

    def test_settings_defaults_and_env_parsing(self, monkeypatch):
        from bot.config.settings import Settings
        for k in ('DPI_MONITOR_ENABLED', 'DPI_MONITOR_INTERVAL_MIN'):
            monkeypatch.delenv(k, raising=False)
        s = Settings()
        assert s.DPI_MONITOR_ENABLED is True
        assert s.DPI_MONITOR_INTERVAL_MIN == 10

        monkeypatch.setenv('DPI_MONITOR_ENABLED', '0')
        monkeypatch.setenv('DPI_MONITOR_INTERVAL_MIN', '15')
        s = Settings()
        assert s.DPI_MONITOR_ENABLED is False
        assert s.DPI_MONITOR_INTERVAL_MIN == 15

        monkeypatch.setenv('DPI_MONITOR_ENABLED', '1')
        monkeypatch.setenv('DPI_MONITOR_INTERVAL_MIN', 'soon')
        s = Settings()
        assert s.DPI_MONITOR_ENABLED is True
        assert s.DPI_MONITOR_INTERVAL_MIN == 10

    def test_job_is_registered_with_the_settings_interval(self, db):
        from bot.services.notifications import NotificationService
        cfg = Mock()
        cfg.DPI_MONITOR_INTERVAL_MIN = 7
        svc = NotificationService(MagicMock(), db, cfg)
        sched = _start(svc)
        jobs = [c for c in sched.calls if c[2].get('id') == 'dpi_monitor']
        assert len(jobs) == 1
        func, trigger, kwargs = jobs[0]
        assert func == svc._dpi_monitor_sync
        assert trigger.kwargs == {'minutes': 7}
        assert kwargs.get('replace_existing') is True
        # The neighbours are still there.
        ids = [c[2].get('id') for c in sched.calls]
        assert 'dpi_collect' in ids and 'outbound_health_cleanup' in ids

    def test_job_interval_falls_back_to_ten_on_a_bare_mock_config(self, db):
        from bot.services.notifications import NotificationService
        svc = NotificationService(MagicMock(), db, Mock())
        sched = _start(svc)
        trigger = next(c[1] for c in sched.calls if c[2].get('id') == 'dpi_monitor')
        assert trigger.kwargs == {'minutes': 10}

    def test_sync_wrapper_builds_the_monitor_from_service_deps(self, db):
        from bot.services.notifications import NotificationService
        bot, cfg = MagicMock(), Mock()
        svc = NotificationService(bot, db, cfg)
        with patch('bot.services.dpi_monitor.DPIMonitor') as M:
            svc._dpi_monitor_sync()
        M.assert_called_once_with(db, cfg, bot)
        M.return_value.run_once.assert_called_once_with()

    def test_sync_wrapper_logs_and_swallows_exceptions(self, db, caplog):
        from bot.services.notifications import NotificationService
        svc = NotificationService(MagicMock(), db, Mock())
        with patch('bot.services.dpi_monitor.DPIMonitor.run_once',
                   side_effect=RuntimeError('disk on fire')), \
             caplog.at_level(logging.ERROR, logger='bot.services.notifications'):
            svc._dpi_monitor_sync()          # must not raise
        assert 'dpi_monitor tick failed' in caplog.text
        assert 'disk on fire' in caplog.text
