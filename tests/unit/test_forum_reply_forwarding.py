"""Tests for ForumHandler: admin replies in support topics must reach the client.

Regression suite for the 2026-08-23 bug: replies from the support topic
never reached the user when the admin wrote anonymously ("Remain
Anonymous" → from = GroupAnonymousBot), when the replier was a chat
admin other than SUPER_ADMIN (chat_id was not passed to _is_admin, so
the getChatMember path never ran), or when the reply was media with a
caption (only `text` was forwarded).

Also serves as the killing suite for mutation testing of
bot/handlers/forum.py (see pyproject.toml [tool.mutmut]).
"""

import pytest
from unittest.mock import MagicMock, patch

from bot.handlers.forum import ForumHandler
from bot.config import Settings

FORUM_ID = '-1001234567890'
SUPER_ADMIN = '111'
ANON_BOT = 1087968824


def make_handler(get_chat_member_status='member'):
    bot = MagicMock()
    bot.send_message.return_value = {'message_id': 1}
    bot.copy_message.return_value = {'message_id': 2}
    bot.get_chat_member.return_value = {'status': get_chat_member_status}

    db = MagicMock()
    user = MagicMock()
    user.chat_id = '777'
    user.username = 'client'
    user.support_topic_id = 555
    db.get_user_by_topic_id.return_value = user

    config = MagicMock(spec=Settings)
    config.FORUM_ENABLED = True
    config.FORUM_GROUP_ID = FORUM_ID
    config.is_admin = lambda uid: str(uid) == SUPER_ADMIN

    handler = ForumHandler(bot, db, config)
    return handler, bot, db, user


def topic_message(from_id=SUPER_ADMIN, text='попробуйте перезайти', thread=555, **extra):
    msg = {
        'message_id': 42,
        'chat': {'id': int(FORUM_ID), 'type': 'supergroup'},
        'message_thread_id': thread,
        'is_topic_message': True,
    }
    if from_id is not None:
        msg['from'] = {'id': from_id, 'username': 'staff'}
    if text is not None:
        msg['text'] = text
    msg.update(extra)
    return {'update_id': 1, 'message': msg}


class TestCanHandle:
    def test_accepts_topic_message_in_forum_group(self):
        handler, *_ = make_handler()
        assert handler.can_handle(topic_message()) is True

    def test_rejects_when_forum_disabled(self):
        handler, *_ = make_handler()
        handler.disabled = True
        assert handler.can_handle(topic_message()) is False

    def test_constructor_disables_handler_when_forum_off(self):
        """disabled must be derived from FORUM_ENABLED in __init__."""
        handler, *_ = make_handler()
        config = handler.config
        config.FORUM_ENABLED = False
        off = ForumHandler(handler.bot, handler.db, config)
        assert off.disabled is True
        assert off.can_handle(topic_message()) is False

    def test_message_without_chat_is_rejected_not_crashed(self):
        """A degenerate update must return False, not raise — can_handle
        runs on the polling thread outside any try/except."""
        handler, *_ = make_handler()
        upd = {'message': {'is_topic_message': True, 'message_thread_id': 5}}
        assert handler.can_handle(upd) is False

    def test_rejects_non_message_update(self):
        handler, *_ = make_handler()
        assert handler.can_handle({'callback_query': {}}) is False

    def test_rejects_non_topic_message(self):
        upd = topic_message()
        del upd['message']['is_topic_message']
        handler, *_ = make_handler()
        assert handler.can_handle(upd) is False

    def test_rejects_other_group(self):
        upd = topic_message()
        upd['message']['chat']['id'] = -100555
        handler, *_ = make_handler()
        assert handler.can_handle(upd) is False


