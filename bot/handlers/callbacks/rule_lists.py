"""«📝 Другой сайт» — a user's "this site does not open" becomes a rule
(IMPROVEMENT_PLAN E2 + E9, storage in ``bot/services/rule_lists.py``).

  user taps 📝 in the 🆘 picker  → ReportSiteHandler arms PENDING_SITE
  the next plain-text message    → SiteReportFlow.consume_pending: host
                                   names extracted, each queued in
                                   ``rule_list_queue`` (domain, chat_id, ts)
  ≥N distinct users / 24 h       → E9 adds it to blocked-recent itself and
                                   tells the topic how to undo it
  otherwise                      → a card in the support topic with
                                   «➕ в blocked-recent» / «✖ игнор»
                                   (RuleListQueueHandler, admin-only)

Complaints about a named site are kept out of ``user_failure_reports``
on purpose: DPIMonitor's R5 reads that table as "a protocol is failing
for this ASN", and a single blocked site says nothing about protocols.
"""

import html
import logging
import time

from bot.handlers.callbacks.base import BaseCallbackHandler
from bot.services import rule_lists as rl
from bot.utils.exceptions import PermissionDeniedError

logger = logging.getLogger(__name__)

REPORT_SITE_CALLBACK = 'report_site'
QUEUE_CALLBACK_PREFIX = 'rlq:'
MAX_DOMAINS_PER_MESSAGE = 3


def site_button(lang: str) -> dict:
    """The «📝 Другой сайт» row of the 🆘 picker."""
    text = '📝 Другой сайт' if lang == 'ru' else '📝 Another website'
    return {'text': text, 'callback_data': REPORT_SITE_CALLBACK}


def _lang(user) -> str:
    return (getattr(user, 'lang', None) or 'ru') if user else 'ru'


_USER_LINES = {
    'queued': ('✅ <code>{d}</code> — передали: посмотрим и, если сайт заблокирован, '
               'пустим его через VPN.',
               '✅ <code>{d}</code> — sent: we will check it and route it through '
               'the VPN if it is blocked.'),
    'auto': ('✅ <code>{d}</code> — пускаем через VPN: на него уже жаловались.',
             '✅ <code>{d}</code> — now routed through the VPN: others reported it too.'),
    'duplicate': ('ℹ️ <code>{d}</code> — ты уже сообщал, мы в курсе.',
                  'ℹ️ <code>{d}</code> — you already reported it, we are on it.'),
    'limit': ('⏳ <code>{d}</code> — на сегодня хватит, спасибо! Остальное посмотрим.',
              '⏳ <code>{d}</code> — that is enough for today, thanks!'),
    'error': ('⚠️ <code>{d}</code> — не получилось сохранить, попробуй позже.',
              '⚠️ <code>{d}</code> — could not save it, please try later.'),
}


