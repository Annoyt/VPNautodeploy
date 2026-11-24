"""Email and Telegram as one account: a code mailed to the address (2026-11-24).

A person who got a key by mail (an ``ext_*`` row whose contact_email is the
address) and later came to Telegram used to be a new user there: a second,
demo key, the paid one left on the mail row (Tatiana, 2026-11-24). Now
«📧 Я регистрировался через почту» and «📧 Добавить почту» ask for the
address, mail a six-digit code to it, and the right code links them
(``link``):

- no other row holds the address: it becomes this user's contact_email;
- another row holds only the address: it moves here;
- one other row holds it with a key, and this user has no key, or a demo key
  against the other's paid one: the key, the payment and the subscription
  record move here (``merge``), the other row stays empty for history, and
  this user's own demo client is for the caller to take off the panel;
- anything else (both paid, several rows): the admin decides.

The subscription link is a hash of the key, so a person's apps keep working.
Codes live 15 minutes with five tries; three letters an hour per user. Only
an HMAC of a code is kept, in ``email_codes``, which backups leave out.
"""

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from bot.config.secret_store import sub_token_key
from bot.utils.validators import validate_email

logger = logging.getLogger(__name__)

TABLE = 'email_codes'
CODE_TTL_S = 15 * 60
MAX_TRIES = 5
MAX_SENDS_PER_HOUR = 3
KEY_STATUSES = ('demo', 'paid', 'support_topic')
# What a key brings with it from the mail row to the Telegram one.
MOVE = (
    'uuid', 'email', 'status', 'subscription_expiry', 'limit_ip', 'quota_gb',
    'last_traffic_update', 'traffic_up', 'traffic_down', 'next_protocol_idx',
    'last_country', 'last_asn', 'last_city', 'last_lat', 'last_lon',
    'contact_email',
)

_LETTERS = {
    'ru': ('NekoVPN: код подтверждения',
           'Ваш код для привязки почты к Telegram: {code}\n\n'
           'Он действует 15 минут. Если вы его не запрашивали, просто '
           'удалите это письмо.'),
    'en': ('NekoVPN: your code',
           'Your code to link this address with Telegram: {code}\n\n'
           'It works for 15 minutes. If you did not ask for it, just '
           'delete this letter.'),
}


def ensure_table(db) -> None:
    with db._connect() as conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} ("
            "chat_id TEXT PRIMARY KEY, email TEXT NOT NULL, "
            "code_hash TEXT NOT NULL, expires_at REAL NOT NULL, "
            "tries INTEGER NOT NULL DEFAULT 0, window_start REAL NOT NULL, "
            "sends INTEGER NOT NULL DEFAULT 0)"
        )


def normalize(email: str) -> str:
    return (email or '').strip().lower()


def masked(email: str) -> str:
    """t***@gmail.com: the address as shown back in the chat."""
    local, _, domain = normalize(email).partition('@')
    return f'{local[:1]}***@{domain}'


def _hash(config, chat_id: str, email: str, code: str) -> str:
    key = sub_token_key(config).encode()
    return hmac.new(key, f'{chat_id}\n{email}\n{code}'.encode(),
                    hashlib.sha256).hexdigest()


def send_code(db, config, mailer, chat_id: str, email: str, *, lang='ru',
              now: Optional[float] = None, rand=secrets.randbelow) -> str:
    """Mail a fresh code. 'sent', 'invalid', 'rate_limited' or
    'mail_failed'; a letter the relay refused does not count."""
    email = normalize(email)
    if not validate_email(email):
        return 'invalid'
    now = time.time() if now is None else now
    ensure_table(db)
    with db._connect() as conn:
        row = conn.execute(
            f"SELECT window_start, sends FROM {TABLE} WHERE chat_id = ?",
            (str(chat_id),)).fetchone()
    window, sends = (row[0], row[1]) if row and now - row[0] < 3600 else (now, 0)
    if sends >= MAX_SENDS_PER_HOUR:
        return 'rate_limited'
    code = f'{rand(1_000_000):06d}'
    subject, body = _LETTERS['en' if lang == 'en' else 'ru']
    if not mailer.send_notice(email, subject, body.format(code=code)):
        return 'mail_failed'
    with db._connect() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {TABLE} (chat_id, email, code_hash, "
            "expires_at, tries, window_start, sends) VALUES (?, ?, ?, ?, 0, ?, ?)",
            (str(chat_id), email, _hash(config, str(chat_id), email, code),
             now + CODE_TTL_S, window, sends + 1))
    return 'sent'


