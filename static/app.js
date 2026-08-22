/* ===== Football Anomaly Bot - Dashboard JS ===== */

const API = {
    anomalies: (status) => `/api/anomalies${status ? `?status=${status}` : ''}`,
    updateStatus: (id) => `/api/anomalies/${id}/status`,
    bulkStatus: '/api/anomalies/bulk-status',
    deleteAnomalies: '/api/anomalies/delete',
    clearAnomalies: '/api/anomalies/clear',
    deletedAnomalies: (result, hideUnique) => {
        const params = new URLSearchParams();
        if (result) params.set('result', result);
        if (hideUnique) params.set('hide_unique', 'true');
        const query = params.toString();
        return `/api/anomalies/deleted${query ? `?${query}` : ''}`;
    },
    restoreAnomalies: '/api/anomalies/restore',
    purgeAnomalies: '/api/anomalies/purge',
    purgeAllAnomalies: '/api/anomalies/purge-all',
    anomalyDetails: (id) => `/api/anomalies/${encodeURIComponent(id)}/details`,
    upcoming: (status) => `/api/upcoming${status ? `?status=${status}` : ''}`,
    updateUpcomingStatus: (id) => `/api/upcoming/${id}/status`,
    bulkUpcomingStatus: '/api/upcoming/bulk-status',
    deleteUpcoming: '/api/upcoming/delete',
    clearUpcoming: '/api/upcoming/clear',
    clearDatabase: '/api/database/clear',
    status: '/api/status',
    triggerUpcoming: '/api/trigger/upcoming-scan',
    triggerFinished: '/api/trigger/finished-scan',
};

const ICONS = {
    bet: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 1v22"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>',
    ignore: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>',
    follow: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
    delete: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>',
    restore: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>',
    purge: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
    details: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18"/><path d="M3 12h18"/><path d="M3 18h12"/></svg>',
};

let anomalies = [];
let upcomingMatches = [];
let deletedAnomalies = [];
let deletedSummary = {
    total: 0, successful: 0, failed: 0, pending: 0, unresolved: 0,
    evaluated: 0, finished_matches: 0, success_rate: 0,
};
let schedulerJobs = [];

const selectedAnomalies = new Set();
const selectedUpcoming = new Set();
const selectedDeleted = new Set();

const anomalyDetailsCache = new Map();
const anomalyDetailsInFlight = new Map();
const expandedAnomalyRows = new Set();
let activeAnomalyMatchFilter = null;

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

function setText(selector, value) {
    const el = $(selector);
    if (el) el.textContent = value;
}

function setAttr(selector, name, value) {
    const el = $(selector);
    if (el) el.setAttribute(name, value);
}

$$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => {
        $$('.tab').forEach((item) => item.classList.remove('active'));
        $$('.tab-content').forEach((item) => item.classList.remove('active'));
        tab.classList.add('active');
        $(`#tab-${tab.dataset.tab}`).classList.add('active');
        if (tab.dataset.tab === 'deleted') loadDeletedAnomalies();
    });
});

function toast(message, isError = false) {
    let el = $('.toast');
    if (!el) {
        el = document.createElement('div');
        el.className = 'toast';
        document.body.appendChild(el);
    }

    el.textContent = message;
    el.classList.toggle('error', isError);
    el.classList.add('show');
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.remove('show'), 3200);
}

async function apiFetch(url, opts = {}) {
    try {
        const response = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...opts,
        });

        if (!response.ok) {
            let message = `HTTP ${response.status}`;
            try {
                const errorBody = await response.json();
                message = errorBody.error || errorBody.detail || message;
            } catch (_) {
                // Keep the HTTP status when the server did not return JSON.
            }
            throw new Error(message);
        }

        return await response.json();
    } catch (error) {
        toast(`İstek başarısız: ${error.message}`, true);
        return null;
    }
}

async function apiPost(url, body) {
    return apiFetch(url, { method: 'POST', body: JSON.stringify(body) });
}

async function postRaw(url, body) {
    try {
        const response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        return { ok: response.ok, status: response.status };
    } catch (error) {
        return { ok: false, status: 0, error };
    }
}

function setButtonBusy(button, busyLabel, idleLabel, busy) {
    if (!button) return;
    button.disabled = busy;
    button.textContent = busy ? busyLabel : idleLabel;
}

function touchLastUpdated() {
    const value = new Date().toLocaleTimeString('tr-TR', {
        timeZone: 'Europe/Istanbul',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
    });
    setText('#last-updated', value);
    setAttr('#status-pill', 'title', `Son yenileme ${value}`);
}

function updateOverview() {
    const anomalyTotal = anomalies.length;
    const newAnomalies = anomalies.filter((item) => item.status === 'new').length;
    const followingAnomalies = anomalies.filter((item) => item.status === 'following').length;
    const followingUpcoming = upcomingMatches.filter((item) => item.status === 'following').length;

    setText('#metric-anomalies', String(anomalyTotal));
    setText('#metric-anomalies-detail', `${newAnomalies} yeni, ${followingAnomalies} takipte`);

    setText('#metric-following', String(followingAnomalies + followingUpcoming));
    setText('#metric-following-detail', `${followingAnomalies} anomali, ${followingUpcoming} gelecek maç`);

    setText('#scheduler-count', `${schedulerJobs.length}`);
    setAttr('#status-pill', 'aria-label', `Sistem durumu, ${schedulerJobs.length} görev`);
}

async function clearAllWithFallback({ clearUrl, deleteUrl, ids, emptyText, successText }) {
    if (!ids.length) {
        toast(emptyText);
        return false;
    }

    const clearRes = await postRaw(clearUrl, {});
    if (clearRes.ok) {
        toast(successText);
        return true;
    }

    if (clearRes.status === 404) {
        const deleteRes = await apiPost(deleteUrl, { ids });
        if (deleteRes && deleteRes.ok) {
            toast(successText);
            return true;
        }
    }

    const code = clearRes.status || 'ağ';
    toast(`İstek başarısız: HTTP ${code}`, true);
    return false;
}

