# §52. What a tunnel user can reach on exit itself: the panel's xray API, hysteria, Caddy's admin (2026-11-11)

Checked because the panel xray's API (3x-ui's `api` inbound, `127.0.0.1:62789` in the 3x-ui container; HandlerService, LoggerService, StatsService, plus RoutingService, which the panel adds to the running config) would let a user list every user's uuid (`xray api lsi`), add or remove users, rewrite the routing, or reset the traffic counters, which means unmetered traffic. The check read exit's config with the owner's OK and changed nothing there. A stand reproduced that config with the same versions; its scripts are not in the repo.

- **The xray API is not reachable through a tunnel today.**
  - Exit runs xray 26.6.22 (the 3x-ui image its compose pins by digest, §23) with 3x-ui's default template: routing `AsIs`; rules `api` → api, `geoip:private` → blocked, bittorrent → blocked; a `direct` freedom without `finalRules`. Every user inbound is VLESS, VMess or Shadowsocks.
  - A literal 127.0.0.1 or 0.0.0.0 is caught by `geoip:private`. A NAME skips the IP rules under `AsIs` and goes to `direct`: localhost, a public name pointing at 127.0.0.1, or a name that rebinds to it. Freedom stops it there: it checks the address it actually dials against a built-in default rule (Xray-core PR #5947, merged 2026-04-15; `proxy/freedom: blocked target: tcp:127.0.0.1:62789` in error.log, info level). Rebinding gets nothing, because the check is on the dialed address.
  - The 3x-ui container sits on the bridge network `vpn-bot_vpn-net` (172.20.0.2). The API is on the container's own loopback, out of reach of host processes such as hysteria.
- **What that default rule depends on** (`proxy/freedom/freedom.go` at v26.6.22, each case proved on the stand):
  - **The inbound type.** Only vless, vmess, trojan, hysteria, wireguard and shadowsocks* get the default rule. socks, http, mixed and dokodemo/tunnel get none: a socks inbound reached the API on the stand with the default template.
  - **No `finalRules` entry that matches first.** Configured rules are checked before the default. Any allow rule that matches 127.0.0.1 switches the protection off: with `[{"action": "allow"}]`, localhost, a loopback name and a rebinding name all reached the API, and `IPIfNonMatch` routing stopped the first two but not rebinding. `finalRules: []` keeps the default.
  - **The xray version.** A build without PR #5947 has no default rule. Since PR #6058 (2026-09-08) freedom no longer dials the address it checked: the dial looks the name up again, and that address is checked too. xray 26.11.9 on the stand still blocked localhost, a loopback name, rebinding and a failed first lookup. PR #6742 skips the rule when the outbound uses `sockopt.dialerProxy`.
- **Hardening, applied on exit 2026-11-13** (checked 2026-11-15: the second rule, right after `api`). The rule: `{"type": "field", "port": "62789,11111", "outboundTag": "blocked"}`. 11111 is the template's metrics listener (pprof and expvar), the same class: with the default rule switched off it answered `/debug/vars` through a tunnel on the stand. The rule needs no name, version or inbound type to hold; on the stand it blocked both ports in every case above. It is the pattern xhr-exit uses for its port 10085 (§51).
  - **Placed BEFORE the `api` rule it blackholes the panel's own polling** (`[api -> blocked]` in access.log): traffic accounting stops without an error (§26).
  - Apply: panel → Xray Configs → the template JSON (Advanced), insert it as the second rule, save, restart xray (a second of reconnects). Check that `docker exec 3x-ui cat /app/bin/config.json` lists it second and that `[api -> api]` lines keep coming in `/etc/x-ui/access.log`.
- **Hysteria did let users reach exit's loopback.** Closed on 2026-11-11 with both fixes below: the ACL in both hysteria configs and Caddy's admin on a unix socket (checked 2026-11-15).
  - Hysteria 2 has no default block for private addresses (its docs, and v2.9.2 on the stand). Exit's `/etc/hysteria/config.yaml` and `config-turbo.yaml` had no `acl:`. Both instances run on the host as root, with no systemd IP restriction and no OUTPUT firewall rule.
  - On exit's loopback, that exposed Caddy's admin API on `127.0.0.1:2019`. It has no auth, and its Host check only refuses a foreign Host header, which the client sets anyway. Also reachable: hysteria's traffic APIs on 9977 / 9978 (they need their secret), containerd, and the docker bridge (172.20.0.x: the panel's :2026, its subscription server :2096).
  - Stand (hysteria 2.9.2, Caddy 2.11.3 in the server's network namespace): `GET /config/` answered 200 through a hy2 tunnel via 127.0.0.1, localhost and a public name pointing at 127.0.0.1. Through that API a hy2 user (demo included) can replace Caddy's whole config. That covers the dashboard's TLS front (:9443 → entry :8080, where admin tokens pass) and the backup subscription domain (:2096, where the NekoVPN app falls back for profiles).
  - **Fix 1, the hysteria ACL.** Back up both configs, append this to each, then `systemctl restart hysteria hysteria-turbo` (hy2 users reconnect within seconds):
    ```yaml
    acl:
      inline:
        - reject(0.0.0.0/8)
        - reject(10.0.0.0/8)
        - reject(100.64.0.0/10)
        - reject(127.0.0.0/8)
        - reject(169.254.0.0/16)
        - reject(172.16.0.0/12)
        - reject(192.168.0.0/16)
        - reject(224.0.0.0/4)
        - reject(240.0.0.0/4)
        - reject(::1/128)
        - reject(fc00::/7)
        - reject(fe80::/10)
    ```
    On the stand every loopback and private target was refused (`"error": "rejected"` in hysteria's log): names and IPv4-mapped forms such as `[::ffff:127.0.0.1]` too. Hysteria resolves a name once and dials the address it checked, so rebinding does not pass. Check: both units active, and the next probe run still has live hy2 rows (§28).
  - **Fix 2, Caddy's admin on a unix socket.** Put a global block at the very top of `/etc/caddy/Caddyfile` (a Caddyfile block needs its braces on lines of their own; the socket goes in the caddy user's home):
    ```
    {
    	admin unix//var/lib/caddy/admin.sock
    }
    ```
    The FIRST apply must go to the old address: `caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --force --address localhost:2019`. A plain `systemctl reload caddy` right after the edit asks the new socket, which does not exist yet, so it fails and the old config stays. After that, `systemctl reload caddy` goes through the socket, and so does acme.sh's renewal hook (the backup domain's cert). `admin off` would break those reloads. Check: `ss -ltn 'sport = :2019'` prints nothing.
