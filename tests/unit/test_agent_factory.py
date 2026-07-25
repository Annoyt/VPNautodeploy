"""Unit tests for bot/services/agent_factory.py (backend selection)."""

import os
import tempfile

import pytest

from bot.services.agent_factory import (
    build_agent_client,
    get_agent_url,
    get_agent_backend,
)
from bot.services.agent_client import AgentClient
from bot.services.hermes_client import HermesAgentClient


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


class _Cfg:
    """Minimal config stand-in with all attrs the factory reads."""
    AGENT_BACKEND = "opencode"
    OPENCODE_URL = "http://host:4096"
    OPENCODE_SERVER_PASSWORD = "pw"
    OPENCODE_USERNAME = "opencode"
    OPENCODE_DEFAULT_MODEL = ""
    OPENCODE_AGENT_DEFAULT = ""
    OPENCODE_AGENT_PLAN = ""
    OPENCODE_AGENT_YOLO = ""
    HERMES_URL = "http://host:4097"
    HERMES_API_KEY = "secret"
    HERMES_MODEL = "hermes-agent"
    AGENT_NODE_TYPE = "control"
    ENTRY_NODE_SSHFS_MOUNT = "/mnt/entry_node"
    DB_PATH = ":memory:"


class TestBackendSelection:
    def test_default_is_opencode(self):
        class C:
            pass
        assert get_agent_backend(C()) == "opencode"

    def test_hermes_selected(self, temp_db):
        cfg = _Cfg(); cfg.AGENT_BACKEND = "hermes"
        assert get_agent_backend(cfg) == "hermes"
        assert get_agent_url(cfg) == "http://host:4097"
        client = build_agent_client(cfg, temp_db)
        assert isinstance(client, HermesAgentClient)
        assert client.api_key == "secret"
        assert client.default_model == "hermes-agent"

    def test_opencode_selected(self, temp_db):
        cfg = _Cfg(); cfg.AGENT_BACKEND = "opencode"
        assert get_agent_backend(cfg) == "opencode"
        assert get_agent_url(cfg) == "http://host:4096"
        client = build_agent_client(cfg, temp_db)
        assert isinstance(client, AgentClient)

    def test_case_insensitive_and_whitespace(self, temp_db):
        cfg = _Cfg(); cfg.AGENT_BACKEND = "  HERMES  "
        assert get_agent_backend(cfg) == "hermes"
        assert isinstance(build_agent_client(cfg, temp_db), HermesAgentClient)

    def test_timeout_passthrough(self, temp_db):
        cfg = _Cfg(); cfg.AGENT_BACKEND = "hermes"
        client = build_agent_client(cfg, temp_db, default_timeout=42)
        assert client.default_timeout == 42

    def test_empty_url_when_hermes_unset(self):
        class C:
            AGENT_BACKEND = "hermes"
            HERMES_URL = ""
        assert get_agent_url(C()) == ""
