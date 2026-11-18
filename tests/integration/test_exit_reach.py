"""§61: a new connection from entry to exit every minute, directly and
through each transit (bot/services/exit_reach.py, AlertManager
``check_exit_reach``).

Pinned: the connection is haproxy's (TCP, PROXY v2 naming the TEST-NET
marker, TLS with Reality's SNI), each way it can fail has its own words, the
verdict tells a block from a dead exit and a broken transit, the alert pages
once per incident and posts its recovery, a restart does not page again, and
the marker never reaches the DPI rows.
"""

import errno
import ipaddress
import socket
import sqlite3
import struct
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot.core.database import Database
from bot.services import exit_reach as er
from bot.services import hop_tls
from bot.services.alert_manager import (
    REPEAT_COOLDOWN_S, RECOVERY_TICKS, Alert, AlertManager, build_default_checks,
)

HEADER_LEN = 16 + 12
EXIT = '84.75.76.109'
DE = '212.60.153.208'


@pytest.fixture(autouse=True)
def _fresh_snapshot():
    er._reset_for_tests()
    yield
    er._reset_for_tests()


@pytest.fixture(scope='module')
def pair(tmp_path_factory):
    d = tmp_path_factory.mktemp('reach-tls')
    cert = hop_tls.generate(str(d / 'tls'), '127.0.0.1')
    return cert, str(Path(cert).with_name(hop_tls.KEY_FILE))


def _read_exact(conn, n):
    buf = b''
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


class Server:
    """A listener on 127.0.0.1 that reads the PROXY header, then:
    'tls' completes a TLS handshake, 'silent' says nothing, 'close' hangs
    up, 'garbage' answers with plain HTTP."""

    def __init__(self, mode, pair=None):
        self.mode = mode
        self.headers, self.snis = [], []
        self.ctx = None
        if mode == 'tls':
            self.ctx = hop_tls.server_context(*pair)
            self.ctx.sni_callback = lambda obj, name, ctx: self.snis.append(name)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(5)
            self.headers.append(_read_exact(conn, HEADER_LEN))
            if self.mode == 'tls':
                tls = self.ctx.wrap_socket(conn, server_side=True)
                tls.close()
            elif self.mode == 'silent':
                time.sleep(2)
            elif self.mode == 'garbage':
                conn.sendall(b'HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n')
                time.sleep(0.5)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.sock.close()


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def servers(pair):
    made = []

    def make(mode):
        s = Server(mode, pair)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def target(port, *, via=False, name='exit', sni='www.bing.com', host='127.0.0.1'):
    key = er.VIA_PREFIX + name if via else er.EXIT_KEY
    return er.Target(key, name, host, port, sni, via)


# ============================================================ the header ==

class TestProxyHeader:

    def test_the_bytes(self):
        h = er.proxy_header()
        assert h[:12] == b'\r\n\r\n\x00\r\nQUIT\n'
        assert h[12] == 0x21                        # version 2, command PROXY
        assert h[13] == 0x11                        # TCP over IPv4
        assert struct.unpack('!H', h[14:16]) == (12,)
        assert socket.inet_ntoa(h[16:20]) == er.MARK_SRC
        assert socket.inet_ntoa(h[20:24]) == er.MARK_DST
        assert struct.unpack('!HH', h[24:28]) == (er.MARK_SPORT, er.EXIT_PORT)
        assert len(h) == HEADER_LEN

    def test_the_marker_is_never_a_client(self):
        ip = ipaddress.ip_address(er.MARK_SRC)
        assert ip in ipaddress.ip_network('192.0.2.0/24')      # RFC 5737
        assert not ip.is_global


# ============================================================ the config ==

