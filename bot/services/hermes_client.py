"""Client for a Hermes Agent API server (`hermes gateway`, OpenAI-compatible).

Drop-in replacement for the OpenCode ``AgentClient``: same public surface
(``is_configured`` / ``ping`` / ``ask`` / ``get_session`` / ``forget_session``)
so the bot's ``/ai`` handlers and the factory don't care which backend is
running. Instead of OpenCode's two-step ``POST /session`` + ``POST
/session/{id}/message``, Hermes exposes a single OpenAI-compatible endpoint
``POST /v1/chat/completions`` that runs the full agent loop (tools, skills)
server-side and returns the final assistant text.

Per-conversation continuity is handled by the server via the
``X-Hermes-Session-Id`` header — we map the bot's ``session_key`` straight
onto it. ``/ai_reset`` rotates that id (persisted in the existing
``ai_sessions`` table) so the next turn starts a fresh Hermes session.

The model is selected in Hermes' own ``config.yaml`` (``model.default``);
the API exposes it under the alias ``"hermes-agent"``, which is what we send
as the request ``model`` field.
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from typing import Optional, Tuple

import requests

# Reuse the skill-routing + error types from the OpenCode client so both
# backends behave identically from the handler's point of view.
from bot.services.agent_client import (
    AgentError,
    AgentUnavailable,
    SYSTEM_PREAMBLE,
    _detect_skill_domains,
    _build_skill_reminder,
)

logger = logging.getLogger(__name__)

# The API server advertises the configured model under this single alias
# (GET /v1/models -> ["hermes-agent"]). The real provider/model lives in
# ~/.hermes/config.yaml. Sending the raw "openrouter/..." string is rejected.
HERMES_MODEL_ALIAS = "hermes-agent"


class HermesAgentClient:
    """Hermes-server HTTP client with SQLite-backed per-session-key memory."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        db_path: str,
        *,
        default_timeout: int = 300,
        default_model: Optional[str] = None,
        # Accepted for signature-compatibility with AgentClient; unused here.
        username: Optional[str] = None,
        agent_default: Optional[str] = None,
        agent_plan: Optional[str] = None,
        agent_yolo: Optional[str] = None,
        node_type: str = "control",
        sshfs_mount: str = "/mnt/entry_node",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self.db_path = db_path
        self.default_timeout = default_timeout
        self.default_model = default_model or HERMES_MODEL_ALIAS
        self.node_type = node_type
        self.sshfs_mount = sshfs_mount
        self._ensure_table()

    # ----- persistent session memory (ai_sessions table, reused as-is) -----

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def _ensure_table(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_sessions (
                    session_key   TEXT PRIMARY KEY,
                    kimi_session  TEXT NOT NULL,
                    created_at    INTEGER NOT NULL,
                    updated_at    INTEGER NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def get_session(self, session_key: str) -> Optional[str]:
        """Return the effective Hermes session id for this key, or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT kimi_session FROM ai_sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        return row[0] if row else None

    def remember_session(self, session_key: str, hermes_session_id: str) -> None:
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO ai_sessions (session_key, kimi_session, created_at, updated_at, message_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(session_key) DO UPDATE SET
                    kimi_session  = excluded.kimi_session,
                    updated_at    = excluded.updated_at,
                    message_count = ai_sessions.message_count + 1
                """,
                (session_key, hermes_session_id, now, now),
            )

    def forget_session(self, session_key: str) -> bool:
        """Rotate the Hermes session id so the next turn starts fresh.

        Hermes has no session-delete endpoint we rely on; instead we forget
        the mapping locally, and the next ``ask`` mints a brand-new
        ``X-Hermes-Session-Id`` (which Hermes treats as a new conversation).
        """
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM ai_sessions WHERE session_key = ?",
                (session_key,),
            )
            return cur.rowcount > 0

    # ----- HTTP plumbing -----

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self, hermes_session_id: Optional[str] = None) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if hermes_session_id:
            # Continuity (same conversation) + long-term memory scoping.
            h["X-Hermes-Session-Id"] = hermes_session_id
            h["X-Hermes-Session-Key"] = hermes_session_id
        return h

    def ping(self) -> dict:
        """GET /v1/models. Any 2xx means the API server is alive."""
        if not self.is_configured():
            raise AgentUnavailable("HERMES_URL not set")
        try:
            r = requests.get(
                f"{self.base_url}/v1/models",
                headers=self._headers(),
                timeout=5,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            raise AgentUnavailable(str(e)) from e
        return {"status": "ok", "version": "hermes"}

    def _selected_model(self) -> Optional[str]:
        """Admin-chosen free model from the /ai_model switcher, or None."""
        try:
            from bot.services.agent_models import get_selected_model
            return get_selected_model(self.db_path)
        except Exception:
            return None

    def _mint_session_id(self, session_key: str) -> str:
        # Unique per mint (uuid suffix) so a reset always yields a new id even
        # if the next turn arrives within the same second — otherwise Hermes
        # would continue the old conversation instead of starting fresh.
        return f"{session_key}:{uuid.uuid4().hex[:12]}"

    # ----- public API (mirrors AgentClient.ask) -----

    def ask(
        self,
        session_key: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        mode: Optional[str] = None,   # accepted for parity; Hermes has no mode agents
        yolo: bool = False,           # accepted for parity; approval mode is server-side
    ) -> Tuple[str, int]:
        """Send a prompt under session_key. Returns (reply, elapsed_ms).

        Timeout classification matches AgentClient: a read timeout means the
        agent outran the clock (session context is preserved) -> AgentError;
        a connect failure means the server is down -> AgentUnavailable.
        """
        if not self.is_configured():
            raise AgentUnavailable("HERMES_URL not set")

        eff_timeout = timeout or self.default_timeout
        # Explicit arg wins; otherwise the admin-selected free model (set via
        # /ai_model), else the configured default alias.
        eff_model = model or self._selected_model() or self.default_model or HERMES_MODEL_ALIAS

        # Fresh session? Prepend the operating preamble once (belt-and-braces
        # on top of the workspace AGENTS.md), then keep skill routing per-turn.
        hermes_session_id = self.get_session(session_key)
        is_fresh = hermes_session_id is None
        if is_fresh:
            hermes_session_id = self._mint_session_id(session_key)

        domains = _detect_skill_domains(prompt)
        if domains:
            prompt = prompt + _build_skill_reminder(domains)
        effective_prompt = (SYSTEM_PREAMBLE + prompt) if is_fresh else prompt

        body = {
            "model": eff_model,
            "messages": [{"role": "user", "content": effective_prompt}],
            "stream": False,
        }

        started = time.time()
        try:
            r = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json=body,
                headers=self._headers(hermes_session_id),
                timeout=eff_timeout + 15,
            )
        except requests.exceptions.ConnectTimeout as e:
            raise AgentUnavailable(str(e)) from e
        except requests.exceptions.Timeout as e:
            raise AgentError(f"agent turn timed out after {eff_timeout}s") from e
        except requests.RequestException as e:
            raise AgentUnavailable(str(e)) from e

        if r.status_code >= 400:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise AgentError(f"HTTP {r.status_code}: {str(detail)[:400]}")

        duration_ms = int((time.time() - started) * 1000)
        try:
            data = r.json()
        except ValueError as e:
            raise AgentError(f"bad JSON from hermes: {e}") from e

        reply = self._extract_text(data)
        # Some turns finish with finish_reason="error" and the error text in
        # content (e.g. an upstream provider 4xx) — surface it as AgentError.
        finish = (data.get("choices") or [{}])[0].get("finish_reason")
        if finish == "error" and reply:
            raise AgentError(reply[:400])

        self.remember_session(session_key, hermes_session_id)
        return reply, duration_ms

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Pull assistant text from an OpenAI-shaped chat completion."""
        for choice in data.get("choices") or []:
            msg = choice.get("message") or {}
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
            # Some servers return content as a list of parts.
            if isinstance(content, list):
                chunks = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("text")
                ]
                if chunks:
                    return "\n".join(chunks).strip()
        return ""
