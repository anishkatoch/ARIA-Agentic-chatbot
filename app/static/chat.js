/* ── Auth guard - redirect to login if no token ──────────────── */
(function() {
  if (!localStorage.getItem('rag_auth_token')) {
    window.location.replace('/auth.html');
  }
})();

/* ── State ───────────────────────────────────────────────────── */
const state = {
  sessionId:    null,
  clientToken:  null,
  activeTab:    'files',
  files:        [],
  isProcessing: false,
  isReady:      true,
  advancedMode: false,
  thinkingMode: false,
  authToken:    null,
  authUser:     null,
  connectorsLoaded: false,
  connectors:   [],
  connectorsUnlockToken: null,
  selectedKnowledgeBaseId: null,
  knowledgeBases: [],
  activeSidebarPanel: 'upload',
};
window._ragState = state; // expose for testing

/* ── Init ────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  initAuth();
  initClientToken();
  initAdvancedMode();
  initThinkingMode();
  setupTabs();
  setupDragDrop();
  setupFileInput();
  setupHeadersToggle();
  checkOAuthReturn();
  restoreSession();
});

/* ── Sidebar nav - opens the side drawer on whichever panel was clicked ───
   The drawer overlays the chat area (like Claude Code's own side panels)
   instead of the panel content being squeezed inline below the rail icons.
   Clicking the already-open panel's icon again closes the drawer. */
const SIDEBAR_PANELS = {
  upload:     { id: 'upload-section',     title: 'Upload a Document', wide: false },
  connectors: { id: 'connectors-section', title: 'Connect a Source',  wide: true },
  knowledge:  { id: 'knowledge-section',  title: 'Knowledge Base',    wide: true },
  settings:   { id: 'settings-section',   title: 'Settings',          wide: true },
};

function showSidebarPanel(name) {
  const drawer = document.getElementById('side-drawer');
  const alreadyOpenOnThis = !drawer.classList.contains('hidden') && state.activeSidebarPanel === name;
  if (alreadyOpenOnThis) {
    closeSidebarDrawer();
    return;
  }

  Object.entries(SIDEBAR_PANELS).forEach(([key, cfg]) => {
    document.getElementById(cfg.id).classList.toggle('hidden', key !== name);
  });
  document.querySelectorAll('.sidebar-nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.panel === name);
  });
  document.getElementById('side-drawer-title').textContent = SIDEBAR_PANELS[name].title;
  drawer.classList.toggle('side-drawer--wide', !!SIDEBAR_PANELS[name].wide);
  // "Process & Chat" only applies to the Upload tab's staged files - picking
  // a connector item ingests immediately on click, and knowledge base /
  // settings panels have nothing to "process" at all. Showing it there was
  // a dead button that looked broken when clicked.
  document.getElementById('process-btn').classList.toggle('hidden', name !== 'upload');

  drawer.classList.remove('hidden');
  state.activeSidebarPanel = name;

  if (name === 'connectors') {
    if (!state.connectorsUnlockToken) {
      showConnectorsUnlockPrompt();
    } else if (!state.connectorsLoaded) {
      state.connectorsLoaded = true;
      loadConnectors();
    }
  } else if (name === 'knowledge') {
    // Always refetch (not just the first time) - an admin can add a new
    // knowledge base or finish processing files into one at any point while
    // this tab is already open, and a one-time-only load would keep showing
    // the stale list until the page is hard-refreshed.
    loadKnowledgeBases();
  }
}

/* ── "Which documents are attached" popover (session badge) ─────── */
async function toggleAttachedDocsPopover(evt) {
  if (evt) evt.stopPropagation();
  const pop = document.getElementById('attached-docs-popover');
  const opening = pop.classList.contains('hidden');
  if (!opening) { pop.classList.add('hidden'); return; }

  pop.classList.remove('hidden');
  const list = document.getElementById('attached-docs-list');
  list.innerHTML = '<div class="attached-docs-empty">Loading…</div>';

  try {
    let docs = [];
    if (state.selectedKnowledgeBaseId) {
      const res = await fetch(`/knowledge-bases/${state.selectedKnowledgeBaseId}/files`, { headers: authHeaders() });
      if (res.ok) docs = await res.json();
    } else if (state.sessionId) {
      const res = await fetch(`/upload/session-documents?session_id=${encodeURIComponent(state.sessionId)}`, { headers: authHeaders() });
      if (res.ok) docs = await res.json();
    }
    list.innerHTML = docs.length
      ? docs.map(d => `<div class="attached-docs-row">${escHtml(d.filename)}</div>`).join('')
      : '<div class="attached-docs-empty">No documents attached yet.</div>';
  } catch (e) {
    list.innerHTML = '<div class="attached-docs-empty">Couldn\'t load the document list.</div>';
  }
}

document.addEventListener('click', (evt) => {
  const pop = document.getElementById('attached-docs-popover');
  const badge = document.getElementById('session-badge');
  if (pop && !pop.classList.contains('hidden') && badge && !badge.contains(evt.target)) {
    pop.classList.add('hidden');
  }
});

/* ── Composer "+" menu (quick attach, right next to the message box) ── */
function toggleComposerMenu(evt) {
  if (evt) evt.stopPropagation();
  const menu = document.getElementById('composer-menu');
  const btn = document.getElementById('composer-plus-btn');
  const opening = menu.classList.contains('hidden');
  menu.classList.toggle('hidden', !opening);
  btn.classList.toggle('active', opening);
}

function composerMenuAction(panel) {
  document.getElementById('composer-menu').classList.add('hidden');
  document.getElementById('composer-plus-btn').classList.remove('active');
  showSidebarPanel(panel);
}

// "Add URL" opens the Upload panel straight on its URL tab - that tab no
// longer has its own button in the Files/API bar since it's reached from
// here instead.
function composerAddUrl() {
  document.getElementById('composer-menu').classList.add('hidden');
  document.getElementById('composer-plus-btn').classList.remove('active');
  showSidebarPanel('upload');
  switchUploadTab('url');
}

document.addEventListener('click', (evt) => {
  const menu = document.getElementById('composer-menu');
  const attach = document.querySelector('.composer-attach');
  if (menu && !menu.classList.contains('hidden') && attach && !attach.contains(evt.target)) {
    menu.classList.add('hidden');
    document.getElementById('composer-plus-btn').classList.remove('active');
  }
});

/* ── Composer attachments popover (paperclip, next to the "+" button) ──
   Lets the user see every document attached to the CURRENT chat and
   detach ("x") any one of them, unlike the read-only header badge popover
   (toggleAttachedDocsPopover above). */
async function toggleComposerAttachmentsPopover(evt) {
  if (evt) evt.stopPropagation();
  const pop = document.getElementById('composer-attachments-popover');
  const opening = pop.classList.contains('hidden');
  if (!opening) { pop.classList.add('hidden'); return; }
  pop.classList.remove('hidden');
  await loadComposerAttachments();
}

async function loadComposerAttachments() {
  const list = document.getElementById('composer-attachments-list');
  list.innerHTML = '<div class="attached-docs-empty">Loading…</div>';

  // A knowledge base (admin-curated, shared) was still falling through to
  // the "no documents attached" empty state here, even though it's exactly
  // what the chat is actually answering from - this popover only ever
  // checked state.sessionId, never state.selectedKnowledgeBaseId (unlike
  // the other attached-docs popover, toggleAttachedDocsPopover, which
  // already handled both cases).
  if (state.selectedKnowledgeBaseId) {
    try {
      const res = await fetch(`/knowledge-bases/${state.selectedKnowledgeBaseId}/files`, { headers: authHeaders() });
      const docs = res.ok ? await res.json() : [];
      // Shared KB documents aren't removable by the user here - no "x" button.
      list.innerHTML = docs.length
        ? docs.map(d => `<div class="attached-docs-row">${escHtml(d.filename)}</div>`).join('')
        : '<div class="attached-docs-empty">No documents in this knowledge base yet.</div>';
    } catch (e) {
      list.innerHTML = '<div class="attached-docs-empty">Couldn\'t load the document list.</div>';
    }
    return;
  }

  if (!state.sessionId) {
    list.innerHTML = '<div class="attached-docs-empty">No documents attached yet.</div>';
    return;
  }
  try {
    const res = await fetch(`/upload/session-documents?session_id=${encodeURIComponent(state.sessionId)}`, { headers: authHeaders() });
    const docs = res.ok ? await res.json() : [];
    list.innerHTML = docs.length
      ? docs.map(d => `
          <div class="attached-docs-row attached-docs-row--removable">
            <span class="attached-docs-name">${escHtml(d.filename)}</span>
            <button class="attached-docs-remove" onclick="detachSessionDocument('${escAttr(d.filename)}')"
                    title="Remove from this chat" aria-label="Remove ${escAttr(d.filename)}">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
            </button>
          </div>`).join('')
      : '<div class="attached-docs-empty">No documents attached yet.</div>';
  } catch (e) {
    list.innerHTML = '<div class="attached-docs-empty">Couldn\'t load the document list.</div>';
  }
}

async function detachSessionDocument(filename) {
  if (!state.sessionId) return;
  try {
    const params = new URLSearchParams({ session_id: state.sessionId, filename });
    const res = await fetch(`/upload/session-documents?${params}`, { method: 'DELETE', headers: authHeaders() });
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    toast(`${filename} removed from this chat`, 'success');
    await loadComposerAttachments();
  } catch (e) {
    toast(e.message, 'error');
  }
}

document.addEventListener('click', (evt) => {
  const pop = document.getElementById('composer-attachments-popover');
  const btn = document.getElementById('composer-attachments-btn');
  if (pop && !pop.classList.contains('hidden') && btn && !btn.contains(evt.target) && !pop.contains(evt.target)) {
    pop.classList.add('hidden');
  }
});

function closeSidebarDrawer() {
  document.getElementById('side-drawer').classList.add('hidden');
  document.querySelectorAll('.sidebar-nav-btn').forEach(btn => btn.classList.remove('active'));
  state.activeSidebarPanel = null;
}

// "Home" nav button - unlike the other rail buttons, it doesn't open a
// panel, it just closes whichever one is open so the chat area underneath
// is visible again. Also collapses the sidebar on mobile/narrow layouts
// where it's expanded, same as picking a connector/upload action does.
function goToChatHome() {
  closeSidebarDrawer();
  const sidebar = document.getElementById('sidebar');
  const hamburger = document.getElementById('sidebar-hamburger');
  if (sidebar.classList.contains('collapsed') === false && window.innerWidth <= 768) {
    sidebar.classList.add('collapsed');
    hamburger.classList.add('collapsed');
  }
  scrollToBottom();
}

/* ── Knowledge bases (admin-curated, shared document sets) ──────── */
async function loadKnowledgeBases() {
  try {
    const res = await fetch('/knowledge-bases', { headers: authHeaders() });
    if (!res.ok) return;
    state.knowledgeBases = await res.json();
    renderKbList();
  } catch (e) {
    console.warn('Failed to load knowledge bases', e);
    const list = document.getElementById('kb-list');
    if (list) list.innerHTML = `<div class="kb-list-empty">Couldn't load knowledge bases.</div>`;
  }
}