class TestParseTransits:

    def test_entries(self):
        assert er.parse_transits('de=212.60.153.208:9441, lax=108.186.129.226:443') == (
            [('de', DE, 9441), ('lax', '108.186.129.226', 443)], [])

    def test_ipv6_in_brackets(self):
        assert er.parse_transits('v6=[2a0c:4ac1:15:6b::a]:443') == (
            [('v6', '2a0c:4ac1:15:6b::a', 443)], [])

    @pytest.mark.parametrize('raw', [None, '', ' , ', 7])
    def test_nothing(self, raw):
        assert er.parse_transits(raw) == ([], [])

    @pytest.mark.parametrize('item', [
        'de',                               # no address
        'DE_1=1.2.3.4:5',                   # underscore
        '=1.2.3.4:5',                       # no name
        'averyveryverylongname=1.2.3.4:5',  # 21 chars
        'de=host.example:443',              # a name needs a lookup
        'de=1.2.3.4',                       # no port
        'de=1.2.3.4:0',
        'de=1.2.3.4:65536',
        'de=1.2.3.4:x',
        'de=2a0c::1:443',                   # IPv6 without brackets
        'de=[1.2.3.4]:443',                 # IPv4 in brackets
    ])
    def test_a_bad_entry_is_left_out(self, item):
        entries, problems = er.parse_transits(f'{item},ok=5.6.7.8:9')
        assert entries == [('ok', '5.6.7.8', 9)]
        assert len(problems) == 1 and repr(item) in problems[0]

    def test_a_repeated_name_keeps_the_first(self):
        entries, problems = er.parse_transits('de=1.2.3.4:5,DE=5.6.7.8:9')
        assert entries == [('de', '1.2.3.4', 5)] and 'уже есть' in problems[0]

    def test_case_and_spaces(self):
        assert er.parse_transits('  De = 1.2.3.4:5  ')[0] == [('de', '1.2.3.4', 5)]

    def test_the_edges(self):
        assert er.parse_transits('a234567890123456=1.2.3.4:1,b=1.2.3.4:65535')[0] == [
            ('a234567890123456', '1.2.3.4', 1), ('b', '1.2.3.4', 65535)]


class TestTargets:

    def cfg(self, **kw):
        base = dict(EXIT_NODE_IP=EXIT, SNI_VALUE=' www.bing.com ',
                    EXIT_TRANSITS=f'de={DE}:9441')
        base.update(kw)
        return SimpleNamespace(**base)

    def test_exit_and_the_transit(self):
        assert er.targets(self.cfg()) == [
            er.Target('reach:exit', 'exit', EXIT, 443, 'www.bing.com', False),
            er.Target('reach:via:de', 'de', DE, 9441, 'www.bing.com', True),
        ]

    def test_a_mock_config_watches_nothing(self):
        assert er.targets(Mock()) == []

    def test_exit_without_an_ip_literal(self):
        assert [t.key for t in er.targets(self.cfg(EXIT_NODE_IP='exit.example'))] == [
            'reach:via:de']

    def test_no_sni(self):
        assert {t.sni for t in er.targets(self.cfg(SNI_VALUE=''))} == {''}

    def test_a_problem_is_logged_once(self, caplog):
        cfg = self.cfg(EXIT_TRANSITS='bad,de=1.2.3.4:5')
        er._LOGGED_PROBLEMS.discard("'bad': имя — латиница, цифры, дефис, до 16 знаков")
        with caplog.at_level('WARNING', logger='bot.services.exit_reach'):
            for _ in range(3):
                assert [t.name for t in er.targets(cfg)] == ['exit', 'de']
        assert sum("'bad'" in r.getMessage() for r in caplog.records) == 1

    def test_settings_and_compose_carry_it(self, monkeypatch):
        from bot.config.settings import Settings
        monkeypatch.setenv('EXIT_TRANSITS', ' de=212.60.153.208:9441 ')
        assert Settings().EXIT_TRANSITS == 'de=212.60.153.208:9441'
        monkeypatch.delenv('EXIT_TRANSITS')
        assert Settings().EXIT_TRANSITS == ''
        compose = (Path(__file__).resolve().parents[2] / 'docker-compose.yml').read_text()
        assert '- EXIT_TRANSITS=${EXIT_TRANSITS:-}' in compose


