"""The bot's public addresses (AGENTS.md §60, §63).

``WEBAPP_URL`` is the admins' dashboard (the Telegram Mini App, /admin).
``SUB_PUBLIC_URL`` is what users fetch: subscription links, the profiles' rule
sets and providers, the app's telemetry, /open, /app and /apk. Empty, it is
``WEBAPP_URL``, which is how the bot ran until 2026-11-18. That day exit's
IPv4 was blocked from Russia and the users' address had to move to entry,
while the dashboard stayed where it was.

``ENTRY_PUBLIC_HOST`` is where users' clients connect for the protocols built
from entry's own address. Empty, it is ``ENTRY_NODE_IP``. On 2026-11-19
AdminVPS moved entry from 130.49.146.10 to 157.22.231.40, and Reality in every
profile named the old address; with a host name here, the next move is a DNS
change.
"""


def public_base_url(config) -> str:
    """Base URL for what users fetch, without a trailing slash; ``''`` when
    the bot knows no public address. A value that is not a string (a test's
    bare Mock) counts as unset."""
    for name in ('SUB_PUBLIC_URL', 'WEBAPP_URL'):
        value = getattr(config, name, '')
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip('/')
    return ''


def entry_public_host(config):
    """The address users' clients connect to for the protocols built from
    entry's own address: Reality and its server-first twin, the SSH path
    through entry, the RU-zone egress, xhr and the share links. An IP or a
    host name.

    ``ENTRY_PUBLIC_HOST`` when it is a non-empty string; otherwise exactly what
    the builders read before it existed (``ENTRY_NODE_IP or ''``), so a config
    without it builds the same bytes."""
    value = getattr(config, 'ENTRY_PUBLIC_HOST', '')
    if isinstance(value, str) and value.strip():
        return value.strip()
    return getattr(config, 'ENTRY_NODE_IP', '') or ''
