"""Inbound mail → key requests.

People without Telegram ask for keys by writing to the service mailbox;
until now those letters sat unread until an operator happened to check
and typed /addmail by hand. This poller closes the loop:

  1. Every few minutes read the INBOX over IMAP (same Gmail creds the
     SMTP relay uses).
  2. Only mail that arrived AFTER the feature went live is considered:
     the first run records UIDNEXT as a baseline and touches nothing —
     the historical backlog stays untouched and unread.
  3. Each new letter from a plausible human address becomes a row in
     ``email_requests`` plus a card in the admin forum (requests topic)
     with «✅ Выдать» / «❌ Отклонить» buttons. Approval provisions a
     DEMO-tier key (user's decision 2026-08-19) and emails it back via
     the existing /addmail machinery.

Nothing is ever issued automatically — the button is the gate, spam
dies in the pending pile.
"""

import email
import email.utils
import imaplib
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Senders that are machinery, not humans.
_MACHINE_SENDER = re.compile(
    r'(no-?reply|mailer-daemon|postmaster|bounce|notification|do-?not-?reply)',
    re.IGNORECASE,
)

_LAST_UID_KEY = 'mail_intake_last_uid'


class MailIntakeService:
    """IMAP poller that turns inbound letters into admin-approvable
    key requests."""

    def __init__(self, bot, db, config):
        self.bot = bot
        self.db = db
        self.config = config
        self.imap_host = (getattr(config, 'IMAP_HOST', '') or
                          'imap.gmail.com').strip()
        self.user = (getattr(config, 'SMTP_USER', '') or '').strip()
        self.password = getattr(config, 'SMTP_PASSWORD', '') or ''

    def is_configured(self) -> bool:
        if str(getattr(self.config, 'MAIL_INTAKE_ENABLED', '1')
               ).strip().lower() in ('0', 'false', 'no'):
            return False
        return bool(self.user and self.password)

    # ---- storage ----

    def _ensure_table(self) -> None:
        with self.db._connect() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS email_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT DEFAULT CURRENT_TIMESTAMP,
                    from_addr TEXT NOT NULL,
                    subject TEXT,
                    message_id TEXT,
                    imap_uid INTEGER,
                    known_user TEXT,
                    status TEXT DEFAULT 'pending',
                    decided_by TEXT,
                    decided_at TEXT
                )
            ''')
            conn.commit()

    # ---- imap plumbing (separate for testability) ----

    def _connect(self):
        m = imaplib.IMAP4_SSL(self.imap_host, 993)
        m.login(self.user, self.password)
        m.select('INBOX', readonly=True)
        return m

    @staticmethod
    def _uidnext(m) -> Optional[int]:
        typ, data = m.status('INBOX', '(UIDNEXT)')
        if typ != 'OK' or not data:
            return None
        match = re.search(rb'UIDNEXT (\d+)', data[0])
        return int(match.group(1)) if match else None

    # ---- main entry, called from the scheduler ----

    def poll_once(self) -> int:
        """Process new mail; returns the number of requests created."""
        if not self.is_configured():
            return 0
        self._ensure_table()

        try:
            m = self._connect()
        except Exception as e:
            logger.warning(f"mail_intake: IMAP connect failed: {e}")
            return 0

        created = 0
        try:
            last_raw = self.db.get_setting(_LAST_UID_KEY)
            if not last_raw:
                # First run: remember where the future starts. The
                # historical backlog is deliberately never processed.
                nxt = self._uidnext(m)
                if nxt:
                    self.db.set_setting(_LAST_UID_KEY, str(nxt))
                    logger.info(f"mail_intake: baseline UID set to {nxt}")
                return 0

            last = int(last_raw)
            typ, data = m.uid('SEARCH', None, f'UID {last}:*')
            if typ != 'OK':
                return 0
            uids = [int(u) for u in (data[0].split() if data and data[0]
                                     else []) if int(u) >= last]
            if not uids:
                return 0

            for uid in sorted(uids):
                try:
                    if self._process_message(m, uid):
                        created += 1
                except Exception as e:
                    logger.warning(f"mail_intake: uid {uid} failed: {e}")
            self.db.set_setting(_LAST_UID_KEY, str(max(uids) + 1))
        finally:
            try:
                m.logout()
            except Exception:
                pass

        if created:
            logger.info(f"mail_intake: created {created} key request(s)")
        return created

    # ---- per-message ----

    def _process_message(self, m, uid: int) -> bool:
        typ, data = m.uid(
            'FETCH', str(uid),
            '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])',
        )
        if typ != 'OK' or not data or data[0] is None:
            return False
        raw = data[0][1] if isinstance(data[0], tuple) else data[0]
        msg = email.message_from_bytes(raw or b'')
        # Some senders RFC2047-encode the whole From value (address
        # included) — decode before parsing or the address is garbage.
        _name, addr = email.utils.parseaddr(
            self._decode(msg.get('From', '')))
        addr = (addr or '').strip().lower()
        subject = self._decode(msg.get('Subject', ''))[:200]
        message_id = (msg.get('Message-ID', '') or '').strip()[:250]

        from bot.utils.validators import validate_email
        if not addr or not validate_email(addr):
            return False
        if addr == self.user.lower():
            return False
        if _MACHINE_SENDER.search(addr):
            return False

        with self.db._connect() as conn:
            if message_id and conn.execute(
                "SELECT 1 FROM email_requests WHERE message_id = ?",
                (message_id,),
            ).fetchone():
                return False
            # One open request per address — a follow-up letter must
            # not spawn a second card.
            if conn.execute(
                "SELECT 1 FROM email_requests WHERE from_addr = ? "
                "AND status = 'pending'", (addr,),
            ).fetchone():
                return False
            known = conn.execute(
                "SELECT chat_id, status FROM users WHERE contact_email = ? "
                "LIMIT 1", (addr,),
            ).fetchone()
            cur = conn.execute(
                "INSERT INTO email_requests "
                "(from_addr, subject, message_id, imap_uid, known_user) "
                "VALUES (?, ?, ?, ?, ?)",
                (addr, subject, message_id, uid,
                 known[0] if known else None),
            )
            req_id = cur.lastrowid
            conn.commit()

        self._notify_admin(req_id, addr, subject,
                           known[1] if known else None)
        return True

    @staticmethod
    def _decode(value) -> str:
        # msg.get() may hand back a Header object for non-ASCII values;
        # everything downstream wants plain str.
        value = str(value or '')
        try:
            parts = email.header.decode_header(value)
            return ''.join(
                p.decode(enc or 'utf-8', 'replace') if isinstance(p, bytes)
                else p
                for p, enc in parts
            )
        except Exception:
            return value

    def _notify_admin(self, req_id: int, addr: str, subject: str,
                      known_status: Optional[str]) -> None:
        demo_gb = int(getattr(self.config, 'DEMO_TRAFFIC_GB', 10) or 10)
        lines = [
            "📨 <b>Заявка на ключ с почты</b>",
            f"От: <code>{addr}</code>",
        ]
        if subject:
            lines.append(f"Тема: {subject}")
        if known_status:
            lines.append(
                f"⚠️ Уже есть юзер со статусом <b>{known_status}</b> — "
                f"«Выдать» просто перешлёт ключ заново."
            )
        else:
            lines.append(f"«Выдать» = демо {demo_gb} ГБ/мес письмом.")
        keyboard = {'inline_keyboard': [[
            {'text': '✅ Выдать', 'callback_data': f'mailreq:ok:{req_id}'},
            {'text': '❌ Отклонить', 'callback_data': f'mailreq:no:{req_id}'},
        ]]}

        # Forum topic first, admin PM only as fallback (house rule).
        if getattr(self.config, 'FORUM_ENABLED', False) and \
                getattr(self.config, 'FORUM_GROUP_ID', None):
            self.bot.send_message(
                chat_id=self.config.FORUM_GROUP_ID,
                text='\n'.join(lines), parse_mode='HTML',
                reply_markup=keyboard,
                message_thread_id=getattr(self.config, 'TOPIC_REQUESTS', None),
            )
        else:
            self.bot.send_message(
                chat_id=str(self.config.SUPER_ADMIN_ID),
                text='\n'.join(lines), parse_mode='HTML',
                reply_markup=keyboard,
            )
