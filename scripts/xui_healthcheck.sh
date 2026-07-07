#!/usr/bin/env bash
# VPN self-heal watchdog for the EXIT node.
#
# Why: the exit is a small (929 MB) VPS. `memory_watchdog.sh` reboots the
# host when MemAvailable stays under ~100 MB, and 3x-ui does not always
# come back after such a reboot (observed: Exited 255, RestartCount 0,
# down for hours → VPN outage). This script brings the VPN container back
# within a minute regardless of how it died.
#
# Install (on the exit host):
#   install -m 0755 scripts/xui_healthcheck.sh /usr/local/bin/xui_healthcheck.sh
#   printf 'SHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n* * * * * root /usr/local/bin/xui_healthcheck.sh\n' > /etc/cron.d/xui-healthcheck
set -uo pipefail

cd /opt/vpn-bot 2>/dev/null || exit 0

status="$(docker inspect -f '{{.State.Status}}' 3x-ui 2>/dev/null || echo missing)"
if [ "$status" != "running" ]; then
    logger -t xui-healthcheck "3x-ui status=${status} — restarting VPN"
    docker compose up -d 3x-ui >/dev/null 2>&1 || docker start 3x-ui >/dev/null 2>&1 || true
fi
