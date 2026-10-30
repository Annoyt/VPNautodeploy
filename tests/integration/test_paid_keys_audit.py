"""scripts/paid_keys_audit.py: paid keys the old «🔑 Мой ключ» rewrote.

Before PR #32 the button re-added every key holder's client with demo
values (10 GB, no expiry, one address), and nothing but a payment,
/quota or /expire rewrites a paid key's quota or date. The dry run is the
audit; --apply fixes active payers; --apply --lapsed also switches
lapsed payers' working keys off.

Real sqlite (users, admin_actions); the panel is the relational fake.
"""

import importlib.util
import io
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from bot.core.database import Database
from bot.models.user import User
from bot.services.billing import tier_client_values

from .relational_panel import GB, Panel, now_ms

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_spec = importlib.util.spec_from_file_location(
    'paid_keys_audit', os.path.join(ROOT, 'scripts', 'paid_keys_audit.py'))
audit_script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit_script)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)


def config(**over):
    cfg = SimpleNamespace(
        PAID_LIMIT_IP=5, PAID_TRAFFIC_GB=100, DEMO_TRAFFIC_GB=10,
        DEMO_OTHER_TRAFFIC_GB=5, DEMO_DAYS=30, INBOUND_ID=1)
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


def iso_in(days):
    day = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=days)
    return day.replace(hour=23, minute=59, second=0, microsecond=0).isoformat()


def ms_of(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def seed(db, chat_id, status='paid', **extra):
    db._users.save(User(
        chat_id=chat_id, username=f'u{chat_id}', status=status,
        uuid=f'00000000-0000-0000-0000-{int(chat_id):012d}',
        email=f'user_{chat_id}@x', **extra))
    return db.get_user(chat_id)


class NoTrafficRow(Panel):
    """The traffic row cannot be read; the record can."""

    def get_client_traffic_sync(self, email):
        return None


class BackMeanwhile(Panel):
    """The key is lost when the audit looks and back when it writes:
    the user pressed «🔑 Мой ключ» in between."""

    def client_attached_sync(self, email):
        answer = super().client_attached_sync(email)
        self.attached.add(email)
        return answer


DAY_MS = 86_400_000


def old_button(panel, email, used=0):
    """What the pre-#32 button left: 10 GB, no expiry, one address."""
    panel.has(email, used=used, total=10 * GB, expiry=0, limit=1)


def run(db, panel, *argv, now=None):
    out = io.StringIO()
    code = audit_script.main(list(argv), db=db, xui=panel, config=config(),
                             out=out, now=now)
    return code, out.getvalue()


def line(out, chat_id):
    [found] = [l for l in out.splitlines() if l.startswith(f'{chat_id}: ')]
    return found


def audit(db):
    with db._connect() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT target_id, details FROM admin_actions WHERE action = ? "
            "ORDER BY id", (audit_script.ACTION,))]


