"""The «Бэкапы» topic and the record of the daily service backup (AGENTS.md §58).

scripts/service_backup.py (root, on entry) collects both nodes, packs the archive
and encrypts it with age; then it hands the file to scripts/backup_send.py inside
the vpn-bot container, which calls ``send_backup`` here. The upload goes through
the bot's TelegramClient and its TG_PROXY_URLS pool: entry cannot reach
api.telegram.org directly.

Two app_settings rows (JSON):
  backup_topic  {"chat_id", "thread_id", "created"}: the forum topic, created on
                the first send (createForumTopic in FORUM_GROUP_ID), created anew
                when Telegram says the thread is gone or the group changed.
  backup_state  {"installed", "last_ok", "last_sent", "last_run"}: what the
                AlertManager check ``backup:stale`` reads (``alert_verdict``).

Only an age file is ever sent: ``send_backup`` refuses anything else, so a bug
upstream cannot post a plaintext archive into the group.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional

import requests

TOPIC_KEY = 'backup_topic'
STATE_KEY = 'backup_state'
TOPIC_NAME = 'Бэкапы'
AGE_HEADER = b'age-encryption.org/v1\n'
PART_BYTES = 45 * 1024 * 1024        # the Bot API takes files up to 50 MB
MAX_ARCHIVE = 2048 * 1024 * 1024
STALE_H = 26                          # a daily timer, plus its slack
CAPTION_MAX = 1024
STATUS_LINE_MAX = 200
RUNBOOK = 'docs/runbooks/backup_restore.md'
PARTS = ('entry', 'bot', 'exit')
NAME_RE = re.compile(r'^nekovpn-backup-\d{8}T\d{6}Z\.tar\.zst\.age$')
INTRO = ('Сюда раз в сутки приходит зашифрованный бэкап всего сервиса (entry и exit): '
         'bot.db, x-ui.db, ключи, конфиги, настройки Hermes. Открыть его можно только '
         'закрытым ключом age владельца. Как расшифровать и восстановить — '
         f'{RUNBOOK} в репо (AGENTS.md §58).')
CHECK_HINT = ('Проверить: systemctl status nekovpn-backup.timer nekovpn-backup.service; '
              'journalctl -u nekovpn-backup -n 60. Запустить сейчас: '
              'systemctl start nekovpn-backup.service')


class BackupError(Exception):
    """A send that did not happen. The text is short and carries no secret."""


class ThreadGone(BackupError):
    """Telegram says the stored topic does not exist any more."""


# ------------------------------------------------------------------ time ----

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def to_iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def fmt_ts(value) -> str:
    t = parse_iso(value) if isinstance(value, str) else value
    return t.strftime('%d.%m.%Y %H:%M UTC') if t else '—'


def fmt_age(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 48:
        return f'{hours} ч'
    return f'{hours // 24} сут' + (f' {hours % 24} ч' if hours % 24 else '')


def human(n) -> str:
    if not isinstance(n, (int, float)):
        return '—'
    if n < 1024:
        return f'{int(n)} Б'
    if n < 1024 * 1024:
        return f'{n / 1024:.1f} КБ'
    return f'{n / 1024 / 1024:.1f} МБ'


def short(text, limit: int = 200) -> str:
    text = ' '.join(str(text or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


# --------------------------------------------------------------- storage ----

def _connect(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect(db_path, timeout=30, isolation_level=None)


def _read_json(conn: sqlite3.Connection, key: str) -> Optional[dict]:
    """None = no row. A damaged value reads as {}: these rows are ours, and
    starting over beats never recording again. A failed read RAISES — nothing
    is ever written over a row that could not be read."""
    row = conn.execute('SELECT value FROM app_settings WHERE key = ?', (key,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(conn: sqlite3.Connection, key: str, value: dict) -> None:
    conn.execute(
        'INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, CURRENT_TIMESTAMP) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP',
        (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))


def load(db_path: str, key: str) -> Optional[dict]:
    conn = _connect(db_path)
    try:
        return _read_json(conn, key)
    finally:
        conn.close()


def _update(db_path: str, key: str, change: Callable[[dict], None]) -> dict:
    """Read-modify-write of one row inside one IMMEDIATE transaction."""
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            value = _read_json(conn, key) or {}
            change(value)
            _write_json(conn, key, value)
            conn.execute('COMMIT')
        except BaseException:
            conn.execute('ROLLBACK')
            raise
        return value
    finally:
        conn.close()


def load_state(db_path: str) -> Optional[dict]:
    return load(db_path, STATE_KEY)


def mark_installed(db_path: str, *, now: Optional[datetime] = None) -> dict:
    """The installer's word: from now on the alert expects backups."""
    stamp = to_iso(now or utcnow())
    return _update(db_path, STATE_KEY, lambda s: s.setdefault('installed', stamp))


