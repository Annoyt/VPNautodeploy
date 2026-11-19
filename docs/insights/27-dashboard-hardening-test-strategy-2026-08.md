# §27. Dashboard hardening + test strategy (2026-08-30 session)

A user-report ("получил ключ, но его нет в списке") unravelled into three latent dashboard bugs and a testing overhaul. PR #1 (`dashboard-hardening-e2e`) has the full story; highlights every agent should know:

- **ext_* (email-only) users**: no Telegram username; their real address lives in `contact_email` (`users.email` is the synthetic panel id `user_ext_…@nekovo.ru` — never mail to it). Every admin surface names them by that address (2026-11-15):
  - `user_label()` in `bot/utils/user_label.py` is the one rule for the bot's messages: `@username`, else `✉️ <contact_email>`, else the message's old fallback (`user_<chat_id>`, `no_username`, …). `userLabel()` in `bot/webapp/app.js` is its copy for the dashboard.
  - Before that, only `/users`, `/find` and the dashboard's user list showed the address. `/onlines`, the SOS reports, admin command answers and the rest of the dashboard showed `user_ext_…`.
  - Name a user through one of the two, never with a new `f"@{username}" if username else …`. The dashboard's search matches `contact_email` too.
  - Provisioning goes ONLY through `AdminHandler._provision_email_user` / `/addmail` — a raw SQL INSERT into `users` once produced a zombie `chat_id=NULL` row (agent incident; the Hermes `user-ops` skill now forbids it).
- **Dead confirm modal**: `hideModal()` nulled `modalCallback` before the confirm handler called it — EVERY confirm-gated dashboard action (paid/ban/approve/reject/revoke/reset) silently did nothing, since the repo-root merge. Fixed in app.js; pinned by Playwright E2E.
- **Dead POSTs (same class, fixed 2026-11-11)**: `apiFetch(path)` took one argument and always sent a GET, so callers passing `{method: 'POST', …}` were silently ignored. Five actions were affected:
  - The Settings tab's plans and reminders saves read the values back and printed «✅ сохранено» with nothing stored.
  - The manual reminder sends (cohort, single user) and the Alerts tab's ✅ ack got 405.

  `apiFetch(path, options)` now passes `options` to `fetch()` and throws the server's `{"error": …}` text; `apiPost` delegates to it, so there is one fetch-and-error path.
- **Agent replies are sanitized (2026-11-11)**: the Alerts tab put `alert_history.kimi_analysis` and `dpi_reports.kimi_analysis` (the agent's free reply, up to 8000 chars) into `innerHTML` as they came. The dashboard URL carries the admin token, so an `<img onerror>` the model echoed from a log line or a user's text ran in the admin's session (an e2e test reproduced it, a collapsed DPI report included).
  - The protocol prompt asks for plain text, which Telegram gets escaped. The DPI prompts ask for HTML, which only the dashboard shows. Both go through `app.js` `agentReplyHtml`.
  - A reply without a formatting tag is shown as written, escaped, line breaks kept (`.agent-text`, `white-space: pre-wrap`).
  - Otherwise the allowed tags (`AGENT_TAGS`) are rebuilt with no attributes. `a` keeps an http(s) href only, with `rel="noopener noreferrer"`, so the token never leaves in a Referer. Other elements give way to their text; `AGENT_DROP` (script, style, iframe, svg…) goes with its content.
  - Never interpolate agent or user text into `innerHTML` raw: `esc()` for text, `agentReplyHtml()` for agent output. Pinned by `TestAgentReplies`. Pinned by `TestPlansSave` / `TestReminders` / `TestAlertAck` in `tests/e2e/test_dashboard_playwright.py`, which read the database after each click. The reminders tests swap in a real `NotificationService` with a Mock bot through `E2EStack.server`: the stack's bare Mock cannot be serialized by `GET /api/admin/reminders`.
- **Stale-snapshot rollback**: `handle_user_action` side effects `save_user()`d a pre-transition user snapshot (full-row write) — reject rolled back to pending_demo, ban/unban reverted for keyed users. Side effects now re-fetch; the reset branch revokes BEFORE `set_state` (the order the Telegram commands always used).
- **`grant_paid` is a real billing grant** (transition + subscriptions row + `grant_paid_access` in the main handler, not in the notification side-effect path which is skipped when NotificationService is down). The detail modal has its own ⭐ paid button (list-card buttons are invisible to mobile admins).
- **Test strategy (4 levels)** — each catches a class the others can't: unit+mutmut (function logic) / `tests/integration/test_web_actions_integration.py` on REAL sqlite (cross-layer row semantics — mocks can't see stale-row overwrites by construction) / `tests/e2e` Playwright (dead frontend JS) / deploy sha-smoke in `deploy_to_entry.sh` (repo-vs-prod drift). Trust a new suite only after re-introducing the bug and watching it fail.
- **Hermes `/ai`**: model switched to `minimax/minimax-m3:free` with a `fallback_providers` chain (nemotron-3-super), skills deploy via `scripts/deploy_hermes_skills.sh` (placeholder substitution; hand-rsync caused drift), watchdog defers restarts while an /ai request is in flight.
  - **v0.23.1 since 2026-11-16** (v0.18.2 before, from July). From about v0.21 on, Hermes needs Python 3.14, so it is now the official managed install:
    - code in `/usr/local/lib/hermes-agent-v0.23`;
    - the home in `/root/.hermes-v023`. `/root/.hermes` is a symlink to it, so the guard and `deploy_hermes_skills.sh` keep their paths.

    `config.yaml` needs `terminal.cwd: /root/hermes-work`. Without it, the workspace AGENTS.md, which carries the healthcheck-first rule, never reaches the system prompt. The procedure, its traps and the rollback are in `docs/runbooks/hermes_update.md`.
  - **New releases are announced** in TOPIC_AI, once per version, by `scripts/hermes_release_notice.py`: a root timer on entry, weekly on Mondays at 06:00 UTC (the owner's choice), installed by `scripts/deploy_hermes_host.sh` next to the guard, whose Telegram path it uses.
    - It compares the version `hermes-api.service` runs (its binary's `--version`) with GitHub's latest release of NousResearch/hermes-agent.
    - It reads the version from the release name: the tags from August to September 2026 were dates (`v2026.9.24` = v0.21.5).
    - A version counts as announced only once Telegram took the message. Two runs in a row that cannot check post one warning: one bad Monday stays quiet.

    Updating stays manual.
- **Cleanup**: removed the embedded 930MB ChatDev clone (authored workflow preserved in `docs/archive/`), `.archive/`, `scratch/`, mempalace artifacts, `htmlcov`/`mutants`; `AGENT.md` and `PROJECT_CONTEXT.md` deleted (stale — their live content moved here).
