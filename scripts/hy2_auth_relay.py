#!/usr/bin/env python3
"""hy2 auth relay on EXIT (IMPROVEMENT_PLAN F, A6; AGENTS.md §56).

Both hysteria instances ask this relay on exit's loopback instead of the bot:

    hysteria        auth.http.url: http://127.0.0.1:9979/api/hy2/auth
    hysteria-turbo  auth.http.url: http://127.0.0.1:9979/api/hy2t/auth

Each request goes on to the bot over the pinned hop (BOT_URL, BOT_CA_FILE: the
exit reporter's settings, AGENTS.md §53) and the bot's answer comes back as it
is. When the bot does not answer (a deploy, a restart, an outage), the relay
answers from what the bot told it, never older than the memory bound (15 min,
the bound of the bot's own memory, A4):

  * the allows the bot gave on that route, each from the moment it was asked;
  * the bot's list of the keys each route lets in right now (GET
    /api/hy2/keys: hashes only), fetched every minute, from the moment the
    fetch began. Most users authenticate a few times a day, so the allows of
    the last 15 minutes alone would miss most connections of a deploy.

A deny drops the key on that route; a deny on hy2 also on hy2t (turbo lets in
a subset of hy2). The newer word wins: a list never overrides what the bot
answered after the list was asked for. A key the bot never let in is refused,
as before. So a key the bot bans or downgrades gets in from memory at most 15
minutes after the bot last let it in.

The listener takes loopback addresses only. Tunnel users cannot reach it:
hysteria's ACL rejects 127.0.0.0/8 (AGENTS.md §52, the healthcheck's hy2_acl
guard), and the xray and sshd exits run in network namespaces of their own.

Stdlib only (exit runs Python 3.13); nothing is imported from the bot.

Environment (systemd reads /etc/exit-dpi.env and /etc/hy2-auth-relay.env as
data; no shell ever sources them):
    BOT_URL             default https://130.49.146.10:8448, the hop's listener.
                        Plain http only to loopback.
    BOT_CA_FILE         default /etc/nekovpn-hop/entry-hop.pem: the ONLY trust
                        anchor. Empty = the system's CAs (a public BOT_URL).
    DPI_REPORT_TOKEN    opens /api/hy2/keys. Without it, the allows only.
    HY2_RELAY_LISTEN    default 127.0.0.1:9979; a loopback address.
    HY2_RELAY_MEMORY_S  default 900, also the ceiling: it may only be lowered
                        (0 = no memory, the relay only passes requests on).

GET /status (loopback) prints counts and ages: no key, no hash, no id.
"""

import collections
import hashlib
import http.client
import http.server
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse

ROUTES = {"/api/hy2/auth": "hy2", "/api/hy2t/auth": "hy2t"}
# Turbo lets in a subset of the main route (paid statuses only, the same
# expiry and quota gates): a deny on hy2 holds for hy2t too, never the reverse.
COVERS = {"hy2": ("hy2", "hy2t"), "hy2t": ("hy2t",)}
KEYS_PATH = "/api/hy2/keys"
# bot/services/hy2_keys.py: HASH_PREFIX, key_hash. Mirrored, pinned by a test.
HASH_PREFIX = b"nekovpn-hy2-auth\x00"
HEX64 = re.compile(r"[0-9a-f]{64}")

