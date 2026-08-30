---
name: dpi-analysis
description: Detect and reason about DPI / censorship signals — short sessions, REALITY handshake failures, TCP RST spikes, per country/ASN. Where the metrics live and how to interpret them.
type: prompt
whenToUse: User asks about DPI, censorship, blocking, active probing, handshake fails, RST spikes, short/dropped sessions, or "нас режут / блокируют / зондируют"
---

You run on **entry** (RU-facing ingress). DPI signals are aggregated into the bot DB
(`/var/lib/vpn-bot/bot.db`, **local**) by the metrics collector; raw evidence is in the xray logs.

# Where the data is

- **`dpi_metrics`** table (bot DB) — 5-min snapshots, per `(country, asn)`:
  `country, asn, as_org, snapshot_at, conn_count, short_session_count, handshake_fail_count, rst_count`.
  Country `*GLOBAL*` is the host-wide roll-up (used for RST).
  - `asn` values carry the `AS` prefix (`'AS51088'`) — queries must include it to match.
  - `country`/`asn` can be NULL when geoip fails (localhost, private IPs). `country` may hold an inbound
    tag (e.g. `*TUNNEL*`) with `asn` NULL — that's per-inbound aggregation, `as_org` names the inbound.
  - Paths: in-container `/var/lib/vpn-bot/bot.db`, on host
    `/var/lib/docker/volumes/vpn-bot_vpn-bot-data/_data/bot.db`. No `sqlite3` binary in the container —
    use `python3 -c "import sqlite3; …"`.
- **`/api/admin/dpi_metrics`** — IP-level detail for a `(country, ASN)` (needs an admin token; see billing-ops for minting one).
- **xray logs** — `access.log` (per-connection) and `error.log` (handshake/REALITY failures). Routed to a shared
  volume so entry can read them; if a query needs raw lines, grep those for the offending `(country/ASN)` window.

# The three signals (same thresholds the auto-alerts use)

Shorthand: `DC="docker compose -f /opt/vpn-bot/docker-compose.yml -f /opt/vpn-bot/docker-compose.entry.yml"`.

Roll up the last hour by `(country, asn)` and compare to the 7-day baseline:
```sh
$DC exec -T vpn-bot python3 -c "
import sqlite3
from datetime import datetime, timedelta
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
cut = (datetime.utcnow()-timedelta(hours=1)).isoformat()
for r in c.execute('''
  SELECT country, asn, MAX(as_org),
         SUM(conn_count), SUM(short_session_count),
         SUM(handshake_fail_count), SUM(rst_count)
  FROM dpi_metrics WHERE snapshot_at >= ? AND country != '*GLOBAL*'
  GROUP BY country, asn HAVING SUM(conn_count) >= 50
  ORDER BY SUM(short_session_count)*1.0/SUM(conn_count) DESC'''), (cut,)):
    print(r)
"
```

| Signal | Metric | Warn / Critical | Reading |
|---|---|---|---|
| **Short sessions** | `short_session_count / conn_count` | ≥0.40 / >0.70 | DPI is **actively cutting** established connections for that cohort. Users get dropped mid-session. |
| **Handshake fails** | `handshake_fail_count / h` vs baseline | ≥5× / >20× baseline (floor 5/h) | **Active probing / scanner** hitting REALITY — SNI or the inbound is being fingerprinted. |
| **RST spike** | `*GLOBAL*` `rst_count` delta | +200% over baseline | Host-wide TCP aborts — broad interference or an upstream reset injector. |

Ignore buckets with `conn_count < 50` (noise). Small absolute counts aren't an incident.

# Interpreting + responding

1. **Confirm it's localized.** One `(country, ASN)` spiking while others are normal = targeted DPI, not an outage
   (if everything is down, use `incident-response`). Note the `as_org` — a single mobile carrier vs. nationwide.
2. **Corroborate with logs.** For a hot bucket, pull the matching xray `error.log` slice (REALITY handshake
   errors) and `access.log` (session durations) for that window to see whether it's probing vs. cutting.
3. **Remediation levers** (propose, don't auto-apply — confirm with the admin):
   - Short sessions on a cohort → move that cohort to the **reserve inbound** (different SNI/params) or **rotate
     the entry IP**.
   - Handshake-fail probing → check the REALITY `sni`/`shortIds`; consider a fresh SNI/short-id set on exit's xray.
   - RST spike host-wide → likely nothing to "fix" locally; document and watch; consider a fronted/obfuscated inbound.
4. **Escalate** anything critical (>0.70 short ratio or >20× hsfail) to the admin with the `(country, ASN, as_org)`
   and the `/api/admin/dpi_metrics` IP breakdown.

# Do NOT
- Don't rotate entry IP or swap SNI/inbound params without the admin's OK — it disrupts every connected user.
- Don't treat a single 5-min snapshot as a trend — always roll up ≥1h and compare to baseline.
- Don't print raw client IPs into the chat beyond what's needed; summarize by `(country, ASN)`.
