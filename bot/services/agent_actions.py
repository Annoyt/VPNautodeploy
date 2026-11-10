"""The agent proposes, the admin presses (IMPROVEMENT_PLAN F, D2).

The protocol agent answers an alert with ИТОГ / ПОДОЗРЕВАЕМЫЙ / СЛЕДУЮЩАЯ
КОМАНДА and, since 2026-11, one more line: ``ДЕЙСТВИЕ: <one of a fixed
list>``. The bot turns that line into a button under the diagnosis; the
button runs ordinary code — never anything the model wrote. The model
decides nothing on its own: the one on duty on 2026-08-30 proposed an
ALTER TABLE on prod, and ``hermes/AGENTS.md`` forbids it to change
anything without the admin's OK.

The list is short on purpose:

* ``restart_xray`` — the panel's xray restart on exit
  (``XUIAPIClient.restart_xray``): the fix for the hot-apply drifts the
  healthcheck names (Shadowsocks 75 vs 84 users on 2026-10-30);
* ``demote <proto>`` — a hand demotion for 6 h, everyone
  (``cascade_manual``, C6): what the operator would type as
  ``/cascade demote <proto>``.

A button lives ``BUTTON_TTL_S``: older presses are refused, an advice
about an outage two hours ago is not an advice about now.
"""

import logging
import re
import time
from datetime import timedelta
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

CALLBACK_PREFIX = 'agent_act:'
BUTTON_TTL_S = 2 * 3600
DEMOTE_TTL = timedelta(hours=6)
_ACTION_RE = re.compile(r'^\s*ДЕЙСТВИЕ\s*:\s*(.+?)\s*$', re.M | re.I)

# The line the prompt asks for (alert_manager._protocol_agent_prompt).
PROMPT_LINE = (
    "ДЕЙСТВИЕ: одно из «restart_xray», «demote <протокол>», «нет» — что "
    "предложить админу кнопкой; сам ты его не выполняешь."
)


def _protocols() -> set:
    try:
        from bot.handlers.callbacks.user import MyKeyAnswerHandler
        return set(MyKeyAnswerHandler.PROTOCOL_METHOD_MAP)
    except Exception:
        return {'reality', 'hy2', 'hy2t', 'ws', 'stls'}


def parse(reply: Optional[str]) -> Optional[Tuple[str, Optional[str]]]:
    """``(action, arg)`` named on the reply's ДЕЙСТВИЕ line, or None when
    there is none, it says «нет», or it names anything off the list."""
    m = _ACTION_RE.search(reply or '')
    if not m:
        return None
    words = re.sub(r'[«»"\'`]', ' ', m.group(1)).lower().split()
    if not words:
        return None
    if words[0] == 'restart_xray':
        return ('restart_xray', None)
    if words[0] == 'demote' and len(words) > 1 and words[1] in _protocols():
        return ('demote', words[1])
    return None


def label(action: str, arg: Optional[str]) -> str:
    if action == 'restart_xray':
        return 'Перезапустить xray на exit'
    if action == 'demote':
        return f'Понизить {arg} на 6 ч'
    return action


def keyboard(action: str, arg: Optional[str], now: Optional[float] = None) -> dict:
    """The inline button for a parsed action; the press time is checked
    against the stamp in its data (64 bytes is plenty)."""
    stamp = int(now if now is not None else time.time())
    data = f"{CALLBACK_PREFIX}{action}:{arg or '-'}:{stamp}"
    return {'inline_keyboard': [[{'text': f'▶️ {label(action, arg)}', 'callback_data': data}]]}


def parse_callback(data: str, now: Optional[float] = None):
    """``(action, arg)`` from a button's data, or an error text. The data
    comes from the client: everything is checked again here."""
    now = now if now is not None else time.time()
    parts = (data or '')[len(CALLBACK_PREFIX):].split(':')
    if len(parts) != 3:
        return 'кнопка повреждена'
    action, arg, stamp = parts
    try:
        age = now - int(stamp)
    except ValueError:
        return 'кнопка повреждена'
    if age < 0 or age > BUTTON_TTL_S:
        return 'кнопка устарела — запусти /protocols и реши заново'
    arg = None if arg == '-' else arg
    if action == 'restart_xray' and arg is None:
        return ('restart_xray', None)
    if action == 'demote' and arg in _protocols():
        return ('demote', arg)
    return 'неизвестное действие'


def execute(action: str, arg: Optional[str], *, db, config, xui, admin_id: str) -> str:
    """Run one whitelisted action; returns the line for the chat. Audited
    as ``agent_action_<action>``. Never raises."""
    try:
        if action == 'restart_xray':
            if xui is None:
                from bot.services.xui_service import XUIService
                xui = XUIService(config)
            ok = bool(xui._run_sync(xui.api.restart_xray()))
            result = ('✅ xray на exit перезапущен' if ok
                      else '❌ панель не подтвердила перезапуск — проверь /protocols')
        elif action == 'demote':
            from bot.services import cascade_manual
            entry = cascade_manual.demote(db, arg, None, DEMOTE_TTL, by=f'admin:{admin_id}')
            result = (f"✅ {arg} в конце каскада до {entry['until'][:16].replace('T', ' ')} UTC; "
                      f"снять — /cascade restore {arg}")
        else:
            return '❌ неизвестное действие'
    except Exception as e:
        logger.exception(f"agent action {action} failed")
        result = f'❌ не вышло: {str(e)[:150]}'
    try:
        db.log_admin_action(admin_id, f'agent_action_{action}', arg, result[:200])
    except Exception as e:
        logger.warning(f"agent action audit failed: {e}")
    return result
