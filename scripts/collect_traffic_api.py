#!/usr/bin/env python3
"""
Traffic collection script using HTTP API (no docker exec).

This script replaces the old collect_traffic.py that used docker exec
to get XRay stats. Now it uses the 3X-UI HTTP API directly.

Usage:
    python scripts/collect_traffic_api.py

Environment:
    XUI_API_URL - URL of 3X-UI panel (default: http://127.0.0.1:2026)
    XUI_API_PATH - API path prefix (default: /panel/api/inbounds)
    XUI_USERNAME - Username for 3X-UI (default: admin)
    XUI_PASSWORD - Password for 3X-UI (default: admin)
    DB_PATH - Path to bot database (default: /var/lib/vpn-bot/bot.db)
    HEALTH_PORT - Port for health check HTTP server (default: 8080)
"""

import asyncio
import os
import sys
import logging
import threading
import json
from datetime import datetime
from typing import Dict, Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.services.xui_api import XRayStatsService
from bot.core.database import Database

# Global state for health check
last_run_success = False
last_run_time = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


class TrafficCollector:
    """Collects and accumulates traffic from XRay via 3X-UI API."""
    
    def __init__(self):
        self.xui_url = os.getenv('XUI_API_URL', 'http://127.0.0.1:2026')
        self.xui_base_path = os.getenv('XUI_BASE_PATH', '/this_is_fine')
        self.xui_api_path = os.getenv('XUI_API_PATH', '/this_is_fine/panel/api/inbounds')
        self.xui_username = os.getenv('XUI_USERNAME', 'admin')
        self.xui_password = os.getenv('XUI_PASSWORD', 'admin')
        self.db_path = os.getenv('DB_PATH', '/var/lib/vpn-bot/bot.db')
        
        self.stats_service = XRayStatsService(
            base_url=self.xui_url,
            username=self.xui_username,
            password=self.xui_password,
            base_path=self.xui_base_path,
            api_path=self.xui_api_path
        )
        self.db = Database(self.db_path)
    
    async def login(self) -> bool:
        """Login to 3X-UI panel."""
        logger.info(f"Logging in to 3X-UI at {self.xui_url}")
        return await self.stats_service.login()
    
    def ensure_tables(self):
        """Ensure traffic_accumulated table exists."""
        conn = self.db._get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS traffic_accumulated (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT UNIQUE NOT NULL,
                    total_up INTEGER DEFAULT 0,
                    total_down INTEGER DEFAULT 0,
                    last_session_up INTEGER DEFAULT 0,
                    last_session_down INTEGER DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Trigger for auto-updating updated_at
            c.execute('''
                CREATE TRIGGER IF NOT EXISTS update_traffic_time 
                AFTER UPDATE ON traffic_accumulated
                BEGIN
                    UPDATE traffic_accumulated SET updated_at = datetime('now') WHERE id = NEW.id;
                END
            ''')
            
            conn.commit()
            logger.info("Traffic table ready")
        finally:
            conn.close()
    
    async def collect_traffic(self) -> Dict[str, Dict[str, int]]:
        """Collect traffic from XRay."""
        logger.info("Collecting traffic from XRay API...")
        stats = await self.stats_service.get_all_clients_stats()
        
        result = {}
        for email, traffic in stats.items():
            result[email] = {
                'up': traffic.up_bytes,
                'down': traffic.down_bytes,
                'total': traffic.total_bytes
            }
        
        logger.info(f"Got traffic for {len(result)} clients")
        return result
    
    def accumulate_traffic(self, traffic_data: Dict[str, Dict[str, int]]) -> int:
        """Accumulate traffic in database."""
        conn = self.db._get_connection()
        try:
            c = conn.cursor()
            updated = 0
            
            for email, data in traffic_data.items():
                current_up = data['up']
                current_down = data['down']
                
                # Get previous values
                c.execute('''
                    SELECT total_up, total_down, last_session_up, last_session_down 
                    FROM traffic_accumulated 
                    WHERE email = ?
                ''', (email,))
                row = c.fetchone()
                
                if row:
                    prev_total_up, prev_total_down, prev_session_up, prev_session_down = row
                    
                    # Check if XRay was restarted (current < previous session)
                    if current_up < prev_session_up or current_down < prev_session_down:
                        # XRay restarted, add current values
                        add_up = current_up
                        add_down = current_down
                    else:
                        # Normal accumulation
                        add_up = current_up - prev_session_up
                        add_down = current_down - prev_session_down
                    
                    new_total_up = prev_total_up + add_up
                    new_total_down = prev_total_down + add_down
                    
                    c.execute('''
                        UPDATE traffic_accumulated 
                        SET total_up = ?, total_down = ?, last_session_up = ?, last_session_down = ?
                        WHERE email = ?
                    ''', (new_total_up, new_total_down, current_up, current_down, email))
                else:
                    # New user
                    c.execute('''
                        INSERT INTO traffic_accumulated (email, total_up, total_down, last_session_up, last_session_down)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (email, current_up, current_down, current_up, current_down))
                
                updated += 1
            
            conn.commit()
            logger.info(f"Updated {updated} users in traffic_accumulated")
            return updated
            
        finally:
            conn.close()
    
    def update_users_table(self) -> int:
        """Update users.traffic_up/down from traffic_accumulated."""
        conn = self.db._get_connection()
        try:
            c = conn.cursor()
            
            # Get all accumulated traffic
            c.execute('SELECT email, total_up, total_down FROM traffic_accumulated')
            updated = 0
            
            for email, total_up, total_down in c.fetchall():
                up_gb = total_up / (1024 ** 3)
                down_gb = total_down / (1024 ** 3)
                
                c.execute('''
                    UPDATE users 
                    SET traffic_up = ?, traffic_down = ?, last_traffic_update = datetime('now')
                    WHERE email = ?
                ''', (up_gb, down_gb, email))
                
                if c.rowcount > 0:
                    updated += 1
            
            conn.commit()
            logger.info(f"Updated {updated} users in users table")
            return updated
            
        finally:
            conn.close()
    
    def show_stats(self):
        """Show current traffic stats."""
        conn = self.db._get_connection()
        try:
            c = conn.cursor()
            c.execute('''
                SELECT email, total_up, total_down 
                FROM traffic_accumulated 
                ORDER BY (total_up + total_down) DESC 
                LIMIT 5
            ''')
            
            print("\nTop 5 users by traffic:")
            for row in c.fetchall():
                email, total_up, total_down = row
                up_gb = total_up / (1024 ** 3)
                down_gb = total_down / (1024 ** 3)
                print(f"  {email}: ↑{up_gb:.3f}GB ↓{down_gb:.3f}GB")
                
        finally:
            conn.close()
    
    async def run(self) -> bool:
        """Run the traffic collection process."""
        try:
            # Ensure tables exist
            self.ensure_tables()
            
            # Login to 3X-UI
            if not await self.login():
                logger.error("Failed to login to 3X-UI")
                return False
            
            # Collect traffic
            traffic_data = await self.collect_traffic()
            
            if not traffic_data:
                logger.warning("No traffic data collected")
                return False
            
            # Accumulate in database
            self.accumulate_traffic(traffic_data)
            
            # Update users table
            self.update_users_table()
            
            # Show stats
            self.show_stats()
            
            logger.info("Traffic collection completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Error during traffic collection: {e}")
            return False
        finally:
            await self.stats_service.close()


class HealthHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for health checks."""
    
    def log_message(self, format, *args):
        # Suppress logging
        pass
    
    def do_GET(self):
        if self.path == '/health':
            global last_run_success, last_run_time
            
            # Check if last run was within 10 minutes
            healthy = False
            if last_run_time:
                elapsed = (datetime.now() - last_run_time).total_seconds()
                healthy = last_run_success and elapsed < 600  # 10 minutes
            
            status = 'healthy' if healthy else 'degraded'
            code = 200 if healthy else 503
            
            response = {
                'status': status,
                'service': 'traffic-collector',
                'last_run': last_run_time.isoformat() if last_run_time else None,
                'last_run_success': last_run_success
            }
            
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()


def start_health_server(port: int = 8080):
    """Start health check HTTP server in background thread."""
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health server started on port {port}")
    return server


async def main():
    """Main entry point."""
    global last_run_success, last_run_time
    
    collector = TrafficCollector()
    success = await collector.run()
    
    # Update global state for health check
    last_run_success = success
    last_run_time = datetime.now()
    
    sys.exit(0 if success else 1)


def run_loop():
    """Run traffic collection in a loop."""
    global last_run_success, last_run_time
    
    # Start health server
    health_port = int(os.getenv('HEALTH_PORT', '8080'))
    start_health_server(health_port)
    
    while True:
        logger.info("[$(date)] Collecting traffic...")
        try:
            collector = TrafficCollector()
            success = asyncio.run(collector.run())
            last_run_success = success
            last_run_time = datetime.now()
        except Exception as e:
            logger.error(f"Collection error: {e}")
            last_run_success = False
        
        # Sleep for 5 minutes
        import time
        time.sleep(300)


if __name__ == '__main__':
    run_loop()
