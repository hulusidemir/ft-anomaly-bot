/* ===== Football Anomaly Bot - Dashboard JS ===== */

const API = {
    anomalies: (status) => `/api/anomalies${status ? `?status=${status}` : ''}`,
    updateStatus: (id) => `/api/anomalies/${id}/status`,
    bulkStatus: '/api/anomalies/bulk-status',
    deleteAnomalies: '/api/anomalies/delete',
    clearAnomalies: '/api/anomalies/clear',
    deletedAnomalies: '/api/anomalies/deleted',
    restoreAnomalies: '/api/anomalies/restore',
    purgeAnomalies: '/api/anomalies/purge',
    purgeAllAnomalies: '/api/anomalies/purge-all',
    analyses: '/api/analyses',
    deleteAnalyses: '/api/analyses/delete',
    clearAnalyses: '/api/analyses/clear',
    liveMatches: '/api/live-matches',
    liveMatches2: '/api/live-matches-2',
    liveMatch2Stats: (id) => `/api/live-matches-2/${encodeURIComponent(id)}/stats`,
    liveMatchDetails: (id) => `/api/live-matches/${encodeURIComponent(id)}/details`,
    liveMatchStatus: (id) => `/api/live-matches/${encodeURIComponent(id)}/status`,
    liveMatchBulkStatus: '/api/live-matches/bulk-status',
    upcoming: (status) => `/api/upcoming${status ? `?status=${status}` : ''}`,
    updateUpcomingStatus: (id) => `/api/upcoming/${id}/status`,
    bulkUpcomingStatus: '/api/upcoming/bulk-status',
    deleteUpcoming: '/api/upcoming/delete',
    clearUpcoming: '/api/upcoming/clear',
    clearDatabase: '/api/database/clear',
    status: '/api/status',
    triggerLive: '/api/trigger/live-scan',
    triggerUpcoming: '/api/trigger/upcoming-scan',
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
let analyses = [];
let upcomingMatches = [];
let liveMatches = [];
let liveMatches2 = [];
let deletedAnomalies = [];
let schedulerJobs = [];
let live2StatsRun = 0;

const selectedAnomalies = new Set();
const selectedAnalyses = new Set();
const selectedUpcoming = new Set();
const selectedLive = new Set();
const selectedLive2 = new Set();
const selectedDeleted = new Set();

const liveDetailsCache = new Map();
const liveDetailsInFlight = new Map();
const expandedLiveRows = new Set();
const LIVE2_STATS_CONCURRENCY = 2;

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
        if (tab.dataset.tab === 'live' && !liveMatches.length) loadLiveMatches();
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

async function apiFetchQuiet(url, opts = {}) {
    try {
        const response = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...opts,
        });

        if (!response.ok) return null;
        return await response.json();
    } catch (_) {
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
    const flaggedUpcoming = upcomingMatches.filter((item) => item.has_anomaly).length;
    const latestAnalysis = analyses[0];

    setText('#metric-anomalies', String(anomalyTotal));
    setText('#metric-anomalies-detail', `${newAnomalies} yeni, ${followingAnomalies} takipte`);

    setText('#metric-following', String(followingAnomalies + followingUpcoming));
    setText('#metric-following-detail', `${followingAnomalies} canlı alarm, ${followingUpcoming} yaklaşan maç`);

    setText('#metric-analyses', String(analyses.length));
    setText('#metric-analyses-detail', latestAnalysis
        ? `Son rapor: ${runTypeLabel(latestAnalysis.run_type)}`
        : 'Henüz analiz kaydı yok');

    setText('#metric-upcoming', String(upcomingMatches.length));
    setText('#metric-upcoming-detail', `${flaggedUpcoming} anomali etiketi taşıyan maç`);

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
            if (tableId === 'live-table') renderLiveMatches();
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
            case 'alert':
                return item.alert_number || 1;
            case 'time':
                return item.created_at || '';
            default:
                return '';
        }
    });

    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-msg">Anomali bulunamadı</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map((item) => {
        const rules = item.triggered_rules || [];
        const ruleHtml = rules.map((rule) => `<li>${escHtml(rule)}</li>`).join('');
        const stateClass = item.status !== 'new' ? `state-${item.status}` : '';
        const time = formatCreatedAt(item.created_at);
        const conditionBadge = item.condition_type === 'A'
            ? '<span class="badge badge-a">A / Beraberlik</span>'
            : '<span class="badge badge-b">B / 1 Fark</span>';
        const alertNumber = item.alert_number || 1;
        const alertBadge = alertNumber > 1
            ? `<span class="badge badge-alert badge-alert-multi">${alertNumber}. uyarı</span>`
            : '<span class="badge badge-alert">1. uyarı</span>';

        return `
        <tr class="${stateClass}" data-id="${item.id}">
            <td class="col-check"><input type="checkbox" class="chk-anomaly" data-id="${item.id}"></td>
            <td>
                <div class="cell-stack">
                    <a class="match-link" href="${sofascoreEventUrl(item.match_id)}" target="_blank" rel="noopener noreferrer">
                        ${escHtml(item.home_team)} vs ${escHtml(item.away_team)}
                    </a>
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
            <td>${alertBadge}</td>
            <td><span class="time-pill">${time}</span></td>
            <td>
                <div class="row-actions row-actions-icons">
                    <button class="icon-btn icon-btn-bet${item.status === 'bet_placed' ? ' active' : ''}" onclick="setStatus(${item.id}, 'bet_placed')" title="Bahis oynandı" aria-label="Bahis oynandı">${ICONS.bet}</button>
                    <button class="icon-btn icon-btn-ignore${item.status === 'ignored' ? ' active' : ''}" onclick="setStatus(${item.id}, 'ignored')" title="Gözardı et" aria-label="Gözardı et">${ICONS.ignore}</button>
                    <button class="icon-btn icon-btn-follow${item.status === 'following' ? ' active' : ''}" onclick="setStatus(${item.id}, 'following')" title="Takip et" aria-label="Takip et">${ICONS.follow}</button>
                    <button class="icon-btn icon-btn-delete" onclick="deleteAnomalyRow(${item.id})" title="Sil" aria-label="Sil">${ICONS.delete}</button>
                </div>
            </td>
        </tr>`;
    }).join('');

    $$('.chk-anomaly').forEach((checkbox) => {
        checkbox.addEventListener('change', () => {
            const id = Number(checkbox.dataset.id);
            if (checkbox.checked) selectedAnomalies.add(id);
            else selectedAnomalies.delete(id);
            updateBulkButtons();
        });
    });
}

