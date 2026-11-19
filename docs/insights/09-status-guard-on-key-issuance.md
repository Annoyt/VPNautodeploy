# §9. Status guard on key issuance

`GetKeyHandler._KEY_ALLOWED_STATUSES` and `CommandHandler._MYKEY_ALLOWED_STATUSES` are `{"demo", "paid", "support_topic"}`. A rejected user can still hit "Demo" → `PENDING_DEMO` (allowed by state machine), but `_process_key_request` refuses to hand out a key until they go through admin approve again. Without this, a stale `uuid` in the user row let them resync their old key.
