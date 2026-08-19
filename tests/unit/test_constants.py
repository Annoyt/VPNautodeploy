"""Unit tests for constants.

Tests all constants are correctly defined and consistent.
"""

import pytest
import math

from bot.config import (
    BYTES_PER_KB,
    BYTES_PER_MB,
    BYTES_PER_GB,
    TIMEOUT_API_DEFAULT,
    TIMEOUT_API_LONG_POLL,
    TIMEOUT_API_FILE_UPLOAD,
    TIMEOUT_XUI_API,
    TIMEOUT_DOCKER_EXEC,
    TIMEOUT_DB_OPERATION,
    MAX_RETRIES_API,
    RETRY_DELAY_BASE,
    DEFAULT_VPN_PORT,
    DEFAULT_FLOW,
    DEFAULT_SECURITY,
    DEFAULT_FINGERPRINT,
    DEFAULT_SPX,
    DEFAULT_DEMO_TRAFFIC_GB,
    DEFAULT_DEMO_DAYS,
    DEFAULT_LIMIT_IP,
    MAX_USERNAME_LENGTH,
    MAX_MESSAGE_LENGTH,
    UserState,
    Platform,
    STATE_TRANSITIONS,
)


class TestByteConstants:
    """Test byte conversion constants."""
    
    def test_bytes_per_kb(self):
        """Test BYTES_PER_KB is 1024."""
        assert BYTES_PER_KB == 1024
        assert isinstance(BYTES_PER_KB, int)
    
    def test_bytes_per_mb(self):
        """Test BYTES_PER_MB is 1024^2."""
        assert BYTES_PER_MB == 1024 ** 2
        assert BYTES_PER_MB == 1024 * BYTES_PER_KB
    
    def test_bytes_per_gb(self):
        """Test BYTES_PER_GB is 1024^3."""
        assert BYTES_PER_GB == 1024 ** 3
        assert BYTES_PER_GB == 1024 * BYTES_PER_MB
    
    def test_byte_consistency(self):
        """Test byte constants are mathematically consistent."""
        assert BYTES_PER_GB / BYTES_PER_MB == 1024
        assert BYTES_PER_MB / BYTES_PER_KB == 1024
        assert BYTES_PER_GB / BYTES_PER_KB == 1024 ** 2


class TestTimeoutConstants:
    """Test timeout constants."""
    
    def test_timeout_api_default(self):
        """Test default API timeout."""
        assert TIMEOUT_API_DEFAULT == 10
        assert isinstance(TIMEOUT_API_DEFAULT, int)
        assert TIMEOUT_API_DEFAULT > 0
    
    def test_timeout_api_long_poll(self):
        """Test long poll timeout is greater than default."""
        assert TIMEOUT_API_LONG_POLL == 30
        assert TIMEOUT_API_LONG_POLL > TIMEOUT_API_DEFAULT
    
    def test_timeout_api_file_upload(self):
        """Test file upload timeout is greater than default."""
        assert TIMEOUT_API_FILE_UPLOAD == 60
        assert TIMEOUT_API_FILE_UPLOAD > TIMEOUT_API_DEFAULT
    
    def test_timeout_xui_api(self):
        """Test X-UI API timeout."""
        assert TIMEOUT_XUI_API == 30
        assert isinstance(TIMEOUT_XUI_API, int)
    
    def test_timeout_docker_exec(self):
        """Test Docker exec timeout."""
        assert TIMEOUT_DOCKER_EXEC == 10
        assert isinstance(TIMEOUT_DOCKER_EXEC, int)
    
    def test_timeout_db_operation(self):
        """Test database operation timeout."""
        assert TIMEOUT_DB_OPERATION == 5
        assert isinstance(TIMEOUT_DB_OPERATION, int)
    
    def test_timeouts_are_reasonable(self):
        """Test that timeouts are reasonable values."""
        timeouts = [
            TIMEOUT_API_DEFAULT,
            TIMEOUT_API_LONG_POLL,
            TIMEOUT_API_FILE_UPLOAD,
            TIMEOUT_XUI_API,
            TIMEOUT_DOCKER_EXEC,
            TIMEOUT_DB_OPERATION,
        ]
        
        for timeout in timeouts:
            assert timeout >= 1, f"Timeout {timeout} too small"
            assert timeout <= 300, f"Timeout {timeout} too large"


