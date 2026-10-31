"""Address limits per tier: a paid key 5 addresses (PAID_LIMIT_IP), a
demo key 2 (DEMO_LIMIT_IP).

The exit panel enforces limitIp with fail2ban: one address over the limit
gets the older one banned for 30 min. Real sqlite (users, admin_actions);
the panel is faked. Covers every path that writes a key's limit: the
payment (grant_paid_access), /addmail, a new Telegram key, «🔑 Мой ключ»
(which used to re-add the key with demo values) and scripts/limit_ip.py
for keys made before.
"""

import importlib.util
import io
import json
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
    'limit_ip', os.path.join(ROOT, 'scripts', 'limit_ip.py'))
lip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lip)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

PAID_UNTIL = datetime.now() + timedelta(days=30)


def config(**over):
    cfg = SimpleNamespace(
        PAID_LIMIT_IP=5, DEMO_LIMIT_IP=2, PAID_TRAFFIC_GB=100,
        DEMO_TRAFFIC_GB=10, DEMO_OTHER_TRAFFIC_GB=5, DEMO_DAYS=30,
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
    """limitIp per email on inbound 1; an update fails for emails in
    ``fail``."""

    def __init__(self, limits=None, fail=(), attached=True):
        self.limits = dict(limits or {})
        self.fail = set(fail)
        self.attached = attached
        self.updates = []
        self.added = []

    def get_inbound_settings_sync(self, inbound_id):
        if inbound_id != 1:
            return None
        clients = [{'email': e, 'limitIp': v} for e, v in self.limits.items()]
        return {'id': 1, 'settings': json.dumps({'clients': clients})}

    def sync_client_settings_sync(self, email, updates):
        self.updates.append((email, dict(updates)))
        if email in self.fail:
            return False
        if 'limitIp' in updates:
            self.limits[email] = updates['limitIp']
        return True

    def add_client_sync(self, client, inbound_id=None):
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
            "ORDER BY id", (lip.ACTION,))]


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

    def test_a_demo_mail_user_gets_two(self, db):
        panel = FakePanel()
        user = self.provision(db, panel, 'c@example.com', status='demo')
        assert user.limit_ip == 2
        assert panel.added[-1]['limitIp'] == 2


class TestNewKey:

    def test_a_new_telegram_key_gets_two(self, db):
        GetKeyHandler._inflight_chat_ids.clear()
        db._users.save(User(chat_id='31', username='u31', status='demo'))
        panel = FakePanel()
        bot = Mock()
        bot.services = {'xui': panel}
        handler = GetKeyHandler(bot, db, config())
        with patch.object(handler, '_send_key_to_user', new_callable=AsyncMock):
            import asyncio
            asyncio.run(handler._process_key_request('31'))
        assert panel.added[-1]['limitIp'] == 2
        assert db.get_user('31').limit_ip == 2


class TestMyKey:
    """«🔑 Мой ключ» on the main menu runs GetKeyHandler for a key holder."""

    def press(self, db, panel, chat_id):
        GetKeyHandler._inflight_chat_ids.clear()
        bot = Mock()
        bot.services = {'xui': panel}
        handler = GetKeyHandler(bot, db, config())
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

    def test_a_lost_demo_key_comes_back_with_two(self, db):
        seed(db, '23', status='demo', limit_ip=1, quota_gb=5.0)
        panel = FakePanel(attached=False)
        self.press(db, panel, '23')
        assert [c['limitIp'] for c in panel.added] == [2]


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
        seed(db, '9', status='demo', limit_ip=3)         # demo raised by hand
        seed(db, '10', status='banned')                  # no tier
        return FakePanel({'user_1@x': 0, 'user_2@x': 10, 'user_3@x': 0,
                          'user_4@x': 1, 'user_5@x': 3, 'user_6@x': 1,
                          'user_7@x': 0, 'user_9@x': 3, 'user_10@x': 1})

    def run(self, db, panel, *argv):
        out = io.StringIO()
        code = lip.main(list(argv), db=db, xui=panel, config=config(), out=out)
        return code, out.getvalue()

    def test_dry_run_writes_nothing(self, db, users):
        code, out = self.run(db, users)
        assert code == 0
        assert 'dry run: paid 5, demo 3 keys, 5 to change' in out
        assert users.updates == [] and audit(db) == []
        assert db.get_user('1').limit_ip == 1

    def test_apply_gives_each_tier_its_limit(self, db, users):
        code, out = self.run(db, users, '--apply')
        assert code == 0
        assert users.limits == {
            'user_1@x': 5, 'user_2@x': 10, 'user_3@x': 0, 'user_4@x': 5,
            'user_5@x': 5, 'user_6@x': 2, 'user_7@x': 2, 'user_9@x': 3,
            'user_10@x': 1}
        assert ([db.get_user(str(c)).limit_ip for c in range(1, 11) if c != 8]
                == [5, 10, 0, 5, 5, 2, 2, 3, 1])
        assert sorted(audit(db)) == [
            ('1', 'paid 0->5'), ('4', 'paid 1->5'), ('5', 'paid 3->5'),
            ('6', 'demo 1->2'), ('7', 'demo 0->2')]

    def test_one_tier(self, db, users):
        self.run(db, users, '--apply', '--tier', 'demo')
        assert sorted(audit(db)) == [('6', 'demo 1->2'), ('7', 'demo 0->2')]
        assert users.limits['user_1@x'] == 0

    def test_a_key_the_panel_does_not_show_is_set(self, db, users):
        del users.limits['user_7@x']
        self.run(db, users, '--apply')
        assert ('7', 'demo None->2') in audit(db)

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
        assert ('1', 'paid 0->5') not in audit(db)
        assert db.get_user('1').limit_ip == 5      # saved before the panel
        users.fail.clear()
        assert self.run(db, users, '--apply')[0] == 0
        assert users.limits['user_1@x'] == 5
        assert ('1', 'paid 0->5') in audit(db)


class TestPanelRead:

    def test_every_key_inbound_once_shadowsocks_first(self):
        cfg = SimpleNamespace(SS_INBOUND_ID=5, INBOUND_ID=1, WS_INBOUND_ID=4,
                              WS2_INBOUND_ID=0)
        assert lip.inbound_ids(cfg) == [5, 1, 4]
        assert lip.inbound_ids(SimpleNamespace(INBOUND_ID=1, SS_INBOUND_ID=1)) == [1]

    def test_the_first_inbound_that_has_the_key_answers(self):
        # Like the bot's own reads: the Shadowsocks inbound first.
        class TwoInbounds:
            def get_inbound_settings_sync(self, iid):
                limit = {5: 2, 1: 1}.get(iid)
                if limit is None:
                    return None
                return {'settings': json.dumps(
                    {'clients': [{'email': 'a@x', 'limitIp': limit}]})}
        cfg = SimpleNamespace(SS_INBOUND_ID=5, INBOUND_ID=1)
        assert lip.panel_reader(TwoInbounds(), cfg)('a@x') == 2


class TestRunAsAFile:
    """The documented call runs the file with no PYTHONPATH."""

    def test_dry_run_from_a_bare_interpreter(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['DB_PATH'] = str(tmp_path / 'bot.db')
        env['XUI_API_URL'] = ''
        r = subprocess.run(
            [sys.executable, os.path.join('scripts', 'limit_ip.py')],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert 'dry run: paid 0, demo 0 keys, 0 to change' in r.stdout
