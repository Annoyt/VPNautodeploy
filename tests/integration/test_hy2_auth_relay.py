"""The hy2 auth relay against the real bot (IMPROVEMENT_PLAN F, A6; AGENTS.md
§56): the bot's key list on a real sqlite, and scripts/hy2_auth_relay.py in
front of the real WebAppServer over its TLS listener (the pinned hop, §53).

What is pinned:
  * ``GET /api/hy2/keys`` lists, per route, exactly the keys the live
    callback lets in, with the id it answers — one population of users run
    through both (statuses, expiry, tier, quota readings fresh / stale /
    absent, a user without an email, a uuid two rows share); hashes, never a
    key; the exit's token; a read failure is a 503, never an empty list; the
    list starts no panel lookup;
  * the relay passes the bot's answers on (the bot's own audit rows show the
    request arrived), and with the bot stopped lets in what the bot let in
    or listed — on that route only — and nothing else; a ban reaches the
    relay through the next list or through the bot's next deny, whichever
    comes first; an old bot without the list leaves the relay with the live
    allows only.
"""

import asyncio
import http.client
import importlib.util
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp.test_utils import make_mocked_request

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import hop_tls, hy2_keys

from .hop_tls_support import PROBE_UUID, UUID, Server

REPO = Path(__file__).resolve().parents[2]
TOKEN = 'tok-a6'
DEMO_UUID = '3b9d0e1f-2a3b-4c5d-8e7f-6a5b4c3d2e10'
STRANGER = '00000000-0000-4000-8000-000000000000'


