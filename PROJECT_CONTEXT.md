# VPN Bot Refactor - Complete Project Context

## Overview

**Project Name**: NekoVPN Bot (vpn-bot-refactor)  
**Type**: Telegram Bot for VPN Service Management  
**Architecture**: Modular, multi-phase refactoring from monolithic script  
**Stack**: Python 3.12, SQLite, Docker, 3X-UI (XRay), systemd  
**Status**: Phases 0-3 Complete, Production Ready  

---

## Architecture

### High-Level Flow
```
User → Telegram Bot → SQLite DB (bot.db)
                           ↓
                    X-UI Sync Service → 3X-UI HTTP API → XRay (Docker)
                           ↓
                    Entry Node (Kaskad/DNAT) → Exit Node (3X-UI)
```

### Deployment Architecture
- **Entry Node**: <entry-host> - Kaskad relay, iptables DNAT/MASQUERADE
- **Exit Node**: <old-blocked-host> - 3X-UI Docker, Bot, Sync service
- **Protocol**: VLESS + XTLS-Reality (DPI-resistant)

---

## Project Structure

```
vpn-bot-refactor/
├── bot/                          # Main application code
│   ├── config/                   # Configuration layer
│   │   ├── settings.py          # ENV vars, Settings class
│   │   └── constants.py         # UserState, Platform, MESSAGES
│   ├── core/                     # Core business logic
│   │   ├── database.py          # SQLite operations (1067 lines, legacy)
│   │   ├── state_machine.py     # User state transitions
│   │   ├── bot.py               # Bot coordinator
│   │   ├── telegram_client.py   # Telegram HTTP API
│   │   ├── polling.py           # Long polling loop
│   │   └── repositories/        # Repository pattern (new)
│   │       ├── user.py
│   │       ├── ticket.py
│   │       └── node.py
│   ├── handlers/                 # Input handling
│   │   ├── base.py              # Abstract BaseHandler
│   │   ├── commands.py          # /start, /help, /stats, /mykey
│   │   ├── callbacks.py         # 14 callback handlers
│   │   ├── messages.py          # Text message handling
│   │   ├── forum.py             # Forum topic support
│   │   └── admin.py             # 13 admin commands
│   ├── models/                   # Data models
│   │   ├── user.py              # User dataclass
│   │   ├── node.py              # Node (entry/exit) model
│   │   └── static_profile.py    # Shared VPN keys
│   ├── services/                 # Business services
│   │   ├── vpn.py               # VLESS key generation
│   │   ├── xui_sync.py          # X-UI multi-node sync
│   │   ├── notifications.py     # User/admin notifications
│   │   ├── admin_notifications.py
│   │   ├── xui_service.py       # Unified HTTP API + DB fallback
│   │   ├── xui_api/client.py    # HTTP API client
│   │   └── node_manager.py      # Multi-node management
│   └── main.py                   # Entry point
├── tests/                        # Test suite
│   ├── unit/                    # 100+ unit tests
│   └── integration/             # Integration tests
├── docs/                         # Documentation
│   ├── ARCHITECTURE.md
│   ├── USER_FLOWS.md
│   ├── ADMIN_FLOWS.md
│   └── REFACTOR_PLAN.md
├── docker-compose.yml            # Docker orchestration
├── Dockerfile
└── requirements.txt
```

---

## Key Components

### 1. Config (bot/config/)

**Settings** (`settings.py`):
- `BOT_TOKEN`, `SUPER_ADMIN_ID`
- `MODE`: "GROUP" (forum) or "PM_ONLY"
- `FORUM_GROUP_ID`, `TOPIC_*` IDs
- `XUI_API_URL`, `XUI_DB_PATH`
- `ENTRY_NODE_IP`, `REALITY_PUBLIC_KEY`, `SNI_VALUE`, `SID_VALUE`
- `DEMO_TRAFFIC_GB=5`, `DEMO_DAYS=7`