def forget(db_path: str) -> None:
    """The timer was removed: no state, no alert. The topic stays."""
    conn = _connect(db_path)
    try:
        conn.execute('DELETE FROM app_settings WHERE key = ?', (STATE_KEY,))
    finally:
        conn.close()


def record_failure(db_path: str, reason: str, *, now: Optional[datetime] = None) -> dict:
    run = {'ts': to_iso(now or utcnow()), 'status': 'failed', 'error': short(reason, 300)}
    return _update(db_path, STATE_KEY, lambda s: s.update(last_run=run))


def missing_parts(meta: dict) -> dict:
    """{part: why} for every part that is not whole."""
    parts = meta.get('parts') if isinstance(meta, dict) else None
    parts = parts if isinstance(parts, dict) else {}
    out = {}
    for name in PARTS:
        part = parts.get(name)
        if not isinstance(part, dict):
            out[name] = 'нет в архиве'
        elif part.get('status') != 'ok':
            problems = [short(p, 120) for p in (part.get('problems') or []) if p]
            out[name] = '; '.join(problems[:3]) or str(part.get('status') or 'не ok')
    return out


def record_sent(db_path: str, meta: dict, name: str, size: int, sha256: str,
                message_ids: List[int], chat_id, thread_id: int, *,
                now: Optional[datetime] = None) -> dict:
    stamp = to_iso(now or utcnow())
    missing = missing_parts(meta)
    complete = bool(meta.get('complete')) and not missing
    entry = {'ts': stamp, 'name': name, 'size': size, 'sha256': sha256,
             'message_ids': list(message_ids), 'chat_id': str(chat_id),
             'thread_id': thread_id, 'missing': missing}

    def change(state: dict) -> None:
        state['last_sent'] = entry
        if complete:
            state['last_ok'] = entry
        state['last_run'] = {
            'ts': stamp, 'status': 'ok' if complete else 'partial',
            'error': '; '.join(f'{k}: {v}' for k, v in missing.items()) or None}
    return _update(db_path, STATE_KEY, change)


# ----------------------------------------------------------------- topic ----

def stored_topic(db_path: str, chat_id) -> Optional[int]:
    topic = load(db_path, TOPIC_KEY) or {}
    if str(topic.get('chat_id')) != str(chat_id):
        return None
    try:
        thread_id = int(topic.get('thread_id') or 0)
    except (TypeError, ValueError):
        return None
    return thread_id if thread_id > 0 else None


def set_topic(db_path: str, chat_id, thread_id: int, *, now: Optional[datetime] = None) -> None:
    value = {'chat_id': str(chat_id), 'thread_id': int(thread_id),
             'created': to_iso(now or utcnow())}

    def replace(topic: dict) -> None:
        topic.clear()
        topic.update(value)
    _update(db_path, TOPIC_KEY, replace)


def create_topic(client, db_path: str, chat_id, *, now: Optional[datetime] = None) -> int:
    thread_id = client.create_forum_topic(chat_id, TOPIC_NAME)
    if not thread_id:
        # create_forum_topic answers None both for a refusal and for no route
        # to Telegram, so the text names both.
        raise BackupError('не смог создать топик «Бэкапы»: нет связи с Telegram '
                          'или у бота нет права Manage Topics в группе')
    set_topic(db_path, chat_id, int(thread_id), now=now)
    try:
        client.send_message(chat_id, INTRO, message_thread_id=int(thread_id))
    except Exception:            # noqa: BLE001 - the intro is a courtesy
        pass
    return int(thread_id)


