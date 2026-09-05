"""The deterministic protocol check must SEE the 2026-09-01 outage.

Four days of dead Reality went unnoticed while every improvised check
(ports, iptables, containers) said "alive". These tests pin the rules of
``assess()`` on fixtures shaped like the real layers: a healthy 7/10
baseline is OK, a dark probe with a flowless panel names the flow wipe
FIRST, all-dark is ONE upstream suspect, a stale pipeline is UNKNOWN
(never OK), an oversized dest cert is named, and the exit codes map as
documented. No host, no network — the collectors are not exercised here.
"""

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

# Same loader shape as tests/unit/test_verify_panel_client_fields.py —
# scripts/ is not a package, so it is loaded by path.
_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'scripts', 'protocol_healthcheck.py',
))
_spec = importlib.util.spec_from_file_location('protocol_healthcheck', _PATH)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)


NOW = datetime(2026, 9, 5, 12, 0, 0)

# Real lines captured 2026-09-05 (entry / exit), IPs as they were.
ENTRY_NAT = [
    '-P PREROUTING ACCEPT',
    '-A PREROUTING -p udp -m udp --dport 8400 -m comment --comment hy2-forward -j DNAT --to-destination 84.75.76.109:8400',
    '-A PREROUTING -p udp -m udp --dport 443 -j DNAT --to-destination 84.75.76.109:8400',
    '-A PREROUTING -p udp -m udp --dport 20000:40000 -m comment --comment hy2-hop -j DNAT --to-destination 84.75.76.109:8400',
    '-A PREROUTING -p udp -m udp --dport 8402 -m comment --comment hy2t-forward -j DNAT --to-destination 84.75.76.109:8402',
    '-A PREROUTING -p udp -m udp --dport 40001:50000 -m comment --comment hy2t-hop -j DNAT --to-destination 84.75.76.109:8402',
]
EXIT_NAT = [
    '-P PREROUTING ACCEPT',
    '-A PREROUTING -p udp -m udp --dport 20000:40000 -m comment --comment hy2-hop-local -j REDIRECT --to-ports 8400',
    '-A PREROUTING -p udp -m udp --dport 40001:50000 -m comment --comment hy2t-hop-local -j REDIRECT --to-ports 8402',
]
CERT_OK_LINES = [
    '<<< TLS 1.3, Handshake [length 0f47], Certificate',
    '    0b 00 0f 43 00 00 0f 3f 00 07 83 30 82 07 7f 30',
]
# 8273 = 0x2051 — microsoft on 2026-07-20 (AGENTS.md §23)
CERT_BAD_LINES = ['<<< TLS 1.3, Handshake [length 2051], Certificate', '    0b 00 ...']


# ---------------------------------------------------------------------------
# fixture builders
# ---------------------------------------------------------------------------

def rows(n_ok=7, n_err_latency=3, n_dark=0, *, runs=3, now=NOW):
    """Probe rows for one protocol over ``runs`` runs, newest first, in the
    [ts, status, latency_ms] shape the inline layer-A script emits."""
    out = []
    for run in range(runs):
        ts = (now - timedelta(minutes=5 + 15 * run)).isoformat()
        out += [[ts, 'ok', 120 + i] for i in range(n_ok)]
        out += [[ts, 'error', 300 + i] for i in range(n_err_latency)]    # HTTP 418 etc.
        out += [[ts, 'timeout', None] for _ in range(n_dark)]
    return out


def dark_rows(runs=3, now=NOW):
    """The 2026-09-01 shape: every row failed WITHOUT a latency."""
    return rows(n_ok=0, n_err_latency=0, n_dark=10, runs=runs, now=now)


def layer_a(*, probes=None, newest_ts=None, audit_rc=0, audit_lines=None,
            alerts=None, ok=True, error=None, now=NOW):
    if probes is None:
        probes = {t: {'rows': rows(now=now), 'last_alive': (now - timedelta(minutes=5)).isoformat()}
                  for t in hc.PROBED}
    if newest_ts is None:
        newest_ts = max((p['rows'][0][0] for p in probes.values() if p.get('rows')),
                        default=None)
    if audit_lines is None:
        audit_lines = ['OK: 324 client records on 4 inbounds, all required per-protocol fields present']
    return {'ok': ok, 'error': error, 'probes': probes, 'newest_ts': newest_ts,
            'alerts': alerts or [], 'audit': {'rc': audit_rc, 'lines': audit_lines},
            'health': {'status': 'healthy', 'version': 'c6bd8b2 2026-09-05T09:08:19Z'}}


def layer_b(*, shadow_tls='active', haproxy='active', probe_proxy='running',
            nat=None, ok=True, error=None):
    return {'ok': ok, 'error': error,
            'services': {'shadow-tls': shadow_tls, 'haproxy': haproxy},
            'containers': {'probe-proxy': probe_proxy, 'vpn-bot': 'running'},
            'nat_prerouting': ENTRY_NAT if nat is None else nat}


