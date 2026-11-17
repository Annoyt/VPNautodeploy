"""scripts/hermes_release_notice.py: a newer stable Hermes release -> the AI topic.

The pure core (versions, the release, the decision, the texts) and run_once
on a fake IO; NoticeIO against a fake systemctl / binary / session; the
units and the deploy script; and the script run as a file from a copy
next to the guard, the way the timer runs it from /usr/local/bin, against
a local HTTP server instead of GitHub.
"""
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT = os.path.join(REPO, 'scripts', 'hermes_release_notice.py')
GUARD = os.path.join(REPO, 'scripts', 'hermes_model_guard.py')

_spec = importlib.util.spec_from_file_location('hermes_release_notice', SCRIPT)
notice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notice)

# What `hermes --version` printed on entry on 2026-11-16.
VERSION_OUTPUT = (
    'Hermes Agent v0.23.1 (2026.9.24) · stable · local 3b4215785882\n'
    'Install directory: /usr/local/lib/hermes-agent-v0.23\n'
    'Install method: git\n'
    'Python: 3.14.9\n'
    'OpenAI SDK: 2.54.0\n'
    'Up to date\n'
)
# `systemctl show -p ExecStart --value hermes-api.service` on entry.
EXEC_START = ('{ path=/usr/local/lib/hermes-agent-v0.23/.hermes/bin/hermes ; '
              'argv[]=/usr/local/lib/hermes-agent-v0.23/.hermes/bin/hermes gateway run ; '
              'ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; '
              'status=0/0 }')
# The shape of GitHub's /releases/latest for v0.23.2, notes shortened.
NOTES = ('# Hermes Agent v0.23.2\n\n**Release Date:** November 16, 2026\n\n'
         '> Patch release. This tag rolls up the ~1,989 PRs merged since v0.23.1 into a '
         'stable release.\n\n<!-- HERMES_BUILDS_TABLE -->\n\n## About this release\n')
LATEST = {
    'tag_name': 'v0.23.2', 'name': 'Hermes Agent v0.23.2', 'draft': False,
    'prerelease': False, 'published_at': '2026-11-16T20:59:18Z', 'body': NOTES,
    'html_url': 'https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.2',
}


def release(version=(0, 23, 2), summary='Patch release.'):
    return notice.Release(version, 'https://github.com/NousResearch/hermes-agent/releases/tag/'
                          + notice.tag_of(version), '16.11.2026 20:59 UTC', summary)


class FakeIO:
    def __init__(self, running=(0, 23, 1), latest=None, send_ok=True,
                 why_running='', why_release=''):
        self.running = running
        self.latest = latest if latest is not None else release()
        self.send_ok = send_ok
        self.why_running = why_running or 'в ExecStart hermes-api.service нет пути к бинарю'
        self.why_release = why_release or 'GitHub: ConnectionError: refused'
        self.sent = []

    def running_version(self):
        return (self.running, '') if self.running else (None, self.why_running)

    def latest_release(self):
        return (self.latest, '') if self.latest else (None, self.why_release)

    def send_telegram(self, text):
        self.sent.append(text)
        return self.send_ok


def state_of(state_dir):
    with open(os.path.join(state_dir, 'state.json'), encoding='utf-8') as fh:
        return json.load(fh)


