# §67. The NekoVPN app's network diagnostics, `/start app` and the new settings fields (2026-11-20, NEKO6)

The bot's side of NekoVPN 0.9.x-neko.6 (`docs/NEKO6_PLAN.md`, kept in the main checkout): D2, the diagnostics intake; D3, `/start app`; D1, the new fields of the signed settings. The app's side is the plan's C1–C5.

## D2. `POST /api/neko/netstat`

With «Помогать искать блокировки» the app reports from where it is whether our channel works. It sends `drop` when the channel does not work, and `ok` once an hour while the VPN is on.

- **Two stages.**
  - Stage 1: the network, the operator and the signal. Every app sends it unless the signed settings say `flags.netstat: false`.
  - Stage 2: the serving cell and a 5-character geohash (about 5 km). Only with the user's own switch.
  - The data goes to entry, and only through the tunnel.
- **The token.** `Authorization: NekoToken <the /sub token>`, in that form only (`netstat.token_of`).
  - No header, the header twice, another scheme, the scheme alone, or anything after the token: 401 with `WWW-Authenticate: NekoToken`. The scheme's name may be in any case.
  - A token of anyone without an active key (demo / paid / support_topic, the gate of /sub): 401.
  - Why Authorization: Caddy's access log, and most others, leave it out. The dashboard's NekoAdmin header works the same way (§65).
  - `X-Neko-Token`, the contract's first draft, is not read.
- **Answers:**
  - 204: stored.
  - 400: a bad body. The text names the field: `Bad body: events[3].rat: one of gsm, lte, nr, umts, unknown`.
  - 401: see the token.
  - 405: any method but POST.
  - 413: over 64 KB, by Content-Length before anything is read, then by what arrives.
  - 429: over 30 requests an hour per token, with `Retry-After`. Every request with a good token counts, a 400 included; a refused one does not.
- **The body** (`bot/services/netstat.py`) is read strictly:
  - `{v: 1, app: "x.y.z-neko.n", events: [1 to 100 events]}`. An unknown field anywhere, NaN, or a field given twice: 400. A new field needs this server first, or a new `v`.
  - Required: `ts`, `kind` (`drop` / `ok`), `net` (`wifi` / `cell` / `other`), `ours` (bool). `ts` is unix seconds below 2^32, so a millisecond stamp is a 400.
  - Optional, null when missing: `mcc` (3 ASCII digits), `mnc` (2 or 3), `rat` (`gsm` / `umts` / `lte` / `nr` / `unknown`), `level` (0–4), `ru` (bool), `proto`, `cell`, `area`.
  - `proto`: up to 16 names (`[a-z][a-z0-9_-]{0,15}`), each true or false.
  - `cell`: only `tac` (up to 2^24−1), `cid` (up to 2^36−1), `pci` (0–1007) and `earfcn` (0–3279165), each an integer or null. Android's «unavailable» (`Integer.MAX_VALUE`) is out of range, so the app sends null.
  - `area`: a geohash of 5 characters.
  - A bool or a float where an integer goes: 400.
  - `ts` is floored to 10 minutes here, as on the phone. An event older than 14 days, or more than an hour ahead of the bot's clock, is valid but not stored.
- **What is kept.** `netstat_events`, created on first use (not in `init_db()`), one row per event: `ts`, `kind`, `net`, `mcc`, `mnc`, `rat`, `level`, `ru`, `ours`, `proto` (compact JSON), `cell_tac`, `cell_cid`, `cell_pci`, `cell_earfcn`, `area`, `app`.
  - No token, no chat_id, no address.
  - The ids are random, so the rows of one request are not a run of ids that would tie one phone's events together.
- **Nothing in the logs.**
  - The handler never reads the caller's address or a forwarding header.
  - `bot/utils/unlogged.py`: the handler marks its own request, a context variable. A filter on `aiohttp.access` and `aiohttp.server` drops that request's records, whatever the access logger, §65's included. aiohttp logs a request from that request's own task, so the mark reaches no other request, not even the next one on the same connection.
  - Every method reaches the handler, so not even a 405 is logged.
  - The handler catches every error, so aiohttp never logs «Error handling request from <address>». Its own failure line has the token replaced by `[token]`.
  - The rate limiter keeps an HMAC of the token, under a key that lives in the process only.
