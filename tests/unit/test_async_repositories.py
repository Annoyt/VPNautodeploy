"""Tests for async repository adapters - Phase 3.5 refactoring."""

import pytest
import asyncio
from unittest.mock import Mock, patch

from bot.core.repositories import (
    AsyncUserRepository,
    AsyncTicketRepository,
    AsyncNodeRepository,
    AsyncMessageMapRepository,
)
from bot.models import User


class TestAsyncUserRepository:
    """Test async user repository adapter."""
    
    @pytest.fixture
    def async_repo(self, tmp_path):
        """Create async repository with temp database."""
        db_path = str(tmp_path / "test.db")
        # Create the database first
        from bot.core.repositories import UserRepository
        sync_repo = UserRepository(db_path)
        # Create tables
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id TEXT PRIMARY KEY,
                username TEXT,
                previous_state TEXT,
                reject_count INTEGER DEFAULT 0,
                uuid TEXT,
                email TEXT,
                status TEXT DEFAULT 'new',
                lang TEXT DEFAULT 'ru',
                platform TEXT,
                support_topic_id INTEGER,
                subscription_expiry TEXT,
                limit_ip INTEGER DEFAULT 1,
                quota_gb REAL DEFAULT 5.0,
                last_traffic_update TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                xui_synced INTEGER DEFAULT 0,
                next_protocol_idx INTEGER DEFAULT 0,
                last_country TEXT,
                last_asn TEXT,
                last_city TEXT,
                last_lat REAL,
                last_lon REAL,
                contact_email TEXT
            )
        ''')
        conn.commit()
        conn.close()
        return AsyncUserRepository(db_path)
    
    @pytest.mark.asyncio
    async def test_get_by_id_not_found(self, async_repo):
        """Test getting non-existent user."""
        result = await async_repo.get_by_id('99999')
        assert result is None
    
    @pytest.mark.asyncio
    async def test_save_and_get_user(self, async_repo):
        """Test saving and retrieving user."""
        user = User(
            chat_id='12345',
            username='testuser',
            uuid='test-uuid',
            email='test@example.com',
            status='demo'
        )
        
        # Save user
        saved = await async_repo.save(user)
        assert saved is True
        
        # Retrieve user
        retrieved = await async_repo.get_by_id('12345')
        assert retrieved is not None
        assert retrieved.chat_id == '12345'
        assert retrieved.username == 'testuser'
    
    @pytest.mark.asyncio
    async def test_get_by_username(self, async_repo):
        """Test getting user by username."""
        user = User(
            chat_id='12345',
            username='testuser',
            uuid='test-uuid',
            email='test@example.com'
        )
        await async_repo.save(user)
        
        retrieved = await async_repo.get_by_username('testuser')
        assert retrieved is not None
        assert retrieved.chat_id == '12345'
    
    @pytest.mark.asyncio
    async def test_update_status(self, async_repo):
        """Test updating user status."""
        user = User(
            chat_id='12345',
            username='testuser',
            uuid='test-uuid',
            status='pending_demo'
        )
        await async_repo.save(user)
        
        # Update status
        updated = await async_repo.update_status('12345', 'demo')
        assert updated is True
        
        # Verify
        retrieved = await async_repo.get_by_id('12345')
        assert retrieved.status == 'demo'
    
    @pytest.mark.asyncio
    async def test_get_by_status(self, async_repo):
        """Test getting users by status."""
        # Create users with different statuses
        user1 = User(chat_id='1', uuid='uuid1', status='pending_demo')
        user2 = User(chat_id='2', uuid='uuid2', status='pending_demo')
        user3 = User(chat_id='3', uuid='uuid3', status='demo')
        
        await async_repo.save(user1)
        await async_repo.save(user2)
        await async_repo.save(user3)
        
        pending = await async_repo.get_by_status('pending_demo')
        assert len(pending) == 2
    
    @pytest.mark.asyncio
    async def test_get_pending(self, async_repo):
        """Test getting pending users."""
        user = User(chat_id='12345', uuid='uuid', status='pending_demo')
        await async_repo.save(user)
        
        pending = await async_repo.get_pending()
        assert len(pending) == 1
        assert pending[0].chat_id == '12345'
    
    @pytest.mark.asyncio
    async def test_get_all(self, async_repo):
        """Test getting all users."""
        user1 = User(chat_id='1', uuid='uuid1')
        user2 = User(chat_id='2', uuid='uuid2')
        
        await async_repo.save(user1)
        await async_repo.save(user2)
        
        all_users = await async_repo.get_all()
        assert len(all_users) == 2
    
    @pytest.mark.asyncio
    async def test_get_stats(self, async_repo):
        """Test getting user statistics."""
        user1 = User(chat_id='1', uuid='uuid1', status='demo')
        user2 = User(chat_id='2', uuid='uuid2', status='pending_demo')
        
        await async_repo.save(user1)
        await async_repo.save(user2)
        
        stats = await async_repo.get_stats()
        assert stats['total'] == 2
        assert 'by_status' in stats


class TestAsyncTicketRepository:
    """Test async ticket repository adapter."""
    
    @pytest.fixture
    def async_repo(self, tmp_path):
        """Create async repository with temp database."""
        db_path = str(tmp_path / "test.db")
        # Create tables
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                chat_id TEXT,
                topic_id INTEGER PRIMARY KEY,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        return AsyncTicketRepository(db_path)
    
    @pytest.mark.asyncio
    async def test_create_and_get_ticket(self, async_repo):
        """Test creating and retrieving ticket."""
        result = await async_repo.create('12345', 100, 'open')
        assert result is True
        
        ticket = await async_repo.get_by_topic_id(100)
        assert ticket is not None
        assert ticket['chat_id'] == '12345'
    
    @pytest.mark.asyncio
    async def test_get_by_chat_id(self, async_repo):
        """Test getting tickets by chat ID."""
        await async_repo.create('12345', 101)
        await async_repo.create('12345', 102)
        
        tickets = await async_repo.get_by_chat_id('12345')
        assert len(tickets) == 2


class TestAsyncNodeRepository:
    """Test async node repository adapter."""
    
    @pytest.fixture
    def async_repo(self, tmp_path):
        """Create async repository with temp database."""
        db_path = str(tmp_path / "test.db")
        # Create tables with full schema
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                type TEXT,
                host TEXT,
                api_port INTEGER,
                vpn_port INTEGER,
                base_path TEXT,
                api_username TEXT,
                api_password TEXT,
                public_key TEXT,
                sni TEXT,
                sid TEXT,
                region TEXT,
                city TEXT,
                status TEXT DEFAULT 'inactive',
                is_primary INTEGER DEFAULT 0,
                weight INTEGER DEFAULT 100,
                max_clients INTEGER DEFAULT 100,
                current_clients INTEGER DEFAULT 0
            )
        ''')
        conn.commit()
        conn.close()
        return AsyncNodeRepository(db_path)
    
    @pytest.mark.asyncio
    async def test_create_and_get_node(self, async_repo):
        """Test creating and retrieving node."""
        from bot.models.node import Node, NodeType, NodeStatus
        
        # Create node with all required fields
        node = Node(
            id=None,
            name='Test Node',
            type=NodeType.EXIT,
            host='192.168.1.1',
            api_port=8080,
            vpn_port=443,
            base_path='/api',
            api_username='admin',
            api_password='secret',
            public_key='key123',
            sni='sni.example.com',
            sid=None,
            region='EU',
            city='Frankfurt',
            status=NodeStatus.ACTIVE,
            is_primary=True,
            weight=100,
            max_clients=100,
            current_clients=0
        )
        
        node_id = await async_repo.create(node)
        assert node_id > 0
        
        retrieved = await async_repo.get_by_id(node_id)
        assert retrieved is not None
        assert retrieved.name == 'Test Node'
    
    @pytest.mark.asyncio
    async def test_update_status(self, async_repo):
        """Test updating node status."""
        from bot.models.node import Node, NodeType, NodeStatus
        
        node = Node(
            id=None,
            name='Test Node',
            type=NodeType.EXIT,
            host='192.168.1.1',
            api_port=8080,
            vpn_port=443,
            base_path='/api',
            api_username='admin',
            api_password='secret',
            public_key='key123',
            sni='sni.example.com',
            sid=None,
            region='EU',
            city='Frankfurt',
            status=NodeStatus.ACTIVE,
            is_primary=True,
            weight=100,
            max_clients=100,
            current_clients=0
        )
        
        node_id = await async_repo.create(node)
        
        # Update status
        updated = await async_repo.update_status(node_id, NodeStatus.OFFLINE)
        assert updated is True
        
        retrieved = await async_repo.get_by_id(node_id)
        assert retrieved.status == NodeStatus.OFFLINE


