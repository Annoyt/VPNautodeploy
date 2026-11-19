# §15. AI agent: kimi-code via a host-side HTTP bridge

Kimi-code CLI is installed in `/root/.kimi-code` on the **host**, not inside the container — its OAuth credentials live in `/root/.kimi-code/credentials` and its binary is 136 MB. The bot reaches it via a tiny FastAPI wrapper:

- **`kimi-bridge.service`** (systemd unit, `/usr/local/bin/kimi_bridge.py`) listens on `0.0.0.0:7077`. Endpoints: `POST /ask {prompt, session_id?, model?}`, `GET /health`, `POST /reset`. Uses `--output-format stream-json` and parses `role=meta, type=session.resume_hint` events to extract `session_id`. Auth via `X-Bridge-Token` header (random 24-byte hex, stored in `/etc/kimi-bridge.env`).

- **docker-compose** adds `extra_hosts: "host.docker.internal:host-gateway"` so the bot resolves the bridge from inside the container at `http://host.docker.internal:7077`.

- **bot/services/kimi_client.py** wraps the HTTP API and persists per-conversation `session_id` in a new SQLite table `ai_sessions(session_key TEXT PRIMARY KEY, kimi_session TEXT, ...)`. Keys: `"pm:<chat_id>"` for DMs, `"topic:<chat_id>:<thread_id>"` for forum topics.

- **bot/handlers/ai_handler.py** runs *before* `CommandHandler` (so `/ai` isn't swallowed). Three entry points: `/ai <prompt>`, `/ai_reset`, and free-text inside `TOPIC_AI` (env var = thread id of the "AI" topic in the forum group; admin-only).

- `TelegramClient.send_chat_action("typing")` is fired before each Kimi call so the admin sees a typing indicator during the 5–30s wait.

The OAuth login is interactive (`kimi → /login` opens a browser). Bootstrap once via `ssh root@<host> -t tmux attach -t kimi-setup`. We pre-create that tmux session in the installer.
