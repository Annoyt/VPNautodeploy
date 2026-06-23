"""VPN Bot entry point"""

import logging
import signal
import sys
from pathlib import Path
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.config import Settings
from bot.core import Bot
from bot.handlers import (
    CommandHandler,
    CallbackHandler,
    AdminHandler,
    MessageHandler,
    ForumHandler
)
from bot.core.web_server import WebAppServer

# Phase 1+ imports
from bot.services.xui_service import XUIService
from bot.services.notifications import NotificationService
from bot.monitoring.alerts import HealthChecker, AlertManager

logger = logging.getLogger(__name__)


def setup_logging() -> None:
    """Configure logging for the bot.

    Writes the same stream both to stdout (so `docker logs` still works)
    and to /var/log/vpn-bot/bot.log (so the dashboard can show the tail
    without invoking docker from inside the container). Rotates the file
    at ~10 MB, keeps 3 backups.
    """
    import os
    from logging.handlers import RotatingFileHandler

    handlers = [logging.StreamHandler(sys.stdout)]

    log_dir = os.environ.get("VPN_BOT_LOG_DIR", "/var/log/vpn-bot")
    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "bot.log")
        file_handler = RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=3,
            encoding="utf-8",
        )
        handlers.append(file_handler)
    except Exception as e:
        # Non-fatal: stdout still works; the dashboard /logs endpoint
        # will just return an empty list.
        print(f"[setup_logging] file handler unavailable ({e}), stdout only", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=handlers,
    )


def validate_config(config: Settings) -> bool:
    """Validate required configuration.
    
    Args:
        config: Settings instance
        
    Returns:
        True if valid
    """
    errors = config.validate()
    if errors:
        for error in errors:
            logger.error(f"Config error: {error}")
        return False
    return True


def register_handlers(bot: Bot, config: Settings) -> None:
    """Register all handlers in correct order.
    
    Order matters! More specific handlers first.
    
    Args:
        bot: Bot instance
        config: Settings configuration
    """
    db = bot.db

    # Payment handler — owns /buy, the buy_plan:N callback, and the
    # two payment events (pre_checkout_query / successful_payment).
    # Must run BEFORE CommandHandler so /buy isn't bounced as
    # "unknown command", and BEFORE CallbackHandler so the buy_plan
    # callback doesn't get routed through the generic dispatcher.
    from bot.handlers.payments import PaymentHandler
    bot.register_handler(PaymentHandler(bot, db, config))
    logger.debug("Registered PaymentHandler")

    # AI handler — owns /ai, /ai_reset and free-text inside TOPIC_AI.
    # Must run BEFORE AdminHandler/CommandHandler so /ai isn't swallowed
    # by the generic command router.
    from bot.handlers.ai_handler import AIHandler
    bot.register_handler(AIHandler(bot, db, config))
    logger.debug("Registered AIHandler")

    # Admin handler - catches admin commands first
    bot.register_handler(AdminHandler(bot, db, config))
    logger.debug("Registered AdminHandler")
    
    # Command handler - catches all /commands
    bot.register_handler(CommandHandler(bot, db, config))
    logger.debug("Registered CommandHandler")
    
    # Callback handler - catches inline button clicks
    bot.register_handler(CallbackHandler(bot, db, config))
    logger.debug("Registered CallbackHandler")
    
    # Message handler - catches regular text messages
    bot.register_handler(MessageHandler(bot, db, config))
    logger.debug("Registered MessageHandler")
    
    # Forum handler - only if forum mode enabled
    if config.FORUM_ENABLED:
        bot.register_handler(ForumHandler(bot, db, config))
        logger.debug("Registered ForumHandler")
    
    logger.info(f"Registered {len(bot.handlers)} handlers")


