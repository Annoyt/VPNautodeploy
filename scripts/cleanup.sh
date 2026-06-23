#!/bin/bash
set -e

INSTALL_DIR="${INSTALL_DIR:-/opt/vpn-bot}"
LOG_DIR="${LOG_DIR:-/var/log/vpn-bot}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/vpn-bot}"
KEEP_BACKUPS="${KEEP_BACKUPS:-10}"
KEEP_LOGS_DAYS="${KEEP_LOGS_DAYS:-30}"
KEEP_TEMP_DAYS="${KEEP_TEMP_DAYS:-7}"

echo "=== VPN Bot Cleanup ==="

# Clean Python cache
echo "Cleaning Python cache..."
find "$INSTALL_DIR" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find "$INSTALL_DIR" -name "*.pyc" -delete 2>/dev/null || true
find "$INSTALL_DIR" -name "*.pyo" -delete 2>/dev/null || true

# Clean temp files
echo "Cleaning temp files older than $KEEP_TEMP_DAYS days..."
find /tmp -name "vpn-bot-*" -mtime +$KEEP_TEMP_DAYS -delete 2>/dev/null || true

# Clean old backups
echo "Cleaning old backups (keeping last $KEEP_BACKUPS)..."
cd "$BACKUP_DIR" 2>/dev/null || exit 0
ls -t bot_*.db.gz 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) | xargs -r rm -f

# Clean old logs
echo "Cleaning old logs (keeping $KEEP_LOGS_DAYS days)..."
find "$LOG_DIR" -name "*.log" -mtime +$KEEP_LOGS_DAYS -delete 2>/dev/null || true

# Docker cleanup (if docker is installed)
if command -v docker &> /dev/null; then
    echo "Cleaning Docker..."
    docker system prune -f --volumes 2>/dev/null || true
fi

# Show disk usage
echo ""
echo "Disk usage after cleanup:"
df -h / | tail -1

echo ""
echo "=== Cleanup Complete ==="
