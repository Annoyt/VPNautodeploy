"""Unit tests for bot/services/agent_client.py (OpenCode HTTP client)."""

import os
import tempfile
from unittest.mock import Mock, patch

import pytest

from bot.services.agent_client import (
    AgentClient,
    AgentUnavailable,
    AgentError,
    _detect_skill_domains,
    _build_skill_reminder,
)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def client(temp_db):
    return AgentClient("http://localhost:4096", "pw", temp_db)


def _resp(status=200, json_data=None):
    r = Mock()
    r.status_code = status
    r.content = b"{}"
    r.text = ""
    r.json = Mock(return_value=json_data or {})
    return r


class TestConfig:
    def test_is_configured(self, temp_db):
        assert AgentClient("http://x", "pw", temp_db).is_configured() is True
        assert AgentClient("", "pw", temp_db).is_configured() is False

    def test_base_url_stripped(self, temp_db):
        c = AgentClient("http://x:4096/", "pw", temp_db)
        assert c.base_url == "http://x:4096"

    def test_auth_header_basic(self, client):
        h = client._headers()
        assert h["Authorization"].startswith("Basic ")

    def test_no_auth_header_without_password(self, temp_db):
        c = AgentClient("http://x", "", temp_db)
        assert "Authorization" not in c._headers()


class TestSessionMemory:
    def test_roundtrip(self, client):
        assert client.get_session("pm:1") is None
        client.remember_session("pm:1", "ses_abc")
        assert client.get_session("pm:1") == "ses_abc"

    def test_forget(self, client):
        client.remember_session("pm:1", "ses_abc")
        with patch("bot.services.agent_client.requests.delete") as d:
            d.return_value = _resp()
            assert client.forget_session("pm:1") is True
        assert client.get_session("pm:1") is None
        assert client.forget_session("pm:1") is False


class TestSkillRouting:
    def test_incident_wins(self):
        assert _detect_skill_domains("прод лежит у всех") == ["incident-response"]

    def test_vpn_ops(self):
        assert "vpn-ops" in _detect_skill_domains("проверь xray inbound")

    def test_generic_fallback(self):
        assert _detect_skill_domains("проверь это") == ["__generic__"]

    def test_no_match(self):
        assert _detect_skill_domains("привет, как дела") == []

    def test_reminder_mentions_skill(self):
        assert "vpn-ops" in _build_skill_reminder(["vpn-ops"])


class TestExtractText:
    def test_parts_text(self):
        assert AgentClient._extract_text(
            {"parts": [{"type": "text", "text": "hi"}]}
        ) == "hi"

    def test_parts_content_key(self):
        assert AgentClient._extract_text(
            {"parts": [{"type": "text", "content": "yo"}]}
        ) == "yo"

    def test_info_parts(self):
        assert AgentClient._extract_text(
            {"info": {"parts": [{"type": "text", "text": "deep"}]}}
        ) == "deep"

    def test_fallback_response_key(self):
        assert AgentClient._extract_text({"response": "flat"}) == "flat"

    def test_empty(self):
        assert AgentClient._extract_text({"parts": []}) == ""


class TestPing:
    def test_ping_ok(self, client):
        with patch("bot.services.agent_client.requests.get") as g:
            g.return_value = _resp(json_data={"healthy": True, "version": "1.2"})
            g.return_value.raise_for_status = Mock()
            out = client.ping()
        assert out == {"status": "ok", "version": "1.2"}

    def test_ping_unavailable(self, temp_db):
        with pytest.raises(AgentUnavailable):
            AgentClient("", "pw", temp_db).ping()


class TestAsk:
    def test_ask_not_configured(self, temp_db):
        with pytest.raises(AgentUnavailable):
            AgentClient("", "pw", temp_db).ask("pm:1", "hi")

    def test_ask_happy_path_creates_and_remembers_session(self, client):
        create = _resp(json_data={"id": "ses_1"})
        message = _resp(json_data={"parts": [{"type": "text", "text": "pong"}]})
        with patch("bot.services.agent_client.requests.post", side_effect=[create, message]) as p:
            reply, ms = client.ask("pm:1", "ping")
        assert reply == "pong"
        assert ms >= 0
        assert client.get_session("pm:1") == "ses_1"
        # First POST creates the session, second sends the message.
        assert p.call_count == 2

    def test_ask_sends_model_as_object(self, temp_db):
        """model must be posted as {providerID, modelID}, not a string
        (OpenCode's /message endpoint rejects a bare string with HTTP 400)."""
        client = AgentClient(
            "http://localhost:4096", "pw", temp_db,
            default_model="opencode-go/minimax-m3",
        )
        create = _resp(json_data={"id": "ses_1"})
        message = _resp(json_data={"parts": [{"type": "text", "text": "ok"}]})
        with patch("bot.services.agent_client.requests.post", side_effect=[create, message]) as p:
            client.ask("pm:1", "hi")
        body = p.call_args_list[1].kwargs["json"]
        assert body["model"] == {"providerID": "opencode-go", "modelID": "minimax-m3"}

    def test_ask_http_error_raises(self, client):
        create = _resp(json_data={"id": "ses_1"})
        err = _resp(status=500)
        err.json = Mock(return_value={"error": "boom"})
        with patch("bot.services.agent_client.requests.post", side_effect=[create, err]):
            with pytest.raises(AgentError):
                client.ask("pm:1", "ping")
