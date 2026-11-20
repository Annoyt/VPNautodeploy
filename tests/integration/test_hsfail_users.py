"""Handshake failures from our users' own addresses are not probing — AGENTS.md §66.

2026-11-19 14:33 → 11-20 07:42 UTC: ~1,850 REALITY handshake failures paged
as ``Probing? RU/AS31133 hsfail N/h``. Most came from addresses that also had
normal sessions in the same hours: our users' clients retrying through the
jammed exit → entry tunnel (§64), almost always "handshake did not complete
successfully". MegaFon's 1,455 came from 2 users' addresses; the strangers
were a handful. The alert read as "Russian ISPs are probing Reality" and
helped switch Reality off.

Now the exit reporter tells a REALITY reject from an address that logged in
as one of our users (``origin: user``) from the rest (``unknown``); the bot
stores the users' part (``hsfail_users``, ``user_ips_json``) apart from the
probe candidates (``probe_ips_json``); ``dpi_hsfail:`` counts the probe
candidates only, ``dpi_hsfail_users:`` says outright that it is not probing,
and both say so while ``healthcheck:entry_tunnel:*`` is open.

Level 2: real sqlite, the real exit reporter on log files, the real
WebAppServer handler, the real alert checks and AlertManager; only the geo
lookups are stubbed. Documentation addresses only (RFC 5737).
"""

import importlib.util
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot.config import Settings
from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.services.alert_manager import (
    DASHBOARD_ONLY_PREFIXES, DPI_AGENT_PREFIXES, Alert, AlertManager, build_default_checks,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    'exit_dpi_reporter_hsfail', str(REPO / 'scripts' / 'exit_dpi_reporter.py'))
reporter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reporter)

ENTRY = '130.49.146.10'
USER_A = '198.51.100.23'      # a MegaFon user
USER_B = '198.51.100.57'      # another MegaFon user
USER_RT = '198.51.100.81'     # a Rostelecom user
STRANGER = '203.0.113.99'     # nobody of ours, on MegaFon
MEGAFON = ('RU', 'AS31133', 'PJSC MegaFon')
ROSTELECOM = ('RU', 'AS12389', 'PJSC Rostelecom')
NETS = {USER_A: MEGAFON, USER_B: MEGAFON, STRANGER: MEGAFON, USER_RT: ROSTELECOM}
for _i in range(20):
    NETS[f'203.0.113.{_i + 120}'] = MEGAFON
TIMEOUT = 'handshake did not complete successfully'


@pytest.fixture
def geo(monkeypatch):
    from bot.services import geoip

    def net(ip):
        return NETS.get(ip, ('DE', 'AS64500', 'Somewhere'))

    monkeypatch.setattr(geoip, 'lookup', lambda ip: (net(ip)[0], ''))
    monkeypatch.setattr(geoip, 'lookup_asn', lambda ip: (net(ip)[1], net(ip)[2]))


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def _server(db):
    config = Mock(spec=Settings)
    config.DPI_REPORT_TOKEN = 'tok'
    config.ENTRY_NODE_IP = ENTRY
    config.EXIT_NODE_IP = '84.75.76.109'
    for name in ('ENTRY_PUBLIC_HOST', 'HY2_HOST', 'FALLBACK_NODE_HOST'):
        setattr(config, name, '')
    config.is_admin = Mock(return_value=False)
    return WebAppServer(config, db, xui_service=Mock())


async def _ingest(srv, payload):
    req = Mock()
    req.headers = {'X-DPI-Token': 'tok'}
    req.json = AsyncMock(return_value=payload)
    resp = await srv.handle_dpi_exit_report(req)
    assert resp.status == 200
    return resp


def _login(ip, port=41000, email='user_a_1@nekovo.ru'):
    return (f"2026/11/19 21:04:10.000000 from {ip}:{port} accepted "
            f"tcp:www.google.com:443 [inbound-443 >> direct] email: {email}")


def _hs_fail(ip, port=41234, reason=TIMEOUT):
    return (f"2026/11/19 21:04:11.123456 [Info] transport/internet/tcp: REALITY: "
            f"processed invalid connection from {ip}:{port}: {reason}")