def init_services(bot: Bot, config: Settings) -> dict:
    """Initialize all services.
    
    Args:
        bot: Bot instance
        config: Settings configuration
        
    Returns:
        Dict with service instances
    """
    services = {}
    
    # Phase 1: X-UI Service (HTTP API + DB fallback)
    try:
        services['xui'] = XUIService(config)
        logger.info("X-UI Service initialized")
        
        # Validate DB path and auto-sync clients on startup
        if services['xui'] and services['xui'].db:
            if services['xui'].validate_db_path_sync():
                sync_result = services['xui'].sync_all_clients_from_bot_db(bot.db)
                logger.info(f"X-UI startup sync result: {sync_result}")
            else:
                logger.error(
                    "X-UI DB path validation FAILED. "
                    "The bot may be writing to a different database than the X-UI container. "
                    "Check XUI_DB_PATH environment variable."
                )
    except Exception as e:
        logger.error(f"Failed to init X-UI Service: {e}")
        services['xui'] = None
    
    # Phase 4: Notification Service
    try:
        services['notifications'] = NotificationService(bot, bot.db, config)
        services['notifications'].start_scheduler()
        logger.info("Notification Service started")
    except Exception as e:
        logger.error(f"Failed to start Notification Service: {e}")
        services['notifications'] = None
    
    # Phase 6: Health Checker & Alert Manager
    try:
        if services['xui']:
            services['health'] = HealthChecker(bot.db, services['xui'])
            services['alerts'] = AlertManager(bot, config)
            logger.info("Health monitoring initialized")
    except Exception as e:
        logger.error(f"Failed to init health monitoring: {e}")
        services['health'] = None
        services['alerts'] = None
    
    return services


def cleanup_services(services: dict) -> None:
    """Cleanup all services on shutdown."""
    logger.info("Cleaning up services...")
    
    # Stop notification scheduler
    if services.get('notifications'):
        try:
            services['notifications'].stop_scheduler()
            logger.info("Notification scheduler stopped")
        except Exception as e:
            logger.error(f"Error stopping scheduler: {e}")
    
    # Close X-UI API session
    if services.get('xui'):
        try:
            if hasattr(services['xui'], 'api') and services['xui'].api:
                session = getattr(services['xui'].api, 'session', None)
                if session:
                    import asyncio
                    try:
                        asyncio.run(session.close())
                        logger.info("X-UI API session closed")
                    except Exception as e:
                        logger.warning(f"Could not close X-UI session: {e}")
                else:
                    logger.info("X-UI Service cleanup")
        except Exception as e:
            logger.error(f"Error closing X-UI Service: {e}")


def main() -> int:
    """Main entry point.
    
    Returns:
        Exit code (0 for success, 1 for error)
    """
    load_dotenv()
    setup_logging()
    
    logger.info("=" * 50)
    logger.info("VPN Bot Starting...")
    logger.info("=" * 50)
    
    # Load and validate config
    config = Settings()
    if not validate_config(config):
        logger.error("Invalid configuration, exiting")
        return 1
    
    logger.info(f"Mode: {config.MODE}")
    logger.info(f"Forum enabled: {config.FORUM_ENABLED}")
    
    # Create bot
    try:
        bot = Bot(config.BOT_TOKEN, config)
        logger.info("Bot instance created")
    except Exception as e:
        logger.exception(f"Failed to create bot: {e}")
        return 1
    
    # Initialize services (Phase 1, 4, 6)
    try:
        services = init_services(bot, config)
        bot.services = services  # Store for access from handlers
    except Exception as e:
        logger.exception(f"Failed to initialize services: {e}")
        return 1
    
    # Register handlers
    try:
        register_handlers(bot, config)
    except Exception as e:
        logger.exception(f"Failed to register handlers: {e}")
        return 1

    # Auto-create any missing forum topics (Requests / AI / ...) and
    # restore their thread ids on subsequent boots. Non-fatal: if the
    # bot isn't a topic-manager admin yet, we just log and move on.
    if config.FORUM_ENABLED:
        try:
            from bot.services.forum_bootstrap import bootstrap_forum_topics
            created = bootstrap_forum_topics(bot, config)
            if created:
                logger.info(f"Forum topic bootstrap: {created}")
        except Exception as e:
            logger.warning(f"Forum topic bootstrap failed (non-fatal): {e}")
    
    # Setup signal handlers
    def signal_handler(signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        cleanup_services(services)
        bot.stop()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Start web app server
    logger.info("Starting WebApp server...")
    web_server = WebAppServer(
        config, 
        bot.db, 
        xui_service=services.get('xui'),
        cluster_manager=services.get('cluster'),
        health_checker=services.get('health'),
        notification_service=services.get('notifications'),
        bot_instance=bot,
    )
    web_server.run_in_thread()
    
    # Start polling (blocks until shutdown)
    logger.info("Starting polling loop...")
    try:
        bot.start()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.exception(f"Bot error: {e}")
        return 1
    finally:
        logger.info("Shutting down...")
        cleanup_services(services)
        bot.stop()
    
    logger.info("Bot stopped")
    return 0


if __name__ == '__main__':
    sys.exit(main())
