"""Long polling service for Telegram bot"""

import logging
import threading
from typing import Callable, Optional

from bot.core.telegram_client import TelegramClient

logger = logging.getLogger(__name__)


class PollingService:
    """Manages long polling loop with graceful shutdown."""
    
    def __init__(self, client: TelegramClient, processor: Callable[[dict], None]):
        """
        Args:
            client: Telegram API client
            processor: Function to process updates
        """
        self.client = client
        self.processor = processor
        self.running = False
        self.offset: Optional[int] = None
        self._stop_event = threading.Event()
    
    def start(self) -> None:
        """Start the polling loop."""
        logger.info("Starting polling service...")
        self.running = True
        self._stop_event.clear()
        
        # Default + pre_checkout_query (off by default in Bot API).
        # Without this Stars / regular invoices silently hang at
        # "Pay" and never confirm.
        ALLOWED = [
            'message',
            'callback_query',
            'pre_checkout_query',
            'edited_message',
        ]

        while self.running:
            try:
                updates, new_offset = self.client.get_updates(
                    offset=self.offset,
                    limit=100,
                    timeout=30,
                    allowed_updates=ALLOWED,
                )
                
                if new_offset is not None:
                    self.offset = new_offset
                
                for update in updates:
                    try:
                        self.processor(update)
                    except Exception as e:
                        logger.exception(f"Failed to process update: {e}")
                
            except Exception as e:
                logger.exception(f"Error in polling loop: {e}")
                # Interruptible sleep — stop() will wake this immediately
                self._stop_event.wait(5)
        
        logger.info("Polling service stopped")
    
    def stop(self) -> None:
        """Stop the polling loop."""
        self.running = False
        self._stop_event.set()
        logger.info("Stop requested")
