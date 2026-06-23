import psutil
import time
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

class SystemStatsService:
    """Service for gathering server system metrics."""
    
    @staticmethod
    def get_stats() -> dict:
        """Get CPU, RAM, Disk, and Uptime metrics."""
        try:
            # CPU
            cpu_percent = psutil.cpu_percent(interval=None)
            cpu_count = psutil.cpu_count()
            
            # RAM
            ram = psutil.virtual_memory()
            ram_total_gb = ram.total / (1024**3)
            ram_used_gb = ram.used / (1024**3)
            ram_percent = ram.percent
            
            # Disk
            disk = psutil.disk_usage('/')
            disk_total_gb = disk.total / (1024**3)
            disk_used_gb = disk.used / (1024**3)
            disk_free_gb = disk.free / (1024**3)
            disk_percent = disk.percent
            
            # Boot time / Uptime
            boot_time_timestamp = psutil.boot_time()
            uptime_seconds = time.time() - boot_time_timestamp
            
            # Load average (Linux only)
            load_avg = [0.0, 0.0, 0.0]
            try:
                load_avg = [round(x, 2) for x in psutil.getloadavg()]
            except (AttributeError, OSError):
                pass

            return {
                'cpu': {
                    'percent': cpu_percent,
                    'cores': cpu_count
                },
                'ram': {
                    'total': ram_total_gb,
                    'used': ram_used_gb,
                    'percent': ram_percent
                },
                'disk': {
                    'total': disk_total_gb,
                    'used': disk_used_gb,
                    'free': disk_free_gb,
                    'percent': disk_percent
                },
                'uptime': uptime_seconds,
                'load_avg': load_avg,
                'timestamp': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
        except Exception as e:
            logger.error(f"Error gathering system stats: {e}")
            return {}

    @staticmethod
    def format_uptime(seconds: float) -> str:
        """Format uptime in seconds to human readable string."""
        days, rem = divmod(int(seconds), 86400)
        hours, rem = divmod(rem, 3600)
        minutes, seconds = divmod(rem, 60)
        
        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        
        return " ".join(parts) if parts else "just started"
