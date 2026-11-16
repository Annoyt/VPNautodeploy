"""What an alert does when a check could not look — review of package B.

* A check that had no verdict for a key (a failed read, stale probes, every
  protocol dark at once, a thin window, missing exit reports, the hours after
  a restart) leaves the key as it was: no «Восстановилось», no fresh page.
* The inbound-drop baseline skips the blocks of the key's own incident, so a
  long outage stays open and a second drop soon after a recovery is still
  seen; eight hours of empty exit reports are the reporter's problem.
* A protocol demoted for everyone (by hand or by DPIMonitor) gives no
  inbound-drop verdict while demoted and for a tail after (as long as the
  demotion, at most a day), and its demoted days stay out of the baseline;
  an alert already open stays open without repeats (2026-11-15: ws).
* The repeat cooldown and the ack are cleared on close only for the families
  whose recovery is posted.
* An open key loaded from alert_state that no check reports heals.
* The reporter's own keys start no agent turn; a batch of two families is
  labelled by its full keys.

Level 2: real sqlite and the real checks; Telegram and the agent stubbed.
"""

import json
import sqlite3
import time
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from bot.core.database import Database
from bot.services import alert_manager as am
from bot.services import cascade_manual, demotion_spans
from bot.services import probe_rules as pr
from bot.services.alert_manager import Alert, AlertManager, build_default_checks

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

TARGETS = list(pr.PROBE_TARGETS)
STEP = timedelta(minutes=5)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def config(tmp_path):
    cfg = Mock()
    cfg.DB_PATH = str(tmp_path / 'bot.db')
    cfg.TOPIC_AI = 55
    cfg.FORUM_GROUP_ID = -100123
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.ALERT_TG_MIN_SEVERITY = 'critical'
    cfg.AGENT_BACKEND = 'hermes'
    cfg.HERMES_URL = ''
    return cfg


def named_check(config, name):
    return next(c for c in build_default_checks(config, Mock()) if c.__name__ == name)


def mgr(db, config, *checks):
    m = AlertManager(Mock(), config, db=db)
    for c in checks:
        m.register(c)
    return m


def texts(m):
    return [c.kwargs['text'] for c in m.bot.send_message.call_args_list]


def recovered(m):
    return [t for t in texts(m) if 'Восстановилось' in t]


def ticks(m, n=am.RECOVERY_TICKS + 1):
    for _ in range(n):
        m.run_once()


class Script:
    """A check whose answer the test sets per tick."""

    def __init__(self, *, multi=None, raises=False):
        self.multi, self.raises, self.out = multi, raises, None
        self.__name__ = f'script_{multi or "single"}'

    def __call__(self):
        if self.raises:
            raise RuntimeError('boom')
        if self.multi is not None:
            return (self.multi, list(self.out or []))
        return self.out


# ------------------------------------------------------------- probes -------

def seed_window(db, tag, ok_per_run, *, runs=3, alive_fail=True, minutes_ago=5):
    now = datetime.utcnow()
    with db._connect() as conn:
        conn.execute("DELETE FROM outbound_health WHERE outbound_tag = ?", (tag,))
        for run in range(runs):
            ts = (now - timedelta(minutes=minutes_ago + run * 15)).isoformat()
            for i, domain in enumerate(TARGETS):
                ok = i < ok_per_run
                conn.execute(
                    "INSERT INTO outbound_health (outbound_tag, target_domain, status,"
                    " latency_ms, error_msg, ts) VALUES (?,?,?,?,?,?)",
                    (tag, domain, 'ok' if ok else 'error',
                     120 if ok else (900 if alive_fail else None),
                     None if ok else 'x', ts))
        conn.commit()


def open_reality(db, config):
    for tag in ('hy2', 'ws', 'stls'):
        seed_window(db, tag, 8)
    seed_window(db, 'reality', 0, alive_fail=False)
    m = mgr(db, config, named_check(config, 'check_protocol_probe_down'))
    m.run_once()
    m.run_once()                                       # min_cycles = 2
    assert m._state['protocol_down:reality'].open
    return m


