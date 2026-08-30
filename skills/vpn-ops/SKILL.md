---
name: vpn-ops
description: VPN infrastructure ops — Xray nodes, X-UI panel, traffic, client configs, and entry↔exit ingress diagnostics
type: prompt
whenToUse: User asks about VPN nodes, server health, client configs, X-UI, traffic, broken keys, or anything that touches entry/exit ingress
---

# Topology (see AGENTS.md for the full table)

You run on **entry** (`<entry-host>`), as root. The **bot** is local here; the **x-ui panel + xray-core**
live on **exit** (`ssh exit-node`, host `<exit-hostname>`, panel `:2026`, xray `:443`). A user hits entry `:443`
→ forwarded to exit `:443` → xray egresses. Keys issued OK but user can't connect ⇒ suspect entry
ingress/routing or xray on exit.

Never `cat` `/opt/vpn-bot/.env` or any SSH key — use them, don't print them.

# Standard diagnostics

When the user says "X не работает" / "ключи кривые" / "клиент не подключается", run this before guessing.

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
```

**Reality params sanity (broken keys):**
- Public key (`pbk`), short id (`sid`), and SNI (`sni`) must match between the issued VLESS link and the xray
  config on **exit**. The canonical values live in `/opt/vpn-bot/.env` on **entry** (the bot passes them to
  clients): `REALITY_PUBLIC_KEY`, `SID_VALUE`, `SNI_VALUE`. If an issued link has `sid=01` while the server
  uses the full sid, the env-passthrough in `docker-compose.entry.yml` is broken. Compare, don't print.

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

# Traffic & quotas

- On exit, the x-ui `client_traffics(email, up, down, total)` table is the source of truth for per-client usage.
- The bot's quota check reads `up + down` and compares to `user.quota_gb * 1024**3`.
- Reset one client's counter via the panel (Inbound → Edit client → "Reset traffic"), not by poking SQLite.

# When to ssh exit-node

Only when a problem is provably on the exit side:
- Bot logs say "key issued OK" + x-ui shows the client active + user still can't connect → xray on exit / entry routing.
- Sudden uniform loss across many users → entry ingress or exit xray down.
- A new client works everywhere except one platform → entry MSS clamping.

Don't `ssh exit-node` just to look around — every hop is a chance to typo a destructive command.
