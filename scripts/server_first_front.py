#!/usr/bin/env python3
"""The server-first front on ENTRY (IMPROVEMENT_PLAN G4, AGENTS.md §62).

Since 2026-11-19 a filter in front of entry drops every connection from Russia
in which the client sends first: the TCP handshake completes, and the
client's first data (a TLS ClientHello, a QUIC packet, an SSH client's
banner) never arrives. A connection in which the server speaks first passes
both ways: checked from a Russian network with an SSH-like banner, a TLS
handshake after it and a 50-KB download.

This front speaks first. On accept it opens its target, writes one banner
line to the client and then pipes both ways, byte for byte. The NekoVPN app
reads and drops that line, then starts its usual Reality or ShadowTLS
handshake (client repo, core/neko_server_first.go). A route marked
``proxy-v2`` first sends its target a PROXY v2 header with the user's
address: haproxy's ``accept-proxy`` bind takes the address from it, so exit
still sees the user, not 127.0.0.1.

Stdlib only. The config is a file of ``key = value`` lines, read as data:

    banner = SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.20
    route = 0.0.0.0:2443 127.0.0.1:12443 proxy-v2
    route = 0.0.0.0:2444 127.0.0.1:443
    max_conns = 4096

    server_first_front.py --config FILE           run
    server_first_front.py --config FILE --check   validate, print the routes

Nothing is logged per user: a summary line every five minutes (accepted,
active, refused, failed, bytes per route) goes to the journal.
"""
import argparse
import asyncio
import ipaddress
import socket
import struct
import sys
import time

DEFAULT_CONFIG = '/etc/nekovpn-sf-front.conf'
# entry's own sshd, the banner checked from Russia on 2026-11-19
DEFAULT_BANNER = 'SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.20'
DEFAULT_MAX_CONNS = 4096
MAX_CONNS_LIMIT = 100000
# The client reads at most 255 bytes up to the line feed (the app's limit).
BANNER_MAX = 253
CONNECT_TIMEOUT_S = 5.0
CHUNK = 65536
STATS_EVERY_S = 300
# A dead peer of a long-lived tunnel is found in about seven minutes.
KEEPALIVE = (300, 30, 4)

PROXY_V2_SIG = b'\r\n\r\n\x00\r\nQUIT\n'


class ConfigError(ValueError):
    pass


class Route:
    def __init__(self, listen, target, proxy_v2=False):
        self.listen = listen
        self.target = target
        self.proxy_v2 = proxy_v2
        self.accepted = 0
        self.active = 0
        self.refused = 0
        self.failed = 0
        self.up = 0
        self.down = 0

    def label(self) -> str:
        return f'{hostport(self.listen)}->{hostport(self.target)}' + (' proxy-v2' if self.proxy_v2 else '')


class Config:
    def __init__(self, banner: bytes, routes, max_conns: int):
        self.banner = banner
        self.routes = routes
        self.max_conns = max_conns


def hostport(addr) -> str:
    host, port = addr
    return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


def parse_hostport(text: str):
    """``IPv4:port`` or ``[IPv6]:port``. A name is refused: the front must not
    depend on a resolver, and its targets are local."""
    text = text.strip()
    if text.startswith('['):
        host, sep, port = text[1:].partition(']:')
    else:
        host, sep, port = text.rpartition(':')
        if ':' in host:
            sep = ''
    if not sep:
        raise ConfigError(f'{text!r}: expected IPv4:port or [IPv6]:port')
    try:
        host = str(ipaddress.ip_address(host))
    except ValueError:
        raise ConfigError(f'{text!r}: the host must be an IP address') from None
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ConfigError(f'{text!r}: the port must be 1-65535')
    return host, int(port)


def check_banner(value: str) -> str:
    if not 1 <= len(value) <= BANNER_MAX:
        raise ConfigError(f'the banner must be 1-{BANNER_MAX} characters')
    if any(not 0x20 <= ord(ch) <= 0x7e for ch in value):
        raise ConfigError('the banner must be printable ASCII on one line')
    return value


def parse_config(text: str) -> Config:
    banner = DEFAULT_BANNER
    max_conns = DEFAULT_MAX_CONNS
    routes = []
    listens = set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        if not sep:
            raise ConfigError(f'line {n}: expected key = value')
        key, value = key.strip().lower(), value.strip()
        if key == 'banner':
            banner = check_banner(value)
        elif key == 'max_conns':
            if not value.isdigit() or not 1 <= int(value) <= MAX_CONNS_LIMIT:
                raise ConfigError(f'line {n}: max_conns must be 1-{MAX_CONNS_LIMIT}')
            max_conns = int(value)
        elif key == 'route':
            parts = value.split()
            if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] != 'proxy-v2'):
                raise ConfigError(f'line {n}: route = LISTEN TARGET [proxy-v2]')
            listen, target = parse_hostport(parts[0]), parse_hostport(parts[1])
            if listen in listens:
                raise ConfigError(f'line {n}: {hostport(listen)} is listed twice')
            listens.add(listen)
            routes.append(Route(listen, target, proxy_v2=len(parts) == 3))
        else:
            raise ConfigError(f'line {n}: unknown key {key!r}')
    if not routes:
        raise ConfigError('no route')
    return Config(banner.encode('ascii') + b'\r\n', routes, max_conns)


