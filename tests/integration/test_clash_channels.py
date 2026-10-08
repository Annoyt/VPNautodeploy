"""FlClash profile as a remote control — IMPROVEMENT_PLAN E3/E4/E5/E6/E20.

* E3  ``Cascade``: a fallback group in the EFFECTIVE cascade order (the
      DE reserve last), the default of ``VPN``; what DPIMonitor demotes
      for the user's network is left out of Cascade/Auto, kept in VPN.
* E4  the groups health-check gstatic (our /probe sits behind exit's
      Caddy — the DE reserve must not look dead while exit is down);
      ``/probe/<token>/<provider>`` is the providers' health check: the
      endpoint answers 204 and leaves one ``client_probe`` heartbeat per
      (user, provider) a minute.
* E5  ``SUB_MIRROR_URLS`` → one ``mirror-<n>`` proxy-provider each.
* E20 ``emergency`` provider on the main domain, refreshed every 10 min.
* E6  under lockdown Cascade starts with ws and DNS rides the tunnel.

Level 2: the /sub and /probe handlers run on a REAL sqlite (Database in
tmp_path) with the real builders; only geo lookups and the panel quota
read are stubbed.
"""

import asyncio
import json
import logging
import re
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import lockdown as lockdown_mod
from bot.services.subscription import CLASH_PROBE_GROUPS, SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
WEB = 'https://dash.example.com'
GSTATIC = 'https://www.gstatic.com/generate_204'
ALL = ('reality', 'hy2', 'ws', 'stls')
FALLBACK = dict(FALLBACK_NODE_HOST='198.51.100.9', FALLBACK_NODE_PBK='de-pbk',
                FALLBACK_NODE_SID='de01', FALLBACK_NODE_SNI='www.google.com')
NAME = {'reality': 'u1-reality', 'hy2': 'u1-hy2', 'hy2t': 'u1-hy2t',
        'ws': 'u1-cdn-ws', 'stls': 'u1-stls', 'de': 'u1-de'}


