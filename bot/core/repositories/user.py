"""User repository for user-related database operations."""

import logging
from typing import Optional, List

from bot.models import User
from .base import BaseRepository

logger = logging.getLogger(__name__)


class UserRepository(BaseRepository):
    """Repository for user operations."""
    
    def get_by_id(self, chat_id: str) -> Optional[User]:
        """Get user by chat_id.
        
        Args:
            chat_id: User's Telegram chat ID
            
        Returns:
            User object or None
        """
        row = self._execute(
            'SELECT * FROM users WHERE chat_id = ?',
            (chat_id,)
        )
        return self._row_to_user(row) if row else None
    
    def get_by_username(self, username: str) -> Optional[User]:
        """Get user by username.
        
        Args:
            username: Telegram username (with or without @)
            
        Returns:
            User object or None
        """
        if str(username).startswith('@'):
            username = username[1:]
        
        row = self._execute(
            'SELECT * FROM users WHERE username = ? COLLATE NOCASE',
            (username,)
        )
        return self._row_to_user(row) if row else None
    
    def get_by_topic_id(self, topic_id: int) -> Optional[User]:
        """Get user by support topic ID.
        
        Args:
            topic_id: Forum topic ID
            
        Returns:
            User object or None
        """
        row = self._execute(
            'SELECT * FROM users WHERE support_topic_id = ?',
            (topic_id,)
        )
        return self._row_to_user(row) if row else None
    
    def get_all(self) -> List[User]:
        """Get all users.
        
        Returns:
            List of User objects
        """
        rows = self._execute_many('SELECT * FROM users ORDER BY created_at DESC')
        return [self._row_to_user(row) for row in rows]
    
    def get_by_status(self, status: str) -> List[User]:
        """Get users by status.
        
        Args:
            status: User status (new, pending_demo, demo, active, etc.)
            
        Returns:
            List of User objects
        """
        rows = self._execute_many(
            'SELECT * FROM users WHERE status = ? ORDER BY created_at DESC',
            (status,)
        )
        return [self._row_to_user(row) for row in rows]
    
    def get_pending(self) -> List[User]:
        """Get users with pending status.
        
        Returns:
            List of pending users
        """
        return self.get_by_status('pending_demo')
    
    def save(self, user: User) -> bool:
        """Save or update user.
        
        Args:
            user: User object to save
            
        Returns:
            True if successful
        """
        try:
            with self._transaction() as c:
                c.execute('''
                    INSERT OR REPLACE INTO users
                    (chat_id, username, previous_state, reject_count, uuid, email, status, lang, platform,
                     support_topic_id, created_at, subscription_expiry, limit_ip,
                     quota_gb, last_traffic_update, next_protocol_idx,
                     last_country, last_asn, last_city, last_lat, last_lon,
                     contact_email)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    user.chat_id, user.username, user.previous_state, user.reject_count,
                    user.uuid, user.email,
                    user.status, user.lang, user.platform,
                    user.support_topic_id, user.created_at,
                    user.subscription_expiry, user.limit_ip,
                    user.quota_gb, user.last_traffic_update,
                    getattr(user, 'next_protocol_idx', 0) or 0,
                    getattr(user, 'last_country', None),
                    getattr(user, 'last_asn', None),
                    getattr(user, 'last_city', None),
                    getattr(user, 'last_lat', None),
                    getattr(user, 'last_lon', None),
                    getattr(user, 'contact_email', None),
                ))
            return True
        except Exception as e:
            logger.error(f"Failed to save user {user.chat_id}: {e}")
            return False
    
    def update_status(self, chat_id: str, status: str) -> bool:
        """Update user status.
        
        Args:
            chat_id: User's chat ID
            status: New status
            
        Returns:
            True if successful
        """
        affected = self._execute_write(
            'UPDATE users SET status = ? WHERE chat_id = ?',
            (status, chat_id)
        )
        return affected > 0
    
    def search(self, query: str) -> List[User]:
        """Search users by username or chat_id (case-insensitive).
        
        Args:
            query: Search string (partial match)
            
        Returns:
            List of matching User objects
        """
        query = query.strip().lstrip('@')
        if not query:
            return self.get_all()
        
        pattern = f'%{query}%'
        rows = self._execute_many(
            '''SELECT * FROM users 
               WHERE username LIKE ? COLLATE NOCASE 
                  OR chat_id LIKE ?
               ORDER BY created_at DESC''',
            (pattern, pattern)
        )
        return [self._row_to_user(row) for row in rows]
    
    def get_with_open_tickets(self) -> List[User]:
        """Get users with open support tickets.
        
        Returns:
            List of users in support_topic state with a topic_id
        """
        rows = self._execute_many(
            '''SELECT * FROM users 
               WHERE status = 'support_topic' 
                 AND support_topic_id IS NOT NULL
               ORDER BY created_at DESC'''
        )
        return [self._row_to_user(row) for row in rows]
    
    def get_registration_stats(self) -> dict:
        """Get registration counts for today, this week, this month.
        
        Returns:
            Dict with 'today', 'this_week', 'this_month' counts
        """
        row_today = self._execute(
            "SELECT COUNT(*) as cnt FROM users WHERE date(created_at) = date('now')"
        )
        row_week = self._execute(
            "SELECT COUNT(*) as cnt FROM users WHERE created_at >= date('now', '-7 days')"
        )
        row_month = self._execute(
            "SELECT COUNT(*) as cnt FROM users WHERE created_at >= date('now', '-30 days')"
        )
        return {
            'today': row_today['cnt'] if row_today else 0,
            'this_week': row_week['cnt'] if row_week else 0,
            'this_month': row_month['cnt'] if row_month else 0,
        }
    
    def get_stats(self) -> dict:
        """Get user statistics.
        
        Returns:
            Dict with user counts by status and platform
        """
        # Stats by status
        status_rows = self._execute_many('''
            SELECT status, COUNT(*) as count 
            FROM users 
            GROUP BY status
        ''')
        
        stats = {'total': 0, 'by_status': {}, 'by_platform': {}}
        for row in status_rows:
            stats['by_status'][row['status']] = row['count']
            stats['total'] += row['count']
        
        # Stats by platform
        platform_rows = self._execute_many('''
            SELECT platform, COUNT(*) as count 
            FROM users 
            WHERE platform IS NOT NULL
            GROUP BY platform
        ''')
        
        for row in platform_rows:
            stats['by_platform'][row['platform']] = row['count']
        
        return stats
    
    def _row_to_user(self, row) -> User:
        """Convert database row to User object."""
        def _safe_get(row, key, default=None):
            try:
                return row[key]
            except (KeyError, IndexError):
                return default
        
        return User(
            chat_id=row['chat_id'],
            username=row['username'],
            previous_state=_safe_get(row, 'previous_state'),
            reject_count=_safe_get(row, 'reject_count') or 0,
            uuid=row['uuid'],
            email=row['email'],
            status=row['status'],
            lang=row['lang'],
            platform=row['platform'],
            support_topic_id=row['support_topic_id'],
            created_at=row['created_at'],
            subscription_expiry=row['subscription_expiry'],
            limit_ip=row['limit_ip'] if row['limit_ip'] is not None else 1,
            quota_gb=row['quota_gb'] if row['quota_gb'] is not None else 5.0,
            last_traffic_update=row['last_traffic_update'],
            next_protocol_idx=_safe_get(row, 'next_protocol_idx') or 0,
            last_country=_safe_get(row, 'last_country'),
            last_asn=_safe_get(row, 'last_asn'),
            last_city=_safe_get(row, 'last_city'),
            last_lat=_safe_get(row, 'last_lat'),
            last_lon=_safe_get(row, 'last_lon'),
        )
