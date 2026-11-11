"""Tests for scripts/exit_dpi_reporter.py — the exit-side xray log
summariser that feeds POST /api/dpi/exit_report.

The parsing regexes are pinned to REAL log lines captured on the exit
host 2026-08-19; if xray changes its log wording these tests are the
early warning that the DPI feed silently went dark.
"""

import importlib.util
import json
import os
import types

import pytest

REPORTER_PATH = os.path.join(
    os.path.dirname(__file__), '..', '..', 'scripts', 'exit_dpi_reporter.py'
)

_spec = importlib.util.spec_from_file_location(
    'exit_dpi_reporter', os.path.abspath(REPORTER_PATH)
)
reporter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reporter)


ACCESS_LINES = [
    # real user via the entry tunnel (cf-ws inbound)
    "2026/08/19 21:34:53.798678 from 130.49.146.10:10611 accepted "
    "udp:172.217.115.4:443 [inbound-2053 >> direct] "
    "email: user_nia1967nia_208560413@nekovo.ru",
    # our own health probe — must be excluded
    "2026/08/19 21:34:36.667557 from 130.49.146.10:46280 accepted "
    "www.youtube.com:443 [inbound-8444 >> direct] "
    "email: probe_monitor@nekovo.ru",
    # panel stats polling over the api inbound — must be excluded
    "2026/08/19 21:34:59.015915 from 127.0.0.1:38850 accepted "
    "tcp:127.0.0.1:62789 [api -> api]",
    # direct hit with a real source IP
    "2026/08/19 21:35:00.000000 from 203.0.113.7:1234 accepted "
    "tcp:example.com:443 [inbound-8444 >> direct] "
    "email: user_direct_42@nekovo.ru",
]

REJECT_LINES = [
    "2026/08/19 19:15:38.794998 [Info] transport/internet/tcp: REALITY: "
    "processed invalid connection from 45.156.128.134:53070: "
    "failed to read client hello",
    "2026/08/19 20:29:43.304042 [Info] transport/internet/tcp: REALITY: "
    "processed invalid connection from 69.5.169.142:3492: "
    "server name mismatch: ",
    "2026/08/19 21:29:01.113771 [Info] [3478817167] app/proxyman/inbound: "
    "connection ends > shadowsocks: serve TCP from 118.193.72.187:52756: "
    "invalid request",
    "2026/08/19 21:36:26.623339 [Info] transport/internet/httpupgrade: "
    "failed to handle request > read tcp "
    "172.20.0.2:2053->66.132.186.184:44204: read: connection reset by peer",
    "2026/08/19 21:38:36.290809 [Info] transport/internet/httpupgrade: "
    "failed to handle request > transport/internet/httpupgrade: "
    "bad path: /sitemap.xml",
]


class TestParsePresence:
    """parse_presence — per-user "which inbound are you on right now".

    This is the only feed that can answer it: the panel keys
    client_traffics by email alone, so its lastOnline/up/down are
    shared across every inbound a user exists on.
    """

    def test_maps_email_to_its_inbound_tag(self):
        out = {p['email']: p['tag'] for p in reporter.parse_presence(ACCESS_LINES)}
        assert out['user_nia1967nia_208560413@nekovo.ru'] == 'inbound-2053'
        assert out['user_direct_42@nekovo.ru'] == 'inbound-8444'

    def test_probe_and_api_traffic_excluded(self):
        emails = {p['email'] for p in reporter.parse_presence(ACCESS_LINES)}
        assert not any(e.startswith('probe') for e in emails)
        # the api line carries no email at all
        assert all(e for e in emails)

    def test_latest_tag_wins_when_user_switches_inbound(self):
        """A user moving transports must report the NEW one — that's the
        whole point of the column."""
        lines = [
            "2026/08/19 21:00:00.000000 from 130.49.146.10:1 accepted "
            "tcp:example.com:443 [inbound-2053 >> direct] email: u@x",
            "2026/08/19 21:00:05.000000 from 130.49.146.10:2 accepted "
            "tcp:example.com:443 [inbound-443 >> direct] email: u@x",
        ]
        out = reporter.parse_presence(lines)
        assert len(out) == 1
        assert out[0]['tag'] == 'inbound-443'
        # count restarts on the switch rather than carrying the old tag's
        assert out[0]['conns'] == 1

    def test_conns_accumulate_on_the_same_inbound(self):
        lines = [
            "2026/08/19 21:00:0%d.000000 from 130.49.146.10:%d accepted "
            "tcp:example.com:443 [inbound-2053 >> direct] email: u@x" % (i, i)
            for i in range(3)
        ]
        out = reporter.parse_presence(lines)
        assert out[0]['conns'] == 3

    def test_empty_input_is_empty_output(self):
        assert reporter.parse_presence([]) == []


