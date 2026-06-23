"""Tests for XUIDatabase (direct 3X-UI DB access)"""

import json
import os
import sqlite3
import tempfile

import pytest

from bot.services.xui_db import XUIDatabase


class TestXUIDatabase:
    """Tests for XUIDatabase class"""
    
    @pytest.fixture
    def xui_db(self):
        """Create temporary X-UI database for testing"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        # Initialize with X-UI schema
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        # inbounds table
        c.execute('''
            CREATE TABLE inbounds (
                id INTEGER PRIMARY KEY,
                protocol TEXT,
                port INTEGER,
                settings TEXT
            )
        ''')
        
        # client_traffics table
        c.execute('''
            CREATE TABLE client_traffics (
                id INTEGER PRIMARY KEY,
                email TEXT,
                up INTEGER DEFAULT 0,
                down INTEGER DEFAULT 0,
                total INTEGER DEFAULT 0
            )
        ''')
        
        conn.commit()
        conn.close()
        
        db = XUIDatabase(db_path)
        yield db
        
        # Cleanup
        os.unlink(db_path)
    
    def test_get_vless_inbound_id_found(self, xui_db):
        """Test finding VLESS inbound ID"""
        # Insert test inbound
        conn = sqlite3.connect(xui_db.db_path)
        c = conn.cursor()
        settings = json.dumps({'clients': []})
        c.execute(
            "INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)",
            (1, 'vless', 443, settings)
        )
        conn.commit()
        conn.close()
        
        result = xui_db.get_vless_inbound_id()
        assert result == 1
    
    def test_get_vless_inbound_id_not_found(self, xui_db):
        """Test when no VLESS inbound exists"""
        result = xui_db.get_vless_inbound_id()
        assert result is None
    
    def test_get_vless_inbound_id_multiple(self, xui_db):
        """Test finding first VLESS inbound when multiple exist"""
        conn = sqlite3.connect(xui_db.db_path)
        c = conn.cursor()
        settings = json.dumps({'clients': []})
        
        # Insert multiple VLESS inbounds
        c.execute(
            "INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)",
            (2, 'vless', 8443, settings)
        )
        c.execute(
            "INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)",
            (1, 'vless', 443, settings)
        )
        conn.commit()
        conn.close()
        
        # Should return the one with lowest port (443)
        result = xui_db.get_vless_inbound_id()
        assert result == 1
    
    def test_get_inbound_settings_with_id(self, xui_db):
        """Test getting inbound settings with explicit ID"""
        conn = sqlite3.connect(xui_db.db_path)
        c = conn.cursor()
        settings = json.dumps({'clients': [{'id': 'test-uuid'}]})
        c.execute(
            "INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)",
            (1, 'vless', 443, settings)
        )
        conn.commit()
        conn.close()
        
        result = xui_db.get_inbound_settings(1)
        assert result is not None
        assert result['clients'][0]['id'] == 'test-uuid'
    
    def test_get_inbound_settings_auto_detect(self, xui_db):
        """Test auto-detection of VLESS inbound"""
        conn = sqlite3.connect(xui_db.db_path)
        c = conn.cursor()
        settings = json.dumps({'clients': []})
        c.execute(
            "INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)",
            (5, 'vless', 443, settings)
        )
        conn.commit()
        conn.close()
        
        # Call without specifying inbound_id
        result = xui_db.get_inbound_settings()
        assert result is not None
    
    def test_get_inbound_settings_not_found(self, xui_db):
        """Test getting settings for non-existent inbound"""
        result = xui_db.get_inbound_settings(999)
        assert result is None
    
    def test_get_inbound_settings_no_vless(self, xui_db):
        """Test auto-detect when no VLESS inbound exists"""
        result = xui_db.get_inbound_settings()
        assert result is None
    
    def test_update_inbound_settings(self, xui_db):
        """Test updating inbound settings"""
        # Setup initial data
        conn = sqlite3.connect(xui_db.db_path)
        c = conn.cursor()
        settings = json.dumps({'clients': []})
        c.execute(
            "INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)",
            (1, 'vless', 443, settings)
        )
        conn.commit()
        conn.close()
        
        # Update settings
        new_settings = {'clients': [{'id': 'new-uuid', 'email': 'test@test.com'}]}
        result = xui_db.update_inbound_settings(new_settings, 1)
        
        assert result is True
        
        # Verify
        conn = sqlite3.connect(xui_db.db_path)
        c = conn.cursor()
        c.execute("SELECT settings FROM inbounds WHERE id = 1")
        row = c.fetchone()
        conn.close()
        
        saved_settings = json.loads(row[0])
        assert saved_settings['clients'][0]['email'] == 'test@test.com'
    
    def test_update_inbound_settings_auto_detect(self, xui_db):
        """Test updating with auto-detected inbound"""
        conn = sqlite3.connect(xui_db.db_path)
        c = conn.cursor()
        settings = json.dumps({'clients': []})
        c.execute(
            "INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)",
            (1, 'vless', 443, settings)
        )
        conn.commit()
        conn.close()
        
        new_settings = {'clients': [{'id': 'auto-uuid'}]}
        result = xui_db.update_inbound_settings(new_settings)
        
        assert result is True
    
    def test_update_inbound_settings_not_found(self, xui_db):
        """Test updating non-existent inbound - SQLite UPDATE returns success even if no rows updated"""
        new_settings = {'clients': []}
        result = xui_db.update_inbound_settings(new_settings, 999)
        
        # Note: SQLite UPDATE returns True even if no rows matched
        # The method doesn't check rowcount, just that query executed without error
        # This is current behavior - may need to be changed in production
        assert result is True  # Current behavior - returns True even if no rows updated
    
    def test_get_client_traffic_found(self, xui_db):
        """Test getting traffic for existing client"""
        conn = sqlite3.connect(xui_db.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO client_traffics (email, up, down, total) VALUES (?, ?, ?, ?)",
            ('test@example.com', 1000, 2000, 3000)
        )
        conn.commit()
        conn.close()
        
        result = xui_db.get_client_traffic('test@example.com')
        
        assert result is not None
        assert result['upload'] == 1000
        assert result['download'] == 2000
        assert result['total'] == 3000
    
    def test_get_client_traffic_not_found(self, xui_db):
        """Test getting traffic for non-existent client"""
        result = xui_db.get_client_traffic('nobody@example.com')
        assert result is None
    
    def test_get_all_client_traffic(self, xui_db):
        """Test getting traffic for all clients"""
        conn = sqlite3.connect(xui_db.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO client_traffics (email, up, down, total) VALUES (?, ?, ?, ?)",
            ('user1@example.com', 1000, 2000, 3000)
        )
        c.execute(
            "INSERT INTO client_traffics (email, up, down, total) VALUES (?, ?, ?, ?)",
            ('user2@example.com', 500, 1500, 2000)
        )
        conn.commit()
        conn.close()
        
        result = xui_db.get_all_client_traffic()
        
        assert len(result) == 2
        assert result['user1@example.com']['upload'] == 1000
        assert result['user2@example.com']['download'] == 1500
    
    def test_get_all_client_traffic_empty(self, xui_db):
        """Test getting traffic when no clients exist"""
        result = xui_db.get_all_client_traffic()
        assert result == {}
    
    def test_get_all_client_traffic_error_handling(self, xui_db):
        """Test error handling when table doesn't exist"""
        # Create new DB without client_traffics table
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            bad_db_path = f.name
        
        conn = sqlite3.connect(bad_db_path)
        c = conn.cursor()
        c.execute('CREATE TABLE inbounds (id INTEGER PRIMARY KEY)')
        conn.commit()
        conn.close()
        
        bad_db = XUIDatabase(bad_db_path)
        result = bad_db.get_all_client_traffic()
        
        assert result == {}  # Should return empty dict on error
        
        os.unlink(bad_db_path)