function renderKbList() {
  const list = document.getElementById('kb-list');
  if (!list) return;
  const rows = [
    { id: '', name: 'My uploaded documents', description: 'Files, URLs, and connectors you add yourself' },
    ...state.knowledgeBases,
  ];
  list.innerHTML = rows.map(kb => {
    const isActive = (state.selectedKnowledgeBaseId || '') === kb.id;
    const safeName = escHtml(kb.name).replace(/'/g, "&#39;");
    // Every row (including "My uploaded documents") gets an explicit
    // Attach/Detach button - no relying on clicking the row itself, since
    // that wasn't discoverable as an action.
    const actionBtn = isActive
      ? (kb.id ? `<button class="kb-row-action detach" onclick="event.stopPropagation(); detachKnowledgeBase();">Detach</button>`
                : `<span class="kb-list-row-attached">Active</span>`)
      : `<button class="kb-row-action attach" onclick="event.stopPropagation(); selectKnowledgeBase('${kb.id}', '${safeName}');">Attach</button>`;
    return `
    <div class="kb-list-row ${isActive ? 'active' : ''}">
      <div class="kb-list-row-top">
        <div class="kb-list-row-name">${escHtml(kb.name)}</div>
        ${actionBtn}
      </div>
      ${kb.description ? `<div class="kb-list-row-desc">${escHtml(kb.description)}</div>` : ''}
    </div>`;
  }).join('');
}

function selectKnowledgeBase(id, name) {
  state.selectedKnowledgeBaseId = id || null;
  renderKbList();

  const badge      = document.getElementById('session-badge');
  const detachBtn  = document.getElementById('kb-detach-btn');
  badge.classList.remove('hidden');
  document.getElementById('session-label').textContent = id ? name : 'Session Active';
  detachBtn.classList.toggle('hidden', !id);
  toast(id ? `Now chatting with "${name}"` : 'Switched to your uploaded documents', 'success');
}

// Explicitly disconnects the active knowledge base - chat falls back to the
// user's own uploaded documents, same effect as selecting that row, just
// reachable with one click from the header instead of opening the panel.
function detachKnowledgeBase() {
  if (!state.selectedKnowledgeBaseId) return;
  selectKnowledgeBase('', 'My uploaded documents');
  toast('Knowledge base detached', 'info');
}

// The OAuth callback (gmail/google-drive) redirects back here
// with ?connected=<provider> instead of leaving the browser on a bare JSON
// page - this reopens the connectors panel straight to the fresh,
// already-connected status, instead of the user having to navigate back and
// manually reload it themselves.
function checkOAuthReturn() {
  const params = new URLSearchParams(location.search);
  const provider = params.get('connected');
  if (!provider) return;

  const url = new URL(location.href);
  url.searchParams.delete('connected');
  history.replaceState({}, '', url);

  showSidebarPanel('connectors');
  toast(`${provider.replace(/_/g, ' ')} connected!`, 'success');
}

function initTheme() {
  const dark = localStorage.getItem('aria_dark_mode') === 'true';
  if (dark) document.documentElement.classList.add('dark');

  const btn = document.createElement('button');
  btn.className = 'theme-toggle';
  btn.title = 'Toggle dark mode';
  btn.innerHTML = dark ? sunIcon() : moonIcon();
  btn.addEventListener('click', () => {
    const isDark = document.documentElement.classList.toggle('dark');
    localStorage.setItem('aria_dark_mode', isDark);
    btn.innerHTML = isDark ? sunIcon() : moonIcon();
  });
  document.querySelector('.header-right').appendChild(btn);
}

function moonIcon() {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>`;
}
function sunIcon() {
  return `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/></svg>`;
}

function initAuth() {
  state.authToken = localStorage.getItem('rag_auth_token');
  state.connectorsUnlockToken = localStorage.getItem('rag_connectors_unlock_token');
  try {
    state.authUser = JSON.parse(localStorage.getItem('rag_auth_user') || 'null');
  } catch (_) { state.authUser = null; }

  if (!state.authToken) { window.location.replace('/auth.html'); return; }

  const user = state.authUser;
  if (!user) return;

  // Inject user chip into header
  const headerRight = document.querySelector('.header-right');
  if (headerRight) {
    const isGuest = user.role === 'guest';
    const initials = (user.name || user.email || 'U')
      .split(' ').map(p => p[0]).join('').toUpperCase().slice(0, 2);

    const chip = document.createElement('div');
    chip.className = 'user-chip';
    chip.innerHTML = `
      <div class="user-avatar-sm">${initials}</div>
      <div class="user-chip-info">
        <span class="user-chip-name">${escHtml(user.name || user.email)}</span>
        ${isGuest ? '<span class="user-chip-role">Guest</span>' : ''}
      </div>
      <button class="logout-btn" onclick="logout()" title="Sign out">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/>
          <polyline points="16 17 21 12 16 7"/>
          <line x1="21" y1="12" x2="9" y2="12"/>
        </svg>
      </button>`;
    headerRight.appendChild(chip);
  }
}

// A Google/Microsoft sign-in leaves its own persisted session behind
// (written by Supabase's JS client on auth.html, under a separate
// "sb-...-auth-token" key) - if that isn't cleared too, auth.html's
// initOAuth() finds it still valid on the very next load and silently
// logs the user back in, making "Sign Out" look like it does nothing.
function clearAuthTokens() {
  localStorage.removeItem('rag_auth_token');
  localStorage.removeItem('rag_auth_user');
  try {
    Object.keys(localStorage)
      .filter(k => k.startsWith('sb-') && k.endsWith('-auth-token'))
      .forEach(k => localStorage.removeItem(k));
  } catch (_) {}
}

function logout() {
  clearAuthTokens();
  window.location.replace('/auth.html');
}

/* ── Auth headers helper ─────────────────────────────────────── */
function authHeaders(extra = {}) {
  return {
    ...(state.authToken ? { 'Authorization': `Bearer ${state.authToken}` } : {}),
    ...extra,
  };
}

// Every /connectors/* call needs this on top of the normal auth header - the
// server rejects anything under that prefix without a valid unlock token,
// regardless of how valid the Bearer token is (see app/connectors/access_gate.py).
function connectorHeaders(extra = {}) {
  return authHeaders({
    ...(state.connectorsUnlockToken ? { 'X-Connectors-Unlock': state.connectorsUnlockToken } : {}),
    ...extra,
  });
}

// A 401 from /connectors/* doesn't always mean the user's own app session
// expired - every connector client (gmail.py, outlook.py, google_drive.py,
// etc.) also raises plain 401s for "this provider isn't connected" or "its
// token expired", which only means that ONE connector needs reconnecting,
// not that the whole chat session is dead. Only app/main.py's auth
// middleware raises the two exact messages checked below; everything else
// under 401 is connector-specific and must fall through to the caller's own
// error handling instead of logging the user out of the whole app.
async function handleAuthError(res) {
  if (res.status !== 401) return false;
  let detail = '';
  try {
    detail = (await res.clone().json())?.detail || '';
  } catch (_) {}
  if (detail !== 'Authentication required' && detail !== 'Invalid or expired token') {
    return false;
  }
  clearAuthTokens();
  window.location.replace('/auth.html');
  return true;
}

function _uuid() {
  try { return crypto.randomUUID(); } catch (_) {}
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  });
}

function initClientToken() {
  let token = localStorage.getItem('rag_client_token');
  if (!token) {
    try {
      token = crypto.randomUUID();
    } catch (_) {
      token = 'uid-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
    }
    localStorage.setItem('rag_client_token', token);
  }
  state.clientToken = token;
}

// Restores the last active session (if any) after a page reload - without
// this, state.sessionId only ever lived in memory, so reloading the page
// (including the full-navigation round trip a connector's OAuth "Connect"
// does) silently orphaned every document uploaded so far: the next
// upload/ingest would start a brand new, unrelated session, making the
// previous one(s) disappear from "attached documents" and from chat even
// though they're still sitting untouched in the database. Called once on
// load, after initClientToken() (needs state.clientToken implicitly via
// authHeaders) and initAuth() (needs state.authToken).
async function restoreSession() {
  const saved = localStorage.getItem('rag_session_id');
  if (!saved) return;
  try {
    const res = await fetch(`/upload/session-documents?session_id=${encodeURIComponent(saved)}`, { headers: authHeaders() });
    if (!res.ok) { localStorage.removeItem('rag_session_id'); return; }
    const docs = await safeJson(res);
    if (!Array.isArray(docs) || docs.length === 0) {
      // Nothing (or no longer anything) under this id - don't force the UI
      // into a "ready" state with nothing actually attached.
      localStorage.removeItem('rag_session_id');
      return;
    }

    state.sessionId = saved;
    state.isReady = true;
    document.getElementById('session-badge').classList.remove('hidden');
    document.getElementById('session-label').textContent = 'Session Active';
    document.getElementById('reset-btn').classList.remove('hidden');

    const input = document.getElementById('chat-input');
    const sendBtn = document.getElementById('send-btn');
    input.disabled = false;
    input.placeholder = 'Ask anything about your data...';
    sendBtn.disabled = false;
  } catch (_) {
    // Network hiccup while restoring - leave the session unattached rather
    // than risk marking the UI "ready" with nothing actually confirmed.
  }
}

function initAdvancedMode() {
  state.advancedMode = localStorage.getItem('rag_advanced_mode') === 'true';
  const toggle = document.getElementById('advanced-toggle');
  if (toggle) {
    toggle.checked = state.advancedMode;
    // Label stays the short, static "Advanced" - the switch's own position
    // and color already communicate on/off, so spelling "Mode: ON/OFF" out
    // in text next to it too is redundant and reads as less polished.
    toggle.addEventListener('change', () => {
      state.advancedMode = toggle.checked;
      localStorage.setItem('rag_advanced_mode', state.advancedMode);
    });
  }
}

function initThinkingMode() {
  state.thinkingMode = localStorage.getItem('rag_thinking_mode') === 'true';
  const toggle = document.getElementById('thinking-toggle');
  if (toggle) {
    toggle.checked = state.thinkingMode;
    toggle.addEventListener('change', () => {
      state.thinkingMode = toggle.checked;
      localStorage.setItem('rag_thinking_mode', state.thinkingMode);
    });
  }
}

/* ── Tabs ────────────────────────────────────────────────────── */
function setupTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => switchUploadTab(tab.dataset.tab));
  });
}

// Shared by the visible Files/API tab buttons and by code that needs to land
// on a specific tab without a button for it (e.g. the composer's "Add URL"
// shortcut, which opens the Upload panel straight on the URL tab even
// though that tab no longer has its own button in the tab bar).
function switchUploadTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));
  state.activeTab = name;
}

/* ── Connectors access gate ──────────────────────────────────── */
// A second secret beyond normal login, required before any /connectors/*
// call succeeds (enforced server-side in app/connectors/access_gate.py) -
// connecting Gmail/GitHub/etc. is real access to someone's real
// third-party accounts, so it gets its own separate lock.
function showConnectorsUnlockPrompt() {
  document.getElementById('connector-modal-title').textContent = 'Connectors are locked';
  const body = document.getElementById('connector-modal-body');
  body.innerHTML = `
    <div class="connector-browse-note">Enter the connectors access key to use Gmail, GitHub, and the other source connectors.</div>
    <input type="password" id="connectors-unlock-input" class="text-input connectors-unlock-input" placeholder="Access key" autocomplete="off" />
    <button class="connectors-unlock-btn" id="connectors-unlock-submit" onclick="submitConnectorsUnlock()">Unlock</button>
    <div class="connector-browse-note" id="connectors-unlock-error"></div>
  `;
  document.getElementById('connector-modal').classList.remove('hidden');
  document.addEventListener('keydown', _connectorModalEscHandler);
  const input = document.getElementById('connectors-unlock-input');
  input.focus();
  input.addEventListener('keydown', e => { if (e.key === 'Enter') submitConnectorsUnlock(); });
}

async function submitConnectorsUnlock() {
  const input = document.getElementById('connectors-unlock-input');
  const errEl = document.getElementById('connectors-unlock-error');
  const key = input.value.trim();
  if (!key) return;
  errEl.textContent = '';
  errEl.classList.remove('is-error');
  try {
    const res = await fetch('/connectors/unlock', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ access_key: key }),
    });
    if (await handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    state.connectorsUnlockToken = data.unlock_token;
    localStorage.setItem('rag_connectors_unlock_token', data.unlock_token);
    closeConnectorModal();
    toast('Connectors unlocked', 'success');
    if (!state.connectorsLoaded) {
      state.connectorsLoaded = true;
      loadConnectors();
    }
  } catch (e) {
    errEl.textContent = e.message;
    errEl.classList.add('is-error');
  }
}

// Clears a stale/expired/wrong-user unlock token and re-prompts, used when a
// /connectors/* call comes back 403 even though the normal auth token is fine.
function handleConnectorsLockError(res) {
  if (res.status === 403) {
    state.connectorsUnlockToken = null;
    localStorage.removeItem('rag_connectors_unlock_token');
    state.connectorsLoaded = false;
    showConnectorsUnlockPrompt();
    return true;
  }
  return false;
}

/* ── Drag & Drop ─────────────────────────────────────────────── */
function setupDragDrop() {
  const zone    = document.getElementById('drop-zone');
  const overlay = document.getElementById('drag-overlay');
  let leaveTimer = null;

  const dropTitle = zone.querySelector('.drop-title');
  const origTitle = dropTitle ? dropTitle.textContent : '';

  function isFileDrag(e) {
    try { return [...e.dataTransfer.types].includes('Files'); } catch { return false; }
  }
  function processFiles(e) {
    const files = e.dataTransfer ? [...e.dataTransfer.files] : [];
    if (files.length > 0) addFiles(files);
  }
  function showDrag() {
    clearTimeout(leaveTimer);
    overlay.classList.remove('hidden');
    zone.classList.add('dragover');
    if (dropTitle) dropTitle.textContent = 'Release to drop';
  }
  function hideDrag() {
    clearTimeout(leaveTimer);
    overlay.classList.add('hidden');
    zone.classList.remove('dragover');
    if (dropTitle) dropTitle.textContent = origTitle;
  }

  // ── Single drop handler on document catches everything ───────
  document.addEventListener('dragenter', e => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    showDrag();
  });
  document.addEventListener('dragover', e => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  document.addEventListener('dragleave', e => {
    if (!isFileDrag(e)) return;
    leaveTimer = setTimeout(() => hideDrag(), 50);
  });
  document.addEventListener('drop', e => {
    if (!e.dataTransfer) return;
    e.preventDefault();
    e.stopPropagation();
    hideDrag();
    processFiles(e);
  });

  // ── Zone click → open file browser ───────────────────────────
  zone.addEventListener('click', e => {
    if (e.target.closest('.browse-btn')) return;
    document.getElementById('file-input').click();
  });
}

function setupFileInput() {
  document.getElementById('file-input').addEventListener('change', e => {
    addFiles([...e.target.files]);
    e.target.value = '';
  });
}

/* ── File management ─────────────────────────────────────────── */
const MAX_FILES      = 5;
const MAX_MB         = 15;
const MAX_SESSION_MB = 50;
const ALLOWED        = ['.pdf', '.doc', '.docx', '.txt', '.md', '.xlsx', '.xls', '.csv'];

function totalSizeBytes() {
  return state.files.reduce((sum, f) => sum + f.size, 0);
}

function addFiles(newFiles) {
  const allowed = newFiles.filter(f => {
    const ext = '.' + f.name.split('.').pop().toLowerCase();
    if (!ALLOWED.includes(ext)) {
      toast(`${f.name}: unsupported format (allowed: PDF, DOC, DOCX, TXT, MD, XLSX, XLS, CSV)`, 'error');
      return false;
    }
    if (f.size > MAX_MB * 1024 * 1024) {
      toast(`${f.name}: exceeds ${MAX_MB} MB per-file limit`, 'error');
      return false;
    }
    return true;
  });

  const combined = [...state.files, ...allowed];
  if (combined.length > MAX_FILES) {
    toast(`Maximum ${MAX_FILES} files per session - extra files skipped`, 'error');
    state.files = combined.slice(0, MAX_FILES);
  } else {
    state.files = combined;
  }

  const totalMB = totalSizeBytes() / (1024 * 1024);
  if (totalMB > MAX_SESSION_MB) {
    toast(`Total size ${totalMB.toFixed(1)} MB exceeds the ${MAX_SESSION_MB} MB session limit`, 'error');
    // Remove files until we're under the limit
    while (totalSizeBytes() > MAX_SESSION_MB * 1024 * 1024 && state.files.length > 0) {
      state.files.pop();
    }
  }

  renderFileList();
}

function removeFile(index) {
  state.files.splice(index, 1);
  renderFileList();
}

function renderFileList() {
  const list = document.getElementById('file-list');
  list.innerHTML = '';
  state.files.forEach((file, i) => {
    const ext  = file.name.split('.').pop().toUpperCase();
    const size = formatBytes(file.size);
    list.innerHTML += `
      <div class="file-chip">
        <div class="file-chip-icon">${ext}</div>
        <div class="file-chip-info">
          <div class="file-chip-name">${escHtml(file.name)}</div>
          <div class="file-chip-size">${size}</div>
        </div>
        <button class="file-chip-remove" onclick="removeFile(${i})">×</button>
      </div>`;
  });

  if (state.files.length > 0) {
    const usedMB  = totalSizeBytes() / (1024 * 1024);
    const pct     = Math.min(100, (usedMB / MAX_SESSION_MB) * 100).toFixed(1);
    const fillCls = pct >= 90 ? 'danger' : pct >= 70 ? 'warn' : '';
    list.innerHTML += `
      <div class="session-size-bar">
        <span>${usedMB.toFixed(1)} MB of ${MAX_SESSION_MB} MB</span>
        <div class="bar-track"><div class="bar-fill ${fillCls}" style="width:${pct}%"></div></div>
      </div>`;
  }
}

/* ── Headers ─────────────────────────────────────────────────── */
function setupHeadersToggle() {
  const btn   = document.getElementById('toggle-headers');
  const panel = document.getElementById('headers-panel');
  btn.addEventListener('click', () => {
    const open = !panel.classList.contains('hidden');
    panel.classList.toggle('hidden', open);
    btn.classList.toggle('open', !open);
  });
}

function addHeaderRow() {
  const row = document.createElement('div');
  row.className = 'header-row';
  row.innerHTML = `
    <input type="text" class="header-key" placeholder="Key (e.g. Authorization)" aria-label="Header key" />
    <input type="text" class="header-val" placeholder="Value" aria-label="Header value" />
    <button class="remove-row" onclick="removeHeaderRow(this)">×</button>`;
  document.getElementById('headers-list').appendChild(row);
}

function removeHeaderRow(btn) {
  btn.closest('.header-row').remove();
}

function collectHeaders() {
  const headers = {};
  document.querySelectorAll('.header-row').forEach(row => {
    const key = row.querySelector('.header-key').value.trim();
    const val = row.querySelector('.header-val').value.trim();
    if (key && val) headers[key] = val;
  });
  return Object.keys(headers).length ? headers : null;
}

/* ── Progress Card ───────────────────────────────────────────── */
class ProgressCard {
  constructor(title) {
    this._steps = {};
    const div = document.createElement('div');
    div.className = 'message bot';
    div.innerHTML = `
      <div class="bot-avatar">AR</div>
      <div>
        <div class="bubble progress-card">
          <div class="progress-title">${escHtml(title)}</div>
          <div class="progress-steps"></div>
          <div class="progress-total hidden"></div>
        </div>
      </div>`;
    clearWelcome();
    document.getElementById('messages').appendChild(div);
    scrollToBottom();
    this._titleEl  = div.querySelector('.progress-title');
    this._stepsEl  = div.querySelector('.progress-steps');
    this._totalEl  = div.querySelector('.progress-total');
  }

  setTitle(title) {
    this._titleEl.textContent = title;
  }

  addStep(stage, message) {
    if (this._steps[stage]) return;
    const row = document.createElement('div');
    row.className = 'progress-step running';
    row.innerHTML = `<span class="ps-icon"></span><span class="ps-label">${escHtml(message)}</span><span class="ps-timer">0.0s</span>`;
    this._stepsEl.appendChild(row);
    scrollToBottom();
    const startTime = Date.now();
    const timerEl = row.querySelector('.ps-timer');
    const interval = setInterval(() => {
      timerEl.textContent = ((Date.now() - startTime) / 1000).toFixed(1) + 's';
    }, 100);
    this._steps[stage] = { row, interval };
  }

  doneStep(stage, message, elapsed) {
    const s = this._steps[stage];
    if (!s) return;
    clearInterval(s.interval);
    s.row.className = 'progress-step done';
    s.row.querySelector('.ps-label').textContent = message;
    s.row.querySelector('.ps-timer').textContent = elapsed.toFixed(2) + 's';
    scrollToBottom();
  }

  errorStep(stage, message) {
    const s = this._steps[stage];
    if (!s) return;
    clearInterval(s.interval);
    s.row.className = 'progress-step error';
    s.row.querySelector('.ps-label').textContent = message;
    s.row.querySelector('.ps-timer').textContent = '';
  }

  addWarning(message) {
    const row = document.createElement('div');
    row.className = 'progress-step skipped';
    row.innerHTML = `<span class="ps-icon"></span><span class="ps-label">${escHtml(message)}</span>`;
    this._stepsEl.appendChild(row);
    scrollToBottom();
  }

  complete(totalElapsed, message = 'Document processed') {
    Object.values(this._steps).forEach(s => clearInterval(s.interval));
    this._totalEl.classList.remove('hidden');
    this._totalEl.textContent = `${message} - ${totalElapsed.toFixed(2)}s`;
    scrollToBottom();
  }

  fail(message) {
    Object.values(this._steps).forEach(s => clearInterval(s.interval));
    this._totalEl.classList.remove('hidden');
    this._totalEl.classList.add('error');
    this._totalEl.textContent = `Failed: ${message}`;
    scrollToBottom();
  }
}

/* ── SSE stream reader ───────────────────────────────────────── */
async function readSSEStream(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let sawTerminalEvent = false;

  const wrappedOnEvent = (ev) => {
    if (ev.type === 'complete' || ev.type === 'error') sawTerminalEvent = true;
    onEvent(ev);
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop();
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try { wrappedOnEvent(JSON.parse(line.slice(6))); } catch (_) {}
      }
    }
  }

  if (!sawTerminalEvent) {
    // Connection closed mid-processing (e.g. an unhandled server error) without
    // ever sending a complete/error event - surface it instead of hanging silently.
    onEvent({ type: 'error', message: 'Connection lost while processing. Please try again.' });
  }
}

function handleProgressEvent(card, ev) {
  if (ev.type === 'step') {
    if (ev.title) card.setTitle(ev.title);
    if (ev.status === 'start') card.addStep(ev.stage, ev.message);
    else if (ev.status === 'done') card.doneStep(ev.stage, ev.message, ev.elapsed ?? 0);
    else if (ev.status === 'error') card.errorStep(ev.stage, ev.message);
  } else if (ev.type === 'dedup_confirm') {
    handleDedupConfirm(card, ev);
  } else if (ev.type === 'warning') {
    card.addWarning(ev.message);
    toast(ev.message, 'warning');
  } else if (ev.type === 'complete') {
    card.setTitle('Process done');
    card.complete(ev.total_elapsed ?? 0, ev.completion_message || 'Document processed');
    onProcessed(ev.session_id);
    toast('Ready to chat!', 'success');
  } else if (ev.type === 'error') {
    card.fail(ev.message);
    toast(ev.message, 'error');
  }
}

/* ── Dedup confirmation ──────────────────────────────────────── */
function handleDedupConfirm(card, ev) {
  // Stop the "Checking…" spinner for this stage
  const prev = card._steps[ev.stage];
  if (prev) {
    clearInterval(prev.interval);
    prev.row.className = 'progress-step done';
    prev.row.querySelector('.ps-label').textContent = `${escHtml(ev.filename)} - duplicate found`;
    prev.row.querySelector('.ps-timer').textContent = '';
  }

  // Confirm card uses its own class to avoid .progress-step grid conflict
  const row = document.createElement('div');
  row.className = 'dedup-confirm-card';
  row.dataset.confirmToken = ev.confirm_token;
  row.innerHTML = `
    <div class="dedup-title">${escHtml(ev.filename)}</div>
    <div class="dedup-meta">${escHtml(ev.file_size)} · ${ev.chunks_stored || 0} chunks already stored</div>
    <div class="dedup-buttons">
      <button class="dedup-btn reuse" onclick="sendDedupAction('${ev.confirm_token}', 'reuse', this)">Use existing</button>
      <button class="dedup-btn reprocess" onclick="sendDedupAction('${ev.confirm_token}', 'reprocess', this)">Re-upload</button>
    </div>
    <div class="dedup-countdown">Auto-reprocessing in <span class="countdown-num">60</span>s</div>`;
  card._stepsEl.appendChild(row);
  scrollToBottom();

  // Store separately - do NOT overwrite card._steps[ev.stage].
  // doneStep() must still operate on the original "✓ duplicate found" row.
  card._confirmCards = card._confirmCards || {};
  card._confirmCards[ev.stage] = row;

  let remaining = 60;
  const countdownEl = row.querySelector('.countdown-num');
  const interval = setInterval(() => {
    remaining--;
    if (countdownEl) countdownEl.textContent = remaining;
    if (remaining <= 0) {
      clearInterval(interval);
      const cd = row.querySelector('.dedup-countdown');
      if (cd) { cd.className = 'dedup-countdown expired'; cd.textContent = 'Timed out - reprocessing'; }
    }
  }, 1000);
  row._dedupInterval = interval;
}

async function sendDedupAction(confirmToken, action, btn) {
  const row = btn.closest('.dedup-confirm-card');
  row.querySelectorAll('.dedup-btn').forEach(b => { b.disabled = true; });
  clearInterval(row._dedupInterval);

  try {
    const res = await fetch('/upload/confirm', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ confirm_token: confirmToken, action }),
    });
    if (!res.ok) {
      const data = await safeJson(res);
      toast(data?.detail || 'Confirmation failed', 'error');
    } else {
      row.remove();
    }
  } catch (e) {
    toast(e.message, 'error');
  }
}

/* ── Process source ──────────────────────────────────────────── */
async function processSource() {
  if (state.isProcessing) return;
  const tab = state.activeTab;

  if (tab === 'files') {
    if (state.files.length === 0) return toast('Please select at least one file', 'error');
    return processFiles();
  }

  if (tab === 'url') {
    const url = document.getElementById('url-input').value.trim();
    if (!url) return toast('Please enter a URL', 'error');
    return processUrl(url);
  }

  if (tab === 'api') {
    const url = document.getElementById('api-url-input').value.trim();
    if (!url) return toast('Please enter an API URL', 'error');
    const apiHeaders = collectHeaders();
    setProcessing(true);
    try {
      const res  = await fetch('/upload/api', { method: 'POST', headers: authHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ url, headers: apiHeaders, session_id: state.sessionId || undefined }) });
      const data = await safeJson(res);
      if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
      onProcessed(data.session_id);
      toast(data.message || 'Ready to chat!', 'success');
    } catch (err) {
      toast(err.message, 'error');
    } finally {
      setProcessing(false);
    }
  }
}

async function processFiles() {
  setProcessing(true);
  const card = new ProgressCard(`Processing ${state.files.length} file(s)`);
  const form = new FormData();
  state.files.forEach(f => form.append('files', f));

  try {
    const res = await fetch('/upload/files', {
      method: 'POST',
      body: form,
      headers: authHeaders({
        'X-Client-Token': state.clientToken || 'anonymous',
        ...(state.sessionId ? { 'X-Session-Id': state.sessionId } : {}),
      }),
    });
    if (!res.ok) {
      if (await handleAuthError(res)) return;
      const data = await safeJson(res);
      throw new Error(data?.detail || `Server error ${res.status}`);
    }
    await readSSEStream(res, (ev) => handleProgressEvent(card, ev));
  } catch (err) {
    card.fail(err.message);
    toast(err.message, 'error');
  } finally {
    setProcessing(false);
  }
}

async function processUrl(url) {
  setProcessing(true);
  const card = new ProgressCard('Processing URL');
  try {
    const res = await fetch('/upload/url', {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ url, session_id: state.sessionId || undefined }),
    });
    if (!res.ok) {
      if (await handleAuthError(res)) return;
      const data = await safeJson(res);
      throw new Error(data?.detail || `Server error ${res.status}`);
    }
    await readSSEStream(res, (ev) => handleProgressEvent(card, ev));
  } catch (err) {
    card.fail(err.message);
    toast(err.message, 'error');
  } finally {
    setProcessing(false);
  }
}

function onProcessed(sessionId) {
  state.sessionId = sessionId;
  state.isReady = true;
  // Persisted so a page reload (or the full-navigation round trip a
  // connector's OAuth "Connect" does) can resume this session instead of
  // orphaning it - see restoreSession().
  localStorage.setItem('rag_session_id', sessionId);
  // A fresh upload/connector ingest always means "chat with what I just
  // added" - clear any previously selected knowledge base so the new
  // session_id actually takes effect instead of being shadowed by a stale
  // knowledge_base_id still being sent on every /chat/ call.
  state.selectedKnowledgeBaseId = null;
  renderKbList();

  document.getElementById('session-badge').classList.remove('hidden');
  document.getElementById('session-label').textContent = 'Session Active';
  document.getElementById('reset-btn').classList.remove('hidden');

  const input  = document.getElementById('chat-input');
  const sendBtn = document.getElementById('send-btn');
  input.disabled  = false;
  input.placeholder = 'Ask anything about your data...';
  sendBtn.disabled = false;
  input.focus();

  clearWelcome();
  appendMessage('bot', "Your data is ready! I'm ARIA - ask me anything about it.");
}

/* ── Connectors ──────────────────────────────────────────────── */
// Brand-flavored glyphs so each connector reads like an app-directory icon
// (own simplified recreations, not exact trademarked artwork).
function _connectorSvg(inner, viewBox = '0 0 24 24') {
  return `<svg width="18" height="18" viewBox="${viewBox}">${inner}</svg>`;
}

const CONNECTOR_ICON = {


  // Simplified Octocat silhouette (the near-universal open-source GitHub mark).
  github: _connectorSvg('<path fill="#181717" d="M12 .5C5.73.5.5 5.73.5 12c0 5.08 3.29 9.39 7.86 10.91.58.1.79-.25.79-.56 0-.27-.01-1.17-.02-2.12-3.2.7-3.88-1.36-3.88-1.36-.53-1.33-1.29-1.69-1.29-1.69-1.05-.72.08-.71.08-.71 1.16.08 1.78 1.19 1.78 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.56-.29-5.26-1.28-5.26-5.69 0-1.26.45-2.29 1.19-3.09-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.18 1.18a11.06 11.06 0 0 1 5.8 0c2.2-1.49 3.17-1.18 3.17-1.18.64 1.59.24 2.76.12 3.05.74.8 1.19 1.83 1.19 3.09 0 4.42-2.71 5.39-5.29 5.68.42.36.78 1.08.78 2.18 0 1.57-.01 2.83-.01 3.22 0 .31.21.67.8.56C19.72 21.39 23 17.08 23 12 23 5.73 17.77.5 12 .5z"/>'),

  gmail: _connectorSvg('<path fill="#EA4335" d="M2 6.5A1.5 1.5 0 0 1 3.5 5h17A1.5 1.5 0 0 1 22 6.5v11a1.5 1.5 0 0 1-1.5 1.5h-17A1.5 1.5 0 0 1 2 17.5v-11z"/><path fill="#fff" d="m4 6 8 6 8-6"/><path fill="#EA4335" d="M4 6v11.3L9.5 12 4 6zm16 0-5.5 6L20 17.3V6z" opacity=".55"/>'),

  google_drive: _connectorSvg('<path fill="#0066DA" d="M7.7 2 1 13.8l3.35 5.8L11 7.8z"/><path fill="#00AC47" d="m4.35 19.6 3.35 5.8h13.3l-3.35-5.8z" transform="translate(0 -5.8)"/><path fill="#EA4335" d="M11 7.8 17.65 19.6H24.65L17.95 2z"/><path fill="#FFBA00" d="M4.35 19.6h13.3l-3.35-5.8H7.7z"/>', '0 0 26 26'),

  google_chat: _connectorSvg('<path fill="#00AC47" d="M12 2a10 10 0 0 0-8.5 15.2L2 22l4.9-1.5A10 10 0 1 0 12 2z"/><circle cx="8" cy="12" r="1.6" fill="#fff"/><circle cx="12" cy="12" r="1.6" fill="#fff"/><circle cx="16" cy="12" r="1.6" fill="#fff"/>'),

  outlook: _connectorSvg('<rect x="2" y="4" width="20" height="16" rx="2" fill="#0078D4"/><path fill="#fff" d="M4 6.5 12 13l8-6.5V8l-8 6-8-6z"/><circle cx="8.5" cy="14" r="3.2" fill="#28A8EA" transform="translate(0 2)"/>'),

  outlook_chat: _connectorSvg('<rect x="2" y="3" width="15" height="14" rx="2" fill="#5059C9"/><path fill="#fff" d="M5 6h9v2H5zM5 9.5h9v2H5z"/><path fill="#5059C9" d="M9 17h6l5 4v-4h-2v-5h-9z"/>'),

  sharepoint: _connectorSvg('<circle cx="9" cy="8" r="6" fill="#036C70"/><circle cx="16" cy="9" r="5" fill="#1A9BA1"/><circle cx="11" cy="16" r="5.5" fill="#37C6D0"/><path fill="#fff" d="M9.2 13.4h5.2a2 2 0 0 1 2 2v.3a4.1 4.1 0 0 1-4.1 4.1H9.2a1 1 0 0 1-1-1v-4.4a1 1 0 0 1 1-1z"/>'),

  teams: _connectorSvg('<path fill="#5059C9" d="M16.5 10.5a2.75 2.75 0 1 0 0-5.5 2.75 2.75 0 0 0 0 5.5zM20 11.5h-2.75a.75.75 0 0 0-.75.75v4.5A3.25 3.25 0 0 0 19.75 20 2.25 2.25 0 0 0 22 17.75V13.5a2 2 0 0 0-2-2z"/><circle cx="9.5" cy="7" r="3.5" fill="#7B83EB"/><path fill="#7B83EB" d="M14 12H5a2 2 0 0 0-2 2v3.5A4.5 4.5 0 0 0 7.5 22h1A5.5 5.5 0 0 0 14 16.5V13a1 1 0 0 0-1-1z"/>'),

  azure_devops: _connectorSvg('<path fill="#0078D4" d="M23 6.1 18 2v3l-9 2.3v7L4 16V9.3L1 10v7.2l5 2L18.1 15v3L23 13.9z"/>'),
};

async function loadConnectors() {
  const list = document.getElementById('connector-list');
  try {
    const res = await fetch('/connectors/status', { headers: connectorHeaders() });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    state.connectors = data.sources || [];
    renderConnectors();
  } catch (e) {
    list.innerHTML = `<div class="connector-empty">Couldn't load connectors: ${escHtml(e.message)}</div>`;
  }
}

