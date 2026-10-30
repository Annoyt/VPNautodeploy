"""A paid key may be used from 5 addresses (PAID_LIMIT_IP), not 1.

The exit panel enforces limitIp with fail2ban: one address over the limit
gets the older one banned for 30 min. Real sqlite (users, admin_actions);
the panel is faked. Covers every path that writes a paid key's limit: the
payment (grant_paid_access), /addmail, «🔑 Мой ключ» (which used to re-add
the key with demo values) and scripts/paid_limit_ip.py for keys paid
before.
"""

import importlib.util
import io
import os
import subprocess
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot.core.database import Database
from bot.handlers.admin.ops import AdminOpsMixin
from bot.handlers.callbacks.user import GetKeyHandler
from bot.models.user import User
from bot.services.billing import grant_paid_access

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_spec = importlib.util.spec_from_file_location(
    'paid_limit_ip', os.path.join(ROOT, 'scripts', 'paid_limit_ip.py'))
pli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pli)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

PAID_UNTIL = datetime.now() + timedelta(days=30)


def config(**over):
    cfg = SimpleNamespace(
        PAID_LIMIT_IP=5, PAID_TRAFFIC_GB=100, DEMO_TRAFFIC_GB=10, DEMO_DAYS=30,
        INBOUND_ID=1, ENTRY_NODE_IP='203.0.113.1', REALITY_PUBLIC_KEY='pk',
        SNI_VALUE='www.bing.com', WEBAPP_URL='https://bot.example',
        BOT_TOKEN='t', SUPER_ADMIN_ID=1)
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def seed(db, chat_id, status='paid', limit_ip=1, key=True, **extra):
    db._users.save(User(
        chat_id=chat_id, username=f'u{chat_id}', status=status,
        limit_ip=limit_ip,
        uuid=f'00000000-0000-0000-0000-{int(chat_id):012d}' if key else None,
        email=f'user_{chat_id}@x' if key else None, **extra))
    return db.get_user(chat_id)


class FakePanel:
    """limitIp per email; an update fails for emails in ``fail``."""

    def __init__(self, limits=None, fail=(), attached=True):
        self.limits = dict(limits or {})
        self.fail = set(fail)
        self.attached = attached
        self.updates = []
        self.added = []

    def get_client_sync(self, email):
        if email not in self.limits:
            return None
        return {'email': email, 'limitIp': self.limits[email]}

    def sync_client_settings_sync(self, email, updates):
        self.updates.append((email, dict(updates)))
        if email in self.fail:
            return False
        if 'limitIp' in updates:
            self.limits[email] = updates['limitIp']
        return True

    def add_client_sync(self, client):
        self.added.append(dict(client))
        return True

    def client_attached_sync(self, email):
        return self.attached

    async def sync_user(self, chat_id, client):
        self.added.append(dict(client))
        return True


def audit(db):
    with db._connect() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT target_id, details FROM admin_actions WHERE action = ? "
            "ORDER BY id", (pli.ACTION,))]


class TestPayment:

    def test_a_payment_gives_five_addresses_in_both_stores(self, db):
        seed(db, '11', status='demo')
        panel = FakePanel()
        grant_paid_access(db, config(), panel, '11', PAID_UNTIL)
        assert db.get_user('11').limit_ip == 5
        assert panel.updates[-1][1]['limitIp'] == 5

    def test_an_admins_no_limit_survives_the_round_trip(self, db):
        # 0 = no limit (/set_limit @user 0): stored, read back, kept.
        seed(db, '12', limit_ip=0)
        panel = FakePanel()
        grant_paid_access(db, config(), panel, '12', PAID_UNTIL)
        assert db.get_user('12').limit_ip == 0
        assert panel.updates[-1][1]['limitIp'] == 0


class TestAddmail:

    def provision(self, db, panel, email, status='paid'):
        handler = AdminOpsMixin.__new__(AdminOpsMixin)
        handler.bot = SimpleNamespace(services={'xui': panel})
        handler.db = db
        handler.config = config()
        handler._provision_email_user(email, 50, 30, status=status)
        with db._connect() as conn:
            chat_id = conn.execute(
                "SELECT chat_id FROM users WHERE contact_email = ?",
                (email,)).fetchone()[0]
        return db.get_user(chat_id)

    def test_a_paid_mail_user_gets_five(self, db):
        panel = FakePanel()
        user = self.provision(db, panel, 'a@example.com')
        assert user.limit_ip == 5
        assert panel.added[-1]['limitIp'] == 5

    def test_a_reissue_keeps_a_raised_limit(self, db):
        panel = FakePanel()
        user = self.provision(db, panel, 'b@example.com')
        user.limit_ip = 10
        db.save_user(user)
        assert self.provision(db, panel, 'b@example.com').limit_ip == 10
        assert panel.added[-1]['limitIp'] == 10

    def test_a_demo_mail_user_keeps_one(self, db):
        panel = FakePanel()
        user = self.provision(db, panel, 'c@example.com', status='demo')
        assert user.limit_ip == 1
        assert panel.added[-1]['limitIp'] == 1


