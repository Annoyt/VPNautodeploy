"""client_probe retention job against a REAL sqlite bot.db.

client_probe holds the heartbeats GET /probe writes from the FlClash
profile's provider health checks (IMPROVEMENT_PLAN E4).
NotificationService._cleanup_client_probe_sync keeps it to 30 days, in
batches with a commit between them — the outbound_health job's contract
(tests/unit/test_outbound_health_retention.py), plus two details of this
table: no id column (batches go by rowid) and ``ts`` written by sqlite's
CURRENT_TIMESTAMP ('YYYY-MM-DD HH:MM:SS', a space where isoformat has
'T'), which the cutoff string must match.

Real ``Database`` on a temp file, so the real schema exists — a mocked
connection cannot show that the rowid batch loop terminates, that each
batch is visible to other connections before the next, or where exactly
the string cutoff falls.
"""

import logging
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock, Mock, patch

import pytest

from bot.core.database import Database
from bot.services.notifications import NotificationService

LOGGER = 'bot.services.notifications'

NOW = datetime(2026, 10, 8, 12, 0, 0)
# Literal 30 on purpose — not the class constant: the threshold itself
# is what these tests pin.
CUTOFF = NOW - timedelta(days=30)


def _ts(dt):
    """The shape sqlite's CURRENT_TIMESTAMP writes (the /probe insert)."""
    return dt.strftime('%Y-%m-%d %H:%M:%S')


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def svc(db):
    service = NotificationService(MagicMock(), db, Mock())
    service.CLIENT_PROBE_CLEANUP_PAUSE_S = 0     # fairness, not correctness
    return service


def _seed(db, stamps, grp='emergency'):
    with db._connect() as conn:
        for dt in stamps:
            conn.execute(
                "INSERT INTO client_probe (chat_id, grp, ts, src_ip) VALUES (?, ?, ?, ?)",
                ('1', grp, _ts(dt) if isinstance(dt, datetime) else dt, '192.0.2.1'),
            )
        conn.commit()


def _remaining(db):
    with db._connect() as conn:
        return sorted(r[0] for r in conn.execute("SELECT ts FROM client_probe"))


def _count(db):
    with db._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM client_probe").fetchone()[0]


class TestCutoff:

    def test_retention_is_thirty_days(self, svc, db):
        assert NotificationService.CLIENT_PROBE_RETENTION_DAYS == 30
        gone = [NOW - timedelta(days=30, seconds=1), NOW - timedelta(days=31),
                NOW - timedelta(days=90)]
        kept = [NOW - timedelta(days=29, hours=23), NOW - timedelta(days=1), NOW]
        _seed(db, gone)
        _seed(db, kept, grp='mirror-1')          # per row, not per provider

        svc._cleanup_client_probe_sync(now=NOW)

        assert _remaining(db) == sorted(_ts(t) for t in kept)

    def test_row_exactly_at_cutoff_is_kept(self, svc, db):
        """``ts < cutoff`` — the row AT the cutoff is inside the window."""
        _seed(db, [CUTOFF - timedelta(seconds=1), CUTOFF, CUTOFF + timedelta(seconds=1)])

        svc._cleanup_client_probe_sync(now=NOW)

        assert _remaining(db) == [_ts(CUTOFF), _ts(CUTOFF + timedelta(seconds=1))]

    def test_later_rows_of_the_cutoff_day_stay(self, svc, db):
        """The space/'T' trap: an isoformat cutoff ('2026-09-08T12:00:00')
        sorts after '2026-09-08 13:00:00' and would drop the whole day."""
        same_day_later = CUTOFF + timedelta(hours=1)
        same_day_earlier = CUTOFF - timedelta(hours=1)
        assert _ts(same_day_later)[:10] == _ts(CUTOFF)[:10]
        _seed(db, [same_day_earlier, same_day_later])

        svc._cleanup_client_probe_sync(now=NOW)

        assert _remaining(db) == [_ts(same_day_later)]

    def test_rows_written_by_the_endpoint_shape_stay(self, svc, db):
        """A row with the DB default ts — exactly what /probe inserts."""
        with db._connect() as conn:
            conn.execute("INSERT INTO client_probe (chat_id, grp, src_ip) "
                         "VALUES ('1', 'emergency', '192.0.2.1')")
            conn.commit()
        _seed(db, [datetime.utcnow() - timedelta(days=31)])

        svc._cleanup_client_probe_sync()          # the scheduler passes nothing

        assert _count(db) == 1

    def test_logs_dropped_and_kept_counts(self, svc, db, caplog):
        _seed(db, [CUTOFF - timedelta(days=2)] * 3 + [NOW] * 2)
        caplog.set_level(logging.INFO, logger=LOGGER)

        svc._cleanup_client_probe_sync(now=NOW)

        assert 'client_probe cleanup: dropped 3 rows older than 30d' in caplog.text
        assert '2 rows kept' in caplog.text

    def test_empty_table_still_logs_a_heartbeat(self, svc, db, caplog):
        caplog.set_level(logging.INFO, logger=LOGGER)

        svc._cleanup_client_probe_sync(now=NOW)

        assert 'dropped 0 rows' in caplog.text and '0 rows kept' in caplog.text