DEFAULT_BOT_URL = "https://130.49.146.10:8448"
DEFAULT_CA_FILE = "/etc/nekovpn-hop/entry-hop.pem"
DEFAULT_LISTEN = "127.0.0.1:9979"
MEMORY_S = 900               # A4's bound (bot/services/hy2_auth_rows.py)
KEYS_EVERY_S = 60.0          # the key list, while the bot answers
KEYS_RETRY_S = 10.0          # ... and while it does not
KEYS_TIMEOUT_S = 15.0
KEYS_MAX_BYTES = 8 * 1024 * 1024
CONNECT_TIMEOUT_S = 3.0
ANSWER_TIMEOUT_S = 6.0       # hysteria gives the whole callback 10 s
SKIP_S = 5.0                 # after a failed connect: memory only, no new try
DENY_KEEP_S = 30.0           # > ANSWER_TIMEOUT_S and KEYS_TIMEOUT_S
IDLE_MAX = 8                 # kept-alive connections to the bot
IDLE_REUSE_S = 30.0          # an idle connection older than this is closed
MAX_BODY = 4096              # hysteria's request is ~150 bytes
MAX_ANSWER = 4096
HANDLER_IDLE_S = 120         # hysteria's client drops idle connections at 90 s
LOG_EVERY_S = 10.0

log = logging.getLogger("hy2-auth-relay")


def key_hash(key: str) -> str:
    """The bot's name for a key in its list (bot/services/hy2_keys.py)."""
    return hashlib.sha256(HASH_PREFIX + key.encode("utf-8", "replace")).hexdigest()


# ------------------------------------------------------------------ memory --

class Memory:
    """What the bot said about each (route, key hash): ``(ts, id)`` for an
    allow, ``(ts, None)`` for a deny; ``ts`` on the monotonic clock. An allow
    is stamped when the bot was asked, a deny when its answer came: the older
    end for an allow, the newer for a deny.

    A deny only has to outlast what it may contradict: an answer to an
    earlier question still on its way (ANSWER_TIMEOUT_S), a key list asked
    for before it (KEYS_TIMEOUT_S). After DENY_KEEP_S it goes, and the key is
    simply unknown, so denies of unknown keys never pile up."""

    def __init__(self, memory_s: float, clock=time.monotonic):
        self.memory_s = memory_s
        self.clock = clock
        self._lock = threading.Lock()
        self._entries = {}
        self._denies = collections.deque()   # (ts, (route, hash)), oldest first
        self.list_at = None        # start of the fetch the list in memory came from
        self.list_size = {}

    def allowed(self, route: str, h: str, ident: str, asked_at: float) -> None:
        with self._lock:
            old = self._entries.get((route, h))
            if old is None or old[0] <= asked_at:
                self._entries[(route, h)] = (asked_at, ident)

    def denied(self, route: str, h: str, answered_at: float) -> None:
        with self._lock:
            self._forget_denies(answered_at)
            for r in COVERS[route]:
                old = self._entries.get((r, h))
                if old is None or old[0] <= answered_at:
                    self._entries[(r, h)] = (answered_at, None)
                    self._denies.append((answered_at, (r, h)))

    def _forget_denies(self, now: float) -> None:
        while self._denies and now - self._denies[0][0] >= DENY_KEEP_S:
            ts, k = self._denies.popleft()
            if self._entries.get(k) == (ts, None):
                del self._entries[k]

    def begin_list(self) -> float:
        return self.clock()

    def end_list(self, started: float, keys) -> None:
        """``keys`` = ``{route: {hash: id}}`` from a good list, or None (no
        list came: the one in memory stays and ages)."""
        if keys is None:
            return
        with self._lock:
            fresh = {}
            for route, entries in keys.items():
                for h, ident in entries.items():
                    fresh[(route, h)] = (started, ident)
            for k, v in self._entries.items():
                if v[0] > started:
                    fresh[k] = v     # the bot said this after the list was asked for
            self._entries = fresh
            self.list_at = started
            self.list_size = {route: len(entries) for route, entries in keys.items()}

    def recall(self, route: str, h: str):
        """``(id, age_s)`` of an allow younger than the bound, else None."""
        now = self.clock()
        with self._lock:
            e = self._entries.get((route, h))
        if e is None or e[1] is None:
            return None
        age = now - e[0]
        if age >= self.memory_s:
            return None
        return e[1], age

    def prune(self) -> None:
        now = self.clock()
        with self._lock:
            self._forget_denies(now)
            for k in [k for k, v in self._entries.items() if now - v[0] >= self.memory_s]:
                del self._entries[k]

    def counts(self) -> dict:
        """Keys each route would let in from memory right now."""
        now = self.clock()
        out = {route: 0 for route in COVERS}
        with self._lock:
            for (route, _h), (ts, ident) in self._entries.items():
                if ident is not None and now - ts < self.memory_s:
                    out[route] += 1
        return out


