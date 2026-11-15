"""A user added by email is shown by its address, not as ``user_ext_…``.

Such a user (/addmail, a mail request) has no Telegram username, a synthetic
chat_id (``ext_<crc32>``) and a synthetic panel email
(``user_ext_<crc32>@nekovo.ru``). Until 2026-11-15 every admin surface but
/users, /find and the dashboard's user list fell back to ``user_<chat_id>``,
so the owner saw ``user_ext_1a2b3c4d`` and could not tell who it was.
``bot/utils/user_label.py`` (and ``userLabel`` in app.js) is the one rule now:
@username, else ✉️ <contact email>, else the surface's old fallback.

Real sqlite wherever a surface reads the database.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.handlers.admin import AdminHandler
from bot.models.user import User
from bot.services import sos
from bot.utils.user_label import user_label

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

EXT = 'ext_1a2b3c4d'
EXT_PANEL = 'user_ext_1a2b3c4d@nekovo.ru'
EXT_MAIL = 'ivan@example.com'
EXT_LABEL = f'✉️ {EXT_MAIL}'


def ext_user(**kw):
    values = dict(
        chat_id=EXT, username=None, email=EXT_PANEL,
        uuid='11111111-2222-4333-8444-555555555555', status='paid',
        contact_email=EXT_MAIL, quota_gb=100.0, limit_ip=5,
        last_country='RU', last_asn='AS31133', last_city='Moscow',
        last_lat=55.75, last_lon=37.62,
    )
    values.update(kw)
    return User(**values)


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / 'bot.db'))
    db.save_user(ext_user())
    db.save_user(User(chat_id='6000001', username='alice', status='demo',
                      email='user_alice_6000001@nekovo.ru',
                      uuid='22222222-2222-4333-8444-555555555555',
                      last_lat=59.9, last_lon=30.3))
    db.save_user(User(chat_id='6000002', username=None, status='demo',
                      email='user_6000002@nekovo.ru',
                      uuid='33333333-2222-4333-8444-555555555555'))
    return db


class TestTheRule:

    def test_the_username_comes_first(self):
        user = User(chat_id='1', username='alice', contact_email='a@example.com')
        assert user_label(user) == '@alice'

    def test_without_a_username_the_contact_address(self):
        assert user_label(ext_user()) == EXT_LABEL

    def test_without_either_the_surfaces_fallback(self):
        user = User(chat_id='6000002', username=None)
        assert user_label(user) == 'user_6000002'
        assert user_label(user, fallback='no_username') == 'no_username'
        assert user_label(ext_user(contact_email=None)) == f'user_{EXT}'

    def test_a_blank_address_counts_as_none(self):
        assert user_label(ext_user(contact_email='   ')) == f'user_{EXT}'
        assert user_label(ext_user(contact_email=f'  {EXT_MAIL} ')) == EXT_LABEL

    def test_html_is_escaped_unless_asked_for_plain_text(self):
        odd = 'a<b>&c@example.com'
        assert user_label(username=None, chat_id='x', contact_email=odd) == \
            '✉️ a&lt;b&gt;&amp;c@example.com'
        assert user_label(username=None, chat_id='x', contact_email=odd,
                          html=False) == f'✉️ {odd}'

    def test_the_three_fields_without_a_user(self):
        assert user_label(username=None, chat_id=EXT,
                          contact_email=EXT_MAIL) == EXT_LABEL
        assert user_label(username='bob', chat_id='7') == '@bob'

    def test_a_contact_address_that_is_not_text_is_ignored(self):
        # Unit tests pass Mock users: an unset attribute is a Mock, not an
        # address, and must not end up in a message.
        user = SimpleNamespace(username=None, chat_id='7', contact_email=Mock())
        assert user_label(user) == 'user_7'


class TestBotMessages:

    @pytest.fixture
    def admin(self, db):
        bot = Mock()
        bot.services = {}
        return AdminHandler(bot, db, Mock())

    def _text(self, admin):
        return admin.bot.send_message.call_args[1]['text']

    def test_onlines_names_the_email_user_by_its_address(self, admin):
        activity = {
            EXT_PANEL: {'ips': ['91.246.101.216'], 'distinct_ips': 1},
            'user_alice_6000001@nekovo.ru': {'ips': ['95.55.1.2'], 'distinct_ips': 1},
            'user_6000002@nekovo.ru': {'ips': ['95.55.1.3'], 'distinct_ips': 1},
            'stranger@nekovo.ru': {'ips': ['95.55.1.4'], 'distinct_ips': 1},
        }
        with patch('bot.services.xray_log.summarize_activity',
                   return_value=activity), \
                patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
            admin.show_onlines('chat', [])
        text = self._text(admin)
        assert f'• {EXT_LABEL}' in text
        assert 'user_ext_' not in text
        # everyone else as before
        assert '• @alice' in text
        assert '• user_6000002' in text
        assert '• stranger@nekovo.ru' in text

    def test_user_card_names_the_email_user_by_its_address(self, admin):
        admin.show_user('chat', [EXT])
        text = self._text(admin)
        assert f'👤 <b>Пользователь {EXT_LABEL}</b>' in text

    def test_a_confirmation_without_a_parse_mode_is_plain_text(self, admin, db):
        # An address the validators would refuse, written straight into the
        # table: HTML escaping would show as &amp; in a plain-text message.
        db.save_user(ext_user(chat_id='ext_0000000a', email='user_ext_0000000a@nekovo.ru',
                              uuid='66666666-2222-4333-8444-555555555555',
                              contact_email='o&k@example.com'))
        admin.ban_user('chat', ['ext_0000000a'])
        text = self._text(admin)
        assert text == '🚫 ✉️ o&k@example.com забанен.'
        assert admin.bot.send_message.call_args[1].get('parse_mode') is None

    def test_sos_report_and_agent_name_the_email_user(self, db):
        user = db.get_user(EXT)
        service = sos.SosService(Mock(), db, Mock())
        lines = service._report_lines(user, 7, 'письмо', [], ['ws'], None)
        assert f'User: <code>{EXT_LABEL}</code> ({EXT}) · paid' in lines
        prompt = sos.agent_prompt(user, 7, 'письмо', [], ['ws'])
        assert f'«{EXT_LABEL}», chat_id {EXT}' in prompt

    def test_the_sos_alert_for_the_agent_names_the_email_user(self, db):
        agent = Mock()
        with patch('bot.services.agent_factory.get_agent_url',
                   return_value='http://agent'), \
                patch.object(sos, '_agent_for', return_value=agent):
            state, _ = sos.SosService(Mock(), db, Mock())._kick_agent(
                db.get_user(EXT), 7, 'письмо', [], ['ws'])
        assert state == 'started'
        assert agent.gate.call_args[0][0].title == f'SOS от {EXT_LABEL}'

    def test_sos_report_keeps_the_old_fallback_without_an_address(self, db):
        user = db.get_user('6000002')
        lines = sos.SosService(Mock(), db, Mock())._report_lines(
            user, 7, '/sos', [], ['ws'], None)
        assert 'User: <code>@6000002</code> (6000002) · demo' in lines


class TestDashboardData:

    @pytest.fixture
    def server(self, db):
        config = Mock()
        config.BOT_TOKEN = 'test_token'
        xui = Mock()
        xui.get_client_traffic = AsyncMock(return_value=None)
        xui.get_all_traffic = Mock(return_value={})
        xui.get_inbound_settings = AsyncMock(return_value={'clients': [
            {'id': '11111111-2222-4333-8444-555555555555', 'email': EXT_PANEL,
             'enable': True},
        ]})
        srv = WebAppServer(config, db, xui_service=xui)
        srv.notification_service = Mock()
        with patch.object(srv, '_validate_admin', return_value=True), \
                patch.object(srv, '_validate_admin_with_rate_limit',
                             return_value=({'id': 1}, None)):
            yield srv

    @staticmethod
    def _request(query=None, match=None, body=None):
        request = Mock()
        request.query = query or {}
        request.match_info = match or {}

        async def _json():
            return body or {}
        request.json = _json
        return request

    @staticmethod
    def _body(resp):
        assert resp.status == 200, resp.body
        return json.loads(resp.body)

    def test_only_text_goes_into_a_payload(self):
        # A Mock user's unset attribute is a Mock: in the JSON it would fail
        # the whole answer with a 500.
        from bot.core.web_server import _contact_email
        assert _contact_email(SimpleNamespace(contact_email=Mock())) is None
        assert _contact_email(SimpleNamespace()) is None
        assert _contact_email(ext_user()) == EXT_MAIL

    @pytest.mark.asyncio
    async def test_the_detail_card_carries_the_address(self, server):
        d = self._body(await server.handle_admin_user_detail(
            self._request(match={'chat_id': EXT})))
        assert d['contact_email'] == EXT_MAIL
        assert d['email'] == EXT_PANEL
        d = self._body(await server.handle_admin_user_detail(
            self._request(match={'chat_id': '6000001'})))
        assert d['contact_email'] is None

    @pytest.mark.asyncio
    async def test_failure_reports_and_the_map_carry_the_address(self, server, db):
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO user_failure_reports (chat_id, country, asn, lat, lon) "
                "VALUES (?, 'RU', 'AS31133', 55.75, 37.62)", (EXT,))
            conn.commit()
        rows = self._body(await server.handle_admin_failure_reports(
            self._request()))['rows']
        assert [r['contact_email'] for r in rows] == [EXT_MAIL]
        geo = self._body(await server.handle_admin_geo_points(self._request()))
        assert [r['contact_email'] for r in geo['reports']] == [EXT_MAIL]
        by_chat = {u['chat_id']: u['contact_email'] for u in geo['users']}
        assert by_chat == {EXT: EXT_MAIL, '6000001': None}

    @pytest.mark.asyncio
    async def test_subscriptions_carry_the_address(self, server, db):
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO subscriptions (chat_id, plan_type, started_at, "
                "expires_at, is_active) VALUES (?, 'paid', '2026-11-01', "
                "'2099-01-01', 1)", (EXT,))
            conn.commit()
        d = self._body(await server.handle_admin_subscriptions(self._request()))
        active = [r for r in d['active'] if r['chat_id'] == EXT]
        assert [r['contact_email'] for r in active] == [EXT_MAIL]
        no_sub = {r['chat_id']: r['contact_email'] for r in d['no_subscription']}
        assert no_sub == {'6000001': None, '6000002': None}

    @pytest.mark.asyncio
    async def test_the_panel_comparison_carries_the_address(self, server, db):
        db.save_user(ext_user(chat_id='ext_00000002', email='user_ext_00000002@nekovo.ru',
                              uuid='44444444-2222-4333-8444-555555555555',
                              contact_email='olga@example.com'))
        d = self._body(await server.handle_admin_xui_clients(self._request()))
        synced = {r['email']: r['bot_user']['contact_email'] for r in d['synced']}
        assert synced == {EXT_PANEL: EXT_MAIL}
        orphans = {r['chat_id']: r['contact_email'] for r in d['orphan_in_bot']}
        assert orphans['ext_00000002'] == 'olga@example.com'

    @pytest.mark.asyncio
    async def test_the_broadcast_preview_names_users_the_same_way(self, server):
        d = self._body(await server.handle_broadcast(self._request(
            body={'text': 'hi', 'confirm': False, 'audience': 'active'})))
        assert set(d['sample']) == {EXT_LABEL, '@alice', 'user_6000002'}

    @pytest.mark.asyncio
    async def test_an_action_names_the_user_in_its_answer_and_push(self, server):
        server._broadcast_ws = AsyncMock()
        d = self._body(await server.handle_user_action(self._request(
            match={'chat_id': EXT}, body={'action': 'set_quota', 'value': 50})))
        assert d['user']['contact_email'] == EXT_MAIL
        kind, payload = server._broadcast_ws.call_args[0]
        assert kind == 'user_update' and payload['contact_email'] == EXT_MAIL

    @pytest.mark.asyncio
    async def test_the_users_list_finds_the_email_user_by_its_address(self, server):
        d = self._body(await server.handle_admin_users(
            self._request(query={'search': 'IVAN@'})))
        assert [u['chat_id'] for u in d['users']] == [EXT]
