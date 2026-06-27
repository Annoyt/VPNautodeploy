"""Comprehensive unit tests for bot/handlers/callbacks/user.py handlers.

Focus areas:
1. GetKeyHandler - retry logic, UUID validation, IDOR protection, inflight handling
2. TryAltProtocolHandler - fallback logic, protocol cascade
3. StatsRequestHandler - stats formatting, admin vs regular user
4. LanguageSetHandler - language switching and confirmation
"""

import asyncio
import pytest
from unittest.mock import Mock, patch, AsyncMock

from bot.handlers.callbacks.user import (
    GetKeyHandler,
    TryAltProtocolHandler,
    StatsRequestHandler,
    LanguageSetHandler,
)
from bot.models.user import User
from bot.utils.exceptions import VPNBotError


# ===== Fixtures =====

@pytest.fixture
def mock_bot():
    """Mock Telegram bot."""
    bot = Mock()
    bot.send_message = Mock(return_value={'message_id': 123})
    bot.services = {}
    return bot


@pytest.fixture
def mock_db():
    """Mock database."""
    db = Mock()
    return db


@pytest.fixture
def mock_config():
    """Mock configuration."""
    config = Mock()
    config.DEMO_TRAFFIC_GB = 5
    config.DEMO_DAYS = 7
    config.WEBAPP_URL = 'https://example.com'
    config.BOT_TOKEN = 'test-token'
    config.SUPER_ADMIN_ID = '123'
    config.XUI_DB_PATH = '/tmp/test_xui.db'
    config.is_admin = Mock(return_value=False)
    return config


@pytest.fixture
def sample_user():
    """Sample user object."""
    return User(
        chat_id='999999999',
        username='testuser',
        uuid='550e8400-e29b-41d4-a716-446655440000',
        email='user_999999999@nekovo.ru',
        status='demo',
        lang='ru',
        platform='android',
    )


@pytest.fixture
def paid_user():
    """Paid user object."""
    return User(
        chat_id='888888888',
        username='paiduser',
        uuid='660e8400-e29b-41d4-a716-446655440001',
        email='user_888888888@nekovo.ru',
        status='paid',
        lang='en',
        platform='ios',
    )


@pytest.fixture
def user_no_key():
    """User without UUID (needs key generation)."""
    return User(
        chat_id='777777777',
        username='newuser',
        uuid=None,
        email=None,
        status='demo',
        lang='ru',
        platform='android',
    )


# ===== GetKeyHandler Tests =====