- **Retention.**
  - Job `netstat_cleanup` (NotificationService): daily, first run 15 minutes after a start, because with a deploy a day a bare 24 h interval never comes round. It drops rows older than 14 days.
  - The daily backup leaves the table out (§58): `BOT_DB_LEFT_OUT` in `scripts/service_backup.py`. The table is dropped from the copy in the container, its pages zeroed (`secure_delete`), and the file rebuilt (`VACUUM`). The live database is only read. A restored bot creates the table anew.
- **Outside the bot, on the owner's word:**
  - entry's nginx (`decoy.conf`; `/api/neko/` goes to the bot with `X-Forwarded-For`) writes its own access log with the client's address. Add `access_log off;` in a `location = /api/neko/netstat`.
  - Caddy on exit: a site that reaches the bot and has a `log` writes the address. It leaves Authorization out by default, so do not set `log_credentials`.
  - The backup's change takes effect once `scripts/install_service_backup.sh` is re-run on entry.
- **Not decided.** A lapsed payer, or a demo user with no traffic left, passes the token check, as on /sub. Their `ours: false` is about their own key, not about blocking.

## D3. `/start app`

`t.me/<bot>?start=app` is where the app's «Нужна подписка» window sends people, next to `/start sos` (§41) and `/start buy` (§46).

- A key holder (demo / paid / support_topic with a uuid) gets one short message, in Russian or English, whose button is the key card's «📲 Открыть в NekoVPN»: `neko_client.open_page_url`, `/open/<token>` on `SUB_PUBLIC_URL` (§38, §60). From that page the app adds the subscription itself.
- The button shows on any platform the bot last recorded: the link comes from the app itself.
- Anyone else, and a key holder while the bot knows no public address, gets the plain /start. For a new user that is the welcome with «Запросить демо».
- The button's text lives in `neko_client.open_button_label`, shared with the key card and /sub.

## D1. The settings fields

`scripts/neko_settings.py` validates them before it signs (§40):

- `bypass_apps`: Android package names, `^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)+$`, none twice, at most 300.
- `mail`: null or the service mailbox, at most 254 characters, with nothing that would end the address or add a header to the app's `mailto:` link.
- `flags.netstat`: a bool.

`bot/neko/settings.source.json` and the signed file are unchanged. The list goes in at signing time, on the owner's word.

## Fixed on the way

- The app's SOS report (§41) read only the first piece of a body that came in pieces, and answered 400. `bot/utils/request_body.py` now reads a body to its end, for the SOS report and for netstat.
- `_reset_demo_quota_db_fallback`, the monthly demo reset's path for a local panel database, used `json` without importing it. No inbound's client list was ever renewed on that path. Entry is API-only and never takes it.

## Tests

- `tests/integration/test_neko_netstat.py`, on the real server at 127.0.0.1 and port 0 with a real sqlite file:
  - every answer and every field;
  - the 10-minute floor and the 14-day window;
  - a body that arrives in pieces;
  - the table's columns and its values;
  - every log record checked for the token and the address;
  - the next request on a keep-alive connection still logged;
  - the cleanup and its job.
- `tests/integration/test_start_app.py`.
- `TestNeko6Fields` in `tests/integration/test_neko_settings.py`.
- `TestBotDbCopy`, and the run with `netstat_events` in the live bot.db, in `tests/unit/test_service_backup.py`.
- The chunked report in `tests/integration/test_neko_app_sos.py`.
- `TestDemoResetDbFallback` in `tests/unit/test_notifications_helpers.py`.

108 mutants were made by hand, each undone by restoring the exact text at its position, and all are killed. One, a 25-minute skew, survived the real-clock window test only when the request landed late in its 10-minute bucket; `test_the_window_edges_on_a_fixed_clock` now kills it.