class SiteReportFlow:
    """The text half of the flow — MessageHandler hands it the message
    that follows the 📝 tap. Telegram I/O only; data and decisions live
    in ``bot.services.rule_lists``."""

    def __init__(self, bot, db, config):
        self.bot = bot
        self.db = db
        self.config = config

    def consume_pending(self, chat_id: str, text: str, user) -> bool:
        """True if the message was the answer to the 📝 prompt (handled,
        valid or not). An answer with no host name keeps the prompt open
        for a retry within the TTL, like the email prompt."""
        from bot.handlers.messages import PENDING_SITE, PENDING_SITE_TTL
        ts = PENDING_SITE.get(chat_id)
        if ts is None:
            return False
        if time.time() - ts > PENDING_SITE_TTL:
            PENDING_SITE.pop(chat_id, None)
            return False
        lang = _lang(user)
        domains = rl.extract_domains(text, limit=MAX_DOMAINS_PER_MESSAGE)
        if not domains:
            msg = ("🤔 Не нашёл адрес сайта. Пришли его целиком — например "
                   "<code>rutracker.org</code> — или просто ссылку."
                   if lang == 'ru' else
                   "🤔 No website address found. Send it in full — e.g. "
                   "<code>rutracker.org</code> — or just the link.")
            self.bot.send_message(chat_id=chat_id, text=msg, parse_mode='HTML')
            return True
        PENDING_SITE.pop(chat_id, None)
        results = [(d, self.submit(d, chat_id, user)) for d in domains]
        idx = 0 if lang == 'ru' else 1
        lines = [_USER_LINES.get(st, _USER_LINES['error'])[idx].format(d=html.escape(d))
                 for d, st in results]
        if any(st in ('queued', 'auto') for _, st in results):
            lines.append("")
            lines.append(
                "Во FlClash такие правила применяются сами в течение часа. "
                "Hiddify их не применяет — если сайт нужен срочно, попробуй FlClash (/sub)."
                if lang == 'ru' else
                "FlClash applies such rules by itself within an hour. Hiddify "
                "ignores them — if you need the site now, try FlClash (/sub).")
        self.bot.send_message(chat_id=chat_id, text="\n".join(lines), parse_mode='HTML')
        return True

    def submit(self, domain: str, chat_id: str, user) -> str:
        """Queue one complaint; let E9 decide; tell the topic. Returns
        queued | auto | duplicate | limit | error | invalid."""
        status, item = rl.record_complaint(self.db, domain, chat_id)
        if status != 'queued':
            return status
        try:
            added = rl.run_auto(self.db, only={item['domain']})
        except Exception:
            logger.exception("rule_lists: auto decision failed")
            added = []
        try:
            if added:
                self._notify_auto(added[0])
            else:
                self._post_card(item, user)
        except Exception:
            logger.exception("rule_lists: topic message failed")
        logger.info(f"site report: chat={chat_id} domain={item['domain']} "
                    f"{'auto-added' if added else 'queued'} #{item['id']}")
        return 'auto' if added else 'queued'

    # ----- the operator side -----

    def _post_card(self, item: dict, user) -> None:
        now = rl.utcnow()
        queue = rl.load_queue(self.db)
        lists = rl.load_lists(self.db)
        group = next((g for g in rl.pending_summary(queue, now)
                      if g['domain'] == item['domain']), None)
        n_window = len(group['users_window']) if group else 1
        threshold = rl.auto_threshold(self.db)
        guard = rl.auto_guard(item['domain'], lists, queue['ignored'], now)
        uname = getattr(user, 'username', None)
        who = f"@{html.escape(uname)}" if uname else "без username"
        where = " ".join(html.escape(str(v)) for v in (
            getattr(user, 'last_asn', None), getattr(user, 'last_country', None)) if v)
        lines = [
            f"📝 <b>Сайт не открывается</b> · жалоба #{item['id']}",
            f"Сайт: <code>{html.escape(item['domain'])}</code>",
            f"От: {who} (<code>{html.escape(str(item['chat_id']))}</code>)"
            + (f" · {where}" if where else ""),
            f"Разных юзеров за {rl.AUTO_WINDOW_HOURS} ч: {n_window}"
            + (f" (авто при {threshold})" if threshold else " (авто выключено)"),
        ]
        if guard:
            lines.append(f"⚠️ авто не добавит: {html.escape(guard)}")
        keyboard = {'inline_keyboard': [[
            {'text': f'➕ в {rl.AUTO_LIST}',
             'callback_data': f"{QUEUE_CALLBACK_PREFIX}add:{item['id']}"},
            {'text': '✖ игнор',
             'callback_data': f"{QUEUE_CALLBACK_PREFIX}ign:{item['id']}"},
        ]]}
        self._send_admin("\n".join(lines), reply_markup=keyboard)

    def _notify_auto(self, add: 'rl.AutoAdd') -> None:
        d = html.escape(add.domain)
        self._send_admin("\n".join([
            f"🤖 <b>{rl.AUTO_LIST}: + <code>{d}</code></b> (авто)",
            f"причина: {html.escape(add.evidence)}",
            "FlClash подтянет список в течение часа (Hiddify правила профиля не применяет)",
            f"откатить: <code>/list rm {rl.AUTO_LIST} {d}</code>",
        ]))

    def _send_admin(self, text: str, reply_markup=None) -> None:
        """The forum's support topic; the admin's PM only without a
        configured group (feedback_no_admin_pm_when_group)."""
        kwargs = {'text': text, 'parse_mode': 'HTML'}
        if reply_markup:
            kwargs['reply_markup'] = reply_markup
        group = getattr(self.config, 'FORUM_GROUP_ID', None)
        if getattr(self.config, 'FORUM_ENABLED', False) and group:
            topic = getattr(self.config, 'TOPIC_SUPPORT', None)
            if isinstance(topic, int) and topic:
                kwargs['message_thread_id'] = topic
            self.bot.send_message(chat_id=group, **kwargs)
        else:
            self.bot.send_message(chat_id=str(self.config.SUPER_ADMIN_ID), **kwargs)


