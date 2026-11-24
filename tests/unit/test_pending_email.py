"""Pending-email prompt flow (📧 button → next plain text = the address).

Regression test for the ziriki case (2026-07-25): the button used to
only print /setemail instructions; users replied with the bare address
and it was swallowed by the "I don't understand" fallback. Since
2026-11-24 the address gets a code first (bot/services/email_link.py);
tests/integration/test_email_link.py covers the code and the linking.
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from bot.handlers.messages import (
    EMAIL_PURPOSE,
    MessageHandler,
    PENDING_EMAIL,
    PENDING_EMAIL_CODE,
    PENDING_EMAIL_TTL,
)


@pytest.fixture(autouse=True)
def _clean_pending():
    for pending in (PENDING_EMAIL, PENDING_EMAIL_CODE, EMAIL_PURPOSE):
        pending.clear()
    yield
    for pending in (PENDING_EMAIL, PENDING_EMAIL_CODE, EMAIL_PURPOSE):
        pending.clear()


@pytest.fixture
def handler():
    h = MessageHandler.__new__(MessageHandler)
    h.db = MagicMock()
    h.bot = MagicMock()
    h.config = MagicMock()
    return h


def make_user(chat_id='52291265', lang='ru'):
    return SimpleNamespace(chat_id=chat_id, lang=lang, contact_email=None)


class TestPendingEmail:
    def test_valid_email_gets_a_code_not_saved_yet(self, handler):
        user = make_user()
        PENDING_EMAIL['52291265'] = time.time()
        with patch('bot.services.email_link.send_code', return_value='sent') as send:
            handled = handler._maybe_handle_pending_email(
                '52291265', 'ziriki@gmail.com', user,
            )
        assert handled is True
        assert send.call_args.args[3:5] == ('52291265', 'ziriki@gmail.com')
        assert user.contact_email is None
        handler.db.save_user.assert_not_called()
        assert '52291265' not in PENDING_EMAIL
        assert '52291265' in PENDING_EMAIL_CODE
        text = handler.bot.send_message.call_args.kwargs['text']
        assert 'z***@gmail.com' in text and 'ziriki@' not in text

    def test_a_refused_letter_reopens_the_prompt(self, handler):
        user = make_user()
        PENDING_EMAIL['52291265'] = time.time()
        with patch('bot.services.email_link.send_code', return_value='mail_failed'):
            handler._maybe_handle_pending_email('52291265', 'ziriki@gmail.com', user)
        assert '52291265' in PENDING_EMAIL
        assert '52291265' not in PENDING_EMAIL_CODE

    def test_too_many_letters_closes_the_flow(self, handler):
        user = make_user()
        PENDING_EMAIL['52291265'] = time.time()
        EMAIL_PURPOSE['52291265'] = 'login'
        with patch('bot.services.email_link.send_code', return_value='rate_limited'):
            handler._maybe_handle_pending_email('52291265', 'ziriki@gmail.com', user)
        assert not ('52291265' in PENDING_EMAIL or '52291265' in PENDING_EMAIL_CODE
                    or '52291265' in EMAIL_PURPOSE)
        assert 'Попробуйте позже' in handler.bot.send_message.call_args.kwargs['text']

    def test_invalid_email_keeps_pending_for_retry(self, handler):
        user = make_user()
        PENDING_EMAIL['52291265'] = time.time()
        handled = handler._maybe_handle_pending_email(
            '52291265', 'ne-pochtka', user,
        )
        assert handled is True
        assert user.contact_email is None
        handler.db.save_user.assert_not_called()
        assert '52291265' in PENDING_EMAIL  # retry window stays open
        markup = handler.bot.send_message.call_args.kwargs['reply_markup']
        assert markup['inline_keyboard'][0][0]['callback_data'] == 'email_cancel'

    def test_expired_pending_passes_through(self, handler):
        user = make_user()
        PENDING_EMAIL['52291265'] = time.time() - PENDING_EMAIL_TTL - 5
        handled = handler._maybe_handle_pending_email(
            '52291265', 'ziriki@gmail.com', user,
        )
        assert handled is False
        assert user.contact_email is None
        assert '52291265' not in PENDING_EMAIL

    def test_no_pending_passes_through(self, handler):
        user = make_user()
        handled = handler._maybe_handle_pending_email(
            '52291265', 'ziriki@gmail.com', user,
        )
        assert handled is False
        handler.db.save_user.assert_not_called()

    def test_the_code_goes_to_the_trimmed_address(self, handler):
        user = make_user()
        PENDING_EMAIL['52291265'] = time.time()
        with patch('bot.services.email_link.send_code', return_value='sent') as send:
            handler._maybe_handle_pending_email('52291265', '  A@b.co  ', user)
        assert send.call_args.args[4] == 'A@b.co'


class TestPromptArmsPending:
    def test_prompt_sets_flag(self):
        from bot.handlers.callbacks.user import EmailPromptHandler
        h = EmailPromptHandler.__new__(EmailPromptHandler)
        h.db = MagicMock()
        h.bot = MagicMock()
        h.db.get_user.return_value = make_user()
        h.handle({'callback_query': {}}, '52291265', '52291265')
        assert PENDING_EMAIL.get('52291265') is not None
        assert EMAIL_PURPOSE.get('52291265') == 'add'
        text = h.bot.send_message.call_args.kwargs['text']
        assert 'ответным сообщением' in text and 'код' in text

    def test_login_button_asks_for_the_address(self):
        from bot.handlers.callbacks.user import EmailLoginHandler
        h = EmailLoginHandler.__new__(EmailLoginHandler)
        h.db = MagicMock()
        h.bot = MagicMock()
        h.db.get_user.return_value = make_user()
        PENDING_EMAIL_CODE['52291265'] = time.time()
        h.handle({'callback_query': {}}, '52291265', '52291265', data='email_login')
        assert PENDING_EMAIL.get('52291265') is not None
        assert EMAIL_PURPOSE.get('52291265') == 'login'
        assert '52291265' not in PENDING_EMAIL_CODE  # the latest tap wins
        assert 'регистрировались' in h.bot.send_message.call_args.kwargs['text']