function renderConnectors() {
  document.getElementById('connector-list').innerHTML =
    state.connectors.map(connectorRowHtml).join('');
}

function connectorRowHtml(c) {
  // For both oauth and byok, "connected" means THIS user has their own
  // credential on file - a clean two-state toggle, same shape for both:
  // not connected -> Connect only; connected -> Browse + Disconnect. To
  // switch accounts/keys, Disconnect first, then Connect again.
  const badge = c.connected
    ? '<span class="connector-badge connected">Connected</span>'
    : (c.configured
        ? ((c.auth === 'oauth' || c.auth === 'byok') ? '<span class="connector-badge not-connected">Not connected</span>' : '')
        : '<span class="connector-badge not-configured">Not configured</span>');

  let action;
  if (c.auth === 'oauth') {
    action = c.connected
      ? `<button class="connector-action-btn" onclick="toggleBrowse('${c.id}')">Browse</button>
         <button class="connector-action-btn secondary" onclick="disconnectOAuth('${c.path}')">Disconnect</button>`
      : `<button class="connector-action-btn" ${c.configured ? '' : 'disabled'} onclick="connectOAuth('${c.path}')">Connect</button>`;
  } else if (c.auth === 'byok') {
    action = c.connected
      ? `<button class="connector-action-btn" onclick="toggleBrowse('${c.id}')">Browse</button>
         <button class="connector-action-btn secondary" onclick="disconnectOAuth('${c.path}')">Disconnect</button>`
      : `<button class="connector-action-btn" onclick="openCredentialsForm('${c.id}')">Connect</button>`;
  } else {
    action = `<button class="connector-action-btn" ${c.configured ? '' : 'disabled'} onclick="toggleBrowse('${c.id}')">Browse</button>`;
  }

  return `
    <div class="connector-row">
      <div class="connector-row-main">
        <div class="connector-icon">${CONNECTOR_ICON[c.id] || escHtml(c.name.slice(0, 2).toUpperCase())}</div>
        <div class="connector-info">
          <div class="connector-name">${escHtml(c.name)}</div>
          <div class="connector-sub">${escHtml(c.type.replace(/_/g, ' '))}</div>
        </div>
        <div class="connector-row-actions">
          ${badge}
          ${action}
        </div>
      </div>
    </div>`;
}