def layer_c(*, hysteria='active', turbo='active', rt443=(81, 81), cf443=(81, 81),
            rt8444=81, cf8444=81, rt2053=81, cf2053=81, accepted=None,
            nat=None, cert_lines=None, conns=None, ok=True, error=None):
    def _ib(count, with_flow=0):
        return {'count': count, 'with_flow': with_flow}
    return {
        'ok': ok, 'error': error,
        'services': {'hysteria': hysteria, 'hysteria-turbo': turbo,
                     'hy2-traffic-collector': 'active', 'hy2-traffic-collector-turbo': 'active'},
        'runtime': {'inbound-443': _ib(*rt443), 'inbound-2053': _ib(rt2053),
                    'inbound-8444': _ib(rt8444), 'inbound-2054': _ib(81)},
        'config': {'inbound-443': _ib(*cf443), 'inbound-2053': _ib(cf2053),
                   'inbound-8444': _ib(cf8444), 'inbound-2054': _ib(81)},
        'accepted': accepted if accepted is not None else
        {'inbound-443': 421, 'inbound-2053': 415, 'inbound-8444': 287},
        'last_accepted': {'inbound-443': '2026/09/05 15:20:11'},
        'nat_prerouting': EXIT_NAT if nat is None else nat,
        'hy2_connections_1h': conns or {'hysteria': 4, 'hysteria-turbo': 1},
        'cert_lines': CERT_OK_LINES if cert_lines is None else cert_lines,
        'cert_sni': 'www.bing.com',
        'free_ram_mb': 194,
        'errors': {},
    }


def layers(a=None, b=None, c=None):
    return {'a': layer_a() if a is None else a,
            'b': layer_b() if b is None else b,
            'c': layer_c() if c is None else c}


def reality_dark(now=NOW, **a_kw):
    """Layer A where reality has been dark for 3 days 18 hours."""
    probes = {t: {'rows': rows(now=now), 'last_alive': (now - timedelta(minutes=5)).isoformat()}
              for t in hc.PROBED}
    probes['reality'] = {'rows': dark_rows(now=now),
                         'last_alive': (now - timedelta(days=3, hours=18, minutes=3)).isoformat()}
    return layer_a(probes=probes, now=now, **a_kw)


def wall_now():
    """Naive UTC 'now' for tests that go through main() (no injected clock):
    fixtures pinned to NOW would read as a stale pipeline 45 min after 12:00."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def states_of(report):
    return {t: p['state'] for t, p in report['protocols'].items()}


def titles(report):
    return [s['title'] for s in report['suspects']]


# ---------------------------------------------------------------------------
# healthy baseline
# ---------------------------------------------------------------------------

class TestHealthyBaseline:

    def test_seven_of_ten_is_ok_everywhere(self):
        """7 ok + 3 errors-with-latency per run is the documented normal."""
        report = hc.assess(layers(), now=NOW)

        assert states_of(report) == {t: 'OK' for t in hc.PROTOCOLS}
        assert report['exit_code'] == 0
        assert report['suspects'] == []
        assert report['verdict'].startswith('ИТОГ: все протоколы OK')
        assert 'reality 21/30' in report['verdict']

    def test_hy2t_is_marked_as_having_no_probe(self):
        report = hc.assess(layers(), now=NOW)
        hy2t = report['protocols']['hy2t']
        assert hy2t['probe_coverage'] is False
        assert any('no probe coverage' in e for e in hy2t['evidence'])
        assert hy2t['state'] == 'OK'

    def test_layers_summary_says_ok_only_for_collected_layers(self):
        report = hc.assess(layers(), now=NOW)
        assert report['layers'] == {'a': 'ok', 'b': 'ok', 'c': 'ok'}

    def test_errors_that_carry_a_latency_count_as_alive(self):
        """LIVENESS RULE: an HTTP 418 through the tunnel proves the tunnel.
        3/10 ok with 7 latency-bearing errors is DEGRADED, not DOWN."""
        probes = {t: {'rows': rows(n_ok=3, n_err_latency=7), 'last_alive': NOW.isoformat()}
                  for t in hc.PROBED}
        report = hc.assess(layers(a=layer_a(probes=probes)), now=NOW)
        assert all(s == 'DEGRADED' for t, s in states_of(report).items() if t in hc.PROBED)
        assert report['exit_code'] == 1


# ---------------------------------------------------------------------------
# the incident
# ---------------------------------------------------------------------------

class TestRealityDark:

    def test_flow_wipe_is_suspect_number_one(self):
        """2026-09-01: reality 0/30 with no latency, audit says empty flow."""
        a = reality_dark(audit_rc=1, audit_lines=[
            'BROKEN: 80 problem(s) across 324 client records on 4 inbounds:',
            "  inbound-443 (id=1, vless): user_x@nekovo.ru has empty 'flow'",
        ])
        # the panel hot-applied the wipe: runtime shows 1 user with flow
        report = hc.assess(layers(a=a, c=layer_c(rt443=(81, 1), cf443=(81, 1))), now=NOW)

        assert report['protocols']['reality']['state'] == 'DOWN'
        assert report['protocols']['reality']['down_for'] == '3д 18ч'
        assert report['verdict'] == 'ИТОГ: reality DOWN 3д 18ч, остальные OK'
        first = report['suspects'][0]
        assert first['rank'] == 1
        assert first['protocol'] == 'reality'
        assert 'flow wipe' in first['title']
        assert 'restore_reality_flow.py --apply' in first['next']
        assert report['exit_code'] == 1

    def test_evidence_shows_zero_alive_and_last_alive_timestamp(self):
        report = hc.assess(layers(a=reality_dark()), now=NOW)
        ev = '\n'.join(report['protocols']['reality']['evidence'])
        assert '0/30 живых' in ev
        assert '2026-09-01T17:57:00' in ev
        assert 'аудит панели: OK' in ev

    def test_hot_apply_drift_runtime_below_config(self):
        """Runtime users-with-flow < config.json with-flow = the panel's
        RemoveUser+AddUser left the running core inconsistent."""
        report = hc.assess(layers(a=reality_dark(), c=layer_c(rt443=(81, 40), cf443=(81, 81))),
                           now=NOW)
        assert any('hot-apply drift' in t for t in titles(report))
        drift = next(s for s in report['suspects'] if 'hot-apply drift' in s['title'])
        assert 'restart_xray' in drift['next']

    def test_hot_apply_drift_runtime_users_below_config_users(self):
        report = hc.assess(layers(a=reality_dark(), c=layer_c(rt443=(60, 60), cf443=(81, 81))),
                           now=NOW)
        assert any('hot-apply drift' in t for t in titles(report))

    def test_cert_8273_names_the_dest_cert(self):
        report = hc.assess(layers(a=reality_dark(), c=layer_c(cert_lines=CERT_BAD_LINES)), now=NOW)
        cert = next(s for s in report['suspects'] if 'cert' in s['title'])
        assert '8273' in cert['title']
        assert '3 местах' in cert['title']
        assert 'openssl s_client' in cert['next']
        assert 'Certificate record 8273' in '\n'.join(report['protocols']['reality']['evidence'])

    def test_flow_wipe_outranks_cert_and_drift(self):
        a = reality_dark(audit_rc=1, audit_lines=["inbound-443 (id=1, vless): u@x has empty 'flow'"])
        report = hc.assess(layers(a=a, c=layer_c(rt443=(81, 1), cf443=(81, 1),
                                                cert_lines=CERT_BAD_LINES)), now=NOW)
        assert 'flow wipe' in report['suspects'][0]['title']
        assert 'cert' in report['suspects'][1]['title']

    def test_a_healthy_cert_is_not_a_suspect(self):
        report = hc.assess(layers(a=reality_dark()), now=NOW)
        assert not any('outgrew 8192' in t for t in titles(report))

    def test_nothing_matches_gives_a_generic_error_log_suspect(self):
        """A DOWN protocol must never come back with an empty suspect list —
        that is the agent's 'everything alive' answer in disguise."""
        report = hc.assess(layers(a=reality_dark()), now=NOW)
        assert len(report['suspects']) == 1
        assert report['suspects'][0]['protocol'] == 'reality'
        assert 'error.log' in report['suspects'][0]['next']

    def test_audit_that_could_not_run_is_called_out(self):
        a = reality_dark(audit_rc=2, audit_lines=['CANNOT CHECK: panel unreachable: timeout'])
        report = hc.assess(layers(a=a), now=NOW)
        assert any('аудит панели не отработал' in t for t in titles(report))


