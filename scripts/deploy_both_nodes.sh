#!/bin/bash
# Deploy VPN Bot to Exit Node
# Current infrastructure: single Exit Node + Entry Node (DNAT forwarder)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Source the operator's .env (kept out of git) for IPs + SSH key path.
# Expected vars: EXIT_NODE_IP, ENTRY_NODE_IP, DEPLOY_SSH_KEY.
if [ -f "$PROJECT_DIR/.env" ]; then
    set -a; . "$PROJECT_DIR/.env"; set +a
fi

SSH_KEY="${DEPLOY_SSH_KEY:-${HOME}/.ssh/id_ed25519}"
EXIT_NODE="${EXIT_NODE_IP:?EXIT_NODE_IP must be set in .env or env}"
ENTRY_NODE="${ENTRY_NODE_IP:?ENTRY_NODE_IP must be set in .env or env}"

echo "============================================"
echo "VPN Bot Deployment"
echo "Time: $(date)"
echo "============================================"
echo ""
echo "Exit Node:  $EXIT_NODE (bot + XRay)"
echo "Entry Node: $ENTRY_NODE (DNAT forwarder, manual config)"
echo ""

# Pre-deployment checks
echo "[PRE-CHECK] Validating code..."
cd "$PROJECT_DIR"
python3 -m py_compile bot/main.py || { echo "❌ Syntax error in main.py"; exit 1; }
python3 -m py_compile bot/services/xui_api/client.py || { echo "❌ Syntax error in client.py"; exit 1; }
python3 -m py_compile bot/config/settings.py || { echo "❌ Syntax error in settings.py"; exit 1; }
echo "✅ Code validation passed"
echo ""

# Function to deploy to a node
deploy_to_node() {
    local NODE_IP=$1
    local NODE_NAME=$2
    
    echo "============================================"
    echo "Deploying to $NODE_NAME ($NODE_IP)"
    echo "============================================"
    
    # Backup
    echo "[$NODE_NAME] Creating backup..."
    ssh -i "$SSH_KEY" -o ConnectTimeout=10 "root@$NODE_IP" \
        "mkdir -p /backup/deploy-$(date +%Y%m%d-%H%M%S) && \
         cp -r /opt/vpn-bot /backup/deploy-$(date +%Y%m%d-%H%M%S)/ 2>/dev/null; \
         cp /etc/cascade-vpn/bot.db /backup/deploy-$(date +%Y%m%d-%H%M%S)/ 2>/dev/null; \
         cp /var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db /backup/deploy-$(date +%Y%m%d-%H%M%S)/ 2>/dev/null; \
         echo 'Backup done'" || { echo "❌ Backup failed"; return 1; }
    
    # Copy code
    echo "[$NODE_NAME] Copying code..."
    rsync -avz --exclude='.git' --exclude='.archive' --exclude='tests' \
          --exclude='__pycache__' -e "ssh -i $SSH_KEY" \
          "$PROJECT_DIR/bot/" "root@$NODE_IP:/opt/vpn-bot/bot/" || {
        echo "❌ Copy failed"; return 1;
    }
    
    # Syntax check on remote
    echo "[$NODE_NAME] Syntax check..."
    ssh -i "$SSH_KEY" -o ConnectTimeout=10 "root@$NODE_IP" \
        "cd /opt/vpn-bot && python3 -m py_compile bot/services/xui_api/client.py bot/config/settings.py bot/handlers/admin.py && echo '✅ Syntax OK'" || {
        echo "❌ Syntax check failed"; return 1;
    }
    
    # Restart service via Docker Compose
    echo "[$NODE_NAME] Restarting Docker container..."
    ssh -i "$SSH_KEY" -o ConnectTimeout=10 "root@$NODE_IP" \
        "cd /opt/vpn-bot && docker compose up -d --build vpn-bot && sleep 5 && \
         docker compose ps | grep -E 'vpn-bot.*Up' && echo '✅ Docker container active'" || {
        echo "❌ Docker container failed to start"; 
        ssh -i "$SSH_KEY" "root@$NODE_IP" "docker compose logs vpn-bot --tail 20"
        return 1;
    }
    
    echo "✅ $NODE_NAME deployment complete!"
    echo ""
}

# Deploy to Exit Node
deploy_to_node "$EXIT_NODE" "Exit Node"

echo "============================================"
echo "✅ Exit Node Updated Successfully!"
echo "============================================"
echo ""
echo "Verification:"
echo "  ssh -i \"$SSH_KEY\" root@\$EXIT_NODE 'cd /opt/vpn-bot && docker compose ps'"
echo ""
echo "⚠️  Reminder: Entry Node ($ENTRY_NODE) only forwards traffic."
echo "    If DNAT rules changed, update them manually on the Entry Node."
echo ""
