"""Which hy2 keys the auth callbacks let in (IMPROVEMENT_PLAN F, A6; AGENTS.md §56).

Both hysteria instances on exit ask a relay on exit's loopback
(``scripts/hy2_auth_relay.py``) instead of this bot. The relay forwards every
request here and returns the answer. While the bot does not answer (a deploy,
a restart, an outage) it answers from memory: the allows it saw, and the list
``GET /api/hy2/keys`` gives it every minute, each for at most 15 minutes.

One definition for the live callback (``WebAppServer._hy2_auth_common``) and
that list (``WebAppServer.handle_hy2_keys``): ``allows`` is the status, expiry
and tier gate, and the quota gate is the server's cached panel reading
(``WebAppServer._hy2_quota_denies``). If the two disagreed, the relay would let
in from the list a key the live callback refuses.

The list carries hashes, never a key. ``key_hash`` is mirrored in the relay,
which runs on exit and imports nothing from the bot; a test pins the two.
"""

import hashlib
from collections import Counter
from datetime import datetime, timezone

# Domain separation: the same uuid hashed for another purpose (the SSH exit's
# user names hash it too, bot/services/ssh_exit.py) gives a different digest.
HASH_PREFIX = b'nekovpn-hy2-auth\x00'

# The two callbacks: (route, paid_only). The main instance is freemium since
# 2026-08-23; turbo stays paid-only.
ROUTES = (('hy2', False), ('hy2t', True))


def key_hash(key: str) -> str:
    """The relay's name for a key: sha256 over the prefix and the key."""
    return hashlib.sha256(HASH_PREFIX + key.encode('utf-8', 'replace')).hexdigest()


def utc_now() -> datetime:
    """Naive UTC, what the callback has always compared subscription_expiry
    with (it is written without a zone)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def expired(expiry, now=None) -> bool:
    """The callback's expiry rule: a parseable date in the past. An empty or
    unparseable one is no limit."""
    if not expiry:
        return False
    try:
        return datetime.fromisoformat(expiry) < (now or utc_now())
    except Exception:
        return False


def allowed_statuses(paid_only: bool) -> set:
    """Turbo mirrors PROTOCOL_TIER: a demo key must not unlock the Brutal
    instance. The main instance also takes demo; every other status (new,
    pending, rejected, banned) fails on both."""
    from bot.handlers.callbacks.user import MyKeyAnswerHandler
    allowed = set(MyKeyAnswerHandler.PAID_USER_STATUSES)
    if not paid_only:
        allowed.add('demo')
    return allowed


def allows(status, expiry, paid_only: bool, now=None) -> bool:
    """The status, expiry and tier gate of one users row."""
    return not expired(expiry, now) and status in allowed_statuses(paid_only)


def read_rows(db) -> list:
    """``(uuid, chat_id, email, status, expiry)`` of every user with a key, a
    uuid held by more than one row left out: the live lookup takes whichever
    row sqlite returns first, and the list must not guess. RAISES when bot.db
    cannot be read: the caller answers 503, never an empty list."""
    with db._connect() as conn:
        rows = [tuple(r) for r in conn.execute(
            "SELECT uuid, chat_id, email, status, subscription_expiry FROM users "
            "WHERE uuid IS NOT NULL AND uuid != ''"
        ).fetchall()]
    count = Counter(r[0] for r in rows)
    return [r for r in rows if count[r[0]] == 1]
