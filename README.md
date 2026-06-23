# Telegram bot for VPN provisioning

Self-hosted VPN service driven by a private Telegram forum-group: users self-serve via the bot, an admin runs ops through inline buttons and an HTTPS Mini-App dashboard, and a Kimi-CLI agent handles diagnostics straight from a topic in the same chat.

## Topology (two-node split)

```
┌─ exit node ────────────────────────────────────────────────────────────┐
│                                                                          │
│  ┌──────────────────── docker compose project "vpn-bot" ──────────────┐  │
│  │                                                                     │  │
│  │   vpn-bot (sync polling, aiohttp dashboard :8080)                  │  │
│  │     ├─ services/xui_service, kimi_client, notifications…           │  │
│  │     └─ /var/lib/vpn-bot/bot.db   (volume vpn-bot-data)             │  │
│  │                                                                     │  │
│  │   3x-ui (Xray Reality :443, panel :2026)                           │  │
│  │     └─ x-ui.db                   (volume 3xui-data)                │  │
│  │                                                                     │  │
│  └─────────────────────────────────────────────────────────────────────┘  │
│                                                                          │
│  Caddy        :9443 → bot :8080   (Let's Encrypt via HTTP-01 on :80)     │
│  kimi-bridge  :7077 ← bot          (FastAPI shim around kimi-code CLI)   │
│  kimi-code                                                                │
│    └─ /root/.kimi-code  (binary, OAuth creds, sessions, skills/, .env)   │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
                              │
                              ↓ ssh + iptables DNAT (admin only)
┌─ entry node ───────────────────────────────────────────────────────────┐
│  iptables PREROUTING DNAT 443 → exit-host:443                            │
└──────────────────────────────────────────────────────────────────────────┘
```

Both hosts and the dashboard FQDN are operator-supplied via `.env` — no defaults are baked into the repo.

## What the bot does

| User-side | Admin-side |
|---|---|
| `/start` → demo request, language pick, platform pick, VLESS link, "Open Dashboard" mini-app | `/admin` → Mini-App (PM) or URL with `admin_token` (group). Approve / reject / ban / revoke / reset / grant +100 GB / broadcast |
| Tap "Поддержка" → first message creates a forum topic with 🔒 Close · 📞 PM · 🚫 Ban buttons | Click 🔒 → ticket log goes to "Solved Issues" topic, media is copied across, user is notified, state returns to demo/paid |
| `/mykey` to fetch their VLESS string again | `/ai <prompt>` or free text in #Kimi topic — Kimi-code runs on the host, can ssh into the entry node, push to GitHub, read screenshots dropped into `/tmp/tg_media` |

## Repo layout

```
bot/
├── core/        ── bot, polling, web_server, telegram_client, database (+ repos), state_machine
├── handlers/    ── commands, messages, admin, forum, ai_handler, callbacks/{user,admin,dispatcher}
├── services/    ── xui_service, kimi_client, notifications (+ scheduler), user_lifecycle, vpn, system_stats
├── webapp/      ── Mini-App (index.html, app.js, style.css) — health, users, stats panels
├── utils/       ── admin_token (HMAC), validators, callback_router, helpers
└── config/      ── settings.py, constants.py (UserState enum)

skills/          ── markdown skill files synced to /root/.kimi-code/skills/ on the host
├── vpn-ops/             ── Xray, X-UI, traffic, client configs
├── server-admin/        ── Docker, systemd, logs, SSH, git workflow
├── code-review/         ── review checklist + when-not-to-push gate
├── incident-response/   ── mass-outage runbook (triage order, broadcast, rollback)
└── billing-ops/         ── subscriptions, payments, quota grants, refunds

scripts/         ── install.sh, deploy_both_nodes.sh, backup.sh, kimi_bridge.py
systemd/         ── vpn-bot-backup.{service,timer}, caddy + kimi-bridge unit fragments
docs/            ── ARCHITECTURE.md, USER_FLOWS.md, ADMIN_FLOWS.md, REFACTOR_PLAN.md
AGENTS.md        ── operational notes / dev log
```

## Required `.env` settings

The repo ships an `.env.example` — copy it and fill in your own values:

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | from @BotFather |
| `SUPER_ADMIN_ID` | your Telegram user id |
| `FORUM_GROUP_ID`, `TOPIC_*` | private forum group + thread ids |
| `EXIT_NODE_IP`, `ENTRY_NODE_IP` | infra topology (used by health checks, deploy scripts) |
| `WEBAPP_URL` | HTTPS URL of your dashboard (your DuckDNS / domain + port) |
| `REALITY_PUBLIC_KEY`, `SID_VALUE`, `SNI_VALUE` | Xray Reality params for issued VLESS links |
| `XUI_USERNAME`, `XUI_PASSWORD` | 3x-ui admin |
| `KIMI_BRIDGE_URL`, `KIMI_BRIDGE_TOKEN` | optional, enables `/ai` |

