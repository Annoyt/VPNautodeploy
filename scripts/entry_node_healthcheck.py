#!/usr/bin/env python3
"""Entry Node Health Check and Failover Manager.

Runs on Entry Node to:
1. Monitor all Exit Nodes (health + performance)
2. Detect failures and trigger failover
3. Maintain user-to-exit routing table
4. Report events to Bot API

Usage:
    python3 entry_node_healthcheck.py --config /etc/vpn/entry-node.yml
    
Systemd service:
    systemctl enable entry-health-monitor
    systemctl start entry-health-monitor
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import aiohttp

# Setup logging
log_path = os.getenv("LOG_PATH", "/var/log/entry-health-monitor.log")
handlers = [logging.StreamHandler()]

try:
    # Try to create file handler
    file_handler = logging.FileHandler(log_path)
    handlers.append(file_handler)
except (PermissionError, OSError) as e:
    # Fall back to stdout only
    print(f"Warning: Cannot write to {log_path}: {e}. Using stdout only.", file=sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=handlers
)
logger = logging.getLogger(__name__)


@dataclass
class ExitNodeConfig:
    """Configuration for an Exit Node."""
    node_id: str
    host: str
    api_port: int = 8081
    health_endpoint: str = "/health"
    weight: int = 100
    is_primary: bool = False
    
    @property
    def api_url(self) -> str:
        return f"http://{self.host}:{self.api_port}"


@dataclass
class UserConnection:
    """Active user connection."""
    user_id: str
    chat_id: str
    email: str
    current_exit: str
    connected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ExitNodeHealthChecker:
    """Checks health and performance of Exit Nodes."""
    
    def __init__(self, session: aiohttp.ClientSession, timeout: int = 10):
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._statuses: Dict[str, dict] = {}
    
    async def check_node(self, node: ExitNodeConfig) -> Optional[dict]:
        """Check health of a single Exit Node."""
        try:
            url = f"{node.api_url}{node.health_endpoint}"
            async with self.session.get(url, timeout=self.timeout) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    data['node_id'] = node.node_id
                    data['is_healthy'] = True
                    data['checked_at'] = datetime.now(timezone.utc).isoformat()
                    self._statuses[node.node_id] = data
                    return data
                else:
                    logger.warning(f"Node {node.node_id} returned {resp.status}")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"Timeout checking node {node.node_id}")
            return None
        except Exception as e:
            logger.error(f"Error checking node {node.node_id}: {e}")
            return None
    
    async def check_all_nodes(self, nodes: List[ExitNodeConfig]) -> Dict[str, dict]:
        """Check all Exit Nodes concurrently."""
        tasks = [self.check_node(node) for node in nodes]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        statuses = {}
        for node, result in zip(nodes, results):
            if isinstance(result, dict):
                statuses[node.node_id] = result
            else:
                # Mark as unhealthy
                statuses[node.node_id] = {
                    'node_id': node.node_id,
                    'is_healthy': False,
                    'error': str(result) if result else "Unknown error",
                    'checked_at': datetime.now(timezone.utc).isoformat(),
                }
        
        return statuses
    
    async def get_performance_metrics(self, node: ExitNodeConfig) -> Optional[dict]:
        """Get performance metrics from Exit Node."""
        try:
            url = f"{node.api_url}/metrics"
            async with self.session.get(url, timeout=self.timeout) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            logger.debug(f"Could not get metrics from {node.node_id}: {e}")
        return None
    
    def get_cached_status(self, node_id: str) -> Optional[dict]:
        """Get cached status for a node."""
        return self._statuses.get(node_id)


class EntryNodeFailoverManager:
    """Manages failover decisions and user routing."""
    
    def __init__(
        self,
        bot_api_url: str,
        bot_api_secret: str,
        session: aiohttp.ClientSession,
    ):
        self.bot_api_url = bot_api_url
        self.bot_api_secret = bot_api_secret
        self.session = session
        
        # User routing: user_id -> UserConnection
        self._user_routes: Dict[str, UserConnection] = {}
        
        # Recent failovers to prevent flapping
        self._recent_failovers: Set[str] = set()
    
    def register_user(self, user_id: str, chat_id: str, email: str, exit_node: str) -> None:
        """Register a user connection."""
        self._user_routes[user_id] = UserConnection(
            user_id=user_id,
            chat_id=chat_id,
            email=email,
            current_exit=exit_node,
        )
        logger.info(f"Registered user {user_id} on {exit_node}")
    
    def unregister_user(self, user_id: str) -> None:
        """Unregister a user connection."""
        if user_id in self._user_routes:
            del self._user_routes[user_id]
            logger.info(f"Unregistered user {user_id}")
    
    async def get_exit_statuses_from_bot(self) -> Dict[str, dict]:
        """Get Exit Node statuses from Bot API."""
        try:
            url = f"{self.bot_api_url}/exit/nodes/status"
            headers = {"X-API-Secret": self.bot_api_secret}
            
            async with self.session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get('nodes', {})
                else:
                    logger.error(f"Bot API returned {resp.status}")
                    return {}
        except Exception as e:
            logger.error(f"Error fetching Exit Node statuses: {e}")
            return {}
    
    def select_best_exit(
        self,
        exit_statuses: Dict[str, dict],
        current_exit: Optional[str] = None,
    ) -> Optional[str]:
        """Select best available Exit Node.
        
        Priority:
        1. Healthy, not throttled
        2. Healthy, throttled (if no other options)
        3. Current exit if still healthy
        """
        healthy = [
            (node_id, status) for node_id, status in exit_statuses.items()
            if status.get('is_healthy', False)
        ]
        
        if not healthy:
            logger.error("No healthy Exit Nodes available!")
            return None
        
        # Priority 1: Not throttled
        preferred = [
            (node_id, status) for node_id, status in healthy
            if not status.get('is_throttled', False)
        ]
        
        if preferred:
            # Sort by performance score (descending)
            preferred.sort(key=lambda x: x[1].get('performance_score', 0), reverse=True)
            return preferred[0][0]
        
        # Priority 2: All throttled, pick least loaded
        healthy.sort(key=lambda x: x[1].get('cpu_percent', 100))
        return healthy[0][0]
    
    async def execute_failover(
        self,
        user_id: str,
        from_exit: str,
        to_exit: str,
        reason: str,
        chat_id: str,
        exit_statuses: Optional[Dict[str, dict]] = None,
    ) -> bool:
        """Execute failover and report to Bot."""
        if user_id in self._recent_failovers:
            logger.info(f"Skipping failover for {user_id} (recent failover)")
            return False
        
        try:
            # 1. Update local routing
            if user_id in self._user_routes:
                self._user_routes[user_id].current_exit = to_exit
            
            # 2. Update X-UI/Kaskad routing (TODO: implement based on your setup)
            await self._update_kaskad_routing(user_id, to_exit)
            
            # 3. Report to Bot API
            await self._report_failover_to_bot(
                user_id=user_id,
                chat_id=chat_id,
                from_exit=from_exit,
                to_exit=to_exit,
                reason=reason,
                exit_statuses=exit_statuses,
            )
            
            # 4. Mark recent failover
            self._recent_failovers.add(user_id)
            asyncio.create_task(self._clear_failover_flag(user_id, delay=300))
            
            logger.info(f"Failover executed: {user_id} from {from_exit} to {to_exit}")
            return True
            
        except Exception as e:
            logger.error(f"Failover failed for {user_id}: {e}")
            return False
    
    async def _update_kaskad_routing(self, user_id: str, new_exit: str) -> None:
        """Update Kaskad routing for user.
        
        This is a placeholder - implement based on your Kaskad setup.
        You might need to:
        - Update iptables rules
        - Update proxy configuration
        - Reload Kaskad
        """
        logger.info(f"Updating Kaskad routing for {user_id} -> {new_exit}")
        # TODO: Implement Kaskad-specific routing update
        pass
    
    async def _report_failover_to_bot(
        self,
        user_id: str,
        chat_id: str,
        from_exit: str,
        to_exit: str,
        reason: str,
        exit_statuses: Optional[Dict[str, dict]] = None,
    ) -> None:
        """Report failover event to Bot API."""
        try:
            url = f"{self.bot_api_url}/failover/event"
            headers = {"X-API-Secret": self.bot_api_secret}
            
            # Get target exit status to check if throttled
            # Use provided statuses or fetch if not available
            statuses = exit_statuses or await self.get_exit_statuses_from_bot()
            target_status = statuses.get(to_exit, {})
            is_throttled = target_status.get('is_throttled', False)
            
            payload = {
                "user_id": user_id,
                "chat_id": chat_id,
                "from_exit": from_exit,
                "to_exit": to_exit,
                "reason": reason,
                "is_throttled_target": is_throttled,
            }
            
            async with self.session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    logger.info(f"Failover reported to bot: {result}")
                else:
                    logger.error(f"Failed to report failover: {resp.status}")
        except Exception as e:
            logger.error(f"Error reporting failover to bot: {e}")
    
    async def _clear_failover_flag(self, user_id: str, delay: int) -> None:
        """Clear recent failover flag after delay."""
        await asyncio.sleep(delay)
        self._recent_failovers.discard(user_id)
    
    async def check_and_failover_users(self) -> None:
        """Check all users and execute failovers if needed."""
        exit_statuses = await self.get_exit_statuses_from_bot()
        
        if not exit_statuses:
            logger.warning("No Exit Node statuses available, skipping failover check")
            return
        
        for user_id, connection in list(self._user_routes.items()):
            current_exit = connection.current_exit
            current_status = exit_statuses.get(current_exit, {})
            
            # Check if current exit is still preferred
            if current_status.get('is_preferred', False):
                continue  # No action needed
            
            if not current_status.get('is_healthy', False):
                logger.warning(f"User {user_id} on unhealthy exit {current_exit}")
            
            # Find better exit
            best_exit = self.select_best_exit(exit_statuses, current_exit)
            
            if best_exit and best_exit != current_exit:
                reason = "current_unhealthy" if not current_status.get('is_healthy') else "better_performance"
                
                # Check if target is throttled
                target_status = exit_statuses.get(best_exit, {})
                if target_status.get('is_throttled', False):
                    logger.warning(
                        f"Failover target {best_exit} is throttled, "
                        f"but no better options available"
                    )
                
                await self.execute_failover(
                    user_id=user_id,
                    from_exit=current_exit,
                    to_exit=best_exit,
                    reason=reason,
                    chat_id=connection.chat_id,
                    exit_statuses=exit_statuses,
                )


class EntryNodeMonitor:
    """Main Entry Node monitoring service."""
    
    def __init__(
        self,
        exit_nodes: List[ExitNodeConfig],
        bot_api_url: str,
        bot_api_secret: str,
        check_interval: int = 5,
    ):
        self.exit_nodes = exit_nodes
        self.bot_api_url = bot_api_url
        self.bot_api_secret = bot_api_secret
        self.check_interval = check_interval
        self._session: Optional[aiohttp.ClientSession] = None
        self._health_checker: Optional[ExitNodeHealthChecker] = None
        self._failover_manager: Optional[EntryNodeFailoverManager] = None
        self._running = False
    
    async def start(self) -> None:
        """Start monitoring service."""
        self._session = aiohttp.ClientSession()
        self._health_checker = ExitNodeHealthChecker(self._session)
        self._failover_manager = EntryNodeFailoverManager(
            bot_api_url=self.bot_api_url,
            bot_api_secret=self.bot_api_secret,
            session=self._session,
        )
        
        self._running = True
        logger.info("Entry Node Monitor started")
        
        try:
            while self._running:
                await self._check_cycle()
                await asyncio.sleep(self.check_interval)
        except asyncio.CancelledError:
            logger.info("Monitor cancelled")
        finally:
            await self.stop()
    
    async def stop(self) -> None:
        """Stop monitoring service."""
        self._running = False
        if self._session:
            await self._session.close()
        logger.info("Entry Node Monitor stopped")
    
    async def _check_cycle(self) -> None:
        """Single check cycle."""
        try:
            # Check all Exit Nodes
            statuses = await self._health_checker.check_all_nodes(self.exit_nodes)
            
            # Log status summary
            healthy_count = sum(1 for s in statuses.values() if s.get('is_healthy'))
            throttled_count = sum(1 for s in statuses.values() if s.get('is_throttled'))
            
            logger.info(
                f"Status check: {healthy_count}/{len(statuses)} healthy, "
                f"{throttled_count} throttled"
            )
            
            # Check for failovers
            await self._failover_manager.check_and_failover_users()
            
        except Exception as e:
            logger.error(f"Error in check cycle: {e}")


def load_config(config_path: str) -> dict:
    """Load configuration from file."""
    if not os.path.exists(config_path):
        # No config file — read the operator's host from env vars. The
        # actual exit IP lives in the operator's .env / nodes.json,
        # NOT in this default which ships in the public repo.
        return {
            "exit_nodes": [
                {
                    "node_id": "exit-primary",
                    "host": os.getenv("EXIT_NODE_IP", ""),
                    "api_port": int(os.getenv("EXIT_NODE_API_PORT", "8081")),
                    "is_primary": True,
                },
            ],
            "bot_api": {
                "url": os.getenv("BOT_API_URL", "http://localhost:8081"),
                "secret": os.getenv("BOT_API_SECRET", ""),
            },
            "check_interval": int(os.getenv("HEALTHCHECK_INTERVAL", "5")),
        }
    
    with open(config_path, 'r') as f:
        if config_path.endswith('.json'):
            return json.load(f)
        else:
            # YAML support would need pyyaml
            raise NotImplementedError("YAML config not yet supported")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Entry Node Health Monitor")
    parser.add_argument(
        "--config",
        default="/etc/vpn/entry-node.yml",
        help="Path to configuration file"
    )
    parser.add_argument(
        "--bot-api",
        default=os.getenv("BOT_API_URL", "http://localhost:8081"),
        help="Bot API URL"
    )
    parser.add_argument(
        "--bot-secret",
        default=os.getenv("BOT_API_SECRET", ""),
        help="Bot API secret"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Check interval in seconds"
    )
    
    args = parser.parse_args()
    
    # Load config
    try:
        config = load_config(args.config)
    except Exception as e:
        logger.warning(f"Could not load config: {e}, using defaults")
        config = load_config("")  # Get defaults
    
    # Parse Exit Node configs
    exit_nodes = [
        ExitNodeConfig(**node_config)
        for node_config in config.get("exit_nodes", [])
    ]
    
    if not exit_nodes:
        logger.error("No Exit Nodes configured!")
        sys.exit(1)
    
    # Get Bot API config
    bot_api_url = config.get("bot_api", {}).get("url", args.bot_api)
    bot_api_secret = config.get("bot_api", {}).get("secret", args.bot_secret)
    check_interval = config.get("check_interval", args.interval)
    
    # Create and start monitor
    monitor = EntryNodeMonitor(
        exit_nodes=exit_nodes,
        bot_api_url=bot_api_url,
        bot_api_secret=bot_api_secret,
        check_interval=check_interval,
    )
    
    try:
        await monitor.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        await monitor.stop()


if __name__ == "__main__":
    asyncio.run(main())