def _load_relay():
    spec = importlib.util.spec_from_file_location('hy2_auth_relay_it',
                                                  REPO / 'scripts' / 'hy2_auth_relay.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load_relay()


# --------------------------------------------------------- the bot's key list --

def _config():
    return SimpleNamespace(BOT_TOKEN='test_token', ENTRY_NODE_IP='203.0.113.20',
                           EXIT_NODE_IP='', DPI_REPORT_TOKEN=TOKEN,
                           is_admin=Mock(return_value=False))


def _srv(tmp_path, users):
    db = Database(str(tmp_path / 'bot.db'))
    for i, (status, uuid, email, expiry) in enumerate(users):
        db._users.save(User(chat_id=str(100 + i), username=f'u{i}', status=status, uuid=uuid,
                            email=email, quota_gb=10.0, lang='ru',
                            subscription_expiry=expiry))
    srv = WebAppServer(_config(), db, xui_service=Mock())
    srv.xui.get_client_traffic = AsyncMock(return_value=None)
    return srv, db


async def _keys(srv, token=TOKEN):
    headers = {} if token is None else {'X-DPI-Token': token}
    resp = await srv.handle_hy2_keys(make_mocked_request('GET', '/api/hy2/keys', headers=headers))
    return resp.status, json.loads(resp.text), resp


async def _live(srv, uuid, turbo):
    req = Mock()
    req.json = AsyncMock(return_value={'auth': uuid, 'addr': '130.49.146.10:5555'})
    handler = srv.handle_hy2t_auth if turbo else srv.handle_hy2_auth
    return json.loads((await handler(req)).text)


async def _drain(srv):
    while srv._bg_tasks:
        await asyncio.gather(*list(srv._bg_tasks), return_exceptions=True)


def _u(n):
    return f'{n:08x}-1111-4222-8333-444455556666'


# (status, uuid, email, expiry), and the panel reading (age_s, reading) the
# bot holds for the email, if any
OVER = {'upload': 60, 'download': 41, 'total': 100, 'enable': True}
UNDER = {'upload': 1, 'download': 2, 'total': 100, 'enable': True}
POPULATION = [
    (('paid', _u(1), 'p1@x', None), None),
    (('paid', _u(2), 'p2@x', '2099-01-01T00:00:00'), None),
    (('paid', _u(3), 'p3@x', '2000-01-01T00:00:00'), None),                 # lapsed
    (('paid', _u(4), 'p4@x', 'not a date'), None),                          # no limit
    (('paid', _u(5), 'p5@x', '2000-01-01T00:00:00+00:00'), None),           # aware: as live
    (('support_topic', _u(6), 's6@x', None), None),
    (('demo', _u(7), 'd7@x', None), None),
    (('demo', _u(8), 'd8@x', '2000-01-01T00:00:00'), None),
    (('demo', _u(9), 'd9@x', None), (0, {'upload': 5, 'download': 5, 'total': 0,
                                         'enable': True})),             # unlimited
    (('new', _u(10), 'n10@x', None), None),
    (('pending_demo', _u(11), 'n11@x', None), None),
    (('rejected', _u(12), 'r12@x', None), None),
    (('banned', _u(13), 'b13@x', None), None),
    (('platform_select', _u(14), 'x14@x', None), None),
    (('paid', _u(15), 'q15@x', None), (0, OVER)),                            # quota spent
    (('paid', _u(16), 'q16@x', None), (0, dict(UNDER, enable=False))),       # switched off
    (('paid', _u(17), 'q17@x', None), (WebAppServer.HY2_QUOTA_STALE_MAX_S + 5, OVER)),
    (('demo', _u(18), 'q18@x', None), (WebAppServer.HY2_QUOTA_FRESH_S + 60, OVER)),
    (('paid', _u(19), 'q19@x', None), (0, UNDER)),
    (('paid', _u(20), None, None), None),                                    # no email: chat_id
    (('paid', _u(21), 'q21@x', None), (0, {'up': 70, 'down': 40, 'total': 100})),
]


class TestKeyList:

    @pytest.mark.asyncio
    async def test_the_list_is_what_the_live_callback_lets_in(self, tmp_path):
        srv, _db = _srv(tmp_path, [u for u, _r in POPULATION])
        now = time.monotonic()
        for (status, uuid, email, expiry), reading in POPULATION:
            if reading is not None:
                srv._hy2_quota_cache[email] = (now - reading[0], reading[1])
        status, keys, _resp = await _keys(srv)
        assert status == 200 and keys['ok'] is True
        # the list asked the panel nothing
        srv.xui.get_client_traffic.assert_not_awaited()
        assert srv._hy2_quota_refreshing == set()
        for route, turbo in (('hy2', False), ('hy2t', True)):
            live = {}
            for (_status, uuid, _email, _expiry), _reading in POPULATION:
                answer = await _live(srv, uuid, turbo)
                if answer['ok']:
                    live[hy2_keys.key_hash(uuid)] = answer['id']
            assert keys[route] == live, route
        await _drain(srv)
        # and the population means something: each gate let some in, kept some out
        names = {hy2_keys.key_hash(u[1]): u for u, _r in POPULATION}
        assert sorted(names[h][2] or 'no-email' for h in keys['hy2']) == [
            'd7@x', 'd9@x', 'no-email', 'p1@x', 'p2@x', 'p4@x', 'p5@x', 'q17@x', 'q19@x',
            's6@x']
        assert sorted(names[h][2] or 'no-email' for h in keys['hy2t']) == [
            'no-email', 'p1@x', 'p2@x', 'p4@x', 'p5@x', 'q17@x', 'q19@x', 's6@x']
        assert keys['hy2'][hy2_keys.key_hash(_u(20))] == '119'

    @pytest.mark.asyncio
    async def test_hashes_never_keys(self, tmp_path):
        srv, _db = _srv(tmp_path, [('paid', UUID, 'u@x', None)])
        status, keys, resp = await _keys(srv)
        assert UUID not in resp.text
        assert keys['hy2'] == {hy2_keys.key_hash(UUID): 'u@x'}
        assert resp.headers['Cache-Control'] == 'no-store'

    @pytest.mark.asyncio
    async def test_a_uuid_two_rows_share_is_left_to_the_live_callback(self, tmp_path):
        srv, _db = _srv(tmp_path, [('paid', UUID, 'a@x', None), ('banned', UUID, 'b@x', None),
                                   ('paid', PROBE_UUID, 'c@x', None), ('paid', '', 'e@x', None)])
        _status, keys, _resp = await _keys(srv)
        assert keys['hy2'] == {hy2_keys.key_hash(PROBE_UUID): 'c@x'}
        assert keys['hy2t'] == {hy2_keys.key_hash(PROBE_UUID): 'c@x'}

    @pytest.mark.asyncio
    @pytest.mark.parametrize('token', [None, '', 'wrong', TOKEN + 'x', 'тест'])
    async def test_the_exits_token_only(self, tmp_path, token):
        srv, _db = _srv(tmp_path, [('paid', UUID, 'u@x', None)])
        status, body, _resp = await _keys(srv, token)
        assert (status, body) == (403, {'ok': False})

    @pytest.mark.asyncio
    async def test_no_token_configured_opens_nothing(self, tmp_path):
        srv, _db = _srv(tmp_path, [('paid', UUID, 'u@x', None)])
        srv.config.DPI_REPORT_TOKEN = ''
        assert (await _keys(srv, ''))[0] == 403

    @pytest.mark.asyncio
    async def test_a_read_that_fails_is_a_503_never_an_empty_list(self, tmp_path, monkeypatch):
        srv, _db = _srv(tmp_path, [('paid', UUID, 'u@x', None)])

        def boom(_db):
            raise RuntimeError('database is locked')
        monkeypatch.setattr(hy2_keys, 'read_rows', boom)
        assert (await _keys(srv))[:2] == (503, {'ok': False})

    def test_the_route_is_registered(self, tmp_path):
        srv, _db = _srv(tmp_path, [])
        routes = {(r.method, r.resource.canonical) for r in srv.app.router.routes()}
        assert ('GET', '/api/hy2/keys') in routes


# ------------------------------------------------- the relay in front of the bot --

@pytest.fixture
def stand(tmp_path):
    """The real bot (TLS listener with a certificate for 127.0.0.1) and the
    relay in front of it, as hysteria sees it."""
    d = tmp_path / 'hop'
    hop_tls.generate(str(d), '127.0.0.1')
    bot = Server(tmp_path, tls_dir=str(d), DPI_REPORT_TOKEN=TOKEN)
    bot.db._users.save(User(chat_id='77', username='demo77', status='demo', uuid=DEMO_UUID,
                            email='d77@x', quota_gb=10.0, lang='ru'))
    up = relay.Upstream(f'https://127.0.0.1:{bot.tls_port}', str(d / 'cert.pem'))
    r = relay.Relay(up, relay.Memory(relay.MEMORY_S), TOKEN)
    srv = relay.Server(('127.0.0.1', 0), r)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    st = SimpleNamespace(bot=bot, relay=r, port=srv.server_address[1], up=True)

    def stop_bot():
        if st.up:
            st.up = False
            bot.close()
    st.stop_bot = stop_bot

    def auth(uuid, turbo=False):
        conn = http.client.HTTPConnection('127.0.0.1', st.port, timeout=15)
        path = '/api/hy2t/auth' if turbo else '/api/hy2/auth'
        conn.request('POST', path, body=json.dumps(
            {'addr': '130.49.146.10:41000', 'auth': uuid, 'tx': 0}).encode(),
            headers={'Content-Type': 'application/json'})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        conn.close()
        assert resp.status == 200
        return data
    st.auth = auth

    def audit():
        asyncio.run_coroutine_threadsafe(_drain(bot.srv), bot.loop).result(10)
        bot.srv._hy2_record_pool.submit(lambda: None).result(10)
        with bot.db._connect() as conn:
            return [tuple(x) for x in conn.execute(
                'SELECT chat_id, decision FROM hy2_auth_log ORDER BY id').fetchall()]
    st.audit = audit
    yield st
    srv.shutdown()
    srv.server_close()
    stop_bot()


def _ban(db, chat_id):
    with db._connect() as conn:
        conn.execute("UPDATE users SET status = 'banned' WHERE chat_id = ?", (chat_id,))
        conn.commit()


ALLOW = {'ok': True, 'id': 'u42@x'}
DENY = {'ok': False}


class TestRelayAndBot:

    def test_the_bots_answers_go_back_and_reach_its_audit(self, stand):
        assert stand.auth(UUID) == ALLOW
        assert stand.auth(UUID, turbo=True) == ALLOW
        assert stand.auth(DEMO_UUID, turbo=True) == DENY
        assert stand.auth(STRANGER) == DENY
        assert stand.audit() == [('42', 'allow'), ('42', 'allow'), ('77', 'deny'),
                                 (None, 'deny')]
        assert stand.relay.served == {'bot_allow': 2, 'bot_deny': 2, 'memory_allow': 0,
                                      'memory_deny': 0}

    def test_bot_stopped_the_list_and_the_allows_decide(self, stand):
        assert stand.relay.refresh_keys() is True
        assert stand.relay.memory.list_size == {'hy2': 3, 'hy2t': 2}
        stand.stop_bot()
        assert stand.auth(UUID) == ALLOW                   # listed, never connected live
        assert stand.auth(UUID, turbo=True) == ALLOW
        assert stand.auth(PROBE_UUID) == {'ok': True, 'id': 'probe_monitor@x'}
        assert stand.auth(DEMO_UUID) == {'ok': True, 'id': 'd77@x'}
        assert stand.auth(DEMO_UUID, turbo=True) == DENY   # not on turbo, from memory either
        assert stand.auth(STRANGER) == DENY
        assert stand.relay.served['memory_allow'] == 4
        assert stand.relay.served['memory_deny'] == 2

    def test_a_ban_reaches_the_relay_with_the_next_list(self, stand):
        assert stand.relay.refresh_keys() is True
        _ban(stand.bot.db, '42')
        assert stand.relay.refresh_keys() is True
        stand.stop_bot()
        assert stand.auth(UUID) == DENY
        assert stand.auth(UUID, turbo=True) == DENY
        assert stand.auth(PROBE_UUID)['ok'] is True

    def test_a_ban_reaches_the_relay_with_the_bots_next_deny(self, stand):
        assert stand.relay.refresh_keys() is True
        _ban(stand.bot.db, '42')
        assert stand.auth(UUID) == DENY                    # the bot says no
        stand.stop_bot()
        assert stand.auth(UUID) == DENY                    # the list still had it
        assert stand.auth(UUID, turbo=True) == DENY        # a deny on hy2 covers hy2t

    def test_an_old_bot_without_the_list_leaves_the_live_allows(self, stand, monkeypatch):
        monkeypatch.setattr(relay, 'KEYS_PATH', '/api/hy2/no-such-list')
        assert stand.relay.refresh_keys() is None          # 404: retried at the normal pace
        assert stand.relay.list_error == 'HTTP 404'
        assert stand.auth(UUID) == ALLOW
        stand.stop_bot()
        assert stand.auth(UUID) == ALLOW                   # allowed live a moment ago
        assert stand.auth(PROBE_UUID) == DENY              # never seen
        assert stand.auth(UUID, turbo=True) == DENY        # never let in there

    def test_a_wrong_token_gets_no_list(self, stand):
        stand.relay.token = 'not-the-token'
        assert stand.relay.refresh_keys() is None
        assert stand.relay.list_error == 'HTTP 403'
        assert stand.relay.memory.list_at is None

    def test_a_list_that_fails_keeps_the_last_one(self, stand):
        assert stand.relay.refresh_keys() is True
        first = stand.relay.memory.list_at
        stand.stop_bot()
        assert stand.relay.refresh_keys() is False         # retried soon
        assert stand.relay.memory.list_at == first
        assert stand.auth(UUID) == ALLOW

    def test_the_list_counts_from_its_fetch_for_the_bound(self, stand):
        clock = [1000.0]
        stand.relay.memory.clock = stand.relay.clock = lambda: clock[0]
        stand.relay.upstream.clock = time.monotonic
        assert stand.relay.refresh_keys() is True
        stand.stop_bot()
        clock[0] += relay.MEMORY_S - 1
        assert stand.auth(UUID) == ALLOW
        clock[0] += 1
        assert stand.auth(UUID) == DENY


# ------------------------------------------------------------------ the runbook --

RUNBOOK = REPO / 'docs' / 'runbooks' / 'hy2_auth_relay.md'
OLD = 'https://entry-hop.invalid:8448/api/hy2'
NEW = 'http://127.0.0.1:9979/api/hy2'
ACL = ('acl:\n  inline:\n    - reject(0.0.0.0/8)\n    - reject(127.0.0.0/8)\n'
       '    - reject(::1/128)\n')
MAIN = ('listen: :8400\nauth:\n  type: http\n  http:\n    url: ' + OLD + '/auth\n'
        'masquerade:\n  type: proxy\n  proxy:\n    url: https://www.bing.com/\n' + ACL)
TURBO = 'listen: :8402\nauth:\n  type: http\n  http:\n    url: ' + OLD + 't/auth\n' + ACL

# Commands the runbook's blocks run on exit: fakes that log, first in PATH;
# anything that would reach the machine's power state, the network or other
# processes fails the test.
_RB_FAKES = {
    'systemctl': ('#!/bin/sh\necho "systemctl $*" >> "$LOG"\n'
                  'if [ "$1 $2" = "is-active --quiet" ]; then [ -e "$STATE/relay-active" ]; exit $?; fi\n'
                  'if [ "$1" = is-active ]; then shift; for u in "$@"; do echo active; done; fi\n'),
    **{t: '#!/bin/sh\necho "TRIPWIRE $0 $*" >> "$LOG"\nexit 1\n'
       for t in ('reboot', 'shutdown', 'poweroff', 'halt', 'init', 'telinit', 'kill', 'pkill',
                 'killall', 'curl', 'ssh', 'docker', 'iptables')},
}


def _rb_blocks(kind):
    import re
    text = RUNBOOK.read_text()
    if kind == 'bash':
        return re.findall(r'```bash\n(.*?)```', text, re.S)
    if kind == 'sh':
        return re.findall(r"bash -s[^\n]*<<'SH'\n(.*?)\n\s*SH\n", text, re.S)
    return re.findall(r"python3 - [^\n]*<<'PY'\n(.*?)\nPY\n", text, re.S)


class TestRunbook:
    """docs/runbooks/hy2_auth_relay.md: every block parses, and the blocks that
    edit exit do exactly their edit, on copies."""

    @pytest.fixture
    def node(self, tmp_path):
        import os
        import subprocess
        bin_dir, state = tmp_path / 'bin', tmp_path / 'state'
        bin_dir.mkdir()
        state.mkdir()
        for name, body in _RB_FAKES.items():
            (bin_dir / name).write_text(body)
            (bin_dir / name).chmod(0o755)
        etc = tmp_path / 'hysteria'
        etc.mkdir()
        (etc / 'config.yaml').write_text(MAIN)
        (etc / 'config-turbo.yaml').write_text(TURBO)
        log = tmp_path / 'calls.log'
        env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'LOG': str(log),
               'STATE': str(state)}

        def run(body, *args):
            body = (body.replace('/etc/hysteria/', f'{etc}/')
                        .replace('/etc/systemd/system/', f'{tmp_path}/units/')
                        .replace('/usr/local/lib/', f'{tmp_path}/lib/'))
            assert '/etc/' not in body and '/usr/local' not in body
            return subprocess.run(['bash', '-s', '--', *args], input=body, env=env,
                                  capture_output=True, text=True, timeout=30)
        node = SimpleNamespace(root=tmp_path, etc=etc, run=run, log=log, state=state)
        node.calls = lambda: log.read_text() if log.exists() else ''
        yield node
        assert 'TRIPWIRE' not in node.calls()

    def _switch(self):
        return next(b for b in _rb_blocks('sh') if 'sed -i' in b)

    def test_every_block_parses(self):
        import ast
        import subprocess
        assert len(_rb_blocks('bash')) >= 9 and len(_rb_blocks('sh')) == 3
        for body in _rb_blocks('bash') + _rb_blocks('sh'):
            r = subprocess.run(['bash', '-n'], input=body, capture_output=True, text=True)
            assert r.returncode == 0, (body[:80], r.stderr)
        pys = _rb_blocks('py')
        assert len(pys) == 1
        ast.parse(pys[0])

    def test_the_switch(self, node):
        (node.state / 'relay-active').touch()
        r = node.run(self._switch(), 'TS')
        assert r.returncode == 0, r.stdout + r.stderr
        assert (node.etc / 'config.yaml').read_text() == MAIN.replace(OLD, NEW)
        assert (node.etc / 'config-turbo.yaml').read_text() == TURBO.replace(OLD, NEW)
        assert 'url: http://127.0.0.1:9979/api/hy2/auth' in (node.etc / 'config.yaml').read_text()
        assert 'url: http://127.0.0.1:9979/api/hy2t/auth' in \
            (node.etc / 'config-turbo.yaml').read_text()
        assert 'https://www.bing.com/' in (node.etc / 'config.yaml').read_text()
        assert (node.etc / 'config.yaml.bak-a6-TS').read_text() == MAIN
        assert (node.etc / 'config-turbo.yaml.bak-a6-TS').read_text() == TURBO
        # a second run finds no line to move and touches nothing
        r = node.run(self._switch(), 'TS2')
        assert r.returncode == 1 and 'not as expected' in r.stdout
        assert not (node.etc / 'config.yaml.bak-a6-TS2').exists()
        assert 'restart' not in node.calls()           # the restart is its own step

    def test_the_switch_waits_for_the_relay(self, node):
        r = node.run(self._switch(), 'TS')
        assert r.returncode == 1 and 'the relay is not running' in r.stdout
        assert (node.etc / 'config.yaml').read_text() == MAIN
        assert not list(node.etc.glob('*.bak-*'))

    @pytest.mark.parametrize('acl', ['', '# acl:\n#   inline:\n#     - reject(127.0.0.0/8)\n'])
    def test_the_switch_needs_the_acl(self, node, acl):
        (node.state / 'relay-active').touch()
        (node.etc / 'config-turbo.yaml').write_text(TURBO.replace(ACL, acl))
        r = node.run(self._switch(), 'TS')
        assert r.returncode == 1 and 'no ACL closing loopback' in r.stdout
        assert (node.etc / 'config.yaml').read_text() == MAIN
        assert not list(node.etc.glob('*.bak-*'))

    def test_rolling_back(self, node):
        (node.state / 'relay-active').touch()
        assert node.run(self._switch(), 'TS').returncode == 0
        remove = next(b for b in _rb_blocks('sh') if 'disable --now' in b)
        (node.root / 'units').mkdir()
        unit = node.root / 'units' / 'hy2-auth-relay.service'
        unit.write_text('[Service]\n')
        lib = node.root / 'lib' / 'nekovpn-hy2-relay'
        lib.mkdir(parents=True)
        (lib / 'hy2_auth_relay.py').write_text('relay')
        # the relay cannot go while hysteria asks it
        r = node.run(remove)
        assert r.returncode == 1 and 'roll back step 3 first' in r.stdout
        assert unit.exists() and lib.exists()
        back = next(b for b in _rb_blocks('sh') if 'bak-a6-$1' in b and 'cp -a "/etc' in b)
        r = node.run(back, 'TS')
        assert r.returncode == 0, r.stderr
        assert (node.etc / 'config.yaml').read_text() == MAIN
        assert (node.etc / 'config-turbo.yaml').read_text() == TURBO
        assert 'systemctl restart hysteria hysteria-turbo' in node.calls()
        r = node.run(remove)
        assert r.returncode == 0 and 'removed' in r.stdout, r.stdout + r.stderr
        assert not unit.exists() and not lib.exists()
        assert 'systemctl disable --now hy2-auth-relay' in node.calls()

    def test_the_key_count_block(self, stand, tmp_path):
        import subprocess
        body = _rb_blocks('py')[0]
        env_file = tmp_path / 'exit-dpi.env'
        cert = tmp_path / 'hop' / 'cert.pem'
        env_file.write_text(f'DPI_REPORT_TOKEN="{TOKEN}"\n# a comment\n'
                            f'BOT_URL=https://127.0.0.1:{stand.bot.tls_port}\n'
                            f"BOT_CA_FILE='{cert}'\n")
        r = subprocess.run(['python3', '-'], input=body.replace('/etc/exit-dpi.env', str(env_file)),
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "{'hy2': 3, 'hy2t': 2}"
        assert TOKEN not in r.stdout + r.stderr

    def test_it_matches_the_code(self):
        text = RUNBOOK.read_text()
        assert relay.DEFAULT_LISTEN == '127.0.0.1:9979' and NEW in text
        # the state the switch starts from is the one §53's runbook leaves
        assert OLD in (REPO / 'docs' / 'runbooks' / 'entry_hop_tls.md').read_text()
        for path in ('scripts/hy2_auth_relay.py', 'scripts/install_hy2_auth_relay.sh',
                     'systemd/hy2-auth-relay.service'):
            assert path in text and (REPO / path).exists()
        installer = (REPO / 'scripts' / 'install_hy2_auth_relay.sh').read_text()
        pattern = "'^[^#]*http://(127\\.[0-9.]+|\\[::1\\]|localhost)(:[0-9]+)?/api/hy2t?/auth'"
        assert pattern in installer and pattern in text
