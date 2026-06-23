"""Tests for stabilization fixes from VPN_FINAL_STABILIZATION.md.

Covers:
- C-04: BANNED users cannot request demo
- H-01: WebAppServer accepts shared xui_service
- H-05: notify_approved defaults lang to 'ru' when None
- M-05: cleanup_services closes aiohttp session
- C-02: sync_user uses asyncio.to_thread for sync calls
"""

import asyncio
import pytest
from unittest.mock import MagicMock, Mock, patch, AsyncMock


class TestBannedUserDemoRequest:
    """Test C-04: BANNED users cannot request demo."""

    def test_can_request_demo_banned_user(self):
        """Test that BANNED users cannot request demo."""
        from bot.handlers.callbacks.user import DemoRequestHandler
        from bot.config import UserState

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()

        handler = DemoRequestHandler(bot, db, config)

        banned_user = MagicMock()
        banned_user.status = UserState.BANNED.value

        assert handler._can_request_demo(banned_user) is False

    def test_can_request_demo_new_user(self):
        """Test that NEW users can request demo."""
        from bot.handlers.callbacks.user import DemoRequestHandler
        from bot.config import UserState

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()

        handler = DemoRequestHandler(bot, db, config)

        new_user = MagicMock()
        new_user.status = UserState.NEW.value

        assert handler._can_request_demo(new_user) is True


class TestWebAppServerSharedXUIService:
    """Test H-01: WebAppServer accepts shared xui_service."""

    def test_init_with_shared_xui_service(self):
        """Test that WebAppServer uses passed xui_service instead of creating new."""
        from bot.core.web_server import WebAppServer

        config = MagicMock()
        db = MagicMock()
        shared_xui = MagicMock()

        with patch('bot.core.web_server.XUIService') as mock_xui_cls:
            server = WebAppServer(config, db, xui_service=shared_xui)

            # Should NOT create new XUIService
            mock_xui_cls.assert_not_called()
            # Should use shared instance
            assert server.xui is shared_xui

    def test_init_without_xui_service_creates_default(self):
        """Test that WebAppServer creates XUIService when none passed."""
        from bot.core.web_server import WebAppServer

        config = MagicMock()
        db = MagicMock()

        with patch('bot.core.web_server.XUIService') as mock_xui_cls:
            mock_instance = MagicMock()
            mock_xui_cls.return_value = mock_instance

            server = WebAppServer(config, db)

            mock_xui_cls.assert_called_once_with(config)
            assert server.xui is mock_instance


class TestNotifyApprovedLangDefault:
    """Test H-05: notify_approved uses 'ru' when target.lang is None."""

    def test_notify_approved_with_none_lang(self):
        """Test that None lang defaults to 'ru'."""
        from bot.handlers.admin.users import AdminUsersMixin
        from bot.config import UserState

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()

        handler = AdminUsersMixin(bot, db, config)

        target = MagicMock()
        target.chat_id = '123'
        target.username = 'testuser'
        target.status = UserState.PENDING_DEMO.value
        target.lang = None  # None lang

        db.get_user.return_value = target

        with patch('bot.handlers.admin.users.StateMachine'):
            with patch('bot.handlers.admin.users.NotificationService') as mock_ns:
                notifier = mock_ns.return_value
                handler.approve_user('admin', ['123'])

                # Should call with 'ru' default, not None
                notifier.notify_approved.assert_called_once_with('123', 'ru')

    def test_notify_payment_approved_with_none_lang(self):
        """Test that None lang defaults to 'ru' in payment approval."""
        from bot.handlers.admin.users import AdminUsersMixin
        from bot.config import UserState

        bot = MagicMock()
        db = MagicMock()
        config = MagicMock()

        handler = AdminUsersMixin(bot, db, config)

        target = MagicMock()
        target.chat_id = '123'
        target.username = 'testuser'
        target.status = UserState.PAID.value
        target.lang = None  # None lang

        db.get_user.return_value = target

        with patch('bot.handlers.admin.users.StateMachine'):
            with patch('bot.handlers.admin.users.NotificationService') as mock_ns:
                notifier = mock_ns.return_value
                handler.approve_payment('admin', ['123'])

                # Should call with 'ru' default, not None
                notifier.notify_payment_approved.assert_called_once_with('123', 'ru')


