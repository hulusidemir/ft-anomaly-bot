/* ===== Football Anomaly Bot - Dashboard JS ===== */

const API = {
    anomalies: (status) => `/api/anomalies${status ? `?status=${status}` : ''}`,
    updateStatus: (id) => `/api/anomalies/${id}/status`,
    bulkStatus: '/api/anomalies/bulk-status',
    deleteAnomalies: '/api/anomalies/delete',
    clearAnomalies: '/api/anomalies/clear',
    analyses: '/api/analyses',
    deleteAnalyses: '/api/analyses/delete',
    clearAnalyses: '/api/analyses/clear',
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

let anomalies = [];
let analyses = [];
let upcomingMatches = [];
let schedulerJobs = [];

const selectedAnomalies = new Set();
const selectedAnalyses = new Set();
const selectedUpcoming = new Set();

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
            throw new Error(`HTTP ${response.status}`);
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
                <div class="row-actions">
                    <button class="row-btn${item.status === 'bet_placed' ? ' active' : ''}" onclick="setStatus(${item.id}, 'bet_placed')">Bahis</button>
                    <button class="row-btn${item.status === 'ignored' ? ' active' : ''}" onclick="setStatus(${item.id}, 'ignored')">Gözardı</button>
                    <button class="row-btn${item.status === 'following' ? ' active' : ''}" onclick="setStatus(${item.id}, 'following')">Takip</button>
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
    const newStatus = anomaly && anomaly.status === status ? 'new' : status;
    const result = await apiPost(API.updateStatus(id), { status: newStatus });
    if (!result || !result.ok) return;

    if (anomaly) anomaly.status = newStatus;
    renderAnomalies();
    updateOverview();
    toast(`Durum güncellendi: ${statusLabel(newStatus)}`);
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
    if (!confirm(`${selectedAnomalies.size} anomali kaydı silinsin mi?`)) return;

    const ids = [...selectedAnomalies];
    const result = await apiPost(API.deleteAnomalies, { ids });
    if (!result || !result.ok) return;

    toast(`${ids.length} anomali silindi`);
    await loadAnomalies();
});

$('#btn-clear-all-anomalies').addEventListener('click', async () => {
    if (!confirm('Tüm anomali geçmişi silinsin mi? Bu işlem geri alınamaz.')) return;

    const ok = await clearAllWithFallback({
        clearUrl: API.clearAnomalies,
        deleteUrl: API.deleteAnomalies,
        ids: anomalies.map((item) => item.id),
        emptyText: 'Silinecek anomali bulunamadı',
        successText: 'Tüm anomali geçmişi silindi',
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

function renderUpcoming() {
    const tbody = $('#upcoming-body');
    const selectAll = $('#select-all-upcoming');
    selectedUpcoming.clear();
    if (selectAll) selectAll.checked = false;
    updateUpcomingBulk();

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
                    <button class="row-btn${item.status === 'following' ? ' active' : ''}" onclick="setUpcomingStatus(${item.id}, 'following')">Takip</button>
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
        renderAnomalies();
        renderAnalyses();
        renderUpcoming();
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
