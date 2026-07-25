#!/usr/bin/env python3
"""Daily snapshot of 3x-ui's SQLite DB, run on the host via systemd timer.

Why: the 2026-07-19 incident wiped every inbound on entry's 3x-ui panel
with zero backups anywhere to recover from. This uses sqlite3's own
backup API (not a raw file copy) so the snapshot is consistent even
while x-ui has the DB open for writes. See systemd/backup-xui-db.timer.
"""

import sqlite3
import sys
import time
from pathlib import Path

SRC = Path("/var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db")
DEST_DIR = Path("/opt/backups/xui")
RETENTION_DAYS = 14


def main() -> int:
    if not SRC.exists():
        print(f"backup_xui_db: source not found: {SRC}", file=sys.stderr)
        return 1

    DEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = DEST_DIR / f"x-ui_{stamp}.db"

    src_conn = sqlite3.connect(f"file:{SRC}?mode=ro", uri=True)
    dest_conn = sqlite3.connect(dest)
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()

    size_kb = dest.stat().st_size / 1024
    print(f"backup_xui_db: wrote {dest} ({size_kb:.0f} KB)")

    cutoff = time.time() - RETENTION_DAYS * 86400
    for f in DEST_DIR.glob("x-ui_*.db"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            print(f"backup_xui_db: pruned {f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
