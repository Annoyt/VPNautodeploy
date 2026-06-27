# NekoVPN Agent Deployment Guide

**This document is for AI agents** deploying NekoVPN from scratch.

## Overview

NekoVPN is a multi-node VPN service with:
- **Entry Node**: iptables DNAT in Russia (DPI circumvention)
- **Exit Node**: 3X-UI Docker + VPN Bot (Telegram management)
- **Protocols**: VLESS+Reality, Hysteria2, VMess+WS+TLS, ShadowTLS

## Prerequisites

You need:
- Git repository access
- VPS provider(s) with API access OR existing servers with SSH
- Telegram Bot token from [@BotFather](https://t.me/botfather)
- (Optional) OpenRouter API key for AI-powered setup assistance

## VPS Providers with API

### Confirmed Working
- **AdminVPS**: API available, Russia-friendly
- TODO: Add more providers as verified

### Does NOT have API
- **BitCloud**: No API access confirmed

### Needs Verification
- DigitalOcean (Russia access?)
- Hetzner (Russia access?)
- Linode/Akamai (Russia access?)

> **Note to agents**: If a provider doesn't work in Russia, it can only be used for Exit Node (outside RF).

## Deployment Methods

### Method 1: API-based Auto-provisioning (Recommended for new installs)

Agent will:
1. Create Entry Node VPS via API
2. Create Exit Node VPS via API
3. Generate SSH keys and inject them
4. Deploy code and start services

### Method 2: Existing Servers (Manual IPs)

Agent will:
1. Ask for Entry Node IP + SSH credentials
2. Ask for Exit Node IP + SSH credentials
3. Deploy code to existing servers

## Architecture

```
┌─────────────────┐         ┌─────────────────┐
│   User Device   │◄────────┤  Entry Node     │
│  (Hiddify app)  │  VLESS   │  (Russia)       │
└─────────────────┘  Reality └─────────────────┘
                                            │ DNAT
                                            ▼
                                    ┌─────────────────┐
                                    │  Exit Node      │
                                    │  (3X-UI + Bot)  │
                                    │  (Any country)  │
                                    └─────────────────┘
```

## Setup Steps (Agent Checklist)

1. **Generate SSH keys** if not exists
2. **Create VPS instances** via API (or use provided IPs)
3. **Install Docker** on both nodes
4. **Deploy Entry Node** (iptables + xray)
5. **Deploy Exit Node** (3X-UI + VPN Bot)
6. **Configure Telegram Bot** with service URLs
7. **Verify connectivity** between nodes
8. **Run health checks**

## File Structure After Deployment

```
/opt/vpn-bot/
├── bot/                    # VPN bot code
├── 3x-ui/                  # 3X-UI configuration
├── docker-compose.yml      # Services stack
├── .env                    # Generated credentials
└── entries/                # Entry node scripts
```

## Environment Variables Required

```
# Telegram
BOT_TOKEN=...              # From @BotFather
SUPER_ADMIN_ID=...         # Your Telegram user ID

# Nodes
ENTRY_NODE_IP=...          # Entry node IP (or auto-discovered)
REALITY_PUBLIC_KEY=...     # Generated after entry deployment

# 3X-UI
XUI_USERNAME=...
XUI_PASSWORD=...

# (Optional) Email
SMTP_HOST=...
SMTP_PORT=587
SMTP_USER=...
SMTP_PASSWORD=...
```

## Getting Keys

- **Telegram Bot**: https://t.me/botfather → /newbot
- **Telegram User ID**: Send /start to @userinfobot
- **AdminVPS API**: https://adminvps.net/api (TODO: verify endpoint)
- **OpenRouter**: https://openrouter.ai/keys (optional)

## Troubleshooting

- Entry node unreachable: Check iptables rules and xray service
- Exit node can't connect to entry: Verify SSH tunnel/firewall
- Bot not responding: Check `docker logs vpn-bot`
- 3X-UI API failing: Verify credentials in .env

## Support

- GitHub Issues: [link]
- Telegram: @nekovpn_support

---

**Last updated:** 2026-06-24
**Agent version:** 1.0
