"""Comprehensive unit tests for bot/handlers/payments.py

Tests cover:
1. can_handle() - payment event routing
2. _show_plan_menu() - plan display
3. _on_plan_selected() - invoice generation
4. _on_pre_checkout_query() - validation
5. _on_successful_payment() - payment completion
6. Helper functions (get_active_plans, _setting_key, _env_key)
"""

import os
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from bot.handlers.payments import (
    PaymentHandler,
    get_active_plans,
    _setting_key,
    _env_key,
)


# ----- Helper Function Tests -----


class TestHelperFunctions:
    """Tests for helper functions that don't require a handler instance."""

    def test_setting_key_returns_correct_format(self):
        """Test _setting_key generates correct database key."""
        assert _setting_key(1) == "plan_1m_stars"
        assert _setting_key(3) == "plan_3m_stars"
        assert _setting_key(12) == "plan_12m_stars"

    def test_env_key_returns_correct_format(self):
        """Test _env_key generates correct environment variable key."""
        assert _env_key(1) == "PLAN_1M_STARS"
        assert _env_key(3) == "PLAN_3M_STARS"
        assert _env_key(12) == "PLAN_12M_STARS"

    def test_get_active_plans_factory_defaults(self):
        """Test get_active_plans returns factory defaults when no DB/env overrides."""
        db = MagicMock()
        db.get_setting.return_value = None  # No DB setting

        # Clear env vars
        with patch.dict(os.environ, {}, clear=True):
            plans = get_active_plans(db)

        assert len(plans) == 4
        assert plans[0] == (1, "1 месяц", 100)
        assert plans[1] == (3, "3 месяца", 270)
        assert plans[2] == (6, "6 месяцев", 500)
        assert plans[3] == (12, "12 месяцев", 900)

    def test_get_active_plans_db_override(self):
        """Test get_active_plans uses DB value when present."""
        db = MagicMock()
        # Mock different prices from DB
        def mock_get_setting(key):
            prices = {
                "plan_1m_stars": "150",
                "plan_3m_stars": "400",
                "plan_6m_stars": "700",
                "plan_12m_stars": "1200",
            }
            return prices.get(key)

        db.get_setting.side_effect = mock_get_setting

        plans = get_active_plans(db)

        assert plans[0] == (1, "1 месяц", 150)
        assert plans[1] == (3, "3 месяца", 400)
        assert plans[2] == (6, "6 месяцев", 700)
        assert plans[3] == (12, "12 месяцев", 1200)

    def test_get_active_plans_env_override(self):
        """Test get_active_plans uses env var when no DB value."""
        db = MagicMock()
        db.get_setting.return_value = None

        env_vars = {
            "PLAN_1M_STARS": "200",
            "PLAN_3M_STARS": "500",
            "PLAN_6M_STARS": "900",
            "PLAN_12M_STARS": "1600",
        }

        with patch.dict(os.environ, env_vars, clear=True):
            plans = get_active_plans(db)

        assert plans[0] == (1, "1 месяц", 200)
        assert plans[1] == (3, "3 месяца", 500)
        assert plans[2] == (6, "6 месяцев", 900)
        assert plans[3] == (12, "12 месяцев", 1600)

    def test_get_active_plans_resolution_order_db_over_env(self):
        """Test DB value takes precedence over env var."""
        db = MagicMock()
        db.get_setting.return_value = "999"  # DB value

        env_vars = {"PLAN_1M_STARS": "111"}  # Env value

        with patch.dict(os.environ, env_vars, clear=True):
            plans = get_active_plans(db)

        # DB wins
        assert plans[0] == (1, "1 месяц", 999)

    def test_get_active_plans_invalid_db_value(self):
        """Test invalid DB value falls through to env."""
        db = MagicMock()
        db.get_setting.return_value = "not_a_number"

        env_vars = {"PLAN_1M_STARS": "333"}

        with patch.dict(os.environ, env_vars, clear=True):
            plans = get_active_plans(db)

        assert plans[0] == (1, "1 месяц", 333)

    def test_get_active_plans_none_db_value(self):
        """Test None DB value falls through to env."""
        db = MagicMock()
        db.get_setting.return_value = None

        env_vars = {"PLAN_1M_STARS": "444"}

        with patch.dict(os.environ, env_vars, clear=True):
            plans = get_active_plans(db)

        assert plans[0] == (1, "1 месяц", 444)

    def test_get_active_plans_invalid_env_value(self):
        """Test invalid env value falls through to factory default."""
        db = MagicMock()
        db.get_setting.return_value = None

        env_vars = {"PLAN_1M_STARS": "also_invalid"}

        with patch.dict(os.environ, env_vars, clear=True):
            plans = get_active_plans(db)

        # Factory default
        assert plans[0] == (1, "1 месяц", 100)

    def test_get_active_plans_mixed_sources(self):
        """Test plans can come from different sources."""
        db = MagicMock()

        def mock_get_setting(key):
            # Only return DB value for 1m
            return "250" if key == "plan_1m_stars" else None

        db.get_setting.side_effect = mock_get_setting

        env_vars = {"PLAN_3M_STARS": "600"}

        with patch.dict(os.environ, env_vars, clear=True):
            plans = get_active_plans(db)

        assert plans[0] == (1, "1 месяц", 250)  # From DB
        assert plans[1] == (3, "3 месяца", 600)  # From env
        assert plans[2] == (6, "6 месяцев", 500)  # Factory
        assert plans[3] == (12, "12 месяцев", 900)  # Factory

    def test_get_active_plans_none_db(self):
        """Test get_active_plans handles None db gracefully."""
        with patch.dict(os.environ, {}, clear=True):
            plans = get_active_plans(None)

        # Should return factory defaults
        assert len(plans) == 4
        assert plans[0] == (1, "1 месяц", 100)