def _config(**over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL=WEB,
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


def _user(status='demo'):
    return SimpleNamespace(uuid=UUID, email='u1@x', status=status, lang='ru', chat_id='1')


def _token():
    return SubscriptionService(_config()).derive_token(UUID)


def _build(protocols=ALL, user=None, config=None, **kw):
    svc = SubscriptionService(config or _config())
    return json.loads(svc.build_clash_config(user or _user(), protocols, **kw))


def _groups(cfg):
    return {g['name']: g for g in cfg['proxy-groups']}


def _names(*protos):
    return [NAME[p] for p in protos]


# ---------------------------------------------------------------- E3 ------

class TestCascadeGroup:

    @pytest.mark.parametrize('order', [
        ('reality', 'hy2', 'ws', 'stls'),
        ('ws', 'stls', 'reality', 'hy2'),
        ('stls', 'hy2', 'reality', 'ws'),
    ])
    def test_fallback_in_cascade_order(self, order):
        g = _groups(_build(order))
        assert g['Cascade']['type'] == 'fallback'
        assert g['Cascade']['proxies'] == _names(*order)
        assert g['Cascade']['interval'] == 180

    def test_de_reserve_is_last(self):
        cfg = _build(('ws', 'stls', 'reality', 'hy2'), user=_user('paid'),
                     config=_config(**FALLBACK))
        assert _groups(cfg)['Cascade']['proxies'] == _names('ws', 'stls', 'reality', 'hy2', 'de')

    def test_vpn_defaults_to_cascade_and_lists_every_server(self):
        cfg = _build(('ws', 'stls', 'reality', 'hy2'))
        names = [p['name'] for p in cfg['proxies']]
        assert names == _names('ws', 'stls', 'reality', 'hy2')
        vpn = _groups(cfg)['VPN']
        # a select group's first member is its default
        assert vpn['type'] == 'select' and vpn['proxies'] == ['Cascade', 'Auto'] + names

    def test_auto_has_the_cascade_set(self):
        g = _groups(_build(('stls', 'ws', 'hy2', 'reality')))
        assert g['Auto']['type'] == 'url-test' and g['Auto']['tolerance'] == 50
        assert g['Auto']['proxies'] == g['Cascade']['proxies']
        assert g['Auto']['interval'] == 180

    def test_demoted_left_out_of_cascade_and_auto_only(self):
        cfg = _build(ALL, demoted=frozenset({'reality', 'hy2'}))
        g = _groups(cfg)
        assert g['Cascade']['proxies'] == _names('ws', 'stls')
        assert g['Auto']['proxies'] == _names('ws', 'stls')
        # the manual pick and the calls group keep them
        assert g['VPN']['proxies'] == ['Cascade', 'Auto'] + _names(*ALL)
        assert g['Calls']['proxies'] == _names('hy2')
        assert [p['name'] for p in cfg['proxies']] == _names(*ALL)

    def test_demoted_keeps_reserve_last(self):
        cfg = _build(ALL, user=_user('paid'), config=_config(**FALLBACK),
                     demoted={'stls'})
        assert _groups(cfg)['Cascade']['proxies'] == _names('reality', 'hy2', 'ws', 'de')

    @pytest.mark.parametrize('demoted', [
        frozenset(), set(), None, {'xhttp', 'bogus', 7}, 42, Mock(),
    ])
    def test_nothing_usable_demoted_excludes_nothing(self, demoted):
        g = _groups(_build(ALL, demoted=demoted))
        assert g['Cascade']['proxies'] == _names(*ALL)
        assert 'exclude-filter' not in g['Cascade']

    @pytest.mark.parametrize('status,protocols,config,expected', [
        ('demo', ('ws', 'stls', 'hy2'), {}, ('ws', 'stls', 'hy2')),
        # paid: the DE reserve alone would be left — keep everything
        ('paid', ALL, FALLBACK, ALL + ('de',)),
    ])
    def test_every_protocol_demoted_excludes_nothing(self, status, protocols, config,
                                                     expected):
        cfg = _build(protocols, user=_user(status), config=_config(**config),
                     demoted=set(protocols))
        g = _groups(cfg)
        assert g['Cascade']['proxies'] == _names(*expected)
        assert 'exclude-filter' not in g['Cascade']

    def test_exclude_filter_drops_provider_copies_of_demoted_only(self):
        g = _groups(_build(ALL, demoted={'hy2', 'ws'}))
        rx = g['Cascade']['exclude-filter']
        assert g['Auto']['exclude-filter'] == rx
        assert 'exclude-filter' not in g['VPN']
        hit = lambda name: re.search(rx, name) is not None  # noqa: E731
        for name in ('[E] u1-hy2', '[M1] u1-cdn-ws', 'u1-hy2', 'u1-cdn-ws'):
            assert hit(name), name
        for name in ('[E] u1-hy2t', 'u1-hy2t', '[E] u1-reality', '[M2] u1-stls',
                     '[E] u1-de', 'u1-hy2-x'):
            assert not hit(name), name

    def test_no_exclude_filter_without_providers(self):
        g = _groups(_build(ALL, config=_config(WEBAPP_URL=''), demoted={'hy2'}))
        assert g['Cascade']['proxies'] == _names('reality', 'ws', 'stls')
        assert 'use' not in g['Cascade'] and 'exclude-filter' not in g['Cascade']

    def test_empty_cascade_unchanged(self):
        cfg = _build(())
        assert cfg['proxy-groups'] == [{'name': 'VPN', 'type': 'select',
                                        'proxies': ['DIRECT']}]
        assert 'proxy-providers' not in cfg


# ---------------------------------------------------------------- E4 ------

class TestProbeUrl:

    def test_groups_check_gstatic_not_our_endpoint(self):
        # /probe sits behind exit's Caddy: with exit down the DE reserve
        # would look dead too and Cascade could not fail over to it.
        cfg = _build(user=_user('paid'), config=_config(
            **FALLBACK, SUB_MIRROR_URLS='https://m1.example.net'))
        g = _groups(cfg)
        assert {g[n]['url'] for n in ('Cascade', 'Auto', 'Calls')} == {GSTATIC}
        assert g['Calls']['interval'] == 180
        assert 'url' not in g['VPN']        # select: never health-checked
        for grp in g.values():
            assert 'expected-status' not in grp   # any answer = alive
        assert '/probe/' not in json.dumps(cfg['proxy-groups'])

    def test_providers_probe_our_endpoint(self):
        p = _build(config=_config(SUB_MIRROR_URLS='https://m1.example.net'))['proxy-providers']
        tok = _token()
        assert p['emergency']['health-check']['url'] == f'{WEB}/probe/{tok}/emergency'
        assert p['mirror-1']['health-check']['url'] == f'{WEB}/probe/{tok}/mirror-1'

    def test_trailing_slash_in_webapp_url(self):
        p = _build(config=_config(WEBAPP_URL=WEB + '/'))['proxy-providers']
        assert p['emergency']['health-check']['url'] == f'{WEB}/probe/{_token()}/emergency'

    def test_gstatic_without_webapp_url(self):
        cfg = _build(config=_config(WEBAPP_URL=''))
        g = _groups(cfg)
        assert {g[n]['url'] for n in ('Cascade', 'Auto', 'Calls')} == {GSTATIC}
        assert 'proxy-providers' not in cfg

    def test_every_probe_url_is_accepted_by_the_endpoint(self):
        config = _config(SUB_MIRROR_URLS='https://m1.example.net,https://m2.example.org')
        cfg = _build(config=config)
        urls = [p['health-check']['url'] for p in cfg['proxy-providers'].values()]
        groups = SubscriptionService(config).probe_groups()
        assert len(urls) == 3
        for url in urls:
            prefix, group = url.rsplit('/', 1)
            assert prefix == f'{WEB}/probe/{_token()}' and group in groups, url
        assert groups == {'emergency', 'mirror-1', 'mirror-2'}

    def test_probe_groups_are_the_emitted_ones_only(self):
        # the provider names — the groups (cascade, auto, calls) are gone
        assert CLASH_PROBE_GROUPS == ('emergency',)
        assert SubscriptionService(_config()).probe_groups() == {'emergency'}
        two = SubscriptionService(_config(
            SUB_MIRROR_URLS='https://m1.example.net,https://m2.example.org')).probe_groups()
        assert two == {'emergency', 'mirror-1', 'mirror-2'}

    def test_singbox_profile_keeps_gstatic(self):
        sb = SubscriptionService(_config()).build_singbox_config(_user(), ALL)
        tests = [o for o in sb['outbounds'] if o['type'] == 'urltest']
        assert tests and {o['url'] for o in tests} == {GSTATIC}
        assert '/probe/' not in json.dumps(sb)


# ------------------------------------------------------------- E20 / E5 ----

class TestProviders:

    def test_emergency_provider(self):
        p = _build()['proxy-providers']
        tok = _token()
        assert list(p) == ['emergency']
        assert p['emergency'] == {
            'type': 'http',
            'url': f'{WEB}/sub/{tok}?format=clash-proxies&channel=emergency',
            'path': './providers/emergency.yaml',
            'interval': 600,
            'proxy': 'DIRECT',
            'health-check': {'enable': True, 'url': f'{WEB}/probe/{tok}/emergency',
                             'interval': 600},
            'override': {'additional-prefix': '[E] '},
        }

    def test_groups_take_the_providers(self):
        g = _groups(_build(config=_config(SUB_MIRROR_URLS='https://m1.example.net')))
        for name in ('VPN', 'Cascade', 'Auto'):
            assert g[name]['use'] == ['emergency', 'mirror-1'], name
        assert 'use' not in g['Calls']

    @pytest.mark.parametrize('raw,mirrors', [
        ('', []),
        ('https://m1.example.net', ['https://m1.example.net']),
        ('https://m1.example.net/, https://m2.example.org',
         ['https://m1.example.net', 'https://m2.example.org']),
    ])
    def test_mirrors(self, raw, mirrors):
        p = _build(config=_config(SUB_MIRROR_URLS=raw))['proxy-providers']
        tok = _token()
        assert list(p) == ['emergency'] + [f'mirror-{n}' for n in range(1, len(mirrors) + 1)]
        for n, base in enumerate(mirrors, 1):
            assert p[f'mirror-{n}'] == {
                'type': 'http',
                'url': f'{base}/sub/{tok}?format=clash-proxies',
                'path': f'./providers/mirror-{n}.yaml',
                'interval': 3600,
                'proxy': 'DIRECT',
                'health-check': {'enable': True,
                                 'url': f'{WEB}/probe/{tok}/mirror-{n}',
                                 'interval': 600},
                'override': {'additional-prefix': f'[M{n}] '},
            }

    def test_mirror_list_is_normalised(self):
        svc = SubscriptionService(_config(SUB_MIRROR_URLS=(
            ' https://a.example/ ,, https://b.example,https://a.example,'
            'ftp://c.example, c.example ,HTTPS://D.example')))
        assert svc.mirror_bases() == ['https://a.example', 'https://b.example',
                                      'HTTPS://D.example']
        assert SubscriptionService(SimpleNamespace(BOT_TOKEN='t')).mirror_bases() == []

    def test_mirrors_without_main_domain(self):
        cfg = _build(config=_config(WEBAPP_URL='', SUB_MIRROR_URLS='https://m1.example.net'))
        p = cfg['proxy-providers']
        assert list(p) == ['mirror-1']                  # no emergency without a main domain
        assert p['mirror-1']['health-check']['url'] == GSTATIC
        assert _groups(cfg)['Cascade']['use'] == ['mirror-1']

    def test_no_channels_without_a_token(self):
        user = SimpleNamespace(uuid=None, email='u1@x', status='demo', lang='ru', chat_id='1')
        svc = SubscriptionService(_config(SUB_MIRROR_URLS='https://m1.example.net'))
        cfg = json.loads(svc.build_clash_config(user, ALL))
        assert 'proxy-providers' not in cfg
        assert _groups(cfg)['Cascade']['url'] == GSTATIC

    def test_provider_body_is_the_profile_proxies(self):
        for status, config in (('demo', _config()), ('paid', _config(**FALLBACK))):
            svc = SubscriptionService(config)
            profile = json.loads(svc.build_clash_config(_user(status), ALL,
                                                        demoted={'reality'}))
            body = json.loads(svc.build_clash_proxies(_user(status), ALL))
            assert body == {'proxies': profile['proxies']}

    def test_settings_and_compose_carry_sub_mirror_urls(self, monkeypatch):
        from pathlib import Path
        from bot.config.settings import Settings
        monkeypatch.setenv('SUB_MIRROR_URLS', ' https://m1.example.net ')
        assert Settings().SUB_MIRROR_URLS == 'https://m1.example.net'
        monkeypatch.delenv('SUB_MIRROR_URLS')
        assert Settings().SUB_MIRROR_URLS == ''
        # AGENTS.md §6: os.getenv sees the CONTAINER env, not .env
        compose = (Path(__file__).resolve().parents[2] / 'docker-compose.yml').read_text()
        assert '- SUB_MIRROR_URLS=${SUB_MIRROR_URLS:-}' in compose


# ------------------------------------------- /sub handler on a real sqlite ----

@pytest.fixture
def geo(monkeypatch):
    """Request-time geo the /sub handler resolves (no mmdb in tests)."""
    from bot.services import geoip
    state = {'country': None, 'asn': None}
    monkeypatch.setattr(geoip, 'lookup',
                        lambda ip: (state['country'], '') if state['country'] else None)
    monkeypatch.setattr(geoip, 'lookup_asn',
                        lambda ip: (state['asn'], 'org') if state['asn'] else None)
    monkeypatch.setattr(geoip, 'lookup_city', lambda ip: None)
    return state


def _server(tmp_path, status='demo', last_asn=None, config=None):
    db = Database(str(tmp_path / 'bot.db'))
    db._users.save(User(chat_id='1', username='u1', status=status, uuid=UUID,
                        email='u1@x', quota_gb=10.0, lang='ru', last_asn=last_asn))
    db.set_setting('cascade_protocol_order', json.dumps(['reality', 'hy2', 'ws', 'stls']))
    srv = WebAppServer(config or _config(**FALLBACK), db, xui_service=Mock())
    srv.xui.get_client_traffic = AsyncMock(return_value={'upload': 1, 'download': 2})
    return srv, db


async def _sub(srv, query, ip='198.51.100.77'):
    req = Mock()
    req.match_info = {'token': srv.subscription.derive_token(UUID)}
    req.rel_url = SimpleNamespace(query=query)
    req.headers = {'User-Agent': 'FlClash/v0.8.90', 'X-Forwarded-For': ip}
    req.remote = ''
    return await srv.handle_subscription(req)


def _rows(db, sql, *args):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]


