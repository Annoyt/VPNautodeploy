"""Pytest configuration and shared fixtures"""

import json
import os
import sqlite3
import tempfile

import pytest
import pytest_asyncio

# Configure pytest-asyncio
pytest_plugins = ('pytest_asyncio',)


@pytest.fixture(autouse=True)
def _hermetic_geoip(monkeypatch, tmp_path_factory):
    """GeoIP never downloads anything and never reads /var/lib in tests.

    bot/services/geoip.py fetches the db-ip mmdb into
    /var/lib/vpn-bot/geoip on the first lookup whenever maxminddb is
    importable, and requirements.txt installs it. A dev box without
    maxminddb answered every lookup with a quiet None; a clean venv (CI)
    failed the /onlines tests with PermissionError('/var/lib/vpn-bot'),
    and where that dir is writable they went to download.db-ip.com
    mid-test (country ~3 MB, city up to ~150 MB). Point the module at a
    directory that never exists and make the download a no-op: every
    lookup is None — what prod answers while its DB is missing. A test
    that needs a country patches bot.services.geoip.lookup itself.
    """
    from bot.services import geoip

    absent = tmp_path_factory.getbasetemp() / 'geoip-absent'
    monkeypatch.setattr(geoip, '_DB_DIR', str(absent))
    for attr in ('_DB_PATH', '_ASN_DB_PATH', '_CITY_DB_PATH'):
        name = os.path.basename(getattr(geoip, attr))
        monkeypatch.setattr(geoip, attr, str(absent / name))
    monkeypatch.setattr(geoip, '_download_db', lambda *a, **kw: False)
    # Fresh readers and caches: a lookup cached as a miss by one test
    # must not answer for a later test that fakes a reader.
    for attr in ('_reader', '_asn_reader', '_city_reader'):
        monkeypatch.setattr(geoip, attr, None)
    for attr in ('_cache', '_asn_cache', '_city_cache'):
        monkeypatch.setattr(geoip, attr, {})


@pytest.fixture(autouse=True)
def _fresh_fallback_membership_cache():
    """FallbackNodeService keeps its panel-membership cache on the class —
    one per process, as in prod, where /sub builds a new service per
    request. A check cached by one test must not answer for the next."""
    from bot.services.fallback_node import FallbackNodeService

    FallbackNodeService._ensure_cache.clear()
    yield
    FallbackNodeService._ensure_cache.clear()


@pytest.fixture
def mock_xui_db():
    """Create temporary X-UI database with test inbound"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    # inbounds table
    c.execute('''
        CREATE TABLE inbounds (
            id INTEGER PRIMARY KEY,
            protocol TEXT,
            port INTEGER,
            settings TEXT
        )
    ''')
    
    # Insert test VLESS inbound
    settings = json.dumps({
        'clients': [
            {
                'id': 'test-client-uuid',
                'email': 'test_client@nekovo.ru',
                'flow': 'xtls-rprx-vision',
                'enable': True
            }
        ]
    })
    c.execute(
        "INSERT INTO inbounds (id, protocol, port, settings) VALUES (?, ?, ?, ?)",
        (1, 'vless', 443, settings)
    )
    
    # client_traffics table
    c.execute('''
        CREATE TABLE client_traffics (
            id INTEGER PRIMARY KEY,
            email TEXT,
            up INTEGER DEFAULT 0,
            down INTEGER DEFAULT 0,
            total INTEGER DEFAULT 0
        )
    ''')
    
    # Insert test traffic data
    c.execute(
        "INSERT INTO client_traffics (email, up, down, total) VALUES (?, ?, ?, ?)",
        ('test_client@nekovo.ru', 1024**3, 2 * 1024**3, 3 * 1024**3)
    )
    
    conn.commit()
    conn.close()
    
    yield db_path
    
    # Cleanup
    os.unlink(db_path)


@pytest.fixture
def mock_bot_db():
    """Create temporary bot database with migrations"""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    from bot.core.database import Database
    db = Database(db_path)
    
    yield db
    
    # Cleanup
    os.unlink(db_path)


@pytest.fixture
def sample_user():
    """Create sample user for tests"""
    from bot.models import User
    
    return User(
        chat_id='123456789',
        username='testuser',
        uuid='test-uuid-123',
        email='test_user@nekovo.ru',
        status='demo',
        lang='ru',
        platform='android',
        support_topic_id=42
    )


@pytest.fixture
def sample_pending_user():
    """Create sample pending user for tests"""
    from bot.models import User
    
    return User(
        chat_id='987654321',
        username='pendinguser',
        status='pending_demo',
        lang='en'
    )


@pytest.fixture
def mock_config():
    """Create mock config for tests"""
    from unittest.mock import Mock
    
    config = Mock()
    config.BOT_TOKEN = 'test_token_123'
    config.DB_PATH = '/tmp/test.db'
    config.XUI_DB_PATH = '/tmp/test_xui.db'
    config.SUPER_ADMIN_ID = '1652899'
    config.FORUM_ENABLED = False
    config.FORUM_GROUP_ID = None
    config.DEMO_TRAFFIC_GB = 5
    config.DEMO_DAYS = 7
    config.ENTRY_NODE_IP = '203.0.113.20'
    config.REALITY_PUBLIC_KEY = 'test_pubkey_123456789'
    config.SNI_VALUE = 'www.microsoft.com'
    config.SID_VALUE = 'test_sid'
    config.MODE = 'PM'
    config.TOPIC_REQUESTS = 15
    config.TOPIC_SUPPORT = 17
    config.TOPIC_PAYMENTS = 16
    config.TOPIC_STATS = 18
    config.TOPIC_SOLVED = 37
    
    def mock_is_admin(user_id):
        return str(user_id) == config.SUPER_ADMIN_ID
    
    config.is_admin = mock_is_admin
    
    return config


@pytest.fixture
def mock_telegram_bot():
    """Create mock Telegram bot for tests"""
    from unittest.mock import Mock
    
    bot = Mock()
    bot.send_message = Mock(return_value={'message_id': 123})
    bot.send_message_to_topic = Mock(return_value={'message_id': 456})
    bot.answer_callback_query = Mock(return_value=True)
    bot.forward_message = Mock(return_value={'message_id': 789})
    bot.create_forum_topic = Mock(return_value=999)
    
    return bot
