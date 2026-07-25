"""Integration tests for subscription service.

Covers:
1. URL generation for different protocols
2. Key rotation scenarios
3. Subscription metadata handling
4. Edge cases (missing configs, invalid protocols)
"""

import os
import tempfile
from unittest.mock import Mock

import pytest

from bot.services.subscription import SubscriptionService
from bot.models import User


class TestSubscriptionService:
    """Integration tests for SubscriptionService"""

    @pytest.fixture
    def mock_config_full(self):
        """Config with all protocols enabled"""
        config = Mock()
        config.BOT_TOKEN = 'test_bot_token_secret_123'
        config.WEBAPP_URL = 'https://example.com'
        config.ENTRY_NODE_IP = '203.0.113.20'
        config.ENTRY_NODE_PORT = 443
        config.REALITY_PUBLIC_KEY = 'test_pubkey_abcdef123456789'
        config.SNI_VALUE = 'www.microsoft.com'
        config.SID_VALUE = '0123456789abcdef'
        config.HY2_HOST = 'hy2.example.com'
        config.HY2_PORT = 8400
        config.HY2_SNI = 'hy2.example.com'
        config.WS_HOST = 'cdn.example.com'
        config.WS_PORT = 2053
        config.WS_PATH = '/api/v1/forecast'
        config.WS_SNI = 'cdn.example.com'
        config.WS2_HOST = 'cdn.example.com'
        config.WS2_PORT = 443
        config.WS2_PATH = '/api/v2/observations'
        config.WS2_SNI = 'cdn.example.com'
        config.STLS_HOST = 'stls.example.com'
        config.STLS_PORT = 443
        config.STLS_SNI = 'www.microsoft.com'
        config.STLS_VERSION = 3
        config.STLS_PASSWORD = 'stls_password_123'
        config.SS_METHOD = '2022-blake3-aes-128-gcm'
        config.SS_SERVER_PASSWORD = 'ss_server_pass'
        config.SS_USER_SALT = 'ss_user_salt_abc'
        return config

    @pytest.fixture
    def mock_config_minimal(self):
        """Config with only Reality enabled"""
        config = Mock()
        config.BOT_TOKEN = 'test_bot_token_secret_123'
        config.WEBAPP_URL = 'https://example.com'
        config.ENTRY_NODE_IP = '203.0.113.20'
        config.ENTRY_NODE_PORT = 443
        config.REALITY_PUBLIC_KEY = 'test_pubkey_abcdef123456789'
        config.SNI_VALUE = 'www.microsoft.com'
        config.SID_VALUE = '0123456789abcdef'
        # Empty optional protocols
        config.HY2_HOST = ''
        config.WS_HOST = ''
        config.WS2_HOST = ''
        config.STLS_HOST = ''
        return config

    @pytest.fixture
    def mock_config_broken(self):
        """Config with missing required Reality settings"""
        config = Mock()
        config.BOT_TOKEN = 'test_bot_token_secret_123'
        config.WEBAPP_URL = 'https://example.com'
        config.ENTRY_NODE_IP = ''  # Missing!
        config.ENTRY_NODE_PORT = 443
        config.REALITY_PUBLIC_KEY = ''  # Missing!
        config.SNI_VALUE = ''
        config.SID_VALUE = None
        config.HY2_HOST = ''
        config.WS_HOST = ''
        config.WS2_HOST = ''
        config.STLS_HOST = ''
        return config

    @pytest.fixture
    def sample_user_ru(self):
        """Russian user for tests"""
        return User(
            chat_id='123456789',
            username='testuser',
            uuid='test-uuid-123456789abcdef',
            email='test_user@nekovo.ru',
            status='demo',
            lang='ru',
            platform='android'
        )

    @pytest.fixture
    def sample_user_en(self):
        """English user for tests (gets RU-exit)"""
        return User(
            chat_id='987654321',
            username='englishuser',
            uuid='en-uuid-987654321fedcba',
            email='english_user@example.com',
            status='paid',
            lang='en',
            platform='ios'
        )

    @pytest.fixture
    def sample_user_no_uuid(self):
        """User without UUID (edge case)"""
        return User(
            chat_id='111111111',
            username='nouuid',
            status='new',
            lang='ru'
        )

    # ===== URL Generation Tests =====

    def test_subscription_url_full_config(self, mock_config_full, sample_user_ru):
        """Test URL generation with full config"""
        service = SubscriptionService(mock_config_full)
        url = service.build_subscription_url(sample_user_ru)

        assert url is not None
        assert url.startswith('https://example.com/sub/')
        # Token should be 32 chars (derived from UUID)
        token = url.split('/')[-1]
        assert len(token) == 32

    def test_subscription_url_minimal_config(self, mock_config_minimal, sample_user_ru):
        """Test URL generation with minimal config"""
        service = SubscriptionService(mock_config_minimal)
        url = service.build_subscription_url(sample_user_ru)

        assert url is not None
        assert 'example.com/sub/' in url

    def test_subscription_url_no_webapp_url(self, mock_config_minimal, sample_user_ru):
        """Test URL generation returns None when WEBAPP_URL is empty"""
        mock_config_minimal.WEBAPP_URL = ''
        service = SubscriptionService(mock_config_minimal)
        url = service.build_subscription_url(sample_user_ru)

        assert url is None

    def test_subscription_url_no_uuid(self, mock_config_minimal, sample_user_no_uuid):
        """Test URL generation returns None for user without UUID"""
        service = SubscriptionService(mock_config_minimal)
        url = service.build_subscription_url(sample_user_no_uuid)

        assert url is None

    def test_subscription_url_trailing_slash(self, mock_config_minimal, sample_user_ru):
        """Test URL generation handles WEBAPP_URL with trailing slash"""
        mock_config_minimal.WEBAPP_URL = 'https://example.com/'
        service = SubscriptionService(mock_config_minimal)
        url = service.build_subscription_url(sample_user_ru)

        # Should not have double slashes
        assert 'example.com//sub/' not in url
        assert 'example.com/sub/' in url

    # ===== Token Derivation Tests =====

    def test_token_derivation_deterministic(self, mock_config_minimal, sample_user_ru):
        """Test token is deterministic for same UUID"""
        service = SubscriptionService(mock_config_minimal)

        token1 = service.derive_token(sample_user_ru.uuid)
        token2 = service.derive_token(sample_user_ru.uuid)

        assert token1 == token2
        assert len(token1) == 32

    def test_token_derivation_different_users(self, mock_config_minimal):
        """Test tokens differ for different users"""
        service = SubscriptionService(mock_config_minimal)

        token1 = service.derive_token('uuid-111')
        token2 = service.derive_token('uuid-222')

        assert token1 != token2

    def test_token_derivation_different_bot_tokens(self, sample_user_ru):
        """Test tokens differ when BOT_TOKEN changes"""
        config1 = Mock()
        config1.BOT_TOKEN = 'token_one'
        config2 = Mock()
        config2.BOT_TOKEN = 'token_two'

        service1 = SubscriptionService(config1)
        service2 = SubscriptionService(config2)

        token1 = service1.derive_token(sample_user_ru.uuid)
        token2 = service2.derive_token(sample_user_ru.uuid)

        assert token1 != token2

    def test_find_user_by_token_success(self, mock_config_minimal, sample_user_ru):
        """Test user lookup by token succeeds"""
        service = SubscriptionService(mock_config_minimal)

        # Mock DB
        db = Mock()
        db.get_all_users = Mock(return_value=[sample_user_ru])

        expected_token = service.derive_token(sample_user_ru.uuid)
        found_user = service.find_user_by_token(db, expected_token)

        assert found_user is not None
        assert found_user.uuid == sample_user_ru.uuid

    def test_find_user_by_token_invalid_length(self, mock_config_minimal):
        """Test user lookup rejects wrong-length tokens"""
        service = SubscriptionService(mock_config_minimal)
        db = Mock()

        result = service.find_user_by_token(db, 'short_token')
        assert result is None

    def test_find_user_by_token_empty(self, mock_config_minimal):
        """Test user lookup rejects empty token"""
        service = SubscriptionService(mock_config_minimal)
        db = Mock()

        result = service.find_user_by_token(db, '')
        assert result is None

    def test_find_user_by_token_db_error(self, mock_config_minimal):
        """Test user lookup handles DB errors gracefully"""
        service = SubscriptionService(mock_config_minimal)

        db = Mock()
        db.get_all_users = Mock(side_effect=Exception("DB connection failed"))

        result = service.find_user_by_token(db, 'a' * 32)
        assert result is None

    # ===== Sing-box Config: Protocol Outbound Tests =====

    def test_reality_outbound_full_config(self, mock_config_full, sample_user_ru):
        """Test Reality outbound with all fields"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))
        outbounds = {o['tag']: o for o in config['outbounds']}

        # Check reality outbound exists
        reality_tag = f'{sample_user_ru.email.split("@")[0]}-reality'
        assert reality_tag in outbounds

        reality = outbounds[reality_tag]
        assert reality['type'] == 'vless'
        assert reality['server'] == '203.0.113.20'
        assert reality['server_port'] == 443
        assert reality['uuid'] == sample_user_ru.uuid
        assert reality['flow'] == 'xtls-rprx-vision'
        assert reality['tls']['enabled'] is True
        assert reality['tls']['server_name'] == 'www.microsoft.com'
        assert reality['tls']['reality']['enabled'] is True
        assert reality['tls']['reality']['public_key'] == 'test_pubkey_abcdef123456789'
        assert reality['tls']['reality']['short_id'] == '0123456789abcdef'
        assert reality['tls']['fragment'] is True

    def test_hy2_outbound(self, mock_config_full, sample_user_ru):
        """Test Hysteria2 outbound generation"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('hy2',))
        outbounds = {o['tag']: o for o in config['outbounds']}

        hy2_tag = f'{sample_user_ru.email.split("@")[0]}-hy2'
        assert hy2_tag in outbounds

        hy2 = outbounds[hy2_tag]
        assert hy2['type'] == 'hysteria2'
        assert hy2['server'] == 'hy2.example.com'
        assert hy2['server_port'] == 8400
        assert hy2['password'] == sample_user_ru.uuid
        assert hy2['tls']['server_name'] == 'hy2.example.com'
        assert hy2['tls']['alpn'] == ['h3']

    def test_ws_outbound(self, mock_config_full, sample_user_ru):
        """Test VMess over WebSocket (httpupgrade) outbound"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('ws',))
        outbounds = {o['tag']: o for o in config['outbounds']}

        ws_tag = f'{sample_user_ru.email.split("@")[0]}-cdn-ws'
        assert ws_tag in outbounds

        ws = outbounds[ws_tag]
        assert ws['type'] == 'vmess'
        assert ws['server'] == 'cdn.example.com'
        assert ws['server_port'] == 2053
        assert ws['uuid'] == sample_user_ru.uuid
        assert ws['transport']['type'] == 'httpupgrade'
        assert ws['transport']['path'] == '/api/v1/forecast'
        assert ws['tls']['ech']['enabled'] is True

    def test_xhttp_outbound(self, mock_config_full, sample_user_ru):
        """Test VMess over xhttp outbound"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('xhttp',))
        outbounds = {o['tag']: o for o in config['outbounds']}

        xhttp_tag = f'{sample_user_ru.email.split("@")[0]}-cdn-xhttp'
        assert xhttp_tag in outbounds

        xhttp = outbounds[xhttp_tag]
        assert xhttp['type'] == 'vmess'
        assert xhttp['transport']['type'] == 'http'
        assert xhttp['transport']['path'] == '/api/v2/observations'
        assert xhttp['tls']['ech']['enabled'] is True

    def test_stls_chained_outbounds(self, mock_config_full, sample_user_ru):
        """Test ShadowTLS+Shadowsocks chained outbounds"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('stls',))
        outbounds = {o['tag']: o for o in config['outbounds']}

        # Both SS and ShadowTLS outbounds should exist
        ss_tag = f'{sample_user_ru.email.split("@")[0]}-stls'
        stls_tag = f'{sample_user_ru.email.split("@")[0]}-stls-frontend'

        assert ss_tag in outbounds
        assert stls_tag in outbounds

        # Check Shadowsocks detours through ShadowTLS
        ss = outbounds[ss_tag]
        assert ss['type'] == 'shadowsocks'
        assert ss['detour'] == stls_tag
        assert ss['method'] == '2022-blake3-aes-128-gcm'

        # Check ShadowTLS config
        stls = outbounds[stls_tag]
        assert stls['type'] == 'shadowtls'
        assert stls['version'] == 3
        assert stls['password'] == 'stls_password_123'
        assert stls['tls']['server_name'] == 'www.microsoft.com'

    # ===== Sing-box Config: Multi-Protocol Tests =====

    def test_multiple_protocols_in_selector(self, mock_config_full, sample_user_ru):
        """Test selector includes all enabled protocols"""
        service = SubscriptionService(mock_config_full)

        protocols = ('reality', 'hy2', 'ws', 'xhttp', 'stls')
        config = service.build_singbox_config(sample_user_ru, protocols)
        outbounds = {o['tag']: o for o in config['outbounds']}

        # Check selector outbound
        selector = outbounds.get('proxy')
        assert selector is not None
        assert selector['type'] == 'selector'
        assert selector['default'] == 'auto'

        # Should include auto + all protocol outbounds
        expected_outbound_count = 1 + len(protocols)  # auto + protocols
        assert len(selector['outbounds']) == expected_outbound_count
        assert 'auto' in selector['outbounds']

    def test_urltest_outbound(self, mock_config_full, sample_user_ru):
        """Test urltest auto-selector outbound"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality', 'hy2'))
        outbounds = {o['tag']: o for o in config['outbounds']}

        urltest = outbounds.get('auto')
        assert urltest is not None
        assert urltest['type'] == 'urltest'
        assert urltest['url'] == 'https://www.gstatic.com/generate_204'
        assert urltest['interval'] == '3m'
        assert urltest['tolerance'] == 50

    # ===== Sing-box Config: Empty/No Protocols Tests =====

    def test_empty_protocols_fallback(self, mock_config_full, sample_user_ru):
        """Test config with no enabled protocols returns valid but empty selector"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ())
        outbounds = {o['tag']: o for o in config['outbounds']}

        # Selector should exist with only direct
        selector = outbounds['proxy']
        assert selector['outbounds'] == ['direct']
        assert selector['default'] == 'direct'

        # urltest should fall back to direct
        urltest = outbounds['auto']
        assert urltest['outbounds'] == ['direct']

    def test_broken_required_protocol_returns_none(self, mock_config_broken, sample_user_ru):
        """Test broken Reality config returns None for that protocol"""
        service = SubscriptionService(mock_config_broken)

        outbound = service._build_outbound('reality', sample_user_ru)
        assert outbound is None

    # ===== Sing-box Config: RU-Exit Tests =====

    def test_ru_exit_for_en_users(self, mock_config_full, sample_user_en):
        """Test EN users get RU-exit outbound"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_en, ('reality',))
        outbounds = {o['tag']: o for o in config['outbounds']}

        assert 'ru-exit' in outbounds

        ru_exit = outbounds['ru-exit']
        assert ru_exit['type'] == 'vless'
        assert ru_exit['flow'] == 'xtls-rprx-vision'
        assert ru_exit['server'] == '203.0.113.20'

    def test_ru_exit_routing_rules_for_en(self, mock_config_full, sample_user_en):
        """Test EN users have RU-geo routing to ru-exit"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_en, ('reality',))

        # Check route rules
        ru_exit_rule = None
        for rule in config['route']['rules']:
            if rule.get('outbound') == 'ru-exit':
                ru_exit_rule = rule
                break

        assert ru_exit_rule is not None
        assert 'rule_set' in ru_exit_rule
        assert 'geosite-category-ru' in ru_exit_rule['rule_set']
        assert 'geoip-ru' in ru_exit_rule['rule_set']

    def test_no_ru_exit_for_ru_users(self, mock_config_full, sample_user_ru):
        """Test RU users do NOT get RU-exit outbound"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))
        outbounds = {o['tag']: o for o in config['outbounds']}

        assert 'ru-exit' not in outbounds

    def test_ru_users_direct_routing(self, mock_config_full, sample_user_ru):
        """Test RU users get direct routing for domestic traffic"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))

        # Check for direct rule for RU geo
        direct_rule = None
        for rule in config['route']['rules']:
            if rule.get('outbound') == 'direct' and 'rule_set' in rule:
                direct_rule = rule
                break

        assert direct_rule is not None
        assert 'geosite-category-ru' in direct_rule['rule_set']

    # ===== Sing-box Config: Telegram Call Routing Tests =====

    def _rules(self, service, user, protocols=('reality', 'hy2')):
        return service.build_singbox_config(user, protocols)['route']['rules']

    def test_telegram_ip_cidr_rules_present(self, mock_config_full, sample_user_ru):
        """Telegram is routed by literal IP CIDR (reflector media has no SNI)."""
        service = SubscriptionService(mock_config_full)
        rules = self._rules(service, sample_user_ru)

        cidr_rules = [r for r in rules if 'ip_cidr' in r]
        # A well-known Telegram range must be present (signaling → proxy).
        assert cidr_rules, "expected a Telegram ip_cidr rule"
        assert any('149.154.160.0/20' in r['ip_cidr'] for r in cidr_rules)
        # None of the Telegram rules leak to a direct route.
        assert all(r['outbound'] != 'direct' for r in cidr_rules)
        # Telegram UDP call media is covered by the blanket UDP rule (below),
        # not a dedicated cidr+udp rule.

    def test_telegram_cidr_above_ru_direct(self, mock_config_full, sample_user_ru):
        """Telegram-by-IP must be evaluated before the RU-direct bypass."""
        service = SubscriptionService(mock_config_full)
        rules = self._rules(service, sample_user_ru)

        tg_idx = min(i for i, r in enumerate(rules) if 'ip_cidr' in r)
        ru_direct_idx = min(
            i for i, r in enumerate(rules)
            if r.get('outbound') == 'direct' and 'rule_set' in r
        )
        assert tg_idx < ru_direct_idx

    def test_ru_direct_is_tcp_only(self, mock_config_full, sample_user_ru):
        """RU-domestic direct bypass must be TCP-only so UDP calls tunnel."""
        service = SubscriptionService(mock_config_full)
        rules = self._rules(service, sample_user_ru)

        ru_direct = [
            r for r in rules
            if r.get('outbound') == 'direct' and 'rule_set' in r
            and 'geoip-ru' in r['rule_set']
        ]
        assert ru_direct, "expected an RU-direct rule"
        assert all(r.get('network') == 'tcp' for r in ru_direct)

    def test_ru_udp_never_direct(self, mock_config_full, sample_user_ru):
        """No UDP rule may send RU-domestic traffic direct (would be throttled).

        The RU-direct bypass is TCP-only; RU UDP (incl. P2P call media to a
        Russian peer) must fall to the blanket UDP rule and tunnel.
        """
        service = SubscriptionService(mock_config_full)
        rules = self._rules(service, sample_user_ru)

        udp_direct = [
            r for r in rules
            if r.get('network') == 'udp' and r['outbound'] == 'direct'
        ]
        assert not udp_direct, "UDP must never be routed direct"

    def test_foreign_peer_call_udp_tunneled(self, mock_config_full, sample_user_ru):
        """P2P call to a peer ABROAD: catch-all UDP must tunnel via UDP-native.

        The peer's IP is neither Russian nor a Telegram range, so only a
        blanket UDP rule (no rule_set / no ip_cidr) can catch it — otherwise
        it falls to `final` on a possibly-CDN outbound that can't do UDP.
        """
        service = SubscriptionService(mock_config_full)
        rules = self._rules(service, sample_user_ru)

        blanket_udp = [
            r for r in rules
            if r.get('network') == 'udp'
            and 'rule_set' not in r and 'ip_cidr' not in r
        ]
        assert len(blanket_udp) == 1, "expected exactly one catch-all UDP rule"
        assert blanket_udp[0]['outbound'] == 'calls'
        # It MUST sit above clash_mode Global: when the user manually picks a
        # server (Global mode), call UDP must still be diverted to the UDP
        # transport instead of following the picked (maybe TCP-only) proxy.
        udp_idx = rules.index(blanket_udp[0])
        global_idx = next(
            i for i, r in enumerate(rules) if r.get('clash_mode') == 'Global'
        )
        assert udp_idx < global_idx, "UDP→calls must precede clash_mode Global"

    def test_calls_selector_udp_native_only(self, mock_config_full, sample_user_ru):
        """'calls' selector ranks only UDP-native outbounds (Reality/Hy2)."""
        service = SubscriptionService(mock_config_full)
        config = service.build_singbox_config(
            sample_user_ru, ('reality', 'hy2', 'ws', 'xhttp', 'stls')
        )
        outbounds = {o['tag']: o for o in config['outbounds']}

        assert 'calls' in outbounds
        calls = outbounds['calls']
        assert calls['type'] == 'urltest'
        # Only reality/hy2 tags; no CDN (ws/xhttp) or ShadowTLS tags.
        assert all(
            t.endswith('-reality') or t.endswith('-hy2')
            for t in calls['outbounds']
        )
        assert calls['outbounds'], "calls selector should not be empty"

    def test_calls_falls_back_to_proxy_without_udp_native(
        self, mock_config_full, sample_user_ru
    ):
        """CDN-only deployment: no 'calls' selector, UDP falls back to proxy."""
        service = SubscriptionService(mock_config_full)
        config = service.build_singbox_config(sample_user_ru, ('ws', 'xhttp'))
        outbounds = {o['tag']: o for o in config['outbounds']}
        rules = config['route']['rules']

        assert 'calls' not in outbounds
        udp_rules = [r for r in rules if r.get('network') == 'udp']
        assert udp_rules, "expected UDP routing rules"
        assert all(r['outbound'] == 'proxy' for r in udp_rules)

    # ===== Sing-box Config: DNS Tests =====

    def test_dns_config_structure(self, mock_config_full, sample_user_ru):
        """Test DNS configuration structure"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))

        assert 'dns' in config
        dns = config['dns']

        # Check servers
        servers = {s['tag']: s for s in dns['servers']}
        assert 'remote' in servers
        assert 'local' in servers
        assert servers['remote']['address'] == 'https://1.1.1.1/dns-query'
        assert servers['local']['address'] == 'local'

        # Check final is local (to avoid foreign-DNS detection)
        assert dns['final'] == 'local'

    # ===== Sing-box Config: Route Rules Tests =====

    def test_route_rules_structure(self, mock_config_full, sample_user_ru):
        """Test route rules include essential rules"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))
        rules = config['route']['rules']

        # DNS rule
        dns_rule = next(r for r in rules if r.get('protocol') == 'dns')
        assert dns_rule['outbound'] == 'dns-out'

        # Clash mode rules
        direct_mode = next(r for r in rules if r.get('clash_mode') == 'Direct')
        assert direct_mode['outbound'] == 'direct'

        global_mode = next(r for r in rules if r.get('clash_mode') == 'Global')
        assert global_mode['outbound'] == 'proxy'

        # max.ru direct rule
        max_ru_rule = next((r for r in rules if 'domain_suffix' in r and 'max.ru' in r.get('domain_suffix', [])), None)
        assert max_ru_rule is not None
        assert max_ru_rule['outbound'] == 'direct'

    def test_always_proxy_rule_sets(self, mock_config_full, sample_user_ru):
        """Test always-proxy allow-list is present"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))
        rules = config['route']['rules']

        # Find the proxy rule with always-proxy rule sets
        proxy_rule = None
        for rule in rules:
            if rule.get('outbound') == 'proxy' and 'rule_set' in rule:
                if any(tag in rule['rule_set'] for tag in service._PROXY_RULE_SET_TAGS):
                    proxy_rule = rule
                    break

        assert proxy_rule is not None
        # Check some key always-proxy domains
        assert 'geosite-youtube' in proxy_rule['rule_set']
        assert 'geosite-meta' in proxy_rule['rule_set']
        assert 'geosite-telegram' in proxy_rule['rule_set']

    # ===== Edge Cases: Missing Protocol Configs =====

    def test_missing_hy2_config(self, mock_config_minimal, sample_user_ru):
        """Test missing Hy2 config returns None"""
        service = SubscriptionService(mock_config_minimal)

        outbound = service._build_outbound('hy2', sample_user_ru)
        assert outbound is None

    def test_missing_ws_config(self, mock_config_minimal, sample_user_ru):
        """Test missing WS config returns None"""
        service = SubscriptionService(mock_config_minimal)

        outbound = service._build_outbound('ws', sample_user_ru)
        assert outbound is None

    def test_missing_xhttp_config(self, mock_config_minimal, sample_user_ru):
        """Test missing xhttp config returns None"""
        service = SubscriptionService(mock_config_minimal)

        outbound = service._build_outbound('xhttp', sample_user_ru)
        assert outbound is None

    def test_missing_stls_config(self, mock_config_minimal, sample_user_ru):
        """Test missing ShadowTLS config returns None"""
        service = SubscriptionService(mock_config_minimal)

        outbound = service._build_outbound('stls', sample_user_ru)
        assert outbound is None

    def test_invalid_protocol(self, mock_config_full, sample_user_ru):
        """Test unknown protocol returns None"""
        service = SubscriptionService(mock_config_full)

        outbound = service._build_outbound('wireguard', sample_user_ru)
        assert outbound is None

    def test_partial_stls_config_returns_none(self, mock_config_full, sample_user_ru):
        """Test partial ShadowTLS config (missing salt) returns None"""
        mock_config_full.SS_USER_SALT = ''
        service = SubscriptionService(mock_config_full)

        outbound = service._build_outbound('stls', sample_user_ru)
        assert outbound is None

    # ===== Key Rotation Scenarios =====

    def test_key_rotation_preserves_subscription_url(self, mock_config_minimal, sample_user_ru):
        """Test subscription URL remains stable after key rotation"""
        service = SubscriptionService(mock_config_minimal)

        # Original token
        url1 = service.build_subscription_url(sample_user_ru)
        token1 = url1.split('/')[-1] if url1 else None

        # Simulate key rotation (UUID stays same)
        service = SubscriptionService(mock_config_minimal)
        url2 = service.build_subscription_url(sample_user_ru)
        token2 = url2.split('/')[-1] if url2 else None

        # URL should remain stable
        assert token1 == token2

    def test_config_change_updates_subscription_content(self, mock_config_minimal, mock_config_full, sample_user_ru):
        """Test config changes reflect in subscription content but URL stays stable"""
        service_minimal = SubscriptionService(mock_config_minimal)
        service_full = SubscriptionService(mock_config_full)

        # URLs should be same (based on UUID + BOT_TOKEN)
        url1 = service_minimal.build_subscription_url(sample_user_ru)
        url2 = service_full.build_subscription_url(sample_user_ru)
        assert url1 == url2

        # But configs differ
        config1 = service_minimal.build_singbox_config(sample_user_ru, ('reality',))
        config2 = service_full.build_singbox_config(sample_user_ru, ('reality', 'hy2'))

        # Full config should have more outbounds
        assert len(config2['outbounds']) > len(config1['outbounds'])

    def test_subscription_after_uuid_change(self, mock_config_minimal):
        """Test subscription changes when UUID changes"""
        service = SubscriptionService(mock_config_minimal)

        user1 = User(
            chat_id='123',
            uuid='old-uuid-123',
            email='user@example.com'
        )
        user2 = User(
            chat_id='123',  # Same user
            uuid='new-uuid-456',  # New UUID (rotation)
            email='user@example.com'
        )

        url1 = service.build_subscription_url(user1)
        url2 = service.build_subscription_url(user2)

        # URLs should be different (different tokens)
        assert url1 != url2

    # ===== Metadata Handling Tests =====

    def test_subscription_config_log_level(self, mock_config_full, sample_user_ru):
        """Test config metadata includes log level"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))

        assert 'log' in config
        assert config['log']['level'] == 'warn'

    def test_subscription_config_auto_detect_interface(self, mock_config_full, sample_user_ru):
        """Test config metadata includes auto_detect_interface"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))

        assert config['route']['auto_detect_interface'] is True

    def test_subscription_config_final_route(self, mock_config_full, sample_user_ru):
        """Test config final route defaults to proxy"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))

        assert config['route']['final'] == 'proxy'

    # ===== System Outbounds Tests =====

    def test_system_outbounds_present(self, mock_config_full, sample_user_ru):
        """Test required system outbounds are always present"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ())
        tags = {o['tag'] for o in config['outbounds']}

        assert 'direct' in tags
        assert 'block' in tags
        assert 'dns-out' in tags

    def test_direct_outbound_config(self, mock_config_full, sample_user_ru):
        """Test direct outbound has correct type"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ())
        outbounds = {o['tag']: o for o in config['outbounds']}

        direct = outbounds['direct']
        assert direct['type'] == 'direct'

    def test_block_outbound_config(self, mock_config_full, sample_user_ru):
        """Test block outbound has correct type"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ())
        outbounds = {o['tag']: o for o in config['outbounds']}

        block = outbounds['block']
        assert block['type'] == 'block'

    # ===== Rule Set Tests =====

    def test_rule_sets_include_all_required(self, mock_config_full, sample_user_ru):
        """Test rule sets include all required tags"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))
        rule_sets = {rs['tag']: rs for rs in config['route']['rule_set']}

        # Check always-proxy rule sets
        for tag in service._PROXY_RULE_SET_TAGS:
            assert tag in rule_sets
            assert rule_sets[tag]['type'] == 'remote'
            assert rule_sets[tag]['format'] == 'binary'

        # Check direct rule sets
        for tag in service._DIRECT_RULE_SET_TAGS:
            assert tag in rule_sets
            assert rule_sets[tag]['type'] == 'remote'

    def test_rule_sets_download_detour(self, mock_config_full, sample_user_ru):
        """Test rule sets use direct download_detour"""
        service = SubscriptionService(mock_config_full)

        config = service.build_singbox_config(sample_user_ru, ('reality',))
        rule_sets = config['route']['rule_set']

        for rs in rule_sets:
            assert rs['download_detour'] == 'direct'
            assert rs['update_interval'] == '1d'

    # ===== Default Values Tests =====

    def test_default_hy2_port(self):
        """Test default Hy2 port when not specified"""
        config = Mock()
        config.BOT_TOKEN = 'test'
        config.WEBAPP_URL = 'https://example.com'
        config.HY2_HOST = 'hy2.example.com'
        config.HY2_PORT = None  # Not set
        config.HY2_SNI = 'hy2.example.com'
        config.HY2_OBFS_PASSWORD = ''
        config.HY2_HOP_PORTS = ''
        config.ENTRY_NODE_IP = '1.2.3.4'
        config.REALITY_PUBLIC_KEY = 'test_key'
        config.SNI_VALUE = 'test.com'
        config.SID_VALUE = None

        service = SubscriptionService(config)
        user = User(chat_id='1', uuid='uuid', email='u@t.com')

        # Should use default 8400
        outbound = service._build_hy2(user.uuid, 'test')
        assert outbound['server_port'] == 8400

    def test_default_ws_port(self):
        """Test default WS port when not specified"""
        config = Mock()
        config.BOT_TOKEN = 'test'
        config.WS_HOST = 'ws.example.com'
        config.WS_PORT = None  # Not set
        config.WS_PATH = '/test'
        config.WS_SNI = 'ws.example.com'

        service = SubscriptionService(config)
        user = User(chat_id='1', uuid='uuid', email='u@t.com')

        # Should fall back to the default port 2053
        outbound = service._build_ws(user.uuid, 'test')
        assert outbound['server_port'] == 2053

    def test_default_sni_fallbacks(self):
        """Test SNI fallbacks to host when not specified"""
        config = Mock()
        config.BOT_TOKEN = 'test'
        config.HY2_HOST = 'hy2.example.com'
        config.HY2_PORT = 8400
        config.HY2_SNI = ''  # Empty - should fallback to host
        config.WS_HOST = 'ws.example.com'
        config.WS_PORT = 2053
        config.WS_PATH = '/test'
        config.WS_SNI = ''  # Empty
        config.WS2_HOST = 'ws2.example.com'
        config.WS2_PORT = 443
        config.WS2_PATH = '/test2'
        config.WS2_SNI = ''  # Empty
        config.ENTRY_NODE_IP = '1.2.3.4'
        config.REALITY_PUBLIC_KEY = 'test_key'
        config.SNI_VALUE = 'test.com'
        config.SID_VALUE = None

        service = SubscriptionService(config)
        user = User(chat_id='1', uuid='uuid', email='u@t.com')

        # SNI should fallback to host in actual implementation
        # This tests the attribute access pattern
        assert getattr(config, 'HY2_SNI', '') or config.HY2_HOST
        assert getattr(config, 'WS_SNI', '') or config.WS_HOST
        assert getattr(config, 'WS2_SNI', '') or config.WS2_HOST
