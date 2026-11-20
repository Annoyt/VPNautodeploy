# §37. NekoVPN client releases: the bot's message with «Что нового» (2026-10-15)

The client (FlClash fork, `Annoyt/vpnautodeploy_client`) ships test builds as GitHub pre-releases; since 0.8.99-neko.2 the app itself offers only `v…-neko.N` releases of that repository (test builds see pre-releases). When a release is PUBLISHED, `scripts/client_release_notify.py` tells the testers:

- Runs inside vpn-bot: `docker exec -w /app vpn-bot python3 scripts/client_release_notify.py <tag>` = dry run (recipients with verdicts + the text); `--send` delivers; `--to id,id` overrides; `--force` re-sends; `--set-testers id,id` stores the default list in `app_settings.client_testers`.
- «Что нового» = the `- ` items between `<!-- flclash:changelog:begin/end -->` in the release body — the markers the app's update dialog reads, filled by the client repo from its `CHANGELOG-NEKO.md`, so the dialog, the release page and the message say the same. No items or no arm64-v8a APK → no message (exit 1); a draft is refused (the API does not show it without a token, and its links 404 for testers).
- One message per tester per tag: the audit row `admin_actions('owner', 'client_release_notify', <chat_id>, <tag>)` is also the guard; inactive or unknown chat_ids are listed, not messaged. The APK (~57 MB) is over the Bot API's 50 MB upload limit, so the message carries a download button, not the file.
- Tests: `tests/integration/test_client_release_notify.py` (real sqlite, GitHub/Telegram faked; one test runs the script as a file from a bare interpreter, the way its first live run failed on `import bot`); 17 mutants, all killed or removed as dead code, restored by reverse text replacement (§28).

**Everyone with a key, `--key-holders` (2026-11-21).** The owner sent 0.9.2-neko.6 to all key holders, not only the testers: «новый клиент и сразу с подпиской».
- Recipients: demo, paid and support_topic users with a uuid, in the order they joined.
- Left out, each with its verdict in the dry run:
  - a payer past the paid-until date: the panel keeps the key off until they pay;
  - a user who picked iPhone at the platform step: NekoVPN has no iOS build;
  - an `ext_*` user: no Telegram chat.
- Each message has the download button and the user's own two: «📲 Открыть в NekoVPN» (`/open/<token>`, §38) and «💻 Другие устройства» (`/app/<token>`, §45). The text adds how a newcomer gets the subscription in.
- The dry run prints a count per verdict and the buttons' labels, never a link. It asks the bot's own listener (`NEKO_LOCAL_BASE`, default `http://127.0.0.1:8080`) for the first user's `/open`: anything but 200 means the script derives tokens the bot does not know.
- The same audit row as the testers' message, so nobody gets one tag twice.
