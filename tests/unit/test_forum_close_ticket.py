"""Killing suite for ForumHandler.handle_close_ticket.

Covers the full close flow: ticket-log compilation into the Solved
topic, media archival, user notification (per language), state-machine
return, topic rename/close ordering, idempotency on stale topics, and
error surfacing. Referenced by the mutation-testing config in
pyproject.toml — before this file, all 176 mutants in
handle_close_ticket were "no tests".
"""

import logging

import pytest
from unittest.mock import MagicMock, patch

from bot.handlers.forum import ForumHandler
from bot.config import Settings, UserState
from bot.models import User

FORUM_ID = '-1001234567890'
TOPIC = 555
SOLVED = 37


def make_handler(user='default', history='default', topic_solved=SOLVED):
    bot = MagicMock()
    bot.send_message.return_value = {'message_id': 1}
    bot.copy_message.return_value = {'message_id': 2}

    if user == 'default':
        user = User(chat_id='777', username='alice', status=UserState.SUPPORT_TOPIC.value,
                    lang='ru', support_topic_id=TOPIC)
    db = MagicMock()
    db.get_user_by_topic_id.return_value = user
    if history == 'default':
        history = [
            {'timestamp': '2026-08-23T07:46:55', 'sender_type': 'user',
             'message_text': 'привет, ничего не работает', 'has_media': 0,
             'message_id': 10},
            {'timestamp': '2026-08-23T07:48:41', 'sender_type': 'admin',
             'message_text': 'смотрим', 'has_media': 1, 'message_id': 11},
        ]
    db.get_ticket_messages.return_value = history

    config = MagicMock(spec=Settings)
    config.FORUM_ENABLED = True
    config.FORUM_GROUP_ID = FORUM_ID
    config.TOPIC_SOLVED = topic_solved
    config.TOPIC_SUPPORT = 17
    config.is_admin = lambda uid: str(uid) == '111'

    handler = ForumHandler(bot, db, config)
    return handler, bot, db, user


def topic_sends(bot, thread):
    return [c.kwargs for c in bot.send_message.call_args_list
            if c.kwargs.get('message_thread_id') == thread
            and str(c.kwargs.get('chat_id')) == FORUM_ID]


def user_sends(bot, chat_id='777'):
    return [c.kwargs for c in bot.send_message.call_args_list
            if str(c.kwargs.get('chat_id')) == chat_id]


@pytest.fixture
def sm():
    with patch('bot.core.state_machine.StateMachine') as sm_cls:
        yield sm_cls


class TestGuards:
    def test_unknown_topic_says_so_and_closes_topic(self, sm):
        handler, bot, db, _ = make_handler(user=None)
        handler.handle_close_ticket({}, TOPIC)

        db.get_user_by_topic_id.assert_called_once_with(TOPIC)
        says = topic_sends(bot, TOPIC)
        assert len(says) == 1
        assert says[0]['text'] == (
            "❌ Не нашёл пользователя для этого топика — "
            "возможно, тикет уже закрыт. Закрываю топик."
        )
        assert says[0]['parse_mode'] == 'HTML'
        bot.close_forum_topic.assert_called_once_with(FORUM_ID, TOPIC)
        db.save_user.assert_not_called()
        assert not user_sends(bot)

    def test_unknown_topic_close_failure_is_swallowed(self, sm, caplog):
        handler, bot, *_ = make_handler(user=None)
        bot.close_forum_topic.side_effect = RuntimeError('gone')
        handler.handle_close_ticket({}, TOPIC)  # must not raise
        assert f'close_forum_topic failed for {TOPIC}' in caplog.text

    def test_stale_topic_closes_only_this_one(self, sm):
        user = User(chat_id='777', username='alice',
                    status=UserState.SUPPORT_TOPIC.value, lang='ru',
                    support_topic_id=999)
        handler, bot, db, _ = make_handler(user=user)
        handler.handle_close_ticket({}, TOPIC)

        says = topic_sends(bot, TOPIC)
        assert len(says) == 1
        assert '999' in says[0]['text'] and '777' in says[0]['text']
        bot.close_forum_topic.assert_called_once_with(FORUM_ID, TOPIC)
        # no SOLVED log, no user notify, ticket binding untouched
        assert not topic_sends(bot, SOLVED)
        assert not user_sends(bot)
        db.save_user.assert_not_called()
        assert user.support_topic_id == 999

    def test_stale_topic_close_failure_is_swallowed(self, sm):
        user = User(chat_id='777', username='a', status='demo',
                    support_topic_id=999)
        handler, bot, *_ = make_handler(user=user)
        bot.close_forum_topic.side_effect = RuntimeError('gone')
        handler.handle_close_ticket({}, TOPIC)  # must not raise