class TestReplyForwarding:
    def test_super_admin_text_reply_reaches_client(self):
        handler, bot, db, user = make_handler()
        handler.handle(topic_message())

        db.get_user_by_topic_id.assert_called_once_with(555)
        bot.send_message.assert_called_once()
        kwargs = bot.send_message.call_args.kwargs
        assert kwargs['chat_id'] == '777'
        assert 'попробуйте перезайти' in kwargs['text']
        assert 'Support Reply' in kwargs['text']
        assert kwargs['parse_mode'] == 'HTML'

    def test_anonymous_admin_reply_reaches_client(self):
        """'Remain Anonymous' admin: from = GroupAnonymousBot, sender_chat = group."""
        handler, bot, *_ = make_handler()
        upd = topic_message(
            from_id=ANON_BOT,
            sender_chat={'id': int(FORUM_ID), 'type': 'supergroup'},
        )
        handler.handle(upd)

        kwargs = bot.send_message.call_args.kwargs
        assert kwargs['chat_id'] == '777'
        # anonymous path must not hit getChatMember — sender_chat is proof
        bot.get_chat_member.assert_not_called()

    def test_chat_admin_reply_reaches_client(self):
        """A group admin who is not SUPER_ADMIN resolves via getChatMember."""
        handler, bot, *_ = make_handler(get_chat_member_status='administrator')
        handler.handle(topic_message(from_id=222))

        bot.get_chat_member.assert_called_once_with(FORUM_ID, '222')
        assert bot.send_message.call_args.kwargs['chat_id'] == '777'

    def test_plain_member_reply_is_not_forwarded(self):
        handler, bot, *_ = make_handler(get_chat_member_status='member')
        handler.handle(topic_message(from_id=333))
        bot.send_message.assert_not_called()
        bot.copy_message.assert_not_called()

    def test_sender_chat_of_foreign_channel_is_not_admin(self):
        """'Send as channel' from a random channel must not count as staff."""
        handler, bot, *_ = make_handler(get_chat_member_status='member')
        upd = topic_message(
            from_id=333,
            sender_chat={'id': -100777, 'type': 'channel'},
        )
        handler.handle(upd)
        bot.send_message.assert_not_called()

    def test_missing_from_and_sender_chat_is_not_forwarded(self):
        handler, bot, *_ = make_handler()
        handler.handle(topic_message(from_id=None))
        bot.send_message.assert_not_called()

    def test_empty_from_does_not_hit_chat_member_api(self):
        """No from.id → deny locally; a getChatMember probe for a bogus
        id would be a wasted API call per message."""
        handler, bot, *_ = make_handler()
        upd = topic_message(from_id=None)
        upd['message']['from'] = {}
        handler.handle(upd)
        bot.send_message.assert_not_called()
        bot.get_chat_member.assert_not_called()

    def test_is_forum_admin_tolerates_degenerate_message(self):
        handler, *_ = make_handler()
        assert handler._is_forum_admin({}) is False

    def test_is_forum_admin_without_chat_skips_chat_member_probe(self):
        """No chat id → nothing to resolve adminship against; must not
        fire a getChatMember request with a made-up chat id."""
        handler, bot, *_ = make_handler()
        result = handler._is_forum_admin({'from': {'id': 222}})
        assert result is False
        bot.get_chat_member.assert_not_called()

    def test_no_user_for_topic_sends_nothing(self):
        handler, bot, db, _ = make_handler()
        db.get_user_by_topic_id.return_value = None
        handler.handle(topic_message())
        bot.send_message.assert_not_called()

    def test_no_thread_id_is_ignored(self):
        handler, bot, db, _ = make_handler()
        handler.handle(topic_message(thread=None))
        bot.send_message.assert_not_called()
        db.log_ticket_message.assert_not_called()

    def test_empty_update_does_not_crash(self):
        handler, bot, *_ = make_handler()
        handler.handle({})
        bot.send_message.assert_not_called()

    def test_sticker_like_message_sends_nothing(self):
        """No text key at all and no forwardable media → stay silent,
        do not invent a body for the client."""
        handler, bot, *_ = make_handler()
        handler.handle(topic_message(text=None, sticker={'file_id': 's1'}))
        bot.send_message.assert_not_called()
        bot.copy_message.assert_not_called()

    def test_empty_text_without_media_sends_nothing(self):
        handler, bot, *_ = make_handler()
        handler.handle(topic_message(text=''))
        bot.send_message.assert_not_called()
        bot.copy_message.assert_not_called()


