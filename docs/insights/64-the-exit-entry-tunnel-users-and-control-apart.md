# §64. The exit → entry tunnel: users and control apart, open-file limits, the watchdog (2026-11-20)

Since 2026-11-19 entry cannot open a connection abroad (§62, §63). Exit dials in instead and forwards its ports over SSH; the server speaks first (`sf.py` as ProxyCommand), the only kind of TCP that passes on that way in. The tunnel was set up by hand on prod and is not in the repo; this section is its description. The code side is the healthcheck's watchdog below.

- **Two connections from exit since 2026-11-20** (units on exit, both `Restart=always`, `LimitNOFILE=65536:524288`):
  - `nekovpn-revssh`, users' traffic: `-R` 19441 → exit :443 (Reality), 19442 → :8444 (stls), 19422 → :22022 (ssh-exit), 19460 → :19460 (hy2 through wstunnel: the container `nekovpn-hy2wst` on entry, its server on exit).
  - `nekovpn-revssh-ctl`, control: `-R` 19888 → tinyproxy :8888 (the bot's Telegram), 19426 → the panel :2026, 19423 → exit's sshd; `-L` 18448 → entry's 127.0.0.1:8448, the hop. Exit still dials 130.49.146.10:8448; this unit's `ExecStartPre` keeps the REDIRECT of that address into 18448 (comment `nekovpn-revssh-hop`).
  - On entry haproxy fronts them for the containers: `bot_tgproxy` 172.17.0.1:19888, `bot_panel` :19426, `bot_reach_tun` :19441.
- **What happened on 2026-11-19/20.** Entry's sshd session of the then single tunnel reached its open-file limit, 1024:
  - 1008 of the open files were hy2 sessions. In UDP mode wstunnel opens one websocket per UDP source: one SSH channel and one open file at each end. With `timeout_sec=0` it never closed any of them, and hy2 hops ports, so every hop is a new source.
  - From 17:16 UTC on 19.11, new connections on every forwarded port waited in the accept queue (66 on :19888). The bot lost Telegram from 18:00, so no alert went out for ten hours. stls and hy2 were dead; Reality worked only when a slot came free.
  - Found at 04:28 UTC on 20.11. Entry's new address was open from Russia all along (Globalping 17/17).
- **Fixed 04:37–04:52 UTC on 20.11:**
  - `nekovpn-hy2wst` runs with `timeout_sec=60`. A session without a packet for a minute is closed; hysteria's keepalive is about 10 s, so a live session never idles that long.
  - Limits. A root session on entry takes its limits from PAM (`pam_limits`), not from `ssh.service`'s `LimitNOFILE`, so `/etc/security/limits.d/90-nekovpn-tunnel.conf` sets root's `nofile` to 65536 / 1048576. `ssh.service.d/10-nekovpn-nofile.conf` raises the listener. On exit, both units carry the same `LimitNOFILE`.
  - The control traffic moved to its own connection (`nekovpn-revssh-ctl`). A jam in the users' tunnel no longer takes the bot's Telegram and alerts, the panel or the hop with it. A restart of `nekovpn-revssh` drops users for a few seconds and leaves the bot alone.
- **The watchdog:** `scripts/protocol_healthcheck.py`, layer B, on its 15-minute timer. `collect_entry_tunnels` finds every sshd process on entry that holds a loopback listener. `entry_tunnels` judges each one:
  - `healthcheck:entry_tunnel:fd`: a session holds half of its open-file limit or more. The title gives the count, the share and the ports.
  - `healthcheck:entry_tunnel:queue`: connections wait to be accepted on a forwarded port in both reads, 2 s apart.
  - Both are critical, because the bot sends only critical alerts to Telegram (`ALERT_TG_MIN_SEVERITY`). A host without such sessions has nothing to report. When `ss` fails, both keys are kept as they were. The bot judges the keys by entry's layer (`_finding_layer` in `bot/services/alert_manager.py`).
  - **What to do.** The alert's command shows which port holds the connections. If it is :19460 (wstunnel), `docker restart nekovpn-hy2wst` frees them at once; hy2 users reconnect within seconds. If one listener queues while its session's file count is low, the session hangs: restart its unit on exit.
- **hy2's ports on entry and the healthcheck's `entry_dnat:<tag>` (since 2026-11-20).**
  - Entry's `PREROUTING` REDIRECTs hy2's UDP (8400, 443, 20000:40000) to :19400 and hy2t's (8402, 40001:50000) to :19402. `nekovpn-hy2wst` listens there, and its `-L udp://0.0.0.0:19400:127.0.0.1:8400` carries the datagrams through the tunnel to hysteria on exit.
  - The check (layer B) used to know only the old form, a DNAT straight to exit. It paged `healthcheck:entry_dnat:hy2` critical on every run from 2026-11-19 while hy2 worked.
  - Now it accepts both forms. For a REDIRECT it reads who listens on the target port (`ss -Hulnp`). For wstunnel it also reads where the `-L udp://` forward of that port goes (`/proc/<pid>/cmdline`; only the forwards are kept). A listener that is not wstunnel counts as carrying the port.
  - It pages in three cases:
    - a port has no rule at all;
    - nobody listens behind a REDIRECT: the container is down, `docker start nekovpn-hy2wst`;
    - the relay forwards to another exit port.
  - When `ss` cannot be read, the key is kept as it was.
- **When the tunnel is rebuilt by hand:**
  - set a finite `timeout_sec` on every UDP forward of wstunnel;
  - keep each REDIRECT target equal to the local port of a wstunnel `-L udp://` forward to the right exit port (19400 → 8400, 19402 → 8402); the healthcheck checks the pairing;
  - give root's sessions on entry a `nofile` of at least 65536;
  - keep the bot's ports in their own connection;
  - after a change, run `systemctl show -p ExecStart` on exit and `ss -Hltnp | grep 127.0.0.1:19` on entry. Each process should hold only its own ports.
- **Tests:** `tests/integration/test_entry_tunnel_watch.py` covers:
  - the readers of `ss` and `/proc/<pid>/limits`;
  - the collector with fakes: two reads, a session that ended between them, `ss` failing;
  - both findings and their edges;
  - the wiring into the report and `unjudged`;
  - the bot side: the alert reaches Telegram, and it is kept while entry could not look.

  `tests/integration/test_entry_udp_relay.py` covers the UDP path:
  - the readers of `ss -Hulnp` and of wstunnel's argv;
  - the collector;
  - both forms and every gap;
  - `unjudged`;
  - the bot side: the open false alarm closes, and it is kept while `ss` could not be read.
