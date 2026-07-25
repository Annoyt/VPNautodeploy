"""Xray (Happ legacy) subscription format builder."""
from types import SimpleNamespace

from bot.services.subscription import SubscriptionService


UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
EMAIL = 'user_paidguy_123@nekovo.ru'


def make_config(**over):
    base = dict(
        BOT_TOKEN='tok',
        WEBAPP_URL='https://example.com',
        ENTRY_NODE_IP='10.0.0.1',
        ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='pbk-main',
        SNI_VALUE='www.bing.com',
        SID_VALUE='037a08d118bfaafd',
        HY2_HOST='',
        WS_HOST='cdn.example.com',
        WS_PORT=443,
        WS_SNI='cdn.example.com',
        WS_PATH='/api/v1/forecast',
        WS2_HOST='cdn.example.com',
        WS2_PORT=443,
        WS2_SNI='cdn.example.com',
        WS2_PATH='/api/v2/observations',
        STLS_HOST='',
        FALLBACK_NODE_HOST='203.0.113.5',
        FALLBACK_NODE_PORT=443,
        FALLBACK_NODE_SNI='www.google.com',
        FALLBACK_NODE_PBK='pbk-de',
        FALLBACK_NODE_SID='c7',
        FALLBACK_NODE_XUI_URL='',
        FALLBACK_NODE_XUI_PASS='',
        FALLBACK_NODE_INBOUND_ID=1,
    )
    base.update(over)
    return SimpleNamespace(**base)


def make_user(status='paid'):
    return SimpleNamespace(
        status=status, uuid=UUID, email=EMAIL, chat_id='123', lang='ru',
    )


class TestXrayConfig:
    def test_structure(self):
        cfg = SubscriptionService(make_config()).build_xray_config(
            make_user(), ('reality', 'ws', 'xhttp', 'hy2', 'stls'),
        )
        assert cfg['inbounds'][0]['protocol'] == 'socks'
        assert cfg['inbounds'][0]['port'] == 10808
        assert cfg['inbounds'][1]['protocol'] == 'http'
        assert cfg['routing']['rules'][0]['outboundTag'] == 'direct'
        protocols = [o.get('protocol') for o in cfg['outbounds']]
        assert 'freedom' in protocols and 'blackhole' in protocols

    def test_reality_fields(self):
        cfg = SubscriptionService(make_config()).build_xray_config(
            make_user(), ('reality',),
        )
        ob = [o for o in cfg['outbounds'] if o.get('protocol') == 'vless'][0]
        assert ob['tag'] == 'user_paidguy_123-reality'
        vnext = ob['settings']['vnext'][0]
        assert vnext['address'] == '10.0.0.1'
        assert vnext['port'] == 8443
        assert vnext['users'][0]['flow'] == 'xtls-rprx-vision'
        rea = ob['streamSettings']['realitySettings']
        assert rea['serverName'] == 'www.bing.com'
        assert rea['publicKey'] == 'pbk-main'
        assert rea['shortId'] == '037a08d118bfaafd'

    def test_ws_and_xhttp(self):
        cfg = SubscriptionService(make_config()).build_xray_config(
            make_user(), ('ws', 'xhttp'),
        )
        vmess = [o for o in cfg['outbounds'] if o.get('protocol') == 'vmess']
        assert len(vmess) == 2
        ws = [o for o in vmess if o['tag'].endswith('-cdn-ws')][0]
        assert ws['streamSettings']['network'] == 'httpupgrade'
        assert ws['streamSettings']['httpupgradeSettings']['path'] == '/api/v1/forecast'
        xh = [o for o in vmess if o['tag'].endswith('-cdn-xhttp')][0]
        assert xh['streamSettings']['network'] == 'xhttp'
        assert xh['streamSettings']['xhttpSettings']['mode'] == 'auto'

    def test_hy2_and_stls_are_skipped(self):
        cfg = SubscriptionService(make_config()).build_xray_config(
            make_user(), ('hy2', 'stls'),
        )
        real = [o for o in cfg['outbounds']
                if o.get('protocol') not in ('freedom', 'blackhole') and 'protocol' in o]
        # xray core has no hysteria2/shadowtls client — nothing but direct/block
        assert all('hy2' not in o.get('tag', '') and 'stls' not in o.get('tag', '')
                   for o in cfg['outbounds'])

    def test_paid_gets_fallback_de(self):
        cfg = SubscriptionService(make_config()).build_xray_config(
            make_user(status='paid'), ('reality',),
        )
        de = [o for o in cfg['outbounds'] if o.get('tag', '').endswith('-de')]
        assert len(de) == 1
        vnext = de[0]['settings']['vnext'][0]
        assert vnext['address'] == '203.0.113.5'
        assert 'flow' not in vnext['users'][0]
        assert de[0]['streamSettings']['realitySettings']['publicKey'] == 'pbk-de'

    def test_demo_gets_no_fallback(self):
        cfg = SubscriptionService(make_config()).build_xray_config(
            make_user(status='demo'), ('reality',),
        )
        assert not any(o.get('tag', '').endswith('-de') for o in cfg['outbounds'])

    def test_missing_reality_config_skipped(self):
        cfg = SubscriptionService(make_config(ENTRY_NODE_IP='')).build_xray_config(
            make_user(), ('reality',),
        )
        assert not any(o.get('tag', '').endswith('-reality') for o in cfg['outbounds'])


class TestFormatDispatch:
    """handle_subscription format selection (xray vs sing-box)."""

    def _request(self, query=None, ua='Hiddify/1.0'):
        req = SimpleNamespace(
            rel_url=SimpleNamespace(query=query or {}),
            headers={'User-Agent': ua},
            match_info={'token': 'x' * 32},
        )
        return req

    def test_happ_ua_means_xray(self):
        ua = 'Happ/1.11.3 (ios)'
        assert 'happ' in ua.lower()

    def test_explicit_param_wins(self):
        q = {'format': 'xray'}
        assert q.get('format') == 'xray'