class TestParseAccess:
    def test_aggregates_per_inbound_tag(self):
        out = {a['tag']: a for a in reporter.parse_access(ACCESS_LINES)}
        assert set(out) == {'inbound-2053', 'inbound-8444'}
        assert out['inbound-2053']['conns'] == 1
        assert out['inbound-2053']['ips'] == {'130.49.146.10': 1}
        assert out['inbound-2053']['uniq_emails'] == 1

    def test_probe_and_api_traffic_excluded(self):
        out = {a['tag']: a for a in reporter.parse_access(ACCESS_LINES)}
        # probe_monitor's ss2022 hit is dropped; only the direct user stays
        assert out['inbound-8444']['conns'] == 1
        assert out['inbound-8444']['ips'] == {'203.0.113.7': 1}
        assert 'api' not in out

    def test_ip_counts_accumulate(self):
        lines = [ACCESS_LINES[0]] * 3
        (bucket,) = reporter.parse_access(lines)
        assert bucket['conns'] == 3
        assert bucket['ips'] == {'130.49.146.10': 3}


class TestParseRejects:
    def test_reality_probe_with_reason(self):
        out = {
            (r['kind'], r['reason']): r
            for r in reporter.parse_rejects(REJECT_LINES)
        }
        hello = out[('reality', 'failed to read client hello')]
        assert hello['count'] == 1
        assert hello['ips'] == {'45.156.128.134': 1}
        # trailing ": " of "server name mismatch: " is stripped
        assert ('reality', 'server name mismatch') in out

    def test_shadowsocks_scanner(self):
        out = {
            (r['kind'], r['reason']): r
            for r in reporter.parse_rejects(REJECT_LINES)
        }
        ss = out[('ss2022', 'invalid request')]
        assert ss['ips'] == {'118.193.72.187': 1}

    def test_httpupgrade_reasons_normalised(self):
        out = {
            (r['kind'], r['reason']): r
            for r in reporter.parse_rejects(REJECT_LINES)
        }
        reset = out[
            ('ws-upgrade', 'read tcp <peer>: read: connection reset by peer')
        ]
        # peer address is extracted before normalisation
        assert reset['ips'] == {'66.132.186.184': 1}
        bad_path = out[
            ('ws-upgrade', 'transport/internet/httpupgrade: bad path')
        ]
        assert bad_path['count'] == 1
        assert bad_path['ips'] == {}


class TestReadNewLines:
    def _write(self, path, text):
        with open(path, 'wb') as f:
            f.write(text.encode())

    def test_first_run_baselines_to_tail(self, tmp_path):
        p = str(tmp_path / 'access.log')
        self._write(p, 'old line 1\nold line 2\n')
        state = {}
        lines, off = reporter.read_new_lines(p, state, 'access')
        assert lines == []
        assert off == os.path.getsize(p)

    def test_incremental_read(self, tmp_path):
        p = str(tmp_path / 'access.log')
        self._write(p, 'a\n')
        state = {'access': os.path.getsize(p)}
        with open(p, 'ab') as f:
            f.write(b'b\nc\n')
        lines, off = reporter.read_new_lines(p, state, 'access')
        assert lines == ['b', 'c']
        assert off == os.path.getsize(p)

    def test_partial_trailing_line_left_for_next_tick(self, tmp_path):
        p = str(tmp_path / 'access.log')
        self._write(p, '')
        state = {'access': 0}
        with open(p, 'ab') as f:
            f.write(b'complete\nhalf-writ')
        lines, off = reporter.read_new_lines(p, state, 'access')
        assert lines == ['complete']
        assert off == len(b'complete\n')

    def test_copytruncate_resets_offset(self, tmp_path):
        p = str(tmp_path / 'access.log')
        self._write(p, 'fresh after rotation\n')
        # stored offset is far past the (now truncated) file size
        state = {'access': 10_000}
        lines, off = reporter.read_new_lines(p, state, 'access')
        assert lines == ['fresh after rotation']
        assert off == os.path.getsize(p)

    def test_oversized_backlog_skips_to_tail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reporter, 'MAX_CHUNK_BYTES', 10)
        p = str(tmp_path / 'access.log')
        self._write(p, 'x' * 100 + '\n')
        state = {'access': 0}
        lines, off = reporter.read_new_lines(p, state, 'access')
        assert lines == []
        assert off == os.path.getsize(p)

    def test_missing_file_is_quiet(self, tmp_path):
        p = str(tmp_path / 'nope.log')
        lines, off = reporter.read_new_lines(p, {'access': 5}, 'access')
        assert lines == []
        assert off == 5


