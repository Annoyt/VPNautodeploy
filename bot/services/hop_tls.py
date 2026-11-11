"""TLS on the exit → entry hop (AGENTS.md §53).

Everything exit sends to this bot crosses the public internet, abroad →
Russia: Caddy's two sites (the dashboard with its admin tokens and
initData, every /sub profile), both hysteria instances' auth callback (the
password is the user's uuid) and the DPI reporter's token. Over plain HTTP
on :8080 anyone recording that hop reads all of it.

The bot serves the same app over HTTPS on ``WEB_TLS_PORT`` (8448) once
``WEB_TLS_DIR`` holds ``cert.pem`` + ``key.pem``. ``scripts/hop_tls.py
init`` makes them inside the vpn-bot container, so the key never leaves the
bot's volume. The certificate is self-signed, and exit pins it: Caddy's
trust pool, hysteria's SSL_CERT_DIR, the reporter's BOT_CA_FILE.

It names ``entry-hop.invalid`` and entry's address. No public CA can ever
certify a ``.invalid`` name, so a client that also trusts the system's CAs
(hysteria) verifies that name. Its name constraints keep the certificate
from vouching for any other name or address, should its key ever leak.
"""

import hashlib
import http.client
import ipaddress
import json
import logging
import os
import sqlite3
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

# The only DNS name the certificate carries and its name constraints permit.
# RFC 2606 reserves .invalid: it resolves nowhere and no public CA issues it.
HOP_NAME = 'entry-hop.invalid'
DEFAULT_PORT = 8448
DEFAULT_DIR = '/var/lib/vpn-bot/hop-tls'
CERT_FILE = 'cert.pem'
KEY_FILE = 'key.pem'
VALID_DAYS = 3650
# The health checker's own account (scripts/gen_probe_config.py): its
# profile is what `verify` fetches through every path.
PROBE_CHAT_ID = 'probe_internal'
VERIFY_UA = 'nekovpn-hop-verify'


def settings(config) -> tuple:
    """``(port, directory)``; port 0 = the HTTPS listener is off."""
    raw = getattr(config, 'WEB_TLS_PORT', DEFAULT_PORT)
    try:
        port = int(raw)
    except (TypeError, ValueError):
        port = 0
    if not 0 < port < 65536:
        port = 0
    directory = (getattr(config, 'WEB_TLS_DIR', '') or '').strip() or DEFAULT_DIR
    return port, directory


def files(directory: str) -> tuple:
    """``(cert, key)`` paths in ``directory``."""
    return os.path.join(directory, CERT_FILE), os.path.join(directory, KEY_FILE)


def server_context(cert: str, key: str) -> ssl.SSLContext:
    """The listener's context: TLS 1.3 only (every client on exit speaks it,
    and 1.3 encrypts the certificate), HTTP/1.1 offered by ALPN, so Caddy
    does not try h2 against aiohttp."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.set_alpn_protocols(['http/1.1'])
    ctx.load_cert_chain(cert, key)
    return ctx


def listener(config, log=None) -> Optional[tuple]:
    """``(port, context)`` for the HTTPS listener, or None: off by config,
    no certificate yet, or one that does not load. Never raises: the plain
    listener and the bot run on either way."""
    log = log or logger
    port, directory = settings(config)
    if not port:
        return None
    cert, key = files(directory)
    if not (os.path.isfile(cert) and os.path.isfile(key)):
        log.info(f"hop TLS: off, no certificate in {directory} (scripts/hop_tls.py init)")
        return None
    try:
        return port, server_context(cert, key)
    except (OSError, ssl.SSLError, ValueError) as e:
        log.error(f"hop TLS: the certificate in {directory} does not load ({e}); "
                  f"the HTTPS listener on :{port} stays off")
        return None


def client_context(cafile: str) -> ssl.SSLContext:
    """Exit's view of the hop: the pinned certificate is the ONLY trust
    anchor (no system CAs), the name is checked, TLS 1.3."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.load_verify_locations(cafile=cafile)
    return ctx


# ------------------------------------------------------------ the certificate

def _openssl_config(ip: str) -> str:
    addr = ipaddress.ip_address(ip)
    mask = ipaddress.ip_network(f'{addr}/{addr.max_prefixlen}').netmask
    return (
        "[req]\n"
        "prompt = no\n"
        "distinguished_name = dn\n"
        "x509_extensions = ext\n"
        "[dn]\n"
        f"CN = {HOP_NAME}\n"
        "[ext]\n"
        # A CA so that its name constraints mean something to every
        # verifier (they are only defined for CAs); pathlen 0, and the key
        # stays on entry.
        "basicConstraints = critical, CA:TRUE, pathlen:0\n"
        "keyUsage = critical, digitalSignature, keyCertSign\n"
        "extendedKeyUsage = serverAuth\n"
        "subjectKeyIdentifier = hash\n"
        f"subjectAltName = DNS:{HOP_NAME}, IP:{addr}\n"
        # What a certificate under this one may name: the hop and entry's
        # address, nothing else. Hysteria trusts the system's CAs next to
        # this one (SSL_CERT_DIR): without the constraints a leaked key
        # could vouch for any site in that process.
        f"nameConstraints = critical, permitted;DNS:{HOP_NAME}, permitted;IP:{addr}/{mask}\n"
    )


