"""«🔑 Мой ключ» puts a key the panel lost back with the user's own tier.

The button (main menu → GetKeyHandler) leaves a key the panel still has
alone. A lost one (3x-ui detaches long-expired clients, a panel wipe)
used to come back as a never-expiring 10 GB key whatever the tier: a
lapsed payer got a free key, an active payer lost their quota and date.

Real sqlite (users); the panel is faked with the semantics of the exit
panel's fork (``relational_panel.Panel``): an add of an email the panel
still has is delete + re-add (used bytes zeroed), an add of a detached
email leaves its traffic row as it was, an update never touches used
bytes.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot.config.constants import BYTES_PER_GB as GB
from bot.core.database import Database
from bot.handlers.callbacks.user import GetKeyHandler
from bot.models.user import User
from bot.services.billing import grant_paid_access

from .relational_panel import Panel, now_ms

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

DAY_MS = 86_400_000


def config(**over):
    cfg = SimpleNamespace(
        PAID_LIMIT_IP=5, PAID_TRAFFIC_GB=100, DEMO_TRAFFIC_GB=10,
        DEMO_OTHER_TRAFFIC_GB=5, DEMO_DAYS=30,
        INBOUND_ID=1, ENTRY_NODE_IP='203.0.113.1', REALITY_PUBLIC_KEY='pk',
        SNI_VALUE='www.bing.com', WEBAPP_URL='https://bot.example',
        BOT_TOKEN='t', SUPER_ADMIN_ID=1)
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_in(days):
    """A paid-until date ``days`` from now, at 23:59 like /expire writes it."""
    day = utcnow() + timedelta(days=days)
    return day.replace(hour=23, minute=59, second=0, microsecond=0).isoformat()


def ms_of(iso):
    """The panel's expiry for a paid-until date, converted the way
    grant_paid_access converts it."""
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


def press(db, panel, chat_id, cfg=None):
    """«🔑 Мой ключ»: returns the handler's bot and whether the key card went out."""
    GetKeyHandler._inflight_chat_ids.clear()
    bot = Mock()
    bot.services = {'xui': panel} if panel is not None else {}
    handler = GetKeyHandler(bot, db, cfg or config())
    with patch.object(handler, '_send_key_to_user', new_callable=AsyncMock) as card:
        asyncio.run(handler._async_handle_get_key(chat_id))
    return bot, card.await_count == 1


def texts(bot):
    return [c.kwargs.get('text', '') for c in bot.send_message.call_args_list]


class TestPayer:

    def test_a_payer_gets_the_paid_tier_back(self, db):
        # The panel's own expiry ran out before the bot's date (an
        # /expire whose panel sync failed): disabled, then detached.
        until = iso_in(20)
        seed(db, '11', subscription_expiry=until)
        panel = Panel()
        panel.has('user_11@x', used=3 * GB, expiry=now_ms() - DAY_MS,
                  enable=False, attached=False)
        _, card = press(db, panel, '11')
        assert card
        assert panel.rows['user_11@x'] == {
            'used': 3 * GB, 'total': 100 * GB, 'expiry': ms_of(until),
            'enable': True, 'limit': 5}
        assert panel.records['user_11@x']['id'] == db.get_user('11').uuid
        assert panel.works('user_11@x')
        assert panel.writes('user_11@x') == ['add', 'update']

    def test_a_wiped_payer_key_comes_back_whole(self, db):
        until = iso_in(20)
        seed(db, '12', subscription_expiry=until)
        panel = Panel()
        _, card = press(db, panel, '12')
        assert card and panel.works('user_12@x')
        assert panel.rows['user_12@x']['total'] == 100 * GB
        assert panel.rows['user_12@x']['expiry'] == ms_of(until)

    def test_a_raised_quota_and_limit_survive(self, db):
        seed(db, '13', subscription_expiry=iso_in(20), quota_gb=500.0,
             limit_ip=10)
        panel = Panel()
        press(db, panel, '13')
        assert panel.rows['user_13@x']['total'] == 500 * GB
        assert panel.rows['user_13@x']['limit'] == 10

    def test_an_admins_unlimited_quota_stays(self, db):
        # /quota @user 0: unlimited, totalGB 0 on the panel.
        seed(db, '18', subscription_expiry=iso_in(20), quota_gb=0.0)
        panel = Panel()
        press(db, panel, '18')
        assert panel.rows['user_18@x']['total'] == 0

    def test_an_admins_no_limit_stays(self, db):
        seed(db, '14', subscription_expiry=iso_in(20), limit_ip=0)
        panel = Panel()
        press(db, panel, '14')
        assert panel.rows['user_14@x']['limit'] == 0

    def test_a_payer_with_a_ticket_keeps_the_ticket(self, db):
        # Why the button does not call grant_paid_access: it would move
        # an open ticket to 'paid'.
        seed(db, '15', status='support_topic', previous_state='paid',
             subscription_expiry=iso_in(20))
        panel = Panel()
        _, card = press(db, panel, '15')
        assert card and panel.works('user_15@x')
        assert panel.rows['user_15@x']['total'] == 100 * GB
        assert db.get_user('15').status == 'support_topic'

    def test_a_payer_without_a_date_has_no_expiry(self, db):
        # No date = no limit, the payer rule's own reading (ru_exit).
        seed(db, '16')
        panel = Panel()
        press(db, panel, '16')
        assert panel.rows['user_16@x']['expiry'] == 0
        assert panel.rows['user_16@x']['total'] == 100 * GB

    def test_the_values_are_what_a_payment_writes(self, db):
        until = utcnow() + timedelta(days=20)
        seed(db, '17', status='demo', quota_gb=10.0)
        paid = Panel()
        paid.has('user_17@x')
        grant_paid_access(db, config(), paid, '17', until)
        lost = Panel()
        press(db, lost, '17')
        assert lost.rows['user_17@x'] == paid.rows['user_17@x']


