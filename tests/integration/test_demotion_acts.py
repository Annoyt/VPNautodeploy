"""A demotion that takes effect — IMPROVEMENT_PLAN F, package C.

* C1  the sing-box ``auto`` urltest leaves the demoted servers out (Karing);
      the ``proxy`` selector keeps them for the manual pick.
* C2  Hiddify drops the profile's groups and balances over every outbound;
      hiddify-core 4.1 leaves ``§hide§``-tagged outbounds out of its selector
      and balancers — so for a Hiddify User-Agent the demoted servers, the
      ShadowTLS frontend and the RU-exit carry the marker. Checked on the
      real core (``TestHiddifyCore``, skipped without the built binary).
* C3  a protocol the user's own client stopped passing leaves that user's
      Cascade/Auto/auto, stays in the manual pick and in telemetry.
* C4  DPIMonitor: two dark clients in two networks, nobody passing →
      a global demotion.
* C5  a target demoted again within a day waits twice as long to come back;
      restores have a cap of their own.
* C6  ``/cascade demote|restore`` — hand demotions with an expiry, apart from
      the monitor's store, honoured by every profile.
* C7  a probe DEGRADED is not a global demotion while the protocol's users
      keep connecting at their usual pace.

Level 2: real sqlite, the real builders, the real /sub handler and monitor.
"""

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
from bot.models.user import User
from bot.services import cascade_manual
from bot.services.dpi_monitor import DPIMonitor, empty_signals, empty_state
from bot.services.personal_demotion import personal_dark
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
ALL = ('reality', 'hy2', 'ws', 'stls')
FALLBACK = dict(FALLBACK_NODE_HOST='198.51.100.9', FALLBACK_NODE_PBK='de-pbk',
                FALLBACK_NODE_SID='de01', FALLBACK_NODE_SNI='www.google.com')
NAME = {'reality': 'u1-reality', 'hy2': 'u1-hy2', 'ws': 'u1-cdn-ws', 'stls': 'u1-stls',
        'de': 'u1-de'}
HIDE = SubscriptionService.HIDE_MARKER