# ---------------------------------------------------------------------------
# other protocols
# ---------------------------------------------------------------------------

class TestStls:

    def _dark(self):
        probes = {t: {'rows': rows(), 'last_alive': NOW.isoformat()} for t in hc.PROBED}
        probes['stls'] = {'rows': dark_rows(), 'last_alive': (NOW - timedelta(hours=2)).isoformat()}
        return layer_a(probes=probes)

    def test_shadowsocks_hot_apply_drift(self):
        report = hc.assess(layers(a=self._dark(), c=layer_c(rt8444=0, cf8444=81)), now=NOW)
        assert report['protocols']['stls']['state'] == 'DOWN'
        t = next(t for t in titles(report) if 'shadowsocks hot-apply drift' in t)
        assert 'Unknown account type' in t

    def test_shadow_tls_inactive_on_entry_comes_first(self):
        report = hc.assess(layers(a=self._dark(), b=layer_b(shadow_tls='failed'),
                                  c=layer_c(rt8444=0, cf8444=81)), now=NOW)
        assert 'shadow-tls' in report['suspects'][0]['title']
        assert 'systemctl status shadow-tls' in report['suspects'][0]['next']

    def test_password_wipe_from_audit(self):
        a = self._dark()
        a['audit'] = {'rc': 1, 'lines': ["inbound-8444 (id=8, shadowsocks): u@x has empty 'password'"]}
        report = hc.assess(layers(a=a), now=NOW)
        assert any('password wipe' in t for t in titles(report))


class TestHy2:

    def _dark(self):
        probes = {t: {'rows': rows(), 'last_alive': NOW.isoformat()} for t in hc.PROBED}
        probes['hy2'] = {'rows': dark_rows(), 'last_alive': (NOW - timedelta(minutes=50)).isoformat()}
        return layer_a(probes=probes)

    def test_entry_nat_rule_gone(self):
        nat = [ln for ln in ENTRY_NAT if '--dport 443 ' not in ln]
        report = hc.assess(layers(a=self._dark(), b=layer_b(nat=nat)), now=NOW)
        t = next(t for t in titles(report) if 'entry NAT rule gone' in t)
        assert '443→:8400' in t
        assert 'НЕТ 443→:8400' in '\n'.join(report['protocols']['hy2']['evidence'])

    def test_daemon_down(self):
        report = hc.assess(layers(a=self._dark(), c=layer_c(hysteria='inactive')), now=NOW)
        assert 'daemon down' in report['suspects'][0]['title']
        assert 'journalctl -u hysteria ' in report['suspects'][0]['next']

    def test_hop_rules_gone(self):
        nat = [ln for ln in EXIT_NAT if '20000:40000' not in ln]
        report = hc.assess(layers(a=self._dark(), c=layer_c(nat=nat)), now=NOW)
        assert any('hop rules gone' in t for t in titles(report))

    def test_all_infra_fine_points_at_auth(self):
        report = hc.assess(layers(a=self._dark()), now=NOW)
        assert len(report['suspects']) == 1
        assert '/api/hy2/auth' in report['suspects'][0]['title']

    def test_verdict_mentions_how_long(self):
        report = hc.assess(layers(a=self._dark()), now=NOW)
        assert report['verdict'] == 'ИТОГ: hy2 DOWN 50 мин, остальные OK'


