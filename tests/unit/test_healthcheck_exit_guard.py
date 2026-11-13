"""What a tunnel user can reach on exit itself — the healthcheck's guard
(AGENTS.md §52).

On 2026-11-11 a read-only check found hysteria running on exit's host with no
ACL: any hy2 user reached the host's loopback, Caddy's admin API on
127.0.0.1:2019 included. The panel xray's API was safe only because xray's
freedom refuses loopback on its own — for some inbound types, without an
allow in finalRules, since v26.4.15. These tests pin the guard that pages
when either comes back: the layer-C helpers (they run on exit, so they are
fed sample text here), the pure verdicts, the findings and the keys a run
could not look at. No host, no network.
"""

import copy
import importlib.util
import json
import os

import pytest

_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'scripts', 'protocol_healthcheck.py'))
_spec = importlib.util.spec_from_file_location('protocol_healthcheck_guard', _PATH)
hc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hc)

G = {}
exec(hc.GUARD_HELPERS, G)          # the functions layer C runs on exit

V26 = 'Xray 26.6.22 (Xray, Penetrates Everything.) b99c3e5 (go1.26.4 linux/amd64)'

# exit's hysteria config as read on 2026-11-11 (secrets replaced by markers)
HY_CONFIG = """# Hysteria2 server config — runs as sidecar to Xray.
listen: :8400

tls:
  cert: /etc/hysteria/cert.pem
  key: /etc/hysteria/key.pem

auth:
  type: http
  http:
    url: http://127.0.0.1:8080/api/hy2/auth?marker=AUTH-MARKER
    insecure: false

ignoreClientBandwidth: true
obfs:
  type: salamander
  salamander:
    password: OBFS-MARKER

trafficStats:
  listen: 127.0.0.1:9977
  secret: STATS-MARKER
"""
ACL_52 = """acl:
  inline:
    - reject(0.0.0.0/8)
    - reject(10.0.0.0/8)
    - reject(100.64.0.0/10)
    - reject(127.0.0.0/8)
    - reject(169.254.0.0/16)
    - reject(172.16.0.0/12)
    - reject(192.168.0.0/16)
    - reject(224.0.0.0/4)
    - reject(240.0.0.0/4)
    - reject(::1/128)
    - reject(fc00::/7)
    - reject(fe80::/10)
"""
UNIT = ("# /etc/systemd/system/hysteria.service\n[Unit]\nDescription=Hysteria\n[Service]\n"
        "ExecStart=/usr/local/bin/hysteria server -c /etc/hysteria/config.yaml\nRestart=always\n")


def facts(text, unit=UNIT, files=None):
    files = dict(files or {})
    files.setdefault('/etc/hysteria/config.yaml', text)

    def read(path):
        if path not in files:
            raise FileNotFoundError(path)
        return files[path]
    return G['guard_hysteria'](unit, read)


