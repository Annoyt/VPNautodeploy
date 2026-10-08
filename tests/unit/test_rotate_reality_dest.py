"""scripts/rotate_reality_dest.py — E10: rotate the Reality dest/SNI across
the exit panel, entry HAProxy, the bot's .env (+ recreate) and probe-proxy
in one checked, reversible run.

What is pinned here, and the prod failure each pin stands for:
- the candidate verdict: Certificate record <= 8000 B judged on the MAX of
  the samples, TLS 1.3, ALPN h2, a valid certificate (2026-07-20: microsoft
  grew to 8273 B and Reality died for everyone, AGENTS.md §23);
- the text editors that touch prod files (the HAProxy ACL line, the compose
  .env) — byte-identical outside the edited span, refuse what they cannot
  edit safely;
- the phase order (panel -> HAProxy -> .env -> bot -> probe-proxy), the
  reverse order for rollback/auto-revert, stop-on-failure, and that a revert
  never overwrites a layer this run did not write;
- nothing is written without a typed ``yes``, and never before the snapshot
  is on disk; rollback restores exactly the snapshot's values and refuses
  when a layer was changed by someone else;
- the host-side scripts themselves, run locally against temp files with fake
  haproxy/systemctl/docker/openssl binaries — the very code ssh ships.
No ssh, no network, no prod.
"""

import copy
import importlib.util
import inspect
import io
import json
import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT = os.path.join(ROOT, 'scripts', 'rotate_reality_dest.py')
# The interpreter that plays "the host" for the shipped scripts; point it at
# an older python to prove they run where the hosts' python3 is older.
REMOTE_PY = os.environ.get('ROTATE_TEST_REMOTE_PY') or sys.executable


def _load(name):
    # scripts/ is not a package — loaded by path, same shape as
    # tests/unit/test_protocol_healthcheck.py.
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, 'scripts', name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rot = _load('rotate_reality_dest')
hc = _load('protocol_healthcheck')

BING, GOOGLE, CF, MS = 'www.bing.com', 'www.google.com', 'www.cloudflare.com', 'www.microsoft.com'
APPLY_WRITES = ['panel_set', 'haproxy_set', 'env_set', 'bot_recreate', 'probe_regen']
ROLLBACK_WRITES = ['env_set', 'bot_recreate', 'probe_regen', 'haproxy_set', 'panel_set']

# The real prod line (task E10) and real-shaped openssl 3.0 output lines.
PROD_ACL = 'acl is_reality_sni req_ssl_sni -i www.bing.com www.google.com'


def tls_lines(length=3920, alpn='h2', verify='0 (ok)', tls13=True):
    lines = [f'<<< TLS 1.3, Handshake [length {length:04x}], Certificate'] if tls13 else []
    lines.append('New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384' if tls13 else 'New, (NONE), Cipher is (NONE)')
    lines.append(f'ALPN protocol: {alpn}' if alpn else 'No ALPN negotiated')
    lines.append(f'Verify return code: {verify}')
    return {'rc': 0 if tls13 else 1, 'lines': lines}


def sample(**kw):
    return rot.parse_tls_sample(tls_lines(**kw))


def opts(*argv):
    return rot.parse_opts(rot.build_parser(), list(argv))


# ---------------------------------------------------------------------------
# HAProxy ACL line
# ---------------------------------------------------------------------------

class TestAclLine:
    def test_prod_line_names(self):
        (entry,) = rot.parse_acl_lines(PROD_ACL, 'is_reality_sni')
        assert entry['names'] == [BING, GOOGLE]
        assert entry['error'] is None

    def test_rewrite_keeps_indent_flags_and_comment(self):
        text = '    acl is_reality_sni req_ssl_sni -i -m str www.bing.com   # reality → exit'
        new = rot.rewrite_acl_text(text, 'is_reality_sni', [[GOOGLE]])
        assert new == '    acl is_reality_sni req_ssl_sni -i -m str www.google.com   # reality → exit'

    def test_req_dot_ssl_sni_and_end_of_flags(self):
        text = 'acl is_reality_sni req.ssl_sni -i -- www.bing.com'
        assert rot.parse_acl_lines(text, 'is_reality_sni')[0]['names'] == [BING]

    def test_other_acls_and_comments_are_not_touched(self):
        text = '\n'.join([
            '#acl is_reality_sni req_ssl_sni -i www.bing.com',
            'acl is_reality_sni_old req_ssl_sni -i www.bing.com',
            'acl other req_ssl_sni -i www.bing.com',
            'use_backend exit_reality if is_reality_sni',
        ])
        assert rot.parse_acl_lines(text, 'is_reality_sni') == []

    @pytest.mark.parametrize('line,why', [
        ('acl is_reality_sni req_ssl_sni -i -f /etc/haproxy/sni.lst', 'файла'),
        ('acl is_reality_sni req_ssl_sni -m end bing.com', '-m end'),
        ('acl is_reality_sni req_ssl_sni -m reg ^www\\.', '-m reg'),
    ])
    def test_unrotatable_lines_are_flagged_and_refused(self, line, why):
        (entry,) = rot.parse_acl_lines(line, 'is_reality_sni')
        assert why in entry['error']
        with pytest.raises(ValueError):
            rot.rewrite_acl_text(line, 'is_reality_sni', [[GOOGLE]])

    def test_line_without_names_gets_them_after_the_flags(self):
        text = 'acl is_reality_sni req_ssl_sni -i   # todo'
        assert rot.rewrite_acl_text(text, 'is_reality_sni', [[GOOGLE]]) == \
            'acl is_reality_sni req_ssl_sni -i www.google.com   # todo'

    def test_every_other_byte_survives_including_crlf(self):
        text = ('global\r\n    log /dev/log local0\r\n'
                'frontend reality\r\n    bind :8443\r\n'
                f'    {PROD_ACL}\r\n'
                '    use_backend exit_reality if is_reality_sni\r\n'
                '    stats auth admin:s3cr3t\r\n')
        new = rot.rewrite_acl_text(text, 'is_reality_sni', [[GOOGLE, CF]])
        assert new == text.replace('www.bing.com www.google.com', 'www.google.com www.cloudflare.com')

    def test_two_lines_rewritten_independently(self):
        text = f'{PROD_ACL}\nfrontend b\n  acl is_reality_sni req_ssl_sni -i www.bing.com\n'
        new = rot.rewrite_acl_text(text, 'is_reality_sni', [[GOOGLE], [GOOGLE, BING]])
        assert [e['names'] for e in rot.parse_acl_lines(new, 'is_reality_sni')] == [[GOOGLE], [GOOGLE, BING]]

    def test_count_mismatch_and_empty_list_raise(self):
        with pytest.raises(ValueError):
            rot.rewrite_acl_text(PROD_ACL, 'is_reality_sni', [[GOOGLE], [BING]])
        with pytest.raises(ValueError):
            rot.rewrite_acl_text(PROD_ACL, 'is_reality_sni', [[]])


class TestTransformNames:
    def test_removed_name_is_replaced_in_place(self):
        assert rot.transform_names([BING, GOOGLE], [BING], [CF]) == [CF, GOOGLE]

    def test_added_name_already_present_just_drops_the_removed_one(self):
        # prod today: rotating bing -> google with "bing google" in the ACL
        assert rot.transform_names([BING, GOOGLE], [BING], [GOOGLE]) == [GOOGLE]

    def test_keep_old_appends_and_removes_nothing(self):
        assert rot.transform_names([BING, GOOGLE], [], [CF, BING]) == [BING, GOOGLE, CF]

    def test_case_insensitive_and_no_duplicates(self):
        assert rot.transform_names(['WWW.Bing.com', GOOGLE], [BING], [GOOGLE.upper()]) == [GOOGLE]


# ---------------------------------------------------------------------------
# compose .env
# ---------------------------------------------------------------------------

ENV_TEXT = ('BOT_TOKEN=123:SECRET\n'
            '# SNI_VALUE=www.microsoft.com\n'
            'SNI_VALUE_OLD=www.microsoft.com\n'
            'SNI_VALUE=www.bing.com\n'
            'XUI_PASSWORD="p#ss word"\n')


