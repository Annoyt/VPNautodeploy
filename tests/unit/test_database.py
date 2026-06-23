"""Tests for database module"""

import os
import tempfile
import pytest
from datetime import datetime

from bot.core.database import Database, User

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)


class TestUser:
    """Tests for User dataclass"""
    
    def test_user_creation(self):
        """Test user creation with defaults"""
        user = User(chat_id='12345')
        
        assert user.chat_id == '12345'
        assert user.username is None
        assert user.uuid is None
        assert user.email is None
        assert user.status == 'new'
        assert user.lang == 'ru'
        assert user.platform is None
        assert user.support_topic_id is None
        assert user.created_at is not None
    
    def test_user_with_all_fields(self):
        """Test user creation with all fields"""
        user = User(
            chat_id='12345',
            username='testuser',
            uuid='uuid-123',
            email='test@example.com',
            status='demo',
            lang='en',
            platform='android',
            support_topic_id=42,
            created_at='2024-01-01T00:00:00'
        )
        
        assert user.chat_id == '12345'
        assert user.username == 'testuser'
        assert user.uuid == 'uuid-123'
        assert user.email == 'test@example.com'
        assert user.status == 'demo'
        assert user.lang == 'en'
        assert user.platform == 'android'
        assert user.support_topic_id == 42
        assert user.created_at == '2024-01-01T00:00:00'
    
    def test_auto_created_at(self):
        """Test created_at is auto-generated"""
        before = datetime.now()
        user = User(chat_id='12345')
        after = datetime.now()
        
        created = datetime.fromisoformat(user.created_at)
        assert before <= created <= after


