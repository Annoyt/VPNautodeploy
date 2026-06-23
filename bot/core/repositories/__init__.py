"""Repository pattern for database access.

This module provides specialized repositories for different entities,
breaking down the monolithic Database class (God Object) into smaller,
more focused components.

Usage:
    # Sync repositories (for sync code)
    from bot.core.repositories import UserRepository, TicketRepository
    
    user_repo = UserRepository(db_path)
    user = user_repo.get_by_id('123456')
    
    # Async repositories (for async code)
    from bot.core.repositories import AsyncUserRepository
    
    user_repo = AsyncUserRepository(db_path)
    user = await user_repo.get_by_id('123456')
"""

from .base import BaseRepository
from .user import UserRepository
from .ticket import TicketRepository
from .node import NodeRepository
from .message_map import MessageMapRepository

# Async adapters
from .async_adapter import (
    AsyncUserRepository,
    AsyncTicketRepository,
    AsyncNodeRepository,
    AsyncMessageMapRepository,
)

__all__ = [
    # Sync repositories
    'BaseRepository',
    'UserRepository',
    'TicketRepository',
    'NodeRepository',
    'MessageMapRepository',
    # Async adapters
    'AsyncUserRepository',
    'AsyncTicketRepository',
    'AsyncNodeRepository',
    'AsyncMessageMapRepository',
]
