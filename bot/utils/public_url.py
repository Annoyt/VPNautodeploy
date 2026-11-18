"""The bot's two public addresses (AGENTS.md §60).

``WEBAPP_URL`` is the admins' dashboard (the Telegram Mini App, /admin).
``SUB_PUBLIC_URL`` is what users fetch: subscription links, the profiles' rule
sets and providers, the app's telemetry, /open, /app and /apk. Empty, it is
``WEBAPP_URL``, which is how the bot ran until 2026-11-18. That day exit's
IPv4 was blocked from Russia and the users' address had to move to entry,
while the dashboard stayed where it was.
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