class TestHeartbeat:
    """IMPROVEMENT_PLAN F, B2: an empty window is still reported — the bot's
    exit_reports heartbeat tells "no report" from "no connections"."""

    def test_an_empty_window_is_posted_and_advances_the_offsets(self, tmp_path, monkeypatch):
        logs = tmp_path / 'logs'
        logs.mkdir()
        (logs / 'access.log').write_text('')
        (logs / 'error.log').write_text('')
        monkeypatch.setattr(reporter, 'LOG_DIR', str(logs))
        monkeypatch.setattr(reporter, 'STATE_PATH', str(tmp_path / 'state.json'), raising=False)
        monkeypatch.setattr(reporter, 'TOKEN', 'sekret')
        sent = []
        monkeypatch.setattr(reporter, 'read_hysteria_events', lambda state: [])
        monkeypatch.setattr(reporter, 'post_report', lambda payload: sent.append(payload) or True)
        assert reporter.run_once() == 0
        assert sent == [{'access': [], 'rejects': [], 'presence': [], 'usage': [],
                         'hy2_sessions': []}]

    def test_a_failed_post_keeps_the_offsets(self, tmp_path, monkeypatch):
        logs = tmp_path / 'logs'
        logs.mkdir()
        (logs / 'access.log').write_text('')
        (logs / 'error.log').write_text('')
        monkeypatch.setattr(reporter, 'LOG_DIR', str(logs))
        monkeypatch.setattr(reporter, 'TOKEN', 'sekret')
        saved = []
        monkeypatch.setattr(reporter, '_save_state', lambda st: saved.append(st))
        monkeypatch.setattr(reporter, '_load_state', lambda: {})
        monkeypatch.setattr(reporter, 'read_hysteria_events', lambda state: [])
        monkeypatch.setattr(reporter, 'post_report', lambda payload: False)
        assert reporter.run_once() == 1
        assert saved == []