async function connectOAuth(path) {
  try {
    const res = await fetch(`/connectors/${path}/auth/login`, { headers: connectorHeaders() });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    window.location.href = data.auth_url;
  } catch (e) {
    toast(e.message, 'error');
  }
}

// Clears the stored token for this provider, then reloads the list so the
// row flips back to its "Connect" state - clicking Connect again goes
// through the provider's account-chooser / re-login prompt (not a silent
// same-account reauth), so this is how a user switches which account is
// linked, not just how they unlink one.
async function disconnectOAuth(path) {
  try {
    const res = await fetch(`/connectors/${path}/disconnect`, { method: 'POST', headers: connectorHeaders() });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    toast('Disconnected', 'success');
    // Gmail and Google Chat share one Google OAuth grant server-side, so
    // disconnecting either unlinks both - a single-row patch would leave the
    // other one showing a stale "Connected" badge. Full /connectors/status
    // refetch keeps every row correct at the cost of one extra round-trip.
    await loadConnectors();
  } catch (e) {
    toast(e.message, 'error');
  }
}

// Opens a form (in the same modal Browse uses) for a byok connector's
// per-provider credential fields, from /status's credential_fields - so this
// one function handles GitHub (1 field) and Azure DevOps (2 fields)
// without hardcoding either of them.
function openCredentialsForm(id) {
  const meta = state.connectors.find(c => c.id === id);
  if (!meta || !meta.credential_fields) return;

  document.getElementById('connector-modal-title').textContent = `Connect your own ${meta.name}`;
  const fieldsHtml = meta.credential_fields.map(f => `
    <label class="field-label" for="cred-${f.key}">${escHtml(f.label)}</label>
    <input type="${f.type === 'password' ? 'password' : 'text'}" id="cred-${f.key}" class="text-input"
           placeholder="${escAttr(f.placeholder || '')}" autocomplete="off" />
  `).join('');
  document.getElementById('connector-modal-body').innerHTML = `
    <div class="connector-browse-note">Your key is stored for your account only and never shared with other users.</div>
    ${fieldsHtml}
    <button class="connector-submit-btn" onclick="submitCredentials('${id}')">Save &amp; Connect</button>
    <div class="connector-browse-note" id="cred-error"></div>
  `;

  document.getElementById('connector-modal').classList.remove('hidden');
  document.addEventListener('keydown', _connectorModalEscHandler);
}

