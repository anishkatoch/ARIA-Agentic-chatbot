/* ── State ───────────────────────────────────────────────────── */
const adminState = {
  token:        null,
  knowledgeBases: [],
  users:          [],
  activeFilesKb:  null,
  activeAccessKb: null,
  activeRolesUser: null,
  knownRoles:     null,   // lazy-loaded from GET /admin/roles, cached per session
  rolesEditors:   {},     // containerId -> { roles: [...] }
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

/* ── Tabs (Knowledge Bases / Users) ──────────────────────────── */
function switchAdminTab(tab) {
  document.getElementById('tab-kb').classList.toggle('active', tab === 'kb');
  document.getElementById('tab-users').classList.toggle('active', tab === 'users');
  document.getElementById('kb-panel').classList.toggle('hidden', tab !== 'kb');
  document.getElementById('users-panel').classList.toggle('hidden', tab !== 'users');
  document.getElementById('btn-new-kb').classList.toggle('hidden', tab !== 'kb');
  document.getElementById('btn-new-user').classList.toggle('hidden', tab !== 'users');
  if (tab === 'users') loadUsers();
}

/* ── Reusable role editor (used by KB access + user roles, both create
   and edit) ── A chip list previewing the current selection, plus an
   always-visible checklist of every role already in use (a permissions-
   picker grid, not a collapsed dropdown someone has to discover and open
   first) - multi-select by ticking more than one, a Select all/Clear all
   pair for bulk changes, and a text field underneath to add a brand-new
   role name. Not a fixed enum: typing a new role here makes it available in
   every other editor's checklist immediately after saving. */
function buildRolesEditor(containerId, initialRoles = []) {
  adminState.rolesEditors[containerId] = { roles: _normalizeRolesClient(initialRoles) };
  renderRolesEditor(containerId);
  loadKnownRoles(() => renderRolesPickerBody(containerId));
}

function _normalizeRolesClient(roles) {
  const seen = new Set();
  const out = [];
  for (const r of roles || []) {
    const v = (r || '').trim().toLowerCase();
    if (v && !seen.has(v)) { seen.add(v); out.push(v); }
  }
  return out;
}

function renderRolesEditor(containerId) {
  const st = adminState.rolesEditors[containerId];
  const container = document.getElementById(containerId);
  if (!container || !st) return;
  const chipsHtml = st.roles.length
    ? st.roles.map(r => `<span class="role-chip">${escHtml(r)}<button type="button" onclick="removeRoleFromEditor('${containerId}', '${escHtml(r).replace(/'/g, "&#39;")}')">&times;</button></span>`).join('')
    : '<span class="roles-chips-empty">All roles (unrestricted)</span>';
  container.innerHTML = `
    <div class="roles-chips">${chipsHtml}</div>
    <div class="roles-picker">
      <div class="roles-picker-header">
        <span class="roles-picker-count" id="${containerId}-count">Loading roles…</span>
        <div class="roles-picker-bulk">
          <button type="button" id="${containerId}-selectall" onclick="selectAllRolesInEditor('${containerId}')">Select all</button>
          <span class="roles-picker-bulk-sep">·</span>
          <button type="button" id="${containerId}-clearall" onclick="clearAllRolesInEditor('${containerId}')">Clear all</button>
        </div>
      </div>
      <div class="roles-dropdown-list" id="${containerId}-list"><div class="roles-dropdown-empty">Loading roles...</div></div>
      <div class="roles-dropdown-newrow">
        <input type="text" class="roles-input" id="${containerId}-input" placeholder="New role name" />
        <button type="button" class="admin-btn admin-btn-primary" onclick="addRoleFromInput('${containerId}')">Add</button>
      </div>
    </div>`;
  document.getElementById(`${containerId}-input`).addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addRoleFromInput(containerId); }
  });
  renderRolesPickerBody(containerId);
}

