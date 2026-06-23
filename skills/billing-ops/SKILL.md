---
name: billing-ops
description: Subscription, payment, quota, and refund operations. Reading subscription state, extending users, granting quota, looking up payment history.
type: prompt
whenToUse: User asks about payments, subscription renewals/extensions, quota grants (клиент оплатил, продли, верни деньги), refunds, billing, or pricing
---

# Domain map

There are three pieces of "billing" state per user:

| Where | What it tracks | Source of truth for |
|---|---|---|
| `users.subscription_expiry` (TEXT, ISO timestamp) | When the user's access expires | "До какого числа активен?" |
| `users.quota_gb` (REAL) | GB ceiling for traffic | "Сколько ГБ ему доступно?" |
| `subscriptions(id, chat_id, plan_type, started_at, expires_at, traffic_limit_gb, traffic_used_gb, is_active)` | Per-period billing record | Audit history, expiring soon, refund |

⚠️ **Schema gotcha**: prod columns are `started_at` / `expires_at`, NOT `start_date` / `end_date`. Code in `bot/core/database.py` was rewritten in May 2026 to match prod; if you see `start_date` in any query, it's wrong.

The dashboard's "Подписки" panel (`/api/admin/subscriptions`) buckets users into: `active`, `expiring_in_7d`, `expired`, `no_subscription`. The last bucket is users who got a key (demo) but never had a row inserted into `subscriptions` — usually demo flow gaps.

# Common operations

## "Покажи статус подписки юзера @X"

```sh
docker compose -f /opt/vpn-bot/docker-compose.yml exec -T vpn-bot python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
cur = c.cursor()
# Adjust the LIKE / chat_id as appropriate
cur.execute('''
  SELECT u.chat_id, u.username, u.status, u.subscription_expiry, u.quota_gb,
         s.plan_type, s.started_at, s.expires_at, s.is_active
  FROM users u LEFT JOIN subscriptions s ON s.chat_id = u.chat_id AND s.is_active = 1
  WHERE LOWER(u.username) LIKE ? OR u.chat_id = ?
''', ('%username_part%', 'chat_id_here'))
for r in cur.fetchall(): print(r)
"
```

## "Продли подписку @X на 30 дней"

There's no CLI command for this — propose the SQL, get user's OK, run it:

```sql
-- show before
SELECT chat_id, subscription_expiry FROM users WHERE chat_id = '<id>';

-- extend by 30 days
UPDATE users
SET subscription_expiry = datetime(COALESCE(subscription_expiry, datetime('now')), '+30 days')
WHERE chat_id = '<id>';

-- mirror into subscriptions if there's an active row
UPDATE subscriptions
SET expires_at = datetime(COALESCE(expires_at, datetime('now')), '+30 days')
WHERE chat_id = '<id>' AND is_active = 1;
```

**Always SELECT before UPDATE** so the user can sanity-check.

## "Дай @X +100 GB"

The dashboard has a button (`grant_100gb` action). From CLI it's two writes — bot DB + x-ui side:

```sh
# Update bot DB
docker compose -f /opt/vpn-bot/docker-compose.yml exec -T vpn-bot python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
c.execute('UPDATE users SET quota_gb = COALESCE(quota_gb,5.0) + 100 WHERE chat_id = ?', ('<id>',))
c.commit()
print('bot OK')
"
# x-ui side: easier to do via the bot's REST action
TOKEN=$(cd /opt/vpn-bot && python3 -c "
import os; from dotenv import load_dotenv; load_dotenv()
from bot.utils.admin_token import make_admin_token
print(make_admin_token(os.environ['BOT_TOKEN'], '1652899'))
")
curl -s -X POST "http://127.0.0.1:8080/api/admin/users/<id>/action?admin_token=$TOKEN" \
    -H 'Content-Type: application/json' -d '{"action":"grant_100gb"}'
```

Notifies the user automatically.

## "Сколько подписок истекает в ближайшие N дней"

```sh
docker compose -f /opt/vpn-bot/docker-compose.yml exec -T vpn-bot python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
cur = c.cursor()
cur.execute('''
  SELECT chat_id, plan_type, expires_at
  FROM subscriptions
  WHERE is_active=1
    AND datetime(expires_at) BETWEEN datetime('now') AND datetime('now','+7 days')
  ORDER BY expires_at
''')
for r in cur.fetchall(): print(r)
"
```

## Refund / "верни деньги"

There's no payment-processor integration — payments are off-platform (user transfers, admin confirms). A refund flow is therefore:
1. Admin transfers money back outside the bot (Telegram has no money flow here).
2. Bot-side, just close the subscription early:

```sql
UPDATE subscriptions SET is_active = 0, expires_at = datetime('now') WHERE id = <sub_id>;
UPDATE users SET status = 'rejected', subscription_expiry = NULL WHERE chat_id = '<id>';
```

3. Revoke the key via dashboard or `/reject` so they lose VPN access immediately.

# Patterns to flag

- A `paid` user with `subscription_expiry < now()` → bot should have moved them to `rejected`, didn't. Likely the expiry-check scheduler stalled (look at `notifications._check_expiring_subscriptions_sync` logs).
- A user in `subscriptions.is_active = 1` with `expires_at < now()` → same, ghost row.
- A user with `users.uuid` set but no row in `subscriptions` → demo issued before the subscriptions table was populated (counted in dashboard's `no_subscription` bucket). For demos this is OK; for `paid` users this means payment was approved but not recorded.

# Tariff / pricing knowledge

- **Demo**: 5 GB / 7 days, automatic on approval. Controlled by `DEMO_TRAFFIC_GB` and `DEMO_DAYS` in `/opt/vpn-bot/.env`.
- **Paid**: handled by admin via `/approve_payment` or dashboard. There is no fixed plan catalogue in code — admin decides duration/quota per case.

# Things you must NOT do

- Don't issue `UPDATE` on `users` or `subscriptions` without showing the SELECT result and the proposed UPDATE to the user first.
- Don't process refunds programmatically — the actual money move is the user's job.
- Don't write to the x-ui SQLite directly; the sync loop will overwrite. Use the bot's REST API instead.
- Don't grant unlimited quota (`quota_gb = 999999`) — set a real number you agreed with the user.
- Don't log payment amounts or method details — the audit row should say "extended +30d" / "granted +100gb", not include any money figure.
