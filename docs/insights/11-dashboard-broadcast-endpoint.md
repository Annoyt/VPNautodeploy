# §11. Dashboard broadcast endpoint

`POST /api/admin/broadcast` with `{text, confirm: bool, audience: "active"|"demo"|"all_known"}`. confirm=false returns a recipient count + 10-username sample; confirm=true sends through `Bot.send_message` in a worker thread with 50 ms cooldown between sends (well under Telegram's 30 msg/s cap). Logged into `admin_actions` table as `webapp_broadcast_<audience>`. UI lives in `app.js::openBroadcastModal` — two-step modal with textarea + audience dropdown.
