"""Telegram Bot API client"""

import json
import logging
import time
from typing import Optional, Dict, Any

import requests

from bot.config import (
    TIMEOUT_API_DEFAULT,
    TIMEOUT_API_LONG_POLL,
    TIMEOUT_API_FILE_UPLOAD,
    MAX_RETRIES_API,
    RETRY_DELAY_BASE,
)

logger = logging.getLogger(__name__)


class TelegramClient:
    """Low-level Telegram Bot API client with retry logic."""
    
    API_URL = "https://api.telegram.org/bot{token}/{method}"
    MAX_RETRIES = MAX_RETRIES_API
    RETRY_DELAY_BASE = RETRY_DELAY_BASE
    
    def __init__(self, token: str):
        self.token = token
        self.session = requests.Session()
    
    def _sanitize_url(self, url: str) -> str:
        """Sanitize URL for logging by masking bot token.
        
        Args:
            url: URL that may contain bot token
            
        Returns:
            Sanitized URL with token masked
        """
        if not url:
            return url
        # Mask token in URL: bot<token>/method -> bot***TOKEN***/method
        import re
        return re.sub(r'(bot)[^/]+', r'\1***TOKEN***', url)
    
    # Default timeouts for different request types
    DEFAULT_TIMEOUT = TIMEOUT_API_DEFAULT
    LONG_POLL_TIMEOUT = TIMEOUT_API_LONG_POLL
    
    def _request(self, method: str, _read_timeout: int = None, **kwargs) -> Dict[str, Any]:
        """Make API request with retry logic.
        
        Args:
            method: API method name
            _read_timeout: HTTP read timeout in seconds (must exceed long-poll timeout)
            **kwargs: Request parameters
            
        Returns:
            API response as dict
            
        Raises:
            requests.RequestException: If all retries failed
        """
        url = self.API_URL.format(token=self.token, method=method)
        
        # Use appropriate timeout: explicit > method-specific > default
        if _read_timeout is None:
            if method == 'getUpdates':
                _read_timeout = self.LONG_POLL_TIMEOUT + 10
            else:
                _read_timeout = self.DEFAULT_TIMEOUT
        
        for attempt in range(self.MAX_RETRIES):
            response = None
            try:
                response = self.session.post(url, json=kwargs, timeout=_read_timeout)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as e:
                # Sanitize error message to prevent token leak in logs and exceptions
                error_msg = str(e)
                error_msg = self._sanitize_url(error_msg)
                # Pull the Telegram-returned description, which is the actually
                # useful bit ("can't parse entities", "chat not found", etc.).
                # Skipping this is why "400 Bad Request" alone tells us nothing.
                tg_description = ''
                if response is not None and response.status_code >= 400:
                    try:
                        body = response.json()
                        tg_description = body.get('description', '') if isinstance(body, dict) else ''
                    except Exception:
                        tg_description = (response.text or '')[:200]
                # Also log a short hint about the request payload so we can
                # tell which message blew up.
                payload_hint = ''
                try:
                    if 'chat_id' in kwargs:
                        payload_hint = f"chat_id={kwargs['chat_id']}"
                    if 'message_thread_id' in kwargs:
                        payload_hint += f" thread={kwargs.get('message_thread_id')}"
                    if 'text' in kwargs:
                        payload_hint += f" text={kwargs['text'][:60]!r}"
                    if 'reply_markup' in kwargs:
                        payload_hint += " has_keyboard"
                except Exception:
                    pass
                logger.warning(
                    f"API request failed (attempt {attempt + 1}/{self.MAX_RETRIES}): {method} "
                    f"{error_msg} | tg='{tg_description}' | {payload_hint}"
                )

                # Entity-parse failures are deterministic — retrying the same
                # payload can't succeed. Drop parse_mode and deliver the text
                # raw instead of losing the message entirely.
                if "can't parse entities" in tg_description and kwargs.get('parse_mode'):
                    logger.warning(f"{method}: dropping parse_mode after entity parse failure")
                    kwargs.pop('parse_mode')
                    continue

                if attempt < self.MAX_RETRIES - 1:
                    delay = self.RETRY_DELAY_BASE * (2 ** attempt)
                    logger.info(f"Retrying in {delay} seconds...")
                    time.sleep(delay)
                else:
                    logger.error(f"API request failed after {self.MAX_RETRIES} attempts")
                    # Re-raise with sanitized message to prevent token leak in tracebacks
                    sanitized_error = self._sanitize_url(str(e))
                    raise requests.RequestException(sanitized_error) from None
        
        return {}  # Should not reach here
    
    def get_updates(
        self,
        offset: Optional[int] = None,
        limit: int = 100,
        timeout: int = 30,
        allowed_updates: Optional[list] = None,
    ) -> tuple[list[dict], Optional[int]]:
        """Fetch updates from Telegram.

        Args:
            offset: Update ID offset
            limit: Maximum number of updates
            timeout: Long polling timeout
            allowed_updates: Optional list — when set, Telegram only
                pushes the listed update types. Needed for
                ``pre_checkout_query`` (off by default) to flow
                through; otherwise Stars payments never confirm.

        Returns:
            Tuple of (updates list, new offset or None)
        """
        params = {'limit': limit, 'timeout': timeout}
        if offset is not None:
            params['offset'] = offset
        if allowed_updates is not None:
            params['allowed_updates'] = json.dumps(allowed_updates)
        
        try:
            result = self._request('getUpdates', _read_timeout=timeout + 10, **params)  # Long poll timeout + buffer
            
            if result.get('ok'):
                updates = result.get('result', [])
                new_offset = None
                if updates:
                    new_offset = max(u['update_id'] for u in updates) + 1
                return updates, new_offset
            else:
                logger.error(f"API error: {result.get('description')}")
                return [], None
                
        except requests.RequestException as e:
            logger.error(f"Failed to get updates: {e}")
            return [], None
    
    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[dict] = None,
        reply_to_message_id: Optional[int] = None,
        message_thread_id: Optional[int] = None,
        **kwargs
    ) -> Optional[dict]:
        """Send a text message.
        
        Args:
            chat_id: Target chat ID
            text: Message text
            parse_mode: Parse mode (HTML, Markdown)
            reply_markup: Keyboard markup
            reply_to_message_id: Message to reply to
            message_thread_id: Forum topic ID
            **kwargs: Additional parameters
            
        Returns:
            Sent message dict or None
        """
        params = {'chat_id': chat_id, 'text': text, **kwargs}
        
        if parse_mode:
            params['parse_mode'] = parse_mode
        if reply_markup:
            params['reply_markup'] = json.dumps(reply_markup)
        if reply_to_message_id:
            params['reply_to_message_id'] = reply_to_message_id
        if message_thread_id:
            params['message_thread_id'] = message_thread_id
        
        try:
            result = self._request('sendMessage', **params)
            if result.get('ok'):
                return result.get('result')
            else:
                logger.error(f"Failed to send message: {result.get('description')}")
                return None
        except requests.RequestException as e:
            logger.error(f"Failed to send message: {e}")
            return None
    
    def close_forum_topic(
        self,
        chat_id: str,
        message_thread_id: int,
        **kwargs
    ) -> Optional[dict]:
        """Close a forum topic.
        
        Args:
            chat_id: Unique identifier for the target chat or username
            message_thread_id: Unique identifier for the target message thread
            **kwargs: Additional parameters
            
        Returns:
            True on success, None on failure
        """
        params = {'chat_id': chat_id, 'message_thread_id': message_thread_id, **kwargs}
        
        try:
            result = self._request('closeForumTopic', **params)
            if result.get('ok'):
                return result.get('result')
            else:
                logger.error(f"Failed to close forum topic: {result.get('description')}")
                return None
        except requests.RequestException as e:
            logger.error(f"Failed to close forum topic: {e}")
            return None
    
    def forward_message(
        self,
        chat_id: str,
        from_chat_id: str,
        message_id: int,
        **kwargs
    ) -> Optional[dict]:
        """Forward a message.
        
        Args:
            chat_id: Target chat ID
            from_chat_id: Source chat ID
            message_id: Message to forward
            **kwargs: Additional parameters
            
        Returns:
            Forwarded message dict or None
        """
        params = {
            'chat_id': chat_id,
            'from_chat_id': from_chat_id,
            'message_id': message_id,
            **kwargs
        }
        
        try:
            result = self._request('forwardMessage', **params)
            if result.get('ok'):
                return result.get('result')
            else:
                logger.error(f"Failed to forward message: {result.get('description')}")
                return None
        except requests.RequestException as e:
            logger.error(f"Failed to forward message: {e}")
            return None
    
    def download_file(self, file_id: str, dest_path: str) -> bool:
        """Download a Telegram file (photo/document/etc) by its ``file_id``
        into ``dest_path`` on the local filesystem.

        Two steps: ``getFile`` returns a relative ``file_path``; then we
        GET ``https://api.telegram.org/file/bot<token>/<file_path>`` and
        stream the bytes to disk. Used by AIHandler to ship admin-sent
        screenshots into ``/tmp/tg_media`` so Kimi can read them off
        the shared mount.

        Returns True on success, False on any failure (logged).
        """
        try:
            info = self._request('getFile', file_id=file_id)
            if not info.get('ok'):
                logger.warning(f"getFile failed for {file_id}: {info.get('description')}")
                return False
            file_path = (info.get('result') or {}).get('file_path')
            if not file_path:
                logger.warning(f"getFile returned no file_path for {file_id}")
                return False
        except requests.RequestException as e:
            logger.warning(f"getFile request failed for {file_id}: {e}")
            return False

        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        import os as _os
        try:
            _os.makedirs(_os.path.dirname(dest_path) or '.', exist_ok=True)
            with self.session.get(url, stream=True, timeout=60) as r:
                if r.status_code != 200:
                    logger.warning(f"download_file: HTTP {r.status_code} for {file_id}")
                    return False
                with open(dest_path, 'wb') as fh:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            fh.write(chunk)
            return True
        except (OSError, requests.RequestException) as e:
            logger.warning(f"download_file write failed for {file_id} -> {dest_path}: {e}")
            return False

    def send_invoice(
        self,
        chat_id: str,
        title: str,
        description: str,
        payload: str,
        currency: str,
        prices: list,
        provider_token: str = "",
        **kwargs,
    ) -> Optional[dict]:
        """Send a Telegram invoice — used for in-app Stars payments.

        ``currency="XTR"`` + empty ``provider_token`` means Telegram
        Stars (no external payment provider). Each ``prices`` entry
        is ``{"label": str, "amount": int}`` where amount is the
        integer number of Stars to charge for that line item.
        ``payload`` is an internal opaque string echoed back in
        ``pre_checkout_query`` / ``successful_payment`` so we can
        attribute the payment to the right user + plan.
        """
        params = {
            'chat_id': chat_id,
            'title': title,
            'description': description,
            'payload': payload,
            'provider_token': provider_token,
            'currency': currency,
            'prices': json.dumps(prices),
            **kwargs,
        }
        try:
            result = self._request('sendInvoice', **params)
            if result.get('ok'):
                return result.get('result')
            logger.warning(f"sendInvoice failed: {result.get('description')}")
            return None
        except requests.RequestException as e:
            logger.warning(f"sendInvoice request failed: {e}")
            return None

    def answer_pre_checkout_query(
        self,
        pre_checkout_query_id: str,
        ok: bool,
        error_message: Optional[str] = None,
    ) -> bool:
        """Confirm / reject a pending invoice. Telegram requires this
        within 10 seconds of receiving ``pre_checkout_query`` or the
        charge is auto-cancelled.
        """
        params = {'pre_checkout_query_id': pre_checkout_query_id, 'ok': ok}
        if error_message:
            params['error_message'] = error_message
        try:
            result = self._request('answerPreCheckoutQuery', **params)
            return bool(result.get('ok'))
        except requests.RequestException as e:
            logger.warning(f"answerPreCheckoutQuery failed: {e}")
            return False

    def copy_message(
        self,
        chat_id: str,
        from_chat_id: str,
        message_id: int,
        **kwargs
    ) -> Optional[dict]:
        """Copy a message into another chat / topic, without the "Forwarded
        from" header. Used to ship attachments from a closed support topic
        into the Solved-issues archive.
        """
        params = {
            'chat_id': chat_id,
            'from_chat_id': from_chat_id,
            'message_id': message_id,
            **kwargs,
        }
        try:
            result = self._request('copyMessage', **params)
            if result.get('ok'):
                return result.get('result')
            logger.warning(f"copyMessage failed: {result.get('description')}")
            return None
        except requests.RequestException as e:
            logger.warning(f"copyMessage request failed: {e}")
            return None

    def edit_forum_topic(
        self,
        chat_id: str,
        message_thread_id: int,
        name: Optional[str] = None,
        icon_custom_emoji_id: Optional[str] = None,
    ) -> bool:
        """Rename a forum topic (used to flag closed tickets in the title)."""
        params = {'chat_id': chat_id, 'message_thread_id': message_thread_id}
        if name is not None:
            params['name'] = name
        if icon_custom_emoji_id is not None:
            params['icon_custom_emoji_id'] = icon_custom_emoji_id
        try:
            result = self._request('editForumTopic', **params)
            return bool(result.get('ok'))
        except requests.RequestException as e:
            logger.warning(f"editForumTopic request failed: {e}")
            return False

    def send_chat_action(
        self,
        chat_id: str,
        action: str = "typing",
        message_thread_id: Optional[int] = None,
    ) -> bool:
        """Show a 'typing…' (or other) status indicator in the chat.

        Used by AIHandler so the admin sees something is happening while
        Kimi takes its 5–30s to produce a long answer.
        """
        kwargs = {"chat_id": chat_id, "action": action}
        if message_thread_id:
            kwargs["message_thread_id"] = message_thread_id
        try:
            result = self._request("sendChatAction", **kwargs)
            return bool(result.get("result", False))
        except Exception as e:
            logger.debug(f"sendChatAction failed (non-fatal): {e}")
            return False

    def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
        **kwargs
    ) -> bool:
        """Answer a callback query.
        
        Args:
            callback_query_id: Callback query ID
            text: Notification text
            show_alert: Show as alert
            **kwargs: Additional parameters
            
        Returns:
            True if successful
        """
        params = {
            'callback_query_id': callback_query_id,
            'show_alert': show_alert,
            **kwargs
        }
        if text:
            params['text'] = text
        
        try:
            result = self._request('answerCallbackQuery', **params)
            return result.get('ok', False)
        except requests.RequestException:
            return False
    
    def create_forum_topic(
        self,
        chat_id: str,
        name: str,
        **kwargs
    ) -> Optional[int]:
        """Create a forum topic.
        
        Args:
            chat_id: Forum group ID
            name: Topic name
            **kwargs: Additional parameters
            
        Returns:
            Topic ID or None
        """
        params = {'chat_id': chat_id, 'name': name, **kwargs}
        
        try:
            result = self._request('createForumTopic', **params)
            if result.get('ok'):
                return result.get('result', {}).get('message_thread_id')
            else:
                logger.error(f"Failed to create topic: {result.get('description')}")
                return None
        except requests.RequestException as e:
            logger.error(f"Failed to create topic: {e}")
            return None

    def send_document(
        self,
        chat_id: str,
        document: str,
        caption: Optional[str] = None,
        **kwargs
    ) -> Optional[dict]:
        """Send a document/file.
        
        Args:
            chat_id: Target chat ID
            document: Path to the file
            caption: Optional caption
            **kwargs: Additional parameters
            
        Returns:
            Sent message dict or None
        """
        url = self.API_URL.format(token=self.token, method='sendDocument')
        params = {'chat_id': chat_id, **kwargs}
        if caption:
            params['caption'] = caption
            
        try:
            with open(document, 'rb') as f:
                files = {'document': f}
                # Note: using raw post here because _request is optimized for JSON
                response = self.session.post(url, data=params, files=files, timeout=TIMEOUT_API_FILE_UPLOAD)
                response.raise_for_status()
                result = response.json()
                if result.get('ok'):
                    return result.get('result')
                else:
                    logger.error(f"Failed to send document: {result.get('description')}")
                    return None
        except Exception as e:
            logger.error(f"Failed to send document: {e}")
            return None

    def get_chat_member(self, chat_id: str, user_id: str) -> Optional[dict]:
        """Get information about a member of a chat.

        Args:
            chat_id: Target chat ID
            user_id: User ID

        Returns:
            ChatMember object or None
        """
        params = {'chat_id': chat_id, 'user_id': user_id}

        try:
            result = self._request('getChatMember', **params)
            if result.get('ok'):
                return result.get('result')
            else:
                logger.error(f"Failed to get chat member: {result.get('description')}")
                return None
        except Exception as e:
            logger.error(f"Failed to get chat member: {e}")
            return None

    def get_chat(self, chat_id: str) -> Optional[dict]:
        """Get information about a chat/user.

        Args:
            chat_id: Target chat ID (can be user ID for private chats)

        Returns:
            Chat object with fields like username, bio, etc. or None
        """
        params = {'chat_id': chat_id}

        try:
            result = self._request('getChat', **params)
            if result.get('ok'):
                return result.get('result')
            else:
                logger.warning(f"Failed to get chat info: {result.get('description')}")
                return None
        except Exception as e:
            logger.warning(f"Failed to get chat info: {e}")
            return None

    def get_user_profile_photos(self, user_id: str, offset: int = 0, limit: int = 1) -> Optional[dict]:
        """Get user's profile photos.

        Args:
            user_id: Target user ID
            offset: Offset for pagination (default 0)
            limit: Number of photos to fetch (default 1, max 100)

        Returns:
            Dict with 'total_count' and 'photos' array or None
        """
        params = {'user_id': user_id, 'offset': offset, 'limit': min(limit, 100)}

        try:
            result = self._request('getUserProfilePhotos', **params)
            if result.get('ok'):
                return result.get('result')
            else:
                logger.warning(f"Failed to get user photos: {result.get('description')}")
                return None
        except Exception as e:
            logger.warning(f"Failed to get user photos: {e}")
            return None

    def delete_message(
        self,
        chat_id: str,
        message_id: int,
        **kwargs
    ) -> bool:
        """Delete a message.
        
        Args:
            chat_id: Target chat ID
            message_id: Message to delete
            **kwargs: Additional parameters
            
        Returns:
            True if successful
        """
        params = {'chat_id': chat_id, 'message_id': message_id, **kwargs}
        
        try:
            result = self._request('deleteMessage', **params)
            return result.get('ok', False)
        except requests.RequestException as e:
            logger.error(f"Failed to delete message: {e}")
            return False

    def set_chat_menu_button(
        self,
        chat_id: Optional[str] = None,
        menu_button: Optional[dict] = None
    ) -> bool:
        """Set menu button for a chat or default for all chats.
        
        Args:
            chat_id: Target chat ID (None for default button)
            menu_button: Menu button config, e.g.
                {"type": "web_app", "text": "Dashboard", "web_app": {"url": "..."}}
                
        Returns:
            True if successful
        """
        params = {}
        if chat_id:
            params['chat_id'] = chat_id
        if menu_button:
            params['menu_button'] = json.dumps(menu_button)
        
        try:
            result = self._request('setChatMenuButton', **params)
            if result.get('ok'):
                logger.info(f"Menu button set for chat {chat_id}")
                return True
            else:
                logger.error(f"Failed to set menu button: {result.get('description')}")
                return False
        except requests.RequestException as e:
            logger.error(f"Failed to set menu button: {e}")
            return False

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[dict] = None
    ) -> Optional[dict]:
        """Edit message text.
        
        Args:
            chat_id: Target chat ID
            message_id: Message to edit
            text: New text
            parse_mode: Parse mode (HTML, Markdown)
            reply_markup: New keyboard markup (None to remove)
            
        Returns:
            Updated message dict or None
        """
        payload = {
            'chat_id': chat_id,
            'message_id': message_id,
            'text': text
        }
        if parse_mode:
            payload['parse_mode'] = parse_mode
        if reply_markup is not None:
            payload['reply_markup'] = json.dumps(reply_markup)
        
        try:
            result = self._request('editMessageText', **payload)
            if result.get('ok'):
                return result.get('result')
            else:
                logger.warning(f"Failed to edit message: {result}")
                return None
        except Exception as e:
            logger.error(f"Error editing message: {e}")
            return None