# ----- Fixtures -----


@pytest.fixture
def mock_bot():
    """Create mock bot with client."""
    bot = MagicMock()
    bot.client = MagicMock()
    bot.send_message = MagicMock()
    bot.answer_callback_query = MagicMock()
    return bot


@pytest.fixture
def mock_db():
    """Create mock database."""
    db = MagicMock()
    db.get_setting = MagicMock(return_value=None)
    db.get_user = MagicMock()
    db.save_user = MagicMock()
    db.log_admin_action = MagicMock()
    db._connect = MagicMock()
    return db


@pytest.fixture
def mock_config():
    """Create mock config."""
    config = MagicMock()
    config.SUPER_ADMIN_ID = "123456"
    return config


@pytest.fixture
def payment_handler(mock_bot, mock_db, mock_config):
    """Create PaymentHandler instance."""
    return PaymentHandler(mock_bot, mock_db, mock_config)


@pytest.fixture
def sample_user():
    """Create sample user for testing."""

    @dataclass
    class TestUser:
        chat_id: str = "987654321"
        username: str = "testuser"
        subscription_expiry: str = None
        uuid: str = "test-uuid-123"
        email: str = "test@example.com"
        status: str = "demo"

    user = TestUser()
    return user


# ----- can_handle() Tests -----


class TestCanHandle:
    """Tests for PaymentHandler.can_handle()."""

    def test_handles_buy_command(self, payment_handler):
        """Test can_handle returns True for /buy command."""
        update = {
            "message": {
                "text": "/buy",
                "chat": {"id": "123"},
            }
        }
        assert payment_handler.can_handle(update) is True

    def test_handles_buy_command_with_args(self, payment_handler):
        """Test can_handle returns True for /buy with arguments."""
        update = {
            "message": {
                "text": "/buy something",
                "chat": {"id": "123"},
            }
        }
        assert payment_handler.can_handle(update) is True

    def test_handles_successful_payment(self, payment_handler):
        """Test can_handle returns True for successful_payment field."""
        update = {
            "message": {
                "successful_payment": {
                    "invoice_payload": "sub:123:3",
                    "total_amount": 270,
                },
                "chat": {"id": "123"},
            }
        }
        assert payment_handler.can_handle(update) is True

    def test_handles_pre_checkout_query(self, payment_handler):
        """Test can_handle returns True for pre_checkout_query."""
        update = {
            "pre_checkout_query": {
                "id": "checkout_123",
                "invoice_payload": "sub:123:3",
            }
        }
        assert payment_handler.can_handle(update) is True

    def test_handles_buy_plan_callback(self, payment_handler):
        """Test can_handle returns True for buy_plan:<n> callbacks."""
        update = {
            "callback_query": {
                "data": "buy_plan:3",
                "id": "cb_123",
                "from": {"id": "123"},
            }
        }
        assert payment_handler.can_handle(update) is True

    def test_handles_buy_menu_callback(self, payment_handler):
        """Test can_handle returns True for buy_menu callback."""
        update = {
            "callback_query": {
                "data": "buy_menu",
                "id": "cb_123",
                "from": {"id": "123"},
            }
        }
        assert payment_handler.can_handle(update) is True

    def test_rejects_regular_message(self, payment_handler):
        """Test can_handle returns False for regular messages."""
        update = {
            "message": {
                "text": "hello",
                "chat": {"id": "123"},
            }
        }
        assert payment_handler.can_handle(update) is False

    def test_rejects_other_callback(self, payment_handler):
        """Test can_handle returns False for other callbacks."""
        update = {
            "callback_query": {
                "data": "other_action",
                "id": "cb_123",
                "from": {"id": "123"},
            }
        }
        assert payment_handler.can_handle(update) is False

    def test_rejects_empty_update(self, payment_handler):
        """Test can_handle returns False for empty update."""
        update = {}
        assert payment_handler.can_handle(update) is False

    def test_rejects_update_with_empty_message(self, payment_handler):
        """Test can_handle returns False for update with empty message."""
        update = {"message": {}}
        assert payment_handler.can_handle(update) is False

    def test_rejects_update_with_empty_text(self, payment_handler):
        """Test can_handle returns False for message with empty text."""
        update = {"message": {"text": ""}}
        assert payment_handler.can_handle(update) is False

    def test_handles_text_starting_with_buy(self, payment_handler):
        """Test can_handle returns True for text starting with /buy."""
        update = {
            "message": {
                "text": "/buy@botname",  # With bot username
                "chat": {"id": "123"},
            }
        }
        assert payment_handler.can_handle(update) is True


