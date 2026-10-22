/* ── State ───────────────────────────────────────────────────── */
const adminState = {
  token:        null,
  knowledgeBases: [],
  activeFilesKb: null,
};

/* ── Init ────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initAdminTheme();
  adminState.token = localStorage.getItem('rag_admin_token');
  if (adminState.token) {
    showDashboard();
    loadKnowledgeBases();
  }
  setupFilesDropzone();
});

function initAdminTheme() {
  const dark = localStorage.getItem('aria_dark_mode') === 'true';
  if (dark) document.documentElement.classList.add('dark');
  const btn = document.getElementById('admin-theme-toggle');
  btn.innerHTML = dark ? sunIcon() : moonIcon();
  btn.addEventListener('click', () => {
    const isDark = document.documentElement.classList.toggle('dark');
    localStorage.setItem('aria_dark_mode', isDark);
    btn.innerHTML = isDark ? sunIcon() : moonIcon();
  });
}
function moonIcon() { return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`; }
function sunIcon() { return `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>`; }

function escHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function authHeaders(extra = {}) {
  return { ...(adminState.token ? { 'Authorization': `Bearer ${adminState.token}` } : {}), ...extra };
}

/* ── Login / logout ──────────────────────────────────────────── */
async function handleAdminLogin(event) {
  event.preventDefault();
  const email = document.getElementById('admin-email').value.trim();
  const password = document.getElementById('admin-password').value;
  const errEl = document.getElementById('admin-login-error');
  const btn = document.getElementById('btn-admin-login');
  errEl.classList.add('hidden');
  setBtnLoading(btn, true);

  try {
    const res = await fetch('/auth/admin-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Sign in failed');

    adminState.token = data.token;
    localStorage.setItem('rag_admin_token', data.token);
    showDashboard();
    loadKnowledgeBases();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  } finally {
    setBtnLoading(btn, false);
  }
}

function adminLogout() {
  localStorage.removeItem('rag_admin_token');
  // Hard navigation instead of toggling panel classes - guarantees a clean
  // slate (no stale KB grid/modal state left over) the same way the main
  // app's logout() always does a full page redirect rather than DOM surgery.
  window.location.replace('/admin.html');
}

function showDashboard() {
  document.getElementById('admin-login-screen').classList.add('hidden');
  document.getElementById('admin-dashboard').classList.remove('hidden');
}

function setBtnLoading(btn, loading) {
  btn.disabled = loading;
  btn.querySelector('.btn-label').style.visibility = loading ? 'hidden' : 'visible';
  btn.querySelector('.btn-spinner').classList.toggle('hidden', !loading);
}

function handleAuthError(res) {
  if (res.status === 401) { adminLogout(); return true; }
  return false;
}

/* ── Knowledge base list ─────────────────────────────────────── */
let _kbPollTimer = null;

async function loadKnowledgeBases() {
  try {
    const res = await fetch('/admin/knowledge-bases', { headers: authHeaders() });
    if (handleAuthError(res)) return;
    adminState.knowledgeBases = await res.json();
    renderKbGrid();

    // While anything is still processing, keep polling so the "Processing..."
    // badge clears on its own once the upload finishes - without this, an
    // admin who isn't watching the upload modal has no way to know it's done.
    clearTimeout(_kbPollTimer);
    const anyProcessing = adminState.knowledgeBases.some(kb => kb.processing_count > 0);
    if (anyProcessing) {
      _kbPollTimer = setTimeout(loadKnowledgeBases, 4000);
    }
  } catch (e) {
    console.error('Failed to load knowledge bases', e);
  }
}

async function renameKb(kbId, currentName) {
  const name = prompt('Rename knowledge base', currentName);
  if (name === null) return;
  const trimmed = name.trim();
  if (!trimmed || trimmed === currentName) return;
  try {
    const res = await fetch(`/admin/knowledge-bases/${kbId}`, {
      method: 'PATCH',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ name: trimmed }),
    });
    const data = await res.json();
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error(data.detail || 'Failed to rename');
    await loadKnowledgeBases();
  } catch (e) {
    alert(e.message);
  }
}

function renderKbGrid() {
  const grid = document.getElementById('kb-grid');
  const empty = document.getElementById('kb-empty');
  if (adminState.knowledgeBases.length === 0) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
    return;
  }
  empty.classList.add('hidden');
  grid.innerHTML = adminState.knowledgeBases.map(kbCardHtml).join('');
}