# ======================================================== one connection ==

class TestProbe:

    def test_a_handshake(self, servers):
        s = servers('tls')
        r = er.probe(target(s.port))
        assert r.ok and r.stage == 'ok' and r.error == ''
        assert s.headers == [er.proxy_header()]
        assert s.snis == ['www.bing.com']

    def test_no_sni(self, servers):
        s = servers('tls')
        assert er.probe(target(s.port, sni='')).ok
        assert s.snis == [None]

    def test_refused(self):
        r = er.probe(target(closed_port()))
        assert (r.ok, r.stage, r.error) == (False, 'tcp', 'refused')

    def test_no_answer_to_the_syn(self, monkeypatch):
        seen = {}

        def hang(addr, timeout):
            seen['timeout'] = timeout
            raise socket.timeout('timed out')
        monkeypatch.setattr(er.socket, 'create_connection', hang)
        monkeypatch.setattr(er, 'CONNECT_TIMEOUT_S', 1.5)
        r = er.probe(target(443))
        assert (r.ok, r.stage, r.error) == (False, 'tcp', 'timeout')
        assert seen['timeout'] == 1.5

    def test_tcp_then_silence(self, servers, monkeypatch):
        monkeypatch.setattr(er, 'HANDSHAKE_TIMEOUT_S', 0.3)
        s = servers('silent')
        r = er.probe(target(s.port))
        assert (r.ok, r.stage, r.error) == (False, 'tls', 'timeout')
        assert 300 <= r.ms < 1500
        assert s.headers == [er.proxy_header()]

    def test_hung_up_after_the_header(self, servers):
        r = er.probe(target(servers('close').port))
        assert not r.ok and r.stage == 'tls' and r.error != 'timeout'

    def test_not_tls(self, servers):
        r = er.probe(target(servers('garbage').port))
        assert not r.ok and r.stage == 'tls' and r.error.startswith('tls: ')

    @pytest.mark.parametrize('exc, why', [
        (ConnectionResetError(), 'reset'),
        (OSError(errno.ENETUNREACH, 'Network is unreachable'), 'unreachable'),
        (OSError(errno.EHOSTUNREACH, 'No route to host'), 'unreachable'),
        (PermissionError(errno.EPERM, 'Operation not permitted'),
         'PermissionError: [Errno 1] Operation not permitted'),
    ])
    def test_reasons(self, exc, why):
        assert er._why(exc) == why

    def test_every_target_at_once(self, servers, monkeypatch):
        monkeypatch.setattr(er, 'HANDSHAKE_TIMEOUT_S', 0.5)
        a, b, ok = servers('silent'), servers('silent'), servers('tls')
        t0 = time.monotonic()
        results = er.probe_all([target(a.port), target(b.port, via=True, name='a'),
                                target(ok.port, via=True, name='b')])
        assert time.monotonic() - t0 < 0.95
        assert [r.ok for r in results.values()] == [False, False, True]
        assert list(results) == ['reach:exit', 'reach:via:a', 'reach:via:b']

    def test_nothing_to_probe(self):
        assert er.probe_all([]) == {}


# =============================================================== verdict ==

X = er.Target('reach:exit', 'exit', EXIT, 443, 'www.bing.com', False)
V = er.Target('reach:via:de', 'de', DE, 9441, 'www.bing.com', True)
OK = er.Result(True, 'ok', 107)
TCP_DEAD = er.Result(False, 'tcp', 4000, 'timeout')
TLS_SILENT = er.Result(False, 'tls', 4002, 'timeout')


