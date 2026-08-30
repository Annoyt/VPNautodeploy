/**
 * NekoVPN Admin Dashboard — app.js
 * Tabs, data fetch, user actions, polling, pull-to-refresh
 */

(function () {
    'use strict';

    // ==================== Config ====================
    const API_BASE = '';          // same origin
    const POLL_INTERVAL_MS = 15000;
    const DEBOUNCE_MS = 400;

    // ==================== State ====================
    const state = {
        initData: '',
        adminToken: '',
        activeTab: 'health',
        pollingEnabled: false,
        pollTimers: {},
        isLoading: false,
        ticketMode: false,
        onlineMode: false,
        onlineByEmail: {},
        ws: null,
        wsReconnectMs: 1000,
    };

    // ==================== Init ====================
    document.addEventListener('DOMContentLoaded', () => {
        // Telegram WebApp
        if (window.Telegram && Telegram.WebApp) {
            Telegram.WebApp.ready();
            Telegram.WebApp.expand();
            state.initData = Telegram.WebApp.initData || '';
        }

        // Fallback auth for non-WebApp opens (URL button in groups):
        // /admin appends ?admin_token=<HMAC> so we can authenticate
        // without Telegram.WebApp.initData.
        try {
            const params = new URLSearchParams(window.location.search);
            state.adminToken = params.get('admin_token') || '';
        } catch (e) {
            console.warn('admin_token parse failed:', e);
        }

        setupTabs();
        setupPollingToggle();
        setupSearch();
        setupTicketFilter();
        setupModal();
        setupPullToRefresh();
        setupPlansHandlers();
        setupRemindersHandlers();
        setupDpiHeatmapHandlers();
        setupProtoStatsHandlers();
        setupAlertsHandlers();
        setupSignalsHandlers();
        connectWebSocket();

        // Poll alerts + signals badges every 30s regardless of active
        // tab so the operator sees the unread counts on any tab.
        setInterval(() => fetchAlertsBadge().catch(() => {}), 30000);
        setInterval(() => fetchSignalsBadge().catch(() => {}), 30000);
        fetchAlertsBadge().catch(() => {});
        fetchSignalsBadge().catch(() => {});

        // Initial load
        loadTab('health');
    });

    // ==================== WebSocket ====================
    // Replaces the 15s polling for the Users + Health tabs. Server
    // pushes "online" snapshots every 5s plus "user_update" /
    // "alert" events synchronously after each admin action. The
    // socket reconnects with exponential backoff (max 30s) if the
    // network drops.
    function connectWebSocket() {
        if (state.ws) return;
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        const parts = [];
        if (state.initData) parts.push(`initData=${encodeURIComponent(state.initData)}`);
        if (state.adminToken) parts.push(`admin_token=${encodeURIComponent(state.adminToken)}`);
        const url = `${proto}//${location.host}/api/admin/ws?${parts.join('&')}`;
        let ws;
        try {
            ws = new WebSocket(url);
        } catch (e) {
            console.warn('WS construct failed:', e);
            scheduleReconnect();
            return;
        }
        state.ws = ws;
        ws.addEventListener('open', () => {
            state.wsReconnectMs = 1000;
            console.log('WS connected');
        });
        ws.addEventListener('message', ev => {
            try {
                const msg = JSON.parse(ev.data);
                handleWsEvent(msg);
            } catch (e) {
                // Plain "pong" / non-JSON — ignore.
            }
        });
        ws.addEventListener('close', () => {
            state.ws = null;
            scheduleReconnect();
        });
        ws.addEventListener('error', () => {
            try { ws.close(); } catch (e) {}
        });
    }

    function scheduleReconnect() {
        setTimeout(() => {
            connectWebSocket();
            state.wsReconnectMs = Math.min(state.wsReconnectMs * 2, 30000);
        }, state.wsReconnectMs);
    }

    function handleWsEvent(msg) {
        if (!msg || !msg.type) return;
        if (msg.type === 'online' && msg.data) {
            state.onlineByEmail = msg.data.by_email || {};
            const el = document.getElementById('stat-online-count');
            if (el) el.textContent = msg.data.count ?? '—';
            // If Users tab is open and we're filtering by online,
            // re-render so freshly-disconnected users disappear.
            if (state.activeTab === 'users' && state.onlineMode) {
                fetchUsers().catch(e => console.error(e));
            }
            return;
        }
        if (msg.type === 'user_update' && msg.data) {
            // Cheapest path: if Users tab is open, refresh the list.
            if (state.activeTab === 'users') {
                fetchUsers().catch(e => console.error(e));
            }
            showToast(`👤 ${msg.data.username || msg.data.chat_id} → ${msg.data.status}`, 'ok');
            return;
        }
    }

    // ==================== Tabs ====================
    function setupTabs() {
        document.querySelectorAll('.tab').forEach(btn => {
            btn.addEventListener('click', () => {
                const tab = btn.dataset.tab;
                if (tab === state.activeTab) return;

                // Update active states
                document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
                btn.classList.add('active');

                document.querySelectorAll('.tab-content').forEach(s => s.classList.remove('active'));
                document.getElementById('tab-' + tab).classList.add('active');

                state.activeTab = tab;
                loadTab(tab);
            });
        });
    }

    function loadTab(tab) {
        stopAllPolling();
        if (tab === 'health')   fetchHealth();
        if (tab === 'users')    fetchUsers();
        if (tab === 'stats')    fetchStats();
        if (tab === 'alerts')   { fetchAlerts(); fetchReports(); }
        if (tab === 'signals')  { fetchFailureReports(); fetchAsnHeatmap(); fetchGeoPoints(); }
        if (tab === 'settings') fetchSettings();
    }

    // ==================== Polling ====================
    function setupPollingToggle() {
        const btn = document.getElementById('btn-polling-toggle');
        btn.addEventListener('click', () => {
            state.pollingEnabled = !state.pollingEnabled;
            btn.classList.toggle('active', state.pollingEnabled);
            if (state.pollingEnabled) {
                startPolling(state.activeTab);
            } else {
                stopAllPolling();
            }
        });
    }

    function startPolling(tab) {
        stopAllPolling();
        state.pollTimers[tab] = setInterval(() => loadTab(tab), POLL_INTERVAL_MS);
    }

    function stopAllPolling() {
        Object.values(state.pollTimers).forEach(clearInterval);
        state.pollTimers = {};
    }

    // ==================== Pull-to-Refresh ====================
    function setupPullToRefresh() {
        let startY = 0;
        let pulling = false;
        const indicator = document.getElementById('ptr-indicator');

        document.addEventListener('touchstart', e => {
            if (window.scrollY === 0 && e.touches.length === 1) {
                startY = e.touches[0].clientY;
                pulling = true;
            }
        }, { passive: true });

        document.addEventListener('touchmove', e => {
            if (!pulling) return;
            const dy = e.touches[0].clientY - startY;
            if (dy > 80) {
                indicator.classList.add('visible');
            }
        }, { passive: true });

        document.addEventListener('touchend', () => {
            if (!pulling) return;
            pulling = false;
            if (indicator.classList.contains('visible')) {
                loadTab(state.activeTab);
                setTimeout(() => indicator.classList.remove('visible'), 1200);
            }
        });
    }

    // ==================== Search & Filters ====================
    function setupSearch() {
        const input = document.getElementById('search-input');
        let timer;
        input.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(() => fetchUsers(), DEBOUNCE_MS);
        });
    }

    function setupTicketFilter() {
        const btn = document.getElementById('btn-tickets');
        btn.addEventListener('click', () => {
            state.ticketMode = !state.ticketMode;
            btn.classList.toggle('active', state.ticketMode);
            if (state.ticketMode) {
                state.onlineMode = false;
                document.getElementById('btn-online').classList.remove('active');
            }
            fetchUsers();
        });

        // "Only online" toggle — filters the user list to those whose
        // email appears in the activity map (access.log in last 60s).
        // Mutually exclusive with the tickets filter so the bar
        // doesn't end up showing nothing.
        const onlineBtn = document.getElementById('btn-online');
        if (onlineBtn) {
            onlineBtn.addEventListener('click', () => {
                state.onlineMode = !state.onlineMode;
                onlineBtn.classList.toggle('active', state.onlineMode);
                if (state.onlineMode) {
                    state.ticketMode = false;
                    document.getElementById('btn-tickets').classList.remove('active');
                }
                fetchUsers();
            });
        }

        document.getElementById('status-filter').addEventListener('change', () => {
            state.ticketMode = false;
            state.onlineMode = false;
            document.getElementById('btn-tickets').classList.remove('active');
            document.getElementById('btn-online').classList.remove('active');
            fetchUsers();
        });

        const broadcastBtn = document.getElementById('btn-broadcast');
        if (broadcastBtn) {
            broadcastBtn.addEventListener('click', openBroadcastModal);
        }

        // Logs panel controls (Stats tab). Both refetch the tail.
        const logsRefresh = document.getElementById('logs-refresh');
        if (logsRefresh) {
            logsRefresh.addEventListener('click', () => {
                fetchLogs().catch(e => console.error('logs refresh:', e));
            });
        }
        const logsLevel = document.getElementById('logs-level-filter');
        if (logsLevel) {
            logsLevel.addEventListener('change', () => {
                fetchLogs().catch(e => console.error('logs level change:', e));
            });
        }
    }

    // ==================== Broadcast ====================
    function openBroadcastModal() {
        const bodyHtml = `
            <div class="broadcast-form">
                <label>Кому отправить:</label>
                <select id="broadcast-audience" class="status-filter">
                    <option value="active" selected>Активные (demo/paid/support)</option>
                    <option value="demo">Только demo</option>
                    <option value="all_known">Все знакомые (demo/paid/support/platform_select/pending)</option>
                </select>
                <label style="margin-top:10px;">Текст (HTML разрешён):</label>
                <textarea id="broadcast-text" class="broadcast-text" rows="6"
                          placeholder="Напишите сообщение для рассылки..."></textarea>
            </div>
        `;
        showModal('📢 Рассылка', bodyHtml, async () => {
            const text = (document.getElementById('broadcast-text').value || '').trim();
            const audience = document.getElementById('broadcast-audience').value;
            if (!text) {
                showToast('⚠️ Пустой текст');
                return;
            }
            try {
                const preview = await apiPost('/api/admin/broadcast',
                    { text, audience, confirm: false });
                confirmBroadcastSend(text, audience, preview);
            } catch (e) {
                showToast(`❌ ${e.message}`);
            }
        });
    }

    function confirmBroadcastSend(text, audience, preview) {
        const sampleHtml = (preview.sample || []).map(esc).join(', ');
        const bodyHtml = `
            <div>Получателей: <b>${preview.recipients_count}</b></div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:4px;">
                Примеры: ${sampleHtml || '—'}
            </div>
            <hr style="border:0;border-top:1px solid var(--border);margin:10px 0;">
            <div style="font-size:13px;white-space:pre-wrap;">${esc(text)}</div>
        `;
        showModal(`Отправить ${preview.recipients_count} пользователям?`, bodyHtml, async () => {
            try {
                const res = await apiPost('/api/admin/broadcast',
                    { text, audience, confirm: true });
                showToast(`📨 Отправлено: ${res.sent}, ошибок: ${res.failed}`);
            } catch (e) {
                showToast(`❌ ${e.message}`);
            }
        });
    }

    // ==================== API Helpers ====================
    function apiUrl(path) {
        const sep = path.includes('?') ? '&' : '?';
        const parts = [];
        if (state.initData) parts.push(`initData=${encodeURIComponent(state.initData)}`);
        if (state.adminToken) parts.push(`admin_token=${encodeURIComponent(state.adminToken)}`);
        if (!parts.length) parts.push(`initData=`);
        return `${API_BASE}${path}${sep}${parts.join('&')}`;
    }

    async function apiFetch(path) {
        const res = await fetch(apiUrl(path));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
    }

    async function apiPost(path, body) {
        const res = await fetch(apiUrl(path), {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.error || `HTTP ${res.status}`);
        }
        return res.json();
    }

    // ==================== Health Tab ====================
    async function fetchHealth() {
        try {
            const data = await apiFetch('/api/admin/health');
            renderSystem(data.system || {});
            const overview = data.nodes_overview || [];
            if (overview.length) {
                renderNodes(overview);
            } else {
                // Cluster manager isn't running on prod yet, so fall back to
                // nodes.json + per-node TCP probe — same UI either way.
                fetchNodesFromJson().catch(e => console.error('nodes fallback:', e));
            }
        } catch (e) {
            console.error('Health fetch error:', e);
            document.getElementById('nodes-list').innerHTML =
                '<div class="placeholder-text">⚠️ Ошибка загрузки</div>';
        }
    }

    async function fetchNodesFromJson() {
        const container = document.getElementById('nodes-list');
        let data;
        try {
            data = await apiFetch('/api/admin/nodes');
        } catch (e) {
            container.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const nodes = data.nodes || [];
        if (!nodes.length) {
            container.innerHTML = '<div class="placeholder-text">nodes.json пуст</div>';
            return;
        }
        const fmtGB = b => b == null ? '—'
            : b < 1024 * 1024 * 1024
                ? `${(b / 1024 / 1024).toFixed(1)} MB`
                : `${(b / 1024 / 1024 / 1024).toFixed(2)} GB`;
        container.innerHTML = nodes.map(n => {
            const reachable = n.probe?.reachable;
            const dotClass = reachable === true ? 'active' : reachable === false ? 'offline' : 'unknown';
            const lat = n.probe?.latency_ms;
            const probeLabel = reachable
                ? `:${n.probe.port} · ${lat}ms`
                : `:${n.probe.port} · ✗ unreachable`;
            return `
                <div class="node-card">
                    <div class="node-dot ${dotClass}"></div>
                    <div class="node-info">
                        <div class="node-name">${esc(n.name || n.id || '')}
                            ${n.is_primary ? '<span class="node-badge leader">primary</span>' : ''}
                        </div>
                        <div class="node-role">${esc(n.role || '')} · ${esc(n.region || '')}</div>
                        <div class="node-meta">
                            <span class="node-host">${esc(n.host || '')}</span>
                            <span class="node-probe">${probeLabel}</span>
                        </div>
                        ${n.traffic_total_bytes != null
                            ? `<div class="node-traffic">${fmtGB(n.traffic_total_bytes)} прошло</div>`
                            : ''}
                    </div>
                </div>`;
        }).join('');
    }

    function renderSystem(sys) {
        setRing('ring-cpu', sys.cpu?.percent ?? 0);
        setRing('ring-ram', sys.ram?.percent ?? 0);
        setRing('ring-disk', sys.disk?.percent ?? 0);
    }

    function setRing(id, pct) {
        const el = document.getElementById(id);
        if (!el) return;

        pct = Math.max(0, Math.min(100, pct));
        const circle = el.querySelector('.ring-fg');
        circle.setAttribute('stroke-dasharray', `${pct} ${100 - pct}`);

        // Color thresholds
        circle.classList.remove('warn', 'crit');
        if (pct >= 90) circle.classList.add('crit');
        else if (pct >= 75) circle.classList.add('warn');

        el.querySelector('.ring-value').textContent = Math.round(pct) + '%';
    }

    function renderNodes(nodes) {
        const container = document.getElementById('nodes-list');
        if (!nodes.length) {
            container.innerHTML = '<div class="placeholder-text">Нет данных о кластере</div>';
            return;
        }

        container.innerHTML = nodes.map(n => {
            const dotClass = n.status === 'active' ? 'active'
                : n.status === 'degraded' ? 'degraded'
                : n.status === 'offline' ? 'offline'
                : 'unknown';
            const badge = n.is_leader
                ? '<span class="node-badge leader">Leader</span>'
                : '';
            return `
                <div class="node-card">
                    <div class="node-dot ${dotClass}"></div>
                    <div class="node-info">
                        <div class="node-name">${esc(n.node_id)}</div>
                        <div class="node-role">${esc(n.role)}</div>
                    </div>
                    ${badge}
                </div>`;
        }).join('');
    }

    // ==================== Users Tab ====================
    async function fetchUsers() {
        const search = document.getElementById('search-input').value.trim();
        const statusVal = document.getElementById('status-filter').value;

        let path = '/api/admin/users?';
        if (state.ticketMode) {
            path += 'filter=tickets';
        } else {
            if (search) path += 'search=' + encodeURIComponent(search) + '&';
            if (statusVal) path += 'status=' + encodeURIComponent(statusVal);
        }

        try {
            const [data, onlineData] = await Promise.all([
                apiFetch(path),
                apiFetch('/api/admin/online_clients').catch(() => ({by_email: {}})),
            ]);
            state.onlineByEmail = onlineData.by_email || {};
            let users = data.users || [];
            if (state.onlineMode) {
                users = users.filter(u => u.email && state.onlineByEmail[u.email]);
            }
            renderUsers(users);
        } catch (e) {
            console.error('Users fetch error:', e);
            document.getElementById('users-list').innerHTML =
                '<div class="placeholder-text">⚠️ Ошибка загрузки</div>';
        }
    }

    function renderUsers(users) {
        const container = document.getElementById('users-list');
        const countEl = document.getElementById('users-count');

        if (!users.length) {
            container.innerHTML = '<div class="placeholder-text">Нет пользователей</div>';
            countEl.textContent = '';
            return;
        }

        countEl.textContent = `Показано: ${users.length}`;

        const activityMap = state.onlineByEmail || {};

        container.innerHTML = users.map(u => {
            const name = u.username ? `@${esc(u.username)}`
                : (u.contact_email ? `✉️ ${esc(u.contact_email)}` : 'no_username');
            const actions = getAvailableActions(u.status);
            const act = u.email ? activityMap[u.email] : null;
            const isOnline = !!(act && act.online);
            const ips = act && act.distinct_ips ? act.distinct_ips : 0;
            const conns = act && act.active_connections ? act.active_connections : 0;
            const rtt = act && act.avg_rtt_ms ? act.avg_rtt_ms : null;
            const overLimit = u.limit_ip && ips > u.limit_ip;
            // First IP's flag — quick visual; multi-country sharing
            // gets the 🚨 flag below.
            const firstIp = act && act.ips && act.ips.length ? act.ips[0] : null;
            const firstFlag = (firstIp && act.ip_geo && act.ip_geo[firstIp])
                ? act.ip_geo[firstIp].flag + ' ' : '';
            const onlineBadge = isOnline
                ? `<span title="онлайн · ${conns} соединений с ${ips} IP">🟢 ${firstFlag}${ips} IP</span>`
                : '';
            const sharingBadge = (act && act.sharing_flag && act.countries && act.countries.length > 1)
                ? `<span class="limit-over" title="один ключ из ${act.countries.length} стран — возможен шаринг">🚨 ${act.countries.join('/')}</span>`
                : '';
            const limitBadge = u.limit_ip
                ? `<span title="лимит IP" class="${overLimit ? 'limit-over' : ''}">🔢 ${esc(String(ips))}/${esc(String(u.limit_ip))}</span>`
                : '';
            const rttBadge = rtt !== null
                ? `<span title="средний RTT юзер→entry">📶 ${rtt} ms</span>`
                : '';

            return `
                <div class="user-card" data-chat-id="${esc(u.chat_id)}"
                     onclick="window.__openDetail('${esc(u.chat_id)}')">
                    <div class="user-card-header">
                        <div class="user-name">${onlineBadge} ${name}<span class="user-id">${esc(u.chat_id)}</span></div>
                        <span class="user-status-badge badge-${esc(u.status)}">${esc(u.status)}</span>
                    </div>
                    <div class="user-meta">
                        ${u.platform ? `<span>📱 ${esc(u.platform)}</span>` : ''}
                        ${u.consumed_gb ? `<span>📊 ${u.consumed_gb} GB</span>` : ''}
                        ${limitBadge}
                        ${rttBadge}
                        ${sharingBadge}
                        ${u.expiry ? `<span>⏰ ${esc(u.expiry)}</span>` : ''}
                        ${u.reject_count ? `<span>❌ ×${u.reject_count}</span>` : ''}
                    </div>
                    ${actions.length ? `
                        <div class="user-actions" onclick="event.stopPropagation()">
                            ${actions.map(a =>
                                `<button class="btn-action btn-${a}" onclick="window.__doAction('${esc(u.chat_id)}','${a}')">${actionLabel(a)}</button>`
                            ).join('')}
                        </div>` : ''}
                </div>`;
        }).join('');
    }

    // ==================== User Detail Modal ====================
    window.__openDetail = async function (chatId) {
        const overlay = document.getElementById('detail-overlay');
        const titleEl = document.getElementById('detail-title');
        const bodyEl = document.getElementById('detail-body');
        titleEl.textContent = `Пользователь #${chatId}`;
        bodyEl.innerHTML = '<div class="placeholder-text">Загрузка…</div>';
        overlay.classList.remove('hidden');

        let d;
        try {
            d = await apiFetch(`/api/admin/users/${encodeURIComponent(chatId)}/detail`);
        } catch (e) {
            bodyEl.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }

        const mb = b => (b / (1024 * 1024)).toFixed(1);
        const gb = b => (b / (1024 * 1024 * 1024)).toFixed(2);
        const t = d.traffic || {};
        const pct = Math.round((t.usage_ratio || 0) * 100);

        const activityMap = state.onlineByEmail || {};
        const act = d.email ? activityMap[d.email] : null;
        const isOnline = !!(act && act.online);
        const ipsCount = act ? (act.distinct_ips || 0) : 0;
        const ipsList = act && act.ips && act.ips.length
            ? act.ips.map(ip => {
                const g = act.ip_geo && act.ip_geo[ip];
                const prefix = g ? `${g.flag} ${g.cc} ` : '';
                return `<span>${prefix}<code>${esc(ip)}</code></span>`;
              }).join('<br>')
            : '—';
        const sharingLine = (act && act.countries && act.countries.length > 1)
            ? `<b class="limit-over">🚨 один ключ из ${act.countries.length} стран: ${act.countries.join(', ')}</b>`
            : '—';
        const limitOver = d.limit_ip && ipsCount > d.limit_ip;
        const ipLine = d.limit_ip
            ? `<b class="${limitOver ? 'limit-over' : ''}">${ipsCount} / ${d.limit_ip}</b>`
            : `<b>${ipsCount}</b>`;
        const activityLine = act && (act.active_connections || act.distinct_destinations)
            ? `${act.active_connections || 0} соединений · ${act.distinct_destinations || 0} назначений`
            : '—';
        const lastSeen = act && act.last_seen ? act.last_seen : '—';
        const rttLine = act && act.avg_rtt_ms
            ? `<b>${act.avg_rtt_ms} ms</b> (средний user→entry)`
            : '—';
        const trafficLine = (t && t.total)
            ? `<b>${gb(t.total)} GB</b>${t.quota_bytes ? ` из ${gb(t.quota_bytes)} GB` : ''}`
            : '0 GB';
        const meta = [
            ['Статус', `<b>${esc(d.status || '—')}</b>`],
            ['Онлайн', isOnline ? '🟢 да' : '⚪ нет'],
            ['IP сейчас (60с)', ipLine],
            ['IP адреса', ipsList],
            ['Гео', sharingLine],
            ['RTT', rttLine],
            ['Трафик', trafficLine],
            ['Активность (60с)', activityLine],
            ['Последнее соединение', lastSeen],
            ['Язык', d.lang || '—'],
            ['Платформа', d.platform || '—'],
            ['Создан', d.created_at || '—'],
            ['Подписка до', d.subscription_expiry || '—'],
            ['Reject count', d.reject_count || 0],
            ['Квота', d.quota_gb ? `${d.quota_gb} GB` : '—'],
        ];

        const credBlock = d.uuid ? `
            <div class="detail-row"><span class="k">UUID</span><span class="v"><code>${esc(d.uuid)}</code></span></div>
            <div class="detail-row"><span class="k">Email</span><span class="v"><code>${esc(d.email || '')}</code></span></div>
        ` : '<div class="detail-row"><span class="k">VPN-ключ</span><span class="v">не выдан</span></div>';

        const trafficBlock = t.quota_bytes > 0 ? `
            <div class="detail-traffic">
                <div class="detail-traffic-bar">
                    <div class="detail-traffic-fill" style="width:${Math.min(100, pct)}%"></div>
                </div>
                <div class="detail-traffic-line">
                    ↑ ${mb(t.up)} MB · ↓ ${mb(t.down)} MB ·
                    <b>${gb(t.total)} / ${gb(t.quota_bytes)} GB</b> (${pct}%)
                </div>
            </div>
        ` : '<div class="detail-traffic-line">Трафика нет</div>';

        const historyBlock = (d.recent_admin_actions || []).length ? `
            <div class="detail-history">
                ${d.recent_admin_actions.map(h => `
                    <div class="history-row">
                        <span class="history-when">${esc(h.at || '')}</span>
                        <span class="history-action">${esc(h.action || '')}</span>
                        <span class="history-by">by ${esc(h.admin_id || '')}</span>
                    </div>
                `).join('')}
            </div>` : '<div class="placeholder-text">История админ-действий пуста</div>';

        // Editable mini-controls — limit_ip, quota_gb, expire date.
        // Each posts {action, value} to the existing user-action endpoint
        // and re-opens the detail to pick up server-side state.
        const limitVal = d.limit_ip ?? '';
        const quotaVal = d.quota_gb ?? '';
        const expireDate = (d.subscription_expiry || '').slice(0, 10);
        // Mobile admins mostly live in this modal — give it the same
        // paid-upgrade shortcut the list card has.
        const paidBlock = ['demo', 'support_topic'].includes(d.status) ? `
            <div class="detail-edit-row">
                <button class="btn btn-primary" id="detail-grant-paid">⭐ Сделать paid (100 ГБ, +30 дней)</button>
            </div>
        ` : '';

        const editBlock = `
            <div class="section-title">Управление лимитами</div>
            ${paidBlock}
            <div class="detail-edit-row">
                <label>Limit IP</label>
                <input type="number" min="0" max="100" step="1" id="edit-limit-ip" value="${esc(String(limitVal))}">
                <button class="btn btn-secondary" data-edit-action="set_limit_ip" data-edit-input="edit-limit-ip">Сохранить</button>
            </div>
            <div class="detail-edit-row">
                <label>Квота (ГБ)</label>
                <input type="number" min="0" step="0.5" id="edit-quota" value="${esc(String(quotaVal))}">
                <button class="btn btn-secondary" data-edit-action="set_quota" data-edit-input="edit-quota">Сохранить</button>
            </div>
            <div class="detail-edit-row">
                <label>Подписка до</label>
                <input type="date" id="edit-expire" value="${esc(expireDate)}">
                <button class="btn btn-secondary" data-edit-action="set_expire" data-edit-input="edit-expire">Сохранить</button>
            </div>
        `;

        // Sparkline canvas — chart is mounted after the modal is in
        // the DOM (Chart.js needs a sized canvas element).
        const chartBlock = d.email ? `
            <div class="section-title">Трафик · 14 дней</div>
            <div class="traffic-chart-wrap">
                <canvas id="traffic-chart" height="80"></canvas>
            </div>
        ` : '';

        bodyEl.innerHTML = `
            <div class="detail-meta">
                ${meta.map(([k, v]) => `<div class="detail-row"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`).join('')}
            </div>
            <div class="section-title">VPN</div>
            ${credBlock}
            <div class="section-title">Трафик</div>
            ${trafficBlock}
            ${chartBlock}
            ${editBlock}
            <div class="section-title">Последние действия</div>
            ${historyBlock}
        `;

        // Fetch + render the sparkline. Best-effort: if Chart.js
        // didn't load (offline / blocked), or the API errors, the
        // canvas just stays blank — nothing else breaks.
        if (d.email && window.Chart) {
            renderTrafficChart(d.email).catch(e => console.error('traffic chart:', e));
        }

        // Paid-upgrade shortcut — same confirm flow as the list card,
        // then re-open the detail to show the new status/quota/expiry.
        const paidBtn = bodyEl.querySelector('#detail-grant-paid');
        if (paidBtn) {
            paidBtn.addEventListener('click', () => {
                // Close the detail first — __doAction opens its own
                // confirm modal, and the list refreshes on success.
                overlay.classList.add('hidden');
                window.__doAction(chatId, 'grant_paid');
            });
        }

        // Wire the three save buttons. Same code path; the action and
        // input element come from data- attributes on the button.
        bodyEl.querySelectorAll('[data-edit-action]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const action = btn.getAttribute('data-edit-action');
                const inp = bodyEl.querySelector('#' + btn.getAttribute('data-edit-input'));
                const value = inp ? inp.value : '';
                btn.disabled = true;
                btn.textContent = 'Сохраняю…';
                try {
                    await apiPost(
                        `/api/admin/users/${encodeURIComponent(chatId)}/action`,
                        { action, value },
                    );
                    showToast('✅ Сохранено', 'ok');
                    // Re-open detail so the new value renders.
                    window.__openDetail(chatId);
                } catch (e) {
                    showToast('⚠️ ' + (e.message || 'не удалось'), 'err');
                    btn.disabled = false;
                    btn.textContent = 'Сохранить';
                }
            });
        });
    };

    async function renderTrafficChart(email) {
        const ctx = document.getElementById('traffic-chart');
        if (!ctx) return;
        let data;
        try {
            data = await apiFetch(
                `/api/admin/traffic_history?email=${encodeURIComponent(email)}&days=14`
            );
        } catch (e) {
            return;
        }
        const points = data.points || [];
        if (!points.length) {
            ctx.parentElement.innerHTML =
                '<div class="placeholder-text">пока нет снепшотов трафика</div>';
            return;
        }
        // delta_bytes per 30-min snapshot — area chart shows
        // consumption rate over time.
        const labels = points.map(p => (p.ts || '').slice(5, 16));  // MM-DD HH:MM
        const values = points.map(p => p.delta_bytes / (1024 * 1024));  // MB
        new window.Chart(ctx, {
            type: 'line',
            data: {
                labels,
                datasets: [{
                    label: 'MB / 30 мин',
                    data: values,
                    fill: true,
                    backgroundColor: 'rgba(80, 180, 255, 0.2)',
                    borderColor: 'rgba(80, 180, 255, 1)',
                    borderWidth: 1.5,
                    tension: 0.2,
                    pointRadius: 0,
                }],
            },
            options: {
                animation: false,
                responsive: true,
                maintainAspectRatio: false,
                scales: {
                    x: { display: false },
                    y: {
                        beginAtZero: true,
                        ticks: { color: 'rgba(150,150,150,0.7)', font: { size: 10 } },
                        grid: { color: 'rgba(255,255,255,0.05)' },
                    },
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: ctx => `${ctx.parsed.y.toFixed(1)} MB`,
                        },
                    },
                },
            },
        });
    }

    document.getElementById('detail-close').addEventListener('click', () => {
        document.getElementById('detail-overlay').classList.add('hidden');
    });
    document.getElementById('detail-overlay').addEventListener('click', e => {
        if (e.target === e.currentTarget) {
            e.currentTarget.classList.add('hidden');
        }
    });

    function getAvailableActions(status) {
        switch (status) {
            case 'new':             return ['ban', 'reset'];
            case 'pending_demo':    return ['approve', 'reject', 'ban'];
            case 'platform_select': return ['reset', 'reject', 'ban'];
            case 'rejected':        return ['approve', 'ban', 'reset'];
            case 'demo':            return ['grant_paid', 'grant_100gb', 'revoke', 'ban', 'reset'];
            case 'paid':            return ['grant_100gb', 'revoke', 'ban', 'reset'];
            case 'support_topic':   return ['grant_paid', 'grant_100gb', 'revoke', 'ban', 'reset'];
            case 'banned':          return ['unban', 'reset'];
            default:                return ['reset'];
        }
    }

    function actionLabel(action) {
        const map = {
            approve: '✅ Approve',
            reject: '🚫 Reject',
            ban: '⛔ Ban',
            unban: '🔓 Unban',
            revoke: '🗝️ Revoke',
            reset: '🔄 Reset',
            grant_100gb: '🎁 +100GB',
            grant_paid: '⭐ Paid',
        };
        return map[action] || action;
    }

    // ==================== User Actions ====================
    window.__doAction = function (chatId, action) {
        const labels = {
            approve: 'одобрить заявку',
            reject: 'отклонить заявку',
            ban: 'забанить пользователя',
            unban: 'разбанить пользователя',
            revoke: 'отозвать ключ и забанить',
            reset: 'сбросить пользователя (всё обнулится)',
            grant_100gb: 'выдать +100 ГБ к квоте',
            grant_paid: 'сделать пользователя платным (полный доступ)',
        };
        showModal(
            `Подтвердите: ${labels[action]}`,
            `Пользователь: <b>${esc(chatId)}</b><br>Действие: <b>${labels[action]}</b>`,
            async () => {
                try {
                    await apiPost(`/api/admin/users/${chatId}/action`, { action });
                    showToast(`✅ ${actionLabel(action)} → ${chatId}`);
                    fetchUsers(); // refresh
                } catch (e) {
                    showToast(`❌ Ошибка: ${e.message}`);
                }
            }
        );
    };

    // ==================== Stats Tab ====================
    async function fetchStats() {
        try {
            const data = await apiFetch('/api/admin/stats');
            renderRegistrations(data.registrations || {});
            renderStatusBars(data.users || {});
            renderTraffic(data.traffic || {});
        } catch (e) {
            console.error('Stats fetch error:', e);
        }
        // Audit + system info + logs + x-ui + subs run in parallel with
        // stats; each failure is isolated so a broken endpoint can't
        // blank out the whole tab.
        fetchAudit().catch(e => console.error('audit fetch:', e));
        fetchSystem().catch(e => console.error('system fetch:', e));
        fetchLogs().catch(e => console.error('logs fetch:', e));
        fetchXuiClients().catch(e => console.error('xui fetch:', e));
        fetchSubscriptions().catch(e => console.error('subs fetch:', e));
        fetchAndRenderOnline().catch(e => console.error('online fetch:', e));
        fetchDpiHeatmap().catch(e => console.error('dpi fetch:', e));
        fetchProtoStats().catch(e => console.error('proto fetch:', e));
    }

    // ==================== Settings tab orchestrator ====================
    // All operator-tunable editors live here. Each fetcher is isolated
    // so a broken endpoint doesn't blank the whole tab.
    async function fetchSettings() {
        fetchCascade().catch(e => console.error('cascade fetch:', e));
        fetchKtexts().catch(e => console.error('ktexts fetch:', e));
        fetchPlans().catch(e => console.error('plans fetch:', e));
        fetchReminders().catch(e => console.error('reminders fetch:', e));
    }

    // ==================== Cascade order editor ====================
    // Drag-free reorder: each row has ↑/↓ buttons. We render the
    // current order top-to-bottom (first protocol on top), and save
    // the array of protocol short-names on click.
    async function fetchCascade() {
        const box = document.getElementById('cascade-editor');
        if (!box) return;
        let data;
        try {
            data = await apiFetch('/api/admin/cascade_order');
        } catch (e) {
            box.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const cfg = data.config || (data.order || []).map(n => ({name: n, enabled: true}));
        const catalog = {};
        (data.catalog || []).forEach(p => { catalog[p.name] = p; });
        // Position numbering counts only ENABLED rows so disabled rows
        // don't claim a slot in the user-facing cascade.
        const renderRow = (entry, i, total) => {
            const p = catalog[entry.name] || { title: entry.name, desc: '', tier: 'paid' };
            const enabled = !!entry.enabled;
            const tierBadge = p.tier === 'free'
                ? '<span class="cascade-tier cascade-tier-free" title="Доступен на демо">free</span>'
                : '<span class="cascade-tier cascade-tier-paid" title="Только для платных подписок (светит entry-IP)">paid</span>';
            const enabledIdx = enabled
                ? (() => {
                    // Number of enabled rows that come before this one.
                    let n = 0;
                    for (let j = 0; j < i; j++) if (currentCfg[j].enabled) n++;
                    return n;
                })()
                : -1;
            const posLabel = enabled
                ? (enabledIdx === 0 ? '★' : '#' + enabledIdx)
                : '—';
            return `
            <div class="cascade-row ${enabled ? '' : 'cascade-row-disabled'}"
                 data-name="${esc(entry.name)}" data-pos="${i}">
                <div class="cascade-pos">${posLabel}</div>
                <label class="cascade-toggle" title="Включить / выключить протокол">
                    <input type="checkbox" class="cascade-enabled" ${enabled ? 'checked' : ''}>
                </label>
                <div class="cascade-body">
                    <div class="cascade-title">${esc(p.title)} ${tierBadge}</div>
                    <div class="cascade-desc">${esc(p.desc)}</div>
                </div>
                <div class="cascade-controls">
                    <button class="btn-chip cascade-up"   ${i === 0 ? 'disabled' : ''}>↑</button>
                    <button class="btn-chip cascade-down" ${i === total - 1 ? 'disabled' : ''}>↓</button>
                </div>
            </div>`;
        };
        let currentCfg = [...cfg];
        const renderAll = () => {
            box.innerHTML = currentCfg
                .map((entry, i) => renderRow(entry, i, currentCfg.length))
                .join('');
            box.querySelectorAll('.cascade-up').forEach(btn => {
                btn.addEventListener('click', e => {
                    const row = e.target.closest('.cascade-row');
                    const pos = parseInt(row.dataset.pos, 10);
                    if (pos > 0) {
                        const [moved] = currentCfg.splice(pos, 1);
                        currentCfg.splice(pos - 1, 0, moved);
                        renderAll();
                        box.dataset.dirty = '1';
                    }
                });
            });
            box.querySelectorAll('.cascade-down').forEach(btn => {
                btn.addEventListener('click', e => {
                    const row = e.target.closest('.cascade-row');
                    const pos = parseInt(row.dataset.pos, 10);
                    if (pos < currentCfg.length - 1) {
                        const [moved] = currentCfg.splice(pos, 1);
                        currentCfg.splice(pos + 1, 0, moved);
                        renderAll();
                        box.dataset.dirty = '1';
                    }
                });
            });
            box.querySelectorAll('.cascade-enabled').forEach(cb => {
                cb.addEventListener('change', e => {
                    const row = e.target.closest('.cascade-row');
                    const pos = parseInt(row.dataset.pos, 10);
                    currentCfg[pos].enabled = e.target.checked;
                    renderAll();
                    box.dataset.dirty = '1';
                });
            });
        };
        box.dataset.dirty = '0';
        box._currentCfg = currentCfg;
        // The closure captures currentCfg; we need to expose it on the
        // element so the save handler can read the live state too.
        renderAll();
    }
    async function saveCascade() {
        const box = document.getElementById('cascade-editor');
        const status = document.getElementById('cascade-status');
        if (!box) return;
        // Re-derive config from DOM (source of truth = what user sees)
        const config = Array.from(box.querySelectorAll('.cascade-row')).map(r => ({
            name: r.dataset.name,
            enabled: r.querySelector('.cascade-enabled')?.checked ?? true,
        }));
        try {
            const res = await apiPost('/api/admin/cascade_order', { config });
            box.dataset.order = JSON.stringify(res.order || []);
            box.dataset.dirty = '0';
            status.textContent = `✓ сохранено: ${(res.order || []).join(' → ')}`;
            status.classList.add('ok');
            setTimeout(() => {
                status.textContent = '';
                status.classList.remove('ok');
            }, 4000);
        } catch (e) {
            status.textContent = `⚠️ ${e.message}`;
            status.classList.add('err');
        }
    }
    async function resetCascade() {
        const box = document.getElementById('cascade-editor');
        if (!box) return;
        try {
            const res = await apiPost('/api/admin/cascade_order', { order: [] });
            await fetchCascade();
            const status = document.getElementById('cascade-status');
            status.textContent = `↺ сброшено к: ${(res.order || []).join(' → ')}`;
            status.classList.add('ok');
            setTimeout(() => {
                status.textContent = '';
                status.classList.remove('ok');
            }, 4000);
        } catch (e) {
            console.error('cascade reset:', e);
        }
    }
    document.getElementById('cascade-reload')?.addEventListener('click', () => fetchCascade());
    document.getElementById('cascade-reset')?.addEventListener('click', () => resetCascade());
    document.getElementById('cascade-save')?.addEventListener('click', () => saveCascade());

    // ==================== Key-message texts editor ====================
    // Per-language form: button label, footer, and title+desc for
    // each protocol. Empty = bot uses default. Placeholders show
    // exactly what default the user would see.
    let _ktextsData = null;
    async function fetchKtexts() {
        const box = document.getElementById('ktexts-editor');
        if (!box) return;
        try {
            _ktextsData = await apiFetch('/api/admin/key_texts');
        } catch (e) {
            box.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        renderKtexts();
    }
    function renderKtexts() {
        const box = document.getElementById('ktexts-editor');
        if (!box || !_ktextsData) return;
        const lang = document.getElementById('ktexts-lang').value;
        const defaults = _ktextsData.defaults[lang] || {};
        const overrides = (_ktextsData.overrides || {})[lang] || {};
        const protos = _ktextsData.protocols || [];
        const dProtos = defaults.protocols || {};
        const oProtos = overrides.protocols || {};
        const protoRows = protos.map(name => {
            const d = dProtos[name] || {};
            const o = oProtos[name] || {};
            return `
            <div class="ktext-proto" data-proto="${esc(name)}">
                <div class="ktext-proto-name">${esc(name)}</div>
                <label>Заголовок</label>
                <input type="text" class="ktext-input ktext-title"
                       value="${esc(o.title || '')}"
                       placeholder="${esc(d.title || '')}">
                <label>Описание</label>
                <textarea class="ktext-input ktext-desc"
                       rows="2"
                       placeholder="${esc(d.desc || '')}">${esc(o.desc || '')}</textarea>
            </div>`;
        }).join('');
        box.innerHTML = `
        <div class="ktext-section">
            <label>Текст кнопки (call-to-action)</label>
            <input type="text" id="ktext-button" class="ktext-input"
                   value="${esc(overrides.button || '')}"
                   placeholder="${esc(defaults.button || '')}">
        </div>
        <div class="ktext-section">
            <label>Подсказка над ключом</label>
            <textarea id="ktext-footer" class="ktext-input" rows="2"
                   placeholder="${esc(defaults.footer || '')}">${esc(overrides.footer || '')}</textarea>
        </div>
        <div class="ktext-section">
            <div class="ktext-section-title">Протоколы</div>
            ${protoRows}
        </div>`;
    }
    async function saveKtexts() {
        if (!_ktextsData) return;
        const status = document.getElementById('ktexts-status');
        // Walk both languages currently in memory + the one being edited
        // in the form, send a complete blob to the server.
        const lang = document.getElementById('ktexts-lang').value;
        const currentLangOverrides = {};
        const btn = document.getElementById('ktext-button').value.trim();
        const ftr = document.getElementById('ktext-footer').value.trim();
        if (btn) currentLangOverrides.button = btn;
        if (ftr) currentLangOverrides.footer = ftr;
        const protoOut = {};
        document.querySelectorAll('.ktext-proto').forEach(div => {
            const name = div.dataset.proto;
            const title = div.querySelector('.ktext-title').value.trim();
            const desc = div.querySelector('.ktext-desc').value.trim();
            const out = {};
            if (title) out.title = title;
            if (desc)  out.desc = desc;
            if (Object.keys(out).length) protoOut[name] = out;
        });
        if (Object.keys(protoOut).length) currentLangOverrides.protocols = protoOut;
        const allOverrides = { ..._ktextsData.overrides };
        if (Object.keys(currentLangOverrides).length) {
            allOverrides[lang] = currentLangOverrides;
        } else {
            delete allOverrides[lang];
        }
        try {
            const res = await apiPost('/api/admin/key_texts', { overrides: allOverrides });
            _ktextsData.overrides = res.overrides || {};
            status.textContent = '✓ сохранено';
            status.classList.add('ok');
            setTimeout(() => {
                status.textContent = ''; status.classList.remove('ok');
            }, 4000);
        } catch (e) {
            status.textContent = `⚠️ ${e.message}`;
            status.classList.add('err');
        }
    }
    document.getElementById('ktexts-reload')?.addEventListener('click', () => fetchKtexts());
    document.getElementById('ktexts-lang')?.addEventListener('change', () => renderKtexts());
    document.getElementById('ktexts-save')?.addEventListener('click', () => saveKtexts());

    // ==================== Plans (Stars pricing) ====================
    // Render: input per plan + per-month hint. Save: POST only the
    // rows the operator actually edited.
    async function fetchPlans() {
        const box = document.getElementById('plans-editor');
        if (!box) return;
        let data;
        try {
            data = await apiFetch('/api/admin/plans');
        } catch (e) {
            box.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const plans = data.plans || [];
        if (!plans.length) {
            box.innerHTML = '<div class="placeholder-text">Нет планов</div>';
            return;
        }
        box.innerHTML = plans.map(p => `
            <div class="plan-row" data-months="${p.months}" data-original="${p.stars}" data-factory="${p.factory_stars}">
                <div>
                    <div class="plan-label">${esc(p.label)}</div>
                    <div class="plan-permonth">${p.per_month} ⭐/мес${p.stars !== p.factory_stars ? ` · factory ${p.factory_stars}` : ''}</div>
                </div>
                <input type="number" min="1" step="1" value="${p.stars}" class="plan-input">
                <span class="plan-star">⭐</span>
                <button class="btn-chip plan-reset" title="Сбросить к factory">↺</button>
            </div>
        `).join('');
        box.querySelectorAll('.plan-input').forEach(inp => {
            inp.addEventListener('input', e => {
                const row = e.target.closest('.plan-row');
                row.classList.toggle(
                    'dirty',
                    String(e.target.value) !== row.dataset.original,
                );
            });
        });
        box.querySelectorAll('.plan-reset').forEach(btn => {
            btn.addEventListener('click', e => {
                const row = e.target.closest('.plan-row');
                const input = row.querySelector('.plan-input');
                input.value = row.dataset.factory;
                row.classList.add('dirty');
                row.dataset.resetRequested = '1';
            });
        });
        const status = document.getElementById('plans-status');
        if (status) { status.textContent = ''; status.className = 'plans-status'; }
    }

    async function savePlans() {
        const box = document.getElementById('plans-editor');
        const status = document.getElementById('plans-status');
        if (!box) return;
        const prices = {};
        let edited = 0;
        box.querySelectorAll('.plan-row.dirty').forEach(row => {
            const months = row.dataset.months;
            if (row.dataset.resetRequested === '1') {
                prices[months] = null;
            } else {
                const v = parseInt(row.querySelector('.plan-input').value, 10);
                if (!Number.isNaN(v) && v > 0) prices[months] = v;
            }
            edited++;
        });
        if (!edited) {
            if (status) { status.textContent = 'нет изменений'; status.className = 'plans-status'; }
            return;
        }
        if (status) { status.textContent = 'сохраняю…'; status.className = 'plans-status'; }
        try {
            await apiFetch('/api/admin/plans', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({prices}),
            });
            if (status) { status.textContent = '✅ сохранено'; status.className = 'plans-status ok'; }
            fetchPlans();
        } catch (e) {
            if (status) { status.textContent = '⚠️ ' + e.message; status.className = 'plans-status err'; }
        }
    }

    function setupPlansHandlers() {
        const saveBtn = document.getElementById('plans-save');
        if (saveBtn) saveBtn.addEventListener('click', savePlans);
        const reloadBtn = document.getElementById('plans-reload');
        if (reloadBtn) reloadBtn.addEventListener('click', fetchPlans);
    }

    // ==================== Reminders (settings + manual send) ====================
    // Field metadata — label + hint shown next to each editor row.
    // Keys mirror the backend's REMINDER_DEFAULTS / NUMERIC_REMINDER_KEYS.
    const REMINDER_FIELDS = [
        {key: 'reminder_interval_hours',      label: 'Интервал крона',          hint: 'часов (применяется после рестарта бота)'},
        {key: 'reminder_min_age_hours',       label: 'Возраст до 1-го реминдера', hint: 'часов с регистрации'},
        {key: 'reminder_repeat_after_hours',  label: 'Не чаще чем раз в',        hint: 'часов между повторами'},
        {key: 'reminder_max_per_user',        label: 'Макс реминдеров на юзера', hint: 'штук'},
        {key: 'reminder_delete_after_days',   label: 'Auto-reject через',        hint: 'дней (0 = выключено)'},
    ];
    const REMINDER_TEXTS = [
        {key: 'reminder_text_new_ru',        label: 'NEW (ru)'},
        {key: 'reminder_text_new_en',        label: 'NEW (en)'},
        {key: 'reminder_text_platform_ru',   label: 'PLATFORM_SELECT (ru)'},
        {key: 'reminder_text_platform_en',   label: 'PLATFORM_SELECT (en)'},
    ];

    async function fetchReminders() {
        const box = document.getElementById('reminders-editor');
        if (!box) return;
        let data;
        try {
            data = await apiFetch('/api/admin/reminders');
        } catch (e) {
            box.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const settings = data.settings || {};
        const cohorts = data.cohorts_eligible || {};
        const numRows = REMINDER_FIELDS.map(f => {
            const s = settings[f.key] || {};
            const val = s.value ?? s.factory ?? '';
            const factory = s.factory ?? '';
            const isFactory = String(val) === String(factory);
            return `<div class="reminder-row" data-key="${f.key}" data-numeric="1" data-original="${val}" data-factory="${factory}">
                <div>
                    <div class="reminder-label">${esc(f.label)}</div>
                    <div class="reminder-hint">${esc(f.hint)}${isFactory ? '' : ` · factory ${esc(factory)}`}</div>
                </div>
                <input type="number" min="0" step="1" value="${esc(val)}" class="reminder-input">
            </div>`;
        }).join('');
        const textRows = REMINDER_TEXTS.map(f => {
            const s = settings[f.key] || {};
            const val = s.value ?? s.factory ?? '';
            const factory = s.factory ?? '';
            const isFactory = String(val) === String(factory);
            return `<div class="reminder-row row-text" data-key="${f.key}" data-numeric="0" data-factory="${esc(factory)}">
                <div class="reminder-label">${esc(f.label)} ${isFactory ? '' : '<span class="reminder-hint">(изменён)</span>'}</div>
                <textarea class="reminder-textarea" data-original="${esc(val)}">${esc(val)}</textarea>
            </div>`;
        }).join('');
        box.innerHTML = `
            <div class="cohort-counts">
                <span class="badge">eligible NEW: ${cohorts.new ?? '?'}</span>
                <span class="badge">eligible PLATFORM_SELECT: ${cohorts.platform_select ?? '?'}</span>
            </div>
            <div class="reminder-section">
                <div class="reminder-section-title">Настройки</div>
                ${numRows}
            </div>
            <div class="reminder-section">
                <div class="reminder-section-title">Тексты</div>
                ${textRows}
            </div>
        `;
        box.querySelectorAll('.reminder-input').forEach(inp => {
            inp.addEventListener('input', e => {
                const row = e.target.closest('.reminder-row');
                row.classList.toggle('dirty', String(e.target.value) !== row.dataset.original);
            });
        });
        box.querySelectorAll('.reminder-textarea').forEach(ta => {
            ta.addEventListener('input', e => {
                const row = e.target.closest('.reminder-row');
                row.classList.toggle('dirty', e.target.value !== ta.dataset.original);
            });
        });
        const status = document.getElementById('reminders-status');
        if (status) { status.textContent = ''; status.className = 'plans-status'; }
    }

    async function saveReminders() {
        const box = document.getElementById('reminders-editor');
        const status = document.getElementById('reminders-status');
        if (!box) return;
        const body = {};
        let edited = 0;
        box.querySelectorAll('.reminder-row.dirty').forEach(row => {
            const key = row.dataset.key;
            if (row.dataset.numeric === '1') {
                const v = parseInt(row.querySelector('.reminder-input').value, 10);
                if (!Number.isNaN(v) && v >= 0) body[key] = v;
            } else {
                body[key] = row.querySelector('.reminder-textarea').value;
            }
            edited++;
        });
        if (!edited) {
            if (status) { status.textContent = 'нет изменений'; status.className = 'plans-status'; }
            return;
        }
        if (status) { status.textContent = 'сохраняю…'; status.className = 'plans-status'; }
        try {
            await apiFetch('/api/admin/reminders', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body),
            });
            if (status) { status.textContent = '✅ сохранено'; status.className = 'plans-status ok'; }
            fetchReminders();
        } catch (e) {
            if (status) { status.textContent = '⚠️ ' + e.message; status.className = 'plans-status err'; }
        }
    }

    async function manualSendCohort() {
        const cohort = document.getElementById('manual-cohort').value;
        const force = document.getElementById('manual-force').checked;
        const status = document.getElementById('manual-status');
        if (!confirm(`Отправить реминдер всем юзерам когорты «${cohort}»${force ? ' (без cooldown)' : ''}?`)) return;
        if (status) { status.textContent = 'отправляю…'; status.className = 'plans-status'; }
        try {
            const r = await apiFetch('/api/admin/reminders/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({scope: 'cohort', cohort, force}),
            });
            if (status) { status.textContent = `✅ отправлено: ${r.sent}`; status.className = 'plans-status ok'; }
        } catch (e) {
            if (status) { status.textContent = '⚠️ ' + e.message; status.className = 'plans-status err'; }
        }
    }

    async function manualSendUser() {
        const chatId = document.getElementById('manual-chat-id').value.trim();
        const force = document.getElementById('manual-force').checked;
        const status = document.getElementById('manual-status');
        if (!chatId) {
            if (status) { status.textContent = 'укажи chat_id'; status.className = 'plans-status err'; }
            return;
        }
        if (status) { status.textContent = 'отправляю…'; status.className = 'plans-status'; }
        try {
            await apiFetch('/api/admin/reminders/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({scope: 'user', chat_id: chatId, force}),
            });
            if (status) { status.textContent = '✅ отправлено'; status.className = 'plans-status ok'; }
        } catch (e) {
            if (status) { status.textContent = '⚠️ ' + e.message; status.className = 'plans-status err'; }
        }
    }

    // ==================== DPI heatmap ====================
    // Two-level: country row (totals + click to expand) → operators
    // (ASN bars sorted by short-session ratio). Empty/no-flag rows
    // show as "??" — still useful (means GeoIP didn't match the IP).
    function ccToFlag(cc) {
        if (!cc || cc.length !== 2 || !/^[A-Z]{2}$/.test(cc)) return '🏳️';
        const base = 0x1F1E6;
        return String.fromCodePoint(base + cc.charCodeAt(0) - 65)
             + String.fromCodePoint(base + cc.charCodeAt(1) - 65);
    }

    async function fetchDpiHeatmap() {
        const box = document.getElementById('dpi-heatmap');
        if (!box) return;
        const hours = document.getElementById('dpi-hours')?.value || '24';
        let data;
        try {
            data = await apiFetch(`/api/admin/dpi_metrics?hours=${hours}`);
        } catch (e) {
            box.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const countries = data.countries || [];
        const globalRow = data.global;
        if (!countries.length && !globalRow) {
            box.innerHTML = `<div class="placeholder-text">Нет данных за окно ${hours}ч (collector ещё не накопил)</div>`;
            return;
        }
        const cls = ratio => ratio > 0.4 ? 'crit' : ratio > 0.15 ? 'warn' : '';
        const pct = ratio => `${Math.round((ratio || 0) * 100)}%`;
        // baseline_ratio: 1.0 = norm; >2 = warn; >5 = crit
        const blCls = r => r === null || r === undefined ? ''
                     : r > 5 ? 'crit' : r > 2 ? 'warn' : '';

        let html = '';
        // Host-wide top row: total TCP aborts in window.
        if (globalRow) {
            const rstTotal = globalRow.operators?.reduce(
                (s, o) => s + (o.rst_count || 0), 0,
            ) || 0;
            html += `<div class="dpi-row-country" data-cc="GLOBAL">
                <div class="dpi-country-head" style="cursor:default">
                    <span class="dpi-flag">🌐</span>
                    <div>
                        <div class="dpi-country-name">HOST WIDE</div>
                        <div class="dpi-country-stats">TCP aborts (RST) in window</div>
                    </div>
                    <span class="dpi-country-stats ${rstTotal > 1000 ? 'crit' : rstTotal > 200 ? 'warn' : ''}">
                        ${rstTotal} RST
                    </span>
                    <div class="dpi-bar"></div>
                    <span></span>
                </div>
            </div>`;
        }
        html += countries.map(c => {
            const flag = ccToFlag(c.country);
            const barW = Math.min(100, Math.round(c.short_ratio * 100));
            const opsHtml = c.operators.map(op => {
                const probes = (op.probe_ips || [])
                    .slice(0, 3)
                    .map(([ip, n]) => `${esc(ip)}×${n}`)
                    .join(', ');
                const reasons = Object.entries(op.reason_buckets || {})
                    .sort((a, b) => b[1] - a[1])
                    .slice(0, 3)
                    .map(([k, v]) => `${esc(k)}:${v}`)
                    .join(' · ');
                const hsRatio = op.hs_baseline_ratio;
                const hsHint = hsRatio !== null && hsRatio !== undefined
                    ? ` (${hsRatio}× baseline)` : '';
                const detailHtml = (op.handshake_fail_count > 0 || probes)
                    ? `<div class="dpi-op-detail">
                          ${probes ? `<div>🎯 probes: ${probes}</div>` : ''}
                          ${reasons ? `<div>📋 ${reasons}</div>` : ''}
                       </div>` : '';
                return `<div class="dpi-op">
                    <div>
                        <div class="dpi-op-name">${esc(op.as_org || '—')}</div>
                        <div class="dpi-op-asn">${esc(op.asn)}</div>
                    </div>
                    <div class="dpi-op-stat ${cls(op.short_ratio)}">${pct(op.short_ratio)} short</div>
                    <div class="dpi-op-stat">${op.conn_count} conn</div>
                    <div class="dpi-op-stat ${blCls(hsRatio)}" title="REALITY handshake failures${hsHint}">
                        ${op.handshake_fail_count} hsfail${hsHint}
                    </div>
                </div>${detailHtml}`;
            }).join('');
            return `<div class="dpi-row-country" data-cc="${esc(c.country)}">
                <div class="dpi-country-head">
                    <span class="dpi-flag">${flag}</span>
                    <div>
                        <div class="dpi-country-name">${esc(c.country)}</div>
                        <div class="dpi-country-stats">${c.conn_count} conn · ${c.handshake_fail_count} hsfail · ${c.operators.length} ASN</div>
                    </div>
                    <span class="dpi-country-stats ${cls(c.short_ratio)}">${pct(c.short_ratio)} short</span>
                    <div class="dpi-bar"><div class="dpi-bar-fill" style="width:${barW}%"></div></div>
                    <span class="dpi-arrow">▶</span>
                </div>
                <div class="dpi-operators">${opsHtml}</div>
            </div>`;
        }).join('');
        box.innerHTML = html;
        box.querySelectorAll('.dpi-country-head').forEach(head => {
            if (head.style.cursor === 'default') return;
            head.addEventListener('click', () => {
                head.closest('.dpi-row-country').classList.toggle('expanded');
            });
        });
    }

    // ==================== Protocol adoption widget ====================
    async function fetchProtoStats() {
        const box = document.getElementById('proto-stats');
        if (!box) return;
        const hours = document.getElementById('proto-hours')?.value || '24';
        let data;
        try {
            data = await apiFetch('/api/admin/protocol_stats?hours=' + hours);
        } catch (e) {
            box.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const v = data.vless || {};
        const h = data.hy2 || {};
        const trend = data.trend || [];

        // Bar trend: each day is one column with two stacked-side bars.
        const maxV = trend.reduce((m, d) => Math.max(m, d.vless || 0), 1);
        const maxH = trend.reduce((m, d) => Math.max(m, d.hy2 || 0), 1);
        const maxBoth = Math.max(maxV, maxH);
        const barsHtml = trend.map(d => {
            const hV = Math.max(1, Math.round(60 * (d.vless || 0) / maxBoth));
            const hH = Math.max(1, Math.round(60 * (d.hy2 || 0) / maxBoth));
            return `<div class="proto-trend-day" title="${esc(d.day)}: VLESS ${d.vless}, Hy2 ${d.hy2}">
                <div class="proto-trend-bar-v" style="height:${hV}px"></div>
                <div class="proto-trend-bar-h" style="height:${hH}px"></div>
            </div>`;
        }).join('');
        const labelsHtml = trend.map(d => {
            const day = (d.day || '').slice(5);  // MM-DD
            return `<span>${esc(day)}</span>`;
        }).join('');

        const countryRow = (c, i) => `
            <div class="proto-country-row">
                <span>${i + 1}. ${esc(c.country)}</span>
                <span>${c.conns}</span>
            </div>
        `;
        const vCountries = (v.top_countries || []).slice(0, 6).map(countryRow).join('') || '<div class="placeholder-text">—</div>';
        const hCountries = (h.top_countries || []).slice(0, 6).map(countryRow).join('') || '<div class="placeholder-text">—</div>';

        box.innerHTML = `
            <div class="proto-cards">
                <div class="proto-card vless">
                    <div class="proto-name">📡 VLESS Reality (TCP)</div>
                    <div class="proto-big">${v.total_conns || 0}</div>
                    <div class="proto-sub">конн. за ${hours}ч · ${v.distinct_buckets || 0} ASN</div>
                </div>
                <div class="proto-card hy2">
                    <div class="proto-name">🛟 Hysteria2 (UDP)</div>
                    <div class="proto-big">${h.allow_conns || 0}</div>
                    <div class="proto-sub">${h.unique_users || 0} уникальных юзеров · ${h.deny_conns || 0} deny</div>
                </div>
            </div>
            ${trend.length ? `<div class="proto-trend">
                <div class="proto-trend-title">Динамика по дням</div>
                <div class="proto-trend-bars">${barsHtml}</div>
                <div class="proto-trend-labels">${labelsHtml}</div>
            </div>` : ''}
            <div class="proto-countries-pair">
                <div class="proto-country-col">
                    <h4>📡 VLESS · топ стран</h4>
                    ${vCountries}
                </div>
                <div class="proto-country-col">
                    <h4>🛟 Hy2 · топ стран</h4>
                    ${hCountries}
                </div>
            </div>
        `;
    }

    function setupProtoStatsHandlers() {
        const reload = document.getElementById('proto-reload');
        if (reload) reload.addEventListener('click', fetchProtoStats);
        const hours = document.getElementById('proto-hours');
        if (hours) hours.addEventListener('change', fetchProtoStats);
    }

    function setupDpiHeatmapHandlers() {
        const reload = document.getElementById('dpi-reload');
        if (reload) reload.addEventListener('click', fetchDpiHeatmap);
        const hours = document.getElementById('dpi-hours');
        if (hours) hours.addEventListener('change', fetchDpiHeatmap);
    }

    // ==================== Alerts tab ====================
    function sevClass(sev) {
        return sev === 'critical' ? 'sev-critical' : 'sev-warn';
    }

    async function fetchAlerts() {
        const box = document.getElementById('alerts-list');
        const totalsBox = document.getElementById('alerts-totals');
        const badge = document.getElementById('alerts-badge');
        if (!box) return;
        const state = document.getElementById('alerts-state')?.value || 'active';
        const hours = document.getElementById('alerts-hours')?.value || '168';
        const key = document.getElementById('alerts-key-filter')?.value?.trim() || '';
        const qs = `?state=${state}&hours=${hours}` + (key ? `&key=${encodeURIComponent(key)}` : '');
        let data;
        try {
            data = await apiFetch('/api/admin/alerts' + qs);
        } catch (e) {
            box.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const t = data.totals || {};
        if (totalsBox) {
            totalsBox.innerHTML = `
                <span class="badge">всего ${t.total}</span>
                <span class="badge" style="color:#ff6b6b">активные ${t.active}</span>
                <span class="badge" style="color:#ffa94d">critical ${t.critical}</span>
            `;
        }
        if (badge) {
            const active = t.active || 0;
            badge.textContent = active;
            badge.classList.toggle('hidden', active === 0);
        }
        const alerts = data.alerts || [];
        if (!alerts.length) {
            box.innerHTML = '<div class="placeholder-text">Нет алертов в этом окне</div>';
            return;
        }
        box.innerHTML = alerts.map(a => {
            const acked = !!a.acked_at;
            const kimiHtml = a.kimi_analysis
                ? `<div class="alert-kimi"><div class="alert-kimi-head">🤖 Kimi</div>${a.kimi_analysis}</div>`
                : '';
            const actionsHtml = !acked
                ? `<div class="alert-actions"><button class="btn-ack" data-id="${a.id}">✅ Подтвердить</button></div>`
                : '';
            return `<div class="alert-row ${sevClass(a.severity)} ${acked ? 'acked' : ''}">
                <div class="alert-head">
                    <div class="alert-title">${esc(a.title)}</div>
                    <div class="alert-key">${esc(a.key)}</div>
                </div>
                ${a.detail ? `<div class="alert-detail">${esc(a.detail)}</div>` : ''}
                ${kimiHtml}
                <div class="alert-meta">
                    <span>${esc(a.fired_at)} · ${esc(a.severity)}</span>
                    ${acked ? `<span>acked ${esc(a.acked_at)} by ${esc(a.acked_by || '?')}</span>` : ''}
                </div>
                ${actionsHtml}
            </div>`;
        }).join('');
        box.querySelectorAll('.btn-ack').forEach(btn => {
            btn.addEventListener('click', async () => {
                const id = btn.dataset.id;
                btn.disabled = true;
                btn.textContent = '...';
                try {
                    await apiFetch(`/api/admin/alerts/${id}/ack`, {method: 'POST'});
                    fetchAlerts();
                } catch (e) {
                    btn.disabled = false;
                    btn.textContent = `⚠️ ${e.message}`;
                }
            });
        });
    }

    async function fetchReports() {
        const box = document.getElementById('reports-list');
        if (!box) return;
        const kind = document.getElementById('reports-kind')?.value || 'daily';
        const days = document.getElementById('reports-days')?.value || '90';
        let data;
        try {
            data = await apiFetch(`/api/admin/dpi_reports?kind=${kind}&days=${days}`);
        } catch (e) {
            box.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const reports = data.reports || [];
        if (!reports.length) {
            box.innerHTML = '<div class="placeholder-text">Нет отчётов в этом окне</div>';
            return;
        }
        box.innerHTML = reports.map((r, idx) => {
            const t = r.totals || {};
            const kimiHtml = r.kimi_analysis
                ? `<div class="alert-kimi"><div class="alert-kimi-head">🤖 Kimi</div>${r.kimi_analysis}</div>`
                : '';
            return `<div class="report-row" data-idx="${idx}">
                <div class="report-head">
                    <div class="report-date">${esc(r.period_start?.slice(0, 10) || '?')} → ${esc(r.period_end?.slice(0, 10) || '?')}</div>
                    <div class="report-totals">conn:${t.conn || 0} · short:${t.short || 0} · hsfail:${t.hsfail || 0}</div>
                </div>
                <div class="report-body">${kimiHtml || '<div class="placeholder-text">Kimi-анализ не сохранён</div>'}</div>
            </div>`;
        }).join('');
        box.querySelectorAll('.report-head').forEach(head => {
            head.addEventListener('click', () => {
                head.closest('.report-row').classList.toggle('expanded');
            });
        });
    }

    // Cheap "are there active alerts" probe — used to drive the tab badge.
    async function fetchAlertsBadge() {
        const badge = document.getElementById('alerts-badge');
        if (!badge) return;
        try {
            const data = await apiFetch('/api/admin/alerts?state=active&hours=168&limit=1');
            const active = data.totals?.active || 0;
            badge.textContent = active;
            badge.classList.toggle('hidden', active === 0);
        } catch (_e) { /* keep last value on transient failure */ }
    }

    function setupAlertsHandlers() {
        ['alerts-state', 'alerts-hours', 'alerts-key-filter'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', fetchAlerts);
        });
        const reload = document.getElementById('alerts-reload');
        if (reload) reload.addEventListener('click', fetchAlerts);
        ['reports-kind', 'reports-days'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', fetchReports);
        });
        const rReload = document.getElementById('reports-reload');
        if (rReload) rReload.addEventListener('click', fetchReports);
    }

    // ==================== Signals tab ====================

    async function fetchFailureReports() {
        const container = document.getElementById('signals-reports-list');
        if (!container) return;
        const state = document.getElementById('signals-reports-state')?.value || 'open';
        const hours = document.getElementById('signals-reports-hours')?.value || '168';
        let data;
        try {
            data = await apiFetch(`/api/admin/failure_reports?state=${state}&hours=${hours}&limit=200`);
        } catch (e) {
            container.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const rows = data.rows || [];
        const badge = document.getElementById('signals-badge');
        if (badge) {
            const openCount = rows.filter(r => !r.acked_at).length;
            badge.textContent = openCount;
            badge.classList.toggle('hidden', openCount === 0);
        }
        if (!rows.length) {
            container.innerHTML = '<div class="placeholder-text">Жалоб нет 🎉</div>';
            return;
        }
        container.innerHTML = rows.map(r => {
            const net = [r.country, r.asn].filter(Boolean).join(' / ') || '—';
            const traffic = r.last_traffic_ts || '—';
            const ackTxt = r.acked_at
                ? `<span class="alert-ack">✓ ${esc(r.acked_at)}${r.ack_note ? ' · ' + esc(r.ack_note) : ''}</span>`
                : `<button class="btn-ack" data-report-id="${r.id}">✓ ack</button>`;
            return `
                <div class="alert-row ${r.acked_at ? 'acked' : 'active'}">
                    <div class="alert-head">
                        <span class="alert-key">#${r.id} ${esc(r.ts)}</span>
                        ${ackTxt}
                    </div>
                    <div class="alert-title">
                        ${esc(r.username || r.chat_id)} (${esc(r.status || '?')}) — ${esc(net)}
                    </div>
                    <div class="alert-detail">Последний трафик: ${esc(traffic)}</div>
                </div>`;
        }).join('');
        container.querySelectorAll('.btn-ack').forEach(btn => {
            btn.addEventListener('click', async () => {
                const id = btn.dataset.reportId;
                btn.disabled = true;
                try {
                    await apiPost(`/api/admin/failure_reports/${id}/ack`, {});
                    fetchFailureReports();
                } catch (e) {
                    btn.disabled = false;
                    alert('Не удалось закрыть: ' + e.message);
                }
            });
        });
    }

    function heatCellClass(conn, fail) {
        const total = conn + fail;
        if (total === 0) return 'heat-none';
        const failRatio = fail / total;
        if (failRatio < 0.15) return 'heat-good';
        if (failRatio < 0.5) return 'heat-warn';
        return 'heat-bad';
    }

    async function fetchAsnHeatmap() {
        const container = document.getElementById('signals-heatmap');
        if (!container) return;
        const hours = document.getElementById('signals-heat-hours')?.value || '24';
        let data;
        try {
            data = await apiFetch(`/api/admin/asn_heatmap?hours=${hours}`);
        } catch (e) {
            container.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const rows = data.rows || [];
        if (!rows.length) {
            container.innerHTML = '<div class="placeholder-text">Нет данных за выбранный период.</div>';
            return;
        }
        // Union of inbound tags across all rows for stable column order.
        const tagSet = new Set();
        rows.forEach(r => Object.keys(r.protocols || {}).forEach(t => tagSet.add(t)));
        // Friendly ordering: stealth & CDN first, direct-to-entry after.
        const tagOrder = ['inbound-8444', 'inbound-2053', 'inbound-2054', 'hy2-8400', 'inbound-8443'];
        const orderedTags = [
            ...tagOrder.filter(t => tagSet.has(t)),
            ...[...tagSet].filter(t => !tagOrder.includes(t)).sort(),
        ];
        const tagLabel = {
            'inbound-8444': 'STLS',
            'inbound-2053': 'WS',
            'inbound-2054': 'XHTTP',
            'hy2-8400':     'HY2',
            'inbound-8443': 'REAL',
        };
        const head = ['<th>Страна</th><th>ASN / Оператор</th><th>👥</th><th>↻ sub</th><th>🆘</th>']
            .concat(orderedTags.map(t => `<th>${esc(tagLabel[t] || t)}</th>`))
            .join('');
        const body = rows.map(r => {
            const cells = orderedTags.map(t => {
                const p = r.protocols[t];
                if (!p) return '<td><span class="heat-cell heat-none">—</span></td>';
                const cls = heatCellClass(p.conn, p.fail);
                const txt = `${p.conn}/${p.fail}`;
                const title = `conn=${p.conn} fail=${p.fail} short=${p.short}`;
                return `<td><span class="heat-cell ${cls}" title="${esc(title)}">${esc(txt)}</span></td>`;
            }).join('');
            const reportsCls = r.failure_reports > 0 ? 'heat-bad' : 'heat-none';
            return `
                <tr>
                    <td>${esc(r.country || '—')}</td>
                    <td><div class="asn-cell"><b>${esc(r.asn || '—')}</b><span class="asn-org">${esc(r.as_org || '')}</span></div></td>
                    <td>${r.active_users || 0}</td>
                    <td>${r.sub_fetches || 0}</td>
                    <td><span class="heat-cell ${reportsCls}">${r.failure_reports || 0}</span></td>
                    ${cells}
                </tr>`;
        }).join('');
        container.innerHTML = `<table class="heatmap-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    }

    function setupSignalsHandlers() {
        ['signals-reports-state', 'signals-reports-hours'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', fetchFailureReports);
        });
        const rReload = document.getElementById('signals-reports-reload');
        if (rReload) rReload.addEventListener('click', fetchFailureReports);
        const hSel = document.getElementById('signals-heat-hours');
        if (hSel) hSel.addEventListener('change', fetchAsnHeatmap);
        const hReload = document.getElementById('signals-heat-reload');
        if (hReload) hReload.addEventListener('click', fetchAsnHeatmap);
        ['signals-map-hours'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', fetchGeoPoints);
        });
        ['signals-map-reports', 'signals-map-fetches', 'signals-map-users',
         'signals-map-heat', 'signals-map-cluster'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.addEventListener('change', renderGeoPoints);
        });
        const mReload = document.getElementById('signals-map-reload');
        if (mReload) mReload.addEventListener('click', fetchGeoPoints);
    }

    // ==================== Signals — map ====================

    let _leafletMap = null;
    let _mapLayers = { reports: null, fetches: null, users: null, heat: null };
    let _lastGeoData = null;

    // Build a marker container honoring the cluster toggle.
    // markercluster groups markers within ~80px on screen, so a region
    // with 20+ pins reads as one labeled circle; zooming in expands
    // the bundle. Falls back to a plain layerGroup when the plugin
    // isn't loaded (offline or CDN miss) so the map still works.
    function makeMarkerContainer() {
        const cluster = document.getElementById('signals-map-cluster')?.checked !== false;
        if (cluster && window.L?.markerClusterGroup) {
            return L.markerClusterGroup({
                showCoverageOnHover: false,
                maxClusterRadius: 60,
                disableClusteringAtZoom: 8,
            });
        }
        return L.layerGroup();
    }

    function ensureLeafletMap() {
        if (_leafletMap) {
            // Existing instance — keep but re-sync size in case the
            // container was hidden when we created it.
            setTimeout(() => _leafletMap.invalidateSize(), 0);
            return _leafletMap;
        }
        const container = document.getElementById('signals-map');
        if (!container || !window.L) return null;
        container.innerHTML = '';
        // Wide Russia + CIS default view: centered around the Urals
        // at zoom 4. Operator can pan/zoom freely from there.
        _leafletMap = L.map(container, { zoomControl: true })
            .setView([56.0, 60.0], 4);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            maxZoom: 18,
            attribution: '© OpenStreetMap',
        }).addTo(_leafletMap);
        return _leafletMap;
    }

    async function fetchGeoPoints() {
        const map = ensureLeafletMap();
        if (!map) return;
        const hours = document.getElementById('signals-map-hours')?.value || '168';
        let data;
        try {
            data = await apiFetch(`/api/admin/geo_points?hours=${hours}`);
        } catch (e) {
            console.warn('geo_points fetch failed:', e);
            return;
        }
        _lastGeoData = data;
        renderGeoPoints();
    }

    function renderGeoPoints() {
        const map = _leafletMap;
        if (!map || !_lastGeoData) return;
        // Clear previous layer groups including the heat layer.
        ['reports', 'fetches', 'users', 'heat'].forEach(k => {
            if (_mapLayers[k]) {
                map.removeLayer(_mapLayers[k]);
                _mapLayers[k] = null;
            }
        });
        const show = {
            reports: document.getElementById('signals-map-reports')?.checked !== false,
            fetches: document.getElementById('signals-map-fetches')?.checked !== false,
            users:   document.getElementById('signals-map-users')?.checked === true,
            heat:    document.getElementById('signals-map-heat')?.checked === true,
        };
        const reports = _lastGeoData.reports || [];
        const fetches = _lastGeoData.fetches || [];
        const users = _lastGeoData.users || [];

        // Heatmap overlay — density of failure reports (weight by
        // 1 per open report, 0.25 per acked report so the acks fade).
        // Sits under the pins so the operator can still click them.
        if (show.heat && reports.length && window.L?.heatLayer) {
            const pts = reports.map(r => [r.lat, r.lon, r.acked ? 0.25 : 1.0]);
            _mapLayers.heat = L.heatLayer(pts, {
                radius: 28,
                blur: 22,
                maxZoom: 7,
                gradient: { 0.2: '#3b82f6', 0.5: '#fde047', 0.8: '#ef4444' },
            }).addTo(map);
        }

        // Reports — red pins, popup with context. Clustered by default.
        if (show.reports && reports.length) {
            const group = makeMarkerContainer();
            reports.forEach(r => {
                const m = L.circleMarker([r.lat, r.lon], {
                    radius: 7,
                    color: r.acked ? '#888' : '#c83a3a',
                    fillColor: r.acked ? '#888' : '#ff5a5a',
                    fillOpacity: 0.85,
                    weight: 2,
                });
                const who = esc(r.username || r.chat_id || '?');
                const net = [r.country, r.asn, r.city].filter(Boolean).map(esc).join(' / ');
                m.bindPopup(`
                    <b>🆘 Report #${r.id}</b><br>
                    ${esc(r.ts)}<br>
                    <code>@${who}</code><br>
                    ${net}<br>
                    ${r.acked ? '<span style="color:#888">✓ закрыта</span>' : '<span style="color:#c83a3a">открыта</span>'}
                `);
                group.addLayer(m);
            });
            group.addTo(map);
            _mapLayers.reports = group;
        }

        // Fetches — green bubbles sized by count. Bubbles are already
        // pre-aggregated server-side (~50km grid), so we don't cluster
        // them — that would obscure the size-encodes-volume signal.
        if (show.fetches && fetches.length) {
            const maxN = fetches.reduce((s, f) => Math.max(s, f.count), 1);
            const group = L.layerGroup();
            fetches.forEach(f => {
                const r = 5 + Math.sqrt(f.count / maxN) * 18;
                const m = L.circleMarker([f.lat, f.lon], {
                    radius: r,
                    color: '#1b8f4a',
                    fillColor: '#28a060',
                    fillOpacity: 0.35,
                    weight: 1,
                });
                const where = [f.country, f.asn, f.city].filter(Boolean).map(esc).join(' / ');
                m.bindPopup(`
                    <b>↻ ${f.count} sub fetches</b><br>
                    ${where}
                `);
                group.addLayer(m);
            });
            group.addTo(map);
            _mapLayers.fetches = group;
        }

        // Active users — small blue dots, off by default. Clustering
        // them helps when many share a city — the cluster bubble
        // shows the count and zooming expands it.
        if (show.users && users.length) {
            const group = makeMarkerContainer();
            users.forEach(u => {
                const m = L.circleMarker([u.lat, u.lon], {
                    radius: 3,
                    color: '#54a0ff',
                    fillColor: '#54a0ff',
                    fillOpacity: 0.7,
                    weight: 1,
                });
                const where = [u.country, u.asn, u.city].filter(Boolean).map(esc).join(' / ');
                m.bindPopup(`
                    <b>👤 ${esc(u.username || u.chat_id || '?')}</b> (${esc(u.status || '?')})<br>
                    ${where}
                `);
                group.addLayer(m);
            });
            group.addTo(map);
            _mapLayers.users = group;
        }

        // After loading data first time, fit the map to known points
        // (reports take priority, then fetches). Only on the very
        // first render to avoid yanking the operator's view around.
        if (!map._fittedOnce) {
            const all = [];
            reports.forEach(r => all.push([r.lat, r.lon]));
            fetches.forEach(f => all.push([f.lat, f.lon]));
            if (all.length) {
                map.fitBounds(all, { padding: [40, 40], maxZoom: 7 });
                map._fittedOnce = true;
            }
        }
    }

    // Cheap probe for the Signals tab badge — counts open failure
    // reports last 7 days.
    async function fetchSignalsBadge() {
        const badge = document.getElementById('signals-badge');
        if (!badge) return;
        try {
            const data = await apiFetch('/api/admin/failure_reports?state=open&hours=168&limit=1');
            const open = (data.rows || []).length;
            badge.textContent = open;
            badge.classList.toggle('hidden', open === 0);
        } catch (_e) { /* noop */ }
    }

    function setupRemindersHandlers() {
        const save = document.getElementById('reminders-save');
        if (save) save.addEventListener('click', saveReminders);
        const reload = document.getElementById('reminders-reload');
        if (reload) reload.addEventListener('click', fetchReminders);
        const sendCohort = document.getElementById('manual-cohort-send');
        if (sendCohort) sendCohort.addEventListener('click', manualSendCohort);
        const sendUser = document.getElementById('manual-user-send');
        if (sendUser) sendUser.addEventListener('click', manualSendUser);
    }

    async function fetchLogs() {
        const container = document.getElementById('logs-list');
        if (!container) return;
        const level = document.getElementById('logs-level-filter')?.value || '';
        const qs = level ? `?limit=200&level=${encodeURIComponent(level)}` : '?limit=200';
        let data;
        try {
            data = await apiFetch('/api/admin/logs' + qs);
        } catch (e) {
            container.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const lines = data.lines || [];
        if (!lines.length) {
            container.innerHTML = '<div class="placeholder-text">Лог пуст (или файл ещё не создан)</div>';
            return;
        }
        container.innerHTML = lines.map(ln => {
            const cls = ln.includes(' - ERROR - ')   ? 'log-error'
                      : ln.includes(' - WARNING - ') ? 'log-warn'
                      : ln.includes(' - DEBUG - ')   ? 'log-debug'
                      : '';
            return `<div class="log-line ${cls}">${esc(ln)}</div>`;
        }).join('');
        // Auto-scroll to bottom for tail-like UX
        container.scrollTop = container.scrollHeight;
    }

    // ==================== X-UI clients sync ====================
    let xuiCache = null;

    async function fetchXuiClients() {
        const summary = document.getElementById('xui-summary');
        const list = document.getElementById('xui-list');
        if (!summary || !list) return;

        let data;
        try {
            data = await apiFetch('/api/admin/xui_clients');
        } catch (e) {
            list.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        xuiCache = data;
        renderXuiSummary(data);
        renderXuiBucket();
    }

    function renderXuiSummary(d) {
        const summary = document.getElementById('xui-summary');
        summary.innerHTML = `
            <span class="sum-pill ok">в x-ui: ${d.inbound_total}</span>
            <span class="sum-pill ok">синхр: ${d.synced_count}</span>
            <span class="sum-pill ${d.orphan_in_xui_count ? 'warn' : ''}">x-ui→null: ${d.orphan_in_xui_count}</span>
            <span class="sum-pill ${d.orphan_in_bot_count ? 'bad' : ''}">бот→null: ${d.orphan_in_bot_count}</span>
        `;
    }

    function renderXuiBucket() {
        if (!xuiCache) return;
        const list = document.getElementById('xui-list');
        const bucket = document.getElementById('xui-bucket').value;
        const rows = xuiCache[bucket] || [];
        if (!rows.length) {
            list.innerHTML = '<div class="placeholder-text">Пусто</div>';
            return;
        }
        const mb = b => (b / (1024 * 1024)).toFixed(1);
        const gb = b => b ? (b / (1024 * 1024 * 1024)).toFixed(2) : '—';

        list.innerHTML = rows.map(r => {
            // synced + orphan_in_xui share the same x-ui-side shape
            if (bucket !== 'orphan_in_bot') {
                const t = r.traffic || {};
                const flag = r.enable ? '' : '<span class="xui-disabled">disabled</span>';
                const mismatch = r.bot_user && !r.bot_user.uuid_match
                    ? '<span class="xui-warn">⚠ UUID mismatch</span>' : '';
                const bu = r.bot_user;
                return `
                    <div class="xui-row" ${bu ? `onclick="window.__openDetail('${esc(bu.chat_id)}')"` : ''}>
                        <div class="xui-row-head">
                            <span class="xui-email">${esc(r.email || 'no_email')}</span>
                            ${bu ? `<span class="xui-bot">@${esc(bu.username || '')} · ${esc(bu.status || '')}</span>` : '<span class="xui-bot xui-orphan">не в бот-БД</span>'}
                        </div>
                        <div class="xui-row-meta">
                            <code class="xui-uuid">${esc((r.uuid || '').slice(0, 8))}…</code>
                            <span>${esc(r.flow || '')}</span>
                            ${flag}${mismatch}
                            <span>↑${mb(t.up)} ↓${mb(t.down)} MB / ${gb(r.total_gb_bytes)} GB</span>
                        </div>
                    </div>`;
            } else {
                return `
                    <div class="xui-row" onclick="window.__openDetail('${esc(r.chat_id)}')">
                        <div class="xui-row-head">
                            <span class="xui-email">${esc(r.email || '')}</span>
                            <span class="xui-bot">@${esc(r.username || '')} · ${esc(r.status || '')}</span>
                        </div>
                        <div class="xui-row-meta">
                            <code class="xui-uuid">${esc((r.uuid || '').slice(0, 8))}…</code>
                            <span class="xui-warn">⚠ нет в x-ui inbound</span>
                        </div>
                    </div>`;
            }
        }).join('');
    }

    document.addEventListener('change', e => {
        if (e.target && e.target.id === 'xui-bucket') renderXuiBucket();
        if (e.target && e.target.id === 'subs-bucket') renderSubsBucket();
    });

    // ==================== Subscriptions ====================
    let subsCache = null;

    async function fetchSubscriptions() {
        const summary = document.getElementById('subs-summary');
        const list = document.getElementById('subs-list');
        if (!summary || !list) return;

        let data;
        try {
            data = await apiFetch('/api/admin/subscriptions');
        } catch (e) {
            list.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        subsCache = data;
        const t = data.totals || {};
        summary.innerHTML = `
            <span class="sum-pill ok">активные: ${t.active || 0}</span>
            <span class="sum-pill ${t.expiring_in_7d ? 'warn' : ''}">истекают &lt; 7д: ${t.expiring_in_7d || 0}</span>
            <span class="sum-pill ${t.expired ? 'bad' : ''}">истёкшие: ${t.expired || 0}</span>
            <span class="sum-pill ${t.no_subscription ? 'warn' : ''}">без записи: ${t.no_subscription || 0}</span>
        `;
        renderSubsBucket();
    }

    function renderSubsBucket() {
        if (!subsCache) return;
        const list = document.getElementById('subs-list');
        const bucket = document.getElementById('subs-bucket').value;
        const rows = subsCache[bucket] || [];
        if (!rows.length) {
            list.innerHTML = '<div class="placeholder-text">Пусто</div>';
            return;
        }
        list.innerHTML = rows.map(r => {
            const isNoSub = bucket === 'no_subscription';
            const end = isNoSub
                ? (r.subscription_expiry || '—')
                : (r.end || '—');
            const plan = isNoSub ? '—' : (r.plan || 'demo');
            return `
                <div class="subs-row" onclick="window.__openDetail('${esc(r.chat_id)}')">
                    <div class="subs-row-head">
                        <span class="subs-user">@${esc(r.username || 'no_username')}</span>
                        <span class="subs-status badge-${esc(r.user_status || r.status || '')}">${esc(r.user_status || r.status || '')}</span>
                    </div>
                    <div class="subs-row-meta">
                        <span class="subs-plan">${esc(plan)}</span>
                        <span class="subs-end">до ${esc(end)}</span>
                        ${!isNoSub && r.start ? `<span class="subs-start">с ${esc(r.start)}</span>` : ''}
                    </div>
                </div>`;
        }).join('');
    }

    async function fetchAudit() {
        const container = document.getElementById('audit-list');
        if (!container) return;
        let data;
        try {
            data = await apiFetch('/api/admin/audit?limit=50');
        } catch (e) {
            container.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const entries = data.entries || [];
        if (!entries.length) {
            container.innerHTML = '<div class="placeholder-text">История пуста</div>';
            return;
        }
        container.innerHTML = entries.map(e => `
            <div class="audit-row">
                <span class="audit-when">${esc(e.at || '')}</span>
                <span class="audit-action">${esc(e.action || '')}</span>
                ${e.target_id
                    ? `<span class="audit-target" onclick="window.__openDetail('${esc(e.target_id)}')">→ ${esc(e.target_id)}</span>`
                    : ''}
                <span class="audit-by">admin ${esc(e.admin_id || '')}</span>
            </div>
        `).join('');
    }

    async function fetchSystem() {
        const container = document.getElementById('system-info');
        if (!container) return;
        let data;
        try {
            data = await apiFetch('/api/admin/system_info');
        } catch (e) {
            container.innerHTML = `<div class="placeholder-text">⚠️ ${esc(e.message)}</div>`;
            return;
        }
        const upMin = Math.max(0, Math.round((data.bot_uptime_sec || 0) / 60));
        const upH = Math.floor(upMin / 60), upM = upMin % 60;
        const uptimeStr = upMin < 0 ? '—' : (upH ? `${upH}ч ${upM}м` : `${upM}м`);
        const sec = data.secret_status || {};
        const secretRow = (label, ok, warn) => `
            <div class="sysinfo-row sysinfo-secret">
                <span class="k">${esc(label)}</span>
                <span class="v ${warn ? 'sysinfo-warn' : (ok ? 'sysinfo-ok' : 'sysinfo-bad')}">
                    ${ok ? (warn ? '⚠ default' : '✓ set') : '✗ missing'}
                </span>
            </div>`;
        const settings = data.settings || {};
        const keysRender = Object.keys(settings).map(k => `
            <div class="sysinfo-row">
                <span class="k">${esc(k)}</span>
                <span class="v"><code>${esc(String(settings[k] ?? ''))}</code></span>
            </div>`).join('');

        container.innerHTML = `
            <div class="sysinfo-row">
                <span class="k">Git SHA</span>
                <span class="v"><code>${esc(data.git_sha || 'n/a')}</code></span>
            </div>
            <div class="sysinfo-row">
                <span class="k">Bot uptime</span>
                <span class="v">${uptimeStr}</span>
            </div>
            ${secretRow('BOT_TOKEN', sec.BOT_TOKEN, false)}
            ${secretRow('REALITY_PUBLIC_KEY', sec.REALITY_PUBLIC_KEY, false)}
            ${secretRow('OPENCODE_SERVER_PASSWORD', sec.OPENCODE_SERVER_PASSWORD, false)}
            ${secretRow('XUI_PASSWORD', !sec.XUI_PASSWORD_default_admin, sec.XUI_PASSWORD_default_admin)}
            <div class="sysinfo-divider"></div>
            ${keysRender}
        `;
    }

    function renderRegistrations(reg) {
        document.getElementById('stat-today').textContent = reg.today ?? '—';
        document.getElementById('stat-week').textContent = reg.this_week ?? '—';
        document.getElementById('stat-month').textContent = reg.this_month ?? '—';
    }

    function renderStatusBars(userStats) {
        const byStatus = userStats.by_status || {};
        const total = userStats.total || 1;

        const container = document.getElementById('status-bars');
        const statusColors = {
            demo: '#3ddc84', paid: '#3ddc84', new: '#54a0ff',
            pending_demo: '#ff9f43', rejected: '#f44060',
            banned: '#f44060', support_topic: '#ff9f43',
            platform_select: '#54a0ff',
        };

        const rows = Object.entries(byStatus)
            .sort((a, b) => b[1] - a[1])
            .map(([status, count]) => {
                const pct = Math.round((count / total) * 100);
                const color = statusColors[status] || '#7c6ef0';
                return `
                    <div class="bar-row">
                        <span class="bar-label">${esc(status)}</span>
                        <div class="bar-track">
                            <div class="bar-fill" style="width:${pct}%; background:${color}"></div>
                        </div>
                        <span class="bar-count">${count}</span>
                    </div>`;
            }).join('');

        container.innerHTML = rows || '<div class="placeholder-text">Нет данных</div>';

        // Platforms
        const byPlatform = userStats.by_platform || {};
        const pContainer = document.getElementById('platform-bars');
        const pRows = Object.entries(byPlatform)
            .sort((a, b) => b[1] - a[1])
            .map(([platform, count]) => {
                const pct = Math.round((count / total) * 100);
                return `
                    <div class="bar-row">
                        <span class="bar-label">${esc(platform)}</span>
                        <div class="bar-track">
                            <div class="bar-fill" style="width:${pct}%"></div>
                        </div>
                        <span class="bar-count">${count}</span>
                    </div>`;
            }).join('');

        pContainer.innerHTML = pRows || '<div class="placeholder-text">Нет данных</div>';
    }

    async function fetchAndRenderOnline() {
        // Best-effort: paint an "X online now" pill next to the >90%
        // quota warning in the Stats tab. Same data feeds the per-row
        // 🟢 badge in the Users tab; fetching twice is cheap and lets
        // each tab fail in isolation.
        const el = document.getElementById('stat-online-count');
        if (!el) return;
        try {
            const data = await apiFetch('/api/admin/online_clients');
            state.onlineByEmail = data.by_email || {};
            el.textContent = data.count ?? '—';
        } catch (e) {
            el.textContent = '—';
        }
    }

    function renderTraffic(traffic) {
        const totalGB = (traffic.total_consumed_bytes || 0) / (1024 ** 3);
        const avgGB = (traffic.avg_per_user_bytes || 0) / (1024 ** 3);

        document.getElementById('stat-traffic-total').textContent = totalGB.toFixed(1) + ' GB';
        document.getElementById('stat-traffic-avg').textContent = avgGB.toFixed(1) + ' GB';
        document.getElementById('stat-traffic-warn').textContent = traffic.users_above_90_percent ?? 0;
    }

    // ==================== Modal ====================
    let modalCallback = null;

    function setupModal() {
        document.getElementById('modal-cancel').addEventListener('click', hideModal);
        document.getElementById('modal-confirm').addEventListener('click', () => {
            // Grab the callback BEFORE hideModal — it nulls modalCallback,
            // which silently killed every confirm-gated action.
            const cb = modalCallback;
            hideModal();
            if (cb) cb();
        });
        document.getElementById('modal-overlay').addEventListener('click', e => {
            if (e.target === e.currentTarget) hideModal();
        });
    }

    function showModal(title, bodyHtml, onConfirm) {
        document.getElementById('modal-title').textContent = title;
        document.getElementById('modal-body').innerHTML = bodyHtml;
        modalCallback = onConfirm;
        document.getElementById('modal-overlay').classList.remove('hidden');
    }

    function hideModal() {
        document.getElementById('modal-overlay').classList.add('hidden');
        modalCallback = null;
    }

    // ==================== Toast ====================
    function showToast(msg) {
        const el = document.getElementById('toast');
        el.textContent = msg;
        el.classList.add('visible');
        setTimeout(() => el.classList.remove('visible'), 2500);
    }

    // ==================== Helpers ====================
    function esc(str) {
        if (str == null) return '';
        const div = document.createElement('div');
        div.textContent = String(str);
        return div.innerHTML;
    }

})();
