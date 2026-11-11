"""scripts/caddy_entry_hop.py — exit's Caddy onto the hop's HTTPS listener
(AGENTS.md §53). The transform on Caddyfile text, and --apply with a fake
``caddy validate``; the real Caddy 2.11.3 loads the result in
tests/integration/test_hop_tls_stand.py.
"""

import importlib.util
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location('caddy_entry_hop',
                                               REPO / 'scripts' / 'caddy_entry_hop.py')
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

PIN = '/etc/nekovpn-hop/entry-hop.pem'
KW = dict(entry='130.49.146.10', old_port=8080, port=8448, pin=PIN, expect=2)
SNIPPET = tool.snippet('130.49.146.10', 8448, PIN)

DASH = 'dash.example.com:9443 {\n\treverse_proxy 130.49.146.10:8080\n}\n'
MIRROR = ('sub.example.xyz:2096 {\n\ttls /etc/ssl/sub/fullchain.pem /etc/ssl/sub/key.pem\n'
          '\thandle_path /rule-sets/* {\n\t\troot * /var/www/rule-sets\n\t\tfile_server\n\t}\n'
          '\thandle {\n\t\treverse_proxy 130.49.146.10:8080\n\t}\n}\n')


class TestTransform:

    def test_snippet(self):
        assert SNIPPET == (
            '(entry_hop) {\n'
            '\treverse_proxy https://130.49.146.10:8448 {\n'
            '\t\ttransport http {\n'
            '\t\t\ttls_server_name entry-hop.invalid\n'
            f'\t\t\ttls_trust_pool file {PIN}\n'
            '\t\t}\n'
            '\t}\n'
            '}\n')

    def test_without_a_global_block(self):
        out = tool.transform(DASH + '\n' + MIRROR, **KW)
        assert out == SNIPPET + '\n' + (DASH + '\n' + MIRROR).replace(
            'reverse_proxy 130.49.146.10:8080', 'import entry_hop')

    def test_after_the_global_block(self):
        glob = '# exit\n{\n\temail ops@example.com\n\tservers {\n\t\tprotocols h1 h2\n\t}\n}\n'
        out = tool.transform(glob + '\n' + DASH + MIRROR, **KW)
        assert out.startswith(glob + '\n' + SNIPPET)
        assert out.count('import entry_hop') == 2 and '8080' not in out

    def test_an_http_scheme_and_trailing_space(self):
        text = DASH.replace('reverse_proxy 130.49.146.10:8080',
                            'reverse_proxy http://130.49.146.10:8080  ') + MIRROR
        assert tool.transform(text, **KW).count('import entry_hop') == 2

    @pytest.mark.parametrize('text,why', [
        (DASH, 'found 1'),
        (DASH + MIRROR + DASH.replace('dash.', 'other.'), 'found 3'),
        (DASH.replace('8080\n', '8080 {\n\t\theader_up X-Real-IP {remote}\n\t}\n') + MIRROR,
         'a block of its own'),
        (DASH.replace('130.49.146.10:8080', '/api* 130.49.146.10:8080') + MIRROR, 'found 1'),
        (DASH.replace('130.49.146.10:8080', '130.49.146.10:8080 130.49.146.11:8080') + MIRROR,
         'found 1'),
        (DASH.replace('8080', '80800') + MIRROR, 'found 1'),
        (SNIPPET + DASH + MIRROR, 'already defined'),
    ])
    def test_anything_unexpected_is_left_alone(self, text, why):
        with pytest.raises(ValueError, match=why):
            tool.transform(text, **KW)

    def test_a_global_block_that_never_closes(self):
        with pytest.raises(ValueError, match='never closes'):
            tool.transform('{\n\tadmin off\n' + DASH + MIRROR, **KW)


class TestApply:

    @pytest.fixture
    def files(self, tmp_path):
        caddyfile = tmp_path / 'Caddyfile'
        caddyfile.write_text(DASH + '\n' + MIRROR)
        caddyfile.chmod(0o640)
        pin = tmp_path / 'entry-hop.pem'
        pin.write_text('-----BEGIN CERTIFICATE-----\n')
        validate = tmp_path / 'caddy'
        validate.write_text('#!/bin/sh\n[ -e "$(dirname "$0")/reject" ] && { echo "Error: bad" >&2; exit 1; }\n'
                            'echo "$*" >> "$(dirname "$0")/validated"\n')
        validate.chmod(0o755)
        return caddyfile, pin, validate

    def _args(self, caddyfile, pin, validate, *extra):
        return ['--caddyfile', str(caddyfile), '--pin', str(pin), '--caddy', str(validate),
                '--reload', f'touch {caddyfile.parent}/reloaded', *extra]

    def test_dry_run_writes_nothing(self, files, capsys):
        caddyfile, pin, validate = files
        before = caddyfile.read_text()
        assert tool.main(self._args(caddyfile, pin, validate)) == 0
        out = capsys.readouterr().out
        assert '+\timport entry_hop' in out and '-\treverse_proxy 130.49.146.10:8080' in out
        assert caddyfile.read_text() == before
        assert not list(caddyfile.parent.glob('Caddyfile.bak-*'))
        assert not (caddyfile.parent / 'validated').exists()

    def test_apply_backs_up_validates_reloads_and_keeps_the_mode(self, files):
        caddyfile, pin, validate = files
        before = caddyfile.read_text()
        assert tool.main(self._args(caddyfile, pin, validate, '--apply')) == 0
        assert caddyfile.read_text() == tool.transform(
            before, entry='130.49.146.10', old_port=8080, port=8448, pin=str(pin), expect=2)
        [backup] = caddyfile.parent.glob('Caddyfile.bak-*')
        assert backup.read_text() == before
        assert (caddyfile.stat().st_mode & 0o777) == 0o640
        assert (caddyfile.parent / 'validated').read_text().split() == \
            ['validate', '--config', str(caddyfile), '--adapter', 'caddyfile']
        assert (caddyfile.parent / 'reloaded').exists()

    def test_a_rejected_config_is_put_back_and_never_reloaded(self, files, capsys):
        caddyfile, pin, validate = files
        before = caddyfile.read_text()
        (validate.parent / 'reject').touch()
        assert tool.main(self._args(caddyfile, pin, validate, '--apply')) == 1
        assert 'put back' in capsys.readouterr().out
        assert caddyfile.read_text() == before
        assert not (caddyfile.parent / 'reloaded').exists()

    def test_no_pin_no_change(self, files, capsys):
        caddyfile, pin, validate = files
        pin.unlink()
        before = caddyfile.read_text()
        assert tool.main(self._args(caddyfile, pin, validate, '--apply')) == 1
        assert 'not readable' in capsys.readouterr().out
        assert caddyfile.read_text() == before

    def test_a_failed_reload_says_how_to_go_back(self, files, capsys):
        caddyfile, pin, validate = files
        args = self._args(caddyfile, pin, validate, '--apply')
        args[args.index('--reload') + 1] = 'false'
        assert tool.main(args) == 1
        out = capsys.readouterr().out
        assert 'reload failed' in out and 'cp -a' in out and 'Caddyfile.bak-' in out
        assert os.path.exists(caddyfile)
