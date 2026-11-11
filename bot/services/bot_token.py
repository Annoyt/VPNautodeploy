"""Change the bot's Telegram token from the dashboard (Settings → «Токен бота»).

The case it is for: the token leaked, @BotFather revokes it and issues a new
one, and the bot has to run on the new one without ssh, from a phone. From
the revoke on, the old token is dead and the bot is silent. The dashboard
still works through that window: it signs and checks its links itself.

How a change goes (``BotTokenChanger.apply``):

1. The page sealed the token (bot/utils/token_envelope.py), because the hop
   from exit's Caddy to entry:8080 is plain HTTP.
2. The format, and the bot id before the colon: only a token of the SAME bot
   is taken. Another bot is a move, not a change of token: its users would
   have to /start it, the forum group would need it as an admin, Stars
   payments stay with the old one, the app's t.me links name the old one.
   And a dashboard that installs any token would let whoever holds a leaked
   admin link run this service on a bot of their own, and sign admin links
   for it.
3. getMe with the new token through TG_PROXY_URLS (entry cannot reach
   api.telegram.org directly). Telegram must accept it.
4. The subscription secret must be pinned (bot/config/secret_store.py).
   Otherwise every user's /sub link changes along with the token.
5. ``secrets/bot_token.json``, an audit row, and a restart in place
   (``request_restart``: the shutdown `docker stop` runs, then an exec of the
   same command, so the container's restart policy does not matter). The
   answer carries an admin link signed with the new token, and the page
   keeps working once the new process is up.

After the restart the bot posts to TOPIC_AI that the token changed, and what
is left by hand: BOT_TOKEN in /opt/vpn-bot/.env on exit (the memory
watchdog's messages) and on entry (so that .env is the truth again).
"""

import hmac
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Callable, Optional, Tuple

import requests

from bot.config import secret_store
from bot.config.secret_store import bot_id_of, token_sha256
from bot.utils.admin_token import make_admin_token
from bot.utils.token_envelope import ChallengeStore, EnvelopeError

logger = logging.getLogger(__name__)

TOKEN_CHECK_TIMEOUT_S = 8
# Short: after a revoke the status should say so within seconds.
STATUS_CACHE_S = 15
RESTART_DELAY_S = 1.5
# The shutdown waits for the polling loop; a long poll can hold it ~40 s.
RESTART_GRACE_S = 45


@dataclass
class TokenCheck:
    """What getMe said about a token. ``detail`` never carries the token."""
    status: str  # 'ok' | 'rejected' | 'unreachable'
    bot: dict = field(default_factory=dict)  # id, username, first_name when ok
    detail: str = ''


def _proxy_label(entry: Optional[str]) -> str:
    if entry is None:
        return 'env'
    if entry == 'direct':
        return 'direct'
    return entry.split('@')[-1]


def check_token(token: str, *, timeout: float = TOKEN_CHECK_TIMEOUT_S) -> TokenCheck:
    """getMe with ``token``, through each TG_PROXY_URLS entry once.

    The bot's own client retries with growing pauses and feeds the outage
    alert; a check from the dashboard must do neither. A request error is
    reported by its type only: requests puts the URL, and the token in it,
    into its messages.
    """
    pool = [p.strip() for p in os.environ.get('TG_PROXY_URLS', '').split(',')
            if p.strip()]
    entries = pool or [None]  # None: requests' own HTTPS_PROXY, as the client
    url = f'https://api.telegram.org/bot{token}/getMe'
    last = 'нет ответа'
    session = requests.Session()
    if pool:
        session.trust_env = False
    try:
        for entry in entries:
            kwargs = {'timeout': timeout}
            if entry is not None:
                kwargs['proxies'] = ({} if entry == 'direct'
                                     else {'http': entry, 'https': entry})
            try:
                r = session.get(url, **kwargs)
            except requests.RequestException as e:
                last = f'{type(e).__name__} через {_proxy_label(entry)}'
                continue
            try:
                body = r.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if (r.status_code == 200 and body.get('ok') is True
                    and isinstance(body.get('result'), dict)):
                res = body['result']
                return TokenCheck('ok', {
                    'id': res.get('id'),
                    'username': str(res.get('username') or ''),
                    'first_name': str(res.get('first_name') or ''),
                })
            description = str(body.get('description') or f'HTTP {r.status_code}')[:160]
            if r.status_code in (401, 404):
                return TokenCheck('rejected', detail=description)
            last = f'{description} через {_proxy_label(entry)}'
    finally:
        session.close()
    return TokenCheck('unreachable', detail=last)


