"""/list — the operator surface of the FlClash rule lists
(IMPROVEMENT_PLAN E1/E2/E9, ``bot/services/rule_lists.py``).

  /list                          every list with its size, the complaints
                                 queue, the auto threshold
  /list show <list>              the entries (who added, when)
  /list add <list> <entry>…      domain(s) or CIDR(s); one admin_actions row each
  /list rm <list> <entry>…       removing from a VPN list also keeps the
                                 auto rule off that domain for 30 days
  /list auto [N|off]             E9 threshold: N distinct users / 24 h

Pure app_settings reads/writes on the polling thread. Answers in the
topic the command came from (AdminHandlerBase._send).
"""

import html
import logging
from datetime import datetime
from typing import List

from bot.services import rule_lists as rl
from .base import AdminHandlerBase

logger = logging.getLogger(__name__)

_CAP = 3900     # Telegram's limit is 4096


def _fmt_ts(iso, now: datetime) -> str:
    s = str(iso or '')
    if len(s) < 16:
        return s or '?'
    hhmm = s[11:16]
    if s[:10] == now.strftime('%Y-%m-%d'):
        return f"{hhmm} UTC"
    return f"{s[8:10]}.{s[5:7]} {hhmm} UTC"


def _who(meta: dict) -> str:
    by = str((meta or {}).get('by') or '?')
    return 'авто' if by == rl.AUTO_ACTOR else f'админ {by}'


def _cap(text: str) -> str:
    """Cut on a line boundary: every line closes the tags it opens, a
    cut inside ``<code>…`` makes Telegram reject the whole message."""
    if len(text) <= _CAP:
        return text
    cut = text.rfind('\n', 0, _CAP)
    return text[:cut if cut > 0 else _CAP] + "\n…(обрезано)"


