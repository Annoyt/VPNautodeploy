"""Comprehensive unit tests for bot/handlers/admin/ops.py.

Focus areas:
1. show_status - error handling when services are down
2. show_onlines - empty case, single user, multiple users, geoip failures
3. find_user - edge cases (empty query, short query, no results)
4. show_recent_actions - pagination edge cases
5. set_quota - invalid inputs, x-ui sync failures
6. set_expire - date parsing errors
7. repair_stuck_support - edge cases
"""

import pytest
import sqlite3
from unittest.mock import Mock, MagicMock, patch
from bot.handlers.admin.ops import AdminOpsMixin, _fmt_bytes, _fmt_uptime


# Fixtures
@pytest.fixture
def mock_bot():
    """Mock Telegram bot for testing."""
    bot = Mock()
    bot.send_message = Mock(return_value={'message_id': 123})
    bot.send_message_to_topic = Mock(return_value={'message_id': 456})
    bot.answer_callback_query = Mock(return_value=True)
    bot.forward_message = Mock(return_value={'message_id': 789})
    bot.create_forum_topic = Mock(return_value=999)
    bot.services = {}
    return bot


@pytest.fixture
def mock_db():
    """Mock database for testing."""
    db = Mock()
    db._connect = MagicMock()
    mock_conn = Mock()
    mock_conn.__enter__ = Mock(return_value=mock_conn)
    mock_conn.__exit__ = Mock(return_value=False)
    db._connect.return_value = mock_conn
    db.get_user = Mock()
    db.save_user = Mock()
    db.log_admin_action = Mock()
    db.get_all_users = Mock(return_value=[])
    db.get_stats = Mock(return_value={'by_status': {}})
    return db


@pytest.fixture
def mock_config():
    """Mock config for testing."""
    config = Mock()
    config.SUPER_ADMIN_ID = '1652899'
    config.FORUM_ENABLED = False
    config.FORUM_GROUP_ID = None
    config.OPENCODE_URL = ''
    config.OPENCODE_SERVER_PASSWORD = ''
    config.DB_PATH = '/tmp/test.db'
    return config


class TestFormatHelpers:
    """Test utility functions used by ops handlers."""

    def test_fmt_bytes_gb(self):
        """Test formatting gigabytes."""
        assert _fmt_bytes(2 * 1024 ** 3) == "2.00 GB"
        assert _fmt_bytes(1.5 * 1024 ** 3) == "1.50 GB"
        assert _fmt_bytes(1024 ** 3) == "1.00 GB"

    def test_fmt_bytes_mb(self):
        """Test formatting megabytes."""
        assert _fmt_bytes(2 * 1024 ** 2) == "2.0 MB"
        assert _fmt_bytes(512 * 1024 ** 2) == "512.0 MB"

    def test_fmt_bytes_kb(self):
        """Test formatting kilobytes."""
        assert _fmt_bytes(2048) == "2 KB"
        assert _fmt_bytes(1024) == "1 KB"

    def test_fmt_bytes_small(self):
        """Test formatting small bytes."""
        assert _fmt_bytes(512) == "512 B"
        assert _fmt_bytes(0) == "0 B"
        assert _fmt_bytes(1) == "1 B"

    def test_fmt_uptime_days(self):
        """Test formatting uptime with days."""
        assert _fmt_uptime(86400 * 2 + 3600 * 3 + 60 * 5) == "2d 3h 5m"
        assert _fmt_uptime(86400) == "1d"
        assert _fmt_uptime(86400 + 1) == "1d"

    def test_fmt_uptime_hours(self):
        """Test formatting uptime with hours only."""
        assert _fmt_uptime(3600 * 5 + 60 * 30) == "5h 30m"
        assert _fmt_uptime(3600) == "1h"
        assert _fmt_uptime(3600 + 60) == "1h 1m"

    def test_fmt_uptime_minutes(self):
        """Test formatting uptime with minutes only."""
        assert _fmt_uptime(180) == "3m"
        assert _fmt_uptime(60) == "1m"
        assert _fmt_uptime(0) == "0m"


