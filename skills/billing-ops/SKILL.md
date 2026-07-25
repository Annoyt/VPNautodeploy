---
name: billing-ops
description: Subscription, payment, quota, and refund operations. Reading subscription state, extending users, granting quota, looking up payment history.
type: prompt
whenToUse: User asks about payments, subscription renewals/extensions, quota grants (клиент оплатил, продли, верни деньги), refunds, billing, or pricing
---

You run on **entry**; the bot DB (`/var/lib/vpn-bot/bot.db`) is **local**. Shorthand:
`DC="docker compose -f /opt/vpn-bot/docker-compose.yml -f /opt/vpn-bot/docker-compose.entry.yml"`.
**Always SELECT before UPDATE** and show the user before mutating.

# Domain map

Three pieces of "billing" state per user:

| Where | What it tracks | Source of truth for |
|---|---|---|
| `users.subscription_expiry` (TEXT, ISO) | When access expires | "До какого числа активен?" |
| `users.quota_gb` (REAL) | GB ceiling for traffic | "Сколько ГБ доступно?" |
| `subscriptions(id, chat_id, plan_type, started_at, expires_at, traffic_limit_gb, traffic_used_gb, is_active)` | Per-period billing record | Audit history, expiring soon, refund |

⚠️ **Schema gotcha**: prod columns are `started_at`/`expires_at`, NOT `start_date`/`end_date`. If a query uses
`start_date`, it's wrong. Also note `users.contact_email` (backup key delivery) vs `users.email` (synthetic x-ui id).

The dashboard "Подписки" panel (`/api/admin/subscriptions`) buckets users into `active`, `expiring_in_7d`,
`expired`, `no_subscription` (got a demo key but no `subscriptions` row).

# Common operations

## "Покажи статус подписки юзера @X"
```sh
$DC exec -T vpn-bot python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
for r in c.execute('''
  SELECT u.chat_id, u.username, u.status, u.subscription_expiry, u.quota_gb,
         s.plan_type, s.started_at, s.expires_at, s.is_active
  FROM users u LEFT JOIN subscriptions s ON s.chat_id = u.chat_id AND s.is_active = 1
  WHERE LOWER(u.username) LIKE ? OR u.chat_id = ?
''', ('%username_part%', 'chat_id_here')): print(r)
"
```

## "Продли подписку @X на 30 дней"
No CLI command — propose the SQL, get OK, run:
```sql
SELECT chat_id, subscription_expiry FROM users WHERE chat_id = '<id>';           -- before
UPDATE users SET subscription_expiry =
  datetime(COALESCE(subscription_expiry, datetime('now')), '+30 days') WHERE chat_id = '<id>';
UPDATE subscriptions SET expires_at =
  datetime(COALESCE(expires_at, datetime('now')), '+30 days') WHERE chat_id = '<id>' AND is_active = 1;
```

## "Дай @X +100 GB"
Prefer the bot's REST action (updates bot DB **and** x-ui on exit, notifies the user):
```sh
TOKEN=$($DC exec -T vpn-bot python3 -c "
import os
from bot.utils.admin_token import make_admin_token
print(make_admin_token(os.environ['BOT_TOKEN'], '<super-admin-id>'))
")
curl -s -X POST "http://127.0.0.1:8080/api/admin/users/<id>/action?admin_token=$TOKEN" \
    -H 'Content-Type: application/json' -d '{"action":"grant_100gb"}'
```

## "Сколько подписок истекает в ближайшие N дней"
```sh
$DC exec -T vpn-bot python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
for r in c.execute('''
  SELECT chat_id, plan_type, expires_at FROM subscriptions
  WHERE is_active=1 AND datetime(expires_at) BETWEEN datetime('now') AND datetime('now','+7 days')
  ORDER BY expires_at'''): print(r)
"
```

## Refund / "верни деньги"
Payments are off-platform (user transfers, admin confirms) — there is no processor to refund. Flow:
1. Admin returns money outside the bot.
2. Close the subscription early (SELECT first, then, with OK):
```sql
UPDATE subscriptions SET is_active = 0, expires_at = datetime('now') WHERE id = <sub_id>;
UPDATE users SET status = 'rejected', subscription_expiry = NULL WHERE chat_id = '<id>';
```
3. Revoke the key via the dashboard or `/reject` so VPN access drops immediately.

# Patterns to flag
- `paid` user with `subscription_expiry < now()` → the expiry scheduler stalled (`notifications._check_expiring_subscriptions_sync`).
- `subscriptions.is_active=1` with `expires_at < now()` → ghost row, same cause.
- `users.uuid` set but no `subscriptions` row → demo (OK) or approved-but-unrecorded paid (investigate).

# Tariff / pricing
- **Demo**: `DEMO_TRAFFIC_GB` / `DEMO_DAYS` in `/opt/vpn-bot/.env`, automatic on approval.
- **Paid / Telegram Stars**: plans in env `PLAN_1M_STARS` … `PLAN_12M_STARS`; admin can also `/approve_payment`.

# Must NOT
- UPDATE `users`/`subscriptions` without showing the SELECT + proposed UPDATE first.
- Process refunds programmatically (money move is the human's job).
- Write x-ui SQLite directly (sync loop overwrites) — use the REST API.
- Grant `quota_gb = 999999`; set a real agreed number.
- Log payment amounts/method — audit says "extended +30d" / "granted +100gb", no money figures.
