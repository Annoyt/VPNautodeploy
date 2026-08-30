"""Tests for aiohttp web server (Mini App API)."""

import pytest
import json
from unittest.mock import Mock, patch, MagicMock
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from bot.core.web_server import WebAppServer
from bot.config import Settings
from bot.core.database import Database
from bot.models import User


class TestWebAppServer:
    """Test WebAppServer with pytest-aiohttp style."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config."""
        config = Mock(spec=Settings)
        config.BOT_TOKEN = "test_token_12345"
        config.is_admin = Mock(return_value=False)
        return config
    
    @pytest.fixture
    def mock_db(self):
        """Create mock database."""
        db = Mock(spec=Database)
        db.get_stats = Mock(return_value={'total': 0})
        db.get_user = Mock(return_value=None)
        db.get_all_users = Mock(return_value=[])
        return db
    
    @pytest.fixture
    def server(self, mock_config, mock_db):
        """Create WebAppServer instance."""
        with patch('bot.core.web_server.XUIService') as mock_xui:
            mock_xui_instance = Mock()
            mock_xui_instance.get_client_traffic = Mock(return_value={})
            mock_xui_instance.get_all_traffic = Mock(return_value={})
            mock_xui_instance.db = Mock()
            mock_xui_instance.db.get_all_client_traffic = Mock(return_value={})
            mock_xui.return_value = mock_xui_instance
            
            server = WebAppServer(mock_config, mock_db)
            server.xui = mock_xui_instance
            return server
    
    @pytest.mark.asyncio
    async def test_health_endpoint_success(self, server, mock_db):
        """Test health check returns healthy status."""
        request = Mock()
        request.query = {}
        
        response = await server.handle_health(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['status'] == 'healthy'
        assert data['database'] == 'connected'
        
    @pytest.mark.asyncio
    async def test_health_endpoint_db_failure(self, server, mock_db):
        """Test health check handles DB failure."""
        mock_db.get_stats = Mock(side_effect=Exception("DB Error"))
        
        request = Mock()
        request.query = {}
        
        response = await server.handle_health(request)
        
        assert response.status == 503
        data = json.loads(response.text)
        assert data['status'] == 'unhealthy'
        
    @pytest.mark.asyncio
    async def test_me_endpoint_no_init_data(self, server):
        """Test /api/me rejects request without initData."""
        request = Mock()
        request.query = {}
        
        response = await server.handle_me(request)
        
        assert response.status == 401
        data = json.loads(response.text)
        assert 'error' in data
        
    @pytest.mark.asyncio
    async def test_me_endpoint_invalid_init_data(self, server):
        """Test /api/me rejects invalid initData."""
        request = Mock()
        request.query = {'initData': 'invalid_data_no_hash'}
        
        response = await server.handle_me(request)
        
        assert response.status == 401
        
    @pytest.mark.asyncio
    async def test_me_endpoint_admin_user(self, server, mock_config):
        """Test /api/me returns admin info for admin."""
        mock_config.is_admin = Mock(return_value=True)
        
        # Create valid initData hash
        init_data = "user=%7B%22id%22%3A12345%2C%22username%22%3A%22admin%22%7D&hash=invalid"
        
        request = Mock()
        request.query = {'initData': init_data}
        
        # Patch _validate_init_data to return admin user
        with patch.object(server, '_validate_init_data', return_value={'id': 12345, 'username': 'admin'}):
            response = await server.handle_me(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['is_admin'] is True
        
    @pytest.mark.asyncio
    async def test_me_endpoint_regular_user_not_found(self, server, mock_db):
        """Test /api/me returns 404 for non-existent user."""
        mock_db.get_user = Mock(return_value=None)
        
        request = Mock()
        request.query = {'initData': 'test'}
        
        with patch.object(server, '_validate_init_data', return_value={'id': 12345}):
            response = await server.handle_me(request)
        
        assert response.status == 404
        data = json.loads(response.text)
        assert 'not found' in data['error'].lower()
        
    @pytest.mark.asyncio
    async def test_me_endpoint_regular_user_no_email(self, server, mock_db):
        """Test /api/me returns 404 for user without email."""
        user = Mock(spec=User)
        user.email = None
        mock_db.get_user = Mock(return_value=user)
        
        request = Mock()
        request.query = {'initData': 'test'}
        
        with patch.object(server, '_validate_init_data', return_value={'id': 12345}):
            response = await server.handle_me(request)
        
        assert response.status == 404
        
    @pytest.mark.asyncio
    async def test_me_endpoint_regular_user_success(self, server, mock_db):
        """Test /api/me returns user data for valid user."""
        user = Mock(spec=User)
        user.email = 'test@example.com'
        user.status = 'demo'
        user.quota_gb = 100
        user.subscription_expiry = '2026-12-31'
        user.platform = 'ios'
        mock_db.get_user = Mock(return_value=user)
        
        async def async_traffic(*args, **kwargs):
            return {'upload': 1024, 'download': 2048}
        server.xui.get_client_traffic = async_traffic
        
        request = Mock()
        request.query = {'initData': 'test'}
        
        with patch.object(server, '_validate_init_data', return_value={'id': 12345}):
            response = await server.handle_me(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert data['is_admin'] is False
        assert data['status'] == 'demo'
        assert data['quota_gb'] == 100
        assert data['consumed_bytes'] == 3072  # 1024 + 2048
        
    @pytest.mark.asyncio
    async def test_admin_users_endpoint_unauthorized(self, server):
        """Test /api/admin/users rejects non-admin."""
        request = Mock()
        request.remote = '127.0.0.1'
        request.query = {'initData': 'test'}

        with patch.object(server, '_validate_init_data', return_value={'id': 12345}):
            response = await server.handle_admin_users(request)

        assert response.status == 401
        
    @pytest.mark.asyncio
    async def test_admin_users_endpoint_success(self, server, mock_config, mock_db):
        """Test /api/admin/users returns all users for admin."""
        mock_config.is_admin = Mock(return_value=True)
        
        user1 = Mock(spec=User)
        user1.chat_id = '123'
        user1.username = 'user1'
        user1.email = 'user1@example.com'
        user1.status = 'demo'
        user1.quota_gb = 100
        user1.subscription_expiry = '2026-12-31'
        user1.platform = 'ios'
        user1.reject_count = 0
        user1.support_topic_id = None
        user1.created_at = '2024-01-01T00:00:00'
        user1.previous_state = None
        user1.limit_ip = 1
        user1.contact_email = None

        mock_db.get_all_users = Mock(return_value=[user1])
        
        # The endpoint reads bulk traffic through the API-aware service
        # method now, not xui.db directly (dead on the entry node).
        server.xui.get_all_traffic = Mock(return_value={
            'user1@example.com': {'upload': 1073741824, 'download': 2147483648}  # 1GB + 2GB
        })
        
        request = Mock()
        request.query = {'initData': 'test'}
        
        with patch.object(server, '_validate_init_data', return_value={'id': 99999}):
            response = await server.handle_admin_users(request)
        
        assert response.status == 200
        data = json.loads(response.text)
        assert 'users' in data
        assert len(data['users']) == 1
        assert data['users'][0]['username'] == 'user1'
        assert data['users'][0]['consumed_gb'] == 3.0  # 3GB consumed


class TestValidateInitData:
    """Test _validate_init_data method."""
    
    @pytest.fixture
    def server(self):
        """Create server with mocked dependencies."""
        config = Mock(spec=Settings)
        config.BOT_TOKEN = "test_token"
        
        with patch('bot.core.web_server.XUIService'):
            with patch('bot.core.web_server.Database'):
                return WebAppServer(config, Mock())
    
    def test_validate_init_data_empty(self, server):
        """Test empty initData returns None."""
        result = server._validate_init_data("")
        assert result is None
        
    def test_validate_init_data_none(self, server):
        """Test None initData returns None."""
        result = server._validate_init_data(None)
        assert result is None
        
    def test_validate_init_data_no_hash(self, server):
        """Test initData without hash returns None."""
        result = server._validate_init_data("user=test&auth_date=123")
        assert result is None
        
    def test_validate_init_data_invalid_hash(self, server):
        """Test initData with invalid hash returns None."""
        result = server._validate_init_data("user=test&hash=invalid_hash")
        assert result is None


class TestHy2AuthQuotaGate:
    """Panel-side quota gate in /api/hy2/auth.

    The bot.db status check said 'allow'; the gate must flip that to
    deny when the panel reports the client disabled or over quota, and
    must fail open when the panel has nothing to say.
    """

    def _make_server(self, traffic):
        from unittest.mock import AsyncMock

        config = Mock(spec=Settings)
        config.BOT_TOKEN = "test_token"
        config.is_admin = Mock(return_value=False)
        config.ENTRY_NODE_IP = ''
        config.EXIT_NODE_IP = ''

        db = MagicMock(spec=Database)
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (
            123, 'user_x_123@nekovo.ru', 'paid', None,
        )
        db._connect.return_value.__enter__.return_value = conn

        xui = Mock()
        xui.get_client_traffic = AsyncMock(return_value=traffic)
        return WebAppServer(config, db, xui_service=xui)

    async def _auth(self, server):
        from unittest.mock import AsyncMock
        request = Mock()
        request.json = AsyncMock(return_value={'auth': 'some-uuid', 'addr': ''})
        response = await server.handle_hy2_auth(request)
        return json.loads(response.text)

    @pytest.mark.asyncio
    async def test_allow_under_quota(self):
        server = self._make_server(
            {'upload': 1, 'download': 2, 'total': 100, 'enable': True}
        )
        data = await self._auth(server)
        assert data['ok'] is True

    @pytest.mark.asyncio
    async def test_deny_when_panel_disabled(self):
        server = self._make_server(
            {'upload': 1, 'download': 2, 'total': 100, 'enable': False}
        )
        data = await self._auth(server)
        assert data['ok'] is False

    @pytest.mark.asyncio
    async def test_deny_when_over_quota(self):
        server = self._make_server(
            {'upload': 60, 'download': 41, 'total': 100, 'enable': True}
        )
        data = await self._auth(server)
        assert data['ok'] is False

    @pytest.mark.asyncio
    async def test_unlimited_total_zero_allows(self):
        server = self._make_server(
            {'upload': 500, 'download': 500, 'total': 0, 'enable': True}
        )
        data = await self._auth(server)
        assert data['ok'] is True

    @pytest.mark.asyncio
    async def test_fail_open_when_panel_has_no_record(self):
        server = self._make_server(None)
        data = await self._auth(server)
        assert data['ok'] is True

    @pytest.mark.asyncio
    async def test_fail_open_when_panel_lookup_raises(self):
        from unittest.mock import AsyncMock
        server = self._make_server({})
        server.xui.get_client_traffic = AsyncMock(
            side_effect=Exception("panel down")
        )
        data = await self._auth(server)
        assert data['ok'] is True


class TestHy2AuthPaidTier:
    """Tier split between the two hysteria instances (2026-08-23):
    the MAIN callback (/api/hy2/auth) is freemium — demo UUIDs connect;
    the TURBO callback (/api/hy2t/auth) stays paid-only so a demo UUID
    extracted from a free key can't unlock the Brutal instance."""

    def _make_server(self, status):
        from unittest.mock import AsyncMock

        config = Mock(spec=Settings)
        config.BOT_TOKEN = "test_token"
        config.is_admin = Mock(return_value=False)
        config.ENTRY_NODE_IP = ''
        config.EXIT_NODE_IP = ''

        db = MagicMock(spec=Database)
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = (
            123, 'user_x_123@nekovo.ru', status, None,
        )
        db._connect.return_value.__enter__.return_value = conn

        xui = Mock()
        xui.get_client_traffic = AsyncMock(return_value={
            'upload': 0, 'download': 0, 'total': 100, 'enable': True,
        })
        return WebAppServer(config, db, xui_service=xui)

    async def _auth(self, server, turbo=False):
        from unittest.mock import AsyncMock
        request = Mock()
        request.json = AsyncMock(return_value={'auth': 'some-uuid', 'addr': ''})
        handler = server.handle_hy2t_auth if turbo else server.handle_hy2_auth
        response = await handler(request)
        return json.loads(response.text)

    @pytest.mark.asyncio
    async def test_paid_allowed(self):
        data = await self._auth(self._make_server('paid'))
        assert data['ok'] is True

    @pytest.mark.asyncio
    async def test_support_topic_allowed(self):
        # Mirrors PAID_USER_STATUSES in the subscription builder.
        data = await self._auth(self._make_server('support_topic'))
        assert data['ok'] is True

    @pytest.mark.asyncio
    async def test_demo_allowed_on_main_instance(self):
        data = await self._auth(self._make_server('demo'))
        assert data['ok'] is True

    @pytest.mark.asyncio
    async def test_pending_denied_on_main_instance(self):
        data = await self._auth(self._make_server('pending_demo'))
        assert data['ok'] is False

    @pytest.mark.asyncio
    async def test_paid_allowed_on_turbo(self):
        data = await self._auth(self._make_server('paid'), turbo=True)
        assert data['ok'] is True

    @pytest.mark.asyncio
    async def test_demo_denied_on_turbo(self):
        data = await self._auth(self._make_server('demo'), turbo=True)
        assert data['ok'] is False


class TestDpiExitReport:
    """POST /api/dpi/exit_report — the exit node's xray-log feed.

    Token gate must be airtight (the endpoint is on the public web
    server), node IPs must bucket as *TUNNEL* instead of leaking the
    entry DC's geo, and scanner IPs must land in real geo buckets so
    the probing alerts finally have countries to talk about.
    """

    ENTRY_IP = '130.49.146.10'

    def _make_server(self, token='sekret'):
        config = Mock(spec=Settings)
        config.BOT_TOKEN = 'test_token'
        config.is_admin = Mock(return_value=False)
        config.ENTRY_NODE_IP = self.ENTRY_IP
        config.EXIT_NODE_IP = '84.75.76.109'
        config.DPI_REPORT_TOKEN = token

        db = MagicMock(spec=Database)
        conn = MagicMock()
        db._connect.return_value.__enter__.return_value = conn
        server = WebAppServer(config, db, xui_service=Mock())
        return server, conn

    def _request(self, payload, token='sekret'):
        from unittest.mock import AsyncMock
        request = Mock()
        request.headers = {'X-DPI-Token': token} if token is not None else {}
        request.json = AsyncMock(return_value=payload)
        return request

    @pytest.mark.asyncio
    async def test_missing_token_rejected(self):
        server, conn = self._make_server()
        resp = await server.handle_dpi_exit_report(
            self._request({'access': []}, token=None)
        )
        assert resp.status == 403
        conn.executemany.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_token_rejected(self):
        server, conn = self._make_server()
        resp = await server.handle_dpi_exit_report(
            self._request({'access': []}, token='nope')
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_unconfigured_endpoint_rejects_everything(self):
        # Blank server-side token = endpoint off, even for blank client
        # tokens (no compare_digest('' , '') backdoor).
        server, conn = self._make_server(token='')
        resp = await server.handle_dpi_exit_report(
            self._request({'access': []}, token='')
        )
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_report_written_with_geo_buckets(self):
        server, conn = self._make_server()
        payload = {
            'access': [
                {'tag': 'inbound-2053', 'conns': 5, 'uniq_emails': 2,
                 'ips': {self.ENTRY_IP: 5}},
            ],
            'rejects': [
                {'kind': 'reality', 'reason': 'failed to read client hello',
                 'count': 3, 'ips': {'45.156.128.134': 3}},
            ],
        }
        with patch('bot.services.geoip.lookup',
                   return_value=('RU', '🇷🇺')), \
             patch('bot.services.geoip.lookup_asn',
                   return_value=('AS197068', 'RKN probing range')):
            resp = await server.handle_dpi_exit_report(self._request(payload))
        assert resp.status == 200
        rows = conn.executemany.call_args[0][1]
        by_bucket = {(r[1], r[4]): r for r in rows}
        # tunneled users: *TUNNEL* bucket, tag mapped to cf-ws
        tunnel = by_bucket[('*TUNNEL*', 'cf-ws')]
        assert tunnel[5] == 5           # conn_count
        assert tunnel[9] == 0           # handshake_fail_count
        # scanner rejects: real geo, probe IPs and reasons preserved
        probing = by_bucket[('RU', 'reality')]
        assert probing[9] == 3
        assert json.loads(probing[10]) == [['45.156.128.134', 3]]
        assert json.loads(probing[11]) == {'failed to read client hello': 3}

    @pytest.mark.asyncio
    async def test_counts_beyond_ip_cap_fall_into_null_bucket(self):
        server, conn = self._make_server()
        payload = {
            'access': [
                # reporter capped the ip dict: 2 attributed, 7 total
                {'tag': 'inbound-8444', 'conns': 7,
                 'ips': {self.ENTRY_IP: 2}},
            ],
            'rejects': [],
        }
        resp = await server.handle_dpi_exit_report(self._request(payload))
        assert resp.status == 200
        rows = conn.executemany.call_args[0][1]
        by_bucket = {(r[1], r[4]): r for r in rows}
        assert by_bucket[('*TUNNEL*', 'ss2022')][5] == 2
        assert by_bucket[(None, 'ss2022')][5] == 5

    @pytest.mark.asyncio
    async def test_empty_report_writes_nothing(self):
        server, conn = self._make_server()
        resp = await server.handle_dpi_exit_report(
            self._request({'access': [], 'rejects': []})
        )
        assert resp.status == 200
        conn.executemany.assert_not_called()