class _Exit:
    """The real reporter, run on log files; every payload it posts is kept."""

    def __init__(self, tmp_path, monkeypatch):
        self.logs = tmp_path / 'exit-logs'
        self.logs.mkdir()
        for name in ('access.log', 'error.log'):
            (self.logs / name).write_text('')
        monkeypatch.setattr(reporter, 'LOG_DIR', str(self.logs))
        monkeypatch.setattr(reporter, 'STATE_PATH', str(tmp_path / 'exit-state.json'))
        monkeypatch.setattr(reporter, 'TOKEN', 'tok')
        monkeypatch.setattr(reporter, 'read_hysteria_events', lambda state: [])
        self.sent = []
        monkeypatch.setattr(reporter, 'post_report', lambda p: self.sent.append(p) or True)
        assert reporter.run_once() == 0          # the first run only baselines
        self.sent.clear()

    def run(self, access=(), errors=()):
        for name, lines in (('access.log', access), ('error.log', errors)):
            with open(self.logs / name, 'a') as f:
                f.write(''.join(line + '\n' for line in lines))
        assert reporter.run_once() == 0
        return self.sent[-1]


def _checks(db):
    cfg = Mock()
    cfg.DB_PATH = db.db_path
    return {c.__name__: c for c in build_default_checks(cfg, Mock())}


def probing(db):
    prefix, alerts = _checks(db)['check_dpi_handshake_spike']()[:2]
    assert prefix == 'dpi_hsfail:'
    return {a.key: a for a in alerts}


def users(db):
    prefix, alerts = _checks(db)['check_dpi_handshake_users']()[:2]
    assert prefix == 'dpi_hsfail_users:'
    return {a.key: a for a in alerts}


def _rows(db, sql, args=()):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def _jam(db, key='healthcheck:entry_tunnel:queue', *, open_=True,
         opened_ago_min=600, closed_ago_min=None):
    now = time.time()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO alert_state (key, open, opened_ts, closed_ts) VALUES (?, ?, ?, ?)",
            (key, int(open_), now - opened_ago_min * 60,
             0 if closed_ago_min is None else now - closed_ago_min * 60))
        conn.commit()


# A seeded dpi_metrics row; snapshot ``ago`` minutes back.
def _seed(db, *, ago=5, net=MEGAFON, tag='reality', conns=0, fails=0, users_=None,
          probe_ips=None, user_ips=None):
    ts = (datetime.utcnow() - timedelta(minutes=ago)).isoformat()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO dpi_metrics (snapshot_at, country, asn, as_org, inbound_tag, "
            "conn_count, handshake_fail_count, hsfail_users, probe_ips_json, user_ips_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, net[0], net[1], net[2], tag, conns, fails, users_,
             json.dumps(probe_ips) if probe_ips else None,
             json.dumps(user_ips) if user_ips else None))
        conn.commit()


# ---------------------------------------------------------------------------
# The pin: 2026-11-20, end to end
# ---------------------------------------------------------------------------

