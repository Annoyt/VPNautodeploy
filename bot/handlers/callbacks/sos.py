"""Buttons under the /sos answer — see bot/services/sos.py."""

import logging

from bot.handlers.callbacks.base import BaseCallbackHandler

logger = logging.getLogger(__name__)


class SosCallbackHandler(BaseCallbackHandler):
    """``sos:kit`` → the offline kit, ``sos:share`` → the share-VPN text.

    Both go to the PRESSER's private chat, never to ``chat_id`` as such:
    the kit holds that user's key, and the buttons only ever sit on a /sos
    answer in their own chat (where the two ids are the same anyway).
    """

    CALLBACK_PATTERN = 'sos:'

    def can_handle(self, callback_data: str) -> bool:
        return bool(callback_data) and callback_data.startswith(self.CALLBACK_PATTERN)

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        from bot.services.sos import (
            CALLBACK_KIT, CALLBACK_SHARE, SosService, user_lang,
        )
        data = kwargs.get('data', '')
        target = str(user_id or chat_id)
        user = self.db.get_user(target)
        svc = SosService(self.bot, self.db, self.config)
        if data == CALLBACK_KIT:
            svc.send_kit(target, user)
        elif data == CALLBACK_SHARE:
            svc.send_share(target, user_lang(user))
        else:
            logger.warning(f"sos callback: unknown data {data!r}")
