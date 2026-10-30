"""Paid-tier promotion — one place for what "user is paid until X" means.

Three surfaces used to implement this independently (Stars payments,
/approve_payment, the dashboard's grant_paid button) and each drifted:
the Stars path never even flipped the status to 'paid' — so the hy2
auth gate kept treating the payer as demo — none of them raised the
quota above the demo default, and the panel sync was either missing or
gated on the dead local-DB mode. Route every grant through
``grant_paid_access`` so the tier is one coherent thing:

  bot.db:   status='paid', subscription_expiry, quota_gb floor,
            limit_ip floor, subscriptions.expires_at
  panel:    expiryTime, enable=1, totalGB, limitIp — via the in-place
            update, with a full re-provision fallback for clients the
            panel has already purged.

«🔑 Мой ключ» (GetKeyHandler) puts a key the panel lost back with its
tier's panel values (``tier_client_values``; a payer's are the ones a
payment writes) through the payment's re-add (``readd_client``). A
lapsed payer's key stays off.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.config import UserState
from bot.config.constants import BYTES_PER_GB
from bot.core.state_machine import StateMachine

logger = logging.getLogger(__name__)

# Addresses a paid key may use at once unless PAID_LIMIT_IP says otherwise.
DEFAULT_PAID_LIMIT_IP = 5


def paid_limit_ip(user, config) -> int:
    """The panel's limitIp for a paid user: PAID_LIMIT_IP (5 by default),
    never below a higher value an admin set by hand (/set_limit, the
    dashboard), and an admin's 0 (no limit) stays 0. PAID_LIMIT_IP 0 or
    less means no limit for every payer.

    The exit panel enforces limitIp with fail2ban: an address over the
    limit gets the older one banned for 30 min, so a payer on a phone, a
    laptop and the app's two-networks mode needs room."""
    try:
        floor = int(getattr(config, 'PAID_LIMIT_IP', DEFAULT_PAID_LIMIT_IP))
    except (TypeError, ValueError):
        floor = DEFAULT_PAID_LIMIT_IP
    if floor <= 0:
        return 0
    try:
        current = int(getattr(user, 'limit_ip', 1))
    except (TypeError, ValueError):
        current = 1
    if current == 0:
        return 0
    return max(current, floor)


def paid_quota_gb(user, config) -> float:
    """A paid key's monthly quota in GB: PAID_TRAFFIC_GB, never below a
    higher quota an admin set by hand (a hand-raised 500 GB survives)."""
    try:
        paid_gb = float(getattr(config, 'PAID_TRAFFIC_GB', 100) or 100)
    except (TypeError, ValueError):
        paid_gb = 100.0
    return max(float(getattr(user, 'quota_gb', 0) or 0), paid_gb)


def _paid_client_values(user, config, expiry_ms: int) -> dict:
    """The panel fields of a paid key that expires at ``expiry_ms``."""
    return {
        'expiryTime': expiry_ms,
        'enable': True,
        'totalGB': int(paid_quota_gb(user, config) * BYTES_PER_GB),
        'limitIp': paid_limit_ip(user, config),
    }


def _paid_until_ms(expiry) -> int:
    """``subscription_expiry`` in ms the way ``grant_paid_access`` sends
    it (``paid_until.timestamp()``), so the panel gets the date the
    payment wrote. 0 = no expiry, for no date or one that does not
    parse: the payer rule (``ru_exit.is_eligible``) reads both as no
    limit."""
    if not expiry:
        return 0
    try:
        return int(datetime.fromisoformat(str(expiry).strip()).timestamp() * 1000)
    except (TypeError, ValueError, OverflowError, OSError):
        return 0


