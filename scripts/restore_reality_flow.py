#!/usr/bin/env python3
"""One-shot repair: restore ``flow`` on every VLESS-Reality client.

WHY THIS EXISTS
---------------
On 2026-09-01 00:00 UTC the monthly quota-reset jobs updated all 81 panel
clients through ``XUIService._update_client_fields_async``. That helper
reads the client record from the ShadowTLS/SS inbound (the only one that
carries the SS-2022 password) and writes the SAME body back to every
inbound. The SS record has no ``flow`` key, so ``_to_v34_client`` emitted
``flow: ""`` and the panel persisted an empty flow on the Reality
inbound. VLESS-Reality clients all use ``xtls-rprx-vision``; with the
server side flowless every handshake failed. Reality served ZERO
connections for four days while probes reported 0/960.

WHAT IT DOES
------------
1. Lists the clients of the Reality inbound that are missing ``flow``.
2. Re-sends each through the panel's update endpoint WITH the flow set.
   The panel itself stores flow only on the VLESS inbound, so the other
   inbounds are unaffected in the DATABASE.
3. Forces a panel-side Xray restart. This is REQUIRED: the fork applies
   client edits to the running core over its API, and that hot-apply
   fails for shadowsocks inbounds when a flow is present ("Unknown
   account type: x" — the leading char of xtls-rprx-vision), which
   silently drops those clients from the RUNNING config. The restart
   makes the panel regenerate config.json from the database, which is
   the only self-consistent state.

Run it ON ENTRY, inside the bot container (it needs the panel creds from
the bot's env)::

    docker cp scripts/restore_reality_flow.py vpn-bot:/tmp/
    docker exec vpn-bot sh -c 'cd /app && cp /tmp/restore_reality_flow.py . \
        && python3 restore_reality_flow.py --apply; rm -f restore_reality_flow.py'

Without ``--apply`` it only reports what it would change.

BACK UP THE PANEL DB FIRST (on exit)::

    docker cp 3x-ui:/etc/x-ui/x-ui.db /opt/backups/x-ui.db.$(date +%F-%H%M%S)
"""

import argparse
import asyncio
import sys
import time

VISION = "xtls-rprx-vision"


async def _main(apply_changes: bool, restart: bool) -> int:
    from bot.config import Settings
    from bot.services.xui_service import XUIService

    cfg = Settings()
    xui = XUIService(cfg)
    if not xui.api:
        print("ERROR: panel API not configured (XUI_API_URL missing)")
        return 2

    reality_id = int(getattr(cfg, "INBOUND_ID", 0) or 1)
    inbound = await xui.api.get_inbound(reality_id)
    if not inbound:
        print(f"ERROR: inbound {reality_id} not readable")
        return 2

    clients = (inbound.get("settings") or {}).get("clients") or []
    missing = [c for c in clients if not c.get("flow")]
    print(f"inbound {reality_id}: {len(clients)} clients, "
          f"{len(missing)} missing flow")
    if not missing:
        print("nothing to repair")
        return 0
    if not apply_changes:
        for c in missing[:10]:
            print("  would fix:", c.get("email"))
        if len(missing) > 10:
            print(f"  … and {len(missing) - 10} more")
        print("dry run — pass --apply to write")
        return 0

    ok = failed = 0
    for i, c in enumerate(missing, 1):
        email = c.get("email") or ""
        if not email:
            continue
        try:
            done = await xui._update_client_fields_async(email, {"flow": VISION})
        except Exception as e:  # noqa: BLE001 - report and continue
            done = False
            print(f"  [{i}/{len(missing)}] {email}: EXCEPTION {e}")
        if done:
            ok += 1
        else:
            failed += 1
            print(f"  [{i}/{len(missing)}] {email}: FAILED")
        if i % 10 == 0:
            print(f"  … {i}/{len(missing)} processed")
        time.sleep(0.2)   # be gentle with the panel

    print(f"updated: {ok}, failed: {failed}")

    if restart:
        print("restarting Xray via panel (regenerates config.json from DB)…")
        restarted = await xui.api.restart_xray()
        print("restart reported:", restarted)

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default: dry run)")
    ap.add_argument("--no-restart", action="store_true",
                    help="skip the panel-side Xray restart")
    a = ap.parse_args()
    sys.exit(asyncio.run(_main(a.apply, not a.no_restart)))
