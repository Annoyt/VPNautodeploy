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


_ITOA64 = './0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz'
# SHA-crypt's byte order for the final digest (Drepper's specification).
_SHA512_ORDER = ((0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4),
                 (47, 5, 26), (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51),
                 (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13), (56, 14, 35),
                 (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19),
                 (62, 20, 41))


def sha512_crypt(password: str, salt: str, rounds: int = 5000) -> str:
    """``$6$<salt>$<hash>`` — glibc's SHA-512 crypt, in Python: the stdlib
    ``crypt`` module is gone from Python 3.13. Default rounds only (the
    form without ``rounds=``), as sshd's chpasswd -e reads it."""
    import hashlib
    key = password.encode('utf-8')
    salt_b = salt.encode('ascii')[:16]
    b = hashlib.sha512(key + salt_b + key).digest()
    a = hashlib.sha512(key + salt_b)
    n = len(key)
    while n > 64:
        a.update(b)
        n -= 64
    a.update(b[:n])
    n = len(key)
    while n:
        a.update(b if n & 1 else key)
        n >>= 1
    a = a.digest()
    dp = hashlib.sha512(key * len(key)).digest()
    p = (dp * (len(key) // 64 + 1))[:len(key)]
    ds = hashlib.sha512(salt_b * (16 + a[0])).digest()
    s = (ds * (len(salt_b) // 64 + 1))[:len(salt_b)]
    c = a
    for i in range(rounds):
        h = hashlib.sha512()
        h.update(p if i & 1 else c)
        if i % 3:
            h.update(s)
        if i % 7:
            h.update(p)
        h.update(c if i & 1 else p)
        c = h.digest()
    out = []
    for b2, b1, b0 in _SHA512_ORDER:
        w = (c[b2] << 16) | (c[b1] << 8) | c[b0]
        for _ in range(4):
            out.append(_ITOA64[w & 0x3f])
            w >>= 6
    w = c[63]
    for _ in range(2):
        out.append(_ITOA64[w & 0x3f])
        w >>= 6
    return f"$6${salt_b.decode('ascii')}${''.join(out)}"


def _crypt_sha512(password: str) -> str:
    import secrets
    salt = ''.join(secrets.choice(_ITOA64) for _ in range(16))
    return sha512_crypt(password, salt)


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
