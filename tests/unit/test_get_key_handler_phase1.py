"""Tests for Phase 1 hardening of GetKeyHandler.

Covers:
- Double-click protection via _inflight_chat_ids
- Bounded retry with exponential backoff in _sync_to_xui
- UUID + VLESS validation in _send_key_to_user
- /mykey command rejecting invalid generated links
"""

import asyncio
import pytest
from unittest.mock import Mock, patch, AsyncMock

from bot.handlers.callbacks.user import GetKeyHandler
from bot.handlers.commands import CommandHandler
from bot.utils.exceptions import VPNBotError


@pytest.fixture
def handler():
    """GetKeyHandler with mocked bot/db/config."""
    GetKeyHandler._inflight_chat_ids.clear()

    mock_bot = Mock()
    mock_bot.services = {'xui': Mock()}
    mock_db = Mock()
    mock_config = Mock()
    mock_config.DEMO_TRAFFIC_GB = 5
    mock_config.DEMO_DAYS = 7
    mock_config.WEBAPP_URL = 'https://example.com'
    mock_config.BOT_TOKEN = 'test-token'

    h = GetKeyHandler(mock_bot, mock_db, mock_config)
    # Speed up retries during tests
    h.SYNC_TIMEOUT_SEC = 0.5
    h.SYNC_BASE_BACKOFF_SEC = 0.01
    return h


@pytest.fixture
def command_handler():
    """CommandHandler with mocked bot/db/config."""
    mock_bot = Mock()
    mock_db = Mock()
    mock_config = Mock()
    mock_config.ENTRY_NODE_IP = '203.0.113.10'
    mock_config.REALITY_PUBLIC_KEY = 'pubkey'
    mock_config.SNI_VALUE = 'www.microsoft.com'
    mock_config.SID_VALUE = ''

    return CommandHandler(mock_bot, mock_db, mock_config)


class TestDoubleClickProtection:
    """A second request for the same chat_id while first is in flight must drop."""

    @pytest.mark.asyncio
    async def test_second_call_dropped_while_first_in_flight(self, handler):
        chat_id = '99999'
        # Claim manually to simulate "already in flight"
        assert handler._claim_inflight(chat_id) is True

        await handler._async_handle_get_key(chat_id)

        # _process_key_request should never have been entered: the user got
        # the "уже обрабатывается" message and that's it.
        handler.bot.send_message.assert_called_once()
        text = handler.bot.send_message.call_args.kwargs.get('text', '')
        assert 'обрабатывается' in text

        # Cleanup
        handler._release_inflight(chat_id)

    @pytest.mark.asyncio
    async def test_inflight_released_after_success(self, handler):
        chat_id = '11111'
        mock_user = Mock(uuid=None, email=None, username='u', lang='ru', status='demo')
        handler.validator.validate_user_exists = Mock(return_value=mock_user)
        handler.bot.services['xui'].sync_user = AsyncMock(return_value=True)

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN:
            MockVPN.return_value.create_client_config.return_value = {
                'id': 'aa11bb22-cc33-dd44-ee55-ff66aa77bb88',
                'email': 'u@nekovo.ru',
            }
            MockVPN.return_value.generate_vless_link.return_value = (
                'vless://aa11bb22-cc33-dd44-ee55-ff66aa77bb88@1.2.3.4:443?security=reality'
            )
            with patch('bot.handlers.callbacks.user.NotificationService'):
                await handler._async_handle_get_key(chat_id)

        assert chat_id not in handler._inflight_chat_ids

    @pytest.mark.asyncio
    async def test_inflight_released_after_exception(self, handler):
        chat_id = '22222'
        mock_user = Mock(uuid=None, email=None, username='u', lang='ru', status='demo')
        handler.validator.validate_user_exists = Mock(return_value=mock_user)
        handler.bot.services['xui'].sync_user = AsyncMock(return_value=False)

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN:
            MockVPN.return_value.create_client_config.return_value = {
                'id': 'aa11bb22-cc33-dd44-ee55-ff66aa77bb88',
                'email': 'u@nekovo.ru',
            }
            await handler._async_handle_get_key(chat_id)

        # In-flight set must be empty even when the inner flow raised
        assert chat_id not in handler._inflight_chat_ids


class TestSyncRetry:
    """_sync_to_xui retries transient X-UI failures before giving up."""

    @pytest.mark.asyncio
    async def test_succeeds_on_third_attempt(self, handler):
        handler.bot.services['xui'].sync_user = AsyncMock(side_effect=[False, False, True])
        await handler._sync_to_xui('12345', {'id': 'abc'})
        assert handler.bot.services['xui'].sync_user.call_count == 3

    @pytest.mark.asyncio
    async def test_raises_after_exhausting_attempts(self, handler):
        handler.bot.services['xui'].sync_user = AsyncMock(return_value=False)
        with pytest.raises(VPNBotError):
            await handler._sync_to_xui('12345', {'id': 'abc'})
        assert handler.bot.services['xui'].sync_user.call_count == handler.SYNC_MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_retries_on_timeout(self, handler):
        async def slow_then_fast(*_a, **_kw):
            if handler.bot.services['xui'].sync_user.call_count == 1:
                await asyncio.sleep(handler.SYNC_TIMEOUT_SEC + 1.0)
                return True
            return True

        handler.bot.services['xui'].sync_user = AsyncMock(side_effect=slow_then_fast)
        await handler._sync_to_xui('12345', {'id': 'abc'})
        assert handler.bot.services['xui'].sync_user.call_count >= 2

    @pytest.mark.asyncio
    async def test_retries_on_exception(self, handler):
        handler.bot.services['xui'].sync_user = AsyncMock(
            side_effect=[ConnectionError("boom"), True]
        )
        await handler._sync_to_xui('12345', {'id': 'abc'})
        assert handler.bot.services['xui'].sync_user.call_count == 2

    @pytest.mark.asyncio
    async def test_no_xui_service_raises_immediately(self, handler):
        handler.bot.services = {}
        with pytest.raises(VPNBotError):
            await handler._sync_to_xui('12345', {'id': 'abc'})


