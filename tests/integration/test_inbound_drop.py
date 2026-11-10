"""Users' connections on an exit inbound fell away — IMPROVEMENT_PLAN F, B2.

2026-10-29 .. 11-02 ws connections on exit dropped to zero for 3.5 days while
the datacenter probes saw ws healthy. ``check_inbound_drop`` reads the exit's
own count (dpi_metrics) in 2-h blocks against the same block on the previous
7 days, and tells "the exit did not report" (exit_reports heartbeat) from
"nobody connected".

Level 2: real sqlite, the real check; the heartbeat handler on a real
WebAppServer.
"""

import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.core.database import Database
from bot.services.alert_manager import build_default_checks

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

STEP = timedelta(minutes=5)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def seed(db, *, tag='cf-ws', per_snapshot=10, days=7.5, quiet_h=0.0, quiet_value=0,
         beats=True, beat_gap_h=None, now=None):
    """Exit reports every 5 min for ``days`` back: ``per_snapshot`` accepted
    connections on ``tag`` (240 a 2-h block), ``quiet_value`` during the last
    ``quiet_h`` hours. ``beat_gap_h`` = (from_h, to_h) ago with no reports."""
    now = now or datetime.utcnow()
    t = now - timedelta(days=days)
    rows, hb = [], []
    while t < now - timedelta(minutes=1):
        ago_h = (now - t).total_seconds() / 3600
        gap = beat_gap_h and beat_gap_h[1] <= ago_h < beat_gap_h[0]
        if not gap:
            n = quiet_value if ago_h < quiet_h else per_snapshot
            rows.append((t.isoformat(), '*TUNNEL*', None, 'entry-forward', tag, n))
            # another inbound keeps reporting alongside
            rows.append((t.isoformat(), '*TUNNEL*', None, 'entry-forward', 'ss2022', 30))
            if beats:
                hb.append((t.isoformat(), 2))
        t += STEP
    with db._connect() as conn:
        conn.executemany(
            "INSERT INTO dpi_metrics (snapshot_at, country, asn, as_org, inbound_tag, "
            "conn_count) VALUES (?,?,?,?,?,?)", rows)
        conn.executemany("INSERT INTO exit_reports (ts, rows) VALUES (?,?)", hb)
        conn.commit()


def check_for(db):
    cfg = Mock()
    cfg.DB_PATH = db.db_path
    return next(c for c in build_default_checks(cfg, Mock())
                if c.__name__ == 'check_inbound_drop')


def run(check):
    prefix, alerts = check()
    assert prefix == 'inbound_drop:'
    return {a.key: a for a in alerts}


class TestDrop:

    def test_normal_traffic_is_quiet(self, db):
        seed(db)
        assert run(check_for(db)) == {}

    def test_eight_quiet_hours_page(self, db):
        seed(db, quiet_h=8.5)
        alerts = run(check_for(db))
        a = alerts['inbound_drop:ws']
        assert a.severity == 'critical' and a.min_cycles == 1
        assert a.title.startswith('Подключения ws на exit упали: 0 за 8 ч при обычных ~960')
        assert 'Инбаунд cf-ws' in a.detail and '0 из ~240' in a.detail
        assert 'inbound_drop:stls' not in alerts            # ss2022 kept its pace

    def test_six_quiet_hours_are_not_enough(self, db):
        seed(db, quiet_h=6.5)
        assert run(check_for(db)) == {}

    def test_a_fifth_of_usual_still_counts_as_usual(self, db):
        seed(db, quiet_h=8.5, quiet_value=2)                 # 48 of 240 = 20 %
        assert run(check_for(db)) == {}
        db2 = db
        with db2._connect() as conn:
            conn.execute("DELETE FROM dpi_metrics")
            conn.execute("DELETE FROM exit_reports")
            conn.commit()
        seed(db2, quiet_h=8.5, quiet_value=1)                # 24 of 240
        assert 'inbound_drop:ws' in run(check_for(db2))

    def test_a_quiet_inbound_proves_nothing(self, db):
        seed(db, per_snapshot=1, quiet_h=8.5)                # usual 24 a block < 50
        assert run(check_for(db)) == {}

    def test_no_reports_is_not_zero_connections(self, db):
        # the exit went silent for the last 3 h: those blocks have no
        # verdict — no inbound_drop, but the reporter itself is flagged
        seed(db, quiet_h=8.5, beat_gap_h=(3, 0))
        alerts = run(check_for(db))
        assert 'inbound_drop:ws' not in alerts
        assert alerts['inbound_drop:no_reports'].severity == 'warn'

    def test_an_open_alert_stays_while_reports_are_missing(self, db):
        seed(db, quiet_h=8.5)
        check = check_for(db)
        assert 'inbound_drop:ws' in run(check)
        with db._connect() as conn:          # 90 min unreported: 6 of 24 reports left
            cut = (datetime.utcnow() - timedelta(minutes=90)).isoformat()
            conn.execute("DELETE FROM exit_reports WHERE ts >= ?", (cut,))
            conn.execute("DELETE FROM dpi_metrics WHERE snapshot_at >= ?", (cut,))
            conn.commit()
        check.cache['at'] = 0.0                              # skip the 10-min cache
        assert 'inbound_drop:ws' in run(check)

    def test_history_before_the_heartbeat_counts_as_reported(self, db):
        # deploy day: exit_reports is empty, dpi_metrics snapshots vouch
        seed(db, quiet_h=8.5, beats=False)
        assert 'inbound_drop:ws' in run(check_for(db))

    def test_traffic_back_clears_it(self, db):
        seed(db, quiet_h=8.5)
        check = check_for(db)
        assert 'inbound_drop:ws' in run(check)
        with db._connect() as conn:
            conn.execute("UPDATE dpi_metrics SET conn_count = 10 WHERE inbound_tag = 'cf-ws'")
            conn.commit()
        assert 'inbound_drop:ws' in run(check)               # cached for 10 min
        check.cache['at'] = 0.0
        assert run(check) == {}


class TestHy2Rollup:

    def test_needs_the_bot_up_through_the_blocks(self, db):
        seed(db, tag='hy2-8400', quiet_h=8.5)
        check = check_for(db)
        assert run(check) == {}                              # just started
        check.cache.update(started=time.time() - 9 * 3600, at=0.0)
        assert 'inbound_drop:hy2' in run(check)


class TestHeartbeat:

    @pytest.mark.asyncio
    async def test_every_report_leaves_a_row_even_an_empty_one(self, db):
        from bot.core.web_server import WebAppServer
        cfg = SimpleNamespace(BOT_TOKEN='t', DPI_REPORT_TOKEN='sekret', ENTRY_NODE_IP='',
                              EXIT_NODE_IP='', WEBAPP_URL='', SUB_MIRROR_URLS='')
        srv = WebAppServer(cfg, db, xui_service=Mock())
        for payload in ({}, {'access': [{'tag': 'inbound-2053', 'conns': 3,
                                         'ips': {'198.51.100.7': 3}}]}):
            req = Mock()
            req.headers = {'X-DPI-Token': 'sekret'}
            req.json = AsyncMock(return_value=payload)
            assert (await srv.handle_dpi_exit_report(req)).status == 200
        bad = Mock()
        bad.headers = {'X-DPI-Token': 'wrong'}
        bad.json = AsyncMock(return_value={})
        assert (await srv.handle_dpi_exit_report(bad)).status == 403
        with db._connect() as conn:
            beats = [r[0] for r in conn.execute("SELECT rows FROM exit_reports ORDER BY id")]
        assert beats == [0, 1]
