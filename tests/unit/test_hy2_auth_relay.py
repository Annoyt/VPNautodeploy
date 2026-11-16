"""scripts/hy2_auth_relay.py — the hy2 auth relay on exit (IMPROVEMENT_PLAN F,
A6; AGENTS.md §56).

What is pinned here, without the bot (a fake upstream, or small HTTP(S)
servers of the test's own):
  * the memory: an allow counts from the question, for at most the bound; a
    deny drops the key (on hy2 also on hy2t, never the reverse); the newer
    word wins between answers and the bot's key list; a list never
    overrides what the bot said after the list was asked for; denies of
    unknown keys are not kept; 0 remembers nothing;
  * what counts as the bot's answer (HTTP 200, a boolean ``ok``, an id for
    an allow) and as its key list (all of it well-formed, or none of it);
  * the hop: the pin is the only trust anchor, kept-alive connections are
    reused and a stale one is retried once, a refused or hanging bot costs
    at most the timeouts and opens a short memory-only window;
  * the listener: loopback addresses only, the two routes, size limits,
    keep-alive, a status that carries no key, hash or id;
  * the hash is the bot's (bot/services/hy2_keys.py), byte for byte.
"""

import http.client
import http.server
import importlib.util
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from bot.services import hop_tls, hy2_keys

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / 'scripts' / 'hy2_auth_relay.py'
UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
OTHER = '1f2e3d4c-5b6a-4978-8a6b-5c4d3e2f1a00'


