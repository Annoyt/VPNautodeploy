# AGENTS.md — VPN Bot

> Agent-focused guidance. For project overview, see `docs/PROJECT.md`.
> Operational insights §1–§66: index at the end of this file, text in `docs/insights/`.

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
deploy sha-smoke) — see §27 ([docs/insights/27-dashboard-hardening-test-strategy-2026-08.md](docs/insights/27-dashboard-hardening-test-strategy-2026-08.md)) for what each catches.
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

### 5. Saving a user writes that user's row and nothing else (2026-11-15)

`UserRepository.save()` (also `db.save_user` and the async adapter) updates the user's row in place, or inserts it when there is none. Until 2026-11-15 it was an `INSERT OR REPLACE`, which had two effects:
- In the repo's schema `users.uuid` and `users.email` are UNIQUE, so a save carrying a uuid or panel email that another row held deleted that row silently, with its status, paid-until date and contact email. This hit tests and any database built from the repo's schema.
- Every save reset the columns it does not write to their defaults. On prod that zeroed `traffic_up` / `traffic_down` until the next mirror run; nothing reads them today.

**Prod's users table is older than the repo's schema** (read-only check, 2026-11-15, 160 rows):
- its only unique index is the chat_id primary key: uuid and email are not UNIQUE there, so a shared uuid is stored twice rather than deleting anyone;
- no two rows share one today: 87 rows hold a uuid and 87 an email, none twice, none in another case or with spaces (a second read-only count, same day);
- it has `traffic_up` / `traffic_down` (`DEFAULT 0`), which the repo's `CREATE TABLE` lacks, and no `xui_synced`;
- it has no NOT NULL column and no `''` uuid or email.

Code that needs one row per uuid must not count on the constraint: prod does not have it. A `CREATE UNIQUE INDEX` on either column would succeed there today. Adding one is a schema change on prod and the owner's call, and only with the 2026-11-15 `save()` deployed: under the old REPLACE the index would bring the silent deletion to prod.

Now:
- **A clash is refused where the schema has the constraint.** `save()` returns False and writes nothing. The log names the column and the user who holds it, never the value: the uuid is the user's key. To move a key to another user, clear it on the old row first. No code does that today.
- **`''` is stored as NULL** for uuid and email. Two empty strings clash; two NULLs do not.
- **A user without a chat_id (None or `''`) is refused.** Such a row belongs to nobody (the §27 zombie), and with a NULL every save added another.
- **A NULL for a NOT NULL column with a default** is written as that default, as REPLACE did. The repo's schema has no such column; prod's older table may.
- **Columns `save()` does not list keep their values.** `SAVE_COLUMNS` in `bot/core/repositories/user.py` is the one list, each value read from the user's attribute of the same name; a column missing from it is never written by a save.

