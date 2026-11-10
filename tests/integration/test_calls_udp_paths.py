"""Calls and UDP — IMPROVEMENT_PLAN F, package A.

* A1  paid users get a second UDP path: the calls group holds hy2 first,
      then Reality and the DE reserve (UDP inside their TCP stream), Turbo
      last. Clash: a fallback group that also takes the channels' copies of
      those servers. sing-box: a urltest whose tolerance keeps the first
      live server in list order.
* A2  what DPIMonitor demotes for the user's network goes to the end of
      the calls group (hy2 demoted → Reality first). Demo profiles have no
      second path: their calls group stays hy2 alone.
* A3  the ShadowTLS server is TCP-only in the sing-box profile, so a client
      that drops the groups (Hiddify) never balances UDP onto it.
* A4  /api/hy2/auth reads users off the event loop, writes its audit row
      after the answer, and decides from the last row a key resolved to
      when bot.db cannot be read (an unknown key still fails closed).

Level 2: real builders; the /sub, kit and hy2 handlers on a REAL sqlite
(Database in tmp_path). Only geo lookups and the panel are stubbed.
"""

import asyncio
import json
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
ALL = ('reality', 'hy2', 'ws', 'stls')
FALLBACK = dict(FALLBACK_NODE_HOST='198.51.100.9', FALLBACK_NODE_PBK='de-pbk',
                FALLBACK_NODE_SID='de01', FALLBACK_NODE_SNI='www.google.com')
NAME = {'reality': 'u1-reality', 'hy2': 'u1-hy2', 'hy2t': 'u1-hy2t',
        'ws': 'u1-cdn-ws', 'stls': 'u1-stls', 'de': 'u1-de'}


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
        SS_USER_SALT='salt', SUB_MIRROR_URLS='',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


TURBO = dict(HY2T_PORT='8402', HY2T_HOP_PORTS='', HY2T_UP_MBPS='', HY2T_DOWN_MBPS='')


def _user(status='paid'):
    return SimpleNamespace(uuid=UUID, email='u1@x', status=status, lang='ru', chat_id='1')


def _names(*protos):
    return [NAME[p] for p in protos]


def _singbox_calls(protocols=ALL, status='paid', config=None, **kw):
    svc = SubscriptionService(config or _config(**FALLBACK))
    cfg = svc.build_singbox_config(_user(status), protocols, **kw)
    return next((o for o in cfg['outbounds'] if o['tag'] == 'calls'), None), cfg


def _clash_groups(protocols=ALL, status='paid', config=None, **kw):
    svc = SubscriptionService(config or _config(**FALLBACK))
    cfg = json.loads(svc.build_clash_config(_user(status), protocols, **kw))
    return {g['name']: g for g in cfg['proxy-groups']}, cfg


# ------------------------------------------------------------- A1 / A2 -------