class TestMediaForwarding:
    def test_admin_photo_with_caption_is_copied_to_client(self):
        handler, bot, *_ = make_handler()
        upd = topic_message(
            text=None,
            photo=[{'file_id': 'abc'}],
            caption='вот скрин настроек',
        )
        handler.handle(upd)

        # header + copyMessage with the original attachment
        header = bot.send_message.call_args.kwargs
        assert header['chat_id'] == '777'
        assert header['text'] == '💬 <b>Support Reply:</b>'
        assert header['parse_mode'] == 'HTML'
        copy = bot.copy_message.call_args.kwargs
        assert copy['chat_id'] == '777'
        assert copy['from_chat_id'] == FORUM_ID
        assert copy['message_id'] == 42

    def test_admin_document_is_copied(self):
        handler, bot, *_ = make_handler()
        handler.handle(topic_message(text=None, document={'file_id': 'doc1'}))
        bot.copy_message.assert_called_once()

    def test_admin_voice_is_copied(self):
        handler, bot, *_ = make_handler()
        handler.handle(topic_message(text=None, voice={'file_id': 'v1'}))
        bot.copy_message.assert_called_once()

    def test_admin_video_is_copied(self):
        handler, bot, *_ = make_handler()
        handler.handle(topic_message(text=None, video={'file_id': 'vid1'}))
        bot.copy_message.assert_called_once()

    def test_media_with_text_prefers_copy_over_duplicate_text(self):
        """A media message never carries `text`, but if both appear the
        attachment path must win — otherwise the client gets the caption
        twice."""
        handler, bot, *_ = make_handler()
        handler.handle(topic_message(text='dup', photo=[{'file_id': 'abc'}]))
        bot.copy_message.assert_called_once()
        # only the header text, not a second forwarded body
        assert bot.send_message.call_count == 1

    def test_member_photo_is_not_copied(self):
        handler, bot, *_ = make_handler(get_chat_member_status='member')
        handler.handle(topic_message(from_id=333, text=None, photo=[{'file_id': 'x'}]))
        bot.copy_message.assert_not_called()


class TestTicketLogging:
    def test_topic_message_is_logged(self):
        handler, bot, db, _ = make_handler()
        handler.handle(topic_message())
        db.log_ticket_message.assert_called_once()
        args = db.log_ticket_message.call_args.args
        assert args[0] == 555            # topic_id
        assert args[1] == 'admin'        # sender_type
        assert args[3] == 'попробуйте перезайти'

    def test_close_command_routes_to_close_ticket(self):
        handler, *_ = make_handler()
        upd = topic_message(text='/close')
        with patch.object(handler, 'handle_close_ticket') as mock_close:
            handler.handle(upd)
            mock_close.assert_called_once_with(upd, 555)

    def test_close_command_is_case_insensitive_and_not_logged(self):
        handler, bot, db, _ = make_handler()
        upd = topic_message(text='/CLOSE resolved')
        with patch.object(handler, 'handle_close_ticket') as mock_close:
            handler.handle(upd)
            mock_close.assert_called_once()
        db.log_ticket_message.assert_not_called()