# ---------------------------------------------------------------- the bot --

class ConnectFailed(Exception):
    """No connection to the bot (refused, timed out, TLS refused)."""


class TooBig(Exception):
    pass


# A kept-alive connection the bot closed while it sat idle fails like this on
# its next use: the request is tried once more on a fresh connection.
_STALE = (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError,
          ssl.SSLEOFError, ssl.SSLZeroReturnError)


def client_context(ca_file: str) -> ssl.SSLContext:
    """The pin as the ONLY trust anchor when there is one (no system CA can
    stand in for it); the name is checked; TLS 1.3, as the bot's listener."""
    if ca_file:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=ca_file)
    else:
        ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    return ctx


class Upstream:
    """The bot, over kept-alive connections: a new TLS connection per auth
    would add two round trips exit → entry to every hy2 handshake."""

    def __init__(self, url: str, ca_file: str, clock=time.monotonic):
        parts = urllib.parse.urlsplit(url)
        self.https = parts.scheme == "https"
        self.host = parts.hostname
        self.port = parts.port or (443 if self.https else 80)
        self.prefix = parts.path.rstrip("/")
        self.context = client_context(ca_file) if self.https else None
        self.clock = clock
        self._lock = threading.Lock()
        self._idle = []
        self.skip_until = float("-inf")
        self.reached_at = None       # the last HTTP answer of any kind
        self.failed_at = None
        self.last_error = None
        self._logged_at = float("-inf")
        self._quiet = 0

    def _new(self):
        if self.https:
            return http.client.HTTPSConnection(self.host, self.port, timeout=CONNECT_TIMEOUT_S,
                                               context=self.context)
        return http.client.HTTPConnection(self.host, self.port, timeout=CONNECT_TIMEOUT_S)

    def _take(self):
        now = self.clock()
        with self._lock:
            while self._idle:
                conn, since = self._idle.pop()
                if now - since < IDLE_REUSE_S:
                    return conn, True
                conn.close()
        return self._new(), False

    def _give(self, conn) -> None:
        with self._lock:
            if len(self._idle) < IDLE_MAX:
                self._idle.append((conn, self.clock()))
                return
        conn.close()

    def _left(self, deadline: float) -> float:
        left = deadline - self.clock()
        if left <= 0:
            raise TimeoutError("no answer in time")
        return left

    def _exchange(self, conn, method, path, body, headers, deadline, max_bytes):
        if conn.sock is None:
            conn.timeout = min(CONNECT_TIMEOUT_S, self._left(deadline))
            try:
                conn.connect()
            except OSError as e:          # refused, timed out, TLS refused
                raise ConnectFailed(f"{type(e).__name__}: {e}") from e
        conn.sock.settimeout(self._left(deadline))
        conn.request(method, self.prefix + path, body=body, headers=headers)
        conn.sock.settimeout(self._left(deadline))
        resp = conn.getresponse()
        data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise TooBig(f"more than {max_bytes} bytes")
        return resp.status, data, resp.isclosed() and not resp.will_close

    def request(self, method, path, body=None, headers=None, *, timeout, max_bytes):
        """``(status, body)``. Raises ConnectFailed, TooBig, OSError (a
        timeout too), http.client.HTTPException."""
        deadline = self.clock() + timeout
        conn, reused = self._take()
        try:
            try:
                status, data, keep = self._exchange(conn, method, path, body, headers or {},
                                                    deadline, max_bytes)
            except _STALE:
                if not reused:
                    raise
                conn.close()
                conn = self._new()
                status, data, keep = self._exchange(conn, method, path, body, headers or {},
                                                    deadline, max_bytes)
        except BaseException:
            conn.close()
            raise
        if keep:
            self._give(conn)
        else:
            conn.close()
        self.reached_at = self.clock()
        self.skip_until = float("-inf")
        return status, data

    def failed(self, why: str) -> None:
        """Remember the failure; log it, at most once per LOG_EVERY_S."""
        now = self.clock()
        self.failed_at = now
        self.last_error = why[:300]
        if now - self._logged_at < LOG_EVERY_S:
            self._quiet += 1
            return
        more = f" (+{self._quiet} more)" if self._quiet else ""
        self._quiet = 0
        self._logged_at = now
        log.warning(f"the bot did not answer: {self.last_error}{more}")

    def ask(self, path: str, body: bytes):
        """The bot's answer to one auth request, ``{"ok": True, "id": ...}``
        or ``{"ok": False}``, or None when there is none."""
        if self.clock() < self.skip_until:
            return None
        try:
            status, data = self.request("POST", path, body,
                                        {"Content-Type": "application/json"},
                                        timeout=ANSWER_TIMEOUT_S, max_bytes=MAX_ANSWER)
        except ConnectFailed as e:
            self.skip_until = self.clock() + SKIP_S
            self.failed(f"connect: {e}")
            return None
        except (OSError, http.client.HTTPException, TooBig, ValueError) as e:
            self.failed(f"{type(e).__name__}: {e}")
            return None
        answer = parse_answer(status, data)
        if answer is None:
            self.failed(f"no usable answer (HTTP {status})")
        return answer


