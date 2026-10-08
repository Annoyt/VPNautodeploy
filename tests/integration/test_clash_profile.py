"""Main profile in Clash/mihomo form for FlClash — build_clash_config.

Hiddify keeps only a sing-box profile's outbounds; FlClash applies a
Clash profile as is. These pin that the Clash form carries the SAME
servers as the sing-box profile (proxies are converted from the sing-box
outbounds, one source of truth) and the same rule order.
"""

import json
from types import SimpleNamespace

import pytest

from bot.services.subscription import SubscriptionService

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
ALL = ('reality', 'hy2', 'ws', 'stls')
FALLBACK = dict(FALLBACK_NODE_HOST='198.51.100.9', FALLBACK_NODE_PBK='de-pbk',
                FALLBACK_NODE_SID='de01', FALLBACK_NODE_SNI='www.google.com')


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
        SS_USER_SALT='salt',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def _user(status='demo'):
    return SimpleNamespace(uuid=UUID, email='u1@x', status=status, lang='ru', chat_id='1')


def _build(protocols=ALL, user=None, config=None, **kw):
    svc = SubscriptionService(config or _config())
    txt = svc.build_clash_config(user or _user(), protocols, **kw)
    return json.loads(txt), txt


def _proxy(cfg, suffix):
    return next(p for p in cfg['proxies'] if p['name'].endswith(suffix))


class TestServers:

    def test_same_servers_as_singbox(self):
        for status, config in (('demo', _config()), ('paid', _config(**FALLBACK))):
            user = _user(status)
            svc = SubscriptionService(config)
            singbox = svc.build_singbox_config(user, ALL)
            selector = next(o for o in singbox['outbounds'] if o['tag'] == 'proxy')
            clash = json.loads(svc.build_clash_config(user, ALL))
            assert [p['name'] for p in clash['proxies']] == selector['outbounds'][1:]

    def test_reality(self):
        p = _proxy(_build()[0], '-reality')
        assert (p['type'], p['server'], p['port'], p['uuid']) == (
            'vless', '203.0.113.20', 8443, UUID)
        assert p['flow'] == 'xtls-rprx-vision' and p['packet-encoding'] == 'xudp'
        assert p['servername'] == 'www.bing.com' and p['client-fingerprint'] == 'chrome'
        assert p['reality-opts'] == {'public-key': 'reality-pbk',
                                     'short-id': '0123456789abcdef'}

    def test_hy2_hopping_and_obfs(self):
        p = _proxy(_build()[0], '-hy2')
        assert (p['type'], p['password'], p['sni']) == ('hysteria2', UUID, 'hy2.example.com')
        assert p['ports'] == '443,20000-40000' and p['hop-interval'] == 30
        assert (p['obfs'], p['obfs-password']) == ('salamander', 'obfs-pw')
        assert p['alpn'] == ['h3']

    def test_ws_httpupgrade(self):
        p = _proxy(_build()[0], '-cdn-ws')
        assert (p['type'], p['server'], p['port']) == ('vmess', 'cdn.example.com', 2053)
        assert p['tls'] is True and p['servername'] == 'cdn.example.com'
        assert p['network'] == 'ws'
        assert p['ws-opts'] == {'path': '/api/v1/forecast',
                                'headers': {'Host': 'cdn.example.com'},
                                'v2ray-http-upgrade': True}

    def test_stls_is_one_tcp_only_proxy(self):
        cfg = _build()[0]
        p = _proxy(cfg, '-stls')
        assert not any(q['name'].endswith('-stls-frontend') for q in cfg['proxies'])
        assert (p['type'], p['plugin'], p['udp']) == ('ss', 'shadow-tls', False)
        assert p['plugin-opts'] == {'host': 'www.microsoft.com', 'password': 'stls-pw',
                                    'version': 3}
        singbox = SubscriptionService(_config()).build_singbox_config(_user(), ALL)
        ss = next(o for o in singbox['outbounds'] if o['tag'].endswith('-stls'))
        assert (p['cipher'], p['password']) == (ss['method'], ss['password'])

    def test_paid_gets_de_fallback(self):
        cfg = _build(user=_user('paid'), config=_config(**FALLBACK))[0]
        p = _proxy(cfg, '-de')
        assert p['server'] == '198.51.100.9' and 'flow' not in p
        assert p['reality-opts'] == {'public-key': 'de-pbk', 'short-id': 'de01'}


