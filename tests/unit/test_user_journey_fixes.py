"""Tests for user journey anomaly fixes (implementation_plan.md).

Covers:
- User model: previous_state, reject_count
- StateMachine: previous_state save, return_from_support
- DemoRequestHandler: REJECTED re-apply with rate-limit
- Commands: /start flow for REJECTED
- Notifications: notify_rejected_can_retry, notify_banned, IDOR fix
- Database: migration ACTIVE→DEMO, BANNED→REJECTED
"""

import pytest
import sqlite3
from unittest.mock import MagicMock, Mock, patch


class TestUserModelNewFields:
    """Test User model previous_state and reject_count."""

    def test_from_row_reads_previous_state(self):
        """Test that from_row reads previous_state column."""
        from bot.models.user import User
        row = {
            'chat_id': '123',
            'username': 'test',
            'previous_state': 'demo',
            'reject_count': 3,
            'status': 'rejected',
            'lang': 'ru'
        }
        user = User.from_row(row)
        assert user.previous_state == 'demo'
        assert user.reject_count == 3

    def test_from_row_missing_fields_defaults(self):
        """Test that missing previous_state/reject_count default safely."""
        from bot.models.user import User
        row = {
            'chat_id': '123',
            'status': 'new'
        }
        user = User.from_row(row)
        assert user.previous_state is None
        assert user.reject_count == 0


class TestStateMachinePreviousState:
    """Test StateMachine previous_state tracking."""

    @pytest.fixture
    def sm_db(self, tmp_path):
        """Create temporary database with full schema."""
        from bot.core.database import Database
        db_path = str(tmp_path / "sm_test.db")
        db = Database(db_path)
        return db

    def test_transition_saves_previous_state(self, sm_db):
        """Test that transition saves previous state before changing."""
        from bot.core.state_machine import StateMachine
        from bot.config.constants import UserState
        from bot.models import User

        sm = StateMachine(sm_db)

        user = User(chat_id='prev_test', status='new')
        sm_db.save_user(user)

        # Transition new -> pending_demo
        result = sm.transition('prev_test', UserState.PENDING_DEMO)
        assert result is True

        user = sm_db.get_user('prev_test')
        assert user.previous_state == 'new'

    def test_return_from_support_to_demo(self, sm_db):
        """Test return_from_support restores DEMO state."""
        from bot.core.state_machine import StateMachine
        from bot.config.constants import UserState
        from bot.models import User

        sm = StateMachine(sm_db)

        user = User(chat_id='ret_test', status='demo', previous_state='demo')
        sm_db.save_user(user)
        sm.transition('ret_test', UserState.SUPPORT_TOPIC)

        result = sm.return_from_support('ret_test')
        assert result is True

        user = sm_db.get_user('ret_test')
        assert user.status == 'demo'

    def test_return_from_support_to_paid(self, sm_db):
        """Test return_from_support restores PAID state."""
        from bot.core.state_machine import StateMachine
        from bot.config.constants import UserState
        from bot.models import User

        sm = StateMachine(sm_db)

        user = User(chat_id='ret_paid', status='paid', previous_state='paid')
        sm_db.save_user(user)
        sm.transition('ret_paid', UserState.SUPPORT_TOPIC)

        result = sm.return_from_support('ret_paid')
        assert result is True

        user = sm_db.get_user('ret_paid')
        assert user.status == 'paid'

    def test_return_from_support_not_in_support(self, sm_db):
        """Test return_from_support fails if user not in SUPPORT_TOPIC."""
        from bot.core.state_machine import StateMachine
        from bot.config.constants import UserState
        from bot.models import User

        sm = StateMachine(sm_db)

        user = User(chat_id='ret_fail', status='demo')
        sm_db.save_user(user)

        result = sm.return_from_support('ret_fail')
        assert result is False


class TestRejectedUserReapply:
    """Test REJECTED user can re-apply with rate-limit."""

    def test_rejected_user_can_request_demo(self):
        """Test REJECTED user with reject_count < limit can request demo."""
        from bot.handlers.callbacks.user import DemoRequestHandler
        from bot.config.constants import UserState

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.MAX_REJECT_RETRIES = 10

        handler = DemoRequestHandler(bot, db, config)
        DemoRequestHandler._demo_request_times.clear()

        user = MagicMock()
        user.status = UserState.REJECTED.value
        user.reject_count = 3

        assert handler._can_request_demo(user) is True

    def test_rejected_user_rate_limited(self):
        """Test REJECTED user with reject_count >= limit cannot request demo."""
        from bot.handlers.callbacks.user import DemoRequestHandler
        from bot.config.constants import UserState

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.MAX_REJECT_RETRIES = 10

        handler = DemoRequestHandler(bot, db, config)

        user = MagicMock()
        user.status = UserState.REJECTED.value
        user.reject_count = 10

        assert handler._can_request_demo(user) is False

    def test_new_user_can_still_request_demo(self):
        """Test NEW users can still request demo."""
        from bot.handlers.callbacks.user import DemoRequestHandler
        from bot.config.constants import UserState

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()

        handler = DemoRequestHandler(bot, db, config)

        user = MagicMock()
        user.status = UserState.NEW.value

        assert handler._can_request_demo(user) is True


