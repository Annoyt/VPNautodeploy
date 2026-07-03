"""X-UI database operations"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Optional

from bot.utils.log_redaction import redact_email

logger = logging.getLogger(__name__)


class XUIDatabase:
    """Low-level X-UI database operations with WAL mode support."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    @contextmanager
    def _connect(self):
        """Create connection with WAL mode enabled.
        
        WAL mode is required because 3X-UI panel writes to the same database
        concurrently. Without WAL, we get 'database is locked' errors.
        The context manager guarantees the connection is closed afterwards,
        preventing leaked file descriptors from keeping the database locked.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()
    
    def get_vless_inbound_id(self) -> Optional[int]:
        """Find first VLESS inbound ID (usually port 443)."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT id FROM inbounds WHERE protocol = 'vless' ORDER BY port LIMIT 1"
                )
                row = c.fetchone()
                return row['id'] if row else None
        except Exception as e:
            logger.error(f"Failed to find VLESS inbound: {e}")
            return None
    
    def get_inbound_settings(self, inbound_id: int = None) -> Optional[dict]:
        """Get and parse inbound settings JSON."""
        # Auto-detect VLESS inbound if not specified
        if inbound_id is None:
            inbound_id = self.get_vless_inbound_id()
            if inbound_id is None:
                logger.error("No VLESS inbound found in database")
                return None
            logger.debug(f"Auto-detected VLESS inbound id={inbound_id}")
        
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute("SELECT settings FROM inbounds WHERE id = ?", (inbound_id,))
                row = c.fetchone()
                
                if row and row['settings']:
                    return json.loads(row['settings'])
                return None
        except Exception as e:
            logger.error(f"Failed to get inbound settings: {e}")
            return None
    
    def update_inbound_settings(self, settings: dict, inbound_id: int = None) -> bool:
        """Update inbound settings JSON with transaction support."""
        # Auto-detect VLESS inbound if not specified
        if inbound_id is None:
            inbound_id = self.get_vless_inbound_id()
            if inbound_id is None:
                logger.error("No VLESS inbound found in database")
                return False
        
        try:
            with self._connect() as conn:
                try:
                    conn.execute("BEGIN TRANSACTION")
                    conn.execute(
                        "UPDATE inbounds SET settings = ? WHERE id = ?",
                        (json.dumps(settings), inbound_id)
                    )
                    conn.commit()
                    return True
                except Exception:
                    conn.rollback()
                    raise
        except Exception as e:
            logger.error(f"Failed to update inbound settings: {e}")
            return False
    
    def get_client_traffic(self, email: str) -> Optional[dict]:
        """Get client traffic statistics."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute(
                    "SELECT up, down, total FROM client_traffics WHERE email = ?",
                    (email,)
                )
                row = c.fetchone()
                
                if row:
                    return {'upload': row['up'], 'download': row['down'], 'total': row['total']}
                return None
        except Exception as e:
            logger.error(f"Failed to get traffic: {e}")
            return None
    
    def get_all_client_traffic(self) -> dict:
        """Get traffic statistics for all clients."""
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute("SELECT email, up, down, total FROM client_traffics")
                rows = c.fetchall()
                
                return {
                    row['email']: {'upload': row['up'], 'download': row['down'], 'total': row['total']}
                    for row in rows
                }
        except Exception as e:
            logger.error(f"Failed to get all traffic: {e}")
            return {}
    
    def ensure_client_traffic(self, email: str, inbound_id: int = None, expiry_time: int = 0, enable: bool = True) -> bool:
        """Ensure client_traffics record exists for a client.
        
        3x-ui requires client_traffics records for traffic accounting and
        limitIp enforcement. Direct DB inserts into inbounds.settings alone
        are insufficient.
        """
        if inbound_id is None:
            inbound_id = self.get_vless_inbound_id()
            if inbound_id is None:
                logger.error("No VLESS inbound found for ensure_client_traffic")
                return False
        
        try:
            with self._connect() as conn:
                c = conn.cursor()
                c.execute('SELECT 1 FROM client_traffics WHERE email = ?', (email,))
                if c.fetchone():
                    return True
                
                c.execute('''
                    INSERT INTO client_traffics (inbound_id, enable, email, up, down, total, expiry_time, reset, last_online)
                    VALUES (?, ?, ?, 0, 0, 0, ?, 0, 0)
                ''', (inbound_id, 1 if enable else 0, email, expiry_time))
                conn.commit()
                logger.info(f"Created client_traffics record for {redact_email(email)}")
                return True
        except Exception as e:
            logger.error(f"Failed to ensure client_traffics for {redact_email(email)}: {e}")
            return False
