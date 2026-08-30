---
name: user-ops
description: User lifecycle — create users (email-only included), find users, grant paid access. The ONLY sanctioned paths for touching the users table.
type: prompt
whenToUse: User asks to create/add a user (создай юзера, добавь пользователя, выдай ключ на почту, addmail), find who a user is (кто такой ext_..., найди юзера), or grant/extend paid access
---

You run on **entry**; the bot DB (`/var/lib/vpn-bot/bot.db`) is local. Shorthand:
`DC="docker compose -f /opt/vpn-bot/docker-compose.yml -f /opt/vpn-bot/docker-compose.entry.yml"`.

# ⛔ The one hard rule

**NEVER create or repair a user with raw SQL** (`INSERT INTO users…`, `ALTER TABLE users…`).
Provisioning touches THREE stores at once: the bot DB row, the x-ui client on **exit** (real key), and the
subscription URL. A raw INSERT produces a zombie row and a key that does not exist in the panel.
*Incident 2026-08-30: an agent raw-INSERT for trsvtatyana@gmail.com made a `chat_id=NULL` row with a fake
UUID; the real user had to be provisioned again properly.*

If a query fails with "no such column" — **your query is wrong, not the schema**. Read the real schema
(`PRAGMA table_info(users)`, read-only) and adapt. Never add columns.

# Identity model

- Telegram users: `chat_id` = numeric Telegram id, `username` may be set.
- Email-only users: `chat_id = ext_<crc32(email)>`, `username = NULL`. Their **real address lives in
  `contact_email`**; `users.email` is the synthetic x-ui id (`user_ext_…@nekovo.ru`) — never mail to it.
- Tiers: `demo` = freemium (10 GB/мес, no expiry date, monthly job renews), `paid` = 100 GB until
  `subscription_expiry`. Protocol gate: plain Hy2 + Reality = all tiers; **Hy2 Turbo (hy2t, Brutal) = paid only**.

# Create an email-only user

Preferred: tell the admin to send `/addmail user@example.com [ГБ] [дней]` in Telegram — it provisions
AND emails the key. If the admin explicitly wants *you* to do it, use the bot's own provisioning code
(idempotent by `contact_email`; re-run re-uses the same UUID so installed keys keep working):

```sh
$DC exec -T vpn-bot python3 - <<'PY'
from bot.config import Settings
from bot.core.database import Database
from bot.handlers.admin import AdminHandler

EMAIL, GB, DAYS, STATUS = 'user@example.com', 10, 30, 'demo'   # paid: GB=100, STATUS='paid'

class StubBot:  # no .services -> handler builds XUIService itself
    def send_message(self, **kw): pass

cfg = Settings(); db = Database(cfg.DB_PATH)
admin = AdminHandler(StubBot(), db, cfg)
url = admin._provision_email_user(EMAIL, GB, DAYS, status=STATUS)
print('sub_url:', url)
PY
```

Then send the key to the real address:

```sh
$DC exec -T vpn-bot python3 - <<'PY'
from bot.config import Settings
from bot.services.email_service import EmailService
ok = EmailService(Settings()).send_key('user@example.com', '<sub_url from previous step>', lang='ru')
print('sent:', ok)
PY
```

Telegram users are NOT created by hand at all — they self-register via `/start` + admin approval.

# Find a user

`/find <текст>` in Telegram searches chat_id/username/email/uuid/**contact_email**. From the shell:

```sh
$DC exec -T vpn-bot python3 -c "
import sqlite3
c = sqlite3.connect('/var/lib/vpn-bot/bot.db')
q = '%SEARCH%'
for r in c.execute('''SELECT chat_id, username, status, quota_gb, subscription_expiry, contact_email
  FROM users WHERE chat_id LIKE ? OR username LIKE ? OR email LIKE ? OR contact_email LIKE ?''',
  (q,q,q,q)): print(r)
"
```

# Grant / extend paid access

Use `grant_paid_access` — it flips status, floors the quota at 100 GB (never lowers a hand-raised one),
sets `subscription_expiry`, and syncs expiry/enable/quota to the x-ui panel on exit. Same semantics as a
Stars payment or `/approve_payment`:

```sh
$DC exec -T vpn-bot python3 - <<'PY'
from datetime import datetime, timedelta
from bot.config import Settings
from bot.core.database import Database
from bot.services.billing import grant_paid_access
from bot.services.xui_service import XUIService

CHAT_ID, DAYS = '<chat_id>', 30
cfg = Settings(); db = Database(cfg.DB_PATH)
res = grant_paid_access(db, cfg, XUIService(cfg), CHAT_ID,
                        datetime.now() + timedelta(days=DAYS))
print(res['status_ok'], 'panel_ok:', res['panel_ok'])
PY
```

If `panel_ok` is `False`, say so — the panel client on exit needs a manual look.

# Must NOT
- INSERT/ALTER on `users` — ever (see the hard rule).
- Mail anything to `users.email` (synthetic) — real address is `contact_email`.
- Set `status='active'` — valid statuses: new, pending_demo, demo, paid, rejected, banned.
- Delete users to "clean up" without an explicit OK; deactivation flows exist (`/reject`, `/ban`).
