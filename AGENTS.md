# AGENTS.md — VPN Bot

> Agent-focused guidance. For project overview, see `docs/PROJECT.md`.

---

## Project Overview

Python Telegram bot for VPN service management. Supports forum-group mode (structured topics) and PM-only mode.

**Current test status (2026-09-06):**
```bash
python3 -m pytest tests/ -q      # 2591 passed (unit + integration; e2e excluded via norecursedirs)
python3 -m pytest tests/e2e -q   # 6 passed — Playwright browser smoke, SEPARATE stage
                                 # (playwright's sync API poisons pytest-asyncio if mixed)
```
Four test levels (unit+mutmut / real-sqlite integration / Playwright E2E /
deploy sha-smoke) — see the 2026-08-30 insights below for what each catches.
E2E deps: `pip install -r requirements-dev.txt && playwright install chromium`.

---

## Architecture

```
bot/
├── config/              # Settings, constants, messages
├── core/
│   ├── database.py      # Legacy Database facade → repositories
│   ├── state_machine.py # StateMachine (sync only)
│   ├── cluster/         # Multi-node cluster code (election, routing, sync API)
│   └── repositories/    # Repository layer (User, Ticket, Node, MessageMap + async adapters)
├── handlers/
│   ├── callbacks/       # Modular callback handlers (sync only)
│   ├── admin/           # Admin command handlers (base, users, broadcast, stats)
│   ├── commands.py      # User commands
│   ├── messages.py      # Message forwarding / support
│   └── base.py          # Base handler
├── services/
│   ├── xui_service.py   # Unified X-UI service (HTTP API + DB fallback)
│   ├── vpn.py           # VLESS key generation
│   ├── notifications.py # Notification service (sync)
│   └── node_cluster.py  # Cluster coordination manager
└── utils/               # Helpers, validators, metrics, callback_router
```

**Removed (legacy async stack):**
- `database_async.py`, `bot_aiogram.py`, `main_async.py` — deleted
- `handlers/callbacks/*_async.py` — deleted
- `services/notifications_async.py` — deleted
- `AsyncStateMachine` — removed from `state_machine.py`

---

## Key Patterns & Constraints

### 1. Legacy Deprecation Policy

The `Database` class in `bot/core/database.py` is a **backward-compatibility facade**. All direct DB access methods emit `DeprecationWarning` and delegate to repositories:

```python
# Legacy (deprecated, emits warning)
self.db.get_user(chat_id)

# Correct way
from bot.core.repositories.user import UserRepository
repo = UserRepository(self.db.db_path)
repo.get_by_id(chat_id)
```

**DO NOT refactor all handlers at once** — many unit tests mock `self.db.get_user()`. Change handler + its tests together.

### 2. X-UI Service Sync Wrappers

`XUIService._run_sync()` uses `ThreadPoolExecutor` + `asyncio.run()` fallback. Never call `loop.run_until_complete()` from async handlers.

### 3. DB Schema Changes

If you modify `database.py` `init_db()` schema, also update:
- `tests/integration/test_migration.py`
- `tests/unit/test_database.py`

### 4. Rate Limiting & IDOR

`DemoRequestHandler` implements 60-second rate limiting via `_demo_request_times` dict. Clear it in tests:
```python
from bot.handlers.callbacks.user import DemoRequestHandler
DemoRequestHandler._demo_request_times.clear()
```

All user-facing callbacks enforce **IDOR protection** — users can only act on their own `chat_id` unless admin.

---

## Testing Guidelines

```bash
# Full suite
PYTHONPATH=$(pwd) pytest -q

# Specific file
PYTHONPATH=$(pwd) pytest tests/unit/test_vpn.py -v
```

**CI** (`.github/workflows/ci.yml`; every PR into `main` and push to `main`, Python 3.11): job `tests` runs
`pytest tests/ -m "not requires_docker and not requires_network"`, job `e2e` runs `pytest tests/e2e` on headless
Chromium with `E2E_REQUIRE_BROWSER=1` (a missing browser fails instead of skipping). The suite must pass in a clean
venv with no network: mark live-stack/internet tests `requires_docker`/`requires_network` (pytest.ini), bind test
servers to port 0, put test-only imports in requirements-dev.txt (GeoIP is stubbed for every test in tests/conftest.py).

Some tests suppress expected `DeprecationWarning` from the legacy Database facade:
```python
pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)
```

Files with this mark: `test_database.py`, `test_security.py`, `test_state_machine.py`, `test_handlers.py`, `test_full_flow.py`, `test_migration.py`, `test_code_review_fixes.py`

---

## Common Pitfalls

1. **Do not change `StateMachine` to use `UserRepository`** without updating all tests that mock `db.get_user` / `db.update_status`.
2. **XUI_DB_PATH must match the Docker volume** used by the `3x-ui` container. Current correct path: `/var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db`.
3. **Always run `validate_db_path_sync()` before DB operations** to detect path mismatches early.

---

## Files to Update Together

| If you change... | Also update... |
|------------------|----------------|
| `bot/core/state_machine.py` | `tests/unit/test_state_machine.py` |
| `bot/handlers/callbacks/user*.py` | Corresponding unit + integration callback tests |
| `bot/services/xui_service.py` | `tests/integration/test_xui_service.py`, `tests/integration/test_code_review_fixes.py` |
| `bot/core/database.py` schema | `tests/integration/test_migration.py`, `tests/unit/test_database.py` |
| `bot/models/vpn_node.py` | All cluster tests using `VPNNode` |

---

## Deployment