class TestDirectedRepliesInSharedTopic:
    """Admin answers in the shared «Поддержка» topic (no bound user):
    "@<chat_id> текст" or a swipe-reply to the bot's failure report must
    be relayed through the bot; the topic gets an explicit ✅/⚠️ ack."""

    def make_shared(self, **kw):
        handler, bot, db, user = make_handler(**kw)
        db.get_user_by_topic_id.return_value = None  # shared topic
        db.get_user.side_effect = lambda cid: user if str(cid) == '777' else None
        db.get_user_by_username.side_effect = (
            lambda uname: user if uname.lower() == 'client' else None
        )
        return handler, bot, db, user

    @staticmethod
    def sends_to(bot, chat_id):
        return [c.kwargs for c in bot.send_message.call_args_list
                if str(c.kwargs.get('chat_id')) == str(chat_id)]

    def test_at_chat_id_reaches_client_with_ack(self):
        handler, bot, *_ = self.make_shared()
        handler.handle(topic_message(text='@777 попробуйте перезайти', thread=17))

        to_user = self.sends_to(bot, '777')
        assert len(to_user) == 1
        assert 'попробуйте перезайти' in to_user[0]['text']
        assert '@777' not in to_user[0]['text']  # addressing prefix stripped
        acks = self.sends_to(bot, FORUM_ID)
        assert len(acks) == 1 and '✅' in acks[0]['text']
        assert acks[0]['message_thread_id'] == 17

    def test_at_username_reaches_client(self):
        handler, bot, *_ = self.make_shared()
        handler.handle(topic_message(text='@client привет', thread=17))
        assert len(self.sends_to(bot, '777')) == 1

    def test_unknown_address_gets_warning_ack(self):
        handler, bot, *_ = self.make_shared()
        handler.handle(topic_message(text='@999999 привет', thread=17))
        assert not self.sends_to(bot, '777')
        acks = self.sends_to(bot, FORUM_ID)
        assert len(acks) == 1 and '⚠️' in acks[0]['text'] and '999999' in acks[0]['text']
        assert acks[0]['message_thread_id'] == 17
        assert acks[0]['parse_mode'] == 'HTML'

    def test_swipe_reply_to_failure_report_reaches_client(self):
        handler, bot, db, user = self.make_shared()
        user.chat_id = '8111788347'
        db.get_user.side_effect = (
            lambda cid: user if str(cid) == '8111788347' else None
        )
        upd = topic_message(text='обновите подписку', thread=17)
        upd['message']['reply_to_message'] = {
            'message_id': 7,
            'text': ('🆘 Failure report #8\nUser: @user_8111788347 (8111788347)\n'
                     'Problem: ничего не грузится'),
        }
        handler.handle(upd)
        to_user = self.sends_to(bot, '8111788347')
        assert len(to_user) == 1
        assert 'обновите подписку' in to_user[0]['text']
        assert any('✅' in a['text'] for a in self.sends_to(bot, FORUM_ID))

    def test_plain_chatter_stays_silent(self):
        handler, bot, *_ = self.make_shared()
        handler.handle(topic_message(text='коллеги, что по нагрузке?', thread=17))
        bot.send_message.assert_not_called()
        bot.copy_message.assert_not_called()

    def test_member_directed_reply_is_ignored(self):
        handler, bot, *_ = self.make_shared(get_chat_member_status='member')
        handler.handle(topic_message(from_id=333, text='@777 привет', thread=17))
        bot.send_message.assert_not_called()

    def test_directed_photo_caption_is_copied_without_prefix(self):
        handler, bot, *_ = self.make_shared()
        handler.handle(topic_message(
            text=None, thread=17,
            photo=[{'file_id': 'p1'}], caption='@777 вот так должно быть',
        ))
        copy = bot.copy_message.call_args.kwargs
        assert copy['chat_id'] == '777'
        assert copy['caption'] == 'вот так должно быть'
        assert any('✅' in a['text'] for a in self.sends_to(bot, FORUM_ID))

    def test_empty_directed_body_gets_warning(self):
        handler, bot, *_ = self.make_shared()
        handler.handle(topic_message(text='@777', thread=17))
        assert not self.sends_to(bot, '777')
        acks = self.sends_to(bot, FORUM_ID)
        assert len(acks) == 1 and '⚠️' in acks[0]['text']

    def test_failed_delivery_gets_warning_ack(self):
        handler, bot, *_ = self.make_shared()

        def send(chat_id=None, **kw):
            if str(chat_id) == '777':
                raise RuntimeError('bot was blocked by the user')
            return {'message_id': 1}

        bot.send_message.side_effect = send
        handler.handle(topic_message(text='@777 привет', thread=17))
        acks = self.sends_to(bot, FORUM_ID)
        assert len(acks) == 1 and '⚠️' in acks[0]['text']
        assert acks[0]['message_thread_id'] == 17

    def test_swipe_reply_media_caption_resolves_id(self):
        """Bot report can be a media post — the id then lives in its
        caption, not text."""
        handler, bot, db, user = self.make_shared()
        user.chat_id = '8111788347'
        db.get_user.side_effect = (
            lambda cid: user if str(cid) == '8111788347' else None
        )
        upd = topic_message(text='включите ECH', thread=17)
        upd['message']['reply_to_message'] = {
            'message_id': 7,
            'photo': [{'file_id': 'rpt'}],
            'caption': 'DPI график. User: @user (8111788347)',
        }
        handler.handle(upd)
        assert len(self.sends_to(bot, '8111788347')) == 1

    def test_swipe_reply_bare_at_id_without_parens_resolves(self):
        handler, bot, db, user = self.make_shared()
        user.chat_id = '8111788347'
        db.get_user.side_effect = (
            lambda cid: user if str(cid) == '8111788347' else None
        )
        upd = topic_message(text='обновите подписку', thread=17)
        upd['message']['reply_to_message'] = {
            'message_id': 7,
            'text': 'жалоба от @8111788347, ничего не грузится',
        }
        handler.handle(upd)
        assert len(self.sends_to(bot, '8111788347')) == 1

    def test_swipe_reply_with_empty_body_sends_nothing_to_client(self):
        """Sticker swipe-reply to a report: nothing forwardable — must
        not invent a body for the client."""
        handler, bot, db, user = self.make_shared()
        user.chat_id = '8111788347'
        db.get_user.side_effect = (
            lambda cid: user if str(cid) == '8111788347' else None
        )
        upd = topic_message(text=None, thread=17, sticker={'file_id': 's'})
        upd['message']['reply_to_message'] = {
            'message_id': 7,
            'text': 'User: @user (8111788347)',
        }
        handler.handle(upd)
        assert not self.sends_to(bot, '8111788347')
        acks = self.sends_to(bot, FORUM_ID)
        assert len(acks) == 1 and '⚠️' in acks[0]['text']

    def test_ack_send_failure_is_logged_not_raised(self, caplog):
        handler, bot, *_ = self.make_shared()
        bot.send_message.side_effect = RuntimeError('topic closed')
        handler._notify_topic(17, 'test')
        assert 'topic ack failed' in caplog.text

    def test_ticket_topic_failed_delivery_gets_warning_ack(self):
        """Bound ticket topic: success is silent, failure must be loud."""
        handler, bot, db, user = make_handler()

        def send(chat_id=None, **kw):
            if str(chat_id) == '777':
                raise RuntimeError('blocked')
            return {'message_id': 1}

        bot.send_message.side_effect = send
        handler.handle(topic_message())
        acks = [c.kwargs for c in bot.send_message.call_args_list
                if str(c.kwargs.get('chat_id')) == FORUM_ID]
        assert len(acks) == 1 and '⚠️' in acks[0]['text']
        assert acks[0]['message_thread_id'] == 555


class TestSendFailures:
    def test_forward_reply_returns_false_on_send_exception(self, caplog):
        handler, bot, _, user = make_handler()
        bot.send_message.side_effect = RuntimeError('network down')
        assert handler._forward_reply_to_user(user, 'hi') is False
        assert 'Failed to send reply to user' in caplog.text

    def test_forward_reply_returns_false_on_none_result(self):
        handler, bot, _, user = make_handler()
        bot.send_message.return_value = None
        assert handler._forward_reply_to_user(user, 'hi') is False

    def test_copy_media_returns_false_on_exception(self, caplog):
        handler, bot, _, user = make_handler()
        bot.copy_message.side_effect = RuntimeError('boom')
        assert handler._copy_media_to_user(user, {'message_id': 42}) is False
        assert 'Failed to copy media reply' in caplog.text

    def test_copy_media_returns_true_on_success(self):
        handler, bot, _, user = make_handler()
        assert handler._copy_media_to_user(user, {'message_id': 42}) is True
