"""Wave E: Services and Utilities - edge cases and bug hunt."""

import pytest
import asyncio
from unittest.mock import MagicMock, Mock, patch, AsyncMock, mock_open
from datetime import datetime

from bot.services.vpn import VPNService
from bot.services.notifications import NotificationService
from bot.services.xui_service import XUIService
from bot.utils.metrics.proc_reader import read_cpu_from_proc, read_memory_from_proc, ProcStatReader
from bot.config import Platform


class TestVPNServiceEdgeCases:
    """Test VPN service edge cases."""
    
    @pytest.fixture
    def vpn_service(self):
        config = MagicMock()
        config.ENTRY_NODE_IP = '203.0.113.20'
        config.REALITY_PUBLIC_KEY = 'test_pubkey'
        config.SNI_VALUE = 'www.microsoft.com'
        config.SID_VALUE = 'test_sid'
        config.DEMO_TRAFFIC_GB = 5
        config.DEMO_DAYS = 7
        return VPNService(config)
    
    def test_validate_config_missing_entry_ip(self):
        """Test VPNService raises ConfigurationError when ENTRY_NODE_IP missing."""
        config = MagicMock()
        config.ENTRY_NODE_IP = None
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        
        with pytest.raises(Exception):
            VPNService(config)
    
    def test_generate_email_with_special_chars(self):
        """Test generate_email sanitizes special characters."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        vpn = VPNService(config)
        
        email = vpn.generate_email('123', 'user@name!#')
        assert 'username' in email
        assert '@nekovo.ru' in email
    
    def test_generate_email_none_username(self):
        """Test generate_email with None username."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        vpn = VPNService(config)
        
        email = vpn.generate_email('123', None)
        assert email == 'user_123@nekovo.ru'
    
    def test_generate_vless_link_empty_uuid(self):
        """Test generate_vless_link raises error on empty UUID."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        config.SID_VALUE = 'sid'
        vpn = VPNService(config)
        
        with pytest.raises(Exception):
            vpn.generate_vless_link('', 'user@nekovo.ru')
    
    def test_build_vless_link_with_none_sid(self):
        """Test _build_vless_link excludes sid when None."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        vpn = VPNService(config)
        
        link = vpn._build_vless_link(
            'uuid-123', 'user@test.com', '1.1.1.1', 'pk', 'sni', None, 443
        )
        assert 'vless://uuid-123@1.1.1.1:443' in link
        assert 'sid=' not in link
    
    def test_build_vless_link_email_without_at(self):
        """Test _build_vless_link handles email without @."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        vpn = VPNService(config)
        
        link = vpn._build_vless_link(
            'uuid-123', 'no-at-sign', '1.1.1.1', 'pk', 'sni', 'sid', 443
        )
        assert '#no-at-sign' in link
    
    def test_create_client_config_defaults(self):
        """Test create_client_config uses defaults from config."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        config.DEMO_TRAFFIC_GB = 10
        config.DEMO_DAYS = 14
        vpn = VPNService(config)
        
        client = vpn.create_client_config('123')
        assert client['limitIp'] == 1
        assert client['totalGB'] == 10 * 1073741824
        # expiry_days defaults to None, which means 0 (no expiry)
        assert client['expiryTime'] == 0
    
    def test_create_client_config_zero_expiry(self):
        """Test create_client_config with expiry_days=0."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        config.DEMO_TRAFFIC_GB = 5
        vpn = VPNService(config)
        
        client = vpn.create_client_config('123', expiry_days=0)
        assert client['expiryTime'] == 0
    
    def test_get_client_info_zero_traffic(self):
        """Test get_client_info with zero traffic."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        vpn = VPNService(config)
        
        info = vpn.get_client_info({'totalGB': 0, 'expiryTime': 0, 'enable': True})
        assert info['traffic_gb'] == 0.0
        assert info['expiry'] == 'No expiry'
    
    def test_get_connection_preview_invalid_url(self):
        """Test get_connection_preview with non-VLESS URL."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        vpn = VPNService(config)
        
        result = vpn.get_connection_preview('https://example.com')
        assert result['valid'] is False
    
    def test_get_connection_preview_empty_fragment(self):
        """Test get_connection_preview with no fragment."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        vpn = VPNService(config)
        
        link = 'vless://uuid@1.1.1.1:443?security=reality&sni=test.com'
        result = vpn.get_connection_preview(link)
        assert result['valid'] is True
        assert result['name'] == 'unnamed'
    
    def test_get_instructions_unknown_lang(self):
        """Test get_instructions falls back to ru for unknown language."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        vpn = VPNService(config)
        
        text = vpn.get_instructions(Platform.ANDROID, lang='zz')
        assert text is not None  # Should fallback to ru
    
    def test_generate_uuid_format(self):
        """Test generate_uuid produces valid v4 UUID."""
        config = MagicMock()
        config.ENTRY_NODE_IP = '1.1.1.1'
        config.REALITY_PUBLIC_KEY = 'pk'
        config.SNI_VALUE = 'sni'
        vpn = VPNService(config)
        
        uid = vpn.generate_uuid()
        assert len(uid) == 36
        assert uid[14] == '4'


class TestNotificationServiceEdgeCases:
    """Test notification service edge cases."""
    
    @pytest.fixture
    def notifier(self):
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.FORUM_ENABLED = False
        config.SUPER_ADMIN_ID = '1652899'
        return NotificationService(bot, db, config)
    
    def test_notify_welcome_missing_language(self, notifier):
        """BUG: notify_welcome crashes when language is not in MESSAGES."""
        with pytest.raises(KeyError):
            notifier.notify_welcome('123', lang='nonexistent')
    
    def test_notify_welcome_success(self, notifier):
        """Test notify_welcome sends message successfully."""
        result = notifier.notify_welcome('123', lang='ru')
        assert result is True
        notifier.bot.send_message.assert_called_once()
    
    def test_notify_main_menu_sends_keyboard(self, notifier):
        """Test notify_main_menu includes action keyboard."""
        result = notifier.notify_main_menu('123', lang='ru')
        assert result is True
        call = notifier.bot.send_message.call_args[1]
        assert 'reply_markup' in call
    
    def test_notify_rejected_no_reason(self, notifier):
        """Test notify_rejected uses default reason when None."""
        notifier.notify_rejected('123', reason=None)
        text = notifier.bot.send_message.call_args[1]['text']
        assert 'Not specified' in text
    
    def test_notify_new_request_forum_disabled(self, notifier):
        """Test notify_new_request sends PM when forum disabled."""
        user = MagicMock()
        user.username = 'testuser'
        user.chat_id = '123'
        notifier.config.FORUM_ENABLED = False
        
        result = notifier.notify_new_request(user)
        assert result is not None
        notifier.bot.send_message.assert_called_once()
    
    def test_notify_new_support_ticket_no_forum(self, notifier):
        """Test notify_new_support_ticket sends PM when no forum."""
        user = MagicMock()
        user.username = 'testuser'
        user.chat_id = '123'
        notifier.config.FORUM_ENABLED = False
        
        result = notifier.notify_new_support_ticket(user, 'help me')
        assert result is not None
        notifier.bot.send_message.assert_called_once()
    
    def test_forward_to_support_no_topic(self, notifier):
        """Test forward_to_support returns None when user has no topic."""
        user = MagicMock()
        user.support_topic_id = None
        
        result = notifier.forward_to_support(user, {'message_id': 1})
        assert result is None
    
    def test_reply_to_user_with_admin_confirmation(self, notifier):
        """Test reply_to_user sends confirmation to admin."""
        result = notifier.reply_to_user('123', 'Hello', admin_chat_id='999')
        assert result is True
        assert notifier.bot.send_message.call_count == 2
    
    def test_notify_payment_issue_forum_enabled(self, notifier):
        """Test notify_payment_issue uses forum topic when enabled."""
        notifier.config.FORUM_ENABLED = True
        notifier.config.FORUM_GROUP_ID = '-1001'
        notifier.config.TOPIC_PAYMENTS = 16
        user = MagicMock()
        user.username = 'testuser'
        user.chat_id = '123'
        
        notifier.notify_payment_issue(user, 'issue')
        notifier.bot.send_message_to_topic.assert_called_once()
    
    def test_notify_stats_empty_dict(self, notifier):
        """Test notify_stats with empty dict for regular user."""
        result = notifier.notify_stats('123', {}, is_admin=False)
        assert result is True
        text = notifier.bot.send_message.call_args[1]['text']
        assert 'No data' in text


class TestXUIServiceSyncWrappers:
    """Test XUIService synchronous wrapper edge cases."""
    
    @pytest.mark.asyncio
    async def test_get_client_traffic_sync_from_async_context(self):
        """Test get_client_traffic_sync works from async context via thread fallback."""
        config = MagicMock()
        config.XUI_API_URL = None
        config.XUI_DB_PATH = None
        service = XUIService(config)
        
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = service.get_client_traffic_sync('test@example.com')
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_inbound_settings_sync_from_async_context(self):
        """Test get_inbound_settings_sync works from async context via thread fallback."""
        config = MagicMock()
        config.XUI_API_URL = None
        config.XUI_DB_PATH = None
        service = XUIService(config)
        
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = service.get_inbound_settings_sync()
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_client_sync_from_async_context(self):
        """Test get_client_sync works from async context via thread fallback."""
        config = MagicMock()
        config.XUI_API_URL = None
        config.XUI_DB_PATH = None
        service = XUIService(config)
        
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = service.get_client_sync('test@example.com')
        assert result is None
    
    def test_get_client_traffic_sync_from_sync_context(self):
        """Test get_client_traffic_sync works from sync context."""
        config = MagicMock()
        config.XUI_API_URL = None
        config.XUI_DB_PATH = None
        service = XUIService(config)
        
        # From sync context (no running loop), asyncio.run() works
        # But there's no DB or API, so it returns None
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = service.get_client_traffic_sync('test@example.com')
        assert result is None
    
    def test_init_legacy_with_db(self):
        """Test legacy initialization with db_path."""
        import tempfile
        import os
        import sqlite3
        
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        try:
            conn = sqlite3.connect(db_path)
            conn.execute('CREATE TABLE client_traffics (id INTEGER PRIMARY KEY)')
            conn.commit()
            conn.close()
            
            service = XUIService(db_path=db_path, api_config=None)
            assert service.db is not None
        finally:
            os.unlink(db_path)
    
    def test_init_settings_db_missing(self):
        """Test initialization when DB path doesn't exist."""
        config = MagicMock()
        config.XUI_API_URL = 'http://127.0.0.1:2053'
        config.XUI_USERNAME = 'admin'
        config.XUI_PASSWORD = 'admin'
        config.XUI_BASE_PATH = '/'
        config.XUI_API_PATH = '/api'
        config.XUI_DB_PATH = '/nonexistent/path/xui.db'
        
        service = XUIService(config)
        assert service.db is None
        assert service.api is not None


