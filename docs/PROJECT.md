# VPN Bot — Project Documentation

> ⚠️ **ИСТОРИЧЕСКИЙ ДОКУМЕНТ (апрель 2026).** Топология с тех пор перевёрнута:
> бот живёт на **entry** (не на exit), деплой — rsync через
> `scripts/deploy_to_entry.sh` (не git-checkout), панель на exit — API-only.
> Актуальный источник правды: **AGENTS.md** (архитектура, деплой, хроника
> граблей) + память агента. Этот файл оставлен как история ранней эпохи.

**Last updated:** 2026-04-17 (see banner)

---

## 1. Environment

| Component | Host | Role |
|---|---|---|
| Exit Node | `<old-blocked-host>` | Bot + 3x-ui (XRay) + SQLite DB |
| Entry Node | `<entry-host>` | iptables DNAT forwarder (443 → Exit) |

**Services on Exit Node:**
- `vpn-bot.service` — Telegram bot (`ExecStart=/opt/vpn-bot/venv/bin/python -m bot.main`)
- `3x-ui` Docker container — XRay VLESS Reality (volume: `vpn-bot_3xui-data`)

**Key paths:**
- Bot code: `/opt/vpn-bot/`
- Bot DB: `/etc/cascade-vpn/bot.db` (env `DB_PATH`)
- X-UI DB: `/var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db` (env `XUI_DB_PATH`)
- systemd unit: `/etc/systemd/system/vpn-bot.service`

---

## 2. Current Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────────────────┐
│  Telegram   │────▶│  Entry Node  │────▶│        Exit Node            │
│   User      │     │ <entry-host>│     │    <old-blocked-host>           │
└─────────────┘     │  iptables    │     │  ┌─────────┐  ┌─────────┐  │
                    │   DNAT 443   │     │  │ vpn-bot │  │ 3x-ui   │  │
                    └──────────────┘     │  │(aiogram)│  │(XRay)   │  │
                                         │  └────┬────┘  └────┬────┘  │
                                         │       │            │       │
                                         │  ┌────▼────────────▼────┐  │
                                         │  │   Shared SQLite      │  │
                                         │  │  bot.db + x-ui.db    │  │
                                         │  └──────────────────────┘  │
                                         └─────────────────────────────┘