class TestVerdict:

    def test_all_well(self):
        assert er.verdict([X, V], {X.key: OK, V.key: OK}, 3) == []

    def test_the_block_of_11_18(self):
        (a,) = er.verdict([X, V], {X.key: TCP_DEAD, V.key: OK}, 3)
        assert a['key'] == 'reach:exit' and a['severity'] == 'critical'
        assert a['title'] == 'Адрес exit 84.75.76.109 не принимает соединения из России'
        assert a['detail'].split('\n') == [
            'Напрямую (84.75.76.109:443): TCP не соединяется: 4 с без ответа.',
            'Через de (212.60.153.208:9441): проходит, 107 мс.',
            'exit отчитался 3 мин назад: связь exit → entry есть.',
            'exit жив: через транзит проходит. Так выглядела блокировка адреса 18.11. '
            'Если трафик ещё идёт напрямую, переключи его на транзит: AGENTS.md §59.',
        ]

    def test_one_working_transit_of_two_is_enough(self):
        w = er.Target('reach:via:w', 'w', '5.6.7.8', 443, '', True)
        out = er.verdict([X, V, w], {X.key: TCP_DEAD, V.key: TCP_DEAD, w.key: OK}, None)
        assert out[0]['title'] == 'Адрес exit 84.75.76.109 не принимает соединения из России'
        assert [a['key'] for a in out] == ['reach:exit', 'reach:via:de']

    def test_a_working_transit_proves_exit_alive_without_reports(self):
        (a,) = er.verdict([X, V], {X.key: TCP_DEAD, V.key: OK}, None)
        assert a['title'] == 'Адрес exit 84.75.76.109 не принимает соединения из России'
        assert 'Отчётов от exit нет (или их не прочитать).' in a['detail']

    def test_blocked_and_the_transit_broken_exit_reporting(self):
        a, v = er.verdict([X, V], {X.key: TCP_DEAD, V.key: TLS_SILENT}, 4)
        assert a['title'] == 'Адрес exit 84.75.76.109 не принимает соединения из России, exit жив'
        assert a['detail'].endswith('Через транзит тоже не пройти: проверь транзитную ноду. '
                                    'Пока она лежит, у пользователей нет пути к exit.')
        assert 'Через de (212.60.153.208:9441): TCP есть, TLS молчит 4 с.' in a['detail']
        assert v['key'] == 'reach:via:de' and v['severity'] == 'critical'
        assert v['title'] == 'Транзит de (212.60.153.208:9441) к exit не проходит'
        assert v['detail'].endswith(
            'Прямой путь тоже закрыт: если трафик идёт через этот транзит, у пользователей '
            'нет связи. На ноде de: systemctl status nekovpn-transit; '
            'iptables -t nat -S | grep nekovpn-transit.')

    def test_blocked_without_a_transit(self):
        (a,) = er.verdict([X], {X.key: TCP_DEAD}, 2)
        assert a['title'].endswith(', exit жив')
        assert 'EXIT_TRANSITS не задан' in a['detail']

    def test_exit_down(self):
        a, _v = er.verdict([X, V], {X.key: TCP_DEAD, V.key: TCP_DEAD}, 47)
        assert a['title'] == 'exit 84.75.76.109 не отвечает: ни соединений из России, ни отчётов'
        assert 'Отчётов от exit нет 47 мин.' in a['detail']

    def test_exit_down_without_a_transit_or_reports(self):
        (a,) = er.verdict([X], {X.key: TCP_DEAD}, None)
        assert a['title'].startswith('exit 84.75.76.109 не отвечает')

    @pytest.mark.parametrize('age, fresh', [(14.9, True), (15, False), (0, True)])
    def test_how_fresh_a_report_is(self, age, fresh):
        (a,) = er.verdict([X], {X.key: TCP_DEAD}, age)
        assert a['title'].endswith(', exit жив') is fresh
        assert ('exit отчитался' in a['detail']) is fresh
        assert er.report_is_fresh(age) is fresh

    def test_a_broken_spare(self):
        (v,) = er.verdict([X, V], {X.key: OK, V.key: TCP_DEAD}, 3)
        assert v['severity'] == 'warn'
        assert 'Прямой путь работает: сломан запасной.' in v['detail']

    def test_a_transit_without_a_direct_check(self):
        (v,) = er.verdict([V], {V.key: TCP_DEAD}, 3)
        assert v['severity'] == 'warn' and 'EXIT_NODE_IP не задан' in v['detail']

    def test_a_target_without_a_result_says_nothing(self):
        assert er.verdict([X, V], {V.key: OK}, 3) == []
        (v,) = er.verdict([X, V], {V.key: TCP_DEAD}, 3)
        assert v['severity'] == 'warn' and 'Напрямую' not in v['detail']

    @pytest.mark.parametrize('result, words', [
        (er.Result(False, 'tcp', 4000, 'refused'), 'TCP: отказ в соединении (порт закрыт)'),
        (er.Result(False, 'tcp', 12, 'reset'), 'TCP: соединение сброшено'),
        (er.Result(False, 'tcp', 3, 'unreachable'), 'TCP: нет маршрута'),
        (er.Result(False, 'tcp', 3, 'OSError: x'), 'TCP: OSError: x'),
        (er.Result(False, 'tls', 4500, 'timeout'), 'TCP есть, TLS молчит 4.5 с'),
        (er.Result(False, 'tls', 9, 'tls: WRONG_VERSION_NUMBER'),
         'TCP есть, TLS не завершился (tls: WRONG_VERSION_NUMBER)'),
        (er.Result(True, 'ok', 48), 'проходит, 48 мс'),
    ])
    def test_words(self, result, words):
        assert er.describe(X, result) == words

    @pytest.mark.parametrize('mins, words', [
        (3.7, 'exit отчитался 3 мин назад: связь exit → entry есть.'),
        (95, 'Отчётов от exit нет 1 ч 35 мин.'),
        (60 * 26, 'Отчётов от exit нет 1 д 2 ч.'),
    ])
    def test_report_words(self, mins, words):
        assert er.report_line(mins) == words