def resolve_topic(client, db_path: str, chat_id, *, now: Optional[datetime] = None) -> int:
    return stored_topic(db_path, chat_id) or create_topic(client, db_path, chat_id, now=now)


# ---------------------------------------------------------------- upload ----

MARK = {'ok': '✅', 'incomplete': '⚠️', 'missing': '❌'}
LABEL = {'entry': 'entry', 'bot': 'bot.db', 'exit': 'exit'}


def status_line(name: str, part) -> str:
    if not isinstance(part, dict):
        return f'❌ {LABEL[name]}: нет в архиве'
    details = []
    for db in part.get('dbs') or []:
        if not isinstance(db, dict):
            continue
        bits = [f'{db.get("name")} {human(db.get("size"))}', f'integrity {db.get("integrity")}']
        if db.get('users') is not None:
            bits.append(f'юзеров {db["users"]}')
        if db.get('clients') is not None:
            bits.append(f'клиентов {db["clients"]}')
        details.append(', '.join(bits))
    if name != 'bot' and part.get('files') is not None:
        details.append(f'файлов {part["files"]}')
    details += [short(p, 120) for p in (part.get('problems') or []) if p]
    line = f'{MARK.get(part.get("status"), "❔")} {LABEL[name]}'
    if details:
        line += ': ' + '; '.join(details)
    return short(line, STATUS_LINE_MAX)


def caption(meta: dict, name: str, size: int, sha256: str, *, part: int = 1, parts: int = 1) -> str:
    """Plain text, no parse mode: nothing in it can break entity parsing."""
    lines = [f'🗄 Бэкап NekoVPN · {fmt_ts(meta.get("created"))}']
    lines += [status_line(n, (meta.get('parts') or {}).get(n)) for n in PARTS]
    if parts > 1:
        lines.append(f'Часть {part} из {parts}: собрать — cat {name}.[0-9][0-9][0-9] > {name}')
    lines += [f'{name} · {human(size)}', f'sha256 {sha256}', f'Расшифровка: {RUNBOOK}']
    text = '\n'.join(lines)
    return text if len(text) <= CAPTION_MAX else text[:CAPTION_MAX - 1] + '…'


def split_parts(data: bytes, limit: Optional[int] = None) -> List[bytes]:
    limit = limit or PART_BYTES
    return [data[i:i + limit] for i in range(0, len(data), limit)] or [data]


def part_names(name: str, count: int) -> List[str]:
    return [name] if count == 1 else [f'{name}.{i:03d}' for i in range(1, count + 1)]


def _upload(client, chat_id, thread_id: int, chunks: List[bytes], name: str, sha256: str,
            meta: dict, size: int) -> List[int]:
    message_ids = []
    names = part_names(name, len(chunks))
    for i, (chunk, filename) in enumerate(zip(chunks, names), 1):
        text = caption(meta, name, size, sha256, part=i, parts=len(chunks))
        try:
            sent = client.send_document_bytes(chat_id, filename, chunk, caption=text,
                                              message_thread_id=thread_id, raise_errors=True)
        except requests.RequestException as e:
            desc = short(getattr(e, 'tg_description', '') or '', 200)
            low = desc.lower()
            if 'thread not found' in low or 'topic_deleted' in low or 'topic_id_invalid' in low:
                raise ThreadGone(desc)
            if 'topic_closed' in low:
                raise BackupError('топик «Бэкапы» закрыт: откройте его в группе')
            raise BackupError(f'Telegram: {desc or short(e, 200)}')
        message_id = (sent or {}).get('message_id')
        if not message_id:
            raise BackupError('Telegram не вернул сообщение')
        message_ids.append(message_id)
    return message_ids


