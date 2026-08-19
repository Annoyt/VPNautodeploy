"""Paid-tier promotion — one place for what "user is paid until X" means.

Three surfaces used to implement this independently (Stars payments,
/approve_payment, the dashboard's grant_paid button) and each drifted:
the Stars path never even flipped the status to 'paid' — so the hy2
auth gate kept treating the payer as demo — none of them raised the
quota above the demo default, and the panel sync was either missing or
gated on the dead local-DB mode. Route every grant through
``grant_paid_access`` so the tier is one coherent thing:

  bot.db:   status='paid', subscription_expiry, quota_gb floor,
            subscriptions.expires_at
  panel:    expiryTime, enable=1, totalGB — via the in-place update,
            with a full re-provision fallback for clients the panel
            has already purged.
"""

import logging
from datetime import datetime

from bot.config import UserState
from bot.config.constants import BYTES_PER_GB
from bot.core.state_machine import StateMachine

logger = logging.getLogger(__name__)


def grant_paid_access(db, config, xui, chat_id: str,
                      paid_until: datetime) -> dict:
    """Make the user paid until ``paid_until`` in every store at once.

    Idempotent: safe to call again with a later date (extension) and it
    never lowers a quota an admin raised by hand.

    Returns ``{'user', 'status_ok', 'panel_ok'}`` — ``panel_ok`` is
    ``None`` when there was nothing to sync (no key issued yet).
    """
    result = {'user': None, 'status_ok': True, 'panel_ok': None}

    user = db.get_user(chat_id)
    if not user:
        logger.warning(f"grant_paid_access: user {chat_id} not found")
        result['status_ok'] = False
        return result

    try:
        paid_gb = float(getattr(config, 'PAID_TRAFFIC_GB', 100) or 100)
    except (TypeError, ValueError):
        paid_gb = 100.0
    user.subscription_expiry = paid_until.isoformat()
    # Floor, never a downgrade: a hand-raised 500 GB quota survives.
    user.quota_gb = max(float(getattr(user, 'quota_gb', 0) or 0), paid_gb)
    db.save_user(user)

    if (getattr(user, 'status', '') or '') != UserState.PAID.value:
        sm = StateMachine(db)
        if sm.transition(chat_id, UserState.PAID):
            user.status = UserState.PAID.value
        else:
            result['status_ok'] = False
            logger.warning(
                f"grant_paid_access: transition to paid failed for "
                f"{chat_id} (status={user.status})"
            )

    # Keep the dashboard's subscription bucket consistent.
    try:
        with db._connect() as conn:
            conn.execute(
                "UPDATE subscriptions SET expires_at = ?, is_active = 1 "
                "WHERE chat_id = ? AND is_active = 1",
                (user.subscription_expiry, str(chat_id)),
            )
    except Exception as e:
        logger.warning(f"grant_paid_access: subscriptions update failed: {e}")

    if getattr(user, 'email', None) and xui:
        expiry_ms = int(paid_until.timestamp() * 1000)
        updates = {
            'expiryTime': expiry_ms,
            'enable': True,
            'totalGB': int(user.quota_gb * BYTES_PER_GB),
        }
        try:
            ok = xui.sync_client_settings_sync(user.email, updates)
            if not ok and getattr(user, 'uuid', None):
                # The panel purges clients that stay expired long
                # enough — re-provision with the SAME uuid so keys the
                # user already installed start working again.
                ok = xui.add_client_sync({
                    'id': user.uuid,
                    'flow': 'xtls-rprx-vision',
                    'email': user.email,
                    'limitIp': int(getattr(user, 'limit_ip', 1) or 1),
                    'totalGB': updates['totalGB'],
                    'expiryTime': expiry_ms,
                    'enable': True,
                }, int(getattr(config, 'INBOUND_ID', 1) or 1))
            result['panel_ok'] = bool(ok)
        except Exception as e:
            result['panel_ok'] = False
            logger.warning(f"grant_paid_access: panel sync failed: {e}")

    result['user'] = user
    return result
