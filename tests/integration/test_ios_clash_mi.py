"""iPhone gets Clash Mi and nothing else (AGENTS.md §69, the owner on
2026-11-25: «пусть будет clash mi», «остальные для apple удали»).

Clash Mi reads only the Clash profile (``?format=clash``). Its import link,
``clashmi://install-config?…&overwrite=false``, opens the app's «Добавление
подписки» screen with the app's own patch switched off; Telegram buttons take
only http(s), so the bot's button opens ``/app/<token>``, whose iPhone section
carries that link. Real sqlite where a handler reads the database.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot.config.constants import PLATFORM_INSTRUCTIONS, Platform
from bot.core.database import Database
from bot.handlers.callbacks.user import build_key_delivery_message, clash_mi_text
from bot.handlers.commands import CommandHandler
from bot.models.user import User
from bot.services import neko_client as nc
from bot.services import sos
from bot.services.email_service import _key_email
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
BASE = 'https://sub.example.test:9443'
OTHERS = ('Karing', 'karing', 'Hiddify', 'FlClash', 'Happ', 'v2rayNG', 'Streisand')


def config(**over):
    cfg = dict(SUB_PUBLIC_URL=BASE, WEBAPP_URL='', SUB_TOKEN_SECRET='pinned-secret',
               BOT_TOKEN='bot-token-not-used')
    cfg.update(over)
    return SimpleNamespace(**cfg)


def user(platform='ios', lang='ru', status='demo', uuid=UUID):
    return SimpleNamespace(uuid=uuid, email='u1@x', status=status, lang=lang,
                           chat_id='1', platform=platform, subscription_expiry=None)


def token():
    return SubscriptionService(config()).derive_token(UUID)


def plain():
    return f'{BASE}/sub/{token()}'


def no_other_app(text):
    for name in OTHERS:
        assert name not in text, name


class TestTheLinks:

    def test_the_app_store_page_is_the_russian_one(self):
        assert nc.CLASH_MI_URL == 'https://apps.apple.com/ru/app/clash-mi/id6744321968'

    def test_the_import_link(self):
        link = nc.clash_mi_link(f'{plain()}?format=clash')
        assert link.startswith('clashmi://install-config?url=https%3A%2F%2F')
        assert link.endswith('%3Fformat%3Dclash&name=NekoVPN&overwrite=false')

    @pytest.mark.parametrize('lang, label', [('ru', '📲 Добавить в Clash Mi'),
                                             ('en', '📲 Add to Clash Mi')])
    def test_the_button_opens_the_app_page(self, lang, label):
        assert nc.clash_mi_button_row(config(), user(lang=lang)) == [
            {'text': label, 'url': f'{BASE}/app/{token()}'}]

    @pytest.mark.parametrize('who', [
        user(platform='android'), user(platform='windows'), user(platform=''),
        user(status='banned'), user(status='new'), user(uuid=None), None,
    ])
    def test_no_button_for_anyone_else(self, who):
        assert nc.clash_mi_button_row(config(), who) is None

    def test_no_button_without_a_public_address(self):
        assert nc.clash_mi_button_row(config(SUB_PUBLIC_URL=''), user()) is None


class TestTheCard:

    @pytest.mark.parametrize('lang', ['ru', 'en'])
    def test_the_card_names_clash_mi_with_the_clash_link(self, lang):
        text, kb = build_key_delivery_message(user(lang=lang), config())
        assert 'Clash Mi' in text and nc.CLASH_MI_URL in text
        assert f'<code>{plain()}?format=clash</code>' in text
        assert f'<code>{plain()}</code>' not in text
        assert '/raw' not in text
        no_other_app(text)
        assert kb['inline_keyboard'][0] == nc.clash_mi_button_row(config(), user(lang=lang))
        platform_row = next(row for row in kb['inline_keyboard']
                            if row[0].get('callback_data', '').startswith('setplat:'))
        assert platform_row[0] == {'text': '🍎 iPhone', 'callback_data': 'setplat:ios'}
        assert kb['inline_keyboard'][-1][0]['callback_data'] == 'report_failure'

    def test_the_steps_name_the_button_the_card_has(self):
        text, _ = build_key_delivery_message(user(), config())
        assert '«📲 Добавить в Clash Mi» ниже' in text
        assert 'Профили → + → Импорт из буфера обмена → Сохранить' in text

    def test_without_the_button_the_steps_say_paste(self):
        text = clash_mi_text(plain(), 'ru', with_button=False)
        assert 'Добавить в Clash Mi' not in text
        assert '2. Скопируй ссылку ниже' in text
        assert f'<code>{plain()}?format=clash</code>' in text
        en = clash_mi_text(plain(), 'en', with_button=False)
        assert 'Add to Clash Mi' not in en and '2. Copy the link below' in en

    def test_android_keeps_its_card(self):
        text, kb = build_key_delivery_message(user(platform='android'), config())
        assert 'Clash Mi' not in text and 'Hiddify' in text
        assert all('Clash Mi' not in b['text'] for row in kb['inline_keyboard'] for b in row)


class TestTheSubCommand:

    def _sub(self, tmp_path, platform, lang='ru'):
        db = Database(str(tmp_path / 'bot.db'))
        db._users.save(User(chat_id='1', username='u1', status='demo', uuid=UUID,
                            email='u1@x', lang=lang, platform=platform))
        bot = Mock()
        CommandHandler(bot, db, config()).handle_sub({}, '1')
        return bot.send_message.call_args.kwargs

    @pytest.mark.parametrize('lang', ['ru', 'en'])
    def test_iphone(self, tmp_path, lang):
        kw = self._sub(tmp_path, 'ios', lang)
        text, rows = kw['text'], kw['reply_markup']['inline_keyboard']
        assert 'Clash Mi' in text and f'<code>{plain()}?format=clash</code>' in text
        no_other_app(text)
        assert rows[0] == [{'text': nc.clash_mi_button_label(lang),
                            'url': f'{BASE}/app/{token()}'}]
        assert all('/open/' not in b.get('url', '') for row in rows for b in row)
        assert rows[1][0] == {'text': '🍎 iPhone', 'callback_data': 'setplat:ios'}

    def test_android_unchanged(self, tmp_path):
        kw = self._sub(tmp_path, 'android')
        assert 'Clash Mi' not in kw['text'] and 'Hiddify' in kw['text']


class TestSos:

    URLS = (f'{BASE}/sub/t?emergency=1', f'{BASE}/sub/t?format=clash&emergency=1')

    @pytest.mark.parametrize('lang', ['ru', 'en'])
    def test_the_iphone_answer(self, lang):
        text = sos.sos_text(self.URLS, ['ok'], lang, is_ios=True)
        assert 'Clash Mi' in text
        assert '<code>https://sub.example.test:9443/sub/t?format=clash&amp;emergency=1</code>' in text
        assert '?emergency=1</code>' not in text.replace('format=clash&amp;emergency=1', '')
        no_other_app(text)

    def test_the_nudge(self):
        for lang in ('ru', 'en'):
            text = sos.nudge_text(lang)
            assert 'Clash Mi (iPhone)' in text and 'Karing' not in text

    @pytest.mark.parametrize('lang', ['ru', 'en'])
    def test_the_mail_letter(self, lang):
        _subject, body = sos.mail_sos_letter(self.URLS, ['ok'], lang)
        assert 'Karing' not in body and 'Clash Mi (iPhone)' in body
        # Clash Mi's steps sit right above the Clash link, Hiddify's above the plain one.
        assert body.index('Clash Mi (iPhone)') < body.index(self.URLS[1]) < body.index('Hiddify')
        assert body.index('Hiddify') < body.index(self.URLS[0] + '\n')

    @pytest.mark.parametrize('lang', ['ru', 'en'])
    def test_the_kit(self, lang):
        intro = sos.kit_intro_text(lang)
        assert 'Clash Mi' in intro and 'Karing' not in intro
        captions = sos.kit_captions(lang)
        assert 'Clash Mi' in captions[sos.KIT_YAML_NAME]
        assert 'Clash Mi' not in captions[sos.KIT_JSON_NAME]


class TestTheLetter:

    @pytest.mark.parametrize('lang', ['ru', 'en'])
    def test_the_iphone_letter(self, lang):
        _subject, body = _key_email(plain(), lang, 'ios')
        assert f'{plain()}?format=clash\n' in body
        assert f'{plain()}\n' not in body
        assert nc.CLASH_MI_URL in body
        no_other_app(body)

    @pytest.mark.parametrize('lang', ['ru', 'en'])
    def test_the_general_letter_gives_iphone_its_own_link(self, lang):
        _subject, body = _key_email(plain(), lang, None)
        assert f'{plain()}\n' in body                      # the key, for Hiddify
        assert f'{plain()}?format=clash\n' in body         # Clash Mi's form of it
        assert nc.CLASH_MI_URL in body and 'Karing' not in body


class TestNothingElseForApple:
    """Every text an iPhone user reads names Clash Mi only."""

    def test_platform_instructions(self):
        for lang in ('ru', 'en'):
            text = PLATFORM_INSTRUCTIONS[lang][Platform.IOS]
            assert 'Clash Mi' in text and 'Karing' not in text

    def test_the_app_page_offers_clash_mi_alone_for_iphone(self):
        page = nc.render_download_page(
            platform='ios', lang='ru', release=None, apk_url=f'{BASE}/apk',
            demo_gb=None, plain_url=plain(), profile_url=f'{plain()}?format=clash',
            self_url=f'{BASE}/app/{token()}')
        ios = page.split('<details id="ios" open>')[1].split('</details>')[0]
        assert ios.count('<div class="app">') == 1 and 'Clash Mi' in ios
        assert 'clashmi://install-config' in ios and 'overwrite=false' in ios
        assert nc.CLASH_MI_URL in ios
        assert 'karing' not in page.lower()