class TestTheNovember20Shape:
    """Two MegaFon users log in now and then and fail 500 handshakes in the
    hour between (the jammed tunnel). Before §66 that paged ``Probing?
    RU/AS31133 hsfail 500/h``; it must not page as probing."""

    async def _hour(self, db, tmp_path, monkeypatch, stranger=0):
        exit_ = _Exit(tmp_path, monkeypatch)
        srv = _server(db)
        await _ingest(srv, exit_.run(access=[_login(USER_A), _login(USER_B, port=2)]))
        payload = exit_.run(errors=[_hs_fail(USER_A)] * 300 + [_hs_fail(USER_B)] * 200
                            + [_hs_fail(STRANGER)] * stranger)
        await _ingest(srv, payload)
        return payload

    @pytest.mark.asyncio
    async def test_a_users_failures_do_not_page_as_probing(self, db, tmp_path, monkeypatch, geo):
        await self._hour(db, tmp_path, monkeypatch)
        assert probing(db) == {}
        a = users(db)['dpi_hsfail_users:RU:AS31133']
        assert a.title == ("Users' handshakes fail, not probing: RU/AS31133 hsfail 500/h "
                           "from 2 user addresses (168.0× baseline)")
        assert 'Это не зондирование' in a.detail and 'PJSC MegaFon' in a.detail
        assert a.severity == 'critical' and a.agent is False
        # stored apart: the total, the users' part, their addresses; no
        # user address among the "probes"
        assert _rows(db, "SELECT conn_count, handshake_fail_count, hsfail_users, "
                         "probe_ips_json, user_ips_json FROM dpi_metrics "
                         "WHERE asn = 'AS31133' AND handshake_fail_count > 0") == [
            (0, 500, 500, None, json.dumps([[USER_A, 300], [USER_B, 200]]))]

    @pytest.mark.asyncio
    async def test_a_stranger_beside_them_is_counted_alone(self, db, tmp_path, monkeypatch, geo):
        await self._hour(db, tmp_path, monkeypatch, stranger=9)
        a = probing(db)['dpi_hsfail:RU:AS31133']
        assert a.title == ("Probing? RU/AS31133 hsfail 9/h from 1 unknown address "
                           "(168.0× baseline)")
        assert 'Ещё 500 — с адресов наших пользователей' in a.detail
        assert a.agent is True
        assert 'dpi_hsfail_users:RU:AS31133' in users(db)
        assert _rows(db, "SELECT probe_ips_json FROM dpi_metrics WHERE asn = 'AS31133' "
                         "AND handshake_fail_count > 0") == [(json.dumps([[STRANGER, 9]]),)]

    @pytest.mark.asyncio
    async def test_the_old_reporter_still_pages_and_says_so(self, db, tmp_path, monkeypatch, geo):
        """An exit reporter from before §66 sends no ``origin``: the bot
        counts every failure as before, and the title says users were not
        told apart (the deploy order of the two does not matter)."""
        payload = await self._hour(db, tmp_path, monkeypatch)
        with db._connect() as conn:
            conn.execute("DELETE FROM dpi_metrics")
            conn.commit()
        for rej in payload['rejects']:
            rej.pop('origin')
        await _ingest(_server(db), payload)
        a = probing(db)['dpi_hsfail:RU:AS31133']
        assert a.title == ("Probing? RU/AS31133 hsfail 500/h (168.0× baseline), "
                           "users not told apart")
        assert 'репортёр exit не разделил' in a.detail
        assert users(db) == {}
        assert _rows(db, "SELECT handshake_fail_count, hsfail_users FROM dpi_metrics "
                         "WHERE handshake_fail_count > 0") == [(500, None)]

    @pytest.mark.asyncio
    async def test_during_the_tunnel_jam_both_say_so(self, db, tmp_path, monkeypatch, geo):
        await self._hour(db, tmp_path, monkeypatch, stranger=9)
        _jam(db, opened_ago_min=4 * 60)
        p = probing(db)['dpi_hsfail:RU:AS31133']
        u = users(db)['dpi_hsfail_users:RU:AS31133']
        for a in (p, u):
            assert a.title.endswith(' — during an entry tunnel jam')
            assert 'healthcheck:entry_tunnel:queue (с ' in a.detail
            assert 'Агент не запускается' in a.detail
            assert a.severity == 'warn' and a.agent is False


# ---------------------------------------------------------------------------
# The ingest: what a report stores
# ---------------------------------------------------------------------------

