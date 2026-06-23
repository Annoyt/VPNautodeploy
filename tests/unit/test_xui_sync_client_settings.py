"""Tests for xui_service.py sync_client_settings_sync fix."""

import pytest
from unittest.mock import MagicMock, patch

from bot.services.xui_service import XUIService
from bot.config import Settings


class TestXUISyncClientSettings:
    """Test sync_client_settings_sync updates settings correctly."""
    
    @pytest.fixture
    def xui_service_with_db(self):
        """Create XUIService with mocked DB."""
        config = MagicMock(spec=Settings)
        config.XUI_API_URL = None
        config.XUI_DB_PATH = '/tmp/test.db'
        
        xui = XUIService(config)
        xui.db = MagicMock()
        return xui
    
    def test_sync_client_settings_updates_expiry(self, xui_service_with_db):
        """Test that expiryTime is updated correctly."""
        xui = xui_service_with_db
        
        # Setup mock inbound settings with a client
        xui.db.get_inbound_settings.return_value = {
            'clients': [
                {
                    'email': 'test@example.com',
                    'id': 'uuid-123',
                    'expiryTime': 0,
                    'limitIp': 1,
                    'totalGB': 5 * 1024**3
                }
            ]
        }
        xui.db.update_inbound_settings.return_value = True
        
        # Update expiry
        result = xui.sync_client_settings_sync('test@example.com', {
            'expiryTime': 1735689600000  # 2025-01-01
        })
        
        assert result is True
        
        # Verify update_inbound_settings was called with updated client
        call_args = xui.db.update_inbound_settings.call_args[0]
        updated_settings = call_args[0]
        client = updated_settings['clients'][0]
        assert client['expiryTime'] == 1735689600000
        assert client['limitIp'] == 1  # Unchanged
        assert client['totalGB'] == 5 * 1024**3  # Unchanged
    
    def test_sync_client_settings_updates_limit_ip(self, xui_service_with_db):
        """Test that limitIp is updated correctly."""
        xui = xui_service_with_db
        
        xui.db.get_inbound_settings.return_value = {
            'clients': [
                {
                    'email': 'test@example.com',
                    'id': 'uuid-123',
                    'expiryTime': 0,
                    'limitIp': 1,
                    'totalGB': 5 * 1024**3
                }
            ]
        }
        xui.db.update_inbound_settings.return_value = True
        
        # Update limitIp
        result = xui.sync_client_settings_sync('test@example.com', {
            'limitIp': 3
        })
        
        assert result is True
        
        call_args = xui.db.update_inbound_settings.call_args[0]
        updated_settings = call_args[0]
        client = updated_settings['clients'][0]
        assert client['limitIp'] == 3
        assert client['expiryTime'] == 0  # Unchanged
    
    def test_sync_client_settings_updates_total_gb(self, xui_service_with_db):
        """Test that totalGB is updated correctly."""
        xui = xui_service_with_db
        
        xui.db.get_inbound_settings.return_value = {
            'clients': [
                {
                    'email': 'test@example.com',
                    'id': 'uuid-123',
                    'expiryTime': 0,
                    'limitIp': 1,
                    'totalGB': 5 * 1024**3
                }
            ]
        }
        xui.db.update_inbound_settings.return_value = True
        
        # Update totalGB to 100GB
        new_total = 100 * 1024**3
        result = xui.sync_client_settings_sync('test@example.com', {
            'totalGB': new_total
        })
        
        assert result is True
        
        call_args = xui.db.update_inbound_settings.call_args[0]
        updated_settings = call_args[0]
        client = updated_settings['clients'][0]
        assert client['totalGB'] == new_total
    
    def test_sync_client_settings_client_not_found(self, xui_service_with_db):
        """Test handling when client is not found."""
        xui = xui_service_with_db
        
        xui.db.get_inbound_settings.return_value = {
            'clients': [
                {
                    'email': 'other@example.com',
                    'id': 'uuid-456',
                }
            ]
        }
        
        # Try to update non-existent client
        result = xui.sync_client_settings_sync('test@example.com', {
            'limitIp': 3
        })
        
        assert result is False
    
    def test_sync_client_settings_no_db(self, xui_service_with_db):
        """Test handling when DB is not available."""
        xui = xui_service_with_db
        xui.db = None
        
        result = xui.sync_client_settings_sync('test@example.com', {
            'limitIp': 3
        })
        
        assert result is False
    
    def test_sync_client_settings_approve_payment_scenario(self, xui_service_with_db):
        """Test the scenario used in approve_payment: expiry + limitIp update."""
        xui = xui_service_with_db
        
        xui.db.get_inbound_settings.return_value = {
            'clients': [
                {
                    'email': 'test@example.com',
                    'id': 'uuid-123',
                    'expiryTime': 0,
                    'limitIp': 1,
                    'totalGB': 5 * 1024**3,
                    'enable': True
                }
            ]
        }
        xui.db.update_inbound_settings.return_value = True
        
        # This is what approve_payment does
        expiry_ts = 1735689600000  # Some future date
        result = xui.sync_client_settings_sync('test@example.com', {
            'expiryTime': expiry_ts,
            'limitIp': 3
        })
        
        assert result is True
        
        call_args = xui.db.update_inbound_settings.call_args[0]
        updated_settings = call_args[0]
        client = updated_settings['clients'][0]
        
        # Both should be updated
        assert client['expiryTime'] == expiry_ts
        assert client['limitIp'] == 3
        # Should not update traffic!
        assert 'upload' not in client
        assert 'download' not in client
