"""scripts/server_first_front.py — the server-first front on entry
(IMPROVEMENT_PLAN G4, AGENTS.md §62).

What is pinned: the front writes its banner line before the client has sent
anything, and only once its target is open; after that it pipes both ways
byte for byte, a clean end passed on as a half-close; a ``proxy-v2`` route
first sends its target a PROXY v2 header with the client's address; a dead
target or a full front gives the client no banner at all; the config is
data, and anything it does not know is refused.
"""

import asyncio
import importlib.util
import ipaddress
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'server_first_front.py'
_spec = importlib.util.spec_from_file_location('server_first_front', SCRIPT)
sff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sff)

BANNER = b'SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.20\r\n'


# --------------------------------------------------------------- config --

def test_a_full_config():
    config = sff.parse_config(
        '# comment\n'
        '\n'
        'banner = SSH-2.0-Test_1\n'
        'route = 0.0.0.0:2443 127.0.0.1:18443 proxy-v2\n'
        'route = [::]:2444 [::1]:443\n'
        'max_conns = 10\n')
    assert config.banner == b'SSH-2.0-Test_1\r\n'
    assert config.max_conns == 10
    assert [(r.listen, r.target, r.proxy_v2) for r in config.routes] == [
        (('0.0.0.0', 2443), ('127.0.0.1', 18443), True),
        (('::', 2444), ('::1', 443), False),
    ]


def test_the_defaults():
    config = sff.parse_config('route = 0.0.0.0:2444 127.0.0.1:443\n')
    assert config.banner == BANNER
    assert config.max_conns == sff.DEFAULT_MAX_CONNS


@pytest.mark.parametrize('text, why', [
    ('', 'no route'),
    ('# only a comment\n', 'no route'),
    ('route = 0.0.0.0:2443\n', 'LISTEN TARGET'),
    ('route = 0.0.0.0:2443 127.0.0.1:18443 proxy-v1\n', 'LISTEN TARGET'),
    ('route = 0.0.0.0:2443 127.0.0.1:18443 proxy-v2 extra\n', 'LISTEN TARGET'),
    ('route = localhost:2443 127.0.0.1:18443\n', 'IP address'),
    ('route = 0.0.0.0:2443 entry.example:443\n', 'IP address'),
    ('route = 0.0.0.0:0 127.0.0.1:443\n', '1-65535'),
    ('route = 0.0.0.0:65536 127.0.0.1:443\n', '1-65535'),
    ('route = 0.0.0.0:x 127.0.0.1:443\n', '1-65535'),
    ('route = ::1:2443 127.0.0.1:443\n', 'IPv4:port'),
    ('route = 0.0.0.0:2443 127.0.0.1:1\nroute = 0.0.0.0:2443 127.0.0.1:2\n', 'twice'),
    ('route = 0.0.0.0:2443 127.0.0.1:443\nmax_conns = 0\n', 'max_conns'),
    ('route = 0.0.0.0:2443 127.0.0.1:443\nmax_conns = 100001\n', 'max_conns'),
    ('route = 0.0.0.0:2443 127.0.0.1:443\nmax_conns = -1\n', 'max_conns'),
    ('route = 0.0.0.0:2443 127.0.0.1:443\nbanner =\n', 'banner'),
    ('route = 0.0.0.0:2443 127.0.0.1:443\nbanner = ' + 'x' * 254 + '\n', 'banner'),
    ('route = 0.0.0.0:2443 127.0.0.1:443\nbanner = SSH-2.0-Тест\n', 'ASCII'),
    ('route = 0.0.0.0:2443 127.0.0.1:443\nbanner = a\tb\n', 'ASCII'),
    ('route = 0.0.0.0:2443 127.0.0.1:443\nlisten = 1\n', 'unknown key'),
    ('route 0.0.0.0:2443 127.0.0.1:443\n', 'key = value'),
])
def test_a_config_it_does_not_know_is_refused(text, why):
    with pytest.raises(sff.ConfigError, match=why):
        sff.parse_config(text)


def test_the_longest_banner_fits_the_apps_limit():
    config = sff.parse_config('route = 0.0.0.0:2443 127.0.0.1:443\nbanner = ' + 'x' * 253 + '\n')
    assert len(config.banner) == 255
    assert config.banner.endswith(b'\r\n')