class TestBatching:

    def test_batches_terminate_and_delete_everything_eligible(self, svc, db, caplog):
        assert NotificationService.CLIENT_PROBE_CLEANUP_BATCH == 20000
        svc.CLIENT_PROBE_CLEANUP_BATCH = 10
        svc.CLIENT_PROBE_CLEANUP_PAUSE_S = 0.001
        _seed(db, [CUTOFF - timedelta(hours=h) for h in range(1, 26)])   # 25 old
        keep = [NOW - timedelta(minutes=m) for m in range(5)]             # 5 new
        _seed(db, keep)
        caplog.set_level(logging.INFO, logger=LOGGER)

        with patch('time.sleep') as sleep:
            svc._cleanup_client_probe_sync(now=NOW)

        assert _remaining(db) == sorted(_ts(t) for t in keep)
        assert sleep.call_count == 3                 # 10 + 10 + 5, then an empty batch
        sleep.assert_called_with(0.001)
        assert 'dropped 25 rows' in caplog.text and 'in 3 batch(es)' in caplog.text

    def test_each_batch_is_committed_before_the_next_starts(self, svc, db):
        svc.CLIENT_PROBE_CLEANUP_BATCH = 10
        _seed(db, [CUTOFF - timedelta(hours=h) for h in range(1, 26)])   # 25 old
        _seed(db, [NOW] * 5)
        seen = []

        def observe(_pause):
            with sqlite3.connect(db.db_path) as other:      # a concurrent reader
                seen.append(other.execute(
                    "SELECT COUNT(*) FROM client_probe").fetchone()[0])

        with patch('time.sleep', side_effect=observe):
            svc._cleanup_client_probe_sync(now=NOW)

        assert seen == [20, 10, 5]


class TestFailure:

    def test_exception_is_logged_and_swallowed(self, svc, db, caplog):
        with db._connect() as conn:
            conn.execute("DROP TABLE client_probe")
            conn.commit()
        caplog.set_level(logging.INFO, logger=LOGGER)

        svc._cleanup_client_probe_sync(now=NOW)          # must not raise

        failed = [r for r in caplog.records
                  if 'client_probe cleanup failed' in r.getMessage()]
        assert len(failed) == 1 and failed[0].exc_info is not None
        assert 'rows kept' not in caplog.text

    def test_finished_batches_stay_deleted(self, svc, db, caplog):
        svc.CLIENT_PROBE_CLEANUP_BATCH = 10
        _seed(db, [CUTOFF - timedelta(hours=h) for h in range(1, 26)])   # 25 old
        caplog.set_level(logging.INFO, logger=LOGGER)

        with patch('time.sleep', side_effect=[None, RuntimeError('disk on fire')]):
            svc._cleanup_client_probe_sync(now=NOW)      # must not raise

        assert _count(db) == 5
        assert 'client_probe cleanup failed after 20 rows' in caplog.text


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


class TestRegistration:

    def test_daily_job_is_registered(self, svc):
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

        calls = [c for c in svc.scheduler.calls if c[2].get('id') == 'client_probe_cleanup']
        assert len(calls) == 1
        func, trigger, kwargs = calls[0]
        assert func == svc._cleanup_client_probe_sync
        assert trigger.kwargs == {'hours': 24}
        assert kwargs.get('replace_existing') is True
