"""bot/services/email_link.py and its Telegram flow: a code mailed to the
address links a mail account with Telegram (2026-11-24, the owner).

The case behind it: Tatiana's paid key lived on an ext_* row (by mail
since 08-30); she came to Telegram and was a new user there. Real sqlite
with the repo's schema; mail, Telegram and the panel faked.
"""

import re
import sqlite3
import time
from types import SimpleNamespace

import pytest

from bot.core.database import Database
from bot.models.user import User
from bot.services import email_link

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

CONFIG = SimpleNamespace(SUB_TOKEN_SECRET='pinned-secret', BOT_TOKEN='bot',
                         FORUM_ENABLED=False, SUPER_ADMIN_ID='900',
                         FORUM_GROUP_ID=0, TOPIC_REQUESTS=0)
NOW = 1_800_000_000.0


class Mailer:
    def __init__(self, ok=True):
        self.ok, self.letters = ok, []

    def send_notice(self, to, subject, body, **kw):
        self.letters.append((to, subject, body))
        return self.ok

    def last_code(self):
        return re.search(r'\b(\d{6})\b', self.letters[-1][2]).group(1)


class Panel:
    def __init__(self):
        self.removed, self.comments = [], []

    def remove_client_sync(self, email):
        self.removed.append(email)
        return True

    def set_client_comment_sync(self, email, comment):
        self.comments.append((email, comment))
        return True


class Bot:
    def __init__(self, mailer, panel):
        self.sent, self.services = [], {'email': mailer, 'xui': panel}

    def send_message(self, **kw):
        self.sent.append(kw)
        return {'message_id': len(self.sent)}

    send_message_to_topic = send_message


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def seed(db, chat_id, **fields):
    db._users.save(User(chat_id=chat_id, **fields))


