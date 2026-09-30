"""Tests for scripts/exit_memory_watchdog.sh — the exit host's memory watchdog.

It decides whether to restart the 3x-ui container or reboot a production
host, so the real bash script is run here, not a re-implementation: fake
/proc/meminfo and /proc/pressure/memory, stub ``docker`` / ``systemctl`` /
``curl`` / ``logger`` / ``sync`` on PATH that only record their arguments.

The regression at the heart of it: its predecessor force-rebooted the exit
host on 2026-09-13, -17 and -25 — dropping every call each time — because it
read MemAvailable alone (68 MB) while >1.3 GB of swap sat free.
"""

import os
import stat
import subprocess
import textwrap

import pytest

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'scripts', 'exit_memory_watchdog.sh'))
CRON = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'scripts', 'exit_memory_watchdog.cron'))

TOKEN = '123456:SECRET-bot-token'
NOW = 1_790_000_000
MB = 1024


@pytest.fixture
def box(tmp_path):
    """A fake host: stub commands, env file, state dir, meminfo/psi paths."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    calls = tmp_path / 'calls'
    calls.write_text('')
    stub = textwrap.dedent('''\
        #!/bin/sh
        {{ printf '%s' "$(basename "$0")"; for a in "$@"; do printf '\\t%s' "$a"; done; printf '\\n'; }} >> "{calls}"
        case "$(basename "$0")" in
          curl) printf '200' ;;
          docker) exit "${{STUB_DOCKER_RC:-0}}" ;;
        esac
        exit 0
        ''').format(calls=calls)
    for name in ('logger', 'docker', 'systemctl', 'curl', 'sync'):
        f = bin_dir / name
        f.write_text(stub)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    (tmp_path / 'bot.env').write_text(
        f'BOT_TOKEN={TOKEN}\nFORUM_GROUP_ID="-1001234"\nTOPIC_AI=55\n')
    return tmp_path


def run(box, *, avail_mb, swap_free_mb, psi_full=0.3, now=NOW, extra_env=None,
        meminfo=True):
    if meminfo:
        (box / 'meminfo').write_text(
            f'MemTotal:         951368 kB\n'
            f'MemAvailable:     {int(avail_mb * MB)} kB\n'
            f'SwapTotal:       1572860 kB\n'
            f'SwapFree:        {int(swap_free_mb * MB)} kB\n')
    (box / 'psi').write_text(
        f'some avg10=1.00 avg60={psi_full} avg300=0.3 total=1\n'
        f'full avg10=0.50 avg60={psi_full} avg300=0.2 total=1\n')
    env = {
        'PATH': f"{box / 'bin'}:{os.environ['PATH']}",
        'MEMINFO': str(box / 'meminfo'),
        'PSI_FILE': str(box / 'psi'),
        'STATE_DIR': str(box / 'state'),
        'NOTIFY_ENV': str(box / 'bot.env'),
        'NOW_EPOCH': str(now),
    }
    env.update(extra_env or {})
    before = len((box / 'calls').read_text().splitlines())
    proc = subprocess.run(['bash', SCRIPT], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    lines = (box / 'calls').read_text().splitlines()[before:]
    return [line.split('\t') for line in lines]


def cmds(calls, name):
    return [c[1:] for c in calls if c[0] == name]


def logs(calls):
    return [' '.join(c[1:]) for c in calls if c[0] == 'logger']


def acted(calls):
    return cmds(calls, 'docker') or cmds(calls, 'systemctl')


def state(box):
    path = box / 'state' / 'state'
    if not path.exists():
        return {}
    return dict(line.split('=', 1) for line in path.read_text().split())


def seed(box, **kv):
    (box / 'state').mkdir(exist_ok=True)
    (box / 'state' / 'state').write_text(''.join(f'{k}={v}\n' for k, v in kv.items()))


LOW = dict(avail_mb=50, swap_free_mb=100)          # 150 MB of RAM+swap left
OK = dict(avail_mb=150, swap_free_mb=1380)         # this host's normal day


class TestNoFalseAlarms:

    def test_a_normal_day_does_nothing(self, box):
        calls = run(box, **OK)
        assert not acted(calls) and not cmds(calls, 'curl')
        assert state(box)['strikes'] == '0'
        assert any('headroom=' in line for line in logs(calls))    # trend line every run

    def test_the_2026_09_25_reboot_does_not_happen_anymore(self, box):
        """MemAvailable 68 MB, 1.35 GB swap free, no thrashing — the old
        watchdog force-rebooted on exactly this. Not an emergency."""
        for _ in range(5):
            calls = run(box, avail_mb=66.7, swap_free_mb=1350, psi_full=0.5)
            assert not acted(calls)
        assert state(box)['strikes'] == '0'

    def test_a_good_check_in_between_resets_the_count(self, box):
        """'Two in a row' means two in a row — the old version kept a strike
        for days while memory hovered between its two thresholds."""
        run(box, **LOW)
        run(box, **OK)
        calls = run(box, **LOW)
        assert not acted(calls)
        assert state(box)['strikes'] == '1'

    def test_blind_watchdog_never_acts(self, box):
        calls = run(box, avail_mb=0, swap_free_mb=0, meminfo=False)
        assert not acted(calls)
        assert any('cannot read' in line for line in logs(calls))


class TestEscalation:

    def test_two_bad_checks_restart_3xui_not_the_host(self, box):
        first = run(box, **LOW)
        assert not acted(first) and state(box)['strikes'] == '1'
        second = run(box, **LOW)
        assert cmds(second, 'docker') == [['restart', '3x-ui']]
        assert not cmds(second, 'systemctl')
        assert state(box) == {'strikes': '0', 'last_restart': str(NOW), 'last_reboot': '0'}

    def test_restart_is_announced_in_the_admin_topic(self, box):
        run(box, **LOW)
        calls = run(box, **LOW)
        [args] = cmds(calls, 'curl')
        assert args[-1] == f'https://api.telegram.org/bot{TOKEN}/sendMessage'
        assert 'chat_id=-1001234' in args and 'message_thread_id=55' in args
        text = next(a for a in args if a.startswith('text='))
        assert '3x-ui' in text and 'hy2' in text

    def test_thrashing_counts_even_with_swap_to_spare(self, box):
        run(box, avail_mb=120, swap_free_mb=1300, psi_full=45.2)
        calls = run(box, avail_mb=120, swap_free_mb=1300, psi_full=45.2)
        assert cmds(calls, 'docker') == [['restart', '3x-ui']]

    def test_restart_that_did_not_hold_escalates_to_a_graceful_reboot(self, box):
        seed(box, strikes=1, last_restart=NOW - 20 * 60, last_reboot=0)
        calls = run(box, **LOW)
        assert cmds(calls, 'systemctl') == [['reboot']]          # graceful, never --force
        assert cmds(calls, 'sync') == [[]]
        assert not cmds(calls, 'docker')
        # written BEFORE rebooting: after boot, a new episode starts at step 1
        assert state(box) == {'strikes': '0', 'last_restart': '0', 'last_reboot': str(NOW)}

    def test_restart_long_ago_just_restarts_again(self, box):
        seed(box, strikes=1, last_restart=NOW - 2 * 3600, last_reboot=0)
        calls = run(box, **LOW)
        assert cmds(calls, 'docker') == [['restart', '3x-ui']] and not cmds(calls, 'systemctl')

    def test_no_reboot_loop(self, box):
        """A reboot within the last 6 h: restart again and call a human."""
        seed(box, strikes=1, last_restart=NOW - 20 * 60, last_reboot=NOW - 3600)
        calls = run(box, **LOW)
        assert not cmds(calls, 'systemctl')
        assert cmds(calls, 'docker') == [['restart', '3x-ui']]
        text = next(a for a in cmds(calls, 'curl')[0] if a.startswith('text='))
        assert 'нужен человек' in text

    def test_failed_restart_is_reported(self, box):
        run(box, **LOW)
        calls = run(box, **LOW, extra_env={'STUB_DOCKER_RC': '1'})
        assert len(cmds(calls, 'curl')) == 2
        assert any('failed' in line for line in logs(calls))


class TestHygiene:

    def test_token_never_reaches_a_log_line(self, box):
        run(box, **LOW)
        calls = run(box, **LOW)
        assert cmds(calls, 'curl')                       # it was used...
        assert not any(TOKEN in line for line in logs(calls))   # ...never logged

    def test_dry_run_touches_nothing(self, box):
        seed(box, strikes=1, last_restart=0, last_reboot=0)
        calls = run(box, **LOW, extra_env={'DRY_RUN': '1'})
        assert not acted(calls) and not cmds(calls, 'curl')
        assert any('would run docker restart 3x-ui' in line for line in logs(calls))
        assert state(box) == {'strikes': '1', 'last_restart': '0', 'last_reboot': '0'}

    def test_missing_notify_env_still_restarts(self, box):
        (box / 'bot.env').unlink()
        run(box, **LOW)
        calls = run(box, **LOW)
        assert cmds(calls, 'docker') == [['restart', '3x-ui']] and not cmds(calls, 'curl')

    def test_script_never_forces_a_reboot(self):
        # code lines only — the header documents the old `reboot --force`
        code = '\n'.join(l for l in open(SCRIPT).read().splitlines()
                         if not l.lstrip().startswith('#'))
        assert '--force' not in code and 'reboot -f' not in code

    def test_cron_runs_the_installed_path_every_5_minutes(self):
        lines = [l for l in open(CRON).read().splitlines() if l and not l.startswith('#')]
        assert '*/5 * * * * root /usr/local/bin/memory_watchdog.sh' in lines
