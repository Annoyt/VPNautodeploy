"""outbound_health retention job against a REAL sqlite bot.db.

Nothing pruned the probe table from 2026-06-21 to 2026-09-04 (326k rows,
+3.8k a day, no retention job at all). NotificationService.
_cleanup_outbound_health_sync is the daily job that closes that gap. It
differs from the other _cleanup_*_sync jobs in one way that matters: it
deletes in BATCHES with a commit between them, because the first prod
run drops ~250k rows on a db shared with HealthChecker inserts, the 60 s
alert tick and /sub reads.

Real ``Database`` on a temp file, so the real schema (and the
idx_outbound_health_ts_tag index the batch subquery leans on) exists. A
mocked connection cannot prove that a
``DELETE … WHERE id IN (SELECT … LIMIT ?)`` loop terminates, that each
batch is visible to other connections before the next one starts, or
that the row sitting exactly on the cutoff survives.
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest

from bot.core.database import Database
from bot.services.notifications import NotificationService

LOGGER = 'bot.services.notifications'

# A fixed "now" so the boundary row can be placed EXACTLY on the cutoff.
# Whole seconds on purpose: isoformat() then carries no fraction, which
# is the shape HealthChecker's utcnow().isoformat() also produces once
# in a million rows — the string comparison must hold for both shapes.
NOW = datetime(2026, 9, 5, 12, 0, 0)
CUTOFF = NOW - timedelta(days=NotificationService.OUTBOUND_HEALTH_RETENTION_DAYS)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def svc(db):
    service = NotificationService(MagicMock(), db, Mock())
    # The pause exists for prod fairness, not for correctness — zero it
    # so a 25-row test does not sleep. Tests that care about the sleep
    # patch time.sleep and count calls instead.
    service.OUTBOUND_HEALTH_CLEANUP_PAUSE_S = 0
    return service


def _seed(db, stamps, tag='reality'):
    """Insert one probe row per timestamp, shaped like HealthChecker does."""
    with db._connect() as conn:
        for ts in stamps:
            conn.execute(
                "INSERT INTO outbound_health (outbound_tag, target_domain,"
                " status, latency_ms, error_msg, ts) VALUES (?, ?, ?, ?, ?, ?)",
                (tag, 'vk.com', 'ok', 120, None,
                 ts.isoformat() if isinstance(ts, datetime) else ts),
            )
        conn.commit()


def _remaining_ts(db):
    with db._connect() as conn:
        return sorted(
            r[0] for r in conn.execute("SELECT ts FROM outbound_health")
        )


def _count(db):
    with db._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM outbound_health").fetchone()[0]


class TestCutoff:

    def test_real_schema_has_the_index_the_batch_subquery_relies_on(self, db):
        with db._connect() as conn:
            names = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' "
                "AND tbl_name = 'outbound_health'")}
        assert 'idx_outbound_health_ts_tag' in names

    def test_old_rows_go_new_rows_stay(self, svc, db):
        old = [CUTOFF - timedelta(days=75), CUTOFF - timedelta(days=10),
               CUTOFF - timedelta(days=1), CUTOFF - timedelta(seconds=1)]
        new = [CUTOFF + timedelta(seconds=1), NOW - timedelta(hours=3),
               NOW - timedelta(minutes=15), NOW]
        _seed(db, old, tag='reality')
        _seed(db, old, tag='hy2')          # retention is per row, not per tag
        _seed(db, new, tag='reality')
        _seed(db, new, tag='ws')

        svc._cleanup_outbound_health_sync(now=NOW)

        assert _remaining_ts(db) == sorted(t.isoformat() for t in new * 2)

    def test_row_exactly_at_cutoff_is_kept(self, svc, db):
        """``ts < cutoff`` — the row AT the cutoff is inside the window."""
        just_before = CUTOFF - timedelta(microseconds=1)
        just_after = CUTOFF + timedelta(microseconds=1)
        _seed(db, [just_before, CUTOFF, just_after])

        svc._cleanup_outbound_health_sync(now=NOW)

        assert _remaining_ts(db) == [CUTOFF.isoformat(), just_after.isoformat()]

    def test_scheduler_call_without_now_uses_wall_clock(self, svc, db):
        """APScheduler calls the job with no arguments."""
        wall = datetime.utcnow()
        days = NotificationService.OUTBOUND_HEALTH_RETENTION_DAYS
        _seed(db, [wall - timedelta(days=days + 1), wall - timedelta(days=1)])

        svc._cleanup_outbound_health_sync()

        assert _remaining_ts(db) == [(wall - timedelta(days=1)).isoformat()]

    def test_logs_dropped_and_kept_counts(self, svc, db, caplog):
        _seed(db, [CUTOFF - timedelta(days=2)] * 3 + [NOW] * 2)
        caplog.set_level(logging.INFO, logger=LOGGER)

        svc._cleanup_outbound_health_sync(now=NOW)

        assert 'outbound_health cleanup: dropped 3 rows' in caplog.text
        assert '2 rows kept' in caplog.text

    def test_empty_table_still_logs_a_heartbeat(self, svc, db, caplog):
        """A run that drops nothing is still a daily size line, not silence."""
        caplog.set_level(logging.INFO, logger=LOGGER)

        svc._cleanup_outbound_health_sync(now=NOW)

        assert 'dropped 0 rows' in caplog.text
        assert '0 rows kept' in caplog.text
        assert _count(db) == 0


class TestBatching:

    def test_more_than_one_batch_terminates_and_deletes_everything_eligible(
            self, svc, db, caplog):
        svc.OUTBOUND_HEALTH_CLEANUP_BATCH = 10
        svc.OUTBOUND_HEALTH_CLEANUP_PAUSE_S = 0.001
        _seed(db, [CUTOFF - timedelta(hours=h) for h in range(1, 26)])   # 25 old
        keep = [NOW - timedelta(minutes=m) for m in range(5)]             # 5 new
        _seed(db, keep)
        caplog.set_level(logging.INFO, logger=LOGGER)

        with patch('time.sleep') as sleep:
            svc._cleanup_outbound_health_sync(now=NOW)

        assert _remaining_ts(db) == sorted(t.isoformat() for t in keep)
        # 10 + 10 + 5, then an empty batch ends the loop without a pause.
        assert sleep.call_count == 3
        sleep.assert_called_with(0.001)
        assert 'dropped 25 rows' in caplog.text
        assert 'in 3 batch(es)' in caplog.text
        assert '5 rows kept' in caplog.text

    def test_each_batch_is_committed_before_the_next_starts(self, svc, db):
        """The whole point of batching: another connection (the alert
        tick, a /sub read, HealthChecker's insert) must see each batch
        land — i.e. the write lock is released between batches, not held
        for one 250k-row transaction."""
        svc.OUTBOUND_HEALTH_CLEANUP_BATCH = 10
        _seed(db, [CUTOFF - timedelta(hours=h) for h in range(1, 26)])   # 25 old
        _seed(db, [NOW] * 5)                                              # 5 new
        seen = []

        def observe(_pause):
            # A separate connection, exactly like a concurrent reader.
            with sqlite3.connect(db.db_path) as other:
                seen.append(other.execute(
                    "SELECT COUNT(*) FROM outbound_health").fetchone()[0])

        with patch('time.sleep', side_effect=observe):
            svc._cleanup_outbound_health_sync(now=NOW)

        assert seen == [20, 10, 5]

    def test_single_batch_when_everything_fits(self, svc, db, caplog):
        _seed(db, [CUTOFF - timedelta(days=1)] * 7)
        caplog.set_level(logging.INFO, logger=LOGGER)

        with patch('time.sleep') as sleep:
            svc._cleanup_outbound_health_sync(now=NOW)

        assert _count(db) == 0
        assert sleep.call_count == 1
        assert 'in 1 batch(es)' in caplog.text


class TestFailure:

    def test_exception_is_logged_with_traceback_and_swallowed(
            self, svc, db, caplog):
        with db._connect() as conn:
            conn.execute("DROP TABLE outbound_health")
            conn.commit()
        caplog.set_level(logging.INFO, logger=LOGGER)

        svc._cleanup_outbound_health_sync(now=NOW)     # must not raise

        failed = [r for r in caplog.records
                  if 'outbound_health cleanup failed' in r.getMessage()]
        assert len(failed) == 1
        assert failed[0].levelno == logging.ERROR
        assert failed[0].exc_info is not None        # logger.exception, not .error
        assert 'no such table' in caplog.text
        # And no success line pretending the run was fine.
        assert 'rows kept' not in caplog.text

    def test_batches_finished_before_a_failure_stay_deleted(
            self, svc, db, caplog):
        """Per-batch commits mean a crash mid-run is progress, not a
        rollback — tomorrow's run continues from where this one stopped."""
        svc.OUTBOUND_HEALTH_CLEANUP_BATCH = 10
        _seed(db, [CUTOFF - timedelta(hours=h) for h in range(1, 26)])   # 25 old
        caplog.set_level(logging.INFO, logger=LOGGER)

        with patch('time.sleep', side_effect=[None, RuntimeError('disk on fire')]):
            svc._cleanup_outbound_health_sync(now=NOW)   # must not raise

        assert _count(db) == 5           # two committed batches gone, 5 left
        assert 'outbound_health cleanup failed after 20 rows' in caplog.text
        assert 'disk on fire' in caplog.text


class _FakeScheduler:
    """Records add_job calls; never starts a thread."""

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


class TestRegistration:

    def test_daily_job_is_registered_in_start_scheduler(self, svc):
        # APScheduler is optional at import time (SCHEDULER_AVAILABLE) and
        # absent on some dev boxes, so the scheduler and both trigger
        # classes are replaced (create=True) rather than relied upon; the
        # AlertManager wiring is stubbed out because it is not what this
        # test pins and start_scheduler already tolerates its failure.
        with patch('bot.services.notifications.SCHEDULER_AVAILABLE', True), \
             patch('bot.services.notifications.BackgroundScheduler',
                   _FakeScheduler, create=True), \
             patch('bot.services.notifications.IntervalTrigger',
                   _FakeTrigger, create=True), \
             patch('bot.services.notifications.CronTrigger',
                   _FakeTrigger, create=True), \
             patch('bot.services.alert_manager.AlertManager',
                   side_effect=RuntimeError('not under test')):
            svc.start_scheduler()

        sched = svc.scheduler
        assert isinstance(sched, _FakeScheduler) and sched.started
        ids = [kw.get('id') for _, _, kw in sched.calls]
        assert 'dpi_metrics_cleanup' in ids          # the precedent is intact
        assert ids.count('outbound_health_cleanup') == 1

        func, trigger, kwargs = next(
            c for c in sched.calls if c[2].get('id') == 'outbound_health_cleanup')
        assert func == svc._cleanup_outbound_health_sync
        assert isinstance(trigger, _FakeTrigger)
        assert trigger.kwargs == {'hours': 24}
        assert kwargs.get('replace_existing') is True
