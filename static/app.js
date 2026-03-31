/* ===== Football Anomaly Bot — Dashboard JS ===== */

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
    status: '/api/status',
    triggerLive: '/api/trigger/live-scan',
    triggerUpcoming: '/api/trigger/upcoming-scan',
};

// ===== State =====
let anomalies = [];
let analyses = [];
let upcomingMatches = [];
const selectedAnomalies = new Set();
const selectedAnalyses = new Set();
const selectedUpcoming = new Set();

// ===== DOM Refs =====
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// ===== Tabs =====
$$('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
        $$('.tab').forEach(t => t.classList.remove('active'));
        $$('.tab-content').forEach(tc => tc.classList.remove('active'));
        tab.classList.add('active');
        $(`#tab-${tab.dataset.tab}`).classList.add('active');
    });
});

// ===== Toast =====
function toast(msg, isError = false) {
    let el = $('.toast');
    if (!el) {
        el = document.createElement('div');
        el.className = 'toast';
        document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.toggle('error', isError);
    el.classList.add('show');
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.remove('show'), 3000);
}

// ===== API Helpers =====
async function apiFetch(url, opts = {}) {
    try {
        const resp = await fetch(url, {
            headers: { 'Content-Type': 'application/json' },
            ...opts,
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return await resp.json();
    } catch (e) {
        toast(`İstek başarısız: ${e.message}`, true);
        return null;
    }
}

async function apiPost(url, body) {
    return apiFetch(url, { method: 'POST', body: JSON.stringify(body) });
}

async function postRaw(url, body) {
    try {
        const resp = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body || {}),
        });
        return { ok: resp.ok, status: resp.status };
    } catch (e) {
        return { ok: false, status: 0, error: e };
    }
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

    // Backward compatibility: if backend has no /clear endpoint yet, fallback to delete-by-ids.
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

// ===== Status Check =====
async function checkStatus() {
    const data = await apiFetch(API.status);
    const dot = $('#status-indicator');
    const text = $('#status-text');
    if (data && data.status === 'running') {
        dot.className = 'status-dot online';
        text.textContent = 'Sistem Aktif';
    } else {
        dot.className = 'status-dot error';
        text.textContent = 'Sistem Pasif';
    }
}

// ===== Sorting & Filtering =====
const sortState = {};  // { tableId: { key, dir } }

function initSortableHeaders() {
    $$('.sortable').forEach(th => {
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
            // Update header indicators
            table.querySelectorAll('.sortable').forEach(h => {
                h.classList.remove('sort-asc', 'sort-desc');
            });
            th.classList.add(sortState[tableId].dir === 'asc' ? 'sort-asc' : 'sort-desc');
            // Re-render
            if (tableId === 'anomaly-table') renderAnomalies();
            else if (tableId === 'upcoming-table') renderUpcoming();
        });
    });
}

function sortData(data, tableId, getSortValue) {
    const s = sortState[tableId];
    if (!s) return data;
    const sorted = [...data].sort((a, b) => {
        const va = getSortValue(a, s.key);
        const vb = getSortValue(b, s.key);
        if (va < vb) return s.dir === 'asc' ? -1 : 1;
        if (va > vb) return s.dir === 'asc' ? 1 : -1;
        return 0;
    });
    return sorted;
}

function filterBySearch(data, query, getSearchText) {
    if (!query) return data;
    const q = query.toLowerCase();
    return data.filter(item => getSearchText(item).toLowerCase().includes(q));
}

function sofascoreEventUrl(eventId) {
    if (!eventId) return '#';
    return `https://www.sofascore.com/event/${encodeURIComponent(eventId)}`;
}

// ===== Anomalies =====
async function loadAnomalies() {
    const filter = $('#filter-status').value;
    const data = await apiFetch(API.anomalies(filter));
    if (data) {
        anomalies = data;
        renderAnomalies();
    }
}