class TestIngest:

    @pytest.mark.asyncio
    async def test_origins_go_to_their_columns(self, db, geo):
        await _ingest(_server(db), {
            'access': [{'tag': 'inbound-443', 'conns': 7, 'ips': {USER_A: 7}}],
            'rejects': [
                {'kind': 'reality', 'reason': TIMEOUT, 'count': 40, 'ips': {USER_A: 40},
                 'origin': 'user'},
                {'kind': 'reality', 'reason': TIMEOUT, 'count': 3, 'ips': {STRANGER: 3},
                 'origin': 'unknown'},
                {'kind': 'reality', 'reason': 'failed to read client hello', 'count': 2,
                 'ips': {STRANGER: 2}, 'origin': 'unknown'},
            ],
        })
        assert _rows(db, "SELECT conn_count, handshake_fail_count, hsfail_users, "
                         "probe_ips_json, user_ips_json, reason_buckets_json FROM dpi_metrics") == [
            (7, 45, 40, json.dumps([[STRANGER, 5]]), json.dumps([[USER_A, 40]]),
             json.dumps({TIMEOUT: 43, 'failed to read client hello': 2}))]

    @pytest.mark.asyncio
    async def test_an_accepted_address_is_never_a_probe(self, db, geo):
        await _ingest(_server(db), {
            'access': [{'tag': 'inbound-443', 'conns': 3, 'ips': {USER_A: 3}}], 'rejects': []})
        assert _rows(db, "SELECT conn_count, handshake_fail_count, hsfail_users, "
                         "probe_ips_json, user_ips_json FROM dpi_metrics") == [
            (3, 0, 0, None, None)]

    @pytest.mark.asyncio
    async def test_a_report_without_origins_is_not_told_apart(self, db, geo):
        await _ingest(_server(db), {
            'access': [],
            'rejects': [{'kind': 'reality', 'reason': TIMEOUT, 'count': 4,
                         'ips': {USER_A: 4}}]})
        assert _rows(db, "SELECT handshake_fail_count, hsfail_users, probe_ips_json, "
                         "user_ips_json FROM dpi_metrics") == [
            (4, None, json.dumps([[USER_A, 4]]), None)]

    @pytest.mark.asyncio
    async def test_our_own_address_is_never_a_users(self, db, geo):
        # entry's address is shared by every user and every scanner behind it
        await _ingest(_server(db), {
            'access': [],
            'rejects': [{'kind': 'reality', 'reason': TIMEOUT, 'count': 6,
                         'ips': {ENTRY: 6}, 'origin': 'user'}]})
        assert _rows(db, "SELECT country, handshake_fail_count, hsfail_users, "
                         "probe_ips_json, user_ips_json FROM dpi_metrics") == [
            ('*TUNNEL*', 6, 0, None, None)]

    @pytest.mark.asyncio
    async def test_failures_past_the_ip_cap_keep_their_origin(self, db, geo):
        await _ingest(_server(db), {
            'access': [],
            'rejects': [{'kind': 'reality', 'reason': TIMEOUT, 'count': 50,
                         'ips': {USER_A: 20}, 'origin': 'user'},
                        {'kind': 'reality', 'reason': TIMEOUT, 'count': 8,
                         'ips': {STRANGER: 5}, 'origin': 'unknown'}]})
        got = dict((r[0], r[1:]) for r in _rows(
            db, "SELECT country, handshake_fail_count, hsfail_users FROM dpi_metrics"))
        assert got == {'RU': (25, 20), None: (33, 30)}

    @pytest.mark.asyncio
    async def test_the_reach_marker_stays_out(self, db, geo):
        from bot.services.exit_reach import MARK_SRC
        await _ingest(_server(db), {
            'access': [],
            'rejects': [{'kind': 'reality', 'reason': 'x', 'count': 2,
                         'ips': {MARK_SRC: 2}, 'origin': 'unknown'}]})
        assert _rows(db, "SELECT * FROM dpi_metrics") == []

    @pytest.mark.asyncio
    async def test_an_unknown_origin_value_is_not_told_apart(self, db, geo):
        await _ingest(_server(db), {
            'access': [],
            'rejects': [{'kind': 'reality', 'reason': TIMEOUT, 'count': 4,
                         'ips': {USER_A: 4}, 'origin': 'friend'}]})
        assert _rows(db, "SELECT hsfail_users, probe_ips_json FROM dpi_metrics") == [
            (None, json.dumps([[USER_A, 4]]))]


# ---------------------------------------------------------------------------
# The checks on seeded rows: baselines, counts, the jam
# ---------------------------------------------------------------------------

def _week(db, *, per_hour_users=0, per_hour_unknown=0, net=MEGAFON, hours=7 * 24):
    """``hours`` of hourly rows ending 61 min ago (outside the alert window)."""
    for h in range(hours):
        fails = per_hour_users + per_hour_unknown
        _seed(db, ago=61 + 60 * h, net=net, conns=50, fails=fails, users_=per_hour_users,
              probe_ips=[[STRANGER, per_hour_unknown]] if per_hour_unknown else None,
              user_ips=[[USER_A, per_hour_users]] if per_hour_users else None)


