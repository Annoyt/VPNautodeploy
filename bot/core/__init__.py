"""Bot core package"""

from bot.core.bot import Bot
from bot.core.database import Database, db_transaction
from bot.core.state_machine import StateMachine
from bot.core.telegram_client import TelegramClient
from bot.core.polling import PollingService

# Import User from models (moved to avoid circular imports)
from bot.models import User

__all__ = [
    'Bot',
    'Database',
    'User',
    'StateMachine',
    'TelegramClient',
    'PollingService',
    'db_transaction',
]

