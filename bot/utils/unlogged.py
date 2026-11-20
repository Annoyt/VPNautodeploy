"""Requests that leave no line in aiohttp's logs.

aiohttp writes a line per request to ``aiohttp.access`` with the caller's
address (``%a``), and, when a handler raises, ``Error handling request from
<address>`` to ``aiohttp.server``. Both reach ``docker logs vpn-bot`` and
bot.log (the dashboard's Logs tab). A handler that must leave no address
behind (``POST /api/neko/netstat``, docs/NEKO6_PLAN.md D2) calls
``unlogged()`` first; the filter ``install()`` puts on both loggers drops
every record of that request, whatever the access log's format or class.

How the filter knows the request: aiohttp runs each request in a task of
its own and writes the access line from that task once the response is out
(web_protocol.py: "a new task is used for copy context vars"), so a context
variable the handler sets is seen there, and in no other request, not even
the next one on the same keep-alive connection.
"""

import contextvars
import logging

LOGGERS = ('aiohttp.access', 'aiohttp.server')

_UNLOGGED = contextvars.ContextVar('unlogged_request', default=False)


class DropUnlogged(logging.Filter):
    """Drops the records of a request that called ``unlogged()``."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _UNLOGGED.get()


_FILTER = DropUnlogged()


def install() -> None:
    """Put the filter on aiohttp's loggers. A second call adds nothing:
    a logger holds a given filter once."""
    for name in LOGGERS:
        logging.getLogger(name).addFilter(_FILTER)


def unlogged() -> None:
    """Keep the current request out of aiohttp's logs."""
    _UNLOGGED.set(True)