class TestProbesWithoutAVerdict:

    def test_stale_probes_are_not_a_recovery(self, db, config):
        m = open_reality(db, config)
        for tag in ('reality', 'hy2', 'ws', 'stls'):
            seed_window(db, tag, 8, minutes_ago=61)    # the job stopped an hour ago
        ticks(m)
        assert recovered(m) == []
        assert m._state['protocol_down:reality'].open
        assert m._state['protocol_down:probe_pipeline'].open

    def test_every_protocol_dark_is_not_a_recovery(self, db, config):
        m = open_reality(db, config)
        for tag in ('hy2', 'ws', 'stls'):
            seed_window(db, tag, 0, alive_fail=False)
        ticks(m)
        assert recovered(m) == []
        assert m._state['protocol_down:reality'].open
        assert m._state['protocol_down:all'].open

    def test_a_failed_read_is_not_a_recovery(self, db, config, tmp_path):
        m = open_reality(db, config)
        config.DB_PATH = str(tmp_path / 'no-such-dir' / 'bot.db')
        ticks(m)
        assert recovered(m) == []
        assert m._state['protocol_down:reality'].open

    def test_a_thin_window_is_not_a_recovery(self, db, config):
        m = open_reality(db, config)
        seed_window(db, 'reality', 8, runs=1)          # 10 rows < 15
        ticks(m)
        assert recovered(m) == []
        assert m._state['protocol_down:reality'].open

    def test_a_real_recovery_is_still_posted(self, db, config):
        m = open_reality(db, config)
        seed_window(db, 'reality', 8)
        ticks(m)
        assert len(recovered(m)) == 1 and 'reality' in recovered(m)[0]
        assert not m._state['protocol_down:reality'].open

    def test_a_switched_off_protocol_heals(self, db, config):
        m = open_reality(db, config)
        db.set_setting('cascade_protocol_order', __import__('json').dumps(
            [{'name': n, 'enabled': n != 'reality'}
             for n in ('stls', 'ws', 'hy2', 'reality', 'hy2t')]))
        ticks(m)
        assert not m._state['protocol_down:reality'].open


# ------------------------------------------------------- inbound drop -------

def seed_series(db, value_at, *, days, tag='cf-ws', beats=True, beat_rows=2):
    """Exit reports every 5 min for ``days`` back; ``value_at(hours_ago)`` =
    accepted connections on ``tag`` in that snapshot (240 a block at 10)."""
    now = datetime.utcnow()
    t = now - timedelta(days=days)
    rows, hb = [], []
    while t < now - timedelta(minutes=1):
        ago_h = (now - t).total_seconds() / 3600
        rows.append((t.isoformat(), '*TUNNEL*', None, 'entry-forward', tag, value_at(ago_h)))
        if beats:
            hb.append((t.isoformat(), beat_rows))
        t += STEP
    with db._connect() as conn:
        conn.executemany(
            "INSERT INTO dpi_metrics (snapshot_at, country, asn, as_org, inbound_tag, "
            "conn_count) VALUES (?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO exit_reports (ts, rows) VALUES (?,?)", hb)
        conn.commit()


def incident(db, key, *, opened_h_ago, closed_h_ago=None):
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO alert_state (key, last_fired_ts, open, notify, title, opened_ts, "
            "closed_ts) VALUES (?, ?, ?, 1, 't', ?, ?)",
            (key, now - opened_h_ago * 3600, 0 if closed_h_ago is not None else 1,
             now - opened_h_ago * 3600,
             0 if closed_h_ago is None else now - closed_h_ago * 3600))
        conn.commit()


def drop_alerts(config):
    result = named_check(config, 'check_inbound_drop')()
    return {a.key: a for a in result[1]}, result[2]