class TestSingboxCalls:

    def test_paid_gets_reality_and_the_reserve_after_hy2(self):
        calls, _ = _singbox_calls()
        assert calls['type'] == 'urltest'
        assert calls['outbounds'] == _names('hy2', 'reality', 'de')

    def test_tolerance_keeps_the_first_live_server(self):
        calls, _ = _singbox_calls()
        tol = calls['tolerance']
        assert tol == SubscriptionService._CALLS_URLTEST_TOLERANCE_MS
        # larger than any delay sing-box can measure (15-s test timeout)...
        assert tol > 15000
        # ...and clear of the uint16 wrap in "delay + tolerance"
        assert 15000 + tol < 65536

    def test_turbo_is_the_last_resort(self):
        calls, _ = _singbox_calls(ALL + ('hy2t',), config=_config(**FALLBACK, **TURBO))
        assert calls['outbounds'] == _names('hy2', 'reality', 'de', 'hy2t')

    def test_demo_keeps_hy2_alone(self):
        # demo profiles carry neither Reality nor the reserve
        calls, _ = _singbox_calls(('ws', 'stls', 'hy2'), status='demo')
        assert calls['outbounds'] == _names('hy2')

    def test_no_hy2_leaves_reality_and_the_reserve(self):
        calls, _ = _singbox_calls(('reality', 'ws', 'stls'))
        assert calls['outbounds'] == _names('reality', 'de')

    @pytest.mark.parametrize('demoted,expected', [
        ({'hy2'}, ('reality', 'de', 'hy2t', 'hy2')),
        ({'hy2', 'hy2t'}, ('reality', 'de', 'hy2', 'hy2t')),
        ({'reality'}, ('hy2', 'de', 'hy2t', 'reality')),
        ({'ws', 'stls'}, ('hy2', 'reality', 'de', 'hy2t')),
        ({'reality', 'hy2', 'hy2t'}, ('de', 'hy2', 'reality', 'hy2t')),
    ])
    def test_demoted_go_to_the_end(self, demoted, expected):
        calls, _ = _singbox_calls(ALL + ('hy2t',), config=_config(**FALLBACK, **TURBO),
                                  demoted=frozenset(demoted))
        assert calls['outbounds'] == _names(*expected)

    def test_demo_has_no_second_path_even_when_hy2_is_demoted(self):
        calls, _ = _singbox_calls(('ws', 'stls', 'hy2'), status='demo',
                                  demoted=frozenset({'hy2'}))
        assert calls['outbounds'] == _names('hy2')

    @pytest.mark.parametrize('demoted', [None, 42, Mock(), {'bogus', 7}, frozenset()])
    def test_unusable_demoted_moves_nothing(self, demoted):
        calls, _ = _singbox_calls(demoted=demoted)
        assert calls['outbounds'] == _names('hy2', 'reality', 'de')

    def test_cascade_groups_are_untouched_by_demotion(self):
        # package C territory: here only the calls group reorders
        _, plain = _singbox_calls()
        _, demoted = _singbox_calls(demoted=frozenset({'hy2'}))
        pick = lambda cfg, tag: next(o for o in cfg['outbounds'] if o['tag'] == tag)
        for tag in ('proxy', 'auto'):
            assert pick(plain, tag) == pick(demoted, tag)

    def test_udp_still_routes_to_calls(self):
        _, cfg = _singbox_calls()
        udp = [r for r in cfg['route']['rules'] if r.get('network') == 'udp'
               and 'port' not in r]
        assert udp and all(r['outbound'] == 'calls' for r in udp)


class TestClashCalls:

    def test_fallback_in_priority_order(self):
        g, _ = _clash_groups()
        calls = g['Calls']
        assert calls['type'] == 'fallback'
        assert calls['proxies'] == _names('hy2', 'reality', 'de')
        assert 'tolerance' not in calls

    def test_takes_only_the_channels_copies_of_its_servers(self):
        g, cfg = _clash_groups(config=_config(**FALLBACK, **TURBO,
                                              SUB_MIRROR_URLS='https://m1.example.net'),
                               protocols=('reality', 'hy2', 'ws', 'stls'))
        calls = g['Calls']
        assert calls['use'] == ['emergency', 'mirror-1']
        rx = re.compile(calls['filter'])
        copies = [f'[{label}] {n}' for label in ('E', 'M1')
                  for n in _names('hy2', 'reality', 'de')]
        others = [f'[E] {n}' for n in _names('ws', 'stls', 'hy2t')]
        assert all(rx.search(n) for n in copies)
        assert not any(rx.search(n) for n in others)
        # the telemetry providers stay out of every group
        assert not any(u.startswith('p-') for u in calls['use'])

    def test_no_public_address_no_channels(self):
        g, _ = _clash_groups(config=_config(**FALLBACK, WEBAPP_URL=''))
        assert 'use' not in g['Calls'] and 'filter' not in g['Calls']
        assert g['Calls']['proxies'] == _names('hy2', 'reality', 'de')

    def test_demoted_hy2_puts_reality_first(self):
        g, _ = _clash_groups(demoted=frozenset({'hy2'}))
        assert g['Calls']['proxies'] == _names('reality', 'de', 'hy2')
        # and the filter still covers every call server, demoted included
        assert re.compile(g['Calls']['filter']).search('[E] u1-hy2')

    def test_demo_keeps_hy2_alone(self):
        g, _ = _clash_groups(('ws', 'stls', 'hy2'), status='demo',
                             demoted=frozenset({'hy2'}))
        assert g['Calls']['proxies'] == _names('hy2')

    def test_rules_send_udp_to_calls(self):
        _, cfg = _clash_groups()
        assert 'NETWORK,UDP,Calls' in cfg['rules']


