# §2. x-ui DB needs writable group access for the bot

The bot writes to `x-ui.db` for `add_client_sync`, `remove_client_sync`, and `sync_all_clients_from_bot_db` on startup. Inside `vpn-bot_3xui-data`, x-ui creates files as `root:root 0644`, but vpn-bot runs as UID 1000. Fix after first start:
```bash
docker run --rm -v vpn-bot_3xui-data:/v alpine sh -c "chown root:1000 /v/* && chmod 664 /v/*.db /v/*.db-shm /v/*.db-wal"
docker restart vpn-bot
```
The mount in compose must **not** carry the `:ro` flag.