# ------------------------------------------------------------ PROXY v2 --

def test_the_proxy_v2_header_for_ipv4():
    header = sff.proxy_v2_header(('203.0.113.7', 50491), ('130.49.146.10', 2443))
    assert header == (b'\r\n\r\n\x00\r\nQUIT\n' + b'\x21\x11' + struct.pack('!H', 12)
                      + ipaddress.ip_address('203.0.113.7').packed
                      + ipaddress.ip_address('130.49.146.10').packed
                      + struct.pack('!HH', 50491, 2443))


def test_the_proxy_v2_header_for_ipv6():
    header = sff.proxy_v2_header(('2001:db8::7', 1), ('2001:db8::1', 2443))
    assert header[12:14] == b'\x21\x21'
    assert struct.unpack('!H', header[14:16]) == (36,)
    assert header[16:32] == ipaddress.ip_address('2001:db8::7').packed
    assert header[48:52] == struct.pack('!HH', 1, 2443)


def test_an_ipv4_mapped_address_is_sent_as_ipv4():
    header = sff.proxy_v2_header(('::ffff:203.0.113.7', 5), ('::ffff:130.49.146.10', 2443))
    assert header[13:14] == b'\x11'
    assert header[16:20] == ipaddress.ip_address('203.0.113.7').packed


def test_mixed_families_make_a_local_header_without_addresses():
    assert sff.proxy_v2_header(('203.0.113.7', 5), ('2001:db8::1', 2443)) == (
        b'\r\n\r\n\x00\r\nQUIT\n' + b'\x20\x00\x00\x00')


# ---------------------------------------------------------- the front --

class Upstream:
    """A target that records what it got and answers ``echo:<data>`` per
    chunk; with ``after_eof`` it answers only once the client half-closed."""

    def __init__(self, after_eof=False):
        self.after_eof = after_eof
        self.received = b''
        self.connections = 0

    async def handle(self, reader, writer):
        self.connections += 1
        try:
            if self.after_eof:
                self.received += await reader.read()
                writer.write(b'answer:' + self.received)
            else:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    self.received += data
                    writer.write(b'echo:' + data)
                    await writer.drain()
            await writer.drain()
        finally:
            writer.close()


async def _start(route_kwargs=None, upstream=None, max_conns=100, banner=BANNER, target=None):
    upstream = upstream or Upstream()
    up_server = await asyncio.start_server(upstream.handle, '127.0.0.1', 0)
    up_port = up_server.sockets[0].getsockname()[1]
    route = sff.Route(('127.0.0.1', 0), target or ('127.0.0.1', up_port), **(route_kwargs or {}))
    front, servers = await sff.start(sff.Config(banner, [route], max_conns))
    port = servers[0].sockets[0].getsockname()[1]
    return front, route, port, upstream, [up_server] + servers


async def _stop(servers):
    for server in servers:
        server.close()
        await server.wait_closed()


async def _read_line(reader):
    return await asyncio.wait_for(reader.readline(), 3)


def test_the_banner_comes_first_then_the_data_both_ways():
    async def go():
        front, route, port, upstream, servers = await _start()
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            # Nothing sent yet: the server speaks first.
            assert await _read_line(reader) == BANNER
            writer.write(b'hello')
            await writer.drain()
            assert await asyncio.wait_for(reader.readexactly(len(b'echo:hello')), 3) == b'echo:hello'
            writer.close()
            await writer.wait_closed()
            await asyncio.sleep(0.05)
            assert upstream.received == b'hello'
            assert (route.accepted, route.failed, route.refused) == (1, 0, 0)
            assert route.up == 5 and route.down == len(b'echo:hello')
            assert front.active == 0 and route.active == 0
        finally:
            await _stop(servers)
    asyncio.run(go())


def test_the_configured_banner_is_the_one_sent():
    async def go():
        _, _, port, _, servers = await _start(banner=b'SSH-2.0-Other\r\n')
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            assert await _read_line(reader) == b'SSH-2.0-Other\r\n'
            writer.close()
        finally:
            await _stop(servers)
    asyncio.run(go())