async function setStatus(id, status) {
    const anomaly = anomalies.find((item) => item.id === id);
    if (!anomaly) return;

    const newStatus = anomaly.status === status ? 'new' : status;
    const siblings = anomalies.filter((item) => item.match_id === anomaly.match_id);
    const ids = siblings.map((item) => item.id);

    const result = ids.length > 1
        ? await apiPost(API.bulkStatus, { ids, status: newStatus })
        : await apiPost(API.updateStatus(id), { status: newStatus });
    if (!result || !result.ok) return;

    siblings.forEach((item) => { item.status = newStatus; });
    renderAnomalies();
    updateOverview();
    const extra = siblings.length > 1 ? ` (${siblings.length} sinyal)` : '';
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

    ids.forEach((id) => {
        const anomaly = anomalies.find((item) => item.id === id);
        if (anomaly) anomaly.status = status;
    });

    renderAnomalies();
    updateOverview();
    toast(`${ids.length} kayıt güncellendi: ${statusLabel(status)}`);
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

async function loadAnalyses() {
    const data = await apiFetch(API.analyses);
    if (!data) return;

    analyses = data;
    renderAnalyses();
    updateOverview();
    touchLastUpdated();
}

function renderAnalyses() {
    const container = $('#analyses-list');
    selectedAnalyses.clear();
    updateAnalysesBulk();

    const searchQuery = ($('#search-analyses') || {}).value || '';
    const filtered = filterBySearch(analyses, searchQuery, (item) => item.analysis_text || '');

    if (!filtered.length) {
        container.innerHTML = '<div class="empty-msg">Henüz analiz yok</div>';
        return;
    }

    container.innerHTML = filtered.map((item) => `
        <article class="analysis-card" data-id="${item.id}">
            <input type="checkbox" class="chk-analysis card-check" data-id="${item.id}">
            <div class="card-header">
                <span class="analysis-run">${runTypeLabel(item.run_type)}</span>
                <span class="card-meta">${item.match_count} maç analiz edildi • ${formatCreatedAt(item.created_at, true)}</span>
            </div>
            <div class="card-body">${escHtml(item.analysis_text)}</div>
        </article>
    `).join('');

    $$('.chk-analysis').forEach((checkbox) => {
        checkbox.addEventListener('change', () => {
            const id = Number(checkbox.dataset.id);
            if (checkbox.checked) selectedAnalyses.add(id);
            else selectedAnalyses.delete(id);
            updateAnalysesBulk();
        });
    });
}

function updateAnalysesBulk() {
    const count = selectedAnalyses.size;
    $('#selected-count-analyses').textContent = `${count} seçili`;
    $('#btn-delete-analyses').disabled = count === 0;
}

$('#btn-delete-analyses').addEventListener('click', async () => {
    if (!confirm(`${selectedAnalyses.size} analiz silinsin mi?`)) return;

    const ids = [...selectedAnalyses];
    const result = await apiPost(API.deleteAnalyses, { ids });
    if (!result || !result.ok) return;

    toast(`${ids.length} analiz silindi`);
    await loadAnalyses();
});

$('#btn-clear-all-analyses').addEventListener('click', async () => {
    if (!confirm('Tüm analiz geçmişi silinsin mi? Bu işlem geri alınamaz.')) return;

    const ok = await clearAllWithFallback({
        clearUrl: API.clearAnalyses,
        deleteUrl: API.deleteAnalyses,
        ids: analyses.map((item) => item.id),
        emptyText: 'Silinecek analiz bulunamadı',
        successText: 'Tüm analiz geçmişi silindi',
    });

    if (ok) await loadAnalyses();
});

$('#btn-refresh-analyses').addEventListener('click', loadAnalyses);
$('#search-analyses').addEventListener('input', renderAnalyses);

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

    filtered = sortData(filtered, 'upcoming-table', (item, key) => {
        switch (key) {
            case 'match':
                return `${item.home_team} ${item.away_team}`.toLowerCase();
            case 'start':
                return item.start_time || 0;
            case 'league':
                return (item.league || '').toLowerCase();
            case 'round':
                return (item.round_info || '').toLowerCase();
            case 'status':
                return item.has_anomaly ? '0' : '1';
            default:
                return '';
        }
    });

    return filtered;
}