class TestHy2Sessions:
    """IMPROVEMENT_PLAN F, D4: sessions paired from the hysteria journal,
    inside one hysteria process; a disconnect closes the connect with its
    address, else the newest open one."""

    def test_pairs_by_client_whatever_the_address(self):
        state = {}
        events = [('hysteria', 100.0, 'connected', 'u1@x', '', 'b:1', '1.2.3.4:1'),
                  ('hysteria', 160.0, 'connected', 'u2@x', '', 'b:1', '5.6.7.8:1'),
                  ('hysteria', 700.0, 'disconnected', 'u1@x',
                   'timeout: no recent network activity', 'b:1', '1.2.3.4:9'),
                  ('hysteria', 800.0, 'disconnected', 'u3@x', 'x', 'b:1', '')]   # never seen open
        sessions = reporter.pair_sessions(events, state, now=800.0)
        assert sessions == [{'id': 'u1@x', 'unit': 'hysteria', 'duration_s': 600,
                             'error': 'timeout: no recent network activity', 'ended_at': 700.0}]
        assert state['hy2_open'] == {'hysteria|b:1|u2@x': [[160.0, '5.6.7.8:1']]}

    def test_a_lost_disconnect_does_not_stretch_the_next_sessions(self):
        # the review's repro: one orphan connect, then sessions of 60 s and
        # 30 s an hour apart — reported as 60 and 30, not 3660 and 3630
        state = {}
        events = [('hysteria', 0.0, 'connected', 'u1@x', '', 'b:1', 'a:1'),
                  ('hysteria', 3600.0, 'connected', 'u1@x', '', 'b:1', 'a:2'),
                  ('hysteria', 3660.0, 'disconnected', 'u1@x', '', 'b:1', 'a:9'),
                  ('hysteria', 7200.0, 'connected', 'u1@x', '', 'b:1', 'a:3'),
                  ('hysteria', 7230.0, 'disconnected', 'u1@x', '', 'b:1', 'a:8')]
        got = [x['duration_s'] for x in reporter.pair_sessions(events, state, now=7230.0)]
        assert got == [60, 30]

    def test_the_same_address_closes_its_own_connect(self):
        state = {}
        events = [('hysteria', 0.0, 'connected', 'u1@x', '', 'b:1', 'phone:1'),
                  ('hysteria', 100.0, 'connected', 'u1@x', '', 'b:1', 'laptop:1'),
                  ('hysteria', 500.0, 'disconnected', 'u1@x', '', 'b:1', 'phone:1')]
        assert reporter.pair_sessions(events, state, now=500.0)[0]['duration_s'] == 500

    def test_a_restart_drops_the_connects_it_killed(self):
        state = {}
        reporter.pair_sessions([('hysteria', 0.0, 'connected', 'u1@x', '', 'b:1', 'a:1')],
                               state, now=0.0)
        # hysteria restarted (new pid): no disconnect was logged for the old one
        events = [('hysteria', 4000.0, 'connected', 'u1@x', '', 'b:2', 'a:2'),
                  ('hysteria', 4060.0, 'disconnected', 'u1@x', '', 'b:2', 'a:7')]
        got = reporter.pair_sessions(events, state, now=4060.0)
        assert [x['duration_s'] for x in got] == [60]
        assert state['hy2_open'] == {}

    def test_an_open_connect_carries_over_and_closes_later(self):
        state = {}
        reporter.pair_sessions([('hysteria', 10.0, 'connected', 'u1@x', '', 'b:1', 'a')],
                               state, now=10.0)
        later = reporter.pair_sessions([('hysteria', 3610.0, 'disconnected', 'u1@x', '', 'b:1', 'a')],
                                       state, now=3610.0)
        assert later[0]['duration_s'] == 3600

    def test_the_two_instances_are_kept_apart_and_the_probe_left_out(self):
        state = {}
        events = [('hysteria', 0.0, 'connected', 'u1@x', ''),
                  ('hysteria-turbo', 5.0, 'disconnected', 'u1@x', ''),
                  ('hysteria', 0.0, 'connected', 'probe_monitor@x', ''),
                  ('hysteria', 9.0, 'disconnected', 'probe_monitor@x', '')]
        assert reporter.pair_sessions(events, state, now=9.0) == []

    def test_a_connect_never_closed_is_forgotten_after_a_day(self):
        state = {}
        reporter.pair_sessions([('hysteria', 0.0, 'connected', 'u1@x', '')], state, now=0.0)
        reporter.pair_sessions([], state, now=25 * 3600.0)
        assert state['hy2_open'] == {}

    def test_an_old_state_shape_is_dropped_not_crashed_on(self):
        state = {'hy2_open': {'hysteria|u1@x': [10.0]}}
        assert reporter.pair_sessions([], state, now=20.0) == []
        assert state['hy2_open'] == {}


class _Run:
    def __init__(self, stdout='', rc=0, stderr=''):
        self.stdout, self.returncode, self.stderr = stdout, rc, stderr


def _entry(cursor, epoch_s, message, pid='77'):
    import json as _json
    return _json.dumps({'__CURSOR': cursor, '_SYSTEMD_UNIT': 'hysteria.service',
                        '_BOOT_ID': 'boot', '_PID': pid,
                        '__REALTIME_TIMESTAMP': str(int(epoch_s * 1e6)), 'MESSAGE': message})


CONNECT = ('2026-11-11T01:17:40+05:00\tINFO\tclient connected\t'
           '{"addr": "1.2.3.4:36389", "id": "u1@x", "tx": 0}')