// Updates the checklist + its header (count, Select all/Clear all enabled
// state) without touching the chips row or re-attaching the input's
// listener - called after every toggle/bulk action/add, and once the known-
// roles list finishes loading async.
function renderRolesPickerBody(containerId) {
  const list = document.getElementById(`${containerId}-list`);
  const countEl = document.getElementById(`${containerId}-count`);
  if (!list || !countEl) return;
  const st = adminState.rolesEditors[containerId];
  const known = adminState.knownRoles || [];

  countEl.textContent = known.length
    ? `${st.roles.length} of ${known.length} role${known.length === 1 ? '' : 's'} selected`
    : 'No roles yet';
  const selectAllBtn = document.getElementById(`${containerId}-selectall`);
  const clearAllBtn = document.getElementById(`${containerId}-clearall`);
  if (selectAllBtn) selectAllBtn.disabled = known.length === 0 || st.roles.length === known.length;
  if (clearAllBtn) clearAllBtn.disabled = st.roles.length === 0;

  list.innerHTML = known.length
    ? known.map(r => {
        const checked = st.roles.includes(r);
        const safe = escHtml(r).replace(/'/g, "&#39;");
        return `
          <label class="roles-dropdown-item ${checked ? 'is-checked' : ''}">
            <input type="checkbox" ${checked ? 'checked' : ''} onchange="toggleRoleInEditor('${containerId}', '${safe}', this.checked)" />
            <span>${escHtml(r)}</span>
          </label>`;
      }).join('')
    : '<div class="roles-dropdown-empty">No roles yet - add one below.</div>';
}

function toggleRoleInEditor(containerId, role, isChecked) {
  const st = adminState.rolesEditors[containerId];
  if (isChecked) {
    if (!st.roles.includes(role)) st.roles.push(role);
  } else {
    st.roles = st.roles.filter(r => r !== role);
  }
  renderRolesChipsOnly(containerId);
  renderRolesPickerBody(containerId);
}

function selectAllRolesInEditor(containerId) {
  const st = adminState.rolesEditors[containerId];
  st.roles = _normalizeRolesClient(adminState.knownRoles || []);
  renderRolesChipsOnly(containerId);
  renderRolesPickerBody(containerId);
}

function clearAllRolesInEditor(containerId) {
  const st = adminState.rolesEditors[containerId];
  st.roles = [];
  renderRolesChipsOnly(containerId);
  renderRolesPickerBody(containerId);
}

// Re-renders just the chips row in place - toggling/bulk-selecting roles
// only needs this + renderRolesPickerBody, not a full renderRolesEditor
// (which would also tear down and rebuild the checklist and new-role input).
function renderRolesChipsOnly(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return;
  const st = adminState.rolesEditors[containerId];
  const chipsEl = container.querySelector('.roles-chips');
  if (!chipsEl) return;
  chipsEl.innerHTML = st.roles.length
    ? st.roles.map(r => `<span class="role-chip">${escHtml(r)}<button type="button" onclick="removeRoleFromEditor('${containerId}', '${escHtml(r).replace(/'/g, "&#39;")}')">&times;</button></span>`).join('')
    : '<span class="roles-chips-empty">All roles (unrestricted)</span>';
}

function addRoleFromInput(containerId) {
  const input = document.getElementById(`${containerId}-input`);
  const v = (input.value || '').trim().toLowerCase();
  if (!v) return;
  const st = adminState.rolesEditors[containerId];
  if (!st.roles.includes(v)) st.roles.push(v);
  if (!adminState.knownRoles) adminState.knownRoles = [];
  if (!adminState.knownRoles.includes(v)) adminState.knownRoles.push(v);
  renderRolesChipsOnly(containerId);
  renderRolesPickerBody(containerId);
  input.value = '';
  input.focus();
}

function removeRoleFromEditor(containerId, role) {
  const st = adminState.rolesEditors[containerId];
  st.roles = st.roles.filter(r => r !== role);
  renderRolesChipsOnly(containerId);
  renderRolesPickerBody(containerId);
}

function getEditorRoles(containerId) {
  return (adminState.rolesEditors[containerId]?.roles) || [];
}

async function loadKnownRoles(onLoaded) {
  if (adminState.knownRoles === null) {
    try {
      const res = await fetch('/admin/roles', { headers: authHeaders() });
      adminState.knownRoles = res.ok ? await res.json() : [];
    } catch (_) { adminState.knownRoles = []; }
  }
  onLoaded();
}

/* ── Users list ───────────────────────────────────────────────── */
async function loadUsers() {
  const tbody = document.getElementById('users-table-body');
  tbody.innerHTML = `<tr><td colspan="4">Loading...</td></tr>`;
  try {
    const res = await fetch('/admin/users', { headers: authHeaders() });
    if (handleAuthError(res)) return;
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || 'Failed to load users');
    }
    adminState.users = await res.json();
    renderUsersTable();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="4">${escHtml(e.message)}</td></tr>`;
  }
}

// Initials shown in the user avatar chip - from the display name when set,
// otherwise the first one or two characters of the email's local part.
function _userInitials(u) {
  const name = (u.name || '').trim();
  if (name) {
    const parts = name.split(/\s+/).filter(Boolean);
    return ((parts[0]?.[0] || '') + (parts[1]?.[0] || '')).toUpperCase() || name[0].toUpperCase();
  }
  const local = (u.email || '').split('@')[0];
  return (local.slice(0, 2) || '?').toUpperCase();
}

function renderUsersTable() {
  const tbody = document.getElementById('users-table-body');
  const empty = document.getElementById('users-empty');
  const table = document.getElementById('users-table');
  if (adminState.users.length === 0) {
    table.classList.add('hidden');
    empty.classList.remove('hidden');
    return;
  }
  table.classList.remove('hidden');
  empty.classList.add('hidden');
  tbody.innerHTML = adminState.users.map(u => {
    const created = u.created_at ? new Date(u.created_at).toLocaleDateString() : '-';
    const rolesHtml = u.roles.length
      ? u.roles.map(r => `<span class="role-chip role-chip--readonly">${escHtml(r)}</span>`).join('')
      : '<span class="roles-chips-empty">No roles</span>';
    return `
      <tr>
        <td>
          <div class="user-cell">
            <span class="user-avatar">${escHtml(_userInitials(u))}</span>
            <div class="user-cell-text">
              <span class="user-cell-name">${escHtml(u.name || u.email)}</span>
              ${u.name ? `<span class="user-cell-email">${escHtml(u.email)}</span>` : ''}
            </div>
          </div>
        </td>
        <td><div class="roles-chips roles-chips--inline">${rolesHtml}</div></td>
        <td>${created}</td>
        <td><button class="admin-btn" onclick="openUserRolesModal('${escHtml(u.email).replace(/'/g, "&#39;")}')">Edit roles</button></td>
      </tr>`;
  }).join('');
}

function openUserRolesModal(email) {
  adminState.activeRolesUser = email;
  const user = adminState.users.find(u => u.email === email);
  document.getElementById('user-roles-modal-title').textContent = `Edit roles - ${email}`;
  document.getElementById('user-roles-error').classList.add('hidden');
  buildRolesEditor('user-roles-editor', user ? user.roles : []);
  document.getElementById('user-roles-modal-backdrop').classList.remove('hidden');
}
function closeUserRolesModal() {
  document.getElementById('user-roles-modal-backdrop').classList.add('hidden');
  adminState.activeRolesUser = null;
}

async function saveUserRoles() {
  const email = adminState.activeRolesUser;
  if (!email) return;
  const errEl = document.getElementById('user-roles-error');
  errEl.classList.add('hidden');
  try {
    const res = await fetch(`/admin/users/${encodeURIComponent(email)}/roles`, {
      method: 'PUT',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ roles: getEditorRoles('user-roles-editor') }),
    });
    const data = await res.json().catch(() => ({}));
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error(data.detail || 'Failed to update roles');
    adminState.knownRoles = null;  // new role names may now be in use - refresh suggestions next time
    closeUserRolesModal();
    await loadUsers();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  }
}

/* ── Create user modal ───────────────────────────────────────── */
function openCreateUserModal() {
  document.getElementById('user-email').value = '';
  document.getElementById('user-name').value = '';
  document.getElementById('user-password').value = '';
  document.getElementById('create-user-error').classList.add('hidden');
  buildRolesEditor('user-create-roles-editor', []);
  document.getElementById('create-user-modal-backdrop').classList.remove('hidden');
}
function closeCreateUserModal() {
  document.getElementById('create-user-modal-backdrop').classList.add('hidden');
}

async function handleCreateUser(event) {
  event.preventDefault();
  const email = document.getElementById('user-email').value.trim();
  const name = document.getElementById('user-name').value.trim();
  const password = document.getElementById('user-password').value;
  const errEl = document.getElementById('create-user-error');
  errEl.classList.add('hidden');

  try {
    const res = await fetch('/admin/users', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ email, password, name: name || null, roles: getEditorRoles('user-create-roles-editor') }),
    });
    const data = await res.json().catch(() => ({}));
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error(data.detail || 'Failed to create user');
    adminState.knownRoles = null;
    closeCreateUserModal();
    await loadUsers();
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  }
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

/* Replaces a single KB in local state from a mutation response and
 * re-renders - avoids a full /admin/knowledge-bases re-fetch + re-render
 * for what is a single-row update. */
function _patchKbInState(data) {
  const idx = adminState.knowledgeBases.findIndex(k => k.id === data.id);
  if (idx === -1) return;
  adminState.knowledgeBases[idx] = data;
  renderKbGrid();
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
    _patchKbInState(data);
  } catch (e) {
    alert(e.message);
  }
}

function renderKbGrid() {
  const grid = document.getElementById('kb-grid');
  const empty = document.getElementById('kb-empty');
  const stats = document.getElementById('kb-stats');
  const kbs = adminState.knowledgeBases;
  if (kbs.length === 0) {
    grid.innerHTML = '';
    empty.classList.remove('hidden');
    stats.classList.add('hidden');
    return;
  }
  empty.classList.add('hidden');
  stats.classList.remove('hidden');
  renderKbStats(kbs);
  grid.innerHTML = kbs.map(kbCardHtml).join('') + kbCreateTileHtml();
}

/* Computed live from the real KB list on every render - never hardcoded. */
function renderKbStats(kbs) {
  const total = kbs.length;
  const active = kbs.filter(kb => kb.status === 'active').length;
  const totalFiles = kbs.reduce((sum, kb) => sum + (kb.document_count || 0), 0);
  const roles = new Set();
  kbs.forEach(kb => (kb.allowed_roles || []).forEach(r => roles.add(r)));
  document.getElementById('kb-stat-total').textContent = total;
  document.getElementById('kb-stat-active').textContent = active;
  document.getElementById('kb-stat-files').textContent = totalFiles;
  document.getElementById('kb-stat-roles').textContent = roles.size;
}

function kbCreateTileHtml() {
  return `
    <button class="kb-create-tile" onclick="openCreateModal()" type="button">
      <span class="kb-create-tile-icon">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      </span>
      Create a new knowledge base
    </button>`;
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
        <span><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>${kb.document_count} file${kb.document_count === 1 ? '' : 's'}</span>
        <span><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>Created ${created}</span>
      </div>
      <div class="kb-access-row">
        <span class="kb-toggle-label">Access</span>
        <button class="kb-access-summary" onclick="openKbAccessModal('${kb.id}')" title="Click to edit which roles can see this knowledge base">
          ${(kb.allowed_roles && kb.allowed_roles.length)
            ? kb.allowed_roles.map(r => `<span class="role-chip role-chip--readonly">${escHtml(r)}</span>`).join('')
            : '<span class="roles-chips-empty">All roles</span>'}
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class="kb-access-edit-icon"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
        </button>
      </div>
      <div class="kb-toggle-row">
        <span class="kb-toggle-label">Active</span>
        <label class="kb-switch">
          <input type="checkbox" ${checked} onchange="toggleKbStatus('${kb.id}', this.checked)" />
          <span class="kb-switch-track"></span>
        </label>
      </div>
      <div class="kb-card-actions">
        <button class="admin-btn" onclick="openFilesModal('${kb.id}')"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>Manage files</button>
        <button class="admin-btn admin-btn-danger" onclick="deleteKb('${kb.id}', '${escHtml(kb.name)}')"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>Delete</button>
      </div>
    </div>`;
}

