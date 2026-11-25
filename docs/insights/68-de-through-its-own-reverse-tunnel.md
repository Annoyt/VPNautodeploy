# §68. DE through its own reverse tunnel: entry reaches the reserve again (2026-11-25)

Since the evening of 2026-11-18, entry (157.22.231.40) cannot open any connection to DE's address 212.60.153.208, nor to exit's 84.75.76.109 (§64). Exit got a reverse tunnel on 19.11, DE did not. On 2026-11-25 DE got one too. Like exit's tunnel, it was set up by hand on prod and is not in the repo; this section describes it.

## What is blocked, measured on 2026-11-25

**From entry to DE's address and exit's address, nothing passes:**
- no ping and no TCP on any port;
- not even an RST from a closed port;
- the path dies right after entry's gateway, 157.22.231.2.

**It is a block on these two addresses, not a cut-off from abroad:**
- the neighbours in their subnets answer from entry: 212.60.153.1 with ping and RST, 84.75.76.1 with ping;
- Cloudflare, GitHub, Fastly and Microsoft open from entry.

**The other direction is open.** From DE and from exit to entry, ping (44 ms), HTTPS on :8443, TLS on :443 and SSH all pass. On 2026-11-19 only server-first TCP passed that way.

**From Russia in general** (Globalping, TCP 443, 12 probes, mostly datacenters):
- DE opens from 9 probes. It fails from Mediasoft (Moscow), HOSTKEY (Tambov) and TOMICA (Tomsk, AS31357, an eyeball network).
- Exit opens from none.

**What the block cost until 2026-11-25:**
- **The bot's second Telegram proxy was dead.** That proxy is DE's tinyproxy. On 2026-11-25 the hosting powered exit off from 11:53 to 12:30 UTC, and the bot's alerts about it (`reach:via:tun`, `tg_api`) were not delivered: both of its proxies were out.
- **The DE panel was out of reach.** Paid users who arrived after 2026-11-18 got no DE key.
- **The de probe read DOWN for days.**
- **DE still allowed only entry's old address.** Its tinyproxy and ufw allow 130.49.146.10 only.

## The setup

**On DE:**
- Unit `nekovpn-revssh-de.service` (`Restart=always`, `MemoryMax=64M`) runs `ssh -N nekovpn-de@157.22.231.40`:
  - key `/root/.ssh/nekovpn_de_revssh`;
  - entry's host key pinned in `/etc/nekovpn-revssh/known_hosts` (`StrictHostKeyChecking=yes`);
  - the same server-first ProxyCommand as exit, `/usr/local/lib/nekovpn-revssh/sf.py`, copied from exit;
  - `ExitOnForwardFailure=yes`;
  - forwards `-R 172.17.0.1:19988:127.0.0.1:8888` (tinyproxy), `-R 172.17.0.1:19526:127.0.0.1:2026` (panel) and `-R 172.17.0.1:19541:127.0.0.1:443` (Reality).
- Nothing else on DE changed. DE hosts other people's services; don't touch them.

**On entry, the account:**
- User `nekovpn-de` with shell `/usr/sbin/nologin`. Its `authorized_keys` holds DE's key with `restrict,port-forwarding,command="/bin/false"`.
- `/etc/ssh/sshd_config.d/61-nekovpn-de.conf` sets `Match User nekovpn-de`:
  - `AllowTcpForwarding remote`;
  - `GatewayPorts clientspecified`;
  - `PermitListen` with exactly the three `172.17.0.1` ports;
  - no tty, agent, X11 or stream-local forwarding;
  - `ClientAlive` 15 s × 3.
- Root's settings did not change. A compromised DE gets three listening ports on the docker bridge and nothing else.

**On entry, the redirect:**
- Unit `nekovpn-de-dnat.service` (oneshot) runs `/usr/local/sbin/nekovpn-de-dnat.sh add|del|show`.
- The script holds nat DNAT rules with the comment `nekovpn-de-dnat`: DE:8888/2026/443 → 172.17.0.1:19988/19526/19541.
- The rules sit in `OUTPUT` for the host and in `PREROUTING` for traffic from 172.16.0.0/12, the containers.
- Everything that names DE stays as it was: `TG_PROXY_URLS`, `FALLBACK_NODE_XUI_URL`, the probe-proxy config (`scripts/gen_probe_config.py`) and `FALLBACK_NODE_HOST` in users' profiles. The bot was neither changed nor restarted.

## Checked on 2026-11-25

From vpn-bot:
- Telegram answers through each of the two proxies: 302 in 0.43 s.
- DE panel login works; the reserve inbound has 20 clients.
- probe-proxy :18086 → `generate_204` answers 204 in 0.46 s.

From the entry host, through DE's real address:
- the panel answers 200;
- tinyproxy answers 407 without credentials;
- Reality shows www.google.com.

## What it means and what is left

- **What the de probe measures now.** It measures DE's xray through the tunnel. It does not show whether users' networks reach DE: they connect to 212.60.153.208 directly. That needs the Globalping check above.
- **Not done:**
  - DE as a backup exit for users behind entry, when exit's tunnel is down. That is the owner's call.
  - The reach check through DE (`EXIT_TRANSITS` `de=…:9441`) still reads dark: 9441 is not in the tunnel.
  - Leftovers on DE: `nekovpn-transit.service` (the DNAT transit of §59) and the tinyproxy and ufw rules for 130.49.146.10.
- **Rollback:**
  - DE: `systemctl disable --now nekovpn-revssh-de`.
  - entry: `systemctl disable --now nekovpn-de-dnat` (its `ExecStop` removes the rules), then remove `61-nekovpn-de.conf` and run `systemctl reload ssh`, then `userdel -r nekovpn-de`.
- **Troubleshooting:**
  - entry: `ss -ltnp | grep 172.17.0.1:19` should show sshd on 19988, 19526 and 19541. Also `journalctl -u ssh | grep nekovpn-de` and `/usr/local/sbin/nekovpn-de-dnat.sh show`.
  - DE: `journalctl -u nekovpn-revssh-de`.
  - If DE is down, its forwards are gone: connections to DE's address from entry then fail at once, where before the tunnel they hung until a timeout.
