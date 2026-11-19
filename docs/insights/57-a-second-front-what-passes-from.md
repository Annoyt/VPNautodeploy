# §57. A second front: what passes from Russia, measured from entry (2026-11-15)

§55 left one question: is our Cloudflare zone listed, or does every little-known name behind Cloudflare freeze? Only a fresh name could tell. It was answered without creating anything on Cloudflare. To the DPI, a little-known third-party name behind Cloudflare, pinned to our address, looks the same as a fresh name of ours: it sees the address and the TLS name, nothing else.

- **How it was measured** (owner's OK, read-only, nothing written on entry):
  - A stdlib script run by entry's own `python3`: one TLS 1.3 GET over HTTP/1.1 per attempt, no compression, 3 rounds per target.
  - "Frozen" = no byte for 10 s inside the bulk probe's 20-s budget.
  - Cloudflare targets were pinned to 104.21.6.133 (cdn / sub) and also fetched on their own addresses.
  - The script and the raw output are in the side-session notes (`cdn-front*`), not in the repo. §55's plain `curl` from entry saw the same cut, so curl is enough for a quick check.
- **Cloudflare, 2026-11-14 21:19–21:31 UTC:**
  - **Whole:** speed.cloudflare.com, 128 KB, 9 of 9 (pinned, on its own address, and with the ClientHello sent in 40-byte segments).
  - **Frozen** at 23–26 KB of body (28.6–31 KB of TCP payload), every round:
    - our `sub.nekoweather.xyz:2096` (the 48.9 KB rule-set);
    - a little-known zone (immersivefactory.com), fragmented too;
    - two `workers.dev` names;
    - the well-known developer sites bun.sh, astro.build and hono.dev.
  - **`pages.dev` is blocked outright:** the names do not resolve on entry, and a handshake pinned to the address gets no answer.
  - So it is not our zone. Every name outside a short allow-list freezes, on the same address and colo. The DPI reads the name through a fragmented ClientHello: an allowed name passes fragmented, an unknown one freezes fragmented. A new name of ours, in any zone or on workers.dev, would freeze. Nothing was created on Cloudflare.
- **The other ways through Cloudflare are closed:**
  - **Fronting** (an allowed name as SNI, ours as Host): Cloudflare itself answers 403 to every cross-zone pair (npm / unpkg / speed, checked from outside Russia on 2026-11-15), although `WS_SNI` can already differ from `WS_HOST`.
  - **ECH:** the zone publishes it (outer name `cloudflare-ech.com`). The DPI has frozen ECH with that name since 2024-11-05 (net4people/bbs#417).
  - **Fragmenting the ClientHello:** no effect (above).
- **Other CDNs from entry, 21:27–21:33 UTC** (little-known names on their own addresses, 3 rounds each):
  - **Whole:**
    - Fastly (`*.edgecompute.app`, 146.75.117.51): 1.5 MB and 225 KB;
    - Azure Front Door (`*.azurefd.net`, 13.107.213.45 and 150.171.109.197): 87 KB and 443 KB;
    - Google Cloud Run (`*.run.app`, 34.143.x): 430 KB and 191 KB;
    - Fly.io (`*.fly.dev`, 77.83.140.94): 143 KB;
    - Akamai (its own test content): 311 KB, but from a cache inside Russia (87.245.198.139 is RETN's Russian network), so it says nothing about a foreign edge.
  - **Frozen:** bunny.net (`*.b-cdn.net`, CDN77's range, at 15–16 KB) and CloudFront (`*.cloudfront.net`, in some rounds inside the handshake).
- **The bot's own referee agrees:** the ws bulk probe in the 24 h to 2026-11-14 21:21 UTC read 96 of 96 `partial`, 4–14 KB of 128 KB.
- **What follows:**
  - ws through Cloudflare stays demoted (`probe_bulk` holds it); no new name fixes it.
  - Profiles still pass the Cloudflare mirror (a paid sing-box profile is about 9.4 KB, a Clash one about 10.5 KB, under the cut). Anything bigger through it freezes.
  - Entry is a Moscow datacenter. Home and mobile networks may judge these providers differently, and an address range that passes today can be frozen later, as Cloudflare's was.
- **The four that passed, from their official docs (2026-11-15):**

  | | WebSocket to our origin | Cost | Account from Russia |
  |---|---|---|---|
  | **Fly.io** | Native through its HTTP handler, no proxy idle timeout; raw TCP/TLS passthrough too | VM from $2.19/mo, EU egress $0.02/GB, no base fee | Card (Stripe) or ≥ $25 of prepaid credit; no proxy clause found |
  | **Fastly** | WebSockets passthrough, paid accounts only (≥ $50/mo); Compute caps a request at 2 min | ≥ $50/mo, EU $0.12/GB | Card; Russia not on its embargo list |
  | **Azure Front Door** | Standard/Premium, `Upgrade: websocket` only, idle 5 min, a connection lasts at most 2 h | $35/mo base + $0.083/GB + requests | Card and phone; Microsoft stopped new sales in Russia in March 2022 |
  | **Google Cloud Run** | Native, but a connection lasts at most 60 min, and the instance is billed while any connection is open | CPU for every open hour + $0.12/GiB | No new Russian sign-ups since 2022 |

  - Not measured: Gcore (cheapest on paper: 1 TB/mo free, then €0.01/GB; WebSockets option; needs a domain of our own). Measure it from entry before considering it.
  - Our clients' httpupgrade handshake carries no `Sec-WebSocket-Key`. Cloudflare accepts that; a provider that checks the WebSocket handshake may not, and then the front needs the real `ws` transport and an inbound of its own on exit. Only a check through the provider answers it.
- **Rollout of a second front (nothing done yet; the owner picks the provider and opens the account):**
  1. **Provider:** Fly.io, unless the owner prefers another. It costs about $20 per TB, keeps WebSockets open with no idle timeout, and can fall back to raw TCP. Its addresses are a hoster's range, the kind the DPI froze elsewhere, so the bulk probe stays the judge. Fastly is the fallback (a big shared CDN, but ≥ $50/mo).
  2. **Relay:** one small app in `fra`, next to exit, behind its default `<app>.fly.dev` name (the suffix measured above). It hands the upgraded connection to the existing ws origin (inbound-2053 behind entry:2053) with Host and TLS name `cdn.nekoweather.xyz`, as Cloudflare does today. Exit and entry stay as they are, unless entry's firewall admits only Cloudflare on :2053 (not recorded in the repo); then the relay's address needs adding.
  3. **Check from entry before any code** (needs the owner's OK): a one-off sing-box client in a throwaway container, with one outbound (VMess + httpupgrade to `<app>.fly.dev:443`, the probe account's key), downloading the bulk probe's 128 KB three times. All three must be whole. This also shows whether the provider takes our handshake.
  4. **Code, one PR:** a new opt-in protocol in the xhr mould (§51): settings for the front's host, sing-box / Clash / xray / link builders reusing `_build_ws`, a probe port in `HealthChecker.probe_ports_for` and `gen_probe_config.py`, `PROTOCOL_TIER`, `OPT_IN_PROTOCOLS`, `lockdown.FRONTED_PROTOCOLS` and `LOCKDOWN_ORDER`, telemetry, and kept out of Calls like ws.
  5. **Deploy it switched off**, regenerate the probe config, and watch `outbound_bulk` for the new tag for at least 24 h: every run `ok`.
  6. **Switch it on** in the cascade editor. ws stays demoted while `probe_bulk` holds it. Clients pick the new server up on their next profile refresh.
