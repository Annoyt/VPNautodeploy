"""Callback handler for the /ai_model free-model switcher (admin only)."""

import logging

from bot.handlers.callbacks.base import BaseCallbackHandler
from bot.services.agent_models import (
    FREE_MODELS,
    CALLBACK_PREFIX,
    set_selected_model,
    label_for,
    build_model_keyboard,
)

logger = logging.getLogger(__name__)


class AiModelSelectHandler(BaseCallbackHandler):
    """Handles ``aimodel:<index>`` — sets the free model the /ai agent uses.

    Selection is persisted in bot.db and read per-request by
    ``HermesAgentClient``, so it takes effect on the next /ai turn with no
    server restart.
    """

    def can_handle(self, callback_data: str) -> bool:
        return bool(callback_data) and callback_data.startswith(CALLBACK_PREFIX)

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        callback = update.get("callback_query", {}) or {}
        data = kwargs.get("data") or callback.get("data", "")
        cb_id = callback.get("id")

        # Admin only — the switcher controls the ops agent.
        if str(user_id) != str(self.config.SUPER_ADMIN_ID):
            if cb_id:
                self.bot.answer_callback_query(cb_id, text="🚫 Только админ")
            return

        try:
            idx = int(data[len(CALLBACK_PREFIX):])
            model_id = FREE_MODELS[idx][0]
        except (ValueError, IndexError):
            if cb_id:
                self.bot.answer_callback_query(cb_id, text="⚠️ Неизвестная модель")
            return

        set_selected_model(self.config.DB_PATH, model_id)

        message = callback.get("message", {}) or {}
        message_id = message.get("message_id")
        if message_id:
            try:
                self.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=(
                        "🤖 <b>Модель для /ai</b> — все бесплатные, с tool-calling.\n"
                        f"Сейчас: <b>{label_for(model_id)}</b>\n<code>{model_id}</code>"
                    ),
                    parse_mode="HTML",
                    reply_markup=build_model_keyboard(model_id),
                )
            except Exception as e:
                logger.warning(f"ai_model keyboard edit failed: {e}")

        if cb_id:
            self.bot.answer_callback_query(cb_id, text=f"✅ {label_for(model_id)}")
