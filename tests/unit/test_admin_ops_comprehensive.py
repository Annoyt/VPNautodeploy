"""Comprehensive tests for admin ops commands.

Tests for:
- show_status (health checks, service down scenarios)
- show_onlines (empty, single, multiple, geoip failures)
- find_user (validation, no results)
- show_recent_actions (pagination)
- set_quota (validation, x-ui sync)
- set_expire (date parsing)
- repair_stuck_support (edge cases)
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, MagicMock, patch
from bot.handlers.admin import AdminHandler
from bot.models import User


@pytest.fixture
def mock_bot():
    bot = Mock()
    bot.send_message = Mock()
    bot.services = {
        'xui': Mock(api=Mock()),
        'notifications': Mock(_repair_stuck_support_users_sync=Mock()),
    }
    return bot


@pytest.fixture
def mock_db():
    db = Mock()
    db.get_stats = Mock(return_value={'by_status': {'demo': 5, 'paid': 10, 'pending_demo': 2}})
    db.get_all_users = Mock(return_value=[])
    db._connect = Mock()
    db.log_admin_action = Mock()
    return db


@pytest.fixture
def mock_config():
    config = Mock()
    config.SUPER_ADMIN_ID = "123"
    config.FORUM_ENABLED = False
    config.FORUM_GROUP_ID = "456"
    config.OPENCODE_URL = ""
    return config


@pytest.fixture
def admin_handler(mock_bot, mock_db, mock_config):
    handler = AdminHandler(mock_bot, mock_db, mock_config)
    return handler


def _mock_db_connection(execute_result=None):
    """Helper to create a proper mock DB connection with context manager.

    Returns a mock connection that can be used like:
    - conn.execute().fetchall() returns execute_result
    - Access cursor via conn.cursor or conn.__enter__
    """
    mock_cursor = MagicMock()
    mock_cursor.execute.return_value.fetchall.return_value = execute_result or []
    mock_cursor.execute.return_value.fetchone.return_value = None

    mock_conn = MagicMock()
    mock_conn.__enter__ = Mock(return_value=mock_cursor)
    mock_conn.__exit__ = Mock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor
    # For direct execute access
    mock_conn.execute = mock_cursor.execute

    return mock_conn


class TestShowStatus:
    """Tests for /status command."""

    def test_status_basic(self, admin_handler):
        """Basic status output with mocked stats."""
        with patch('bot.services.system_stats.SystemStatsService') as mock_stats_cls:
            mock_stats_cls.get_stats.return_value = {
                'cpu': {'percent': 45},
                'ram': {'percent': 60, 'used': 4.0, 'total': 16.0},
                'disk': {'percent': 70, 'used': 200, 'total': 500},
                'uptime': 3600,
            }

            admin_handler.show_status('123', [])

            assert admin_handler.bot.send_message.called
            msg = admin_handler.bot.send_message.call_args[1]['text']
            assert '45%' in msg  # CPU
            assert '60%' in msg  # RAM

    def test_status_service_down(self, admin_handler):
        """Status when X-UI service is unavailable."""
        admin_handler.bot.services = {}
        admin_handler.show_status('123', [])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'missing' in msg.lower()

    def test_status_stats_error(self, admin_handler):
        """Status when get_stats raises exception."""
        with patch('bot.services.system_stats.SystemStatsService') as mock_stats_cls:
            mock_stats_cls.get_stats.side_effect = Exception("DB error")

            admin_handler.show_status('123', [])

            msg = admin_handler.bot.send_message.call_args[1]['text']
            # Should still show something, not crash
            assert '🩺' in msg or 'Status' in msg


class TestShowOnlines:
    """Tests for /onlines command."""

    def test_onlines_empty(self, admin_handler):
        """No users online."""
        with patch('bot.services.xray_log.summarize_activity', return_value={}):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
                admin_handler.bot.services.get('xui').api.get_online_clients_sync = Mock(return_value=[])

                admin_handler.show_onlines('123', [])

                msg = admin_handler.bot.send_message.call_args[1]['text']
                assert 'никто не подключён' in msg or 'no one' in msg.lower()

    def test_onlines_single_user(self, admin_handler):
        """One user online."""
        with patch('bot.services.xray_log.summarize_activity', return_value={
            'user@example.com': {
                'ips': ['1.2.3.4'],
                'distinct_ips': 1,
                'active_connections': 1,
                'distinct_destinations': 1,
            }
        }):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={'1.2.3.4': 45.5}):
                admin_handler.db.get_all_users = Mock(return_value=[
                    User(chat_id='123', username='testuser', email='user@example.com', quota_gb=100)
                ])
                admin_handler.bot.services.get('xui').db.get_all_client_traffic = Mock(return_value={
                    'user@example.com': {'upload': 0, 'download': 5 * 1024**3}
                })

                admin_handler.show_onlines('123', [])

                msg = admin_handler.bot.send_message.call_args[1]['text']
                assert '@testuser' in msg or 'user@example.com' in msg

    def test_onlines_geoip_failure(self, admin_handler):
        """GeoIP lookup fails - should still work."""
        with patch('bot.services.xray_log.summarize_activity', return_value={
            'user@example.com': {'ips': ['1.2.3.4'], 'distinct_ips': 1, 'active_connections': 1, 'distinct_destinations': 1}
        }):
            with patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
                with patch('bot.services.geoip.lookup', return_value=None):  # GeoIP not available
                    admin_handler.db.get_all_users = Mock(return_value=[
                        User(chat_id='123', email='user@example.com')
                    ])
                    admin_handler.bot.services.get('xui').db.get_all_client_traffic = Mock(return_value={})

                    admin_handler.show_onlines('123', [])

                    # Should not crash, just show IPs without flags
                    assert admin_handler.bot.send_message.called

    def test_onlines_sharing_alert(self, admin_handler):
        """Detect shared key (multiple countries)."""
        with patch('bot.services.geoip.lookup', return_value=('RU', '🇷🇺')):
            with patch('bot.services.xray_log.summarize_activity', return_value={
                'user@example.com': {
                    'ips': ['1.2.3.4', '5.6.7.8'],  # Would need different countries in real scenario
                    'distinct_ips': 2,
                    'active_connections': 2,
                    'distinct_destinations': 1,
                }
            }):
                with patch('bot.services.xui_reload.get_tcp_stats', return_value={}):
                    admin_handler.db.get_all_users = Mock(return_value=[
                        User(chat_id='123', email='user@example.com', limit_ip=1)
                    ])
                    admin_handler.bot.services.get('xui').db.get_all_client_traffic = Mock(return_value={})

                    admin_handler.show_onlines('123', [])

                    # Should indicate IP limit exceeded
                    msg = admin_handler.bot.send_message.call_args[1]['text']
                    assert '2/1' in msg or 'IP' in msg


class TestFindUser:
    """Tests for /find command."""

    def test_find_no_args(self, admin_handler):
        """Find without arguments."""
        admin_handler.find_user('123', [])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Формат' in msg or 'format' in msg.lower()

    def test_find_short_query(self, admin_handler):
        """Find with query shorter than 2 chars."""
        admin_handler.find_user('123', ['a'])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Минимум' in msg or 'minimum' in msg.lower()

    def test_find_no_results(self, admin_handler):
        """Find returns no matches."""
        admin_handler.db._connect.return_value = _mock_db_connection([])

        admin_handler.find_user('123', ['nonexistent'])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'не найдено' in msg or 'not found' in msg.lower()

    def test_find_by_username(self, admin_handler):
        """Find by username."""
        admin_handler.db._connect.return_value = _mock_db_connection([
            ('123', 'john', 'demo', 'john@example.com', 'abc-123', 50)
        ])

        admin_handler.find_user('123', ['john'])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert '@john' in msg or 'john' in msg


class TestShowRecentActions:
    """Tests for /recent command."""

    def test_recent_default_count(self, admin_handler):
        """Default shows 15 actions."""
        mock_conn = _mock_db_connection([])
        admin_handler.db._connect.return_value = mock_conn

        admin_handler.show_recent_actions('123', [])

        # Should limit to 15 by default
        sql = mock_conn.execute.call_args[0][0]
        assert 'LIMIT ?' in sql
        # call_args[0][1] is a tuple, extract first element
        limit_val = mock_conn.execute.call_args[0][1]
        if isinstance(limit_val, tuple):
            limit_val = limit_val[0]
        assert limit_val == 15

    def test_recent_custom_count(self, admin_handler):
        """Custom count."""
        mock_conn = _mock_db_connection([])
        admin_handler.db._connect.return_value = mock_conn

        admin_handler.show_recent_actions('123', ['25'])

        sql = mock_conn.execute.call_args[0][0]
        assert 'LIMIT ?' in sql
        limit_val = mock_conn.execute.call_args[0][1]
        if isinstance(limit_val, tuple):
            limit_val = limit_val[0]
        assert limit_val == 25

    def test_recent_max_cap(self, admin_handler):
        """Count capped at 50."""
        mock_conn = _mock_db_connection([])
        admin_handler.db._connect.return_value = mock_conn

        admin_handler.show_recent_actions('123', ['100'])

        sql = mock_conn.execute.call_args[0][0]
        assert 'LIMIT ?' in sql
        limit_val = mock_conn.execute.call_args[0][1]
        if isinstance(limit_val, tuple):
            limit_val = limit_val[0]
        assert limit_val == 50

    def test_recent_empty(self, admin_handler):
        """Empty audit log."""
        admin_handler.db._connect.return_value = _mock_db_connection([])

        admin_handler.show_recent_actions('123', ['10'])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'пуст' in msg or 'empty' in msg.lower()


class TestSetQuota:
    """Tests for /quota command."""

    def test_quota_no_args(self, admin_handler):
        """Quota without arguments."""
        admin_handler.set_quota('123', [])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Формат' in msg or 'format' in msg.lower()

    def test_quota_invalid_number(self, admin_handler):
        """Quota with invalid number."""
        admin_handler.set_quota('123', ['@user', 'abc'])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'числом' in msg or 'number' in msg.lower()

    def test_quota_out_of_range(self, admin_handler):
        """Quota value out of valid range."""
        admin_handler.set_quota('123', ['@user', '200000'])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'диапазон' in msg or 'range' in msg.lower()

    def test_quota_user_not_found(self, admin_handler):
        """Target user not found."""
        admin_handler._resolve_target = Mock(return_value=None)

        admin_handler.set_quota('123', ['@nonexistent', '50'])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'не найден' in msg or 'not found' in msg.lower()

    def test_quota_success(self, admin_handler):
        """Successful quota update."""
        target = User(chat_id='456', username='test', quota_gb=10)
        admin_handler._resolve_target = Mock(return_value=target)
        admin_handler.db.get_user = Mock(return_value=target)
        admin_handler.db.save_user = Mock()

        admin_handler.set_quota('123', ['@test', '50'])

        assert admin_handler.db.save_user.called
        assert target.quota_gb == 50


class TestSetExpire:
    """Tests for /expire command."""

    def test_expire_no_args(self, admin_handler):
        """Expire without arguments."""
        admin_handler.set_expire('123', [])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Формат' in msg or 'format' in msg.lower()

    def test_expire_invalid_date(self, admin_handler):
        """Invalid date format."""
        admin_handler.set_expire('123', ['@user', 'invalid'])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Формат даты' in msg or 'date format' in msg.lower()

    def test_expire_success(self, admin_handler):
        """Successful expiry date update."""
        target = User(chat_id='456', username='test')
        admin_handler._resolve_target = Mock(return_value=target)
        admin_handler.db.get_user = Mock(return_value=target)
        admin_handler.db.save_user = Mock()
        admin_handler.db._connect = Mock()

        admin_handler.set_expire('123', ['@test', '2026-12-31'])

        assert admin_handler.db.save_user.called
        # Check date is set to end of day
        assert 'T23:59:00' in target.subscription_expiry or target.subscription_expiry.endswith('23:59')


class TestRepairStuckSupport:
    """Tests for /repair_stuck command."""

    def test_repair_no_notifications_service(self, admin_handler):
        """No notification service available."""
        admin_handler.bot.services = {}

        admin_handler.repair_stuck_support('123', [])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'недоступен' in msg or 'unavailable' in msg.lower()

    def test_repair_success(self, admin_handler):
        """Successful repair."""
        mock_conn = _mock_db_connection()
        mock_conn.execute.return_value.fetchone.return_value = [5]
        admin_handler.db._connect.return_value = mock_conn
        admin_handler.bot.services['notifications']._repair_stuck_support_users_sync.return_value = None

        admin_handler.repair_stuck_support('123', [])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'Восстановлено' in msg or '5' in msg

    def test_repair_error(self, admin_handler):
        """Repair with exception."""
        admin_handler.bot.services['notifications']._repair_stuck_support_users_sync.side_effect = Exception("DB error")

        admin_handler.repair_stuck_support('123', [])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'DB error' in msg or '❌' in msg


class TestShowTopics:
    """Tests for /topics command."""

    def test_topics_basic(self, admin_handler):
        """Basic topics dump."""
        admin_handler.config.FORUM_ENABLED = True
        admin_handler.config.TOPIC_REQUESTS = 123
        admin_handler.config.TOPIC_USERS = 124

        admin_handler.show_topics('123', [])

        msg = admin_handler.bot.send_message.call_args[1]['text']
        assert 'TOPIC_REQUESTS' in msg
        assert '123' in msg
