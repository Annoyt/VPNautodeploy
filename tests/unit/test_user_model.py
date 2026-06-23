"""Tests for User model - Phase 1 fixes verification."""

import pytest
from bot.models.user import User


class TestUserFromRow:
    """Test User.from_row() method with all fields mapped."""
    
    def test_from_row_complete_mapping(self):
        """Test that all fields are correctly mapped from database row."""
        row = {
            'chat_id': '12345',
            'username': 'testuser',
            'uuid': 'test-uuid-123',
            'email': 'test@example.com',
            'status': 'demo',
            'lang': 'en',
            'platform': 'ios',
            'support_topic_id': 42,
            'created_at': '2024-01-15T10:30:00',
            'subscription_expiry': '2024-12-31T23:59:59',
            'limit_ip': 2,
            'quota_gb': 10.5,
            'last_traffic_update': '2024-01-20T14:00:00'  # H-08 fix
        }
        
        user = User.from_row(row)
        
        assert user.chat_id == '12345'
        assert user.username == 'testuser'
        assert user.uuid == 'test-uuid-123'
        assert user.email == 'test@example.com'
        assert user.status == 'demo'
        assert user.lang == 'en'
        assert user.platform == 'ios'
        assert user.support_topic_id == 42
        assert user.created_at == '2024-01-15T10:30:00'
        assert user.subscription_expiry == '2024-12-31T23:59:59'
        assert user.limit_ip == 2
        assert user.quota_gb == 10.5
        # Critical: last_traffic_update must be mapped (H-08 fix)
        assert user.last_traffic_update == '2024-01-20T14:00:00'
    
    def test_from_row_with_none_values(self):
        """Test handling of None values in database row."""
        row = {
            'chat_id': '12345',
            'username': None,
            'uuid': None,
            'email': None,
            'status': 'new',
            'lang': 'ru',
            'platform': None,
            'support_topic_id': None,
            'created_at': '2024-01-15T10:30:00',
            'subscription_expiry': None,
            'limit_ip': None,
            'quota_gb': None,
            'last_traffic_update': None
        }
        
        user = User.from_row(row)
        
        assert user.username is None
        assert user.uuid is None
        assert user.email is None
        assert user.platform is None
        assert user.support_topic_id is None
        assert user.subscription_expiry is None
        assert user.last_traffic_update is None
        # Defaults should be applied
        assert user.limit_ip == 1  # default
        assert user.quota_gb == 5.0  # default
    
    def test_from_row_missing_last_traffic_update(self):
        """Test that missing last_traffic_update field doesn't crash."""
        row = {
            'chat_id': '12345',
            'username': 'test',
            'uuid': None,
            'email': None,
            'status': 'new',
            'lang': 'ru',
            'platform': None,
            'support_topic_id': None,
            'created_at': '2024-01-15T10:30:00',
            'subscription_expiry': None,
            'limit_ip': 1,
            'quota_gb': 5.0
            # Note: last_traffic_update is missing from row
        }
        
        # Should not raise KeyError
        user = User.from_row(row)
        assert user.last_traffic_update is None
    
    def test_from_row_with_defaults(self):
        """Test default values are applied correctly."""
        row = {
            'chat_id': '99999',
            'username': None,
            'uuid': None,
            'email': None,
            'status': 'new',
            'lang': 'ru',
            'platform': None,
            'support_topic_id': None,
            'created_at': '2024-01-01T00:00:00',
            'subscription_expiry': None,
            'limit_ip': None,
            'quota_gb': None,
            'last_traffic_update': None
        }
        
        user = User.from_row(row)
        
        assert user.limit_ip == 1  # default
        assert user.quota_gb == 5.0  # default


class TestUserModel:
    """Test User model general functionality."""
    
    def test_user_creation(self):
        """Test basic User creation."""
        user = User(chat_id='12345', username='test')
        
        assert user.chat_id == '12345'
        assert user.username == 'test'
        assert user.status == 'new'  # default
        assert user.lang == 'ru'  # default
        assert user.limit_ip == 1  # default
        assert user.quota_gb == 5.0  # default
        assert user.last_traffic_update is None
        assert user.created_at is not None  # auto-generated
    
    def test_user_post_init(self):
        """Test that __post_init__ sets created_at."""
        user = User(chat_id='12345')
        
        assert user.created_at is not None
        assert isinstance(user.created_at, str)
        assert 'T' in user.created_at  # ISO format
