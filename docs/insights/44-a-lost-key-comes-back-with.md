# §44. «🔑 Мой ключ»: a lost key comes back with its tier, a lapsed payer's stays off (2026-10-30)

When the panel has lost a key holder's client (3x-ui detaches long-expired clients from every inbound; a panel wipe), `GetKeyHandler._resync_existing_key` puts it back. Until now it used demo values for every tier: 10 GB, no expiry. A lapsed payer got a free key that never expired, and an active payer lost their quota and paid-until date.

- **Values** come from `billing.tier_client_values(user, config)`:
  - A payer (`ru_exit.is_eligible`) gets what a payment writes (`_paid_client_values`): `paid_quota_gb`, the paid-until date in ms converted the way `grant_paid_access` does it, and `paid_limit_ip`. No date = no expiry. An admin's quota 0 (`/quota @user 0`, unlimited) stays 0, as their limit 0 does; a payment used to floor it to 100 GB.
  - A lapsed payer (status `paid` after the date, or `support_topic` with a passed date) gets `None`: nothing is written to the panel. The user gets «Платная подписка закончилась DD.MM.YYYY…» with /buy and a «💳 Продлить подписку» button (`buy_menu`), not the key card. A re-add with the past expiry would only put back a key that is off. A payment re-adds a lost client itself, under the same uuid.
  - A demo user gets the key's own monthly quota: `users.quota_gb`, which the monthly reset (10 or 5 GB, §43), the app's raise and an admin's gift write, never below `DEMO_OTHER_TRAFFIC_GB` (an admin's 0 stays 0). Plus DEMO_DAYS from now, like a new key, and their own address limit. If an app key's stored quota is stale, the app's next fetch raises it.
- **Re-add:** `billing.readd_client` (add under the same uuid, then the in-place update), shared with `grant_paid_access`. The update is required: the add of a detached client only re-attaches its relational record, and the stale `client_traffics` row (enable=0, old expiry) keeps gating the key. The old button re-add had no update, so a detached key it restored stayed off.
- **The attachment gate fails closed.** `XUIAPIClient.get_inbound` answers None on any error (a timeout, a refused login), and `client_attached_sync` used to read that as "not on that inbound". So a panel hiccup made an attached client look lost, and the button, the monthly demo job or the audit would re-add it: delete + re-add, zeroed traffic. It now answers "attached" when a configured inbound read fails. A configured inbound id the panel does not have blocks re-adds the same way, so keep the ids right. Tests: `tests/integration/test_xui_client_attached.py`.
- **One attempt**, not `_sync_to_xui`'s three, and not through `sync_user`. When an add went through but its answer was lost, a retry adds a client the panel has again: delete + re-add, zeroed traffic. `sync_user` also reports a failed xray reload (entry's sidecar) as a failed add.
- **No second demo quota in a month:** an attached client, a spent one included, is never re-added. The add of a detached client keeps its traffic row, and the update never touches used bytes; only resetTraffic does, and only the monthly job calls it. Limit: a panel that lost the row too (a wipe) has no usage left to keep, so such a key comes back with the full quota.
- **Keys the old button rewrote:** run `docker exec -w /app vpn-bot python3 scripts/paid_keys_audit.py`. Its dry run is the audit:
  - for every payer with a key, the panel's quota, expiry, limit and enable against what a payment writes, plus the bot's quota and limit floors;
  - for a lapsed payer, whether the key still works.

  `--apply` fixes active payers: the bot's floors first, then the key in place, or a re-add under the same uuid when the panel lost it. `--apply --lapsed` also switches lapsed payers' working keys off (the passed paid-until date plus enable=false). It is a separate flag because it takes a working key away.

  A failed panel read is reported and never acted on. Each write is audited as `admin_actions('owner', 'paid_keys_audit', <chat_id>, <what changed>)`.

  Left alone on purpose:
  - an unlimited quota on the panel (the old button wrote 10 GB, never 0);
  - an expiry within a minute of the bot's date (`/addmail` reads the clock twice);
  - `probe_internal`, the health checker's client.

  First prod check (read-only, 2026-10-30): 9 payers, all exactly as a payment writes them, nothing to fix.

  Tests: `tests/integration/test_paid_keys_audit.py`, using the shared fake `tests/integration/relational_panel.py`. 40 mutants killed, restored byte for byte with a hash check.
- **Unchanged:** a lapsed payer whose client is still attached gets the key card, as with `/mykey`. **Not done:** a payer's first key (`_create_new_key`, no uuid yet) is still built from demo values.
- **Tests:** `tests/integration/test_mykey_lost_key.py` (real sqlite; the fake panel has the fork's relational semantics), `tests/integration/test_xui_client_attached.py`, `TestPaidQuotaGb` in `tests/unit/test_billing.py`, the lost-key cases in `tests/unit/test_user_callbacks.py`, and `TestLostKeyReaddIsOneAttempt` in `tests/unit/test_get_key_handler_phase1.py`. 49 mutants killed (PR #32's re-add and the old fail-open gate put back among them), restored byte for byte with a hash check.
