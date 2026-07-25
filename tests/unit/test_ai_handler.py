"""Comprehensive unit tests for bot/handlers/ai_handler.py.

Tests cover:
1. can_handle() - AI command routing (/ai, /ai_reset, etc.)
2. handle() - main entry point routing
3. _handle_prompt() - core AI interaction
4. _handle_reset() - session clearing
5. _handle_status() - status display
6. Photo handling (_download_photo, _send_file)
7. Error cases (API down, timeout, etc.)
"""

from unittest.mock import Mock, patch

import pytest

from bot.handlers.ai_handler import (
    AIHandler,
    _extract_files,
    _start_typing_keepalive,
    TELEGRAM_TEXT_LIMIT,
)
from bot.services.agent_client import (
    AgentUnavailable,
    AgentError,
    AgentClient,
)



def _wait_turn(handler):
    """Join the async agent-turn worker so asserts are deterministic."""
    t = getattr(handler, "_last_turn_thread", None)
    if t is not None:
        t.join(timeout=5)

@pytest.fixture
def mock_config():
    """Create mock config with AI settings."""
    config = Mock()
    config.SUPER_ADMIN_ID = '1652899'
    config.OPENCODE_URL = 'http://localhost:4096'
    config.OPENCODE_USERNAME = 'opencode'
    config.OPENCODE_SERVER_PASSWORD = 'test_pw'
    config.OPENCODE_DEFAULT_MODEL = ''
    config.OPENCODE_AGENT_DEFAULT = ''
    config.OPENCODE_AGENT_PLAN = ''
    config.OPENCODE_AGENT_YOLO = ''
    config.AI_DEFAULT_MODE = 'fast'
    config.AGENT_NODE_TYPE = 'control'
    config.ENTRY_NODE_SSHFS_MOUNT = '/mnt/entry_node'
    config.TOPIC_AI = 42
    config.DB_PATH = '/tmp/test.db'

    def mock_is_admin(user_id):
        return str(user_id) == config.SUPER_ADMIN_ID

    config.is_admin = mock_is_admin
    return config


@pytest.fixture
def mock_bot():
    """Create mock bot with Telegram client."""
    bot = Mock()
    bot.client = Mock()
    bot.client.send_chat_action = Mock(return_value=True)
    bot.client.download_file = Mock(return_value=True)
    bot.client.send_document = Mock(return_value={'message_id': 123})
    bot.send_message = Mock(return_value={'message_id': 456})
    return bot


@pytest.fixture
def mock_db():
    """Create mock database."""
    db = Mock()
    conn = Mock()
    cursor = Mock()
    cursor.fetchone = Mock(return_value=[5])
    cursor.execute = Mock(return_value=cursor)
    conn.cursor = Mock(return_value=cursor)
    conn.__enter__ = Mock(return_value=conn)
    conn.__exit__ = Mock(return_value=False)
    db._connect = Mock(return_value=conn)
    return db


@pytest.fixture
def mock_agent_client():
    """Create mock AgentClient."""
    client = Mock(spec=AgentClient)
    client.ask = Mock(return_value=("Test response", 1500))
    client.ping = Mock(return_value={'status': 'ok', 'version': '1.0.0'})
    client.forget_session = Mock(return_value=True)
    client.get_session = Mock(return_value='ses_abc123')
    return client


@pytest.fixture
def ai_handler(mock_bot, mock_db, mock_config, mock_agent_client):
    """Create AIHandler with mocks."""
    handler = AIHandler(mock_bot, mock_db, mock_config)
    handler._client = mock_agent_client
    # Spy on _reply while still delegating to the real impl (which calls
    # bot.send_message), so tests can assert on either _reply.call_args or
    # bot.send_message.call_args.
    handler._reply = Mock(wraps=handler._reply)
    return handler