# exit's running xray config as read on 2026-11-11 (3x-ui's default template),
# clients and keys replaced by markers
def xray_config():
    client = {'id': 'CLIENT-UUID-MARKER', 'email': 'user_marker@example', 'flow': 'xtls-rprx-vision'}
    return {
        'api': {'services': ['HandlerService', 'LoggerService', 'StatsService', 'RoutingService'],
                'tag': 'api'},
        'metrics': {'tag': 'metrics_out', 'listen': '127.0.0.1:11111'},
        'inbounds': [
            {'tag': 'api', 'listen': '127.0.0.1', 'port': 62789, 'protocol': 'dokodemo-door',
             'settings': {'address': '127.0.0.1'}},
            {'tag': 'inbound-443', 'listen': '0.0.0.0', 'port': 443, 'protocol': 'vless',
             'settings': {'clients': [client], 'decryption': 'none'},
             'streamSettings': {'security': 'reality',
                                'realitySettings': {'privateKey': 'PRIVATE-KEY-MARKER'}}},
            {'tag': 'inbound-2053', 'listen': '0.0.0.0', 'port': 2053, 'protocol': 'vmess',
             'settings': {'clients': [client]}},
            {'tag': 'inbound-8444', 'listen': '0.0.0.0', 'port': 8444, 'protocol': 'shadowsocks',
             'settings': {'clients': [{'password': 'SS-PASSWORD-MARKER'}]}},
            {'tag': 'inbound-2054', 'listen': '0.0.0.0', 'port': 2054, 'protocol': 'vmess',
             'settings': {'clients': [client]}},
        ],
        'outbounds': [
            {'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'AsIs', 'redirect': ''}},
            {'tag': 'blocked', 'protocol': 'blackhole', 'settings': {}},
        ],
        'routing': {'domainStrategy': 'AsIs', 'rules': [
            {'type': 'field', 'inboundTag': ['api'], 'outboundTag': 'api'},
            {'type': 'field', 'outboundTag': 'blocked', 'ip': ['geoip:private']},
            {'type': 'field', 'outboundTag': 'blocked', 'protocol': ['bittorrent']},
        ]},
    }


PORT_RULE = {'type': 'field', 'port': '62789,11111', 'outboundTag': 'blocked'}


def with_socks(cfg):
    cfg['inbounds'].append({'tag': 'inbound-1080', 'listen': '0.0.0.0', 'port': 1080,
                            'protocol': 'socks', 'settings': {'auth': 'noauth'}})
    return cfg


def view(cfg=None):
    return G['guard_xray_view'](cfg if cfg is not None else xray_config())


def layer_c(*, hysteria='active', turbo='active', hy=None, turbo_facts=None, listen=('127.0.0.1:2019',),
            version=V26, cfg=None, guard=None):
    hy = facts(HY_CONFIG + ACL_52) if hy is None else hy
    g = {'hysteria': {'hysteria': hy, 'hysteria-turbo': hy if turbo_facts is None else turbo_facts},
         'listen_2019': list(listen) if listen is not None else None,
         'xray_version': version, 'xray': view(cfg)}
    if guard is not None:
        g = guard
    return {'ok': True, 'services': {'hysteria': hysteria, 'hysteria-turbo': turbo}, 'guard': g}


def found(c):
    return {f['key']: f for f in hc.exit_guard(c)[0]}


# ---------------------------------------------------------------- on exit ---

class TestHelpersOnExit:

    def test_no_acl_block_leaves_nothing_of_the_config(self):
        got = facts(HY_CONFIG)
        assert got == {'config': '/etc/hysteria/config.yaml', 'acl_lines': []}

    def test_only_the_acl_block_leaves_the_host(self):
        got = facts(HY_CONFIG + ACL_52 + 'masquerade:\n  type: proxy\n')
        text = json.dumps(got)
        assert 'reject(127.0.0.0/8)' in text
        for marker in ('AUTH-MARKER', 'OBFS-MARKER', 'STATS-MARKER', 'masquerade'):
            assert marker not in text

    def test_an_acl_in_the_middle_ends_at_the_next_top_level_key(self):
        text = 'listen: :8400\n' + ACL_52 + '# a comment\n\nobfs:\n  type: salamander\n  salamander:\n    password: OBFS-MARKER\n'
        got = facts(text)
        assert got['acl_lines'][0] == 'acl:' and 'OBFS-MARKER' not in json.dumps(got)

    def test_a_column_zero_comment_inside_the_block_does_not_end_it(self):
        text = 'acl:\n  inline:\n    - reject(127.0.0.0/8)\n# v6 below\n    - reject(::1/128)\nobfs:\n  type: x\n'
        rules, err = hc.parse_hysteria_acl(facts(text))
        assert hc.hysteria_uncovered(rules, ['127.0.0.1', '::1']) == []

    def test_the_last_execstart_wins(self):
        unit = UNIT + "# /etc/systemd/system/hysteria.service.d/override.conf\n[Service]\nExecStart=\n" \
                      "ExecStart=/usr/local/bin/hysteria server --config=/etc/hysteria/other.yaml\n"
        got = facts('', unit=unit, files={'/etc/hysteria/other.yaml': HY_CONFIG})
        assert got['config'] == '/etc/hysteria/other.yaml'

    def test_no_config_flag_is_an_error(self):
        assert 'error' in facts(HY_CONFIG, unit='[Service]\nExecStart=/usr/local/bin/hysteria server\n')

    def test_an_unreadable_config_is_an_error(self):
        got = G['guard_hysteria'](UNIT, lambda p: (_ for _ in ()).throw(PermissionError(p)))
        assert got['config'] == '/etc/hysteria/config.yaml' and 'error' in got

    def test_an_acl_file_is_read_and_its_error_kept(self):
        text = HY_CONFIG + 'acl:\n  file: /etc/hysteria/acl.txt\n'
        ok = facts(text, files={'/etc/hysteria/acl.txt': 'reject(127.0.0.0/8)\n# note\n'})
        assert ok['acl_file'] == '/etc/hysteria/acl.txt'
        assert ok['acl_file_lines'] == ['reject(127.0.0.0/8)', '# note']
        missing = facts(text)
        assert missing['acl_file_error']

    def test_a_json_config_sends_the_acl_object_only(self):
        unit = UNIT.replace('config.yaml', 'config.json')
        cfg = {'auth': {'type': 'password', 'password': 'AUTH-MARKER'},
               'acl': {'inline': ['reject(127.0.0.0/8)']}}
        got = facts('', unit=unit, files={'/etc/hysteria/config.json': json.dumps(cfg)})
        assert got['acl_json'] == {'inline': ['reject(127.0.0.0/8)']}
        assert 'AUTH-MARKER' not in json.dumps(got)

    def test_listeners_on_2019(self):
        ss = ('LISTEN 0 4096 127.0.0.1:2019 0.0.0.0:* users:(("caddy",pid=825,fd=11))\n'
              'LISTEN 0 4096 127.0.0.1:20190 0.0.0.0:*\n'
              'LISTEN 0 4096 127.0.0.1:12019 0.0.0.0:*\n'
              'LISTEN 0 4096 [::1]:2019 [::]:*\n'
              'LISTEN 0 4096 0.0.0.0:443 0.0.0.0:*\n')
        assert G['guard_listen'](ss, 2019) == ['127.0.0.1:2019', '[::1]:2019']
        assert G['guard_listen']('', 2019) == []

    def test_the_xray_view_carries_no_client_key_or_password(self):
        cfg = xray_config()
        cfg['routing']['rules'].append({'type': 'field', 'user': ['user_marker@example'],
                                        'outboundTag': 'direct'})
        got = view(cfg)
        text = json.dumps(got)
        for marker in ('CLIENT-UUID-MARKER', 'PRIVATE-KEY-MARKER', 'SS-PASSWORD-MARKER',
                       'user_marker@example'):
            assert marker not in text
        assert got['rules'][-1]['other'] == ['user']
        assert [ib['protocol'] for ib in got['inbounds']] == [
            'dokodemo-door', 'vless', 'vmess', 'shadowsocks', 'vmess']

    def test_the_remote_script_compiles_with_its_helpers(self):
        src = hc.GUARD_HELPERS + hc.REMOTE_C % {'sni': 'www.bing.com'}
        compile(src, '<layer C>', 'exec')
        assert 'guard_hysteria(o, guard_read)' in src and 'ss -ltnH' in src
        assert 'xray-linux-amd64 version' in src


# ------------------------------------------------------------- hysteria ACL ---

def rules_of(acl_text):
    rules, err = hc.parse_hysteria_acl({'config': 'x', 'acl_lines': acl_text.splitlines()})
    assert err is None
    return rules


class TestHysteriaAcl:

    def test_the_section_52_block_closes_loopback_and_private(self):
        rules = rules_of(ACL_52)
        assert hc.hysteria_uncovered(rules, hc.HY2_LOOPBACK_TARGETS) == []
        assert hc.hysteria_uncovered(rules, hc.HY2_PRIVATE_TARGETS) == []

    def test_no_acl_leaves_everything(self):
        assert hc.hysteria_uncovered([], ['127.0.0.1', '10.0.0.1']) == ['127.0.0.1', '10.0.0.1']

    def test_reject_all_closes_everything(self):
        assert hc.hysteria_uncovered(rules_of('acl:\n  inline:\n    - reject(all)\n'),
                                     ['127.0.0.1', '::1']) == []

    def test_a_reject_for_one_port_is_not_enough(self):
        rules = rules_of('acl:\n  inline:\n    - reject(127.0.0.0/8, tcp/2019)\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1']) == ['127.0.0.1']

    def test_a_reject_for_one_port_then_all_is_enough(self):
        rules = rules_of('acl:\n  inline:\n    - reject(127.0.0.0/8, tcp/2019)\n    - reject(127.0.0.0/8)\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1']) == []

    def test_a_direct_for_some_ports_before_the_reject_opens_them(self):
        rules = rules_of('acl:\n  inline:\n    - direct(127.0.0.1, tcp/2019)\n    - reject(127.0.0.0/8)\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1']) == ['127.0.0.1']

    def test_a_udp_only_direct_does_not_open_tcp(self):
        rules = rules_of('acl:\n  inline:\n    - direct(127.0.0.0/8, udp/53)\n    - reject(127.0.0.0/8)\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1']) == []

    def test_a_udp_only_reject_leaves_tcp(self):
        rules = rules_of('acl:\n  inline:\n    - reject(127.0.0.0/8, udp)\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1']) == ['127.0.0.1']

    def test_a_tcp_reject_covers_tcp(self):
        rules = rules_of('acl:\n  inline:\n    - reject(127.0.0.0/8, tcp)\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1']) == []

    def test_an_earlier_direct_for_everything_wins(self):
        rules = rules_of('acl:\n  inline:\n    - direct(all)\n    - reject(127.0.0.0/8)\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1']) == ['127.0.0.1']

    def test_v4_rules_do_not_cover_v6_loopback(self):
        rules = rules_of('acl:\n  inline:\n    - reject(127.0.0.0/8)\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1', '::1']) == ['::1']

    def test_geoip_private_is_not_credited(self):
        rules = rules_of('acl:\n  inline:\n    - reject(geoip:private)\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1']) == ['127.0.0.1']

    def test_flow_style_quotes_and_comments(self):
        rules = rules_of('acl:\n  inline: ["reject(127.0.0.0/8)", \'reject(::1/128)\']  # loopback\n')
        assert hc.hysteria_uncovered(rules, ['127.0.0.1', '::1']) == []
        assert rules_of('acl:\n  inline:\n    # - reject(127.0.0.0/8)\n') == []

    def test_unread_facts_are_not_an_empty_acl(self):
        for info in (None, {}, {'error': 'x'}, {'missing': 'x'}, {'config': 'x'},
                     {'config': 'x', 'acl_lines': [], 'acl_file': '/a', 'acl_file_error': 'gone'}):
            assert hc.parse_hysteria_acl(info)[0] is None


