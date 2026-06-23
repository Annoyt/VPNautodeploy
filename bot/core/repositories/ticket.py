"""Ticket repository for support ticket operations."""

import logging
from datetime import datetime, timedelta
from typing import Optional, List

from .base import BaseRepository

logger = logging.getLogger(__name__)


class TicketRepository(BaseRepository):
    """Repository for support ticket operations."""
    
    def create(self, chat_id: str, topic_id: int, status: str = 'open') -> bool:
        """Create new support ticket.
        
        Args:
            chat_id: User's chat ID
            topic_id: Forum topic ID
            status: Ticket status (open, closed)
            
        Returns:
            True if successful
        """
        try:
            with self._transaction() as c:
                c.execute('''
                    INSERT OR REPLACE INTO tickets 
                    (chat_id, topic_id, status, created_at)
                    VALUES (?, ?, ?, ?)
                ''', (chat_id, topic_id, status, datetime.now().isoformat()))
            return True
        except Exception as e:
            logger.error(f"Failed to create ticket for {chat_id}: {e}")
            return False
    
    def get_by_topic_id(self, topic_id: int) -> Optional[dict]:
        """Get ticket by topic ID.
        
        Args:
            topic_id: Forum topic ID
            
        Returns:
            Ticket dict or None
        """
        row = self._execute(
            'SELECT * FROM tickets WHERE topic_id = ?',
            (topic_id,)
        )
        if row:
            return dict(row)
        return None
    
    def get_by_chat_id(self, chat_id: str) -> List[dict]:
        """Get all tickets for user.
        
        Args:
            chat_id: User's chat ID
            
        Returns:
            List of ticket dicts
        """
        rows = self._execute_many(
            'SELECT * FROM tickets WHERE chat_id = ? ORDER BY created_at DESC',
            (chat_id,)
        )
        return [dict(row) for row in rows]
    
    def update_status(self, topic_id: int, status: str) -> bool:
        """Update ticket status.
        
        Args:
            topic_id: Forum topic ID
            status: New status (open, closed)
            
        Returns:
            True if successful
        """
        affected = self._execute_write(
            'UPDATE tickets SET status = ? WHERE topic_id = ?',
            (status, topic_id)
        )
        return affected > 0
    
    def close_ticket(self, topic_id: int) -> bool:
        """Close ticket.
        
        Args:
            topic_id: Forum topic ID
            
        Returns:
            True if successful
        """
        try:
            with self._transaction() as c:
                c.execute('''
                    UPDATE tickets 
                    SET status = ?, closed_at = ?
                    WHERE topic_id = ?
                ''', ('closed', datetime.now().isoformat(), topic_id))
            return True
        except Exception as e:
            logger.error(f"Failed to close ticket {topic_id}: {e}")
            return False
    
    def log_ticket_message(self, topic_id: int, sender_type: str, sender_name: str,
                           text: str, has_media: bool = False, media_file_id: str = None,
                           message_id: int = None) -> bool:
        """Log a message to ticket_messages table.
        
        Args:
            topic_id: Forum topic ID
            sender_type: 'user' or 'admin'
            sender_name: Display name of sender
            text: Message text
            has_media: Whether message has media
            media_file_id: Telegram file ID if media present
            message_id: Telegram message ID
            
        Returns:
            True if successful
        """
        try:
            with self._transaction() as c:
                # Real prod columns are `message_text` and `timestamp`; the
                # repo signature still uses the friendlier `text` / `created_at`
                # names for callers (see save_user-style consistency).
                c.execute('''
                    INSERT INTO ticket_messages
                    (topic_id, sender_type, sender_name, message_text, has_media, media_file_id, message_id, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (topic_id, sender_type, sender_name, text, has_media, media_file_id, message_id, datetime.now().isoformat()))
            return True
        except Exception as e:
            logger.error(f"Failed to log ticket message: {e}")
            return False
    
    def get_ticket_messages(self, topic_id: int) -> List[dict]:
        """Get all messages for a ticket.
        
        Args:
            topic_id: Forum topic ID
            
        Returns:
            List of message dicts
        """
        rows = self._execute_many(
            'SELECT * FROM ticket_messages WHERE topic_id = ? ORDER BY timestamp ASC',
            (topic_id,)
        )
        return [dict(row) for row in rows]

    def cleanup_old_messages(self, days: int = 30) -> int:
        """Delete ticket_messages older than ``days`` days.

        Called by the daily scheduler in NotificationService. Keeps the
        SQLite file from growing forever for low-volume installs; the
        Solved-issues archive in Telegram is the long-term record.
        """
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            with self._transaction() as c:
                cur = c.execute(
                    "DELETE FROM ticket_messages WHERE timestamp < ?",
                    (cutoff,),
                )
                deleted = cur.rowcount
            if deleted:
                logger.info(f"Ticket cleanup: removed {deleted} messages older than {days}d")
            return deleted
        except Exception as e:
            logger.error(f"Ticket cleanup failed: {e}")
            return 0
