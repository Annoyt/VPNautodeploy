"""Pick the /ai agent backend (Hermes or OpenCode) from config.

One place decides which client the bot talks to, so the five call sites
(ai_handler, alert_manager x2, notifications, admin/ops) don't each branch.
Both clients expose the same surface (``is_configured`` / ``ping`` / ``ask`` /
``get_session`` / ``forget_session``), so callers are backend-agnostic.

``AGENT_BACKEND=hermes`` → ``HermesAgentClient`` (HERMES_URL/HERMES_API_KEY);
anything else → the legacy ``AgentClient`` (OPENCODE_URL/…).
"""

from __future__ import annotations

from typing import Optional


def get_agent_backend(config) -> str:
    """Normalized backend name: 'hermes' or 'opencode'."""
    return (getattr(config, "AGENT_BACKEND", "") or "opencode").strip().lower()


def get_agent_url(config) -> str:
    """URL of the configured backend, or '' when /ai is disabled.

    Call sites use this for the "is the agent configured?" gate instead of
    reaching for OPENCODE_URL directly.
    """
    if get_agent_backend(config) == "hermes":
        return getattr(config, "HERMES_URL", "") or ""
    return getattr(config, "OPENCODE_URL", "") or ""


def build_agent_client(config, db_path: str, *, default_timeout: int = 300):
    """Construct the agent client for the active backend."""
    if get_agent_backend(config) == "hermes":
        from bot.services.hermes_client import HermesAgentClient
        return HermesAgentClient(
            getattr(config, "HERMES_URL", "") or "",
            getattr(config, "HERMES_API_KEY", "") or "",
            db_path,
            default_timeout=default_timeout,
            default_model=getattr(config, "HERMES_MODEL", "") or None,
            node_type=getattr(config, "AGENT_NODE_TYPE", "control"),
            sshfs_mount=getattr(config, "ENTRY_NODE_SSHFS_MOUNT", "/mnt/entry_node"),
        )

    from bot.services.agent_client import AgentClient
    return AgentClient(
        getattr(config, "OPENCODE_URL", "") or "",
        getattr(config, "OPENCODE_SERVER_PASSWORD", "") or "",
        db_path,
        default_timeout=default_timeout,
        username=getattr(config, "OPENCODE_USERNAME", "opencode"),
        default_model=getattr(config, "OPENCODE_DEFAULT_MODEL", "") or None,
        agent_default=getattr(config, "OPENCODE_AGENT_DEFAULT", "") or None,
        agent_plan=getattr(config, "OPENCODE_AGENT_PLAN", "") or None,
        agent_yolo=getattr(config, "OPENCODE_AGENT_YOLO", "") or None,
        node_type=getattr(config, "AGENT_NODE_TYPE", "control"),
        sshfs_mount=getattr(config, "ENTRY_NODE_SSHFS_MOUNT", "/mnt/entry_node"),
    )