class TestGroups:

    def _groups(self, protocols=ALL):
        return {g['name']: g for g in _build(protocols)[0]['proxy-groups']}

    def test_vpn_auto_calls(self):
        g = self._groups()
        names = [p['name'] for p in _build()[0]['proxies']]
        assert g['VPN']['type'] == 'select' and g['VPN']['proxies'] == ['Auto'] + names
        assert g['Auto']['type'] == 'url-test' and g['Auto']['proxies'] == names
        # Hy2 present → it alone carries calls (as in the sing-box profile)
        assert [n[-4:] for n in g['Calls']['proxies']] == ['-hy2']

    def test_calls_fall_back_to_reality(self):
        assert self._groups(('reality', 'ws'))['Calls']['proxies'] == ['u1-reality']

    def test_no_udp_native_means_no_calls_group(self):
        cfg = _build(('ws', 'stls'))[0]
        assert 'Calls' not in {g['name'] for g in cfg['proxy-groups']}
        assert 'NETWORK,UDP,VPN' in cfg['rules']

    def test_empty_cascade_is_still_valid(self):
        cfg = _build(())[0]
        assert cfg['proxies'] == []
        assert cfg['proxy-groups'] == [{'name': 'VPN', 'type': 'select',
                                        'proxies': ['DIRECT']}]


class TestRulesAndDns:

    def test_rule_order_mirrors_singbox(self):
        rules = _build()[0]['rules']

        def at(prefix):
            return next(i for i, r in enumerate(rules) if r.startswith(prefix))
        quic = at('AND,((NETWORK,UDP),(DST-PORT,443)')
        udp = rules.index('NETWORK,UDP,Calls')
        tg = at('IP-CIDR,91.105.192.0/23,VPN')
        yt = rules.index('GEOSITE,youtube,VPN')
        ru = rules.index('GEOSITE,category-ru,DIRECT')
        assert quic < udp < tg < rules.index('DOMAIN-SUFFIX,max.ru,DIRECT') < yt < ru
        assert rules.index('GEOIP,RU,DIRECT') > ru
        assert rules[-1] == 'MATCH,VPN'
        assert 'IP-CIDR6,2001:67c:4e8::/48,VPN,no-resolve' in rules

    def test_always_proxy_list_matches_singbox(self):
        rules = _build()[0]['rules']
        codes = [r.split(',')[1] for r in rules if r.startswith('GEOSITE,') and r.endswith(',VPN')]
        assert codes == [t.split('-', 1)[1] for t in SubscriptionService._PROXY_RULE_SET_TAGS]

    def test_dns_normal_is_russian_resolver(self):
        dns = _build()[0]['dns']
        assert dns['nameserver'] == ['77.88.8.8', '77.88.8.1']
        assert dns['enhanced-mode'] == 'fake-ip'

    def test_dns_lockdown_goes_through_tunnel(self):
        dns = _build(lockdown=True)[0]['dns']
        assert dns['nameserver'] == ['https://1.1.1.1/dns-query#VPN']
        assert dns['proxy-server-nameserver'][0] == 'https://1.1.1.1/dns-query'

    def test_is_yaml_too(self):
        yaml = pytest.importorskip('yaml')
        cfg, txt = _build(user=_user('paid'), config=_config(**FALLBACK))
        assert yaml.safe_load(txt) == cfg


class TestConverter:

    def test_unknown_shapes_are_skipped(self):
        conv = SubscriptionService._to_clash_proxy
        base = {'tag': 'x', 'server': 'h', 'server_port': 1}
        assert conv({**base, 'type': 'tuic'}, {}) is None
        assert conv({**base, 'type': 'vmess', 'uuid': UUID,
                     'transport': {'type': 'grpc'}}, {}) is None
        assert conv({**base, 'type': 'shadowsocks', 'method': 'm', 'password': 'p',
                     'detour': 'missing'}, {}) is None

    def test_single_hop_port_and_bandwidth(self):
        ob = {'type': 'hysteria2', 'tag': 'x-hy2t', 'server': 'h', 'server_port': 8402,
              'password': 'pw', 'tls': {'server_name': 's'},
              'server_ports': ['8402:8402', '40001:50000'], 'hop_interval': '30s',
              'up_mbps': 20, 'down_mbps': 60}
        p = SubscriptionService._to_clash_proxy(ob, {})
        assert p['ports'] == '8402,40001-50000'
        assert (p['up'], p['down']) == ('20 Mbps', '60 Mbps')


# ---------- Bot: the two-client choice (Hiddify | FlClash) ----------

from unittest.mock import Mock                                      # noqa: E402

from bot.config.constants import (                                  # noqa: E402
    FLCLASH_DOWNLOAD_URL, HIDDIFY_DOWNLOAD_URL,
)
from bot.core.database import Database                              # noqa: E402
from bot.handlers.callbacks.user import build_key_delivery_message  # noqa: E402
from bot.handlers.commands import CommandHandler                    # noqa: E402
from bot.models.user import User                                    # noqa: E402

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

