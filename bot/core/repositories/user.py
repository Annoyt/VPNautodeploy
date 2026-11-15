"""User repository for user-related database operations."""

import logging
import sqlite3
from typing import Optional, List

from bot.models import User
from .base import BaseRepository

logger = logging.getLogger(__name__)

# The columns save() writes, in this order, each from the user's attribute
# of the same name. Any other column of the row keeps its value:
# traffic_up / traffic_down (the traffic mirror's), xui_synced, and
# whatever else an older prod schema carries. A new column the bot should
# save goes here.
SAVE_COLUMNS = (
    'chat_id', 'username', 'previous_state', 'reject_count', 'uuid', 'email',
    'status', 'lang', 'platform', 'support_topic_id', 'created_at',
    'subscription_expiry', 'limit_ip', 'quota_gb', 'last_traffic_update',
    'next_protocol_idx', 'last_country', 'last_asn', 'last_city',
    'last_lat', 'last_lon', 'contact_email',
)
# UNIQUE in the repo's schema: one key (uuid) and one panel id (email) per
# user. Prod's older table has neither constraint (checked 2026-11-15).
KEY_COLUMNS = ('uuid', 'email')


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
        """Write the user's row: update it in place, or insert it when
        there is none. True when it was written.

        It never touches another user's row. Until 2026-11-15 this was an
        INSERT OR REPLACE. A uuid or panel email that another row held
        silently deleted that row where both are UNIQUE (the repo's schema;
        prod's older table has neither constraint), and every save reset
        the columns it does not write to their defaults. Now such a clash
        is refused and nothing is written. The log names the column and
        the user who holds it, never the value: the uuid is the user's
        key.

        A NULL for a NOT NULL column that has a default is written as that
        default, as REPLACE did, so a row of an older schema saves the way
        it always has.
        """
        values = self._save_values(user)
        # A row without a chat_id is nobody's: nothing finds it, and every
        # such save would add another (a NULL never matches a NULL).
        if values['chat_id'] is None or values['chat_id'] == '':
            logger.error("Refused to save a user without a chat_id. Nothing was written.")
            return False
        try:
            with self._transaction() as c:
                exprs = self._value_exprs(c)
                rest = SAVE_COLUMNS[1:]
                c.execute(
                    'UPDATE users SET '
                    + ', '.join(f'{col} = {exprs[col]}' for col in rest)
                    + ' WHERE chat_id = ?',
                    tuple(values[col] for col in rest) + (values['chat_id'],),
                )
                # The UPDATE took the write lock, matched or not: no other
                # writer can insert this chat_id before the INSERT.
                if c.rowcount == 0:
                    c.execute(
                        f'INSERT INTO users ({", ".join(SAVE_COLUMNS)}) VALUES ('
                        + ', '.join(exprs[col] for col in SAVE_COLUMNS) + ')',
                        tuple(values[col] for col in SAVE_COLUMNS),
                    )
            return True
        except sqlite3.IntegrityError as e:
            holders = self._key_holders(values)
            if holders:
                logger.error(
                    "Refused to save user %s: %s. Nothing was written.",
                    user.chat_id,
                    '; '.join(f"its {col} belongs to user {other}"
                              for col, other in holders),
                )
            else:
                logger.error(f"Failed to save user {user.chat_id}: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to save user {user.chat_id}: {e}")
            return False

    @staticmethod
    def _save_values(user: User) -> dict:
        """The value save() writes for each of SAVE_COLUMNS: the user's
        attribute of that name."""
        values = {col: getattr(user, col, None) for col in SAVE_COLUMNS}
        values['next_protocol_idx'] = values['next_protocol_idx'] or 0
        # An empty string is no key. Stored, it is a value like any other,
        # so a second user with '' would clash with the first.
        for col in KEY_COLUMNS:
            if values[col] == '':
                values[col] = None
        return values

    @staticmethod
    def _value_exprs(c) -> dict:
        """The SQL for each column's value: '?', or COALESCE(?, <default>)
        for a NOT NULL column with a default. REPLACE wrote the default
        for a NULL there, while a plain INSERT or UPDATE refuses the NULL.
        The repo's schema has no such column; an older prod table may."""
        info = {row[1]: row for row in c.execute('PRAGMA table_info(users)')}
        exprs = {}
        for col in SAVE_COLUMNS:
            row = info.get(col)
            # (cid, name, type, notnull, dflt_value, pk)
            if row is not None and row[3] and row[4] is not None:
                exprs[col] = f'COALESCE(?, {row[4]})'
            else:
                exprs[col] = '?'
        return exprs

    def _key_holders(self, values: dict) -> list:
        """(column, chat_id) for each key column whose value another
        user's row holds. Empty when that cannot be read."""
        found = []
        try:
            conn = self._connect()
            try:
                for col in KEY_COLUMNS:
                    if values[col] is None:
                        continue
                    row = conn.execute(
                        f'SELECT chat_id FROM users WHERE {col} = ? '
                        'AND chat_id != ? LIMIT 1',
                        (values[col], values['chat_id']),
                    ).fetchone()
                    if row:
                        found.append((col, row[0]))
            finally:
                conn.close()
        except sqlite3.Error:
            return []
        return found
    
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
            # Without this, save() persists contact_email but every
            # subsequent get_by_id() dropped it → the "email the key"
            # flow always saw None and answered "set your email first"
            # even right after /setemail succeeded.
            contact_email=_safe_get(row, 'contact_email'),
        )
