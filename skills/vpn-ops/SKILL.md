---
name: vpn-ops
description: VPN infrastructure ops — Xray nodes, X-UI panel, traffic, client configs, per-protocol liveness, and entry↔exit ingress diagnostics
type: prompt
whenToUse: User asks about VPN nodes, server health, client configs, X-UI, traffic, broken keys, which protocol is down (какой протокол не работает / что с reality|hy2|ws|stls), or anything that touches entry/exit ingress, or why the cascade / protocol order changed (каскад, порядок протоколов, почему X в конце)
---

# Topology (see AGENTS.md for the full table)

You run on **entry** (`<entry-host>`), as root. The **bot** is local here; the **x-ui panel + xray-core**
live on **exit** (`ssh exit-node`, host `<exit-hostname>`, panel `:2026`, xray `:443`). A user hits entry `:443`
→ forwarded to exit `:443` → xray egresses. Keys issued OK but user can't connect ⇒ suspect entry
ingress/routing or xray on exit.

Never `cat` `/opt/vpn-bot/.env` or any SSH key — use them, don't print them.

# Standard diagnostics

When the user says "X не работает" / "ключи кривые" / "клиент не подключается" / "какой протокол лежит", run
this before guessing — and run it in THIS order.

## STEP 0 — ALWAYS FIRST (before any ss / iptables / docker ps)

```sh
python3 /opt/vpn-bot/scripts/protocol_healthcheck.py          # add --json if you need to parse it
```

Read its **ИТОГ** block. It prints one verdict per protocol with **ranked suspects and the exact next command for
each** — those suspects ARE your hypotheses. Verify them in the order printed; do not invent a parallel plan.
Exit code: `0` = every protocol OK (reality/hy2/ws/stls by probes, hy2t by exit state), `1` = at least one
protocol DOWN or DEGRADED, `2` = could not assess — a layer FAILED (bot container / bot.db / exit ssh) or the
probe pipeline is stale, and nothing else proved a failure. A 2 is never a pass: read its `Слои:` line to see
WHICH layer failed, then use the fallback below. A BROKEN panel audit on a probed inbound is exit 1 on its own
(that protocol is marked DEGRADED and the flow/password-wipe suspect is printed) — the audit is an EARLIER
signal than the probes, which run from a single client out of ~80.

Why this comes before the port/process checks: during the **4-day Reality outage of 2026-09-01..04** `ss`,
`iptables`, `docker ps`, `systemctl` and the xray logs were green the entire time. The failure was a per-client
field in the panel (`flow` wiped on 80/81 Reality clients), and the ONLY two signals that saw it were the probe
table (`outbound_health`) and the panel audit. Starting from ports and containers yields "everything is alive"
after 100 s of work and misses the outage completely. The healthcheck already combines those two signals.

**If the healthcheck cannot assess** (exit 2, traceback, file missing) — collect its two inputs by hand,
then continue to the entry/exit commands below:

```sh
# 1) panel audit: is every client record usable for its inbound's protocol?  0 clean / 1 BROKEN / 2 cannot check
docker exec -e PYTHONPATH=/app vpn-bot python3 /app/scripts/verify_panel_client_fields.py
# 2) probe table: per protocol — rows in the last 3 h, how many showed life, newest row
docker exec vpn-bot python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
q = \"SELECT outbound_tag, COUNT(*), SUM(latency_ms IS NOT NULL OR status='ok'), MAX(ts) FROM outbound_health WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S','now','-3 hours') GROUP BY outbound_tag\"
for r in c.execute(q): print(r)
"
```

> **What the probe table means.** `outbound_health` in bot.db gets **10 rows per protocol every 15 min**
> (`:01/:16/:31/:46`), one per target domain, sent through the real tunnel (probe-proxy sidecar, tags
> `reality` / `hy2` / `ws` / `stls`). **7/10 `ok` is normal** — vk / yandex / sberbank legitimately fail through a
> foreign exit. **Liveness = a latency came back**: a row proves the tunnel works if
> `latency_ms IS NOT NULL OR status='ok'` (an HTTP 418 that arrived through the tunnel still has a latency; the
> 2026-09-01 rows had `latency_ms NULL` — nothing ever connected). A protocol with zero such rows across 3
> consecutive runs is dead — exactly what the `protocol_down:<tag>` alert fires on. `status` alone is a trap:
> counting errors makes a healthy protocol look sick and a dead one look "only a bit worse".

**On entry (local — the bot):**
```sh
DC="docker compose -f /opt/vpn-bot/docker-compose.yml -f /opt/vpn-bot/docker-compose.entry.yml"
$DC ps                                              # vpn-bot Up + healthy?
$DC logs vpn-bot --tail 30 | grep -iE 'error|warn'
ss -tlnp | grep -E ':(443|8080)'                    # entry ingress + bot dashboard
curl -s http://127.0.0.1:8080/health | head -c 200
```

