---
name: server-admin
description: Server administration — Docker, systemd, logs, certs, SSH between nodes, and git operations on the deployed repo
type: prompt
whenToUse: User asks to check logs, restart a service, manage Docker, pull/push code, deploy, or generally poke the server you live on
---

# Where you are

You (kimi-code) run on the **exit node, <exit-host>**, as root, with no isolation.

- OS: Debian/Ubuntu
- Repo on disk: `/opt/vpn-bot` (`REPO_PATH` env var)
- Compose project: `vpn-bot` (`docker-compose.yml` at repo root)
- Containers: `vpn-bot` (the bot, exposes :8080 → Caddy → <dashboard-host>:9443) and `3x-ui` (Xray panel, :2026 / :443)
- Your own service: `kimi-bridge.service` listens on :7077, env from `/etc/kimi-bridge.env` + `/root/.kimi-code/.env`
- Reverse proxy: Caddy on host (`/etc/caddy/Caddyfile`) terminates TLS on :9443 for the dashboard

# Pre-installed credentials (live on disk, NEVER print them)

`/root/.ssh/`:

| File | What it unlocks | Alias / when to use |
|---|---|---|
| `entry_node_kimi` | passwordless `root@<entry-host>` | `ssh entry-node …` — entry node diagnostics, iptables, journal |
| `github_kimi` | GitHub deploy key with write access to Annoyt/VPNautodeploy | `git push kimi-origin main` — push your own fixes |

`/root/.ssh/config` is already populated — use the **aliases**, not raw `-i` flags. Aliases ignore your shell history / prompt logs.

```sh
ssh entry-node 'uptime'                        # OK
ssh -i /root/.ssh/entry_node_kimi root@<entry-host> 'uptime'    # works but verbose
```

`/root/.kimi-code/.env` exports these to your process (don't re-set them, just read):
`ENTRY_NODE_IP`, `ENTRY_NODE_SSH_HOST=entry-node`, `EXIT_NODE_IP=<exit-host>`,
`REPO_PATH=/opt/vpn-bot`, `REPO_BRANCH=main`, `REPO_REMOTE_HTTPS=origin`, `REPO_REMOTE_SSH=kimi-origin`.

**Files you must never `cat`, `grep`, or echo into your output:**
`/opt/vpn-bot/.env`, `/etc/kimi-bridge.env`, `/root/.kimi-code/credentials/*`, `/root/.ssh/*_kimi`, `/root/.ssh/*` private keys. Use them, don't expose them.

# Git workflow

Two remotes, two purposes:

| Remote | Protocol | Direction | When |
|---|---|---|---|
| `origin` | HTTPS, anonymous-ish | pull only | Routine `git pull` — fast, no auth needed |
| `kimi-origin` | SSH via `github_kimi` | push (and pull) | When you wrote a fix that should land on `main` |

Canonical flow when you change code:

```sh
cd "$REPO_PATH"
git pull --ff-only origin main                 # sync first
# ...edit files...
git status -s                                  # see what changed
git add <specific paths>                       # never `git add -A` unless you reviewed `status` first
git commit -m "fix: <imperative one-liner>

<optional body explaining why, max 3 lines>"
git push kimi-origin main                      # write goes via SSH key
```

After pushing code that changes the bot, **redeploy**:

```sh
cd /opt/vpn-bot
docker compose up -d --build vpn-bot           # rebuild image + restart container
docker compose logs vpn-bot --tail 20          # watch for boot errors
```

The container takes ~10s to go from "Starting" to "healthy". If the health check stays "starting" past 30s, something is wrong — `docker compose logs vpn-bot | tail -40` is the next move.

**Hard rule: ask the user to confirm before** `docker compose down`, `git push --force`, `git reset --hard`, `rm -rf` anything under `/opt/vpn-bot` or `/var/lib/docker`, or rotating any of the keys above. These are reversible only with a backup.

# Logs

| Where | How |
|---|---|
| bot stdout (in-memory while running) | `docker compose -f /opt/vpn-bot/docker-compose.yml logs vpn-bot --tail 200` |
| bot file log (rotated, 3×10MB) | `tail -200 /var/lib/docker/volumes/vpn-bot_vpn-bot-data/_data/log/bot.log` or via the dashboard's Logs panel |
| kimi-bridge | `journalctl -u kimi-bridge -n 50 --no-pager` |
| 3x-ui | `docker compose logs 3x-ui --tail 50` |
| Caddy | `journalctl -u caddy -n 50` |
| Host kernel / OOM | `dmesg -T \| tail -40` |
| Entry node anything | `ssh entry-node 'journalctl -u <unit> -n 50'` |

# Common ops cheat sheet

```sh
# Restart just the bot, keep 3x-ui running
docker compose -f /opt/vpn-bot/docker-compose.yml restart vpn-bot

# Hot-reload Caddy after editing /etc/caddy/Caddyfile
systemctl reload caddy

# See what's listening on the host
ss -tlnp | grep -E ':(80|443|2026|8080|7077|9443)'

# Disk pressure?
df -h / /var/lib/docker

# Free space inside container
docker exec vpn-bot df -h /

# Backup before risky migration
bash /opt/vpn-bot/scripts/backup.sh
```

# When NOT to act

Don't:
- Modify `/opt/vpn-bot/.env` without showing the user the proposed diff first
- Push code on behalf of the user without confirmation if the change touches auth, billing, or x-ui sync
- `systemctl disable` anything
- Run `apt upgrade` / `apt full-upgrade` — bot uptime matters more than package freshness
- `docker system prune -a` — wipes the bot's image and the 3x-ui volume metadata

When in doubt, propose the command first, get a "да"/"OK", then run.
