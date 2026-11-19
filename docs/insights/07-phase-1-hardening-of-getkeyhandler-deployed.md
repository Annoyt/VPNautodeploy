# §7. Phase 1 hardening of GetKeyHandler (deployed 2026-05-12)

The key-issuance flow has three guardrails layered onto it:
- `GetKeyHandler._inflight_chat_ids` (class-level set + `threading.Lock`) drops duplicate "get key" callbacks that fire while the first one is still running. Survives across `asyncio.run()` cycles that `_run_async` may spin up.
- `_sync_to_xui` retries up to `SYNC_MAX_ATTEMPTS=3` with exponential backoff (`SYNC_BASE_BACKOFF_SEC * 2^(attempt-1)`) and per-attempt `SYNC_TIMEOUT_SEC=15.0` timeout. Tests monkeypatch these constants to keep CI fast.
- `_send_key_to_user` and `/mykey` both call `bot/utils/validators.py::validate_vless_url` before notifying the user, rejecting `vless://None@...`, `vless://@host`, missing port, etc.
Don't remove any of these without updating `tests/unit/test_get_key_handler_phase1.py` and `tests/unit/test_key_creation_rollback.py`.

For full procedures, see `docs/PROJECT.md` §6 and `scripts/deploy.sh`.