class TestVersions:
    def test_running_version_from_the_real_output(self):
        assert notice.running_from_output(VERSION_OUTPUT) == (0, 23, 1)

    def test_running_version_after_a_warning_line(self):
        out = 'hermes: launched again after an interrupted update\n' + VERSION_OUTPUT
        assert notice.running_from_output(out) == (0, 23, 1)

    def test_no_running_version_in_other_text(self):
        assert notice.running_from_output('') is None
        assert notice.running_from_output(None) is None
        assert notice.running_from_output('Python: 3.14.9\nUp to date\n') is None
        # the date in brackets is not the version
        assert notice.running_from_output('Hermes Agent (2026.9.24)\n') is None

    def test_a_date_is_not_a_version(self):
        assert notice.running_from_output('Hermes Agent v2026.9.24 · stable\n') is None
        assert notice.parse_announced('v2026.9.24') is None
        assert notice.parse_announced('v999.0.0') == (999, 0, 0)

    def test_announced_is_read_strictly(self):
        assert notice.parse_announced('v0.23.2') == (0, 23, 2)
        for bad in (None, '', '0.23.2', 'v0.23', 'v0.23.2-rc1', ' v0.23.2', 5, ['v0.23.2']):
            assert notice.parse_announced(bad) is None, bad

    def test_versions_compare_as_numbers(self):
        assert (0, 23, 10) > (0, 23, 9)            # not as text
        assert notice.tag_of((0, 23, 10)) == 'v0.23.10'

    def test_exec_path(self):
        assert notice.exec_path(EXEC_START) == '/usr/local/lib/hermes-agent-v0.23/.hermes/bin/hermes'
        assert notice.exec_path('') is None
        assert notice.exec_path(None) is None
        assert notice.exec_path('{ path=relative/hermes ; argv[]=x }') is None


class TestRelease:
    def test_the_v0_23_2_release(self):
        rel, why = notice.release_from(LATEST)
        assert why == ''
        assert rel.version == (0, 23, 2) and rel.tag == 'v0.23.2'
        assert rel.url == LATEST['html_url']
        assert rel.published == '16.11.2026 20:59 UTC'
        assert rel.summary.startswith('Patch release. This tag rolls up the ~1,989 PRs')

    def test_a_date_tag_takes_the_version_from_the_name(self):
        rel, _ = notice.release_from(dict(LATEST, tag_name='v2026.9.24',
                                          name='Hermes Agent v0.21.5 (v2026.9.24)'))
        assert rel.version == (0, 21, 5)

    def test_a_date_first_in_the_name_is_skipped(self):
        rel, _ = notice.release_from(dict(LATEST, name='Hermes Agent (v2026.9.24) v0.21.5'))
        assert rel.version == (0, 21, 5)

    def test_the_tag_when_the_name_has_no_version(self):
        rel, _ = notice.release_from(dict(LATEST, name='The Hearth Release', tag_name='v0.24.0'))
        assert rel.version == (0, 24, 0)

    def test_no_version_anywhere(self):
        rel, why = notice.release_from(dict(LATEST, name='Hermes Agent', tag_name='v2026.9.24'))
        assert rel is None and 'нет версии' in why

    def test_drafts_and_pre_releases_are_refused(self):
        for key in ('draft', 'prerelease'):
            rel, why = notice.release_from(dict(LATEST, **{key: True}))
            assert rel is None and 'pre-release' in why

    def test_not_a_release(self):
        for payload in (None, [], 'v0.23.2', [LATEST]):
            rel, why = notice.release_from(payload)
            assert rel is None and why

    def test_a_link_off_github_becomes_the_release_list(self):
        rel, _ = notice.release_from(dict(LATEST, html_url='https://example.com/x'))
        assert rel.url == notice.RELEASES_PAGE
        rel, _ = notice.release_from(dict(LATEST, html_url=None))
        assert rel.url == notice.RELEASES_PAGE

    def test_published_text(self):
        assert notice.published_text('2026-11-16T20:59:18Z') == '16.11.2026 20:59 UTC'
        assert notice.published_text(None) == ''
        assert notice.published_text('yesterday') == ''


class TestSummary:
    def test_skips_headings_comments_and_the_date(self):
        body = ('# Title\n<!-- table -->\n**Release Date:** today\n\n'
                '> **Patch** release with `code`.\nSecond line.\n')
        assert notice.release_summary(body) == 'Patch release with code.'

    def test_a_long_line_is_cut_at_a_word(self):
        line = 'word ' * 100
        out = notice.release_summary(line, limit=42)
        assert out.endswith('…') and len(out) <= 43
        assert out[:-1] == out[:-1].rstrip() and 'wor…' not in out

    def test_nothing_to_say(self):
        assert notice.release_summary(None) == ''
        assert notice.release_summary('') == ''
        assert notice.release_summary('# Only a title\n<!-- x -->\n') == ''


