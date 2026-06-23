---
name: incident-response
description: Runbook for mass outages — prod down, en-masse client disconnects. Order of triage, who to notify, how to roll back.
type: prompt
whenToUse: User reports a mass outage (лежит, не работает у всех, массово отваливаются, прод down, outage, срочно) or anything implying an incident affecting many users at once
---

# First 60 seconds — confirm scope, don't act yet

Before touching anything, answer: **is this one user, some users, or everyone?**

```sh
# How many users are paid/demo and supposedly active right now?
docker compose -f /opt/vpn-bot/docker-compose.yml exec -T vpn-bot python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
for r in c.execute(\"SELECT status, COUNT(*) FROM users GROUP BY status\"): print(r)
"

# Is anyone connected to Xray right now?
ss -tn dst :443 | head -10
ss -tn '( sport = :443 )' | wc -l

# Health endpoint
curl -s http://127.0.0.1:8080/health | head -c 200
```

If only 1-2 users complain → it's a user-specific issue, use `vpn-ops` skill. If half+ complain → it's an incident.

# Triage order (do NOT skip steps)

```
1. Exit node containers up?         docker compose -f /opt/vpn-bot/docker-compose.yml ps
2. Xray inbound listening?           ss -tlnp | grep :443
3. Reality params unchanged?         docker exec 3x-ui cat /etc/x-ui/x-ui.json | python3 -m json.tool | grep -E 'publicKey|shortIds|serverName'
4. Entry node reachable?             ssh entry-node 'uptime'
5. Entry iptables DNAT present?      ssh entry-node 'iptables -t nat -S PREROUTING | grep 443'
6. Cert valid for the dashboard?     echo | openssl s_client -connect <dashboard-host>:9443 2>/dev/null | openssl x509 -noout -dates
7. DNS resolving the entry host?     dig +short <dashboard-host>
8. Disk full?                        df -h / /var/lib/docker
9. OOM killer fired?                 dmesg -T | grep -iE 'killed|oom' | tail -5
```

Stop at the first failed step — fix that, re-test, don't continue down the list with a known broken upstream.

# Communicate while you debug

If the incident lasts >5 minutes, broadcast to users. The dashboard has a Broadcast UI, but from CLI:

```sh
# Mint admin token
TOKEN=$(cd /opt/vpn-bot && python3 -c "
import os; from dotenv import load_dotenv; load_dotenv()
from bot.utils.admin_token import make_admin_token
print(make_admin_token(os.environ['BOT_TOKEN'], '1652899'))
")

# Preview first (confirm=false)
curl -s -X POST "http://127.0.0.1:8080/api/admin/broadcast?admin_token=$TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"text":"⚠️ Ведутся технические работы, восстановление в течение 15 минут.","audience":"active","confirm":false}'

# Send for real — ONLY after the user OKs the preview
curl -s -X POST "http://127.0.0.1:8080/api/admin/broadcast?admin_token=$TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"text":"...","audience":"active","confirm":true}'
```

**Never send a broadcast without the user's explicit "OK, отправляй".** ~80 paid users get a notification per send; a typo in there is a public mistake.

# Rollback paths

In order of preference (least → most destructive):

1. **Restart the bot container.** Fixes ~half of incidents that are stuck-state or memory-leak related.
   ```sh
   docker compose -f /opt/vpn-bot/docker-compose.yml restart vpn-bot
   ```

2. **Revert the last commit and redeploy.** If the incident started right after `docker compose up --build`:
   ```sh
   cd /opt/vpn-bot
   git log --oneline -5
   git revert <bad-sha> --no-edit
   git push kimi-origin main
   docker compose up -d --build vpn-bot
   ```

3. **Restore from yesterday's backup tarball.** Only after the user confirms:
   ```sh
   ls -lt /opt/backups/*.tar.gz | head -3
   # Show the user, get OK, then:
   # ...stop containers, swap volume contents, restart...
   ```

# Common-cause cheat sheet

| Symptom | Most likely cause | First check |
|---|---|---|
| All users disconnected, Xray container down | OOM, restart loop | `dmesg -T \| tail`, `docker compose logs vpn-bot --tail 50` |
| New keys "fail to connect", old ones work | `sid` / `pbk` env passthrough broken in docker-compose | `docker exec vpn-bot env \| grep -E 'SID_VALUE\|REALITY'` |
| Bot polls but doesn't respond | Telegram rate limit or BOT_TOKEN revoked | `docker compose logs vpn-bot \| grep -i '429\|401\|forbidden'` |
| Dashboard 502 | Caddy can't reach :8080 | `journalctl -u caddy -n 30`, `curl -s http://127.0.0.1:8080/health` |
| Mass disconnect every ~6 minutes | Entry iptables NAT timeout shorter than client keepalive | `ssh entry-node 'sysctl net.netfilter.nf_conntrack_*timeout*'` |
| Subscription panel HTTP 500 | Schema drift between `database.py` and prod DB | `docker compose logs vpn-bot \| grep 'no such column'` |

# Post-mortem (after the dust settles)

When the incident is closed:

1. Note the **trigger event** (what change/condition kicked it off).
2. Note the **detection delay** (incident start → first complaint).
3. Note the **mitigation** (what actually fixed it).
4. Add a line to AGENTS.md if there's a permanent lesson (new check, new monitoring, schema invariant).

Don't skip this — incidents repeat when the post-mortem skips.

# Do NOT during an incident

- Run `docker system prune` (wipes images mid-restart).
- `git push --force` (can break the deploy on entry if entry pulls too).
- Restart `kimi-bridge` (you'd lose your own conversation context).
- `systemctl restart docker` (kills both vpn-bot and 3x-ui at once, longer downtime).
- Push a "quick fix" without showing the diff to the user first.
