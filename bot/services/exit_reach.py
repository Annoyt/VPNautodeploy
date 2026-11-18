"""Can entry still open a NEW connection to exit? (AGENTS.md §61)

On 2026-11-18, between 13:10 and 13:20 UTC, the TSPU at entry's provider
started dropping every new flow from Russia to exit's address. Flows opened
before then kept working, and so did flows that exit opened towards entry
(§59). The probes saw it 45 minutes later: they run every 15 minutes and
judge three runs. By then every user was without a connection.

This check opens a fresh connection every minute, from the bot container,
whose traffic leaves by entry's address like haproxy's. It goes to exit
itself (the direct path) and through each transit in ``EXIT_TRANSITS``. A
connection is the one haproxy makes for a user: TCP, a PROXY v2 header,
then a TLS handshake with Reality's SNI. Reality does not know us, so it
completes the handshake with its dest's certificate. Data has to move both
ways, which also catches a block that lets TCP connect and then goes
silent.

The PROXY header names ``MARK_SRC`` (RFC 5737 TEST-NET-1, never a client)
as the source. exit's xray logs each of our handshakes as a rejected
connection from it, and ``WebAppServer._dpi_exit_rows`` drops those lines,
so the check adds nothing to the DPI rows or their alerts.

Stdlib only; nothing here talks to the database or Telegram.
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import re
import socket
import ssl
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

KEY_PREFIX = 'reach:'
EXIT_KEY = 'reach:exit'
VIA_PREFIX = 'reach:via:'

# exit's Reality port: haproxy's backend for users.
EXIT_PORT = 443
CONNECT_TIMEOUT_S = 4.0
HANDSHAKE_TIMEOUT_S = 4.0
# Two failed minutes in a row before the alert fires; one lost handshake is
# not a block.
MIN_CYCLES = 2
# The exit DPI reporter posts every 5 minutes (exit → entry). A report this
# recent proves exit is up.
REPORT_FRESH_MIN = 15
# /protocols shows the last check only while it is this recent.
SNAPSHOT_FRESH_S = 300

PROXY_V2_SIGNATURE = b'\r\n\r\n\x00\r\nQUIT\n'
MARK_SRC = '192.0.2.44'
MARK_DST = '192.0.2.45'
MARK_SPORT = 44444

_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,15}$')
_LOGGED_PROBLEMS: set = set()


@dataclass(frozen=True)
class Target:
    key: str        # the alert key: reach:exit, reach:via:<name>
    name: str       # 'exit', or the transit's name
    host: str       # an IP literal: a name would need a lookup with no timeout
    port: int
    sni: str        # '' = no SNI
    via: bool       # a transit to exit, not exit itself

    @property
    def addr(self) -> str:
        host = f'[{self.host}]' if ':' in self.host else self.host
        return f'{host}:{self.port}'


@dataclass(frozen=True)
class Result:
    ok: bool
    stage: str      # 'ok', 'tcp' (no connection) or 'tls' (connected, no handshake)
    ms: int         # time taken, a failure's included
    error: str = ''  # 'timeout', 'refused', 'reset', 'unreachable', or a short text


# ----- configuration -----

def _ip_literal(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def parse_transits(raw) -> Tuple[List[Tuple[str, str, int]], List[str]]:
    """``EXIT_TRANSITS``: comma-separated ``name=host:port``, the host an IP
    literal (an IPv6 one in brackets). Returns ``(entries, problems)``; a bad
    entry is left out and named in ``problems``, a repeated name keeps its
    first entry."""
    entries: List[Tuple[str, str, int]] = []
    problems: List[str] = []
    if not isinstance(raw, str):
        return entries, problems
    seen = set()
    for item in raw.split(','):
        item = item.strip()
        if not item:
            continue
        name, sep, addr = item.partition('=')
        name = name.strip().lower()
        addr = addr.strip()
        if not sep or not _NAME_RE.match(name):
            problems.append(f'{item!r}: имя — латиница, цифры, дефис, до 16 знаков')
            continue
        if addr.startswith('['):
            host, _, rest = addr[1:].partition(']')
            port_s = rest[1:] if rest.startswith(':') else ''
        else:
            host, _, port_s = addr.rpartition(':')
        ip = _ip_literal(host)
        if ip is None or (':' in ip) != addr.startswith('['):
            problems.append(f'{item!r}: нужен IP-адрес (IPv6 в скобках)')
            continue
        try:
            port = int(port_s)
        except ValueError:
            port = 0
        if not 0 < port < 65536:
            problems.append(f'{item!r}: нужен порт 1–65535')
            continue
        if name in seen:
            problems.append(f'{item!r}: имя {name} уже есть')
            continue
        seen.add(name)
        entries.append((name, ip, port))
    return entries, problems


def targets(config) -> List[Target]:
    """exit itself (``EXIT_NODE_IP``) and each transit of ``EXIT_TRANSITS``,
    all with Reality's SNI (``SNI_VALUE``). No exit address and no transit
    = nothing to watch."""
    sni = getattr(config, 'SNI_VALUE', '')
    sni = sni.strip() if isinstance(sni, str) else ''
    out: List[Target] = []
    exit_ip = _ip_literal(getattr(config, 'EXIT_NODE_IP', ''))
    if exit_ip:
        out.append(Target(EXIT_KEY, 'exit', exit_ip, EXIT_PORT, sni, False))
    entries, problems = parse_transits(getattr(config, 'EXIT_TRANSITS', ''))
    for p in problems:
        if p not in _LOGGED_PROBLEMS:     # the check runs every minute
            _LOGGED_PROBLEMS.add(p)
            logger.warning(f'EXIT_TRANSITS: пропущено {p}')
    for name, host, port in entries:
        out.append(Target(VIA_PREFIX + name, name, host, port, sni, True))
    return out


# ----- one connection -----

def proxy_header() -> bytes:
    """PROXY v2, command PROXY, TCP over IPv4, source ``MARK_SRC``."""
    body = (socket.inet_aton(MARK_SRC) + socket.inet_aton(MARK_DST)
            + struct.pack('!HH', MARK_SPORT, EXIT_PORT))
    return PROXY_V2_SIGNATURE + bytes((0x21, 0x11)) + struct.pack('!H', len(body)) + body


def _why(e: BaseException) -> str:
    if isinstance(e, TimeoutError):          # socket.timeout too
        return 'timeout'
    if isinstance(e, ConnectionRefusedError):
        return 'refused'
    if isinstance(e, ConnectionResetError):
        return 'reset'
    if isinstance(e, ssl.SSLError):
        return f'tls: {getattr(e, "reason", None) or e}'[:80]
    if isinstance(e, OSError) and e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return 'unreachable'
    return f'{type(e).__name__}: {e}'[:80]


def _context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Reality answers with its dest's certificate and we connect by address:
    # nothing here is worth verifying, the handshake itself is the test.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_alpn_protocols(['h2', 'http/1.1'])
    return ctx


def probe(target: Target, *, connect_timeout: Optional[float] = None,
          handshake_timeout: Optional[float] = None) -> Result:
    """One fresh connection to ``target``, closed at once. Never raises.
    The timeouts default to the module's, read at call time."""
    connect_timeout = CONNECT_TIMEOUT_S if connect_timeout is None else connect_timeout
    handshake_timeout = HANDSHAKE_TIMEOUT_S if handshake_timeout is None else handshake_timeout
    t0 = time.monotonic()

    def spent() -> int:
        return int((time.monotonic() - t0) * 1000)

    try:
        sock = socket.create_connection((target.host, target.port), timeout=connect_timeout)
    except OSError as e:
        return Result(False, 'tcp', spent(), _why(e))
    try:
        sock.settimeout(handshake_timeout)
        sock.sendall(proxy_header())
        tls = _context().wrap_socket(sock, server_hostname=target.sni or None,
                                     do_handshake_on_connect=False)
        try:
            tls.do_handshake()
        finally:
            tls.close()
    except (OSError, ValueError) as e:       # ssl.SSLError is an OSError
        return Result(False, 'tls', spent(), _why(e))
    finally:
        sock.close()
    return Result(True, 'ok', spent())