class TestBaselines:

    def test_users_failures_do_not_hide_a_probing_burst(self, db):
        # a week of users' failures at 60/h, then 30/h from strangers: the
        # probe candidates' own baseline is the strangers' alone
        _week(db, per_hour_users=60)
        _seed(db, conns=50, fails=90, users_=60, probe_ips=[[STRANGER, 30]],
              user_ips=[[USER_A, 60]])
        a = probing(db)['dpi_hsfail:RU:AS31133']
        assert a.title.startswith('Probing? RU/AS31133 hsfail 30/h from 1 unknown address (')

    def test_steady_users_failures_are_their_usual(self, db):
        _week(db, per_hour_users=60)
        _seed(db, conns=50, fails=60, users_=60, user_ips=[[USER_A, 60]])
        assert users(db) == {}
        assert probing(db) == {}

    def test_steady_strangers_are_their_usual(self, db):
        _week(db, per_hour_unknown=30)
        _seed(db, conns=50, fails=30, users_=0, probe_ips=[[STRANGER, 30]])
        assert probing(db) == {}

    def test_the_floor_holds(self, db):
        _seed(db, conns=5, fails=4, users_=0, probe_ips=[[STRANGER, 4]])
        _seed(db, net=ROSTELECOM, conns=5, fails=4, users_=4, user_ips=[[USER_RT, 4]])
        assert probing(db) == {} and users(db) == {}


class TestTitles:

    def test_addresses_are_counted_across_the_hour(self, db):
        _seed(db, ago=5, conns=5, fails=12, users_=0,
              probe_ips=[[STRANGER, 6], ['203.0.113.120', 6]])
        _seed(db, ago=10, conns=5, fails=12, users_=0,
              probe_ips=[[STRANGER, 6], ['203.0.113.121', 6]])
        assert 'from 3 unknown addresses' in probing(db)['dpi_hsfail:RU:AS31133'].title

    def test_a_full_top_list_is_a_floor(self, db):
        ips = [[f'203.0.113.{120 + i}', 3] for i in range(10)]
        _seed(db, conns=5, fails=40, users_=0, probe_ips=ips)
        assert 'from 10+ unknown addresses' in probing(db)['dpi_hsfail:RU:AS31133'].title

    def test_one_user_address(self, db):
        _seed(db, net=ROSTELECOM, conns=5, fails=103, users_=103, user_ips=[[USER_RT, 103]])
        a = users(db)['dpi_hsfail_users:RU:AS12389']
        assert 'hsfail 103/h from 1 user address (' in a.title

    def test_the_tunnel_bucket_is_never_users(self, db):
        _seed(db, net=('*TUNNEL*', None, 'entry-forward'), conns=5, fails=80, users_=80)
        assert users(db) == {}


class TestTheJam:

    def _burst(self, db):
        _seed(db, conns=5, fails=40, users_=30, probe_ips=[[STRANGER, 10]],
              user_ips=[[USER_A, 30]])

    def test_no_jam_no_note(self, db):
        self._burst(db)
        p = probing(db)['dpi_hsfail:RU:AS31133']
        u = users(db)['dpi_hsfail_users:RU:AS31133']
        assert 'tunnel' not in p.title and 'tunnel' not in u.title
        assert p.agent is True and u.agent is False

    def test_a_jam_that_ended_in_the_window_still_counts(self, db):
        self._burst(db)
        _jam(db, open_=False, opened_ago_min=300, closed_ago_min=20)
        a = probing(db)['dpi_hsfail:RU:AS31133']
        assert a.title.endswith(' — during an entry tunnel jam')
        assert ' до ' in a.detail and a.agent is False

    def test_a_jam_that_ended_before_the_window_does_not(self, db):
        self._burst(db)
        _jam(db, open_=False, opened_ago_min=300, closed_ago_min=90)
        assert 'tunnel' not in probing(db)['dpi_hsfail:RU:AS31133'].title

    def test_other_open_alerts_are_no_jam(self, db):
        self._burst(db)
        _jam(db, key='reach:exit')                   # can stay open for days (§61)
        _jam(db, key='healthcheck:entry_dnat:hy2')
        _jam(db, key='healthcheck:entryXtunnel:fd')  # LIKE's '_' would have matched
        assert 'tunnel' not in probing(db)['dpi_hsfail:RU:AS31133'].title

    def test_the_fd_finding_is_a_jam_too(self, db):
        self._burst(db)
        _jam(db, key='healthcheck:entry_tunnel:fd')
        assert 'healthcheck:entry_tunnel:fd' in users(db)['dpi_hsfail_users:RU:AS31133'].detail

    def test_the_watchdogs_keys_are_the_jams(self):
        """The healthcheck names its tunnel findings; the bot prefixes them
        with 'healthcheck:'. A renamed finding must not quietly turn the
        DPI alerts' note and R3's freeze off."""
        from bot.services.tunnel_jam import PREFIX
        from .test_entry_tunnel_watch import _hc
        keys = _hc().TUNNEL_KEYS
        assert keys and all(f'healthcheck:{k}'.startswith(PREFIX) for k in keys)


