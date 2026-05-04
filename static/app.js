// HTTP status code explanations for common/proxy-relevant codes
const HTTP_STATUS_EXPLANATIONS = {
    400: 'Bad Request — the request was malformed',
    401: 'Unauthorized — invalid or missing API key',
    403: 'Forbidden — access denied',
    404: 'Not Found — the requested resource is not available',
    408: 'Request Timeout — the server timed out waiting for the request',
    413: 'Payload Too Large — request exceeds the maximum allowed size',
    429: 'Rate Limited — too many requests, please slow down',
    500: 'Internal Server Error — the upstream server encountered an error',
    502: 'Bad Gateway — the upstream server returned an invalid response',
    503: 'Service Unavailable — the service is temporarily unavailable',
    504: 'Gateway Timeout — the upstream server timed out',
    520: 'Cloudflare 520 — origin server returned an empty or unknown response',
    521: 'Cloudflare 521 — the origin server is down or refused the connection',
    522: 'Cloudflare 522 — connection to the origin server timed out',
    523: 'Cloudflare 523 — the origin server is unreachable',
    524: 'Cloudflare 524 — a connection timeout occurred (origin too slow)',
    525: 'Cloudflare 525 — SSL handshake failed between Cloudflare and the origin',
    526: 'Cloudflare 526 — invalid SSL certificate on the origin server',
    530: 'Cloudflare 530 — origin DNS error (domain does not resolve)',
};

function getErrorDetails(errorStr) {
    if (!errorStr) return { short: 'Unknown error', explanation: '' };
    const match = errorStr.match(/HTTP\s+(\d{3})/i);
    if (match) {
        const code = parseInt(match[1]);
        const explanation = HTTP_STATUS_EXPLANATIONS[code];
        return { short: errorStr, explanation: explanation || `HTTP status code ${code}` };
    }
    return { short: errorStr, explanation: '' };
}

function formatNumber(num) {
    return num ? num.toLocaleString() : '0';
}

function formatTime() {
    return new Date().toLocaleTimeString();
}

function formatDateTime(isoString) {
    if (!isoString) return '-';
    return new Date(isoString).toLocaleString();
}

function todayStr() {
    return new Date().toLocaleDateString('en-CA');
}

function daysAgoStr(n) {
    const d = new Date();
    d.setDate(d.getDate() - n);
    return d.toLocaleDateString('en-CA');
}

// Global filter state
let filterFrom = todayStr();
let filterTo = todayStr();

// Pagination state
let currentPage = 1;
const perPage = 20;
let totalPages = 1;

// Config state
let configData = null;
let availableModels = [];

async function fetchStats(from, to) {
    try {
        let url = '/api/stats?';
        if (from) url += `from_date=${from}&`;
        if (to) url += `to_date=${to}`;
        const resp = await fetch(url);
        return await resp.json();
    } catch (e) {
        console.error('Failed to fetch stats:', e);
        return null;
    }
}

async function fetchHistory(from, to, page = 1) {
    try {
        const offset = (page - 1) * perPage;
        let url = `/api/history?limit=${perPage}&offset=${offset}`;
        if (from) url += `&from_date=${from}`;
        if (to) url += `&to_date=${to}`;
        const resp = await fetch(url);
        return await resp.json();
    } catch (e) {
        console.error('Failed to fetch history:', e);
        return null;
    }
}

async function fetchConfig() {
    try {
        const resp = await fetch('/api/config');
        configData = await resp.json();
        return configData;
    } catch (e) {
        console.error('Failed to fetch config:', e);
        return null;
    }
}

async function fetchQuotas() {
    try {
        const resp = await fetch('/api/quotas');
        return await resp.json();
    } catch (e) {
        console.error('Failed to fetch quotas:', e);
        return null;
    }
}

