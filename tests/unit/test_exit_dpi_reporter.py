"""Tests for scripts/exit_dpi_reporter.py — the exit-side xray log
summariser that feeds POST /api/dpi/exit_report.

The parsing regexes are pinned to REAL log lines captured on the exit
host 2026-08-19; if xray changes its log wording these tests are the
early warning that the DPI feed silently went dark.
"""

import importlib.util
import os

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
        assert sent == [{'access': [], 'rejects': [], 'presence': [], 'hy2_sessions': []}]

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
    """IMPROVEMENT_PLAN F, D4: sessions paired from the hysteria journal."""

    def test_pairs_by_client_whatever_the_address(self):
        state = {}
        events = [('hysteria', 100.0, 'connected', 'u1@x', ''),
                  ('hysteria', 160.0, 'connected', 'u2@x', ''),
                  ('hysteria', 700.0, 'disconnected', 'u1@x',
                   'timeout: no recent network activity'),
                  ('hysteria', 800.0, 'disconnected', 'u3@x', 'x')]   # never seen open
        sessions = reporter.pair_sessions(events, state, now=800.0)
        assert sessions == [{'id': 'u1@x', 'unit': 'hysteria', 'duration_s': 600,
                             'error': 'timeout: no recent network activity'}]
        assert state['hy2_open'] == {'hysteria|u2@x': [160.0]}

    def test_a_disconnect_closes_the_oldest_open_connect(self):
        state = {}
        events = [('hysteria', 0.0, 'connected', 'u1@x', ''),
                  ('hysteria', 100.0, 'connected', 'u1@x', ''),
                  ('hysteria', 400.0, 'disconnected', 'u1@x', '')]
        assert reporter.pair_sessions(events, state, now=400.0)[0]['duration_s'] == 400
        assert state['hy2_open'] == {'hysteria|u1@x': [100.0]}

    def test_an_open_connect_carries_over_and_closes_later(self):
        state = {}
        reporter.pair_sessions([('hysteria', 10.0, 'connected', 'u1@x', '')], state, now=10.0)
        later = reporter.pair_sessions([('hysteria', 3610.0, 'disconnected', 'u1@x', '')],
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

    def test_journal_lines_are_parsed(self, monkeypatch):
        import json as _json
        lines = [
            {'__CURSOR': 'c1', '_SYSTEMD_UNIT': 'hysteria.service',
             '__REALTIME_TIMESTAMP': '1762800000000000',
             'MESSAGE': '2026-11-11T01:17:40+05:00\tINFO\tclient connected\t'
                        '{"addr": "1.2.3.4:36389", "id": "u1@x", "tx": 0}'},
            {'__CURSOR': 'c2', '_SYSTEMD_UNIT': 'hysteria.service',
             '__REALTIME_TIMESTAMP': '1762800060000000', 'MESSAGE': 'something else'},
        ]

        class Done:
            stdout = '\n'.join(_json.dumps(x) for x in lines)
        calls = []
        monkeypatch.setattr(reporter.subprocess, 'run',
                            lambda cmd, **kw: calls.append(cmd) or Done())
        state = {'journal_cursor': 'c0'}
        events = reporter.read_hysteria_events(state)
        assert events == [('hysteria', 1762800000.0, 'connected', 'u1@x', '')]
        assert state['journal_cursor'] == 'c2'
        assert calls[0][-2:] == ['--after-cursor', 'c0']
