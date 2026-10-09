"""/sub records the client's network only when the address is really the client's.

A client that refreshes its profile THROUGH the tunnel reaches /sub from the
node the tunnel leaves by (exit, entry, the DE reserve), or as 172.x after a
hairpin through a docker bridge. Before the guard, the handler geo-resolved
that address and stamped the node's ASN/country on the user
(users.last_country/last_asn/last_city plus a sub_fetches row), so the
per-ASN cascade and DPIMonitor's per-ASN rules counted them as a customer of
our hoster, and failure reports printed the node's network with a fresh
"/sub" age. Now an address of ours is not looked up at all, the same guard as
hy2 auth: the profile is still served, ordered by the user's STORED network,
and nothing about the network is written.

Level 2: the /sub handler on a REAL sqlite (Database in tmp_path) with the
real builders; only the geo lookups and the panel quota read are stubbed. The
geo stub answers for EVERY address, private ones included, so a test passes
only because the handler never asked.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '5d1c0b9a-8e7f-4a6b-9c5d-4e3f2a1b0c9d'
ENTRY = '203.0.113.20'
EXIT = '203.0.113.30'
HY2 = '203.0.113.40'
DE = '198.51.100.9'
# A documentation range on purpose: ipaddress' is_private calls it private,
# so a guard built on is_private would wrongly skip this real client.
REAL = '198.51.100.77'
REAL_NET = ('RU', 'AS31133', 'Moscow')        # what REAL resolves to
NODE_NET = ('DE', 'AS24940', 'Falkenstein')   # what any other address resolves to
STORED_NET = ('RU', 'AS8359', 'Kazan')        # the user's row before the fetch
NAME = {'reality': 'u1-reality', 'hy2': 'u1-hy2', 'ws': 'u1-cdn-ws', 'stls': 'u1-stls'}

INTERNAL = (
    '172.17.0.1',       # default docker bridge gateway
    '172.20.0.5',       # a compose network
    '10.1.2.3',
    '192.168.1.10',
    '127.0.0.1',
    '169.254.1.1',
    '100.64.0.7',       # carrier-grade NAT
    '::1',
    'fd00::5',
    'fe80::1',
    '::ffff:172.17.0.2',
)


def _config(**over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL='https://dash.example.com',
        ENTRY_NODE_IP=ENTRY, ENTRY_NODE_PORT=8443, EXIT_NODE_IP=EXIT,
        REALITY_PUBLIC_KEY='reality-pbk', SNI_VALUE='www.bing.com',
        SID_VALUE='0123456789abcdef',
        HY2_HOST=HY2, HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='443,20000:40000', HY2T_PORT='',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api/v1/forecast',
        WS_SNI='cdn.example.com',
        STLS_HOST=ENTRY, STLS_PORT=443, STLS_SNI='www.microsoft.com',
        STLS_VERSION=3, STLS_PASSWORD='stls-pw',
        SS_METHOD='2022-blake3-aes-128-gcm', SS_SERVER_PASSWORD='srv-pw',
        SS_USER_SALT='salt', SUB_MIRROR_URLS='',
        FALLBACK_NODE_HOST=DE, FALLBACK_NODE_PBK='de-pbk',
        FALLBACK_NODE_SID='de01', FALLBACK_NODE_SNI='www.google.com',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


@pytest.fixture
def geo(monkeypatch):
    """Every address resolves; REAL to REAL_NET, anything else to NODE_NET.
    Returns the list of addresses the handler asked about."""
    from bot.services import geoip
    asked = []

    def net(ip):
        asked.append(ip)
        return REAL_NET if ip == REAL else NODE_NET

    monkeypatch.setattr(geoip, 'lookup', lambda ip: (net(ip)[0], ''))
    monkeypatch.setattr(geoip, 'lookup_asn', lambda ip: (net(ip)[1], 'org'))
    monkeypatch.setattr(geoip, 'lookup_city',
                        lambda ip: (net(ip)[2], 'region', 55.75, 37.62))
    return asked


def _server(tmp_path, config=None):
    db = Database(str(tmp_path / 'bot.db'))
    country, asn, city = STORED_NET
    db._users.save(User(chat_id='1', username='u1', status='paid', uuid=UUID,
                        email='u1@x', quota_gb=100.0, lang='ru',
                        last_country=country, last_asn=asn, last_city=city))
    db.set_setting('cascade_protocol_order', json.dumps(['reality', 'hy2', 'ws', 'stls']))
    srv = WebAppServer(config or _config(), db, xui_service=Mock())
    srv.xui.get_client_traffic = AsyncMock(return_value={'upload': 1, 'download': 2})
    return srv, db


async def _sub(srv, ip=None, query=None, remote=''):
    req = Mock()
    req.match_info = {'token': srv.subscription.derive_token(UUID)}
    req.rel_url = SimpleNamespace(query=query or {})
    req.headers = {'User-Agent': 'Hiddify/2.5.7'}
    if ip is not None:
        req.headers['X-Forwarded-For'] = ip
    req.remote = remote
    return await srv.handle_subscription(req)


def _rows(db, sql, *args):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]


def _network(db):
    return _rows(db, "SELECT last_country, last_asn, last_city FROM users")[0]


def _fetches(db):
    return _rows(db, "SELECT country, asn, city FROM sub_fetches ORDER BY rowid")


def _served(resp):
    assert resp.status == 200
    tags = [o.get('tag') for o in json.loads(resp.text)['outbounds']]
    assert 'u1-reality' in tags and 'u1-de' in tags
    return tags


class TestRecordsTheClientsNetwork:

    @pytest.mark.asyncio
    async def test_a_real_address_is_recorded(self, tmp_path, geo):
        srv, db = _server(tmp_path)
        _served(await _sub(srv, REAL))
        assert _network(db) == REAL_NET
        assert _fetches(db) == [REAL_NET]
        assert set(geo) == {REAL}

    @pytest.mark.asyncio
    async def test_the_first_forwarded_address_counts(self, tmp_path, geo):
        srv, db = _server(tmp_path)
        _served(await _sub(srv, f'{REAL}, {EXIT}'))
        assert _network(db) == REAL_NET

    @pytest.mark.asyncio
    async def test_the_peer_address_without_forwarding_headers(self, tmp_path, geo):
        srv, db = _server(tmp_path)
        _served(await _sub(srv, remote=REAL))
        assert _network(db) == REAL_NET


class TestOurOwnAddressesAreNotTheClientsNetwork:

    @pytest.mark.asyncio
    @pytest.mark.parametrize('ip', [EXIT, ENTRY, HY2, DE], ids=['exit', 'entry', 'hy2', 'de'])
    async def test_a_node_leaves_the_stored_network_alone(self, tmp_path, geo, ip):
        srv, db = _server(tmp_path)
        _served(await _sub(srv, ip))
        assert _network(db) == STORED_NET
        assert _fetches(db) == []
        assert geo == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize('ip', INTERNAL)
    async def test_internal_addresses_are_skipped(self, tmp_path, geo, ip):
        srv, db = _server(tmp_path)
        _served(await _sub(srv, ip))
        assert _network(db) == STORED_NET
        assert _fetches(db) == []
        assert geo == []

    @pytest.mark.asyncio
    async def test_a_node_written_as_ipv4_mapped_ipv6(self, tmp_path, geo):
        srv, db = _server(tmp_path)
        _served(await _sub(srv, f'::ffff:{EXIT}'))
        assert _network(db) == STORED_NET
        assert _fetches(db) == []

    @pytest.mark.asyncio
    async def test_docker_peer_without_forwarding_headers(self, tmp_path, geo):
        # The bot's :8080 reached straight from the docker network.
        srv, db = _server(tmp_path)
        _served(await _sub(srv, remote='172.17.0.1'))
        assert _network(db) == STORED_NET
        assert _fetches(db) == []

    @pytest.mark.asyncio
    async def test_a_tunnel_refresh_does_not_freshen_the_report_age(self, tmp_path, geo):
        # Failure reports print max(sub_fetches.ts) as the age of the
        # stored network; a fetch that observed no network must not move it.
        srv, db = _server(tmp_path)
        _served(await _sub(srv, REAL))
        first = _rows(db, "SELECT max(ts), count(*) FROM sub_fetches")
        _served(await _sub(srv, EXIT))
        _served(await _sub(srv, '172.18.0.3'))
        assert _rows(db, "SELECT max(ts), count(*) FROM sub_fetches") == first
        assert _network(db) == REAL_NET

    @pytest.mark.asyncio
    async def test_ordered_by_the_stored_network(self, tmp_path, geo):
        srv, db = _server(tmp_path)
        db.set_setting('cascade_by_asn', json.dumps({
            NODE_NET[1]: ['stls', 'hy2', 'ws', 'reality'],
            STORED_NET[1]: ['ws', 'stls', 'reality', 'hy2'],
            REAL_NET[1]: ['hy2', 'reality', 'stls', 'ws'],
        }))

        async def order(ip, query):
            body = json.loads((await _sub(srv, ip, query)).text)
            return [p['name'] for p in body['proxies'] if p['name'] != 'u1-de']

        for query in ({'format': 'clash-proxies'}, {'format': 'clash'}):
            assert await order(EXIT, query) == [
                NAME[p] for p in ('ws', 'stls', 'reality', 'hy2')], query
        # the same request from the client's own network follows that network
        assert await order(REAL, {'format': 'clash-proxies'}) == [
            NAME[p] for p in ('hy2', 'reality', 'stls', 'ws')]

    @pytest.mark.asyncio
    async def test_demotions_of_the_nodes_network_are_not_applied(self, tmp_path, geo):
        srv, db = _server(tmp_path)
        db.set_setting('cascade_auto', json.dumps({'global': {}, 'asn': {
            NODE_NET[1]: {'ws': {'since': '2026-10-08T10:00:00', 'reason': 'probe_dark'}},
        }}))
        cfg = json.loads((await _sub(srv, EXIT, {'format': 'clash'})).text)
        cascade = next(g for g in cfg['proxy-groups'] if g['name'] == 'Cascade')
        assert NAME['ws'] in cascade['proxies']


class TestIsOwnAddress:

    @pytest.mark.parametrize('ip', [ENTRY, EXIT, HY2, DE, f'::ffff:{DE}', *INTERNAL])
    def test_ours(self, tmp_path, ip):
        srv, _db = _server(tmp_path)
        assert srv._is_own_address(ip) is True

    @pytest.mark.parametrize('ip', [
        REAL, '192.0.2.10', '203.0.113.99', '8.8.8.8', '2001:db8::1',
        '2a00:1450:4010:c05::64', '', 'unknown', None,
    ])
    def test_not_ours(self, tmp_path, ip):
        srv, _db = _server(tmp_path)
        assert srv._is_own_address(ip) is False

    def test_padded_and_empty_settings(self, tmp_path):
        srv, _db = _server(tmp_path, _config(EXIT_NODE_IP=f' {EXIT} ', ENTRY_NODE_IP='',
                                             FALLBACK_NODE_HOST=''))
        assert srv._is_own_address(EXIT) is True
        assert srv._is_own_address(ENTRY) is False
        assert srv._is_own_address(DE) is False

    @pytest.mark.asyncio
    async def test_a_host_name_setting_is_not_resolved(self, tmp_path, geo, monkeypatch):
        import socket

        def no_dns(*a, **kw):
            raise AssertionError('the guard must not resolve host names')

        monkeypatch.setattr(socket, 'getaddrinfo', no_dns)
        monkeypatch.setattr(socket, 'gethostbyname', no_dns)
        # HY2_HOST as a name sits between the entry/exit IPs and the
        # reserve's IP: the settings after it must still count.
        srv, db = _server(tmp_path, _config(HY2_HOST='hy2.example.com'))
        assert srv._is_own_address(HY2) is False
        assert srv._is_own_address(DE) is True
        assert srv._is_own_address(EXIT) is True
        _served(await _sub(srv, DE))
        assert _network(db) == STORED_NET
        assert geo == []
