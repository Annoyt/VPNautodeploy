"""Tests for MessageMapRepository."""

import pytest
import sqlite3
import tempfile
import os

from bot.core.repositories.message_map import MessageMapRepository


class TestMessageMapRepository:
    """Test MessageMapRepository functionality."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database with message_map schema."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE message_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_msg_id INTEGER,
                user_chat_id TEXT,
                user_msg_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(admin_msg_id, user_chat_id)
            )
        ''')
        conn.commit()
        conn.close()

        yield db_path

        os.unlink(db_path)

    @pytest.fixture
    def repository(self, temp_db):
        """Create MessageMapRepository instance."""
        return MessageMapRepository(temp_db)

    def test_create_mapping(self, repository):
        """Test creating a message mapping."""
        result = repository.create(100, '12345', 50)
        assert result is True

        mapping = repository.get_by_admin_msg(100)
        assert mapping is not None
        assert mapping['admin_msg_id'] == 100
        assert mapping['user_chat_id'] == '12345'
        assert mapping['user_msg_id'] == 50

    def test_create_or_replace(self, repository):
        """Test INSERT OR REPLACE updates existing mapping."""
        repository.create(100, '12345', 50)
        result = repository.create(100, '12345', 99)
        assert result is True

        mapping = repository.get_by_admin_msg(100)
        assert mapping['user_msg_id'] == 99

    def test_get_by_admin_msg_nonexistent(self, repository):
        """Test get_by_admin_msg returns None for missing mapping."""
        assert repository.get_by_admin_msg(999) is None

    def test_delete_existing(self, repository):
        """Test deleting an existing mapping."""
        repository.create(100, '12345', 50)
        result = repository.delete(100)
        assert result is True
        assert repository.get_by_admin_msg(100) is None

    def test_delete_nonexistent(self, repository):
        """Test delete returns False for nonexistent mapping."""
        assert repository.delete(999) is False

    def test_log_message_map_alias(self, repository):
        """Test log_message_map alias calls create."""
        result = repository.log_message_map(200, '67890', 77)
        assert result is True

        mapping = repository.get_by_admin_msg(200)
        assert mapping['user_chat_id'] == '67890'

    def test_get_mapped_user_message_alias(self, repository):
        """Test get_mapped_user_message returns compatibility dict."""
        repository.create(300, '11111', 88)
        result = repository.get_mapped_user_message(300)
        assert result == {'chat_id': '11111', 'message_id': 88}

    def test_get_mapped_user_message_nonexistent(self, repository):
        """Test get_mapped_user_message returns None for missing mapping."""
        assert repository.get_mapped_user_message(999) is None

    def test_sql_injection_safety_create(self, repository):
        """Test create is safe from SQL injection."""
        malicious = "'; DROP TABLE message_map; --"
        repository.create(1, malicious, 1)

        mapping = repository.get_by_admin_msg(1)
        assert mapping is not None
        assert mapping['user_chat_id'] == malicious

    def test_sql_injection_safety_delete(self, repository):
        """Test delete is safe from SQL injection."""
        repository.create(1, 'safe', 1)
        malicious = "1; DROP TABLE message_map; --"
        result = repository.delete(malicious)
        assert result is False

        # Verify table still exists
        assert repository.get_by_admin_msg(1) is not None

    def test_null_chat_id(self, repository):
        """Test mapping with NULL user_chat_id."""
        repository.create(1, None, 10)
        mapping = repository.get_by_admin_msg(1)
        assert mapping['user_chat_id'] is None

    def test_multiple_same_admin_different_user(self, repository):
        """Test multiple mappings with same admin_msg_id but different users."""
        repository.create(100, 'user1', 1)
        repository.create(100, 'user2', 2)

        mapping = repository.get_by_admin_msg(100)
        # Should return one row; exact row may vary by insertion order
        assert mapping['admin_msg_id'] == 100

    def test_get_mapped_user_message_most_recent(self, repository):
        """Test alias returns most recent mapping via ORDER BY id DESC."""
        repository.create(100, 'first', 1)
        repository.create(100, 'second', 2)

        result = repository.get_mapped_user_message(100)
        assert result == {'chat_id': 'second', 'message_id': 2}