class TestLapsedPayer:

    def test_a_lapsed_payers_lost_key_stays_off(self, db):
        until = iso_in(-10)
        seed(db, '21', subscription_expiry=until)
        panel = Panel()
        panel.has('user_21@x', expiry=ms_of(until), enable=False,
                  attached=False)
        bot, card = press(db, panel, '21')
        assert not card
        assert panel.writes('user_21@x') == []
        assert not panel.works('user_21@x')
        [text] = texts(bot)
        assert '/buy' in text
        assert datetime.fromisoformat(until).strftime('%d.%m.%Y') in text
        keyboard = bot.send_message.call_args.kwargs['reply_markup']
        assert keyboard['inline_keyboard'][0][0]['callback_data'] == 'buy_menu'

    def test_a_lapsed_payer_with_a_ticket_gets_the_hint(self, db):
        seed(db, '22', status='support_topic', previous_state='paid',
             subscription_expiry=iso_in(-10))
        panel = Panel()
        bot, card = press(db, panel, '22')
        assert not card and panel.writes('user_22@x') == []
        assert '/buy' in texts(bot)[0]

    def test_the_hint_speaks_english(self, db):
        seed(db, '23', subscription_expiry=iso_in(-10), lang='en')
        bot, _ = press(db, Panel(), '23')
        assert 'Renew it with /buy' in texts(bot)[0]

    def test_the_hint_needs_no_panel(self, db):
        seed(db, '24', subscription_expiry=iso_in(-10))
        bot, card = press(db, None, '24')
        assert not card and '/buy' in texts(bot)[0]

    def test_an_attached_lapsed_key_is_left_alone(self, db):
        # Nothing to re-add: the card goes out as before, as /mykey's does.
        until = iso_in(-10)
        seed(db, '25', subscription_expiry=until)
        panel = Panel()
        panel.has('user_25@x', expiry=ms_of(until), enable=False)
        _, card = press(db, panel, '25')
        assert card and panel.writes('user_25@x') == []

    def test_a_payment_brings_the_same_key_back(self, db):
        # What the hint promises: grant_paid_access re-adds the lost
        # client under the uuid the user's apps already hold.
        seed(db, '26', subscription_expiry=iso_in(-10))
        panel = Panel()
        press(db, panel, '26')
        grant_paid_access(db, config(), panel, '26',
                          utcnow() + timedelta(days=30))
        assert panel.works('user_26@x')
        assert panel.records['user_26@x']['id'] == db.get_user('26').uuid


