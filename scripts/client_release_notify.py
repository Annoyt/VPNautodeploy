#!/usr/bin/env python3
"""Announce a published NekoVPN client release to its testers, with «Что нового».

Runs inside the vpn-bot container (BOT_TOKEN, TG_PROXY_URLS and the DB):

    docker exec -w /app vpn-bot python3 scripts/client_release_notify.py v0.8.99-neko.2
        dry run: prints the recipients and the message, sends nothing
    ... --send                    deliver
    ... --to 6186958828,99820428  recipients instead of app_settings.client_testers
    ... --force                   send again to those who already got this tag
    ... --key-holders             everyone with a working key instead of the testers
    docker exec -w /app vpn-bot python3 scripts/client_release_notify.py \\
        --set-testers 6186958828,99820428,361920703   store the default recipients

«Что нового» = the items of the GitHub release body between the markers the
app's update dialog reads (<!-- flclash:changelog:begin/end -->). The client
repo writes them from its CHANGELOG-NEKO.md (tool/neko/release_notes.dart),
so the dialog, the release page and this message say the same thing.

--key-holders (the owner, 2026-11-21: «рассылай всем»): demo, paid and
support_topic users with a uuid. Each message carries the user's own
«📲 Открыть в NekoVPN» (/open/<token>, the subscription in one tap) and
/app/<token> (every app, the subscription in each). Left out: a payer whose
paid-until date has passed (the panel keeps the key off until they pay),
an iPhone user (no NekoVPN there) and an ext_* user (no Telegram chat).
The dry run asks the bot's own /open for the first user's link, so a
token the bot would not know shows before anyone gets it.

Only a PUBLISHED release: a draft is invisible to the API without a token,
and its download links would 404 for the testers. Each delivered message is
audited as admin_actions('owner', 'client_release_notify', <chat_id>, <tag>),
and that row also keeps a second run from messaging the same tester twice.
"""

import argparse
import html
import json
import os
import sys
import time
from datetime import datetime, timezone

REPO = 'Annoyt/vpnautodeploy_client'
API = 'https://api.github.com/repos/' + REPO + '/releases/tags/{tag}'
SETTING_KEY = 'client_testers'
ACTION = 'client_release_notify'
NOTES_BEGIN = '<!-- flclash:changelog:begin -->'
NOTES_END = '<!-- flclash:changelog:end -->'
ACTIVE_STATUSES = ('demo', 'paid', 'support_topic')
SEND_GAP_S = 0.05
# The bot's own listener inside its container, for the dry run's link check.
LOCAL_BASE = os.environ.get('NEKO_LOCAL_BASE', 'http://127.0.0.1:8080')
SEND = 'send'


def whats_new(body):
    """The «Что нового» items: the "- " lines between the markers. A body
    without the begin marker has none (never the whole body: outside the
    markers sit install hints, not changes)."""
    if not body:
        return []
    begin = body.find(NOTES_BEGIN)
    if begin < 0:
        return []
    end = body.find(NOTES_END, begin)
    scope = body[begin + len(NOTES_BEGIN):end if end >= 0 else len(body)]
    items = []
    for line in scope.splitlines():
        line = line.strip()
        if line.startswith('- '):
            items.append(line[2:].strip())
    return items


def apk_url(release, abi='arm64-v8a'):
    """Download link of the APK for this ABI, or None."""
    for asset in release.get('assets') or []:
        if (asset.get('name') or '').endswith(f'-android-{abi}.apk'):
            return asset.get('browser_download_url')
    return None


def build_message(release, everyone=False):
    """(text, keyboard) of the announcement, HTML. Raises ValueError when
    the release has no «Что нового» or no arm64 APK: an announcement
    without either is worse than none. ``everyone``: the text also tells
    a newcomer how the subscription gets in (the personal button)."""
    tag = release.get('tag_name') or ''
    items = whats_new(release.get('body'))
    if not items:
        raise ValueError(f'{tag}: the release body has no «Что нового» items')
    apk = apk_url(release)
    if not apk:
        raise ValueError(f'{tag}: no arm64-v8a APK among the release assets')
    version = tag[1:] if tag.startswith('v') else tag
    lines = [
        f'🐱 <b>NekoVPN {html.escape(version)} для Android</b>',
        '',
        '<b>Что нового</b>',
        *(f'• {html.escape(item)}' for item in items),
        '',
        'Как обновить: скачайте APK кнопкой ниже и установите поверх прежней '
        'версии, профили и настройки сохранятся.',
    ]
    if everyone:
        lines += [
            '',
            'Ещё нет NekoVPN? Установите APK и нажмите «📲 Открыть в NekoVPN»: '
            'приложение добавит вашу подписку. Для компьютера и других '
            'приложений — кнопка «Другие устройства».',
        ]
    keyboard = {'inline_keyboard': [
        [{'text': '⬇️ Скачать NekoVPN (Android)', 'url': apk}],
        [{'text': 'Другие версии и контрольные суммы',
          'url': release.get('html_url') or f'https://github.com/{REPO}/releases'}],
    ]}
    return '\n'.join(lines), keyboard


