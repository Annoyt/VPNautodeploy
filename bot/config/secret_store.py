"""Secrets the bot keeps next to its database (2026-11-11).

Two files in ``<dirname(DB_PATH)>/secrets/``; on entry that is the
``vpn-bot-data`` volume, ``/var/lib/vpn-bot/secrets``. The directory is
0700 and the files 0600, owned by the bot's user. Root on the host can
read them, as it can read .env, and the volume backups carry them: they
are as secret as .env. Never print them (hermes/AGENTS.md, the skills).

``sub_token_secret.json`` holds the key of the subscription tokens. A /sub
    URL is ``HMAC-SHA256(key, uuid)[:32]`` (``SubscriptionService.
    derive_token``), and every link a user holds comes from it: /sub,
    /open, /app, /kit, the Clash providers, the /probe telemetry, the
    NekoVPN app's mirrors. Until 2026-11-11 the key WAS BOT_TOKEN, so a new
    bot token changed all of them at once. The bot now pins the key at
    startup (``bot.services.bot_token.on_startup``, from the token in .env
    at that first start), and from then on the bot token can change while
    no link does. The file is a copy of that bot token.

``bot_token.json`` holds a bot token set from the dashboard (Settings →
    «Токен бота»). The container cannot edit the host's .env, so the token
    lives here. It is used instead of .env's BOT_TOKEN for as long as .env
    still holds the value it replaced (``env_sha256``). Whoever edits .env
    after that has the newer word: .env wins, and the bot moves this file
    aside at startup.

Stdlib only: ``Settings`` reads this while ``bot.config`` is still being
imported.
"""

import hashlib
import json
import logging
import os
import re
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

SECRETS_DIRNAME = 'secrets'
SUB_SECRET_FILE = 'sub_token_secret.json'
BOT_TOKEN_FILE = 'bot_token.json'
SUPERSEDED_PREFIX = 'bot_token.superseded-'

# <bot id>:<secret>. BotFather issues 8-10 digits and 35 characters; both
# sides are a little wider, so a longer id or secret later still passes.
TOKEN_RE = re.compile(r'^([0-9]{5,15}):[A-Za-z0-9_-]{30,64}$')
_SHA256_HEX_RE = re.compile(r'^[0-9a-f]{64}$')


class SecretFileError(Exception):
    """The file exists but cannot be read, or holds something else."""


def bot_id_of(token) -> Optional[int]:
    """The bot id in front of the colon, or None if ``token`` is not one."""
    if not isinstance(token, str):
        return None
    m = TOKEN_RE.match(token)
    return int(m.group(1)) if m else None


def token_sha256(token) -> str:
    return hashlib.sha256((token or '').encode('utf-8')).hexdigest()


def data_dir_of(db_path) -> str:
    return os.path.dirname(db_path or '') or '.'


def secrets_dir(data_dir: str) -> str:
    return os.path.join(data_dir, SECRETS_DIRNAME)


def _path(data_dir: str, name: str) -> str:
    return os.path.join(secrets_dir(data_dir), name)


def _read_json(path: str) -> Optional[dict]:
    """The object in ``path``; None when the file does not exist."""
    try:
        with open(path, encoding='utf-8') as fh:
            obj = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise SecretFileError(f'{os.path.basename(path)}: {type(e).__name__}') from None
    if not isinstance(obj, dict):
        raise SecretFileError(f'{os.path.basename(path)}: not a JSON object')
    return obj


# ---------- the bot token set from the dashboard ----------

def read_bot_token_record(data_dir: str) -> Optional[dict]:
    """``bot_token.json`` if it holds a well-formed record, else None.

    A broken file is logged and treated as absent: the bot then runs on
    .env's token, and the dashboard shows where the token came from.
    """
    try:
        rec = _read_json(_path(data_dir, BOT_TOKEN_FILE))
    except SecretFileError as e:
        logger.error(f"bot token: {e} — running on .env's BOT_TOKEN")
        return None
    if rec is None:
        return None
    if bot_id_of(rec.get('token')) is None or not (
            isinstance(rec.get('env_sha256'), str)
            and _SHA256_HEX_RE.match(rec['env_sha256'])):
        logger.error("bot token: bot_token.json is malformed — running on .env's BOT_TOKEN")
        return None
    return rec


