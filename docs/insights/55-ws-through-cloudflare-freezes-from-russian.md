# §55. ws through Cloudflare freezes from Russian networks; DPIMonitor `probe_bulk` (2026-11-13)

The bulk probe (§48) has read ws `partial` in every run since it began: 210 of 210 by 2026-11-13. Each run got 4–14 KB of the 128 KB download, then 20 s of silence, while ws's HEADs read 30/30.

- **Where it breaks: the client's own leg to Cloudflare, from a Russian network.**
  - From entry (a Russian VPS), a plain `curl` of a 50 KB file through `sub.nekoweather.xyz:2096` stops at 20–26 KB.
  - The same file comes whole to the DE node in 0.14 s.
  - The ws tunnel run from DE (DE → Cloudflare → entry:2053 → exit) moves 1 MB in 1.7 s. So Cloudflare's leg to entry and the DNAT to exit are fine.
- **What is cut is our domain, not Cloudflare.**
  - From entry, these all download whole: `speed.cloudflare.com` pinned to the very address `cdn.nekoweather.xyz` resolves to (104.21.6.133), unpkg, jsDelivr, the npm registry, developers.cloudflare.com.
  - Either our zone is listed, or every little-known name behind Cloudflare is. Telling the two apart needs a fresh hostname.
- **TLS fragmentation does not help.** A ClientHello cut into 40-byte TCP segments froze at the same ~25 KB, so the sing-box profile's `tls.fragment` does not save Hiddify or Karing users.
- **Why nothing saw it:** every HEAD, client health check and `p-ws` telemetry row stays far below the threshold.
  - A Clash client's Cascade (fallback on gstatic) takes ws as alive.
  - Hiddify's balancer sends ws a share of flows: three AS41330 users split their connections about evenly between ws and the others (11-11…11-13).
  - Lockdown puts ws first.
- **What still works:** profiles are below the threshold (the probe account's Clash profile is 9.5 KB), so the Cloudflare mirror still serves them (`hop_tls.py verify`). Rule-sets come from `WEBAPP_URL` (exit, no Cloudflare).
- **Operator action, 2026-11-13:** ws demoted by hand (`cascade_manual`, by `owner`, until 2026-11-20 14:10 UTC). It is last for everyone, out of sing-box `auto`, and hidden in Hiddify. Its users left, so `inbound_drop:ws` paged critical every 2 h from 11-14 until the alert learned to give a demoted protocol no verdict (§48, 2026-11-15).
- **`probe_bulk`** (`bot/services/dpi_monitor.py`, ranked right after `probe_degraded`):
  - Fires on a switched-on cascade protocol when all of its newest `BULK_RUNS` (3) downloads are `partial` or `timeout`, its HEAD window is judged, and another cascade protocol's newest download passed. That last check is the control: if everything freezes, the cause is the download target or exit's egress, and nothing moves.
  - Demotes globally after the usual 2 evaluations.
  - Restores when its own newest download has passed for 6 evaluations (the C5 back-off applies). `error`, `proxy_down` or no rows give no verdict, and the demotion waits.
  - Never vetoed by users' connection counts (C7): the freeze comes after the handshake, so connections look normal.
  - Stale probes freeze it, as they freeze R1 and R2.
- **The structural fix** is a second front (plan B2/B3): a hostname or CDN that Russian DPI does not cut.
- **Tests:** `TestProbeBulk` in `tests/integration/test_dpi_monitor.py` (real sqlite, 16 cases). 20 mutants killed, restored by positional replacement with a hash check.