# ----------------------------------------------------------------- A3 --------

class TestShadowTlsIsTcpOnly:

    def test_singbox_ss_outbound_declares_tcp(self):
        _, cfg = _singbox_calls()
        ss = next(o for o in cfg['outbounds'] if o['tag'] == 'u1-stls')
        assert ss['type'] == 'shadowsocks' and ss['network'] == 'tcp'
        front = next(o for o in cfg['outbounds'] if o['tag'] == 'u1-stls-frontend')
        assert 'network' not in front

    def test_clash_form_is_udp_false(self):
        _, cfg = _clash_groups()
        ss = next(p for p in cfg['proxies'] if p['name'] == 'u1-stls')
        assert ss['udp'] is False

    def test_stls_never_in_calls(self):
        calls, _ = _singbox_calls()
        assert 'u1-stls' not in calls['outbounds']


# ------------------------------------------- /sub and the kit on sqlite ------

@pytest.fixture
def geo(monkeypatch):
    from bot.services import geoip
    state = {'country': None, 'asn': None}
    monkeypatch.setattr(geoip, 'lookup',
                        lambda ip: (state['country'], '') if state['country'] else None)
    monkeypatch.setattr(geoip, 'lookup_asn',
                        lambda ip: (state['asn'], 'org') if state['asn'] else None)
    monkeypatch.setattr(geoip, 'lookup_city', lambda ip: None)
    return state


def _db(tmp_path, status='paid', last_asn='AS31133'):
    db = Database(str(tmp_path / 'bot.db'))
    db._users.save(User(chat_id='1', username='u1', status=status, uuid=UUID,
                        email='u1@x', quota_gb=100.0, lang='ru', last_asn=last_asn))
    db.set_setting('cascade_protocol_order', json.dumps(['reality', 'hy2', 'ws', 'stls']))
    return db


def _demote(db, asn, *protos):
    since = '2026-11-10T10:00:00'
    db.set_setting('cascade_auto', json.dumps({
        'global': {},
        'asn': {asn: {p: {'since': since, 'reason': 'udp_storm_asn'} for p in protos}},
    }))


async def _sub(srv, query, ip='198.51.100.77'):
    req = Mock()
    req.match_info = {'token': srv.subscription.derive_token(UUID)}
    req.rel_url = SimpleNamespace(query=query)
    req.headers = {'User-Agent': 'Karing/1.2', 'X-Forwarded-For': ip}
    req.remote = ''
    return await srv.handle_subscription(req)


def _server(db):
    srv = WebAppServer(_config(**FALLBACK), db, xui_service=Mock())
    srv.xui.get_client_traffic = AsyncMock(return_value={'upload': 1, 'download': 2})
    srv._schedule_fallback_provisioning = Mock()
    return srv


class TestSubPassesTheDemotions:

    @pytest.mark.asyncio
    async def test_singbox_profile_follows_the_request_network(self, tmp_path, geo):
        db = _db(tmp_path)
        _demote(db, 'AS31133', 'hy2')
        geo.update(country='RU', asn='AS31133')
        resp = await _sub(_server(db), {})
        assert resp.status == 200
        cfg = json.loads(resp.text)
        calls = next(o for o in cfg['outbounds'] if o['tag'] == 'calls')
        assert calls['outbounds'] == _names('reality', 'de', 'hy2')

    @pytest.mark.asyncio
    async def test_other_network_keeps_hy2_first(self, tmp_path, geo):
        db = _db(tmp_path)
        _demote(db, 'AS31133', 'hy2')
        geo.update(country='RU', asn='AS12389')
        cfg = json.loads((await _sub(_server(db), {})).text)
        calls = next(o for o in cfg['outbounds'] if o['tag'] == 'calls')
        assert calls['outbounds'] == _names('hy2', 'reality', 'de')

    @pytest.mark.asyncio
    async def test_clash_profile_follows_it_too(self, tmp_path, geo):
        db = _db(tmp_path)
        _demote(db, 'AS31133', 'hy2')
        geo.update(country='RU', asn='AS31133')
        cfg = json.loads((await _sub(_server(db), {'format': 'clash'})).text)
        calls = next(g for g in cfg['proxy-groups'] if g['name'] == 'Calls')
        assert calls['proxies'] == _names('reality', 'de', 'hy2')

    @pytest.mark.asyncio
    async def test_own_address_falls_back_to_the_stored_network(self, tmp_path, geo):
        # a refresh through the tunnel leaves by our node: the stored
        # last_asn orders the profile, as for the Clash form
        db = _db(tmp_path, last_asn='AS31133')
        _demote(db, 'AS31133', 'hy2')
        cfg = json.loads((await _sub(_server(db), {}, ip='203.0.113.20')).text)
        calls = next(o for o in cfg['outbounds'] if o['tag'] == 'calls')
        assert calls['outbounds'][0] == 'u1-reality'


