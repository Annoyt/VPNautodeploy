"""Share-links subscription format (Happ / v2rayNG per-server list).

Served as PLAIN TEXT lines (not base64) — Happ iOS silently imports
nothing from a base64 blob; every other client accepts plain lines too.
"""
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
        HY2_HOST='hy.example.com',
        HY2_PORT=8400,
        HY2_SNI='hy.example.com',
        HY2_OBFS_PASSWORD='obfs-pw',
        HY2_HOP_PORTS='20000:40000',
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


def decode(body: str) -> list[str]:
    return body.splitlines()


class TestLinksFormat:
    def test_all_protocols_present_as_separate_links(self):
        out = SubscriptionService(make_config()).build_links(
            make_user(), ('reality', 'hy2', 'ws', 'xhttp'),
        )
        links = decode(out)
        # xhttp retired 2026-08-19: even if a stored cascade still names
        # it, no link is emitted. 3 live protocols + DE fallback (paid).
        assert len(links) == 4
        schemes = [l.split('://')[0] for l in links]
        assert schemes == ['vless', 'hysteria2', 'vmess', 'vless']

    def test_reality_link_params(self):
        out = SubscriptionService(make_config()).build_links(
            make_user(), ('reality',),
        )
        link = decode(out)[0]
        assert link.startswith(f'vless://{UUID}@10.0.0.1:8443?')
        assert 'security=reality' in link
        assert 'sni=www.bing.com' in link
        assert 'pbk=pbk-main' in link
        assert 'sid=037a08d118bfaafd' in link
        assert 'flow=xtls-rprx-vision' in link

    def test_hy2_link_has_obfs_and_hop(self):
        out = SubscriptionService(make_config()).build_links(
            make_user(), ('hy2',),
        )
        link = decode(out)[0]
        assert link.startswith('hysteria2://')
        assert 'obfs=salamander' in link
        assert 'obfs-password=obfs-pw' in link
        assert 'mport=20000-40000' in link

    def test_paid_gets_de_fallback_link(self):
        out = SubscriptionService(make_config()).build_links(
            make_user(status='paid'), ('reality',),
        )
        links = decode(out)
        de = [l for l in links if l.endswith('-de')]
        assert len(de) == 1
        assert de[0].startswith(f'vless://{UUID}@203.0.113.5:443?')
        assert 'pbk=pbk-de' in de[0]
        assert 'flow=' not in de[0]

    def test_demo_gets_no_de_link(self):
        out = SubscriptionService(make_config()).build_links(
            make_user(status='demo'), ('reality',),
        )
        assert not any(l.endswith('-de') for l in decode(out))

    def test_missing_protocol_config_skipped(self):
        out = SubscriptionService(
            make_config(HY2_HOST='', WS_HOST='', WS2_HOST=''),
        ).build_links(make_user(), ('reality', 'hy2', 'ws', 'xhttp'))
        links = decode(out)
        assert all('hy.example.com' not in l for l in links)
        assert all('cdn.example.com' not in l for l in links)
