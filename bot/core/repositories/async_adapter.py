"""Async adapters for sync repositories.

This module provides async wrappers around sync repositories using asyncio.to_thread().
This eliminates the need for duplicate sync/async code while maintaining full async support.

Usage:
    from bot.core.repositories import AsyncUserRepository
    
    repo = AsyncUserRepository(db_path)
    user = await repo.get_by_id('123456')
"""

import asyncio
import logging
from typing import Optional, List, Dict, Any

from bot.models import User
from .user import UserRepository
from .ticket import TicketRepository
from .node import NodeRepository
from .message_map import MessageMapRepository

logger = logging.getLogger(__name__)


class _AsyncRepositoryBase:
    """Base class for async repository adapters."""
    
    def __init__(self, db_path: str):
        self._sync_repo = None
        self._db_path = db_path
    
    async def _run_in_thread(self, method, *args, **kwargs):
        """Run sync method in thread pool."""
        return await asyncio.to_thread(method, *args, **kwargs)


class AsyncUserRepository(_AsyncRepositoryBase):
    """Async adapter for UserRepository."""
    
    def __init__(self, db_path: str):
        super().__init__(db_path)
        self._sync_repo = UserRepository(db_path)
    
    async def get_by_id(self, chat_id: str) -> Optional[User]:
        """Get user by chat_id."""
        return await self._run_in_thread(self._sync_repo.get_by_id, chat_id)
    
    async def get_by_username(self, username: str) -> Optional[User]:
        """Get user by username."""
        return await self._run_in_thread(self._sync_repo.get_by_username, username)
    
    async def get_by_topic_id(self, topic_id: int) -> Optional[User]:
        """Get user by support topic ID."""
        return await self._run_in_thread(self._sync_repo.get_by_topic_id, topic_id)
    
    async def get_all(self) -> List[User]:
        """Get all users."""
        return await self._run_in_thread(self._sync_repo.get_all)
    
    async def get_by_status(self, status: str) -> List[User]:
        """Get users by status."""
        return await self._run_in_thread(self._sync_repo.get_by_status, status)
    
    async def get_pending(self) -> List[User]:
        """Get pending users."""
        return await self._run_in_thread(self._sync_repo.get_pending)
    
    async def save(self, user: User) -> bool:
        """Save or update user."""
        return await self._run_in_thread(self._sync_repo.save, user)
    
    async def update_status(self, chat_id: str, status: str) -> bool:
        """Update user status."""
        return await self._run_in_thread(self._sync_repo.update_status, chat_id, status)
    
    async def get_stats(self) -> dict:
        """Get user statistics."""
        return await self._run_in_thread(self._sync_repo.get_stats)


class AsyncTicketRepository(_AsyncRepositoryBase):
    """Async adapter for TicketRepository."""
    
    def __init__(self, db_path: str):
        super().__init__(db_path)
        self._sync_repo = TicketRepository(db_path)
    
    async def create(self, chat_id: str, topic_id: int, status: str = 'open') -> bool:
        """Create new support ticket."""
        return await self._run_in_thread(self._sync_repo.create, chat_id, topic_id, status)
    
    async def get_by_topic_id(self, topic_id: int) -> Optional[Dict[str, Any]]:
        """Get ticket by forum topic ID."""
        return await self._run_in_thread(self._sync_repo.get_by_topic_id, topic_id)
    
    async def get_by_chat_id(self, chat_id: str) -> List[Dict[str, Any]]:
        """Get all tickets for a user."""
        return await self._run_in_thread(self._sync_repo.get_by_chat_id, chat_id)
    
    async def update_status(self, topic_id: int, status: str) -> bool:
        """Update ticket status."""
        return await self._run_in_thread(self._sync_repo.update_status, topic_id, status)
    
    async def close_ticket(self, topic_id: int) -> bool:
        """Close a ticket."""
        return await self._run_in_thread(self._sync_repo.close_ticket, topic_id)


class AsyncNodeRepository(_AsyncRepositoryBase):
    """Async adapter for NodeRepository."""
    
    def __init__(self, db_path: str):
        super().__init__(db_path)
        self._sync_repo = NodeRepository(db_path)
    
    async def get_by_id(self, node_id: int):
        """Get node by ID."""
        return await self._run_in_thread(self._sync_repo.get_by_id, node_id)
    
    async def get_by_name(self, name: str):
        """Get node by name."""
        return await self._run_in_thread(self._sync_repo.get_by_name, name)
    
    async def get_all(self, node_type=None):
        """Get all nodes."""
        return await self._run_in_thread(self._sync_repo.get_all, node_type)
    
    async def get_by_status(self, status):
        """Get nodes by status."""
        return await self._run_in_thread(self._sync_repo.get_by_status, status)
    
    async def create(self, node) -> int:
        """Create new node."""
        return await self._run_in_thread(self._sync_repo.create, node)
    
    async def update_status(self, node_id: int, status) -> bool:
        """Update node status."""
        return await self._run_in_thread(self._sync_repo.update_status, node_id, status)
    
    async def update_client_count(self, node_id: int, count: int) -> bool:
        """Update node client count."""
        return await self._run_in_thread(self._sync_repo.update_client_count, node_id, count)
    
    async def delete(self, node_id: int) -> bool:
        """Delete node."""
        return await self._run_in_thread(self._sync_repo.delete, node_id)


class AsyncMessageMapRepository(_AsyncRepositoryBase):
    """Async adapter for MessageMapRepository."""
    
    def __init__(self, db_path: str):
        super().__init__(db_path)
        self._sync_repo = MessageMapRepository(db_path)
    
    async def create(self, admin_msg_id: int, user_chat_id: str, user_msg_id: int) -> bool:
        """Create message mapping."""
        return await self._run_in_thread(
            self._sync_repo.create, admin_msg_id, user_chat_id, user_msg_id
        )
    
    async def get_by_admin_msg(self, admin_msg_id: int) -> Optional[Dict[str, Any]]:
        """Get mapping by admin message ID."""
        return await self._run_in_thread(
            self._sync_repo.get_by_admin_msg, admin_msg_id
        )
    
    async def delete(self, admin_msg_id: int) -> bool:
        """Delete message mapping."""
        return await self._run_in_thread(
            self._sync_repo.delete, admin_msg_id
        )
    
    # Aliases for backward compatibility
    async def log_message_map(self, admin_msg_id: int, user_chat_id: str,
                              user_msg_id: int) -> bool:
        """Log message mapping between admin and user (backward compatibility)."""
        return await self.create(admin_msg_id, user_chat_id, user_msg_id)
    
    async def get_mapped_user_message(self, admin_msg_id: int) -> Optional[Dict[str, Any]]:
        """Get user message info by admin message ID (backward compatibility)."""
        return await self._run_in_thread(
            self._sync_repo.get_mapped_user_message, admin_msg_id
        )