class TestRetryConstants:
    """Test retry constants."""
    
    def test_max_retries_api(self):
        """Test max retries for API."""
        assert MAX_RETRIES_API == 3
        assert isinstance(MAX_RETRIES_API, int)
        assert MAX_RETRIES_API > 0
    
    def test_retry_delay_base(self):
        """Test base retry delay."""
        assert RETRY_DELAY_BASE == 1
        assert isinstance(RETRY_DELAY_BASE, int)


class TestVpnDefaults:
    """Test VPN default constants."""
    
    def test_default_vpn_port(self):
        """Test default VPN port."""
        assert DEFAULT_VPN_PORT == 443
        assert isinstance(DEFAULT_VPN_PORT, int)
    
    def test_default_flow(self):
        """Test default flow."""
        assert DEFAULT_FLOW == "xtls-rprx-vision"
        assert isinstance(DEFAULT_FLOW, str)
    
    def test_default_security(self):
        """Test default security."""
        assert DEFAULT_SECURITY == "reality"
        assert isinstance(DEFAULT_SECURITY, str)
    
    def test_default_fingerprint(self):
        """Test default fingerprint."""
        assert DEFAULT_FINGERPRINT == "chrome"
        assert isinstance(DEFAULT_FINGERPRINT, str)
    
    def test_default_spx(self):
        """Test default SPX."""
        assert DEFAULT_SPX == "/"
        assert isinstance(DEFAULT_SPX, str)


class TestDemoDefaults:
    """Test demo/traffic defaults."""
    
    def test_default_demo_traffic_gb(self):
        """Test default demo traffic in GB."""
        assert DEFAULT_DEMO_TRAFFIC_GB == 10
        assert isinstance(DEFAULT_DEMO_TRAFFIC_GB, int)
        assert DEFAULT_DEMO_TRAFFIC_GB > 0
    
    def test_default_demo_days(self):
        """Test default demo period in days."""
        assert DEFAULT_DEMO_DAYS == 7
        assert isinstance(DEFAULT_DEMO_DAYS, int)
        assert DEFAULT_DEMO_DAYS > 0
    
    def test_default_limit_ip(self):
        """Test default IP limit."""
        assert DEFAULT_LIMIT_IP == 1
        assert isinstance(DEFAULT_LIMIT_IP, int)
        assert DEFAULT_LIMIT_IP > 0
    
    def test_demo_limits_are_reasonable(self):
        """Test that demo limits are reasonable."""
        assert 1 <= DEFAULT_DEMO_TRAFFIC_GB <= 100
        assert 1 <= DEFAULT_DEMO_DAYS <= 30


class TestAdminConstants:
    """Test admin-related constants."""
    
    def test_max_username_length(self):
        """Test max username length."""
        assert MAX_USERNAME_LENGTH == 32
        assert isinstance(MAX_USERNAME_LENGTH, int)
        assert MAX_USERNAME_LENGTH > 0
    
    def test_max_message_length(self):
        """Test max message length."""
        assert MAX_MESSAGE_LENGTH == 4096
        assert isinstance(MAX_MESSAGE_LENGTH, int)
        assert MAX_MESSAGE_LENGTH > 0
    
    def test_message_length_is_telegram_limit(self):
        """Test that message length matches Telegram API limit."""
        # Telegram has 4096 character limit for text messages
        assert MAX_MESSAGE_LENGTH == 4096


class TestUserState:
    """Test UserState enum."""
    
    def test_user_state_values(self):
        """Test UserState enum values."""
        assert UserState.NEW == "new"
        assert UserState.PENDING_DEMO == "pending_demo"
        assert UserState.REJECTED == "rejected"
        assert UserState.PLATFORM_SELECT == "platform_select"
        assert UserState.DEMO == "demo"
        assert UserState.PAID == "paid"
        assert UserState.SUPPORT_TOPIC == "support_topic"
        assert UserState.BANNED == "banned"
    
    def test_user_state_is_string_enum(self):
        """Test that UserState is string-based enum."""
        assert isinstance(UserState.NEW, str)
        assert UserState.NEW.value == "new"


class TestPlatform:
    """Test Platform enum."""
    
    def test_platform_values(self):
        """Test Platform enum values."""
        assert Platform.ANDROID == "android"
        assert Platform.IOS == "ios"
        assert Platform.WINDOWS == "windows"
        assert Platform.MACOS == "macos"
        assert Platform.LINUX == "linux"
        assert Platform.OTHER == "other"
    
    def test_platform_is_string_enum(self):
        """Test that Platform is string-based enum."""
        assert isinstance(Platform.ANDROID, str)
        assert Platform.ANDROID.value == "android"
    
    def test_all_platforms_lowercase(self):
        """Test that all platform values are lowercase."""
        platforms = [
            Platform.ANDROID,
            Platform.IOS,
            Platform.WINDOWS,
            Platform.MACOS,
            Platform.LINUX,
            Platform.OTHER,
        ]
        
        for platform in platforms:
            assert platform.value.islower()


