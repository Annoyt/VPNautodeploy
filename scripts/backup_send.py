#!/usr/bin/env python3
"""The bot's side of the daily service backup (AGENTS.md §58). Runs inside the
vpn-bot container (BOT_TOKEN, TG_PROXY_URLS, FORUM_GROUP_ID and bot.db are
there); scripts/service_backup.py calls it from entry:

    docker exec -i vpn-bot python3 /app/scripts/backup_send.py send \\
        --name NAME --sha256 HEX --meta-json JSON  < archive.tar.zst.age
        upload into the «Бэкапы» topic (made on the first send), record the run
    ... fail --reason TEXT     record a failed run (the alert shows its reason)
    ... installed              the timer is installed: the alert starts watching
    ... forget                 the timer is removed: the alert stops
    ... status                 what the alert sees, and the topic
    ... topic [--create | --set THREAD_ID]

`send` and `fail` print one JSON line ({"ok": …}). Nothing here prints the
archive, a token or a secret: the archive is only ever an age file (send
refuses anything else), and the meta carries statuses and counts.
"""

import argparse
import json
import os
import sys


def _settings():
    import warnings
    warnings.simplefilter('ignore')
    from bot.config import Settings
    return Settings()


def main(argv=None, *, db_path=None, client=None, chat_id=None, stdin=None,
         out=sys.stdout, now=None):
    from bot.services import backups

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    p_send = sub.add_parser('send', help='upload the archive read from stdin')
    p_send.add_argument('--name', required=True)
    p_send.add_argument('--sha256', required=True)
    p_send.add_argument('--meta-json', default='{}')
    p_fail = sub.add_parser('fail', help='record a failed run')
    p_fail.add_argument('--reason', required=True)
    sub.add_parser('installed', help='the timer is installed')
    sub.add_parser('forget', help='the timer is removed')
    sub.add_parser('status', help='what the alert sees')
    p_topic = sub.add_parser('topic', help='show, create or pin the «Бэкапы» topic')
    group = p_topic.add_mutually_exclusive_group()
    group.add_argument('--create', action='store_true')
    group.add_argument('--set', type=int, metavar='THREAD_ID')
    args = ap.parse_args(argv)

    settings = None
    if db_path is None or (chat_id is None and args.cmd in ('send', 'topic', 'status')):
        settings = _settings()
    db_path = db_path or settings.DB_PATH
    if chat_id is None and settings is not None:
        chat_id = settings.FORUM_GROUP_ID

    def get_client():
        if client is not None:
            return client
        from bot.core.telegram_client import TelegramClient
        return TelegramClient((settings or _settings()).BOT_TOKEN)

    def emit(obj):
        print(json.dumps(obj, ensure_ascii=False), file=out, flush=True)

    if args.cmd == 'send':
        try:
            meta = json.loads(args.meta_json or '{}')
        except ValueError:
            meta = {}
        data = (stdin if stdin is not None else sys.stdin.buffer).read()
        try:
            result = backups.send_backup(get_client(), db_path, chat_id, data, args.name,
                                         args.sha256, meta, now=now)
        except backups.BackupError as e:
            emit({'ok': False, 'error': str(e)})
            return 1
        emit(result)
        return 0

    if args.cmd == 'fail':
        backups.record_failure(db_path, args.reason, now=now)
        emit({'ok': True})
        return 0

    if args.cmd == 'installed':
        backups.mark_installed(db_path, now=now)
        print('backup: the alert watches from now on', file=out)
        return 0

    if args.cmd == 'forget':
        backups.forget(db_path)
        print('backup: state cleared, the alert is off', file=out)
        return 0

    if args.cmd == 'topic':
        if args.set:
            backups.set_topic(db_path, chat_id, args.set, now=now)
        elif args.create:
            try:
                backups.resolve_topic(get_client(), db_path, chat_id, now=now)
            except backups.BackupError as e:
                # Telegram's own answer is in the client's log line on stderr.
                print(f'ERROR: {e}', file=out)
                return 1
        thread_id = backups.stored_topic(db_path, chat_id)
        print(f'topic «{backups.TOPIC_NAME}» in {chat_id}: '
              f'{thread_id if thread_id else "not created yet (it is made on the first send)"}',
              file=out)
        return 0

    # status
    state = backups.load_state(db_path)
    thread_id = backups.stored_topic(db_path, chat_id)
    print(f'topic: {thread_id or "—"} in {chat_id}', file=out)
    if not state:
        print('state: none (the timer is not installed, or never reported)', file=out)
        return 0
    print(f'installed: {backups.fmt_ts(state.get("installed"))}', file=out)
    for key in ('last_ok', 'last_sent'):
        entry = state.get(key) or {}
        if entry:
            missing = entry.get('missing') or {}
            print(f'{key}: {backups.fmt_ts(entry.get("ts"))} {entry.get("name")} '
                  f'{backups.human(entry.get("size"))}'
                  + (f' — without {", ".join(missing)}' if missing else ''), file=out)
    run = state.get('last_run') or {}
    if run:
        print(f'last_run: {backups.fmt_ts(run.get("ts"))} {run.get("status")}'
              + (f' — {run["error"]}' if run.get('error') else ''), file=out)
    verdict = backups.alert_verdict(state, now or backups.utcnow())
    print(f'alert: {verdict["title"] if verdict else "none"}', file=out)
    return 0


if __name__ == '__main__':
    # Run as a file, its own directory leads sys.path; the bot package sits
    # one level up (/app in the container).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(main())