def probe_all(targets_: Sequence[Target]) -> Dict[str, Result]:
    """Every target at once: the slowest one sets the time, about
    ``CONNECT_TIMEOUT_S + 2 * HANDSHAKE_TIMEOUT_S`` at worst."""
    if not targets_:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(targets_)),
                            thread_name_prefix='exit-reach') as pool:
        futures = {t.key: pool.submit(probe, t) for t in targets_}
        return {key: f.result() for key, f in futures.items()}


# ----- the last check, for /protocols and the log -----

_LOCK = threading.Lock()
_LAST: dict = {}


def record(targets_: Sequence[Target], results: Dict[str, Result],
           now: Optional[float] = None) -> None:
    """Keep the check for /protocols; log each path that changed state."""
    with _LOCK:
        before = _LAST.get('results') or {}
        for t in targets_:
            r = results.get(t.key)
            old = before.get(t.key)
            if r is not None and (old is None or old.ok != r.ok):
                if r.ok:
                    logger.info(f'reach {t.name} {t.addr}: проходит, {r.ms} мс')
                else:
                    logger.warning(f'reach {t.name} {t.addr}: {r.stage} {r.error} ({r.ms} мс)')
        _LAST.clear()
        _LAST.update(ts=time.time() if now is None else now,
                     targets=list(targets_), results=dict(results))


def snapshot() -> Optional[dict]:
    with _LOCK:
        return dict(_LAST) if _LAST else None


def _reset_for_tests() -> None:
    with _LOCK:
        _LAST.clear()


# ----- words -----

def _seconds(ms: int) -> str:
    return f'{ms / 1000:.1f}'.rstrip('0').rstrip('.')


def path_label(target: Target) -> str:
    return 'напрямую' if not target.via else f'через {target.name}'


