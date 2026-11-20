"""The web server's access log, without the credentials some URLs carry.

aiohttp's own access logger writes each request line and Referer as they
came, into ``docker logs vpn-bot`` and /var/log/vpn-bot/bot.log (the
dashboard's Logs tab). Until 2026-11-20 the dashboard put its credentials
in every URL it called: ``admin_token`` (valid for an hour) and
``initData`` (no expiry: a logged one opens the dashboard until the bot
token changes), and the page's own address, ``/?admin_token=…``, came back
as the Referer of each call. The dashboard now sends them in the
Authorization header, but the URL still carries them in three cases: the
WebSocket (a browser gives a WebSocket no headers), a page loaded before
the change, and the ``/admin`` link the page opens with. A user's
subscription token is always a path segment: ``/sub/<token>``,
``/open/<token>``, ``/app/<token>``, ``/probe/<token>/<group>``,
``/api/neko/sos/<token>``.

RedactingAccessLogger writes aiohttp's default line with those values
replaced by ``[redacted]``, in the request target and in the Referer.
"""

from __future__ import annotations

import datetime
import logging
import re
import time as time_mod
from urllib.parse import unquote, unquote_plus

from aiohttp.abc import AbstractAccessLogger

REDACTED = '[redacted]'

# Query parameters that carry a credential. Compared lowercased and
# percent-decoded: aiohttp decodes a name before a handler reads it.
SECRET_PARAMS = frozenset({'admin_token', 'initdata'})

# The path segment after one of these is a user's subscription token.
_TOKEN_AFTER = frozenset({'sub', 'open', 'app', 'probe'})
_SOS_PREFIX = ('api', 'neko', 'sos')

_QUERY_SEPARATORS = re.compile(r'([&;])')


def redact_url(url: str) -> str:
    """``url`` (a request target or a Referer) with every credential in
    it replaced by REDACTED, everything else as it came."""
    if not url:
        return url
    path, sep, query = url.partition('?')
    segments = path.split('/')
    names = [unquote(s).lower() for s in segments]
    for i in range(1, len(segments)):
        if segments[i] and (names[i - 1] in _TOKEN_AFTER
                            or tuple(names[max(0, i - 3):i]) == _SOS_PREFIX):
            segments[i] = REDACTED
    path = '/'.join(segments)
    if sep:
        pieces = _QUERY_SEPARATORS.split(query)
        for j in range(0, len(pieces), 2):        # the odd ones are separators
            name, eq, _value = pieces[j].partition('=')
            if eq and unquote_plus(name).strip().lower() in SECRET_PARAMS:
                pieces[j] = f'{name}={REDACTED}'
        query = ''.join(pieces)
    return path + sep + query


class RedactingAccessLogger(AbstractAccessLogger):
    """aiohttp's default access line (``%a %t "%r" %s %b "%{Referer}i"
    "%{User-Agent}i"``) with redact_url() applied to the request target
    and the Referer.

    The line is built here rather than by aiohttp's AccessLogger: that
    class finds the formatter of each field on itself by name, so a
    subclass cannot change one. ``log_format`` is not read, and the
    record carries no ``extra`` fields: nothing here reads them.
    """

    def log(self, request, response, time: float) -> None:
        try:
            # %t as aiohttp writes it: the request's start, in local time
            tz = datetime.timezone(datetime.timedelta(seconds=-time_mod.timezone))
            start = datetime.datetime.now(tz) - datetime.timedelta(seconds=time)
            if request is None:
                remote = first_line = referer = agent = '-'
            else:
                remote = request.remote or '-'
                first_line = '{} {} HTTP/{}.{}'.format(
                    request.method, redact_url(request.path_qs),
                    request.version.major, request.version.minor)
                referer = redact_url(request.headers.get('Referer', '-'))
                agent = request.headers.get('User-Agent', '-')
            self.logger.info('%s [%s] "%s" %s %s "%s" "%s"',
                             remote, start.strftime('%d/%b/%Y:%H:%M:%S %z'),
                             first_line, response.status, response.body_length,
                             referer, agent)
        except Exception:
            self.logger.exception('Error in logging')

    @property
    def enabled(self) -> bool:
        # aiohttp skips log() altogether when this is False
        return self.logger.isEnabledFor(logging.INFO)