DISCONNECT = ('2026-11-11T02:01:40+05:00\tINFO\tclient disconnected\t'
              '{"addr": "1.2.3.4:36389", "id": "u1@x", '
              '"error": "timeout: no recent network activity"}')


class TestJournalRead:

    def test_journal_lines_are_parsed(self, monkeypatch):
        now = 1762800100.0
        out = '\n'.join([_entry('c1', 1762800000.0, CONNECT),
                         _entry('c2', 1762800060.0, 'something else')])
        calls = []
        monkeypatch.setattr(reporter.subprocess, 'run',
                            lambda cmd, **kw: calls.append(cmd) or _Run(out))
        state = {'journal_cursor': 'c0', 'journal_cursor_ts': now - 300}
        events = reporter.read_hysteria_events(state, now=now)
        assert events == [('hysteria', 1762800000.0, 'connected', 'u1@x', '', 'boot:77',
                           '1.2.3.4:36389')]
        assert state['journal_cursor'] == 'c2' and state['journal_cursor_ts'] == 1762800060.0
        assert calls[0][-2:] == ['--after-cursor', 'c0']

    def test_hysterias_coloured_lines_arrive_as_bytes(self, monkeypatch):
        # what journalctl -o json gives for hysteria on exit: MESSAGE as an
        # array of byte values, because the level is coloured (ESC[34m)
        import json as _json
        text = ('2026-11-11T09:43:57+05:00\t\x1b[34mINFO\x1b[0m\tclient disconnected\t'
                '{"addr": "1.2.3.4:36389", "id": "u1@x", "error": "timeout: no recent network activity"}')
        entry = {'__CURSOR': 'c1', '_SYSTEMD_UNIT': 'hysteria.service', '_BOOT_ID': 'boot',
                 '_PID': '77', '__REALTIME_TIMESTAMP': '1762800000000000',
                 'MESSAGE': list(text.encode('utf-8'))}
        monkeypatch.setattr(reporter.subprocess, 'run',
                            lambda cmd, **kw: _Run(_json.dumps(entry)))
        events = reporter.read_hysteria_events({}, now=1762800100.0)
        assert events == [('hysteria', 1762800000.0, 'disconnected', 'u1@x',
                           'timeout: no recent network activity', 'boot:77', '1.2.3.4:36389')]

    def test_a_message_that_is_not_bytes_is_skipped(self, monkeypatch):
        import json as _json
        entry = {'__CURSOR': 'c1', '_SYSTEMD_UNIT': 'hysteria.service',
                 '__REALTIME_TIMESTAMP': '1762800000000000', 'MESSAGE': [300, 'x']}
        monkeypatch.setattr(reporter.subprocess, 'run',
                            lambda cmd, **kw: _Run(_json.dumps(entry)))
        assert reporter.read_hysteria_events({}, now=1762800100.0) == []

    def test_a_failing_journalctl_starts_over(self, monkeypatch):
        monkeypatch.setattr(reporter.subprocess, 'run',
                            lambda cmd, **kw: _Run('', rc=1, stderr='Failed to seek to cursor'))
        state = {'journal_cursor': 'bad', 'journal_cursor_ts': 990.0,
                 'hy2_open': {'hysteria|b:1|u1@x': [[900.0, 'a']]}}
        assert reporter.read_hysteria_events(state, now=1000.0) == []
        assert 'journal_cursor' not in state and state['hy2_open'] == {}

    def test_a_timeout_starts_over(self, monkeypatch):
        def slow(cmd, **kw):
            raise reporter.subprocess.TimeoutExpired(cmd, 30)
        monkeypatch.setattr(reporter.subprocess, 'run', slow)
        state = {'journal_cursor': 'c', 'journal_cursor_ts': 990.0}
        assert reporter.read_hysteria_events(state, now=1000.0) == []
        assert 'journal_cursor' not in state

    def test_a_quiet_journal_keeps_its_cursor_and_open_connects(self, monkeypatch):
        # the cursor's entry is two hours old, the last read 29 min: hysteria
        # was quiet, the reporter was not down
        calls = []
        monkeypatch.setattr(reporter.subprocess, 'run',
                            lambda cmd, **kw: calls.append(cmd) or _Run(''))
        state = {'journal_cursor': 'c', 'journal_cursor_ts': 10000.0 - 2 * 3600,
                 'journal_read_ts': 10000.0 - 29 * 60,
                 'hy2_open': {'hysteria|b:1|u1@x': [[2000.0, 'a']]}}
        assert reporter.read_hysteria_events(state, now=10000.0) == []
        assert calls[0][-2:] == ['--after-cursor', 'c']
        assert state['hy2_open'] == {'hysteria|b:1|u1@x': [[2000.0, 'a']]}
        assert state['journal_read_ts'] == 10000.0

    def test_a_reporter_that_was_down_starts_over(self, monkeypatch):
        calls = []
        monkeypatch.setattr(reporter.subprocess, 'run',
                            lambda cmd, **kw: calls.append(cmd) or _Run(''))
        state = {'journal_cursor': 'c', 'journal_cursor_ts': 10000.0 - 40 * 60,
                 'journal_read_ts': 10000.0 - 31 * 60,
                 'hy2_open': {'hysteria|b:1|u1@x': [[2000.0, 'a']]}}
        reporter.read_hysteria_events(state, now=10000.0)
        assert calls[0][-2:] == ['--since', '-10min']
        assert state['hy2_open'] == {}

    def test_a_state_from_before_the_read_time_falls_back_to_the_cursor(self, monkeypatch):
        # what exit's reporter wrote until 2026-11-11: no journal_read_ts
        calls = []
        monkeypatch.setattr(reporter.subprocess, 'run',
                            lambda cmd, **kw: calls.append(cmd) or _Run(''))
        fresh = {'journal_cursor': 'c', 'journal_cursor_ts': 1000.0,
                 'hy2_open': {'hysteria|b:1|u1@x': [[900.0, 'a']]}}
        old = json.loads(json.dumps(fresh))
        reporter.read_hysteria_events(fresh, now=1000.0 + 5 * 60)
        reporter.read_hysteria_events(old, now=1000.0 + 31 * 60)
        assert [c[-2:] for c in calls] == [['--after-cursor', 'c'], ['--since', '-10min']]
        assert fresh['hy2_open'] and old['hy2_open'] == {}

    def test_a_session_open_across_a_quiet_journal_is_reported(self, tmp_path, monkeypatch):
        # exit, 2026-11-11: hysteria logged nothing for 35 min while the
        # reporter ran every 5; judged by the age of the cursor's entry, the
        # reporter forgot the open connects and such sessions never came
        logs = tmp_path / 'logs'
        logs.mkdir()
        (logs / 'access.log').write_text('')
        (logs / 'error.log').write_text('')
        monkeypatch.setattr(reporter, 'LOG_DIR', str(logs))
        monkeypatch.setattr(reporter, 'STATE_PATH', str(tmp_path / 'state.json'), raising=False)
        monkeypatch.setattr(reporter, 'TOKEN', 'sekret')
        t0 = 1762800000.0
        clock = [t0]
        monkeypatch.setattr(reporter, 'time', types.SimpleNamespace(time=lambda: clock[0]))
        journal = iter([_entry('c1', t0, CONNECT), '',
                        _entry('c2', t0 + 44 * 60, DISCONNECT)])
        calls = []
        monkeypatch.setattr(reporter.subprocess, 'run',
                            lambda cmd, **kw: calls.append(cmd) or _Run(next(journal)))
        sent = []
        monkeypatch.setattr(reporter, 'post_report', lambda payload: sent.append(payload) or True)
        # the last run reads 20 min after the one before it, 45 min after
        # the cursor's entry
        for at in (t0 + 60, t0 + 25 * 60, t0 + 45 * 60):
            clock[0] = at
            assert reporter.run_once() == 0
        assert [c[-2:] for c in calls] == [['--since', '-10min'], ['--after-cursor', 'c1'],
                                           ['--after-cursor', 'c1']]
        assert sent[-1]['hy2_sessions'] == [{'id': 'u1@x', 'unit': 'hysteria',
                                             'duration_s': 44 * 60,
                                             'error': 'timeout: no recent network activity',
                                             'ended_at': t0 + 44 * 60}]

    def test_entries_from_before_the_cursor_are_skipped(self, monkeypatch):
        # a vacuumed cursor: journalctl starts from the head with rc 0
        out = '\n'.join([_entry('old', 100.0, CONNECT), _entry('new', 1000.0, CONNECT)])
        monkeypatch.setattr(reporter.subprocess, 'run', lambda cmd, **kw: _Run(out))
        state = {'journal_cursor': 'gone', 'journal_cursor_ts': 900.0}
        events = reporter.read_hysteria_events(state, now=1001.0)
        assert [e[1] for e in events] == [1000.0]

    def test_the_journal_never_costs_the_report(self, tmp_path, monkeypatch):
        logs = tmp_path / 'logs'
        logs.mkdir()
        (logs / 'access.log').write_text('')
        (logs / 'error.log').write_text('')
        monkeypatch.setattr(reporter, 'LOG_DIR', str(logs))
        monkeypatch.setattr(reporter, 'STATE_PATH', str(tmp_path / 'state.json'), raising=False)
        monkeypatch.setattr(reporter, 'TOKEN', 'sekret')

        def broken(state):
            raise RuntimeError('boom')
        monkeypatch.setattr(reporter, 'read_hysteria_events', broken)
        sent = []
        monkeypatch.setattr(reporter, 'post_report', lambda payload: sent.append(payload) or True)
        assert reporter.run_once() == 0
        assert sent and sent[0]['hy2_sessions'] == []


