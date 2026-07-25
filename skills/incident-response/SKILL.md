---
name: incident-response
description: Runbook for mass outages — prod down, en-masse client disconnects. Order of triage, who to notify, how to roll back.
type: prompt
whenToUse: User reports a mass outage (лежит, не работает у всех, массово отваливаются, прод down, outage, срочно) or anything implying an incident affecting many users at once
---

You run on **entry**; the bot is local, xray + 3x-ui are on **exit** (`ssh exit-node`). Shorthand:
`DC="docker compose -f /opt/vpn-bot/docker-compose.yml -f /opt/vpn-bot/docker-compose.entry.yml"`.

# First 60 seconds — confirm scope, don't act yet

Is this **one user, some users, or everyone?**

```sh
# How many users are paid/demo right now?
$DC exec -T vpn-bot python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
for r in c.execute(\"SELECT status, COUNT(*) FROM users GROUP BY status\"): print(r)
"
# Bot health
curl -s http://127.0.0.1:8080/health | head -c 200
# Anyone connected to xray on exit right now?
ssh exit-node "ss -tn '( sport = :443 )' | wc -l"
```

If only 1-2 users complain → user-specific, use `vpn-ops`. If half+ complain → incident.

# Triage order (do NOT skip steps — stop at the first failure, fix, re-test)

```
1. Bot container up?          $DC ps
2. Entry ingress listening?   ss -tlnp | grep :443
3. Exit reachable?            ssh exit-node 'uptime'
4. xray inbound on exit up?   ssh exit-node 'ss -tlnp | grep :443'
5. 3x-ui panel on exit up?    ssh exit-node 'ss -tlnp | grep :2026; docker ps | grep 3x-ui'
6. Reality params unchanged?  ssh exit-node 'docker exec 3x-ui cat /etc/x-ui/x-ui.json' | python3 -m json.tool | grep -E 'publicKey|shortIds|serverName'
7. Dashboard cert valid?      echo | openssl s_client -connect <dashboard-host>:9443 2>/dev/null | openssl x509 -noout -dates
8. Disk full (either node)?   df -h / /var/lib/docker ; ssh exit-node 'df -h /'
9. OOM killer fired?          dmesg -T | grep -iE 'killed|oom' | tail -5 ; ssh exit-node 'dmesg -T | grep -iE oom | tail -5'
```

Exit is the tight box (929 MB) — step 9 there is a frequent culprit.

# Communicate while you debug

If the incident lasts >5 min, broadcast. From CLI (bot is local on entry):

```sh
TOKEN=$($DC exec -T vpn-bot python3 -c "
import os
from bot.utils.admin_token import make_admin_token
print(make_admin_token(os.environ['BOT_TOKEN'], '<super-admin-id>'))
")
# Preview first (confirm=false)
curl -s -X POST "http://127.0.0.1:8080/api/admin/broadcast?admin_token=$TOKEN" \
    -H 'Content-Type: application/json' \
    -d '{"text":"⚠️ Ведутся технические работы, восстановление в течение 15 минут.","audience":"active","confirm":false}'
# Send for real — ONLY after the user OKs the preview (confirm=true)
```

**Never broadcast without the user's explicit "OK, отправляй".** ~80 paid users get it per send.

# Rollback paths (least → most destructive)

1. **Restart the bot container** — fixes ~half of stuck-state / memory-leak incidents:
   `$DC restart vpn-bot`
2. **Restart xray / 3x-ui on exit** — if the break is exit-side:
   `ssh exit-node 'docker restart 3x-ui'` (confirm first).
3. **Revert a bad code deploy** — code is NOT git-managed on entry. If the incident started right after a
   redeploy, the fix is authored/reverted in the **dev repo** and re-rsynced by the human; you can't `git revert`
   here. As a stopgap you may hot-edit the offending file under `/opt/vpn-bot/bot/` + `$DC up -d --build vpn-bot`,
   then hand the change to the human. Confirm before rebuilding.
4. **Restore from backup** — only after the user confirms: `ls -lt /opt/backups/*.tar.gz | head -3`, show, get OK.

# Common-cause cheat sheet

| Symptom | Most likely cause | First check |
|---|---|---|
| All users down, xray container down on exit | OOM / restart loop on the 929 MB exit | `ssh exit-node 'dmesg -T \| tail; docker ps -a \| grep 3x-ui'` |
| New keys "fail to connect", old ones work | `sid`/`pbk` env passthrough broken | `$DC exec -T vpn-bot env \| grep -E 'SID_VALUE\|REALITY'` |
| Bot polls but doesn't respond | Telegram rate limit / BOT_TOKEN revoked, or Telegram egress proxy down | `$DC logs vpn-bot \| grep -i '429\|401\|forbidden'` |
| Dashboard 502 | reverse proxy can't reach `:8080` | `curl -s http://127.0.0.1:8080/health` |
| Mass disconnect every ~few min | entry NAT/conntrack timeout < client keepalive | `sysctl net.netfilter.nf_conntrack_*timeout*` |
| Panel/subscription HTTP 500 | schema drift between `database.py` and prod DB | `$DC logs vpn-bot \| grep 'no such column'` |

# After it's over

Note the **trigger**, the **detection delay**, and the **mitigation**. If there's a permanent lesson (new check,
schema invariant), tell the human to add it to the repo `AGENTS.md`. Incidents repeat when the post-mortem is skipped.

# Do NOT during an incident
- `docker system prune` (wipes images mid-restart).
- Restart `hermes-api` (kills your own run/context).
- `systemctl restart docker` on either node (takes everything down at once).
- Ship a "quick fix" without showing the diff to the user first.
