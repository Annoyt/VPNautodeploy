"""Log redaction utilities for sensitive data.

UUIDs, tokens, and VPN keys should never appear in logs in plaintext.
Use these helpers to redact them.
"""

import re
from typing import Any


# Pattern for UUID (8-4-4-4-12 hex digits)
UUID_PATTERN = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
    re.IGNORECASE
)

# Pattern for VLESS URLs (contain UUIDs)
VLESS_PATTERN = re.compile(
    r'vless://[a-z0-9-]+@',
    re.IGNORECASE
)


def redact_uuid(text: str) -> str:
    """Redact UUIDs from text.

    Args:
        text: Text that may contain UUIDs

    Returns:
        Text with UUIDs replaced by [REDACTED]
    """
    if not text:
        return text

    # Redact full UUIDs
    result = UUID_PATTERN.sub('[UUID_REDACTED]', text)

    return result


def redact_email(text: str) -> str:
    """Redact email addresses (may contain UUIDs as local part).

    Args:
        text: Text that may contain emails

    Returns:
        Text with emails redacted
    """
    if not text:
        return text

    # Pattern: local-part@domain
    # Redact local part but keep domain for debugging
    email_pattern = re.compile(r'\b([a-zA-Z0-9._%+-]+)@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b')

    def replace_email(match):
        domain = match.group(2)
        return f'[REDACTED]@{domain}'

    return email_pattern.sub(replace_email, text)


def redact_vless_url(text: str) -> str:
    """Redact VLESS VPN URLs (contain UUIDs).

    Args:
        text: Text that may contain VLESS URLs

    Returns:
        Text with VLESS URLs redacted
    """
    if not text:
        return text

    # Redact entire URL (too sensitive to show any part)
    return VLESS_PATTERN.sub('vless://[REDACTED]@', text)


def redact_sensitive(text: str) -> str:
    """Redact all sensitive data from text.

    Args:
        text: Text that may contain sensitive data

    Returns:
        Text with UUIDs, emails, and VLESS URLs redacted
    """
    if not text:
        return text

    # Apply all redactions
    result = redact_uuid(text)
    result = redact_email(result)
    result = redact_vless_url(result)

    return result


def safe_format(format_string: str, **kwargs) -> str:
    """Safely format a string with sensitive data redaction.

    Use this instead of f-strings or .format() for log messages
    that may contain sensitive data.

    Args:
        format_string: Format string with {key} placeholders
        **kwargs: Values to format

    Returns:
        Formatted string with sensitive data redacted

    Example:
        >>> safe_format("User {email} created", email="uuid@domain.com")
        "User [REDACTED]@domain.com created"
    """
    # First, format normally
    formatted = format_string.format(**kwargs)

    # Then redact
    return redact_sensitive(formatted)


class RedactedFormatter:
    """Custom formatter that redacts sensitive data from log records.

    Usage with logging:
        import logging
        formatter = RedactedFormatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    """

    def __init__(self, fmt=None):
        """Initialize formatter.

        Args:
            fmt: Format string (same as logging.Formatter)
        """
        self.fmt = fmt or '%(message)s'

    def format(self, record):
        """Format log record with sensitive data redacted."""
        # Format using standard formatter
        formatted = record.getMessage()

        # Add exception info if present
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            formatted += '\n' + record.exc_text

        # Redact sensitive data
        formatted = redact_sensitive(formatted)

        # Apply format string
        if self.fmt:
            formatted = self.fmt % {
                'name': record.name,
                'levelname': record.levelname,
                'asctime': self.formatTime(record),
                'message': formatted,
            }

        return formatted

    def formatException(self, exc_info):
        """Format exception with stack trace."""
        import traceback
        return ''.join(traceback.format_exception(*exc_info))

    def formatTime(self, record, datefmt=None):
        """Format time."""
        import time
        ct = time.localtime(record.created)
        if datefmt:
            return time.strftime(datefmt, ct)
        else:
            return time.strftime('%Y-%m-%d %H:%M:%S', ct)


class JSONFormatter:
    """JSON formatter with sensitive data redaction.

    Outputs logs as JSON for easy parsing by log aggregators.
    Automatically redacts UUIDs, emails, and tokens.

    Usage:
        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
    """

    def format(self, record):
        """Format log record as JSON with redaction."""
        import json

        # Build log entry
        log_entry = {
            'timestamp': self.formatTime(record),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }

        # Add exception info if present
        if record.exc_info:
            log_entry['exception'] = self.formatException(record.exc_info)

        # Add extra fields from record
        for key, value in record.__dict__.items():
            if key not in {'name', 'msg', 'args', 'levelname', 'levelno',
                          'pathname', 'filename', 'module', 'exc_info',
                          'exc_text', 'created', 'created_millis',
                          'msecs', 'relativeCreated', 'thread', 'threadName',
                          'processName', 'process', 'message', 'asctime',
                          'funcName', 'lineno', 'module', 'pathname', 'func',
                          'stack_info'}:
                log_entry[key] = value

        # Redact sensitive data in message and exception
        if 'message' in log_entry:
            log_entry['message'] = redact_sensitive(log_entry['message'])
        if 'exception' in log_entry:
            log_entry['exception'] = redact_sensitive(log_entry['exception'])

        return json.dumps(log_entry, default=str, ensure_ascii=False)

    def formatException(self, exc_info):
        """Format exception with stack trace."""
        import traceback
        return ''.join(traceback.format_exception(*exc_info))

    def formatTime(self, record, datefmt=None):
        """Format time as ISO 8601 string."""
        import time
        ct = time.localtime(record.created)
        if datefmt:
            return time.strftime(datefmt, ct)
        else:
            # ISO 8601 format
            return time.strftime('%Y-%m-%dT%H:%M:%S', ct)
