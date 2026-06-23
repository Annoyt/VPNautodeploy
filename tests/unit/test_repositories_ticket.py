"""Tests for TicketRepository."""

import pytest
import sqlite3
import tempfile
import os

from bot.core.repositories.ticket import TicketRepository


class TestTicketRepository:
    """Test TicketRepository functionality."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database with tickets schema."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER UNIQUE,
                chat_id TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id)
            )
        ''')
        c.execute('''
            CREATE TABLE ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER,
                sender_type TEXT,
                sender_name TEXT,
                message_text TEXT,
                has_media BOOLEAN DEFAULT 0,
                media_file_id TEXT,
                message_id INTEGER,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (topic_id) REFERENCES tickets(topic_id)
            )
        ''')
        conn.commit()
        conn.close()

        yield db_path

        os.unlink(db_path)

    @pytest.fixture
    def repository(self, temp_db):
        """Create TicketRepository instance."""
        return TicketRepository(temp_db)

    def test_create_ticket(self, repository):
        """Test creating a ticket."""
        result = repository.create('12345', 42, 'open')
        assert result is True

        ticket = repository.get_by_topic_id(42)
        assert ticket is not None
        assert ticket['chat_id'] == '12345'
        assert ticket['status'] == 'open'

    def test_create_or_replace_existing_ticket(self, repository):
        """Test INSERT OR REPLACE updates existing ticket."""
        repository.create('12345', 42, 'open')
        result = repository.create('12345', 42, 'closed')
        assert result is True

        ticket = repository.get_by_topic_id(42)
        assert ticket['status'] == 'closed'

    def test_get_by_topic_id_nonexistent(self, repository):
        """Test get_by_topic_id returns None for missing ticket."""
        assert repository.get_by_topic_id(999) is None

    def test_get_by_chat_id_multiple(self, repository):
        """Test get_by_chat_id returns all user tickets."""
        repository.create('12345', 1, 'open')
        repository.create('12345', 2, 'closed')
        repository.create('99999', 3, 'open')

        tickets = repository.get_by_chat_id('12345')
        assert len(tickets) == 2
        assert {t['topic_id'] for t in tickets} == {1, 2}

    def test_get_by_chat_id_empty(self, repository):
        """Test get_by_chat_id returns empty list for unknown user."""
        assert repository.get_by_chat_id('unknown') == []

    def test_update_status(self, repository):
        """Test updating ticket status."""
        repository.create('12345', 42, 'open')
        result = repository.update_status(42, 'resolved')
        assert result is True

        ticket = repository.get_by_topic_id(42)
        assert ticket['status'] == 'resolved'

    def test_update_status_nonexistent(self, repository):
        """Test update_status returns False for nonexistent ticket."""
        assert repository.update_status(999, 'resolved') is False

    def test_close_ticket(self, repository):
        """Test close_ticket updates status and sets closed_at."""
        repository.create('12345', 42, 'open')
        result = repository.close_ticket(42)
        assert result is True

        ticket = repository.get_by_topic_id(42)
        assert ticket['status'] == 'closed'
        assert ticket['closed_at'] is not None

    def test_log_ticket_message(self, repository):
        """Test logging a ticket message."""
        repository.create('12345', 42, 'open')
        result = repository.log_ticket_message(
            topic_id=42,
            sender_type='user',
            sender_name='TestUser',
            text='Hello!',
            has_media=True,
            media_file_id='file123',
            message_id=100
        )
        assert result is True

        messages = repository.get_ticket_messages(42)
        assert len(messages) == 1
        assert messages[0]['message_text'] == 'Hello!'
        assert messages[0]['has_media'] == 1
        assert messages[0]['media_file_id'] == 'file123'

    def test_get_ticket_messages_empty(self, repository):
        """Test get_ticket_messages returns empty list when no messages."""
        repository.create('12345', 42, 'open')
        assert repository.get_ticket_messages(42) == []

    def test_get_ticket_messages_ordered(self, repository):
        """Test messages are returned in ascending timestamp order."""
        repository.create('12345', 42, 'open')
        repository.log_ticket_message(42, 'user', 'U1', 'first')
        repository.log_ticket_message(42, 'admin', 'A1', 'second')

        messages = repository.get_ticket_messages(42)
        assert len(messages) == 2
        assert messages[0]['message_text'] == 'first'
        assert messages[1]['message_text'] == 'second'

    def test_sql_injection_safety_create(self, repository):
        """Test create is safe from SQL injection."""
        malicious = "'; DROP TABLE tickets; --"
        repository.create(malicious, 1, 'open')

        # Verify table still exists
        ticket = repository.get_by_topic_id(1)
        assert ticket is not None
        assert ticket['chat_id'] == malicious

    def test_sql_injection_safety_get_by_chat_id(self, repository):
        """Test get_by_chat_id is safe from SQL injection."""
        repository.create('user1', 1, 'open')
        malicious = "'; DELETE FROM tickets; --"
        tickets = repository.get_by_chat_id(malicious)
        assert tickets == []

        # Verify original data intact
        assert repository.get_by_chat_id('user1') is not None

    def test_null_message_fields(self, repository):
        """Test log_ticket_message handles None values safely."""
        repository.create('12345', 42, 'open')
        result = repository.log_ticket_message(
            topic_id=42,
            sender_type='user',
            sender_name=None,
            text=None,
            has_media=False,
            media_file_id=None,
            message_id=None
        )
        assert result is True
        msg = repository.get_ticket_messages(42)[0]
        assert msg['sender_name'] is None
        assert msg['message_text'] is None


class TestTicketRepositoryTransactionRollback:
    """Test transaction rollback behavior."""

    @pytest.fixture
    def temp_db_broken(self):
        """Create temp DB without ticket_messages table to trigger FK failure."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER UNIQUE,
                chat_id TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT
            )
        ''')
        conn.commit()
        conn.close()

        yield db_path
        os.unlink(db_path)

    def test_transaction_rollback_on_error(self, temp_db_broken):
        """Test that failed write doesn't corrupt database."""
        repo = TicketRepository(temp_db_broken)
        repo.create('123', 1, 'open')

        # This should fail because ticket_messages table doesn't exist
        result = repo.log_ticket_message(1, 'user', 'U', 'text')
        assert result is False

        # Original ticket should remain intact
        ticket = repo.get_by_topic_id(1)
        assert ticket is not None