class TestEnvFile:
    @pytest.mark.parametrize('line,value', [
        ('SNI_VALUE=www.bing.com', BING),
        ('SNI_VALUE="www.bing.com"', BING),
        ("export SNI_VALUE='www.bing.com'  # after §23", BING),
        ('  SNI_VALUE = www.bing.com # note', BING),
        ('SNI_VALUE=www.bing.com\r', BING),
    ])
    def test_dialects(self, line, value):
        assert rot.parse_env_value(line, 'SNI_VALUE') == (value, 1)

    def test_comments_and_lookalike_keys_do_not_count(self):
        assert rot.parse_env_value(ENV_TEXT, 'SNI_VALUE') == (BING, 1)
        assert rot.parse_env_value('# SNI_VALUE=x\nSNI_VALUE_OLD=y', 'SNI_VALUE') == (None, 0)

    def test_rewrite_touches_exactly_one_line(self):
        new = rot.rewrite_env_text(ENV_TEXT, 'SNI_VALUE', GOOGLE)
        assert new == ENV_TEXT.replace('SNI_VALUE=www.bing.com\n', 'SNI_VALUE=www.google.com\n')

    def test_rewrite_keeps_export_quotes_comment_and_crlf(self):
        text = "a=1\r\nexport SNI_VALUE='www.bing.com'  # §23\r\nb=2\r\n"
        assert rot.rewrite_env_text(text, 'SNI_VALUE', GOOGLE) == \
            "a=1\r\nexport SNI_VALUE='www.google.com'  # §23\r\nb=2\r\n"

    def test_absent_key_is_appended_and_none_removes_it(self):
        assert rot.rewrite_env_text('A=1', 'SNI_VALUE', GOOGLE) == 'A=1\nSNI_VALUE=www.google.com\n'
        assert rot.rewrite_env_text('A=1\n', 'SNI_VALUE', GOOGLE) == 'A=1\nSNI_VALUE=www.google.com\n'
        assert rot.rewrite_env_text('A=1\nSNI_VALUE=x\nB=2\n', 'SNI_VALUE', None) == 'A=1\nB=2\n'
        assert rot.rewrite_env_text('A=1\n', 'SNI_VALUE', None) == 'A=1\n'

    def test_duplicate_definitions_refuse(self):
        text = 'SNI_VALUE=www.bing.com\nSNI_VALUE=www.google.com\n'
        assert rot.parse_env_value(text, 'SNI_VALUE') == (GOOGLE, 2)
        with pytest.raises(ValueError):
            rot.rewrite_env_text(text, 'SNI_VALUE', CF)


# ---------------------------------------------------------------------------
# Panel inbound (both JSON-column shapes the fork returns)
# ---------------------------------------------------------------------------

REALITY = {'show': False, 'xver': 1, 'dest': 'www.bing.com:443', 'serverNames': [BING],
           'privateKey': 'PRIVATE-KEY-SECRET', 'shortIds': ['6ba85179e30d4fc2'],
           'settings': {'publicKey': 'pub', 'fingerprint': 'chrome', 'serverName': '', 'spiderX': '/'}}
CLIENTS = {'clients': [{'id': 'uuid-secret-1', 'email': 'a@nekovo.ru', 'flow': 'xtls-rprx-vision'},
                       {'id': 'uuid-secret-2', 'email': 'b@nekovo.ru', 'flow': ''}],
           'decryption': 'none'}


def inbound(as_string=True, **reality):
    rs = dict(copy.deepcopy(REALITY), **reality)
    stream = {'network': 'tcp', 'security': 'reality', 'realitySettings': rs}
    return {'id': 1, 'tag': 'inbound-443', 'protocol': 'vless', 'port': 443,
            'settings': json.dumps(CLIENTS) if as_string else copy.deepcopy(CLIENTS),
            'streamSettings': json.dumps(stream) if as_string else stream}


class TestPanelHelpers:
    @pytest.mark.parametrize('as_string', [True, False])
    def test_read_reality_reports_facts_and_no_secrets(self, as_string):
        facts = rot.read_reality(inbound(as_string))
        assert facts['dest_fields'] == {'dest': 'www.bing.com:443'}
        assert facts['server_names'] == [BING]
        assert (facts['clients'], facts['with_flow']) == (2, 1)
        dumped = json.dumps(facts)
        assert 'PRIVATE-KEY-SECRET' not in dumped and 'uuid-secret' not in dumped

    def test_target_key_of_newer_panels_is_read_and_written(self):
        rs = copy.deepcopy(REALITY)
        rs['target'] = rs.pop('dest')
        ib = inbound(True)
        ib['streamSettings'] = json.dumps({'network': 'tcp', 'security': 'reality', 'realitySettings': rs})
        assert rot.read_reality(ib)['dest_fields'] == {'target': 'www.bing.com:443'}
        stream = rot.apply_reality_changes(rot.parse_blob(ib['streamSettings']),
                                           {'dest_fields': {'target': 'www.google.com:443'},
                                            'server_names': [GOOGLE], 'settings_server_name': ''})
        assert stream['realitySettings']['target'] == 'www.google.com:443'
        assert 'dest' not in stream['realitySettings']

    def test_apply_changes_only_what_a_rotation_owns(self):
        stream = rot.parse_blob(inbound(True)['streamSettings'])
        rot.apply_reality_changes(stream, {'dest_fields': {'dest': 'www.google.com:443'},
                                           'server_names': [GOOGLE], 'settings_server_name': None})
        rs = stream['realitySettings']
        assert (rs['dest'], rs['serverNames']) == ('www.google.com:443', [GOOGLE])
        for key in ('privateKey', 'shortIds', 'xver', 'show'):
            assert rs[key] == REALITY[key]
        assert rs['settings'] == REALITY['settings']          # None = leave the share-link hint alone

    def test_same_panel_ignores_case_but_not_values(self):
        a = {'dest_fields': {'dest': 'www.bing.com:443'}, 'server_names': ['WWW.BING.COM'], 'settings_server_name': ''}
        b = {'dest_fields': {'dest': 'www.bing.com:443'}, 'server_names': [BING], 'settings_server_name': ''}
        assert rot.same_panel(a, b)
        assert not rot.same_panel(a, dict(b, dest_fields={'dest': 'www.bing.com:8443'}))


class TestSharedHelpersAreSelfContained:
    """They run on the hosts from inspect.getsource — no module globals."""

    @pytest.mark.parametrize('func,args', [
        ('parse_acl_lines', (PROD_ACL, 'is_reality_sni')),
        ('parse_env_value', ('SNI_VALUE=x', 'SNI_VALUE')),
        ('rewrite_env_text', ('SNI_VALUE=x\n', 'SNI_VALUE', 'y')),
        ('rewrite_acl_text', (PROD_ACL, 'is_reality_sni', [[GOOGLE]])),
        ('read_reality', (inbound(True),)),
        ('probe_reality_server_name', ({'outbounds': [{'tls': {'reality': {'enabled': True}, 'server_name': BING}}]},)),
        ('xray_reality_for_tag', ({'inbounds': []}, 'inbound-443')),
    ])
    def test_runs_from_source_alone(self, func, args):
        deps = {
            'rewrite_acl_text': [rot.parse_acl_lines], 'parse_env_value': [rot._env_assignment],
            'rewrite_env_text': [rot._env_assignment], 'read_reality': [rot.parse_blob],
        }.get(func, [])
        ns = {}
        for f in deps + [getattr(rot, func)]:
            exec(textwrap.dedent(inspect.getsource(f)), ns)
        assert ns[func](*args) == getattr(rot, func)(*args)

    def test_every_remote_script_compiles(self):
        o = opts('--sni', GOOGLE)
        drivers = [(rot.ENTRY_READ, (rot.parse_acl_lines, rot._env_assignment, rot.parse_env_value,
                                     rot.probe_reality_server_name)),
                   (rot.PANEL, rot.PANEL_FUNCS), (rot.EXIT_PROBE, (rot.xray_reality_for_tag,)),
                   (rot.ENTRY_TLS, (rot._env_assignment, rot.parse_env_value)),
                   (rot.HAPROXY_WRITE, rot.ACL_FUNCS), (rot.ENV_WRITE, rot.ENV_FUNCS),
                   (rot.BOT_RECREATE, ()), (rot.PROBE_REGEN, (rot.probe_reality_server_name,))]
        for driver, funcs in drivers:
            script = rot.remote_script(driver, {'sni': o.sni, 'quote': "it's \"odd\""}, funcs)
            compile(script, '<remote>', 'exec')


# ---------------------------------------------------------------------------
# Candidate verdict — the §23 threshold
# ---------------------------------------------------------------------------

