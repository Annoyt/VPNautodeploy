"""Per-protocol client telemetry (IMPROVEMENT_PLAN E8) — the profile side.

* one ``p-<proto>`` proxy-provider per protocol the FlClash profile holds
  (``de`` = the reserve node), ``?format=clash-proxies&only=<proto>``,
  fetched DIRECT, health-checked on ``/probe/<token>/p-<proto>`` with
  ``lazy: false`` and used by no group (mihomo checks an unused provider
  only with ``lazy: false`` — see ``_clash_telemetry_providers``);
* ``only=``: that protocol's server alone, an empty list for anything else;
  no parameter = every server, byte-identical to before;
* ``/probe`` accepts exactly the ``p-<proto>`` this deployment can emit
  (``SubscriptionService.telemetry_protocols`` — the profile's own builders).

The DPIMonitor rule that reads these rows (R6 ``client_dark``) and the
reverse SOS (E21) are in test_client_dark.py.

Level 2: the real builders, the real /sub and /probe handlers on a REAL
sqlite (Database in tmp_path); only geo lookups and the panel quota read
are stubbed.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
WEB = 'https://dash.example.com'
GSTATIC = 'https://www.gstatic.com/generate_204'
ALL = ('reality', 'hy2', 'ws', 'stls')
FALLBACK = dict(FALLBACK_NODE_HOST='198.51.100.9', FALLBACK_NODE_PBK='de-pbk',
                FALLBACK_NODE_SID='de01', FALLBACK_NODE_SNI='www.google.com')
HY2T = dict(HY2T_PORT='8402', HY2T_HOP_PORTS='8402,40001:50000')
NAME = {'reality': 'u1-reality', 'hy2': 'u1-hy2', 'hy2t': 'u1-hy2t',
        'ws': 'u1-cdn-ws', 'stls': 'u1-stls', 'de': 'u1-de'}


def make_config(**over):
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


def _user(status='demo', uuid=UUID):
    return SimpleNamespace(uuid=uuid, email='u1@x', status=status, lang='ru', chat_id='1')


def _token(uuid=UUID):
    return SubscriptionService(make_config()).derive_token(uuid)


def _profile(protocols=ALL, user=None, config=None, **kw):
    svc = SubscriptionService(config or make_config())
    return json.loads(svc.build_clash_config(user or _user(), protocols, **kw))


def _telemetry(cfg):
    return {k: v for k, v in (cfg.get('proxy-providers') or {}).items()
            if k.startswith('p-')}


def _names(*protos):
    return [NAME[p] for p in protos]


# =====================================================================
# E8 — the profile
# =====================================================================

class TestTelemetryProviders:

    def test_one_provider_per_protocol_in_cascade_order_reserve_last(self):
        order = ('ws', 'stls', 'reality', 'hy2', 'hy2t')
        cfg = _profile(order, user=_user('paid'), config=make_config(**FALLBACK, **HY2T))
        assert list(cfg['proxy-providers']) == [
            'emergency', 'p-ws', 'p-stls', 'p-reality', 'p-hy2', 'p-hy2t', 'p-de']

    def test_provider_shape(self):
        tok = _token()
        assert _telemetry(_profile())['p-hy2'] == {
            'type': 'http',
            'url': f'{WEB}/sub/{tok}?format=clash-proxies&only=hy2',
            'path': './providers/p-hy2.yaml',
            'interval': 3600,
            # never through the tunnel: the telemetry would die with it
            'proxy': 'DIRECT',
            'health-check': {'enable': True, 'url': f'{WEB}/probe/{tok}/p-hy2',
                             'interval': 600,
                             # an unused provider is checked only with lazy: false
                             'lazy': False},
            'override': {'additional-prefix': '[P] '},
        }

    def test_no_group_uses_them(self):
        cfg = _profile(ALL, user=_user('paid'), config=make_config(
            **FALLBACK, SUB_MIRROR_URLS='https://m1.example.net'), demoted={'hy2'})
        for group in cfg['proxy-groups']:
            assert not [u for u in group.get('use', ()) if u.startswith('p-')], group['name']
            assert not [p for p in group['proxies'] if p.startswith('[P]')], group['name']
        g = {x['name']: x for x in cfg['proxy-groups']}
        assert g['VPN']['use'] == ['emergency', 'mirror-1']
        assert g['Cascade']['url'] == GSTATIC        # the groups keep gstatic

    def test_demo_profile_reports_its_own_tier_only(self):
        # get_cascade_order hands a demo user the free protocols only
        cfg = _profile(('ws', 'stls', 'hy2'), user=_user('demo'),
                       config=make_config(**FALLBACK, **HY2T))
        assert list(_telemetry(cfg)) == ['p-ws', 'p-stls', 'p-hy2']

    def test_a_protocol_the_deployment_cannot_build_gets_none(self):
        # hy2t asked for, HY2T_PORT empty → no server → no provider
        cfg = _profile(('hy2t', 'ws'), user=_user('paid'))
        assert list(_telemetry(cfg)) == ['p-ws']

    @pytest.mark.parametrize('over,user', [
        ({'WEBAPP_URL': ''}, None),
        ({'WEBAPP_URL': '', 'SUB_MIRROR_URLS': 'https://m1.example.net'}, None),
        ({}, SimpleNamespace(uuid=None, email='u1@x', status='demo', lang='ru',
                             chat_id='1')),
    ])
    def test_none_without_a_public_address_or_a_token(self, over, user):
        svc = SubscriptionService(make_config(**over))
        cfg = json.loads(svc.build_clash_config(user or _user(), ALL))
        assert _telemetry(cfg) == {}

    def test_none_for_an_empty_cascade(self):
        assert 'proxy-providers' not in _profile(())

    def test_every_telemetry_url_is_accepted_by_the_endpoint(self):
        config = make_config(**FALLBACK, **HY2T)
        cfg = _profile(('reality', 'hy2', 'hy2t', 'ws', 'stls'), user=_user('paid'),
                       config=config)
        groups = SubscriptionService(config).probe_groups()
        tel = _telemetry(cfg)
        assert len(tel) == 6
        for name, prov in tel.items():
            prefix, group = prov['health-check']['url'].rsplit('/', 1)
            assert prefix == f'{WEB}/probe/{_token()}' and group == name in groups


class TestTelemetryProtocols:

    @pytest.mark.parametrize('over,expected', [
        ({}, {'reality', 'hy2', 'ws', 'stls'}),
        (dict(**HY2T), {'reality', 'hy2', 'hy2t', 'ws', 'stls'}),
        (dict(**FALLBACK), {'reality', 'hy2', 'ws', 'stls', 'de'}),
        ({'WS_HOST': ''}, {'reality', 'hy2', 'stls'}),
        ({'REALITY_PUBLIC_KEY': ''}, {'hy2', 'ws', 'stls'}),
        ({'HY2_HOST': ''}, {'reality', 'ws', 'stls'}),
        ({'STLS_PASSWORD': ''}, {'reality', 'hy2', 'ws'}),
        ({'WS_PORT': 'not-a-port'}, {'reality', 'hy2', 'stls'}),   # builder raises
        ({'FALLBACK_NODE_HOST': '198.51.100.9'}, {'reality', 'hy2', 'ws', 'stls'}),
    ])
    def test_follows_the_builders(self, over, expected):
        assert SubscriptionService(make_config(**over)).telemetry_protocols() == expected

    def test_probe_groups_carry_them(self):
        svc = SubscriptionService(make_config(**FALLBACK,
                                              SUB_MIRROR_URLS='https://m1.example.net'))
        assert svc.probe_groups() == {'emergency', 'mirror-1', 'p-reality', 'p-hy2',
                                      'p-ws', 'p-stls', 'p-de'}

    def test_a_broken_builder_is_quiet(self, caplog):
        SubscriptionService(make_config(WS_PORT='x')).telemetry_protocols()
        assert 'failed to build' not in caplog.text


# =====================================================================
# E8 — ?format=clash-proxies&only=<proto>
# =====================================================================

class TestOnly:

    def _proxies(self, user, protocols=ALL, config=None, only=None):
        svc = SubscriptionService(config or make_config(**FALLBACK, **HY2T))
        return json.loads(svc.build_clash_proxies(user, protocols, only=only))['proxies']

    @pytest.mark.parametrize('proto', ['reality', 'hy2', 'ws', 'stls'])
    def test_only_that_protocols_server(self, proto):
        everything = self._proxies(_user('paid'))
        assert self._proxies(_user('paid'), only=proto) == [
            p for p in everything if p['name'] == NAME[proto]]

    def test_de_is_the_reserve_node(self):
        assert [p['name'] for p in self._proxies(_user('paid'), only='de')] == ['u1-de']
        assert self._proxies(_user('demo'), protocols=('ws', 'hy2'), only='de') == []

    @pytest.mark.parametrize('only', ['bogus', '', 'xhttp', 'p-hy2', 'de ws', 'None'])
    def test_anything_else_is_an_empty_list(self, only):
        assert self._proxies(_user('paid'), only=only) == []

    def test_a_protocol_not_in_this_users_cascade_is_empty(self):
        assert self._proxies(_user('demo'), protocols=('ws', 'stls', 'hy2'),
                             only='reality') == []

    def test_case_and_spaces_are_tolerated(self):
        assert [p['name'] for p in self._proxies(_user('paid'), only=' WS ')] == ['u1-cdn-ws']

    def test_no_parameter_is_every_server_as_before(self):
        svc = SubscriptionService(make_config(**FALLBACK))
        assert svc.build_clash_proxies(_user('paid'), ALL) == \
            svc.build_clash_proxies(_user('paid'), ALL, only=None)
        assert len(self._proxies(_user('paid'))) == 5


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


def _server(tmp_path, status='paid', config=None):
    db = Database(str(tmp_path / 'bot.db'))
    db._users.save(User(chat_id='1', username='u1', status=status, uuid=UUID,
                        email='u1@x', quota_gb=10.0, lang='ru'))
    db.set_setting('cascade_protocol_order', json.dumps(['reality', 'hy2', 'ws', 'stls']))
    srv = WebAppServer(config or make_config(**FALLBACK), db, xui_service=Mock())
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


class TestOnlyThroughTheHandler:

    @pytest.mark.asyncio
    async def test_each_provider_url_of_the_profile_serves_its_protocol(self, tmp_path, geo):
        srv, _db = _server(tmp_path)
        profile = json.loads((await _sub(srv, {'format': 'clash'})).text)
        tel = _telemetry(profile)
        assert list(tel) == ['p-reality', 'p-hy2', 'p-ws', 'p-stls', 'p-de']
        for name, prov in tel.items():
            query = dict(kv.split('=') for kv in prov['url'].split('?', 1)[1].split('&'))
            resp = await _sub(srv, query)
            assert resp.status == 200 and resp.headers['cache-control'] == 'no-store'
            got = json.loads(resp.text)['proxies']
            assert [p['name'] for p in got] == [NAME[name[2:]]]
            assert got == [p for p in profile['proxies'] if p['name'] == NAME[name[2:]]]

    @pytest.mark.asyncio
    async def test_read_only_like_every_provider_fetch(self, tmp_path, geo):
        srv, db = _server(tmp_path)
        geo.update(country='RU', asn='AS31133')
        await _sub(srv, {'format': 'clash-proxies', 'only': 'hy2'})
        assert _rows(db, "SELECT last_asn FROM users") == [(None,)]
        assert _rows(db, "SELECT count(*) FROM sub_fetches") == [(0,)]
        srv.xui.get_client_traffic.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize('only', ['', 'bogus', 'x' * 100])
    async def test_unknown_is_an_empty_list(self, tmp_path, geo, only):
        srv, _db = _server(tmp_path)
        resp = await _sub(srv, {'format': 'clash-proxies', 'only': only})
        assert resp.status == 200 and json.loads(resp.text) == {'proxies': []}

    @pytest.mark.asyncio
    async def test_ignored_outside_clash_proxies(self, tmp_path, geo):
        srv, _db = _server(tmp_path)
        for fmt in ('clash', 'links', 'xray', ''):
            base = await _sub(srv, {'format': fmt} if fmt else {})
            with_only = await _sub(srv, {**({'format': fmt} if fmt else {}), 'only': 'hy2'})
            assert with_only.body == base.body, fmt


# =====================================================================
# E8 — /probe accepts p-<proto>
# =====================================================================

async def _flush(srv):
    while True:
        pending = [t for t in srv._bg_tasks if not t.done()]
        if not pending:
            return
        await asyncio.wait(pending)


class TestProbeEndpoint:

    @pytest.mark.asyncio
    @pytest.mark.parametrize('config,accepted,refused', [
        (make_config(), ['p-reality', 'p-hy2', 'p-ws', 'p-stls'],
         ['p-hy2t', 'p-de', 'p-xhttp', 'p-', 'p-HY2', 'p-bogus']),
        (make_config(**FALLBACK, **HY2T), ['p-hy2t', 'p-de'], ['p-xhttp']),
    ])
    async def test_rows_only_for_the_protocols_this_deployment_emits(
            self, tmp_path, config, accepted, refused):
        srv, db = _server(tmp_path, config=config)
        async with TestClient(TestServer(srv.app)) as client:
            for group in accepted + refused:
                resp = await client.request('HEAD', f'/probe/{_token()}/{group}')
                assert resp.status == 204
                await _flush(srv)
        assert [r[0] for r in _rows(db, "SELECT grp FROM client_probe ORDER BY rowid")] \
            == accepted

    @pytest.mark.asyncio
    async def test_still_one_row_a_minute_per_protocol(self, tmp_path):
        srv, db = _server(tmp_path)
        async with TestClient(TestServer(srv.app)) as client:
            for group in ('p-hy2', 'p-hy2', 'p-ws', 'p-hy2'):
                await client.get(f'/probe/{_token()}/{group}')
                await _flush(srv)
        assert _rows(db, "SELECT chat_id, grp FROM client_probe ORDER BY rowid") == [
            ('1', 'p-hy2'), ('1', 'p-ws')]