class TestHappyPath:
    def test_ack_then_log_to_solved_topic(self, sm):
        handler, bot, db, _ = make_handler()
        handler.handle_close_ticket({}, TOPIC)

        db.get_ticket_messages.assert_called_once_with(TOPIC)
        acks = topic_sends(bot, TOPIC)
        assert len(acks) == 1 and acks[0]['text'] == '🔒 Закрываю тикет…'

        logs = topic_sends(bot, SOLVED)
        assert len(logs) == 1
        log = logs[0]['text']
        assert f'Ticket Log: #T{TOPIC}' in log
        assert '<code>777</code>' in log and '@alice' in log
        # lines joined with a bare newline, no decoration around it
        assert '\n[07:46] 👤 User: привет, ничего не работает' in log
        assert '\n[07:48] 👨‍💻 Admin: смотрим 📎' in log
        assert 'Вложений: 1' in log
        assert logs[0]['parse_mode'] == 'HTML'

    def test_close_is_logged(self, sm, caplog):
        caplog.set_level(logging.INFO)
        handler, *_ = make_handler()
        handler.handle_close_ticket({}, TOPIC)
        assert f'Ticket closed for user 777 in topic {TOPIC}' in caplog.text

    def test_history_entry_without_text_stays_blank(self, sm):
        history = [{'timestamp': '2026-08-23T07:46:55', 'sender_type': 'user',
                    'message_text': None, 'has_media': 0, 'message_id': 10}]
        handler, bot, *_ = make_handler(history=history)
        handler.handle_close_ticket({}, TOPIC)
        log = topic_sends(bot, SOLVED)[0]['text']
        assert '\n[07:46] 👤 User: ' in log
        assert 'None' not in log and 'XXXX' not in log

    def test_media_copied_into_solved_archive(self, sm):
        handler, bot, *_ = make_handler()
        handler.handle_close_ticket({}, TOPIC)

        bot.copy_message.assert_called_once()
        copy = bot.copy_message.call_args.kwargs
        assert copy['chat_id'] == FORUM_ID
        assert copy['from_chat_id'] == FORUM_ID
        assert copy['message_id'] == 11
        assert copy['message_thread_id'] == SOLVED
        assert copy['caption'] == f'#ticket_{TOPIC}'

    RU_CLOSED = "✅ Ваш тикет закрыт. Если появятся новые вопросы — создайте новый тикет."
    EN_CLOSED = "✅ Your ticket is closed. Open a new one if you have further questions."

    def test_user_notified_in_russian(self, sm):
        handler, bot, *_ = make_handler()
        handler.handle_close_ticket({}, TOPIC)
        notes = user_sends(bot)
        assert len(notes) == 1
        assert notes[0]['text'] == self.RU_CLOSED

    def test_user_notified_in_english(self, sm):
        user = User(chat_id='777', username='alice', status='demo',
                    lang='en', support_topic_id=TOPIC)
        handler, bot, *_ = make_handler(user=user)
        handler.handle_close_ticket({}, TOPIC)
        assert user_sends(bot)[0]['text'] == self.EN_CLOSED

    def test_missing_lang_defaults_to_russian(self, sm):
        user = User(chat_id='777', username='alice', status='demo',
                    lang=None, support_topic_id=TOPIC)
        handler, bot, *_ = make_handler(user=user)
        handler.handle_close_ticket({}, TOPIC)
        assert user_sends(bot)[0]['text'] == self.RU_CLOSED

    def test_unknown_lang_falls_back_to_russian(self, sm):
        user = User(chat_id='777', username='alice', status='demo',
                    lang='de', support_topic_id=TOPIC)
        handler, bot, *_ = make_handler(user=user)
        handler.handle_close_ticket({}, TOPIC)
        assert user_sends(bot)[0]['text'] == self.RU_CLOSED

    def test_support_state_returns_via_state_machine(self, sm):
        handler, bot, db, _ = make_handler()
        handler.handle_close_ticket({}, TOPIC)
        sm.assert_called_once_with(db)
        sm.return_value.return_from_support.assert_called_once_with('777')

    def test_other_state_skips_state_machine(self, sm):
        user = User(chat_id='777', username='alice', status='demo',
                    lang='ru', support_topic_id=TOPIC)
        handler, *_ = make_handler(user=user)
        handler.handle_close_ticket({}, TOPIC)
        sm.return_value.return_from_support.assert_not_called()

    def test_topic_renamed_closed_and_binding_cleared(self, sm):
        handler, bot, db, user = make_handler()
        handler.handle_close_ticket({}, TOPIC)

        rename = bot.edit_forum_topic.call_args.kwargs
        assert rename['chat_id'] == FORUM_ID
        assert rename['message_thread_id'] == TOPIC
        assert rename['name'] == '✅ @alice'

        bot.close_forum_topic.assert_called_once_with(FORUM_ID, TOPIC)
        assert user.support_topic_id is None
        db.save_user.assert_called_once_with(user)

    def test_no_username_falls_back_to_user_id(self, sm):
        user = User(chat_id='777', username=None, status='demo',
                    lang='ru', support_topic_id=TOPIC)
        handler, bot, *_ = make_handler(user=user)
        handler.handle_close_ticket({}, TOPIC)
        assert 'user_777' in topic_sends(bot, SOLVED)[0]['text']
        assert bot.edit_forum_topic.call_args.kwargs['name'] == '✅ user_777'

    def test_long_username_rename_is_capped_at_128(self, sm):
        user = User(chat_id='777', username='x' * 200, status='demo',
                    lang='ru', support_topic_id=TOPIC)
        handler, bot, *_ = make_handler(user=user)
        handler.handle_close_ticket({}, TOPIC)
        assert len(bot.edit_forum_topic.call_args.kwargs['name']) == 128

    def test_solved_topic_falls_back_to_support(self, sm):
        handler, bot, *_ = make_handler(topic_solved=None)
        handler.handle_close_ticket({}, TOPIC)
        assert len(topic_sends(bot, 17)) == 1
        assert bot.copy_message.call_args.kwargs['message_thread_id'] == 17

    def test_config_without_solved_attr_falls_back_to_support(self, sm):
        """Old prod configs may predate TOPIC_SOLVED entirely — the
        getattr default must keep the close flow alive."""
        handler, bot, *_ = make_handler()
        del handler.config.TOPIC_SOLVED
        handler.handle_close_ticket({}, TOPIC)
        assert len(topic_sends(bot, 17)) == 1
        assert not [c for c in bot.send_message.call_args_list
                    if 'Не удалось закрыть' in c.kwargs.get('text', '')]

    def test_empty_history_still_produces_header_log(self, sm):
        handler, bot, *_ = make_handler(history=None)
        handler.handle_close_ticket({}, TOPIC)
        log = topic_sends(bot, SOLVED)[0]['text']
        assert f'Ticket Log: #T{TOPIC}' in log
        assert 'Вложений' not in log
        bot.copy_message.assert_not_called()