class TestExtractFiles:
    """Test _extract_files helper function."""

    def test_extract_no_files(self):
        """Test text without file markers."""
        text = "This is plain text with no files."
        cleaned, files = _extract_files(text)
        assert cleaned == text
        assert files == []

    def test_extract_single_file_no_caption(self):
        """Test single file marker without caption."""
        text = "Look at this: [[SEND_FILE: /path/to/file.png]]"
        cleaned, files = _extract_files(text)
        assert cleaned == "Look at this:"
        assert files == [("/path/to/file.png", None)]

    def test_extract_single_file_with_caption(self):
        """Test single file marker with caption."""
        text = "Result: [[SEND_FILE: /path/to/result.png | Here is the result]]"
        cleaned, files = _extract_files(text)
        assert cleaned == "Result:"
        assert files == [("/path/to/result.png", "Here is the result")]

    def test_extract_multiple_files(self):
        """Test multiple file markers."""
        text = """
        First: [[SEND_FILE: /tmp/a.png | First image]]
        Some text between.
        Second: [[SEND_FILE: /tmp/b.json]]
        """
        cleaned, files = _extract_files(text)
        assert "First:" in cleaned
        assert "Second:" in cleaned
        assert "[[SEND_FILE:" not in cleaned
        assert len(files) == 2
        assert files[0] == ("/tmp/a.png", "First image")
        assert files[1] == ("/tmp/b.json", None)

    def test_extract_files_with_spaces(self):
        """Test file marker with various whitespace."""
        text = "[[SEND_FILE:  /path/with/spaces.png  |  caption  ]]"
        cleaned, files = _extract_files(text)
        assert files == [("/path/with/spaces.png", "caption")]

    def test_extract_empty_file_marker(self):
        """Test file marker with empty path (should be ignored)."""
        text = "[[SEND_FILE: | caption]]"
        cleaned, files = _extract_files(text)
        assert cleaned == ""
        assert files == []

    def test_extract_files_preserves_other_text(self):
        """Test that non-marker text is preserved."""
        text = "Hello [[SEND_FILE: /a.png]] world [[SEND_FILE: /b.png | end]]!"
        cleaned, files = _extract_files(text)
        assert "Hello" in cleaned
        assert "world" in cleaned
        assert "!" in cleaned
        assert "[[SEND_FILE:" not in cleaned
        assert len(files) == 2


class TestStartTypingKeepalive:
    """Test _start_typing_keepalive helper function."""

    @patch('threading.Thread')
    @patch('threading.Event')
    def test_typing_keepalive_starts_thread(self, mock_event, mock_thread, mock_bot):
        """Test that typing keepalive starts a daemon thread."""
        mock_stop = Mock()
        mock_event.return_value = mock_stop
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        stop = _start_typing_keepalive(mock_bot, 'chat_123', 42)

        mock_thread.assert_called_once()
        mock_thread_instance.start.assert_called_once()
        assert mock_thread.call_args[1]['daemon'] is True
        assert stop == mock_stop

    @patch('threading.Thread')
    @patch('threading.Event')
    def test_typing_keepalive_sends_action(self, mock_event, mock_thread, mock_bot):
        """Test that typing keepalive sends chat action."""
        mock_stop = Mock()
        mock_stop.is_set = Mock(side_effect=[False, True])
        mock_stop.wait = Mock(return_value=None)
        mock_event.return_value = mock_stop
        mock_thread_instance = Mock()
        mock_thread.return_value = mock_thread_instance

        _start_typing_keepalive(mock_bot, 'chat_123', 42)

        # Get the thread target and call it once
        target = mock_thread.call_args[1]['target']
        target()

        mock_bot.client.send_chat_action.assert_called_with(
            chat_id='chat_123',
            action='typing',
            message_thread_id=42,
        )