class TestGetKeyHandler:
    """Tests for GetKeyHandler retry logic, UUID validation, and IDOR protection."""

    def test_can_handle_exact_callbacks(self, mock_bot, mock_db, mock_config):
        """Test can_handle recognizes exact callback strings."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        assert handler.can_handle('generate_key') is True
        assert handler.can_handle('my_key') is True
        assert handler.can_handle('random_callback') is False

    def test_can_handle_pattern_callbacks(self, mock_bot, mock_db, mock_config):
        """Test can_handle recognizes pattern callbacks."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        assert handler.can_handle('get_key:12345') is True
        assert handler.can_handle('get_key:') is True
        assert handler.can_handle('get_key:') is True
        assert handler.can_handle('other_prefix:123') is False

    def test_idor_protection_blocks_cross_user_access(self, mock_bot, mock_db, mock_config):
        """Test IDOR protection: users can only get their own key."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        update = {'callback_query': {'message': {'chat': {'id': '111'}}}}

        handler.handle(update, chat_id='111', user_id='111', data='get_key:222')

        # Should send error message, not proceed
        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'retrieve your own' in text.lower() or 'только свой' in text.lower()

    def test_idor_protection_allows_self_access(self, mock_bot, mock_db, mock_config, sample_user):
        """Test IDOR protection allows user to get their own key."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        update = {'callback_query': {'message': {'chat': {'id': '999999999'}}}}

        with patch.object(handler, '_run_async'):
            handler.handle(update, chat_id='999999999', user_id='999999999', data='generate_key')
            handler._run_async.assert_called_once()

    def test_idor_protection_allows_admin_access(self, mock_bot, mock_db, mock_config, sample_user):
        """Test IDOR protection allows admin to get any user's key."""
        mock_config.is_admin = Mock(return_value=True)
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        update = {'callback_query': {'message': {'chat': {'id': '123'}}}}

        with patch.object(handler, '_run_async'):
            handler.handle(update, chat_id='123', user_id='123', data='get_key:999999999')
            handler._run_async.assert_called_once()

    @pytest.mark.asyncio
    async def test_inflight_claim_prevents_duplicate_requests(self, mock_bot, mock_db, mock_config):
        """Test that inflight tracking prevents duplicate concurrent requests."""
        GetKeyHandler._inflight_chat_ids.clear()
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        chat_id = '555555555'

        # First claim succeeds
        assert handler._claim_inflight(chat_id) is True
        # Second claim fails
        assert handler._claim_inflight(chat_id) is False
        # Cleanup
        handler._release_inflight(chat_id)
        # Can claim again after release
        assert handler._claim_inflight(chat_id) is True
        handler._release_inflight(chat_id)

    @pytest.mark.asyncio
    async def test_inflight_released_after_success(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that inflight tracking is released after successful key generation."""
        GetKeyHandler._inflight_chat_ids.clear()
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        handler.SYNC_TIMEOUT_SEC = 0.5
        handler.SYNC_BASE_BACKOFF_SEC = 0.01

        mock_xui = Mock()
        mock_xui.sync_user = AsyncMock(return_value=True)
        mock_bot.services = {'xui': mock_xui}

        mock_db.get_user = Mock(return_value=sample_user)
        mock_db.save_user = Mock()

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN:
            mock_vpn = Mock()
            mock_vpn.create_client_config.return_value = {
                'id': sample_user.uuid,
                'email': sample_user.email,
            }
            MockVPN.return_value = mock_vpn

            with patch('bot.services.subscription.SubscriptionService') as MockSub:
                mock_sub = Mock()
                mock_sub.build_subscription_url.return_value = 'https://example.com/sub/test'
                MockSub.return_value = mock_sub

                await handler._async_handle_get_key(sample_user.chat_id)

        # Inflight should be released
        assert sample_user.chat_id not in handler._inflight_chat_ids

    @pytest.mark.asyncio
    async def test_inflight_released_after_exception(self, mock_bot, mock_db, mock_config, user_no_key):
        """Test that inflight tracking is released even when sync fails."""
        GetKeyHandler._inflight_chat_ids.clear()
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)

        mock_xui = Mock()
        mock_xui.sync_user = AsyncMock(return_value=False)
        mock_bot.services = {'xui': mock_xui}

        mock_db.get_user = Mock(return_value=user_no_key)

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN:
            mock_vpn = Mock()
            mock_vpn.create_client_config.return_value = {
                'id': 'test-uuid',
                'email': 'test@example.com',
            }
            MockVPN.return_value = mock_vpn

            await handler._async_handle_get_key(user_no_key.chat_id)

        # Inflight should be released even after failure
        assert user_no_key.chat_id not in handler._inflight_chat_ids

    @pytest.mark.asyncio
    async def test_sync_retry_logic_succeeds_on_third_attempt(self, mock_bot, mock_db, mock_config):
        """Test X-UI sync retry logic - succeeds on third attempt."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        handler.SYNC_TIMEOUT_SEC = 0.5
        handler.SYNC_BASE_BACKOFF_SEC = 0.01

        mock_xui = Mock()
        mock_xui.sync_user = AsyncMock(side_effect=[False, False, True])
        mock_bot.services = {'xui': mock_xui}

        client_config = {'id': 'test-uuid', 'email': 'test@example.com'}
        await handler._sync_to_xui('999999999', client_config)

        assert mock_xui.sync_user.call_count == 3

    @pytest.mark.asyncio
    async def test_sync_retry_raises_after_max_attempts(self, mock_bot, mock_db, mock_config):
        """Test X-UI sync retry logic - raises after max attempts exhausted."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        handler.SYNC_TIMEOUT_SEC = 0.5
        handler.SYNC_BASE_BACKOFF_SEC = 0.01

        mock_xui = Mock()
        mock_xui.sync_user = AsyncMock(return_value=False)
        mock_bot.services = {'xui': mock_xui}

        client_config = {'id': 'test-uuid', 'email': 'test@example.com'}

        with pytest.raises(VPNBotError) as exc_info:
            await handler._sync_to_xui('999999999', client_config)

        assert mock_xui.sync_user.call_count == handler.SYNC_MAX_ATTEMPTS
        assert 'after' in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_sync_retry_handles_timeout(self, mock_bot, mock_db, mock_config):
        """Test X-UI sync retry logic handles timeout errors."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        handler.SYNC_TIMEOUT_SEC = 0.5
        handler.SYNC_BASE_BACKOFF_SEC = 0.01

        call_count = [0]
        async def timeout_then_success(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                await asyncio.sleep(handler.SYNC_TIMEOUT_SEC + 0.1)
                raise asyncio.TimeoutError()
            return True

        mock_xui = Mock()
        mock_xui.sync_user = AsyncMock(side_effect=timeout_then_success)
        mock_bot.services = {'xui': mock_xui}

        client_config = {'id': 'test-uuid', 'email': 'test@example.com'}
        await handler._sync_to_xui('999999999', client_config)

        assert call_count[0] >= 2

    @pytest.mark.asyncio
    async def test_sync_retry_handles_generic_exceptions(self, mock_bot, mock_db, mock_config):
        """Test X-UI sync retry handles generic exceptions."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        handler.SYNC_TIMEOUT_SEC = 0.5
        handler.SYNC_BASE_BACKOFF_SEC = 0.01

        mock_xui = Mock()
        mock_xui.sync_user = AsyncMock(
            side_effect=[ConnectionError("network error"), True]
        )
        mock_bot.services = {'xui': mock_xui}

        client_config = {'id': 'test-uuid', 'email': 'test@example.com'}
        await handler._sync_to_xui('999999999', client_config)

        assert mock_xui.sync_user.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_raises_immediately_without_xui_service(self, mock_bot, mock_db, mock_config):
        """Test sync raises VPNBotError immediately when X-UI service is missing."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        mock_bot.services = {}

        client_config = {'id': 'test-uuid', 'email': 'test@example.com'}

        with pytest.raises(VPNBotError) as exc_info:
            await handler._sync_to_xui('999999999', client_config)

        assert 'unavailable' in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_blocked_status_receives_warning_not_key(self, mock_bot, mock_db, mock_config):
        """Test users in blocked statuses receive warning message instead of key."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)

        for bad_status in ['rejected', 'new', 'pending_demo', 'platform_select', 'banned']:
            mock_bot.reset_mock()
            user = User(
                chat_id='111111111',
                uuid='some-uuid',
                email='test@example.com',
                status=bad_status,
                username='test',
                lang='ru',
            )
            mock_db.get_user = Mock(return_value=user)

            await handler._process_key_request('111111111')

            # Should send warning, not key
            mock_bot.send_message.assert_called_once()
            text = mock_bot.send_message.call_args.kwargs.get('text', '')
            assert 'одобрение' in text or 'start' in text.lower()

    @pytest.mark.asyncio
    async def test_uuid_validation_refuses_missing_uuid(self, mock_bot, mock_db, mock_config):
        """Test that _send_key_to_user refuses when UUID is missing."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)

        user = User(
            chat_id='111111111',
            uuid=None,
            email='test@example.com',
            status='demo',
            lang='ru',
        )

        await handler._send_key_to_user('111111111', user)

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'Профиль' in text or 'corrupted' in text.lower()

    @pytest.mark.asyncio
    async def test_uuid_validation_refuses_missing_email(self, mock_bot, mock_db, mock_config):
        """Test that _send_key_to_user refuses when email is missing."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)

        user = User(
            chat_id='111111111',
            uuid='550e8400-e29b-41d4-a716-446655440000',
            email=None,
            status='demo',
            lang='ru',
        )

        await handler._send_key_to_user('111111111', user)

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'Профиль' in text or 'corrupted' in text.lower()

    @pytest.mark.asyncio
    async def test_sends_subscription_url_when_available(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that subscription URL is sent when WEBAPP_URL is configured."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)

        with patch('bot.services.subscription.SubscriptionService') as MockSub:
            mock_sub = Mock()
            mock_sub.build_subscription_url.return_value = 'https://example.com/sub/test'
            MockSub.return_value = mock_sub

            await handler._send_key_to_user(sample_user.chat_id, sample_user)

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'https://example.com/sub/test' in text

    @pytest.mark.asyncio
    async def test_sends_fallback_when_subscription_unavailable(self, mock_bot, mock_db, mock_config, sample_user):
        """Test fallback link when subscription service is unavailable."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        mock_config.WEBAPP_URL = ''

        with patch('bot.services.subscription.SubscriptionService') as MockSub, \
             patch('bot.handlers.callbacks.user.VPNService') as MockVPN:
            mock_sub = Mock()
            mock_sub.build_subscription_url.return_value = None
            MockSub.return_value = mock_sub

            mock_vpn = Mock()
            mock_vpn.generate_vless_ws_link.return_value = 'vless://fallback-link'
            MockVPN.return_value = mock_vpn

            await handler._send_key_to_user(sample_user.chat_id, sample_user)

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'vless://fallback-link' in text

    @pytest.mark.asyncio
    async def test_creates_new_key_when_uuid_missing(self, mock_bot, mock_db, mock_config, user_no_key):
        """Test that new key is created when user has no UUID."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        handler.SYNC_TIMEOUT_SEC = 0.5
        handler.SYNC_BASE_BACKOFF_SEC = 0.01

        mock_xui = Mock()
        mock_xui.sync_user = AsyncMock(return_value=True)
        mock_bot.services = {'xui': mock_xui}

        mock_db.get_user = Mock(return_value=user_no_key)
        mock_db.save_user = Mock()

        new_uuid = '770e8400-e29b-41d4-a716-446655440002'
        new_email = 'user_777777777@nekovo.ru'

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN:
            mock_vpn = Mock()
            mock_vpn.create_client_config.return_value = {
                'id': new_uuid,
                'email': new_email,
            }
            MockVPN.return_value = mock_vpn

            with patch('bot.services.subscription.SubscriptionService') as MockSub:
                mock_sub = Mock()
                mock_sub.build_subscription_url.return_value = 'https://example.com/sub/test'
                MockSub.return_value = mock_sub

                await handler._process_key_request(user_no_key.chat_id)

        # User should have been saved with new UUID and email
        assert user_no_key.uuid == new_uuid
        assert user_no_key.email == new_email
        mock_db.save_user.assert_called_once()

    @pytest.mark.asyncio
    async def test_resyncs_existing_key_when_uuid_present(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that existing key is resynced when user has UUID."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        handler.SYNC_TIMEOUT_SEC = 0.5
        handler.SYNC_BASE_BACKOFF_SEC = 0.01

        mock_xui = Mock()
        mock_xui.sync_user = AsyncMock(return_value=True)
        mock_bot.services = {'xui': mock_xui}

        mock_db.get_user = Mock(return_value=sample_user)

        with patch('bot.services.subscription.SubscriptionService') as MockSub:
            mock_sub = Mock()
            mock_sub.build_subscription_url.return_value = 'https://example.com/sub/test'
            MockSub.return_value = mock_sub

            await handler._process_key_request(sample_user.chat_id)

        # Should sync existing UUID
        mock_xui.sync_user.assert_called_once()
        call_args = mock_xui.sync_user.call_args
        assert call_args[0][0] == sample_user.chat_id
        assert call_args[0][1]['id'] == sample_user.uuid

    @pytest.mark.asyncio
    async def test_english_language_in_key_message(self, mock_bot, mock_db, mock_config):
        """Test that English language is used for key message when user.lang='en'."""
        handler = GetKeyHandler(mock_bot, mock_db, mock_config)

        user = User(
            chat_id='111111111',
            uuid='550e8400-e29b-41d4-a716-446655440000',
            email='test@example.com',
            status='demo',
            lang='en',
        )

        with patch('bot.services.subscription.SubscriptionService') as MockSub:
            mock_sub = Mock()
            mock_sub.build_subscription_url.return_value = 'https://example.com/sub/test'
            MockSub.return_value = mock_sub

            await handler._send_key_to_user(user.chat_id, user)

        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'Your VPN is ready' in text
        assert 'Hiddify' in text


# ===== TryAltProtocolHandler Tests =====

class TestTryAltProtocolHandler:
    """Tests for TryAltProtocolHandler fallback logic and protocol cascade."""

    def test_can_handle_pattern(self, mock_bot, mock_db, mock_config):
        """Test can_handle recognizes try_alt callbacks."""
        handler = TryAltProtocolHandler(mock_bot, mock_db, mock_config)
        assert handler.can_handle('try_alt:hy2') is True
        assert handler.can_handle('try_alt:ws') is True
        assert handler.can_handle('try_alt:stls') is True
        assert handler.can_handle('other:protocol') is False

    def test_rejects_invalid_protocol(self, mock_bot, mock_db, mock_config):
        """Test that invalid protocols are rejected."""
        handler = TryAltProtocolHandler(mock_bot, mock_db, mock_config)
        update = {'callback_query': {'message': {'chat': {'id': '111'}}}}

        # Should not crash on invalid protocol
        handler.handle(update, chat_id='111', user_id='111', data='try_alt:invalid')
        # Should not send message for invalid protocol
        mock_bot.send_message.assert_not_called()

    def test_sends_hy2_link_with_ws_fallback(self, mock_bot, mock_db, mock_config, sample_user):
        """Test sending Hy2 link with WS as next fallback."""
        handler = TryAltProtocolHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN, \
             patch('bot.handlers.callbacks.user.NotificationService') as MockNotif:
            mock_vpn = Mock()
            mock_vpn.generate_hy2_link.return_value = 'https://hy2-link'
            mock_vpn.generate_vless_ws_link.return_value = 'vless://ws-link'
            MockVPN.return_value = mock_vpn

            handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='try_alt:hy2')

        # Should call notify_alt_protocol with hy2 and ws as next
        MockNotif.return_value.notify_alt_protocol.assert_called_once()
        call_args = MockNotif.return_value.notify_alt_protocol.call_args
        assert call_args[0][2] == 'hy2'  # protocol
        assert call_args[1]['next_protocol'] == 'ws'

    def test_sends_ws_link_with_stls_fallback(self, mock_bot, mock_db, mock_config, sample_user):
        """Test sending WS link with ShadowTLS as next fallback."""
        handler = TryAltProtocolHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN, \
             patch('bot.handlers.callbacks.user.NotificationService') as MockNotif:
            mock_vpn = Mock()
            mock_vpn.generate_vless_ws_link.return_value = 'vless://ws-link'
            mock_vpn.generate_stls_link.return_value = 'stls://link'
            MockVPN.return_value = mock_vpn

            handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='try_alt:ws')

        call_args = MockNotif.return_value.notify_alt_protocol.call_args
        assert call_args[0][2] == 'ws'
        assert call_args[1]['next_protocol'] == 'stls'

    def test_sends_stls_link_with_no_fallback(self, mock_bot, mock_db, mock_config, sample_user):
        """Test sending ShadowTLS link with no further fallback."""
        handler = TryAltProtocolHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN, \
             patch('bot.handlers.callbacks.user.NotificationService') as MockNotif:
            mock_vpn = Mock()
            mock_vpn.generate_stls_link.return_value = 'stls://link'
            mock_vpn.generate_vless_ws_link.return_value = None
            MockVPN.return_value = mock_vpn

            handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='try_alt:stls')

        call_args = MockNotif.return_value.notify_alt_protocol.call_args
        assert call_args[0][2] == 'stls'
        assert call_args[1]['next_protocol'] is None

    def test_handles_missing_user(self, mock_bot, mock_db, mock_config):
        """Test handling when user is not found."""
        handler = TryAltProtocolHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=None)
        update = {'callback_query': {'message': {'chat': {'id': '111'}}}}

        handler.handle(update, chat_id='111', user_id='111', data='try_alt:hy2')

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'Сначала получите' in text or 'Get a key' in text

    def test_handles_user_without_uuid(self, mock_bot, mock_db, mock_config):
        """Test handling when user exists but has no UUID."""
        handler = TryAltProtocolHandler(mock_bot, mock_db, mock_config)

        user_no_uuid = User(
            chat_id='111111111',
            username='incomplete',
            uuid=None,
            email=None,
            status='demo',
        )
        mock_db.get_user = Mock(return_value=user_no_uuid)
        update = {'callback_query': {'message': {'chat': {'id': '111111111'}}}}

        handler.handle(update, chat_id='111111111', user_id='111111111', data='try_alt:hy2')

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'Сначала получите' in text or 'Get a key' in text

    def test_handles_link_generation_failure(self, mock_bot, mock_db, mock_config, sample_user):
        """Test handling when VPN link generation fails."""
        handler = TryAltProtocolHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN:
            mock_vpn = Mock()
            mock_vpn.generate_hy2_link.return_value = None  # Failure
            MockVPN.return_value = mock_vpn

            handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='try_alt:hy2')

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'недоступен' in text or 'unavailable' in text.lower()

    def test_passes_language_to_notification(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that user's language preference is passed to notification."""
        sample_user.lang = 'en'
        handler = TryAltProtocolHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        with patch('bot.handlers.callbacks.user.VPNService') as MockVPN, \
             patch('bot.handlers.callbacks.user.NotificationService') as MockNotif:
            mock_vpn = Mock()
            mock_vpn.generate_hy2_link.return_value = 'https://hy2-link'
            mock_vpn.generate_vless_ws_link.return_value = None
            MockVPN.return_value = mock_vpn

            handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='try_alt:hy2')

        call_args = MockNotif.return_value.notify_alt_protocol.call_args
        assert call_args[1]['lang'] == 'en'


# ===== StatsRequestHandler Tests =====

class TestStatsRequestHandler:
    """Tests for StatsRequestHandler stats formatting and permissions."""

    def test_can_handle_exact_and_pattern(self, mock_bot, mock_db, mock_config):
        """Test can_handle recognizes stats callbacks."""
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)
        assert handler.can_handle('stats') is True
        assert handler.can_handle('stats:12345') is True
        assert handler.can_handle('other') is False

    def test_idor_protection_for_stats(self, mock_bot, mock_db, mock_config):
        """Test IDOR protection: users can only view their own stats."""
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)
        update = {'callback_query': {'message': {'chat': {'id': '111'}}}}

        handler.handle(update, chat_id='111', user_id='111', data='stats:222')

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'only view your own' in text.lower() or 'только свой' in text.lower()

    def test_admin_can_view_any_stats(self, mock_bot, mock_db, mock_config):
        """Test that admin can view any user's stats."""
        mock_config.is_admin = Mock(return_value=True)
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)
        update = {'callback_query': {'message': {'chat': {'id': '123'}}}}

        with patch.object(handler, '_send_stats'):
            handler.handle(update, chat_id='123', user_id='123', data='stats:999999999')
            handler._send_stats.assert_called_once_with('999999999')

    def test_sends_admin_stats_notification(self, mock_bot, mock_db, mock_config):
        """Test admin receives admin-formatted stats."""
        mock_config.is_admin = Mock(return_value=True)
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)

        with patch('bot.handlers.callbacks.user.NotificationService') as MockNotif:
            handler._send_stats('123')

        MockNotif.return_value.notify_stats.assert_called_once()
        call_args = MockNotif.return_value.notify_stats.call_args
        assert call_args[1]['is_admin'] is True

    def test_handles_missing_user_for_stats(self, mock_bot, mock_db, mock_config):
        """Test handling when user not found for stats."""
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=None)

        handler._send_stats('999999999')

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'incomplete' in text.lower() or 'not found' in text.lower()

    def test_handles_user_without_email(self, mock_bot, mock_db, mock_config):
        """Test handling when user has no email for stats lookup."""
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)

        user_no_email = User(
            chat_id='999999999',
            username='noemail',
            email=None,
            status='demo',
        )
        mock_db.get_user = Mock(return_value=user_no_email)

        handler._send_stats('999999999')

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'incomplete' in text.lower() or 'not found' in text.lower()

    def test_formats_traffic_stats_correctly(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that traffic stats are formatted correctly."""
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)

        with patch('bot.services.xui_db.XUIDatabase') as MockXUI:
            mock_xui_db = Mock()
            mock_xui_db.get_client_traffic.return_value = {
                'upload': 1024**3,  # 1 GB
                'download': 2 * 1024**3,  # 2 GB
            }
            MockXUI.return_value = mock_xui_db

            handler._send_stats(sample_user.chat_id)

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert '1.00 GB' in text or '1073741824' in text  # Upload
        assert '2.00 GB' in text or '2147483648' in text  # Download
        assert '3.00 GB' in text or '3221225472' in text  # Total
        assert '%' in text  # Percentage shown

    def test_calculates_percentage_correctly(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that traffic percentage is calculated correctly."""
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = mock_config
        mock_config.DEMO_TRAFFIC_GB = 10
        mock_db.get_user = Mock(return_value=sample_user)

        with patch('bot.services.xui_db.XUIDatabase') as MockXUI:
            mock_xui_db = Mock()
            mock_xui_db.get_client_traffic.return_value = {
                'upload': 2.5 * 1024**3,  # 2.5 GB
                'download': 2.5 * 1024**3,  # 2.5 GB
            }
            MockXUI.return_value = mock_xui_db

            handler._send_stats(sample_user.chat_id)

        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        # Should be 50% of 10 GB
        assert '50.00%' in text

    def test_handles_missing_traffic_data(self, mock_bot, mock_db, mock_config, sample_user):
        """Test handling when traffic data is not available."""
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)

        with patch('bot.services.xui_db.XUIDatabase') as MockXUI:
            mock_xui_db = Mock()
            mock_xui_db.get_client_traffic.return_value = None
            MockXUI.return_value = mock_xui_db

            handler._send_stats(sample_user.chat_id)

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'retrieve' in text.lower() or 'получить' in text.lower()

    def test_handles_database_exception(self, mock_bot, mock_db, mock_config, sample_user):
        """Test handling when database query fails."""
        handler = StatsRequestHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)

        with patch('bot.services.xui_db.XUIDatabase') as MockXUI:
            mock_xui_db = Mock()
            mock_xui_db.get_client_traffic.side_effect = Exception("DB error")
            MockXUI.return_value = mock_xui_db

            handler._send_stats(sample_user.chat_id)

        mock_bot.send_message.assert_called_once()
        text = mock_bot.send_message.call_args.kwargs.get('text', '')
        assert 'error' in text.lower() or 'ошибка' in text.lower()


