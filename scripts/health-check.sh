#!/bin/bash
# VPN Bot Health Check Script

if systemctl is-active --quiet vpn-bot.service; then
    echo "VPN Bot is running normally."
    exit 0
else
    echo "VPN Bot is NOT running!"
    systemctl status vpn-bot.service --no-pager
    exit 1
fi
