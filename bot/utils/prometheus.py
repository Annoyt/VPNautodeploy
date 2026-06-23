"""Prometheus metrics collection for VPN bot.

Tracks:
- User funnel (new → demo → paid)
- X-UI API latency and errors
- Protocol usage and success rates
- Payment conversions
"""

import time
import logging
from collections import defaultdict
from typing import Dict, Callable
from functools import wraps
import threading

logger = logging.getLogger(__name__)


class MetricsRegistry:
    """Simple metrics registry for Prometheus export.

    No external dependencies - works with pure Python.
    """

    def __init__(self):
        """Initialize metrics registry."""
        self._counters: Dict[str, float] = defaultdict(float)
        self._gauges: Dict[str, float] = {}
        self._histograms: Dict[str, list] = defaultdict(list)
        self._labels: Dict[str, set] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, labels: Dict[str, str] = None) -> None:
        """Increment a counter metric.

        Args:
            name: Metric name (should use '_' separator)
            labels: Optional label dict
        """
        key = self._make_key(name, labels)
        with self._lock:
            self._counters[key] += 1

    def gauge(self, name: str, value: float, labels: Dict[str, str] = None) -> None:
        """Set a gauge metric value.

        Args:
            name: Metric name
            value: Gauge value
            labels: Optional label dict
        """
        key = self._make_key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def histogram(self, name: str, value: float, labels: Dict[str, str] = None) -> None:
        """Record a value in a histogram.

        Args:
            name: Metric name
            value: Value to record
            labels: Optional label dict
        """
        key = self._make_key(name, labels)
        with self._lock:
            self._histograms[key].append(value)

    def _make_key(self, name: str, labels: Dict[str, str] = None) -> str:
        """Create a unique key for metric with labels."""
        if labels:
            label_str = ','.join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            return f"{name}{{{label_str}}}"
        return name

    def export(self) -> str:
        """Export metrics in Prometheus text format.

        Returns:
            Metrics in Prometheus exposition format
        """
        lines = []

        with self._lock:
            # Export counters
            for key, value in self._counters.items():
                lines.append(f"# TYPE {key.split('{')[0]} counter")
                lines.append(f"{key} {value}")

            # Export gauges
            for key, value in self._gauges.items():
                lines.append(f"# TYPE {key.split('{')[0]} gauge")
                lines.append(f"{key} {value}")

            # Export histograms (simplified as quantiles)
            for key, values in self._histograms.items():
                if values:
                    lines.append(f"# TYPE {key.split('{')[0]} histogram")
                    lines.append(f"{key}_count {len(values)}")
                    lines.append(f"{key}_sum {sum(values)}")
                    # Calculate quantiles
                    sorted_values = sorted(values)
                    for q in [0.5, 0.9, 0.95, 0.99]:
                        idx = int(len(sorted_values) * q)
                        quantile = sorted_values[min(idx, len(sorted_values) - 1)]
                        lines.append(f"{key}_quantile{{quantile=\"{q}\"}} {quantile}")

        return '\n'.join(lines) + '\n'


# Global metrics registry
metrics = MetricsRegistry()


# Timing decorator for API calls
def timed(metric_name: str):
    """Decorator to time function calls and record as histogram.

    Args:
        metric_name: Name of the metric

    Returns:
        Decorated function
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                duration = time.time() - start
                metrics.histogram(metric_name, duration)
        return wrapper
    return decorator


# Convenience functions for common metrics
def inc_user_funnel(stage: str, status: str = 'success') -> None:
    """Record user funnel event.

    Args:
        stage: Funnel stage (new_request, demo_approved, paid_subscribed)
        status: Event status (success, failed)
    """
    metrics.counter('vpn_user_funnel_total', {'stage': stage, 'status': status})


def inc_payment_event(event_type: str, amount_stars: int = 0) -> None:
    """Record payment event.

    Args:
        event_type: Event type (invoice_created, payment_completed, payment_failed)
        amount_stars: Payment amount in Stars
    """
    metrics.counter('vpn_payment_events_total', {'type': event_type})
    if amount_stars:
        metrics.counter('vpn_payment_revenue_stars', amount_stars)


def inc_protocol_event(protocol: str, event: str = 'generated') -> None:
    """Record protocol-related event.

    Args:
        protocol: Protocol name (vless, hy2, vmess, shadowtls)
        event: Event type (generated, switched, failed)
    """
    metrics.counter('vpn_protocol_events_total', {'protocol': protocol, 'event': event})


def set_gauge_users(count: int, status: str = None) -> None:
    """Set user count gauge.

    Args:
        count: Number of users
        status: User status filter (new, demo, paid, total)
    """
    labels = {'status': status} if status else None
    metrics.gauge('vpn_users_count', count, labels)


def record_xui_api(call_type: str, duration: float, success: bool = True) -> None:
    """Record X-UI API call.

    Args:
        call_type: Type of call (get_traffic, add_client, remove_client)
        duration: Call duration in seconds
        success: Whether call succeeded
    """
    metrics.histogram('xui_api_duration_seconds', duration, {'call_type': call_type})
    metrics.counter('xui_api_calls_total', {'call_type': call_type, 'status': 'success' if success else 'failed'})


def set_system_gauge(metric_name: str, value: float) -> None:
    """Set system-level gauge.

    Args:
        metric_name: Metric name (disk_usage_percent, memory_usage_percent, etc.)
        value: Gauge value
    """
    metrics.gauge(f'vpn_{metric_name}', value)
