# §43. Demo allowance by client app: 10 GB with NekoVPN, 5 GB otherwise (2026-10-30)

Owner's decision: demo users who run our NekoVPN app get 10 GB a month, everyone else gets 5 GB. The quota belongs to the key, not to a device: the app on one phone gives the key 10 GB everywhere it is used. Code: `bot/services/demo_tiers.py`.

- **Settings.** `DEMO_TRAFFIC_GB` (10) is the allowance with the app; `DEMO_OTHER_TRAFFIC_GB` (new, default 5) is the allowance with any other client. Entry's compose and .env pass only `DEMO_TRAFFIC_GB=10`, so the 5 comes from the code default.
- **Recognising the app.** A profile fetch (`/sub`, any format except `clash-proxies`) whose User-Agent starts with `NekoVPN/v<version>`. FlClash sends `FlClash/v…` and our self-test sends `NekoVPN-selftest`; neither counts.
  - Each app fetch upserts `client_apps(chat_id, app='nekovpn', version, last_seen)`. The table holds no address and no raw header.
  - `/sub` never waits for this. `WebAppServer._schedule_neko_app` runs it in the background on a thread of its own, at most one call per uuid.
- **Raising a key.** `grant_app_allowance` re-reads the user and acts only on status `demo` with `quota_gb` below 10.
  - The panel gets an in-place update: `totalGB` 10 GB and `enable` true, so a key that ran out of its 5 GB gets the rest at once.
  - Then `users.quota_gb` = 10. A refused update changes nothing, and the app's next fetch (every 6 h) tries again.
- **Monthly reset (the 1st).** Keys the app fetched within 7 days (`NEKO_APP_SEEN_DAYS`) get 10 GB; the rest get 5. The panel quota and `users.quota_gb` follow.
  - If `client_apps` is unreadable, everyone gets 5 for the moment, and the app's next fetch raises its keys.
  - Nobody is cut mid-month: November 1 is the first reset with the split.
- **New keys** (Telegram, mail requests) start at 5 GB with `users.quota_gb` = 5. A key the panel lost comes back with its own `users.quota_gb`, never below 5 GB (§44).
- **Texts: what counts.** A phone's own counter counts everything, so every place that shows a quota states the rule:
  - the stats screens (`/stats`, the 📊 button);
  - the 80% / 100% warnings;
  - the monthly refresh notice;
  - the demo key card and `/sub`.

  The wording is the owner's, from the 2026-10-30 broadcast: «В NekoVPN и FlClash учитывается ТОЛЬКО трафик вне РФ: российские сайты открываются напрямую и в лимит не входят».
  - It names NekoVPN and FlClash because only there does our profile's routing apply. Hiddify drops it, and there Russian sites go through the VPN and count.
  - iOS texts say only «Учитывается только трафик через VPN»: the iPhone card names Karing only, and Karing's routing is unverified.
  - A demo key under 10 GB also gets the app's download link (`neko_client.RELEASES_URL`; NekoVPN is Android-only).
  - Upsell lines name no paid quota: the 100% warning, `/help` and the `/buy` command description just say /buy.
- **Tests:** `tests/integration/test_demo_tiers.py` (real sqlite: the UA, the record and its window, the raise, `/sub` in the background, the reset, warnings, cards, stats, new keys) and `test_client_apps_table`. 34 mutants killed with a positional restore.
