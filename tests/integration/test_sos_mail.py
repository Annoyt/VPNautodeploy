"""SOS by mail (IMPROVEMENT_PLAN E24).

A letter from a key holder's ``contact_email`` (demo / paid /
support_topic) whose subject or body says sos / help / не работает /
не подключается gets the emergency subscription back by mail, a report
in the support topic and a ``user_failure_reports`` row (target
``sos_mail``) — at most once an hour per address. Everything else (an
unknown sender, no trigger word, a quote of our own letter) goes down the
key-request path exactly as before.

Level 2: the real Database (users / email_requests / user_failure_reports
/ app_settings) behind MailIntakeService; IMAP is a fake that serves the
header fetch and the body fetch, SMTP is a mock mailer.
"""

from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from bot.core.database import Database
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
from bot.models.user import User
from bot.services.email_service import _key_email
from bot.services.mail_intake import MailIntakeService
from bot.services.sos import (
    letter_text, mail_sos_letter, match_sos_keywords, strip_quoted,
)
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

ADDR = 'ivan@mail.ru'
CID = '52291265'
UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
GROUP, TOPIC_SUPPORT, TOPIC_REQUESTS = -1001234, 17, 15


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def make_config(**over):
    cfg = dict(
        SMTP_USER='svc@gmail.com', SMTP_PASSWORD='app-pw', IMAP_HOST='imap.test',
        MAIL_INTAKE_ENABLED='1', FORUM_ENABLED=True, FORUM_GROUP_ID=GROUP,
        TOPIC_REQUESTS=TOPIC_REQUESTS, TOPIC_SUPPORT=TOPIC_SUPPORT,
        SUPER_ADMIN_ID='1652899', DEMO_TRAFFIC_GB=10,
        BOT_TOKEN='test_token', WEBAPP_URL='https://dash.example.com',
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='pbk', SNI_VALUE='www.bing.com', SID_VALUE='01',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/p', WS_SNI='cdn.example.com',
        HY2T_PORT='', AGENT_BACKEND='hermes', HERMES_URL='',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


@pytest.fixture(autouse=True)
def _panel(monkeypatch):
    MK._panel_skip_until = 0.0
    monkeypatch.setattr(MK, '_fetch_panel_last_online_ms', lambda self, email: 0)
    yield
    MK._panel_skip_until = 0.0


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / 'bot.db'))
    d.set_setting('mail_intake_last_uid', '800')
    return d


@pytest.fixture
def mailer():
    m = Mock()
    m.send_notice.return_value = True
    return m


def add_user(db, *, status='paid', contact_email=ADDR, uuid=UUID, lang='ru',
             chat_id=CID):
    db._users.save(User(chat_id=chat_id, username='ivan', status=status, uuid=uuid,
                        email=f'user_ivan_{chat_id}@nekovo.ru' if uuid else None,
                        lang=lang, contact_email=contact_email,
                        last_country='RU', last_asn='AS31133'))


def letter(subject='Вопрос', body='', *, sender=ADDR, msg_id='<m1@x>', html=None):
    if html is not None:
        msg = MIMEMultipart('alternative')
        msg.attach(MIMEText(html, 'html', 'utf-8'))
    else:
        msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = sender
    msg['Subject'] = subject
    msg['Message-ID'] = msg_id
    return msg.as_bytes()


def imap(messages):
    """Fake IMAP: header fetches get the header block, body fetches the
    whole letter — the shape imaplib returns."""
    m = MagicMock()
    m.status.return_value = ('OK', [b'"INBOX" (UIDNEXT 900)'])

    def _uid(cmd, *args):
        if cmd == 'SEARCH':
            return ('OK', [' '.join(str(u) for u in sorted(messages)).encode()])
        if cmd == 'FETCH':
            uid, what = int(args[0]), args[1]
            raw = messages.get(uid)
            if raw is None:
                return ('OK', [None])
            if 'HEADER.FIELDS' in what:
                return ('OK', [(b'HEADER', raw.split(b'\n\n', 1)[0] + b'\n\n')])
            return ('OK', [(b'BODY', raw), b')'])
        raise AssertionError(cmd)

    m.uid.side_effect = _uid
    return m


def poll(db, mailer, messages, config=None, bot=None):
    bot = bot or Mock()
    bot.services = {'email': mailer}
    svc = MailIntakeService(bot, db, config or make_config())
    svc._connect = Mock(return_value=imap(messages))
    created = svc.poll_once()
    return svc, created


def posts(bot, topic):
    return [c.kwargs for c in bot.send_message.call_args_list
            if c.kwargs.get('message_thread_id') == topic]


def rows(db, sql, *args):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]


