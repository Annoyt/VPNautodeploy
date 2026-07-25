"""User-lifecycle helpers shared across handlers.

The "kill the user's active VPN" sequence is needed by several entry
points (/reject command, /ban command, Reject inline callback, Revoke
inline callback, the admin dashboard's reject and ban actions). Keeping
the logic in one place stops bugs of the form "I fixed reject but
forgot to fix the dashboard does the same thing".
"""

import logging
from typing import Optional

from bot.utils.log_redaction import redact_email

logger = logging.getLogger(__name__)


def revoke_user_key(user, xui_service, db) -> None:
    """Revoke a user's VPN key.

    Removes the matching client from the x-ui inbound (if both an email
    and an x-ui service are available), then clears uuid/email on the
    User row and persists it. Safe to call when the user has no key —
    the function becomes a no-op in that case.

    Args:
        user: User model row to mutate. uuid and email get nulled.
        xui_service: XUIService instance from bot.services dict, may be None.
        db: Database facade (or any object exposing save_user(user)).
    """
    email = getattr(user, "email", None)
    if email and xui_service is not None:
        try:
            xui_service.remove_client_sync(email)
            logger.info(f"revoke_user_key: removed x-ui client {redact_email(email)} for user {user.chat_id}")
        except Exception as e:
            # Non-fatal: client may already be gone from x-ui, or x-ui may
            # be temporarily down. We still want to clear the DB row.
            logger.warning(f"revoke_user_key: x-ui remove failed for {redact_email(email)}: {e}")

    # Mirror the revocation onto the reserve fallback node (paid tier).
    # Best-effort: a dead reserve panel must not block the local revoke.
    uuid = getattr(user, "uuid", None)
    if uuid:
        try:
            from bot.services.fallback_node import FallbackNodeService
            from bot.config import Settings
            fb = FallbackNodeService(Settings())
            if fb.enabled and fb._api_configured:
                fb.remove_client(uuid)
                logger.info(f"revoke_user_key: removed fallback-node client for user {user.chat_id}")
        except Exception as e:
            logger.warning(f"revoke_user_key: fallback-node remove failed for {user.chat_id}: {e}")

    if getattr(user, "uuid", None) or getattr(user, "email", None):
        user.uuid = None
        user.email = None
        db.save_user(user)
        logger.info(f"revoke_user_key: cleared uuid/email for user {user.chat_id}")
