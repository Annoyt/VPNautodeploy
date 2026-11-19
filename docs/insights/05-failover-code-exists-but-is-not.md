# §5. Failover code exists but is not wired

`bot/core/cluster/smart_routing.py` (`SmartRoutingTable`, `should_failover`) is **never instantiated in production code** — only in tests. Same for `create_failover_api`. Multi-node `_generate_multi_node_link` in `vpn.py` is also dead because handlers call the legacy `generate_vless_link(uuid, email)` directly. The Phase 5 roadmap in conversation memory plans to wire these up via a `QuotaMonitor` background task.
