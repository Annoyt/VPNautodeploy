"""Server-side rule lists for Clash / mihomo clients (FlClash) —
IMPROVEMENT_PLAN E1 (lists as ``rule-providers``), E2 (managed from the
bot, user complaints queue), E9 (complaints become a rule on their own).

Why lists and not the profile
-----------------------------
FlClash refreshes the PROFILE once a day by default, but honours the
``interval`` of every ``rule-provider`` in it. So everything urgent —
"this site got blocked yesterday", "this RU site must go direct" — lives
in four small lists the profile only POINTS at
(``build_clash_config`` emits the providers when WEBAPP_URL is set);
the bot serves them at ``/lists/clash/<name>.yaml`` and clients pull
them hourly. Hiddify drops a sing-box profile's rules altogether
(IMPROVEMENT_PLAN §E), so the lists exist for the Clash profile only —
the sing-box profile is untouched.

  always-proxy    domains → VPN      the operator's "always tunnel" list
  blocked-recent  domains → VPN      recently blocked sites (users' complaints)
  ru-direct       domains → DIRECT   RU sites that must not see the exit IP
  ru-direct-ip    CIDRs   → DIRECT   same, by address (rule is no-resolve)

The VPN lists sit above the built-in always-proxy geosites, the DIRECT
lists above ``GEOSITE,category-ru`` — the operator's word beats the
generic databases, and a VPN list beats a DIRECT one.

Storage (app_settings, JSON strings — never hand-edit while the bot runs)
------------------------------------------------------------------------
  rule_lists        {"<list>": {"<entry>": {"ts", "by", "note"?}}}
                    entries normalised (lower-case, punycode, no www.,
                    network address for CIDRs); a hand-edited list of
                    strings is accepted on read
  rule_list_queue   {"next_id": int,
                     "items": [{"id", "domain", "chat_id", "ts", "status"}],
                     "ignored": {"<domain>": {"ts", "by"}}}
                    users' "this site does not open" complaints; status
                    pending → added | ignored; pruned after 7 days
  rule_list_auto_threshold   E9 threshold, default 2, "0"/"off" = no auto

Every change of a list is one ``admin_actions`` row (actor = the admin's
id, or ``rule_lists`` for E9). Readers never raise: a broken JSON value
reads as empty lists; the HTTP endpoint answers 503 instead so clients
keep the copy they cached (an empty answer would wipe it for an hour).
Writers read strictly — a locked sqlite read taken for "no row" would
write a fresh structure over the real lists (the hazard
DPIMonitor._read_setting guards against).
"""

import ipaddress
import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------- the lists ----------

@dataclass(frozen=True)
class ListSpec:
    name: str
    behavior: str       # mihomo rule-provider behavior: 'domain' | 'ipcidr'
    target: str         # Clash target: the 'VPN' group or DIRECT
    no_resolve: bool    # append ',no-resolve' to the RULE-SET rule
    title: str          # for the operator surface


LISTS: Dict[str, ListSpec] = {
    'always-proxy': ListSpec('always-proxy', 'domain', 'VPN', False,
                             'всегда через VPN'),
    'blocked-recent': ListSpec('blocked-recent', 'domain', 'VPN', False,
                               'заблокировано недавно → VPN'),
    'ru-direct': ListSpec('ru-direct', 'domain', 'DIRECT', False,
                          'RU напрямую (домены)'),
    'ru-direct-ip': ListSpec('ru-direct-ip', 'ipcidr', 'DIRECT', True,
                             'RU напрямую (IP/CIDR, no-resolve)'),
}
LIST_NAMES = tuple(LISTS)
VPN_LISTS = tuple(n for n, s in LISTS.items() if s.target == 'VPN')
AUTO_LIST = 'blocked-recent'
MAX_ENTRIES = 1000          # per list; operator lists are tens of entries

SETTING_KEY = 'rule_lists'
BACKUP_KEY = 'rule_lists.bak'
QUEUE_KEY = 'rule_list_queue'
THRESHOLD_KEY = 'rule_list_auto_threshold'

# admin_actions vocabulary
ACTION_ADD = 'rule_list_add'
ACTION_RM = 'rule_list_rm'
ACTION_IGNORE = 'rule_list_ignore'
ACTION_THRESHOLD = 'rule_list_auto_threshold'
ACTION_AUTO_ADD = 'auto_add'
AUTO_ACTOR = 'rule_lists'

# HTTP / profile contract (rule-providers in the Clash profile)
LIST_URL_PATH = '/lists/clash/{name}.yaml'
PROVIDER_INTERVAL_S = 3600
PROVIDER_CACHE_MAX_AGE_S = 300

