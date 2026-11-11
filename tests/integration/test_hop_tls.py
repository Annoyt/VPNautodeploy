"""TLS on the exit → entry hop (AGENTS.md §53; bot/services/hop_tls.py,
scripts/hop_tls.py).

What is pinned, on the real WebAppServer and a real sqlite:
  * the certificate: names entry-hop.invalid and entry's address and nothing
    else, its name constraints refuse anything a leaked key would sign for
    another name or address, the key is 0600 in a 0700 directory, and a
    pair is never replaced without --force;
  * the listener: the same app over TLS 1.3 only (same routes: /health, the
    hy2 auth callback hysteria calls, /sub), the caller's address unchanged,
    refused to a client that does not pin the certificate, and off without a
    certificate; a TLS listener that cannot start leaves the plain one up;
  * ``check`` (the deploy smoke) and ``verify`` (the cutover check: /health
    and the probe account's profile through every path) report codes, sizes
    and hashes, never the token, the profile or the key.
The stands against the real Caddy and hysteria are in test_hop_tls_stand.py.
"""

import asyncio
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.services import hop_tls

from .hop_tls_support import ENTRY_IP, PROBE_UUID, REPO, UUID, make_config
from .hop_tls_support import Server as _Server
from .hop_tls_support import free_port as _free_port

def _openssl_text(path):
    return subprocess.run(['openssl', 'x509', '-in', str(path), '-noout', '-text'],
                          capture_output=True, text=True, check=True).stdout


# ------------------------------------------------------------ the certificate