class TestDecide:
    def test_a_newer_release_is_announced(self):
        text = notice.decide((0, 23, 1), release(), None)
        assert 'Вышел Hermes v0.23.2' in text and 'работает v0.23.1' in text

    def test_the_running_release_is_not(self):
        assert notice.decide((0, 23, 2), release(), None) is None

    def test_an_older_release_is_not(self):
        assert notice.decide((0, 24, 0), release(), None) is None

    def test_an_announced_release_is_not_announced_again(self):
        assert notice.decide((0, 23, 1), release(), (0, 23, 2)) is None
        assert notice.decide((0, 23, 1), release(), (0, 24, 0)) is None

    def test_a_release_newer_than_the_announced_one_is(self):
        assert notice.decide((0, 23, 1), release((0, 24, 0)), (0, 23, 2)) is not None


class TestTexts:
    def test_the_notice(self):
        text = notice.format_notice((0, 23, 1), release())
        assert text.splitlines()[0] == '🆕 <b>Вышел Hermes v0.23.2</b> (16.11.2026 20:59 UTC)'
        assert '«Patch release.»' in text
        assert ('<a href="https://github.com/NousResearch/hermes-agent/releases/tag/v0.23.2">'
                'v0.23.2 на GitHub</a>') in text
        assert '<code>docs/runbooks/hermes_update.md</code>' in text

    def test_text_from_github_is_escaped(self):
        rel = notice.Release((0, 23, 2), 'https://github.com/x?a=1&b="2"', '',
                             '<script>alert(1)</script> & co')
        text = notice.format_notice((0, 23, 1), rel)
        assert '<script>' not in text and '&lt;script&gt;' in text and '&amp; co' in text
        assert 'href="https://github.com/x?a=1&amp;b=&quot;2&quot;"' in text
        assert '()' not in text.splitlines()[0]          # no empty date brackets

    def test_no_summary_no_quote_line(self):
        assert '«' not in notice.format_notice((0, 23, 1), release(summary=''))

    def test_the_failure_warning(self):
        text = notice.format_failure(7, 'GitHub: <b>timeout</b>')
        assert 'не сработала 7 раз подряд' in text
        assert '&lt;b&gt;timeout&lt;/b&gt;' in text
        assert 'journalctl -u hermes-release-notice' in text

    @pytest.mark.parametrize('n, count', [
        (1, '1 раз'), (2, '2 раза'), (3, '3 раза'), (4, '4 раза'), (5, '5 раз'),
        (11, '11 раз'), (12, '12 раз'), (13, '13 раз'), (14, '14 раз'), (20, '20 раз'),
        (21, '21 раз'), (22, '22 раза'), (104, '104 раза'), (112, '112 раз'),
    ])
    def test_the_count_reads_right(self, n, count):
        assert f'не сработала {count} подряд' in notice.format_failure(n, 'x')