class TestInboundDropBaseline:

    def test_a_four_day_outage_stays_open(self, db, config):
        seed_series(db, lambda h: 0 if h < 100 else 10, days=12)
        # without the incident on record the outage's own zeros are "usual"
        assert 'inbound_drop:ws' not in drop_alerts(config)[0]
        incident(db, 'inbound_drop:ws', opened_h_ago=92)
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:ws' in alerts and 'inbound_drop:ws' in judged
        assert '0 из ~240' in alerts['inbound_drop:ws'].detail

    def test_a_second_drop_soon_after_a_recovery_is_seen(self, db, config):
        # out from 132 h to 36 h ago, back for a day, out again for 8.5 h
        seed_series(db, lambda h: 0 if (h < 8.5 or 36 <= h < 132) else 10, days=12)
        assert 'inbound_drop:ws' not in drop_alerts(config)[0]
        incident(db, 'inbound_drop:ws', opened_h_ago=124, closed_h_ago=36)
        assert 'inbound_drop:ws' in drop_alerts(config)[0]

    def test_too_little_history_is_no_verdict(self, db, config):
        seed_series(db, lambda h: 0 if h < 8.5 else 10, days=3)
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:ws' not in alerts and 'inbound_drop:ws' not in judged

    def test_unreported_baseline_days_are_skipped_not_zero(self, db, config):
        # days 2..5 back had no reports at all: as zeros they would make the
        # median 0; skipped, the days before them stand in
        seed_series(db, lambda h: 0 if h < 8.5 else 10, days=12)
        with db._connect() as conn:
            lo = (datetime.utcnow() - timedelta(hours=130)).isoformat()
            hi = (datetime.utcnow() - timedelta(hours=40)).isoformat()
            conn.execute("DELETE FROM dpi_metrics WHERE snapshot_at BETWEEN ? AND ?", (lo, hi))
            conn.execute("DELETE FROM exit_reports WHERE ts BETWEEN ? AND ?", (lo, hi))
            conn.commit()
        assert 'inbound_drop:ws' in drop_alerts(config)[0]


class TestInboundDropAcrossARestart:

    def test_an_open_hy2_drop_survives_a_restart(self, db, config):
        seed_series(db, lambda h: 0 if h < 8.5 else 10, days=7.5, tag='hy2-8400')
        first = named_check(config, 'check_inbound_drop')
        first.cache.update(started=time.time() - 9 * 3600, at=0.0)
        m0 = mgr(db, config, first)
        m0.run_once()
        assert m0._state['inbound_drop:hy2'].open
        assert any('Авторизации hy2 упали' in t for t in texts(m0))
        # the deploy: a new process, a fresh check (hy2 has no verdict for 8 h)
        m1 = mgr(db, config, named_check(config, 'check_inbound_drop'))
        for _ in range(am.RECOVERY_TICKS + 1):
            m1._checks[0].cache['at'] = 0.0
            m1.run_once()
        assert recovered(m1) == []
        assert m1._state['inbound_drop:hy2'].open


# ------------------------------------------- inbound drop while demoted -----

def hours_ago(h):
    return datetime.utcnow() - timedelta(hours=h)


def iso(ts):
    return ts.isoformat(timespec='seconds')


def demote_by_hand(db, proto='ws', *, since_h=48, ttl_h=72, asn=None):
    cascade_manual.demote(db, proto, asn, timedelta(hours=ttl_h), 'admin:1',
                          now=hours_ago(since_h))


def demote_by_monitor(db, proto='ws', *, since_h=48):
    db.set_setting('cascade_auto', json.dumps({
        'global': {proto: {'since': iso(hours_ago(since_h)), 'reason': 'probe_bulk',
                           'evidence': 'bulk 3/3 partial'}},
        'asn': {}}))


def set_journal(db, **spans):
    """``ws=[(started_h_ago, ended_h_ago | None), …]``."""
    db.set_setting(demotion_spans.SETTING_KEY, json.dumps({
        proto: [[iso(hours_ago(a)), None if b is None else iso(hours_ago(b))] for a, b in ss]
        for proto, ss in spans.items()}))


def journal(db):
    raw = db.get_setting(demotion_spans.SETTING_KEY)
    return json.loads(raw) if raw else None


def quiet_now(h):
    """A normal week (240 a block), then 8.5 quiet hours."""
    return 0 if h < 8.5 else 10