function renderUpcoming() {
    const tbody = $('#upcoming-body');
    const selectAll = $('#select-all-upcoming');
    selectedUpcoming.clear();
    if (selectAll) selectAll.checked = false;
    updateUpcomingBulk();

    const filtered = getVisibleUpcomingMatches();

    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">Yaklaşan maç bulunamadı</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map((item) => {
        const stateClass = item.status !== 'new' ? `state-${item.status}` : '';
        const anomalyClass = item.has_anomaly ? 'anomaly-row' : '';
        const anomalyBadge = item.has_anomaly ? '<span class="badge badge-anomaly">Anomali etiketi</span>' : '';

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
            <td><span class="time-pill">${formatStartTime(item.start_time, item.scan_date)}</span></td>
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
        toast('Kopyalanacak yaklaşan maç bulunamadı', true);
        return;
    }

    const text = filtered.map((item) => {
        const matchName = `${item.home_team} vs ${item.away_team}`;
        return `${formatStartTime(item.start_time, item.scan_date)} - ${matchName}`;
    }).join('\n');

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
    if (!confirm('Tüm yaklaşan maç kayıtları silinsin mi? Bu işlem geri alınamaz.')) return;

    const ok = await clearAllWithFallback({
        clearUrl: API.clearUpcoming,
        deleteUrl: API.deleteUpcoming,
        ids: upcomingMatches.map((item) => item.id),
        emptyText: 'Silinecek maç kaydı bulunamadı',
        successText: 'Tüm maç kayıtları silindi',
    });

    if (ok) await loadUpcoming();
});

$('#filter-upcoming-status').addEventListener('change', loadUpcoming);
$('#btn-refresh-upcoming').addEventListener('click', loadUpcoming);
$('#search-upcoming').addEventListener('input', renderUpcoming);
$('#btn-copy-upcoming').addEventListener('click', copyUpcomingMatches);

$('#btn-trigger-upcoming').addEventListener('click', async () => {
    const button = $('#btn-trigger-upcoming');
    setButtonBusy(button, 'Çekiliyor...', 'Maçları Çek', true);

    const result = await apiPost(API.triggerUpcoming, {});
    if (result && result.ok) {
        toast('Yaklaşan maçlar çekiliyor, birkaç saniye sonra tablo güncellenecek');
        setTimeout(loadUpcoming, 8000);
    }

    setTimeout(() => {
        setButtonBusy(button, 'Çekiliyor...', 'Maçları Çek', false);
    }, 10000);
});

/* ===== Live Matches ===== */

async function loadLiveMatches() {
    const tbody = $('#live-body');
    const button = $('#btn-refresh-live');
    if (tbody) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">Canlı maçlar çekiliyor...</td></tr>';
    }
    setButtonBusy(button, 'Çekiliyor...', 'Güncel Canlı Maçları Çek', true);

    const data = await apiFetch(API.liveMatches);
    setButtonBusy(button, 'Çekiliyor...', 'Güncel Canlı Maçları Çek', false);

    if (!data) {
        if (tbody) {
            tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">Canlı maç listesi alınamadı. Tekrar deneyin.</td></tr>';
        }
        return;
    }

    liveMatches = data;
    renderLiveMatches();
    touchLastUpdated();
}

function getVisibleLiveMatches() {
    const filter = ($('#filter-live-status') || {}).value || '';
    const searchQuery = ($('#search-live') || {}).value || '';

    let filtered = liveMatches;
    if (filter) filtered = filtered.filter((item) => (item.status || 'new') === filter);

    filtered = filterBySearch(filtered, searchQuery, (item) =>
        `${item.home_team} ${item.away_team} ${item.league} ${item.score_home}-${item.score_away}`
    );

    filtered = sortData(filtered, 'live-table', (item, key) => {
        switch (key) {
            case 'match':
                return `${item.home_team} ${item.away_team}`.toLowerCase();
            case 'score':
                return item.score_home * 100 + item.score_away;
            case 'minute':
                return item.minute || 0;
            case 'league':
                return (item.league || '').toLowerCase();
            default:
                return '';
        }
    });

    return filtered;
}

function renderLiveMatches() {
    const tbody = $('#live-body');
    if (!tbody) return;
    const selectAll = $('#select-all-live');
    selectedLive.clear();
    if (selectAll) selectAll.checked = false;
    updateLiveBulk();

    const filtered = getVisibleLiveMatches();
    setText('#live-count', `${liveMatches.length} maç`);

    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="empty-msg">Canlı maç bulunamadı</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map((item) => buildLiveRowHtml(item)).join('');

    $$('.chk-live').forEach((checkbox) => {
        checkbox.addEventListener('change', () => {
            const eid = checkbox.dataset.eid;
            if (checkbox.checked) selectedLive.add(eid);
            else selectedLive.delete(eid);
            updateLiveBulk();
        });
    });

    $$('.live-row-main').forEach((row) => {
        row.addEventListener('click', (event) => {
            if (event.target.closest('button, input, .row-actions, .col-check')) return;
            const url = sofascoreEventUrl(row.dataset.eid);
            if (url && url !== '#') window.open(url, '_blank', 'noopener,noreferrer');
        });
    });

    // Re-expand any previously-open rows
    expandedLiveRows.forEach((eid) => renderLiveDetails(eid));
}