class TestClashProxiesFormat:

    @pytest.mark.asyncio
    async def test_only_proxies_and_provider_headers(self, tmp_path, geo):
        srv, db = _server(tmp_path, status='paid')
        resp = await _sub(srv, {'format': 'clash-proxies', 'channel': 'emergency'})
        assert resp.status == 200
        body = json.loads(resp.text)
        assert list(body) == ['proxies']
        profile = json.loads((await _sub(srv, {'format': 'clash'})).text)
        assert body['proxies'] == profile['proxies']
        assert [p['name'] for p in body['proxies']] == _names(*ALL, 'de')
        assert resp.content_type == 'text/plain'
        assert resp.headers['cache-control'] == 'no-store'
        for profile_header in ('content-disposition', 'profile-title',
                               'profile-update-interval', 'subscription-userinfo'):
            assert profile_header not in resp.headers

    @pytest.mark.asyncio
    async def test_read_only(self, tmp_path, geo):
        srv, db = _server(tmp_path, status='paid')
        geo.update(country='NL', asn='AS31133')
        await _sub(srv, {'format': 'clash-proxies'})
        assert _rows(db, "SELECT last_asn, last_country FROM users") == [(None, None)]
        assert _rows(db, "SELECT count(*) FROM sub_fetches") == [(0,)]
        srv.xui.get_client_traffic.assert_not_awaited()
        # ...unlike the profile fetch, which owns those side effects
        await _sub(srv, {'format': 'clash'})
        assert _rows(db, "SELECT last_asn, last_country FROM users") == [('AS31133', 'NL')]
        assert _rows(db, "SELECT count(*) FROM sub_fetches") == [(1,)]
        srv.xui.get_client_traffic.assert_awaited()

    @pytest.mark.asyncio
    async def test_ordered_by_the_request_network(self, tmp_path, geo):
        srv, db = _server(tmp_path)
        db.set_setting('cascade_by_asn', json.dumps({'AS31133': ['stls', 'hy2', 'ws']}))
        geo.update(asn='AS31133')
        body = json.loads((await _sub(srv, {'format': 'clash-proxies'})).text)
        assert [p['name'] for p in body['proxies']] == _names('stls', 'hy2', 'ws')

    @pytest.mark.asyncio
    async def test_channel_is_logged(self, tmp_path, geo, caplog):
        srv, _db = _server(tmp_path)
        with caplog.at_level(logging.INFO, logger='bot.core.web_server'):
            await _sub(srv, {'format': 'clash-proxies', 'channel': 'Emergency<script>'})
        assert 'clash-proxies channel=emergencyscript for 1' in caplog.text

    @pytest.mark.asyncio
    async def test_same_gates_as_sub(self, tmp_path, geo):
        srv, db = _server(tmp_path)
        req = Mock()
        req.match_info = {'token': '0' * 32}
        req.rel_url = SimpleNamespace(query={'format': 'clash-proxies'})
        req.headers = {}
        req.remote = ''
        assert (await srv.handle_subscription(req)).status == 404
        user = db._users.get_by_id('1')
        user.status = 'banned'
        db._users.save(user)
        assert (await _sub(srv, {'format': 'clash-proxies'})).status == 410