class TestSosLetter:

    def test_key_holder_with_trigger_word_gets_the_emergency_links(self, db, mailer):
        add_user(db)
        svc, created = poll(db, mailer, {800: letter('SOS', 'всё пропало')})
        assert created == 0                      # not a key request

        (call,) = mailer.send_notice.call_args_list
        to, subject, body = call.args
        assert to == ADDR and subject == 'NekoVPN: аварийная подписка'
        assert call.kwargs['in_reply_to'] == '<m1@x>'
        base = SubscriptionService(make_config()).build_subscription_url(
            db.get_user(CID))
        assert f'{base}?emergency=1' in body
        assert f'{base}?format=clash&emergency=1' in body
        assert 'ОБНОВИТЕ ОСНОВНУЮ ПОДПИСКУ' in body and 'ЧТО СЕЙЧАС РАБОТАЕТ' in body
        # plain text in the «вы» of our letters, not the bot's «ты»
        assert '📶 Ваша сеть (AS31133): за последний час данных от других нет.' in body
        assert 'Твоя' not in body and '<b>' not in body and '&amp;' not in body

        (report,) = posts(svc.bot, TOPIC_SUPPORT)
        assert report['chat_id'] == GROUP
        assert '🆘 <b>SOS #' in report['text'] and '· почта' in report['text']
        assert f'Письмо от <code>{ADDR}</code>: «SOS»' in report['text']
        assert 'Ответ с аварийной подпиской: отправлен' in report['text']
        assert not posts(svc.bot, TOPIC_REQUESTS)          # no key-request card
        assert rows(db, "SELECT chat_id, target_domain FROM user_failure_reports") == \
            [(CID, 'sos_mail')]
        assert rows(db, "SELECT from_addr, status, known_user FROM email_requests") == \
            [(ADDR, 'sos', CID)]

    @pytest.mark.parametrize('subject,body', [
        ('Re: ключ', 'Здравствуйте, у меня VPN не работает с утра'),
        ('вопрос', 'НЕ ПОДКЛЮЧАЕТСЯ!!!'),
        ('Help', ''),
        ('', 'please help, nothing loads'),
        ('сос', 'не\nподключается'),
    ])
    def test_trigger_in_subject_or_body(self, db, mailer, subject, body):
        add_user(db)
        poll(db, mailer, {800: letter(subject, body)})
        assert mailer.send_notice.call_count == 1

    def test_html_only_letter(self, db, mailer):
        add_user(db)
        poll(db, mailer, {800: letter('?', html='<p>Ничего <b>не работает</b></p>')})
        assert mailer.send_notice.call_count == 1

    def test_address_matches_case_insensitively(self, db, mailer):
        add_user(db, contact_email=' Ivan@Mail.RU ')
        poll(db, mailer, {800: letter('sos', sender='IVAN@mail.ru')})
        assert mailer.send_notice.call_count == 1

    def test_english_user_gets_the_english_letter(self, db, mailer):
        add_user(db, lang='en')
        poll(db, mailer, {800: letter('help')})
        assert mailer.send_notice.call_args.args[1] == 'NekoVPN: emergency subscription'

    def test_once_an_hour_per_address(self, db, mailer):
        add_user(db)
        svc, _ = poll(db, mailer, {800: letter('SOS', msg_id='<a@x>')})
        svc2, created = poll(db, mailer, {801: letter('SOS!!', msg_id='<b@x>')})
        assert mailer.send_notice.call_count == 1
        assert created == 0 and not svc2.bot.send_message.called
        assert len(rows(db, "SELECT id FROM user_failure_reports")) == 1
        # 59 min later: still quiet; 61 min: answered again
        with db._connect() as conn:
            conn.execute("UPDATE email_requests SET ts = ?",
                         ((utcnow() - timedelta(minutes=59)).strftime('%Y-%m-%d %H:%M:%S'),))
        poll(db, mailer, {802: letter('SOS', msg_id='<c@x>')})
        assert mailer.send_notice.call_count == 1
        with db._connect() as conn:
            conn.execute("UPDATE email_requests SET ts = ?",
                         ((utcnow() - timedelta(minutes=61)).strftime('%Y-%m-%d %H:%M:%S'),))
        poll(db, mailer, {803: letter('SOS', msg_id='<d@x>')})
        assert mailer.send_notice.call_count == 2

    def test_same_message_id_is_not_answered_twice(self, db, mailer):
        add_user(db)
        poll(db, mailer, {800: letter('SOS', msg_id='<same@x>')})
        with db._connect() as conn:
            conn.execute("UPDATE email_requests SET ts = '2000-01-01 00:00:00'")
        poll(db, mailer, {801: letter('SOS', msg_id='<same@x>')})
        assert mailer.send_notice.call_count == 1

    def test_smtp_failure_is_reported_to_the_topic(self, db, mailer):
        add_user(db)
        mailer.send_notice.return_value = False
        svc, _ = poll(db, mailer, {800: letter('SOS')})
        assert 'не ушёл (SMTP)' in posts(svc.bot, TOPIC_SUPPORT)[0]['text']
        assert rows(db, "SELECT target_domain FROM user_failure_reports") == [('sos_mail',)]

    def test_pending_key_request_does_not_swallow_an_sos(self, db, mailer):
        add_user(db)
        poll(db, mailer, {800: letter('хочу ключ', msg_id='<r@x>')})   # card
        assert rows(db, "SELECT status FROM email_requests") == [('pending',)]
        poll(db, mailer, {801: letter('SOS', msg_id='<s@x>')})
        assert mailer.send_notice.call_count == 1