def _str_attr(config, name: str, default: str = '') -> str:
    value = getattr(config, name, default)
    return value if isinstance(value, str) else default


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _error(code: str, message: str) -> dict:
    return {'ok': False, 'error': code, 'message': message}


class BotTokenChanger:
    """The dashboard's view of the bot token, and the one way to change it."""

    def __init__(self, config, db, *, check: Callable = check_token,
                 restart: Optional[Callable] = None,
                 challenges: Optional[ChallengeStore] = None):
        self.config = config
        self.db = db
        self._check = check
        self._restart = restart or request_restart
        self._challenges = challenges or ChallengeStore()
        self._lock = threading.Lock()
        self._applied = False
        self._status_cache: Optional[Tuple[float, str, TokenCheck]] = None

    @property
    def data_dir(self) -> str:
        return secret_store.data_dir_of(self.db.db_path)

    # ---- reading ----

    def _source(self) -> str:
        source = _str_attr(self.config, 'BOT_TOKEN_SOURCE', 'env')
        return source if source in ('env', 'dashboard') else 'env'

    def _current_check(self, token: str) -> TokenCheck:
        sha = token_sha256(token)
        cached = self._status_cache
        if cached and cached[1] == sha and time.monotonic() - cached[0] < STATUS_CACHE_S:
            return cached[2]
        check = self._check(token)
        self._status_cache = (time.monotonic(), sha, check)
        return check

    def sub_secret_pinned(self) -> bool:
        """The links' key is on disk and is the one this process signs with."""
        state, secret = secret_store.read_sub_secret(self.data_dir)
        current = _str_attr(self.config, 'SUB_TOKEN_SECRET')
        return (state == 'ok' and bool(current)
                and hmac.compare_digest(secret.encode(), current.encode()))

    def status(self, *, admin_id: str, plain_hop: bool, link: bool = False) -> dict:
        token = _str_attr(self.config, 'BOT_TOKEN')
        check = self._current_check(token) if token else TokenCheck('rejected', detail='нет токена')
        source = self._source()
        rec = secret_store.read_bot_token_record(self.data_dir) if source == 'dashboard' else None
        out = {
            'bot_id': bot_id_of(token),
            'username': check.bot.get('username') or None,
            'token_status': check.status,
            'token_detail': check.detail,
            'source': source,
            'changed_at': (rec or {}).get('set_at'),
            'changed_by': (rec or {}).get('set_by'),
            'sub_secret_pinned': self.sub_secret_pinned(),
            'plain_hop': plain_hop,
            'restarting': self._applied,
        }
        if link:
            # A link that keeps opening the dashboard after the revoke: a
            # Mini App reopened then gets initData signed with the NEW token,
            # which this process cannot check yet.
            base = _str_attr(self.config, 'WEBAPP_URL').rstrip('/')
            if base and token:
                out['link'] = f'{base}/?admin_token={make_admin_token(token, str(admin_id))}'
        return out

    def challenge(self) -> dict:
        return self._challenges.new()

    # ---- changing ----

    def _ensure_pinned(self) -> Tuple[bool, str]:
        """Pin the links' key if this process can still tell what it is."""
        if self.sub_secret_pinned():
            return True, ''
        state, _ = secret_store.read_sub_secret(self.data_dir)
        if state == 'broken':
            return False, 'файл секрета не читается'
        if state == 'ok':
            return False, 'файл секрета не совпадает с тем, чем подписаны ссылки'
        # Missing. The links are signed with BOT_TOKEN only while the bot
        # runs on .env's token and no dashboard change ever happened here.
        if self._source() != 'env' or secret_store.dashboard_history(self.data_dir):
            return False, 'файл секрета пропал после прошлой смены токена'
        token = _str_attr(self.config, 'BOT_TOKEN')
        if not token:
            return False, 'нет токена'
        try:
            secret_store.write_sub_secret(self.data_dir, token)
        except OSError as e:
            return False, f'не записался: {type(e).__name__}'
        self.config.SUB_TOKEN_SECRET = token
        logger.warning("subscription secret pinned at the token change")
        return True, ''

    def apply(self, admin_id: str, envelope) -> Tuple[int, dict]:
        """``(http_status, body)``. Changes nothing unless every check passes."""
        if not isinstance(envelope, dict):
            return 400, _error('envelope', 'Пустой запрос — обнови страницу.')
        with self._lock:
            if self._applied:
                return 409, _error('restarting', 'Токен уже заменён — бот перезапускается.')
            try:
                raw = self._challenges.open(envelope.get('id'), envelope.get('A'),
                                            envelope.get('ct'), envelope.get('tag'))
                token = raw.decode('utf-8').strip()
            except (EnvelopeError, UnicodeDecodeError) as e:
                # EnvelopeError says why; a decode error would quote the bytes
                why = e if isinstance(e, EnvelopeError) else type(e).__name__
                logger.warning(f"bot token: sealed token rejected ({why})")
                return 400, _error('envelope', 'Не удалось расшифровать токен — '
                                   'обнови страницу и вставь его ещё раз.')
            new_id = bot_id_of(token)
            if new_id is None:
                return 400, _error('format', 'Это не токен бота: он выглядит как '
                                   '123456789:AAE… (цифры, двоеточие, 35 символов).')
            current = _str_attr(self.config, 'BOT_TOKEN')
            if hmac.compare_digest(token.encode(), current.encode()):
                return 400, _error('same', 'Бот уже работает на этом токене.')
            current_id = bot_id_of(current)
            if current_id is None or new_id != current_id:
                return 409, _error('other_bot', (
                    f'Это токен другого бота (id {new_id}), а этот — id {current_id}. '
                    'Здесь меняется только токен того же бота (после Revoke в '
                    '@BotFather). Переезд на другого бота — это /start у всех '
                    'юзеров, права в группе, платежи и ссылки t.me — через .env.'))
            check = self._check(token)
            if check.status == 'rejected':
                return 400, _error('rejected', f'Telegram не принял токен ({check.detail}). '
                                   'Ничего не менял.')
            if check.status != 'ok':
                return 503, _error('unreachable', f'До Telegram не достучаться ({check.detail}). '
                                   'Ничего не менял — попробуй ещё раз.')
            if check.bot.get('id') != current_id:
                return 409, _error('other_bot', 'Telegram говорит, что это другой бот. '
                                   'Ничего не менял.')
            pinned, why = self._ensure_pinned()
            if not pinned:
                logger.critical(f"bot token change refused: subscription secret ({why})")
                return 500, _error('pin', f'Секрет ссылок подписок не закреплён ({why}): '
                                   'с новым токеном у всех юзеров сменились бы ссылки. '
                                   'Ничего не менял.')
            username = check.bot.get('username') or ''
            record = {
                'v': 1,
                'token': token,
                'bot_id': new_id,
                'username': username,
                # .env's value as this process read it: the new token applies
                # for as long as .env still says this.
                'env_sha256': token_sha256(_str_attr(self.config, 'BOT_TOKEN_ENV', current)),
                'set_at': _now_iso(),
                'set_by': str(admin_id),
                'announce': True,
            }
            try:
                secret_store.write_bot_token_record(self.data_dir, record)
            except OSError as e:
                logger.error(f"bot token: could not save ({type(e).__name__})")
                return 500, _error('write', f'Не смог сохранить токен ({type(e).__name__}). '
                                   'Ничего не менял.')
            self._applied = True
            source_before = self._source()
        try:
            self.db.log_admin_action(str(admin_id), 'bot_token_set', f'bot:{new_id}',
                                     f'@{username}; before: {source_before}')
        except Exception as e:
            logger.warning(f"bot token: audit row failed: {e}")
        logger.warning(f"bot token changed from the dashboard by {admin_id} "
                       f"(bot {new_id} @{username}); restarting")
        self._restart(RESTART_DELAY_S)
        return 200, {
            'ok': True,
            'bot': {'id': new_id, 'username': username},
            'admin_token': make_admin_token(token, str(admin_id)),
            'restart_in': RESTART_DELAY_S,
        }