class TestInboundDropWhileDemoted:
    """2026-11-15: ws, demoted by hand on 11-13 after the Cloudflare freeze,
    paged «Подключения ws на exit упали» critical every 2 h: its users left
    it because of the demotion."""

    def test_a_hand_demotion_gives_no_verdict(self, db, config):
        seed_series(db, quiet_now, days=7.5)
        assert 'inbound_drop:ws' in drop_alerts(config)[0]      # pages without it
        demote_by_hand(db)
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:ws' not in alerts and 'inbound_drop:ws' not in judged

    def test_a_monitor_demotion_too(self, db, config):
        seed_series(db, quiet_now, days=7.5)
        demote_by_monitor(db)
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:ws' not in alerts and 'inbound_drop:ws' not in judged

    def test_the_other_inbounds_keep_their_verdict(self, db, config):
        seed_series(db, quiet_now, days=7.5)
        seed_series(db, quiet_now, days=7.5, tag='reality', beats=False)
        demote_by_hand(db, 'ws')
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:reality' in alerts and 'inbound_drop:ws' not in judged

    def test_a_demotion_for_one_network_still_pages(self, db, config):
        # an inbound counts every network's users: one network leaving does
        # not take it below a fifth, so a drop this deep is not explained
        seed_series(db, quiet_now, days=7.5)
        demote_by_hand(db, asn='AS31133')
        assert 'inbound_drop:ws' in drop_alerts(config)[0]

    def test_an_expired_hand_demotion_is_none(self, db, config):
        seed_series(db, quiet_now, days=7.5)
        demote_by_hand(db, since_h=80, ttl_h=24)               # ended 56 h ago, never seen
        assert 'inbound_drop:ws' in drop_alerts(config)[0]

    def test_the_journal_follows_the_stores(self, db, config):
        seed_series(db, quiet_now, days=7.5)
        demote_by_hand(db, since_h=48)
        check = named_check(config, 'check_inbound_drop')
        check()
        [[start, end]] = journal(db)['ws']
        assert end is None
        assert abs(datetime.fromisoformat(start) - hours_ago(48)) < timedelta(minutes=1)
        cascade_manual.restore(db, 'ws', None)
        check.cache['at'] = 0.0
        judged = check()[2]
        [[start2, end2]] = journal(db)['ws']
        assert start2 == start
        assert abs(datetime.fromisoformat(end2) - datetime.utcnow()) < timedelta(minutes=1)
        # ended a moment ago: its tail still covers the window
        assert 'inbound_drop:ws' not in judged

    def test_the_tail_lasts_a_day_after_a_long_demotion(self, db, config):
        seed_series(db, quiet_now, days=12)
        set_journal(db, ws=[(92, 20)])          # three days, ended 20 h ago
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:ws' not in judged
        set_journal(db, ws=[(105, 33)])         # its tail ended 9 h ago
        assert 'inbound_drop:ws' in drop_alerts(config)[0]

    def test_a_short_demotion_has_a_short_tail(self, db, config):
        seed_series(db, quiet_now, days=7.5)
        set_journal(db, ws=[(11, 10)])          # one hour: its tail ended 9 h ago
        assert 'inbound_drop:ws' in drop_alerts(config)[0]
        set_journal(db, ws=[(8, 7)])            # its tail reaches into the window
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:ws' not in judged

    def test_demoted_days_stay_out_of_the_baseline(self, db, config):
        # two normal weeks, but users left ws during a six-day demotion that
        # ended two days ago; now 8.5 quiet hours. With the demoted days in
        # it the "usual" would be 24 a block, below the 50 that proves anything.
        seed_series(db, lambda h: 0 if h < 8.5 else (1 if 48 <= h < 192 else 10), days=14.5)
        set_journal(db, ws=[(192, 48)])
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:ws' in alerts
        assert '0 из ~240' in alerts['inbound_drop:ws'].detail

    def test_an_open_alert_stays_open_and_quiet(self, db, config):
        seed_series(db, quiet_now, days=7.5)
        check = named_check(config, 'check_inbound_drop')
        m = mgr(db, config, check)
        m.run_once()
        assert m._state['inbound_drop:ws'].open
        # control: past the repeat cooldown the drop pages again
        sent = len(texts(m))
        check.cache['at'] = 0.0
        m._state['inbound_drop:ws'].last_fired_ts = 0.0
        m.run_once()
        assert len(texts(m)) > sent
        demote_by_hand(db, since_h=1)
        sent = len(texts(m))
        for _ in range(am.RECOVERY_TICKS + 1):
            check.cache['at'] = 0.0
            m._state['inbound_drop:ws'].last_fired_ts = 0.0
            m.run_once()
        assert len(texts(m)) == sent                           # no repeat, no recovery
        assert m._state['inbound_drop:ws'].open

    def test_a_failed_journal_write_costs_only_the_sample(self, db, config, monkeypatch):
        seed_series(db, quiet_now, days=7.5)
        demote_by_hand(db)

        def locked(conn, spans):
            raise sqlite3.OperationalError('database is locked')

        monkeypatch.setattr(demotion_spans, 'save', locked)
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:ws' not in judged                 # read from the store
        assert 'inbound_drop:stls' in judged
        assert journal(db) is None

    def test_a_broken_journal_starts_over(self, db, config):
        seed_series(db, quiet_now, days=7.5)
        db.set_setting(demotion_spans.SETTING_KEY, '{not json')
        demote_by_hand(db)
        assert 'inbound_drop:ws' not in drop_alerts(config)[1]
        assert journal(db)['ws'][0][1] is None

    def test_protocols_without_an_inbound_are_not_journaled(self, db, config):
        seed_series(db, quiet_now, days=7.5)
        demote_by_hand(db, 'hy2t')
        demote_by_monitor(db, 'de')
        assert 'inbound_drop:ws' in drop_alerts(config)[0]
        assert journal(db) is None


