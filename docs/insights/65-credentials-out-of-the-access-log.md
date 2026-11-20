# §65. Credentials out of the access log: the dashboard's Authorization header, initData for a day (2026-11-20)

Found on entry on 2026-11-20. aiohttp's default access logger (`aiohttp.access`, INFO) writes each request line and its Referer in full, into `docker logs vpn-bot` and `/var/log/vpn-bot/bot.log` (the dashboard's Logs tab shows its tail). The dashboard sent its credentials in the URL of every call (`?initData=…&admin_token=…`, with polling every 15 s). Its own address, `/?admin_token=…` from `/admin`, went out as the Referer of each call. The admin token lives an hour. **initData had no expiry:** `_validate_init_data` checked the signature and nothing else, so a logged one opened the dashboard until the bot token changed. Users' subscription tokens, which are path segments, were in the log too.

- **The header.** `Authorization: NekoAdmin initData=<enc>&admin_token=<enc>`. It is the query string the page used to append, moved into one header: `authHeader` / `withAuth` in `bot/webapp/app.js`, `_dashboard_credentials` in `bot/core/web_server.py`.
  - **One header for both.** The Mini App may hold both credentials. After a bot-token change (§54), its initData is signed with the old token, and only the admin token the change hands out still passes.
  - **Authorization, not a header of our own.** Caddy's access log (where one is on) and most other loggers leave Authorization out.
  - The scheme name is case-insensitive. When the header is there, the URL is not read.
- **The URL still opens the dashboard.** This covers the WebSocket (`/api/admin/ws`: a browser gives a WebSocket no headers), a page loaded before the deploy (until it is reloaded), `/api/me` and curl. The `/admin` link itself still carries `?admin_token=`.
- **`bot/utils/access_log.py`.** `RedactingAccessLogger` writes aiohttp's default line (`%a %t "%r" %s %b "%{Referer}i" "%{User-Agent}i"`), passing the request target and the Referer through `redact_url`. These values become `[redacted]`:
  - `admin_token` and `initData` in the query. Names are compared lowercased and percent-decoded, because aiohttp decodes a name before a handler reads it.
  - the segment after `/sub/`, `/open/`, `/app/`, `/probe/` and `/api/neko/sos/`.

  `WebAppServer.make_runner()` builds the runner with this logger, and `run_in_thread` uses `make_runner()`. aiohttp's own `AccessLogger` cannot be subclassed for this: it looks up each field's formatter on `AccessLogger` itself, by name.
- **initData lives a day.** `_validate_init_data` now also requires an `auth_date` (when Telegram opened the Mini App) at most `INIT_DATA_MAX_AGE_S` = 24 h old. One without a whole-number `auth_date` is refused. A date ahead of the bot's clock passes: only Telegram can sign one, so it means the clock is behind. A Mini App left open longer than a day gets 401 until it is reopened. The hash comparison is `hmac.compare_digest` on bytes.
- **`Referrer-Policy: strict-origin`** on the dashboard page. Its calls get the origin alone, and so do the CDNs, Google Fonts and the OSM tiles from older WebViews. Not `no-referrer`: OSM's tile servers refuse a page that sends no Referer.
- **What was logged before the deploy stays** in docker's json log and in bot.log until rotation. Count it, never print it:
  ```sh
  ssh entry 'docker logs vpn-bot 2>&1 | grep -c "initData="'
  ssh entry 'docker logs vpn-bot 2>&1 | grep -c "admin_token="'
  ```
  The admin tokens expired within the hour. A logged initData stops working a day after it was signed, so a day after this deploy every one in the old logs is dead. Before the deploy, only a bot-token change (dashboard «Токен бота», §54) kills them.
- **Caddy on exit:** its dashboard site had no `log` directive when §12 was written. `_init_sub_fetches` in `bot/core/database.py` mentions JSONL logs in `/var/log/caddy/`. Wherever a Caddy site logs, the URLs of old pages and of the WebSocket are in that log too.
- **Hermes:** the skills `billing-ops` and `incident-response`, and the agent's own `hermes/AGENTS.md`, call the admin API with the header. `scripts/deploy_hermes_skills.sh` installs them.
- Tests:
  - `tests/unit/test_access_log.py`;
  - `TestValidateInitData` in `tests/unit/test_web_server.py`: the day, a date ahead, a missing or non-numeric `auth_date`;
  - `tests/integration/test_access_log_redaction.py`: the real server on `make_runner()` with the header, the URL, the WebSocket, the Referer and the token paths, where no access-log record holds a credential;
  - `TestCredentialsStayOutOfUrls` in `tests/e2e/test_dashboard_playwright.py`: Chromium sends the header on every call, the Referer is clean, and the Mini App's initData goes in the header.