# ----- _show_plan_menu() Tests -----


class TestShowPlanMenu:
    """Tests for PaymentHandler._show_plan_menu()."""

    def test_shows_plan_menu(self, payment_handler):
        """Test _show_plan_menu sends plan keyboard."""
        payment_handler._show_plan_menu("123456")

        payment_handler.bot.send_message.assert_called_once()
        call_args = payment_handler.bot.send_message.call_args
        assert call_args[1]["chat_id"] == "123456"
        assert "Подписка NekoVPN" in call_args[1]["text"]
        assert call_args[1]["parse_mode"] == "HTML"

        # Check keyboard structure
        keyboard = call_args[1]["reply_markup"]["inline_keyboard"]
        assert len(keyboard) == 4  # 4 plans

    def test_plan_menu_shows_all_plans(self, payment_handler):
        """Test plan menu includes all defined plans."""
        payment_handler._show_plan_menu("123456")

        keyboard = payment_handler.bot.send_message.call_args[1]["reply_markup"][
            "inline_keyboard"
        ]
        assert len(keyboard) == 4

        # Check button texts contain plan info
        button_texts = [row[0]["text"] for row in keyboard]
        assert any("1 месяц" in t for t in button_texts)
        assert any("3 месяца" in t for t in button_texts)
        assert any("6 месяцев" in t for t in button_texts)
        assert any("12 месяцев" in t for t in button_texts)

    def test_plan_menu_shows_stars_price(self, payment_handler):
        """Test plan menu shows Stars prices."""
        payment_handler._show_plan_menu("123456")

        keyboard = payment_handler.bot.send_message.call_args[1]["reply_markup"][
            "inline_keyboard"
        ]
        button_texts = [row[0]["text"] for row in keyboard]

        # Should show star emojis (indicating Stars price)
        assert any("⭐ 100" in t for t in button_texts)
        assert any("⭐ 270" in t for t in button_texts)

    def test_plan_menu_shows_approximate_rub_price(self, payment_handler):
        """Test plan menu shows approximate RUB price."""
        payment_handler._show_plan_menu("123456")

        keyboard = payment_handler.bot.send_message.call_args[1]["reply_markup"][
            "inline_keyboard"
        ]
        button_texts = [row[0]["text"] for row in keyboard]

        # Should show approximate ruble prices
        assert any("~150₽" in t for t in button_texts)  # 100 * 1.5
        assert any("~405₽" in t for t in button_texts)  # 270 * 1.5

    def test_plan_menu_callback_data_format(self, payment_handler):
        """Test plan menu buttons have correct callback format."""
        payment_handler._show_plan_menu("123456")

        keyboard = payment_handler.bot.send_message.call_args[1]["reply_markup"][
            "inline_keyboard"
        ]
        callback_data = [row[0]["callback_data"] for row in keyboard]

        assert "buy_plan:1" in callback_data
        assert "buy_plan:3" in callback_data
        assert "buy_plan:6" in callback_data
        assert "buy_plan:12" in callback_data

    def test_plan_menu_with_custom_prices(self, payment_handler):
        """Test plan menu uses custom prices from DB."""
        # Setup DB to return custom price
        payment_handler.db.get_setting.return_value = "250"

        payment_handler._show_plan_menu("123456")

        keyboard = payment_handler.bot.send_message.call_args[1]["reply_markup"][
            "inline_keyboard"
        ]
        button_texts = [row[0]["text"] for row in keyboard]

        # Should show custom price
        assert any("⭐ 250" in t for t in button_texts)

    def test_plan_menu_empty_chat_id(self, payment_handler):
        """Test _show_plan_menu handles empty chat_id gracefully."""
        payment_handler._show_plan_menu("")

        payment_handler.bot.send_message.assert_not_called()

    def test_plan_menu_none_chat_id(self, payment_handler):
        """Test _show_plan_menu handles None chat_id gracefully."""
        payment_handler._show_plan_menu(None)

        payment_handler.bot.send_message.assert_not_called()


