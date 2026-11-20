"""scripts/client_release_notify.py — the bot's message on a NekoVPN client
release: «Что нового» from the release body, the testers, one message each.

Real sqlite (app_settings, users, admin_actions); GitHub and Telegram faked.
"""

import importlib.util
import io
import json
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from bot.core.database import Database
from bot.models.user import User

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_spec = importlib.util.spec_from_file_location(
    'client_release_notify', os.path.join(ROOT, 'scripts', 'client_release_notify.py'))
crn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(crn)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

TAG = 'v0.8.99-neko.2'
APK = ('https://github.com/Annoyt/vpnautodeploy_client/releases/download/'
       f'{TAG}/NekoVPN-0.8.99-neko.2-android-arm64-v8a.apk')
BODY = (
    'Тестовая сборка.\n\nЧто нового:\n\n'
    '<!-- flclash:changelog:begin -->\n'
    '- Обновления только от NekoVPN. Без FlClash.\n'
    '- Окно <b>обновления</b> & ссылки.\n'
    '<!-- flclash:changelog:end -->\n\n'
    'Какой файл ставить:\n- `arm64-v8a` почти всем.\n'
)


def release(body=BODY, assets=None, draft=False):
    if assets is None:
        assets = [
            {'name': 'NekoVPN-0.8.99-neko.2-android-armeabi-v7a.apk',
             'browser_download_url': APK.replace('arm64-v8a', 'armeabi-v7a')},
            {'name': 'NekoVPN-0.8.99-neko.2-android-arm64-v8a.apk',
             'browser_download_url': APK},
            {'name': 'SHA256SUMS', 'browser_download_url': 'x'},
        ]
    return {'tag_name': TAG, 'body': body, 'assets': assets, 'draft': draft,
            'html_url': f'https://github.com/Annoyt/vpnautodeploy_client/releases/tag/{TAG}'}


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def seed(db, chat_id, status='paid'):
    db._users.save(User(chat_id=chat_id, username=f'u{chat_id}', status=status,
                        uuid=f'00000000-0000-0000-0000-{int(chat_id):012d}',
                        email=f'{chat_id}@x'))


class FakeTG:
    def __init__(self, fail=()):
        self.sent = []
        self.fail = set(fail)

    def send_message(self, **kw):
        self.sent.append(kw)
        if kw['chat_id'] in self.fail:
            return None
        return {'message_id': 1000 + len(self.sent)}


def run(db, *argv, tg=None, rel=None, config=None, check=None):
    out = io.StringIO()
    code = crn.main(list(argv), db=db, tg=tg, out=out, config=config, check=check,
                    fetch=lambda tag: rel if rel is not None else release())
    return code, out.getvalue()


def verdicts(planned):
    return [(cid, verdict) for cid, verdict, _ in planned]


def audits(db):
    with db._connect() as conn:
        return conn.execute(
            "SELECT target_id, details FROM admin_actions WHERE action = ? "
            "ORDER BY id", (crn.ACTION,)).fetchall()


