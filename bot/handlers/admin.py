"""Admin handler - DEPRECATED, use admin package instead.

This module re-exports AdminHandler from the admin package for backward compatibility.
The monolithic 722-line implementation has been split into modular components.

New structure:
    bot/handlers/admin/
    ├── __init__.py      # Main AdminHandler combining all mixins
    ├── base.py          # Base functionality (AdminHandlerBase)
    ├── users.py         # User management (AdminUsersMixin)
    ├── broadcast.py     # Broadcast operations (AdminBroadcastMixin)
    └── stats.py         # Statistics (AdminStatsMixin)

Migration:
    Old: from bot.handlers.admin import AdminHandler
    New: from bot.handlers.admin import AdminHandler (same import, new implementation)
"""

# Import from new modular structure
from bot.handlers.admin import (
    AdminHandler,
    AdminHandlerBase,
    AdminUsersMixin,
    AdminBroadcastMixin,
    AdminStatsMixin
)



__all__ = [
    'AdminHandler',
    'AdminHandlerBase',
    'AdminUsersMixin',
    'AdminBroadcastMixin',
    'AdminStatsMixin'
]
