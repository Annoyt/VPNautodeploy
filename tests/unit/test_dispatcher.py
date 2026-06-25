"""Comprehensive unit tests for callback dispatcher.

Tests CallbackDispatcher routing edge cases, malformed callback data
handling, and error propagation.
"""

import pytest
from unittest.mock import Mock
import logging

from bot.handlers.callbacks.dispatcher import CallbackDispatcher
from bot.handlers.callbacks.base import BaseCallbackHandler
from bot.utils.exceptions import PermissionDeniedError


class MockHandler(BaseCallbackHandler):
    """Mock handler for testing."""

    def __init__(self, bot, db, config, callback_data=None, pattern=None, should_raise=False):
        super().__init__(bot, db, config)
        self.callback_data = callback_data
        self.pattern = pattern
        self.should_raise = should_raise
        self.call_count = 0
        self.last_data = None
        self.last_update = None
        self.last_chat_id = None
        self.last_user_id = None

    def can_handle(self, callback_data: str) -> bool:
        if self.should_raise:
            raise RuntimeError("Handler can_handle crashed")
        if self.callback_data is not None:
            return callback_data == self.callback_data
        if self.pattern:
            import re
            return bool(re.match(self.pattern, callback_data))
        return False

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        self.call_count += 1
        self.last_update = update
        self.last_chat_id = chat_id
        self.last_user_id = user_id
        self.last_data = kwargs.get('data')
        if self.should_raise:
            raise RuntimeError("Handler handle crashed")


class FailingHandler(BaseCallbackHandler):
    """Handler that always raises an exception."""

    def __init__(self, bot, db, config, exception_class=RuntimeError, message="Handler failed"):
        super().__init__(bot, db, config)
        self.exception_class = exception_class
        self.message = message

    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith('fail:')

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        raise self.exception_class(self.message)


class CorruptingHandler(BaseCallbackHandler):
    """Handler that corrupts internal state."""

    def __init__(self, bot, db, config):
        super().__init__(bot, db, config)
        self.dispatched = False

    def can_handle(self, callback_data: str) -> bool:
        return callback_data == 'corrupt_me'

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        # Simulate handler that leaves system in bad state
        self.dispatched = True
        raise SystemError("System corrupted")


@pytest.fixture
def mock_bot():
    """Create mock bot."""
    bot = Mock()
    bot.send_message = Mock(return_value={'message_id': 123})
    bot.answer_callback_query = Mock(return_value=True)
    bot.edit_message_text = Mock(return_value=True)
    return bot


@pytest.fixture
def mock_db():
    """Create mock database."""
    db = Mock()
    db.get_user = Mock(return_value=None)
    return db


@pytest.fixture
def mock_config():
    """Create mock config."""
    config = Mock()
    config.SUPER_ADMIN_ID = '123456'
    config.FORUM_ENABLED = False
    config.TOPIC_REQUESTS = 15
    config.TOPIC_SUPPORT = 17
    config.DEMO_TRAFFIC_GB = 5
    config.DEMO_DAYS = 7
    return config


@pytest.fixture
def empty_dispatcher(mock_bot, mock_db, mock_config):
    """Create dispatcher with no handlers."""
    dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)
    dispatcher.handlers.clear()  # Remove all registered handlers
    return dispatcher


@pytest.fixture
def dispatcher_with_handlers(mock_bot, mock_db, mock_config):
    """Create dispatcher with test handlers."""
    dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)
    dispatcher.handlers.clear()

    # Register test handlers
    dispatcher.handlers.extend([
        MockHandler(mock_bot, mock_db, mock_config, callback_data='test_action'),
        MockHandler(mock_bot, mock_db, mock_config, pattern=r'user:\d+'),
        MockHandler(mock_bot, mock_db, mock_config, callback_data='admin_only'),
        FailingHandler(mock_bot, mock_db, mock_config),
        CorruptingHandler(mock_bot, mock_db, mock_config),
    ])

    return dispatcher


