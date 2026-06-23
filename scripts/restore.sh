#!/bin/bash
set -e

DB_PATH="${DB_PATH:-/var/lib/vpn-bot/bot.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/vpn-bot}"
USER="vpn-bot"

usage() {
    echo "Usage: $0 [backup_file]"
    echo "Restore database from backup"
    echo ""
    echo "If no backup_file specified, uses the most recent backup"
    exit 1
}

echo "=== VPN Bot Restore ==="

# Check if running as root
if [[ $EUID -ne 0 ]]; then
   echo "Error: This script must be run as root"
   exit 1
fi

# Determine backup file
if [[ -n "$1" ]]; then
    BACKUP_FILE="$1"
else
    # Find most recent backup
    BACKUP_FILE=$(ls -t "$BACKUP_DIR"/bot_*.db.gz 2>/dev/null | head -1)
    if [[ -z "$BACKUP_FILE" ]]; then
        echo "Error: No backup files found in $BACKUP_DIR"
        exit 1
    fi
fi

# Check if backup exists
if [[ ! -f "$BACKUP_FILE" ]]; then
    echo "Error: Backup file not found: $BACKUP_FILE"
    exit 1
fi

echo "Using backup: $BACKUP_FILE"

# Stop service
echo "Stopping service..."
systemctl stop vpn-bot || true

# Create pre-restore backup
echo "Creating safety backup..."
if [[ -f "$DB_PATH" ]]; then
    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    SAFETY_BACKUP="$BACKUP_DIR/pre_restore_${TIMESTAMP}.db"
    sudo -u "$USER" sqlite3 "$DB_PATH" ".backup '$SAFETY_BACKUP'"
    gzip "$SAFETY_BACKUP"
    echo "Safety backup: ${SAFETY_BACKUP}.gz"
fi

# Restore database
echo "Restoring database..."

# If compressed, extract
if [[ "$BACKUP_FILE" == *.gz ]]; then
    TMP_FILE=$(mktemp)
    gunzip -c "$BACKUP_FILE" > "$TMP_FILE"
    sudo -u "$USER" sqlite3 "$DB_PATH" ".restore '$TMP_FILE'"
    rm -f "$TMP_FILE"
else
    sudo -u "$USER" sqlite3 "$DB_PATH" ".restore '$BACKUP_FILE'"
fi

chown "$USER:$USER" "$DB_PATH"
chmod 600 "$DB_PATH"

# Start service
echo "Starting service..."
systemctl start vpn-bot

echo ""
echo "=== Restore Complete ==="
echo "Check status: systemctl status vpn-bot"
echo "Check logs: journalctl -u vpn-bot -f"
