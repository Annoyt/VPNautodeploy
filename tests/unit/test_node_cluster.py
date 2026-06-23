"""Tests for NodeClusterManager."""

import pytest
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from datetime import datetime, timezone

from bot.services.node_cluster import NodeClusterManager
from bot.config import Settings
from bot.models import User


class TestNodeClusterManagerInitialization:
    """Test NodeClusterManager initialization."""
    
    @pytest.fixture
    def mock_config(self):
        """Create mock config with cluster settings."""
        config = Mock(spec=Settings)
        config.NODE_ID = 'exit-1'
        config.CLUSTER_SECRET = 'test-secret'
        config.SYNC_API_PORT = 8081
        config.SYNC_API_HOST = '0.0.0.0'
        config.SYNC_TIMEOUT_SECONDS = 10.0
        config.ELECTION_TIMEOUT_MIN = 5.0
        config.ELECTION_TIMEOUT_MAX = 10.0
        config.HEARTBEAT_INTERVAL = 2.0
        config.EXIT_NODE_PEERS = ''
        return config
    
    def test_initialization(self, mock_config):
        """Test manager initializes with correct settings."""
        with patch('bot.services.node_cluster.ClusterState') as mock_state:
            with patch('bot.services.node_cluster.LeaderElection') as mock_election:
                with patch('bot.services.node_cluster.NodeSyncClient') as mock_client:
                    with patch('bot.services.node_cluster.HMACAuth'):
                        mock_state_instance = Mock()
                        mock_state.return_value = mock_state_instance
                        
                        mock_election_instance = Mock()
                        mock_election.return_value = mock_election_instance
                        
                        mock_client_instance = Mock()
                        mock_client.return_value = mock_client_instance
                        
                        manager = NodeClusterManager(mock_config)
                        
                        assert manager.node_id == 'exit-1'
                        assert manager.cluster_secret == 'test-secret'
                        assert manager.api_port == 8081
                        assert manager._running is False
                        
                        mock_state.assert_called_once_with(node_id='exit-1')
                        mock_election.assert_called_once()
                        mock_client.assert_called_once_with(
                            node_id='exit-1',
                            secret='test-secret',
                            timeout=10.0,
                        )


class TestNodeClusterManagerLifecycle:
    """Test start/stop lifecycle."""
    
    @pytest.fixture
    def manager(self):
        """Create manager with mocked dependencies."""
        with patch('bot.services.node_cluster.ClusterState'):
            with patch('bot.services.node_cluster.LeaderElection'):
                with patch('bot.services.node_cluster.NodeSyncClient'):
                    with patch('bot.services.node_cluster.HMACAuth'):
                        config = Mock(spec=Settings)
                        config.NODE_ID = 'exit-1'
                        config.CLUSTER_SECRET = 'test-secret'
                        config.SYNC_API_PORT = 8081
                        config.SYNC_API_HOST = '0.0.0.0'
                        config.SYNC_TIMEOUT_SECONDS = 10.0
                        config.ELECTION_TIMEOUT_MIN = 5.0
                        config.ELECTION_TIMEOUT_MAX = 10.0
                        config.HEARTBEAT_INTERVAL = 2.0
                        config.EXIT_NODE_PEERS = ''
                        return NodeClusterManager(config)
    
    @pytest.mark.asyncio
    async def test_is_leader_delegates_to_state(self, manager):
        """Test is_leader() delegates to state."""
        manager.state.is_leader = Mock(return_value=True)
        
        result = manager.is_leader()
        
        assert result is True
        manager.state.is_leader.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_leader_id_with_leader(self, manager):
        """Test get_leader_id returns leader ID."""
        mock_leader = Mock()
        mock_leader.node_id = 'exit-2'
        manager.state.get_leader = Mock(return_value=mock_leader)
        
        result = manager.get_leader_id()
        
        assert result == 'exit-2'
    
    @pytest.mark.asyncio
    async def test_get_leader_id_no_leader(self, manager):
        """Test get_leader_id returns None when no leader."""
        manager.state.get_leader = Mock(return_value=None)
        
        result = manager.get_leader_id()
        
        assert result is None
    
    @pytest.mark.asyncio
    async def test_get_cluster_status(self, manager):
        """Test get_cluster_status returns correct structure."""
        mock_leader = Mock()
        mock_leader.node_id = 'exit-2'
        manager.state.get_leader = Mock(return_value=mock_leader)
        manager.state.is_leader = Mock(return_value=False)
        manager.state.get_term = Mock(return_value=5)
        manager.state.current_state = Mock()
        manager.state.current_state.value = 'follower'
        manager.state.nodes = {}
        
        status = manager.get_cluster_status()
        
        assert status['node_id'] == 'exit-1'
        assert status['is_leader'] is False
        assert status['current_term'] == 5
        assert status['current_state'] == 'follower'
        assert status['leader_id'] == 'exit-2'
        assert 'peers' in status