async function checkStatus() {
    const data = await apiFetch(API.status);
    const dot = $('#status-indicator');
    const pill = $('#status-pill');

    if (data && data.status === 'running') {
        schedulerJobs = data.scheduler_jobs || [];
        if (dot) dot.className = 'status-dot online';
        if (pill) {
            pill.setAttribute('aria-label', 'Sistem aktif');
            pill.setAttribute('title', 'Sistem aktif');
        }
        touchLastUpdated();
    } else {
        schedulerJobs = [];
        if (dot) dot.className = 'status-dot error';
        if (pill) {
            pill.setAttribute('aria-label', 'Sistem pasif');
            pill.setAttribute('title', 'Sistem pasif');
        }
    }

    updateOverview();
}

const sortState = {};

function initSortableHeaders() {
    $$('.sortable').forEach((th) => {
        th.addEventListener('click', () => {
            const table = th.closest('table');
            const key = th.dataset.sort;
            const tableId = table.id;
            const prev = sortState[tableId];

            if (prev && prev.key === key) {
                sortState[tableId] = { key, dir: prev.dir === 'asc' ? 'desc' : 'asc' };
            } else {
                sortState[tableId] = { key, dir: 'asc' };
            }

            table.querySelectorAll('.sortable').forEach((header) => {
                header.classList.remove('sort-asc', 'sort-desc');
            });
            th.classList.add(sortState[tableId].dir === 'asc' ? 'sort-asc' : 'sort-desc');

            if (tableId === 'anomaly-table') renderAnomalies();
            if (tableId === 'upcoming-table') renderUpcoming();
        });
    });
}

function sortData(data, tableId, getSortValue) {
    const state = sortState[tableId];
    if (!state) return data;

    return [...data].sort((a, b) => {
        const first = getSortValue(a, state.key);
        const second = getSortValue(b, state.key);

        if (first < second) return state.dir === 'asc' ? -1 : 1;
        if (first > second) return state.dir === 'asc' ? 1 : -1;
        return 0;
    });
}

function filterBySearch(data, query, getSearchText) {
    if (!query) return data;
    const lowered = query.toLowerCase();
    return data.filter((item) => getSearchText(item).toLowerCase().includes(lowered));
}

function sofascoreEventUrl(eventId) {
    if (!eventId) return '#';
    return `https://www.sofascore.com/event/${encodeURIComponent(eventId)}`;
}

async function loadAnomalies() {
    const filter = $('#filter-status').value;
    const data = await apiFetch(API.anomalies(filter));
    if (!data) return;

    anomalies = data;
    if (activeAnomalyMatchFilter && !anomalies.some(
        (item) => String(item.match_id) === activeAnomalyMatchFilter
    )) {
        activeAnomalyMatchFilter = null;
    }
    renderAnomalies();
    updateOverview();
    touchLastUpdated();
}

function renderAnomalies() {
    const tbody = $('#anomaly-body');
    const selectAll = $('#select-all-anomalies');
    selectedAnomalies.clear();
    if (selectAll) selectAll.checked = false;
    updateBulkButtons();

    const searchQuery = ($('#search-anomalies') || {}).value || '';
    let filtered = filterBySearch(anomalies, searchQuery, (item) =>
        `${item.home_team} ${item.away_team} ${item.league} ${item.condition_type}`
    );
    if (activeAnomalyMatchFilter) {
        filtered = filtered.filter(
            (item) => String(item.match_id) === activeAnomalyMatchFilter
        );
    }

    filtered = sortData(filtered, 'anomaly-table', (item, key) => {
        switch (key) {
            case 'match':
                return `${item.home_team} ${item.away_team}`.toLowerCase();
            case 'score':
                return item.score_home * 100 + item.score_away;
            case 'minute':
                return item.minute;
            case 'league':
                return (item.league || '').toLowerCase();
            case 'condition':
                return item.condition_type;
            case 'time':
                return item.detected_at_tr || item.created_at || '';
            default:
                return '';
        }
    });

    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty-msg">Anomali bulunamadı</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map((item) => {
        const rules = item.triggered_rules || [];
        const ruleHtml = rules.map((rule) => `<li>${escHtml(rule)}</li>`).join('');
        const stateClass = item.status !== 'new' ? `state-${item.status}` : '';
        const time = item.detected_at_tr
            ? formatTurkeyTimestamp(item.detected_at_tr)
            : formatCreatedAt(item.created_at);
        const conditionBadge = item.condition_type === 'A'
            ? '<span class="badge badge-a">A / Beraberlik</span>'
            : '<span class="badge badge-b">B / 1 Fark</span>';
        const alertNumber = item.alert_number || 1;
        const signalBadge = alertNumber > 1
            ? `<button type="button" class="signal-filter${activeAnomalyMatchFilter === String(item.match_id) ? ' active' : ''}" data-match-id="${escHtml(item.match_id)}" title="Bu maçın sinyallerini göster">${alertNumber}. sinyal</button>`
            : '';

        const isExpanded = expandedAnomalyRows.has(item.id);
        const expandedClass = isExpanded ? 'expanded' : '';
        const detailsHidden = isExpanded ? '' : 'display:none;';
        return `
        <tr class="anomaly-row-main ${expandedClass} ${stateClass}" data-id="${item.id}">
            <td class="col-check"><input type="checkbox" class="chk-anomaly" data-id="${item.id}"></td>
            <td>
                <div class="cell-stack">
                    <a class="match-link" href="${sofascoreEventUrl(item.match_id)}" target="_blank" rel="noopener noreferrer">
                        ${escHtml(item.home_team)} vs ${escHtml(item.away_team)}
                    </a>
                    ${signalBadge}
                    <span class="cell-subtle">Maç ID: ${escHtml(item.match_id)}</span>
                </div>
            </td>
            <td><span class="score-pill">${item.score_home} - ${item.score_away}</span></td>
            <td><span class="table-tag">${item.minute}'</span></td>
            <td>
                <div class="cell-stack">
                    <span>${escHtml(item.league || '-')}</span>
                    <span class="cell-subtle">${statusLabel(item.status)}</span>
                </div>
            </td>
            <td>${conditionBadge}</td>
            <td><ul class="rules-list">${ruleHtml}</ul></td>
            <td><span class="time-pill">${time}</span></td>
            <td>
                <div class="row-actions row-actions-icons">
                    <button class="icon-btn icon-btn-details${isExpanded ? ' active' : ''}" onclick="toggleAnomalyDetails(${item.id})" title="Detay" aria-label="Detay">${ICONS.details}</button>
                    <button class="icon-btn icon-btn-bet${item.status === 'bet_placed' ? ' active' : ''}" onclick="setStatus(${item.id}, 'bet_placed')" title="Bahis oynandı" aria-label="Bahis oynandı">${ICONS.bet}</button>
                    <button class="icon-btn icon-btn-ignore${item.status === 'ignored' ? ' active' : ''}" onclick="setStatus(${item.id}, 'ignored')" title="Gözardı et" aria-label="Gözardı et">${ICONS.ignore}</button>
                    <button class="icon-btn icon-btn-follow${item.status === 'following' ? ' active' : ''}" onclick="setStatus(${item.id}, 'following')" title="Takip et" aria-label="Takip et">${ICONS.follow}</button>
                    <button class="icon-btn icon-btn-delete" onclick="deleteAnomalyRow(${item.id})" title="Sil" aria-label="Sil">${ICONS.delete}</button>
                </div>
            </td>
        </tr>
        <tr class="anomaly-row-details" data-aid-details="${item.id}" style="${detailsHidden}">
            <td colspan="9">
                <div class="anomaly-details">Detaylar yükleniyor...</div>
            </td>
        </tr>`;
    }).join('');

    expandedAnomalyRows.forEach((aid) => renderAnomalyDetails(aid));

    $$('.chk-anomaly').forEach((checkbox) => {
        checkbox.addEventListener('change', () => {
            const id = Number(checkbox.dataset.id);
            if (checkbox.checked) selectedAnomalies.add(id);
            else selectedAnomalies.delete(id);
            updateBulkButtons();
        });
    });
    $$('.signal-filter').forEach((button) => {
        button.addEventListener('click', () => {
            const matchId = String(button.dataset.matchId);
            const wasActive = activeAnomalyMatchFilter === matchId;
            activeAnomalyMatchFilter = wasActive ? null : matchId;
            renderAnomalies();
            toast(wasActive ? 'Maç filtresi kaldırıldı' : 'Yalnızca bu maçın sinyalleri gösteriliyor');
        });
    });
}

