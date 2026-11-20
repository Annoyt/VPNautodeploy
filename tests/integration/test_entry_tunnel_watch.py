"""The healthcheck's watch over the reverse SSH tunnels that end on entry
(docs/insights/64-the-exit-entry-tunnel-users-and-control-apart.md; 2026-11-20).

On 2026-11-19/20 entry's sshd session of the exit → entry tunnel reached its
open-file limit, 1024: 1008 were hy2 sessions through wstunnel that never
closed. New connections on every forwarded port waited in the accept queue,
the bot lost Telegram and nobody was told for ten hours. The script's layer B
now reads each such session (``collect_entry_tunnels``) and pages
(``entry_tunnels``):

* ``entry_tunnel:fd``: a session at half its open-file limit or more;
* ``entry_tunnel:queue``: a forwarded port with connections waiting in both
  reads.

Level 2: the healthcheck script loaded by its path; its ``ss`` and ``/proc``
replaced by fakes in the shapes read on entry on 2026-11-20; the real pager
over real sqlite for the bot's side.
"""

import importlib.util
import os
from pathlib import Path

import pytest

from bot.core.database import Database

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

REPO = Path(__file__).resolve().parents[2]


def _hc():
    path = REPO / 'scripts' / 'protocol_healthcheck.py'
    spec = importlib.util.spec_from_file_location('protocol_healthcheck_tunnel', str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ``ss -Hltnp`` on entry, 2026-11-20 after the split (shortened): the two
# tunnel sessions on loopback, haproxy on docker0 and on every address, the
# docker proxy, the server-first front.
SS_ENTRY = """\
LISTEN 0      4096         0.0.0.0:22022      0.0.0.0:*    users:(("haproxy",pid=1690,fd=7))
LISTEN 0      128          0.0.0.0:2443       0.0.0.0:*    users:(("python3",pid=876,fd=6))
LISTEN 0      4096         0.0.0.0:8448       0.0.0.0:*    users:(("docker-proxy",pid=39126,fd=8))
LISTEN 0      128        127.0.0.1:19422      0.0.0.0:*    users:(("sshd",pid=124720,fd=9))
LISTEN 0      128        127.0.0.1:19441      0.0.0.0:*    users:(("sshd",pid=124720,fd=7))
LISTEN 0      128        127.0.0.1:19442      0.0.0.0:*    users:(("sshd",pid=124720,fd=8))
LISTEN 3      128        127.0.0.1:19460      0.0.0.0:*    users:(("sshd",pid=124720,fd=10))
LISTEN 0      128        127.0.0.1:19423      0.0.0.0:*    users:(("sshd",pid=124778,fd=9))
LISTEN 0      128        127.0.0.1:19426      0.0.0.0:*    users:(("sshd",pid=124778,fd=7))
LISTEN 66     128        127.0.0.1:19888      0.0.0.0:*    users:(("sshd",pid=124778,fd=8))
LISTEN 0      4096      172.17.0.1:19888      0.0.0.0:*    users:(("haproxy",pid=21862,fd=12))
LISTEN 0      128          0.0.0.0:22         0.0.0.0:*    users:(("sshd",pid=3943,fd=3))
LISTEN 0      128             [::]:22            [::]:*    users:(("sshd",pid=3943,fd=4))
"""

LIMITS = """\
Limit                     Soft Limit           Hard Limit           Units
Max cpu time              unlimited            unlimited            seconds
Max open files            {soft}                 1048576              files
Max locked memory         8388608              8388608              bytes
"""


def _proc(tmp_path, sessions):
    """A fake /proc: ``{pid: (open files, soft limit)}``."""
    root = tmp_path / 'proc'
    for pid, (fds, soft) in sessions.items():
        fd = root / str(pid) / 'fd'
        fd.mkdir(parents=True)
        for i in range(fds):
            (fd / str(i)).touch()
        (root / str(pid) / 'limits').write_text(LIMITS.format(soft=soft))
    return str(root)


def _ss(hc, monkeypatch, *answers):
    """``_run`` answers ``ss -Hltnp`` with the given (rc, stdout) in turn and
    records every argv; anything else it is asked is a test failure."""
    calls = []
    queue = list(answers)

    def fake_run(argv, timeout, stdin=None):
        calls.append(list(argv))
        assert argv == ['ss', '-Hltnp'], argv
        rc, out = queue.pop(0)
        return rc, out, '' if rc == 0 else 'ss: cannot open netlink socket'
    monkeypatch.setattr(hc, '_run', fake_run)
    return calls


def _session(pid=1, fds=10, limit=1024, queues=None):
    queues = queues or {19441: [0, 0]}
    return {'pid': pid, 'fds': fds, 'limit': limit,
            'listeners': [{'port': p, 'queue': q} for p, q in queues.items()]}


class TestReaders:

    def test_listeners_of_sshd_on_loopback_only(self):
        hc = _hc()
        got = hc.parse_tunnel_listeners(SS_ENTRY)
        assert got == {124720: {19422: 0, 19441: 0, 19442: 0, 19460: 3},
                       124778: {19423: 0, 19426: 0, 19888: 66}}

    def test_names_addresses_and_junk(self):
        hc = _hc()
        text = (
            'LISTEN 2 128 127.0.0.1:19500 0.0.0.0:* users:(("sshd-session",pid=7,fd=5))\n'
            'LISTEN 1 128 [::1]:19501 [::]:* users:(("sshd",pid=7,fd=6))\n'
            'LISTEN 0 128 127.0.0.53%lo:53 0.0.0.0:* users:(("systemd-resolve",pid=5,fd=14))\n'
            'LISTEN 0 128 127.0.0.1:19502 0.0.0.0:* users:(("ssh",pid=8,fd=4))\n'
            'LISTEN 0 128 127.0.0.1:19503 0.0.0.0:* users:(("haproxy",pid=9,fd=4),("sshd",pid=10,fd=4))\n'
            'ESTAB 0 0 127.0.0.1:19441 127.0.0.1:50000 users:(("sshd",pid=7,fd=9))\n'
            'LISTEN x 128 127.0.0.1:19504 0.0.0.0:* users:(("sshd",pid=7,fd=7))\n'
            'LISTEN 0 128 127.0.0.1:* 0.0.0.0:* users:(("sshd",pid=7,fd=8))\n'
            'LISTEN 0 128 127.0.0.1:19505 0.0.0.0:*\n'
            '\n'
            'garbage\n')
        assert hc.parse_tunnel_listeners(text) == {7: {19500: 2, 19501: 1}, 10: {19503: 0}}
        assert hc.parse_tunnel_listeners('') == {}
        assert hc.parse_tunnel_listeners(None) == {}

    def test_soft_open_files_limit(self):
        hc = _hc()
        assert hc.parse_nofile_soft(LIMITS.format(soft=1024)) == 1024
        assert hc.parse_nofile_soft(LIMITS.format(soft=65536)) == 65536
        assert hc.parse_nofile_soft(LIMITS.format(soft='unlimited')) is None
        assert hc.parse_nofile_soft('Max cpu time unlimited unlimited seconds\n') is None
        assert hc.parse_nofile_soft('') is None
        assert hc.parse_nofile_soft(None) is None


class TestCollector:

    def test_two_reads_paired_per_port(self, tmp_path, monkeypatch):
        hc = _hc()
        later = SS_ENTRY.replace('LISTEN 3      128        127.0.0.1:19460',
                                 'LISTEN 0      128        127.0.0.1:19460') \
                        .replace('LISTEN 66     128        127.0.0.1:19888',
                                 'LISTEN 64     128        127.0.0.1:19888')
        calls = _ss(hc, monkeypatch, (0, SS_ENTRY), (0, later))
        slept = []
        proc = _proc(tmp_path, {124720: (13, 65536), 124778: (11, 1024)})
        got = hc.collect_entry_tunnels(sleep=slept.append, proc=proc)
        assert calls == [['ss', '-Hltnp'], ['ss', '-Hltnp']]
        assert slept == [hc.TUNNEL_QUEUE_GAP_S] and hc.TUNNEL_QUEUE_GAP_S == 2.0
        assert got == [
            {'pid': 124720, 'fds': 13, 'limit': 65536,
             'listeners': [{'port': 19422, 'queue': [0, 0]}, {'port': 19441, 'queue': [0, 0]},
                           {'port': 19442, 'queue': [0, 0]}, {'port': 19460, 'queue': [3, 0]}]},
            {'pid': 124778, 'fds': 11, 'limit': 1024,
             'listeners': [{'port': 19423, 'queue': [0, 0]}, {'port': 19426, 'queue': [0, 0]},
                           {'port': 19888, 'queue': [66, 64]}]},
        ]

    def test_a_session_that_ended_between_the_reads_is_left_out(self, tmp_path, monkeypatch):
        hc = _hc()
        # 124778 reconnected: a new pid holds its ports in the second read
        later = SS_ENTRY.replace('pid=124778', 'pid=200001')
        _ss(hc, monkeypatch, (0, SS_ENTRY), (0, later))
        proc = _proc(tmp_path, {124720: (13, 65536), 124778: (11, 65536), 200001: (5, 65536)})
        got = hc.collect_entry_tunnels(sleep=lambda s: None, proc=proc)
        assert [t['pid'] for t in got] == [124720]

    def test_a_port_in_one_read_only_is_left_out(self, tmp_path, monkeypatch):
        hc = _hc()
        later = '\n'.join(line for line in SS_ENTRY.splitlines() if ':19442 ' not in line)
        _ss(hc, monkeypatch, (0, SS_ENTRY), (0, later))
        proc = _proc(tmp_path, {124720: (13, 65536), 124778: (11, 65536)})
        got = hc.collect_entry_tunnels(sleep=lambda s: None, proc=proc)
        assert [lst['port'] for lst in got[0]['listeners']] == [19422, 19441, 19460]

    def test_a_session_gone_from_proc_is_left_out(self, tmp_path, monkeypatch):
        hc = _hc()
        _ss(hc, monkeypatch, (0, SS_ENTRY), (0, SS_ENTRY))
        proc = _proc(tmp_path, {124720: (13, 65536)})
        got = hc.collect_entry_tunnels(sleep=lambda s: None, proc=proc)
        assert [t['pid'] for t in got] == [124720]

    def test_no_tunnel_here_is_an_empty_list(self, tmp_path, monkeypatch):
        hc = _hc()
        plain = '\n'.join(line for line in SS_ENTRY.splitlines() if '127.0.0.1:19' not in line)
        _ss(hc, monkeypatch, (0, plain), (0, plain))
        assert hc.collect_entry_tunnels(sleep=lambda s: None, proc=str(tmp_path)) == []

    @pytest.mark.parametrize('fail_at', [0, 1])
    def test_ss_that_fails_is_not_looked(self, tmp_path, monkeypatch, fail_at):
        hc = _hc()
        answers = [(0, SS_ENTRY), (0, SS_ENTRY)]
        answers[fail_at] = (1, '')
        _ss(hc, monkeypatch, *answers[:fail_at + 1])
        proc = _proc(tmp_path, {124720: (13, 65536), 124778: (11, 65536)})
        assert hc.collect_entry_tunnels(sleep=lambda s: None, proc=proc) is None

    def test_layer_b_carries_the_tunnels(self, monkeypatch):
        hc = _hc()

        def fake_run(argv, timeout, stdin=None):
            if argv[:2] == ['systemctl', 'is-active']:
                return 0, 'active\n', ''
            if argv[:2] == ['docker', 'inspect']:
                return 0, 'running\n', ''
            if argv[:2] == ['iptables', '-t']:
                return 0, '-P PREROUTING ACCEPT\n', ''
            raise AssertionError(argv)
        monkeypatch.setattr(hc, '_run', fake_run)
        monkeypatch.setattr(hc, 'collect_ssh_relay', lambda: {'port': None, 'state': None})
        seen = [_session()]
        monkeypatch.setattr(hc, 'collect_entry_tunnels', lambda: seen)
        b = hc.collect_layer_b()
        assert b['ok'] is True and b['tunnels'] is seen

        def broken():
            raise RuntimeError('proc went away')
        monkeypatch.setattr(hc, 'collect_entry_tunnels', broken)
        b = hc.collect_layer_b()
        # a reader that crashed is "not looked", and layer B still looked
        assert b['ok'] is True and b['tunnels'] is None


class TestFindings:

    def test_not_looked_keeps_both_keys(self):
        hc = _hc()
        assert hc.TUNNEL_KEYS == ('entry_tunnel:fd', 'entry_tunnel:queue')
        assert hc.entry_tunnels({}) == ([], ['entry_tunnel:fd', 'entry_tunnel:queue'])
        assert hc.entry_tunnels({'tunnels': None}) == ([], ['entry_tunnel:fd', 'entry_tunnel:queue'])
        assert hc.entry_tunnels({'tunnels': 'x'}) == ([], ['entry_tunnel:fd', 'entry_tunnel:queue'])

    def test_no_tunnel_and_a_healthy_one_say_nothing(self):
        hc = _hc()
        assert hc.entry_tunnels({'tunnels': []}) == ([], [])
        assert hc.entry_tunnels({'tunnels': [_session(fds=13, limit=65536)]}) == ([], [])

    def test_half_the_limit_pages_critical(self):
        hc = _hc()
        assert hc.TUNNEL_FD_SHARE == 0.5
        assert hc.entry_tunnels({'tunnels': [_session(fds=511, limit=1024)]}) == ([], [])
        found, unjudged = hc.entry_tunnels({'tunnels': [_session(fds=512, limit=1024)]})
        assert unjudged == [] and len(found) == 1
        f = found[0]
        assert (f['key'], f['layer'], f['severity']) == ('entry_tunnel:fd', 'b', 'critical')
        assert '512 из 1024 файлов (50%' in f['title'] and 'порты :19441' in f['title']
        assert 'перестанет принимать' in f['title']
        assert f['next'] == hc.TUNNEL_FD_NEXT and 'docs/insights/64' in f['next']

    def test_the_night_of_2026_11_20(self):
        hc = _hc()
        night = [_session(pid=21511, fds=1024, limit=1024,
                          queues={19441: [0, 0], 19460: [3, 3], 19888: [66, 64]})]
        found, unjudged = hc.entry_tunnels({'tunnels': night})
        assert unjudged == []
        by_key = {f['key']: f for f in found}
        assert list(by_key) == ['entry_tunnel:fd', 'entry_tunnel:queue']
        fd = by_key['entry_tunnel:fd']
        assert '1024 из 1024 файлов (100%, порты :19441, :19460, :19888)' in fd['title']
        assert 'новые соединения не принимаются' in fd['title']
        q = by_key['entry_tunnel:queue']
        assert q['severity'] == 'critical' and q['layer'] == 'b'
        assert q['title'].endswith('ждут в очереди: :19460 (3), :19888 (66)')
        assert q['next'] == hc.TUNNEL_QUEUE_NEXT and 'docs/insights/64' in q['next']

    def test_the_worst_session_is_named(self):
        hc = _hc()
        users = _session(pid=1, fds=40000, limit=65536, queues={19460: [0, 0]})
        control = _session(pid=2, fds=900, limit=1024, queues={19888: [0, 0]})
        found, _ = hc.entry_tunnels({'tunnels': [users, control]})
        assert len(found) == 1 and '900 из 1024 файлов (87%, порты :19888)' in found[0]['title']
        found, _ = hc.entry_tunnels({'tunnels': [control, users]})
        assert '900 из 1024' in found[0]['title']

    def test_a_session_without_a_known_limit_is_skipped(self):
        hc = _hc()
        for limit in (None, 0, '1024'):
            assert hc.entry_tunnels({'tunnels': [_session(fds=5000, limit=limit)]}) == ([], [])
        assert hc.entry_tunnels({'tunnels': [_session(fds=None, limit=1024)]}) == ([], [])

    @pytest.mark.parametrize('queue', [[0, 5], [5, 0], [0, 0], [5], [5, 5, 5], None, ['5', '5']])
    def test_a_queue_must_stand_in_both_reads(self, queue):
        hc = _hc()
        found, _ = hc.entry_tunnels({'tunnels': [_session(queues={19888: queue})]})
        assert found == []

    def test_queues_of_both_sessions_are_listed(self):
        hc = _hc()
        found, _ = hc.entry_tunnels({'tunnels': [
            _session(pid=1, queues={19441: [1, 2], 19442: [0, 0]}),
            _session(pid=2, queues={19888: [7, 7]})]})
        assert [f['key'] for f in found] == ['entry_tunnel:queue']
        assert found[0]['title'].endswith(':19441 (2), :19888 (7)')


class TestReport:

    def _layers(self, tunnels, b_ok=True):
        b = {'ok': b_ok, 'services': {'shadow-tls': 'active', 'haproxy': 'active'},
             'containers': {}, 'nat_prerouting': [],
             'ssh_relay': {'port': None, 'state': None}, 'tunnels': tunnels}
        return {'a': {'ok': False}, 'b': b, 'c': {'ok': False}}

    def test_findings_and_unjudged_while_layer_b_looked(self):
        hc = _hc()
        full = [_session(fds=1024, limit=1024, queues={19888: [9, 9]})]
        keys = [f['key'] for f in hc.findings_for(self._layers(full))]
        assert 'entry_tunnel:fd' in keys and 'entry_tunnel:queue' in keys
        assert not set(hc.TUNNEL_KEYS) & set(hc.unjudged_for(self._layers(full)))
        assert set(hc.TUNNEL_KEYS) <= set(hc.unjudged_for(self._layers(None)))
        assert not [k for k in hc.findings_for(self._layers(None)) if k['key'] in hc.TUNNEL_KEYS]
        # layer B could not look at all: the bot keeps B's keys by itself
        assert not [f for f in hc.findings_for(self._layers(full, b_ok=False))
                    if f['key'].startswith('entry_tunnel')]
        assert not set(hc.TUNNEL_KEYS) & set(hc.unjudged_for(self._layers(None, b_ok=False)))

    def test_the_human_report_lists_it(self):
        hc = _hc()
        layers = self._layers([_session(fds=1024, limit=1024)])
        report = hc.assess(layers)
        assert [f['key'] for f in report['findings'] if f['key'].startswith('entry_tunnel')] == [
            'entry_tunnel:fd']
        assert 'entry_tunnel:fd: туннель exit → entry занял 1024 из 1024' in hc.render_human(report)

    def test_the_section_is_there(self):
        doc = REPO / 'docs' / 'insights' / '64-the-exit-entry-tunnel-users-and-control-apart.md'
        text = doc.read_text()
        assert 'entry_tunnel:fd' in text and 'entry_tunnel:queue' in text
        assert '§64' in (REPO / 'AGENTS.md').read_text()


class TestTheBot:

    FD = {'key': 'entry_tunnel:fd', 'severity': 'critical', 'layer': 'b',
          'title': 'туннель exit → entry занял 1024 из 1024 файлов (100%, порты :19888)',
          'next': 'ss -Hltnp'}

    def test_it_reaches_telegram(self, tmp_path):
        from .test_adviser import _manager, _report, _texts
        db = Database(str(tmp_path / 'bot.db'))
        mgr = _manager(db)
        _report(db, findings=[self.FD])
        mgr.run_once()
        assert mgr._state['healthcheck:entry_tunnel:fd'].open
        assert any('занял 1024 из 1024 файлов' in t for t in _texts(mgr))

    def test_kept_while_entry_could_not_look(self, tmp_path):
        """The key is entry's (B): with entry unreadable the alert stays open;
        with exit unreadable it is judged — the tunnel's end is on entry."""
        from bot.services.alert_manager import RECOVERY_TICKS
        from .test_adviser import _judged, _manager, _report, _texts
        db = Database(str(tmp_path / 'bot.db'))
        mgr = _manager(db)
        _report(db, findings=[self.FD])
        mgr.run_once()
        no_entry = {'a': 'ok', 'b': 'FAILED: iptables: timed out', 'c': 'ok'}
        _report(db, minutes_ago=0, findings=[], layers=no_entry)
        for _ in range(RECOVERY_TICKS + 1):
            mgr.run_once()
        assert not any('Восстановилось' in t for t in _texts(mgr))
        assert mgr._state['healthcheck:entry_tunnel:fd'].open
        assert not _judged(db, 'healthcheck:entry_tunnel:fd')
        assert not _judged(db, 'healthcheck:entry_tunnel:queue')
        no_exit = {'a': 'ok', 'b': 'ok', 'c': 'FAILED: ssh: timed out'}
        _report(db, minutes_ago=0, findings=[], layers=no_exit)
        assert _judged(db, 'healthcheck:entry_tunnel:fd')
        assert _judged(db, 'healthcheck:entry_tunnel:queue')