class TestDryRun:

    def test_the_old_buttons_values_are_found(self, db):
        until = iso_in(20)
        seed(db, '1', subscription_expiry=until)
        panel = Panel()
        old_button(panel, 'user_1@x')
        code, out = run(db, panel)
        assert code == 0
        shown = datetime.fromtimestamp(ms_of(until) / 1000, tz=timezone.utc)
        assert line(out, '1') == (
            '1: paid active: bot quota 5 GB -> 100 GB, bot limit 1 -> 5, '
            'quota 10 GB -> 100 GB, '
            f'expiry never -> {shown:%Y-%m-%d %H:%M}, limit 1 -> 5  <- fix')
        assert '1 to fix' in out and 'dry run' in out
        assert panel.writes('user_1@x') == [] and audit(db) == []
        assert db.get_user('1').quota_gb == 5.0

    def test_a_key_that_matches_is_ok(self, db):
        user = seed(db, '2', subscription_expiry=iso_in(20), quota_gb=100.0,
                    limit_ip=5)
        target = tier_client_values(user, config())
        panel = Panel()
        panel.has('user_2@x', total=target['totalGB'],
                  expiry=target['expiryTime'], limit=target['limitIp'])
        _, out = run(db, panel)
        assert line(out, '2') == '2: paid active: ok'
        assert '1 ok, 0 to fix' in out

    def test_a_limit_alone_is_fixed(self, db):
        user = seed(db, '24', subscription_expiry=iso_in(20), quota_gb=100.0,
                    limit_ip=5)
        target = tier_client_values(user, config())
        panel = Panel()
        panel.has('user_24@x', total=target['totalGB'],
                  expiry=target['expiryTime'], limit=3)
        _, out = run(db, panel)
        assert line(out, '24') == '24: paid active: limit 3 -> 5  <- fix'

    def test_a_key_expiring_this_moment_is_off(self, db):
        # The panel's rule: an expiry at or before now has passed.
        seed(db, '25', subscription_expiry=iso_in(-10))
        now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
        panel = Panel()
        panel.has('user_25@x', expiry=int(
            now.replace(tzinfo=timezone.utc).timestamp() * 1000))
        _, out = run(db, panel, now=now)
        assert '0 still working, 1 off' in out

    @pytest.mark.parametrize('skew,flagged', [
        (5, False), (59_000, False), (60_000, False), (-59_000, False),
        (61_000, True), (-61_000, True)])
    def test_an_expiry_off_by_seconds_is_not_drift(self, db, skew, flagged):
        # /addmail reads the clock twice: ext_eacc5860 on prod, 2026-10-30.
        user = seed(db, '26', subscription_expiry=iso_in(20), quota_gb=100.0,
                    limit_ip=5)
        target = tier_client_values(user, config())
        panel = Panel()
        panel.has('user_26@x', total=target['totalGB'],
                  expiry=target['expiryTime'] + skew, limit=5)
        _, out = run(db, panel)
        assert ('expiry' in line(out, '26')) is flagged

    def test_an_unlimited_panel_key_is_left_unlimited(self, db):
        # Someone's decision: the old button wrote 10 GB, never 0.
        until = iso_in(20)
        seed(db, '27', subscription_expiry=until, quota_gb=100.0, limit_ip=5)
        panel = Panel()
        panel.has('user_27@x', total=0, expiry=0, limit=5)
        code, out = run(db, panel, '--apply')
        assert 'quota' not in line(out, '27') and 'expiry never ->' in line(out, '27')
        assert code == 0 and panel.rows['user_27@x']['total'] == 0
        assert panel.rows['user_27@x']['expiry'] == ms_of(until)

    def test_an_unlimited_key_that_is_off_is_enabled(self, db):
        user = seed(db, '28', subscription_expiry=iso_in(20), quota_gb=100.0,
                    limit_ip=5)
        target = tier_client_values(user, config())
        panel = Panel()
        panel.has('user_28@x', used=500 * GB, total=0,
                  expiry=target['expiryTime'], limit=5, enable=False)
        _, out = run(db, panel)
        assert line(out, '28') == '28: paid active: enable False -> True  <- fix'

    def test_the_probe_account_is_left_out(self, db):
        db._users.save(User(
            chat_id='probe_internal', status='paid', quota_gb=1.0,
            uuid='00000000-0000-0000-0000-0000000000ff', email='probe@x',
            subscription_expiry='2099-01-01T00:00:00'))
        panel = Panel()
        panel.has('probe@x', total=0, expiry=0)
        _, out = run(db, panel)
        assert out.startswith('0 payers')

    def test_a_lapsed_payers_working_key_is_listed(self, db):
        seed(db, '3', subscription_expiry=iso_in(-10))
        panel = Panel()
        old_button(panel, 'user_3@x')
        _, out = run(db, panel)
        assert line(out, '3').startswith(f"3: paid lapsed since {iso_in(-10)[:10]}: ")
        assert line(out, '3').endswith('<- still works; --lapsed switches it off')
        assert '1 still working, 0 off' in out

    def test_a_lapsed_payers_dead_key_is_off(self, db):
        until = iso_in(-10)
        seed(db, '4', subscription_expiry=until)
        panel = Panel()
        panel.has('user_4@x', expiry=ms_of(until), enable=False)
        _, out = run(db, panel)
        assert 'disabled' in line(out, '4')
        assert '0 still working, 1 off' in out

    @pytest.mark.parametrize('state', [
        {'used': 10 * GB, 'total': 10 * GB},                # spent
        {'expiry': -DAY_MS},                                # expired, not swept
        {'enable': False},                                  # disabled, no expiry
    ])
    def test_a_lapsed_key_the_panel_keeps_out_is_off(self, db, state):
        seed(db, '5', subscription_expiry=iso_in(-10))
        panel = Panel()
        if 'expiry' in state:
            state = {**state, 'expiry': now_ms() + state['expiry']}
        panel.has('user_5@x', **state)
        _, out = run(db, panel)
        assert '0 still working, 1 off' in out

    def test_without_a_traffic_row_the_record_counts(self, db):
        seed(db, '10', subscription_expiry=iso_in(20))
        panel = NoTrafficRow()
        old_button(panel, 'user_10@x')
        _, out = run(db, panel)
        assert 'quota 10 GB -> 100 GB' in line(out, '10')

    def test_demo_keys_are_left_out(self, db):
        seed(db, '6', status='demo')
        seed(db, '7', status='support_topic', previous_state='demo')
        panel = Panel()
        old_button(panel, 'user_6@x')
        old_button(panel, 'user_7@x')
        _, out = run(db, panel)
        assert out.startswith('0 payers')

    def test_a_payer_with_a_ticket_is_audited(self, db):
        seed(db, '8', status='support_topic', previous_state='paid',
             subscription_expiry=iso_in(20))
        panel = Panel()
        old_button(panel, 'user_8@x')
        _, out = run(db, panel)
        assert line(out, '8').startswith('8: support_topic active: ')

    def test_a_lost_key_is_not_an_unreadable_one(self, db):
        seed(db, '9', subscription_expiry=iso_in(20))
        lost = Panel()
        lost.has('user_9@x', attached=False)
        _, out = run(db, lost)
        assert 'not on the panel' in line(out, '9')
        assert line(out, '9').endswith('<- re-add')
        _, out = run(db, Panel(fail_read=True))
        assert 'panel unreadable' in line(out, '9')
        assert line(out, '9').endswith('<- not touched')
        assert 'Panel unreadable: 1.' in out

    def test_lapsed_goes_with_apply(self, db):
        with pytest.raises(SystemExit) as exit_:
            run(db, Panel(), '--lapsed')
        assert exit_.value.code == 2