class TestCertificate:

    def test_names_constraints_and_modes(self, tmp_path):
        d = tmp_path / 'hop-tls'
        cert = hop_tls.generate(str(d), ENTRY_IP)
        text = _openssl_text(cert)
        assert 'DNS:entry-hop.invalid, IP Address:203.0.113.20' in text
        constraints = text.split('X509v3 Name Constraints: critical')[1].split('Signature')[0]
        assert constraints.split() == ['Permitted:', 'DNS:entry-hop.invalid',
                                       'IP:203.0.113.20/255.255.255.255']
        assert 'CA:TRUE, pathlen:0' in text
        assert 'TLS Web Server Authentication' in text
        assert 'prime256v1' in text or 'P-256' in text
        assert (os.stat(d).st_mode & 0o777) == 0o700
        assert (os.stat(d / 'key.pem').st_mode & 0o777) == 0o600
        assert (os.stat(cert).st_mode & 0o777) == 0o644
        assert sorted(os.listdir(d)) == ['cert.pem', 'key.pem']      # no temp left
        not_after = subprocess.run(['openssl', 'x509', '-in', cert, '-noout', '-checkend',
                                   str(3640 * 86400)], capture_output=True).returncode
        assert not_after == 0                                    # ~10 years

    def test_modes_whatever_openssl_writes(self, tmp_path):
        # an openssl that leaves the key world-readable, and a directory
        # that already exists 0755: generate tightens both
        loose = tmp_path / 'openssl'
        loose.write_text('#!/bin/sh\nopenssl "$@" || exit $?\n'
                         'for a; do case "$prev" in -keyout) chmod 0644 "$a";; esac; prev=$a; done\n')
        loose.chmod(0o755)
        d = tmp_path / 'hop-tls'
        d.mkdir(mode=0o755)
        d.chmod(0o755)
        hop_tls.generate(str(d), ENTRY_IP, openssl=str(loose))
        assert (os.stat(d).st_mode & 0o777) == 0o700
        assert (os.stat(d / 'key.pem').st_mode & 0o777) == 0o600

    def test_ipv6_entry(self, tmp_path):
        cert = hop_tls.generate(str(tmp_path / 'd'), '2001:db8::7')
        text = _openssl_text(cert)
        assert 'IP Address:2001:DB8:0:0:0:0:0:7' in text
        assert 'IP:2001:DB8:0:0:0:0:0:7/FFFF:FFFF:FFFF:FFFF:FFFF:FFFF:FFFF:FFFF' in text

    def test_never_replaced_without_force(self, tmp_path):
        d = str(tmp_path / 'd')
        cert = hop_tls.generate(d, ENTRY_IP)
        before = Path(cert).read_text()
        with pytest.raises(FileExistsError):
            hop_tls.generate(d, ENTRY_IP)
        assert Path(cert).read_text() == before
        os.unlink(cert)                    # a key alone also blocks it
        with pytest.raises(FileExistsError):
            hop_tls.generate(d, ENTRY_IP)
        hop_tls.generate(d, ENTRY_IP, force=True)
        assert Path(cert).read_text() != before

    @pytest.mark.parametrize('ip', ['', 'entry.example.com', '203.0.113.300'])
    def test_an_address_is_required(self, tmp_path, ip):
        with pytest.raises(ValueError):
            hop_tls.generate(str(tmp_path / 'd'), ip)
        assert not (tmp_path / 'd').exists()

    def test_fingerprint_is_what_openssl_prints_on_exit(self, tmp_path):
        cert = hop_tls.generate(str(tmp_path / 'd'), ENTRY_IP)
        out = subprocess.run(['openssl', 'x509', '-in', cert, '-noout', '-fingerprint',
                              '-sha256'], capture_output=True, text=True).stdout
        assert out.strip().split('=', 1)[1] == hop_tls.describe(cert)['sha256']
        info = hop_tls.describe(cert)
        assert info['names'] == 'DNS:entry-hop.invalid, IP Address:203.0.113.20'
        assert 'PRIVATE' not in info['pem'] and info['pem'].startswith('-----BEGIN CERTIFICATE')

    def _forge(self, tmp_path, cert, key, *, san):
        """A leaf the hop key signs for ``san``: what a leaked key could make."""
        csr_key = tmp_path / 'forged.key'
        leaf = tmp_path / 'forged.pem'
        ext = tmp_path / 'ext.cnf'
        ext.write_text(f'subjectAltName={san}\nextendedKeyUsage=serverAuth\n'
                       'basicConstraints=critical,CA:FALSE\n')
        csr = tmp_path / 'forged.csr'
        subprocess.run(['openssl', 'req', '-new', '-newkey', 'ec', '-pkeyopt',
                        'ec_paramgen_curve:prime256v1', '-nodes', '-keyout', str(csr_key),
                        '-out', str(csr), '-subj', '/CN=forged'], check=True, capture_output=True)
        subprocess.run(['openssl', 'x509', '-req', '-in', str(csr), '-CA', cert, '-CAkey', key,
                        '-set_serial', '7', '-days', '1', '-out', str(leaf), '-extfile', str(ext)],
                       check=True, capture_output=True)
        return str(leaf), str(csr_key)

    @pytest.mark.parametrize('san,host,ok', [
        ('DNS:entry-hop.invalid', 'entry-hop.invalid', True),
        ('DNS:www.bing.com', 'www.bing.com', False),
        ('IP:8.8.8.8', '8.8.8.8', False),
    ])
    def test_a_leaked_key_vouches_for_nothing_else(self, tmp_path, san, host, ok):
        # what the name constraints are for: the pin sits next to the
        # system's CAs in hysteria's process (SSL_CERT_DIR)
        d = tmp_path / 'd'
        cert = hop_tls.generate(str(d), ENTRY_IP)
        leaf, leaf_key = self._forge(tmp_path, cert, str(d / 'key.pem'), san=san)
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(leaf, leaf_key)
        port, done = _tls_echo(server)
        try:
            with socket.create_connection(('127.0.0.1', port), 5) as raw:
                hop_tls.client_context(cert).wrap_socket(raw, server_hostname=host).close()
            accepted = True
        except ssl.SSLCertVerificationError as e:
            accepted = False
            assert 'name constraint' in str(e).lower() or 'permitted' in str(e).lower(), e
        finally:
            done.set()
        assert accepted is ok


def _tls_echo(context):
    """A one-thread TLS acceptor that completes handshakes and closes."""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    s.listen()
    s.settimeout(0.5)
    done = threading.Event()

    def run():
        while not done.is_set():
            try:
                c, _ = s.accept()
            except OSError:
                continue
            try:
                context.wrap_socket(c, server_side=True).close()
            except (OSError, ssl.SSLError):
                c.close()
        s.close()
    threading.Thread(target=run, daemon=True).start()
    return s.getsockname()[1], done


# ------------------------------------------------------------ settings

