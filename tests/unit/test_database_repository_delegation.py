"""Tests for Database repository delegation - Phase 3.

Verifies that Database class properly delegates to repositories.
"""

import pytest
import warnings
from unittest.mock import Mock, patch

from bot.core.database import Database


class TestDatabaseRepositoryDelegation:
    """Test Database class delegates to repositories."""
    
    @pytest.fixture
    def db(self, tmp_path):
        """Create Database with temp path."""
        db_path = str(tmp_path / "test.db")
        with patch('bot.core.database.UserRepository'), \
             patch('bot.core.database.TicketRepository'), \
             patch('bot.core.database.NodeRepository'), \
             patch('bot.core.database.MessageMapRepository'):
            database = Database(db_path)
            # Mock the repositories
            database._users = Mock()
            database._tickets = Mock()
            database._nodes = Mock()
            database._message_map = Mock()
            return database
    
    def test_delegation_mapping_exists(self, db):
        """Test that method mapping exists."""
        assert hasattr(db, '_METHOD_MAP')
        assert 'get_user' in db._METHOD_MAP
        assert 'get_all_users' in db._METHOD_MAP
        assert 'save_user' in db._METHOD_MAP
    
    def test_get_user_delegates_to_user_repository(self, db):
        """Test that get_user delegates to UserRepository.get_by_id."""
        expected_user = Mock()
        db._users.get_by_id.return_value = expected_user
        
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = db.get_user('12345')
        
        db._users.get_by_id.assert_called_once_with('12345')
        assert result == expected_user
    
    def test_get_pending_users_delegates_to_user_repository(self, db):
        """Test that get_pending_users delegates to UserRepository.get_pending."""
        expected_users = [Mock(), Mock()]
        db._users.get_pending.return_value = expected_users
        
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = db.get_pending_users()
        
        db._users.get_pending.assert_called_once()
        assert result == expected_users
    
    def test_get_node_delegates_to_node_repository(self, db):
        """Test that get_node delegates to NodeRepository.get_by_id."""
        expected_node = Mock()
        db._nodes.get_by_id.return_value = expected_node
        
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = db.get_node(1)
        
        db._nodes.get_by_id.assert_called_once_with(1)
        assert result == expected_node
    
    def test_delegation_emits_deprecation_warning(self, db):
        """Test that delegation emits deprecation warning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            db.get_user('12345')
            
            # Should emit deprecation warning
            assert len(w) >= 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert 'deprecated' in str(w[0].message).lower()
    
    def test_unmapped_method_raises_attribute_error(self, db):
        """Test that unmapped methods raise AttributeError."""
        with pytest.raises(AttributeError) as exc_info:
            db.nonexistent_method()
        
        assert 'nonexistent_method' in str(exc_info.value)
        assert 'delegation mapping' in str(exc_info.value)


class TestDatabaseBackwardCompatibility:
    """Test backward compatibility with legacy Database methods."""
    
    @pytest.fixture
    def db(self, tmp_path):
        """Create real Database with temp path."""
        db_path = str(tmp_path / "test.db")
        return Database(db_path)
    
    def test_init_db_creates_tables(self, db):
        """Test that init_db creates all required tables."""
        import sqlite3
        
        with sqlite3.connect(db.db_path) as conn:
            c = conn.cursor()
            
            # Check tables exist
            c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row[0] for row in c.fetchall()}
            
            assert 'users' in tables
            assert 'tickets' in tables
            assert 'nodes' in tables
            assert 'message_map' in tables
    
    def test_log_admin_action_works(self, db):
        """Test legacy log_admin_action method."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = db.log_admin_action('admin123', 'approve', 'user456', 'test')
        
        assert result is True
    
    def test_record_traffic_works(self, db):
        """Test legacy record_traffic method."""
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = db.record_traffic('test@example.com', 1000, 2000)
        
        assert result is True


class TestDatabaseRepositoryMigrationPath:
    """Document migration path from Database to repositories."""
    
    def test_migration_guide_in_docstring(self):
        """Test that migration guide is in module docstring."""
        import bot.core.database as db_module
        
        assert 'Migration Guide' in db_module.__doc__
        assert 'UserRepository' in db_module.__doc__
    
    def test_all_mapped_methods_have_replacement(self):
        """Test that all mapped methods have documented replacements."""
        from bot.core.database import Database
        
        # All methods should map to existing repository methods
        for db_method, (repo_name, repo_method) in Database._METHOD_MAP.items():
            assert repo_name.startswith('_'), f"Repository name should be private: {repo_name}"
            assert repo_method, f"Repository method not specified for {db_method}"