async function setStatus(id, status) {
    const anomaly = anomalies.find((item) => item.id === id);
    if (!anomaly) return;

    const newStatus = anomaly.status === status ? 'new' : status;
    const result = await apiPost(API.updateStatus(id), { status: newStatus });
    if (!result || !result.ok) return;

    await loadAnomalies();
    const extra = result.updated > 1 ? ` (${result.updated} sinyal)` : '';
    toast(`Durum güncellendi: ${statusLabel(newStatus)}${extra}`);
}

async function deleteAnomalyRow(id) {
    const anomaly = anomalies.find((item) => item.id === id);
    if (!anomaly) return;

    const siblings = anomalies.filter((item) => item.match_id === anomaly.match_id);
    const ids = siblings.map((item) => item.id);
    const extra = siblings.length > 1 ? ` (${siblings.length} sinyal)` : '';

    if (!confirm(`${anomaly.home_team} vs ${anomaly.away_team} kaydı silinsin mi?${extra}\nSilinen Maçlar bölümüne taşınacak.`)) return;

    const result = await apiPost(API.deleteAnomalies, { ids });
    if (!result || !result.ok) return;

    toast(`${ids.length} kayıt Silinen Maçlar'a taşındı`);
    await loadAnomalies();
}

function updateBulkButtons() {
    const count = selectedAnomalies.size;
    $('#selected-count').textContent = `${count} seçili`;
    $('#btn-bulk-bet').disabled = count === 0;
    $('#btn-bulk-ignore').disabled = count === 0;
    $('#btn-bulk-follow').disabled = count === 0;
    $('#btn-bulk-delete').disabled = count === 0;
}

async function bulkStatus(status) {
    const ids = [...selectedAnomalies];
    const result = await apiPost(API.bulkStatus, { ids, status });
    if (!result || !result.ok) return;

    await loadAnomalies();
    toast(`${result.updated || ids.length} sinyal güncellendi: ${statusLabel(status)}`);
}

$('#select-all-anomalies').addEventListener('change', (event) => {
    const checked = event.target.checked;
    $$('.chk-anomaly').forEach((checkbox) => {
        checkbox.checked = checked;
        const id = Number(checkbox.dataset.id);
        if (checked) selectedAnomalies.add(id);
        else selectedAnomalies.delete(id);
    });
    updateBulkButtons();
});

$('#btn-bulk-bet').addEventListener('click', () => bulkStatus('bet_placed'));
$('#btn-bulk-ignore').addEventListener('click', () => bulkStatus('ignored'));
$('#btn-bulk-follow').addEventListener('click', () => bulkStatus('following'));

$('#btn-bulk-delete').addEventListener('click', async () => {
    if (!confirm(`${selectedAnomalies.size} anomali Silinen Maçlar bölümüne taşınsın mı?`)) return;

    const ids = [...selectedAnomalies];
    const result = await apiPost(API.deleteAnomalies, { ids });
    if (!result || !result.ok) return;

    toast(`${ids.length} kayıt Silinen Maçlar'a taşındı`);
    await loadAnomalies();
});

$('#btn-clear-all-anomalies').addEventListener('click', async () => {
    if (!confirm('Tüm anomaliler Silinen Maçlar bölümüne taşınsın mı?')) return;

    const ok = await clearAllWithFallback({
        clearUrl: API.clearAnomalies,
        deleteUrl: API.deleteAnomalies,
        ids: anomalies.map((item) => item.id),
        emptyText: 'Taşınacak anomali bulunamadı',
        successText: 'Tüm anomaliler Silinen Maçlar\'a taşındı',
    });

    if (ok) await loadAnomalies();
});

$('#filter-status').addEventListener('change', loadAnomalies);
$('#btn-refresh').addEventListener('click', loadAnomalies);
$('#search-anomalies').addEventListener('input', renderAnomalies);