class TestRunOnce:
    def test_up_to_date(self, tmp_path):
        io = FakeIO(running=(0, 23, 2))
        assert notice.run_once(io, str(tmp_path), dry_run=False) == 0
        assert io.sent == []
        assert state_of(tmp_path)['announced'] is None

    def test_a_new_release_once(self, tmp_path):
        io = FakeIO()
        assert notice.run_once(io, str(tmp_path), dry_run=False) == 0
        assert len(io.sent) == 1 and 'Вышел Hermes v0.23.2' in io.sent[0]
        assert state_of(tmp_path)['announced'] == 'v0.23.2'
        assert notice.run_once(io, str(tmp_path), dry_run=False) == 0
        assert len(io.sent) == 1                       # once per version

    def test_an_undelivered_notice_goes_out_next_run(self, tmp_path):
        io = FakeIO(send_ok=False)
        assert notice.run_once(io, str(tmp_path), dry_run=False) == 1
        assert state_of(tmp_path)['announced'] is None
        io.send_ok = True
        assert notice.run_once(io, str(tmp_path), dry_run=False) == 0
        assert len(io.sent) == 2 and state_of(tmp_path)['announced'] == 'v0.23.2'

    def test_the_next_release_is_announced_too(self, tmp_path):
        io = FakeIO()
        notice.run_once(io, str(tmp_path), dry_run=False)
        io.latest = release((0, 24, 0))
        notice.run_once(io, str(tmp_path), dry_run=False)
        assert len(io.sent) == 2 and 'Вышел Hermes v0.24.0' in io.sent[1]

    def test_after_an_update_nothing_more(self, tmp_path):
        io = FakeIO()
        notice.run_once(io, str(tmp_path), dry_run=False)
        io.running = (0, 23, 2)
        assert notice.run_once(io, str(tmp_path), dry_run=False) == 0
        assert len(io.sent) == 1

    @pytest.mark.parametrize('side', ['github', 'binary'])
    def test_a_blind_streak_warns_once(self, tmp_path, side):
        io = FakeIO(**({'latest': False} if side == 'github' else {'running': None}))
        for run in range(1, notice.FAIL_NOTIFY_AFTER):
            assert notice.run_once(io, str(tmp_path), dry_run=False) == 2
            assert io.sent == [], run
        assert notice.run_once(io, str(tmp_path), dry_run=False) == 2
        assert len(io.sent) == 1
        assert 'не сработала 2 раза подряд' in io.sent[0]
        why = io.why_release if side == 'github' else io.why_running
        assert why in io.sent[0]
        notice.run_once(io, str(tmp_path), dry_run=False)
        assert len(io.sent) == 1                       # once per streak
        assert state_of(tmp_path)['failures'] == notice.FAIL_NOTIFY_AFTER + 1

    def test_a_good_run_ends_the_streak(self, tmp_path):
        io = FakeIO(latest=False)
        for _ in range(notice.FAIL_NOTIFY_AFTER):
            notice.run_once(io, str(tmp_path), dry_run=False)
        assert len(io.sent) == 1
        io.latest, io.running = release(), (0, 23, 2)
        assert notice.run_once(io, str(tmp_path), dry_run=False) == 0
        assert state_of(tmp_path)['failures'] == 0
        assert state_of(tmp_path)['failure_warned'] is False
        io.latest = False
        for _ in range(notice.FAIL_NOTIFY_AFTER):
            notice.run_once(io, str(tmp_path), dry_run=False)
        assert len(io.sent) == 2                       # a new streak warns again

    def test_an_undelivered_warning_is_retried(self, tmp_path):
        io = FakeIO(latest=False, send_ok=False)
        for _ in range(notice.FAIL_NOTIFY_AFTER):
            notice.run_once(io, str(tmp_path), dry_run=False)
        assert len(io.sent) == 1 and state_of(tmp_path)['failure_warned'] is False
        io.send_ok = True
        notice.run_once(io, str(tmp_path), dry_run=False)
        assert len(io.sent) == 2 and state_of(tmp_path)['failure_warned'] is True

    def test_dry_run_sends_and_writes_nothing(self, tmp_path, capsys):
        io = FakeIO()
        assert notice.run_once(io, str(tmp_path), dry_run=True) == 0
        assert io.sent == [] and not os.path.exists(tmp_path / 'state.json')
        assert 'would send' in capsys.readouterr().out
        blind = FakeIO(latest=False)
        for _ in range(notice.FAIL_NOTIFY_AFTER + 1):
            assert notice.run_once(blind, str(tmp_path), dry_run=True) == 2
        assert blind.sent == [] and not os.path.exists(tmp_path / 'state.json')

    def test_a_broken_state_starts_fresh(self, tmp_path):
        (tmp_path / 'state.json').write_text('{not json')
        io = FakeIO()
        assert notice.run_once(io, str(tmp_path), dry_run=False) == 0
        assert state_of(tmp_path)['announced'] == 'v0.23.2'
        (tmp_path / 'state.json').write_text(json.dumps({'announced': 'v0.23.2', 'failures': 'x'}))
        assert notice.load_state(str(tmp_path))['failures'] == 0