class TestCommandsStartRejected:
    """Test /start command for REJECTED users."""

    def test_start_rejected_user(self):
        """Test /start for REJECTED user shows retry message."""
        from bot.handlers.commands import CommandHandler
        from bot.config.constants import UserState

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.MAX_REJECT_RETRIES = 10

        handler = CommandHandler(bot, db, config)

        user = MagicMock()
        user.status = UserState.REJECTED.value
        user.lang = 'ru'
        user.reject_count = 2
        db.get_user.return_value = user

        update = {'message': {'chat': {'id': 123}, 'from': {'id': 123}}}

        with patch.object(handler, '_get_or_create_user', return_value=user):
            handler.handle_start(update, '123')

        # Should call notify_rejected_can_retry
        calls = bot.send_message.call_args_list
        assert len(calls) > 0


class TestNotificationsNewMethods:
    """Test new notification methods."""

    def test_notify_rejected_can_retry_shows_remaining(self):
        """Test notify_rejected_can_retry shows remaining retries."""
        from bot.services.notifications import NotificationService

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.MAX_REJECT_RETRIES = 10

        notifier = NotificationService(bot, db, config)

        user = MagicMock()
        user.lang = 'ru'
        user.reject_count = 7

        notifier.notify_rejected_can_retry('123', user)

        call_args = bot.send_message.call_args[1]
        assert 'Осталось попыток: 3' in call_args['text']
        assert call_args['reply_markup'] is not None

    def test_notify_rejected_can_retry_no_retries_left(self):
        """Test notify_rejected_can_retry with no retries left has no keyboard."""
        from bot.services.notifications import NotificationService

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()
        config.MAX_REJECT_RETRIES = 10

        notifier = NotificationService(bot, db, config)

        user = MagicMock()
        user.lang = 'en'
        user.reject_count = 10

        notifier.notify_rejected_can_retry('123', user)

        call_args = bot.send_message.call_args[1]
        assert 'Retries remaining: 0' in call_args['text']
        assert call_args['reply_markup'] is None

    def test_notify_banned(self):
        """Test notify_banned sends correct message."""
        from bot.services.notifications import NotificationService

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()

        notifier = NotificationService(bot, db, config)

        notifier.notify_banned('123', 'ru')
        assert 'заблокирован' in bot.send_message.call_args[1]['text']

        notifier.notify_banned('123', 'en')
        assert 'banned' in bot.send_message.call_args[1]['text'].lower()

    def test_main_menu_idor_fixed(self):
        """Test that main menu callback_data does not contain chat_id."""
        from bot.services.notifications import NotificationService

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()

        notifier = NotificationService(bot, db, config)
        notifier.notify_main_menu('12345', 'ru')

        keyboard = bot.send_message.call_args[1]['reply_markup']
        buttons = keyboard['inline_keyboard'][0] + keyboard['inline_keyboard'][1]
        for btn in buttons:
            assert '12345' not in btn['callback_data'], f"IDOR leak in {btn['callback_data']}"


class TestDatabaseMigration:
    """Test database migrations on init."""

    def test_active_migrated_to_demo(self, tmp_path):
        """Test that active users are migrated to demo on init."""
        from bot.core.database import Database
        from bot.models import User

        db_path = str(tmp_path / "test_migrate.db")
        # Create full schema first
        db = Database(db_path)
        db.save_user(User(chat_id='1', status='active'))
        db.save_user(User(chat_id='2', status='demo'))

        # Re-init Database to trigger migrations again
        db2 = Database(db_path)

        user1 = db2.get_user('1')
        user2 = db2.get_user('2')
        assert user1.status == 'demo'
        assert user2.status == 'demo'

    def test_banned_without_username_migrated_to_rejected(self, tmp_path):
        """Test that banned users without username are migrated to rejected."""
        from bot.core.database import Database
        from bot.models import User

        db_path = str(tmp_path / "test_reject.db")
        db = Database(db_path)
        db.save_user(User(chat_id='1', username=None, status='banned'))
        db.save_user(User(chat_id='2', username='john', status='banned'))

        # Re-init to trigger migrations
        db2 = Database(db_path)

        user1 = db2.get_user('1')
        user2 = db2.get_user('2')
        assert user1.status == 'rejected'
        assert user1.reject_count == 1
        assert user2.status == 'banned'