class TestRunAsAFile:
    """The documented call: `python3 scripts/client_release_notify.py`,
    no PYTHONPATH. Its first live run failed on `import bot`."""

    def test_set_testers_from_a_bare_interpreter(self, tmp_path):
        import subprocess
        import sys
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['DB_PATH'] = str(tmp_path / 'bot.db')
        r = subprocess.run(
            [sys.executable, os.path.join('scripts', 'client_release_notify.py'),
             '--set-testers', '5,6'],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert 'testers: 5, 6' in r.stdout
        db = Database(env['DB_PATH'])
        assert json.loads(db.get_setting(crn.SETTING_KEY)) == ['5', '6']


class TestWhatsNew:

    def test_items_between_the_markers_only(self):
        assert crn.whats_new(BODY) == [
            'Обновления только от NekoVPN. Без FlClash.',
            'Окно <b>обновления</b> & ссылки.',
        ]

    @pytest.mark.parametrize('body', [
        None, '', '- a bullet\n- but no markers\n',
        'Какой файл ставить, длинное вступление без маркеров:\n- `arm64-v8a` почти всем.\n',
    ])
    def test_no_markers_no_items(self, body):
        # Never the whole body: outside the markers sit install hints.
        assert crn.whats_new(body) == []

    def test_missing_end_marker_reads_to_the_end(self):
        assert crn.whats_new('<!-- flclash:changelog:begin -->\n- one\n') == ['one']

    def test_blank_bullets_are_skipped(self):
        body = '<!-- flclash:changelog:begin -->\n- \n-   \n- real\n<!-- flclash:changelog:end -->'
        assert crn.whats_new(body) == ['real']


class TestMessage:

    def test_text_and_buttons(self):
        text, kb = crn.build_message(release())
        assert text.startswith('🐱 <b>NekoVPN 0.8.99-neko.2 для Android</b>')
        assert '<b>Что нового</b>' in text
        # The items are text, not markup: escaped for parse_mode=HTML.
        assert '• Окно &lt;b&gt;обновления&lt;/b&gt; &amp; ссылки.' in text
        assert 'arm64-v8a почти всем' not in text      # install hint, not news
        assert kb['inline_keyboard'][0] == [{'text': '⬇️ Скачать NekoVPN (Android)', 'url': APK}]
        assert kb['inline_keyboard'][1][0]['url'].endswith(f'/releases/tag/{TAG}')

    def test_no_whats_new_no_message(self):
        with pytest.raises(ValueError, match='Что нового'):
            crn.build_message(release(body='no markers here'))

    def test_no_arm64_apk_no_message(self):
        assets = [{'name': 'NekoVPN-x-android-x86_64.apk', 'browser_download_url': 'u'}]
        with pytest.raises(ValueError, match='arm64'):
            crn.build_message(release(assets=assets))


class TestFetch:

    def _get(self, status, payload=None):
        def get(url, **kw):
            assert url == crn.API.format(tag=TAG)
            return SimpleNamespace(status_code=status, json=lambda: payload,
                                   raise_for_status=lambda: None)
        return get

    def test_published(self):
        assert crn.fetch_release(TAG, get=self._get(200, release()))['tag_name'] == TAG

    def test_draft_is_refused(self):
        with pytest.raises(ValueError, match='not visible'):
            crn.fetch_release(TAG, get=self._get(404))
        with pytest.raises(ValueError, match='draft'):
            crn.fetch_release(TAG, get=self._get(200, release(draft=True)))


class TestRecipients:

    def test_set_testers_is_stored_trimmed_and_unique(self, db):
        code, out = run(db, '--set-testers', ' 1, 2,1 ')
        assert code == 0 and 'testers: 1, 2' in out
        assert json.loads(db.get_setting(crn.SETTING_KEY)) == ['1', '2']

    def test_no_recipients_is_an_error(self, db):
        code, out = run(db, TAG)
        assert code == 1 and 'no recipients' in out

    def test_plan_verdicts(self, db):
        seed(db, '1')
        seed(db, '2', status='banned')
        db.log_admin_action('owner', crn.ACTION, '3', TAG)
        seed(db, '3')
        assert verdicts(crn.plan(db, ['1', '2', '3', '4'], TAG)) == [
            ('1', 'send'), ('2', 'inactive (banned)'),
            ('3', 'sent before'), ('4', 'no such user')]
        assert verdicts(crn.plan(db, ['3'], TAG, force=True)) == [('3', 'send')]

    def test_sent_before_is_per_tag(self, db):
        seed(db, '1')
        db.log_admin_action('owner', crn.ACTION, '1', 'v0.8.99-neko.1')
        assert verdicts(crn.plan(db, ['1'], TAG)) == [('1', 'send')]


class TestSend:

    def test_dry_run_sends_nothing(self, db):
        seed(db, '1')
        tg = FakeTG()
        code, out = run(db, TAG, '--to', '1', tg=tg)
        assert code == 0 and tg.sent == [] and audits(db) == []
        assert '1: send' in out and 'dry run: 1 would get it' in out
        assert '<b>Что нового</b>' in out

    def test_send_delivers_audits_and_is_idempotent(self, db, monkeypatch):
        monkeypatch.setattr(crn, 'SEND_GAP_S', 0)
        seed(db, '1')
        seed(db, '2')
        db.set_setting(crn.SETTING_KEY, json.dumps(['1', '2']))
        tg = FakeTG()
        code, out = run(db, TAG, '--send', tg=tg)
        assert code == 0
        assert [m['chat_id'] for m in tg.sent] == ['1', '2']
        assert tg.sent[0]['parse_mode'] == 'HTML'
        assert tg.sent[0]['reply_markup']['inline_keyboard'][0][0]['url'] == APK
        assert [tuple(r) for r in audits(db)] == [('1', TAG), ('2', TAG)]
        # A second run reaches nobody twice.
        tg2 = FakeTG()
        code, out = run(db, TAG, '--send', tg=tg2)
        assert code == 0 and tg2.sent == [] and out.count('sent before') == 2

    def test_a_failed_send_is_not_audited_and_fails_the_run(self, db, monkeypatch):
        monkeypatch.setattr(crn, 'SEND_GAP_S', 0)
        seed(db, '1')
        seed(db, '2')
        tg = FakeTG(fail={'2'})
        code, out = run(db, TAG, '--to', '1,2', '--send', tg=tg)
        assert code == 1 and '2: FAILED' in out
        assert [tuple(r) for r in audits(db)] == [('1', TAG)]

    def test_a_release_without_whats_new_sends_nothing(self, db):
        seed(db, '1')
        tg = FakeTG()
        code, out = run(db, TAG, '--to', '1', '--send', tg=tg,
                        rel=release(body='no markers'))
        assert code == 1 and tg.sent == [] and 'Что нового' in out


# ---------- --key-holders: everyone with a working key (2026-11-21) ----------

BASE = 'https://sub.example.test:9443'
CONFIG = SimpleNamespace(SUB_PUBLIC_URL=BASE, SUB_TOKEN_SECRET='pinned-secret',
                         BOT_TOKEN='bot-token-not-used')
NOW = datetime(2026, 11, 21, 4, 0)


def holder(db, chat_id, status='demo', uuid=True, **fields):
    db._users.save(User(
        chat_id=chat_id, username=f'u{chat_id}', status=status,
        uuid=f'11111111-0000-0000-0000-{abs(hash(chat_id)) % 10**12:012d}' if uuid else None,
        email=f'{chat_id}@x' if uuid else None, **fields))
    return db._users.get_by_id(chat_id)


def token_of(user):
    from bot.services.subscription import SubscriptionService
    return SubscriptionService(CONFIG).derive_token(user.uuid)


class TestKeyHolders:

    def test_only_key_statuses_with_a_uuid(self, db):
        holder(db, '1', 'demo')
        holder(db, '2', 'paid')
        holder(db, '3', 'support_topic')
        holder(db, '4', 'banned')
        holder(db, '5', 'new', uuid=False)
        holder(db, '6', 'demo', uuid=False)
        assert crn.key_holders(db) == ['1', '2', '3']

    def test_verdicts(self, db):
        holder(db, '10', 'paid', subscription_expiry='2026-12-01T00:00:00')
        holder(db, '11', 'paid', subscription_expiry='2026-11-01T00:00:00')
        holder(db, '12', 'paid', subscription_expiry='2026-11-01T00:00:00+05:00')
        holder(db, '13', 'paid')                       # no date: the admin's call
        holder(db, '14', 'demo', platform='ios')
        holder(db, '15', 'demo', platform='android')
        holder(db, '16', 'demo', platform='windows')
        holder(db, 'ext_ab12', 'paid')
        holder(db, '17', 'demo')
        db.log_admin_action('owner', crn.ACTION, '17', TAG)
        ids = crn.key_holders(db)
        got = verdicts(crn.plan(db, ids, TAG, everyone=True, config=CONFIG, now=NOW))
        assert got == [
            ('10', 'send'),
            ('11', 'lapsed (paid until 2026-11-01)'),
            ('12', 'lapsed (paid until 2026-10-31)'),
            ('13', 'send'),
            ('14', 'iPhone'),
            ('15', 'send'),
            ('16', 'send'),
            ('ext_ab12', 'not a Telegram chat'),
            ('17', 'sent before'),
        ]

    def test_the_testers_keep_their_old_plan(self, db):
        # Without --key-holders nothing new is left out: a tester on iPhone
        # or past the paid date is the tester's own business.
        holder(db, '1', 'paid', subscription_expiry='2026-01-01T00:00:00',
               platform='ios')
        planned = crn.plan(db, ['1'], TAG, now=NOW)
        assert planned == [('1', 'send', [])]

    def test_personal_rows(self, db):
        user = holder(db, '1')
        rows = crn.personal_rows(CONFIG, user)
        token = token_of(user)
        assert rows == [
            [{'text': '📲 Открыть в NekoVPN', 'url': f'{BASE}/open/{token}'}],
            [{'text': '💻 Другие устройства', 'url': f'{BASE}/app/{token}'}],
        ]
        en = holder(db, '2', lang='en')
        labels = [r[0]['text'] for r in crn.personal_rows(CONFIG, en)]
        assert labels == ['📲 Open in NekoVPN', '💻 Other devices']

    def test_no_public_address_no_send(self, db):
        holder(db, '1')
        bare = SimpleNamespace(SUB_PUBLIC_URL='', WEBAPP_URL='', BOT_TOKEN='x')
        assert verdicts(crn.plan(db, ['1'], TAG, everyone=True, config=bare, now=NOW)) == [
            ('1', 'no subscription link')]

    def test_text_tells_a_newcomer_about_the_button(self):
        text, _ = crn.build_message(release(), everyone=True)
        assert 'Ещё нет NekoVPN?' in text and '«📲 Открыть в NekoVPN»' in text
        plain, _ = crn.build_message(release())
        assert 'Ещё нет NekoVPN?' not in plain


class FakeResp:
    def __init__(self, status):
        self.status_code = status


class TestKeyHoldersRun:

    def test_dry_run_counts_and_checks_a_link_without_printing_tokens(self, db):
        a = holder(db, '1')
        holder(db, '2', platform='ios')
        asked = []

        def check(url, **kw):
            asked.append(url)
            assert kw['allow_redirects'] is False
            return FakeResp(200)

        tg = FakeTG()
        code, out = run(db, TAG, '--key-holders', tg=tg, config=CONFIG, check=check)
        assert code == 0 and tg.sent == [] and audits(db) == []
        assert '--- send: 1, iPhone: 1' in out
        assert ("--- buttons: ⬇️ Скачать NekoVPN (Android) | 📲 Открыть в NekoVPN"
                " | 💻 Другие устройства") in out
        assert "link check, the first one's /open: 200" in out
        assert 'dry run: 1 would get it' in out
        assert asked == [f'{crn.LOCAL_BASE}/open/{token_of(a)}']
        assert token_of(a) not in out

    def test_a_link_the_bot_does_not_know_shows_in_the_dry_run(self, db):
        holder(db, '1')
        code, out = run(db, TAG, '--key-holders', config=CONFIG,
                        check=lambda url, **kw: FakeResp(404))
        assert code == 0 and "/open: 404" in out

    def test_an_unreachable_bot_is_reported_not_raised(self, db):
        holder(db, '1')

        def down(url, **kw):
            raise ConnectionError('refused')

        code, out = run(db, TAG, '--key-holders', config=CONFIG, check=down)
        assert code == 0 and '/open: error (ConnectionError)' in out

    def test_send_gives_each_their_own_buttons_once(self, db, monkeypatch):
        monkeypatch.setattr(crn, 'SEND_GAP_S', 0)
        a = holder(db, '1')
        b = holder(db, '2', 'paid', subscription_expiry='2099-01-01T00:00:00')
        holder(db, '3', 'paid', subscription_expiry='2026-01-01T00:00:00')
        tg = FakeTG()
        code, out = run(db, TAG, '--key-holders', '--send', tg=tg, config=CONFIG)
        assert code == 0
        assert [m['chat_id'] for m in tg.sent] == ['1', '2']
        for msg, user in zip(tg.sent, (a, b)):
            kb = msg['reply_markup']['inline_keyboard']
            assert kb[0] == [{'text': '⬇️ Скачать NekoVPN (Android)', 'url': APK}]
            assert kb[1][0]['url'] == f'{BASE}/open/{token_of(user)}'
            assert kb[2][0]['url'] == f'{BASE}/app/{token_of(user)}'
            assert 'Ещё нет NekoVPN?' in msg['text']
        assert [tuple(r) for r in audits(db)] == [('1', TAG), ('2', TAG)]
        tg2 = FakeTG()
        code, out = run(db, TAG, '--key-holders', '--send', tg=tg2, config=CONFIG)
        assert code == 0 and tg2.sent == []
        assert '1: sent before\n2: sent before\n' in out

    def test_a_raising_send_does_not_stop_the_rest(self, db, monkeypatch):
        monkeypatch.setattr(crn, 'SEND_GAP_S', 0)
        holder(db, '1')
        holder(db, '2')

        class Flaky(FakeTG):
            def send_message(self, **kw):
                if kw['chat_id'] == '1':
                    raise ValueError('bad json')
                return super().send_message(**kw)

        tg = Flaky()
        code, out = run(db, TAG, '--key-holders', '--send', tg=tg, config=CONFIG)
        assert code == 1 and '1: FAILED' in out and '2: SENT' in out
        assert [tuple(r) for r in audits(db)] == [('2', TAG)]

    def test_to_and_key_holders_exclude_each_other(self, db):
        code, out = run(db, TAG, '--key-holders', '--to', '1', config=CONFIG)
        assert code == 1 and 'exclude each other' in out