```

### Stack
- Python 3.12, aiogram 3.x, SQLite (WAL mode)
- 3x-ui (ghcr.io/mhsanaei/3x-ui:latest) in Docker
- XRay VLESS Reality on port 443
- systemd service for bot

### Code Structure

```
bot/
├── main.py                      # Entry point (sync), init services
├── config/
│   ├── settings.py              # Env vars + defaults
│   └── constants.py             # UserState, BYTES_PER_GB, MESSAGES
├── core/
│   ├── database.py              # Facade → UserRepository, TicketRepository, NodeRepository, MessageMapRepository
│   ├── repositories/            # Repository pattern (user, ticket, node, message_map, async adapters)
│   ├── state_machine.py         # UserState transitions
│   ├── bot.py                   # Legacy sync Bot wrapper
│   └── cluster/                 # Failover: election, routing, sync API, health checks
├── handlers/
│   ├── commands.py              # /start, /help, /stats
│   ├── callbacks/
│   │   ├── dispatcher.py        # Chain of Responsibility for callbacks
│   │   ├── base.py              # ValidationService, ForumMessageService
│   │   ├── user.py              # Demo, key, stats, support, language
│   │   └── admin.py             # Approve, reject, revoke, profile, reset, close ticket
│   ├── admin/                   # Modular admin handlers (base, users, broadcast, stats)
│   ├── messages.py              # PM support threading
│   └── forum.py                 # Forum topic management
├── services/
│   ├── vpn.py                   # VLESS key generation
│   ├── xui_service.py           # Unified: HTTP API + DB fallback
│   ├── xui_db.py                # Direct x-ui.db operations
│   ├── xui_reload.py            # XRay reload via docker exec
│   ├── notifications.py         # Scheduled + instant notifications
│   ├── node_cluster.py          # Multi-node management
│   └── failover_notifications.py
├── models/
│   ├── user.py                  # User dataclass
│   ├── vpn_node.py              # VPNNode (Exit/Entry)
│   └── cluster/                 # Failover models
├── utils/
│   ├── callback_router.py       # Declarative callback registration
│   ├── validators.py            # Input validation
│   └── exceptions.py            # Custom exception hierarchy
└── webapp/                      # Telegram Mini App dashboard
```

---

## 3. Database Schema

### Bot Database (`bot.db`)

| Table | Purpose | Key Fields |
|---|---|---|
| `users` | User profiles, FSM state, VPN identity | `chat_id` PK, `uuid`, `email`, `status`, `quota_gb`, `limit_ip` |
| `tickets` | Support ticket tracking | `topic_id` UNIQUE, `chat_id`, `status`, `closed_at` |
| `ticket_messages` | Forum thread history | `topic_id`, `sender_type`, `text`, `has_media` |
| `message_map` | PM mode reply threading | `admin_msg_id`, `user_chat_id`, `user_msg_id` |
| `nodes` | Exit/Entry node registry | `name`, `type`, `host`, `public_key`, `sni`, `weight`, `status` |
| `node_assignments` | Per-user node routing | `chat_id`, `exit_node_id`, `entry_node_id` |
| `node_failover_log` | Automatic failover events | `chat_id`, `from_node_id`, `to_node_id`, `reason` |
| `traffic_log` | Periodic traffic snapshots | `email`, `upload_bytes`, `download_bytes`, `recorded_at` |
| `static_profiles` | Shared static VPN keys | `name`, `vless_url`, `max_users`, `current_users` |
| `subscriptions` | Subscription plans | `chat_id`, `plan_type`, `start_date`, `end_date`, `is_active` |
| `notification_log` | Notification deduplication | `chat_id`, `notification_type`, `sent_at` |
| `admin_actions` | Admin audit trail | `admin_id`, `action`, `target_id`, `details` |
| `xui_synced` | Legacy: synced client tracking | `email` PK, `synced_at` |
| `xui_api_config` | Runtime X-UI API settings (singleton) | `base_url`, `username`, `password`, `use_api`, `inbound_id` |

### X-UI Database (`x-ui.db`)

Managed by 3x-ui container. Bot reads/writes via `XUIDatabase`:

| Table | Purpose | Bot Access |
|---|---|---|
| `inbounds` | XRay inbound configs (JSON) | Read/write `settings` JSON to add/remove VLESS clients |
| `client_traffics` | Traffic counters per client | Read stats, ensure records exist for new clients |

---

## 4. User Flows

### Principles
1. ≤3 clicks to result
2. Clear feedback after every action
3. Key can be re-obtained anytime
4. 24/7 Support button always available

### Flow 1: New User
```
/start
  → "🚀 Запросить демо"
    → Status: PENDING_DEMO, admin notified
      → Admin approves
        → "Выберите платформу" (Android, iOS, Windows, macOS)
          → "📋 Получить ключ"
            → VLESS link + QR + instructions
              → Status: ACTIVE
```

State chain: `NEW → PENDING_DEMO → PLATFORM_SELECT → ACTIVE`

### Flow 2: Existing User
| Action | Path |
|---|---|
| Check key | Main menu → "🔑 Мой ключ" → VLESS link + QR |
| Statistics | Main menu → "📊 Статистика" → used/total traffic |
| Support | Main menu → "💬 Поддержка" → creates ticket |
| Full version | Main menu → "💎 Полная версия" → payment info |

### Flow 3: Problems
| Problem | Bot Response |
|---|---|
| Key doesn't work | Troubleshooting wizard → or ticket |
| Traffic exhausted | "⚠️ Demo expired (5GB). [Buy full version]" |
| Forgot platform | Re-select from menu anytime |

### Keyboards
```
ACTIVE menu:          Platform select:
┌────────┬────────┐   ┌────────┬────────┐
│📊 Stats│🔑 Key  │   │Android │  iOS   │
├────────┼────────┤   ├────────┼────────┤
│💬 Supp │💎 Full │   │Windows │ macOS  │
└────────┴────────┘   ├────────┴────────┤
                      │     Другая      │
                      └─────────────────┘
