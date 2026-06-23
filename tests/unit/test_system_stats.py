"""Tests for system stats service."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

from bot.services.system_stats import SystemStatsService


class TestSystemStatsService:
    """Test SystemStatsService class."""
    
    @patch('bot.services.system_stats.psutil')
    @patch('bot.services.system_stats.time.time')
    def test_get_stats_success(self, mock_time, mock_psutil):
        """Test successful stats gathering."""
        # Setup mocks
        mock_psutil.cpu_percent.return_value = 25.5
        mock_psutil.cpu_count.return_value = 4
        
        mock_ram = Mock()
        mock_ram.total = 8 * 1024**3  # 8 GB
        mock_ram.used = 4 * 1024**3   # 4 GB
        mock_ram.percent = 50.0
        mock_psutil.virtual_memory.return_value = mock_ram
        
        mock_disk = Mock()
        mock_disk.total = 100 * 1024**3  # 100 GB
        mock_disk.used = 30 * 1024**3    # 30 GB
        mock_disk.free = 70 * 1024**3    # 70 GB
        mock_disk.percent = 30.0
        mock_psutil.disk_usage.return_value = mock_disk
        
        mock_psutil.boot_time.return_value = 1000000
        mock_time.return_value = 1003600  # 1 hour later
        
        mock_psutil.getloadavg.return_value = [0.5, 0.7, 0.9]
        
        # Execute
        stats = SystemStatsService.get_stats()
        
        # Verify
        assert stats['cpu']['percent'] == 25.5
        assert stats['cpu']['cores'] == 4
        assert stats['ram']['percent'] == 50.0
        assert stats['ram']['total'] == 8.0  # GB
        assert stats['ram']['used'] == 4.0   # GB
        assert stats['disk']['percent'] == 30.0
        assert stats['disk']['free'] == 70.0  # GB
        assert stats['uptime'] == 3600  # 1 hour
        assert stats['load_avg'] == [0.5, 0.7, 0.9]
        assert 'timestamp' in stats
        
    @patch('bot.services.system_stats.psutil')
    def test_get_stats_loadavg_not_available(self, mock_psutil):
        """Test stats when load average is not available (non-Linux)."""
        mock_psutil.cpu_percent.return_value = 0
        mock_psutil.cpu_count.return_value = 1
        mock_psutil.virtual_memory.return_value = Mock(total=1, used=0, percent=0)
        mock_psutil.disk_usage.return_value = Mock(total=1, used=0, free=1, percent=0)
        mock_psutil.boot_time.return_value = 0
        
        # Simulate AttributeError (Windows)
        mock_psutil.getloadavg.side_effect = AttributeError("Not available on Windows")
        
        stats = SystemStatsService.get_stats()
        
        assert stats['load_avg'] == [0.0, 0.0, 0.0]
        
    @patch('bot.services.system_stats.psutil')
    def test_get_stats_oserror(self, mock_psutil):
        """Test stats when OSError occurs (e.g., container)."""
        mock_psutil.cpu_percent.return_value = 0
        mock_psutil.cpu_count.return_value = 1
        mock_psutil.virtual_memory.return_value = Mock(total=1, used=0, percent=0)
        mock_psutil.disk_usage.return_value = Mock(total=1, used=0, free=1, percent=0)
        mock_psutil.boot_time.return_value = 0
        
        # Simulate OSError
        mock_psutil.getloadavg.side_effect = OSError("Load average not available")
        
        stats = SystemStatsService.get_stats()
        
        assert stats['load_avg'] == [0.0, 0.0, 0.0]
        
    @patch('bot.services.system_stats.psutil.cpu_percent')
    def test_get_stats_exception(self, mock_cpu_percent):
        """Test stats gathering handles exception gracefully."""
        mock_cpu_percent.side_effect = Exception("CPU access denied")
        
        stats = SystemStatsService.get_stats()
        
        assert stats == {}


class TestFormatUptime:
    """Test format_uptime static method."""
    
    def test_format_uptime_days_hours_minutes(self):
        """Test formatting with days, hours and minutes."""
        # 2 days, 4 hours, 30 minutes = 189000 seconds
        result = SystemStatsService.format_uptime(189000)
        assert result == "2d 4h 30m"
        
    def test_format_uptime_hours_minutes(self):
        """Test formatting with hours and minutes."""
        # 3 hours, 45 minutes
        result = SystemStatsService.format_uptime(3 * 3600 + 45 * 60)
        assert result == "3h 45m"
        
    def test_format_uptime_minutes_only(self):
        """Test formatting with only minutes."""
        result = SystemStatsService.format_uptime(30 * 60)
        assert result == "30m"
        
    def test_format_uptime_just_started(self):
        """Test formatting for very short uptime."""
        result = SystemStatsService.format_uptime(10)
        assert result == "just started"
        
    def test_format_uptime_zero(self):
        """Test formatting for zero uptime."""
        result = SystemStatsService.format_uptime(0)
        assert result == "just started"
        
    def test_format_uptime_exact_day(self):
        """Test formatting for exactly 1 day."""
        result = SystemStatsService.format_uptime(86400)
        assert result == "1d"
        
    def test_format_uptime_large_numbers(self):
        """Test formatting for large uptime values."""
        # 100 days, 12 hours, 30 minutes
        result = SystemStatsService.format_uptime(100 * 86400 + 12 * 3600 + 30 * 60)
        assert result == "100d 12h 30m"
