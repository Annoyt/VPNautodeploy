# §25. Client-app churn: Happ out, Karing in; multiple /sub formats (2026-07-25)

RU App Store purged proxy clients in waves: first Hiddify, then Happ (verified gone by 2026-07-25). Current recommendations: **Android/PC → Hiddify**, **iOS → Karing** (sing-box, still in RU store, reads the plain `/sub` with zero special-casing). All user-facing text (key card, /sub, PLATFORM_INSTRUCTIONS, email letter) reflects this.

`/sub/<token>` now serves **three formats** (web_server.handle_subscription):
- **default sing-box JSON** — Hiddify/Karing;
- **`?format=links`** (or a `Happ*` UA) — plain-text share-links, one server per line, for v2rayNG/Streisand. MUST stay plain text: Happ iOS silently imported nothing from a base64 blob. Reuses VPNService generators; `generate_hy2_link` now carries `obfs`/`mport` params (raw hy2 links were dead since salamander went live server-side — try_alt:hy2 fixed by the same change);
- **`?format=xray`** — full xray-core client config. Happ imports it as a SINGLE profile (passes 1:1 to core) — that's why "only one key" showed up; kept for raw-config/TV use cases.

**Dashboard `grant_paid` action** — demo/support_topic → PAID via normal transition, user notified, full cascade + DE fallback unlock on next /sub refresh. Hidden for already-paid rows.

**📧 email prompt is now stateful** — the button arms `MessageHandler.PENDING_EMAIL` (10 min TTL); the user's next plain-text message is validated and saved to `contact_email`. Previously the button only printed /setemail instructions and users' bare-address replies died in the "I don't understand" fallback. Gotcha that cost a debug cycle: `callbacks/user.py` has TWO `EmailPromptHandler` classes — the second definition (bottom of file) shadows the first; edit the bottom one.

**Platform re-selection**: `setplat:<p>` buttons on the key card and /sub let key holders switch device without admin help. Deliberately bypasses `PlatformSelectHandler` (its `_process_platform_selection` forces a DEMO transition) — SetPlatformHandler only updates `user.platform` and re-renders the card via the shared `build_key_delivery_message()`.

**ziriki LTE case (2026-07-25)**: hy2 handshake passes on throttled mobile UDP but streams die in ~8s (`tx:0` → "timeout: no recent network activity"). Nothing server-side left to fix — QUIC needs clean UDP. UDP *apps* (calls) still work over xudp inside TCP protocols via the 'calls' selector. Also: roaming RU SIMs get home-operator DPI abroad.
