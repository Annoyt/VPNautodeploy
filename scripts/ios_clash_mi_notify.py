#!/usr/bin/env python3
"""Tell iPhone key holders to move to Clash Mi, each with their own subscription.

Runs inside the vpn-bot container (BOT_TOKEN, TG_PROXY_URLS and the DB):

    docker exec -w /app vpn-bot python3 scripts/ios_clash_mi_notify.py
        dry run: the recipients with verdicts and the first message, no token
    ... --send        deliver
    ... --force       also those who already got it
    ... --to ID,ID    these chat_ids instead of every iPhone key holder

The owner, 2026-11-25 (AGENTS.md §69): iPhone gets Clash Mi and nothing
else, and «отправишь ссылку на клиент тем кто скачивал для апл ссылку на клш
в стор и подписку новую». Recipients: key holders (demo, paid, support_topic
with a uuid) whose platform is iPhone. Left out: an ext_* user (no Telegram
chat) and a payer past the paid-until date (the key stays off until they
pay). ``--to`` skips the platform check: a test send to a non-iPhone chat.

Each message carries the user's own Clash link, the one Clash Mi reads, in
the bot's own iPhone steps (``clash_mi_text``, the key card's), with
«⬇️ Clash Mi в App Store» and «📲 Добавить в Clash Mi» (``/app/<token>``,
whose iPhone section has Clash Mi's import link).

The dry run asks the bot's own listener for the first user's /app page and
checks that it offers Clash Mi: a bot without §69 still offers Karing there.
Each delivered message is audited as
admin_actions('owner', 'ios_clash_mi_notify', <chat_id>, TAG); a second run
skips those.
"""

import argparse
import importlib.util
import os
import sys
import time
from datetime import datetime, timezone

ACTION = 'ios_clash_mi_notify'
TAG = 'clash-mi-2026-11'
SEND = 'send'
SEND_GAP_S = 0.05
LOCAL_BASE = os.environ.get('NEKO_LOCAL_BASE', 'http://127.0.0.1:8080')


def _release_notify():
    """scripts/client_release_notify.py, loaded from this file's directory:
    its key_holders / lapsed / paid_until / parse_ids are this script's
    too."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'client_release_notify.py')
    spec = importlib.util.spec_from_file_location('client_release_notify', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


crn = _release_notify()


def already_notified(db, chat_id):
    with db._connect() as conn:
        row = conn.execute(
            'SELECT 1 FROM admin_actions WHERE action = ? AND target_id = ? '
            'AND details = ? LIMIT 1', (ACTION, str(chat_id), TAG),
        ).fetchone()
    return row is not None


def build_message(config, user):
    """(text, keyboard) for this user, or None when the bot cannot build
    the user's links (no public address)."""
    from bot.handlers.callbacks.user import clash_mi_text
    from bot.services import neko_client
    from bot.services.subscription import SubscriptionService
    url = SubscriptionService(config).build_subscription_url(user)
    page = neko_client.download_page_url(config, user)
    if not url or '/app/' not in page:
        return None
    en = getattr(user, 'lang', None) == 'en'
    if en:
        text = (
            "🍎 <b>VPN on iPhone: moving to Clash Mi</b>\n\n"
            "Hiddify and Happ are gone from the Russian App Store. For iPhone we "
            "now have one app, Clash Mi: it is on the App Store and runs the "
            "same core as our NekoVPN for Android. It takes a new link to your "
            "subscription, below.\n\n"
            + clash_mi_text(url, 'en', with_button=True)
            + "\n\nYou can delete the old app once Clash Mi connects."
        )
        store = '⬇️ Clash Mi on the App Store'
    else:
        text = (
            "🍎 <b>VPN на iPhone: переходим на Clash Mi</b>\n\n"
            "Hiddify и Happ из российского App Store уже убрали. Для iPhone у нас "
            "теперь одно приложение — Clash Mi: оно есть в App Store и "
            "работает на том же движке, что наш NekoVPN для Android. Подписка "
            "для него — новая ссылка, она ниже.\n\n"
            + clash_mi_text(url, 'ru', with_button=True)
            + "\n\nСтарое приложение можно удалить, когда Clash Mi подключится."
        )
        store = '⬇️ Clash Mi в App Store'
    keyboard = {'inline_keyboard': [
        [{'text': store, 'url': neko_client.CLASH_MI_URL}],
        [{'text': neko_client.clash_mi_button_label('en' if en else 'ru'),
          'url': page}],
    ]}
    return text, keyboard


