# Hybrid Entry Node Failover Architecture

## Overview

This architecture provides automatic failover between Exit Nodes with performance awareness. When an Exit Node becomes unhealthy or overloaded, user connections are automatically rerouted to the best available alternative.

### Key Features

- **Performance-Aware Routing**: Considers CPU load and throttling status
- **Silent Failover**: Users experience seamless reconnection
- **Admin Control**: Optional broadcast notifications via Telegram buttons
- **Configurable Thresholds**: Per-server CPU limits and time windows

---

## Architecture Components

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────────┐
│   User      │────▶│  Entry Node  │────▶│ Exit Node 1 (High)  │
│  (VLESS)    │     │  (Kaskad)    │     │ CPU: 15% ✓          │
└─────────────┘     └──────────────┘     └─────────────────────┘
                              │
                              │ Health Check Fails
                              ▼
                     ┌─────────────────────────────────────────┐
                     │  Performance Monitor Decision:          │
                     │  - Check Exit 2 CPU: 45% (throttled)    │
                     │  - No other options available           │
                     │  → Allow failover with warning          │
                     └─────────────────────────────────────────┘
                              │
                              ▼
                     ┌─────────────────────┐
                     │ Exit Node 2 (Limit) │
                     │ CPU: 45% ⚠️         │
                     │ THROTTLED           │
                     └─────────────────────┘
                              │
                              │ POST /failover/event
                              ▼
                     ┌──────────────┐
                     │     Bot      │
                     │  Notify Admin│
                     └──────────────┘
```

---

## Component Reference

### 1. Performance Monitor (`bot/core/cluster/`)

| File | Purpose | Lines |
|------|---------|-------|
| `node_tracker.py` | Tracks single Exit Node metrics | 131 |
| `performance_monitor.py` | Manages multiple Exit Nodes | 133 |

**Key Classes:**
- `NodePerformanceTracker`: Rolling window CPU tracking, throttle detection
- `ClusterPerformanceMonitor`: Multi-node coordination, best node selection

### 2. Smart Routing (`bot/core/cluster/smart_routing.py`)

**Key Class:** `SmartRoutingTable`

**Routing Logic:**
1. Check if current Exit Node is optimal (healthy + not throttled) → Stay
2. Check failover cooldown (5 min) → Delay if active
3. Check max failover count (3) → Stay if exceeded
4. Find best alternative (preferred > throttled > none)
5. If target throttled → Delay with warning

### 3. Failover API (`bot/core/cluster/`)

| File | Purpose | Lines |
|------|---------|-------|
| `failover_api.py` | FastAPI endpoints | 163 |
| `failover_schemas.py` | Pydantic models | 59 |

**Endpoints:**
- `GET /exit/nodes/status` - Get all Exit Node statuses
- `POST /failover/event` - Report failover event
- `GET /failover/decision/{user_id}` - Get routing decision
- `POST /admin/broadcast` - Send message to users

### 4. Notification Service (`bot/services/failover_notifications.py`)

**Key Class:** `FailoverNotificationService`

**Features:**
- Silent mode (users not notified by default)
- Admin notifications with inline keyboard
- Batch processing of multiple failovers
- Broadcast to affected users (admin-controlled)

### 5. Entry Node Script (`scripts/entry_node_healthcheck.py`)

**Purpose:** Runs on Entry Node to monitor Exit Nodes and execute failovers.

**Features:**
- Health checks every 5 seconds
- Performance data collection
- Automatic failover execution
- Event reporting to Bot API

---

## Configuration

### Server Performance Tiers

```python
EXIT_NODE_TIERS = {
    "exit-frankfurt-1": {
        "tier": "high",
        "cpu_limit": None,  # No throttling
    },
    "exit-amsterdam-1": {
        "tier": "limited",
        "cpu_limit": 30,           # Throttle at 30%
        "throttle_window": 300,    # For 5 minutes
    },
}
```

### Failover Behavior

```python
FAILOVER_COOLDOWN_SECONDS = 300      # 5 min between failovers
MAX_FAILOVER_COUNT = 3               # Max failovers per user
THROTTLED_FAILOVER_DELAY = 30        # Delay before throttled failover
FAILOVER_NOTIFICATION_POLICY = "silent"  # silent | admin_approval | generic
```

---

## Failover Rules

### Priority Order

1. **Primary (Frankfurt)** - If healthy and not throttled → Use
2. **Backup Normal (Amsterdam < 30%)** - If primary down → Failover
3. **Backup Throttled (Amsterdam > 30% for 5min)** - If no other options → Delay 30s, then failover with warning

### Throttle Detection

```
CPU samples every 5 seconds
↓
Rolling window: 60 samples (5 minutes)
↓
Average CPU > threshold (30%)?
↓
YES → Mark as THROTTLED
NO  → Normal or Warning (if >80% of threshold)
```

---

## Testing

### Test Coverage

See [TESTING.md](TESTING.md) for detailed test guide.

### Quick Test Commands

```bash
# Unit tests
pytest tests/unit/test_performance_monitor.py -v
pytest tests/unit/test_smart_routing.py -v
pytest tests/unit/test_failover_api.py -v
pytest tests/unit/test_failover_notifications.py -v

# All failover tests
pytest tests/unit/test_*failover*.py tests/unit/test_performance*.py tests/unit/test_smart_routing.py -v
```

---

## Deployment

### Entry Node Setup

```bash
# Copy script
scp scripts/entry_node_healthcheck.py root@entry-node:/opt/vpn/

# Create systemd service
cat > /etc/systemd/system/entry-health-monitor.service << 'EOF'
[Unit]
Description=Entry Node Health Monitor
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/vpn/entry_node_healthcheck.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl enable entry-health-monitor
systemctl start entry-health-monitor
```

### Bot Configuration

Add to `.env`:
```bash
# Failover configuration
FAILOVER_NOTIFICATION_POLICY=silent
CPU_THROTTLE_THRESHOLD=30.0
CPU_THROTTLE_WINDOW=300
```

---

## Monitoring

### Logs

```bash
# Entry Node
journalctl -u entry-health-monitor -f

# Bot
journalctl -u vpn-bot -f | grep -i failover
```

### Metrics

- `failover_count` - Total failovers executed
- `throttle_events` - Number of throttle state changes
- `routing_decisions` - Decision distribution (stay/failover/delay)

---

## Troubleshooting

### Issue: Failover not triggering

**Check:**
1. Entry Node can reach Bot API
2. Exit Node health checks passing
3. CPU threshold configuration correct
4. Failover cooldown not active

### Issue: Users not receiving broadcasts

**Check:**
1. Admin clicked "📢 Разослать" button
2. Bot has permission to message users
3. `chat_id`s are correct in failover events

### Issue: All Exit Nodes throttled

**This is expected behavior** when backup server is overloaded. Options:
1. Increase CPU threshold for backup
2. Add more Exit Nodes
3. Accept degraded performance with warning

---

## References

- [Architecture Overview](ARCHITECTURE.md)
- [Testing Guide](TESTING.md)
- [Code Review Report](CODE_REVIEW_FAILOVER.md)