**Constants** (`constants.py`):
```python
UserState: NEW → PENDING_DEMO → PLATFORM_SELECT → ACTIVE/DEMO
Platform: ANDROID, IOS, WINDOWS, MACOS, OTHER
MESSAGES: ru/en localized strings
STATE_TRANSITIONS: Valid state transitions
```

### 2. Database (bot/core/database.py)

**Tables**:
- `users`: chat_id, username, uuid, email, status, lang, platform, support_topic_id, subscription_expiry, limit_ip, quota_gb
- `admin_logs`: admin actions tracking
- `xui_synced`: Sync status tracking
- `message_map`: PM mode reply threading
- `ticket_messages`: Support ticket history
- `notifications`: Notification tracking (expiry_24h, etc.)
- `static_profiles`: Shared VPN keys
- `subscriptions`: Plan tracking
- `xui_api_config`: HTTP API settings

**Key Methods**:
- `save_user()`, `get_user()`, `update_status()`
- `get_pending_users()`, `get_stats()`
- `mark_xui_synced()`, `is_synced()`
- `mark_notified()`, `was_notified()`
- `log_message_map()`, `get_mapped_user_message()`

### 3. State Machine (bot/core/state_machine.py)

**States**:
```
NEW → PENDING_DEMO → PLATFORM_SELECT → ACTIVE
                                      ↓
                                    DEMO → PAID
                                      ↓
                                  SUPPORT_TOPIC
                                      ↓
                                    BANNED → NEW (reset)
```

**Methods**:
- `can_transition(from_state, to_state) -> bool`
- `transition(chat_id, new_state) -> bool`
- `get_state(chat_id) -> UserState`
- `set_state(chat_id, state) -> bool`

### 4. Bot Core (bot/core/bot.py)

**Class Bot**:
- `register_handler(handler)` - Add handlers
- `_handle_update(update)` - Route to appropriate handler
- `start()`, `stop()` - Lifecycle
- `send_message()`, `answer_callback_query()`
- `create_forum_topic()`, `close_forum_topic()`

### 5. VPN Service (bot/services/vpn.py)

**Methods**:
- `generate_uuid() -> str` - UUID v4
- `generate_email(chat_id, username) -> str` - user_{username}_{chat_id}@nekovo.ru
- `generate_vless_link(uuid, email) -> str` - VLESS URL with REALITY/Flow
- `get_instructions(platform, lang) -> str` - Setup guides
- `create_client_config(chat_id, **kwargs) -> dict` - Full config

**VLESS Link Format**:
```
vless://{uuid}@{entry_ip}:443?security=reality&flow=xtls-rprx-vision&...
```

### 6. X-UI Sync (bot/services/xui_sync.py)

**Legacy Methods** (single-node):
- `add_client(client, inbound_id)` - Add to X-UI
- `remove_client(email, inbound_id)` - Remove from X-UI
- `sync_user(chat_id, config)` - Sync single user

**Multi-Node Methods**:
- `sync_user_to_node(chat_id, node_id, config)`
- `remove_user_from_node(chat_id, node_id)`
- `failover_user(chat_id, from_node, to_node)`
- `get_user_traffic_all_nodes(chat_id)`

**X-UI Service** (`xui_service.py`):
- HTTP API as primary method
- DB fallback if shared volume available
- Unified interface for containerized deployment

### 7. Notifications (bot/services/notifications.py)

**User Notifications**:
- `notify_welcome()` - Welcome + demo button
- `notify_pending()` - Request pending
- `notify_approved()` - Approval + platform selection
- `notify_platform_selected()` - Instructions
- `notify_key_generated()` - VLESS key
- `notify_rejected()` - Rejection with reason
- `notify_main_menu()` - Main menu keyboard

**Admin Notifications**:
- `notify_new_request()` - Demo request to admin
- `notify_new_support_ticket()` - Support message
- `notify_payment_issue()` - Payment problems
- `notify_stats()` - Traffic statistics

### 8. Handlers

**BaseHandler** (bot/handlers/base.py):
- Abstract: `can_handle()`, `handle()`
- Helpers: `_get_chat_id()`, `_get_user_id()`, `_get_username()`
- Admin: `_is_admin()`, `_is_chat_admin()`
- User: `_get_or_create_user()`