**On exit (one SSH hop — xray + x-ui):**
```sh
ssh exit-node 'ss -tlnp | grep -E ":(443|2026)"'                 # xray inbound + panel listening?
ssh exit-node 'docker ps --format "{{.Names}}: {{.Status}}" | grep -i 3x-ui'
ssh exit-node 'journalctl -u xray -n 30 --no-pager' 2>/dev/null   # if xray runs under systemd there
# per-inbound "accepted" in the last 5000 access-log lines — a listening inbound with ZERO accepted is dead
ssh exit-node 'docker exec 3x-ui sh -c "grep -h accepted /etc/x-ui/access.log | tail -5000 | grep -oE \"\[inbound-[0-9]+\" | sort | uniq -c"'
ssh exit-node 'systemctl is-active hysteria hysteria-turbo hy2-traffic-collector hy2-traffic-collector-turbo'
```

# Reality (VLESS + XTLS-Reality, exit `:443`, entry haproxy `:8443` in front)

**Params sanity (broken keys):**
- Public key (`pbk`), short id (`sid`), and SNI (`sni`) must match between the issued VLESS link and the xray
  config on **exit**. The canonical values live in `/opt/vpn-bot/.env` on **entry** (the bot passes them to
  clients): `REALITY_PUBLIC_KEY`, `SID_VALUE`, `SNI_VALUE`. If an issued link has `sid=01` while the server
  uses the full sid, the env-passthrough in `docker-compose.entry.yml` is broken. Compare, don't print.

**Dest certificate size (Reality dies for EVERYONE if it grows):** xtls/reality hardcodes an 8192-byte buffer for
the dest's TLS `Certificate` message. On 2026-07-20 Microsoft's chain grew to 8273 B and Reality was down for all
users until the dest moved to `www.bing.com` (AGENTS.md §23). Check from **exit** (that is who dials the dest):

```sh
ssh exit-node 'timeout 20 openssl s_client -connect www.bing.com:443 -servername www.bing.com -tls1_3 -msg </dev/null 2>/dev/null | grep -A1 "Certificate$"'
```
The `[length XXXX]` hex on the `Certificate` line must stay **≤ ~0x1F40 (8000 B)**. Bigger ⇒ that is your Reality
outage; the fix is a new dest/SNI, changed in **three places at once** (panel inbound on exit, HAProxy ACL on
entry, `SNI_VALUE` in the bot `.env`) — never just one.

**Per-client `flow` (the 2026-09-01..04 outage):** every client on the Reality inbound MUST carry
`flow=xtls-rprx-vision`. With an empty flow the server refuses every Vision handshake, inbound-443 serves zero
connections — and every port/process check stays green. On 2026-09-01 00:00 UTC the monthly quota job wrote a
flow-less body to 80/81 clients (it copied the record from the SS inbound, which has no `flow` key); probes logged
0/960 and nobody noticed for four days. The bot now restores the default flow when it updates a Reality client
(`xui_service.PER_INBOUND_FIELDS` merge) and `deploy_to_entry.sh` runs the panel audit as a post-deploy smoke,
but the panel can still be damaged by hand, by a fork upgrade or by a new caller. When the healthcheck / audit
prints `BROKEN … has empty 'flow'`:

```sh
# on exit — back up the panel DB first
ssh exit-node 'docker cp 3x-ui:/etc/x-ui/x-ui.db /opt/backups/x-ui.db.$(date +%F-%H%M%S)'
# on entry — dry run (reports only), then apply: restores flow via the panel API, then a PANEL-side xray restart
docker exec -e PYTHONPATH=/app vpn-bot python3 /app/scripts/restore_reality_flow.py
docker exec -e PYTHONPATH=/app vpn-bot python3 /app/scripts/restore_reality_flow.py --apply
# prove it reached the RUNNING core, not just the DB: the two counts must be equal (N users, N with flow)
ssh exit-node 'docker exec 3x-ui sh -c "/app/bin/xray-linux-amd64 api inbounduser -s 127.0.0.1:62789 -tag inbound-443"' \
  | python3 -c "import json,sys; u=json.load(sys.stdin).get('users') or []; print(len(u), 'users,', sum(1 for x in u if (x.get('account') or {}).get('flow')), 'with flow')"
```

Rules that make or break this repair:
- The **panel-side restart is mandatory** (`xui.api.restart_xray()`; the script does it unless `--no-restart`).
  `reload_xray()` in `xui_reload.py` is NOT it — that hits ENTRY's sidecar. The fork hot-applies a client edit as
  RemoveUser+AddUser per inbound, and the AddUser always fails on the shadowsocks-2022 inbound, so every edited
  client silently drops out of the RUNNING SS inbound until config.json is regenerated from the DB by a restart.
- **Never repair with `add_client`** — delete+re-add wipes the client's accumulated traffic. Update, don't re-add.
- Confirm with `python3 /opt/vpn-bot/scripts/protocol_healthcheck.py` (audit clean) and, one probe run later
  (≤15 min), fresh `reality` rows **with a latency** in `outbound_health`. "Panel says OK" alone is not a confirm.

# X-UI (on exit)

- HTTP API on exit, panel `:2026`. The bot reads/writes it via `bot/services/xui_service.py` over the private
  link; credentials `XUI_USERNAME` / `XUI_PASSWORD` are in `/opt/vpn-bot/.env` on entry.
