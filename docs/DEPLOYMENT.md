# VPN Bot Deployment Guide

## Prerequisites

- Ubuntu 22.04 LTS (recommended)
- Python 3.10+
- Root access
- Telegram Bot Token (from @BotFather)
- Telegram User ID (from @userinfobot)

## Quick Start

```bash
# 1. Clone repository
git clone https://github.com/your-repo/vpn-bot.git /opt/vpn-bot
cd /opt/vpn-bot

# 2. Run installation
sudo bash scripts/install.sh

# 3. Configure environment
sudo nano /etc/vpn-bot/.env

# 4. Start service
sudo systemctl start vpn-bot
sudo systemctl enable vpn-bot

# 5. Check status
sudo systemctl status vpn-bot
sudo journalctl -u vpn-bot -f
```

## Configuration

Edit `/etc/vpn-bot/.env`:

```bash
# Required
BOT_TOKEN=your_bot_token_here
SUPER_ADMIN_ID=your_telegram_id

# VPN Settings
ENTRY_NODE_IP=your_entry_node_ip
REALITY_PUBLIC_KEY=your_public_key
SNI_VALUE=www.microsoft.com

# Optional Forum Settings
FORUM_GROUP_ID=-1001234567890
```

## Management Commands

### Start/Stop/Restart
```bash
sudo systemctl start vpn-bot
sudo systemctl stop vpn-bot
sudo systemctl restart vpn-bot
```

### View Logs
```bash
# Real-time logs
sudo journalctl -u vpn-bot -f

# Last 100 lines
sudo journalctl -u vpn-bot -n 100
```

### Health Check
```bash
sudo bash /opt/vpn-bot/scripts/health-check.sh
```

### Backup Database
```bash
sudo bash /opt/vpn-bot/scripts/backup.sh
```

### Restore Database
```bash
# Restore latest backup
sudo bash /opt/vpn-bot/scripts/restore.sh

# Restore specific backup
sudo bash /opt/vpn-bot/scripts/restore.sh /var/backups/vpn-bot/bot_20240325_120000.db.gz
```

## Deployment (Updates)

```bash
cd /opt/vpn-bot
sudo bash scripts/deploy.sh
```

The deploy script will:
1. Create database backup
2. Pull latest code
3. Update dependencies
4. Run migrations
5. Restart service
6. Run health checks
7. **Auto-rollback on failure**
8. Cleanup old files on success

## Troubleshooting

### Service Won't Start
```bash
# Check logs
sudo journalctl -u vpn-bot -n 50

# Check config
sudo bash /opt/vpn-bot/scripts/health-check.sh

# Verify permissions
ls -la /var/lib/vpn-bot/
ls -la /etc/vpn-bot/.env
```

### Database Issues
```bash
# Check database
sudo sqlite3 /var/lib/vpn-bot/bot.db ".tables"

# Restore from backup
sudo bash /opt/vpn-bot/scripts/restore.sh
```

### Permission Denied
```bash
# Fix permissions
sudo chown -R vpn-bot:vpn-bot /opt/vpn-bot
sudo chown -R vpn-bot:vpn-bot /var/lib/vpn-bot
sudo chmod 600 /etc/vpn-bot/.env
```

## Backup Strategy

Backups are stored in `/var/backups/vpn-bot/`:
- Automatic backup before each deploy
- Last 10 backups kept automatically
- Compresssed with gzip

Manual backup:
```bash
sudo bash /opt/vpn-bot/scripts/backup.sh
```

## Security

- Bot runs as unprivileged user `vpn-bot`
- Configuration file has 600 permissions
- Database has 600 permissions
- No secrets in code (all in .env)

## Rollback

If deployment fails, automatic rollback occurs:
- Database restored from pre-deploy backup
- Code reverted to previous git commit
- Service restarted

Manual rollback:
```bash
sudo bash /opt/vpn-bot/scripts/restore.sh
sudo systemctl restart vpn-bot
```

## Monitoring

### Health Check Script
Checks:
- systemd service status
- Database accessibility
- Disk space
- Telegram API connectivity

### Log Rotation
Logs are managed by journald. To persist logs:
```bash
sudo mkdir -p /var/log/vpn-bot
sudo bash /opt/vpn-bot/scripts/cleanup.sh
```

## File Locations

| Path | Purpose |
|------|---------|
| `/opt/vpn-bot` | Application code |
| `/etc/vpn-bot/.env` | Configuration |
| `/var/lib/vpn-bot/bot.db` | Database |
| `/var/log/vpn-bot/` | Log files |
| `/var/backups/vpn-bot/` | Backups |
| `/etc/systemd/system/vpn-bot.service` | systemd unit |

## Support

For issues:
1. Check logs: `sudo journalctl -u vpn-bot -f`
2. Run health check: `sudo bash scripts/health-check.sh`
3. Verify configuration: `cat /etc/vpn-bot/.env`
