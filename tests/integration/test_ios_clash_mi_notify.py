"""scripts/ios_clash_mi_notify.py — iPhone key holders get Clash Mi in the
App Store and their own Clash subscription (AGENTS.md §69).

Real sqlite (users, admin_actions); Telegram and the bot's listener faked.
"""

import importlib.util
import io
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from bot.core.database import Database
from bot.models.user import User
from bot.services import neko_client as nc
from bot.services.subscription import SubscriptionService

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_spec = importlib.util.spec_from_file_location(
    'ios_clash_mi_notify', os.path.join(ROOT, 'scripts', 'ios_clash_mi_notify.py'))
icm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(icm)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

BASE = 'https://sub.example.test:9443'
CONFIG = SimpleNamespace(SUB_PUBLIC_URL=BASE, SUB_TOKEN_SECRET='pinned-secret',
                         BOT_TOKEN='bot-token-not-used')
NOW = datetime(2026, 11, 25, 12, 0)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def holder(db, chat_id, status='demo', uuid=True, platform='ios', **fields):
    db._users.save(User(
        chat_id=chat_id, username=f'u{chat_id}', status=status, platform=platform,
        uuid=f'22222222-0000-0000-0000-{abs(hash(chat_id)) % 10**12:012d}' if uuid else None,
        email=f'{chat_id}@x' if uuid else None, **fields))
    return db._users.get_by_id(chat_id)


def token_of(user):
    return SubscriptionService(CONFIG).derive_token(user.uuid)


class FakeTG:
    def __init__(self, fail=()):
        self.sent = []
        self.fail = set(fail)

    def send_message(self, **kw):
        self.sent.append(kw)
        if kw['chat_id'] in self.fail:
            return None
        return {'message_id': 2000 + len(self.sent)}


class FakeResp:
    def __init__(self, status, text=''):
        self.status_code = status
        self.text = text


def run(db, *argv, tg=None, check=None):
    out = io.StringIO()
    code = icm.main(list(argv), db=db, tg=tg, out=out, config=CONFIG,
                    check=check or (lambda url, **kw: FakeResp(200, 'clashmi://x')),
                    now=NOW)
    return code, out.getvalue()


def audits(db):
    with db._connect() as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT target_id, details FROM admin_actions WHERE action = ? "
            "ORDER BY id", (icm.ACTION,)).fetchall()]


class TestPlan:

    def test_verdicts(self, db):
        holder(db, '1')
        holder(db, '2', platform='android')
        holder(db, '3', status='paid', subscription_expiry='2026-11-01T00:00:00')
        holder(db, '4', status='paid', subscription_expiry='2026-12-31T00:00:00')
        holder(db, 'ext_ab12', status='paid')
        holder(db, '5', status='banned')
        holder(db, '6')
        db.log_admin_action('owner', icm.ACTION, '6', icm.TAG)
        holder(db, '7', status='support_topic')
        ids = icm.crn.key_holders(db)
        got = [(cid, v) for cid, v, _ in icm.plan(db, ids, CONFIG, now=NOW)]
        assert got == [
            ('1', 'send'),
            ('2', 'not iPhone'),
            ('3', 'lapsed (paid until 2026-11-01)'),
            ('4', 'send'),
            ('ext_ab12', 'not a Telegram chat'),
            ('6', 'sent before'),
            ('7', 'send'),
        ]

    def test_to_skips_the_platform_check(self, db):
        holder(db, '9', platform='android')
        got = icm.plan(db, ['9', '404'], CONFIG, everyone=False, now=NOW)
        assert [(c, v) for c, v, _ in got] == [('9', 'send'), ('404', 'no such user')]

    def test_no_public_address_no_send(self, db):
        holder(db, '1')
        bare = SimpleNamespace(SUB_PUBLIC_URL='', WEBAPP_URL='', BOT_TOKEN='x')
        assert icm.plan(db, ['1'], bare, now=NOW)[0][1] == 'no subscription link'