def _config(**over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL='https://dash.example.com',
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='reality-pbk', SNI_VALUE='www.bing.com',
        SID_VALUE='0123456789abcdef',
        HY2_HOST='203.0.113.20', HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='443,20000:40000', HY2T_PORT='',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api/v1/forecast',
        WS_SNI='cdn.example.com',
        STLS_HOST='203.0.113.20', STLS_PORT=443, STLS_SNI='www.microsoft.com',
        STLS_VERSION=3, STLS_PASSWORD='stls-pw',
        SS_METHOD='2022-blake3-aes-128-gcm', SS_SERVER_PASSWORD='srv-pw',
        SS_USER_SALT='salt', SUB_MIRROR_URLS='', **FALLBACK,
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def _user(status='paid', lang='ru'):
    return SimpleNamespace(uuid=UUID, email='u1@x', status=status, lang=lang, chat_id='1')


def _names(*protos):
    return [NAME[p] for p in protos]


def _singbox(**kw):
    user = kw.pop('user', None) or _user()
    cfg = SubscriptionService(_config()).build_singbox_config(user, ALL, **kw)
    return cfg, {o['tag']: o for o in cfg['outbounds']}


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


# ------------------------------------------------------------------ C1 -------

class TestSingboxAuto:

    def test_demoted_leave_auto_and_stay_in_the_manual_pick(self):
        _cfg, ob = _singbox(demoted=frozenset({'hy2', 'ws'}))
        assert ob['auto']['outbounds'] == _names('reality', 'stls', 'de')
        assert ob['proxy']['outbounds'] == ['auto'] + _names('reality', 'hy2', 'ws', 'stls', 'de')

    def test_demoting_everything_excludes_nothing(self):
        _cfg, ob = _singbox(demoted=frozenset(ALL))
        assert ob['auto']['outbounds'] == _names(*ALL, 'de')

    @pytest.mark.parametrize('junk', [None, 42, Mock(), {'bogus'}])
    def test_junk_excludes_nothing(self, junk):
        _cfg, ob = _singbox(demoted=junk)
        assert ob['auto']['outbounds'] == _names(*ALL, 'de')


# ------------------------------------------------------------------ C2 -------

class TestHiddifyMarker:

    def test_demoted_frontend_and_ru_exit_are_marked(self):
        cfg, ob = _singbox(demoted=frozenset({'hy2'}), hiddify=True, user=_user(lang='en'))
        assert f'u1-hy2 {HIDE}' in ob and 'u1-hy2' not in ob
        assert f'u1-stls-frontend {HIDE}' in ob
        assert ob['u1-stls']['detour'] == f'u1-stls-frontend {HIDE}'
        assert f'ru-exit {HIDE}' in ob
        assert any(r.get('outbound') == f'ru-exit {HIDE}' for r in cfg['route']['rules'])
        # the groups follow the rename
        assert f'u1-hy2 {HIDE}' in ob['proxy']['outbounds']
        assert f'u1-hy2 {HIDE}' not in ob['auto']['outbounds']
        assert ob['calls']['outbounds'][-1] == f'u1-hy2 {HIDE}'

    def test_other_clients_keep_plain_tags(self):
        _cfg, ob = _singbox(demoted=frozenset({'hy2'}), user=_user(lang='en'))
        assert 'u1-hy2' in ob and 'u1-stls-frontend' in ob and 'ru-exit' in ob
        assert not any(HIDE in t for t in ob)

    def test_nothing_demoted_still_hides_the_frontend(self):
        _cfg, ob = _singbox(hiddify=True)
        assert [t for t in ob if HIDE in t] == [f'u1-stls-frontend {HIDE}']

    def test_everything_demoted_hides_no_server(self):
        _cfg, ob = _singbox(demoted=frozenset(ALL), hiddify=True)
        assert [t for t in ob if HIDE in t] == [f'u1-stls-frontend {HIDE}']


HIDDIFY_CLI = os.environ.get('HIDDIFY_CLI') or '/tmp/nekovpn-test-cache/hiddify-cli-4.1.0'


@pytest.mark.skipif(not os.access(HIDDIFY_CLI, os.X_OK),
                    reason='hiddify-core 4.1.0 CLI not built (see AGENTS.md §49)')
class TestHiddifyCore:
    """The bot's own Hiddify profile through hiddify-core 4.1.0's `build`:
    what ends up in the core's selector and both balancers."""

    def _built(self, tmp_path, **kw):
        cfg, _ob = _singbox(**kw)
        (tmp_path / 'p.json').write_text(json.dumps(cfg))
        r = subprocess.run([HIDDIFY_CLI, 'build', '-c', str(tmp_path / 'p.json'),
                            '-o', 'out.json'], cwd=tmp_path, capture_output=True,
                           text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-500:]
        out = json.loads((tmp_path / 'out.json').read_text())
        return {o['tag']: o for o in out['outbounds']}

    def test_hidden_servers_stay_out_of_every_group(self, tmp_path):
        built = self._built(tmp_path, demoted=frozenset({'hy2'}), hiddify=True,
                            user=_user(lang='en'))
        for group in ('select', 'balance', 'lowest'):
            members = built[group]['outbounds']
            assert not any(HIDE in m for m in members), (group, members)
            assert 'u1-reality' in members
        assert f'u1-hy2 {HIDE}' in built            # still there for detours
        assert f'u1-stls-frontend {HIDE}' in built

    def test_without_the_marker_the_core_balances_everything(self, tmp_path):
        built = self._built(tmp_path, demoted=frozenset({'hy2'}))
        assert {'u1-hy2', 'u1-stls-frontend'} <= set(built['balance']['outbounds'])


class TestSubMarksForHiddifyOnly:

    @pytest.mark.asyncio
    @pytest.mark.parametrize('ua,marked', [('HiddifyNext/4.1.2 (android)', True),
                                           ('Karing/1.2.3', False), ('', False)])
    async def test_by_user_agent(self, db, monkeypatch, ua, marked):
        from bot.services import geoip
        monkeypatch.setattr(geoip, 'lookup', lambda ip: None)
        monkeypatch.setattr(geoip, 'lookup_asn', lambda ip: None)
        monkeypatch.setattr(geoip, 'lookup_city', lambda ip: None)
        db._users.save(User(chat_id='1', username='u1', status='paid', uuid=UUID,
                            email='u1@x', quota_gb=100.0, lang='ru'))
        srv = WebAppServer(_config(), db, xui_service=Mock())
        srv.xui.get_client_traffic = AsyncMock(return_value={})
        srv._schedule_fallback_provisioning = Mock()
        req = Mock()
        req.match_info = {'token': srv.subscription.derive_token(UUID)}
        req.rel_url = SimpleNamespace(query={})
        req.headers = {'User-Agent': ua, 'X-Forwarded-For': '198.51.100.7'}
        req.remote = ''
        tags = {o['tag'] for o in json.loads((await srv.handle_subscription(req)).text)['outbounds']}
        assert (f'u1-stls-frontend {HIDE}' in tags) is marked


# ------------------------------------------------------------------ C3 -------

def _probe_rows(db, chat_id, grp, n, *, minutes_ago=5, step=10):
    now = datetime.utcnow()
    with db._connect() as conn:
        for i in range(n):
            ts = (now - timedelta(minutes=minutes_ago + i * step)).strftime('%Y-%m-%d %H:%M:%S')
            conn.execute("INSERT INTO client_probe (chat_id, grp, ts) VALUES (?, ?, ?)",
                         (chat_id, grp, ts))
        conn.commit()


class TestPersonalDemotion:

    def test_a_protocol_the_client_stopped_passing(self, db):
        _probe_rows(db, '1', 'p-reality', 15)
        _probe_rows(db, '1', 'p-hy2', 2, minutes_ago=60 * 24)     # passed yesterday
        assert personal_dark(db, '1', ALL) == {'hy2'}

    def test_a_closed_app_is_no_signal(self, db):
        _probe_rows(db, '1', 'p-reality', 5)                       # < 2 h of activity
        _probe_rows(db, '1', 'p-hy2', 2, minutes_ago=60 * 24)
        assert personal_dark(db, '1', ALL) == set()

    def test_never_passed_is_not_stopped_passing(self, db):
        _probe_rows(db, '1', 'p-reality', 15)
        assert personal_dark(db, '1', ALL) == set()

    def test_the_reserve_counts_for_nothing(self, db):
        _probe_rows(db, '1', 'p-de', 30)
        _probe_rows(db, '1', 'p-hy2', 2, minutes_ago=60 * 24)
        assert personal_dark(db, '1', ALL) == set()

    def test_back_through_it_lifts_it(self, db):
        _probe_rows(db, '1', 'p-reality', 15)
        _probe_rows(db, '1', 'p-hy2', 2, minutes_ago=60 * 24)
        _probe_rows(db, '1', 'p-hy2', 1)
        assert personal_dark(db, '1', ALL) == set()

    def test_unreadable_demotes_nothing(self, db):
        broken = Mock()
        broken._connect.side_effect = RuntimeError('locked')
        assert personal_dark(broken, '1', ALL) == set()

    @pytest.mark.asyncio
    async def test_the_users_profiles_follow(self, db, monkeypatch):
        from bot.services import geoip
        monkeypatch.setattr(geoip, 'lookup', lambda ip: None)
        monkeypatch.setattr(geoip, 'lookup_asn', lambda ip: None)
        monkeypatch.setattr(geoip, 'lookup_city', lambda ip: None)
        db._users.save(User(chat_id='1', username='u1', status='paid', uuid=UUID,
                            email='u1@x', quota_gb=100.0, lang='ru'))
        db.set_setting('cascade_protocol_order', json.dumps(list(ALL)))
        _probe_rows(db, '1', 'p-reality', 15)
        _probe_rows(db, '1', 'p-hy2', 2, minutes_ago=60 * 24)
        srv = WebAppServer(_config(), db, xui_service=Mock())
        srv.xui.get_client_traffic = AsyncMock(return_value={})
        srv._schedule_fallback_provisioning = Mock()

        async def fetch(query):
            req = Mock()
            req.match_info = {'token': srv.subscription.derive_token(UUID)}
            req.rel_url = SimpleNamespace(query=query)
            req.headers = {'User-Agent': 'FlClash/v0.8.90', 'X-Forwarded-For': '198.51.100.7'}
            req.remote = ''
            return json.loads((await srv.handle_subscription(req)).text)

        clash = await fetch({'format': 'clash'})
        groups = {g['name']: g for g in clash['proxy-groups']}
        assert 'u1-hy2' not in groups['Cascade']['proxies']
        assert 'u1-hy2' in groups['VPN']['proxies']
        assert groups['Calls']['proxies'][-1] == 'u1-hy2'
        assert 'p-hy2' in clash['proxy-providers']            # telemetry keeps checking
        singbox = await fetch({})
        auto = next(o for o in singbox['outbounds'] if o['tag'] == 'auto')
        assert 'u1-hy2' not in auto['outbounds']


# ------------------------------------------------------------------ C4 -------

def _seed_client(db, chat_id, asn, protos, status='paid',
                 seen=('reality', 'hy2', 'ws', 'stls')):
    with db._connect() as conn:
        conn.execute("INSERT OR REPLACE INTO users (chat_id, username, status, last_asn) "
                     "VALUES (?,?,?,?)", (chat_id, chat_id, status, asn))
        conn.commit()
    for p in seen:                      # every protocol passed a day ago
        _probe_rows(db, chat_id, f'p-{p}', 1, minutes_ago=24 * 60)
    for p in protos:
        _probe_rows(db, chat_id, f'p-{p}', 3)


class TestClientsAcrossNetworks:
    """R6's eligibility applies (the protocol must be in the client's
    profile), so the monitor gets a config that builds every protocol."""

    def test_two_networks_nobody_through_is_global(self, db):
        _seed_client(db, '1', 'AS31133', ('reality', 'hy2', 'stls'))
        _seed_client(db, '2', 'AS56981', ('reality', 'hy2', 'stls'))
        sig = DPIMonitor(db, _config(), None).collect_signals()
        assert set(sig['client_dark_global']) == {'ws'}
        assert 'ws' in sig['client_dark_global']['ws'] and 'AS31133' in sig['client_dark_global']['ws']

    def test_one_network_is_r6_territory(self, db):
        _seed_client(db, '1', 'AS31133', ('reality', 'hy2', 'stls'))
        _seed_client(db, '2', 'AS31133', ('reality', 'hy2', 'stls'))
        assert DPIMonitor(db, _config(), None).collect_signals()['client_dark_global'] == {}

    def test_one_client_through_it_is_enough_to_stay(self, db):
        _seed_client(db, '1', 'AS31133', ('reality', 'hy2', 'stls'))
        _seed_client(db, '2', 'AS56981', ('reality', 'hy2', 'stls'))
        _seed_client(db, '3', 'AS8359', ('reality', 'ws'))
        assert DPIMonitor(db, _config(), None).collect_signals()['client_dark_global'] == {}

    def test_it_demotes_globally_after_two_evaluations(self):
        sig = empty_signals()
        sig['probe'].update(stale=False, measured=['reality', 'hy2', 'ws', 'stls'])
        sig['client_dark_global'] = {'ws': 'клиенты: 0/2 по ws'}
        state, now = empty_state(), datetime(2026, 11, 10, 12)
        state, changes = DPIMonitor.evaluate(sig, state, now)
        assert changes == []
        state, changes = DPIMonitor.evaluate(sig, state, now + timedelta(minutes=10))
        assert [(c.scope, c.protocol, c.reason) for c in changes] == [
            ('global', 'ws', 'client_dark_global')]


# ------------------------------------------------------------------ C5 -------

class TestBackoff:

    def _cycle(self, state, now, *, dark, quiet_runs):
        """Demote ws (two dark evaluations), then quiet evaluations."""
        sig = empty_signals()
        sig['probe'].update(stale=False, measured=['reality', 'ws'])
        restored_after = None
        for _ in range(2):
            sig['probe']['dark'] = {'ws': 'dark'} if dark else {}
            state, _c = DPIMonitor.evaluate(sig, state, now)
            now += timedelta(minutes=10)
        sig['probe']['dark'] = {}
        for i in range(quiet_runs):
            state, changes = DPIMonitor.evaluate(sig, state, now)
            now += timedelta(minutes=10)
            if changes and changes[0].action == 'restore':
                restored_after = i + 1
                break
        return state, now, restored_after

    def test_each_repeat_within_a_day_doubles_the_wait(self):
        state, now = empty_state(), datetime(2026, 11, 10, 8)
        waits = []
        for _ in range(4):
            state, now, after = self._cycle(state, now, dark=True, quiet_runs=60)
            waits.append(after)
            now += timedelta(minutes=40)              # past the change gap
        assert waits == [6, 12, 24, 48]

    def test_the_wait_stops_growing_at_eight_hours(self):
        from bot.services.dpi_monitor import restore_after
        now = datetime(2026, 11, 10, 12)
        entry = {'demotions': [(now - timedelta(hours=h)).isoformat() for h in range(8)]}
        assert restore_after(entry, now) == 48
        entry = {'demotions': [(now - timedelta(hours=30)).isoformat()]}
        assert restore_after(entry, now) == 6         # older than a day: forgotten

    def test_a_day_later_it_starts_over(self):
        state, now = empty_state(), datetime(2026, 11, 10, 8)
        state, now, first = self._cycle(state, now, dark=True, quiet_runs=60)
        now += timedelta(hours=25)
        state, now, again = self._cycle(state, now, dark=True, quiet_runs=60)
        assert (first, again) == (6, 6)


# ------------------------------------------------------------------ C6 -------

class TestHandDemotions:

    def test_demote_applies_everywhere_and_expires(self, db):
        now = datetime.utcnow()
        cascade_manual.demote(db, 'hy2', None, timedelta(hours=6), by='admin:1', now=now)
        cascade_manual.demote(db, 'reality', 'AS31133', timedelta(hours=1), by='admin:1', now=now)
        assert MK.get_auto_demotions(db, None) == {'hy2'}
        assert MK.get_auto_demotions(db, 'as31133') == {'hy2', 'reality'}
        later = now + timedelta(hours=2)
        assert cascade_manual.demoted_for(db, 'AS31133', later) == {'hy2'}
        assert cascade_manual.demoted_for(db, 'AS31133', now + timedelta(hours=7)) == set()

    def test_the_monitor_never_touches_them(self, db):
        # hy2 demoted by hand AND by the monitor's probe rule; hy2's probes
        # are measured and healthy, so the monitor restores ITS entry over
        # the run — the hand entry must outlive that
        cascade_manual.demote(db, 'hy2', None, timedelta(hours=6), by='admin:1')
        start = datetime.utcnow()
        db.set_setting('cascade_auto', json.dumps({'global': {'hy2': {
            'since': (start - timedelta(hours=2)).isoformat(), 'reason': 'probe_dark',
            'evidence': 'x'}}, 'asn': {}}))
        monitor = DPIMonitor(db, Mock(), None)
        for i in range(8):
            now = start + timedelta(minutes=10 * i)
            with db._connect() as conn:
                conn.execute("DELETE FROM outbound_health")
                conn.commit()
            for tag in ('reality', 'hy2', 'ws', 'stls'):
                _seed_probes(db, tag, 9, now=now)
            monitor.run_once(now=now)
        assert 'hy2' not in json.loads(db.get_setting('cascade_auto') or '{}').get('global', {})
        assert cascade_manual.load(db)['global']['hy2']['by'] == 'admin:1'
        assert MK.get_auto_demotions(db, None) == {'hy2'}

    def test_cascade_order_sinks_them(self, db):
        db.set_setting('cascade_protocol_order', json.dumps(list(ALL)))
        cascade_manual.demote(db, 'reality', None, timedelta(hours=6), by='admin:1')
        order = MK.get_cascade_order(db)
        assert order[-1] == 'reality' and order[:3] == ('hy2', 'ws', 'stls')

    @pytest.mark.parametrize('raw,ttl', [('30m', timedelta(minutes=30)), ('6h', timedelta(hours=6)),
                                         ('2d', timedelta(days=2)), ('99d', timedelta(days=7)),
                                         ('0h', None), ('6x', None), ('', None)])
    def test_ttl(self, raw, ttl):
        assert cascade_manual.parse_ttl(raw) == ttl

    def test_bad_json_demotes_nothing(self, db):
        db.set_setting('cascade_manual', '{nope')
        assert MK.get_auto_demotions(db, 'AS1') == set()


class TestCascadeCommand:

    @pytest.fixture
    def handler(self, db):
        from bot.handlers.admin.ops import AdminOpsMixin
        h = AdminOpsMixin.__new__(AdminOpsMixin)
        h.db = db
        h.config = SimpleNamespace(DPI_MONITOR_ENABLED=True)
        h.sent = []
        h._send = lambda **kw: h.sent.append(kw['text'])
        h._caller_admin_id = lambda: '1652899'
        return h

    def _run(self, handler, *args):
        handler.show_cascade('chat', list(args))
        return handler.sent[-1]

    def test_demote_restore_and_overview(self, handler, db):
        text = self._run(handler, 'demote', 'hy2', 'AS31133', '2h')
        assert 'hy2 → в конец каскада' in text and 'AS31133' in text
        assert cascade_manual.demoted_for(db, 'AS31133') == {'hy2'}
        assert 'Ручные понижения' in self._run(handler)
        text = self._run(handler, 'restore', 'hy2', 'AS31133')
        assert 'снято' in text and cascade_manual.demoted_for(db, 'AS31133') == set()
        with db._connect() as conn:
            acts = [tuple(r) for r in conn.execute(
                "SELECT action, target_id FROM admin_actions ORDER BY id")]
        assert acts == [('cascade_manual_demote', 'asn:AS31133:hy2'),
                        ('cascade_manual_restore', 'asn:AS31133:hy2')]

    def test_default_ttl_is_six_hours(self, handler, db):
        self._run(handler, 'demote', 'ws')
        until = datetime.fromisoformat(cascade_manual.load(db)['global']['ws']['until'])
        assert abs((until - datetime.utcnow()) - timedelta(hours=6)) < timedelta(minutes=2)

    @pytest.mark.parametrize('args', [('demote',), ('demote', 'xhttpx'), ('demote', 'hy2', 'soon')])
    def test_bad_input_changes_nothing(self, handler, db, args):
        self._run(handler, *args)
        assert cascade_manual.load(db) == {'global': {}, 'asn': {}}

    def test_reset_clears_hand_demotions_too(self, handler, db):
        self._run(handler, 'demote', 'hy2')
        text = self._run(handler, 'reset')
        assert 'hy2 (глобально, вручную)' in text
        assert cascade_manual.load(db) == {'global': {}, 'asn': {}}


# ------------------------------------------------------------------ C7 -------

def _seed_conns(db, tag, per_snapshot, *, recent=None, days=7.2):
    now = datetime.utcnow()
    t = now - timedelta(days=days)
    rows = []
    while t < now - timedelta(minutes=1):
        recent_block = (now - t) < timedelta(hours=2)
        n = recent if (recent is not None and recent_block) else per_snapshot
        rows.append((t.isoformat(), '*TUNNEL*', tag, n))
        t += timedelta(minutes=5)
    with db._connect() as conn:
        conn.executemany("INSERT INTO dpi_metrics (snapshot_at, country, inbound_tag, conn_count) "
                         "VALUES (?,?,?,?)", rows)
        conn.commit()


def _seed_probes(db, tag, ok_per_run, now=None):
    from bot.services import probe_rules as pr
    now = now or datetime.utcnow()
    with db._connect() as conn:
        for run in range(3):
            ts = (now - timedelta(minutes=5 + run * 15)).isoformat()
            for i, d in enumerate(pr.PROBE_TARGETS):
                ok = i < ok_per_run
                conn.execute("INSERT INTO outbound_health (outbound_tag, target_domain, status,"
                             " latency_ms, ts) VALUES (?,?,?,?,?)",
                             (tag, d, 'ok' if ok else 'error', 120 if ok else 900, ts))
        conn.commit()


class TestSlowProbesNormalUsers:

    def _probe(self, db):
        for tag in ('reality', 'hy2', 'stls'):
            _seed_probes(db, tag, 9)
        _seed_probes(db, 'ws', 1)                         # 3/30 → DEGRADED
        return DPIMonitor(db, Mock(), None).collect_signals()['probe']

    def test_users_at_their_usual_pace_veto_it(self, db):
        _seed_conns(db, 'cf-ws', 10)                      # 240 every 2 h
        probe = self._probe(db)
        assert 'ws' not in probe['degraded']
        assert 'как обычно' in probe['vetoed']['ws']

    def test_users_gone_too_keep_it(self, db):
        _seed_conns(db, 'cf-ws', 10, recent=1)            # 24 of 240
        probe = self._probe(db)
        assert 'ws' in probe['degraded'] and probe['vetoed'] == {}

    def test_a_quiet_inbound_vetoes_nothing(self, db):
        _seed_conns(db, 'cf-ws', 1)                       # 24 a block < 50
        assert 'ws' in self._probe(db)['degraded']

    def test_a_dark_probe_is_never_vetoed(self, db):
        _seed_conns(db, 'cf-ws', 10)
        for tag in ('reality', 'hy2', 'stls'):
            _seed_probes(db, tag, 9)
        now = datetime.utcnow()
        from bot.services import probe_rules as pr
        with db._connect() as conn:
            for run in range(3):
                ts = (now - timedelta(minutes=5 + run * 15)).isoformat()
                for d in pr.PROBE_TARGETS:
                    conn.execute("INSERT INTO outbound_health (outbound_tag, target_domain,"
                                 " status, latency_ms, ts) VALUES ('ws', ?, 'timeout', NULL, ?)",
                                 (d, ts))
            conn.commit()
        probe = DPIMonitor(db, Mock(), None).collect_signals()['probe']
        assert 'ws' in probe['dark']
        assert probe['vetoed'] == {}
        # and the monitor demotes it on the second evaluation
        monitor = DPIMonitor(db, Mock(), None)
        monitor.run_once(now=now)
        changes = monitor.run_once(now=now + timedelta(minutes=10))
        assert [(c.protocol, c.action) for c in changes] == [('ws', 'demote')]


# ------------------------------------------------------- review of C -------

class TestANewProtocolIsNotDead:

    def test_a_freshly_enabled_protocol_is_not_demoted(self, db):
        # hy2t switched on: every telemetry client lacks it, none ever passed it
        cfg = _config(HY2T_PORT='8402', HY2T_HOP_PORTS='8402,40001:50000')
        db.set_setting('cascade_protocol_order', json.dumps(list(ALL) + ['hy2t']))
        _seed_client(db, '1', 'AS31133', ('reality', 'hy2', 'ws', 'stls'))
        _seed_client(db, '2', 'AS56981', ('reality', 'hy2', 'ws', 'stls'))
        assert DPIMonitor(db, cfg, None).collect_signals()['client_dark_global'] == {}

    def test_one_it_passed_this_week_is(self, db):
        cfg = _config(HY2T_PORT='8402', HY2T_HOP_PORTS='8402,40001:50000')
        db.set_setting('cascade_protocol_order', json.dumps(list(ALL) + ['hy2t']))
        for cid, asn in (('1', 'AS31133'), ('2', 'AS56981')):
            _seed_client(db, cid, asn, ('reality', 'hy2', 'ws', 'stls'),
                         seen=('reality', 'hy2', 'ws', 'stls', 'hy2t'))
        assert set(DPIMonitor(db, cfg, None).collect_signals()['client_dark_global']) == {'hy2t'}

    def test_everyone_offline_is_no_verdict(self, db):
        state = empty_state()
        state['auto']['global'] = {'ws': {'since': datetime.utcnow().isoformat(),
                                          'reason': 'client_dark_global', 'evidence': 'x'}}
        now = datetime.utcnow()
        for i in range(1, 9):
            state, changes = DPIMonitor.evaluate(empty_signals(), state, now + timedelta(minutes=10 * i))
            assert changes == []
        assert 'ws' in state['auto']['global']

    def test_a_client_passing_it_again_restores_it(self, db):
        state = empty_state()
        state['auto']['global'] = {'ws': {'since': datetime.utcnow().isoformat(),
                                          'reason': 'client_dark_global', 'evidence': 'x'}}
        sig = empty_signals()
        sig['client_alive'] = {'global': ['ws'], 'asn': {}}
        now = datetime.utcnow() + timedelta(hours=1)
        changes = []
        for i in range(8):
            state, changes = DPIMonitor.evaluate(sig, state, now + timedelta(minutes=10 * i))
            if changes:
                break
        assert [(c.protocol, c.action) for c in changes] == [('ws', 'restore')]


def _seed_window_values(db, tag, per_day_value, *, current, days=8, missing=()):
    """dpi_metrics every 5 min: ``current`` per snapshot in the last 45 min,
    ``per_day_value[d]`` on day d back; no rows at all on ``missing`` days."""
    now = datetime.utcnow()
    t = now - timedelta(days=days)
    rows = []
    while t < now - timedelta(minutes=1):
        ago = now - t
        d = int(ago.total_seconds() // 86400)
        if d not in missing:
            n = current if ago < timedelta(minutes=45) else per_day_value.get(d, 10)
            rows.append((t.isoformat(), '*TUNNEL*', tag, n))
        t += timedelta(minutes=5)
    with db._connect() as conn:
        conn.executemany("INSERT INTO dpi_metrics (snapshot_at, country, inbound_tag, conn_count) "
                         "VALUES (?,?,?,?)", rows)
        conn.commit()


class TestVetoReview:

    def _probe(self, db, tag='ws'):
        for t in ('reality', 'hy2', 'stls', 'ws'):
            _seed_probes(db, t, 1 if t == tag else 9)
        return DPIMonitor(db, Mock(), None).collect_signals()['probe']

    def test_a_reconnect_storm_is_not_as_usual(self, db):
        _seed_conns(db, 'cf-ws', 10, recent=30)           # three times usual
        probe = self._probe(db)
        assert 'ws' in probe['degraded'] and probe['vetoed'] == {}

    def test_hy2_is_never_vetoed(self, db):
        # its count is the bot's rollup of auth allows: a storm raises it.
        # The exit reports every window (ss2022), so only the tag decides.
        _seed_conns(db, 'hy2-8400', 10)
        _seed_conns(db, 'ss2022', 10)
        probe = self._probe(db, 'hy2')
        assert 'hy2' in probe['degraded'] and probe['vetoed'] == {}

    def test_unreported_baseline_days_are_skipped_not_zero(self, db):
        # usual 54 / 108 / 108 / 108 on days 1-4, days 5-7 unreported, now 45:
        # skipped, the median is 108 and 45 is below half of it; as zeros
        # the median would be 54 and the veto would hold
        _seed_window_values(db, 'cf-ws', {1: 6, 2: 12, 3: 12, 4: 12}, current=5,
                            missing=(5, 6, 7))
        probe = self._probe(db)
        assert 'ws' in probe['degraded'] and probe['vetoed'] == {}

    def test_too_few_reported_days_is_no_veto(self, db):
        _seed_window_values(db, 'cf-ws', {}, current=10, missing=(1, 2, 3, 4))
        probe = self._probe(db)
        assert probe['vetoed'] == {}

    def test_an_unreported_window_is_no_veto(self, db):
        _seed_conns(db, 'cf-ws', 10)
        with db._connect() as conn:
            cut = (datetime.utcnow() - timedelta(minutes=45)).isoformat()
            conn.execute("DELETE FROM dpi_metrics WHERE snapshot_at >= ?", (cut,))
            conn.commit()
        probe = self._probe(db)
        assert probe['vetoed'] == {}

    def test_the_window_is_the_probes_45_minutes(self, db):
        # users came back 40 min ago after an hour away: the last 45 min
        # are as usual — vetoed (a 2-h block would still read half-empty)
        now = datetime.utcnow()
        _seed_conns(db, 'cf-ws', 10)
        with db._connect() as conn:
            lo = (now - timedelta(minutes=120)).isoformat()
            hi = (now - timedelta(minutes=46)).isoformat()
            conn.execute("UPDATE dpi_metrics SET conn_count = 0 WHERE snapshot_at BETWEEN ? AND ?",
                         (lo, hi))
            conn.commit()
        probe = self._probe(db)
        assert 'ws' in probe['vetoed']


class TestHiddifyKeepsTwoServers:

    def _demo(self, demoted):
        user = _user(status='demo')
        cfg = SubscriptionService(_config()).build_singbox_config(
            user, ('ws', 'stls', 'hy2'), demoted=frozenset(demoted), hiddify=True)
        return {o['tag'] for o in cfg['outbounds']}

    def test_one_demoted_of_three_is_hidden(self):
        tags = self._demo({'hy2'})
        assert f'u1-hy2 {HIDE}' in tags

    def test_two_demoted_of_three_hides_none(self):
        tags = self._demo({'hy2', 'ws'})
        assert 'u1-hy2' in tags and 'u1-cdn-ws' in tags
        assert [t for t in tags if HIDE in t] == [f'u1-stls-frontend {HIDE}']

    def test_the_reserve_does_not_count_as_visible(self):
        _cfg, ob = _singbox(demoted=frozenset({'reality', 'hy2', 'ws'}), hiddify=True)
        assert 'u1-reality' in ob and 'u1-hy2' in ob and 'u1-cdn-ws' in ob


class TestHiddifyGetsNoPersonalDemotion:

    @pytest.mark.asyncio
    async def test_the_users_telemetry_is_another_devices(self, db, monkeypatch):
        from bot.services import geoip
        monkeypatch.setattr(geoip, 'lookup', lambda ip: None)
        monkeypatch.setattr(geoip, 'lookup_asn', lambda ip: None)
        monkeypatch.setattr(geoip, 'lookup_city', lambda ip: None)
        db._users.save(User(chat_id='1', username='u1', status='paid', uuid=UUID,
                            email='u1@x', quota_gb=100.0, lang='ru'))
        db.set_setting('cascade_protocol_order', json.dumps(list(ALL)))
        _probe_rows(db, '1', 'p-reality', 15)
        _probe_rows(db, '1', 'p-hy2', 2, minutes_ago=60 * 24)
        srv = WebAppServer(_config(), db, xui_service=Mock())
        srv.xui.get_client_traffic = AsyncMock(return_value={})
        srv._schedule_fallback_provisioning = Mock()
        threads = []
        import threading
        from bot.services import personal_demotion as pd
        real = pd.personal_dark

        def spy(*a, **kw):
            threads.append(threading.current_thread().name)
            return real(*a, **kw)
        monkeypatch.setattr(pd, 'personal_dark', spy)

        async def fetch(ua):
            req = Mock()
            req.match_info = {'token': srv.subscription.derive_token(UUID)}
            req.rel_url = SimpleNamespace(query={})
            req.headers = {'User-Agent': ua, 'X-Forwarded-For': '198.51.100.7'}
            req.remote = ''
            return {o['tag'] for o in json.loads((await srv.handle_subscription(req)).text)['outbounds']}

        assert f'u1-hy2 {HIDE}' not in await fetch('HiddifyNext/4.1.2 (android)')
        assert 'u1-hy2' in await fetch('HiddifyNext/4.1.2 (android)')
        await fetch('Karing/1.2.3')                     # personal demotion applies here
        assert threads and threading.main_thread().name not in threads


class TestPersonalCountsChecksNotRows:

    def test_three_protocols_for_half_an_hour_is_not_two_hours(self, db):
        # the review's repro: four checks x three other protocols = 12 rows
        for p in ('reality', 'ws', 'stls'):
            _probe_rows(db, '1', f'p-{p}', 4, minutes_ago=1)
        _probe_rows(db, '1', 'p-hy2', 2, minutes_ago=60 * 24)
        assert personal_dark(db, '1', ALL) == set()

    def test_twelve_slots_across_protocols_is(self, db):
        _probe_rows(db, '1', 'p-reality', 6, minutes_ago=1)
        _probe_rows(db, '1', 'p-ws', 6, minutes_ago=61)
        _probe_rows(db, '1', 'p-hy2', 2, minutes_ago=60 * 24)
        assert personal_dark(db, '1', ALL) == {'hy2'}


class TestHandDemotionStore:

    def test_an_offset_until_is_read_not_dropped(self, db):
        until = (datetime.utcnow() + timedelta(hours=3)).isoformat() + '+00:00'
        db.set_setting('cascade_manual', json.dumps(
            {'global': {'hy2': {'since': 'x', 'until': until, 'by': 'admin:1'}}, 'asn': {}}))
        assert cascade_manual.demoted_for(db, None) == {'hy2'}
        far = (datetime.utcnow() - timedelta(hours=1)).isoformat() + '-03:00'
        db.set_setting('cascade_manual', json.dumps(
            {'global': {'hy2': {'since': 'x', 'until': far, 'by': 'admin:1'}}, 'asn': {}}))
        assert cascade_manual.demoted_for(db, None) == {'hy2'}   # 2 h ahead in UTC

    def test_a_locked_read_never_overwrites_the_store(self, db, monkeypatch):
        # the read hits a lock, the write a moment later would go through
        import sqlite3
        cascade_manual.demote(db, 'ws', 'AS1', timedelta(hours=6), by='admin:1')
        stored = db.get_setting('cascade_manual')
        real = db._connect
        calls = []

        def locked_once():
            calls.append(1)
            if len(calls) == 1:
                raise sqlite3.OperationalError('database is locked')
            return real()
        monkeypatch.setattr(db, '_connect', locked_once)
        with pytest.raises(cascade_manual.ManualStoreError):
            cascade_manual.demote(db, 'hy2', None, timedelta(hours=6), by='admin:1')
        monkeypatch.setattr(db, '_connect', real)
        assert db.get_setting('cascade_manual') == stored

    def test_a_failed_write_is_not_reported_as_done(self, db, monkeypatch):
        monkeypatch.setattr(db, 'set_setting', lambda *a: False)
        with pytest.raises(cascade_manual.ManualStoreError):
            cascade_manual.demote(db, 'hy2', None, timedelta(hours=6), by='admin:1')


class TestCascadeDemoteArguments:

    @pytest.fixture
    def handler(self, db):
        from bot.handlers.admin.ops import AdminOpsMixin
        h = AdminOpsMixin.__new__(AdminOpsMixin)
        h.db = db
        h.config = SimpleNamespace(DPI_MONITOR_ENABLED=True)
        h.sent = []
        h._send = lambda **kw: h.sent.append(kw['text'])
        h._caller_admin_id = lambda: '1652899'
        return h

    def _run(self, handler, *args):
        handler.show_cascade('chat', list(args))
        return handler.sent[-1]

    @pytest.mark.parametrize('args', [('demote', 'hy2', '30'), ('demote', 'hy2', 'AS1', 'AS2'),
                                      ('demote', 'hy2', '2h', '6h'), ('restore', 'hy2', 'AS1', 'x'),
                                      ('restore', 'hy2', '31133')])
    def test_ambiguous_input_changes_nothing(self, handler, db, args):
        if args[0] == 'restore':
            cascade_manual.demote(db, 'hy2', 'AS1', timedelta(hours=6), by='admin:1')
        before = db.get_setting('cascade_manual')
        text = self._run(handler, *args)
        assert text.startswith('⚠️')
        assert db.get_setting('cascade_manual') == before

    def test_a_failed_write_answers_and_logs_nothing(self, handler, db, monkeypatch):
        monkeypatch.setattr(db, 'set_setting', lambda *a: False)
        text = self._run(handler, 'demote', 'hy2', '2h')
        assert text.startswith('❌')
        with db._connect() as conn:
            assert conn.execute("SELECT COUNT(*) FROM admin_actions").fetchone()[0] == 0

    def test_the_asn_view_lists_hand_demotions(self, handler, db):
        cascade_manual.demote(db, 'hy2', 'AS31133', timedelta(hours=6), by='admin:1')
        cascade_manual.demote(db, 'ws', 'AS8359', timedelta(hours=6), by='admin:1')
        text = self._run(handler, 'AS31133')
        assert 'Ручные понижения' in text and 'hy2 (AS31133)' in text
        assert 'AS8359' not in text


class TestKitForHiddify:

    def test_the_json_is_built_as_for_hiddify(self, db, monkeypatch):
        from bot.services import sos
        db._users.save(User(chat_id='1', username='u1', status='paid', uuid=UUID,
                            email='u1@x', quota_gb=100.0, lang='ru', last_asn='AS31133'))
        db.set_setting('cascade_protocol_order', json.dumps(list(ALL)))
        _probe_rows(db, '1', 'p-reality', 15)
        _probe_rows(db, '1', 'p-hy2', 2, minutes_ago=60 * 24)       # personal: hy2
        user = db.get_user('1')
        kit = sos.build_kit(db, _config(), user)
        files = {name: body for name, body in kit} if isinstance(kit, list) else kit
        body = next(v for k, v in files.items() if str(k).endswith('.json'))
        tags = {o['tag'] for o in json.loads(body)['outbounds']}
        assert f'u1-stls-frontend {HIDE}' in tags
        assert 'u1-hy2' in tags                          # no personal demotion there


class TestStateRoundTrip:

    def test_the_days_demotions_survive_a_save(self, db):
        mon = DPIMonitor(db, Mock(), None)
        state = empty_state()
        stamps = [(datetime.utcnow() - timedelta(hours=h)).isoformat() for h in (3, 1)]
        state['targets']['global:ws'] = {'bad': 0, 'good': 2, 'rule': 'probe_dark',
                                         'last_change': stamps[-1], 'demotions': stamps}
        mon.save_state(state)
        assert mon.load_state()['targets']['global:ws']['demotions'] == stamps