- **Guarded by the healthcheck** (layer C, the same 15-minute timer, §50). `GUARD_HELPERS` in `scripts/protocol_healthcheck.py` run on exit ahead of the layer-C script and send back only what the verdict needs: each hysteria unit's `acl` block (or its rules file), never the rest of its config; the listeners on :2019; the xray version; a view of config.json without clients, keys or passwords. The verdicts are pure functions (`exit_guard`).
  - **`hy2_acl:<unit>`**, for each hysteria unit that is ACTIVE, whatever the cascade says: hy2t's auth checks only the paid status, so a running turbo instance still lets paid users in. Critical when the ACL leaves 127.0.0.1 or ::1 open to TCP; warn when only private networks stay open (10/8, 100.64/10, 169.254.169.254, 172.16/12 including the docker bridge, 192.168/16, fc00::/7, fe80::/10). The first rule covering all of TCP to an address decides. A reject for some ports only does not count; an earlier non-reject for some ports leaves them open. `geoip:` entries are not credited: use CIDRs as above. The title names Caddy's admin when :2019 listens. Exit as read on 2026-11-11 raised it critical for both units until Fix 1 went in that evening.
  - **`xray_api_exposed`** (critical): some user inbound can dial the API port or the metrics listener through a freedom that does not refuse loopback. The check walks the routing as a user would: a rule with conditions beyond inbound, port and network may or may not match, and the user picks. Freedom refuses loopback when a `finalRules` block matches first, or else when the version is ≥ 26.4.15 and the inbound is a covered kind. dokodemo/tunnel inbounds are skipped: their client cannot pick the destination. The next command is the port rule with the config's own ports and blackhole tag.
  - **`xray_api_polling`** (critical): the panel's own request (the api inbound to 127.0.0.1:<port>) does not reach the api outbound first. This happens with a port rule or a `geoip:private` rule placed before the api rule, or with no api rule at all. Traffic accounting has stopped.
  - **`exit_guard_unreadable`** (warn): one of these reads failed (a unit's config or rules file, the xray version or config). Its keys go into the report's `unjudged` list, and `check_healthcheck` keeps their alerts as they were: "could not look" is never "fixed", here per key and not only per layer.
  - After deploying, re-run `sudo /opt/vpn-bot/scripts/install_protocol_healthcheck.sh` on entry. The timer runs a root-owned copy of the script.
  - A unit counts as stopped only when systemd says `inactive` or `failed`; any other answer (a bus error) leaves its key unjudged.
  - Tests: `tests/unit/test_healthcheck_exit_guard.py` (the helpers on sample text, both verdicts, the findings, the unjudged keys) and `TestHealthcheckAlerts::test_a_key_the_run_could_not_look_at_keeps_its_alert`. 46 mutants killed, run on a copy with a positional restore and a hash check.
- **Logs** (read-only, 2026-11-11): today's access.log has no user request to `:62789` or `localhost`. error.log since 2026-08-19 has 125 freedom blocks, all to 10.58.x.x:443 (sites whose names resolve to private addresses), none to loopback. error.log is 2.4 GB and is never rotated.
- **The DE reserve** (x-ui 2.8.11, host install, its xray on the host network): fixed 2026-11-13. It ran xray 26.2.6, older than PR #5947, so a DE user reached `localhost:11111` (HTTP 200), the musicbot's Bot API on `127.0.0.1:8081` and the xray API.
  - Now xray 26.6.22, the exit's version: the official release zip, its sha256 checked against the digest GitHub publishes, `xray run -test` on the live config first.
  - The port rule `62789,11111,8081` sits second in the template. A name that points at any other loopback or private port is stopped by freedom's default rule (checked: `localhost:2026` hangs, the panel's public address answers).
  - The panel's API there, from vpn-bot with `FallbackNodeService`'s session: POST `/sub/panel/xray/` reads the template, POST `/sub/panel/xray/update` (form `xraySetting` + `outboundTestUrl`) writes, POST `/sub/panel/api/server/restartXrayService` restarts xray.
  - Backups on DE: `/root/nekovpn-backups/*-20261113-132354`, including the old binary `xray-linux-amd64-26.2.6`.
  - When x-ui on DE swaps xray, check that the rule is still second and the version is between 26.4.15 (PR #5947) and 26.7.10: mihomo, the NekoVPN app's core, keeps no Reality compatibility with xray 26.7.11 and later.