def describe(target: Target, result: Result) -> str:
    """What happened on one path, as a short phrase."""
    if result.ok:
        return f'проходит, {result.ms} мс'
    if result.stage == 'tcp':
        return {
            'timeout': f'TCP не соединяется: {_seconds(result.ms)} с без ответа',
            'refused': 'TCP: отказ в соединении (порт закрыт)',
            'reset': 'TCP: соединение сброшено',
            'unreachable': 'TCP: нет маршрута',
        }.get(result.error, f'TCP: {result.error}')
    if result.error == 'timeout':
        return f'TCP есть, TLS молчит {_seconds(result.ms)} с'
    return f'TCP есть, TLS не завершился ({result.error})'


def _minutes(mins: float) -> str:
    mins = int(mins)
    if mins < 60:
        return f'{mins} мин'
    hours, mins = divmod(mins, 60)
    return f'{hours} ч {mins:02d} мин' if hours < 24 else f'{hours // 24} д {hours % 24} ч'


def report_is_fresh(report_age_min: Optional[float]) -> bool:
    """Did exit report recently enough to count as alive?"""
    return report_age_min is not None and report_age_min < REPORT_FRESH_MIN


def report_line(report_age_min: Optional[float]) -> str:
    if report_age_min is None:
        return 'Отчётов от exit нет (или их не прочитать).'
    if report_is_fresh(report_age_min):
        return f'exit отчитался {_minutes(report_age_min)} назад: связь exit → entry есть.'
    return f'Отчётов от exit нет {_minutes(report_age_min)}.'


def verdict(targets_: Sequence[Target], results: Dict[str, Result],
            report_age_min: Optional[float]) -> List[dict]:
    """Pure: the alerts for one check. Each is ``{key, severity, title,
    detail}``; a target without a result is left out."""
    judged = [t for t in targets_ if t.key in results]
    exit_t = next((t for t in judged if not t.via), None)
    vias = [t for t in judged if t.via]
    exit_ok = exit_t is None or results[exit_t.key].ok
    any_via_ok = any(results[t.key].ok for t in vias)
    fresh = report_is_fresh(report_age_min)
    lines = [f'{path_label(t).capitalize()} ({t.addr}): {describe(t, results[t.key])}.'
             for t in judged]
    lines.append(report_line(report_age_min))
    out: List[dict] = []

    if not exit_ok:
        ip = exit_t.host
        if any_via_ok:
            title = f'Адрес exit {ip} не принимает соединения из России'
            tail = ('exit жив: через транзит проходит. Так выглядела блокировка адреса '
                    '18.11. Если трафик ещё идёт напрямую, переключи его на транзит: '
                    'AGENTS.md §59.')
        elif fresh:
            title = f'Адрес exit {ip} не принимает соединения из России, exit жив'
            if vias:
                tail = ('Через транзит тоже не пройти: проверь транзитную ноду. Пока она '
                        'лежит, у пользователей нет пути к exit.')
            else:
                tail = ('Транзита нет (EXIT_TRANSITS не задан): у пользователей нет пути '
                        'к exit. Как переключиться на транзит: AGENTS.md §59.')
        else:
            title = f'exit {ip} не отвечает: ни соединений из России, ни отчётов'
            tail = ('exit, похоже, лежит или потерял сеть: проверь его у провайдера. '
                    'Если он жив, пропали и соединения из России, и его отчёты.')
        out.append({'key': exit_t.key, 'severity': 'critical', 'title': title,
                    'detail': '\n'.join(lines + [tail])})

    for t in vias:
        if results[t.key].ok:
            continue
        if exit_t is None:
            severity = 'warn'
            tail = 'Прямой путь не проверяется: EXIT_NODE_IP не задан.'
        elif exit_ok:
            severity = 'warn'
            tail = 'Прямой путь работает: сломан запасной.'
        else:
            severity = 'critical'
            tail = ('Прямой путь тоже закрыт: если трафик идёт через этот транзит, '
                    'у пользователей нет связи.')
        tail += (f' На ноде {t.name}: systemctl status nekovpn-transit; '
                 'iptables -t nat -S | grep nekovpn-transit.')
        out.append({'key': t.key, 'severity': severity,
                    'title': f'Транзит {t.name} ({t.addr}) к exit не проходит',
                    'detail': '\n'.join(lines + [tail])})
    return out


def reach_line(snap: Optional[dict], now: Optional[float] = None) -> Optional[str]:
    """One /protocols line from the last check, or None without a recent one."""
    if not snap:
        return None
    now = time.time() if now is None else now
    age = now - float(snap.get('ts') or 0)
    if age > SNAPSHOT_FRESH_S:
        return None
    parts = []
    for t in snap.get('targets') or []:
        r = (snap.get('results') or {}).get(t.key)
        if r is None:
            continue
        parts.append(f"{'✅' if r.ok else '❌'} {path_label(t)}: {describe(t, r)}")
    if not parts:
        return None
    return f'🛣 Пути к exit ({int(max(age, 0))} с назад): ' + ' · '.join(parts)
