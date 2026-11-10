"""Stand for the calls group (IMPROVEMENT_PLAN F, A1): the bot's own Clash and
sing-box profiles, loaded into mihomo and sing-box 1.11.15, against the exit's
xray (26.6.22, VLESS-Reality) and a hysteria2 server.

What it pins:
  * a Reality client the inbound does not know FAILS the url test (xray
    answers "invalid request user id") — the fear that kept hy2 alone in the
    calls group until 2026-11 does not hold;
  * Calls / calls pick hy2 while it works and Reality once hy2 is gone.

Needs docker and the internet (Reality dest www.google.com, test url
gstatic): ``requires_docker`` + ``requires_network``, deselected in CI. The
groups' check interval is cut to 10 s here so the run takes ~1 min; the
profiles keep 180 s / 3 min. Each client gets one change the stand needs and
the profile cannot carry: the hysteria2 certificate is self-signed.
"""

import copy
import json
import subprocess
import time
import urllib.error
import urllib.request
import uuid as uuidlib
from types import SimpleNamespace

import pytest

from bot.services.subscription import SubscriptionService

pytestmark = [pytest.mark.requires_docker, pytest.mark.requires_network]

XRAY = 'ghcr.io/xtls/xray-core:26.6.22'          # the exit's version, 2026-11-10
SINGBOX = 'ghcr.io/sagernet/sing-box:v1.11.15'
MIHOMO = 'metacubex/mihomo:v1.19.33'             # the NekoVPN core's base
NET = 'nekovpn-calls-stand'
DEST = 'www.google.com'
SID = '0123456789abcdef'
TEST_URL = 'https://www.gstatic.com/generate_204'
GOOD = str(uuidlib.uuid4())
UNKNOWN = str(uuidlib.uuid4())
OBFS = 'stand-obfs'
CONTAINERS = ('stand-reality', 'stand-hy2', 'stand-mihomo', 'stand-singbox')


def _docker(*args, check=True):
    r = subprocess.run(['docker', *args], capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(f'docker {args[:3]}: {r.stderr.strip()}')
    return r.stdout.strip()


def _have(image):
    return subprocess.run(['docker', 'image', 'inspect', image],
                          capture_output=True).returncode == 0


def _api(port, path):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=20) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, {}
    except OSError:
        return None, {}


def _config(pub):
    return SimpleNamespace(
        BOT_TOKEN='t', WEBAPP_URL='',
        ENTRY_NODE_IP='stand-reality', ENTRY_NODE_PORT=443,
        REALITY_PUBLIC_KEY=pub, SNI_VALUE=DEST, SID_VALUE=SID,
        HY2_HOST='stand-hy2', HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD=OBFS, HY2_HOP_PORTS='', HY2T_PORT='',
        WS_HOST='', STLS_HOST='', SUB_MIRROR_URLS='',
    )


def _user(uid):
    return SimpleNamespace(uuid=uid, email='u1@x', status='paid', lang='ru', chat_id='1')


def _strip_fragment(node):
    if isinstance(node, dict):
        node.pop('fragment', None)
        node.pop('fragment_fallback_delay', None)
        for v in node.values():
            _strip_fragment(v)
    elif isinstance(node, list):
        for v in node:
            _strip_fragment(v)


