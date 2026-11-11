"""scripts/deploy_to_entry.sh — the smoke of the hop's HTTPS listener (AGENTS.md
§53): once the bot has its certificate, a deploy whose listener does not answer
with the deployed sha fails, while /health on 8080 alone would look fine.

The script runs for real from a throwaway git checkout (it stamps
bot/version.txt there); ``rsync`` and ``ssh`` are fakes that answer like
entry does. Nothing reaches a server.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
st = Path(os.environ['FAKE_STATE'])
cmd = sys.argv[-1]
with open(st / 'ssh.log', 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
stamp = (Path(os.environ['CHECKOUT']) / 'bot' / 'version.txt').read_text().strip()
if 'deploy_entry_bot.sh' in cmd:
    sys.exit(0)
if '127.0.0.1:8080/health' in cmd:
    print(json.dumps({'status': 'healthy', 'version': stamp}))
    sys.exit(0)
if 'hop_tls.py check' in cmd:
    hop = (st / 'hop').read_text()
    if hop == 'down':
        print('Error response from daemon: container vpn-bot is not running', file=sys.stderr)
        sys.exit(1)
    print(hop.replace('STAMP', stamp))
    sys.exit(1 if '"fail"' in hop else 0)
if 'verify_panel_client_fields.py' in cmd:
    sys.exit(0)
sys.exit(f'fake ssh: unexpected {cmd}')
'''


@pytest.fixture
def checkout(tmp_path):
    if shutil.which('git') is None:
        pytest.skip('git is not installed')
    co = tmp_path / 'checkout'
    (co / 'scripts').mkdir(parents=True)
    (co / 'bot').mkdir()
    (co / 'bot' / 'x.py').write_text('x = 1\n')
    (co / '.gitignore').write_text('bot/version.txt\n')
    shutil.copy(REPO / 'scripts' / 'deploy_to_entry.sh', co / 'scripts' / 'deploy_to_entry.sh')
    git = ['git', '-C', str(co), '-c', 'user.name=t', '-c', 'user.email=t@t']
    subprocess.run(git[:3] + ['init', '-q'], check=True)
    subprocess.run(git + ['add', '.'], check=True)
    subprocess.run(git + ['commit', '-qm', 'init'], check=True)
    state = tmp_path / 'state'
    state.mkdir()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'ssh').write_text(FAKE_SSH)
    (bin_dir / 'rsync').write_text('#!/bin/sh\nexit 0\n')
    for name in ('ssh', 'rsync'):
        (bin_dir / name).chmod(0o755)

    def run(hop):
        (state / 'hop').write_text(hop)
        env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
               'FAKE_STATE': str(state), 'CHECKOUT': str(co)}
        return subprocess.run(['bash', str(co / 'scripts' / 'deploy_to_entry.sh')],
                              env=env, capture_output=True, text=True, timeout=60)
    return run


def test_off_is_skipped(checkout):
    r = checkout(json.dumps({'state': 'off', 'reason': 'no certificate in /var/lib/vpn-bot/hop-tls'}))
    assert r.returncode == 0, r.stderr
    assert 'off: no certificate in /var/lib/vpn-bot/hop-tls' in r.stdout
    assert 'is live' in r.stdout


def test_the_listener_answers_the_deployed_sha(checkout):
    r = checkout(json.dumps({'state': 'ok', 'port': 8448, 'version': 'STAMP', 'status': 'healthy'}))
    assert r.returncode == 0, r.stderr
    assert 'over TLS: ' in r.stdout and 'is live' in r.stdout


def test_another_version_over_tls_fails(checkout):
    r = checkout(json.dumps({'state': 'ok', 'port': 8448, 'version': 'old 2026', 'status': 'healthy'}))
    assert r.returncode == 1
    assert "answers 'old 2026'" in r.stderr and 'is live' not in r.stdout


@pytest.mark.parametrize('hop', [
    json.dumps({'state': 'fail', 'error': 'ConnectionRefusedError: [Errno 111]'}),
    'down',                                  # no JSON at all: the check could not run
    'garbage',
])
def test_a_failed_or_missing_check_fails(checkout, hop):
    r = checkout(hop)
    assert r.returncode == 1
    assert 'failed its check' in r.stderr and 'is live' not in r.stdout
