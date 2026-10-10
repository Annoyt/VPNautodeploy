"""Reverse SOS — the bot writes first (IMPROVEMENT_PLAN E21).

Why
---
A user whose protocol died in their network learns about it the hard way:
the VPN stops, they wait, they reinstall, they write "не работает" — if
they write at all. With the per-protocol client telemetry of E8 the
server sees it first: the user's own FlClash stopped getting through a
protocol while its other protocols still pass. So when DPIMonitor moves
that protocol to the tail for the user's network (R6, ``client_dark``),
the users of that network who were using it get one short message: the
protocol stopped working in your network, the subscription is already
rebuilt — refresh the profile; if that does not help — the emergency
subscription (``bot/services/sos.py``) or /sos.

Which demotions write to users: R6 ``client_dark`` only
-------------------------------------------------------
The message states a fact about the reader's network — "P stopped working
for you" — and only R6 measures exactly that: the readers' OWN clients,
per protocol, per network. The other per-ASN rules are inferences that
can be wrong for the very people we would write to:

* R3 ``reality_asn`` counts handshake failures arriving at exit, and
  part of them is the operator's own scanners (AS31205 probes Reality at
  86 attempts a second with zero successes — memory note) — R3 fires
  there while Reality works fine for every user of that network;
* R4 ``udp_storm_asn`` is one user's reconnect storm, R5
  ``user_reports_asn`` two complaints mapped to the head of the order —
  neither knows the protocol is dead for anyone else.

A false "your VPN broke" costs trust and invites needless reinstalls, so
those rules keep moving the cascade silently, as before.

Who
---
Users whose ``users.last_asn`` is the demoted ASN, with an active key
(demo / paid / support_topic + uuid), and with at least one
``client_probe`` row ``p-<proto>`` in the last 24 h: their own client was
getting through that protocol within a day and no longer is (R6 fired on
"nobody in that network got through it for 2 h"). Telegram for real
chats; ``ext_*`` (email-only) users get a letter at ``contact_email``
through the SMTP relay — never ``users.email``, the synthetic panel id.

How often
---------
One message per chat_id per 24 h, over every channel and every event:
``app_settings.reverse_sos_sent`` = ``{chat_id: iso ts}``, pruned past
24 h on each write — the E9 pattern (a small JSON map in app_settings).
Read strictly: a locked read taken for "nobody was messaged" would send
everyone a second message, so an unreadable map skips the event. The
recipients are claimed (written) BEFORE the sends: a failed send costs
that user one message, never a duplicate. Several protocols demoted for
one network in one run go out as ONE message naming them all.

Operator side
-------------
One ``admin_actions('dpi_monitor', 'reverse_sos', target='asn:<ASN>:<proto>',
details='recipients=N …')`` row per demotion (also with zero recipients —
"nobody there used it in 24 h" is an answer too) and one line in the AI
topic per run, next to DPIMonitor's own message. Sends run on a daemon
thread 50 ms apart, as the broadcasts do; the monitor tick never waits.
The letter carries none of the mail-SOS trigger words (sos, help,
«не работает», «не подключается»): a reply quoting it must not read as an
SOS letter (E24).
"""

from __future__ import annotations

import html
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

TRIGGER_RULES = frozenset({'client_dark'})   # see "Which demotions write"
RECIPIENT_WINDOW_H = 24        # had a p-<proto> row this recently
COOLDOWN_H = 24                # one message per chat_id per day
SEND_DELAY_S = 0.05            # the broadcast cadence
SENT_SETTING_KEY = 'reverse_sos_sent'
ACTOR = 'dpi_monitor'
ACTION = 'reverse_sos'
ACTIVE_STATUSES = ('demo', 'paid', 'support_topic')
TELEMETRY_PREFIX = 'p-'        # mirrors subscription.CLASH_TELEMETRY_PREFIX


@dataclass
class Recipient:
    chat_id: str
    lang: str
    uuid: str
    contact_email: Optional[str]
    protocols: List[str] = field(default_factory=list)
    targets: List[str] = field(default_factory=list)   # audit targets covered

    @property
    def is_mail(self) -> bool:
        return self.chat_id.startswith('ext_')


