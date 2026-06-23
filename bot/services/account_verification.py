"""Account verification service for detecting real users vs bots."""

import logging
from typing import Tuple, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class VerificationResult:
    """Result of account verification."""
    is_realistic: bool
    signals: List[str]
    confidence: str  # 'high', 'medium', 'low'


class AccountVerificationService:
    """Service for verifying user accounts to distinguish real users from bots.

    Checks multiple signals:
    - Has username
    - Has profile photos
    - Has bio (description)
    - Account age (if available)

    Auto-approves if ANY signal is present.
    """

    def __init__(self, bot):
        """Initialize verification service.

        Args:
            bot: Bot instance with get_chat and get_user_profile_photos methods
        """
        self.bot = bot

    def verify_account(self, chat_id: str) -> VerificationResult:
        """Verify if account looks realistic (not a bot).

        Args:
            chat_id: User's chat ID

        Returns:
            VerificationResult with is_realistic flag and detected signals
        """
        signals = []
        confidence = 'low'

        try:
            # Get chat info (includes username, bio, etc.)
            chat_info = self.bot.get_chat(chat_id)

            if not chat_info:
                logger.warning(f"Failed to get chat info for {chat_id}")
                return VerificationResult(
                    is_realistic=False,
                    signals=['error_getting_info'],
                    confidence='low'
                )

            # Signal 1: Has username
            if chat_info.get('username'):
                signals.append('username')

            # Signal 2: Has bio (description)
            if chat_info.get('bio'):
                signals.append('bio')

            # Signal 3: Has profile photos
            try:
                photos = self.bot.get_user_profile_photos(chat_id, limit=1)
                if photos and photos.get('total_count', 0) > 0:
                    signals.append('photos')
            except Exception as e:
                logger.debug(f"Failed to get photos for {chat_id}: {e}")

            # Calculate confidence
            is_realistic = len(signals) > 0

            if len(signals) >= 2:
                confidence = 'high'
            elif len(signals) == 1:
                confidence = 'medium'

            logger.info(
                f"Account verification for {chat_id}: "
                f"realistic={is_realistic}, signals={signals}, confidence={confidence}"
            )

            return VerificationResult(
                is_realistic=is_realistic,
                signals=signals,
                confidence=confidence
            )

        except Exception as e:
            logger.error(f"Error during account verification for {chat_id}: {e}")
            return VerificationResult(
                is_realistic=False,
                signals=['verification_error'],
                confidence='low'
            )

    def get_rejection_reason(self, result: VerificationResult, lang: str = 'ru') -> str:
        """Get human-readable rejection reason.

        Args:
            result: Verification result
            lang: Language code

        Returns:
            Rejection reason text
        """
        if lang == 'ru':
            if 'verification_error' in result.signals:
                return "Ошибка при проверке аккаунта. Попробуй позже."
            if 'error_getting_info' in result.signals:
                return "Не удалось получить информацию об аккаунте."

            return (
                "Аккаунт выглядит как новый или бот.\n\n"
                "Добавь @username, фото или описание профиля — "
                "и мы одобрим автоматически."
            )
        else:
            if 'verification_error' in result.signals:
                return "Error verifying account. Try later."
            if 'error_getting_info' in result.signals:
                return "Could not get account information."

            return (
                "Account looks new or like a bot.\n\n"
                "Add @username, photo or bio — "
                "and we'll auto-approve."
            )