def fetch_release(tag, get=None):
    """The PUBLISHED release with this tag, from the GitHub API."""
    if get is None:
        import requests
        get = requests.get
    resp = get(API.format(tag=tag), timeout=20,
               headers={'Accept': 'application/vnd.github+json'})
    if resp.status_code == 404:
        raise ValueError(f'{tag}: no published release (a draft is not visible)')
    resp.raise_for_status()
    release = resp.json()
    if release.get('draft'):
        raise ValueError(f'{tag}: still a draft')
    return release


def parse_ids(raw):
    """'1, 2,2' -> ['1', '2']: trimmed, de-duplicated, order kept."""
    out = []
    for part in (raw or '').split(','):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def load_testers(db):
    raw = db.get_setting(SETTING_KEY)
    try:
        ids = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(i) for i in ids if str(i).strip()] if isinstance(ids, list) else []


def already_notified(db, chat_id, tag):
    with db._connect() as conn:
        row = conn.execute(
            'SELECT 1 FROM admin_actions WHERE action = ? AND target_id = ? '
            'AND details = ? LIMIT 1', (ACTION, str(chat_id), tag),
        ).fetchone()
    return row is not None


def key_holders(db):
    """chat_ids of everyone with a key (a uuid and a key status), in the
    order they joined."""
    marks = ', '.join('?' * len(ACTIVE_STATUSES))
    with db._connect() as conn:
        rows = conn.execute(
            f"SELECT chat_id FROM users WHERE status IN ({marks}) "
            "AND uuid IS NOT NULL AND TRIM(uuid) != '' ORDER BY rowid",
            ACTIVE_STATUSES,
        ).fetchall()
    return [str(r[0]).strip() for r in rows if r[0] is not None and str(r[0]).strip()]


def paid_until(user):
    """The paid-until date as naive UTC, or None when there is none or it
    cannot be read."""
    raw = getattr(user, 'subscription_expiry', None)
    if not raw:
        return None
    try:
        until = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if until.tzinfo is not None:
        until = until.astimezone(timezone.utc).replace(tzinfo=None)
    return until


def lapsed(user, now):
    """A payer past the paid-until date: the panel keeps the key off until
    they pay again (the monthly reset skips them too)."""
    if (user.status or '') != 'paid':
        return False
    until = paid_until(user)
    return until is not None and until < now


def personal_rows(config, user):
    """The user's own buttons, [] when the bot cannot build them:
    «📲 Открыть в NekoVPN» (/open/<token>) and the app page (/app/<token>)."""
    from bot.services import neko_client
    open_url = neko_client.open_page_url(config, user)
    page_url = neko_client.download_page_url(config, user)
    if not open_url or not page_url or '/app/' not in page_url:
        return []
    lang = getattr(user, 'lang', None)
    other = 'Other devices' if lang == 'en' else 'Другие устройства'
    return [
        [{'text': neko_client.open_button_label(lang), 'url': open_url}],
        [{'text': f'💻 {other}', 'url': page_url}],
    ]


def personal_keyboard(keyboard, rows):
    """The download button, then the user's own rows."""
    return {'inline_keyboard': [keyboard['inline_keyboard'][0], *rows]}


def plan(db, ids, tag, force=False, everyone=False, config=None, now=None):
    """[(chat_id, verdict, rows)]: verdict 'send', 'sent before', 'no such
    user', 'inactive (<status>)'; with ``everyone`` also 'not a Telegram
    chat', 'lapsed (paid until <date>)', 'iPhone' and 'no subscription
    link'. ``rows`` are the user's own buttons (``everyone`` only)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    out = []
    for cid in ids:
        user = db._users.get_by_id(cid)
        rows = []
        if user is None:
            verdict = 'no such user'
        elif (user.status or '') not in ACTIVE_STATUSES:
            verdict = f'inactive ({user.status})'
        elif everyone and not cid.lstrip('-').isdigit():
            verdict = 'not a Telegram chat'
        elif everyone and lapsed(user, now):
            verdict = f'lapsed (paid until {paid_until(user):%Y-%m-%d})'
        elif everyone and (getattr(user, 'platform', None) or '') == 'ios':
            verdict = 'iPhone'
        elif not force and already_notified(db, cid, tag):
            verdict = 'sent before'
        else:
            verdict = SEND
            if everyone:
                rows = personal_rows(config, user)
                if not rows:
                    verdict = 'no subscription link'
        out.append((cid, verdict, rows))
    return out


def deliver(tg, db, ids, text, keyboard, tag, sleep=time.sleep, rows=None):
    """Send to each id; audit the delivered ones. [(chat_id, message_id)].
    ``rows``: chat_id → the user's own buttons, under the download one."""
    results = []
    for cid in ids:
        own = (rows or {}).get(cid)
        markup = personal_keyboard(keyboard, own) if own else keyboard
        try:
            sent = tg.send_message(chat_id=cid, text=text, parse_mode='HTML',
                                   reply_markup=markup,
                                   disable_web_page_preview=True)
        except Exception as e:
            print(f'{cid}: {type(e).__name__}', file=sys.stderr)
            sent = None
        mid = sent.get('message_id') if sent else None
        if mid:
            db.log_admin_action('owner', ACTION, cid, tag)
        results.append((cid, mid))
        sleep(SEND_GAP_S)
    return results