function formatResetTime(seconds) {
    if (seconds <= 0) return 'now';
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function renderQuotas(data) {
    const container = document.getElementById('quota-workspaces');
    const statusMsg = document.getElementById('quota-status-message');

    if (!data || Object.keys(data).length === 0) {
        statusMsg.textContent = 'Quota tracking not configured. Add workspace credentials in Configuration -> API Keys.';
        container.innerHTML = '';
        return;
    }

    statusMsg.textContent = '';
    const entries = Object.entries(data);
    const showHeaders = entries.length > 1;

    let allHtml = '';
    for (const [wsId, wsData] of entries) {
        const status = wsData.status || 'error';
        const error = wsData.error || '';
        const quotas = wsData.quotas || {};
        const fetchedAt = wsData.fetched_at || null;

        if (status === 'error' && !quotas.rolling && !quotas.weekly && !quotas.monthly) {
            allHtml += `<div class="quota-workspace">
                <div class="quota-error">Workspace ${wsId.slice(0, 8)}...: Error — ${error}</div>
            </div>`;
            continue;
        }

        let wsHtml = '';
        if (showHeaders) {
            wsHtml += `<h3 class="quota-workspace-title">Workspace: ${wsId}</h3>`;
        }

        // Per-workspace status message
        if (status === 'error') {
            wsHtml += `<p class="config-hint" style="color:var(--danger)">Last fetch error: ${error}. Showing cached data.</p>`;
        }

        wsHtml += '<div class="quota-grid">';
        for (const period of ['rolling', 'weekly', 'monthly']) {
            const q = quotas[period] || { usage_percent: 0, reset_in_sec: 0 };
            const pct = Math.min(100, Math.max(0, q.usage_percent || 0));
            const remaining = (100 - pct).toFixed(1);

            const barClass = pct >= 85 ? 'quota-bar-danger' : pct >= 60 ? 'quota-bar-warning' : 'quota-bar-ok';
            const label = period === 'rolling' ? '5-Hour Rolling' : period.charAt(0).toUpperCase() + period.slice(1);
            const resetSec = q.reset_in_sec || 0;
            const resetText = resetSec > 0 ? 'Resets in ' + formatResetTime(resetSec) : '';
            const resetTarget = resetSec > 0 ? Math.floor(Date.now() / 1000) + resetSec : '';

            wsHtml += `<div class="quota-item">
                <div class="quota-header">
                    <span class="quota-label">${label}</span>
                    <span class="quota-reset" data-reset-target="${resetTarget}">${resetText}</span>
                </div>
                <div class="quota-bar-container">
                    <div class="quota-bar ${barClass}" style="width:${pct}%"></div>
                </div>
                <div class="quota-stats">
                    <span class="quota-usage">${pct.toFixed(1)}%</span>
                    <span class="quota-remaining">${remaining}% remaining</span>
                </div>
            </div>`;
        }
        wsHtml += '</div>';

        if (fetchedAt) {
            wsHtml += `<div class="quota-footer"><span class="quota-fetched-at">Last updated: ${formatDateTime(fetchedAt)}</span></div>`;
        }

        allHtml += `<div class="quota-workspace">${wsHtml}</div>`;
    }

    container.innerHTML = allHtml;
}

function renderStats(data) {
    if (!data) return;

    const t = data.totals;
    document.getElementById('total-input').textContent = formatNumber(t.input);
    document.getElementById('total-output').textContent = formatNumber(t.output);
    document.getElementById('total-cache').textContent = formatNumber(t.cache);
    document.getElementById('total-all').textContent = formatNumber(t.total);
    document.getElementById('total-success').textContent = formatNumber(t.success_count);
    document.getElementById('total-fail').textContent = formatNumber(t.fail_count);
    document.getElementById('avg-duration').textContent = t.avg_duration_ms ? formatNumber(t.avg_duration_ms) : '-';
    document.getElementById('total-requests').textContent = formatNumber(t.count);

    const tbody = document.getElementById('model-tbody');
    const models = data.models;

    if (Object.keys(models).length === 0) {
        tbody.innerHTML = '<tr><td colspan="9">No data</td></tr>';
        return;
    }

    let html = '';
    for (const [model, s] of Object.entries(models)) {
        html += `<tr>
            <td>${model}</td>
            <td>${formatNumber(s.input)}</td>
            <td>${formatNumber(s.output)}</td>
            <td>${formatNumber(s.cache)}</td>
            <td>${formatNumber(s.total)}</td>
            <td>${s.pct}</td>
            <td>${formatNumber(s.success_count)}</td>
            <td>${formatNumber(s.fail_count)}</td>
            <td>${s.avg_duration_ms ? formatNumber(s.avg_duration_ms) : '-'}</td>
        </tr>`;
    }
    tbody.innerHTML = html;
}

let chartTokens = null;
let chartModelTokens = null;
let chartModelRequests = null;

const CHART_COLORS = ['#4fc3f7', '#ff8a65', '#81c784', '#ba68c8', '#ffd54f', '#f06292', '#4dd0e1', '#aed581'];

function makeChartOpts(textColor) {
    return { responsive: true, plugins: { legend: { labels: { color: textColor } } } };
}

function renderCharts(data) {
    if (!data) return;
    const isDark = document.documentElement.getAttribute('data-theme') !== 'light';
    const textColor = isDark ? '#e0e0e0' : '#333';

    const t = data.totals;
    const tokenData = [t.input, t.output, t.cache];

    // Token distribution
    if (chartTokens) {
        chartTokens.data.datasets[0].data = tokenData;
        chartTokens.options.plugins.legend.labels.color = textColor;
        chartTokens.update('none');
    } else {
        chartTokens = new Chart(document.getElementById('chart-tokens'), {
            type: 'doughnut',
            data: {
                labels: ['Input', 'Output', 'Cache'],
                datasets: [{ data: tokenData, backgroundColor: ['#4fc3f7', '#ff8a65', '#81c784'], borderWidth: 0 }]
            },
            options: makeChartOpts(textColor)
        });
    }

    // Per model
    const models = Object.entries(data.models);
    const modelLabels = models.map(([m]) => m);
    const modelTokenData = models.map(([, s]) => s.total);
    const modelRequestData = models.map(([, s]) => s.count);
    const colors = CHART_COLORS.slice(0, modelLabels.length);

    if (chartModelTokens) {
        chartModelTokens.data.labels = modelLabels;
        chartModelTokens.data.datasets[0].data = modelTokenData;
        chartModelTokens.data.datasets[0].backgroundColor = colors;
        chartModelTokens.options.plugins.legend.labels.color = textColor;
        chartModelTokens.update('none');
    } else {
        chartModelTokens = new Chart(document.getElementById('chart-model-tokens'), {
            type: 'doughnut',
            data: {
                labels: modelLabels,
                datasets: [{ data: modelTokenData, backgroundColor: colors, borderWidth: 0 }]
            },
            options: makeChartOpts(textColor)
        });
    }

    if (chartModelRequests) {
        chartModelRequests.data.labels = modelLabels;
        chartModelRequests.data.datasets[0].data = modelRequestData;
        chartModelRequests.data.datasets[0].backgroundColor = colors;
        chartModelRequests.options.plugins.legend.labels.color = textColor;
        chartModelRequests.update('none');
    } else {
        chartModelRequests = new Chart(document.getElementById('chart-model-requests'), {
            type: 'doughnut',
            data: {
                labels: modelLabels,
                datasets: [{ data: modelRequestData, backgroundColor: colors, borderWidth: 0 }]
            },
            options: makeChartOpts(textColor)
        });
    }
}

function renderHistory(data) {
    const tbody = document.getElementById('history-tbody');
    if (!data || data.logs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="10">No history</td></tr>';
        updatePagination(1, 1);
        return;
    }

    let html = '';
    for (const log of data.logs) {
        const duration = log.duration_ms ? formatNumber(log.duration_ms) : '-';
        let status;
        if (log.success) {
            status = '<span class="status-ok">&#10004;</span>';
        } else {
            const errDetail = getErrorDetails(log.error || '');
            const escError = (log.error || '').replace(/"/g, '&quot;').replace(/</g, '&lt;');
            const escExplanation = errDetail.explanation ? errDetail.explanation.replace(/"/g, '&quot;').replace(/</g, '&lt;') : '';
            // Build a rich tooltip: error → explanation → context
            const tooltipParts = ['Error: ' + escError];
            if (escExplanation) tooltipParts.push(escExplanation);
            tooltipParts.push('Model: ' + (log.model || '-') + ' | Protocol: ' + (log.protocol || '-'));
            const tooltipText = tooltipParts.join('&#10;');
            status = `<span class="status-fail">&#10008;</span> <span class="error-text">${escError}</span><span class="status-info" title="${tooltipText}">&#9432;</span>`;
        }
        const thinking = log.thinking || '-';
        const effort = log.effort || '-';
        html += `<tr>
            <td>${formatDateTime(log.timestamp)}</td>
            <td>${log.original_model || '-'}</td>
            <td>${log.model || '-'}</td>
            <td>${formatNumber(log.tokens_input)}</td>
            <td>${formatNumber(log.tokens_output)}</td>
            <td>${formatNumber(log.tokens_cache)}</td>
            <td>${thinking}</td>
            <td>${effort}</td>
            <td>${duration}</td>
            <td>${status}</td>
        </tr>`;
    }
    tbody.innerHTML = html;

    // Update pagination
    totalPages = Math.ceil(data.total / perPage);
    updatePagination(currentPage, totalPages);
}

function renderConfig(data) {
    if (!data) return;

    // Proxy status
    const statusDot = document.getElementById('proxy-status-dot');
    const statusText = document.getElementById('proxy-status-text');
    const statusAddr = document.getElementById('proxy-status-addr');
    const statusLan = document.getElementById('proxy-status-lan');
    const btnStart = document.getElementById('btn-proxy-start');
    const btnStop = document.getElementById('btn-proxy-stop');

    if (data.proxy_running) {
        statusDot.className = 'status-dot running';
        statusText.textContent = 'Running';
        btnStart.disabled = true;
        btnStop.disabled = false;
    } else {
        statusDot.className = 'status-dot stopped';
        statusText.textContent = 'Stopped';
        btnStart.disabled = false;
        btnStop.disabled = true;
    }

    // Local address — click to copy
    const localUrl = `http://localhost:${data.port}`;
    statusAddr.textContent = localUrl;
    statusAddr.title = 'Click to copy';
    statusAddr.onclick = () => {
        navigator.clipboard.writeText(localUrl).then(() => {
            statusAddr.textContent = 'Copied!';
            setTimeout(() => { statusAddr.textContent = localUrl; }, 1500);
        }).catch(() => {});
    };

    // LAN address
    if (data.local_ip && data.local_ip !== '127.0.0.1') {
        const lanUrl = `http://${data.local_ip}:${data.port}`;
        statusLan.textContent = ' / ' + lanUrl;
        statusLan.title = 'Click to copy';
        statusLan.className = 'config-addr clickable';
        statusLan.style.display = '';
        statusLan.onclick = () => {
            navigator.clipboard.writeText(lanUrl).then(() => {
                statusLan.textContent = ' / Copied!';
                setTimeout(() => { statusLan.textContent = ' / ' + lanUrl; }, 1500);
            }).catch(() => {});
        };
    } else {
        statusLan.style.display = 'none';
    }

    // Port settings
    document.getElementById('cfg-port').value = data.port;
    document.getElementById('cfg-web-port').value = data.web_port;
    document.getElementById('cfg-proxy').value = data.proxy || '';
    document.getElementById('cfg-bind-address').value = data.host || '0.0.0.0';
    document.getElementById('cfg-routing').value = data.routing || 'round-robin';
    document.getElementById('cfg-disable-mapping').checked = data.disable_mapping || false;

    // Enable/disable mapping fields based on checkbox
    const mappingGrid = document.getElementById('mapping-grid');
    const routeFields = mappingGrid ? mappingGrid.querySelectorAll('.config-field') : [];
    routeFields.forEach(f => f.style.opacity = data.disable_mapping ? '0.4' : '1');

    // Model mapping selects
    const modelIds = Object.keys(data.models);
    availableModels = modelIds;
    for (const route of ['opus', 'sonnet', 'haiku']) {
        const select = document.getElementById(`route-${route}`);
        const currentModel = data.routes[route]?.model || '';
        select.innerHTML = modelIds.map(id =>
            `<option value="${id}" ${id === currentModel ? 'selected' : ''}>${id}</option>`
        ).join('');
    }

    // Populate cr-model select in add section
    const crModelSelect = document.getElementById('cr-model');
    crModelSelect.innerHTML = '<option value="">Select model...</option>' +
        modelIds.map(id => `<option value="${id}">${id}</option>`).join('');

    // Available models table
    const tbody = document.getElementById('models-tbody');
    const limits = data.model_limits || {};
    const caps = data.model_capabilities || {};
    const capLabels = { chat: 'Chat', vision: 'Vision', tools: 'Tools', code: 'Code', 'web-search': 'Web' };
    let html = '';
    for (const [id, info] of Object.entries(data.models)) {
        const lim = limits[id] || [];
        const modelCaps = caps[id] || [];
        const capHtml = modelCaps.map(c => `<span class="cap-badge">${capLabels[c] || c}</span>`).join('') || '<span class="text-dim">-</span>';
        const badge = info.source === 'upstream' ? ' <span class="new-badge">NEW</span>' : '';
        html += `<tr>
            <td><span class="clickable-model-id" data-id="${id}">${id}</span>${badge}</td>
            <td style="white-space:nowrap">${capHtml}</td>
            <td>${info.protocol}</td>
            <td>${info.endpoint}</td>
            <td>${lim[0] ? formatNumber(lim[0]) : '-'}</td>
            <td>${lim[1] ? formatNumber(lim[1]) : '-'}</td>
            <td>${lim[2] ? formatNumber(lim[2]) : '-'}</td>
        </tr>`;
    }
    tbody.innerHTML = html || '<tr><td colspan="7">No models</td></tr>';

    // API keys table
    renderApiKeysTable(data.api_keys || []);

    // Custom routes table
    renderCustomRoutes(data.custom_routes || {}, modelIds);
}

function renderCustomRoutes(routes, modelIds) {
    const tbody = document.getElementById('custom-routes-tbody');
    const entries = Object.entries(routes);
    if (entries.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3">No custom routes</td></tr>';
        return;
    }
    const opts = modelIds.map(id => `<option value="${id}">${id}</option>`).join('');
    let html = '';
    for (const [key, info] of entries) {
        const match = (info.match || []).join(', ');
        const model = info.model || '';
        html += `<tr>
            <td><input type="text" class="config-input cr-edit-match" value="${match}" style="width:100%"></td>
            <td><select class="config-select cr-edit-model" style="width:100%"><option value="">Select...</option>${opts.replace(`value="${model}"`, `value="${model}" selected`)}</select></td>
            <td><button class="btn btn-danger btn-sm cr-delete-btn" data-key="${key}">Delete</button></td>
        </tr>`;
    }
    tbody.innerHTML = html;
}

function renderApiKeysTable(apiKeys) {
    const tbody = document.getElementById('api-keys-tbody');
    if (!apiKeys || apiKeys.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4">No API keys configured</td></tr>';
        return;
    }
    tbody.innerHTML = apiKeys.map((k, i) => {
        const keyPlaceholder = k.api_key_masked || '****';
        const ws = k.go_workspace_id || '';
        const wsPlaceholder = k.go_workspace_id_masked || ws.slice(0, 4) + '****' || '';
        const cookiePlaceholder = k.go_auth_cookie_masked || '****';
        return `<tr data-index="${i}">
            <td><input type="password" class="config-input ak-key" value="${k.api_key || ''}" placeholder="${keyPlaceholder}" style="width:100%;font-family:monospace"></td>
            <td><input type="text" class="config-input ak-workspace" value="${ws}" style="width:100%"></td>
            <td><input type="password" class="config-input ak-cookie" value="${k.go_auth_cookie || ''}" placeholder="${cookiePlaceholder}" style="width:100%;font-family:monospace"></td>
            <td><button class="btn btn-danger btn-sm ak-delete-btn">Delete</button></td>
        </tr>`;
    }).join('');
}

function updatePagination(current, total) {
    const pageInfo = document.getElementById('page-info');
    const prevBtn = document.getElementById('prev-page');
    const nextBtn = document.getElementById('next-page');

    pageInfo.textContent = `Page ${current} of ${total}`;
    prevBtn.disabled = current <= 1;
    nextBtn.disabled = current >= total;
}

function setupTabs() {
    const tabs = document.querySelectorAll('.tab');
    const contents = document.querySelectorAll('.tab-content');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            const target = tab.dataset.tab;
            tabs.forEach(t => t.classList.remove('active'));
            contents.forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(target).classList.add('active');
            // Load data when switching tabs
            if (target === 'config') {
                fetchConfig().then(renderConfig);
            } else if (target === 'quotas') {
                fetchQuotas().then(renderQuotas);
            }
        });
    });
}