class TestMyKey:
    """«🔑 Мой ключ» on the main menu runs GetKeyHandler for a key holder."""

    def press(self, db, panel, chat_id):
        GetKeyHandler._inflight_chat_ids.clear()
        bot = Mock()
        bot.services = {'xui': panel}
        cfg = Mock()
        cfg.DEMO_TRAFFIC_GB = 10
        handler = GetKeyHandler(bot, db, cfg)
        with patch.object(handler, '_send_key_to_user', new_callable=AsyncMock) as sent:
            import asyncio
            asyncio.run(handler._process_key_request(chat_id))
        sent.assert_awaited_once()

    def test_a_key_the_panel_has_is_left_alone(self, db):
        seed(db, '21', limit_ip=5)
        panel = FakePanel(attached=True)
        self.press(db, panel, '21')
        assert panel.added == []

    def test_a_lost_key_comes_back_with_its_own_limit(self, db):
        seed(db, '22', limit_ip=5)
        panel = FakePanel(attached=False)
        self.press(db, panel, '22')
        assert [c['limitIp'] for c in panel.added] == [5]


class TestBackfill:

    @pytest.fixture
    def users(self, db):
        seed(db, '1')                                    # paid, panel 0
        seed(db, '2', limit_ip=10)                       # raised by hand
        seed(db, '3', limit_ip=0)                        # no limit by hand
        seed(db, '4', status='support_topic',            # payer with a ticket
             subscription_expiry=PAID_UNTIL.isoformat())
        seed(db, '5', status='support_topic', previous_state='paid')
        seed(db, '6', status='support_topic', previous_state='demo')
        seed(db, '7', status='demo')
        seed(db, '8', key=False)                         # paid, no key yet
        return FakePanel({'user_1@x': 0, 'user_2@x': 10, 'user_3@x': 0,
                          'user_4@x': 1, 'user_5@x': 3, 'user_6@x': 1,
                          'user_7@x': 0})

    def run(self, db, panel, *argv):
        out = io.StringIO()
        code = pli.main(list(argv), db=db, xui=panel, config=config(), out=out)
        return code, out.getvalue()

    def test_dry_run_writes_nothing(self, db, users):
        code, out = self.run(db, users)
        assert code == 0
        assert 'dry run: 5 payers' in out
        assert users.updates == [] and audit(db) == []
        assert db.get_user('1').limit_ip == 1

    def test_apply_raises_payers_only(self, db, users):
        code, out = self.run(db, users, '--apply')
        assert code == 0
        assert users.limits == {
            'user_1@x': 5, 'user_2@x': 10, 'user_3@x': 0, 'user_4@x': 5,
            'user_5@x': 5, 'user_6@x': 1, 'user_7@x': 0}
        assert [db.get_user(c).limit_ip for c in '1234567'] == [5, 10, 0, 5, 5, 1, 1]
        assert sorted(audit(db)) == [('1', '0->5'), ('4', '1->5'), ('5', '3->5')]

    def test_a_second_run_changes_nothing(self, db, users):
        self.run(db, users, '--apply')
        users.updates.clear()
        code, out = self.run(db, users, '--apply')
        assert code == 0 and users.updates == []
        assert 'updated 0' in out

    def test_a_panel_drift_alone_is_fixed(self, db, users):
        # The bot says 5, the panel still 1: the panel decides.
        user = db.get_user('1')
        user.limit_ip = 5
        db.save_user(user)
        users.limits['user_1@x'] = 1
        self.run(db, users, '--apply')
        assert users.limits['user_1@x'] == 5

    def test_a_failed_panel_update_is_retried_next_run(self, db, users):
        users.fail.add('user_1@x')
        code, out = self.run(db, users, '--apply')
        assert code == 1 and 'failed: 1' in out
        assert ('1', '0->5') not in audit(db)
        assert db.get_user('1').limit_ip == 5      # saved before the panel
        users.fail.clear()
        assert self.run(db, users, '--apply')[0] == 0
        assert users.limits['user_1@x'] == 5
        assert ('1', '0->5') in audit(db)


class TestRunAsAFile:
    """The documented call runs the file with no PYTHONPATH."""

    def test_dry_run_from_a_bare_interpreter(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['DB_PATH'] = str(tmp_path / 'bot.db')
        env['XUI_API_URL'] = ''
        r = subprocess.run(
            [sys.executable, os.path.join('scripts', 'paid_limit_ip.py')],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert 'dry run: 0 payers' in r.stdout
