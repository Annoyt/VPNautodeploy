"""SOS channels — what a user can reach for when the VPN stops working
(IMPROVEMENT_PLAN E19 /sos, E24 SOS by mail, E27 offline kit, E29 "share
with neighbours", A1.2 "refresh your subscription" nudge).

Why
---
Everything here must already be in the user's hands BEFORE an outage:
during one there is nothing left to hand it out through. So each piece is
small and self-contained:

* ``/sos`` (Telegram) answers at once with the EMERGENCY profile links —
  the lockdown profile (ws-first order, DNS through the tunnel; B1/B5)
  forced for one user via ``/sub/<token>?emergency=1`` — how to refresh
  the main subscription, and what works right now: our own probes (the
  /protocols judgement, ``admin/ops.probe_verdict``) plus what other
  users of the SAME network (ASN) connected with in the last hour. The
  operator gets the failure-report facts (``MyKeyAnswerHandler.
  _report_facts`` — not a second collector) with an SOS mark in the
  support topic, a ``user_failure_reports`` row (target ``sos``) and an
  agent diagnosis kicked the way the protocol_down pager does it.
* SOS by mail — a letter from a key holder's ``contact_email`` with a
  trigger word gets the same links back by mail (E24). The address is
  the identity: ``users.email`` is the synthetic panel id and is never
  written to.
* ``/kit`` — the same emergency profile as two FILES (Clash/mihomo and
  sing-box); a file imports with no network at all (E27).
* ``/share`` — text: how a device with a working VPN shares it over the
  LAN (FlClash "Allow LAN", mixed port 7890) — one hop (E29).

Limits: one /sos per 10 minutes per user (the row in
``user_failure_reports`` IS the limiter, so it survives a restart); one
kit per 10 minutes (in memory); one mail SOS reply per hour per address
(``email_requests`` row with status ``sos``). A repeat inside the window
gets a short answer and no operator ping.

Nothing here ever DMs the admin: operator-side output goes to the forum
support topic, like the "не работает" reports (house rule).
"""

from __future__ import annotations

import email
import html
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---- who / how often ---------------------------------------------------------
SOS_STATUSES = frozenset({'demo', 'paid', 'support_topic'})
PAID_STATUSES = frozenset({'paid', 'support_topic'})
SOS_RATE_LIMIT_S = 600          # one /sos per user per 10 min
KIT_RATE_LIMIT_S = 600          # one kit per user per 10 min
MAIL_SOS_DEDUP_S = 3600         # one mail SOS reply per address per hour

# ---- storage -----------------------------------------------------------------
SOS_TARGET = 'sos'              # user_failure_reports.target_domain
MAIL_SOS_TARGET = 'sos_mail'
MAIL_SOS_STATUS = 'sos'         # email_requests.status of an SOS letter

# ---- mail trigger -------------------------------------------------------------
MAIL_SOS_KEYWORDS = ('sos', 'не работает', 'не подключается', 'help')
# Latin words whole (no "helpful", no "SOSiska"); the Russian phrases with
# any whitespace between the words (a line break in a letter included).
_KEYWORD_RE = re.compile(
    r'(?<!\w)(?:sos|help)(?!\w)|не\s+работает|не\s+подключается',
    re.IGNORECASE,
)
# Where a quoted earlier letter starts: our own letters (the key letter
# says "Не подключается?") come back quoted in every reply, and a quote
# must never read as a fresh SOS.
_QUOTE_HEADER_RE = re.compile(
    r'^\s*(?:-{2,}.*(?:original message|forwarded message|исходное сообщение|'
    r'пересылаемое сообщение|пересланное сообщение).*'
    r'|on\s.{0,300}\swrote:\s*'
    r'|.{0,300}\s(?:пишет|написал|написала|написал\(а\)):\s*'
    r'|.{0,300}<[^<>@\s]+@[^<>\s]+>.{0,40}:\s*'
    r'|_{8,}\s*'                                    # Outlook's separator
    r'|(?:from|от|отправитель):\s.*@.*)$',          # …and its header block
    re.IGNORECASE,
)
MAIL_BODY_FETCH_BYTES = 65536   # the start of a letter is where the words are

# ---- offline kit / share ------------------------------------------------------
KIT_YAML_NAME = 'NekoVPN-emergency.yaml'
KIT_JSON_NAME = 'NekoVPN-emergency.json'
SHARE_PORT = 7890               # FlClash's default mixed (HTTP+SOCKS) port

# ---- callbacks ----------------------------------------------------------------
CALLBACK_PREFIX = 'sos:'
CALLBACK_KIT = 'sos:kit'
CALLBACK_SHARE = 'sos:share'

# ---- "what works in your network" ---------------------------------------------
NETWORK_WINDOW_MIN = 60
NETWORK_REPORTS_WINDOW_H = 6
# exit inbound (user_presence.proto) → cascade protocol name
_PRESENCE_PROTO = {'reality': 'reality', 'cf-ws': 'ws', 'ss2022': 'stls',
                   'xhttp': 'xhttp'}
PROTO_LABEL = {
    'ws': 'Cloudflare (WS)',
    'stls': 'ShadowTLS',
    'reality': 'Reality',
    'hy2': 'Hysteria2',
    'hy2t': 'Hysteria2 Turbo',
    'xhttp': 'XHTTP',
}
_VERDICT_TEXT = {
    'ru': {'ok': ('✅', 'работает'), 'silent': ('⚠️', 'с перебоями'),
           'degraded': ('⚠️', 'с перебоями'), 'down': ('❌', 'не отвечает')},
    'en': {'ok': ('✅', 'works'), 'silent': ('⚠️', 'unstable'),
           'degraded': ('⚠️', 'unstable'), 'down': ('❌', 'not responding')},
}

# ---- agent ---------------------------------------------------------------------
# One SOS diagnosis at a time against the single Hermes on entry: during a
# mass outage every user presses /sos, and protocol_down already kicks its
# own turns. The rest are skipped (the report says so), never queued.
AGENT_MAX_CONCURRENT = 1
# The worker waits for the SOS report to be posted before asking the agent
# (the reply must land under the report); a stuck post costs at most this.
AGENT_GATE_TIMEOUT_S = 30


# ---- small helpers -------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_ts(value) -> Optional[datetime]:
    """Naive UTC from either DB spelling ('2026-10-08 12:00:00' or ISO)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', ''))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _sql_ts(when: datetime) -> str:
    return when.strftime('%Y-%m-%d %H:%M:%S')


def user_lang(user) -> str:
    return 'en' if (getattr(user, 'lang', None) or 'ru') == 'en' else 'ru'


def has_active_key(user) -> bool:
    """A key the emergency profile can be built for: an approved status
    and a uuid (the /sub token derives from it)."""
    return bool(user and getattr(user, 'uuid', None)
                and (getattr(user, 'status', '') or '') in SOS_STATUSES)


def _norm_asn(asn) -> Optional[str]:
    if not isinstance(asn, str):
        return None
    asn = asn.strip().upper()
    return asn or None


def match_sos_keywords(text: str) -> bool:
    return bool(text) and bool(_KEYWORD_RE.search(text))


def strip_quoted(text: str) -> str:
    """The user's own words: cut at the first quote header, drop '>' lines."""
    out = []
    for line in (text or '').splitlines():
        if _QUOTE_HEADER_RE.match(line):
            break
        if line.lstrip().startswith('>'):
            continue
        out.append(line)
    return '\n'.join(out)