/* ── KB access (roles) modal ─────────────────────────────────── */
function openKbAccessModal(kbId) {
  adminState.activeAccessKb = kbId;
  const kb = adminState.knowledgeBases.find(k => k.id === kbId);
  document.getElementById('kb-access-modal-title').textContent = `Access - ${kb ? kb.name : ''}`;
  document.getElementById('kb-access-error').classList.add('hidden');
  buildRolesEditor('kb-access-roles-editor', kb ? kb.allowed_roles : []);
  document.getElementById('kb-access-modal-backdrop').classList.remove('hidden');
}
function closeKbAccessModal() {
  document.getElementById('kb-access-modal-backdrop').classList.add('hidden');
  adminState.activeAccessKb = null;
}

async function saveKbAccess() {
  const kbId = adminState.activeAccessKb;
  if (!kbId) return;
  const errEl = document.getElementById('kb-access-error');
  errEl.classList.add('hidden');
  try {
    const res = await fetch(`/admin/knowledge-bases/${kbId}`, {
      method: 'PATCH',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ allowed_roles: getEditorRoles('kb-access-roles-editor') }),
    });
    const data = await res.json().catch(() => ({}));
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error(data.detail || 'Failed to update access');
    adminState.knownRoles = null;
    closeKbAccessModal();
    _patchKbInState(data);
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.remove('hidden');
  }
}