**CommandHandler** (bot/handlers/commands.py):
- `/start` - Welcome, auto-create user
- `/help` - Help text
- `/stats` - User statistics
- `/mykey` - Show VLESS key

**CallbackHandler** (bot/handlers/callbacks.py):
- `request_demo` → State: PENDING_DEMO
- `approve:{chat_id}` → Admin approval
- `reject:{chat_id}` → Admin rejection
- `platform:{type}:{uuid}` → Platform selection
- `get_key:{chat_id}` → Generate VLESS key
- `support:{chat_id}` → Support request
- `stats:{chat_id}` → Statistics
- `set_lang:{lang}` → Change language

**MessageHandler** (bot/handlers/messages.py):
- Handle user messages
- Forward admin replies (PM mode)
- Message mapping for threading

**ForumHandler** (bot/handlers/forum.py):
- Create support topics
- Route forum replies to users
- Close topics on resolution
- Conditional: `FORUM_ENABLED=True`

**AdminHandler** (bot/handlers/admin.py):
- `/pending` - Show pending
- `/approve {id}` - Approve user
- `/reject {id} [reason]` - Reject user
- `/user {id}` - Show user info
- `/ban {id}` - Ban user
- `/unban {id}` - Unban user
- `/broadcast {msg}` - Broadcast
- `/users` - Active users
- `/users_all` - All users
- `/backup` - Backup DB
- `/stats` - Overall stats

---

## Refactoring Phases

### Phase 0: Deployment Blockers ✅
- **DOCK-01**: XUI DB access from container
  - Solution: Unified XUIService with HTTP API priority
- **MIG-01**: Notification timestamp format bug
  - Solution: Fixed `was_notified()` SQLite timestamp format

### Phase 1: Core Modules ✅
1. **Config** - Settings, Constants, UserState, Platform
2. **Database** - User dataclass, CRUD operations, transactions
3. **State Machine** - Transition validation, state management
4. **Bot Core** - Handler registry, update routing

### Phase 2: Services ✅
1. **VPN Service** - UUID, email, VLESS link generation
2. **X-UI Sync** - add/remove client, sync_pending_users
3. **Notifications** - User and admin notifications

### Phase 3: Handlers ✅
1. **Base Handler** - Abstract interface
2. **Command Handler** - /start, /help, /stats, /mykey
3. **Callback Handler** - 14 callback handlers
4. **Message Handler** - Support forwarding
5. **Forum Handler** - Topic management
6. **Admin Handler** - 13 admin commands

---

## Test Coverage

```
Total: 247 passed, 5 skipped, 1 failed (docker-compose not installed)

Unit Tests:
- test_vpn.py: 12 passed (VPNService)
- test_xui_sync.py: 12 passed (XUISyncService)
- test_xui_db.py: 15 passed (XUIDatabase)
- test_notifications.py: 24 passed (NotificationService)
- test_database.py: 26 passed (Database)
- test_state_machine.py: 12 passed (StateMachine)
- test_handlers.py: 18 passed (Handler integration)
```

---

## Docker Configuration

**Services** (docker-compose.yml):
1. **3x-ui**: ghcr.io/mhsanaei/3x-ui:latest
   - Ports: 443, 2026, 8443
   - Volume: 3xui-data
   
2. **vpn-bot**: Custom build
   - Env: BOT_TOKEN, ADMIN_CHAT_ID, XUI_API_URL
   - Volumes: vpn-bot-data, vpn-bot-logs, 3xui-data (ro)
   - Port: 8080 (localhost only)
   
3. **traffic-collector**: Custom build
   - Script: collect_traffic_api.py
   - HTTP API mode for cross-container access

---

## Security Considerations

1. **XSS Protection**: `escapeHtml()` in webapp/app.js
2. **Client-side Admin**: Removed from JS, server-side validation
3. **Directory Listing**: Disabled (`show_index=False`)
4. **Error Exposure**: Generic messages
5. **X-UI Access**: HTTP API primary, DB fallback optional
6. **Token Security**: BOT_TOKEN from env only
7. **Panel Obfuscation**: Secret path + nginx reverse proxy

