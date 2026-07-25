"""Pending-email prompt flow (📧 button → next plain text = the address).

Regression test for the ziriki case (2026-07-25): the button used to
only print /setemail instructions; users replied with the bare address
and it was swallowed by the "I don't understand" fallback.
"""
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bot.handlers.messages import (
    MessageHandler,
    PENDING_EMAIL,
    PENDING_EMAIL_TTL,
)


@pytest.fixture(autouse=True)
def _clean_pending():
    PENDING_EMAIL.clear()
    yield
    PENDING_EMAIL.clear()


@pytest.fixture
def handler():
    h = MessageHandler.__new__(MessageHandler)
    h.db = MagicMock()
    h.bot = MagicMock()
    return h


def make_user(chat_id='52291265', lang='ru'):
    return SimpleNamespace(chat_id=chat_id, lang=lang, contact_email=None)


class TestPendingEmail:
    def test_valid_email_saved_and_pending_cleared(self, handler):
        user = make_user()
        PENDING_EMAIL['52291265'] = time.time()
        handled = handler._maybe_handle_pending_email(
            '52291265', 'ziriki@gmail.com', user,
        )
        assert handled is True
        assert user.contact_email == 'ziriki@gmail.com'
        handler.db.save_user.assert_called_once_with(user)
        assert '52291265' not in PENDING_EMAIL
        text = handler.bot.send_message.call_args.kwargs['text']
        assert 'ziriki@gmail.com' in text

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

    def test_whitespace_and_case_normalised(self, handler):
        user = make_user()
        PENDING_EMAIL['52291265'] = time.time()
        handler._maybe_handle_pending_email('52291265', '  A@b.co  ', user)
        assert user.contact_email == 'A@b.co'


class TestPromptArmsPending:
    def test_prompt_sets_flag(self):
        from bot.handlers.callbacks.user import EmailPromptHandler
        h = EmailPromptHandler.__new__(EmailPromptHandler)
        h.db = MagicMock()
        h.bot = MagicMock()
        h.db.get_user.return_value = make_user()
        h.handle({'callback_query': {}}, '52291265', '52291265')
        assert PENDING_EMAIL.get('52291265') is not None
        text = h.bot.send_message.call_args.kwargs['text']
        assert 'ответным сообщением' in text