class TestSettings:

    def test_defaults(self):
        assert hop_tls.settings(SimpleNamespace()) == (8448, '/var/lib/vpn-bot/hop-tls')

    @pytest.mark.parametrize('value', [0, '0', '', 'x', None, -1, 65536])
    def test_off(self, value):
        assert hop_tls.settings(SimpleNamespace(WEB_TLS_PORT=value))[0] == 0

    def test_settings_class_reads_the_environment(self, monkeypatch):
        from bot.config.settings import Settings
        monkeypatch.delenv('WEB_TLS_PORT', raising=False)
        monkeypatch.delenv('WEB_TLS_DIR', raising=False)
        s = Settings()
        assert (s.WEB_TLS_PORT, s.WEB_TLS_DIR) == (8448, '/var/lib/vpn-bot/hop-tls')
        monkeypatch.setenv('WEB_TLS_PORT', 'nope')
        monkeypatch.setenv('WEB_TLS_DIR', '/x/y')
        s = Settings()
        assert (s.WEB_TLS_PORT, s.WEB_TLS_DIR) == (0, '/x/y')

    def test_listener_is_off_without_a_certificate(self, tmp_path, caplog):
        caplog.set_level('INFO')
        cfg = make_config(WEB_TLS_PORT=8448, WEB_TLS_DIR=str(tmp_path / 'none'))
        assert hop_tls.listener(cfg) is None
        assert 'hop_tls.py init' in caplog.text
        assert hop_tls.listener(make_config(WEB_TLS_PORT=0, WEB_TLS_DIR=str(tmp_path))) is None

    def test_a_pair_that_does_not_load_is_logged_and_off(self, tmp_path, caplog):
        a, b = tmp_path / 'a', tmp_path / 'b'
        hop_tls.generate(str(a), ENTRY_IP)
        hop_tls.generate(str(b), ENTRY_IP)
        os.replace(b / 'key.pem', a / 'key.pem')        # a mismatched pair
        cfg = make_config(WEB_TLS_PORT=8448, WEB_TLS_DIR=str(a))
        assert hop_tls.listener(cfg) is None
        assert 'does not load' in caplog.text

    def test_the_client_trusts_the_pin_alone(self, tmp_path):
        # no system CA can stand in for the pin (Let's Encrypt issues
        # certificates for IP addresses)
        cert = hop_tls.generate(str(tmp_path / 'd'), ENTRY_IP)
        ctx = hop_tls.client_context(cert)
        assert ctx.cert_store_stats()['x509_ca'] == 1
        assert [c['subject'] for c in ctx.get_ca_certs()] == [((('commonName', 'entry-hop.invalid'),),)]
        assert ctx.check_hostname and ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3

    def test_port_0_is_off_with_a_certificate_too(self, tmp_path):
        hop_tls.generate(str(tmp_path / 'd'), ENTRY_IP)
        assert hop_tls.listener(make_config(WEB_TLS_PORT=0, WEB_TLS_DIR=str(tmp_path / 'd'))) is None

    def test_listener_context(self, tmp_path):
        hop_tls.generate(str(tmp_path / 'd'), ENTRY_IP)
        port, ctx = hop_tls.listener(make_config(WEB_TLS_PORT=9443, WEB_TLS_DIR=str(tmp_path / 'd')))
        assert port == 9443
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3


# ------------------------------------------------------------ the real server

@pytest.fixture
def server(tmp_path):
    s = _Server(tmp_path)
    yield s
    s.close()