# ========================================================== through ticks ==

def put_report(db_path, minutes_ago):
    ts = (datetime.utcnow() - timedelta(minutes=minutes_ago)).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO exit_reports (ts, rows) VALUES (?, 0)", (ts,))


@pytest.fixture
def stand(tmp_path, servers, monkeypatch):
    """exit = whatever port EXIT_PORT names (closed at first), the transit
    'de' = a TLS server; short timeouts; a fresh report from exit."""
    db_path = str(tmp_path / 'bot.db')
    db = Database(db_path)
    tls = servers('tls')
    monkeypatch.setattr(er, 'EXIT_PORT', closed_port())
    cfg = Mock()
    cfg.DB_PATH = db_path
    cfg.TOPIC_AI = 55
    cfg.FORUM_GROUP_ID = -100123
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.ALERT_TG_MIN_SEVERITY = 'critical'
    cfg.EXIT_NODE_IP = '127.0.0.1'
    cfg.SNI_VALUE = 'www.bing.com'
    cfg.EXIT_TRANSITS = f'de=127.0.0.1:{tls.port}'
    put_report(db_path, 2)
    check = next(c for c in build_default_checks(cfg, Mock())
                 if c.__name__ == 'check_exit_reach')
    return SimpleNamespace(db=db, cfg=cfg, tls=tls, check=check)


def manager(stand):
    m = AlertManager(Mock(), stand.cfg, db=stand.db)
    m.register(stand.check)
    return m


def texts(m):
    return [c.kwargs['text'] for c in m.bot.send_message.call_args_list]