def _plain(address):
    ip = ipaddress.ip_address(address)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def proxy_v2_header(src, dst) -> bytes:
    """The PROXY v2 header for a TCP connection from ``src`` to ``dst``
    (``(ip, port)`` each). Two address families that do not match (which a
    dual-stack listener could give) make a LOCAL header without addresses:
    haproxy then logs the front's own address, never a wrong one."""
    s, d = _plain(src[0]), _plain(dst[0])
    if s.version != d.version:
        return PROXY_V2_SIG + b'\x20\x00\x00\x00'
    family = b'\x11' if s.version == 4 else b'\x21'
    body = s.packed + d.packed + struct.pack('!HH', src[1], dst[1])
    return PROXY_V2_SIG + b'\x21' + family + struct.pack('!H', len(body)) + body


def _tune(writer) -> None:
    sock = writer.get_extra_info('socket')
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'TCP_KEEPIDLE'):
            idle, interval, count = KEEPALIVE
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
    except OSError:
        pass


def _abort(writer) -> None:
    if writer is None:
        return
    try:
        writer.transport.abort()
    except Exception:
        pass


class Front:
    def __init__(self, config: Config):
        self.config = config
        self.active = 0

    async def handle(self, route: Route, reader, writer) -> None:
        if self.active >= self.config.max_conns:
            route.refused += 1
            _abort(writer)
            return
        self.active += 1
        route.accepted += 1
        route.active += 1
        upstream = None
        try:
            peer = writer.get_extra_info('peername')
            local = writer.get_extra_info('sockname')
            _tune(writer)
            try:
                up_reader, upstream = await asyncio.wait_for(
                    asyncio.open_connection(*route.target), CONNECT_TIMEOUT_S)
            except (OSError, asyncio.TimeoutError):
                # No banner for a dead target: the app moves on to its next
                # server at once instead of starting a handshake into nothing.
                route.failed += 1
                return
            _tune(upstream)
            if route.proxy_v2:
                upstream.write(proxy_v2_header(peer[:2], local[:2]))
            writer.write(self.config.banner)
            await asyncio.gather(
                self._pipe(reader, upstream, route, 'up', writer),
                self._pipe(up_reader, writer, route, 'down', upstream),
            )
        finally:
            self.active -= 1
            route.active -= 1
            for w in (writer, upstream):
                if w is not None:
                    try:
                        w.close()
                    except Exception:
                        pass

    @staticmethod
    async def _pipe(src, dst, route: Route, counter: str, other) -> None:
        """One direction. A clean end is passed on as a half-close, so the
        other direction finishes its answer; a broken one aborts both."""
        try:
            while True:
                data = await src.read(CHUNK)
                if not data:
                    break
                setattr(route, counter, getattr(route, counter) + len(data))
                dst.write(data)
                await dst.drain()
            if dst.can_write_eof():
                dst.write_eof()
            else:
                _abort(dst)
        except (OSError, ConnectionError, asyncio.IncompleteReadError, RuntimeError):
            _abort(dst)
            _abort(other)


def log(*parts) -> None:
    print(time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime()), *parts, flush=True)


async def start(config: Config):
    """Listen on every route; returns the front and the servers (a test
    reads the bound ports from them)."""
    front = Front(config)
    servers = []
    for route in config.routes:
        server = await asyncio.start_server(
            lambda r, w, route=route: front.handle(route, r, w),
            route.listen[0], route.listen[1], reuse_address=True, backlog=1024)
        servers.append(server)
    return front, servers


async def run(config: Config) -> None:
    front, servers = await start(config)
    for route in config.routes:
        log('listening', route.label())
    while True:
        await asyncio.sleep(STATS_EVERY_S)
        log('active', front.active, '|', ' | '.join(
            f'{r.label()}: accepted={r.accepted} active={r.active} refused={r.refused}'
            f' failed={r.failed} up={r.up}B down={r.down}B' for r in config.routes))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='server-first front (AGENTS.md §62)')
    parser.add_argument('--config', default=DEFAULT_CONFIG)
    parser.add_argument('--check', action='store_true', help='validate the config and exit')
    args = parser.parse_args(argv)
    try:
        with open(args.config, encoding='utf-8') as f:
            config = parse_config(f.read())
    except (OSError, UnicodeDecodeError, ConfigError) as e:
        print(f'{args.config}: {e}', file=sys.stderr)
        return 2
    if args.check:
        for route in config.routes:
            print(route.label())
        return 0
    try:
        asyncio.run(run(config))
    except OSError as e:
        print(f'cannot listen: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