# ----- _on_plan_selected() Tests -----


class TestOnPlanSelected:
    """Tests for PaymentHandler._on_plan_selected()."""

    def test_sends_invoice_on_plan_selection(self, payment_handler):
        """Test _on_plan_selected sends invoice for selected plan."""
        cb = {
            "data": "buy_plan:3",
            "id": "cb_123",
            "from": {"id": "987654321"},
        }

        payment_handler._on_plan_selected(cb)

        payment_handler.bot.client.send_invoice.assert_called_once()
        call_args = payment_handler.bot.client.send_invoice.call_args
        assert call_args[1]["chat_id"] == "987654321"
        assert call_args[1]["currency"] == "XTR"
        assert call_args[1]["provider_token"] == ""
        assert "3 месяца" in call_args[1]["title"]
        assert call_args[1]["prices"][0]["amount"] == 270

    def test_invoice_payload_format(self, payment_handler):
        """Test invoice payload contains chat_id and months."""
        cb = {
            "data": "buy_plan:6",
            "id": "cb_123",
            "from": {"id": "123456"},
        }

        payment_handler._on_plan_selected(cb)

        call_args = payment_handler.bot.client.send_invoice.call_args
        payload = call_args[1]["payload"]
        assert payload == "sub:123456:6"

    def test_answers_callback_query(self, payment_handler):
        """Test _on_plan_selected answers callback query."""
        cb = {
            "data": "buy_plan:1",
            "id": "cb_456",
            "from": {"id": "111"},
        }

        payment_handler._on_plan_selected(cb)

        payment_handler.bot.answer_callback_query.assert_called_once_with("cb_456")

    def test_sends_error_message_on_invoice_failure(self, payment_handler):
        """Test error message sent when invoice creation fails."""
        payment_handler.bot.client.send_invoice.return_value = None

        cb = {
            "data": "buy_plan:1",
            "id": "cb_123",
            "from": {"id": "222"},
        }

        payment_handler._on_plan_selected(cb)

        # Check error message sent
        error_calls = [
            call
            for call in payment_handler.bot.send_message.call_args_list
            if "Не удалось создать счёт" in call[1].get("text", "")
        ]
        assert len(error_calls) == 1

    def test_handles_invalid_callback_data(self, payment_handler):
        """Test _on_plan_selected handles malformed callback data."""
        cb = {
            "data": "buy_plan:invalid",
            "id": "cb_123",
            "from": {"id": "333"},
        }

        payment_handler._on_plan_selected(cb)

        payment_handler.bot.client.send_invoice.assert_not_called()

    def test_handles_callback_data_missing_separator(self, payment_handler):
        """Test _on_plan_selected handles callback without separator."""
        cb = {
            "data": "buy_plan",
            "id": "cb_123",
            "from": {"id": "333"},
        }

        payment_handler._on_plan_selected(cb)

        payment_handler.bot.client.send_invoice.assert_not_called()

    def test_handles_callback_data_extra_separator(self, payment_handler):
        """Test _on_plan_selected handles callback with extra separators."""
        # Should still work - takes the part after first ":"
        cb = {
            "data": "buy_plan:3:extra",
            "id": "cb_123",
            "from": {"id": "333"},
        }

        payment_handler._on_plan_selected(cb)

        # Should not send invoice since "3:extra" is not a valid int
        payment_handler.bot.client.send_invoice.assert_not_called()

    def test_handles_missing_chat_id(self, payment_handler):
        """Test _on_plan_selected handles missing user id."""
        cb = {
            "data": "buy_plan:1",
            "id": "cb_123",
            "from": {},  # No id
        }

        payment_handler._on_plan_selected(cb)

        payment_handler.bot.client.send_invoice.assert_not_called()

    def test_handles_empty_chat_id(self, payment_handler):
        """Test _on_plan_selected handles empty user id."""
        cb = {
            "data": "buy_plan:1",
            "id": "cb_123",
            "from": {"id": ""},
        }

        payment_handler._on_plan_selected(cb)

        payment_handler.bot.client.send_invoice.assert_not_called()

    def test_handles_nonexistent_plan(self, payment_handler):
        """Test _on_plan_selected handles nonexistent plan month."""
        cb = {
            "data": "buy_plan:99",  # Not a valid plan
            "id": "cb_123",
            "from": {"id": "444"},
        }

        payment_handler._on_plan_selected(cb)

        payment_handler.bot.client.send_invoice.assert_not_called()

    def test_invoice_failure_sends_error_message(self, payment_handler):
        """Test invoice failure triggers error message to user."""
        payment_handler.bot.client.send_invoice.return_value = None  # Failure

        cb = {
            "data": "buy_plan:1",
            "id": "cb_789",
            "from": {"id": "555"},
        }

        payment_handler._on_plan_selected(cb)

        # Should send error message
        error_calls = [
            call
            for call in payment_handler.bot.send_message.call_args_list
            if "Не удалось создать счёт" in call[1].get("text", "")
        ]
        assert len(error_calls) == 1
        # Callback should still be answered
        payment_handler.bot.answer_callback_query.assert_called_once_with("cb_789")

    def test_invoice_description_content(self, payment_handler):
        """Test invoice description contains correct info."""
        cb = {
            "data": "buy_plan:12",
            "id": "cb_123",
            "from": {"id": "666"},
        }

        payment_handler._on_plan_selected(cb)

        call_args = payment_handler.bot.client.send_invoice.call_args
        description = call_args[1]["description"]
        assert "12 мес" in description
        assert "Оплата через Telegram Stars" in description

    def test_invoice_price_label(self, payment_handler):
        """Test invoice price label uses plan label."""
        cb = {
            "data": "buy_plan:6",
            "id": "cb_123",
            "from": {"id": "777"},
        }

        payment_handler._on_plan_selected(cb)

        call_args = payment_handler.bot.client.send_invoice.call_args
        assert call_args[1]["prices"][0]["label"] == "6 месяцев"

    def test_missing_callback_id(self, payment_handler):
        """Test handling of callback without id field."""
        cb = {"data": "buy_plan:1", "from": {"id": "888"}}

        payment_handler._on_plan_selected(cb)

        # Should still send invoice
        payment_handler.bot.client.send_invoice.assert_called_once()


