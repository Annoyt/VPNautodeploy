"""Free-model registry + persisted selection for the Hermes ``/ai`` agent.

The Hermes API server accepts any OpenRouter model id in the request ``model``
field (verified), so the admin can switch the ``/ai`` model **per request** —
no host access, no config edit, no restart. The choice is persisted in the bot
DB (``agent_settings`` kv table) and read by ``HermesAgentClient`` on each turn.

Only free + tool-calling models belong here — the agent must be able to call
bash/edit tools, so non-tool models are useless regardless of how cheap.
"""

from __future__ import annotations

import sqlite3
from typing import List, Optional, Tuple

# Curated free + tool-calling OpenRouter models good for an ops agent.
# (model_id, human label). Order = display order in the switcher; the INDEX is
# used in callback_data, so only ever append/replace — don't reorder blindly.
FREE_MODELS: List[Tuple[str, str]] = [
    ("qwen/qwen3-coder:free", "Qwen3-Coder · код/ops ⭐"),
    ("openai/gpt-oss-120b:free", "GPT-OSS 120B · надёжный"),
    ("nvidia/nemotron-3-super-120b-a12b:free", "Nemotron Super 120B"),
    ("nvidia/nemotron-3-ultra-550b-a55b:free", "Nemotron Ultra 550B · умный"),
    ("qwen/qwen3-next-80b-a3b-instruct:free", "Qwen3-Next 80B"),
    ("google/gemma-4-31b-it:free", "Gemma 4 31B"),
]

# When nothing is selected, HermesAgentClient falls back to its default_model
# (the "hermes-agent" alias → Hermes config.yaml default), which is this model.
DEFAULT_MODEL_ID = "nvidia/nemotron-3-super-120b-a12b:free"

CALLBACK_PREFIX = "aimodel:"
_SETTINGS_KEY = "selected_model"


def label_for(model_id: str) -> str:
    for mid, label in FREE_MODELS:
        if mid == model_id:
            return label
    return model_id


def is_valid_model(model_id: str) -> bool:
    return any(mid == model_id for mid, _ in FREE_MODELS)


def resolve_arg(arg: str) -> Optional[str]:
    """Resolve a /ai_model argument (1-based number, exact id, or substring)."""
    arg = (arg or "").strip()
    if not arg:
        return None
    if arg.isdigit():
        idx = int(arg) - 1
        if 0 <= idx < len(FREE_MODELS):
            return FREE_MODELS[idx][0]
        return None
    low = arg.lower()
    # exact id first, then unique substring match on id or label
    for mid, _ in FREE_MODELS:
        if mid.lower() == low:
            return mid
    matches = [
        mid for mid, label in FREE_MODELS
        if low in mid.lower() or low in label.lower()
    ]
    return matches[0] if len(matches) == 1 else None


# ----- persistence (agent_settings kv table in bot.db) -----

def _ensure_table(db_path: str) -> None:
    with sqlite3.connect(db_path, timeout=10) as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS agent_settings "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )


def get_selected_model(db_path: str) -> Optional[str]:
    """Return the admin-selected model id, or None (→ use the default)."""
    try:
        _ensure_table(db_path)
        with sqlite3.connect(db_path, timeout=10) as c:
            row = c.execute(
                "SELECT value FROM agent_settings WHERE key = ?", (_SETTINGS_KEY,)
            ).fetchone()
        return row[0] if row and row[0] else None
    except sqlite3.Error:
        return None


def set_selected_model(db_path: str, model_id: str) -> None:
    _ensure_table(db_path)
    with sqlite3.connect(db_path, timeout=10) as c:
        c.execute(
            "INSERT INTO agent_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_SETTINGS_KEY, model_id),
        )


# ----- inline keyboard (shared by /ai_model and the callback handler) -----

def build_model_keyboard(current_id: Optional[str]) -> dict:
    """Inline keyboard: one button per model, ✓ on the active one."""
    effective = current_id or DEFAULT_MODEL_ID
    rows = []
    for i, (mid, label) in enumerate(FREE_MODELS):
        mark = "✓ " if mid == effective else ""
        rows.append([{"text": f"{mark}{label}", "callback_data": f"{CALLBACK_PREFIX}{i}"}])
    return {"inline_keyboard": rows}