def _sql_ts(when: datetime) -> str:
    # client_probe.ts is sqlite CURRENT_TIMESTAMP: 'YYYY-MM-DD HH:MM:SS'.
    return when.strftime('%Y-%m-%d %H:%M:%S')


def triggering(changes) -> list:
    """The applied changes that write to users: per-ASN demotions by a
    rule in ``TRIGGER_RULES``."""
    return [
        c for c in (changes or ())
        if getattr(c, 'action', None) == 'demote' and getattr(c, 'scope', None) == 'asn'
        and getattr(c, 'reason', None) in TRIGGER_RULES and getattr(c, 'target', None)
    ]


def select_users(db, asn: str, protocol: str, now: datetime) -> List[tuple]:
    """``(chat_id, lang, uuid, contact_email)`` of the active key holders of
    ``asn`` whose client reported ``protocol`` within RECIPIENT_WINDOW_H."""
    cutoff = _sql_ts(now - timedelta(hours=RECIPIENT_WINDOW_H))
    placeholders = ','.join('?' * len(ACTIVE_STATUSES))
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT u.chat_id, u.lang, u.uuid, u.contact_email FROM users u "
            "WHERE UPPER(TRIM(u.last_asn)) = ? "
            f"AND u.status IN ({placeholders}) "
            "AND u.uuid IS NOT NULL AND u.uuid != '' "
            "AND EXISTS (SELECT 1 FROM client_probe c WHERE c.chat_id = u.chat_id "
            "AND c.grp = ? AND c.ts >= ?) "
            "ORDER BY u.chat_id",
            (str(asn).strip().upper(), *ACTIVE_STATUSES,
             f'{TELEMETRY_PREFIX}{protocol}', cutoff),
        ).fetchall()]


