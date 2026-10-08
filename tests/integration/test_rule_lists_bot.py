"""FlClash rule lists from the bot — /list, «📝 Другой сайт», the
complaint cards and E9 end to end, on a REAL sqlite bot.db
(IMPROVEMENT_PLAN E2/E9).

  /list …                 the operator's view and edits, one admin_actions
                          row per change, answered in the source topic
  🆘 → 📝 Другой сайт     arms a prompt; the next plain text names the site;
                          it is queued (rule_list_queue) — never written to
                          user_failure_reports (DPIMonitor R5 reads that as
                          "a protocol fails for this ASN")
  card buttons            «➕ в blocked-recent» / «✖ игнор», SUPER_ADMIN only:
                          callback data is client-supplied, anyone can send
                          ``rlq:add:1`` from their own chat with the bot
  E9                      the second distinct user within 24 h adds the
                          domain itself and the topic gets the undo command
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot.core.database import Database
from bot.handlers.admin import AdminHandler
from bot.handlers.admin.base import ADMIN_HELP_TEXT, AdminHandlerBase
from bot.handlers.callbacks.dispatcher import CallbackDispatcher
from bot.handlers.callbacks.rule_lists import ReportSiteHandler, RuleListQueueHandler
from bot.handlers.callbacks.user import EmailPromptHandler, MyKeyAnswerHandler
from bot.handlers.messages import (
    PENDING_EMAIL, PENDING_SITE, PENDING_SITE_TTL, MessageHandler,
)
from bot.models.user import User
from bot.services import rule_lists as rl

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

ADMIN = '1652899'
GROUP = '-1003686477257'
TOPIC_SUPPORT = 17
UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'


@pytest.fixture(autouse=True)
def _clean_pending():
    PENDING_SITE.clear()
    PENDING_EMAIL.clear()
    yield
    PENDING_SITE.clear()
    PENDING_EMAIL.clear()


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def config():
    return SimpleNamespace(
        BOT_TOKEN='test_token', SUPER_ADMIN_ID=ADMIN, FORUM_ENABLED=True,
        FORUM_GROUP_ID=GROUP, TOPIC_SUPPORT=TOPIC_SUPPORT, TOPIC_REQUESTS=15,
        WEBAPP_URL='https://dash.example.com',
        is_admin=lambda uid: str(uid) == ADMIN,
    )


@pytest.fixture
def bot():
    b = Mock()
    b.get_chat_member.return_value = {'status': 'member'}
    return b


def seed_user(db, chat_id='111', username='alice', status='demo', uuid='auto', **kw):
    # one uuid per user: users.uuid is UNIQUE and save_user is an
    # INSERT OR REPLACE — a shared uuid silently deletes the earlier row
    if uuid == 'auto':
        uuid = f"{UUID[:24]}{int(chat_id):012d}"
    db.save_user(User(chat_id=chat_id, username=username, status=status, uuid=uuid,
                      email=f'{chat_id}@x', lang=kw.pop('lang', 'ru'), **kw))


def audit(db) -> list:
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT admin_id, action, target_id, details FROM admin_actions "
            "ORDER BY id").fetchall()]


def queue(db) -> dict:
    return rl.parse_queue(db.get_setting(rl.QUEUE_KEY))


def lists(db) -> dict:
    return rl.load_lists(db)


def sends(bot, **match) -> list:
    """kwargs of every send_message call matching all of ``match``."""
    out = []
    for call in bot.send_message.call_args_list:
        kw = dict(call.kwargs)
        if all(kw.get(k) == v for k, v in match.items()):
            out.append(kw)
    return out


# ---------- /list ----------

class TestListCommand:

    @pytest.fixture
    def admin(self, bot, db, config):
        return AdminHandler(bot, db, config)

    def cmd(self, admin, text, user_id=ADMIN, thread=42) -> dict:
        update = {'message': {'text': text, 'chat': {'id': int(GROUP)},
                              'from': {'id': int(user_id)},
                              'message_thread_id': thread}}
        admin.bot.send_message.reset_mock()
        assert admin.can_handle(update)
        admin.handle(update)
        admin.bot.send_message.assert_called_once()
        return admin.bot.send_message.call_args.kwargs

    def test_registered_and_advertised(self):
        assert AdminHandlerBase.ADMIN_COMMANDS['/list'] == 'show_rule_lists'
        assert callable(getattr(AdminHandler, 'show_rule_lists'))
        assert '<code>/list</code>' in ADMIN_HELP_TEXT

    def test_reply_lands_in_the_source_topic(self, admin):
        kw = self.cmd(admin, '/list', thread=42)
        assert kw['chat_id'] == GROUP and kw['message_thread_id'] == 42
        assert kw['parse_mode'] == 'HTML'

    def test_overview(self, admin, db):
        rl.add_entry(db, 'blocked-recent', 'a.com', actor=ADMIN)
        rl.add_entry(db, 'blocked-recent', 'b.com', actor=rl.AUTO_ACTOR)
        rl.add_entry(db, 'ru-direct-ip', '1.2.3.0/24', actor=ADMIN)
        rl.record_complaint(db, 'queued.com', '111')
        text = self.cmd(admin, '/list')['text']
        assert '<b>blocked-recent</b> — заблокировано недавно → VPN: 2 (авто: 1)' in text
        assert '<b>always-proxy</b> — всегда через VPN: 0' in text
        assert '<b>ru-direct-ip</b> — RU напрямую (IP/CIDR, no-resolve): 1' in text
        assert 'https://dash.example.com/lists/clash/&lt;список&gt;.yaml' in text
        assert 'ждут решения: 1' in text and '<code>queued.com</code> — 1 юз.' in text
        assert 'авто в blocked-recent: 2+ разных юзеров за 24 ч' in text

    def test_overview_warns_about_a_broken_store(self, admin, db):
        db.set_setting(rl.SETTING_KEY, '{oops')
        assert '⚠️ rule_lists битый JSON' in self.cmd(admin, '/list')['text']

    def test_add_and_rm_roundtrip(self, admin, db):
        text = self.cmd(admin, '/list add blocked-recent https://www.Rutracker.org/forum')['text']
        assert '✅ <b>blocked-recent</b>: + <code>rutracker.org</code>' in text
        assert 'FlClash подтянет' in text
        assert list(lists(db)['blocked-recent']) == ['rutracker.org']
        assert audit(db) == [(ADMIN, 'rule_list_add', 'blocked-recent:rutracker.org', '/list add')]

        text = self.cmd(admin, '/list rm blocked-recent rutracker.org')['text']
        assert '🗑 <b>blocked-recent</b>: − <code>rutracker.org</code>' in text
        assert 'авто не вернёт его 30 дн.' in text
        assert lists(db)['blocked-recent'] == {}
        assert audit(db)[-1] == (ADMIN, 'rule_list_rm', 'blocked-recent:rutracker.org', '/list rm')
        assert 'rutracker.org' in queue(db)['ignored']

    def test_add_several_at_once(self, admin, db):
        text = self.cmd(admin, '/list add ru-direct a.ru b.ru a.ru sub.b.ru')['text']
        assert text.count('✅') == 2
        assert 'ℹ️ <code>a.ru</code> уже в ru-direct' in text
        assert 'уже покрыт <code>b.ru</code>' in text
        assert list(lists(db)['ru-direct']) == ['a.ru', 'b.ru']
        assert [r[1] for r in audit(db)] == ['rule_list_add', 'rule_list_add']

    def test_invalid_entry_writes_nothing(self, admin, db):
        text = self.cmd(admin, '/list add ru-direct-ip 0.0.0.0/0')['text']
        assert text.startswith('❌') and 'слишком широкая' in text
        assert db.get_setting(rl.SETTING_KEY) is None and audit(db) == []

    def test_unknown_list_and_usage(self, admin, db):
        assert '❌ нет такого списка' in self.cmd(admin, '/list add blocked x.com')['text']
        assert '/list show' in self.cmd(admin, '/list frobnicate')['text']
        assert '/list show' in self.cmd(admin, '/list add blocked-recent')['text']
        assert audit(db) == []

    def test_direct_entry_under_a_vpn_one_is_flagged(self, admin, db):
        rl.add_entry(db, 'blocked-recent', 'example.com', actor=ADMIN)
        text = self.cmd(admin, '/list add ru-direct example.com')['text']
        assert '⚠️' in text and 'VPN-списки стоят выше' in text

    def test_show(self, admin, db):
        rl.add_entry(db, 'blocked-recent', 'a.com', actor=ADMIN, note='/list add')
        rl.add_entry(db, 'blocked-recent', 'b.com', actor=rl.AUTO_ACTOR, note='2 жалобы')
        text = self.cmd(admin, '/list show blocked-recent')['text']
        assert '1. <code>a.com</code> — админ 1652899' in text
        assert '2. <code>b.com</code> — авто' in text and '2 жалобы' in text
        assert '<i>пусто</i>' in self.cmd(admin, '/list ru-direct')['text']

    def test_auto_threshold(self, admin, db):
        assert '2+ разных юзеров' in self.cmd(admin, '/list auto')['text']
        assert '3+ разных юзеров' in self.cmd(admin, '/list auto 3')['text']
        assert db.get_setting(rl.THRESHOLD_KEY) == '3'
        assert '⏸' in self.cmd(admin, '/list auto off')['text']
        assert db.get_setting(rl.THRESHOLD_KEY) == '0'
        assert '❌' in self.cmd(admin, '/list auto many')['text']
        assert [r[1:] for r in audit(db)] == [
            ('rule_list_auto_threshold', 'rule_list_auto_threshold', '3'),
            ('rule_list_auto_threshold', 'rule_list_auto_threshold', '0')]

    def test_audit_names_the_admin_who_typed_it(self, admin, db):
        admin.bot.get_chat_member.return_value = {'status': 'administrator'}
        self.cmd(admin, '/list add always-proxy example.com', user_id='777')
        self.cmd(admin, '/list auto 4', user_id='777')
        assert [r[0] for r in audit(db)] == ['777', '777']
        assert lists(db)['always-proxy']['example.com']['by'] == '777'

    def test_non_admin_is_not_routed(self, admin):
        update = {'message': {'text': '/list add blocked-recent a.com',
                              'chat': {'id': int(GROUP)}, 'from': {'id': 111}}}
        assert not admin.can_handle(update)


# ---------- 🆘 → 📝 Другой сайт → the queue ----------

def press(handler_cls, bot, db, config, data, user_id='111', chat_id=None, thread=None):
    h = handler_cls(bot, db, config)
    msg = {'message_id': 900, 'chat': {'id': chat_id or user_id}, 'text': 'card'}
    if thread is not None:
        msg['message_thread_id'] = thread
    update = {'callback_query': {'id': 'cb', 'from': {'id': user_id}, 'data': data,
                                 'message': msg}}
    h.handle(update, str(chat_id or user_id), str(user_id), data=data)
    return h


def say(bot, db, config, text, chat_id='111'):
    update = {'message': {'text': text, 'chat': {'id': int(chat_id), 'type': 'private'},
                          'from': {'id': int(chat_id)}}}
    MessageHandler(bot, db, config).handle(update)


def report_site(bot, db, config, text, chat_id='111'):
    press(ReportSiteHandler, bot, db, config, 'report_site', user_id=chat_id)
    bot.send_message.reset_mock()
    say(bot, db, config, text, chat_id=chat_id)


def failure_reports(db) -> int:
    with db._connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM user_failure_reports").fetchone()[0]


class TestSiteReport:

    def test_picker_offers_the_site_button(self, bot, db, config):
        h = MyKeyAnswerHandler(bot, db, config)
        h._show_category_picker('111', 'ru')
        rows = bot.send_message.call_args.kwargs['reply_markup']['inline_keyboard']
        assert rows[-1] == [{'text': '📝 Другой сайт', 'callback_data': 'report_site'}]
        h._show_category_picker('111', 'en')
        rows = bot.send_message.call_args.kwargs['reply_markup']['inline_keyboard']
        assert rows[-1][0]['text'] == '📝 Another website'

    def test_tap_arms_the_prompt(self, bot, db, config):
        seed_user(db)
        PENDING_EMAIL['111'] = time.time()
        press(ReportSiteHandler, bot, db, config, 'report_site', thread=None)
        assert '111' in PENDING_SITE and '111' not in PENDING_EMAIL
        kw = bot.send_message.call_args.kwargs
        assert kw['chat_id'] == '111' and 'Какой сайт не открывается' in kw['text']

    def test_no_key_no_prompt(self, bot, db, config):
        seed_user(db, uuid=None)
        press(ReportSiteHandler, bot, db, config, 'report_site')
        assert PENDING_SITE == {}
        assert 'Сначала получите ключ' in bot.send_message.call_args.kwargs['text']

    def test_email_prompt_disarms_the_site_prompt(self, bot, db, config):
        seed_user(db)
        PENDING_SITE['111'] = time.time()
        EmailPromptHandler(bot, db, config).handle({'callback_query': {}}, '111', '111')
        assert '111' in PENDING_EMAIL and '111' not in PENDING_SITE

    def test_message_is_queued_and_carded(self, bot, db, config):
        seed_user(db, last_asn='AS31133', last_country='RU')
        report_site(bot, db, config, 'не открывается https://rutracker.org/forum')
        [it] = queue(db)['items']
        assert (it['id'], it['domain'], it['chat_id'], it['status']) == (
            1, 'rutracker.org', '111', 'pending')
        [card] = sends(bot, chat_id=GROUP)
        assert card['message_thread_id'] == TOPIC_SUPPORT
        assert '<code>rutracker.org</code>' in card['text'] and '@alice' in card['text']
        assert 'AS31133 RU' in card['text'] and 'авто при 2' in card['text']
        assert card['reply_markup'] == {'inline_keyboard': [[
            {'text': '➕ в blocked-recent', 'callback_data': 'rlq:add:1'},
            {'text': '✖ игнор', 'callback_data': 'rlq:ign:1'}]]}
        [reply] = sends(bot, chat_id='111')
        assert '✅ <code>rutracker.org</code> — передали' in reply['text']
        assert 'Hiddify их не применяет' in reply['text']
        assert PENDING_SITE == {}
        assert failure_reports(db) == 0          # never the R5 heatmap
        assert lists(db)['blocked-recent'] == {}

    def test_up_to_three_sites_per_message(self, bot, db, config):
        seed_user(db)
        report_site(bot, db, config, 'a.com b.org c.net d.io')
        assert [it['domain'] for it in queue(db)['items']] == ['a.com', 'b.org', 'c.net']
        assert len(sends(bot, chat_id=GROUP)) == 3

    def test_no_address_keeps_the_prompt(self, bot, db, config):
        seed_user(db)
        report_site(bot, db, config, 'привет, ничего не грузит')
        assert 'Не нашёл адрес сайта' in sends(bot, chat_id='111')[0]['text']
        assert '111' in PENDING_SITE and queue(db)['items'] == []
        say(bot, db, config, 'rutracker.org')
        assert [it['domain'] for it in queue(db)['items']] == ['rutracker.org']

    def test_expired_prompt_passes_through(self, bot, db, config):
        seed_user(db)
        PENDING_SITE['111'] = time.time() - PENDING_SITE_TTL - 5
        say(bot, db, config, 'rutracker.org')
        assert queue(db)['items'] == [] and PENDING_SITE == {}
        assert 'не понимаю' in bot.send_message.call_args.kwargs['text']

    def test_duplicate_is_acknowledged_once(self, bot, db, config):
        seed_user(db)
        report_site(bot, db, config, 'rutracker.org')
        report_site(bot, db, config, 'https://rutracker.org/')
        assert 'ты уже сообщал' in sends(bot, chat_id='111')[0]['text']
        assert sends(bot, chat_id=GROUP) == [] and len(queue(db)['items']) == 1

    def test_ru_zone_card_says_auto_will_not_touch_it(self, bot, db, config):
        seed_user(db)
        report_site(bot, db, config, 'gosuslugi.ru')
        assert '⚠️ авто не добавит: RU-зона' in sends(bot, chat_id=GROUP)[0]['text']

    def test_pm_fallback_without_a_forum(self, bot, db, config):
        config.FORUM_ENABLED = False
        seed_user(db)
        report_site(bot, db, config, 'rutracker.org')
        [card] = sends(bot, chat_id=ADMIN)
        assert 'message_thread_id' not in card and 'rlq:add:1' in json.dumps(card)


# ---------- the card's buttons ----------

class TestQueueButtons:

    @pytest.fixture
    def carded(self, bot, db, config):
        seed_user(db)
        report_site(bot, db, config, 'rutracker.org')
        bot.reset_mock()
        return db

    def tap(self, bot, db, config, data, user_id=ADMIN):
        return press(RuleListQueueHandler, bot, db, config, data, user_id=user_id,
                     chat_id=GROUP, thread=TOPIC_SUPPORT)

    def test_add(self, bot, carded, config):
        db = carded
        self.tap(bot, db, config, 'rlq:add:1')
        assert list(lists(db)['blocked-recent']) == ['rutracker.org']
        assert audit(db) == [(ADMIN, 'rule_list_add', 'blocked-recent:rutracker.org', 'жалоба #1')]
        assert queue(db)['items'][0]['status'] == 'added'
        kw = bot.edit_message_text.call_args.kwargs
        assert kw['chat_id'] == GROUP and kw['message_id'] == 900
        assert kw['reply_markup'] == {'inline_keyboard': []}
        assert '✅ добавлен в blocked-recent' in kw['text']
        assert '/list rm blocked-recent rutracker.org' in kw['text']

    def test_add_twice_is_harmless(self, bot, carded, config):
        self.tap(bot, carded, config, 'rlq:add:1')
        self.tap(bot, carded, config, 'rlq:add:1')
        assert 'ℹ️ уже в blocked-recent' in bot.edit_message_text.call_args.kwargs['text']
        assert len(audit(carded)) == 1

    def test_ignore_holds_auto_off(self, bot, carded, config, db):
        self.tap(bot, carded, config, 'rlq:ign:1')
        q = queue(carded)
        assert q['items'][0]['status'] == 'ignored' and 'rutracker.org' in q['ignored']
        assert audit(carded) == [(ADMIN, 'rule_list_ignore', 'blocked-recent:rutracker.org',
                                  'жалоба #1')]
        assert '✖ проигнорирован' in bot.edit_message_text.call_args.kwargs['text']
        # two fresh users later: the admin's "no" still stands
        for cid in ('222', '333'):
            seed_user(carded, chat_id=cid, username=f'u{cid}')
            report_site(bot, carded, config, 'rutracker.org', chat_id=cid)
        assert [it['chat_id'] for it in queue(carded)['items']
                if it['status'] == 'pending'] == ['222', '333']
        assert lists(carded)['blocked-recent'] == {}
        assert 'админ отклонил' in sends(bot, chat_id=GROUP)[-1]['text']

    @pytest.mark.parametrize('data', ['rlq:add:1', 'rlq:ign:1'])
    def test_non_admin_is_refused(self, bot, carded, config, data):
        self.tap(bot, carded, config, data, user_id='111')
        assert lists(carded)['blocked-recent'] == {} and audit(carded) == []
        assert queue(carded)['items'][0]['status'] == 'pending'
        bot.send_message.assert_called_once_with(chat_id='111', text='❌ No permission.')
        bot.edit_message_text.assert_not_called()

    def test_stale_card(self, bot, carded, config):
        self.tap(bot, carded, config, 'rlq:add:999')
        assert 'жалоба #999 не найдена' in bot.edit_message_text.call_args.kwargs['text']
        assert lists(carded)['blocked-recent'] == {}

    @pytest.mark.parametrize('data', ['rlq:add:x', 'rlq:drop:1', 'rlq:add', 'rlq:add:1:2'])
    def test_malformed_data_does_nothing(self, bot, carded, config, data):
        self.tap(bot, carded, config, data)
        assert lists(carded)['blocked-recent'] == {}
        bot.edit_message_text.assert_not_called()

    @pytest.mark.parametrize('data,cls', [
        ('report_site', 'ReportSiteHandler'), ('rlq:add:1', 'RuleListQueueHandler'),
        ('rlq:ign:7', 'RuleListQueueHandler'),
    ])
    def test_dispatcher_routes(self, bot, db, config, data, cls):
        handlers = CallbackDispatcher(bot, db, config).handlers
        first = next(h for h in handlers if h.can_handle(data))
        assert type(first).__name__ == cls


# ---------- E9 end to end ----------

class TestAutoEndToEnd:

    def test_second_user_adds_it_and_the_topic_gets_the_undo(self, bot, db, config):
        for cid in ('111', '222'):
            seed_user(db, chat_id=cid, username=f'u{cid}')
        report_site(bot, db, config, 'rutracker.org', chat_id='111')
        assert [it['chat_id'] for it in queue(db)['items']] == ['111']
        assert lists(db)['blocked-recent'] == {}
        report_site(bot, db, config, 'https://rutracker.org/forum', chat_id='222')
        meta = lists(db)['blocked-recent']['rutracker.org']
        assert meta['by'] == 'rule_lists'
        row = audit(db)[-1]
        assert row[:3] == ('rule_lists', 'auto_add', 'blocked-recent:rutracker.org')
        assert '2 жалобы от разных юзеров за 24 ч (порог 2)' in row[3]
        [note] = sends(bot, chat_id=GROUP)
        assert note['message_thread_id'] == TOPIC_SUPPORT and 'reply_markup' not in note
        assert '<code>/list rm blocked-recent rutracker.org</code>' in note['text']
        assert 'пускаем через VPN' in sends(bot, chat_id='222')[0]['text']
        assert "'+.rutracker.org'" in rl.serve_provider(db, 'blocked-recent')[1]
        # the undo from the topic message really undoes it
        admin = AdminHandler(bot, db, config)
        admin.handle({'message': {'text': '/list rm blocked-recent rutracker.org',
                                  'chat': {'id': int(GROUP)}, 'from': {'id': int(ADMIN)}}})
        assert lists(db)['blocked-recent'] == {}

    def test_one_user_cannot_do_it_alone(self, bot, db, config):
        seed_user(db)
        for text in ('rutracker.org', 'rutracker.org', 'www.rutracker.org'):
            report_site(bot, db, config, text)
        assert [it['chat_id'] for it in queue(db)['items']] == ['111']
        assert lists(db)['blocked-recent'] == {}

    def test_threshold_three(self, bot, db, config):
        db.set_setting(rl.THRESHOLD_KEY, '3')
        for cid in ('111', '222'):
            seed_user(db, chat_id=cid, username=f'u{cid}')
            report_site(bot, db, config, 'rutracker.org', chat_id=cid)
        assert lists(db)['blocked-recent'] == {}
        assert 'авто при 3' in sends(bot, chat_id=GROUP)[0]['text']
        seed_user(db, chat_id='333', username='u333')
        report_site(bot, db, config, 'rutracker.org', chat_id='333')
        assert 'rutracker.org' in lists(db)['blocked-recent']
