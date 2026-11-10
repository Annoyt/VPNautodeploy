"""The TLS fingerprint each user's client presents (IMPROVEMENT_PLAN F, E1).

Every TLS server we hand out — Reality, the CDN ws, ShadowTLS's frontend,
the DE reserve, the RU-zone, every share link and the xray format — used
``chrome`` for everyone. Public reports from 2026 describe a freeze keyed on
a series of TLS handshakes with a flagged uTLS fingerprint (Chrome, Safari,
iOS) from a "suspicious" subnet; with one fingerprint for the whole base
there is nothing to compare and nothing to rotate to.

``TLS_FINGERPRINTS`` (env, comma-separated) is the pool; each user gets one
value from it, chosen by their uuid, so a user's fingerprint is stable
across profile fetches and devices, and the pool splits the base. Unset or
unusable = ``chrome`` for everyone, byte-identical profiles.

Only values every client core we serve knows are accepted: mihomo
(``client-fingerprint``), sing-box (``utls.fingerprint``) and xray
(``fingerprint`` / ``fp``) share chrome, firefox, edge, safari, ios,
android, 360, qq and random.
"""

import hashlib
from typing import Optional, Tuple

DEFAULT = 'chrome'
ALLOWED = ('chrome', 'firefox', 'edge', 'safari', 'ios', 'android', '360', 'qq', 'random')


def pool(config) -> Tuple[str, ...]:
    """The configured pool, in order, unknown and repeated values dropped;
    ``(DEFAULT,)`` when nothing usable is configured."""
    raw = getattr(config, 'TLS_FINGERPRINTS', '') if config is not None else ''
    if not isinstance(raw, str):
        return (DEFAULT,)
    out = []
    for item in raw.split(','):
        fp = item.strip().lower()
        if fp in ALLOWED and fp not in out:
            out.append(fp)
    return tuple(out) or (DEFAULT,)


def for_user(config, uuid: Optional[str]) -> str:
    """The fingerprint for the user with ``uuid``: stable, one per user."""
    choices = pool(config)
    if len(choices) == 1 or not uuid:
        return choices[0]
    digest = hashlib.sha256(str(uuid).encode('utf-8')).digest()
    return choices[int.from_bytes(digest[:4], 'big') % len(choices)]