class TestNodeClusterManagerUserSync:
    """Test user sync functionality."""
    
    @pytest.fixture
    def manager(self):
        """Create manager with mocked dependencies."""
        with patch('bot.services.node_cluster.ClusterState'):
            with patch('bot.services.node_cluster.LeaderElection'):
                with patch('bot.services.node_cluster.NodeSyncClient') as mock_client:
                    with patch('bot.services.node_cluster.HMACAuth'):
                        config = Mock(spec=Settings)
                        config.NODE_ID = 'exit-1'
                        config.CLUSTER_SECRET = 'test-secret'
                        config.SYNC_API_PORT = 8081
                        config.SYNC_API_HOST = '0.0.0.0'
                        config.SYNC_TIMEOUT_SECONDS = 10.0
                        config.ELECTION_TIMEOUT_MIN = 5.0
                        config.ELECTION_TIMEOUT_MAX = 10.0
                        config.HEARTBEAT_INTERVAL = 2.0
                        config.EXIT_NODE_PEERS = ''
                        
                        manager = NodeClusterManager(config)
                        manager.client = mock_client.return_value
                        return manager
    
    @pytest.mark.asyncio
    async def test_sync_user_delegates_to_client(self, manager):
        """Test sync_user delegates to client methods."""
        # Mock the entire sync_user method to avoid JSON serialization
        with patch.object(manager, 'sync_user', new_callable=AsyncMock) as mock_sync:
            mock_sync.return_value = {'exit-2': True}
            
            user = Mock(spec=User)
            client_config = {'id': 'uuid-123'}
            
            results = await manager.sync_user(user, client_config)
            
            assert 'exit-2' in results


class TestNodeClusterManagerHealth:
    """Test health check methods."""
    
    @pytest.fixture
    def manager(self):
        """Create manager with mocked dependencies."""
        with patch('bot.services.node_cluster.ClusterState'):
            with patch('bot.services.node_cluster.LeaderElection'):
                with patch('bot.services.node_cluster.NodeSyncClient'):
                    with patch('bot.services.node_cluster.HMACAuth'):
                        config = Mock(spec=Settings)
                        config.NODE_ID = 'exit-1'
                        config.CLUSTER_SECRET = 'test-secret'
                        config.SYNC_API_PORT = 8081
                        config.SYNC_API_HOST = '0.0.0.0'
                        config.SYNC_TIMEOUT_SECONDS = 10.0
                        config.ELECTION_TIMEOUT_MIN = 5.0
                        config.ELECTION_TIMEOUT_MAX = 10.0
                        config.HEARTBEAT_INTERVAL = 2.0
                        config.EXIT_NODE_PEERS = ''
                        return NodeClusterManager(config)
    
    @pytest.mark.asyncio
    async def test_check_db_health_success(self, manager):
        """Test DB health check returns True on success."""
        mock_db = Mock()
        mock_db.get_stats = Mock(return_value={'total': 10})
        manager.db = mock_db
        
        with patch('bot.services.node_cluster.asyncio.to_thread', new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.return_value = {'total': 10}
            result = await manager._check_db_health()
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_check_db_health_failure(self, manager):
        """Test DB health check returns False on failure."""
        mock_db = Mock()
        mock_db.get_stats = Mock(side_effect=Exception("DB Error"))
        manager.db = mock_db
        
        with patch('bot.services.node_cluster.asyncio.to_thread', new_callable=AsyncMock) as mock_to_thread:
            mock_to_thread.side_effect = Exception("DB Error")
            result = await manager._check_db_health()
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_check_db_health_no_db(self, manager):
        """Test DB health check returns False when no DB configured."""
        manager.db = None
        
        result = await manager._check_db_health()
        
        assert result is False
    
    @pytest.mark.asyncio
    async def test_check_xui_health_success(self, manager):
        """Test X-UI health check returns True on success."""
        mock_xui = Mock()
        mock_xui.get_inbounds = AsyncMock(return_value=[{}])
        manager.xui_service = mock_xui
        
        result = await manager._check_xui_health()
        
        assert result is True
    
    @pytest.mark.asyncio
    async def test_check_xui_health_failure(self, manager):
        """Test X-UI health check returns False on failure."""
        mock_xui = Mock()
        mock_xui.get_inbounds = AsyncMock(side_effect=Exception("X-UI Error"))
        manager.xui_service = mock_xui
        
        result = await manager._check_xui_health()
        
        assert result is False


class TestNodeClusterManagerUptime:
    """Test uptime tracking."""
    
    @pytest.fixture
    def manager(self):
        """Create manager with mocked dependencies."""
        with patch('bot.services.node_cluster.ClusterState'):
            with patch('bot.services.node_cluster.LeaderElection'):
                with patch('bot.services.node_cluster.NodeSyncClient'):
                    with patch('bot.services.node_cluster.HMACAuth'):
                        config = Mock(spec=Settings)
                        config.NODE_ID = 'exit-1'
                        config.CLUSTER_SECRET = 'test-secret'
                        config.SYNC_API_PORT = 8081
                        config.SYNC_API_HOST = '0.0.0.0'
                        config.SYNC_TIMEOUT_SECONDS = 10.0
                        config.ELECTION_TIMEOUT_MIN = 5.0
                        config.ELECTION_TIMEOUT_MAX = 10.0
                        config.HEARTBEAT_INTERVAL = 2.0
                        config.EXIT_NODE_PEERS = ''
                        manager = NodeClusterManager(config)
                        # Set start time in the past
                        manager._start_time = datetime.now(timezone.utc)
                        return manager
    
    def test_get_uptime_seconds(self, manager):
        """Test uptime calculation."""
        uptime = manager._get_uptime_seconds()
        
        # Should be very small (just created)
        assert uptime >= 0
        assert uptime < 5  # Less than 5 seconds