function renderAnomalies() {
    const tbody = $('#anomaly-body');
    selectedAnomalies.clear();
    updateBulkButtons();

    // Search filter
    const searchQuery = ($('#search-anomalies') || {}).value || '';
    let filtered = filterBySearch(anomalies, searchQuery, a =>
        `${a.home_team} ${a.away_team} ${a.league} ${a.condition_type}`
    );

    // Sort
    filtered = sortData(filtered, 'anomaly-table', (a, key) => {
        switch (key) {
            case 'match': return `${a.home_team} ${a.away_team}`.toLowerCase();
            case 'score': return a.score_home * 100 + a.score_away;
            case 'minute': return a.minute;
            case 'league': return (a.league || '').toLowerCase();
            case 'condition': return a.condition_type;
            case 'alert': return a.alert_number || 1;
            case 'time': return a.created_at || '';
            default: return '';
        }
    });

    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="10" class="empty-msg">Anomali bulunamadı</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map(a => {
        const rules = (a.triggered_rules || []);
        const ruleHtml = rules.map(r => `<li>${escHtml(r)}</li>`).join('');
        const stateClass = a.status !== 'new' ? `state-${a.status}` : '';
        const time = a.created_at ? new Date(a.created_at + 'Z').toLocaleTimeString('tr-TR', { timeZone: 'Europe/Istanbul' }) : '';
        const condBadge = a.condition_type === 'A'
            ? '<span class="badge badge-a">A — Beraberlik</span>'
            : '<span class="badge badge-b">B — 1 Fark</span>';
        const alertNum = a.alert_number || 1;
        const alertBadge = alertNum > 1
            ? `<span class="badge badge-alert badge-alert-multi">🔔 ${alertNum}. Uyarı</span>`
            : `<span class="badge badge-alert">1. Uyarı</span>`;

        return `
        <tr class="${stateClass}" data-id="${a.id}">
            <td class="col-check"><input type="checkbox" class="chk-anomaly" data-id="${a.id}"></td>
            <td>
                <a class="match-link" href="${sofascoreEventUrl(a.match_id)}" target="_blank" rel="noopener noreferrer">
                    <strong>${escHtml(a.home_team)}</strong> vs <strong>${escHtml(a.away_team)}</strong>
                </a>
            </td>
            <td>${a.score_home} - ${a.score_away}</td>
            <td>${a.minute}'</td>
            <td>${escHtml(a.league)}</td>
            <td>${condBadge}</td>
            <td><ul class="rules-list">${ruleHtml}</ul></td>
            <td>${alertBadge}</td>
            <td>${time}</td>
            <td>
                <div class="row-actions">
                    <button class="row-btn bet${a.status==='bet_placed'?' active':''}" onclick="setStatus(${a.id},'bet_placed')" title="Bahis Oynandı">💰 Bahis</button>
                    <button class="row-btn ignore${a.status==='ignored'?' active':''}" onclick="setStatus(${a.id},'ignored')" title="Gözardı Et">🚫 Gözardı</button>
                    <button class="row-btn follow${a.status==='following'?' active':''}" onclick="setStatus(${a.id},'following')" title="Takip Et">👁 Takip</button>
                </div>
            </td>
        </tr>`;
    }).join('');

    // Checkbox listeners
    $$('.chk-anomaly').forEach(chk => {
        chk.addEventListener('change', () => {
            const id = parseInt(chk.dataset.id);
            chk.checked ? selectedAnomalies.add(id) : selectedAnomalies.delete(id);
            updateBulkButtons();
        });
    });
}

async function setStatus(id, status) {
    const a = anomalies.find(x => x.id === id);
    const newStatus = (a && a.status === status) ? 'new' : status;
    const res = await apiPost(API.updateStatus(id), { status: newStatus });
    if (res && res.ok) {
        if (a) a.status = newStatus;
        renderAnomalies();
        const labels = {new:'Yeni',bet_placed:'Bahis Oynandı',ignored:'Gözardı Edildi',following:'Takip Ediliyor'};
        toast(`Durum → ${labels[newStatus] || newStatus}`);
    }
}

function updateBulkButtons() {
    const count = selectedAnomalies.size;
    $('#selected-count').textContent = `${count} seçili`;
    $('#btn-bulk-bet').disabled = count === 0;
    $('#btn-bulk-ignore').disabled = count === 0;
    $('#btn-bulk-follow').disabled = count === 0;
    $('#btn-bulk-delete').disabled = count === 0;
}

$('#select-all-anomalies').addEventListener('change', (e) => {
    const checked = e.target.checked;
    $$('.chk-anomaly').forEach(chk => {
        chk.checked = checked;
        const id = parseInt(chk.dataset.id);
        checked ? selectedAnomalies.add(id) : selectedAnomalies.delete(id);
    });
    updateBulkButtons();
});

$('#btn-bulk-bet').addEventListener('click', () => bulkStatus('bet_placed'));
$('#btn-bulk-ignore').addEventListener('click', () => bulkStatus('ignored'));
$('#btn-bulk-follow').addEventListener('click', () => bulkStatus('following'));
$('#btn-bulk-delete').addEventListener('click', async () => {
    if (!confirm(`${selectedAnomalies.size} öğeyi silmek istediğinize emin misiniz?`)) return;
    const ids = [...selectedAnomalies];
    const res = await apiPost(API.deleteAnomalies, { ids });
    if (res && res.ok) {
        toast(`${ids.length} anomali silindi`);
        await loadAnomalies();
    }
});