async function loadUpcoming() {
    const filter = $('#filter-upcoming-status').value;
    const data = await apiFetch(API.upcoming(filter));
    if (!data) return;

    upcomingMatches = data;
    renderUpcoming();
    updateOverview();
    touchLastUpdated();
}

function getVisibleUpcomingMatches() {
    const searchQuery = ($('#search-upcoming') || {}).value || '';
    let filtered = filterBySearch(upcomingMatches, searchQuery, (item) =>
        `${item.home_team} ${item.away_team} ${item.league} ${item.round_info || ''}`
    );

    return sortData(filtered, 'upcoming-table', (item, key) => {
        switch (key) {
            case 'match':
                return `${item.home_team} ${item.away_team}`.toLowerCase();
            case 'start':
                return Number(item.start_time) || 0;
            case 'league':
                return (item.league || '').toLowerCase();
            case 'round':
                return (item.round_info || '').toLowerCase();
            case 'status':
                return item.status || 'new';
            default:
                return '';
        }
    });
}

function renderUpcoming() {
    const tbody = $('#upcoming-body');
    const selectAll = $('#select-all-upcoming');
    selectedUpcoming.clear();
    if (selectAll) selectAll.checked = false;
    updateUpcomingBulk();

    const filtered = getVisibleUpcomingMatches();
    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">Gelecek 24 saatte maç bulunamadı veya liste henüz çekilmedi</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map((item) => {
        const stateClass = item.status !== 'new' ? `state-${item.status}` : '';
        const anomalyClass = item.has_anomaly ? 'anomaly-row' : '';
        const anomalyBadge = item.has_anomaly
            ? '<span class="badge badge-anomaly">Anomali etiketi</span>'
            : '';

        return `
        <tr class="${stateClass} ${anomalyClass}" data-id="${item.id}">
            <td class="col-check"><input type="checkbox" class="chk-upcoming" data-id="${item.id}"></td>
            <td>
                <div class="cell-stack">
                    <a class="match-link" href="${sofascoreEventUrl(item.event_id)}" target="_blank" rel="noopener noreferrer">
                        ${escHtml(item.home_team)} vs ${escHtml(item.away_team)}
                    </a>
                    <span class="cell-subtle">Etkinlik ID: ${escHtml(item.event_id)}</span>
                </div>
            </td>
            <td><span class="time-pill">${formatStartTime(item.start_time)}</span></td>
            <td>${escHtml(item.league || '-')}</td>
            <td>${escHtml(item.round_info || '-')}</td>
            <td>
                <div class="cell-stack">
                    ${anomalyBadge}
                    <span class="upcoming-status-label">${upcomingStatusLabel(item.status)}</span>
                </div>
            </td>
            <td>
                <div class="row-actions">
                    <button class="row-btn row-btn-follow${item.status === 'following' ? ' active' : ''}" onclick="setUpcomingStatus(${item.id}, 'following')">Takip</button>
                    <button class="row-btn${item.status === 'ignored' ? ' active' : ''}" onclick="setUpcomingStatus(${item.id}, 'ignored')">Gözardı</button>
                </div>
            </td>
        </tr>`;
    }).join('');

    $$('.chk-upcoming').forEach((checkbox) => {
        checkbox.addEventListener('change', () => {
            const id = Number(checkbox.dataset.id);
            if (checkbox.checked) selectedUpcoming.add(id);
            else selectedUpcoming.delete(id);
            updateUpcomingBulk();
        });
    });
}

async function copyUpcomingMatches() {
    const filtered = getVisibleUpcomingMatches();
    if (!filtered.length) {
        toast('Kopyalanacak maç bulunamadı', true);
        return;
    }

    const text = filtered.map((item) =>
        `${formatStartTime(item.start_time)} - ${item.home_team} vs ${item.away_team}`
    ).join('\n');

    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(text);
        } else {
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.setAttribute('readonly', '');
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            textarea.remove();
        }
        toast(`${filtered.length} maç kopyalandı`);
    } catch (error) {
        toast(`Kopyalama başarısız: ${error.message}`, true);
    }
}

function upcomingStatusLabel(status) {
    const labels = {
        new: 'Yeni',
        following: 'Takip ediliyor',
        ignored: 'Gözardı edildi',
    };
    return labels[status] || status || 'Yeni';
}

async function setUpcomingStatus(id, status) {
    const match = upcomingMatches.find((item) => item.id === id);
    const newStatus = match && match.status === status ? 'new' : status;
    const result = await apiPost(API.updateUpcomingStatus(id), { status: newStatus });
    if (!result || !result.ok) return;

    if (match) match.status = newStatus;
    renderUpcoming();
    updateOverview();
    toast(`Durum güncellendi: ${upcomingStatusLabel(newStatus)}`);
}

function updateUpcomingBulk() {
    const count = selectedUpcoming.size;
    $('#selected-count-upcoming').textContent = `${count} seçili`;
    $('#btn-bulk-follow-upcoming').disabled = count === 0;
    $('#btn-bulk-ignore-upcoming').disabled = count === 0;
    $('#btn-bulk-delete-upcoming').disabled = count === 0;
}

async function bulkUpcomingStatus(status) {
    const ids = [...selectedUpcoming];
    const result = await apiPost(API.bulkUpcomingStatus, { ids, status });
    if (!result || !result.ok) return;

    ids.forEach((id) => {
        const match = upcomingMatches.find((item) => item.id === id);
        if (match) match.status = status;
    });
    renderUpcoming();
    updateOverview();
    toast(`${ids.length} maç güncellendi: ${upcomingStatusLabel(status)}`);
}

$('#select-all-upcoming').addEventListener('change', (event) => {
    const checked = event.target.checked;
    $$('.chk-upcoming').forEach((checkbox) => {
        checkbox.checked = checked;
        const id = Number(checkbox.dataset.id);
        if (checked) selectedUpcoming.add(id);
        else selectedUpcoming.delete(id);
    });
    updateUpcomingBulk();
});

$('#btn-bulk-follow-upcoming').addEventListener('click', () => bulkUpcomingStatus('following'));
$('#btn-bulk-ignore-upcoming').addEventListener('click', () => bulkUpcomingStatus('ignored'));