function kbCardHtml(kb) {
  const checked = kb.status === 'active' ? 'checked' : '';
  const created = kb.created_at ? new Date(kb.created_at).toLocaleDateString() : '-';
  const processing = kb.processing_count > 0;
  return `
    <div class="kb-card" data-kb-id="${kb.id}">
      <div class="kb-card-top">
        <div class="kb-card-name-row">
          <div class="kb-card-name" id="kb-name-${kb.id}">${escHtml(kb.name)}</div>
          <button class="kb-rename-btn" title="Rename" onclick="renameKb('${kb.id}', '${escHtml(kb.name).replace(/'/g, "&#39;")}')">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          </button>
        </div>
        <span class="kb-status-badge ${kb.status}">${kb.status}</span>
      </div>
      <div class="kb-card-desc">${escHtml(kb.description || 'No description')}</div>
      ${processing ? `<div class="kb-processing-badge"><span class="kb-spinner"></span> Processing ${kb.processing_count} file${kb.processing_count === 1 ? '' : ''}...</div>` : ''}
      <div class="kb-card-meta">
        <span>${kb.document_count} file${kb.document_count === 1 ? '' : 's'}</span>
        <span>Created ${created}</span>
      </div>
      <div class="kb-toggle-row">
        <span class="kb-toggle-label">Active</span>
        <label class="kb-switch">
          <input type="checkbox" ${checked} onchange="toggleKbStatus('${kb.id}', this.checked)" />
          <span class="kb-switch-track"></span>
        </label>
      </div>
      <div class="kb-card-actions">
        <button class="admin-btn" onclick="openFilesModal('${kb.id}')">Manage files</button>
        <button class="admin-btn admin-btn-danger" onclick="deleteKb('${kb.id}', '${escHtml(kb.name)}')">Delete</button>
      </div>
    </div>`;
}

async function toggleKbStatus(kbId, isActive) {
  try {
    const res = await fetch(`/admin/knowledge-bases/${kbId}`, {
      method: 'PATCH',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ status: isActive ? 'active' : 'inactive' }),
    });
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error('Failed to update status');
    await loadKnowledgeBases();
  } catch (e) {
    alert(e.message);
    await loadKnowledgeBases();
  }
}

async function deleteKb(kbId, name) {
  if (!confirm(`Delete "${name}"? This removes the knowledge base and its file records.`)) return;
  try {
    const res = await fetch(`/admin/knowledge-bases/${kbId}`, { method: 'DELETE', headers: authHeaders() });
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error('Failed to delete');
    await loadKnowledgeBases();
  } catch (e) {
    alert(e.message);
  }
}

/* ── Create KB modal ─────────────────────────────────────────── */
function openCreateModal() {
  document.getElementById('kb-name').value = '';
  document.getElementById('kb-description').value = '';
  document.getElementById('create-kb-error').classList.add('hidden');
  document.getElementById('create-modal-backdrop').classList.remove('hidden');
}
function closeCreateModal() {
  document.getElementById('create-modal-backdrop').classList.add('hidden');
}

async function handleCreateKb(event) {
  event.preventDefault();
  const name = document.getElementById('kb-name').value.trim();
  const description = document.getElementById('kb-description').value.trim();
  const errEl = document.getElementById('create-kb-error');
  errEl.classList.add('hidden');

  try {
    const res = await fetch('/admin/knowledge-bases', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ name, description: description || null }),
    });
    const data = await res.json();
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error(data.detail || 'Failed to create knowledge base');
    closeCreateModal();
    await loadKnowledgeBases();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  }
}

/* ── Files modal ──────────────────────────────────────────────── */
function openFilesModal(kbId) {
  adminState.activeFilesKb = kbId;
  const kb = adminState.knowledgeBases.find(k => k.id === kbId);
  document.getElementById('files-modal-title').textContent = `Files - ${kb ? kb.name : ''}`;
  document.getElementById('files-progress').classList.add('hidden');
  document.getElementById('files-progress').innerHTML = '';
  document.getElementById('files-modal-backdrop').classList.remove('hidden');
  loadKbFiles(kbId);
}
function closeFilesModal() {
  document.getElementById('files-modal-backdrop').classList.add('hidden');
  adminState.activeFilesKb = null;
  clearTimeout(_filesPollTimer);
}

let _filesPollTimer = null;

