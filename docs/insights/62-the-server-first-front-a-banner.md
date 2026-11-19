# §62. The server-first front: a banner first, for the NekoVPN app (2026-11-19, IMPROVEMENT_PLAN G4)

Since about 00:00 UTC on 2026-11-19 users in Russia reach entry's address but none of their data arrives. The TCP handshake completes, and then the client's first bytes are dropped: a Reality or ShadowTLS ClientHello, hy2's QUIC packets, plain HTTPS to the subscription address. From 01:00 UTC no user reached exit at all. The probes stayed green because they run on entry itself.

- **What passes, checked from the owner's machine in Russia (ООО «СЕТ», 2026-11-19).**
  - A connection in which the server speaks first passes both ways. `curl telnet` to :22: sshd's banner arrives, a typed `hello` arrives (sshd logs «invalid format»), and sshd's answer comes back.
  - A client that sends first does not. `ssh -v -p 22022` (OpenSSH sends its version at once) times out in `kex_exchange_identification`, although the relay answers from entry in 148 ms.
  - TLS after a server's line passes. A temporary front sent an SSH-like banner, then piped to haproxy :8443. A .NET TLS client read the banner, then did TLS 1.2 to the decoy and fetched 50 332 bytes in 168 ms. The same client straight to :8443 timed out.
- **The front** (`scripts/server_first_front.py`, stdlib asyncio, config `/etc/nekovpn-sf-front.conf` read as data):
  - `route = LISTEN TARGET [proxy-v2]`, `banner`, `max_conns`; anything else is refused.
  - Per connection it opens the target (5 s), sends a PROXY v2 header with the user's address on a `proxy-v2` route, writes the banner line, then pipes byte for byte. A clean end is passed on as a half-close.
  - A dead target or a full front gives the client no banner, so the app moves on at once.
  - Nothing is logged per user: a summary line every five minutes.
- **The routes on entry** (`systemd/nekovpn-sf-front.conf.example`):
  - `0.0.0.0:2443 → 127.0.0.1:12443 proxy-v2`: haproxy's `bind 127.0.0.1:12443 accept-proxy` in `frontend xray_in` (not 18443: the decoy nginx listens there). It routes by SNI exactly like :8443 (Reality, xhr, the decoy), and its `send-proxy-v2` gives exit the user's address.
  - `0.0.0.0:2444 → 127.0.0.1:443`: shadow-tls.
- **Unit and installer.** `systemd/server-first-front.service` runs a root-owned copy with `python3 -I`, `--check` first, DynamicUser, no capabilities, `Restart=always`. `scripts/install_server_first_front.sh` copies the script into `/usr/local/lib/nekovpn-sf-front` (never the rsync target, §36). It writes the config from the example only when there is none, and never touches haproxy or shadow-tls. For a `proxy-v2` route it looks for `bind <target> accept-proxy` in haproxy's config, not just a listener: the first draft pointed at 18443, where the decoy nginx already listens. It prints the bind line haproxy still needs, says when something else holds the port, and asks for a reload when the line is there but nothing listens yet. Re-run it after the script changes.
- **The profile.** Settings `SF_REALITY_PORT`, `SF_STLS_PORT`, `NEKO_SF_MIN_VERSION`; compose passes them (entry's compose is hand-kept: add the lines there).
  - A NekoVPN app at or above the minimum version (`SubscriptionService.server_first_for`, by User-Agent) gets, in its Clash profile, a twin of each Reality, xhr and stls server. A twin is the same proxy on the front's port, named `<name>-sf`, with `neko-server-first: true`.
  - The twin comes right before its server in VPN, Cascade and Auto, and the Reality twin in Calls.
  - For demotions a twin's protocol is `<proto>-sf`: a demotion of the server the filter kills does not take its twin out of Cascade. The telemetry providers stay per plain protocol.
  - The sing-box, links and xray forms never get twins. With the settings unset the profile is byte for byte what it was.
- **The app** (client repo, `core/neko_server_first.go`, from 0.9.2-neko.5). At load time, after the gateway patch, the core removes the marker and sets `dialer-proxy: neko-server-first`: a local SOCKS5 helper on 127.0.0.1 and a port the system picks. The helper dials with mihomo's dialer (outside the tunnel, like every server connection), drops the banner line (at most 255 bytes, up to its line feed) and hands the connection over. It connects only to the servers the current profile marks, so another app on the phone cannot use it to leave the VPN.
- **Limits.**
  - Only NekoVPN on Android: there is no desktop NekoVPN build, and Hiddify, Karing and FlClash cannot wait for a server's line. They need a new address (plan G0).
  - hy2 (UDP) is not covered.
  - The filter may learn this too; then change the banner, the ports or the method (plan G3).
- **Rollout** (each step on the owner's word):
  1. Deploy the bot (`./scripts/deploy_to_entry.sh`).
  2. On entry, add `bind 127.0.0.1:12443 accept-proxy` to `frontend xray_in`, then `haproxy -c -f /etc/haproxy/haproxy.cfg` and `systemctl reload haproxy`.
  3. Run `sudo /opt/vpn-bot/scripts/install_server_first_front.sh`.
  4. Add `SF_REALITY_PORT=2443`, `SF_STLS_PORT=2444`, `NEKO_SF_MIN_VERSION=0.9.2-neko.5` to `.env`, the same three lines to entry's compose, then `docker compose up -d --no-deps vpn-bot`.
  5. Check from a Russian network: TLS through :2443 with SNI `www.bing.com` and through :2444 with `www.microsoft.com`, after reading the banner.
  6. Publish NekoVPN 0.9.2-neko.5 and run the notifier (§37).
- **Tests:** `tests/unit/test_server_first_front.py` (the config, the PROXY v2 header, the banner before any client data, the pipe and its half-close, a dead target, a full front, a reset), `tests/unit/test_install_server_first_front_sh.py` (the installer with fakes and tripwires, the unit), `tests/integration/test_server_first_profile.py` (who gets twins, their place in every group, demotions, telemetry, byte-identical when unset); the client's `core/neko_server_first_test.go`.
