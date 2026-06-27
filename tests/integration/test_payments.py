"""Integration tests for payment flow (Telegram Stars).

Tests cover:
1. Invoice creation (/buy command and plan selection)
2. Pre-checkout query validation
3. Successful payment processing
4. Subscription activation after payment
5. Error cases (malformed payload, unknown user, etc.)
"""

import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest

from bot.config import Settings
from bot.core.database import Database, User
from bot.handlers.payments import PaymentHandler, get_active_plans

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)


class TestPaymentFlowIntegration:
    """Integration tests for payment flow with database and bot mocks."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for tests."""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        yield db_path
        os.unlink(db_path)

    @pytest.fixture
    def mock_config(self):
        """Create mock config with payment settings."""
        config = Mock(spec=Settings)
        config.BOT_TOKEN = 'test_token'
        config.DB_PATH = '/tmp/test.db'
        config.XUI_DB_PATH = '/tmp/test_xui.db'
        config.SUPER_ADMIN_ID = '1652899'
        config.FORUM_ENABLED = False
        config.FORUM_GROUP_ID = None
        config.DEMO_TRAFFIC_GB = 5
        config.DEMO_DAYS = 7
        config.ENTRY_NODE_IP = '1.2.3.4'
        config.REALITY_PUBLIC_KEY = 'test_key'
        config.SNI_VALUE = 'test.com'
        config.SID_VALUE = ''
        config.MODE = 'PM'
        config.is_admin = Mock(return_value=False)
        return config

    @pytest.fixture
    def mock_bot(self, mock_config, temp_db):
        """Create mock bot with database and client."""
        mock_config.DB_PATH = temp_db

        bot = Mock()
        bot.db = Database(temp_db)
        bot.config = mock_config
        bot.handlers = []

        # Mock client methods
        bot.client = Mock()
        bot.client.send_invoice = Mock(return_value={'message_id': 123})
        bot.client.answer_pre_checkout_query = Mock(return_value=True)
        bot.client.answer_callback_query = Mock(return_value=True)

        # Mock bot methods
        bot.send_message = Mock(return_value={'message_id': 123})
        bot.answer_callback_query = Mock(return_value=True)

        return bot

    @pytest.fixture
    def payment_handler(self, mock_bot, mock_config):
        """Create PaymentHandler instance."""
        return PaymentHandler(mock_bot, mock_bot.db, mock_config)

    # ===== Plan Loading Tests =====

    def test_get_active_plans_factory_defaults(self, payment_handler):
        """Test get_active_plans returns factory defaults when no DB/env overrides."""
        plans = get_active_plans(payment_handler.db)
        assert len(plans) == 4
        # Check structure: (months, label, stars)
        assert plans[0][0] == 1
        assert plans[0][1] == '1 месяц'
        assert plans[0][2] == 100
        assert plans[1][0] == 3
        assert plans[1][2] == 270
        assert plans[2][0] == 6
        assert plans[2][2] == 500
        assert plans[3][0] == 12
        assert plans[3][2] == 900

    def test_get_active_plans_env_override(self, payment_handler, monkeypatch):
        """Test get_active_plans respects env var overrides."""
        monkeypatch.setenv('PLAN_1M_STARS', '150')
        monkeypatch.setenv('PLAN_3M_STARS', '400')

        plans = get_active_plans(payment_handler.db)
        assert plans[0][2] == 150  # 1 month overridden
        assert plans[1][2] == 400  # 3 months overridden
        assert plans[2][2] == 500  # 6 months factory default
        assert plans[3][2] == 900  # 12 months factory default

    def test_get_active_plans_db_override(self, payment_handler):
        """Test get_active_plans respects DB overrides (highest priority)."""
        # Set DB values
        payment_handler.db.set_setting('plan_1m_stars', '200')
        payment_handler.db.set_setting('plan_3m_stars', '550')

        plans = get_active_plans(payment_handler.db)
        assert plans[0][2] == 200  # 1 month from DB
        assert plans[1][2] == 550  # 3 months from DB

    # ===== /buy Command and Plan Menu Tests =====

    def test_payment_handler_handles_buy_command(self, payment_handler):
        """Test PaymentHandler handles /buy command."""
        update = {
            'message': {
                'text': '/buy',
                'chat': {'id': 123456},
                'from': {'id': 123456, 'username': 'testuser'}
            }
        }

        assert payment_handler.can_handle(update) is True

    def test_show_plan_menu_sends_keyboard(self, payment_handler):
        """Test _show_plan_menu sends plan selection keyboard."""
        chat_id = '123456'
        payment_handler._show_plan_menu(chat_id)

        # Verify send_message was called
        assert payment_handler.bot.send_message.called
        call_kwargs = payment_handler.bot.send_message.call_args[1]
        assert call_kwargs['chat_id'] == chat_id
        assert '💳 <b>Подписка NekoVPN</b>' in call_kwargs['text']
        assert call_kwargs['parse_mode'] == 'HTML'

        # Check keyboard has 4 plan buttons
        keyboard = call_kwargs['reply_markup']['inline_keyboard']
        assert len(keyboard) == 4

        # Check first button format: "1 месяц — ⭐ 100 (~150₽)"
        first_btn = keyboard[0][0]
        assert '1 месяц' in first_btn['text']
        assert '⭐' in first_btn['text']
        assert 'buy_plan:1' == first_btn['callback_data']

    def test_payment_handler_does_not_handle_regular_messages(self, payment_handler):
        """Test PaymentHandler ignores non-command messages."""
        update = {
            'message': {
                'text': 'Hello',
                'chat': {'id': 123456},
                'from': {'id': 123456}
            }
        }

        assert payment_handler.can_handle(update) is False

    # ===== Callback Routing Tests =====

    def test_payment_handler_handles_buy_menu_callback(self, payment_handler):
        """Test PaymentHandler handles buy_menu callback."""
        update = {
            'callback_query': {
                'data': 'buy_menu',
                'id': 'cb_123',
                'from': {'id': 123456},
                'message': {'chat': {'id': 123456}}
            }
        }

        assert payment_handler.can_handle(update) is True
        payment_handler.handle(update)

        # Verify plan menu was shown
        assert payment_handler.bot.send_message.called
        # Verify callback was answered
        payment_handler.bot.answer_callback_query.assert_called_once_with('cb_123')

    def test_payment_handler_handles_plan_selection_callback(self, payment_handler):
        """Test PaymentHandler handles buy_plan:<months> callback."""
        update = {
            'callback_query': {
                'data': 'buy_plan:3',
                'id': 'cb_456',
                'from': {'id': 123456},
                'message': {'chat': {'id': 123456}}
            }
        }

        assert payment_handler.can_handle(update) is True

    # ===== Invoice Creation Tests =====

    def test_on_plan_selected_sends_invoice(self, payment_handler):
        """Test _on_plan_selected sends invoice with correct payload."""
        callback = {
            'data': 'buy_plan:3',
            'id': 'cb_789',
            'from': {'id': 123456},
            'message': {'chat': {'id': 123456}}
        }

        payment_handler._on_plan_selected(callback)

        # Verify send_invoice was called
        assert payment_handler.bot.client.send_invoice.called
        call_kwargs = payment_handler.bot.client.send_invoice.call_args[1]

        assert call_kwargs['chat_id'] == '123456'
        assert call_kwargs['currency'] == 'XTR'
        assert call_kwargs['provider_token'] == ''
        assert call_kwargs['title'] == 'NekoVPN — 3 месяца'
        assert '3 мес.' in call_kwargs['description']
        assert call_kwargs['payload'] == 'sub:123456:3'
        assert call_kwargs['prices'][0]['label'] == '3 месяца'
        assert call_kwargs['prices'][0]['amount'] == 270

        # Verify callback was answered
        payment_handler.bot.answer_callback_query.assert_called_once_with('cb_789')

    def test_on_plan_selected_invalid_plan(self, payment_handler):
        """Test _on_plan_selected ignores invalid plan months."""
        callback = {
            'data': 'buy_plan:99',  # Invalid plan
            'id': 'cb_invalid',
            'from': {'id': 123456},
            'message': {'chat': {'id': 123456}}
        }

        payment_handler._on_plan_selected(callback)

        # Should not send invoice for invalid plan
        assert not payment_handler.bot.client.send_invoice.called

    def test_on_plan_selected_malformed_callback(self, payment_handler):
        """Test _on_plan_selected handles malformed callback data."""
        callback = {
            'data': 'buy_plan:abc',  # Not a number
            'id': 'cb_malformed',
            'from': {'id': 123456},
            'message': {'chat': {'id': 123456}}
        }

        payment_handler._on_plan_selected(callback)

        # Should not crash or send invoice
        assert not payment_handler.bot.client.send_invoice.called

    def test_on_plan_selected_invoice_failure_sends_error(self, payment_handler):
        """Test _on_plan_selected sends error message when invoice creation fails."""
        # Make send_invoice return None (failure)
        payment_handler.bot.client.send_invoice = Mock(return_value=None)

        callback = {
            'data': 'buy_plan:1',
            'id': 'cb_fail',
            'from': {'id': 123456},
            'message': {'chat': {'id': 123456}}
        }

        payment_handler._on_plan_selected(callback)

        # Verify error message was sent
        assert payment_handler.bot.send_message.called
        call_kwargs = payment_handler.bot.send_message.call_args[1]
        assert '⚠️ Не удалось создать счёт' in call_kwargs['text']

    # ===== Pre-checkout Query Tests =====

    def test_payment_handler_handles_pre_checkout_query(self, payment_handler):
        """Test PaymentHandler handles pre_checkout_query updates."""
        update = {
            'pre_checkout_query': {
                'id': 'pcq_123',
                'invoice_payload': 'sub:123456:3',
                'from': {'id': 123456}
            }
        }

        assert payment_handler.can_handle(update) is True

    def test_on_pre_checkout_valid_payload(self, payment_handler):
        """Test _on_pre_checkout confirms valid payload."""
        pcq = {
            'id': 'pcq_valid',
            'invoice_payload': 'sub:123456:12'
        }

        payment_handler._on_pre_checkout(pcq)

        # Verify answer_pre_checkout_query was called with ok=True
        payment_handler.bot.client.answer_pre_checkout_query.assert_called_once_with(
            'pcq_valid', ok=True
        )

    def test_on_pre_checkout_malformed_payload(self, payment_handler):
        """Test _on_pre_checkout rejects malformed payload."""
        pcq = {
            'id': 'pcq_malformed',
            'invoice_payload': 'invalid:payload'
        }

        payment_handler._on_pre_checkout(pcq)

        # Verify answer_pre_checkout_query was called with ok=False
        payment_handler.bot.client.answer_pre_checkout_query.assert_called_once_with(
            'pcq_malformed',
            ok=False,
            error_message='Не распознан тип подписки.'
        )

    def test_on_pre_checkout_missing_colon_payload(self, payment_handler):
        """Test _on_pre_checkout rejects payload without colons."""
        pcq = {
            'id': 'pcq_nocolon',
            'invoice_payload': 'sub123456'
        }

        payment_handler._on_pre_checkout(pcq)

        # Should reject
        payment_handler.bot.client.answer_pre_checkout_query.assert_called_once_with(
            'pcq_nocolon',
            ok=False,
            error_message='Не распознан тип подписки.'
        )

    def test_on_pre_checkout_empty_payload(self, payment_handler):
        """Test _on_pre_checkout rejects empty payload."""
        pcq = {
            'id': 'pcq_empty',
            'invoice_payload': ''
        }

        payment_handler._on_pre_checkout(pcq)

        # Should reject
        payment_handler.bot.client.answer_pre_checkout_query.assert_called_once_with(
            'pcq_empty',
            ok=False,
            error_message='Не распознан тип подписки.'
        )

    # ===== Successful Payment Tests =====

    def test_payment_handler_handles_successful_payment(self, payment_handler):
        """Test PaymentHandler handles successful_payment in message."""
        update = {
            'message': {
                'chat': {'id': 123456},
                'from': {'id': 123456},
                'successful_payment': {
                    'invoice_payload': 'sub:123456:3',
                    'total_amount': 270,
                    'telegram_payment_charge_id': 'tx_12345'
                }
            }
        }

        assert payment_handler.can_handle(update) is True

    def test_on_successful_payment_activates_subscription(self, payment_handler):
        """Test _on_successful_payment extends user subscription."""
        # Create user with existing subscription expiring soon
        expiry = (datetime.now() + timedelta(days=5)).isoformat()
        user = User(
            chat_id='123456',
            username='payuser',
            subscription_expiry=expiry,
            email='pay@nekovo.ru'
        )
        payment_handler.db.save_user(user)

        # Also create a subscription row for the user
        with payment_handler.db._connect() as conn:
            conn.execute(
                "INSERT INTO subscriptions (chat_id, plan_type, started_at, expires_at, is_active) "
                "VALUES (?, ?, ?, ?, 1)",
                ('123456', 'paid', expiry, expiry)
            )

        message = {
            'chat': {'id': 123456},
            'successful_payment': {
                'invoice_payload': 'sub:123456:3',
                'total_amount': 270,
                'telegram_payment_charge_id': 'tx_abc123'
            }
        }

        payment_handler._on_successful_payment(message)

        # Verify user subscription was extended
        updated_user = payment_handler.db.get_user('123456')
        assert updated_user is not None

        # Should be extended from max(now, old_expiry) + 90 days
        new_expiry = datetime.fromisoformat(updated_user.subscription_expiry)
        expected_min = datetime.now() + timedelta(days=90)
        assert new_expiry >= expected_min.replace(microsecond=0)

        # Verify success messages were sent (user + admin)
        assert payment_handler.bot.send_message.call_count >= 1

        # Find the user's success message (sent to their chat_id)
        user_calls = [
            c for c in payment_handler.bot.send_message.call_args_list
            if c[1].get('chat_id') == '123456'
        ]
        assert len(user_calls) > 0
        user_msg = user_calls[0][1]['text']
        assert '🎉 <b>Подписка активирована!</b>' in user_msg
        assert '3 мес.' in user_msg
        assert '270 ⭐' in user_msg

    def test_on_successful_payment_new_user_no_existing_subscription(self, payment_handler):
        """Test _on_successful_payment creates subscription for new user."""
        # Create user without subscription_expiry
        user = User(
            chat_id='789012',
            username='newpayer',
            email='new@nekovo.ru'
        )
        payment_handler.db.save_user(user)

        message = {
            'chat': {'id': 789012},
            'successful_payment': {
                'invoice_payload': 'sub:789012:1',
                'total_amount': 100,
                'telegram_payment_charge_id': 'tx_new123'
            }
        }

        payment_handler._on_successful_payment(message)

        # Verify user got subscription
        updated_user = payment_handler.db.get_user('789012')
        assert updated_user.subscription_expiry is not None

        new_expiry = datetime.fromisoformat(updated_user.subscription_expiry)
        expected_min = datetime.now() + timedelta(days=29)  # Allow 1 day tolerance
        expected_max = datetime.now() + timedelta(days=31)
        assert expected_min <= new_expiry <= expected_max

        # Verify success message was sent to user
        user_calls = [
            c for c in payment_handler.bot.send_message.call_args_list
            if c[1].get('chat_id') == '789012'
        ]
        assert len(user_calls) > 0
        user_msg = user_calls[0][1]['text']
        assert '🎉 <b>Подписка активирована!</b>' in user_msg
        assert '1 мес.' in user_msg

    def test_on_successful_payment_unknown_user(self, payment_handler):
        """Test _on_successful_payment handles unknown user gracefully."""
        # Don't create user in DB

        message = {
            'chat': {'id': 999999},
            'successful_payment': {
                'invoice_payload': 'sub:999999:1',
                'total_amount': 100,
                'telegram_payment_charge_id': 'tx_unknown'
            }
        }

        # Should not crash
        payment_handler._on_successful_payment(message)

        # No success message should be sent for unknown user
        assert not payment_handler.bot.send_message.called

    def test_on_successful_payment_payload_mismatch(self, payment_handler):
        """Test _on_successful_payment handles payload chat mismatch."""
        # Create user
        user = User(
            chat_id='111222',
            username='mismatch',
            email='mismatch@nekovo.ru'
        )
        payment_handler.db.save_user(user)

        # Payload says one chat, message says another
        message = {
            'chat': {'id': 111222},
            'successful_payment': {
                'invoice_payload': 'sub:999999:1',  # Wrong chat
                'total_amount': 100,
                'telegram_payment_charge_id': 'tx_mismatch'
            }
        }

        # Should still honour the event chat
        payment_handler._on_successful_payment(message)

        # User 111222 should get subscription
        updated_user = payment_handler.db.get_user('111222')
        assert updated_user.subscription_expiry is not None

    def test_on_successful_payment_malformed_payload(self, payment_handler):
        """Test _on_successful_payment handles malformed payload."""
        user = User(chat_id='444555', username='badpayload')
        payment_handler.db.save_user(user)

        message = {
            'chat': {'id': 444555},
            'successful_payment': {
                'invoice_payload': 'invalid_payload_format',
                'total_amount': 100,
                'telegram_payment_charge_id': 'tx_bad'
            }
        }

        # Should not crash
        payment_handler._on_successful_payment(message)

        # No activation should happen
        updated_user = payment_handler.db.get_user('444555')
        assert updated_user.subscription_expiry is None

    def test_on_successful_payment_logs_admin_action(self, payment_handler):
        """Test _on_successful_payment logs admin action for audit."""
        user = User(
            chat_id='333444',
            username='auditor',
            email='audit@nekovo.ru'
        )
        payment_handler.db.save_user(user)

        message = {
            'chat': {'id': 333444},
            'successful_payment': {
                'invoice_payload': 'sub:333444:6',
                'total_amount': 500,
                'telegram_payment_charge_id': 'tx_audit'
            }
        }

        payment_handler._on_successful_payment(message)

        # Verify admin action was logged
        # Check the admin_actions table (columns: admin_id, action, target_id, details)
        with payment_handler.db._connect() as conn:
            result = conn.execute(
                "SELECT * FROM admin_actions WHERE admin_id = ? AND action = ? AND target_id = ? ORDER BY id DESC LIMIT 1",
                ('self-serve', 'payment_stars', '333444')
            ).fetchone()

        assert result is not None
        assert result['admin_id'] == 'self-serve'
        assert result['action'] == 'payment_stars'
        assert '+6 мес' in result['details']
        assert '500 ⭐' in result['details']

    def test_on_successful_payment_notifies_admin(self, payment_handler):
        """Test _on_successful_payment sends notification to admin."""
        user = User(
            chat_id='777888',
            username='richuser',
            email='rich@nekovo.ru'
        )
        payment_handler.db.save_user(user)

        message = {
            'chat': {'id': 777888},
            'successful_payment': {
                'invoice_payload': 'sub:777888:12',
                'total_amount': 900,
                'telegram_payment_charge_id': 'tx_rich'
            }
        }

        payment_handler._on_successful_payment(message)

        # Find the admin notification call
        admin_calls = [
            c for c in payment_handler.bot.send_message.call_args_list
            if c[1].get('chat_id') == '1652899'
        ]

        assert len(admin_calls) > 0
        admin_msg = admin_calls[0][1]['text']
        assert '@richuser' in admin_msg or '777888' in admin_msg
        assert '12 мес' in admin_msg
        assert '900 ⭐' in admin_msg

    # ===== Subscription Expiry Stacking Tests =====

    def test_on_successful_payment_stacks_subscription(self, payment_handler):
        """Test that payments stack on existing subscription (not from today)."""
        # Set expiry 60 days in the future
        far_expiry = (datetime.now() + timedelta(days=60)).isoformat()
        user = User(
            chat_id='stackuser',
            username='stacker',
            subscription_expiry=far_expiry,
            email='stack@nekovo.ru'
        )
        payment_handler.db.save_user(user)

        # Buy 1 more month (30 days)
        message = {
            'chat': {'id': 'stackuser'},
            'successful_payment': {
                'invoice_payload': 'sub:stackuser:1',
                'total_amount': 100,
                'telegram_payment_charge_id': 'tx_stack'
            }
        }

        payment_handler._on_successful_payment(message)

        # Should be ~90 days from now (60 existing + 30 new), not 30
        updated_user = payment_handler.db.get_user('stackuser')
        new_expiry = datetime.fromisoformat(updated_user.subscription_expiry)
        expected_min = datetime.now() + timedelta(days=89)  # Allow 1 day tolerance

        assert new_expiry >= expected_min.replace(microsecond=0)

    # ===== Error Handling Tests =====

    def test_handle_update_without_chat_id(self, payment_handler):
        """Test handler handles updates without chat_id gracefully."""
        callback = {
            'data': 'buy_plan:1',
            'id': 'cb_nochat',
            'from': {},  # No id
            'message': {'chat': {}}  # No id
        }

        # Should not crash
        payment_handler._on_plan_selected(callback)

        # Should not send invoice without chat_id
        assert not payment_handler.bot.client.send_invoice.called

    def test_on_successful_payment_with_invalid_expiry_date(self, payment_handler):
        """Test _on_successful_payment handles user with invalid expiry date."""
        # Create user with malformed expiry
        user = User(
            chat_id='baddate',
            username='baddateuser',
            subscription_expiry='not-a-valid-iso-date',
            email='bad@nekovo.ru'
        )
        payment_handler.db.save_user(user)

        message = {
            'chat': {'id': 'baddate'},
            'successful_payment': {
                'invoice_payload': 'sub:baddate:1',
                'total_amount': 100,
                'telegram_payment_charge_id': 'tx_baddate'
            }
        }

        # Should not crash, should use current time as base
        payment_handler._on_successful_payment(message)

        updated_user = payment_handler.db.get_user('baddate')
        assert updated_user.subscription_expiry is not None

        # Should be ~30 days from now
        new_expiry = datetime.fromisoformat(updated_user.subscription_expiry)
        expected_min = datetime.now() + timedelta(days=29)
        assert new_expiry >= expected_min.replace(microsecond=0)

    # ===== Full Flow Integration Tests =====

    def test_full_payment_flow_from_buy_to_activation(self, payment_handler):
        """Test complete flow: /buy -> plan selection -> payment -> activation."""
        chat_id = 'fullflow'

        # Step 1: /buy command
        buy_update = {
            'message': {
                'text': '/buy',
                'chat': {'id': chat_id},
                'from': {'id': chat_id, 'username': 'fullflowuser'}
            }
        }

        payment_handler.handle(buy_update)
        assert payment_handler.bot.send_message.called
        payment_handler.bot.send_message.reset_mock()

        # Step 2: User selects 3-month plan
        plan_callback = {
            'callback_query': {
                'data': 'buy_plan:3',
                'id': 'cb_full',
                'from': {'id': chat_id},
                'message': {'chat': {'id': chat_id}}
            }
        }

        payment_handler.handle(plan_callback)
        assert payment_handler.bot.client.send_invoice.called
        invoice_call = payment_handler.bot.client.send_invoice.call_args[1]
        assert invoice_call['payload'] == f'sub:{chat_id}:3'
        payment_handler.bot.client.send_invoice.reset_mock()

        # Step 3: Pre-checkout query
        pcq_update = {
            'pre_checkout_query': {
                'id': 'pcq_full',
                'invoice_payload': f'sub:{chat_id}:3'
            }
        }

        payment_handler.handle(pcq_update)
        payment_handler.bot.client.answer_pre_checkout_query.assert_called_with(
            'pcq_full', ok=True
        )
        payment_handler.bot.client.answer_pre_checkout_query.reset_mock()

        # Step 4: Successful payment
        user = User(
            chat_id=chat_id,
            username='fullflowuser',
            email='full@nekovo.ru'
        )
        payment_handler.db.save_user(user)

        payment_update = {
            'message': {
                'chat': {'id': chat_id},
                'successful_payment': {
                    'invoice_payload': f'sub:{chat_id}:3',
                    'total_amount': 270,
                    'telegram_payment_charge_id': 'tx_fullflow'
                }
            }
        }

        payment_handler.handle(payment_update)

        # Verify activation
        updated_user = payment_handler.db.get_user(chat_id)
        assert updated_user.subscription_expiry is not None

        # Verify success message sent to user
        user_calls = [
            c for c in payment_handler.bot.send_message.call_args_list
            if c[1].get('chat_id') == chat_id
        ]
        assert len(user_calls) > 0
        success_msg = user_calls[0][1]['text']
        assert '🎉 <b>Подписка активирована!</b>' in success_msg
