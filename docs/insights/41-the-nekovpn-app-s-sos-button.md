# §41. The NekoVPN app's SOS button: POST /api/neko/sos/<token> (2026-10-25, client plan E16/E23)

The app posts what it measured. The bot reports it like `/sos` (§33): a `user_failure_reports` row with target `sos_app`, which DPIMonitor R5 counts like any report, and one post to TOPIC_SUPPORT headed `SOS #id · приложение NekoVPN`. The post carries the usual facts plus `app_report_lines`: app version, platform, network, VPN on/off, profile age, settings version, and up to 30 proxy delays. The agent kick is the /sos one.

- **Answer** (`SosService.handle_app_sos`, `no-store`): `{status: reported|repeat|no_key, lines, emergency, report_id?}`.
  - `lines` are the SOS letter's plain «вы» status lines (`status_lines(rich=False)`) without the /sos-only "(шаг 2)".
  - `emergency` is the FlClash emergency profile URL.
  - One report per user per 10 min (`sos_app` row = limiter); a repeat still gets `lines` and `emergency`.
- **Gates:** the /sub token (404 / 410), a body of at most 16 KB checked before parsing (413 by Content-Length or by the read itself), JSON (400). Only known report fields are read, each cut short and HTML-escaped, because the body comes from the user's device.
- **`t.me/<bot>?start=sos`** (`/start sos`) runs `/sos`. The app offers this link when it reached no origin.
- **Tests:** `tests/integration/test_neko_app_sos.py` (real sqlite); 16 mutants killed with a positional restore.
