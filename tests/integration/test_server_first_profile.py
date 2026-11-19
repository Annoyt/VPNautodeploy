"""The server-first twins in the NekoVPN Clash profile (IMPROVEMENT_PLAN G4,
AGENTS.md §62).

The front on entry writes a banner line before the client sends anything
(scripts/server_first_front.py); the NekoVPN app from NEKO_SF_MIN_VERSION on
drops it (client repo, core/neko_server_first.go). What is pinned here: only
such an app gets the twins, and only for the protocols the front serves; a
twin is its server on the front's port with the marker, right before it in
every group; a demotion of the plain protocol does not take the twin out of
Cascade; the telemetry providers stay per plain protocol; with nothing
configured, the profile is byte for byte what it was.
"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings("ignore:Database\\..*is deprecated:DeprecationWarning")

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
ALL = ('reality', 'hy2', 'ws', 'stls')
SF = dict(SF_REALITY_PORT='2443', SF_STLS_PORT='2444', NEKO_SF_MIN_VERSION='0.9.2-neko.5')
MARKER = SubscriptionService.SERVER_FIRST_MARKER


def _config(**over):
    cfg = dict(
        BOT_TOKEN='t', WEBAPP_URL='https://dash.example.com',
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='reality-pbk', SNI_VALUE='www.bing.com', SID_VALUE='0123456789abcdef',
        HY2_HOST='203.0.113.20', HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='', HY2T_PORT='',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api', WS_SNI='cdn.example.com',
        WS2_HOST='', STLS_HOST='203.0.113.20', STLS_PORT=443, STLS_SNI='www.microsoft.com',
        STLS_VERSION=3, STLS_PASSWORD='stls-pw', SS_METHOD='2022-blake3-aes-128-gcm',
        SS_SERVER_PASSWORD='srv-pw', SS_USER_SALT='salt', SUB_MIRROR_URLS='',
        FALLBACK_NODE_HOST='', TLS_FINGERPRINTS='',
        XHR_SNI='', XHR_PATH='/xhr', XHR_PUBLIC_KEY='', XHR_SID='', XHR_PORT=0,
        XHR_MAX_CONNECTIONS=3,
        SSH_EXIT_HOST='', SSH_EXIT_PORT='22022', SSH_HOST_KEY='', SSH_USER_SECRET='',
        SF_REALITY_PORT='', SF_STLS_PORT='', NEKO_SF_MIN_VERSION='',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def _user(status='paid'):
    return SimpleNamespace(uuid=UUID, email='u1@x', status=status, lang='ru', chat_id='1',
                           subscription_expiry=None, previous_state=None)


def _text(protocols=ALL, config=None, **kw):
    svc = SubscriptionService(config or _config(**SF))
    return svc.build_clash_config(kw.pop('user', None) or _user(), protocols, **kw)


def _clash(protocols=ALL, config=None, **kw):
    return json.loads(_text(protocols, config, **kw))


def _group(clash, name):
    return next(g for g in clash['proxy-groups'] if g['name'] == name)


def _names(clash):
    return [p['name'] for p in clash['proxies']]


def _by_name(clash):
    return {p['name']: p for p in clash['proxies']}


def _plain_names():
    plain = _clash(server_first=False)
    reality = next(p['name'] for p in plain['proxies'] if p['type'] == 'vless')
    stls = next(p['name'] for p in plain['proxies'] if p.get('plugin') == 'shadow-tls')
    return plain, reality, stls


class TestTwins:

    def test_without_the_flag_there_are_none(self):
        clash = _clash(server_first=False)
        assert not any(n.endswith('-sf') for n in _names(clash))
        assert not any(MARKER in p for p in clash['proxies'])

    def test_each_twin_comes_right_before_its_server(self):
        plain, reality, stls = _plain_names()
        expected = []
        for name in _names(plain):
            if name in (reality, stls):
                expected.append(f'{name}-sf')
            expected.append(name)
        assert _names(_clash(server_first=True)) == expected

    def test_a_twin_is_its_server_on_the_front_port_with_the_marker(self):
        plain, reality, stls = _plain_names()
        twins = _by_name(_clash(server_first=True))
        originals = _by_name(plain)
        for name, port in ((reality, 2443), (stls, 2444)):
            twin = dict(twins[f'{name}-sf'])
            assert twin.pop(MARKER) is True
            assert twin.pop('port') == port
            assert twin.pop('name') == f'{name}-sf'
            original = dict(originals[name])
            original.pop('port')
            original.pop('name')
            assert twin == original
            # the server itself is left as it was
            assert twins[name] == originals[name]
            assert MARKER not in twins[name]

    def test_every_group_lists_the_twin_before_its_server(self):
        plain, reality, stls = _plain_names()
        clash = _clash(server_first=True)
        for group in ('VPN', 'Cascade', 'Auto'):
            members = _group(clash, group)['proxies']
            for name in (reality, stls):
                assert members.index(f'{name}-sf') == members.index(name) - 1, group

    def test_the_reality_twin_carries_calls_before_reality(self):
        plain, reality, _ = _plain_names()
        calls = _group(_clash(server_first=True), 'Calls')['proxies']
        assert calls.index(f'{reality}-sf') == calls.index(reality) - 1
        # ShadowTLS carries no UDP: neither it nor its twin is a call server
        assert not any(n.endswith('-stls-sf') or n.endswith('-stls') for n in calls)

    def test_a_demotion_of_the_plain_protocol_keeps_the_twin_in_cascade(self):
        _, reality, _ = _plain_names()
        clash = _clash(server_first=True, demoted={'reality'})
        cascade = _group(clash, 'Cascade')['proxies']
        assert reality not in cascade
        assert f'{reality}-sf' in cascade
        # the manual pick keeps both
        assert {reality, f'{reality}-sf'} <= set(_group(clash, 'VPN')['proxies'])
        calls = _group(clash, 'Calls')['proxies']
        assert calls.index(f'{reality}-sf') < calls.index(reality)

    def test_the_telemetry_providers_stay_per_plain_protocol(self):
        plain = _clash(server_first=False)
        twinned = _clash(server_first=True)
        assert plain.get('proxy-providers') == twinned.get('proxy-providers')
        assert not any(k.endswith('-sf') for k in (twinned.get('proxy-providers') or {}))

    def test_only_the_protocols_the_front_serves(self):
        _, reality, stls = _plain_names()
        clash = _clash(server_first=True, config=_config(**{**SF, 'SF_STLS_PORT': ''}))
        assert f'{reality}-sf' in _names(clash)
        assert f'{stls}-sf' not in _names(clash)

    def test_xhr_gets_a_twin_on_the_reality_port(self):
        config = _config(**SF, XHR_SNI='dl.google.com')
        clash = _clash(ALL + ('xhr',), config=config, server_first=True)
        xhr = next(p for p in clash['proxies'] if p.get('network') == 'xhttp'
                   and not p['name'].endswith('-sf'))
        twin = _by_name(clash)[f"{xhr['name']}-sf"]
        assert twin['port'] == 2443 and twin[MARKER] is True

    def test_hy2_and_ws_get_no_twin(self):
        clash = _clash(server_first=True)
        assert not any(p['name'].endswith('-sf') and p['type'] in ('hysteria2', 'vmess')
                       for p in clash['proxies'])

    @pytest.mark.parametrize('port', ['', ' ', '0', '65536', 'abc', '-1', '2443x'])
    def test_an_unusable_port_is_no_twin(self, port):
        config = _config(**{**SF, 'SF_REALITY_PORT': port, 'SF_STLS_PORT': port})
        assert SubscriptionService(config).server_first_ports() == {}
        clash = _clash(server_first=True, config=config)
        assert not any(n.endswith('-sf') for n in _names(clash))

    def test_with_nothing_configured_the_profile_is_byte_for_byte_the_same(self):
        bare = vars(_config()).copy()
        for key in ('SF_REALITY_PORT', 'SF_STLS_PORT', 'NEKO_SF_MIN_VERSION'):
            bare.pop(key)
        before = _text(config=SimpleNamespace(**bare))
        assert _text(config=_config(), server_first=True) == before
        assert _text(config=_config(**SF), server_first=False) == before


class TestWhoGetsThem:

    @pytest.mark.parametrize('version, gets', [
        ('0.9.2-neko.5', True),
        ('0.9.2-neko.6', True),
        ('0.9.2-neko.5+2026112001', True),
        ('v0.9.2-neko.5', True),
        ('0.9.3', True),
        ('0.10.0-neko.1', True),
        ('0.9.2-neko.4', False),
        ('0.9.2', False),
        ('0.8.99-neko.9', False),
        ('0.9.2-beta.5', False),
        ('garbage', False),
        ('', False),
        (None, False),
        (5, False),
    ])
    def test_by_app_version(self, version, gets):
        assert SubscriptionService(_config(**SF)).server_first_for(version) is gets

    @pytest.mark.parametrize('over', [
        {'NEKO_SF_MIN_VERSION': ''},
        {'NEKO_SF_MIN_VERSION': 'soon'},
        {'SF_REALITY_PORT': '', 'SF_STLS_PORT': ''},
    ])
    def test_not_without_a_minimum_version_or_a_port(self, over):
        svc = SubscriptionService(_config(**{**SF, **over}))
        assert svc.server_first_for('9.9.9-neko.9') is False


class TestTheHandler:

    def _server(self, tmp_path, monkeypatch):
        from bot.services import geoip
        monkeypatch.setattr(geoip, 'lookup', lambda ip: None)
        monkeypatch.setattr(geoip, 'lookup_asn', lambda ip: None)
        monkeypatch.setattr(geoip, 'lookup_city', lambda ip: None)
        db = Database(str(tmp_path / 'bot.db'))
        db._users.save(User(chat_id='1', username='u1', status='paid', uuid=UUID,
                            email='u1@x', quota_gb=100.0, lang='ru'))
        srv = WebAppServer(_config(**SF), db, xui_service=Mock())
        srv.xui.get_client_traffic = AsyncMock(return_value={})
        srv._schedule_fallback_provisioning = Mock()
        srv._schedule_neko_app = Mock()
        return srv

    async def _fetch(self, srv, ua, fmt='clash'):
        req = Mock()
        req.match_info = {'token': srv.subscription.derive_token(UUID)}
        req.rel_url = SimpleNamespace(query={'format': fmt} if fmt else {})
        req.headers = {'User-Agent': ua, 'X-Forwarded-For': '198.51.100.7'}
        req.remote = ''
        return (await srv.handle_subscription(req)).text

    @pytest.mark.asyncio
    @pytest.mark.parametrize('ua, twins', [
        ('NekoVPN/v0.9.2-neko.5 clash-verge Platform/android', True),
        ('NekoVPN/v0.9.3 clash-verge Platform/android', True),
        ('NekoVPN/v0.9.2-neko.4 clash-verge Platform/android', False),
        ('FlClash/v0.8.90 clash-verge Platform/android', False),
        ('clash-verge/v1.7', False),
        ('', False),
    ])
    async def test_by_user_agent(self, tmp_path, monkeypatch, ua, twins):
        srv = self._server(tmp_path, monkeypatch)
        clash = json.loads(await self._fetch(srv, ua))
        assert any(p.get(MARKER) for p in clash['proxies']) is twins

    @pytest.mark.asyncio
    async def test_the_sing_box_profile_never_gets_them(self, tmp_path, monkeypatch):
        srv = self._server(tmp_path, monkeypatch)
        body = await self._fetch(srv, 'NekoVPN/v0.9.2-neko.5 clash-verge Platform/android', fmt=None)
        assert MARKER not in body and '-sf"' not in body