$('#btn-clear-all-anomalies').addEventListener('click', async () => {
    if (!confirm('Tüm anomali geçmişi silinsin mi? Bu işlem geri alınamaz.')) return;
    const ok = await clearAllWithFallback({
        clearUrl: API.clearAnomalies,
        deleteUrl: API.deleteAnomalies,
        ids: anomalies.map(a => a.id),
        emptyText: 'Silinecek anomali bulunamadı',
        successText: 'Tüm anomali geçmişi silindi',
    });
    if (ok) {
        await loadAnomalies();
    }
});

async function bulkStatus(status) {
    const ids = [...selectedAnomalies];
    const res = await apiPost(API.bulkStatus, { ids, status });
    if (res && res.ok) {
        ids.forEach(id => {
            const a = anomalies.find(x => x.id === id);
            if (a) a.status = status;
        });
        renderAnomalies();
        const labels = {bet_placed:'Bahis Oynandı',ignored:'Gözardı Edildi',following:'Takip Ediliyor'};
        toast(`${ids.length} öğe → ${labels[status] || status}`);
    }
}

$('#filter-status').addEventListener('change', loadAnomalies);
$('#btn-refresh').addEventListener('click', loadAnomalies);
$('#search-anomalies').addEventListener('input', renderAnomalies);

// ===== Analyses =====
async function loadAnalyses() {
    const data = await apiFetch(API.analyses);
    if (data) {
        analyses = data;
        renderAnalyses();
    }
}