class TestKit:

    def test_both_kit_profiles_follow_the_stored_network(self, tmp_path):
        from bot.services import sos
        db = _db(tmp_path, last_asn='AS31133')
        _demote(db, 'AS31133', 'hy2')
        user = db._users.get_by_id('1')
        files = dict(sos.build_kit(db, _config(**FALLBACK), user))
        singbox = json.loads(files[sos.KIT_JSON_NAME])
        clash = json.loads(files[sos.KIT_YAML_NAME])
        calls = next(o for o in singbox['outbounds'] if o['tag'] == 'calls')
        group = next(g for g in clash['proxy-groups'] if g['name'] == 'Calls')
        assert calls['outbounds'] == group['proxies'] == _names('reality', 'de', 'hy2')


# ----------------------------------------------------------------- A4 --------

def _hy2_server(tmp_path, status='paid', expiry=None):
    db = Database(str(tmp_path / 'bot.db'))
    db._users.save(User(chat_id='42', username='u42', status=status, uuid=UUID,
                        email='u42@x', quota_gb=100.0, lang='ru',
                        subscription_expiry=expiry))
    config = _config()
    config.is_admin = Mock(return_value=False)
    srv = WebAppServer(config, db, xui_service=Mock())
    # the panel quota reading: warm and under quota, so only the users
    # row decides
    srv._hy2_quota_cache['u42@x'] = (time.monotonic(),
                                     {'upload': 1, 'download': 1, 'total': 0, 'enable': True})
    srv.xui.get_client_traffic = AsyncMock(return_value={'upload': 1, 'download': 1})
    return srv, db


async def _auth(srv, turbo=False, auth=UUID):
    req = Mock()
    req.json = AsyncMock(return_value={'auth': auth, 'addr': '130.49.146.10:5555'})
    handler = srv.handle_hy2t_auth if turbo else srv.handle_hy2_auth
    return json.loads((await handler(req)).text)


async def _drain(srv):
    while srv._bg_tasks:
        await asyncio.gather(*list(srv._bg_tasks), return_exceptions=True)


def _log(db):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT chat_id, decision FROM hy2_auth_log ORDER BY id").fetchall()]


def _break_db(srv):
    """Every read of the users row fails, as on a locked or broken bot.db."""
    def boom(_password):
        raise RuntimeError('database is locked')
    srv._hy2_lookup_user = boom