class TestUsageBlock:

    def test_every_inbound_with_its_count(self):
        lines = [
            '2026/11/10 10:00:00.1 from 203.0.113.5:5555 accepted tcp:a.com:443 [inbound-443 >> direct] email: u1@x',
            '2026/11/10 10:00:01.1 from 203.0.113.5:5556 accepted tcp:b.com:443 [inbound-443 >> direct] email: u1@x',
            '2026/11/10 10:00:02.1 from 203.0.113.5:5557 accepted tcp:c.com:443 [inbound-2053 >> direct] email: u1@x',
            '2026/11/10 10:00:03.1 from 127.0.0.1:5558 accepted tcp:d.com:443 [api >> api] email: u1@x',
            '2026/11/10 10:00:04.1 from 203.0.113.6:5559 accepted tcp:e.com:443 [inbound-443 >> direct] email: probe_x@x',
        ]
        got = reporter.parse_usage(lines)
        assert got == [{'email': 'u1@x', 'tag': 'inbound-2053', 'conns': 1},
                       {'email': 'u1@x', 'tag': 'inbound-443', 'conns': 2}]



# --------------------------------------------- the hop's TLS (AGENTS.md §53) --

class _TlsBot:
    """The bot's report endpoint over TLS with the hop's certificate: it
    records the token it was sent and answers {"ok": true}."""

    def __init__(self, cert_dir):
        import http.server
        import ssl
        import threading
        seen = self.seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                seen.append((self.path, self.headers.get('X-DPI-Token'), json.loads(body)))
                out = b'{"ok": true, "rows": 0}'
                self.send_response(200)
                self.send_header('Content-Length', str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.load_cert_chain(os.path.join(cert_dir, 'cert.pem'), os.path.join(cert_dir, 'key.pem'))
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestHopTls:
    """BOT_URL / BOT_CA_FILE: the report (and the token it carries, which
    also opens /api/xhr/users and /api/ssh/users) goes over the hop's TLS
    listener, trusting the pinned certificate alone; never in clear."""

    @pytest.fixture
    def pins(self, tmp_path):
        from bot.services import hop_tls
        good, other = tmp_path / 'good', tmp_path / 'other'
        hop_tls.generate(str(good), '127.0.0.1')
        hop_tls.generate(str(other), '127.0.0.1')
        return good, other

    def test_defaults_are_the_hop(self, monkeypatch):
        monkeypatch.delenv('BOT_URL', raising=False)
        monkeypatch.delenv('BOT_CA_FILE', raising=False)
        spec = importlib.util.spec_from_file_location('exit_dpi_reporter_fresh',
                                                      os.path.abspath(REPORTER_PATH))
        fresh = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fresh)
        assert fresh.BOT_URL == 'https://130.49.146.10:8448'
        assert fresh.BOT_CA_FILE == '/etc/nekovpn-hop/entry-hop.pem'

    @pytest.mark.parametrize('url', [
        'http://130.49.146.10:8080', 'http://entry.example.com', 'http://127.evil.com:8080',
        'http://127.0.0.1.evil.com', 'http://user@127.0.0.1@evil.com/', 'http://10.0.0.1',
    ])
    def test_plain_http_to_another_host_is_refused(self, url, monkeypatch):
        monkeypatch.setattr(reporter, 'BOT_URL', url)
        monkeypatch.setattr(reporter, 'TOKEN', 'sekret')
        assert 'in clear' in reporter.bot_problem()

        def no_wire(*a, **kw):
            raise AssertionError('the token went out')
        monkeypatch.setattr(reporter.urllib.request, 'urlopen', no_wire)
        assert reporter.post_report({}) is False

    @pytest.mark.parametrize('url', ['http://127.0.0.1:8080', 'http://localhost:8080',
                                     'http://[::1]:8080'])
    def test_plain_http_to_loopback_is_allowed(self, url):
        assert reporter.bot_problem(url, '') is None

    def test_other_schemes_and_a_missing_pin(self, tmp_path):
        assert 'not an http(s) URL' in reporter.bot_problem('ftp://x', '')
        assert 'not an http(s) URL' in reporter.bot_problem('https://', '')
        assert 'not readable' in reporter.bot_problem('https://1.2.3.4:8448',
                                                      str(tmp_path / 'missing.pem'))
        assert reporter.bot_problem('https://sub.example.com', '') is None   # system CAs

    def test_a_report_over_the_pinned_hop(self, pins, monkeypatch):
        good, _other = pins
        bot = _TlsBot(str(good))
        try:
            monkeypatch.setattr(reporter, 'BOT_URL', f'https://127.0.0.1:{bot.port}')
            monkeypatch.setattr(reporter, 'BOT_CA_FILE', str(good / 'cert.pem'))
            monkeypatch.setattr(reporter, 'TOKEN', 'sekret')
            assert reporter.post_report({'access': []}) is True
            assert bot.seen == [('/api/dpi/exit_report', 'sekret', {'access': []})]
        finally:
            bot.close()

    def test_another_certificate_gets_nothing(self, pins, monkeypatch):
        good, other = pins
        bot = _TlsBot(str(good))
        try:
            monkeypatch.setattr(reporter, 'BOT_URL', f'https://127.0.0.1:{bot.port}')
            monkeypatch.setattr(reporter, 'BOT_CA_FILE', str(other / 'cert.pem'))
            monkeypatch.setattr(reporter, 'TOKEN', 'sekret')
            assert reporter.post_report({'access': []}) is False
            assert bot.seen == []                   # refused before a byte of HTTP
        finally:
            bot.close()

    @pytest.mark.parametrize('url', ['https://127.0.0.1:{port}', 'HTTPS://127.0.0.1:{port}'])
    def test_the_pin_is_the_only_trust_anchor(self, pins, monkeypatch, url):
        # no system CA can stand in for the pin, whatever it issued for
        # entry's address (Let's Encrypt issues IP certificates)
        good, _other = pins
        bot = _TlsBot(str(good))
        try:
            monkeypatch.setattr(reporter, 'BOT_URL', url.format(port=bot.port))
            monkeypatch.setattr(reporter, 'BOT_CA_FILE', str(good / 'cert.pem'))
            monkeypatch.setattr(reporter, 'TOKEN', 'sekret')

            def system_cas(*a, **kw):
                raise AssertionError('the system CAs were trusted')
            monkeypatch.setattr(reporter.ssl, 'create_default_context', system_cas)
            assert reporter.post_report({}) is True
        finally:
            bot.close()

    def test_an_empty_pin_is_the_system_cas(self, monkeypatch):
        monkeypatch.setattr(reporter, 'BOT_URL', 'https://sub.example.com:2096')
        monkeypatch.setattr(reporter, 'BOT_CA_FILE', '')
        sentinel = object()
        monkeypatch.setattr(reporter.ssl, 'create_default_context', lambda: sentinel)
        assert reporter._ssl_context() is sentinel
        monkeypatch.setattr(reporter, 'BOT_URL', 'http://127.0.0.1:8080')
        assert reporter._ssl_context() is None