# queue (E2) and auto (E9)
QUEUE_RETENTION_DAYS = 7
QUEUE_MAX_ITEMS = 500
USER_DAILY_LIMIT = 5        # complaints per user per 24 h
IGNORE_DAYS = 30            # an admin's "no" holds the auto rule off this long
AUTO_WINDOW_HOURS = 24
DEFAULT_AUTO_THRESHOLD = 2
# Never auto-routed through the VPN: for a RU-zone site that does not
# open, the usual cure is ru-direct (it refuses the foreign exit IP),
# not the tunnel — and two demo accounts must not be able to push
# vk.ru / bank domains through the exit for every FlClash user. The
# card still lands in the topic; the admin decides with one tap.
RU_ZONE_TLDS = frozenset({'ru', 'su', 'xn--p1ai'})   # xn--p1ai = .рф

_LOCK = threading.RLock()


class ListsUnreadable(RuntimeError):
    """app_settings could not be READ (sqlite error, not a missing row)."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_ts(value) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------- validation ----------

_SCHEME_RE = re.compile(r'^[a-z][a-z0-9+.-]*://')
_LABEL_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')
_TLD_RE = re.compile(r'^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$')
_DOMAIN_STRIP = ' \t\r\n.,;:!?()[]{}<>"\'`«»“”„'
_CIDR_STRIP = ' \t\r\n,;"\'`«»“”„'
_SAFE_ENTRY_RE = re.compile(r'^[a-z0-9.:/-]+$')   # what may reach the YAML
_MIN_PREFIX = {4: 8, 6: 16}


def normalize_domain(raw) -> Optional[str]:
    """``'https://WWW.Example.com:443/a?b'`` → ``'example.com'``; None if
    it is not a public-looking host name. Accepts URLs, ``*.``/``+.``
    wildcards, a trailing dot and IDN (→ punycode). Rejects IP
    addresses, single labels, numeric TLDs and anything with a char a
    host name cannot carry — whatever passes here is written into the
    YAML the clients parse, and a broken provider is a broken profile.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip().strip(_DOMAIN_STRIP).lower()
    s = _SCHEME_RE.sub('', s)
    s = re.split(r'[/?#\\]', s, maxsplit=1)[0]
    s = s.rsplit('@', 1)[-1]
    if s.startswith('['):           # IPv6 literal
        return None
    s = s.split(':', 1)[0]
    for prefix in ('*.', '+.'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    s = s.strip('.')
    if s.startswith('www.') and s.count('.') >= 2:
        s = s[4:]
    if not s or '..' in s:
        return None
    if not s.isascii():
        # every real TLD is 2+ characters — keeps prose like «т.е.» out
        if len(s.rsplit('.', 1)[-1]) < 2:
            return None
        try:
            s = s.encode('idna').decode('ascii')
        except (UnicodeError, ValueError):
            return None
    if len(s) > 253:
        return None
    labels = s.split('.')
    if len(labels) < 2:
        return None
    if not all(_LABEL_RE.match(label) for label in labels):
        return None
    if not _TLD_RE.match(labels[-1]):
        return None
    return s


def _check_cidr(raw) -> Tuple[Optional[str], Optional[str]]:
    if not isinstance(raw, str):
        return None, 'пусто'
    s = raw.strip().strip(_CIDR_STRIP)
    try:
        net = ipaddress.ip_network(s, strict=False)
    except ValueError:
        return None, f'не IP и не CIDR: {s[:60]}'
    floor = _MIN_PREFIX[net.version]
    if net.prefixlen < floor:
        return None, (f'слишком широкая сеть /{net.prefixlen} — '
                      f'минимум /{_MIN_PREFIX[4]} для IPv4, /{_MIN_PREFIX[6]} для IPv6')
    return str(net), None


def normalize_cidr(raw) -> Optional[str]:
    """``'1.2.3.4'`` → ``'1.2.3.4/32'``, ``'1.2.3.5/24'`` → ``'1.2.3.0/24'``;
    None for garbage and for nets broader than /8 (v4) or /16 (v6) — a
    ``0.0.0.0/0`` in ru-direct-ip would send every connection past the
    tunnel."""
    return _check_cidr(raw)[0]


def _looks_like_ip(raw: str) -> bool:
    try:
        ipaddress.ip_network(raw.strip().strip(_CIDR_STRIP), strict=False)
        return True
    except ValueError:
        return False


def validate_entry(name: str, raw) -> Tuple[Optional[str], Optional[str]]:
    """``(normalised entry, None)`` or ``(None, human reason)``."""
    spec = LISTS.get(name)
    if spec is None:
        return None, f'нет такого списка: {name}'
    if not isinstance(raw, str) or not raw.strip():
        return None, 'пусто'
    if spec.behavior == 'ipcidr':
        return _check_cidr(raw)
    if _looks_like_ip(raw):
        return None, 'это IP/сеть — для адресов есть список ru-direct-ip'
    value = normalize_domain(raw)
    if value is None:
        return None, f'не похоже на домен: {raw.strip()[:60]}'
    return value, None


def extract_domains(text, limit: int = 3) -> List[str]:
    """Host names found in a user's free-text message ("не открывается
    https://rutracker.org/forum", "youtube.com, vk.com"), normalised,
    deduplicated, at most ``limit``."""
    if not isinstance(text, str):
        return []
    found: List[str] = []
    for token in re.split(r'[\s,;]+', text):
        if '.' not in token:
            continue
        value = normalize_domain(token)
        if value and value not in found:
            found.append(value)
            if len(found) >= limit:
                break
    return found


def is_ru_zone(domain: str) -> bool:
    return domain.rsplit('.', 1)[-1] in RU_ZONE_TLDS


def find_cover(entries: Iterable[str], name: str, entry: str) -> Optional[str]:
    """The entry of ``entries`` that already matches ``entry``: the same
    value, a parent domain (``+.example.com`` covers ``a.example.com``)
    or a supernet. None when nothing does."""
    entries = set(entries)
    spec = LISTS[name]
    if spec.behavior == 'domain':
        labels = entry.split('.')
        for i in range(len(labels) - 1):
            candidate = '.'.join(labels[i:])
            if candidate in entries:
                return candidate
        return None
    try:
        net = ipaddress.ip_network(entry, strict=False)
    except ValueError:
        return None
    for other in sorted(entries):
        try:
            onet = ipaddress.ip_network(other, strict=False)
        except ValueError:
            continue
        if onet.version == net.version and net.subnet_of(onet):
            return other
    return None


# ---------- reading / writing app_settings ----------

def _read_raw(db, key: str) -> Optional[str]:
    """One app_settings value; None when the row is missing. Raises
    ``ListsUnreadable`` on a sqlite error — ``Database.get_setting``
    folds that into its default, which a writer must never see as "no
    lists yet". Non-sqlite dbs (fakes, mocks) go through get_setting."""
    connect = getattr(db, '_connect', None)
    if connect is None:
        return db.get_setting(key)
    try:
        conn = connect()
    except sqlite3.Error as e:
        raise ListsUnreadable(f'connect: {e}') from e
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error as e:
        raise ListsUnreadable(f'{key}: {e}') from e
    except Exception:
        return db.get_setting(key)
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not row:
        return None
    value = row[0]
    return value if isinstance(value, str) else None


def _empty_lists() -> Dict[str, Dict[str, dict]]:
    return {name: {} for name in LIST_NAMES}


def parse_lists(raw) -> Dict[str, Dict[str, dict]]:
    """``rule_lists`` JSON → ``{list: {entry: meta}}`` with every entry
    re-validated (a hand-edit typo is dropped, never served). Missing /
    empty = empty lists. Raises ValueError on JSON that is not an
    object — the caller decides between "empty" and "keep the old"."""
    lists = _empty_lists()
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return lists
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('rule_lists is not a JSON object')
    for name in LIST_NAMES:
        value = data.get(name)
        if isinstance(value, dict):
            pairs = list(value.items())
        elif isinstance(value, list):
            pairs = [(e, {}) for e in value]
        else:
            continue
        bucket = lists[name]
        for entry, meta in pairs:
            norm, _ = validate_entry(name, entry)
            if norm is None:
                logger.warning(f"rule_lists[{name}]: dropping invalid entry {str(entry)[:60]!r}")
                continue
            if norm in bucket or len(bucket) >= MAX_ENTRIES:
                continue
            bucket[norm] = dict(meta) if isinstance(meta, dict) else {}
    return lists


def load_lists_strict(db) -> Dict[str, Dict[str, dict]]:
    """Raises ``ListsUnreadable`` (sqlite) or ``ValueError`` (bad JSON)."""
    return parse_lists(_read_raw(db, SETTING_KEY))


def load_lists(db) -> Dict[str, Dict[str, dict]]:
    """Never raises: any problem reads as four empty lists."""
    try:
        return load_lists_strict(db)
    except (ListsUnreadable, ValueError) as e:
        logger.warning(f"rule_lists unreadable ({e}); treating as empty")
        return _empty_lists()
    except Exception as e:      # a mock / fake db in a unit test
        logger.warning(f"rule_lists read failed ({e}); treating as empty")
        return _empty_lists()


def lists_health(db) -> Optional[str]:
    """None when ``rule_lists`` reads cleanly, else why it does not —
    for the operator card (clients get 503 meanwhile)."""
    try:
        load_lists_strict(db)
        return None
    except ListsUnreadable as e:
        return f'не читается: {e}'
    except ValueError as e:
        return f'битый JSON: {e}'


def _load_lists_for_write(db) -> Tuple[Dict[str, Dict[str, dict]], Optional[str]]:
    """Strict read for a read-modify-write. A sqlite error propagates
    (abort, write nothing); unparseable JSON is backed up to
    ``rule_lists.bak`` and the write starts from empty lists — the
    operator's command must still work, and the bad value is kept."""
    raw = _read_raw(db, SETTING_KEY)
    try:
        return parse_lists(raw), None
    except ValueError as e:
        db.set_setting(BACKUP_KEY, raw if isinstance(raw, str) else '')
        logger.warning(f"rule_lists: bad JSON backed up to {BACKUP_KEY} ({e})")
        return _empty_lists(), f'rule_lists был битым JSON — сохранён в {BACKUP_KEY}, списки начаты заново'


def _write_lists(db, lists) -> bool:
    return bool(db.set_setting(SETTING_KEY, json.dumps(lists, ensure_ascii=False)))


def _audit(db, actor: str, action: str, target: str, details: str) -> None:
    try:
        db.log_admin_action(str(actor), action, target, details)
    except Exception as e:
        logger.warning(f"rule_lists: audit write failed ({action} {target}): {e}")


@dataclass
class ListChange:
    """Outcome of add_entry / remove_entry. ``status``: added | removed |
    exists | covered | absent | invalid | full | unknown_list | error.
    ``detail``: the covering entry or the reason; ``warning``: something
    the operator should know although the change went through."""
    status: str
    name: str
    entry: Optional[str] = None
    detail: str = ''
    warning: str = ''

    @property
    def changed(self) -> bool:
        return self.status in ('added', 'removed')


def conflicts(lists, name: str, entry: str) -> List[Tuple[str, str]]:
    """``[(other list, covering entry)]`` that route ``entry`` the other
    way: a VPN list for a DIRECT entry and vice versa. VPN lists are
    evaluated first, so a DIRECT entry under a VPN one has no effect."""
    spec = LISTS[name]
    out = []
    for other, ospec in LISTS.items():
        if ospec.target == spec.target or ospec.behavior != spec.behavior:
            continue
        cover = find_cover(lists.get(other) or {}, other, entry)
        if cover:
            out.append((other, cover))
    return out


def add_entry(db, name: str, raw, *, actor: str, note: str = '',
              action: str = ACTION_ADD, now: Optional[datetime] = None) -> ListChange:
    """Validate, deduplicate (exact or covered by a parent / supernet),
    cap at MAX_ENTRIES, persist, audit. Adding a domain to a VPN list
    also settles its pending complaints and lifts an admin's "ignore"
    — the operator has just decided the other way."""
    if name not in LISTS:
        return ListChange('unknown_list', name, detail=f'нет такого списка: {name}')
    entry, err = validate_entry(name, raw)
    if entry is None:
        return ListChange('invalid', name, detail=err or '')
    now = now or utcnow()
    with _LOCK:
        try:
            lists, warning = _load_lists_for_write(db)
        except ListsUnreadable as e:
            return ListChange('error', name, entry, f'не прочитать {SETTING_KEY}: {e}')
        bucket = lists[name]
        cover = find_cover(bucket, name, entry)
        if cover == entry:
            return ListChange('exists', name, entry)
        if cover:
            return ListChange('covered', name, entry, cover)
        if len(bucket) >= MAX_ENTRIES:
            return ListChange('full', name, entry, f'в списке уже {MAX_ENTRIES} записей')
        meta = {'ts': _iso(now), 'by': str(actor)}
        if note:
            meta['note'] = note[:200]
        bucket[entry] = meta
        if not _write_lists(db, lists):
            return ListChange('error', name, entry, 'запись в app_settings не удалась')
        if not warning:
            clash = conflicts(lists, name, entry)
            if clash:
                warning = '; '.join(
                    f'также в {other} ({cover})' for other, cover in clash)
    _audit(db, actor, action, f'{name}:{entry}', note)
    if name in VPN_LISTS and LISTS[name].behavior == 'domain':
        settle_domain(db, entry, 'added', by=str(actor), now=now, unignore=True)
    return ListChange('added', name, entry, warning=warning or '')


def remove_entry(db, name: str, raw, *, actor: str, note: str = '',
                 now: Optional[datetime] = None) -> ListChange:
    """Remove one entry. Removing a domain from a VPN list also holds
    the E9 auto rule off it for IGNORE_DAYS — otherwise the next two
    complaints would put back what the operator just took out."""
    if name not in LISTS:
        return ListChange('unknown_list', name, detail=f'нет такого списка: {name}')
    entry, err = validate_entry(name, raw)
    if entry is None:
        return ListChange('invalid', name, detail=err or '')
    now = now or utcnow()
    with _LOCK:
        try:
            lists, warning = _load_lists_for_write(db)
        except ListsUnreadable as e:
            return ListChange('error', name, entry, f'не прочитать {SETTING_KEY}: {e}')
        bucket = lists[name]
        if entry not in bucket:
            cover = find_cover(bucket, name, entry)
            return ListChange('absent', name, entry, cover or '')
        del bucket[entry]
        if not _write_lists(db, lists):
            return ListChange('error', name, entry, 'запись в app_settings не удалась')
    _audit(db, actor, ACTION_RM, f'{name}:{entry}', note)
    if name in VPN_LISTS and LISTS[name].behavior == 'domain':
        ignore_domain(db, entry, actor=str(actor), now=now, audit=False)
    return ListChange('removed', name, entry, warning=warning or '')


# ---------- provider files (HTTP) and the Clash profile ----------

def provider_yaml(name: str, entries: Iterable[str]) -> str:
    """A mihomo rule-provider file (``format: yaml``): ``payload:`` + one
    single-quoted item per entry — ``'+.example.com'`` (the domain and
    every subdomain) or a CIDR. Empty list = ``payload: []`` (mihomo
    accepts it: an empty rule set that matches nothing). Generated by
    hand — PyYAML is not in the image; the entries are validated, and
    the character filter below keeps a quote out of the YAML even if a
    future caller skips validation."""
    spec = LISTS[name]
    items = []
    for entry in entries:
        if not isinstance(entry, str) or not _SAFE_ENTRY_RE.match(entry):
            continue
        items.append(f"+.{entry}" if spec.behavior == 'domain' else entry)
    head = f"# NekoVPN rule list: {name} ({len(items)})"
    if not items:
        return f"{head}\npayload: []\n"
    body = "\n".join(f"  - '{item}'" for item in items)
    return f"{head}\npayload:\n{body}\n"


def serve_provider(db, name: str) -> Tuple[int, str]:
    """``(HTTP status, body)`` for ``GET /lists/clash/<name>.yaml``:
    200 + YAML, 404 for an unknown name, 503 when the lists cannot be
    read — a failed fetch keeps the client's cached copy, an empty 200
    would wipe it until the next interval."""
    if name not in LISTS:
        return 404, 'Not found'
    try:
        lists = load_lists_strict(db)
    except (ListsUnreadable, ValueError) as e:
        logger.warning(f"rule list {name}: serving 503 ({e})")
        return 503, 'Temporarily unavailable'
    return 200, provider_yaml(name, lists[name])


def clash_rule_providers(base_url: str) -> dict:
    """The ``rule-providers`` block of the Clash profile, one http
    provider per list on the bot's public URL."""
    base = (base_url or '').strip().rstrip('/')
    return {
        name: {
            'type': 'http',
            'behavior': spec.behavior,
            'format': 'yaml',
            'url': base + LIST_URL_PATH.format(name=name),
            'path': f'./lists/{name}.yaml',
            'interval': PROVIDER_INTERVAL_S,
        }
        for name, spec in LISTS.items()
    }


def clash_rule(name: str) -> str:
    spec = LISTS[name]
    return f"RULE-SET,{name},{spec.target}" + (',no-resolve' if spec.no_resolve else '')


def clash_list_rules(target: str) -> List[str]:
    """``RULE-SET`` rules of every list routed to ``target``, in list order."""
    return [clash_rule(n) for n, s in LISTS.items() if s.target == target]


def clash_quic_direct_rules() -> List[str]:
    """The RU QUIC:443 carve-out for the DIRECT domain lists — the same
    reason as for ``category-ru``: a site whose TCP goes direct must not
    see its HTTP/3 arrive from the exit IP (the VK "VPN detected" banner)."""
    return [f"AND,((NETWORK,UDP),(DST-PORT,443),(RULE-SET,{n})),DIRECT"
            for n, s in LISTS.items() if s.target == 'DIRECT' and s.behavior == 'domain']


# ---------- complaints queue (E2) ----------

def _empty_queue() -> dict:
    return {'next_id': 1, 'items': [], 'ignored': {}}


def parse_queue(raw) -> dict:
    """Tolerant: bad JSON / wrong shapes → an empty queue; items without
    an id, domain, chat_id or a parseable ts are dropped; a bare list is
    read as the items."""
    queue = _empty_queue()
    if not isinstance(raw, str) or not raw.strip():
        return queue
    try:
        data = json.loads(raw)
    except ValueError:
        logger.warning(f"{QUEUE_KEY}: not valid JSON, treating as empty")
        return queue
    if isinstance(data, list):
        data = {'items': data}
    if not isinstance(data, dict):
        return queue
    max_id = 0
    for it in data.get('items') or []:
        if not isinstance(it, dict):
            continue
        try:
            item_id = int(it.get('id'))
        except (TypeError, ValueError):
            continue
        domain = normalize_domain(it.get('domain'))
        chat_id = it.get('chat_id')
        if not domain or chat_id in (None, '') or _parse_ts(it.get('ts')) is None:
            continue
        status = it.get('status') if it.get('status') in ('pending', 'added', 'ignored') else 'pending'
        item = {'id': item_id, 'domain': domain, 'chat_id': str(chat_id),
                'ts': str(it['ts']), 'status': status}
        for k in ('settled_by', 'settled_at'):
            if it.get(k):
                item[k] = str(it[k])
        queue['items'].append(item)
        max_id = max(max_id, item_id)
    ignored = data.get('ignored')
    if isinstance(ignored, dict):
        for domain, meta in ignored.items():
            d = normalize_domain(domain)
            meta = meta if isinstance(meta, dict) else {}
            if d and _parse_ts(meta.get('ts')) is not None:
                queue['ignored'][d] = {'ts': str(meta['ts']), 'by': str(meta.get('by') or '')}
    try:
        next_id = int(data.get('next_id') or 0)
    except (TypeError, ValueError):
        next_id = 0
    queue['next_id'] = max(next_id, max_id + 1, 1)
    return queue


def load_queue(db) -> dict:
    """Never raises."""
    try:
        return parse_queue(_read_raw(db, QUEUE_KEY))
    except Exception as e:
        logger.warning(f"{QUEUE_KEY} unreadable ({e}); treating as empty")
        return _empty_queue()


def _prune_queue(queue: dict, now: datetime) -> None:
    cutoff = now - timedelta(days=QUEUE_RETENTION_DAYS)
    items = [it for it in queue['items'] if (_parse_ts(it['ts']) or now) >= cutoff]
    queue['items'] = items[-QUEUE_MAX_ITEMS:]
    ign_cutoff = now - timedelta(days=IGNORE_DAYS)
    queue['ignored'] = {d: m for d, m in queue['ignored'].items()
                        if (_parse_ts(m.get('ts')) or now) >= ign_cutoff}


def _write_queue(db, queue) -> bool:
    return bool(db.set_setting(QUEUE_KEY, json.dumps(queue, ensure_ascii=False)))


def record_complaint(db, domain: str, chat_id, *,
                     now: Optional[datetime] = None) -> Tuple[str, Optional[dict]]:
    """Queue one "site does not open" complaint. Returns ``(status,
    item)``: ``queued`` (new item), ``duplicate`` (this user's pending
    complaint about this domain — the existing item), ``limit`` (more
    than USER_DAILY_LIMIT complaints from this user in 24 h), ``invalid``
    or ``error`` (item None)."""
    domain = normalize_domain(domain)
    if not domain or chat_id in (None, ''):
        return 'invalid', None
    chat_id = str(chat_id)
    now = now or utcnow()
    with _LOCK:
        try:
            queue = parse_queue(_read_raw(db, QUEUE_KEY))
        except ListsUnreadable as e:
            logger.warning(f"{QUEUE_KEY}: read failed ({e})")
            return 'error', None
        _prune_queue(queue, now)
        day_ago = now - timedelta(hours=24)
        mine = [it for it in queue['items'] if it['chat_id'] == chat_id]
        for it in mine:
            if it['domain'] == domain and it['status'] == 'pending':
                return 'duplicate', it
        if sum(1 for it in mine if (_parse_ts(it['ts']) or now) >= day_ago) >= USER_DAILY_LIMIT:
            return 'limit', None
        item = {'id': queue['next_id'], 'domain': domain, 'chat_id': chat_id,
                'ts': _iso(now), 'status': 'pending'}
        queue['next_id'] += 1
        queue['items'].append(item)
        _prune_queue(queue, now)
        if not _write_queue(db, queue):
            return 'error', None
    return 'queued', item


def find_item(db, item_id) -> Optional[dict]:
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return None
    return next((it for it in load_queue(db)['items'] if it['id'] == item_id), None)


def settle_domain(db, domain: str, status: str, *, by: str,
                  now: Optional[datetime] = None, unignore: bool = False) -> int:
    """Mark every pending complaint about ``domain`` as ``status``
    (added | ignored). Returns how many. ``unignore`` drops an admin's
    earlier "ignore" for the domain."""
    now = now or utcnow()
    with _LOCK:
        try:
            queue = parse_queue(_read_raw(db, QUEUE_KEY))
        except ListsUnreadable as e:
            logger.warning(f"{QUEUE_KEY}: settle {domain} skipped ({e})")
            return 0
        n = 0
        for it in queue['items']:
            if it['domain'] == domain and it['status'] == 'pending':
                it.update(status=status, settled_by=by, settled_at=_iso(now))
                n += 1
        dropped = unignore and queue['ignored'].pop(domain, None) is not None
        if n or dropped:
            _write_queue(db, queue)
    return n


def ignore_domain(db, domain: str, *, actor: str, now: Optional[datetime] = None,
                  audit: bool = True, note: str = '') -> int:
    """An admin's "no": pending complaints → ignored, and the E9 auto
    rule keeps off the domain for IGNORE_DAYS. Returns the number of
    complaints settled."""
    domain = normalize_domain(domain)
    if not domain:
        return 0
    now = now or utcnow()
    with _LOCK:
        try:
            queue = parse_queue(_read_raw(db, QUEUE_KEY))
        except ListsUnreadable as e:
            logger.warning(f"{QUEUE_KEY}: ignore {domain} skipped ({e})")
            return 0
        _prune_queue(queue, now)
        n = 0
        for it in queue['items']:
            if it['domain'] == domain and it['status'] == 'pending':
                it.update(status='ignored', settled_by=str(actor), settled_at=_iso(now))
                n += 1
        queue['ignored'][domain] = {'ts': _iso(now), 'by': str(actor)}
        _write_queue(db, queue)
    if audit:
        _audit(db, actor, ACTION_IGNORE, f'{AUTO_LIST}:{domain}', note)
    return n


def pending_summary(queue: dict, now: datetime,
                    window_hours: int = AUTO_WINDOW_HOURS) -> List[dict]:
    """Pending complaints grouped by domain, most users first: ``[{domain,
    users (all pending), users_window (distinct, inside the auto window),
    last_ts}]``."""
    cutoff = now - timedelta(hours=window_hours)
    groups: Dict[str, dict] = {}
    for it in queue.get('items') or []:
        if it.get('status') != 'pending':
            continue
        g = groups.setdefault(it['domain'], {'domain': it['domain'], 'users': set(),
                                             'users_window': set(), 'last_ts': ''})
        g['users'].add(it['chat_id'])
        ts = _parse_ts(it['ts'])
        if ts is not None and ts >= cutoff:
            g['users_window'].add(it['chat_id'])
        g['last_ts'] = max(g['last_ts'], it['ts'])
    # most users first; among equals the freshest complaint first
    out = sorted(groups.values(), key=lambda g: g['last_ts'], reverse=True)
    out.sort(key=lambda g: -len(g['users']))
    return out


# ---------- E9: complaints become a rule ----------

@dataclass(frozen=True)
class AutoAdd:
    domain: str
    chat_ids: Tuple[str, ...]
    evidence: str


def parse_threshold(raw) -> int:
    """``rule_list_auto_threshold``: N distinct users; ``0``/``off`` =
    the auto rule is off; missing or junk = DEFAULT_AUTO_THRESHOLD."""
    if raw is None:
        return DEFAULT_AUTO_THRESHOLD
    s = str(raw).strip().lower()
    if s in ('off', 'no', 'false', 'disabled'):
        return 0
    try:
        n = int(s)
    except ValueError:
        return DEFAULT_AUTO_THRESHOLD
    return n if n >= 0 else DEFAULT_AUTO_THRESHOLD


def auto_threshold(db) -> int:
    try:
        return parse_threshold(db.get_setting(THRESHOLD_KEY))
    except Exception:
        return DEFAULT_AUTO_THRESHOLD


def set_auto_threshold(db, value: int, *, actor: str) -> bool:
    value = max(0, int(value))
    ok = bool(db.set_setting(THRESHOLD_KEY, str(value)))
    if ok:
        _audit(db, actor, ACTION_THRESHOLD, THRESHOLD_KEY, str(value))
    return ok


def auto_guard(domain: str, lists, ignored, now: datetime) -> Optional[str]:
    """Why E9 must NOT add ``domain`` (None = free to add). Pure.
    Already tunnelled by a VPN list; sent direct by the operator
    (ru-direct — the operator's word wins); ignored by an admin within
    IGNORE_DAYS; a RU-zone domain (see RU_ZONE_TLDS)."""
    for name in VPN_LISTS:
        cover = find_cover((lists or {}).get(name) or {}, name, domain)
        if cover:
            return f'уже в {name}' + (f' ({cover})' if cover != domain else '')
    cover = find_cover((lists or {}).get('ru-direct') or {}, 'ru-direct', domain)
    if cover:
        return f'в ru-direct ({cover}) — решение оператора'
    meta = (ignored or {}).get(domain)
    if isinstance(meta, dict):
        ts = _parse_ts(meta.get('ts'))
        if ts is not None and ts >= now - timedelta(days=IGNORE_DAYS):
            until = ts + timedelta(days=IGNORE_DAYS)
            return f'админ отклонил — авто не добавит до {until:%d.%m}'
    if is_ru_zone(domain):
        return 'RU-зона — авто не трогает (такому сайту чаще нужен ru-direct)'
    return None


def _complaints_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return 'жалоба'
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return 'жалобы'
    return 'жалоб'


def decide_auto_adds(items, lists, ignored, *, threshold: int, now: datetime,
                     only: Optional[Iterable[str]] = None,
                     window_hours: int = AUTO_WINDOW_HOURS) -> List[AutoAdd]:
    """PURE decision of E9: every domain with at least ``threshold``
    DISTINCT users among the PENDING complaints of the last
    ``window_hours`` and no ``auto_guard`` objection. ``only`` restricts
    the look to the domains just complained about (the caller runs this
    right after queueing, so a backlog never floods the list at once).
    ``threshold < 1`` = the auto rule is off."""
    if threshold < 1:
        return []
    only = set(only) if only is not None else None
    cutoff = now - timedelta(hours=window_hours)
    users: Dict[str, set] = {}
    for it in items or []:
        if not isinstance(it, dict) or it.get('status', 'pending') != 'pending':
            continue
        domain, chat_id = it.get('domain'), it.get('chat_id')
        ts = _parse_ts(it.get('ts'))
        if not domain or chat_id in (None, '') or ts is None or ts < cutoff:
            continue
        if only is not None and domain not in only:
            continue
        users.setdefault(domain, set()).add(str(chat_id))
    out = []
    for domain in sorted(users):
        n = len(users[domain])
        if n < threshold:
            continue
        if auto_guard(domain, lists, ignored, now):
            continue
        out.append(AutoAdd(
            domain, tuple(sorted(users[domain])),
            f'{n} {_complaints_word(n)} от разных юзеров за {window_hours} ч '
            f'(порог {threshold})',
        ))
    return out


def run_auto(db, *, only: Optional[Iterable[str]] = None,
             now: Optional[datetime] = None) -> List[AutoAdd]:
    """Apply E9: decide on the current queue and lists, add each winner
    to blocked-recent (one ``admin_actions('rule_lists', 'auto_add')``
    row each), settle its complaints. Returns what was added. Nothing
    is decided on lists it cannot read — the ru-direct guard would be
    blind. A threshold of 0 (off) decides nothing."""
    threshold = auto_threshold(db)
    now = now or utcnow()
    with _LOCK:
        try:
            lists = load_lists_strict(db)
            queue = parse_queue(_read_raw(db, QUEUE_KEY))
        except (ListsUnreadable, ValueError) as e:
            logger.warning(f"rule_lists auto: skipped, state unreadable ({e})")
            return []
        decisions = decide_auto_adds(queue['items'], lists, queue['ignored'],
                                     threshold=threshold, now=now, only=only)
        applied = []
        for d in decisions:
            note = f"{d.evidence}; юзеры: {', '.join(d.chat_ids)}"
            change = add_entry(db, AUTO_LIST, d.domain, actor=AUTO_ACTOR, note=note,
                               action=ACTION_AUTO_ADD, now=now)
            if change.status == 'added':
                applied.append(d)
            else:
                logger.warning(f"rule_lists auto: {d.domain} not added ({change.status} {change.detail})")
    return applied
