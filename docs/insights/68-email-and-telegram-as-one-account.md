# §68. Email and Telegram as one account: a code mailed to the address (2026-11-24)

**Why.** On 2026-11-24 Tatiana, who had a paid key by mail since 08-30 (an `ext_*` row with her address as `contact_email`), came to Telegram from the NekoVPN app. The bot saw a new user and started her on a demo. The owner: «надо как-то объединять майл и аккаунт тг». He chose a code mailed to the address, for any mail provider. A verified Google login was set aside: it needs Google Play services, which the client fork strips, or accounts.google.com, which whitelisted mobile networks may not open.

## The flow

- **Entry points.**
  - «📧 Я регистрировался через почту» on the welcome screen (`email_login`); the address is required.
  - «📧 Добавить почту» (`add_email_prompt`) goes through the same code now: the bot answers SOS letters from that address, so it should be the user's.
  - `/setemail` still saves without a code; admins use it in forum topics.
- **The code** (`bot/services/email_link.py`):
  - six digits, mailed through the Gmail relay (`EmailService.send_notice`);
  - 15 minutes, five tries, three letters an hour per user; a letter the relay refused does not count;
  - only an HMAC of the code is stored (key: the pinned subscription secret), in `email_codes`, which backups leave out (`BOT_DB_LEFT_OUT`);
  - state between messages: `PENDING_EMAIL` (the address), `PENDING_EMAIL_CODE` (the code), `EMAIL_PURPOSE` in `bot/handlers/messages.py`; «🔁 Отправить ещё раз» and «✖️ Отмена» under the letter (`EmailCodeFlowHandler`).
- **A right code links** (`email_link.link`, one transaction):
  - nobody else holds the address: it becomes this user's `contact_email`;
  - another row holds only the address: it moves here;
  - one other row holds it with a key, and this user has no key, or a demo key against the other's paid one: the key, the payment, traffic and geo fields, the address and the `subscriptions` row move here. The other row stays empty for history (`status = 'new'`, `previous_state` = what it was) and gets an `email_link_merge` audit row. A replaced demo client leaves the panel. The subscription link is a hash of the key, so the person's apps keep working;
  - anything else (keys on both that both count, several holders): nothing changes; the user is told the admin will join them, and the admin gets «⚠️ Свяжите вручную» in TOPIC_REQUESTS.
- **A merge** tells the admin «🔗 Почта и Telegram объединены» and gives the user «🔑 Мой ключ».

## Tatiana by hand, before the code

The same moves, one transaction on prod, 2026-11-24; the owner approved in chat. Backup of both rows at `/root/nekovpn-merge-20261124/before.json` on entry. Her links answered 200 after it, and her panel client stayed enabled until 2028-03-30.

## What is next

The app side: «Я регистрировался через почту» in the «Нет подписки» window. The phone's mail accounts (Android's account chooser: Google, Yandex, mail.ru) or a typed address, the same code, and the app adds the subscription itself, without Telegram. It needs two API calls on the bot and a client release.

Tests: `tests/integration/test_email_link.py` (real sqlite: codes, every link rule, the flow from the button to the merge), `tests/unit/test_pending_email.py`; 12 mutants killed.