class AdminListsMixin(AdminHandlerBase):
    """/list — rule lists for the Clash profile + the complaints queue."""

    LIST_USAGE = (
        "📋 <b>/list</b> — списки правил FlClash (rule-providers)\n"
        "• <code>/list</code> — все списки, размеры, жалобы, порог авто\n"
        "• <code>/list show &lt;список&gt;</code> — записи\n"
        "• <code>/list add &lt;список&gt; &lt;домен|CIDR&gt; …</code> — добавить\n"
        "• <code>/list rm &lt;список&gt; &lt;домен|CIDR&gt; …</code> — убрать\n"
        "• <code>/list auto N</code> / <code>off</code> — авто в blocked-recent "
        "при N разных юзерах за 24 ч\n"
        "списки: " + ", ".join(f"<code>{n}</code>" for n in rl.LIST_NAMES)
    )

    def show_rule_lists(self, chat_id: str, args: list) -> None:
        sub = (args[0] if args else '').strip().lower()
        rest = list(args[1:])
        try:
            if not sub:
                text = self._lists_overview()
            elif sub in ('show', 'ls'):
                text = self._list_show(rest)
            elif sub == 'add':
                text = self._list_change('add', rest)
            elif sub in ('rm', 'del', 'remove'):
                text = self._list_change('rm', rest)
            elif sub == 'auto':
                text = self._list_auto(rest)
            elif sub in rl.LISTS:
                text = self._list_show([sub])
            else:
                text = self.LIST_USAGE
        except Exception as e:      # must always answer
            logger.exception("/list failed")
            text = f"❌ /list: {html.escape(str(e))[:200]}"
        self._send(chat_id=chat_id, text=_cap(text), parse_mode='HTML')

    # ----- views -----

    def _lists_overview(self) -> str:
        now = rl.utcnow()
        lists = rl.load_lists(self.db)
        lines = ["📋 <b>Списки правил FlClash</b> (rule-providers, клиент обновляет раз в час)"]
        health = rl.lists_health(self.db)
        if health:
            lines.append(f"⚠️ rule_lists {html.escape(health)} — клиенты получают 503 и "
                         f"держат прошлую версию; правка через /list add|rm начнёт "
                         f"списки заново (старое — в {rl.BACKUP_KEY})")
        for name, spec in rl.LISTS.items():
            entries = lists[name]
            auto = sum(1 for m in entries.values() if _who(m) == 'авто')
            tail = f" (авто: {auto})" if auto else ""
            lines.append(f"• <b>{name}</b> — {html.escape(spec.title)}: {len(entries)}{tail}")
        base = getattr(self.config, 'WEBAPP_URL', '') or ''
        if isinstance(base, str) and base.strip():
            lines.append("раздаются: " + html.escape(base.strip().rstrip('/')
                         + rl.LIST_URL_PATH.format(name='<список>')))
        else:
            lines.append("⚠️ WEBAPP_URL не задан — профиль FlClash на списки не ссылается")
        lines.append("<i>Hiddify правила профиля не применяет — списки работают только во FlClash</i>")
        lines.append("")
        lines.extend(self._queue_lines(now))
        lines.append("")
        lines.append("<i>/list show &lt;список&gt; · /list add|rm &lt;список&gt; &lt;домен|CIDR&gt; · "
                     "/list auto N|off</i>")
        return "\n".join(lines)

    def _queue_lines(self, now: datetime) -> List[str]:
        queue = rl.load_queue(self.db)
        groups = rl.pending_summary(queue, now)
        out = [f"<b>Жалобы «сайт не открывается»</b> — ждут решения: {len(groups)}"]
        for g in groups[:10]:
            n = len(g['users'])
            out.append(f"• <code>{html.escape(g['domain'])}</code> — {n} юз., "
                       f"последняя {_fmt_ts(g['last_ts'], now)}")
        if len(groups) > 10:
            out.append(f"…и ещё {len(groups) - 10}")
        threshold = rl.auto_threshold(self.db)
        if threshold:
            out.append(f"авто в blocked-recent: {threshold}+ разных юзеров за "
                       f"{rl.AUTO_WINDOW_HOURS} ч (RU-зона, ru-direct и игнор — никогда)")
        else:
            out.append("авто в blocked-recent: выключено (/list auto 2 — включить)")
        if queue['ignored']:
            names = sorted(queue['ignored'])
            shown = ", ".join(html.escape(d) for d in names[:10])
            more = f" …+{len(names) - 10}" if len(names) > 10 else ""
            out.append(f"игнор (авто не добавит {rl.IGNORE_DAYS} дн.): {shown}{more}")
        return out

    def _list_show(self, rest: list) -> str:
        name = (rest[0] if rest else '').strip().lower()
        if name not in rl.LISTS:
            return self.LIST_USAGE
        now = rl.utcnow()
        entries = rl.load_lists(self.db)[name]
        spec = rl.LISTS[name]
        lines = [f"📋 <b>{name}</b> — {html.escape(spec.title)} · {len(entries)}"]
        if not entries:
            lines.append("<i>пусто</i>")
        for i, (entry, meta) in enumerate(entries.items(), 1):
            line = (f"{i}. <code>{html.escape(entry)}</code> — {html.escape(_who(meta))}, "
                    f"{_fmt_ts(meta.get('ts'), now)}")
            if meta.get('note'):
                line += f" · {html.escape(str(meta['note'])[:80])}"
            lines.append(line)
        lines.append("")
        lines.append(f"<i>/list add {name} &lt;…&gt; · /list rm {name} &lt;…&gt;</i>")
        return "\n".join(lines)

    # ----- changes -----

    def _list_change(self, op: str, rest: list) -> str:
        if len(rest) < 2:
            return self.LIST_USAGE
        name = rest[0].strip().lower()
        if name not in rl.LISTS:
            return (f"❌ нет такого списка: <code>{html.escape(name)}</code>\n"
                    + "списки: " + ", ".join(f"<code>{n}</code>" for n in rl.LIST_NAMES))
        actor = self._list_actor()
        out = []
        for raw in rest[1:21]:          # a sane cap per command
            if op == 'add':
                change = rl.add_entry(self.db, name, raw, actor=actor, note='/list add')
            else:
                change = rl.remove_entry(self.db, name, raw, actor=actor, note='/list rm')
            out.append(self._change_line(change, raw))
        if any(line.startswith(('✅', '🗑')) for line in out):
            out.append("<i>FlClash подтянет изменения в течение часа</i>")
        return "\n".join(out)

    @staticmethod
    def _change_line(ch: 'rl.ListChange', raw: str) -> str:
        entry = html.escape(ch.entry or raw)
        name = ch.name
        if ch.status == 'added':
            line = f"✅ <b>{name}</b>: + <code>{entry}</code>"
        elif ch.status == 'removed':
            line = f"🗑 <b>{name}</b>: − <code>{entry}</code>"
            if name in rl.VPN_LISTS and rl.LISTS[name].behavior == 'domain':
                line += f" · авто не вернёт его {rl.IGNORE_DAYS} дн."
        elif ch.status == 'exists':
            line = f"ℹ️ <code>{entry}</code> уже в {name}"
        elif ch.status == 'covered':
            line = (f"ℹ️ <code>{entry}</code> уже покрыт "
                    f"<code>{html.escape(ch.detail)}</code> в {name}")
        elif ch.status == 'absent':
            line = f"ℹ️ <code>{entry}</code> нет в {name}"
            if ch.detail:
                line += f" (покрыт <code>{html.escape(ch.detail)}</code> — убирай его)"
        else:
            line = f"❌ <code>{html.escape(raw[:80])}</code>: {html.escape(ch.detail or ch.status)}"
        if ch.warning:
            line += f"\n⚠️ {html.escape(ch.warning)}"
            if ch.status == 'added' and rl.LISTS[name].target == 'DIRECT':
                line += " — VPN-списки стоят выше, эта запись не сработает"
        return line

    def _list_auto(self, rest: list) -> str:
        current = rl.auto_threshold(self.db)
        if not rest:
            state = (f"{current}+ разных юзеров за {rl.AUTO_WINDOW_HOURS} ч"
                     if current else "выключено")
            return (f"🤖 <b>Авто в blocked-recent</b>: {state}\n"
                    f"<i>/list auto N — порог · /list auto off — выключить</i>")
        arg = rest[0].strip().lower()
        if arg in ('off', '0'):
            value = 0
        elif arg.isdigit() and 1 <= int(arg) <= 100:
            value = int(arg)
        else:
            return "❌ порог — число 1…100 или off"
        if not rl.set_auto_threshold(self.db, value, actor=self._list_actor()):
            return "❌ не удалось записать порог в app_settings"
        if value:
            return (f"🤖 <b>Авто в blocked-recent</b>: {value}+ разных юзеров за "
                    f"{rl.AUTO_WINDOW_HOURS} ч (было: {current or 'выкл'})")
        return f"⏸ <b>Авто в blocked-recent выключено</b> (было: {current or 'выкл'})"

    def _list_actor(self) -> str:
        """The admin who typed the command, for the audit row."""
        upd = getattr(self, '_current_update', None) or {}
        try:
            uid = self._get_user_id(upd)
        except Exception:
            uid = None
        return str(uid or getattr(self.config, 'SUPER_ADMIN_ID', '') or 'admin')
