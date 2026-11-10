"""SSH as the paid users' emergency path (IMPROVEMENT_PLAN F, E4).

Public reports from 2025–2026 describe Russian networks freezing a TCP
connection to foreign hosting after ~16 KB — and SSH as exempt from that
rule. An SSH server is a different kind of traffic altogether, and both
cores we serve speak it: mihomo (``type: ssh``) and sing-box (``ssh``
outbound). So the emergency profiles (``/sub?emergency=1``, the SOS kit,
the lockdown profile) of paid users get one more server, last: an sshd on
exit that only forwards (no shell, no tty).

* **Who:** ``ru_exit.is_eligible`` — paid and not lapsed. There is no
  traffic accounting on this path, hence paid only, and only in the
  emergency profiles.
* **Credentials** are derived, never stored: user ``n<12 hex of
  sha256(uuid)>``, password = HMAC-SHA256(SSH_USER_SECRET, uuid),
  base64url, 32 chars. The profile carries them; the server gets only
  SHA-512 crypt hashes from ``GET /api/ssh/users`` (``users_payload``).
* **Server:** ``scripts/setup_ssh_exit.sh`` on exit — a container with
  ``ForceCommand /bin/false`` and forwarding only, users synced from that
  endpoint by a timer, its host key pinned in the profile
  (``SSH_HOST_KEY``).

Off while SSH_EXIT_HOST, SSH_USER_SECRET or SSH_HOST_KEY is empty.
"""

import base64
import hashlib
import hmac
import logging
import warnings
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)


def enabled(config) -> bool:
    return all(isinstance(getattr(config, k, ''), str) and getattr(config, k, '').strip()
               for k in ('SSH_EXIT_HOST', 'SSH_USER_SECRET', 'SSH_HOST_KEY'))


def username_for(uuid: str) -> str:
    return 'n' + hashlib.sha256(str(uuid).encode('utf-8')).hexdigest()[:12]


def password_for(config, uuid: str) -> str:
    secret = (getattr(config, 'SSH_USER_SECRET', '') or '').encode('utf-8')
    mac = hmac.new(secret, str(uuid).encode('utf-8'), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode('ascii').rstrip('=')[:32]


def _port(config) -> int:
    try:
        return int(getattr(config, 'SSH_EXIT_PORT', 22022) or 22022)
    except (TypeError, ValueError):
        return 22022


def eligible(user, now: Optional[datetime] = None) -> bool:
    try:
        from bot.services.ru_exit import is_eligible
        return bool(is_eligible(user, now or datetime.utcnow()))
    except Exception:
        return False


def outbound(config, user, now: Optional[datetime] = None) -> Optional[dict]:
    """The sing-box ``ssh`` outbound for ``user`` (the Clash converter reads
    the same dict), or None when off or the user is not eligible."""
    uuid = getattr(user, 'uuid', None)
    if not (enabled(config) and uuid and eligible(user, now)):
        return None
    prefix = (getattr(user, 'email', '') or 'user').split('@')[0]
    return {
        'type': 'ssh',
        'tag': f'{prefix}-ssh',
        'server': config.SSH_EXIT_HOST.strip(),
        'server_port': _port(config),
        'user': username_for(uuid),
        'password': password_for(config, uuid),
        'host_key': [config.SSH_HOST_KEY.strip()],
    }


def _crypt_sha512(password: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        import crypt
    return crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))


def users_payload(db, config, now: Optional[datetime] = None) -> List[dict]:
    """``[{'user', 'hash'}]`` for every eligible user — what the exit's sync
    puts into its sshd: SHA-512 crypt hashes, never a password or a uuid."""
    if not enabled(config):
        return []
    now = now or datetime.utcnow()
    from types import SimpleNamespace
    out = []
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT uuid, status, subscription_expiry, previous_state FROM users "
            "WHERE status IN ('paid', 'support_topic') "
            "AND uuid IS NOT NULL AND uuid != ''").fetchall()
    for uuid, status, expiry, previous in rows:
        user = SimpleNamespace(uuid=uuid, status=status, subscription_expiry=expiry,
                               previous_state=previous)
        if not eligible(user, now):
            continue
        out.append({'user': username_for(uuid),
                    'hash': _crypt_sha512(password_for(config, uuid))})
    return out