class TestListener:

    def test_the_same_app_over_tls(self, server):
        status, body, peer = server.hop('/health')
        assert status == 200
        assert json.loads(body) == json.loads(server.plain('/health')[1])
        assert ('IP Address', ENTRY_IP) in peer['subjectAltName']
        token = server.srv.subscription.derive_token(UUID)
        status, body, _ = server.hop(f'/sub/{token}?format=clash')
        assert status == 200 and body == server.plain(f'/sub/{token}?format=clash')[1]

    def test_the_callers_address_is_kept(self, server):
        _s, body, _p = server.hop('/_peer', headers={'X-Forwarded-For': '198.51.100.7'})
        assert json.loads(body) == {'remote': '127.0.0.1', 'xff': '198.51.100.7',
                                    'secure': True}

    def test_hysterias_auth_callback(self, server):
        # what both hysteria instances post on every new connection: the
        # password is the uuid, the reason the hop needs TLS at all
        ctx = hop_tls.client_context(server.cert)
        conn = hop_tls._NamedHTTPS('127.0.0.1', server.tls_port, hop_tls.HOP_NAME, ctx, 10)
        conn.request('POST', '/api/hy2/auth',
                     body=json.dumps({'auth': UUID, 'addr': '130.49.146.10:5555', 'tx': 0}),
                     headers={'Content-Type': 'application/json'})
        resp = conn.getresponse()
        assert resp.status == 200 and json.loads(resp.read()) == {'ok': True, 'id': 'u42@x'}
        conn.close()

    def test_a_client_without_the_pin_is_refused(self, server, tmp_path):
        other = hop_tls.generate(str(tmp_path / 'other'), ENTRY_IP)
        with pytest.raises(ssl.SSLCertVerificationError):
            hop_tls._hop_get('127.0.0.1', server.tls_port, other, '/health')
        # nor do the system's CAs stand in for it
        with pytest.raises(urllib.error.URLError) as e:
            urllib.request.urlopen(f'https://127.0.0.1:{server.tls_port}/health', timeout=5,
                                   context=ssl.create_default_context())
        assert isinstance(e.value.reason, ssl.SSLCertVerificationError)

    def test_the_wrong_name_is_refused(self, server):
        with pytest.raises(ssl.SSLCertVerificationError):
            server.hop('/health', name='entry.example.com')
        # entry's address is the other name it carries
        assert server.hop('/health', name=ENTRY_IP)[0] == 200

    def test_http_1_1_by_alpn(self, server):
        # Caddy offers h2 first; aiohttp speaks HTTP/1.1 only
        ctx = hop_tls.client_context(server.cert)
        ctx.set_alpn_protocols(['h2', 'http/1.1'])
        with socket.create_connection(('127.0.0.1', server.tls_port), 5) as raw:
            with ctx.wrap_socket(raw, server_hostname=hop_tls.HOP_NAME) as tls:
                assert tls.selected_alpn_protocol() == 'http/1.1'
                assert tls.version() == 'TLSv1.3'

    def test_tls_12_is_refused(self, server):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_verify_locations(server.cert)
        with socket.create_connection(('127.0.0.1', server.tls_port), 5) as raw:
            with pytest.raises(ssl.SSLError):
                ctx.wrap_socket(raw, server_hostname=hop_tls.HOP_NAME)

    def test_plain_http_gets_no_answer_on_the_tls_port(self, server):
        with pytest.raises(Exception):
            urllib.request.urlopen(f'http://127.0.0.1:{server.tls_port}/health', timeout=5)

    def test_a_tls_port_that_is_taken_leaves_the_plain_one(self, tmp_path, caplog):
        taken = socket.socket()
        taken.bind(('127.0.0.1', 0))
        taken.listen()
        hop_tls.generate(str(tmp_path / 'd'), ENTRY_IP)
        cfg = make_config(WEB_TLS_PORT=taken.getsockname()[1], WEB_TLS_DIR=str(tmp_path / 'd'))
        srv = WebAppServer(cfg, Database(str(tmp_path / 'bot.db')), xui_service=Mock())
        loop = asyncio.new_event_loop()
        runner = web.AppRunner(srv.app)
        loop.run_until_complete(runner.setup())
        port = _free_port()
        try:
            sites = loop.run_until_complete(
                srv.start_sites(runner, '127.0.0.1', port, hop_tls.listener(cfg)))
            assert len(sites) == 1 and 'did not start' in caplog.text
        finally:
            loop.run_until_complete(runner.cleanup())
            loop.close()
            taken.close()

    def test_no_certificate_no_tls_listener(self, tmp_path):
        s = _Server(tmp_path, tls_dir=str(tmp_path / 'empty'))
        try:
            assert len(s.sites) == 1
            assert s.plain('/health')[0] == 200
            with pytest.raises(OSError):
                socket.create_connection(('127.0.0.1', s.tls_port), 2).close()
        finally:
            s.close()

    def test_run_in_thread_starts_both(self, tmp_path):
        hop_tls.generate(str(tmp_path / 'd'), ENTRY_IP)
        tls_port, port = _free_port(), _free_port()
        cfg = make_config(WEB_TLS_PORT=tls_port, WEB_TLS_DIR=str(tmp_path / 'd'))
        WebAppServer(cfg, Database(str(tmp_path / 'bot.db')), xui_service=Mock()).run_in_thread(port)
        deadline = time.monotonic() + 10
        while True:
            try:
                status, _b, _p = hop_tls._hop_get('127.0.0.1', tls_port,
                                                  str(tmp_path / 'd' / 'cert.pem'), '/health')
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        assert status == 200


# ------------------------------------------------------------ check / verify