---

## Deployment

### Exit Node Setup:
```bash
# 1. Clone repo
git clone ... /opt/vpn-bot
cd /opt/vpn-bot

# 2. Create .env
BOT_TOKEN=...
ADMIN_CHAT_ID=...
XUI_API_URL=http://3x-ui:2053

# 3. Start services
docker-compose up -d

# 4. Setup 3X-UI
# Access https://exit-node:2053/secret-path
# Create VLESS inbound with REALITY

# 5. Configure bot
# Copy Reality keys to .env
```

### Critical Rules:
1. **Never restart Docker** for X-UI changes → use `pkill -HUP xray-linux-amd64`
2. **Callback format**: `approve:{chat_id}` (no username)
3. **SSH keys**: ~/.ssh/entry_node_key, ~/.ssh/exit_node_key

---

## User Flows

### Demo Request Flow:
1. User: Click "🎁 Request Demo"
2. Bot: State → PENDING_DEMO, notify admins
3. Admin: Click "✅ Approve"
4. Bot: State → PLATFORM_SELECT, ask platform
5. User: Select platform (Android/iOS/etc)
6. Bot: Show instructions, "Get Key" button
7. User: Click "Get Key"
8. Bot: Generate UUID, email, VLESS key, sync to X-UI
9. Bot: Send key, State → DEMO

### Support Flow (Forum Mode):
1. User: Click "💬 Support"
2. Bot: Create forum topic "Support: {username}"
3. User: Send message
4. Bot: Forward to forum topic
5. Admin: Reply in forum
6. Bot: Forward reply to user PM
7. Admin: Click "Close Ticket"
8. Bot: Close topic, compile transcript

---

## Multi-Node Support

**Architecture**:
```
User → Entry Node (RU) → Exit Node 1 (EU)
                              ↓ Failover
                         Exit Node 2 (EU)
```

**Components**:
- `NodeManager` - Node health, selection
- `NodeRepository` - Node DB operations
- Multi-node VPN methods in VPNService
- Failover in XUISyncService

---

## Environment Variables

```bash
# Required
BOT_TOKEN=your_telegram_bot_token
SUPER_ADMIN_ID=your_telegram_chat_id
ENTRY_NODE_IP=<entry-host>
REALITY_PUBLIC_KEY=...
SID_VALUE=...

# Optional
MODE=GROUP  # or PM_ONLY
FORUM_GROUP_ID=-100...
XUI_API_URL=http://3x-ui:2053
XUI_DB_PATH=/opt/3x-ui/db/x-ui.db
DB_PATH=/etc/cascade-vpn/bot.db
WEBAPP_URL=https://...
DEMO_TRAFFIC_GB=5
DEMO_DAYS=7
```

---

## Known Issues & TODO

1. **callbacks.py** - Long if/elif chain (router pattern candidate)
2. **database.py** - Large file (refactoring to repositories in progress)
3. **docker-compose test** - Requires docker-compose installed
4. **AdminNotificationService** - Import issue (use functions directly)

---

## Recent Changes (Current Session)

1. **Unified XUIService** - HTTP API priority with DB fallback
2. **Timestamp Bug Fix** - `was_notified()` SQLite format fix
3. **MemPalace Integration** - Project context documentation

---

## Contact & Resources

- **YouTube**: @antenkaru
- **3X-UI**: https://github.com/mhsanaei/3x-ui
- **AmneziaWG**: https://github.com/amnezia-vpn/amneziawg-go
- **MTProto**: https://github.com/9seconds/mtg

---

## Stats

- **Lines of Code**: ~5000 (Python)
- **Test Coverage**: 247 tests
- **Files**: 50+ Python modules
- **Docker Services**: 3
- **Phases Complete**: 4/4

---

*Generated for MemPalace indexing - Session: Phase 0-3 Complete*
*Date: 2026-04-07*