async function toggleKbStatus(kbId, isActive) {
  // Optimistic update: flip the local status and re-render the stat card
  // immediately so it doesn't wait on the PATCH round-trip + full reload.
  const kb = adminState.knowledgeBases.find(k => k.id === kbId);
  const previousStatus = kb ? kb.status : null;
  if (kb) {
    kb.status = isActive ? 'active' : 'inactive';
    renderKbGrid();
  }
  try {
    const res = await fetch(`/admin/knowledge-bases/${kbId}`, {
      method: 'PATCH',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ status: isActive ? 'active' : 'inactive' }),
    });
    const data = await res.json().catch(() => null);
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error('Failed to update status');
    if (data) _patchKbInState(data);
  } catch (e) {
    if (kb) kb.status = previousStatus;
    renderKbGrid();
    alert(e.message);
  }
}

async function deleteKb(kbId, name) {
  if (!confirm(`Delete "${name}"? This removes the knowledge base and its file records.`)) return;
  try {
    const res = await fetch(`/admin/knowledge-bases/${kbId}`, { method: 'DELETE', headers: authHeaders() });
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error('Failed to delete');
    adminState.knowledgeBases = adminState.knowledgeBases.filter(k => k.id !== kbId);
    renderKbGrid();
  } catch (e) {
    alert(e.message);
  }
}