class TestProcReaderEdgeCases:
    """Test proc_reader edge cases."""
    
    def test_read_cpu_from_proc_empty_file(self):
        """Test read_cpu_from_proc handles empty /proc/stat."""
        with patch('builtins.open', mock_open(read_data="")):
            result = read_cpu_from_proc()
        assert result == 0.0
    
    def test_read_cpu_from_proc_malformed_line(self):
        """BUG: read_cpu_from_proc could crash on 'cpu' line with no values."""
        with patch('builtins.open', mock_open(read_data="cpu\n")):
            result = read_cpu_from_proc()
        assert result == 0.0
    
    def test_read_cpu_from_proc_negative_values(self):
        """Test read_cpu_from_proc with negative-like values (should not happen but guards)."""
        # Actually /proc/stat never has negatives, but check total > 0 guard
        with patch('builtins.open', mock_open(read_data="cpu  1 1 1 0\n")):
            result = read_cpu_from_proc()
        assert result == 100.0
    
    def test_read_memory_from_proc_no_available(self):
        """Test read_memory_from_proc falls back to MemFree when MemAvailable is missing."""
        meminfo = "MemTotal: 1000 kB\nMemFree: 500 kB\n"
        with patch('builtins.open', mock_open(read_data=meminfo)):
            result = read_memory_from_proc()
        assert result == 50.0
    
    def test_proc_stat_reader_empty_file(self):
        """Test ProcStatReader handles empty /proc/stat."""
        reader = ProcStatReader()
        with patch('builtins.open', mock_open(read_data="")):
            result = reader.read_cpu_percent()
        assert result == 0.0
    
    def test_proc_stat_reader_short_cpu_line(self):
        """Test ProcStatReader handles 'cpu' line with no values."""
        reader = ProcStatReader()
        with patch('builtins.open', mock_open(read_data="cpu\n")):
            result = reader.read_cpu_percent()
        assert result == 0.0
    
    def test_proc_stat_reader_zero_total_diff(self):
        """Test ProcStatReader handles identical readings (zero diff)."""
        reader = ProcStatReader()
        mock_stat = "cpu  1000 200 300 4000\n"
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            reader.read_cpu_percent()  # baseline
        
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            result = reader.read_cpu_percent()
        assert result == 0.0
    
    def test_proc_stat_reader_clamps_to_100(self):
        """Test ProcStatReader clamps usage to 100%."""
        reader = ProcStatReader()
        stat1 = "cpu  1000 200 300 4000\n"
        stat2 = "cpu  2000 400 600 4000\n"  # idle didn't change, others doubled
        with patch('builtins.open', mock_open(read_data=stat1)):
            reader.read_cpu_percent()
        
        with patch('builtins.open', mock_open(read_data=stat2)):
            result = reader.read_cpu_percent()
        
        assert result == 100.0
    
    def test_proc_stat_reader_no_cpu_line(self):
        """Test ProcStatReader handles missing 'cpu' line."""
        reader = ProcStatReader()
        with patch('builtins.open', mock_open(read_data="cpu0 100 200 300 400\n")):
            result = reader.read_cpu_percent()
        assert result == 0.0