@pytest.fixture(scope='module')
def stand(tmp_path_factory):
    try:
        _docker('version')
    except (AssertionError, FileNotFoundError):
        pytest.skip('docker is not available')
    for image in (XRAY, SINGBOX, MIHOMO):
        if not _have(image):
            pytest.skip(f'{image} not pulled')
    work = tmp_path_factory.mktemp('calls-stand')
    keys = dict(l.split(':', 1) for l in _docker('run', '--rm', XRAY, 'x25519').splitlines()
                if ':' in l)
    priv = keys['PrivateKey'].strip()
    pub = next(v for k, v in keys.items() if k.startswith('Password')).strip()
    for d in ('xray', 'hy2', 'mihomo', 'singbox'):
        (work / d).mkdir()
    (work / 'xray' / 'config.json').write_text(json.dumps({
        'log': {'loglevel': 'warning'},
        'inbounds': [{
            'port': 443, 'protocol': 'vless',
            'settings': {'clients': [{'id': GOOD, 'flow': 'xtls-rprx-vision'}],
                         'decryption': 'none'},
            'streamSettings': {'network': 'tcp', 'security': 'reality', 'realitySettings': {
                'dest': f'{DEST}:443', 'serverNames': [DEST], 'privateKey': priv,
                'shortIds': [SID]}},
        }],
        'outbounds': [{'protocol': 'freedom'}],
    }))
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                    'ec_paramgen_curve:prime256v1', '-nodes', '-days', '2',
                    '-keyout', str(work / 'hy2' / 'key.pem'),
                    '-out', str(work / 'hy2' / 'cert.pem'), '-subj', '/CN=hy2.example.com'],
                   check=True, capture_output=True)
    (work / 'hy2' / 'key.pem').chmod(0o644)
    (work / 'hy2' / 'config.json').write_text(json.dumps({
        'inbounds': [{'type': 'hysteria2', 'listen': '::', 'listen_port': 8400,
                      'obfs': {'type': 'salamander', 'password': OBFS},
                      'users': [{'password': GOOD}],
                      'tls': {'enabled': True, 'alpn': ['h3'],
                              'certificate_path': '/w/cert.pem', 'key_path': '/w/key.pem'}}],
        'outbounds': [{'type': 'direct'}],
    }))

    svc = SubscriptionService(_config(pub))
    protocols = ('reality', 'hy2')
    clash = json.loads(svc.build_clash_config(_user(GOOD), protocols))
    unknown = json.loads(svc.build_clash_config(_user(UNKNOWN), protocols))
    stray = copy.deepcopy(next(p for p in unknown['proxies'] if p['name'].endswith('-reality')))
    stray['name'] = 'u1-reality-unknown'
    clash['proxies'].append(stray)
    for p in clash['proxies']:
        if p['type'] == 'hysteria2':
            p['skip-cert-verify'] = True          # self-signed on the stand
    for g in clash['proxy-groups']:
        g['interval'] = 10
    clash.update({'rules': ['MATCH,Calls'], 'external-controller': '0.0.0.0:9090'})
    clash.pop('dns', None)
    (work / 'mihomo' / 'config.yaml').write_text(json.dumps(clash))

    singbox = svc.build_singbox_config(_user(GOOD), protocols)
    _strip_fragment(singbox)                      # 1.11 predates tls.fragment
    for ob in singbox['outbounds']:
        if ob.get('type') == 'hysteria2':
            ob['tls']['insecure'] = True
        if ob.get('tag') == 'calls':
            ob['interval'] = '10s'
    singbox['route'] = {'final': 'calls'}
    singbox.pop('dns', None)
    singbox['outbounds'] = [o for o in singbox['outbounds'] if o['type'] != 'dns']
    singbox['experimental'] = {'clash_api': {'external_controller': '0.0.0.0:9090'}}
    (work / 'singbox' / 'config.json').write_text(json.dumps(singbox))

    _docker('network', 'create', NET, check=False)
    for c in CONTAINERS:
        _docker('rm', '-f', c, check=False)
    try:
        _docker('run', '-d', '--name', 'stand-reality', '--network', NET,
                '-v', f'{work}/xray:/etc/xray', XRAY, 'run', '-c', '/etc/xray/config.json')
        _docker('run', '-d', '--name', 'stand-hy2', '--network', NET,
                '-v', f'{work}/hy2:/w', SINGBOX, 'run', '-c', '/w/config.json')
        _docker('run', '-d', '--name', 'stand-mihomo', '--network', NET,
                '-p', '127.0.0.1:19090:9090', '-v', f'{work}/mihomo:/root/.config/mihomo',
                MIHOMO, '-d', '/root/.config/mihomo')
        _docker('run', '-d', '--name', 'stand-singbox', '--network', NET,
                '-p', '127.0.0.1:19091:9090', '-v', f'{work}/singbox:/w',
                SINGBOX, 'run', '-c', '/w/config.json')
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if _api(19090, '/version')[0] == 200 and _api(19091, '/version')[0] == 200:
                break
            time.sleep(0.5)
        else:
            pytest.fail('stand clients did not come up: '
                        + _docker('logs', 'stand-singbox', check=False))
        yield SimpleNamespace(mihomo=19090, singbox=19091)
    finally:
        for c in CONTAINERS:
            _docker('rm', '-f', c, check=False)
        _docker('network', 'rm', NET, check=False)


Q = f'?url={TEST_URL}&timeout=8000'


def _now(port, group):
    _api(port, f'/group/{group}/delay{Q}')        # the group's periodic check
    return _api(port, f'/proxies/{group}')[1].get('now')


def test_a_key_the_inbound_does_not_know_fails_the_check(stand):
    assert _api(stand.mihomo, f'/proxies/u1-reality/delay{Q}')[0] == 200
    assert _api(stand.mihomo, f'/proxies/u1-reality-unknown/delay{Q}')[0] != 200


def test_calls_pick_hy2_then_reality(stand):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and _now(stand.singbox, 'calls') != 'u1-hy2':
        time.sleep(1)
    assert _now(stand.mihomo, 'Calls') == 'u1-hy2'
    assert _now(stand.singbox, 'calls') == 'u1-hy2'
    _docker('stop', 'stand-hy2')
    try:
        time.sleep(12)                            # past the 10-s check interval
        assert _now(stand.mihomo, 'Calls') == 'u1-reality'
        assert _now(stand.singbox, 'calls') == 'u1-reality'
    finally:
        _docker('start', 'stand-hy2')
