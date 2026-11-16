"""Stand for the hy2 auth relay (IMPROVEMENT_PLAN F, A6; AGENTS.md §56) with
exit's hysteria, 2.9.2.

What it pins:
  * the auth path as it runs on exit: hysteria asks the relay on loopback
    (``http://127.0.0.1:<port>/api/hy2/auth``), the relay — the script itself,
    run as the unit runs it, ``python3 -I`` — asks the real bot over its TLS
    listener with the pin. Bot up: a paid key gets through and the bot logs
    the allow, an unknown key is refused by the bot. Bot stopped: the key the
    bot let in passes, so does a key only the bot's list held, an unknown key
    does not; once the memory bound has passed, the remembered key does not
    either;
  * tunnel users cannot reach the relay: hysteria in a network namespace of
    its own with the ACL of AGENTS.md §52 (read from AGENTS.md itself), the
    relay listening on that namespace's loopback. Through the tunnel the
    relay is out of reach by every spelling of loopback (127.0.0.1,
    localhost, a name that resolves to 127.0.0.1, 0.0.0.0, the IPv4-mapped
    form) and by the server's own address (it listens on loopback only),
    while an ordinary address passes; without the ACL, 127.0.0.1 reaches it —
    so the ACL is what keeps it out.

Needs docker and two images pulled (tobyxdd/hysteria:v2.9.2,
python:3.11-slim); no internet.
"""

import http.client
import http.server
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
import uuid as uuidlib
from pathlib import Path

import pytest

from bot.services import hop_tls

from .hop_tls_support import PROBE_UUID, REPO, UUID, Server, free_port

pytestmark = pytest.mark.requires_docker

HYSTERIA = 'tobyxdd/hysteria:v2.9.2'    # exit's version
PYTHON = 'python:3.11-slim'
RELAY = REPO / 'scripts' / 'hy2_auth_relay.py'
TOKEN = 'tok-stand'
MARKER = b'through the tunnel'
QUIET = ('-e', 'HYSTERIA_DISABLE_UPDATE_CHECK=1')


def _docker(*args, check=True, timeout=120):
    r = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode:
        raise AssertionError(f'docker {args[:3]}: {r.stderr.strip()[-800:]}')
    return r


def _need(*images):
    if shutil.which('docker') is None:
        pytest.skip('docker is not installed')
    try:
        _docker('version')
    except (AssertionError, subprocess.TimeoutExpired):
        pytest.skip('docker is not available')
    for image in images:
        if _docker('image', 'inspect', image, check=False).returncode:
            pytest.skip(f'{image} not pulled')


class _Container:
    def __init__(self, name, *args):
        self.name = name
        _docker('rm', '-f', name, check=False)
        _docker('run', '-d', '--name', name, *args)

    def logs(self):
        r = _docker('logs', self.name, check=False)
        return r.stdout + r.stderr

    def ip(self, network):
        return _docker('inspect', '-f',
                       f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
                       self.name).stdout.strip()

    def close(self):
        _docker('rm', '-f', self.name, check=False)


def _get(proxy, url, timeout=5):
    """``(status, body)`` of a GET through the hysteria client's HTTP proxy;
    status 0 = the proxy dropped the request."""
    host, port = proxy
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request('GET', url, headers={'Host': url.split('/')[2]})
        resp = conn.getresponse()
        return resp.status, resp.read()
    except (OSError, http.client.HTTPException):
        return 0, b''
    finally:
        conn.close()


def _tls_pair(work):
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                    'ec_paramgen_curve:prime256v1', '-nodes', '-days', '2',
                    '-keyout', str(work / 'qkey.pem'), '-out', str(work / 'qcert.pem'),
                    '-subj', '/CN=hy2.example.com'], check=True, capture_output=True)
    (work / 'qkey.pem').chmod(0o644)


