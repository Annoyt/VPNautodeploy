# §39. NekoVPN reads the profile's headers: refresh interval, mirrors (2026-10-25)

Client 0.9.2-neko.1 (`Annoyt/vpnautodeploy_client` PR #8) acts on two headers of the `?format=clash` answer. Other clients ignore both, and response bodies are unchanged.

- **`profile-update-interval`** (the bot has always sent `6`): the app sets the profile's auto-update interval from it on every fetch, clamped to 1 h–7 d. FlClash ignores the header and refreshes once a day.
- **`nekovpn-mirrors`** (clash format only, the one the app fetches): this very profile on each https `SUB_MIRROR_URLS` base, with the same token and the same query (`WebAppServer._profile_mirror_urls`).
  - The app stores the list per profile. When the profile's own URL fails, it fetches from these in order.
  - http mirrors are left out because the URL carries the token.
  - A mirror must reverse-proxy `/sub/` to this bot, as `sub.nekoweather.xyz:2096` does.
- **A fetch through the Cloudflare mirror says nothing about the user's network.** Caddy on exit `:2096` sees the CF edge, not the client, so such a request resolves to AS13335. `CDN_HOP_ASNS` (`web_server.py`, Cloudflare) makes `/sub` treat it like our own addresses (§29): nothing is recorded, and the stored network orders the profile. WARP users are covered by the same rule. Before 2026-10-25 these fetches stamped AS13335 on the user, and the mirror fallback would have multiplied that.
- Tests: `TestMirrorsHeader` in `tests/integration/test_clash_channels.py` and `TestCdnHop` in `tests/integration/test_sub_own_address.py`; 6 mutants killed by reverse text replacement (§28).