def parse_answer(status: int, data: bytes):
    """What hysteria acts on, from the bot's reply; None when it is not one:
    an answer is HTTP 200 with a boolean ``ok`` (and an id for an allow)."""
    if status != 200:
        return None
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("ok"), bool):
        return None
    if not obj["ok"]:
        return {"ok": False}
    ident = obj.get("id")
    if not isinstance(ident, str) or not ident:
        return None
    return {"ok": True, "id": ident}


def parse_keys(data: bytes):
    """``{route: {hash: id}}`` from the bot's key list, or None when any of it
    is off: a list is taken whole or not at all."""
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("ok") is not True:
        return None
    out = {}
    for route in COVERS:
        entries = obj.get(route)
        if not isinstance(entries, dict):
            return None
        for h, ident in entries.items():
            if not (HEX64.fullmatch(h) and isinstance(ident, str) and 0 < len(ident) <= 256):
                return None
        out[route] = dict(entries)
    return out


# ------------------------------------------------------------------- relay --

class Relay:
    def __init__(self, upstream: Upstream, memory: Memory, token: str = ""):
        self.upstream = upstream
        self.memory = memory
        self.token = token
        self.clock = memory.clock
        self._lock = threading.Lock()
        self.served = {"bot_allow": 0, "bot_deny": 0, "memory_allow": 0, "memory_deny": 0}
        self.list_error = None
        self._denied_logged_at = float("-inf")
        self._denied_quiet = 0

    def _count(self, what: str) -> None:
        with self._lock:
            self.served[what] += 1

    def auth(self, path: str, body: bytes):
        """``(http status, answer)`` for one hysteria request."""
        route = ROUTES.get(path)
        if route is None:
            return 404, {"ok": False}
        try:
            payload = json.loads(body)
        except ValueError:
            return 400, {"ok": False}
        if not isinstance(payload, dict):
            return 400, {"ok": False}
        key = payload.get("auth")
        h = key_hash(key) if isinstance(key, str) and key else None
        asked_at = self.clock()
        answer = self.upstream.ask(path, body)
        if answer is not None:
            if answer["ok"]:
                if h:
                    self.memory.allowed(route, h, answer["id"], asked_at)
                self._count("bot_allow")
            else:
                if h:
                    self.memory.denied(route, h, self.clock())
                self._count("bot_deny")
            return 200, answer
        kept = self.memory.recall(route, h) if h else None
        if kept is not None:
            ident, age = kept
            self._count("memory_allow")
            log.info(f"{route}: allow {ident} from memory, {int(age)} s old: the bot did not answer")
            return 200, {"ok": True, "id": ident}
        self._count("memory_deny")
        self._log_denied(route)
        return 200, {"ok": False}

    def _log_denied(self, route: str) -> None:
        now = self.clock()
        if now - self._denied_logged_at < LOG_EVERY_S:
            self._denied_quiet += 1
            return
        more = f" (+{self._denied_quiet} more)" if self._denied_quiet else ""
        self._denied_quiet = 0
        self._denied_logged_at = now
        log.info(f"{route}: deny, the bot did not answer and the key is not "
                 f"remembered{more}")

    def refresh_keys(self):
        """Fetch the bot's key list. True = taken, False = the bot did not
        give one (try again soon), None = it will not (no token, 403, 404:
        try again at the normal pace)."""
        if not self.token:
            return None
        started = self.memory.begin_list()
        keys, outcome = None, False
        try:
            status, data = self.upstream.request(
                "GET", KEYS_PATH, None, {"X-DPI-Token": self.token},
                timeout=KEYS_TIMEOUT_S, max_bytes=KEYS_MAX_BYTES)
            if status == 200:
                keys = parse_keys(data)
                self.list_error = None if keys is not None else "a malformed key list"
                outcome = keys is not None
            else:
                self.list_error = f"HTTP {status}"
                outcome = None if status in (403, 404) else False
        except Exception as e:
            self.list_error = f"{type(e).__name__}: {e}"[:300]
        finally:
            self.memory.end_list(started, keys)
        if keys is not None:
            log.info(f"key list: {len(keys['hy2'])} hy2, {len(keys['hy2t'])} hy2t")
        else:
            log.warning(f"key list not taken: {self.list_error}")
        return outcome

    def run_keys(self, stop: threading.Event) -> None:
        while not stop.is_set():
            outcome = self.refresh_keys()
            self.memory.prune()
            stop.wait(KEYS_RETRY_S if outcome is False else KEYS_EVERY_S)

    def status(self) -> dict:
        now = self.clock()

        def ago(t):
            return None if t is None else round(now - t, 1)
        with self._lock:
            served = dict(self.served)
        up = self.upstream
        return {
            "ok": True,
            "memory_s": self.memory.memory_s,
            "bot": {"reached_s_ago": ago(up.reached_at), "failed_s_ago": ago(up.failed_at),
                    "last_error": up.last_error, "skipping": now < up.skip_until},
            "key_list": {"on": bool(self.token), "age_s": ago(self.memory.list_at),
                         "size": dict(self.memory.list_size), "last_error": self.list_error},
            "remembered": self.memory.counts(),
            "served": served,
        }


