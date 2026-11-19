# §56. The hy2 auth relay on exit: hysteria keeps letting users in while the bot is down (2026-11-15, plan F, A6)

Both hysteria instances asked the bot on every new connection (`/api/hy2/auth`, `/api/hy2t/auth` over the pinned hop, §53). While the bot did not answer (15–30 s per deploy, longer in an outage), hysteria refused every new hy2 and hy2t connection.

**Deployed 2026-11-16, on the owner's word.**
- 18:52 UTC: the bot with `/api/hy2/keys` (6a0eef9).
- 18:56 UTC: the relay on exit.
- 19:17:43 UTC: both hysteria `url:` lines ask the relay. Backups: `/etc/hysteria/*.yaml.bak-a6-20261116-190751` and `-191735`, both holding the hop URL.
- Not yet checked: the memory while the bot is down. The next ordinary deploy checks it (runbook step 4).

Hysteria's configs change only on the owner's word: a hysteria restart makes every hy2 user reconnect.

- **What runs where.**
  - Entry, the bot: `GET /api/hy2/keys`, behind the exit's token (`_dpi_token_ok`). It answers `{"ok", "hy2": {hash: id}, "hy2t": {hash: id}}`: the keys each callback lets in right now.
    - The hash is `bot/services/hy2_keys.py:key_hash`, sha256 over a fixed prefix and the key. The prefix keeps it apart from the SSH exit's `n<12 hex>` names, which hash the uuid too.
    - The gates are the live callback's own: `hy2_keys.allows` (status, expiry, tier) and `_hy2_quota_denies` (the cached panel reading). The list starts no panel lookup.
    - A uuid two users rows share is left out: the live lookup takes whichever row sqlite returns first.
    - A read failure answers 503, never an empty list.
  - Exit: `hy2-auth-relay.service` runs `scripts/hy2_auth_relay.py` on `127.0.0.1:9979`. It is stdlib only, tested on Python 3.11 and 3.12; exit runs 3.13.
    - hysteria's `auth.http.url` becomes `http://127.0.0.1:9979/api/hy2/auth`; turbo's, `/api/hy2t/auth`.
    - The relay asks the bot over the pinned hop with the reporter's settings (`BOT_URL`, `BOT_CA_FILE`, `DPI_REPORT_TOKEN` from `/etc/exit-dpi.env`). The pin is the only trust anchor; TLS 1.3.
    - It keeps its connections to the bot alive: a new TLS connection per auth would add two round trips exit → entry to every hy2 handshake (the 2026-09-30 "slow hysteria" complaint).