class TestProfileDemotions:

    AUTO = {'global': {'ws': {'since': '2026-10-08T10:00:00', 'reason': 'probe_dark'}},
            'asn': {'AS31133': {'reality': {'since': '2026-10-08T10:00:00',
                                            'reason': 'reality_hsfail'}}}}

    async def _groups(self, srv, ip='198.51.100.77'):
        cfg = json.loads((await _sub(srv, {'format': 'clash'}, ip=ip)).text)
        return _groups(cfg)

    @pytest.mark.asyncio
    async def test_request_asn_demotions_leave_cascade(self, tmp_path, geo):
        srv, db = _server(tmp_path, status='paid')
        db.set_setting('cascade_auto', json.dumps(self.AUTO))
        geo.update(asn='AS31133')
        g = await self._groups(srv)
        assert g['Cascade']['proxies'] == _names('hy2', 'stls', 'de')
        assert g['Auto']['proxies'] == _names('hy2', 'stls', 'de')
        # get_cascade_order already sank the demoted ones to the tail
        assert g['VPN']['proxies'] == ['Cascade', 'Auto'] + _names('hy2', 'stls',
                                                                   'reality', 'ws', 'de')

    @pytest.mark.asyncio
    async def test_falls_back_to_stored_asn(self, tmp_path, geo):
        srv, db = _server(tmp_path, status='paid', last_asn='AS31133')
        db.set_setting('cascade_auto', json.dumps(self.AUTO))
        g = await self._groups(srv)              # no request geo
        assert g['Cascade']['proxies'] == _names('hy2', 'stls', 'de')

    @pytest.mark.asyncio
    async def test_other_network_gets_global_only(self, tmp_path, geo):
        srv, db = _server(tmp_path, status='paid')
        db.set_setting('cascade_auto', json.dumps(self.AUTO))
        geo.update(asn='AS8359')
        g = await self._groups(srv)
        assert g['Cascade']['proxies'] == _names('reality', 'hy2', 'stls', 'de')

    @pytest.mark.asyncio
    async def test_bad_cascade_auto_excludes_nothing(self, tmp_path, geo):
        srv, db = _server(tmp_path, status='paid')
        db.set_setting('cascade_auto', '{not json')
        g = await self._groups(srv)
        assert g['Cascade']['proxies'] == _names(*ALL, 'de')