# ---------- startup ----------

def on_startup(config) -> None:
    """Before anything signs a link: move a stale dashboard token aside and
    pin the subscription secret on the first start."""
    data_dir = secret_store.data_dir_of(config.DB_PATH)
    source = getattr(config, 'BOT_TOKEN_SOURCE', 'env')
    if source == 'dashboard':
        rec = secret_store.read_bot_token_record(data_dir) or {}
        logger.info(f"bot token: set from the dashboard {rec.get('set_at')} "
                    f"by {rec.get('set_by')} (.env still holds the old one)")
    elif secret_store.read_bot_token_record(data_dir) is not None:
        moved = secret_store.retire_bot_token_record(data_dir)
        logger.warning("bot token: BOT_TOKEN in .env changed after the dashboard "
                       f"set one — running on .env, the dashboard's moved to {moved}")
    state, _ = secret_store.read_sub_secret(data_dir)
    if state == 'ok':
        return
    if state == 'broken':
        logger.critical("subscription secret unreadable — /sub links are signed with "
                        "the bot token until secrets/sub_token_secret.json is fixed")
        return
    if source != 'env' or secret_store.dashboard_history(data_dir):
        logger.critical("subscription secret missing after a dashboard token change — "
                        "/sub links signed with the current token do not match the old "
                        "ones; restore secrets/sub_token_secret.json from a volume backup")
        return
    if not config.BOT_TOKEN:
        return
    try:
        secret_store.write_sub_secret(data_dir, config.BOT_TOKEN)
    except OSError as e:
        logger.error(f"subscription secret: could not pin ({type(e).__name__}); "
                     "the dashboard refuses a token change until it is")
        return
    config.SUB_TOKEN_SECRET = config.BOT_TOKEN
    logger.info("subscription secret pinned: /sub links no longer follow the bot token")