class TestAsyncMessageMapRepository:
    """Test async message map repository adapter."""
    
    @pytest.fixture
    def async_repo(self, tmp_path):
        """Create async repository with temp database."""
        db_path = str(tmp_path / "test.db")
        # Create tables
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS message_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_msg_id INTEGER,
                user_chat_id TEXT,
                user_msg_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        return AsyncMessageMapRepository(db_path)
    
    @pytest.mark.asyncio
    async def test_log_and_get_message_map(self, async_repo):
        """Test logging and retrieving message mapping."""
        logged = await async_repo.log_message_map(
            admin_msg_id=100,
            user_chat_id='12345',
            user_msg_id=50
        )
        assert logged is True
        
        mapping = await async_repo.get_mapped_user_message(100)
        assert mapping is not None
        assert mapping['chat_id'] == '12345'
        assert mapping['message_id'] == 50


class TestAsyncRepositoryConcurrency:
    """Test concurrent access to async repositories."""
    
    @pytest.mark.asyncio
    async def test_concurrent_reads(self, tmp_path):
        """Test concurrent read operations."""
        db_path = str(tmp_path / "test.db")
        
        # Setup with full schema
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute('''
            CREATE TABLE users (
                chat_id TEXT PRIMARY KEY,
                username TEXT,
                previous_state TEXT,
                reject_count INTEGER DEFAULT 0,
                uuid TEXT,
                email TEXT,
                status TEXT,
                lang TEXT,
                platform TEXT,
                support_topic_id INTEGER,
                created_at TEXT,
                subscription_expiry TEXT,
                limit_ip INTEGER,
                quota_gb REAL,
                last_traffic_update TEXT,
                xui_synced INTEGER DEFAULT 0,
                next_protocol_idx INTEGER DEFAULT 0,
                last_country TEXT,
                last_asn TEXT,
                last_city TEXT,
                last_lat REAL,
                last_lon REAL,
                contact_email TEXT
            )
        ''')
        for i in range(10):
            conn.execute(
                '''INSERT INTO users
                   (chat_id, username, uuid, email, status, lang, platform,
                    support_topic_id, subscription_expiry, limit_ip, quota_gb,
                    last_traffic_update, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (f'user{i}', f'name{i}', f'uuid{i}', f'email{i}@test.com',
                 'demo', 'ru', None, None, None, 1, 5.0, None, None)
            )
        conn.commit()
        conn.close()
        
        repo = AsyncUserRepository(db_path)
        
        # Concurrent reads
        tasks = [repo.get_by_id(f'user{i}') for i in range(10)]
        results = await asyncio.gather(*tasks)
        
        assert len(results) == 10
        assert all(r is not None for r in results)