Tests: `tests/integration/test_user_save.py` (real sqlite: the repo's schema, prod's table as read, and other older shapes built by hand).

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
| `bot/utils/user_label.py` (how an admin sees a user) | `userLabel` in `bot/webapp/app.js` (the same rule), `tests/integration/test_email_user_labels.py`, `TestEmailUsers` in `tests/e2e/test_dashboard_playwright.py` |
| `bot/services/xui_service.py` | `tests/integration/test_xui_service.py`, `tests/integration/test_code_review_fixes.py` |
| `bot/services/billing.py` | `tests/unit/test_billing.py`, `tests/integration/test_limit_ip.py`, `tests/integration/test_mykey_lost_key.py` (the key re-add uses its values) |
| `bot/core/database.py` schema | `tests/integration/test_migration.py`, `tests/unit/test_database.py` |
| `bot/core/repositories/user.py` `save()` / `SAVE_COLUMNS` | `tests/integration/test_user_save.py`, `tests/unit/test_repositories_user.py` |
| `bot/models/vpn_node.py` | All cluster tests using `VPNNode` |
| `scripts/hermes_model_guard.py` (`GuardIO`, `parse_env_file`, `effective_bot_token`, `save_state`) | `scripts/hermes_release_notice.py` loads them from its own directory, `tests/unit/test_hermes_release_notice.py` |
| `bot/services/exit_reach.py` (the PROXY header's marker, `MARK_SRC`) | `WebAppServer._dpi_exit_rows` drops that source, `tests/integration/test_exit_reach.py` |
| `scripts/exit_dpi_reporter.py` (a reject's `origin`, the users' addresses), `WebAppServer._dpi_exit_rows` (`hsfail_users`, `user_ips_json`), the DPI handshake checks in `bot/services/alert_manager.py` | `tests/unit/test_exit_dpi_reporter.py`, `tests/integration/test_hsfail_users.py`, `skills/dpi-analysis/SKILL.md`; the reporter runs on exit, installed by hand (§66) |
| `bot/services/tunnel_jam.py` (what counts as our tunnel's jam), `TUNNEL_KEYS` in `scripts/protocol_healthcheck.py`, R3's freeze in `bot/services/dpi_monitor.py` (`TUNNEL_JAM_COLLECTORS`) | `tests/integration/test_dpi_monitor.py::TestTunnelJam`, `tests/integration/test_hsfail_users.py` (a test keeps the keys and the prefix equal) |
| `bot/utils/public_url.py` (`entry_public_host`), the entry-address builders in `bot/services/subscription.py`, `vpn.py`, `ssh_exit.py` | `tests/integration/test_entry_public_host.py` (every entry server follows `ENTRY_PUBLIC_HOST`; unset is byte-identical) |
| `bot/services/hop_tls.py`, `scripts/caddy_entry_hop.py`, `systemd/hysteria-entry-hop.conf` | `tests/integration/test_hop_tls.py`, `tests/integration/test_hop_tls_stand.py`, `docs/runbooks/entry_hop_tls.md` (its blocks run in the tests) |
| `bot/services/hy2_keys.py`, the hy2 callback in `bot/core/web_server.py`, `scripts/hy2_auth_relay.py`, `scripts/install_hy2_auth_relay.sh`, `systemd/hy2-auth-relay.service` | `tests/integration/test_hy2_auth_relay.py` (the key list must equal what the live callback lets in), `tests/unit/test_hy2_auth_relay.py`, `tests/unit/test_install_hy2_auth_relay_sh.py`, `tests/integration/test_hy2_auth_relay_stand.py`, `docs/runbooks/hy2_auth_relay.md` (its blocks run in the tests) |
| `scripts/service_backup.py` (what goes in), `bot/services/backups.py`, `scripts/backup_send.py`, `scripts/install_service_backup.sh`, `systemd/nekovpn-backup.{service,timer}` | `tests/unit/test_service_backup.py`, `tests/integration/test_backup_send.py`, `tests/integration/test_backup_alert.py`, `tests/unit/test_install_service_backup_sh.py`, `docs/runbooks/backup_restore.md` (its check block runs in the tests); re-run `install_service_backup.sh` on entry after the script changes |
| `skills/` (a new Hermes skill) | `HERMES_SKILLS` in `scripts/service_backup.py` (a test keeps them equal) |
| `scripts/server_first_front.py`, `systemd/server-first-front.service`, `scripts/install_server_first_front.sh` | `tests/unit/test_server_first_front.py`, `tests/unit/test_install_server_first_front_sh.py`; re-run the installer on entry after the script changes |
| `SubscriptionService.SERVER_FIRST_MARKER` / the `-sf` twins | the client repo's `nekoServerFirstMarker` (core/neko_server_first.go, the same string), `tests/integration/test_server_first_profile.py` |
| A route with a token in its path or a credential in its query | `_TOKEN_AFTER` / `SECRET_PARAMS` in `bot/utils/access_log.py` (the access log redacts them, §65), `tests/unit/test_access_log.py`, `tests/integration/test_access_log_redaction.py` |
| `authHeader` in `bot/webapp/app.js` (the dashboard's `Authorization: NekoAdmin` header) | `_dashboard_credentials` in `bot/core/web_server.py`, `TestCredentialsStayOutOfUrls` in `tests/e2e/test_dashboard_playwright.py`, the curl lines in `skills/billing-ops`, `skills/incident-response` and `hermes/AGENTS.md` |

---

## Deployment

**Active production (as of 2026-08-30; flipped since the May notes — the bot moved to ENTRY on 2026-07-19):**
- **Entry node** (`ssh entry`) — the **vpn-bot container** lives HERE (`/opt/vpn-bot`), plus a LOCAL `3x-ui` for CF-fronted inbounds, ingress :443, dashboard API :8080 (and the same app over HTTPS on :8448 for exit, §53), and the Hermes `/ai` agent (:4097, systemd `hermes-api.service`; v0.23.1 since 2026-11-16, update and rollback in `docs/runbooks/hermes_update.md`).
- **Exit node** (`ssh vpn-exit`) — the real x-ui panel :2026 + xray :443 (the backend the bot manages over HTTP API), Caddy :9443 → entry (dashboard TLS; the hop to entry is TLS to :8448 with a pinned certificate since the §53 cutover of 2026-11-11, and :8080 is closed to exit since 2026-11-12), tinyproxy :8888 (bot's Telegram egress).
- **GitHub**: `git@github.com:Annoyt/VPNautodeploy.git` — `origin/main` is the source of truth.
- **`/opt/vpn-bot` on entry is NOT a git checkout** — it's an rsync target. `git pull/reset` there is impossible; code that isn't rsynced doesn't exist in prod (a fix once sat undeployed for 11 days while everyone debugged "a bug").

**Canonical deploy (2026-08-30+), from the dev machine:**
```bash
./scripts/deploy_to_entry.sh                 # full bot/ sync
./scripts/deploy_to_entry.sh bot/core/web_server.py   # or a subset
```
It stamps the git sha into `bot/version.txt`, rsyncs ONLY `bot/` + `scripts/` + `systemd/` (unit files for host installers, installed by nobody on the way)
(never compose/.env — entry keeps hand-tuned copies), runs the remote
`deploy_entry_bot.sh` (hardcoded `--no-deps` so 3x-ui is never recreated —
see the 2026-07-19 incident), then FAILS unless `/health` reports that
exact sha, and, once the hop's certificate exists, unless the HTTPS listener
on 8448 answers the same sha through the pin (§53). Commit before deploying
or the stamp says `-dirty`.

- Compose project name is pinned to `vpn-bot` via `name: vpn-bot` — must not change (volume names derive from it).
- Volumes on entry: `vpn-bot_3xui-data` (local 3x-ui), `vpn-bot_vpn-bot-data` (bot.db at `/var/lib/vpn-bot/`), `vpn-bot_vpn-bot-logs`.
- The bot talks to BOTH panels via HTTP API only; `xui.db` direct access is dead on entry (the May notes about making x-ui.db group-writable are historical).
- **Backup subscription domain (2026-10-08, IMPROVEMENT_PLAN E5):** `https://sub.nekoweather.xyz:2096` — Cloudflare-proxied A record to exit, Caddy site block on exit `:2096` (a port CF proxies end-to-end) reverse-proxying entry (`:8448` over pinned TLS since the §53 cutover of 2026-11-11) + the `/rule-sets/` mirror; cert from acme.sh `dns_cf` installed to `/etc/ssl/sub` with reloadcmd `chgrp caddy … && chmod 640 … && systemctl reload caddy` (acme.sh writes the key 600 root — without the hook every renewal breaks the Caddy reload). Caddyfile backups: `/etc/caddy/Caddyfile.bak-*`.

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

## Operational Insights — index

Sections §1–§62 live in [`docs/insights/`](docs/insights/), one file per section; their text was moved out of this file unchanged on 2026-11-19. Code, alerts and docs cite them as `§NN`, and the number is the file's prefix. Before working on a topic, open its section from this index or find it by search: the MCP tool `qmd` (`query`, collection `vpn-docs`) where it is installed, or grep over `docs/insights/`.

A new insight goes into `docs/insights/NN-<slug>.md` under the next free number, with a line in this index.

**Operational Insights (2026-05-12).** These are bear traps that bit during the prod consolidation. Read them before touching deploy/compose.

- §1. x-ui binds to 2026 internally, not 2053 — [docs/insights/01-x-ui-binds-to-2026-internally.md](docs/insights/01-x-ui-binds-to-2026-internally.md)
- §2. x-ui DB needs writable group access for the bot — [docs/insights/02-x-ui-db-needs-writable-group.md](docs/insights/02-x-ui-db-needs-writable-group.md)
- §3. Compose project name = volume prefix — [docs/insights/03-compose-project-name-volume-prefix.md](docs/insights/03-compose-project-name-volume-prefix.md)
- §4. Multiple legacy bot.db files on host — [docs/insights/04-multiple-legacy-bot-db-files-on.md](docs/insights/04-multiple-legacy-bot-db-files-on.md)
- §5. Failover code exists but is not wired — [docs/insights/05-failover-code-exists-but-is-not.md](docs/insights/05-failover-code-exists-but-is-not.md)
- §6. Compose must pass SID_VALUE / SNI_VALUE / DEMO_* explicitly — [docs/insights/06-compose-must-pass-sid-value-sni.md](docs/insights/06-compose-must-pass-sid-value-sni.md)
- §7. Phase 1 hardening of GetKeyHandler (deployed 2026-05-12) — [docs/insights/07-phase-1-hardening-of-getkeyhandler-deployed.md](docs/insights/07-phase-1-hardening-of-getkeyhandler-deployed.md)

**Operational Insights (2026-05-29 session).** A long session covering: lifecycle helper unification, dashboard expansion, TLS reverse proxy, group-mode admin UX, kimi-code agent integration. Notes below in chronological order so an agent can re-create the same outcome step-by-step.

- §8. One revoke path through `services/user_lifecycle.py` — [docs/insights/08-one-revoke-path-through-services-user.md](docs/insights/08-one-revoke-path-through-services-user.md)
- §9. Status guard on key issuance — [docs/insights/09-status-guard-on-key-issuance.md](docs/insights/09-status-guard-on-key-issuance.md)
- §10. Dashboard now covers every status — [docs/insights/10-dashboard-now-covers-every-status.md](docs/insights/10-dashboard-now-covers-every-status.md)
- §11. Dashboard broadcast endpoint — [docs/insights/11-dashboard-broadcast-endpoint.md](docs/insights/11-dashboard-broadcast-endpoint.md)
- §12. TLS reverse proxy for the admin Mini App — [docs/insights/12-tls-reverse-proxy-for-the-admin.md](docs/insights/12-tls-reverse-proxy-for-the-admin.md)
- §13. Admin UX in forum groups — [docs/insights/13-admin-ux-in-forum-groups.md](docs/insights/13-admin-ux-in-forum-groups.md)
- §14. Telegram API errors are mute unless you pull `description` — [docs/insights/14-telegram-api-errors-are-mute-unless.md](docs/insights/14-telegram-api-errors-are-mute-unless.md)
- §15. AI agent: kimi-code via a host-side HTTP bridge — [docs/insights/15-ai-agent-kimi-code-via-a.md](docs/insights/15-ai-agent-kimi-code-via-a.md)
- §16. Why not give Kimi full root over the prod box? — [docs/insights/16-why-not-give-kimi-full-root.md](docs/insights/16-why-not-give-kimi-full-root.md)
- §17. Backup state — [docs/insights/17-backup-state.md](docs/insights/17-backup-state.md)
- §18. Support-ticket rework (2026-05-31) — [docs/insights/18-support-ticket-rework-2026-05-31.md](docs/insights/18-support-ticket-rework-2026-05-31.md)
- §19. Kimi domain-aware skill routing — [docs/insights/19-kimi-domain-aware-skill-routing.md](docs/insights/19-kimi-domain-aware-skill-routing.md)
- §20. Server-side keys for Kimi (/root/.kimi-code/.env) — [docs/insights/20-server-side-keys-for-kimi-root.md](docs/insights/20-server-side-keys-for-kimi-root.md)
- §21. Photo ingestion + self-healing schedulers (2026-06-01) — [docs/insights/21-photo-ingestion-self-healing-schedulers-2026.md](docs/insights/21-photo-ingestion-self-healing-schedulers-2026.md)
- §22. AI agent: kimi-code → OpenCode (2026-07-04) — [docs/insights/22-ai-agent-kimi-code-opencode-2026.md](docs/insights/22-ai-agent-kimi-code-opencode-2026.md)
- §23. Reality mass outage: Microsoft's cert outgrew xray's 8192-byte buffer (2026-07-20) — [docs/insights/23-reality-mass-outage-microsoft-s-cert.md](docs/insights/23-reality-mass-outage-microsoft-s-cert.md)
- §24. Telegram-egress failover + reserve DE node for paid users (2026-07-25) — [docs/insights/24-telegram-egress-failover-reserve-de-node.md](docs/insights/24-telegram-egress-failover-reserve-de-node.md)
- §25. Client-app churn: Happ out, Karing in; multiple /sub formats (2026-07-25) — [docs/insights/25-client-app-churn-happ-out-karing.md](docs/insights/25-client-app-churn-happ-out-karing.md)
- §26. Architecture snapshot — 2026-08-19 (большой ремонт учёта/биллинга) — [docs/insights/26-architecture-snapshot-2026-08-19.md](docs/insights/26-architecture-snapshot-2026-08-19.md)
- §27. Dashboard hardening + test strategy (2026-08-30 session) — [docs/insights/27-dashboard-hardening-test-strategy-2026-08.md](docs/insights/27-dashboard-hardening-test-strategy-2026-08.md)
- §28. Agent runbook, alert→agent diagnosis, free-model guard (2026-09-05 session) — [docs/insights/28-agent-runbook-alert-agent-diagnosis-free.md](docs/insights/28-agent-runbook-alert-agent-diagnosis-free.md)
- §29. DPIMonitor — the feedback loop is closed (2026-09-06 session) — [docs/insights/29-dpimonitor-the-feedback-loop-is-closed.md](docs/insights/29-dpimonitor-the-feedback-loop-is-closed.md)
- §30. Lockdown mode + shutdown auto-detect (2026-09-06 session) — [docs/insights/30-lockdown-mode-shutdown-auto-detect-2026.md](docs/insights/30-lockdown-mode-shutdown-auto-detect-2026.md)
- §31. FlClash rule lists — the profile as a remote control (2026-10-08 session) — [docs/insights/31-flclash-rule-lists-the-profile-as.md](docs/insights/31-flclash-rule-lists-the-profile-as.md)
- §32. FlClash profile channels — client-side cascade, own probes, emergency/mirror providers (2026-10-08) — [docs/insights/32-flclash-profile-channels-client-side-cascade.md](docs/insights/32-flclash-profile-channels-client-side-cascade.md)
- §33. SOS channels: /sos, SOS by mail, offline kit, /share, /nudge_sub (2026-10-08) — [docs/insights/33-sos-channels-sos-sos-by-mail.md](docs/insights/33-sos-channels-sos-sos-by-mail.md)
- §34. Per-protocol client telemetry (E8) → DPIMonitor R6 `client_dark` → reverse SOS (E21) (2026-10-09) — [docs/insights/34-per-protocol-client-telemetry-e8-dpimonitor.md](docs/insights/34-per-protocol-client-telemetry-e8-dpimonitor.md)
- §35. Client health dashboard — protocol × operator, seen from the clients (2026-10-10) — [docs/insights/35-client-health-dashboard-protocol-operator-seen.md](docs/insights/35-client-health-dashboard-protocol-operator-seen.md)
- §36. RU-zone — Russian sites from abroad, paid users only (2026-10-10) — [docs/insights/36-ru-zone-russian-sites-from-abroad.md](docs/insights/36-ru-zone-russian-sites-from-abroad.md)
- §37. NekoVPN client releases: the bot's message with «Что нового» (2026-10-15) — [docs/insights/37-nekovpn-client-releases-the-bot-s.md](docs/insights/37-nekovpn-client-releases-the-bot-s.md)
- §38. One-tap import into NekoVPN: `/open/<token>` (2026-10-25) — [docs/insights/38-one-tap-import-into-nekovpn-open.md](docs/insights/38-one-tap-import-into-nekovpn-open.md)
- §39. NekoVPN reads the profile's headers: refresh interval, mirrors (2026-10-25) — [docs/insights/39-nekovpn-reads-the-profile-s-headers.md](docs/insights/39-nekovpn-reads-the-profile-s-headers.md)
- §40. Signed remote settings for the NekoVPN app (2026-10-25, client plan E14) — [docs/insights/40-signed-remote-settings-for-the-nekovpn.md](docs/insights/40-signed-remote-settings-for-the-nekovpn.md)
- §41. The NekoVPN app's SOS button: POST /api/neko/sos/<token> (2026-10-25, client plan E16/E23) — [docs/insights/41-the-nekovpn-app-s-sos-button.md](docs/insights/41-the-nekovpn-app-s-sos-button.md)
- §42. Address limits: paid 5, demo 2 (2026-10-30, demo 2026-11-01) — [docs/insights/42-address-limits-paid-5-demo-2.md](docs/insights/42-address-limits-paid-5-demo-2.md)
- §43. Demo allowance by client app: 10 GB with NekoVPN, 5 GB otherwise (2026-10-30) — [docs/insights/43-demo-allowance-by-client-app-10.md](docs/insights/43-demo-allowance-by-client-app-10.md)
- §44. «🔑 Мой ключ»: a lost key comes back with its tier, a lapsed payer's stays off (2026-10-30) — [docs/insights/44-a-lost-key-comes-back-with.md](docs/insights/44-a-lost-key-comes-back-with.md)
- §45. The app chooser: /app, /app/<token>, /apk (2026-11-01, client plan E35) — [docs/insights/45-the-app-chooser-app-app-token.md](docs/insights/45-the-app-chooser-app-app-token.md)
- §46. The NekoVPN app's quota card: the `nekovpn-plan` header and `/start buy` (2026-11-01) — [docs/insights/46-the-nekovpn-app-s-quota-card.md](docs/insights/46-the-nekovpn-app-s-quota-card.md)
- §47. Calls get a second UDP path; the hy2 callback stops blocking (2026-11-11, plan F, package A) — [docs/insights/47-calls-get-a-second-udp-path.md](docs/insights/47-calls-get-a-second-udp-path.md)
- §48. Probes and alerts: one definition, users' connections, state that survives a restart (2026-11-11, plan F, package B) — [docs/insights/48-probes-and-alerts-one-definition-users.md](docs/insights/48-probes-and-alerts-one-definition-users.md)
- §49. A demotion that reaches every client (2026-11-11, plan F, package C) — [docs/insights/49-a-demotion-that-reaches-every-client.md](docs/insights/49-a-demotion-that-reaches-every-client.md)
- §50. The healthcheck on a timer, the agent as an adviser, usage and hy2 sessions (2026-11-11, plan F, package D) — [docs/insights/50-the-healthcheck-on-a-timer-the.md](docs/insights/50-the-healthcheck-on-a-timer-the.md)
- §51. New protocols and fingerprints: per-user TLS fingerprint, VLESS XHTTP Reality, SSH emergency path (2026-11-11, plan F, package E) — [docs/insights/51-new-protocols-and-fingerprints-per-user.md](docs/insights/51-new-protocols-and-fingerprints-per-user.md)
- §52. What a tunnel user can reach on exit itself: the panel's xray API, hysteria, Caddy's admin (2026-11-11) — [docs/insights/52-what-a-tunnel-user-can-reach.md](docs/insights/52-what-a-tunnel-user-can-reach.md)
- §53. TLS on the exit → entry hop: the bot's own HTTPS listener, pinned on exit (2026-11-11) — [docs/insights/53-tls-on-the-exit-entry-hop.md](docs/insights/53-tls-on-the-exit-entry-hop.md)
- §54. The bot token from the dashboard: Settings → «Токен бота» (2026-11-11) — [docs/insights/54-the-bot-token-from-the-dashboard.md](docs/insights/54-the-bot-token-from-the-dashboard.md)
- §55. ws through Cloudflare freezes from Russian networks; DPIMonitor `probe_bulk` (2026-11-13) — [docs/insights/55-ws-through-cloudflare-freezes-from-russian.md](docs/insights/55-ws-through-cloudflare-freezes-from-russian.md)
- §56. The hy2 auth relay on exit: hysteria keeps letting users in while the bot is down (2026-11-15, plan F, A6) — [docs/insights/56-the-hy2-auth-relay-on-exit.md](docs/insights/56-the-hy2-auth-relay-on-exit.md)
- §57. A second front: what passes from Russia, measured from entry (2026-11-15) — [docs/insights/57-a-second-front-what-passes-from.md](docs/insights/57-a-second-front-what-passes-from.md)
- §58. Daily encrypted backup of the whole service → the «Бэкапы» topic (2026-11-17) — [docs/insights/58-daily-encrypted-backup-of-the-whole.md](docs/insights/58-daily-encrypted-backup-of-the-whole.md)
- §59. Exit's IPv4 blocked from Russia; entry reaches exit through the DE reserve (2026-11-18) — [docs/insights/59-exit-s-ipv4-blocked-from-russia.md](docs/insights/59-exit-s-ipv4-blocked-from-russia.md)
- §60. The users' address apart from the dashboard: `SUB_PUBLIC_URL` (2026-11-18) — [docs/insights/60-the-users-address-apart-from-the.md](docs/insights/60-the-users-address-apart-from-the.md)
- §61. A new connection to exit every minute: the reach check (2026-11-19) — [docs/insights/61-a-new-connection-to-exit-every.md](docs/insights/61-a-new-connection-to-exit-every.md)
- §62. The server-first front: a banner first, for the NekoVPN app (2026-11-19, IMPROVEMENT_PLAN G4) — [docs/insights/62-the-server-first-front-a-banner.md](docs/insights/62-the-server-first-front-a-banner.md)
- §63. Entry's address for clients, apart from the node's own: `ENTRY_PUBLIC_HOST`, `HOP_TLS_IP` (2026-11-19) — [docs/insights/63-entry-s-address-for-clients-apart.md](docs/insights/63-entry-s-address-for-clients-apart.md)
- §64. The exit → entry tunnel: users and control apart, open-file limits, the watchdog (2026-11-20) — [docs/insights/64-the-exit-entry-tunnel-users-and-control-apart.md](docs/insights/64-the-exit-entry-tunnel-users-and-control-apart.md)
- §65. Credentials out of the access log: the dashboard's Authorization header, initData for a day (2026-11-20) — [docs/insights/65-credentials-out-of-the-access-log.md](docs/insights/65-credentials-out-of-the-access-log.md)
- §66. Handshake failures from users' own addresses are not probing: `dpi_hsfail` and `dpi_hsfail_users` (2026-11-20) — [docs/insights/66-handshake-failures-from-users-addresses-are-not-probing.md](docs/insights/66-handshake-failures-from-users-addresses-are-not-probing.md)