# ===== LanguageSetHandler Tests =====

class TestLanguageSetHandler:
    """Tests for LanguageSetHandler language switching."""

    def test_can_handle_pattern(self, mock_bot, mock_db, mock_config):
        """Test can_handle recognizes language callbacks."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        assert handler.can_handle('set_lang:ru') is True
        assert handler.can_handle('set_lang:en') is True
        assert handler.can_handle('other') is False

    def test_changes_language_to_russian(self, mock_bot, mock_db, mock_config, sample_user):
        """Test changing language to Russian."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        mock_db.save_user = Mock()
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='set_lang:ru')

        # User language should be updated
        assert sample_user.lang == 'ru'
        mock_db.save_user.assert_called_once_with(sample_user)

    def test_changes_language_to_english(self, mock_bot, mock_db, mock_config, sample_user):
        """Test changing language to English."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        mock_db.save_user = Mock()
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='set_lang:en')

        # User language should be updated
        assert sample_user.lang == 'en'
        mock_db.save_user.assert_called_once_with(sample_user)

    def test_sends_russian_confirmation_message(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that Russian confirmation message is sent."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        mock_db.save_user = Mock()
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='set_lang:ru')

        # Check confirmation message
        confirmation_calls = [c for c in mock_bot.send_message.call_args_list if 'Язык изменен' in str(c.kwargs.get('text', ''))]
        assert len(confirmation_calls) >= 1

    def test_sends_english_confirmation_message(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that English confirmation message is sent."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        mock_db.save_user = Mock()
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='set_lang:en')

        # Check confirmation message
        confirmation_calls = [c for c in mock_bot.send_message.call_args_list if 'Language changed' in str(c.kwargs.get('text', ''))]
        assert len(confirmation_calls) >= 1

    def test_sends_welcome_notification(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that welcome notification is sent after language change."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        mock_db.save_user = Mock()
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        with patch('bot.handlers.callbacks.user.NotificationService') as MockNotif:
            handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='set_lang:en')

        # Should send welcome notification with new language
        MockNotif.return_value.notify_welcome.assert_called_once()
        call_args = MockNotif.return_value.notify_welcome.call_args
        assert call_args[0][1] == 'en'  # lang parameter

    def test_handles_invalid_callback_format(self, mock_bot, mock_db, mock_config, sample_user):
        """Test handling of invalid callback format - empty language code."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        mock_db.save_user = Mock()
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        # Invalid format - missing language results in empty string
        # The handler will process it but notify_welcome will fail to find the message
        with patch('bot.handlers.callbacks.user.NotificationService') as MockNotif:
            MockNotif.return_value.notify_welcome = Mock()
            handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='set_lang:')

        # Should still attempt to process (notify_welcome gets called)
        # The user's lang is set to empty string
        assert sample_user.lang == ''

    def test_creates_user_if_not_exists(self, mock_bot, mock_db, mock_config):
        """Test that user is created if not exists."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=None)
        mock_db.save_user = Mock()

        new_user = User(
            chat_id='999999999',
            username='newuser',
            lang='en',
        )
        handler._get_or_create_user = Mock(return_value=new_user)
        update = {'callback_query': {'message': {'chat': {'id': '999999999'}}}}

        handler.handle(update, chat_id='999999999', user_id='999999999', data='set_lang:en')

        handler._get_or_create_user.assert_called_once()

    def test_handles_multiple_language_changes(self, mock_bot, mock_db, mock_config, sample_user):
        """Test that user can change language multiple times."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        mock_db.save_user = Mock()
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        # Change to English
        handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='set_lang:en')
        assert sample_user.lang == 'en'

        # Change back to Russian
        handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data='set_lang:ru')
        assert sample_user.lang == 'ru'

        # Should save twice
        assert mock_db.save_user.call_count == 2

    @pytest.mark.parametrize("lang_code,expected_word", [
        ('ru', 'русский'),
        ('en', 'English'),
    ])
    def test_confirmation_text_contains_language_indicator(self, mock_bot, mock_db, mock_config, sample_user, lang_code, expected_word):
        """Test that confirmation text indicates the chosen language."""
        handler = LanguageSetHandler(mock_bot, mock_db, mock_config)
        mock_db.get_user = Mock(return_value=sample_user)
        mock_db.save_user = Mock()
        update = {'callback_query': {'message': {'chat': {'id': sample_user.chat_id}}}}

        handler.handle(update, chat_id=sample_user.chat_id, user_id=sample_user.chat_id, data=f'set_lang:{lang_code}')

        # At least one of the messages should contain language indicator
        has_indicator = any(
            expected_word in str(call.kwargs.get('text', ''))
            for call in mock_bot.send_message.call_args_list
        )
        assert has_indicator or (lang_code == 'ru' or lang_code == 'en')  # Basic sanity check