class TestDatabase:
    """Tests for Database class"""
    
    @pytest.fixture
    def db(self):
        """Create temporary database for testing"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        database = Database(db_path)
        yield database
        
        # Cleanup
        os.unlink(db_path)
    
    def test_init_creates_tables(self, db):
        """Test initialization creates required tables"""
        conn = db._connect()
        c = conn.cursor()
        
        for table in ['users', 'admin_actions', 'xui_synced', 'message_map']:
            c.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            assert c.fetchone() is not None, f"Table {table} not created"
        
        conn.close()
    
    def test_save_and_get_user(self, db):
        """Test saving and retrieving a user"""
        user = User(
            chat_id='12345',
            username='testuser',
            status='pending_demo'
        )
        
        assert db.save_user(user) == True
        
        retrieved = db.get_user('12345')
        assert retrieved is not None
        assert retrieved.chat_id == '12345'
        assert retrieved.username == 'testuser'
        assert retrieved.status == 'pending_demo'
    
    def test_get_user_not_found(self, db):
        """Test retrieving non-existent user"""
        user = db.get_user('99999')
        assert user is None
    
    def test_update_user(self, db):
        """Test updating existing user"""
        user = User(chat_id='12345', username='oldname', status='new')
        db.save_user(user)
        
        user.username = 'newname'
        user.status = 'demo'
        db.save_user(user)
        
        retrieved = db.get_user('12345')
        assert retrieved.username == 'newname'
        assert retrieved.status == 'demo'
    
    def test_update_status(self, db):
        """Test update_status method"""
        user = User(chat_id='12345', status='new')
        db.save_user(user)
        
        assert db.update_status('12345', 'demo') == True
        
        retrieved = db.get_user('12345')
        assert retrieved.status == 'demo'
    
    def test_update_status_nonexistent_user(self, db):
        """Test update_status for non-existent user"""
        result = db.update_status('99999', 'demo')
        assert result == False
    
    def test_get_pending_users(self, db):
        """Test getting pending demo users"""
        db.save_user(User(chat_id='1', status='pending_demo'))
        db.save_user(User(chat_id='2', status='pending_demo'))
        db.save_user(User(chat_id='3', status='demo'))
        db.save_user(User(chat_id='4', status='new'))
        
        pending = db.get_pending_users()
        
        assert len(pending) == 2
        chat_ids = {u.chat_id for u in pending}
        assert chat_ids == {'1', '2'}
    
    def test_get_all_users(self, db):
        """Test getting all users"""
        db.save_user(User(chat_id='1', status='new'))
        db.save_user(User(chat_id='2', status='demo'))
        db.save_user(User(chat_id='3', status='banned'))
        
        all_users = db.get_all_users()
        assert len(all_users) == 3
        chat_ids = {u.chat_id for u in all_users}
        assert chat_ids == {'1', '2', '3'}
    
    def test_get_all_users_empty(self, db):
        """Test getting all users from empty DB"""
        all_users = db.get_all_users()
        assert all_users == []
    
    def test_get_user_by_topic_id(self, db):
        """Test finding user by support topic ID"""
        db.save_user(User(chat_id='111', support_topic_id=42))
        db.save_user(User(chat_id='222', support_topic_id=99))
        
        user = db.get_user_by_topic_id(42)
        assert user is not None
        assert user.chat_id == '111'
        
        user2 = db.get_user_by_topic_id(99)
        assert user2.chat_id == '222'
    
    def test_get_user_by_topic_id_not_found(self, db):
        """Test topic ID lookup when not found"""
        result = db.get_user_by_topic_id(9999)
        assert result is None
    
    def test_get_stats(self, db):
        """Test getting statistics"""
        db.save_user(User(chat_id='1', status='new', platform='android'))
        db.save_user(User(chat_id='2', status='demo', platform='ios'))
        db.save_user(User(chat_id='3', status='demo', platform='android'))
        db.save_user(User(chat_id='4', status='pending_demo'))
        
        stats = db.get_stats()
        
        assert stats['total'] == 4
        assert stats['by_status']['new'] == 1
        assert stats['by_status']['demo'] == 2
        assert stats['by_status']['pending_demo'] == 1
        assert stats['by_platform']['android'] == 2
        assert stats['by_platform']['ios'] == 1
    
    def test_get_stats_empty(self, db):
        """Test getting stats with no users"""
        stats = db.get_stats()
        
        assert stats['total'] == 0
        assert stats['by_status'] == {}
        assert stats['by_platform'] == {}
    
    def test_log_admin_action(self, db):
        """Test logging admin actions"""
        db.log_admin_action('admin123', 'approve', 'user456', 'Test details')
        
        conn = db._connect()
        c = conn.cursor()
        c.execute('SELECT * FROM admin_actions')
        row = c.fetchone()
        conn.close()
        
        assert row is not None
        assert row[1] == 'admin123'
        assert row[2] == 'approve'
        assert row[3] == 'user456'
        assert row[4] == 'Test details'
        assert row[5] is not None
    
    # ===== New: message_map tests =====
    
    def test_log_message_map(self, db):
        """Test logging message mapping for PM mode"""
        result = db.log_message_map(100, '12345', 200)
        assert result == True
    
    def test_get_mapped_user_message(self, db):
        """Test retrieving mapped user message"""
        db.log_message_map(100, '12345', 200)
        
        mapped = db.get_mapped_user_message(100)
        assert mapped is not None
        assert mapped['chat_id'] == '12345'
        assert mapped['message_id'] == 200
    
    def test_get_mapped_user_message_not_found(self, db):
        """Test mapped message not found"""
        mapped = db.get_mapped_user_message(9999)
        assert mapped is None
    
    def test_message_map_multiple_entries(self, db):
        """Test multiple mappings return latest"""
        db.log_message_map(100, '111', 200)
        db.log_message_map(100, '222', 300)
        
        mapped = db.get_mapped_user_message(100)
        # Should return latest entry (ORDER BY id DESC LIMIT 1)
        assert mapped['chat_id'] == '222'
        assert mapped['message_id'] == 300
    
    # ===== New: xui_synced tests =====
    
    def test_mark_xui_synced(self, db):
        """Test marking email as synced"""
        result = db.mark_xui_synced('user@vpn.com')
        assert result == True
    
    def test_is_xui_synced_true(self, db):
        """Test checking synced email"""
        db.mark_xui_synced('user@vpn.com')
        assert db.is_xui_synced('user@vpn.com') == True
    
    def test_is_xui_synced_false(self, db):
        """Test checking non-synced email"""
        assert db.is_xui_synced('nobody@vpn.com') == False
    
    def test_mark_xui_synced_idempotent(self, db):
        """Test marking same email twice doesn't error (INSERT OR REPLACE)"""
        db.mark_xui_synced('user@vpn.com')
        db.mark_xui_synced('user@vpn.com')
        assert db.is_xui_synced('user@vpn.com') == True
    
    def test_user_with_all_fields_roundtrip(self, db):
        """Test saving and retrieving user with all fields populated"""
        user = User(
            chat_id='12345',
            username='testuser',
            uuid='test-uuid-123',
            email='test@example.com',
            status='demo',
            lang='en',
            platform='windows',
            support_topic_id=99,
            created_at='2024-03-25T10:00:00'
        )
        
        db.save_user(user)
        retrieved = db.get_user('12345')
        
        assert retrieved.chat_id == user.chat_id
        assert retrieved.username == user.username
        assert retrieved.uuid == user.uuid
        assert retrieved.email == user.email
        assert retrieved.status == user.status
        assert retrieved.lang == user.lang
        assert retrieved.platform == user.platform
        assert retrieved.support_topic_id == user.support_topic_id
        assert retrieved.created_at == user.created_at
