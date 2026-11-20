"""A request's body read to its end, up to a limit.

``request.content.read(n)`` returns what has arrived so far, at most ``n``
bytes, not ``n`` bytes: a body that came in pieces (chunked, or just split
on the way) was cut at its first piece. Until 2026-11-20 the NekoVPN app's
SOS report (``POST /api/neko/sos/<token>``) read its body that way and
answered such a body with 400.
"""

from typing import Optional


async def read_body(request, limit: int) -> Optional[bytes]:
    """The whole body, or None when it is over ``limit``: by its
    Content-Length, before anything is read, or by what arrives."""
    if (request.content_length or 0) > limit:
        return None
    chunks, size = [], 0
    while True:
        chunk = await request.content.read(limit + 1 - size)
        if not chunk:
            return b''.join(chunks)
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