class TestApply:

    def test_active_payers_get_the_paid_tier(self, db):
        until = iso_in(20)
        seed(db, '11', subscription_expiry=until)
        seed(db, '12', subscription_expiry=iso_in(-10))
        panel = Panel()
        old_button(panel, 'user_11@x', used=7 * GB)
        old_button(panel, 'user_12@x')
        code, out = run(db, panel, '--apply')
        assert code == 0 and 'written 1, failed: none' in out
        assert panel.rows['user_11@x'] == {
            'used': 7 * GB, 'total': 100 * GB, 'expiry': ms_of(until),
            'enable': True, 'limit': 5}
        assert panel.writes('user_11@x') == ['update']   # never an add
        user = db.get_user('11')
        assert (user.quota_gb, user.limit_ip) == (100.0, 5)
        assert panel.works('user_12@x')                  # lapsed: no --lapsed
        assert [target for target, _ in audit(db)] == ['11']

    def test_lapsed_switches_working_keys_off(self, db):
        until = iso_in(-10)
        seed(db, '13', subscription_expiry=until)
        panel = Panel()
        old_button(panel, 'user_13@x')
        code, _ = run(db, panel, '--apply', '--lapsed')
        assert code == 0
        assert not panel.works('user_13@x')
        assert panel.rows['user_13@x']['expiry'] == ms_of(until)
        assert panel.rows['user_13@x']['enable'] is False
        [(target, details)] = audit(db)
        assert target == '13' and details.startswith('lapsed: off')

    def test_a_dead_lapsed_key_is_not_written(self, db):
        until = iso_in(-10)
        seed(db, '14', subscription_expiry=until)
        panel = Panel()
        panel.has('user_14@x', expiry=ms_of(until), enable=False)
        run(db, panel, '--apply', '--lapsed')
        assert panel.writes('user_14@x') == [] and audit(db) == []

    def test_a_disabled_key_with_quota_left_is_enabled(self, db):
        user = seed(db, '21', subscription_expiry=iso_in(20), quota_gb=100.0,
                    limit_ip=5)
        target = tier_client_values(user, config())
        panel = Panel()
        panel.has('user_21@x', used=40 * GB, total=target['totalGB'],
                  expiry=target['expiryTime'], limit=5, enable=False)
        code, out = run(db, panel, '--apply')
        assert 'enable False -> True' in line(out, '21')
        assert code == 0 and panel.works('user_21@x')

    def test_a_key_that_spent_the_paid_quota_stays_off(self, db):
        user = seed(db, '22', subscription_expiry=iso_in(20), quota_gb=100.0,
                    limit_ip=5)
        target = tier_client_values(user, config())
        panel = Panel()
        panel.has('user_22@x', used=100 * GB, total=target['totalGB'],
                  expiry=target['expiryTime'], limit=5, enable=False)
        _, out = run(db, panel, '--apply')
        assert line(out, '22') == '22: paid active: ok'
        assert panel.writes('user_22@x') == []

    def test_a_key_back_meanwhile_is_not_added_again(self, db):
        # An add of a client the panel has is delete + re-add: zeroed traffic.
        seed(db, '23', subscription_expiry=iso_in(20))
        panel = BackMeanwhile()
        panel.has('user_23@x', used=3 * GB, attached=False)
        code, out = run(db, panel, '--apply')
        assert code == 1 and 'failed: 23' in out
        assert panel.writes('user_23@x') == []
        assert panel.rows['user_23@x']['used'] == 3 * GB

    def test_a_lost_key_comes_back_under_its_uuid(self, db):
        # The bot's floors are right already: the key alone is the change.
        seed(db, '15', subscription_expiry=iso_in(20), quota_gb=100.0,
             limit_ip=5)
        panel = Panel()
        panel.has('user_15@x', used=2 * GB, enable=False, attached=False)
        code, _ = run(db, panel, '--apply')
        assert code == 0 and panel.works('user_15@x')
        assert panel.writes('user_15@x') == ['add', 'update']
        assert panel.records['user_15@x']['id'] == db.get_user('15').uuid
        assert panel.rows['user_15@x']['used'] == 2 * GB

    def test_an_unreadable_key_is_not_touched(self, db):
        seed(db, '16', subscription_expiry=iso_in(20))
        panel = Panel(fail_read=True)
        code, _ = run(db, panel, '--apply')
        assert code == 0 and panel.writes('user_16@x') == []
        assert db.get_user('16').quota_gb == 5.0 and audit(db) == []

    def test_the_bots_copy_alone_needs_no_panel_write(self, db):
        user = seed(db, '17', subscription_expiry=iso_in(20), limit_ip=5)
        target = tier_client_values(user, config())
        panel = Panel()
        panel.has('user_17@x', total=target['totalGB'],
                  expiry=target['expiryTime'], limit=target['limitIp'])
        code, _ = run(db, panel, '--apply')
        assert code == 0 and panel.writes('user_17@x') == []
        assert db.get_user('17').quota_gb == 100.0
        assert [target for target, _ in audit(db)] == ['17']

    def test_a_second_run_writes_nothing(self, db):
        seed(db, '18', subscription_expiry=iso_in(20))
        seed(db, '19', subscription_expiry=iso_in(-10))
        panel = Panel()
        old_button(panel, 'user_18@x')
        old_button(panel, 'user_19@x')
        run(db, panel, '--apply', '--lapsed')
        writes = len(panel.writes('user_18@x') + panel.writes('user_19@x'))
        code, out = run(db, panel, '--apply', '--lapsed')
        assert code == 0 and 'written 0' in out
        assert len(panel.writes('user_18@x') + panel.writes('user_19@x')) == writes

    def test_a_failed_write_is_left_to_the_next_run(self, db):
        seed(db, '20', subscription_expiry=iso_in(20))
        panel = Panel(fail_update=True)
        old_button(panel, 'user_20@x')
        code, out = run(db, panel, '--apply')
        assert code == 1 and 'failed: 20' in out and audit(db) == []
        assert db.get_user('20').quota_gb == 100.0      # the bot's copy first
        panel.fail_update = False
        assert run(db, panel, '--apply')[0] == 0
        assert panel.rows['user_20@x']['total'] == 100 * GB
        assert [target for target, _ in audit(db)] == ['20']


class TestRunAsAFile:
    """The documented call runs the file with no PYTHONPATH."""

    def test_dry_run_from_a_bare_interpreter(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['DB_PATH'] = str(tmp_path / 'bot.db')
        env['XUI_API_URL'] = ''
        r = subprocess.run(
            [sys.executable, os.path.join('scripts', 'paid_keys_audit.py')],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert r.stdout.startswith('0 payers') and 'dry run' in r.stdout
