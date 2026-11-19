# §59. Exit's IPv4 blocked from Russia; entry reaches exit through the DE reserve (2026-11-18)

From 13:10–13:20 UTC on 2026-11-18, no new connection from entry reaches exit's address 84.75.76.109. Every protocol went dark at once: the last live probes were at 13:09–13:10. With them went the bot's panel calls (the dashboard showed nobody online), the bot's Telegram proxy and the agent's model proxy.

- **How it was told apart from an outage** (all read-only):
  - Nothing new opens from entry to exit: ICMP and TCP on every port, 22 and 22022 included. Flows opened before the block keep working, which is why the `ssh` / `ssh-entry` probes stayed green: mihomo holds one SSH connection.
  - exit → entry works both ways (ping, HTTPS to :8448). So entry's answers to exit-initiated flows do reach exit: the filter is stateful, not a route.
  - A TTL trace to exit's gateway 84.75.76.1 (same provider, dataforest AS215607) reaches it in 5 hops, and .110 answers every ping. To .109 nothing comes back after entry's first hop (130.49.146.2). A sniffer on exit (Python AF_PACKET; exit has no tcpdump) sees no SYN from entry.
  - Conclusion: the TSPU at entry's provider (AdminVPS, AS211183) drops new flows to that one address. DE and other countries reach exit as before. exit's IPv6 (2a0c:4ac1:15:6b::a) answers DE in 14 ms, but entry has no IPv6.
- **The transit** (owner's OK; nothing changes for clients, whose profiles name entry):
  - **DE** (`/root/nekovpn-transit/apply.sh`, `remove.sh`, unit `nekovpn-transit.service`, enabled): DNAT from entry's address only, SNAT (MASQUERADE) to DE's own address, two ACCEPT rules in `DOCKER-USER`, all tagged `nekovpn-transit`. Port map: TCP 9441→443 (Reality), 9442→8444 (stls), 9453→2053 and 9454→2054 (ws), 9426→2026 (panel), 9422→22022 (SSH exit), 9423→22 (exit's sshd, for the healthcheck); UDP 8400→8400 (hy2) and 8402→8402 (hy2t). Entry's DNAT keeps those target ports, which the healthcheck's `entry_dnat` check expects. 9400→8400 and 9402→8402 were the first hy2 ports; they stay for flows opened before the move. `nf_conntrack_max` was raised from 8192 to 65536 (hashsize 16384). Firewall backup: `/root/nekovpn-backups/iptables-before-transit-20261118-155305.rules`.
  - **exit**: `exit_firewall.sh` reads `TRANSIT_SOURCE_IPS` from `/etc/nekovpn-transit.env` and gives each address entry's :443 allowance, right before the DROP. Without it Reality through DE is dropped. Backups: `exit_firewall.sh.bak-transit-20261118-155410`, `/root/iptables-before-transit-20261118-155410.rules`.
  - **entry**:
    - haproxy's Reality backend → `212.60.153.208:9441`;
    - shadow-tls `--server 212.60.153.208:9442`;
    - the seven hy2/hy2t/ws DNAT rules → DE (hy2 and hy2t on DE's 8400/8402, ws on 9453/9454), live and in `rules.v4`, plus MASQUERADE and FORWARD rules for DE; the exit ones stay for the way back;
    - root's `~/.ssh/config`: `exit-node` → DE:9423 with `HostKeyAlias 84.75.76.109`, so the healthcheck's layer C (and `ssh exit-node`) reaches exit again;
    - ssh-relay → `212.60.153.208:9422`;
    - the bot's `.env`: `XUI_API_URL=http://212.60.153.208:9426`, `TG_PROXY_URLS` and `HTTPS_PROXY` with DE's tinyproxy first, `NO_PROXY` += DE;
    - Hermes' `HTTPS_PROXY` → DE's tinyproxy (it had no second proxy).

    Backups are `*-transit-20261118-*` / `*-exitblock-*` in `/root/nekovpn-backups/` and next to Hermes' `.env`. `EXIT_NODE_IP` and `SSH_EXIT_HOST` keep exit's address: exit still reaches entry from it, and users abroad still reach the SSH exit there.
  - Not moved: xhr (switched off in the cascade) and the `ssh` probe (it is the direct path, and that is dead from Russia).
  - **Since 2026-11-19 such a block pages in about 2 minutes:** the reach check (§61) opens a new connection to exit every minute, directly and through the transit.
  - **Undo** (once exit has an address entry reaches): put the backups back on entry (haproxy reload, shadow-tls and ssh-relay restart, `rules.v4` and the live rules, `.env` plus `docker compose up -d --no-deps vpn-bot`, root's `~/.ssh/config`). Then run `remove.sh` and `systemctl disable nekovpn-transit` on DE, and on exit remove `/etc/nekovpn-transit.env` and `systemctl restart exit-firewall`.
- **Through a transit, hysteria and xray see the transit's address, not entry's.** The hy2 auth callback and the exit report only knew `ENTRY_NODE_IP` / `EXIT_NODE_IP` as "ours", so every hy2 auth stamped DE's network on its user (`users.last_*`), and the exit report filed DE as a country of users. Both now ask `WebAppServer._is_own_address`, the /sub guard (node settings including `FALLBACK_NODE_HOST`, plus internal space). Tests: `TestTransitHostsOnTheOtherPaths` in `tests/integration/test_sub_own_address.py`; 4 mutants (the old two-address check on each path) killed.