class TestCheck:

    def test_ok(self, server):
        from bot.core.web_server import _deployed_version
        result = hop_tls.check(server.config)
        assert result == {'state': 'ok', 'port': server.tls_port,
                          'version': _deployed_version(), 'status': 'healthy'}

    def test_off(self, tmp_path):
        assert hop_tls.check(make_config(WEB_TLS_PORT=0))['state'] == 'off'
        cfg = make_config(WEB_TLS_PORT=8448, WEB_TLS_DIR=str(tmp_path))
        assert hop_tls.check(cfg) == {'state': 'off', 'reason': f'no certificate in {tmp_path}'}

    def test_fails_when_nothing_listens(self, tmp_path):
        hop_tls.generate(str(tmp_path / 'd'), ENTRY_IP)
        cfg = make_config(WEB_TLS_PORT=_free_port(), WEB_TLS_DIR=str(tmp_path / 'd'))
        result = hop_tls.check(cfg)
        assert result['state'] == 'fail' and 'ConnectionRefused' in result['error']

    def test_fails_when_the_certificate_misses_entrys_address(self, server):
        # the reporter dials entry's address: a certificate made before an
        # address change would pass Caddy (by name) and fail the reporter
        server.config.ENTRY_NODE_IP = '198.51.100.99'
        result = hop_tls.check(server.config)
        assert result['state'] == 'fail' and '198.51.100.99' in result['error']

    def test_fails_when_another_certificate_answers(self, server, tmp_path):
        other = str(tmp_path / 'other')
        hop_tls.generate(other, ENTRY_IP)
        cfg = make_config(WEB_TLS_PORT=server.tls_port, WEB_TLS_DIR=other,
                          ENTRY_NODE_IP=ENTRY_IP)
        result = hop_tls.check(cfg)
        assert result['state'] == 'fail' and 'SSLCertVerificationError' in result['error']


