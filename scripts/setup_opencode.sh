#!/usr/bin/env bash
# Set up the OpenCode headless server as the vpn-bot /ai backend.
#
# Replaces the old kimi-code CLI + kimi-bridge. OpenCode ships its own
# HTTP server (`opencode serve`), so there is no custom FastAPI bridge
# anymore — the bot talks straight to it with HTTP basic auth.
#
# Run on the node that should host the agent (control node by default;
# see AGENT_NODE_TYPE). Idempotent: safe to re-run.
#
# Required env (export before running, or put in /etc/opencode.env):
#   OPENCODE_SERVER_PASSWORD   basic-auth password the bot will use
#   <provider key>            e.g. MOONSHOT_API_KEY or ANTHROPIC_API_KEY,
#                             whichever your opencode.json "model" needs.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/vpn-bot}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="/etc/opencode.env"

echo "=== Installing OpenCode server ==="

# 1. Install the opencode CLI if missing.
if ! command -v opencode >/dev/null 2>&1; then
  echo "[1/5] Installing opencode…"
  curl -fsSL https://opencode.ai/install | bash
else
  echo "[1/5] opencode already installed: $(command -v opencode)"
fi

# 2. Project config: opencode.json (permissions) lives in the serve
#    working dir so the server enforces the allow/deny bash rules.
echo "[2/5] Installing opencode.json into ${APP_DIR}…"
mkdir -p "${APP_DIR}"
cp "${REPO_DIR}/scripts/opencode.json" "${APP_DIR}/opencode.json"

# 3. Env file with the basic-auth password + provider key(s).
echo "[3/5] Writing ${ENV_FILE}…"
: "${OPENCODE_SERVER_PASSWORD:?set OPENCODE_SERVER_PASSWORD in env before running}"
umask 077
{
  echo "OPENCODE_SERVER_PASSWORD=${OPENCODE_SERVER_PASSWORD}"
  # Pass through whichever provider key is set in the current env.
  [ -n "${MOONSHOT_API_KEY:-}" ]  && echo "MOONSHOT_API_KEY=${MOONSHOT_API_KEY}"
  [ -n "${ANTHROPIC_API_KEY:-}" ] && echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
  [ -n "${OPENROUTER_API_KEY:-}" ] && echo "OPENROUTER_API_KEY=${OPENROUTER_API_KEY}"
} > "${ENV_FILE}"
chmod 600 "${ENV_FILE}"

# 4. Shared out dir for [[SEND_FILE]] handover (bot reads from same mount).
mkdir -p /tmp/agent_out

# 5. systemd unit → opencode serve on :4096.
echo "[4/5] Installing systemd unit…"
cp "${REPO_DIR}/scripts/opencode.service" /etc/systemd/system/opencode.service
systemctl daemon-reload
systemctl enable opencode
systemctl restart opencode

echo "[5/5] Waiting for health…"
sleep 3
if curl -fsS -u "opencode:${OPENCODE_SERVER_PASSWORD}" \
      http://127.0.0.1:4096/global/health >/dev/null 2>&1; then
  echo "✅ opencode server is healthy on :4096"
else
  echo "⚠️  health check failed — inspect: journalctl -u opencode -n 40 --no-pager"
fi

cat <<EOF

=== Configuration for docker-compose .env ===
OPENCODE_URL=http://host.docker.internal:4096
OPENCODE_USERNAME=opencode
OPENCODE_SERVER_PASSWORD=${OPENCODE_SERVER_PASSWORD}
OPENCODE_DEFAULT_MODEL=moonshotai/kimi-k2   # or anthropic/claude-..., etc.
OPENCODE_AGENT_PLAN=plan                     # built-in deep-think agent
AGENT_NODE_TYPE=control

Then: cd ${APP_DIR} && docker compose up -d vpn-bot
NOTE: pick the provider/model in ${APP_DIR}/opencode.json; make sure the
matching API key is in ${ENV_FILE}. Verify the permission schema against
your installed opencode version (opencode.json format can change).
EOF