# ------------------------------------------------------------- xray's API ---

def exposed_of(cfg, version=(26, 6, 22)):
    return [(ib, port) for ib, _p, port, _w, _o, _why in hc.xray_guard_verdict(view(cfg), version)['exposed']]


class TestXrayApi:

    def test_exit_today_is_closed(self):
        verdict = hc.xray_guard_verdict(view(), (26, 6, 22))
        assert verdict['exposed'] == [] and verdict['polling'] is None
        assert verdict['ports'] == {62789: 'API', 11111: 'metrics'}

    def test_a_socks_inbound_reaches_both_ports(self):
        assert exposed_of(with_socks(xray_config())) == [('inbound-1080', 11111), ('inbound-1080', 62789)]

    @pytest.mark.parametrize('proto', ['http', 'mixed'])
    def test_other_unguarded_kinds(self, proto):
        cfg = with_socks(xray_config())
        cfg['inbounds'][-1]['protocol'] = proto
        assert ('inbound-1080', 62789) in exposed_of(cfg)

    @pytest.mark.parametrize('proto', ['trojan', 'shadowsocks-2022', 'wireguard', 'hysteria'])
    def test_guarded_kinds(self, proto):
        cfg = with_socks(xray_config())
        cfg['inbounds'][-1]['protocol'] = proto
        assert exposed_of(cfg) == []

    def test_a_tunnel_inbound_cannot_pick_its_destination(self):
        cfg = with_socks(xray_config())
        cfg['inbounds'][-1]['protocol'] = 'dokodemo-door'
        assert exposed_of(cfg) == []

    def test_a_loopback_only_inbound_is_not_user_facing(self):
        cfg = with_socks(xray_config())
        cfg['inbounds'][-1]['listen'] = '127.0.0.1'
        assert exposed_of(cfg) == []

    def test_the_port_rule_after_api_closes_socks(self):
        cfg = with_socks(xray_config())
        cfg['routing']['rules'].insert(1, dict(PORT_RULE))
        verdict = hc.xray_guard_verdict(view(cfg), (26, 6, 22))
        assert verdict['exposed'] == [] and verdict['polling'] is None

    def test_a_port_rule_for_the_api_only_leaves_metrics(self):
        cfg = with_socks(xray_config())
        cfg['routing']['rules'].insert(1, {'type': 'field', 'port': '62789', 'outboundTag': 'blocked'})
        assert exposed_of(cfg) == [('inbound-1080', 11111)]

    def test_a_port_rule_behind_a_direct_rule_is_bypassed(self):
        cfg = with_socks(xray_config())
        cfg['routing']['rules'][1:1] = [
            {'type': 'field', 'domain': ['keyword:cdn'], 'outboundTag': 'direct'}, dict(PORT_RULE)]
        assert ('inbound-1080', 62789) in exposed_of(cfg)

    def test_the_port_rule_before_api_stops_the_panels_polling(self):
        cfg = xray_config()
        cfg['routing']['rules'].insert(0, dict(PORT_RULE))
        verdict = hc.xray_guard_verdict(view(cfg), (26, 6, 22))
        assert verdict['polling'] == (0, 'blocked')

    def test_a_private_ip_rule_before_api_stops_it_too(self):
        cfg = xray_config()
        rules = cfg['routing']['rules']
        rules[0], rules[1] = rules[1], rules[0]
        assert hc.xray_guard_verdict(view(cfg), (26, 6, 22))['polling'] == (0, 'blocked')

    def test_rules_that_never_see_the_polling_are_fine(self):
        cfg = xray_config()
        cfg['routing']['rules'][0:0] = [
            {'type': 'field', 'inboundTag': ['inbound-443'], 'outboundTag': 'blocked'},
            {'type': 'field', 'domain': ['geosite:category-ads'], 'outboundTag': 'blocked'},
            {'type': 'field', 'port': '25', 'outboundTag': 'blocked'},
            {'type': 'field', 'network': 'udp', 'outboundTag': 'blocked'},
            {'type': 'field', 'ip': ['8.8.8.8/32'], 'outboundTag': 'blocked'}]
        assert hc.xray_guard_verdict(view(cfg), (26, 6, 22))['polling'] is None

    def test_no_api_rule_stops_the_polling(self):
        cfg = xray_config()
        del cfg['routing']['rules'][0]          # 127.0.0.1 now meets geoip:private first
        verdict = hc.xray_guard_verdict(view(cfg), (26, 6, 22))
        assert verdict['polling'] == (0, 'blocked')
        assert verdict['ports'] == {62789: 'API', 11111: 'metrics'}
        cfg['routing']['rules'] = []
        assert hc.xray_guard_verdict(view(cfg), (26, 6, 22))['polling'] == (None, 'direct')
        f = found(layer_c(cfg=cfg))['xray_api_polling']
        assert 'нет api-правила' in f['title']

    def test_an_xray_older_than_the_default_rule(self):
        assert exposed_of(xray_config(), version=(26, 3, 27)) != []
        assert exposed_of(xray_config(), version=(26, 4, 14)) != []
        assert exposed_of(xray_config(), version=(26, 4, 15)) == []
        assert exposed_of(xray_config(), version=(1, 8, 24)) != []

    @pytest.mark.parametrize('final_rules,open_', [
        ([{'action': 'allow'}], True),
        ([{'action': 'allow', 'ip': ['geoip:private']}], True),
        ([{'action': 'allow', 'ip': ['127.0.0.1/32']}], True),
        ([{'action': 'allow', 'ip': ['ext:custom.dat:lan']}], True),          # unreadable allow: assume it covers
        ([{'action': 'allow', 'ip': ['8.8.8.8']}], False),
        ([{'action': 'allow', 'port': '443'}], False),
        ([{'action': 'allow', 'network': 'udp'}], False),
        ([{'action': 'block', 'ip': ['geoip:private']}, {'action': 'allow'}], False),
        ([], False),
    ])
    def test_final_rules(self, final_rules, open_):
        cfg = xray_config()
        cfg['outbounds'][0]['settings']['finalRules'] = final_rules
        assert bool(exposed_of(cfg)) is open_

    def test_a_block_final_rule_guards_even_a_socks_inbound(self):
        cfg = with_socks(xray_config())
        cfg['outbounds'][0]['settings']['finalRules'] = [{'action': 'block', 'ip': ['127.0.0.0/8']}]
        assert exposed_of(cfg) == []

    def test_a_balancer_of_freedoms(self):
        cfg = with_socks(xray_config())
        cfg['outbounds'] = [{'tag': 'blocked', 'protocol': 'blackhole'},
                            {'tag': 'out-a', 'protocol': 'freedom'}]
        cfg['routing']['balancers'] = [{'tag': 'b', 'selector': ['out-']}]
        cfg['routing']['rules'].append({'type': 'field', 'network': 'tcp,udp', 'balancerTag': 'b'})
        assert ('inbound-1080', 62789) in exposed_of(cfg)

    def test_a_first_outbound_that_leaves_the_host_is_safe(self):
        cfg = with_socks(xray_config())
        cfg['outbounds'].insert(0, {'tag': 'warp', 'protocol': 'wireguard', 'settings': {}})
        assert exposed_of(cfg) == []

    def test_the_api_on_its_own_listener(self):
        cfg = with_socks(xray_config())
        del cfg['inbounds'][0]
        del cfg['routing']['rules'][0]
        cfg['api']['listen'] = '127.0.0.1:10085'
        verdict = hc.xray_guard_verdict(view(cfg), (26, 6, 22))
        assert verdict['ports'] == {10085: 'API', 11111: 'metrics'}
        assert ('inbound-1080', 10085) in [(e[0], e[2]) for e in verdict['exposed']]
        assert verdict['polling'] is None

    def test_port_specs(self):
        assert hc.port_spec_has('62789', 62789) and hc.port_spec_has(62789, 62789)
        assert hc.port_spec_has('53,1000-70000', 62789)
        assert not hc.port_spec_has('53,443', 62789) and not hc.port_spec_has('x', 62789)
        assert hc.parse_xray_version(V26) == (26, 6, 22)
        assert hc.parse_xray_version('') is None


