"""New protocols and fingerprints — IMPROVEMENT_PLAN F, package E.

* E1  the TLS fingerprint is a server setting (``TLS_FINGERPRINTS``), one
      value per user by uuid, used by every builder; unset = chrome.
* E3  VLESS + XHTTP + Reality (``xhr``): Clash / xray / links builders
      (sing-box has no XHTTP — its profile leaves it out), opt-in in the
      cascade, probed through a mihomo sidecar, an exit inbound script.
* E4  SSH as the paid users' emergency path: derived credentials, only in
      the emergency profiles, a hash-only user feed for the exit's sshd.
* E5  ``/probe-big`` — a 204 with ~20 KB of headers as the check address of
      the client testers' groups (an experiment).
* E6  under lockdown ws leads only where the CDN front is seen to pass.

Level 2: real builders, real sqlite; the docker stands at the bottom
(``requires_docker`` + ``requires_network``) run the bot's own XHTTP proxy
and SSH proxy in mihomo 1.19.33 against xray 26.6.22 / the script's sshd.
"""

import crypt  # noqa: F401  (stdlib on 3.11/3.12; checks the hashes below)
import importlib.util
import json
import os
import subprocess
import time
import urllib.request
import uuid as uuidlib
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.core.database import Database
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
from bot.services import lockdown as ld
from bot.services import ssh_exit, tls_fingerprint
from bot.services.health_checker import HealthChecker
from bot.services.subscription import SubscriptionService
from bot.services.vpn import VPNService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning",
    "ignore:'crypt' is deprecated:DeprecationWarning",
)

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
ALL = ('reality', 'hy2', 'ws', 'stls')


