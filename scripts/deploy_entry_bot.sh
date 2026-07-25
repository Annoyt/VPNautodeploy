#!/usr/bin/env bash
# Deploy the vpn-bot code currently on entry's /opt/vpn-bot into a fresh
# image and restart ONLY the vpn-bot container.
#
# Why this exists: `docker compose up -d vpn-bot` alone still recreates
# every dependency (3x-ui, via depends_on) if compose thinks anything in
# its config changed. On 2026-07-19 that silently pulled a breaking 3x-ui
# major upgrade off an unpinned :latest tag and wiped the panel. This
# script hardcodes --no-deps so a vpn-bot deploy can never again touch
# 3x-ui as a side effect — see project_xui_incident_2026_07_19 in memory.
#
# Usage (run ON entry, from /opt/vpn-bot, after rsync'ing changed files):
#   ./scripts/deploy_entry_bot.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> Building vpn-bot image"
docker compose build vpn-bot

echo "==> Starting vpn-bot (--no-deps: 3x-ui is never touched by this script)"
docker compose up -d --no-deps vpn-bot

echo "==> Waiting for health"
for i in $(seq 1 12); do
    status=$(docker inspect --format '{{.State.Health.Status}}' vpn-bot 2>/dev/null || echo "unknown")
    echo "  check $i: $status"
    if [ "$status" = "healthy" ]; then
        echo "==> vpn-bot is healthy"
        exit 0
    fi
    sleep 5
done

echo "==> vpn-bot did not report healthy in time — check: docker logs vpn-bot --tail 50" >&2
exit 1
