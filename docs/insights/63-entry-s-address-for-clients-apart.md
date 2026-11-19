# §63. Entry's address for clients, apart from the node's own: `ENTRY_PUBLIC_HOST`, `HOP_TLS_IP` (2026-11-19)

From about 00:00 UTC on 2026-11-19 every client-first flow to entry's address, 130.49.146.10, was dropped after the TCP handshake, from Russia and from abroad (§62). A neighbour in the same /24 answered all 17 of Globalping's Russian probes, so only that one address was listed. At about 12:25 UTC the owner pressed «Сменить» (change the main IP) in AdminVPS's panel. The change came through at 14:33 UTC: a reboot, the new address 157.22.231.40 (same AS211183) and a new SSH host key. The new address is open from Russia.

- **What a new entry address breaks.** All of these were fixed by hand the same day:
  - exit's tunnel (`nekovpn-revssh`) dials entry through the `entry-node` alias in `/root/.ssh/config`;
  - haproxy failed at boot: the bot's frontends on `172.17.0.1` cannot bind before Docker creates `docker0`. A drop-in, `haproxy.service.d/10-nekovpn-after-docker.conf` (`After=` and `Wants=docker.service`), fixes it;
  - hy2's wstunnel client listened on the address, and its NAT rules pointed at it. Now it listens on `0.0.0.0`, and the rules are `REDIRECT --to-ports`, so neither depends on the address;
  - the DNS records on the address (`radar`, `mqtt`, `cdn`);
  - `ENTRY_NODE_IP`: Reality and the other entry-address servers in every profile, and the probe configs.
- **`ENTRY_PUBLIC_HOST`** (env; compose passes it): where clients connect for the protocols built from entry's own address. These are Reality and its server-first twin (§62), the SSH path through entry (§51), the RU-zone egress (§36), xhr (§51) and the share links. It is an IP or a host name.
  - `bot/utils/public_url.py:entry_public_host` returns it, else exactly what the builders read before (`ENTRY_NODE_IP or ''`). Unset, every profile is byte for byte what it was.
  - hy2 and stls have host settings of their own (`HY2_HOST`, `STLS_HOST`); the subscription has `SUB_PUBLIC_URL` (§60). On prod all three name `radar.nekoweather.xyz`.
  - **With a name, the next move of entry's address is a DNS change.** Clients resolve the name when they connect, so nobody has to refresh a profile. With an IP, Reality comes back only with each user's next profile fetch, which is what happened on 2026-11-19.
  - An IP value counts as our own address for `/sub`'s network attribution (`_OWN_NODE_SETTINGS`). A name is skipped, never resolved.
- **`HOP_TLS_IP`** (env; compose passes it): the address on the exit → entry hop's certificate (§53), the one exit dials. Empty, it is `ENTRY_NODE_IP`.
  - `hop_tls.check` (the deploy smoke) compares the certificate with it (`hop_tls.hop_address`), and `scripts/hop_tls.py init` takes it as the default `--ip`.
  - Since 2026-11-19 exit still dials 130.49.146.10. Its `nekovpn-revssh` REDIRECT sends that address's :8448 into the tunnel, so Caddy, the hy2 auth relay and the reporter keep their pinned URLs. On prod `HOP_TLS_IP=130.49.146.10` and `ENTRY_NODE_IP=157.22.231.40`; without `HOP_TLS_IP` every deploy fails its smoke.
- **The next time entry's address changes:**
  1. exit: point the `entry-node` alias at the new address and restart `nekovpn-revssh`. The alias accepts a new host key; compare its fingerprint with the one entry shows.
  2. DNS: move every A record on the old address. acme.sh's Cloudflare token on exit can do it.
  3. entry `.env`: `ENTRY_NODE_IP` = the new address, then recreate vpn-bot (`--no-deps`). Regenerate both probe configs (`gen_probe_config.py`, `gen_probe_config_mihomo.py`) and restart the sidecars. `ENTRY_PUBLIC_HOST` stays the name, `HOP_TLS_IP` stays what exit dials.
  4. Check from Russia with Globalping: HTTPS from Russian eyeball and datacenter probes. A TLS timeout after a TCP connect is the block's signature; "connection refused" means nothing listens.
- **Tests:** `tests/integration/test_entry_public_host.py` and `TestCheck` / `TestCli` in `tests/integration/test_hop_tls.py`. They cover:
  - the helper;
  - byte-identical output with the setting unset, empty or equal to `ENTRY_NODE_IP`;
  - with a name: every entry server follows it, and the node's IP is gone from every output (Clash with the twins and the emergency path, sing-box, Hiddify, the providers, links, xray, the RU-zone, xhr, the share links);
  - the own-address list, the settings and compose;
  - the hop check and `init`.

  14 mutants killed, restored by a positional reverse replacement with a hash check (§28).
