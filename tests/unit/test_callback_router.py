"""Unit tests for callback router.

Tests the router pattern implementation.
"""

import pytest
from unittest.mock import Mock, MagicMock

from bot.utils.callback_router import CallbackRouter, extract_chat_id, extract_user_id, extract_callback_id


class TestCallbackRouter:
    """Test CallbackRouter class."""
    
    @pytest.fixture
    def router(self):
        """Create fresh router instance."""
        return CallbackRouter()
    
    def test_router_initialization(self, router):
        """Test router initializes with empty handlers."""
        assert len(router._handlers) == 0
        assert len(router._pattern_handlers) == 0
    
    def test_callback_decorator_registers_handler(self, router):
        """Test @router.callback() decorator registers handler."""
        @router.callback('test_action')
        def handle_test(update, **kwargs):
            pass
        
        assert 'test_action' in router._handlers
        assert router._handlers['test_action'] == handle_test
    
    def test_callback_pattern_decorator_registers_handler(self, router):
        """Test @router.callback_pattern() decorator registers handler."""
        @router.callback_pattern(r'test:(\d+)')
        def handle_test_pattern(update, match, **kwargs):
            pass
        
        assert len(router._pattern_handlers) == 1
        pattern, handler = router._pattern_handlers[0]
        assert handler == handle_test_pattern
    
    def test_route_exact_match(self, router):
        """Test routing exact match callbacks."""
        handler_mock = Mock()
        
        @router.callback('exact_match')
        def handle_exact(update, **kwargs):
            handler_mock(**kwargs)
        
        result = router.route('exact_match', {}, chat_id='123')
        
        assert result is True
        handler_mock.assert_called_once()
    
    def test_route_pattern_match(self, router):
        """Test routing pattern match callbacks."""
        handler_mock = Mock()
        
        @router.callback_pattern(r'user:(\d+)')
        def handle_user(update, match, **kwargs):
            handler_mock(user_id=match.group(1), **kwargs)
        
        result = router.route('user:456', {}, chat_id='123')
        
        assert result is True
        handler_mock.assert_called_once_with(user_id='456', chat_id='123', data='user:456')
    
    def test_route_no_match(self, router):
        """Test routing unknown callbacks."""
        result = router.route('unknown', {}, chat_id='123')
        assert result is False
    
    def test_route_priority_exact_over_pattern(self, router):
        """Test exact match has priority over pattern."""
        exact_handler = Mock()
        pattern_handler = Mock()
        
        @router.callback('action:123')
        def handle_exact(update, **kwargs):
            exact_handler(**kwargs)
        
        @router.callback_pattern(r'action:(\d+)')
        def handle_pattern(update, match, **kwargs):
            pattern_handler(**kwargs)
        
        router.route('action:123', {})
        
        exact_handler.assert_called_once()
        pattern_handler.assert_not_called()
    
    def test_route_passes_kwargs(self, router):
        """Test that route passes kwargs to handler."""
        handler_mock = Mock()
        
        @router.callback('test')
        def handle_test(update, chat_id, user_id, **kwargs):
            handler_mock(update=update, chat_id=chat_id, user_id=user_id, **kwargs)
        
        router.route('test', {'update': True}, chat_id='123', user_id='456', extra='value')
        
        handler_mock.assert_called_once_with(
            update={'update': True},
            chat_id='123',
            user_id='456',
            data='test',
            extra='value'
        )
    
    def test_route_async_handler(self, router):
        """Test routing to async handler."""
        import asyncio
        
        async_handler_mock = Mock()
        
        @router.callback('async_test')
        async def handle_async(update, **kwargs):
            async_handler_mock(**kwargs)
        
        result = router.route('async_test', {}, chat_id='123')
        
        assert result is True
        # Async handler should be called (via asyncio.run)
        # Note: In real async context, we'd need to await
    
    def test_route_handler_exception(self, router):
        """Test that handler exceptions are propagated."""
        @router.callback('error')
        def handle_error(update, **kwargs):
            raise ValueError("Test error")
        
        # Exception is propagated to caller
        with pytest.raises(ValueError, match="Test error"):
            router.route('error', {}, chat_id='123')
    
    def test_multiple_exact_handlers(self, router):
        """Test registering multiple exact handlers."""
        @router.callback('action1')
        def handle1(update, **kwargs): pass
        
        @router.callback('action2')
        def handle2(update, **kwargs): pass
        
        @router.callback('action3')
        def handle3(update, **kwargs): pass
        
        assert len(router._handlers) == 3
        assert 'action1' in router._handlers
        assert 'action2' in router._handlers
        assert 'action3' in router._handlers
    
    def test_multiple_pattern_handlers(self, router):
        """Test registering multiple pattern handlers."""
        @router.callback_pattern(r'pattern1:(\d+)')
        def handle1(update, match, **kwargs): pass
        
        @router.callback_pattern(r'pattern2:(\w+)')
        def handle2(update, match, **kwargs): pass
        
        assert len(router._pattern_handlers) == 2
    
    def test_pattern_matching_order(self, router):
        """Test pattern handlers are checked in registration order."""
        handler1_mock = Mock()
        handler2_mock = Mock()
        
        @router.callback_pattern(r'action:(\d+)')
        def handle1(update, match, **kwargs):
            handler1_mock(**kwargs)
        
        @router.callback_pattern(r'action:(\w+)')
        def handle2(update, match, **kwargs):
            handler2_mock(**kwargs)
        
        # This matches both patterns, first should win
        router.route('action:123', {})
        
        handler1_mock.assert_called_once()
        handler2_mock.assert_not_called()
    
    def test_get_registered_handlers(self, router):
        """Test getting list of registered handlers."""
        @router.callback('exact1')
        def handle1(update, **kwargs): pass
        
        @router.callback_pattern(r'pattern1:(\d+)')
        def handle2(update, match, **kwargs): pass
        
        handlers = router.get_registered_handlers()
        
        assert 'exact1' in handlers
        assert r'pattern1:(\d+)' in handlers
        assert len(handlers) == 2


