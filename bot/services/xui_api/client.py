"""Async HTTP API client for 3X-UI panel."""

import asyncio
import json
import logging
from typing import Optional, List, Dict, Any
import aiohttp
from dataclasses import dataclass

from bot.config import TIMEOUT_XUI_API

logger = logging.getLogger(__name__)


@dataclass
class XUIClientConfig:
    """Configuration for X-UI API client."""
    base_url: str = "http://127.0.0.1:2026"
    username: str = "admin"
    password: str = "admin"
    timeout: int = TIMEOUT_XUI_API
    base_path: str = "/this_is_fine"
    api_path: str = "/this_is_fine/panel/api/inbounds"
    
    def __repr__(self) -> str:
        """Safe repr that masks password."""
        return (f"XUIClientConfig(base_url='{self.base_url}', "
                f"username='{self.username}', password='***', "
                f"timeout={self.timeout}, base_path='{self.base_path}', "
                f"api_path='{self.api_path}')")


class XUIAPIClient:
    """Async HTTP API client for 3X-UI panel.
    
    Replaces docker exec calls with clean HTTP API.
    """
    
    def __init__(self, config: XUIClientConfig):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None
        self._cookies: Optional[aiohttp.CookieJar] = None
        # 3x-ui's newer SPA login requires a CSRF token issued by the
        # panel HTML and echoed back via X-CSRF-TOKEN header. Held
        # across requests so subsequent calls reuse it.
        self._csrf_token: Optional[str] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session for the current event loop.

        Two subtleties:

        * The panel is addressed by IP (e.g. http://84.75.76.109:2026) and
          aiohttp's default CookieJar refuses cookies from IP hosts
          (RFC 6265); without the session cookie the login POST 403s. So
          the jar is created with ``unsafe=True``.
        * The bot's sync wrappers run each call in a fresh ``asyncio.run``
          loop. An aiohttp session is bound to the loop it was created on,
          so a session kept from a previous (now-closed) loop must be
          discarded — reusing it raises "Event loop is closed". We detect a
          loop change and rebuild, forcing a re-login (the old cookie jar
          goes with the old session).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        stale = (
            self.session is None
            or self.session.closed
            or (loop is not None and getattr(self.session, "_loop", None) is not loop)
        )
        if stale:
            self.session = None
            self._csrf_token = None
            timeout = aiohttp.ClientTimeout(total=self.config.timeout)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=aiohttp.CookieJar(unsafe=True),
            )
        return self.session

    async def _fetch_csrf(self) -> Optional[str]:
        """GET the panel HTML to grab the ``csrf-token`` meta tag.

        Side effect: the panel sets a session cookie at the same time,
        which we keep on ``self.session`` for subsequent requests.
        """
        import re
        session = await self._get_session()
        url = f"{self.config.base_url}{self.config.base_path}/"
        try:
            async with session.get(url) as r:
                if r.status != 200:
                    logger.warning(f"XUI panel HTML fetch failed: {r.status}")
                    return None
                text = await r.text()
        except Exception as e:
            logger.warning(f"XUI panel HTML fetch error: {e}")
            return None
        m = re.search(r'csrf-token"\s*content="([^"]+)"', text)
        if not m:
            logger.warning("XUI panel HTML had no csrf-token meta")
            return None
        token = m.group(1)
        self._csrf_token = token
        return token

    async def login(self) -> bool:
        """Login to 3X-UI panel.

        Modern 3x-ui needs JSON body + X-CSRF-TOKEN. The token comes
        from the meta tag on the panel landing page; that GET also
        seeds the session cookie this client will use.

        Returns:
            True if login successful, False otherwise.
        """
        try:
            csrf = await self._fetch_csrf()
            if not csrf:
                return False
            session = await self._get_session()
            url = f"{self.config.base_url}{self.config.base_path}/login"
            headers = {
                "X-CSRF-TOKEN": csrf,
                "Content-Type": "application/json",
            }
            data = {
                "username": self.config.username,
                "password": self.config.password,
            }
            async with session.post(url, json=data, headers=headers) as response:
                if response.status == 200:
                    body = await response.json(content_type=None)
                    if body.get("success"):
                        logger.info("XUI API login successful")
                        return True
                    logger.warning(f"XUI API login refused: {body.get('msg')}")
                    return False
                logger.warning(f"XUI API login failed: HTTP {response.status}")
                return False
        except Exception as e:
            logger.error(f"XUI API login error: {e}")
            return False

    async def get_online_clients(self) -> list:
        """Return the list of currently-online client emails from the
        3x-ui ``/panel/api/inbounds/onlines`` endpoint.

        Returns an empty list (and logs a warning) if the API call
        fails; callers should treat that as "data unavailable", not
        as "no one is online".
        """
        if not self._csrf_token:
            # Lazy login on first call.
            if not await self.login():
                return []
        session = await self._get_session()
        url = f"{self.config.base_url}{self.config.base_path}/panel/api/inbounds/onlines"
        headers = {"X-CSRF-TOKEN": self._csrf_token or ""}
        try:
            async with session.post(url, headers=headers) as r:
                if r.status == 401:
                    # Session expired — re-login + retry once.
                    if not await self.login():
                        return []
                    headers["X-CSRF-TOKEN"] = self._csrf_token or ""
                    async with session.post(url, headers=headers) as r2:
                        if r2.status != 200:
                            return []
                        body = await r2.json(content_type=None)
                else:
                    if r.status == 404:
                        # This 3x-ui version does not expose an onlines
                        # endpoint; downstream sources (xray access.log,
                        # tcp-stats) will provide the data instead.
                        logger.debug("get_online_clients: 404 (endpoint not available)")
                        return []
                    if r.status != 200:
                        logger.warning(f"get_online_clients: HTTP {r.status}")
                        return []
                    body = await r.json(content_type=None)
            if not body.get("success"):
                return []
            obj = body.get("obj") or []
            # Defensive: ensure list of strings.
            return [str(x) for x in obj if isinstance(x, (str, bytes))]
        except Exception as e:
            logger.warning(f"get_online_clients error: {e}")
            return []

    def get_online_clients_sync(self) -> list:
        """Sync wrapper for get_online_clients.

        Safely handles both sync context (no running loop) and async context
        (inside aiohttp/web server). Uses a separate thread with a new loop
        when called from an async context to avoid "Event loop is closed" errors.
        """
        try:
            loop = asyncio.get_running_loop()
            # We're inside an async context - run in a separate thread with new loop
            import concurrent.futures
            import threading

            result_container = []
            exception_container = []

            def run_in_new_loop():
                try:
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        result = new_loop.run_until_complete(self.get_online_clients())
                        result_container.append(result)
                    finally:
                        new_loop.close()
                except Exception as e:
                    exception_container.append(e)

            thread = threading.Thread(target=run_in_new_loop, daemon=True)
            thread.start()
            thread.join(timeout=5.0)  # 5 second timeout

            if exception_container:
                raise exception_container[0]
            if not result_container:
                logger.warning("get_online_clients_sync: timeout or no result")
                return []
            return result_container[0]

        except RuntimeError:
            # No running loop - safe to use asyncio.run()
            return asyncio.run(self.get_online_clients())

    async def _ensure_auth(self) -> bool:
        """Make sure we hold a session cookie + CSRF token.

        The panel API endpoints are session-authenticated: an
        unauthenticated call is *not* answered with 401 but with a 404
        (the panel hides the API behind the login gate), so callers must
        log in proactively rather than relying on a 401 retry.

        Touch ``_get_session`` first: if the event loop changed since the
        last call it rebuilds the session and clears ``_csrf_token``, so
        the check below correctly forces a re-login instead of reusing a
        token that belonged to a now-discarded cookie jar.
        """
        await self._get_session()
        if self._csrf_token:
            return True
        return await self.login()

    def _auth_headers(self, extra: Optional[dict] = None) -> dict:
        """Headers every authenticated API call needs (CSRF token)."""
        headers = {"X-CSRF-TOKEN": self._csrf_token or ""}
        if extra:
            headers.update(extra)
        return headers

    async def _authenticated_request(self, method: str, url: str, **kwargs) -> aiohttp.ClientResponse:
        """Make authenticated request, logging in first when needed.

        The panel returns 404 (not 401) for API calls made without a
        session, so we log in *before* the first request and only fall
        back to a re-login on 401/403 (expired session / stale CSRF).

        Args:
            method: HTTP method (get, post, etc.)
            url: Request URL
            **kwargs: Additional arguments for the request

        Returns:
            ClientResponse object
        """
        await self._ensure_auth()
        session = await self._get_session()

        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("X-CSRF-TOKEN", self._csrf_token or "")

        response = await session.request(method, url, headers=headers, **kwargs)

        # 401/403 → session expired or CSRF rotated. Re-login and retry once.
        if response.status in (401, 403):
            logger.warning(f"XUI API returned {response.status}, attempting re-login...")
            if await self.login():
                headers["X-CSRF-TOKEN"] = self._csrf_token or ""
                response = await session.request(method, url, headers=headers, **kwargs)
            else:
                logger.error("XUI API re-login failed")

        return response
    
    async def get_inbounds(self) -> List[Dict[str, Any]]:
        """Get all inbounds from 3X-UI.
        
        Returns:
            List of inbound configurations.
        """
        try:
            url = f"{self.config.base_url}{self.config.api_path}/list"
            
            response = await self._authenticated_request('GET', url)
            
            if response.status == 200:
                data = await response.json()
                return data.get("obj", [])
            
            logger.warning(f"Failed to get inbounds: {response.status}")
            return []
                
        except Exception as e:
            logger.error(f"Error getting inbounds: {e}")
            return []
    
    async def get_inbound(self, inbound_id: int) -> Optional[Dict[str, Any]]:
        """Get specific inbound by ID.
        
        Args:
            inbound_id: The inbound ID.
            
        Returns:
            Inbound configuration or None.
        """
        try:
            url = f"{self.config.base_url}{self.config.api_path}/get/{inbound_id}"
            
            response = await self._authenticated_request('GET', url)
            
            if response.status == 200:
                data = await response.json()
                return data.get("obj")
            
            logger.warning(f"Failed to get inbound {inbound_id}: {response.status}")
            return None
                
        except Exception as e:
            logger.error(f"Error getting inbound {inbound_id}: {e}")
            return None
    
    async def get_client_traffic(self, email: str) -> Optional[Dict[str, Any]]:
        """Get traffic stats for specific client by email.
        
        Args:
            email: Client email.
            
        Returns:
            Traffic stats or None.
        """
        try:
            # Get all inbounds and find client stats
            inbounds = await self.get_inbounds()
            
            for inbound in inbounds:
                client_stats = inbound.get("clientStats", [])
                for stats in client_stats:
                    if stats.get("email") == email:
                        return {
                            "email": email,
                            "up": stats.get("up", 0),
                            "down": stats.get("down", 0),
                            "total": stats.get("total", 0),
                        }
            
            return None
            
        except Exception as e:
            logger.error(f"Error getting client traffic for {email}: {e}")
            return None
    
    async def get_all_clients_stats(self) -> Dict[str, Dict[str, int]]:
        """Get traffic stats for all clients.
        
        Returns:
            Dict mapping email to traffic stats.
        """
        try:
            inbounds = await self.get_inbounds()
            stats = {}
            
            for inbound in inbounds:
                # Try clientStats first (has traffic data)
                client_stats = inbound.get("clientStats", [])
                if client_stats:
                    for client_stat in client_stats:
                        email = client_stat.get("email")
                        if email:
                            stats[email] = {
                                "up": client_stat.get("up", 0),
                                "down": client_stat.get("down", 0),
                                "total": client_stat.get("total", 0),
                            }
                else:
                    # Fallback: parse clients from settings JSON
                    import json
                    settings_str = inbound.get("settings", "{}")
                    try:
                        settings = json.loads(settings_str)
                        clients = settings.get("clients", [])
                        for client in clients:
                            email = client.get("email")
                            if email:
                                stats[email] = {
                                    "up": 0,
                                    "down": 0,
                                    "total": 0,
                                }
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse settings JSON for inbound {inbound.get('id')}")
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting all clients stats: {e}")
            return {}
    
    async def update_inbound(self, inbound_id: int, inbound_data: dict) -> bool:
        """Update inbound configuration via API.
        
        Args:
            inbound_id: The inbound ID to update
            inbound_data: Complete inbound configuration dict
            
        Returns:
            True if successful
        """
        try:
            url = f"{self.config.base_url}{self.config.api_path}/update/{inbound_id}"
            
            response = await self._authenticated_request('POST', url, json=inbound_data)
            
            if response.status == 200:
                data = await response.json()
                if data.get("success"):
                    logger.info(f"Inbound {inbound_id} updated successfully")
                    return True
                else:
                    logger.warning(f"API returned error: {data.get('msg', 'Unknown error')}")
                    return False
            else:
                logger.warning(f"Failed to update inbound {inbound_id}: {response.status}")
                return False
                    
        except Exception as e:
            logger.error(f"Error updating inbound {inbound_id}: {e}")
            return False
    
    def _clients_url(self, suffix: str) -> str:
        """Build a URL under the v3.4.0 relational clients namespace
        (``/panel/api/clients``), which is separate from the inbounds
        API path used for listing."""
        return f"{self.config.base_url}{self.config.base_path}/panel/api/clients{suffix}"

    @staticmethod
    def _to_v34_client(cfg: dict) -> dict:
        """Map the bot's classic 3x-ui client dict to the v3.4.0 shape.

        The bot builds clients in the old xray/settings style (``id`` =
        UUID, camelCase ``limitIp``/``totalGB``/``expiryTime``). 3x-ui
        v3.4.0's ``clients/add`` reads the UUID from the ``id`` field
        (verified: sending ``uuid`` is ignored and a fresh UUID is
        generated, sending ``id`` preserves it) and honours ``password``
        (the SS-2022 per-user key). Preserving both keeps the bot's
        already-generated subscription links valid.
        """
        def _int(*keys):
            for k in keys:
                v = cfg.get(k)
                if v is not None and v != "":
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return 0
            return 0
        return {
            "email": cfg.get("email", ""),
            # v3.4.0 add binds the client UUID from "id", not "uuid".
            "id": cfg.get("id") or cfg.get("uuid") or "",
            "password": cfg.get("password", ""),
            "flow": cfg.get("flow", ""),
            "security": cfg.get("security", "auto"),
            "limit_ip": _int("limitIp", "limit_ip"),
            "total_gb": _int("totalGB", "total_gb"),
            "expiry_time": _int("expiryTime", "expiry_time"),
            "enable": bool(cfg.get("enable", True)),
            "tg_id": _int("tgId", "tg_id"),
            "sub_id": cfg.get("subId") or cfg.get("sub_id") or "",
            "comment": cfg.get("comment", ""),
            "reset": _int("reset"),
        }

    async def add_client(self, inbound_ids, client_config: dict) -> bool:
        """Attach a client to one or more inbounds (3x-ui v3.4.0).

        v3.4.0 manages clients relationally: a client is a first-class
        record keyed by email and linked to inbounds. This POSTs to
        ``/panel/api/clients/add`` with ``{client, inboundIds}``. Because
        re-adding the same email fails ("email already in use"), all
        target inbounds (primary + WS mirror, etc.) must be passed here
        together rather than in separate calls.

        Args:
            inbound_ids: Inbound ID or list of IDs to attach the client to.
            client_config: The bot's classic client dict (id/email/...).

        Returns:
            True if the panel reported success.
        """
        if isinstance(inbound_ids, int):
            inbound_ids = [inbound_ids]
        try:
            await self._ensure_auth()
            session = await self._get_session()
            payload = {
                "client": self._to_v34_client(client_config),
                "inboundIds": [int(i) for i in inbound_ids],
            }
            async with session.post(self._clients_url("/add"), json=payload,
                                    headers=self._auth_headers()) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if data.get("success"):
                        logger.info(f"Client added to inbounds {payload['inboundIds']}")
                        return True
                    logger.warning(f"addClient error: {data.get('msg', 'unknown')}")
                    return False
                logger.warning(f"Failed to add client: HTTP {response.status}")
                return False
        except Exception as e:
            logger.error(f"Error adding client: {e}")
            return False

    async def del_client_by_email(self, email: str) -> bool:
        """Delete a client everywhere by email (3x-ui v3.4.0).

        v3.4.0 has no per-inbound ``delClient``; clients are first-class
        and removed via ``POST /panel/api/clients/bulkDel`` with a list of
        emails. Detaches from every inbound the client was attached to.

        Returns:
            True if the panel deleted at least one client.
        """
        try:
            await self._ensure_auth()
            session = await self._get_session()
            async with session.post(self._clients_url("/bulkDel"),
                                    json={"emails": [email]},
                                    headers=self._auth_headers()) as response:
                if response.status == 200:
                    data = await response.json(content_type=None)
                    if data.get("success"):
                        deleted = (data.get("obj") or {}).get("deleted", 0)
                        logger.info(f"Deleted client {email}: {deleted}")
                        return deleted > 0
                    logger.warning(f"bulkDel error: {data.get('msg', 'unknown')}")
                    return False
                logger.warning(f"Failed to delete client: HTTP {response.status}")
                return False
        except Exception as e:
            logger.error(f"Error deleting client: {e}")
            return False

    async def close(self):
        """Close the HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
