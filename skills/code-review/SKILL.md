---
name: code-review
description: Review code in the vpn-bot-refactor project; optionally land the change via git push
type: prompt
whenToUse: User asks to review code, audit a diff, check a PR, or "fix and commit" something concrete
arguments:
  - target
---

# Review target

Default working tree: `$REPO_PATH` = `/opt/vpn-bot` (synced from `git@github.com:Annoyt/VPNautodeploy.git`, branch `main`).

If `$target` looks like a path → read that file. If it looks like a commit SHA → `git -C "$REPO_PATH" show <sha>`. If it looks like a PR number → `gh pr view <n>` (assume `gh` is configured).

# Review checklist

1. **Async/sync correctness** — `aiohttp` handlers in `bot/core/web_server.py` must use `await asyncio.to_thread(...)` for blocking DB and sync XUI calls; never call `time.sleep` inside the event loop. The bot itself runs on sync polling, so handlers there are free to block.
2. **Error handling** — every external call (Telegram API, XUI, x-ui DB) wrapped; failures logged, never raise into the polling loop.
3. **SQL safety** — parameterized queries only; `?`-binding, not f-strings. Schema names must match the *prod* DB (`message_text`, `timestamp`, `started_at`, `expires_at`), not the legacy names in `CREATE TABLE`.
4. **Auth / IDOR** — every admin endpoint checks `_validate_admin`; every callback that targets another user verifies `validator.validate_admin(user_id)`.
5. **Secrets** — never log `BOT_TOKEN`, `XUI_PASSWORD`, `REALITY_PUBLIC_KEY`, `KIMI_BRIDGE_TOKEN`, or session/cred file paths. The dashboard's `secret_status` whitelist is the contract.
6. **Forum-mode compat** — anything that sends a message to `FORUM_GROUP_ID` must include `message_thread_id`. Inline `web_app` buttons must only appear in 1:1 chats (group falls back to `url` button + `admin_token` query).
7. **Schema drift** — if a CREATE TABLE in `bot/core/database.py` was added/renamed, check the prod table actually matches (the `CREATE IF NOT EXISTS` does **not** migrate existing tables).

# Landing a change

If the user explicitly says "fix and commit" / "почини и закоммить":

```sh
cd "$REPO_PATH"
git pull --ff-only origin main
# edit files
python3 -c "import sys; sys.path.insert(0,'.'); from bot.handlers.callbacks import *"   # smoke-import what you touched
git add <specific paths>
git status -s                                      # show user what you're about to commit
# ASK FOR CONFIRMATION HERE if change touches auth, billing, or x-ui sync
git commit -m "<type>(<scope>): <imperative summary>

<optional body>

Co-Authored-By: kimi-bot <kimi-bot@local>"
git push kimi-origin main                          # writes via /root/.ssh/github_kimi (deploy key)
```

Then deploy and verify:

```sh
docker compose -f "$REPO_PATH/docker-compose.yml" up -d --build vpn-bot
sleep 8
docker compose -f "$REPO_PATH/docker-compose.yml" logs vpn-bot --tail 20 | grep -iE 'error|warn|registered'
```

# Standards (what to flag in any review)

1. No blocking I/O on the asyncio event loop.
2. Complete error handling at every external boundary.
3. Parameterized SQL, no string-formatted queries.
4. Single Responsibility — handlers route, services do work, repositories own SQL.
5. No DRY duplication — common ops live in `bot/services/user_lifecycle.py` (`revoke_user_key`) and `bot/utils/validators.py`.
6. Comments only when *why* is non-obvious; no commentary that restates the code.
7. Tests follow code: if you touched `forward_to_support`, the smoke import + a targeted log read are the bar.

# When NOT to push

- Auth, billing, or x-ui sync changes → propose diff, wait for confirmation.
- Bumping versions in `requirements.txt` → propose, wait.
- Anything that touches `/opt/vpn-bot/.env` schema → never push without user.
- Schema migrations (`CREATE`, `ALTER`, `DROP`) → propose the SQL, get OK, then run.

If review only — review, summarize, don't touch the working tree.