# ---------------------------------------------------------------------------
# AlertManager: dashboard only, and no agent turn during a jam
# ---------------------------------------------------------------------------

def _manager(db):
    cfg = Mock()
    cfg.DB_PATH = db.db_path
    cfg.TOPIC_AI, cfg.FORUM_GROUP_ID, cfg.SUPER_ADMIN_ID = 55, -100, '1'
    cfg.ALERT_TG_MIN_SEVERITY = 'critical'
    mgr = AlertManager(Mock(), cfg, db=db)
    checks = _checks(db)
    for name in ('check_healthcheck', 'check_dpi_handshake_spike', 'check_dpi_handshake_users'):
        mgr.register(checks[name])
    return mgr


class TestTheManager:

    def test_the_prefixes(self):
        assert 'dpi_hsfail_users:' in DASHBOARD_ONLY_PREFIXES
        assert not 'dpi_hsfail_users:RU:AS1'.startswith(DPI_AGENT_PREFIXES)
        assert Alert('k', 'warn', 't').agent is True

    def test_users_failures_stay_on_the_dashboard_with_no_agent(self, db):
        _seed(db, conns=5, fails=500, users_=500, user_ips=[[USER_A, 300], [USER_B, 200]])
        mgr = _manager(db)
        with patch.object(mgr, '_kick_dpi_agent') as kick:
            mgr.run_once()
            mgr.run_once()                                   # min_cycles=2
        assert mgr._state['dpi_hsfail_users:RU:AS31133'].open
        assert 'dpi_hsfail:RU:AS31133' not in mgr._state
        kick.assert_not_called()
        mgr.bot.send_message.assert_not_called()
        assert _rows(db, "SELECT key, severity FROM alert_history") == [
            ('dpi_hsfail_users:RU:AS31133', 'critical')]

    def test_probing_kicks_the_agent_but_not_during_a_jam(self, db):
        _seed(db, conns=5, fails=40, users_=0, probe_ips=[[STRANGER, 40]])
        mgr = _manager(db)
        with patch.object(mgr, '_kick_dpi_agent') as kick:
            mgr.run_once()
            mgr.run_once()
        assert kick.call_count == 1

        db2 = Database(str(Path(db.db_path).with_name('jam.db')))
        _seed(db2, conns=5, fails=40, users_=0, probe_ips=[[STRANGER, 40]])
        _jam(db2)
        mgr2 = _manager(db2)
        with patch.object(mgr2, '_kick_dpi_agent') as kick2:
            mgr2.run_once()
            mgr2.run_once()
        assert mgr2._state['dpi_hsfail:RU:AS31133'].open
        kick2.assert_not_called()
        assert _rows(db2, "SELECT severity FROM alert_history") == [('warn',)]

    def test_a_jam_found_this_tick_is_seen_this_tick(self, db):
        """check_healthcheck runs before the DPI checks: the tunnel finding
        it opens is already in alert_state when they look."""
        from .test_adviser import _report
        _seed(db, conns=5, fails=40, users_=0, probe_ips=[[STRANGER, 40]])
        _report(db, findings=[{'key': 'entry_tunnel:queue', 'severity': 'critical',
                               'layer': 'b', 'title': 'очередь на :19441', 'next': 'ss'}])
        mgr = _manager(db)
        with patch.object(mgr, '_kick_dpi_agent') as kick:
            mgr.run_once()
            mgr.run_once()
        titles = [r[0] for r in _rows(db, "SELECT title FROM alert_history WHERE key = ?",
                                      ('dpi_hsfail:RU:AS31133',))]
        assert titles and titles[0].endswith(' — during an entry tunnel jam')
        kick.assert_not_called()


# ---------------------------------------------------------------------------
# The other readers
# ---------------------------------------------------------------------------

