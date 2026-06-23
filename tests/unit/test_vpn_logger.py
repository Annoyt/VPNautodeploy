"""Tests for vpn.py - logger import fix verification."""

import pytest
from unittest.mock import MagicMock, patch

from bot.services.vpn import VPNService, logger
from bot.config import Settings
from bot.utils.exceptions import VPNGenerationError


class TestVPNLogger:
    """Test that logger is properly imported and used."""
    
    def test_logger_defined_at_module_level(self):
        """Verify logger is defined at module import time."""
        # If this test runs, logger was successfully imported at module level
        assert logger is not None
        assert logger.name == 'bot.services.vpn'
    
    def test_generate_vless_link_logs_errors(self):
        """Test that generate_vless_link properly logs errors on failure."""
        config = MagicMock(spec=Settings)
        config.ENTRY_NODE_IP = '192.168.1.1'
        config.REALITY_PUBLIC_KEY = 'test_key'
        config.SNI_VALUE = 'test.com'
        config.SID_VALUE = 'test_sid'
        
        vpn = VPNService(config)
        
        # Test that logger.error is called when build fails
        with patch.object(logger, 'error') as mock_log_error:
            with patch.object(vpn, '_build_vless_link', side_effect=Exception('Test error')):
                with pytest.raises(VPNGenerationError):
                    vpn.generate_vless_link('test-uuid', 'test@example.com')
                
                # Verify error was logged
                mock_log_error.assert_called_once()
                assert 'Failed to generate VLESS link' in mock_log_error.call_args[0][0]


class TestVPNServiceBasic:
    """Basic functionality tests for VPNService."""
    
    def test_generate_uuid(self):
        """Test UUID generation."""
        config = MagicMock(spec=Settings)
        config.ENTRY_NODE_IP = '192.168.1.1'
        config.REALITY_PUBLIC_KEY = 'test_key'
        config.SNI_VALUE = 'test.com'
        config.SID_VALUE = 'test_sid'
        
        vpn = VPNService(config)
        uuid1 = vpn.generate_uuid()
        uuid2 = vpn.generate_uuid()
        
        # Verify UUIDs are valid and unique
        assert uuid1 != uuid2
        assert len(uuid1) == 36  # Standard UUID format
        assert '-' in uuid1
    
    def test_generate_email(self):
        """Test email generation."""
        config = MagicMock(spec=Settings)
        config.ENTRY_NODE_IP = '192.168.1.1'
        config.REALITY_PUBLIC_KEY = 'test_key'
        config.SNI_VALUE = 'test.com'
        config.SID_VALUE = 'test_sid'
        
        vpn = VPNService(config)
        
        # Without username
        email1 = vpn.generate_email('123456')
        assert email1 == 'user_123456@nekovo.ru'
        
        # With username
        email2 = vpn.generate_email('123456', 'john_doe')
        assert email2 == 'user_john_doe_123456@nekovo.ru'
        
        # With special chars in username (should be sanitized)
        email3 = vpn.generate_email('123456', 'john@doe!')
        assert email3 == 'user_johndoe_123456@nekovo.ru'
    
    def test_generate_vless_link_success(self):
        """Test successful VLESS link generation."""
        config = MagicMock(spec=Settings)
        config.ENTRY_NODE_IP = '192.168.1.1'
        config.REALITY_PUBLIC_KEY = 'test_public_key'
        config.SNI_VALUE = 'test.com'
        config.SID_VALUE = 'test_sid'
        
        vpn = VPNService(config)
        
        vless = vpn.generate_vless_link(
            client_uuid='test-uuid-1234',
            email='test@nekovo.ru'
        )
        
        # Verify VLESS format
        assert vless.startswith('vless://')
        assert 'test-uuid-1234' in vless
        assert '192.168.1.1' in vless
        assert 'test_public_key' in vless
        assert 'test.com' in vless
        assert 'test_sid' in vless
        assert 'xtls-rprx-vision' in vless