class TestDemo:
    """A demo key comes back with its own monthly quota: users.quota_gb,
    which the monthly reset (10 GB with the NekoVPN app, 5 otherwise),
    the app's raise and an admin's gift write (demo_tiers)."""

    def test_a_lost_demo_key_comes_back_as_a_demo_key(self, db):
        seed(db, '31', status='demo', quota_gb=10.0)       # an app key
        panel = Panel()
        panel.has('user_31@x', used=4 * GB, expiry=now_ms() - 40 * DAY_MS,
                  enable=False, attached=False)
        before = now_ms()
        _, card = press(db, panel, '31')
        row = panel.rows['user_31@x']
        assert card and panel.works('user_31@x')
        assert row['total'] == 10 * GB and row['limit'] == 2   # DEMO_LIMIT_IP
        assert before + 30 * DAY_MS <= row['expiry'] <= now_ms() + 30 * DAY_MS
        assert row['used'] == 4 * GB             # the month's use still counts

    def test_a_key_on_the_other_allowance_keeps_it(self, db):
        seed(db, '36', status='demo', quota_gb=5.0)
        panel = Panel()
        press(db, panel, '36')
        assert panel.rows['user_36@x']['total'] == 5 * GB

    def test_an_admins_gift_survives(self, db):
        seed(db, '37', status='demo', quota_gb=110.0)
        panel = Panel()
        press(db, panel, '37')
        assert panel.rows['user_37@x']['total'] == 110 * GB

    def test_an_unlimited_demo_key_stays_unlimited(self, db):
        seed(db, '39', status='demo', quota_gb=0.0)
        panel = Panel()
        press(db, panel, '39')
        assert panel.rows['user_39@x']['total'] == 0

    def test_no_quota_below_the_other_allowance(self, db):
        seed(db, '38', status='demo', quota_gb=2.0)
        panel = Panel()
        press(db, panel, '38', config(DEMO_OTHER_TRAFFIC_GB=7))
        assert panel.rows['user_38@x']['total'] == 7 * GB

    def test_a_spent_month_is_not_granted_again(self, db):
        seed(db, '32', status='demo', quota_gb=10.0)
        panel = Panel()
        panel.has('user_32@x', used=10 * GB, expiry=now_ms() - 40 * DAY_MS,
                  enable=False, attached=False)
        press(db, panel, '32')
        assert panel.rows['user_32@x']['used'] == 10 * GB
        assert not panel.works('user_32@x')

    def test_a_spent_attached_key_is_left_alone(self, db):
        seed(db, '33', status='demo')
        panel = Panel()
        panel.has('user_33@x', used=10 * GB)
        _, card = press(db, panel, '33')
        assert card and panel.writes('user_33@x') == []
        assert panel.rows['user_33@x']['used'] == 10 * GB

    def test_a_demo_user_with_a_ticket_gets_a_demo_key(self, db):
        seed(db, '34', status='support_topic', previous_state='demo',
             quota_gb=10.0)
        panel = Panel()
        press(db, panel, '34')
        assert panel.rows['user_34@x']['total'] == 10 * GB
        assert panel.rows['user_34@x']['expiry'] > 0

    def test_a_raised_demo_limit_survives(self, db):
        seed(db, '35', status='demo', limit_ip=3)
        panel = Panel()
        press(db, panel, '35')
        assert panel.rows['user_35@x']['limit'] == 3


class TestFailures:
    """One attempt: a retried add of a client the first attempt already
    put back is delete + re-add, which zeroes its traffic."""

    def test_a_failed_add_is_reported_once(self, db):
        seed(db, '41', status='demo')
        panel = Panel(fail_add=True)
        bot, card = press(db, panel, '41')
        assert not card
        assert panel.writes('user_41@x') == ['add']
        assert 'Не удалось восстановить' in texts(bot)[0]

    def test_a_failed_update_is_reported(self, db):
        # The add alone leaves the stale row gating the key.
        seed(db, '42', status='demo')
        panel = Panel(fail_update=True)
        panel.has('user_42@x', used=GB, enable=False, attached=False)
        bot, card = press(db, panel, '42')
        assert not card and not panel.works('user_42@x')
        assert panel.writes('user_42@x') == ['add', 'update']
        assert 'Не удалось восстановить' in texts(bot)[0]

    def test_no_panel_service(self, db):
        seed(db, '43', status='demo')
        bot, card = press(db, None, '43')
        assert not card and 'временно недоступен' in texts(bot)[0]