class TestReaders:

    @pytest.mark.asyncio
    async def test_the_dashboard_shows_the_parts(self, db):
        _seed(db, conns=5, fails=45, users_=40, probe_ips=[[STRANGER, 5]],
              user_ips=[[USER_A, 40]])
        _seed(db, ago=10, conns=0, fails=7)                  # an old row: not told apart
        srv = _server(db)
        req = Mock()
        req.query = {'hours': '24'}
        with patch.object(srv, '_validate_admin', return_value={'id': 1}):
            resp = await srv.handle_admin_dpi_metrics(req)
        (ru,) = [c for c in json.loads(resp.text)['countries'] if c['country'] == 'RU']
        (op,) = ru['operators']
        assert (op['handshake_fail_count'], op['hsfail_users'], op['hsfail_unknown'],
                op['hsfail_not_told']) == (52, 40, 5, 7)
        assert ru['hsfail_users'] == 40
        assert USER_A not in json.dumps(op['probe_ips'])

    def test_r3_still_counts_users_failures(self, db):
        """DPIMonitor's R3 asks "can this network's users use Reality?" — a
        user's failures are exactly its signal (the 2026-09-01 flow wipe:
        879 failures, 0 connections), so it keeps the total."""
        from bot.services.dpi_monitor import DPIMonitor
        _seed(db, conns=0, fails=40, users_=40, user_ips=[[USER_A, 40]])
        with db._connect() as conn:
            got = DPIMonitor(db, Mock())._collect_reality_asn(conn, datetime.utcnow())
        assert set(got) == {'AS31133'}

    def test_the_daily_summary_names_the_users_part(self, db):
        from bot.services.notifications import NotificationService
        _seed(db, conns=5, fails=45, users_=40)
        svc = NotificationService.__new__(NotificationService)
        svc.db = db
        svc.bot = Mock()
        svc.config = Mock(TOPIC_AI=55, FORUM_GROUP_ID=-100, HERMES_URL='', OPENCODE_URL='')
        with patch('bot.services.agent_factory.get_agent_url', return_value=''):
            svc._dpi_daily_summary_sync()
        text = svc.bot.send_message.call_args.kwargs['text']
        assert 'hsfail: 45 (у наших юзеров: 40)' in text
        snap = json.loads(_rows(db, "SELECT snapshot_json FROM dpi_reports")[0][0])
        assert snap['totals']['hsfail_users'] == 40


class TestTheSchema:

    def test_a_fresh_db_has_the_columns(self, db):
        cols = [r[1] for r in _rows(db, "PRAGMA table_info(dpi_metrics)")]
        assert cols[-2:] == ['hsfail_users', 'user_ips_json']

    def test_an_old_table_is_migrated(self, tmp_path):
        import sqlite3
        path = str(tmp_path / 'old.db')
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE dpi_metrics (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "snapshot_at TEXT NOT NULL, country TEXT, asn TEXT, as_org TEXT, "
                     "inbound_tag TEXT, conn_count INTEGER NOT NULL DEFAULT 0, "
                     "avg_session_sec REAL, short_session_count INTEGER NOT NULL DEFAULT 0, "
                     "rst_count INTEGER NOT NULL DEFAULT 0, "
                     "handshake_fail_count INTEGER NOT NULL DEFAULT 0, "
                     "probe_ips_json TEXT, reason_buckets_json TEXT)")
        conn.execute("INSERT INTO dpi_metrics (snapshot_at, handshake_fail_count) "
                     "VALUES ('2026-11-19T20:00:00', 9)")
        conn.commit()
        conn.close()
        old = Database(path)
        assert _rows(old, "SELECT handshake_fail_count, hsfail_users, user_ips_json "
                          "FROM dpi_metrics") == [(9, None, None)]


class TestTheDocs:

    def test_the_section_is_there(self):
        doc = next((REPO / 'docs' / 'insights').glob('66-*.md'))
        text = doc.read_text()
        for word in ('dpi_hsfail_users', 'hsfail_users', 'origin', 'USER_ADDR_MEMORY_H',
                     'healthcheck:entry_tunnel'):
            assert word in text
        assert '§66' in (REPO / 'AGENTS.md').read_text()
        skill = (REPO / 'skills' / 'dpi-analysis' / 'SKILL.md').read_text()
        assert 'hsfail_users' in skill
