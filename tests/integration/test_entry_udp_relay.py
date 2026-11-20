"""hy2's UDP on entry: a DNAT straight to exit, or a REDIRECT to the relay
(docs/insights/64-the-exit-entry-tunnel-users-and-control-apart.md; 2026-11-20).

Since 2026-11-19 entry cannot reach exit, so hy2's ports on entry are
REDIRECTed to wstunnel's client (container nekovpn-hy2wst, :19400 for hy2,
:19402 for hy2t), which carries the datagrams through the reverse tunnel to
hysteria on exit. The healthcheck knew only the DNAT form and paged
``healthcheck:entry_dnat:hy2`` (critical) every run while hy2 worked. Now
layer B reads who listens behind each REDIRECT (``collect_udp_relays``) and
``entry_udp_gaps`` accepts either form:

* no rule for a port at all: as before;
* a REDIRECT to a port nobody listens on (the relay is down);
* a relay that forwards to another exit port.

ss that fails leaves the keys as they were (``unjudged``), never "fine".

Level 2: the script loaded by its path; ``ss`` and ``/proc`` replaced by
fakes in the shapes read on entry on 2026-11-20; the real pager over real
sqlite for the bot's side.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from bot.core.database import Database

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

REPO = Path(__file__).resolve().parents[2]


def _hc():
    path = REPO / 'scripts' / 'protocol_healthcheck.py'
    spec = importlib.util.spec_from_file_location('protocol_healthcheck_udp_relay', str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ``iptables -t nat -S PREROUTING`` on entry, 2026-11-20 (the TCP targets
# replaced by a documentation address).
NAT_RELAY = [
    '-P PREROUTING ACCEPT',
    '-A PREROUTING -p tcp -m tcp --dport 2053 -m comment --comment vmess-forward -j DNAT --to-destination 198.51.100.7:9453',
    '-A PREROUTING -p tcp -m tcp --dport 2054 -m comment --comment vmess-xhttp-forward -j DNAT --to-destination 198.51.100.7:9454',
    '-A PREROUTING -p udp -m udp --dport 8400 -m comment --comment hy2-forward -j REDIRECT --to-ports 19400',
    '-A PREROUTING -p udp -m udp --dport 443 -m comment --comment hy2-udp443 -j REDIRECT --to-ports 19400',
    '-A PREROUTING -p udp -m udp --dport 20000:40000 -m comment --comment hy2-hop -j REDIRECT --to-ports 19400',
    '-A PREROUTING -p udp -m udp --dport 8402 -m comment --comment hy2t-forward -j REDIRECT --to-ports 19402',
    '-A PREROUTING -p udp -m udp --dport 40001:50000 -m comment --comment hy2t-hop -j REDIRECT --to-ports 19402',
    '-A PREROUTING -m addrtype --dst-type LOCAL -j DOCKER',
]
# The form until 2026-11-19: DNAT straight to exit.
NAT_DNAT = [
    '-P PREROUTING ACCEPT',
    '-A PREROUTING -p udp -m udp --dport 8400 -j DNAT --to-destination 198.51.100.1:8400',
    '-A PREROUTING -p udp -m udp --dport 443 -j DNAT --to-destination 198.51.100.1:8400',
    '-A PREROUTING -p udp -m udp --dport 20000:40000 -j DNAT --to-destination 198.51.100.1:8400',
    '-A PREROUTING -p udp -m udp --dport 8402 -j DNAT --to-destination 198.51.100.1:8402',
    '-A PREROUTING -p udp -m udp --dport 40001:50000 -j DNAT --to-destination 198.51.100.1:8402',
]
# ``ss -Hulnp`` on entry, 2026-11-20 (the DHCP client's address replaced).
SS_UDP = """\
UNCONN 0      0                 0.0.0.0:19400 0.0.0.0:* users:(("wstunnel",pid=121711,fd=9))
UNCONN 0      0                 0.0.0.0:19402 0.0.0.0:* users:(("wstunnel",pid=121711,fd=10))
UNCONN 0      0      203.0.113.10%eth0:68    0.0.0.0:* users:(("systemd-network",pid=423,fd=21))
"""
# /proc/121711/cmdline of that wstunnel, 2026-11-20.
WSTUNNEL_CMDLINE = ('/wstunnel\0client\0-L\0udp://0.0.0.0:19400:127.0.0.1:8400?timeout_sec=60\0'
                    '-L\0udp://0.0.0.0:19402:127.0.0.1:8402?timeout_sec=60\0ws://127.0.0.1:19460\0')
RELAYS_OK = {'19400': '8400', '19402': '8402'}


def _proc(tmp_path, cmdlines):
    """A fake /proc: ``{pid: cmdline}``."""
    root = tmp_path / 'proc'
    for pid, cmdline in cmdlines.items():
        (root / str(pid)).mkdir(parents=True)
        (root / str(pid) / 'cmdline').write_bytes(cmdline.encode())
    return str(root)


def _ss(hc, monkeypatch, rc=0, out=SS_UDP):
    """``_run`` answers ``ss -Hulnp``; anything else it is asked fails the test."""
    calls = []

    def fake_run(argv, timeout, stdin=None):
        calls.append(list(argv))
        assert argv == ['ss', '-Hulnp'], argv
        return rc, out, '' if rc == 0 else 'ss: cannot open netlink socket'
    monkeypatch.setattr(hc, '_run', fake_run)
    return calls


class TestReaders:

    def test_udp_listeners_as_read_on_entry(self):
        hc = _hc()
        assert hc.parse_udp_listeners(SS_UDP) == {
            '19400': [('wstunnel', 121711)], '19402': [('wstunnel', 121711)],
            '68': [('systemd-network', 423)]}

    def test_loopback_tcp_and_junk_are_left_out(self):
        hc = _hc()
        text = '\n'.join([
            'State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process',
            'UNCONN 0      0      127.0.0.53%lo:53   0.0.0.0:* users:(("systemd-resolve",pid=7,fd=13))',
            'UNCONN 0      0      127.0.0.1:19400    0.0.0.0:* users:(("wstunnel",pid=8,fd=9))',
            'UNCONN 0      0          [::1]:323         [::]:* users:(("chronyd",pid=9,fd=6))',
            'LISTEN 0      128      0.0.0.0:22       0.0.0.0:* users:(("sshd",pid=10,fd=3))',
            'UNCONN 0      0           [::]:19402       [::]:* users:(("wstunnel",pid=11,fd=10))',
            'UNCONN 0      0              *:5353           *:*',
            'UNCONN x      0        0.0.0.0:1          0.0.0.0:*',
            'UNCONN 0      0        0.0.0.0:http       0.0.0.0:*',
            'garbage', '',
        ])
        assert hc.parse_udp_listeners(text) == {'19402': [('wstunnel', 11)], '5353': []}
        assert hc.parse_udp_listeners(None) == {} == hc.parse_udp_listeners('')

    def test_wstunnel_forwards_as_run_on_entry(self):
        hc = _hc()
        assert hc.parse_wstunnel_udp(WSTUNNEL_CMDLINE.split('\0')) == RELAYS_OK

    @pytest.mark.parametrize('argv', [
        ['-L', 'udp://19400:127.0.0.1:8400'],
        ['-L', 'udp://[::]:19400:[::1]:8400?timeout_sec=60'],
        ['-L=udp://0.0.0.0:19400:127.0.0.1:8400'],
        ['-Ludp://0.0.0.0:19400:127.0.0.1:8400?timeout_sec=60'],
        ['--local-to-remote', 'udp://0.0.0.0:19400:exit.example:8400'],
        ['--local-to-remote=udp://0.0.0.0:19400:127.0.0.1:8400'],
    ])
    def test_wstunnel_spellings(self, argv):
        hc = _hc()
        assert hc.parse_wstunnel_udp(['wstunnel', 'client'] + argv + ['ws://127.0.0.1:19460']) == {
            '19400': '8400'}

    @pytest.mark.parametrize('argv', [
        ['-L', 'tcp://0.0.0.0:19400:127.0.0.1:8400'],
        ['-R', 'udp://0.0.0.0:19400:127.0.0.1:8400'],
        ['--log-lvl', 'udp://0.0.0.0:19400:127.0.0.1:8400'],
        ['-L', 'udp://0.0.0.0:19400:127.0.0.1'],
        ['-L', 'udp://0.0.0.0:19400:127.0.0.1:8400junk'],
        ['-L'],
        [],
    ])
    def test_what_is_not_a_udp_forward(self, argv):
        hc = _hc()
        assert hc.parse_wstunnel_udp(argv) == {}
        assert hc.parse_wstunnel_udp(None) == {}


class TestCollector:

    def test_the_relays_as_on_entry(self, tmp_path, monkeypatch):
        hc = _hc()
        calls = _ss(hc, monkeypatch)
        proc = _proc(tmp_path, {121711: WSTUNNEL_CMDLINE})
        assert hc.collect_udp_relays({'19400', '19402'}, proc=proc) == RELAYS_OK
        assert calls == [['ss', '-Hulnp']]

    def test_a_port_nobody_listens_on_is_left_out(self, tmp_path, monkeypatch):
        hc = _hc()
        _ss(hc, monkeypatch)
        proc = _proc(tmp_path, {121711: WSTUNNEL_CMDLINE})
        assert hc.collect_udp_relays({'19400', '19404'}, proc=proc) == {'19400': '8400'}
        assert hc.collect_udp_relays([19402], proc=proc) == {'19402': '8402'}

    def test_a_listener_that_does_not_say(self, tmp_path, monkeypatch):
        hc = _hc()
        _ss(hc, monkeypatch, out='\n'.join([
            'UNCONN 0 0 0.0.0.0:19400 0.0.0.0:* users:(("socat",pid=5,fd=3))',
            'UNCONN 0 0 0.0.0.0:19402 0.0.0.0:* users:(("wstunnel",pid=6,fd=3))',
            'UNCONN 0 0 0.0.0.0:19404 0.0.0.0:* users:(("wstunnel",pid=7,fd=3))',
            'UNCONN 0 0 0.0.0.0:19406 0.0.0.0:*',
        ]))
        # socat's argv is not read as wstunnel's even where it would parse;
        # pid 6 is gone from /proc; pid 7 forwards other ports only
        proc = _proc(tmp_path, {5: 'socat\0-L\0udp://0.0.0.0:19400:127.0.0.1:8402\0',
                                7: '/wstunnel\0client\0-L\0udp://0.0.0.0:1:127.0.0.1:2\0'})
        assert hc.collect_udp_relays({'19400', '19402', '19404', '19406'}, proc=proc) == {
            '19400': None, '19402': None, '19404': None, '19406': None}

    def test_no_redirect_asks_nothing(self, tmp_path, monkeypatch):
        hc = _hc()
        calls = _ss(hc, monkeypatch)
        assert hc.collect_udp_relays(set(), proc=str(tmp_path)) == {}
        assert hc.collect_udp_relays(None, proc=str(tmp_path)) == {}
        assert calls == []

    def test_ss_that_fails_is_not_looked(self, tmp_path, monkeypatch):
        hc = _hc()
        _ss(hc, monkeypatch, rc=1, out='')
        assert hc.collect_udp_relays({'19400'}, proc=str(tmp_path)) is None

    def test_layer_b_carries_the_relays(self, monkeypatch):
        hc = _hc()

        def fake_run(argv, timeout, stdin=None):
            if argv[:2] == ['systemctl', 'is-active']:
                return 0, 'active\n', ''
            if argv[:2] == ['docker', 'inspect']:
                return 0, 'running\n', ''
            if argv[:2] == ['iptables', '-t']:
                return 0, '\n'.join(NAT_RELAY) + '\n', ''
            raise AssertionError(argv)
        monkeypatch.setattr(hc, '_run', fake_run)
        monkeypatch.setattr(hc, 'collect_ssh_relay', lambda: {'port': None, 'state': None})
        monkeypatch.setattr(hc, 'collect_entry_tunnels', lambda: [])
        asked = []

        def relays(ports):
            asked.append(set(ports))
            return dict(RELAYS_OK)
        monkeypatch.setattr(hc, 'collect_udp_relays', relays)
        b = hc.collect_layer_b()
        assert asked == [{'19400', '19402'}]            # the REDIRECT targets, UDP only
        assert b['ok'] is True and b['udp_relays'] == RELAYS_OK

        def broken(ports):
            raise RuntimeError('proc went away')
        monkeypatch.setattr(hc, 'collect_udp_relays', broken)
        b = hc.collect_layer_b()
        # a reader that crashed is "not looked", and layer B still looked
        assert b['ok'] is True and b['udp_relays'] is None


def _b(nat=NAT_RELAY, relays=RELAYS_OK):
    b = {'ok': True, 'nat_prerouting': list(nat)}
    if relays is not False:
        b['udp_relays'] = relays
    return b


class TestGaps:

    def test_the_relay_of_2026_11_20_carries_both(self):
        hc = _hc()
        assert hc.entry_udp_gaps('hy2', _b()) == []
        assert hc.entry_udp_gaps('hy2t', _b()) == []

    def test_the_dnat_form_still_counts_and_needs_no_relays(self):
        hc = _hc()
        for relays in (None, False, {}):
            assert hc.entry_udp_gaps('hy2', _b(NAT_DNAT, relays)) == []
            assert hc.entry_udp_gaps('hy2t', _b(NAT_DNAT, relays)) == []

    def test_the_relay_is_down(self):
        hc = _hc()
        gaps = hc.entry_udp_gaps('hy2', _b(relays={'19402': '8402'}))
        assert gaps == [{'dport': d, 'port': '8400', 'why': 'deaf', 'via': '19400'}
                        for d in ('443', '8400', '20000:40000')]
        assert hc.udp_gap_text(gaps) == (
            'REDIRECT 443, 8400, 20000:40000 → :19400, но на :19400 никто не слушает')
        assert hc.entry_udp_gaps('hy2t', _b(relays={'19402': '8402'})) == []

    def test_the_relay_forwards_elsewhere(self):
        hc = _hc()
        gaps = hc.entry_udp_gaps('hy2', _b(relays={'19400': '8402', '19402': '8402'}))
        assert {g['why'] for g in gaps} == {'elsewhere'} and len(gaps) == 3
        assert hc.udp_gap_text(gaps) == (
            'REDIRECT 443, 8400, 20000:40000 → :19400, '
            'а ретранслятор на :19400 ведёт на exit :8402, не на :8400')

    def test_a_listener_that_does_not_say_is_taken_as_carrying(self):
        hc = _hc()
        relays = {'19400': None, '19402': None}
        assert hc.entry_udp_gaps('hy2', _b(relays=relays)) == []
        assert hc.entry_udp_gaps('hy2t', _b(relays=relays)) == []

    def test_relays_not_looked_is_not_judged(self):
        hc = _hc()
        for relays in (None, False, 'x'):
            assert hc.entry_udp_gaps('hy2', _b(relays=relays)) is None
            assert hc.entry_udp_gaps('hy2t', _b(relays=relays)) is None

    def test_a_missing_rule_is_named_even_while_the_relays_are_not_looked(self):
        hc = _hc()
        nat = [ln for ln in NAT_RELAY if '--dport 443 ' not in ln]
        assert hc.entry_udp_gaps('hy2', _b(nat, None)) == [
            {'dport': '443', 'port': '8400', 'why': 'none'}]

    def test_no_rules_at_all(self):
        hc = _hc()
        gaps = hc.entry_udp_gaps('hy2', _b(['-P PREROUTING ACCEPT']))
        assert [g['why'] for g in gaps] == ['none'] * 3
        assert hc.udp_gap_text(gaps) == 'нет правил 443→:8400, 8400→:8400, 20000:40000→:8400'
        one = hc.entry_udp_gaps('hy2', _b([ln for ln in NAT_RELAY if '--dport 443 ' not in ln]))
        assert hc.udp_gap_text(one) == 'нет правила 443→:8400'

    def test_both_forms_side_by_side(self):
        hc = _hc()
        nat = [NAT_DNAT[2]] + [ln for ln in NAT_RELAY if '--dport 443 ' not in ln]
        assert hc.entry_udp_gaps('hy2', _b(nat, {})) == [
            {'dport': d, 'port': '8400', 'why': 'deaf', 'via': '19400'} for d in ('8400', '20000:40000')]
        assert hc.entry_udp_gaps('hy2', _b(nat)) == []

    def test_what_to_run(self):
        hc = _hc()
        nxt = hc.udp_gap_next(hc.entry_udp_gaps('hy2', _b(relays={})))
        assert "ss -Hulnp | grep -E ':(19400) '" in nxt
        assert 'docker ps -a --filter name=nekovpn-hy2wst' in nxt
        assert 'docs/insights/64' in nxt
        plain = hc.udp_gap_next(hc.entry_udp_gaps('hy2', _b(['-P PREROUTING ACCEPT'])))
        assert plain.startswith("iptables -t nat -S PREROUTING | grep -E '8400|8402|dport 443'")
        assert 'ss -Hulnp' not in plain


class TestReport:

    def _layers(self, relays=RELAYS_OK, nat=NAT_RELAY, hy2t_off=False):
        a = {'ok': False}
        if hy2t_off:
            a = {'ok': True, 'audit': {'rc': 0, 'lines': ['OK']},
                 'cascade_order': json.dumps([{'name': n, 'enabled': n != 'hy2t'}
                                              for n in ('reality', 'hy2', 'ws', 'stls', 'hy2t')])}
        b = {'ok': True, 'services': {'shadow-tls': 'active', 'haproxy': 'active'},
             'containers': {}, 'nat_prerouting': list(nat), 'udp_relays': relays,
             'ssh_relay': {'port': None, 'state': None}, 'tunnels': []}
        return {'a': a, 'b': b, 'c': {'ok': False}}

    def test_the_false_alarm_of_2026_11_20_is_gone(self):
        hc = _hc()
        layers = self._layers()
        assert not [f for f in hc.findings_for(layers) if f['key'].startswith('entry_dnat')]
        assert not [k for k in hc.unjudged_for(layers) if k.startswith('entry_dnat')]

    def test_the_relay_down_pages_critical(self):
        hc = _hc()
        found = {f['key']: f for f in hc.findings_for(self._layers(relays={}))}
        hy2 = found['entry_dnat:hy2']
        assert (hy2['severity'], hy2['layer']) == ('critical', 'b')
        assert hy2['title'] == ('hy2: UDP с entry не доходит до exit — REDIRECT 443, 8400, '
                                '20000:40000 → :19400, но на :19400 никто не слушает')
        assert "ss -Hulnp | grep -E ':(19400) '" in hy2['next']
        assert found['entry_dnat:hy2t']['severity'] == 'warn'
        off = {f['key'] for f in hc.findings_for(self._layers(relays={}, hy2t_off=True))}
        assert 'entry_dnat:hy2' in off and 'entry_dnat:hy2t' not in off

    def test_not_looked_keeps_the_keys(self):
        hc = _hc()
        layers = self._layers(relays=None)
        assert not [f for f in hc.findings_for(layers) if f['key'].startswith('entry_dnat')]
        assert {'entry_dnat:hy2', 'entry_dnat:hy2t'} <= set(hc.unjudged_for(layers))
        off = hc.unjudged_for(self._layers(relays=None, hy2t_off=True))
        assert 'entry_dnat:hy2' in off and 'entry_dnat:hy2t' not in off
        # the old DNAT form is judged without the relays
        dnat = self._layers(relays=None, nat=NAT_DNAT)
        assert not [k for k in hc.unjudged_for(dnat) if k.startswith('entry_dnat')]
        # layer B could not look at all: the bot keeps B's keys by itself
        blind = self._layers(relays=None)
        blind['b']['ok'] = False
        assert not [k for k in hc.unjudged_for(blind) if k.startswith('entry_dnat')]

    def test_the_human_report_lists_it(self):
        hc = _hc()
        report = hc.assess(self._layers(relays={}))
        assert 'entry_dnat:hy2: hy2: UDP с entry не доходит до exit' in hc.render_human(report)
        assert not [k for k in report['unjudged'] if k.startswith('entry_dnat')]
        blind = hc.assess(self._layers(relays=None))
        assert 'entry_dnat:hy2' in blind['unjudged']

    def test_the_section_is_there(self):
        doc = REPO / 'docs' / 'insights' / '64-the-exit-entry-tunnel-users-and-control-apart.md'
        text = doc.read_text()
        assert 'entry_dnat' in text and 'nekovpn-hy2wst' in text


class TestTheBot:

    OLD = {'key': 'entry_dnat:hy2', 'severity': 'critical', 'layer': 'b',
           'title': 'hy2: нет UDP DNAT 443→:8400, 8400→:8400, 20000:40000→:8400 на entry',
           'next': "iptables -t nat -S PREROUTING | grep -E '8400|8402|dport 443'"}

    def test_the_open_false_alarm_closes(self, tmp_path):
        from bot.services.alert_manager import RECOVERY_TICKS
        from .test_adviser import _judged, _manager, _report
        db = Database(str(tmp_path / 'bot.db'))
        mgr = _manager(db)
        _report(db, findings=[self.OLD])
        mgr.run_once()
        assert mgr._state['healthcheck:entry_dnat:hy2'].open
        _report(db, minutes_ago=0, findings=[], unjudged=[])
        for _ in range(RECOVERY_TICKS + 1):
            mgr.run_once()
        assert _judged(db, 'healthcheck:entry_dnat:hy2')
        assert not mgr._state['healthcheck:entry_dnat:hy2'].open

    def test_kept_while_the_relays_could_not_be_read(self, tmp_path):
        from bot.services.alert_manager import RECOVERY_TICKS
        from .test_adviser import _judged, _manager, _report, _texts
        db = Database(str(tmp_path / 'bot.db'))
        mgr = _manager(db)
        _report(db, findings=[self.OLD])
        mgr.run_once()
        _report(db, minutes_ago=0, findings=[], unjudged=['entry_dnat:hy2'])
        for _ in range(RECOVERY_TICKS + 1):
            mgr.run_once()
        assert not any('Восстановилось' in t for t in _texts(mgr))
        assert mgr._state['healthcheck:entry_dnat:hy2'].open
        assert not _judged(db, 'healthcheck:entry_dnat:hy2')