def announce_text(rec: dict) -> str:
    when = str(rec.get('set_at') or '').replace('T', ' ').replace('Z', ' UTC')
    name = f"@{escape(str(rec.get('username')))}" if rec.get('username') else 'Бот'
    return (
        f"🔑 <b>Токен бота заменён</b> из дашборда ({escape(when)}, админ "
        f"<code>{escape(str(rec.get('set_by') or '?'))}</code>).\n"
        f"{name} работает на новом токене. Ссылки подписок и ключи юзеров "
        "не изменились.\n\n"
        "Осталось руками, не срочно — впиши новый токен в BOT_TOKEN:\n"
        "• <code>/opt/vpn-bot/.env</code> на exit: по нему пишет сюда watchdog памяти;\n"
        "• <code>/opt/vpn-bot/.env</code> на entry: до этого бот берёт токен из "
        "дашборда, а .env хранит старый."
    )


def announce_if_pending(bot, config) -> bool:
    """Once, after a restart onto a dashboard token: tell the admin topic."""
    if getattr(config, 'BOT_TOKEN_SOURCE', 'env') != 'dashboard':
        return False
    data_dir = secret_store.data_dir_of(config.DB_PATH)
    rec = secret_store.read_bot_token_record(data_dir)
    if not rec or not rec.get('announce'):
        return False
    text = announce_text(rec)
    group = getattr(config, 'FORUM_GROUP_ID', None)
    topic = getattr(config, 'TOPIC_AI', 0) or 0
    targets = []
    if group and topic:
        targets.append({'chat_id': group, 'message_thread_id': topic})
    if getattr(config, 'SUPER_ADMIN_ID', None):
        targets.append({'chat_id': config.SUPER_ADMIN_ID})  # fallback only
    for target in targets:
        try:
            if bot.send_message(text=text, parse_mode='HTML', **target) is not None:
                break
        except Exception as e:
            logger.warning(f"bot token: announcement failed: {e}")
    else:
        return False
    rec['announce'] = False
    try:
        secret_store.write_bot_token_record(data_dir, rec)
    except OSError as e:
        logger.warning(f"bot token: could not mark the announcement sent ({type(e).__name__})")
    return True


# ---------- restart in place ----------

_RESTART = {'requested': False, 'timers': []}


def request_restart(delay: float = RESTART_DELAY_S) -> None:
    """Restart this process in place once the answer is out.

    After ``delay``: SIGTERM to itself, the shutdown `docker stop` runs;
    ``main`` then ends with ``reexec_if_requested``. If the shutdown hangs
    for RESTART_GRACE_S, exec anyway.
    """
    def _go():
        _RESTART['requested'] = True
        force = threading.Timer(RESTART_GRACE_S, _force_exec)
        force.daemon = True
        _RESTART['timers'].append(force)
        force.start()
        os.kill(os.getpid(), signal.SIGTERM)

    timer = threading.Timer(delay, _go)
    timer.daemon = True
    _RESTART['timers'].append(timer)
    timer.start()


def cancel_restart() -> None:
    """Forget a requested restart and stop its timers (tests)."""
    for timer in _RESTART['timers']:
        timer.cancel()
    _RESTART['timers'].clear()
    _RESTART['requested'] = False


def restart_requested() -> bool:
    return _RESTART['requested']


def _exec_argv() -> list:
    orig = list(getattr(sys, 'orig_argv', []) or [])
    return [sys.executable] + (orig[1:] if len(orig) > 1 else ['-m', 'bot.main'])


def _exec() -> None:
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    os.execv(sys.executable, _exec_argv())


def _force_exec() -> None:
    if not _RESTART['requested']:
        return
    logger.error(f"restart: shutdown not done in {RESTART_GRACE_S}s — exec anyway")
    _exec()


def reexec_if_requested() -> None:
    """The last thing ``main`` does: start over in this process if a token
    change asked for it (the same command, the same environment)."""
    if not _RESTART['requested']:
        return
    logger.warning("restart: exec of a fresh bot process for the new token")
    _exec()
