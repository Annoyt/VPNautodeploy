# §10. Dashboard now covers every status

`bot/webapp/app.js::getAvailableActions` returns reset/ban for `new`, reset+reject+ban for `platform_select`, reset+revoke+ban+grant_100gb for `demo/paid/support_topic`, unban+reset for `banned`. Three new backend actions in `web_server.handle_user_action`: `reset` (set_state NEW + reset_user_data), `revoke` (BANNED + revoke_user_key + notify), `grant_100gb` (no state change, bumps `user.quota_gb` and propagates `totalGB` to x-ui via `add_client_sync`).