class TestAIHandlerCanHandle:
    """Test can_handle() - AI command routing."""

    def test_can_handle_ai_command(self, ai_handler):
        """Test /ai command is handled."""
        update = {
            'message': {
                'text': '/ai test prompt',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_ai_reset_command(self, ai_handler):
        """Test /ai_reset command is handled."""
        update = {
            'message': {
                'text': '/ai_reset',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_ai_status_command(self, ai_handler):
        """Test /ai_status command is handled."""
        update = {
            'message': {
                'text': '/ai_status',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_ai_fast_command(self, ai_handler):
        """Test /ai_fast command is handled."""
        update = {
            'message': {
                'text': '/ai_fast quick question',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_ai_plan_command(self, ai_handler):
        """Test /ai_plan command is handled."""
        update = {
            'message': {
                'text': '/ai_plan detailed plan',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_ai_skill_command(self, ai_handler):
        """Test /ai_skill command is handled."""
        update = {
            'message': {
                'text': '/ai_skill vpn-ops check status',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_ai_yolo_command(self, ai_handler):
        """Test /ai_yolo command is handled."""
        update = {
            'message': {
                'text': '/ai_yolo risky command',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_with_bot_mention(self, ai_handler):
        """Test command with @bot mention."""
        update = {
            'message': {
                'text': '/ai@MyBot test',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_free_text_in_ai_topic(self, ai_handler):
        """Test free text in TOPIC_AI is handled."""
        update = {
            'message': {
                'text': 'help me with xray config',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
                'message_thread_id': 42,
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_free_text_wrong_topic(self, ai_handler):
        """Test free text in wrong topic is not handled."""
        update = {
            'message': {
                'text': 'help me',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
                'message_thread_id': 99,
            }
        }
        assert ai_handler.can_handle(update) is False

    def test_can_handle_free_text_no_topic(self, ai_handler):
        """Test free text without topic_id is not handled."""
        update = {
            'message': {
                'text': 'help me',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is False

    def test_can_handle_non_admin(self, ai_handler):
        """Test non-admin messages are not handled."""
        update = {
            'message': {
                'text': '/ai test',
                'from': {'id': '999999'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is False

    def test_can_handle_unknown_command_in_topic(self, ai_handler):
        """Test unknown slash command in AI topic is not handled."""
        update = {
            'message': {
                'text': '/unknown command',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
                'message_thread_id': 42,
            }
        }
        assert ai_handler.can_handle(update) is False

    def test_can_handle_empty_message(self, ai_handler):
        """Test empty message is not handled."""
        update = {
            'message': {
                'text': '',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is False

    def test_can_handle_photo_in_ai_topic(self, ai_handler):
        """Test photo in AI topic is handled."""
        update = {
            'message': {
                'photo': [{'file_id': 'xyz'}],
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
                'message_thread_id': 42,
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_photo_with_caption(self, ai_handler):
        """Test photo with caption /ai command is handled."""
        update = {
            'message': {
                'photo': [{'file_id': 'xyz'}],
                'caption': '/ai explain this',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is True

    def test_can_handle_no_text_no_photo(self, ai_handler):
        """Test message without text or photo is not handled."""
        update = {
            'message': {
                'sticker': {'file_id': 'xyz'},
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert ai_handler.can_handle(update) is False

    def test_can_handle_topic_ai_zero(self, mock_bot, mock_db, mock_config):
        """Test when TOPIC_AI is 0 (disabled)."""
        mock_config.TOPIC_AI = 0
        handler = AIHandler(mock_bot, mock_db, mock_config)
        handler._client = Mock()

        update = {
            'message': {
                'text': 'free text',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        assert handler.can_handle(update) is False


class TestAIHandlerHandle:
    """Test handle() - main entry point routing."""

    def test_handle_ai_reset(self, ai_handler):
        """Test handle routes /ai_reset to _handle_reset."""
        update = {
            'message': {
                'text': '/ai_reset',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler._handle_reset = Mock()
        ai_handler.handle(update)
        ai_handler._handle_reset.assert_called_once_with('123', None, '1652899')

    def test_handle_ai_status(self, ai_handler):
        """Test handle routes /ai_status to _handle_status."""
        update = {
            'message': {
                'text': '/ai_status',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler._handle_status = Mock()
        ai_handler.handle(update)
        ai_handler._handle_status.assert_called_once_with('123', None)

    def test_handle_ai_skill(self, ai_handler):
        """Test handle routes /ai_skill to _handle_skill."""
        update = {
            'message': {
                'text': '/ai_skill vpn-ops check status',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler._handle_skill = Mock()
        ai_handler.handle(update)
        ai_handler._handle_skill.assert_called_once_with('123', None, '/ai_skill vpn-ops check status')

    def test_handle_ai_yolo(self, ai_handler):
        """Test handle routes /ai_yolo to _handle_yolo."""
        update = {
            'message': {
                'text': '/ai_yolo restart server',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler._handle_yolo = Mock()
        ai_handler.handle(update)
        ai_handler._handle_yolo.assert_called_once_with('123', None, '/ai_yolo restart server')

    def test_handle_ai_fast_mode(self, ai_handler):
        """Test /ai_fast sets mode to 'fast'."""
        update = {
            'message': {
                'text': '/ai_fast quick question',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler._handle_prompt = Mock()
        ai_handler.handle(update)
        call_kwargs = ai_handler._handle_prompt.call_args[1]
        assert call_kwargs['mode'] == 'fast'

    def test_handle_ai_plan_mode(self, ai_handler):
        """Test /ai_plan sets mode to 'plan'."""
        update = {
            'message': {
                'text': '/ai_plan detailed plan',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler._handle_prompt = Mock()
        ai_handler.handle(update)
        call_kwargs = ai_handler._handle_prompt.call_args[1]
        assert call_kwargs['mode'] == 'plan'

    def test_handle_ai_default_mode(self, ai_handler):
        """Test /ai uses default mode (None, resolved inside _handle_prompt)."""
        update = {
            'message': {
                'text': '/ai regular question',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler._handle_prompt = Mock()
        ai_handler.handle(update)
        call_kwargs = ai_handler._handle_prompt.call_args[1]
        assert call_kwargs['mode'] is None

    def test_handle_free_text_in_topic(self, ai_handler):
        """Test free text in TOPIC_AI triggers prompt with default mode."""
        update = {
            'message': {
                'text': 'help me debug',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
                'message_thread_id': 42,
            }
        }
        ai_handler._handle_prompt = Mock()
        ai_handler.handle(update)
        call_args = ai_handler._handle_prompt.call_args
        assert call_args[0][2] == 'help me debug'  # prompt is positional
        assert call_args[1]['mode'] is None

    def test_handle_non_admin_rejected(self, ai_handler):
        """Test non-admin user is rejected."""
        update = {
            'message': {
                'text': '/ai test',
                'from': {'id': '999999'},
                'chat': {'id': '123'},
            }
        }
        ai_handler.handle(update)
        ai_handler._reply.assert_called_once_with('123', None, '🚫 Только админ.')

    def test_handle_ai_command_no_prompt(self, ai_handler):
        """Test /ai with no prompt shows usage."""
        update = {
            'message': {
                'text': '/ai',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler.handle(update)
        reply_call = ai_handler.bot.send_message.call_args
        assert 'Использование:' in reply_call[1]['text']

    def test_handle_ai_with_photo(self, ai_handler):
        """Test photo is downloaded and path included in prompt."""
        update = {
            'message': {
                'photo': [{'file_id': 'photo123'}],
                'caption': '/ai what is this',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler._download_photo = Mock(return_value='/tmp/tg_media/test.jpg')
        ai_handler._handle_prompt = Mock()

        ai_handler.handle(update)

        photo_path = ai_handler._handle_prompt.call_args[0][2]
        assert '[Вложение от админа: /tmp/tg_media/test.jpg]' in photo_path

    def test_handle_photo_without_caption(self, ai_handler):
        """Test photo without caption uses default prompt."""
        update = {
            'message': {
                'photo': [{'file_id': 'photo123'}],
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        ai_handler._download_photo = Mock(return_value='/tmp/tg_media/test.jpg')
        ai_handler._handle_prompt = Mock()

        ai_handler.handle(update)

        photo_path = ai_handler._handle_prompt.call_args[0][2]
        assert 'Посмотри файл и опиши что видишь' in photo_path

    def test_handle_photo_cleanup_on_success(self, ai_handler):
        """Test photo is cleaned up after successful handling."""
        update = {
            'message': {
                'photo': [{'file_id': 'photo123'}],
                'caption': '/ai test',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        photo_path = '/tmp/test_photo.jpg'
        ai_handler._download_photo = Mock(return_value=photo_path)
        ai_handler._handle_prompt = Mock()

        with patch('os.unlink') as mock_unlink:
            ai_handler.handle(update)
            mock_unlink.assert_called_once_with(photo_path)

    def test_handle_photo_cleanup_on_error(self, ai_handler):
        """Test photo is cleaned up even when handler fails."""
        update = {
            'message': {
                'photo': [{'file_id': 'photo123'}],
                'caption': '/ai test',
                'from': {'id': '1652899'},
                'chat': {'id': '123'},
            }
        }
        photo_path = '/tmp/test_photo.jpg'
        ai_handler._download_photo = Mock(return_value=photo_path)
        ai_handler._handle_prompt = Mock(side_effect=Exception("Test error"))

        with patch('os.unlink') as mock_unlink:
            try:
                ai_handler.handle(update)
            except Exception:
                pass
            mock_unlink.assert_called_once_with(photo_path)


class TestHandlePrompt:
    """Test _handle_prompt() - core AI interaction."""

    def test_handle_prompt_success(self, ai_handler):
        """Test successful prompt handling."""
        ai_handler._handle_prompt('123', None, 'test prompt')
        _wait_turn(ai_handler)

        ai_handler._client.ask.assert_called_once()
        ai_handler.bot.send_message.assert_called_once()
        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert 'Test response' in reply_text
        assert '⏱' in reply_text

    def test_handle_prompt_with_session_key(self, ai_handler):
        """Test session key is generated correctly."""
        ai_handler._handle_prompt('123', 42, 'test')
        _wait_turn(ai_handler)

        call_args = ai_handler._client.ask.call_args
        assert call_args[0][0] == 'topic:123:42'  # session_key

    def test_handle_prompt_no_client_configured(self, ai_handler):
        """Test error when client is not configured."""
        ai_handler._client = None
        ai_handler._handle_prompt('123', None, 'test')
        _wait_turn(ai_handler)

        ai_handler._reply.assert_called_once()
        reply_text = ai_handler._reply.call_args[0][2]
        assert 'AI не сконфигурирован' in reply_text

    def test_handle_prompt_bridge_unavailable(self, ai_handler):
        """Test handling when bridge is unavailable."""
        ai_handler._client.ask = Mock(side_effect=AgentUnavailable("Connection failed"))
        ai_handler._handle_prompt('123', None, 'test')
        _wait_turn(ai_handler)

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'AI-сервер не отвечает' in reply_text

    def test_handle_prompt_rate_limit_error(self, ai_handler):
        """Test pretty error for rate limit (429)."""
        error = AgentError("HTTP 429: rate_limit_exceeded")
        ai_handler._client.ask = Mock(side_effect=error)
        ai_handler._handle_prompt('123', None, 'test')
        _wait_turn(ai_handler)

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Квота API провайдера исчерпана' in reply_text

    def test_handle_prompt_timeout_error(self, ai_handler):
        """Test pretty error for timeout (504)."""
        error = AgentError("HTTP 504: gateway timeout")
        ai_handler._client.ask = Mock(side_effect=error)
        ai_handler._handle_prompt('123', None, 'test')
        _wait_turn(ai_handler)

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'не уложился в лимит' in reply_text

    def test_handle_prompt_generic_error(self, ai_handler):
        """Test generic error message."""
        error = AgentError("HTTP 500: internal error")
        ai_handler._client.ask = Mock(side_effect=error)
        ai_handler._handle_prompt('123', None, 'test')
        _wait_turn(ai_handler)

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Ошибка агента:' in reply_text

    def test_handle_prompt_unexpected_exception(self, ai_handler):
        """Test handling of unexpected exceptions."""
        ai_handler._client.ask = Mock(side_effect=RuntimeError("Unexpected"))
        ai_handler._handle_prompt('123', None, 'test')
        _wait_turn(ai_handler)

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Неожиданная ошибка' in reply_text

    def test_handle_prompt_empty_response(self, ai_handler):
        """Test empty response from the agent."""
        ai_handler._client.ask = Mock(return_value=("", 1000))
        ai_handler._handle_prompt('123', None, 'test')
        _wait_turn(ai_handler)

        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert '(пустой ответ от агента)' in reply_text

    def test_handle_prompt_with_file_marker(self, ai_handler):
        """Test file marker is extracted and file is sent."""
        ai_handler._client.ask = Mock(
            return_value=("Check this [[SEND_FILE: /tmp/result.png | analysis]]", 1500)
        )
        ai_handler._send_file = Mock()
        ai_handler._handle_prompt('123', None, 'test')
        _wait_turn(ai_handler)

        ai_handler._send_file.assert_called_once_with('123', None, '/tmp/result.png', 'analysis')

    def test_handle_prompt_long_response_truncated(self, ai_handler):
        """Test long response is truncated to TELEGRAM_TEXT_LIMIT."""
        long_text = "A" * (TELEGRAM_TEXT_LIMIT + 1000)
        ai_handler._client.ask = Mock(return_value=(long_text, 1500))
        ai_handler._handle_prompt('123', None, 'test')
        _wait_turn(ai_handler)

        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert len(reply_text) <= TELEGRAM_TEXT_LIMIT + 200  # + footer
        assert '(обрезано)' in reply_text

    def test_handle_prompt_mode_footer(self, ai_handler):
        """Test footer includes mode information."""
        ai_handler._client.ask = Mock(return_value=("Response", 1500))
        ai_handler._handle_prompt('123', None, 'test', mode='plan')
        _wait_turn(ai_handler)

        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert '🧠 plan' in reply_text

    def test_handle_prompt_yolo_footer(self, ai_handler):
        """Test footer includes yolo marker."""
        ai_handler._client.ask = Mock(return_value=("Response", 1500))
        ai_handler._handle_prompt('123', None, 'test', yolo=True)
        _wait_turn(ai_handler)

        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert '☠️ yolo' in reply_text

    def test_handle_prompt_default_mode_resolves(self, ai_handler):
        """Test default mode is resolved from config when None is passed."""
        ai_handler._client.ask = Mock(return_value=("Response", 1500))
        ai_handler._handle_prompt('123', None, 'test', mode=None)
        _wait_turn(ai_handler)

        # Should use config default ('fast' in our mock_config)
        call_args = ai_handler._client.ask.call_args
        assert call_args[1]['mode'] == 'fast'

    def test_handle_prompt_typing_keepalive_stops(self, ai_handler):
        """Test typing keepalive is stopped after response."""
        with patch('bot.handlers.ai_handler._start_typing_keepalive') as mock_typing:
            mock_stop = Mock()
            mock_typing.return_value = mock_stop
            ai_handler._client.ask = Mock(return_value=("Response", 1500))
            ai_handler._handle_prompt('123', None, 'test')
            _wait_turn(ai_handler)
            mock_stop.set.assert_called_once()


class TestHandleReset:
    """Test _handle_reset() - session clearing."""

    def test_handle_reset_existing_session(self, ai_handler):
        """Test reset with existing session."""
        ai_handler._client.forget_session = Mock(return_value=True)
        ai_handler._handle_reset('123', None, '1652899')

        reply_text = ai_handler._reply.call_args[0][2]
        assert '🔄 Сессия очищена' in reply_text

    def test_handle_reset_no_session(self, ai_handler):
        """Test reset when no session exists."""
        ai_handler._client.forget_session = Mock(return_value=False)
        ai_handler._handle_reset('123', None, '1652899')

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Активной сессии не было' in reply_text

    def test_handle_reset_no_client(self, ai_handler):
        """Test reset when client is not configured."""
        ai_handler._client = None
        ai_handler._handle_reset('123', None, '1652899')

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'AI не сконфигурирован' in reply_text

    def test_handle_reset_non_admin(self, ai_handler):
        """Test reset by non-admin is rejected."""
        ai_handler._handle_reset('123', None, '999999')

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Только админ' in reply_text

    def test_handle_reset_session_key_format(self, ai_handler):
        """Test session key format for thread vs PM."""
        ai_handler._handle_reset('123', 42, '1652899')

        call_args = ai_handler._client.forget_session.call_args
        assert call_args[0][0] == 'topic:123:42'


class TestHandleStatus:
    """Test _handle_status() - status display."""

    def test_handle_status_success(self, ai_handler):
        """Test status displays all information."""
        ai_handler._handle_status('123', None)

        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert '🤖 <b>AI Status:</b>' in reply_text
        assert 'Server Status:' in reply_text

    def test_handle_status_no_client(self, ai_handler):
        """Test status when client is not configured."""
        ai_handler._client = None
        ai_handler._handle_status('123', None)

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'AI не сконфигурирован' in reply_text

    def test_handle_status_degraded(self, ai_handler):
        """Test status when bridge is degraded."""
        ai_handler._client.ping = Mock(return_value={'status': 'error'})
        ai_handler._handle_status('123', None)

        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert 'Degraded' in reply_text

    def test_handle_status_ping_exception(self, ai_handler):
        """Test status when ping raises exception."""
        ai_handler._client.ping = Mock(side_effect=Exception("Connection error"))
        ai_handler._handle_status('123', None)

        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert 'Unavailable' in reply_text
        assert 'Connection error' in reply_text

    def test_handle_status_session_info(self, ai_handler):
        """Test status includes current session info."""
        ai_handler._client.get_session = Mock(return_value='session_xyz')
        ai_handler._handle_status('123', None)

        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert 'session_xyz' in reply_text

    def test_handle_status_no_session(self, ai_handler):
        """Test status when no current session."""
        ai_handler._client.get_session = Mock(return_value=None)
        ai_handler._handle_status('123', None)

        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert 'None' in reply_text


class TestHandleSkill:
    """Test _handle_skill() command handler."""

    def test_handle_skill_success(self, ai_handler):
        """Test skill command with valid skill."""
        ai_handler._handle_prompt = Mock()
        ai_handler._handle_skill('123', None, '/ai_skill vpn-ops check status')

        call_args = ai_handler._handle_prompt.call_args
        prompt = call_args[0][2]
        assert '/skill:vpn-ops' in prompt

    def test_handle_skill_no_client(self, ai_handler):
        """Test skill when client is not configured."""
        ai_handler._client = None
        ai_handler._handle_skill('123', None, '/ai_skill vpn-ops test')

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'AI не сконфигурирован' in reply_text

    def test_handle_skill_missing_args(self, ai_handler):
        """Test skill command with missing arguments."""
        ai_handler._handle_skill('123', None, '/ai_skill')

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Использование:' in reply_text
        assert '/ai_skill' in reply_text

    def test_handle_skill_invalid_skill_name(self, ai_handler):
        """Test skill command with invalid skill name."""
        ai_handler._handle_skill('123', None, '/ai_skill invalid-skill test')

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Неверное имя навыка' in reply_text
        assert 'vpn-ops' in reply_text  # shows allowed skills


class TestHandleYolo:
    """Test _handle_yolo() command handler."""

    def test_handle_yolo_success(self, ai_handler):
        """Test yolo command passes yolo=True."""
        ai_handler._handle_prompt = Mock()
        ai_handler._handle_yolo('123', None, '/ai_yolo risky command')

        call_args = ai_handler._handle_prompt.call_args
        assert call_args[1]['yolo'] is True
        prompt = call_args[0][2]  # prompt is positional
        assert 'risky command' in prompt

    def test_handle_yolo_no_client(self, ai_handler):
        """Test yolo when client is not configured."""
        ai_handler._client = None
        ai_handler._handle_yolo('123', None, '/ai_yolo test')

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'AI не сконфигурирован' in reply_text

    def test_handle_yolo_missing_args(self, ai_handler):
        """Test yolo command with missing arguments."""
        ai_handler._handle_yolo('123', None, '/ai_yolo')

        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Использование:' in reply_text


class TestDownloadPhoto:
    """Test _download_photo() - photo handling."""

    def test_download_photo_success(self, ai_handler, tmp_path):
        """Test successful photo download."""
        msg = {
            'photo': [
                {'file_id': 'small'},
                {'file_id': 'large123'},
            ]
        }
        ai_handler.bot.client.download_file = Mock(return_value=True)

        with patch('bot.handlers.ai_handler.TG_MEDIA_DIR', str(tmp_path)):
            result = ai_handler._download_photo(msg, '123', None)

        assert result is not None
        assert 'tg_photo_123_' in result
        assert 'large123' in result
        ai_handler.bot.client.download_file.assert_called_once()

    def test_download_photo_no_photo(self, ai_handler):
        """Test with no photo in message."""
        msg = {'photo': []}
        result = ai_handler._download_photo(msg, '123', None)
        assert result is None

    def test_download_photo_download_fails(self, ai_handler):
        """Test when download fails."""
        msg = {'photo': [{'file_id': 'xyz'}]}
        ai_handler.bot.client.download_file = Mock(return_value=False)

        result = ai_handler._download_photo(msg, '123', None)
        assert result is None
        ai_handler._reply.assert_called_once()
        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Не удалось скачать фото' in reply_text

    def test_download_photo_uses_largest(self, ai_handler, tmp_path):
        """Test uses the largest photo (last in array)."""
        msg = {
            'photo': [
                {'file_id': 'small'},
                {'file_id': 'medium'},
                {'file_id': 'large_orig'},
            ]
        }
        ai_handler.bot.client.download_file = Mock(return_value=True)

        with patch('bot.handlers.ai_handler.TG_MEDIA_DIR', str(tmp_path)):
            result = ai_handler._download_photo(msg, '123', None)

        call_args = ai_handler.bot.client.download_file.call_args
        assert call_args[0][0] == 'large_orig'

    def test_download_photo_no_file_id(self, ai_handler):
        """Test when photo has no file_id."""
        msg = {'photo': [{'size': 123}]}  # no file_id
        result = ai_handler._download_photo(msg, '123', None)
        assert result is None


class TestSendFile:
    """Test _send_file() - sending files from the shared out dir.

    OpenCode writes into AGENT_OUT_DIR (a bind-mount shared with the host);
    the bot reads the file directly — no HTTP download. Only paths under
    AGENT_OUT_DIR are accepted.
    """

    @staticmethod
    def _make_file(tmp_path, name="result.png"):
        p = tmp_path / name
        p.write_bytes(b"data")
        return str(p)

    def test_send_file_success(self, ai_handler, tmp_path):
        """Valid file under the out dir is sent as a document."""
        path = self._make_file(tmp_path)
        with patch('bot.handlers.ai_handler.AGENT_OUT_DIR', str(tmp_path)), \
                patch('os.unlink'):
            ai_handler._send_file('123', None, path, 'Caption')
        ai_handler.bot.client.send_document.assert_called_once()
        assert ai_handler.bot.client.send_document.call_args[1]['document'] == path

    def test_send_file_outside_dir_rejected(self, ai_handler, tmp_path):
        """A path outside the shared out dir is refused (anti-exfiltration)."""
        with patch('bot.handlers.ai_handler.AGENT_OUT_DIR', str(tmp_path)):
            ai_handler._send_file('123', None, '/etc/passwd', 'x')
        ai_handler.bot.client.send_document.assert_not_called()
        reply_text = ai_handler._reply.call_args[0][2]
        assert 'вне разрешённого каталога' in reply_text

    def test_send_file_missing(self, ai_handler, tmp_path):
        """A missing file under the out dir reports not-found."""
        with patch('bot.handlers.ai_handler.AGENT_OUT_DIR', str(tmp_path)):
            ai_handler._send_file('123', None, str(tmp_path / 'nope.png'), None)
        ai_handler.bot.client.send_document.assert_not_called()
        reply_text = ai_handler._reply.call_args[0][2]
        assert 'не найден' in reply_text

    def test_send_file_telegram_rejects(self, ai_handler, tmp_path):
        """Test when Telegram rejects the file."""
        path = self._make_file(tmp_path)
        ai_handler.bot.client.send_document = Mock(return_value=None)
        with patch('bot.handlers.ai_handler.AGENT_OUT_DIR', str(tmp_path)), \
                patch('os.unlink'):
            ai_handler._send_file('123', None, path, 'Caption')
        reply_text = ai_handler._reply.call_args[0][2]
        assert 'Telegram отверг файл' in reply_text

    def test_send_file_with_thread_id(self, ai_handler, tmp_path):
        """Test file sending with thread_id."""
        path = self._make_file(tmp_path)
        with patch('bot.handlers.ai_handler.AGENT_OUT_DIR', str(tmp_path)), \
                patch('os.unlink'):
            ai_handler._send_file('123', 42, path, 'Caption')
        call_kwargs = ai_handler.bot.client.send_document.call_args[1]
        assert call_kwargs['message_thread_id'] == 42

    def test_send_file_cleanup(self, ai_handler, tmp_path):
        """Test the out file is cleaned up after sending."""
        path = self._make_file(tmp_path)
        with patch('bot.handlers.ai_handler.AGENT_OUT_DIR', str(tmp_path)), \
                patch('os.unlink') as mock_unlink:
            ai_handler._send_file('123', None, path, 'Caption')
            mock_unlink.assert_called_once()

    def test_send_file_cleanup_error_logged(self, ai_handler, tmp_path):
        """Test cleanup errors are caught."""
        path = self._make_file(tmp_path)
        with patch('bot.handlers.ai_handler.AGENT_OUT_DIR', str(tmp_path)), \
                patch('os.unlink', side_effect=OSError("Permission denied")):
            ai_handler._send_file('123', None, path, 'Caption')
        # Should not raise, file cleanup failure is swallowed.
        assert True


class TestSessionKey:
    """Test _session_key() helper method."""

    def test_session_key_with_thread_id(self, ai_handler):
        """Test session key format with thread_id."""
        key = ai_handler._session_key('123', 42)
        assert key == 'topic:123:42'

    def test_session_key_without_thread_id(self, ai_handler):
        """Test session key format without thread_id (PM)."""
        key = ai_handler._session_key('123', None)
        assert key == 'pm:123'

    def test_session_key_thread_id_zero(self, ai_handler):
        """Test session key with thread_id=0 (treated as no thread)."""
        key = ai_handler._session_key('123', 0)
        # When thread_id is 0 (falsy), should still be pm: format per current code
        assert key == 'pm:123'


class TestAIHandlerInit:
    """Test AIHandler initialization."""

    def test_init_with_opencode_url(self, mock_bot, mock_db, mock_config):
        """Test initialization creates AgentClient when URL is set."""
        mock_config.OPENCODE_URL = 'http://localhost:4096'
        handler = AIHandler(mock_bot, mock_db, mock_config)
        assert handler._client is not None

    def test_init_without_opencode_url(self, mock_bot, mock_db, mock_config):
        """Test initialization skips client when URL is not set."""
        mock_config.OPENCODE_URL = ''
        handler = AIHandler(mock_bot, mock_db, mock_config)
        assert handler._client is None


class TestReplyMethod:
    """Test _reply() method."""

    def test_reply_basic(self, ai_handler):
        """Test basic reply call."""
        ai_handler._reply('123', None, 'Test message')
        ai_handler.bot.send_message.assert_called_once_with(
            chat_id='123',
            text='Test message',
            message_thread_id=None,
            parse_mode=None,
        )

    def test_reply_with_parse_mode(self, ai_handler):
        """Test reply with parse_mode."""
        ai_handler._reply('123', 42, '<b>Bold</b>', parse_mode='HTML')
        ai_handler.bot.send_message.assert_called_once_with(
            chat_id='123',
            text='<b>Bold</b>',
            message_thread_id=42,
            parse_mode='HTML',
        )

    def test_reply_exception_logged(self, ai_handler):
        """Test exception in reply is logged."""
        ai_handler.bot.send_message = Mock(side_effect=Exception("Telegram error"))
        # Should not raise, just log
        ai_handler._reply('123', None, 'Test')
        assert True


class TestDBErrorHandling:
    """Test database error handling in status."""

    def test_status_db_query_fails(self, ai_handler):
        """Test status when DB query fails."""
        ai_handler.db._connect = Mock(side_effect=Exception("DB error"))
        ai_handler._handle_status('123', None)

        # Should still show status without crashing
        reply_text = ai_handler.bot.send_message.call_args[1]['text']
        assert 'AI Status:' in reply_text


class TestTypingKeepaliveError:
    """Test typing keepalive error handling."""

    def test_typing_keepalive_exception_in_loop(self, ai_handler):
        """Test typing keepalive handles exceptions gracefully."""
        with patch('threading.Thread') as mock_thread:
            def side_effect(target, name, daemon):
                t = Mock()
                # Simulate the thread function
                def run_once():
                    try:
                        target()
                    except Exception:
                        pass
                t.start = Mock()
                return t
            mock_thread.side_effect = side_effect

            with patch.object(ai_handler.bot.client, 'send_chat_action',
                             side_effect=Exception("API error")):
                # Should not crash
                stop = _start_typing_keepalive(ai_handler.bot, '123', 42)
                assert stop is not None