$('#btn-bulk-delete-upcoming').addEventListener('click', async () => {
    if (!confirm(`${selectedUpcoming.size} maç kaydı silinsin mi?`)) return;
    const ids = [...selectedUpcoming];
    const result = await apiPost(API.deleteUpcoming, { ids });
    if (!result || !result.ok) return;
    toast(`${ids.length} maç silindi`);
    await loadUpcoming();
});

$('#btn-clear-all-upcoming').addEventListener('click', async () => {
    if (!confirm('24 saatlik maç kayıtları silinsin mi?')) return;
    const ok = await clearAllWithFallback({
        clearUrl: API.clearUpcoming,
        deleteUrl: API.deleteUpcoming,
        ids: upcomingMatches.map((item) => item.id),
        emptyText: 'Silinecek maç kaydı bulunamadı',
        successText: '24 saatlik maç kayıtları silindi',
    });
    if (ok) await loadUpcoming();
});

$('#filter-upcoming-status').addEventListener('change', loadUpcoming);
$('#btn-refresh-upcoming').addEventListener('click', loadUpcoming);
$('#search-upcoming').addEventListener('input', renderUpcoming);
$('#btn-copy-upcoming').addEventListener('click', copyUpcomingMatches);

$('#btn-trigger-upcoming').addEventListener('click', async () => {
    const button = $('#btn-trigger-upcoming');
    setButtonBusy(button, 'Çekiliyor...', 'Gelecek Maçları Çek', true);
    try {
        const result = await apiPost(API.triggerUpcoming, {});
        if (result && result.ok) {
            await loadUpcoming();
            toast(`Gelecek 24 saat için ${result.count} maç güncellendi`);
        }
    } finally {
        setButtonBusy(button, 'Çekiliyor...', 'Gelecek Maçları Çek', false);
    }
});

async function ensureAnomalyDetails(eventId) {
    if (anomalyDetailsCache.has(eventId)) return anomalyDetailsCache.get(eventId);
    if (anomalyDetailsInFlight.has(eventId)) return anomalyDetailsInFlight.get(eventId);

    const promise = (async () => {
        const data = await apiFetch(API.anomalyDetails(eventId));
        if (data) anomalyDetailsCache.set(eventId, data);
        anomalyDetailsInFlight.delete(eventId);
        return data;
    })();
    anomalyDetailsInFlight.set(eventId, promise);
    return promise;
}

function buildMatchDetailsHtml(match, data) {
    const stats = data.stats || {};
    const form = data.form || { home: {}, away: {} };
    const votes = data.votes || {};
    const odds = data.odds || {};

    const rows = [
        { label: 'Topa Sahip Olma', home: stats.possession_home, away: stats.possession_away, unit: '%' },
        { label: 'Beklenen Gol (xG)', home: stats.expected_goals_home, away: stats.expected_goals_away, decimals: 2 },
        { label: 'Toplam Şut', home: stats.total_shots_home, away: stats.total_shots_away },
        { label: 'İsabetli Şut', home: stats.shots_on_target_home, away: stats.shots_on_target_away },
        { label: 'Kaçan Şut', home: stats.shots_off_target_home, away: stats.shots_off_target_away },
        { label: 'Bloklanmış Şut', home: stats.blocked_shots_home, away: stats.blocked_shots_away },
        { label: 'Büyük Şans', home: stats.big_chances_home, away: stats.big_chances_away },
        { label: 'Korner', home: stats.corner_kicks_home, away: stats.corner_kicks_away },
        { label: 'Pas İsabeti', home: statPercent(stats.pass_accuracy_home, passAccuracy(stats.accurate_passes_home, stats.total_passes_home)), away: statPercent(stats.pass_accuracy_away, passAccuracy(stats.accurate_passes_away, stats.total_passes_away)), unit: '%' },
        { label: 'Ofsayt', home: stats.offsides_home, away: stats.offsides_away },
        { label: 'Faul', home: stats.fouls_home, away: stats.fouls_away },
        { label: 'Sarı Kart', home: stats.yellow_cards_home, away: stats.yellow_cards_away },
        { label: 'Kırmızı Kart', home: stats.red_cards_home, away: stats.red_cards_away },
    ];

    const visibleRows = rows.filter((r) => {
        const home = Number(r.home) || 0;
        const away = Number(r.away) || 0;
        return home > 0 || away > 0;
    });

    const statsHtml = visibleRows.map((r) => {
        const home = Number(r.home) || 0;
        const away = Number(r.away) || 0;
        const total = home + away;
        const hPct = total > 0 ? (home * 100) / total : 50;
        const aPct = total > 0 ? (away * 100) / total : 50;
        const suffix = r.unit || '';
        const displayH = `${formatNumber(home, r.decimals)}${suffix}`;
        const displayA = `${formatNumber(away, r.decimals)}${suffix}`;

        return `
            <div class="stat-row">
                <div class="stat-label">${r.label}</div>
                <div class="stat-bars">
                    <span class="stat-value stat-value-home">${displayH}</span>
                    <div class="stat-bar">
                        <div class="stat-bar-home" style="width:${hPct.toFixed(1)}%"></div>
                        <div class="stat-bar-away" style="width:${aPct.toFixed(1)}%"></div>
                    </div>
                    <span class="stat-value stat-value-away">${displayA}</span>
                </div>
            </div>`;
    }).join('');

    const formHtml = renderFormBlock(match, form);
    const expectationHtml = renderExpectationBlock(match, votes, odds);

    return `
        <div class="anomaly-details-grid">
            <div class="anomaly-details-col anomaly-details-col-stats">
                <h3 class="anomaly-details-title">Maç İstatistikleri</h3>
                ${statsHtml || '<div class="anomaly-details-empty">İstatistik verisi henüz yok</div>'}
            </div>
            <div class="anomaly-details-col">
                <h3 class="anomaly-details-title">Form Durumu</h3>
                ${formHtml}
                <h3 class="anomaly-details-title" style="margin-top:18px;">Beklenti</h3>
                ${expectationHtml}
            </div>
        </div>`;
}