class TestReporterKeys:

    def test_never_a_report_is_nothing_to_say(self, db, config):
        alerts, judged = drop_alerts(config)
        assert 'inbound_drop:no_reports' not in alerts
        assert 'inbound_drop:no_reports' in judged

    def test_empty_reports_are_the_reporters_problem(self, db, config):
        seed_series(db, lambda h: 10, days=7.5)
        with db._connect() as conn:                       # 8.5 h of empty reports
            cut = (datetime.utcnow() - timedelta(hours=8.5)).isoformat()
            conn.execute("DELETE FROM dpi_metrics WHERE snapshot_at >= ?", (cut,))
            conn.execute("UPDATE exit_reports SET rows = 0 WHERE ts >= ?", (cut,))
            conn.commit()
        alerts, judged = drop_alerts(config)
        assert alerts['inbound_drop:empty_reports'].severity == 'warn'
        assert 'inbound_drop:ws' not in alerts and 'inbound_drop:ws' not in judged

    def test_the_reporter_keys_start_no_agent_turn(self, db, config):
        config.HERMES_URL = 'http://hermes:4097'
        client = Mock()
        client.ask.return_value = ('ИТОГ', 1)
        with patch('bot.services.agent_factory.build_agent_client', return_value=client):
            check = Script(multi='inbound_drop:')
            check.out = [Alert(key=k, severity='critical', min_cycles=1, title=k)
                         for k in am.AGENT_SKIP_KEYS]
            m = mgr(db, config, check)
            m.run_once()
            t = m._last_agent_thread
            if t is not None:
                t.join(timeout=10)
        assert client.ask.call_count == 0


# --------------------------------------------------- cooldown and ack -------

def disk(**kw):
    return Alert(key='disk', severity='critical', min_cycles=1, title='Диск 96%', **kw)


