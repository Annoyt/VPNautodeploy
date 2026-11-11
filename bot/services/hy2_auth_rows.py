"""The last users row each hy2 key resolved to (IMPROVEMENT_PLAN F, A4).

``WebAppServer._hy2_user_row`` reads the users row behind a hy2 key on a
worker thread. When that read fails (a locked or broken bot.db), the auth
callback decides from the row the key resolved to last time, if that row is
younger than ``MAX_AGE_S``. A key never seen fails closed.

The memory is process-wide, not per server instance, so the places that take
a key away can drop it at once: ``revoke_user_key`` (reject, ban, revoke) and
every ``StateMachine`` status change forget the user's rows. Without that, a
banned key that did not reconnect in between was let in from memory whenever
the read failed later. ``MAX_AGE_S`` is minutes, not hours: a lock rides out
in seconds, and a longer outage should stop letting in what it last saw.
"""

import threading
import time
from typing import Optional, Tuple

MAX_AGE_S = 15 * 60

_lock = threading.Lock()
# uuid -> (monotonic ts, (chat_id, email, status, expiry))
_rows: dict = {}


def remember(uuid: str, row: tuple) -> None:
    with _lock:
        _rows[uuid] = (time.monotonic(), tuple(row))


def recall(uuid: str) -> Optional[Tuple[float, tuple]]:
    """``(age_s, row)`` when a row younger than ``MAX_AGE_S`` is kept."""
    with _lock:
        kept = _rows.get(uuid)
    if kept is None:
        return None
    age = time.monotonic() - kept[0]
    if age >= MAX_AGE_S:
        return None
    return age, kept[1]


def forget(uuid: Optional[str]) -> None:
    if not uuid:
        return
    with _lock:
        _rows.pop(uuid, None)


def forget_chat(chat_id) -> None:
    """Drop every row of ``chat_id`` (a status change: the next auth must
    read the new status, not decide from the old one)."""
    if chat_id is None:
        return
    key = str(chat_id)
    with _lock:
        for uuid in [u for u, (_ts, row) in _rows.items() if str(row[0]) == key]:
            _rows.pop(uuid, None)


def clear() -> None:
    with _lock:
        _rows.clear()