def effective_bot_token(env_token, data_dir: str) -> Tuple[str, str, Optional[dict]]:
    """``(token, source, record)``: the token the bot runs on.

    ``source`` is ``'dashboard'`` while .env still holds the value the
    dashboard replaced, else ``'env'``. ``record`` is ``bot_token.json``
    whether it applies or not (``on_startup`` moves a stale one aside).

    MIRROR: ``scripts/hermes_model_guard.py`` ``effective_bot_token`` repeats
    this rule (it runs as root and must not import the bot's code). Change
    one, change the other: tests/unit/test_hermes_model_guard.py runs both
    on the same files.
    """
    rec = read_bot_token_record(data_dir)
    if rec is not None and rec['env_sha256'] == token_sha256(env_token):
        return rec['token'], 'dashboard', rec
    return env_token or '', 'env', rec


def dashboard_history(data_dir: str) -> bool:
    """A token was ever set from the dashboard here (in effect or moved aside)."""
    try:
        names = os.listdir(secrets_dir(data_dir))
    except FileNotFoundError:
        return False
    return any(n == BOT_TOKEN_FILE or n.startswith(SUPERSEDED_PREFIX) for n in names)


def write_bot_token_record(data_dir: str, record: dict) -> str:
    return write_secret(data_dir, BOT_TOKEN_FILE, record)


def retire_bot_token_record(data_dir: str) -> Optional[str]:
    """Move ``bot_token.json`` aside; its name, or None if there was none.

    Kept, not deleted: when .env got the wrong value, the token the
    dashboard set is still here to put back.
    """
    src = _path(data_dir, BOT_TOKEN_FILE)
    stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
    dest = _path(data_dir, f'{SUPERSEDED_PREFIX}{stamp}.json')
    try:
        os.replace(src, dest)
    except FileNotFoundError:
        return None
    _fsync_dir(secrets_dir(data_dir))
    return os.path.basename(dest)


# ---------- the key of the subscription tokens ----------

def read_sub_secret(data_dir: str) -> Tuple[str, Optional[str]]:
    """``(state, secret)``: ``'ok'``, ``'missing'`` or ``'broken'``.

    ``'broken'`` (the file exists and cannot be used) must never be
    re-pinned: that would replace the real key with the current token.
    """
    try:
        rec = _read_json(_path(data_dir, SUB_SECRET_FILE))
    except SecretFileError as e:
        logger.critical(f"subscription secret: {e}")
        return 'broken', None
    if rec is None:
        return 'missing', None
    secret = rec.get('secret')
    if not isinstance(secret, str) or not secret:
        logger.critical("subscription secret: sub_token_secret.json has no secret")
        return 'broken', None
    return 'ok', secret


def write_sub_secret(data_dir: str, secret: str) -> str:
    return write_secret(data_dir, SUB_SECRET_FILE, {
        'secret': secret,
        'pinned_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })


def sub_token_key(config) -> str:
    """The key ``SubscriptionService.derive_token`` signs with: the pinned
    secret, or BOT_TOKEN where none is pinned (tests, a first start that
    could not write the file) — the same key the links had before."""
    secret = getattr(config, 'SUB_TOKEN_SECRET', None)
    if isinstance(secret, str) and secret:
        return secret
    return getattr(config, 'BOT_TOKEN', '') or ''


# ---------- writing ----------

def write_secret(data_dir: str, name: str, obj: dict) -> str:
    """Write ``obj`` as JSON to ``secrets/<name>``: a 0600 file in a 0700
    directory, replaced atomically, on disk before this returns."""
    d = secrets_dir(data_dir)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)  # makedirs' mode is masked by the umask
    path = os.path.join(d, name)
    tmp = f'{path}.tmp-{os.getpid()}-{time.monotonic_ns()}'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            os.fchmod(fh.fileno(), 0o600)  # the open's mode is masked too
            json.dump(obj, fh, ensure_ascii=False, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    _fsync_dir(d)
    return path


def _fsync_dir(d: str) -> None:
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