class FakeProc:
    def __init__(self, stdout='', returncode=0):
        self.stdout, self.returncode = stdout, returncode


class TestNoticeIO:
    def _io(self, session=None):
        return notice.NoticeIO(type('GIO', (), {'session': session})())

    def _run(self, monkeypatch, systemctl, binary):
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            step = systemctl if argv[0] == 'systemctl' else binary
            if isinstance(step, BaseException):
                raise step
            return step
        monkeypatch.setattr(notice.subprocess, 'run', fake_run)
        return calls

    def test_the_binary_the_unit_runs(self, monkeypatch):
        calls = self._run(monkeypatch, FakeProc(EXEC_START), FakeProc(VERSION_OUTPUT))
        assert self._io().running_version() == ((0, 23, 1), '')
        assert calls == [['systemctl', 'show', '-p', 'ExecStart', '--value', 'hermes-api.service'],
                         ['/usr/local/lib/hermes-agent-v0.23/.hermes/bin/hermes', '--version']]

    def test_no_systemctl(self, monkeypatch):
        self._run(monkeypatch, FileNotFoundError('systemctl'), None)
        version, why = self._io().running_version()
        assert version is None and 'systemctl show' in why

    def test_no_such_unit(self, monkeypatch):
        self._run(monkeypatch, FakeProc('', 0), None)
        version, why = self._io().running_version()
        assert version is None and 'нет пути' in why

    def test_a_failed_systemctl_is_not_read(self, monkeypatch):
        calls = self._run(monkeypatch, FakeProc(EXEC_START, 1), FakeProc(VERSION_OUTPUT))
        version, why = self._io().running_version()
        assert version is None and 'нет пути' in why
        assert len(calls) == 1                         # the binary was not run

    def test_a_hanging_binary(self, monkeypatch):
        self._run(monkeypatch, FakeProc(EXEC_START), subprocess.TimeoutExpired('hermes', 60))
        version, why = self._io().running_version()
        assert version is None and 'TimeoutExpired' in why

    def test_a_binary_that_says_something_else(self, monkeypatch):
        self._run(monkeypatch, FakeProc(EXEC_START), FakeProc('Traceback …\n', 1))
        version, why = self._io().running_version()
        assert version is None and 'код 1' in why

    def test_github_answers(self):
        seen = {}

        class Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return LATEST

        class Session:
            def get(self, url, **kw):
                seen.update(url=url, **kw)
                return Resp()
        rel, why = self._io(Session()).latest_release()
        assert rel.tag == 'v0.23.2' and why == ''
        assert seen['url'] == notice.LATEST_URL
        assert seen['headers']['User-Agent'] == notice.USER_AGENT
        assert seen['timeout'] == notice.HTTP_TIMEOUT

    def test_an_error_status_is_a_failure(self):
        class Resp:
            def raise_for_status(self):
                raise RuntimeError('403 rate limit exceeded')

            def json(self):
                return {'message': 'API rate limit exceeded'}

        class Session:
            def get(self, url, **kw):
                return Resp()
        rel, why = self._io(Session()).latest_release()
        assert rel is None and why == 'GitHub: RuntimeError: 403 rate limit exceeded'

    def test_github_fails_and_secrets_stay_out(self):
        notice.guard.register_secret('http://user:pw@proxy.invalid:8888')

        class Session:
            def get(self, url, **kw):
                raise ConnectionError('via http://user:pw@proxy.invalid:8888 refused')
        rel, why = self._io(Session()).latest_release()
        assert rel is None and why.startswith('GitHub: ConnectionError')
        assert 'user:pw' not in why and '***' in why