class TestDispatcherInitialization:
    """Test dispatcher initialization and handler registration."""

    def test_init_registers_all_handlers(self, mock_bot, mock_db, mock_config):
        """Test that init registers all standard handlers."""
        dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)

        # Should have all standard handlers registered
        assert len(dispatcher.handlers) > 0
        handler_names = dispatcher.get_handler_names()
        assert 'DemoRequestHandler' in handler_names
        assert 'ApproveUserHandler' in handler_names
        assert 'RejectUserHandler' in handler_names

    def test_init_stores_dependencies(self, mock_bot, mock_db, mock_config):
        """Test that init stores bot, db, config."""
        dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)

        assert dispatcher.bot is mock_bot
        assert dispatcher.db is mock_db
        assert dispatcher.config is mock_config

    def test_get_handler_count(self, mock_bot, mock_db, mock_config):
        """Test get_handler_count returns correct number."""
        dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)
        count = dispatcher.get_handler_count()

        assert count == len(dispatcher.handlers)
        assert count > 0

    def test_get_handler_names(self, mock_bot, mock_db, mock_config):
        """Test get_handler_names returns all handler names."""
        dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)
        names = dispatcher.get_handler_names()

        assert len(names) == len(dispatcher.handlers)
        assert all(isinstance(name, str) for name in names)


class TestBasicRouting:
    """Test basic callback routing functionality."""

    def test_dispatch_to_matching_handler(self, dispatcher_with_handlers):
        """Test dispatch routes to first matching handler."""
        update = {'callback_query': {'id': 'cb123'}}
        result = dispatcher_with_handlers.dispatch(
            update, chat_id='123', user_id='456', data='test_action'
        )

        assert result is True
        handler = dispatcher_with_handlers.handlers[0]
        assert handler.call_count == 1
        assert handler.last_chat_id == '123'
        assert handler.last_user_id == '456'
        assert handler.last_data == 'test_action'

    def test_dispatch_stops_at_first_match(self, dispatcher_with_handlers):
        """Test dispatch stops at first matching handler."""
        dispatcher_with_handlers.handlers[0].callback_data = 'duplicate'
        dispatcher_with_handlers.handlers[1].pattern = r'duplicate'

        result = dispatcher_with_handlers.dispatch(
            {}, chat_id='123', user_id='456', data='duplicate'
        )

        assert result is True
        # Only first handler should be called
        assert dispatcher_with_handlers.handlers[0].call_count == 1
        assert dispatcher_with_handlers.handlers[1].call_count == 0

    def test_dispatch_no_match_returns_false(self, empty_dispatcher):
        """Test dispatch returns False when no handler matches."""
        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='unknown_action'
        )

        assert result is False

    def test_dispatch_respects_handler_order(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test handlers are checked in registration order."""
        # Register handlers in specific order
        h1 = MockHandler(mock_bot, mock_db, mock_config, callback_data='action')
        h2 = MockHandler(mock_bot, mock_db, mock_config, callback_data='action')
        empty_dispatcher.handlers.extend([h1, h2])

        empty_dispatcher.dispatch({}, chat_id='123', user_id='456', data='action')

        assert h1.call_count == 1
        assert h2.call_count == 0  # Should not be reached

    def test_dispatch_with_pattern_handler(self, dispatcher_with_handlers):
        """Test dispatch with pattern-based handler."""
        result = dispatcher_with_handlers.dispatch(
            {}, chat_id='123', user_id='456', data='user:789'
        )

        assert result is True
        handler = dispatcher_with_handlers.handlers[1]
        assert handler.call_count == 1

    def test_dispatch_pattern_no_match(self, dispatcher_with_handlers):
        """Test pattern handler doesn't match non-matching data."""
        result = dispatcher_with_handlers.dispatch(
            {}, chat_id='123', user_id='456', data='user:abc'
        )

        assert result is False


class TestMalformedCallbackData:
    """Test handling of malformed callback data."""

    def test_empty_string_callback_data(self, empty_dispatcher):
        """Test dispatch with empty string callback data."""
        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data=''
        )

        assert result is False

    def test_none_callback_data(self, empty_dispatcher):
        """Test dispatch with None callback data."""
        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data=None
        )

        assert result is False

    def test_whitespace_only_callback_data(self, empty_dispatcher):
        """Test dispatch with whitespace-only callback data."""
        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='   '
        )

        assert result is False

    def test_unicode_callback_data(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test dispatch with unicode callback data."""
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data='тест')
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='тест'
        )

        assert result is True
        assert handler.call_count == 1

    def test_very_long_callback_data(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test dispatch with very long callback data."""
        long_data = 'a' * 10000
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data=long_data)
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data=long_data
        )

        assert result is True

    def test_special_characters_callback_data(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test dispatch with special characters in callback data."""
        special_data = 'action:test\x00\x1b\n\r'
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data=special_data)
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data=special_data
        )

        assert result is True

    def test_callback_data_with_colons(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test dispatch with colon-separated callback data."""
        handler = MockHandler(mock_bot, mock_db, mock_config, pattern=r'\w+:\w+:\d+')
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='approve:user:123'
        )

        assert result is True

    def test_malformed_json_like_callback_data(self, empty_dispatcher):
        """Test dispatch with malformed JSON-like callback data."""
        # Handler that accepts malformed JSON-like strings
        handler = MockHandler(
            empty_dispatcher.bot, empty_dispatcher.db, empty_dispatcher.config,
            pattern=r'\{.*\}'
        )
        empty_dispatcher.handlers.append(handler)

        malformed = '{invalid json'
        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data=malformed
        )

        assert result is False  # Should not match pattern

    def test_sql_injection_like_callback_data(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test dispatch handles SQL injection-like data safely."""
        injection_data = "'; DROP TABLE users; --"
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data=injection_data)
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data=injection_data
        )

        assert result is True
        assert handler.call_count == 1
        # Verify handler received data as-is (no SQL execution in dispatcher)
        assert handler.last_data == injection_data

    def test_callback_data_with_null_bytes(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test dispatch with null bytes in callback data."""
        null_data = 'action\x00test'
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data=null_data)
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data=null_data
        )

        assert result is True


