# §14. Telegram API errors are mute unless you pull `description`

`bot/core/telegram_client.py::_request` now logs the Telegram-returned `description` from the 4xx response body plus the method name + a tag from the payload (`chat_id`, `thread`, `text[:60]`, `has_keyboard`). Without that we lost an hour chasing why "400 Bad Request" was happening — turned out to be `BUTTON_TYPE_INVALID` in a forum group.
