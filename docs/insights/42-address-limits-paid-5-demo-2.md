# §42. Address limits: paid 5, demo 2 (2026-10-30, demo 2026-11-01)

The exit panel enforces `limitIp` with fail2ban (jail `3x-ipl` inside the 3x-ui container). When a key goes over its limit, the older address is banned for 30 min. Every key got the new-key default of 1, so a payer on a phone and a laptop, or anyone on the app's two-networks mode or behind its gateway, kept losing one of them.

- **`PAID_LIMIT_IP`** (default 5) and **`DEMO_LIMIT_IP`** (default 2, owner's decision of 2026-11-01). Both are passed by compose; 0 = no limit.
  - `billing.paid_limit_ip` / `demo_limit_ip` return the larger of the stored limit and the tier's value. An admin's 0 (`/set_limit @user 0`, the dashboard) stays 0.
  - Written to `users.limit_ip` and the panel by: `grant_paid_access` (every payment); `/addmail` (either tier); a new Telegram key; the lost-key re-add (`tier_client_values`); the monthly demo job's re-add of a detached client.
- **«🔑 Мой ключ»** (main menu → `GetKeyHandler`) no longer re-adds a key the panel still has. On the relational panel a re-add is delete + re-add: it zeroed the key's traffic and rewrote it with demo values (1 address, 10 GB, no expiry). A key the panel lost is still re-added, with its tier's values (§44).
- **Keys made before:** `docker exec -w /app vpn-bot python3 scripts/limit_ip.py` is a dry run; `--apply` updates; `--tier paid|demo` narrows it. (It was `paid_limit_ip.py` until 2026-11-01.)
  - It reads every key inbound once, Shadowsocks first, because the panel had drifted from the bot: keys it took in before the camelCase fix are unlimited (0) there.
  - Every change is audited as `admin_actions('owner', 'limit_ip', <chat_id>, '<tier> old->new')`. The paid run of 2026-10-30 is under the old action name `paid_limit_ip`.
- **Tests:** `tests/integration/test_limit_ip.py` (real sqlite) and `TestPaidLimitIp` / `TestDemoLimitIp` in `tests/unit/test_billing.py`. 42 mutants killed with a positional restore.
