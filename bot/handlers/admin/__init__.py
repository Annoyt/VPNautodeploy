"""Admin handlers package - split from monolithic admin.py.

This package replaces the single admin.py (722 lines) with modular handlers:
- base.py: Base admin functionality
- users.py: User management (approve/reject/ban/unban)
- broadcast.py: Broadcast operations
- stats.py: Statistics and reporting

Migration completed as part of Phase 3 refactoring.
"""

from .base import AdminHandlerBase
from .users import AdminUsersMixin
from .broadcast import AdminBroadcastMixin
from .stats import AdminStatsMixin
from .ops import AdminOpsMixin


class AdminHandler(AdminUsersMixin, AdminBroadcastMixin, AdminStatsMixin, AdminOpsMixin):
    """Main admin handler combining all mixin functionality.

    Modular design (replaces the original 722-line admin.py):
    - AdminHandlerBase: routing + auth + thread_id helpers
    - AdminUsersMixin: approve / reject / ban / unban / reset /
      set_limit / grant_100gb / approve_payment
    - AdminBroadcastMixin: /broadcast preview/confirm/cancel
    - AdminStatsMixin: /stats, /users, /users_all, /pending, /backup
    - AdminOpsMixin: /status, /whoami, /onlines, /find, /recent, /repair_stuck,
      /topics, /quota, /expire
    """

    # Admin command routing table (inherited from AdminHandlerBase via mixins)
    ADMIN_COMMANDS = AdminHandlerBase.ADMIN_COMMANDS

    # Instance-level storage for pending broadcasts (admin_id -> message_text)
    _pending_broadcasts: dict = {}

    def __init__(self, bot, db, config):
        super().__init__(bot, db, config)
        self._pending_broadcasts = {}


__all__ = [
    'AdminHandler',
    'AdminHandlerBase',
    'AdminUsersMixin',
    'AdminBroadcastMixin',
    'AdminStatsMixin',
    'AdminOpsMixin',
]
