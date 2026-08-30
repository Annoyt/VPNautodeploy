# AGENTS.md — VPN Bot

> Agent-focused guidance. For project overview, see `docs/PROJECT.md`.

---

## Project Overview

Python Telegram bot for VPN service management. Supports forum-group mode (structured topics) and PM-only mode.

**Current test status (2026-08-30):**
```bash
python3 -m pytest tests/ -q      # 2044 passed (unit + integration; e2e excluded via norecursedirs)
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
