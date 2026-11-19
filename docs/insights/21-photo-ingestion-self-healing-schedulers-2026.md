# §21. Photo ingestion + self-healing schedulers (2026-06-01)

Closed three remaining gaps in the support/AI flow.

**Telegram → Kimi photo ingestion.** `AIHandler.can_handle` was gating on `text` only — any message that contained a photo (with or without caption) silently dropped because `msg.get("text")` was empty. Now `can_handle` accepts photo OR text OR caption, and `handle` calls a new `_download_photo` helper that pulls the largest photo size via `TelegramClient.download_file` (also new — getFile + streamed HTTP) into `/tmp/tg_media/tg_photo_<chat>_<ts>_<file_id_tail>.jpg`. The host path is spliced into the prompt as `[Вложение от админа: …]`. `docker-compose.yml` mounts `/tmp/tg_media:/tmp/tg_media` so the kimi binary on the host sees the same file. A `finally` block in `handle` unlinks the photo after the request — temp files do not accumulate.

**Self-healing schedulers.** `NotificationService.start_scheduler` now registers two new APScheduler jobs:
- `support_state_repair` (hourly) finds users with `status='support_topic'` AND `support_topic_id IS NULL` and reverts them via `StateMachine.set_state`. This is the "tapped Поддержка but never wrote" pattern — 5 users were stuck like that on prod (boriskonale, Madina_Fat, sergod72, Ilyastarasov, ImLovingIt7), one-shot DB UPDATE flipped them back to demo.
- `tg_media_cleanup` (hourly) walks `/tmp/tg_media/` and unlinks any file with mtime > 1h. Backstop for the per-request `finally`.

**StateMachine.return_from_support edge cases.** Old code defaulted to DEMO unconditionally if the previous_state wasn't DEMO or PAID. Two problems: (a) a user who never had a key would jump from support_topic to demo, which is wrong (no email = need re-approval); (b) if the validated transition was rejected, the user would stay stuck. Now default is `DEMO if user.email else NEW`, and if `transition` returns False we fall through to `set_state` to force the move. The self-loop case where `previous_state == support_topic` also falls through to the email-based default instead of ping-ponging.

**Skills + triggers.** Five skills live now (`vpn-ops`, `server-admin`, `code-review`, `incident-response`, `billing-ops`). Trigger detection (`_detect_skill_domains`) routes by per-domain marker tuples; `incident-response` short-circuits everything else when it fires so the reminder during an outage doesn't drown in four skill paths.

**Branch cleanup.** `feature/xray-bot-integration` was the default branch on GitHub but `0` commits ahead of `main` (everything was already merged a few sessions ago). Switched the GitHub default to `main` and deleted the feature branch. Single canonical line of history.