function buildLiveRowHtml(item) {
    const statusValue = item.status || 'new';
    const stateClass = statusValue !== 'new' ? `state-${statusValue}` : '';
    const expanded = expandedLiveRows.has(item.event_id) ? 'expanded' : '';
    const statusDesc = item.status_desc ? ` • ${escHtml(item.status_desc)}` : '';
    return `
        <tr class="live-row-main ${stateClass} ${expanded}" data-eid="${escHtml(item.event_id)}" title="Sofascore sayfasını aç">
            <td class="col-check"><input type="checkbox" class="chk-live" data-eid="${escHtml(item.event_id)}"></td>
            <td>
                <div class="cell-stack">
                    <span class="match-link">${escHtml(item.home_team)} vs ${escHtml(item.away_team)}</span>
                    <span class="cell-subtle">Etkinlik ID: ${escHtml(item.event_id)}${statusDesc}</span>
                </div>
            </td>
            <td><span class="score-pill">${item.score_home} - ${item.score_away}</span></td>
            <td><span class="table-tag live-minute">${item.minute || 0}'</span></td>
            <td>${escHtml(item.league || '-')}</td>
            <td>
                <div class="row-actions row-actions-icons">
                    <button class="icon-btn icon-btn-details${expandedLiveRows.has(item.event_id) ? ' active' : ''}" onclick="toggleLiveDetails('${escAttr(item.event_id)}')" title="Detayları gör" aria-label="Detayları gör">${ICONS.details}</button>
                    <button class="icon-btn icon-btn-bet${statusValue === 'bet_placed' ? ' active' : ''}" onclick="setLiveStatus('${escAttr(item.event_id)}', 'bet_placed')" title="Bahis oynandı" aria-label="Bahis oynandı">${ICONS.bet}</button>
                    <button class="icon-btn icon-btn-ignore${statusValue === 'ignored' ? ' active' : ''}" onclick="setLiveStatus('${escAttr(item.event_id)}', 'ignored')" title="Gözardı et" aria-label="Gözardı et">${ICONS.ignore}</button>
                    <button class="icon-btn icon-btn-follow${statusValue === 'following' ? ' active' : ''}" onclick="setLiveStatus('${escAttr(item.event_id)}', 'following')" title="Takip et" aria-label="Takip et">${ICONS.follow}</button>
                </div>
            </td>
        </tr>
        <tr class="live-row-details" data-eid-details="${escHtml(item.event_id)}" style="${expandedLiveRows.has(item.event_id) ? '' : 'display:none;'}">
            <td colspan="6">
                <div class="live-details">Detaylar yükleniyor...</div>
            </td>
        </tr>`;
}

