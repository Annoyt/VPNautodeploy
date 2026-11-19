# §13. Admin UX in forum groups

Two Telegram Bot API constraints that bit us:
- The persistent Mini-App menu button (left of the input field) is 1:1-chat-only — silently ignored in groups.
- Inline `web_app` buttons are also 1:1-chat-only and return `BUTTON_TYPE_INVALID` in forum groups.

`CommandHandler.handle_admin` now picks shape by chat kind: `web_app` button in PM, `url` button (same URL, opens Telegram in-app browser) in groups. `_is_admin` calls in `commands.py` now read `user_id` from `message.from.id` via the new `_command_user_id(update)` helper — the old `_is_admin(chat_id)` was always false in a group because `chat_id` is the group's negative ID.