async function submitCredentials(id) {
  const meta = state.connectors.find(c => c.id === id);
  if (!meta || !meta.credential_fields) return;
  const errEl = document.getElementById('cred-error');
  errEl.textContent = '';

  const body = {};
  for (const f of meta.credential_fields) {
    body[f.key] = document.getElementById(`cred-${f.key}`).value.trim();
  }

  try {
    const res = await fetch(`/connectors/${meta.path}/credentials`, {
      method: 'POST',
      headers: connectorHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    closeConnectorModal();
    toast('Connected', 'success');
    // Same as disconnectOAuth: the save call already told us this worked,
    // so update this one row in place instead of a full status refetch.
    meta.connected = true;
    meta.own_credentials = true;
    renderConnectors();
  } catch (e) {
    errEl.textContent = e.message;
  }
}

// Opens the browse popup for a connector. Each provider's existing browse
// functions are unchanged - they render into whatever element currently has
// id `connector-browse-<id>`, so we create that single container fresh inside
// the modal body each time it opens (only one connector is browsed at once).
function openConnectorModal(id) {
  const meta = state.connectors.find(c => c.id === id);
  document.getElementById('connector-modal-title').textContent = meta ? `Browse ${meta.name}` : 'Browse';

  const body = document.getElementById('connector-modal-body');
  body.innerHTML = `<div class="connector-browse-hint">Click a file below to add it to this chat instantly - no extra "Process" step needed.</div>` +
    `<div id="connector-browse-${id}"></div>`;

  document.getElementById('connector-modal').classList.remove('hidden');
  document.addEventListener('keydown', _connectorModalEscHandler);

  if (id === 'github') return browseGithubRepos();
  if (id === 'gmail') return browseGmailMessages();
  if (id === 'google_drive') return browseGoogleDriveRoot();
  if (id === 'google_chat') return browseGoogleChatSpaces();
  if (id === 'outlook') return browseOutlookMessages();
  if (id === 'outlook_chat') return browseOutlookChats();
  if (id === 'sharepoint') return browseSharepointSites();
  if (id === 'teams') return browseTeamsList();
  if (id === 'azure_devops') return browseAzureDevOpsProjects();
  body.innerHTML = '<div class="connector-browse-note">Browsing UI for this source isn\'t built yet - ' +
    'once you add real credentials for it, ask to have the picker wired up next. The API itself already ' +
    'works (see <code>/docs</code>).</div>';
}
// Kept as the name used by existing onclick="toggleBrowse(...)" attributes.
function toggleBrowse(id) {
  openConnectorModal(id);
}

function _connectorModalEscHandler(e) {
  if (e.key === 'Escape') closeConnectorModal();
}

function closeConnectorModal() {
  document.getElementById('connector-modal').classList.add('hidden');
  document.getElementById('connector-modal-body').innerHTML = '';
  document.removeEventListener('keydown', _connectorModalEscHandler);
}

/* ── Shared connector-browse fetch/render skeleton ───────────────
   Used by every non-paginated browse*() function below: shows "Loading…",
   fetches the given URL, checks for an auth error, parses the JSON
   response, and renders whatever HTML the caller's `render(data)` callback
   returns - the one thing (loading state, fetch+headers, auth check,
   JSON parse, ok-check, error catch) that was identically repeated in
   every browse function. Each connector keeps its own `render` callback
   verbatim (crumbs, row markup, empty-state text all differ per
   connector), so nothing about what gets displayed changes, only where
   the repeated plumbing around it lives. Gmail's and Google Drive's
   browsers aren't built on this - they support "load more" pagination
   (append mode, a running next-page-token), which is a genuinely
   different shape this skeleton doesn't try to force-fit. */
async function renderConnectorBrowse(elementId, url, render) {
  const el = document.getElementById(elementId);
  el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  try {
    const res = await fetch(url, { headers: connectorHeaders() });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    el.innerHTML = render(data);
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}


/* ── GitHub browser ──────────────────────────────────────────── */
async function browseGithubRepos() {
  await renderConnectorBrowse(
    'connector-browse-github',
    '/connectors/github/repos?per_page=100',
    data => (data.items || []).map(r => `
      <div class="connector-item-row is-folder" onclick="browseGithubContents('${escAttr(r.owner)}', '${escAttr(r.name)}', '${escAttr(r.default_branch)}', '')">
        <span class="ci-name">📁 ${escHtml(r.full_name)}</span>
        <span class="ci-meta">${r.private ? 'Private' : 'Public'}</span>
      </div>`).join('') || '<div class="connector-browse-note">No repositories</div>',
  );
}

async function browseGithubContents(owner, repo, ref, path) {
  const params = new URLSearchParams({ path });
  if (ref) params.set('ref', ref);
  await renderConnectorBrowse(
    'connector-browse-github',
    `/connectors/github/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/contents?${params}`,
    data => {
      const crumbs = [
        `<a class="crumb" onclick="browseGithubRepos()">Repos</a>`,
        `<a class="crumb" onclick="browseGithubContents('${escAttr(owner)}', '${escAttr(repo)}', '${escAttr(ref)}', '')">${escHtml(owner)}/${escHtml(repo)}</a>`,
      ];
      let acc = '';
      path.split('/').filter(Boolean).forEach(p => {
        acc = acc ? `${acc}/${p}` : p;
        crumbs.push(`<a class="crumb" onclick="browseGithubContents('${escAttr(owner)}', '${escAttr(repo)}', '${escAttr(ref)}', '${escAttr(acc)}')">${escHtml(p)}</a>`);
      });

      const rows = (data.items || []).map(item => item.type === 'dir'
        ? `<div class="connector-item-row is-folder" onclick="browseGithubContents('${escAttr(owner)}', '${escAttr(repo)}', '${escAttr(ref)}', '${escAttr(item.path)}')">
             <span class="ci-name">📁 ${escHtml(item.name)}</span>
           </div>`
        : `<div class="connector-item-row" onclick="ingestGithubFile('${escAttr(owner)}', '${escAttr(repo)}', '${escAttr(item.path)}', '${escAttr(ref)}', '${escAttr(item.name)}')">
             <span class="ci-name">📄 ${escHtml(item.name)}</span>
             <span class="ci-meta">${item.size != null ? formatBytes(item.size) : ''}</span>
           </div>`
      ).join('') || '<div class="connector-browse-note">Empty folder</div>';

      return `<div class="connector-breadcrumb">${crumbs.join(' / ')}</div>` + rows;
    },
  );
}

async function ingestGithubFile(owner, repo, path, ref, name) {
  await runConnectorIngest('github', { owner, repo, path, ref: ref || null }, name);
}

/* ── Gmail browser ────────────────────────────────────────────── */
let _gmailNextToken = null;
let _gmailLabel = 'inbox';
let _gmailSelected = new Set();   // message ids selected across the current label's loaded pages

const GMAIL_LABELS = [
  { id: 'inbox', name: 'Inbox' },
  { id: 'spam', name: 'Spam' },
  { id: 'promotions', name: 'Promotions' },
  { id: 'social', name: 'Social' },
  { id: 'updates', name: 'Updates' },
  { id: 'forums', name: 'Forums' },
  { id: 'sent', name: 'Sent' },
  { id: 'all', name: 'All Mail' },
];

function _gmailLabelTabsHtml() {
  return `<div class="gmail-label-tabs">` +
    GMAIL_LABELS.map(l => `<button class="gmail-label-tab${l.id === _gmailLabel ? ' active' : ''}" onclick="switchGmailLabel('${l.id}')">${escHtml(l.name)}</button>`).join('') +
    `</div>`;
}

function _gmailBulkControlsHtml() {
  const n = _gmailSelected.size;
  return `
    <div class="gmail-bulk-controls">
      <label class="gmail-select-all">
        <input type="checkbox" id="gmail-select-all-cb" onchange="toggleGmailSelectAllLoaded(this.checked)">
        Select all loaded
      </label>
      <button class="connector-action-btn" ${n ? '' : 'disabled'} onclick="bulkIngestGmail('selected')">Add selected (${n})</button>
      <span class="gmail-bulk-sep">or</span>
      <input type="number" id="gmail-latest-count" class="gmail-count-input" min="1" placeholder="e.g. 100">
      <button class="connector-action-btn" onclick="bulkIngestGmail('count')">Add latest N</button>
      <button class="connector-action-btn secondary" onclick="bulkIngestGmail('all')">Add all in ${escHtml(_gmailLabel)}</button>
    </div>`;
}

function switchGmailLabel(label) {
  if (label === _gmailLabel) return;
  _gmailLabel = label;
  _gmailSelected.clear();
  _gmailNextToken = null;
  browseGmailMessages();
}

function toggleGmailSelect(messageId, checked) {
  if (checked) _gmailSelected.add(messageId);
  else _gmailSelected.delete(messageId);
  const el = document.getElementById('connector-browse-gmail');
  const controls = el?.querySelector('.gmail-bulk-controls');
  if (controls) controls.outerHTML = _gmailBulkControlsHtml();
}

function toggleGmailSelectAllLoaded(checked) {
  const el = document.getElementById('connector-browse-gmail');
  el.querySelectorAll('.gmail-row-cb').forEach(cb => {
    cb.checked = checked;
    if (checked) _gmailSelected.add(cb.dataset.id);
    else _gmailSelected.delete(cb.dataset.id);
  });
  const controls = el.querySelector('.gmail-bulk-controls');
  if (controls) controls.outerHTML = _gmailBulkControlsHtml();
}

async function browseGmailMessages(pageToken = '', append = false) {
  const el = document.getElementById('connector-browse-gmail');
  if (!append) el.innerHTML = _gmailLabelTabsHtml() + _gmailBulkControlsHtml() + '<div class="connector-browse-note">Loading…</div>';
  try {
    const params = new URLSearchParams({ max_results: '50', label: _gmailLabel });
    if (pageToken) params.set('page_token', pageToken);
    const res = await fetch(`/connectors/gmail/messages?${params}`, { headers: connectorHeaders() });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const rows = (data.messages || []).map(m => `
      <div class="connector-item-row gmail-row">
        <input type="checkbox" class="gmail-row-cb" data-id="${escAttr(m.id)}" ${_gmailSelected.has(m.id) ? 'checked' : ''}
               onclick="event.stopPropagation(); toggleGmailSelect('${escAttr(m.id)}', this.checked)">
        <span class="ci-name" onclick="browseGmailMessage('${escAttr(m.id)}', '${escAttr(m.subject)}')">✉️ ${escHtml(m.subject || '(No Subject)')}</span>
        <span class="ci-meta" onclick="browseGmailMessage('${escAttr(m.id)}', '${escAttr(m.subject)}')">${escHtml(m.from || '')}</span>
      </div>`).join('');

    _gmailNextToken = data.next_page_token || null;
    const loadMore = _gmailNextToken
      ? `<button class="connector-load-more" onclick="browseGmailMessages('${escAttr(_gmailNextToken)}', true)">Load more messages</button>`
      : '';

    if (append) {
      const prevBtn = el.querySelector('.connector-load-more');
      if (prevBtn) prevBtn.remove();
      el.insertAdjacentHTML('beforeend', rows + loadMore);
    } else {
      el.innerHTML = _gmailLabelTabsHtml() + _gmailBulkControlsHtml() +
        ((rows + loadMore) || '<div class="connector-browse-note">No messages in this label</div>');
    }
  } catch (e) {
    el.innerHTML = _gmailLabelTabsHtml() + `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseGmailMessage(messageId, subject) {
  await renderConnectorBrowse(
    'connector-browse-gmail',
    `/connectors/gmail/messages/${encodeURIComponent(messageId)}`,
    data => {
      const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseGmailMessages()">${escHtml(GMAIL_LABELS.find(l => l.id === _gmailLabel)?.name || 'Inbox')}</a> / ${escHtml(subject)}</div>`;
      // Ingesting a message always pulls in the body text plus every
      // attachment's extracted text, combined into one document - there's
      // no per-attachment picking, so attachments are listed here just for
      // visibility, not as separate clickable actions.
      const attachments = data.attachments || [];
      const addLabel = attachments.length
        ? `Add this email (body + ${attachments.length} attachment${attachments.length > 1 ? 's' : ''})`
        : 'Add this email';
      const addRow = `
        <div class="connector-item-row">
          <span class="ci-name">✉️ ${escHtml(subject)}</span>
          <button class="connector-action-btn" onclick="event.stopPropagation(); ingestGmailEmail('${escAttr(messageId)}', '${escAttr(subject)}')">${escHtml(addLabel)}</button>
        </div>`;
      const metaRows = `
        <div class="connector-browse-note">
          ${data.cc ? `Cc: ${escHtml(data.cc)}<br>` : ''}
          To: ${escHtml(data.to || '')}<br>
          Date: ${escHtml(data.date || '')}
        </div>`;
      const attachmentRows = attachments.map(a => `
        <div class="connector-item-row">
          <span class="ci-name">📎 ${escHtml(a.filename)}</span>
          <span class="ci-meta">${a.size != null ? formatBytes(a.size) : ''}</span>
        </div>`).join('');
      return crumbs + addRow + metaRows + attachmentRows;
    },
  );
}

async function ingestGmailEmail(messageId, subject) {
  // Gmail's single-email ingest is one blocking call with no SSE progress
  // events (unlike /files or the bulk endpoint below) - runConnectorIngest's
  // plain toast was easy to miss since it auto-dismisses after 4s, often
  // before parsing+embedding the email (and its attachments) finishes. Use
  // the same persistent ProgressCard the upload/bulk flows use instead, so
  // there's always a visible "Processing… -> done" state on screen.
  closeConnectorModal();
  const label = subject || 'email';
  const card = new ProgressCard(`Adding ${label}`);
  card.addStep('gmail_single', `Processing ${label} (body + attachments)...`);
  const t0 = Date.now();
  try {
    const res = await fetch('/connectors/gmail/ingest', {
      method: 'POST',
      headers: connectorHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ message_id: messageId, session_id: state.sessionId || undefined }),
    });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    const elapsed = (Date.now() - t0) / 1000;
    card.doneStep('gmail_single', data.filename || label, elapsed);
    card.complete(elapsed, 'Email processed - ready to chat!');
    onProcessed(data.session_id);
    toast('Ready to chat!', 'success');
  } catch (e) {
    card.errorStep('gmail_single', e.message);
    card.fail(e.message);
    toast(e.message, 'error');
  }
}