def pending_email(db, chat_id: str) -> Optional[str]:
    """The address the user's live code was sent to, if any."""
    ensure_table(db)
    with db._connect() as conn:
        row = conn.execute(f"SELECT email FROM {TABLE} WHERE chat_id = ?",
                           (str(chat_id),)).fetchone()
    return row[0] if row else None


def check_code(db, config, chat_id: str, code: str, *,
               now: Optional[float] = None):
    """(outcome, email): 'ok' with the address, else 'none', 'expired',
    'wrong' or 'too_many' with None. A right code is spent."""
    now = time.time() if now is None else now
    ensure_table(db)
    chat_id = str(chat_id)
    with db._connect() as conn:
        row = conn.execute(
            f"SELECT email, code_hash, expires_at, tries FROM {TABLE} "
            "WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row:
            return 'none', None
        email, code_hash, expires_at, tries = row
        if now > expires_at:
            return 'expired', None
        if tries >= MAX_TRIES:
            return 'too_many', None
        if hmac.compare_digest(_hash(config, chat_id, email, (code or '').strip()), code_hash):
            conn.execute(f"UPDATE {TABLE} SET expires_at = 0 WHERE chat_id = ?", (chat_id,))
            return 'ok', email
        conn.execute(f"UPDATE {TABLE} SET tries = tries + 1 WHERE chat_id = ?", (chat_id,))
    return ('too_many' if tries + 1 >= MAX_TRIES else 'wrong'), None


def tries_left(db, chat_id: str) -> int:
    with db._connect() as conn:
        row = conn.execute(f"SELECT tries FROM {TABLE} WHERE chat_id = ?",
                           (str(chat_id),)).fetchone()
    return max(0, MAX_TRIES - (row[0] if row else MAX_TRIES))


@dataclass
class LinkResult:
    outcome: str                          # 'linked', 'merged' or 'admin'
    source: Optional[str] = None          # the other row's chat_id
    dropped_client: Optional[str] = None  # this user's replaced panel client
    paid_until: Optional[str] = None


def link(db, chat_id: str, email: str) -> LinkResult:
    """Tie a verified address to ``chat_id``, merging the row that holds
    it when the rules above allow; one transaction."""
    email, chat_id = normalize(email), str(chat_id)
    conn = db._connect()
    conn.isolation_level = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        result = _link(conn, chat_id, email)
        conn.execute('COMMIT' if result.outcome != 'admin' else 'ROLLBACK')
        return result
    except Exception:
        conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


def _link(conn, chat_id: str, email: str) -> LinkResult:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    target = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    others = conn.execute(
        "SELECT * FROM users WHERE LOWER(TRIM(contact_email)) = ? AND chat_id != ?",
        (email, chat_id)).fetchall()
    if target is None:
        raise ValueError(f'email link: no user {chat_id}')
    if len(others) > 1:
        return LinkResult('admin', source=','.join(r['chat_id'] for r in others))
    if not others:
        conn.execute("UPDATE users SET contact_email = ? WHERE chat_id = ?", (email, chat_id))
        return LinkResult('linked')
    source = others[0]
    if not (source['uuid'] and source['status'] in KEY_STATUSES):
        conn.execute("UPDATE users SET contact_email = NULL WHERE chat_id = ?", (source['chat_id'],))
        conn.execute("UPDATE users SET contact_email = ? WHERE chat_id = ?", (email, chat_id))
        return LinkResult('linked', source=source['chat_id'])
    if target['uuid'] and not (target['status'] == 'demo' and source['status'] == 'paid'):
        return LinkResult('admin', source=source['chat_id'])
    move = [c for c in MOVE if c in cols]
    conn.execute(
        "UPDATE users SET uuid = NULL, email = NULL, contact_email = NULL, "
        "previous_state = status, status = 'new' WHERE chat_id = ?", (source['chat_id'],))
    conn.execute(
        "UPDATE users SET " + ', '.join(f'{c} = ?' for c in move) + " WHERE chat_id = ?",
        tuple(email if c == 'contact_email' else source[c] for c in move) + (chat_id,))
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'subscriptions'").fetchone():
        conn.execute("UPDATE subscriptions SET chat_id = ? WHERE chat_id = ?", (chat_id, source['chat_id']))
    conn.execute(
        "INSERT INTO admin_actions (admin_id, action, target_id, details) "
        "VALUES ('self', 'email_link_merge', ?, ?)", (chat_id, f"from {source['chat_id']}"))
    return LinkResult(
        'merged', source=source['chat_id'],
        dropped_client=target['email'] if target['uuid'] else None,
        paid_until=source['subscription_expiry'] if source['status'] == 'paid' else None)