def test_a_proxy_v2_route_tells_the_target_who_the_client_is():
    async def go():
        _, _, port, upstream, servers = await _start({'proxy_v2': True})
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            client_port = writer.get_extra_info('sockname')[1]
            assert await _read_line(reader) == BANNER
            writer.write(b'tls')
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(len(b'echo:') + 28 + 3), 3)
            writer.close()
            await asyncio.sleep(0.05)
            header = sff.proxy_v2_header(('127.0.0.1', client_port), ('127.0.0.1', port))
            assert upstream.received == header + b'tls'
        finally:
            await _stop(servers)
    asyncio.run(go())


def test_a_plain_route_sends_no_header():
    async def go():
        _, _, port, upstream, servers = await _start()
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            await _read_line(reader)
            writer.write(b'x')
            await writer.drain()
            await asyncio.wait_for(reader.readexactly(6), 3)
            writer.close()
            await asyncio.sleep(0.05)
            assert upstream.received == b'x'
        finally:
            await _stop(servers)
    asyncio.run(go())


def test_a_half_close_reaches_the_target_and_its_answer_comes_back():
    async def go():
        _, _, port, upstream, servers = await _start(upstream=Upstream(after_eof=True))
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            assert await _read_line(reader) == BANNER
            writer.write(b'request')
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), 3) == b'answer:request'
            writer.close()
        finally:
            await _stop(servers)
    asyncio.run(go())


def _free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_a_dead_target_gives_no_banner():
    async def go():
        _, route, port, _, servers = await _start(target=('127.0.0.1', _free_port()))
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            assert await asyncio.wait_for(reader.read(), 3) == b''
            writer.close()
            await asyncio.sleep(0.05)
            assert route.failed == 1
        finally:
            await _stop(servers)
    asyncio.run(go())


def test_a_full_front_refuses_without_a_banner():
    async def go():
        front, route, port, _, servers = await _start(max_conns=1)
        try:
            r1, w1 = await asyncio.open_connection('127.0.0.1', port)
            assert await _read_line(r1) == BANNER
            r2, w2 = await asyncio.open_connection('127.0.0.1', port)
            assert await asyncio.wait_for(r2.read(), 3) == b''
            await asyncio.sleep(0.05)
            assert route.refused == 1 and front.active == 1
            w1.close()
            w2.close()
            await asyncio.sleep(0.05)
            # The slot is free again.
            r3, w3 = await asyncio.open_connection('127.0.0.1', port)
            assert await _read_line(r3) == BANNER
            w3.close()
        finally:
            await _stop(servers)
    asyncio.run(go())


def test_a_client_that_resets_frees_its_slot():
    async def go():
        front, route, port, _, servers = await _start()
        try:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            assert await _read_line(reader) == BANNER
            assert front.active == 1
            writer.get_extra_info('socket').setsockopt(
                socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            writer.transport.abort()   # an RST, not a FIN
            for _ in range(50):
                if front.active == 0:
                    break
                await asyncio.sleep(0.02)
            assert front.active == 0 and route.active == 0
        finally:
            await _stop(servers)
    asyncio.run(go())


# ----------------------------------------------------------------- CLI --

def _cli(tmp_path, text, *args):
    conf = tmp_path / 'front.conf'
    conf.write_text(text)
    return subprocess.run([sys.executable, '-I', str(SCRIPT), '--config', str(conf), *args],
                          capture_output=True, text=True, timeout=30)


def test_check_prints_the_routes(tmp_path):
    out = _cli(tmp_path, 'route = 0.0.0.0:2443 127.0.0.1:18443 proxy-v2\n'
                         'route = 0.0.0.0:2444 127.0.0.1:443\n', '--check')
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ['0.0.0.0:2443->127.0.0.1:18443 proxy-v2',
                                       '0.0.0.0:2444->127.0.0.1:443']


def test_a_bad_config_exits_2(tmp_path):
    out = _cli(tmp_path, 'route = localhost:2443 127.0.0.1:443\n', '--check')
    assert out.returncode == 2
    assert 'IP address' in out.stderr


def test_a_missing_config_exits_2(tmp_path):
    out = subprocess.run([sys.executable, '-I', str(SCRIPT), '--config', str(tmp_path / 'none'),
                          '--check'], capture_output=True, text=True, timeout=30)
    assert out.returncode == 2
