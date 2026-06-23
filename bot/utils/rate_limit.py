"""Simple in-memory rate limiting for API endpoints.

TODO: Migrate to Redis-based distributed rate limiting for multi-instance deployments.
"""

import time
import threading
from collections import defaultdict
from typing import Optional
import logging

logger = logging.getLogger(__name__)


class RateLimiter:
    """In-memory rate limiter using sliding window counter.

    Tracks requests per key (IP, token, etc.) within a time window.
    """

    def __init__(self):
        """Initialize rate limiter with thread-safe storage."""
        # {key: [(timestamp, count)]}
        self._requests = defaultdict(list)
        self._lock = threading.Lock()

    def is_allowed(
        self,
        key: str,
        max_requests: int = 60,
        window_seconds: int = 60
    ) -> bool:
        """Check if request is allowed under rate limit.

        Args:
            key: Identifier to rate limit (IP, token, etc.)
            max_requests: Maximum requests allowed in window
            window_seconds: Time window in seconds

        Returns:
            True if request is allowed, False if rate limited
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            # Clean old requests
            if key in self._requests:
                self._requests[key] = [
                    (ts, count) for ts, count in self._requests[key]
                    if ts > cutoff
                ]

            # Count requests in window
            current_count = sum(count for _, count in self._requests[key])

            if current_count >= max_requests:
                logger.warning(
                    f"Rate limit exceeded for {key}: "
                    f"{current_count}/{max_requests} in {window_seconds}s"
                )
                return False

            # Record this request
            self._requests[key].append((now, 1))
            return True

    def get_remaining(
        self,
        key: str,
        max_requests: int,
        window_seconds: int = 60
    ) -> int:
        """Get remaining requests before rate limit.

        Args:
            key: Identifier to check
            max_requests: Maximum requests allowed
            window_seconds: Time window in seconds

        Returns:
            Number of requests remaining
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            if key not in self._requests:
                return max_requests

            # Clean old requests first
            self._requests[key] = [
                (ts, count) for ts, count in self._requests[key]
                if ts > cutoff
            ]

            current_count = sum(count for _, count in self._requests[key])
            return max(0, max_requests - current_count)

    def reset(self, key: str) -> None:
        """Reset rate limit for a key.

        Args:
            key: Identifier to reset
        """
        with self._lock:
            if key in self._requests:
                del self._requests[key]

    def cleanup_old_entries(self, older_than_seconds: int = 3600) -> int:
        """Clean up entries older than specified time.

        Args:
            older_than_seconds: Remove entries older than this

        Returns:
            Number of entries cleaned
        """
        now = time.time()
        cutoff = now - older_than_seconds
        cleaned = 0

        with self._lock:
            keys_to_delete = []
            for key, requests in self._requests.items():
                # Remove old requests
                self._requests[key] = [
                    (ts, count) for ts, count in requests
                    if ts > cutoff
                ]

                # Mark empty keys for deletion
                if not self._requests[key]:
                    keys_to_delete.append(key)

            for key in keys_to_delete:
                del self._requests[key]
                cleaned += 1

        return cleaned


# Global rate limiter instance
_admin_rate_limiter = RateLimiter()


def check_admin_rate_limit(identifier: str) -> bool:
    """Check if admin request is allowed under rate limit.

    Rate limits: 60 requests per minute per identifier.

    Args:
        identifier: Unique identifier (IP, token, etc.)

    Returns:
        True if allowed, False if rate limited
    """
    return _admin_rate_limiter.is_allowed(
        key=identifier,
        max_requests=60,
        window_seconds=60
    )


def get_admin_rate_limit_remaining(identifier: str) -> int:
    """Get remaining admin requests before rate limit.

    Args:
        identifier: Unique identifier (IP, token, etc.)

    Returns:
        Number of requests remaining
    """
    return _admin_rate_limiter.get_remaining(
        key=identifier,
        max_requests=60,
        window_seconds=60
    )
