"""Tests for VPN service"""

import pytest
from unittest.mock import Mock

from bot.config import Settings, Platform
from bot.services.vpn import VPNService


class TestVPNService:
    """Tests for VPNService"""
    
    @pytest.fixture
    def config(self):
        """Create mock config"""
        cfg = Mock(spec=Settings)
        cfg.ENTRY_NODE_IP = '203.0.113.20'
        cfg.REALITY_PUBLIC_KEY = 'test_pubkey_123'
        cfg.SNI_VALUE = 'www.microsoft.com'
        cfg.SID_VALUE = 'test_sid'
        cfg.DEMO_TRAFFIC_GB = 5
        cfg.DEMO_DAYS = 7
        return cfg
    
    @pytest.fixture
    def vpn(self, config):
        """Create VPNService instance"""
        return VPNService(config)
    
    def test_generate_uuid(self, vpn):
        """Test UUID generation"""
        uuid1 = vpn.generate_uuid()
        uuid2 = vpn.generate_uuid()
        
        # Should be valid UUID format
        assert len(uuid1) == 36
        assert uuid1.count('-') == 4
        
        # Should be unique
        assert uuid1 != uuid2
    
    def test_generate_email_with_username(self, vpn):
        """Test email generation with username"""
        email = vpn.generate_email('123456', 'testuser')
        
        assert email.startswith('user_testuser_123456@')
        assert 'nekovo.ru' in email
    
    def test_generate_email_without_username(self, vpn):
        """Test email generation without username"""
        email = vpn.generate_email('123456', None)
        
        assert email == 'user_123456@nekovo.ru'
    
    def test_generate_email_cleans_special_chars(self, vpn):
        """Test email sanitizes special characters in username"""
        email = vpn.generate_email('123456', 'test@user!name')
        
        # Should remove @ and !
        assert '@' not in email.split('@')[0]  # Only one @ in email
        assert '!' not in email
    
    def test_generate_email_handles_negative_chat_id(self, vpn):
        """Test email generation handles negative chat IDs"""
        email = vpn.generate_email('-100123456789', 'user')
        
        # Should strip the minus sign
        assert 'user_user_100123456789@' in email
    
    def test_generate_vless_link(self, vpn, config):
        """Test VLESS link generation"""
        uuid = 'test-uuid-123'
        email = 'user_test@nekovo.ru'
        
        link = vpn.generate_vless_link(uuid, email)
        
        # Should be valid VLESS URL
        assert link.startswith('vless://')
        assert uuid in link
        assert config.ENTRY_NODE_IP in link
        assert config.REALITY_PUBLIC_KEY in link
        assert config.SNI_VALUE in link
        assert 'flow=xtls-rprx-vision' in link
        assert 'security=reality' in link
    
    def test_generate_vless_link_without_sid(self, vpn):
        """Test VLESS link generation when SID is empty"""
        vpn.config.SID_VALUE = ''
        
        link = vpn.generate_vless_link('uuid', 'email')
        
        # Must include empty sid parameter for XRay Reality handshake compatibility
        assert 'sid=' in link
    
    def test_get_instructions_russian(self, vpn):
        """Test getting Russian instructions"""
        instructions = vpn.get_instructions(Platform.ANDROID, 'ru')
        
        assert len(instructions) > 0
        assert 'Hiddify' in instructions

    def test_get_instructions_english(self, vpn):
        """Test getting English instructions"""
        instructions = vpn.get_instructions(Platform.IOS, 'en')

        assert len(instructions) > 0
        assert 'Hiddify' in instructions
    
    def test_get_instructions_invalid_lang_fallback(self, vpn):
        """Test fallback to Russian for invalid language"""
        instructions = vpn.get_instructions(Platform.ANDROID, 'invalid_lang')
        
        # Should return Russian instructions
        assert len(instructions) > 0
    
    def test_create_client_config_defaults(self, vpn, config):
        """Test client config creation with defaults"""
        client = vpn.create_client_config('123456', 'testuser')
        
        assert 'id' in client
        assert len(client['id']) == 36  # UUID
        assert client['flow'] == 'xtls-rprx-vision'
        assert client['email'] == 'user_testuser_123456@nekovo.ru'
        assert client['limitIp'] == 1
        assert client['totalGB'] == config.DEMO_TRAFFIC_GB * 1024 ** 3
        assert client['expiryTime'] == 0  # No expiry by default
        assert client['enable'] is True
    
    def test_create_client_config_with_expiry(self, vpn):
        """Test client config creation with expiry"""
        client = vpn.create_client_config(
            '123456',
            'testuser',
            traffic_gb=10,
            expiry_days=30
        )
        
        assert client['totalGB'] == 10 * 1024 ** 3
        assert client['expiryTime'] > 0
    
    def test_get_client_info(self, vpn):
        """Test extracting client info"""
        client_config = {
            'id': 'test-uuid',
            'email': 'test@example.com',
            'totalGB': 5 * 1024 ** 3,
            'expiryTime': 0,
            'enable': True
        }
        
        info = vpn.get_client_info(client_config)
        
        assert info['uuid'] == 'test-uuid'
        assert info['email'] == 'test@example.com'
        assert info['traffic_gb'] == 5.0
        assert info['expiry'] == 'No expiry'
        assert info['enabled'] is True
    
    def test_get_client_info_with_expiry(self, vpn):
        """Test client info with expiry date"""
        from datetime import datetime, timedelta
        expiry_ts = int((datetime.now() + timedelta(days=30)).timestamp() * 1000)
        
        client_config = {
            'id': 'test',
            'email': 'test@test.com',
            'totalGB': 10 * 1024 ** 3,
            'expiryTime': expiry_ts,
            'enable': True
        }
        
        info = vpn.get_client_info(client_config)
        
        assert info['expiry'] != 'No expiry'
        # Should be a date string
        assert len(info['expiry']) == 10  # YYYY-MM-DD
