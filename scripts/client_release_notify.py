#!/usr/bin/env python3
"""Announce a published NekoVPN client release to its testers, with «Что нового».

Runs inside the vpn-bot container (BOT_TOKEN, TG_PROXY_URLS and the DB):

    docker exec -w /app vpn-bot python3 scripts/client_release_notify.py v0.8.99-neko.2
        dry run: prints the recipients and the message, sends nothing
    ... --send                    deliver
    ... --to 6186958828,99820428  recipients instead of app_settings.client_testers
    ... --force                   send again to those who already got this tag
    docker exec -w /app vpn-bot python3 scripts/client_release_notify.py \\
        --set-testers 6186958828,99820428,361920703   store the default recipients

«Что нового» = the items of the GitHub release body between the markers the
app's update dialog reads (<!-- flclash:changelog:begin/end -->). The client
repo writes them from its CHANGELOG-NEKO.md (tool/neko/release_notes.dart),
so the dialog, the release page and this message say the same thing.

Only a PUBLISHED release: a draft is invisible to the API without a token,
and its download links would 404 for the testers. Each delivered message is
audited as admin_actions('owner', 'client_release_notify', <chat_id>, <tag>),
and that row also keeps a second run from messaging the same tester twice.
"""

import argparse
import html
import json
import sys
import time

REPO = 'Annoyt/vpnautodeploy_client'
API = 'https://api.github.com/repos/' + REPO + '/releases/tags/{tag}'
SETTING_KEY = 'client_testers'
ACTION = 'client_release_notify'
NOTES_BEGIN = '<!-- flclash:changelog:begin -->'
NOTES_END = '<!-- flclash:changelog:end -->'
ACTIVE_STATUSES = ('demo', 'paid', 'support_topic')
SEND_GAP_S = 0.05


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


def build_message(release):
    """(text, keyboard) of the announcement, HTML. Raises ValueError when
    the release has no «Что нового» or no arm64 APK: an announcement
    without either is worse than none."""
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


def plan(db, ids, tag, force=False):
    """[(chat_id, verdict)]: 'send', 'sent before', 'no such user',
    'inactive (<status>)'."""
    out = []
    for cid in ids:
        user = db._users.get_by_id(cid)
        if user is None:
            out.append((cid, 'no such user'))
        elif (user.status or '') not in ACTIVE_STATUSES:
            out.append((cid, f'inactive ({user.status})'))
        elif not force and already_notified(db, cid, tag):
            out.append((cid, 'sent before'))
        else:
            out.append((cid, 'send'))
    return out


def deliver(tg, db, ids, text, keyboard, tag, sleep=time.sleep):
    """Send to each id; audit the delivered ones. [(chat_id, message_id)]."""
    results = []
    for cid in ids:
        sent = tg.send_message(chat_id=cid, text=text, parse_mode='HTML',
                               reply_markup=keyboard,
                               disable_web_page_preview=True)
        mid = sent.get('message_id') if sent else None
        if mid:
            db.log_admin_action('owner', ACTION, cid, tag)
        results.append((cid, mid))
        sleep(SEND_GAP_S)
    return results


def main(argv=None, *, db=None, tg=None, fetch=fetch_release, out=sys.stdout):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('tag', nargs='?', help='release tag, e.g. v0.8.99-neko.2')
    ap.add_argument('--send', action='store_true', help='deliver (default: dry run)')
    ap.add_argument('--to', help='comma-separated chat_ids instead of the stored testers')
    ap.add_argument('--force', action='store_true', help='also those who already got this tag')
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
        text, keyboard = build_message(release)
    except ValueError as e:
        print(f'ERROR: {e}', file=out)
        return 1

    ids = parse_ids(args.to) if args.to else load_testers(db)
    if not ids:
        print('ERROR: no recipients: pass --to or store them with --set-testers', file=out)
        return 1
    verdicts = plan(db, ids, args.tag, force=args.force)
    for cid, verdict in verdicts:
        print(f'{cid}: {verdict}', file=out)
    print('---', file=out)
    print(text, file=out)
    targets = [cid for cid, verdict in verdicts if verdict == 'send']
    if not args.send:
        print(f'--- dry run: {len(targets)} would get it; add --send', file=out)
        return 0
    if tg is None:
        from bot.config import Settings
        from bot.core.telegram_client import TelegramClient
        tg = TelegramClient(Settings().BOT_TOKEN)
    results = deliver(tg, db, targets, text, keyboard, args.tag)
    failed = [cid for cid, mid in results if not mid]
    for cid, mid in results:
        print(f'{cid}: {"SENT " + str(mid) if mid else "FAILED"}', file=out)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