class TestThroughTheTick:

    def test_the_check(self, stand):
        prefix, (a,) = stand.check()
        assert prefix == 'reach:'
        assert (a.key, a.severity, a.min_cycles, a.repeat) == ('reach:exit', 'critical', 2, False)
        assert a.title == 'Адрес exit 127.0.0.1 не принимает соединения из России'
        assert 'Напрямую (127.0.0.1:' in a.detail and ': TCP: отказ в соединении' in a.detail
        assert f'Через de (127.0.0.1:{stand.tls.port}): проходит' in a.detail
        assert 'exit отчитался 2 мин назад' in a.detail

    def test_pages_once_then_posts_the_recovery(self, stand, monkeypatch):
        m = manager(stand)
        m.run_once()
        assert texts(m) == []                                   # min_cycles 2
        m.run_once()
        (page,) = texts(m)
        assert 'Адрес exit 127.0.0.1 не принимает соединения из России' in page
        assert m.bot.send_message.call_args.kwargs['message_thread_id'] == 55
        for _ in range(3):
            m.run_once()
        m._state['reach:exit'].last_fired_ts -= REPEAT_COOLDOWN_S + 60
        m.run_once()
        assert len(texts(m)) == 1                               # once per incident
        monkeypatch.setattr(er, 'EXIT_PORT', stand.tls.port)    # the block lifts
        for _ in range(RECOVERY_TICKS):
            m.run_once()
        assert len(texts(m)) == 2
        assert texts(m)[1].startswith(
            '✅ <b>Восстановилось:</b> Адрес exit 127.0.0.1 не принимает соединения из России')
        monkeypatch.setattr(er, 'EXIT_PORT', closed_port())     # and comes back
        m.run_once()
        m.run_once()
        assert len(texts(m)) == 3 and 'не принимает соединения' in texts(m)[2]

    def test_a_restart_does_not_page_again(self, stand):
        m = manager(stand)
        m.run_once()
        m.run_once()
        assert len(texts(m)) == 1
        again = manager(stand)
        for _ in range(3):
            again.run_once()
        assert texts(again) == []

    def test_a_broken_spare_stays_on_the_dashboard(self, stand, monkeypatch):
        monkeypatch.setattr(er, 'EXIT_PORT', stand.tls.port)
        stand.cfg.EXIT_TRANSITS = f'de=127.0.0.1:{closed_port()}'
        m = manager(stand)
        m.run_once()
        m.run_once()
        assert texts(m) == []
        with sqlite3.connect(stand.cfg.DB_PATH) as conn:
            rows = conn.execute("SELECT key, severity FROM alert_history").fetchall()
        assert rows == [('reach:via:de', 'warn')]

    def test_nothing_to_watch_opens_no_socket(self, stand, monkeypatch):
        stand.cfg.EXIT_NODE_IP = Mock()
        stand.cfg.EXIT_TRANSITS = Mock()
        monkeypatch.setattr(er, 'probe', Mock(side_effect=AssertionError('no probe')))
        assert stand.check() == ('reach:', [])

    def test_the_newest_report_counts(self, stand):
        put_report(stand.cfg.DB_PATH, 300)
        _prefix, (a,) = stand.check()
        assert 'exit отчитался 2 мин назад' in a.detail

    def test_no_report_and_a_dead_transit_reads_as_exit_down(self, stand):
        with sqlite3.connect(stand.cfg.DB_PATH) as conn:
            conn.execute("DELETE FROM exit_reports")
        stand.cfg.EXIT_TRANSITS = f'de=127.0.0.1:{closed_port()}'
        _prefix, alerts = stand.check()
        assert alerts[0].title == 'exit 127.0.0.1 не отвечает: ни соединений из России, ни отчётов'

    def test_an_unreadable_database_is_no_report(self, stand, tmp_path):
        bad = tmp_path / 'junk.db'
        bad.write_bytes(b'not a database' * 100)
        stand.cfg.DB_PATH = str(bad)
        stand.cfg.EXIT_TRANSITS = ''
        _prefix, (a,) = stand.check()
        assert 'Отчётов от exit нет (или их не прочитать).' in a.detail

    def test_the_protocols_line(self, stand):
        stand.check()
        line = er.reach_line(er.snapshot())
        assert line.startswith('🛣 Пути к exit (0 с назад): ❌ напрямую: TCP: отказ в соединении')
        assert ' · ✅ через de: проходит, ' in line

    def test_the_protocols_card_carries_it(self, stand):
        from bot.handlers.admin.ops import AdminOpsMixin
        stand.check()
        h = AdminOpsMixin.__new__(AdminOpsMixin)
        h.db, h.config = stand.db, stand.cfg
        h._panel_audit_line = lambda: 'audit: skipped'
        h._recent_alert_lines = lambda now: []
        assert '🛣 Пути к exit (' in h._build_protocols_report()


