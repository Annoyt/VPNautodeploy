"""Tests for database migrations"""

import pytest
from bot.core.database import Database

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)


class TestMigrationV3:
    """Tests for migration v3 (XRay-bot integration)"""
    
    def test_new_tables_created(self, mock_bot_db):
        """Test that all v3 tables are created"""
        conn = mock_bot_db._connect()
        c = conn.cursor()
        
        tables = [
            'static_profiles',
            'subscriptions', 
            'traffic_log',
            'notification_log',
            'xui_api_config'
        ]
        
        for table in tables:
            c.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            assert c.fetchone() is not None, f"Table {table} not created"
        
        conn.close()
    
    def test_xui_api_config_defaults(self, mock_bot_db):
        """Test xui_api_config has default values"""
        config = mock_bot_db.get_xui_api_config()
        
        assert config['base_url'] == 'http://127.0.0.1:2053'
        assert config['username'] == 'admin'
        assert config['password'] == 'admin'
        assert config['use_api'] is False
        assert config['inbound_id'] == 1
    
    def test_static_profile_crud(self, mock_bot_db):
        """Test static profile create and read"""
        # Create
        result = mock_bot_db.create_static_profile(
            name='Premium-EU-01',
            vless_url='vless://test@example.com',
            email='profile@nekovo.ru',
            max_users=10
        )
        assert result is True
        
        # Read
        profile = mock_bot_db.get_static_profile('Premium-EU-01')
        assert profile is not None
        assert profile['name'] == 'Premium-EU-01'
        assert profile['max_users'] == 10
        assert profile['current_users'] == 0
        assert profile['enabled'] is True
    
    def test_subscription_crud(self, mock_bot_db):
        """Test subscription create and read"""
        # Create user first
        from bot.models import User
        mock_bot_db.save_user(User(chat_id='123456', status='demo'))
        
        # Create subscription
        result = mock_bot_db.create_subscription(
            chat_id='123456',
            plan_type='demo',
            expiry_days=7,
            traffic_gb=5.0
        )
        assert result is True
        
        # Read
        sub = mock_bot_db.get_subscription('123456')
        assert sub is not None
        assert sub['plan_type'] == 'demo'
        assert sub['traffic_limit_gb'] == 5.0
        assert sub['is_active'] is True
    
    def test_notification_tracking(self, mock_bot_db):
        """Test notification tracking"""
        # Mark notification
        result = mock_bot_db.mark_notified('123456', 'expiry_24h')
        assert result is True
        
        # Check it was sent
        was_sent = mock_bot_db.was_notified('123456', 'expiry_24h')
        assert was_sent is True
        
        # Check different type wasn't sent
        was_sent = mock_bot_db.was_notified('123456', 'expired')
        assert was_sent is False
    
    def test_traffic_history(self, mock_bot_db):
        """Test traffic history recording"""
        result = mock_bot_db.record_traffic(
            email='test@example.com',
            upload=1024,
            download=2048
        )
        assert result is True
