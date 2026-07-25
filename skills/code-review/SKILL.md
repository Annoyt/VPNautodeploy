---
name: code-review
description: Review vpn-bot code and, where safe, propose or apply a change. Landing to main happens in the dev repo, not on entry.
type: prompt
whenToUse: User asks to review code, audit a diff, check a PR, or "fix and commit" something concrete
arguments:
  - target
---

# Where the code is

On **entry**, the deployed tree is `/opt/vpn-bot` — **rsync-deployed, NOT a git checkout**. You can read and
(for emergencies) hot-edit it, but you **cannot `git commit`/`git push` from here**. Real landing to `main`
happens in the developer's repo, which you don't have. So: **review + propose**; for an approved emergency
fix, edit under `/opt/vpn-bot/bot/` and rebuild, then hand the change back so the human mirrors it into the repo.

If `$target` is a path → read it. A commit SHA or PR number → say you can't reach git/`gh` from entry and ask the
human to paste the diff, or review what's on disk.

# Review checklist

1. **Async/sync correctness** — `aiohttp` handlers in `bot/core/web_server.py` must use
   `await asyncio.to_thread(...)` for blocking DB / sync XUI calls; never `time.sleep` in the event loop. The bot
   itself runs on sync polling, so handlers there may block.
2. **Error handling** — every external call (Telegram API, XUI, x-ui DB) wrapped; failures logged, never raised
   into the polling loop.
3. **SQL safety** — parameterized `?`-binding only, never f-strings. Names must match the **prod** DB
   (`message_text`, `timestamp`, `started_at`, `expires_at`), not legacy `CREATE TABLE` names.
4. **Auth / IDOR** — every admin endpoint checks `_validate_admin`; every callback targeting another user verifies
   `validator.validate_admin(user_id)`.
5. **Secrets** — never log `BOT_TOKEN`, `XUI_PASSWORD`, `REALITY_PUBLIC_KEY`, SMTP creds, or cred file paths. The
   dashboard `secret_status` whitelist is the contract.
6. **Forum-mode compat** — anything sending to `FORUM_GROUP_ID` must include `message_thread_id`. Telegram
   `parse_mode=HTML` text must be `html.escape`d (raw `<` silently kills the send). Inline `web_app` buttons only
   in 1:1 chats. **House rule: no admin PM while the forum group works** — reply in topics, PM is fallback only.
7. **Schema drift** — `CREATE IF NOT EXISTS` does **not** migrate existing tables; if a column was added, check the
   prod DB actually has it (add an `ALTER` migration in `database.py`).

# Emergency hotfix flow (only if the user says "почини и rebuild")

```sh
DC="docker compose -f /opt/vpn-bot/docker-compose.yml -f /opt/vpn-bot/docker-compose.entry.yml"
# edit files under /opt/vpn-bot/bot/
$DC exec -T vpn-bot python3 -c "import bot.main"      # smoke-import what you touched
# ASK FOR CONFIRMATION if the change touches auth, billing, or x-ui sync
$DC up -d --build vpn-bot
sleep 8
$DC logs vpn-bot --tail 20 | grep -iE 'error|warn|registered'
```
Then give the admin the exact diff so they commit it in the dev repo — otherwise the next rsync deploy reverts it.

# Standards to flag in any review
1. No blocking I/O on the asyncio event loop.
2. Complete error handling at every external boundary.
3. Parameterized SQL, no string-formatted queries.
4. Single Responsibility — handlers route, services do work, repositories own SQL.
5. Reuse common ops (`bot/services/user_lifecycle.py` `revoke_user_key`, `bot/utils/validators.py`) — no DRY duplication.
6. Comments only when *why* is non-obvious.
7. Tests follow code; run `pytest -q` on the dev box, not here (the deployed tree has no test deps guaranteed).

# When NOT to touch
- Auth, billing, or x-ui sync changes → propose the diff, wait for confirmation.
- `requirements.txt` bumps, `.env` schema, schema migrations (`CREATE`/`ALTER`/`DROP`) → propose, get OK.
- Review-only requests → review, summarize, don't modify the tree.