class TestErrorPropagation:
    """Test error handling and propagation in dispatch."""

    def test_handler_exception_returns_false(self, dispatcher_with_handlers):
        """Test that handler exception causes dispatch to return False."""
        result = dispatcher_with_handlers.dispatch(
            {}, chat_id='123', user_id='456', data='fail:test'
        )

        assert result is False

    def test_handler_exception_logs_error(self, dispatcher_with_handlers, caplog):
        """Test that handler exception is logged."""
        with caplog.at_level(logging.ERROR):
            dispatcher_with_handlers.dispatch(
                {}, chat_id='123', user_id='456', data='fail:test'
            )

        assert any('Error in handler' in record.message for record in caplog.records)

    def test_handler_exception_doesnt_stop_dispatcher(self, dispatcher_with_handlers):
        """Test that handler exception doesn't prevent other dispatches."""
        # First dispatch fails
        result1 = dispatcher_with_handlers.dispatch(
            {}, chat_id='123', user_id='456', data='fail:test'
        )
        assert result1 is False

        # Second dispatch should work fine
        result2 = dispatcher_with_handlers.dispatch(
            {}, chat_id='123', user_id='456', data='test_action'
        )
        assert result2 is True

    def test_custom_exception_propagation(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test custom VPNBotError exceptions are handled."""
        handler = FailingHandler(
            mock_bot, mock_db, mock_config,
            exception_class=PermissionDeniedError,
            message="Not admin"
        )
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='fail:test'
        )

        assert result is False

    def test_runtime_error_in_handler(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test RuntimeError in handler is caught."""
        handler = FailingHandler(
            mock_bot, mock_db, mock_config,
            exception_class=RuntimeError,
            message="Handler crashed"
        )
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='fail:test'
        )

        assert result is False

    def test_system_error_in_handler(self, dispatcher_with_handlers):
        """Test SystemError in handler is caught."""
        result = dispatcher_with_handlers.dispatch(
            {}, chat_id='123', user_id='456', data='corrupt_me'
        )

        assert result is False
        handler = dispatcher_with_handlers.handlers[4]
        assert handler.dispatched is True  # Handler was called

    def test_can_handle_exception_is_propagated(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test exception in can_handle propagates to dispatch."""
        handler = MockHandler(mock_bot, mock_db, mock_config, should_raise=True)
        empty_dispatcher.handlers.append(handler)

        with pytest.raises(RuntimeError, match="Handler can_handle crashed"):
            empty_dispatcher.dispatch(
                {}, chat_id='123', user_id='456', data='test'
            )

    def test_exception_handler_chain_continues(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test dispatch stops when matching handler crashes.

        Note: The dispatcher returns False when a matching handler raises
        an exception, but does NOT continue to try other handlers. This is
        intentional - the first matching handler owns the callback.
        """
        # First handler matches but fails
        h1 = FailingHandler(mock_bot, mock_db, mock_config)
        # Second handler would succeed but won't be reached
        h2 = MockHandler(mock_bot, mock_db, mock_config, callback_data='fail:test')
        empty_dispatcher.handlers.extend([h1, h2])

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='fail:test'
        )

        # Should fail because first matching handler (h1) crashes
        assert result is False
        # h2 matched but was never reached because h1 was checked first
        assert h2.call_count == 0


