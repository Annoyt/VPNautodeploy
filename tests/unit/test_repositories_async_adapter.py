"""Tests for async repository adapters."""

import pytest
import pytest_asyncio
import sqlite3
import tempfile
import os

from bot.core.repositories.async_adapter import (
    _AsyncRepositoryBase,
    AsyncUserRepository,
    AsyncTicketRepository,
    AsyncNodeRepository,
    AsyncMessageMapRepository,
)
from bot.models import User
from bot.models.node import Node, NodeType, NodeStatus


class TestAsyncRepositoryBase:
    """Test base async adapter functionality."""

    @pytest.mark.asyncio
    async def test_run_in_thread_basic(self):
        """Test _run_in_thread executes sync function in thread."""
        base = _AsyncRepositoryBase('/tmp/test.db')
        result = await base._run_in_thread(lambda: 42)
        assert result == 42

    @pytest.mark.asyncio
    async def test_run_in_thread_with_args(self):
        """Test _run_in_thread passes args and kwargs."""
        base = _AsyncRepositoryBase('/tmp/test.db')
        result = await base._run_in_thread(lambda x, y=0: x + y, 10, y=5)
        assert result == 15


class TestAsyncUserRepository:
    """Test AsyncUserRepository."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE users (
                chat_id TEXT PRIMARY KEY,
                username TEXT,
                previous_state TEXT,
                reject_count INTEGER DEFAULT 0,
                uuid TEXT UNIQUE,
                email TEXT UNIQUE,
                status TEXT DEFAULT 'new',
                lang TEXT DEFAULT 'ru',
                platform TEXT,
                support_topic_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                subscription_expiry TEXT,
                limit_ip INTEGER DEFAULT 1,
                quota_gb REAL DEFAULT 5.0,
                last_traffic_update TEXT,
                xui_synced INTEGER DEFAULT 0,
                next_protocol_idx INTEGER DEFAULT 0,
                last_country TEXT,
                last_asn TEXT,
                last_city TEXT,
                last_lat REAL,
                last_lon REAL
            )
        ''')
        c.execute('''
            INSERT INTO users (chat_id, username, previous_state, reject_count, uuid, email, status, lang, limit_ip, quota_gb)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', ('123', 'testuser', None, 0, 'uuid-123', 'test@example.com', 'demo', 'en', 1, 5.0))
        conn.commit()
        conn.close()

        yield db_path
        os.unlink(db_path)

    @pytest.fixture
    def repository(self, temp_db):
        return AsyncUserRepository(temp_db)

    @pytest.mark.asyncio
    async def test_get_by_id_existing(self, repository):
        user = await repository.get_by_id('123')
        assert user is not None
        assert user.username == 'testuser'

    @pytest.mark.asyncio
    async def test_get_by_id_nonexistent(self, repository):
        user = await repository.get_by_id('999')
        assert user is None

    @pytest.mark.asyncio
    async def test_get_by_username(self, repository):
        user = await repository.get_by_username('testuser')
        assert user is not None
        assert user.chat_id == '123'

    @pytest.mark.asyncio
    async def test_get_stats(self, repository):
        stats = await repository.get_stats()
        assert 'total' in stats
        assert stats['total'] == 1

    @pytest.mark.asyncio
    async def test_save_and_update_status(self, repository):
        user = User(
            chat_id='456', username='newuser', uuid='uuid-456',
            email='new@example.com', status='pending_demo'
        )
        result = await repository.save(user)
        assert result is True

        result = await repository.update_status('456', 'demo')
        assert result is True

        saved = await repository.get_by_id('456')
        assert saved.status == 'demo'


class TestAsyncTicketRepository:
    """Test AsyncTicketRepository."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER UNIQUE,
                chat_id TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT
            )
        ''')
        conn.commit()
        conn.close()

        yield db_path
        os.unlink(db_path)

    @pytest.fixture
    def repository(self, temp_db):
        return AsyncTicketRepository(temp_db)

    @pytest.mark.asyncio
    async def test_create_and_get(self, repository):
        result = await repository.create('123', 42, 'open')
        assert result is True

        ticket = await repository.get_by_topic_id(42)
        assert ticket['chat_id'] == '123'

    @pytest.mark.asyncio
    async def test_update_status(self, repository):
        await repository.create('123', 42, 'open')
        result = await repository.update_status(42, 'closed')
        assert result is True

        ticket = await repository.get_by_topic_id(42)
        assert ticket['status'] == 'closed'

    @pytest.mark.asyncio
    async def test_close_ticket(self, repository):
        await repository.create('123', 42, 'open')
        result = await repository.close_ticket(42)
        assert result is True

        ticket = await repository.get_by_topic_id(42)
        assert ticket['status'] == 'closed'

    @pytest.mark.asyncio
    async def test_get_by_chat_id(self, repository):
        await repository.create('123', 1, 'open')
        await repository.create('123', 2, 'open')
        tickets = await repository.get_by_chat_id('123')
        assert len(tickets) == 2


class TestAsyncNodeRepository:
    """Test AsyncNodeRepository."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                type TEXT,
                host TEXT,
                api_port INTEGER DEFAULT 8080,
                vpn_port INTEGER DEFAULT 443,
                base_path TEXT,
                api_username TEXT,
                api_password TEXT,
                public_key TEXT,
                sni TEXT,
                sid TEXT,
                region TEXT,
                city TEXT,
                status TEXT DEFAULT 'active',
                is_primary INTEGER DEFAULT 0,
                weight INTEGER DEFAULT 100,
                max_clients INTEGER DEFAULT 100,
                current_clients INTEGER DEFAULT 0
            )
        ''')
        c.execute('''
            INSERT INTO nodes (name, type, host, status)
            VALUES (?, ?, ?, ?)
        ''', ('node1', 'exit', '10.0.0.1', 'active'))
        conn.commit()
        conn.close()

        yield db_path
        os.unlink(db_path)

    @pytest.fixture
    def repository(self, temp_db):
        return AsyncNodeRepository(temp_db)

    @pytest.mark.asyncio
    async def test_get_by_id_existing(self, repository):
        node = await repository.get_by_id(1)
        assert node is not None
        assert node.name == 'node1'

    @pytest.mark.asyncio
    async def test_get_by_name(self, repository):
        node = await repository.get_by_name('node1')
        assert node is not None
        assert node.host == '10.0.0.1'

    @pytest.mark.asyncio
    async def test_create_and_delete(self, repository):
        node = Node(
            id=None, name='newnode', type=NodeType.EXIT, host='10.0.0.2',
            api_port=8080, vpn_port=443, base_path='/api',
            api_username='admin', api_password='pass', public_key='pk',
            sni='sni', sid='sid', region='EU', city='Berlin',
            is_primary=False, weight=100, max_clients=100, current_clients=0,
            status=NodeStatus.ACTIVE
        )
        node_id = await repository.create(node)
        assert node_id > 0

        fetched = await repository.get_by_id(node_id)
        assert fetched.name == 'newnode'

        result = await repository.delete(node_id)
        assert result is True
        assert await repository.get_by_id(node_id) is None

    @pytest.mark.asyncio
    async def test_update_status(self, repository):
        result = await repository.update_status(1, 'maintenance')
        assert result is True

        node = await repository.get_by_id(1)
        assert node.status == NodeStatus.MAINTENANCE


class TestAsyncMessageMapRepository:
    """Test AsyncMessageMapRepository."""

    @pytest.fixture
    def temp_db(self):
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name

        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE message_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_msg_id INTEGER,
                user_chat_id TEXT,
                user_msg_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(admin_msg_id, user_chat_id)
            )
        ''')
        conn.commit()
        conn.close()

        yield db_path
        os.unlink(db_path)

    @pytest.fixture
    def repository(self, temp_db):
        return AsyncMessageMapRepository(temp_db)

    @pytest.mark.asyncio
    async def test_create_and_get(self, repository):
        result = await repository.create(100, '123', 50)
        assert result is True

        mapping = await repository.get_by_admin_msg(100)
        assert mapping['user_chat_id'] == '123'

    @pytest.mark.asyncio
    async def test_delete(self, repository):
        await repository.create(100, '123', 50)
        result = await repository.delete(100)
        assert result is True
        assert await repository.get_by_admin_msg(100) is None