```

---

## 5. Admin Flows

### Mode 1: With Forum Group (Recommended)

Forum structure:
```
📊 VPN Admin Panel
├── 📈 Statistics (topic 18)
├── 📝 Requests (topic 15)
├── 💳 Payments (topic 16)
├── 🆘 Support (topic 17)
└── ✅ Solved (topic 37)
```

**Request handling:**
- New demo request appears in Requests with inline buttons: ✅ Одобрить | ❌ Отклонить | 💬 Написать | 👁 Профиль
- Approve → user gets platform selection, client added to X-UI
- Reject → user gets reason, status reset to NEW

**Support ticket lifecycle:**
- User clicks "💬 Поддержка" → bot creates topic in Support
- Admin replies in topic → bot forwards to user PM
- User replies in PM → bot posts to topic with "👤 Пользователь:" prefix
- Close ticket → history moved to Solved, topic closed

### Mode 2: PM Only (Minimal)
- `FORUM_ENABLED = False`
- Admin receives all requests and support in PM
- Commands: `/approve ID`, `/reject ID [reason]`, `/user ID`, `/stats`, `/broadcast`

### Admin Commands
```
/stats      — Full statistics
/pending    — Pending users list
/user ID    — User profile + traffic
/ban ID     — Block user
/unban ID   — Unblock user
/broadcast  — Mass message
/backup     — DB backup
/reset ID   — Reset user to NEW (removes from X-UI)
/grant ID   — +100GB traffic
```

### Role Levels
| Role | Permissions |
|---|---|
| SUPER_ADMIN | All commands, settings, admin management |
| ADMIN | Approve/reject, support, stats, ban/unban |
| SUPPORT | Support replies, view profiles, no approvals |

---

## 6. Deployment

### Quick Deploy (Exit Node)
```bash
# Backup
mkdir -p /backup/deploy-$(date +%Y%m%d-%H%M%S)
cp -r /opt/vpn-bot /backup/deploy-.../
cp /etc/cascade-vpn/bot.db /backup/deploy-.../
cp /var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db /backup/deploy-.../

# Deploy code
rsync -avz --exclude='.git' --exclude='tests' --exclude='__pycache__' \
  ./bot/ root@<old-blocked-host>:/opt/vpn-bot/

# Restart
ssh root@<old-blocked-host> "systemctl daemon-reload && systemctl restart vpn-bot"
```

### Critical Environment Variables
```bash
BOT_TOKEN=...
SUPER_ADMIN_ID=1652899
DB_PATH=/etc/cascade-vpn/bot.db
XUI_DB_PATH=/var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db
XUI_API_URL=http://127.0.0.1:2026
XUI_BASE_PATH=/this_is_fine
XUI_API_PATH=/this_is_fine/panel/api/inbounds
ENTRY_NODE_IP=<entry-host>
REALITY_PUBLIC_KEY=...
SNI_VALUE=www.microsoft.com
SID_VALUE=01
```

### Health Check
```bash
journalctl -u vpn-bot -f
systemctl status vpn-bot
python3 -m py_compile bot/main.py
```

---

## 7. Failover Architecture

> Status: Implemented but single-node only currently. Multi-node code exists in `bot/core/cluster/`.

When multiple Exit Nodes are available:
1. Entry Node health-checks all Exit Nodes every 5s
2. If primary Exit fails → Performance Monitor selects best alternative
3. Smart Routing considers CPU load, throttle status, cooldown
4. Bot notifies admin of failover events
5. Users experience seamless reconnection (same key, different IP)

### Components
- `node_tracker.py` — Rolling window CPU tracking, throttle detection
- `smart_routing.py` — Routing decisions (stay / failover / delay)
- `failover_api.py` — FastAPI endpoints for cluster sync
- `entry_node_healthcheck.py` — Runs on Entry Node, executes failovers

### Configuration
```python
FAILOVER_COOLDOWN_SECONDS = 300
MAX_FAILOVER_COUNT = 3
THROTTLED_FAILOVER_DELAY = 30
FAILOVER_NOTIFICATION_POLICY = "silent"
```

---

## 8. Task Status

### Completed ✅
- [x] Modular architecture (repositories, services, handlers)
- [x] X-UI DB path fix (Docker volume binding)
- [x] DNAT Entry Node fix (443 → Exit Node)
- [x] VLESS URL fix (sid=01, removed empty spx)
- [x] Positional row[N] indices → named columns
- [x] Legacy async stack removal (database_async, bot_aiogram, main_async, async handlers)
- [x] DB alias tables removed (traffic_history, notifications, admin_logs)
- [x] Deploy scripts updated for single Exit Node
- [x] 1032 tests passing

### Open ❌
- [ ] CI/CD pipeline
- [ ] Test bot (@test_nekovpn_bot)
- [ ] Multi-node failover in production (code ready, only 1 Exit Node deployed)
- [ ] Traffic collection automation (`scripts/collect_traffic_api.py` exists but not scheduled)