class TestCandidateVerdict:
    def test_threshold_mirrors_the_health_check(self):
        assert rot.CERT_RECORD_LIMIT == hc.CERT_LIMIT_BYTES == 8000

    @pytest.mark.parametrize('length,ok', [
        (2520, True), (3920, True), (8000, True),       # google, bing, the edge itself
        (8001, False), (8100, False), (8273, False),    # past the margin; microsoft 2026-07-20
    ])
    def test_record_size_edge(self, length, ok):
        problems = rot.cert_problems([sample(length=length)] * 3)
        assert (problems == []) is ok, problems
        if not ok:
            assert f'{length} Б > 8000' in problems[0]

    def test_judged_on_the_worst_sample(self):
        # §23: CDN edges serve different chains — one big edge is enough to kill Reality
        problems = rot.cert_problems([sample(length=3920), sample(length=8273), sample(length=3920)])
        assert any('8273' in p for p in problems)

    @pytest.mark.parametrize('kw,needle', [
        ({'tls13': False}, 'TLS 1.3 не установился'),
        ({'alpn': None}, 'ALPN h2'),
        ({'alpn': 'http/1.1'}, 'ALPN h2'),
        ({'verify': '62 (hostname mismatch)'}, 'hostname mismatch'),
    ])
    def test_tls13_h2_and_valid_cert_are_required(self, kw, needle):
        problems = rot.cert_problems([sample(**kw)])
        assert any(needle in p for p in problems), problems

    def test_unreachable_candidate(self):
        s = rot.parse_tls_sample({'rc': 1, 'lines': ['40C7:error:10080002:BIO routines::getaddrinfo '
                                                     'www.nope.example: Name or service not known']})
        assert not s['tls13'] and 'getaddrinfo' in s['error']
        assert rot.cert_problems([s])[0].startswith('TLS 1.3 не установился в 1/1')

    def test_no_samples_is_a_problem_not_a_pass(self):
        assert rot.cert_problems([]) != []

    def test_parse_real_openssl_lines(self):
        s = rot.parse_tls_sample({'rc': 0, 'lines': [
            '<<< TLS 1.3, Handshake [length 0f47], Certificate',
            'New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384',
            'ALPN protocol: h2', 'Verify return code: 0 (ok)']})
        assert (s['record_len'], s['tls13'], s['alpn'], s['verify']) == (0x0f47, True, 'h2', 0)


# ---------------------------------------------------------------------------
# CLI argument rules
# ---------------------------------------------------------------------------