def _html_to_text(raw: str) -> str:
    # Quoted history lives in <blockquote> / Gmail's gmail_quote block.
    raw = re.sub(r'(?is)<div[^>]*class="[^"]*gmail_quote.*', '', raw)
    raw = re.sub(r'(?is)<blockquote.*?</blockquote>', ' ', raw)
    raw = re.sub(r'(?is)<(script|style).*?</\1>', ' ', raw)
    raw = re.sub(r'(?i)<br\s*/?>|</p>|</div>', '\n', raw)
    return html.unescape(re.sub(r'<[^>]+>', ' ', raw))


def letter_text(raw: bytes) -> str:
    """Plain text of a letter (text/plain preferred, else de-tagged HTML),
    quotes removed. Attachments are skipped."""
    msg = email.message_from_bytes(raw or b'')
    plain, rich = [], []
    for part in msg.walk():
        if part.is_multipart():
            continue
        if (part.get_content_disposition() or '') == 'attachment':
            continue
        ctype = part.get_content_type()
        if ctype not in ('text/plain', 'text/html'):
            continue
        try:
            payload = part.get_payload(decode=True) or b''
            body = payload.decode(part.get_content_charset() or 'utf-8', 'replace')
        except Exception:
            continue
        (plain if ctype == 'text/plain' else rich).append(body)
    if plain:
        return strip_quoted('\n'.join(plain))
    return strip_quoted(_html_to_text('\n'.join(rich)))


def support_topic(config) -> Tuple[Optional[str], Optional[int]]:
    """Where the "не работает" reports go — the SOS reports follow them."""
    group = getattr(config, 'FORUM_GROUP_ID', 0) or None
    topic = getattr(config, 'TOPIC_SUPPORT', 0) or None
    return group, topic


def _plain(line: str) -> str:
    """An HTML report line as plain text (agent prompt, logs)."""
    return html.unescape(re.sub(r'<[^>]+>', '', line))