**Active production (as of 2026-08-30; flipped since the May notes — the bot moved to ENTRY on 2026-07-19):**
- **Entry node** (`ssh entry`) — the **vpn-bot container** lives HERE (`/opt/vpn-bot`), plus a LOCAL `3x-ui` for CF-fronted inbounds, ingress :443, dashboard API :8080, and the Hermes `/ai` agent (:4097, systemd `hermes-api.service`).
- **Exit node** (`ssh vpn-exit`) — the real x-ui panel :2026 + xray :443 (the backend the bot manages over HTTP API), Caddy :9443 → entry :8080 (dashboard TLS), tinyproxy :8888 (bot's Telegram egress).
- **GitHub**: `git@github.com:Annoyt/VPNautodeploy.git` — `origin/main` is the source of truth.
- **`/opt/vpn-bot` on entry is NOT a git checkout** — it's an rsync target. `git pull/reset` there is impossible; code that isn't rsynced doesn't exist in prod (a fix once sat undeployed for 11 days while everyone debugged "a bug").

**Canonical deploy (2026-08-30+), from the dev machine:**
```bash
./scripts/deploy_to_entry.sh                 # full bot/ sync
./scripts/deploy_to_entry.sh bot/core/web_server.py   # or a subset
```
It stamps the git sha into `bot/version.txt`, rsyncs ONLY `bot/` + `scripts/`
(never compose/.env — entry keeps hand-tuned copies), runs the remote
`deploy_entry_bot.sh` (hardcoded `--no-deps` so 3x-ui is never recreated —
see the 2026-07-19 incident), then FAILS unless `/health` reports that
exact sha. Commit before deploying or the stamp says `-dirty`.

- Compose project name is pinned to `vpn-bot` via `name: vpn-bot` — must not change (volume names derive from it).
- Volumes on entry: `vpn-bot_3xui-data` (local 3x-ui), `vpn-bot_vpn-bot-data` (bot.db at `/var/lib/vpn-bot/`), `vpn-bot_vpn-bot-logs`.
- The bot talks to BOTH panels via HTTP API only; `xui.db` direct access is dead on entry (the May notes about making x-ui.db group-writable are historical).
- **Backup subscription domain (2026-10-08, IMPROVEMENT_PLAN E5):** `https://sub.nekoweather.xyz:2096` — Cloudflare-proxied A record to exit, Caddy site block on exit `:2096` (a port CF proxies end-to-end) reverse-proxying entry `:8080` + the `/rule-sets/` mirror; cert from acme.sh `dns_cf` installed to `/etc/ssl/sub` with reloadcmd `chgrp caddy … && chmod 640 … && systemctl reload caddy` (acme.sh writes the key 600 root — without the hook every renewal breaks the Caddy reload). Caddyfile backups: `/etc/caddy/Caddyfile.bak-*`.

**Backups before risky operations:**
```bash
# On prod
mkdir -p /opt/backups
TS=$(date +%Y%m%d-%H%M%S)
tar --exclude="/opt/vpn-bot/venv" --exclude="*/__pycache__" -czf "/opt/backups/vpn-bot-files-${TS}.tar.gz" /opt/vpn-bot
for vol in vpn-bot_3xui-data vpn-bot_vpn-bot-data vpn-bot_vpn-bot-logs; do
  docker run --rm -v ${vol}:/data:ro -v /opt/backups:/backup \
    alpine tar czf "/backup/volume-${vol}-${TS}.tar.gz" -C /data .
done
```

---

## Operational Insights (2026-05-12)

These are bear traps that bit during the prod consolidation. Read them before touching deploy/compose.

### 1. x-ui binds to 2026 internally, not 2053
The 3x-ui web/API listens on port **2026** inside its container (`Web server running HTTP on [::]:2026` in logs). Several old compose files declared `2053:2053` mapping, and `XUI_API_URL=http://3x-ui:2053` — that combination cannot work. The bot only reached x-ui via Docker's internal bridge anyway, but healthchecks broke. Always use 2026.

### 2. x-ui DB needs writable group access for the bot
The bot writes to `x-ui.db` for `add_client_sync`, `remove_client_sync`, and `sync_all_clients_from_bot_db` on startup. Inside `vpn-bot_3xui-data`, x-ui creates files as `root:root 0644`, but vpn-bot runs as UID 1000. Fix after first start:
```bash
docker run --rm -v vpn-bot_3xui-data:/v alpine sh -c "chown root:1000 /v/* && chmod 664 /v/*.db /v/*.db-shm /v/*.db-wal"
docker restart vpn-bot
```
The mount in compose must **not** carry the `:ro` flag.

### 3. Compose project name = volume prefix
Docker Compose names volumes `<project>_<volume>`. Project name defaults to the parent directory; if you `docker compose up` from a differently-named directory (e.g. `vpn-bot-refactor/`), you'll silently create a parallel set of volumes and lose data. The fixed `name: vpn-bot` in compose protects against this.

### 4. Multiple legacy bot.db files on host
`/opt/vpn-bot/data/bot.db`, `/opt/vpn-bot/data/vpn_bot.db`, `/etc/cascade-vpn/bot.db` are all **stale** (April snapshots) — the live DB is inside the `vpn-bot_vpn-bot-data` Docker volume. Don't try to "merge" them; treat them as cold backups only.

### 5. Failover code exists but is not wired
`bot/core/cluster/smart_routing.py` (`SmartRoutingTable`, `should_failover`) is **never instantiated in production code** — only in tests. Same for `create_failover_api`. Multi-node `_generate_multi_node_link` in `vpn.py` is also dead because handlers call the legacy `generate_vless_link(uuid, email)` directly. The Phase 5 roadmap in conversation memory plans to wire these up via a `QuotaMonitor` background task.

### 6. Compose must pass SID_VALUE / SNI_VALUE / DEMO_* explicitly
`bot/config/settings.py` reads `SID_VALUE` etc. via `os.getenv(...)` with `'01'` as the default. `os.getenv` looks at the **container's** environment, not the project `.env`. If the compose service lacks `- SID_VALUE=${SID_VALUE}`, the bot silently falls back to `sid=01` and every generated VLESS link will fail Reality auth — TLS masquerade still completes (clients show "Connected") but no tunnel is built. We fixed this on 2026-05-12 but it is the easiest bear trap in this repo to re-introduce.

### 7. Phase 1 hardening of GetKeyHandler (deployed 2026-05-12)
The key-issuance flow has three guardrails layered onto it:
- `GetKeyHandler._inflight_chat_ids` (class-level set + `threading.Lock`) drops duplicate "get key" callbacks that fire while the first one is still running. Survives across `asyncio.run()` cycles that `_run_async` may spin up.
- `_sync_to_xui` retries up to `SYNC_MAX_ATTEMPTS=3` with exponential backoff (`SYNC_BASE_BACKOFF_SEC * 2^(attempt-1)`) and per-attempt `SYNC_TIMEOUT_SEC=15.0` timeout. Tests monkeypatch these constants to keep CI fast.
- `_send_key_to_user` and `/mykey` both call `bot/utils/validators.py::validate_vless_url` before notifying the user, rejecting `vless://None@...`, `vless://@host`, missing port, etc.
Don't remove any of these without updating `tests/unit/test_get_key_handler_phase1.py` and `tests/unit/test_key_creation_rollback.py`.

For full procedures, see `docs/PROJECT.md` §6 and `scripts/deploy.sh`.

---

## Operational Insights (2026-05-29 session)

A long session covering: lifecycle helper unification, dashboard expansion, TLS reverse proxy, group-mode admin UX, kimi-code agent integration. Notes below in chronological order so an agent can re-create the same outcome step-by-step.

### 8. One revoke path through `services/user_lifecycle.py`
`/reject`, `/ban`, the Reject and Revoke inline callbacks, plus the dashboard `reject`/`ban` actions all used to set state and only sometimes remove the x-ui client / clear `uuid+email`. The single helper `revoke_user_key(user, xui, db)` does both. `/unban` clears leftover `uuid+email` defensively so legacy rows can't re-issue. `/reset` and the `_reset_approval` callback now also zero `reject_count`, otherwise a user who hit `MAX_REJECT_RETRIES` stayed locked out forever after admin reset.

### 9. Status guard on key issuance
`GetKeyHandler._KEY_ALLOWED_STATUSES` and `CommandHandler._MYKEY_ALLOWED_STATUSES` are `{"demo", "paid", "support_topic"}`. A rejected user can still hit "Demo" → `PENDING_DEMO` (allowed by state machine), but `_process_key_request` refuses to hand out a key until they go through admin approve again. Without this, a stale `uuid` in the user row let them resync their old key.

### 10. Dashboard now covers every status
`bot/webapp/app.js::getAvailableActions` returns reset/ban for `new`, reset+reject+ban for `platform_select`, reset+revoke+ban+grant_100gb for `demo/paid/support_topic`, unban+reset for `banned`. Three new backend actions in `web_server.handle_user_action`: `reset` (set_state NEW + reset_user_data), `revoke` (BANNED + revoke_user_key + notify), `grant_100gb` (no state change, bumps `user.quota_gb` and propagates `totalGB` to x-ui via `add_client_sync`).

### 11. Dashboard broadcast endpoint
`POST /api/admin/broadcast` with `{text, confirm: bool, audience: "active"|"demo"|"all_known"}`. confirm=false returns a recipient count + 10-username sample; confirm=true sends through `Bot.send_message` in a worker thread with 50 ms cooldown between sends (well under Telegram's 30 msg/s cap). Logged into `admin_actions` table as `webapp_broadcast_<audience>`. UI lives in `app.js::openBroadcastModal` — two-step modal with textarea + audience dropdown.

### 12. TLS reverse proxy for the admin Mini App
Telegram WebApp requires HTTPS. Xray Reality already owns :443 on the host. Solution: Caddy installed via the official apt repo, listening on **:9443** for `<dashboard-host>`, automatically handling Let's Encrypt via HTTP-01 on :80. Caddyfile is two lines:
```
<dashboard-host>:9443 {
    reverse_proxy 127.0.0.1:8080
}
```
Cert renews automatically. The bot publishes `WEBAPP_URL=https://<dashboard-host>:9443/` to admins; the hardcoded ngrok default in `settings.py` is now empty so the bot fails loudly instead of pointing at a dead URL.

### 13. Admin UX in forum groups
Two Telegram Bot API constraints that bit us:
- The persistent Mini-App menu button (left of the input field) is 1:1-chat-only — silently ignored in groups.
- Inline `web_app` buttons are also 1:1-chat-only and return `BUTTON_TYPE_INVALID` in forum groups.

`CommandHandler.handle_admin` now picks shape by chat kind: `web_app` button in PM, `url` button (same URL, opens Telegram in-app browser) in groups. `_is_admin` calls in `commands.py` now read `user_id` from `message.from.id` via the new `_command_user_id(update)` helper — the old `_is_admin(chat_id)` was always false in a group because `chat_id` is the group's negative ID.

### 14. Telegram API errors are mute unless you pull `description`
`bot/core/telegram_client.py::_request` now logs the Telegram-returned `description` from the 4xx response body plus the method name + a tag from the payload (`chat_id`, `thread`, `text[:60]`, `has_keyboard`). Without that we lost an hour chasing why "400 Bad Request" was happening — turned out to be `BUTTON_TYPE_INVALID` in a forum group.

### 15. AI agent: kimi-code via a host-side HTTP bridge
Kimi-code CLI is installed in `/root/.kimi-code` on the **host**, not inside the container — its OAuth credentials live in `/root/.kimi-code/credentials` and its binary is 136 MB. The bot reaches it via a tiny FastAPI wrapper:

- **`kimi-bridge.service`** (systemd unit, `/usr/local/bin/kimi_bridge.py`) listens on `0.0.0.0:7077`. Endpoints: `POST /ask {prompt, session_id?, model?}`, `GET /health`, `POST /reset`. Uses `--output-format stream-json` and parses `role=meta, type=session.resume_hint` events to extract `session_id`. Auth via `X-Bridge-Token` header (random 24-byte hex, stored in `/etc/kimi-bridge.env`).

- **docker-compose** adds `extra_hosts: "host.docker.internal:host-gateway"` so the bot resolves the bridge from inside the container at `http://host.docker.internal:7077`.

- **bot/services/kimi_client.py** wraps the HTTP API and persists per-conversation `session_id` in a new SQLite table `ai_sessions(session_key TEXT PRIMARY KEY, kimi_session TEXT, ...)`. Keys: `"pm:<chat_id>"` for DMs, `"topic:<chat_id>:<thread_id>"` for forum topics.

- **bot/handlers/ai_handler.py** runs *before* `CommandHandler` (so `/ai` isn't swallowed). Three entry points: `/ai <prompt>`, `/ai_reset`, and free-text inside `TOPIC_AI` (env var = thread id of the "AI" topic in the forum group; admin-only).

- `TelegramClient.send_chat_action("typing")` is fired before each Kimi call so the admin sees a typing indicator during the 5–30s wait.

The OAuth login is interactive (`kimi → /login` opens a browser). Bootstrap once via `ssh root@<host> -t tmux attach -t kimi-setup`. We pre-create that tmux session in the installer.

### 16. Why not give Kimi full root over the prod box?
We did. The user explicitly asked for "no isolation". Important consequences:
- Kimi can `rm`, `docker compose down`, exfiltrate `.env`, etc.
- First message in any new session should be a system-prompt: "Don't print `/opt/vpn-bot/.env`, `/root/.kimi-code/credentials/*`, ask before destructive ops." Kimi remembers it via session memory.
- If you tighten this later, the natural restriction point is the bridge: filter the `prompt` for known dangerous commands, or run kimi under a non-root user with selective `sudoers` rules.

### 17. Backup state
Tarballs live at `/opt/backups/*.tar.gz` on the prod host — manual snapshots taken before each risky migration. Automated job lives in `scripts/backup.sh` + `systemd/vpn-bot-backup.{service,timer}`. The timer runs daily and keeps the last 7 snapshots; older ones are pruned. Volume contents (`vpn-bot_3xui-data`, `vpn-bot_vpn-bot-data`) are dumped via an `alpine` one-shot container that mounts the volume read-only and tars it to the host directory.

### 18. Support-ticket rework (2026-05-31)

Multi-step fix of the support flow after Ilyastarasov's tickets started getting lost. Symptoms: clicking 🔒 did nothing, every user message spawned a fresh topic, the dashboard subscriptions panel returned HTTP 500.

**Schema fixes** — prod DB schema diverged from the `CREATE TABLE` in `bot/core/database.py`:
- `ticket_messages` actually has columns `message_text` + `timestamp`, not `text` + `created_at`. Every `_log_ticket_message` was silently failing.
- `subscriptions` actually has `started_at` + `expires_at`, not `start_date` + `end_date`. `handle_admin_subscriptions` raised 500, `get_expiring_subscriptions` returned [].
- The repo + dashboard SQL now use the prod column names; outward dict shapes keep `start_date`/`end_date` for caller compatibility.

**Topic persistence bug** — `notify_new_support_ticket` created the topic and returned `topic_id`, but `handle_support_message` never wrote it back to `user.support_topic_id`. Every subsequent message took the "new ticket" branch. Now we `save_user(user)` immediately after `notify_new_support_ticket` returns.

**Duplicate first-message** — `notify_new_support_ticket` already embeds the user's text in the initial "🆘 Support Request" post; the follow-up `forward_to_support` call was sending the same text a second time. Track `was_new_ticket` before the new-ticket branch and skip the forward in that case.

**Stale topic auto-recovery** — when an admin deletes a topic, the stored `support_topic_id` becomes useless. `forwardMessage` returned `Bad Request: message thread not found` 3× and `result['message_id']` crashed on None. `forward_to_support` now catches the failure, clears the stale id, mints a fresh topic via `notify_new_support_ticket`, saves the new id, and retries — all transparent to the user.

**🔒 Close button was broken** — `forum.handle_close_ticket` did `from bot.models import UserState`, but `UserState` lives in `bot.config.constants` (re-exported only via `bot.config`). ImportError every click → dispatcher logged "Error in handler CloseTicketHandler" + "Unknown callback data" and the admin saw the spinner vanish with no message. Switched import. Also wrapped the entire body in try/except so the next bug surfaces back into the topic instead of silently swallowing.

**Ticket UX rework** — initial Support Request post now has a 3-button inline keyboard:
- **🔒 Закрыть** — `close_ticket:<topic_id>` → `CloseTicketHandler` → `ForumHandler.handle_close_ticket`. Compiles a Russian log, sends to `TOPIC_SOLVED`, copies media via `copyMessage` (new `Bot.copy_message` wrapper), notifies the user (defaults `lang` to `ru` if None), runs `StateMachine.return_from_support`, renames the topic to `✅ @username` via `Bot.edit_forum_topic` (new wrapper), closes it, clears `user.support_topic_id`. Includes an idempotency check so duplicate clicks during stale-topic recovery don't double-fire.
- **📞 PM** — URL button to `https://t.me/<username>` or `tg://user?id=<chat_id>` for anonymous users. No callback handler needed.
- **🚫 Бан** — `ban_from_ticket:<chat_id>:<topic_id>` → new `BanFromTicketHandler`. Revokes the x-ui client + transitions to `BANNED` via `revoke_user_key` + `StateMachine.transition`, notifies the user, logs an audit row (`webapp_ban_from_ticket`), then chains into `handle_close_ticket` to archive the conversation.

**Topic title** — instead of `Support: @user` (truncated to 20 chars, no context), the title is now `🎫 @username · <first line of issue ≤60 chars>`, capped at 128. Lets the admin spot tickets in the topic list without opening each one.

**Daily cleanup** — `BackgroundScheduler` gets a new job `ticket_cleanup` (interval=24h) that calls `TicketRepository.cleanup_old_messages(days=30)`. The Telegram Solved archive is the long-term log; SQLite rows are only needed for the open + recent-review window.

**Dashboard auth via admin_token** — separate but adjacent fix: in groups the `/admin` button can't be `web_app` (BUTTON_TYPE_INVALID), only `url`. The url opens the dashboard in an external browser with no Telegram.WebApp.initData → every admin endpoint returned 401. `bot/utils/admin_token.py` mints a 24h HMAC-SHA256 token (signed with BOT_TOKEN), `handle_admin` appends it as `?admin_token=...` to the dashboard URL, `web_server._validate_admin` accepts either initData or token, and `app.js` parses the token from `window.location.search`.

### 19. Kimi domain-aware skill routing
The original 18-word flat trigger list in `kimi_client.ask()` made Kimi waste a turn on `ls /root/.kimi-code/skills/` and sometimes pick the wrong skill. Replaced with per-domain marker tuples (`VPN_OPS_MARKERS`, `SERVER_ADMIN_MARKERS`, `CODE_REVIEW_MARKERS`) plus a `GENERIC_TROUBLE_MARKERS` fallback. `_detect_skill_domains(prompt)` returns the matched skill names; `_build_skill_reminder(domains)` injects a system-reminder with the **exact SKILL.md path** so Kimi reads only the relevant skill. Negative-test prompts (привет, переведи, анекдот) match nothing → no reminder → no overhead.

### 20. Server-side keys for Kimi (/root/.kimi-code/.env)
Kimi runs unisolated as root on the prod box (<exit-host>). To let it diagnose the entry node (<entry-host>) without prompting for credentials and to let it sync the repo, two private keys live next to it:
- `/root/.ssh/entry_node_kimi` — SSH key for `root@<entry-host>` (entry). Public half is added to that box's `~root/.ssh/authorized_keys`.
- `/root/.ssh/github_kimi` — SSH key for `git@github.com`. Public half is registered on the GitHub repo as a deploy key with write access.
- `/root/.ssh/config` aliases: `Host entry-node` → <entry-host>, `Host github.com` → github with the right IdentityFile.
- `/root/.gitconfig` sets `user.name=kimi-bot` / `user.email=kimi-bot@local` so commits Kimi creates aren't anonymous.
- `/root/.kimi-code/.env` exports: `ENTRY_NODE_IP=<entry-host>`, `ENTRY_NODE_SSH_KEY=/root/.ssh/entry_node_kimi`, `REPO_PATH=/opt/vpn-bot`, `GITHUB_SSH_KEY=/root/.ssh/github_kimi`. The Kimi bridge already inherits the process env, so these reach the CLI for free.

**Security note**: anyone with shell on <exit-host> can read those keys. Rotate any time the host is suspected compromised. The user accepted this tradeoff for the convenience of one-click skill execution.

### 21. Photo ingestion + self-healing schedulers (2026-06-01)

Closed three remaining gaps in the support/AI flow.

**Telegram → Kimi photo ingestion.** `AIHandler.can_handle` was gating on `text` only — any message that contained a photo (with or without caption) silently dropped because `msg.get("text")` was empty. Now `can_handle` accepts photo OR text OR caption, and `handle` calls a new `_download_photo` helper that pulls the largest photo size via `TelegramClient.download_file` (also new — getFile + streamed HTTP) into `/tmp/tg_media/tg_photo_<chat>_<ts>_<file_id_tail>.jpg`. The host path is spliced into the prompt as `[Вложение от админа: …]`. `docker-compose.yml` mounts `/tmp/tg_media:/tmp/tg_media` so the kimi binary on the host sees the same file. A `finally` block in `handle` unlinks the photo after the request — temp files do not accumulate.

**Self-healing schedulers.** `NotificationService.start_scheduler` now registers two new APScheduler jobs:
- `support_state_repair` (hourly) finds users with `status='support_topic'` AND `support_topic_id IS NULL` and reverts them via `StateMachine.set_state`. This is the "tapped Поддержка but never wrote" pattern — 5 users were stuck like that on prod (boriskonale, Madina_Fat, sergod72, Ilyastarasov, ImLovingIt7), one-shot DB UPDATE flipped them back to demo.
- `tg_media_cleanup` (hourly) walks `/tmp/tg_media/` and unlinks any file with mtime > 1h. Backstop for the per-request `finally`.

**StateMachine.return_from_support edge cases.** Old code defaulted to DEMO unconditionally if the previous_state wasn't DEMO or PAID. Two problems: (a) a user who never had a key would jump from support_topic to demo, which is wrong (no email = need re-approval); (b) if the validated transition was rejected, the user would stay stuck. Now default is `DEMO if user.email else NEW`, and if `transition` returns False we fall through to `set_state` to force the move. The self-loop case where `previous_state == support_topic` also falls through to the email-based default instead of ping-ponging.

**Skills + triggers.** Five skills live now (`vpn-ops`, `server-admin`, `code-review`, `incident-response`, `billing-ops`). Trigger detection (`_detect_skill_domains`) routes by per-domain marker tuples; `incident-response` short-circuits everything else when it fires so the reminder during an outage doesn't drown in four skill paths.

**Branch cleanup.** `feature/xray-bot-integration` was the default branch on GitHub but `0` commits ahead of `main` (everything was already merged a few sessions ago). Switched the GitHub default to `main` and deleted the feature branch. Single canonical line of history.

### 22. AI agent: kimi-code → OpenCode (2026-07-04)

The `/ai` backend was fully switched from kimi-code to **OpenCode**. Kimi is gone — no fallback. If you're looking for `kimi_client.py`, `kimi_bridge.py`, or `kimi-bridge.service`, they were **deleted**.

- **No custom bridge anymore.** OpenCode ships its own headless HTTP server (`opencode serve`, :4096). The bot talks to it directly at `http://host.docker.internal:4096` with HTTP **basic auth** (`OPENCODE_SERVER_PASSWORD`, username `opencode`). The old FastAPI shim is obsolete.
- **Client:** `bot/services/agent_client.py::AgentClient` — thin `requests` client. Endpoints live in `_create_session` / `_send_message` (`POST /session`, `POST /session/{id}/message` with `{parts:[{type:text,text}], model?, agent?}`). Response parsing is deliberately tolerant (accepts `parts`/`info.parts`, `text`/`content`) because OpenCode's API shape drifts between versions — **verify against the pinned server's `/doc`** if prompts start failing.
- **Config:** `OPENCODE_URL`, `OPENCODE_USERNAME`, `OPENCODE_SERVER_PASSWORD`, `OPENCODE_DEFAULT_MODEL` (provider/model form), `OPENCODE_AGENT_PLAN/YOLO/DEFAULT`, `AI_DEFAULT_MODE`, `AGENT_NODE_TYPE` (`control`|`entry`). `KIMI_*` vars are removed.
- **Permissions:** `scripts/opencode.json` sets per-tool `allow`/`deny` (bash deny-list for catastrophic commands) — this is the structural fix for the old "root, no isolation" gap (§16). Tune the deny-list per deployment; note "ask" doesn't work headless, so use allow/deny only.
- **`[[SEND_FILE]]`:** the agent now writes files into the shared `/tmp/agent_out` bind-mount and the bot reads them directly (`AGENT_OUT_DIR`). The old bridge `/file` endpoint is gone. `/tmp/tg_media` (photo ingestion) is unchanged.
- **Ops:** `scripts/opencode.service` (systemd) + `scripts/setup_opencode.sh` replace the kimi units; `install.sh` flag is now `--no-agent`. Session memory still lives in the `ai_sessions` table (column `kimi_session` kept as opaque storage — no migration).
- **Skill routing** (`_detect_skill_domains` marker layer) was preserved as prompt injection. Not yet migrated: `skills/*/SKILL.md` still reference `/root/.kimi-code/` paths, and host-side SSH keys are still named `*_kimi` — cosmetic follow-ups, not blockers.

### 23. Reality mass outage: Microsoft's cert outgrew xray's 8192-byte buffer (2026-07-20)

Symptom: VLESS-Reality dead for ALL users (thousands of `REALITY: processed invalid connection ... handshake did not complete successfully` per day from real RU IPs), while hy2/stls/ws kept working. Root cause chain, all confirmed on prod:

- Microsoft rotated the `www.microsoft.com` cert chain (now OCSP-stapled) → the TLS Certificate record is **8273 bytes**, over the hardcoded `size = 8192` limit in `github.com/xtls/reality` `tls.go`. xray rejects the dest handshake → every authenticated client fails. Upstream issue: XTLS/Xray-core#6356. Debug signature (needs `show: true` in realitySettings): `Certificate: 8273` then `isHandshakeComplete.Load(): false`.
- **Any Reality dest can rot like this overnight** — the cert size is outside our control. Verify a candidate target before adopting it: `openssl s_client -connect <host>:443 -servername <host> -tls1_3 -msg </dev/null | grep -A1 'Certificate$'` → record must be ≤ ~8000 bytes. Sizes measured 2026-07-20: microsoft 8273 (BAD), bing 3920, google 2520, dl.google.com 4874, cloudflare 2521.
- Fix deployed: inbound 1 `dest`/`serverNames` → `www.bing.com`, entry HAProxy ACL `is_reality_sni` → `www.bing.com`, bot `.env` `SNI_VALUE=www.bing.com` + container restart. Keys/shortId unchanged. **All three layers must move together** (x-ui inbound, HAProxy SNI ACL, bot SNI_VALUE) and users must refresh their subscription — stale configs send the old SNI and fail auth.
- **Next time use the playbook (E10):** `scripts/rotate_reality_dest.py --sni <host>` (read-only check: cert record ≤ 8000 B, TLS 1.3, h2, measured from exit) then `--apply` — panel → HAProxy → `.env` + bot recreate → probe-proxy regen (a FOURTH layer: its config is generated from `SNI_VALUE`, forget it and DPIMonitor demotes Reality), snapshot first, `--rollback`; `docs/runbooks/rotate_reality_dest.md`.
- Red herring worth remembering: exit's IPv6 egress is flaky (1/6 connects to microsoft.com timeout; **0/12 from inside the 3x-ui container**) — looked like the cause but wasn't. Mitigation kept anyway: `sysctls: net.ipv6.conf.all.disable_ipv6=1` on the 3x-ui compose service (errno-99 fail-fast → instant Go fallback to IPv4). In repo compose since 2026-07-20.
- Rare single success among mass failures (one user got through once) is explained by Akamai edge rotation: some edges still served the old smaller cert.

**x-ui v3.5 panel API cheatsheet** (burned during this fix):
- Login requires: GET `/<webBasePath>/` → parse `csrf-token` meta tag + keep session cookie → POST `/<webBasePath>/login` JSON `{username,password}` with `X-CSRF-TOKEN` header. Form-encoded login 403s.
- Clients are **relational** since 3.4: `POST /panel/api/clients/add` fails with "email already in use" if the email exists on ANY inbound. To attach an existing client to another inbound use `GET /panel/api/inbounds/get/<id>` → append to `settings.clients` → `POST /panel/api/inbounds/update/<id>`.
- The `secret` row in the settings table is NOT a URL path component (login 404s if you treat it as one).
- After the 2026-07-19 panel wipe+restore, 5 users were missing on the SS inbound (id 5) and 3 on xhttp (id 6) — back-filled via the update-inbound path above. `sync_all_clients_from_bot_db` only reconciles inbound 1, so drift on 4/5/6 is invisible to it; audit with a per-inbound membership diff when users report "one protocol works, another doesn't".
- Exit's `/opt/vpn-bot/docker-compose.yml` has **diverged from origin/main** (still unpinned `:latest`; repo is digest-pinned). A `git reset --hard` there would wipe the sysctls hotfix and re-expose the image-pull bear trap — sync deliberately, and `docker compose up -d 3x-ui` (service-scoped, never bare `up -d`) to avoid recreating the dead vpn-bot service.

### 24. Telegram-egress failover + reserve DE node for paid users (2026-07-25)

**Problem 1: bot Telegram connectivity was a SPOF.** Entry is РКН-blocked from `api.telegram.org` directly (all TG IP ranges time out), so the bot's only egress was `HTTPS_PROXY` → tinyproxy on exit:8888. An entry↔exit flap on 2026-07-23 silently took the bot offline for minutes.

**Fix:** `TG_PROXY_URLS` (comma-separated, literal `direct` supported) drives a failover pool in `TelegramClient` — sticky active proxy, rotation on `ConnectionError` only (HTTP 4xx ≠ proxy failure), 120 s cooldown with half-open retry, creds stripped from logs. `TG_API_OUTAGE` module state feeds AlertManager `check_telegram_api` (critical on >3 min outage + one-shot recovery notice). A second tinyproxy now runs on the **reserve DE node** (mytherm, also hosts the vkmusicbot prod — do not disturb its containers/nginx). Its tinyproxy config is a copy of exit's (Allow entry IP + BasicAuth, ConnectPort 443/563) plus a ufw rule scoped to the entry IP.

**Problem 2: paid users had nowhere to switch when the main cascade degrades.** The reserve node's x-ui (host install, **panel 2.8.11** — form login, classic `/panel/api/inbounds/addClient`+`delClient/<uuid>`, NO CSRF, panel is **https** on :2026 with webBasePath `/sub/`) holds VLESS+Reality inbound 1 (`www.google.com` SNI — cert record 2520 B, safe from the #6356 size trap).

**Fix:** `bot/services/fallback_node.py::FallbackNodeService` — lazy provisioning on paid `/sub` fetch (idempotent, 10-min membership cache — on the CLASS, keyed by uuid: `/sub` and `/kit` build a service per request, and until 2026-10-10 the cache sat on the instance, never hit, and every paid `/sub` fetch logged into the reserve panel; `remove_client` evicts the uuid; a failed panel call is not cached), same uuid as the main system, revocation mirrored in `revoke_user_key`. Subscription appends a `<email>-de` outbound for `FALLBACK_ALLOWED_STATUSES=('paid','support_topic')`. Bear traps learned the hard way:
- The fallback session MUST set `trust_env = False` — otherwise the panel call rides `HTTPS_PROXY` and tinyproxy's ConnectPort allowlist 403s the :2026 CONNECT.
- The subscription FALLBACK block is wrapped in try/except: a dead reserve panel must never kill `/sub` for all paid users.
- …nor slow it down (2026-10-10): `/sub` never awaits the panel. `WebAppServer._schedule_fallback_provisioning` runs `ensure_client` as a background task, one per uuid in flight, on a one-thread pool of its own. The outbound is in the profile either way. Each panel request may take the 15-s timeout. Awaited inline, a blackholed panel held every paid fetch that missed the cache, and a few hung calls on asyncio's default pool (cpu+4 threads) stalled every `to_thread` of the web server, including `/sub`'s own token lookup. A connection-level failure (refused, reset, timeout) makes every `ensure_client` skip the panel for 60 s (`_PANEL_SKIP_S`); with one worker, a dead panel costs one timeout, not one per user. An answer, even an error, opens no window, and `remove_client` (revocation) always asks.
- Compose must pass `FALLBACK_NODE_*`/`EXIT_NODE_IP` explicitly — `.env` alone doesn't reach the container.
- Panel admin creds were reset via `x-ui setting -username admin -password …` on 2026-07-25 (old password unknown); they live in entry's `.env` as `FALLBACK_NODE_XUI_USER/PASS`. ufw on the reserve allows :2026 from entry (note: a pre-existing "Anywhere" rule for 2026 exists — tightening it is a deliberate follow-up, verify the owner doesn't use the panel from elsewhere first).

**Deploy topology gotcha:** entry's `/opt/vpn-bot` is NOT a git repo — deploy there is `rsync` (excludes in scripts/deploy_entry_bot.sh usage note) + `./scripts/deploy_entry_bot.sh` (builds and recreates ONLY vpn-bot, `--no-deps` so 3x-ui is never dragged along). GitHub pushes run from the exit host via `/root/.ssh/github_kimi` (both local and exit `origin` remotes are HTTPS and can't push; push with an explicit `git@github.com:…` URL). Exit's checkout has unpublished prod commits — never `git reset --hard` there blindly; push refs without touching its working tree (`git fetch bundle main:refs/remotes/local/main && git push <ssh-url> refs/remotes/local/main:main`).

### 25. Client-app churn: Happ out, Karing in; multiple /sub formats (2026-07-25)

RU App Store purged proxy clients in waves: first Hiddify, then Happ (verified gone by 2026-07-25). Current recommendations: **Android/PC → Hiddify**, **iOS → Karing** (sing-box, still in RU store, reads the plain `/sub` with zero special-casing). All user-facing text (key card, /sub, PLATFORM_INSTRUCTIONS, email letter) reflects this.

`/sub/<token>` now serves **three formats** (web_server.handle_subscription):
- **default sing-box JSON** — Hiddify/Karing;
- **`?format=links`** (or a `Happ*` UA) — plain-text share-links, one server per line, for v2rayNG/Streisand. MUST stay plain text: Happ iOS silently imported nothing from a base64 blob. Reuses VPNService generators; `generate_hy2_link` now carries `obfs`/`mport` params (raw hy2 links were dead since salamander went live server-side — try_alt:hy2 fixed by the same change);
- **`?format=xray`** — full xray-core client config. Happ imports it as a SINGLE profile (passes 1:1 to core) — that's why "only one key" showed up; kept for raw-config/TV use cases.

**Dashboard `grant_paid` action** — demo/support_topic → PAID via normal transition, user notified, full cascade + DE fallback unlock on next /sub refresh. Hidden for already-paid rows.

**📧 email prompt is now stateful** — the button arms `MessageHandler.PENDING_EMAIL` (10 min TTL); the user's next plain-text message is validated and saved to `contact_email`. Previously the button only printed /setemail instructions and users' bare-address replies died in the "I don't understand" fallback. Gotcha that cost a debug cycle: `callbacks/user.py` has TWO `EmailPromptHandler` classes — the second definition (bottom of file) shadows the first; edit the bottom one.

**Platform re-selection**: `setplat:<p>` buttons on the key card and /sub let key holders switch device without admin help. Deliberately bypasses `PlatformSelectHandler` (its `_process_platform_selection` forces a DEMO transition) — SetPlatformHandler only updates `user.platform` and re-renders the card via the shared `build_key_delivery_message()`.

**ziriki LTE case (2026-07-25)**: hy2 handshake passes on throttled mobile UDP but streams die in ~8s (`tx:0` → "timeout: no recent network activity"). Nothing server-side left to fix — QUIC needs clean UDP. UDP *apps* (calls) still work over xudp inside TCP protocols via the 'calls' selector. Also: roaming RU SIMs get home-operator DPI abroad.

### 26. Architecture snapshot — 2026-08-19 (большой ремонт учёта/биллинга)

(Перенесено из удалённого PROJECT_CONTEXT.md; полная хроника — в памяти агента.)

**Тарифы.** Демо = freemium 10 ГБ/мес навсегда (DEMO_TRAFFIC_GB=10, DEMO_DAYS=30); paid = 100 ГБ/мес (PAID_TRAFFIC_GB, floor) до даты `users.subscription_expiry`. Единственное определение paid-тира — `bot/services/billing.py:grant_paid_access()` — через него ходят Stars-оплата, /approve_payment и дашборд. Месячный сброс счётчиков (демо+paid) — ботовская джоба 1-го числа 00:00 UTC; панельный rolling reset отключён.

**Учёт трафика.** Панель exit считает все xray-протоколы в одну строку client_traffics на email (держится на api-инбаунде dokodemo 127.0.0.1:62789 в xrayTemplateConfig). Hy2 доливает systemd-мост `hy2-traffic-collector` на exit (hysteria trafficStats API), он же кикает over-quota live-сессии и бампает last_online. Бот зеркалирует цифры в users.traffic_* каждые 10 мин и шлёт предупреждения на 80%/100% квоты (ext-юзерам — письмом).

**Почта.** Исходящие (ключи, уведомления) — Gmail SMTP-релей; входящие заявки на ключ — IMAP-поллер каждые 3 мин → карточка с кнопками «Выдать (демо)/Отклонить» в топик заявок; карточки самообновляются.

**Мониторинг.** probe-proxy сайдкар (sing-box, конфиг генерится scripts/gen_probe_config.py) — HealthChecker ходит через реальные туннели per-protocol; /onlines читает clientStats.lastOnline; DPI-алерты гейтятся на реальные когорты и тренд (2 цикла).

**Команды.** Ответы админ-команд всегда в топик источника (AdminHandlerBase._send); в группах CommandHandler заявляет только /admin; все панельные чтения в командах — через API-aware методы XUIService (xui.db на entry = None). Дрифт карт команд/справки ловят тесты TestCommandMapIntegrity.

### 27. Dashboard hardening + test strategy (2026-08-30 session)

A user-report ("получил ключ, но его нет в списке") unravelled into three latent dashboard bugs and a testing overhaul. PR #1 (`dashboard-hardening-e2e`) has the full story; highlights every agent should know:

- **ext_* (email-only) users**: no Telegram username; their real address lives in `contact_email` (`users.email` is the synthetic panel id `user_ext_…@nekovo.ru` — never mail to it). `/users`, `/find`, and the dashboard now surface `contact_email`. Provisioning goes ONLY through `AdminHandler._provision_email_user` / `/addmail` — a raw SQL INSERT into `users` once produced a zombie `chat_id=NULL` row (agent incident; the Hermes `user-ops` skill now forbids it).
- **Dead confirm modal**: `hideModal()` nulled `modalCallback` before the confirm handler called it — EVERY confirm-gated dashboard action (paid/ban/approve/reject/revoke/reset) silently did nothing, since the repo-root merge. Fixed in app.js; pinned by Playwright E2E.
- **Stale-snapshot rollback**: `handle_user_action` side effects `save_user()`d a pre-transition user snapshot (full-row write) — reject rolled back to pending_demo, ban/unban reverted for keyed users. Side effects now re-fetch; the reset branch revokes BEFORE `set_state` (the order the Telegram commands always used).
- **`grant_paid` is a real billing grant** (transition + subscriptions row + `grant_paid_access` in the main handler, not in the notification side-effect path which is skipped when NotificationService is down). The detail modal has its own ⭐ paid button (list-card buttons are invisible to mobile admins).
- **Test strategy (4 levels)** — each catches a class the others can't: unit+mutmut (function logic) / `tests/integration/test_web_actions_integration.py` on REAL sqlite (cross-layer row semantics — mocks can't see stale-row overwrites by construction) / `tests/e2e` Playwright (dead frontend JS) / deploy sha-smoke in `deploy_to_entry.sh` (repo-vs-prod drift). Trust a new suite only after re-introducing the bug and watching it fail.
- **Hermes `/ai`**: model switched to `minimax/minimax-m3:free` with a `fallback_providers` chain (nemotron-3-super), skills deploy via `scripts/deploy_hermes_skills.sh` (placeholder substitution; hand-rsync caused drift), watchdog defers restarts while an /ai request is in flight.
- **Cleanup**: removed the embedded 930MB ChatDev clone (authored workflow preserved in `docs/archive/`), `.archive/`, `scratch/`, mempalace artifacts, `htmlcov`/`mutants`; `AGENT.md` and `PROJECT_CONTEXT.md` deleted (stale — their live content moved here).