/* ── Create KB modal ─────────────────────────────────────────── */
function openCreateModal() {
  document.getElementById('kb-name').value = '';
  document.getElementById('kb-description').value = '';
  document.getElementById('create-kb-error').classList.add('hidden');
  buildRolesEditor('kb-create-roles-editor', []);
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
      body: JSON.stringify({
        name, description: description || null,
        allowed_roles: getEditorRoles('kb-create-roles-editor'),
      }),
    });
    const data = await res.json();
    if (handleAuthError(res)) return;
    if (!res.ok) throw new Error(data.detail || 'Failed to create knowledge base');
    adminState.knownRoles = null;
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
  document.getElementById('files-item-id').value = '';
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
        <td>${f.item_id ? escHtml(f.item_id) : '<span class="files-hint">-</span>'}</td>
        <td>
          v${f.version ?? 1}${f.is_superseded ? ' <span class="files-hint">(older)</span>' : ''}
          <button class="file-history-btn" title="View version history" onclick="viewFileHistory('${kbId}', '${escHtml(f.filename).replace(/'/g, "&#39;")}', ${f.item_id ? `'${escHtml(f.item_id).replace(/'/g, "&#39;")}'` : 'null'})">history</button>
        </td>
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

async function viewFileHistory(kbId, filename, itemId) {
  try {
    const url = itemId
      ? `/admin/knowledge-bases/${kbId}/items/${encodeURIComponent(itemId)}/versions`
      : `/admin/knowledge-bases/${kbId}/documents/${encodeURIComponent(filename)}/versions`;
    const res = await fetch(url, { headers: authHeaders() });
    if (handleAuthError(res)) return;
    const versions = await res.json();
    if (!versions.length) { alert('No version history found.'); return; }

    const lines = versions.map(v =>
      `v${v.version}${v.is_superseded ? ' (superseded)' : ' (current)'} - ${v.filename} - uploaded ${v.uploaded_at ? new Date(v.uploaded_at).toLocaleString() : 'unknown'}`
    );

    if (versions.length < 2) {
      alert(`Only one version on file:\n\n${lines.join('\n')}`);
      return;
    }

    const latest = versions[versions.length - 1];
    const previous = versions[versions.length - 2];
    const diffRes = await fetch(
      `/admin/knowledge-bases/documents/${latest.id}/diff?against=${previous.id}`,
      { headers: authHeaders() }
    );
    if (!diffRes.ok) {
      alert(`Version history:\n\n${lines.join('\n')}\n\n(Diff unavailable.)`);
      return;
    }
    const diff = await diffRes.json();
    const diffLines = diff.lines
      .filter(l => l.type !== 'unchanged')
      .slice(0, 40)
      .map(l => `${l.type === 'added' ? '+' : '-'} ${l.text}`);
    alert(
      `Version history:\n\n${lines.join('\n')}\n\n` +
      `Diff (v${previous.version} -> v${latest.version}), ${diff.lines_added} added / ${diff.lines_removed} removed:\n\n` +
      (diffLines.length ? diffLines.join('\n') : '(no line-level changes)')
    );
  } catch (e) {
    alert('Failed to load version history.');
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

  const itemId = (document.getElementById('files-item-id').value || '').trim();
  if (itemId && fileList.length > 1) {
    alert(
      'An Item ID identifies one logical document. Upload one file at a time ' +
      'when using an Item ID, or clear it to upload multiple unrelated files.'
    );
    return;
  }

  const formData = new FormData();
  for (const f of fileList) formData.append('files', f);
  // item_ids must align 1:1 with files by position - the backend ignores
  // the whole list if the counts don't match. The same item_id (if any) is
  // applied to every file in this batch; leave the field blank to fall
  // back to filename-based matching.
  if (itemId) {
    for (let i = 0; i < fileList.length; i++) formData.append('item_ids', itemId);
  }

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