class TestOptions:
    def test_defaults_are_the_operator_aliases(self):
        o = opts('--sni', 'WWW.Google.com.')
        assert (o.sni, o.dest_resolved, o.entry, o.exit_host) == (GOOGLE, 'www.google.com:443', 'entry', 'vpn-exit')
        assert o.env_file == '/opt/vpn-bot/.env'
        assert o.probe_cfg == '/opt/vpn-bot/probe-proxy/config.json'

    @pytest.mark.parametrize('argv', [
        [],                                            # check needs --sni
        ['--sni', '1.2.3.4'],                          # an IP is not an SNI
        ['--sni', 'bad_host.com'],
        ['--sni', 'www.google.com;id'],
        ['--sni', GOOGLE, '--dest', 'www.google.com:99999'],
        ['--rollback', '--sni', GOOGLE],               # rollback takes values from the snapshot
        ['--sni', GOOGLE, '--apply', '--verify'],
        ['--sni', GOOGLE, '--env-file', 'relative/.env'],
        ['--sni', GOOGLE, '--haproxy-cfg', '/etc/haproxy/my cfg'],
        ['--sni', GOOGLE, '--cert-samples', '0'],
    ])
    def test_rejected(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            opts(*argv)
        assert exc.value.code == 2

    def test_dest_may_differ_from_sni(self):
        assert opts('--sni', GOOGLE, '--dest', '142.250.0.1:443').dest_resolved == '142.250.0.1:443'


# ---------------------------------------------------------------------------
# Flows on the fixture world (FakeIO — the ROTATE_FAKE=1 world)
# ---------------------------------------------------------------------------

def run(argv, fio, tmp_path, answer='yes'):
    """main() against the fake world; the snapshot lives in tmp_path."""
    out, err = io.StringIO(), io.StringIO()

    def confirm(prompt):
        if answer is None:
            raise AssertionError(f'confirmation asked in a read-only mode: {prompt}')
        return answer

    rc = rot.main(list(argv) + ['--snapshot', str(tmp_path / 'snap.json')], io=fio, confirm=confirm,
                  out=out, err=err, env={})
    return rc, out.getvalue()


def itog(out):
    """The run's one result line (§28 output contract: exactly one ИТОГ)."""
    lines = [x for x in out.splitlines() if x.startswith('ИТОГ:')]
    assert len(lines) == 1, out
    return lines[0]


def writes(fio):
    return [op for op, _ in fio.calls if op in rot.WRITE_OPS]


def snap(tmp_path):
    with open(tmp_path / 'snap.json', encoding='utf-8') as fh:
        return json.load(fh)


def layers(fio):
    """The values a rotation owns, from the fake world."""
    w = fio.w
    return (w['panel']['dest_fields'], w['panel']['server_names'], w['haproxy']['acl_lines'],
            w['env']['sni'], w['bot']['container_sni'], w['probe']['server_name'])


ORIGINAL = ({'dest': 'www.bing.com:443'}, [BING], [[BING, GOOGLE]], BING, BING, BING)
ROTATED = ({'dest': 'www.google.com:443'}, [GOOGLE], [[GOOGLE]], GOOGLE, GOOGLE, GOOGLE)


class TestCheck:
    def test_plan_for_the_prod_shape(self, tmp_path):
        fio = rot.FakeIO()
        rc, out = run(['--sni', GOOGLE], fio, tmp_path, answer=None)
        assert rc == 0, out
        first = itog(out)
        assert out.splitlines()[0] == first                                 # result first
        assert first.startswith('ИТОГ: кандидат www.google.com годен (Certificate 2520 Б')
        assert 'www.bing.com www.google.com → www.google.com' in out       # ACL drops bing in place
        assert 'SNI_VALUE: www.bing.com → www.google.com' in out
        assert 'перестанут проходить Reality' in out                       # the stale-client warning
        assert writes(fio) == [] and not (tmp_path / 'snap.json').exists()

    def test_oversized_candidate_is_exit_1(self, tmp_path):
        rc, out = run(['--sni', MS], rot.FakeIO(), tmp_path, answer=None)
        assert rc == 1
        assert itog(out) == ('ИТОГ: ПРОВЕРКА НЕ ПРОШЛА — кандидат www.microsoft.com: '
                                       'Certificate-запись 8273 Б > 8000 (буфер xtls/reality — 8192, §23)')

    def test_unreachable_candidate_is_exit_1(self, tmp_path):
        rc, out = run(['--sni', 'www.nope.example'], rot.FakeIO(), tmp_path, answer=None)
        assert rc == 1 and 'getaddrinfo' in itog(out)

    @pytest.mark.parametrize('op', ['read_panel', 'read_entry', 'probe_exit'])
    def test_a_layer_that_cannot_be_read_is_exit_2(self, op, tmp_path):
        fio = rot.FakeIO(fail=[op])
        rc, out = run(['--sni', GOOGLE], fio, tmp_path, answer=None)
        assert rc == 2 and itog(out).startswith('ИТОГ: НЕ СМОГ ПОСМОТРЕТЬ')

    def test_a_dead_bot_is_named_as_the_reason_the_panel_is_blind(self, tmp_path):
        fio = rot.FakeIO()
        fio.w['bot']['state'] = 'exited'
        rc, out = run(['--sni', GOOGLE], fio, tmp_path, answer=None)
        assert rc == 2 and 'docker compose up -d --no-deps vpn-bot' in out

    @pytest.mark.parametrize('mutate,needle', [
        (lambda w: w['panel'].update(security='tls'), 'не VLESS-Reality'),
        (lambda w: w['panel'].update(dest_fields={}), 'ни dest, ни target'),
        (lambda w: w['haproxy'].update(acl_lines=[]), 'нет активной строки'),
        (lambda w: w['haproxy'].update(active='failed'), 'haproxy на entry не active'),
        (lambda w: w['env'].update(count=2), 'SNI_VALUE задан'),
        (lambda w: w['bot'].update(health='unhealthy'), 'бот не healthy'),
    ])
    def test_shapes_this_playbook_does_not_edit_are_exit_1(self, mutate, needle, tmp_path):
        fio = rot.FakeIO()
        mutate(fio.w)
        rc, out = run(['--sni', GOOGLE], fio, tmp_path, answer=None)
        assert rc == 1 and needle in itog(out), out

    def test_nothing_to_do_when_every_layer_is_already_there(self, tmp_path):
        fio = rot.FakeIO()
        rc, out = run(['--sni', BING], fio, tmp_path)
        assert rc == 0 and 'все слои уже на www.bing.com' in itog(out)
        rc, out = run(['--sni', BING, '--apply'], fio, tmp_path, answer=None)   # never asks
        assert rc == 0 and writes(fio) == [] and not (tmp_path / 'snap.json').exists()

    def test_a_stopped_probe_proxy_is_announced_before_the_run(self, tmp_path):
        fio = rot.FakeIO()
        fio.w['probe']['container'] = 'exited'
        rc, out = run(['--sni', GOOGLE], fio, tmp_path, answer=None)
        assert rc == 0 and 'probe-proxy не запущен (exited)' in out

    def test_unset_sni_value_is_called_out(self, tmp_path):
        fio = rot.FakeIO()
        fio.w['env']['sni'] = None
        rc, out = run(['--sni', GOOGLE], fio, tmp_path, answer=None)
        assert 'SNI_VALUE не задан' in out and 'www.microsoft.com' in out
        assert 'SNI_VALUE: (не задан) → www.google.com' in out

    def test_layers_already_out_of_sync_are_named(self, tmp_path):
        fio = rot.FakeIO()
        fio.w['env']['sni'] = MS                  # someone edited .env and did not recreate
        rc, out = run(['--sni', GOOGLE], fio, tmp_path, answer=None)
        assert 'УЖЕ рассинхронизированы' in out and 'без пересоздания' in out

    def test_keep_old_sni_plan_and_its_honesty_check(self, tmp_path):
        fio = rot.FakeIO()
        rc, out = run(['--sni', GOOGLE, '--keep-old-sni'], fio, tmp_path, answer=None)
        assert rc == 0
        assert 'serverNames' in out and 'www.bing.com → www.google.com www.bing.com' in out
        assert 'acl' not in [line.split(':')[0].strip() for line in out.splitlines()]   # ACL already has both
        # the fake google dest does not serve bing's name -> the option would not save old clients
        assert 'новый dest НЕ принимает старый SNI' in out


class TestApply:
    def test_layers_move_in_order_and_verify_passes(self, tmp_path):
        fio = rot.FakeIO()
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 0, out
        assert writes(fio) == APPLY_WRITES
        assert layers(fio) == ROTATED
        assert itog(out) == 'ИТОГ: ротация на www.google.com применена и проверена — verify OK'
        assert out.startswith('ПЛАН: кандидат www.google.com годен')      # the plan the operator confirmed
        s = snap(tmp_path)
        assert s['status'] == 'applied'
        assert (s['old']['env']['sni'], s['new']['env']['sni']) == (BING, GOOGLE)
        assert s['old']['haproxy']['acl_lines'] == [[BING, GOOGLE]]

    def test_panel_update_requests_the_xray_restart_unless_told_not_to(self, tmp_path):
        fio = rot.FakeIO()
        run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert [kw['restart'] for op, kw in fio.calls if op == 'panel_set'] == [True]
        fio2 = rot.FakeIO()
        rc, out = run(['--sni', GOOGLE, '--apply', '--no-xray-restart'], fio2, tmp_path / 'x' if False else tmp_path / 'b')
        assert [kw['restart'] for op, kw in fio2.calls if op == 'panel_set'] == [False]
        # without a restart exit's config.json lags: that is a warning, not a failed rotation
        assert rc == 0 and '[WARN] exit config.json' in out

    def test_snapshot_is_on_disk_before_the_first_write(self, tmp_path):
        fio = rot.FakeIO()
        seen = []

        def on_write(op):
            if not seen:
                s = snap(tmp_path)
                seen.append((op, s['status'], s['old']['env']['sni'], s['old']['panel']['dest_fields']))
        fio.on_write = on_write
        run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert seen == [('panel_set', 'pending', BING, {'dest': 'www.bing.com:443'})]

    @pytest.mark.parametrize('answer', ['no', '', 'y', 'да', 'yes please', 'YES'])
    def test_nothing_happens_without_a_typed_yes(self, answer, tmp_path):
        fio = rot.FakeIO()
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path, answer=answer)
        assert rc == 1 and 'отменено оператором — ничего не изменено' in out
        assert writes(fio) == [] and layers(fio) == ORIGINAL
        assert not (tmp_path / 'snap.json').exists()

    def test_check_failure_never_reaches_the_prompt(self, tmp_path):
        fio = rot.FakeIO()
        rc, out = run(['--sni', MS, '--apply'], fio, tmp_path, answer=None)
        assert rc == 1 and writes(fio) == [] and 'Не применяю' in out

    def test_haproxy_failure_stops_and_reverts_the_panel(self, tmp_path):
        fio = rot.FakeIO(fail=['haproxy_set'])
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 1
        assert writes(fio) == ['panel_set', 'haproxy_set', 'panel_set']   # .env/bot/probe never touched
        revert = [kw for op, kw in fio.calls if op == 'panel_set'][1]
        assert revert['target']['dest_fields'] == {'dest': 'www.bing.com:443'}
        assert layers(fio) == ORIGINAL
        assert snap(tmp_path)['status'] == 'reverted'
        assert 'уже сделанное откачено' in itog(out)

    def test_bot_failure_reverts_in_reverse_order(self, tmp_path):
        fio = rot.FakeIO(fail={'bot_recreate': 1})          # the first recreate fails, the revert's works
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 1
        assert writes(fio) == ['panel_set', 'haproxy_set', 'env_set', 'bot_recreate'] + ROLLBACK_WRITES
        assert layers(fio) == ORIGINAL
        assert snap(tmp_path)['status'] == 'reverted'

    def test_without_auto_revert_the_mixed_state_is_reported_not_hidden(self, tmp_path):
        fio = rot.FakeIO(fail=['haproxy_set'])
        rc, out = run(['--sni', GOOGLE, '--apply', '--no-auto-revert'], fio, tmp_path)
        assert rc == 1 and writes(fio) == ['panel_set', 'haproxy_set']
        assert fio.w['panel']['server_names'] == [GOOGLE]
        assert snap(tmp_path)['status'] == 'failed'
        assert 'рассинхронизированы: --rollback' in itog(out)

    def test_probe_failure_keeps_the_rotation_and_a_rerun_finishes_it(self, tmp_path):
        fio = rot.FakeIO(fail={'probe_regen': 1})
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 1 and writes(fio) == APPLY_WRITES      # no revert: users are already fine
        assert snap(tmp_path)['status'] == 'partial'
        assert 'probe-proxy не обновлён' in itog(out)
        fio.calls.clear()
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 0, out
        assert writes(fio) == ['probe_regen']
        assert layers(fio) == ROTATED and snap(tmp_path)['status'] == 'applied'

    def test_resume_takes_the_target_from_the_snapshot(self, tmp_path):
        # Stopped between panel and HAProxy: the panel already says google only.
        # A plan computed from THIS world would no longer know bing must leave
        # the ACL — the snapshot does.
        fio = rot.FakeIO(fail=['haproxy_set'])
        run(['--sni', GOOGLE, '--apply', '--no-auto-revert'], fio, tmp_path)
        fio.fail.clear()
        fio.calls.clear()
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 0, out
        assert [kw['target'] for op, kw in fio.calls if op == 'haproxy_set'] == [[[GOOGLE]]]
        assert layers(fio) == ROTATED
        assert snap(tmp_path)['old']['haproxy']['acl_lines'] == [[BING, GOOGLE]]   # rollback still knows

    def test_an_unfinished_rotation_blocks_a_different_one(self, tmp_path):
        fio = rot.FakeIO(fail={'probe_regen': 1})
        run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        fio.calls.clear()
        rc, out = run(['--sni', CF, '--apply'], fio, tmp_path, answer=None)
        assert rc == 1 and writes(fio) == [] and 'сначала --rollback' in out
        rc, out = run(['--sni', CF], fio, tmp_path, answer=None)          # check says the same
        assert rc == 1 and 'сначала --rollback' in itog(out)
        rc, out = run(['--sni', CF, '--apply', '--force-snapshot'], fio, tmp_path)
        assert rc == 0, out
        archived = [p for p in os.listdir(tmp_path) if p.startswith('snap.') and p != 'snap.json']
        assert len(archived) == 1
        assert snap(tmp_path)['old']['env']['sni'] == GOOGLE

    def test_a_layer_changed_after_the_check_is_never_overwritten(self, tmp_path):
        fio = rot.FakeIO()

        def meddle(op):
            if op == 'panel_set' and fio.w['panel']['server_names'] == [BING]:
                fio.w['panel']['server_names'] = ['www.yahoo.com']   # an edit in the panel UI meanwhile
        fio.on_write = meddle
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 1
        assert writes(fio) == ['panel_set']            # refused before writing; nothing to revert
        assert fio.w['panel']['server_names'] == ['www.yahoo.com']
        assert 'изменилась после проверки' in out

    def test_revert_never_overwrites_what_this_run_did_not_write(self, tmp_path):
        fio = rot.FakeIO(fail=['env_set'])

        def meddle(op):
            if op == 'env_set':
                fio.w['haproxy']['acl_lines'] = [['www.yahoo.com']]   # someone fixes HAProxy by hand
        fio.on_write = meddle
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 1
        assert writes(fio) == ['panel_set', 'haproxy_set', 'env_set']   # no revert write on a foreign ACL
        assert fio.w['haproxy']['acl_lines'] == [['www.yahoo.com']]
        assert snap(tmp_path)['status'] == 'failed'

    def test_revert_leaves_a_panel_someone_else_edited_meanwhile(self, tmp_path):
        # Our panel write landed; while HAProxy was failing, someone changed the
        # panel again in the UI. The revert must not "restore" over their edit.
        fio = rot.FakeIO(fail=['haproxy_set'])

        def meddle(op):
            if op == 'haproxy_set':
                fio.w['panel']['server_names'] = ['www.yahoo.com']
        fio.on_write = meddle
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 1
        assert writes(fio) == ['panel_set', 'haproxy_set']
        assert fio.w['panel']['server_names'] == ['www.yahoo.com']
        assert snap(tmp_path)['status'] == 'failed' and 'её правили не мы' in out

    def test_corrupted_snapshot_is_exit_2(self, tmp_path):
        (tmp_path / 'snap.json').write_text('{"version": 1, "old": null')
        fio = rot.FakeIO()
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path, answer=None)
        assert rc == 2 and writes(fio) == []

    def test_interrupt_leaves_a_pending_snapshot_for_rollback(self, tmp_path):
        fio = rot.FakeIO()

        def boom(op):
            if op == 'haproxy_set':
                raise KeyboardInterrupt
        fio.on_write = boom
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 1 and 'ПРЕРВАНО' in out
        assert snap(tmp_path)['status'] == 'pending'