def _config(**over):
    cfg = dict(
        BOT_TOKEN='t', WEBAPP_URL='https://dash.example.com',
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='reality-pbk', SNI_VALUE='www.bing.com', SID_VALUE='0123456789abcdef',
        HY2_HOST='203.0.113.20', HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='', HY2T_PORT='',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api', WS_SNI='cdn.example.com',
        WS2_HOST='', STLS_HOST='203.0.113.20', STLS_PORT=443, STLS_SNI='www.microsoft.com',
        STLS_VERSION=3, STLS_PASSWORD='stls-pw', SS_METHOD='2022-blake3-aes-128-gcm',
        SS_SERVER_PASSWORD='srv-pw', SS_USER_SALT='salt', SUB_MIRROR_URLS='',
        FALLBACK_NODE_HOST='', TLS_FINGERPRINTS='',
        XHR_SNI='', XHR_PATH='/xhr', XHR_PUBLIC_KEY='', XHR_SID='', XHR_PORT=0,
        XHR_EXIT_PORT=2055, XHR_INBOUND_ID=0, XHR_MAX_CONNECTIONS=3,
        SSH_EXIT_HOST='', SSH_EXIT_PORT='22022', SSH_HOST_KEY='', SSH_USER_SECRET='',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


XHR = dict(XHR_SNI='dl.google.com')
SSH = dict(SSH_EXIT_HOST='198.51.100.4', SSH_HOST_KEY='ssh-ed25519 AAAAC3Nza',
           SSH_USER_SECRET='s3cret')


def _user(status='paid', uid=UUID, expiry=None):
    return SimpleNamespace(uuid=uid, email='u1@x', status=status, lang='ru', chat_id='1',
                           subscription_expiry=expiry, previous_state=None)


def _clash(protocols=ALL, config=None, **kw):
    svc = SubscriptionService(config or _config())
    return json.loads(svc.build_clash_config(kw.pop('user', None) or _user(), protocols, **kw))


def _singbox(protocols=ALL, config=None, **kw):
    svc = SubscriptionService(config or _config())
    return svc.build_singbox_config(kw.pop('user', None) or _user(), protocols, **kw)


# ------------------------------------------------------------------ E1 -------

class TestFingerprints:

    def test_unset_is_chrome_for_everyone(self):
        assert tls_fingerprint.pool(_config()) == ('chrome',)
        assert tls_fingerprint.for_user(_config(), UUID) == 'chrome'

    def test_the_pool_keeps_known_values_in_order(self):
        cfg = _config(TLS_FINGERPRINTS=' Firefox, chrome, bogus, firefox ,edge')
        assert tls_fingerprint.pool(cfg) == ('firefox', 'chrome', 'edge')
        assert tls_fingerprint.pool(_config(TLS_FINGERPRINTS='bogus')) == ('chrome',)

    def test_stable_per_user_and_spread_over_users(self):
        cfg = _config(TLS_FINGERPRINTS='chrome,firefox,edge,safari')
        picks = [tls_fingerprint.for_user(cfg, str(uuidlib.UUID(int=i))) for i in range(400)]
        assert set(picks) == {'chrome', 'firefox', 'edge', 'safari'}
        assert all(tls_fingerprint.for_user(cfg, str(uuidlib.UUID(int=i))) == picks[i]
                   for i in range(400))

    def test_every_builder_uses_the_users_value(self):
        cfg = _config(TLS_FINGERPRINTS='firefox', **XHR)
        sb = _singbox(ALL + ('xhr',), config=cfg)
        fps = {o['tag']: (o.get('tls') or {}).get('utls', {}).get('fingerprint')
               for o in sb['outbounds'] if (o.get('tls') or {}).get('utls')}
        assert fps and set(fps.values()) == {'firefox'}
        clash = _clash(ALL + ('xhr',), config=cfg)
        assert {p['client-fingerprint'] for p in clash['proxies'] if 'client-fingerprint' in p} \
            == {'firefox'}
        xray = SubscriptionService(cfg).build_xray_config(_user(), ('reality', 'ws', 'xhr'))
        found = [ob['streamSettings'].get('realitySettings', ob['streamSettings'].get('tlsSettings'))
                 ['fingerprint'] for ob in xray['outbounds'] if 'streamSettings' in ob
                 and ob['streamSettings'].get('security') in ('reality', 'tls')]
        assert found and set(found) == {'firefox'}
        vpn = VPNService(SimpleNamespace(**vars(cfg)))
        assert 'fp=firefox' in vpn.generate_vless_link(UUID, 'u1@x')


    def test_the_ru_zone_profiles_too(self):
        cfg = _config(TLS_FINGERPRINTS='edge')
        svc = SubscriptionService(cfg)
        ru_exit = {'port': 8445, 'sni': 'www.google.com', 'pbk': 'ru-pbk', 'sid': 'ab'}
        sb = svc.build_abroad_singbox_config(_user(), ru_exit)
        assert {o['tls']['utls']['fingerprint'] for o in sb['outbounds']
                if (o.get('tls') or {}).get('utls')} == {'edge'}
        clash = json.loads(svc.build_abroad_clash_config(_user(), ru_exit))
        assert {p['client-fingerprint'] for p in clash['proxies']} == {'edge'}


# ------------------------------------------------------------------ E3 -------

class TestXhr:

    def test_off_without_its_sni(self):
        assert SubscriptionService(_config())._build_xhr(UUID, 'u1') is None
        assert VPNService(_config()).generate_xhr_link(UUID, 'u1@x') is None

    def test_clash_proxy(self):
        proxies = {p['name']: p for p in _clash(ALL + ('xhr',), config=_config(**XHR))['proxies']}
        p = proxies['u1-xhr']
        assert (p['type'], p['network'], p['server'], p['port'], p['servername']) == \
            ('vless', 'xhttp', '203.0.113.20', 8443, 'dl.google.com')
        assert p['xhttp-opts'] == {'path': '/xhr', 'mode': 'auto',
                                   'reuse-settings': {'max-connections': '3'}}
        assert p['reality-opts'] == {'public-key': 'reality-pbk', 'short-id': '0123456789abcdef'}
        assert 'flow' not in p

    def test_singbox_leaves_it_out(self):
        sb = _singbox(ALL + ('xhr',), config=_config(**XHR))
        tags = {o['tag'] for o in sb['outbounds']}
        assert 'u1-xhr' not in tags
        assert all('u1-xhr' not in o.get('outbounds', []) for o in sb['outbounds'])
        assert not any((o.get('transport') or {}).get('type') == 'xhttp' for o in sb['outbounds'])

    def test_xray_and_link(self):
        cfg = _config(**XHR, XHR_PUBLIC_KEY='xpbk', XHR_SID='ab12')
        xray = SubscriptionService(cfg).build_xray_config(_user(), ('xhr',))
        ob = next(o for o in xray['outbounds'] if o.get('tag') == 'u1-xhr')
        st = ob['streamSettings']
        assert (st['network'], st['security']) == ('xhttp', 'reality')
        assert st['xhttpSettings']['extra']['xmux']['maxConnections'] == 3
        assert st['realitySettings']['publicKey'] == 'xpbk'
        link = VPNService(cfg).generate_xhr_link(UUID, 'u1@x')
        assert link.startswith(f'vless://{UUID}@203.0.113.20:8443?type=xhttp&security=reality')
        assert 'sni=dl.google.com' in link and 'pbk=xpbk' in link and 'sid=ab12' in link
        assert 'path=%2Fxhr' in link and link.endswith('#u1-xhr')

    def test_opt_in_in_the_cascade(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        names = {c['name']: c['enabled'] for c in MK.get_cascade_config(db)}
        assert names['xhr'] is False and 'xhr' not in MK.get_cascade_order(db)
        db.set_setting('cascade_protocol_order', json.dumps(['reality', 'hy2']))
        assert {c['name']: c['enabled'] for c in MK.get_cascade_config(db)}['xhr'] is False
        db.set_setting('cascade_protocol_order', json.dumps(
            [{'name': 'reality', 'enabled': True}, {'name': 'xhr', 'enabled': True}]))
        assert 'xhr' in MK.get_cascade_order(db, user=_user('paid'))
        assert 'xhr' not in MK.get_cascade_order(db, user=_user('demo'))   # paid tier

    def test_probe_ports_and_sidecars(self):
        hc = HealthChecker(Mock(), _config(**XHR))
        assert hc.probe_ports['xhr'] == 18087
        assert hc._proxy_url('xhr') == 'http://probe-proxy-mihomo:18087'
        assert hc._proxy_url('reality') == 'http://probe-proxy:18081'
        assert 'xhr' not in HealthChecker.probe_ports_for(_config())
        gen = _load('gen_probe_config.py')
        user = SimpleNamespace(uuid=UUID, email='probe_monitor@x', status='paid')
        cfg = gen.build_config(_config(**XHR), user)
        assert 'probe-in-xhr' not in {i['tag'] for i in cfg['inbounds']}
        mih = _load('gen_probe_config_mihomo.py').build_config(_config(**XHR), user)
        assert mih['listeners'] == [{'name': 'probe-in-xhr', 'type': 'http', 'listen': '0.0.0.0',
                                     'port': 18087, 'proxy': 'probe_monitor-xhr'}]
        assert mih['proxies'][0]['network'] == 'xhttp'

    def test_inbound_script_shapes(self):
        setup = _load('setup_xhr_inbound.py')
        source = {
            'streamSettings': json.dumps({'network': 'tcp', 'security': 'reality',
                                          'tcpSettings': {'acceptProxyProtocol': True},
                                          'realitySettings': {'privateKey': 'PRIV', 'xver': 0,
                                                              'shortIds': ['aa', 'bb'],
                                                              'settings': {'publicKey': 'PUB'}}}),
            'settings': json.dumps({'clients': [{'id': UUID, 'email': 'u1@x', 'flow': 'xtls-rprx-vision',
                                                  'totalGB': 0, 'enable': True}]}),
        }
        inbound = setup.build_inbound(source, sni='dl.google.com', port=2055, path='/xhr')
        stream = json.loads(inbound['streamSettings'])
        assert (inbound['port'], inbound['protocol']) == (2055, 'vless')
        assert stream['network'] == 'xhttp' and stream['xhttpSettings'] == {'path': '/xhr', 'mode': 'auto'}
        r = stream['realitySettings']
        assert (r['dest'], r['serverNames'], r['privateKey'], r['shortIds']) == \
            ('dl.google.com:443', ['dl.google.com'], 'PRIV', ['aa'])
        assert stream['sockopt'] == {'acceptProxyProtocol': True}
        assert setup.clients_for(source) == [{'id': UUID, 'email': 'u1@x', 'totalGB': 0,
                                              'enable': True, 'flow': ''}]
        assert 'send-proxy-v2' in setup.haproxy_lines('dl.google.com', '198.51.100.1', 2055, True)
        with pytest.raises(ValueError):
            setup.build_inbound({'streamSettings': '{}'}, sni='x', port=1, path='/')

    def test_provisioning_attaches_new_keys(self):
        from bot.services.xui_service import XUIService
        svc = XUIService.__new__(XUIService)
        svc.config = SimpleNamespace(WS_INBOUND_ID=3, SS_INBOUND_ID=5, WS2_INBOUND_ID=6,
                                     XHR_INBOUND_ID=7)
        assert svc._client_inbound_ids(1) == [1, 3, 5, 6, 7]


def _load(name):
    path = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', name)
    spec = importlib.util.spec_from_file_location(name.replace('.py', '') + '_e', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------------ E4 -------

class TestSshExit:

    def test_derived_credentials(self):
        cfg = _config(**SSH)
        assert ssh_exit.username_for(UUID) == ssh_exit.username_for(UUID)
        assert ssh_exit.username_for(UUID).startswith('n') and len(ssh_exit.username_for(UUID)) == 13
        pw = ssh_exit.password_for(cfg, UUID)
        assert len(pw) == 32 and pw == ssh_exit.password_for(cfg, UUID)
        assert pw != ssh_exit.password_for(_config(**{**SSH, 'SSH_USER_SECRET': 'other'}), UUID)

    def test_only_emergency_profiles_of_payers(self):
        cfg = _config(**SSH)
        assert not any(o['type'] == 'ssh' for o in _singbox(config=cfg)['outbounds'])
        sb = _singbox(config=cfg, lockdown=True)
        ssh = next(o for o in sb['outbounds'] if o['type'] == 'ssh')
        assert ssh['server'] == '198.51.100.4' and ssh['host_key'] == ['ssh-ed25519 AAAAC3Nza']
        assert ssh['tag'] not in next(o for o in sb['outbounds'] if o['tag'] == 'calls')['outbounds']
        clash = _clash(config=cfg, lockdown=True)
        proxy = next(p for p in clash['proxies'] if p['type'] == 'ssh')
        assert proxy['username'] == ssh['user'] and proxy['host-key'] == ['ssh-ed25519 AAAAC3Nza']
        assert not any(p['type'] == 'ssh' for p in _clash(config=cfg)['proxies'])
        demo = _singbox(config=cfg, lockdown=True, user=_user('demo'), protocols=('ws', 'hy2'))
        assert not any(o['type'] == 'ssh' for o in demo['outbounds'])
        lapsed = _singbox(config=cfg, lockdown=True,
                          user=_user('paid', expiry='2000-01-01T00:00:00'))
        assert not any(o['type'] == 'ssh' for o in lapsed['outbounds'])

    def test_off_without_its_settings(self):
        assert ssh_exit.outbound(_config(), _user()) is None

    def test_the_feed_has_hashes_only(self, tmp_path):
        import crypt as _crypt
        db = Database(str(tmp_path / 'bot.db'))
        with db._connect() as conn:
            conn.executemany("INSERT INTO users (chat_id, username, status, uuid, subscription_expiry) "
                             "VALUES (?,?,?,?,?)",
                             [('1', 'a', 'paid', UUID, None), ('2', 'b', 'demo', str(uuidlib.uuid4()), None),
                              ('3', 'c', 'paid', str(uuidlib.uuid4()), '2000-01-01T00:00:00')])
            conn.commit()
        cfg = _config(**SSH)
        users = ssh_exit.users_payload(db, cfg)
        assert [u['user'] for u in users] == [ssh_exit.username_for(UUID)]
        h = users[0]['hash']
        assert h.startswith('$6$') and _crypt.crypt(ssh_exit.password_for(cfg, UUID), h) == h
        assert UUID not in json.dumps(users)

    @pytest.mark.asyncio
    async def test_the_endpoint_gate(self, tmp_path):
        from bot.core.web_server import WebAppServer
        db = Database(str(tmp_path / 'bot.db'))
        srv = WebAppServer(_config(**SSH, DPI_REPORT_TOKEN='tok'), db, xui_service=Mock())
        req = Mock()
        req.headers = {'X-DPI-Token': 'nope'}
        assert (await srv.handle_ssh_users(req)).status == 403
        req.headers = {'X-DPI-Token': 'tok'}
        resp = await srv.handle_ssh_users(req)
        assert resp.status == 200 and json.loads(resp.text) == {'ok': True, 'users': []}
        off = WebAppServer(_config(DPI_REPORT_TOKEN='tok'), db, xui_service=Mock())
        assert (await off.handle_ssh_users(req)).status == 404

    def test_sshd_config_forwards_only(self):
        out = subprocess.run(['bash', os.path.join(os.path.dirname(__file__), '..', '..',
                                                   'scripts', 'setup_ssh_exit.sh'),
                              '--print-config'], capture_output=True, text=True, check=True).stdout
        for line in ('PermitTTY no', 'ForceCommand /bin/false', 'AllowTcpForwarding local',
                     'PermitRootLogin no', 'AllowGroups vpn', 'X11Forwarding no'):
            assert line in out.splitlines()


# ------------------------------------------------------------------ E5 -------

class TestBigCheck:

    @pytest.mark.asyncio
    async def test_headers_past_sixteen_kilobytes(self, tmp_path):
        from bot.core.web_server import WebAppServer
        srv = WebAppServer(_config(), Database(str(tmp_path / 'bot.db')), xui_service=Mock())
        resp = await srv.handle_probe_big(Mock())
        size = sum(len(k) + len(v) + 4 for k, v in resp.headers.items())
        assert resp.status == 204 and size > 16 * 1024

    def test_testers_only_when_switched_on(self, tmp_path):
        from bot.core.web_server import WebAppServer
        db = Database(str(tmp_path / 'bot.db'))
        srv = WebAppServer(_config(), db, xui_service=Mock())
        tester = SimpleNamespace(chat_id='1')
        db.set_setting('client_testers', json.dumps(['1']))
        assert srv._big_check_for(tester) is False
        db.set_setting('probe_big_testers', '1')
        assert srv._big_check_for(tester) is True
        assert srv._big_check_for(SimpleNamespace(chat_id='2')) is False

    def test_the_groups_check_it(self):
        groups = {g['name']: g for g in _clash(big_check=True)['proxy-groups']}
        for name in ('Cascade', 'Auto', 'Calls'):
            assert groups[name]['url'] == 'https://dash.example.com/probe-big'
        plain = {g['name']: g for g in _clash()['proxy-groups']}
        assert plain['Cascade']['url'] == 'https://www.gstatic.com/generate_204'


# ------------------------------------------------------------------ E6 -------

class TestLockdownFront:

    def _db(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        db.set_setting('cascade_protocol_order', json.dumps(['reality', 'hy2', 'ws', 'stls']))
        db.set_setting('lockdown_mode', json.dumps({'mode': 'on', 'active': True, 'by': 'admin:1'}))
        with db._connect() as conn:
            conn.execute("INSERT INTO users (chat_id, username, status, last_asn) "
                         "VALUES ('9', 'x', 'paid', 'AS31133')")
            conn.commit()
        return db

    def test_no_evidence_keeps_tcp_direct_first(self, tmp_path):
        db = self._db(tmp_path)
        assert MK.get_cascade_order(db, asn='AS31133')[:3] == ('stls', 'reality', 'ws')

    def test_a_client_through_ws_puts_it_first(self, tmp_path):
        db = self._db(tmp_path)
        with db._connect() as conn:
            conn.execute("INSERT INTO client_probe (chat_id, grp) VALUES ('9', 'p-ws')")
            conn.commit()
        assert ld.fronted_passes(db, 'AS31133') is True
        assert MK.get_cascade_order(db, asn='AS31133')[0] == 'ws'
        assert MK.get_cascade_order(db, asn='AS12389')[0] == 'stls'

    def test_users_on_cf_ws_count_too(self, tmp_path):
        db = self._db(tmp_path)
        with db._connect() as conn:
            conn.execute("INSERT INTO protocol_usage VALUES (?, '9', 'cf-ws', 4)",
                         (datetime.utcnow().strftime('%Y-%m-%d'),))
            conn.commit()
        assert MK.get_cascade_order(db, asn='AS31133')[0] == 'ws'

    def test_projection(self):
        assert ld.apply_lockdown_order(['reality', 'hy2', 'ws', 'stls'], fronted_ok=False) == \
            ['stls', 'reality', 'ws', 'hy2']
        assert ld.apply_lockdown_order(['reality', 'hy2', 'ws', 'stls']) == \
            ['ws', 'stls', 'reality', 'hy2']


# ------------------------------------------------------------------ stands ---

XRAY = 'ghcr.io/xtls/xray-core:26.6.22'
MIHOMO = 'metacubex/mihomo:v1.19.33'


def _have(image):
    return subprocess.run(['docker', 'image', 'inspect', image], capture_output=True).returncode == 0


def _docker(*a, check=True):
    r = subprocess.run(['docker', *a], capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(r.stderr)
    return r.stdout.strip()


def _api(port, path):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=20) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError:
        return None


@pytest.mark.requires_docker
@pytest.mark.requires_network
def test_stand_xhr_in_mihomo_against_xray(tmp_path):
    """The bot's own xhr proxy against the inbound setup_xhr_inbound.py would
    create, on the exit's xray (26.6.22): a delay test passes, an unknown
    uuid does not."""
    if not (_have(XRAY) and _have(MIHOMO)):
        pytest.skip('stand images not pulled')
    keys = dict(l.split(':', 1) for l in _docker('run', '--rm', XRAY, 'x25519').splitlines() if ':' in l)
    priv = keys['PrivateKey'].strip()
    pub = next(v for k, v in keys.items() if k.startswith('Password')).strip()
    setup = _load('setup_xhr_inbound.py')
    source = {'streamSettings': json.dumps({'realitySettings': {'privateKey': priv,
                                                                 'shortIds': ['0123456789abcdef']}}),
              'settings': json.dumps({'clients': [{'id': UUID, 'email': 'u1@x'}]})}
    inbound = setup.build_inbound(source, sni='www.google.com', port=443, path='/xhr')
    stream = json.loads(inbound['streamSettings'])
    (tmp_path / 'x').mkdir()
    (tmp_path / 'm').mkdir()
    (tmp_path / 'x' / 'config.json').write_text(json.dumps({
        'inbounds': [{'port': 443, 'protocol': 'vless', 'settings': {
            'clients': [{'id': c['id']} for c in setup.clients_for(source)], 'decryption': 'none'},
            'streamSettings': stream}],
        'outbounds': [{'protocol': 'freedom'}]}))
    cfg = _config(ENTRY_NODE_IP='e-xray', XHR_SNI='www.google.com', XHR_PORT=443,
                  REALITY_PUBLIC_KEY=pub, SID_VALUE='0123456789abcdef')
    clash = _clash(('xhr',), config=cfg)
    good = next(p for p in clash['proxies'] if p['name'] == 'u1-xhr')
    bad = dict(good, name='u1-xhr-unknown', uuid=str(uuidlib.uuid4()))
    (tmp_path / 'm' / 'config.yaml').write_text(json.dumps({
        'mode': 'rule', 'external-controller': '0.0.0.0:9090', 'proxies': [good, bad],
        'rules': ['MATCH,DIRECT']}))
    net = 'nekovpn-xhr-stand'
    _docker('network', 'create', net, check=False)
    try:
        _docker('run', '-d', '--name', 'e-xray', '--network', net, '-v',
                f'{tmp_path}/x:/etc/xray', XRAY, 'run', '-c', '/etc/xray/config.json')
        _docker('run', '-d', '--name', 'e-mihomo', '--network', net, '-p', '127.0.0.1:19290:9090',
                '-v', f'{tmp_path}/m:/root/.config/mihomo', MIHOMO, '-d', '/root/.config/mihomo')
        time.sleep(4)
        q = '?url=https://www.gstatic.com/generate_204&timeout=10000'
        assert _api(19290, f'/proxies/u1-xhr/delay{q}') == 200
        assert _api(19290, f'/proxies/u1-xhr-unknown/delay{q}') != 200
    finally:
        for c in ('e-xray', 'e-mihomo'):
            _docker('rm', '-f', c, check=False)
        _docker('network', 'rm', net, check=False)


@pytest.mark.requires_docker
@pytest.mark.requires_network
def test_stand_ssh_in_mihomo_against_the_scripts_sshd(tmp_path):
    """The script's sshd_config in an alpine sshd, the bot's hashed user,
    the bot's own ssh proxy in mihomo: it forwards; a wrong password does
    not get in."""
    if not _have(MIHOMO):
        pytest.skip('mihomo image not pulled')
    script = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'setup_ssh_exit.sh')
    sshd = subprocess.run(['bash', script, '--print-config'], capture_output=True, text=True,
                          check=True).stdout
    (tmp_path / 'sshd_config').write_text(sshd)
    (tmp_path / 'keys').mkdir()
    image = 'nekovpn-ssh-stand:local'
    build = subprocess.run(['docker', 'build', '-q', '-t', image, '-'], input=(
        'FROM alpine:3.20\nRUN apk add --no-cache openssh-server shadow && addgroup vpn\n'
        'CMD ["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd_config"]\n'),
        capture_output=True, text=True)
    if build.returncode:
        pytest.skip(f'could not build the sshd image: {build.stderr[-200:]}')
    _docker('run', '--rm', '-v', f'{tmp_path}/keys:/k', image, 'ssh-keygen', '-q', '-t', 'ed25519',
            '-N', '', '-f', '/k/ssh_host_ed25519_key')
    host_key = ' '.join((tmp_path / 'keys' / 'ssh_host_ed25519_key.pub').read_text().split()[:2])
    cfg = _config(SSH_EXIT_HOST='e-sshd', SSH_EXIT_PORT='22', SSH_HOST_KEY=host_key,
                  SSH_USER_SECRET='stand-secret')
    name = ssh_exit.username_for(UUID)
    hashed = ssh_exit._crypt_sha512(ssh_exit.password_for(cfg, UUID))
    net = 'nekovpn-ssh-stand'
    _docker('network', 'create', net, check=False)
    try:
        _docker('run', '-d', '--name', 'e-sshd', '--network', net,
                '-v', f'{tmp_path}/sshd_config:/etc/ssh/sshd_config:ro',
                '-v', f'{tmp_path}/keys:/etc/ssh/keys:ro', image)
        _docker('exec', 'e-sshd', 'adduser', '-D', '-H', '-s', '/bin/false', '-G', 'vpn', name)
        subprocess.run(['docker', 'exec', '-i', 'e-sshd', 'chpasswd', '-e'],
                       input=f'{name}:{hashed}\n', text=True, check=True)
        proxy = next(p for p in _clash(config=cfg, lockdown=True)['proxies'] if p['type'] == 'ssh')
        wrong = dict(proxy, name='u1-ssh-wrong', password='x' * 32)
        (tmp_path / 'm').mkdir()
        (tmp_path / 'm' / 'config.yaml').write_text(json.dumps({
            'mode': 'rule', 'external-controller': '0.0.0.0:9090', 'proxies': [proxy, wrong],
            'rules': ['MATCH,DIRECT']}))
        _docker('run', '-d', '--name', 'e-mihomo-ssh', '--network', net, '-p', '127.0.0.1:19390:9090',
                '-v', f'{tmp_path}/m:/root/.config/mihomo', MIHOMO, '-d', '/root/.config/mihomo')
        time.sleep(4)
        q = '?url=https://www.gstatic.com/generate_204&timeout=10000'
        assert _api(19390, f"/proxies/{proxy['name']}/delay{q}") == 200
        assert _api(19390, f'/proxies/u1-ssh-wrong/delay{q}') != 200
    finally:
        for c in ('e-sshd', 'e-mihomo-ssh'):
            _docker('rm', '-f', c, check=False)
        _docker('network', 'rm', net, check=False)