class TestCleanupServicesClosesSession:
    """Test M-05: cleanup_services closes aiohttp session."""

    def test_cleanup_closes_xui_session(self):
        """Test that cleanup_services closes X-UI API session."""
        from bot.main import cleanup_services

        mock_session = MagicMock()
        mock_session.close = MagicMock(return_value=None)

        mock_api = MagicMock()
        mock_api.session = mock_session

        mock_xui = MagicMock()
        mock_xui.api = mock_api

        services = {'xui': mock_xui}

        with patch('asyncio.run') as mock_asyncio_run:
            cleanup_services(services)

            # Should attempt to close session via asyncio.run
            mock_asyncio_run.assert_called_once()

    def test_cleanup_without_xui_service(self):
        """Test that cleanup_services handles missing xui gracefully."""
        from bot.main import cleanup_services

        services = {}

        # Should not raise
        cleanup_services(services)

    def test_cleanup_xui_without_session(self):
        """Test that cleanup_services handles xui without session gracefully."""
        from bot.main import cleanup_services

        mock_xui = MagicMock()
        mock_xui.api = MagicMock()
        # No session attribute
        del mock_xui.api.session

        services = {'xui': mock_xui}

        # Should not raise
        cleanup_services(services)


class TestSyncUserUsesToThread:
    """Test C-02: sync_user wraps sync calls in asyncio.to_thread."""

    @pytest.mark.asyncio
    async def test_sync_user_calls_add_client_in_thread(self):
        """Test that sync_user calls add_client_sync via asyncio.to_thread."""
        from bot.services.xui_service import XUIService

        with patch('bot.services.xui_service.XUIAPIClient'):
            with patch('bot.services.xui_service.XUIDatabase'):
                service = XUIService(db_path='/tmp/test.db', api_config={
                    'base_url': 'http://test:2053',
                    'username': 'admin',
                    'password': 'admin'
                })

                service.add_client_sync = MagicMock(return_value=True)
                service.reload_xray_sync = MagicMock(return_value=True)

                client_config = {
                    'email': 'test@nekovo.ru',
                    'id': 'uuid-test',
                    'enable': True
                }

                with patch('bot.services.xui_service.asyncio.to_thread') as mock_to_thread:
                    async def side_effect(func, *args):
                        return func(*args)
                    mock_to_thread.side_effect = side_effect

                    result = await service.sync_user('123', client_config)

                    assert result is True
                    # Should call to_thread for add_client_sync
                    assert mock_to_thread.call_count >= 1
                    # First call should be add_client_sync
                    first_call = mock_to_thread.call_args_list[0]
                    assert first_call[0][0] == service.add_client_sync

    @pytest.mark.asyncio
    async def test_sync_user_calls_reload_in_thread(self):
        """Test that sync_user calls reload_xray_sync via asyncio.to_thread."""
        from bot.services.xui_service import XUIService

        with patch('bot.services.xui_service.XUIAPIClient'):
            with patch('bot.services.xui_service.XUIDatabase'):
                service = XUIService(db_path='/tmp/test.db', api_config={
                    'base_url': 'http://test:2053',
                    'username': 'admin',
                    'password': 'admin'
                })

                service.add_client_sync = MagicMock(return_value=True)
                service.reload_xray_sync = MagicMock(return_value=True)

                client_config = {
                    'email': 'test2@nekovo.ru',
                    'id': 'uuid-test-2',
                    'enable': True
                }

                with patch('bot.services.xui_service.asyncio.to_thread') as mock_to_thread:
                    async def side_effect(func, *args):
                        return func(*args)
                    mock_to_thread.side_effect = side_effect

                    result = await service.sync_user('123', client_config)

                    assert result is True
                    # Should call to_thread for reload_xray_sync too
                    call_targets = [call[0][0] for call in mock_to_thread.call_args_list]
                    assert service.reload_xray_sync in call_targets
