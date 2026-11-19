# §61. A new connection to exit every minute: the reach check (2026-11-19)

The probes saw exit's IPv4 block 45 minutes after it began (§59): they run every 15 minutes and judge three runs. `check_exit_reach` (AlertManager, every 60 s; `bot/services/exit_reach.py`) opens a fresh connection each minute from the bot container. Its traffic leaves by entry's address, like haproxy's.

- **The connection** is the one haproxy makes for a user: TCP to exit's Reality port (443), a PROXY v2 header, a TLS handshake with `SNI_VALUE`. Reality does not know the client, so it completes the handshake with its dest's certificate. Data has to move both ways, so a block that lets TCP connect and then goes silent (reported for a RU → abroad cascade on 2026-11-18) fails at the TLS step, as «TCP есть, TLS молчит».
- **Paths:** exit itself (`EXIT_NODE_IP`) and every transit in `EXIT_TRANSITS`.
  - Format: `name=ip:port,…`. The host must be an IP literal, because a name would need a lookup with no timeout; IPv6 goes in brackets.
  - On entry: `EXIT_TRANSITS=de=212.60.153.208:9441`. Compose passes it. Entry's compose is hand-kept, so the line goes there by hand.
- **The marker.** The PROXY header names `192.0.2.44` (RFC 5737 TEST-NET-1) as the source, and exit's xray logs each handshake as a rejected Reality connection from it.
  - `_dpi_exit_rows` drops those lines and counts them as seen, so they do not land in the geo-less remainder either.
  - Without the marker the check would add two rejects a minute to the `*TUNNEL*` bucket, and could trip `dpi_hsfail` along with a DPI agent turn.
  - When the reporter's top-20 cut leaves the marker out, its count lands in the geo-less bucket. The DPI alerts skip that bucket.
- **Alerts** (`reach:`):
  - **When:** after two failed minutes in a row.
  - **Once per incident.** `Alert.repeat=False` (new) means no repeat every 2 h while the key stays open: a block can last days, and protocol_down already pages for users. The recovery is posted (`reach:` is in `RECOVERY_PREFIXES`), and the next incident pages again.
  - **`reach:exit`**, critical, with three titles:
    - «Адрес exit … не принимает соединения из России»: a transit passes, so exit is alive (the signature of 11-18);
    - «…, exit жив»: no transit passes, but exit's last report is under 15 minutes old;
    - «exit … не отвечает: ни соединений из России, ни отчётов»: neither.
  - **`reach:via:<name>`**: critical while the direct path is closed too; warn (dashboard only) when only the spare is broken.
  - **The detail** has a line per path, saying what failed (TCP timeout, refused, reset, TLS silent, TLS error), plus the age of exit's last report.
- **Cost:**
  - two handshakes a minute, about 6 KB each;
  - the tick waits for the slowest connection, at most about 12 s (a 4-s connect, then 4-s handshake reads);
  - exit's error.log gains two lines a minute.
- **/protocols** shows the last check: «🛣 Пути к exit (N с назад): ❌ напрямую: … · ✅ через de: проходит, 107 мс».
- **Deploy:** the bot, plus `EXIT_TRANSITS` in entry's `.env` and in its hand-kept compose. While 84.75.76.109 stays blocked, the first ticks after the deploy post «Адрес exit 84.75.76.109 не принимает соединения из России» once, and nothing more until the address opens.
- **Tests:** `tests/integration/test_exit_reach.py`, real sockets on 127.0.0.1 and a real AlertManager tick on a real sqlite. Covered:
  - the header's bytes and the SNI;
  - each way a path fails;
  - every verdict branch and the report's freshness edge;
  - one page per incident, then the recovery and the next incident;
  - no page after a restart, and the spare on the dashboard;
  - the marker kept out of the DPI rows.