class TestVerify:

    def test_every_path_answers_the_same(self, tmp_path):
        s = _Server(tmp_path)
        try:
            s.config.WEBAPP_URL = f'http://127.0.0.1:{s.port}/'
            s.config.SUB_MIRROR_URLS = f'http://localhost:{s.port}'
            result = hop_tls.verify(s.config, s.config.DB_PATH)
            assert result['ok'], result['problems']
            assert [r['path'] for r in result['paths']] == ['hop', 'webapp', 'mirror-1']
            assert len({r['sub_clash']['sha256'] for r in result['paths']}) == 1
            assert len({r['sub']['sha256'] for r in result['paths']}) == 1
            assert result['paths'][0]['sub_clash']['size'] > 1000
            assert {r['health']['status'] for r in result['paths']} == {200}
            assert len({r['health']['version'] for r in result['paths']}) == 1
            token = s.srv.subscription.derive_token(PROBE_UUID)
            assert token not in json.dumps(result)
        finally:
            s.close()

    def test_a_path_that_answers_otherwise_is_a_problem(self, tmp_path):
        s = _Server(tmp_path)
        (tmp_path / 'x').mkdir()
        other = _Server(tmp_path / 'x', BOT_TOKEN='another-bot')
        try:
            s.config.WEBAPP_URL = f'http://127.0.0.1:{s.port}'
            # a mirror that reaches another bot: the probe token is unknown there
            s.config.SUB_MIRROR_URLS = f'http://127.0.0.1:{other.port}'
            result = hop_tls.verify(s.config, s.config.DB_PATH)
            assert not result['ok']
            assert 'mirror-1 sub_clash: HTTP 404' in result['problems']
            assert 'sub_clash: the bodies differ between paths' in result['problems']
        finally:
            s.close()
            other.close()

    def test_an_unreachable_path_is_a_problem_not_a_crash(self, server):
        server.config.WEBAPP_URL = f'http://127.0.0.1:{_free_port()}'
        result = hop_tls.verify(server.config, server.config.DB_PATH)
        assert not result['ok']
        assert result['paths'][1]['health'] == {'error': 'URLError'}

    def test_the_public_paths_ignore_the_containers_proxy(self, server, monkeypatch):
        # the container's HTTPS_PROXY is the Telegram egress (tinyproxy, :443
        # only): a fetch through it would fail for the wrong reason
        for k in ('no_proxy', 'NO_PROXY'):
            monkeypatch.delenv(k, raising=False)
        for k in ('http_proxy', 'HTTP_PROXY', 'https_proxy', 'HTTPS_PROXY'):
            monkeypatch.setenv(k, f'http://127.0.0.1:{_free_port()}')
        server.config.WEBAPP_URL = f'http://127.0.0.1:{server.port}'
        result = hop_tls.verify(server.config, server.config.DB_PATH)
        assert result['ok'], result['problems']

    def test_a_missing_bot_db_is_a_problem_and_never_created(self, tmp_path):
        missing = tmp_path / 'bot.db'                 # a directory that exists
        result = hop_tls.verify(make_config(), str(missing))
        assert not result['ok'] and 'bot.db is not readable' in result['problems'][0]
        assert not missing.exists()

    def test_no_probe_account(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        result = hop_tls.verify(make_config(), str(tmp_path / 'bot.db'))
        assert not result['ok'] and 'probe_internal' in result['problems'][0]
        del db

    def test_reads_bot_db_read_only(self, server, tmp_path):
        os.chmod(server.config.DB_PATH, 0o444)
        try:
            assert hop_tls._probe_uuid(server.config.DB_PATH) == PROBE_UUID
        finally:
            os.chmod(server.config.DB_PATH, 0o644)


# ------------------------------------------------------------ the CLI

def _cli(*argv, config):
    sys.path.insert(0, str(REPO / 'scripts'))
    try:
        import importlib
        mod = importlib.import_module('hop_tls')
    finally:
        sys.path.pop(0)
    out = io.StringIO()
    return mod.main(list(argv), config=config, out=out), out.getvalue()


class TestCli:

    def test_init_show_check_verify(self, tmp_path):
        d = tmp_path / 'hop-tls'
        cfg = make_config(WEB_TLS_PORT=8448, WEB_TLS_DIR=str(d))
        rc, out = _cli('init', config=cfg)
        assert rc == 0 and 'BEGIN CERTIFICATE' in out and 'PRIVATE' not in out
        assert hop_tls.describe(str(d / 'cert.pem'))['sha256'] in out
        rc, out = _cli('init', config=cfg)
        assert rc == 1 and 'already holds' in out
        rc, pem = _cli('show', '--pem', config=cfg)
        assert rc == 0 and pem == (d / 'cert.pem').read_text().strip() + '\n'
        rc, out = _cli('check', config=cfg)
        assert rc == 1 and json.loads(out)['state'] == 'fail'      # nothing listens
        rc, out = _cli('init', '--ip', '198.51.100.1', '--force', config=cfg)
        assert rc == 0 and 'IP Address:198.51.100.1' in out

    def test_init_needs_an_address(self, tmp_path):
        cfg = make_config(WEB_TLS_DIR=str(tmp_path / 'd'), ENTRY_NODE_IP='')
        assert _cli('init', config=cfg)[0] == 2
        assert _cli('init', '--ip', 'entry.example.com', config=cfg)[0] == 2
        assert not (tmp_path / 'd').exists()

    def test_show_without_a_certificate(self, tmp_path):
        rc, out = _cli('show', config=make_config(WEB_TLS_DIR=str(tmp_path)))
        assert rc == 1 and 'no certificate' in out

    def test_check_and_verify_against_the_server(self, tmp_path):
        s = _Server(tmp_path)
        try:
            rc, out = _cli('check', config=s.config)
            assert rc == 0 and json.loads(out)['state'] == 'ok'
            s.config.WEBAPP_URL = f'http://127.0.0.1:{s.port}'
            rc, out = _cli('verify', config=s.config)
            assert rc == 0 and out.rstrip().endswith('OK: every path answers the same')
            assert s.srv.subscription.derive_token(PROBE_UUID) not in out
            s.config.SUB_MIRROR_URLS = f'http://127.0.0.1:{_free_port()}'
            rc, out = _cli('verify', config=s.config)
            assert rc == 1 and 'PROBLEM: mirror-1 health: URLError' in out
        finally:
            s.close()

    def test_runs_as_a_file_from_a_bare_interpreter(self, tmp_path):
        # the way the container runs it: python3 scripts/hop_tls.py, cwd /app
        d = tmp_path / 'hop-tls'
        hop_tls.generate(str(d), ENTRY_IP)
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env.update(WEB_TLS_DIR=str(d), PYTHONDONTWRITEBYTECODE='1')
        r = subprocess.run([sys.executable, str(REPO / 'scripts' / 'hop_tls.py'), 'show'],
                           cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert hop_tls.describe(str(d / 'cert.pem'))['sha256'] in r.stdout


# ------------------------------------------------------------ the runbook

RUNBOOK = REPO / 'docs' / 'runbooks' / 'entry_hop_tls.md'

# Commands the runbook's blocks run on the nodes: here they are fakes that log
# and succeed (iptables answers -S like entry), first in PATH; anything that
# would reach the machine's power state fails the test.
_FAKES = {
    'iptables': '#!/bin/sh\necho "iptables $*" >> "$LOG"\n'
                'if [ "$1" = -S ]; then printf "%s\\n" "-P FORWARD DROP" '
                '"-A DOCKER-USER -s 84.75.76.109/32 -p tcp -m tcp --dport 8080 -m comment --comment entry-fw-8080 -j RETURN" '
                '"-A DOCKER-USER -p tcp -m tcp --dport 8080 -m comment --comment entry-fw-8080 -j DROP"; fi\n',
    'systemctl': '#!/bin/sh\necho "systemctl $*" >> "$LOG"\n',
    'journalctl': '#!/bin/sh\necho "journalctl $*" >> "$LOG"\n',
    **{t: '#!/bin/sh\necho "TRIPWIRE $0 $*" >> "$LOG"\nexit 1\n'
       for t in ('reboot', 'shutdown', 'poweroff', 'halt', 'init')},
}


def _blocks(kind):
    import re
    text = RUNBOOK.read_text()
    if kind == 'bash':
        return re.findall(r'```bash\n(.*?)```', text, re.S)
    if kind == 'sh':
        return re.findall(r"bash -s[^\n]*<<'SH'\n(.*?)\n\s*SH\n", text, re.S)
    return re.findall(r"python3 - [^\n]*<<'PY'\n(.*?)\nPY\n", text, re.S)


class TestRunbook:
    """docs/runbooks/entry_hop_tls.md: every block parses, and the blocks
    that edit files on the nodes do exactly their edit, on copies."""

    @pytest.fixture
    def node(self, tmp_path):
        bin_dir = tmp_path / 'bin'
        bin_dir.mkdir()
        for name, body in _FAKES.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        log = tmp_path / 'calls.log'
        env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'LOG': str(log)}

        def run(body, *args, interpreter='bash'):
            cmd = ['bash', '-s', '--', *args] if interpreter == 'bash' else ['python3', '-', *args]
            return subprocess.run(cmd, input=body, env=env, capture_output=True, text=True,
                                  timeout=30)
        yield SimpleNamespace(root=tmp_path, run=run, log=log)
        assert 'TRIPWIRE' not in (log.read_text() if log.exists() else '')

    def test_every_block_parses(self):
        import ast
        bash = _blocks('bash') + _blocks('sh')
        assert len(_blocks('bash')) >= 20 and len(_blocks('sh')) >= 8
        for body in bash:
            r = subprocess.run(['bash', '-n'], input=body, capture_output=True, text=True)
            assert r.returncode == 0, (body[:80], r.stderr)
        pys = _blocks('py')
        assert len(pys) == 3
        for body in pys:
            ast.parse(body)

    def test_the_hysteria_block(self, node):
        body = next(b for b in _blocks('sh') if 'config-turbo.yaml' in b and 'install -D' in b)
        etc, units = node.root / 'hysteria', node.root / 'units'
        etc.mkdir()
        main = ('listen: :8400\nauth:\n  type: http\n  http:\n'
                '    url: http://130.49.146.10:8080/api/hy2/auth\n'
                'masquerade:\n  type: proxy\n  proxy:\n    url: https://www.bing.com/\n')
        (etc / 'config.yaml').write_text(main)
        (etc / 'config-turbo.yaml').write_text(
            'auth:\n  type: http\n  http:\n    url: http://130.49.146.10:8080/api/hy2t/auth\n')
        dropin = node.root / 'dropin.conf'
        dropin.write_text((REPO / 'systemd' / 'hysteria-entry-hop.conf').read_text())
        body = (body.replace('/etc/hysteria/', f'{etc}/')
                    .replace('/etc/systemd/system/', f'{units}/')
                    .replace('/tmp/hysteria-entry-hop.conf', str(dropin)))
        assert '/etc/' not in body.replace('/etc/hysteria', '')
        r = node.run(body, 'TS', '130.49.146.10')
        assert r.returncode == 0, r.stderr
        assert (etc / 'config.yaml').read_text() == main.replace(
            'http://130.49.146.10:8080/api/hy2/auth', 'https://entry-hop.invalid:8448/api/hy2/auth')
        assert 'url: https://entry-hop.invalid:8448/api/hy2t/auth' in (etc / 'config-turbo.yaml').read_text()
        assert (etc / 'config.yaml.bak-TS').read_text() == main
        for unit in ('hysteria', 'hysteria-turbo'):
            assert (units / f'{unit}.service.d' / 'entry-hop.conf').read_text() == \
                (REPO / 'systemd' / 'hysteria-entry-hop.conf').read_text()
        assert 'systemctl daemon-reload' in node.log.read_text()
        # a second run finds no line to move and touches nothing
        dropin.write_text('x')
        r = node.run(body, 'TS2', '130.49.146.10')
        assert r.returncode == 1 and 'not as expected' in r.stdout
        assert not (etc / 'config.yaml.bak-TS2').exists()

    def test_the_compose_block(self, node):
        body = next(b for b in _blocks('py') if 'docker-compose.yml' in b)
        d = node.root / 'opt'
        d.mkdir()
        compose = 'services:\n  vpn-bot:\n    ports:\n      - "${WEB_BIND:-127.0.0.1}:8080:8080"\n'
        (d / 'docker-compose.yml').write_text(compose)
        (d / '.env').write_text('BOT_TOKEN=x\nWEB_BIND=0.0.0.0')          # no final newline
        body = body.replace('/opt/vpn-bot/', f'{d}/')
        r = node.run(body, interpreter='python')
        assert r.returncode == 0, r.stderr
        assert (d / 'docker-compose.yml').read_text() == \
            compose + '      - "${WEB_TLS_BIND:-127.0.0.1}:8448:8448"\n'
        assert (d / '.env').read_text() == 'BOT_TOKEN=x\nWEB_BIND=0.0.0.0\nWEB_TLS_BIND=0.0.0.0\n'
        assert node.run(body, interpreter='python').returncode == 1      # once only
        assert (d / '.env').read_text().count('WEB_TLS_BIND') == 1

    def test_the_rules_v4_blocks(self, node):
        rules = node.root / 'rules.v4'
        rules.write_text(
            '*filter\n:DOCKER-USER - [0:0]\n'
            '-A DOCKER-USER -s 84.75.76.109/32 -p tcp -m tcp --dport 8080 -m comment --comment entry-fw-8080 -j RETURN\n'
            '-A DOCKER-USER -p tcp -m tcp --dport 8080 -m comment --comment entry-fw-8080 -j DROP\n'
            'COMMIT\n')
        insert = next(b for b in _blocks('py') if 'entry-fw-8448' in b)
        r = node.run(insert.replace('/etc/iptables/rules.v4', str(rules)), '84.75.76.109',
                     interpreter='python')
        assert r.returncode == 0, r.stderr
        lines = rules.read_text().splitlines()
        assert lines[2:4] == [
            '-A DOCKER-USER -s 84.75.76.109/32 -p tcp -m conntrack --ctorigdstport 8448 '
            '--ctdir ORIGINAL -m comment --comment entry-fw-8448 -j RETURN',
            '-A DOCKER-USER -p tcp -m conntrack --ctorigdstport 8448 --ctdir ORIGINAL '
            '-m comment --comment entry-fw-8448 -j DROP']
        remove = next(b for b in _blocks('py') if 'entry-fw-8080' in b)
        r = node.run(remove.replace('/etc/iptables/rules.v4', str(rules)), interpreter='python')
        assert r.returncode == 0, r.stderr
        text = rules.read_text()
        assert '--dport 8080 -m comment --comment entry-fw-8080 -j RETURN' not in text
        assert '--dport 8080 -m comment --comment entry-fw-8080 -j DROP' in text

    def test_closing_8080_deletes_exactly_the_exit_rule(self, node):
        body = next(b for b in _blocks('sh') if 'entry-fw-8080' in b)
        r = node.run(body)
        assert r.returncode == 0 and 'closed' in r.stdout, r.stderr
        assert ('iptables -D DOCKER-USER -s 84.75.76.109/32 -p tcp -m tcp --dport 8080 '
                '-m comment --comment entry-fw-8080 -j RETURN') in node.log.read_text()

    def test_the_reporter_block(self, node):
        body = next(b for b in _blocks('sh') if 'exit-dpi.env' in b and 'printf' in b)
        env = node.root / 'exit-dpi.env'
        env.write_text('DPI_REPORT_TOKEN=sekret\nBOT_URL=http://130.49.146.10:8080')
        (node.root / 'exit_dpi_reporter.py').write_text('old')
        (node.root / 'exit_dpi_reporter.py.new').write_text('new')
        body = body.replace('/etc/exit-dpi.env', str(env)).replace('/usr/local/bin/', f'{node.root}/')
        r = node.run(body, 'TS', '130.49.146.10')
        assert r.returncode == 0, r.stderr
        assert env.read_text() == ('DPI_REPORT_TOKEN=sekret\nBOT_URL=https://130.49.146.10:8448\n'
                                   'BOT_CA_FILE=/etc/nekovpn-hop/entry-hop.pem\n')
        assert 'sekret' not in r.stdout                      # only the two keys are shown
        assert (node.root / 'exit_dpi_reporter.py').read_text() == 'new'
        assert (node.root / 'exit-dpi.env.bak-TS').read_text().startswith('DPI_REPORT_TOKEN=')
