"""Stand for the exit → entry hop's TLS (AGENTS.md §53): the bot's own HTTPS
listener (the real WebAppServer on a real sqlite) behind exit's versions of
the two programs that call it, Caddy 2.11.3 and hysteria 2.9.2.

What it pins:
  * Caddy, with the Caddyfile scripts/caddy_entry_hop.py writes from an
    exit-shaped one (a global block, the dashboard site, the mirror site with
    its /rule-sets/ files): /health, the probe account's profile and the hy2
    callback pass, byte for byte, over the TLS listener; a different pin, a
    different name, or the system's CAs instead of the pin: 502, nothing
    reaches the bot;
  * hysteria, with the auth URL of the runbook and the environment of
    systemd/hysteria-entry-hop.conf (SSL_CERT_DIR = the system's CAs + the
    pin; the hosts file in place of /etc/hosts): a client with a paid uuid
    gets through and the bot logs the allow, an unknown uuid is denied by the
    bot, and without the pin the callback never reaches the bot (fail closed),
    even with a resolver of its own in hysteria's config;
  * Go's x509 (what both run on) refuses a certificate a leaked hop key signs
    for any other name or address: the name constraints hold in hysteria's
    process, where the pin sits next to the system's CAs.

Needs docker and the three images pulled (caddy:2.11.3-alpine,
tobyxdd/hysteria:v2.9.2, golang:1.25-alpine); no internet. Containers run on
the host network to reach the bot on 127.0.0.1.
"""

import http.server
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid as uuidlib

import pytest

from bot.services import hop_tls

from .hop_tls_support import PROBE_UUID, REPO, UUID, Server, free_port

pytestmark = pytest.mark.requires_docker

CADDY = 'caddy:2.11.3-alpine'           # exit's version (§52's stand)
HYSTERIA = 'tobyxdd/hysteria:v2.9.2'    # exit's version
GO = 'golang:1.25-alpine'
PIN_DIR = '/etc/nekovpn-hop'
DROP_IN = REPO / 'systemd' / 'hysteria-entry-hop.conf'
RUNBOOK = REPO / 'docs' / 'runbooks' / 'entry_hop_tls.md'


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


