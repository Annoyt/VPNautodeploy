"""Auto-create the forum topics the bot expects on first run.

The bot routes messages by TOPIC_* env vars (TOPIC_REQUESTS, TOPIC_AI, …).
On a brand-new deploy those vars are blank, so the bot has nowhere to
send things. This helper creates whatever is missing and writes the
assignment to two places:

  1. The bot's settings.* attributes in memory (so the running process
     immediately uses the new ids).
  2. A persistent bot_state table in bot.db, so the next restart can
     read them back without re-creating topics — and without needing
     someone to manually edit .env.

Requires:
  - MODE=GROUP and FORUM_GROUP_ID set
  - The bot is an admin in FORUM_GROUP_ID with "Manage Topics" permission
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Iterable, Optional, Tuple

logger = logging.getLogger(__name__)


# Settings attribute -> default topic name in Russian.
# Order matters only cosmetically (Telegram shows topics in creation order).
REQUIRED_TOPICS: Tuple[Tuple[str, str], ...] = (
    ("TOPIC_REQUESTS", "Заявки"),
    ("TOPIC_USERS",    "Пользователи"),
    ("TOPIC_DEMO",     "Демо"),
    ("TOPIC_REJECTED", "Отклонённые"),
    ("TOPIC_STATS",    "Статистика"),
    ("TOPIC_PAYMENTS", "Платежи"),
    ("TOPIC_SUPPORT",  "Поддержка"),
    ("TOPIC_SOLVED",   "Решённые"),
    ("TOPIC_AI",       "AI"),
)


def _ensure_table(db_path: str) -> None:
    with sqlite3.connect(db_path, timeout=10) as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_state (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
            )
            """
        )


def _read_state(db_path: str) -> Dict[str, str]:
    _ensure_table(db_path)
    with sqlite3.connect(db_path, timeout=10) as c:
        rows = c.execute("SELECT key, value FROM bot_state").fetchall()
    return {k: v for k, v in rows}


def _save_state(db_path: str, key: str, value: str) -> None:
    with sqlite3.connect(db_path, timeout=10) as c:
        c.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = strftime('%s','now')",
            (key, value),
        )


def bootstrap_forum_topics(bot, config) -> Optional[Dict[str, int]]:
    """Create missing forum topics + remember their ids.

    Returns a dict of {attr_name: thread_id} for everything we touched
    (created or restored from persistent state). Returns None if forum
    mode isn't on.
    """
    if not getattr(config, "FORUM_ENABLED", False):
        return None
    if not getattr(config, "FORUM_GROUP_ID", ""):
        logger.info("forum bootstrap: FORUM_GROUP_ID not set, skipping")
        return None

    db_path = config.DB_PATH
    state = _read_state(db_path)

    touched: Dict[str, int] = {}
    for attr, default_name in REQUIRED_TOPICS:
        current = getattr(config, attr, 0) or 0
        if current:
            # Already configured via env, nothing to do.
            continue

        persisted = state.get(attr)
        if persisted and persisted.isdigit():
            tid = int(persisted)
            setattr(config, attr, tid)
            touched[attr] = tid
            logger.info(f"forum bootstrap: restored {attr}={tid} from bot_state")
            continue

        # Need to create it.
        name = state.get(f"{attr}_NAME", default_name)
        logger.info(f"forum bootstrap: creating topic '{name}' for {attr}")
        try:
            tid = bot.create_forum_topic(
                chat_id=config.FORUM_GROUP_ID,
                name=name,
            )
        except Exception as e:
            logger.warning(f"forum bootstrap: createForumTopic failed for {name}: {e}")
            continue

        if not tid:
            logger.warning(
                f"forum bootstrap: createForumTopic returned no id for {name} — "
                "probably no 'Manage Topics' permission. Set the topic id by hand in .env."
            )
            continue

        setattr(config, attr, tid)
        _save_state(db_path, attr, str(tid))
        touched[attr] = tid
        logger.info(f"forum bootstrap: created {attr}={tid}")

    return touched