class TestMessage:

    @pytest.mark.parametrize('lang, store, add', [
        ('ru', '⬇️ Clash Mi в App Store', '📲 Добавить в Clash Mi'),
        ('en', '⬇️ Clash Mi on the App Store', '📲 Add to Clash Mi'),
    ])
    def test_own_link_and_both_buttons(self, db, lang, store, add):
        user = holder(db, '1', lang=lang)
        text, keyboard = icm.build_message(CONFIG, user)
        url = f'{BASE}/sub/{token_of(user)}'
        assert f'<code>{url}?format=clash</code>' in text
        assert nc.CLASH_MI_URL in text and 'Clash Mi' in text
        assert 'Karing' not in text
        assert keyboard == {'inline_keyboard': [
            [{'text': store, 'url': nc.CLASH_MI_URL}],
            [{'text': add, 'url': f'{BASE}/app/{token_of(user)}'}],
        ]}

    def test_each_user_gets_their_own_link(self, db):
        a, b = holder(db, '1'), holder(db, '2')
        text_a, _ = icm.build_message(CONFIG, a)
        text_b, _ = icm.build_message(CONFIG, b)
        assert token_of(a) in text_a and token_of(a) not in text_b


class TestRun:

    def test_dry_run_sends_nothing_and_prints_no_token(self, db):
        a = holder(db, '1')
        holder(db, '2', platform='android')
        asked = []

        def check(url, **kw):
            asked.append((url, kw['headers']['User-Agent']))
            return FakeResp(200, '<a href="clashmi://install-config?url=x">')

        tg = FakeTG()
        code, out = run(db, tg=tg, check=check)
        assert code == 0 and tg.sent == [] and audits(db) == []
        assert '1: send' in out and '2: not iPhone' not in out
        assert '--- send: 1, not iPhone: 1' in out
        assert '--- buttons: ⬇️ Clash Mi в App Store | 📲 Добавить в Clash Mi' in out
        assert "page check, the first one's /app: 200, Clash Mi" in out
        assert 'dry run: 1 would get it' in out
        assert token_of(a) not in out and '<token>' in out
        assert asked[0][0] == f'{icm.LOCAL_BASE}/app/{token_of(a)}'
        assert 'iPhone' in asked[0][1]

    def test_an_old_page_shows_in_the_dry_run(self, db):
        holder(db, '1')
        code, out = run(db, check=lambda url, **kw: FakeResp(200, 'karing://install-config'))
        assert code == 0 and 'no Clash Mi: the bot runs an older page' in out

    def test_send_audits_and_a_second_run_skips(self, db):
        holder(db, '1')
        holder(db, '2')
        tg = FakeTG(fail={'2'})
        code, out = run(db, '--send', tg=tg)
        assert code == 1                                   # one failed
        assert [m['chat_id'] for m in tg.sent] == ['1', '2']
        assert tg.sent[0]['parse_mode'] == 'HTML'
        assert tg.sent[0]['disable_web_page_preview'] is True
        assert audits(db) == [('1', icm.TAG)]
        assert '1: SENT 2001' in out and '2: FAILED' in out
        tg2 = FakeTG()
        code, out = run(db, '--send', tg=tg2)
        assert code == 0 and [m['chat_id'] for m in tg2.sent] == ['2']
        assert audits(db) == [('1', icm.TAG), ('2', icm.TAG)]

    def test_to_sends_to_a_test_chat(self, db):
        holder(db, '7', platform='android')
        tg = FakeTG()
        code, _out = run(db, '--send', '--to', '7', tg=tg)
        assert code == 0 and [m['chat_id'] for m in tg.sent] == ['7']


class TestRunAsAFile:
    """The documented call: `python3 scripts/ios_clash_mi_notify.py`, no
    PYTHONPATH (the release script's first live run failed on `import bot`)."""

    def test_dry_run_from_a_bare_interpreter(self, tmp_path):
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['DB_PATH'] = str(tmp_path / 'bot.db')
        db = Database(env['DB_PATH'])
        holder(db, '1')
        env.update(SUB_PUBLIC_URL='', WEBAPP_URL='')
        r = subprocess.run(
            [sys.executable, os.path.join('scripts', 'ios_clash_mi_notify.py')],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert '1: no subscription link' in r.stdout
        assert 'dry run: 0 would get it' in r.stdout