async function toggleAnomalyDetails(anomalyId) {
    const detailsRow = document.querySelector(`[data-aid-details="${anomalyId}"]`);
    const mainRow = document.querySelector(`.anomaly-row-main[data-id="${anomalyId}"]`);
    if (!detailsRow) return;

    const detailsBtn = mainRow ? mainRow.querySelector('.icon-btn-details') : null;

    if (expandedAnomalyRows.has(anomalyId)) {
        expandedAnomalyRows.delete(anomalyId);
        detailsRow.style.display = 'none';
        if (mainRow) mainRow.classList.remove('expanded');
        if (detailsBtn) detailsBtn.classList.remove('active');
        return;
    }

    expandedAnomalyRows.add(anomalyId);
    detailsRow.style.display = '';
    if (mainRow) mainRow.classList.add('expanded');
    if (detailsBtn) detailsBtn.classList.add('active');
    renderAnomalyDetails(anomalyId);

    const anomaly = anomalies.find((item) => item.id === anomalyId);
    if (!anomaly) return;
    await ensureAnomalyDetails(String(anomaly.match_id));
    renderAnomalyDetails(anomalyId);
}

function renderAnomalyDetails(anomalyId) {
    const container = document.querySelector(
        `.anomaly-row-details[data-aid-details="${anomalyId}"] .anomaly-details`
    );
    if (!container) return;

    const anomaly = anomalies.find((item) => item.id === anomalyId);
    if (!anomaly) {
        container.innerHTML = '<div class="anomaly-details-empty">Anomali bulunamadı</div>';
        return;
    }

    const data = anomalyDetailsCache.get(String(anomaly.match_id));
    if (!data) {
        container.innerHTML = '<div class="anomaly-details-loading">Detaylar yükleniyor...</div>';
        return;
    }

    container.innerHTML = buildMatchDetailsHtml(anomaly, data);
}

function renderFormBlock(match, form) {
    const home = form.home || {};
    const away = form.away || {};
    if (!home.form && !away.form) {
        return '<div class="anomaly-details-empty">Form verisi bulunamadı</div>';
    }

    const renderChips = (list) => {
        if (!list || !list.length) return '<span class="form-empty">-</span>';
        return list.map((ch) => {
            const letter = String(ch).toUpperCase()[0] || '-';
            const cls = letter === 'W' ? 'form-win' : letter === 'L' ? 'form-loss' : 'form-draw';
            return `<span class="form-chip ${cls}">${letter}</span>`;
        }).join('');
    };

    const homePos = home.position != null ? `#${home.position}` : '-';
    const awayPos = away.position != null ? `#${away.position}` : '-';
    const homeRating = home.avg_rating != null ? formatNumber(home.avg_rating) : (home.value || '-');
    const awayRating = away.avg_rating != null ? formatNumber(away.avg_rating) : (away.value || '-');

    return `
        <div class="form-block">
            <div class="form-team">
                <div class="form-team-name">${escHtml(match.home_team || 'Ev')}</div>
                <div class="form-chips">${renderChips(home.form)}</div>
                <div class="form-meta">Sıralama: <strong>${homePos}</strong> • Puan: <strong>${homeRating}</strong></div>
            </div>
            <div class="form-team">
                <div class="form-team-name">${escHtml(match.away_team || 'Dep')}</div>
                <div class="form-chips">${renderChips(away.form)}</div>
                <div class="form-meta">Sıralama: <strong>${awayPos}</strong> • Puan: <strong>${awayRating}</strong></div>
            </div>
        </div>`;
}

function renderExpectationBlock(match, votes, odds) {
    const hasOdds = odds && (odds.home || odds.draw || odds.away);
    const hasVotes = votes && (votes.home_pct || votes.draw_pct || votes.away_pct);

    if (!hasOdds && !hasVotes) {
        return '<div class="anomaly-details-empty">Beklenti verisi bulunamadı</div>';
    }

    const oddsHtml = hasOdds ? `
        <div class="expectation-row">
            <span class="expectation-label">Oranlar</span>
            <div class="expectation-cells">
                <span class="exp-cell"><strong>1</strong> ${odds.home ?? '-'}</span>
                <span class="exp-cell"><strong>X</strong> ${odds.draw ?? '-'}</span>
                <span class="exp-cell"><strong>2</strong> ${odds.away ?? '-'}</span>
            </div>
        </div>` : '';

    const votesHtml = hasVotes ? `
        <div class="expectation-row">
            <span class="expectation-label">Taraftar (${votes.total || 0} oy)</span>
            <div class="expectation-bar">
                <div class="expectation-bar-home" style="width:${votes.home_pct || 0}%" title="${match.home_team}: ${votes.home_pct || 0}%"></div>
                <div class="expectation-bar-draw" style="width:${votes.draw_pct || 0}%" title="Beraberlik: ${votes.draw_pct || 0}%"></div>
                <div class="expectation-bar-away" style="width:${votes.away_pct || 0}%" title="${match.away_team}: ${votes.away_pct || 0}%"></div>
            </div>
            <div class="expectation-legend">
                <span>${escHtml(match.home_team || '')} ${votes.home_pct || 0}%</span>
                <span>Beraberlik ${votes.draw_pct || 0}%</span>
                <span>${escHtml(match.away_team || '')} ${votes.away_pct || 0}%</span>
            </div>
        </div>` : '';

    return oddsHtml + votesHtml;
}

function statPercent(primary, fallback) {
    const value = Number(primary);
    if (Number.isFinite(value) && value > 0) return value;
    return fallback;
}

function passAccuracy(accurate, total) {
    const acc = Number(accurate) || 0;
    const ttl = Number(total) || 0;
    if (ttl <= 0) return 0;
    return (acc * 100) / ttl;
}

function formatNumber(value, decimals = null) {
    if (value == null || value === '') return '-';
    const num = Number(value);
    if (!Number.isFinite(num)) return String(value);
    if (decimals != null) return num.toFixed(decimals);
    if (Math.abs(num) >= 10 || Number.isInteger(num)) return String(Math.round(num));
    return num.toFixed(1);
}

