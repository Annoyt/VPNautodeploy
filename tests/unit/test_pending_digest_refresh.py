"""Pending-requests digest must survive button clicks.

Regression suite for the 2026-08-23 bug: one tap on an approve/reject
button of the hourly «⏳ Незакрытые заявки на демо» digest wiped the
whole card (update_request_message stamped it into a single-user
"✅ APPROVED" message). Each tap must instead re-render the digest:
remaining users keep their buttons, and the action lands in a visible
history trail at the bottom.
"""

import pytest
from unittest.mock import MagicMock, patch

from bot.config import Settings, UserState
from bot.handlers.callbacks.admin import ApproveUserHandler, RejectUserHandler
from bot.services.notifications import build_pending_digest
from bot.models import User

FORUM_ID = '-1001234567890'
ADMIN = '111'


class TestBuildPendingDigest:
    def test_renders_rows_with_digest_buttons(self):
        text, kb = build_pending_digest([
            ('123456789', 'alice', '2026-08-20T10:00:00'),
            ('987654321', None, '2026-08-21T11:00:00'),
        ])
        assert 'Незакрытые заявки на демо: 2' in text
        assert '@alice' in text and 'user_987654321' in text
        assert '2026-08-20' in text
        rows = kb['inline_keyboard']
        assert len(rows) == 2
        assert rows[0][0]['callback_data'] == 'approve:123456789:digest'
        assert rows[0][1]['callback_data'] == 'reject:123456789:digest'

    def test_truncates_after_twenty(self):
        rows_in = [(str(i), None, '2026-08-01') for i in range(25)]
        text, kb = build_pending_digest(rows_in)
        assert '… и ещё 5' in text
        assert len(kb['inline_keyboard']) == 20

    def test_empty_rows_render_all_clear_with_stripped_keyboard(self):
        text, kb = build_pending_digest([])
        assert 'Незакрытых заявок нет' in text
        assert kb == {'inline_keyboard': []}

    def test_history_is_appended(self):
        text, _ = build_pending_digest(
            [('123456789', 'alice', '2026-08-20')],
            history=['✅ @bob — одобрен', '❌ user_5 — отклонён'],
        )
        assert text.endswith('✅ @bob — одобрен\n❌ user_5 — отклонён')


def make_ctx(target_status='pending_demo', remaining=None):
    bot = MagicMock()
    bot.edit_message_text.return_value = {'message_id': 900}
    bot.send_message.return_value = {'message_id': 1}
    db = MagicMock()

    target = User(chat_id='123456789', username='alice',
                  status=target_status, lang='ru',
                  created_at='2026-08-20T10:00:00')
    db.get_user.side_effect = (
        lambda cid: target if str(cid) == '123456789' else None
    )
    db.get_users_by_status.return_value = remaining if remaining is not None else [
        User(chat_id='987654321', username=None, status='pending_demo',
             created_at='2026-08-21T11:00:00'),
    ]

    config = MagicMock(spec=Settings)
    config.SUPER_ADMIN_ID = ADMIN
    config.FORUM_ENABLED = True
    config.FORUM_GROUP_ID = FORUM_ID
    config.TOPIC_REQUESTS = 15
    config.TOPIC_USERS = 19
    config.TOPIC_REJECTED = 20
    return bot, db, config, target


def digest_update(data, text=None):
    msg = {
        'message_id': 900,
        'chat': {'id': int(FORUM_ID)},
        'message_thread_id': 15,
        'text': text if text is not None else (
            '⏳ Незакрытые заявки на демо: 2\n'
            '• @alice 123456789 (с 2026-08-20)\n'
            '• user_987654321 987654321 (с 2026-08-21)'
        ),
    }
    return {'callback_query': {'data': data, 'message': msg,
                               'from': {'id': int(ADMIN)}}}


@pytest.fixture
def patched_deps():
    with patch('bot.handlers.callbacks.admin.StateMachine') as sm, \
         patch('bot.handlers.callbacks.admin.NotificationService') as ns, \
         patch('bot.handlers.callbacks.admin.revoke_user_key') as rk, \
         patch('bot.handlers.callbacks.admin.build_rejected_user_keyboard',
               return_value={'inline_keyboard': []}):
        yield sm, ns, rk