def _get(url, *, data=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _wait(fn, what, timeout=30):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            got = fn()
            if got:
                return got
        except (OSError, urllib.error.URLError) as e:
            last = e
        time.sleep(0.3)
    raise AssertionError(f'{what}: not within {timeout}s ({last})')


class _Container:
    def __init__(self, name, *args):
        self.name = name
        _docker('rm', '-f', name, check=False)
        _docker('run', '-d', '--name', name, '--network', 'host', *args)

    def logs(self):
        r = _docker('logs', self.name, check=False)
        return r.stdout + r.stderr

    def close(self):
        _docker('rm', '-f', self.name, check=False)


def _drop_in_env():
    """SSL_CERT_DIR exactly as the drop-in sets it, and the hosts file it binds."""
    text = DROP_IN.read_text()
    env = dict(re.findall(r'^Environment=(\w+)=(\S+)$', text, re.M))
    binds = re.findall(r'^BindReadOnlyPaths=(\S+):(\S+)$', text, re.M)
    return env, binds


# ------------------------------------------------------------------- Caddy --

@pytest.fixture
def bot(tmp_path):
    s = Server(tmp_path)
    yield s
    s.close()


def _exit_shaped_caddyfile(bot_port, dash, mirror):
    """The shape of exit's Caddyfile: a global block, the dashboard site and
    the mirror site (its rule-set files and the bot), both in clear to entry."""
    return (
        "{\n\tadmin off\n}\n\n"
        f"http://127.0.0.1:{dash} {{\n"
        f"\treverse_proxy 127.0.0.1:{bot_port}\n"
        "}\n\n"
        f"http://127.0.0.1:{mirror} {{\n"
        "\thandle_path /rule-sets/* {\n"
        "\t\troot * /srv/rule-sets\n"
        "\t\tfile_server\n"
        "\t}\n"
        "\thandle {\n"
        f"\t\treverse_proxy 127.0.0.1:{bot_port}\n"
        "\t}\n"
        "}\n"
    )


def _broken_sites(tls_port, ports):
    """Sites that must NOT reach the bot: another pin, another name, the
    system's CAs in place of the pin."""
    wrong_pin, wrong_name, system = ports
    return (
        f"\nhttp://127.0.0.1:{wrong_pin} {{\n"
        f"\treverse_proxy https://127.0.0.1:{tls_port} {{\n"
        "\t\ttransport http {\n"
        "\t\t\ttls_server_name entry-hop.invalid\n"
        f"\t\t\ttls_trust_pool file {PIN_DIR}/other.pem\n"
        "\t\t}\n\t}\n}\n"
        f"\nhttp://127.0.0.1:{wrong_name} {{\n"
        f"\treverse_proxy https://127.0.0.1:{tls_port} {{\n"
        "\t\ttransport http {\n"
        "\t\t\ttls_server_name entry.example.com\n"
        f"\t\t\ttls_trust_pool file {PIN_DIR}/entry-hop.pem\n"
        "\t\t}\n\t}\n}\n"
        f"\nhttp://127.0.0.1:{system} {{\n"
        f"\treverse_proxy https://127.0.0.1:{tls_port} {{\n"
        "\t\ttransport http {\n"
        "\t\t\ttls_server_name entry-hop.invalid\n"
        "\t\t}\n\t}\n}\n"
    )


class TestCaddy:

    def test_exits_caddy_over_the_pinned_hop(self, bot, tmp_path):
        _need(CADDY)
        import sys
        sys.path.insert(0, str(REPO / 'scripts'))
        try:
            import caddy_entry_hop
        finally:
            sys.path.pop(0)
        pins = tmp_path / 'pins'
        pins.mkdir()
        shutil.copy(bot.cert, pins / 'entry-hop.pem')
        other = tmp_path / 'other'
        hop_tls.generate(str(other), '127.0.0.1')
        shutil.copy(other / 'cert.pem', pins / 'other.pem')
        rules = tmp_path / 'rule-sets'
        rules.mkdir()
        (rules / 'geosite-x.srs').write_bytes(b'rule-set bytes')
        dash, mirror = free_port(), free_port()
        broken = (free_port(), free_port(), free_port())
        old = _exit_shaped_caddyfile(bot.port, dash, mirror)
        caddyfile = tmp_path / 'Caddyfile'
        caddyfile.write_text(old)
        # the tool, as on exit, minus the reload (this Caddy starts from the
        # file); `caddy validate` is the real one, in the container
        validate = tmp_path / 'validate.sh'
        validate.write_text(
            '#!/bin/sh\nexec docker run --rm --network none '
            f'-v {caddyfile}:/etc/caddy/Caddyfile:ro -v {pins}:{pins}:ro '
            f'{CADDY} caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile\n')
        validate.chmod(0o755)
        rc = caddy_entry_hop.main([
            '--caddyfile', str(caddyfile), '--entry', '127.0.0.1',
            '--old-port', str(bot.port), '--port', str(bot.tls_port),
            '--pin', str(pins / 'entry-hop.pem'), '--apply',
            '--caddy', str(validate), '--reload', ''])
        assert rc == 0
        new = caddyfile.read_text()
        assert new.count('import entry_hop') == 2 and str(bot.port) not in new
        # the pin as the container sees it
        caddyfile.write_text(new.replace(str(pins / 'entry-hop.pem'), f'{PIN_DIR}/entry-hop.pem')
                             + _broken_sites(bot.tls_port, broken))
        caddy = _Container(f'hop-caddy-{dash}', '-v', f'{caddyfile}:/etc/caddy/Caddyfile:ro',
                           '-v', f'{pins}:{PIN_DIR}:ro', '-v', f'{rules}:/srv/rule-sets:ro',
                           CADDY)
        try:
            _wait(lambda: _get(f'http://127.0.0.1:{dash}/health')[0] == 200, 'caddy up')
            direct = json.loads(bot.plain('/health')[1])
            token = bot.srv.subscription.derive_token(PROBE_UUID)
            profile = bot.plain(f'/sub/{token}?format=clash')[1]
            for front in (dash, mirror):
                status, body = _get(f'http://127.0.0.1:{front}/health')
                assert status == 200 and json.loads(body) == direct
                assert _get(f'http://127.0.0.1:{front}/sub/{token}?format=clash') == (200, profile)
                # arrived over the TLS listener, from Caddy, for the client
                peer = json.loads(_get(f'http://127.0.0.1:{front}/_peer')[1])
                assert peer == {'remote': '127.0.0.1', 'xff': '127.0.0.1', 'secure': True}
            assert _get(f'http://127.0.0.1:{mirror}/rule-sets/geosite-x.srs') == \
                (200, b'rule-set bytes')
            status, body = _get(f'http://127.0.0.1:{dash}/api/hy2/auth',
                                data=json.dumps({'auth': UUID, 'addr': '1.2.3.4:5'}).encode(),
                                headers={'Content-Type': 'application/json'})
            assert (status, json.loads(body)) == (200, {'ok': True, 'id': 'u42@x'})
            for front in broken:
                assert _get(f'http://127.0.0.1:{front}/_peer')[0] == 502, front
            logs = caddy.logs()
            assert 'certificate signed by unknown authority' in logs
            assert 'entry.example.com' in logs
        finally:
            caddy.close()

    def test_the_runbook_shows_the_snippet_the_tool_writes(self):
        import sys
        sys.path.insert(0, str(REPO / 'scripts'))
        try:
            import caddy_entry_hop
        finally:
            sys.path.pop(0)
        text = RUNBOOK.read_text()
        block = caddy_entry_hop.snippet('130.49.146.10', 8448, '/etc/nekovpn-hop/entry-hop.pem')
        assert block in text


# ---------------------------------------------------------------- hysteria --

class _Web(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        out = b'through the tunnel'
        self.send_response(200)
        self.send_header('Content-Length', str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def _auth_rows(db):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            'SELECT chat_id, decision FROM hy2_auth_log ORDER BY id').fetchall()]


class TestHysteria:

    def _stand(self, bot, tmp_path, *, with_pin_env, password):
        work = tmp_path / f'hy-{with_pin_env}-{password[:8]}'
        work.mkdir()
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                        'ec_paramgen_curve:prime256v1', '-nodes', '-days', '2',
                        '-keyout', str(work / 'qkey.pem'), '-out', str(work / 'qcert.pem'),
                        '-subj', '/CN=hy2.example.com'], check=True, capture_output=True)
        (work / 'qkey.pem').chmod(0o644)
        udp, http_proxy = free_port(), free_port()
        # the runbook's auth URL, on the stand's port; a resolver of hysteria's
        # own that answers nothing: the callback must not depend on it
        (work / 'server.yaml').write_text(
            f'listen: 127.0.0.1:{udp}\n'
            'tls:\n  cert: /w/qcert.pem\n  key: /w/qkey.pem\n'
            'auth:\n  type: http\n  http:\n'
            f'    url: https://entry-hop.invalid:{bot.tls_port}/api/hy2/auth\n'
            'resolver:\n  type: udp\n  udp:\n    addr: 127.0.0.1:9\n    timeout: 2s\n')
        (work / 'client.yaml').write_text(
            f'server: 127.0.0.1:{udp}\nauth: {password}\n'
            'tls:\n  sni: hy2.example.com\n  insecure: true\n'
            f'http:\n  listen: 127.0.0.1:{http_proxy}\n')
        pins = work / 'pins'
        pins.mkdir()
        shutil.copy(bot.cert, pins / 'entry-hop.pem')
        # the drop-in's hosts file: the name resolves to "entry"
        (pins / 'hosts').write_text('127.0.0.1 localhost\n127.0.0.1 entry-hop.invalid\n')
        env, binds = _drop_in_env()
        assert env == {'SSL_CERT_DIR': f'/etc/ssl/certs:{PIN_DIR}'}
        assert binds == [(f'{PIN_DIR}/hosts', '/etc/hosts')]
        args = ['-v', f'{work}:/w:ro', '-v', f'{pins}:{PIN_DIR}:ro']
        for src, dst in binds:
            args += ['-v', f'{pins / src.rsplit("/", 1)[1]}:{dst}:ro']
        if with_pin_env:
            args += [f'-e{k}={v}' for k, v in env.items()]
        quiet = ('-e', 'HYSTERIA_DISABLE_UPDATE_CHECK=1')
        server = _Container(f'hop-hy-server-{udp}', *args, *quiet, HYSTERIA, 'server', '-c',
                            '/w/server.yaml')
        client = _Container(f'hop-hy-client-{udp}', '-v', f'{work}:/w:ro', *quiet, HYSTERIA,
                            'client', '-c', '/w/client.yaml')
        return server, client, http_proxy

    def _through(self, http_proxy, web_port, timeout=25):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {'http': f'http://127.0.0.1:{http_proxy}'}))

        def once():
            with opener.open(f'http://127.0.0.1:{web_port}/', timeout=5) as r:
                return r.read()
        return _wait(once, 'a request through hysteria', timeout)

    @pytest.fixture
    def web_port(self):
        srv = http.server.HTTPServer(('127.0.0.1', 0), _Web)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield srv.server_address[1]
        srv.shutdown()

    def _settle(self, bot):
        import asyncio

        async def drain():
            while bot.srv._bg_tasks:
                await asyncio.gather(*list(bot.srv._bg_tasks), return_exceptions=True)
        asyncio.run_coroutine_threadsafe(drain(), bot.loop).result(10)
        bot.srv._hy2_record_pool.submit(lambda: None).result(10)

    def test_a_paid_uuid_gets_through_over_the_pinned_hop(self, bot, tmp_path, web_port):
        _need(HYSTERIA)
        server, client, proxy = self._stand(bot, tmp_path, with_pin_env=True, password=UUID)
        try:
            assert self._through(proxy, web_port) == b'through the tunnel'
            self._settle(bot)
            assert ('42', 'allow') in _auth_rows(bot.db)
        finally:
            client.close()
            server.close()

    def test_an_unknown_uuid_is_denied_by_the_bot(self, bot, tmp_path, web_port):
        _need(HYSTERIA)
        stranger = str(uuidlib.uuid4())
        server, client, proxy = self._stand(bot, tmp_path, with_pin_env=True, password=stranger)
        try:
            with pytest.raises(AssertionError):
                self._through(proxy, web_port, timeout=8)
            self._settle(bot)
            assert _auth_rows(bot.db) == [(None, 'deny')]   # the bot answered, and said no
            assert 'authentication error' in client.logs()
        finally:
            client.close()
            server.close()

    def test_without_the_pin_the_callback_never_reaches_the_bot(self, bot, tmp_path, web_port):
        _need(HYSTERIA)
        server, client, proxy = self._stand(bot, tmp_path, with_pin_env=False, password=UUID)
        try:
            with pytest.raises(AssertionError):
                self._through(proxy, web_port, timeout=8)
            # the only difference from the passing stand is the pin's
            # SSL_CERT_DIR: hysteria (which logs no reason) could not verify
            # the bot, and no request reached it
            self._settle(bot)
            assert _auth_rows(bot.db) == []
            assert 'authentication error' in client.logs()
        finally:
            client.close()
            server.close()


