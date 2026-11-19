# §45. The app chooser: /app, /app/<token>, /apk (2026-11-01, client plan E35)

Since 2026-10-31 every Android key holder gets the «📲 Открыть в NekoVPN» button, and the app gives demo keys 10 GB (§43). Without the app installed, every link used to lead to the GitHub release list: pre-releases with three APKs each. The page on the bot's own domain replaces it (`bot/services/neko_client.py`). The CF mirror serves it too, and the client repo publishes a static copy on GitHub Pages.

- **`GET /apk[?abi=…]`** — a 302 to the newest NekoVPN APK, arm64-v8a unless another ABI is asked for. It is a stable link that follows new releases. When GitHub cannot be asked, it goes to the release page.
- **`GET /app`** — the chooser. Three sections; the one for the visitor's device (by User-Agent) is open:
  - Android: NekoVPN (recommended; download, version and size, install steps, the other ABIs), Hiddify, FlClash;
  - iPhone and iPad: Karing;
  - computer: FlClash (recommended), Hiddify.

  Without a token it says where the subscription is (the bot, /sub); for demo it prints both allowances.
- **`GET /app/<token>`** (the /sub token, with /sub's 404 / 410) gives every app an «Добавить подписку» link, in the form that app reads:
  - NekoVPN: the `/open` intent with the Clash profile, falling back to this page;
  - FlClash: `clash://install-config?url=<Clash profile>`;
  - Hiddify: `hiddify://import/<plain /sub>#NekoVPN` (its LinkParser);
  - Karing: `karing://install-config?url=<plain /sub>&name=NekoVPN` (its scheme handler).

  Both subscription links are also there to copy by hand. A payer sees no demo line.
- **`/open/<token>`'s fallback** (the intent extra and the «Скачать» link) is `/app/<token>` instead of the release list. The demo texts (§43: key card, /sub, warnings, the monthly notice) link to `/app`.
- **Releases:** `ReleaseCache` asks `GET /repos/Annoyt/vpnautodeploy_client/releases` at most every 10 min; a failure is remembered for 1 min. It takes the newest non-draft `v…-neko.N` release with an arm64 APK; pre-releases count, since every build so far is one.
- **Headers:** `no-store`, `no-referrer`, `noindex`, `DENY` framing, and a CSP without any script.
- **Tests:** `tests/integration/test_neko_download.py` (real sqlite, GitHub faked); 23 mutants killed with a positional restore.