# ------------------------------------------------------------------ server --

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "hy2-auth-relay"
    sys_version = ""
    timeout = HANDLER_IDLE_S
    # One write per answer, no Nagle wait: hysteria holds the client's
    # handshake on it.
    wbufsize = -1
    disable_nagle_algorithm = True

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._reply(411, {"ok": False}, close=True)
        if not 0 <= n <= MAX_BODY:
            return self._reply(413, {"ok": False}, close=True)
        body = self.rfile.read(n)
        if len(body) != n:
            self.close_connection = True
            return
        status, answer = self.server.relay.auth(self.path, body)
        self._reply(status, answer, close=status != 200)

    def do_GET(self):
        if self.path != "/status":
            return self._reply(404, {"ok": False})
        self._reply(200, self.server.relay.status())

    def _reply(self, status, obj, close=False):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass                      # hysteria logs every connection already


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128      # a reconnect storm after a hysteria restart

    def __init__(self, address, relay: Relay):
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.relay = relay
        super().__init__(address, Handler)


# ------------------------------------------------------------------ config --

def parse_listen(value: str):
    """``(host, port)`` of a loopback address, or raise ValueError."""
    host, sep, port = value.strip().rpartition(":")
    if not sep:
        raise ValueError(f"HY2_RELAY_LISTEN {value!r} is not host:port")
    host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"HY2_RELAY_LISTEN {value!r}: the host must be an IP address")
    # An IPv4-mapped address reads as loopback on some Pythons and not on
    # others: one spelling of each address, the plain one.
    if getattr(addr, "ipv4_mapped", None) is not None or not addr.is_loopback:
        raise ValueError(f"HY2_RELAY_LISTEN {value!r} is not a loopback address: "
                         f"tunnel users must not reach the relay")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"HY2_RELAY_LISTEN {value!r}: bad port")
    return str(addr), int(port)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_config(env):
    """``(config dict, problems)``; any problem = the relay does not start."""
    problems = []
    cfg = {"bot_url": (env.get("BOT_URL") or DEFAULT_BOT_URL).strip().rstrip("/"),
           "ca_file": env.get("BOT_CA_FILE", DEFAULT_CA_FILE).strip(),
           "token": (env.get("DPI_REPORT_TOKEN") or "").strip(),
           "memory_s": MEMORY_S, "host": None, "port": None}
    try:
        cfg["host"], cfg["port"] = parse_listen(env.get("HY2_RELAY_LISTEN") or DEFAULT_LISTEN)
    except ValueError as e:
        problems.append(str(e))
    raw = (env.get("HY2_RELAY_MEMORY_S") or "").strip()
    if raw:
        if not raw.isdigit():
            problems.append(f"HY2_RELAY_MEMORY_S {raw!r} is not a whole number of seconds")
        elif int(raw) > MEMORY_S:
            log.warning(f"HY2_RELAY_MEMORY_S {raw} is above the bound: {MEMORY_S} s it is")
        else:
            cfg["memory_s"] = int(raw)
    parts = urllib.parse.urlsplit(cfg["bot_url"])
    if parts.scheme == "http":
        if not _is_loopback(parts.hostname or ""):
            problems.append(f"BOT_URL {cfg['bot_url']} is plain http to another host: every "
                            f"user's key would cross the internet in clear (AGENTS.md §53)")
    elif parts.scheme != "https" or not parts.hostname:
        problems.append(f"BOT_URL {cfg['bot_url']!r} is not an http(s) URL")
    elif cfg["ca_file"]:
        try:
            client_context(cfg["ca_file"])
        except (OSError, ssl.SSLError) as e:
            problems.append(f"BOT_CA_FILE {cfg['ca_file']} does not load ({e}); "
                            f"empty = the system's CAs")
    return cfg, problems


def main(env=None) -> int:
    env = os.environ if env is None else env
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                        stream=sys.stderr)
    cfg, problems = load_config(env)
    if problems:
        for p in problems:
            log.error(p)
        return 2
    memory = Memory(cfg["memory_s"])
    relay = Relay(Upstream(cfg["bot_url"], cfg["ca_file"]), memory, cfg["token"])
    try:
        server = Server((cfg["host"], cfg["port"]), relay)
    except OSError as e:
        log.error(f"cannot listen on {cfg['host']}:{cfg['port']}: {e}")
        return 2
    stop = threading.Event()
    threading.Thread(target=relay.run_keys, args=(stop,), name="key-list", daemon=True).start()

    def _stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    log.info(f"listening on {cfg['host']}:{cfg['port']}; bot {cfg['bot_url']} "
             f"(trusts {cfg['ca_file'] or 'the system CAs'}); memory {cfg['memory_s']} s; "
             f"key list {'on' if cfg['token'] else 'off (no DPI_REPORT_TOKEN)'}")
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