- x-ui SQLite lives on exit; touch it **read-only** for diagnostics only (the bot's sync loop is the writer):
  `ssh exit-node 'sqlite3 -readonly <x-ui.db path> "SELECT ..."'`. Never write it directly.
- 3x-ui v3.4.0 keys clients **globally by email** — a client add that fails with "already in use" means the
  email exists; the bot handles this by delete-by-email + re-add (preserves the UUID). See `xui_service.py`.

# Hysteria2 — two instances on exit

- **Plain hy2** = freemium tier (every user incl. demo). **hy2t "Turbo"** = second hysteria2 instance on exit
  `:8402` with Brutal congestion control — the **paid-default** protocol.
- Auth is split: bot serves `/api/hy2/auth` (demo+paid) vs `/api/hy2t/auth` (paid only). A demo user "hy2
  works but Turbo denies" is the tier gate working, not a bug. Env knobs: `HY2T_*`.
- Port-hopping format gotcha (broke Hy2 on 2026-07-26): sing-box `server_ports` wants an **array of "X:Y"**
  ranges; the hysteria2 URI `mport` wants a **comma-separated string**. Never copy one format into the other.
- UDP-native traffic (Telegram calls) routes via the `calls` selector (Reality/Hy2/Hy2t) — RU-direct TCP with
  a QUIC:443 carve-out is intentional (VK banner), don't "fix" it.

# Cascade is self-tuning (DPIMonitor, since 2026-09-06)

The protocol order users get from `/sub`, the key card and `?format=links` is **no longer only the operator's
setting**. `bot/services/dpi_monitor.py` runs inside the bot every 10 min and moves a protocol to the END of the
order (it never removes one) when the data says it is failing: probes DARK/DEGRADED → globally; a Reality
handshake-fail storm in `dpi_metrics` for one ASN, a hy2-auth reconnect storm from a user of that ASN, or ≥2
"не работает" reports from one ASN → for that ASN only. It restores the protocol by itself after ~1 h of clean
signals. So, BEFORE you "fix" an order that looks wrong:

1. **A protocol at the end, or an ASN with its own order, is a finding with a stored reason — read it first.** The
   admin runs `/cascade` (or `/cascade AS31133`) in Telegram; you read the same thing from bot.db, read-only:
   ```sh
   docker exec vpn-bot python3 -c "
   import sqlite3
   c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
   for k in ('cascade_protocol_order','cascade_by_asn','cascade_by_country','cascade_auto','dpi_monitor_state','dpi_monitor_enabled'):
       r = c.execute('SELECT value FROM app_settings WHERE key = ?', (k,)).fetchone(); print(k, '=', r[0] if r else None)
   "
   ```
   `cascade_auto` holds every ACTIVE auto-demotion (`global` + per-`asn`) with `since`, `reason` (the rule id:
   `probe_dark` / `probe_degraded` / `reality_asn` / `udp_storm_asn` / `user_reports_asn`) and `evidence` (the
   human line with the numbers);
   `admin_actions` rows by `dpi_monitor` (`cascade_auto_demote` / `cascade_auto_restore`) are the history; the AI
   topic got one message per run that changed something.
2. A demotion with a reason means: go check THAT protocol (STEP 0 healthcheck, Reality section above) — the monitor
   found the problem before the user did. Don't reorder around it.
3. Wrong demotion (the signal was noise, the protocol is fine)? The one-command undo is **`/cascade reset`** (admin,
   in Telegram) — it clears `cascade_auto` + `dpi_monitor_state` and logs the action; `/cascade off` pauses the
   monitor without clearing. Propose those to the admin. **Never `UPDATE`/`DELETE` `cascade_auto`,
   `dpi_monitor_state` or any `cascade_*` key by hand** — a hand-edited JSON is exactly what the monitor replaced,
   and the next run will not know what you meant (it may restore, re-demote, or count your edit as its own).
4. The operator's `cascade_protocol_order` / `cascade_by_asn` always win on the BASE order; the monitor only
   reorders inside it. "Pin protocol X first regardless of signals" is `/cascade off` **then** `/cascade reset`
   (off only stops NEW changes — demotions already in effect stay until reset), not an edit.

# Traffic & quotas

- On exit, the x-ui `client_traffics(email, up, down, total)` table is the source of truth for per-client usage.
- The bot's quota check reads `up + down` and compares to `user.quota_gb * 1024**3`.
- Reset one client's counter via the panel (Inbound → Edit client → "Reset traffic"), not by poking SQLite.

# When to ssh exit-node

Only when a problem is provably on the exit side:
- The healthcheck says ONE protocol is dark while the others are alive → inbound / per-client field on exit
  (see the Reality section) — this is the one case where the exit hop is justified immediately.
- Bot logs say "key issued OK" + x-ui shows the client active + user still can't connect → xray on exit / entry routing.
- Sudden uniform loss across many users → entry ingress or exit xray down.
- A new client works everywhere except one platform → entry MSS clamping.

Don't `ssh exit-node` just to look around — every hop is a chance to typo a destructive command.