class TestExtractFunctions:
    """Test utility extraction functions."""
    
    def test_extract_chat_id(self):
        """Test extracting chat_id from update."""
        update = {
            'callback_query': {
                'message': {
                    'chat': {'id': 123456}
                }
            }
        }
        
        chat_id = extract_chat_id(update)
        assert chat_id == '123456'
    
    def test_extract_chat_id_missing(self):
        """Test extracting chat_id when missing."""
        update = {'callback_query': {'message': {}}}
        chat_id = extract_chat_id(update)
        assert chat_id is None
    
    def test_extract_user_id(self):
        """Test extracting user_id from update."""
        update = {
            'callback_query': {
                'from': {'id': 789012}
            }
        }
        
        user_id = extract_user_id(update)
        assert user_id == '789012'
    
    def test_extract_user_id_missing(self):
        """Test extracting user_id when missing."""
        update = {'callback_query': {}}
        user_id = extract_user_id(update)
        assert user_id is None
    
    def test_extract_callback_id(self):
        """Test extracting callback_id from update."""
        update = {
            'callback_query': {
                'id': 'callback_123'
            }
        }
        
        callback_id = extract_callback_id(update)
        assert callback_id == 'callback_123'
    
    def test_extract_callback_id_missing(self):
        """Test extracting callback_id when missing."""
        update = {'callback_query': {}}
        callback_id = extract_callback_id(update)
        assert callback_id is None


class TestRouterIntegration:
    """Test router integration scenarios."""
    
    def test_complex_routing_scenario(self):
        """Test complex routing with multiple handlers."""
        router = CallbackRouter()
        
        results = []
        
        @router.callback('start')
        def handle_start(update, **kwargs):
            results.append('start')
        
        @router.callback_pattern(r'user:(\d+)')
        def handle_user(update, match, **kwargs):
            results.append(f"user:{match.group(1)}")
        
        @router.callback_pattern(r'admin:(\w+)')
        def handle_admin(update, match, **kwargs):
            results.append(f"admin:{match.group(1)}")
        
        @router.callback('help')
        def handle_help(update, **kwargs):
            results.append('help')
        
        # Route multiple callbacks
        router.route('start', {})
        router.route('user:123', {})
        router.route('admin:delete', {})
        router.route('help', {})
        router.route('unknown', {})  # Should not match
        
        assert results == ['start', 'user:123', 'admin:delete', 'help']
    
    def test_handler_modifying_shared_state(self):
        """Test handlers can modify shared state."""
        router = CallbackRouter()
        state = {'count': 0, 'last_action': None}
        
        @router.callback('increment')
        def handle_increment(update, **kwargs):
            state['count'] += 1
            state['last_action'] = 'increment'
        
        @router.callback('decrement')
        def handle_decrement(update, **kwargs):
            state['count'] -= 1
            state['last_action'] = 'decrement'
        
        router.route('increment', {})
        router.route('increment', {})
        router.route('decrement', {})
        
        assert state['count'] == 1
        assert state['last_action'] == 'decrement'
    
    def test_handler_with_complex_pattern(self):
        """Test handler with complex regex pattern."""
        router = CallbackRouter()
        results = []
        
        @router.callback_pattern(r'set_lang:(\w+):user:(\d+)')
        def handle_complex(update, match, **kwargs):
            lang = match.group(1)
            user_id = match.group(2)
            results.append({'lang': lang, 'user_id': user_id})
        
        router.route('set_lang:en:user:123', {})
        router.route('set_lang:ru:user:456', {})
        
        assert len(results) == 2
        assert results[0] == {'lang': 'en', 'user_id': '123'}
        assert results[1] == {'lang': 'ru', 'user_id': '456'}


@pytest.fixture
def router():
    """Create fresh router instance (module-level)."""
    return CallbackRouter()


class TestRouterEdgeCases:
    """Test router edge cases."""
    
    def test_empty_callback_data(self, router):
        """Test routing empty callback data."""
        result = router.route('', {}, chat_id='123')
        assert result is False
    
    def test_none_callback_data(self, router):
        """Test routing None callback data."""
        result = router.route(None, {}, chat_id='123')
        assert result is False
    
    def test_handler_returning_value(self, router):
        """Test that handler return value doesn't affect routing."""
        @router.callback('test')
        def handle_test(update, **kwargs):
            return "some value"  # Should be ignored
        
        result = router.route('test', {})
        assert result is True
    
    def test_pattern_with_special_characters(self, router):
        """Test pattern with special regex characters."""
        handler_mock = Mock()
        
        @router.callback_pattern(r'action\.(\d+)\.confirm')
        def handle_dots(update, match, **kwargs):
            handler_mock(action_id=match.group(1))
        
        router.route('action.123.confirm', {})
        
        handler_mock.assert_called_once_with(action_id='123')
    
    def test_unicode_callback_data(self, router):
        """Test routing unicode callback data."""
        handler_mock = Mock()
        
        @router.callback('тест')
        def handle_unicode(update, **kwargs):
            handler_mock(**kwargs)
        
        result = router.route('тест', {}, chat_id='123')
        
        assert result is True
        handler_mock.assert_called_once()