class TestBuildIO:
    def test_the_guards_telegram_path(self, tmp_path):
        token = '123456789:' + 'A' * 35
        env = tmp_path / '.env'
        env.write_text(f'BOT_TOKEN={token}\nFORUM_GROUP_ID=-1001\nTOPIC_AI=7\n'
                       'HTTPS_PROXY=http://u:secretpw@127.0.0.1:1\n')
        io = notice.build_io(str(env), bot_token_override=str(tmp_path / 'none.json'))
        assert isinstance(io.gio, notice.guard.GuardIO)
        assert io.gio.bot_token == token and io.gio.chat_id == '-1001' and io.gio.thread_id == 7
        assert io.gio.tg_proxies == {'https': 'http://u:secretpw@127.0.0.1:1',
                                     'http': 'http://u:secretpw@127.0.0.1:1'}
        assert io.gio.or_proxies is None              # GitHub directly
        assert io.gio.session.trust_env is False
        assert 'secretpw' not in notice.guard.redact('x http://u:secretpw@127.0.0.1:1 y')
        assert token not in notice.guard.redact(f'url /bot{token}/sendMessage')

    def test_the_dashboards_token_wins_while_env_holds_the_old_one(self, tmp_path):
        import hashlib
        old, new = '123456789:' + 'A' * 35, '123456789:' + 'B' * 35
        env = tmp_path / '.env'
        env.write_text(f'BOT_TOKEN={old}\nFORUM_GROUP_ID=-1001\n')
        override = tmp_path / 'bot_token.json'
        override.write_text(json.dumps({'token': new,
                                        'env_sha256': hashlib.sha256(old.encode()).hexdigest()}))
        assert notice.build_io(str(env), bot_token_override=str(override)).gio.bot_token == new

    def test_a_missing_env_file_is_could_not_check(self, tmp_path):
        assert notice.main(['--dry-run', '--bot-env', str(tmp_path / 'missing.env'),
                            '--state-dir', str(tmp_path)]) == 2


