"""Unit tests for bot/services/hermes_client.py (Hermes API client)."""

import os
import tempfile
from unittest.mock import Mock, patch

import pytest
import requests

from bot.services.hermes_client import HermesAgentClient, HERMES_MODEL_ALIAS
from bot.services.agent_client import AgentUnavailable, AgentError, SYSTEM_PREAMBLE


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
    return HermesAgentClient("http://localhost:4097", "secret", temp_db)


def _resp(status=200, json_data=None, text=""):
    r = Mock()
    r.status_code = status
    r.content = b"{}"
    r.text = text
    r.json = Mock(return_value=json_data if json_data is not None else {})
    return r


def _completion(content, finish="stop"):
    return {"choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": finish}]}


class TestConfig:
    def test_is_configured(self, temp_db):
        assert HermesAgentClient("http://x", "k", temp_db).is_configured() is True
        assert HermesAgentClient("", "k", temp_db).is_configured() is False

    def test_base_url_stripped(self, temp_db):
        assert HermesAgentClient("http://x:4097/", "k", temp_db).base_url == "http://x:4097"

    def test_default_model_alias(self, temp_db):
        assert HermesAgentClient("http://x", "k", temp_db).default_model == HERMES_MODEL_ALIAS
        assert HermesAgentClient("http://x", "k", temp_db, default_model="foo").default_model == "foo"

    def test_headers_bearer_and_session(self, client):
        h = client._headers("sess-1")
        assert h["Authorization"] == "Bearer secret"
        assert h["X-Hermes-Session-Id"] == "sess-1"
        assert h["X-Hermes-Session-Key"] == "sess-1"

    def test_headers_no_session(self, client):
        h = client._headers()
        assert "X-Hermes-Session-Id" not in h


class TestAsk:
    def test_posts_chat_completions_and_parses(self, client):
        with patch("bot.services.hermes_client.requests.post",
                   return_value=_resp(json_data=_completion("hello world"))) as mp:
            reply, ms = client.ask("pm:1", "привет")
        assert reply == "hello world"
        assert ms >= 0
        url = mp.call_args[0][0]
        assert url.endswith("/v1/chat/completions")
        body = mp.call_args.kwargs["json"]
        assert body["model"] == HERMES_MODEL_ALIAS
        assert body["stream"] is False
        assert body["messages"][0]["role"] == "user"
        # Fresh session → preamble prepended
        assert SYSTEM_PREAMBLE in body["messages"][0]["content"]
        # Bearer + session headers present
        headers = mp.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer secret"
        assert "X-Hermes-Session-Id" in headers

    def test_second_turn_no_preamble_same_session(self, client):
        with patch("bot.services.hermes_client.requests.post",
                   return_value=_resp(json_data=_completion("a"))):
            client.ask("pm:1", "first")
        with patch("bot.services.hermes_client.requests.post",
                   return_value=_resp(json_data=_completion("b"))) as mp2:
            client.ask("pm:1", "second")
        body = mp2.call_args.kwargs["json"]
        assert SYSTEM_PREAMBLE not in body["messages"][0]["content"]
        # continuity: same session id reused
        assert mp2.call_args.kwargs["headers"]["X-Hermes-Session-Id"] == client.get_session("pm:1")

    def test_read_timeout_is_agent_error(self, client):
        with patch("bot.services.hermes_client.requests.post",
                   side_effect=requests.exceptions.Timeout("read timed out")):
            with pytest.raises(AgentError):
                client.ask("pm:1", "x", timeout=5)

    def test_connect_timeout_is_unavailable(self, client):
        with patch("bot.services.hermes_client.requests.post",
                   side_effect=requests.exceptions.ConnectTimeout("no connect")):
            with pytest.raises(AgentUnavailable):
                client.ask("pm:1", "x")

    def test_request_exception_is_unavailable(self, client):
        with patch("bot.services.hermes_client.requests.post",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            with pytest.raises(AgentUnavailable):
                client.ask("pm:1", "x")

    def test_http_error_is_agent_error(self, client):
        with patch("bot.services.hermes_client.requests.post",
                   return_value=_resp(status=500, json_data={"error": "boom"})):
            with pytest.raises(AgentError):
                client.ask("pm:1", "x")

    def test_finish_reason_error_raises(self, client):
        bad = _completion("openrouter/... is not a valid model ID", finish="error")
        with patch("bot.services.hermes_client.requests.post",
                   return_value=_resp(json_data=bad)):
            with pytest.raises(AgentError):
                client.ask("pm:1", "x")

    def test_not_configured_raises(self, temp_db):
        c = HermesAgentClient("", "k", temp_db)
        with pytest.raises(AgentUnavailable):
            c.ask("pm:1", "x")


class TestSessionReset:
    def test_forget_rotates_session(self, client):
        with patch("bot.services.hermes_client.requests.post",
                   return_value=_resp(json_data=_completion("a"))):
            client.ask("pm:1", "first")
        first_id = client.get_session("pm:1")
        assert first_id is not None
        assert client.forget_session("pm:1") is True
        assert client.get_session("pm:1") is None
        # next turn mints a fresh id (preamble again) different from the old one
        with patch("bot.services.hermes_client.requests.post",
                   return_value=_resp(json_data=_completion("b"))) as mp:
            client.ask("pm:1", "second")
        new_id = mp.call_args.kwargs["headers"]["X-Hermes-Session-Id"]
        assert new_id != first_id
        assert SYSTEM_PREAMBLE in mp.call_args.kwargs["json"]["messages"][0]["content"]

    def test_forget_unknown_returns_false(self, client):
        assert client.forget_session("nope") is False


class TestPing:
    def test_ping_ok(self, client):
        r = _resp(json_data={"data": [{"id": "hermes-agent"}]})
        r.raise_for_status = Mock()
        with patch("bot.services.hermes_client.requests.get", return_value=r):
            out = client.ping()
        assert out["status"] == "ok"

    def test_ping_down_raises_unavailable(self, client):
        with patch("bot.services.hermes_client.requests.get",
                   side_effect=requests.exceptions.ConnectionError("down")):
            with pytest.raises(AgentUnavailable):
                client.ping()

    def test_ping_not_configured(self, temp_db):
        with pytest.raises(AgentUnavailable):
            HermesAgentClient("", "k", temp_db).ping()