def _load():
    spec = importlib.util.spec_from_file_location('hy2_auth_relay_under_test', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


relay = _load()


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


H = relay.key_hash(UUID)
H2 = relay.key_hash(OTHER)


# ------------------------------------------------------------------ the hash --

class TestHash:

    def test_the_bot_and_the_relay_name_a_key_alike(self):
        assert relay.HASH_PREFIX == hy2_keys.HASH_PREFIX
        for key in (UUID, OTHER, '', 'ключ', 'a' * 300, '\ud800'):
            assert relay.key_hash(key) == hy2_keys.key_hash(key)

    def test_it_is_no_plain_sha256_of_the_key(self):
        import hashlib
        assert H != hashlib.sha256(UUID.encode()).hexdigest()
        assert relay.HEX64.fullmatch(H)


# ---------------------------------------------------------------- the memory --

class TestMemory:

    def mem(self, memory_s=900):
        clock = Clock()
        return relay.Memory(memory_s, clock), clock

    def test_an_allow_is_recalled_until_the_bound(self):
        m, clock = self.mem()
        m.allowed('hy2', H, 'u@x', clock())
        clock.tick(899.9)
        assert m.recall('hy2', H) == ('u@x', pytest.approx(899.9))
        clock.tick(0.1)
        assert m.recall('hy2', H) is None

    def test_the_bound_counts_from_the_question(self):
        m, clock = self.mem()
        asked = clock()
        clock.tick(5)                         # the bot took 5 s to answer
        m.allowed('hy2', H, 'u@x', asked)
        clock.tick(895)
        assert m.recall('hy2', H) is None

    def test_an_allow_is_for_its_route_only(self):
        m, clock = self.mem()
        m.allowed('hy2', H, 'u@x', clock())
        assert m.recall('hy2t', H) is None
        assert m.recall('hy2', H2) is None

    def test_a_deny_drops_the_key(self):
        m, clock = self.mem()
        m.allowed('hy2', H, 'u@x', clock())
        clock.tick(1)
        m.denied('hy2', H, clock())
        assert m.recall('hy2', H) is None

    def test_a_deny_on_hy2_holds_for_hy2t(self):
        m, clock = self.mem()
        m.allowed('hy2', H, 'u@x', clock())
        m.allowed('hy2t', H, 'u@x', clock())
        clock.tick(1)
        m.denied('hy2', H, clock())
        assert m.recall('hy2t', H) is None

    def test_a_deny_on_hy2t_leaves_hy2(self):
        # a demo key: refused by turbo, still welcome on the main instance
        m, clock = self.mem()
        m.allowed('hy2', H, 'u@x', clock())
        m.allowed('hy2t', H, 'u@x', clock())
        clock.tick(1)
        m.denied('hy2t', H, clock())
        assert m.recall('hy2', H)[0] == 'u@x'
        assert m.recall('hy2t', H) is None

    def test_the_newer_word_wins(self):
        m, clock = self.mem()
        asked = clock()
        clock.tick(1)
        m.denied('hy2', H, clock())           # answered after the allow was asked
        m.allowed('hy2', H, 'u@x', asked)     # ... whose answer came in last
        assert m.recall('hy2', H) is None
        clock.tick(1)
        m.allowed('hy2', H, 'u@x', clock())   # asked after the deny
        assert m.recall('hy2', H)[0] == 'u@x'

    def test_denies_of_unknown_keys_are_forgotten_soon(self):
        m, clock = self.mem()
        for i in range(100):
            m.denied('hy2', relay.key_hash(f'stranger-{i}'), clock())
        assert len(m._entries) == 200                   # hy2 and hy2t
        clock.tick(relay.DENY_KEEP_S - 0.1)
        m.prune()
        assert len(m._entries) == 200
        clock.tick(0.1)
        m.denied('hy2', H, clock())                     # any deny clears the old ones
        assert set(m._entries) == {('hy2', H), ('hy2t', H)}
        clock.tick(relay.DENY_KEEP_S)
        m.prune()                                       # ... and so does the prune
        assert m._entries == {}

    def test_a_deny_outlasts_the_answers_and_lists_it_may_contradict(self):
        assert relay.DENY_KEEP_S > max(relay.ANSWER_TIMEOUT_S, relay.KEYS_TIMEOUT_S)
        m, clock = self.mem()
        asked = clock()
        clock.tick(relay.ANSWER_TIMEOUT_S - 0.5)
        m.denied('hy2', H, clock())
        clock.tick(0.5)
        m.allowed('hy2', H, 'u@x', asked)               # the late answer
        assert m.recall('hy2', H) is None

    def test_a_forgotten_deny_leaves_a_newer_allow_alone(self):
        m, clock = self.mem()
        m.denied('hy2', H, clock())
        clock.tick(1)
        m.allowed('hy2', H, 'u@x', clock())
        clock.tick(relay.DENY_KEEP_S)
        m.prune()
        assert m.recall('hy2', H)[0] == 'u@x'

    def test_a_deny_during_a_list_fetch_wins_over_the_list(self):
        # the list was asked for before the bot banned the key; the deny
        # came in while it was on its way
        m, clock = self.mem()
        started = m.begin_list()
        clock.tick(0.2)
        m.denied('hy2', H, clock())
        clock.tick(0.2)
        m.end_list(started, {'hy2': {H: 'u@x'}, 'hy2t': {H: 'u@x'}})
        assert m.recall('hy2', H) is None
        assert m.recall('hy2t', H) is None

    def test_a_list_replaces_what_is_older(self):
        m, clock = self.mem()
        m.allowed('hy2', H, 'u@x', clock())
        clock.tick(10)
        started = m.begin_list()
        m.end_list(started, {'hy2': {H2: 'v@x'}, 'hy2t': {}})
        assert m.recall('hy2', H) is None     # the bot no longer lists it
        assert m.recall('hy2', H2)[0] == 'v@x'
        assert m.list_at == started and m.list_size == {'hy2': 1, 'hy2t': 0}

    def test_an_answer_after_the_list_was_asked_for_survives_it(self):
        m, clock = self.mem()
        started = m.begin_list()
        clock.tick(0.1)
        m.allowed('hy2', H, 'u@x', clock())   # a new key, allowed live
        m.end_list(started, {'hy2': {}, 'hy2t': {}})
        assert m.recall('hy2', H)[0] == 'u@x'

    def test_the_list_counts_from_the_start_of_its_fetch(self):
        m, clock = self.mem()
        started = m.begin_list()
        clock.tick(3)
        m.end_list(started, {'hy2': {H: 'u@x'}, 'hy2t': {}})
        clock.tick(897)
        assert m.recall('hy2', H) is None

    def test_no_list_keeps_the_one_in_memory(self):
        m, clock = self.mem()
        first = m.begin_list()
        m.end_list(first, {'hy2': {H: 'u@x'}, 'hy2t': {}})
        clock.tick(60)
        m.end_list(m.begin_list(), None)
        assert m.recall('hy2', H) == ('u@x', 60.0)
        assert m.list_at == first

    def test_zero_remembers_nothing(self):
        m, clock = self.mem(0)
        m.allowed('hy2', H, 'u@x', clock())
        assert m.recall('hy2', H) is None

    def test_prune_and_counts(self):
        m, clock = self.mem()
        m.denied('hy2', relay.key_hash('stranger'), clock())
        assert m.counts() == {'hy2': 0, 'hy2t': 0}      # a deny lets nobody in
        m.allowed('hy2', H, 'u@x', clock())
        clock.tick(600)
        m.allowed('hy2', H2, 'v@x', clock())
        m.allowed('hy2t', H2, 'v@x', clock())
        assert m.counts() == {'hy2': 2, 'hy2t': 1}
        clock.tick(300)
        assert m.counts() == {'hy2': 1, 'hy2t': 1}
        m.prune()
        assert set(m._entries) == {('hy2', H2), ('hy2t', H2)}


# ---------------------------------------------------------------- the parsing --

class TestParse:

    @pytest.mark.parametrize('status, body, want', [
        (200, b'{"ok": true, "id": "u@x"}', {'ok': True, 'id': 'u@x'}),
        (200, b'{"ok": false}', {'ok': False}),
        (200, b'{"ok": false, "id": "u@x"}', {'ok': False}),
        (500, b'{"ok": true, "id": "u@x"}', None),
        (403, b'{"ok": false}', None),
        (200, b'<html>', None),
        (200, b'[true]', None),
        (200, b'{"ok": "true", "id": "u@x"}', None),
        (200, b'{"ok": 1, "id": "u@x"}', None),
        (200, b'{"ok": true}', None),
        (200, b'{"ok": true, "id": ""}', None),
        (200, b'{"ok": true, "id": 42}', None),
    ])
    def test_an_answer(self, status, body, want):
        assert relay.parse_answer(status, body) == want

    def good(self, **over):
        doc = {'ok': True, 'hy2': {H: 'u@x', H2: 'v@x'}, 'hy2t': {H: 'u@x'}}
        doc.update(over)
        return json.dumps(doc).encode()

    def test_a_list(self):
        assert relay.parse_keys(self.good()) == {'hy2': {H: 'u@x', H2: 'v@x'},
                                                 'hy2t': {H: 'u@x'}}

    @pytest.mark.parametrize('over', [
        {'ok': False}, {'ok': 'yes'}, {'hy2': None}, {'hy2t': []},
        {'hy2': {H.upper(): 'u@x'}}, {'hy2': {H[:-1]: 'u@x'}}, {'hy2': {UUID: 'u@x'}},
        {'hy2': {H: ''}}, {'hy2': {H: 'x' * 257}}, {'hy2t': {H: 7}},
    ])
    def test_any_flaw_rejects_the_whole_list(self, over):
        assert relay.parse_keys(self.good(**over)) is None

    def test_a_list_without_one_route_is_refused(self):
        assert relay.parse_keys(json.dumps({'ok': True, 'hy2': {}}).encode()) is None
        assert relay.parse_keys(b'not json') is None


# ------------------------------------------------------------------ the relay --

class FakeUpstream:
    """The bot as the relay sees it: an answer, or None (no answer)."""

    def __init__(self):
        self.answer = None
        self.calls = []
        self.reached_at = self.failed_at = self.last_error = None
        self.skip_until = float('-inf')

    def ask(self, path, body):
        self.calls.append((path, body))
        return self.answer


def body(auth=UUID, **extra):
    return json.dumps({'addr': '130.49.146.10:41234', 'auth': auth, 'tx': 0, **extra}).encode()


class TestDecide:

    def make(self, memory_s=900):
        clock = Clock()
        up = FakeUpstream()
        return relay.Relay(up, relay.Memory(memory_s, clock)), up, clock

    def test_the_bots_answers_go_back_as_they_are(self):
        r, up, _ = self.make()
        up.answer = {'ok': True, 'id': 'u@x'}
        assert r.auth('/api/hy2/auth', body()) == (200, {'ok': True, 'id': 'u@x'})
        up.answer = {'ok': False}
        assert r.auth('/api/hy2t/auth', body()) == (200, {'ok': False})
        # the request goes on byte for byte, to the same route
        assert up.calls == [('/api/hy2/auth', body()), ('/api/hy2t/auth', body())]

    def test_bot_down_a_remembered_key_passes_others_do_not(self):
        r, up, clock = self.make()
        up.answer = {'ok': True, 'id': 'u@x'}
        r.auth('/api/hy2/auth', body())
        up.answer = None
        clock.tick(60)
        assert r.auth('/api/hy2/auth', body()) == (200, {'ok': True, 'id': 'u@x'})
        assert r.auth('/api/hy2/auth', body(OTHER)) == (200, {'ok': False})
        assert r.auth('/api/hy2t/auth', body()) == (200, {'ok': False})   # never let in there
        assert r.served == {'bot_allow': 1, 'bot_deny': 0, 'memory_allow': 1, 'memory_deny': 2}

    def test_the_bound_counts_from_the_question_not_the_answer(self):
        r, up, clock = self.make()

        class Slow(FakeUpstream):
            def ask(self, path, body):
                clock.tick(5)                       # the bot took 5 s
                return self.answer
        r.upstream = slow = Slow()
        slow.answer = {'ok': True, 'id': 'u@x'}
        r.auth('/api/hy2/auth', body())
        slow.answer = None
        clock.tick(894)                             # 899 s after the question
        assert r.auth('/api/hy2/auth', body())[1]['ok'] is False

    def test_bot_down_after_the_bound_nobody_passes(self):
        r, up, clock = self.make()
        up.answer = {'ok': True, 'id': 'u@x'}
        r.auth('/api/hy2/auth', body())
        up.answer = None
        clock.tick(900)
        assert r.auth('/api/hy2/auth', body()) == (200, {'ok': False})

    def test_a_deny_from_the_bot_is_not_undone_by_memory(self):
        r, up, clock = self.make()
        up.answer = {'ok': True, 'id': 'u@x'}
        r.auth('/api/hy2/auth', body())
        clock.tick(1)
        up.answer = {'ok': False}                       # banned
        r.auth('/api/hy2/auth', body())
        up.answer = None
        assert r.auth('/api/hy2/auth', body()) == (200, {'ok': False})

    def test_the_relay_never_answers_from_memory_while_the_bot_answers(self):
        r, up, clock = self.make()
        up.answer = {'ok': True, 'id': 'u@x'}
        r.auth('/api/hy2/auth', body())
        up.answer = {'ok': False}
        assert r.auth('/api/hy2/auth', body()) == (200, {'ok': False})

    @pytest.mark.parametrize('payload', [{'auth': 42}, {'auth': ''}, {'addr': 'x'}])
    def test_a_request_without_a_key_goes_to_the_bot_and_memory_holds_nothing(self, payload):
        r, up, _ = self.make()
        raw = json.dumps(payload).encode()
        up.answer = {'ok': True, 'id': 'u@x'}            # whatever the bot makes of it
        assert r.auth('/api/hy2/auth', raw) == (200, {'ok': True, 'id': 'u@x'})
        assert up.calls == [('/api/hy2/auth', raw)]
        up.answer = None
        assert r.auth('/api/hy2/auth', raw) == (200, {'ok': False})
        assert r.memory._entries == {}

    @pytest.mark.parametrize('path, raw, want', [
        ('/api/hy2/auth?x=1', body(), 404), ('/api/hy2/keys', body(), 404),
        ('/', body(), 404), ('/api/hy2/auth', b'{', 400), ('/api/hy2/auth', b'[]', 400),
        ('/api/hy2/auth', b'"x"', 400),
    ])
    def test_what_is_not_an_auth_request_never_reaches_the_bot(self, path, raw, want):
        r, up, _ = self.make()
        up.answer = {'ok': True, 'id': 'u@x'}
        assert r.auth(path, raw) == (want, {'ok': False})
        assert up.calls == []


# ------------------------------------------------------------------- servers --

def _cert(tmp_path, name='hop', ip='127.0.0.1'):
    d = tmp_path / name
    if not (d / 'cert.pem').exists():
        hop_tls.generate(str(d), ip)
    return str(d / 'cert.pem'), str(d / 'key.pem')


class _Bot(http.server.BaseHTTPRequestHandler):
    """A stand-in bot. ``self.server.mode``: allow / deny / 500 / junk / big /
    close (answer, then drop the connection without saying so)."""
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        self.rfile.read(n)
        self.server.hits.append(self.path)
        mode = self.server.mode
        data = {'allow': b'{"ok": true, "id": "u@x"}', 'deny': b'{"ok": false}',
                '500': b'{"ok": true, "id": "u@x"}', 'junk': b'<html>',
                'big': b'{"ok": true, "id": "' + b'x' * 5000 + b'"}',
                'close': b'{"ok": true, "id": "u@x"}'}[mode]
        self.send_response(500 if mode == '500' else 200)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        if mode == 'close':
            self.close_connection = True

    do_GET = do_POST

    def log_message(self, *a):
        pass


class _BotServer(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, ctx=None, mode='allow'):
        self.hits, self.conns, self.mode = [], 0, mode
        self.ctx = ctx
        super().__init__(('127.0.0.1', 0), _Bot)

    def get_request(self):
        sock, addr = super().get_request()
        self.conns += 1
        if self.ctx is not None:
            sock = self.ctx.wrap_socket(sock, server_side=True)
        return sock, addr


@pytest.fixture
def bot_factory(tmp_path):
    started = []

    def make(mode='allow', tls=True, cert=None):
        ctx = None
        if tls:
            c, k = cert or _cert(tmp_path)
            ctx = hop_tls.server_context(c, k)
        srv = _BotServer(ctx, mode)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv
    yield make
    for s in started:
        s.shutdown()
        s.server_close()


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class TestUpstream:

    def up(self, tmp_path, port, *, tls=True, ca=None, clock=time.monotonic):
        if tls:
            ca = ca or str(tmp_path / 'hop' / 'cert.pem')
            return relay.Upstream(f'https://127.0.0.1:{port}', ca, clock)
        return relay.Upstream(f'http://127.0.0.1:{port}', '', clock)

    def test_the_pin_is_the_only_trust_anchor(self, tmp_path):
        cert, _ = _cert(tmp_path)
        ctx = relay.client_context(cert)
        assert len(ctx.get_ca_certs()) == 1
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
        assert ctx.minimum_version == ssl.TLSVersion.TLSv1_3

    def test_the_pin_context_never_loads_the_system_cas(self, tmp_path, monkeypatch):
        cert, _ = _cert(tmp_path)
        loaded = []
        monkeypatch.setattr(ssl.SSLContext, 'load_default_certs',
                            lambda self, *a, **k: loaded.append('default'))
        monkeypatch.setattr(ssl.SSLContext, 'set_default_verify_paths',
                            lambda self, *a, **k: loaded.append('paths'))
        relay.client_context(cert)
        assert loaded == []
        relay.client_context('')                    # no pin: the system's CAs
        assert loaded

    def test_answers_over_the_pin_on_one_connection(self, tmp_path, bot_factory):
        srv = bot_factory('allow')
        up = self.up(tmp_path, srv.server_address[1])
        for _ in range(5):
            assert up.ask('/api/hy2/auth', body()) == {'ok': True, 'id': 'u@x'}
        assert srv.conns == 1 and srv.hits == ['/api/hy2/auth'] * 5

    def test_a_connection_the_bot_dropped_is_retried_once(self, tmp_path, bot_factory):
        srv = bot_factory('close')
        up = self.up(tmp_path, srv.server_address[1])
        for _ in range(3):
            assert up.ask('/api/hy2t/auth', body()) == {'ok': True, 'id': 'u@x'}
        assert srv.hits == ['/api/hy2t/auth'] * 3        # each request reached it once

    def test_a_bot_url_with_a_path_prefix(self, tmp_path, bot_factory):
        srv = bot_factory('allow', tls=False)
        up = relay.Upstream(f'http://127.0.0.1:{srv.server_address[1]}/bot/', '')
        up.ask('/api/hy2/auth', body())
        assert srv.hits == ['/bot/api/hy2/auth']

    @pytest.mark.parametrize('mode, why', [('500', 'HTTP 500'), ('junk', 'HTTP 200'),
                                           ('big', 'TooBig: more than 4096 bytes')])
    def test_what_is_no_answer(self, tmp_path, bot_factory, mode, why):
        srv = bot_factory(mode)
        up = self.up(tmp_path, srv.server_address[1])
        assert up.ask('/api/hy2/auth', body()) is None
        assert why in up.last_error and up.failed_at is not None
        assert up.skip_until == float('-inf')          # the bot is there: ask again

    def test_another_certificate_is_no_answer(self, tmp_path, bot_factory):
        srv = bot_factory('allow', cert=_cert(tmp_path, 'other'))
        _cert(tmp_path)                                 # the pin the relay holds
        up = self.up(tmp_path, srv.server_address[1])
        assert up.ask('/api/hy2/auth', body()) is None
        assert 'CERTIFICATE_VERIFY_FAILED' in up.last_error
        assert srv.hits == []

    def test_another_name_is_no_answer(self, tmp_path, bot_factory):
        # the pin names 203.0.113.20 and entry-hop.invalid, not 127.0.0.1
        srv = bot_factory('allow', cert=_cert(tmp_path, 'hop', '203.0.113.20'))
        up = self.up(tmp_path, srv.server_address[1])
        assert up.ask('/api/hy2/auth', body()) is None
        assert srv.hits == []

    def test_plain_http_to_a_tls_bot_is_no_answer(self, tmp_path, bot_factory):
        srv = bot_factory('allow')
        up = self.up(tmp_path, srv.server_address[1], tls=False)
        assert up.ask('/api/hy2/auth', body()) is None

    def test_refused_opens_a_short_memory_only_window(self, tmp_path):
        _cert(tmp_path)
        clock = Clock()
        port = _free_port()
        up = self.up(tmp_path, port, clock=clock)
        assert up.ask('/api/hy2/auth', body()) is None
        assert 'ConnectionRefused' in up.last_error
        # the bot comes back: inside the window nobody asks it
        lsock = socket.socket()
        lsock.bind(('127.0.0.1', port))
        lsock.listen(8)
        lsock.settimeout(0.3)
        try:
            clock.tick(relay.SKIP_S - 0.01)
            assert up.ask('/api/hy2/auth', body()) is None
            with pytest.raises(socket.timeout):
                lsock.accept()
            clock.tick(0.01)
            threading.Thread(target=up.ask, args=('/api/hy2/auth', body()), daemon=True).start()
            conn, _ = lsock.accept()                    # asked again after the window
            conn.close()
        finally:
            lsock.close()

    def test_an_answer_closes_the_window(self, tmp_path, bot_factory):
        # the key list's fetch reaches the bot first: auth asks it again at once
        srv = bot_factory('allow')
        clock = Clock()
        up = self.up(tmp_path, srv.server_address[1], clock=clock)
        up.skip_until = clock() + relay.SKIP_S
        assert up.ask('/api/hy2/auth', body()) is None
        assert up.request('GET', '/api/hy2/keys', timeout=5, max_bytes=100)[0] == 200
        assert up.ask('/api/hy2/auth', body()) == {'ok': True, 'id': 'u@x'}

    def test_a_bot_that_takes_the_connection_and_hangs(self, tmp_path, monkeypatch):
        _cert(tmp_path)
        monkeypatch.setattr(relay, 'CONNECT_TIMEOUT_S', 0.3)
        monkeypatch.setattr(relay, 'ANSWER_TIMEOUT_S', 0.8)
        lsock = socket.socket()
        lsock.bind(('127.0.0.1', 0))
        lsock.listen(8)                                 # accepts, never speaks TLS
        try:
            up = self.up(tmp_path, lsock.getsockname()[1])
            t0 = time.monotonic()
            assert up.ask('/api/hy2/auth', body()) is None
            assert time.monotonic() - t0 < 1.5
            assert up.skip_until > time.monotonic()     # a connect that failed
        finally:
            lsock.close()

    def test_a_bot_that_never_answers_costs_at_most_the_answer_timeout(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(relay, 'ANSWER_TIMEOUT_S', 0.8)
        lsock = socket.socket()
        lsock.bind(('127.0.0.1', 0))
        lsock.listen(8)
        held = []

        def take():
            c, _ = lsock.accept()
            held.append(c)                              # reads nothing, says nothing
        threading.Thread(target=take, daemon=True).start()
        try:
            up = self.up(tmp_path, lsock.getsockname()[1], tls=False)
            t0 = time.monotonic()
            assert up.ask('/api/hy2/auth', body()) is None
            assert 0.7 < time.monotonic() - t0 < 1.5
            assert up.skip_until == float('-inf')       # it was there: not a failed connect
        finally:
            for c in held:
                c.close()
            lsock.close()

    def test_no_more_than_the_idle_cap_is_kept(self, tmp_path, bot_factory):
        srv = bot_factory('allow')
        up = self.up(tmp_path, srv.server_address[1])
        conns = [up._take()[0] for _ in range(relay.IDLE_MAX + 3)]
        for c in conns:
            c.connect()
            up._give(c)
        assert len(up._idle) == relay.IDLE_MAX

    def test_an_old_idle_connection_is_not_reused(self, tmp_path, bot_factory):
        srv = bot_factory('allow')
        clock = Clock()
        up = self.up(tmp_path, srv.server_address[1], clock=clock)
        up.ask('/api/hy2/auth', body())
        clock.tick(relay.IDLE_REUSE_S)
        up.ask('/api/hy2/auth', body())
        assert srv.conns == 2


# ------------------------------------------------------------- the listener --

@pytest.fixture
def listener():
    started = []

    def make(answer=None, memory_s=900):
        up = FakeUpstream()
        up.answer = answer
        r = relay.Relay(up, relay.Memory(memory_s), token='')
        srv = relay.Server(('127.0.0.1', 0), r)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv, r, up
    yield make
    for s in started:
        s.shutdown()
        s.server_close()


def _post(conn, path, data, headers=None):
    conn.request('POST', path, body=data, headers={'Content-Type': 'application/json',
                                                   **(headers or {})})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read() or b'null'), resp


class TestListener:

    def test_two_answers_on_one_connection(self, listener):
        srv, r, up = listener({'ok': True, 'id': 'u@x'})
        conn = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
        for path in ('/api/hy2/auth', '/api/hy2t/auth'):
            status, data, resp = _post(conn, path, body())
            assert (status, data) == (200, {'ok': True, 'id': 'u@x'})
            assert resp.getheader('Content-Type') == 'application/json'
            assert not resp.will_close
        conn.close()

    @pytest.mark.parametrize('path, data, headers, want', [
        ('/api/hy2/auth', b'{' * (relay.MAX_BODY + 1), {}, 413),
        ('/api/hy2/auth', body(), {'Content-Length': 'abc'}, 411),
        ('/api/hy2/auth', b'{not json', {}, 400),
        ('/nope', body(), {}, 404),
    ])
    def test_what_is_refused(self, listener, path, data, headers, want):
        srv, r, up = listener({'ok': True, 'id': 'u@x'})
        conn = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
        status, payload, resp = _post(conn, path, data, headers)
        assert (status, payload) == (want, {'ok': False})
        assert resp.will_close
        assert up.calls == []
        conn.close()

    def test_the_status_carries_no_key_hash_or_id(self, listener):
        srv, r, up = listener({'ok': True, 'id': 'secret-id@x'})
        conn = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
        _post(conn, '/api/hy2/auth', body())
        conn.request('GET', '/status')
        resp = conn.getresponse()
        text = resp.read().decode()
        status = json.loads(text)
        assert resp.status == 200 and status['ok'] is True
        assert status['remembered'] == {'hy2': 1, 'hy2t': 0}
        assert status['served']['bot_allow'] == 1
        assert status['key_list']['on'] is False
        for secret in (UUID, H, 'secret-id'):
            assert secret not in text
        conn.request('GET', '/status?x')
        assert conn.getresponse().status == 404
        conn.close()


# ---------------------------------------------------------------- the config --

class TestConfig:

    def env(self, tmp_path, **over):
        cert, _ = _cert(tmp_path)
        env = {'BOT_URL': 'https://130.49.146.10:8448', 'BOT_CA_FILE': cert,
               'DPI_REPORT_TOKEN': ' tok '}
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def test_the_defaults(self, tmp_path):
        cfg, problems = relay.load_config(self.env(tmp_path, BOT_URL=None))
        assert problems == []
        assert cfg['bot_url'] == 'https://130.49.146.10:8448'
        assert (cfg['host'], cfg['port']) == ('127.0.0.1', 9979)
        assert cfg['memory_s'] == 900 and cfg['token'] == 'tok'

    def test_the_default_pin_is_the_hops(self):
        cfg, problems = relay.load_config({})
        assert cfg['ca_file'] == '/etc/nekovpn-hop/entry-hop.pem'
        if not os.path.exists(cfg['ca_file']):
            assert any('BOT_CA_FILE' in p for p in problems)

    @pytest.mark.parametrize('listen, host, port', [
        ('127.0.0.1:9979', '127.0.0.1', 9979), ('127.0.0.2:1', '127.0.0.2', 1),
        ('[::1]:9979', '::1', 9979), (' 127.0.0.1:65535 ', '127.0.0.1', 65535),
    ])
    def test_loopback_listeners(self, tmp_path, listen, host, port):
        cfg, problems = relay.load_config(self.env(tmp_path, HY2_RELAY_LISTEN=listen))
        assert problems == [] and (cfg['host'], cfg['port']) == (host, port)

    @pytest.mark.parametrize('listen', [
        '0.0.0.0:9979', '[::]:9979', '10.0.0.1:9979', '172.20.0.1:9979', '84.75.76.109:9979',
        'localhost:9979', '[::ffff:127.0.0.1]:9979', '127.0.0.1', '127.0.0.1:0',
        '127.0.0.1:65536', '127.0.0.1:x', ':9979',
    ])
    def test_anything_else_does_not_start(self, tmp_path, listen):
        _cfg, problems = relay.load_config(self.env(tmp_path, HY2_RELAY_LISTEN=listen))
        assert problems and 'HY2_RELAY_LISTEN' in problems[0]

    @pytest.mark.parametrize('url, ok', [
        ('http://127.0.0.1:8080', True), ('http://localhost:8080', True),
        ('http://130.49.146.10:8080', False), ('ftp://127.0.0.1', False),
        ('https://', False), ('https://entry-hop.invalid:8448/', True),
    ])
    def test_the_bot_url(self, tmp_path, url, ok):
        _cfg, problems = relay.load_config(self.env(tmp_path, BOT_URL=url))
        assert (problems == []) is ok, problems

    def test_a_pin_that_does_not_load(self, tmp_path):
        bad = tmp_path / 'bad.pem'
        bad.write_text('not a certificate')
        for path in (str(bad), str(tmp_path / 'missing.pem')):
            _cfg, problems = relay.load_config(self.env(tmp_path, BOT_CA_FILE=path))
            assert problems and 'BOT_CA_FILE' in problems[0]
        cfg, problems = relay.load_config(self.env(tmp_path, BOT_CA_FILE=''))
        assert problems == [] and cfg['ca_file'] == ''      # the system's CAs

    @pytest.mark.parametrize('raw, want', [('300', 300), ('0', 0), ('900', 900),
                                           ('901', 900), ('86400', 900), ('', 900)])
    def test_the_memory_may_only_be_lowered(self, tmp_path, raw, want):
        cfg, problems = relay.load_config(self.env(tmp_path, HY2_RELAY_MEMORY_S=raw))
        assert problems == [] and cfg['memory_s'] == want

    @pytest.mark.parametrize('raw', ['-5', '1.5', 'abc', '15m'])
    def test_a_memory_that_is_no_number(self, tmp_path, raw):
        _cfg, problems = relay.load_config(self.env(tmp_path, HY2_RELAY_MEMORY_S=raw))
        assert problems and 'HY2_RELAY_MEMORY_S' in problems[0]


# --------------------------------------------------------------- the process --

class TestProcess:

    def run(self, env, timeout=10):
        base = {k: v for k, v in os.environ.items()
                if not (k.startswith('HY2_RELAY_') or k in ('BOT_URL', 'BOT_CA_FILE',
                                                             'DPI_REPORT_TOKEN'))}
        return subprocess.Popen([sys.executable, '-I', str(SCRIPT)], env={**base, **env},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_a_bad_config_does_not_listen(self, tmp_path):
        port = _free_port()
        p = self.run({'HY2_RELAY_LISTEN': f'0.0.0.0:{port}', 'BOT_CA_FILE': ''})
        _out, err = p.communicate(timeout=10)
        assert p.returncode == 2
        assert 'not a loopback address' in err
        with pytest.raises(OSError):
            socket.create_connection(('127.0.0.1', port), timeout=0.5).close()

    def test_it_stops_on_sigterm(self, tmp_path):
        port = _free_port()
        p = self.run({'HY2_RELAY_LISTEN': f'127.0.0.1:{port}', 'BOT_CA_FILE': '',
                      'BOT_URL': f'http://127.0.0.1:{_free_port()}'})
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    socket.create_connection(('127.0.0.1', port), timeout=0.5).close()
                    break
                except OSError:
                    assert time.monotonic() < deadline, p.stderr.read() if p.poll() else ''
                    time.sleep(0.1)
            p.terminate()
            _out, err = p.communicate(timeout=10)
        finally:
            p.kill()
        assert p.returncode == 0, err
        assert 'key list off (no DPI_REPORT_TOKEN)' in err and err.rstrip().endswith('stopped')