async function loadDeletedAnomalies() {
    const resultFilter = ($('#filter-deleted-result') || {}).value || '';
    const hideUnique = Boolean(($('#filter-deleted-hide-unique') || {}).checked);
    const data = await apiFetch(API.deletedAnomalies(resultFilter, hideUnique));
    if (!data) return;

    // Accept the old array response during rolling deployments.
    deletedAnomalies = Array.isArray(data) ? data : (data.items || []);
    if (!Array.isArray(data)) deletedSummary = data.summary || deletedSummary;
    renderDeletedSummary();
    renderDeletedAnomalies();
    touchLastUpdated();
}

function renderDeletedSummary() {
    setText('#deleted-summary-total', String(deletedSummary.total || 0));
    setText('#deleted-summary-evaluated', String(deletedSummary.evaluated || 0));
    setText('#deleted-summary-successful', String(deletedSummary.successful || 0));
    setText('#deleted-summary-failed', String(deletedSummary.failed || 0));
    setText('#deleted-summary-pending', String(deletedSummary.pending || 0));
    setText('#deleted-summary-unresolved', String(deletedSummary.unresolved || 0));
    const rate = Number(deletedSummary.success_rate || 0).toLocaleString('tr-TR', {
        maximumFractionDigits: 1,
    });
    setText('#deleted-summary-rate', `%${rate}`);
    setText('#deleted-summary-matches', String(deletedSummary.finished_matches || 0));
}

function deletedResultLabel(status) {
    const labels = {
        successful: 'Başarılı',
        failed: 'Başarısız',
        pending: 'Sonuç bekliyor',
        unresolved: 'Değerlendirilemedi',
    };
    return labels[status] || 'Sonuç bekliyor';
}

function renderDeletedAnomalies() {
    const tbody = $('#deleted-body');
    const selectAll = $('#select-all-deleted');
    selectedDeleted.clear();
    if (selectAll) selectAll.checked = false;
    updateDeletedBulk();

    const searchQuery = ($('#search-deleted') || {}).value || '';
    const filtered = filterBySearch(deletedAnomalies, searchQuery, (item) =>
        `${item.home_team} ${item.away_team} ${item.league} ${item.condition_type}`
    );

    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-msg">Bu filtrede kayıt yok</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map((item) => {
        const conditionBadge = item.condition_type === 'A'
            ? '<span class="badge badge-a">A / Beraberlik</span>'
            : '<span class="badge badge-b">B / 1 Fark</span>';
        const completedTime = item.finished_at
            ? formatTurkeyTimestamp(item.finished_at, true)
            : formatCreatedAt(item.deleted_at, true);
        const finalScore = item.final_score_home == null || item.final_score_away == null
            ? '-'
            : `${item.final_score_home} - ${item.final_score_away}`;
        const dominantTeam = item.dominant_side === 'home'
            ? item.home_team
            : (item.dominant_side === 'away' ? item.away_team : 'Belirlenemedi');
        const resultStatus = item.result_status || 'pending';
        const resultBadge = `<span class="result-badge result-${resultStatus}">${deletedResultLabel(resultStatus)}</span>`;
        const restoreButton = resultStatus === 'pending'
            ? `<button class="icon-btn icon-btn-restore" onclick="restoreDeletedRow(${item.id})" title="Geri yükle" aria-label="Geri yükle">${ICONS.restore}</button>`
            : '';

        return `
        <tr data-id="${item.id}">
            <td class="col-check"><input type="checkbox" class="chk-deleted" data-id="${item.id}"></td>
            <td>
                <div class="cell-stack">
                    <a class="match-link" href="${sofascoreEventUrl(item.match_id)}" target="_blank" rel="noopener noreferrer">
                        ${escHtml(item.home_team)} vs ${escHtml(item.away_team)}
                    </a>
                    <span class="cell-subtle">Maç ID: ${escHtml(item.match_id)}</span>
                </div>
            </td>
            <td>
                <div class="cell-stack">
                    <span class="score-pill">${item.score_home} - ${item.score_away}</span>
                    <span class="cell-subtle">${item.minute}'. dakika</span>
                </div>
            </td>
            <td><span class="score-pill score-pill-final">${finalScore}</span></td>
            <td><span class="dominant-team">${escHtml(dominantTeam)}</span></td>
            <td>${resultBadge}</td>
            <td>${escHtml(item.league || '-')}</td>
            <td>${conditionBadge}</td>
            <td><span class="time-pill">${completedTime}</span></td>
            <td>
                <div class="row-actions row-actions-icons">
                    ${restoreButton}
                    <button class="icon-btn icon-btn-purge" onclick="purgeDeletedRow(${item.id})" title="Kalıcı sil" aria-label="Kalıcı sil">${ICONS.purge}</button>
                </div>
            </td>
        </tr>`;
    }).join('');

    $$('.chk-deleted').forEach((checkbox) => {
        checkbox.addEventListener('change', () => {
            const id = Number(checkbox.dataset.id);
            if (checkbox.checked) selectedDeleted.add(id);
            else selectedDeleted.delete(id);
            updateDeletedBulk();
        });
    });
}

function updateDeletedBulk() {
    const count = selectedDeleted.size;
    const restorableCount = [...selectedDeleted].filter((id) => {
        const item = deletedAnomalies.find((row) => row.id === id);
        return item && (item.result_status || 'pending') === 'pending';
    }).length;
    $('#selected-count-deleted').textContent = `${count} seçili`;
    $('#btn-bulk-restore-deleted').disabled = restorableCount === 0;
    $('#btn-bulk-purge-deleted').disabled = count === 0;
}

async function restoreDeletedRow(id) {
    const result = await apiPost(API.restoreAnomalies, { ids: [id] });
    if (!result || !result.ok) return;
    toast(result.restored ? 'Kayıt geri yüklendi' : 'Tamamlanmış maçlar geri yüklenemez');
    await Promise.all([loadDeletedAnomalies(), loadAnomalies()]);
}

async function purgeDeletedRow(id) {
    if (!confirm('Bu kayıt veritabanından kalıcı olarak silinsin mi?')) return;
    const result = await apiPost(API.purgeAnomalies, { ids: [id] });
    if (!result || !result.ok) return;
    toast('Kayıt kalıcı olarak silindi');
    await loadDeletedAnomalies();
}

