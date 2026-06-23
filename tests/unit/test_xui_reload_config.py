"""Tests for XUI reload configuration - Phase 1 fix verification."""

import os
import pytest
from unittest.mock import Mock, patch, MagicMock

from bot.config import Settings


class TestXUIContainerNameConfig:
    """Test XUI_CONTAINER_NAME configuration (C-05 fix)."""
    
    def test_default_container_name(self):
        """Test default container name is '3x-ui'."""
        settings = Settings()
        assert hasattr(settings, 'XUI_CONTAINER_NAME')
        assert settings.XUI_CONTAINER_NAME == '3x-ui'
    
    def test_custom_container_name_from_env(self):
        """Test custom container name from environment variable."""
        with patch.dict(os.environ, {'XUI_CONTAINER_NAME': 'custom-xui'}):
            settings = Settings()
            assert settings.XUI_CONTAINER_NAME == 'custom-xui'
    
    def test_container_name_is_string(self):
        """Test that container name is always a string."""
        settings = Settings()
        assert isinstance(settings.XUI_CONTAINER_NAME, str)


class TestXUIReloadFunction:
    """Test reload_xray function talks to the sidecar."""

    @patch('bot.services.xui_reload.requests.post')
    @patch('bot.services.xui_reload._config')
    def test_reload_uses_sidecar_url_and_token(self, mock_config, mock_post):
        """Test that reload_xray POSTs to the configured sidecar with token."""
        mock_config.return_value = ('http://host.docker.internal:7079', 'secret-token')

        mock_response = Mock()
        mock_response.status_code = 200
        mock_post.return_value = mock_response

        from bot.services.xui_reload import reload_xray
        result = reload_xray()

        assert result is True
        mock_post.assert_called_once()
        url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args.kwargs.get('url')
        assert url == 'http://host.docker.internal:7079/reload-xray'
        assert mock_post.call_args.kwargs.get('headers') == {'X-Token': 'secret-token'}

    @patch('bot.services.xui_reload.requests.post')
    @patch('bot.services.xui_reload._config')
    def test_reload_noop_when_url_missing(self, mock_config, mock_post):
        """Test that reload_xray is a no-op when XRAY_RELOAD_URL is empty."""
        mock_config.return_value = ('', '')

        from bot.services.xui_reload import reload_xray
        result = reload_xray()

        assert result is False
        mock_post.assert_not_called()

    @patch('bot.services.xui_reload.requests.post')
    @patch('bot.services.xui_reload._config')
    def test_reload_treats_sidecar_cooldown_as_success(self, mock_config, mock_post):
        """Test that HTTP 429 from sidecar is treated as success."""
        mock_config.return_value = ('http://host.docker.internal:7079', 'secret-token')

        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.json.return_value = {'retry_after_sec': 5}
        mock_post.return_value = mock_response

        from bot.services.xui_reload import reload_xray
        result = reload_xray()

        assert result is True


class TestXUIReloadSecurity:
    """Test security aspects of XUI reload."""

    def test_no_hardcoded_secrets_in_source(self):
        """Verify no hardcoded URLs or tokens in reload function."""
        import inspect
        from bot.services import xui_reload

        reload_source = inspect.getsource(xui_reload.reload_xray)
        config_source = inspect.getsource(xui_reload._config)

        # Secrets must come from environment / _config, not literals.
        assert 'XRAY_RELOAD_URL' in config_source
        assert 'XRAY_RELOAD_TOKEN' in config_source
        assert 'http://host.docker.internal' not in reload_source
