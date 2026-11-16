"""How an admin sees a user: one rule for the bot's messages and the dashboard.

A user added by email (/addmail, a mail request) has no Telegram username, a
synthetic chat_id (``ext_1a2b3c4d``) and a synthetic panel email
(``user_ext_1a2b3c4d@nekovo.ru``): none of them says who it is. Their real
address is ``contact_email``, so a user without a username is shown by it.
``userLabel`` in ``bot/webapp/app.js`` follows the same rule.
"""
import html as _html
from typing import Any, Optional


def user_label(user: Any = None, *, username: Optional[str] = None,
               chat_id: Any = None, contact_email: Optional[str] = None,
               fallback: Optional[str] = None, html: bool = True) -> str:
    """``@username``; else ``✉️ <contact email>``; else ``fallback``
    (``user_<chat_id>`` when not given).

    Takes a user (anything with ``username``, ``chat_id`` and
    ``contact_email``) or those three fields. With ``html`` (the default)
    the result is escaped for Telegram's HTML parse mode; pass
    ``html=False`` for plain text: a message sent without a parse mode, a
    topic name, an alert title, JSON, an agent prompt.
    """
    if user is not None:
        username = getattr(user, 'username', None)
        chat_id = getattr(user, 'chat_id', None)
        contact_email = getattr(user, 'contact_email', None)
    if username:
        label = f"@{username}"
    elif isinstance(contact_email, str) and contact_email.strip():
        label = f"✉️ {contact_email.strip()}"
    elif fallback is not None:
        label = fallback
    else:
        label = f"user_{chat_id}"
    return _html.escape(label, quote=False) if html else label
