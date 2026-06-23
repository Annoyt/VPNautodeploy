"""Tests for VPN service - Multi-node support."""

import pytest
from unittest.mock import Mock, AsyncMock

from bot.utils.exceptions import VPNGenerationError

from bot.services.vpn import VPNService
from bot.models.node import Node, NodeType, NodeStatus


class TestVPNServiceMultiNode:
    """Tests for VPNService multi-node functionality."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config"""
        config = Mock()
        config.ENTRY_NODE_IP = '203.0.113.20'
        config.ENTRY_NODE_PORT = 443
        config.REALITY_PUBLIC_KEY = 'test_pubkey'
        config.SNI_VALUE = 'www.microsoft.com'
        config.SID_VALUE = ''
        config.DEMO_TRAFFIC_GB = 5
        config.DB_PATH = ':memory:'
        return config
    
    @pytest.fixture
    def mock_node_manager(self):
        """Create mock NodeManager"""
        nm = Mock()
        return nm
    
    @pytest.fixture
    def vpn_service_legacy(self, mock_config):
        """VPNService in legacy mode"""
        return VPNService(config=mock_config)
    
    @pytest.fixture
    def vpn_service_multi(self, mock_config, mock_node_manager):
        """VPNService in multi-node mode"""
        return VPNService(config=mock_config, node_manager=mock_node_manager)
    
    @pytest.fixture
    def sample_exit_node(self):
        """Sample exit node"""
        return Node(
            id=1,
            name='exit-frankfurt-1',
            type=NodeType.EXIT,
            host='203.0.113.30',
            api_port=2026,
            vpn_port=443,
            base_path='/this_is_fine',
            api_username='admin',
            api_password='admin123',
            public_key='exit_pubkey',
            sni='www.microsoft.com',
            sid='',
            region='eu',
            city='frankfurt',
            status=NodeStatus.ACTIVE,
            is_primary=True,
            weight=100,
            max_clients=100,
            current_clients=10
        )
    
    @pytest.fixture
    def sample_entry_node(self):
        """Sample entry node"""
        return Node(
            id=2,
            name='entry-moscow-1',
            type=NodeType.ENTRY,
            host='203.0.113.20',
            api_port=2026,
            vpn_port=443,
            base_path='/this_is_fine',
            api_username='admin',
            api_password='admin123',
            public_key='entry_pubkey',
            sni='www.microsoft.com',
            sid='',
            region='ru',
            city='moscow',
            status=NodeStatus.ACTIVE,
            is_primary=True,
            weight=100,
            max_clients=200,
            current_clients=50
        )
    
    def test_build_vless_link_basic(self, vpn_service_legacy):
        """Test building VLESS link with basic parameters"""
        link = vpn_service_legacy._build_vless_link(
            client_uuid='test-uuid-1234',
            email='user_test@nekovo.ru',
            entry_ip='203.0.113.20',
            public_key='test_pubkey',
            sni='www.microsoft.com',
            sid='',
            port=443
        )
        
        assert link.startswith('vless://')
        assert 'test-uuid-1234' in link
        assert '203.0.113.20:443' in link
        assert 'pbk=test_pubkey' in link
        assert 'sni=www.microsoft.com' in link
        assert 'user_test' in link  # Connection name
    
    def test_build_vless_link_with_sid(self, vpn_service_legacy):
        """Test building VLESS link with short ID"""
        link = vpn_service_legacy._build_vless_link(
            client_uuid='test-uuid',
            email='user@nekovo.ru',
            entry_ip='1.1.1.1',
            public_key='pk',
            sni='sni.com',
            sid='01',
            port=443
        )
        
        assert 'sid=01' in link
    
    @pytest.mark.asyncio
    async def test_generate_vless_link_for_user_with_assignment(
        self, vpn_service_multi, mock_node_manager, sample_exit_node, sample_entry_node
    ):
        """Test generating link when user has node assignment"""
        # Mock user
        mock_user = Mock()
        mock_user.chat_id = '12345'
        mock_user.uuid = 'test-uuid'
        mock_user.email = 'user_test@nekovo.ru'
        
        # Mock node assignment
        mock_node_manager.get_user_nodes = AsyncMock(return_value={
            'exit_node_id': 1,
            'entry_node_id': 2
        })
        mock_node_manager.get_node = AsyncMock(side_effect=[
            sample_exit_node,  # First call for exit node
            sample_entry_node  # Second call for entry node
        ])
        
        link = await vpn_service_multi.generate_vless_link_for_user('12345', mock_user)
        
        assert link is not None
        assert link.startswith('vless://')
        assert 'test-uuid' in link
        assert '203.0.113.20' in link  # Entry node IP
        assert 'exit_pubkey' in link  # Exit node public key
    
    @pytest.mark.asyncio
    async def test_generate_vless_link_for_user_auto_assign(
        self, vpn_service_multi, mock_node_manager, sample_exit_node, sample_entry_node
    ):
        """Test generating link with auto-assignment"""
        mock_user = Mock()
        mock_user.chat_id = '12345'
        mock_user.uuid = 'test-uuid'
        mock_user.email = 'user_test@nekovo.ru'
        
        # No existing assignment
        mock_node_manager.get_user_nodes = AsyncMock(return_value=None)
        mock_node_manager.find_best_exit_for_user = AsyncMock(return_value=sample_exit_node)
        mock_node_manager.assign_user_to_node = AsyncMock(return_value=True)
        mock_node_manager.get_available_entry = AsyncMock(return_value=sample_entry_node)
        mock_node_manager.get_node = AsyncMock(return_value=sample_exit_node)
        
        link = await vpn_service_multi.generate_vless_link_for_user('12345', mock_user)
        
        assert link is not None
        mock_node_manager.assign_user_to_node.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_generate_vless_link_for_user_no_nodes(
        self, vpn_service_multi, mock_node_manager
    ):
        """Test when no nodes available"""
        mock_user = Mock()
        mock_user.chat_id = '12345'
        mock_user.uuid = 'test-uuid'
        mock_user.email = 'user@nekovo.ru'
        
        mock_node_manager.get_user_nodes = AsyncMock(return_value=None)
        mock_node_manager.find_best_exit_for_user = AsyncMock(return_value=None)
        
        with pytest.raises(VPNGenerationError):
            await vpn_service_multi.generate_vless_link_for_user('12345', mock_user)
    
    @pytest.mark.asyncio
    async def test_generate_vless_link_legacy_mode(self, vpn_service_legacy):
        """Test that legacy mode still works"""
        mock_user = Mock()
        mock_user.chat_id = '12345'
        mock_user.uuid = 'test-uuid'
        mock_user.email = 'user_test@nekovo.ru'
        
        link = await vpn_service_legacy.generate_vless_link_for_user('12345', mock_user)
        
        assert link is not None
        assert link.startswith('vless://')
        assert '203.0.113.20' in link  # From config
    
    @pytest.mark.asyncio
    async def test_get_connection_info_multi_node(
        self, vpn_service_multi, mock_node_manager, sample_exit_node, sample_entry_node
    ):
        """Test getting connection info in multi-node mode"""
        mock_node_manager.get_user_nodes = AsyncMock(return_value={
            'exit_node_id': 1,
            'entry_node_id': 2
        })
        mock_node_manager.get_node = AsyncMock(side_effect=[
            sample_exit_node,
            sample_entry_node
        ])
        
        info = await vpn_service_multi.get_connection_info('12345')
        
        assert info is not None
        assert info['type'] == 'multi-node'
        assert info['entry_ip'] == '203.0.113.20'
        assert info['exit_ip'] == '203.0.113.30'
        assert info['exit_node_name'] == 'exit-frankfurt-1'
        assert info['region'] == 'eu'
    
    @pytest.mark.asyncio
    async def test_get_connection_info_legacy_mode(self, vpn_service_legacy):
        """Test getting connection info in legacy mode"""
        info = await vpn_service_legacy.get_connection_info('12345')
        
        assert info is not None
        assert info['type'] == 'legacy'
        assert info['entry_ip'] == '203.0.113.20'
        assert info['port'] == 443
    
    def test_get_connection_preview_valid(self, vpn_service_legacy):
        """Test parsing valid VLESS link"""
        vless_link = 'vless://uuid-123@host.com:443?security=reality&sni=example.com&flow=xtls-rprx-vision#MyConnection'
        
        preview = vpn_service_legacy.get_connection_preview(vless_link)
        
        assert preview['valid'] is True
        assert preview['uuid'] == 'uuid-123'
        assert preview['host'] == 'host.com'
        assert preview['port'] == 443
        assert preview['name'] == 'MyConnection'
        assert preview['sni'] == 'example.com'
    
    def test_get_connection_preview_invalid(self, vpn_service_legacy):
        """Test parsing invalid link"""
        preview = vpn_service_legacy.get_connection_preview('not-a-valid-link')
        
        assert preview['valid'] is False
        assert 'error' in preview
    
    def test_create_client_config_defaults(self, vpn_service_legacy):
        """Test creating client config with defaults"""
        config = vpn_service_legacy.create_client_config('12345')
        
        assert 'id' in config
        assert config['email'] == 'user_12345@nekovo.ru'
        assert config['flow'] == 'xtls-rprx-vision'
        assert config['limitIp'] == 1
        assert config['totalGB'] == 5 * 1024 ** 3  # 5 GB
        assert config['enable'] is True
    
    def test_create_client_config_with_username(self, vpn_service_legacy):
        """Test creating client config with username"""
        config = vpn_service_legacy.create_client_config('12345', username='john_doe')
        
        assert config['email'] == 'user_john_doe_12345@nekovo.ru'
    
    def test_get_client_info(self, vpn_service_legacy):
        """Test extracting client info"""
        client_config = {
            'id': 'test-uuid',
            'email': 'user@test.com',
            'totalGB': 10 * 1024 ** 3,
            'expiryTime': 0,
            'enable': True
        }
        
        info = vpn_service_legacy.get_client_info(client_config)
        
        assert info['uuid'] == 'test-uuid'
        assert info['email'] == 'user@test.com'
        assert info['traffic_gb'] == 10.0
        assert info['expiry'] == 'No expiry'
        assert info['enabled'] is True