$('#select-all-deleted').addEventListener('change', (event) => {
    const checked = event.target.checked;
    $$('.chk-deleted').forEach((checkbox) => {
        checkbox.checked = checked;
        const id = Number(checkbox.dataset.id);
        if (checked) selectedDeleted.add(id);
        else selectedDeleted.delete(id);
    });
    updateDeletedBulk();
});

$('#btn-refresh-deleted').addEventListener('click', loadDeletedAnomalies);
$('#filter-deleted-result').addEventListener('change', loadDeletedAnomalies);
$('#filter-deleted-hide-unique').addEventListener('change', loadDeletedAnomalies);
$('#search-deleted').addEventListener('input', renderDeletedAnomalies);

async function triggerFinishedCheck() {
    const buttons = [
        { element: $('#btn-trigger-finished-main'), label: 'Bitenleri Kontrol Et' },
        { element: $('#btn-trigger-finished'), label: 'Sonuçları Kontrol Et' },
    ];
    buttons.forEach(({ element, label }) => {
        setButtonBusy(element, 'Kontrol ediliyor...', label, true);
    });
    try {
        const result = await apiPost(API.triggerFinished, {});
        if (result && result.ok) {
            await Promise.all([loadDeletedAnomalies(), loadAnomalies()]);
            toast(`${result.checked} maç kontrol edildi, ${result.archived} sinyal arşivlendi`);
        }
    } finally {
        buttons.forEach(({ element, label }) => {
            setButtonBusy(element, 'Kontrol ediliyor...', label, false);
        });
    }
}

$('#btn-trigger-finished-main').addEventListener('click', triggerFinishedCheck);
$('#btn-trigger-finished').addEventListener('click', triggerFinishedCheck);

$('#btn-bulk-restore-deleted').addEventListener('click', async () => {
    const ids = [...selectedDeleted].filter((id) => {
        const item = deletedAnomalies.find((row) => row.id === id);
        return item && (item.result_status || 'pending') === 'pending';
    });
    const result = await apiPost(API.restoreAnomalies, { ids });
    if (!result || !result.ok) return;
    toast(`${result.restored || 0} kayıt geri yüklendi`);
    await Promise.all([loadDeletedAnomalies(), loadAnomalies()]);
});

$('#btn-bulk-purge-deleted').addEventListener('click', async () => {
    if (!confirm(`${selectedDeleted.size} kayıt veritabanından kalıcı olarak silinsin mi?`)) return;
    const ids = [...selectedDeleted];
    const result = await apiPost(API.purgeAnomalies, { ids });
    if (!result || !result.ok) return;
    toast(`${ids.length} kayıt kalıcı olarak silindi`);
    await loadDeletedAnomalies();
});

$('#btn-purge-all-deleted').addEventListener('click', async () => {
    if (!confirm('Çöpteki tüm kayıtlar kalıcı olarak silinsin mi? Bu işlem geri alınamaz.')) return;
    const result = await apiPost(API.purgeAllAnomalies, {});
    if (!result || !result.ok) return;
    toast('Çöp boşaltıldı');
    await loadDeletedAnomalies();
});

$('#btn-refresh-all').addEventListener('click', async () => {
    const button = $('#btn-refresh-all');
    setButtonBusy(button, 'Yenileniyor...', 'Tümünü Yenile', true);
    await refreshAllData();
    toast('Dashboard yenilendi');
    setButtonBusy(button, 'Yenileniyor...', 'Tümünü Yenile', false);
});

$('#btn-clear-database').addEventListener('click', async () => {
    if (!confirm('Tüm anomali ve 24 saatlik maç kayıtları temizlensin mi?')) return;

    const button = $('#btn-clear-database');
    setButtonBusy(button, 'Temizleniyor...', 'VERİTABANINI TEMİZLE', true);

    const result = await apiPost(API.clearDatabase, {});
    if (result && result.ok) {
        anomalies = [];
        upcomingMatches = [];
        deletedAnomalies = [];
        deletedSummary = {
            total: 0, successful: 0, failed: 0, pending: 0, unresolved: 0,
            evaluated: 0, finished_matches: 0, success_rate: 0,
        };
        renderAnomalies();
        renderUpcoming();
        renderDeletedAnomalies();
        renderDeletedSummary();
        updateOverview();
        touchLastUpdated();
        toast('Veritabanı temizlendi');
    }

    setButtonBusy(button, 'Temizleniyor...', 'VERİTABANINI TEMİZLE', false);
});

function escHtml(value) {
    if (!value) return '';
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function formatStartTime(startTime) {
    const timestamp = Number(startTime);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return '-';
    return new Date(timestamp * 1000).toLocaleString('tr-TR', {
        timeZone: 'Europe/Istanbul',
        day: '2-digit',
        month: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
    });
}

function formatCreatedAt(value, includeDate = false) {
    if (!value) return '-';
    const date = new Date(`${value}Z`);
    return date.toLocaleString('tr-TR', {
        timeZone: 'Europe/Istanbul',
        day: includeDate ? '2-digit' : undefined,
        month: includeDate ? '2-digit' : undefined,
        year: includeDate ? 'numeric' : undefined,
        hour: '2-digit',
        minute: '2-digit',
    });
}

function formatTurkeyTimestamp(value, includeDate = false) {
    if (!value) return '-';
    const normalized = String(value).trim().replace('T', ' ');
    const match = normalized.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/);
    if (!match) return value;

    const [, year, month, day, hour, minute] = match;
    return includeDate
        ? `${day}.${month}.${year} ${hour}:${minute}`
        : `${hour}:${minute}`;
}

function statusLabel(status) {
    const labels = {
        new: 'Yeni',
        bet_placed: 'Bahis oynandı',
        ignored: 'Gözardı edildi',
        following: 'Takip ediliyor',
    };
    return labels[status] || status || 'Yeni';
}

async function refreshAllData() {
    await Promise.all([
        checkStatus(),
        loadAnomalies(),
        loadUpcoming(),
    ]);
}

(async () => {
    initSortableHeaders();
    await refreshAllData();

    setInterval(loadAnomalies, 60000);
    setInterval(checkStatus, 30000);
})();
