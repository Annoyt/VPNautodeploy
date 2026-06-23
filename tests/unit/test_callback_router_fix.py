"""Tests for callback_router.py _run_async fix."""

import pytest
import asyncio
from unittest.mock import MagicMock, Mock, patch

from bot.utils.callback_router import CallbackRouter

pytestmark = pytest.mark.filterwarnings(
    "ignore:coroutine .* was never awaited:RuntimeWarning"
)


class TestCallbackRouterRunAsync:
    """Test _run_async error handling in callback_router."""
    
    @pytest.fixture
    def router(self):
        """Create CallbackRouter instance."""
        return CallbackRouter()
    
    def test_run_async_no_loop_exception_caught(self, router):
        """Test _run_async catches and logs exceptions when no event loop."""
        async def error_coro():
            raise ValueError("Test error")
        
        with patch('bot.utils.callback_router.logger') as mock_logger:
            router._run_async(error_coro())
            
            # Verify exception was logged
            mock_logger.exception.assert_called()
            assert "async callback router" in mock_logger.exception.call_args[0][0].lower()
    
    def test_run_async_with_loop_schedules_task_with_callback(self, router):
        """Test _run_async schedules task with done callback."""
        async def success_coro():
            return "success"
        
        async def async_test():
            loop = asyncio.get_running_loop()
            
            with patch.object(loop, 'create_task') as mock_create_task:
                mock_task = MagicMock()
                mock_create_task.return_value = mock_task
                
                router._run_async(success_coro())
                
                # Verify task was created
                mock_create_task.assert_called_once()
                # Verify done callback was added
                mock_task.add_done_callback.assert_called_once()
                
                # Close the wrapped coroutine to prevent GC RuntimeWarning
                mock_create_task.call_args[0][0].close()
        
        asyncio.run(async_test())
    
    def test_run_async_task_done_callback_logs_cancelled(self, router):
        """Test that cancelled tasks are logged."""
        async def long_coro():
            await asyncio.sleep(10)
        
        async def async_test():
            with patch('bot.utils.callback_router.logger') as mock_logger:
                router._run_async(long_coro())
                
                # Get the created task
                tasks = [t for t in asyncio.all_tasks() if t != asyncio.current_task()]
                if tasks:
                    task = tasks[0]
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    
                    # Give a moment for callback to execute
                    await asyncio.sleep(0.01)
        
        asyncio.run(async_test())


class TestCallbackRouterIntegration:
    """Integration tests for callback_router with _run_async."""
    
    def test_route_with_async_handler(self):
        """Test that async handlers are executed via _run_async."""
        router = CallbackRouter()
        
        async_handler_calls = []
        
        @router.callback('test_async')
        async def async_handler(update, data, **kwargs):
            async_handler_calls.append((update, data))
        
        with patch.object(router, '_run_async') as mock_run_async:
            router.route('test_async', {'test': 'update'})
            
            # Verify _run_async was called
            mock_run_async.assert_called_once()
            
            # Close the passed coroutine to prevent GC RuntimeWarning
            mock_run_async.call_args[0][0].close()


class TestBaseCallbackHandlerRunAsync:
    """Test BaseCallbackHandler._run_async fix."""

    def _make_handler(self):
        from bot.handlers.callbacks.base import BaseCallbackHandler

        class ConcreteHandler(BaseCallbackHandler):
            def can_handle(self, callback_data: str) -> bool:
                return True
            def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
                pass

        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        return ConcreteHandler(mock_bot, mock_db, mock_config)

    def test_run_async_schedules_task_in_running_loop(self):
        """Test that _run_async schedules a task instead of crashing."""
        import asyncio

        async def async_test():
            handler = self._make_handler()

            async def success_coro():
                return "done"

            loop = asyncio.get_running_loop()
            with patch.object(loop, 'create_task') as mock_create_task:
                mock_task = MagicMock()
                mock_create_task.return_value = mock_task

                handler._run_async(success_coro())

                mock_create_task.assert_called_once()
                mock_task.add_done_callback.assert_called_once()
                # Close the coroutine wrapper to prevent RuntimeWarning
                mock_create_task.call_args[0][0].close()

        asyncio.run(async_test())

    def test_run_async_logs_exceptions_in_running_loop(self):
        """Test that exceptions inside running loop are logged."""
        import asyncio

        async def async_test():
            handler = self._make_handler()

            async def error_coro():
                raise ValueError("Test error")

            with patch('bot.handlers.callbacks.base.logger') as mock_logger:
                handler._run_async(error_coro())
                # Allow the task to complete
                await asyncio.sleep(0.05)

                mock_logger.exception.assert_called()
                log_message = mock_logger.exception.call_args[0][0]
                assert "async callback handler" in log_message.lower()

        asyncio.run(async_test())
