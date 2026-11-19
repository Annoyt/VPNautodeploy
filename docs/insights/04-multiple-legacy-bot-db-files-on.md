# §4. Multiple legacy bot.db files on host

`/opt/vpn-bot/data/bot.db`, `/opt/vpn-bot/data/vpn_bot.db`, `/etc/cascade-vpn/bot.db` are all **stale** (April snapshots) — the live DB is inside the `vpn-bot_vpn-bot-data` Docker volume. Don't try to "merge" them; treat them as cold backups only.
