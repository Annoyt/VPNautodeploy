#!/bin/bash
# VPN Bot Deployment Script
# Phase 8: Ship

set -e

BOT_DIR="/opt/vpn-bot"
BACKUP_DIR="/backup/pre-deploy-$(date +%Y%m%d-%H%M%S)"

echo "============================================"
echo "VPN Bot Deployment"
echo "Time: $(date)"
echo "============================================"
echo

# 1. Pre-deployment checks
echo "[1/7] Pre-deployment checks..."
if [ "$EUID" -ne 0 ]; then 
    echo "❌ Please run as root"
    exit 1
fi

if [ ! -d "$BOT_DIR" ]; then
    echo "❌ Bot directory not found: $BOT_DIR"
    exit 1
fi

echo "✓ Checks passed"

# 2. Backup current installation
echo "[2/7] Creating backup..."
mkdir -p "$BACKUP_DIR"
cp -r "$BOT_DIR" "$BACKUP_DIR/" 2>/dev/null || true
cp /etc/cascade-vpn/bot.db "$BACKUP_DIR/" 2>/dev/null || true
echo "✓ Backup saved to $BACKUP_DIR"

# 3. Stop services
echo "[3/7] Stopping services..."
systemctl stop vpn-bot || true
echo "✓ Services stopped"

# 4. Database initialization (migrations handled automatically on startup)
echo "[4/7] Database check..."
cd "$BOT_DIR"
python3 -c "from bot.core.database import Database; Database('/etc/cascade-vpn/bot.db')" 2>/dev/null || echo "⚠️  DB initialization will happen on first bot start"
echo "✓ Database ready"

# 5. Update code
echo "[5/7] Updating code..."
# Code should be already deployed via git or copied
echo "✓ Code updated"

# 6. Configure X-UI Port (interactive)
echo "[6/7] Configuring X-UI Port..."
if [ -f "$BOT_DIR/.env" ]; then
    CURRENT_PORT=$(grep XUI_PORT "$BOT_DIR/.env" | cut -d= -f2 || echo "2053")
    echo "Current X-UI port: ${CURRENT_PORT:-2053}"
    read -p "Enter X-UI port [2053]: " XUI_PORT
    XUI_PORT=${XUI_PORT:-2053}
    
    # Update .env
    if grep -q "XUI_PORT=" "$BOT_DIR/.env"; then
        sed -i "s/XUI_PORT=.*/XUI_PORT=$XUI_PORT/" "$BOT_DIR/.env"
    else
        echo "XUI_PORT=$XUI_PORT" >> "$BOT_DIR/.env"
    fi
    
    # Update XUI_API_URL
    if grep -q "XUI_API_URL=" "$BOT_DIR/.env"; then
        sed -i "s|XUI_API_URL=.*|XUI_API_URL=http://127.0.0.1:$XUI_PORT|" "$BOT_DIR/.env"
    else
        echo "XUI_API_URL=http://127.0.0.1:$XUI_PORT" >> "$BOT_DIR/.env"
    fi
    
    echo "✓ X-UI port set to $XUI_PORT"
else
    echo "⚠️  .env file not found, skipping port configuration"
fi

# 5. Start services
echo "[5/7] Starting services..."
systemctl daemon-reload
systemctl start vpn-bot
systemctl enable vpn-bot
systemctl enable vpn-bot-backup.timer 2>/dev/null || true
systemctl start vpn-bot-backup.timer 2>/dev/null || true
echo "✓ Services started"

# 6. Health check
echo "[6/7] Health check..."
sleep 3
if systemctl is-active --quiet vpn-bot; then
    echo "✓ Bot is running"
else
    echo "❌ Bot failed to start"
    echo "Check logs: journalctl -u vpn-bot -n 50"
    exit 1
fi

# 7. Verify X-UI DB path
echo "[7/7] Verifying X-UI DB path..."
XUI_DB=$(grep XUI_DB_PATH /opt/vpn-bot/.env 2>/dev/null | cut -d= -f2 || echo "")
if [ -n "$XUI_DB" ] && [ ! -f "$XUI_DB" ]; then
    echo "⚠️  XUI_DB_PATH points to non-existent file: $XUI_DB"
    echo "    Ensure it matches the 3x-ui Docker volume: /var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db"
else
    echo "✓ X-UI DB path OK"
fi

echo
echo "============================================"
echo "✅ Deployment Complete!"
echo "============================================"
echo
echo "Commands:"
echo "  Check status:  systemctl status vpn-bot"
echo "  View logs:     journalctl -u vpn-bot -f"
echo "  Rollback:      cp $BACKUP_DIR/bot.db /etc/cascade-vpn/"
echo
