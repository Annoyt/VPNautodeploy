"""Base repository with common database operations."""

import logging
import re
import sqlite3
from contextlib import contextmanager
from typing import Optional

logger = logging.getLogger(__name__)


class BaseRepository:
    """Base class for all repositories."""
    
    def __init__(self, db_path: str):
        """Initialize repository.
        
        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path
    
    def _connect(self):
        """Create database connection with row factory."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    @contextmanager
    def _transaction(self):
        """Transaction context manager."""
        conn = self._connect()
        try:
            yield conn.cursor()
            conn.commit()
        except sqlite3.Error as e:
            conn.rollback()
            logger.error(f"Transaction failed: {e}")
            raise
        finally:
            conn.close()
    
    def _execute(self, query: str, params: tuple = ()) -> Optional[sqlite3.Row]:
        """Execute query and return single row.
        
        Args:
            query: SQL query
            params: Query parameters
            
        Returns:
            Single row or None
        """
        with self._connect() as conn:
            c = conn.cursor()
            c.execute(query, params)
            return c.fetchone()
    
    def _execute_many(self, query: str, params: tuple = ()) -> list:
        """Execute query and return all rows.
        
        Args:
            query: SQL query
            params: Query parameters
            
        Returns:
            List of rows
        """
        with self._connect() as conn:
            c = conn.cursor()
            c.execute(query, params)
            return c.fetchall()
    
    def _execute_write(self, query: str, params: tuple = ()) -> int:
        """Execute write query and return affected rows.
        
        Args:
            query: SQL query
            params: Query parameters
            
        Returns:
            Number of affected rows
        """
        with self._transaction() as c:
            c.execute(query, params)
            return c.rowcount