function setupFilter() {
    const rangeBtns = document.querySelectorAll('.filter-bar .btn[data-range]');
    const customFields = [document.getElementById('from-date'), document.getElementById('to-date'),
                          document.getElementById('date-sep'), document.getElementById('apply-filter')];

    function setActiveRange(range) {
        rangeBtns.forEach(b => b.classList.toggle('active', b.dataset.range === range));
        const isCustom = range === 'custom';
        customFields.forEach(el => el.style.display = isCustom ? '' : 'none');
    }

    rangeBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const range = btn.dataset.range;
            setActiveRange(range);
            currentPage = 1; // Reset to first page when filter changes
            if (range === 'today') { filterFrom = todayStr(); filterTo = todayStr(); }
            else if (range === '7d') { filterFrom = daysAgoStr(7); filterTo = todayStr(); }
            else if (range === '30d') { filterFrom = daysAgoStr(30); filterTo = todayStr(); }
            else if (range === 'custom') {
                document.getElementById('from-date').value = filterFrom;
                document.getElementById('to-date').value = filterTo;
                return;
            }
            refreshAll();
        });
    });

    document.getElementById('apply-filter').addEventListener('click', () => {
        filterFrom = document.getElementById('from-date').value;
        filterTo = document.getElementById('to-date').value;
        currentPage = 1;
        refreshAll();
    });
}

