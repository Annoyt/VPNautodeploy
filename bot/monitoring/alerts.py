"""Health monitoring and alerting system.

Provides health checks for database, X-UI connection, and client consistency.
All sync DB calls use asyncio.to_thread() to avoid blocking event loop (H-07 fix).
"""

import asyncio
import logging
from typing import Dict, Any, Optional
from datetime import datetime

from bot.config import TIMEOUT_HEALTH_CHECK

logger = logging.getLogger(__name__)

# Alert thresholds
ALERT_THRESHOLDS = {
    'db_response_ms': 1000,      # Database response time
    'orphaned_clients': 10,      # Clients in X-UI without bot users
    'missing_clients': 5,        # Bot users without X-UI clients
    'disk_usage_percent': 90,    # Disk usage
    'memory_usage_percent': 85,  # Memory usage
}


class HealthChecker:
    """Health checker with non-blocking DB calls (H-07 fix)."""
    
    def __init__(self, db, xui, bot=None):
        """Initialize health checker.
        
        Args:
            db: Database instance
            xui: XUIService instance
            bot: Optional Telegram bot for alerts
        """
        self.db = db
        self.xui = xui
        self.bot = bot
        self.checks = [
            'db_integrity',
            'xui_connection',
            'orphaned_clients',
            'missing_clients',
        ]
    
    async def run_all_checks(self) -> Dict[str, Any]:
        """Run all health checks.
        
        Returns:
            Dict with check results
        """
        results = {}
        
        for check_name in self.checks:
            try:
                method = getattr(self, f'check_{check_name}')
                results[check_name] = await method()
            except Exception as e:
                logger.error(f"Health check {check_name} failed: {e}")
                results[check_name] = False
        
        return results
    
    async def check_db_integrity(self) -> bool:
        """Check database integrity.
        
        Uses asyncio.to_thread() to avoid blocking event loop (H-07 fix).
        """
        import sqlite3
        
        def _check():
            try:
                conn = sqlite3.connect(self.db.db_path)
                try:
                    result = conn.execute("PRAGMA integrity_check").fetchone()
                    return result[0] == 'ok'
                finally:
                    conn.close()
            except Exception as e:
                logger.error(f"DB integrity check failed: {e}")
                return False
        
        return await asyncio.to_thread(_check)
    
    async def check_xui_connection(self) -> bool:
        """Check X-UI connectivity."""
        try:
            if self.xui.api:
                return await self.xui.api.login()
            return True  # DB-only mode is OK
        except Exception as e:
            logger.error(f"X-UI connection check failed: {e}")
            return False
    
    async def check_orphaned_clients(self) -> bool:
        """Check for clients in X-UI without users in bot DB.
        
        Uses asyncio.to_thread() for DB calls to avoid blocking (H-07 fix).
        """
        # API-only node (no local x-ui.db): reconciliation needs the raw
        # panel DB, which lives on the panel host. Skipping beats firing
        # an hourly "health check failed" alert on every cycle.
        if getattr(self.xui, 'db', None) is None:
            return True
        try:
            # Get all emails from X-UI (sync call in thread)
            all_traffic = await asyncio.to_thread(
                self.xui.db.get_all_client_traffic
            )
            xui_emails = set(all_traffic.keys())
            
            # Get all users from bot DB (sync call in thread)
            all_users = await asyncio.to_thread(self.db.get_all_users)
            bot_emails = {u.email for u in all_users if u.email}
            
            orphaned = xui_emails - bot_emails
            if len(orphaned) > ALERT_THRESHOLDS['orphaned_clients']:
                logger.warning(f"Found {len(orphaned)} orphaned clients")
                return False
            
            return True
        except Exception as e:
            logger.error(f"Orphaned clients check failed: {e}")
            return False
    
    async def check_missing_clients(self) -> bool:
        """Check for users in bot DB without clients in X-UI.
        
        Uses asyncio.to_thread() for DB calls to avoid blocking (H-07 fix).
        """
        # Same as check_orphaned_clients: needs the local x-ui.db.
        if getattr(self.xui, 'db', None) is None:
            return True
        try:
            # Get active users (sync call in thread)
            all_users = await asyncio.to_thread(self.db.get_all_users)
            active_users = [u for u in all_users 
                          if u.status in ('active', 'demo', 'paid')]
            
            missing = []
            for user in active_users:
                if user.email:
                    # Check if client exists in X-UI (sync call in thread)
                    traffic = await asyncio.to_thread(
                        self.xui.db.get_client_traffic, user.email
                    )
                    if traffic is None:
                        missing.append(user)
            
            if missing:
                logger.warning(f"Found {len(missing)} users missing in X-UI")
                return False
            
            return True
        except Exception as e:
            logger.error(f"Missing clients check failed: {e}")
            return False
    
    async def check_disk_space(self) -> bool:
        """Check disk space usage."""
        try:
            import shutil
            usage = shutil.disk_usage('/')
            percent_used = (usage.used / usage.total) * 100
            
            if percent_used > ALERT_THRESHOLDS['disk_usage_percent']:
                logger.warning(f"Disk usage is {percent_used:.1f}%")
                return False
            
            return True
        except Exception as e:
            logger.error(f"Disk check failed: {e}")
            return False
    
    async def check_memory_usage(self) -> bool:
        """Check memory usage."""
        try:
            import psutil
            memory = psutil.virtual_memory()
            
            if memory.percent > ALERT_THRESHOLDS['memory_usage_percent']:
                logger.warning(f"Memory usage is {memory.percent}%")
                return False
            
            return True
        except ImportError:
            # psutil not installed, skip this check
            return True
        except Exception as e:
            logger.error(f"Memory check failed: {e}")
            return False


class AlertManager:
    """Manage alerts and notifications."""
    
    def __init__(self, bot, admin_chat_id: str):
        """Initialize alert manager.
        
        Args:
            bot: Telegram bot instance
            admin_chat_id: Admin chat ID for alerts
        """
        self.bot = bot
        self.admin_chat_id = admin_chat_id
        self.alert_history: Dict[str, datetime] = {}
    
    async def send_alert(self, message: str, alert_type: str = 'general'):
        """Send alert to admin.
        
        Args:
            message: Alert message
            alert_type: Type of alert for deduplication
        """
        # Rate limit alerts (max 1 per hour per type)
        now = datetime.now()
        last_alert = self.alert_history.get(alert_type)
        
        if last_alert:
            hours_since = (now - last_alert).total_seconds() / 3600
            if hours_since < 1:
                return  # Skip, too soon
        
        try:
            text = f"🚨 <b>Alert</b>\n\n{message}"
            await self.bot.send_message(
                chat_id=self.admin_chat_id,
                text=text,
                parse_mode='HTML'
            )
            self.alert_history[alert_type] = now
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
    
    async def check_and_alert(self, health_checker: HealthChecker):
        """Run health checks and send alerts for failures."""
        results = await health_checker.run_all_checks()
        
        for check_name, passed in results.items():
            if not passed:
                await self.send_alert(
                    f"Health check failed: {check_name}",
                    alert_type=f"health_{check_name}"
                )