class TestHy2Turbo:
    """No probe → the verdict is layer C (+ entry DNAT) only."""

    def test_inactive_daemon_is_down_even_when_probes_are_fine(self):
        report = hc.assess(layers(c=layer_c(turbo='inactive')), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'DOWN'
        assert 'daemon down' in report['suspects'][0]['title']
        assert 'hysteria-turbo' in report['suspects'][0]['title']
        assert report['exit_code'] == 1
        assert report['verdict'] == 'ИТОГ: hy2t DOWN, остальные OK'

    def test_missing_entry_dnat_8402_is_down(self):
        nat = [ln for ln in ENTRY_NAT if '--dport 8402 ' not in ln]
        report = hc.assess(layers(b=layer_b(nat=nat)), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'DOWN'
        assert any('entry NAT rule gone' in t and '8402' in t for t in titles(report))

    def test_missing_hop_only_is_degraded(self):
        nat = [ln for ln in EXIT_NAT if '40001:50000' not in ln]
        report = hc.assess(layers(c=layer_c(nat=nat)), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'DEGRADED'
        assert any('hop rules gone' in t for t in titles(report))

    def test_zero_connections_in_an_hour_is_not_an_outage(self):
        """Long-lived QUIC sessions: the probe reuses its connection, so
        'client connected' can legitimately be 0 for an hour."""
        report = hc.assess(layers(c=layer_c(conns={'hysteria': 0, 'hysteria-turbo': 0})), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'OK'
        assert report['exit_code'] == 0

    def test_exit_unreachable_leaves_hy2t_unknown_not_ok(self):
        report = hc.assess(layers(c={'ok': False, 'error': 'ssh: connect timed out'}), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'UNKNOWN'
        assert report['layers']['c'].startswith('FAILED')
        assert report['exit_code'] == 2
        assert any('слой C' in t for t in titles(report))


# ---------------------------------------------------------------------------
# collective failure modes
# ---------------------------------------------------------------------------

class TestAllDark:

    def test_single_upstream_suspect(self):
        probes = {t: {'rows': dark_rows(), 'last_alive': (NOW - timedelta(hours=1)).isoformat()}
                  for t in hc.PROBED}
        report = hc.assess(layers(a=layer_a(probes=probes)), now=NOW)

        assert all(report['protocols'][t]['state'] == 'DOWN' for t in hc.PROBED)
        assert len(report['suspects']) == 1
        assert report['suspects'][0]['protocol'] == '*'
        assert 'upstream' in report['suspects'][0]['title']
        assert 'probe-proxy' in report['suspects'][0]['next']
        assert report['verdict'].startswith('ИТОГ: ВСЕ протоколы DOWN')
        assert report['exit_code'] == 1

    def test_dead_probe_proxy_container_is_named_in_the_upstream_suspect(self):
        probes = {t: {'rows': dark_rows(), 'last_alive': NOW.isoformat()} for t in hc.PROBED}
        report = hc.assess(layers(a=layer_a(probes=probes), b=layer_b(probe_proxy='exited')),
                           now=NOW)
        assert 'probe-proxy: exited' in report['suspects'][0]['title']

    def test_all_dark_does_not_list_per_protocol_suspects_even_with_matching_signals(self):
        """A flowless audit during an all-dark window is still ONE incident
        to chase first; per-protocol guesses would send the operator four
        ways at once."""
        probes = {t: {'rows': dark_rows(), 'last_alive': NOW.isoformat()} for t in hc.PROBED}
        a = layer_a(probes=probes, audit_rc=1, audit_lines=["inbound-443: u has empty 'flow'"])
        report = hc.assess(layers(a=a, c=layer_c(cert_lines=CERT_BAD_LINES, hysteria='inactive')),
                           now=NOW)
        assert len(report['suspects']) == 1


class TestStalePipeline:

    def test_stale_rows_make_every_probed_protocol_unknown(self):
        old = NOW - timedelta(hours=2)
        probes = {t: {'rows': rows(now=old), 'last_alive': old.isoformat()} for t in hc.PROBED}
        report = hc.assess(layers(a=layer_a(probes=probes)), now=NOW)

        for t in hc.PROBED:
            assert report['protocols'][t]['state'] == 'UNKNOWN', t
        assert 'probe pipeline dead' in report['suspects'][0]['title']
        # newest row = old run start - 5 min = 2h05 ago
        assert report['verdict'].startswith('ИТОГ: пробы не пишутся 2ч 05мин')
        assert report['exit_code'] == 2

    def test_no_rows_at_all_is_stale_too(self):
        report = hc.assess(layers(a=layer_a(probes={}, newest_ts=None)), now=NOW)
        assert report['protocols']['reality']['state'] == 'UNKNOWN'
        assert 'probe pipeline dead' in report['suspects'][0]['title']
        assert report['exit_code'] == 2

    def test_stale_probes_do_not_hide_a_dead_hysteria_turbo(self):
        """Precedence: a definite failure from another layer beats 'cannot
        assess'."""
        old = NOW - timedelta(hours=2)
        probes = {t: {'rows': rows(now=old), 'last_alive': old.isoformat()} for t in hc.PROBED}
        report = hc.assess(layers(a=layer_a(probes=probes), c=layer_c(turbo='failed')), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'DOWN'
        assert report['exit_code'] == 1

    def test_fresh_rows_at_forty_minutes_are_not_stale(self):
        """One missed run is normal jitter; three (45 min) is the line."""
        recent = NOW - timedelta(minutes=40)
        probes = {t: {'rows': rows(now=recent), 'last_alive': recent.isoformat()} for t in hc.PROBED}
        report = hc.assess(layers(a=layer_a(probes=probes)), now=NOW)
        assert report['exit_code'] == 0


class TestFailedLayers:

    def test_a_and_c_failed_cannot_assess(self):
        report = hc.assess({'a': {'ok': False, 'error': 'docker exec rc=1'},
                            'b': layer_b(),
                            'c': {'ok': False, 'error': 'ssh timeout'}}, now=NOW)
        assert report['exit_code'] == 2
        assert all(p['state'] == 'UNKNOWN' for p in report['protocols'].values())
        assert report['layers']['a'].startswith('FAILED: docker exec')
        assert report['layers']['c'].startswith('FAILED: ssh')
        assert report['verdict'].startswith('ИТОГ: слой A недоступен')

    def test_a_failed_is_never_reported_ok_even_when_exit_looks_fine(self):
        report = hc.assess(layers(a={'ok': False, 'error': 'bot.db: locked'}), now=NOW)
        assert all(report['protocols'][t]['state'] == 'UNKNOWN' for t in hc.PROBED)
        assert report['protocols']['hy2t']['state'] == 'OK'      # layer C still speaks for hy2t
        assert report['exit_code'] == 2
        assert any('слой A' in t for t in titles(report))

    def test_a_failed_but_exit_shows_a_dead_daemon(self):
        report = hc.assess(layers(a={'ok': False, 'error': 'x'}, c=layer_c(hysteria='inactive')),
                           now=NOW)
        # hy2 has no probe verdict; the daemon fact is still surfaced via hy2t? No —
        # hysteria (free) is hy2's unit. Without probes hy2 stays UNKNOWN, but the
        # daemon state is on record in evidence.
        assert report['protocols']['hy2']['state'] == 'UNKNOWN'
        assert 'hysteria inactive' in '\n'.join(report['protocols']['hy2']['evidence'])

    def test_completely_empty_layers_do_not_crash(self):
        report = hc.assess({}, now=NOW)
        assert report['exit_code'] == 2
        assert set(report['protocols']) == set(hc.PROTOCOLS)

    def test_b_failed_does_not_invent_missing_nat_rules(self):
        """iptables that did not answer must not become 'NAT rule gone'."""
        probes = {t: {'rows': rows(), 'last_alive': NOW.isoformat()} for t in hc.PROBED}
        probes['hy2'] = {'rows': dark_rows(), 'last_alive': NOW.isoformat()}
        report = hc.assess(layers(a=layer_a(probes=probes),
                                  b={'ok': False, 'error': 'iptables: permission denied'}), now=NOW)
        assert not any('NAT rule gone' in t for t in titles(report))
        assert report['layers']['b'].startswith('FAILED')


class TestOneTagStale:
    """The pipeline check is global (MAX(ts) over all tags). One tag whose
    rows stopped 2 h ago while the others keep writing must be UNKNOWN —
    its old rows say nothing about now."""

    def _a(self):
        old = NOW - timedelta(hours=2)
        probes = {t: {'rows': rows(), 'last_alive': NOW.isoformat()} for t in hc.PROBED}
        probes['reality'] = {'rows': rows(now=old), 'last_alive': old.isoformat()}
        return layer_a(probes=probes)

    def test_stale_tag_is_unknown_not_ok(self):
        report = hc.assess(layers(a=self._a()), now=NOW)
        assert report['protocols']['reality']['state'] == 'UNKNOWN'
        assert 'пробы по reality не пишутся' in '\n'.join(report['protocols']['reality']['evidence'])
        assert report['protocols']['hy2']['state'] == 'OK'
        assert report['exit_code'] == 2
        assert 'reality UNKNOWN' in report['verdict']


class TestDarkNewestRun:
    """The 3-run average hides an outage that began < 45 min ago: two good
    runs + one fully dark run is still 14/30 'OK'. The newest full run
    without a single ok must be DEGRADED and say so."""

    def _a(self, newest_ok=0, newest_alive=0, older_runs=2):
        newest = rows(n_ok=newest_ok, n_err_latency=newest_alive - newest_ok,
                      n_dark=10 - newest_alive, runs=1)
        older = rows(runs=older_runs + 1)[10:]          # runs at -20 and -35 min
        probes = {t: {'rows': rows(), 'last_alive': NOW.isoformat()} for t in hc.PROBED}
        probes['ws'] = {'rows': newest + older, 'last_alive': NOW.isoformat()}
        return layer_a(probes=probes)

    def test_newest_run_all_dark_is_degraded(self):
        report = hc.assess(layers(a=self._a()), now=NOW)
        ws = report['protocols']['ws']
        assert ws['state'] == 'DEGRADED'
        assert ws['degraded_reason'] == 'dark_run'
        ev = '\n'.join(ws['evidence'])
        assert 'последний прогон: ok 0/10, живых 0/10' in ev
        assert 'туннель не установился ни разу' in ev
        assert report['exit_code'] == 1
        top = report['suspects'][0]
        assert top['protocol'] == 'ws' and 'НАЧАЛО падения' in top['title']
        assert "outbound_tag='ws'" in top['next']

    def test_newest_run_alive_but_zero_ok_is_degraded(self):
        """The live 2026-09-05 13:24 ws run: 1 error-with-latency, 9 dark."""
        report = hc.assess(layers(a=self._a(newest_ok=0, newest_alive=1)), now=NOW)
        ws = report['protocols']['ws']
        assert ws['state'] == 'DEGRADED'
        assert 'живых 1/10' in '\n'.join(ws['evidence'])
        assert 'ни один сайт не прошёл' in '\n'.join(ws['evidence'])

    def test_newest_run_with_one_ok_stays_ok(self):
        report = hc.assess(layers(a=self._a(newest_ok=1, newest_alive=3)), now=NOW)
        assert report['protocols']['ws']['state'] == 'OK'
        assert 'последний прогон: ok 1/10, живых 3/10' in '\n'.join(report['protocols']['ws']['evidence'])
        assert report['exit_code'] == 0

    def test_healthy_evidence_shows_the_newest_run(self):
        report = hc.assess(layers(), now=NOW)
        assert 'последний прогон: ok 7/10, живых 10/10' in '\n'.join(report['protocols']['reality']['evidence'])

    def test_latest_run_helper_groups_by_five_minutes(self):
        rs = rows(runs=3)
        assert len(hc._latest_run(rs, NOW)) == 10
        assert hc._latest_run([], NOW) == []
        assert hc._latest_run([['garbage', 'ok', 1]], NOW) == []


class TestAuditBrokenWithGreenProbes:
    """The probe is ONE client of 81. On 2026-09-01 exactly one Reality
    client kept its flow; had it been the probe's, every probe row would
    have been green while 80 users could not connect. A BROKEN audit on an
    inbound must degrade that protocol even with green probes."""

    FLOW = ['BROKEN: 80 problem(s) across 324 client records on 4 inbounds:',
            "  inbound-443 (id=1, vless): u1@x has empty 'flow'",
            "  inbound-443 (id=1, vless): u2@x has empty 'flow'"]

    def test_flow_wipe_degrades_reality_and_is_suspect_one(self):
        report = hc.assess(layers(a=layer_a(audit_rc=1, audit_lines=self.FLOW)), now=NOW)
        r = report['protocols']['reality']
        assert r['state'] == 'DEGRADED'
        assert r['degraded_reason'] == 'audit'
        assert 'BROKEN на inbound-443' in '\n'.join(r['evidence'])
        assert report['exit_code'] == 1
        assert 'flow wipe' in report['suspects'][0]['title']
        assert 'restore_reality_flow.py --apply' in report['suspects'][0]['next']
        # no misleading "ok below half of normal" suspect for an audit-degrade
        assert not any('деградирован' in t for t in titles(report))
        assert report['protocols']['ws']['state'] == 'OK'

    def test_password_wipe_degrades_stls(self):
        lines = ['BROKEN: 3 problem(s) across 324 client records on 4 inbounds:',
                 "  inbound-8444 (id=8, shadowsocks): u@x has empty 'password'"]
        report = hc.assess(layers(a=layer_a(audit_rc=1, audit_lines=lines)), now=NOW)
        assert report['protocols']['stls']['state'] == 'DEGRADED'
        assert report['protocols']['reality']['state'] == 'OK'
        assert any('password wipe' in t for t in titles(report))
        assert report['exit_code'] == 1

    def test_other_broken_field_is_pinned_to_its_inbound(self):
        lines = ['BROKEN: 1 problem(s) across 324 client records on 4 inbounds:',
                 "  inbound-2053 (id=2, vmess): u@x must NOT carry 'flow' (got 'xtls-rprx-vision')"]
        report = hc.assess(layers(a=layer_a(audit_rc=1, audit_lines=lines)), now=NOW)
        assert report['protocols']['ws']['state'] == 'DEGRADED'
        generic = next(s for s in report['suspects'] if 'аудит панели BROKEN' in s['title'])
        assert 'inbound-2053' in generic['title']
        assert 'verify_panel_client_fields.py' in generic['next']

    def test_broken_on_an_unprobed_inbound_is_a_suspect_but_no_state_change(self):
        lines = ['BROKEN: 1 problem(s) across 324 client records on 4 inbounds:',
                 "  inbound-2054 (id=9, vmess): u@x has empty 'id'"]
        report = hc.assess(layers(a=layer_a(audit_rc=1, audit_lines=lines)), now=NOW)
        assert all(report['protocols'][t]['state'] == 'OK' for t in hc.PROTOCOLS)
        assert any('inbound-2054' in t for t in titles(report))

    def test_audit_flags_parse_inbounds_and_errors(self):
        flags = hc._audit_flags({'rc': 1, 'lines': self.FLOW})
        assert flags['broken_inbounds'] == {'inbound-443'}
        assert flags['flow_missing'] is True
        assert hc._audit_flags({'rc': 0, 'lines': ["x inbound-443 has empty 'flow'"]})['broken_inbounds'] == set()
        assert 'timed out' in hc._audit_flags({'rc': None, 'error': 'Command timed out'})['summary']

    def test_all_dark_still_wins_over_audit(self):
        probes = {t: {'rows': dark_rows(), 'last_alive': NOW.isoformat()} for t in hc.PROBED}
        report = hc.assess(layers(a=layer_a(probes=probes, audit_rc=1, audit_lines=self.FLOW)), now=NOW)
        assert len(report['suspects']) == 1 and 'upstream' in report['suspects'][0]['title']


class TestLayerBFailed:
    """Entry iptables/systemd not answering must not leave hy2t 'OK' and
    rc=0 with 'B FAILED' in the header."""

    def test_hy2t_is_unknown_and_exit_is_two(self):
        report = hc.assess(layers(b={'ok': False, 'error': 'iptables: permission denied'}), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'UNKNOWN'
        assert 'entry DNAT не проверен' in '\n'.join(report['protocols']['hy2t']['evidence'])
        assert 'entry DNAT не проверен' in '\n'.join(report['protocols']['hy2']['evidence'])
        assert report['exit_code'] == 2
        assert report['layers']['b'].startswith('FAILED')
        assert any('слой B' in t for t in titles(report))

    def test_dead_turbo_daemon_still_wins_when_b_failed(self):
        report = hc.assess(layers(b={'ok': False, 'error': 'x'}, c=layer_c(turbo='failed')), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'DOWN'
        assert report['exit_code'] == 1


class TestExitNatUnknown:
    """An empty exit iptables dump means 'did not answer', never 'no rules'."""

    def test_no_hop_rules_gone_suspect_for_dark_hy2(self):
        probes = {t: {'rows': rows(), 'last_alive': NOW.isoformat()} for t in hc.PROBED}
        probes['hy2'] = {'rows': dark_rows(), 'last_alive': NOW.isoformat()}
        report = hc.assess(layers(a=layer_a(probes=probes), c=layer_c(nat=[])), now=NOW)
        assert not any('hop rules gone' in t for t in titles(report))

    def test_hy2t_is_unknown_not_degraded(self):
        c = layer_c(nat=[])
        c['errors'] = {'nat': 'iptables: command not found'}
        report = hc.assess(layers(c=c), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'UNKNOWN'
        assert 'command not found' in '\n'.join(report['protocols']['hy2t']['evidence'])
        assert report['exit_code'] == 2

    def test_remote_script_records_a_nat_error(self):
        src = hc.REMOTE_C % {'sni': 'www.bing.com'}
        assert '"errors"]["nat"]' in src


# ---------------------------------------------------------------------------
# exit codes
# ---------------------------------------------------------------------------

class TestExitCodes:

    @pytest.mark.parametrize('states,code', [
        ({t: 'OK' for t in hc.PROTOCOLS}, 0),
        ({**{t: 'OK' for t in hc.PROTOCOLS}, 'reality': 'DOWN'}, 1),
        ({**{t: 'OK' for t in hc.PROTOCOLS}, 'ws': 'DEGRADED'}, 1),
        ({**{t: 'UNKNOWN' for t in hc.PROTOCOLS}, 'hy2t': 'DOWN'}, 1),
        ({t: 'UNKNOWN' for t in hc.PROTOCOLS}, 2),
        ({**{t: 'OK' for t in hc.PROTOCOLS}, 'hy2t': 'UNKNOWN'}, 2),
        ({}, 2),
    ])
    def test_mapping(self, states, code):
        assert hc.exit_code_for(states) == code


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------

class TestCertParser:

    def test_bing_today(self):
        assert hc.parse_cert_record_len(CERT_OK_LINES) == 0x0f47 == 3911

    def test_microsoft_2026_07_20(self):
        assert hc.parse_cert_record_len(CERT_BAD_LINES) == 8273
        assert 8273 > hc.CERT_LIMIT_BYTES

    def test_no_certificate_line(self):
        assert hc.parse_cert_record_len([]) is None
        assert hc.parse_cert_record_len(None) is None
        assert hc.parse_cert_record_len(['<<< TLS 1.3, Handshake [length 007a], ServerHello']) is None

    def test_does_not_confuse_certificate_verify(self):
        """The CertificateVerify record follows and is tiny; the regex must
        bind to the exact 'Certificate' word."""
        lines = ['<<< TLS 1.3, Handshake [length 0104], CertificateVerify']
        assert hc.parse_cert_record_len(lines) is None


class TestNatParsers:

    def test_entry_dnat_rules(self):
        got = hc.parse_dnat_rules(ENTRY_NAT)
        for tag in ('hy2', 'hy2t'):
            for rule in hc.ENTRY_DNAT_FOR[tag]:
                assert rule in got, rule

    def test_exit_redirect_rules(self):
        got = hc.parse_redirect_rules(EXIT_NAT)
        assert got == {('20000:40000', '8400'), ('40001:50000', '8402')}

    def test_tcp_rules_are_ignored(self):
        line = '-A PREROUTING -p tcp -m tcp --dport 443 -j DNAT --to-destination 1.2.3.4:8400'
        assert hc.parse_dnat_rules([line]) == set()

    def test_none_is_empty(self):
        assert hc.parse_dnat_rules(None) == set()
        assert hc.parse_redirect_rules(None) == set()


class TestExitJsonParser:

    def test_full_blob_round_trips(self):
        blob = {'services': {'hysteria': 'active'}, 'runtime': {'inbound-443': {'count': 81, 'with_flow': 81}},
                'config': {}, 'accepted': {'inbound-443': 5}, 'nat_prerouting': [], 'hy2_connections_1h': {},
                'cert_lines': CERT_OK_LINES, 'free_ram_mb': 194, 'errors': {}}
        got = hc.parse_exit_json(json.dumps(blob))
        assert got['ok'] is True
        assert got['runtime']['inbound-443']['count'] == 81
        assert got['free_ram_mb'] == 194

    def test_missing_keys_get_empty_defaults(self):
        got = hc.parse_exit_json('{"services": {"hysteria": "active"}}')
        assert got['ok'] is True
        assert got['runtime'] == {}
        assert got['nat_prerouting'] == []
        assert got['free_ram_mb'] is None
        assert got['cert_lines'] == []

    def test_wrong_types_fall_back_to_defaults(self):
        """A container-typed key ({} / []) that came back as a string must
        not reach the readers; the numeric scalar is coerced or dropped."""
        got = hc.parse_exit_json('{"runtime": "oops", "nat_prerouting": "x", "free_ram_mb": "194"}')
        assert got['runtime'] == {}
        assert got['nat_prerouting'] == []
        assert got['free_ram_mb'] == 194
        assert hc.parse_exit_json('{"free_ram_mb": "n/a"}')['free_ram_mb'] is None

    def test_motd_before_the_blob_is_skipped(self):
        got = hc.parse_exit_json('Welcome to Ubuntu\nLast login: ...\n{"services": {}}\n')
        assert got['ok'] is True

    def test_garbage_is_a_failed_layer_not_an_exception(self):
        got = hc.parse_exit_json('ssh: connect to host exit-node port 22: Connection timed out')
        assert got['ok'] is False
        assert 'no JSON' in got['error']

    def test_empty_is_a_failed_layer(self):
        assert hc.parse_exit_json('')['ok'] is False
        assert hc.parse_exit_json(None)['ok'] is False

    def test_broken_json_is_a_failed_layer(self):
        got = hc.parse_exit_json('{"services": ')
        assert got['ok'] is False
        assert 'unparsable' in got['error']

    def test_partial_blob_still_yields_a_verdict(self):
        """Half-broken exit: services came back, xray api did not."""
        c = hc.parse_exit_json('{"services": {"hysteria": "active", "hysteria-turbo": "inactive"}}')
        report = hc.assess(layers(c=c), now=NOW)
        assert report['protocols']['hy2t']['state'] == 'DOWN'


class TestEmbeddedScriptsCompile:
    """The two remote payloads are strings — a typo there is only caught at
    runtime on prod. Compile them here (prod image is python 3.11, so
    nothing 3.12-only may sneak in either)."""

    def test_layer_a_inline_script_compiles(self):
        compile(hc.INLINE_A, '<INLINE_A>', 'exec')
        assert '/var/lib/vpn-bot/bot.db' in hc.INLINE_A
        assert 'LIMIT 30' in hc.INLINE_A
        assert "latency_ms IS NOT NULL OR status = 'ok'" in hc.INLINE_A

    def test_layer_c_remote_script_compiles_with_sni(self):
        src = hc.REMOTE_C % {'sni': 'www.bing.com'}
        compile(src, '<REMOTE_C>', 'exec')
        assert 'www.bing.com:443' in src
        assert 'inbounduser' in src
        assert '/app/bin/config.json' in src

    def test_sni_must_be_a_hostname(self, monkeypatch, capsys):
        """The SNI is interpolated into a remote shell line — refuse anything
        that is not a bare hostname before it reaches ssh."""
        monkeypatch.setattr(hc, 'collect_all', lambda sni: pytest.fail('must not collect'))
        assert hc.main(['--sni', 'x; rm -rf /']) == 2


# ---------------------------------------------------------------------------
# rendering + CLI glue
# ---------------------------------------------------------------------------

class TestRendering:

    def test_verdict_is_the_first_line(self):
        report = hc.assess(layers(a=reality_dark()), now=NOW)
        text = hc.render_human(report)
        assert text.splitlines()[0] == 'ИТОГ: reality DOWN 3д 18ч, остальные OK'
        assert '[reality] DOWN — лежит 3д 18ч' in text
        assert 'ПОДОЗРЕВАЕМЫЕ' in text
        assert '→ ' in text

    def test_alerts_block_shows_ack_state(self):
        a = reality_dark(alerts=[['protocol_down:reality', 'critical', 'Протокол reality мёртв',
                                  '2026-09-05 09:16:00', None]])
        text = hc.render_human(hc.assess(layers(a=a), now=NOW))
        assert 'protocol_down:reality' in text
        assert 'НЕ квитирован' in text

    def test_healthy_report_says_nothing_to_fix(self):
        text = hc.render_human(hc.assess(layers(), now=NOW))
        assert 'ПОДОЗРЕВАЕМЫЕ: нет' in text

    def test_main_json_mode_prints_report_and_returns_exit_code(self, monkeypatch, capsys):
        monkeypatch.setattr(hc, 'collect_all', lambda sni: layers(a=reality_dark(now=wall_now())))
        rc = hc.main(['--json', '--sni', 'www.bing.com'])
        out = json.loads(capsys.readouterr().out)
        assert rc == 1
        assert out['exit_code'] == 1
        assert out['protocols']['reality']['state'] == 'DOWN'
        assert 'layers_raw' in out

    def test_main_human_mode_returns_zero_on_healthy(self, monkeypatch, capsys):
        monkeypatch.setattr(hc, 'collect_all', lambda sni: layers(a=layer_a(now=wall_now())))
        rc = hc.main(['--sni', 'www.bing.com'])
        assert rc == 0
        assert capsys.readouterr().out.startswith('ИТОГ: все протоколы OK')

    def test_main_uses_the_wall_clock_so_pinned_fixtures_read_stale(self, monkeypatch, capsys):
        """Guard for the two tests above: main() has no injected clock, so a
        fixture pinned to 12:00 UTC must NOT be silently fresh forever."""
        pinned = NOW - timedelta(days=30)
        monkeypatch.setattr(hc, 'collect_all', lambda sni: layers(a=layer_a(now=pinned)))
        assert hc.main(['--sni', 'www.bing.com']) == 2
        assert 'пробы не пишутся' in capsys.readouterr().out

    def test_humanize(self):
        assert hc._humanize(5) == '5 мин'
        assert hc._humanize(125) == '2ч 05мин'
        assert hc._humanize(3 * 1440 + 18 * 60 + 3) == '3д 18ч'
        assert hc._humanize(float('inf')) == '—'