class TestRollback:
    def _applied(self, tmp_path):
        fio = rot.FakeIO()
        rc, out = run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        assert rc == 0, out
        fio.calls.clear()
        return fio

    def test_restores_every_layer_in_reverse_order(self, tmp_path):
        fio = self._applied(tmp_path)
        rc, out = run(['--rollback'], fio, tmp_path)
        assert rc == 0, out
        assert writes(fio) == ROLLBACK_WRITES
        assert layers(fio) == ORIGINAL
        assert snap(tmp_path)['status'] == 'rolled_back'
        assert itog(out) == 'ИТОГ: откат на www.bing.com выполнен и проверен — verify OK'
        assert out.startswith('ПЛАН ОТКАТА: вернуть www.bing.com (шагов: 5)')

    def test_the_values_come_from_the_snapshot(self, tmp_path):
        fio = self._applied(tmp_path)
        s = snap(tmp_path)
        s['old']['panel']['dest_fields'] = {'dest': 'www.bing.com:8443'}
        (tmp_path / 'snap.json').write_text(json.dumps(s))
        run(['--rollback'], fio, tmp_path)
        (panel_kw,) = [kw for op, kw in fio.calls if op == 'panel_set']
        assert panel_kw['target']['dest_fields'] == {'dest': 'www.bing.com:8443'}
        assert panel_kw['expect']['dest_fields'] == {'dest': 'www.google.com:443'}

    def test_refuses_when_a_layer_was_changed_by_someone_else(self, tmp_path):
        fio = self._applied(tmp_path)
        fio.w['env']['sni'] = 'www.yahoo.com'
        rc, out = run(['--rollback'], fio, tmp_path, answer=None)
        assert rc == 1 and writes(fio) == []
        assert 'слой изменён после ротации' in itog(out)

    def test_needs_a_typed_yes(self, tmp_path):
        fio = self._applied(tmp_path)
        rc, out = run(['--rollback'], fio, tmp_path, answer='no')
        assert rc == 1 and writes(fio) == [] and layers(fio) == ROTATED
        assert snap(tmp_path)['status'] == 'applied'

    def test_no_snapshot_is_exit_2(self, tmp_path):
        rc, out = run(['--rollback'], rot.FakeIO(), tmp_path, answer=None)
        assert rc == 2 and 'снимка нет' in out

    def test_blind_layer_is_exit_2_and_touches_nothing(self, tmp_path):
        fio = self._applied(tmp_path)
        fio.fail = {'read_panel': -1}
        rc, out = run(['--rollback'], fio, tmp_path, answer=None)
        assert rc == 2 and writes(fio) == []

    def test_second_rollback_has_nothing_to_do(self, tmp_path):
        fio = self._applied(tmp_path)
        run(['--rollback'], fio, tmp_path)
        fio.calls.clear()
        rc, out = run(['--rollback'], fio, tmp_path, answer=None)
        assert rc == 0 and writes(fio) == [] and 'откатывать нечего' in out


class TestVerify:
    def test_after_apply_from_snapshot_and_from_sni(self, tmp_path):
        fio = rot.FakeIO()
        run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        rc, out = run(['--verify'], fio, tmp_path, answer=None)
        assert rc == 0 and itog(out).startswith('ИТОГ: verify OK — все слои на www.google.com')
        rc, out = run(['--verify', '--sni', GOOGLE], fio, tmp_path, answer=None)
        assert rc == 0

    def test_before_apply_the_new_sni_fails_everywhere(self, tmp_path):
        rc, out = run(['--verify', '--sni', GOOGLE], rot.FakeIO(), tmp_path, answer=None)
        assert rc == 1
        assert itog(out).startswith('ИТОГ: verify НЕ ПРОШЁЛ — панель exit · inbound 1, .env бота, бот (контейнер), probe-proxy')

    def test_without_sni_or_snapshot_is_exit_2(self, tmp_path):
        rc, out = run(['--verify'], rot.FakeIO(), tmp_path, answer=None)
        assert rc == 2

    def test_ssh_failure_is_blind_never_ok(self, tmp_path):
        fio = rot.FakeIO()
        run(['--sni', GOOGLE, '--apply'], fio, tmp_path)
        fio.fail = {'tls_entry': -1}
        rc, out = run(['--verify'], fio, tmp_path, answer=None)
        assert rc == 2 and '[BLIND] TLS через entry' in out


def checks_for(state, target, *, xray=None, tls=None, strict=False):
    xray = xray if xray is not None else {'ok': True, 'xray': {'dest_fields': {'dest': 'www.google.com:443'},
                                                               'server_names': [GOOGLE]}}
    tls = tls if tls is not None else {'ok': True, 'addr': '203.0.113.10', 'port': '8443',
                                       'sample': tls_lines(length=2520)}
    return rot.verify_checks(state, xray, tls, target, strict)


def rotated_state():
    fio = rot.FakeIO()
    fio.w['panel'].update(dest_fields={'dest': 'www.google.com:443'}, server_names=[GOOGLE])
    fio.w['haproxy']['acl_lines'] = [[GOOGLE]]
    fio.w['env']['sni'] = fio.w['bot']['container_sni'] = fio.w['probe']['server_name'] = GOOGLE
    return rot.collect_state(fio)


class TestVerifyChecks:
    LOOSE = {'sni': GOOGLE, 'dest': 'www.google.com:443', 'server_names': None, 'acl_lines': None,
             'probe_present': None, 'clients_before': None}

    def test_all_ok(self):
        checks = checks_for(rotated_state(), self.LOOSE)
        assert rot.verify_code(checks) == 0, checks
        assert [c[0] for c in checks] == ['OK'] * 7

    def test_handshake_with_a_foreign_certificate_is_a_failure(self):
        tls = {'ok': True, 'addr': 'x', 'port': '8443', 'sample': tls_lines(verify='62 (hostname mismatch)')}
        checks = checks_for(rotated_state(), self.LOOSE, tls=tls)
        (row,) = [c for c in checks if c[1] == 'TLS через entry']
        assert row[0] == 'FAIL' and 'haproxy увёл SNI не на exit' in row[2]

    def test_incomplete_handshake_is_a_failure(self):
        tls = {'ok': True, 'addr': 'x', 'port': '8443', 'sample': tls_lines(tls13=False)}
        assert rot.verify_code(checks_for(rotated_state(), self.LOOSE, tls=tls)) == 1

    def test_runtime_lag_is_a_warning_unless_xray_was_restarted(self):
        lag = {'ok': True, 'xray': {'dest_fields': {'dest': 'www.bing.com:443'}, 'server_names': [BING]}}
        soft = checks_for(rotated_state(), self.LOOSE, xray=lag, strict=False)
        hard = checks_for(rotated_state(), self.LOOSE, xray=lag, strict=True)
        assert ('WARN', 'exit config.json') in [(c[0], c[1]) for c in soft] and rot.verify_code(soft) == 0
        assert ('FAIL', 'exit config.json') in [(c[0], c[1]) for c in hard] and rot.verify_code(hard) == 1

    def test_exact_target_compares_whole_lists(self):
        target = dict(self.LOOSE, server_names=[GOOGLE, BING], acl_lines=[[GOOGLE, BING]])
        checks = checks_for(rotated_state(), target)
        failed = [c[1] for c in checks if c[0] == 'FAIL']
        assert failed == ['панель exit · inbound 1', 'haproxy entry']

    def test_fewer_clients_after_the_update_is_flagged(self):
        target = dict(self.LOOSE, clients_before=82)
        assert ('WARN', 'панель: клиенты') in [(c[0], c[1]) for c in checks_for(rotated_state(), target)]

    def test_target_sni_unset_accepts_the_compose_default(self):
        # rolling back to "SNI_VALUE was not in .env": compose fills in its default
        fio = rot.FakeIO()
        fio.w['env']['sni'] = None
        fio.w['bot']['container_sni'] = fio.w['probe']['server_name'] = MS
        state = rot.collect_state(fio)
        target = dict(self.LOOSE, sni=None, dest='www.bing.com:443')
        statuses = {c[1]: c[0] for c in rot.verify_checks(state, {'ok': False}, {'ok': False}, target, False)}
        assert statuses['бот (контейнер)'] == 'OK' and statuses['probe-proxy'] == 'OK'
        assert statuses['TLS через entry'] == 'WARN'
        cur = rot.values_from_state(state)
        assert rot.steps_needed(cur, dict(cur, env={'sni': None}), rot.ROLLBACK_ORDER) == []


