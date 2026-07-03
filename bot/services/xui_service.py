"""Unified X-UI Service - HTTP API first with DB fallback.

This service provides a unified interface for X-UI operations,
using HTTP API as primary method with direct DB access as fallback.
"""

import logging
import asyncio
import concurrent.futures
from typing import Optional, Dict, Any

from bot.config import Settings
from bot.services.xui_api.client import XUIAPIClient, XUIClientConfig
from bot.services.xui_db import XUIDatabase
from bot.utils.log_redaction import redact_email

logger = logging.getLogger(__name__)


class XUIService:
    """Unified service for X-UI operations.
    
    Priority:
    1. HTTP API (works across containers)
    2. Direct DB access (fallback, requires shared volume)
    
    Usage (new):
        xui = XUIService(config)
        traffic = await xui.get_client_traffic("user@example.com")
    
    Usage (legacy backward compatible):
        xui = XUIService(db_path='/path/to/x-ui.db', api_config={...})
    """
    
    # ThreadPoolExecutor for running async coroutines from sync context
    # This avoids 'This event loop is already running' when called from async code
    _executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='xui_sync')
    
    def __init__(self, config: Settings = None, *, db_path: str = None, api_config: Dict[str, Any] = None):
        """Initialize X-UI service.
        
        Supports two calling conventions:
        1. New: XUIService(config) - preferred
        2. Legacy: XUIService(db_path=..., api_config=...) - backward compatible
        
        Args:
            config: Application settings with XUI_API_URL and XUI_DB_PATH (new style)
            db_path: Direct DB path (legacy style)
            api_config: Dict with base_url, username, password (legacy style)
        """
        self.config = config
        self.api: Optional[XUIAPIClient] = None
        self.db: Optional[XUIDatabase] = None
        
        # Determine configuration source (new style vs legacy)
        if config is not None:
            # New style with Settings object
            self._init_from_settings(config)
        else:
            # Legacy style with individual parameters
            self._init_legacy(db_path, api_config)
    
    def _run_sync(self, coro):
        """Run async coroutine from sync context safely.
        
        If called from async context, runs in a separate thread to avoid
        'This event loop is already running' error.
        
        Args:
            coro: Async coroutine to run
            
        Returns:
            Coroutine result
        """
        try:
            loop = asyncio.get_running_loop()
            # Async context: must run in a separate thread
            return self._executor.submit(asyncio.run, coro).result(timeout=30)
        except RuntimeError:
            # Sync context: safe to use asyncio.run directly
            return asyncio.run(coro)
    
    def _init_from_settings(self, config: Settings):
        """Initialize from Settings object (new style)."""
        # HTTP API client (primary)
        if config.XUI_API_URL:
            api_config = XUIClientConfig(
                base_url=config.XUI_API_URL,
                username=getattr(config, 'XUI_USERNAME', 'admin'),
                password=getattr(config, 'XUI_PASSWORD', 'admin'),
                base_path=getattr(config, 'XUI_BASE_PATH', '/this_is_fine'),
                api_path=getattr(config, 'XUI_API_PATH', '/this_is_fine/panel/api/inbounds')
            )
            self.api = XUIAPIClient(api_config)
            logger.info(f"X-UI HTTP API configured: {config.XUI_API_URL}")
        
        # DB fallback (only if file exists)
        if config.XUI_DB_PATH:
            import os
            if os.path.exists(config.XUI_DB_PATH):
                self.db = XUIDatabase(config.XUI_DB_PATH)
                logger.info(f"X-UI DB configured: {config.XUI_DB_PATH}")
                self._validate_db_path(config.XUI_DB_PATH)
            else:
                logger.warning(
                    f"X-UI DB not found at {config.XUI_DB_PATH}. "
                    "Falling back to API-only mode. "
                    "For DB access, ensure shared volume is mounted."
                )
    
    def _validate_db_path(self, db_path: str):
        """Validate that configured DB path points to a real X-UI database.
        
        Prevents the silent failure where bot writes to a stale bind mount
        while the 3x-ui container reads from its Docker volume.
        """
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                c = conn.cursor()
                # Check for inbounds table and at least one VLESS inbound
                c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbounds'")
                if not c.fetchone():
                    logger.error(
                        f"CRITICAL: {db_path} does not contain an 'inbounds' table. "
                        "This is likely NOT the X-UI database the container uses. "
                        "Check XUI_DB_PATH env variable."
                    )
                    return
                c.execute("SELECT 1 FROM inbounds WHERE protocol = 'vless' LIMIT 1")
                if not c.fetchone():
                    logger.error(
                        f"CRITICAL: {db_path} has no VLESS inbound. "
                        "X-UI may be using a different database file."
                    )
                    return
            finally:
                conn.close()
        except Exception as e:
            logger.error(f"Failed to validate X-UI DB path {db_path}: {e}")
            return
        
        # Cross-check against Docker volume if possible (only when docker CLI is available).
        # When the bot itself runs inside a container without docker-socket access, skip silently.
        import shutil
        if shutil.which("docker"):
            try:
                import subprocess
                result = subprocess.run(
                    ['docker', 'inspect', getattr(self.config, 'XUI_CONTAINER_NAME', '3x-ui'),
                     '--format', '{{range .Mounts}}{{if eq .Destination "/etc/x-ui"}}{{println .Source}}{{end}}{{end}}'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    volume_source = result.stdout.strip()
                    expected_path = f"{volume_source}/x-ui.db"
                    if volume_source and db_path != expected_path:
                        logger.error(
                            f"CRITICAL: Configured XUI_DB_PATH ({db_path}) does not match "
                            f"the 3x-ui Docker volume mount ({expected_path}). "
                            "The bot and X-UI container will see DIFFERENT databases. "
                            "Set XUI_DB_PATH={expected_path} to fix this."
                        )
            except Exception:
                pass  # Docker call failed; non-fatal.
    
    def _init_legacy(self, db_path: Optional[str], api_config: Optional[Dict[str, Any]]):
        """Initialize from legacy parameters (backward compatibility)."""
        # HTTP API client
        if api_config and api_config.get('base_url'):
            cfg = XUIClientConfig(
                base_url=api_config['base_url'],
                username=api_config.get('username', 'admin'),
                password=api_config.get('password', 'admin'),
                base_path=api_config.get('base_path', '/this_is_fine'),
                api_path=api_config.get('api_path', '/this_is_fine/panel/api/inbounds')
            )
            self.api = XUIAPIClient(cfg)
            logger.info(f"X-UI HTTP API configured (legacy): {api_config['base_url']}")
        
        # DB
        if db_path:
            try:
                self.db = XUIDatabase(db_path)
                logger.info(f"X-UI DB configured (legacy): {db_path}")
            except Exception as e:
                logger.warning(f"Failed to initialize DB (legacy): {e}")
    
    async def get_client_traffic(self, email: str) -> Optional[Dict]:
        """Get traffic statistics for a client.
        
        Args:
            email: Client email address
            
        Returns:
            Dict with upload/download/total or None if not found
        """
        # Try API first
        if self.api:
            try:
                result = await self.api.get_client_traffic(email)
                if result:
                    return {
                        'upload': result.get('up', 0),
                        'download': result.get('down', 0),
                        'total': result.get('total', 0)
                    }
            except Exception as e:
                logger.warning(f"API traffic fetch failed for {redact_email(email)}: {e}")
        
        # Fallback to DB
        if self.db:
            return self.db.get_client_traffic(email)
        
        return None
    
    async def get_inbound_settings(self, inbound_id: int = None) -> Optional[Dict]:
        """Get inbound settings by ID or auto-detect VLESS inbound.
        
        Args:
            inbound_id: Inbound ID (if None, auto-detects first VLESS inbound)
            
        Returns:
            Dict with settings including 'clients' list
        """
        # Try API first
        if self.api:
            try:
                if inbound_id is not None:
                    result = await self.api.get_inbound(inbound_id)
                else:
                    # Auto-detect: get first VLESS inbound from list
                    inbounds = await self.api.get_inbounds()
                    vless_inbound = None
                    for inbound in inbounds:
                        if inbound.get('protocol') == 'vless':
                            vless_inbound = inbound
                            break
                    result = vless_inbound
                
                if result:
                    return result
            except Exception as e:
                logger.warning(f"API get_inbound failed for {inbound_id}: {e}")
        
        # Fallback to DB
        if self.db:
            return self.db.get_inbound_settings(inbound_id)
        
        return None
    
    def get_all_traffic(self) -> Dict[str, Dict]:
        """Get traffic for all clients (synchronous).
        
        Note: This method is synchronous for backward compatibility.
        For async operations, use get_client_traffic() in a loop.
        
        Returns:
            Dict mapping email to traffic stats
        """
        # DB is faster for bulk operations
        if self.db:
            return self.db.get_all_client_traffic()
        
        logger.warning("Cannot get_all_traffic: DB not available (bulk ops require DB)")
        return {}
    
    def get_client_traffic_sync(self, email: str) -> Optional[Dict]:
        """Get traffic statistics for a client (synchronous wrapper).
        
        DEPRECATED: Use await get_client_traffic() instead.
        This method is kept for backward compatibility only.
        
        Args:
            email: Client email address
            
        Returns:
            Dict with upload/download/total or None if not found
        """
        import warnings
        warnings.warn(
            "get_client_traffic_sync is deprecated, use await get_client_traffic()",
            DeprecationWarning,
            stacklevel=2
        )
        return self._run_sync(self.get_client_traffic(email))
    
    def get_inbound_settings_sync(self, inbound_id: int = None) -> Optional[Dict]:
        """Get inbound settings (synchronous wrapper).
        
        DEPRECATED: Use await get_inbound_settings() instead.
        This method is kept for backward compatibility only.
        
        Args:
            inbound_id: Inbound ID (if None, auto-detects first VLESS inbound)
            
        Returns:
            Dict with settings including 'clients' list
        """
        import warnings
        warnings.warn(
            "get_inbound_settings_sync is deprecated, use await get_inbound_settings()",
            DeprecationWarning,
            stacklevel=2
        )
        return self._run_sync(self.get_inbound_settings(inbound_id))
    
    def get_client_sync(self, email: str) -> Optional[Dict]:
        """Get client information (synchronous wrapper).
        
        DEPRECATED: Use await get_client() instead.
        This method is kept for backward compatibility only.
        
        Args:
            email: Client email
            
        Returns:
            Client dict or None
        """
        import warnings
        warnings.warn(
            "get_client_sync is deprecated, use await get_client()",
            DeprecationWarning,
            stacklevel=2
        )
        return self._run_sync(self.get_client(email))
    
    async def update_client_traffic(self, email: str, upload: int, download: int) -> bool:
        """Update traffic for a client.
        
        Args:
            email: Client email
            upload: Upload bytes
            download: Download bytes
            
        Returns:
            True if successful
        """
        # Try API first
        if self.api:
            try:
                success = await self.api.update_client_traffic(email, upload, download)
                if success:
                    return True
            except Exception as e:
                logger.warning(f"API update failed for {redact_email(email)}: {e}")
        
        # Fallback to DB
        if self.db:
            return self.db.update_client_traffic(email, upload, download)
        
        return False
    
    async def reset_client_traffic(self, email: str) -> bool:
        """Reset traffic counters for a client.
        
        Args:
            email: Client email
            
        Returns:
            True if successful
        """
        # Try API first
        if self.api:
            try:
                success = await self.api.reset_client_traffic(email)
                if success:
                    return True
            except Exception as e:
                logger.warning(f"API reset failed for {redact_email(email)}: {e}")
        
        # Fallback to DB
        if self.db:
            return self.db.reset_client_traffic(email)
        
        return False
    
    async def get_client(self, email: str) -> Optional[Dict]:
        """Get client information.
        
        Args:
            email: Client email
            
        Returns:
            Client dict or None
        """
        # Try API first
        if self.api:
            try:
                client = await self.api.get_client(email)
                if client:
                    return client
            except Exception as e:
                logger.warning(f"API get_client failed for {redact_email(email)}: {e}")
        
        # Fallback to DB
        if self.db:
            return self.db.get_client_by_email(email)
        
        return None
    
    async def get_online_users(self) -> list:
        """Get list of currently online users.
        
        Returns:
            List of email addresses
        """
        if not self.api:
            logger.warning("API not available for online users check")
            return []
        
        try:
            # Try login first (API requires authentication)
            if hasattr(self.api, 'login') and callable(self.api.login):
                login_success = await self.api.login()
                if not login_success:
                    logger.warning("Failed to login for online users check")
                    return []
            
            # Use get_online_clients method
            if hasattr(self.api, 'get_online_clients'):
                return await self.api.get_online_clients()
            
            logger.warning("API does not support get_online_clients")
            return []
            
        except Exception as e:
            logger.warning(f"API get_online_users failed: {e}")
            return []
    
    # ===== Sync methods for DB operations =====
    
    def add_client_sync(self, client: dict, inbound_id: int = None) -> bool:
        """Add client to X-UI inbound (synchronous DB operation).
        
        Args:
            client: Client config dict with 'id', 'email', etc.
            inbound_id: Optional inbound ID (auto-detected if None)
            
        Returns:
            True if successful
        """
        if not self.db:
            logger.error("No X-UI database configured")
            return False
        
        try:
            settings = self.db.get_inbound_settings(inbound_id)
            if settings is None:
                logger.error(f"Inbound {inbound_id} not found")
                return False
            
            clients = settings.get('clients', [])
            email = client.get('email')
            
            # Remove duplicate if exists
            clients = [c for c in clients if c.get('email') != email]
            clients.append(client)
            settings['clients'] = clients
            settings.setdefault('decryption', 'none')
            
            if not self.db.update_inbound_settings(settings, inbound_id):
                return False
            
            # Ensure 3x-ui client_traffics record exists for traffic accounting
            self.db.ensure_client_traffic(
                email=email,
                inbound_id=inbound_id,
                expiry_time=client.get('expiryTime', 0),
                enable=client.get('enable', True)
            )

            # Mirror to the CF-fronted WS inbound (Phase H) so the user's
            # UUID is also valid on the cdn.<domain>:2053 path. Skipped
            # if WS_INBOUND_ID isn't configured, or if we were already
            # writing to it (caller passed it explicitly).
            ws_inbound_id = int(getattr(self.config, 'WS_INBOUND_ID', 0) or 0)
            if ws_inbound_id and ws_inbound_id != inbound_id:
                self._mirror_client_add(client, ws_inbound_id)

            # Nudge Xray to re-read SQLite (graceful, ~zero downtime).
            # Soft failure here is fine — the row is in x-ui.db; the
            # next reload (manual or by another op) will pick it up.
            if not self.reload_xray_sync():
                logger.warning(
                    f"Added client {email} to SQLite but reload sidecar failed; "
                    f"key may not work until next reload."
                )

            logger.info(f"Added client {redact_email(email)} to inbound {inbound_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to add client: {e}")
            return False

    def _mirror_client_add(self, client: dict, ws_inbound_id: int) -> None:
        """Add client to the secondary WS inbound. Soft-fail: if the
        WS inbound is missing or write fails, the Reality path still
        works — user just won't have the CDN fallback until next sync."""
        try:
            settings = self.db.get_inbound_settings(ws_inbound_id)
            if settings is None:
                logger.warning(f"WS inbound {ws_inbound_id} missing — skipping mirror")
                return
            email = client.get('email')
            clients = [c for c in settings.get('clients', []) if c.get('email') != email]
            clients.append(client)
            settings['clients'] = clients
            settings.setdefault('decryption', 'none')
            if not self.db.update_inbound_settings(settings, ws_inbound_id):
                logger.warning(f"WS inbound update failed for {redact_email(email)}")
                return
            self.db.ensure_client_traffic(
                email=email,
                inbound_id=ws_inbound_id,
                expiry_time=client.get('expiryTime', 0),
                enable=client.get('enable', True),
            )
            logger.info(f"Mirrored client {redact_email(email)} to WS inbound {ws_inbound_id}")
        except Exception as e:
            logger.warning(f"WS mirror add soft-failed for {client.get('email')}: {e}")

    def _mirror_client_remove(self, email: str, ws_inbound_id: int) -> None:
        """Remove client from the secondary WS inbound. Soft-fail."""
        try:
            settings = self.db.get_inbound_settings(ws_inbound_id)
            if settings is None:
                return
            clients = [c for c in settings.get('clients', []) if c.get('email') != email]
            settings['clients'] = clients
            settings.setdefault('decryption', 'none')
            self.db.update_inbound_settings(settings, ws_inbound_id)
        except Exception as e:
            logger.warning(f"WS mirror remove soft-failed for {redact_email(email)}: {e}")

    def remove_client_sync(self, email: str, inbound_id: int = None) -> bool:
        """Remove client from X-UI inbound (synchronous DB operation).
        
        Args:
            email: Client email to remove
            inbound_id: Optional inbound ID (auto-detected if None)
            
        Returns:
            True if successful
        """
        if not self.db:
            logger.error("No X-UI database configured")
            return False
        
        try:
            settings = self.db.get_inbound_settings(inbound_id)
            if settings is None:
                logger.error(f"Inbound {inbound_id} not found")
                return False
            
            clients = settings.get('clients', [])
            original_count = len(clients)
            clients = [c for c in clients if c.get('email') != email]
            
            if len(clients) == original_count:
                logger.warning(f"Client {redact_email(email)} not found")
                return False

            settings['clients'] = clients
            settings.setdefault('decryption', 'none')
            if not self.db.update_inbound_settings(settings, inbound_id):
                return False

            # Mirror remove to the CF-fronted WS inbound (Phase H).
            ws_inbound_id = int(getattr(self.config, 'WS_INBOUND_ID', 0) or 0)
            if ws_inbound_id and ws_inbound_id != inbound_id:
                self._mirror_client_remove(email, ws_inbound_id)

            if not self.reload_xray_sync():
                logger.warning(
                    f"Removed client {email} from SQLite but reload sidecar "
                    f"failed; the user may still connect until next reload."
                )

            logger.info(f"Removed client {redact_email(email)}")
            return True

        except Exception as e:
            logger.error(f"Failed to remove client: {e}")
            return False
    
    # Note: _run_async(), _get_executor(), shutdown_executor() removed (C-03 fix)
    # They were causing deadlock risks when called from async context.
    # Use asyncio.run() directly for sync wrappers or better - use async methods.
    
    def sync_client_settings_sync(self, email: str, updates: dict) -> bool:
        """Sync client settings (sync wrapper for backward compatibility).
        
        Updates client settings like expiryTime, limitIp, totalGB in X-UI.
        This modifies the client's config in the inbound settings.
        
        Args:
            email: Client email
            updates: Dict of settings to update (expiryTime, limitIp, totalGB)
            
        Returns:
            True if successful
        """
        if not self.db:
            logger.error("No X-UI database configured for sync_client_settings_sync")
            return False
        
        try:
            # Get current inbound settings
            settings = self.db.get_inbound_settings()
            if not settings:
                logger.error("No inbound settings found")
                return False
            
            # Find and update client
            clients = settings.get('clients', [])
            client_found = False
            for client in clients:
                if client.get('email') == email:
                    # Update client settings
                    if 'expiryTime' in updates:
                        client['expiryTime'] = updates['expiryTime']
                    if 'limitIp' in updates:
                        client['limitIp'] = updates['limitIp']
                    if 'totalGB' in updates:
                        client['totalGB'] = updates['totalGB']
                    if 'enable' in updates:
                        client['enable'] = updates['enable']
                    client_found = True
                    break
            
            if not client_found:
                logger.warning(f"Client {redact_email(email)} not found in inbound settings")
                return False
            
            # Save updated settings
            if not self.db.update_inbound_settings(settings):
                logger.error("Failed to update inbound settings")
                return False

            if not self.reload_xray_sync():
                logger.warning(
                    f"Updated settings for {email} in SQLite but reload sidecar "
                    f"failed; new limits will be picked up on next reload."
                )

            logger.info(f"Updated client settings for {redact_email(email)}: {updates}")
            return True
            
        except Exception as e:
            logger.exception(f"Failed to sync client settings: {e}")
            return False
    
    def validate_db_path_sync(self) -> bool:
        """Synchronous validation that XUI_DB_PATH is correct.
        
        Checks:
        1. DB file is accessible and contains 'inbounds' table with VLESS inbound
        2. If Docker is available, configured path matches the 3x-ui container volume
        
        Returns:
            True if validation passes, False otherwise (errors are logged)
        """
        if not self.db:
            logger.error("validate_db_path_sync: No X-UI DB configured")
            return False
        
        db_path = self.db.db_path if hasattr(self.db, 'db_path') else str(self.db)
        
        try:
            import sqlite3
            conn = sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbounds'")
            if not c.fetchone():
                logger.error(
                    f"CRITICAL: {db_path} does not contain an 'inbounds' table. "
                    "This is likely NOT the X-UI database the container uses."
                )
                conn.close()
                return False
            c.execute("SELECT 1 FROM inbounds WHERE protocol = 'vless' LIMIT 1")
            if not c.fetchone():
                logger.error(
                    f"CRITICAL: {db_path} has no VLESS inbound. "
                    "X-UI may be using a different database file."
                )
                conn.close()
                return False
            conn.close()
        except Exception as e:
            logger.error(f"validate_db_path_sync: Failed to read {db_path}: {e}")
            return False
        
        # Cross-check against Docker volume if possible (skip when docker CLI absent).
        import shutil
        if shutil.which("docker"):
            try:
                import subprocess
                container_name = getattr(self.config, 'XUI_CONTAINER_NAME', '3x-ui')
                result = subprocess.run(
                    ['docker', 'inspect', container_name,
                     '--format', '{{range .Mounts}}{{if eq .Destination "/etc/x-ui"}}{{println .Source}}{{end}}{{end}}'],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    volume_source = result.stdout.strip()
                    expected_path = f"{volume_source}/x-ui.db"
                    if volume_source and db_path != expected_path:
                        logger.error(
                            f"CRITICAL: Configured XUI_DB_PATH ({db_path}) does not match "
                            f"the {container_name} Docker volume mount ({expected_path}). "
                            "The bot and X-UI container will see DIFFERENT databases. "
                            f"Set XUI_DB_PATH={expected_path} to fix this."
                        )
                        return False
            except Exception:
                pass  # Docker call failed; non-fatal.

        logger.info(f"validate_db_path_sync: X-UI DB path {db_path} is valid")
        return True
    
    def sync_all_clients_from_bot_db(self, bot_db) -> dict:
        """Synchronize all active bot users into the X-UI database.
        
        Compares bot DB users (those with email and uuid) against X-UI inbound
        clients. Adds missing clients, updates UUID mismatches, removes stale
        clients from X-UI that are not in the bot DB, and reloads XRay.
        
        Args:
            bot_db: Bot Database instance with get_all_users() method
            
        Returns:
            Dict with counts: added, updated_uuid, removed, unchanged, errors
        """
        if not self.db:
            logger.error("sync_all_clients_from_bot_db: No X-UI DB configured")
            return {"added": 0, "updated_uuid": 0, "removed": 0, "unchanged": 0, "errors": 1}
        
        if not self.validate_db_path_sync():
            logger.error("sync_all_clients_from_bot_db: DB path validation failed, aborting sync")
            return {"added": 0, "updated_uuid": 0, "removed": 0, "unchanged": 0, "errors": 1}
        
        result = {"added": 0, "updated_uuid": 0, "removed": 0, "unchanged": 0, "errors": 0}
        
        try:
            # Get bot users with email and uuid
            bot_users = [u for u in bot_db.get_all_users() if u.email and u.uuid]
            bot_emails = {u.email for u in bot_users}
            
            # Get current X-UI inbound settings
            settings = self.db.get_inbound_settings()
            if not settings:
                logger.error("sync_all_clients_from_bot_db: No inbound settings found in X-UI DB")
                return {**result, "errors": 1}
            
            inbound_id = settings.get('id', 1)
            clients = settings.get('clients', [])
            xui_emails = {c.get('email') for c in clients if c.get('email')}
            
            # Build updated clients list
            updated_clients = []
            
            for client in clients:
                email = client.get('email')
                if email not in bot_emails:
                    result["removed"] += 1
                    logger.info(f"sync_all_clients_from_bot_db: Removing stale client {redact_email(email)}")
                    continue
                
                bot_user = next((u for u in bot_users if u.email == email), None)
                if bot_user and client.get('id') != bot_user.uuid:
                    result["updated_uuid"] += 1
                    logger.info(f"sync_all_clients_from_bot_db: Updating UUID for {redact_email(email)}")
                    client['id'] = bot_user.uuid
                updated_clients.append(client)
            
            # Add missing clients
            existing_emails = {c.get('email') for c in updated_clients}
            for user in bot_users:
                if user.email not in existing_emails:
                    result["added"] += 1
                    logger.info(f"sync_all_clients_from_bot_db: Adding client {user.email}")
                    updated_clients.append({
                        "id": user.uuid,
                        "email": user.email,
                        "enable": True,
                        "expiryTime": 0,
                        "flow": "xtls-rprx-vision",
                        "limitIp": getattr(user, 'limit_ip', 1),
                        "totalGB": int((getattr(user, 'quota_gb', 5.0) or 5.0) * 1024**3),
                        "tgId": "",
                        "subId": "",
                        "comment": ""
                    })
                    self.db.ensure_client_traffic(
                        email=user.email,
                        inbound_id=inbound_id,
                        expiry_time=0,
                        enable=True
                    )
            
            settings['clients'] = updated_clients
            settings.setdefault('decryption', 'none')
            if not self.db.update_inbound_settings(settings, inbound_id):
                logger.error("sync_all_clients_from_bot_db: Failed to update inbound settings")
                return {**result, "errors": 1}
            
            if not self.reload_xray_sync():
                logger.error("sync_all_clients_from_bot_db: XRay reload failed")
                return {**result, "errors": 1}
            
            logger.info(
                f"sync_all_clients_from_bot_db complete: "
                f"added={result['added']}, updated_uuid={result['updated_uuid']}, "
                f"removed={result['removed']}, unchanged={len(updated_clients) - result['added'] - result['updated_uuid']}"
            )
            return result
            
        except Exception as e:
            logger.exception(f"sync_all_clients_from_bot_db failed: {e}")
            return {**result, "errors": 1}
    
    async def get_online_clients(self) -> list:
        """Return emails currently online per the 3x-ui /onlines API.

        Empty list if the API client isn't configured or login fails —
        callers should treat that as "data unavailable", not as "no one
        online". The endpoint returns a flat list of emails (no per-IP
        breakdown; 3x-ui only knows session-level presence).
        """
        if not self.api:
            return []
        try:
            return await self.api.get_online_clients()
        except Exception as e:
            logger.warning(f"get_online_clients failed: {e}")
            return []

    def reload_xray_sync(self) -> bool:
        """Reload XRay configuration (sync wrapper).
        
        Returns:
            True if successful
        """
        from bot.services.xui_reload import reload_xray
        return reload_xray()
    
    # ===== Convenience methods matching XUISyncService API =====
    
    async def sync_user(self, chat_id: str, client_config: dict) -> bool:
        """Sync user to X-UI (convenience method matching XUISyncService API).
        
        Args:
            chat_id: User's Telegram chat ID
            client_config: X-UI client configuration dict
            
        Returns:
            True if sync successful
        """
        email = client_config.get('email')
        if not email:
            logger.error("Client config missing email")
            return False
        
        # Add to X-UI via sync method (run in thread to avoid blocking event loop)
        success = await asyncio.to_thread(self.add_client_sync, client_config)
        if not success:
            logger.error(f"Failed to add client {redact_email(email)}")
            return False
        
        reload_ok = await asyncio.to_thread(self.reload_xray_sync)
        if not reload_ok:
            logger.warning(f"Client {redact_email(email)} added but XRay reload failed")
            return False
        
        logger.info(f"Successfully synced user {chat_id} ({redact_email(email)})")
        return True
    
    def remove_client(self, email: str, inbound_id: int = None) -> bool:
        """Remove client from X-UI (convenience method matching XUISyncService API).
        
        Args:
            email: Client email to remove
            inbound_id: Optional inbound ID (auto-detected if None)
            
        Returns:
            True if successful
        """
        if not self.remove_client_sync(email, inbound_id):
            return False
        
        if not self.reload_xray_sync():
            logger.warning(f"Removed client {redact_email(email)} but XRay reload failed")
            return False
        
        return True
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - cleanup resources."""
        if self.api:
            await self.api.close()
