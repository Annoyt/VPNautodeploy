"""Turbo Hy2 variant (second hysteria instance, Brutal CC honoured).

Covers the share-link generator, the sing-box outbound (real
up_mbps/down_mbps hints), tier gating and the calls-selector inclusion.
"""
from types import SimpleNamespace

from bot.services.subscription import SubscriptionService
from bot.services.vpn import VPNService
from bot.handlers.callbacks.user import MyKeyAnswerHandler

UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
EMAIL = 'user_paidguy_123@nekovo.ru'


def make_config(**over):
    base = dict(
        BOT_TOKEN='tok',
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
        HY2T_PORT='8402',
        HY2T_HOP_PORTS='40001:50000',
        HY2T_UP_MBPS=20,
        HY2T_DOWN_MBPS=60,
        WS_HOST='',
        WS2_HOST='',
        STLS_HOST='',
        FALLBACK_NODE_HOST='',
    )
    base.update(over)
    return SimpleNamespace(**base)


def make_user(status='paid'):
    return SimpleNamespace(
        status=status, uuid=UUID, email=EMAIL, chat_id='123', lang='ru',
    )


class TestShareLink:
    def test_turbo_link_has_own_port_range_and_bandwidth(self):
        vpn = VPNService(make_config())
        link = vpn.generate_hy2t_link(UUID, EMAIL)
        assert link.startswith(f'hysteria2://{UUID}@hy.example.com:8402?')
        assert 'mport=40001-50000' in link
        assert 'upmbps=20' in link and 'downmbps=60' in link
        assert 'obfs=salamander' in link and 'obfs-password=obfs-pw' in link
        assert link.endswith('#user_paidguy_123-hy2t')

    def test_main_link_unchanged_by_turbo(self):
        vpn = VPNService(make_config())
        link = vpn.generate_hy2_link(UUID, EMAIL)
        assert ':8400?' in link
        assert 'mport=20000-40000' in link
        assert 'upmbps' not in link
        assert link.endswith('#user_paidguy_123-hy2')

    def test_no_turbo_port_disables_variant(self):
        vpn = VPNService(make_config(HY2T_PORT=''))
        assert vpn.generate_hy2t_link(UUID, EMAIL) is None

    def test_bad_turbo_port_disables_variant(self):
        vpn = VPNService(make_config(HY2T_PORT='oops'))
        assert vpn.generate_hy2t_link(UUID, EMAIL) is None

    def test_build_links_contains_both_hy2_variants(self):
        svc = SubscriptionService(make_config())
        body = svc.build_links(make_user(), ('hy2', 'hy2t'))
        lines = body.splitlines()
        assert any(l.endswith('-hy2') for l in lines)
        assert any(l.endswith('-hy2t') for l in lines)


class TestSingboxOutbound:
    def test_turbo_outbound_carries_brutal_hints(self):
        svc = SubscriptionService(make_config())
        cfg = svc.build_singbox_config(make_user(), ('hy2', 'hy2t'))
        obs = {o['tag']: o for o in cfg['outbounds'] if o.get('type') == 'hysteria2'}
        turbo = obs['user_paidguy_123-hy2t']
        assert turbo['server_port'] == 8402
        assert turbo['up_mbps'] == 20 and turbo['down_mbps'] == 60
        assert turbo['server_ports'] == ['40001:50000']
        assert turbo['obfs']['type'] == 'salamander'
        main = obs['user_paidguy_123-hy2']
        assert 'up_mbps' not in main and 'down_mbps' not in main
        assert main['server_port'] == 8400

    def test_turbo_joins_calls_selector(self):
        svc = SubscriptionService(make_config())
        cfg = svc.build_singbox_config(make_user(), ('hy2', 'hy2t'))
        calls = next(o for o in cfg['outbounds'] if o.get('tag') == 'calls')
        assert 'user_paidguy_123-hy2' in calls['outbounds']
        assert 'user_paidguy_123-hy2t' in calls['outbounds']

    def test_turbo_disabled_is_skipped_silently(self):
        svc = SubscriptionService(make_config(HY2T_PORT=''))
        cfg = svc.build_singbox_config(make_user(), ('hy2', 'hy2t'))
        tags = [o.get('tag', '') for o in cfg['outbounds']]
        assert 'user_paidguy_123-hy2' in tags
        assert 'user_paidguy_123-hy2t' not in tags


class TestCascade:
    def test_hy2t_registered_and_paid_gated(self):
        assert MyKeyAnswerHandler.PROTOCOL_METHOD_MAP['hy2t'] == 'generate_hy2t_link'
        assert MyKeyAnswerHandler.PROTOCOL_TIER['hy2t'] == 'paid'
        assert 'hy2t' in MyKeyAnswerHandler.DEFAULT_CASCADE_ORDER

    def test_free_tier_never_sees_hy2t(self):
        order = MyKeyAnswerHandler.get_cascade_order(
            db=None, user=SimpleNamespace(status='demo', last_asn=None,
                                          last_country=None))
        assert 'hy2t' not in order
        assert 'hy2' not in order

    def test_paid_tier_sees_hy2t(self):
        order = MyKeyAnswerHandler.get_cascade_order(
            db=None, user=SimpleNamespace(status='paid', last_asn=None,
                                          last_country=None))
        assert 'hy2t' in order

    def test_country_override_still_appends_hy2t(self):
        """KZ ladder predates hy2t — the projection must append it."""
        order = MyKeyAnswerHandler.get_cascade_order(
            db=None, user=SimpleNamespace(status='paid', last_asn=None,
                                          last_country='KZ'))
        assert order[0] == 'reality'
        assert 'hy2t' in order