def generate(directory: str, ip: str, *, force: bool = False,
             days: int = VALID_DAYS, openssl: str = 'openssl') -> str:
    """Make the key (EC P-256, 0600) and the self-signed certificate in
    ``directory`` (0700). Refuses to replace a pair unless ``force``:
    exit pins this certificate, and a new one cuts exit off until it is
    pinned again. Returns the certificate's path."""
    addr = str(ipaddress.ip_address((ip or '').strip()))
    cert, key = files(directory)
    if not force and (os.path.lexists(cert) or os.path.lexists(key)):
        raise FileExistsError(
            f"{directory} already holds a certificate (show prints it; "
            f"--force makes a new one, and exit must pin that one)")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    with tempfile.TemporaryDirectory(dir=directory, prefix='.new-') as tmp:
        cnf = os.path.join(tmp, 'req.cnf')
        new_cert, new_key = os.path.join(tmp, CERT_FILE), os.path.join(tmp, KEY_FILE)
        with open(cnf, 'w') as f:
            f.write(_openssl_config(addr))
        r = subprocess.run(
            [openssl, 'req', '-x509', '-new', '-newkey', 'ec',
             '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
             '-days', str(int(days)), '-config', cnf,
             '-keyout', new_key, '-out', new_cert],
            capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"openssl req failed: {r.stderr.strip()[-400:]}")
        os.chmod(new_key, 0o600)
        os.chmod(new_cert, 0o644)
        # The key first: a listener started between the two moves fails to
        # load a mismatched pair and stays off; it never serves a stale one.
        os.replace(new_key, key)
        os.replace(new_cert, cert)
    return cert


def fingerprint(pem: str) -> str:
    """SHA-256 of the certificate as ``AB:CD:…``, the form ``openssl x509
    -noout -fingerprint -sha256`` prints on exit."""
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ':'.join(digest[i:i + 2] for i in range(0, len(digest), 2))