class TestHy2AuthWhenTheDbFails:

    @pytest.mark.asyncio
    async def test_a_key_seen_before_still_gets_in(self, tmp_path):
        srv, _db = _hy2_server(tmp_path)
        assert (await _auth(srv))['ok'] is True          # a good read
        _break_db(srv)
        assert (await _auth(srv)) == {'ok': True, 'id': 'u42@x'}
        await _drain(srv)

    @pytest.mark.asyncio
    async def test_an_unknown_key_fails_closed(self, tmp_path):
        srv, _db = _hy2_server(tmp_path)
        _break_db(srv)
        assert (await _auth(srv))['ok'] is False
        await _drain(srv)

    @pytest.mark.asyncio
    async def test_the_remembered_row_runs_the_same_gates(self, tmp_path):
        # a demo key remembered from the main instance must not open Turbo
        srv, _db = _hy2_server(tmp_path, status='demo')
        assert (await _auth(srv))['ok'] is True
        _break_db(srv)
        assert (await _auth(srv, turbo=True))['ok'] is False
        assert (await _auth(srv))['ok'] is True
        await _drain(srv)

    @pytest.mark.asyncio
    async def test_an_expiry_passed_since_is_honoured(self, tmp_path):
        srv, _db = _hy2_server(tmp_path, expiry='2099-01-01T00:00:00')
        assert (await _auth(srv))['ok'] is True
        stamp, row = srv._hy2_auth_rows[UUID]
        srv._hy2_auth_rows[UUID] = (stamp, row[:3] + ('2000-01-01T00:00:00',))
        _break_db(srv)
        assert (await _auth(srv))['ok'] is False
        await _drain(srv)

    @pytest.mark.asyncio
    async def test_a_row_too_old_is_not_used(self, tmp_path):
        srv, _db = _hy2_server(tmp_path)
        assert (await _auth(srv))['ok'] is True
        stamp, row = srv._hy2_auth_rows[UUID]
        srv._hy2_auth_rows[UUID] = (time.monotonic() - srv.HY2_AUTH_ROW_MAX_S - 1, row)
        _break_db(srv)
        assert (await _auth(srv))['ok'] is False
        await _drain(srv)

    @pytest.mark.asyncio
    async def test_a_revoked_key_is_forgotten(self, tmp_path):
        srv, db = _hy2_server(tmp_path)
        assert (await _auth(srv))['ok'] is True
        with db._connect() as conn:                       # revoke: uuid cleared
            conn.execute("UPDATE users SET uuid = NULL WHERE chat_id = '42'")
            conn.commit()
        assert (await _auth(srv))['ok'] is False
        assert UUID not in srv._hy2_auth_rows
        _break_db(srv)
        assert (await _auth(srv))['ok'] is False
        await _drain(srv)

    @pytest.mark.asyncio
    async def test_a_status_change_reaches_the_memory(self, tmp_path):
        srv, db = _hy2_server(tmp_path)
        assert (await _auth(srv))['ok'] is True
        with db._connect() as conn:
            conn.execute("UPDATE users SET status = 'banned' WHERE chat_id = '42'")
            conn.commit()
        assert (await _auth(srv))['ok'] is False          # good read: banned
        _break_db(srv)
        assert (await _auth(srv))['ok'] is False          # remembered: banned
        await _drain(srv)


class TestHy2AuthOffTheAnswerPath:

    @pytest.mark.asyncio
    async def test_audit_row_is_written_after_the_answer(self, tmp_path):
        srv, db = _hy2_server(tmp_path)
        release = threading.Event()
        real = srv._hy2_record

        def held(*a):
            release.wait(5)
            real(*a)
        srv._hy2_record = held
        assert (await _auth(srv))['ok'] is True           # answered while held
        assert _log(db) == []
        release.set()
        await _drain(srv)
        assert _log(db) == [('42', 'allow')]

    @pytest.mark.asyncio
    async def test_denies_are_audited_too(self, tmp_path):
        srv, db = _hy2_server(tmp_path)
        await _auth(srv, auth='not-a-key')
        await _drain(srv)
        assert _log(db) == [(None, 'deny')]

    @pytest.mark.asyncio
    async def test_a_failing_audit_write_changes_no_answer(self, tmp_path):
        srv, db = _hy2_server(tmp_path)
        with db._connect() as conn:
            conn.execute("DROP TABLE hy2_auth_log")
            conn.commit()
        assert (await _auth(srv))['ok'] is True
        await _drain(srv)

    @pytest.mark.asyncio
    async def test_a_slow_lookup_does_not_hold_the_event_loop(self, tmp_path):
        srv, _db = _hy2_server(tmp_path)
        real = srv._hy2_lookup_user

        def slow(password):
            time.sleep(0.4)
            return real(password)
        srv._hy2_lookup_user = slow
        task = asyncio.ensure_future(_auth(srv))
        t0 = time.monotonic()
        await asyncio.sleep(0.05)
        assert time.monotonic() - t0 < 0.3      # the loop ran meanwhile
        assert not task.done()
        assert (await task)['ok'] is True
        await _drain(srv)