DOWNLOAD_ROW = [{'text': '⬇️ Hiddify', 'url': HIDDIFY_DOWNLOAD_URL},
                {'text': '⬇️ FlClash', 'url': FLCLASH_DOWNLOAD_URL}]


def _card_user(platform='android', lang='ru'):
    return SimpleNamespace(uuid=UUID, email='u1@x', status='demo', lang=lang,
                           chat_id='1', platform=platform)


def _sub_url():
    return SubscriptionService(_config()).build_subscription_url(_card_user())


class TestClientChoice:

    @pytest.mark.parametrize('lang', ['ru', 'en'])
    def test_key_card_offers_both(self, lang):
        text, kb = build_key_delivery_message(_card_user(lang=lang), _config())
        url = _sub_url()
        assert f'<code>{url}</code>' in text                     # Hiddify
        assert f'<code>{url}?format=clash</code>' in text        # FlClash
        assert text.index('Hiddify') < text.index('FlClash')     # Hiddify first
        assert 'Google Play' in text
        assert kb['inline_keyboard'][0] == DOWNLOAD_ROW
        assert kb['inline_keyboard'][-1][0]['callback_data'] == 'report_failure'

    def test_iphone_card_stays_karing_only(self):
        text, kb = build_key_delivery_message(_card_user(platform='ios'), _config())
        assert 'Karing' in text and 'FlClash' not in text and 'format=clash' not in text
        assert DOWNLOAD_ROW not in kb['inline_keyboard']

    def _sub(self, tmp_path, platform):
        db = Database(str(tmp_path / 'bot.db'))
        db._users.save(User(chat_id='1', username='u1', status='demo', uuid=UUID,
                            email='u1@x', lang='ru', platform=platform))
        bot = Mock()
        CommandHandler(bot, db, _config()).handle_sub({}, '1')
        return bot.send_message.call_args.kwargs

    def test_sub_command_offers_both(self, tmp_path):
        kw = self._sub(tmp_path, 'windows')
        url = _sub_url()
        assert f'<code>{url}</code>' in kw['text']
        assert f'<code>{url}?format=clash</code>' in kw['text']
        assert kw['reply_markup']['inline_keyboard'][0] == DOWNLOAD_ROW

    def test_sub_command_iphone_unchanged(self, tmp_path):
        kw = self._sub(tmp_path, 'ios')
        assert 'format=clash' not in kw['text']
        assert DOWNLOAD_ROW not in kw['reply_markup']['inline_keyboard']

    def test_no_play_link_for_flclash(self):
        assert 'play.google.com' not in FLCLASH_DOWNLOAD_URL
        assert FLCLASH_DOWNLOAD_URL.startswith('https://chen08209.github.io/FlClash')


# ---------- /sub?format=clash (handler) ----------

from unittest.mock import AsyncMock                                 # noqa: E402

from bot.core.web_server import WebAppServer                        # noqa: E402


class TestSubHandler:
    """Which profile /sub serves. The builders are covered above; here
    they are stubbed so only the dispatch is under test."""

    @pytest.fixture
    def server(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        db._users.save(User(chat_id='1', username='u1', status='demo', uuid=UUID,
                            email='u1@x', quota_gb=10.0))
        cfg = Mock()
        cfg.BOT_TOKEN = 'test_token'
        srv = WebAppServer(cfg, db, xui_service=Mock())
        srv.xui.get_client_traffic = AsyncMock(return_value={})
        srv.subscription.build_singbox_config = Mock(return_value={'singbox': True})
        srv.subscription.build_clash_config = Mock(return_value='{"clash": true}')
        return srv

    async def _get(self, server, query, ua='HiddifyNext/4.1.2 (android)'):
        req = Mock()
        req.match_info = {'token': server.subscription.derive_token(UUID)}
        req.rel_url = SimpleNamespace(query=query)
        req.headers = {'User-Agent': ua}
        req.remote = ''
        resp = await server.handle_subscription(req)
        assert resp.status == 200
        return resp

    @pytest.mark.asyncio
    async def test_default_is_still_singbox(self, server):
        # Hiddify / Karing keep getting exactly what they got before.
        resp = await self._get(server, {})
        assert json.loads(resp.text) == {'singbox': True}
        assert 'content-disposition' not in resp.headers
        server.subscription.build_clash_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_format_clash(self, server):
        resp = await self._get(server, {'format': 'clash'}, ua='FlClash/v0.8.90')
        assert resp.content_type == 'text/plain'
        assert json.loads(resp.text) == {'clash': True}
        assert 'NekoVPN.yaml' in resp.headers['content-disposition']
        assert resp.headers['profile-title'] == 'NekoVPN'
        server.subscription.build_singbox_config.assert_not_called()
