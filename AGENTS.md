# AGENTS.md — VPN Bot

> Agent-focused guidance. For project overview, see `docs/PROJECT.md`.

---

## Project Overview

Python Telegram bot for VPN service management. Supports forum-group mode (structured topics) and PM-only mode.

**Current test status (2026-05-12):**
```bash
PYTHONPATH=$(pwd) python3 -m pytest -q
# Result: 1083 passed, 5 skipped, 1 flake (test_user_not_saved_on_sync_failure
#         — passes in isolation, fails only in full-suite ordering)
```

Run with PYTHONPATH:
```bash
PYTHONPATH=$(pwd) pytest -q --ignore=skills/
```

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

**Active production (as of 2026-05-12):**
- **Exit Node**: the exit host — bot + 3X-UI in Docker (this is the live one)
- **Entry Node**: the entry host — iptables DNAT forwarder
- **Git remote `exit`** (`<old-blocked-host>`): currently **blocked / unreachable**, do not push there
- **GitHub**: `https://github.com/Annoyt/VPNautodeploy.git` — `origin/main` is the source of truth

**How the bot actually runs:**
- `vpn-bot.service` systemd unit exists but is **disabled** — do not use it
- Deploy is **Docker Compose** from `/opt/vpn-bot/` on the prod host
- Compose project name is pinned to `vpn-bot` via `name: vpn-bot` at the top of `docker-compose.yml`. This must not change — `XUI_DB_PATH` hardcodes the resulting volume path
- Three canonical containers:
  - `3x-ui` (image `ghcr.io/mhsanaei/3x-ui:latest`) — internal listener on **port 2026** (not 2053, despite what older configs say), web path `/this_is_fine`
  - `vpn-bot` (image built from `./Dockerfile`) — Python sync polling, runs as UID 1000
  - ~~`traffic-collector`~~ — **removed** on 2026-05-12 (was unhealthy; its job moves into vpn-bot QuotaMonitor in Phase 5)
- Three canonical Docker volumes (all under project name `vpn-bot`):
  - `vpn-bot_3xui-data` — 3x-ui's `/etc/x-ui` AND vpn-bot's read-only ❌ — see below
  - `vpn-bot_vpn-bot-data` — bot's `/var/lib/vpn-bot/` (bot.db lives here)
  - `vpn-bot_vpn-bot-logs` — bot's `/var/log/vpn-bot/`

**Deploy procedure (validated):**
```bash
# 1. Locally
git push origin main

# 2. On prod (root@<exit-host>)
cd /opt/vpn-bot
git fetch origin && git reset --hard origin/main   # NB: also wipes uncommitted manual hotfixes; stash first if you want them
docker compose up -d --build
```

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

**Fix:** `bot/services/fallback_node.py::FallbackNodeService` — lazy provisioning on paid `/sub` fetch (idempotent, 10-min membership cache), same uuid as the main system, revocation mirrored in `revoke_user_key`. Subscription appends a `<email>-de` outbound for `FALLBACK_ALLOWED_STATUSES=('paid','support_topic')`. Bear traps learned the hard way:
- The fallback session MUST set `trust_env = False` — otherwise the panel call rides `HTTPS_PROXY` and tinyproxy's ConnectPort allowlist 403s the :2026 CONNECT.
- The subscription FALLBACK block is wrapped in try/except: a dead reserve panel must never kill `/sub` for all paid users.
- Compose must pass `FALLBACK_NODE_*`/`EXIT_NODE_IP` explicitly — `.env` alone doesn't reach the container.
- Panel admin creds were reset via `x-ui setting -username admin -password …` on 2026-07-25 (old password unknown); they live in entry's `.env` as `FALLBACK_NODE_XUI_USER/PASS`. ufw on the reserve allows :2026 from entry (note: a pre-existing "Anywhere" rule for 2026 exists — tightening it is a deliberate follow-up, verify the owner doesn't use the panel from elsewhere first).

**Deploy topology gotcha:** entry's `/opt/vpn-bot` is NOT a git repo — deploy there is `rsync` (excludes in scripts/deploy_entry_bot.sh usage note) + `./scripts/deploy_entry_bot.sh` (builds and recreates ONLY vpn-bot, `--no-deps` so 3x-ui is never dragged along). GitHub pushes run from the exit host via `/root/.ssh/github_kimi` (both local and exit `origin` remotes are HTTPS and can't push; push with an explicit `git@github.com:…` URL). Exit's checkout has unpublished prod commits — never `git reset --hard` there blindly; push refs without touching its working tree (`git fetch bundle main:refs/remotes/local/main && git push <ssh-url> refs/remotes/local/main:main`).
