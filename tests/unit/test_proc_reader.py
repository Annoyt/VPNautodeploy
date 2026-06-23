"""Tests for /proc filesystem metrics reader."""

import pytest
from unittest.mock import mock_open, patch, MagicMock

from bot.utils.metrics.proc_reader import (
    read_cpu_from_proc,
    read_memory_from_proc,
    ProcStatReader,
)


class TestReadCpuFromProc:
    """Test read_cpu_from_proc function."""
    
    def test_read_cpu_success(self):
        """Test successful CPU reading from /proc/stat."""
        mock_stat = "cpu  1000 200 300 4000\ncpu0 500 100 150 2000\n"
        
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            result = read_cpu_from_proc()
        
        # Calculation: (1000 + 200 + 300) / (1000 + 200 + 300 + 4000) * 100
        # = 1500 / 5500 * 100 = 27.27%
        assert 27.0 <= result <= 28.0
        
    def test_read_cpu_handles_exception(self):
        """Test CPU reading handles file read error gracefully."""
        with patch('builtins.open', side_effect=IOError("Permission denied")):
            result = read_cpu_from_proc()
        
        assert result == 0.0
        
    def test_read_cpu_empty_file(self):
        """Test CPU reading with empty /proc/stat."""
        with patch('builtins.open', mock_open(read_data="")):
            result = read_cpu_from_proc()
        
        assert result == 0.0
        
    def test_read_cpu_no_cpu_line(self):
        """Test CPU reading when no 'cpu' line in file."""
        mock_stat = "cpu0 1000 200 300 4000\n"
        
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            result = read_cpu_from_proc()
        
        assert result == 0.0
        
    def test_read_cpu_zero_total(self):
        """Test CPU reading with zero total (edge case)."""
        mock_stat = "cpu  0 0 0 0\n"
        
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            result = read_cpu_from_proc()
        
        assert result == 0.0


class TestReadMemoryFromProc:
    """Test read_memory_from_proc function."""
    
    def test_read_memory_success(self):
        """Test successful memory reading from /proc/meminfo."""
        mock_meminfo = """MemTotal:       16000000 kB
MemFree:         4000000 kB
MemAvailable:    8000000 kB
Buffers:          500000 kB
"""
        
        with patch('builtins.open', mock_open(read_data=mock_meminfo)):
            result = read_memory_from_proc()
        
        # Calculation: (16000000 - 8000000) / 16000000 * 100 = 50%
        assert result == 50.0
        
    def test_read_memory_handles_exception(self):
        """Test memory reading handles file read error gracefully."""
        with patch('builtins.open', side_effect=IOError("Permission denied")):
            result = read_memory_from_proc()
        
        assert result == 0.0
        
    def test_read_memory_missing_values(self):
        """Test memory reading with missing MemTotal/MemAvailable."""
        mock_meminfo = """MemFree: 4000000 kB
Buffers: 500000 kB
"""
        
        with patch('builtins.open', mock_open(read_data=mock_meminfo)):
            result = read_memory_from_proc()
        
        assert result == 0.0
        
    def test_read_memory_zero_total(self):
        """Test memory reading with zero MemTotal."""
        mock_meminfo = """MemTotal:       0 kB
MemAvailable:   8000000 kB
"""
        
        with patch('builtins.open', mock_open(read_data=mock_meminfo)):
            result = read_memory_from_proc()
        
        assert result == 0.0


class TestProcStatReader:
    """Test ProcStatReader class."""
    
    def test_first_call_returns_zero(self):
        """Test first call returns 0.0 (no baseline)."""
        mock_stat = "cpu  1000 200 300 4000\n"
        reader = ProcStatReader()
        
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            result = reader.read_cpu_percent()
        
        assert result == 0.0
        assert reader._prev_idle == 4000
        assert reader._prev_total == 5500
        
    def test_second_call_calculates_usage(self):
        """Test second call calculates CPU usage."""
        reader = ProcStatReader()
        reader._prev_idle = 4000
        reader._prev_total = 5500
        
        # Simulate next read with increased values
        mock_stat = "cpu  2000 400 600 8000\n"
        
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            result = reader.read_cpu_percent()
        
        # Calculation: 
        # idle_diff = 8000 - 4000 = 4000
        # total_diff = 11000 - 5500 = 5500
        # usage = 100 * (5500 - 4000) / 5500 = 27.27%
        assert 27.0 <= result <= 28.0
        
    def test_handles_file_read_error(self):
        """Test ProcStatReader handles file read errors."""
        reader = ProcStatReader()
        
        with patch('builtins.open', side_effect=IOError("Permission denied")):
            result = reader.read_cpu_percent()
        
        assert result == 0.0
        
    def test_handles_no_cpu_line(self):
        """Test ProcStatReader handles missing 'cpu' line."""
        mock_stat = "cpu0 1000 200 300 4000\n"
        reader = ProcStatReader()
        
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            result = reader.read_cpu_percent()
        
        assert result == 0.0
        
    def test_result_clamped_to_100(self):
        """Test CPU percentage is clamped to 0-100 range."""
        reader = ProcStatReader()
        reader._prev_idle = 0
        reader._prev_total = 100
        
        # Simulate unrealistic values that would give >100%
        mock_stat = "cpu  10000 0 0 0\n"
        
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            result = reader.read_cpu_percent()
        
        assert 0.0 <= result <= 100.0
        
    def test_zero_total_diff(self):
        """Test handling when total_diff is zero."""
        reader = ProcStatReader()
        reader._prev_idle = 4000
        reader._prev_total = 5500
        
        # Same values - no change
        mock_stat = "cpu  1000 200 300 4000\n"
        
        with patch('builtins.open', mock_open(read_data=mock_stat)):
            result = reader.read_cpu_percent()
        
        assert result == 0.0
