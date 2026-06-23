"""Tests for utils package"""

import pytest

from bot.utils import (
    validate_chat_id,
    validate_email,
    validate_uuid,
    validate_callback_data,
    validate_platform,
    escape_markdown,
    escape_html,
    truncate_text,
    format_bytes,
    format_duration,
    mask_sensitive
)


class TestValidators:
    """Tests for validator functions"""
    
    def test_validate_chat_id_valid(self):
        """Test valid chat IDs"""
        assert validate_chat_id('123456') is True
        assert validate_chat_id('-1001234567890') is True
        assert validate_chat_id('0') is True
    
    def test_validate_chat_id_invalid(self):
        """Test invalid chat IDs"""
        assert validate_chat_id('') is False
        assert validate_chat_id('abc') is False
        assert validate_chat_id('12.34') is False
        assert validate_chat_id(None) is False
    
    def test_validate_email_valid(self):
        """Test valid emails"""
        assert validate_email('test@example.com') is True
        assert validate_email('user.name@domain.co.uk') is True
        assert validate_email('user+tag@example.com') is True
    
    def test_validate_email_invalid(self):
        """Test invalid emails"""
        assert validate_email('') is False
        assert validate_email('test@') is False
        assert validate_email('@example.com') is False
        assert validate_email('test@example') is False
        assert validate_email('plain-text') is False
    
    def test_validate_uuid_valid(self):
        """Test valid UUIDs"""
        assert validate_uuid('550e8400-e29b-41d4-a716-446655440000') is True
        assert validate_uuid('6ba7b810-9dad-11d1-80b4-00c04fd430c8') is True
    
    def test_validate_uuid_invalid(self):
        """Test invalid UUIDs"""
        assert validate_uuid('') is False
        assert validate_uuid('not-a-uuid') is False
        assert validate_uuid('550e8400-e29b-41d4-a716') is False  # Too short
        assert validate_uuid('550e8400-e29b-41d4-a716-44665544000g') is False  # Invalid char
    
    def test_validate_callback_data_valid(self):
        """Test valid callback data"""
        assert validate_callback_data('approve:123456') is True
        assert validate_callback_data('a' * 64) is True
    
    def test_validate_callback_data_invalid(self):
        """Test invalid callback data"""
        assert validate_callback_data('') is False
        assert validate_callback_data('a' * 100) is False  # Too long
    
    def test_validate_platform_valid(self):
        """Test valid platforms"""
        assert validate_platform('android') is True
        assert validate_platform('ios') is True
        assert validate_platform('windows') is True
        assert validate_platform('macos') is True
        assert validate_platform('other') is True
    
    def test_validate_platform_custom(self):
        """Test custom allowed platforms"""
        assert validate_platform('linux', allowed=['linux', 'android']) is True
        assert validate_platform('ios', allowed=['linux', 'android']) is False
    
    def test_validate_platform_invalid(self):
        """Test invalid platforms"""
        assert validate_platform('') is False
        assert validate_platform('invalid') is False


class TestHelpers:
    """Tests for helper functions"""
    
    def test_escape_markdown(self):
        """Test Markdown escaping"""
        assert escape_markdown('test_text') == 'test\\_text'
        assert escape_markdown('*bold*') == '\\*bold\\*'
        assert escape_markdown('[link](url)') == '\\[link\\]\\(url\\)'
    
    def test_escape_html(self):
        """Test HTML escaping"""
        assert escape_html('<script>alert(1)</script>') == '&lt;script&gt;alert(1)&lt;/script&gt;'
        assert escape_html('a & b') == 'a &amp; b'
    
    def test_truncate_text_no_truncate(self):
        """Test text that doesn't need truncation"""
        text = "Short text"
        assert truncate_text(text, max_length=100) == text
    
    def test_truncate_text_truncate(self):
        """Test text that needs truncation"""
        text = "a" * 5000
        result = truncate_text(text, max_length=4096)
        assert len(result) == 4096
        assert result.endswith('...')
    
    def test_format_bytes(self):
        """Test byte formatting"""
        assert format_bytes(0) == "0.00 B"
        assert format_bytes(1024) == "1.00 KB"
        assert format_bytes(1024 ** 2) == "1.00 MB"
        assert format_bytes(1024 ** 3) == "1.00 GB"
        assert format_bytes(1024 ** 4) == "1.00 TB"
    
    def test_format_bytes_negative(self):
        """Test negative bytes"""
        assert format_bytes(-100) == "0 B"
    
    def test_format_duration(self):
        """Test duration formatting"""
        assert format_duration(30) == "30s"
        assert format_duration(120) == "2m"
        assert format_duration(7200) == "2h"
        assert format_duration(86400) == "1d"
        assert format_duration(90000) == "1d 1h"  # 25 hours
    
    def test_mask_sensitive(self):
        """Test sensitive data masking"""
        assert mask_sensitive('1234567890') == "******7890"
        assert mask_sensitive('1234') == "****"
        assert mask_sensitive('123', visible_chars=2) == "*23"
        assert mask_sensitive('') == ""