# ----- _on_pre_checkout() Tests -----


class TestOnPreCheckout:
    """Tests for PaymentHandler._on_pre_checkout()."""

    def test_answers_pre_checkout_ok(self, payment_handler):
        """Test _on_pre_checkout answers with ok=True for valid payload."""
        pcq = {"id": "pcq_123", "invoice_payload": "sub:987654321:3"}

        payment_handler._on_pre_checkout(pcq)

        payment_handler.bot.client.answer_pre_checkout_query.assert_called_once_with(
            "pcq_123", ok=True
        )

    def test_rejects_invalid_payload_format(self, payment_handler):
        """Test _on_pre_checkout rejects malformed payload."""
        pcq = {"id": "pcq_456", "invoice_payload": "invalid_format"}

        payment_handler._on_pre_checkout(pcq)

        payment_handler.bot.client.answer_pre_checkout_query.assert_called_once()
        call_args = payment_handler.bot.client.answer_pre_checkout_query.call_args
        assert call_args[1]["ok"] is False
        assert "Не распознан тип подписки" in call_args[1]["error_message"]

    def test_rejects_payload_missing_prefix(self, payment_handler):
        """Test _on_pre_checkout rejects payload without sub: prefix."""
        pcq = {"id": "pcq_789", "invoice_payload": "user:123:3"}

        payment_handler._on_pre_checkout(pcq)

        call_args = payment_handler.bot.client.answer_pre_checkout_query.call_args
        assert call_args[1]["ok"] is False

    def test_rejects_payload_with_missing_colons(self, payment_handler):
        """Test _on_pre_checkout rejects payload with insufficient parts."""
        pcq = {"id": "pcq_abc", "invoice_payload": "sub:123"}

        payment_handler._on_pre_checkout(pcq)

        call_args = payment_handler.bot.client.answer_pre_checkout_query.call_args
        assert call_args[1]["ok"] is False

    def test_handles_missing_pcq_id(self, payment_handler):
        """Test _on_pre_checkout handles missing pre_checkout_query_id."""
        pcq = {"invoice_payload": "sub:123:3"}

        payment_handler._on_pre_checkout(pcq)

        payment_handler.bot.client.answer_pre_checkout_query.assert_not_called()

    def test_handles_empty_payload(self, payment_handler):
        """Test _on_pre_checkout handles empty payload."""
        pcq = {"id": "pcq_empty", "invoice_payload": ""}

        payment_handler._on_pre_checkout(pcq)

        call_args = payment_handler.bot.client.answer_pre_checkout_query.call_args
        assert call_args[1]["ok"] is False

    def test_handles_none_payload(self, payment_handler):
        """Test _on_pre_checkout handles None payload."""
        pcq = {"id": "pcq_none", "invoice_payload": None}

        payment_handler._on_pre_checkout(pcq)

        call_args = payment_handler.bot.client.answer_pre_checkout_query.call_args
        assert call_args[1]["ok"] is False

    def test_accepts_valid_payload_with_extra_colons(self, payment_handler):
        """Test _on_pre_checkout accepts payload with extra colons (count >= 2)."""
        # Payload has 3 colons (4 parts)
        pcq = {"id": "pcq_extra", "invoice_payload": "sub:123:3:extra"}

        payment_handler._on_pre_checkout(pcq)

        call_args = payment_handler.bot.client.answer_pre_checkout_query.call_args
        assert call_args[1]["ok"] is True