def link_check(open_url, base, local=LOCAL_BASE, get=None):
    """What the bot's own listener answers for the path of ``open_url``: a
    status code, or why there is none. A token the bot does not know is a
    404 here, before anyone gets the button. The token is never printed."""
    if not base or not open_url.startswith(base + '/'):
        return 'skipped (no public address)'
    if get is None:
        import requests
        get = requests.get
    try:
        resp = get(local.rstrip('/') + open_url[len(base):], timeout=10,
                   allow_redirects=False)
    except Exception as e:
        return f'error ({type(e).__name__})'
    return str(resp.status_code)


def main(argv=None, *, db=None, tg=None, fetch=fetch_release, out=sys.stdout,
         config=None, check=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('tag', nargs='?', help='release tag, e.g. v0.8.99-neko.2')
    ap.add_argument('--send', action='store_true', help='deliver (default: dry run)')
    ap.add_argument('--to', help='comma-separated chat_ids instead of the stored testers')
    ap.add_argument('--force', action='store_true', help='also those who already got this tag')
    ap.add_argument('--key-holders', action='store_true',
                    help='everyone with a working key, with their own subscription buttons')
    ap.add_argument('--set-testers', help='store comma-separated chat_ids as the testers')
    args = ap.parse_args(argv)

    if db is None:
        import warnings
        warnings.simplefilter('ignore')
        from bot.config import Settings
        from bot.core.database import Database
        db = Database(Settings().DB_PATH)

    if args.set_testers is not None:
        ids = parse_ids(args.set_testers)
        db.set_setting(SETTING_KEY, json.dumps(ids))
        print(f'testers: {", ".join(ids) or "(none)"}', file=out)
        if not args.tag:
            return 0
    if not args.tag:
        ap.error('a release tag is required')

    try:
        release = fetch(args.tag)
        text, keyboard = build_message(release, everyone=args.key_holders)
    except ValueError as e:
        print(f'ERROR: {e}', file=out)
        return 1

    everyone = args.key_holders
    if everyone and args.to:
        print('ERROR: --to and --key-holders exclude each other', file=out)
        return 1
    if everyone:
        ids = key_holders(db)
        if config is None:
            from bot.config import Settings
            config = Settings()
    else:
        ids = parse_ids(args.to) if args.to else load_testers(db)
    if not ids:
        print('ERROR: no recipients: pass --to or store them with --set-testers', file=out)
        return 1
    verdicts = plan(db, ids, args.tag, force=args.force, everyone=everyone,
                    config=config)
    for cid, verdict, _ in verdicts:
        print(f'{cid}: {verdict}', file=out)
    targets = [cid for cid, verdict, _ in verdicts if verdict == SEND]
    rows = {cid: own for cid, verdict, own in verdicts if verdict == SEND and own}
    if everyone:
        counts = {}
        for _, verdict, _ in verdicts:
            kind = verdict.split(' (')[0]
            counts[kind] = counts.get(kind, 0) + 1
        print('--- ' + ', '.join(f'{k}: {n}' for k, n in counts.items()), file=out)
    print('---', file=out)
    print(text, file=out)
    if not args.send:
        if everyone and targets:
            from bot.utils.public_url import public_base_url
            first = rows[targets[0]]
            labels = [b['text'] for row in personal_keyboard(keyboard, first)['inline_keyboard']
                      for b in row]
            print('--- buttons: ' + ' | '.join(labels), file=out)
            status = link_check(first[0][0]['url'], public_base_url(config), get=check)
            print(f'--- link check, the first one\'s /open: {status}', file=out)
        print(f'--- dry run: {len(targets)} would get it; add --send', file=out)
        return 0
    if tg is None:
        from bot.config import Settings
        from bot.core.telegram_client import TelegramClient
        tg = TelegramClient(Settings().BOT_TOKEN)
    results = deliver(tg, db, targets, text, keyboard, args.tag, rows=rows)
    failed = [cid for cid, mid in results if not mid]
    for cid, mid in results:
        print(f'{cid}: {"SENT " + str(mid) if mid else "FAILED"}', file=out)
    return 1 if failed else 0


if __name__ == '__main__':
    # Run as a file, its own directory leads sys.path; the bot package sits
    # one level up (/app in the container).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
