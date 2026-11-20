"""``t.me/<bot>?start=app`` (docs/NEKO6_PLAN.md D3): where the NekoVPN app's
«Нужна подписка» window sends people.

A key holder (demo / paid / support_topic with a uuid) gets one message
whose button is the key card's «📲 Открыть в NekoVPN», ``/open/<token>``
(AGENTS.md §38): from that page the app adds the subscription itself.
Anyone else gets exactly what a plain /start gives them. Real sqlite;
Telegram is a Mock.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot.core.database import Database
from bot.handlers.commands import CommandHandler
from bot.models.user import User
from bot.services import neko_client
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

CID = '52291265'
UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
USERS_BASE = 'https://radar.example.com:8443'      # SUB_PUBLIC_URL, §60


def make_config(**over):
    cfg = dict(BOT_TOKEN='start-app-bot-token', WEBAPP_URL='https://dash.example.com',
               SUB_PUBLIC_URL=USERS_BASE, FORUM_ENABLED=False, SUPER_ADMIN_ID='1',
               MAX_REJECT_RETRIES=10)
    cfg.update(over)
    ns = SimpleNamespace(**cfg)
    ns.is_admin = lambda user_id: str(user_id) == '1'
    return ns


def seed(db, status='paid', uuid=UUID, lang='ru', platform='android'):
    db._users.save(User(chat_id=CID, username='ziriki', status=status, uuid=uuid,
                        email=f'user_ziriki_{CID}@nekovo.ru' if uuid else None,
                        lang=lang, platform=platform))


def start(db, config, text):
    """The messages /start sent, as the kwargs of each send_message."""
    bot = Mock()
    bot.send_message.return_value = {'message_id': 1}
    update = {'message': {'text': text, 'chat': {'id': int(CID), 'type': 'private'},
                          'from': {'id': int(CID), 'username': 'ziriki'}}}
    CommandHandler(bot, db, config).handle_start(update, CID)
    return [c.kwargs for c in bot.send_message.call_args_list]


def open_url():
    token = SubscriptionService(make_config()).derive_token(UUID)
    return f'{USERS_BASE}/open/{token}'


class TestKeyHolder:

    @pytest.mark.parametrize('status', ['demo', 'paid', 'support_topic'])
    def test_gets_the_open_button(self, tmp_path, status):
        db = Database(str(tmp_path / 'bot.db'))
        seed(db, status=status)
        [sent] = start(db, make_config(), '/start app')
        assert sent['chat_id'] == CID
        assert sent['text'].startswith('📲 Ваша подписка готова для NekoVPN.')
        assert sent['reply_markup'] == {'inline_keyboard': [[
            {'text': '📲 Открыть в NekoVPN', 'url': open_url()},
        ]]}

    def test_the_url_is_the_key_cards(self, tmp_path):
        # the same link the key card's and /sub's button opens
        db = Database(str(tmp_path / 'bot.db'))
        seed(db)
        user = db._users.get_by_id(CID)
        [sent] = start(db, make_config(), '/start app')
        [button] = sent['reply_markup']['inline_keyboard'][0]
        assert button == neko_client.open_button_row(db, make_config(), user)[0]
        assert button['url'] == neko_client.open_page_url(make_config(), user)

    def test_in_english(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        seed(db, lang='en')
        [sent] = start(db, make_config(), '/start app')
        assert sent['text'].startswith('📲 Your subscription is ready for NekoVPN.')
        assert sent['reply_markup']['inline_keyboard'][0][0] == {
            'text': '📲 Open in NekoVPN', 'url': open_url()}

    @pytest.mark.parametrize('platform', ['ios', 'windows', None])
    def test_on_any_platform(self, tmp_path, platform):
        # the link comes from the app itself, whatever platform the bot
        # last heard of
        db = Database(str(tmp_path / 'bot.db'))
        seed(db, platform=platform)
        [sent] = start(db, make_config(), '/start app')
        assert sent['reply_markup']['inline_keyboard'][0][0]['url'] == open_url()

    def test_the_word_in_any_case(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        seed(db)
        assert start(db, make_config(), '/start APP') == start(db, make_config(), '/start app')


def _same_as_plain_start(tmp_path, text, config=None, **user):
    """/start <text> and /start on two databases seeded alike."""
    config = config or make_config()
    a, b = Database(str(tmp_path / 'a.db')), Database(str(tmp_path / 'b.db'))
    if user:
        seed(a, **user)
        seed(b, **user)
    got, plain = start(a, config, text), start(b, config, '/start')
    assert got == plain
    assert got, 'plain /start sent nothing'
    return got


class TestEveryoneElse:

    def test_a_new_user_gets_the_welcome(self, tmp_path):
        got = _same_as_plain_start(tmp_path, '/start app')
        # the welcome with «Запросить демо»: the path of a new user
        buttons = [b['callback_data'] for row in got[0]['reply_markup']['inline_keyboard']
                   for b in row]
        assert 'request_demo' in buttons

    @pytest.mark.parametrize('status,uuid', [
        ('new', None), ('pending_demo', None), ('rejected', None),
        ('platform_select', None), ('banned', UUID),
        ('demo', None), ('paid', None), ('support_topic', None),
    ])
    def test_without_an_active_key(self, tmp_path, status, uuid):
        _same_as_plain_start(tmp_path, '/start app', status=status, uuid=uuid)

    def test_without_a_public_address(self, tmp_path):
        config = make_config(SUB_PUBLIC_URL='', WEBAPP_URL='')
        _same_as_plain_start(tmp_path, '/start app', config=config, status='paid', uuid=UUID)

    @pytest.mark.parametrize('text', ['/start apps', '/start app2', '/start'])
    def test_other_words(self, tmp_path, text):
        _same_as_plain_start(tmp_path, text, status='paid', uuid=UUID)