class ReportSiteHandler(BaseCallbackHandler):
    """«📝 Другой сайт» in the 🆘 picker: ask for the address and arm
    the pending prompt (keyed by the PRESSER — in a group chat_id is
    the group). Only key holders: a crafted callback must not let an
    outsider feed the queue."""

    def can_handle(self, callback_data: str) -> bool:
        return callback_data == REPORT_SITE_CALLBACK

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        from bot.handlers.messages import PENDING_EMAIL, PENDING_SITE
        presser = str(user_id or chat_id)
        user = self.db.get_user(presser) or self.db.get_user(chat_id)
        lang = _lang(user)
        cb_msg = (update.get('callback_query') or {}).get('message') or {}
        thread_id = cb_msg.get('message_thread_id')
        if not user or not getattr(user, 'uuid', None):
            text = ("⚠️ Сначала получите ключ — /start" if lang == 'ru'
                    else "⚠️ Get a key first — /start")
            self.bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id)
            return
        PENDING_SITE[presser] = time.time()
        # one prompt at a time: the next message answers the latest tap
        PENDING_EMAIL.pop(presser, None)
        text = ("📝 <b>Какой сайт не открывается?</b>\n\n"
                "Пришли адрес ответным сообщением — например <code>rutracker.org</code> "
                "или ссылку. Проверим и, если сайт заблокирован, пустим его через VPN."
                if lang == 'ru' else
                "📝 <b>Which website does not open?</b>\n\n"
                "Reply with its address — e.g. <code>rutracker.org</code> — or a link. "
                "We will check it and route it through the VPN if it is blocked.")
        self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML',
                              message_thread_id=thread_id)


class RuleListQueueHandler(BaseCallbackHandler):
    """«➕ в blocked-recent» / «✖ игнор» on a complaint card
    (``rlq:add:<id>`` / ``rlq:ign:<id>``). Admin-only: callback data is
    client-supplied, anyone can send ``rlq:add:1`` from their own chat
    with the bot. The decision covers the DOMAIN (all its pending
    complaints), the card is stamped in place."""

    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(QUEUE_CALLBACK_PREFIX)

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        try:
            self.validator.validate_admin(user_id)
        except PermissionDeniedError:
            logger.warning(f"rlq: non-admin {user_id} pressed {kwargs.get('data')!r}")
            self.bot.send_message(chat_id=user_id, text="❌ No permission.")
            return
        parts = (kwargs.get('data') or '').split(':')
        if len(parts) != 3 or parts[1] not in ('add', 'ign') or not parts[2].isdigit():
            return
        action, item_id = parts[1], int(parts[2])
        message = (update.get('callback_query') or {}).get('message') or {}
        actor = str(user_id)
        item = rl.find_item(self.db, item_id)
        if item is None:
            self._stamp(message, chat_id,
                        f"⚠️ жалоба #{item_id} не найдена (старше "
                        f"{rl.QUEUE_RETENTION_DAYS} дн.?) — вручную: "
                        f"/list add {rl.AUTO_LIST} &lt;домен&gt;")
            return
        domain = item['domain']
        d = html.escape(domain)
        if action == 'add':
            ch = rl.add_entry(self.db, rl.AUTO_LIST, domain, actor=actor,
                              note=f'жалоба #{item_id}')
            if ch.status == 'added':
                line = (f"✅ добавлен в {rl.AUTO_LIST} (админ {actor}) · откатить: "
                        f"/list rm {rl.AUTO_LIST} {d}")
            elif ch.status in ('exists', 'covered'):
                rl.settle_domain(self.db, domain, 'added', by=actor)
                cover = f" (покрыт {html.escape(ch.detail)})" if ch.status == 'covered' else ""
                line = f"ℹ️ уже в {rl.AUTO_LIST}{cover}"
            else:
                line = f"❌ не добавлен: {html.escape(ch.detail or ch.status)}"
        else:
            rl.ignore_domain(self.db, domain, actor=actor, note=f'жалоба #{item_id}')
            line = (f"✖ проигнорирован (админ {actor}) — авто не добавит "
                    f"{rl.IGNORE_DAYS} дн.")
        self._stamp(message, chat_id, line)

    def _stamp(self, message: dict, chat_id: str, line: str) -> None:
        """Card text + the decision, buttons gone — the card is the
        record; a separate reply would just pile up in the topic."""
        msg_id = message.get('message_id')
        src_chat = (message.get('chat') or {}).get('id', chat_id)
        if not msg_id:
            self.bot.send_message(chat_id=chat_id, text=line, parse_mode='HTML',
                                  message_thread_id=message.get('message_thread_id'))
            return
        try:
            self.bot.edit_message_text(
                chat_id=str(src_chat), message_id=msg_id,
                text=f"{html.escape(message.get('text') or '')}\n\n{line}",
                parse_mode='HTML', reply_markup={'inline_keyboard': []},
            )
        except Exception as e:
            logger.warning(f"rlq: card edit failed: {e}")
