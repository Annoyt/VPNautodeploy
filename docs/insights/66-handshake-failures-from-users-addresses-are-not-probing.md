# §66. Handshake failures from users' own addresses are not probing: `dpi_hsfail` and `dpi_hsfail_users` (2026-11-20)

- **What happened.** From 2026-11-19 14:33 to 2026-11-20 07:42 UTC, exit's REALITY logged about 1,850 handshake failures. A read-only look at prod found where they came from:
  - Most came from addresses that also had normal sessions (≥ 20 KB) through entry's haproxy :8443 in the same hours. These are our users' clients.
  - The reason was almost always «handshake did not complete successfully».
  - The peaks matched the exit → entry tunnel outage (17:16 → 04:37 UTC, §64). The clients retried through a jammed tunnel.
  - Per network:
    - MegaFon AS31133: 1,455 failures. Two of its listed addresses were users', and about 1,500 of the short connections came from them.
    - Rostelecom AS12389: 103 failures, all from one user's address.
    - ER-Telecom AS56981: 164 failures, from one user's address and one unknown.
  - Real unknown sources were few: MTS (two addresses, 18 failures) and a couple of hosting scanners.
  - The alert read «Probing? RU/AS31133 hsfail N/h (X× baseline)» and helped switch Reality off.
- **Why it read that way.**
  - The exit reporter sent rejects per (kind, reason) with their top 20 addresses, and never linked a failing address to that address's logins.
  - The bot added the failures up per (country, ASN).
  - `probe_ips_json` also held the addresses of accepted connections. The dashboard listed our users as «🎯 probes».

## The exit reporter (`scripts/exit_dpi_reporter.py`, on exit)

- **The memory.** The reporter remembers every address that logged in as one of our users: an access-log line with a user's email.
  - These do not count: the probe client (`probe…`), the panel's api polling, loopback.
  - The memory lives in the state file as `user_addrs`, `{address: last login}`, and is kept for `USER_ADDR_MEMORY_H` hours (default 24). It holds at most 50,000 entries; above that the oldest go.
  - An IPv6 address counts by its /64, because a phone's privacy address changes inside it.
- **The `origin` of a reject.** A REALITY reject gets `origin: "user"` when its address is in the memory or logged in during the same window. Every other reject gets `"unknown"`.
  - A (kind, reason) seen from both is sent as two entries. Their counts add up to the one entry an older reporter sent, so an older bot adds them up as before.
- **Only REALITY.** Its address is the client's own: it comes from the PROXY header that entry's haproxy sends. ss2022 and the ws upgrade see a front that users and strangers share (entry's MASQUERADE, the tunnel's loopback, a Cloudflare edge). Their rejects are always `"unknown"`.
- **When the memory is saved.** It advances with the log offsets, only once the bot took the report, like the journal cursor. If the memory fails, the report still goes out in the old shape.
- **Known gap.** A client that fails from a new address before its first login there counts as unknown. So the error goes towards «probing», never towards hiding a prober.

## The bot

- **Two new columns in `dpi_metrics`:**
  - `hsfail_users` is the part of `handshake_fail_count` that came from users' addresses. NULL means the row was not told apart: an older reporter, the hy2 rollup, or rows from before the column.
  - `user_ips_json` holds the top 10 users' addresses that failed.
  - `handshake_fail_count` stays the total.
- **`probe_ips_json`** now holds only the failures' other addresses. An accepted connection's address never goes there.
- **Our own addresses** (the `*TUNNEL*` bucket) are never counted as users'. Every user and every scanner behind entry shares them.
- **`dpi_hsfail:<cc>:<asn>`** counts `handshake_fail_count − hsfail_users`: unknown addresses, plus rows not told apart. Its 7-day baseline is counted the same way.
  - Title: «Probing? RU/AS31133 hsfail 9/h from 1 unknown address (8.0× baseline)».
  - While the hour holds rows that were not told apart, the title ends «…, users not told apart».
- **`dpi_hsfail_users:<cc>:<asn>`** (new) counts the users' failures against their own baseline, with the same floor (5/h) and ratio (5×).
  - Title: «Users' handshakes fail, not probing: RU/AS31133 hsfail 500/h from 2 user addresses (168.0× baseline)».
  - Dashboard only, and no agent turn. It skips the tunnel bucket and the bucket without geo.
- **Address counts** are the distinct addresses in the hour's rows. «10+» means some row held its top-10 limit.
- **A tunnel jam.** This applies while a `healthcheck:entry_tunnel:*` alert (§64) is open, or after it closed within the alert's hour:
  - both titles end with «— during an entry tunnel jam»;
  - the detail names the key and its span;
  - the severity is warn;
  - no agent turn starts (`Alert.agent=False`, new; `_fire` honours it for every family).
  - `check_healthcheck` runs earlier in the same tick, so a jam it finds now already counts.
  - `reach:exit` does not count as a jam: it can stay open for days while exit's direct address is blocked (§61).
- **Unchanged.** DPIMonitor's R3 still counts the total. It asks whether a network's users can use Reality, and their failures are its signal: on 2026-09-01 it saw 879 failures and 0 connections. The heatmap totals and `inbound_drop` are the same too.
- **The other readers:**
  - The daily DPI summary says «hsfail: N (у наших юзеров: U)». The `dpi_reports` snapshot and the agent's payload carry `hsfail_users`.
  - In `/api/admin/dpi_metrics`, each operator gains `hsfail_users`, `hsfail_unknown` and `hsfail_not_told`. The DPI tab adds «👤 у наших юзеров: N — не зонд».
  - The `dpi-analysis` skill knows both alerts.

## Deploy (two places, in either order)

- **The bot:** `./scripts/deploy_to_entry.sh`. On start, the migration adds the two columns.
- **The skill:** `scripts/deploy_hermes_skills.sh`.
- **The reporter on exit,** the same way as in `docs/runbooks/entry_hop_tls.md`:

  ```bash
  scp scripts/exit_dpi_reporter.py vpn-exit:/usr/local/bin/exit_dpi_reporter.py.new
  ssh vpn-exit 'TS=$(date +%Y%m%d-%H%M%S); cp -a /usr/local/bin/exit_dpi_reporter.py "/usr/local/bin/exit_dpi_reporter.py.bak-$TS" && install -m 0755 /usr/local/bin/exit_dpi_reporter.py.new /usr/local/bin/exit_dpi_reporter.py && rm /usr/local/bin/exit_dpi_reporter.py.new && systemctl start exit-dpi-reporter.service; journalctl -u exit-dpi-reporter -n 3 --no-pager'
  ```

  The log line then reads «… rejects (N from users' addresses) …».
- **Mixed versions:**
  - An old reporter with the new bot: rows are not told apart, and the probing title says so.
  - The new reporter with an old bot: the totals are as before.
- **The memory starts empty.** For the first day after the reporter's deploy, an address counts as a user's only after its first login.

## Tests

- `tests/unit/test_exit_dpi_reporter.py`:
  - `TestUsersAddresses`: the address key, the memory, origins, REALITY only, the old shape;
  - `TestUsersAddressesInARun`: the state file, a failed POST, a broken memory.
- `tests/integration/test_hsfail_users.py` pins the 2026-11-20 shape end to end: the real reporter on log files, the real handler, real sqlite, the real checks and `AlertManager`. It also covers the ingest, baselines, titles, the jam, the dashboard, R3, the daily summary and the migration.
- `tests/integration/test_sub_own_address.py`: an accepted address is no longer in `probe_ips_json`.
- 27 mutants were killed and restored by reverse text replacement (§28).