# ---------------------------------------------------------------------------
# The host-side scripts, run locally: same code, fake host binaries
# ---------------------------------------------------------------------------

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
path = os.environ['FAKE_STATE']
st = json.load(open(path))
with open(os.environ['FAKE_LOG'], 'a') as fh:
    fh.write(json.dumps(['docker'] + sys.argv[1:]) + '\n')
a = sys.argv[1:]
def save():
    json.dump(st, open(path, 'w'))
if a[0] == 'inspect':
    v = st['containers'].get(a[-1])
    if v is None:
        print('Error: No such object: ' + a[-1], file=sys.stderr); sys.exit(1)
    print(v); sys.exit(0)
if a[0] == 'exec':
    rest = a[1:]
    while rest[0].startswith('-'):
        rest = rest[2:] if rest[0] in ('-e', '-w') else rest[1:]
    name, cmd = rest[0], rest[1:]
    if st['containers'].get(name) != 'running':
        print('Error response from daemon: container %s is not running' % name, file=sys.stderr); sys.exit(1)
    if cmd[0] == 'printenv':
        v = st['container_env'].get(cmd[1])
        if v is None:
            sys.exit(1)
        print(v); sys.exit(0)
    if cmd[-1].endswith('gen_probe_config.py'):
        cfg = json.loads(json.dumps(st['gen_config']))
        for ob in cfg['outbounds']:
            if ob.get('tls', {}).get('reality'):
                ob['tls']['server_name'] = st['container_env'].get('SNI_VALUE')
        print(json.dumps(cfg)); sys.exit(0)
    if cmd[:2] == ['sing-box', 'check']:
        sys.exit(st.get('singbox_rc', 0))
    if cmd[0] == 'cat':
        print(json.dumps(st['xray_config'])); sys.exit(0)
if a[0] == 'compose':
    if st.get('compose_rc'):
        print('compose failed', file=sys.stderr); sys.exit(st['compose_rc'])
    for line in open(os.path.join(os.getcwd(), '.env')).read().splitlines():
        if line.startswith('SNI_VALUE='):
            st['container_env']['SNI_VALUE'] = line.split('=', 1)[1].strip().strip('"')
    st['containers'][a[-1]] = 'running'
    save(); sys.exit(0)
if a[0] == 'restart':
    sys.exit(st.get('restart_rc', 0))
sys.exit(0)
'''

FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
import json, os, sys
path = os.environ['FAKE_STATE']
st = json.load(open(path))
with open(os.environ['FAKE_LOG'], 'a') as fh:
    fh.write(json.dumps(['systemctl'] + sys.argv[1:]) + '\n')
if sys.argv[1] == 'is-active':
    print(st.get('active', 'active')); sys.exit(0 if st.get('active', 'active') == 'active' else 3)
if sys.argv[1] == 'reload':
    rcs = st.get('reload_rcs') or [0]
    rc = rcs.pop(0)
    st['reload_rcs'] = rcs
    st['active'] = 'active' if rc == 0 else 'failed'
    json.dump(st, open(path, 'w'))
    sys.exit(rc)
'''

FAKE_HAPROXY = r'''#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ['FAKE_STATE']))
with open(os.environ['FAKE_LOG'], 'a') as fh:
    fh.write(json.dumps(['haproxy'] + sys.argv[1:]) + '\n')
if st.get('haproxy_check_rc'):
    print('[ALERT] (1) : config : parsing [%s:3] : unknown keyword' % sys.argv[-1], file=sys.stderr)
    sys.exit(st['haproxy_check_rc'])
print('Configuration file is valid')
'''

FAKE_OPENSSL = r'''#!/usr/bin/env python3
import json, os, sys
st = json.load(open(os.environ['FAKE_STATE']))
with open(os.environ['FAKE_LOG'], 'a') as fh:
    fh.write(json.dumps(['openssl'] + sys.argv[1:]) + '\n')
a = sys.argv[1:]
host = a[a.index('-connect') + 1].rsplit(':', 1)[0]
sni = a[a.index('-servername') + 1]
info = st['tls'].get(host)
if info is None:
    print('40C7E0F1:error:10080002:BIO routines:BIO_lookup_ex:system lib: getaddrinfo %s: '
          'Name or service not known' % host, file=sys.stderr)
    print('connect:errno=0', file=sys.stderr); sys.exit(1)
print('CONNECTED(00000003)')
print('>>> TLS 1.3, Handshake [length 0200], ClientHello')
print('<<< TLS 1.3, Handshake [length 007a], ServerHello')
print('<<< TLS 1.3, Handshake [length %04x], Certificate' % info['len'])
print('    0b 00 0f 43 00 00 0f 3f 00 07 83 30 82 07 7f 30')
print('---')
print('New, TLSv1.3, Cipher is TLS_AES_256_GCM_SHA384')
print('ALPN protocol: h2')
print('Verify return code: %s' % ('0 (ok)' if info.get('names', [host]).count(sni) else '62 (hostname mismatch)'))
'''

FAKE_BOT_CONFIG = '''import os
class Settings:
    INBOUND_ID = 1
'''

FAKE_XUI_SERVICE = '''import json, logging, os
log = logging.getLogger('bot.services.xui_api.client')

class _API:
    path = os.environ['FAKE_PANEL']

    def _load(self):
        return json.load(open(self.path))

    def _save(self, d):
        json.dump(d, open(self.path, 'w'))

    async def get_inbound(self, iid):
        return self._load()['inbounds'].get(str(iid))

    async def update_inbound(self, iid, body):
        d = self._load()
        if d.get('update_fails'):
            log.warning('API returned error: fake refusal')
            return False
        d['inbounds'][str(iid)] = body
        d['updates'] = d.get('updates', 0) + 1
        self._save(d)
        return True

    async def restart_xray(self):
        d = self._load()
        d['restarts'] = d.get('restarts', 0) + 1
        self._save(d)
        # like the real panel: a restart regenerates exit's config.json from the DB
        st = json.load(open(os.environ['FAKE_STATE']))
        ib = d['inbounds']['1']
        stream = ib['streamSettings']
        stream = json.loads(stream) if isinstance(stream, str) else stream
        st['xray_config'] = {'inbounds': [{'tag': ib['tag'], 'streamSettings': stream}]}
        json.dump(st, open(os.environ['FAKE_STATE'], 'w'))
        return True

    async def close(self):
        pass

class XUIService:
    def __init__(self, cfg):
        self.api = _API()
'''

HAPROXY_CFG = f'''global
    log /dev/log local0
    stats socket /run/haproxy/admin.sock mode 660
frontend reality_in
    bind :8443
    mode tcp
    tcp-request inspect-delay 5s
    tcp-request content accept if {{ req_ssl_hello_type 1 }}
    {PROD_ACL}
    use_backend exit_reality if is_reality_sni
    default_backend decoy
listen stats
    bind 127.0.0.1:9000
    stats auth admin:HAPROXY-SECRET
'''

ENV_FILE = ('BOT_TOKEN=123456:BOT-TOKEN-SECRET\n'
            'XUI_PASSWORD="xui-secret"\n'
            'ENTRY_NODE_IP=203.0.113.10\n'
            'ENTRY_NODE_PORT=8443\n'
            'SNI_VALUE=www.bing.com\n')

