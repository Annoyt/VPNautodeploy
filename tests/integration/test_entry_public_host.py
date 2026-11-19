"""ENTRY_PUBLIC_HOST: entry's address for clients, apart from the node's own
(AGENTS.md §63).

On 2026-11-19 AdminVPS moved entry from 130.49.146.10 to 157.22.231.40. hy2,
stls and the subscription follow the ``radar`` name, so they came back with a
DNS change. Reality, its server-first twin, the SSH path through entry, the
RU-zone egress, xhr and the share links were built from ENTRY_NODE_IP, so every
profile named the dead address until its user refreshed, and ENTRY_NODE_IP
could not simply follow: the hop's certificate carries the address exit dials
(§53), and exit still dials the old one into the tunnel. ENTRY_PUBLIC_HOST is
where clients connect (an IP or a name; empty = ENTRY_NODE_IP, byte for byte
what the builders made before). HOP_TLS_IP, the hop certificate's address, is
tested in test_hop_tls.py.

Level 2: the real builders on the config the SSH-path tests use.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot.core.web_server import WebAppServer
from bot.services.subscription import SubscriptionService
from bot.services.vpn import VPNService
from bot.utils.public_url import entry_public_host

from .test_ssh_probe import CASCADE, SSH, _config

UUID = '6f1e2d3c-4b5a-4968-8778-695a4b3c2d1e'
NODE = '203.0.113.20'            # ENTRY_NODE_IP of the shared _config
NAME = 'entry.example.net'
RU_EXIT = {'port': 8445, 'sni': 'www.google.com', 'pbk': 'ru-pbk', 'sid': 'ab01'}


def cfg(**over):
    """hy2 and stls on a name of their own, so the node's IP can only come
    from the builders this setting is about; the server-first front, xhr and
    the SSH relay on."""
    base = dict(SSH, HY2_HOST='radar.example.net', STLS_HOST='radar.example.net',
                SF_REALITY_PORT='2443', SF_STLS_PORT='2444', NEKO_SF_MIN_VERSION='0.9.2-neko.5',
                XHR_SNI='gateway.icloud.com', SSH_ENTRY_PORT='2222')
    base.update(over)
    return _config(**base)


def user(status='paid', expiry='2099-12-31T23:59:59'):
    return SimpleNamespace(uuid=UUID, email='u1@x', status=status, lang='ru', chat_id='1',
                           subscription_expiry=expiry, previous_state=None)


def texts(config):
    """Every profile and link a client of ``config`` can get, as text."""
    svc, u = SubscriptionService(config), user()
    vpn = VPNService(config)
    return {
        'clash emergency + twins': svc.build_clash_config(u, CASCADE, lockdown=True,
                                                          server_first=True),
        'clash': svc.build_clash_config(u, CASCADE),
        'sing-box emergency': json.dumps(svc.build_singbox_config(u, CASCADE, lockdown=True)),
        'sing-box': json.dumps(svc.build_singbox_config(u, CASCADE)),
        'hiddify': json.dumps(svc.build_singbox_config(u, CASCADE, hiddify=True)),
        'providers': svc.build_clash_proxies(u, CASCADE),
        'links': svc.build_links(u, CASCADE),
        'xray': json.dumps(svc.build_xray_config(u, CASCADE)),
        'ru-zone': json.dumps(svc._build_ru_zone(UUID, RU_EXIT)),
        'xhr': json.dumps(svc._build_xhr(UUID, 'u1')),
        'vless link': vpn.generate_vless_link(UUID, 'u1@x'),
        'xhr link': vpn.generate_xhr_link(UUID, 'u1@x'),
    }


# ------------------------------------------------------------------ helper ---

class TestHelper:

    def test_the_setting_wins_without_its_spaces(self):
        c = SimpleNamespace(ENTRY_PUBLIC_HOST=f'  {NAME} ', ENTRY_NODE_IP=NODE)
        assert entry_public_host(c) == NAME

    def test_empty_or_not_a_string_is_entry_node_ip(self):
        for value in ('', '   ', None, 8443, Mock()):
            c = SimpleNamespace(ENTRY_PUBLIC_HOST=value, ENTRY_NODE_IP=NODE)
            assert entry_public_host(c) == NODE, value
        assert entry_public_host(SimpleNamespace(ENTRY_NODE_IP=NODE)) == NODE   # an older config

    def test_neither_is_empty(self):
        assert entry_public_host(SimpleNamespace(ENTRY_NODE_IP='')) == ''
        assert entry_public_host(SimpleNamespace(ENTRY_NODE_IP=None)) == ''
        assert entry_public_host(SimpleNamespace()) == ''


# ---------------------------------------------------------------- profiles ---

class TestProfiles:

    def test_unset_builds_what_it_built_before(self):
        # unset, empty and equal to ENTRY_NODE_IP: the same bytes everywhere
        before = texts(cfg())
        for value in ('', '  ', NODE):
            assert texts(cfg(ENTRY_PUBLIC_HOST=value)) == before, repr(value)

    def test_every_entry_server_follows_it(self):
        got = texts(cfg(ENTRY_PUBLIC_HOST=NAME))
        for what, text in got.items():
            assert text and text != 'null', what
            assert NODE not in text, what          # nothing still names the node's IP
            assert NAME in text, what

    def test_the_server_of_each_entry_protocol(self):
        svc = SubscriptionService(cfg(ENTRY_PUBLIC_HOST=NAME))
        prof = json.loads(svc.build_clash_config(user(), CASCADE, lockdown=True,
                                                 server_first=True))
        servers = {p['name']: p['server'] for p in prof['proxies']}
        assert servers['u1-reality'] == NAME
        assert servers['u1-reality-sf'] == NAME and servers['u1-stls-sf'] == 'radar.example.net'
        assert servers['u1-ssh-entry'] == NAME
        assert servers['u1-ssh'] == SSH['SSH_EXIT_HOST']         # exit's own address stays
        assert servers['u1-hy2'] == servers['u1-stls'] == 'radar.example.net'
        sb = svc.build_singbox_config(user(), CASCADE, lockdown=True)
        by_tag = {o['tag']: o.get('server') for o in sb['outbounds']}
        assert by_tag['u1-reality'] == by_tag['u1-ssh-entry'] == NAME
        xray = svc.build_xray_config(user(), CASCADE)
        addresses = [v['address'] for o in xray['outbounds'] if o.get('protocol') == 'vless'
                     for v in o['settings']['vnext']]
        assert addresses == [NAME]

    def test_the_ru_zone_keeps_a_host_of_its_own(self):
        svc = SubscriptionService(cfg(ENTRY_PUBLIC_HOST=NAME))
        assert svc._build_ru_zone(UUID, RU_EXIT)['server'] == NAME
        assert svc._build_ru_zone(UUID, {**RU_EXIT, 'host': '198.51.100.8'})['server'] \
            == '198.51.100.8'

    def test_no_address_no_entry_servers(self):
        svc = SubscriptionService(cfg(ENTRY_NODE_IP='', ENTRY_PUBLIC_HOST=''))
        assert svc._build_reality(UUID, 'u1') is None
        assert svc._build_xhr(UUID, 'u1') is None
        assert svc._build_ru_zone(UUID, RU_EXIT) is None


# ------------------------------------------------------------- own address ---

class TestOwnAddress:

    def _server(self, **over):
        srv = WebAppServer.__new__(WebAppServer)
        srv.config = cfg(**over)
        return srv

    def test_an_ip_in_the_setting_is_ours(self):
        srv = self._server(ENTRY_PUBLIC_HOST='198.51.100.77')
        assert srv._is_own_address('198.51.100.77')
        assert srv._is_own_address(NODE)                   # the node's own still is
        assert not srv._is_own_address('198.51.100.78')

    def test_a_name_is_skipped(self):
        srv = self._server(ENTRY_PUBLIC_HOST=NAME)
        assert not srv._is_own_address('198.51.100.77')
        assert srv._is_own_address(NODE)


# ---------------------------------------------------------- settings, compose ---

class TestSettings:

    def test_settings_and_compose_carry_both(self, monkeypatch):
        from bot.config.settings import Settings
        monkeypatch.setenv('ENTRY_PUBLIC_HOST', f' {NAME} ')
        monkeypatch.setenv('HOP_TLS_IP', ' 130.49.146.10 ')
        s = Settings()
        assert (s.ENTRY_PUBLIC_HOST, s.HOP_TLS_IP) == (NAME, '130.49.146.10')
        monkeypatch.delenv('ENTRY_PUBLIC_HOST')
        monkeypatch.delenv('HOP_TLS_IP')
        s = Settings()
        assert (s.ENTRY_PUBLIC_HOST, s.HOP_TLS_IP) == ('', '')
        compose = (Path(__file__).resolve().parents[2] / 'docker-compose.yml').read_text()
        assert '- ENTRY_PUBLIC_HOST=${ENTRY_PUBLIC_HOST:-}' in compose
        assert '- HOP_TLS_IP=${HOP_TLS_IP:-}' in compose
        example = (Path(__file__).resolve().parents[2] / '.env.example').read_text()
        assert 'ENTRY_PUBLIC_HOST=' in example and 'HOP_TLS_IP=' in example

    @pytest.mark.parametrize('name', ['ENTRY_PUBLIC_HOST', 'HOP_TLS_IP'])
    def test_the_dashboard_may_show_them(self, name):
        src = (Path(__file__).resolve().parents[2] / 'bot/core/web_server.py').read_text()
        start = src.index('public_keys = [')
        assert f"'{name}'" in src[start:src.index(']', start)]
