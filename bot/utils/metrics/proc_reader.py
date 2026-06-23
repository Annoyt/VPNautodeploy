"""Read system metrics from /proc filesystem (Linux only).

Fallback when psutil is not available.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def read_cpu_from_proc() -> float:
    """Read CPU usage from /proc/stat.
    
    Returns:
        CPU usage percentage (0-100)
    """
    try:
        with open('/proc/stat', 'r') as f:
            line = f.readline()
            fields = line.split()
            if fields[0] == 'cpu':
                # user + nice + system + idle
                user = int(fields[1])
                nice = int(fields[2])
                system = int(fields[3])
                idle = int(fields[4])
                total = user + nice + system + idle
                return 100.0 * (user + nice + system) / total if total > 0 else 0.0
    except Exception as e:
        logger.debug(f"Failed to read CPU from /proc/stat: {e}")
    return 0.0


def read_memory_from_proc() -> float:
    """Read memory usage from /proc/meminfo.
    
    Returns:
        Memory usage percentage (0-100)
    """
    try:
        with open('/proc/meminfo', 'r') as f:
            mem_total = 0
            mem_available = 0
            mem_free = 0
            for line in f:
                if line.startswith('MemTotal:'):
                    mem_total = int(line.split()[1])
                elif line.startswith('MemAvailable:'):
                    mem_available = int(line.split()[1])
                elif line.startswith('MemFree:'):
                    mem_free = int(line.split()[1])
            
            if mem_total > 0:
                available = mem_available if mem_available > 0 else mem_free
                return 100.0 * (mem_total - available) / mem_total
    except Exception as e:
        logger.debug(f"Failed to read memory from /proc/meminfo: {e}")
    return 0.0


class ProcStatReader:
    """Reader for /proc/stat CPU metrics.
    
    Tracks CPU times between calls to calculate usage percentage.
    """
    
    def __init__(self):
        self._prev_idle: Optional[int] = None
        self._prev_total: Optional[int] = None
    
    def read_cpu_percent(self) -> float:
        """Read current CPU usage percentage.
        
        Calculates difference from previous call.
        First call returns 0.0 (no baseline).
        """
        try:
            with open('/proc/stat', 'r') as f:
                line = f.readline()
                fields = line.split()
                if fields[0] != 'cpu':
                    return 0.0
                
                user = int(fields[1])
                nice = int(fields[2])
                system = int(fields[3])
                idle = int(fields[4])
                
                total = user + nice + system + idle
                
                if self._prev_total is None or self._prev_idle is None:
                    # First call, just store values
                    self._prev_idle = idle
                    self._prev_total = total
                    return 0.0
                
                # Calculate difference
                idle_diff = idle - self._prev_idle
                total_diff = total - self._prev_total
                
                if total_diff > 0:
                    usage_percent = 100.0 * (total_diff - idle_diff) / total_diff
                else:
                    usage_percent = 0.0
                
                # Store for next call
                self._prev_idle = idle
                self._prev_total = total
                
                return max(0.0, min(100.0, usage_percent))
                
        except Exception as e:
            logger.error(f"Error reading /proc/stat: {e}")
            return 0.0
