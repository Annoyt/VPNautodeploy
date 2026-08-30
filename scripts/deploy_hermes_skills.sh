#!/usr/bin/env bash
# Deploy Hermes agent skills + workspace AGENTS.md from the repo to entry.
#
# The repo copies are sanitized templates (placeholders like <entry-host>);
# real values live in scripts/hermes_skill_vars.local (UNTRACKED — never
# commit it). This script substitutes them and rsyncs the result, so the
# deployed skills stop drifting from the repo by hand-edits.
#
# Usage (from the repo root, on the dev machine):
#   ./scripts/deploy_hermes_skills.sh
#
# hermes_skill_vars.local format (shell vars):
#   ENTRY_HOST=1.2.3.4
#   EXIT_HOST=5.6.7.8
#   EXIT_HOSTNAME=somehost
#   SUPER_ADMIN_ID=123456
#   FORUM_GROUP_ID=-100123
#   DASHBOARD_HOST=example.duckdns.org
set -euo pipefail

cd "$(dirname "$0")/.."

VARS_FILE="scripts/hermes_skill_vars.local"
if [ ! -f "$VARS_FILE" ]; then
    echo "ERROR: $VARS_FILE not found — create it (see header of this script)." >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$VARS_FILE"
: "${ENTRY_HOST:?}" "${EXIT_HOST:?}" "${EXIT_HOSTNAME:?}" "${SUPER_ADMIN_ID:?}" "${DASHBOARD_HOST:?}"

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

cp -r skills/. "$STAGE/skills/"
mkdir -p "$STAGE/workspace"
cp hermes/AGENTS.md "$STAGE/workspace/AGENTS.md"

find "$STAGE" -name '*.md' -exec sed -i \
    -e "s|<entry-host>|$ENTRY_HOST|g" \
    -e "s|<exit-host>|$EXIT_HOST|g" \
    -e "s|<exit-hostname>|$EXIT_HOSTNAME|g" \
    -e "s|<super-admin-id>|$SUPER_ADMIN_ID|g" \
    -e "s|<forum-group-id>|${FORUM_GROUP_ID:-<forum-group-id>}|g" \
    -e "s|<dashboard-host>|$DASHBOARD_HOST|g" {} +

echo "==> Syncing skills (only ours — stock hermes skills untouched)"
for d in "$STAGE"/skills/*/; do
    name=$(basename "$d")
    rsync -a --delete "$d" "entry:/root/.hermes/skills/$name/"
    echo "  $name"
done

echo "==> Syncing workspace AGENTS.md"
rsync -a "$STAGE/workspace/AGENTS.md" entry:/root/hermes-work/AGENTS.md

echo "==> Done. Restart hermes-api to pick up AGENTS.md changes:"
echo "    ssh entry systemctl restart hermes-api.service"