def plan(db, ids, config, force=False, everyone=True, now=None):
    """[(chat_id, verdict, message)]: verdict 'send', 'no such user',
    'inactive (<status>)', 'not a Telegram chat', 'lapsed (paid until
    <date>)', 'not iPhone' (``everyone`` only), 'sent before' or 'no
    subscription link'; ``message`` is (text, keyboard) for 'send'."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for cid in ids:
        user = db._users.get_by_id(cid)
        message = None
        if user is None:
            verdict = 'no such user'
        elif (user.status or '') not in crn.ACTIVE_STATUSES:
            verdict = f'inactive ({user.status})'
        elif not getattr(user, 'uuid', None):
            verdict = 'no key'
        elif not cid.lstrip('-').isdigit():
            verdict = 'not a Telegram chat'
        elif crn.lapsed(user, now):
            verdict = f'lapsed (paid until {crn.paid_until(user):%Y-%m-%d})'
        elif everyone and (getattr(user, 'platform', None) or '') != 'ios':
            verdict = 'not iPhone'
        elif not force and already_notified(db, cid):
            verdict = 'sent before'
        else:
            message = build_message(config, user)
            verdict = SEND if message else 'no subscription link'
        out.append((cid, verdict, message))
    return out


def deliver(tg, db, planned, sleep=time.sleep):
    """Send each planned message; audit the delivered ones.
    [(chat_id, message_id)]."""
    results = []
    for cid, verdict, message in planned:
        if verdict != SEND:
            continue
        text, keyboard = message
        try:
            sent = tg.send_message(chat_id=cid, text=text, parse_mode='HTML',
                                   reply_markup=keyboard,
                                   disable_web_page_preview=True)
        except Exception as e:
            print(f'{cid}: {type(e).__name__}', file=sys.stderr)
            sent = None
        mid = sent.get('message_id') if sent else None
        if mid:
            db.log_admin_action('owner', ACTION, cid, TAG)
        results.append((cid, mid))
        sleep(SEND_GAP_S)
    return results


def page_check(page_url, base, local=LOCAL_BASE, get=None):
    """What the bot's own listener serves at the path of ``page_url``: the
    status and whether the page offers Clash Mi. The token is never
    printed."""
    if not base or not page_url.startswith(base + '/'):
        return 'skipped (no public address)'
    if get is None:
        import requests
        get = requests.get
    try:
        resp = get(local.rstrip('/') + page_url[len(base):], timeout=10,
                   allow_redirects=False,
                   headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0)'})
    except Exception as e:
        return f'error ({type(e).__name__})'
    body = getattr(resp, 'text', '') or ''
    if resp.status_code != 200:
        return str(resp.status_code)
    if 'clashmi://' in body:
        return '200, Clash Mi'
    return '200, but no Clash Mi: the bot runs an older page'


def masked(text, token):
    return text.replace(token, '<token>') if token else text


def main(argv=None, *, db=None, tg=None, out=sys.stdout, config=None,
         check=None, now=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--send', action='store_true', help='deliver (default: dry run)')
    ap.add_argument('--force', action='store_true', help='also those who already got it')
    ap.add_argument('--to', help='comma-separated chat_ids instead of every iPhone key holder')
    args = ap.parse_args(argv)

    if db is None:
        import warnings
        warnings.simplefilter('ignore')
        from bot.config import Settings
        from bot.core.database import Database
        db = Database(Settings().DB_PATH)
    if config is None:
        from bot.config import Settings
        config = Settings()

    everyone = not args.to
    ids = crn.parse_ids(args.to) if args.to else crn.key_holders(db)
    if not ids:
        print('ERROR: no recipients', file=out)
        return 1
    planned = plan(db, ids, config, force=args.force, everyone=everyone, now=now)
    counts = {}
    for cid, verdict, _ in planned:
        kind = verdict.split(' (')[0]
        counts[kind] = counts.get(kind, 0) + 1
        if not everyone or verdict != 'not iPhone':
            print(f'{cid}: {verdict}', file=out)
    print('--- ' + ', '.join(f'{k}: {n}' for k, n in counts.items()), file=out)
    targets = [item for item in planned if item[1] == SEND]
    if not args.send:
        if targets:
            from bot.services.subscription import SubscriptionService
            from bot.utils.public_url import public_base_url
            cid, _, (text, keyboard) = targets[0]
            user = db._users.get_by_id(cid)
            token = SubscriptionService(config).derive_token(user.uuid)
            print(f'--- the message to {cid}:', file=out)
            print(masked(text, token), file=out)
            labels = [b['text'] for row in keyboard['inline_keyboard'] for b in row]
            print('--- buttons: ' + ' | '.join(labels), file=out)
            status = page_check(keyboard['inline_keyboard'][1][0]['url'],
                                public_base_url(config), get=check)
            print(f"--- page check, the first one's /app: {status}", file=out)
        print(f'--- dry run: {len(targets)} would get it; add --send', file=out)
        return 0
    if tg is None:
        from bot.core.telegram_client import TelegramClient
        tg = TelegramClient(config.BOT_TOKEN)
    results = deliver(tg, db, planned)
    for cid, mid in results:
        print(f'{cid}: {"SENT " + str(mid) if mid else "FAILED"}', file=out)
    return 1 if any(not mid for _, mid in results) else 0


if __name__ == '__main__':
    # Run as a file, its own directory leads sys.path; the bot package sits
    # one level up (/app in the container).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