### 28. Agent runbook, alert→agent diagnosis, free-model guard (2026-09-05 session)

Triggered by "the agent answers nonsense": asked which protocol is down it walked ports/iptables/containers for 105 s and said "all alive" — the exact checks that were green throughout the 4-day Reality outage. What landed (PR #4, stacked on #3):

- **`scripts/protocol_healthcheck.py`** (runs as root on entry, ~15 s): three isolated layers (bot db probes + panel field audit / entry NAT+systemd / exit runtime over one ssh: xray users per inbound with flow counts, config.json, accepted per inbound, hysteria units, hop REDIRECTs, Reality dest cert record size). Pure `assess()` → OK/DEGRADED/DOWN/UNKNOWN per protocol, ranked suspects with the exact next command, exit 0/1/2 — **2 = "could not look", never a pass**; a BROKEN panel audit alone is DEGRADED (the audit is an earlier signal than the probe, which runs from one client of ~80). hy2t is judged from probe rows when they exist, from layer C alone otherwise.
- **STEP 0 rule** in vpn-ops / incident-response / hermes AGENTS.md: run the healthcheck first, reason only from its ИТОГ. SYSTEM_PREAMBLE got an output contract (result first, no process narration, real line breaks) and the marker tuples learned "протокол / probe / hy2 / stls / упал / лежит". Same question afterwards: 32 s, correct (named the live ws degradation), asked before restarting.
- **Alert → agent**: `protocol_down:*` kicks Hermes on a daemon worker (one turn per key; tick never held), diagnosis stored in `alert_history.kimi_analysis` **and** posted to the alert's topic. DPI kicks moved off the tick too (`_spawn_agent_worker`, `DPI_AGENT_MAX_CONCURRENT = 2`, excess skipped not queued). `send_message` returning None now counts as not delivered → PM fallback for criticals (it used to vanish silently).
- **New keys**: `protocol_down:<tag>:degraded` (warn, ok < 25 % over ≥25 rows — ws sat at 2/30 for an hour on 2026-09-05, a CF-path slowdown with no user impact: real users' accepted rate on inbound-2053 was steady and there was no reconnect storm; a plain HEAD on the CF host always returns 520 — that is normal for an httpupgrade origin, not evidence). `/protocols` admin command = the same view without an LLM.
- **Free-model guard** `scripts/hermes_model_guard.py` (30-min timer via `deploy_hermes_host.sh`): key usage growth > $0.001 → critical; primary paid → promote the first free fallback (old primary dropped from the chain, config backed up, restart deferred while `/ai` turns are in flight); primary MISSING → only on the second sighting (`:free` ids blink); blind (API/model list unavailable) → no decisions, warn after 3 misses; nothing free → critical, no rewrite. Notifies TOPIC_AI via the bot's HTTPS_PROXY, pending queue survives a dead Telegram.
- **Probe coverage for hy2t** (:18085, emitted only with `HY2T_PORT`; single port table `HealthChecker.probe_ports_for` shared with `gen_probe_config.py`) and **retention for `outbound_health`** (30 d, daily, 20k-row batches with per-batch commit; ~200k rows pruned on the first run).
- **Process lesson, twice**: never `git checkout -- file` to undo a mutation test — it erases every uncommitted change in the file (it wiped three builders' work once; recovered by replaying their Edit calls from the subagent transcripts). Undo a mutation with the reverse text replacement.

### 29. DPIMonitor — the feedback loop is closed (2026-09-06 session)

IMPROVEMENT_PLAN A1, the project's #1 open item since July: telemetry was collected (`dpi_metrics`, probes, hy2-auth log, "не работает" reports) but nothing acted on it — the operator hand-edited `app_settings` JSON. What landed:

- **`bot/services/dpi_monitor.py::DPIMonitor(db, config, bot=None)`** — pure core `evaluate(signals, state, now) -> (new_state, changes)` with NO I/O (that is what the tests drive), thin `collect_signals` / `apply_changes` around it, `run_once(dry_run=False) -> list[Change]`. `Change = (scope global|asn, target None|'AS…', protocol, action demote|restore, reason, evidence)`. Scheduled from `NotificationService.start_scheduler` (`IntervalTrigger(minutes=10)`, id `dpi_monitor`, `replace_existing=True`); exceptions logged, never raised. Settings: `DPI_MONITOR_ENABLED` (default `1`), `DPI_MONITOR_INTERVAL_MIN` (10); runtime switch `app_settings.dpi_monitor_enabled` — when `0` the job logs and exits, state untouched.
- **The only action is "move to the end"**, never remove/disable. `MyKeyAnswerHandler.get_cascade_order` takes the operator's order (ASN override > country > global enabled list), then stable-partitions it by `get_auto_demotions(db, asn)` (union of global + that ASN's entries; bad JSON → empty set): non-demoted first, demoted after, tier filter last. The operator's `cascade_by_asn` / `cascade_protocol_order` always decide the BASE order; auto only reorders inside it. `apply_auto=False` gives the dashboard editor the raw operator order.
- **Storage** (JSON strings in `app_settings`): `cascade_auto` = EFFECTIVE demotions only — `{"global": {"<proto>": {"since", "reason", "evidence"}}, "asn": {"AS31133": {"<proto>": {...}}}}` (`reason` = the rule id, e.g. `probe_dark`; `evidence` = the human line, e.g. «пробы 0/30 за 3 прогона…» — surfaces should print `evidence` and fall back to `reason`); `dpi_monitor_state` = `{"targets": {"global:ws": {"bad", "good", "last_change", rule}, "asn:AS31133:reality": {...}}, "last_run", "runs"}`. Do not hand-edit either — `/cascade reset` clears both and logs the action.
- **Rules → target.** R1 probe DARK (zero alive rows across the last 3 runs, ≥15 samples) → demote globally. R2 probe DEGRADED (ok/len < 0.25 over ≥25 samples) → globally. Both use the SAME numbers as the pager — `PROBE_RUNS / PROBE_MIN_SAMPLES / PROBE_DEGRADED_OK_RATIO / PROBE_DEGRADED_MIN_SAMPLES / PROBE_STALE_MIN` — but those are function locals of `alert_manager.build_default_checks` (not importable), so `dpi_monitor.py` carries a verbatim MIRROR with a cross-reference comment on each side: change one, change the other, or the monitor and `protocol_down:<tag>[:degraded]` start disagreeing about what "dark" means. R3 Reality per ASN: `dpi_metrics` rows with `inbound_tag='reality'` for that ASN over 2 h, `handshake_fail ≥ 30 AND ≥ 2 × conn` → demote `reality` for the ASN. R4 hy2 storm: one chat_id with ≥30 `allow` rows in `hy2_auth_log` over 2 h and a known `users.last_asn` → demote BOTH `hy2` and `hy2t` for that ASN (UDP throttling hits both instances). R5 failure reports: ≥2 distinct chat_ids from one ASN in 6 h → demote the CURRENT HEAD of that ASN's effective order (head after existing auto-demotions), bounded to 2 R5 demotions per ASN.
- **Hysteresis / guards.** Demote when the target's `bad` streak reaches 2 consecutive evaluations (~20 min), restore when `good` reaches 6 (~1 h); ≥30 min between changes of one target; ≤2 changes per run, ranked DARK > DEGRADED > R3 > R4 > R5, restores after demotes. "Good" = the rule that demoted it is quiet this evaluation — a demotion is restored only by its own rule (rule id stored in state). **All probed protocols dark at once = do nothing** (upstream outage; `protocol_down:all` already pages, reordering is noise). Probe pipeline stale (newest `outbound_health` row > 45 min old) = skip R1/R2.
- **Visibility.** Every applied change → `db.log_admin_action('dpi_monitor', 'cascade_auto_demote'|'cascade_auto_restore', target='<scope>:<asn|global>:<proto>', details=reason+evidence)` plus ONE HTML message per run to `FORUM_GROUP_ID` / `TOPIC_AI` ("🔁 Каскад: ws → в конец (глобально) / Причина: пробы 0/30 за 45 мин (DARK) / Отменить: /cascade reset"). Never a PM; `bot=None` skips the send. `/cascade` (effective global order with tier tags, auto-demoted set with since/reason, per-ASN entries, on/off, last run), `/cascade AS31133`, `/cascade reset`, `/cascade on|off` — registered in `ADMIN_COMMANDS` + `ADMIN_HELP_TEXT` (drift pinned by `TestCommandMapIntegrity`); dashboard `GET /api/admin/cascade_order` gained `"auto": {enabled, global, asn, last_run}` and the editor shows "⚠️ авто-понижены: ws (пробы 0/30, с 14:03)".

**Calibration that set the thresholds (all read from prod, read-only):**
- Probes: 10 rows/protocol every 15 min, 7/10 ok is normal (vk/yandex/sberbank fail through a foreign exit). Liveness = `latency_ms IS NOT NULL OR status='ok'`.
- Reality per ASN: Reality users reach exit with their REAL IP (PROXY protocol via entry haproxy), so `dpi_metrics` rows with `inbound_tag='reality'` and an ASN ARE that operator's users. MegaFon AS31133: 7-day totals **903 handshake-fails / 39 conns**, of which **879 hs / 0 conn on 2026-09-01** (the flow-wipe outage day, §28); every other ASN sits at a ratio ≤ 0.1. Hence R3 = `hsfail ≥ 30 AND ≥ 2×conn` over 2 h. `cf-ws` / `ss2022` rows collapse into `country='*TUNNEL*'` (entry MASQUERADE) with no ASN, and their hs-fails are mostly background probing (ss2022 ~1–2k/day vs 7–17k conns) — **never use `*TUNNEL*` rows for per-ASN decisions**.
- hy2 storms: normal is **4–8 allows/day/user**; healthy heavy users peak ~44/day; ziriki on throttled mobile UDP (2026-09-01/02, same client as the §25 LTE case) produced **108–128/day with 17–25/h bursts**. Hence R4 = ≥30 allows in 2 h. `hy2_auth_log.asn` is NULL on every row (the auth call comes from the hysteria server on exit, src = entry), so a storm is attributed to `users.last_asn` of that chat_id.
- Failure reports: **2 rows in 30 days** — a weak signal, hence R5 needs ≥2 distinct users from one ASN inside 6 h and is ranked last.
- Coverage limit worth remembering: **53 of 85 active users have `users.last_asn` NULL** — they have not fetched `/sub` since geo landed. Precisely what that costs: `/sub` resolves the ASN from the request IP at fetch time (and writes it back to `last_asn`), so a per-ASN demotion reaches anyone who fetches `/sub` from that ASN — unless the request comes from one of OUR addresses (`WebAppServer._is_own_address`: ENTRY/EXIT_NODE_IP, HY2_HOST and FALLBACK_NODE_HOST when they are IPs, loopback/RFC 1918/docker/link-local/CGNAT/ULA; a refresh through the tunnel leaves by a node, a docker hairpin arrives as 172.x): then nothing is resolved or written (no `last_*`, no `sub_fetches` row, whose max(ts) failure reports print as the age of the stored network) and the profile is ordered by the stored network. Until 2026-10-10 such fetches stamped the hoster's ASN on the user; the NULL cohort simply does not fetch, so for them only the key card counts (which reads `last_asn` → NULL → global demotions only), and an R4 storm from one of them cannot be attributed to an ASN at all (the rule joins on `last_asn`). `short_session_count` in `dpi_metrics` is always 0 (not computed). Both are A1.2 in the plan, not bugs in the monitor.

**Design rules (why it looks the way it does):** never remove a protocol, only reorder — a wrong decision costs latency, not connectivity; the operator's explicit override always wins on the base order, and `/cascade reset` undoes everything in one command; hysteresis on both edges so a single bad probe run cannot flap the order users see; the upstream-outage guard because "everything is dark" is a link/exit incident that already pages and reordering would only add noise; every change is written to `admin_actions` and posted to the AI topic because an invisible auto-action is indistinguishable from a bug. Tests are level-2 on a REAL sqlite (`Database(tmp_path/...)`) seeding `outbound_health` / `dpi_metrics` / `hy2_auth_log` / `users` / `app_settings`; each rule and the hysteresis are pinned and mutation-checked by reverse text replacement (see the §28 lesson — never `git checkout`). Python 3.11-compatible (prod image).

### 30. Lockdown mode + shutdown auto-detect (2026-09-06 session)

IMPROVEMENT_PLAN B1 + B5 — the whitelist scenario (regional shutdown / "sovereign internet": the network passes ONLY allowed destinations; direct connections to our entry IP die, CDN-fronted traffic survives). Two halves, both layered on mechanisms that already existed (cascade, DPIMonitor, AlertManager) — no new job, no new table.

- **`bot/services/lockdown.py`.** `LOCKDOWN_ORDER = ('ws', 'stls', 'reality', 'hy2', 'hy2t')` — fronted first, TCP-direct next, UDP last (UDP is the first thing to die under throttling or a whitelist); operator override `app_settings.cascade_lockdown` (JSON list; unknown names dropped, empty → default). `DIRECT_PROTOCOLS = ('reality', 'hy2', 'hy2t', 'stls')`, `FRONTED_PROTOCOLS = ('ws',)`. State in `app_settings.lockdown_mode`: `{"mode": "auto"|"on"|"off", "active", "since", "by": "admin:<id>"|"auto:probe_signature"|"system", "reason", "streak_on", "streak_off", "last_change"}`; missing/bad JSON → mode `auto`, inactive. `load_lockdown` / `is_lockdown_active` never raise (every `/sub` calls the latter); `set_mode(db, mode, *, by, reason)` — `on` → active now, `off` → inactive, `auto` → keep `active` as is and hand control back to the detector; `apply_lockdown_order(ordered)` is a stable projection (LOCKDOWN_ORDER members first, in that order, then whatever is left in its existing order); `evaluate_lockdown(probe_signals, lstate, now) -> (new_lstate, Event|None)` is PURE — that is what the tests drive; `format_lockdown_html` renders the topic message.
- **Where it plugs in.** `get_cascade_order` gained a layer BETWEEN the operator base order and the DPIMonitor auto-demotion partition: `if is_lockdown_active(db): ordered = apply_lockdown_order(ordered)`; keyword-only `apply_lockdown=True` (the dashboard raw view passes False); tier filter stays LAST — so today paid = `ws, stls, reality, hy2, hy2t`, demo = `ws, stls, hy2`. `SubscriptionService.build_singbox_config(user, enabled_protocols, *, lockdown=False)`: under lockdown the DNS rules collapse to `[clash Direct → local]` and `dns.final = 'remote'` — every lookup goes through the tunnel (local resolvers are the first thing poisoned under a whitelist; the normal profile's DIRECT rule_sets `geosite-category-ru` / `geoip-ru` → local would hand the poisoned answer over before the proxy is ever consulted). Route rules are UNCHANGED on purpose — the RU-direct TCP bypass (+ QUIC:443 carve-out) is what keeps whitelisted RU sites working, and the UDP `calls` path is untouched. `build_links` / `build_xray_config` already follow the cascade order — no change (so `?format=links` / `?format=xray` / the key card get the lockdown ORDER but not the DNS profile — sing-box subscription only). `/sub` in `web_server.py` computes `lockdown = is_lockdown_active(self.db)` once per request and passes it.
- **Detector = a hook in `DPIMonitor.run_once`**, after its own evaluate/apply, on the SAME probe-signals object it already collected (no second SQL pass). Result kept as the instance attribute `lockdown_event` (None or the Event) after every run. Not `dry_run` + event → persist `lockdown_mode`, `db.log_admin_action('dpi_monitor', 'lockdown_auto_on'|'lockdown_auto_off', target='lockdown', details=evidence)`, ONE HTML message to `FORUM_GROUP_ID` / `TOPIC_AI` («LOCKDOWN включён автоматически: прямые протоколы (reality, hy2, hy2t, stls) не отвечают 20 мин+ (2 оценки подряд), CF-фронт (ws) жив. Каскад: ws, stls, …; DNS через туннель. Снять: /lockdown off · Зафиксировать: /lockdown on · Оповестить юзеров: /broadcast»; the restore message is the mirror). `dry_run` computes, writes/sends nothing. A collector failure that leaves the probe rules UNKNOWN freezes the lockdown streaks too. Caveat that follows from riding the tick: `/cascade off` (`dpi_monitor_enabled=0`) stops the lockdown detector as well — the operator who paused the monitor still has `/lockdown on`. The streaks live INSIDE `lockdown_mode`, not in `dpi_monitor_state` — `/cascade reset` wipes the latter and must not give the detector amnesia; lockdown has its own undo (`/lockdown off`).
- **The signature, and why it looks like this.** Signature = at least 2 DIRECT protocols measured, ALL measured DIRECT protocols dark (the same DARK as the pager and R1: zero alive rows over the last 3 runs, ≥15 samples, liveness `latency_ms IS NOT NULL OR status='ok'`), AND `ws` measured and NOT dark. The probe vantage is the ENTRY host — a RU VPS — going through probe-proxy to exit. Under a whitelist regime the entry→exit hop to a foreign IP dies for reality/hy2/hy2t/stls while `ws` (entry→Cloudflare→exit) survives: that split is the only thing the probes can see that separates "the region cut the foreign internet" from "our exit / link / sidecar is down". Hence `ws` alive is a HARD requirement, not a bonus: ws dark too = upstream outage (`protocol_down:all` already pages, DPIMonitor's all-dark guard already freezes) → streaks untouched, no event. "≥2 direct measured" because hy2t's probe exists only with `HY2T_PORT` and ONE dark protocol is a per-inbound fault (§28), not a shutdown. Hysteresis is asymmetric on purpose: `streak_on` reaches 2 (~20 min) → `auto_on` (only when mode == `auto` and not already active) — turning on is cheap, it costs latency (DNS through the tunnel, ws first), never connectivity; `streak_off` counts evaluations with ≥2 direct protocols alive and needs 12 (~2 h) → `auto_off` — a flapping shutdown must not flip users' DNS profile back and forth every 20 min. `auto_off` fires ONLY when `by` starts with `auto:`: an admin's `/lockdown on` is a decision and the detector does not second-guess it (mode `on`/`off` → the detector keeps counting streaks so `/lockdown` shows what it would do, but never touches `active`). Stale probes (newest row > 45 min) → streaks frozen, no event.
- **Operator surface.** `/lockdown` (mode, active, since/by/reason, streaks, effective orders for demo and paid, hint lines), `/lockdown on|off|auto` (`set_mode` with `by='admin:<chat_id>'`, each logged as `admin_actions('<admin>', 'lockdown_set', target=mode)`) — registered in `ADMIN_COMMANDS` + `ADMIN_HELP_TEXT` (`TestCommandMapIntegrity`). AlertManager check `lockdown:active`: while `lockdown_mode.active` → `Alert(severity='critical', min_cycles=1, title 'LOCKDOWN активен (<by>, с <since>)', detail = reason + '/lockdown off')`, None when inactive so the tracker heals; `REPEAT_COOLDOWN_S` makes it a reminder every 2 h while it stays on. It does NOT kick the agent (the alert→agent hook is keyed on the `protocol_down:` prefix) — it is a state reminder, not a fault. Dashboard: `GET /api/admin/cascade_order` adds `"lockdown": {mode, active, since, by, reason}` (tolerant of bad JSON); the cascade editor shows a red banner «LOCKDOWN активен (…): порядок ws, stls, …; снять — /lockdown off в боте» when active, a grey «lockdown: auto (детектор следит)» otherwise.
- **Deliberately NOT automated** (follow-ups, tracked in the plan): (1) broadcasting users — sing-box clients refresh `/sub` within ~6 h and urltest already avoids dead outbounds; the topic message says `/broadcast` and the admin decides; (2) switching `STLS_SNI` / Reality dest to a whitelisted domain — that is shadow-tls on entry (the handshake server must really answer that SNI) + HAProxy SNI ACL on entry + panel dest/serverNames on exit + the bot's `.env`, in lockstep, with the dest cert-size check from §23 — an infrastructure operation, not an `app_settings` toggle (B2); (3) a second CDN front (B3) — under lockdown `ws` is both the most valuable and the only front; (4) route rules / RU-direct — untouched by design (B5).
- **Tests** are level-2 on a real sqlite (`Database(tmp)`): detector (signature → 2 evaluations → `auto_on`; 1 → nothing; ws also dark → nothing; only 1 direct protocol measured → nothing; stale → frozen; `auto_off` after 12 healthy evaluations only when `by` starts with `auto:`; mode `on` pins active through healthy evaluations, mode `off` pins inactive through the signature), `set_mode` transitions, `apply_lockdown_order` incl. the `cascade_lockdown` override and unknown names, `get_cascade_order` under lockdown for demo/paid and with `apply_lockdown=False`, subscription DNS profile flips with route rules unchanged (`test_dns_config_structure` / `test_route_rules_structure` / `test_always_proxy_rule_sets` stay green), `/sub` passes the flag (mocked `db.get_setting`), alert emits/heals, `/lockdown` renders and transitions, API block tolerant of bad JSON. Every threshold/branch mutation-checked and restored by reverse text replacement (§28 lesson). Runbooks moved in the same change: `skills/incident-response` (all-dark fork: ws alive + direct dark ⇒ whitelist ⇒ check `/lockdown`; everything dark ⇒ upstream), `skills/vpn-ops` (ws-first order = lockdown, read `/lockdown` first), `hermes/AGENTS.md` (golden rule: never flip it without the admin's OK).

### 31. FlClash rule lists — the profile as a remote control (2026-10-08 session)

IMPROVEMENT_PLAN E1 + E2 + E9 (section E: PR #12). FlClash refreshes the PROFILE once a day but honours each `rule-provider`'s interval, so urgent routing lives in lists the Clash profile points at. Hiddify drops a sing-box profile's rules — the lists are Clash-only; the sing-box profile is byte-identical to before (504 combinations hashed).

- **`bot/services/rule_lists.py`** — `app_settings.rule_lists` = `{"<list>": {"<entry>": {ts, by, note}}}`: `always-proxy`, `blocked-recent` (domains → VPN), `ru-direct` (domains → DIRECT), `ru-direct-ip` (CIDRs → DIRECT, `no-resolve`). Validated on write AND on read — a provider mihomo cannot parse breaks the whole profile: URL → host, lower-case, punycode, no `www.`, ≥2 labels, real TLD, no IPs in domain lists; CIDR → network, nothing broader than /8 (v4) or /16 (v6). Dedup incl. parent / supernet; ≤1000 per list. Writers read STRICTLY (a locked SELECT taken for "no row" would overwrite the lists — the DPIMonitor._read_setting hazard); bad JSON goes to `rule_lists.bak` before a write starts over. Audit: `rule_list_add|rm|ignore|auto_threshold` (actor = the typing admin), E9 = `('rule_lists', 'auto_add')`.
- **`GET /lists/clash/<name>.yaml`** (public, like /sub): `payload:` of `'+.domain'` / CIDR, empty = `payload: []`; `text/yaml`, `max-age=300`; 404 unknown name; **503 while the store is unreadable** — a failed fetch keeps the client's cached copy, an empty 200 would wipe it for an hour. Not `/rule-sets/` (the .srs mirror).
- **Profile** (`build_clash_config`, only with WEBAPP_URL): `rule-providers` (http, yaml, interval 3600, `./lists/<name>.yaml`); VPN lists right before the always-proxy geosites, DIRECT lists right before `GEOSITE,category-ru`, `ru-direct` also in the RU QUIC:443 carve-out. mihomo fetches providers THROUGH its own rules (`mihomo --> host match Match using VPN`), i.e. via the tunnel; a fetch from the user's IP (E12 per-operator monitoring) would need `proxy: DIRECT` on the providers.
- **`/list`** — overview (sizes, queue, threshold, ignores), `/list show|add|rm <list> <entry>…`, `/list auto N|off`. `/list rm` from a VPN list keeps E9 off that domain for 30 days, or the next two complaints would undo the operator.
- **«📝 Другой сайт»** (last row of the 🆘 picker, `bot/handlers/callbacks/rule_lists.py`) → `PENDING_SITE` (10 min, arming it disarms PENDING_EMAIL and vice versa) → up to 3 host names from the next plain text → `app_settings.rule_list_queue` (`{next_id, items:[{id, domain, chat_id, ts, status}], ignored:{domain:{ts, by}}}`; 5/user/24 h, 7-day retention) → card in TOPIC_SUPPORT with «➕ в blocked-recent» / «✖ игнор» (`rlq:add|ign:<id>`, SUPER_ADMIN only — callback data is client-supplied). NOT written to `user_failure_reports`: DPIMonitor R5 would read a blocked site as a failing protocol.
- **E9** (`decide_auto_adds`, pure): ≥ `rule_list_auto_threshold` (default 2; `0`/`off` = off) DISTINCT users with pending complaints about a domain in 24 h → `blocked-recent` + a topic message with `/list rm blocked-recent <domain>`. Evaluated right after each complaint, for that domain only (a threshold change never floods the list from the backlog). Never over: a VPN-list cover, `ru-direct` (operator wins), an admin's ignore < 30 days, RU-zone TLDs `.ru/.su/.рф` (the cure there is usually ru-direct; two demo accounts must not push vk.ru / banks through the exit for everyone).
- Tests: `tests/integration/test_rule_lists.py` + `test_rule_lists_bot.py` (real sqlite, 173); 49 mutations killed by reverse text replacement. mihomo v1.19.32: `-t` + real runs fetching the providers from the bot.

### 32. FlClash profile channels — client-side cascade, own probes, emergency/mirror providers (2026-10-08)

IMPROVEMENT_PLAN E3/E4/E5/E6/E20, Clash profile only (`build_clash_config`); the Hiddify sing-box profile, links and xray formats stay byte-identical (checked over 768 sing-box combinations + the /sub handler).

- **Groups.** `VPN` (select) = `Cascade, Auto, <every server>`; `Cascade` (fallback, the default) = the EFFECTIVE cascade order from `get_cascade_order` (lockdown projection and DPIMonitor demotions already applied), DE reserve last; `Auto` (url-test) = the same set; `Calls` unchanged. `/sub?format=clash` passes `get_auto_demotions(db, request ASN or last_asn)`: demoted protocols leave Cascade/Auto (provider copies too, via an end-anchored `exclude-filter`) and stay in VPN/Calls. A demotion of EVERY cascade protocol excludes nothing (the reserve alone would be left).
- **Health checks.** Cascade / Auto / Calls check `https://www.gstatic.com/generate_204`, like the sing-box profile. Группы — gstatic, чтобы DE не считался мёртвым при падении exit; телеметрия по группам вернётся с проверочной точкой на entry, E25. `/probe/<token>/<provider>` is the providers' health check only — telemetry, never a failover input (mihomo tests a provider's proxies against its own url AND its groups' url; the groups decide on gstatic). The endpoint answers 204 BEFORE any lookup (the client times that answer — never make it wait for sqlite) and a background task writes ≤1 `client_probe(chat_id, grp, ts, src_ip)` row per (user, provider) a minute; the segment is a closed set (`SubscriptionService.probe_groups()`: `emergency` + one `mirror-<n>` per configured mirror); `src_ip` is our egress (exit / DE), not the user. mihomo tests with **HEAD**; no `expected-status`, so any HTTP answer = alive. Retention: 30 days, daily, batches of 20k with a commit per batch (`NotificationService._cleanup_client_probe_sync`, job `client_probe_cleanup`, the outbound_health pattern); `ts` is sqlite's `CURRENT_TIMESTAMP` with a SPACE, so the cutoff string is formatted the same way — an isoformat cutoff would drop up to a day too much.
- **proxy-providers.** `emergency` = `{WEBAPP_URL}/sub/<token>?format=clash-proxies&channel=emergency`, interval 600 (E20); `mirror-<n>` per `SUB_MIRROR_URLS` entry = `<mirror>/sub/<token>?format=clash-proxies`, interval 3600 (E5); health-check = their `/probe` url, interval 600. VPN/Cascade/Auto `use` them after the profile's own servers. **`proxy: DIRECT` is mandatory** (unlike §31's rule-providers, which may ride the tunnel): without it mihomo routes a provider fetch by the profile's rules (`MATCH,VPN` → the tunnel; verified on mihomo 1.19.32), i.e. the channel dies with the servers it must replace. `override.additional-prefix` (`[E] `, `[M1] `) — same names as the profile otherwise, and a select group resolves a pick by name.
- **`?format=clash-proxies`** = only `{"proxies": [...]}` (same converter, ordered by the request's network), read-only: no last-geo write, no `sub_fetches` row ("/sub age" stays the PROFILE age), no reserve provisioning, no panel quota read; `cache-control: no-store`; `channel` is only logged. A mirror must reverse-proxy that path to this bot.
- **Not done yet:** DIRECT fetches to foreign hosts fail under a whitelist (a RU-hosted mirror, E25, covers it).
- **Test trap (Python 3.12):** `gather()` over already-finished tasks completes without yielding — a `while tasks: await gather(*tasks)` helper spins forever if the discard callbacks are still queued. Wait on PENDING tasks only.

### 33. SOS channels: /sos, SOS by mail, offline kit, /share, /nudge_sub (2026-10-08)

IMPROVEMENT_PLAN E19 / E24 / E27 / E29 + the A1.2 nudge. Premise: whatever a user needs during an outage must reach them BEFORE it — during one there is nothing left to hand it out through. Everything lives in `bot/services/sos.py` (`SosService` + pure text/format helpers); the command, callback, admin and mail handlers only delegate.

- **Emergency profile.** `/sub/<token>?emergency=1` (exactly `1`) forces the lockdown profile for that one response, whatever `lockdown_mode` says: `get_cascade_order(..., force_lockdown=True)` (the `apply_lockdown_order` projection; wins over `apply_lockdown=False`; DPIMonitor demotions still sink after it; tier filter last) and `lockdown=True` for the DNS profile of both `build_singbox_config` and `build_clash_config`. Headers: `profile-title: NekoVPN SOS`, Clash file `NekoVPN-SOS.yaml` — the emergency link is added NEXT TO the main subscription, and the names keep them apart. Ignored on `?format=clash-proxies` (§32's provider refresh is a bare server list for the profile holding it, never a profile of its own). No parameter or any other value → byte-identical to before (checked on 480 combinations status × lang × platform × lockdown × cascade_auto × format incl. `clash-proxies` × ASN against origin/main after §32; pinned by `test_normal_profile_is_byte_identical`). Hiddify drops a profile's DNS/route anyway (memory note) — the emergency profile is fully effective in FlClash, and the texts say so.
- **/sos** (active key = demo/paid/support_topic + uuid, else «Сначала получите ключ»). The user gets: both emergency links (FlClash `?format=clash&emergency=1`, Hiddify `?emergency=1`; iOS — Karing and the sing-box link only), how to refresh the main subscription, and «что сейчас работает»: our probes judged by `admin/ops.probe_verdict` over `read_probe_state` — the /protocols judgement, extracted so the card and the user text cannot disagree (stale probes → "no fresh checks", never a stale verdict; only the user's tier protocols, in emergency order) — plus "your network": other users with the same `users.last_asn` seen in `user_presence` / `hy2_auth_log` within 60 min, DPIMonitor demotions for that ASN, other users' reports in 6 h, the AS org from `dpi_metrics`; empty `last_asn` → «обнови подписку». Buttons: download row (not on iOS), `sos:kit`, `sos:share`. Operator side: a `user_failure_reports` row with target `sos` (counted by DPIMonitor R5) and one HTML post to `FORUM_GROUP_ID` / `TOPIC_SUPPORT` (where «не работает» reports go): `🆘 SOS #id · /sos` + the `MyKeyAnswerHandler._report_facts` / `_format_report_facts` lines (the same collector, not a second one) + emergency cascade + DPIMonitor + lockdown + network + agent status. Limit: one per 10 min per chat_id — the `sos` row IS the limiter (survives restarts); a repeat gets a one-liner and no report.
- **Agent diagnosis for /sos.** `SosAgent` subclasses `AlertManager` and reuses `_spawn_agent_worker` / `_kick_agent` as is (daemon worker, one turn per key `sos:<chat_id>`, error containment). Differences: its own `Semaphore(1)` (the excess is skipped, the report says «агент занят» — during a mass outage everyone presses /sos and protocol_down kicks its own turns), the reply goes to the SUPPORT topic (never a PM), and a gate — the worker asks the agent only after the report is posted (30 s cap), so the diagnosis cannot land above the report. The prompt pins `protocol_healthcheck.py` as the first action, read-only, ИТОГ / ПОДОЗРЕВАЕМЫЙ / ЧТО ОТВЕТИТЬ / СЛЕДУЮЩАЯ КОМАНДА; routing pinned to vpn-ops (no incident / code-review / billing markers — hence "/sub" rather than «подписка», tier «платный/демо»).
- **SOS by mail (E24).** `MailIntakeService._process_message` → `_handle_sos_letter`, BEFORE the request path (whose one-open-request rule would swallow it): sender == `contact_email` (trimmed, case-insensitive) of an active key holder — never `users.email` — AND a trigger in the subject or body: `sos` / `help` as whole words, `не\s+работает`, `не\s+подключается`. The body (first 64 KB, `BODY.PEEK[]<0.65536>`) is fetched only for key holders, with quotes stripped (`>` lines; Gmail / Yandex / Mail.ru / Outlook headers; HTML blockquote / gmail_quote): every reply quotes our key letter, and it says «Не подключается?». The answer (subject «NekoVPN: аварийная подписка», `In-Reply-To` set via `EmailService.send_notice(..., in_reply_to=)`) carries the links, refresh steps and the status lines, and contains no trigger word (pinned) — a reply to it cannot loop. Dedupe one per hour per address via an `email_requests` row with status `sos` (also the audit trail); one Message-ID is never answered twice. Report target `sos_mail`. No agent kick from mail (spec). Unknown sender / no trigger / no active key → the old card path, unchanged.
- **/kit (E27).** Two documents through `Bot.send_document(chat_id, filename, content, caption=None, parse_mode=None)` → `TelegramClient.send_document_bytes` (multipart through `_request(_files=...)`: the same retries and `TG_PROXY_URLS` pool). The old path-based `send_document` (the agent's `[[SEND_FILE]]`) now delegates there — it used to be a bare `session.post` that skipped the pool, i.e. a direct upload, which cannot work from entry. Files: `NekoVPN-emergency.yaml` (exactly the `/sub?format=clash&emergency=1` text for the user's stored network — JSON is YAML; `demoted` from `get_auto_demotions(last_asn)` like the /sub clash branch, so §32's Cascade/Auto `exclude-filter` matches; pinned by `test_kit_yaml_is_what_the_emergency_link_serves`) and `NekoVPN-emergency.json` (sing-box, emergency). Built and sent on a worker; paid users are provisioned on the DE node first (`ensure_client`, as /sub does). One per 10 min (in memory; a failed upload clears the mark). Hiddify 4.1.x has NO «Файл» in its "+" sheet (QR / «Буфер обмена» / «Вручную»; File exists only in unreleased main) — the text says «+ → Файл», else copy the .json text into «+ → Буфер обмена». The Clash file carries the §31 rule-providers and the §32 proxy-providers (`emergency`, `mirror-<n>`): offline, mihomo starts anyway (verified under `docker --network none`: the config loads, provider fetches fail as non-fatal errors) and fetches them once a path is up. `test_kit_providers_load_in_mihomo` (`requires_docker`) serves the kit's providers from the bot's own WebAppServer on 127.0.0.1 (main + a mirror) and asserts via mihomo's controller that both proxy-providers load DIRECT with the `[E] ` / `[M1] ` copies and that Cascade drops the demoted protocol's copies. Validated by `mihomo -t` (needs geodata — the test caches it under `$TMPDIR/nekovpn-test-cache`; markers `requires_docker` + `requires_network`) and `sing-box check` 1.11.15 on the profile minus `tls.fragment` (1.11 predates the field; clients' cores take it; `requires_docker`).
- **/share (E29)**, text only: FlClash «Инструменты → Общие → Входящие → Разрешить LAN», «Порт» 7890 (mixed); the device IP from the network settings; the proxy on the second device (Android Wi-Fi → Прокси: Вручную; Windows Параметры → Прокси; iPhone, as a client only, Wi-Fi → (i) → Настройка прокси → Вручную); turn Allow LAN off afterwards — no auth, the whole Wi-Fi can use it.
- **/nudge_sub [go]** (admin, A1.2): active (demo/paid/support_topic) with a uuid and a blank `last_asn`. Preview = count, Telegram vs mail-only (`ext_*`, not messaged), a sample of 10, the last run; `go` → worker, `nudge_text` ru/en, 50 ms apart, `admin_actions(<admin>, 'nudge_sub', '<sent>/<total>', 'sent=N failed=M')`, the result posted back to the topic.
- **Registration.** `CommandHandler.COMMANDS` /sos /kit /share + /help (ru/en) + `Bot.USER_COMMANDS`; `SosCallbackHandler` (`sos:` prefix) in the dispatcher — files always go to the PRESSER (`user_id`), never into a group; `ADMIN_COMMANDS['/nudge_sub']` + `ADMIN_HELP_TEXT` (user commands are mentioned there without `<code>`: the drift test reads `<code>` mentions as admin commands).
- **Tests:** `tests/integration/test_sos_channels.py`, `tests/integration/test_sos_mail.py`, `tests/unit/test_telegram_send_document.py`, real sqlite. 30 mutants — the three limits at their boundaries, every trigger word, word boundaries and whitespace, subject/body checks, quote stripping, status/uuid gates, `force_lockdown`, the emergency parse / DNS / order and its `clash-proxies` exemption, the kit's `demoted`, nudge filters and delay, the agent gate, the upload's proxy pool — all killed, restored by reverse text replacement (§28).

### 34. Per-protocol client telemetry (E8) → DPIMonitor R6 `client_dark` → reverse SOS (E21) (2026-10-09)

IMPROVEMENT_PLAN E8 + E21; the base the C2 dashboard reads. Clash profile only: the Hiddify sing-box profile, links, xray, `clash-proxies` without `only` and every handler-level `/sub` answer are byte-identical (11 952 hashes vs origin/main; the Clash profile compared minus the `p-*` providers).

- **Contract (shared with C2).** `client_probe` row with `grp = 'p-<proto>'` (`reality` / `hy2` / `hy2t` / `ws` / `stls`, or `de` = the reserve) = "client `chat_id` got through `<proto>` at `ts`". No `p-<proto>` row while the same client has rows for other protocols in the same window = `<proto>` does not work for it. The client's network = `users.last_asn` (its last PROFILE fetch; provider fetches stay read-only, §32). `emergency` / `mirror-<n>` rows are channel heartbeats, not protocols. ≈6 rows/protocol/hour per running client.
- **Profile** (`_clash_telemetry_providers`): one proxy-provider `p-<proto>` per protocol the profile holds (cascade order, `p-de` last), `?format=clash-proxies&only=<proto>`, interval 3600, `proxy: DIRECT`, health-check `/probe/<token>/p-<proto>` every 600 s, `lazy: false`, prefix `[P] `, in NO group's `use`. mihomo 1.19.32 loads an unused provider and, with `lazy: false`, checks it at load and on every interval (default `lazy: true`: at load only, "Skip once health check because we are lazy") — so no extra group. `?only=` = that protocol's server alone (`de` = reserve), anything else `{"proxies": []}`. `/probe` accepts `p-<proto>` for `telemetry_protocols()` = what the profile's own builders produce with this config (+ `de` with the reserve) — config-only, before the 204.
- **R6 `client_dark`** (2 h, per `last_asn` × proto): `dead` = clients with a live row through ANOTHER cascade protocol and none for proto, proto offered to them (active status, tier, the operator's enabled set — hy2t off since 2026-09-21 — built here); `alive` = clients with a proto row; `dead ≥ 3 AND alive == 0` → demote for that ASN. Silence is never a signal: only a live CASCADE row counts (no rows = closed app / sleeping phone / exit down; `de`-only = not the cascade). Hysteresis 2/6, gap 30 min, cap 2/run, rank DARK > DEGRADED > `client_dark` > R3 > R4 > R5 (rank also picks the owner when R6 and R3 both hit reality@ASN); restored when its own (ASN, proto) is quiet; collector failure / all-dark → frozen. Slow on purpose (twelve straight failed checks). Not calibrated on prod data — revisit.
- **E21** (`bot/services/reverse_sos.py`, after `apply_changes`; not in dry_run / without a bot): ONLY applied `client_dark` demotions write to users (R3 also fires on an operator's own scanners — AS31205; R4/R5 are one storm / two complaints). Recipients: that ASN, active key, a `p-<proto>` row in 24 h. Telegram «В вашей сети перестал работать … обновите профиль» + FlClash emergency link (`sos.emergency_urls`) + /sos, ru/en, one message per run; `ext_*` → letter to `contact_email`, no mail-SOS trigger words. One per chat_id per 24 h: `app_settings.reverse_sos_sent` `{chat_id: iso}`, strict read (unreadable → skip), claimed BEFORE sending. Daemon thread, 50 ms apart; `admin_actions('dpi_monitor', 'reverse_sos', 'asn:<ASN>:<proto>', 'recipients=… telegram=… mail=… failed=… cooldown=… unreachable=… selected=…')` per demotion + one line in TOPIC_AI.
- **Tests:** `test_client_telemetry.py` (profile, `only=`, `/probe`, a `requires_docker` mihomo load check), `test_client_dark.py` (R6 + E21), real sqlite; 49 mutants killed by reverse replacement (§28). Live (`/tmp` harness): the bot's profile in mihomo 1.19.32 against sing-box 1.11.15 serving every protocol on 127.0.0.1 + the real WebAppServer — every `p-*` health check reaches `/probe/<token>/p-<proto>` through its own protocol; hy2 server removed → R6 demotes hy2@AS31133 on the 2nd evaluation → E21 writes to the 3 users.

### 35. Client health dashboard — protocol × operator, seen from the clients (2026-10-10)

IMPROVEMENT_PLAN C2 (the rest), on E8's data (`client_probe`, §32). API + dashboard only; the bot command is another branch's.

- **`GET /api/admin/client_health?hours=1|6|24|168`** (default 24, anything else → 400; admin via initData / admin_token like the other Signals reads) → `bot/services/client_health.py`: ONE query over client_probe (a row per (client, grp) in the window, `ts >= now-hours` in the table's own SPACE format, joined to `users`) + the cascade setting + a pure `summarize()` — never a query per operator / protocol. Per `users.last_asn` (NULL / blank / no users row → `asn: null`, «неизвестно», listed last): `clients` (any row, channels too), `probed`, per protocol `alive` / `dead` / `rate = alive/(alive+dead)` (null = no data) / `last_ts`; `protocols` = seen in the window, order reality, hy2, hy2t, ws, stls, de; `total` over every client; caps 20k pairs (`truncated`) / 200 rows (`rows_total`). ~0.2 s on a 30-day 2.4M-row table (covering-index scan, whatever the window).
- **dead = rows on other protocols in the window, none on this one, AND the client's profile carries the protocol** (`offer_rules`: an active status — demo / paid / support_topic, /sub 410s the rest; enabled in the operator's cascade; the tier — `MyKeyAnswerHandler.PROTOCOL_TIER` / `PAID_USER_STATUSES`, DE reserve = `FALLBACK_ALLOWED_STATUSES`; unknown protocols — every active client): a demo profile has no reality / hy2t / de, and without the gate every paid-only column reads as the paid share of the base. Channels (`emergency`, `mirror-<n>`) count a client, never a protocol, never a failure. A DE success counts as "up", so a client alive ONLY via the reserve is dead on every main protocol — the "cascade cut for this operator" (entry IP blocked) picture; a DPIMonitor rule asking whether REORDERING helps may ignore such clients, but alive and the gate must mean the same there (two definitions drift like the §29 probe mirror).
- **Dashboard**: Signals tab → «📶 Клиенты по операторам» under the ASN heatmap: total row first, `%` + `alive/n` per cell, bands ≥ 0.75 green / ≥ 0.25 yellow / < 0.25 red / null grey — `CLIENT_RATE_GOOD` / `CLIENT_RATE_BAD` in app.js are the only copy; window select 1ч/6ч/24ч/7д (= the API's set, pinned); a late answer for an older window is dropped (sequence guard).
- **Tests**: `tests/integration/test_client_health.py` (real sqlite) + `TestSignalsClientHealth` in tests/e2e (API mocked via `page.route`, plus one run on the real endpoint); `page_factory(users_tab=False)` opens the dashboard without the Users tab. 32 Python + 13 JS mutants (rate, the dead gate, channels, the window, thresholds, edges, 0 vs null, the window param, the listener, the guard) killed, restored by reverse text replacement with a hash check (§28).

### 36. RU-zone — Russian sites from abroad, paid users only (2026-10-10)

A paid user abroad gets RU sites (banks, Gosuslugi, VK, Yandex) through a Russian address, everything else direct. None of the regular protocols can do it: on entry they are all transit to exit (haproxy :8443 → exit, shadow-tls :443 → exit:8444, hy2/CDN DNAT'ed), so RU sites see exit's foreign IP. Ran as a two-account allowlist test from 2026-10-08; opened to every paid user here.

- **Server**: a standalone sing-box container `ru-exit` on entry (VLESS-Reality :8445, SNI `www.google.com`, cert record 2520 B — the §23 check runs at setup), egress straight from entry's RU address. haproxy, shadow-tls and 3x-ui are untouched. `scripts/setup_ru_exit.sh` (root, on entry): no argument = set up or re-run (keys once in `/etc/ru-exit/keys.env`, knobs remembered in `server.env`, config, container, `app_settings.ru_exit` = `{port, sni, pbk, sid}`, systemd `ru-exit-sync.timer`); `--sync` = what the timer runs every 2 min; `--remove` = setting cleared first, then timer and container (keys stay). The timer runs `/usr/local/sbin/ru-exit-sync`, a root-owned copy setup installs, never the rsync'd file: `/opt/vpn-bot` belongs to the deploy account (`tunnel`, group-writable), and a root timer must not execute a file another account can change — so **re-run setup after the script changes** (it recreates the container: a second of reconnects). The egress rejects private addresses, BitTorrent (sniffed) and SMTP :25 — everything leaves from the address the whole service enters by.
- **Who** (`bot/services/ru_exit.py::is_eligible` — one rule for the button, `/sub?mode=abroad` and the server's user list): status `paid` whose `subscription_expiry` has not passed — a lapsed payer KEEPS status `paid` (the panel's expiryTime switches the main key off; this egress never asks the panel) — or `support_topic` for a payer with an open ticket, told apart from a demo user with one by the paid-until date (demo users never have one) or, without a date, by `previous_state == 'paid'`. Stricter than `PAID_USER_STATUSES`, which keeps the paid protocols for a demo user while a ticket is open. Empty / unparseable date = no limit, as in the hy2t auth gate.
- **User sync**: `--sync` runs `python3 -P bot/services/ru_exit.py users` inside vpn-bot (read-only sqlite on `$DB_PATH`, JSON on stdout), renders the config deterministically (users sorted by name, one per uuid, malformed uuids skipped — one would fail `sing-box check` for everyone) and recreates the container only when it differs from the live one (a second of reconnects for RU-zone users). New config → `sing-box check` before the live one moves; a container that does not come back → rolled back to `config.json.prev`; a list that suddenly reads 0 users while the live one has some → not applied by the timer (a broken read is likelier than every payer lapsing at once; a hand-run setup applies it); bot not running / gone mid-read → a skip, not a failure; `flock` keeps a hand run and the timer apart. **Why the module imports nothing from `bot` at the top:** run as a file with `-P`, it skips the `bot.services` package init — `python -m bot.services.ru_exit` cost 1.2 s of the entry's single core per run.
- **Client**: `/sub/<token>?mode=abroad&format=clash` (FlClash: RU-zone proxy for `.ru/.su/.рф` + `geosite category-ru` + `GEOIP RU`; Google / YouTube / Telegram always direct, `MATCH,DIRECT`; fake-ip DNS so entry resolves proxied names in RU) and `?mode=abroad` (sing-box). Hiddify drops a profile's routing (it would send everything through RU) — the bot hands out only the FlClash link; no iOS (FlClash has none, Karing's handling of profile routing is unverified). Gated before anything records the request's network: **403** for anyone `is_eligible` leaves out, **404** while `app_settings.ru_exit` is unset, **503** when there is no address to give — never the home profile under this URL (a failed refresh leaves FlClash the profile it has; a full-VPN profile there would silently reroute everything). `?format=clash-proxies` ignores the mode.
- **Bot**: «🇷🇺 RU-зона» on the main menu (`notify_main_menu(..., user=)`), the key card (`build_key_delivery_message(..., db=)`, above the report row) and /sub — only for eligible users while the egress is set up. `RuZoneHandler` (`ru_zone`) sends the FlClash link to the presser's own private chat only; a key holder outside the rule (stale button, lapsed payer) gets «входит в платную подписку: /buy», anyone else «Сейчас недоступно». /help lists it among the /buy perks.
- **Not done:** RU-zone traffic is NOT counted against the quota — it never touches the x-ui panel, and sing-box keeps per-user counters only with the v2ray API, which the release image is built without (tags: gvisor, quic, dhcp, wireguard, ech, utls, reality_server, acme, clash_api). FlClash's Global mode turns it into a full VPN through the RU address; the BitTorrent / :25 guards are the abuse floor until metering exists.
- **Tests**: `tests/integration/test_ru_exit.py` (real sqlite: the rule incl. timezones and the second of expiry, the server list, the CLI run as the timer runs it — bare interpreter, no PYTHONPATH — /sub 403/404/503 and nothing recorded on refusal, buttons, handler) + `tests/unit/test_setup_ru_exit_sh.py` (the script for real with fake `docker` / `systemctl` / `ss` / `openssl` / `id` keeping state in files; the rendered config through the real `sing-box check`, `requires_docker`). 32 mutants killed, restored by reverse text replacement with a hash check (§28).
