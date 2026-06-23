---
name: vpn-ops
description: VPN infrastructure ops — Xray nodes, X-UI panel, traffic, client configs, and entry↔exit failover diagnostics
type: prompt
whenToUse: User asks about VPN nodes, server health, client configs, X-UI, traffic, broken keys, or anything that touches entry/exit ingress
---

# Infra topology

Two-node split:

| Role | IP | Where it runs | What lives there |
|---|---|---|---|
| **Exit** | <exit-host> | this host (where you, kimi, are running) | Xray-core inbound on :443, 3x-ui panel on :2026, vpn-bot container, traffic counters |
| **Entry** | <entry-host> | remote — reach via `ssh entry-node` | DNAT/SNAT, iptables routing, drops to Xray on the exit |

A user connects to **entry** :443 → entry rewrites the destination via `iptables -t nat` and forwards to **exit** :443 → Xray on exit terminates Reality, decrypts, egresses to the open internet. If keys are issued correctly but users can't connect, the breakage is almost always on entry's iptables, the entry SSH tunnel, or Xray on exit.

# Access creds (already on disk, NO secrets ever in chat)

You have two pre-installed SSH keys, owned by root, mode 600:

| Key | Purpose | Use it when |
|---|---|---|
| `/root/.ssh/entry_node_kimi` | passwordless SSH into <entry-host> | Investigating entry-side issues, restarting iptables, checking DNAT rules, viewing entry's journal |
| `/root/.ssh/github_kimi` | GitHub push/pull (deploy key with write access) | Pulling latest code, pushing a fix you wrote |

The user-friendly way to use them — `/root/.ssh/config` already pre-wires aliases:

```sh
ssh entry-node 'whoami'                   # uses entry_node_kimi automatically
ssh entry-node 'systemctl status xray'    # ...same
cd /opt/vpn-bot && git pull origin main   # read-only, https
cd /opt/vpn-bot && git push kimi-origin main   # uses github_kimi via SSH
```

Environment variables loaded into your process from `/root/.kimi-code/.env`:
`ENTRY_NODE_IP`, `ENTRY_NODE_SSH_HOST=entry-node`, `EXIT_NODE_IP`, `REPO_PATH=/opt/vpn-bot`, `REPO_REMOTE_SSH=kimi-origin`.

**Never `cat` private keys, `~/.gitconfig` credentials, `/opt/vpn-bot/.env`, or `/root/.kimi-code/credentials/`.** Use them transparently, don't print them.

# Standard diagnostics

When the user says "X не работает" / "ключи кривые" / "клиент не подключается", run this checklist before guessing:

**On exit (here, no SSH needed):**
```sh
docker compose -f /opt/vpn-bot/docker-compose.yml ps           # vpn-bot + 3x-ui both Up?
docker compose -f /opt/vpn-bot/docker-compose.yml logs vpn-bot --tail 30 | grep -iE 'error|warn'
ss -tlnp | grep -E ':(443|2026|8080)'                          # Xray + 3x-ui + bot dashboard
```

**On entry (one SSH hop):**
```sh
ssh entry-node 'iptables -t nat -L PREROUTING -n -v | head -20'   # DNAT rules present?
ssh entry-node 'ss -tn dst :443 dst <exit-host> | head -5'        # active tunnels to exit
ssh entry-node 'journalctl -u xray -n 30 --no-pager' 2>/dev/null   # if entry runs its own xray
ssh entry-node 'tail -20 /var/log/iptables.log' 2>/dev/null        # if iptables logging on
```

**Reality params sanity (broken keys):**
- Public key (`pbk`), short id (`sid`), and SNI (`sni`) must match between the issued VLESS link and the Xray config on exit. `/opt/vpn-bot/.env` on exit holds the canonical values: `REALITY_PUBLIC_KEY`, `SID_VALUE`, `SNI_VALUE`. The bot reads them from container env; if a VLESS link has `sid=01` while server uses `sid=037a08d118bfaafd`, the env-passthrough in `docker-compose.yml` is broken.

# X-UI

- HTTP API on this exit, port 2026 inside the network. Bot reads/writes via `bot/services/xui_service.py`. Credentials in `/opt/vpn-bot/.env` (`XUI_USERNAME`, `XUI_PASSWORD`).
- Web UI at `https://<exit-host>:2026/this_is_fine/`.
- Direct SQLite at `/var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db` — use only for read-only diagnostics, never write directly (bot's sync loop will overwrite).

# Traffic & quotas

- 3x-ui DB column `client_traffics(email, up, down, total)` is the source of truth for per-client consumption.
- Bot's quota check reads `up + down` and compares to `user.quota_gb * 1024**3`.
- Reset a single client's counter: x-ui UI → Inbound → Edit client → "Reset traffic". Don't poke the SQLite directly.

# When to ssh entry-node

Use it sparingly — only when a problem is provably on the entry side:
- Bot logs say "key issued OK" + x-ui shows the client active + user still can't connect → entry iptables / SNAT
- Sudden uniform connection loss across many users → entry firewall or BGP issue
- New client works for everyone except one platform → entry MSS clamping

Don't ssh entry-node just to "look around" — every hop is a chance to typo a destructive command.
