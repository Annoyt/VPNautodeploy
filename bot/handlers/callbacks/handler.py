"""Sync callback handler wrapper for legacy Bot integration."""

import logging

from bot.handlers.base import BaseHandler
from bot.handlers.callbacks.dispatcher import CallbackDispatcher
from bot.utils.callback_router import extract_chat_id, extract_user_id, extract_callback_id

logger = logging.getLogger(__name__)


class CallbackHandler(BaseHandler):
    """Handler for callback queries using modular dispatcher."""

    def __init__(self, bot, db, config):
        super().__init__(bot, db, config)
        self.dispatcher = CallbackDispatcher(bot, db, config)

    def can_handle(self, update: dict) -> bool:
        return 'callback_query' in update

    def handle(self, update: dict) -> None:
        callback = update.get('callback_query', {})
        data = callback.get('data', '')
        chat_id = extract_chat_id(update)
        user_id = extract_user_id(update)

        if not chat_id or not user_id:
            logger.warning("Cannot handle callback: missing chat_id or user_id")
            return

        # Success paths used to log nothing at all, which made "the
        # button does nothing" reports undebuggable post-factum.
        logger.info(f"callback: data={data!r} chat={chat_id} user={user_id}")

        callback_id = extract_callback_id(update)
        if callback_id:
            self.bot.answer_callback_query(callback_id)

        handled = self.dispatcher.dispatch(update, chat_id, user_id, data)
        if not handled:
            logger.warning(f"Unknown callback data: {data}")
