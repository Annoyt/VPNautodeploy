"""Probes and their readers — IMPROVEMENT_PLAN F, package B (B1, B4, B5, B6).

* B1  the prober: every request of a run starts at once, so each has its own
      10-s budget (until 2026-11 five connections served ten requests and the
      wait counted in the timeout); ten targets that all pass through the
      exit; a bulk download per protocol in its own table.
* B5  one definition of dark / degraded (bot/services/probe_rules.py) for the
      pager, DPIMonitor, /protocols + /sos and the agent's script.
* B4  problem 7: what the operator switched off is neither paged nor demoted;
      problem 8: a demoted protocol without a judged window is not "quiet".
* B6  the DE reserve is probed, pages as the reserve, and never counts in
      "every protocol dark at once".

Level 2: real sqlite (Database in tmp_path), the real checks; the prober
runs against a local forward proxy on 127.0.0.1.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web

from bot.core.database import Database
from bot.services import probe_rules as pr
from bot.services.alert_manager import build_default_checks
from bot.services.dpi_monitor import DPIMonitor
from bot.services.health_checker import HealthChecker

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

TARGETS = list(pr.PROBE_TARGETS)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def seed_window(db, tag, ok_per_run, *, runs=3, alive_fail=True, now=None,
                minutes_ago=5):
    """``runs`` runs of ten rows, newest first — HealthChecker's shape.
    Failures carry a latency when ``alive_fail`` (an error that came back
    through the tunnel), none otherwise (nothing connected)."""
    now = now or datetime.utcnow()
    with db._connect() as conn:
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


def set_cascade(db, *off):
    db.set_setting('cascade_protocol_order', json.dumps(
        [{'name': n, 'enabled': n not in off}
         for n in ('stls', 'ws', 'hy2', 'reality', 'hy2t')]))


def pager(db):
    cfg = Mock()
    cfg.DB_PATH = db.db_path
    check = next(c for c in build_default_checks(cfg, Mock())
                 if c.__name__ == 'check_protocol_probe_down')
    prefix, alerts = check()[:2]
    assert prefix == 'protocol_down:'
    return {a.key: a for a in alerts}


# ------------------------------------------------------------ B5 rules -------

class TestSharedRules:

    @pytest.mark.parametrize('ok,alive,n,verdict', [
        (0, 0, 30, 'dark'),
        (0, 0, 14, 'thin'),          # under PROBE_MIN_SAMPLES: no verdict
        (0, 0, 15, 'dark'),
        (7, 30, 30, 'ok'),           # 23 % is degraded…
        (6, 30, 30, 'degraded'),
        (8, 30, 30, 'ok'),           # …27 % is not
        (7, 30, 29, 'ok'),
        (2, 20, 20, 'ok'),           # degraded needs ≥ 25 rows
        (3, 30, 30, 'degraded'),
    ])
    def test_judge_window(self, ok, alive, n, verdict):
        rows = ([('ok', 100)] * ok + [('error', 900)] * (alive - ok)
                + [('timeout', None)] * (n - alive))
        if verdict == 'ok' and ok == 7:
            rows = [('ok', 100)] * 8 + [('error', 900)] * (n - 8)
        assert pr.judge_window(rows)['verdict'] == verdict

    def test_degraded_boundary_is_strictly_below_a_quarter(self):
        rows = [('ok', 1)] * 25 + [('error', 9)] * 75      # exactly 25 %
        assert pr.judge_window(rows[:100])['verdict'] == 'ok'
        rows = [('ok', 1)] * 24 + [('error', 9)] * 76
        assert pr.judge_window(rows)['verdict'] == 'degraded'

    def test_an_error_with_a_latency_is_alive(self):
        assert pr.is_alive('error', 418) and pr.is_alive('ok', None)
        assert not pr.is_alive('timeout', None)

    def test_last_run_dark(self):
        assert pr.last_run_dark([('error', 9)] * 10 + [('ok', 1)] * 20) is True
        assert pr.last_run_dark([('ok', 1)] + [('error', 9)] * 29) is False
        assert pr.last_run_dark([('error', 9)] * 9) is None   # no full run yet

    @pytest.mark.parametrize('raw,off', [
        # an opt-in protocol (xhr) is off until an entry switches it on,
        # unreadable and missing settings included
        (None, {'xhr'}), ('', {'xhr'}), ('{bad', {'xhr'}), ('42', {'xhr'}), ('[]', {'xhr'}),
        ('["hy2", "stls"]', {'xhr'}),                             # legacy: listed = on
        ('[{"name": "hy2t", "enabled": false}, {"name": "hy2"}]', {'hy2t', 'xhr'}),
        ([{'name': 'ws', 'enabled': False}, 'x', {'enabled': False}], {'ws', 'xhr'}),
        ('[{"name": "xhr", "enabled": true}]', set()),
        ('[{"name": "xhr"}, {"name": "hy2t", "enabled": false}]', {'hy2t'}),
        ('["hy2", "xhr"]', set()),
        # a name counts by its first entry, as in the editor
        ('[{"name": "ws", "enabled": true}, {"name": "ws", "enabled": false}]', {'xhr'}),
        ('[{"name": "xhr", "enabled": false}, {"name": "xhr", "enabled": true}]', {'xhr'}),
    ])
    def test_operator_disabled(self, raw, off):
        assert pr.operator_disabled(raw) == off

    @pytest.mark.parametrize('raw', [
        None, '', '{bad', '42', '[]', '["zzz"]', '["hy2", "stls"]', '["hy2", "xhr"]',
        # prod on 2026-11-12: saved before xhr existed
        '[{"name": "hy2t", "enabled": false}, {"name": "stls", "enabled": true},'
        ' {"name": "ws", "enabled": true}, {"name": "hy2", "enabled": true},'
        ' {"name": "reality", "enabled": true}]',
        '[{"name": "xhr", "enabled": true}, {"name": "ws", "enabled": false}]',
        '[{"name": "xhr", "enabled": false}]', '[{"name": "xhr"}]',
        '[{"name": "ws", "enabled": true}, {"name": "ws", "enabled": false}]',
        '[{"name": "reality", "enabled": false}, "reality", {"name": "zzz", "enabled": false}]',
    ])
    def test_off_is_what_the_cascade_editor_shows(self, raw):
        """The pager, DPIMonitor and the healthcheck script call a protocol
        off exactly when the editor (and so every profile) does."""
        from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
        editor = MK.get_cascade_config(SimpleNamespace(get_setting=lambda key: raw))
        editor_off = {c['name'] for c in editor if not c['enabled']}
        assert pr.operator_disabled(raw) & set(MK.PROTOCOL_METHOD_MAP) == editor_off
        assert MK.OPT_IN_PROTOCOLS is pr.OPT_IN_PROTOCOLS

    def test_the_targets_all_pass_through_a_foreign_exit(self):
        # vk.com answered 418 and sberbank.ru failed TLS (Russian CA) on
        # every probe of the week to 2026-11-10: they made 8/10 the norm
        assert 'vk.com' not in TARGETS and 'sberbank.ru' not in TARGETS

    def test_the_script_needs_the_same_sample_size_for_degraded(self):
        from scripts import protocol_healthcheck as hc
        now = datetime.utcnow()
        rows = [[(now - timedelta(minutes=5 + 15 * (i // 10))).isoformat(),
                 'ok' if i % 10 < 2 else 'error', 900] for i in range(20)]
        # 4/20 = 20 % but only 20 rows: the pager says "not enough to call
        # it degraded", and so must the script
        assert hc._probe_verdict('ws', {'ws': {'rows': rows, 'last_alive': rows[0][0]}},
                                 now)['state'] == 'OK'

    def test_every_reader_uses_the_same_numbers(self):
        from bot.handlers.admin.ops import AdminOpsMixin
        from bot.services import dpi_monitor as dm
        from scripts import protocol_healthcheck as hc
        assert HealthChecker.TARGET_DOMAINS == TARGETS and len(TARGETS) == pr.PROBE_DOMAINS == 10
        assert (dm.PROBE_RUNS, dm.PROBE_MIN_SAMPLES, dm.PROBE_DEGRADED_OK_RATIO,
                dm.PROBE_DEGRADED_MIN_SAMPLES, dm.PROBE_STALE_MIN) == (
            pr.PROBE_RUNS, pr.PROBE_MIN_SAMPLES, pr.PROBE_DEGRADED_OK_RATIO,
            pr.PROBE_DEGRADED_MIN_SAMPLES, pr.PROBE_STALE_MIN)
        assert (AdminOpsMixin.PROTO_RUNS, AdminOpsMixin.PROTO_MIN_SAMPLES,
                AdminOpsMixin.PROTO_WINDOW_H) == (pr.PROBE_RUNS, pr.PROBE_MIN_SAMPLES,
                                                  pr.PROBE_WINDOW_H)
        assert (hc.DEGRADED_BELOW, hc.DEGRADED_MIN_SAMPLES, hc.PROBE_DOMAINS) == (
            pr.PROBE_DEGRADED_OK_RATIO, pr.PROBE_DEGRADED_MIN_SAMPLES, pr.PROBE_DOMAINS)

    @pytest.mark.parametrize('ok_per_run,expect', [(2, 'degraded'), (3, 'ok'), (0, 'dark')])
    def test_same_rows_same_verdict_everywhere(self, db, ok_per_run, expect):
        """2/10 a run: the pager, the monitor, /protocols and the script all
        say degraded; 3/10 (9/30): all say fine. Until 2026-11 /protocols
        said degraded below half of the last run and the script below 35 %."""
        from bot.handlers.admin.ops import probe_verdict, read_probe_state
        from scripts import protocol_healthcheck as hc
        for tag in ('reality', 'hy2', 'stls'):
            seed_window(db, tag, 8)
        seed_window(db, 'ws', ok_per_run, alive_fail=ok_per_run != 0)
        alerts = pager(db)
        pager_says = ('dark' if 'protocol_down:ws' in alerts
                      else 'degraded' if 'protocol_down:ws:degraded' in alerts else 'ok')
        mon = DPIMonitor(db, Mock(), None).collect_signals()['probe']
        monitor_says = ('dark' if 'ws' in mon['dark']
                        else 'degraded' if 'ws' in mon['degraded'] else 'ok')
        cfg = SimpleNamespace(HY2T_PORT='', FALLBACK_NODE_HOST='', FALLBACK_NODE_PBK='')
        state = read_probe_state(db, cfg, datetime.utcnow(), window_h=3, runs=3)
        card = {'down': 'dark', 'degraded': 'degraded', 'ok': 'ok', 'silent': 'ok'}[
            probe_verdict(state['per_tag']['ws'], min_samples=15)]
        with db._connect() as conn:
            rows = [[r[0], r[1], r[2]] for r in conn.execute(
                "SELECT ts, status, latency_ms FROM outbound_health WHERE outbound_tag='ws' "
                "ORDER BY ts DESC").fetchall()]
        script = hc._probe_verdict('ws', {'ws': {'rows': rows, 'last_alive': rows[0][0]}},
                                   datetime.utcnow())['state']
        script_says = {'DOWN': 'dark', 'DEGRADED': 'degraded', 'OK': 'ok'}[script]
        if expect == 'dark':
            assert pager_says == monitor_says == card == script_says == 'dark'
        else:
            assert pager_says == monitor_says == card == expect
            # the script adds its early sign (a newest run with no ok) on top
            assert script_says in (expect, 'degraded')


# ------------------------------------------------------------ B4 / B6 --------

class TestPager:

    def test_a_disabled_protocol_is_not_paged(self, db):
        set_cascade(db, 'hy2t')
        for tag in ('reality', 'hy2', 'ws', 'stls'):
            seed_window(db, tag, 8)
        seed_window(db, 'hy2t', 0, alive_fail=False)
        assert 'protocol_down:hy2t' not in pager(db)
        set_cascade(db)                                      # switched back on
        assert 'protocol_down:hy2t' in pager(db)

    def test_an_opt_in_protocol_nobody_switched_on_is_not_paged(self, db):
        # prod, 2026-11-12: the saved cascade predates xhr, the xhr server
        # lists nobody, and its probe is dark by construction
        set_cascade(db)
        for tag in ('reality', 'hy2', 'ws', 'stls', 'hy2t'):
            seed_window(db, tag, 8)
        seed_window(db, 'xhr', 0, alive_fail=False)
        assert not any(k.startswith('protocol_down:xhr') for k in pager(db))
        db.set_setting('cascade_protocol_order', json.dumps(
            [{'name': n, 'enabled': True} for n in ('stls', 'ws', 'hy2', 'reality', 'hy2t', 'xhr')]))
        assert 'protocol_down:xhr' in pager(db)            # switched on: judged

    def test_a_switched_off_protocol_that_answers_rules_out_all_dark(self, db):
        # ws and stls are off in the cascade but their probes answer: the
        # channel is up, so reality and hy2 dark are two inbounds, not one
        # upstream incident (review of B: the old rule said «общий канал»)
        set_cascade(db, 'ws', 'stls')
        for tag in ('reality', 'hy2'):
            seed_window(db, tag, 0, alive_fail=False)
        for tag in ('ws', 'stls'):
            seed_window(db, tag, 8)
        alerts = pager(db)
        assert 'protocol_down:all' not in alerts
        assert {'protocol_down:reality', 'protocol_down:hy2'} <= set(alerts)
        assert not any(k.startswith(('protocol_down:ws', 'protocol_down:stls')) for k in alerts)

    def test_switched_off_and_dark_too_is_all_dark(self, db):
        set_cascade(db, 'ws', 'stls')
        for tag in ('reality', 'hy2', 'ws', 'stls'):
            seed_window(db, tag, 0, alive_fail=False)
        alerts = pager(db)
        assert 'protocol_down:all' in alerts and len(alerts) == 1

    def test_the_reserve_pages_as_the_reserve(self, db):
        for tag in ('reality', 'hy2', 'ws', 'stls'):
            seed_window(db, tag, 8)
        seed_window(db, 'de', 0, alive_fail=False)
        alerts = pager(db)
        assert alerts['protocol_down:de'].title.startswith('Резерв DE не отвечает')
        assert alerts['protocol_down:de'].severity == 'critical'

    def test_exit_down_is_all_dark_even_with_the_reserve_alive(self, db):
        for tag in ('reality', 'hy2', 'ws', 'stls'):
            seed_window(db, tag, 0, alive_fail=False)
        seed_window(db, 'de', 10)
        alerts = pager(db)
        assert list(alerts) == ['protocol_down:all']

    def test_every_alert_carries_every_protocol(self, db):
        set_cascade(db, 'hy2t')
        for tag, ok in (('reality', 8), ('hy2', 0), ('ws', 9), ('stls', 2), ('hy2t', 0)):
            seed_window(db, tag, ok, alive_fail=tag != 'hy2')
        alerts = pager(db)
        for key in ('protocol_down:hy2', 'protocol_down:stls:degraded'):
            line = alerts[key].detail.rsplit('Сейчас по пробам: ', 1)[1]
            assert line.startswith('hy2 ok 0/30 (живых 0) · hy2t выключен · reality ok 24/30')
            assert 'stls ok 6/30' in line and 'ws ok 27/30' in line


class TestMonitor:

    def test_a_disabled_protocol_is_not_demoted(self, db):
        set_cascade(db, 'ws')
        for tag in ('reality', 'hy2', 'stls'):
            seed_window(db, tag, 8)
        seed_window(db, 'ws', 0, alive_fail=False)
        signals = DPIMonitor(db, Mock(), None).collect_signals()['probe']
        assert 'ws' not in signals['dark'] and 'ws' not in signals['measured']

    def test_an_opt_in_protocol_nobody_switched_on_is_not_demoted(self, db):
        set_cascade(db)                                     # saved before xhr existed
        for tag in ('reality', 'hy2', 'stls', 'ws'):
            seed_window(db, tag, 8)
        seed_window(db, 'xhr', 0, alive_fail=False)
        signals = DPIMonitor(db, Mock(), None).collect_signals()['probe']
        assert 'xhr' not in signals['dark'] and 'xhr' not in signals['measured']
        db.set_setting('cascade_protocol_order', json.dumps(
            [{'name': n, 'enabled': True} for n in ('stls', 'ws', 'hy2', 'reality', 'xhr')]))
        assert 'xhr' in DPIMonitor(db, Mock(), None).collect_signals()['probe']['dark']

    def test_the_reserve_is_never_a_monitor_signal(self, db):
        seed_window(db, 'de', 0, alive_fail=False)
        for tag in ('reality', 'hy2', 'ws', 'stls'):
            seed_window(db, tag, 8)
        signals = DPIMonitor(db, Mock(), None).collect_signals()['probe']
        assert 'de' not in signals['measured'] and not signals['dark']

    def test_no_window_means_no_good_evaluation(self):
        """Problem 8: hy2 demoted by the probes, then its rows thin out —
        the good streak must wait, not count up to a restore."""
        from bot.services.dpi_monitor import empty_signals
        state = {'targets': {'global:hy2': {'bad': 0, 'good': 3,
                                                   'last_change': None, 'rule': 'probe_dark'}},
                 'auto': {'global': {'hy2': {'since': 'x', 'reason': 'probe_dark'}}, 'asn': {}}}
        sig = empty_signals()
        sig['probe'].update(stale=False, measured=['reality', 'ws', 'stls'])
        now = datetime(2026, 11, 10, 12)
        for _ in range(10):
            state, changes = DPIMonitor.evaluate(sig, state, now)
            assert changes == []
            now += timedelta(minutes=10)
        assert state['targets']['global:hy2']['good'] == 3
        # measured and quiet again → the streak resumes and restores
        sig['probe']['measured'].append('hy2')
        for _ in range(3):
            state, changes = DPIMonitor.evaluate(sig, state, now)
            now += timedelta(minutes=10)
        assert [c.action for c in changes] == ['restore']


# ------------------------------------------------------------ B1 prober ------

class _ForwardProxy:
    """A plain-http forward proxy on 127.0.0.1: answers HEAD after
    ``delay`` s, and the bulk path with ``bulk`` bytes (stalling after
    ``stall_after`` bytes when set)."""

    def __init__(self, delay=0.0, bulk=None, stall_after=None, status=200):
        self.delay, self.bulk, self.stall_after, self.status = delay, bulk, stall_after, status
        self.inflight = self.peak = 0

    async def handle(self, request):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            if 'bulk' in request.path_qs:
                resp = web.StreamResponse(status=200)
                await resp.prepare(request)
                sent = 0
                while sent < (self.bulk or 0):
                    if self.stall_after is not None and sent >= self.stall_after:
                        await asyncio.sleep(3)
                    await resp.write(b'x' * 16384)
                    sent += 16384
                return resp
            await asyncio.sleep(self.delay)
            return web.Response(status=self.status)
        finally:
            self.inflight -= 1

    async def __aenter__(self):
        # A bare server, no router: a forward proxy gets absolute-form
        # targets ("HEAD http://yandex.ru"), which no route would match.
        self.runner = web.ServerRunner(web.Server(self.handle))
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


def _checker(db, port, **over):
    hc = HealthChecker(db, SimpleNamespace(HY2T_PORT='', FALLBACK_NODE_HOST='',
                                           FALLBACK_NODE_PBK='', PROBE_PROXY_HOST='127.0.0.1'))
    hc.TARGET_SCHEME = 'http'
    hc.BULK_URL = 'http://bulk.test/bulk?bytes={bytes}'
    hc.TIMEOUT_SEC = over.pop('timeout', 1.0)
    hc.BULK_TIMEOUT_SEC = over.pop('bulk_timeout', 2.0)
    hc._proxy_url = lambda protocol: f'http://127.0.0.1:{port}'
    for k, v in over.items():
        setattr(hc, k, v)
    return hc


def _rows(db, table='outbound_health'):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            f"SELECT * FROM {table} ORDER BY id").fetchall()]


class TestProber:

    @pytest.mark.asyncio
    async def test_a_slow_path_is_not_read_as_degraded(self, db):
        """Every target answers in 0.6 s against a 1-s budget: all ten pass,
        because all ten start at once. Five connections for ten requests
        (the pre-2026-11 pool) would time out half of them."""
        async with _ForwardProxy(delay=0.6, bulk=HealthChecker.BULK_BYTES) as proxy:
            hc = _checker(db, proxy.port)
            res = await hc._check_protocol('ws')
        assert {d: r['status'] for d, r in res.items()} == {d: 'ok' for d in TARGETS}
        assert proxy.peak >= len(TARGETS)
        assert [r[3] for r in _rows(db)] == TARGETS        # target_domain

    @pytest.mark.asyncio
    async def test_bulk_probe_is_written_apart(self, db):
        async with _ForwardProxy(bulk=HealthChecker.BULK_BYTES) as proxy:
            await _checker(db, proxy.port)._check_protocol('reality')
        bulk = _rows(db, 'outbound_bulk')
        assert len(bulk) == 1
        _id, ts, tag, status, got, duration, err = bulk[0]
        assert (tag, status, got, err) == ('reality', 'ok', HealthChecker.BULK_BYTES, None)
        assert HealthChecker.BULK_BYTES >= 64 * 1024
        # the HEAD rows stay ten a run — the thresholds count them
        assert len(_rows(db)) == pr.PROBE_DOMAINS

    @pytest.mark.asyncio
    async def test_a_short_body_is_partial(self, db):
        async with _ForwardProxy(bulk=32 * 1024) as proxy:
            await _checker(db, proxy.port)._check_protocol('ws')
        _id, _ts, _tag, status, got, _d, err = _rows(db, 'outbound_bulk')[0]
        assert (status, got, err) == ('partial', 32 * 1024, 'short body')

    @pytest.mark.asyncio
    async def test_a_hanging_target_costs_its_own_budget_only(self, db):
        async with _ForwardProxy(delay=1.5, bulk=HealthChecker.BULK_BYTES) as proxy:
            t0 = time.monotonic()
            res = await _checker(db, proxy.port, timeout=0.5)._check_protocol('ws')
            took = time.monotonic() - t0
        assert {r['status'] for r in res.values()} == {'timeout'}
        assert took < 2.5          # ten budgets in parallel, not one after another

    @pytest.mark.asyncio
    async def test_a_freeze_after_the_first_kilobytes_is_partial(self, db):
        async with _ForwardProxy(bulk=HealthChecker.BULK_BYTES, stall_after=16384) as proxy:
            await _checker(db, proxy.port, bulk_timeout=1.0)._check_protocol('ws')
        _id, _ts, _tag, status, got, _d, _e = _rows(db, 'outbound_bulk')[0]
        assert status == 'partial' and 0 < got < HealthChecker.BULK_BYTES

    @pytest.mark.asyncio
    async def test_reserve_without_a_sidecar_inbound_writes_nothing(self, db, unused_tcp_port):
        hc = _checker(db, unused_tcp_port)       # nothing listens: proxy_down
        assert await hc._check_protocol('de') is None
        assert _rows(db) == [] and _rows(db, 'outbound_bulk') == []
        # a base protocol in the same state is recorded (a sidecar outage)
        await hc._check_protocol('ws')
        assert {r[4] for r in _rows(db)} == {'proxy_down'}  # status

    def test_reserve_joins_where_configured(self):
        on = SimpleNamespace(HY2T_PORT='', FALLBACK_NODE_HOST='198.51.100.9',
                             FALLBACK_NODE_PBK='pbk')
        assert HealthChecker.probe_ports_for(on)['de'] == 18086
        off = SimpleNamespace(HY2T_PORT='', FALLBACK_NODE_HOST='198.51.100.9',
                              FALLBACK_NODE_PBK='')
        assert 'de' not in HealthChecker.probe_ports_for(off)
        assert 'de' not in HealthChecker.probe_ports_for(Mock())


@pytest.fixture
def unused_tcp_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]
