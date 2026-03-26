/* ===== Football Anomaly Bot — Dashboard JS ===== */

const API = {
    anomalies: (status) => `/api/anomalies${status ? `?status=${status}` : ''}`,
    updateStatus: (id) => `/api/anomalies/${id}/status`,
    bulkStatus: '/api/anomalies/bulk-status',
    deleteAnomalies: '/api/anomalies/delete',
    analyses: '/api/analyses',
    deleteAnalyses: '/api/analyses/delete',
    status: '/api/status',
    triggerLive: '/api/trigger/live-scan',
    triggerUpcoming: '/api/trigger/upcoming-scan',
};

// ===== State =====
let anomalies = [];
let analyses = [];
const selectedAnomalies = new Set();
const selectedAnalyses = new Set();

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

    if (!anomalies.length) {
        tbody.innerHTML = '<tr><td colspan="9" class="empty-msg">Anomali bulunamadı</td></tr>';
        return;
    }

    tbody.innerHTML = anomalies.map(a => {
        const rules = (a.triggered_rules || []);
        const ruleHtml = rules.map(r => `<li>${escHtml(r)}</li>`).join('');
        const stateClass = a.status !== 'new' ? `state-${a.status}` : '';
        const time = a.created_at ? new Date(a.created_at + 'Z').toLocaleTimeString() : '';
        const condBadge = a.condition_type === 'A'
            ? '<span class="badge badge-a">A — Beraberlik</span>'
            : '<span class="badge badge-b">B — 1 Fark</span>';

        return `
        <tr class="${stateClass}" data-id="${a.id}">
            <td class="col-check"><input type="checkbox" class="chk-anomaly" data-id="${a.id}"></td>
            <td><strong>${escHtml(a.home_team)}</strong> vs <strong>${escHtml(a.away_team)}</strong></td>
            <td>${a.score_home} - ${a.score_away}</td>
            <td>${a.minute}'</td>
            <td>${escHtml(a.league)}</td>
            <td>${condBadge}</td>
            <td><ul class="rules-list">${ruleHtml}</ul></td>
            <td>${time}</td>
            <td>
                <div class="row-actions">
                    <button class="row-btn bet" onclick="setStatus(${a.id},'bet_placed')" title="Bahis Oynandı">💰 Bahis</button>
                    <button class="row-btn ignore" onclick="setStatus(${a.id},'ignored')" title="Gözardı Et">🚫 Gözardı</button>
                    <button class="row-btn follow" onclick="setStatus(${a.id},'following')" title="Takip Et">👁 Takip</button>
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
    const res = await apiPost(API.updateStatus(id), { status });
    if (res && res.ok) {
        // Update local state
        const a = anomalies.find(x => x.id === id);
        if (a) a.status = status;
        renderAnomalies();
        const labels = {bet_placed:'Bahis Oynandı',ignored:'Gözardı Edildi',following:'Takip Ediliyor'};
        toast(`Durum → ${labels[status] || status}`);
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

    if (!analyses.length) {
        container.innerHTML = '<div class="empty-msg">Henüz analiz yok</div>';
        return;
    }

    container.innerHTML = analyses.map(a => {
        const time = a.created_at ? new Date(a.created_at + 'Z').toLocaleString() : '';
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

$('#btn-refresh-analyses').addEventListener('click', loadAnalyses);

// ===== Utility =====
function escHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ===== Init =====
(async () => {
    await checkStatus();
    await loadAnomalies();
    await loadAnalyses();

    // Auto-refresh anomalies every 60s
    setInterval(loadAnomalies, 60000);
    setInterval(checkStatus, 30000);
})();
