"""Message map repository for admin-user message threading."""

import logging
from typing import Optional

from .base import BaseRepository

logger = logging.getLogger(__name__)


class MessageMapRepository(BaseRepository):
    """Repository for message mapping (forum threading)."""
    
    def create(self, admin_msg_id: int, user_chat_id: str, user_msg_id: int) -> bool:
        """Create message mapping.
        
        Args:
            admin_msg_id: Admin message ID in forum
            user_chat_id: User's chat ID
            user_msg_id: User's original message ID
            
        Returns:
            True if successful
        """
        try:
            with self._transaction() as c:
                c.execute('''
                    INSERT OR REPLACE INTO message_map 
                    (admin_msg_id, user_chat_id, user_msg_id)
                    VALUES (?, ?, ?)
                ''', (admin_msg_id, user_chat_id, user_msg_id))
            return True
        except Exception as e:
            logger.error(f"Failed to create message mapping: {e}")
            return False
    
    def get_by_admin_msg(self, admin_msg_id: int) -> Optional[dict]:
        """Get mapping by admin message ID.
        
        Args:
            admin_msg_id: Admin message ID
            
        Returns:
            Mapping dict or None
        """
        row = self._execute(
            'SELECT * FROM message_map WHERE admin_msg_id = ?',
            (admin_msg_id,)
        )
        return dict(row) if row else None
    
    def delete(self, admin_msg_id: int) -> bool:
        """Delete message mapping.
        
        Args:
            admin_msg_id: Admin message ID
            
        Returns:
            True if successful
        """
        affected = self._execute_write(
            'DELETE FROM message_map WHERE admin_msg_id = ?',
            (admin_msg_id,)
        )
        return affected > 0
    
    # Aliases for backward compatibility with Database class
    def log_message_map(self, admin_msg_id: int, user_chat_id: str, user_msg_id: int) -> bool:
        """Alias for create() - backward compatibility."""
        return self.create(admin_msg_id, user_chat_id, user_msg_id)
    
    def get_mapped_user_message(self, admin_msg_id: int) -> Optional[dict]:
        """Alias for get_by_admin_msg() - backward compatibility.
        
        Returns dict with 'chat_id' and 'message_id' keys for compatibility.
        Returns the most recent mapping if multiple exist.
        """
        row = self._execute(
            'SELECT user_chat_id, user_msg_id FROM message_map WHERE admin_msg_id = ? ORDER BY id DESC LIMIT 1',
            (admin_msg_id,)
        )
        if row:
            return {
                'chat_id': row['user_chat_id'],
                'message_id': row['user_msg_id']
            }
        return None