class TestOldPathUnchanged:

    def test_unknown_sender_with_trigger_gets_a_request_card(self, db, mailer):
        svc, created = poll(db, mailer, {800: letter('SOS', 'не работает',
                                                     sender='stranger@x.ru')})
        assert created == 1
        mailer.send_notice.assert_not_called()
        (card,) = posts(svc.bot, TOPIC_REQUESTS)
        assert 'Заявка на ключ с почты' in card['text']
        assert rows(db, "SELECT status FROM email_requests") == [('pending',)]
        assert not rows(db, "SELECT id FROM user_failure_reports")

    def test_key_holder_without_trigger_gets_the_old_card(self, db, mailer):
        add_user(db)
        svc, created = poll(db, mailer, {800: letter('хочу второй ключ', 'для ноутбука')})
        assert created == 1
        mailer.send_notice.assert_not_called()
        assert 'Уже есть юзер со статусом <b>paid</b>' in \
            posts(svc.bot, TOPIC_REQUESTS)[0]['text']

    @pytest.mark.parametrize('status,uuid', [('banned', UUID), ('pending_demo', None),
                                             ('demo', None)])
    def test_no_active_key_means_the_old_path(self, db, mailer, status, uuid):
        add_user(db, status=status, uuid=uuid)
        _, created = poll(db, mailer, {800: letter('SOS')})
        assert created == 1
        mailer.send_notice.assert_not_called()

    def test_a_quote_of_our_key_letter_is_not_an_sos(self, db, mailer):
        """Our key letter says "Не подключается?" — every reply quotes it."""
        add_user(db)
        _subject, key_body = _key_email('https://x/sub/abc', 'ru')
        quoted = '\n'.join('> ' + ln for ln in key_body.splitlines())
        body = ('Спасибо, всё ок!\n\nпн, 6 окт. 2026 г. в 14:03, NekoVPN '
                '<svc@gmail.com>:\n' + quoted)
        _, created = poll(db, mailer, {800: letter('Re: Ваш ключ', body)})
        assert created == 1
        mailer.send_notice.assert_not_called()

    def test_body_is_fetched_only_for_key_holders(self, db, mailer):
        bot = Mock()
        bot.services = {'email': mailer}
        svc = MailIntakeService(bot, db, make_config())
        fake = imap({800: letter('x', 'не работает', sender='stranger@x.ru')})
        svc._connect = Mock(return_value=fake)
        svc.poll_once()
        fetches = [c.args[2] for c in fake.uid.call_args_list if c.args[0] == 'FETCH']
        assert fetches and all('HEADER.FIELDS' in f for f in fetches)


class TestKeywordsAndQuotes:

    @pytest.mark.parametrize('text', [
        'SOS', 'sos!!!', 'Help!', 'please help me', 'Не работает', 'не\n работает',
        'НЕ ПОДКЛЮЧАЕТСЯ', 'у меня не подключается vpn'])
    def test_triggers(self, text):
        assert match_sos_keywords(text)

    @pytest.mark.parametrize('text', [
        '', 'helpful', 'SOSiska', 'работает', 'подключается', 'S.O.S', 'dispose'])
    def test_not_triggers(self, text):
        assert not match_sos_keywords(text)

    @pytest.mark.parametrize('header', [
        'On Mon, Oct 6, 2026 at 2:03 PM NekoVPN <svc@gmail.com> wrote:',
        'пн, 6 окт. 2026 г. в 14:03, NekoVPN <svc@gmail.com>:',
        '06.10.2026, 14:03, "NekoVPN" <svc@gmail.com>:',
        '-----Original Message-----',
        '-------- Пересылаемое сообщение --------',
        '________________________________',
        'From: NekoVPN <svc@gmail.com>',
    ])
    def test_quote_headers_cut_the_rest(self, header):
        assert strip_quoted(f'мои слова\n{header}\nне работает') == 'мои слова'

    def test_quoted_lines_are_dropped(self):
        assert strip_quoted('ок\n> не работает\n>> help') == 'ок'

    def test_html_quote_blocks_are_dropped(self):
        raw = letter('x', html='<div>спасибо</div><blockquote>Не подключается?</blockquote>'
                               '<div class="gmail_quote">help</div>')
        assert not match_sos_keywords(letter_text(raw))
        assert 'спасибо' in letter_text(raw)

    @pytest.mark.parametrize('lang', ['ru', 'en'])
    def test_our_sos_letter_never_triggers_itself(self, lang):
        """A reply quoting our letter after the hour must not loop."""
        subject, body = mail_sos_letter(
            ('https://x/sub/t?emergency=1', 'https://x/sub/t?format=clash&emergency=1'),
            ['❌ Reality — не отвечает', '✅ Cloudflare (WS) — работает'], lang)
        assert not match_sos_keywords(subject)
        assert not match_sos_keywords(body)
        assert not match_sos_keywords(mail_sos_letter(None, [], lang)[1])
