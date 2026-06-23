"""Tests for unified X-UI service"""

import json
import os
import sqlite3
import tempfile
from unittest.mock import Mock, patch

import pytest

from bot.services.xui_service import XUIService


class TestXUIService:
    """Tests for unified XUIService"""
    
    @pytest.fixture
    def temp_db(self):
        """Create temporary X-UI database for testing"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            xui_db_path = f.name
        
        # Initialize X-UI DB with inbounds table
        conn = sqlite3.connect(xui_db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE inbounds (
                id INTEGER PRIMARY KEY,
                protocol TEXT,
                port INTEGER,
                settings TEXT
            )
        ''')
        # Insert test inbound
        settings = json.dumps({
            'clients': []
        })
        c.execute("INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)", (1, 'vless', 443, settings))
        conn.commit()
        conn.close()
        
        yield xui_db_path
        
        # Cleanup
        os.unlink(xui_db_path)
    
    @pytest.fixture
    def xui_service(self, temp_db):
        """Create XUIService instance"""
        return XUIService(db_path=temp_db)
    
    def test_get_inbound_settings(self, xui_service):
        """Test getting inbound settings"""
        settings = xui_service.db.get_inbound_settings(1)
        
        assert settings is not None
        assert 'clients' in settings
        assert settings['clients'] == []
    
    def test_get_inbound_settings_not_found(self, xui_service):
        """Test getting non-existent inbound"""
        settings = xui_service.db.get_inbound_settings(999)
        
        assert settings is None
    
    def test_update_inbound_settings(self, xui_service):
        """Test updating inbound settings"""
        new_settings = {
            'clients': [{'id': 'test-uuid', 'email': 'test@test.com'}]
        }
        
        result = xui_service.db.update_inbound_settings(new_settings, 1)
        
        assert result is True
        
        # Verify
        settings = xui_service.db.get_inbound_settings(1)
        assert len(settings['clients']) == 1
        assert settings['clients'][0]['email'] == 'test@test.com'
    
    def test_add_client_sync(self, xui_service):
        """Test adding client to X-UI (sync)"""
        client = {
            'id': 'test-uuid-123',
            'email': 'user_test@nekovo.ru',
            'flow': 'xtls-rprx-vision',
            'enable': True
        }
        
        result = xui_service.add_client_sync(client)
        
        assert result is True
        
        # Verify client was added
        settings = xui_service.db.get_inbound_settings(1)
        assert len(settings['clients']) == 1
        assert settings['clients'][0]['email'] == 'user_test@nekovo.ru'
    
    def test_add_client_sync_adds_decryption_none(self, xui_service):
        """Test that add_client_sync ensures decryption=none for VLESS."""
        client = {
            'id': 'test-uuid-123',
            'email': 'user_test@nekovo.ru',
            'flow': 'xtls-rprx-vision',
            'enable': True
        }
        
        result = xui_service.add_client_sync(client)
        
        assert result is True
        settings = xui_service.db.get_inbound_settings(1)
        assert settings.get('decryption') == 'none'
    
    def test_add_client_duplicate_email_replaces(self, xui_service):
        """Test that adding client with same email replaces existing"""
        client1 = {
            'id': 'uuid-1',
            'email': 'user@test.com',
            'enable': True
        }
        client2 = {
            'id': 'uuid-2',
            'email': 'user@test.com',
            'enable': True
        }
        
        xui_service.add_client_sync(client1)
        result = xui_service.add_client_sync(client2)
        
        assert result is True
        
        # Should only have one client with updated UUID
        settings = xui_service.db.get_inbound_settings(1)
        assert len(settings['clients']) == 1
        assert settings['clients'][0]['id'] == 'uuid-2'
    
    @patch('bot.services.xui_reload.reload_xray', return_value=True)
    def test_remove_client_sync(self, mock_reload, xui_service):
        """Test removing client from X-UI (sync)"""
        # Add a client first
        client = {
            'id': 'test-uuid',
            'email': 'remove_me@nekovo.ru',
            'enable': True
        }
        xui_service.add_client_sync(client)
        
        # Remove it
        result = xui_service.remove_client_sync('remove_me@nekovo.ru')
        
        assert result is True
        
        # Verify removed
        settings = xui_service.db.get_inbound_settings(1)
        assert len(settings['clients']) == 0
    
    @patch('bot.services.xui_reload.reload_xray', return_value=True)
    def test_remove_client_not_found(self, mock_reload, xui_service):
        """Test removing non-existent client"""
        result = xui_service.remove_client_sync('nonexistent@nekovo.ru')
        
        assert result is False
    
    @patch('bot.services.xui_reload.reload_xray')
    def test_reload_xray_sync_success(self, mock_reload, xui_service):
        """Test XRay reload success"""
        mock_reload.return_value = True
        
        result = xui_service.reload_xray_sync()
        
        assert result is True
    
    @patch('bot.services.xui_reload.reload_xray')
    def test_reload_xray_sync_failure(self, mock_reload, xui_service):
        """Test XRay reload failure"""
        mock_reload.return_value = False
        
        result = xui_service.reload_xray_sync()
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_sync_user(self, xui_service):
        """Test syncing user to X-UI"""
        with patch('bot.services.xui_reload.reload_xray', return_value=True):
            client_config = {
                'id': 'sync-test-uuid',
                'email': 'sync_test@nekovo.ru',
                'flow': 'xtls-rprx-vision',
                'enable': True
            }
            
            result = await xui_service.sync_user('123456', client_config)
            
            assert result is True
            
            # Verify in X-UI DB
            settings = xui_service.db.get_inbound_settings(1)
            assert any(c['email'] == 'sync_test@nekovo.ru' for c in settings['clients'])
    
    @pytest.mark.asyncio
    async def test_sync_user_already_exists(self, xui_service):
        """Test syncing user that already exists (should replace)"""
        # Add client first
        client1 = {
            'id': 'uuid-1',
            'email': 'already@nekovo.ru',
            'enable': True
        }
        xui_service.add_client_sync(client1)
        
        # Sync with same email but different UUID
        with patch('bot.services.xui_reload.reload_xray', return_value=True):
            client2 = {
                'id': 'uuid-2',
                'email': 'already@nekovo.ru',
                'enable': True
            }
            result = await xui_service.sync_user('123456', client2)
            assert result is True
            
            # Verify UUID was updated
            settings = xui_service.db.get_inbound_settings(1)
            assert settings['clients'][0]['id'] == 'uuid-2'
    
    @pytest.mark.asyncio
    async def test_sync_user_reload_fails(self, xui_service):
        """Test sync when XRay reload fails"""
        with patch('bot.services.xui_reload.reload_xray', return_value=False):
            client_config = {
                'id': 'fail-test-uuid',
                'email': 'fail_test@nekovo.ru',
                'enable': True
            }
            
            result = await xui_service.sync_user('123456', client_config)
            
            # Should return False (client added but reload failed)
            assert result is False
    
    def test_get_client_traffic_no_data(self, xui_service):
        """Test getting traffic for client with no data"""
        traffic = xui_service.db.get_client_traffic('nonexistent@nekovo.ru')
        
        assert traffic is None
    
    def test_get_all_traffic_empty(self, xui_service):
        """Test getting all traffic when no clients"""
        traffic = xui_service.get_all_traffic()
        
        assert traffic == {}
    
    @patch('bot.services.xui_reload.reload_xray', return_value=True)
    def test_remove_client_convenience_method(self, mock_reload, xui_service):
        """Test remove_client convenience method (adds reload)"""
        # Add a client first
        client = {
            'id': 'test-uuid',
            'email': 'remove_me@nekovo.ru',
            'enable': True
        }
        xui_service.add_client_sync(client)
        
        # Remove via convenience method (includes reload).
        # add_client_sync, remove_client_sync and remove_client each reload.
        result = xui_service.remove_client('remove_me@nekovo.ru')

        assert result is True
        assert mock_reload.call_count == 3