function escAttr(value) {
    return String(value || '').replace(/'/g, "\\'");
}

async function toggleLiveDetails(eventId) {
    const detailsRow = document.querySelector(`[data-eid-details="${CSS.escape(eventId)}"]`);
    const mainRow = document.querySelector(`.live-row-main[data-eid="${CSS.escape(eventId)}"]`);
    if (!detailsRow) return;

    const detailsBtn = mainRow ? mainRow.querySelector('.icon-btn-details') : null;

    if (expandedLiveRows.has(eventId)) {
        expandedLiveRows.delete(eventId);
        detailsRow.style.display = 'none';
        if (mainRow) mainRow.classList.remove('expanded');
        if (detailsBtn) detailsBtn.classList.remove('active');
        return;
    }

    expandedLiveRows.add(eventId);
    detailsRow.style.display = '';
    if (mainRow) mainRow.classList.add('expanded');
    if (detailsBtn) detailsBtn.classList.add('active');
    renderLiveDetails(eventId);
    await ensureLiveDetails(eventId);
    renderLiveDetails(eventId);
}

async function ensureLiveDetails(eventId) {
    if (liveDetailsCache.has(eventId)) return liveDetailsCache.get(eventId);
    if (liveDetailsInFlight.has(eventId)) return liveDetailsInFlight.get(eventId);

    const promise = (async () => {
        const data = await apiFetch(API.liveMatchDetails(eventId));
        if (data) liveDetailsCache.set(eventId, data);
        liveDetailsInFlight.delete(eventId);
        return data;
    })();
    liveDetailsInFlight.set(eventId, promise);
    return promise;
}

function renderLiveDetails(eventId) {
    const container = document.querySelector(
        `.live-row-details[data-eid-details="${CSS.escape(eventId)}"] .live-details`
    );
    if (!container) return;

    const data = liveDetailsCache.get(eventId);
    if (!data) {
        container.innerHTML = '<div class="live-details-loading">Detaylar yükleniyor...</div>';
        return;
    }

    const match = liveMatches.find((m) => m.event_id === eventId) || {};
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

    container.innerHTML = `
        <div class="live-details-grid">
            <div class="live-details-col live-details-col-stats">
                <h3 class="live-details-title">Maç İstatistikleri</h3>
                ${statsHtml || '<div class="live-details-empty">İstatistik verisi henüz yok</div>'}
            </div>
            <div class="live-details-col">
                <h3 class="live-details-title">Form Durumu</h3>
                ${formHtml}
                <h3 class="live-details-title" style="margin-top:18px;">Beklenti</h3>
                ${expectationHtml}
            </div>
        </div>`;
}

function renderFormBlock(match, form) {
    const home = form.home || {};
    const away = form.away || {};
    if (!home.form && !away.form) {
        return '<div class="live-details-empty">Form verisi bulunamadı</div>';
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
        return '<div class="live-details-empty">Beklenti verisi bulunamadı</div>';
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

function updateLiveBulk() {
    const count = selectedLive.size;
    setText('#selected-count-live', `${count} seçili`);
    const betBtn = $('#btn-bulk-bet-live');
    const ignBtn = $('#btn-bulk-ignore-live');
    const folBtn = $('#btn-bulk-follow-live');
    if (betBtn) betBtn.disabled = count === 0;
    if (ignBtn) ignBtn.disabled = count === 0;
    if (folBtn) folBtn.disabled = count === 0;
}

async function setLiveStatus(eventId, status) {
    const match = liveMatches.find((m) => m.event_id === eventId);
    if (!match) return;
    const newStatus = match.status === status ? 'new' : status;

    const result = await apiPost(API.liveMatchStatus(eventId), { status: newStatus });
    if (!result || !result.ok) return;

    match.status = newStatus;
    renderLiveMatches();
    toast(`Durum güncellendi: ${statusLabel(newStatus)}`);
}

async function bulkLiveStatus(status) {
    const eventIds = [...selectedLive];
    if (!eventIds.length) return;

    const result = await apiPost(API.liveMatchBulkStatus, { event_ids: eventIds, status });
    if (!result || !result.ok) return;

    eventIds.forEach((eid) => {
        const match = liveMatches.find((m) => m.event_id === eid);
        if (match) match.status = status;
    });

    renderLiveMatches();
    toast(`${eventIds.length} maç güncellendi: ${statusLabel(status)}`);
}

const selectAllLive = $('#select-all-live');
if (selectAllLive) {
    selectAllLive.addEventListener('change', (event) => {
        const checked = event.target.checked;
        $$('.chk-live').forEach((checkbox) => {
            checkbox.checked = checked;
            const eid = checkbox.dataset.eid;
            if (checked) selectedLive.add(eid);
            else selectedLive.delete(eid);
        });
        updateLiveBulk();
    });
}

const btnRefreshLive = $('#btn-refresh-live');
if (btnRefreshLive) btnRefreshLive.addEventListener('click', loadLiveMatches);

const filterLive = $('#filter-live-status');
if (filterLive) filterLive.addEventListener('change', renderLiveMatches);

const searchLive = $('#search-live');
if (searchLive) searchLive.addEventListener('input', renderLiveMatches);

const btnBulkBetLive = $('#btn-bulk-bet-live');
if (btnBulkBetLive) btnBulkBetLive.addEventListener('click', () => bulkLiveStatus('bet_placed'));
const btnBulkIgnoreLive = $('#btn-bulk-ignore-live');
if (btnBulkIgnoreLive) btnBulkIgnoreLive.addEventListener('click', () => bulkLiveStatus('ignored'));
const btnBulkFollowLive = $('#btn-bulk-follow-live');
if (btnBulkFollowLive) btnBulkFollowLive.addEventListener('click', () => bulkLiveStatus('following'));

/* ===== Live Matches 2 ===== */

async function loadLiveMatches2() {
    const list = $('#live2-list');
    const button = $('#btn-fetch-live2');
    const runId = live2StatsRun + 1;
    live2StatsRun = runId;
    if (list) {
        list.innerHTML = '<div class="empty-msg">Canlı maçlar çekiliyor...</div>';
    }
    setButtonBusy(button, 'Çekiliyor...', 'Canlı Maçları Çek', true);

    const data = await apiFetch(API.liveMatches2);
    setButtonBusy(button, 'Çekiliyor...', 'Canlı Maçları Çek', false);

    if (!data) {
        liveMatches2 = [];
        renderLive2Matches('Canlı maç listesi alınamadı. Tekrar deneyin.');
        return;
    }

    liveMatches2 = (Array.isArray(data) ? data : []).map((item) => ({
        ...item,
        stats: null,
        statsLoading: true,
        statsError: '',
    }));
    renderLive2Matches();
    touchLastUpdated();
    if (liveMatches2.length) {
        toast(`${liveMatches2.length} canlı maç listelendi, istatistikler çekiliyor`);
        loadLive2StatsProgressively(runId);
    }
}

function getVisibleLive2Matches() {
    const filter = ($('#filter-live2-status') || {}).value || '';
    const searchQuery = ($('#search-live2') || {}).value || '';

    let filtered = liveMatches2;
    if (filter) filtered = filtered.filter((item) => (item.status || 'new') === filter);

    filtered = filterBySearch(filtered, searchQuery, (item) =>
        `${item.home_team} ${item.away_team} ${item.league} ${item.score_home}-${item.score_away} ${item.status_desc || ''}`
    );

    return filtered;
}

function renderLive2Matches(emptyText = 'Canlı maç bulunamadı') {
    const list = $('#live2-list');
    if (!list) return;
    const selectAll = $('#select-all-live2');
    selectedLive2.clear();
    if (selectAll) selectAll.checked = false;
    updateLive2Bulk();

    const filtered = getVisibleLive2Matches();
    setText('#live2-count', `${liveMatches2.length} maç`);

    if (!filtered.length) {
        list.innerHTML = `<div class="empty-msg">${escHtml(emptyText)}</div>`;
        return;
    }

    list.innerHTML = filtered.map((item) => buildLive2CardHtml(item)).join('');
    $$('.chk-live2').forEach((checkbox) => {
        checkbox.addEventListener('change', () => {
            const eid = checkbox.dataset.eid;
            if (checkbox.checked) selectedLive2.add(eid);
            else selectedLive2.delete(eid);
            updateLive2Bulk();
        });
    });
}

function buildLive2CardHtml(item) {
    const statusValue = item.status || 'new';
    const stateClass = statusValue !== 'new' ? `state-${statusValue}` : '';
    const statusDesc = item.status_desc ? escHtml(item.status_desc) : 'Canlı';

    return `
        <article class="live2-card ${stateClass}" data-eid="${escHtml(item.event_id)}">
            <div class="live2-card-head">
                <label class="live2-check">
                    <input type="checkbox" class="chk-live2" data-eid="${escHtml(item.event_id)}">
                </label>
                <div class="live2-match">
                    <a class="match-link" href="${sofascoreEventUrl(item.event_id)}" target="_blank" rel="noopener noreferrer">
                        ${escHtml(item.home_team)} vs ${escHtml(item.away_team)}
                    </a>
                    <div class="live2-meta">${escHtml(item.league || '-')} • ${statusDesc} • Etkinlik ID: ${escHtml(item.event_id)}</div>
                </div>
                <div class="live2-scorebox">
                    <span class="score-pill">${item.score_home} - ${item.score_away}</span>
                    <span class="table-tag live-minute">${item.minute || 0}'</span>
                    <span class="upcoming-status-label">${statusLabel(statusValue)}</span>
                </div>
                <div class="row-actions row-actions-icons live2-actions">
                    <button class="icon-btn icon-btn-bet${statusValue === 'bet_placed' ? ' active' : ''}" onclick="setLive2Status('${escAttr(item.event_id)}', 'bet_placed')" title="Bahis oynandı" aria-label="Bahis oynandı">${ICONS.bet}</button>
                    <button class="icon-btn icon-btn-ignore${statusValue === 'ignored' ? ' active' : ''}" onclick="setLive2Status('${escAttr(item.event_id)}', 'ignored')" title="Gözardı et" aria-label="Gözardı et">${ICONS.ignore}</button>
                    <button class="icon-btn icon-btn-follow${statusValue === 'following' ? ' active' : ''}" onclick="setLive2Status('${escAttr(item.event_id)}', 'following')" title="Takip et" aria-label="Takip et">${ICONS.follow}</button>
                </div>
            </div>
            <div class="live2-text-data">
                <span><strong>Durum:</strong> ${statusDesc}</span>
                <span><strong>Skor:</strong> ${item.score_home} - ${item.score_away}</span>
                <span><strong>Dakika:</strong> ${item.minute || 0}'</span>
                <span><strong>Lig:</strong> ${escHtml(item.league || '-')}</span>
            </div>
            <div class="live2-stats-text" data-live2-stats="${escHtml(item.event_id)}">
                ${renderLive2StatsText(item)}
            </div>
        </article>`;
}

function renderLive2StatsText(item) {
    if (item.statsLoading) {
        return '<div class="live-details-loading">İstatistikler çekiliyor...</div>';
    }
    if (item.statsError) {
        return `<div class="live-details-empty">${escHtml(item.statsError)}</div>`;
    }
    const stats = item.stats || {};
    if (!hasLive2Stats(stats)) {
        return '<div class="live-details-empty">Bu maç için istatistik verisi henüz yok</div>';
    }

    const home = item.home_team || 'Ev';
    const away = item.away_team || 'Dep';
    const lines = [];

    addLive2Line(lines, 'Topa sahip olma', stats.possession_home, stats.possession_away, '%');
    addLive2Line(lines, 'Toplam şut', stats.total_shots_home, stats.total_shots_away);
    addLive2Line(lines, 'Kaleyi bulan şut', stats.shots_on_target_home, stats.shots_on_target_away);
    addLive2Line(lines, 'Kaleyi bulmayan şut', stats.shots_off_target_home, stats.shots_off_target_away);
    addLive2Line(lines, 'Bloklanan şut', stats.blocked_shots_home, stats.blocked_shots_away);
    addLive2Line(lines, 'Tehlikeli atak', stats.dangerous_attacks_home, stats.dangerous_attacks_away);
    addLive2Line(lines, 'Beklenen gol (xG)', stats.expected_goals_home, stats.expected_goals_away, '', 2);
    addLive2Line(lines, 'Büyük şans', stats.big_chances_home, stats.big_chances_away);
    addLive2Line(lines, 'Korner', stats.corner_kicks_home, stats.corner_kicks_away);
    addLive2Line(lines, 'Ofsayt', stats.offsides_home, stats.offsides_away);
    addLive2Line(lines, 'Faul', stats.fouls_home, stats.fouls_away);
    addLive2Line(lines, 'Sarı kart', stats.yellow_cards_home, stats.yellow_cards_away);
    addLive2Line(lines, 'Kırmızı kart', stats.red_cards_home, stats.red_cards_away);

    const passHome = statPercent(stats.pass_accuracy_home, passAccuracy(stats.accurate_passes_home, stats.total_passes_home));
    const passAway = statPercent(stats.pass_accuracy_away, passAccuracy(stats.accurate_passes_away, stats.total_passes_away));
    addLive2Line(lines, 'Pas isabeti', passHome, passAway, '%');

    return `
        <div class="live2-analyst-text">
            <strong>Analist özeti:</strong> ${escHtml(buildLive2AnalystNote(item, stats))}
        </div>
        <div class="live2-stat-lines">
            ${lines.map((line) => `<div>${escHtml(line)}</div>`).join('')}
        </div>
        <div class="live2-stat-footnote">Karşılaştırma sırası: ${escHtml(home)} - ${escHtml(away)}</div>`;
}

function hasLive2Stats(stats) {
    return [
        stats.possession_home, stats.possession_away,
        stats.total_shots_home, stats.total_shots_away,
        stats.shots_on_target_home, stats.shots_on_target_away,
        stats.shots_off_target_home, stats.shots_off_target_away,
        stats.blocked_shots_home, stats.blocked_shots_away,
        stats.dangerous_attacks_home, stats.dangerous_attacks_away,
        stats.expected_goals_home, stats.expected_goals_away,
        stats.big_chances_home, stats.big_chances_away,
        stats.corner_kicks_home, stats.corner_kicks_away,
    ].some((value) => Number(value) > 0);
}

function addLive2Line(lines, label, homeValue, awayValue, unit = '', decimals = null) {
    const home = Number(homeValue) || 0;
    const away = Number(awayValue) || 0;
    if (home <= 0 && away <= 0) return;
    lines.push(`${label}: ${formatNumber(home, decimals)}${unit} - ${formatNumber(away, decimals)}${unit}`);
}

function buildLive2AnalystNote(item, stats) {
    const home = item.home_team || 'Ev sahibi';
    const away = item.away_team || 'Deplasman';
    const homePressure = (
        (Number(stats.shots_on_target_home) || 0) * 3
        + (Number(stats.total_shots_home) || 0)
        + (Number(stats.dangerous_attacks_home) || 0) / 4
        + (Number(stats.expected_goals_home) || 0) * 4
        + (Number(stats.big_chances_home) || 0) * 2
    );
    const awayPressure = (
        (Number(stats.shots_on_target_away) || 0) * 3
        + (Number(stats.total_shots_away) || 0)
        + (Number(stats.dangerous_attacks_away) || 0) / 4
        + (Number(stats.expected_goals_away) || 0) * 4
        + (Number(stats.big_chances_away) || 0) * 2
    );
    const diff = Math.abs(homePressure - awayPressure);
    if (diff < 4) {
        return 'Baskı dengeli görünüyor; tek bir şut metriğine göre hüküm vermemek gerekir.';
    }
    const leader = homePressure > awayPressure ? home : away;
    const follower = homePressure > awayPressure ? away : home;
    if (diff >= 14) {
        return `${leader} belirgin baskı kuruyor; ${follower} savunmada daha fazla reaksiyon vermek zorunda kalmış.`;
    }
    return `${leader} tarafında hafif/orta seviye üstünlük var; isabetli şut ve tehlikeli atak trendi takip edilmeli.`;
}

function updateLive2StatsBlock(eventId) {
    const item = liveMatches2.find((match) => match.event_id === eventId);
    if (!item) return;
    const block = document.querySelector(`.live2-card[data-eid="${CSS.escape(eventId)}"] .live2-stats-text`);
    if (block) block.innerHTML = renderLive2StatsText(item);
}

async function loadLive2StatsProgressively(runId) {
    let nextIndex = 0;
    const worker = async () => {
        while (runId === live2StatsRun && nextIndex < liveMatches2.length) {
            const item = liveMatches2[nextIndex];
            nextIndex += 1;
            if (!item) continue;
            const data = await apiFetchQuiet(API.liveMatch2Stats(item.event_id));
            if (runId !== live2StatsRun) return;
            item.stats = data && data.stats ? data.stats : null;
            item.statsError = data ? '' : 'İstatistik verisi alınamadı';
            item.statsLoading = false;
            updateLive2StatsBlock(item.event_id);
        }
    };

    const workers = Array.from(
        { length: Math.min(LIVE2_STATS_CONCURRENCY, liveMatches2.length) },
        worker
    );
    await Promise.all(workers);
    if (runId === live2StatsRun && liveMatches2.length) {
        toast('Canlı Maçlar-2 istatistikleri güncellendi');
    }
}

function updateLive2Bulk() {
    const count = selectedLive2.size;
    setText('#selected-count-live2', `${count} seçili`);
    const betBtn = $('#btn-bulk-bet-live2');
    const ignBtn = $('#btn-bulk-ignore-live2');
    const folBtn = $('#btn-bulk-follow-live2');
    if (betBtn) betBtn.disabled = count === 0;
    if (ignBtn) ignBtn.disabled = count === 0;
    if (folBtn) folBtn.disabled = count === 0;
}

async function setLive2Status(eventId, status) {
    const match = liveMatches2.find((m) => m.event_id === eventId);
    if (!match) return;
    const newStatus = match.status === status ? 'new' : status;

    const result = await apiPost(API.liveMatchStatus(eventId), { status: newStatus });
    if (!result || !result.ok) return;

    match.status = newStatus;
    renderLive2Matches();
    toast(`Durum güncellendi: ${statusLabel(newStatus)}`);
}

async function bulkLive2Status(status) {
    const eventIds = [...selectedLive2];
    if (!eventIds.length) return;

    const result = await apiPost(API.liveMatchBulkStatus, { event_ids: eventIds, status });
    if (!result || !result.ok) return;

    eventIds.forEach((eid) => {
        const match = liveMatches2.find((m) => m.event_id === eid);
        if (match) match.status = status;
    });

    renderLive2Matches();
    toast(`${eventIds.length} maç güncellendi: ${statusLabel(status)}`);
}

const btnFetchLive2 = $('#btn-fetch-live2');
if (btnFetchLive2) btnFetchLive2.addEventListener('click', loadLiveMatches2);

const filterLive2 = $('#filter-live2-status');
if (filterLive2) filterLive2.addEventListener('change', renderLive2Matches);

const searchLive2 = $('#search-live2');
if (searchLive2) searchLive2.addEventListener('input', renderLive2Matches);

const selectAllLive2 = $('#select-all-live2');
if (selectAllLive2) {
    selectAllLive2.addEventListener('change', (event) => {
        const checked = event.target.checked;
        $$('.chk-live2').forEach((checkbox) => {
            checkbox.checked = checked;
            const eid = checkbox.dataset.eid;
            if (checked) selectedLive2.add(eid);
            else selectedLive2.delete(eid);
        });
        updateLive2Bulk();
    });
}

const btnBulkBetLive2 = $('#btn-bulk-bet-live2');
if (btnBulkBetLive2) btnBulkBetLive2.addEventListener('click', () => bulkLive2Status('bet_placed'));
const btnBulkIgnoreLive2 = $('#btn-bulk-ignore-live2');
if (btnBulkIgnoreLive2) btnBulkIgnoreLive2.addEventListener('click', () => bulkLive2Status('ignored'));
const btnBulkFollowLive2 = $('#btn-bulk-follow-live2');
if (btnBulkFollowLive2) btnBulkFollowLive2.addEventListener('click', () => bulkLive2Status('following'));

// Hook sortable headers for live-table (initSortableHeaders runs once and
// only dispatches to anomaly/upcoming; extend its dispatcher):
function refreshVisibleLiveMatches() {
    const activeTab = document.querySelector('.tab.active');
    if (activeTab && activeTab.dataset.tab === 'live') renderLiveMatches();
}

async function loadDeletedAnomalies() {
    const data = await apiFetch(API.deletedAnomalies);
    if (!data) return;

    deletedAnomalies = data;
    renderDeletedAnomalies();
    touchLastUpdated();
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
        tbody.innerHTML = '<tr><td colspan="8" class="empty-msg">Silinen kayıt yok</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map((item) => {
        const conditionBadge = item.condition_type === 'A'
            ? '<span class="badge badge-a">A / Beraberlik</span>'
            : '<span class="badge badge-b">B / 1 Fark</span>';
        const deletedTime = formatCreatedAt(item.deleted_at, true);

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
            <td><span class="score-pill">${item.score_home} - ${item.score_away}</span></td>
            <td><span class="table-tag">${item.minute}'</span></td>
            <td>${escHtml(item.league || '-')}</td>
            <td>${conditionBadge}</td>
            <td><span class="time-pill">${deletedTime}</span></td>
            <td>
                <div class="row-actions row-actions-icons">
                    <button class="icon-btn icon-btn-restore" onclick="restoreDeletedRow(${item.id})" title="Geri yükle" aria-label="Geri yükle">${ICONS.restore}</button>
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
    $('#selected-count-deleted').textContent = `${count} seçili`;
    $('#btn-bulk-restore-deleted').disabled = count === 0;
    $('#btn-bulk-purge-deleted').disabled = count === 0;
}

async function restoreDeletedRow(id) {
    const result = await apiPost(API.restoreAnomalies, { ids: [id] });
    if (!result || !result.ok) return;
    toast('Kayıt geri yüklendi');
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
$('#search-deleted').addEventListener('input', renderDeletedAnomalies);

$('#btn-bulk-restore-deleted').addEventListener('click', async () => {
    const ids = [...selectedDeleted];
    const result = await apiPost(API.restoreAnomalies, { ids });
    if (!result || !result.ok) return;
    toast(`${ids.length} kayıt geri yüklendi`);
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
    if (!confirm('Tüm veritabanı kayıtları temizlensin mi? Anomaliler, analizler ve yaklaşan maçlar silinecek.')) return;

    const button = $('#btn-clear-database');
    setButtonBusy(button, 'Temizleniyor...', 'VERİTABANINI TEMİZLE', true);

    const result = await apiPost(API.clearDatabase, {});
    if (result && result.ok) {
        anomalies = [];
        analyses = [];
        upcomingMatches = [];
        liveMatches = [];
        liveMatches2 = [];
        deletedAnomalies = [];
        renderAnomalies();
        renderAnalyses();
        renderUpcoming();
        renderLiveMatches();
        renderLive2Matches();
        renderDeletedAnomalies();
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

function formatStartTime(startTime, scanDate) {
    if (!startTime) return '-';

    const numericValue = Number(startTime);
    if (numericValue > 86400) {
        const date = new Date(numericValue * 1000);
        return date.toLocaleString('tr-TR', {
            timeZone: 'Europe/Istanbul',
            day: '2-digit',
            month: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
        });
    }

    if (typeof startTime === 'string' && startTime.includes(':')) {
        const datePart = scanDate || '';
        return datePart ? `${datePart.slice(8, 10)}.${datePart.slice(5, 7)} ${startTime}` : startTime;
    }

    return '-';
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

function runTypeLabel(runType) {
    const labels = {
        morning: 'Sabah raporu',
        evening: 'Akşam raporu',
        manual: 'Manuel rapor',
    };
    return labels[runType] || 'Rapor';
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

$('#btn-ai-analysis').addEventListener('click', async () => {
    const modal = $('#ai-modal');
    const meta = $('#modal-meta');
    const body = $('#modal-body');

    modal.style.display = 'flex';
    meta.textContent = '';
    body.textContent = 'Yükleniyor...';

    let data = analyses;
    if (!data.length) {
        data = await apiFetch(API.analyses);
    }

    if (!data || !data.length) {
        body.innerHTML = '<div class="empty-msg">Henüz yapay zeka analizi bulunmuyor.</div>';
        return;
    }

    const latest = data[0];
    meta.textContent = `${runTypeLabel(latest.run_type)} • ${latest.match_count} maç • ${formatCreatedAt(latest.created_at, true)}`;
    body.textContent = latest.analysis_text || 'Analiz metni bulunamadı.';
});

$('#modal-close').addEventListener('click', () => {
    $('#ai-modal').style.display = 'none';
});

$('#ai-modal').addEventListener('click', (event) => {
    if (event.target === event.currentTarget) {
        $('#ai-modal').style.display = 'none';
    }
});

document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && $('#ai-modal').style.display !== 'none') {
        $('#ai-modal').style.display = 'none';
    }
});

async function refreshAllData() {
    await Promise.all([
        checkStatus(),
        loadAnomalies(),
        loadAnalyses(),
        loadUpcoming(),
    ]);
}

(async () => {
    initSortableHeaders();
    await refreshAllData();

    setInterval(loadAnomalies, 60000);
    setInterval(loadUpcoming, 60000);
    setInterval(checkStatus, 30000);
})();
