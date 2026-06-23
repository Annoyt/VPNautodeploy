"""Tests for key creation rollback on sync failure.

Verifies that user DB state remains clean when X-UI sync fails.
"""

import pytest
from unittest.mock import Mock, patch, AsyncMock

from bot.handlers.callbacks.user import GetKeyHandler
from bot.utils.exceptions import VPNBotError


class TestKeyCreationRollback:
    """Test that DB save only happens after successful X-UI sync."""

    @pytest.fixture
    def handler(self):
        mock_bot = Mock()
        mock_bot.services = {'xui': Mock()}
        mock_db = Mock()
        mock_config = Mock()
        mock_config.DEMO_TRAFFIC_GB = 5
        mock_config.DEMO_DAYS = 7

        handler = GetKeyHandler(mock_bot, mock_db, mock_config)
        return handler

    @pytest.mark.asyncio
    async def test_user_not_saved_on_sync_failure(self, handler):
        """Test that user.uuid/email are not persisted when X-UI sync fails."""
        from bot.services.vpn import VPNService

        mock_user = Mock()
        mock_user.uuid = None
        mock_user.email = None
        mock_user.username = 'testuser'
        mock_user.status = 'demo'  # required by GetKeyHandler status guard

        handler.validator.validate_user_exists = Mock(return_value=mock_user)

        # Simulate X-UI sync failure
        handler.bot.services['xui'].sync_user = AsyncMock(return_value=False)

        with patch('bot.handlers.callbacks.user.VPNService.create_client_config', return_value={
            'id': 'test-uuid-123',
            'email': 'user_testuser_123@nekovo.ru',
            'flow': 'xtls-rprx-vision',
            'limitIp': 1,
            'totalGB': 5 * 1024 ** 3,
            'expiryTime': 0,
            'enable': True
        }):
            with pytest.raises(VPNBotError):
                await handler._process_key_request('12345')

        # DB save should NOT have been called because sync failed
        handler.db.save_user.assert_not_called()
        # But user object was mutated in memory (expected)
        assert mock_user.uuid == 'test-uuid-123'
        assert mock_user.email == 'user_testuser_123@nekovo.ru'

    @pytest.mark.asyncio
    async def test_user_saved_on_sync_success(self, handler):
        """Test that user.uuid/email are persisted when X-UI sync succeeds."""
        from bot.services.vpn import VPNService

        mock_user = Mock()
        mock_user.uuid = None
        mock_user.email = None
        mock_user.username = 'testuser'
        mock_user.status = 'demo'  # required by GetKeyHandler status guard

        handler.validator.validate_user_exists = Mock(return_value=mock_user)

        # Simulate successful X-UI sync
        handler.bot.services['xui'].sync_user = AsyncMock(return_value=True)

        with patch('bot.handlers.callbacks.user.VPNService.create_client_config', return_value={
            'id': 'test-uuid-123',
            'email': 'user_testuser_123@nekovo.ru',
            'flow': 'xtls-rprx-vision',
            'limitIp': 1,
            'totalGB': 5 * 1024 ** 3,
            'expiryTime': 0,
            'enable': True
        }):
            with patch.object(handler, '_send_key_to_user', new_callable=AsyncMock):
                await handler._process_key_request('12345')

        # DB save SHOULD have been called after successful sync
        handler.db.save_user.assert_called_once_with(mock_user)

    @pytest.mark.asyncio
    async def test_resync_does_not_call_save_user(self, handler):
        """Test that resync branch does not redundantly save user."""
        mock_user = Mock()
        mock_user.uuid = 'existing-uuid'
        mock_user.email = 'existing@nekovo.ru'
        mock_user.status = 'demo'  # required by GetKeyHandler status guard

        handler.validator.validate_user_exists = Mock(return_value=mock_user)
        handler.bot.services['xui'].sync_user = AsyncMock(return_value=True)

        with patch.object(handler, '_send_key_to_user', new_callable=AsyncMock):
            await handler._process_key_request('12345')

        # save_user should NOT be called in resync path
        handler.db.save_user.assert_not_called()
