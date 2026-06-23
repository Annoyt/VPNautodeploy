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
        
        mock_db.get_all_users = Mock(return_value=[user1])
        
        server.xui.db.get_all_client_traffic = Mock(return_value={
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