class TestSendKeyValidation:
    """_send_key_to_user must refuse to ship a broken VLESS link."""

    @pytest.mark.asyncio
    async def test_refuses_when_uuid_missing(self, handler):
        user = Mock(uuid=None, email='x@y.z', lang='ru')
        await handler._send_key_to_user('123', user)
        # Sent the "профиль повреждён" message, not the key
        handler.bot.send_message.assert_called_once()
        assert 'Профиль' in handler.bot.send_message.call_args.kwargs.get('text', '')

    @pytest.mark.asyncio
    async def test_refuses_when_email_missing(self, handler):
        user = Mock(uuid='aa11bb22-cc33-dd44-ee55-ff66aa77bb88', email=None, lang='ru')
        await handler._send_key_to_user('123', user)
        handler.bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_refuses_when_subscription_url_missing(self, handler):
        user = Mock(uuid='aa11bb22-cc33-dd44-ee55-ff66aa77bb88', email='x@y.z', lang='ru')
        handler.config.WEBAPP_URL = ''
        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN:
            MockVPN.return_value.generate_vless_ws_link.return_value = ''
            await handler._send_key_to_user('123', user)
        handler.bot.send_message.assert_called_once()
        text = handler.bot.send_message.call_args.kwargs.get('text', '')
        assert 'Сервис' in text or 'Service' in text

    @pytest.mark.asyncio
    async def test_sends_subscription_url(self, handler):
        user = Mock(uuid='aa11bb22-cc33-dd44-ee55-ff66aa77bb88', email='x@y.z', lang='ru')
        with patch('bot.services.subscription.SubscriptionService') as MockSub:
            MockSub.return_value.build_subscription_url.return_value = (
                'https://example.com/sub/abc123'
            )
            await handler._send_key_to_user('123', user)
            handler.bot.send_message.assert_called_once()
            text = handler.bot.send_message.call_args.kwargs.get('text', '')
            assert 'https://example.com/sub/abc123' in text


class TestStatusGuard:
    """Users in disallowed states must not receive a key."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_status", ["rejected", "new", "pending_demo", "platform_select", "banned"])
    async def test_blocked_statuses_do_not_receive_key(self, handler, bad_status):
        user = Mock(uuid="aa11bb22-cc33-dd44-ee55-ff66aa77bb88",
                    email="x@y.z", username='u', lang='ru', status=bad_status)
        handler.validator.validate_user_exists = Mock(return_value=user)

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN, \
             patch('bot.handlers.callbacks.user.NotificationService') as MockNotif:
            await handler._process_key_request("12345")
            MockNotif.return_value.notify_key_generated.assert_not_called()
            MockVPN.return_value.generate_vless_link.assert_not_called()

        # User got a "go through /start first" message, not the key.
        handler.bot.send_message.assert_called_once()
        text = handler.bot.send_message.call_args.kwargs.get('text', '')
        assert 'одобрение' in text or 'start' in text.lower()


class TestMykeyCommandValidation:
    """The /mykey command must apply the same validation gate."""

    def test_mykey_refuses_when_subscription_url_missing(self, command_handler):
        user = Mock(uuid='aa11bb22-cc33-dd44-ee55-ff66aa77bb88', email='x@y.z', lang='ru', status='demo')
        command_handler.db.get_user = Mock(return_value=user)
        command_handler.config.WEBAPP_URL = ''

        command_handler.handle_mykey({}, '12345')

        command_handler.bot.send_message.assert_called_once()
        text = command_handler.bot.send_message.call_args.kwargs.get('text', '')
        assert 'Subscription URL' in text or 'недоступен' in text

    def test_mykey_sends_subscription_url(self, command_handler):
        user = Mock(uuid='aa11bb22-cc33-dd44-ee55-ff66aa77bb88', email='x@y.z', lang='ru', status='demo')
        command_handler.db.get_user = Mock(return_value=user)
        command_handler.config.WEBAPP_URL = 'https://example.com'

        with patch('bot.services.subscription.SubscriptionService') as MockSub:
            MockSub.return_value.build_subscription_url.return_value = (
                'https://example.com/sub/abc123'
            )
            command_handler.handle_mykey({}, '12345')
            command_handler.bot.send_message.assert_called_once()
            text = command_handler.bot.send_message.call_args.kwargs.get('text', '')
            assert 'https://example.com/sub/abc123' in text