PROBE_CONFIG = {'log': {'level': 'warn'}, 'inbounds': [],
                'outbounds': [{'type': 'vless', 'tag': 'probe-reality', 'uuid': 'probe-uuid-secret',
                               'tls': {'enabled': True, 'server_name': BING,
                                       'reality': {'enabled': True, 'public_key': 'pub'}}},
                              {'type': 'direct', 'tag': 'direct'}]}


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'status': 'healthy', 'version': 'test'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A fake entry+exit host in tmp_path: real files, fake binaries, a
    /health server, and a RealIO whose runner swaps ssh for a local python."""
    fakebin = tmp_path / 'bin'
    fakebin.mkdir()
    for name, body in (('docker', FAKE_DOCKER), ('systemctl', FAKE_SYSTEMCTL),
                       ('haproxy', FAKE_HAPROXY), ('openssl', FAKE_OPENSSL)):
        p = fakebin / name
        p.write_text(body)
        p.chmod(0o755)
    pkg = tmp_path / 'pkg' / 'bot' / 'services'
    pkg.mkdir(parents=True)
    (tmp_path / 'pkg' / 'bot' / '__init__.py').write_text('')
    (tmp_path / 'pkg' / 'bot' / 'config.py').write_text(FAKE_BOT_CONFIG)
    (pkg / '__init__.py').write_text('')
    (pkg / 'xui_service.py').write_text(FAKE_XUI_SERVICE)

    botdir = tmp_path / 'opt' / 'vpn-bot'
    (botdir / 'probe-proxy').mkdir(parents=True)
    (botdir / '.env').write_text(ENV_FILE)
    os.chmod(botdir / '.env', 0o640)
    (botdir / 'probe-proxy' / 'config.json').write_text(json.dumps(PROBE_CONFIG))
    cfg = tmp_path / 'etc' / 'haproxy.cfg'
    cfg.parent.mkdir()
    cfg.write_text(HAPROXY_CFG)

    state = {'containers': {'vpn-bot': 'running', 'probe-proxy': 'running', '3x-ui': 'running'},
             'container_env': {'SNI_VALUE': BING}, 'gen_config': PROBE_CONFIG,
             'tls': {BING: {'len': 3920}, GOOGLE: {'len': 2520}, MS: {'len': 8273},
                     '203.0.113.10': {'len': 3920, 'names': [BING]}},
             'xray_config': {'inbounds': [{'tag': 'inbound-443', 'streamSettings': {'realitySettings': {
                 'dest': 'www.bing.com:443', 'serverNames': [BING], 'privateKey': 'PRIVATE-KEY-SECRET'}}}]}}
    state_path = tmp_path / 'state.json'
    state_path.write_text(json.dumps(state))
    panel_path = tmp_path / 'panel.json'
    panel_path.write_text(json.dumps({'inbounds': {'1': inbound(True)}}))
    log_path = tmp_path / 'calls.log'
    log_path.write_text('')

    server = HTTPServer(('127.0.0.1', 0), _Health)
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()
    monkeypatch.setattr(rot, 'HEALTH_URL', f'http://127.0.0.1:{server.server_address[1]}/health')
    monkeypatch.setattr(rot, 'BOT_WAIT_S', 5)

    ssh_calls = []

    def runner(argv, stdin, timeout):
        assert argv[:5] == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10']
        ssh_calls.append(argv[5:])
        env = dict(os.environ, PATH=f'{fakebin}{os.pathsep}{os.environ["PATH"]}', FAKE_STATE=str(state_path),
                   FAKE_LOG=str(log_path), FAKE_PANEL=str(panel_path))
        env.pop('PYTHONPATH', None)
        if argv[6:] == rot.container_py(rot.BOT_CONTAINER):
            env['PYTHONPATH'] = str(tmp_path / 'pkg')        # "inside the container": the bot package
        else:
            assert argv[6:] == rot.HOST_PY
        p = subprocess.run([REMOTE_PY, '-'], input=stdin, capture_output=True, text=True,
                           timeout=60, env=env, cwd=str(tmp_path))
        return p.returncode, p.stdout, p.stderr

    o = opts('--sni', GOOGLE, '--haproxy-cfg', str(cfg), '--bot-dir', str(botdir))

    class Host:
        io = rot.RealIO(o, runner=runner)
        calls = ssh_calls

        @staticmethod
        def state():
            return json.loads(state_path.read_text())

        @staticmethod
        def set(**kw):
            s = json.loads(state_path.read_text())
            s.update(kw)
            state_path.write_text(json.dumps(s))

        @staticmethod
        def panel():
            return json.loads(panel_path.read_text())

        @staticmethod
        def log():
            return [json.loads(x) for x in log_path.read_text().splitlines()]

    Host.cfg, Host.botdir, Host.opts = cfg, botdir, o
    yield Host
    server.shutdown()


class TestHostScripts:
    def test_entry_read_sees_every_layer_and_leaks_nothing(self, host):
        res = host.io.read_entry()
        assert res['ok'] and host.calls == [['entry', 'python3', '-']]
        assert res['haproxy']['acl_lines'] == [[BING, GOOGLE]] and res['haproxy']['active'] == 'active'
        assert res['haproxy']['writable'] and res['haproxy']['haproxy_bin']
        assert (res['env']['sni'], res['env']['count'], res['env']['entry_port']) == (BING, 1, '8443')
        assert (res['bot']['state'], res['bot']['container_sni'], res['bot']['health']) == ('running', BING, 'healthy')
        assert (res['probe']['present'], res['probe']['server_name']) == (True, BING)
        dumped = json.dumps(res)
        for secret in ('BOT-TOKEN-SECRET', 'xui-secret', 'HAPROXY-SECRET', 'probe-uuid-secret'):
            assert secret not in dumped

    def test_panel_read_and_update_through_the_bot_package(self, host):
        res = host.io.read_panel()
        assert res['ok'] and host.calls[-1][0] == 'entry' and host.calls[-1][1:4] == ['docker', 'exec', '-i']
        cur = res['inbound']
        target = rot.plan_target({'panel': cur, 'haproxy': {'acl_lines': [[BING]]},
                                  'probe': {'present': True}}, GOOGLE, 'www.google.com:443', False)['panel']
        upd = host.io.panel_set(cur, target, True)
        assert upd['ok'] and upd['restarted'] and upd['after']['server_names'] == [GOOGLE]
        stored = host.panel()
        assert (stored['updates'], stored['restarts']) == (1, 1)
        ib = stored['inbounds']['1']
        assert isinstance(ib['streamSettings'], str)            # same column shape back
        rs = json.loads(ib['streamSettings'])['realitySettings']
        assert (rs['dest'], rs['serverNames'], rs['privateKey']) == ('www.google.com:443', [GOOGLE], 'PRIVATE-KEY-SECRET')
        assert ib['settings'] == inbound(True)['settings']       # clients posted back verbatim
        assert 'PRIVATE-KEY-SECRET' not in json.dumps(upd)

    def test_panel_update_refuses_a_changed_panel(self, host):
        cur = host.io.read_panel()['inbound']
        stale = dict(cur, server_names=['www.yahoo.com'])
        res = host.io.panel_set(stale, dict(cur, server_names=[GOOGLE]), True)
        assert not res['ok'] and res['conflict'] and 'updates' not in host.panel()

    def test_panel_refusal_surfaces_the_panel_log(self, host):
        p = host.panel()
        p['update_fails'] = True
        (host.cfg.parent.parent / 'panel.json').write_text(json.dumps(p))
        cur = host.io.read_panel()['inbound']
        res = host.io.panel_set(cur, dict(cur, server_names=[GOOGLE]), True)
        assert not res['ok'] and 'fake refusal' in rot.step_detail('panel', res)

    def test_haproxy_rewrite_checks_reloads_and_keeps_a_backup(self, host):
        res = host.io.haproxy_set([[BING, GOOGLE]], [[GOOGLE]])
        assert res['ok'] and res['changed'] and res['acl_lines'] == [[GOOGLE]]
        assert host.cfg.read_text() == HAPROXY_CFG.replace('www.bing.com www.google.com', 'www.google.com')
        assert open(res['backup']).read() == HAPROXY_CFG
        log = host.log()
        assert log[0][:3] == ['haproxy', '-c', '-f'] and log[0][3].endswith('.rotate-new')
        assert ['systemctl', 'reload', 'haproxy'] in log
        assert not os.path.exists(str(host.cfg) + '.rotate-new')

    def test_a_second_write_in_the_same_run_keeps_the_first_backup(self, host):
        # apply and its auto-revert share one stamp: the ORIGINAL must survive
        first = host.io.haproxy_set([[BING, GOOGLE]], [[GOOGLE]])
        second = host.io.haproxy_set([[GOOGLE]], [[BING, GOOGLE]])
        assert first['backup'] != second['backup']
        assert open(first['backup']).read() == HAPROXY_CFG
        assert host.cfg.read_text() == HAPROXY_CFG
        e1 = host.io.env_set(BING, GOOGLE)
        e2 = host.io.env_set(GOOGLE, BING)
        assert e1['backup'] != e2['backup'] and open(e1['backup']).read() == ENV_FILE

    def test_haproxy_check_failure_leaves_the_file_untouched(self, host):
        host.set(haproxy_check_rc=1)
        res = host.io.haproxy_set([[BING, GOOGLE]], [[GOOGLE]])
        assert not res['ok'] and res['stage'] == 'check' and 'unknown keyword' in res['error']
        assert host.cfg.read_text() == HAPROXY_CFG
        assert not os.path.exists(str(host.cfg) + '.rotate-new')
        assert ['systemctl', 'reload', 'haproxy'] not in host.log()

    def test_haproxy_reload_failure_puts_the_file_back(self, host):
        host.set(reload_rcs=[1, 0])
        res = host.io.haproxy_set([[BING, GOOGLE]], [[GOOGLE]])
        assert not res['ok'] and res['stage'] == 'reload' and res['restored']
        assert res['active_after_restore'] == 'active'
        assert host.cfg.read_text() == HAPROXY_CFG

    def test_haproxy_refuses_a_changed_acl(self, host):
        res = host.io.haproxy_set([[BING]], [[GOOGLE]])
        assert not res['ok'] and res['conflict'] and host.cfg.read_text() == HAPROXY_CFG

    def test_env_rewrite_keeps_mode_and_backs_up_privately(self, host):
        env = host.botdir / '.env'
        res = host.io.env_set(BING, GOOGLE)
        assert res['ok'] and res['value'] == GOOGLE
        assert env.read_text() == ENV_FILE.replace('SNI_VALUE=www.bing.com', 'SNI_VALUE=www.google.com')
        assert oct(os.stat(env).st_mode & 0o777) == oct(0o640)
        assert oct(os.stat(res['backup']).st_mode & 0o777) == oct(0o600)
        assert open(res['backup']).read() == ENV_FILE
        assert 'BOT-TOKEN-SECRET' not in json.dumps(res)

    def test_env_refuses_a_changed_value(self, host):
        res = host.io.env_set(MS, GOOGLE)
        assert not res['ok'] and res['conflict']
        assert (host.botdir / '.env').read_text() == ENV_FILE

    def test_bot_recreate_is_no_deps_force_recreate_and_waits_for_the_new_env(self, host):
        host.io.env_set(BING, GOOGLE)
        res = host.io.bot_recreate(GOOGLE)
        assert res['ok'] and (res['health'], res['container_sni']) == ('healthy', GOOGLE)
        # --no-deps: never drag 3x-ui along (2026-07-19); --force-recreate: a restart keeps
        # the OLD env; --no-build: no surprise build/pull in the middle of an incident
        assert ['docker', 'compose', 'up', '-d', '--no-deps', '--force-recreate', '--no-build', 'vpn-bot'] in host.log()

    def test_bot_recreate_with_the_old_env_is_a_failure(self, host):
        res = host.io.bot_recreate(GOOGLE)                     # .env still says bing
        assert not res['ok'] and 'www.bing.com' in res['error']

    def test_probe_regen_checks_then_swaps_then_restarts(self, host):
        host.io.env_set(BING, GOOGLE)
        host.io.bot_recreate(GOOGLE)
        res = host.io.probe_regen(GOOGLE)
        assert res['ok'] and res['server_name'] == GOOGLE
        live = json.loads((host.botdir / 'probe-proxy' / 'config.json').read_text())
        assert rot.probe_reality_server_name(live) == GOOGLE
        assert json.loads(open(res['backup']).read()) == PROBE_CONFIG
        log = host.log()
        check = [x for x in log if x[:3] == ['docker', 'exec', 'probe-proxy']][0]
        assert check[3:6] == ['sing-box', 'check', '-c'] and check[6] == '/etc/sing-box/config.json.rotate-new'
        assert ['docker', 'restart', 'probe-proxy'] in log
        assert 'probe-uuid-secret' not in json.dumps(res)

    def test_probe_regen_with_a_bad_config_keeps_the_live_one(self, host):
        host.io.env_set(BING, GOOGLE)
        host.io.bot_recreate(GOOGLE)
        host.set(singbox_rc=1)
        res = host.io.probe_regen(GOOGLE)
        assert not res['ok'] and 'sing-box check' in res['error']
        assert json.loads((host.botdir / 'probe-proxy' / 'config.json').read_text()) == PROBE_CONFIG
        assert ['docker', 'restart', 'probe-proxy'] not in host.log()

    def test_exit_probe_runs_openssl_the_way_xray_dials(self, host):
        jobs = [{'tag': 'candidate', 'host': GOOGLE, 'port': 443, 'sni': GOOGLE, 'samples': 2},
                {'tag': 'current', 'host': BING, 'port': 443, 'sni': BING, 'samples': 1}]
        res = host.io.probe_exit(jobs, 'inbound-443')
        assert res['ok'] and host.calls[-1] == ['vpn-exit', 'python3', '-']
        cand = [rot.parse_tls_sample(s) for s in res['tls']['candidate']]
        assert [s['record_len'] for s in cand] == [2520, 2520] and rot.cert_problems(cand) == []
        assert rot.parse_tls_sample(res['tls']['current'][0])['record_len'] == 3920
        assert res['xray'] == {'dest_fields': {'dest': 'www.bing.com:443'}, 'server_names': [BING]}
        argv = [x for x in host.log() if x[0] == 'openssl'][0]
        assert argv[1:3] == ['s_client', '-4']
        for flag in ('-tls1_3', '-msg'):
            assert flag in argv
        assert argv[argv.index('-alpn') + 1] == 'h2'
        assert argv[argv.index('-servername') + 1] == GOOGLE == argv[argv.index('-verify_hostname') + 1]

    def test_entry_tls_probe_dials_the_public_reality_port(self, host):
        res = host.io.tls_entry(BING)
        assert res['ok'] and (res['addr'], res['port']) == ('203.0.113.10', '8443')
        s = rot.parse_tls_sample(res['sample'])
        assert s['tls13'] and s['verify'] == 0

    def test_entry_tls_probe_prefers_what_the_running_bot_hands_out(self, host):
        # entry's hand-tuned compose may set the Reality port outside .env
        st = host.state()
        st['container_env'].update(ENTRY_NODE_IP='198.51.100.7', ENTRY_NODE_PORT='9443')
        st['tls']['198.51.100.7'] = {'len': 3920, 'names': [BING]}
        host.set(**st)
        res = host.io.tls_entry(BING)
        assert (res['addr'], res['port'], res['source']) == ('198.51.100.7', '9443', 'env контейнера vpn-bot')

    def test_full_apply_and_rollback_on_the_fake_host(self, host, tmp_path):
        """End to end through RealIO: every remote script in its real order."""
        host.set(tls=dict(host.state()['tls'], **{'203.0.113.10': {'len': 2520, 'names': [GOOGLE, BING]}}))
        out, err = io.StringIO(), io.StringIO()
        argv = ['--sni', GOOGLE, '--apply', '--haproxy-cfg', str(host.cfg), '--bot-dir', str(host.botdir),
                '--snapshot', str(tmp_path / 'snap.json')]
        rc = rot.main(argv, io=host.io, confirm=lambda p: 'yes', out=out, err=err, env={})
        text = out.getvalue()
        assert rc == 0, text
        assert itog(text) == 'ИТОГ: ротация на www.google.com применена и проверена — verify OK'
        assert 'SNI_VALUE=www.google.com' in (host.botdir / '.env').read_text()
        assert 'acl is_reality_sni req_ssl_sni -i www.google.com\n' in host.cfg.read_text()
        out2 = io.StringIO()
        rc = rot.main(['--rollback', '--haproxy-cfg', str(host.cfg), '--bot-dir', str(host.botdir),
                       '--snapshot', str(tmp_path / 'snap.json')],
                      io=host.io, confirm=lambda p: 'yes', out=out2, err=err, env={})
        assert rc == 0, out2.getvalue()
        assert itog(out2.getvalue()) == 'ИТОГ: откат на www.bing.com выполнен и проверен — verify OK'
        assert (host.botdir / '.env').read_text() == ENV_FILE
        assert host.cfg.read_text() == HAPROXY_CFG
        rs = json.loads(host.panel()['inbounds']['1']['streamSettings'])['realitySettings']
        assert (rs['dest'], rs['serverNames']) == ('www.bing.com:443', [BING])


class TestCommandLine:
    def test_help(self):
        p = subprocess.run([sys.executable, SCRIPT, '--help'], capture_output=True, text=True, timeout=60)
        assert p.returncode == 0 and 'ROTATE_FAKE' in p.stdout and '--rollback' in p.stdout

    def test_fake_dry_run_needs_no_host(self, tmp_path):
        env = dict(os.environ, ROTATE_FAKE='1', TMPDIR=str(tmp_path))
        p = subprocess.run([sys.executable, SCRIPT, '--sni', GOOGLE], capture_output=True, text=True,
                           timeout=60, env=env)
        assert p.returncode == 0, p.stdout + p.stderr
        assert p.stdout.splitlines()[0].startswith('ИТОГ: кандидат www.google.com годен')
        assert '[ROTATE_FAKE]' in p.stderr

    def test_unknown_fake_failure_is_refused(self, tmp_path):
        env = dict(os.environ, ROTATE_FAKE='1', ROTATE_FAKE_FAIL='nope', TMPDIR=str(tmp_path))
        p = subprocess.run([sys.executable, SCRIPT, '--sni', GOOGLE], capture_output=True, text=True,
                           timeout=60, env=env)
        assert p.returncode != 0 and 'ROTATE_FAKE_FAIL' in p.stderr