class TestCooldownScope:

    def test_a_flapping_disk_does_not_page_on_every_flap(self, db, config):
        check = Script()
        m = mgr(db, config, check)
        for _ in range(3):
            check.out = disk()
            m.run_once()
            check.out = None
            ticks(m)                                   # closes, quietly
        assert len([t for t in texts(m) if 'Диск' in t]) == 1

    def test_a_flapping_dpi_bucket_does_not_page_on_every_flap(self, db, config):
        config.ALERT_TG_MIN_SEVERITY = 'warn'
        check = Script(multi='dpi_rst:')
        m = mgr(db, config, check)
        for _ in range(3):
            check.out = [Alert(key='dpi_rst:global', severity='warn', min_cycles=1,
                               title='RST spike')]
            m.run_once()
            check.out = []
            ticks(m)
        assert m._state['dpi_rst:global'].last_fired_ts > 0
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM alert_history WHERE key = "
                                "'dpi_rst:global'").fetchone()[0] == 1

    def test_an_ack_ends_with_the_recovery(self, db, config):
        check = Script(multi='protocol_down:')
        proto = Alert(key='protocol_down:hy2', severity='critical', min_cycles=1,
                      title='Протокол hy2 мёртв')
        check.out = [proto]
        m = mgr(db, config, check)
        m.run_once()
        m.ack('protocol_down:hy2', by='admin')
        check.out = []
        ticks(m)
        assert len(recovered(m)) == 1
        check.out = [proto]
        m.run_once()                                   # within the 6-h ack
        assert len([t for t in texts(m) if 'мёртв' in t and 'Восстановилось' not in t]) == 2


def test_the_incident_times_are_stored(db, config):
    check = Script(multi='inbound_drop:')
    check.out = [Alert(key='inbound_drop:ws', severity='critical', min_cycles=1, title='t')]
    m = mgr(db, config, check)
    before = time.time()
    m.run_once()
    check.out = []
    ticks(m)
    with db._connect() as conn:
        opened, closed, is_open = conn.execute(
            "SELECT opened_ts, closed_ts, open FROM alert_state "
            "WHERE key = 'inbound_drop:ws'").fetchone()
    assert before <= opened <= closed and is_open == 0
    # a fire of a key that is already open keeps the incident's start
    check.out = [Alert(key='inbound_drop:ws', severity='critical', min_cycles=1, title='t')]
    m.run_once()
    first = m._state['inbound_drop:ws'].opened_ts
    m._state['inbound_drop:ws'].last_fired_ts = 0.0     # past the cooldown
    m.run_once()
    assert m._state['inbound_drop:ws'].opened_ts == first


# ------------------------------------------------------------ orphans -------

class TestOrphans:

    def _loaded(self, db, config, *checks):
        with db._connect() as conn:
            conn.execute("INSERT INTO alert_state (key, last_fired_ts, open, notify, title) "
                         "VALUES ('disk', ?, 1, 1, 'Диск 96%')", (time.time() - 600,))
            conn.commit()
        return mgr(db, config, *checks)

    def test_a_loaded_key_nobody_reports_heals(self, db, config):
        m = self._loaded(db, config, Script())
        ticks(m)
        assert not m._state['disk'].open
        assert recovered(m) == []                      # not a posted family

    def test_not_while_a_check_raises(self, db, config):
        m = self._loaded(db, config, Script(), Script(raises=True))
        ticks(m)
        assert m._state['disk'].open

    def test_a_key_its_check_reports_again_stays(self, db, config):
        check = Script()
        check.out = disk()
        m = self._loaded(db, config, check)
        ticks(m)
        assert m._state['disk'].open


# ------------------------------------------------------- batch label --------

def test_a_batch_of_two_families_names_both_keys(db, config):
    config.HERMES_URL = 'http://hermes:4097'
    client = Mock()
    client.ask.return_value = ('ИТОГ', 1)
    with patch('bot.services.agent_factory.build_agent_client', return_value=client):
        probes = Script(multi='protocol_down:')
        probes.out = [Alert(key='protocol_down:hy2', severity='critical', min_cycles=1,
                            title='Протокол hy2 мёртв')]
        drops = Script(multi='inbound_drop:')
        drops.out = [Alert(key='inbound_drop:ws', severity='critical', min_cycles=1,
                           title='Подключения ws упали')]
        m = mgr(db, config, probes, drops)
        m.run_once()
        m._last_agent_thread.join(timeout=10)
    session = client.ask.call_args.args[0]
    assert session.startswith('proto-alert:inbound_drop:ws+protocol_down:hy2:')