function setupConfig() {
    // Disable mapping toggle
    const disableMapping = document.getElementById('cfg-disable-mapping');
    const mappingGrid = document.getElementById('mapping-grid');
    disableMapping.addEventListener('change', () => {
        const mappingFields = mappingGrid ? mappingGrid.querySelectorAll('.config-field, .config-select') : [];
        mappingFields.forEach(f => {
            f.disabled = disableMapping.checked;
            f.style.opacity = disableMapping.checked ? '0.4' : '1';
        });
    });

    // Save config
    const saveBtn = document.getElementById('btn-save-config');
    const saveStatus = document.getElementById('config-save-status');
    saveBtn.addEventListener('click', async () => {
        const payload = {
            routes: {
                opus: document.getElementById('route-opus').value,
                sonnet: document.getElementById('route-sonnet').value,
                haiku: document.getElementById('route-haiku').value,
            },
            port: parseInt(document.getElementById('cfg-port').value),
            web_port: parseInt(document.getElementById('cfg-web-port').value),
            host: document.getElementById('cfg-bind-address').value,
            routing: document.getElementById('cfg-routing').value,
            proxy: document.getElementById('cfg-proxy').value,
            disable_mapping: document.getElementById('cfg-disable-mapping').checked,
        };
        try {
            const resp = await fetch('/api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload),
            });
            const result = await resp.json();
            document.getElementById('restart-notice').style.display = result.needs_restart ? '' : 'none';
            saveStatus.textContent = result.message || 'Saved!';
            saveStatus.className = 'save-status success';
            setTimeout(() => { saveStatus.textContent = ''; }, 3000);
            // Refresh config display
            fetchConfig().then(renderConfig);
        } catch (e) {
            saveStatus.textContent = 'Error saving';
            saveStatus.className = 'save-status error';
            console.error('Failed to save config:', e);
        }
    });

    // Proxy start/stop
    document.getElementById('btn-proxy-start').addEventListener('click', async () => {
        try {
            await fetch('/api/proxy/start', { method: 'POST' });
            fetchConfig().then(renderConfig);
        } catch (e) { console.error(e); }
    });

    document.getElementById('btn-proxy-stop').addEventListener('click', async () => {
        try {
            await fetch('/api/proxy/stop', { method: 'POST' });
            fetchConfig().then(renderConfig);
        } catch (e) { console.error(e); }
    });

    // Custom routes: add
    document.getElementById('cr-add-btn').addEventListener('click', () => {
        const match = document.getElementById('cr-match').value.trim();
        const model = document.getElementById('cr-model').value;
        if (!match || !model) { alert('Enter match keyword and select a backend model.'); return; }
        const tbody = document.getElementById('custom-routes-tbody');
        const key = match.toLowerCase().replace(/[^a-z0-9]/g, '');
        const opts = availableModels.map(id => `<option value="${id}" ${id === model ? 'selected' : ''}>${id}</option>`).join('');
        const row = document.createElement('tr');
        row.id = `cr-row-${key}`;
        row.innerHTML = `<td><input type="text" class="config-input cr-edit-match" value="${match}" style="width:100%"></td>
            <td><select class="config-select cr-edit-model" style="width:100%"><option value="">Select...</option>${opts}</select></td>
            <td><button class="btn btn-danger btn-sm cr-delete-btn" data-key="${key}">Delete</button></td>`;
        // Remove empty state
        if (tbody.querySelector('td[colspan]')) tbody.innerHTML = '';
        tbody.appendChild(row);
        document.getElementById('cr-match').value = '';
        document.getElementById('cr-model').selectedIndex = 0;
    });

    // Custom routes: delete (delegated)
    document.getElementById('custom-routes-tbody').addEventListener('click', (e) => {
        const btn = e.target.closest('.cr-delete-btn');
        if (!btn) return;
        const row = btn.closest('tr');
        if (row) row.remove();
        const tbody = document.getElementById('custom-routes-tbody');
        if (!tbody.querySelector('tr')) {
            tbody.innerHTML = '<tr><td colspan="3">No custom routes</td></tr>';
        }
    });

    // Custom routes: save
    document.getElementById('cr-save-btn').addEventListener('click', async () => {
        const tbody = document.getElementById('custom-routes-tbody');
        const rows = tbody.querySelectorAll('tr');
        const routes = {};
        for (const row of rows) {
            const matchInput = row.querySelector('.cr-edit-match');
            const modelInput = row.querySelector('.cr-edit-model');
            if (!matchInput || !modelInput) continue;
            const match = matchInput.value.trim();
            const model = modelInput.value.trim();
            if (!match || !model) continue;
            const key = match.toLowerCase().replace(/[^a-z0-9]/g, '');
            routes[key] = { match: [match], model: model };
        }
        const status = document.getElementById('cr-save-status');
        try {
            const resp = await fetch('/api/config/custom-routes', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(routes),
            });
            const result = await resp.json();
            status.textContent = 'Saved!';
            status.className = 'save-status success';
            setTimeout(() => { status.textContent = ''; }, 3000);
            fetchConfig().then(renderConfig);
        } catch (e) {
            status.textContent = 'Error saving';
            status.className = 'save-status error';
            console.error(e);
        }
    });

    // ── API Keys management ──

    // Add key row
    document.getElementById('api-key-add-btn').addEventListener('click', () => {
        const tbody = document.getElementById('api-keys-tbody');
        const emptyRow = tbody.querySelector('td[colspan]');
        if (emptyRow) tbody.innerHTML = '';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><input type="password" class="config-input ak-key" placeholder="sk-..." style="width:100%;font-family:monospace"></td>
            <td><input type="text" class="config-input ak-workspace" placeholder="wrk_..." style="width:100%"></td>
            <td><input type="password" class="config-input ak-cookie" placeholder="Fe26.2..." style="width:100%;font-family:monospace"></td>
            <td><button class="btn btn-danger btn-sm ak-delete-btn">Delete</button></td>
        `;
        tbody.appendChild(tr);
    });

    // Delete key row (delegated)
    document.getElementById('api-keys-tbody').addEventListener('click', (e) => {
        const btn = e.target.closest('.ak-delete-btn');
        if (!btn) return;
        const row = btn.closest('tr');
        if (row) row.remove();
        const tbody = document.getElementById('api-keys-tbody');
        if (!tbody.querySelector('tr')) {
            tbody.innerHTML = '<tr><td colspan="4">No API keys configured</td></tr>';
        }
    });

    // Save API keys
    document.getElementById('api-key-save-btn').addEventListener('click', async () => {
        const rows = document.querySelectorAll('#api-keys-tbody tr');
        const keys = [];
        for (const row of rows) {
            const apiKey = row.querySelector('.ak-key')?.value?.trim();
            if (!apiKey) continue; // skip empty rows
            keys.push({
                api_key: apiKey,
                go_workspace_id: row.querySelector('.ak-workspace')?.value?.trim() || '',
                go_auth_cookie: row.querySelector('.ak-cookie')?.value?.trim() || '',
            });
        }
        const status = document.getElementById('api-key-save-status');
        try {
            const resp = await fetch('/api/config/api-keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ api_keys: keys }),
            });
            const result = await resp.json();
            status.textContent = 'Saved!';
            status.className = 'save-status success';
            setTimeout(() => { status.textContent = ''; }, 3000);
            fetchConfig().then(renderConfig);
        } catch (e) {
            status.textContent = 'Error saving';
            status.className = 'save-status error';
            console.error(e);
        }
    });
}

async function refreshAll() {
    const [stats, history, quotas] = await Promise.all([
        fetchStats(filterFrom, filterTo),
        fetchHistory(filterFrom, filterTo, currentPage),
        fetchQuotas()
    ]);
    renderStats(stats);
    renderCharts(stats);
    renderHistory(history);
    renderQuotas(quotas);
    document.getElementById('last-update').textContent = `Last update: ${formatTime()}`;
}

async function loadHistory() {
    const history = await fetchHistory(filterFrom, filterTo, currentPage);
    renderHistory(history);
}

async function deleteHistory(before = null, all = false) {
    try {
        let url = '/api/history?';
        if (all) url += 'all=true';
        else {
            const d = before || filterTo;
            url += `before=${d}`;
        }
        await fetch(url, { method: 'DELETE' });
        currentPage = 1;
        refreshAll();
    } catch (e) {
        console.error('Failed to delete:', e);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // Theme
    const saved = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    const toggle = document.getElementById('theme-toggle');
    toggle.textContent = saved === 'dark' ? '☾' : '☀';
    toggle.addEventListener('click', () => {
        const current = document.documentElement.getAttribute('data-theme');
        const next = current === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        localStorage.setItem('theme', next);
        toggle.textContent = next === 'dark' ? '☾' : '☀';
    });

    setupTabs();
    setupFilter();
    setupConfig();
    refreshAll();

    // ── SSE real-time updates ──
    let eventSource = null;
    let pollTimer = null;

    function startPolling(intervalMs) {
        if (pollTimer) clearInterval(pollTimer);
        pollTimer = setInterval(refreshAll, intervalMs);
    }

    function stopPolling() {
        if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    }

    function connectSSE() {
        if (eventSource) eventSource.close();
        const es = new EventSource('/api/events');
        eventSource = es;

        es.addEventListener('stats_updated', () => {
            if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
            // Debounce: coalesce rapid events into a single refresh
            if (window._sseRefresh) clearTimeout(window._sseRefresh);
            window._sseRefresh = setTimeout(() => {
                window._sseRefresh = null;
                refreshAll();
            }, 200);
        });
        es.addEventListener('connected', () => { stopPolling(); });
        es.addEventListener('quotas_updated', () => {
            fetchQuotas().then(renderQuotas);
        });

        es.onerror = () => {
            if (es !== eventSource) return; // stale
            if (eventSource) { eventSource.close(); eventSource = null; }
            if (!pollTimer) startPolling(15000);
            setTimeout(connectSSE, 5000);
        };
    }

    connectSSE();

    // Backup poll in case SSE never connects
    if (!pollTimer) startPolling(30000);

    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            if (eventSource) { eventSource.close(); eventSource = null; }
            stopPolling();
        } else {
            refreshAll();
            if (!eventSource) connectSSE();
            if (!pollTimer) startPolling(30000);
        }
    });

    // ── Quota countdown ticker ──
    setInterval(() => {
        document.querySelectorAll('.quota-reset').forEach(el => {
            const target = parseInt(el.dataset.resetTarget || '0', 10);
            if (!target) return;
            const remaining = target - Math.floor(Date.now() / 1000);
            el.textContent = remaining <= 0 ? 'Resetting...' : 'Resets in ' + formatResetTime(remaining);
        });
    }, 1000);

    const deleteBtn = document.getElementById('delete-btn');
    const deleteMenu = document.getElementById('delete-menu');
    const deleteDate = document.getElementById('delete-date');

    deleteBtn.addEventListener('click', () => {
        deleteMenu.style.display = deleteMenu.style.display === 'none' ? '' : 'none';
    });

    document.getElementById('delete-all-opt').addEventListener('click', () => {
        if (confirm('Delete all history?')) {
            deleteHistory(null, true);
        }
        deleteMenu.style.display = 'none';
    });

    document.getElementById('delete-by-date-opt').addEventListener('click', () => {
        deleteMenu.style.display = 'none';
        document.getElementById('delete-date').value = todayStr();
        document.getElementById('delete-modal').style.display = '';
    });

    document.getElementById('modal-delete-btn').addEventListener('click', () => {
        const d = document.getElementById('delete-date').value;
        if (d && confirm(`Delete history before ${d}?`)) {
            deleteHistory(d);
        }
        document.getElementById('delete-modal').style.display = 'none';
    });

    document.getElementById('modal-cancel-btn').addEventListener('click', () => {
        document.getElementById('delete-modal').style.display = 'none';
    });

    // Pagination buttons
    document.getElementById('prev-page').addEventListener('click', () => {
        if (currentPage > 1) {
            currentPage--;
            loadHistory();
        }
    });

    document.getElementById('next-page').addEventListener('click', () => {
        if (currentPage < totalPages) {
            currentPage++;
            loadHistory();
        }
    });

    document.addEventListener('click', (e) => {
        if (!e.target.closest('.delete-section')) {
            deleteMenu.style.display = 'none';
        }
    });

    // ── Model ID click-to-copy ──
    document.getElementById('models-tbody').addEventListener('click', (e) => {
        const el = e.target.closest('.clickable-model-id');
        if (!el) return;
        const id = el.dataset.id;
        if (!id) return;
        navigator.clipboard.writeText(id).then(() => {
            const orig = el.textContent;
            el.textContent = '✓ Copied';
            el.style.color = 'var(--success)';
            setTimeout(() => {
                el.textContent = orig;
                el.style.color = '';
            }, 1500);
        }).catch(() => {});
    });
});