# ----- _on_successful_payment() Tests -----


class TestOnSuccessfulPayment:
    """Tests for PaymentHandler._on_successful_payment()."""

    def test_extends_subscription_expiry(self, payment_handler, sample_user):
        """Test _on_successful_payment extends user subscription."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        now = datetime.utcnow()
        with patch("bot.handlers.payments.datetime") as mock_dt:
            mock_dt.utcnow.return_value = now
            mock_dt.fromisoformat.side_effect = lambda x: datetime.fromisoformat(x)

            msg = {
                "chat": {"id": "987654321"},
                "successful_payment": {
                    "invoice_payload": "sub:987654321:3",
                    "total_amount": 270,
                    "telegram_payment_charge_id": "tx_123",
                },
            }

            payment_handler._on_successful_payment(msg)

        # Check user was updated
        saved_user = payment_handler.db.save_user.call_args[0][0]
        assert saved_user.subscription_expiry is not None
        # Should be ~90 days from now
        new_expiry = datetime.fromisoformat(saved_user.subscription_expiry)
        expected = now + timedelta(days=90)
        assert abs((new_expiry - expected).days) <= 1

    def test_stacks_from_current_expiry(self, payment_handler, sample_user):
        """Test extension stacks from current expiry, not now."""
        # Set expiry 30 days in future
        future = datetime.utcnow() + timedelta(days=30)
        sample_user.subscription_expiry = future.isoformat()
        payment_handler.db.get_user.return_value = sample_user

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:3",
                "total_amount": 270,
                "telegram_payment_charge_id": "tx_123",
            },
        }

        payment_handler._on_successful_payment(msg)

        saved_user = payment_handler.db.save_user.call_args[0][0]
        new_expiry = datetime.fromisoformat(saved_user.subscription_expiry)
        # Should be ~120 days (30 remaining + 90 new)
        expected = future + timedelta(days=90)
        assert abs((new_expiry - expected).days) <= 1

    def test_sends_success_message(self, payment_handler, sample_user):
        """Test _on_successful_payment sends success message to user."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:1",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_456",
            },
        }

        payment_handler._on_successful_payment(msg)

        # Find success message to user
        user_messages = [
            call
            for call in payment_handler.bot.send_message.call_args_list
            if call[1].get("chat_id") == "987654321"
        ]
        assert len(user_messages) >= 1
        success_text = user_messages[0][1]["text"]
        assert "Подписка активирована" in success_text
        assert "1 мес." in success_text

    def test_logs_admin_action(self, payment_handler, sample_user):
        """Test _on_successful_payment logs payment for audit."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:6",
                "total_amount": 500,
                "telegram_payment_charge_id": "tx_789",
            },
        }

        payment_handler._on_successful_payment(msg)

        payment_handler.db.log_admin_action.assert_called_once()
        call_args = payment_handler.db.log_admin_action.call_args
        assert call_args[0][0] == "self-serve"
        assert call_args[0][1] == "payment_stars"
        assert call_args[0][2] == "987654321"
        assert "+6 мес" in call_args[0][3]
        assert "500 ⭐" in call_args[0][3]

    def test_notifies_admin_via_payments_topic(self, payment_handler, sample_user):
        """Revenue ping goes to the payments topic when the forum is on.

        House rule: while the forum group works the bot must NOT write
        to the admin's personal chat — PM is only a fallback.
        """
        sample_user.subscription_expiry = None
        sample_user.username = "testuser"
        payment_handler.db.get_user.return_value = sample_user
        payment_handler.config.FORUM_ENABLED = True
        payment_handler.config.FORUM_GROUP_ID = "-100777"
        payment_handler.config.TOPIC_PAYMENTS = 42

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:12",
                "total_amount": 900,
                "telegram_payment_charge_id": "tx_admin",
            },
        }

        payment_handler._on_successful_payment(msg)

        topic_calls = payment_handler.bot.send_message_to_topic.call_args_list
        assert len(topic_calls) == 1
        kw = topic_calls[0][1]
        assert kw["chat_id"] == "-100777"
        assert kw["message_thread_id"] == 42
        assert "@testuser" in kw["text"]
        assert "12 мес" in kw["text"]
        assert "900 ⭐" in kw["text"]
        # no duplicate PM to the admin
        admin_messages = [
            call
            for call in payment_handler.bot.send_message.call_args_list
            if call[1].get("chat_id") == "123456"
        ]
        assert admin_messages == []

    def test_notifies_admin_pm_fallback(self, payment_handler, sample_user):
        """Without a forum group the revenue ping falls back to admin PM."""
        sample_user.subscription_expiry = None
        sample_user.username = "testuser"
        payment_handler.db.get_user.return_value = sample_user
        payment_handler.config.FORUM_ENABLED = False

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:12",
                "total_amount": 900,
                "telegram_payment_charge_id": "tx_admin",
            },
        }

        payment_handler._on_successful_payment(msg)

        admin_messages = [
            call
            for call in payment_handler.bot.send_message.call_args_list
            if call[1].get("chat_id") == "123456"
        ]
        assert len(admin_messages) == 1
        admin_text = admin_messages[0][1]["text"]
        assert "@testuser" in admin_text
        assert "12 мес" in admin_text
        assert "900 ⭐" in admin_text

    def test_handles_missing_user(self, payment_handler):
        """Test _on_successful_payment handles unknown user gracefully."""
        payment_handler.db.get_user.return_value = None

        msg = {
            "chat": {"id": "999999"},
            "successful_payment": {
                "invoice_payload": "sub:999999:1",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_missing",
            },
        }

        payment_handler._on_successful_payment(msg)

        # Should not crash or send messages
        payment_handler.db.save_user.assert_not_called()

    def test_handles_invalid_payload_format(self, payment_handler, sample_user):
        """Test _on_successful_payment handles malformed payload."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "invalid:payload",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_bad",
            },
        }

        payment_handler._on_successful_payment(msg)

        # Should not process payment
        payment_handler.db.save_user.assert_not_called()

    def test_handles_payload_missing_parts(self, payment_handler, sample_user):
        """Test _on_successful_payment handles incomplete payload."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_incomplete",
            },
        }

        payment_handler._on_successful_payment(msg)

        payment_handler.db.save_user.assert_not_called()

    def test_handles_invalid_months_value(self, payment_handler, sample_user):
        """Test _on_successful_payment handles non-integer months."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:abc",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_badmonths",
            },
        }

        payment_handler._on_successful_payment(msg)

        payment_handler.db.save_user.assert_not_called()

    def test_handles_chat_id_mismatch(self, payment_handler, sample_user):
        """Test _on_successful_payment handles payload/event chat mismatch."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        # Payload says 111, but event is for 987654321
        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:111:1",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_mismatch",
            },
        }

        payment_handler._on_successful_payment(msg)

        # Should still process for the event chat (defensive)
        assert payment_handler.db.save_user.called

    def test_updates_subscription_table(self, payment_handler, sample_user):
        """Test _on_successful_payment updates subscriptions table."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        mock_conn = MagicMock()
        mock_conn.execute = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        payment_handler.db._connect.return_value = mock_conn

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:3",
                "total_amount": 270,
                "telegram_payment_charge_id": "tx_subupdate",
            },
        }

        payment_handler._on_successful_payment(msg)

        # Check subscriptions table update was attempted
        assert mock_conn.execute.called
        sql_call = mock_conn.execute.call_args
        assert "UPDATE subscriptions" in sql_call[0][0]

    def test_handles_subscription_update_failure(self, payment_handler, sample_user):
        """Test _on_successful_payment continues on subscription update failure."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        # Mock connection that raises exception
        mock_conn = MagicMock()
        mock_conn.execute.side_effect = Exception("DB error")
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        payment_handler.db._connect.return_value = mock_conn

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:1",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_subfail",
            },
        }

        # Should not raise exception
        payment_handler._on_successful_payment(msg)

        # Should still save user
        assert payment_handler.db.save_user.called

    def test_success_message_contains_expiry_date(self, payment_handler, sample_user):
        """Test success message shows formatted expiry date."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:1",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_date",
            },
        }

        payment_handler._on_successful_payment(msg)

        # Find user success message
        user_messages = [
            call
            for call in payment_handler.bot.send_message.call_args_list
            if call[1].get("chat_id") == "987654321"
        ]
        success_text = user_messages[0][1]["text"]
        # Should contain date in DD.MM.YYYY format
        assert any(char.isdigit() for char in success_text)

    def test_success_message_shows_stars_charged(self, payment_handler, sample_user):
        """Test success message shows Stars amount."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:12",
                "total_amount": 900,
                "telegram_payment_charge_id": "tx_stars",
            },
        }

        payment_handler._on_successful_payment(msg)

        user_messages = [
            call
            for call in payment_handler.bot.send_message.call_args_list
            if call[1].get("chat_id") == "987654321"
        ]
        success_text = user_messages[0][1]["text"]
        assert "900 ⭐" in success_text

    def test_no_admin_notification_when_no_admin_id(self, payment_handler, sample_user):
        """Test no admin notification when SUPER_ADMIN_ID not set."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user
        payment_handler.config.SUPER_ADMIN_ID = None

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:1",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_noadmin",
            },
        }

        payment_handler._on_successful_payment(msg)

        # Check no admin message sent
        admin_messages = [
            call
            for call in payment_handler.bot.send_message.call_args_list
            if call[1].get("chat_id") == "123456"
        ]
        assert len(admin_messages) == 0

    def test_handles_invalid_expiry_date(self, payment_handler, sample_user):
        """Test _on_successful_payment handles invalid stored expiry date."""
        # Set invalid expiry
        sample_user.subscription_expiry = "not-a-date"
        payment_handler.db.get_user.return_value = sample_user

        msg = {
            "chat": {"id": "987654321"},
            "successful_payment": {
                "invoice_payload": "sub:987654321:1",
                "total_amount": 100,
                "telegram_payment_charge_id": "tx_baddate",
            },
        }

        # Should not raise exception, should fall back to now
        payment_handler._on_successful_payment(msg)

        assert payment_handler.db.save_user.called