class TestReachLine:

    def test_none_without_a_check(self):
        assert er.reach_line(None) is None
        assert er.reach_line({}) is None

    def test_none_when_old(self):
        snap = {'ts': 1000.0, 'targets': [X], 'results': {X.key: OK}}
        assert er.reach_line(snap, now=1000.0 + er.SNAPSHOT_FRESH_S + 1) is None
        assert er.reach_line(snap, now=1000.0 + er.SNAPSHOT_FRESH_S) == (
            f'🛣 Пути к exit ({er.SNAPSHOT_FRESH_S} с назад): ✅ напрямую: проходит, 107 мс')

    def test_a_target_without_a_result(self):
        assert er.reach_line({'ts': 5.0, 'targets': [X], 'results': {}}, now=5.0) is None


class TestRecordLogsChanges:

    def test_only_a_change_is_logged(self, caplog):
        with caplog.at_level('INFO', logger='bot.services.exit_reach'):
            er.record([X], {X.key: OK})
            er.record([X], {X.key: OK})
            er.record([X], {X.key: TCP_DEAD})
            er.record([X], {X.key: TCP_DEAD})
        msgs = [r.getMessage() for r in caplog.records]
        assert msgs == [f'reach exit {EXIT}:443: проходит, 107 мс',
                        f'reach exit {EXIT}:443: tcp timeout (4000 мс)']


# ===================================================== the alert manager ==

class TestRepeat:
    """An ordinary alert still repeats after the cooldown; repeat=False does
    not while its key is open."""

    def _tick(self, repeat):
        cfg = Mock()
        cfg.TOPIC_AI, cfg.FORUM_GROUP_ID, cfg.ALERT_TG_MIN_SEVERITY = 55, -1, 'critical'
        m = AlertManager(Mock(), cfg)
        m.register(lambda: Alert('k', 'critical', 't', min_cycles=1, repeat=repeat))
        m.run_once()
        m._state['k'].last_fired_ts -= REPEAT_COOLDOWN_S + 1
        m.run_once()
        return m.bot.send_message.call_count

    def test_repeats_by_default(self):
        assert Alert('k', 'warn', 't').repeat is True
        assert self._tick(True) == 2

    def test_once(self):
        assert self._tick(False) == 1


# ======================================================= the DPI rows ==

class TestTheMarkerNeverReachesTheDpiRows:

    REAL = '198.51.100.77'

    def test_rejects_and_access(self, tmp_path, monkeypatch):
        from bot.core.web_server import WebAppServer
        from bot.services import geoip
        monkeypatch.setattr(geoip, 'lookup', lambda ip: ('RU', ''))
        monkeypatch.setattr(geoip, 'lookup_asn', lambda ip: ('AS31133', 'org'))
        cfg = SimpleNamespace(ENTRY_NODE_IP='203.0.113.20', EXIT_NODE_IP='203.0.113.30',
                              HY2_HOST='', FALLBACK_NODE_HOST='', BOT_TOKEN='t',
                              WEBAPP_URL='', SUB_PUBLIC_URL='')
        srv = WebAppServer(cfg, Database(str(tmp_path / 'bot.db')), xui_service=Mock())
        rows = srv._dpi_exit_rows({
            'rejects': [{'kind': 'reality', 'reason': 'x', 'count': 7,
                         'ips': {er.MARK_SRC: 5, self.REAL: 2}}],
            'access': [{'tag': 'inbound-443', 'conns': 4,
                        'ips': {er.MARK_SRC: 1, self.REAL: 3}}],
        })
        # one bucket, the real client's; nothing left over for the geo-less
        # remainder either (the marker counts as seen, not as unattributed)
        assert [(r[1], r[2], r[4], r[5], r[9]) for r in rows] == [
            ('RU', 'AS31133', 'reality', 3, 2)]
        assert er.MARK_SRC not in (rows[0][10] or '')