def load_sent(db) -> dict:
    """``{chat_id: iso ts}`` of the last reverse SOS per user. Raises
    ``sqlite3.Error`` when the row cannot be READ (the caller skips the
    event); bad JSON is an empty map — this is a rate limit, not data."""
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (SENT_SETTING_KEY,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return {}
    try:
        parsed = json.loads(row[0])
    except (TypeError, ValueError):
        logger.warning(f"reverse_sos: bad JSON in app_settings[{SENT_SETTING_KEY}] — reset")
        return {}
    return {str(k): v for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def _sent_at(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace('Z', ''))
    except (TypeError, ValueError):
        return None


def on_cooldown(sent: dict, chat_id: str, now: datetime) -> bool:
    at = _sent_at(sent.get(chat_id))
    return at is not None and now - at < timedelta(hours=COOLDOWN_H)


def _reachable(chat_id: str, contact_email: Optional[str], mailer) -> bool:
    if chat_id.startswith('ext_'):
        return bool(contact_email and mailer is not None and mailer.is_configured())
    return chat_id.lstrip('-').isdigit()


def plan(db, changes, now: datetime, sent: dict, mailer) -> Tuple[List[Recipient], Dict[str, dict]]:
    """Recipients (one per user, every protocol of this run named) and
    per-change stats for the audit. Pure apart from the user query."""
    recipients: Dict[str, Recipient] = {}
    stats: Dict[str, dict] = {}
    for ch in triggering(changes):
        target = ch.audit_target
        st = stats.setdefault(target, {
            'asn': ch.target, 'protocol': ch.protocol, 'selected': 0, 'cooldown': 0,
            'unreachable': 0, 'telegram': 0, 'mail': 0, 'failed': 0,
        })
        for chat_id, lang, uuid, contact in select_users(db, ch.target, ch.protocol, now):
            chat_id = str(chat_id)
            st['selected'] += 1
            if on_cooldown(sent, chat_id, now):
                st['cooldown'] += 1
                continue
            if not _reachable(chat_id, contact, mailer):
                st['unreachable'] += 1
                continue
            r = recipients.get(chat_id)
            if r is None:
                r = recipients[chat_id] = Recipient(
                    chat_id=chat_id, lang='en' if (lang or 'ru') == 'en' else 'ru',
                    uuid=str(uuid), contact_email=contact)
            if ch.protocol not in r.protocols:
                r.protocols.append(ch.protocol)
            r.targets.append(target)
    return list(recipients.values()), stats


def claim(db, sent: dict, recipients: List[Recipient], now: datetime) -> bool:
    """Write the recipients into the limiter before anything is sent."""
    keep = {k: v for k, v in sent.items() if not _expired(v, now)}
    for r in recipients:
        keep[r.chat_id] = now.isoformat()
    return db.set_setting(SENT_SETTING_KEY, json.dumps(keep, sort_keys=True)) is not False


def _expired(value, now: datetime) -> bool:
    at = _sent_at(value)
    return at is None or now - at >= timedelta(hours=COOLDOWN_H)


# ---- texts ------------------------------------------------------------------

def _labels(protocols: List[str], lang: str) -> str:
    from bot.services.sos import PROTO_LABEL
    names = [PROTO_LABEL.get(p, p) for p in protocols]
    if len(names) <= 1:
        return ''.join(names)
    joiner = ' and ' if lang == 'en' else ' и '
    return ', '.join(names[:-1]) + joiner + names[-1]


def _emergency_url(config, recipient: Recipient) -> Optional[str]:
    """The user's FlClash emergency link (``sos.emergency_urls``) — the
    readers use FlClash: only its profile reports p-<proto>."""
    try:
        from bot.services.sos import emergency_urls
        urls = emergency_urls(config, SimpleNamespace(uuid=recipient.uuid))
    except Exception as e:
        logger.warning(f"reverse_sos: emergency url failed: {e}")
        return None
    return urls[1] if urls else None


def telegram_text(protocols: List[str], lang: str, url: Optional[str]) -> str:
    labels = html.escape(_labels(protocols, lang))
    plural = len(protocols) > 1
    if lang == 'en':
        head = (f"⚠️ <b>{labels} stopped working in your network.</b>\n"
                "Your subscription is already rebuilt — refresh the profile: "
                "FlClash → Profiles → ⟳.")
        tail = ("Still stuck? Add the emergency subscription "
                "(FlClash: Profiles → + → URL):\n"
                f"<code>{html.escape(url)}</code>\nor tap /sos." if url
                else "Still stuck? Tap /sos.")
        return f"{head}\n{tail}"
    verb = 'перестали работать' if plural else 'перестал работать'
    head = (f"⚠️ <b>В вашей сети {verb} {labels}.</b>\n"
            "Подписка уже перестроена — обновите профиль: FlClash → «Профили» → ⟳.")
    tail = ("Не помогло — добавьте аварийную подписку "
            "(FlClash: «Профили» → + → URL):\n"
            f"<code>{html.escape(url)}</code>\nили нажмите /sos." if url
            else "Не помогло — нажмите /sos.")
    return f"{head}\n{tail}"


def mail_letter(protocols: List[str], lang: str, url: Optional[str]) -> Tuple[str, str]:
    """(subject, plain body) for ext_* users. No mail-SOS trigger word in
    either (see the module docstring) — pinned by a test."""
    labels = _labels(protocols, lang)
    plural = len(protocols) > 1
    if lang == 'en':
        subject = f"NekoVPN: {labels} stopped working in your network"
        link = (f"Still stuck? Add the emergency subscription (FlClash: Profiles → + → URL):\n"
                f"{url}\n\n" if url else "")
        body = (
            "Hi!\n\n"
            f"{labels} stopped working in your network. Your subscription is\n"
            "already rebuilt — refresh the profile in the app:\n"
            "FlClash: Profiles → ⟳ at the top.\n\n"
            f"{link}"
            "If that does not do it either, just reply to this letter.\n"
        )
        return subject, body
    verb = 'перестали работать' if plural else 'перестал работать'
    subject = f"NekoVPN: в вашей сети {verb} {labels}"
    link = (f"Не помогло — добавьте аварийную подписку (FlClash: «Профили» → + → URL):\n"
            f"{url}\n\n" if url else "")
    body = (
        "Здравствуйте!\n\n"
        f"В вашей сети {verb} {labels}. Подписка уже перестроена —\n"
        "обновите профиль в приложении:\n"
        "FlClash: «Профили» → ⟳ вверху.\n\n"
        f"{link}"
        "Если и это не поможет — просто ответьте на это письмо.\n"
    )
    return subject, body


def topic_line(stats: Dict[str, dict]) -> str:
    parts = []
    for target in sorted(stats):
        st = stats[target]
        sent = st['telegram'] + st['mail']
        bits = [f"Telegram {st['telegram']}", f"почта {st['mail']}"]
        if st['failed']:
            bits.append(f"ошибок {st['failed']}")
        if st['cooldown']:
            bits.append(f"суточный лимит {st['cooldown']}")
        if st['unreachable']:
            bits.append(f"без канала {st['unreachable']}")
        parts.append(f"{html.escape(str(st['asn']))} · {html.escape(st['protocol'])} — "
                     f"написали {sent} ({', '.join(bits)})")
    return "📣 Обратный SOS: " + '; '.join(parts)


def audit_details(st: dict) -> str:
    return (f"recipients={st['telegram'] + st['mail'] + st['failed']} "
            f"telegram={st['telegram']} mail={st['mail']} failed={st['failed']} "
            f"cooldown={st['cooldown']} unreachable={st['unreachable']} "
            f"selected={st['selected']}")


# ---- entry point --------------------------------------------------------------

def _mailer_for(bot, config):
    services = getattr(bot, 'services', None)
    mailer = services.get('email') if isinstance(services, dict) else None
    if mailer is None:
        try:
            from bot.services.email_service import EmailService
            mailer = EmailService(config)
        except Exception as e:
            logger.warning(f"reverse_sos: no mailer: {e}")
            return None
    return mailer


def start(db, config, bot, changes, *, now: Optional[datetime] = None,
          post_topic: Optional[Callable[[str], None]] = None,
          mailer=None) -> Optional[threading.Thread]:
    """Called by DPIMonitor after a run's changes are applied. Returns the
    sender thread (tests join it), or None when nothing triggers, there
    is no bot (manual runs), or the limiter cannot be read / written.
    Never raises."""
    try:
        trig = triggering(changes)
        if not trig:
            return None
        if bot is None:
            logger.info("reverse_sos: no bot — users not messaged")
            return None
        now = now or datetime.utcnow()
        if mailer is None:
            mailer = _mailer_for(bot, config)
        try:
            sent = load_sent(db)
        except sqlite3.Error as e:
            logger.warning(f"reverse_sos: app_settings[{SENT_SETTING_KEY}] unreadable ({e}) "
                           f"— skipped rather than risk a second message")
            return None
        recipients, stats = plan(db, trig, now, sent, mailer)
        if recipients and not claim(db, sent, recipients, now):
            logger.error(f"reverse_sos: could not write app_settings[{SENT_SETTING_KEY}] "
                         f"— nothing sent ({len(recipients)} recipients)")
            return None
    except Exception as e:
        logger.exception(f"reverse_sos: planning failed: {e}")
        return None

    def _worker() -> None:
        for r in recipients:
            ok = False
            try:
                url = _emergency_url(config, r)
                if r.is_mail:
                    subject, body = mail_letter(r.protocols, r.lang, url)
                    ok = bool(mailer.send_notice(r.contact_email, subject, body))
                else:
                    ok = bool(bot.send_message(
                        chat_id=r.chat_id, text=telegram_text(r.protocols, r.lang, url),
                        parse_mode='HTML'))
            except Exception as e:
                logger.warning(f"reverse_sos: send to {r.chat_id} failed: {e}")
            for target in r.targets:
                st = stats[target]
                if not ok:
                    st['failed'] += 1
                elif r.is_mail:
                    st['mail'] += 1
                else:
                    st['telegram'] += 1
            time.sleep(SEND_DELAY_S)
        for target, st in stats.items():
            try:
                db.log_admin_action(ACTOR, ACTION, target_id=target, details=audit_details(st))
            except Exception as e:
                logger.warning(f"reverse_sos: audit write failed for {target}: {e}")
        if post_topic is not None:
            try:
                post_topic(topic_line(stats))
            except Exception as e:
                logger.warning(f"reverse_sos: topic line failed: {e}")
        logger.info("reverse_sos: " + '; '.join(
            f"{t} {audit_details(s)}" for t, s in sorted(stats.items())))

    t = threading.Thread(target=_worker, daemon=True, name='reverse-sos')
    t.start()
    return t