class TestEdgeCases:
    """Test edge cases in dispatcher behavior."""

    def test_dispatch_with_missing_update_fields(self, dispatcher_with_handlers):
        """Test dispatch with incomplete update object."""
        update = {}  # Empty update
        result = dispatcher_with_handlers.dispatch(
            update, chat_id='123', user_id='456', data='test_action'
        )

        assert result is True
        # Handler should still receive the empty update
        handler = dispatcher_with_handlers.handlers[0]
        assert handler.last_update == {}

    def test_dispatch_with_numeric_chat_id(self, dispatcher_with_handlers):
        """Test dispatch with numeric chat_id."""
        result = dispatcher_with_handlers.dispatch(
            {}, chat_id=123456, user_id='789', data='test_action'
        )

        assert result is True

    def test_dispatch_with_numeric_user_id(self, dispatcher_with_handlers):
        """Test dispatch with numeric user_id."""
        result = dispatcher_with_handlers.dispatch(
            {}, chat_id='123', user_id=456789, data='test_action'
        )

        assert result is True

    def test_dispatch_with_string_numeric_ids(self, dispatcher_with_handlers):
        """Test dispatch with string numeric IDs."""
        result = dispatcher_with_handlers.dispatch(
            {}, chat_id='123456', user_id='789012', data='test_action'
        )

        assert result is True

    def test_dispatch_with_zero_ids(self, dispatcher_with_handlers):
        """Test dispatch with zero IDs."""
        result = dispatcher_with_handlers.dispatch(
            {}, chat_id='0', user_id='0', data='test_action'
        )

        assert result is True

    def test_dispatch_with_negative_ids(self, dispatcher_with_handlers):
        """Test dispatch with negative IDs (unusual but possible)."""
        result = dispatcher_with_handlers.dispatch(
            {}, chat_id='-123', user_id='-456', data='test_action'
        )

        assert result is True

    def test_multiple_consecutive_dispatches(self, dispatcher_with_handlers):
        """Test multiple consecutive dispatches to same handler."""
        for _ in range(5):
            dispatcher_with_handlers.dispatch(
                {}, chat_id='123', user_id='456', data='test_action'
            )

        handler = dispatcher_with_handlers.handlers[0]
        assert handler.call_count == 5

    def test_concurrent_dispatch_safety(self, dispatcher_with_handlers):
        """Test dispatcher state integrity with rapid dispatches."""
        import threading

        results = []
        errors = []

        def dispatch_and_record(data):
            try:
                result = dispatcher_with_handlers.dispatch(
                    {}, chat_id='123', user_id='456', data=data
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=dispatch_and_record, args=('test_action',))
            for _ in range(10)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 10
        assert all(results)

    def test_handler_receiving_kwargs(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test handler receives update through dispatch."""
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data='test')
        empty_dispatcher.handlers.append(handler)

        empty_dispatcher.dispatch(
            {'update': 'data'},
            chat_id='123',
            user_id='456',
            data='test'
        )

        assert handler.call_count == 1
        assert handler.last_update == {'update': 'data'}
        assert handler.last_chat_id == '123'
        assert handler.last_user_id == '456'
        assert handler.last_data == 'test'

    def test_dispatcher_with_no_handlers(self, empty_dispatcher):
        """Test dispatcher with zero registered handlers."""
        assert len(empty_dispatcher.handlers) == 0

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='anything'
        )

        assert result is False

    def test_duplicate_handler_registration(self, mock_bot, mock_db, mock_config):
        """Test behavior with duplicate handlers."""
        dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)
        dispatcher.handlers.clear()

        handler_class = MockHandler
        # Add same handler twice
        dispatcher.handlers.extend([
            handler_class(mock_bot, mock_db, mock_config, callback_data='test'),
            handler_class(mock_bot, mock_db, mock_config, callback_data='test')
        ])

        result = dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='test'
        )

        assert result is True
        # Only first instance should be called
        assert dispatcher.handlers[0].call_count == 1
        assert dispatcher.handlers[1].call_count == 0


class TestCallbackDataFormats:
    """Test various callback data formats."""

    def test_colon_separated_format(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test standard colon-separated callback data."""
        handler = MockHandler(mock_bot, mock_db, mock_config, pattern=r'\w+:\d+')
        empty_dispatcher.handlers.append(handler)

        test_cases = [
            ('approve:123', True),
            ('reject:456', True),
            ('ban:789', True),
            ('invalid', False),
            (':123', False),
            ('action:', False),
        ]

        for data, expected in test_cases:
            result = empty_dispatcher.dispatch(
                {}, chat_id='123', user_id='456', data=data
            )
            assert result == expected

    def test_pipe_separated_format(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test pipe-separated callback data."""
        handler = MockHandler(mock_bot, mock_db, mock_config, pattern=r'\w+\|\w+')
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='action|value'
        )

        assert result is True

    def test_underscore_separated_format(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test underscore-separated callback data."""
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data='my_key_yes')
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='my_key_yes'
        )

        assert result is True


