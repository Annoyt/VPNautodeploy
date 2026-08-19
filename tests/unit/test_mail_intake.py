"""Inbound-mail key requests: poller, admin card, approve flow.

User flow being protected: a person without Telegram writes to the
service mailbox → a card with «Выдать/Отклонить» lands in the admin
forum → one click provisions a DEMO key and emails it back. Nothing
is issued without the click, the pre-launch mailbox backlog is never
touched, and machinery senders never become requests.
"""

import sqlite3
from unittest.mock import MagicMock, Mock, patch

import pytest

from bot.services.mail_intake import MailIntakeService


def _config(**over):
    cfg = Mock()
    cfg.SMTP_USER = 'svc@gmail.com'
    cfg.SMTP_PASSWORD = 'app-pw'
    cfg.IMAP_HOST = 'imap.test'
    cfg.MAIL_INTAKE_ENABLED = '1'
    cfg.FORUM_ENABLED = True
    cfg.FORUM_GROUP_ID = '-100777'
    cfg.TOPIC_REQUESTS = 15
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.DEMO_TRAFFIC_GB = 10
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


class _FakeDB:
    """Real sqlite behind the Database surface the service touches."""

    def __init__(self, tmp_path):
        self.path = str(tmp_path / 'bot.db')
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE users (chat_id TEXT, contact_email TEXT, "
            "status TEXT)")
        conn.execute(
            "CREATE TABLE settings_kv (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()

    def _connect(self):
        return sqlite3.connect(self.path)

    def get_setting(self, key, default=None):
        with self._connect() as c:
            row = c.execute(
                "SELECT value FROM settings_kv WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else default

    def set_setting(self, key, value):
        with self._connect() as c:
            c.execute(
                "INSERT INTO settings_kv (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))
            c.commit()
        return True


def _raw(from_addr, subject='хочу ключ', msg_id='<m1@x>'):
    return (
        f'From: {from_addr}\r\nSubject: {subject}\r\n'
        f'Message-ID: {msg_id}\r\n\r\n'
    ).encode()


def _imap(uidnext=100, messages=None):
    """Fake imaplib connection: status/uid(SEARCH)/uid(FETCH)/logout."""
    m = MagicMock()
    m.status.return_value = ('OK', [f'"INBOX" (UIDNEXT {uidnext})'.encode()])
    msgs = messages or {}

    def _uid(cmd, *args):
        if cmd == 'SEARCH':
            uids = ' '.join(str(u) for u in sorted(msgs)).encode()
            return ('OK', [uids])
        if cmd == 'FETCH':
            uid = int(args[0])
            if uid in msgs:
                return ('OK', [(b'HEADER', msgs[uid])])
            return ('OK', [None])
        raise AssertionError(cmd)

    m.uid.side_effect = _uid
    return m


class TestMailIntake:

    def _svc(self, tmp_path, imap, **cfg_over):
        svc = MailIntakeService(Mock(), _FakeDB(tmp_path), _config(**cfg_over))
        svc._connect = Mock(return_value=imap)
        return svc

    def test_first_run_sets_baseline_and_touches_nothing(self, tmp_path):
        svc = self._svc(tmp_path, _imap(uidnext=774, messages={5: _raw('a@b.com')}))
        assert svc.poll_once() == 0
        assert svc.db.get_setting('mail_intake_last_uid') == '774'
        svc.bot.send_message.assert_not_called()

    def test_new_mail_becomes_request_card(self, tmp_path):
        svc = self._svc(tmp_path, _imap(messages={800: _raw('ivan@mail.ru')}))
        svc.db.set_setting('mail_intake_last_uid', '800')

        assert svc.poll_once() == 1

        with svc.db._connect() as c:
            row = c.execute(
                "SELECT from_addr, status FROM email_requests").fetchone()
        assert row == ('ivan@mail.ru', 'pending')
        # Card lands in the requests topic with both buttons.
        kwargs = svc.bot.send_message.call_args.kwargs
        assert kwargs['chat_id'] == '-100777'
        assert kwargs['message_thread_id'] == 15
        buttons = kwargs['reply_markup']['inline_keyboard'][0]
        assert buttons[0]['callback_data'].startswith('mailreq:ok:')
        assert buttons[1]['callback_data'].startswith('mailreq:no:')
        # Cursor advanced past the processed mail.
        assert svc.db.get_setting('mail_intake_last_uid') == '801'

    def test_machinery_and_own_mail_skipped(self, tmp_path):
        svc = self._svc(tmp_path, _imap(messages={
            800: _raw('no-reply@spam.io', msg_id='<m1@x>'),
            801: _raw('mailer-daemon@google.com', msg_id='<m2@x>'),
            802: _raw('svc@gmail.com', msg_id='<m3@x>'),
        }))
        svc.db.set_setting('mail_intake_last_uid', '800')

        assert svc.poll_once() == 0
        svc.bot.send_message.assert_not_called()

    def test_duplicate_message_id_and_open_request_skipped(self, tmp_path):
        svc = self._svc(tmp_path, _imap(messages={
            800: _raw('ivan@mail.ru', msg_id='<same@x>'),
            801: _raw('ivan@mail.ru', msg_id='<same@x>'),      # dup id
            802: _raw('ivan@mail.ru', msg_id='<другое@x>'),    # open request
        }))
        svc.db.set_setting('mail_intake_last_uid', '800')

        assert svc.poll_once() == 1

    def test_known_user_flagged_on_card(self, tmp_path):
        db = _FakeDB(tmp_path)
        with db._connect() as c:
            c.execute("INSERT INTO users VALUES ('ext_1', 'ivan@mail.ru', 'paid')")
            c.commit()
        svc = MailIntakeService(Mock(), db, _config())
        svc._connect = Mock(return_value=_imap(messages={800: _raw('ivan@mail.ru')}))
        svc.db.set_setting('mail_intake_last_uid', '800')

        assert svc.poll_once() == 1
        text = svc.bot.send_message.call_args.kwargs['text']
        assert 'Уже есть юзер' in text

    def test_disabled_by_env(self, tmp_path):
        svc = self._svc(tmp_path, _imap(), MAIL_INTAKE_ENABLED='0')
        assert svc.poll_once() == 0
        svc._connect.assert_not_called()

    def test_pm_fallback_without_forum(self, tmp_path):
        svc = self._svc(tmp_path, _imap(messages={800: _raw('x@y.com')}),
                        FORUM_ENABLED=False)
        svc.db.set_setting('mail_intake_last_uid', '800')

        assert svc.poll_once() == 1
        assert svc.bot.send_message.call_args.kwargs['chat_id'] == '1652899'


class TestMailRequestCallback:

    def _handler(self, tmp_path, status='pending'):
        from bot.handlers.callbacks.admin import MailRequestHandler
        db = _FakeDB(tmp_path)
        with db._connect() as c:
            c.execute(
                "CREATE TABLE email_requests (id INTEGER PRIMARY KEY, "
                "from_addr TEXT, status TEXT, decided_by TEXT, decided_at TEXT)")
            c.execute("INSERT INTO email_requests VALUES (7, 'ivan@mail.ru', ?, NULL, NULL)",
                      (status,))
            c.execute("CREATE TABLE admin_logs (a TEXT)")
            c.commit()
        db.log_admin_action = Mock()
        h = MailRequestHandler(Mock(), db, _config())
        h.validator = Mock()   # admin check passes
        return h

    def _update(self):
        return {'callback_query': {
            'id': 'cb1',
            'message': {'message_thread_id': 15},
        }}

    def test_approve_provisions_demo_and_mails_key(self, tmp_path):
        h = self._handler(tmp_path)
        mailer = Mock()
        mailer.is_configured.return_value = True
        mailer.send_key.return_value = True
        h.bot.services = {'email': mailer}

        with patch('bot.handlers.admin.AdminHandler') as AH:
            AH.return_value._provision_email_user.return_value = 'https://sub/x'
            h.handle(self._update(), '-100777', '1652899',
                     data='mailreq:ok:7')
            import time
            time.sleep(0.2)   # sender worker thread

        AH.return_value._provision_email_user.assert_called_once_with(
            'ivan@mail.ru', 10, 30, status='demo')
        mailer.send_key.assert_called_once()
        with h.db._connect() as c:
            assert c.execute("SELECT status FROM email_requests WHERE id=7"
                             ).fetchone()[0] == 'approved'

    def test_reject_marks_and_does_not_provision(self, tmp_path):
        h = self._handler(tmp_path)
        with patch('bot.handlers.admin.AdminHandler') as AH:
            h.handle(self._update(), '-100777', '1652899',
                     data='mailreq:no:7')
        AH.return_value._provision_email_user.assert_not_called()
        with h.db._connect() as c:
            assert c.execute("SELECT status FROM email_requests WHERE id=7"
                             ).fetchone()[0] == 'rejected'

    def test_already_processed_answered_without_side_effects(self, tmp_path):
        h = self._handler(tmp_path, status='approved')
        with patch('bot.handlers.admin.AdminHandler') as AH:
            h.handle(self._update(), '-100777', '1652899',
                     data='mailreq:ok:7')
        AH.return_value._provision_email_user.assert_not_called()
        h.bot.answer_callback_query.assert_called_once()


def test_fully_encoded_from_header_still_parses(tmp_path):
    """Some senders RFC2047-encode the entire From value, address and
    all — the poller must decode before parseaddr or every such letter
    is silently dropped (caught live on 2026-08-19)."""
    from email.header import Header
    encoded_from = Header('Тест Флоу <ivan@mail.ru>', 'utf-8').encode()
    raw = (
        f'From: {encoded_from}\r\nSubject: key please\r\n'
        f'Message-ID: <enc@x>\r\n\r\n'
    ).encode()

    svc = MailIntakeService(Mock(), _FakeDB(tmp_path), _config())
    svc._connect = Mock(return_value=_imap(messages={800: raw}))
    svc.db.set_setting('mail_intake_last_uid', '800')

    assert svc.poll_once() == 1
    with svc.db._connect() as c:
        assert c.execute("SELECT from_addr FROM email_requests"
                         ).fetchone()[0] == 'ivan@mail.ru'