# --------------------------------------------------------------------- Go ---

NC_GO = r'''package main

import (
	"crypto/x509"
	"encoding/pem"
	"fmt"
	"os"
)

func load(p string) *x509.Certificate {
	b, err := os.ReadFile(p)
	if err != nil {
		panic(err)
	}
	blk, _ := pem.Decode(b)
	c, err := x509.ParseCertificate(blk.Bytes)
	if err != nil {
		panic(err)
	}
	return c
}

func main() {
	roots := x509.NewCertPool()
	roots.AddCert(load(os.Args[1]))
	for i := 2; i+1 < len(os.Args); i += 2 {
		_, err := load(os.Args[i]).Verify(x509.VerifyOptions{Roots: roots, DNSName: os.Args[i+1]})
		if err != nil {
			fmt.Printf("%s REJECT %v\n", os.Args[i+1], err)
		} else {
			fmt.Printf("%s OK\n", os.Args[i+1])
		}
	}
}
'''


class TestGoNameConstraints:

    def test_a_leaked_key_vouches_for_nothing_else_in_go(self, tmp_path):
        _need(GO)
        d = tmp_path / 'hop'
        cert = hop_tls.generate(str(d), '203.0.113.20')
        cases = []
        for i, (san, host) in enumerate((('DNS:entry-hop.invalid', 'entry-hop.invalid'),
                                         ('DNS:www.bing.com', 'www.bing.com'),
                                         ('IP:8.8.8.8', '8.8.8.8'),
                                         ('IP:203.0.113.20', '203.0.113.20'))):
            key, csr, leaf = (tmp_path / f'l{i}.key', tmp_path / f'l{i}.csr',
                              tmp_path / f'l{i}.pem')
            ext = tmp_path / f'l{i}.cnf'
            ext.write_text(f'subjectAltName={san}\nextendedKeyUsage=serverAuth\n')
            subprocess.run(['openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt',
                            'ec_paramgen_curve:prime256v1', '-nodes', '-keyout', str(key),
                            '-out', str(csr), '-subj', '/CN=forged'],
                           check=True, capture_output=True)
            subprocess.run(['openssl', 'x509', '-req', '-in', str(csr), '-CA', cert,
                            '-CAkey', str(d / 'key.pem'), '-set_serial', str(10 + i),
                            '-days', '1', '-out', str(leaf), '-extfile', str(ext)],
                           check=True, capture_output=True)
            cases += [f'/w/l{i}.pem', host]
        shutil.copy(cert, tmp_path / 'pin.pem')
        (tmp_path / 'nc.go').write_text(NC_GO)
        r = _docker('run', '--rm', '--network', 'none', '-v', f'{tmp_path}:/w:ro',
                    '-e', 'GOCACHE=/tmp/gocache', '-e', 'GOFLAGS=-buildvcs=false', GO,
                    'go', 'run', '/w/nc.go', '/w/pin.pem', *cases,
                    # the pin itself as the leaf: what the listener serves
                    '/w/pin.pem', 'entry-hop.invalid', '/w/pin.pem', '203.0.113.20',
                    '/w/pin.pem', 'entry.example.com', timeout=300)
        out = r.stdout.strip().splitlines()
        assert out[0] == 'entry-hop.invalid OK'             # its own name
        assert out[1].startswith('www.bing.com REJECT') and 'not authorized' in out[1], out
        assert out[2].startswith('8.8.8.8 REJECT') and 'not authorized' in out[2], out
        assert out[3] == '203.0.113.20 OK'                  # entry's address
        # the pin itself, as the listener serves it: its two names, nothing else
        assert out[4:6] == ['entry-hop.invalid OK', '203.0.113.20 OK']
        assert out[6].startswith('entry.example.com REJECT') and len(out) == 7, out