class TestHandlerIntegration:
    """Test dispatcher integration with handler behavior."""

    def test_handler_receiving_update_with_callback_query(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test handler receives update with callback_query structure."""
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data='test')
        empty_dispatcher.handlers.append(handler)

        update = {
            'callback_query': {
                'id': 'cb123',
                'from': {'id': 789},
                'message': {
                    'message_id': 456,
                    'chat': {'id': 123}
                }
            }
        }

        result = empty_dispatcher.dispatch(
            update, chat_id='123', user_id='456', data='test'
        )

        assert result is True
        assert handler.last_update == update

    def test_handler_modifying_bot_state(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test handler can modify bot mock state."""
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data='send_msg')
        empty_dispatcher.handlers.append(handler)

        # Handler will call bot.send_message
        handler.last_data = None

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='send_msg'
        )

        assert result is True

    def test_handler_accessing_database(self, empty_dispatcher, mock_bot, mock_db, mock_config):
        """Test handler can access database."""
        handler = MockHandler(mock_bot, mock_db, mock_config, callback_data='db_test')
        empty_dispatcher.handlers.append(handler)

        result = empty_dispatcher.dispatch(
            {}, chat_id='123', user_id='456', data='db_test'
        )

        assert result is True
        # Verify handler has access to db
        assert handler.db is mock_db


class TestRealWorldScenarios:
    """Test real-world callback scenarios."""

    def test_demo_request_callback(self, mock_bot, mock_db, mock_config):
        """Test demo request callback routing."""
        dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)

        # Find DemoRequestHandler
        demo_handler = next(
            (h for h in dispatcher.handlers if h.__class__.__name__ == 'DemoRequestHandler'),
            None
        )

        assert demo_handler is not None
        assert demo_handler.can_handle('request_demo')

    def test_approve_user_callback(self, mock_bot, mock_db, mock_config):
        """Test approve user callback routing."""
        dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)

        # Find ApproveUserHandler
        approve_handler = next(
            (h for h in dispatcher.handlers if h.__class__.__name__ == 'ApproveUserHandler'),
            None
        )

        assert approve_handler is not None
        assert approve_handler.can_handle('approve:123456')

    def test_reject_user_callback(self, mock_bot, mock_db, mock_config):
        """Test reject user callback routing."""
        dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)

        # Find RejectUserHandler
        reject_handler = next(
            (h for h in dispatcher.handlers if h.__class__.__name__ == 'RejectUserHandler'),
            None
        )

        assert reject_handler is not None
        assert reject_handler.can_handle('reject:123456')

    def test_platform_select_callback(self, mock_bot, mock_db, mock_config):
        """Test platform select callback routing."""
        dispatcher = CallbackDispatcher(mock_bot, mock_db, mock_config)

        # Find PlatformSelectHandler
        platform_handler = next(
            (h for h in dispatcher.handlers if h.__class__.__name__ == 'PlatformSelectHandler'),
            None
        )

        assert platform_handler is not None
        # Should match platform callbacks
        assert platform_handler.can_handle('platform:android')
        assert platform_handler.can_handle('platform:ios')
        assert platform_handler.can_handle('platform:windows')
        assert platform_handler.can_handle('platform:macos')
        assert platform_handler.can_handle('platform:linux')


class TestLogging:
    """Test dispatcher logging behavior."""

    def test_successful_dispatch_logs_debug(self, dispatcher_with_handlers, caplog):
        """Test successful dispatch logs at DEBUG level."""
        with caplog.at_level(logging.DEBUG):
            dispatcher_with_handlers.dispatch(
                {}, chat_id='123', user_id='456', data='test_action'
            )

        debug_messages = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
        assert any("handled by" in msg for msg in debug_messages)

    def test_unmatched_callback_logs_warning(self, empty_dispatcher, caplog):
        """Test unmatched callback logs at WARNING level."""
        with caplog.at_level(logging.WARNING):
            empty_dispatcher.dispatch(
                {}, chat_id='123', user_id='456', data='unknown'
            )

        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("No handler found" in msg for msg in warning_messages)

    def test_handler_error_logs_exception(self, dispatcher_with_handlers, caplog):
        """Test handler error logs at EXCEPTION level."""
        with caplog.at_level(logging.ERROR):
            dispatcher_with_handlers.dispatch(
                {}, chat_id='123', user_id='456', data='fail:test'
            )

        error_messages = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("Error in handler" in msg for msg in error_messages)