- **What the relay answers.**
  - When the bot answers (HTTP 200, a boolean `ok`, an id for an allow), that answer goes back as it is. The relay never answers from memory while the bot answers.
  - When it does not (refused, 3 s to connect or 6 s in all, TLS refused, 5xx, junk), the relay answers from memory, per route. Hysteria gives the callback 10 s.
    - Memory holds the allows the bot gave, each counted from the moment the bot was asked, and the bot's key list, counted from the start of its fetch. The list is fetched every 60 s, every 10 s while that fails.
    - Either counts for 15 min (`MEMORY_S`, A4's bound). `HY2_RELAY_MEMORY_S` in `/etc/hy2-auth-relay.env` may only lower it; 0 = no memory, the relay only passes requests on.
    - A deny from the bot drops the key on that route; a deny on hy2 drops it on hy2t too (turbo lets in a subset). The newer word wins: a list never overrides what the bot answered after the list was asked for.
    - A key the bot never let in is refused, as before. So a banned or downgraded key gets in from memory at most 15 min after the bot last let it in.
  - Why a list and not only the allows: users authenticate a few times a day (4–8 allows per user, §29). Most new connections in a deploy's window come from keys the bot has not seen for hours.
  - After a refused or timed-out connect, the relay answers from memory for 5 s (`SKIP_S`) without asking. During an entry blackhole a handshake then does not wait 3 s each time. Any answer from the bot, the list's fetch included, ends that window.
  - Allows from memory are not in the bot's `hy2_auth_log`; `journalctl -u hy2-auth-relay` has each, with the id and the age.
  - An over-quota key the bot's cache had not seen gets in from the list during an outage, as on the first connect after a bot restart. hy2-traffic-collector kicks it.
- **Tunnel users cannot reach it.**
  - It binds loopback addresses only. An IPv4-mapped one is refused: Pythons disagree on whether it is loopback (3.11.18 yes, 3.12.3 no).
  - Through hysteria, the §52 ACL rejects every spelling of loopback. Users of xray and of the SSH and xhr exits are in network namespaces of their own.
  - The stand shows it: with the ACL, `127.0.0.1`, `localhost`, a name resolving to 127.0.0.1, `0.0.0.0`, `[::ffff:127.0.0.1]` and `127.1.2.3` are all `rejected`, and the server's own address gets `connection refused`. Without the ACL the first five reach the relay. The runbook's switch refuses a config without `reject(127.0.0.0/8)`.
- **The unit** (`systemd/hy2-auth-relay.service`).
  - `DynamicUser`, no capabilities, `ProtectSystem=strict`, ordered before both hysteria units.
  - `Restart=always` with `StartLimitIntervalSec=0`: while it is down, hysteria refuses every new hy2 connection, so it never stops restarting.
  - It runs a root-owned copy in `/usr/local/lib/nekovpn-hy2-relay/` with `python3 -I`, never the rsync target (§36).
  - `scripts/install_hy2_auth_relay.sh` installs it and fails when the relay does not stay up. It never touches hysteria, and `--remove` refuses while a hysteria config still asks the relay.
  - Re-run the installer after the script changes, while the bot answers: a restart empties the memory (the list refills it within a second), and new hy2 connections are refused for that second.
- **Watched.**
  - `curl -s 127.0.0.1:9979/status` on exit: counts and ages, no key, hash or id.
  - The healthcheck (§50) pages `healthcheck:hy2_relay` (critical) when a running hysteria asks the relay and the relay is `inactive`, `failed` or `activating` (its restart loop). A state or config it cannot read keeps the alert as it is. Layer C sends only whether each config asks the relay (`auth_relay`), never the URL.
  - After deploying, re-run `install_protocol_healthcheck.sh` on entry: the timer runs a root-owned copy.
- **Rollout and rollback:** `docs/runbooks/hy2_auth_relay.md`; its file-editing blocks run in the tests.
  - Order: the bot (deploy), then the relay on exit (users unaffected: hysteria still asks the bot), then hysteria's two `url:` lines and a restart in a quiet hour. The check that memory works is the next ordinary deploy.
  - **Judge a hysteria restart by a fresh client**: the runbook's check connects a new hysteria client on exit to each instance over loopback, with the probe account's key.
    - Never judge it by the probe through probe-proxy. The probe's long-lived QUIC connection to the old process is dead until the idle timeout (about 30 s for sing-box; the server's `quic.maxIdleTimeout` is 60 s), so the probe reads `000` while the relay works.
    - On 2026-11-16 the first switch was rolled back after a minute for exactly that.
    - The client's SNI must be a name in the server's certificate: with no matching SNI, hysteria 2.9's `sniGuard` (default `dns-san`) answers `tls: internal error`.
    - Real clients take the same 30–60 s to reconnect. In the evening hy2 sees 0–8 connects per 10 min, so a minute without an allow proves nothing.
  - Rollback: the hysteria configs from their `.bak-a6-<ts>` and a restart; the §53 drop-in stays, so the hop URL works at once. Then the relay.
  - The relay also works with a bot that has no `/api/hy2/keys` (404): then only the live allows count.
- **Tests:**
  - `tests/unit/test_hy2_auth_relay.py`: the memory, the parsing, the hop against servers of the test's own (the pin, keep-alive, a dropped connection, refused, hanging and silent bots), the listener, the config, the process.
  - `tests/integration/test_hy2_auth_relay.py`: the key list on a real sqlite equals what the live callback lets in, over a 21-user population; the relay in front of the real bot over TLS; the runbook's blocks.
  - `tests/unit/test_install_hy2_auth_relay_sh.py`: the installer with a fake `systemctl` and tripwires first in PATH, run first in `nekovpn-tests:py311`.
  - `TestAuthRelay` in `tests/unit/test_healthcheck_exit_guard.py`.
  - Stand `tests/integration/test_hy2_auth_relay_stand.py` (`requires_docker`): hysteria 2.9.2 → the relay → the real bot. Bot up: allow, and an unknown key refused by the bot. Bot stopped: the remembered key and a key only the list held pass, an unknown one does not, and past the bound neither does. Plus the ACL part above.
  - 81 mutants killed: the memory's bound, newer-word and deny rules; the parsing; the hop's keep-alive, retry, timeouts, pin and memory-only window; the listener and config checks; the key list's gates and token; the healthcheck verdict; the installer, the unit and the runbook's guards. Each ran on a fresh bytecode cache and was restored by positional replacement with a hash check.
  - Four survived the first run and got tests. One showed that `users.uuid` is `UNIQUE` (a second save replaces the row), so the shared-uuid guard in `read_rows` is tested on a table of an older schema.
