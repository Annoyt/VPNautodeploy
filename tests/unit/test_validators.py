"""Tests for input validation utilities."""

import pytest

from bot.utils.validators import (
    validate_chat_id,
    validate_email,
    validate_uuid,
    validate_callback_data,
    validate_platform,
)


class TestValidateChatId:
    """Test validate_chat_id function."""
    
    def test_valid_numeric_chat_id(self):
        """Test valid numeric chat ID."""
        assert validate_chat_id("123456789") is True
        assert validate_chat_id("9999999999999999") is True
        
    def test_valid_negative_chat_id(self):
        """Test valid negative chat ID (for groups)."""
        assert validate_chat_id("-1001234567890") is True
        
    def test_invalid_empty_string(self):
        """Test empty string is invalid."""
        assert validate_chat_id("") is False
        
    def test_invalid_none(self):
        """Test None is invalid."""
        assert validate_chat_id(None) is False
        
    def test_invalid_non_numeric(self):
        """Test non-numeric string is invalid."""
        assert validate_chat_id("abc123") is False
        assert validate_chat_id("user_123") is False
        
    def test_invalid_with_spaces(self):
        """Test string with spaces is invalid."""
        assert validate_chat_id("123 456") is False


class TestValidateEmail:
    """Test validate_email function."""
    
    def test_valid_emails(self):
        """Test various valid email formats."""
        assert validate_email("user@example.com") is True
        assert validate_email("user.name@example.co.uk") is True
        assert validate_email("user+tag@example.org") is True
        assert validate_email("user_name@example.io") is True
        assert validate_email("123@example.com") is True
        
    def test_invalid_empty_string(self):
        """Test empty string is invalid."""
        assert validate_email("") is False
        
    def test_invalid_none(self):
        """Test None is invalid."""
        assert validate_email(None) is False
        
    def test_invalid_missing_at(self):
        """Test email without @ is invalid."""
        assert validate_email("userexample.com") is False
        
    def test_invalid_missing_domain(self):
        """Test email without domain is invalid."""
        assert validate_email("user@") is False
        assert validate_email("user@.com") is False
        
    def test_invalid_missing_local(self):
        """Test email without local part is invalid."""
        assert validate_email("@example.com") is False
        
    def test_invalid_spaces(self):
        """Test email with spaces is invalid."""
        assert validate_email("user @example.com") is False
        assert validate_email("user@ example.com") is False


class TestValidateUuid:
    """Test validate_uuid function."""
    
    def test_valid_uuid_v4(self):
        """Test valid UUID v4 format."""
        assert validate_uuid("550e8400-e29b-41d4-a716-446655440000") is True
        assert validate_uuid("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11") is True
        
    def test_valid_uuid_case_insensitive(self):
        """Test UUID validation is case insensitive."""
        assert validate_uuid("550E8400-E29B-41D4-A716-446655440000") is True
        assert validate_uuid("AABBCCDD-EEFF-1122-3344-5566778899AA") is True
        
    def test_invalid_empty_string(self):
        """Test empty string is invalid."""
        assert validate_uuid("") is False
        
    def test_invalid_none(self):
        """Test None is invalid."""
        assert validate_uuid(None) is False
        
    def test_invalid_missing_dashes(self):
        """Test UUID without dashes is invalid."""
        assert validate_uuid("550e8400e29b41d4a716446655440000") is False
        
    def test_invalid_wrong_length(self):
        """Test UUID with wrong length is invalid."""
        assert validate_uuid("550e8400-e29b-41d4-a716-44665544000") is False  # One char short
        assert validate_uuid("550e8400-e29b-41d4-a716-4466554400000") is False  # One char extra
        
    def test_invalid_wrong_format(self):
        """Test UUID with wrong format is invalid."""
        assert validate_uuid("550e8400-e29b-41d4-a716-44665544000g") is False  # 'g' is invalid
        assert validate_uuid("550e8400-e29b-41d4-a716_446655440000") is False  # '_' instead of '-'


class TestValidateCallbackData:
    """Test validate_callback_data function."""
    
    def test_valid_short_data(self):
        """Test valid short callback data."""
        assert validate_callback_data("action:123") is True
        assert validate_callback_data("yes") is True
        assert validate_callback_data("request_demo") is True
        
    def test_valid_exactly_64_bytes(self):
        """Test data exactly 64 bytes is valid."""
        data = "a" * 64  # 64 ASCII chars = 64 bytes
        assert validate_callback_data(data) is True
        
    def test_invalid_empty_string(self):
        """Test empty string is invalid."""
        assert validate_callback_data("") is False
        
    def test_invalid_none(self):
        """Test None is invalid."""
        assert validate_callback_data(None) is False
        
    def test_invalid_too_long(self):
        """Test data over 64 bytes is invalid."""
        data = "a" * 65  # 65 bytes
        assert validate_callback_data(data) is False
        
    def test_invalid_unicode_over_64_bytes(self):
        """Test unicode data that exceeds 64 bytes when encoded."""
        # 20 emoji chars = 80 bytes (4 bytes per emoji)
        data = "😀" * 20
        assert validate_callback_data(data) is False
        
    def test_valid_unicode_within_limit(self):
        """Test unicode data within 64 bytes."""
        # 10 emoji chars = 40 bytes
        data = "😀" * 10
        assert validate_callback_data(data) is True
        
    def test_custom_max_length(self):
        """Test custom max length parameter."""
        assert validate_callback_data("short", max_length=100) is True
        assert validate_callback_data("a" * 50, max_length=32) is False


class TestValidatePlatform:
    """Test validate_platform function."""
    
    def test_valid_default_platforms(self):
        """Test valid platforms from default list."""
        assert validate_platform("android") is True
        assert validate_platform("ios") is True
        assert validate_platform("windows") is True
        assert validate_platform("macos") is True
        assert validate_platform("other") is True
        
    def test_valid_case_insensitive(self):
        """Test platform validation is case insensitive."""
        assert validate_platform("ANDROID") is True
        assert validate_platform("iOS") is True
        assert validate_platform("Windows") is True
        
    def test_invalid_empty_string(self):
        """Test empty string is invalid."""
        assert validate_platform("") is False
        
    def test_invalid_none(self):
        """Test None is invalid."""
        assert validate_platform(None) is False
        
    def test_invalid_not_in_list(self):
        """Test platform not in allowed list is invalid."""
        assert validate_platform("linux") is False
        assert validate_platform("ubuntu") is False
        
    def test_custom_allowed_list(self):
        """Test custom allowed platforms list."""
        custom_allowed = ['linux', 'ubuntu', 'debian']
        assert validate_platform("linux", allowed=custom_allowed) is True
        assert validate_platform("android", allowed=custom_allowed) is False
        
    def test_empty_allowed_list(self):
        """Test empty allowed list rejects all platforms."""
        assert validate_platform("android", allowed=[]) is False
