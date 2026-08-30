#!/usr/bin/env bash
# One-command deploy from the dev repo to the prod vpn-bot on entry,
# with a version stamp and a post-deploy smoke check.
#
# Why: /opt/vpn-bot on entry is an rsync copy, not a git checkout.
# Fixes landed in the repo but never rsynced simply don't exist in prod
# (65b9b4c sat undeployed for 11 days while everyone debugged a "bug").
# This script stamps the deploy with the git sha, ships the code, runs
# the remote rebuild, then verifies /health reports that exact sha.
#
# Usage:
#   ./scripts/deploy_to_entry.sh              # full bot/ + deploy script
#   ./scripts/deploy_to_entry.sh bot/core/web_server.py bot/webapp/app.js
#
# NEVER pass compose files or .env here — entry keeps hand-tuned copies
# (see project-deploy-entry-bot memory / AGENTS.md).
set -euo pipefail

cd "$(dirname "$0")/.."

sha=$(git rev-parse --short HEAD)
if ! git diff --quiet -- bot/ scripts/ || ! git diff --cached --quiet -- bot/ scripts/; then
    sha="${sha}-dirty"
fi
stamp="${sha} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "$stamp" > bot/version.txt
echo "==> Version stamp: $stamp"

paths=("$@")
if [ ${#paths[@]} -eq 0 ]; then
    paths=(bot/ scripts/deploy_entry_bot.sh)
fi
# version.txt must always ship, whatever subset is deployed.
paths+=(bot/version.txt)

for p in "${paths[@]}"; do
    case "$p" in
        bot/*|bot|scripts/*) ;;
        *) echo "ERROR: refusing to deploy '$p' — only bot/ and scripts/ belong here." >&2
           exit 1 ;;
    esac
done

echo "==> Rsync to entry:/opt/vpn-bot (itemized — watch for unexpected drift)"
rsync -aviR "${paths[@]}" entry:/opt/vpn-bot/ | grep -v '/$' || true

echo "==> Remote rebuild (--no-deps)"
ssh entry "cd /opt/vpn-bot && ./scripts/deploy_entry_bot.sh"

echo "==> Smoke: /health version must match the stamp"
deployed=$(ssh entry "curl -s --max-time 10 http://127.0.0.1:8080/health" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('version','?')); exit(0 if d.get('status')=='healthy' else 1)")
echo "    deployed: $deployed"
if [ "$deployed" != "$stamp" ]; then
    echo "ERROR: version mismatch — prod runs '$deployed', expected '$stamp'." >&2
    exit 1
fi
echo "==> Deploy verified: $sha is live."