async function loadKbFiles(kbId) {
  const tbody = document.getElementById('files-table-body');
  tbody.innerHTML = `<tr><td colspan="6">Loading...</td></tr>`;
  clearTimeout(_filesPollTimer);
  try {
    const res = await fetch(`/admin/knowledge-bases/${kbId}/files`, { headers: authHeaders() });
    if (handleAuthError(res)) return;
    const files = await res.json();
    if (files.length === 0) {
      tbody.innerHTML = `<tr><td colspan="6">No files uploaded yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = files.map(f => `
      <tr>
        <td>${escHtml(f.filename)}</td>
        <td>${humanSize(f.file_size)}</td>
        <td>${f.chunks_stored ?? '-'}</td>
        <td>${f.status === 'pending' ? '<span class="kb-processing-badge" style="display:inline-flex"><span class="kb-spinner"></span> processing</span>' : escHtml(f.status)}</td>
        <td>${f.uploaded_at ? new Date(f.uploaded_at).toLocaleString() : '-'}</td>
        <td><button class="file-delete-btn" title="Delete this file" onclick="deleteKbFile('${kbId}', '${f.id}', '${escHtml(f.filename).replace(/'/g, "&#39;")}')">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
        </button></td>
      </tr>`).join('');

    // Keep this table live while a row is still mid-processing, same reason
    // as the dashboard's own poll - a reopened modal should reflect reality.
    if (files.some(f => f.status === 'pending') && adminState.activeFilesKb === kbId) {
      _filesPollTimer = setTimeout(() => loadKbFiles(kbId), 4000);
    }
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="6">Failed to load files.</td></tr>`;
  }
}

async function deleteKbFile(kbId, docId, filename) {
  if (!confirm(`Delete "${filename}" from this knowledge base?`)) return;
  try {
    const res = await fetch(`/admin/knowledge-bases/${kbId}/files/${docId}`, {
      method: 'DELETE',
      headers: authHeaders(),
    });
    if (handleAuthError(res)) return;
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || 'Failed to delete file');
    }
    await loadKbFiles(kbId);
    await loadKnowledgeBases();
  } catch (e) {
    alert(e.message);
  }
}

function humanSize(bytes) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function setupFilesDropzone() {
  const zone = document.getElementById('files-dropzone');
  const input = document.getElementById('files-input');

  zone.addEventListener('click', (e) => { if (e.target.tagName !== 'LABEL') input.click(); });
  input.addEventListener('change', () => { if (input.files.length) uploadFiles(input.files); input.value = ''; });

  ['dragenter', 'dragover'].forEach(evt =>
    zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.add('dragover'); })
  );
  ['dragleave', 'drop'].forEach(evt =>
    zone.addEventListener(evt, (e) => { e.preventDefault(); zone.classList.remove('dragover'); })
  );
  zone.addEventListener('drop', (e) => {
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
}

async function uploadFiles(fileList) {
  const kbId = adminState.activeFilesKb;
  if (!kbId) return;

  const progressEl = document.getElementById('files-progress');
  progressEl.classList.remove('hidden');
  progressEl.innerHTML = '';

  const formData = new FormData();
  for (const f of fileList) formData.append('files', f);

  const addRow = (text, cls = '', spinner = false) => {
    const row = document.createElement('div');
    row.className = `files-progress-row ${cls}`;
    row.innerHTML = spinner ? `<span class="kb-spinner" style="display:inline-block;margin-right:6px;"></span>${escHtml(text)}` : escHtml(text);
    progressEl.appendChild(row);
    progressEl.scrollTop = progressEl.scrollHeight;
    return row;
  };

  // Visible the instant the user drops/selects files - requests can take a
  // few seconds to even start streaming back (upload transfer + the server
  // spinning up parsing), and with nothing shown in that gap it looks like
  // nothing happened, same complaint as the dashboard-level indicator below.
  const startingRow = addRow(
    `Uploading ${fileList.length} file${fileList.length === 1 ? '' : 's'}...`, '', true
  );

  try {
    const res = await fetch(`/admin/knowledge-bases/${kbId}/files`, {
      method: 'POST',
      headers: authHeaders(),
      body: formData,
    });
    if (handleAuthError(res)) return;
    if (!res.ok || !res.body) {
      startingRow.remove();
      const data = await res.json().catch(() => ({}));
      addRow(data.detail || 'Upload failed', 'error');
      return;
    }

    let firstEvent = true;
    await readAdminSSEStream(res, (ev) => {
      if (firstEvent) { startingRow.remove(); firstEvent = false; }
      if (ev.type === 'step' && ev.status === 'done') addRow(ev.message, 'done');
      else if (ev.type === 'warning') addRow(ev.message, 'error');
      else if (ev.type === 'error') addRow(ev.message, 'error');
      else if (ev.type === 'complete') {
        addRow(ev.completion_message || 'Done', 'done');
        loadKbFiles(kbId);
        loadKnowledgeBases();
      }
    });
  } catch (e) {
    startingRow.remove();
    addRow(`Upload failed: ${e.message}`, 'error');
  }
}

async function readAdminSSEStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try { onEvent(JSON.parse(line.slice(6))); } catch (_) {}
      }
    }
  }
}