def row(db, chat_id):
    conn = sqlite3.connect(db.db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    finally:
        conn.close()


def tatiana(db):
    """The mail row with the paid key, its subscription record, and the
    keyless Telegram row she made later."""
    seed(db, 'ext_aa', uuid='uuid-paid', email='user_ext@nekovo.ru', status='paid',
         contact_email='Tat@Gmail.com', subscription_expiry='2028-03-30T23:59:00',
         quota_gb=100.0, limit_ip=5, platform='android')
    with sqlite3.connect(db.db_path) as conn:
        conn.execute("INSERT INTO subscriptions (chat_id, plan_type, expires_at, is_active) "
                     "VALUES ('ext_aa', 'monthly', '2028-03-30T23:59:00', 1)")
    seed(db, '555', status='new', platform='android')


class TestCodes:

    def test_a_letter_with_six_digits_and_only_a_hash_kept(self, db):
        mailer = Mailer()
        assert email_link.send_code(db, CONFIG, mailer, '555', ' T@Gmail.com ',
                                    now=NOW, rand=lambda n: 4242) == 'sent'
        to, subject, body = mailer.letters[-1]
        assert to == 't@gmail.com' and 'код' in subject and '004242' in body
        with sqlite3.connect(db.db_path) as conn:
            stored = conn.execute("SELECT * FROM email_codes").fetchall()
        assert len(stored) == 1 and '004242' not in repr(stored)

    def test_not_an_address_sends_nothing(self, db):
        mailer = Mailer()
        assert email_link.send_code(db, CONFIG, mailer, '555', 'ne-pochta', now=NOW) == 'invalid'
        assert mailer.letters == []

    def test_three_letters_an_hour(self, db):
        mailer = Mailer()
        for i in range(3):
            assert email_link.send_code(db, CONFIG, mailer, '555', 't@x.io', now=NOW + i) == 'sent'
        assert email_link.send_code(db, CONFIG, mailer, '555', 't@x.io', now=NOW + 10) == 'rate_limited'
        assert len(mailer.letters) == 3
        assert email_link.send_code(db, CONFIG, mailer, '555', 't@x.io', now=NOW + 3601) == 'sent'

    def test_a_refused_letter_does_not_count(self, db):
        assert email_link.send_code(db, CONFIG, Mailer(ok=False), '555', 't@x.io',
                                    now=NOW) == 'mail_failed'
        mailer = Mailer()
        for i in range(3):
            assert email_link.send_code(db, CONFIG, mailer, '555', 't@x.io', now=NOW + i) == 'sent'

    def test_the_right_code_works_once(self, db):
        mailer = Mailer()
        email_link.send_code(db, CONFIG, mailer, '555', 'T@x.io', now=NOW)
        code = mailer.last_code()
        assert email_link.check_code(db, CONFIG, '555', code, now=NOW + 60) == ('ok', 't@x.io')
        assert email_link.check_code(db, CONFIG, '555', code, now=NOW + 61) == ('expired', None)

    def test_a_code_is_bound_to_its_user(self, db):
        mailer = Mailer()
        email_link.send_code(db, CONFIG, mailer, '555', 't@x.io', now=NOW)
        code = mailer.last_code()
        assert email_link.check_code(db, CONFIG, '777', code, now=NOW + 1) == ('none', None)

    def test_five_tries(self, db):
        mailer = Mailer()
        email_link.send_code(db, CONFIG, mailer, '555', 't@x.io', now=NOW, rand=lambda n: 111111)
        for left in (4, 3, 2, 1):
            assert email_link.check_code(db, CONFIG, '555', '222222', now=NOW + 1) == ('wrong', None)
            assert email_link.tries_left(db, '555') == left
        assert email_link.check_code(db, CONFIG, '555', '222222', now=NOW + 1) == ('too_many', None)
        assert email_link.check_code(db, CONFIG, '555', '111111', now=NOW + 1) == ('too_many', None)

    def test_fifteen_minutes(self, db):
        mailer = Mailer()
        email_link.send_code(db, CONFIG, mailer, '555', 't@x.io', now=NOW)
        code = mailer.last_code()
        assert email_link.check_code(db, CONFIG, '555', code,
                                     now=NOW + email_link.CODE_TTL_S + 1) == ('expired', None)

    def test_a_new_letter_gives_new_tries(self, db):
        mailer = Mailer()
        email_link.send_code(db, CONFIG, mailer, '555', 't@x.io', now=NOW, rand=lambda n: 111111)
        for _ in range(5):
            email_link.check_code(db, CONFIG, '555', '000000', now=NOW + 1)
        email_link.send_code(db, CONFIG, mailer, '555', 't@x.io', now=NOW + 2, rand=lambda n: 333333)
        assert email_link.check_code(db, CONFIG, '555', '333333', now=NOW + 3) == ('ok', 't@x.io')


class TestLink:

    def test_nobody_holds_it_it_becomes_hers(self, db):
        seed(db, '555', status='new')
        assert email_link.link(db, '555', 'T@x.io').outcome == 'linked'
        assert row(db, '555')['contact_email'] == 't@x.io'

    def test_a_keyless_holder_gives_the_address_up(self, db):
        seed(db, 'ext_old', status='new', contact_email='t@x.io')
        seed(db, '555', status='new')
        result = email_link.link(db, '555', 't@x.io')
        assert (result.outcome, result.source) == ('linked', 'ext_old')
        assert row(db, 'ext_old')['contact_email'] is None
        assert row(db, '555')['contact_email'] == 't@x.io'

    def test_tatiana_her_paid_key_comes_to_telegram(self, db):
        tatiana(db)
        result = email_link.link(db, '555', ' tat@gmail.com ')
        assert (result.outcome, result.source, result.dropped_client) == ('merged', 'ext_aa', None)
        assert result.paid_until == '2028-03-30T23:59:00'
        tg, mail = row(db, '555'), row(db, 'ext_aa')
        assert (tg['uuid'], tg['email'], tg['status']) == ('uuid-paid', 'user_ext@nekovo.ru', 'paid')
        assert (tg['subscription_expiry'], tg['quota_gb'], tg['limit_ip']) == ('2028-03-30T23:59:00', 100.0, 5)
        assert tg['contact_email'] == 'tat@gmail.com'
        assert (mail['uuid'], mail['email'], mail['contact_email']) == (None, None, None)
        assert (mail['status'], mail['previous_state']) == ('new', 'paid')
        with sqlite3.connect(db.db_path) as conn:
            assert conn.execute("SELECT chat_id FROM subscriptions").fetchall() == [('555',)]
            assert conn.execute(
                "SELECT admin_id, action, target_id, details FROM admin_actions"
            ).fetchall() == [('self', 'email_link_merge', '555', 'from ext_aa')]

    def test_a_demo_key_gives_way_to_the_paid_one(self, db):
        tatiana(db)
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("UPDATE users SET uuid = 'uuid-demo', email = 'user_555@nekovo.ru', "
                         "status = 'demo' WHERE chat_id = '555'")
        result = email_link.link(db, '555', 'tat@gmail.com')
        assert (result.outcome, result.dropped_client) == ('merged', 'user_555@nekovo.ru')
        assert row(db, '555')['uuid'] == 'uuid-paid'

    def test_two_keys_that_both_count_go_to_the_admin(self, db):
        tatiana(db)
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("UPDATE users SET uuid = 'uuid-own', email = 'user_555@nekovo.ru', "
                         "status = 'paid' WHERE chat_id = '555'")
        result = email_link.link(db, '555', 'tat@gmail.com')
        assert (result.outcome, result.source) == ('admin', 'ext_aa')
        assert row(db, '555')['uuid'] == 'uuid-own'
        assert row(db, 'ext_aa')['uuid'] == 'uuid-paid'
        assert row(db, '555')['contact_email'] is None

    def test_a_demo_mail_row_does_not_replace_a_key(self, db):
        seed(db, 'ext_d', uuid='uuid-d', email='user_d@nekovo.ru', status='demo',
             contact_email='t@x.io')
        seed(db, '555', uuid='uuid-own', email='user_555@nekovo.ru', status='demo')
        assert email_link.link(db, '555', 't@x.io').outcome == 'admin'

    def test_several_holders_go_to_the_admin(self, db):
        seed(db, 'ext_1', status='new', contact_email='t@x.io')
        seed(db, 'ext_2', status='new', contact_email='T@X.io')
        seed(db, '555', status='new')
        assert email_link.link(db, '555', 't@x.io').outcome == 'admin'
        assert row(db, '555')['contact_email'] is None


class TestFlow:
    """The buttons and messages end to end: «📧 Я регистрировался через
    почту», the address, a wrong code, the right one."""

    @pytest.fixture
    def flow(self, db):
        from bot.handlers.messages import (
            EMAIL_PURPOSE, PENDING_EMAIL, PENDING_EMAIL_CODE, MessageHandler)
        for pending in (PENDING_EMAIL, PENDING_EMAIL_CODE, EMAIL_PURPOSE):
            pending.clear()
        mailer, panel = Mailer(), Panel()
        bot = Bot(mailer, panel)
        handler = MessageHandler.__new__(MessageHandler)
        handler.db, handler.bot, handler.config = db, bot, CONFIG

        def mine():
            return [m for m in bot.sent if str(m.get('chat_id')) == '555'][-1]

        def say(text):
            handler.handle({'message': {'text': text, 'chat': {'id': 555},
                                        'from': {'id': 555}}})
            return mine()

        def press(data):
            from bot.handlers.callbacks.user import EmailCodeFlowHandler, EmailLoginHandler
            cls = EmailLoginHandler if data == 'email_login' else EmailCodeFlowHandler
            h = cls.__new__(cls)
            h.db, h.bot, h.config = db, bot, CONFIG
            h.handle({}, '555', '555', data=data)
            return mine()

        yield SimpleNamespace(say=say, press=press, mailer=mailer, panel=panel, bot=bot)
        for pending in (PENDING_EMAIL, PENDING_EMAIL_CODE, EMAIL_PURPOSE):
            pending.clear()

    def test_tatiana_from_the_welcome_button(self, db, flow):
        tatiana(db)
        assert 'регистрировались' in flow.press('email_login')['text']
        reply = flow.say('Tat@Gmail.com')
        assert flow.mailer.letters[-1][0] == 'tat@gmail.com'
        assert 't***@gmail.com' in reply['text']
        code = flow.mailer.last_code()
        assert 'Осталось попыток: 4' in flow.say('000000' if code != '000000' else '999999')['text']
        reply = flow.say(code[:3] + ' ' + code[3:])
        assert 'оплачена до 30.03.2028' in reply['text']
        assert reply['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'my_key'
        assert row(db, '555')['uuid'] == 'uuid-paid'
        admin = [m for m in flow.bot.sent if m.get('chat_id') == '900']
        assert len(admin) == 1 and 'объединены' in admin[0]['text'] and 'ext_aa' in admin[0]['text']
        assert 'Оплачено до: 30.03.2028' in admin[0]['text']
        assert flow.panel.comments == [('user_ext@nekovo.ru', 'tat@gmail.com')]
        assert flow.panel.removed == []

    def test_a_replaced_demo_client_leaves_the_panel(self, db, flow):
        tatiana(db)
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("UPDATE users SET uuid = 'uuid-demo', email = 'user_555@nekovo.ru', "
                         "status = 'demo' WHERE chat_id = '555'")
        flow.press('email_login')
        flow.say('tat@gmail.com')
        flow.say(flow.mailer.last_code())
        assert flow.panel.removed == ['user_555@nekovo.ru']

    def test_no_subscription_on_it_offers_a_demo(self, db, flow):
        seed(db, '555', status='new')
        flow.press('email_login')
        flow.say('new@x.io')
        reply = flow.say(flow.mailer.last_code())
        assert 'подписки на неё у нас нет' in reply['text']
        assert reply['reply_markup']['inline_keyboard'][0][0]['callback_data'] == 'request_demo'
        assert row(db, '555')['contact_email'] == 'new@x.io'

    def test_a_conflict_reaches_the_admin_and_changes_nothing(self, db, flow):
        tatiana(db)
        with sqlite3.connect(db.db_path) as conn:
            conn.execute("UPDATE users SET uuid = 'uuid-own', email = 'user_555@nekovo.ru', "
                         "status = 'paid' WHERE chat_id = '555'")
        flow.press('email_login')
        flow.say('tat@gmail.com')
        assert 'администратору' in flow.say(flow.mailer.last_code())['text']
        assert any('Свяжите вручную' in m['text'] for m in flow.bot.sent if m.get('chat_id') == '900')
        assert row(db, '555')['uuid'] == 'uuid-own'

    def test_the_add_button_saves_after_the_code(self, db, flow):
        from bot.handlers.callbacks.user import arm_email_prompt
        seed(db, '555', status='demo', uuid='uuid-own', email='user_555@nekovo.ru')
        arm_email_prompt('555', 'add')
        flow.say('me@x.io')
        assert row(db, '555')['contact_email'] is None
        assert 'сохранена' in flow.say(flow.mailer.last_code())['text']
        assert row(db, '555')['contact_email'] == 'me@x.io'

    def test_not_digits_while_a_code_is_awaited(self, db, flow):
        seed(db, '555', status='new')
        flow.press('email_login')
        flow.say('t@x.io')
        assert 'шесть цифр' in flow.say('привет')['text']

    def test_send_again_and_cancel(self, db, flow):
        from bot.handlers.messages import PENDING_EMAIL_CODE
        seed(db, '555', status='new')
        flow.press('email_login')
        flow.say('t@x.io')
        first = flow.mailer.last_code()
        flow.press('email_resend')
        assert len(flow.mailer.letters) == 2
        assert flow.mailer.letters[-1][0] == 't@x.io'
        assert flow.press('email_cancel')['text'] == 'Отменено.'
        assert '555' not in PENDING_EMAIL_CODE
        assert first is not None