def _ago_min(when: Optional[datetime], now: datetime) -> int:
    if not when:
        return 0
    return max(0, int((now - when).total_seconds() // 60))


# ---- emergency profile ----------------------------------------------------------

def emergency_urls(config, user) -> Optional[Tuple[str, str]]:
    """``(sing-box URL, FlClash URL)`` of the user's emergency profile, or
    None when the bot does not know its public address (WEBAPP_URL)."""
    from bot.services.subscription import SubscriptionService
    base = SubscriptionService(config).build_subscription_url(user)
    if not base:
        return None
    return f'{base}?emergency=1', f'{base}?format=clash&emergency=1'


def emergency_cascade(db, user) -> tuple:
    """The order the emergency profile carries for this user (stored geo —
    there is no request IP outside /sub)."""
    from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
    return MK.get_cascade_order(db, user=user, force_lockdown=True)


def build_kit(db, config, user) -> List[Tuple[str, bytes]]:
    """The offline kit: ``[(name, bytes)]`` — the Clash/mihomo profile
    (JSON, a YAML 1.2 subset — exactly what ``/sub?format=clash&
    emergency=1`` serves for the user's stored network, demotion-aware
    groups and providers included) and the sing-box profile, both
    emergency."""
    from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
    from bot.services.subscription import SubscriptionService
    cascade = emergency_cascade(db, user)
    sub = SubscriptionService(config)
    demoted = MK.get_auto_demotions(db, getattr(user, 'last_asn', None))
    clash = sub.build_clash_config(user, cascade, lockdown=True,
                                   demoted=frozenset(demoted))
    singbox = sub.build_singbox_config(user, cascade, lockdown=True)
    return [
        (KIT_YAML_NAME, clash.encode('utf-8')),
        (KIT_JSON_NAME,
         json.dumps(singbox, ensure_ascii=False, indent=2).encode('utf-8')),
    ]


# ---- "what works right now" -------------------------------------------------------

def probe_summary(db, config, protocols, now: datetime) -> dict:
    """Our probes for ``protocols``, judged exactly like /protocols:
    ``{'stale': bool, 'items': [(proto, verdict)]}`` — verdicts from
    ``admin/ops.probe_verdict``; protocols without rows are left out."""
    from bot.handlers.admin.ops import (
        AdminOpsMixin as _Ops, _minutes_since, probe_verdict, read_probe_state,
    )
    try:
        state = read_probe_state(db, config, now, window_h=_Ops.PROTO_WINDOW_H,
                                 runs=_Ops.PROTO_RUNS)
    except Exception as e:
        logger.warning(f"sos: probe read failed: {e}")
        return {'stale': True, 'items': []}
    newest = state.get('newest')
    if not newest or _minutes_since(newest, now) > _Ops.PROTO_STALE_MIN:
        return {'stale': True, 'items': []}
    items = []
    for proto in protocols:
        verdict = probe_verdict(state['per_tag'].get(proto),
                                min_samples=_Ops.PROTO_MIN_SAMPLES)
        if verdict != 'unknown':
            items.append((proto, verdict))
    return {'stale': False, 'items': items}


def load_auto_demotions(db) -> dict:
    """DPIMonitor's effective demotions (``cascade_auto``), normalised;
    bad JSON → nothing demoted."""
    try:
        from bot.services.dpi_monitor import normalize_auto
        raw = db.get_setting('cascade_auto')
        return normalize_auto(json.loads(raw) if raw else {})
    except Exception as e:
        logger.warning(f"sos: cascade_auto read failed: {e}")
        return {'global': {}, 'asn': {}}


def network_signal(db, asn, exclude_chat_id, now: datetime) -> Optional[dict]:
    """What other users of the same ASN show: protocols they connected
    with in the last hour (exit presence + hy2 auths, by ``users.last_asn``),
    other users' failure reports in 6 h, DPIMonitor's demotions for the
    ASN, the AS org name when dpi_metrics knows it. None when the ASN is
    unknown. Each read is best-effort."""
    asn = _norm_asn(asn)
    if not asn:
        return None
    cid = str(exclude_chat_id or '')
    out = {'asn': asn, 'org': None, 'working': {}, 'reports': 0,
           'demoted': sorted(load_auto_demotions(db)['asn'].get(asn, {}))}
    since = now - timedelta(minutes=NETWORK_WINDOW_MIN)
    try:
        with db._connect() as conn:
            for proto, n in conn.execute(
                "SELECT up.proto, COUNT(DISTINCT u.chat_id) FROM user_presence up "
                "JOIN users u ON u.email = up.email "
                "WHERE UPPER(TRIM(u.last_asn)) = ? AND u.chat_id != ? "
                "AND up.seen_at >= ? GROUP BY up.proto",
                (asn, cid, since.isoformat()),
            ).fetchall():
                key = _PRESENCE_PROTO.get(proto, proto)
                out['working'][key] = out['working'].get(key, 0) + int(n or 0)
            n = conn.execute(
                "SELECT COUNT(DISTINCT h.chat_id) FROM hy2_auth_log h "
                "JOIN users u ON u.chat_id = h.chat_id "
                "WHERE UPPER(TRIM(u.last_asn)) = ? AND h.chat_id != ? "
                "AND h.decision = 'allow' AND h.ts >= ?",
                (asn, cid, _sql_ts(since)),
            ).fetchone()[0]
            if n:
                out['working']['hy2'] = out['working'].get('hy2', 0) + int(n)
            out['reports'] = int(conn.execute(
                "SELECT COUNT(DISTINCT chat_id) FROM user_failure_reports "
                "WHERE UPPER(TRIM(asn)) = ? AND chat_id != ? AND ts >= ?",
                (asn, cid, _sql_ts(now - timedelta(hours=NETWORK_REPORTS_WINDOW_H))),
            ).fetchone()[0] or 0)
            row = conn.execute(
                "SELECT as_org FROM dpi_metrics WHERE UPPER(TRIM(asn)) = ? "
                "AND as_org IS NOT NULL AND TRIM(as_org) != '' "
                "ORDER BY snapshot_at DESC LIMIT 1", (asn,),
            ).fetchone()
            out['org'] = row[0].strip() if row else None
    except Exception as e:
        logger.warning(f"sos: network signal for {asn} partial: {e}")
    return out


def _proto_list(counts: Dict[str, int], lang: str = 'ru') -> str:
    """'Reality — 3 чел., Cloudflare (WS) — 1 чел.' (busiest first)."""
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if lang == 'en':
        return ', '.join(f"{PROTO_LABEL.get(p, p)} — {n} user{'s' if n != 1 else ''}"
                         for p, n in ranked)
    return ', '.join(f"{PROTO_LABEL.get(p, p)} — {n} чел." for p, n in ranked)


def _ago_text(minutes: int, lang: str) -> str:
    if minutes < 1:
        return 'just now' if lang == 'en' else 'только что'
    return f"{minutes} min ago" if lang == 'en' else f"{minutes} мин назад"


def status_lines(probes: dict, net: Optional[dict], lang: str, *,
                 rich: bool = True) -> List[str]:
    """Section 3 of the /sos answer; ``rich=False`` is the SOS letter —
    plain text, and in Russian the «вы» of our letters instead of the
    bot's «ты»."""
    esc = html.escape if rich else (lambda s: s)
    ru = lang != 'en'
    # (informal, formal) Russian pairs for the network lines
    your = 'Твоя' if rich else 'Ваша'
    of_your = 'твоей' if rich else 'вашей'
    lines = ["Наши серверы (проверяем каждые 15 мин):" if ru
             else "Our servers (checked every 15 min):"]
    if probes.get('stale'):
        lines = ["Свежих проверок серверов сейчас нет — смотрим вручную." if ru
                 else "No fresh server checks right now — we're looking by hand."]
    elif not probes.get('items'):
        lines = ["Данных проверок пока нет." if ru else "No check data yet."]
    else:
        words = _VERDICT_TEXT['ru' if ru else 'en']
        for proto, verdict in probes['items']:
            icon, word = words.get(verdict, ('⚪', '?'))
            lines.append(f"{icon} {PROTO_LABEL.get(proto, proto)} — {word}")
    if net is None:
        lines.append(
            ("📶 Твою сеть мы пока не знаем — обнови подписку (шаг 2), и здесь "
             "появится сводка по твоему оператору." if rich else
             "📶 Вашу сеть мы пока не знаем — обновите подписку (шаг 2), и здесь "
             "появится сводка по вашему оператору.") if ru else
            "📶 We don't know your network yet — refresh the subscription "
            "(step 2) and this will show your provider.")
        return lines
    where = esc(f"{net['org']}, {net['asn']}" if net.get('org') else net['asn'])
    if net['working']:
        lines.append(
            f"📶 {your} сеть ({where}): за последний час у других работали: "
            f"{_proto_list(net['working'])}" if ru else
            f"📶 Your network ({where}): in the last hour others connected via: "
            f"{_proto_list(net['working'], 'en')}")
    else:
        lines.append(f"📶 {your} сеть ({where}): за последний час данных от других нет."
                     if ru else
                     f"📶 Your network ({where}): no data from others in the last hour.")
    if net['demoted']:
        names = ', '.join(PROTO_LABEL.get(p, p) for p in net['demoted'])
        one = len(net['demoted']) == 1
        if ru:
            lines.append(f"Там сейчас хуже проходит {names} — в профиле он стоит "
                         f"последним." if one else
                         f"Там сейчас хуже проходят {names} — в профиле они стоят "
                         f"последними.")
        else:
            lines.append(f"Doing worse there now: {names} — last in your profile.")
    if net['reports']:
        lines.append(f"Ещё {net['reports']} чел. из {of_your} сети сообщили о проблемах "
                     f"за {NETWORK_REPORTS_WINDOW_H} ч — мы видим." if ru else
                     f"{net['reports']} more people on your network reported problems "
                     f"in the last {NETWORK_REPORTS_WINDOW_H} h — we see it.")
    return lines


# ---- user-facing texts --------------------------------------------------------------

def sos_text(urls: Optional[Tuple[str, str]], status: List[str], lang: str,
             is_ios: bool) -> str:
    ru = lang != 'en'
    parts = ["🆘 <b>Аварийный режим</b>\n\nСигнал получили — посмотрим, что у "
             "тебя. А пока сделай вот что:" if ru else
             "🆘 <b>Emergency mode</b>\n\nGot your signal — we'll look into it. "
             "Meanwhile:"]
    why = ("В ней первым идёт Cloudflare, а DNS — через VPN: так соединение "
           "проходит, даже когда сеть пускает только «разрешённые» сайты."
           if ru else
           "It puts Cloudflare first and sends DNS through the VPN, so it gets "
           "through even when the network only lets \"allowed\" sites pass.")
    keep = ("Это вторая подписка — основную не удаляй." if ru
            else "Add it as a second subscription — keep the main one.")
    if urls is None:
        block = ("<b>1. Аварийная подписка</b> сейчас недоступна — обнови основную "
                 "(шаг 2), мы уже смотрим." if ru else
                 "<b>1. The emergency subscription</b> is unavailable right now — "
                 "refresh the main one (step 2), we're on it.")
    elif is_ios:
        sb = html.escape(urls[0])
        block = ((f"<b>1. Добавь аварийную подписку.</b> {why}\n"
                  f"Karing: <b>+ → Добавить из ссылки</b> → вставить:\n"
                  f"<code>{sb}</code>\n{keep}") if ru else
                 (f"<b>1. Add the emergency subscription.</b> {why}\n"
                  f"Karing: <b>+ → Add from URL</b> → paste:\n"
                  f"<code>{sb}</code>\n{keep}"))
    else:
        sb, clash = html.escape(urls[0]), html.escape(urls[1])
        block = ((f"<b>1. Добавь аварийную подписку.</b> {why} Лучше всего она "
                  f"работает в FlClash.\n"
                  f"FlClash: <b>Профили → + → URL</b> → вставить:\n"
                  f"<code>{clash}</code>\n"
                  f"Hiddify: скопируй ссылку → <b>+ → Буфер обмена</b>:\n"
                  f"<code>{sb}</code>\n{keep}") if ru else
                 (f"<b>1. Add the emergency subscription.</b> {why} Works best "
                  f"in FlClash.\n"
                  f"FlClash: <b>Profiles → + → URL</b> → paste:\n"
                  f"<code>{clash}</code>\n"
                  f"Hiddify: copy the link → <b>+ → Clipboard</b>:\n"
                  f"<code>{sb}</code>\n{keep}"))
    parts.append(block)
    if is_ios:
        refresh = ("<b>2. Обнови основную подписку</b> в Karing (список профилей → "
                   "обновить).\nНе помогло — переключись между Wi-Fi и мобильным "
                   "интернетом." if ru else
                   "<b>2. Refresh your main subscription</b> in Karing (profile list "
                   "→ update).\nStill stuck? Switch between Wi-Fi and mobile data.")
    else:
        refresh = ("<b>2. Обнови основную подписку.</b>\n"
                   "Hiddify: ⟳ на карточке профиля (или потяни экран вниз).\n"
                   "FlClash: «Профили» → ⟳ вверху.\n"
                   "Не помогло — переключись между Wi-Fi и мобильным интернетом."
                   if ru else
                   "<b>2. Refresh your main subscription.</b>\n"
                   "Hiddify: ⟳ on the profile card (or pull down).\n"
                   "FlClash: Profiles → ⟳ at the top.\n"
                   "Still stuck? Switch between Wi-Fi and mobile data.")
    parts.append(refresh)
    parts.append(("<b>3. Что сейчас работает</b>\n" if ru
                  else "<b>3. What works right now</b>\n") + '\n'.join(status))
    parts.append("📦 Сохрани офлайн-комплект заранее: /kit или кнопка ниже." if ru
                 else "📦 Save the offline kit in advance: /kit or the button below.")
    return '\n\n'.join(parts)


def sos_keyboard(lang: str, is_ios: bool) -> dict:
    ru = lang != 'en'
    rows = [
        [{'text': '📦 Офлайн-комплект' if ru else '📦 Offline kit',
          'callback_data': CALLBACK_KIT}],
        [{'text': '📶 Раздать VPN соседям' if ru else '📶 Share VPN nearby',
          'callback_data': CALLBACK_SHARE}],
    ]
    if not is_ios:
        from bot.handlers.callbacks.user import client_download_row
        rows.insert(0, client_download_row())
    return {'inline_keyboard': rows}


def sos_repeat_text(lang: str, minutes_ago: int) -> str:
    when = _ago_text(minutes_ago, lang)
    if lang == 'en':
        return (f"⏳ Got your SOS {when} — we're on it. The emergency links are "
                f"in the message above; offline kit — /kit.")
    return (f"⏳ SOS уже получен {when} — мы смотрим. Аварийные ссылки — в "
            f"сообщении выше, офлайн-комплект — /kit.")


def no_key_text(lang: str) -> str:
    return ("⚠️ Get a key first — /start" if lang == 'en'
            else "⚠️ Сначала получите ключ — /start")


def kit_intro_text(lang: str) -> str:
    if lang == 'en':
        return (
            "📦 <b>Offline kit</b>\n\n"
            "Two files with the emergency profile (Cloudflare first, DNS through "
            "the VPN). They import with no internet at all — <b>save them now</b>, "
            "while everything works.\n\n"
            "• <b>FlClash</b> — the .yaml file: <b>Profiles → + → File</b>.\n"
            "• <b>Hiddify</b> — the .json file: <b>+ → File</b>; no such button "
            "(Hiddify 4.1)? Open the .json, copy all of its text and add it via "
            "<b>+ → Clipboard</b>.\n"
            "• Other sing-box apps (Karing) — import the .json as a profile file.\n\n"
            "⚠️ The files hold your personal key — don't forward them. Servers "
            "change: grab a fresh kit (/kit) once a month."
        )
    return (
        "📦 <b>Офлайн-комплект</b>\n\n"
        "Два файла с аварийным профилем (Cloudflare первым, DNS через VPN). "
        "Импортируются вообще без интернета — <b>сохрани их сейчас</b>, пока всё "
        "работает.\n\n"
        "• <b>FlClash</b> — файл .yaml: <b>Профили → + → Файл</b>.\n"
        "• <b>Hiddify</b> — файл .json: <b>+ → Файл</b>; нет такой кнопки "
        "(Hiddify 4.1)? Открой .json, скопируй весь текст и добавь через "
        "<b>+ → Буфер обмена</b>.\n"
        "• Другие приложения на sing-box (Karing) — импорт .json как файла профиля.\n\n"
        "⚠️ В файлах твой личный ключ — не пересылай их. Серверы меняются: раз в "
        "месяц бери свежий комплект (/kit)."
    )


def kit_captions(lang: str) -> Dict[str, str]:
    if lang == 'en':
        return {KIT_YAML_NAME: "FlClash: Profiles → + → File",
                KIT_JSON_NAME: "Hiddify / sing-box: + → File"}
    return {KIT_YAML_NAME: "FlClash: Профили → + → Файл",
            KIT_JSON_NAME: "Hiddify / sing-box: + → Файл"}


def kit_repeat_text(lang: str, minutes_ago: int) -> str:
    when = _ago_text(minutes_ago, lang)
    if lang == 'en':
        return f"⏳ The kit was sent {when} — the files are above in this chat."
    return f"⏳ Комплект уже отправлен {when} — файлы выше в чате."


def kit_error_text(lang: str) -> str:
    if lang == 'en':
        return "⚠️ Couldn't send the kit — try again in a minute, or use /sos."
    return "⚠️ Не получилось отправить комплект — попробуй через минуту или жми /sos."


def share_text(lang: str) -> str:
    """E29 — one hop over the LAN through FlClash's mixed port. Paths are
    FlClash's own labels (Tools → General → Inbound: Port, Allow LAN)."""
    port = SHARE_PORT
    if lang == 'en':
        return (
            "📶 <b>Share your VPN with neighbours</b> (one hop)\n\n"
            "If the VPN works on your device but not on another one (a second "
            "phone, a laptop, a TV), your device can share its VPN over Wi-Fi. "
            "Both must be on the same network (one router or hotspot).\n\n"
            "<b>On the device with a working VPN (FlClash: Android / Windows / "
            "macOS):</b>\n"
            "1. FlClash: <b>Tools → General</b>, section «Inbound» → turn on "
            f"<b>Allow LAN</b>. «Port» is in the same section — <code>{port}</code> "
            "by default.\n"
            "2. Find this device's IP in the network settings: Android — "
            "<b>Settings → Wi-Fi → (your network) → IP address</b>; Windows — "
            "<b>Settings → Network & internet → Wi-Fi → network properties → IPv4 "
            "address</b>; macOS — <b>System Settings → Wi-Fi → Details → TCP/IP</b>. "
            "It looks like <code>192.168.x.x</code>.\n\n"
            f"<b>On the other device</b> set the proxy <code>IP:{port}</code>:\n"
            "• Android: <b>Settings → Wi-Fi → the network (⚙) → Edit → Advanced → "
            f"Proxy: Manual</b> → host = IP, port = {port}.\n"
            "• Windows: <b>Settings → Network & internet → Proxy → Use a proxy "
            f"server</b> → address = IP, port = {port}.\n"
            "• iPhone (as the client only): <b>Settings → Wi-Fi → (i) next to the "
            f"network → Configure Proxy → Manual</b> → server = IP, port = {port}.\n\n"
            "⚠️ Browsers and most apps follow the proxy, not all of them. Keep the "
            "sharing device on and FlClash connected. Turn Allow LAN off when "
            "you're done — otherwise anyone on that Wi-Fi can use it."
        )
    return (
        "📶 <b>Раздать VPN соседям</b> (один хоп)\n\n"
        "Если у тебя VPN работает, а на другом устройстве (второй телефон, "
        "ноутбук, ТВ) — нет, твоё устройство может раздать свой VPN по Wi-Fi. Оба "
        "должны быть в одной сети (один роутер или точка доступа).\n\n"
        "<b>На устройстве с рабочим VPN (FlClash: Android / Windows / macOS):</b>\n"
        "1. FlClash: <b>Инструменты → Общие</b>, раздел «Входящие» → включи "
        f"<b>«Разрешить LAN»</b>. Там же «Порт» — по умолчанию <code>{port}</code>.\n"
        "2. Узнай IP этого устройства в настройках сети: Android — "
        "<b>Настройки → Wi-Fi → (твоя сеть) → IP-адрес</b>; Windows — "
        "<b>Параметры → Сеть и Интернет → Wi-Fi → свойства сети → IPv4-адрес</b>; "
        "macOS — <b>Системные настройки → Wi-Fi → Подробнее → TCP/IP</b>. Обычно "
        "вида <code>192.168.x.x</code>.\n\n"
        f"<b>На втором устройстве</b> укажи прокси <code>IP:{port}</code>:\n"
        "• Android: <b>Настройки → Wi-Fi → сеть (⚙) → Изменить → Расширенные "
        f"настройки → Прокси: Вручную</b> → узел = IP, порт = {port}.\n"
        "• Windows: <b>Параметры → Сеть и Интернет → Прокси → Использовать "
        f"прокси-сервер</b> → адрес = IP, порт = {port}.\n"
        "• iPhone (только как клиент): <b>Настройки → Wi-Fi → (i) у сети → "
        f"Настройка прокси → Вручную</b> → сервер = IP, порт = {port}.\n\n"
        "⚠️ Через прокси идут браузер и большинство приложений, но не все. "
        "Раздающее устройство должно оставаться включённым, FlClash — "
        "подключённым. Выключи «Разрешить LAN», когда закончишь — иначе прокси "
        "открыт всем в этой Wi-Fi."
    )


def nudge_text(lang: str) -> str:
    """A1.2 — users whose ``last_asn`` is empty have not fetched /sub since
    geo landed; per-ASN tuning only reaches a client that refreshes."""
    if lang == 'en':
        return (
            "🔄 <b>Please refresh your subscription</b>\n\n"
            "We tune the server order for each provider. For that to reach you, "
            "your app needs to refresh the subscription at least once:\n"
            "• Hiddify: ⟳ on the profile card (or pull down)\n"
            "• FlClash: Profiles → ⟳ at the top\n"
            "• Karing: refresh the profile in the profile list\n\n"
            "Nothing to reinstall. VPN not connecting? /sos"
        )
    return (
        "🔄 <b>Обнови, пожалуйста, подписку</b>\n\n"
        "Мы подстраиваем порядок серверов под разных операторов. Чтобы это "
        "дошло до тебя, приложению нужно хотя бы раз обновить подписку:\n"
        "• Hiddify: ⟳ на карточке профиля (или потяни экран вниз)\n"
        "• FlClash: «Профили» → ⟳ вверху\n"
        "• Karing: обнови профиль в списке профилей\n\n"
        "Ничего переустанавливать не нужно. VPN не подключается? /sos"
    )


def mail_sos_letter(urls: Optional[Tuple[str, str]], status: List[str],
                    lang: str) -> Tuple[str, str]:
    """(subject, plain body) of the SOS reply. Deliberately free of the
    trigger words: a reply quoting it must not read as a new SOS, and the
    subject is ours, not "Re: SOS"."""
    ru = lang != 'en'
    status_block = '\n'.join(status)
    if ru:
        subject = "NekoVPN: аварийная подписка"
        if urls:
            links = (
                "FlClash (Android/ПК): Профили → + → URL → вставьте:\n"
                f"{urls[1]}\n\n"
                "Hiddify: скопируйте ссылку → + → Буфер обмена.\n"
                "Karing (iPhone): + → Добавить из ссылки.\n"
                f"{urls[0]}\n"
            )
        else:
            links = "Аварийная ссылка сейчас недоступна — мы уже смотрим.\n"
        body = (
            "Здравствуйте! Получили ваше письмо — вот что можно сделать\n"
            "прямо сейчас.\n"
            "\n"
            "=== 1. АВАРИЙНАЯ ПОДПИСКА ===\n"
            "В ней первым идёт Cloudflare, а DNS — через VPN: так соединение\n"
            "проходит, даже когда сеть пускает только «разрешённые» сайты.\n"
            "Добавьте её второй подпиской — основную не удаляйте.\n"
            "\n"
            f"{links}"
            "\n"
            "=== 2. ОБНОВИТЕ ОСНОВНУЮ ПОДПИСКУ ===\n"
            "Hiddify: кнопка ⟳ на карточке профиля (или потяните экран вниз).\n"
            "FlClash: «Профили» → ⟳ вверху.\n"
            "Karing: обновите профиль в списке профилей.\n"
            "Не помогло — переключитесь между Wi-Fi и мобильным интернетом.\n"
            "\n"
            "=== 3. ЧТО СЕЙЧАС РАБОТАЕТ ===\n"
            f"{status_block}\n"
            "\n"
            "Мы получили сигнал и посмотрим, что у вас. Если станет хуже —\n"
            "просто ответьте на это письмо.\n"
        )
        return subject, body
    subject = "NekoVPN: emergency subscription"
    if urls:
        links = (
            "FlClash (Android/PC): Profiles → + → URL → paste:\n"
            f"{urls[1]}\n\n"
            "Hiddify: copy the link → + → Clipboard.\n"
            "Karing (iPhone): + → Add from URL.\n"
            f"{urls[0]}\n"
        )
    else:
        links = "The emergency link is unavailable right now — we're on it.\n"
    body = (
        "Hi! We got your letter — here is what you can do right now.\n"
        "\n"
        "=== 1. EMERGENCY SUBSCRIPTION ===\n"
        "It puts Cloudflare first and sends DNS through the VPN, so it gets\n"
        "through even when the network only lets \"allowed\" sites pass.\n"
        "Add it as a second subscription — keep the main one.\n"
        "\n"
        f"{links}"
        "\n"
        "=== 2. REFRESH YOUR MAIN SUBSCRIPTION ===\n"
        "Hiddify: the ⟳ button on the profile card (or pull down).\n"
        "FlClash: Profiles → ⟳ at the top.\n"
        "Karing: refresh the profile in the profile list.\n"
        "Still stuck? Switch between Wi-Fi and mobile data.\n"
        "\n"
        "=== 3. WHAT WORKS RIGHT NOW ===\n"
        f"{status_block}\n"
        "\n"
        "We got your signal and will look into it. If it gets worse, just\n"
        "reply to this letter.\n"
    )
    return subject, body


# ---- agent ---------------------------------------------------------------------------

def _make_sos_agent_class():
    """``AlertManager`` with the SOS delivery: the protocol_down plumbing
    (daemon worker, one turn per key, slot cap, error containment, reply
    stored nowhere but posted) re-used as is; only the channel differs —
    the support topic where the SOS report went, never a PM — plus a
    gate, so the diagnosis can never land above the report it answers."""
    from bot.services.alert_manager import AlertManager

    class SosAgent(AlertManager):
        def __init__(self, bot, config, db):
            super().__init__(bot, config, db=db)
            self.slots = threading.Semaphore(AGENT_MAX_CONCURRENT)
            self._gates: Dict[int, threading.Event] = {}
            self._gates_lock = threading.Lock()

        def gate(self, alert) -> threading.Event:
            """The worker for ``alert`` holds its agent call until this is
            set (or ``AGENT_GATE_TIMEOUT_S`` passes)."""
            ev = threading.Event()
            with self._gates_lock:
                self._gates[id(alert)] = ev
            return ev

        def drop_gate(self, alert) -> None:
            with self._gates_lock:
                self._gates.pop(id(alert), None)

        def _kick_agent(self, alert, alert_db_id, **kwargs):
            with self._gates_lock:
                ev = self._gates.pop(id(alert), None)
            if ev is not None and not ev.wait(AGENT_GATE_TIMEOUT_S):
                logger.warning(f"sos agent: report for {alert.key} not posted "
                               f"in {AGENT_GATE_TIMEOUT_S}s — asking anyway")
            return super()._kick_agent(alert, alert_db_id, **kwargs)

        def _deliver_to_admin(self, text: str, *, reply_markup=None,
                              pm_fallback: bool = False) -> bool:
            group, topic = support_topic(self.config)
            if not (group and topic):
                return False
            try:
                return self.bot.send_message(
                    chat_id=group, message_thread_id=topic, text=text,
                    parse_mode='HTML', reply_markup=reply_markup,
                ) is not None
            except Exception as e:
                logger.warning(f"sos agent: follow-up send failed: {e}")
                return False

    return SosAgent


_agent_lock = threading.Lock()
_agent = None


def _agent_for(bot, config, db):
    """One kicker per process (its in-flight set and slot are the point);
    rebuilt if the bot/config/db it was made for changed (tests)."""
    global _agent
    with _agent_lock:
        a = _agent
        if a is None or a.bot is not bot or a.config is not config or a.db is not db:
            a = _agent = _make_sos_agent_class()(bot, config, db)
        return a


def agent_prompt(user, report_id, source: str, fact_lines: List[str],
                 order) -> str:
    """Same contract as the protocol_down prompt: ONE first action — the
    healthcheck — then a read-only look at this client, a short answer,
    no changes. Routed to vpn-ops: none of the incident-response marker
    words, no Latin " pr…" (code-review) and "/sub" rather than the
    billing word for subscription."""
    from bot.services.alert_manager import PROTOCOL_HEALTHCHECK_CMD
    tier = 'платный' if (getattr(user, 'status', '') or '') in PAID_STATUSES else 'демо'
    uname = getattr(user, 'username', None) or getattr(user, 'chat_id', '?')
    return (
        f"SOS ОТ ПОЛЬЗОВАТЕЛЯ ({source}): «@{uname}», chat_id "
        f"{getattr(user, 'chat_id', '?')}, email в панели "
        f"{getattr(user, 'email', None) or '—'}, тир: {tier}, отчёт №{report_id or '?'}.\n"
        + '\n'.join(_plain(x) for x in fact_lines) + '\n'
        f"Аварийный каскад для него: {', '.join(order) or '—'}\n\n"
        f"Ты на entry-хосте под root. ПЕРВЫМ действием выполни ровно эту команду:\n"
        f"  {PROTOCOL_HEALTHCHECK_CMD}\n"
        f"Потом только чтением сверь этого клиента: есть ли он в панели exit с "
        f"непустыми полями и что о нём за сутки в sub_fetches, hy2_auth_log и "
        f"user_presence (bot.db). Рассуждай по выводу команд, не по догадкам.\n\n"
        f"Формат ответа — plain text, без markdown-заборов и заголовков, не "
        f"длиннее 900 символов, без описания процесса:\n"
        f"ИТОГ: есть ли у него рабочий протокол и какой.\n"
        f"ПОДОЗРЕВАЕМЫЙ: главная причина + улика (процитируй строку вывода).\n"
        f"ЧТО ОТВЕТИТЬ: 1–2 фразы для пользователя.\n"
        f"СЛЕДУЮЩАЯ КОМАНДА: одна точная команда для подтверждения или починки.\n\n"
        f"Ничего не меняй сам: никаких рестартов, правок панели, iptables и "
        f"записи в БД без явного OK админа."
    )


# ---- the service --------------------------------------------------------------------------

class SosService:
    """The flows; handlers (commands, callbacks, the mail poller) delegate."""

    # chat_id → time.time() of the last kit; tests clear it.
    _kit_sent_at: Dict[str, float] = {}
    _kit_lock = threading.Lock()

    def __init__(self, bot, db, config):
        self.bot = bot
        self.db = db
        self.config = config

    # ---- shared pieces ----

    def _facts(self, user) -> Tuple[dict, list]:
        """The failure-report facts and their lines — the SAME collector the
        "не работает" button uses (panel read capped at 3 s)."""
        from bot.handlers.callbacks.user import MyKeyAnswerHandler
        mk = MyKeyAnswerHandler(self.bot, self.db, self.config)
        facts = mk._report_facts(getattr(user, 'chat_id', None),
                                 getattr(user, 'email', None))
        lines = mk._format_report_facts(
            facts, getattr(user, 'last_country', None),
            getattr(user, 'last_asn', None), utcnow())
        return facts, lines

    def _last_report_at(self, chat_id, target: str) -> Optional[datetime]:
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT MAX(ts) FROM user_failure_reports "
                    "WHERE chat_id = ? AND target_domain = ?",
                    (str(chat_id), target),
                ).fetchone()
        except Exception as e:
            logger.warning(f"sos: rate-limit read failed: {e}")
            return None
        return _parse_ts(row[0]) if row else None

    def _record(self, user, target: str, facts: dict) -> Optional[int]:
        """The ``user_failure_reports`` row — same columns the button writes,
        so the dashboard triage list and DPIMonitor's R5 see it too."""
        status, at = facts.get('panel') or (None, None)
        sub = facts.get('sub_fetch')
        try:
            with self.db._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO user_failure_reports "
                    "(chat_id, country, asn, city, lat, lon, "
                    " last_sub_fetch_ts, last_traffic_ts, target_domain) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(getattr(user, 'chat_id', '')),
                     getattr(user, 'last_country', None),
                     getattr(user, 'last_asn', None),
                     getattr(user, 'last_city', None),
                     getattr(user, 'last_lat', None),
                     getattr(user, 'last_lon', None),
                     _sql_ts(sub) if sub else None,
                     _sql_ts(at) if status == 'ok' and at else None,
                     target),
                )
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logger.error(f"sos: report insert failed for {getattr(user, 'chat_id', '?')}: {e}")
            return None

    def _status(self, user, now: datetime) -> Tuple[dict, Optional[dict], tuple]:
        """Probe summary over the user's tier protocols in emergency order,
        and the network signal."""
        try:
            order = emergency_cascade(self.db, user)
        except Exception as e:
            logger.warning(f"sos: cascade read failed: {e}")
            order = ()
        probes = probe_summary(self.db, self.config, order, now)
        net = network_signal(self.db, getattr(user, 'last_asn', None),
                             getattr(user, 'chat_id', None), now)
        return probes, net, order

    def _kick_agent(self, user, report_id, source: str, fact_lines,
                    order) -> Tuple[str, Optional[threading.Event]]:
        """``(state, gate)`` — state ``started`` / ``busy`` / ``off`` is what
        the report says; the started worker waits for ``gate`` (set it
        once the report is posted)."""
        try:
            from bot.services.agent_factory import get_agent_url
            if not get_agent_url(self.config):
                return 'off', None
            from bot.services.alert_manager import Alert, PROTOCOL_AGENT_TIMEOUT_S
            agent = _agent_for(self.bot, self.config, self.db)
            uname = getattr(user, 'username', None) or getattr(user, 'chat_id', '?')
            alert = Alert(key=f"sos:{getattr(user, 'chat_id', '?')}", severity='warn',
                          title=f"SOS от @{uname}", min_cycles=1)
            gate = agent.gate(alert)
            t = agent._spawn_agent_worker(
                alert, None,
                prompt=agent_prompt(user, report_id, source, fact_lines, order),
                session_prefix='sos', post_to_topic=True,
                timeout=PROTOCOL_AGENT_TIMEOUT_S, slots=agent.slots,
            )
            if t is None:
                agent.drop_gate(alert)
                return 'busy', None
            return 'started', gate
        except Exception as e:
            logger.warning(f"sos: agent kick failed: {e}")
            return 'off', None

    def _report_lines(self, user, report_id, source: str, fact_lines, order,
                      net: Optional[dict]) -> List[str]:
        uname = getattr(user, 'username', None) or getattr(user, 'chat_id', '?')
        lines = [
            f"🆘 <b>SOS #{report_id or '?'}</b> · {html.escape(source)}",
            f"User: <code>@{html.escape(str(uname))}</code> "
            f"({html.escape(str(getattr(user, 'chat_id', '?')))}) · "
            f"{html.escape(str(getattr(user, 'status', '?')))}",
            *fact_lines,
            f"Аварийный каскад: {html.escape(', '.join(order)) or '—'}",
        ]
        auto = load_auto_demotions(self.db)
        asn = _norm_asn(getattr(user, 'last_asn', None))
        parts = []
        if auto['global']:
            parts.append('глобально — ' + ', '.join(
                f"{p} ({html.escape(str(m.get('evidence') or m.get('reason') or '?'))})"
                for p, m in sorted(auto['global'].items())))
        if asn and auto['asn'].get(asn):
            parts.append(f"{asn} — " + ', '.join(
                f"{p} ({html.escape(str(m.get('evidence') or m.get('reason') or '?'))})"
                for p, m in sorted(auto['asn'][asn].items())))
        lines.append("DPIMonitor: " + ('; '.join(parts) if parts else 'понижений нет'))
        try:
            from bot.services.lockdown import load_lockdown
            ld = load_lockdown(self.db)
            lines.append(
                f"Lockdown: {'АКТИВЕН' if ld.get('active') else 'выкл'} "
                f"(режим {html.escape(str(ld.get('mode')))}"
                + (f", {html.escape(str(ld.get('by')))}" if ld.get('active') else '')
                + ")")
        except Exception as e:
            logger.warning(f"sos: lockdown read failed: {e}")
        if net is not None:
            seen = _proto_list(net['working']) or 'никого'
            lines.append(f"Сеть {net['asn']} у других за {NETWORK_WINDOW_MIN} мин: "
                         f"{html.escape(seen)}; жалоб из сети за "
                         f"{NETWORK_REPORTS_WINDOW_H} ч: {net['reports']}")
        return lines

    def _post_report(self, lines: List[str]) -> None:
        group, topic = support_topic(self.config)
        if not (group and topic):
            return
        try:
            self.bot.send_message(chat_id=group, message_thread_id=topic,
                                  text='\n'.join(lines), parse_mode='HTML')
        except Exception as e:
            logger.warning(f"sos: topic report failed: {e}")

    @staticmethod
    def _agent_line(state: str) -> str:
        return {
            'started': "🤖 Диагностика агентом запущена — ответ придёт сюда.",
            'busy': "🤖 Агент занят другим SOS — эта диагностика не запущена.",
        }.get(state, "🤖 Агент не настроен — без диагностики.")

    # ---- E19: /sos ----

    def handle_sos(self, chat_id, user, *, now: Optional[datetime] = None) -> Optional[int]:
        """Answer the user, then report. Returns the report id (None when
        nothing was reported: no key, or a repeat inside the window)."""
        lang = user_lang(user)
        if not has_active_key(user):
            self.bot.send_message(chat_id=chat_id, text=no_key_text(lang))
            return None
        now = now or utcnow()
        last = self._last_report_at(user.chat_id, SOS_TARGET)
        if last is not None and (now - last).total_seconds() < SOS_RATE_LIMIT_S:
            self.bot.send_message(chat_id=chat_id,
                                  text=sos_repeat_text(lang, _ago_min(last, now)))
            return None

        # The user first: the links matter more than our bookkeeping.
        is_ios = (getattr(user, 'platform', None) or '') == 'ios'
        probes, net, order = self._status(user, now)
        try:
            urls = emergency_urls(self.config, user)
        except Exception as e:
            logger.warning(f"sos: emergency url failed: {e}")
            urls = None
        self.bot.send_message(
            chat_id=chat_id,
            text=sos_text(urls, status_lines(probes, net, lang), lang, is_ios),
            parse_mode='HTML', reply_markup=sos_keyboard(lang, is_ios),
        )

        facts, fact_lines = self._facts(user)
        report_id = self._record(user, SOS_TARGET, facts)
        agent, gate = self._kick_agent(user, report_id, '/sos', fact_lines, order)
        try:
            lines = self._report_lines(user, report_id, '/sos', fact_lines, order, net)
            lines.append(self._agent_line(agent))
            self._post_report(lines)
        finally:
            if gate is not None:
                gate.set()          # the diagnosis may follow the report now
        logger.info(f"sos: user={user.chat_id} asn={getattr(user, 'last_asn', None)} "
                    f"report_id={report_id} agent={agent}")
        return report_id

    # ---- E27: /kit ----

    def send_kit(self, chat_id, user) -> Optional[threading.Thread]:
        """Two profile files on a worker thread (the paid DE node is
        provisioned first, like /sub does; uploads take seconds and the
        polling thread handles updates one at a time). Returns the
        worker so tests can join it."""
        lang = user_lang(user)
        if not has_active_key(user):
            self.bot.send_message(chat_id=chat_id, text=no_key_text(lang))
            return None
        key = str(user.chat_id)
        now = time.time()
        cls = type(self)
        with cls._kit_lock:
            last = cls._kit_sent_at.get(key, 0.0)
            if now - last < KIT_RATE_LIMIT_S:
                self.bot.send_message(
                    chat_id=chat_id,
                    text=kit_repeat_text(lang, int((now - last) // 60)))
                return None
            cls._kit_sent_at[key] = now

        def _forget() -> None:
            with cls._kit_lock:
                if cls._kit_sent_at.get(key) == now:
                    cls._kit_sent_at.pop(key, None)

        def _worker() -> None:
            try:
                if getattr(user, 'status', None) in PAID_STATUSES:
                    try:
                        from bot.services.fallback_node import FallbackNodeService
                        FallbackNodeService(self.config).ensure_client(user)
                    except Exception as e:
                        logger.warning(f"kit: fallback provisioning failed: {e}")
                files = build_kit(self.db, self.config, user)
                self.bot.send_message(chat_id=chat_id, text=kit_intro_text(lang),
                                      parse_mode='HTML')
                captions = kit_captions(lang)
                for name, content in files:
                    sent = self.bot.send_document(
                        chat_id=chat_id, filename=name, content=content,
                        caption=captions.get(name))
                    if not sent:
                        raise RuntimeError(f"{name} was not accepted")
                logger.info(f"kit: sent to {key}")
            except Exception as e:
                logger.warning(f"kit: failed for {key}: {e}")
                _forget()   # a kit that never arrived must not block a retry
                try:
                    self.bot.send_message(chat_id=chat_id, text=kit_error_text(lang))
                except Exception:
                    pass

        t = threading.Thread(target=_worker, daemon=True, name=f"sos-kit-{key}")
        t.start()
        return t

    # ---- E29: /share ----

    def send_share(self, chat_id, lang: str) -> None:
        self.bot.send_message(chat_id=chat_id, text=share_text(lang),
                              parse_mode='HTML')

    # ---- E24: SOS by mail ----

    def last_mail_sos_at(self, addr: str) -> Optional[datetime]:
        try:
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT MAX(ts) FROM email_requests "
                    "WHERE from_addr = ? AND status = ?",
                    (addr, MAIL_SOS_STATUS),
                ).fetchone()
        except Exception as e:
            logger.warning(f"sos mail: dedup read failed: {e}")
            return None
        return _parse_ts(row[0]) if row else None

    def handle_mail_sos(self, user, addr: str, subject: str, message_id: str,
                        uid=None, *, mailer=None, now: Optional[datetime] = None) -> bool:
        """Reply with the emergency links, record, ping the topic. Returns
        True when the letter was consumed (also when deduplicated)."""
        now = now or utcnow()
        last = self.last_mail_sos_at(addr)
        if last is not None and (now - last).total_seconds() < MAIL_SOS_DEDUP_S:
            logger.info(f"sos mail: {addr[:3]}*** again within "
                        f"{MAIL_SOS_DEDUP_S // 60} min — no second reply")
            return True
        try:
            with self.db._connect() as conn:
                conn.execute(
                    "INSERT INTO email_requests "
                    "(from_addr, subject, message_id, imap_uid, known_user, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (addr, (subject or '')[:200], message_id, uid,
                     str(getattr(user, 'chat_id', '')), MAIL_SOS_STATUS),
                )
                conn.commit()
        except Exception as e:
            logger.warning(f"sos mail: request row failed: {e}")

        lang = user_lang(user)
        probes, net, order = self._status(user, now)
        try:
            urls = emergency_urls(self.config, user)
        except Exception as e:
            logger.warning(f"sos mail: emergency url failed: {e}")
            urls = None
        letter_subject, body = mail_sos_letter(
            urls, status_lines(probes, net, lang, rich=False), lang)
        if mailer is None:
            from bot.services.email_service import EmailService
            mailer = EmailService(self.config)
        try:
            sent = bool(mailer.send_notice(addr, letter_subject, body,
                                           in_reply_to=message_id or None))
        except Exception as e:
            logger.warning(f"sos mail: reply failed: {e}")
            sent = False

        facts, fact_lines = self._facts(user)
        report_id = self._record(user, MAIL_SOS_TARGET, facts)
        lines = self._report_lines(user, report_id, 'почта', fact_lines, order, net)
        lines.insert(2, f"Письмо от <code>{html.escape(addr)}</code>"
                        + (f": «{html.escape((subject or '')[:120])}»" if subject else ''))
        lines.append("✉️ Ответ с аварийной подпиской: "
                     + ("отправлен" if sent else "⚠️ не ушёл (SMTP)"))
        self._post_report(lines)
        logger.info(f"sos mail: user={getattr(user, 'chat_id', '?')} "
                    f"report_id={report_id} replied={sent}")
        return True
