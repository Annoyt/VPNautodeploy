"""Database operations - Facade with repository delegation.

DEPRECATED: This module is kept for backward compatibility.
New code should use repositories directly from bot.core.repositories.

Migration Guide:
    Old: db.get_user(chat_id)
    New: UserRepository(db_path).get_by_id(chat_id)
    
    Old: db.get_pending_users()
    New: UserRepository(db_path).get_pending()
    
    Old: db.create_node(node_data)
    New: NodeRepository(db_path).create(Node(**node_data))
"""

import functools
import logging
import re
import sqlite3
import warnings
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from bot.models import User

# Import repositories for delegation
from bot.core.repositories import (
    UserRepository, 
    TicketRepository, 
    NodeRepository,
    MessageMapRepository
)

logger = logging.getLogger(__name__)


def db_transaction(method):
    """Decorator for database transactions (legacy, kept for compatibility)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        conn = None
        try:
            conn = self._connect()
            result = method(self, conn.cursor(), *args, **kwargs)
            conn.commit()
            return result
        except sqlite3.Error as e:
            if conn:
                conn.rollback()
            logger.error(f"DB error: {e}")
            return False
        finally:
            if conn:
                conn.close()
    return wrapper


class Database:
    """SQLite database facade delegating to repositories.
    
    DEPRECATED: This class is a compatibility facade. 
    It delegates all operations to specialized repositories.
    
    For new code, use repositories directly:
        from bot.core.repositories import UserRepository, TicketRepository, NodeRepository
    """
    
    # Method mapping: Database method -> (repository_name, repository_method)
    _METHOD_MAP = {
        # User operations
        'get_user': ('_users', 'get_by_id'),
        'get_user_by_username': ('_users', 'get_by_username'),
        'get_user_by_topic_id': ('_users', 'get_by_topic_id'),
        'get_all_users': ('_users', 'get_all'),
        'get_pending_users': ('_users', 'get_pending'),
        'get_users_by_status': ('_users', 'get_by_status'),
        'save_user': ('_users', 'save'),
        'update_status': ('_users', 'update_status'),
        
        # Node operations
        'get_node': ('_nodes', 'get_by_id'),
        'get_nodes': ('_nodes', 'get_all'),
        'get_nodes_by_status': ('_nodes', 'get_by_status'),
        'create_node': ('_nodes', 'create'),
        'update_node_status': ('_nodes', 'update_status'),
        'update_node_clients_count': ('_nodes', 'update_client_count'),
        
        # Ticket operations
        'create_ticket': ('_tickets', 'create'),
        'get_ticket_by_topic_id': ('_tickets', 'get_by_topic_id'),
        'get_tickets_by_chat_id': ('_tickets', 'get_by_chat_id'),
        'update_ticket_status': ('_tickets', 'update_status'),
        'close_ticket': ('_tickets', 'close_ticket'),
        'log_ticket_message': ('_tickets', 'log_ticket_message'),
        'get_ticket_messages': ('_tickets', 'get_ticket_messages'),
        'cleanup_old_ticket_messages': ('_tickets', 'cleanup_old_messages'),
        
        # Message map operations
        'log_message_map': ('_message_map', 'log_message_map'),
        'get_mapped_user_message': ('_message_map', 'get_mapped_user_message'),
    }
    
    def __init__(self, db_path: str):
        """Initialize database facade with repositories.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._ensure_dir()
        self.init_db()
        # Run in their own transactions so a failure in init_db (e.g.
        # a legacy table on prod that diverges from the code-shipped
        # schema and trips an index migration) doesn't roll these
        # back — they're load-bearing for app_settings + DPI metrics.
        self._init_app_settings()
        self._init_dpi_metrics()
        self._init_dpi_reports()
        self._init_alert_history()
        self._init_hy2_auth_log()
        self._init_user_failure_reports()
        self._init_sub_fetches()
        self._init_outbound_health()

        # Initialize repositories for delegation
        self._users = UserRepository(db_path)
        self._tickets = TicketRepository(db_path)
        self._nodes = NodeRepository(db_path)
        self._message_map = MessageMapRepository(db_path)
        
        # Track which methods are being used (for migration tracking)
        self._used_methods = set()
    
    def _ensure_dir(self):
        """Ensure database directory exists."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
    
    def _connect(self):
        """Create database connection with row factory and WAL mode."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn
    
    def _get_connection(self):
        """Get database connection (alias for _connect)."""
        return self._connect()
    
    def __getattr__(self, name: str):
        """Delegate method calls to appropriate repository.
        
        This implements the Facade pattern, routing method calls to
        the appropriate specialized repository.
        
        Args:
            name: Method name being accessed
            
        Returns:
            Bound method from repository
            
        Raises:
            AttributeError: If method not found in any repository
        """
        # Check if this is a mapped method
        if name in self._METHOD_MAP:
            repo_name, method_name = self._METHOD_MAP[name]
            repo = getattr(self, repo_name)
            method = getattr(repo, method_name)
            
            # Track usage for migration purposes
            self._used_methods.add(name)
            
            # Warn about deprecation on first use
            if len(self._used_methods) <= 5:  # Limit warnings
                warnings.warn(
                    f"Database.{name} is deprecated. "
                    f"Use {repo.__class__.__name__}.{method_name} directly.",
                    DeprecationWarning,
                    stacklevel=2
                )
            
            return method
        
        # If not mapped, raise AttributeError
        raise AttributeError(
            f"'{self.__class__.__name__}' has no attribute '{name}'. "
            f"Method not found in delegation mapping. "
            f"Available methods: {list(self._METHOD_MAP.keys())}"
        )
    
    def create_tables(self) -> bool:
        """Create database tables (backward compatibility alias for init_db)."""
        return self.init_db()
    
    def reset_user_data(self, chat_id: str) -> bool:
        """Reset user VPN data (legacy backward compatibility)."""
        import warnings
        warnings.warn(
            "Database.reset_user_data is deprecated. Use UserRepository.save directly.",
            DeprecationWarning,
            stacklevel=2
        )
        user = self.get_user(chat_id)
        if not user:
            return False
        user.uuid = None
        user.email = None
        user.platform = None
        # /reset is supposed to be a clean slate, including the demo
        # request quota — otherwise a user who was rejected
        # MAX_REJECT_RETRIES times stays locked out forever.
        user.reject_count = 0
        return self.save_user(user)
    
    @db_transaction
    def init_db(self, c) -> bool:
        """Initialize database tables."""
        # Users table
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
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
                last_lon REAL,
                contact_email TEXT
            )
        ''')

        # Idempotent migration for existing databases
        for ddl in (
            "ALTER TABLE users ADD COLUMN previous_state TEXT",
            "ALTER TABLE users ADD COLUMN reject_count INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN next_protocol_idx INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN last_country TEXT",
            "ALTER TABLE users ADD COLUMN last_asn TEXT",
            "ALTER TABLE users ADD COLUMN last_city TEXT",
            "ALTER TABLE users ADD COLUMN last_lat REAL",
            "ALTER TABLE users ADD COLUMN last_lon REAL",
            "ALTER TABLE users ADD COLUMN contact_email TEXT",
        ):
            try:
                c.execute(ddl)
            except sqlite3.OperationalError:
                pass
        
        # Data migrations: convert false BANNED to REJECTED, old ACTIVE to DEMO
        c.execute("UPDATE users SET status='rejected', reject_count=1 WHERE status='banned' AND (username IS NULL OR username='')")
        c.execute("UPDATE users SET status='demo' WHERE status='active'")
        
        # Create indexes for users
        c.execute('CREATE INDEX IF NOT EXISTS idx_users_status ON users(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_users_topic ON users(support_topic_id)')
        
        # Tickets table
        c.execute('''
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER UNIQUE,
                chat_id TEXT,
                status TEXT DEFAULT 'open',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id)
            )
        ''')
        
        # Ticket messages table (for forum thread history)
        # Prod columns are message_text + timestamp (see TicketRepository).
        c.execute('''
            CREATE TABLE IF NOT EXISTS ticket_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER,
                sender_type TEXT,
                sender_name TEXT,
                message_text TEXT,
                has_media BOOLEAN DEFAULT 0,
                media_file_id TEXT,
                message_id INTEGER,
                timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (topic_id) REFERENCES tickets(topic_id)
            )
        ''')
        
        # Message map table
        c.execute('''
            CREATE TABLE IF NOT EXISTS message_map (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_msg_id INTEGER,
                user_chat_id TEXT,
                user_msg_id INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(admin_msg_id, user_chat_id)
            )
        ''')
        
        # Nodes table
        c.execute('''
            CREATE TABLE IF NOT EXISTS nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
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
                status TEXT DEFAULT 'active',
                is_primary INTEGER DEFAULT 0,
                weight INTEGER DEFAULT 100,
                max_clients INTEGER DEFAULT 100,
                current_clients INTEGER DEFAULT 0,
                health_check_url TEXT,
                last_health_check TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Node assignments table
        c.execute('''
            CREATE TABLE IF NOT EXISTS node_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                exit_node_id INTEGER,
                entry_node_id INTEGER,
                assigned_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id),
                FOREIGN KEY (exit_node_id) REFERENCES nodes(id),
                FOREIGN KEY (entry_node_id) REFERENCES nodes(id),
                UNIQUE(chat_id, exit_node_id)
            )
        ''')
        
        # Node failover log
        c.execute('''
            CREATE TABLE IF NOT EXISTS node_failover_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                from_node_id INTEGER,
                to_node_id INTEGER,
                reason TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Traffic log table
        c.execute('''
            CREATE TABLE IF NOT EXISTS traffic_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT,
                upload_bytes INTEGER DEFAULT 0,
                download_bytes INTEGER DEFAULT 0,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Static profiles table
        c.execute('''
            CREATE TABLE IF NOT EXISTS static_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE,
                vless_url TEXT,
                email TEXT,
                max_users INTEGER DEFAULT 10,
                current_users INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Subscriptions table
        # Prod columns are started_at + expires_at (see database.py queries).
        c.execute('''
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                plan_type TEXT DEFAULT 'demo',
                started_at TEXT,
                expires_at TEXT,
                is_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (chat_id) REFERENCES users(chat_id)
            )
        ''')
        
        # Notifications log table
        c.execute('''
            CREATE TABLE IF NOT EXISTS notification_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                notification_type TEXT,
                sent_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Per-client traffic snapshots. NotificationService inserts one
        # row per email every 30 minutes from the live x-ui
        # client_traffics table; the dashboard derives per-period deltas
        # on read (the absolute counters monotonically grow until a
        # client is reset, then jump back to 0 — we detect that and zero
        # the delta instead of going negative).
        c.execute('''
            CREATE TABLE IF NOT EXISTS traffic_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                upload_bytes INTEGER DEFAULT 0,
                download_bytes INTEGER DEFAULT 0,
                recorded_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute(
            'CREATE INDEX IF NOT EXISTS idx_traffic_history_email_ts '
            'ON traffic_history(email, recorded_at)'
        )
        
        # Admin actions log
        c.execute('''
            CREATE TABLE IF NOT EXISTS admin_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id TEXT,
                action TEXT,
                target_id TEXT,
                details TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Legacy xui_synced table (for backward compatibility)
        c.execute('''
            CREATE TABLE IF NOT EXISTS xui_synced (
                email TEXT PRIMARY KEY,
                synced_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # XUI API config table
        c.execute('''
            CREATE TABLE IF NOT EXISTS xui_api_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                base_url TEXT DEFAULT 'http://127.0.0.1:2053',
                username TEXT DEFAULT 'admin',
                password TEXT DEFAULT 'admin',
                api_path TEXT DEFAULT '/this_is_fine/panel/api/inbounds',
                use_api INTEGER DEFAULT 0,
                inbound_id INTEGER DEFAULT 1
            )
        ''')
        
        # Insert default XUI API config if not exists
        c.execute('''
            INSERT OR IGNORE INTO xui_api_config (id, base_url, username, password, use_api, inbound_id)
            VALUES (1, 'http://127.0.0.1:2053', 'admin', 'admin', 0, 1)
        ''')

        # app_settings is created in _init_app_settings (own tx) to
        # survive other migration failures inside this method.
        
        # === Schema migrations (idempotent) ===
        try:
            c.execute("ALTER TABLE users ADD COLUMN previous_state TEXT")
        except Exception:
            pass  # Column already exists
        try:
            c.execute("ALTER TABLE users ADD COLUMN reject_count INTEGER DEFAULT 0")
        except Exception:
            pass  # Column already exists
        
        # === Data migration: false BANNED → REJECTED ===
        # Users without username that were banned are likely rejected bots
        c.execute("""
            UPDATE users 
            SET status = 'rejected', reject_count = 1
            WHERE status = 'banned'
            AND (username IS NULL OR username = '')
        """)
        if c.rowcount > 0:
            logger.info(f"Migrated {c.rowcount} false-banned users to REJECTED")
        
        # === Data migration: ACTIVE → DEMO ===
        c.execute("UPDATE users SET status = 'demo' WHERE status = 'active'")
        if c.rowcount > 0:
            logger.info(f"Migrated {c.rowcount} active users to DEMO")
        
        logger.info("Database initialized successfully")
        return True
    
    # Legacy methods kept for backward compatibility
    # These will be removed in future versions
    
    def get_stats(self) -> dict:
        """Get database statistics (legacy, delegates to UserRepository)."""
        warnings.warn(
            "Database.get_stats is deprecated. Use UserRepository.get_stats()",
            DeprecationWarning,
            stacklevel=2
        )
        return self._users.get_stats()
    
    def _init_app_settings(self) -> None:
        """Ensure app_settings exists. Standalone tx so any other
        migration failure inside init_db can't roll this back.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS app_settings ("
                    "key TEXT PRIMARY KEY, "
                    "value TEXT NOT NULL, "
                    "updated_at TEXT DEFAULT CURRENT_TIMESTAMP)"
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"_init_app_settings failed: {e}")

    def _init_dpi_reports(self) -> None:
        """Aggregated DPI summaries (daily/weekly/monthly) for the
        retrospective dashboard view. Kept much longer than raw metrics
        (365 days vs 30) since each row is one JSON snapshot per period,
        not 12 rows per hour.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS dpi_reports ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "kind TEXT NOT NULL, "                  # 'daily' / 'weekly' / 'monthly'
                    "period_start TEXT NOT NULL, "
                    "period_end TEXT NOT NULL, "
                    "snapshot_json TEXT NOT NULL, "         # full rollup
                    "kimi_analysis TEXT, "                  # nullable
                    "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dpi_reports_kind_start "
                    "ON dpi_reports(kind, period_start)"
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"_init_dpi_reports failed: {e}")

    def _init_hy2_auth_log(self) -> None:
        """Audit log of every Hysteria2 auth callback.

        Used by the adoption widget — we count how many distinct users
        actually use Hy2 vs VLESS, broken down by country/ASN of the
        source IP. Retention is 7 days (the widget only cares about
        recent trend; full history is in dpi_metrics).

        ``decision`` is 'allow' or 'deny'. Deny rows are useful too
        — a sudden spike of denies = either a banned user trying or
        a probe with a guessed UUID.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS hy2_auth_log ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "chat_id TEXT, "
                    "decision TEXT NOT NULL, "
                    "addr_ip TEXT, "
                    "country TEXT, "
                    "asn TEXT, "
                    "as_org TEXT)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_hy2_auth_log_ts "
                    "ON hy2_auth_log(ts)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_hy2_auth_log_chat_ts "
                    "ON hy2_auth_log(chat_id, ts)"
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"_init_hy2_auth_log failed: {e}")

    def _init_user_failure_reports(self) -> None:
        """User-reported connectivity failures. Single source of
        ground-truth for per-(country, ASN) blocking heatmaps.

        Written when the user taps the «🆘 не подключается» button on
        any key message. Each row captures *everything we knew about
        the user at the moment they complained* so support can triage
        without round-tripping the user for diagnostics, and the
        analytics job can correlate failures with observed traffic.

        Fields:
        - ``chat_id`` — user's TG id, ties row to ``users``.
        - ``country`` / ``asn`` — snapshotted ``users.last_country`` /
          ``users.last_asn`` so deletes / re-geo don't lose history.
        - ``last_sub_fetch_ts`` — last successful /sub refresh; if it's
          older than a few hours when complaint comes in, the user is
          on a stale config and the urltest auto-failover hasn't fired.
        - ``last_traffic_ts`` — last byte through any inbound, from
          xui_synced / traffic_log.
        - ``acked_at`` — set when operator acks via dashboard.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS user_failure_reports ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "chat_id TEXT, "
                    "country TEXT, "
                    "asn TEXT, "
                    "last_sub_fetch_ts TEXT, "
                    "last_traffic_ts TEXT, "
                    "acked_at TEXT, "
                    "ack_note TEXT)"
                )
                # city / lat / lon snapshot — same justification as
                # country/asn: the user might move tomorrow, but the
                # historical incident happened from this location.
                # Idempotent ALTERs so prod upgrades-in-place.
                for ddl in (
                    "ALTER TABLE user_failure_reports ADD COLUMN city TEXT",
                    "ALTER TABLE user_failure_reports ADD COLUMN lat REAL",
                    "ALTER TABLE user_failure_reports ADD COLUMN lon REAL",
                    "ALTER TABLE user_failure_reports ADD COLUMN target_domain TEXT",
                ):
                    try:
                        conn.execute(ddl)
                    except sqlite3.OperationalError:
                        pass
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ufr_ts ON user_failure_reports(ts)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ufr_chat_ts "
                    "ON user_failure_reports(chat_id, ts)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ufr_country_asn_ts "
                    "ON user_failure_reports(country, asn, ts)"
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"_init_user_failure_reports failed: {e}")

    def _init_sub_fetches(self) -> None:
        """Subscription refresh log. Every successful ``/sub/<token>``
        hit appends one row with (country, asn, ts) — gives us a
        per-(country, ASN) liveness signal for users who picked the
        subscription path (i.e. don't show up in xray's access.log
        unless they actively connect).

        Why a dedicated table instead of just reading Caddy access logs:
        Caddy logs are JSONL in /var/log/caddy/ on the host, neither
        easily joinable in SQL nor under the bot's retention control.
        A small in-bot table keeps everything in one place and lets
        the dashboard's failure heatmap correlate fetches with reports.

        Kept narrow on purpose — no IP, no User-Agent. ASN is the only
        sub-RU axis we have anyway, and not storing IPs avoids becoming
        a forensic surface if the host is later compromised.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS sub_fetches ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "chat_id TEXT, "
                    "country TEXT, "
                    "asn TEXT)"
                )
                for ddl in (
                    "ALTER TABLE sub_fetches ADD COLUMN city TEXT",
                    "ALTER TABLE sub_fetches ADD COLUMN lat REAL",
                    "ALTER TABLE sub_fetches ADD COLUMN lon REAL",
                ):
                    try:
                        conn.execute(ddl)
                    except sqlite3.OperationalError:
                        pass
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sub_fetches_ts "
                    "ON sub_fetches(ts)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sub_fetches_country_asn_ts "
                    "ON sub_fetches(country, asn, ts)"
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"_init_sub_fetches failed: {e}")

    def _init_alert_history(self) -> None:
        """Persistent alert history for the dashboard. Replaces the
        Telegram-only firehose — operator can now scroll/filter past
        alerts and read Kimi follow-ups without spamming TOPIC_AI.

        - ``key`` matches the AlertManager key (e.g. dpi_short:RU:AS8402)
          so we can join with the in-memory tracker if needed.
        - ``kimi_analysis`` is filled lazily after the Kimi bridge call
          returns; ``NULL`` means "not yet analysed" or "not eligible".
        - ``acked_at`` is set when the operator clicks ✅ in the
          dashboard; old alerts before that just stay un-acked.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS alert_history ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "key TEXT NOT NULL, "
                    "severity TEXT NOT NULL, "
                    "title TEXT NOT NULL, "
                    "detail TEXT, "
                    "fired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "kimi_analysis TEXT, "
                    "kimi_at TEXT, "
                    "acked_at TEXT, "
                    "acked_by TEXT)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_alert_history_fired_at "
                    "ON alert_history(fired_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_alert_history_key "
                    "ON alert_history(key, fired_at)"
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"_init_alert_history failed: {e}")

    def _init_dpi_metrics(self) -> None:
        """Per-(country, ASN) rollup of session-quality signals, written
        every 5 minutes by NotificationService._dpi_collect_sync.

        - ``country`` / ``asn`` / ``as_org`` come from GeoIP lookup of
          the connect source.
        - ``conn_count`` / ``avg_session_sec`` / ``short_session_count``
          are derived from Xray access.log + live ``ss -tin`` deltas.
        - ``rst_count`` / ``handshake_fail_count`` get filled by Phase
          B once we wire ``error.log`` + ``nstat``; until then they
          stay 0.
        - ``inbound_tag`` is the Xray inbound the connection landed on
          (e.g. 'reality-443', 'hy2-8400'). For now there's only one,
          so it's a constant — but we want the column ready for the
          multi-inbound rollout.

        Index is on (snapshot_at) for time-window queries and on
        (country, asn, snapshot_at) for the heatmap drill-down.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS dpi_metrics ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "snapshot_at TEXT NOT NULL, "
                    "country TEXT, "
                    "asn TEXT, "
                    "as_org TEXT, "
                    "inbound_tag TEXT, "
                    "conn_count INTEGER NOT NULL DEFAULT 0, "
                    "avg_session_sec REAL, "
                    "short_session_count INTEGER NOT NULL DEFAULT 0, "
                    "rst_count INTEGER NOT NULL DEFAULT 0, "
                    "handshake_fail_count INTEGER NOT NULL DEFAULT 0)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dpi_metrics_snap "
                    "ON dpi_metrics(snapshot_at)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_dpi_metrics_country_asn "
                    "ON dpi_metrics(country, asn, snapshot_at)"
                )
                # Phase-B columns — JSON blobs with per-snapshot detail.
                # ALTER ADD is idempotent only because we wrap in try/except;
                # SQLite has no "ADD COLUMN IF NOT EXISTS".
                for ddl in (
                    "ALTER TABLE dpi_metrics ADD COLUMN probe_ips_json TEXT",
                    "ALTER TABLE dpi_metrics ADD COLUMN reason_buckets_json TEXT",
                ):
                    try:
                        conn.execute(ddl)
                    except sqlite3.OperationalError:
                        pass  # column already exists
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"_init_dpi_metrics failed: {e}")

    def _init_outbound_health(self) -> None:
        """Active health checks for popular domains through each outbound.

        Written by the background scheduler every 15 minutes. Tracks
        which services are reachable from which locations — helps us
        distinguish "VPN completely dead" from "only RU sites blocked".

        ``outbound_tag`` — cascade protocol name (reality, hy2, ws, xhttp, stls).
        ``target_domain`` — domain we checked (vk.com, yandex.ru, etc.).
        ``status`` — 'ok', 'timeout', 'error', 'blocked'.
        ``latency_ms`` — round-trip time if successful, NULL if not.
        """
        try:
            with self._connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS outbound_health ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "ts TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "outbound_tag TEXT NOT NULL, "
                    "target_domain TEXT NOT NULL, "
                    "status TEXT NOT NULL, "
                    "latency_ms INTEGER, "
                    "error_msg TEXT)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_outbound_health_ts_tag "
                    "ON outbound_health(ts, outbound_tag)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_outbound_health_status "
                    "ON outbound_health(status, ts)"
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error(f"_init_outbound_health failed: {e}")

    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read a tunable from app_settings. Returns default if missing."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT value FROM app_settings WHERE key = ?", (key,)
                ).fetchone()
                return row["value"] if row else default
        except sqlite3.Error as e:
            logger.error(f"get_setting({key!r}) failed: {e}")
            return default

    def set_setting(self, key: str, value: str) -> bool:
        """Upsert a tunable in app_settings."""
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO app_settings(key, value, updated_at) "
                    "VALUES(?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(key) DO UPDATE SET "
                    "value = excluded.value, updated_at = CURRENT_TIMESTAMP",
                    (key, value),
                )
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error(f"set_setting({key!r}) failed: {e}")
            return False

    def log_admin_action(self, admin_id: str, action: str, target_id: str = None, details: str = None) -> bool:
        """Log admin action (legacy, kept for compatibility)."""
        try:
            with self._connect() as conn:
                conn.execute('''
                    INSERT INTO admin_actions (admin_id, action, target_id, details)
                    VALUES (?, ?, ?, ?)
                ''', (admin_id, action, target_id, details))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error(f"Failed to log admin action: {e}")
            return False
    
    def get_expiring_subscriptions(self, hours: int = 24) -> List[dict]:
        """Get subscriptions expiring within specified hours (legacy)."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('''
                    SELECT s.*, u.username, u.email 
                    FROM subscriptions s
                    JOIN users u ON s.chat_id = u.chat_id
                    WHERE s.is_active = 1
                    AND datetime(s.expires_at) <= datetime('now', '+' || ? || ' hours')
                    AND datetime(s.expires_at) > datetime('now')
                ''', (hours,))
                return [dict(row) for row in c.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Failed to get expiring subscriptions: {e}")
            return []
    
    def mark_notified(self, chat_id: str, notification_type: str) -> bool:
        """Mark notification as sent (legacy)."""
        try:
            with self._connect() as conn:
                conn.execute('''
                    INSERT INTO notification_log (chat_id, notification_type)
                    VALUES (?, ?)
                ''', (chat_id, notification_type))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error(f"Failed to mark notification: {e}")
            return False
    
    def was_notified(self, chat_id: str, notification_type: str, hours: int = 24) -> bool:
        """Check if notification was sent recently (legacy)."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('''
                    SELECT 1 FROM notification_log
                    WHERE chat_id = ?
                    AND notification_type = ?
                    AND datetime(sent_at) > datetime('now', '-' || ? || ' hours')
                    LIMIT 1
                ''', (chat_id, notification_type, hours))
                return c.fetchone() is not None
        except sqlite3.Error as e:
            logger.error(f"Failed to check notification status: {e}")
            return False
    
    def get_traffic(self, email: str) -> Optional[dict]:
        """Get traffic stats for email (legacy)."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('''
                    SELECT SUM(upload_bytes) as total_up, SUM(download_bytes) as total_down
                    FROM traffic_log
                    WHERE email = ?
                ''', (email,))
                row = c.fetchone()
                if row and (row['total_up'] or row['total_down']):
                    return {'upload': row['total_up'] or 0, 'download': row['total_down'] or 0}
                return None
        except sqlite3.Error as e:
            logger.error(f"Failed to get traffic: {e}")
            return None
    
    def record_traffic(self, email: str, upload: int, download: int) -> bool:
        """Record traffic stats (legacy)."""
        try:
            with self._connect() as conn:
                conn.execute('''
                    INSERT INTO traffic_log (email, upload_bytes, download_bytes)
                    VALUES (?, ?, ?)
                ''', (email, upload, download))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error(f"Failed to record traffic: {e}")
            return False
    
    def mark_xui_synced(self, email: str) -> bool:
        """Mark user as synced with X-UI (legacy)."""
        try:
            with self._connect() as conn:
                conn.execute('''
                    INSERT OR REPLACE INTO xui_synced (email, synced_at)
                    VALUES (?, CURRENT_TIMESTAMP)
                ''', (email,))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error(f"Failed to mark XUI synced: {e}")
            return False
    
    def is_xui_synced(self, email: str) -> bool:
        """Check if user is synced with X-UI (legacy)."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('''
                    SELECT 1 FROM xui_synced WHERE email = ?
                ''', (email,))
                return c.fetchone() is not None
        except sqlite3.Error as e:
            logger.error(f"Failed to check XUI sync status: {e}")
            return False
    
    def get_xui_api_config(self) -> dict:
        """Get XUI API configuration (legacy)."""
        defaults = {
            'base_url': 'http://127.0.0.1:2053',
            'username': 'admin',
            'password': 'admin',
            'api_path': '/this_is_fine/panel/api/inbounds',
            'use_api': False,
            'inbound_id': 1
        }
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('SELECT * FROM xui_api_config WHERE id = 1')
                row = c.fetchone()
                if row:
                    def _get(col, default):
                        try:
                            return row[col]
                        except (IndexError, KeyError):
                            return default
                    return {
                        'base_url': _get('base_url', defaults['base_url']),
                        'username': _get('username', defaults['username']),
                        'password': _get('password', defaults['password']),
                        'api_path': _get('api_path', defaults['api_path']),
                        'use_api': bool(_get('use_api', defaults['use_api'])),
                        'inbound_id': _get('inbound_id', defaults['inbound_id'])
                    }
                return defaults
        except sqlite3.Error as e:
            logger.error(f"Failed to get XUI API config: {e}")
            return defaults
    
    def create_static_profile(self, name: str, vless_url: str, email: str = None, max_users: int = 10) -> bool:
        """Create static profile (legacy)."""
        try:
            with self._connect() as conn:
                conn.execute('''
                    INSERT INTO static_profiles (name, vless_url, email, max_users, current_users, is_active)
                    VALUES (?, ?, ?, ?, 0, 1)
                ''', (name, vless_url, email, max_users))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error(f"Failed to create static profile: {e}")
            return False
    
    def get_static_profile(self, name: str) -> Optional[dict]:
        """Get static profile by name (legacy)."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('SELECT * FROM static_profiles WHERE name = ?', (name,))
                row = c.fetchone()
                if row:
                    return {
                        'name': row['name'],
                        'vless_url': row['vless_url'],
                        'email': row['email'],
                        'max_users': row['max_users'],
                        'current_users': row['current_users'],
                        'enabled': bool(row['is_active'])
                    }
                return None
        except sqlite3.Error as e:
            logger.error(f"Failed to get static profile: {e}")
            return None
    
    def create_subscription(self, chat_id: str, plan_type: str = 'demo', expiry_days: int = 7, traffic_gb: float = 5.0, end_date: str = None) -> bool:
        """Create subscription for user (legacy)."""
        from datetime import datetime, timedelta
        try:
            start_date = datetime.now().isoformat()
            if end_date is None:
                end_date = (datetime.now() + timedelta(days=expiry_days)).isoformat()
            with self._connect() as conn:
                # Prod table column names diverged from the original CREATE TABLE;
                # the schema there uses `started_at`/`expires_at`.
                conn.execute('''
                    INSERT INTO subscriptions (chat_id, plan_type, started_at, expires_at, is_active)
                    VALUES (?, ?, ?, ?, 1)
                ''', (chat_id, plan_type, start_date, end_date))
                conn.commit()
                return True
        except sqlite3.Error as e:
            logger.error(f"Failed to create subscription: {e}")
            return False
    
    def get_subscription(self, chat_id: str) -> Optional[dict]:
        """Get active subscription for user (legacy)."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('''
                    SELECT * FROM subscriptions 
                    WHERE chat_id = ? AND is_active = 1
                    ORDER BY id DESC LIMIT 1
                ''', (chat_id,))
                row = c.fetchone()
                if row:
                    return {
                        'chat_id': row['chat_id'],
                        'plan_type': row['plan_type'],
                        'start_date': row['started_at'],
                        'end_date': row['expires_at'],
                        'is_active': bool(row['is_active']),
                        'traffic_limit_gb': 5.0
                    }
                return None
        except sqlite3.Error as e:
            logger.error(f"Failed to get subscription: {e}")
            return None
    
    def get_primary_node(self, node_type: str) -> Optional[dict]:
        """Get primary node by type (legacy)."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('''
                    SELECT * FROM nodes 
                    WHERE type = ? AND is_primary = 1 AND status = 'active'
                    LIMIT 1
                ''', (node_type,))
                row = c.fetchone()
                return self._row_to_node(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"Failed to get primary node: {e}")
            return None
    
    def get_users_on_node(self, node_id: int) -> List[str]:
        """Get users assigned to node (legacy)."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('''
                    SELECT chat_id FROM node_assignments
                    WHERE exit_node_id = ? OR entry_node_id = ?
                ''', (node_id, node_id))
                return [row['chat_id'] for row in c.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Failed to get users on node: {e}")
            return []
    
    def _row_to_node(self, row) -> dict:
        """Convert database row to node dictionary.
        
        Supports both tuple rows and sqlite3.Row (dict-like) for robustness.
        """
        # sqlite3.Row - use column names for robustness
        return {
            'id': row['id'],
            'name': row['name'],
            'type': row['type'],
            'host': row['host'],
            'api_port': row['api_port'],
            'vpn_port': row['vpn_port'],
            'base_path': row['base_path'],
            'api_username': row['api_username'],
            'api_password': row['api_password'],
            'public_key': row['public_key'],
            'sni': row['sni'],
            'sid': row['sid'],
            'region': row['region'],
            'city': row['city'],
            'status': row['status'],
            'is_primary': bool(row['is_primary']),
            'weight': row['weight'],
            'max_clients': row['max_clients'],
            'current_clients': row['current_clients'],
            'health_check_url': row['health_check_url'],
            'last_health_check': row['last_health_check'],
            'created_at': row['created_at']
        }