## Quick start

```bash
# 0. clone
git clone <this-repo> /opt/vpn-bot
cd /opt/vpn-bot

# 1. config — fill .env from .env.example
cp .env.example .env
$EDITOR .env

# 2. one-shot installer (Caddy + kimi + bridge + backup timer; flags to skip parts)
sudo bash scripts/install.sh                 # all-in
sudo bash scripts/install.sh --no-kimi       # bot only

# 3. or bring up the stack manually
docker compose up -d --build
docker compose ps                            # vpn-bot + 3x-ui, both healthy

# 4. /admin in the forum group → "Open Dashboard"
```

## Dashboard auth (two paths)

- **PM with bot** → `web_app` inline button → Telegram injects `Telegram.WebApp.initData` → server validates HMAC against `BOT_TOKEN`.
- **Forum group** → `url` button (Telegram disallows `web_app` in groups) → URL carries `?admin_token=<HMAC>`. Token is HMAC-SHA256 over `BOT_TOKEN` + admin_id + 24h expiry, minted fresh on every `/admin`. Works in any external browser.

Code: [bot/utils/admin_token.py](bot/utils/admin_token.py) + `_validate_admin` in [bot/core/web_server.py](bot/core/web_server.py).

## Kimi-CLI integration

Kimi runs as root on the host (no isolation — operator choice). Reached from the bot container at `http://host.docker.internal:7077` (kimi-bridge FastAPI). Per-conversation context lives in a SQLite `ai_sessions` table.

| Trigger | Behaviour |
|---|---|
| `/ai <prompt>` (any topic, admin only) | Default mode (`fast`), routed via skill markers |
| `/ai_plan` / `/ai_fast` / `/ai_yolo <prompt>` | Force `--plan` / no flag / `-y` |
| `/ai_skill <vpn-ops\|server-admin\|code-review\|incident-response\|billing-ops> <prompt>` | Force one specific skill |
| `/ai_status` / `/ai_reset` | Bridge health / drop current session |
| Free text in `TOPIC_AI` (admin only) | Same as `/ai`, default mode |
| **Photo in `TOPIC_AI`** | Downloaded to `/tmp/tg_media` (shared mount), path spliced into prompt, cleaned up after the response |

Skill routing in [bot/services/kimi_client.py](bot/services/kimi_client.py): ~150 markers across 5 domain groups + a generic fallback. `incident-response` short-circuits everything else when it fires.

## Server-side keys layout

```
/root/.ssh/
├── entry_node_kimi        # ED25519 → root@<entry-host> (alias `ssh entry-node`)
├── entry_node_kimi.pub
├── github_kimi            # ED25519 → git@github.com (GitHub deploy key, write enabled)
├── github_kimi.pub
├── config                 # aliases entry-node, github.com
└── known_hosts            # github.com pinned

/root/.kimi-code/.env       # ENTRY_NODE_IP, REPO_PATH, REPO_REMOTE_SSH, GITHUB_SSH_KEY, …
```

The kimi-bridge systemd unit loads `/root/.kimi-code/.env` via `EnvironmentFile=-`, so the kimi process sees those vars without extra plumbing.

## Scheduled jobs (NotificationService, APScheduler)

| Job | Interval | What it does |
|---|---|---|
| `check_expiring` | 1h | warns users whose subscription expires within 24h |
| `ticket_cleanup` | 24h | drops `ticket_messages` rows older than 30d (Telegram Solved-issues topic is the long-term archive) |
| `support_state_repair` | 1h | reverts users stuck in `status=support_topic` with `support_topic_id=NULL` (clicked Поддержка, never wrote) |
| `tg_media_cleanup` | 1h | unlinks `/tmp/tg_media/*` older than 1h (safety net for per-request cleanup) |

## Testing

```bash
python3 -m pytest tests/unit/ tests/integration/ -q
```

## Deployment

Single command on a fresh server:

```bash
sudo bash scripts/install.sh
```

For incremental updates from the dev machine:

```bash
git push origin main
ssh root@<exit-host> 'cd /opt/vpn-bot && git pull && docker compose up -d --build vpn-bot'
```

Kimi-from-host can do the same via `git pull && git push kimi-origin main` (write via deploy key, read via HTTPS).

## Documentation

- **[AGENTS.md](AGENTS.md)** — full dev log, schema gotchas, architectural decisions
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — module-level architecture
- [docs/USER_FLOWS.md](docs/USER_FLOWS.md) / [docs/ADMIN_FLOWS.md](docs/ADMIN_FLOWS.md) — UX flows
- [skills/](skills/) — Kimi runbooks (synced to host)

## License

MIT