# ---------------------------------------------------------------- findings ---

class TestFindings:

    def test_exit_after_the_fix_is_quiet(self):
        assert hc.exit_guard(layer_c()) == ([], [])

    def test_exit_on_2026_11_11(self):
        got = found(layer_c(hy=facts(HY_CONFIG)))
        assert {k: f['severity'] for k, f in got.items()} == {
            'hy2_acl:hysteria': 'critical', 'hy2_acl:hysteria-turbo': 'critical'}
        f = got['hy2_acl:hysteria']
        assert f['layer'] == 'c' and 'нет ACL' in f['title'] and '127.0.0.1:2019' in f['title']
        assert '/etc/hysteria/config.yaml' in f['next'] and 'restart hysteria' in f['next']

    def test_caddy_is_named_only_when_it_listens(self):
        got = found(layer_c(hy=facts(HY_CONFIG), listen=()))
        assert 'Caddy' not in got['hy2_acl:hysteria']['title']

    def test_private_networks_left_open_is_a_warning(self):
        acl = 'acl:\n  inline:\n    - reject(127.0.0.0/8)\n    - reject(::1/128)\n'
        got = found(layer_c(hy=facts(HY_CONFIG + acl)))
        assert got['hy2_acl:hysteria']['severity'] == 'warn'
        assert '172.20.0.2' in got['hy2_acl:hysteria']['title']

    @pytest.mark.parametrize('state', ['inactive', 'failed'])
    def test_a_stopped_unit_serves_nobody(self, state):
        assert hc.exit_guard(layer_c(turbo=state, turbo_facts={'error': 'x'})) == ([], [])

    def test_a_starting_unit_is_judged(self):
        assert 'hy2_acl:hysteria-turbo' in found(layer_c(turbo='activating', turbo_facts=facts(HY_CONFIG)))

    def test_an_unanswered_state_is_unjudged_not_stopped(self):
        findings, unjudged = hc.exit_guard(layer_c(turbo='Failed to connect to bus'))
        assert unjudged == ['hy2_acl:hysteria-turbo']
        assert [f['key'] for f in findings] == ['exit_guard_unreadable']

    def test_a_switched_off_turbo_still_running_is_judged(self):
        # hy2t is off in the cascade, but a running instance still lets paid users in
        got = found(layer_c(turbo_facts=facts(HY_CONFIG)))
        assert got['hy2_acl:hysteria-turbo']['severity'] == 'critical'

    def test_an_unreadable_config_is_unjudged_not_fixed(self):
        findings, unjudged = hc.exit_guard(layer_c(turbo_facts={'config': '/x', 'error': 'denied'}))
        assert unjudged == ['hy2_acl:hysteria-turbo']
        assert [f['key'] for f in findings] == ['exit_guard_unreadable']
        assert findings[0]['severity'] == 'warn' and 'denied' in findings[0]['title']

    def test_an_unknown_unit_state_is_unjudged(self):
        c = layer_c()
        del c['services']['hysteria']
        assert hc.exit_guard(c)[1] == ['hy2_acl:hysteria']

    def test_an_exposed_api(self):
        got = found(layer_c(cfg=with_socks(xray_config())))
        f = got['xray_api_exposed']
        assert f['severity'] == 'critical' and 'inbound-1080 (socks)' in f['title']
        assert '"port": "11111,62789"' in f['next'] and '"outboundTag": "blocked"' in f['next']

    def test_the_panels_polling_blocked(self):
        cfg = xray_config()
        cfg['routing']['rules'].insert(0, dict(PORT_RULE))
        f = found(layer_c(cfg=cfg))['xray_api_polling']
        assert f['severity'] == 'critical' and 'правило №1' in f['title'] and 'blocked' in f['title']

    @pytest.mark.parametrize('over', [{'xray_version': ''}, {'xray': None}])
    def test_an_unread_xray_is_unjudged(self, over):
        c = layer_c(cfg=with_socks(xray_config()))
        c['guard'].update(over)
        findings, unjudged = hc.exit_guard(c)
        assert unjudged == list(hc.GUARD_KEYS_XRAY)
        assert [f['key'] for f in findings] == ['exit_guard_unreadable']

    def test_a_collector_without_the_guard_judges_none_of_its_keys(self):
        c = layer_c()
        del c['guard']
        assert hc.exit_guard(c) == ([], ['hy2_acl:hysteria', 'hy2_acl:hysteria-turbo',
                                         'xray_api_exposed', 'xray_api_polling'])

    def test_a_crashed_guard_read(self):
        findings, unjudged = hc.exit_guard(layer_c(guard={'hysteria': {}, 'error': 'boom'}))
        assert [f['key'] for f in findings] == ['exit_guard_unreadable']
        assert len(unjudged) == 4

    def test_findings_for_carries_them_only_when_exit_looked(self):
        base = {'a': {'ok': True}, 'b': {'ok': False}, 'c': layer_c(hy=facts(HY_CONFIG))}
        keys = {f['key'] for f in hc.findings_for(base)}
        assert {'hy2_acl:hysteria', 'hy2_acl:hysteria-turbo'} <= keys
        down = copy.deepcopy(base)
        down['c']['ok'] = False
        assert not any(f['key'].startswith(('hy2_acl', 'xray_api')) for f in hc.findings_for(down))
        assert hc.unjudged_for(down) == []
        assert hc.unjudged_for({'c': {'ok': False}}) == []

    def test_the_report_lists_unjudged_keys_and_shows_findings(self):
        c = layer_c(hy=facts(HY_CONFIG), turbo_facts={'config': '/x', 'error': 'denied'})
        report = hc.assess({'a': {'ok': False}, 'b': {'ok': False}, 'c': c})
        # this layer C carries no exit_servers facts: those keys are unjudged
        # too (TestExitServers in test_protocol_healthcheck.py)
        assert [k for k in report['unjudged']
                if not k.startswith(('exit_server:', 'exit_sync:'))] == ['hy2_acl:hysteria-turbo']
        text = hc.render_human(report)
        assert 'НАХОДКИ' in text and 'hy2_acl:hysteria' in text and 'exit_guard_unreadable' in text

    def test_parse_exit_json_keeps_the_guard(self):
        blob = json.dumps({'services': {}, 'guard': {'listen_2019': []}})
        assert hc.parse_exit_json(blob)['guard'] == {'listen_2019': []}
        assert hc.parse_exit_json(json.dumps({'guard': 'x'}))['guard'] == {}
