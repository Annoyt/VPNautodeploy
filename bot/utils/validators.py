"""Input validation utilities"""

import re
from typing import Optional


def validate_chat_id(chat_id: str) -> bool:
    """Validate chat ID format.
    
    Args:
        chat_id: Chat ID string
        
    Returns:
        True if valid
    """
    if not chat_id:
        return False
    try:
        int(chat_id)
        return True
    except ValueError:
        return False


def validate_email(email: str) -> bool:
    """Validate email format.
    
    Args:
        email: Email string
        
    Returns:
        True if valid
    """
    if not email:
        return False
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None


def validate_uuid(uuid_str: str) -> bool:
    """Validate UUID v4 format.
    
    Args:
        uuid_str: UUID string
        
    Returns:
        True if valid
    """
    if not uuid_str:
        return False
    pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
    return re.match(pattern, uuid_str.lower()) is not None


def validate_callback_data(data: str, max_length: int = 64) -> bool:
    """Validate callback data format.
    
    Telegram limits callback data to 64 bytes.
    
    Args:
        data: Callback data string
        max_length: Maximum allowed length
        
    Returns:
        True if valid
    """
    if not data:
        return False
    if len(data.encode('utf-8')) > max_length:
        return False
    return True


def validate_platform(platform: str, allowed: Optional[list] = None) -> bool:
    """Validate platform name.

    Args:
        platform: Platform string
        allowed: List of allowed platforms

    Returns:
        True if valid
    """
    if not platform:
        return False

    if allowed is None:
        allowed = ['android', 'ios', 'windows', 'macos', 'other']

    return platform.lower() in allowed


def validate_vless_url(url: str) -> bool:
    """Validate VLESS URL shape before sending to user.

    Catches the broken-key shapes the bot has historically generated:
    `vless://None@...`, `vless://@host:443`, missing host, missing port.

    Args:
        url: VLESS URL string

    Returns:
        True if the URL has a uuid@host:port skeleton with a valid uuid and port
    """
    if not url or not isinstance(url, str):
        return False
    if not url.startswith("vless://"):
        return False
    rest = url[len("vless://"):]
    if "@" not in rest:
        return False
    auth, host_part = rest.split("@", 1)
    if not validate_uuid(auth):
        return False
    host_port = host_part.split("?", 1)[0].split("#", 1)[0]
    if ":" not in host_port:
        return False
    host, port = host_port.rsplit(":", 1)
    if not host:
        return False
    try:
        port_num = int(port)
    except ValueError:
        return False
    return 1 <= port_num <= 65535