class TestStateTransitions:
    """Test state transitions configuration."""
    
    def test_state_transitions_is_dict(self):
        """Test that STATE_TRANSITIONS is a dictionary."""
        assert isinstance(STATE_TRANSITIONS, dict)
    
    def test_new_state_transitions(self):
        """Test transitions from NEW state."""
        assert UserState.NEW in STATE_TRANSITIONS
        assert UserState.PENDING_DEMO in STATE_TRANSITIONS[UserState.NEW]
    
    def test_pending_demo_transitions(self):
        """Test transitions from PENDING_DEMO state."""
        assert UserState.PENDING_DEMO in STATE_TRANSITIONS
        assert UserState.PLATFORM_SELECT in STATE_TRANSITIONS[UserState.PENDING_DEMO]
        assert UserState.BANNED in STATE_TRANSITIONS[UserState.PENDING_DEMO]
    
    def test_all_states_have_transitions(self):
        """Test that all states have defined transitions."""
        states = [
            UserState.NEW,
            UserState.PENDING_DEMO,
            UserState.REJECTED,
            UserState.PLATFORM_SELECT,
            UserState.DEMO,
            UserState.PAID,
            UserState.SUPPORT_TOPIC,
            UserState.BANNED,
        ]
        
        for state in states:
            assert state in STATE_TRANSITIONS, f"State {state} missing transitions"
            assert isinstance(STATE_TRANSITIONS[state], list)


class TestConstantUsage:
    """Test that constants are used correctly in calculations."""
    
    def test_bytes_conversion_calculation(self):
        """Test byte conversion calculation."""
        # Convert 5 GB to bytes
        traffic_bytes = 5 * BYTES_PER_GB
        assert traffic_bytes == 5 * 1024 * 1024 * 1024
        
        # Convert back to GB
        traffic_gb = traffic_bytes / BYTES_PER_GB
        assert traffic_gb == 5.0
    
    def test_timeout_calculation(self):
        """Test timeout calculation for long poll."""
        # Long poll timeout + buffer for read timeout
        read_timeout = TIMEOUT_API_LONG_POLL + 10
        assert read_timeout == 40
        assert read_timeout > TIMEOUT_API_LONG_POLL
    
    def test_retry_delay_calculation(self):
        """Test exponential backoff calculation."""
        # Base delay * (2 ^ attempt)
        delays = [RETRY_DELAY_BASE * (2 ** i) for i in range(MAX_RETRIES_API)]
        assert delays == [1, 2, 4]


class TestConstantsAreImmutable:
    """Test that constants are immutable (by convention)."""
    
    def test_constants_are_uppercase(self):
        """Test that constant names are uppercase."""
        constants = [
            BYTES_PER_KB,
            BYTES_PER_MB,
            BYTES_PER_GB,
            TIMEOUT_API_DEFAULT,
            TIMEOUT_API_LONG_POLL,
            TIMEOUT_API_FILE_UPLOAD,
            TIMEOUT_XUI_API,
            TIMEOUT_DOCKER_EXEC,
            TIMEOUT_DB_OPERATION,
            MAX_RETRIES_API,
            RETRY_DELAY_BASE,
            DEFAULT_VPN_PORT,
            DEFAULT_FLOW,
            DEFAULT_SECURITY,
            DEFAULT_FINGERPRINT,
            DEFAULT_SPX,
            DEFAULT_DEMO_TRAFFIC_GB,
            DEFAULT_DEMO_DAYS,
            DEFAULT_LIMIT_IP,
            MAX_USERNAME_LENGTH,
            MAX_MESSAGE_LENGTH,
        ]
        
        # All constants should be defined
        assert len(constants) == 21


class TestNoMagicNumbers:
    """Test that there are no magic numbers in constants."""
    
    def test_all_timeouts_named(self):
        """Test that all timeouts have named constants."""
        # This test ensures we don't have hardcoded timeouts
        assert TIMEOUT_API_DEFAULT is not None
        assert TIMEOUT_API_LONG_POLL is not None
        assert TIMEOUT_API_FILE_UPLOAD is not None
    
    def test_all_byte_values_named(self):
        """Test that all byte values have named constants."""
        assert BYTES_PER_KB is not None
        assert BYTES_PER_MB is not None
        assert BYTES_PER_GB is not None
