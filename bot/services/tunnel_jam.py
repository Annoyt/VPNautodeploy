"""Is our own exit → entry tunnel jammed? (AGENTS.md §64, §66)

The healthcheck's watchdog (``scripts/protocol_healthcheck.py``, layer B)
pages ``healthcheck:entry_tunnel:fd`` when the reverse tunnel's sshd session
holds half its open-file limit or more, and ``healthcheck:entry_tunnel:queue``
when connections wait on a forwarded port. While one is open, users' clients
retry through the jam and fail their REALITY handshakes: 2026-11-19/20, about
1,850 failures in 17 hours, nearly all «handshake did not complete
successfully» from our users' own addresses. Those failures are our path's,
not an operator's or a prober's: the DPI handshake alerts say so, and
DPIMonitor's R3 gives no verdict on them.

AlertManager keeps every alert's incident in ``alert_state`` (``open``,
``opened_ts``, ``closed_ts``; unix seconds). A jam that closed a moment ago
still counts for a window whose failures it caused, so the readers pass the
start of their own window.

``reach:exit`` is no jam: it can stay open for days while exit's direct
address is blocked (§61). Stdlib only; the caller owns the connection.
"""

from __future__ import annotations

from typing import List, Tuple

PREFIX = 'healthcheck:entry_tunnel:'


def jams(conn, since_ts: float) -> List[Tuple[str, float, float, bool]]:
    """The tunnel alerts open now or closed at ``since_ts`` or later:
    ``[(key, opened_ts, closed_ts, still_open)]``, oldest first. Raises on
    a read error: whether "could not look" freezes or passes is the
    caller's to say."""
    rows = conn.execute(
        "SELECT key, open, opened_ts, closed_ts FROM alert_state "
        "WHERE open = 1 OR closed_ts >= ? ORDER BY opened_ts, key",
        (since_ts,),
    ).fetchall()
    # By prefix in Python: in SQL LIKE the '_' of 'entry_tunnel' is a
    # wildcard and would let other keys in.
    return [(str(key), float(opened or 0), float(closed or 0), bool(is_open))
            for key, is_open, opened, closed in rows
            if str(key).startswith(PREFIX)]