def tier_client_values(user, config,
                       now: Optional[datetime] = None) -> Optional[dict]:
    """The panel fields (``expiryTime`` ms, 0 = none; ``enable``;
    ``totalGB`` bytes; ``limitIp``) of the key a key holder's tier
    entitles them to, for putting back a key the panel lost. None for a
    lapsed payer: only a payment gives them an enabled key again.

    - A payer is who ``ru_exit.is_eligible`` lets in (it asks for a
      uuid, which a key holder has): status 'paid' until a date that has
      not passed, or a payer with an open ticket. Paid quota
      (``paid_quota_gb``), the paid-until date and ``paid_limit_ip``.
    - A lapsed payer: status 'paid' after the date (a lapsed payer keeps
      the status; the panel's expiry is what switches the key off), or
      'support_topic' with a date that has passed (demo users never
      have one).
    - Everyone else holds a demo key: its own monthly quota
      (``users.quota_gb``, which the monthly reset, the NekoVPN app's
      raise and an admin's gift write; never below the other-clients
      allowance, see ``demo_tiers``), DEMO_DAYS from now like a new key,
      the user's own address limit. The monthly demo job renews it on
      the 1st.
    """
    from bot.services.demo_tiers import demo_traffic_gb
    from bot.services.ru_exit import is_eligible
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    expiry = getattr(user, 'subscription_expiry', None)
    if is_eligible(user, now):
        return _paid_client_values(user, config, _paid_until_ms(expiry))
    status = getattr(user, 'status', None)
    if status == UserState.PAID.value or (
            status == UserState.SUPPORT_TOPIC.value and expiry):
        return None
    try:
        demo_gb = float(getattr(user, 'quota_gb', 0) or 0)
    except (TypeError, ValueError):
        demo_gb = 0.0
    demo_gb = max(demo_gb, float(demo_traffic_gb(config, in_app=False)))
    try:
        days = int(getattr(config, 'DEMO_DAYS', 30))
    except (TypeError, ValueError):
        days = 30
    try:
        limit_ip = int(getattr(user, 'limit_ip', 1))
    except (TypeError, ValueError):
        limit_ip = 1
    expiry_ms = 0
    if days > 0:
        until = now.replace(tzinfo=timezone.utc) + timedelta(days=days)
        expiry_ms = int(until.timestamp() * 1000)
    return {
        'expiryTime': expiry_ms,
        'enable': True,
        'totalGB': int(demo_gb * BYTES_PER_GB),
        'limitIp': limit_ip,
    }


def readd_client(xui, config, user, values: dict) -> bool:
    """Put a client the panel lost back under the SAME uuid, so the keys
    the user installed work again, with ``values`` (expiryTime, enable,
    totalGB, limitIp).

    The add of a lost client only re-attaches its relational record:
    the stale client_traffics row (enable=0, old expiry) still gates
    xray/hy2 until an in-place update touches it, so the update follows
    and its result is the answer. Neither call touches the row's used
    bytes. A client the panel still has is another matter: its add is
    delete + re-add, which zeroes them."""
    readded = xui.add_client_sync({
        'id': user.uuid,
        'flow': 'xtls-rprx-vision',
        'email': user.email,
        **values,
    }, int(getattr(config, 'INBOUND_ID', 1) or 1))
    if not readded:
        return False
    if xui.sync_client_settings_sync(user.email, values):
        return True
    logger.warning(
        f"readd_client: the key of {getattr(user, 'chat_id', '?')} is back "
        f"on the panel, but the in-place update failed: its stale traffic "
        f"row still gates it"
    )
    return False


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

    user.subscription_expiry = paid_until.isoformat()
    # Floor, never a downgrade: a hand-raised 500 GB quota survives.
    user.quota_gb = paid_quota_gb(user, config)
    user.limit_ip = paid_limit_ip(user, config)
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
        updates = _paid_client_values(
            user, config, int(paid_until.timestamp() * 1000))
        try:
            ok = xui.sync_client_settings_sync(user.email, updates)
            if not ok and getattr(user, 'uuid', None):
                # The panel purges clients that stay expired long
                # enough — re-provision with the SAME uuid so keys the
                # user already installed start working again.
                ok = readd_client(xui, config, user, updates)
            result['panel_ok'] = bool(ok)
        except Exception as e:
            result['panel_ok'] = False
            logger.warning(f"grant_paid_access: panel sync failed: {e}")

    result['user'] = user
    return result
