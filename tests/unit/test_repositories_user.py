"""Tests for UserRepository."""

import pytest
import sqlite3
import tempfile
import os
from datetime import datetime

from bot.core.repositories.user import UserRepository
from bot.models import User


class TestUserRepository:
    """Test UserRepository functionality."""
    
    @pytest.fixture
    def temp_db(self):
        """Create temporary database with users table."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        # Create users table
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE users (
                chat_id TEXT PRIMARY KEY,
                username TEXT,
                previous_state TEXT,
                reject_count INTEGER DEFAULT 0,
                uuid TEXT,
                email TEXT,
                status TEXT DEFAULT 'new',
                lang TEXT DEFAULT 'ru',
                platform TEXT,
                support_topic_id INTEGER,
                created_at TEXT,
                subscription_expiry TEXT,
                limit_ip INTEGER DEFAULT 1,
                quota_gb REAL DEFAULT 5.0,
                last_traffic_update TEXT,
                xui_synced INTEGER DEFAULT 0,
                next_protocol_idx INTEGER DEFAULT 0,
                last_country TEXT,
                last_asn TEXT,
                last_city TEXT,
                last_lat REAL,
                last_lon REAL,
                contact_email TEXT
            )
        ''')
        
        # Insert test users
        c.execute('''
            INSERT INTO users 
            (chat_id, username, previous_state, reject_count, uuid, email, status, lang, platform, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', ('123', 'user1', None, 0, 'uuid1', 'user1@test.com', 'demo', 'ru', 'ios', datetime.now().isoformat()))
        
        c.execute('''
            INSERT INTO users 
            (chat_id, username, previous_state, reject_count, uuid, email, status, lang, platform, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', ('456', 'user2', None, 0, 'uuid2', 'user2@test.com', 'pending_demo', 'en', 'android', datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        yield db_path
        
        os.unlink(db_path)
    
    @pytest.fixture
    def repository(self, temp_db):
        """Create UserRepository instance."""
        return UserRepository(temp_db)
    
    def test_get_by_id_existing(self, repository):
        """Test get existing user by ID."""
        user = repository.get_by_id('123')
        
        assert user is not None
        assert user.chat_id == '123'
        assert user.username == 'user1'
        assert user.email == 'user1@test.com'
    
    def test_get_by_id_nonexistent(self, repository):
        """Test get non-existent user returns None."""
        user = repository.get_by_id('999')
        
        assert user is None
    
    def test_get_by_username_with_at(self, repository):
        """Test get user by username with @ prefix."""
        user = repository.get_by_username('@user1')
        
        assert user is not None
        assert user.chat_id == '123'
    
    def test_get_by_username_without_at(self, repository):
        """Test get user by username without @ prefix."""
        user = repository.get_by_username('user1')
        
        assert user is not None
        assert user.chat_id == '123'
    
    def test_get_by_username_case_insensitive(self, repository):
        """Test username search is case insensitive."""
        user = repository.get_by_username('USER1')
        
        assert user is not None
        assert user.chat_id == '123'
    
    def test_get_all(self, repository):
        """Test get all users."""
        users = repository.get_all()
        
        assert len(users) == 2
        assert all(isinstance(u, User) for u in users)
    
    def test_get_by_status(self, repository):
        """Test get users by status."""
        demo_users = repository.get_by_status('demo')
        
        assert len(demo_users) == 1
        assert demo_users[0].chat_id == '123'
    
    def test_get_pending(self, repository):
        """Test get pending users."""
        pending = repository.get_pending()
        
        assert len(pending) == 1
        assert pending[0].status == 'pending_demo'
    
    def test_save_new_user(self, repository):
        """Test saving new user."""
        new_user = User(
            chat_id='789',
            username='newuser',
            email='new@test.com',
            status='new',
            lang='ru',
            platform='windows'
        )
        
        result = repository.save(new_user)
        
        assert result is True
        
        # Verify saved
        saved = repository.get_by_id('789')
        assert saved is not None
        assert saved.username == 'newuser'
    
    def test_save_updates_existing(self, repository):
        """Test save updates existing user."""
        existing = repository.get_by_id('123')
        existing.username = 'updated_name'
        
        result = repository.save(existing)
        
        assert result is True
        
        # Verify updated
        updated = repository.get_by_id('123')
        assert updated.username == 'updated_name'
    
    def test_update_status(self, repository):
        """Test updating user status."""
        result = repository.update_status('123', 'banned')
        
        assert result is True
        
        # Verify updated
        user = repository.get_by_id('123')
        assert user.status == 'banned'
    
    def test_update_status_nonexistent(self, repository):
        """Test updating status of non-existent user."""
        result = repository.update_status('999', 'demo')
        
        assert result is False
    
    def test_get_stats(self, repository):
        """Test getting user statistics."""
        stats = repository.get_stats()
        
        assert stats['total'] == 2
        assert 'demo' in stats['by_status']
        assert 'pending_demo' in stats['by_status']
        assert 'ios' in stats['by_platform']
        assert 'android' in stats['by_platform']
    
    def test_get_by_topic_id(self, repository):
        """Test get user by support topic ID."""
        # Update user with topic_id
        user = repository.get_by_id('123')
        user.support_topic_id = 99
        repository.save(user)
        
        found = repository.get_by_topic_id(99)
        
        assert found is not None
        assert found.chat_id == '123'
    
    def test_get_by_topic_id_nonexistent(self, repository):
        """Test get user by non-existent topic ID."""
        found = repository.get_by_topic_id(999)
        
        assert found is None
