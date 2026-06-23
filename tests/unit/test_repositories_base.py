"""Tests for base repository."""

import pytest
import sqlite3
import tempfile
import os
from unittest.mock import Mock, patch

from bot.core.repositories.base import BaseRepository


class TestBaseRepository:
    """Test BaseRepository functionality."""
    
    @pytest.fixture
    def temp_db(self):
        """Create temporary database."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        # Create test table
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE test_table (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                value INTEGER
            )
        ''')
        c.execute("INSERT INTO test_table (name, value) VALUES ('test1', 100)")
        c.execute("INSERT INTO test_table (name, value) VALUES ('test2', 200)")
        conn.commit()
        conn.close()
        
        yield db_path
        
        # Cleanup
        os.unlink(db_path)
    
    @pytest.fixture
    def repository(self, temp_db):
        """Create repository instance."""
        return BaseRepository(temp_db)
    
    def test_repository_initialization(self, temp_db):
        """Test repository stores db_path."""
        repo = BaseRepository(temp_db)
        assert repo.db_path == temp_db
    
    def test_connect_creates_connection(self, repository):
        """Test _connect creates valid connection."""
        conn = repository._connect()
        
        assert conn is not None
        assert isinstance(conn, sqlite3.Connection)
        assert conn.row_factory == sqlite3.Row
        conn.close()
    
    def test_execute_returns_single_row(self, repository):
        """Test _execute returns single row."""
        row = repository._execute(
            "SELECT * FROM test_table WHERE id = ?",
            (1,)
        )
        
        assert row is not None
        assert row['id'] == 1
        assert row['name'] == 'test1'
        assert row['value'] == 100
    
    def test_execute_returns_none_for_no_match(self, repository):
        """Test _execute returns None when no rows match."""
        row = repository._execute(
            "SELECT * FROM test_table WHERE id = ?",
            (999,)
        )
        
        assert row is None
    
    def test_execute_many_returns_all_rows(self, repository):
        """Test _execute_many returns all matching rows."""
        rows = repository._execute_many("SELECT * FROM test_table")
        
        assert len(rows) == 2
        assert rows[0]['name'] == 'test1'
        assert rows[1]['name'] == 'test2'
    
    def test_execute_many_with_params(self, repository):
        """Test _execute_many with parameters."""
        rows = repository._execute_many(
            "SELECT * FROM test_table WHERE value > ?",
            (150,)
        )
        
        assert len(rows) == 1
        assert rows[0]['name'] == 'test2'
    
    def test_execute_write_inserts_data(self, repository):
        """Test _execute_write inserts data."""
        affected = repository._execute_write(
            "INSERT INTO test_table (name, value) VALUES (?, ?)",
            ('test3', 300)
        )
        
        assert affected == 1
        
        # Verify insertion
        row = repository._execute("SELECT * FROM test_table WHERE name = ?", ('test3',))
        assert row is not None
        assert row['value'] == 300
    
    def test_execute_write_updates_data(self, repository):
        """Test _execute_write updates data."""
        affected = repository._execute_write(
            "UPDATE test_table SET value = ? WHERE name = ?",
            (999, 'test1')
        )
        
        assert affected == 1
        
        # Verify update
        row = repository._execute("SELECT * FROM test_table WHERE name = ?", ('test1',))
        assert row['value'] == 999
    
    def test_execute_write_deletes_data(self, repository):
        """Test _execute_write deletes data."""
        affected = repository._execute_write(
            "DELETE FROM test_table WHERE id = ?",
            (1,)
        )
        
        assert affected == 1
        
        # Verify deletion
        row = repository._execute("SELECT * FROM test_table WHERE id = ?", (1,))
        assert row is None
    
    def test_transaction_commits_on_success(self, repository):
        """Test transaction commits when no error."""
        with repository._transaction() as c:
            c.execute("INSERT INTO test_table (name, value) VALUES ('trans_test', 500)")
        
        # Verify data committed
        row = repository._execute("SELECT * FROM test_table WHERE name = ?", ('trans_test',))
        assert row is not None
        assert row['value'] == 500
    
    def test_transaction_rolls_back_on_error(self, repository):
        """Test transaction rolls back on error."""
        try:
            with repository._transaction() as c:
                c.execute("INSERT INTO test_table (name, value) VALUES ('rollback_test', 999)")
                raise ValueError("Simulated error")
        except ValueError:
            pass
        
        # Verify data was NOT committed
        row = repository._execute("SELECT * FROM test_table WHERE name = ?", ('rollback_test',))
        assert row is None
    
    def test_transaction_logs_error(self, repository):
        """Test transaction logs SQLite errors."""
        with patch('bot.core.repositories.base.logger') as mock_logger:
            try:
                with repository._transaction() as c:
                    # Invalid SQL to trigger error
                    c.execute("INVALID SQL")
            except sqlite3.Error:
                pass
            
            mock_logger.error.assert_called_once()
            assert "Transaction failed" in mock_logger.error.call_args[0][0]
