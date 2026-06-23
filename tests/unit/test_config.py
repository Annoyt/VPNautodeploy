"""Tests for config module"""

import os
import pytest

from bot.config import (
    Settings,
    UserState,
    Platform,
    STATE_TRANSITIONS,
    MESSAGES,
    PLATFORM_INSTRUCTIONS,
)


class TestSettings:
    """Tests for Settings class (instance-level attributes)"""
    
    def test_default_values(self):
        """Test default settings values via instance"""
        s = Settings()
        
        assert s.MODE == 'GROUP'
        assert s.FORUM_ENABLED == True
        assert s.TOPIC_STATS == 18
        assert s.TOPIC_REQUESTS == 15
        assert s.TOPIC_SUPPORT == 17
        assert s.TOPIC_PAYMENTS == 16
        assert s.TOPIC_SOLVED == 37
        assert s.DB_PATH == '/etc/cascade-vpn/bot.db'
        assert s.XUI_DB_PATH == '/opt/3x-ui/db/x-ui.db'
        assert s.DEMO_TRAFFIC_GB == 5
        assert s.DEMO_DAYS == 7
    
    def test_instance_independence(self):
        """Two instances don't share mutable state"""
        s1 = Settings()
        s2 = Settings()
        s1.DEMO_TRAFFIC_GB = 99
        assert s2.DEMO_TRAFFIC_GB == 5

    def test_validate_empty_token(self):
        """Test validation with empty BOT_TOKEN"""
        s = Settings()
        s.BOT_TOKEN = ''
        
        errors = s.validate()
        assert 'BOT_TOKEN is required' in errors
    
    def test_validate_group_mode_no_forum(self):
        """Test validation for GROUP mode without FORUM_GROUP_ID"""
        s = Settings()
        s.MODE = 'GROUP'
        s.FORUM_GROUP_ID = None
        
        errors = s.validate()
        assert 'FORUM_GROUP_ID required when MODE=GROUP' in errors
    
    def test_is_admin(self):
        """Test admin check"""
        s = Settings()
        s.SUPER_ADMIN_ID = '12345'
        
        assert s.is_admin('12345') == True
        assert s.is_admin('99999') == False


class TestUserState:
    """Tests for UserState enum"""
    
    def test_states_exist(self):
        """Test all expected states exist"""
        assert UserState.NEW.value == 'new'
        assert UserState.PENDING_DEMO.value == 'pending_demo'
        assert UserState.REJECTED.value == 'rejected'
        assert UserState.PLATFORM_SELECT.value == 'platform_select'
        assert UserState.DEMO.value == 'demo'
        assert UserState.PAID.value == 'paid'
        assert UserState.SUPPORT_TOPIC.value == 'support_topic'
        assert UserState.BANNED.value == 'banned'
    
    def test_state_transitions(self):
        """Test state transition rules"""
        assert UserState.PENDING_DEMO in STATE_TRANSITIONS[UserState.NEW]
        assert UserState.PLATFORM_SELECT in STATE_TRANSITIONS[UserState.PENDING_DEMO]
        assert UserState.REJECTED in STATE_TRANSITIONS[UserState.PENDING_DEMO]
        assert UserState.BANNED in STATE_TRANSITIONS[UserState.PENDING_DEMO]
        assert UserState.PENDING_DEMO in STATE_TRANSITIONS[UserState.REJECTED]
        assert UserState.DEMO in STATE_TRANSITIONS[UserState.PLATFORM_SELECT]
        assert UserState.SUPPORT_TOPIC in STATE_TRANSITIONS[UserState.DEMO]
        assert UserState.SUPPORT_TOPIC in STATE_TRANSITIONS[UserState.PAID]
        assert UserState.DEMO in STATE_TRANSITIONS[UserState.SUPPORT_TOPIC]
        assert UserState.PAID in STATE_TRANSITIONS[UserState.SUPPORT_TOPIC]
        assert UserState.NEW in STATE_TRANSITIONS[UserState.BANNED]


class TestPlatform:
    """Tests for Platform enum"""
    
    def test_platforms_exist(self):
        """Test all expected platforms exist"""
        assert Platform.ANDROID.value == 'android'
        assert Platform.IOS.value == 'ios'
        assert Platform.WINDOWS.value == 'windows'
        assert Platform.MACOS.value == 'macos'
        assert Platform.OTHER.value == 'other'


class TestMessages:
    """Tests for message constants"""
    
    def test_russian_messages_exist(self):
        """Test Russian messages are defined"""
        required_keys = [
            'welcome', 'request_sent', 'approved', 'rejected',
            'platform_selected', 'key_generated', 'support_prompt',
            'support_ticket_created', 'error_generic',
            'btn_request_demo', 'btn_my_key', 'btn_statistics',
            'btn_support', 'btn_full_version'
        ]
        
        for key in required_keys:
            assert key in MESSAGES['ru'], f"Missing Russian message: {key}"
    
    def test_english_messages_exist(self):
        """Test English messages are defined"""
        required_keys = [
            'welcome', 'request_sent', 'approved', 'rejected',
            'platform_selected', 'key_generated', 'support_prompt',
            'support_ticket_created', 'error_generic',
            'btn_request_demo', 'btn_my_key', 'btn_statistics',
            'btn_support', 'btn_full_version'
        ]
        
        for key in required_keys:
            assert key in MESSAGES['en'], f"Missing English message: {key}"


class TestPlatformInstructions:
    """Tests for platform instructions"""
    
    def test_all_platforms_have_instructions(self):
        """Test all platforms have instructions in both languages"""
        for platform in Platform:
            assert platform in PLATFORM_INSTRUCTIONS['ru'], f"Missing Russian instructions for {platform}"
            assert platform in PLATFORM_INSTRUCTIONS['en'], f"Missing English instructions for {platform}"
    
    def test_instructions_are_non_empty(self):
        """Test instructions contain actual content"""
        for lang in ['ru', 'en']:
            for platform in Platform:
                instructions = PLATFORM_INSTRUCTIONS[lang][platform]
                assert len(instructions) > 10, f"Instructions too short for {lang}/{platform}"