class TestApproveFromDigest:
    def test_digest_is_rerendered_not_wiped(self, patched_deps):
        sm, ns, _ = patched_deps
        bot, db, config, _ = make_ctx()
        h = ApproveUserHandler(bot, db, config)
        h.handle(digest_update('approve:123456789:digest'),
                 FORUM_ID, ADMIN, data='approve:123456789:digest')

        sm.return_value.transition.assert_called_once()
        edit = bot.edit_message_text.call_args.kwargs
        assert edit['message_id'] == 900
        # remaining user keeps their row and buttons
        assert 'user_987654321' in edit['text']
        kb = edit['reply_markup']['inline_keyboard']
        assert kb and kb[0][0]['callback_data'] == 'approve:987654321:digest'
        # the action leaves a visible trace
        assert '✅ @alice — одобрен' in edit['text']

    def test_plain_approve_still_stamps_single_card(self, patched_deps):
        """Per-user request cards keep the old stamp behaviour."""
        bot, db, config, _ = make_ctx()
        h = ApproveUserHandler(bot, db, config)
        h.handle(digest_update('approve:123456789'),
                 FORUM_ID, ADMIN, data='approve:123456789')
        edit = bot.edit_message_text.call_args.kwargs
        assert 'APPROVED' in edit['text']
        assert edit['reply_markup'] == {'inline_keyboard': []}

    def test_already_processed_leaves_trace_without_transition(self, patched_deps):
        sm, *_ = patched_deps
        bot, db, config, _ = make_ctx(target_status='demo')
        h = ApproveUserHandler(bot, db, config)
        h.handle(digest_update('approve:123456789:digest'),
                 FORUM_ID, ADMIN, data='approve:123456789:digest')
        sm.return_value.transition.assert_not_called()
        edit = bot.edit_message_text.call_args.kwargs
        assert 'уже обработан' in edit['text']

    def test_history_lines_accumulate_across_clicks(self, patched_deps):
        bot, db, config, _ = make_ctx()
        h = ApproveUserHandler(bot, db, config)
        prior = ('⏳ Незакрытые заявки на демо: 2\n'
                 '• @alice 123456789 (с 2026-08-20)\n'
                 '\n'
                 '✅ @bob — одобрен')
        h.handle(digest_update('approve:123456789:digest', text=prior),
                 FORUM_ID, ADMIN, data='approve:123456789:digest')
        edit = bot.edit_message_text.call_args.kwargs
        assert '✅ @bob — одобрен' in edit['text']
        assert '✅ @alice — одобрен' in edit['text']

    def test_last_pending_click_shows_all_clear(self, patched_deps):
        bot, db, config, _ = make_ctx(remaining=[])
        h = ApproveUserHandler(bot, db, config)
        h.handle(digest_update('approve:123456789:digest'),
                 FORUM_ID, ADMIN, data='approve:123456789:digest')
        edit = bot.edit_message_text.call_args.kwargs
        assert 'Незакрытых заявок нет' in edit['text']
        assert edit['reply_markup'] == {'inline_keyboard': []}
        assert '✅ @alice — одобрен' in edit['text']

    def test_non_admin_click_is_denied(self, patched_deps):
        sm, *_ = patched_deps
        bot, db, config, _ = make_ctx()
        h = ApproveUserHandler(bot, db, config)
        h.handle(digest_update('approve:123456789:digest'),
                 FORUM_ID, '222', data='approve:123456789:digest')
        sm.return_value.transition.assert_not_called()
        bot.edit_message_text.assert_not_called()


class TestRejectFromDigest:
    def test_digest_is_rerendered_with_reject_trace(self, patched_deps):
        sm, ns, rk = patched_deps
        bot, db, config, _ = make_ctx()
        h = RejectUserHandler(bot, db, config)
        h.handle(digest_update('reject:123456789:digest'),
                 FORUM_ID, ADMIN, data='reject:123456789:digest')

        rk.assert_called_once()
        edit = bot.edit_message_text.call_args.kwargs
        assert '❌ @alice — отклонён' in edit['text']
        assert 'user_987654321' in edit['text']
        # rejected-topic card still posted
        card = bot.send_message_to_topic.call_args.kwargs
        assert card['message_thread_id'] == 20
        assert '123456789' in card['text']

    def test_plain_reject_still_stamps_single_card(self, patched_deps):
        bot, db, config, _ = make_ctx()
        h = RejectUserHandler(bot, db, config)
        h.handle(digest_update('reject:123456789'),
                 FORUM_ID, ADMIN, data='reject:123456789')
        edit = bot.edit_message_text.call_args.kwargs
        assert 'REJECTED' in edit['text']