class TestResilience:
    def test_media_without_message_id_does_not_stop_later_copies(self, sm):
        """A missing message_id skips ONE entry (continue, not break)."""
        history = [
            {'timestamp': '2026-08-23T07:46:55', 'sender_type': 'user',
             'message_text': 'скрин', 'has_media': 1, 'message_id': None},
            {'timestamp': '2026-08-23T07:47:55', 'sender_type': 'user',
             'message_text': 'ещё скрин', 'has_media': 1, 'message_id': 11},
        ]
        handler, bot, *_ = make_handler(history=history)
        handler.handle_close_ticket({}, TOPIC)
        bot.copy_message.assert_called_once()
        assert bot.copy_message.call_args.kwargs['message_id'] == 11
        assert len(user_sends(bot)) == 1  # flow continued

    def test_one_failed_copy_does_not_abort_the_rest(self, sm, caplog):
        history = [
            {'timestamp': '2026-08-23T07:46:55', 'sender_type': 'user',
             'message_text': 'a', 'has_media': 1, 'message_id': 10},
            {'timestamp': '2026-08-23T07:47:55', 'sender_type': 'user',
             'message_text': 'b', 'has_media': 1, 'message_id': 11},
        ]
        handler, bot, *_ = make_handler(history=history)
        bot.copy_message.side_effect = [RuntimeError('too old'), {'message_id': 3}]
        handler.handle_close_ticket({}, TOPIC)
        assert bot.copy_message.call_count == 2
        assert len(user_sends(bot)) == 1
        assert 'copy media 10 failed' in caplog.text
        # both attachments counted in the log footer
        assert 'Вложений: 2' in topic_sends(bot, SOLVED)[0]['text']

    def test_rename_failure_still_closes_and_saves(self, sm, caplog):
        handler, bot, db, user = make_handler()
        bot.edit_forum_topic.side_effect = RuntimeError('no rights')
        handler.handle_close_ticket({}, TOPIC)
        bot.close_forum_topic.assert_called_once()
        assert user.support_topic_id is None
        db.save_user.assert_called_once()
        assert f'edit_forum_topic rename failed for {TOPIC}' in caplog.text

    def test_close_failure_still_clears_binding(self, sm, caplog):
        """close_forum_topic dying must not orphan the user on a dead topic."""
        handler, bot, db, user = make_handler()
        bot.close_forum_topic.side_effect = RuntimeError('flood limit')
        handler.handle_close_ticket({}, TOPIC)
        assert user.support_topic_id is None
        db.save_user.assert_called_once_with(user)
        assert f'close_forum_topic failed for {TOPIC}' in caplog.text

    def test_midway_exception_is_reported_into_topic(self, sm, caplog):
        handler, bot, db, _ = make_handler()
        db.get_ticket_messages.side_effect = RuntimeError('db locked')
        handler.handle_close_ticket({}, TOPIC)
        says = topic_sends(bot, TOPIC)
        # ack + error report carrying the actual exception text
        errors = [s for s in says if 'Не удалось закрыть тикет' in s['text']]
        assert len(errors) == 1
        assert 'RuntimeError: db locked' in errors[0]['text']
        db.save_user.assert_not_called()
        assert f'handle_close_ticket failed for topic {TOPIC}' in caplog.text

    def test_error_report_truncates_long_exception_text(self, sm):
        handler, bot, db, _ = make_handler()
        db.get_ticket_messages.side_effect = RuntimeError('x' * 300)
        handler.handle_close_ticket({}, TOPIC)
        error = next(s['text'] for s in topic_sends(bot, TOPIC)
                     if 'Не удалось закрыть тикет' in s['text'])
        assert 'x' * 200 in error
        assert 'x' * 201 not in error

    def test_total_telegram_outage_does_not_raise(self, sm, caplog):
        handler, bot, *_ = make_handler()
        bot.send_message.side_effect = RuntimeError('network down')
        bot.close_forum_topic.side_effect = RuntimeError('network down')
        bot.edit_forum_topic.side_effect = RuntimeError('network down')
        handler.handle_close_ticket({}, TOPIC)  # must not raise
        assert 'feedback send failed' in caplog.text
