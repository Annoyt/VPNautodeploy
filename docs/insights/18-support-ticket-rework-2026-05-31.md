# §18. Support-ticket rework (2026-05-31)

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
