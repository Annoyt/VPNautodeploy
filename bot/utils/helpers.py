"""General utility functions"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def escape_markdown(text: str) -> str:
    """Escape Markdown special characters for Telegram.
    
    Args:
        text: Input text
        
    Returns:
        Escaped text
    """
    if text is None:
        return ''
    chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in chars:
        text = text.replace(char, f'\\{char}')
    return text


def escape_html(text: str) -> str:
    """Escape HTML special characters.
    
    Args:
        text: Input text
        
    Returns:
        Escaped text
    """
    if text is None:
        return ''
    return (
        text.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


def truncate_text(text: str, max_length: int = 4096) -> str:
    """Truncate text to max length for Telegram messages.
    
    Telegram message limit is 4096 characters.
    
    Args:
        text: Input text
        max_length: Maximum length
        
    Returns:
        Truncated text
    """
    if text is None:
        return ''
    if len(text) <= max_length:
        return text
    return text[:max_length - 3] + '...'


def format_bytes(bytes_value: int) -> str:
    """Format bytes to human readable string.
    
    Args:
        bytes_value: Bytes value
        
    Returns:
        Formatted string (e.g., "1.50 GB")
    """
    if bytes_value < 0:
        return "0 B"
    
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_value < 1024:
            return f"{bytes_value:.2f} {unit}"
        bytes_value /= 1024
    return f"{bytes_value:.2f} PB"


def format_duration(seconds: int) -> str:
    """Format duration in seconds to human readable string.
    
    Args:
        seconds: Duration in seconds
        
    Returns:
        Formatted string (e.g., "2 days, 3 hours")
    """
    if seconds < 60:
        return f"{seconds}s"
    
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    
    days = hours // 24
    remaining_hours = hours % 24
    
    if remaining_hours == 0:
        return f"{days}d"
    return f"{days}d {remaining_hours}h"


def mask_sensitive(text: str, visible_chars: int = 4) -> str:
    """Mask sensitive data (tokens, keys).
    
    Args:
        text: Sensitive text
        visible_chars: Number of visible characters at end
        
    Returns:
        Masked text
    """
    if not text:
        return ""
    if len(text) <= visible_chars:
        return "*" * len(text)
    return "*" * (len(text) - visible_chars) + text[-visible_chars:]