class TestShowStatus:
    """Tests for show_status - health display with service failure handling."""

    @pytest.fixture
    def handler(self, mock_bot, mock_db, mock_config):
        """Create AdminOpsMixin handler for testing."""
        handler = AdminOpsMixin.__new__(AdminOpsMixin)
        handler.bot = mock_bot
        handler.db = mock_db
        handler.config = mock_config
        handler._get_thread_id = Mock(return_value=None)
        return handler

    def test_show_status_all_services_up(self, handler):
        """Test status display when all services are healthy."""
        mock_stats = {
            'cpu': {'percent': 42.5},
            'ram': {'percent': 65, 'used': 4.2, 'total': 8.0},
            'disk': {'percent': 55, 'used': 120, 'total': 256},
            'uptime': 86400 * 10 + 3600 * 2
        }

        mock_xui = Mock()
        mock_xui.db = Mock()
        handler.bot.services = {'xui': mock_xui}

        handler.db.get_stats = Mock(return_value={
            'by_status': {
                ('demo',): 10,
                ('paid',): 5,
                ('support_topic',): 2,
                ('pending_demo',): 3,
                ('platform_select',): 1,
                ('rejected',): 7,
                ('banned',): 1
            }
        })

        with patch('bot.services.system_stats.SystemStatsService.get_stats', return_value=mock_stats):
            with patch('bot.services.agent_client.AgentClient') as mock_agent_class:
                mock_agent = Mock()
                mock_agent.ping.return_value = {'status': 'ok'}
                mock_agent_class.return_value = mock_agent

                handler.show_status('test_chat', [])

        # Verify message was sent with expected structure
        handler.bot.send_message.assert_called_once()
        args = handler.bot.send_message.call_args
        assert '🩺 <b>Status</b>' in args[1]['text']
        assert '<b>ok</b>' in args[1]['text']
        assert 'CPU <b>42.5%</b>' in args[1]['text']

    def test_show_status_system_stats_exception(self, handler):
        """Test status display handles SystemStatsService failure gracefully."""
        handler.bot.services = {}

        with patch('bot.services.system_stats.SystemStatsService.get_stats', side_effect=RuntimeError("psutil failed")):
            handler.show_status('test_chat', [])

        handler.bot.send_message.assert_called_once()
        text = handler.bot.send_message.call_args[1]['text']
        # Should show error indicator but not crash
        assert '🩺 <b>Status</b>' in text

    def test_show_status_xui_missing(self, handler):
        """Test status display when X-UI service is unavailable."""
        handler.bot.services = {}  # No X-UI service

        with patch('bot.services.system_stats.SystemStatsService.get_stats', return_value={'uptime': 0}):
            handler.show_status('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        assert '<b>missing</b>' in text

    def test_show_status_opencode_down(self, handler):
        """Test status display when the OpenCode server is down."""
        handler.bot.services = {}

        with patch('bot.services.system_stats.SystemStatsService.get_stats', return_value={'uptime': 0}):
            with patch('bot.services.agent_client.AgentUnavailable', Exception):
                with patch('bot.services.agent_client.AgentClient') as mock_agent_class:
                    mock_agent = Mock()
                    mock_agent.ping.side_effect = Exception("Connection refused")
                    mock_agent_class.return_value = mock_agent

                    handler.show_status('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        # Should show some indicator for the OpenCode agent
        assert 'OpenCode' in text

    def test_show_status_db_stats_error(self, handler):
        """Test status display when database stats query fails."""
        handler.bot.services = {}
        handler.db.get_stats = Mock(side_effect=sqlite3.DatabaseError("DB locked"))

        with patch('bot.services.system_stats.SystemStatsService.get_stats', return_value={'uptime': 0}):
            handler.show_status('test_chat', [])

        # Should still send message even if stats failed
        handler.bot.send_message.assert_called_once()

    def test_show_status_empty_stats(self, handler):
        """Test status display when stats return empty/None values."""
        handler.bot.services = {}
        handler.db.get_stats = Mock(return_value={'by_status': {}})

        with patch('bot.services.system_stats.SystemStatsService.get_stats', return_value={'uptime': 0}):
            handler.show_status('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        assert '<b>0</b> total' in text  # Should handle empty counts


class TestShowOnlines:
    """Tests for show_onlines - live connection display."""

    @pytest.fixture
    def handler(self, mock_bot, mock_db, mock_config):
        """Create AdminOpsMixin handler for testing."""
        handler = AdminOpsMixin.__new__(AdminOpsMixin)
        handler.bot = mock_bot
        handler.db = mock_db
        handler.config = mock_config
        handler._get_thread_id = Mock(return_value=None)
        return handler

    def test_show_onlines_empty(self, handler):
        """Test /onlines when no users are connected."""
        handler.bot.services = {}

        with patch('bot.services.xray_log.summarize_activity', return_value={}):
            handler.show_onlines('test_chat', [])

        handler.bot.send_message.assert_called_once()
        text = handler.bot.send_message.call_args[1]['text']
        assert "⚪ Сейчас никто не подключён" in text

    def test_show_onlines_single_user(self, handler):
        """Test /onlines with one connected user."""
        handler.bot.services = {}

        activity = {
            'user@example.com': {
                'ips': ['91.246.101.216'],
                'distinct_ips': 1,
                'active_connections': 5,
                'distinct_destinations': 10
            }
        }

        mock_user = Mock()
        mock_user.username = 'testuser'
        mock_user.chat_id = '123456'
        mock_user.quota_gb = 50
        mock_user.email = 'user@example.com'
        handler.db.get_all_users = Mock(return_value=[mock_user])

        with patch('bot.services.xray_log.summarize_activity', return_value=activity):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
                handler.show_onlines('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        assert "🟢 <b>Сейчас онлайн: 1</b>" in text
        assert '@testuser' in text or 'user_123456' in text

    def test_show_onlines_multiple_users(self, handler):
        """Test /onlines with multiple connected users."""
        handler.bot.services = {}

        activity = {
            'user1@example.com': {
                'ips': ['91.246.101.216'],
                'distinct_ips': 1,
                'active_connections': 5,
                'distinct_destinations': 10
            },
            'user2@example.com': {
                'ips': ['95.55.123.45'],
                'distinct_ips': 1,
                'active_connections': 3,
                'distinct_destinations': 7
            }
        }

        mock_users = [
            Mock(username=f'user{i}', chat_id=str(100000 + i), quota_gb=50, email=f'user{i}@example.com')
            for i in range(1, 3)
        ]
        handler.db.get_all_users = Mock(return_value=mock_users)

        with patch('bot.services.xray_log.summarize_activity', return_value=activity):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={'91.246.101.216': 25.5}):
                handler.show_onlines('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        assert "🟢 <b>Сейчас онлайн: 2</b>" in text

    def test_show_onlines_geoip_lookup(self, handler):
        """Test /onlines with GeoIP lookup."""
        handler.bot.services = {}

        activity = {
            'user@example.com': {
                'ips': ['91.246.101.216', '185.100.50.20'],
                'distinct_ips': 2,
                'active_connections': 5,
                'distinct_destinations': 10
            }
        }

        handler.db.get_all_users = Mock(return_value=[
            Mock(username='testuser', chat_id='123456', quota_gb=50, email='user@example.com')
        ])

        with patch('bot.services.xray_log.summarize_activity', return_value=activity):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
                with patch('bot.services.geoip.lookup') as mock_geo:
                    mock_geo.side_effect = lambda ip: ('RU', '🇷🇺') if '91.246' in ip else ('KZ', '🇰🇿')
                    handler.show_onlines('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        # Should show flags when GeoIP works
        assert '🇷🇺' in text or '91.246' in text

    def test_show_onlines_geoip_failure(self, handler):
        """Test /onlines handles GeoIP module import failure."""
        handler.bot.services = {}

        activity = {'user@example.com': {'ips': ['91.246.101.216'], 'distinct_ips': 1}}

        handler.db.get_all_users = Mock(return_value=[
            Mock(username='testuser', chat_id='123456', quota_gb=50)
        ])

        with patch('bot.services.xray_log.summarize_activity', return_value=activity):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
                with patch('bot.services.geoip.lookup', None):
                    handler.show_onlines('test_chat', [])

        # Should still work without GeoIP, just show IPs
        handler.bot.send_message.assert_called_once()
        text = handler.bot.send_message.call_args[1]['text']
        assert '91.246.101.216' in text

    def test_show_onlines_sharing_detection(self, handler):
        """Test /onlines detects multi-country sharing."""
        handler.bot.services = {}

        activity = {
            'user@example.com': {
                'ips': ['91.246.101.216', '185.100.50.20'],  # Different countries
                'distinct_ips': 2,
                'active_connections': 5,
                'distinct_destinations': 10
            }
        }

        handler.db.get_all_users = Mock(return_value=[
            Mock(username='testuser', chat_id='123456', quota_gb=50)
        ])

        with patch('bot.services.xray_log.summarize_activity', return_value=activity):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
                with patch('bot.services.geoip.lookup') as mock_geo:
                    mock_geo.side_effect = lambda ip: ('RU', '🇷🇺') if '91.246' in ip else ('KZ', '🇰🇿')
                    handler.show_onlines('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        # Should show sharing marker
        assert '🚨' in text

    def test_show_onlines_log_parse_error(self, handler):
        """Test /onlines handles access.log parse errors."""
        handler.bot.services = {}

        with patch('bot.services.xray_log.summarize_activity', side_effect=IOError("Log not found")):
            handler.show_onlines('test_chat', [])

        # Should treat error as empty activity
        handler.bot.send_message.assert_called_once()
        text = handler.bot.send_message.call_args[1]['text']
        assert "никто не подключён" in text

    def test_show_onlines_xui_api_error(self, handler):
        """Test /onlines handles X-UI API errors."""
        handler.bot.services = {}
        handler.db.get_all_users = Mock(return_value=[])

        activity = {'user@example.com': {'ips': ['91.246.101.216'], 'distinct_ips': 1}}

        with patch('bot.services.xray_log.summarize_activity', return_value=activity):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
                handler.show_onlines('test_chat', [])

        # Should still work with partial data
        handler.bot.send_message.assert_called_once()

    def test_show_onlines_text_truncation(self, handler):
        """Test /onlines truncates long text to fit Telegram limits."""
        handler.bot.services = {}

        # Create enough activity to exceed 4096 char limit
        activity = {}
        mock_users = []
        for i in range(50):
            email = f'user{i}@example.com'
            activity[email] = {
                'ips': [f'91.246.{i}.{i+1}'],
                'distinct_ips': 1,
                'active_connections': 1,
                'distinct_destinations': 1
            }
            mock_users.append(
                Mock(username=f'user{i}', chat_id=str(100000 + i), quota_gb=50, email=email)
            )

        handler.db.get_all_users = Mock(return_value=mock_users)

        with patch('bot.services.xray_log.summarize_activity', return_value=activity):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
                handler.show_onlines('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        assert len(text) <= 4096  # Telegram's limit
        assert '…(обрезано)' in text or len(text) < 3900


class TestFindUser:
    """Tests for find_user - user search functionality."""

    @pytest.fixture
    def handler(self, mock_bot, mock_db, mock_config):
        """Create AdminOpsMixin handler for testing."""
        handler = AdminOpsMixin.__new__(AdminOpsMixin)
        handler.bot = mock_bot
        handler.db = mock_db
        handler.config = mock_config
        handler._get_thread_id = Mock(return_value=None)
        return handler

    def test_find_user_empty_query(self, handler):
        """Test /find with no search query."""
        handler.find_user('test_chat', [])

        handler.bot.send_message.assert_called_once()
        text = handler.bot.send_message.call_args[1]['text']
        assert "Формат: /find <текст>" in text

    def test_find_user_short_query(self, handler):
        """Test /find with query shorter than 2 characters."""
        handler.find_user('test_chat', ['a'])

        handler.bot.send_message.assert_called_once()
        text = handler.bot.send_message.call_args[1]['text']
        assert "Минимум 2 символа" in text

    def test_find_user_no_results(self, handler):
        """Test /find returns no results."""
        # Set up fresh mocks for this specific test
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = []
        mock_conn = Mock()
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.find_user('test_chat', ['nonexistent'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "Ничего не найдено" in text

    def test_find_user_by_username(self, handler):
        """Test /find searching by username."""
        handler.db._connect = MagicMock()
        mock_conn = handler.db._connect.return_value
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock()
        mock_conn.execute().fetchall.return_value = [
            ('123456', 'testuser', 'paid', 'user@example.com', 'uuid-123', 50)
        ]

        handler.find_user('test_chat', ['test'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "🔍 <b>Найдено 1" in text
        assert '@testuser' in text

    def test_find_user_by_chat_id(self, handler):
        """Test /find searching by chat_id."""
        handler.db._connect = MagicMock()
        mock_conn = handler.db._connect.return_value
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock()
        mock_conn.execute().fetchall.return_value = [
            ('123456', 'testuser', 'paid', 'user@example.com', 'uuid-123', 50)
        ]

        handler.find_user('test_chat', ['123456'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "123456" in text

    def test_find_user_by_uuid_prefix(self, handler):
        """Test /find searching by UUID prefix."""
        handler.db._connect = MagicMock()
        mock_conn = handler.db._connect.return_value
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock()
        mock_conn.execute().fetchall.return_value = [
            ('123456', 'testuser', 'paid', 'user@example.com', 'uuid-abc-123', 50)
        ]

        handler.find_user('test_chat', ['uuid-abc'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "uuid-abc" in text

    def test_find_user_at_username(self, handler):
        """Test /find with @username format."""
        handler.db._connect = MagicMock()
        mock_conn = handler.db._connect.return_value
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock()
        mock_conn.execute().fetchall.return_value = [
            ('123456', 'testuser', 'paid', 'user@example.com', 'uuid-123', 50)
        ]

        handler.find_user('test_chat', ['@testuser'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "@testuser" in text or "testuser" in text

    def test_find_user_db_error(self, handler):
        """Test /find handles database errors."""
        handler.db._connect = Mock(side_effect=sqlite3.DatabaseError("DB locked"))

        handler.find_user('test_chat', ['test'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "DB error" in text or "ошибка" in text.lower()

    def test_find_user_limit_20_results(self, handler):
        """Test /find limits results to 20."""
        handler.db._connect = MagicMock()
        mock_conn = handler.db._connect.return_value
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock()
        # Return 25 results but query should limit to 20
        mock_conn.execute().fetchall.return_value = [
            (str(i), f'user{i}', 'paid', f'user{i}@ex.com', f'uuid{i}', 50)
            for i in range(25)
        ]

        handler.find_user('test_chat', ['user'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "топ 20" in text

    def test_find_user_null_username_email(self, handler):
        """Test /find handles users with null username/email."""
        handler.db._connect = MagicMock()
        mock_conn = handler.db._connect.return_value
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock()
        mock_conn.execute().fetchall.return_value = [
            ('123456', None, 'demo', None, 'uuid-123', 5)
        ]

        handler.find_user('test_chat', ['123'])

        text = handler.bot.send_message.call_args[1]['text']
        # Should show dashes for missing fields
        assert '—' in text


class TestShowRecentActions:
    """Tests for show_recent_actions - audit log display."""

    @pytest.fixture
    def handler(self, mock_bot, mock_db, mock_config):
        """Create AdminOpsMixin handler for testing."""
        handler = AdminOpsMixin.__new__(AdminOpsMixin)
        handler.bot = mock_bot
        handler.db = mock_db
        handler.config = mock_config
        handler._get_thread_id = Mock(return_value=None)
        return handler

    def test_show_recent_default(self, handler):
        """Test /recent with default count (15)."""
        # Set up fresh mocks for this specific test
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = [
            ('admin_id', 'cmd_quota', '123456', '2026-06-25T10:30:00')
        ]
        # conn.execute(sql, params).fetchall() - execute returns cursor
        mock_cursor.execute = Mock(return_value=mock_cursor)
        mock_conn = Mock()
        mock_conn.execute = mock_cursor.execute
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.show_recent_actions('test_chat', [])

        # Should default to 15
        assert mock_cursor.execute.called
        call_args = mock_cursor.execute.call_args
        assert 'LIMIT ?' in call_args[0][0]
        assert call_args[0][1][0] == 15

    def test_show_recent_custom_count(self, handler):
        """Test /recent with custom count."""
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.execute = Mock(return_value=mock_cursor)
        mock_conn = Mock()
        mock_conn.execute = mock_cursor.execute
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.show_recent_actions('test_chat', ['25'])

        call_args = mock_cursor.execute.call_args
        assert call_args[0][1][0] == 25

    def test_show_recent_max_limit(self, handler):
        """Test /recent enforces max limit of 50."""
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.execute = Mock(return_value=mock_cursor)
        mock_conn = Mock()
        mock_conn.execute = mock_cursor.execute
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.show_recent_actions('test_chat', ['100'])

        call_args = mock_cursor.execute.call_args
        # Should cap at 50
        assert call_args[0][1][0] == 50

    def test_show_recent_min_limit(self, handler):
        """Test /recent enforces min limit of 1."""
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.execute = Mock(return_value=mock_cursor)
        mock_conn = Mock()
        mock_conn.execute = mock_cursor.execute
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.show_recent_actions('test_chat', ['0'])

        call_args = mock_cursor.execute.call_args
        assert call_args[0][1][0] == 1

    def test_show_recent_invalid_number(self, handler):
        """Test /recent handles invalid number gracefully."""
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = []
        mock_conn = Mock()
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.show_recent_actions('test_chat', ['abc'])

        # Should default to 15
        handler.bot.send_message.assert_called_once()

    def test_show_recent_empty_log(self, handler):
        """Test /recent when audit log is empty."""
        handler.db._connect = MagicMock()
        mock_conn = handler.db._connect.return_value
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock()
        mock_conn.execute().fetchall.return_value = []

        handler.show_recent_actions('test_chat', ['5'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "📭 Журнал пуст" in text

    def test_show_recent_db_error(self, handler):
        """Test /recent handles database errors."""
        handler.db._connect = Mock(side_effect=sqlite3.DatabaseError("DB locked"))

        handler.show_recent_actions('test_chat', ['10'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "DB error" in text or "ошибка" in text.lower()

    def test_show_recent_formatting(self, handler):
        """Test /recent formats timestamps correctly."""
        handler.db._connect = MagicMock()
        mock_conn = handler.db._connect.return_value
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock()
        mock_conn.execute().fetchall.return_value = [
            ('admin123', 'cmd_quota', 'user456', '2026-06-25T10:30:45')
        ]

        handler.show_recent_actions('test_chat', ['10'])

        text = handler.bot.send_message.call_args[1]['text']
        # Should format timestamp as MM-DD HH:MM
        assert '06-25' in text
        assert '10:30' in text


class TestSetQuota:
    """Tests for set_quota - quota modification command."""

    @pytest.fixture
    def handler(self, mock_bot, mock_db, mock_config):
        """Create AdminOpsMixin handler for testing."""
        handler = AdminOpsMixin.__new__(AdminOpsMixin)
        handler.bot = mock_bot
        handler.db = mock_db
        handler.config = mock_config
        handler._get_thread_id = Mock(return_value=None)
        handler._resolve_target = Mock()
        return handler

    def test_set_quota_missing_args(self, handler):
        """Test /quota with insufficient arguments."""
        handler.set_quota('test_chat', ['@user'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "Формат: /quota" in text

    def test_set_quota_user_not_found(self, handler):
        """Test /quota when user doesn't exist."""
        handler._resolve_target.return_value = None

        handler.set_quota('test_chat', ['@nonexistent', '50'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "не найден" in text

    def test_set_quota_invalid_number(self, handler):
        """Test /quota with non-numeric quota value."""
        handler._resolve_target.return_value = Mock(username='test')

        handler.set_quota('test_chat', ['@user', 'abc'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "должно быть числом" in text

    def test_set_quota_negative(self, handler):
        """Test /quota with negative value."""
        handler._resolve_target.return_value = Mock(username='test')

        handler.set_quota('test_chat', ['@user', '-10'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "вне диапазона" in text

    def test_set_quota_too_large(self, handler):
        """Test /quota with value exceeding max."""
        handler._resolve_target.return_value = Mock(username='test')

        handler.set_quota('test_chat', ['@user', '100001'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "вне диапазона" in text

    def test_set_quota_user_disappears(self, handler):
        """Test /quota when user vanishes between resolve and save."""
        target = Mock(username='test', chat_id='123')
        handler._resolve_target.return_value = target
        handler.db.get_user = Mock(return_value=None)

        handler.set_quota('test_chat', ['@user', '50'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "исчез между" in text

    def test_set_quota_success(self, handler):
        """Test /quota successful update."""
        target = Mock(username='testuser', chat_id='123', email='test@example.com')
        handler._resolve_target.return_value = target

        mock_user = Mock()
        mock_user.quota_gb = 10
        mock_user.email = 'test@example.com'
        handler.db.get_user = Mock(return_value=mock_user)
        handler.db.save_user = Mock()
        handler.db.log_admin_action = Mock()

        handler.set_quota('test_chat', ['@user', '50'])

        assert mock_user.quota_gb == 50
        handler.db.save_user.assert_called_once_with(mock_user)

    def test_set_quota_xui_sync_success(self, handler):
        """Test /quota successfully syncs to x-ui."""
        target = Mock(username='testuser', chat_id='123', email='test@example.com')
        handler._resolve_target.return_value = target

        mock_user = Mock()
        mock_user.quota_gb = 10
        mock_user.email = 'test@example.com'
        handler.db.get_user = Mock(return_value=mock_user)
        handler.db.save_user = Mock()

        mock_xui = Mock()
        mock_xui.get_client_sync = Mock(return_value={'totalGB': 10737418240, 'inbound_id': 1})
        mock_xui.add_client_sync = Mock()
        handler.bot.services = {'xui': mock_xui}

        handler.set_quota('test_chat', ['@user', '50'])

        # Should have synced to x-ui
        mock_xui.get_client_sync.assert_called_once_with('test@example.com')
        assert mock_xui.add_client_sync.called

    def test_set_quota_xui_sync_failure(self, handler):
        """Test /quota handles x-ui sync failure gracefully."""
        target = Mock(username='testuser', chat_id='123', email='test@example.com')
        handler._resolve_target.return_value = target

        mock_user = Mock()
        mock_user.quota_gb = 10
        mock_user.email = 'test@example.com'
        handler.db.get_user = Mock(return_value=mock_user)
        handler.db.save_user = Mock()

        mock_xui = Mock()
        mock_xui.get_client_sync = Mock(side_effect=Exception("x-ui down"))
        handler.bot.services = {'xui': mock_xui}

        handler.set_quota('test_chat', ['@user', '50'])

        text = handler.bot.send_message.call_args[1]['text']
        # Should show x-ui error but still update DB
        assert "x-ui error" in text.lower()

    def test_set_quota_no_email(self, handler):
        """Test /quota with user who has no email (skip x-ui sync)."""
        target = Mock(username='testuser', chat_id='123')
        handler._resolve_target.return_value = target

        mock_user = Mock()
        mock_user.quota_gb = 10
        mock_user.email = None
        handler.db.get_user = Mock(return_value=mock_user)
        handler.db.save_user = Mock()

        handler.bot.services = {}

        handler.set_quota('test_chat', ['@user', '50'])

        # Should still update DB
        assert mock_user.quota_gb == 50


class TestSetExpire:
    """Tests for set_expire - subscription expiry date setting."""

    @pytest.fixture
    def handler(self, mock_bot, mock_db, mock_config):
        """Create AdminOpsMixin handler for testing."""
        handler = AdminOpsMixin.__new__(AdminOpsMixin)
        handler.bot = mock_bot
        handler.db = mock_db
        handler.config = mock_config
        handler._get_thread_id = Mock(return_value=None)
        handler._resolve_target = Mock()
        return handler

    def test_set_expire_missing_args(self, handler):
        """Test /expire with insufficient arguments."""
        handler.set_expire('test_chat', ['@user'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "Формат: /expire" in text

    def test_set_expire_user_not_found(self, handler):
        """Test /expire when user doesn't exist."""
        handler._resolve_target.return_value = None

        handler.set_expire('test_chat', ['@nonexistent', '2026-12-31'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "не найден" in text

    def test_set_expire_invalid_date_format(self, handler):
        """Test /expire with invalid date format."""
        handler._resolve_target.return_value = Mock()

        handler.set_expire('test_chat', ['@user', '31-12-2026'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "Формат даты" in text or "YYYY-MM-DD" in text

    def test_set_expire_invalid_date(self, handler):
        """Test /expire with nonsensical date."""
        handler._resolve_target.return_value = Mock()

        handler.set_expire('test_chat', ['@user', '2026-13-45'])

        text = handler.bot.send_message.call_args[1]['text']
        # Should catch the ValueError
        assert "Формат даты" in text or "YYYY-MM-DD" in text

    def test_set_expire_user_disappears(self, handler):
        """Test /expire when user vanishes between resolve and save."""
        target = Mock(username='test', chat_id='123')
        handler._resolve_target.return_value = target
        handler.db.get_user = Mock(return_value=None)

        handler.set_expire('test_chat', ['@user', '2026-12-31'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "исчез между" in text

    def test_set_expire_success(self, handler):
        """Test /expire successful update."""
        target = Mock(username='testuser', chat_id='123')
        handler._resolve_target.return_value = target

        mock_user = Mock()
        mock_user.subscription_expiry = '2026-06-01T00:00:00'
        handler.db.get_user = Mock(return_value=mock_user)
        handler.db.save_user = Mock()
        handler.db.log_admin_action = Mock()

        handler.set_expire('test_chat', ['@user', '2026-12-31'])

        # Should parse date and add end-of-day time
        assert '2026-12-31' in mock_user.subscription_expiry
        assert '23:59' in mock_user.subscription_expiry

    def test_set_expire_subscription_sync(self, handler):
        """Test /expire updates subscriptions table."""
        target = Mock(username='testuser', chat_id='123')
        handler._resolve_target.return_value = target

        mock_user = Mock()
        mock_user.subscription_expiry = None
        handler.db.get_user = Mock(return_value=mock_user)
        handler.db.save_user = Mock()

        mock_conn = MagicMock()
        handler.db._connect = Mock(return_value=mock_conn)
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock()
        mock_conn.execute().rowcount = 1

        handler.set_expire('test_chat', ['@user', '2026-12-31'])

        text = handler.bot.send_message.call_args[1]['text']
        assert "subscriptions:" in text

    def test_set_expire_subscription_sync_error(self, handler):
        """Test /expire handles subscriptions sync error."""
        target = Mock(username='testuser', chat_id='123')
        handler._resolve_target.return_value = target

        mock_user = Mock()
        mock_user.subscription_expiry = None
        handler.db.get_user = Mock(return_value=mock_user)
        handler.db.save_user = Mock()

        handler.db._connect = Mock(side_effect=sqlite3.DatabaseError("DB locked"))

        handler.set_expire('test_chat', ['@user', '2026-12-31'])

        text = handler.bot.send_message.call_args[1]['text']
        # Should show error but still update user
        assert "sync error" in text.lower() or "ошибка" in text.lower()

    def test_set_expire_past_date(self, handler):
        """Test /expire accepts past dates (for immediate expiry)."""
        target = Mock(username='testuser', chat_id='123')
        handler._resolve_target.return_value = target

        mock_user = Mock()
        mock_user.subscription_expiry = None
        handler.db.get_user = Mock(return_value=mock_user)
        handler.db.save_user = Mock()

        handler.set_expire('test_chat', ['@user', '2025-01-01'])

        # Should accept past dates
        assert '2025-01-01' in mock_user.subscription_expiry


class TestRepairStuckSupport:
    """Tests for repair_stuck_support - manual stuck user repair."""

    @pytest.fixture
    def handler(self, mock_bot, mock_db, mock_config):
        """Create AdminOpsMixin handler for testing."""
        handler = AdminOpsMixin.__new__(AdminOpsMixin)
        handler.bot = mock_bot
        handler.db = mock_db
        handler.config = mock_config
        handler._get_thread_id = Mock(return_value=None)
        return handler

    def test_repair_stuck_no_notifier_service(self, handler):
        """Test /repair_stuck when NotificationService is unavailable."""
        handler.bot.services = {}

        handler.repair_stuck_support('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        assert "NotificationService" in text

    def test_repair_stuck_success(self, handler):
        """Test /repair_stuck successfully repairs users."""
        mock_notifier = Mock()
        mock_notifier._repair_stuck_support_users_sync = Mock()
        handler.bot.services = {'notifications': mock_notifier}

        # Mock DB queries - return different values on each call
        mock_cursor = Mock()
        mock_cursor.fetchone.side_effect = [[5], [0]]  # 5 stuck before, 0 after
        mock_conn = Mock()
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.repair_stuck_support('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        assert "Восстановлено: <b>5</b>" in text
        assert "До: 5" in text
        assert "после: 0" in text

    def test_repair_stuck_none_before(self, handler):
        """Test /repair_stuck when query returns None."""
        mock_notifier = Mock()
        handler.bot.services = {'notifications': mock_notifier}

        mock_cursor = Mock()
        mock_cursor.fetchone.return_value = None
        mock_conn = Mock()
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.repair_stuck_support('test_chat', [])

        mock_notifier._repair_stuck_support_users_sync.assert_called_once()

    def test_repair_stuck_exception(self, handler):
        """Test /repair_stuck handles exceptions."""
        mock_notifier = Mock()
        mock_notifier._repair_stuck_support_users_sync = Mock(side_effect=Exception("Repair failed"))
        handler.bot.services = {'notifications': mock_notifier}

        handler.repair_stuck_support('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        assert "Repair failed" in text or "❌" in text

    def test_repair_stuck_db_error(self, handler):
        """Test /repair_stuck handles database errors gracefully."""
        mock_notifier = Mock()
        mock_notifier._repair_stuck_support_users_sync = Mock()
        handler.bot.services = {'notifications': mock_notifier}

        # DB error happens on first query (before repair), so repair is never called
        mock_cursor = Mock()
        mock_cursor.fetchone.side_effect = sqlite3.DatabaseError("DB locked")
        mock_conn = Mock()
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.repair_stuck_support('test_chat', [])

        # Due to DB error, repair is not called and error message is sent
        mock_notifier._repair_stuck_support_users_sync.assert_not_called()

        text = handler.bot.send_message.call_args[1]['text']
        assert "DB locked" in text or "❌" in text

    def test_repair_stuck_no_users_to_fix(self, handler):
        """Test /repair_stuck when there are no stuck users."""
        mock_notifier = Mock()
        handler.bot.services = {'notifications': mock_notifier}

        mock_cursor = Mock()
        mock_cursor.fetchone.side_effect = [[0], [0]]
        mock_conn = Mock()
        mock_conn.execute.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        handler.db._connect = Mock(return_value=mock_conn)

        handler.repair_stuck_support('test_chat', [])

        text = handler.bot.send_message.call_args[1]['text']
        assert "Восстановлено: <b>0</b>" in text