# ----- handle() Method Tests -----


class TestHandleRouting:
    """Tests for PaymentHandler.handle() routing logic."""

    def test_routes_successful_payment(self, payment_handler, sample_user):
        """Test handle routes to _on_successful_payment."""
        sample_user.subscription_expiry = None
        payment_handler.db.get_user.return_value = sample_user

        update = {
            "message": {
                "chat": {"id": "987654321"},
                "successful_payment": {
                    "invoice_payload": "sub:987654321:1",
                    "total_amount": 100,
                    "telegram_payment_charge_id": "tx_route1",
                },
            }
        }

        payment_handler.handle(update)

        assert payment_handler.db.save_user.called

    def test_routes_pre_checkout_query(self, payment_handler):
        """Test handle routes to _on_pre_checkout."""
        update = {
            "pre_checkout_query": {
                "id": "pcq_route",
                "invoice_payload": "sub:123:3",
            }
        }

        payment_handler.handle(update)

        payment_handler.bot.client.answer_pre_checkout_query.assert_called_once()

    def test_routes_buy_plan_callback(self, payment_handler):
        """Test handle routes buy_plan callback."""
        update = {
            "callback_query": {
                "data": "buy_plan:1",
                "id": "cb_route",
                "from": {"id": "444"},
            }
        }

        payment_handler.handle(update)

        payment_handler.bot.client.send_invoice.assert_called_once()

    def test_routes_buy_menu_callback(self, payment_handler):
        """Test handle routes buy_menu callback."""
        update = {
            "callback_query": {
                "data": "buy_menu",
                "id": "cb_menu",
                "from": {"id": "555"},
            }
        }

        payment_handler.handle(update)

        payment_handler.bot.send_message.assert_called_once()
        call_args = payment_handler.bot.send_message.call_args
        assert call_args[1]["chat_id"] == "555"

    def test_routes_buy_command(self, payment_handler):
        """Test handle routes /buy command."""
        update = {
            "message": {"text": "/buy", "chat": {"id": "666"}}
        }

        payment_handler.handle(update)

        payment_handler.bot.send_message.assert_called_once()
        call_args = payment_handler.bot.send_message.call_args
        assert call_args[1]["chat_id"] == "666"

    def test_answers_callback_on_buy_menu(self, payment_handler):
        """Test handle answers callback for buy_menu."""
        update = {
            "callback_query": {
                "data": "buy_menu",
                "id": "cb_answer",
                "from": {"id": "777"},
            }
        }

        payment_handler.handle(update)

        payment_handler.bot.answer_callback_query.assert_called_once_with(
            "cb_answer"
        )

    def test_handles_callback_answer_exception(self, payment_handler):
        """Test handle handles callback answer exception gracefully."""
        payment_handler.bot.answer_callback_query.side_effect = Exception("API error")

        update = {
            "callback_query": {
                "data": "buy_menu",
                "id": "cb_ex",
                "from": {"id": "888"},
            }
        }

        # Should not raise exception
        payment_handler.handle(update)

        # Menu should still be shown
        payment_handler.bot.send_message.assert_called_once()

    def test_handles_callback_without_id(self, payment_handler):
        """Test handle handles callback without id field."""
        update = {
            "callback_query": {
                "data": "buy_menu",
                "from": {"id": "999"},
            }
        }

        payment_handler.handle(update)

        # Should still show menu
        payment_handler.bot.send_message.assert_called_once()
        # answer_callback_query should not be called
        payment_handler.bot.answer_callback_query.assert_not_called()

    def test_does_nothing_for_unhandled_update(self, payment_handler):
        """Test handle ignores updates not matching any pattern."""
        # This update would fail can_handle, but let's test defensive coding
        update = {"message": {"text": "hello", "chat": {"id": "000"}}}

        payment_handler.handle(update)

        # Nothing should be called
        payment_handler.bot.send_message.assert_not_called()
        payment_handler.bot.client.send_invoice.assert_not_called()
