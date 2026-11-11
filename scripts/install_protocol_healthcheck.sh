#!/usr/bin/env bash
# Install the protocol healthcheck timer on ENTRY (IMPROVEMENT_PLAN F, D1).
# Run as root from the rsync'd checkout:
#
#     sudo /opt/vpn-bot/scripts/install_protocol_healthcheck.sh
#
# The timer runs as root, so it runs root-owned COPIES of the script and of
# bot/services/probe_rules.py (stdlib-only, loaded by path), never the files
# in /opt/vpn-bot: that tree belongs to the deploy account (AGENTS.md §36).
# Re-run after either file changes. `--remove` stops and removes it all.
set -euo pipefail

LIB_DIR="${HC_LIB_DIR:-/usr/local/lib/nekovpn-healthcheck}"
UNIT_DIR="${HC_UNIT_DIR:-/etc/systemd/system}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$(id -u)" != "0" ] && [ "${HC_ALLOW_NONROOT:-0}" != "1" ]; then
    echo "run as root" >&2
    exit 1
fi

if [ "${1:-}" = "--remove" ]; then
    "$SYSTEMCTL" disable --now protocol-healthcheck.timer >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/protocol-healthcheck.service" "$UNIT_DIR/protocol-healthcheck.timer"
    rm -rf "$LIB_DIR"
    "$SYSTEMCTL" daemon-reload
    echo "removed"
    exit 0
fi

for f in "$SRC/scripts/protocol_healthcheck.py" "$SRC/bot/services/probe_rules.py" \
         "$SRC/systemd/protocol-healthcheck.service" "$SRC/systemd/protocol-healthcheck.timer"; do
    [ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done

install -d -m 0755 "$LIB_DIR"
install -m 0755 "$SRC/scripts/protocol_healthcheck.py" "$LIB_DIR/protocol_healthcheck.py"
install -m 0644 "$SRC/bot/services/probe_rules.py" "$LIB_DIR/probe_rules.py"
if [ "$(id -u)" = "0" ]; then
    chown -R root:root "$LIB_DIR"
fi
install -m 0644 "$SRC/systemd/protocol-healthcheck.service" "$UNIT_DIR/protocol-healthcheck.service"
install -m 0644 "$SRC/systemd/protocol-healthcheck.timer" "$UNIT_DIR/protocol-healthcheck.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now protocol-healthcheck.timer
echo "installed: $LIB_DIR, timer protocol-healthcheck.timer enabled"
