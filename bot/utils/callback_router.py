"""Callback router for clean handler registration.

Replaces long if/elif chains with decorator-based routing.

Usage:
    router = CallbackRouter()
    
    @router.callback('request_demo')
    def handle_demo(update, chat_id, **kwargs):
        ...
    
    @router.callback_pattern(r'approve:(\\d+)')
    def handle_approve(update, chat_id, match, **kwargs):
        target_id = match.group(1)
        ...
"""

import re
import logging
from typing import Callable, Optional, Pattern

logger = logging.getLogger(__name__)


class CallbackRouter:
    """Router for callback query handlers.
    
    Supports:
    - Exact match routing: @router.callback('exact_value')
    - Pattern routing: @router.callback_pattern(r'prefix:(\\d+)')
    - Async and sync handlers
    """
    
    def __init__(self):
        self._handlers: dict[str, Callable] = {}  # exact matches
        self._pattern_handlers: list[tuple[Pattern, Callable]] = []  # regex patterns
    
    def callback(self, data: str) -> Callable:
        """Decorator to register exact match handler.
        
        Args:
            data: Exact callback_data value to match
            
        Returns:
            Decorator function
        """
        def decorator(func: Callable) -> Callable:
            self._handlers[data] = func
            return func
        return decorator
    
    def callback_pattern(self, pattern: str) -> Callable:
        """Decorator to register pattern-based handler.
        
        Args:
            pattern: Regex pattern to match against callback_data
            
        Returns:
            Decorator function
        """
        compiled = re.compile(pattern)
        
        def decorator(func: Callable) -> Callable:
            self._pattern_handlers.append((compiled, func))
            return func
        return decorator
    
    def route(self, data: str, update: dict, **kwargs) -> bool:
        """Route callback data to appropriate handler.
        
        Args:
            data: Callback data string
            update: Telegram update object
            **kwargs: Additional arguments passed to handler
            
        Returns:
            True if handler found and executed, False otherwise
        """
        # Try exact match first
        if data in self._handlers:
            handler = self._handlers[data]
            self._execute_handler(handler, update, data, None, **kwargs)
            return True
        
        # Try pattern matching
        for pattern, handler in self._pattern_handlers:
            match = pattern.match(data)
            if match:
                self._execute_handler(handler, update, data, match, **kwargs)
                return True
        
        return False
    
    def _execute_handler(self, handler: Callable, update: dict, 
                         data: str, match: Optional[re.Match], **kwargs):
        """Execute handler with appropriate arguments.
        
        Args:
            handler: Handler function
            update: Telegram update object
            data: Full callback data
            match: Regex match object (None for exact matches)
            **kwargs: Additional arguments
        """
        import asyncio
        
        # Build arguments dict
        args = {'update': update, 'data': data, **kwargs}
        if match:
            args['match'] = match
        
        # Call handler
        result = handler(**args)
        # Handle async functions safely
        if asyncio.iscoroutine(result):
            self._run_async(result)
    
    def _run_async(self, coro):
        """Run async coroutine safely in sync context.
        
        Avoids creating nested event loops. If called from async context,
        schedules the coroutine as a task with error handling.
        """
        import asyncio
        
        async def _wrapped_coro():
            """Wrapper that catches and logs exceptions."""
            try:
                await coro
            except Exception as e:
                logger.exception(f"Error in async callback router handler: {e}")
        
        def _on_task_done(task):
            """Log task completion status."""
            if task.cancelled():
                logger.warning("Async callback router task was cancelled")
            elif task.exception():
                logger.exception(f"Async callback router task failed: {task.exception()}")
        
        try:
            loop = asyncio.get_running_loop()
            # Already in async context - schedule as task
            logger.debug("Scheduling async handler in existing event loop")
            task = loop.create_task(_wrapped_coro())
            task.add_done_callback(_on_task_done)
        except RuntimeError:
            # No event loop running - safe to use asyncio.run()
            try:
                asyncio.run(_wrapped_coro())
            except Exception as e:
                logger.exception(f"Error running async callback router: {e}")
    
    def get_registered_handlers(self) -> list[str]:
        """Get list of registered handler patterns for debugging.
        
        Returns:
            List of registered patterns
        """
        handlers = list(self._handlers.keys())
        patterns = [p.pattern for p, _ in self._pattern_handlers]
        return handlers + patterns


def extract_chat_id(update: dict) -> Optional[str]:
    """Extract chat_id from callback query update.
    
    Args:
        update: Telegram update object
        
    Returns:
        Chat ID string or None
    """
    callback = update.get('callback_query', {})
    message = callback.get('message', {})
    chat = message.get('chat', {})
    return str(chat.get('id')) if chat.get('id') else None


def extract_user_id(update: dict) -> Optional[str]:
    """Extract user_id from callback query update.
    
    Args:
        update: Telegram update object
        
    Returns:
        User ID string or None
    """
    callback = update.get('callback_query', {})
    from_user = callback.get('from', {})
    return str(from_user.get('id')) if from_user.get('id') else None


def extract_callback_id(update: dict) -> Optional[str]:
    """Extract callback query ID from update.
    
    Args:
        update: Telegram update object
        
    Returns:
        Callback ID string or None
    """
    callback = update.get('callback_query', {})
    return callback.get('id')