class TestDeployArtifacts:
    def _read(self, rel):
        with open(os.path.join(REPO, rel), encoding='utf-8') as fh:
            return fh.read()

    def test_service_unit(self):
        unit = self._read('systemd/hermes-release-notice.service')
        assert 'Type=oneshot' in unit
        assert 'ExecStart=/usr/local/bin/hermes_release_notice.py\n' in unit
        assert 'StateDirectory=hermes-release-notice' in unit
        assert notice.STATE_DIR == '/var/lib/hermes-release-notice'
        assert 'TimeoutStartSec=' in unit and 'MemoryMax=' in unit
        assert 'User=' not in unit                     # root: the bot's .env, /root/.hermes

    def test_two_mondays(self):
        # The warning comes after this many runs in a row that could not
        # check. With the weekly timer, 2 lets one bad Monday pass and warns
        # a week after the first failure. Change one, change the other.
        assert notice.FAIL_NOTIFY_AFTER == 2
        assert 'OnCalendar=Mon *-*-* 06:00:00 UTC\n' in self._read('systemd/hermes-release-notice.timer')

    def test_timer_unit(self):
        timer = self._read('systemd/hermes-release-notice.timer')
        calendar = [line.split('=', 1)[1] for line in timer.splitlines()
                    if line.startswith('OnCalendar=')]
        assert calendar == ['Mon *-*-* 06:00:00 UTC']
        assert 'Persistent=true' in timer
        assert 'WantedBy=timers.target' in timer
        if shutil.which('systemd-analyze'):
            r = subprocess.run(['systemd-analyze', 'calendar', '--iterations=3', calendar[0]],
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, 'TZ': 'UTC'})
            assert r.returncode == 0, r.stderr
            elapses = re.findall(r'(?:Next elapse|Iteration #\d+): (.+)', r.stdout)
            assert len(elapses) == 3, r.stdout
            for when in elapses:                       # once a week, Monday 06:00 UTC
                assert when.startswith('Mon ') and when.endswith(' 06:00:00 UTC'), when

    def test_deploy_script_ships_it_next_to_the_guard(self):
        sh = self._read('scripts/deploy_hermes_host.sh')
        for name in ('scripts/hermes_release_notice.py', 'systemd/hermes-release-notice.service',
                     'systemd/hermes-release-notice.timer'):
            assert name in sh, name
        assert ('rsync -a --chmod=F0755 scripts/hermes_release_notice.py '
                '"$ENTRY:/usr/local/bin/hermes_release_notice.py"') in sh
        assert '"$ENTRY:/usr/local/bin/hermes_model_guard.py"' in sh   # its guard, same dir
        assert 'check_drift scripts/hermes_release_notice.py /usr/local/bin/hermes_release_notice.py' in sh
        assert 'py_compile scripts/hermes_model_guard.py scripts/hermes_release_notice.py' in sh
        assert 'hermes-release-notice.timer' in sh.split('enable --now', 1)[1].split('list-timers')[0]
        assert "/usr/local/bin/hermes_release_notice.py --dry-run" in sh
        r = subprocess.run(['bash', '-n', os.path.join(REPO, 'scripts/deploy_hermes_host.sh')],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr

    def test_executable_like_the_guard(self):
        mode = os.stat(SCRIPT).st_mode
        assert mode & stat.S_IXUSR
        with open(SCRIPT, encoding='utf-8') as fh:
            assert fh.readline() == '#!/usr/bin/env python3\n'


class _Latest(BaseHTTPRequestHandler):
    payload = LATEST

    def do_GET(self):
        body = json.dumps(self.payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestAsTheTimerRunsIt:
    """The file run by itself from a copy next to the guard, like /usr/local/bin."""

    @pytest.fixture
    def host(self, tmp_path):
        bindir = tmp_path / 'usr-local-bin'
        bindir.mkdir()
        for src in (SCRIPT, GUARD):
            shutil.copy(src, bindir / os.path.basename(src))
        fake = tmp_path / 'fakebin'
        fake.mkdir()
        hermes = fake / 'hermes'
        hermes.write_text('#!/bin/sh\n[ "$1" = --version ] || exit 64\ncat <<"EOF"\n'
                          + VERSION_OUTPUT + 'EOF\n')
        (fake / 'systemctl').write_text(
            f'#!/bin/sh\necho "{{ path={hermes} ; argv[]={hermes} gateway run ; ignore_errors=no }}"\n')
        for f in (hermes, fake / 'systemctl'):
            f.chmod(0o755)
        env = tmp_path / 'bot.env'
        env.write_text('FORUM_GROUP_ID=-1001\nTOPIC_AI=7\n')     # no token: nothing can be sent
        server = ThreadingHTTPServer(('127.0.0.1', 0), _Latest)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        state = tmp_path / 'state'
        yield {'script': str(bindir / 'hermes_release_notice.py'), 'env': str(env),
               'state': str(state), 'path': f'{fake}:/usr/bin:/bin',
               'url': f'http://127.0.0.1:{server.server_address[1]}/releases/latest'}
        server.shutdown()

    def _run(self, host, *extra):
        return subprocess.run(
            [sys.executable, host['script'], '--bot-env', host['env'], '--state-dir', host['state'],
             '--latest-url', host['url'], *extra],
            capture_output=True, text=True, timeout=120,
            env={'PATH': host['path'], 'LANG': 'C.UTF-8'})

    def test_dry_run_shows_the_notice(self, host):
        r = self._run(host, '--dry-run')
        assert r.returncode == 0, r.stderr
        assert 'would send:' in r.stdout
        assert '🆕 <b>Вышел Hermes v0.23.2</b> (16.11.2026 20:59 UTC)' in r.stdout
        assert 'На entry работает v0.23.1.' in r.stdout
        assert not os.path.exists(host['state'])

    def test_no_token_means_not_delivered_and_not_announced(self, host):
        r = self._run(host)
        assert r.returncode == 1, r.stderr
        assert 'not delivered' in r.stderr
        assert state_of(host['state'])['announced'] is None