class _Web(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', str(len(MARKER)))
        self.end_headers()
        self.wfile.write(MARKER)

    def log_message(self, *a):
        pass


# ------------------------------------------------- the auth path, bot up/down --

class _Relay:
    """The script as the unit runs it, in the stand's environment."""

    def __init__(self, bot, cert, memory_s=None):
        self.port = free_port()
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith('HY2_RELAY_') or k in ('BOT_URL', 'BOT_CA_FILE',
                                                           'DPI_REPORT_TOKEN'))}
        env.update(BOT_URL=f'https://127.0.0.1:{bot.tls_port}', BOT_CA_FILE=str(cert),
                   DPI_REPORT_TOKEN=TOKEN, HY2_RELAY_LISTEN=f'127.0.0.1:{self.port}')
        if memory_s is not None:
            env['HY2_RELAY_MEMORY_S'] = str(memory_s)
        self.proc = subprocess.Popen([sys.executable, '-I', str(RELAY)], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.lines = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip())

    def status(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request('GET', '/status')
        return json.loads(conn.getresponse().read())

    def wait_for_list(self, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.status()['key_list']['size']:
                    return self.status()
            except OSError:
                pass
            time.sleep(0.2)
        raise AssertionError(f'the relay took no key list: {self.lines}')

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def web_port():
    srv = http.server.HTTPServer(('127.0.0.1', 0), _Web)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def stand(tmp_path, web_port):
    _need(HYSTERIA)
    d = tmp_path / 'hop'
    hop_tls.generate(str(d), '127.0.0.1')
    bot = Server(tmp_path, tls_dir=str(d), DPI_REPORT_TOKEN=TOKEN)
    up = [True]
    things = []

    class Stand:
        relay = None

        def start_relay(self, **kw):
            self.relay = _Relay(bot, d / 'cert.pem', **kw)
            things.append(self.relay)
            return self.relay

        def start_hysteria(self):
            work = tmp_path / 'hy'
            work.mkdir()
            _tls_pair(work)
            self.udp = free_port()
            (work / 'server.yaml').write_text(
                f'listen: 127.0.0.1:{self.udp}\n'
                'tls:\n  cert: /w/qcert.pem\n  key: /w/qkey.pem\n'
                'auth:\n  type: http\n  http:\n'
                f'    url: http://127.0.0.1:{self.relay.port}/api/hy2/auth\n')
            self.work = work
            server = _Container(f'a6-hy-server-{self.udp}', '--network', 'host',
                                '-v', f'{work}:/w:ro', *QUIET, HYSTERIA,
                                'server', '-c', '/w/server.yaml')
            things.append(server)
            return server

        def client(self, password, timeout=20):
            """'pass' (a request went through), 'refused' (hysteria said
            authentication error), or 'timeout'."""
            proxy = free_port()
            cfg = self.work / f'client-{proxy}.yaml'
            cfg.write_text(f'server: 127.0.0.1:{self.udp}\nauth: {password}\n'
                           'tls:\n  sni: hy2.example.com\n  insecure: true\n'
                           f'http:\n  listen: 127.0.0.1:{proxy}\n')
            c = _Container(f'a6-hy-client-{proxy}', '--network', 'host',
                           '-v', f'{self.work}:/w:ro', *QUIET, HYSTERIA,
                           'client', '-c', f'/w/{cfg.name}')
            try:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if _get(('127.0.0.1', proxy), f'http://127.0.0.1:{web_port}/') == (200, MARKER):
                        return 'pass'
                    if 'authentication error' in c.logs():
                        return 'refused'
                    time.sleep(0.3)
                return 'timeout'
            finally:
                c.close()

        def stop_bot(self):
            if up[0]:
                up[0] = False
                bot.close()

        def audit(self):
            import asyncio

            async def drain():
                while bot.srv._bg_tasks:
                    await asyncio.gather(*list(bot.srv._bg_tasks), return_exceptions=True)
            asyncio.run_coroutine_threadsafe(drain(), bot.loop).result(10)
            bot.srv._hy2_record_pool.submit(lambda: None).result(10)
            with bot.db._connect() as conn:
                return [tuple(r) for r in conn.execute(
                    'SELECT chat_id, decision FROM hy2_auth_log ORDER BY id').fetchall()]

    st = Stand()
    yield st
    for t in reversed(things):
        t.close()
    st.stop_bot()


class TestBotUpAndDown:

    def test_the_relay_answers_for_the_bot_while_it_is_down(self, stand):
        stranger = str(uuidlib.uuid4())
        relay = stand.start_relay()
        assert relay.wait_for_list()['key_list']['size'] == {'hy2': 2, 'hy2t': 2}
        stand.start_hysteria()
        # bot up: hysteria -> relay -> bot (TLS, pin) and back
        assert stand.client(UUID) == 'pass'
        assert stand.client(stranger) == 'refused'
        assert stand.audit() == [('42', 'allow'), (None, 'deny')]
        # bot stopped: a deploy
        stand.stop_bot()
        assert stand.client(UUID) == 'pass'           # let in by the bot a moment ago
        assert stand.client(PROBE_UUID) == 'pass'     # never connected: the bot's list
        assert stand.client(stranger) == 'refused'
        status = relay.status()
        assert status['served'] == {'bot_allow': 1, 'bot_deny': 1, 'memory_allow': 2,
                                    'memory_deny': 1}
        assert 'ConnectionRefused' in status['bot']['last_error']
        assert any('allow u42@x from memory' in line for line in relay.lines)
        assert any('allow probe_monitor@x from memory' in line for line in relay.lines)

    def test_past_the_bound_the_remembered_key_is_refused(self, stand):
        relay = stand.start_relay(memory_s=12)
        relay.wait_for_list()
        stand.start_hysteria()
        stand.stop_bot()
        assert stand.client(UUID) == 'pass'
        # the newest word from the bot is the list, fetched at the start
        age = relay.status()['key_list']['age_s']
        time.sleep(max(0.0, 12 - age) + 0.5)
        assert stand.client(UUID) == 'refused'
        assert relay.status()['remembered'] == {'hy2': 0, 'hy2t': 0}


# --------------------------------------------- tunnel users and the listener --

def _documented_acl():
    """The ACL AGENTS.md §52 tells the owner to add to both hysteria configs."""
    text = (REPO / 'AGENTS.md').read_text()
    blocks = [b for b in re.findall(r'```yaml\n(.*?)```', text, re.S) if b.lstrip().startswith('acl:')]
    assert len(blocks) == 1, blocks
    acl = textwrap.dedent(blocks[0])
    assert 'reject(127.0.0.0/8)' in acl and 'reject(0.0.0.0/8)' in acl
    return acl


def _subnet_free(net):
    used = []
    for nid in _docker('network', 'ls', '-q').stdout.split():
        for sub in _docker('network', 'inspect', '-f',
                           '{{range .IPAM.Config}}{{.Subnet}} {{end}}', nid).stdout.split():
            try:
                used.append(ipaddress.ip_network(sub))
            except ValueError:
                pass
    return not any(net.overlaps(u) for u in used)


class TestTunnelUsersCannotReachTheRelay:

    RELAY_PORT = 9979

    @pytest.fixture
    def namespace_stand(self, tmp_path):
        _need(HYSTERIA, PYTHON)
        # A test network out of every range the ACL rejects, so an ordinary
        # address is there to pass (TEST-NET-2 / -3 / -1, RFC 5737).
        net = next((n for n in ('198.51.100.0/24', '203.0.113.0/24', '192.0.2.0/24')
                    if _subnet_free(ipaddress.ip_network(n))), None)
        if net is None:
            pytest.skip('no free TEST-NET subnet for the stand')
        name = f'a6-acl-{free_port()}'
        _docker('network', 'create', '--subnet', net, name)
        made = []
        work = tmp_path / 'ns'
        work.mkdir()
        _tls_pair(work)
        (work / 'www').mkdir()
        (work / 'www' / 'marker').write_bytes(MARKER)
        shutil.copy(RELAY, work / 'hy2_auth_relay.py')

        def start(with_acl):
            tag = 'acl' if with_acl else 'open'
            (work / f'server-{tag}.yaml').write_text(
                'listen: :8443\n'
                'tls:\n  cert: /w/qcert.pem\n  key: /w/qkey.pem\n'
                'auth:\n  type: password\n  password: stand-pw\n'
                + (_documented_acl() if with_acl else ''))
            srv = _Container(f'{name}-srv-{tag}', '--network', name,
                             '--add-host', 'relay.test:127.0.0.1', '-v', f'{work}:/w:ro',
                             *QUIET, HYSTERIA, 'server', '-c', f'/w/server-{tag}.yaml')
            made.append(srv)
            # the relay in hysteria's own network namespace, as on exit
            rel = _Container(f'{name}-relay-{tag}', '--network', f'container:{srv.name}',
                             '-v', f'{work}:/w:ro', '-e', f'HY2_RELAY_LISTEN=127.0.0.1:{self.RELAY_PORT}',
                             '-e', 'BOT_URL=http://127.0.0.1:1', '-e', 'BOT_CA_FILE=',
                             PYTHON, 'python3', '-I', '/w/hy2_auth_relay.py')
            made.append(rel)
            srv_ip = srv.ip(name)
            (work / f'client-{tag}.yaml').write_text(
                f'server: {srv_ip}:8443\nauth: stand-pw\n'
                'tls:\n  sni: hy2.example.com\n  insecure: true\n'
                'http:\n  listen: 0.0.0.0:1080\n')
            cli = _Container(f'{name}-cli-{tag}', '--network', name, '-v', f'{work}:/w:ro',
                             *QUIET, HYSTERIA, 'client', '-c', f'/w/client-{tag}.yaml')
            made.append(cli)
            return srv, rel, srv_ip, (cli.ip(name), 1080)

        web = _Container(f'{name}-web', '--network', name, '-v', f'{work}/www:/www:ro',
                         PYTHON, 'python3', '-m', 'http.server', '-d', '/www', '8080')
        made.append(web)
        try:
            yield start, web.ip(name)
        finally:
            for c in reversed(made):
                c.close()
            _docker('network', 'rm', name, check=False)

    def _relay_up(self, rel):
        """The relay answers inside the namespace (the control for "out of
        reach" below: it is there)."""
        probe = ('import json, urllib.request; print(json.load(urllib.request.urlopen('
                 f'"http://127.0.0.1:{self.RELAY_PORT}/status", timeout=3))["ok"])')
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            r = _docker('exec', rel.name, 'python3', '-c', probe, check=False)
            if r.stdout.strip() == 'True':
                return
            time.sleep(0.3)
        raise AssertionError(f'the relay did not come up: {rel.logs()}')

    def _tunnel_up(self, proxy, web_ip):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            if _get(proxy, f'http://{web_ip}:8080/marker') == (200, MARKER):
                return
            time.sleep(0.3)
        raise AssertionError('the tunnel never carried a request')

    LOOPBACK = ('127.0.0.1', 'localhost', 'relay.test', '0.0.0.0', '[::ffff:127.0.0.1]')

    def _errors(self, srv):
        """reqAddr -> error, from hysteria's own log of the tunnel's dials."""
        out = {}
        for m in re.finditer(r'TCP error\s+(\{.*\})', srv.logs()):
            d = json.loads(m.group(1))
            out[d['reqAddr']] = d['error']
        return out

    def test_every_spelling_of_loopback_is_rejected(self, namespace_stand):
        start, web_ip = namespace_stand
        srv, rel, srv_ip, proxy = start(with_acl=True)
        self._relay_up(rel)
        self._tunnel_up(proxy, web_ip)            # an ordinary address passes
        p = self.RELAY_PORT
        for host in self.LOOPBACK + ('127.1.2.3', srv_ip):
            status, body = _get(proxy, f'http://{host}:{p}/status')
            assert (status, body) == (502, b''), host
        errors = self._errors(srv)
        for host in self.LOOPBACK + ('127.1.2.3',):
            assert errors[f'{host}:{p}'] == 'rejected', (host, errors)
        # the server's own address passes the ACL, and nothing listens there
        assert errors[f'{srv_ip}:{p}'].endswith('connect: connection refused'), errors

    def test_without_the_acl_loopback_would_reach_it(self, namespace_stand):
        start, web_ip = namespace_stand
        srv, rel, srv_ip, proxy = start(with_acl=False)
        self._relay_up(rel)
        self._tunnel_up(proxy, web_ip)
        p = self.RELAY_PORT
        for host in self.LOOPBACK:
            status, body = _get(proxy, f'http://{host}:{p}/status')
            assert status == 200 and json.loads(body)['ok'] is True, host
        # loopback only: not on another loopback address, not on the server's own
        for host in ('127.1.2.3', srv_ip):
            assert _get(proxy, f'http://{host}:{p}/status') == (502, b''), host
            assert self._errors(srv)[f'{host}:{p}'].endswith('connection refused')