def send_backup(client, db_path: str, chat_id, data: bytes, name: str, sha256: str, meta: dict,
                *, now: Optional[datetime] = None) -> dict:
    """Upload the archive into the topic and record the run. Raises
    BackupError, and then records nothing (the caller reports the failure)."""
    now = now or utcnow()
    if not chat_id:
        raise BackupError('FORUM_GROUP_ID не задан: некуда отправлять')
    if not NAME_RE.match(name or ''):
        raise BackupError('имя архива не похоже на nekovpn-backup-<время>.tar.zst.age')
    if not data.startswith(AGE_HEADER):
        raise BackupError('на вход пришёл не файл age: нешифрованное не отправляю')
    if len(data) > MAX_ARCHIVE:
        raise BackupError(f'архив {human(len(data))}: больше {human(MAX_ARCHIVE)}')
    if hashlib.sha256(data).hexdigest() != (sha256 or '').lower():
        raise BackupError('sha256 не совпал: архив дошёл до бота не целиком')
    meta = meta if isinstance(meta, dict) else {}
    chunks = split_parts(data)
    thread_id = resolve_topic(client, db_path, chat_id, now=now)
    try:
        message_ids = _upload(client, chat_id, thread_id, chunks, name, sha256, meta, len(data))
    except ThreadGone:
        thread_id = create_topic(client, db_path, chat_id, now=now)
        message_ids = _upload(client, chat_id, thread_id, chunks, name, sha256, meta, len(data))
    record_sent(db_path, meta, name, len(data), sha256, message_ids, chat_id, thread_id, now=now)
    return {'ok': True, 'thread_id': thread_id, 'message_ids': message_ids,
            'parts': len(chunks), 'complete': bool(meta.get('complete')) and not missing_parts(meta)}


# ----------------------------------------------------------------- alert ----

def alert_verdict(state, now: datetime) -> Optional[dict]:
    """What AlertManager's ``backup:stale`` says about ``backup_state``. Pure.

    No state = the timer is not installed: nothing to say. A complete backup
    younger than STALE_H: nothing. Otherwise critical — a missing backup is
    the one thing nobody notices until the day it is needed. A failed or
    partial run before the first complete one pages at once; a timer that
    never ran pages STALE_H after it was installed."""
    if not isinstance(state, dict) or not state:
        return None
    limit = timedelta(hours=STALE_H)
    last_ok = parse_iso((state.get('last_ok') or {}).get('ts')
                        if isinstance(state.get('last_ok'), dict) else None)
    if last_ok and now - last_ok < limit:
        return None
    run = state.get('last_run') if isinstance(state.get('last_run'), dict) else {}
    run_ts = parse_iso(run.get('ts'))
    installed = parse_iso(state.get('installed'))
    sent = state.get('last_sent') if isinstance(state.get('last_sent'), dict) else {}
    sent_ts = parse_iso(sent.get('ts'))

    if last_ok:
        title = f'Полный бэкап сервиса не уходил {fmt_age(now - last_ok)}'
    elif run_ts:
        title = 'Полного бэкапа сервиса ещё не было'
    elif installed and now - installed >= limit:
        title = f'Бэкап сервиса ни разу не запускался (таймер стоит с {fmt_ts(installed)})'
    else:
        return None

    detail = []
    if last_ok:
        detail.append(f'Последний полный: {fmt_ts(last_ok)}.')
    if sent_ts and (last_ok is None or sent_ts > last_ok):
        missing = sent.get('missing') if isinstance(sent.get('missing'), dict) else {}
        why = '; '.join(f'{k}: {short(v, 120)}' for k, v in missing.items()) or '—'
        detail.append(f'Последний архив {fmt_ts(sent_ts)} ушёл не целым: {why}.')
    if run_ts and run.get('status') == 'failed':
        detail.append(f'Последний запуск {fmt_ts(run_ts)}: {short(run.get("error"), 300)}.')
    detail.append(CHECK_HINT)
    return {'key': 'backup:stale', 'severity': 'critical', 'title': title,
            'detail': ' '.join(detail)}
