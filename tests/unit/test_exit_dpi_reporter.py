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