async function bulkIngestGmail(mode) {
  const body = { mode, label: _gmailLabel, session_id: state.sessionId || undefined };
  let label;
  if (mode === 'selected') {
    if (!_gmailSelected.size) return;
    body.message_ids = Array.from(_gmailSelected);
    label = `${body.message_ids.length} selected email(s)`;
  } else if (mode === 'count') {
    const n = parseInt(document.getElementById('gmail-latest-count')?.value || '', 10);
    if (!n || n < 1) { toast('Enter how many of the latest emails to add', 'error'); return; }
    body.count = n;
    label = `the latest ${n} email(s)`;
  } else {
    label = `all emails in ${_gmailLabel}`;
  }

  closeConnectorModal();
  const card = new ProgressCard(`Adding ${label}`);
  try {
    const res = await fetch('/connectors/gmail/ingest/bulk', {
      method: 'POST',
      headers: connectorHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    if (!res.ok) {
      const data = await safeJson(res);
      throw new Error(data?.detail || `Server error ${res.status}`);
    }
    await readSSEStream(res, (ev) => handleProgressEvent(card, ev));
    _gmailSelected.clear();
  } catch (e) {
    card.fail(e.message);
    toast(e.message, 'error');
  }
}

/* ── Google Chat browser ──────────────────────────────────────── */
// Mirrors the Gmail label-tab + checkbox + bulk-select pattern above (reuses
// the same .gmail-label-tab* / .gmail-row-cb CSS - it's generic chrome, not
// Gmail-specific styling). A Google Chat "space" covers spaces, group
// chats, AND direct messages (distinguished only by spaceType), so the tabs
// here are "All / Spaces / Group Chats / Direct Messages" instead of Gmail's
// inbox/spam/etc. labels.
let _gchatSpaceType = 'all';
let _gchatSelected = new Set();

const GCHAT_SPACE_TYPES = [
  { id: 'all', name: 'All' },
  { id: 'space', name: 'Spaces' },
  { id: 'group_chat', name: 'Group Chats' },
  { id: 'direct_message', name: 'Direct Messages' },
];

function _gchatTabsHtml() {
  return `<div class="gmail-label-tabs">` +
    GCHAT_SPACE_TYPES.map(t => `<button class="gmail-label-tab${t.id === _gchatSpaceType ? ' active' : ''}" onclick="switchGchatSpaceType('${t.id}')">${escHtml(t.name)}</button>`).join('') +
    `</div>`;
}

function _gchatBulkControlsHtml() {
  const n = _gchatSelected.size;
  return `
    <div class="gmail-bulk-controls">
      <label class="gmail-select-all">
        <input type="checkbox" id="gchat-select-all-cb" onchange="toggleGchatSelectAllLoaded(this.checked)">
        Select all loaded
      </label>
      <button class="connector-action-btn" ${n ? '' : 'disabled'} onclick="bulkIngestGoogleChat('selected')">Add selected (${n})</button>
      <span class="gmail-bulk-sep">or</span>
      <button class="connector-action-btn secondary" onclick="bulkIngestGoogleChat('all')">Add all in ${escHtml(GCHAT_SPACE_TYPES.find(t => t.id === _gchatSpaceType)?.name || 'All')}</button>
    </div>`;
}

function switchGchatSpaceType(type) {
  if (type === _gchatSpaceType) return;
  _gchatSpaceType = type;
  _gchatSelected.clear();
  browseGoogleChatSpaces();
}

function toggleGchatSelect(spaceId, checked) {
  if (checked) _gchatSelected.add(spaceId);
  else _gchatSelected.delete(spaceId);
  const el = document.getElementById('connector-browse-google_chat');
  const controls = el?.querySelector('.gmail-bulk-controls');
  if (controls) controls.outerHTML = _gchatBulkControlsHtml();
}

function toggleGchatSelectAllLoaded(checked) {
  const el = document.getElementById('connector-browse-google_chat');
  el.querySelectorAll('.gmail-row-cb').forEach(cb => {
    cb.checked = checked;
    if (checked) _gchatSelected.add(cb.dataset.id);
    else _gchatSelected.delete(cb.dataset.id);
  });
  const controls = el.querySelector('.gmail-bulk-controls');
  if (controls) controls.outerHTML = _gchatBulkControlsHtml();
}

async function browseGoogleChatSpaces(pageToken = '', append = false) {
  const el = document.getElementById('connector-browse-google_chat');
  if (!append) el.innerHTML = _gchatTabsHtml() + _gchatBulkControlsHtml() + '<div class="connector-browse-note">Loading…</div>';
  try {
    const params = new URLSearchParams({ page_size: '50', space_type: _gchatSpaceType });
    if (pageToken) params.set('page_token', pageToken);
    const res = await fetch(`/connectors/google-chat/spaces?${params}`, { headers: connectorHeaders() });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const rows = (data.spaces || []).map(s => `
      <div class="connector-item-row gmail-row">
        <input type="checkbox" class="gmail-row-cb" data-id="${escAttr(s.id)}" ${_gchatSelected.has(s.id) ? 'checked' : ''}
               onclick="event.stopPropagation(); toggleGchatSelect('${escAttr(s.id)}', this.checked)">
        <span class="ci-name">💬 ${escHtml(s.name)}</span>
        <span class="ci-meta">${escHtml((s.space_type || '').replace(/_/g, ' ').toLowerCase())}</span>
      </div>`).join('');

    const nextToken = data.next_page_token || null;
    const loadMore = nextToken
      ? `<button class="connector-load-more" onclick="browseGoogleChatSpaces('${escAttr(nextToken)}', true)">Load more chats</button>`
      : '';

    if (append) {
      const prevBtn = el.querySelector('.connector-load-more');
      if (prevBtn) prevBtn.remove();
      el.insertAdjacentHTML('beforeend', rows + loadMore);
    } else {
      el.innerHTML = _gchatTabsHtml() + _gchatBulkControlsHtml() +
        ((rows + loadMore) || '<div class="connector-browse-note">No chats/spaces in this tab</div>');
    }
  } catch (e) {
    el.innerHTML = _gchatTabsHtml() + `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function bulkIngestGoogleChat(mode) {
  const body = { mode, space_type: _gchatSpaceType, session_id: state.sessionId || undefined };
  let label;
  if (mode === 'selected') {
    if (!_gchatSelected.size) return;
    body.space_ids = Array.from(_gchatSelected);
    label = `${body.space_ids.length} selected chat(s)`;
  } else {
    label = `all chats in ${_gchatSpaceType}`;
  }

  closeConnectorModal();
  const card = new ProgressCard(`Adding ${label}`);
  try {
    const res = await fetch('/connectors/google-chat/ingest/bulk', {
      method: 'POST',
      headers: connectorHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    if (!res.ok) {
      const data = await safeJson(res);
      throw new Error(data?.detail || `Server error ${res.status}`);
    }
    await readSSEStream(res, (ev) => handleProgressEvent(card, ev));
    _gchatSelected.clear();
  } catch (e) {
    card.fail(e.message);
    toast(e.message, 'error');
  }
}

/* ── Outlook Mail browser ─────────────────────────────────────── */
// Mirrors the Gmail label-tab + checkbox + bulk-select pattern exactly
// (same .gmail-label-tab* / .gmail-row-cb CSS). Folder tabs instead of
// Gmail's labels, and Graph's @odata.nextLink instead of a page token, but
// otherwise identical shape - selected/count/all bulk modes included.
let _outlookFolder = 'inbox';
let _outlookSelected = new Set();
let _outlookNextLink = null;

const OUTLOOK_FOLDERS = [
  { id: 'inbox', name: 'Inbox' },
  { id: 'junk', name: 'Junk' },
  { id: 'sent', name: 'Sent' },
  { id: 'drafts', name: 'Drafts' },
  { id: 'deleted', name: 'Deleted' },
  { id: 'all', name: 'All Mail' },
];

function _outlookTabsHtml() {
  return `<div class="gmail-label-tabs">` +
    OUTLOOK_FOLDERS.map(f => `<button class="gmail-label-tab${f.id === _outlookFolder ? ' active' : ''}" onclick="switchOutlookFolder('${f.id}')">${escHtml(f.name)}</button>`).join('') +
    `</div>`;
}

function _outlookBulkControlsHtml() {
  const n = _outlookSelected.size;
  return `
    <div class="gmail-bulk-controls">
      <label class="gmail-select-all">
        <input type="checkbox" id="outlook-select-all-cb" onchange="toggleOutlookSelectAllLoaded(this.checked)">
        Select all loaded
      </label>
      <button class="connector-action-btn" ${n ? '' : 'disabled'} onclick="bulkIngestOutlook('selected')">Add selected (${n})</button>
      <span class="gmail-bulk-sep">or</span>
      <input type="number" id="outlook-latest-count" class="gmail-count-input" min="1" placeholder="e.g. 100">
      <button class="connector-action-btn" onclick="bulkIngestOutlook('count')">Add latest N</button>
      <button class="connector-action-btn secondary" onclick="bulkIngestOutlook('all')">Add all in ${escHtml(_outlookFolder)}</button>
    </div>`;
}

function switchOutlookFolder(folder) {
  if (folder === _outlookFolder) return;
  _outlookFolder = folder;
  _outlookSelected.clear();
  _outlookNextLink = null;
  browseOutlookMessages();
}

function toggleOutlookSelect(messageId, checked) {
  if (checked) _outlookSelected.add(messageId);
  else _outlookSelected.delete(messageId);
  const el = document.getElementById('connector-browse-outlook');
  const controls = el?.querySelector('.gmail-bulk-controls');
  if (controls) controls.outerHTML = _outlookBulkControlsHtml();
}

function toggleOutlookSelectAllLoaded(checked) {
  const el = document.getElementById('connector-browse-outlook');
  el.querySelectorAll('.gmail-row-cb').forEach(cb => {
    cb.checked = checked;
    if (checked) _outlookSelected.add(cb.dataset.id);
    else _outlookSelected.delete(cb.dataset.id);
  });
  const controls = el.querySelector('.gmail-bulk-controls');
  if (controls) controls.outerHTML = _outlookBulkControlsHtml();
}

async function browseOutlookMessages(nextLink = '', append = false) {
  const el = document.getElementById('connector-browse-outlook');
  if (!append) el.innerHTML = _outlookTabsHtml() + _outlookBulkControlsHtml() + '<div class="connector-browse-note">Loading…</div>';
  try {
    const params = new URLSearchParams({ max_results: '25', folder: _outlookFolder });
    if (nextLink) params.set('next_link', nextLink);
    const res = await fetch(`/connectors/outlook/messages?${params}`, { headers: connectorHeaders() });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const rows = (data.messages || []).map(m => `
      <div class="connector-item-row gmail-row">
        <input type="checkbox" class="gmail-row-cb" data-id="${escAttr(m.id)}" ${_outlookSelected.has(m.id) ? 'checked' : ''}
               onclick="event.stopPropagation(); toggleOutlookSelect('${escAttr(m.id)}', this.checked)">
        <span class="ci-name" onclick="browseOutlookMessage('${escAttr(m.id)}', '${escAttr(m.subject)}')">✉️ ${escHtml(m.subject || '(No Subject)')}</span>
        <span class="ci-meta" onclick="browseOutlookMessage('${escAttr(m.id)}', '${escAttr(m.subject)}')">${escHtml(m.from || '')}</span>
      </div>`).join('');

    _outlookNextLink = data.next_link || null;
    const loadMore = _outlookNextLink
      ? `<button class="connector-load-more" onclick="browseOutlookMessages('${escAttr(_outlookNextLink)}', true)">Load more messages</button>`
      : '';

    if (append) {
      const prevBtn = el.querySelector('.connector-load-more');
      if (prevBtn) prevBtn.remove();
      el.insertAdjacentHTML('beforeend', rows + loadMore);
    } else {
      el.innerHTML = _outlookTabsHtml() + _outlookBulkControlsHtml() +
        ((rows + loadMore) || '<div class="connector-browse-note">No messages in this folder</div>');
    }
  } catch (e) {
    el.innerHTML = _outlookTabsHtml() + `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function browseOutlookMessage(messageId, subject) {
  await renderConnectorBrowse(
    'connector-browse-outlook',
    `/connectors/outlook/messages/${encodeURIComponent(messageId)}`,
    data => {
      const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseOutlookMessages()">${escHtml(OUTLOOK_FOLDERS.find(f => f.id === _outlookFolder)?.name || 'Inbox')}</a> / ${escHtml(subject)}</div>`;
      const attachments = data.attachments || [];
      const addLabel = attachments.length
        ? `Add this email (body + ${attachments.length} attachment${attachments.length > 1 ? 's' : ''})`
        : 'Add this email';
      const addRow = `
        <div class="connector-item-row">
          <span class="ci-name">✉️ ${escHtml(subject)}</span>
          <button class="connector-action-btn" onclick="event.stopPropagation(); ingestOutlookEmail('${escAttr(messageId)}', '${escAttr(subject)}')">${escHtml(addLabel)}</button>
        </div>`;
      const metaRows = `
        <div class="connector-browse-note">
          ${data.cc ? `Cc: ${escHtml(data.cc)}<br>` : ''}
          To: ${escHtml(data.to || '')}<br>
          Date: ${escHtml(data.date || '')}
        </div>`;
      const attachmentRows = attachments.map(a => `
        <div class="connector-item-row">
          <span class="ci-name">📎 ${escHtml(a.filename)}</span>
          <span class="ci-meta">${a.size != null ? formatBytes(a.size) : ''}</span>
        </div>`).join('');
      return crumbs + addRow + metaRows + attachmentRows;
    },
  );
}

async function ingestOutlookEmail(messageId, subject) {
  // Routed through the bulk endpoint with a single id - it already streams
  // real SSE progress (unlike Gmail's single-message endpoint, which has no
  // SSE and needed its own manual ProgressCard handling in ingestGmailEmail),
  // so this can just reuse handleProgressEvent directly like bulkIngestOutlook.
  closeConnectorModal();
  const card = new ProgressCard(`Adding ${subject || 'email'}`);
  try {
    const res = await fetch('/connectors/outlook/ingest/bulk', {
      method: 'POST',
      headers: connectorHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ mode: 'selected', message_ids: [messageId], session_id: state.sessionId || undefined }),
    });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    if (!res.ok) {
      const data = await safeJson(res);
      throw new Error(data?.detail || `Server error ${res.status}`);
    }
    await readSSEStream(res, (ev) => handleProgressEvent(card, ev));
  } catch (e) {
    card.fail(e.message);
    toast(e.message, 'error');
  }
}

async function bulkIngestOutlook(mode) {
  const body = { mode, folder: _outlookFolder, session_id: state.sessionId || undefined };
  let label;
  if (mode === 'selected') {
    if (!_outlookSelected.size) return;
    body.message_ids = Array.from(_outlookSelected);
    label = `${body.message_ids.length} selected email(s)`;
  } else if (mode === 'count') {
    const n = parseInt(document.getElementById('outlook-latest-count')?.value || '', 10);
    if (!n || n < 1) { toast('Enter how many of the latest emails to add', 'error'); return; }
    body.count = n;
    label = `the latest ${n} email(s)`;
  } else {
    label = `all emails in ${_outlookFolder}`;
  }

  closeConnectorModal();
  const card = new ProgressCard(`Adding ${label}`);
  try {
    const res = await fetch('/connectors/outlook/ingest/bulk', {
      method: 'POST',
      headers: connectorHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    if (!res.ok) {
      const data = await safeJson(res);
      throw new Error(data?.detail || `Server error ${res.status}`);
    }
    await readSSEStream(res, (ev) => handleProgressEvent(card, ev));
    _outlookSelected.clear();
  } catch (e) {
    card.fail(e.message);
    toast(e.message, 'error');
  }
}

/* ── Outlook Chat browser ─────────────────────────────────────── */
// Mirrors the Google Chat tab + checkbox + bulk-select pattern exactly.
let _ochatType = 'all';
let _ochatSelected = new Set();

const OCHAT_TYPES = [
  { id: 'all', name: 'All' },
  { id: 'one_on_one', name: 'Direct' },
  { id: 'group', name: 'Group Chats' },
  { id: 'meeting', name: 'Meetings' },
];

function _ochatTabsHtml() {
  return `<div class="gmail-label-tabs">` +
    OCHAT_TYPES.map(t => `<button class="gmail-label-tab${t.id === _ochatType ? ' active' : ''}" onclick="switchOchatType('${t.id}')">${escHtml(t.name)}</button>`).join('') +
    `</div>`;
}

function _ochatBulkControlsHtml() {
  const n = _ochatSelected.size;
  return `
    <div class="gmail-bulk-controls">
      <label class="gmail-select-all">
        <input type="checkbox" id="ochat-select-all-cb" onchange="toggleOchatSelectAllLoaded(this.checked)">
        Select all loaded
      </label>
      <button class="connector-action-btn" ${n ? '' : 'disabled'} onclick="bulkIngestOutlookChat('selected')">Add selected (${n})</button>
      <span class="gmail-bulk-sep">or</span>
      <button class="connector-action-btn secondary" onclick="bulkIngestOutlookChat('all')">Add all in ${escHtml(OCHAT_TYPES.find(t => t.id === _ochatType)?.name || 'All')}</button>
    </div>`;
}

function switchOchatType(type) {
  if (type === _ochatType) return;
  _ochatType = type;
  _ochatSelected.clear();
  browseOutlookChats();
}

function toggleOchatSelect(chatId, checked) {
  if (checked) _ochatSelected.add(chatId);
  else _ochatSelected.delete(chatId);
  const el = document.getElementById('connector-browse-outlook_chat');
  const controls = el?.querySelector('.gmail-bulk-controls');
  if (controls) controls.outerHTML = _ochatBulkControlsHtml();
}

function toggleOchatSelectAllLoaded(checked) {
  const el = document.getElementById('connector-browse-outlook_chat');
  el.querySelectorAll('.gmail-row-cb').forEach(cb => {
    cb.checked = checked;
    if (checked) _ochatSelected.add(cb.dataset.id);
    else _ochatSelected.delete(cb.dataset.id);
  });
  const controls = el.querySelector('.gmail-bulk-controls');
  if (controls) controls.outerHTML = _ochatBulkControlsHtml();
}

async function browseOutlookChats(nextLink = '', append = false) {
  const el = document.getElementById('connector-browse-outlook_chat');
  if (!append) el.innerHTML = _ochatTabsHtml() + _ochatBulkControlsHtml() + '<div class="connector-browse-note">Loading…</div>';
  try {
    const params = new URLSearchParams({ page_size: '50', chat_type: _ochatType });
    if (nextLink) params.set('next_link', nextLink);
    const res = await fetch(`/connectors/outlook-chat/chats?${params}`, { headers: connectorHeaders() });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const rows = (data.chats || []).map(c => `
      <div class="connector-item-row gmail-row">
        <input type="checkbox" class="gmail-row-cb" data-id="${escAttr(c.id)}" ${_ochatSelected.has(c.id) ? 'checked' : ''}
               onclick="event.stopPropagation(); toggleOchatSelect('${escAttr(c.id)}', this.checked)">
        <span class="ci-name">💬 ${escHtml(c.name)}</span>
        <span class="ci-meta">${escHtml((c.chat_type || '').replace(/_/g, ' ').toLowerCase())}</span>
      </div>`).join('');

    const next = data.next_link || null;
    const loadMore = next
      ? `<button class="connector-load-more" onclick="browseOutlookChats('${escAttr(next)}', true)">Load more chats</button>`
      : '';

    if (append) {
      const prevBtn = el.querySelector('.connector-load-more');
      if (prevBtn) prevBtn.remove();
      el.insertAdjacentHTML('beforeend', rows + loadMore);
    } else {
      el.innerHTML = _ochatTabsHtml() + _ochatBulkControlsHtml() +
        ((rows + loadMore) || '<div class="connector-browse-note">No chats in this tab</div>');
    }
  } catch (e) {
    el.innerHTML = _ochatTabsHtml() + `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function bulkIngestOutlookChat(mode) {
  const body = { mode, chat_type: _ochatType, session_id: state.sessionId || undefined };
  let label;
  if (mode === 'selected') {
    if (!_ochatSelected.size) return;
    body.chat_ids = Array.from(_ochatSelected);
    label = `${body.chat_ids.length} selected chat(s)`;
  } else {
    label = `all chats in ${_ochatType}`;
  }

  closeConnectorModal();
  const card = new ProgressCard(`Adding ${label}`);
  try {
    const res = await fetch('/connectors/outlook-chat/ingest/bulk', {
      method: 'POST',
      headers: connectorHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body),
    });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    if (!res.ok) {
      const data = await safeJson(res);
      throw new Error(data?.detail || `Server error ${res.status}`);
    }
    await readSSEStream(res, (ev) => handleProgressEvent(card, ev));
    _ochatSelected.clear();
  } catch (e) {
    card.fail(e.message);
    toast(e.message, 'error');
  }
}

/* ── Google Drive browser ────────────────────────────────────── */
let _gdriveTrail = [];
let _gdriveNextToken = null;

async function browseGoogleDriveRoot() {
  _gdriveTrail = [{ id: 'root', name: 'My Drive' }];
  await loadGoogleDriveFolder();
}

async function browseGoogleDriveCrumb(index) {
  _gdriveTrail = _gdriveTrail.slice(0, index + 1);
  await loadGoogleDriveFolder();
}

async function browseGoogleDriveOpen(id, name) {
  _gdriveTrail.push({ id, name });
  await loadGoogleDriveFolder();
}

async function loadGoogleDriveFolder(pageToken = '', append = false) {
  const el = document.getElementById('connector-browse-google_drive');
  if (!append) el.innerHTML = '<div class="connector-browse-note">Loading…</div>';
  const current = _gdriveTrail[_gdriveTrail.length - 1];
  try {
    const q = `'${current.id}' in parents and trashed = false`;
    const params = new URLSearchParams({ max_results: '100', q });
    if (pageToken) params.set('page_token', pageToken);
    const res = await fetch(`/connectors/google-drive/files?${params}`, { headers: connectorHeaders() });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);

    const crumbs = `<div class="connector-breadcrumb">${_gdriveTrail.map((t, i) =>
      `<a class="crumb" onclick="browseGoogleDriveCrumb(${i})">${escHtml(t.name)}</a>`).join(' / ')}</div>`;

    const rows = (data.files || []).map(f => f.mimeType === 'application/vnd.google-apps.folder'
      ? `<div class="connector-item-row is-folder" onclick="browseGoogleDriveOpen('${escAttr(f.id)}', '${escAttr(f.name)}')">
           <span class="ci-name">📁 ${escHtml(f.name)}</span>
         </div>`
      : `<div class="connector-item-row" onclick="ingestGoogleDriveFile('${escAttr(f.id)}', '${escAttr(f.name)}')">
           <span class="ci-name">📄 ${escHtml(f.name)}</span>
           <span class="ci-meta">${f.size != null ? formatBytes(f.size) : ''}</span>
         </div>`
    ).join('');

    _gdriveNextToken = data.next_page_token || null;
    const loadMore = _gdriveNextToken
      ? `<button class="connector-load-more" onclick="loadGoogleDriveFolder('${escAttr(_gdriveNextToken)}', true)">Load more</button>`
      : '';

    if (append) {
      const prevBtn = el.querySelector('.connector-load-more');
      if (prevBtn) prevBtn.remove();
      el.insertAdjacentHTML('beforeend', rows + loadMore);
    } else {
      el.innerHTML = crumbs + ((rows + loadMore) || '<div class="connector-browse-note">Empty folder</div>');
    }
  } catch (e) {
    el.innerHTML = `<div class="connector-browse-note">${escHtml(e.message)}</div>`;
  }
}

async function ingestGoogleDriveFile(fileId, name) {
  await runConnectorIngest('google_drive', { file_id: fileId }, name);
}

/* ── SharePoint browser ───────────────────────────────────────── */
let _spState = { siteId: '', siteName: '', driveId: '', driveName: '', trail: [] };

async function browseSharepointSites() {
  await renderConnectorBrowse(
    'connector-browse-sharepoint',
    '/connectors/sharepoint/sites',
    data => (data.value || []).map(s => `
      <div class="connector-item-row is-folder" onclick="browseSharepointDrives('${escAttr(s.id)}', '${escAttr(s.displayName || s.name)}')">
        <span class="ci-name">📁 ${escHtml(s.displayName || s.name)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No sites</div>',
  );
}

async function browseSharepointDrives(siteId, siteName) {
  _spState = { siteId, siteName, driveId: '', driveName: '', trail: [] };
  await renderConnectorBrowse(
    'connector-browse-sharepoint',
    `/connectors/sharepoint/sites/${encodeURIComponent(siteId)}/drives`,
    data => {
      const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseSharepointSites()">Sites</a> / ${escHtml(siteName)}</div>`;
      const rows = (data.value || []).map(d => `
        <div class="connector-item-row is-folder" onclick="browseSharepointOpenDrive('${escAttr(d.id)}', '${escAttr(d.name)}')">
          <span class="ci-name">📁 ${escHtml(d.name)}</span>
        </div>`).join('') || '<div class="connector-browse-note">No document libraries on this site</div>';
      return crumbs + rows;
    },
  );
}

async function browseSharepointOpenDrive(driveId, driveName) {
  _spState.driveId = driveId;
  _spState.driveName = driveName;
  _spState.trail = [{ id: 'root', name: driveName }];
  await loadSharepointItems();
}

async function browseSharepointOpenFolder(folderId, folderName) {
  _spState.trail.push({ id: folderId, name: folderName });
  await loadSharepointItems();
}

async function browseSharepointCrumb(index) {
  _spState.trail = _spState.trail.slice(0, index + 1);
  await loadSharepointItems();
}

async function loadSharepointItems() {
  const current = _spState.trail[_spState.trail.length - 1];
  await renderConnectorBrowse(
    'connector-browse-sharepoint',
    `/connectors/sharepoint/drives/${encodeURIComponent(_spState.driveId)}/items?folder_id=${encodeURIComponent(current.id)}`,
    data => {
      const crumbs = [
        `<a class="crumb" onclick="browseSharepointSites()">Sites</a>`,
        `<a class="crumb" onclick="browseSharepointDrives('${escAttr(_spState.siteId)}', '${escAttr(_spState.siteName)}')">${escHtml(_spState.siteName)}</a>`,
        ..._spState.trail.map((t, i) => `<a class="crumb" onclick="browseSharepointCrumb(${i})">${escHtml(t.name)}</a>`),
      ];

      const rows = (data.value || []).map(item => item.folder
        ? `<div class="connector-item-row is-folder" onclick="browseSharepointOpenFolder('${escAttr(item.id)}', '${escAttr(item.name)}')">
             <span class="ci-name">📁 ${escHtml(item.name)}</span>
           </div>`
        : `<div class="connector-item-row" onclick="ingestSharepointFile('${escAttr(item.id)}', '${escAttr(item.name)}')">
             <span class="ci-name">📄 ${escHtml(item.name)}</span>
             <span class="ci-meta">${item.size != null ? formatBytes(item.size) : ''}</span>
           </div>`
      ).join('') || '<div class="connector-browse-note">Empty folder</div>';

      return `<div class="connector-breadcrumb">${crumbs.join(' / ')}</div>` + rows;
    },
  );
}

async function ingestSharepointFile(itemId, name) {
  await runConnectorIngest('sharepoint', { drive_id: _spState.driveId, item_id: itemId }, name);
}

/* ── Microsoft Teams browser ──────────────────────────────────── */
// Teams has no file drill-down - a channel's messages are themselves the
// ingestable unit, so each channel row carries an explicit "Add" action
// instead of opening a deeper level.
async function browseTeamsList() {
  await renderConnectorBrowse(
    'connector-browse-teams',
    '/connectors/teams/list',
    data => (data.value || []).map(t => `
      <div class="connector-item-row is-folder" onclick="browseTeamsChannels('${escAttr(t.id)}', '${escAttr(t.displayName)}')">
        <span class="ci-name">📁 ${escHtml(t.displayName)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No teams</div>',
  );
}

async function browseTeamsChannels(teamId, teamName) {
  await renderConnectorBrowse(
    'connector-browse-teams',
    `/connectors/teams/${encodeURIComponent(teamId)}/channels`,
    data => {
      const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseTeamsList()">Teams</a> / ${escHtml(teamName)}</div>`;
      const rows = (data.value || []).map(c => `
        <div class="connector-item-row">
          <span class="ci-name">💬 ${escHtml(c.displayName)}</span>
          <span class="ci-meta">${escHtml(c.membershipType || '')}</span>
          <button class="connector-action-btn" onclick="event.stopPropagation(); ingestTeamsChannel('${escAttr(teamId)}', '${escAttr(c.id)}', '${escAttr(teamName)}', '${escAttr(c.displayName)}')">Add this channel</button>
        </div>`).join('') || '<div class="connector-browse-note">No channels in this team</div>';
      return crumbs + rows;
    },
  );
}

async function ingestTeamsChannel(teamId, channelId, teamName, channelName) {
  await runConnectorIngest('teams', { team_id: teamId, channel_id: channelId }, `${teamName} / ${channelName}`);
}

/* ── Azure DevOps Repos browser ──────────────────────────────── */
async function browseAzureDevOpsProjects() {
  await renderConnectorBrowse(
    'connector-browse-azure_devops',
    '/connectors/azure-devops/projects',
    data => (data.value || []).map(p => `
      <div class="connector-item-row is-folder" onclick="browseAzureDevOpsRepos('${escAttr(p.name)}')">
        <span class="ci-name">📁 ${escHtml(p.name)}</span>
      </div>`).join('') || '<div class="connector-browse-note">No projects</div>',
  );
}

async function browseAzureDevOpsRepos(project) {
  await renderConnectorBrowse(
    'connector-browse-azure_devops',
    `/connectors/azure-devops/projects/${encodeURIComponent(project)}/repos`,
    data => {
      const crumbs = `<div class="connector-breadcrumb"><a class="crumb" onclick="browseAzureDevOpsProjects()">Projects</a> / ${escHtml(project)}</div>`;
      const rows = (data.value || []).map(r => `
        <div class="connector-item-row is-folder" onclick="browseAzureDevOpsItems('${escAttr(project)}', '${escAttr(r.id)}', '${escAttr(r.name)}', '/')">
          <span class="ci-name">📁 ${escHtml(r.name)}</span>
        </div>`).join('') || '<div class="connector-browse-note">No repositories in this project</div>';
      return crumbs + rows;
    },
  );
}

async function browseAzureDevOpsItems(project, repoId, repoName, path) {
  const params = new URLSearchParams({ path, recursion_level: 'OneLevel' });
  await renderConnectorBrowse(
    'connector-browse-azure_devops',
    `/connectors/azure-devops/projects/${encodeURIComponent(project)}/repos/${encodeURIComponent(repoId)}/items?${params}`,
    data => {
      const crumbs = [
        `<a class="crumb" onclick="browseAzureDevOpsProjects()">Projects</a>`,
        `<a class="crumb" onclick="browseAzureDevOpsRepos('${escAttr(project)}')">${escHtml(project)}</a>`,
        `<a class="crumb" onclick="browseAzureDevOpsItems('${escAttr(project)}', '${escAttr(repoId)}', '${escAttr(repoName)}', '/')">${escHtml(repoName)}</a>`,
      ];
      let acc = '';
      path.split('/').filter(Boolean).forEach(p => {
        acc = acc ? `${acc}/${p}` : `/${p}`;
        crumbs.push(`<a class="crumb" onclick="browseAzureDevOpsItems('${escAttr(project)}', '${escAttr(repoId)}', '${escAttr(repoName)}', '${escAttr(acc)}')">${escHtml(p)}</a>`);
      });

      // OneLevel + scopePath returns the folder itself alongside its children - drop the self-entry.
      const items = (data.value || []).filter(it => it.path !== path);
      const rows = items.map(it => it.isFolder
        ? `<div class="connector-item-row is-folder" onclick="browseAzureDevOpsItems('${escAttr(project)}', '${escAttr(repoId)}', '${escAttr(repoName)}', '${escAttr(it.path)}')">
             <span class="ci-name">📁 ${escHtml(it.path.split('/').pop())}</span>
           </div>`
        : `<div class="connector-item-row" onclick="ingestAzureDevOpsFile('${escAttr(project)}', '${escAttr(repoId)}', '${escAttr(it.path)}')">
             <span class="ci-name">📄 ${escHtml(it.path.split('/').pop())}</span>
           </div>`
      ).join('') || '<div class="connector-browse-note">Empty folder</div>';

      return `<div class="connector-breadcrumb">${crumbs.join(' / ')}</div>` + rows;
    },
  );
}

async function ingestAzureDevOpsFile(project, repoId, path) {
  await runConnectorIngest('azure_devops', { project, repo_id: repoId, path }, path.split('/').pop());
}

/* ── Shared ingest call ──────────────────────────────────────── */
async function runConnectorIngest(provider, body, filename) {
  // Shared by every connector that ingests a single file in one blocking
  // call with no SSE progress (github, google_drive, sharepoint, teams,
  // azure_devops - see app/connectors/pipeline_bridge.py). This used to
  // just show a toast, which auto-dismisses after 4s and was easy to miss
  // if parsing+embedding took longer than that - switched to the same
  // persistent ProgressCard every other ingest path uses (uploads, Gmail/
  // Outlook single-email, the Gmail/Google Chat/Outlook/Outlook Chat bulk
  // endpoints), so there's always a visible "Processing… -> done" state.
  closeConnectorModal();
  const card = new ProgressCard(`Adding ${filename}`);
  card.addStep('connector_single', `Processing ${filename}...`);
  const t0 = Date.now();
  try {
    const res = await fetch(`/connectors/${provider}/ingest`, {
      method: 'POST',
      headers: connectorHeaders({ 'Content-Type': 'application/json' }),
      // Attach the active session (if one exists) so this document joins it
      // instead of starting an isolated new session that orphans whatever
      // was added before - set centrally here so every ingestXFile() caller
      // gets it for free.
      body: JSON.stringify({ ...body, session_id: state.sessionId || undefined }),
    });
    if (await handleAuthError(res)) return;
    if (handleConnectorsLockError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) throw new Error(data?.detail || `Server error ${res.status}`);
    const elapsed = (Date.now() - t0) / 1000;
    card.doneStep('connector_single', data.filename || filename, elapsed);
    card.complete(elapsed, `${filename} added - ready to chat!`);
    onProcessed(data.session_id);
    toast('Ready to chat!', 'success');
  } catch (e) {
    card.errorStep('connector_single', e.message);
    card.fail(e.message);
    toast(e.message, 'error');
  }
}

/* ── Small helpers ───────────────────────────────────────────── */
function escAttr(str) {
  return String(str ?? '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}
function breadcrumbHtml(path, onCrumbClick) {
  if (!path) return '';
  const parts = path.split('/').filter(Boolean);
  let acc = '';
  const crumbs = [`<a class="crumb" onclick="${onCrumbClick('')}">Root</a>`];
  parts.forEach(p => {
    acc = acc ? `${acc}/${p}` : p;
    crumbs.push(`<a class="crumb" onclick="${onCrumbClick(acc)}">${escHtml(p)}</a>`);
  });
  return `<div class="connector-breadcrumb">${crumbs.join(' / ')}</div>`;
}

/* ── Chat ────────────────────────────────────────────────────── */
function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

async function sendMessage() {
  const input = document.getElementById('chat-input');
  const question = input.value.trim();
  if (!question || !state.isReady || state.isProcessing) return;

  input.value = '';
  autoResize(input);
  clearWelcome();
  appendMessage('user', question);
  showTyping(true);

  try {
    const res  = await fetch('/chat/', {
      method: 'POST',
      headers: authHeaders({
        'Content-Type':    'application/json',
        'X-Advanced-Mode':  state.advancedMode  ? 'true' : 'false',
        'X-Thinking-Mode':  state.thinkingMode  ? 'true' : 'false',
      }),
      body: JSON.stringify({
        session_id: state.sessionId,
        question,
        knowledge_base_id: state.selectedKnowledgeBaseId || undefined,
      }),
    });
    if (await handleAuthError(res)) return;
    const data = await safeJson(res);
    if (!res.ok) {
      const detail = data?.detail;
      const msg = Array.isArray(detail) ? (detail[0]?.msg || 'Request error') : (detail || `Server error ${res.status}`);
      throw new Error(msg);
    }
    appendMessage('bot', data.answer, data.elapsed_ms, data.citations || []);
  } catch (err) {
    appendMessage('bot', `Error: ${err.message}`);
    toast(err.message, 'error');
  } finally {
    showTyping(false);
  }
}

/* ── Message rendering ───────────────────────────────────────── */
function appendMessage(role, text, elapsedMs = null, citations = []) {
  const messages = document.getElementById('messages');

  const div = document.createElement('div');
  div.className = `message ${role}`;

  const avatarHtml = role === 'bot'
    ? `<div class="bot-avatar">AR</div>`
    : `<div class="user-avatar">👤</div>`;

  const content = role === 'bot'
    ? marked.parse(text)
    : `<p>${escHtml(text)}</p>`;

  const timingHtml = (role === 'bot' && elapsedMs != null)
    ? `<div class="answer-timing">⏱ ${elapsedMs.toLocaleString()} ms</div>`
    : '';

  let citationsHtml = '';
  if (role === 'bot' && citations.length > 0) {
    const id = 'cit-' + Math.random().toString(36).slice(2, 8);
    const rows = citations.map(c => {
      const src = escHtml(c.source || 'unknown');
      const chunk = c.chunk_index ?? '?';
      const preview = escHtml((c.preview || '').replace(/\n+/g, ' '));
      const pageHtml = c.page_number != null ? `<span class="cit-sep">-</span><span class="cit-page">Page ${c.page_number}</span>` : '';
      const confHtml = c.confidence != null ? `<span class="cit-sep">-</span><span class="cit-conf">Confidence ${(c.confidence * 100).toFixed(1)}%</span>` : '';
      return `<div class="citation-item">
        <span class="cit-arrow">▶</span>
        <span class="cit-source">${src}</span>
        <span class="cit-sep">-</span>
        <span class="cit-chunk">Chunk #${chunk}</span>
        ${pageHtml}${confHtml}
        <span class="cit-sep">-</span>
        <span class="cit-preview">"${preview}"</span>
      </div>`;
    }).join('');
    citationsHtml = `
      <div class="citations-block">
        <button class="citations-toggle" onclick="toggleCitations('${id}')">
          <span class="cit-toggle-icon">▶</span> ${citations.length} source${citations.length > 1 ? 's' : ''} used
        </button>
        <div class="citations-list hidden" id="${id}">${rows}</div>
      </div>`;
  }

  const copyBtn = role === 'bot'
    ? `<div class="bubble-actions">
        <button class="copy-btn" onclick="copyText(this, ${escHtml(JSON.stringify(text))})">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          Copy
        </button>
      </div>`
    : '';

  div.innerHTML = `
    ${avatarHtml}
    <div>
      <div class="bubble">${content}${timingHtml}${citationsHtml}</div>
      ${copyBtn}
    </div>`;

  messages.appendChild(div);
  scrollToBottom();
}

function toggleCitations(id) {
  const list = document.getElementById(id);
  const btn  = list.previousElementSibling;
  const icon = btn.querySelector('.cit-toggle-icon');
  const open = list.classList.toggle('hidden');
  icon.textContent = open ? '▶' : '▼';
}

function clearWelcome() {
  const w = document.querySelector('.welcome');
  if (w) w.remove();
}

function showTyping(show) {
  document.getElementById('typing').classList.toggle('hidden', !show);
  if (show) scrollToBottom();
}

function scrollToBottom() {
  const el = document.getElementById('messages');
  el.scrollTop = el.scrollHeight;
}

function copyText(btn, text) {
  navigator.clipboard.writeText(text).then(() => {
    btn.innerHTML = `✓ Copied`;
    setTimeout(() => {
      btn.innerHTML = `<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Copy`;
    }, 2000);
  });
}

/* ── Reset ───────────────────────────────────────────────────── */
function resetSession() {
  state.sessionId  = null;
  state.isReady    = false;
  state.files      = [];
  state.selectedKnowledgeBaseId = null;
  localStorage.removeItem('rag_session_id');
  renderFileList();
  renderKbList();

  document.getElementById('session-badge').classList.add('hidden');
  document.getElementById('reset-btn').classList.add('hidden');

  const input   = document.getElementById('chat-input');
  const sendBtn = document.getElementById('send-btn');
  input.disabled   = true;
  input.placeholder = 'Process a data source first to start chatting...';
  sendBtn.disabled = true;

  const messages = document.getElementById('messages');
  messages.innerHTML = `
    <div class="welcome">
      <div class="aria-welcome-avatar">
        <img src="/dabur-logo.svg" alt="Dabur" class="brand-logo-img" />
      </div>
      <h2 class="welcome-title">Hi, I'm ARIA ✦</h2>
      <p class="welcome-greeting">Upload your data and I'll answer anything about it.</p>
      <div class="welcome-steps">
        <div class="step"><span class="step-num">1</span>Upload a file, URL, or API</div>
        <div class="step"><span class="step-num">2</span>Or connect Gmail, GitHub &amp; more</div>
        <div class="step"><span class="step-num">3</span>Click Process &amp; Chat</div>
        <div class="step"><span class="step-num">4</span>Ask me anything</div>
      </div>
    </div>`;
}

/* ── Processing state ────────────────────────────────────────── */
function setProcessing(on) {
  state.isProcessing = on;
  const btn     = document.getElementById('process-btn');
  const btnText = btn.querySelector('.btn-text');
  const spinner = btn.querySelector('.btn-spinner');
  btn.disabled = on;
  btnText.classList.toggle('hidden', on);
  spinner.classList.toggle('hidden', !on);
}

/* ── Auto-resize textarea ────────────────────────────────────── */
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

/* ── Sidebar hamburger toggle (mobile) ──────────────────────── */
function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  const btn     = document.getElementById('sidebar-hamburger');
  sidebar.classList.toggle('collapsed');
  btn.classList.toggle('collapsed');
}

/* ── Toast ───────────────────────────────────────────────────── */
function toast(msg, type = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

/* ── Helpers ─────────────────────────────────────────────────── */
function escHtml(str) {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

async function safeJson(res) {
  try {
    return await res.json();
  } catch {
    return { detail: `Server error ${res.status}` };
  }
}