function renderAnalyses() {
    const container = $('#analyses-list');
    selectedAnalyses.clear();
    updateAnalysesBulk();

    const searchQuery = ($('#search-analyses') || {}).value || '';
    let filtered = filterBySearch(analyses, searchQuery, a => a.analysis_text || '');

    if (!filtered.length) {
        container.innerHTML = '<div class="empty-msg">Henüz analiz yok</div>';
        return;
    }

    container.innerHTML = filtered.map(a => {
        const time = a.created_at ? new Date(a.created_at + 'Z').toLocaleString('tr-TR', { timeZone: 'Europe/Istanbul' }) : '';
        const runLabel = a.run_type === 'morning' ? '🌅 Sabah' : a.run_type === 'evening' ? '🌆 Akşam' : '🔧 Manuel';
        return `
        <div class="analysis-card" data-id="${a.id}">
            <input type="checkbox" class="chk-analysis card-check" data-id="${a.id}">
            <div class="card-header">
                <span>${runLabel} — ${a.match_count} maç</span>
                <span class="card-meta">${time}</span>
            </div>
            <div class="card-body">${escHtml(a.analysis_text)}</div>
        </div>`;
    }).join('');

    $$('.chk-analysis').forEach(chk => {
        chk.addEventListener('change', () => {
            const id = parseInt(chk.dataset.id);
            chk.checked ? selectedAnalyses.add(id) : selectedAnalyses.delete(id);
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
    if (!confirm(`${selectedAnalyses.size} analizi silmek istediğinize emin misiniz?`)) return;
    const ids = [...selectedAnalyses];
    const res = await apiPost(API.deleteAnalyses, { ids });
    if (res && res.ok) {
        toast(`${ids.length} analiz silindi`);
        await loadAnalyses();
    }
});

$('#btn-clear-all-analyses').addEventListener('click', async () => {
    if (!confirm('Tüm analiz geçmişi silinsin mi? Bu işlem geri alınamaz.')) return;
    const ok = await clearAllWithFallback({
        clearUrl: API.clearAnalyses,
        deleteUrl: API.deleteAnalyses,
        ids: analyses.map(a => a.id),
        emptyText: 'Silinecek analiz bulunamadı',
        successText: 'Tüm analiz geçmişi silindi',
    });
    if (ok) {
        await loadAnalyses();
    }
});

$('#btn-refresh-analyses').addEventListener('click', loadAnalyses);
$('#search-analyses').addEventListener('input', renderAnalyses);

// ===== Upcoming Matches =====
async function loadUpcoming() {
    const filter = $('#filter-upcoming-status').value;
    const data = await apiFetch(API.upcoming(filter));
    if (data) {
        upcomingMatches = data;
        renderUpcoming();
    }
}

function renderUpcoming() {
    const tbody = $('#upcoming-body');
    selectedUpcoming.clear();
    updateUpcomingBulk();

    // Search filter
    const searchQuery = ($('#search-upcoming') || {}).value || '';
    let filtered = filterBySearch(upcomingMatches, searchQuery, m =>
        `${m.home_team} ${m.away_team} ${m.league} ${m.round_info || ''}`
    );

    // Sort
    filtered = sortData(filtered, 'upcoming-table', (m, key) => {
        switch (key) {
            case 'match': return `${m.home_team} ${m.away_team}`.toLowerCase();
            case 'start': return m.start_time || 0;
            case 'league': return (m.league || '').toLowerCase();
            case 'round': return (m.round_info || '').toLowerCase();
            case 'status': return m.has_anomaly ? '0' : '1';  // anomaly first
            default: return '';
        }
    });

    if (!filtered.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty-msg">Yaklaşan maç bulunamadı</td></tr>';
        return;
    }

    tbody.innerHTML = filtered.map(m => {
        const stateClass = m.status !== 'new' ? `state-${m.status}` : '';
        const anomalyClass = m.has_anomaly ? 'anomaly-row' : '';
        const startTime = formatStartTime(m.start_time, m.scan_date);
        const anomalyBadge = m.has_anomaly
            ? '<span class="badge badge-anomaly">⚠ Anomali</span>'
            : '';

        return `
        <tr class="${stateClass} ${anomalyClass}" data-id="${m.id}">
            <td class="col-check"><input type="checkbox" class="chk-upcoming" data-id="${m.id}"></td>
            <td>
                <a class="match-link" href="${sofascoreEventUrl(m.event_id)}" target="_blank" rel="noopener noreferrer">
                    <strong>${escHtml(m.home_team)}</strong> vs <strong>${escHtml(m.away_team)}</strong>
                </a>
            </td>
            <td>${startTime}</td>
            <td>${escHtml(m.league)}</td>
            <td>${escHtml(m.round_info || '')}</td>
            <td>${anomalyBadge} <span class="upcoming-status-label">${upcomingStatusLabel(m.status)}</span></td>
            <td>
                <div class="row-actions">
                    <button class="row-btn follow${m.status==='following'?' active':''}" onclick="setUpcomingStatus(${m.id},'following')" title="Takip Et">👁 Takip</button>
                    <button class="row-btn ignore${m.status==='ignored'?' active':''}" onclick="setUpcomingStatus(${m.id},'ignored')" title="Gözardı Et">🚫 Gözardı</button>
                </div>
            </td>
        </tr>`;
    }).join('');

    $$('.chk-upcoming').forEach(chk => {
        chk.addEventListener('change', () => {
            const id = parseInt(chk.dataset.id);
            chk.checked ? selectedUpcoming.add(id) : selectedUpcoming.delete(id);
            updateUpcomingBulk();
        });
    });
}

function upcomingStatusLabel(status) {
    const labels = { new: 'Yeni', following: 'Takip Ediliyor', ignored: 'Gözardı Edildi' };
    return labels[status] || status || 'Yeni';
}

async function setUpcomingStatus(id, status) {
    const m = upcomingMatches.find(x => x.id === id);
    const newStatus = (m && m.status === status) ? 'new' : status;
    const res = await apiPost(API.updateUpcomingStatus(id), { status: newStatus });
    if (res && res.ok) {
        if (m) m.status = newStatus;
        renderUpcoming();
        toast(`Durum → ${upcomingStatusLabel(newStatus)}`);
    }
}

function updateUpcomingBulk() {
    const count = selectedUpcoming.size;
    $('#selected-count-upcoming').textContent = `${count} seçili`;
    $('#btn-bulk-follow-upcoming').disabled = count === 0;
    $('#btn-bulk-ignore-upcoming').disabled = count === 0;
    $('#btn-bulk-delete-upcoming').disabled = count === 0;
}

$('#select-all-upcoming').addEventListener('change', (e) => {
    const checked = e.target.checked;
    $$('.chk-upcoming').forEach(chk => {
        chk.checked = checked;
        const id = parseInt(chk.dataset.id);
        checked ? selectedUpcoming.add(id) : selectedUpcoming.delete(id);
    });
    updateUpcomingBulk();
});

$('#btn-bulk-follow-upcoming').addEventListener('click', () => bulkUpcomingStatus('following'));
$('#btn-bulk-ignore-upcoming').addEventListener('click', () => bulkUpcomingStatus('ignored'));
$('#btn-bulk-delete-upcoming').addEventListener('click', async () => {
    if (!confirm(`${selectedUpcoming.size} maçı silmek istediğinize emin misiniz?`)) return;
    const ids = [...selectedUpcoming];
    const res = await apiPost(API.deleteUpcoming, { ids });
    if (res && res.ok) {
        toast(`${ids.length} maç silindi`);
        await loadUpcoming();
    }
});

$('#btn-clear-all-upcoming').addEventListener('click', async () => {
    if (!confirm('Tüm geçmiş/yaklaşan maç kayıtları silinsin mi? Bu işlem geri alınamaz.')) return;
    const ok = await clearAllWithFallback({
        clearUrl: API.clearUpcoming,
        deleteUrl: API.deleteUpcoming,
        ids: upcomingMatches.map(m => m.id),
        emptyText: 'Silinecek geçmiş maç bulunamadı',
        successText: 'Tüm maç kayıtları silindi',
    });
    if (ok) {
        await loadUpcoming();
    }
});

async function bulkUpcomingStatus(status) {
    const ids = [...selectedUpcoming];
    const res = await apiPost(API.bulkUpcomingStatus, { ids, status });
    if (res && res.ok) {
        ids.forEach(id => {
            const m = upcomingMatches.find(x => x.id === id);
            if (m) m.status = status;
        });
        renderUpcoming();
        toast(`${ids.length} maç → ${upcomingStatusLabel(status)}`);
    }
}

$('#filter-upcoming-status').addEventListener('change', loadUpcoming);
$('#btn-refresh-upcoming').addEventListener('click', loadUpcoming);
$('#search-upcoming').addEventListener('input', renderUpcoming);
$('#btn-trigger-upcoming').addEventListener('click', async () => {
    const btn = $('#btn-trigger-upcoming');
    btn.disabled = true;
    btn.textContent = '⏳ Çekiliyor...';
    const res = await apiPost(API.triggerUpcoming, {});
    if (res && res.ok) {
        toast('Yaklaşan maçlar çekiliyor, birkaç saniye bekleyin...');
        setTimeout(loadUpcoming, 8000);
    }
    setTimeout(() => {
        btn.disabled = false;
        btn.textContent = '🚀 Maçları Çek';
    }, 10000);
});

// ===== Utility =====
function escHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function formatStartTime(startTime, scanDate) {
    if (!startTime) return '';
    const num = Number(startTime);
    // Unix timestamp (large number)
    if (num > 86400) {
        const d = new Date(num * 1000);
        return d.toLocaleString('tr-TR', {
            timeZone: 'Europe/Istanbul',
            day: '2-digit', month: '2-digit',
            hour: '2-digit', minute: '2-digit'
        });
    }
    // Legacy HH:MM format — show as-is with scan_date
    if (typeof startTime === 'string' && startTime.includes(':')) {
        const datePart = scanDate || '';
        return datePart ? `${datePart.slice(8,10)}.${datePart.slice(5,7)} ${startTime}` : startTime;
    }
    return '';
}

// ===== AI Analysis Modal =====
$('#btn-ai-analysis').addEventListener('click', async () => {
    const modal = $('#ai-modal');
    const meta = $('#modal-meta');
    const body = $('#modal-body');

    modal.style.display = 'flex';
    meta.textContent = '';
    body.textContent = 'Yükleniyor...';

    const data = await apiFetch(API.analyses);
    if (!data || data.length === 0) {
        meta.textContent = '';
        body.innerHTML = '<div class="empty-msg">Henüz yapay zeka analizi bulunmuyor.</div>';
        return;
    }

    const latest = data[0]; // most recent
    const time = latest.created_at ? new Date(latest.created_at + 'Z').toLocaleString('tr-TR', { timeZone: 'Europe/Istanbul' }) : '';
    const runLabel = latest.run_type === 'morning' ? '🌅 Sabah' : latest.run_type === 'evening' ? '🌆 Akşam' : '🔧 Manuel';
    meta.textContent = `${runLabel} Raporu — ${latest.match_count} maç analiz edildi — ${time}`;
    body.textContent = latest.analysis_text || 'Analiz metni bulunamadı.';
});

$('#modal-close').addEventListener('click', () => {
    $('#ai-modal').style.display = 'none';
});

$('#ai-modal').addEventListener('click', (e) => {
    if (e.target === e.currentTarget) {
        $('#ai-modal').style.display = 'none';
    }
});

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('#ai-modal').style.display !== 'none') {
        $('#ai-modal').style.display = 'none';
    }
});

// ===== Init =====
(async () => {
    initSortableHeaders();
    await checkStatus();
    await loadAnomalies();
    await loadAnalyses();
    await loadUpcoming();

    // Auto-refresh every 60s
    setInterval(loadAnomalies, 60000);
    setInterval(loadUpcoming, 60000);
    setInterval(checkStatus, 30000);
})();