# ---------------------------------------------------------------- E6 ------

class TestLockdownProfile:

    @pytest.mark.asyncio
    @pytest.mark.parametrize('status,normal,expected', [
        ('paid', ('reality', 'hy2', 'ws', 'stls', 'de'), ('ws', 'stls', 'reality', 'hy2', 'de')),
        ('demo', ('hy2', 'ws', 'stls'), ('ws', 'stls', 'hy2')),
    ])
    async def test_ws_first_and_dns_through_the_tunnel(self, tmp_path, geo, status,
                                                       normal, expected):
        srv, db = _server(tmp_path, status=status)
        before = json.loads((await _sub(srv, {'format': 'clash'})).text)
        assert _groups(before)['Cascade']['proxies'] == _names(*normal)
        assert before['dns']['nameserver'] == ['77.88.8.8', '77.88.8.1']

        lockdown_mod.set_mode(db, 'on', by='admin:1', reason='test')
        cfg = json.loads((await _sub(srv, {'format': 'clash'})).text)
        g = _groups(cfg)
        assert g['Cascade']['proxies'] == _names(*expected)
        assert g['Auto']['proxies'] == _names(*expected)
        assert cfg['dns']['nameserver'] == ['https://1.1.1.1/dns-query#VPN']
        # the 10-min channel serves the lockdown order too
        body = json.loads((await _sub(srv, {'format': 'clash-proxies'})).text)
        assert [p['name'] for p in body['proxies']] == _names(*expected)