def describe(cert_path: str, openssl: str = 'openssl') -> dict:
    """The public facts exit needs: the PEM, its fingerprint, expiry, names."""
    with open(cert_path) as f:
        pem = f.read()

    def field(*args):
        r = subprocess.run([openssl, 'x509', '-in', cert_path, '-noout', *args],
                           capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ''

    end = field('-enddate')
    san = field('-ext', 'subjectAltName')
    return {
        'pem': pem.strip() + '\n',
        'sha256': fingerprint(pem),
        'not_after': end.split('=', 1)[1] if '=' in end else '',
        'names': ' '.join(san.splitlines()[1:]).strip() if san else '',
    }


# ------------------------------------------------------------ checks

class _NamedHTTPS(http.client.HTTPSConnection):
    """Dial ``host`` but verify ``name`` (and send it as SNI), the way
    exit's Caddy reaches the hop: it dials entry's address with
    ``tls_server_name entry-hop.invalid``."""

    def __init__(self, host, port, name, context, timeout):
        super().__init__(host, port, timeout=timeout, context=context)
        self._tls_name = name
        self._tls_context = context

    def connect(self):
        http.client.HTTPConnection.connect(self)
        self.sock = self._tls_context.wrap_socket(self.sock, server_hostname=self._tls_name)


def _hop_get(host, port, cafile, path, *, name=HOP_NAME, timeout=10.0,
             headers=None):
    """GET ``path`` over the hop: ``(status, body, peer certificate)``."""
    conn = _NamedHTTPS(host, port, name, client_context(cafile), timeout)
    try:
        conn.connect()
        # Read before the response: a "Connection: close" answer drops the socket.
        peer = conn.sock.getpeercert() or {}
        conn.request('GET', path, headers={'User-Agent': VERIFY_UA, **(headers or {})})
        resp = conn.getresponse()
        return resp.status, resp.read(), peer
    finally:
        conn.close()


def _peer_ips(peer: dict) -> set:
    return {v for k, v in peer.get('subjectAltName', ()) if k == 'IP Address'}


def check(config, *, host: str = '127.0.0.1', timeout: float = 10.0) -> dict:
    """``/health`` over the HTTPS listener as exit sees it: the pinned
    certificate is the only trust anchor and ``entry-hop.invalid`` the name
    it must carry; entry's address must be on it too (the reporter dials
    and verifies that). ``state``: off / ok / fail. The deploy smoke."""
    port, directory = settings(config)
    cert, _key = files(directory)
    if not port:
        return {'state': 'off', 'reason': 'WEB_TLS_PORT is 0'}
    if not os.path.isfile(cert):
        return {'state': 'off', 'reason': f'no certificate in {directory}'}
    try:
        status, body, peer = _hop_get(host, port, cert, '/health', timeout=timeout)
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        return {'state': 'fail', 'error': f'{type(e).__name__}: {e}'}
    entry_ip = (getattr(config, 'ENTRY_NODE_IP', '') or '').strip()
    if entry_ip and entry_ip not in _peer_ips(peer):
        return {'state': 'fail',
                'error': f'the certificate does not carry ENTRY_NODE_IP {entry_ip} '
                         f'(it has {sorted(_peer_ips(peer))}); make a new one '
                         f'(init --force) and pin it on exit'}
    try:
        health = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        health = {}
    if status != 200 or not isinstance(health, dict):
        return {'state': 'fail', 'error': f'/health answered HTTP {status}'}
    return {'state': 'ok', 'port': port, 'version': health.get('version', '?'),
            'status': health.get('status', '?')}


def _probe_uuid(db_path: str) -> Optional[str]:
    """The probe account's uuid, read-only (no migration, no write)."""
    conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=10)
    try:
        row = conn.execute("SELECT uuid FROM users WHERE chat_id = ?",
                           (PROBE_CHAT_ID,)).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def _public_get(url: str, timeout: float):
    """GET through the public path: the system's CAs, no proxy (the
    container's HTTPS_PROXY is the Telegram egress, which allows :443 only)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers={'User-Agent': VERIFY_UA})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b''


def verify(config, db_path: str, *, host: str = '127.0.0.1',
           timeout: float = 20.0, public_get=None) -> dict:
    """``/health`` and the probe account's profile (``?format=clash`` and
    the plain ``/sub``) through the hop listener itself, ``WEBAPP_URL`` and
    every ``SUB_MIRROR_URLS`` mirror: the same version everywhere and the
    same bytes, 200 each time. Nothing secret is returned: codes, sizes,
    the first 12 hex of each body's SHA-256."""
    public_get = public_get or _public_get
    port, directory = settings(config)
    cert, _key = files(directory)
    out = {'ok': False, 'paths': [], 'problems': []}
    try:
        uuid = _probe_uuid(db_path)
    except sqlite3.Error as e:
        out['problems'].append(f'bot.db is not readable ({type(e).__name__})')
        return out
    if not uuid:
        out['problems'].append(f'no {PROBE_CHAT_ID} account with a uuid in bot.db')
        return out
    from bot.services.subscription import SubscriptionService
    token = SubscriptionService(config).derive_token(uuid)
    targets = []
    if port and os.path.isfile(cert):
        targets.append(('hop', None))
    else:
        out['problems'].append('the hop listener is off (no certificate or WEB_TLS_PORT 0)')
    base = (getattr(config, 'WEBAPP_URL', '') or '').strip().rstrip('/')
    if base:
        targets.append(('webapp', base))
    for i, m in enumerate(u.strip().rstrip('/') for u in
                          (getattr(config, 'SUB_MIRROR_URLS', '') or '').split(',')):
        if m:
            targets.append((f'mirror-{i + 1}', m))
    queries = (('health', '/health'), ('sub_clash', f'/sub/{token}?format=clash'),
               ('sub', f'/sub/{token}'))
    seen = {}
    for label, base_url in targets:
        row = {'path': label}
        for key, path in queries:
            try:
                if base_url is None:
                    status, body, _peer = _hop_get(host, port, cert, path, timeout=timeout)
                else:
                    status, body = public_get(base_url + path, timeout)
            except Exception as e:  # noqa: BLE001 — a failed path is a reported row
                row[key] = {'error': type(e).__name__}
                out['problems'].append(f'{label} {key}: {type(e).__name__}')
                continue
            cell = {'status': status, 'size': len(body),
                    'sha256': hashlib.sha256(body).hexdigest()[:12]}
            if key == 'health':
                try:
                    cell['version'] = json.loads(body.decode('utf-8')).get('version', '?')
                except (UnicodeDecodeError, ValueError, AttributeError):
                    cell['version'] = '?'
                cell.pop('sha256')
            row[key] = cell
            if status != 200:
                out['problems'].append(f'{label} {key}: HTTP {status}')
            seen.setdefault(key, set()).add(cell.get('version') if key == 'health'
                                            else cell['sha256'])
        out['paths'].append(row)
    for key, values in seen.items():
        if len(values) > 1:
            what = 'versions' if key == 'health' else 'bodies'
            out['problems'].append(f'{key}: the {what} differ between paths')
    out['ok'] = bool(targets) and not out['problems']
    return out