# ------------------------------------------------------- /probe endpoint ----

@pytest.fixture
def probe_srv(tmp_path):
    db = Database(str(tmp_path / 'bot.db'))
    db._users.save(User(chat_id='1', username='u1', status='demo', uuid=UUID,
                        email='u1@x', quota_gb=10.0))
    config = _config(SUB_MIRROR_URLS='https://m1.example.net,https://m2.example.org')
    return WebAppServer(config, db, xui_service=Mock()), db


def _client(srv):
    return TestClient(TestServer(srv.app))


async def _flush(srv):
    """Wait for the heartbeat tasks /probe spawned. Only on PENDING ones:
    since 3.12 gather() over finished tasks completes without yielding,
    so looping on the set until their discard callbacks run would spin."""
    while True:
        pending = [t for t in srv._bg_tasks if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending)


def _probes(db):
    return _rows(db, "SELECT chat_id, grp, src_ip FROM client_probe ORDER BY rowid")


class TestProbeEndpoint:

    @pytest.mark.asyncio
    @pytest.mark.parametrize('method', ['GET', 'HEAD'])  # mihomo tests with HEAD
    async def test_204_and_a_heartbeat(self, probe_srv, method):
        srv, db = probe_srv
        async with _client(srv) as client:
            resp = await client.request(method, f'/probe/{_token()}/emergency',
                                        headers={'X-Forwarded-For': '192.0.2.10, 10.0.0.1'})
            assert resp.status == 204
            assert await resp.read() == b''
            await _flush(srv)
        assert _probes(db) == [('1', 'emergency', '192.0.2.10')]
        assert _rows(db, "SELECT count(*) FROM client_probe WHERE ts IS NOT NULL") == [(1,)]

    @pytest.mark.asyncio
    async def test_one_row_per_group_per_minute(self, probe_srv):
        srv, db = probe_srv
        async with _client(srv) as client:
            for group in ('emergency', 'emergency', 'mirror-1', 'emergency', 'mirror-2',
                          'mirror-1'):
                assert (await client.get(f'/probe/{_token()}/{group}')).status == 204
                await _flush(srv)
            assert [r[1] for r in _probes(db)] == ['emergency', 'mirror-1', 'mirror-2']
            # a minute later the same provider writes again
            srv._probe_last[('1', 'emergency')] -= 61
            await client.get(f'/probe/{_token()}/emergency')
            await _flush(srv)
        assert [r[1] for r in _probes(db)] == ['emergency', 'mirror-1', 'mirror-2',
                                               'emergency']

    @pytest.mark.asyncio
    async def test_window_is_sixty_seconds(self, probe_srv):
        srv, db = probe_srv
        assert srv.PROBE_MIN_INTERVAL_S == 60
        async with _client(srv) as client:
            await client.get(f'/probe/{_token()}/mirror-2')
            await _flush(srv)
            srv._probe_last[('1', 'mirror-2')] -= 59
            await client.get(f'/probe/{_token()}/mirror-2')
            await _flush(srv)
        assert len(_probes(db)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize('path', [
        f'/probe/{"0" * 32}/emergency',               # unknown token
        f'/probe/{UUID.replace("-", "")[:31]}/emergency',  # malformed
        '/probe/' + 'A' * 32 + '/emergency',          # not lowercase hex
        '/probe/TOKEN/vpn',                           # unknown group (token below)
        '/probe/TOKEN/cascade',                       # groups check gstatic now
        '/probe/TOKEN/mirror-3',                      # only 2 mirrors configured
    ])
    async def test_unknown_is_204_without_a_row(self, probe_srv, path):
        srv, db = probe_srv
        path = path.replace('TOKEN', _token())
        async with _client(srv) as client:
            resp = await client.get(path)
            assert resp.status == 204 and await resp.read() == b''
            await _flush(srv)
        assert _probes(db) == []

    @pytest.mark.asyncio
    async def test_malformed_never_reaches_the_lookup(self, probe_srv, monkeypatch):
        srv, _db = probe_srv
        seen = []

        async def record(token, group, src_ip):
            seen.append((token, group))

        monkeypatch.setattr(srv, '_record_probe', record)
        async with _client(srv) as client:
            for path in (f'/probe/{_token()[:31]}/mirror-1', f'/probe/{_token()}0/mirror-1',
                         f'/probe/{_token().upper()}/mirror-1', f'/probe/{_token()}/Mirror-1',
                         f'/probe/{_token()}/mirror-0', f'/probe/{_token()}/mirror-3',
                         f'/probe/{_token()}/auto', f'/probe/{_token()}/mirror-1'):
                assert (await client.get(path)).status == 204
            await _flush(srv)
        assert seen == [(_token(), 'mirror-1')]

    @pytest.mark.asyncio
    async def test_answers_before_the_write(self, probe_srv, monkeypatch):
        # The client times this answer: a slow or locked sqlite must never
        # make a healthy tunnel look slow or dead.
        srv, db = probe_srv
        release = threading.Event()
        written = []

        def slow_insert(chat_id, group, src_ip):
            release.wait(5)
            written.append((chat_id, group))

        monkeypatch.setattr(srv, '_insert_client_probe', slow_insert)
        async with _client(srv) as client:
            try:
                resp = await asyncio.wait_for(
                    client.get(f'/probe/{_token()}/mirror-1'), timeout=2)
                assert resp.status == 204
                assert written == []
            finally:
                release.set()
            await _flush(srv)
        assert written == [('1', 'mirror-1')]

    @pytest.mark.asyncio
    async def test_db_failure_is_still_204(self, probe_srv, caplog):
        srv, db = probe_srv
        with db._connect() as conn:
            conn.execute("DROP TABLE client_probe")
        async with _client(srv) as client:
            assert (await client.get(f'/probe/{_token()}/emergency')).status == 204
            await _flush(srv)
        assert 'heartbeat not recorded' in caplog.text

    @pytest.mark.asyncio
    async def test_a_user_keyed_later_is_found_after_the_miss_window(self, probe_srv):
        srv, db = probe_srv
        other_uuid = '11111111-2222-3333-4444-555555555555'
        other = srv.subscription.derive_token(other_uuid)
        async with _client(srv) as client:
            await client.get(f'/probe/{_token()}/emergency')    # builds the map
            await _flush(srv)
            db._users.save(User(chat_id='2', username='u2', status='demo',
                                uuid=other_uuid, email='u2@x'))
            await client.get(f'/probe/{other}/emergency')       # miss within 60 s
            await _flush(srv)
            assert [r[0] for r in _probes(db)] == ['1']
            srv._probe_tokens_at -= 61                          # a minute later
            await client.get(f'/probe/{other}/emergency')
            await _flush(srv)
        assert [r[0] for r in _probes(db)] == ['1', '2']

    @pytest.mark.asyncio
    async def test_made_up_tokens_cost_one_scan_a_minute(self, probe_srv, monkeypatch):
        srv, _db = probe_srv
        scans = []
        real = srv._probe_token_map
        monkeypatch.setattr(srv, '_probe_token_map', lambda: scans.append(1) or real())
        async with _client(srv) as client:
            for n in range(5):
                await client.get(f'/probe/{n:032x}/emergency')
                await _flush(srv)
        assert len(scans) == 1

    @pytest.mark.asyncio
    async def test_failed_rebuild_waits_a_minute_too(self, probe_srv, monkeypatch):
        srv, db = probe_srv
        scans = []

        def broken():
            scans.append(1)
            raise RuntimeError('users table is gone')

        monkeypatch.setattr(srv, '_probe_token_map', broken)
        async with _client(srv) as client:
            for _ in range(3):
                assert (await client.get(f'/probe/{_token()}/emergency')).status == 204
                await _flush(srv)
        assert scans == [1] and _probes(db) == []

    @pytest.mark.asyncio
    async def test_map_is_rebuilt_every_ten_minutes(self, probe_srv):
        srv, db = probe_srv
        assert srv.PROBE_TOKEN_REFRESH_S == 600
        async with _client(srv) as client:
            await client.get(f'/probe/{_token()}/emergency')
            await _flush(srv)
            # the user is re-keyed: the old token must stop counting
            user = db._users.get_by_id('1')
            user.uuid = '99999999-2222-3333-4444-555555555555'
            db._users.save(user)
            srv._probe_last.clear()
            srv._probe_tokens_at -= 599
            await client.get(f'/probe/{_token()}/mirror-1')     # still cached
            await _flush(srv)
            srv._probe_last.clear()
            srv._probe_tokens_at -= 2                           # > 600 s old
            await client.get(f'/probe/{_token()}/mirror-2')
            await _flush(srv)
        assert [r[1] for r in _probes(db)] == ['emergency', 'mirror-1']


class TestClientProbeSchema:

    def test_table_and_indexes(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        with db._connect() as conn:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(client_probe)")]
            idx = {r[1] for r in conn.execute("PRAGMA index_list(client_probe)")}
        assert cols == ['chat_id', 'grp', 'ts', 'src_ip']
        assert {'idx_client_probe_chat_grp_ts', 'idx_client_probe_ts'} <= idx

    def test_init_is_idempotent(self, tmp_path):
        path = str(tmp_path / 'bot.db')
        db = Database(path)
        with db._connect() as conn:
            conn.execute("INSERT INTO client_probe (chat_id, grp) VALUES ('1', 'emergency')")
            conn.commit()
        db2 = Database(path)
        with db2._connect() as conn:
            assert conn.execute("SELECT count(*) FROM client_probe").fetchone()[0] == 1
